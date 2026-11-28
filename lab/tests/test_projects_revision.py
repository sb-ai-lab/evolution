import os
import subprocess

from evolution_lab.projects.inventory import Entry, collect, diff, inventory_digest
from evolution_lab.projects.models import Implementation, Project, WorkingCopy
from evolution_lab.projects.revision import materials_revision


def _tree(root):
    (root / "lab").mkdir(parents=True)
    (root / "agent").mkdir()
    (root / "lab" / "SPEC.md").write_text("spec\n")
    (root / "agent" / "a.py").write_text("A = 1\n")
    return root


def _project(root, exclude=()):
    return Project("p", "P", "ready", None, "t", None, 1, WorkingCopy(str(root), False, False, None), "lab",
                   tuple(exclude), (Implementation("agent", "agent", "agent", {"kind": "connected"}, None, None),))


def test_collect_prefix_and_rule(tmp_path):
    root = _tree(tmp_path)
    (root / "agent" / "__pycache__").mkdir()
    (root / "agent" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"x")
    (root / "agent" / ".git").write_text("gitdir: elsewhere\n")
    os.symlink("a.py", root / "agent" / "link.py")
    entries = collect(root / "agent", prefix="agent")
    assert [e.path for e in entries] == ["agent", "agent/a.py", "agent/link.py"]
    assert entries[2] == Entry("agent/link.py", "symlink", 0, None, None, "a.py")


def test_collect_of_an_excluded_source_is_empty(tmp_path):
    """ADR-0017 p. 6 at the source boundary (codex r2): a selected ``.git`` or ``*.pyc``
    contributes nothing, like the same entry met as a child; a directory named ``*.pyc`` stays."""
    root = _tree(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "agent" / "m.pyc").write_bytes(b"\x00secret")
    (root / "agent" / "d.pyc").mkdir()
    (root / "agent" / "d.pyc" / "x.txt").write_text("x\n")
    assert collect(root / ".git", prefix=".git") == ()
    assert collect(root / "agent" / "m.pyc", prefix="agent/m.pyc") == ()
    assert [e.path for e in collect(root / "agent" / "d.pyc", prefix="d.pyc")] == ["d.pyc", "d.pyc/x.txt"]


def test_materials_root_git_gives_an_empty_inventory(tmp_path):
    root = _tree(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    project = Project("p", "P", "ready", None, "t", None, 1, WorkingCopy(str(root), False, False, None), ".git",
                      (), (Implementation("agent", "agent", "agent", {"kind": "connected"}, None, None),))
    revision = materials_revision(project)
    assert revision.place_digest("materials_root") == inventory_digest(())
    assert not any(entry.path.startswith(".git") for entry in revision.entries)


def test_a_place_nested_inside_an_excluded_directory_gives_an_empty_inventory(tmp_path):
    """Follow-up to codex r2 (ADR-0017 p. 6, one rule with the Bundle): an excluded ancestor
    excludes the source too — materials_root ".git/hooks", a relative place under
    ".pytest_cache", an absolute place under a ".git" directory. None of their bytes enter."""
    root = _tree(tmp_path / "wc")
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\necho secret\n")
    (root / ".pytest_cache" / "v").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "cache.json").write_text("{}")
    outside = tmp_path / "repo" / ".git" / "x"
    outside.mkdir(parents=True)
    (outside / "a.py").write_text("SECRET = 1\n")
    project = Project("p", "P", "ready", None, "t", None, 1, WorkingCopy(str(root), False, False, None),
                      ".git/hooks", (),
                      (Implementation("cache", "cache", ".pytest_cache/v", {"kind": "connected"}, None, None),
                       Implementation("abs", "abs", str(outside), {"kind": "connected"}, None, None)))
    revision = materials_revision(project)
    assert revision.place_digest("materials_root") == inventory_digest(())
    assert revision.place_digest("implementation", "cache") == inventory_digest(())
    assert revision.place_digest("implementation", "abs") == inventory_digest(())
    assert revision.entries == () and revision.files == 0


def test_a_directory_named_like_bytecode_stays_as_an_ancestor(tmp_path):
    root = _tree(tmp_path / "wc")
    (root / "lab" / "d.pyc" / "sub").mkdir(parents=True)
    (root / "lab" / "d.pyc" / "sub" / "x.txt").write_text("x\n")
    entries = collect(root / "lab" / "d.pyc" / "sub", prefix="lab/d.pyc/sub")
    assert [e.path for e in entries] == ["lab/d.pyc/sub", "lab/d.pyc/sub/x.txt"]


def test_revision_ignores_caches_and_git_but_sees_ignored_data(tmp_path):
    root = _tree(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text("lab/data/\n")
    first = materials_revision(_project(root))
    (root / "agent" / "__pycache__").mkdir()
    (root / "agent" / "__pycache__" / "a.pyc").write_bytes(b"x")
    assert materials_revision(_project(root)).digest == first.digest
    (root / "lab" / "data").mkdir()
    (root / "lab" / "data" / "corpus.jsonl").write_text("{}\n")
    assert materials_revision(_project(root)).digest != first.digest



def test_revision_ignores_a_dot_venv_and_its_symlinks(tmp_path):
    import sys
    root = _tree(tmp_path)
    first = materials_revision(_project(root))
    venv = root / "agent" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert materials_revision(_project(root)).digest == first.digest

def test_project_exclude_applies_to_revision(tmp_path):
    root = _tree(tmp_path)
    first = materials_revision(_project(root, exclude=["lab/tmp/**"]))
    (root / "lab" / "tmp").mkdir()
    (root / "lab" / "tmp" / "x").write_text("1")
    assert materials_revision(_project(root, exclude=["lab/tmp/**"])).digest == first.digest


def test_place_digests_are_independent(tmp_path):
    root = _tree(tmp_path)
    first = materials_revision(_project(root))
    (root / "lab" / "SPEC.md").write_text("spec v2\n")
    second = materials_revision(_project(root))
    assert second.place_digest("implementation", "agent") == first.place_digest("implementation", "agent")
    assert second.place_digest("materials_root") != first.place_digest("materials_root")
    assert second.to_payload()["schema"] == "lab-materials-revision/1"


def test_hash_cache_is_used(tmp_path):
    root = _tree(tmp_path)
    calls = {"store": 0}
    class Cache(dict):
        def lookup(self, path, key):
            return self.get((path, key))
        def store(self, path, key, sha256):
            calls["store"] += 1
            self[(path, key)] = sha256
    cache = Cache()
    materials_revision(_project(root), cache=cache)
    stored = calls["store"]
    materials_revision(_project(root), cache=cache)
    assert stored > 0 and calls["store"] == stored


def test_diff_classifies_rename_modify_add_remove():
    old = (Entry("a.py", "file", 0, 1, "11"), Entry("b.py", "file", 0, 1, "22"), Entry("c.py", "file", 0, 1, "33"))
    new = (Entry("x/a.py", "file", 0, 1, "11"), Entry("b.py", "file", 0, 1, "99"), Entry("d.py", "file", 0, 1, "44"))
    changes = diff(old, new)
    assert changes.renamed == (("a.py", "x/a.py"),)
    assert changes.modified == ("b.py",) and changes.added == ("d.py",) and changes.removed == ("c.py",)
    payload = changes.to_payload()
    assert payload["counts"] == {"renamed": 1, "modified": 1, "added": 1, "removed": 1}
    assert diff(old, old).empty
    assert inventory_digest(old) != inventory_digest(new)


# --- Final-review fix wave ---

def test_git_state_never_takes_optional_locks(tmp_path, monkeypatch):
    """I7: polling the revision must not refresh (write) the team's ``.git/index``."""
    from evolution_lab.projects import revision as revision_module
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(revision_module.subprocess, "run", run)
    assert revision_module.git_state(tmp_path) == {"commit": "", "clean": True}
    assert [argv[:4] for argv in calls] == [["git", "--no-optional-locks", "-C", str(tmp_path)]] * 2
    assert calls[0][4:] == ["rev-parse", "HEAD"]
    assert calls[1][4:] == ["status", "--porcelain", "--untracked-files=normal"]


def test_materials_revision_leaves_a_stat_dirty_index_alone(tmp_path):
    root = _tree(tmp_path)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(argv, cwd=root, check=True, env=env)
    index = root / ".git" / "index"
    before = index.stat().st_mtime_ns
    os.utime(root / "agent" / "a.py", ns=(before + 5_000_000_000, before + 5_000_000_000))  # same bytes, new stat
    from dataclasses import replace
    project = replace(_project(root), working_copy=WorkingCopy(str(root), False, True, None))
    assert materials_revision(project).git["clean"] is True
    assert index.stat().st_mtime_ns == before


def test_git_state_reads_its_own_repository_whatever_git_dir_says(tmp_path, monkeypatch):
    """codex r1 of the dev35 branch (fold-in): an inherited GIT_DIR/GIT_WORK_TREE must not make the revision
    describe another repository than the working copy."""
    from evolution_lab.projects import revision as revision_module
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    heads = {}
    for name in ("wc", "other"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.txt").write_text(name)
        for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-q", "-m", name]):
            subprocess.run(argv, cwd=tmp_path / name, check=True, env=env)
        heads[name] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path / name, check=True, env=env,
                                     capture_output=True, text=True).stdout.strip()
    (tmp_path / "other" / "f.txt").write_text("dirty")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "other"))
    assert revision_module.git_state(tmp_path / "wc") == {"commit": heads["wc"], "clean": True}


def _nested(root, place=".", materials="lab", exclude=()):
    return Project("p", "P", "ready", None, "t", None, 1, WorkingCopy(str(root), False, False, None), materials,
                   tuple(exclude), (Implementation("agent", "agent", place, {"kind": "connected"}, None, None),))


def test_a_nested_materials_root_is_cut_from_the_implementation_place(tmp_path):
    """W84 (spec 2026-10-02 §3.1): writing into materials inside the place leaves the place digest alone; the
    materials place and the overall revision still see it."""
    root = _tree(tmp_path)
    first = materials_revision(_nested(root))
    (root / "lab" / "SPEC.md").write_text("spec v2\n")
    (root / "lab" / "package").mkdir()
    (root / "lab" / "package" / "lab-package.json").write_text("{}\n")
    second = materials_revision(_nested(root))
    assert second.place_digest("implementation", "agent") == first.place_digest("implementation", "agent")
    assert second.place_digest("materials_root") != first.place_digest("materials_root")
    assert second.digest != first.digest
    assert {"lab/SPEC.md", "lab/package/lab-package.json", "agent/a.py"} <= {e.path for e in second.entries}
    (root / "agent" / "a.py").write_text("A = 2\n")
    assert materials_revision(_nested(root)).place_digest("implementation", "agent") != \
        second.place_digest("implementation", "agent")


def test_the_cut_place_digest_is_the_inventory_without_the_materials_subtree(tmp_path):
    root = _tree(tmp_path)
    revision = materials_revision(_nested(root))
    expected = inventory_digest(tuple(e for e in collect(root, prefix="") if e.path != "lab"
                                      and not e.path.startswith("lab/")))
    assert revision.place_digest("implementation", "agent") == expected


def test_the_cut_respects_segment_boundaries(tmp_path):
    root = _tree(tmp_path)
    first = materials_revision(_nested(root))
    (root / "lab2").mkdir()
    (root / "lab2" / "x.txt").write_text("x\n")
    assert materials_revision(_nested(root)).place_digest("implementation", "agent") != \
        first.place_digest("implementation", "agent")


def test_equal_paths_and_a_place_inside_the_materials_keep_the_whole_subtree(tmp_path):
    """Only materials strictly inside the place are cut (user decision 2026-10-02, option A)."""
    root = _tree(tmp_path)
    same = _nested(root, place="lab", materials="lab")
    before = materials_revision(same).place_digest("implementation", "agent")
    (root / "lab" / "SPEC.md").write_text("spec v2\n")
    assert materials_revision(same).place_digest("implementation", "agent") != before
    (root / "lab" / "agent").mkdir()
    (root / "lab" / "agent" / "b.py").write_text("B = 1\n")
    inner = _nested(root, place="lab/agent", materials="lab")
    materials_before = materials_revision(inner).place_digest("materials_root")
    (root / "lab" / "agent" / "b.py").write_text("B = 2\n")
    assert materials_revision(inner).place_digest("materials_root") != materials_before


def test_an_absolute_place_containing_the_materials_is_cut_too(tmp_path):
    root = _tree(tmp_path / "repo")
    project = _nested(root / "lab" / "..", place=str(root))  # an unnormalised working copy path, an absolute place
    first = materials_revision(project)
    (root / "lab" / "SPEC.md").write_text("spec v2\n")
    second = materials_revision(project)
    assert second.place_digest("implementation", "agent") == first.place_digest("implementation", "agent")
    assert any(e.path.startswith("@agent/") for e in second.entries)


def test_exclude_still_applies_before_the_cut(tmp_path):
    root = _tree(tmp_path)
    first = materials_revision(_nested(root, exclude=["agent/tmp/**"]))
    (root / "agent" / "tmp").mkdir()
    (root / "agent" / "tmp" / "x").write_text("1")
    assert materials_revision(_nested(root, exclude=["agent/tmp/**"])).place_digest("implementation", "agent") == \
        first.place_digest("implementation", "agent")
