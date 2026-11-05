"""Family-14 role visibility: the pure view, the sparse projection and its digest.

F0 R64 and R67, routine choices 3 and 4 of the H11 plan. A *hidden* file is
one the artifact includes but that lies outside `readable_paths ∪
writable_paths`: it must never appear in a role projection, and publication
from the full capture must keep its bytes and modes.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from evolution.tree.artifacts.artifact_copy import restore_writable, snapshot_tree
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_policy import matches as policy_matches
from evolution.tree.roles.role_visibility import RoleVisibilityView
from evolution.tree.roles.workspace import (
    ActorViolation,
    Budgets,
    ControlCollision,
    WorkspaceChanged,
    _writable,
    admit,
    capture_tree,
    discard,
    project,
    publish,
    role_projection_digest,
    visible_capture,
)
from evolution.tree.target.connection_contract import (
    ConnectionContract,
    MetricContract,
    RoleVisibility,
)

WRITABLE = ("skills/**",)
READABLE = ("prompts/**", "*.txt")


def _view(
    readable: tuple[str, ...] = READABLE, writable: tuple[str, ...] = WRITABLE
) -> RoleVisibilityView:
    return RoleVisibilityView(readable_paths=readable, writable_paths=writable)


def _budgets() -> Budgets:
    return Budgets(max_changed_files=None, max_written_bytes=1 << 20, max_payload_bytes=1 << 16)


def _write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, mode)


def _workspace(base: Path) -> Path:
    """Writable, readable-only and hidden files, with a hidden executable and odd modes.

    Visible under `_view()`: `skills/SKILL.md` (writable), `prompts/system.md`
    and `notes/deep/readme.txt` (readable). Hidden: `runtime/model.bin`,
    `tools/run.sh` (executable), `secret/inner/key.pem`, the hidden-only
    directories `runtime`, `tools`, `secret`, `secret/inner`, and the empty
    directory `empty`.
    """

    workspace = base / "ws"
    workspace.mkdir()
    _write(workspace / "skills" / "SKILL.md", b"skill v1\n", 0o644)
    _write(workspace / "prompts" / "system.md", b"system prompt\n", 0o644)
    _write(workspace / "notes" / "deep" / "readme.txt", b"readme\n", 0o640)
    _write(workspace / "runtime" / "model.bin", b"\x00\x01binary\xff", 0o600)
    _write(workspace / "tools" / "run.sh", b"#!/bin/sh\necho hi\n", 0o755)
    _write(workspace / "secret" / "inner" / "key.pem", b"-----KEY-----\n", 0o400)
    (workspace / "empty").mkdir()
    os.chmod(workspace / "empty", 0o711)
    os.chmod(workspace / "secret" / "inner", 0o750)
    os.chmod(workspace / "secret", 0o701)
    os.chmod(workspace / "notes" / "deep", 0o750)
    return workspace


VISIBLE_FILES = {"skills/SKILL.md", "prompts/system.md", "notes/deep/readme.txt"}
VISIBLE_DIRECTORIES = {"skills", "prompts", "notes", "notes/deep"}
HIDDEN = {
    "runtime",
    "runtime/model.bin",
    "tools",
    "tools/run.sh",
    "secret",
    "secret/inner",
    "secret/inner/key.pem",
    "empty",
}


def _entries(root: Path) -> set[str]:
    return {path for path in capture_tree(root) if path != "."}


# -- the effective set (choice 4) --------------------------------------------


@pytest.mark.parametrize(
    ("readable", "writable", "path", "expected"),
    [
        # writable-only: a writable path is visible with no readable rule at all
        ((), ("skills/**",), "skills/SKILL.md", True),
        ((), ("skills/**",), "prompts/system.md", False),
        # readable-only: readable paths are visible but not writable
        (("prompts/**",), (), "prompts/system.md", True),
        (("prompts/**",), (), "skills/SKILL.md", False),
        # overlapping globs: either side is enough, overlap changes nothing
        (("skills/*.md", "**/*.md"), ("skills/**",), "skills/SKILL.md", True),
        (("skills/*.md", "**/*.md"), ("skills/**",), "docs/a.md", True),
        (("skills/*.md", "**/*.md"), ("skills/**",), "docs/a.bin", False),
        # a basename pattern matches at any depth
        (("*.txt",), (), "a/b/c/readme.txt", True),
        (("*.txt",), (), "readme.txt", True),
        (("*.txt",), (), "a/b/readme.md", False),
        # a basename pattern naming a directory covers its subtree
        (("prompts",), (), "prompts/system.md", True),
        (("prompts",), (), "x/prompts/system.md", True),
        # an anchored pattern covers the subtree under it, but only at the root
        (("docs/guide",), (), "docs/guide/a/b.md", True),
        (("docs/guide",), (), "other/docs/guide/a.md", False),
        (("docs/guide",), (), "docs/other.md", False),
        # empty readable_paths: only the writable set is visible
        ((), ("skills/SKILL.md",), "skills/SKILL.md", True),
        ((), ("skills/SKILL.md",), "skills/other.md", False),
        # a dependency path is hidden unless a readable rule names it
        ((), ("skills/**",), "runtime/model.bin", False),
        (("runtime/model.bin",), ("skills/**",), "runtime/model.bin", True),
    ],
)
def test_visible_is_the_union_of_readable_and_writable_rules(
    readable: tuple[str, ...], writable: tuple[str, ...], path: str, expected: bool
) -> None:
    assert _view(readable, writable).visible(path) is expected


def test_empty_rules_hide_everything() -> None:
    view = _view((), ())
    assert not view.visible("skills/SKILL.md")
    assert not view.visible("a")


def test_for_campaign_reads_role_visibility_and_the_writable_set() -> None:
    connection = ConnectionContract(
        metric_contract=MetricContract(kind="pointwise", target_fields=()),
        role_visibility=RoleVisibility(readable_paths=("prompts/**",)),
    )
    policy = ArtifactPolicyV1(
        excludes=(), dependency_paths=("runtime/model.bin",), writable_paths=("skills/**",)
    )
    view = RoleVisibilityView.for_campaign(connection, policy)
    assert view == RoleVisibilityView(readable_paths=("prompts/**",), writable_paths=("skills/**",))
    assert view is not None
    assert view.visible("skills/SKILL.md")
    assert view.visible("prompts/system.md")
    # the declared dependency is included in the artifact but hidden from the roles
    assert not view.visible("runtime/model.bin")


def test_for_campaign_is_none_without_a_connection() -> None:
    """Families up to 13 carry no `ConnectionContract`; they get no view at all."""

    policy = ArtifactPolicyV1(excludes=(), dependency_paths=(), writable_paths=("skills/**",))
    assert RoleVisibilityView.for_campaign(None, policy) is None


def test_an_excluded_path_is_absent_however_readable_it_is(tmp_path: Path) -> None:
    """Exclusions win: a readable rule cannot restore what the store copy never holds."""

    source = tmp_path / "src"
    _write(source / "skills" / "SKILL.md", b"skill\n", 0o644)
    _write(source / ".env", b"SECRET=1\n", 0o600)
    _write(source / "logs" / "run.log", b"log\n", 0o644)
    policy = ArtifactPolicyV1(excludes=(), dependency_paths=(), writable_paths=("skills/**",))
    published = tmp_path / "published"
    snapshot_tree(source, policy, published)
    view = _view(("**", ".env", "*.log"), policy.writable_paths)
    assert view.visible(".env")  # the rule matches...
    projected = visible_capture(capture_tree(published), view.visible)
    assert ".env" not in projected  # ... but the source tree never holds it
    assert "logs/run.log" not in projected
    assert set(projected) == {".", "skills", "skills/SKILL.md"}


_PROPERTY_SEGMENTS = ("skills", "prompts", "a", "b.md", "x.txt", ".github", "deep", "SKILL.md")
_PROPERTY_RULES = (
    "skills/**",
    "**/*.md",
    "*.md",
    "skill**",
    ".github/**",
    "a/b.md",
    "prompts",
    "deep/*",
    "*.txt",
    "x.txt",
)


def test_every_path_admit_accepts_as_writable_is_visible() -> None:
    """Property, over a seeded sample: writable ⊆ visible for any readable set."""

    generator = random.Random(20261012)
    checked = 0
    for _ in range(400):
        writable = tuple(generator.sample(_PROPERTY_RULES, generator.randint(0, 3)))
        readable = tuple(generator.sample(_PROPERTY_RULES, generator.randint(0, 3)))
        view = _view(readable, writable)
        for _ in range(20):
            path = "/".join(
                generator.choice(_PROPERTY_SEGMENTS) for _ in range(generator.randint(1, 4))
            )
            # `_writable` is the predicate `admit` itself applies to every changed file.
            if _writable(path, writable):
                checked += 1
                assert view.visible(path), (path, readable, writable)
            expected = any(
                policy_matches(rule, PurePosixPath(path), False) for rule in readable + writable
            )
            assert view.visible(path) is expected
    assert checked > 100


# -- the filtered capture and its digest (choice 3) ---------------------------


def test_visible_capture_keeps_root_visible_files_and_their_ancestors_only(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    capture = capture_tree(workspace)
    projected = visible_capture(capture, _view().visible)
    assert set(projected) == {"."} | VISIBLE_FILES | VISIBLE_DIRECTORIES
    for path, entry in projected.items():
        assert entry == capture[path]  # each with its captured mode and bytes
    assert not set(projected) & HIDDEN


def test_visible_capture_of_nothing_visible_is_the_root_alone(tmp_path: Path) -> None:
    capture = capture_tree(_workspace(tmp_path))
    assert visible_capture(capture, _view((), ()).visible) == {".": capture["."]}


def _expected_digest(entries: list[dict[str, Any]]) -> str:
    payload = {"version": "role_projection/1", "entries": entries}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_role_projection_digest_pins_the_role_projection_1_encoding(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / "a" / "run.sh", b"#!/bin/sh\n", 0o750)
    _write(root / "a" / "b.md", b"bee\n", 0o600)
    _write(root / "hidden.bin", b"\x00", 0o644)
    visible: Callable[[str], bool] = lambda path: path.startswith("a/")  # noqa: E731
    expected = _expected_digest(
        [
            {"path": "a", "kind": "directory"},
            {"path": "a/b.md", "kind": "file", "executable": False,
             "sha256": hashlib.sha256(b"bee\n").hexdigest()},
            {"path": "a/run.sh", "kind": "file", "executable": True,
             "sha256": hashlib.sha256(b"#!/bin/sh\n").hexdigest()},
        ]
    )  # fmt: skip
    assert role_projection_digest(capture_tree(root), visible) == expected


def test_published_and_restore_writable_copies_share_one_digest(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    policy = ArtifactPolicyV1(
        excludes=(), dependency_paths=("runtime/model.bin",), writable_paths=WRITABLE
    )
    published = tmp_path / "published"
    snapshot_tree(source, policy, published)
    attempt = tmp_path / "attempt"
    shutil.copytree(published, attempt)
    restore_writable(attempt)
    published_capture, attempt_capture = capture_tree(published), capture_tree(attempt)
    assert published_capture["skills/SKILL.md"].mode != attempt_capture["skills/SKILL.md"].mode
    assert published_capture["skills"].mode != attempt_capture["skills"].mode
    visible = _view().visible
    assert role_projection_digest(published_capture, visible) == role_projection_digest(
        attempt_capture, visible
    )


def test_executable_bit_counts_on_visible_files_only(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    visible = _view().visible
    before = role_projection_digest(capture_tree(workspace), visible)
    os.chmod(workspace / "tools" / "run.sh", 0o644)  # hidden: no execute bit any more
    assert role_projection_digest(capture_tree(workspace), visible) == before
    os.chmod(workspace / "skills" / "SKILL.md", 0o744)  # visible: gains an execute bit
    assert role_projection_digest(capture_tree(workspace), visible) != before


def test_digest_ignores_hidden_content_and_covers_visible_content(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    visible = _view().visible
    before = role_projection_digest(capture_tree(workspace), visible)
    (workspace / "runtime" / "model.bin").write_bytes(b"other")
    (workspace / "extra").mkdir()
    assert role_projection_digest(capture_tree(workspace), visible) == before
    (workspace / "prompts" / "system.md").write_bytes(b"changed\n")
    assert role_projection_digest(capture_tree(workspace), visible) != before


# -- the sparse projection ---------------------------------------------------


def test_sparse_projection_never_materializes_a_hidden_entry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    projection = project(workspace, visible=_view().visible)
    try:
        assert _entries(projection.root) == VISIBLE_FILES | VISIBLE_DIRECTORIES
        # the capture stays full: publication works from it
        assert HIDDEN <= set(projection.capture)
        assert set(projection.baseline) == {"."} | VISIBLE_FILES | VISIBLE_DIRECTORIES
        # every projected file at 0600, every projected directory at its captured mode
        for path in VISIBLE_FILES:
            assert stat.S_IMODE((projection.root / path).stat().st_mode) == 0o600
        for path in VISIBLE_DIRECTORIES:
            assert (
                stat.S_IMODE((projection.root / path).stat().st_mode)
                == projection.capture[path].mode
            )
        assert projection.changes() == ()
    finally:
        discard(projection)


def test_project_accepts_the_matching_expected_digest(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    visible = _view().visible
    digest = role_projection_digest(capture_tree(workspace), visible)
    projection = project(workspace, visible=visible, expected_digest=digest)
    try:
        assert _entries(projection.root) == VISIBLE_FILES | VISIBLE_DIRECTORIES
    finally:
        discard(projection)


def test_project_refuses_a_digest_mismatch_before_materializing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    visible = _view().visible
    digest = role_projection_digest(capture_tree(workspace), visible)
    (workspace / "prompts" / "system.md").write_bytes(b"drifted\n")
    tmp_root = tmp_path / "private"
    tmp_root.mkdir()
    with pytest.raises(WorkspaceChanged, match="role projection changed"):
        project(workspace, visible=visible, expected_digest=digest, tmp_root=tmp_root)
    assert list(tmp_root.iterdir()) == []


def test_expected_digest_without_visible_is_a_value_error(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    tmp_root = tmp_path / "private"
    tmp_root.mkdir()
    with pytest.raises(ValueError, match="visible"):
        project(workspace, expected_digest="0" * 64, tmp_root=tmp_root)
    assert list(tmp_root.iterdir()) == []


def test_hidden_control_prefix_still_collides_on_the_full_capture(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _write(workspace / ".evolution" / "x.json", b"{}", 0o600)
    assert not _view().visible(".evolution/x.json")
    with pytest.raises(ControlCollision):
        project(workspace, control_prefix=".evolution", visible=_view().visible)


# -- adversarial admission and publication over a sparse projection -----------


def _sparse(tmp_path: Path) -> tuple[Path, Any]:
    workspace = _workspace(tmp_path)
    return workspace, project(workspace, visible=_view().visible)


def _admit(projection: Any) -> Any:
    return admit(projection, writable_paths=WRITABLE, budgets=_budgets())


def test_creating_a_file_at_an_existing_hidden_path_is_refused(tmp_path: Path) -> None:
    workspace, projection = _sparse(tmp_path)
    try:
        _write(projection.root / "runtime" / "model.bin", b"forged", 0o600)
        with pytest.raises(ActorViolation, match="outside writable set: runtime/model.bin"):
            _admit(projection)
    finally:
        discard(projection)
    assert (workspace / "runtime" / "model.bin").read_bytes() == b"\x00\x01binary\xff"


def test_recreating_a_hidden_only_directory_is_refused(tmp_path: Path) -> None:
    _workspace_root, projection = _sparse(tmp_path)
    try:
        (projection.root / "secret").mkdir(mode=0o700)
        with pytest.raises(ActorViolation, match="unused directory: secret"):
            _admit(projection)
        _write(projection.root / "secret" / "new.pem", b"x", 0o600)
        with pytest.raises(ActorViolation, match="outside writable set: secret/new.pem"):
            _admit(projection)
    finally:
        discard(projection)


def test_a_directory_created_over_a_hidden_file_is_refused(tmp_path: Path) -> None:
    """A basename writable rule admits `README/x.md`; the hidden file `README` must survive."""

    workspace = _workspace(tmp_path)
    _write(workspace / "README", b"hidden readme\n", 0o644)
    projection = project(workspace, visible=_view(writable=("*.md",)).visible)
    try:
        assert not (projection.root / "README").exists()
        _write(projection.root / "README" / "x.md", b"new\n", 0o600)
        with pytest.raises(ActorViolation, match="entry kind changed: README"):
            admit(projection, writable_paths=("*.md",), budgets=_budgets())
    finally:
        discard(projection)
    assert (workspace / "README").read_bytes() == b"hidden readme\n"


def test_a_file_created_over_a_hidden_empty_directory_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    projection = project(workspace, visible=_view(writable=("skills/**", "empty")).visible)
    try:
        assert not (projection.root / "empty").exists()  # an empty directory is not projected
        _write(projection.root / "empty", b"file now\n", 0o600)
        with pytest.raises(ActorViolation, match="entry kind changed: empty"):
            admit(projection, writable_paths=("skills/**", "empty"), budgets=_budgets())
    finally:
        discard(projection)
    assert (workspace / "empty").is_dir()


def _probe(tmp_path: Path, name: str, act: Callable[[Path], None]) -> str:
    """The refusal text an actor gets for `act` on a fresh sparse projection, path elided.

    Writable `skills/**`; hidden: the directory `tools`, the file `README`, the
    file `runtime/model.bin` and every other hidden entry of `_workspace`.
    """

    base = tmp_path / name
    base.mkdir()
    workspace = _workspace(base)
    _write(workspace / "README", b"hidden readme\n", 0o644)
    projection = project(workspace, visible=_view().visible)
    try:
        act(projection.root)
        with pytest.raises(ActorViolation) as refused:
            _admit(projection)
    finally:
        discard(projection)
    return str(refused.value)


def _file(path: str) -> Callable[[Path], None]:
    return lambda root: _write(root / path, b"probe\n", 0o600)


def _directory_with_child(path: str) -> Callable[[Path], None]:
    return lambda root: _write(root / path / "x.bin", b"probe\n", 0o600)


def _empty_directory(path: str) -> Callable[[Path], None]:
    return lambda root: (root / path).mkdir(mode=0o700)


@pytest.mark.parametrize(
    ("hidden", "absent", "make", "expected"),
    [
        # a file at a non-writable path: over a hidden directory, a hidden file, nothing
        ("tools", "nothere", _file, "outside writable set: {}"),
        ("runtime/model.bin", "runtime/nothere.bin", _file, "outside writable set: {}"),
        # a directory with a non-writable child: over a hidden file, a hidden directory
        ("README", "NOPE", _directory_with_child, "outside writable set: {}/x.bin"),
        ("secret", "NOPE", _directory_with_child, "outside writable set: {}/x.bin"),
        # an empty directory: over a hidden file, a hidden directory
        ("README", "NOPE", _empty_directory, "unused directory: {}"),
        ("tools", "NOPE", _empty_directory, "unused directory: {}"),
    ],
)
def test_probes_at_hidden_and_absent_paths_get_the_same_refusal(
    tmp_path: Path,
    hidden: str,
    absent: str,
    make: Callable[[str], Callable[[Path], None]],
    expected: str,
) -> None:
    """A refusal never tells the actor whether a path it may not touch is hidden or absent."""

    assert _probe(tmp_path, "hidden", make(hidden)) == expected.format(hidden)
    assert _probe(tmp_path, "absent", make(absent)) == expected.format(absent)


def test_any_other_violation_wins_over_a_kind_collision(tmp_path: Path) -> None:
    """`README/x.md` collides with the hidden `README`, but the read-only edit is reported."""

    workspace = _workspace(tmp_path)
    _write(workspace / "README", b"hidden readme\n", 0o644)
    projection = project(workspace, visible=_view(writable=("*.md",)).visible)
    try:
        _write(projection.root / "README" / "x.md", b"new\n", 0o600)
        (projection.root / "notes" / "deep" / "readme.txt").write_bytes(b"edited\n")
        with pytest.raises(ActorViolation, match="^outside writable set: notes/deep/readme.txt$"):
            admit(projection, writable_paths=("*.md",), budgets=_budgets())
    finally:
        discard(projection)


def test_a_too_narrow_visible_cannot_publish_over_a_hidden_file(tmp_path: Path) -> None:
    """Backstop: a caller whose `visible` omits a writable rule cannot overwrite a hidden file."""

    workspace = _workspace(tmp_path)
    projection = project(workspace, visible=_view(readable=("prompts/**",), writable=()).visible)
    try:
        assert not (projection.root / "skills").exists()
        _write(projection.root / "skills" / "SKILL.md", b"forged\n", 0o600)
        with pytest.raises(ActorViolation, match="^hidden entry collision: skills/SKILL.md$"):
            _admit(projection)
    finally:
        discard(projection)
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"skill v1\n"


def test_a_writable_file_may_join_a_hidden_directory(tmp_path: Path) -> None:
    """Same-kind directories merge: the hidden directory keeps its mode and contents."""

    workspace = _workspace(tmp_path)
    projection = project(workspace, visible=_view(writable=("skills/**", "*.md")).visible)
    try:
        _write(projection.root / "secret" / "notes.md", b"joined\n", 0o600)
        changes = admit(projection, writable_paths=("skills/**", "*.md"), budgets=_budgets())
        assert [(change.path, change.action) for change in changes] == [
            ("secret/notes.md", "create")
        ]
        publish(changes, projection)
    finally:
        discard(projection)
    assert stat.S_IMODE((workspace / "secret").lstat().st_mode) == 0o701
    assert (workspace / "secret" / "inner" / "key.pem").read_bytes() == b"-----KEY-----\n"
    assert (workspace / "secret" / "notes.md").read_bytes() == b"joined\n"


@pytest.mark.parametrize("kind", ["symlink", "hard_link", "fifo"])
def test_links_and_fifos_in_a_sparse_projection_are_unsupported_entries(
    tmp_path: Path, kind: str
) -> None:
    workspace, projection = _sparse(tmp_path)
    try:
        target = projection.root / "skills" / "evil"
        if kind == "symlink":
            os.symlink(workspace / "runtime" / "model.bin", target)
        elif kind == "hard_link":
            os.link(projection.root / "prompts" / "system.md", target)
        else:
            os.mkfifo(target)
        with pytest.raises(ActorViolation, match="unsupported entry"):
            _admit(projection)
    finally:
        discard(projection)


def test_mode_only_changes_and_read_only_deletes_are_refused(tmp_path: Path) -> None:
    _workspace_root, projection = _sparse(tmp_path)
    try:
        os.chmod(projection.root / "prompts" / "system.md", 0o644)
        with pytest.raises(ActorViolation, match="outside writable set: prompts/system.md"):
            _admit(projection)
        os.chmod(projection.root / "prompts" / "system.md", 0o600)
        os.chmod(projection.root / "skills" / "SKILL.md", 0o644)
        with pytest.raises(ActorViolation, match="mode changed: skills/SKILL.md"):
            _admit(projection)
        os.chmod(projection.root / "skills" / "SKILL.md", 0o600)
        (projection.root / "notes" / "deep" / "readme.txt").unlink()
        with pytest.raises(ActorViolation, match="outside writable set: notes/deep/readme.txt"):
            _admit(projection)
    finally:
        discard(projection)


def test_concurrent_mode_change_of_a_hidden_file_blocks_publication(tmp_path: Path) -> None:
    workspace, projection = _sparse(tmp_path)
    try:
        (projection.root / "skills" / "SKILL.md").write_bytes(b"skill v2\n")
        changes = _admit(projection)
        os.chmod(workspace / "tools" / "run.sh", 0o744)
        with pytest.raises(WorkspaceChanged):
            publish(changes, projection)
    finally:
        discard(projection)
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"skill v1\n"


def test_publication_preserves_every_hidden_byte_and_mode(tmp_path: Path) -> None:
    workspace, projection = _sparse(tmp_path)
    hidden_before = {
        path: entry for path, entry in capture_tree(workspace).items() if path in HIDDEN
    }
    try:
        (projection.root / "skills" / "SKILL.md").write_bytes(b"skill v2\n")
        _write(projection.root / "skills" / "extra.md", b"extra\n", 0o600)
        changes = _admit(projection)
        publish(changes, projection)
    finally:
        discard(projection)
    after = capture_tree(workspace)
    assert {path: after[path] for path in HIDDEN} == hidden_before
    assert after["tools/run.sh"].mode & 0o111 == 0o111
    assert stat.S_IMODE(after["empty"].mode) == 0o711
    assert stat.S_IMODE(after["secret"].mode) == 0o701
    assert after["skills/SKILL.md"].content == b"skill v2\n"
    assert after["skills/SKILL.md"].mode == 0o644
    assert after["skills/extra.md"].content == b"extra\n"
