import os
import stat
from pathlib import PurePosixPath

import pytest

from evolution.tree.artifacts.artifact_errors import ArtifactUnsafeError
from evolution.tree.artifacts.artifact_policy import (
    DEFAULT_EXCLUDES,
    EMPTY_DEPENDENCY_DIGEST,
    ArtifactPolicyV1,
    changes,
    matches,
    scan_tree,
)


def write(root, relative, data=b"x", executable=False):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def seed(tmp_path):
    root = tmp_path / "seed"
    write(root, "skills/SKILL.md", b"skill")
    write(root, "scripts/run.sh", b"#!/bin/sh", executable=True)
    write(root, "uv.lock", b"lock")
    write(root, ".env", b"SECRET=1")
    write(root, "src/__pycache__/x.pyc", b"pyc")
    write(root, "deep/node_modules/m/index.js", b"js")
    return root


def policy(**kw):
    base = dict(
        excludes=("build/**",), dependency_paths=("uv.lock",), writable_paths=("skills/**",)
    )
    base.update(kw)
    return ArtifactPolicyV1(**base)


def test_pattern_language():
    assert matches("node_modules", PurePosixPath("deep/node_modules"), True)
    assert matches("*.log", PurePosixPath("a/b/c.log"), False)
    assert matches("build/**", PurePosixPath("build/out/x"), False)
    assert not matches("build/**", PurePosixPath("src/build/x"), False)
    assert matches("skills/*/SKILL.md", PurePosixPath("skills/t/SKILL.md"), False)
    assert not matches("skills/*/SKILL.md", PurePosixPath("skills/t/u/SKILL.md"), False)
    # An anchored pattern matches the directory itself, not only its contents.
    assert matches("build/**", PurePosixPath("build"), True)


@pytest.mark.parametrize("bad", ["../x", "/abs", "a//b", "!neg", ""])
def test_policy_rejects_bad_patterns(bad):
    with pytest.raises(ValueError):
        policy(excludes=(bad,))


def test_policy_rejects_dependency_or_writable_conflicts():
    with pytest.raises(ValueError):
        policy(dependency_paths=(".env",))
    with pytest.raises(ValueError):
        policy(writable_paths=("uv.lock",))
    with pytest.raises(ValueError):
        policy(writable_paths=("*.log",))


def test_policy_digest_and_roundtrip():
    p = policy()
    assert ArtifactPolicyV1.from_payload(p.to_payload()) == p
    assert p.digest == policy().digest and p.digest != policy(excludes=()).digest
    assert set(DEFAULT_EXCLUDES) <= set(p.effective_excludes)


def test_scan_membership_and_digests(tmp_path):
    root = seed(tmp_path)
    scan = scan_tree(root, policy())
    paths = [e.path for e in scan.artifact]
    assert "skills/SKILL.md" in paths and "scripts/run.sh" in paths
    assert not any(p.startswith((".env", "src/__pycache__", "deep/node_modules")) for p in paths)
    assert [e.path for e in scan.dependency] == ["uv.lock"]
    assert scan.file_count == 3
    assert scan.byte_count == len(b"skill") + len(b"#!/bin/sh") + len(b"lock")
    assert scan_tree(root, policy(dependency_paths=())).dependency_digest == EMPTY_DEPENDENCY_DIGEST


def test_digest_ignores_mtime_mode_and_order_but_not_exec_or_bytes(tmp_path):
    a = seed(tmp_path / "a")
    b = seed(tmp_path / "b")
    os.utime(b / "skills/SKILL.md", (1, 1))
    (b / "skills/SKILL.md").chmod(0o444)
    assert scan_tree(a, policy()).candidate_digest == scan_tree(b, policy()).candidate_digest
    (b / "scripts/run.sh").chmod(0o644)
    assert scan_tree(a, policy()).candidate_digest != scan_tree(b, policy()).candidate_digest
    write(a, "skills/SKILL.md", b"other")
    assert (
        scan_tree(a, policy()).candidate_digest
        != scan_tree(seed(tmp_path / "c"), policy()).candidate_digest
    )


@pytest.mark.parametrize(
    "make,code",
    [
        (lambda r: os.symlink("skills/SKILL.md", r / "skills/link"), "symlink"),
        (lambda r: os.mkfifo(r / "skills/fifo"), "special_file"),
        (lambda r: os.link(r / "skills/SKILL.md", r / "skills/alias"), "hard_link"),
        (lambda r: (r / "uv.lock").unlink(), "dependency_missing"),
    ],
)
def test_scan_rejects_unsafe_trees(tmp_path, make, code):
    root = seed(tmp_path)
    make(root)
    with pytest.raises(ArtifactUnsafeError) as info:
        scan_tree(root, policy())
    assert info.value.code == code


def test_unsafe_entries_inside_excluded_regions_are_ignored(tmp_path):
    """A real seed has a `.venv/bin/python` symlink and hard links under
    `node_modules`; scanning into them would make every real tree unsafe and the
    default exclusions inert. Neither entry is in either set, so neither digest
    moves.
    """
    root = seed(tmp_path)
    before = scan_tree(root, policy())
    (root / ".venv/bin").mkdir(parents=True)
    os.symlink("python3.11", root / ".venv/bin/python")
    os.mkfifo(root / ".venv/socket")
    os.link(root / "deep/node_modules/m/index.js", root / "deep/node_modules/m/alias.js")
    after = scan_tree(root, policy())
    assert after.candidate_digest == before.candidate_digest
    assert after.dependency_digest == before.dependency_digest
    assert (after.file_count, after.byte_count) == (before.file_count, before.byte_count)


def test_hard_link_from_an_excluded_dir_into_the_artifact_set_is_unsafe(tmp_path):
    """Containment does not depend on scanning excluded regions: the alias is
    caught through the included file's own `st_nlink`.
    """
    root = seed(tmp_path)
    os.link(root / "skills/SKILL.md", root / "deep/node_modules/m/alias.md")
    with pytest.raises(ArtifactUnsafeError) as info:
        scan_tree(root, policy())
    assert info.value.code == "hard_link"


def test_hard_link_to_outside_file_is_unsafe(tmp_path):
    root = seed(tmp_path)
    outside = write(tmp_path / "outside", "secret.txt", b"outside")
    os.link(outside, root / "skills/alias.txt")
    with pytest.raises(ArtifactUnsafeError) as info:
        scan_tree(root, policy())
    assert info.value.code == "hard_link"


def test_dependency_path_that_is_a_directory_is_unsafe(tmp_path):
    root = seed(tmp_path)
    (root / "uv.lock").unlink()
    (root / "uv.lock").mkdir()
    with pytest.raises(ArtifactUnsafeError) as info:
        scan_tree(root, policy())
    assert info.value.code == "dependency_missing"


def test_changes_detects_create_modify_delete_and_exec(tmp_path):
    parent = seed(tmp_path / "p")
    child = seed(tmp_path / "c")
    write(child, "skills/new/SKILL.md", b"new")
    write(child, "skills/SKILL.md", b"changed")
    (child / "scripts/run.sh").unlink()
    got = changes(scan_tree(parent, policy()), scan_tree(child, policy()), parent, child)
    assert got == ("scripts/run.sh", "skills/SKILL.md", "skills/new", "skills/new/SKILL.md")


def test_unreadable_included_directory_fails_the_scan(tmp_path):
    """An included directory that cannot be read must fail the walk, not be skipped:
    `os.walk` would otherwise yield a smaller tree whose digests claim to be the
    exact identity of a root that was never fully read.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 directory, so the failure cannot be provoked")
    root = seed(tmp_path)
    write(root, "skills/private/secret.md", b"private")
    private = root / "skills/private"
    private.chmod(0o000)
    try:
        with pytest.raises(OSError):
            scan_tree(root, policy())
    finally:
        private.chmod(0o700)
    scan = scan_tree(root, policy())
    assert "skills/private/secret.md" in {entry.path for entry in scan.artifact}
