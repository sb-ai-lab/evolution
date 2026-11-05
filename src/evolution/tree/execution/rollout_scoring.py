"""Read-only subprocess scoring for Evolution v2 trajectories."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from evolution.eval.proc import run_capture
from evolution.tree.execution.command_binding import CommandBinding, CommandChanged
from evolution.tree.execution.interaction import (
    CandidateView,
    EvalCase,
    ScoreResult,
    ScorerIdentity,
    ScoringView,
    validate_score_result,
)
from evolution.tree.execution.interaction import ScoringError as ScoringError
from evolution.tree.execution.sample_outcomes import classify_sample_outcome
from evolution.tree.execution.scorer_trace_writer import PackedRecord, PackedTraceWriter
from evolution.tree.execution.scorer_traces import PACK_ID, TRACE_ID, TraceStatus
from evolution.tree.storage.journal_errors import JournalBoundaryError

# A short safe trace identifier, shared by packed and historical directory evidence.
_TRACE_ID = TRACE_ID

MAX_VERDICTS = 32
MAX_VERDICT_NOTE_CHARS = 256
CRITERION = re.compile(r"[A-Za-z0-9._-]{1,64}")
_VERDICT_KEYS = frozenset({"criterion", "passed", "note"})
_VERDICT_REQUIRED = frozenset({"criterion", "passed"})


def parse_verdicts(value: object) -> tuple[dict[str, Any], ...]:
    """The closed verdict grammar: criterion, passed, optional note."""

    if not isinstance(value, list):
        raise ScoringError("subprocess scorer verdicts must be an array", code="output_invalid")
    if len(value) > MAX_VERDICTS:
        raise ScoringError(
            f"subprocess scorer verdicts exceed {MAX_VERDICTS}", code="output_invalid"
        )
    seen: set[str] = set()
    parsed: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not (_VERDICT_REQUIRED <= item.keys() <= _VERDICT_KEYS):
            raise ScoringError(
                "subprocess scorer verdict has keys outside the grammar", code="output_invalid"
            )
        criterion = item["criterion"]
        passed = item["passed"]
        if not isinstance(criterion, str) or CRITERION.fullmatch(criterion) is None:
            raise ScoringError(
                "subprocess scorer verdict criterion is not an identifier", code="output_invalid"
            )
        if type(passed) is not bool:
            raise ScoringError(
                "subprocess scorer verdict passed is not a boolean", code="output_invalid"
            )
        if criterion in seen:
            raise ScoringError(
                "subprocess scorer verdict criterion is duplicated", code="output_invalid"
            )
        seen.add(criterion)
        note = item.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note) > MAX_VERDICT_NOTE_CHARS:
                raise ScoringError(
                    "subprocess scorer verdict note is not a bounded string", code="output_invalid"
                )
            if any(ord(char) < 32 and char != "\n" for char in note):
                raise ScoringError(
                    "subprocess scorer verdict note carries a control character",
                    code="output_invalid",
                )
        parsed.append({"criterion": criterion, "passed": passed, "note": note})
    return tuple(parsed)


MAX_DIAGNOSTICS_BYTES: Final = 64 * 1024
MAX_DIAGNOSTICS_DEPTH: Final = 8


def _diagnostics_too_deep(value: object) -> bool:
    """Whether `value` nests objects/arrays deeper than `MAX_DIAGNOSTICS_DEPTH`.

    Iterative, and it stops at the first container past the limit, so a
    pathologically nested value costs no recursion.
    """

    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth + 1 > MAX_DIAGNOSTICS_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def parse_diagnostics(value: object) -> Mapping[str, Any]:
    """ADR-0021: the scorer's Reflector-addressed object, bounded and finite."""

    if not isinstance(value, Mapping):
        raise ScoringError("subprocess scorer diagnostics must be an object", code="output_invalid")
    # Depth first: it bounds the recursion the encoder below needs.
    if _diagnostics_too_deep(value):
        raise ScoringError("subprocess scorer diagnostics exceed depth 8", code="output_invalid")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ScoringError(
            "subprocess scorer diagnostics must be finite JSON", code="output_invalid"
        ) from exc
    if len(encoded) > MAX_DIAGNOSTICS_BYTES:
        raise ScoringError(
            "subprocess scorer diagnostics exceed 65536 bytes", code="output_invalid"
        )
    return value


@dataclass(frozen=True)
class SubprocessScorer:
    """Run a declared scorer with narrow input and verified Candidate bytes.

    The command is a :class:`CommandBinding`: the program, its resolved paths
    and its child environment are fixed once, at construction. A caller that
    needs a binding of its own (a seed-checked one, say) passes ``binding``;
    the declaration must then agree with it, field for field, and the resolved
    values are taken from the binding.
    """

    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    trace_root: Path
    dependency_paths: tuple[Path, ...] = ()
    allowed_env: frozenset[str] = frozenset()
    audit: Callable[[dict[str, Any]], None] | None = field(default=None, compare=False, repr=False)
    binding: CommandBinding | None = field(default=None, compare=False, repr=False)
    #: Instance digests of the run-directory hygiene findings the operator has
    #: accepted (PDR-0010). Empty by default, so a scorer bound without a
    #: decision re-admits its trace root exactly as it always did.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    #: Family 8 only (ADR-0021): whether this scorer's output grammar admits
    #: `diagnostics`. False by default, so every existing caller is unchanged.
    accepts_diagnostics: bool = field(default=False, kw_only=True)
    identity: ScorerIdentity = field(init=False)
    _sealed_proof: Mapping[str, Any] = field(init=False, repr=False, compare=False)
    _proof_paths: tuple[Path, ...] = field(init=False, repr=False, compare=False)

    _writers: dict[str, PackedTraceWriter] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _writers_lock: Any = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    _untraced_pack_id: str = field(
        default_factory=lambda: "untraced-" + uuid4().hex, init=False, repr=False, compare=False
    )

    def _close_traces(self) -> None:
        """End this product-owned binding's sessions, including faulted ones."""
        with self._writers_lock:
            for writer in self._writers.values():
                writer.close()
            self._writers.clear()

    def _writer(self, pack_id: str) -> PackedTraceWriter:
        with self._writers_lock:
            if pack_id not in self._writers:
                self._writers[pack_id] = PackedTraceWriter(
                    self.trace_root, pack_id, admitted_hygiene=self.admitted_hygiene
                )
            return self._writers[pack_id]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("scorer timeout_seconds must be positive and finite")
        binding = self.binding
        if binding is None:
            binding = CommandBinding(
                argv=tuple(self.argv),
                cwd=Path(self.cwd),
                dependency_paths=tuple(self.dependency_paths),
                allowed_env=frozenset(self.allowed_env),
            )
        elif (
            tuple(self.argv) != binding.argv
            or Path(self.cwd).resolve() != binding.cwd
            or {Path(path).resolve() for path in self.dependency_paths}
            != set(binding.dependency_paths)
            or frozenset(self.allowed_env) != binding.allowed_env
        ):
            raise ValueError("scorer declaration must match the supplied binding")
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "argv", binding.argv)
        object.__setattr__(self, "cwd", binding.cwd)
        object.__setattr__(self, "trace_root", Path(self.trace_root).resolve())
        object.__setattr__(self, "dependency_paths", binding.dependency_paths)
        object.__setattr__(self, "allowed_env", binding.allowed_env)
        object.__setattr__(self, "_proof_paths", binding.proof_paths)
        proof = self._command_proof()
        object.__setattr__(self, "_sealed_proof", proof)
        object.__setattr__(self, "identity", ScorerIdentity("subprocess", _json_digest(proof)))

    @property
    def command(self) -> CommandBinding:
        """The bound command; ``__post_init__`` always leaves one behind."""

        binding = self.binding
        if binding is None:  # pragma: no cover - unreachable after __post_init__
            raise ScoringError("scorer command binding is unset")
        return binding

    @property
    def recovery_mode(self) -> str:
        """The sealed subprocess command supports deterministic recomputation."""
        return "deterministic"

    async def score(
        self, case: EvalCase, view: ScoringView, candidate: CandidateView
    ) -> ScoreResult:
        return await self._score(
            case, view, candidate, trace_id=None, pack_id=self._untraced_pack_id
        )

    async def score_traced(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        *,
        trace_id: str,
        pack_id: str,
    ) -> ScoreResult:
        """Score a fresh try in the caller's measurement journal pack."""
        if not isinstance(pack_id, str) or PACK_ID.fullmatch(pack_id) is None:
            raise ValueError("pack id is not a safe file name")
        if not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id):
            raise ScoringError("scorer trace id must be one short path segment")
        return await self._score(case, view, candidate, trace_id=trace_id, pack_id=pack_id)

    async def _score(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        *,
        trace_id: str | None,
        pack_id: str,
    ) -> ScoreResult:
        if classify_sample_outcome(view.terminal) != "score":
            raise ScoringError(f"terminal {view.terminal!r} is not scoreable")
        candidate.verify()
        self._assert_command_outside_candidate(candidate)
        fault = False
        try:
            try:
                result = await self._score_in_worker(case, view, candidate, trace_id, pack_id)
            except JournalBoundaryError:
                # ADR-0040 D7: trace storage faults bypass scorer-error handling.
                fault = True
                raise
            except ScoringError:
                raise
            except Exception as exc:
                raise ScoringError("subprocess scorer failed", code="process_failed") from exc
        finally:
            try:
                candidate.verify()
            except Exception:
                if not fault:
                    raise
        try:
            validate_score_result(case, result)
        except ValueError as exc:
            raise ScoringError(
                f"subprocess scorer critical_pass is invalid: {exc}", code="output_invalid"
            ) from exc
        return result

    async def _score_in_worker(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        trace_id: str | None,
        pack_id: str,
    ) -> ScoreResult:
        worker = asyncio.create_task(
            asyncio.to_thread(self._score_sync, case, view, candidate, trace_id, pack_id)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    cancellation.add_note(f"Scorer cleanup failure: {type(exc).__name__}")
            if not worker.cancelled():
                failure = worker.exception()
                if failure is not None:
                    cancellation.add_note(f"Scorer cleanup failure: {type(failure).__name__}")
            raise

    def _score_sync(
        self,
        case: EvalCase,
        view: ScoringView,
        candidate: CandidateView,
        trace_id: str | None,
        pack_id: str,
    ) -> ScoreResult:
        try:
            self.command.verify()
        except CommandChanged as exc:
            raise ScoringError("scorer command proof changed before execution") from exc
        payload = {
            "case_id": case.case_id,
            "public": _json_value(case.public),
            "oracle": _json_value(case.oracle),
            "view": view.to_payload(),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        input_bytes = serialized.encode()
        writer = self._writer(pack_id)
        record_id = trace_id if trace_id is not None else uuid4().hex
        request = {
            "scorer": self.identity.digest,
            "trace_id": trace_id,
            "input": payload,
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        }
        # Preserve atomic_write_private_json's exact historical representation.
        writer.append_request(
            record_id,
            (json.dumps(request, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8"),
        )
        started = time.monotonic()
        fault = False
        try:
            with writer.streams() as (stdout_trace, stderr_trace):
                execution_error: BaseException | None = None
                status: TraceStatus = "completed"
                returncode: int | None = None
                try:
                    completed = run_capture(
                        list(self.command.spawn_argv()),
                        cwd=self.cwd,
                        env={**self.command.child_env, "EVO_CANDIDATE_ROOT": str(candidate.root)},
                        input=serialized,
                        timeout=float(self.timeout_seconds),
                        stdout_sink=stdout_trace,
                        stderr_sink=stderr_trace,
                    )
                    returncode = completed.returncode
                    status = "completed" if returncode == 0 else "failed"
                except JournalBoundaryError:
                    raise
                except subprocess.TimeoutExpired as exc:
                    execution_error = exc
                    status = "timeout"
                except OSError as exc:
                    execution_error = exc
                    status = "start_failed"
                output_record = writer.append_output(
                    record_id, stdout_trace, stderr_trace, status=status, returncode=returncode
                )
            self._audit(status, output_record, input_bytes, started, returncode=returncode)
            if isinstance(execution_error, subprocess.TimeoutExpired):
                raise ScoringError(
                    "subprocess scorer timed out", code="timeout"
                ) from execution_error
            if execution_error is not None:
                raise ScoringError(
                    "subprocess scorer could not be started", code="process_failed"
                ) from execution_error
        except JournalBoundaryError:
            fault = True
            raise
        finally:
            try:
                self.command.verify()
            except CommandChanged as exc:
                if not fault:
                    raise ScoringError("scorer command proof changed during execution") from exc
        if completed.returncode:
            raise ScoringError(
                f"subprocess scorer exited {completed.returncode}", code="process_failed"
            )
        output = _parse_json_object(completed.stdout)
        return _parse_score_result(output, diagnostics=self.accepts_diagnostics)

    def _assert_command_outside_candidate(self, candidate: CandidateView) -> None:
        for path in self._proof_paths:
            if path.is_relative_to(candidate.root):
                raise ScoringError("scorer command must be outside the Candidate")

    def _command_proof(self) -> dict[str, Any]:
        return self.command.identity_proof(timeout_seconds=float(self.timeout_seconds))

    def _audit(
        self,
        status: str,
        output_record: PackedRecord,
        input_bytes: bytes,
        started: float,
        *,
        returncode: int | None = None,
    ) -> None:
        if self.audit is None:
            return
        trace = {
            stream: {
                "path": str(output_record.path.relative_to(self.trace_root)),
                "offset": location.offset,
                "bytes": location.length,
                "sha256": location.digest,
            }
            for stream, location in output_record.sections.items()
        }
        record: dict[str, Any] = {
            "event": "scorer_command",
            "scorer_digest": self.identity.digest,
            "status": status,
            "input_bytes": len(input_bytes),
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "trace": trace,
            "duration_seconds": time.monotonic() - started,
        }
        if returncode is not None:
            record["returncode"] = returncode
        self.audit(record)


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        output = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_fields,
        )
    except _DuplicateJsonField as exc:
        raise ScoringError(str(exc), code="output_invalid") from exc
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        # A pathologically nested document costs the parser a `RecursionError`,
        # which is still only a document this contract refuses to read.
        raise ScoringError(
            "subprocess scorer stdout must be one JSON object", code="output_invalid"
        ) from exc
    if not isinstance(output, dict):
        raise ScoringError(
            "subprocess scorer stdout must be one JSON object", code="output_invalid"
        )
    return output


def parse_scorer_stdout(raw: str) -> dict[str, Any]:
    """The scorer's one JSON object, read with the same strictness `score` applies.

    Public for the brief's observation projection, which
    reads `public_feedback` from a trace's `stdout` and must not accept a
    document the scorer contract would have refused.
    """

    return _parse_json_object(raw)


def _parse_score_result(output: Mapping[str, Any], *, diagnostics: bool = False) -> ScoreResult:
    allowed = {"metrics", "critical_pass", "verdicts", "public_feedback", "details"}
    if diagnostics:
        allowed = allowed | {"diagnostics"}
    if set(output) - allowed:
        raise ScoringError("subprocess scorer output has unknown fields", code="output_invalid")
    metrics = output.get("metrics")
    verdicts = parse_verdicts(output.get("verdicts", []))
    public_feedback = output.get("public_feedback", {})
    details = output.get("details", {})
    parsed_diagnostics = (
        parse_diagnostics(output["diagnostics"]) if "diagnostics" in output else None
    )
    if not isinstance(metrics, Mapping):
        raise ScoringError("subprocess scorer metrics must be an object", code="output_invalid")
    if not isinstance(public_feedback, Mapping):
        raise ScoringError(
            "subprocess scorer public_feedback must be an object", code="output_invalid"
        )
    if not isinstance(details, Mapping):
        raise ScoringError("subprocess scorer details must be an object", code="output_invalid")
    try:
        return ScoreResult(
            metrics,
            critical_pass=output.get("critical_pass"),
            verdicts=verdicts,
            public_feedback=public_feedback,
            details=details,
            diagnostics=parsed_diagnostics,
        )
    except (TypeError, ValueError) as exc:
        typed = exc if isinstance(exc, ScoringError) else None
        raise ScoringError(
            "subprocess scorer returned an invalid ScoreResult",
            code=typed.code if typed else "output_invalid",
            metric=typed.metric if typed else None,
        ) from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class _DuplicateJsonField(ValueError):
    pass


def _object_without_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonField(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _close_scorer_traces(scorer: object) -> None:
    """Release sessions only at a product-owned subprocess binding boundary."""
    if isinstance(scorer, SubprocessScorer):
        scorer._close_traces()
