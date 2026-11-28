"""`lab evaluate DIR`: the first measurement of a project folder (spec §3.3, §3.3.1; ADR-0032 decisions 3-6).

The flow is the one of ``examples/owner/evaluation.py``: Bundle, a target for
conformance (when ``launch.json`` declares it), a fresh target for the fixed
evaluation, stop, and a check that nothing is left running. Every step goes on
only on the facts that script checks; the exit code says which kind of fact
failed (ADR-0032 p.5): 0 accepted, 1 the run was not accepted, 2 a project error
before the start, 3 a target that would not start, or a Lab or environment failure.

Secret values come only from ``environ`` and go only to Owner calls; they are
never written, returned or printed.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path

from typing import Any, Mapping, Sequence

from evolution_lab._paths import canonical_root
from evolution_lab.quickstart._errors import QuickstartError
from evolution_lab.quickstart._resolve import EVALUATION, LAUNCH, Resolution, resolve_project

SUMMARY_SCHEMA = "lab-evaluate-summary/1"
_SCHEMA_FILE = "lab-evaluate-summary-1.schema.json"
ACTIVE_MARKER = ".active"
_RUNNING = {"reserved", "running", "stopping"}
# Preparation refusals that mean "your project files", as opposed to Lab or the environment.
_PROJECT_CODES = {
    "invalid_request", "unsupported_schema", "unsupported_input", "manifest_invalid", "reserved_public_input",
    "reserved_environment_name", "linked_run_unsupported", "grouper_kind_unsupported", "corpus_inside_role_workspace",
}


AGENT_STDERR_LINES = 40


@dataclass(frozen=True)
class AgentStderr:
    """The last lines the target process (the user's adapter and agent) wrote to stderr, secrets redacted."""

    path: Path
    lines: tuple[str, ...]


@dataclass(frozen=True)
class EvaluateResult:
    """What one ``lab evaluate`` run ended with; ``summary`` is ``lab-evaluate-summary/1``.

    ``agent_stderr`` is set on a run that was not accepted when the target process of this run wrote to
    stderr: an adapter that does not import, or ``build``/``factory`` raising, shows up only there."""

    exit_code: int
    run_dir: Path
    summary: dict[str, Any]
    reason: str | None
    resolution: Resolution | None = None
    rows: tuple[dict[str, str], ...] = ()
    log: tuple[str, ...] = ()
    agent_stderr: AgentStderr | None = None


def summary_schema() -> dict[str, Any]:
    """The JSON Schema of ``lab-evaluate-summary/1``, shipped in the wheel."""
    return json.loads((resources.files("evolution_lab.quickstart") / _SCHEMA_FILE).read_text(encoding="utf-8"))


def pair_label(lab_version: str, core_version: str) -> str:
    """``dev72-b17`` for Lab ``0.0.3.dev72`` and Core ``2.0.0b17``.

    This mirrors the justfile ``pair`` rule: the Lab version after its last dot, then the Core version
    without its leading release numbers. Where a part comes out empty (a final Core release such as
    ``2.1.0``), the full version stands in, as the justfile does, so ``just demo`` and ``lab evaluate``
    name the same ``state-<pair>``."""
    lab = lab_version.rsplit(".", 1)[-1] or lab_version
    core = re.sub(r"^[0-9.]+", "", core_version) or core_version
    return f"{lab}-{core}"


def installed_versions() -> tuple[str, str]:
    return metadata.version("evolution-lab"), metadata.version("evo-skills")


# --- step 1: the project files and the secrets of both roles -------------------------------------------------

def _load(project: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads((project / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QuickstartError("project_invalid", f"{name} is missing in {project}", file=name) from None
    except (OSError, ValueError) as error:
        raise QuickstartError("project_invalid", f"{name} is not valid JSON: {error}", file=name) from None
    if not isinstance(value, dict):
        raise QuickstartError("project_invalid", f"{name} must be a JSON object", file=name)
    return value


def _names(value: Any, name: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise QuickstartError("project_invalid", f"{name}:{field} must be a list of names", file=name, field=field)
    return tuple(value)


def secret_roles(launch: Mapping[str, Any], evaluation: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Target names (``launch.secret_names``) and scorer names (``scorer.allowed_env`` minus ``launch.env``)."""
    env = launch.get("env")
    if not isinstance(env, dict):
        raise QuickstartError("project_invalid", f"{LAUNCH}:env must be an object", file=LAUNCH, field="env")
    target = _names(launch.get("secret_names"), LAUNCH, "secret_names")
    scorer = evaluation.get("scorer")
    if not isinstance(scorer, dict):
        raise QuickstartError("project_invalid", f"{EVALUATION}:scorer must be an object", file=EVALUATION,
                              field="scorer")
    allowed = _names(scorer.get("allowed_env"), EVALUATION, "scorer.allowed_env")
    return target, tuple(name for name in allowed if name not in env)


def _secret_values(environ: Mapping[str, str], names: Sequence[str], role: str, file: str) -> dict[str, str]:
    for name in names:
        if name not in environ:
            raise QuickstartError(
                "secret_missing", f"secret {name} ({role} role, {file}) is not set in the environment",
                file=file, field=name, role=role)
    return {name: environ[name] for name in names}


# Core's evaluation corpus loader is the one source of the corpus rules (the loader preparation uses). Lab only
# runs it before anything starts and says its ``CorpusError`` in plain words, naming the file and the line.
_CORPUS_WORDS = {
    "not_json": "not valid JSON",
    "not_object": "not a JSON object",
    "missing_key": "a required field is missing",
    "unknown_key": "a field the corpus does not accept (reflection_metadata belongs only in a campaign corpus)",
    "bad_type": "a field has the wrong type",
    "bad_value": "a field has a value the corpus does not accept",
    "duplicate_case_id": "a case_id an earlier line already uses",
    "empty_corpus": "the corpus has no cases",
    "unreadable": "the file cannot be read as UTF-8 text",
}


def corpus_problem(code: str, line: int) -> str:
    """Core's ``CorpusError`` in plain words: ``line 2: a case_id an earlier line already uses (duplicate_case_id)``;
    a file-wide error (line 0) has no line. A code Lab has no words for is named as it is."""
    words = _CORPUS_WORDS.get(code, code.replace("_", " "))
    return (f"line {line}: " if line > 0 else "") + f"{words} ({code})"


def check_corpus(project: Path, evaluation: Mapping[str, Any]) -> None:
    """Refuse a corpus the core refuses: ``corpus_invalid`` naming the file and, for a line, ``line N: <what>``."""
    from evolution.tree.execution.corpus import CorpusError, load_eval_corpus

    corpus = evaluation.get("corpus")
    name = corpus.get("path") if isinstance(corpus, Mapping) else None
    if not isinstance(name, str) or not name:
        return  # preparation refuses a malformed corpus declaration in evaluation-input.json
    path = project / name
    if not path.exists():
        raise QuickstartError("corpus_invalid", f"{name}: the file is missing ({EVALUATION} corpus.path)",
                              file=name)
    try:
        load_eval_corpus(path)
    except CorpusError as error:
        problem = corpus_problem(str(error.code), int(error.line))
        raise QuickstartError("corpus_invalid", f"{name}: {problem}", file=name,
                              field=problem if error.line > 0 else None) from None


def check_scorer(project: Path, evaluation: Mapping[str, Any]) -> None:
    """A scorer directory or Python script that is not in the project, named before anything starts."""
    scorer = evaluation.get("scorer")
    cwd = scorer.get("cwd") if isinstance(scorer, Mapping) else None
    if not isinstance(cwd, str):
        return
    directory = project / cwd
    if not directory.is_dir():
        raise QuickstartError("project_invalid", f"{EVALUATION}:scorer.cwd: {cwd} is not a directory in the project",
                              file=EVALUATION, field="scorer.cwd")
    argv = scorer.get("argv")
    script = argv[1] if isinstance(argv, list) and len(argv) > 1 else None
    if (isinstance(script, str) and script.endswith(".py") and not script.startswith("-")
            and not (directory / script).is_file()):
        raise QuickstartError("project_invalid", f"{EVALUATION}:scorer.argv.1: {Path(cwd) / script} does not exist",
                              file=EVALUATION, field="scorer.argv.1")


def _check_arguments(wheels: Sequence[Path], python: Path | None) -> None:
    for wheel in wheels:
        if not wheel.is_file():
            raise QuickstartError("argument_invalid", f"--wheel {wheel}: no such file", field="--wheel")
    if python is not None and not python.is_file():
        raise QuickstartError("argument_invalid", f"--python {python}: no such file", field="--python")


def _wheel_pins(wheels: Sequence[Path]) -> list[dict[str, str]]:
    pins = []
    for wheel in wheels:
        digest = hashlib.sha256()
        with wheel.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        pins.append({"path": str(wheel), "sha256": digest.hexdigest()})
    return pins


_LINE_DIAGNOSIS = re.compile(r"\b([a-z_]+) at line (\d+)\s*$")
_REQUEST_CODES = {"invalid_request", "unsupported_schema", "unsupported_input"}


def _refusal_place(error, source: Path, corpus: str | None) -> tuple[str | None, str | None]:
    """Which project file a preparation refusal is about, when Lab can tell (preparation itself says not)."""
    from evolution_lab.preparation import PreparationError
    from evolution_lab.preparation.evaluation import load_evaluation_input

    if error.code == "artifacts_invalid":
        match = _LINE_DIAGNOSIS.search(str(error))
        if match is not None and corpus is not None:
            return corpus, f"line {match.group(2)}: {match.group(1)}"
        return None, None
    if error.code in _REQUEST_CODES:
        try:
            load_evaluation_input(source / EVALUATION, "bundle")
        except PreparationError:
            return EVALUATION, None
        except Exception:
            return None, None
        return f"{LAUNCH} or {EVALUATION}", None
    return None, None


# --- step 2-3: state, leftovers of earlier runs, the run directory --------------------------------------------

def _active_runs(runs: Path) -> list[int]:
    if not runs.is_dir():
        return []
    return sorted(int(path.parent.name) for path in runs.glob(f"*/{ACTIVE_MARKER}") if path.parent.name.isdigit())


def _allocate_run(runs: Path) -> tuple[int, Path]:
    runs.mkdir(mode=0o700, exist_ok=True)
    number = 1 + max((int(path.name) for path in runs.iterdir() if path.name.isdigit()), default=0)
    while True:
        run_dir = runs / f"{number:03d}"
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            number += 1
            continue
        (run_dir / ACTIVE_MARKER).write_text(json.dumps({"run": number}) + "\n", encoding="utf-8")
        return number, run_dir


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _clean_leftovers(owner, state: Path, runs: Path) -> list[int]:
    """Clean blockers of interrupted runs through Owner; name the runs that left them."""
    from evolution_lab.owner import OwnerError

    leftovers = _active_runs(runs)
    blockers = (await owner.health()).blockers
    tried: set = set()
    failure = None
    while blockers:
        ref = next((item for item in blockers if item not in tried), None)
        if ref is None:
            break
        tried.add(ref)
        try:
            status = await owner.wait(await owner.cleanup(ref))
            if status.state != "succeeded":
                failure = (ref, status.failure.code if status.failure is not None else status.state)
        except OwnerError as error:
            failure = (ref, error.code)
        blockers = (await owner.health()).blockers
    if blockers:
        ref, code = failure if failure is not None else (blockers[0], "cleanup_unconfirmed")
        raise QuickstartError(
            "cleanup_failed",
            f"leftovers of an earlier run in {state} were not cleaned up: {ref.kind} {ref.id} ({code}); "
            f"{len(blockers)} blocker(s) remain",
            file=str(state), field=code, exit_code=3)
    cleaned = leftovers if tried else []
    for number in leftovers:
        with contextlib.suppress(OSError):
            (runs / f"{number:03d}" / ACTIVE_MARKER).unlink()
    return cleaned


# --- steps 5-7: targets, conformance, evaluation ------------------------------------------------------------

class _Stop(Exception):
    """A step did not confirm its facts; ``exit_code`` is 1 or 3."""

    def __init__(self, exit_code: int, reason: str) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.reason = reason


def _scope_names(state: Path) -> set[str]:
    with contextlib.suppress(OSError):
        return {path.name for path in (state / "scopes").iterdir()}
    return set()


async def _start_target(owner, connection, secrets: Mapping[str, str], targets: list,
                        state: Path | None = None, scopes: list | None = None):
    """Start a target; the Owner scopes created by this start (the target process's logs) go to ``scopes``."""
    from evolution_lab.owner import OwnerError, TargetRef

    before = _scope_names(state) if state is not None else set()
    try:
        status = await owner.wait(await owner.start_target(connection, secrets=dict(secrets)))
    except OwnerError as error:
        raise _Stop(3, f"target start refused: {error.code}") from None
    finally:
        if state is not None and scopes is not None:
            scopes.append(sorted(_scope_names(state) - before))
    if status.ref is not None and status.ref.kind == "target":
        targets.append(TargetRef(status.ref.id, status.owner_generation))
    if status.state != "succeeded" or status.ref is None or status.ref.kind != "target":
        code = status.failure.code if status.failure is not None else status.state
        raise _Stop(3, f"target start not confirmed: {code}")
    return targets[-1]


async def _finished(owner, ref):
    status = await owner.status(ref)
    while status.control_state in _RUNNING:
        await asyncio.sleep(0.1)
        status = await owner.status(ref)
    return status


async def _conformance(owner, connection, target, summary: dict[str, Any]) -> None:
    from evolution_lab.owner import OwnerError

    try:
        status = await owner.wait(await owner.conformance(connection, target_ref=target))
    except OwnerError as error:
        raise _Stop(3, f"conformance refused: {error.code}") from None
    payload = (status.result.to_payload()["payload"]
               if status.result is not None and status.result.kind == "conformance" else None)
    report = payload.get("core_report") if isinstance(payload, dict) else None
    after = await owner.target(target)
    blockers = (await owner.health()).blockers
    failed = ([check.get("name") for check in report.get("checks", ())
               if isinstance(check, dict) and check.get("ok") is not True]
              if isinstance(report, dict) else [])
    summary["conformance"] = {
        "state": status.state,
        "failure": None if status.failure is None else status.failure.code,
        "core_ok": report.get("ok") if isinstance(report, dict) else None,
        "failed_checks": [str(name) for name in failed],
        "process": payload.get("process") if isinstance(payload, dict) else None,
        "cleanup": payload.get("cleanup") if isinstance(payload, dict) else None,
        "target_cleanup": payload.get("target_cleanup") if isinstance(payload, dict) else None,
        "persistence": status.persistence.to_payload(),
        "target_state": after.state,
    }
    summary["blocker_count"] = len(blockers)
    problems = []
    if status.state != "succeeded" or not isinstance(report, dict):
        problems.append(f"conformance task {status.state}"
                        + ("" if status.failure is None else f": {status.failure.code}"))
    for name in ("cleanup", "target_cleanup"):
        value = payload.get(name) if isinstance(payload, dict) else None
        if not isinstance(value, dict) or value.get("complete") is not True:
            problems.append(f"conformance {name} not confirmed")
    if after.state != "stopped":
        problems.append(f"conformance target is {after.state}, not stopped")
    if blockers:
        problems.append(f"{len(blockers)} blocker(s) after conformance")
    if problems:
        raise _Stop(3, "; ".join(problems))
    if report.get("ok") is not True:
        failed_checks = ", ".join(summary["conformance"]["failed_checks"]) or "core_report.ok is false"
        raise _Stop(1, f"conformance failed: {failed_checks}")


def _samples(core: Any, primary: str | None, unmeasured: Any = None) -> list[dict[str, Any]]:
    """One row per core sample. ``outcome`` is why a sample was not scored (``scorer_failed``,
    ``target_failed``, ...), from the core row or the result's ``unmeasured`` list; null when it was."""
    rows = core.get("samples") if isinstance(core, Mapping) else None
    outcomes = {(str(item.get("case_id")), str(item.get("sample_id"))): item.get("outcome")
                for item in unmeasured or () if isinstance(item, Mapping)}
    samples = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
        score = metrics.get(primary) if primary is not None else None
        key = (str(row.get("case_id")), str(row.get("sample_id")))
        verdict = row.get("evaluation") if isinstance(row.get("evaluation"), Mapping) else {}
        outcome = verdict.get("outcome") if verdict.get("kind") not in (None, "score") else outcomes.get(key)
        samples.append({
            "case_id": key[0],
            "sample_id": key[1],
            "score": score if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
            "execution_state": row.get("execution_state"),
            "scoring_state": row.get("scoring_state"),
            "outcome": outcome if isinstance(outcome, str) else None,
        })
    return samples


def _quality(samples: Sequence[Mapping[str, Any]], primary: str | None) -> dict[str, Any]:
    """The primary metric over the scored samples: its mean, and how many samples got 1 where every score is 0 or 1."""
    scores = [sample["score"] for sample in samples if sample["score"] is not None]
    binary = bool(scores) and all(score in (0, 1) for score in scores)
    return {"metric": primary, "mean": sum(scores) / len(scores) if scores else None, "scored": len(scores),
            "full_score_count": sum(1 for score in scores if score == 1) if binary else None}


def _coverage_ok(expected: int, scored: int, min_coverage: float) -> bool:
    """The comparison of ``report.py``: ``scored / expected >= min_coverage``. The result carries the input's
    threshold, or 1.0 (all samples) when the input declares none."""
    return expected > 0 and scored / expected >= min_coverage


async def _evaluation(owner, prepared, target, scorer_secrets: Mapping[str, str], summary: dict[str, Any],
                      operation: list) -> None:
    from evolution_lab.owner import OwnerError, ResourceRef

    try:
        accepted = await owner.start(prepared.digest, target_ref=target, secrets=dict(scorer_secrets))
    except OwnerError as error:
        raise _Stop(3, f"evaluation start refused: {error.code}") from None
    ref = ResourceRef("operation", accepted.id)
    operation.append(ref)
    status = await _finished(owner, ref)
    try:
        report_state = (await owner.report(ref)).state
    except OwnerError as error:
        report_state = f"unavailable: {error.code}"
    result = status.result
    primary = prepared.evaluation.metric_plan.get("primary")
    summary["evaluation"] = {
        "operation_id": ref.id,
        "control_state": status.control_state,
        "failure": None if status.failure is None else status.failure.code,
        "process": None if result is None else result.process.to_payload(),
        "coverage": None if result is None or result.coverage is None else result.coverage.to_payload(),
        "min_coverage": None if result is None else result.min_coverage,
        "stopped_reason": None if result is None else result.stopped_reason,
        "target_cleanup": status.target_cleanup.to_payload(),
        "persistence": None if result is None else result.persistence.to_payload(),
        "results_state": status.results_state,
        "report_state": report_state,
    }
    if result is not None and result.kind == "evaluation":
        name = primary if isinstance(primary, str) else None
        summary["samples"] = _samples(result.core, name, result.to_payload().get("unmeasured"))
        summary["quality"] = _quality(summary["samples"], name)


def _reports_provenance(project: Path) -> bool:
    from evolution_lab.quickstart._resolve import _get

    try:
        policy = _load(project, "campaign-launch.json").get("policy")
        if not isinstance(policy, str) or not policy:
            return False
        manifest = (project / policy).resolve(strict=True)
        if not manifest.is_relative_to(project):
            return False
        return _get(_load(project, str(manifest)), "campaign.gate.provenance.kind")[1] == "reported"
    except (OSError, RuntimeError, ValueError, QuickstartError):
        return False


def _evaluation_provenance(measurement: Path, project: Path) -> dict[str, Any] | None:
    from evolution.tree import measurement_provenance

    try:
        counts = measurement_provenance(measurement)
    except ValueError:
        return None
    notice = None
    if not counts.recorded:
        notice = "provenance_not_recorded"
    elif counts.reported == 0 and (counts.not_reported or counts.overflow) and _reports_provenance(project):
        notice = "baseline_provenance_unknown"
    return {"recorded": counts.recorded, "reported": counts.reported,
            "overflow": counts.overflow, "not_reported": counts.not_reported, "notice": notice}


def _evaluation_verdict(summary: dict[str, Any], status_result_kind: str | None,
                        has_core: bool) -> tuple[int, str | None]:
    evaluation = summary["evaluation"]
    lab, agent = [], []
    if evaluation["control_state"] != "finished":
        lab.append(f"evaluation ended {evaluation['control_state']}"
                   + ("" if evaluation["failure"] is None else f": {evaluation['failure']}"))
    if status_result_kind != "evaluation" or not has_core or evaluation["coverage"] is None:
        lab.append("no evaluation result with core and coverage")
    if evaluation["persistence"] is None or evaluation["persistence"]["state"] != "saved":
        lab.append("result not saved")
    if evaluation["report_state"] != "available":
        lab.append(f"report {evaluation['report_state']}")
    final = summary["final_target"]
    if final is None or final["state"] != "stopped":
        lab.append("target stop not confirmed")
    if summary["blocker_count"]:
        lab.append(f"cleanup not confirmed: {summary['blocker_count']} blocker(s) remain")
    coverage = evaluation["coverage"]
    counted = "" if coverage is None else f" (scored {coverage['scored']}/{coverage['expected']})"
    if evaluation["stopped_reason"]:
        agent.append(f"stopped: {evaluation['stopped_reason']}{counted}")
    elif coverage is not None and evaluation["min_coverage"] is not None:
        if not _coverage_ok(coverage["expected"], coverage["scored"], evaluation["min_coverage"]):
            agent.append(f"coverage below {evaluation['min_coverage']}{counted}")
    if lab:
        return 3, "; ".join(lab + agent)
    if agent:
        return 1, "; ".join(agent)
    return 0, None


async def _stop_target(owner, target) -> None:
    from evolution_lab.owner import OwnerError

    # Owner bounds the stop by the launch's stop_grace_seconds, then forces it.
    with contextlib.suppress(OwnerError):
        if (await owner.target(target)).state != "stopped":
            await owner.wait(await owner.stop_target(target))


async def _abort(owner, operation: list, targets: list, grace: float) -> None:
    """Ctrl+C: stop the evaluation and every target this run started, through Owner.

    A turn in flight can hold the evaluation for its whole timeout, so each
    wait is bounded by the launch's ``stop_grace_seconds``; then Owner closes
    within the same grace, which stops what is still running. Whatever it could
    not confirm stays a blocker that the next run cleans up.
    """
    from evolution_lab.owner import OwnerError

    await _stop_all(owner, operation, targets, grace, (OwnerError, TimeoutError))
    with contextlib.suppress(OwnerError):
        await owner.close(wait_seconds=grace)


async def _stop_all(owner, operation: list, targets: list, grace: float, quiet: tuple) -> None:
    """Stop this run's evaluation and targets, each wait bounded by ``grace``; ``quiet`` errors are skipped."""
    for ref in operation:
        with contextlib.suppress(*quiet):
            await owner.stop(ref)
            await asyncio.wait_for(_finished(owner, ref), grace)
    for target in targets:
        with contextlib.suppress(*quiet):
            if (await owner.target(target)).state != "stopped":
                await asyncio.wait_for(owner.wait(await owner.stop_target(target)), grace)


def _grace(launch: Mapping[str, Any]) -> float:
    value = launch.get("stop_grace_seconds")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 10.0


def _summary(number: int, pair: str, versions: tuple[str, str], state: Path, python: Path, digest: str,
             cleaned: list[int]) -> dict[str, Any]:
    return {
        "schema": SUMMARY_SCHEMA, "exit_code": 3, "reason": None, "run": number, "pair": pair,
        "lab_version": versions[0], "core_version": versions[1], "state": str(state),
        "environment": {"python": str(python), "target_environment": digest},
        "resolved": None, "cleaned_leftovers": cleaned, "prepared": None, "conformance": None,
        "evaluation": None, "final_target": None, "samples": [], "blocker_count": 0, "error": None,
    }


def _tail(path: Path, count: int) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 64 * 1024))
        data = handle.read()
    return data.decode("utf-8", errors="replace").splitlines()[-count:]


def _agent_stderr(state: Path, scopes: Sequence[Sequence[str]], secrets: Mapping[str, str]) -> AgentStderr | None:
    """The newest non-empty ``stderr.log`` of the targets this run started (the state layout, read only)."""
    from evolution_lab.quickstart._readout import redact

    for names in reversed(scopes):
        for name in reversed(names):
            path = state / "scopes" / name / "stderr.log"
            try:
                if path.stat().st_size == 0:
                    continue
                lines = _tail(path, AGENT_STDERR_LINES)
            except OSError:
                continue
            if any(line.strip() for line in lines):
                return AgentStderr(path, tuple(redact(lines, secrets)))
    return None


async def _finish(owner, summary: dict[str, Any], exit_code: int, reason: str | None, run_dir: Path,
                  resolution: Resolution | None, operation: Sequence, log: list[str],
                  secrets: Mapping[str, str], *, state: Path | None = None,
                  scopes: Sequence[Sequence[str]] = ()) -> EvaluateResult:
    """Fix the verdict and write the run files while Owner is still open."""
    from evolution_lab.quickstart._readout import redact, redact_summary, write_run_files

    summary["exit_code"], summary["reason"] = exit_code, reason
    # A sanitized copy: only the free text values are rewritten, never the keys or schema-owned values.
    summary = redact_summary(summary, secrets)
    reason = summary["reason"]
    log.append(f"result: exit {exit_code}" + ("" if reason is None else f" ({reason})"))
    stderr = _agent_stderr(state, scopes, secrets) if exit_code != 0 and state is not None else None
    if stderr is not None:
        log.append(f"the agent's process wrote (last lines of {stderr.path}):")
        log.extend(f"    {line}" for line in stderr.lines)
    rows = await write_run_files(owner, operation[-1] if operation else None, run_dir, resolution, summary,
                                 log=log, secrets=secrets)
    return EvaluateResult(exit_code, run_dir, summary, reason, resolution, tuple(rows),
                          tuple(redact(log, secrets)), stderr)


async def evaluate(project: Path, *, wheels: Sequence[Path], python: Path | None = None,
                   environ: Mapping[str, str] | None = None) -> EvaluateResult:
    """Run the first measurement of ``project`` and say whether it is accepted.

    Errors found before a run directory exists (project files, the corpus,
    arguments, secrets, environment, leftovers that could not be cleaned) raise
    ``QuickstartError`` with ``exit_code`` 2 or 3. From then on the result
    carries the exit code. Any other failure raises ``QuickstartError``
    ``unexpected_error`` (exit code 3) with ``state`` and ``run_dir`` when they
    are known; the original exception is its ``__cause__``.
    ``asyncio.CancelledError`` stops this run's targets and task through Owner
    and propagates.
    """
    from evolution_lab.quickstart._readout import redact

    where: dict[str, Any] = {"state": None, "run_dir": None, "secrets": {}}
    try:
        return await _evaluate(project, wheels=wheels, python=python, environ=environ, where=where)
    except (QuickstartError, asyncio.CancelledError):
        raise
    except Exception as error:
        message = redact(f"{type(error).__name__}: {error}", where["secrets"])
        raise QuickstartError(
            "unexpected_error", f"lab evaluate stopped on an unexpected error: {message}", exit_code=3,
            state=None if where["state"] is None else str(where["state"]),
            run_dir=None if where["run_dir"] is None else str(where["run_dir"])) from error


async def _evaluate(project: Path, *, wheels: Sequence[Path], python: Path | None,
                    environ: Mapping[str, str] | None, where: dict[str, Any]) -> EvaluateResult:
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.preparation import (EnvironmentInput, EvaluationSource, PreparationError, PreparationStore,
                                           SourceRoot, describe_environment)

    try:
        project = canonical_root(project)
    except ValueError:
        raise QuickstartError('project_invalid', 'invalid project directory') from None
    if project.is_symlink():
        raise QuickstartError('project_invalid', f'{project} is a symlinked project directory')
    environ = os.environ if environ is None else environ
    if not project.is_dir():
        raise QuickstartError("project_invalid", f"{project} is not a project directory")
    wheels = tuple(Path(wheel).absolute() for wheel in wheels)
    if not wheels:
        raise QuickstartError("wheels_missing", "pass the Lab and Core wheels with --wheel (just evaluate does)")
    _check_arguments(wheels, None if python is None else Path(python).absolute())
    launch, evaluation = _load(project, LAUNCH), _load(project, EVALUATION)
    target_names, scorer_names = secret_roles(launch, evaluation)
    target_secrets = _secret_values(environ, target_names, "target", LAUNCH)
    scorer_secrets = _secret_values(environ, scorer_names, "scorer", EVALUATION)
    where["secrets"] = {**target_secrets, **scorer_secrets}
    check_corpus(project, evaluation)
    check_scorer(project, evaluation)

    execution = Path(sys.executable).absolute()
    python = execution if python is None else Path(python).absolute()
    execution_input = EnvironmentInput(execution, wheels)
    inputs = (execution_input,) if python == execution else (execution_input, EnvironmentInput(python, wheels))
    try:
        descriptors = [await asyncio.to_thread(describe_environment, value) for value in inputs]
    except PreparationError as error:
        raise QuickstartError(error.code, f"the Python environment check failed: {error}",
                              exit_code=3) from None
    target_environment = descriptors[-1]
    versions = installed_versions()
    pair = pair_label(*versions)
    lab_dir = project / ".lab"
    state, runs = lab_dir / f"state-{pair}", lab_dir / "runs"
    where["state"] = state
    pins = await asyncio.to_thread(_wheel_pins, wheels)
    lab_dir.mkdir(mode=0o700, exist_ok=True)

    try:
        async with contextlib.AsyncExitStack() as stack:
            try:
                owner = await stack.enter_async_context(Owner.open(state))
            except OwnerError as error:
                raise QuickstartError(error.code, f"cannot open the state {state}: {error}", file=str(state),
                                      exit_code=3) from None
            cleaned = await _clean_leftovers(owner, state, runs)
            number, run_dir = _allocate_run(runs)
            where["run_dir"] = run_dir
            source = run_dir / "source"
            try:
                resolution = await asyncio.to_thread(
                    resolve_project, project, source, environment=target_environment, port=_free_port(),
                    lab_version=versions[0], core_version=versions[1], pair=pair, python=str(python),
                    wheels=pins)
            except BaseException:
                shutil.rmtree(run_dir, ignore_errors=True)
                raise
            log = [f"run {number:03d}: state {state}"]
            if cleaned:
                log.append("cleaned leftovers of run " + ", ".join(f"{n:03d}" for n in cleaned))
            log.append("resolved lab:auto fields: " + (", ".join(sorted(resolution.substituted)) or "none"))
            summary = _summary(number, pair, versions, state, python, target_environment.digest, cleaned)
            summary["resolved"] = resolution.payload()
            resolved_launch = json.loads((source / LAUNCH).read_text(encoding="utf-8"))
            summary["conformance_declared"] = resolved_launch.get("conformance") is not None
            try:
                store = await asyncio.to_thread(PreparationStore, state)
                bundle = await asyncio.to_thread(
                    store.create_bundle, source=EvaluationSource(source / EVALUATION, source / LAUNCH),
                    environments=inputs, env=dict(resolved_launch["env"]),
                    secret_names=sorted(set(target_names) | set(scorer_names)),
                    roots=(SourceRoot("project", source),))
                prepared = await asyncio.to_thread(store.prepare_evaluation, bundle.digest,
                                                   selection={"all": True})
            except PreparationError as error:
                corpus = evaluation.get("corpus")
                file, field = _refusal_place(error, source,
                                             corpus.get("path") if isinstance(corpus, Mapping) else None)
                summary["error"] = {"code": error.code, "message": str(error), "file": file, "field": field}
                _drop_marker(run_dir)
                log.append(f"preparation refused: {error.code}")
                place = "" if file is None else f" ({file}" + ("" if field is None else f", {field}") + ")"
                # Invalid artifacts are the project's mistake only when the diagnosis names a project file
                # (a corpus line); otherwise they are a Lab or environment failure.
                project_error = error.code in _PROJECT_CODES or (error.code == "artifacts_invalid"
                                                                 and file is not None)
                return await _finish(owner, summary, 2 if project_error else 3,
                                     f"preparation refused{place}: {error}", run_dir, resolution, [], log,
                                     where["secrets"])
            summary["prepared"] = {
                "kind": prepared.kind, "case_count": len(prepared.evaluation.case_ids),
                "repeat_count": len(prepared.evaluation.repeat_seeds),
                "min_coverage": prepared.evaluation.min_coverage,
            }
            log.append(f"prepared: {summary['prepared']['case_count']} case(s) x "
                       f"{summary['prepared']['repeat_count']} repeat(s)")
            targets: list = []
            operation: list = []
            scopes: list = []
            try:
                exit_code, reason = await _measure(owner, bundle, prepared, resolved_launch, target_secrets,
                                                   scorer_secrets, summary, targets, operation, log,
                                                   state=state, scopes=scopes)
            except asyncio.CancelledError:
                await asyncio.shield(_abort(owner, operation, targets, _grace(resolved_launch)))
                raise
            except (_Stop, OwnerError) as error:
                stop = error if isinstance(error, _Stop) else _Stop(3, f"Owner refused: {error.code}")
                for target in targets:
                    await _stop_target(owner, target)
                summary["blocker_count"] = len((await owner.health()).blockers)
                exit_code, reason = stop.exit_code, stop.reason
                if summary["blocker_count"]:
                    exit_code = 3
                    reason = f"{reason}; cleanup not confirmed: {summary['blocker_count']} blocker(s) remain"
            except Exception:
                # Not a fact a step checks: stop what this run started, then report it as unexpected.
                await asyncio.shield(_stop_all(owner, operation, targets, _grace(resolved_launch), (Exception,)))
                raise
            if summary["evaluation"] is not None:
                identifier = summary["evaluation"]["operation_id"]
                measurement = state / "artifacts" / identifier / identifier / "measurement"
                provenance = await asyncio.to_thread(_evaluation_provenance, measurement, source)
                if provenance is not None:
                    summary["provenance"] = provenance
            if not summary["blocker_count"]:
                _drop_marker(run_dir)
            return await _finish(owner, summary, exit_code, reason, run_dir, resolution, operation, log,
                                {**target_secrets, **scorer_secrets}, state=state, scopes=scopes)
    except asyncio.CancelledError:
        print(f"lab evaluate: interrupted; the state is in {state}", file=sys.stderr, flush=True)
        raise


async def _measure(owner, bundle, prepared, launch, target_secrets, scorer_secrets, summary, targets,
                   operation, log: list[str], *, state: Path | None = None,
                   scopes: list | None = None) -> tuple[int, str | None]:
    """Steps 5-7: conformance when declared, a fresh target, the fixed evaluation, the final stop."""
    connection = await owner.bind(bundle.digest)
    if launch.get("conformance") is not None:
        log.append("conformance: starting the target")
        first = await _start_target(owner, connection, target_secrets, targets, state, scopes)
        try:
            await _conformance(owner, connection, first, summary)
        finally:
            conformance = summary["conformance"]
            if conformance is not None:
                log.append(f"conformance: {conformance['state']}, core ok {conformance['core_ok']}"
                           + (f", failed {', '.join(conformance['failed_checks'])}"
                              if conformance["failed_checks"] else ""))
    log.append("evaluation: starting a fresh target")
    second = await _start_target(owner, connection, target_secrets, targets, state, scopes)
    if len(targets) > 1 and second.id == targets[0].id:
        raise _Stop(3, "the evaluation reused the conformance target")
    log.append("evaluation: running")
    try:
        await _evaluation(owner, prepared, second, scorer_secrets, summary, operation)
    finally:
        if summary["evaluation"] is not None:
            coverage = summary["evaluation"]["coverage"]
            log.append(f"evaluation: {summary['evaluation']['control_state']}"
                       + ("" if coverage is None else f", scored {coverage['scored']}/{coverage['expected']}"))
    status = await owner.status(operation[-1])
    await _stop_target(owner, second)
    after = await owner.target(second)
    summary["final_target"] = {"state": after.state, "cleanup": after.cleanup.to_payload()}
    log.append(f"final target: {after.state}")
    summary["blocker_count"] = len((await owner.health()).blockers)
    result = status.result
    return _evaluation_verdict(summary, None if result is None else result.kind,
                               result is not None and result.core is not None)


def _drop_marker(run_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (run_dir / ACTIVE_MARKER).unlink()
