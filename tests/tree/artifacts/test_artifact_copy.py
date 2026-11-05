import errno
import os
import stat

import pytest

from evolution.tree.artifacts.artifact_copy import (
    check_destination_parent,
    fsync_tree,
    map_os_error,
    publish,
    remove_tree,
    restore_writable,
    snapshot_tree,
    stage_copy,
)
from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from tests.tree.artifacts.test_artifact_policy import seed, write

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))


def test_stage_copies_only_the_two_sets_with_fresh_inodes_and_freezes(tmp_path):
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert staged.scan.candidate_digest == expected.candidate_digest
    assert not (staged.root / ".env").exists()
    assert not (staged.root / "src" / "__pycache__").exists()
    # "src" itself is not excluded by any pattern (only its "__pycache__" child
    # is), so scan_tree includes it as its own hollow directory Entry; digest
    # equality with `expected` requires stage_copy to recreate it too.
    assert (staged.root / "src").is_dir()
    assert (staged.root / "uv.lock").exists()
    assert (staged.root / "skills/SKILL.md").stat().st_ino != (
        src / "skills/SKILL.md"
    ).stat().st_ino
    assert not (staged.root / "skills/SKILL.md").stat().st_mode & stat.S_IWUSR
    assert (staged.root / "scripts/run.sh").stat().st_mode & stat.S_IXUSR


def test_stage_rejects_source_changed_after_prescan(tmp_path):
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    write(src, "skills/SKILL.md", b"changed")
    with pytest.raises(ArtifactUnsafeError) as info:
        stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert info.value.code == "artifact_unsafe"
    assert list((tmp_path / "staging").iterdir()) == []  # staging discarded


def test_publish_then_reuse_then_reject(tmp_path, monkeypatch):
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    dest = tmp_path / "artifacts/champions/v0"
    dest.parent.mkdir(parents=True)
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "published"
    assert synced and not staged.root.exists()
    assert dest.stat().st_mode & 0o222 == 0
    with pytest.raises(PermissionError):
        (dest / "INTRUDER").write_bytes(b"x")
    synced.clear()
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "reused"
    assert synced, "reuse must re-run the durability barrier"
    (dest / "skills/SKILL.md").chmod(0o644)
    (dest / "skills/SKILL.md").write_bytes(b"tampered")
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    with pytest.raises(ArtifactUnsafeError) as info:
        publish(staged, dest, expected, POLICY)
    assert info.value.code == "destination_exists"


def test_publish_removes_staging_when_the_rename_fails(tmp_path, monkeypatch):
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    dest = tmp_path / "artifacts/champions/v0"
    dest.parent.mkdir(parents=True)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        publish(staged, dest, expected, POLICY)
    assert not staged.root.exists()
    assert not dest.exists()


def test_stage_rejects_a_source_entry_that_vanished_after_prescan(tmp_path):
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    (src / "skills/SKILL.md").unlink()
    with pytest.raises(ArtifactUnsafeError) as info:
        stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert info.value.code == "artifact_unsafe"
    assert list((tmp_path / "staging").iterdir()) == []  # staging discarded


def test_publish_rejects_reuse_of_a_foreign_owned_destination(tmp_path, monkeypatch):
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    dest = tmp_path / "artifacts/champions/v0"
    dest.parent.mkdir(parents=True)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "published"
    real_euid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    with pytest.raises(ArtifactUnsafeError) as info:
        publish(staged, dest, expected, POLICY)
    assert info.value.code == "destination_exists"


def test_capacity_and_io_mapping_and_restore_remove(tmp_path):
    assert map_os_error(OSError(errno.ENOSPC, "x")) == "storage_capacity_exhausted"
    assert map_os_error(OSError(errno.EDQUOT, "x")) == "storage_capacity_exhausted"
    assert map_os_error(OSError(errno.EIO, "x")) == "io_failure"
    src = seed(tmp_path)
    staged = stage_copy(src, scan_tree(src, POLICY), POLICY, tmp_path / "staging", freeze=True)
    restore_writable(staged.root)
    assert (staged.root / "skills/SKILL.md").stat().st_mode & stat.S_IWUSR
    os.symlink("/etc", staged.root / "skills/evil")
    remove_tree(staged.root)
    assert not staged.root.exists() and os.path.exists("/etc")


def test_remove_tree_refuses_a_root_whose_last_component_is_dot_dot(tmp_path):
    """`shutil.rmtree` would otherwise delete `tmp_path` itself wholesale:
    `Path` does not normalise a trailing "..", so `tmp_path / "x" / ".."`
    names `tmp_path`, not `tmp_path / "x"`. Checked by name before any
    `lstat`, so it is refused even though `tmp_path` obviously exists."""
    (tmp_path / "keep.txt").write_text("keep me")
    with pytest.raises(ArtifactUnsafeError) as info:
        remove_tree(tmp_path / "x" / "..")
    assert info.value.code == "escape"
    assert (tmp_path / "keep.txt").exists()


def test_fsync_tree_covers_every_file_and_directory_without_following_symlinks(
    tmp_path, monkeypatch
):
    src = seed(tmp_path)
    opened = []
    real_open = os.open
    monkeypatch.setattr(
        os,
        "open",
        lambda p, flags, *a, **k: (opened.append((str(p), flags)), real_open(p, flags, *a, **k))[1],
    )
    fsync_tree(src)
    paths = [p for p, _flags in opened]
    assert (
        str(src / "skills/SKILL.md") in paths and str(src / "skills") in paths and str(src) in paths
    )
    assert all(flags & os.O_NOFOLLOW for _path, flags in opened)


def test_fsync_tree_propagates_a_listing_error_instead_of_completing_silently(tmp_path):
    root = tmp_path / "root"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_bytes(b"x")
    os.chmod(locked, 0o000)
    try:
        with pytest.raises(OSError):
            fsync_tree(root)
    finally:
        os.chmod(locked, 0o700)  # allow test cleanup to remove it


def test_check_destination_parent_rejects_symlink_escape(tmp_path):
    run_root = tmp_path / "run"
    (run_root / "artifacts").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, run_root / "artifacts" / "champions")
    with pytest.raises(ArtifactUnsafeError) as info:
        check_destination_parent(run_root / "artifacts" / "champions" / "v0", run_root)
    assert info.value.code == "escape"


def test_check_destination_parent_accepts_a_strict_child(tmp_path):
    run_root = tmp_path / "run"
    (run_root / "artifacts" / "champions").mkdir(parents=True)
    check_destination_parent(run_root / "artifacts" / "champions" / "v0", run_root)


def test_check_destination_parent_rejects_foreign_owner(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    (run_root / "artifacts" / "champions").mkdir(parents=True)
    real_euid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
    with pytest.raises(ArtifactUnsafeError) as info:
        check_destination_parent(run_root / "artifacts" / "champions" / "v0", run_root)
    assert info.value.code == "foreign_owner"


def test_check_destination_parent_rejects_escape_outside_run_root(tmp_path):
    run_root = tmp_path / "run"
    (run_root / "artifacts").mkdir(parents=True)
    with pytest.raises(ArtifactUnsafeError) as info:
        check_destination_parent(tmp_path / "outside" / "v0", run_root)
    assert info.value.code == "escape"


def test_stage_rejects_an_included_file_added_after_the_prescan(tmp_path):
    """The staged tree reproduces `expected`; only re-scanning the source sees it."""
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    write(src, "skills/EXTRA.md", b"added")
    staging = tmp_path / "staging"
    with pytest.raises(ArtifactUnsafeError) as info:
        stage_copy(src, expected, POLICY, staging, freeze=True)
    assert info.value.code == "artifact_unsafe"
    assert list(staging.iterdir()) == []  # staging discarded


def test_reuse_rejects_a_destination_carrying_an_excluded_extra_entry(tmp_path):
    """Equal digests are not equal trees: the digests never cover an excluded name."""
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    dest = tmp_path / "artifacts/champions/v0"
    dest.parent.mkdir(parents=True)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "published"
    # The identical destination is still reusable.
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "reused"
    dest.chmod(0o700)
    (dest / ".env").write_bytes(b"SECRET=1")
    assert scan_tree(dest, POLICY).candidate_digest == expected.candidate_digest
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    with pytest.raises(ArtifactUnsafeError) as info:
        publish(staged, dest, expected, POLICY)
    assert info.value.code == "destination_exists"
    assert not staged.root.exists()


def test_reuse_refreezes_a_destination_whose_modes_drifted(tmp_path):
    """Reuse adopts bytes it did not write: a writable entry inside an otherwise
    exact destination must not be published under a frozen artifact's name.
    """
    src = seed(tmp_path)
    expected = scan_tree(src, POLICY)
    dest = tmp_path / "artifacts/champions/v0"
    dest.parent.mkdir(parents=True)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "published"
    dest.chmod(0o700)
    (dest / "skills").chmod(0o755)
    (dest / "skills/SKILL.md").chmod(0o644)
    staged = stage_copy(src, expected, POLICY, tmp_path / "staging", freeze=True)
    assert publish(staged, dest, expected, POLICY) == "reused"
    for path in (dest, *dest.rglob("*")):
        assert path.lstat().st_mode & 0o222 == 0, path


def _tree(root):
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "SKILL.md").write_text("# hello\nbody\n")
    (root / "uv.lock").write_text("lock\n")
    (root / "build").mkdir()
    (root / "build" / "junk").write_text("x")


def test_snapshot_copies_exactly_the_scanned_entries_and_freezes(tmp_path):
    policy = ArtifactPolicyV1(("build/**",), ("uv.lock",), ("skills/**",))
    source = tmp_path / "src"
    _tree(source)
    destination = tmp_path / "snap" / "i1"
    scan = snapshot_tree(source, policy, destination)
    assert scan == scan_tree(destination, policy)
    assert scan.candidate_digest == scan_tree(source, policy).candidate_digest
    assert not (destination / "build").exists()
    for entry in (destination, *destination.rglob("*")):
        assert not stat.S_IMODE(entry.lstat().st_mode) & stat.S_IWUSR
    with pytest.raises(PermissionError):
        (destination / "new-top-level").write_text("x")


def test_snapshot_refuses_an_existing_destination(tmp_path):
    policy = ArtifactPolicyV1((), (), ("skills/**",))
    source = tmp_path / "src"
    _tree(source)
    destination = tmp_path / "snap"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        snapshot_tree(source, policy, destination)


def test_snapshot_refuses_a_source_that_changes_during_the_copy(tmp_path, monkeypatch):
    import evolution.tree.artifacts.artifact_copy as module

    policy = ArtifactPolicyV1((), (), ("skills/**",))
    source = tmp_path / "src"
    _tree(source)
    original = module._copy_entries

    def mutate_then_copy(src, dst, entries):
        original(src, dst, entries)
        (source / "skills" / "SKILL.md").write_text("# changed\n")

    monkeypatch.setattr(module, "_copy_entries", mutate_then_copy)
    with pytest.raises(ArtifactUnsafeError) as info:
        snapshot_tree(source, policy, tmp_path / "snap")
    assert info.value.code == "source_changed"
    assert not (tmp_path / "snap").exists()
