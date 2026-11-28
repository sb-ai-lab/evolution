"""Clone the team repository into a Lab-owned working copy (never push; fetches only through ``lab project sync``, PDR-0024)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from .errors import ProjectError

_TIMEOUT = 600
_TAIL_LINES = 20
_LOG = logging.getLogger("evolution_lab.projects")
# ``scheme://<userinfo>@`` and a bare ``user:secret@`` (git echoes a git:// host that way).
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s'\"]*@")
_BARE_CREDENTIALS = re.compile(r"(?<![^\s'\"(])[^/\s'\"@:]+:[^/\s'\"@]+@")


def is_url(template: str) -> bool:
    """A git URL by its syntax, as git reads it, not by what is on disk: ``scheme://`` or scp-like ``host:path``
    (a ``:`` with no ``/`` before it). The one URL check of the port and the CLI's ``--clone``/``--template``."""
    if "://" in template:
        return True
    head, colon, _ = template.partition(":")
    return bool(colon) and "/" not in head


def _redact(text: str) -> str:
    return _BARE_CREDENTIALS.sub("***@", _URL_USERINFO.sub(r"\1***@", text))


def _warn(argv: tuple[str, ...], what: str, stderr: str | bytes | None) -> None:
    """The closed ``clone_failed`` envelope cannot carry git's diagnostics, so its stderr tail
    goes to the service log instead (codex review: clone-wrapper-git-semantics)."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    tail = "\n".join((stderr or "").splitlines()[-_TAIL_LINES:])
    _LOG.warning("git %s %s; stderr tail:\n%s", argv[0] if argv else "", what, _redact(tail),
                 extra={"lab_code": "clone_failed"})


def _ssh_command_configured(env: dict) -> bool:
    """Whether git config (system or global scope) names ``core.sshCommand``. ``GIT_DIR`` points at no
    repository, so a local config around the process's directory is not read — the clone never reads it either."""
    try:
        result = subprocess.run(["git", "config", "--get", "core.sshCommand"], env={**env, "GIT_DIR": os.devnull},
                                capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


# Variables that choose or locate a repository instead of the command's cwd (``git help environment``): the
# superset of ``git rev-parse --local-env-vars`` (git 2.5x) and the discovery/namespace ones it does not list.
# Inherited by the service, they would send a clone's or a port's git commands into another repository, e.g. the
# project's working copy (PDR-0018, codex r1 of the dev35 branch). A fixed tuple, not that probe: no git call per
# command, and the dropped set never depends on the installed git.
REPOSITORY_SELECTORS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_PREFIX", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_GRAFT_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE",
)


def scrubbed_environment() -> dict:
    """The service's environment without the repository selectors: every git command Lab runs (clone, port, the
    revision's ``git_state``) works only in the directory it names."""
    return {name: value for name, value in os.environ.items() if name not in REPOSITORY_SELECTORS}


def _environment() -> dict:
    """The environment of a clone's or a port's git commands: scrubbed, never prompting."""
    env = scrubbed_environment()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env and not _ssh_command_configured(env):
        # Never wait on an ssh password/passphrase prompt (final review M5); a caller's own
        # ssh command, from the environment or git config, is kept as is (R-b).
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def _git(*argv: str, cwd: Path | None = None, env: dict | None = None) -> str:
    env = _environment() if env is None else env
    try:
        result = subprocess.run(["git", *argv], cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as error:
        _warn(argv, f"timed out after {_TIMEOUT} s", error.stderr)
        raise ProjectError("clone_failed") from None
    except OSError as error:
        _warn(argv, f"could not run ({error.strerror or type(error).__name__})", None)
        raise ProjectError("clone_failed") from None
    if result.returncode != 0:
        _warn(argv, f"exited {result.returncode}", result.stderr)
        raise ProjectError("clone_failed")
    return result.stdout.strip()


def _resolves(ref: str, destination: Path, env: dict) -> bool:
    try:
        result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=destination,
                                env=env, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _start_point(rev: str, destination: Path, env: dict) -> str:
    """The first of ``refs/remotes/origin/<rev>``, ``refs/tags/<rev>`` that names a commit in the
    fresh clone, else the bare ``rev`` as a commit-ish (codex review, R-c): a clone has only the
    default branch locally, and a hex-named branch must not be read as a commit prefix. A bare
    ``rev`` that does not resolve fails in checkout, whose stderr is logged."""
    for candidate in (f"refs/remotes/origin/{rev}", f"refs/tags/{rev}"):
        if _resolves(candidate, destination, env):
            return candidate
    return rev


def _option_like(rev: object) -> bool:
    """No revision starts with ``-``; such a ``rev`` would reach git as an option."""
    return isinstance(rev, str) and rev.startswith("-")


def clone_repository(repo: str, rev: str | None, destination: Path, branch: str) -> dict:
    if _option_like(rev):
        raise ProjectError("invalid_request")
    env = _environment()
    _git("clone", "--quiet", "--", repo, str(destination), env=env)
    start = [_start_point(rev, destination, env)] if rev else []
    # --no-track: ``lab/<id>`` is Lab's own branch, never an upstream of the team's (Lab never
    # pushes; it fetches only through ``lab project sync``, PDR-0024).
    # No --end-of-options: an older git checkout takes it for the start point and fails (seen on 2.34 and 2.43,
    # the git of Ubuntu 22.04 and 24.04). The start point never looks like an option: a bare ``rev`` starting with ``-`` is refused
    # above, and the probed refs always start with ``refs/``.
    _git("checkout", "--quiet", "--no-track", "-b", branch, *start, cwd=destination, env=env)
    return {"repo": repo, "base_revision": _git("rev-parse", "HEAD", cwd=destination, env=env), "branch": branch}
