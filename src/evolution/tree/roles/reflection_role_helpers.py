"""Sealed thin launchers for a campaign-owned reflection read session."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

from evolution.tree.roles.reflection_reader import ReaderIntegrityError, ReflectionReadSession
from evolution.tree.roles.reflector_contracts import GroupError, ProbeAccess

PROBE_CALL_LOG_NAME = "calls.jsonl"
MAX_PROBE_CALL_LOG_BYTES = 1024 * 1024
MAX_PROBE_CALL_LOG_ENTRIES = 256


def read_probe_call_log(workspace: Path, control_prefix: str) -> tuple[dict[str, Any], ...]:
    """Read bounded role-written claims without following role-controlled links."""
    try:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(workspace, flags)
            stack.callback(os.close, directory)
            for part in (control_prefix, "probe"):
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(
                PROBE_CALL_LOG_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            stack.callback(os.close, descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROBE_CALL_LOG_BYTES:
                return ()
            with os.fdopen(os.dup(descriptor), "rb") as source:
                content = source.read(MAX_PROBE_CALL_LOG_BYTES + 1)
            if len(content) > MAX_PROBE_CALL_LOG_BYTES:
                return ()
    except OSError:
        return ()
    requests = []
    for line in content.splitlines()[:MAX_PROBE_CALL_LOG_ENTRIES]:
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(value, dict) and set(value) == {"method", "path", "body"}:
            requests.append(value)
    return tuple(requests)


_METHODS = frozenset(
    {
        "list_history",
        "read_history",
        "list_evidence",
        "read_evidence",
        "read_trace",
        "list_documents",
        "read_document",
        "list_grouping_inputs",
        "read_grouping_input",
        "stream_grouping_inputs",
    }
)
# This code is independent of package discovery in the declared child's environment.
_CLIENT = r"""import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from evolution.tree.roles.reflection_read_client import call, stream
from evolution.tree.roles.reflection_reader import ReaderArgumentError, ReaderIntegrityError
try:
    config = json.loads(pathlib.Path(__file__).with_name("session.json").read_text())
    if len(sys.argv) > 2:
        raise ReaderArgumentError("invalid_arguments")
    arguments = json.loads(sys.argv[1]) if len(sys.argv) == 2 else {}
    if not isinstance(arguments, dict):
        raise ReaderArgumentError("invalid_arguments")
    if METHOD == "stream_grouping_inputs":
        for frame in stream(config["endpoint"], config["capability"], METHOD, arguments):
            print(json.dumps(frame, ensure_ascii=False, separators=(",", ":")), flush=True)
    else:
        print(json.dumps(call(config["endpoint"], config["capability"], METHOD, arguments), ensure_ascii=False))
except (ReaderArgumentError, ReaderIntegrityError) as error:
    print(json.dumps({"error": {"code": error.code}}))
    sys.exit(1)
except Exception:
    print('{"error":{"code":"reader_unavailable"}}')
    sys.exit(1)
"""


@dataclass(frozen=True)
class RoleHelpers:
    descriptors: Mapping[str, tuple[str, ...]]
    seals: tuple[tuple[Path, str], ...]

    def verify(self) -> None:
        for path, digest in self.seals:
            try:
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ReaderIntegrityError("helper_changed")
            except OSError as exc:
                raise ReaderIntegrityError("helper_changed") from exc


def install_helpers(control: Path, reader: ReflectionReadSession) -> RoleHelpers:
    """Write only launchers and ephemeral capability data, never source bodies."""
    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = reader.client_descriptor()
    files = {
        "session.json": json.dumps(
            {"endpoint": descriptor.url, "capability": descriptor.capability}
        ).encode()
    }
    files.update(
        {
            "evolution/__init__.py": b"",
            "evolution/tree/__init__.py": b"",
            "evolution/tree/roles/__init__.py": b"",
        }
    )
    for name in ("reflection_read_client.py", "reflection_reader.py"):
        files["evolution/tree/roles/" + name] = (
            resources.files("evolution.tree.roles").joinpath(name).read_bytes()
        )
    commands: dict[str, tuple[str, ...]] = {}
    for method in sorted(_METHODS & reader.methods):
        name = method + ".py"
        files[name] = ("METHOD = " + repr(method) + "\n" + _CLIENT).encode()
        commands[method] = (sys.executable, "-B", "-I", str((control / name).resolve()))
    seals = []
    for name, content in files.items():
        path = control / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive creation rejects preexisting files and symlinks in the role plane.
        with path.open("xb") as output:
            output.write(content)
        path.chmod(0o400)
        seals.append((path, hashlib.sha256(content).hexdigest()))
    return RoleHelpers(MappingProxyType(commands), tuple(seals))


def probe_log_secrets(access: ProbeAccess | None) -> tuple[str, ...]:
    """Operation-local redactions; neither value participates in role identity."""
    if access is None or access.url is None:
        return ()
    from urllib.parse import urlsplit

    parts = urlsplit(access.url).path.split("/")
    token = parts[-2] if len(parts) >= 3 and parts[-1] == "probe" else ""
    return (access.url, token) if token else (access.url,)


def probe_opening(
    access: ProbeAccess | None,
    command: Sequence[str] | None = None,
    *,
    typed_body: bool = False,
    health_unavailable: bool = False,
) -> str:
    if access is None:
        return ""
    invocation = (
        "Invoke probe_request with "
        if command is None
        else "Invoke the separate sealed probe helper argv " + json.dumps(list(command)) + " with "
    )
    return (
        "\n\n" + invocation + 'one JSON object {"method":...,"path":...,"body":...}. '
        "External HTTP callers POST that object to probe.url. Allowed methods: "
        + ", ".join(access.methods)
        + f". Initial remaining budget: {access.budget}; dispatched calls consume budget. "
        'Results are {"seq","status","body","truncated"} or {"error","detail"}. '
        "Bodies are bounded and may be truncated. Non-2xx HTTP statuses are responses; "
        "HTTP success proves only an HTTP response, not semantic support. Do not retry "
        "probe errors automatically. Optional probe_refs must cite returned positive seq values. "
        "Every affected_cases entry still requires a cited baseline observation. "
        "On context_exhausted finish_reflection using the evidence already available."
        + (
            " body is the JSON value to send (an object, a list, a string, a number, true, false or null), not text that contains JSON."
            if typed_body
            else ""
        )
        + (" Environment description: " + access.description if access.description else "")
        + (
            " The probe did not answer its health check. Probe calls stay available and each one tries the probe again; a call that fails answers unavailable, so retry later or continue without the probe."
            if health_unavailable or access.health_unavailable
            else ""
        )
        + (
            " The probe mediator could not start; probe calls answer unavailable. Continue without probe evidence."
            if access.url is None or access.mediator_failed
            else ""
        )
    )


_PROBE_LAUNCHER = """import json, os, pathlib, stat, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from probe_client import call, ProbeClientError
try:
    if len(sys.argv) != 2:
        raise ProbeClientError("invalid_request")
    value = json.loads(sys.argv[1])
    if not isinstance(value, dict) or set(value) != {"method", "path", "body"}:
        raise ProbeClientError("invalid_request")
    config = json.loads(pathlib.Path(__file__).with_name("session.json").read_text())
    if config.get("on_unavailable") == "continue" and config["url"] is None:
        result = {"error": "unavailable", "detail": "probe unavailable: continue without it or retry later"}
        try:
            line = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\\n").encode("utf-8")
            descriptor = os.open(pathlib.Path(__file__).with_name(config["call_log"]), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if stat.S_ISREG(metadata.st_mode) and metadata.st_size + len(line) <= config["call_log_max_bytes"]:
                    os.write(descriptor, line)
            finally:
                os.close(descriptor)
        except (OSError, ValueError, UnicodeError, RecursionError):
            pass
    else:
        result = call(config["url"], value, deadline_at=config["deadline_at"])
except ProbeClientError as error:
    if error.code == "unavailable" and config.get("on_unavailable") == "continue":
        result = {"error": "unavailable", "detail": "probe unavailable: continue without it or retry later"}
    else:
        result = {"error": error.code, "detail": "probe helper request failed"}
except Exception:
    result = {"error": "invalid_request", "detail": "probe helper request failed"}
print(json.dumps(result, ensure_ascii=False), flush=True)
"""


def install_probe_helper(control: Path, access: ProbeAccess, *, deadline_at: float) -> RoleHelpers:
    """Install a standalone stdlib client outside the evidence reader registry."""
    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = {
        "session.json": json.dumps(
            {
                "url": access.url,
                "deadline_at": deadline_at,
                **(
                    {"on_unavailable": access.on_unavailable}
                    if access.on_unavailable is not None
                    else {}
                ),
                **(
                    {
                        "call_log": PROBE_CALL_LOG_NAME,
                        "call_log_max_bytes": MAX_PROBE_CALL_LOG_BYTES,
                    }
                    if access.on_unavailable == "continue" and access.url is None
                    else {}
                ),
            }
        ).encode(),
        "probe_client.py": resources.files("evolution.tree.target")
        .joinpath("probe_client.py")
        .read_bytes(),
        "probe_wire.py": resources.files("evolution.tree.target")
        .joinpath("probe_wire.py")
        .read_bytes(),
        "probe_request.py": _PROBE_LAUNCHER.encode(),
    }
    seals = []
    for name, content in files.items():
        path = control / name
        with path.open("xb") as output:
            output.write(content)
        path.chmod(0o400)
        seals.append((path, hashlib.sha256(content).hexdigest()))
    command = (sys.executable, "-B", "-I", str((control / "probe_request.py").resolve()))
    return RoleHelpers(MappingProxyType({"probe_request": command}), tuple(seals))


def groups_without_probe_refs(groups: Any, *, probe: bool) -> Any:
    """Shape-only precheck. Durable sequence membership belongs to the campaign."""
    if not probe or not isinstance(groups, (list, tuple)):
        return groups
    copies = []
    for group in groups:
        if not isinstance(group, dict):
            copies.append(group)
            continue
        if "probe_refs" in group:
            refs = group["probe_refs"]
            if not isinstance(refs, list) or any(type(seq) is not int or seq <= 0 for seq in refs):
                raise GroupError("invalid_result", "probe_refs must be positive exact integers")
        copies.append({key: value for key, value in group.items() if key != "probe_refs"})
    return copies
