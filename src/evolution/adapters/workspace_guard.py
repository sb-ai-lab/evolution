"""Workspace-scoped guardrails for framework experiment runs.

The Table 3 adapters execute several third-party agent frameworks on the same
host. Setting a process cwd is not a sandbox: an agent tool can still reference
absolute host paths such as ``$HOME``. This module provides a small,
framework-neutral guard layer:

* consistent workspace/run-home environment variables;
* JSONL audit logs for setup, allowed opt-in events, and blocks;
* path/command checks that allow normal relative workspace operations while
  rejecting host-root traversal;
* optional ``sitecustomize`` injection for generated Python helper scripts
  that would otherwise bypass shell-command wrappers with ``os.walk`` or
  ``glob`` over host roots;
* optional tool adapters for frameworks whose file/terminal tools run in the
  current Python process.

It is not an OS sandbox. It is a deterministic guard for the framework tool
surfaces we control in this repository.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_WRAPPED_COMMANDS = ("find", "fd", "rg", "grep", "ls", "du", "tree")
# Destructive shell commands whose path arguments must stay inside the task
# workspace. Wrapped on PATH (like _WRAPPED_COMMANDS) but with a path-resolving
# policy instead of the read-tool regex, so relative traversal (rm -rf ../../x)
# is caught as well as absolute host paths.
_DESTRUCTIVE_COMMANDS = ("rm", "rmdir", "mv", "shred", "truncate", "dd", "chmod", "chown")
# Host base-env bin dirs stripped from the agent subprocess PATH so that bare
# ``python``/``pip`` can never resolve to the host base conda env (the env wiped
# in the incident). Override with EVOLUTION_GUARD_STRIP_PATH_DIRS (os.pathsep).
_HOST_BASE_ENV_DIRS = tuple(
    d
    for d in os.environ.get("EVOLUTION_GUARD_STRIP_PATH_DIRS", "/home/user/conda").split(os.pathsep)
    if d
)
_BROAD_ROOT_COMMAND_RE = re.compile(
    r"(?is)(?:^|[;&|]\s*)"
    r"(?:find|fd|rg|grep|ls|du|tree)\b"
    r"[^\n;|&]*"
    r"(?:^|\s)(?:/|/home|/root|/tmp|/var|/mnt|/media|/data|/workspace)"
    r"(?:/|\s|$)"
)
_BLOCKED_ABSOLUTE_RE = re.compile(
    r"(?is)(?<![\w.-])"
    r"(?:/home(?:/|\b)|/root(?:/|\b)|/data(?:/|\b)|/workspace(?:/|\b)|"
    r"/mnt(?:/|\b)|/media(?:/|\b)|/proc(?:/|\b)|/sys(?:/|\b)|/etc(?:/|\b)|"
    r"~(?:/|\b)|\$HOME(?:/|\b)|\$\{HOME\}(?:/|\b))"
)
_PATCH_PATH_RE = re.compile(
    r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$",
    re.MULTILINE,
)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_candidate(path: str | None, workspace_dir: Path) -> Path:
    raw = path or "."
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_dir / candidate
    return candidate.resolve(strict=False)


@dataclass(frozen=True)
class WorkspaceGuard:
    framework: str
    workspace_dir: Path
    run_home: Path | None = None
    audit_log_path: Path | None = None
    log_allows: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_dir", self.workspace_dir.resolve(strict=False))
        if self.run_home is not None:
            object.__setattr__(self, "run_home", self.run_home.resolve(strict=False))
        if self.audit_log_path is not None:
            object.__setattr__(self, "audit_log_path", self.audit_log_path.resolve(strict=False))

    def log(
        self,
        *,
        event: str,
        decision: str,
        subject: str = "",
        action: str = "",
        reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.audit_log_path is None:
            return
        payload: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "framework": self.framework,
            "event": event,
            "decision": decision,
            "action": action,
            "subject": subject[:1000],
            "reason": reason[:1000],
            "workspace": str(self.workspace_dir),
            "run_home": str(self.run_home) if self.run_home is not None else "",
        }
        if extra:
            payload["extra"] = extra
        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # Audit logging must never break an experiment run.
            pass

    def path_block_reason(self, path: str | None, *, label: str = "path") -> str | None:
        resolved = _resolve_candidate(path, self.workspace_dir)
        if resolved == self.workspace_dir or _is_relative_to(resolved, self.workspace_dir):
            if self.log_allows:
                self.log(
                    event="path_check",
                    decision="allow",
                    action=label,
                    subject=str(path),
                    extra={"resolved": str(resolved)},
                )
            return None
        reason = (
            f"BLOCKED: {label} must stay inside the task workspace. "
            f"Got {path!r}, resolved to {resolved}. Workspace: {self.workspace_dir}."
        )
        self.log(
            event="path_check",
            decision="block",
            action=label,
            subject=str(path),
            reason=reason,
            extra={"resolved": str(resolved)},
        )
        return reason

    def terminal_command_block_reason(self, command: str) -> str | None:
        # Preserve the actual workspace absolute path, then detect host-root
        # references elsewhere in the command string.
        normalized = command.replace(str(self.workspace_dir), "$TASK_WORKSPACE")
        allowed_roots_raw = os.environ.get("MEMENTO_BENCHMARK_ALLOWED_ROOTS", "")
        for raw in allowed_roots_raw.split(os.pathsep):
            if not raw:
                continue
            try:
                allowed = str(Path(raw).resolve(strict=False))
            except OSError:
                allowed = raw
            if allowed:
                normalized = normalized.replace(allowed, "$BENCHMARK_ALLOWED_ROOT")
        if self.run_home is not None:
            normalized = normalized.replace(str(self.run_home), "$RUN_HOME")
        if _BROAD_ROOT_COMMAND_RE.search(normalized):
            reason = (
                "BLOCKED: broad filesystem search/list commands rooted at a "
                "host directory are disabled for Table 3 runs. Use relative "
                "paths inside the task workspace."
            )
            self.log(
                event="command_check",
                decision="block",
                action="terminal",
                subject=command,
                reason=reason,
            )
            return reason
        match = _BLOCKED_ABSOLUTE_RE.search(normalized)
        if match:
            reason = (
                "BLOCKED: terminal commands may not reference host/root "
                f"directories outside the task workspace ({match.group(0)!r}). "
                "Use relative paths."
            )
            self.log(
                event="command_check",
                decision="block",
                action="terminal",
                subject=command,
                reason=reason,
            )
            return reason
        if self.log_allows:
            self.log(event="command_check", decision="allow", action="terminal", subject=command)
        return None

    def json_block(self, message: str) -> str:
        return json.dumps(
            {
                "output": "",
                "exit_code": -1,
                "error": message,
                "status": "blocked",
            },
            ensure_ascii=False,
        )


def make_workspace_guard(
    *,
    framework: str,
    workspace_dir: Path,
    run_home: Path | None = None,
    audit_log_path: Path | None = None,
    log_allows: bool | None = None,
) -> WorkspaceGuard:
    if log_allows is None:
        log_allows = os.environ.get("EVOLUTION_WORKSPACE_GUARD_LOG_ALLOW", "").lower() in {
            "1",
            "true",
            "yes",
        }
    return WorkspaceGuard(
        framework=framework,
        workspace_dir=Path(workspace_dir),
        run_home=Path(run_home) if run_home is not None else None,
        audit_log_path=Path(audit_log_path) if audit_log_path is not None else None,
        log_allows=log_allows,
    )


def ensure_task_venv(
    workspace_dir: Path,
    role: str | None,
    *,
    guard: WorkspaceGuard | None = None,
) -> Path | None:
    """Idempotently create a per-task venv whose base is the role-env python.

    Installs the agent performs (``pip install …``) land in this throwaway venv,
    never the shared host base env or the role env. Role libraries stay
    importable via ``--system-site-packages``. Returns the venv python path, or
    ``None`` if creation failed (the caller logs/falls back).
    """

    import subprocess

    from ..eval.python_env import resolve_python_for_role

    workspace_dir = Path(workspace_dir).resolve()
    venv_dir = workspace_dir / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        return venv_python
    base_python = resolve_python_for_role(role)
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [base_python, "-m", "venv", "--system-site-packages", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if guard is not None:
            guard.log(
                event="task_venv",
                decision="allow",
                action="create_failed",
                subject=str(venv_dir),
                reason=repr(exc),
            )
        return None
    if proc.returncode != 0 or not venv_python.exists():
        if guard is not None:
            guard.log(
                event="task_venv",
                decision="allow",
                action="create_failed",
                subject=str(venv_dir),
                reason=(proc.stderr or proc.stdout or "")[:500],
            )
        return None
    if guard is not None:
        guard.log(
            event="task_venv",
            decision="allow",
            action="create",
            subject=str(venv_python),
        )
    return venv_python


def _is_host_base_dir(seg: str) -> bool:
    if not seg:
        return False
    try:
        resolved = Path(seg).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for base in _HOST_BASE_ENV_DIRS:
        base_path = Path(base)
        if resolved == base_path or _is_relative_to(resolved, base_path):
            return True
    return False


def _strip_host_base_dirs(env: dict[str, str]) -> None:
    """Remove host base-env bin dirs from ``env['PATH']``.

    Bare ``python``/``python3``/``pip`` can then never resolve to the leaked
    host base conda env (the env wiped in the incident). The pre-strip PATH is
    preserved in ``EVOLUTION_WORKSPACE_GUARD_ORIG_PATH`` so the read/destructive
    command wrappers' ``_exec_real`` can still find real binaries.
    """

    orig_path = env.get("PATH", os.environ.get("PATH", ""))
    env.setdefault("EVOLUTION_WORKSPACE_GUARD_ORIG_PATH", orig_path)
    kept = [
        seg for seg in env.get("PATH", "").split(os.pathsep) if seg and not _is_host_base_dir(seg)
    ]
    env["PATH"] = os.pathsep.join(kept)


def _harden_path_for_venv(env: dict[str, str], venv_python: Path) -> None:
    """Prepend the venv bin and strip host base-env dirs from ``env['PATH']``.

    Bare ``python``/``python3``/``pip`` then resolve to the per-task venv, and
    the host base conda env is removed from resolution entirely.
    """

    venv_bin = str(venv_python.parent)
    orig_path = env.get("PATH", os.environ.get("PATH", ""))
    env.setdefault("EVOLUTION_WORKSPACE_GUARD_ORIG_PATH", orig_path)
    kept = [
        seg
        for seg in env.get("PATH", "").split(os.pathsep)
        if seg and seg != venv_bin and not _is_host_base_dir(seg)
    ]
    env["PATH"] = os.pathsep.join([venv_bin, *kept])
    env["VIRTUAL_ENV"] = str(venv_python.parent.parent)
    env.pop("PYTHONHOME", None)


def build_guarded_env(
    *,
    framework: str,
    workspace_dir: Path,
    run_home: Path | None = None,
    audit_log_path: Path | None = None,
    base_env: dict[str, str] | None = None,
    set_home: bool = False,
    command_wrappers: bool = False,
    python_fs_guard_on_pythonpath: bool = True,
    make_task_venv: bool = False,
    task_role: str | None = None,
) -> dict[str, str]:
    """Return env vars carrying workspace guard metadata.

    ``set_home`` is opt-in to avoid changing framework behavior accidentally.
    Frameworks that already require isolated HOME (Hermes/Memento) pass it
    explicitly. ``make_task_venv`` creates a per-task venv from the role-env
    python and hardens PATH so the agent's pip/python resolve to it, never the
    host base env.
    """

    guard = make_workspace_guard(
        framework=framework,
        workspace_dir=workspace_dir,
        run_home=run_home,
        audit_log_path=audit_log_path,
    )
    env = dict(base_env or os.environ)
    env["EVOLUTION_WORKSPACE_GUARD"] = "1"
    env["EVOLUTION_WORKSPACE_GUARD_FRAMEWORK"] = framework
    env["EVOLUTION_WORKSPACE_GUARD_WORKSPACE"] = str(guard.workspace_dir)
    if guard.run_home is not None:
        env["EVOLUTION_WORKSPACE_GUARD_RUN_HOME"] = str(guard.run_home)
    env["TASK_WORKSPACE"] = str(guard.workspace_dir)
    env["SB_OUTPUT"] = str(guard.workspace_dir)
    if guard.audit_log_path is not None:
        env["EVOLUTION_WORKSPACE_GUARD_LOG"] = str(guard.audit_log_path)
    if set_home and guard.run_home is not None:
        env["HOME"] = str(guard.run_home)
    if make_task_venv:
        venv_python = ensure_task_venv(guard.workspace_dir, task_role, guard=guard)
        if venv_python is not None:
            _harden_path_for_venv(env, venv_python)
            env["EVOLUTION_TASK_VENV_PYTHON"] = str(venv_python)
    if command_wrappers:
        _strip_host_base_dirs(env)
        _install_command_wrappers(guard, env)
        _install_python_fs_guard(
            guard,
            env,
            on_pythonpath=python_fs_guard_on_pythonpath,
        )
    guard.log(event="setup", decision="allow", action="env", subject="build_guarded_env")
    return env


def _install_command_wrappers(guard: WorkspaceGuard, env: dict[str, str]) -> None:
    """Prepend tiny PATH wrappers for broad filesystem commands.

    The wrappers are fail-open if the guard package cannot be imported, so they
    should not break third-party framework behavior. When import succeeds they
    apply the same command policy as in-process terminal tools and then exec the
    real binary from the original PATH.
    """

    root = guard.audit_log_path.parent if guard.audit_log_path is not None else guard.workspace_dir
    wrapper_dir = root / "_workspace_guard_bin"
    src_root = Path(__file__).resolve().parents[2]
    script = """#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import sys


def _exec_real() -> None:
    cmd = Path(sys.argv[0]).name
    real = shutil.which(cmd, path=os.environ.get("EVOLUTION_WORKSPACE_GUARD_ORIG_PATH", ""))
    if not real:
        print(f"workspace guard: real command not found for {cmd}", file=sys.stderr)
        sys.exit(127)
    os.execv(real, [real, *sys.argv[1:]])


src = os.environ.get("EVOLUTION_WORKSPACE_GUARD_SRC")
if src:
    sys.path.insert(0, src)

try:
    from evolution.adapters.workspace_guard import make_workspace_guard

    log = os.environ.get("EVOLUTION_WORKSPACE_GUARD_LOG") or ""
    guard = make_workspace_guard(
        framework=os.environ.get("EVOLUTION_WORKSPACE_GUARD_FRAMEWORK", "subprocess"),
        workspace_dir=Path(os.environ["EVOLUTION_WORKSPACE_GUARD_WORKSPACE"]),
        audit_log_path=Path(log) if log else None,
    )
    rendered = " ".join(shlex.quote(arg) for arg in [Path(sys.argv[0]).name, *sys.argv[1:]])
    reason = guard.terminal_command_block_reason(rendered)
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(126)
except Exception:
    # Preserve framework functionality if guard bootstrap fails.
    pass

_exec_real()
"""
    # Destructive commands (rm/mv/...) get a path-resolving policy: every
    # non-flag argument (and the value of key=value forms like dd of=PATH) is
    # resolved against cwd and refused if it lands outside the task workspace or
    # run-home. This catches relative traversal (rm -rf ../../x) as well as
    # absolute host paths, while leaving in-workspace cleanup untouched.
    destructive_script = """#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def _exec_real() -> None:
    cmd = Path(sys.argv[0]).name
    real = shutil.which(cmd, path=os.environ.get("EVOLUTION_WORKSPACE_GUARD_ORIG_PATH", ""))
    if not real:
        print(f"workspace guard: real command not found for {cmd}", file=sys.stderr)
        sys.exit(127)
    os.execv(real, [real, *sys.argv[1:]])


def _allowed_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("EVOLUTION_WORKSPACE_GUARD_WORKSPACE", "EVOLUTION_WORKSPACE_GUARD_RUN_HOME"):
        val = os.environ.get(key)
        if val:
            try:
                roots.append(Path(val).resolve())
            except (OSError, RuntimeError, ValueError):
                pass
    import tempfile
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _candidate_paths(arg: str):
    if not arg or arg.startswith("-"):
        return
    yield arg
    if "=" in arg:
        tail = arg.split("=", 1)[1]
        if tail:
            yield tail


def _blocked(arg: str, roots: list[Path]) -> bool:
    if not roots:
        return False
    for cand in _candidate_paths(arg):
        try:
            p = Path(cand).expanduser()
        except (TypeError, ValueError):
            continue
        if not p.is_absolute():
            p = Path.cwd() / p
        resolved = p.resolve(strict=False)
        if not any(resolved == r or _is_relative_to(resolved, r) for r in roots):
            return True
    return False


try:
    _roots = _allowed_roots()
    _bad = [a for a in sys.argv[1:] if _blocked(a, _roots)]
    if _bad:
        print(
            "BLOCKED: "
            + Path(sys.argv[0]).name
            + " target(s) outside the task workspace are disabled: "
            + repr(_bad),
            file=sys.stderr,
        )
        sys.exit(126)
except Exception:
    pass

_exec_real()
"""
    try:
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        for name in _WRAPPED_COMMANDS:
            path = wrapper_dir / name
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        for name in _DESTRUCTIVE_COMMANDS:
            path = wrapper_dir / name
            path.write_text(destructive_script, encoding="utf-8")
            path.chmod(0o755)
    except OSError as exc:
        guard.log(
            event="command_wrappers",
            decision="allow",
            action="install_failed",
            subject=str(wrapper_dir),
            reason=repr(exc),
        )
        return

    orig_path = env.get("EVOLUTION_WORKSPACE_GUARD_ORIG_PATH") or env.get("PATH", "")
    env["EVOLUTION_WORKSPACE_GUARD_ORIG_PATH"] = orig_path
    env["EVOLUTION_WORKSPACE_GUARD_SRC"] = str(src_root)
    # Prepend the wrapper dir to the CURRENT PATH so venv-hardening (if any) is
    # preserved; do not rebuild from ORIG_PATH (which still contains host dirs).
    current_path = env.get("PATH", "")
    env["PATH"] = str(wrapper_dir) + (os.pathsep + current_path if current_path else "")
    guard.log(
        event="command_wrappers",
        decision="allow",
        action="install",
        subject=str(wrapper_dir),
        extra={"commands": list(_WRAPPED_COMMANDS) + list(_DESTRUCTIVE_COMMANDS)},
    )


def _install_python_fs_guard(
    guard: WorkspaceGuard,
    env: dict[str, str],
    *,
    on_pythonpath: bool,
) -> None:
    """Install a sitecustomize guard for generated Python helper scripts.

    Shell wrappers catch broad host-root searches such as ``find /home``.
    Some agents instead write and execute Python scripts that call
    ``os.walk('/home/...')`` or ``glob.glob('/home/**/*.pdf')``. Since those
    run in a separate interpreter, we prepend a tiny ``sitecustomize`` module
    that blocks recursive enumeration outside the task workspace while leaving
    normal imports and direct library file reads alone.
    """

    root = guard.audit_log_path.parent if guard.audit_log_path is not None else guard.workspace_dir
    site_dir = root / "_workspace_guard_py"
    script = r"""from __future__ import annotations

import glob as _glob
import json as _json
import os as _os
from pathlib import Path as _Path
import time as _time


_WORKSPACE = _Path(_os.environ.get("EVOLUTION_WORKSPACE_GUARD_WORKSPACE", ".")).resolve()
_RUN_HOME_RAW = _os.environ.get("EVOLUTION_WORKSPACE_GUARD_RUN_HOME", "")
_RUN_HOME = _Path(_RUN_HOME_RAW).resolve() if _RUN_HOME_RAW else None
_LOG = _os.environ.get("EVOLUTION_WORKSPACE_GUARD_LOG") or ""
_FRAMEWORK = _os.environ.get("EVOLUTION_WORKSPACE_GUARD_FRAMEWORK", "python")
_PROTECTED_ROOTS = tuple(_Path(p) for p in (
    "/home",
    "/root",
    "/tmp",
    "/data",
    "/workspace",
    "/mnt",
    "/media",
    "/proc",
    "/sys",
    "/etc",
))


def _is_relative_to(path: _Path, root: _Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _log(action: str, subject: object, reason: str) -> None:
    if not _LOG:
        return
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(
                _json.dumps(
                    {
                        "ts": round(_time.time(), 3),
                        "framework": _FRAMEWORK,
                        "event": "python_fs_check",
                        "decision": "block",
                        "action": action,
                        "subject": str(subject)[:1000],
                        "reason": reason[:1000],
                        "workspace": str(_WORKSPACE),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _outside_workspace(path: object) -> bool:
    try:
        p = _Path(path).expanduser()
    except TypeError:
        return False
    if not p.is_absolute():
        return False
    resolved = p.resolve(strict=False)
    if resolved == _WORKSPACE or _is_relative_to(resolved, _WORKSPACE):
        return False
    if _RUN_HOME is not None and (resolved == _RUN_HOME or _is_relative_to(resolved, _RUN_HOME)):
        return False
    if not any(resolved == root or _is_relative_to(resolved, root) for root in _PROTECTED_ROOTS):
        return False
    return True


def _glob_root(pattern: object) -> _Path | None:
    if not isinstance(pattern, (str, bytes, _os.PathLike)):
        return None
    text = _os.fsdecode(pattern)
    if not text.startswith("/"):
        return None
    cut = len(text)
    for marker in ("*", "?", "["):
        pos = text.find(marker)
        if pos >= 0:
            cut = min(cut, pos)
    prefix = text[:cut] or "/"
    return _Path(prefix).resolve(strict=False)


_orig_os_walk = _os.walk


def _guarded_os_walk(top, *args, **kwargs):
    if _outside_workspace(top):
        _log("os.walk", top, "recursive host-root traversal is disabled")
        return iter(())
    return _orig_os_walk(top, *args, **kwargs)


_os.walk = _guarded_os_walk

_orig_glob = _glob.glob
_orig_iglob = _glob.iglob


def _glob_blocked(pattern) -> bool:
    root = _glob_root(pattern)
    if root is None:
        return False
    protected = any(root == protected_root or _is_relative_to(root, protected_root) for protected_root in _PROTECTED_ROOTS)
    blocked = protected and root != _WORKSPACE and not _is_relative_to(root, _WORKSPACE)
    if blocked and _RUN_HOME is not None:
        blocked = root != _RUN_HOME and not _is_relative_to(root, _RUN_HOME)
    if blocked:
        _log("glob", pattern, "absolute glob outside task workspace is disabled")
    return blocked


def _guarded_glob(pathname, *args, **kwargs):
    if _glob_blocked(pathname):
        return []
    return _orig_glob(pathname, *args, **kwargs)


def _guarded_iglob(pathname, *args, **kwargs):
    if _glob_blocked(pathname):
        return iter(())
    return _orig_iglob(pathname, *args, **kwargs)


_glob.glob = _guarded_glob
_glob.iglob = _guarded_iglob

_orig_path_glob = _Path.glob
_orig_path_rglob = _Path.rglob


def _guarded_path_glob(self, pattern, *args, **kwargs):
    if _outside_workspace(self):
        _log("Path.glob", self, "path glob outside task workspace is disabled")
        return iter(())
    return _orig_path_glob(self, pattern, *args, **kwargs)


def _guarded_path_rglob(self, pattern, *args, **kwargs):
    if _outside_workspace(self):
        _log("Path.rglob", self, "path rglob outside task workspace is disabled")
        return iter(())
    return _orig_path_rglob(self, pattern, *args, **kwargs)


_Path.glob = _guarded_path_glob
_Path.rglob = _guarded_path_rglob


import builtins as _builtins
import io as _io
import shutil as _shutil
import tempfile as _tempfile


def _mutate_roots() -> list:
    roots = [_WORKSPACE]
    if _RUN_HOME is not None:
        roots.append(_RUN_HOME)
    try:
        roots.append(_Path(_tempfile.gettempdir()).resolve())
    except Exception:
        pass
    return roots


_MUTATE_ROOTS = _mutate_roots()

_SAFE_DEVICE_FILES = frozenset({
    "/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
    "/dev/tty", "/dev/stdin", "/dev/stdout", "/dev/stderr",
})


def _is_safe_special(p) -> bool:
    try:
        s = p.as_posix()
    except Exception:
        return False
    if s in _SAFE_DEVICE_FILES:
        return True
    return s.startswith("/dev/fd/") or s.startswith("/proc/self/fd/") or (
        s.startswith("/proc/") and "/fd/" in s
    )


def _mutation_blocked(path) -> bool:
    if isinstance(path, int):
        return False
    try:
        raw = _os.fsdecode(path) if isinstance(path, (bytes, bytearray)) else path
        p = _Path(raw).expanduser()
    except (TypeError, ValueError):
        return False
    if not p.is_absolute():
        try:
            p = _Path.cwd() / p
        except (OSError, ValueError):
            return False
    if _is_safe_special(p):
        return False
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    if _is_safe_special(resolved):
        return False
    return not any(resolved == r or _is_relative_to(resolved, r) for r in _MUTATE_ROOTS)


def _is_log_path(file) -> bool:
    if not _LOG:
        return False
    try:
        raw = _os.fsdecode(file) if isinstance(file, (bytes, bytearray)) else file
        return _Path(raw) == _Path(_LOG)
    except (TypeError, ValueError):
        return False


def _refuse(action: str, target) -> None:
    _log(action, target, "mutation outside task workspace is disabled")
    raise PermissionError(
        "workspace guard: refusing " + action + " outside the task workspace: " + str(target)[:200]
    )


_orig_os_remove = _os.remove
_orig_os_unlink = _os.unlink
_orig_os_rmdir = _os.rmdir
_orig_os_rename = _os.rename
_orig_os_replace = _os.replace


def _guarded_os_remove(path, *args, **kwargs):
    if _mutation_blocked(path):
        _refuse("os.remove", path)
    return _orig_os_remove(path, *args, **kwargs)


def _guarded_os_unlink(path, *args, **kwargs):
    if _mutation_blocked(path):
        _refuse("os.unlink", path)
    return _orig_os_unlink(path, *args, **kwargs)


def _guarded_os_rmdir(path, *args, **kwargs):
    if _mutation_blocked(path):
        _refuse("os.rmdir", path)
    return _orig_os_rmdir(path, *args, **kwargs)


def _guarded_os_rename(src, dst, *args, **kwargs):
    if _mutation_blocked(src) or _mutation_blocked(dst):
        _refuse("os.rename", dst)
    return _orig_os_rename(src, dst, *args, **kwargs)


def _guarded_os_replace(src, dst, *args, **kwargs):
    if _mutation_blocked(src) or _mutation_blocked(dst):
        _refuse("os.replace", dst)
    return _orig_os_replace(src, dst, *args, **kwargs)


_os.remove = _guarded_os_remove
_os.unlink = _guarded_os_unlink
_os.rmdir = _guarded_os_rmdir
_os.rename = _guarded_os_rename
_os.replace = _guarded_os_replace


_orig_shutil_rmtree = _shutil.rmtree


def _guarded_shutil_rmtree(path, *args, **kwargs):
    if _mutation_blocked(path):
        _refuse("shutil.rmtree", path)
    return _orig_shutil_rmtree(path, *args, **kwargs)


_shutil.rmtree = _guarded_shutil_rmtree


_orig_path_unlink = _Path.unlink
_orig_path_rmdir = _Path.rmdir


def _guarded_path_unlink(self, *args, **kwargs):
    if _mutation_blocked(self):
        _refuse("Path.unlink", self)
    return _orig_path_unlink(self, *args, **kwargs)


def _guarded_path_rmdir(self, *args, **kwargs):
    if _mutation_blocked(self):
        _refuse("Path.rmdir", self)
    return _orig_path_rmdir(self, *args, **kwargs)


_Path.unlink = _guarded_path_unlink
_Path.rmdir = _guarded_path_rmdir


def _is_write_mode(mode) -> bool:
    if not isinstance(mode, str):
        return False
    return any(ch in mode for ch in ("w", "a", "x", "+"))


_orig_builtins_open = _builtins.open


def _guarded_open(file, mode="r", *args, **kwargs):
    if _is_write_mode(mode) and not _is_log_path(file) and _mutation_blocked(file):
        _refuse("open", file)
    return _orig_builtins_open(file, mode, *args, **kwargs)


_builtins.open = _guarded_open
_io.open = _guarded_open
"""
    try:
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(script, encoding="utf-8")
    except OSError as exc:
        guard.log(
            event="python_fs_guard",
            decision="allow",
            action="install_failed",
            subject=str(site_dir),
            reason=repr(exc),
        )
        return

    if on_pythonpath:
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(site_dir) + (os.pathsep + old if old else "")
    env["EVOLUTION_PYTHON_FS_GUARD_SITE_DIR"] = str(site_dir)
    env["EVOLUTION_PYTHON_FS_GUARD"] = "1"
    guard.log(
        event="python_fs_guard",
        decision="allow",
        action="install",
        subject=str(site_dir),
    )


def configure_process_workspace(
    *,
    framework: str,
    workspace_dir: Path,
    run_home: Path | None = None,
    audit_log_path: Path | None = None,
    set_home: bool = False,
    command_wrappers: bool = False,
    make_task_venv: bool = False,
    task_role: str | None = None,
) -> WorkspaceGuard:
    """Mutate ``os.environ`` with guard metadata for in-process frameworks."""

    env = build_guarded_env(
        framework=framework,
        workspace_dir=workspace_dir,
        run_home=run_home,
        audit_log_path=audit_log_path,
        base_env=os.environ,
        set_home=set_home,
        command_wrappers=command_wrappers,
        make_task_venv=make_task_venv,
        task_role=task_role,
    )
    os.environ.update(env)
    return make_workspace_guard(
        framework=framework,
        workspace_dir=workspace_dir,
        run_home=run_home,
        audit_log_path=audit_log_path,
    )


def configure_hermes_terminal_env(
    *,
    workspace_dir: Path,
    run_home: Path,
    terminal_timeout_s: int = 45,
    audit_log_path: Path | None = None,
    make_task_venv: bool = False,
    task_role: str | None = None,
) -> WorkspaceGuard:
    """Configure Hermes-specific terminal env vars through the generic guard."""

    isolated_home = run_home / "home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    guard = configure_process_workspace(
        framework="hermes",
        workspace_dir=workspace_dir,
        run_home=isolated_home,
        audit_log_path=audit_log_path,
        set_home=True,
        command_wrappers=True,
        make_task_venv=make_task_venv,
        task_role=task_role,
    )
    os.environ["TERMINAL_CWD"] = str(guard.workspace_dir)
    os.environ["HERMES_WORKSPACE"] = str(guard.workspace_dir)
    os.environ["HOME"] = str(
        isolated_home
    )  # isolate $HOME: agent/uv dotfile writes hit the ephemeral run home, not real ~/.zshrc
    sandbox_dir = Path(run_home).resolve() / "sandboxes"
    scratch_dir = Path(run_home).resolve() / "scratch"
    tmp_dir = Path(run_home).resolve() / "tmp"
    cache_dir = Path(run_home).resolve() / "cache"
    config_dir = Path(run_home).resolve() / "config"
    data_dir = Path(run_home).resolve() / "data"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TERMINAL_SANDBOX_DIR"] = str(sandbox_dir)
    os.environ["TERMINAL_SCRATCH_DIR"] = str(scratch_dir)
    os.environ["APPTAINER_CACHEDIR"] = str(scratch_dir / ".apptainer")
    os.environ["TMPDIR"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["XDG_CACHE_HOME"] = str(cache_dir)
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ.setdefault("TERMINAL_ENV", "local")
    os.environ["TERMINAL_TIMEOUT"] = str(max(1, int(terminal_timeout_s)))
    os.environ["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = str(max(1, int(terminal_timeout_s)))
    os.environ.setdefault("TERMINAL_LOCAL_PERSISTENT", "false")
    guard.log(
        event="setup",
        decision="allow",
        action="hermes_terminal_env",
        subject=str(guard.workspace_dir),
        extra={"terminal_timeout_s": int(terminal_timeout_s)},
    )
    return guard


def install_framework_tool_guards(
    *,
    framework: str,
    workspace_dir: Path,
    run_home: Path | None = None,
    audit_log_path: Path | None = None,
    disable_terminal: bool = False,
) -> WorkspaceGuard:
    """Install in-process tool guards for frameworks with patchable tools."""

    guard = make_workspace_guard(
        framework=framework,
        workspace_dir=workspace_dir,
        run_home=run_home,
        audit_log_path=audit_log_path,
    )
    if framework == "hermes":
        _install_hermes_tool_guards(guard, disable_terminal=disable_terminal)
        return guard
    guard.log(
        event="tool_adapter",
        decision="allow",
        action="noop",
        subject=framework,
        reason="no in-process tool adapter registered for this framework",
    )
    return guard


def _install_hermes_tool_guards(guard: WorkspaceGuard, *, disable_terminal: bool = False) -> None:
    import tools.file_tools as file_tools  # type: ignore
    import tools.terminal_tool as terminal_tool  # type: ignore

    if not hasattr(terminal_tool, "_evolution_orig_terminal_tool"):
        terminal_tool._evolution_orig_terminal_tool = terminal_tool.terminal_tool

    orig_terminal: Callable[..., str] = terminal_tool._evolution_orig_terminal_tool

    def guarded_terminal_tool(
        command: str,
        background: bool = False,
        timeout: int | None = None,
        task_id: str | None = None,
        force: bool = False,
        workdir: str | None = None,
        pty: bool = False,
        notify_on_complete: bool = False,
        watch_patterns: list[str] | None = None,
    ) -> str:
        if disable_terminal:
            reason = (
                "BLOCKED: Hermes terminal tools are disabled in skill-edit mode; "
                "use skill_view and skill_manage only."
            )
            guard.log(
                event="command_check",
                decision="block",
                action="terminal",
                subject=command if isinstance(command, str) else repr(command),
                reason=reason,
            )
            return guard.json_block(reason)
        if not isinstance(command, str):
            return orig_terminal(
                command=command,
                background=background,
                timeout=timeout,
                task_id=task_id,
                force=force,
                workdir=workdir,
                pty=pty,
                notify_on_complete=notify_on_complete,
                watch_patterns=watch_patterns,
            )
        if workdir:
            reason = guard.path_block_reason(workdir, label="terminal workdir")
            if reason:
                return guard.json_block(reason)
        reason = guard.terminal_command_block_reason(command)
        if reason:
            return guard.json_block(reason)
        return orig_terminal(
            command=command,
            background=background,
            timeout=timeout,
            task_id=task_id,
            force=force,
            workdir=workdir,
            pty=pty,
            notify_on_complete=notify_on_complete,
            watch_patterns=watch_patterns,
        )

    terminal_tool.terminal_tool = guarded_terminal_tool

    if not hasattr(file_tools, "_evolution_orig_read_file_tool"):
        file_tools._evolution_orig_read_file_tool = file_tools.read_file_tool
        file_tools._evolution_orig_write_file_tool = file_tools.write_file_tool
        file_tools._evolution_orig_patch_tool = file_tools.patch_tool
        file_tools._evolution_orig_search_tool = file_tools.search_tool

    orig_read: Callable[..., str] = file_tools._evolution_orig_read_file_tool
    orig_write: Callable[..., str] = file_tools._evolution_orig_write_file_tool
    orig_patch: Callable[..., str] = file_tools._evolution_orig_patch_tool
    orig_search: Callable[..., str] = file_tools._evolution_orig_search_tool

    def guarded_read_file_tool(path: str, *args: Any, **kwargs: Any) -> str:
        reason = guard.path_block_reason(path, label="read_file path")
        if reason:
            return guard.json_block(reason)
        return orig_read(path, *args, **kwargs)

    def guarded_write_file_tool(path: str, content: str, *args: Any, **kwargs: Any) -> str:
        reason = guard.path_block_reason(path, label="write_file path")
        if reason:
            return guard.json_block(reason)
        return orig_write(path, content, *args, **kwargs)

    def guarded_patch_tool(*args: Any, **kwargs: Any) -> str:
        path = kwargs.get("path")
        if len(args) >= 2:
            path = args[1]
        if path:
            reason = guard.path_block_reason(str(path), label="patch path")
            if reason:
                return guard.json_block(reason)
        patch = kwargs.get("patch")
        if patch is None and len(args) >= 6:
            patch = args[5]
        if patch:
            for match in _PATCH_PATH_RE.finditer(str(patch)):
                reason = guard.path_block_reason(match.group(1).strip(), label="patch file")
                if reason:
                    return guard.json_block(reason)
        return orig_patch(*args, **kwargs)

    def guarded_search_tool(
        pattern: str,
        target: str = "content",
        path: str = ".",
        *args: Any,
        **kwargs: Any,
    ) -> str:
        reason = guard.path_block_reason(path, label="search_files path")
        if reason:
            return guard.json_block(reason)
        return orig_search(pattern, target, path, *args, **kwargs)

    file_tools.read_file_tool = guarded_read_file_tool
    file_tools.write_file_tool = guarded_write_file_tool
    file_tools.patch_tool = guarded_patch_tool
    file_tools.search_tool = guarded_search_tool
    guard.log(event="tool_adapter", decision="allow", action="install", subject="hermes")


# Backward-compatible aliases for callers/tests that only need the pure checks.
def path_block_reason(path: str | None, workspace_dir: Path, *, label: str = "path") -> str | None:
    return make_workspace_guard(framework="generic", workspace_dir=workspace_dir).path_block_reason(
        path,
        label=label,
    )


def terminal_command_block_reason(command: str, workspace_dir: Path) -> str | None:
    return make_workspace_guard(
        framework="generic",
        workspace_dir=workspace_dir,
    ).terminal_command_block_reason(command)


__all__ = [
    "WorkspaceGuard",
    "build_guarded_env",
    "configure_hermes_terminal_env",
    "configure_process_workspace",
    "install_framework_tool_guards",
    "make_workspace_guard",
    "path_block_reason",
    "terminal_command_block_reason",
]
