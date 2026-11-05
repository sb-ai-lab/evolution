"""Lazy control plane and bounded result read for a family-14 agent Grouper.

The campaign owns the reader and a fresh projection. This module writes only
stable control data and thin launchers; the agent's GROUPS and the controller's
VALIDATION remain mutable for the runtime's one repair call.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from evolution.llm.agent_backend import AgentBackendError, reports_tool_turns
from evolution.tree.execution.check_modes import scan_grouping_wire
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.agent_role import agent_log_secrets, call_agent, verify_agent_seals
from evolution.tree.roles.config import AgentCliGrouperSection
from evolution.tree.roles.grouping.model_grouping import (
    ModelGroup,
    parse_map_response,
    resolve_overlaps,
)
from evolution.tree.roles.grouping.model_grouping_runtime import (
    Fallback,
    ModelGroupingOutcome,
    admit_model_groups,
)
from evolution.tree.roles.grouping.reflect_grouping import GroupingRegistry
from evolution.tree.roles.reflection_reader import (
    CONTENT_CHARS,
    GROUPING_PAGE_SIZE,
    MAX_STREAM_FRAME_BYTES,
    ReaderIntegrityError,
    ReflectionReadSession,
)
from evolution.tree.roles.reflection_role_helpers import install_helpers
from evolution.tree.roles.role_failures import GrouperRoleFailure, GroupingExecutionError
from evolution.tree.roles.seals import FileSeal, SealChanged
from evolution.tree.roles.workspace import AttemptLog, open_attempt_log

CONTROL_PREFIX: Final = ".evolution"
REQUEST_FILE: Final = "request.json"
OPENING_FILE: Final = "OPENING.md"
GROUPS_FILE: Final = "GROUPS.json"
VALIDATION_FILE: Final = "VALIDATION.json"
GROUPING_METHODS: Final = frozenset(
    {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"}
)
MAX_REQUEST_BYTES: Final = 64 * 1024  # The existing grouping descriptor bound.


class GroupingControlError(ValueError):
    """A fixed, value-free grouping fault with an existing journal code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Seal:
    path: Path
    size: int
    digest: str


@dataclass(frozen=True)
class GroupingControlPlane:
    """Stable files verified before/after a call; output files are excluded."""

    root: Path
    request_path: Path
    opening_path: Path
    sealed_paths: tuple[Path, ...]
    _seals: tuple[_Seal, ...]
    _control_identity: tuple[int, int]
    _readers_identity: tuple[int, int]

    def verify(self) -> None:
        control = self.root / CONTROL_PREFIX
        readers = control / "readers"
        for directory, identity in (
            (control, self._control_identity),
            (readers, self._readers_identity),
        ):
            try:
                current = directory.lstat()
            except OSError as exc:
                if directory == readers:
                    raise ReaderIntegrityError("helper_changed") from exc
                raise GroupingControlError("changed_seal") from exc
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                if directory == readers:
                    raise ReaderIntegrityError("helper_changed")
                raise GroupingControlError("changed_seal")
        for seal in self._seals:
            try:
                raw, mode = _read_regular(self.root, seal.path, seal.size)
            except (OSError, GroupingControlError) as exc:
                if seal.path.is_relative_to(readers):
                    raise ReaderIntegrityError("helper_changed") from exc
                raise GroupingControlError("changed_seal") from exc
            if (
                len(raw) != seal.size
                or mode & 0o222
                or hashlib.sha256(raw).hexdigest() != seal.digest
            ):
                if seal.path.is_relative_to(readers):
                    raise ReaderIntegrityError("helper_changed")
                raise GroupingControlError("changed_seal")


def _directory_identity(path: Path) -> tuple[int, int]:
    current = path.lstat()
    if not stat.S_ISDIR(current.st_mode):
        raise GroupingControlError("control_collision")
    return current.st_dev, current.st_ino


def _write_stable(path: Path, content: bytes) -> None:
    # The projection is private; exclusive creation still refuses a raced
    # pre-existing entry, including a symlink, without touching its target.
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400), "wb"
    ) as output:
        output.write(content)


def _read_regular(root: Path, path: Path, bound: int) -> tuple[bytes, int]:
    """Open every component without following links; bound reads and avoid FIFOs."""

    parts = path.relative_to(root).parts
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        for part in parts[:-1]:
            directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            stack.callback(os.close, directory)
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
        )
        stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise GroupingControlError("control_collision")
        if info.st_size > bound:
            raise GroupingControlError("output_too_large")
        content = bytearray()
        while len(content) <= bound:
            chunk = os.read(descriptor, min(65536, bound + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        return bytes(content), info.st_mode


def write_grouping_control(
    root: Path,
    *,
    registry: GroupingRegistry,
    reader: ReflectionReadSession,
    section: AgentCliGrouperSection,
    policy_digest: str,
    opening: str,
    parent_digest: str | None = None,
) -> GroupingControlPlane:
    """Install only the grouping request, opening, and three sealed launchers.

    `root` is the campaign's fresh projection. Existing entries are refused
    before any source material is copied; no source bodies are ever copied.
    The caller closes `reader` and discards `root` in its own `finally` block.
    """

    root = Path(root)
    if reader.methods != GROUPING_METHODS:
        raise ValueError("grouping_reader_methods")
    _directory_identity(root)
    entries = tuple(root.iterdir())
    if any(entry.name != CONTROL_PREFIX for entry in entries):
        raise GroupingControlError("control_collision")
    control = root / CONTROL_PREFIX
    if entries:
        _directory_identity(control)
        if any(control.iterdir()):
            raise GroupingControlError("control_collision")
    else:
        try:
            control.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise GroupingControlError("control_collision") from exc
    readers = control / "readers"
    try:
        readers.mkdir(mode=0o700)
        helpers = install_helpers(readers, reader)
    except FileExistsError as exc:
        raise GroupingControlError("control_collision") from exc
    if set(helpers.descriptors) != GROUPING_METHODS:
        raise GroupingControlError("control_collision")
    request = {
        "version": "agent_grouping_request/1",
        "baseline_binding": registry.baseline_binding,
        "parent_digest": parent_digest,
        "policy_digest": policy_digest,
        "registry_digest": registry.digest,
        "record_count": len(registry.records),
        "limits": {
            "max_turns": section.role.max_turns,
            "timeout_seconds": section.timeout_seconds,
            "max_payload_bytes": section.role.budgets.max_payload_bytes,
            "page_size": GROUPING_PAGE_SIZE,
            "chunk_characters": CONTENT_CHARS,
            "stream_frame_bytes": MAX_STREAM_FRAME_BYTES,
        },
        "output": {
            "path": f"{CONTROL_PREFIX}/{GROUPS_FILE}",
            "top_level_keys": ["groups"],
            "required_group_keys": [
                "id",
                "title",
                "failure_mode",
                "expected_behaviour",
                "question",
                "members",
                "affected_cases",
                "citations",
                "priority",
            ],
            "optional_group_keys": ["metrics", "provenance"],
            "members": "evidence id and variant label pairs",
        },
        "helpers": {name: list(argv) for name, argv in helpers.descriptors.items()},
        "opening": opening,
    }
    encoded = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise GroupingControlError("descriptor_too_large")
    request_path = control / REQUEST_FILE
    opening_path = control / OPENING_FILE
    try:
        _write_stable(request_path, encoded)
        _write_stable(opening_path, opening.encode("utf-8"))
    except FileExistsError as exc:
        raise GroupingControlError("control_collision") from exc
    paths = (*[path for path, _digest in helpers.seals], request_path, opening_path)
    seals = tuple(
        _Seal(path, path.stat().st_size, digest)
        for path, digest in (
            *helpers.seals,
            (request_path, hashlib.sha256(encoded).hexdigest()),
            (opening_path, hashlib.sha256(opening.encode()).hexdigest()),
        )
    )
    return GroupingControlPlane(
        root,
        request_path,
        opening_path,
        paths,
        seals,
        _directory_identity(control),
        _directory_identity(readers),
    )


def read_grouping_output(
    root: Path, *, max_payload_bytes: int
) -> tuple[str | None, tuple[str, ...]]:
    """Read raw GROUPS text for the runtime's structural pre-check.

    Missing and invalid UTF-8 are safe content defects for one repair.
    Links, nonregular entries and oversize output retain existing grouping
    fault routes. Permission denials on the agent-owned output are control
    collisions; other I/O failures propagate as storage faults.
    """

    if type(max_payload_bytes) is not int or max_payload_bytes <= 0:
        raise ValueError("max_payload_bytes")
    root = Path(root)
    control = root / CONTROL_PREFIX
    _directory_identity(control)
    output = control / GROUPS_FILE
    try:
        data, _mode = _read_regular(root, output, max_payload_bytes)
    except FileNotFoundError:
        return None, (f"{CONTROL_PREFIX}/{GROUPS_FILE} is missing",)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM):
            raise GroupingControlError("control_collision") from exc
        raise
    if len(data) > max_payload_bytes:
        raise GroupingControlError("output_too_large")
    try:
        return data.decode("utf-8"), ()
    except UnicodeDecodeError:
        return None, (f"{CONTROL_PREFIX}/{GROUPS_FILE} is not UTF-8 text",)


# Runtime policy is deliberately separate from the shared CLI call: this role
# owns its control plane, one repair, and family-14 model admission.
@dataclass
class _CallLog(AttemptLog):
    """Mark log failures so the call boundary cannot label storage as a role fault."""

    forbidden_secret: Callable[[str], bool] = field(kw_only=True)
    failure: Exception | None = field(default=None, init=False)

    def record(self, value: Mapping[str, Any]) -> None:
        if value.get("phase") == "response":
            _refuse_secret(str(value.get("content", "")), self.forbidden_secret)
        try:
            super().record(value)
        except Exception as exc:
            self.failure = exc
            raise


_OPENING: Final = (
    "Read .evolution/request.json and use its grouping readers to inspect the train "
    "observations. Group every failing available observation exactly once in "
    ".evolution/GROUPS.json, using the declared output contract. Passing and "
    "unavailable observations belong to no group. Describe observed failures only; "
    "do not diagnose causes or propose repairs."
)
_REPAIR_OPENING: Final = (
    _OPENING + " Correct the defects in .evolution/VALIDATION.json and replace "
    ".evolution/GROUPS.json with the complete corrected answer."
)
_CONTROL_CODES: Final = {
    "changed_seal": "command_changed",
    "control_collision": "helper_unavailable",
    "output_too_large": "result_too_large",
}


def _refuse_secret(text: str, forbidden: Callable[[str], bool]) -> None:
    """Inspect raw text and decoded strings before any group can be discarded."""

    if scan_grouping_wire(forbidden, text):
        raise GroupingExecutionError("secret_detected")


def _write_validation(root: Path, issues: tuple[str, ...]) -> None:
    """Replace any previous mutable entry without following an agent's link."""

    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            control = os.open(
                CONTROL_PREFIX, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            stack.callback(os.close, control)
            try:
                os.unlink(VALIDATION_FILE, dir_fd=control)
            except FileNotFoundError:
                pass
            with os.fdopen(
                os.open(
                    VALIDATION_FILE,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=control,
                ),
                "w",
                encoding="utf-8",
            ) as output:
                json.dump({"issues": issues}, output, ensure_ascii=False)
    except OSError as exc:
        if exc.errno in (
            errno.EISDIR,
            errno.ELOOP,
            errno.ENOTDIR,
            errno.EEXIST,
            errno.EACCES,
            errno.EPERM,
        ):
            raise GroupingControlError("control_collision") from exc
        raise


@dataclass(frozen=True)
class AgentCliGrouper:
    """One lazy CLI operation and at most one repair, with shared admission.

    The campaign owns the reader and projection lifetime. ``verify_admission``
    must scan the campaign's bound frozen sources; it runs even when the CLI
    fails, before translating that failure into a Grouper role failure.
    """

    section: AgentCliGrouperSection
    program: FileSeal
    prompt: FileSeal
    child_env: Mapping[str, str]
    log_root: Path
    secrets: tuple[str, ...]
    backend_factory: Callable[..., Any] | None = None
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    family15: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.program.path is None:
            raise ValueError("the agent CLI executable must be sealed as a file")
        if self.section.role.max_turns is not None and not reports_tool_turns(
            self.section.role.model
        ):
            raise ValueError("max_turns needs a CLI that reports turn telemetry")

    async def run(
        self,
        registry: GroupingRegistry,
        *,
        reader: ReflectionReadSession,
        control_root: Path,
        operation: str,
        policy_identity: str,
        parent_digest: str | None,
        verify_admission: Callable[[], None],
        cancelled: Callable[[], bool],
        forbidden_secret: Callable[[str], bool],
        oracle_needle: Callable[[str], bool],
    ) -> ModelGroupingOutcome:
        started = time.monotonic()
        calls = {"agent_calls": 0, "repairs": 0, "turns": 0}
        failing = tuple(r.evidence_id for r in registry.records if r.failing and r.available)
        case_of = {r.evidence_id: r.case_id for r in registry.records}

        def check_cancelled() -> None:
            if cancelled():
                raise GroupingExecutionError("cancelled")

        def verify(control: GroupingControlPlane | None = None) -> None:
            if verify_agent_seals(self.prompt, self.program) is not None:
                raise GroupingExecutionError("command_changed")
            if control is not None:
                control.verify()
            reader.verify_admission(verify_admission)

        def read_output() -> tuple[str | None, tuple[str, ...]]:
            text, issues = read_grouping_output(
                control_root, max_payload_bytes=self.section.role.budgets.max_payload_bytes
            )
            # Scan before parsing can discard malformed or ineligible groups.
            if text is not None:
                _refuse_secret(text, forbidden_secret)
            return text, issues

        check_cancelled()
        verify()
        try:
            control = write_grouping_control(
                control_root,
                registry=registry,
                reader=reader,
                section=self.section,
                policy_digest=policy_identity,
                parent_digest=parent_digest,
                opening=_OPENING,
            )
            descriptor = reader.client_descriptor()
            with open_attempt_log(
                self.log_root,
                f"grouping-{operation}",
                secrets=(
                    *agent_log_secrets(self.secrets, self.program),
                    descriptor.capability,
                    descriptor.url,
                ),
                admitted=self.admitted_hygiene,
            ) as sink:
                log = _CallLog(
                    sink.path, sink._handle, sink._secrets, forbidden_secret=forbidden_secret
                )
                groups: tuple[ModelGroup, ...] = ()
                fallback: Fallback | None = None
                if failing:
                    for call in (1, 2):
                        check_cancelled()
                        verify(control)
                        if time.monotonic() - started >= self.section.timeout_seconds:
                            raise GrouperRoleFailure("timeout", "unknown")
                        try:
                            turns = await call_agent(
                                section=self.section.role,
                                program=self.program,
                                prompt=self.prompt,
                                child_env=self.child_env,
                                root=control_root,
                                opening=_OPENING if call == 1 else _REPAIR_OPENING,
                                deadline_seconds=self.section.timeout_seconds,
                                started=started,
                                log=log,
                                call=call,
                                backend_factory=self.backend_factory,
                            )
                        except Exception as exc:
                            # Writes happen inside call_agent. Keep their exact
                            # storage route and do not attempt another log write.
                            if log.failure is exc:
                                raise
                            verify(control)
                            read_output()
                            check_cancelled()
                            if isinstance(exc, SealChanged):
                                raise GroupingExecutionError("command_changed") from exc
                            if isinstance(
                                exc,
                                (
                                    GroupingExecutionError,
                                    ReaderIntegrityError,
                                    ValueError,
                                    TypeError,
                                    KeyError,
                                    WorkspaceError,
                                ),
                            ):
                                raise
                            reason = "exception"
                            if isinstance(exc, TimeoutError) or (
                                isinstance(exc, AgentBackendError) and exc.timed_out
                            ):
                                reason = "timeout"
                            elif isinstance(exc, AgentBackendError):
                                reason = "agent_error"
                            log.record(
                                {
                                    "phase": "failure",
                                    "reason": reason,
                                    "exception_class": type(exc).__name__,
                                }
                            )
                            raise GrouperRoleFailure(reason, "unknown") from exc
                        calls["agent_calls"] += 1
                        calls["repairs"] += call - 1
                        calls["turns"] += int(turns)
                        verify(control)
                        text, issues = read_output()
                        check_cancelled()
                        if time.monotonic() - started >= self.section.timeout_seconds:
                            raise GrouperRoleFailure("timeout", "unknown")
                        if (
                            self.section.role.max_turns is not None
                            and calls["turns"] > self.section.role.max_turns
                        ):
                            fallback = ("all", "turns_exhausted")
                            break
                        parsed = parse_map_response(
                            text or "",
                            chunk_failing=failing,
                            case_of=case_of,
                            provenance_available=False,
                        )
                        groups = resolve_overlaps(parsed.groups)
                        issues = issues or parsed.defects
                        if not issues or call == 2:
                            break
                        if (
                            self.section.role.max_turns is not None
                            and calls["turns"] >= self.section.role.max_turns
                        ):
                            groups = ()
                            fallback = ("all", "turns_exhausted")
                            break
                        _write_validation(control_root, issues)
                verify(control)
                check_cancelled()
                try:
                    return admit_model_groups(
                        registry,
                        groups,
                        fallback,
                        describe_fields=self.family15,
                        family14=True,
                        failing=frozenset(failing),
                        policy_identity=policy_identity,
                        fallback_features=None
                        if self.section.fallback is None
                        else self.section.fallback.features,
                        forbidden_secret=forbidden_secret,
                        oracle_needle=oracle_needle,
                        log=log,
                        calls=calls,
                    )
                finally:
                    # Shared admission catches ValueError for malformed groups;
                    # a closed-log write is not an answer defect or fallback.
                    if log.failure is not None:
                        raise log.failure
        except GroupingControlError as exc:
            raise GroupingExecutionError(_CONTROL_CODES.get(exc.code, exc.code)) from exc
