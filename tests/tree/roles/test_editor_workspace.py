"""Contracts for the shared Candidate-editor workspace module."""

from __future__ import annotations

import errno
import json
import os
import random
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.workspace import (
    _REMOVAL_DESCRIPTORS,
    ActorViolation,
    Budgets,
    BudgetViolation,
    ControlCollision,
    FileState,
    WorkspaceChanged,
    _remove_contents,
    admit,
    bounded_note,
    capture_tree,
    changed_file_paths,
    discard,
    freeze_projection,
    matches_rule,
    open_attempt_log,
    project,
    publish,
    relative_candidate_path,
    relative_rule,
    self_check,
)


def _candidate(base: Path) -> Path:
    """A Candidate workspace with three files at three different modes."""

    workspace = base / "ws"
    (workspace / "dir").mkdir(parents=True)
    (workspace / "a.md").write_text("alpha", encoding="utf-8")
    os.chmod(workspace / "a.md", 0o644)
    (workspace / "dir" / "b.py").write_text("x = 1", encoding="utf-8")
    os.chmod(workspace / "dir" / "b.py", 0o755)
    (workspace / "cfg.yaml").write_text("k: v", encoding="utf-8")
    os.chmod(workspace / "cfg.yaml", 0o644)
    return workspace


def _budgets(**overrides: Any) -> Budgets:
    values: dict[str, Any] = {
        "max_changed_files": None,
        "max_written_bytes": 1 << 20,
        "max_payload_bytes": 1 << 16,
    }
    values.update(overrides)
    return Budgets(**values)


def test_project_normalizes_modes_and_keeps_two_baselines(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    assert projection.capture["a.md"].mode & 0o777 == 0o644
    assert projection.baseline["a.md"].mode & 0o777 == 0o600
    assert (projection.root / "a.md").stat().st_mode & 0o777 == 0o600
    assert projection.capture["dir"].mode & 0o777 == 0o755
    assert (projection.root / "dir").stat().st_mode & 0o777 == 0o755
    assert projection.changes() == ()


def test_actor_mode_change_is_a_change_and_admission_refuses_it(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    os.chmod(projection.root / "a.md", 0o644)
    assert [change.path for change in projection.changes()] == ["a.md"]
    with pytest.raises(ActorViolation, match="mode"):
        admit(projection, writable_paths=("**",), budgets=_budgets())


def test_control_prefix_is_excluded_and_collision_refused(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace, control_prefix=".evolution")
    (projection.root / ".evolution").mkdir()
    (projection.root / ".evolution" / "EDIT_REQUEST.json").write_text("{}", encoding="utf-8")
    projection.mark_control()
    assert projection.changes() == ()
    assert admit(projection, writable_paths=("**",), budgets=_budgets()) == ()

    (projection.root / ".evolution" / "EDIT_REQUEST.json").write_text('{"x":1}', encoding="utf-8")
    assert projection.changes() == ()
    with pytest.raises(ActorViolation, match="control plane"):
        admit(projection, writable_paths=("**",), budgets=_budgets())

    (workspace / ".evolution").mkdir()
    with pytest.raises(ControlCollision):
        project(workspace, control_prefix=".evolution")


def test_publish_stages_from_capture_plus_admitted_changes(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    previous_umask = os.umask(0o022)
    try:
        (projection.root / "a.md").write_text("beta", encoding="utf-8")
        (projection.root / "new.txt").write_text("n", encoding="utf-8")
    finally:
        os.umask(previous_umask)
    changes = admit(projection, writable_paths=("a.md", "new.txt"), budgets=_budgets())
    publish(changes, projection)
    assert (workspace / "a.md").read_text(encoding="utf-8") == "beta"
    assert (workspace / "a.md").stat().st_mode & 0o777 == 0o644
    assert (workspace / "dir" / "b.py").stat().st_mode & 0o777 == 0o755
    assert (workspace / "dir" / "b.py").read_text(encoding="utf-8") == "x = 1"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "n"
    # A created file keeps the mode the actor gave it in the projection.
    assert (workspace / "new.txt").stat().st_mode & 0o777 == 0o644


def test_created_file_with_an_executable_mode_is_refused(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "run.sh").write_text("echo hi", encoding="utf-8")
    os.chmod(projection.root / "run.sh", 0o755)
    with pytest.raises(ActorViolation, match="unsafe mode: run.sh"):
        admit(projection, writable_paths=("**",), budgets=_budgets())
    assert not (workspace / "run.sh").exists()


def test_writable_paths_use_the_artifact_policy_grammar(tmp_path: Path) -> None:
    """Operator patterns are the policy's, not the actor path grammar's."""

    projection = project(_candidate(tmp_path / "policy"))
    (projection.root / "skills").mkdir(mode=0o755)
    (projection.root / "skills" / "x.md").write_text("s", encoding="utf-8")
    os.chmod(projection.root / "skills" / "x.md", 0o600)
    (projection.root / ".github").mkdir(mode=0o755)
    (projection.root / ".github" / "y").write_text("g", encoding="utf-8")
    os.chmod(projection.root / ".github" / "y", 0o600)
    admitted = admit(projection, writable_paths=("skill**", ".github/**"), budgets=_budgets())
    assert sorted(change.path for change in admitted) == [".github/y", "skills/x.md"]

    outside = project(_candidate(tmp_path / "outside-policy"))
    (outside.root / "other").mkdir(mode=0o755)
    (outside.root / "other" / "z").write_text("z", encoding="utf-8")
    os.chmod(outside.root / "other" / "z", 0o600)
    with pytest.raises(ActorViolation, match="outside writable set: other/z"):
        admit(outside, writable_paths=("skill**", ".github/**"), budgets=_budgets())

    # gitignore-like: a pattern with no "/" matches a basename at any depth.
    unanchored = project(_candidate(tmp_path / "unanchored"))
    (unanchored.root / "skills").mkdir(mode=0o755)
    (unanchored.root / "skills" / "a.md").write_text("a", encoding="utf-8")
    os.chmod(unanchored.root / "skills" / "a.md", 0o600)
    admitted = admit(unanchored, writable_paths=("*.md",), budgets=_budgets())
    assert [change.path for change in admitted] == ["skills/a.md"]


def test_publish_deletes_and_creates_nested_directories(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "cfg.yaml").unlink()
    (projection.root / "sub").mkdir(mode=0o755)
    (projection.root / "sub" / "c.md").write_text("c", encoding="utf-8")
    os.chmod(projection.root / "sub" / "c.md", 0o600)
    changes = admit(projection, writable_paths=("**",), budgets=_budgets())
    publish(changes, projection)
    assert not (workspace / "cfg.yaml").exists()
    assert (workspace / "sub" / "c.md").read_text(encoding="utf-8") == "c"
    # A directory the publication has to invent takes the Candidate root's mode.
    assert (workspace / "sub").stat().st_mode & 0o777 == workspace.stat().st_mode & 0o777


def test_discard_removes_the_projection_and_leaves_the_candidate(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "a.md").write_text("beta", encoding="utf-8")
    discard(projection)
    assert not projection.root.exists()
    assert (workspace / "a.md").read_text(encoding="utf-8") == "alpha"
    discard(projection)


def test_publish_refuses_in_place_drift_and_writes_nothing(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "a.md").write_text("beta", encoding="utf-8")
    changes = admit(projection, writable_paths=("a.md",), budgets=_budgets())
    (workspace / "a.md").write_text("drifted", encoding="utf-8")
    with pytest.raises(WorkspaceChanged):
        publish(changes, projection)
    assert (workspace / "a.md").read_text(encoding="utf-8") == "drifted"


@pytest.mark.parametrize("drift", ["add", "delete", "symlink"])
def test_publish_refuses_added_deleted_or_linked_entries(tmp_path: Path, drift: str) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "a.md").write_text("beta", encoding="utf-8")
    changes = admit(projection, writable_paths=("a.md",), budgets=_budgets())
    if drift == "add":
        (workspace / "z").write_text("z", encoding="utf-8")
    elif drift == "delete":
        (workspace / "cfg.yaml").unlink()
    else:
        (workspace / "lnk").symlink_to("/etc/passwd")
    with pytest.raises(WorkspaceChanged):
        publish(changes, projection)
    assert (workspace / "a.md").read_text(encoding="utf-8") == "alpha"
    assert (workspace / "dir" / "b.py").read_text(encoding="utf-8") == "x = 1"
    if drift == "symlink":
        assert (workspace / "lnk").is_symlink()
        assert os.readlink(workspace / "lnk") == "/etc/passwd"


def test_publish_refuses_root_replacement_before_first_write(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "a.md").write_text("beta", encoding="utf-8")
    changes = admit(projection, writable_paths=("a.md",), budgets=_budgets())
    workspace.rename(tmp_path / "ws2")
    workspace.mkdir()
    (workspace / "marker.txt").write_text("marker", encoding="utf-8")
    with pytest.raises(WorkspaceChanged):
        publish(changes, projection)
    assert (workspace / "marker.txt").read_text(encoding="utf-8") == "marker"
    assert sorted(item.name for item in workspace.iterdir()) == ["marker.txt"]
    assert (tmp_path / "ws2" / "a.md").read_text(encoding="utf-8") == "alpha"


def test_publish_restores_backup_when_rename_in_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "a.md").write_text("beta", encoding="utf-8")
    changes = admit(projection, writable_paths=("a.md",), budgets=_budgets())

    real_rename = os.rename
    calls = {"count": 0}

    def failing_rename(source: Any, destination: Any, **kwargs: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("rename refused by the test")
        real_rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(OSError, match="rename refused by the test"):
        publish(changes, projection)
    monkeypatch.undo()

    assert calls["count"] == 3
    assert (workspace / "a.md").read_text(encoding="utf-8") == "alpha"
    assert (workspace / "a.md").stat().st_mode & 0o777 == 0o644
    assert (workspace / "dir" / "b.py").read_text(encoding="utf-8") == "x = 1"
    assert (workspace / "cfg.yaml").read_text(encoding="utf-8") == "k: v"


def test_publish_restores_the_backup_when_the_root_cannot_be_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `os.rename(root, backup)` the Candidate lives only in the backup:
    a `mkdir` that fails there must put it back, not leave the pathname empty.
    """

    workspace = _candidate(tmp_path)
    projection = project(workspace)
    (projection.root / "a.md").write_text("beta", encoding="utf-8")
    changes = admit(projection, writable_paths=("a.md",), budgets=_budgets())
    before = {
        entry.relative_to(workspace).as_posix(): (
            entry.stat().st_mode & 0o777,
            entry.read_bytes() if entry.is_file() else None,
        )
        for entry in sorted(workspace.rglob("*"))
    }

    real_mkdir = Path.mkdir
    fired = {"count": 0}

    def failing_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == workspace and not fired["count"]:
            fired["count"] += 1
            raise OSError("mkdir refused by the test")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    with pytest.raises(WorkspaceChanged):
        publish(changes, projection)
    monkeypatch.undo()

    assert fired["count"] == 1
    assert workspace.is_dir()
    after = {
        entry.relative_to(workspace).as_posix(): (
            entry.stat().st_mode & 0o777,
            entry.read_bytes() if entry.is_file() else None,
        )
        for entry in sorted(workspace.rglob("*"))
    }
    assert after == before
    assert [item.name for item in tmp_path.iterdir() if "evo-editor-backup" in item.name] == []


def test_admit_rules(tmp_path: Path) -> None:
    outside = project(_candidate(tmp_path / "outside"))
    (outside.root / "other.txt").write_text("o", encoding="utf-8")
    with pytest.raises(ActorViolation, match="outside writable set: other.txt"):
        admit(outside, writable_paths=("a.md",), budgets=_budgets())

    unsafe = project(_candidate(tmp_path / "unsafe"))
    (unsafe.root / "run.sh").write_text("echo", encoding="utf-8")
    os.chmod(unsafe.root / "run.sh", 0o755)
    with pytest.raises(ActorViolation, match="unsafe mode: run.sh"):
        admit(unsafe, writable_paths=("**",), budgets=_budgets())

    payload = project(_candidate(tmp_path / "payload"))
    (payload.root / "a.md").write_text("x" * 200, encoding="utf-8")
    with pytest.raises(ActorViolation, match="payload limit exceeded: a.md"):
        admit(payload, writable_paths=("**",), budgets=_budgets(max_payload_bytes=100))

    cumulative = project(_candidate(tmp_path / "cumulative"))
    (cumulative.root / "a.md").write_text("x" * 100, encoding="utf-8")
    (cumulative.root / "cfg.yaml").write_text("y" * 100, encoding="utf-8")
    with pytest.raises(ActorViolation, match="written-byte budget exceeded"):
        admit(cumulative, writable_paths=("**",), budgets=_budgets(max_written_bytes=150))

    counted = project(_candidate(tmp_path / "counted"))
    (counted.root / "a.md").write_text("beta", encoding="utf-8")
    (counted.root / "cfg.yaml").write_text("k: w", encoding="utf-8")
    with pytest.raises(ActorViolation, match="changed-file budget exceeded"):
        admit(counted, writable_paths=("**",), budgets=_budgets(max_changed_files=1))

    flipped = project(_candidate(tmp_path / "flipped"))
    (flipped.root / "a.md").unlink()
    (flipped.root / "a.md").mkdir(mode=0o755)
    with pytest.raises(ActorViolation, match="entry kind changed: a.md"):
        admit(flipped, writable_paths=("**",), budgets=_budgets())

    unused = project(_candidate(tmp_path / "unused"))
    (unused.root / "empty").mkdir(mode=0o755)
    with pytest.raises(ActorViolation, match="unused directory: empty"):
        admit(unused, writable_paths=("**",), budgets=_budgets())

    binary = project(_candidate(tmp_path / "binary"))
    (binary.root / "a.md").write_bytes(b"\xff\xfe")
    with pytest.raises(ActorViolation, match="not UTF-8: a.md"):
        admit(binary, writable_paths=("**",), budgets=_budgets())


def test_admit_reports_an_actor_symlink_as_actor_violation(tmp_path: Path) -> None:
    """The projection is the actor's output, so its refusals are its class."""

    linked = project(_candidate(tmp_path / "linked"))
    (linked.root / "lnk.md").symlink_to(linked.root / "a.md")
    with pytest.raises(ActorViolation, match="unsupported entry"):
        admit(linked, writable_paths=("**",), budgets=_budgets())

    piped = project(_candidate(tmp_path / "piped"))
    os.mkfifo(piped.root / "pipe")
    with pytest.raises(ActorViolation, match="unsupported entry"):
        admit(piped, writable_paths=("**",), budgets=_budgets())


def test_capture_tree_refuses_symlink_and_fifo(tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "lnk").symlink_to(tmp_path / "absent")
    with pytest.raises(WorkspaceError):
        capture_tree(linked)

    piped = tmp_path / "piped"
    piped.mkdir()
    os.mkfifo(piped / "pipe")
    with pytest.raises(WorkspaceError):
        capture_tree(piped)


def test_matches_rule_and_relative_rule() -> None:
    assert matches_rule("skills/a/b.md", "skills/**")
    assert matches_rule("x.md", "*.md")
    assert not matches_rule("d/x.md", "*.md")
    assert relative_rule("skills/**") == "skills/**"
    for rule in ("/abs", "../x", "./x", "a/**b", ""):
        with pytest.raises(ValueError):
            relative_rule(rule)


def test_relative_candidate_path_admits_dot_prefixed_names_and_refuses_escapes() -> None:
    """A Candidate path is not a policy rule: `.github` is a file, not a traversal."""

    for value in (".github/x", "a/.hidden", "dir/sub.md", ".github/workflows/ci.yml"):
        assert relative_candidate_path(value) == value
    for value in ("/abs", "a/../b", "./a", "a//b", "a\0b", "..", "", "a/"):
        with pytest.raises(ValueError):
            relative_candidate_path(value)
    # The rule grammar refuses exactly the dot-prefixed names this admits.
    with pytest.raises(ValueError):
        relative_rule(".github/x")


def test_self_check_reports_yaml_and_python_errors(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "bad.yaml").write_text("k: [", encoding="utf-8")
    (root / "bad.py").write_text("def (", encoding="utf-8")
    (root / "ok.yml").write_text("k: v", encoding="utf-8")
    (root / "ok.py").write_text("x = 1", encoding="utf-8")
    issues = self_check(root, ("bad.yaml", "bad.py", "ok.yml", "ok.py"))
    assert len(issues) == 2
    assert any(issue.startswith("bad.yaml: ") for issue in issues)
    assert any(issue.startswith("bad.py: ") for issue in issues)
    assert self_check(root, ("ok.yml", "ok.py")) == ()
    assert self_check(root, ("bad.yaml.txt",)) == ()


def test_attempt_log_is_private_redacted_and_never_follows_links(tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    with open_attempt_log(log_root, "attempt-1", secrets=("sekret-value",)) as log:
        log.record({"note": "carries sekret-value inline"})
    assert log_root.stat().st_mode & 0o777 == 0o700
    path = log_root / "attempt-1.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text(encoding="utf-8")
    assert "sekret-value" not in text
    assert json.loads(text.strip()) == {"note": "carries [REDACTED] inline"}

    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("", encoding="utf-8")
    linked_root = tmp_path / "linked-logs"
    linked_root.mkdir(mode=0o700)
    (linked_root / "attempt-2.jsonl").symlink_to(elsewhere)
    with pytest.raises(OSError):
        open_attempt_log(linked_root, "attempt-2", secrets=())
    assert elsewhere.read_text(encoding="utf-8") == ""


def test_attempt_log_refuses_a_fifo_at_the_attempt_path_without_blocking(
    tmp_path: Path,
) -> None:
    """A FIFO is refused, and refused *promptly*.

    The pathname is predictable and `editor-logs` may already have entries on
    resume, so a FIFO there is reachable. Without `O_NONBLOCK` the open waits
    for a reader forever, on the event-loop thread, before the editor's
    deadline can fire; the elapsed-time bound is the whole point of the test.
    """

    log_root = tmp_path / "logs"
    log_root.mkdir(mode=0o700)
    os.mkfifo(log_root / "attempt-1.jsonl")

    started = time.monotonic()
    with pytest.raises(WorkspaceError) as refusal:
        open_attempt_log(log_root, "attempt-1", secrets=())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the open must not wait for a reader"
    # The basename only: an absolute path is what the contract keeps out of a refusal.
    assert str(refusal.value) == "attempt log is not a private regular file: attempt-1.jsonl"


def test_attempt_log_refuses_a_hard_link_out_of_the_log_root(tmp_path: Path) -> None:
    """`O_NOFOLLOW` sees no symlink here: the second name is the same inode."""

    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("untouched\n", encoding="utf-8")
    os.chmod(elsewhere, 0o644)
    log_root = tmp_path / "logs"
    log_root.mkdir(mode=0o700)
    os.link(elsewhere, log_root / "attempt-3.jsonl")

    with pytest.raises(WorkspaceError) as refusal:
        open_attempt_log(log_root, "attempt-3", secrets=())

    assert str(refusal.value) == "attempt log is not a private regular file: attempt-3.jsonl"
    assert elsewhere.read_text(encoding="utf-8") == "untouched\n"
    # Not even the `fchmod` reached it: the refusal precedes every write.
    assert elsewhere.stat().st_mode & 0o777 == 0o644


def test_attempt_log_appends_to_an_existing_private_regular_file(tmp_path: Path) -> None:
    """The admissible pre-existing entry: a plain, owner-owned, single-link file."""

    log_root = tmp_path / "logs"
    log_root.mkdir(mode=0o700)
    path = log_root / "attempt-4.jsonl"
    path.write_text('{"note": "earlier"}\n', encoding="utf-8")
    os.chmod(path, 0o600)

    with open_attempt_log(log_root, "attempt-4", secrets=()) as log:
        log.record({"note": "later"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"note": "earlier"}, {"note": "later"}]


def test_bounded_note_cuts_on_a_character_boundary() -> None:
    note = bounded_note("я" * 5000)
    assert len(note.encode("utf-8")) <= 4096
    assert note == "я" * 2048
    assert bounded_note("short") == "short"


def test_project_honours_tmp_root(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    holding = tmp_path / "holding"
    holding.mkdir()

    projection = project(workspace, tmp_root=holding)

    assert projection.root.is_relative_to(holding)
    discard(projection)


def test_project_without_visible_materializes_the_whole_capture(tmp_path: Path) -> None:
    """Pin (H11): `visible=None` writes every captured entry, as before role visibility.

    Families up to 13 call `project` without `visible`; their projection must
    stay byte- and mode-identical: every directory -- an empty one included --
    at its captured mode, every regular file with its bytes at `0600`.
    """

    workspace = _candidate(tmp_path)
    (workspace / "empty").mkdir()
    os.chmod(workspace / "empty", 0o711)
    os.chmod(workspace / "dir", 0o750)
    root_mode = capture_tree(workspace)["."].mode
    expected = {
        ".": FileState(root_mode, b"", "dir"),
        "a.md": FileState(0o600, b"alpha", "file"),
        "cfg.yaml": FileState(0o600, b"k: v", "file"),
        "dir": FileState(0o750, b"", "dir"),
        "dir/b.py": FileState(0o600, b"x = 1", "file"),
        "empty": FileState(0o711, b"", "dir"),
    }
    for projection in (project(workspace), project(workspace, visible=None)):
        try:
            assert capture_tree(projection.root) == expected
            assert projection.baseline == expected
            assert projection.capture == capture_tree(workspace)
        finally:
            discard(projection)


def test_freeze_projection_makes_everything_read_only(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)

    freeze_projection(projection)

    assert projection.frozen is True
    assert (projection.root / "a.md").stat().st_mode & 0o777 == 0o400
    assert (projection.root / "dir" / "b.py").stat().st_mode & 0o777 == 0o400
    assert (projection.root / "dir").stat().st_mode & 0o777 == 0o500
    assert projection.root.stat().st_mode & 0o777 == 0o500
    with pytest.raises(PermissionError):
        (projection.root / "a.md").write_text("nope", encoding="utf-8")
    with pytest.raises(PermissionError):
        (projection.root / "new.txt").write_text("nope", encoding="utf-8")


def test_freeze_projection_leaves_the_control_prefix_writable(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace, control_prefix=".evolution")
    (projection.root / ".evolution").mkdir()
    (projection.root / ".evolution" / "REFLECT_REQUEST.json").write_text("{}", encoding="utf-8")
    projection.mark_control()

    freeze_projection(projection)

    assert (projection.root / "a.md").stat().st_mode & 0o777 == 0o400
    # The control subtree keeps the mode `_materialize`/the editor gave it.
    control_dir = projection.root / ".evolution"
    assert control_dir.stat().st_mode & 0o777 not in (0o400, 0o500)
    (control_dir / "REFLECTION.json").write_text("{}", encoding="utf-8")
    assert (control_dir / "REFLECTION.json").read_text(encoding="utf-8") == "{}"


def test_freeze_projection_creates_the_control_prefix_under_a_read_only_parent(
    tmp_path: Path,
) -> None:
    """A published Parent is read-only, so only the freeze can still create it.

    `project` copies the capture's own modes, so the projection of a `0o500`
    Candidate root cannot be written by anyone afterwards -- including the
    `agent_cli` Reflector, whose CLI must write `REFLECTION.json` into
    `.evolution/`. The freeze creates that one directory before it locks the
    tree down.
    """

    workspace = _candidate(tmp_path)
    workspace.chmod(0o500)
    try:
        projection = project(workspace, control_prefix=".evolution")
    finally:
        workspace.chmod(0o700)

    freeze_projection(projection)

    control = projection.root / ".evolution"
    assert control.is_dir()
    (control / "REFLECTION.json").write_text("{}", encoding="utf-8")
    assert projection.root.stat().st_mode & 0o777 == 0o500
    assert (projection.root / "a.md").stat().st_mode & 0o777 == 0o400
    # The control plane is the actor's own: it is outside the compared baseline.
    assert ".evolution" not in projection.baseline
    discard(projection)


def test_discard_restores_write_bits_on_a_frozen_projection(tmp_path: Path) -> None:
    workspace = _candidate(tmp_path)
    projection = project(workspace)
    freeze_projection(projection)

    discard(projection)

    assert not projection.root.exists()
    assert (workspace / "a.md").read_text(encoding="utf-8") == "alpha"
    discard(projection)  # idempotent, as for an unfrozen projection


#: Deeper than the default recursion limit on every supported Python.
DEEP = 1_400


@pytest.mark.parametrize("frozen", [False, True], ids=["unfrozen", "frozen"])
def test_discard_removes_a_tree_deeper_than_the_recursion_limit(
    tmp_path: Path, frozen: bool
) -> None:
    """An actor may nest its projection arbitrarily deep; `discard` still removes it.

    Python 3.11's `os.walk` and `shutil.rmtree` recurse once per level, so a
    tree deeper than the recursion limit made the cleanup raise an
    unclassified `RecursionError` and undid every caller's classification of
    the deep tree (H11 review A1). The removal must also never follow a link
    out of the projection: a symlink at depth to a read-only directory and to
    a read-only file outside keeps both, modes included. A FIFO and a
    read-only directory at depth are removed too.
    """

    workspace = _candidate(tmp_path)
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    kept = outside / "inner" / "kept.txt"
    kept.write_text("kept", encoding="utf-8")
    os.chmod(kept, 0o444)
    os.chmod(outside / "inner", 0o555)
    os.chmod(outside, 0o555)
    projection = project(workspace)
    if frozen:
        freeze_projection(projection)
    top = projection.root / "dir"
    os.chmod(top, 0o700)
    deep = top
    for _ in range(DEEP):  # `mkdir(parents=True)` recurses too
        deep = deep / "d"
        deep.mkdir()
    (deep / "bottom.md").write_text("bottom", encoding="utf-8")
    os.chmod(deep / "bottom.md", 0o400)
    os.symlink(outside, deep / "to-outside-dir")
    os.symlink(kept, deep / "to-outside-file")
    os.mkfifo(deep / "pipe")
    (deep / "locked").mkdir()
    (deep / "locked" / "f.md").write_text("x", encoding="utf-8")
    os.chmod(deep / "locked", 0o500)
    os.chmod(top.joinpath(*["d"] * (DEEP // 2)), 0o500)
    if frozen:
        os.chmod(top, 0o500)
    temporary = Path(projection._tmp.name)

    try:
        discard(projection)

        assert not temporary.exists()
        assert (workspace / "a.md").read_text(encoding="utf-8") == "alpha"
        assert kept.read_text(encoding="utf-8") == "kept"
        assert [path.stat().st_mode & 0o777 for path in (outside, outside / "inner", kept)] == [
            0o555,
            0o555,
            0o444,
        ]
        discard(projection)  # idempotent
    finally:
        os.chmod(outside, 0o755)
        os.chmod(outside / "inner", 0o755)


#: Run in a child process: it lowers its own descriptor limit far below the depth.
_DESCRIPTOR_LIMITED_DISCARD = r"""
import os, resource, sys
from pathlib import Path
from evolution.tree.roles.workspace import discard, freeze_projection, project

workspace, depth, limit, frozen = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
outside = Path(sys.argv[5])
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
projection = project(workspace)
if frozen == "frozen":
    freeze_projection(projection)
top = projection.root / "dir"
os.chmod(top, 0o700)
deep = top
for level in range(depth):
    deep = deep / "d"
    deep.mkdir()
    if level % 100 == 0:
        os.symlink(outside, deep / "to-outside")
        (deep / "f.md").write_text("x")
        os.mkfifo(deep / "pipe")
os.chmod(top.joinpath(*["d"] * (depth // 2)), 0o500)
os.chmod(top, 0o500)
temporary = Path(projection._tmp.name)
discard(projection)
assert not temporary.exists(), "the deep tree was left behind"
assert sorted(os.listdir(outside)) == ["kept.txt"]
"""


@pytest.mark.parametrize("frozen", ["unfrozen", "frozen"])
def test_discard_removes_a_deep_tree_under_a_low_descriptor_limit(
    tmp_path: Path, frozen: str
) -> None:
    """The removal holds a bounded number of descriptors, not one per level.

    A default soft limit of 1,024 open files is common, and a Reflector or
    Editor may nest far deeper than that; the child process lowers its own
    limit to 256 and the 1,400-level tree must still go entirely, without
    following the links planted along the way.
    """

    workspace = _candidate(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("kept", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _DESCRIPTOR_LIMITED_DISCARD,
            str(workspace),
            str(DEEP),
            "256",
            frozen,
            str(outside),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert (outside / "kept.txt").read_text(encoding="utf-8") == "kept"


def _nest(top: Path, depth: int) -> None:
    """`depth` directories `d` below `top`, made by descriptor so a path past `PATH_MAX` works."""

    flags = os.O_RDONLY | os.O_DIRECTORY
    descriptor = os.open(top, flags)
    try:
        for _ in range(depth):
            os.mkdir("d", 0o755, dir_fd=descriptor)
            child = os.open("d", flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


#: Isolate the process-global counters from other tests' threads and finalizers.
_MEASURE_REMOVAL = r"""
import os, sys, time
from pathlib import Path
from evolution.tree.roles.workspace import _REMOVAL_DESCRIPTORS, _remove_contents

root, depth = Path(sys.argv[1]), int(sys.argv[2])
real_open, real_close = os.open, os.close
held = set()
counts = {"open": 0, "peak": 0}

def counting_open(path, flags, mode=0o777, *, dir_fd=None):
    descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
    counts["open"] += 1
    held.add(descriptor)
    counts["peak"] = max(counts["peak"], len(held))
    return descriptor

def counting_close(descriptor):
    held.discard(descriptor)
    real_close(descriptor)

os.open, os.close = counting_open, counting_close
started = time.monotonic()
try:
    _remove_contents(root)
finally:
    os.open, os.close = real_open, real_close
elapsed = time.monotonic() - started

assert list(root.iterdir()) == []
assert counts["open"] <= 3 * depth, counts
assert counts["peak"] <= _REMOVAL_DESCRIPTORS + 1, counts
assert not held, held  # every descriptor closed
assert elapsed < 30.0, elapsed  # generous: linear work, far from the cubic minutes
"""


def test_removal_cost_is_linear_and_its_descriptors_bounded(tmp_path: Path) -> None:
    """Each level costs a bounded number of opens, and at most the bound is held at once.

    Re-entering an evicted level by name from the
    root, and scanning released frames on every eviction, made the removal
    cubic in depth, so an actor nesting tens of thousands of levels stalled
    the campaign's event loop inside `discard`. Counting `os.open` calls is
    deterministic where a time bound is not. Run the counters in a fresh
    child: unrelated temporary-directory finalizers in a pytest worker can
    otherwise add their own opens to the process-global measurement.
    """

    root = tmp_path / "tree"
    root.mkdir()
    depth = 5_000
    _nest(root, depth)
    completed = subprocess.run(
        [sys.executable, "-c", _MEASURE_REMOVAL, str(root), str(depth)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("attack", ["strip_search", "move_level"])
def test_a_racing_actor_cannot_make_removal_quadratic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    """An actor that wins every race on `..` costs a bounded number of extra opens.

    a process the role left behind can refuse
    each climb through `..` with one syscall -- strip the search bit of the
    level about to be climbed from, or move that level under another parent
    -- and every refusal used to cost a reopen by name from the root, which
    made the removal quadratic. The hook here attacks right before every
    `..` open, after the walker's own restore, so the actor always wins. The
    by-name reopens are budgeted; past the budget a refused `..` leaves its
    branch for `TemporaryDirectory.cleanup()`.
    """

    if attack == "move_level" and not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc/self/fd to name the level being climbed from")
    root = tmp_path / "tree"
    root.mkdir()
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    depth = 1_500
    _nest(root, depth)
    real_open = os.open
    counts = {"open": 0, "attacks": 0}

    def attacking_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: Any = None) -> int:
        counts["open"] += 1
        if path == ".." and dir_fd is not None:
            counts["attacks"] += 1
            if attack == "strip_search":
                os.fchmod(dir_fd, 0o600)
            else:
                here = os.readlink(f"/proc/self/fd/{dir_fd}")
                os.rename(here, stranger / str(counts["attacks"]))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", attacking_open)
    try:
        _remove_contents(root)
    finally:
        monkeypatch.undo()

    assert counts["attacks"] > 0
    assert counts["open"] <= 4 * depth
    _remove_contents(root)  # unattacked, whatever was left goes too
    assert list(root.iterdir()) == []
    # The actor moved levels -- with whatever was dropped below them -- out of
    # the tree; that is no longer the removal's, but the test cleans it up.
    _remove_contents(stranger)


def test_a_stripped_search_bit_does_not_refuse_the_climb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walker restores its own level's bits before it opens `..` from it.

    An actor that strips the search bit of each level as the walk empties it
    (between the walk's passes, not inside them) would otherwise refuse every
    climb through `..`, spend the reopen budget and leave the tree behind.
    """

    root = tmp_path / "tree"
    root.mkdir()
    depth = 1_500
    _nest(root, depth)
    real_rmdir, real_open = os.rmdir, os.open
    counts = {"open": 0}

    def stripping_rmdir(path: Any, *, dir_fd: Any = None) -> None:
        real_rmdir(path, dir_fd=dir_fd)
        if dir_fd is not None:
            os.fchmod(dir_fd, 0o600)

    def counting_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: Any = None) -> int:
        counts["open"] += 1
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", stripping_rmdir)
    monkeypatch.setattr(os, "open", counting_open)
    try:
        _remove_contents(root)
    finally:
        monkeypatch.undo()

    assert list(root.iterdir()) == []
    assert counts["open"] <= 2 * depth + 2


@pytest.mark.parametrize("impostor", ["directory", "symlink"])
def test_removal_follows_a_renamed_level_never_the_impostor_at_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, impostor: str
) -> None:
    """Past the descriptor bound an ancestor is closed and later re-entered.

    It is re-entered through `..` from the level below, which is never a
    link, and must have the identity it had -- so the walk keeps removing the
    directory it listed, as a held descriptor would, even after that one is
    renamed away. Whatever replaces it at its old name (another directory or
    a link to an outside one) is never emptied or followed.
    """

    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("kept", encoding="utf-8")
    parent = root / "a"
    # Two deep branches: whichever the walk takes first, the other is still
    # pending in the evicted `swap` level when the walk comes back to it.
    for branch in ("d", "e"):
        (parent / "swap" / branch).mkdir(parents=True)
        _nest(parent / "swap" / branch, 2 * _REMOVAL_DESCRIPTORS)
    real_rmdir = os.rmdir

    def swapping_rmdir(path: Any, *, dir_fd: Any = None) -> None:
        # The first removal is the deepest level: every shallow one is evicted.
        if not (parent / "moved").exists():
            os.rename(parent / "swap", parent / "moved")
            if impostor == "directory":
                for branch in ("d", "e"):
                    (parent / "swap" / branch).mkdir(parents=True)
                    (parent / "swap" / branch / "decoy.txt").write_text("decoy", encoding="utf-8")
            else:
                (parent / "swap").symlink_to(outside)
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", swapping_rmdir)
    try:
        _remove_contents(root)
    finally:
        monkeypatch.undo()

    assert list((parent / "moved").iterdir()) == []  # the listed directory, emptied
    if impostor == "directory":
        for branch in ("d", "e"):
            assert (parent / "swap" / branch / "decoy.txt").read_text(encoding="utf-8") == "decoy"
    else:
        assert (parent / "swap").is_symlink()
    assert sorted(os.listdir(outside)) == ["kept.txt"]


def test_removal_never_takes_a_moved_levels_new_parent_for_its_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A level moved under another directory: its `..` is no longer the level above it.

    The identity check refuses that `..`; the walk re-enters the real parent
    by name from the root instead (each level verified) and finishes it, and
    the stranger directory's entries -- one shares the name of the parent's
    pending branch -- are never touched.
    """

    root = tmp_path / "tree"
    stranger = tmp_path / "stranger"
    parent = root / "a" / "swap"
    for branch in ("d", "e"):
        (parent / branch).mkdir(parents=True)
        _nest(parent / branch, 2 * _REMOVAL_DESCRIPTORS)
    for branch in ("d", "e"):
        os.chmod(parent / branch, 0o755)
    real_rmdir = os.rmdir
    moved: list[str] = []

    def moving_rmdir(path: Any, *, dir_fd: Any = None) -> None:
        if not moved:
            # The branch being walked is the one its listing restored to 0o700.
            walked = next(
                branch for branch in ("d", "e") if (parent / branch).stat().st_mode & 0o777 == 0o700
            )
            other = "e" if walked == "d" else "d"
            (stranger / other).mkdir(parents=True)
            (stranger / other / "decoy.txt").write_text("decoy", encoding="utf-8")
            os.rename(parent / walked, stranger / walked)
            moved.append(other)
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", moving_rmdir)
    try:
        _remove_contents(root)
    finally:
        monkeypatch.undo()

    (other,) = moved
    assert (stranger / other / "decoy.txt").read_text(encoding="utf-8") == "decoy"
    assert list(root.iterdir()) == []  # the real parent and its pending branch went


def _refusing_nofollow_chmod(real: Any) -> Any:
    """`os.chmod` as on glibc < 2.32: every no-follow chmod with `dir_fd` is unsupported.

    CPython then raises `ValueError` ("cannot use dir_fd and follow_symlinks
    together"), not `NotImplementedError`.
    """

    def chmod(path: Any, mode: int, *, dir_fd: Any = None, follow_symlinks: bool = True) -> None:
        if dir_fd is not None and not follow_symlinks:
            raise ValueError("chmod: cannot use dir_fd and follow_symlinks together")
        real(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    return chmod


@pytest.mark.parametrize("case", ["link_swapped_in", "unsupported_nofollow_chmod"])
def test_discard_survives_a_refused_nofollow_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A directory that cannot be opened is chmodded by name, no-follow, and may not crash.

    `link_swapped_in`: the directory is replaced by a symlink to an outside
    directory between the listing and a refused open; the no-follow chmod of a
    link is unsupported (`ValueError` with `dir_fd` on Linux). The link is
    unlinked and never followed. `unsupported_nofollow_chmod`: an unreadable
    directory on a C library without no-follow chmod; it is left to
    `TemporaryDirectory.cleanup()`, which resets permissions and removes it.
    """

    workspace = _candidate(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("kept", encoding="utf-8")
    os.chmod(outside, 0o555)
    projection = project(workspace)
    victim = projection.root / "victim"
    victim.mkdir()
    (victim / "f.md").write_text("x", encoding="utf-8")
    temporary = Path(projection._tmp.name)
    real_open = os.open
    swapped: list[str] = []

    def refusing_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: Any = None) -> int:
        if path == "victim" and dir_fd is not None and not swapped:
            swapped.append(path)
            (victim / "f.md").unlink()
            victim.rmdir()
            victim.symlink_to(outside)
            raise PermissionError(errno.EACCES, "refused", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    if case == "link_swapped_in":
        monkeypatch.setattr(os, "open", refusing_open)
    else:
        os.chmod(victim, 0o000)
        monkeypatch.setattr(os, "chmod", _refusing_nofollow_chmod(os.chmod))

    try:
        discard(projection)
    finally:
        monkeypatch.undo()
        os.chmod(outside, 0o755)

    assert swapped == (["victim"] if case == "link_swapped_in" else [])
    assert not temporary.exists()
    assert sorted(os.listdir(outside)) == ["kept.txt"]
    assert (outside / "kept.txt").read_text(encoding="utf-8") == "kept"


def test_discard_leaves_a_deep_subtree_it_cannot_open_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch the removal cannot enter is left to `cleanup()`, which may not raise either.

    Such a leftover can be deep (an unreadable directory on a C library
    without no-follow chmod, or a branch whose identity changed); Python
    3.11's recursive `rmtree` then raises `RecursionError` rather than the
    `OSError` `discard` has always suppressed.
    """

    workspace = _candidate(tmp_path)
    projection = project(workspace)
    deep = projection.root / "locked"
    deep.mkdir()
    for _ in range(DEEP):
        deep = deep / "d"
        deep.mkdir()
    temporary = Path(projection._tmp.name)
    real_open = os.open

    def refusing_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: Any = None) -> int:
        if path == "locked" and dir_fd is not None:
            raise PermissionError(errno.EACCES, "refused", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", refusing_open)
    try:
        discard(projection)
    finally:
        monkeypatch.undo()

    # Python 3.14's iterative `rmtree` may already have removed the leftover
    # by its full path; 3.11's recursed into it. Unrefused, it goes either way.
    discard(projection)
    assert not temporary.exists()


def test_a_revert_frees_its_slot_and_a_delete_counts(tmp_path: Path) -> None:
    reverted = project(_candidate(tmp_path / "reverted"))
    (reverted.root / "a.md").write_text("beta", encoding="utf-8")
    (reverted.root / "cfg.yaml").write_text("k: w", encoding="utf-8")
    (reverted.root / "a.md").write_text("alpha", encoding="utf-8")  # the original bytes
    assert changed_file_paths(reverted) == ("cfg.yaml",)
    admitted = admit(reverted, writable_paths=("**",), budgets=_budgets(max_changed_files=1))
    assert [c.path for c in admitted] == ["cfg.yaml"]

    deleted = project(_candidate(tmp_path / "deleted"))
    (deleted.root / "a.md").unlink()
    (deleted.root / "cfg.yaml").write_text("k: w", encoding="utf-8")
    assert changed_file_paths(deleted) == ("a.md", "cfg.yaml")
    with pytest.raises(ActorViolation, match="changed-file budget exceeded"):
        admit(deleted, writable_paths=("**",), budgets=_budgets(max_changed_files=1))


def test_a_deleted_file_written_back_still_counts(tmp_path: Path) -> None:
    """Today's rule (e1 known issues): writing a deleted file's bytes back does
    not free its slot. The re-created file has the writer's mode, not the
    projection's baseline mode, so it still differs and `admit` refuses the mode
    change. Re-created at the baseline mode it would match again."""

    projection = project(_candidate(tmp_path))
    target = projection.root / "a.md"
    baseline_mode = stat.S_IMODE(target.stat().st_mode)
    target.unlink()
    assert changed_file_paths(projection) == ("a.md",)
    target.write_text("alpha", encoding="utf-8")  # the original bytes
    os.chmod(target, 0o644)  # what `write_text` gives under the usual umask 022
    assert baseline_mode == 0o600
    assert changed_file_paths(projection) == ("a.md",)
    with pytest.raises(ActorViolation, match="mode changed: a.md"):
        admit(projection, writable_paths=("**",), budgets=_budgets(max_changed_files=1))

    os.chmod(target, baseline_mode)
    assert changed_file_paths(projection) == ()


@pytest.mark.parametrize("seed", range(40))
def test_the_live_count_equals_what_admission_counts(tmp_path: Path, seed: int) -> None:
    """The contract count agreement: random creates, updates, deletes and reverts."""

    rng = random.Random(seed)
    projection = project(_candidate(tmp_path))
    originals = {
        path.relative_to(projection.root).as_posix(): path.read_bytes()
        for path in projection.root.rglob("*")
        if path.is_file()
    }
    created = 0
    for _ in range(rng.randint(1, 12)):
        live = sorted(
            path.relative_to(projection.root).as_posix()
            for path in projection.root.rglob("*")
            if path.is_file()
        )
        operation = rng.choice(("create", "update", "delete", "revert"))
        if operation == "create":
            created += 1
            (projection.root / f"n{created}.md").write_text(f"new {created}", encoding="utf-8")
        elif operation == "update" and live:
            (projection.root / rng.choice(live)).write_text(f"v{rng.random()}", encoding="utf-8")
        elif operation == "delete" and live:
            (projection.root / rng.choice(live)).unlink()
        elif operation == "revert":
            relative = rng.choice(sorted(originals))
            target = projection.root / relative
            target.write_bytes(originals[relative])
            target.chmod(0o600)  # the projection's normalized file mode (`_normalized`)
    paths = changed_file_paths(projection)
    admitted = admit(projection, writable_paths=("**",), budgets=_budgets())
    assert paths == tuple(change.path for change in admitted)
    if paths:
        exact = admit(
            projection, writable_paths=("**",), budgets=_budgets(max_changed_files=len(paths))
        )
        assert tuple(change.path for change in exact) == paths
    if len(paths) > 1:
        with pytest.raises(ActorViolation, match="changed-file budget exceeded"):
            admit(
                projection,
                writable_paths=("**",),
                budgets=_budgets(max_changed_files=len(paths) - 1),
            )


def test_the_two_budget_refusals_carry_closed_codes(tmp_path: Path) -> None:
    written = project(_candidate(tmp_path / "written"))
    (written.root / "a.md").write_text("x" * 100, encoding="utf-8")
    with pytest.raises(BudgetViolation) as refused:
        admit(written, writable_paths=("**",), budgets=_budgets(max_written_bytes=10))
    assert (refused.value.code, str(refused.value)) == (
        "written_byte_budget",
        "written-byte budget exceeded",
    )

    counted = project(_candidate(tmp_path / "counted"))
    (counted.root / "a.md").write_text("beta", encoding="utf-8")
    (counted.root / "cfg.yaml").write_text("k: w", encoding="utf-8")
    with pytest.raises(BudgetViolation) as refused:
        admit(counted, writable_paths=("**",), budgets=_budgets(max_changed_files=1))
    assert (refused.value.code, str(refused.value)) == (
        "changed_file_budget",
        "changed-file budget exceeded",
    )

    outside = project(_candidate(tmp_path / "outside"))
    (outside.root / "other.txt").write_text("o", encoding="utf-8")
    with pytest.raises(ActorViolation) as refused:
        admit(outside, writable_paths=("a.md",), budgets=_budgets())
    assert not isinstance(refused.value, BudgetViolation)
    assert refused.value.code == "actor_violation"
