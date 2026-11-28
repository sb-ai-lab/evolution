"""``lab project port`` (spec 2026-10-01 §3, PDR-0018, ADR-0020): a port repository built from a template's
tracked files at one commit, registered as an implementation ``ported_from`` its source in one registry
transaction, with the template provenance in the ``ports`` table.

Three phases. ``plan_port``: the checks of §3.1, before any write. ``stage``: the template snapshot, ``git init``,
the first commit and the ``lab/port/created`` tag in a staging directory next to the destination — slow, outside
the session's mutation lock. ``publish``: the same checks again, one ``rename`` and the registry transaction, under
the lock; the directory is removed only when the transaction did not commit. Lab never writes into the working
copy, never sets a remote, never pushes, never touches the global git config, and never logs a git argv (a
template URL may carry credentials)."""
from __future__ import annotations

import errno
import io
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from .clone import _environment, _redact, is_url
from .errors import ProjectError
from .ids import validate_id

PORT_TAG = "lab/port/created"
_TIMEOUT = 600
_TAIL_LINES = 20
_LOG = logging.getLogger("evolution_lab.projects")
_NOT_A_REPOSITORY = "does not appear to be a git repository"
_NO_REMOTE_REF = "couldn't find remote ref"
_FULL_HASH = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
# The URL git calls run with no credential helper: a token in the template URL is never approved into a store.
_NO_CREDENTIAL_HELPER = ("-c", "credential.helper=")
_LAB_IDENTITY = ("-c", "user.name=Evolution Lab", "-c", "user.email=lab@evolution-lab.invalid")

# The ownership mark of a temporary directory a port creates (codex r2, PDR-0018): an empty regular file *next to* the
# directory, named after it, so the published tree never carries it and no template content can forge it. Lab's
# sweep at open removes a directory only with this mark (``registry._sweep_port_leftovers``).
LEFTOVER_MARKER_SUFFIX = ".lab-port-leftover"

__all__ = ["LEFTOVER_MARKER_SUFFIX", "PORT_TAG", "PortPlan", "Staged", "is_leftover_marker", "is_marked_leftover",
           "is_url", "mark_leftover", "plan_port", "publish", "recheck", "stage", "unmark_leftover"]


def _marker(directory: Path) -> Path:
    return directory.parent / f"{directory.name}{LEFTOVER_MARKER_SUFFIX}"


def mark_leftover(directory: Path) -> None:
    """Mark ``directory`` (just created by this port) as Lab's until :func:`unmark_leftover`."""
    fd = os.open(_marker(directory), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def unmark_leftover(directory: Path) -> None:
    try:
        os.unlink(_marker(directory))
    except FileNotFoundError:
        pass


def is_leftover_marker(path: Path) -> bool:
    """A marker is a regular file (never a symlink) with the marker suffix."""
    try:
        return path.name.endswith(LEFTOVER_MARKER_SUFFIX) and stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def is_marked_leftover(directory: Path) -> bool:
    """Whether ``directory`` is a real directory (not a symlink) that carries Lab's port-leftover mark."""
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return False
    except OSError:
        return False
    return is_leftover_marker(_marker(directory))


@dataclass(frozen=True, slots=True)
class PortPlan:
    project_id: str
    source_id: str
    port_id: str
    destination: Path
    template: str = field(repr=False)  # as given: used only to fetch, never stored, shown, logged or repr'd
    template_display: str  # credentials redacted: what `ports`, the commit message and the output carry
    ref: str
    default_destination: bool  # LAB_HOME/ports/<project>/<port-id>: the only destination an error may name


@dataclass(frozen=True, slots=True)
class Staged:
    directory: Path
    commit: str  # the template commit the snapshot was taken at


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _check_source(project, source_id: str) -> None:
    source = project.implementation(source_id)
    if source is None:
        raise ProjectError("implementation_not_found")
    if source.archived is not None:
        raise ProjectError("implementation_archived")


def _check_id(project, port_id: str) -> None:
    if project.implementation(port_id) is not None:  # archived ones keep their id
        raise ProjectError("id_taken")


def _taken(destination: Path, default: bool) -> ProjectError:
    """``port_destination_exists``: Lab names its own default place; an ``--into`` path is request data, not echoed."""
    return ProjectError("port_destination_exists", str(destination) if default else None)


def _check_destination(project, home: Path, destination: Path, default: bool) -> None:
    if _inside(destination.resolve(), Path(project.working_copy.path).resolve()):
        raise ProjectError("port_into_working_copy")
    if not default and _inside(destination.resolve(), Path(home).resolve()):
        # LAB_HOME is Lab's own: its open sweeps leftovers there, so no caller's port may land in it.
        raise ProjectError("port_into_lab_home")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise _taken(destination, default)


def _default_id(project, source_id: str) -> str:
    taken = {item.id for item in project.implementations}
    candidate, number = f"{source_id}-port", 1
    while candidate in taken:
        number += 1
        candidate = f"{source_id}-port-{number}"
    return validate_id(candidate)


def plan_port(projects, project_id: str, *, source_id: str, template: str, ref: str | None, port_id: str | None,
              into: str | None) -> PortPlan:
    """§3.1, before any write: the source, the port id, git, the destination."""
    project = projects.ready(project_id)
    if not isinstance(source_id, str) or not source_id:
        raise ProjectError("invalid_request")
    ref = "HEAD" if ref is None else ref
    for value in (template, ref):
        if not isinstance(value, str) or not value or value.startswith("-") or "\x00" in value:
            raise ProjectError("invalid_request")
    if not is_url(template) and not os.path.isabs(template):
        raise ProjectError("invalid_request")
    _check_source(project, source_id)
    port_id = _default_id(project, source_id) if port_id is None else validate_id(port_id)
    _check_id(project, port_id)
    if shutil.which("git") is None:
        raise ProjectError("git_unavailable")
    if into is None:
        destination = projects.home / "ports" / project.id / port_id
    elif isinstance(into, str) and os.path.isabs(into) and "\x00" not in into:
        destination = Path(os.path.normpath(into))
    else:
        raise ProjectError("invalid_request")
    _check_destination(project, projects.home, destination, into is None)
    return PortPlan(project.id, source_id, port_id, destination, template, _redact(template), ref, into is None)


def recheck(projects, plan: PortPlan) -> None:
    """§3.4: publication repeats §3.1 against the project as it is now (the fetch ran outside the lock)."""
    project = projects.ready(plan.project_id)
    _check_source(project, plan.source_id)
    _check_id(project, plan.port_id)
    _check_destination(project, projects.home, plan.destination, plan.default_destination)


def _log(result: subprocess.CompletedProcess, code: str) -> None:
    """git's stderr tail, credentials redacted; the argv is never logged (it may hold the template URL)."""
    tail = "\n".join((result.stderr or "").splitlines()[-_TAIL_LINES:])
    _LOG.warning("git exited %s; stderr tail:\n%s", result.returncode, _redact(tail), extra={"lab_code": code})


def _run(argv: list[str], *, cwd: Path | None, env: dict, code: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        _LOG.warning("git could not run: %s", type(error).__name__, extra={"lab_code": code})
        raise ProjectError(code) from None


def _ok(argv: list[str], *, cwd: Path | None, env: dict, code: str) -> str:
    result = _run(argv, cwd=cwd, env=env, code=code)
    if result.returncode != 0:
        _log(result, code)
        raise ProjectError(code)
    return result.stdout.strip()


def _extract(repository: Path, commit: str, staging: Path, env: dict) -> None:
    """The tracked files of ``commit`` as ``git archive`` gives them: no history, no remote."""
    try:
        archive = subprocess.run(["git", "--no-optional-locks", "archive", "--format=tar", commit], cwd=repository,
                                 env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ProjectError("template_fetch_failed") from None
    if archive.returncode != 0:
        raise ProjectError("template_fetch_failed")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        # "tar" keeps the template's own symlinks and refuses absolute or ".." member names; Python 3.11.4 is the
        # package floor (spec 2026-10-02 §4), so the filter is never skipped.
        tar.extractall(staging, filter="tar")


def _snapshot_local(source: Path, ref: str, staging: Path, env: dict) -> str:
    if not source.is_dir():
        raise ProjectError("template_not_git")
    top = _run(["rev-parse", "--show-toplevel"], cwd=source, env=env, code="template_not_git")
    if top.returncode == 0:
        # `rev-parse` walks up: a directory inside another repository is not a template (registry._git_present).
        if Path(top.stdout.strip()).resolve() != source.resolve():
            raise ProjectError("template_not_git")
    else:
        # A bare repository is a template only at its own top: `refs/`, `hooks/`, `objects/` inside one also answer
        # `--is-bare-repository` with true.
        bare = _run(["rev-parse", "--is-bare-repository"], cwd=source, env=env, code="template_not_git")
        git_dir = _run(["rev-parse", "--absolute-git-dir"], cwd=source, env=env, code="template_not_git")
        if (bare.stdout.strip() != "true" or git_dir.returncode != 0
                or Path(git_dir.stdout.strip()).resolve() != source.resolve()):
            raise ProjectError("template_not_git")
    resolved = _run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=source, env=env,
                    code="template_ref_not_found")
    if resolved.returncode != 0:
        raise ProjectError("template_ref_not_found")
    commit = resolved.stdout.strip()
    _extract(source, commit, staging, env)
    return commit


def _snapshot_remote(home: Path, plan: PortPlan, staging: Path, env: dict) -> str:
    """A shallow fetch of ``ref`` into ``LAB_HOME/tmp``, removed afterwards (§3.2)."""
    english = {**env, "LC_ALL": "C"}  # git's messages are matched below
    work = home / "tmp" / f"port-{secrets.token_hex(4)}"
    work.mkdir(mode=0o700, parents=True)
    try:
        mark_leftover(work)
        listed = _run([*_NO_CREDENTIAL_HELPER, "ls-remote", plan.template, plan.ref], cwd=work, env=english, code="template_fetch_failed")
        if listed.returncode != 0:
            _log(listed, "template_fetch_failed")
            raise ProjectError("template_not_git" if _NOT_A_REPOSITORY in listed.stderr else "template_fetch_failed")
        if not listed.stdout.strip() and not _FULL_HASH.match(plan.ref):
            raise ProjectError("template_ref_not_found")
        _ok(["init", "-q"], cwd=work, env=env, code="template_fetch_failed")
        fetched = _run([*_NO_CREDENTIAL_HELPER, "fetch", "-q", "--depth", "1", plan.template, plan.ref], cwd=work, env=english,
                       code="template_fetch_failed")
        if fetched.returncode != 0:
            _log(fetched, "template_fetch_failed")
            raise ProjectError("template_ref_not_found" if _NO_REMOTE_REF in fetched.stderr else "template_fetch_failed")
        commit = _ok(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=work, env=env, code="template_fetch_failed")
        _extract(work, commit, staging, env)
        return commit
    finally:
        _discard(work)


def _identity(directory: Path, env: dict) -> tuple[str, ...]:
    """The user's git identity when configured; otherwise Lab's, for this command only (§3.3)."""
    for key in ("user.name", "user.email"):
        found = _run(["config", "--get", key], cwd=directory, env=env, code="port_failed")
        if found.returncode != 0 or not found.stdout.strip():
            return _LAB_IDENTITY
    return ()


def _init_repository(staging: Path, plan: PortPlan, commit: str, env: dict) -> None:
    _ok(["init", "-q"], cwd=staging, env=env, code="port_failed")
    _ok(["add", "-A", "-f", "."], cwd=staging, env=env, code="port_failed")  # exactly the template's tracked files
    message = f"Lab: port of {plan.source_id} from {plan.template_display}@{commit}"
    _ok([*_identity(staging, env), "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "--allow-empty",
         "-m", message], cwd=staging, env=env, code="port_failed")
    # A lightweight tag even under a user's `tag.gpgSign=true`, which would sign it and open an editor.
    _ok(["-c", "tag.gpgSign=false", "tag", PORT_TAG], cwd=staging, env=env, code="port_failed")


def stage(home: Path, plan: PortPlan) -> Staged:
    """The port repository, complete, in a staging directory next to the destination (§3.2-§3.3)."""
    env = _environment()
    parent = plan.destination.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = parent / f".staging-{plan.destination.name}-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o700)
    try:
        mark_leftover(staging)
        if is_url(plan.template):
            commit = _snapshot_remote(home, plan, staging, env)
        else:
            commit = _snapshot_local(Path(plan.template), plan.ref, staging, env)
        _init_repository(staging, plan, commit, env)
    except BaseException:
        _discard(staging)
        raise
    return Staged(staging, commit)


def _discard(directory: Path) -> None:
    """Remove a marked temporary directory; its mark goes only with it, so a partial removal is retried at open."""
    shutil.rmtree(directory, ignore_errors=True)
    if not os.path.lexists(directory):
        unmark_leftover(directory)


def publish(projects, plan: PortPlan, staged: Staged):
    """§3.4, under the session's mutation lock: recheck, one rename, one registry transaction."""
    try:
        recheck(projects, plan)
    except BaseException:
        _discard(staged.directory)
        raise
    was_empty_directory = plan.destination.is_dir()
    try:
        os.rename(staged.directory, plan.destination)
    except OSError as error:
        _discard(staged.directory)
        if error.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR, errno.EISDIR):
            raise _taken(plan.destination, plan.default_destination) from None
        raise
    # After the rename: the mark names the staging, which no longer exists, so the published port never matches it;
    # a crash before this line leaves only an orphan mark, which the sweep removes.
    unmark_leftover(staged.directory)
    try:
        return projects.register_port(plan.project_id, source_id=plan.source_id, port_id=plan.port_id,
                                      place=str(plan.destination), template_source=plan.template_display,
                                      template_ref=plan.ref, template_commit=staged.commit)
    except BaseException:
        # Not committed: remove what this call created; a destination that was an empty directory stays one.
        shutil.rmtree(plan.destination, ignore_errors=True)
        if was_empty_directory:
            plan.destination.mkdir(mode=0o700, exist_ok=True)
        raise
