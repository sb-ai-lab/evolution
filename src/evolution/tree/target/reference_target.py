"""The reference `/evolution/v2` target: `ReferenceBackend`, the demonstration
behaviour (the first line of `skills/SKILL.md` plus the message), bound to the
`TargetServer` library (ADR-0009). The wire mechanism lives in
`target_server.py`; the names the suites import from here are re-exported.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, Entry
from evolution.tree.execution.interaction import Message, SeedMode, TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.target.target_server import (  # noqa: F401 — re-exported for the suites
    NEUTRAL_MISBEHAVE,
    RETRYABLE,
    BackendReply,
    LoadedTree,
    Misbehave,
    TargetServer,
    _error_payload,
    serve_target,
)

NO_ATTESTED_SKILL = "reference target: no attested skill"


def _skill_line(
    root: Path, artifact: tuple[Entry, ...], dependency: tuple[Entry, ...]
) -> tuple[str, tuple[str, ...]]:
    """The first line of `skills/SKILL.md`, derived only from the attested artifact set.

    Read once at instance creation: the loaded bytes are what the instance is
    attested for, so re-reading per turn would let the answer drift away from the
    digest the target attested. The answer is a function of the attested
    artifact set, never of excluded bytes: the file is read only when the scan
    that produced either `candidate_digest` or `dependency_digest` already
    recorded an `Entry` at exactly `skills/SKILL.md` with `kind == "file"` —
    the file is attested by either digest, since a policy may route it into
    either scanned set. A policy that *excludes* `skills/**` never puts that
    entry in either set, so this never reads the path at all — reading it
    directly, as a policy-blind lookup would, could answer from bytes no
    digest covers, including a symlink `scan_tree` would have refused had the
    path been scanned. When the entry is absent from both sets — excluded by
    policy, missing from the tree, or refused by the scan — the answer is the
    fixed `NO_ATTESTED_SKILL` constant. Returns `(line, used)`, where `used` is
    `("skills/SKILL.md",)` exactly when the attested file was read successfully.
    """
    attested = any(
        entry.path == "skills/SKILL.md" and entry.kind == "file"
        for entry in (*artifact, *dependency)
    )
    if not attested:
        return NO_ATTESTED_SKILL, ()
    try:
        text = (root / "skills" / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return NO_ATTESTED_SKILL, ()
    lines = text.splitlines()
    return (lines[0] if lines else ""), ("skills/SKILL.md",)


class _ReferenceSession:
    def __init__(
        self, skill_line: str, used: tuple[str, ...], seed: int | None, max_answer_chars: int
    ) -> None:
        self._skill_line = skill_line
        self._used = used
        self._max_answer_chars = max_answer_chars
        self.effective_seed = seed

    def turn(self, message: Message) -> BackendReply:
        full = f"{self._skill_line} | {message.content}"
        truncated = len(full) > self._max_answer_chars
        answer = full[: self._max_answer_chars] if truncated else full
        usage = Usage(
            prompt=len(message.content),
            completion=len(answer),
            cached=0,
            total=len(message.content) + len(answer),
        )
        result = TurnResult(
            events=({"type": "assistant_message", "content": answer},),
            usage=usage,
            latency_ms=0,
            outcome="ok",
            provenance={"used": list(self._used)},
        )
        return BackendReply(result, truncated=truncated)

    def close(self) -> None:
        return None


class _ReferenceInstance:
    def __init__(self, backend: ReferenceBackend, tree: LoadedTree) -> None:
        self._backend = backend
        self._seed_mode = tree.seed_mode
        self._skill_line, self._used = _skill_line(tree.root, tree.artifact, tree.dependency)

    def open(self, public: Mapping[str, Any], seed: int | None) -> _ReferenceSession:
        effective = self._backend.next_seed() if self._seed_mode == "unpaired" else seed
        return _ReferenceSession(
            self._skill_line, self._used, effective, self._backend.max_answer_chars
        )

    def close(self) -> None:
        return None


class ReferenceBackend:
    """The demonstration backend: answers `<first line of skills/SKILL.md> | <message>`."""

    def __init__(self, max_answer_chars: int = 4000) -> None:
        if type(max_answer_chars) is not int or max_answer_chars < 1:
            raise ValueError("max_answer_chars must be an integer >= 1")
        self.max_answer_chars = max_answer_chars
        self._seed_lock = threading.Lock()
        self._seed_counter = 0

    def next_seed(self) -> int:
        with self._seed_lock:
            self._seed_counter += 1
            return self._seed_counter

    def load(self, tree: LoadedTree) -> _ReferenceInstance:
        return _ReferenceInstance(self, tree)


class ReferenceTarget(TargetServer):
    """A complete `/evolution/v2` target: the library bound to `ReferenceBackend`."""

    def __init__(
        self,
        *,
        allowed_artifact_root: Path,
        policy: ArtifactPolicyV1,
        seed_mode: SeedMode = "exact",
        max_instances: int = 4,
        max_concurrent_sessions: int = 8,
        max_input_bytes: int = 1 << 20,
        max_output_bytes: int = 1 << 20,
        max_answer_chars: int = 4000,
        tombstones: int = 1024,
        target_id: str = "reference",
        contract_digest: str = "reference-contract-1",
        settings_hash: str = "reference-settings-1",
        runtime_ref_digest: str = "reference-runtime-1",
    ) -> None:
        super().__init__(
            ReferenceBackend(max_answer_chars),
            allowed_artifact_root=allowed_artifact_root,
            policy=policy,
            seed_mode=seed_mode,
            max_instances=max_instances,
            max_concurrent_sessions=max_concurrent_sessions,
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
            tombstones=tombstones,
            target_id=target_id,
            contract_digest=contract_digest,
            settings_hash=settings_hash,
            runtime_ref_digest=runtime_ref_digest,
        )
        self.max_answer_chars = max_answer_chars


def serve(target: TargetServer, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Kept for the suites and `evo-target`; `target_server.serve_target` is the library name."""

    return serve_target(target, host, port)
