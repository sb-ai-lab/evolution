"""Compact-storage redesign units: volume tripwire, trace retention, state archive."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution.core.models import SkillTrace
from evolution.eval.state_archive import find_store_archive, pack_state_tree, unpack_state_tree
from evolution.eval.volume_guard import (
    InodeFloorError,
    assert_inode_headroom,
    free_inodes,
    preflight_inodes,
)
from evolution.lineage.traces import TraceStore

# ── volume_guard ──────────────────────────────────────────────────────────────


def test_preflight_raises_below_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    free = free_inodes(tmp_path)
    assert free is not None and free > 0
    monkeypatch.setenv("EVO_MIN_FREE_INODES", str(free + 1_000_000))
    with pytest.raises(InodeFloorError, match="EVO_MIN_FREE_INODES"):
        preflight_inodes(tmp_path)


def test_preflight_passes_and_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVO_MIN_FREE_INODES", "1")
    assert preflight_inodes(tmp_path) is not None
    monkeypatch.setenv("EVO_MIN_FREE_INODES", "0")
    assert preflight_inodes(tmp_path) is not None
    assert preflight_inodes(tmp_path / "does-not-exist") is None


def test_headroom_aborts_below_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    free = free_inodes(tmp_path)
    assert free is not None
    monkeypatch.setenv("EVO_ABORT_FREE_INODES", str(free + 1_000_000))
    with pytest.raises(InodeFloorError, match="EVO_ABORT_FREE_INODES"):
        assert_inode_headroom(tmp_path, context="test")
    monkeypatch.setenv("EVO_ABORT_FREE_INODES", "1")
    assert assert_inode_headroom(tmp_path) is not None


# ── TraceStore retention ──────────────────────────────────────────────────────


def _trace(i: int) -> SkillTrace:
    return SkillTrace(
        id=f"t{i:03d}",
        skill_name="s",
        skill_version="v1",
        model="m",
        task="task",
        timestamp=datetime(2026, 7, 1, 12, 0, i),
    )


def _store(tmp_path: Path) -> TraceStore:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    return TraceStore(SimpleNamespace(path=skill_dir))


def test_trace_retention_prunes_oldest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVO_TRACE_RETENTION", "3")
    store = _store(tmp_path)
    for i in range(5):
        store.save(_trace(i), prompt="p", response="r", code="c")

    index = store.list()
    assert [t["id"] for t in index] == ["t002", "t003", "t004"]
    assert not (store.traces_dir / "t000").exists()
    assert not (store.traces_dir / "t001").exists()
    assert (store.traces_dir / "t004" / "prompt.md").read_text() == "p"


def test_trace_retention_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVO_TRACE_RETENTION", raising=False)
    store = _store(tmp_path)
    for i in range(5):
        store.save(_trace(i), prompt="p", response="r", code="c")
    assert len(store.list()) == 5
    assert (store.traces_dir / "t000").exists()


# ── state_archive (memento version snapshots) ─────────────────────────────────


def _make_memento_store(root: Path) -> Path:
    store = root / "native_state" / "memento"
    ms = store / "memento_s"
    (ms / "db").mkdir(parents=True)
    (ms / "db" / "memento_s.db").write_bytes(b"\x00sqlite-bytes\x01" * 20)
    (ms / "context" / "sessions" / "abc").mkdir(parents=True)
    (ms / "context" / "sessions" / "abc" / "artifact.txt").write_text("ctx", encoding="utf-8")
    (ms / "config.json").write_text('{"k":1}', encoding="utf-8")
    (ms / "skills" / "skill-creator" / "scripts").mkdir(parents=True)
    (ms / "skills" / "skill-creator" / "scripts" / "init.py").write_text("x=1", encoding="utf-8")
    ws = ms / "workspace" / "d"
    ws.mkdir(parents=True)
    (ws / "SKILL.md").write_text("evolved body", encoding="utf-8")
    (store / "documents.json").write_text('[{"text":"doc"}]', encoding="utf-8")
    return store


def _tree_digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_pack_unpack_restores_byte_identical_store(tmp_path: Path) -> None:
    store = _make_memento_store(tmp_path)
    version_dir = tmp_path / "native_state_versions" / "memento" / "v3"

    archive = pack_state_tree(store, version_dir)

    assert archive is not None and archive == find_store_archive(version_dir)
    # loose reader-set for the GUI diff / export, matching its glob shapes
    assert (version_dir / "memento_s" / "workspace" / "d" / "SKILL.md").read_text() == (
        "evolved body"
    )
    assert (version_dir / "documents.json").is_file()
    # the heavy store internals are NOT loose
    assert not (version_dir / "memento_s" / "db").exists()

    ref = tmp_path / "ref_restore"
    shutil.copytree(store, ref)
    restored = tmp_path / "restored"
    assert unpack_state_tree(version_dir, restored)
    assert _tree_digest(restored) == _tree_digest(ref)


def test_unpack_legacy_full_copy_dir(tmp_path: Path) -> None:
    store = _make_memento_store(tmp_path)
    legacy_version = tmp_path / "native_state_versions" / "memento" / "v1"
    shutil.copytree(store, legacy_version)

    restored = tmp_path / "restored_legacy"
    assert unpack_state_tree(legacy_version, restored)
    assert _tree_digest(restored) == _tree_digest(store)


def test_pack_inode_win(tmp_path: Path) -> None:
    store = _make_memento_store(tmp_path)
    version_dir = tmp_path / "v_new"
    pack_state_tree(store, version_dir)
    packed = sum(1 for p in version_dir.rglob("*") if p.is_file())
    full = sum(1 for p in store.rglob("*") if p.is_file())
    assert packed < full


# ── shared guard scaffolding ──────────────────────────────────────────────────


def _guarded_env_for(ws: Path) -> dict[str, str]:
    from evolution.eval.workspace_guard import build_guarded_env

    ws.mkdir(parents=True, exist_ok=True)
    return build_guarded_env(
        framework="evolution",
        workspace_dir=ws,
        run_home=ws / "_solve_home",
        audit_log_path=ws / ".workspace_guard.jsonl",
        base_env={"PATH": "/usr/bin"},
        set_home=True,
        command_wrappers=True,
    )


def test_shared_guard_built_once_and_pure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared_root = tmp_path / "run" / "_sharedguard"
    monkeypatch.setenv("EVO_SHARED_GUARD_DIR", str(shared_root))

    ws_a = tmp_path / "cell_a" / "leaf"
    ws_b = tmp_path / "cell_b" / "leaf"
    env_a = _guarded_env_for(ws_a)
    env_b = _guarded_env_for(ws_b)

    bin_dir = shared_root / "bin"
    py_dir = shared_root / "py"
    for env in (env_a, env_b):
        assert env["PATH"].split(":")[0] == str(bin_dir)
        assert env["PYTHONPATH"].split(":")[0] == str(py_dir)
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    # per-solve enforcement stays env-carried, never baked into shared bytes
    assert (
        env_a["EVOLUTION_WORKSPACE_GUARD_WORKSPACE"]
        != (env_b["EVOLUTION_WORKSPACE_GUARD_WORKSPACE"])
    )
    for leaf_ws in (ws_a, ws_b):
        assert not (leaf_ws / "_workspace_guard_bin").exists()
        assert not (leaf_ws / "_workspace_guard_py").exists()
    shim_files = sorted(p for p in shared_root.rglob("*") if p.is_file())
    assert len([p for p in shim_files if p.parent == bin_dir]) == 15
    assert (py_dir / "sitecustomize.py").is_file()
    for shim in shim_files:
        text = shim.read_text(encoding="utf-8", errors="replace")
        assert str(tmp_path) not in text
    assert not list(py_dir.rglob("*.pyc"))


def test_shared_guard_fallback_per_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVO_SHARED_GUARD_DIR", raising=False)
    ws = tmp_path / "leaf"
    env = _guarded_env_for(ws)
    assert (ws / "_workspace_guard_bin").is_dir()
    assert (ws / "_workspace_guard_py" / "sitecustomize.py").is_file()
    assert env["PATH"].split(":")[0] == str(ws / "_workspace_guard_bin")


# ── corpus base: hardlink staging ─────────────────────────────────────────────


def _write_base_task(task_dir: Path, *, datagen: str | None = None) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text('[task]\nname = "tiny"\n', encoding="utf-8")
    (task_dir / "instruction.md").write_text("do it", encoding="utf-8")
    env_data = task_dir / "environment" / "data"
    env_data.mkdir(parents=True)
    (env_data / "input.csv").write_text("a,b\n1,2\n" * 200, encoding="utf-8")
    (env_data / "messages.db").write_bytes(b"\x00fakedb\x01" * 16)
    (task_dir / "solve.sh").write_text(
        "#!/usr/bin/env bash\npython3 solution.py\n", encoding="utf-8"
    )
    if datagen is not None:
        (task_dir / "data_generator.py").write_text(datagen, encoding="utf-8")


def _enable_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_name: str) -> Path:
    base_root = tmp_path / "_base"
    census = tmp_path / "census.json"
    census.write_text(
        f'{{"tasks": {{"{task_name}": {{"writes": []}}}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("EVO_CORPUS_BASE_ROOT", str(base_root))
    monkeypatch.setenv("EVO_CORPUS_WRITE_CENSUS", str(census))
    return base_root


def _walk_bytes(root: Path) -> dict[str, bytes]:
    import os as _os

    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in _os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def test_staged_workspace_matches_copied_read_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.eval.workspace_materializer import materialize_workspace

    task = tmp_path / "taskA"
    _write_base_task(
        task,
        datagen='from pathlib import Path\nPath("environment/data/gen.csv").write_text("g\\n1\\n")\n',
    )

    monkeypatch.delenv("EVO_CORPUS_BASE_ROOT", raising=False)
    monkeypatch.delenv("EVO_CORPUS_WRITE_CENSUS", raising=False)
    ws_copy = tmp_path / "ws_copy"
    materialize_workspace(task, ws_copy, contract="agent")

    base_root = _enable_base(tmp_path, monkeypatch, "taskA")
    ws_link = tmp_path / "ws_link"
    materialize_workspace(task, ws_link, contract="agent")

    copied = _walk_bytes(ws_copy)
    staged = _walk_bytes(ws_link)
    assert staged == copied
    base_dir = base_root / "agent" / "taskA"
    assert (base_dir / "manifest.json").is_file()

    # shipped corpus files share the base inode; generated/db files are copies
    linked = ws_link / "environment" / "data" / "input.csv"
    assert linked.stat().st_ino == (base_dir / "environment" / "data" / "input.csv").stat().st_ino
    for private_rel in ("environment/data/messages.db", "gen.csv", "solve.sh"):
        private = ws_link / private_rel
        assert private.stat().st_nlink == 1, private_rel


def test_core_base_staging_scrubs_oracle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evolution.eval.workspace_materializer import materialize_workspace

    task = tmp_path / "taskCore"
    _write_base_task(task)
    (task / "tests").mkdir()
    (task / "tests" / "test_outputs.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (task / ".ground_truth.json").write_text("oracle", encoding="utf-8")
    (task / "solution.py").write_text("# oracle\n", encoding="utf-8")
    base_root = _enable_base(tmp_path, monkeypatch, "taskCore")

    ws = tmp_path / "ws_core"
    materialize_workspace(task, ws, contract="core")

    assert (base_root / "core" / "taskCore" / "tests" / "test_outputs.py").is_file()
    assert (ws / "environment" / "data" / "input.csv").is_file()
    assert not (ws / "tests").exists()
    assert not (ws / ".ground_truth.json").exists()
    assert not (ws / "solution.py").exists()


def test_staged_db_write_does_not_poison_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.eval.corpus_base import _sha256_file
    from evolution.eval.workspace_materializer import materialize_workspace

    task = tmp_path / "taskB"
    _write_base_task(task)
    base_root = _enable_base(tmp_path, monkeypatch, "taskB")
    ws = tmp_path / "ws"
    materialize_workspace(task, ws, contract="agent")

    base_db = base_root / "agent" / "taskB" / "environment" / "data" / "messages.db"
    before = _sha256_file(base_db)
    (ws / "environment" / "data" / "messages.db").write_bytes(b"MUTATED IN PLACE")
    assert _sha256_file(base_db) == before


def test_unvetted_task_falls_back_to_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evolution.eval.workspace_materializer import materialize_workspace

    task = tmp_path / "taskC"
    _write_base_task(task)
    base_root = _enable_base(tmp_path, monkeypatch, "someOtherTask")
    ws = tmp_path / "ws"
    materialize_workspace(task, ws, contract="agent")
    assert (ws / "environment" / "data" / "input.csv").stat().st_nlink == 1
    assert not (base_root / "agent" / "taskC").exists()


def test_nondeterministic_datagen_refuses_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.eval.workspace_materializer import materialize_workspace

    task = tmp_path / "taskD"
    _write_base_task(
        task,
        datagen=(
            "import os\nfrom pathlib import Path\n"
            'Path("environment/data/gen.bin").write_bytes(os.urandom(8))\n'
        ),
    )
    base_root = _enable_base(tmp_path, monkeypatch, "taskD")
    ws = tmp_path / "ws"
    materialize_workspace(task, ws, contract="agent")
    assert (base_root / "agent" / ".taskD.nondeterministic").is_file()
    assert not (base_root / "agent" / "taskD").exists()
    assert (ws / "environment" / "data" / "input.csv").stat().st_nlink == 1


def test_rmtree_counted_clears_readonly_tree(tmp_path: Path) -> None:
    import os as _os

    from evolution.eval.trace_cleanup import _rmtree_counted

    tree = tmp_path / "ro"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("x", encoding="utf-8")
    _os.chmod(tree / "sub", 0o500)
    _os.chmod(tree, 0o500)
    assert _rmtree_counted(tree)
    assert not tree.exists()
