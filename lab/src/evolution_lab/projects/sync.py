"""Explicit sync of a Lab clone with its source (PDR-0024, spec 2026-10-08-lab-project-sync): ``fetch`` and
``inspect`` report what would come; ``integrate`` merges exactly the reported commit into ``lab/<id>``. Lab never
pushes and fetches only here. Pure functions over the clone directory; locks and the registry are the caller's."""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from .clone import _environment, _redact
from .errors import ProjectError
from .port import _LAB_IDENTITY, _NO_CREDENTIAL_HELPER

__all__ = ["fetch", "inspect", "integrate", "COMMITS_LIMIT", "FILES_LIMIT", "PATHS_LIMIT"]

COMMITS_LIMIT, FILES_LIMIT, PATHS_LIMIT = 200, 1000, 50
CHECK_SCHEMA = "lab-project-sync-check/1"
_TIMEOUT, _TAIL_LINES = 600, 20
_SHA = re.compile(r"[0-9a-f]{40}")
_LOG = logging.getLogger("evolution_lab.projects")


def _env() -> dict:
    env = {**_environment(), "LC_ALL": "C"}
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"):
        env.pop(name, None)  # the ``-c user.*`` Lab identity must hold for the merge commit
    return env


def _log(result: subprocess.CompletedProcess, code: str) -> None:
    tail = "\n".join((result.stderr or "").splitlines()[-_TAIL_LINES:])
    _LOG.warning("git exited %s; stderr tail:\n%s", result.returncode, _redact(tail), extra={"lab_code": code})


def _filter_settings(clone: Path) -> list[tuple[str, str]]:
    """The user's effective ``filter.*`` keys (global, system, includes, the clone's own), read under the normal
    environment. Filter drivers change how content is stored (Git LFS), not how a merge resolves, so the isolated
    merge must keep them or it writes raw blobs (LFS pointers) and refuses on a racy index."""
    found = _run(["config", "--includes", "-z", "--get-regexp", r"^filter\."], clone, "merge_failed")
    if found.returncode not in (0, 1):
        _log(found, "merge_failed")
        raise ProjectError("merge_failed")
    settings = []
    for entry in found.stdout.split("\0"):
        if entry:
            key, _, value = entry.partition("\n")
            settings.append((key, value if "\n" in entry else "true"))  # a key without a value is boolean true
    return settings


def _merge_env(clone: Path) -> dict:
    """The environment of a merge, its abort and the restore checks: no user or system git configuration, so a
    ``merge.default`` or a merge driver inherited from there cannot change the outcome (codex r1 #1) -- except the
    user's ``filter.*`` keys, passed as config parameters (git >= 2.31). A fetch keeps the user's config (ssh and
    credentials may live there); the clone's own config is checked by ``_custom_merge_config``."""
    env = {**_env(), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    settings = _filter_settings(clone)
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for n, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = key, value
    return env


def _run(argv: list[str], clone: Path, code: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *argv], cwd=clone, env=_env() if env is None else env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        _LOG.warning("git could not run: %s", type(error).__name__, extra={"lab_code": code})
        raise ProjectError(code) from None


def _ok(argv: list[str], clone: Path, code: str, *, strip: bool = True, env: dict | None = None) -> str:
    result = _run(argv, clone, code, env=env)
    if result.returncode != 0:
        _log(result, code)
        raise ProjectError(code)
    return result.stdout.strip() if strip else result.stdout  # ``-z`` output: a path may start with whitespace


def _commit(clone: Path, ref: str) -> str | None:
    result = _run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], clone, "revision_not_found")
    return result.stdout.strip() if result.returncode == 0 else None


def _is_ancestor(clone: Path, older: str, newer: str) -> bool:
    result = _run(["merge-base", "--is-ancestor", older, newer], clone, "merge_failed")
    if result.returncode not in (0, 1):
        _log(result, "merge_failed")
        raise ProjectError("merge_failed")
    return result.returncode == 0


def fetch(clone: Path) -> None:
    _ok([*_NO_CREDENTIAL_HELPER, "fetch", "--quiet", "--prune", "--tags", "--force", "origin"], clone, "sync_fetch_failed")


def _in_source(clone: Path, sha: str) -> bool:
    """Is ``sha`` reachable from a remote-tracking branch or a tag of the source (not only from the clone)?"""
    out = _ok(["for-each-ref", "--count=1", "--contains", sha, "refs/remotes/origin", "refs/tags"], clone,
              "revision_not_found")
    return bool(out)


_CHECKED = "refs/lab/sync-checked"  # Lab's own refs in the clone: one per shown upstream, ``<_CHECKED>/<sha>``


def _approved(clone: Path, sha: str) -> bool:
    """May ``sha`` be applied? It is in the source now, or it is (an ancestor of) a commit ``inspect`` showed: a source
    branch deleted after the check must not strand the commit the person approved (codex r1 #4). A sha that only
    the clone has (an assistant commit) is neither."""
    out = _ok(["for-each-ref", "--count=1", "--contains", sha, "refs/remotes/origin", "refs/tags", _CHECKED], clone,
              "upstream_changed")
    return bool(out)


def _upstream(clone: Path, rev: str | None) -> tuple[str, str | None]:
    if rev is None:
        head = _run(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], clone, "revision_not_found")
        name = head.stdout.strip() if head.returncode == 0 else ""
        sha = _commit(clone, name) if name else None
        if sha is None:
            raise ProjectError("revision_not_found", "The source has no default branch; pass --rev.")
        return sha, "origin/" + name.removeprefix("refs/remotes/origin/")
    for ref, label in ((f"refs/remotes/origin/{rev}", f"origin/{rev}"), (f"refs/tags/{rev}", f"tag:{rev}")):
        sha = _commit(clone, ref)
        if sha is not None:
            return sha, label
    sha = _commit(clone, rev)
    if sha is None:
        raise ProjectError("revision_not_found")
    return sha, None


def _dirty(clone: Path, *, env: dict | None = None) -> list[dict]:
    status = _run(["status", "--porcelain=v1", "-z", "--untracked-files=no"], clone, "working_copy_dirty", env=env)
    if status.returncode != 0:
        _log(status, "working_copy_dirty")
        raise ProjectError("working_copy_dirty")
    out = status.stdout  # not stripped: the first entry starts with the (possibly blank) index column
    paths, tokens = [], iter(out.split("\0"))
    for entry in tokens:
        if len(entry) <= 3:
            continue
        paths.append({"path": entry[3:]})
        if entry[0] in "RC" or entry[1] in "RC":
            next(tokens, None)  # the source path of a rename/copy follows as its own NUL field
    git_dir = Path(_ok(["rev-parse", "--absolute-git-dir"], clone, "working_copy_dirty", env=env))
    for marker, state in (("MERGE_HEAD", "merge_in_progress"), ("rebase-merge", "rebase_in_progress"),
                          ("rebase-apply", "rebase_in_progress")):
        if (git_dir / marker).exists():
            paths.append({"path": None, "state": state})
            break
    return paths[:PATHS_LIMIT]


def _commits(clone: Path, base: str, upstream: str) -> tuple[list[dict], bool]:
    out = _ok(["log", f"--max-count={COMMITS_LIMIT + 1}", "-z", "--format=%H%x1f%an%x1f%aI%x1f%s",
               f"{base}..{upstream}"], clone, "merge_failed", strip=False)
    rows = [dict(zip(("sha", "author", "date", "subject"), entry.split("\x1f"))) for entry in out.split("\0") if entry]
    return rows[:COMMITS_LIMIT], len(rows) > COMMITS_LIMIT


def _files(clone: Path, base: str, upstream: str) -> tuple[list[dict], bool]:
    out = _ok(["diff", "--name-status", "-M", "-z", base, upstream], clone, "merge_failed", strip=False).split("\0")
    rows, i = [], 0
    while i < len(out) and out[i]:
        status = out[i][0]
        if status in "RC":
            rows.append({"path": out[i + 2], "status": "R", "old_path": out[i + 1]}); i += 3
        else:
            rows.append({"path": out[i + 1], "status": status if status in "AMD" else "M"}); i += 2
    return rows[:FILES_LIMIT], len(rows) > FILES_LIMIT


def _mode(clone: Path, base: str, upstream: str) -> str:
    head = _ok(["rev-parse", "HEAD"], clone, "merge_failed")
    if upstream == base or _is_ancestor(clone, upstream, head):
        return "up_to_date"
    return "fast_forward" if _is_ancestor(clone, head, upstream) else "merge"


def inspect(clone: Path, *, project_id: str, base_revision: str, rev: str | None) -> dict:
    upstream, label = _upstream(clone, rev)
    if not _in_source(clone, upstream):
        raise ProjectError("revision_not_found", "The revision is not a commit of the source.")
    if not _is_ancestor(clone, base_revision, upstream):
        raise ProjectError("history_diverged")
    _ok(["update-ref", f"{_CHECKED}/{upstream}", upstream], clone, "sync_fetch_failed")  # what the person is shown
    commits, commits_truncated = _commits(clone, base_revision, upstream)
    files, files_truncated = _files(clone, base_revision, upstream)
    return {"schema": CHECK_SCHEMA, "project": project_id, "base_revision": base_revision,
            "upstream": {"sha": upstream, "ref": label}, "mode": _mode(clone, base_revision, upstream),
            "commits": commits, "commits_truncated": commits_truncated,
            "files": files, "files_truncated": files_truncated, "dirty": _dirty(clone)}


_DETAIL_BUDGET = 4000


def _listing(paths: list[str]) -> str:
    """Comma-joined paths within the ProjectError detail cap; the rest is counted, not listed."""
    shown, used = [], 0
    for path in paths:
        if used + len(path) + 2 > _DETAIL_BUDGET:
            break
        shown.append(path)
        used += len(path) + 2
    text = ", ".join(shown)
    return text if len(shown) == len(paths) else f"{text}, … (+{len(paths) - len(shown)} more)"


def _ignored_overwrites(clone: Path, upstream: str) -> list[str]:
    """Ignored local files (or directories) that merging ``upstream`` would overwrite; ort ignores
    ``--no-overwrite-ignore`` in a true merge, so Lab checks before running any merge."""
    ignored = [p for p in _ok(["ls-files", "-z", "--others", "--ignored", "--exclude-standard"], clone,
                              "merge_failed", strip=False).split("\0") if p]
    if not ignored:
        return []
    added = [p for p in _ok(["diff", "--name-only", "-z", "--no-renames", "--diff-filter=AR", "HEAD", upstream],
                            clone, "merge_failed", strip=False).split("\0") if p]
    files = set(ignored)
    dirs = {"/".join(p.split("/")[:i]) for p in ignored for i in range(1, p.count("/") + 1)}
    hits = []
    for path in added:
        parents = {"/".join(path.split("/")[:i]) for i in range(1, path.count("/") + 1)}
        if path in files or path in dirs or parents & files:
            hits.append(path)
    return hits


def _conflicts(clone: Path, result: subprocess.CompletedProcess, env: dict) -> list[str]:
    unmerged = _run(["diff", "--name-only", "--diff-filter=U", "-z"], clone, "merge_failed", env=env).stdout
    paths = [p for p in unmerged.split("\0") if p]
    if not paths and "would be overwritten" in (result.stderr or ""):
        paths = [line.strip() for line in result.stderr.splitlines() if line.startswith("\t")]
    return paths[:PATHS_LIMIT]


def _custom_merge_config(clone: Path, env: dict) -> bool:
    """Does the configuration a merge will read name a merge driver or a default one? Read under the merge's own
    isolated environment, with includes and without a scope flag: local, worktree and [include]/[includeIf] files all
    count (codex r2 #1). The clone is Lab's: such config means a person changed it, and a driver may keep local content
    as a 'successful' merge."""
    found = _run(["config", "--includes", "--get-regexp", r"^merge\.(default|.*\.driver)$"], clone, "merge_failed",
                 env=env)
    if found.returncode not in (0, 1):
        _log(found, "merge_failed")
        raise ProjectError("merge_failed")
    return found.returncode == 0


def _merge_attribute_in_info(clone: Path) -> bool:
    """Does ``$GIT_DIR/info/attributes`` set the ``merge`` attribute? ``core.attributesFile`` does not cover that file,
    and a ``merge=union`` there would resolve a conflict silently."""
    git_dir = Path(_ok(["rev-parse", "--absolute-git-dir"], clone, "merge_failed"))
    try:
        text = (git_dir / "info" / "attributes").read_text(errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        tokens = line.strip().split()
        if not tokens or tokens[0].startswith("#"):
            continue
        if any(t in ("merge", "-merge", "!merge") or t.startswith("merge=") for t in tokens[1:]):
            return True
    return False


def _recover(clone: Path, before: str, conflicts: list[str], env: dict) -> ProjectError:
    """The one way out of a merge that did not complete (also one that raised): abort what is in progress, verify the
    copy is as before, and say what happened. Never raises; a verification that itself fails is 'unfinished'."""
    try:
        git_dir = Path(_ok(["rev-parse", "--absolute-git-dir"], clone, "merge_failed", env=env))
        if (git_dir / "MERGE_HEAD").exists():
            _run(["merge", "--abort"], clone, "merge_failed", env=env)
        restored = _ok(["rev-parse", "HEAD"], clone, "merge_failed", env=env) == before and not _dirty(clone, env=env)
    except Exception:
        restored = False
    if not restored:
        return ProjectError("merge_failed", "The working copy is left in an unfinished state; a person must repair it.")
    return ProjectError("merge_conflict", _listing(conflicts)) if conflicts else ProjectError("merge_failed")


def integrate(clone: Path, *, branch: str, base_revision: str, upstream: str) -> dict:
    if not _SHA.fullmatch(upstream):
        raise ProjectError("invalid_request")
    if _ok(["branch", "--show-current"], clone, "branch_mismatch") != branch:
        raise ProjectError("branch_mismatch")
    if _commit(clone, upstream) is None or not _approved(clone, upstream):
        raise ProjectError("upstream_changed")
    if not _is_ancestor(clone, base_revision, upstream):
        raise ProjectError("history_diverged")
    dirty = _dirty(clone)
    if dirty:
        raise ProjectError("working_copy_dirty", _listing([d["path"] or d["state"] for d in dirty]))
    before = _ok(["rev-parse", "HEAD"], clone, "merge_failed")
    if _is_ancestor(clone, upstream, before):
        return {"mode": "up_to_date", "merged": upstream, "head": before}
    overwritten = _ignored_overwrites(clone, upstream)
    if overwritten:
        raise ProjectError("merge_conflict", _listing(overwritten))
    env = _merge_env(clone)
    if _custom_merge_config(clone, env):
        raise ProjectError("merge_failed", "custom merge driver configured in the working copy")
    if _merge_attribute_in_info(clone):
        raise ProjectError("merge_failed", "merge attribute configured in the working copy")
    fast = _is_ancestor(clone, before, upstream)
    argv = ["-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-c", "merge.ff=true",
            "-c", f"branch.{branch}.mergeOptions=", "-c", "rerere.enabled=false",
            *_LAB_IDENTITY, "merge", "--no-overwrite-ignore", "--no-verify", "--no-gpg-sign", "--no-verify-signatures", "--no-autostash",
            *(["--ff-only"] if fast else ["--no-ff", "--no-edit", "-m", f"lab: sync {upstream[:12]}"]), upstream]
    conflicts: list[str] = []
    try:
        result = _run(argv, clone, "merge_failed", env=env)
        if result.returncode != 0:
            _log(result, "merge_failed")
            conflicts = _conflicts(clone, result, env)
    except Exception:  # a timeout or a failing git mid-merge: the same recovery as a plain conflict
        raise _recover(clone, before, conflicts, env) from None
    if result.returncode != 0:
        raise _recover(clone, before, conflicts, env)
    try:  # git reported success: from here a failure is never a failed merge (codex r2 #2)
        head = _ok(["rev-parse", "HEAD"], clone, "merge_failed", env=env)
    except Exception:
        raise ProjectError("sync_not_recorded") from None
    return {"mode": "fast_forward" if fast else "merge", "merged": upstream, "head": head}
