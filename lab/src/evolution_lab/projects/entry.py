"""The target entry file ``lab-target-entry/1``: how a live target without a package starts (spec 2026-10-02 §2.1)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evolution_lab.preparation.storage import canonical_json_bytes

from .errors import ProjectError

ENTRY_FILE = "lab-target-entry.json"
ENTRY_SCHEMA = "lab-target-entry/1"
DEFAULT_LIMITS = {"max_instances": 2, "max_concurrent_sessions": 1, "max_input_bytes": 1 << 20, "max_output_bytes": 64 << 10}
_REQUIRED = frozenset({"schema", "agent", "program_paths", "environment", "env", "secret_names"})
_OPTIONAL = frozenset({"limits", "startup_timeout_seconds", "stop_grace_seconds"})
_AGENT_KEYS = frozenset({"module", "attribute", "import_root"})
_ENV_KEYS = frozenset({"executable", "wheel_paths", "runtime_files"})


@dataclass(frozen=True)
class TargetEntry:
    path: Path
    agent: dict[str, str]
    program_paths: tuple[str, ...]
    environment: dict | None  # None means {"kind": "service"}
    env: dict[str, str]
    secret_names: tuple[str, ...]
    limits: dict[str, int]
    startup_timeout_seconds: float
    stop_grace_seconds: float
    digest: str  # sha256 of the canonical JSON of the file


def _bad(key: str) -> ProjectError:
    return ProjectError("target_entry_invalid", key)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _relative(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _outside(value: Any, root: Path) -> bool:
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        return False
    return not Path(value).resolve().is_relative_to(root)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def read_target_entry(place: Path) -> TargetEntry | None:
    path = Path(place) / ENTRY_FILE
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _bad("file") from None
    if not isinstance(document, dict) or document.get("schema") != ENTRY_SCHEMA:
        raise ProjectError("unsupported_schema")
    keys = set(document)
    for key in sorted(_REQUIRED - keys):
        raise _bad(key)
    if keys - _REQUIRED - _OPTIONAL:
        raise _bad("unknown key")
    root = Path(place).resolve()

    agent = document["agent"]
    if (not isinstance(agent, dict) or set(agent) != _AGENT_KEYS
            or not all(isinstance(v, str) and v for v in agent.values())):
        raise _bad("agent")
    paths = document["program_paths"]
    if not _is_str_list(paths) or not paths or not all(_relative(p) for p in paths):
        raise _bad("program_paths")
    if not _relative(agent["import_root"]):
        raise _bad("agent")

    environment = document["environment"]
    if environment == {"kind": "service"}:
        environment = None
    elif (isinstance(environment, dict) and set(environment) == _ENV_KEYS
          and isinstance(environment["executable"], str)
          and _is_str_list(environment["wheel_paths"]) and _is_str_list(environment["runtime_files"])
          and all(_outside(p, root) for p in [environment["executable"], *environment["wheel_paths"],
                                              *environment["runtime_files"]])):
        environment = {"executable": environment["executable"], "wheel_paths": list(environment["wheel_paths"]),
                       "runtime_files": list(environment["runtime_files"])}
    else:
        raise _bad("environment")

    env = document["env"]
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise _bad("env")
    names = document["secret_names"]
    if (not _is_str_list(names) or len(set(names)) != len(names) or not all(names)
            or set(names) & set(env)):
        raise _bad("secret_names")

    limits = dict(DEFAULT_LIMITS)
    if "limits" in document:
        given = document["limits"]
        if (not isinstance(given, dict) or not set(given) <= set(DEFAULT_LIMITS)
                or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in given.values())):
            raise _bad("limits")
        limits.update(given)
    timeouts = {}
    for key, default in (("startup_timeout_seconds", 30.0), ("stop_grace_seconds", 5.0)):
        value = document.get(key, default)
        if not _number(value):
            raise _bad(key)
        timeouts[key] = float(value)

    return TargetEntry(
        path=path, agent=dict(agent), program_paths=tuple(paths), environment=environment, env=dict(env),
        secret_names=tuple(names), limits=limits, startup_timeout_seconds=timeouts["startup_timeout_seconds"],
        stop_grace_seconds=timeouts["stop_grace_seconds"],
        digest=hashlib.sha256(canonical_json_bytes(document)).hexdigest())


def probe_launch(entry: TargetEntry, *, target_id: str, base_url: str) -> dict:
    """A ``lab-target-launch/2`` document without ``environment_ref``; preparation fills it and the runtime digest."""
    settings = {"env": dict(sorted(entry.env.items())), "secret_names": sorted(entry.secret_names)}
    return {
        "schema": "lab-target-launch/2",
        "agent": dict(entry.agent),
        "program_paths": list(entry.program_paths),
        "env": dict(entry.env), "secret_names": list(entry.secret_names),
        "endpoint": {"base_url": base_url},
        "identity": {"target_id": target_id, "contract_digest": entry.digest,
                     "settings_hash": hashlib.sha256(canonical_json_bytes(settings)).hexdigest(),
                     "runtime_ref_digest": None, "seed_mode": "none"},
        "policy": ENTRY_FILE,          # replaced by the synthesized policy entry in preparation (relocation.launch)
        "limits": dict(entry.limits),
        "startup_timeout_seconds": entry.startup_timeout_seconds,
        "stop_grace_seconds": entry.stop_grace_seconds,
        "conformance": None,
    }
