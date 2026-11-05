"""The pure helpers of the preview rehearsal script.

The rehearsal itself builds a wheel and creates virtual environments, so it
is run by hand as part of the whole-change gate (spec §5.2). What a test can
hold still is the arithmetic the rehearsal's claims rest on: the fingerprint
that backs the containment and rollback checks, the root refusal, the parse
of `--help` into a command set, and the stop helper that has to take a
child's descendants with it.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v2_preview_rehearsal", ROOT / "scripts" / "v2_preview_rehearsal.py"
)
assert SPEC and SPEC.loader
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)


def _tree(root: Path) -> None:
    (root / "a").mkdir()
    (root / "a" / "one.txt").write_text("one", encoding="utf-8")
    (root / "two.txt").write_text("two", encoding="utf-8")
    os.symlink("two.txt", root / "link")


def test_process_log_summary_reads_one_private_log(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    path = logs / "evolve-20261014T153012Z-48211.log"
    path.write_text(
        '2026-10-14T15:30:12.000Z stdout   "terminal": "finished",\n'
        "2026-10-14T15:30:13.000Z log exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert rehearsal.process_log_summary(tmp_path) == {
        "count": 1,
        "directory_mode": 0o700,
        "file_mode": 0o600,
        "exit_0": True,
        "finished_stdout": True,
    }


@pytest.mark.parametrize(
    "problem", ["missing", "multiple", "directory_mode", "file_mode", "exit", "terminal"]
)
def test_process_log_summary_exposes_invalid_rehearsal_evidence(
    tmp_path: Path, problem: str
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    path = logs / "evolve-20261014T153012Z-48211.log"
    path.write_text(
        '2026-10-14T15:30:12.000Z stdout   "terminal": "finished",\n'
        "2026-10-14T15:30:13.000Z log exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    if problem == "missing":
        path.unlink()
    elif problem == "multiple":
        (logs / "evolve-20261014T153014Z-48212.log").write_text("another")
    elif problem == "directory_mode":
        logs.chmod(0o755)
    elif problem == "file_mode":
        path.chmod(0o644)
    elif problem == "exit":
        path.write_text(path.read_text().replace("exit 0", "exit 1"))
    else:
        path.write_text(path.read_text().replace("finished", "suspended"))
    summary = rehearsal.process_log_summary(tmp_path)
    if problem in ("missing", "multiple"):
        assert summary["count"] == (0 if problem == "missing" else 2)
        assert summary["exit_0"] is False and summary["finished_stdout"] is False
    elif problem.endswith("mode"):
        assert summary[problem] == (0o755 if problem == "directory_mode" else 0o644)
    else:
        assert summary["exit_0" if problem == "exit" else "finished_stdout"] is False


def test_fingerprint_is_deterministic_and_equal_for_equal_trees(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    _tree(left)
    _tree(right)

    assert rehearsal.fingerprint(left) == rehearsal.fingerprint(left)
    assert rehearsal.fingerprint(left) == rehearsal.fingerprint(right)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: (r / "two.txt").write_text("tw0", encoding="utf-8"), id="byte"),
        pytest.param(lambda r: (r / "three.txt").write_text("", encoding="utf-8"), id="added"),
        pytest.param(lambda r: (r / "a" / "one.txt").unlink(), id="removed"),
        pytest.param(lambda r: (r / "empty").mkdir(), id="empty-dir"),
        pytest.param(lambda r: os.mkfifo(r / "fifo"), id="fifo"),
        pytest.param(lambda r: (r / "two.txt").chmod(0o755), id="mode"),
        pytest.param(lambda r: (os.unlink(r / "link"), os.symlink("a", r / "link")), id="link"),
    ],
)
def test_fingerprint_changes_on_every_observed_mutation(tmp_path: Path, mutate) -> None:
    _tree(tmp_path)
    before = rehearsal.fingerprint(tmp_path)

    mutate(tmp_path)

    assert rehearsal.fingerprint(tmp_path) != before


def test_fingerprint_ignores_bytecode_caches_and_named_exclusions(tmp_path: Path) -> None:
    _tree(tmp_path)
    before = rehearsal.fingerprint(tmp_path, exclude=("runs",))

    (tmp_path / "a" / "__pycache__").mkdir()
    (tmp_path / "a" / "__pycache__" / "one.cpython-311.pyc").write_bytes(b"\x00")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "campaign.jsonl").write_text("{}", encoding="utf-8")

    assert rehearsal.fingerprint(tmp_path, exclude=("runs",)) == before
    assert rehearsal.fingerprint(tmp_path) != before


def test_fingerprint_exclusion_is_top_level_only(tmp_path: Path) -> None:
    _tree(tmp_path)
    before = rehearsal.fingerprint(tmp_path, exclude=("runs",))

    (tmp_path / "a" / "runs").mkdir()
    (tmp_path / "a" / "runs" / "campaign.jsonl").write_text("{}", encoding="utf-8")

    assert rehearsal.fingerprint(tmp_path, exclude=("runs",)) != before


def test_fingerprint_root_sees_a_stray_entry_directly_under_root_but_not_report_json(
    tmp_path: Path,
) -> None:
    for name in ("control", "home", "tmp", "cwd", "preview", "dist"):
        (tmp_path / name).mkdir()
    (tmp_path / "demo").mkdir()
    before = rehearsal.fingerprint_root(tmp_path)

    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    assert rehearsal.fingerprint_root(tmp_path) == before

    (tmp_path / "stray").write_text("", encoding="utf-8")
    assert rehearsal.fingerprint_root(tmp_path) != before


def test_root_refusal_names_the_checkout_and_the_non_empty_directory(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    outside = tmp_path / "outside"

    assert rehearsal.refuse_root(checkout / "src" / "root", checkout) == "root_inside_checkout"
    assert rehearsal.refuse_root(checkout, checkout) == "root_inside_checkout"
    assert rehearsal.refuse_root(outside, checkout) is None
    outside.mkdir()
    assert rehearsal.refuse_root(outside, checkout) is None
    (outside / "stale").write_text("", encoding="utf-8")
    assert rehearsal.refuse_root(outside, checkout) == "root_not_empty"

    a_file = tmp_path / "outside-file"
    a_file.write_text("", encoding="utf-8")
    assert rehearsal.refuse_root(a_file, checkout) == "root_not_a_directory"


def test_root_refusal_resolves_a_relative_root_against_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "nested" / "stale").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert rehearsal.refuse_root(Path("outside/nested"), checkout) == "root_not_empty"


def test_canonical_arguments_resolves_root_and_wheel_and_a_pathlike_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wheel-dir").mkdir()
    (tmp_path / "wheel-dir" / "thing.whl").write_text("", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    monkeypatch.chdir(tmp_path)

    root, wheel, python = rehearsal.canonical_arguments(
        Path("preview-run"), Path("wheel-dir/thing.whl"), "bin/python3"
    )

    assert root == tmp_path / "preview-run"
    assert wheel == tmp_path / "wheel-dir" / "thing.whl"
    assert python == str(tmp_path / "bin" / "python3")


def test_canonical_arguments_leaves_a_bare_python_and_a_missing_wheel_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    root, wheel, python = rehearsal.canonical_arguments(Path("run"), None, "python3")

    assert root == tmp_path / "run"
    assert wheel is None
    assert python == "python3"


def test_parse_commands_reads_the_commands_section_of_click_help() -> None:
    help_text = (
        "Usage: evo [OPTIONS] COMMAND [ARGS]...\n\n"
        "  Evolution: trace-driven skill lifecycle for LLM agents.\n\n"
        "Options:\n  --version  Show version and exit.\n  --help     Show this message and exit.\n\n"
        "Commands:\n  init   Initialize evolution tracking.\n"
        "  tree   Evolve a local service through immutable Candidate\n"
        "         snapshots.\n  split  Inspect train/test split manifests.\n"
    )

    assert rehearsal.parse_commands(help_text) == {"init", "tree", "split"}
    assert rehearsal.parse_commands("Usage: evo\n\nOptions:\n  --help\n") == set()


def test_stop_group_takes_the_grandchild_with_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.pid"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            f"open({str(marker)!r}, 'w').write(str(g.pid))\n"
            "time.sleep(600)\n",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(200):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.05)
    grandchild = int(marker.read_text())

    code = rehearsal.stop_group(child, grace=2.0)

    assert child.poll() is not None and code == child.returncode
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"grandchild {grandchild} survived stop_group")


def test_read_ready_line_times_out_stops_the_group_and_raises_failure() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )

    try:
        with pytest.raises(rehearsal.Failure):
            rehearsal.read_ready_line(process, 0.5)

        for _ in range(100):
            if process.poll() is not None:
                break
            time.sleep(0.05)
        assert process.poll() is not None
    finally:
        rehearsal.stop_group(process, grace=1.0)


class _RaisingStdout:
    def readline(self) -> str:
        raise KeyboardInterrupt


class _FakeProcess:
    """Enough of `Popen[str]` for `read_ready_line`'s `BaseException` path."""

    def __init__(self) -> None:
        self.pid = os.getpid()  # a real, already-running process; never signalled here
        self.returncode: int | None = None
        self.stdout = _RaisingStdout()

    def poll(self) -> int | None:
        return self.returncode


def test_read_ready_line_stops_the_group_and_reraises_on_any_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    stopped: list[object] = []
    monkeypatch.setattr(rehearsal, "stop_group", lambda p, **kw: stopped.append(p))

    with pytest.raises(KeyboardInterrupt):
        rehearsal.read_ready_line(process, 5.0)  # type: ignore[arg-type]

    assert stopped == [process]


def test_boundary_env_redirects_every_implicit_location_under_root(tmp_path: Path) -> None:
    env = rehearsal.boundary_env(tmp_path)

    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        assert Path(env[name]).is_relative_to(tmp_path), name
    assert "PYTHONPATH" not in env
    assert env["PATH"] == os.environ["PATH"]


def test_set_port_rewrites_only_the_base_url(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"schema": "evolution-v2-manifest/2", "target": {"base_url": "http://127.0.0.1:8765", '
        '"seed_mode": "exact"}, "seed_root": "seed"}\n',
        encoding="utf-8",
    )

    rehearsal.set_port(manifest, 40123)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["target"] == {"base_url": "http://127.0.0.1:40123", "seed_mode": "exact"}
    assert payload["seed_root"] == "seed"
    assert stat.S_ISREG(manifest.stat().st_mode)


def test_rehearsal_has_no_retired_scaffold_or_product_import_dependency() -> None:
    source = (ROOT / "scripts" / "v2_preview_rehearsal.py").read_text(encoding="utf-8")
    assert "scaffold_v13" not in source
    assert "load_v13_sample" not in source
    assert "v14_from_v13_sample" not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert all(name.split(".")[0] not in {"evolution", "tests", "runpy"} for name in names)


def test_rehearsals_retired_schema_document_is_refused_without_writes(tmp_path: Path) -> None:
    from evolution.tree.reference_target_cli import scaffold

    result = scaffold(tmp_path / "demo")
    document = json.loads(result.manifest.read_text(encoding="utf-8"))
    original = json.loads(json.dumps(document))
    refused = rehearsal.retired_schema_manifest(document)
    assert refused == {**original, "schema": "evolution-v2-manifest/14"}
    assert document == original
    refused["target"]["base_url"] = "http://127.0.0.1:1"
    assert document == original, "the helper must not share nested mutable fields"

    path = result.manifest.parent / "manifest-14.json"
    path.write_text(json.dumps(refused), encoding="utf-8")
    run_dir = result.manifest.parent / "runs" / "refused-14"
    before = rehearsal.fingerprint(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    completed = rehearsal.run_step(
        [
            sys.executable,
            "-c",
            "from evolution.cli.main import app; app()",
            "tree",
            "evolve",
            "--manifest",
            str(path),
            "--run-dir",
            str(run_dir),
        ],
        env=env,
        cwd=tmp_path,
        timeout=30,
        what="evo tree evolve (/14 checkout regression)",
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "evo tree evolve: refused: manifest_schema at schema\n"
        "evo tree evolve: expected evolution-v2-manifest/15 or /16\n"
        "evo tree evolve: class: hard\n"
    )
    assert not run_dir.exists()
    assert rehearsal.fingerprint(tmp_path) == before


def test_the_rehearsals_successor_manifest_loads_under_the_scaffolds_schema(tmp_path: Path) -> None:
    """The guard two lagged checks needed (D2b rehearsal, 2026-09-20).

    Checks 14 and 15 write a successor manifest beside the scaffolded one and
    hand it to the installed `evo`. Nothing proved that what they write is a
    document the current schema accepts, so check 14 went on writing `/5`'s
    bare-string `linked_from` after the scaffold moved to `/6`, where it would
    have been refused `bad_type` before the run. The scaffold has since moved
    to `/7` (W19), which is why this test's name says "the scaffold's schema"
    rather than naming one: the body loads whatever `scaffold` writes and is
    the thing that must not lag it. The script still imports nothing from
    `evolution`; this test does the importing for it.
    """

    from evolution.tree.manifest.manifest_v2 import load_manifest_v2
    from evolution.tree.reference_target_cli import scaffold

    result = scaffold(tmp_path / "demo")
    document = json.loads(result.manifest.read_text(encoding="utf-8"))
    run_dir = tmp_path / "demo" / "runs" / "run-1"
    champion_path = "artifacts/champions/v1"
    champion = run_dir / champion_path
    champion.mkdir(parents=True)
    (champion / "seed.txt").write_text("champion", encoding="utf-8")

    for baseline in ("fresh", "inherit"):
        successor = rehearsal.successor_manifest(
            document, run_dir=run_dir, champion_path=champion_path, baseline=baseline
        )
        path = result.manifest.parent / f"manifest-{baseline}.json"
        path.write_text(json.dumps(successor, indent=2) + "\n", encoding="utf-8")

        manifest = load_manifest_v2(path)

        assert manifest.linked_from == str(run_dir)
        assert manifest.linked_baseline == baseline
        assert manifest.schema == document["schema"]
        assert manifest.schema == "evolution-v2-manifest/15"
    assert "linked_from" not in document, "the helper must not mutate the caller's document"
