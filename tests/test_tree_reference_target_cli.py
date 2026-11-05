from __future__ import annotations

import asyncio
import errno
import http.client
import importlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import evolution.tree as tree_package
import evolution.tree.reference_target_cli as cli_module
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifacts import ExpectedAttestation
from evolution.tree.execution.interaction import RolloutInfrastructureError
from evolution.tree.manifest.manifest_v2 import load_manifest_v2
from evolution.tree.reference_target_cli import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    REFERENCE_CONTRACT_DIGEST,
    REFERENCE_SETTINGS_HASH,
    SAMPLE_SKILL_MD,
    SAMPLE_UV_LOCK,
    BindError,
    ScaffoldError,
    app,
    bind_reference_target,
    render_sample_manifest,
    sample_program_text,
    scaffold,
)
from evolution.tree.target.http_adapter import HttpSessionAdapter
from evolution.tree.target.reference_target import ReferenceTarget, serve

pytestmark = pytest.mark.slow

POLICY = {"excludes": [], "dependency_paths": ["uv.lock"], "writable_paths": ["skills/**"]}


def write_manifest(directory: Path, *, seed="seed", port=8765, target_extra=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    seed_root = directory / seed
    (seed_root / "skills").mkdir(parents=True, exist_ok=True)
    (seed_root / "skills" / "SKILL.md").write_text("first line\n")
    (seed_root / "uv.lock").write_text("lock\n")
    target = {"base_url": f"http://127.0.0.1:{port}", "seed_mode": "exact"}
    target.update(target_extra or {})
    from tests.tree.manifest.test_manifest_v15 import v15_document

    payload = {
        **v15_document(),
        "seed_root": seed,
        "target": target,
        "artifacts": POLICY,
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(payload))
    return path


def test_bind_defaults_root_to_manifest_directory(tmp_path):
    manifest = load_manifest_v2(write_manifest(tmp_path / "demo"))
    target = bind_reference_target(manifest)
    assert target.allowed_artifact_root == Path(os.path.realpath(tmp_path / "demo"))
    assert target.seed_mode == "exact"
    assert target.policy == manifest.policy
    assert target.contract_digest == REFERENCE_CONTRACT_DIGEST
    assert target.settings_hash == REFERENCE_SETTINGS_HASH
    assert target.max_input_bytes == MAX_INPUT_BYTES
    assert target.max_output_bytes == MAX_OUTPUT_BYTES


def test_bind_explicit_root_wins_and_is_realpathed(tmp_path):
    manifest = load_manifest_v2(write_manifest(tmp_path / "root" / "demo"))
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "root")
    target = bind_reference_target(manifest, allowed_root=link)
    assert target.allowed_artifact_root == Path(os.path.realpath(tmp_path / "root"))


def test_bind_refuses_missing_root(tmp_path):
    manifest = load_manifest_v2(write_manifest(tmp_path / "demo"))
    with pytest.raises(BindError) as info:
        bind_reference_target(manifest, allowed_root=tmp_path / "absent")
    assert info.value.code == "root_not_directory"


def test_bind_refuses_seed_outside_root(tmp_path):
    manifest = load_manifest_v2(write_manifest(tmp_path / "demo", seed="../elsewhere"))
    with pytest.raises(BindError) as info:
        bind_reference_target(manifest)
    assert info.value.code == "seed_outside_root"


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"contract_digest": "other-contract"}, "pin_mismatch"),
        ({"settings_hash": "other-settings"}, "pin_mismatch"),
        ({"min_input_bytes": MAX_INPUT_BYTES + 1}, "limits_incompatible"),
        ({"max_response_bytes": MAX_OUTPUT_BYTES - 1}, "limits_incompatible"),
    ],
)
def test_bind_refuses_what_preflight_would_refuse(tmp_path, extra, code):
    manifest = load_manifest_v2(write_manifest(tmp_path / "demo", target_extra=extra))
    with pytest.raises(BindError) as info:
        bind_reference_target(manifest)
    assert info.value.code == code


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"contract_digest": REFERENCE_CONTRACT_DIGEST, "settings_hash": REFERENCE_SETTINGS_HASH},
        {"min_input_bytes": MAX_INPUT_BYTES, "max_response_bytes": MAX_OUTPUT_BYTES},
    ],
)
def test_bound_target_passes_preflight(tmp_path, extra):
    manifest = load_manifest_v2(write_manifest(tmp_path / "demo", target_extra=extra))
    target = bind_reference_target(manifest)
    server = serve(target)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        port = server.server_address[1]
        bound = load_manifest_v2(write_manifest(tmp_path / "demo", port=port, target_extra=extra))
        adapter = HttpSessionAdapter(bound.target, bound.policy, run_id="bind-test")
        scan = scan_tree(bound.seed_root, bound.policy)
        expected = ExpectedAttestation(scan.candidate_digest, scan.dependency_digest)
        preflight = asyncio.run(adapter.preflight(expected))
        assert preflight is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_serve_closes_the_target_after_the_server(tmp_path, monkeypatch):
    """`serve` calls `shutdown`, `server_close` (inside `finish`), then `target.close()` (ADR-0009)."""
    import evolution.tree.reference_target_cli as cli
    from evolution.tree.target.reference_target import serve as serve_reference_target

    order: list[str] = []
    cli.scaffold(tmp_path / "demo")
    manifest = tmp_path / "demo" / "manifest.json"
    real_bind = cli.bind_reference_target

    def bind(document, allowed_root=None):
        target = real_bind(document, allowed_root)
        real_close = target.close

        def close() -> None:
            order.append("target.close")
            real_close()

        target.close = close  # type: ignore[method-assign]
        return target

    monkeypatch.setattr(cli, "bind_reference_target", bind)
    # An ephemeral port instead of the sample's 8765, so the test never collides.
    monkeypatch.setattr(
        cli, "_serve_target", lambda target, host, port: serve_reference_target(target, port=0)
    )
    real_finish = cli._Serving.finish

    def finish(self):
        order.append("finish")
        return real_finish(self)

    monkeypatch.setattr(cli._Serving, "finish", finish)
    monkeypatch.setattr(cli._Serving, "wait", lambda self: self.stop.set())
    cli.serve(manifest, None)
    assert order == ["finish", "target.close"]


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"contract_digest": "other-contract"}, "pin_mismatch"),
        ({"settings_hash": "other-settings"}, "pin_mismatch"),
        ({"min_input_bytes": MAX_INPUT_BYTES + 1}, "limits_incompatible"),
        ({"max_response_bytes": MAX_OUTPUT_BYTES - 1}, "limits_incompatible"),
    ],
)
def test_live_preflight_refuses_what_bind_would_refuse(tmp_path, extra, code):
    """Spec §8: a manifest `bind_reference_target` refuses is one a live
    `HttpSessionAdapter.preflight`, run against a target actually bound from an
    accepting manifest, refuses with the same code."""
    accepting = load_manifest_v2(write_manifest(tmp_path / "demo"))
    target = bind_reference_target(accepting)
    server = serve(target)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        port = server.server_address[1]
        refused = load_manifest_v2(write_manifest(tmp_path / "demo", port=port, target_extra=extra))
        adapter = HttpSessionAdapter(refused.target, refused.policy, run_id="live-refuse-test")
        scan = scan_tree(refused.seed_root, refused.policy)
        expected = ExpectedAttestation(scan.candidate_digest, scan.dependency_digest)
        with pytest.raises(RolloutInfrastructureError) as info:
            asyncio.run(adapter.preflight(expected))
        assert info.value.code == code
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_cli_constants_match_reference_target_defaults(tmp_path):
    """The CLI's binding constants are not a second definition of the reference
    target's own defaults; they equal what `ReferenceTarget` picks unasked."""
    default_target = ReferenceTarget(
        allowed_artifact_root=tmp_path, policy=ArtifactPolicyV1((), (), ())
    )
    assert MAX_INPUT_BYTES == default_target.max_input_bytes
    assert MAX_OUTPUT_BYTES == default_target.max_output_bytes
    assert REFERENCE_CONTRACT_DIGEST == default_target.contract_digest
    assert REFERENCE_SETTINGS_HASH == default_target.settings_hash


runner = CliRunner()


def test_scaffold_writes_exactly_seven_files(tmp_path):
    result = scaffold(tmp_path / "demo")
    demo = tmp_path / "demo"
    assert result.dir == demo.resolve()
    assert sorted(str(p.relative_to(demo)) for p in demo.rglob("*") if p.is_file()) == [
        "corpus.jsonl",
        "editor/edit.py",
        "manifest.json",
        "reflector/reflect.py",
        "scorer/score.py",
        "seed/skills/SKILL.md",
        "seed/uv.lock",
    ]
    assert (demo / "manifest.json").read_text() == render_sample_manifest()
    assert (demo / "seed" / "skills" / "SKILL.md").read_text() == SAMPLE_SKILL_MD
    assert (demo / "seed" / "uv.lock").read_text() == SAMPLE_UV_LOCK
    for path in demo.rglob("*"):
        if path.is_file():
            assert not os.access(path, os.X_OK)
    for path in [demo, *[p for p in demo.rglob("*") if p.is_dir()]]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    manifest = load_manifest_v2(demo / "manifest.json")
    assert manifest.schema == "evolution-v2-manifest/15"
    document = json.loads((demo / "manifest.json").read_text())
    campaign = document["campaign"]
    # R72: the scaffold writes `/15` with the recommended values (PDR-0028).
    assert "alpha" not in campaign
    assert "regression_margin" not in campaign["gate"]
    assert campaign["evaluation"] == {"repeats_per_question": 3}
    assert campaign["round_stop"] == "first_accepted"
    assert campaign["baseline_refresh_after_rounds"] == 3
    assert campaign["gate"] == {
        "mode": "statistical",
        "stability": "stable",
        "focus": {"confidence": 0.9, "margin": 0},
        "causal": {"confidence": 0.9, "tolerance": "measured_noise"},
        "provenance": {"kind": "reported"},
        "fresh_parent": False,
        "global": "point",
    }
    # ADR-0034: the Grouper is a top-level role; `reflector.grouping` is refused.
    assert document["grouper"] == {
        "kind": "fields",
        "features": [
            {
                "name": "failed_criteria",
                "source": "observation",
                "path": "/failed_criteria",
                "description": "Failed criteria.",
            },
            {
                "name": "terminal",
                "source": "observation",
                "path": "/terminal",
                "description": "Terminal observation status.",
            },
        ],
    }
    assert "grouping" not in document["reflector"]
    # ADR-0029: the metric contract the reference target satisfies, and no
    # readable paths for the roles.
    assert document["scorer"]["metric_contract"] == {
        "version": "metric_contract/1",
        "kind": "pointwise",
        "target_fields": [
            {"event": "assistant_message", "pointer": "/content", "value": "nonempty_string"}
        ],
    }
    assert document["role_visibility"] == {"version": "role_visibility/1", "readable_paths": []}
    # I1: the scaffold declares the call-retry bounds `/8` requires -- four
    # attempts per target call, and a turn that is never resent, which is the
    # sound default for an agent whose session a failed turn may have changed.
    assert campaign["call_retries"] == {
        "max_attempts": 4,
        "turn_max_attempts": 1,
    }
    assert manifest.seed_root == (demo / "seed").resolve()
    assert manifest.policy == ArtifactPolicyV1(("build/**",), ("uv.lock",), ("skills/**",))
    assert manifest.target.base_url == "http://127.0.0.1:8765"
    assert manifest.workflow is not None
    assert manifest.workflow.corpus_path == (demo / "corpus.jsonl").resolve()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo"]


def test_scaffold_writes_nothing_outside_dir(tmp_path):
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "marker").write_text("x")
    scaffold(tmp_path / "nested" / "demo")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["canary", "nested"]
    assert sorted(p.name for p in (tmp_path / "nested").iterdir()) == ["demo"]
    assert (canary / "marker").read_text() == "x"


def test_scaffold_fills_an_existing_empty_directory(tmp_path):
    (tmp_path / "demo").mkdir()
    scaffold(tmp_path / "demo")
    assert (tmp_path / "demo" / "manifest.json").is_file()


def test_scaffold_refuses_nonempty_and_file(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "keep").write_text("x")
    with pytest.raises(ScaffoldError) as info:
        scaffold(tmp_path / "demo")
    assert info.value.code == "dir_not_empty"
    assert (tmp_path / "demo" / "keep").read_text() == "x"
    (tmp_path / "file").write_text("x")
    with pytest.raises(ScaffoldError) as info:
        scaffold(tmp_path / "file")
    assert info.value.code == "not_a_directory"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo", "file"]


def test_scaffold_refuses_symlink_at_dir(tmp_path):
    # A rename can never target a symlink, so DIR being one is refused up
    # front, before any staging write, the same as a plain file at DIR.
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "demo"
    link.symlink_to(target)
    with pytest.raises(ScaffoldError) as info:
        scaffold(link)
    assert info.value.code == "not_a_directory"
    assert link.is_symlink()
    assert list(target.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo", "elsewhere"]


def test_scaffold_cleans_up_staging_on_base_exception(tmp_path, monkeypatch):
    # A `BaseException` (e.g. `KeyboardInterrupt`) during a write must still
    # be cleaned up: the cleanup lives in a `finally`, not in an
    # `except (OSError, ValueError)` that a `BaseException` skips.
    calls = {"n": 0}
    real = cli_module.atomic_write_private_text

    def flaky(path, text, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(path, text, **kwargs)

    monkeypatch.setattr(cli_module, "atomic_write_private_text", flaky)
    with pytest.raises(KeyboardInterrupt):
        scaffold(tmp_path / "demo")
    assert not (tmp_path / "demo").exists()
    assert list(tmp_path.iterdir()) == []


def test_scaffold_failure_leaves_nothing_and_retries(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = cli_module.atomic_write_private_text

    def flaky(path, text, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(path, text, **kwargs)

    monkeypatch.setattr(cli_module, "atomic_write_private_text", flaky)
    with pytest.raises(ScaffoldError) as info:
        scaffold(tmp_path / "demo")
    assert info.value.code == "scaffold_failed"
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(cli_module, "atomic_write_private_text", real)
    scaffold(tmp_path / "demo")
    assert (tmp_path / "demo" / "manifest.json").is_file()


def test_init_command_prints_one_json_line(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path / "demo")])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "status": "created",
        "dir": str((tmp_path / "demo").resolve()),
        "manifest": str((tmp_path / "demo" / "manifest.json").resolve()),
        "seed_root": str((tmp_path / "demo" / "seed").resolve()),
    }


def test_init_command_refusal_is_exit_2_with_empty_stdout(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "keep").write_text("x")
    result = runner.invoke(app, ["init", str(tmp_path / "demo")])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "evo-target: init refused: dir_not_empty" in result.stderr


def test_scaffold_clears_symlink_at_staging_path(tmp_path, monkeypatch):
    # Create a symlink that blocks the staging path before scaffold
    staging_path = tmp_path / ".demo.evo-target-12345"
    staging_path.symlink_to("/tmp")
    # Monkeypatch getpid to use the same staging name
    monkeypatch.setattr("os.getpid", lambda: 12345)
    result = scaffold(tmp_path / "demo")
    assert result.dir == (tmp_path / "demo").resolve()
    assert (tmp_path / "demo" / "manifest.json").is_file()
    # The symlink should have been cleared by scaffold
    assert not staging_path.is_symlink()


def test_scaffold_clears_stale_staging_directory(tmp_path):
    staging_path = tmp_path / ".demo.evo-target-stale"
    staging_path.mkdir()
    (staging_path / "stray").write_text("x")
    result = scaffold(tmp_path / "demo")
    assert result.dir == (tmp_path / "demo").resolve()
    assert (tmp_path / "demo" / "manifest.json").is_file()
    assert not (tmp_path / "demo" / "stray").exists()
    assert sorted(p.name for p in (tmp_path / "demo").rglob("*") if p.is_file()) == [
        "SKILL.md",
        "corpus.jsonl",
        "edit.py",
        "manifest.json",
        "reflect.py",
        "score.py",
        "uv.lock",
    ]


def test_scaffold_copies_the_packaged_sample_bytes(tmp_path):
    from importlib import resources

    demo = scaffold(tmp_path / "demo").dir
    sample = resources.files("evolution.tree.reference_sample")
    assert (demo / "scorer" / "score.py").read_bytes() == (sample / "score.py").read_bytes()
    assert (demo / "editor" / "edit.py").read_bytes() == (sample / "edit.py").read_bytes()
    assert (demo / "reflector" / "reflect.py").read_bytes() == (sample / "reflect.py").read_bytes()
    assert (demo / "corpus.jsonl").read_bytes() == (sample / "corpus.jsonl").read_bytes()


def test_two_scaffolds_are_byte_identical(tmp_path):
    a = scaffold(tmp_path / "a").dir
    b = scaffold(tmp_path / "b").dir

    def listing(d):
        return sorted((str(p.relative_to(d)), p.read_bytes()) for p in d.rglob("*") if p.is_file())

    assert listing(a) == listing(b)


def test_scaffolded_manifest_binds_offline(tmp_path, monkeypatch):
    # `python3` must be on the child PATH at bind time: point PATH at the
    # running interpreter's own directory (a venv always provides `python3`
    # there); fall back to a symlink if this interpreter's directory does not.
    python_dir = Path(sys.executable).parent
    if not (python_dir / "python3").exists():
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        python_dir = bin_dir
    monkeypatch.setenv("PATH", f"{python_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    from evolution.tree.bindings import build_editor, build_environment, build_scorer

    demo = scaffold(tmp_path / "demo").dir
    manifest = load_manifest_v2(demo / "manifest.json")
    assert manifest.workflow is not None
    workflow = manifest.workflow
    run_dir = tmp_path / "run"
    build_environment(workflow.environment)
    build_scorer(workflow.scorer, seed_root=manifest.seed_root, run_dir=run_dir)
    build_editor(
        workflow.editor,
        seed_root=manifest.seed_root,
        run_dir=run_dir,
        environ=os.environ,
    )
    assert not run_dir.exists()  # binding writes nothing


def test_init_help_and_docstring_name_the_campaign_sample():
    import evolution.tree.reference_target_cli as cli

    assert cli.__doc__ is not None
    assert "/15" in cli.__doc__ and "corpus" in cli.__doc__
    result = runner.invoke(app, ["init", "--help"])
    assert "corpus" in result.output


def test_sample_program_text_refuses_an_unknown_name():
    with pytest.raises(ValueError, match="unknown sample file: nope.py"):
        sample_program_text("nope.py")


def test_scaffold_classifies_enotdir_as_not_a_directory(tmp_path, monkeypatch):
    # Target doesn't exist yet, passes pre-check
    # Monkeypatch os.rename to create a file at final and raise ENOTDIR
    calls = {"n": 0}

    def mock_rename(src, dst):
        calls["n"] += 1
        # Create a regular file at the destination to simulate conflict
        Path(dst).write_text("conflict")
        raise OSError(errno.ENOTDIR, "Not a directory")

    monkeypatch.setattr("os.rename", mock_rename)
    with pytest.raises(ScaffoldError) as info:
        scaffold(tmp_path / "demo")
    assert info.value.code == "not_a_directory"
    assert calls["n"] == 1  # Verify mock was called exactly once
    # Verify staging directory was cleaned up
    staging = tmp_path / f".demo.evo-target-{os.getpid()}"
    assert not staging.exists()


def test_scaffold_classifies_enotempty_as_dir_not_empty(tmp_path, monkeypatch):
    # Create an empty directory at target, passes pre-check
    (tmp_path / "demo").mkdir()
    calls = {"n": 0}

    def mock_rename(src, dst):
        calls["n"] += 1
        # Drop a file into the destination to make it non-empty
        (Path(dst) / "blocking-file").write_text("x")
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    monkeypatch.setattr("os.rename", mock_rename)
    with pytest.raises(ScaffoldError) as info:
        scaffold(tmp_path / "demo")
    assert info.value.code == "dir_not_empty"
    assert calls["n"] == 1  # Verify mock was called exactly once
    # Verify staging directory was cleaned up
    staging = tmp_path / f".demo.evo-target-{os.getpid()}"
    assert not staging.exists()


def test_scaffold_classifies_an_unlistable_directory_as_scaffold_failed(tmp_path):
    """`_nonempty_directory`'s `iterdir()` raises `PermissionError` on a
    directory with no read/execute bits; that must be classified like any
    other `OSError` inside `scaffold`'s boundary, not escape as a bare
    traceback."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 directory, so the failure cannot be provoked")
    target = tmp_path / "demo"
    target.mkdir()
    target.chmod(0o000)
    try:
        with pytest.raises(ScaffoldError) as info:
            scaffold(target)
        assert info.value.code == "scaffold_failed"
    finally:
        target.chmod(0o700)


def test_init_command_unlistable_directory_is_exit_2_with_no_traceback(tmp_path, capsys):
    """Calls the `init` command function directly rather than through
    `runner.invoke(app, ...)`: typer's own `Path` argument type checks
    `os.access(DIR, os.R_OK)` during CLI argument parsing and refuses a
    0o000 directory with its own "is not readable" message before `init`'s
    body — and therefore `scaffold` — ever runs, which would test typer's
    argument parser rather than the `scaffold_failed` boundary this fix
    wave adds.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 directory, so the failure cannot be provoked")
    target = tmp_path / "demo"
    target.mkdir()
    target.chmod(0o000)
    try:
        with pytest.raises(typer.Exit) as info:
            cli_module.init(target)
    finally:
        target.chmod(0o700)
    assert info.value.exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "evo-target: init refused: scaffold_failed" in captured.err


def test_init_command_not_a_directory_is_exit_2(tmp_path):
    (tmp_path / "demo").write_text("x")
    result = runner.invoke(app, ["init", str(tmp_path / "demo")])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "evo-target: init refused: not_a_directory" in result.stderr


# The repo root's `src`, so every subprocess this module spawns imports
# THIS checkout regardless of which venv `sys.executable` resolves to (an
# ambient interpreter's own editable install can point at a different,
# stale checkout).
SRC = Path(__file__).resolve().parents[1] / "src"
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": str(SRC)}

SERVE_ARGV = [sys.executable, "-m", "evolution.tree.reference_target_cli", "serve"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_serve(manifest: Path, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [*SERVE_ARGV, str(manifest), *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=SUBPROCESS_ENV,
    )


def stop(proc: subprocess.Popen, sig=signal.SIGINT) -> tuple[int, str, str]:
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=20)
    return proc.returncode, out, err


def scaffold_on_port(tmp_path: Path, port: int) -> Path:
    scaffold(tmp_path / "demo")
    manifest = tmp_path / "demo" / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["target"]["base_url"] = f"http://127.0.0.1:{port}"
    manifest.write_text(json.dumps(payload, indent=2) + "\n")
    return manifest


def test_serve_ready_line_then_conformance_then_sigint(tmp_path):
    from evolution.tree.conformance import run_conformance

    port = free_port()
    manifest = scaffold_on_port(tmp_path, port)
    proc = start_serve(manifest)
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["status"] == "listening"
        assert ready["base_url"] == f"http://127.0.0.1:{port}"
        assert ready["allowed_artifact_root"] == str((tmp_path / "demo").resolve())
        assert ready["seed_root"] == str((tmp_path / "demo" / "seed").resolve())
        assert ready["seed_mode"] == "exact"
        assert len(ready["policy_digest"]) == 64
        assert len(ready["boot_id"]) == 32
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/evolution/v2/health")
        assert conn.getresponse().status == 200
        conn.close()
        report = asyncio.run(run_conformance(load_manifest_v2(manifest)))
        assert report.ok, report.to_payload()
        assert report.to_payload()["leaked"] == []
    finally:
        code, out, err = stop(proc)
    assert code == 0
    assert out == ""
    assert err == ""


def test_serve_sigterm_is_clean_too(tmp_path):
    port = free_port()
    proc = start_serve(scaffold_on_port(tmp_path, port))
    proc.stdout.readline()
    code, out, err = stop(proc, signal.SIGTERM)
    assert (code, out, err) == (0, "", "")


def test_serve_refuses_bad_manifest_with_exit_2(tmp_path):
    manifest = scaffold_on_port(tmp_path, free_port())
    payload = json.loads(manifest.read_text())
    payload["schema"] = "evolution-v2-manifest/999"
    manifest.write_text(json.dumps(payload))
    proc = start_serve(manifest)
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert out == ""
    # PDR-0038 / ADR-0046: the campaign-schema refusal is shared by every loader.
    assert err == (
        "evo-target: serve refused: manifest_schema; expected evolution-v2-manifest/15 or /16\n"
    )


def test_serve_refuses_seed_outside_root(tmp_path):
    manifest = scaffold_on_port(tmp_path, free_port())
    (tmp_path / "other").mkdir()
    proc = start_serve(manifest, "--allowed-root", str(tmp_path / "other"))
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert out == ""
    assert err == "evo-target: serve refused: seed_outside_root\n"


def test_serve_exit_1_when_port_is_held(tmp_path):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        proc = start_serve(scaffold_on_port(tmp_path, port))
        out, err = proc.communicate(timeout=20)
    finally:
        holder.close()
    assert proc.returncode == 1
    assert out == ""
    assert err.startswith(f"evo-target: cannot bind 127.0.0.1:{port}: ")


def test_serve_loop_failure_is_exit_1_with_one_line(tmp_path):
    port = free_port()
    manifest = scaffold_on_port(tmp_path, port)
    snippet = (
        "import sys\n"
        "from evolution.tree.target import reference_target_http as h\n"
        "def boom(self, poll_interval=0.5):\n"
        "    raise RuntimeError('loop died')\n"
        "h._TargetServer.serve_forever = boom\n"
        "from evolution.tree.reference_target_cli import app\n"
        "app(['serve', sys.argv[1]])\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", snippet, str(manifest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=SUBPROCESS_ENV,
    )
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 1
    assert err == "evo-target: serving stopped: RuntimeError\n"
    # `serving.ready()` only prints the ready line once `started` is set and
    # `stop` is not; a loop that fails synchronously inside `serve_forever`
    # sets `stop` immediately after, so no ready line is ever printed here.
    assert out == ""
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))  # the socket was closed
    holder.close()


def test_serve_signal_between_bind_and_thread_start_is_still_clean(tmp_path):
    # A SIGINT that lands after the socket is bound but before the serving
    # thread starts must not escape as an uncaught KeyboardInterrupt: the
    # handlers install before the bind, so this window is already covered.
    port = free_port()
    manifest = scaffold_on_port(tmp_path, port)
    snippet = (
        "import os, signal, sys\n"
        "from evolution.tree import reference_target_cli as cli\n"
        "_real_serve_target = cli._serve_target\n"
        "def _kill_before_return(target, host, port):\n"
        "    server = _real_serve_target(target, host=host, port=port)\n"
        "    os.kill(os.getpid(), signal.SIGINT)\n"
        "    return server\n"
        "cli._serve_target = _kill_before_return\n"
        "cli.app(['serve', sys.argv[1]])\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", snippet, str(manifest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=SUBPROCESS_ENV,
    )
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0
    assert err == ""
    # The signal lands, and `stop` is set, before `attach` is even called;
    # `serving.ready()` therefore always finds `stop` set and never prints
    # the ready line.
    assert out == ""
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))  # the socket was closed
    holder.close()


def test_serve_ipv6_loopback(tmp_path):
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    probe = socket.socket(socket.AF_INET6)
    try:
        probe.bind(("::1", 0))
        port = probe.getsockname()[1]
    except OSError:
        pytest.skip("no IPv6 loopback")
    finally:
        probe.close()
    manifest = scaffold_on_port(tmp_path, port)
    payload = json.loads(manifest.read_text())
    payload["target"]["base_url"] = f"http://[::1]:{port}"
    manifest.write_text(json.dumps(payload))
    proc = start_serve(manifest)
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["base_url"] == f"http://[::1]:{port}"
        conn = http.client.HTTPConnection("::1", port, timeout=5)
        conn.request("GET", "/evolution/v2/health")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        code, _, _ = stop(proc)
    assert code == 0


def test_public_api_exports_the_reference_target_only():
    assert "ReferenceTarget" in tree_package.__all__
    assert "serve_reference_target" in tree_package.__all__
    assert "Misbehave" not in tree_package.__all__
    assert tree_package.ReferenceTarget is ReferenceTarget
    assert tree_package.serve_reference_target is serve


def test_console_script_entry_point_is_registered():
    # `importlib.metadata` reads the *installed* distribution's metadata,
    # which under an ambient interpreter whose editable install points at a
    # different, stale checkout would prove nothing about this checkout.
    # `pyproject.toml` on disk is unambiguous: it is this checkout's own
    # source of truth for what `evo-target` names.
    root = SRC.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    entry = pyproject["project"]["scripts"]["evo-target"]
    assert entry == "evolution.tree.reference_target_cli:app"
    module_name, _, attr_name = entry.partition(":")
    module = importlib.import_module(module_name)
    assert getattr(module, attr_name) is app


def test_sample_program_text_is_byte_exact(tmp_path, monkeypatch):
    """W2 finding F3: a packaged program is copied byte for byte, `\\r` included.

    `Path.read_text` translates newlines, so a program with Windows line
    endings would be silently normalized by the scaffold; the sample bytes are
    read and written as bytes, and the text view is a plain decode.
    """

    from evolution.tree import reference_target_cli

    package = tmp_path / "package"
    package.mkdir()
    (package / "score.py").write_bytes(b"print('a')\r\nprint('b')\r\n")
    monkeypatch.setattr(reference_target_cli.resources, "files", lambda _name: package)

    assert reference_target_cli.sample_program_bytes("score.py") == b"print('a')\r\nprint('b')\r\n"
    assert reference_target_cli.sample_program_text("score.py") == "print('a')\r\nprint('b')\r\n"
