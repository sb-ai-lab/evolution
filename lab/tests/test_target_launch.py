"""Launcher pieces (spec W19 §5.2). The full start runs through Owner in Task 8."""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from evolution_lab.owner.protocol import encode_frame

_ECHO_AGENT_ROOT = str(Path(__file__).parent / "owner_fixtures" / "echo_agent")


def _close_quietly(fd):
    try:
        os.close(fd)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _import_state():
    """`import_launch` reloads the module (and `import_build` may insert
    `import_root` into `sys.path`); undo both so later tests see a clean
    interpreter. Also collects fds `import_launch` opens for a guaranteed
    close at teardown, whether or not the module or the test already closed
    them, so no descriptor leaks past this file."""
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    opened_fds = []
    try:
        yield opened_fds
    finally:
        for fd in opened_fds:
            _close_quietly(fd)
        sys.path[:] = path_before
        for name in set(sys.modules) - modules_before:
            del sys.modules[name]


def import_launch(monkeypatch, opened_fds):
    # The module pops both fds at import; give it throwaway pipes. The write
    # end is never used by the module, so close it immediately.
    for name in ("LAB_TARGET_CONFIG_FD", "LAB_TARGET_READY_FD"):
        read, write = os.pipe()
        os.close(write)
        monkeypatch.setenv(name, str(read))
        opened_fds.append(read)
    sys.modules.pop("evolution_lab.target.launch", None)
    import evolution_lab.target.launch as launch
    return launch


def test_fds_are_popped_and_not_inheritable(monkeypatch, _import_state):
    launch = import_launch(monkeypatch, _import_state)
    assert "LAB_TARGET_CONFIG_FD" not in os.environ and "LAB_TARGET_READY_FD" not in os.environ
    assert not os.get_inheritable(launch._config_fd) and not os.get_inheritable(launch._ready_fd)


def test_load_config_reads_one_frame_and_checks_schema(monkeypatch, _import_state):
    launch = import_launch(monkeypatch, _import_state)
    read, write = os.pipe()
    os.write(write, encode_frame({"schema": "lab-target-runtime/1"})); os.close(write)
    with pytest.raises(ValueError):
        launch.load_config(read)


def test_context_splits_secrets_from_env(monkeypatch, _import_state, tmp_path):
    launch = import_launch(monkeypatch, _import_state)
    context = launch.context_from({"secret_names": ["TOKEN"], "work_directory": str(tmp_path)},
                                  {"TOKEN": "s", "MODE": "x"})
    assert dict(context.secrets) == {"TOKEN": "s"} and dict(context.env) == {"MODE": "x"}


def test_import_build_uses_import_root_only_when_given(monkeypatch, _import_state, tmp_path):
    launch = import_launch(monkeypatch, _import_state)
    package = tmp_path / "pkg"; package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "entry.py").write_text("from .helper import VALUE\ndef build(context):\n    return VALUE\n")
    (package / "helper.py").write_text("VALUE = 'multi-file'\n")
    build = launch.import_build({"module": "pkg.entry", "attribute": "build", "import_root": str(tmp_path)})
    assert build(None) == "multi-file"
    with pytest.raises(ModuleNotFoundError):
        launch.import_build({"module": "absent_pkg.entry", "attribute": "build", "import_root": None})


def _config(tmp_path, **overrides):
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    (tmp_path / "snapshot").mkdir(exist_ok=True)
    config = dict(
        agent={"module": "echo_agent", "attribute": "build", "import_root": _ECHO_AGENT_ROOT},
        secret_names=[],
        work_directory=str(tmp_path / "work"),
        allowed_artifact_root=str(tmp_path / "artifacts"),
        snapshot_root=str(tmp_path / "snapshot"),
        policy={"version": 1, "excludes": [], "dependency_paths": [], "writable_paths": []},
        identity=dict(target_id="synthetic", contract_digest="c" * 64,
                      settings_hash="s" * 64, runtime_ref_digest="r" * 64, seed_mode="none"),
        limits=dict(max_instances=1, max_concurrent_sessions=1,
                    max_input_bytes=1000, max_output_bytes=1000),
        base_url="http://127.0.0.1:0",
        launch_id="launch-1",
        launch_digest="d" * 64,
    )
    config.update(overrides)
    return config


def test_run_returns_zero_without_ready_frame_when_stop_is_set_before_serve(monkeypatch, _import_state, tmp_path):
    """(a) A stop that arrived during build must not bind or write ready."""
    launch = import_launch(monkeypatch, _import_state)
    stop = threading.Event()
    stop.set()
    ready_read, ready_write = os.pipe()
    _import_state.extend((ready_read, ready_write))

    result = launch.run(_config(tmp_path), stop, ready_write)

    assert result == 0
    os.close(ready_write)
    assert os.read(ready_read, 4) == b""  # EOF: the write end closed with nothing written


def test_run_closes_server_when_serve_target_raises(monkeypatch, _import_state, tmp_path):
    """(b) `serve_target` raising must still run `server.close()`."""
    launch = import_launch(monkeypatch, _import_state)
    import evolution.tree as tree

    closed = []
    original_close = tree.TargetServer.close

    def spy_close(self):
        closed.append(True)
        return original_close(self)

    def raising_serve_target(server, host, port):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(tree.TargetServer, "close", spy_close)
    monkeypatch.setattr(tree, "serve_target", raising_serve_target)

    stop = threading.Event()
    ready_read, ready_write = os.pipe()
    _import_state.extend((ready_read, ready_write))

    with pytest.raises(RuntimeError, match="bind failed"):
        launch.run(_config(tmp_path), stop, ready_write)

    assert closed == [True]


@pytest.mark.parametrize("turn,want", [
    (None, "target_failed"),
    ({"empty_answer": "target_failed"}, "target_failed"),
    ({"empty_answer": "ok"}, "ok"),
])
def test_run_applies_runtime_empty_answer_policy(monkeypatch, _import_state, tmp_path, turn, want):
    """Serve seam avoids a listening socket; real server/backend perform the turn."""
    from evolution.tree.execution.wire_v2 import PREFIX, STRATEGY, new_instance_id
    from evolution_lab.target import AgentTurn, TurnReason
    from tests.test_target_backend import make
    import evolution.tree as tree

    launch = import_launch(monkeypatch, _import_state)
    factory, _ = make([], script={"x": lambda state: AgentTurn("ok", "", reason=TurnReason("agent_outcome", "gave_up"))})
    monkeypatch.setattr(launch, "import_build", lambda agent: lambda context: factory)
    config = _config(tmp_path)
    if turn is not None:
        config["turn"] = turn
    candidate = Path(config["allowed_artifact_root"]) / "candidate"
    candidate.mkdir()
    replies = []

    def serve_one_turn(server, host, port):
        instance_id = new_instance_id("run")
        status, _ = server.dispatch("POST", f"{PREFIX}/instances", json.dumps({
            "request_id": "create", "instance_id": instance_id, "candidate_root": str(candidate),
            "policy": config["policy"], "strategy": STRATEGY}).encode())
        assert status == 200
        status, payload = server.dispatch("POST", f"{PREFIX}/instances/{instance_id}/sessions",
                                          json.dumps({"request_id": "open", "public": {}, "seed": None}).encode())
        assert status == 200
        session_id = json.loads(payload)["session_id"]
        status, payload = server.dispatch("POST", f"{PREFIX}/instances/{instance_id}/sessions/{session_id}/turns",
                                          json.dumps({"request_id": "turn", "message": {"content": "x", "fields": {}}}).encode())
        assert status == 200
        replies.append(json.loads(payload))
        raise RuntimeError("turn completed")

    monkeypatch.setattr(tree, "serve_target", serve_one_turn)
    ready_read, ready_write = os.pipe()
    _import_state.extend((ready_read, ready_write))
    with pytest.raises(RuntimeError, match="turn completed"):
        launch.run(config, threading.Event(), ready_write)
    assert replies[0]["outcome"] == want
    assert replies[0]["reason"] == ({"category": "lab", "code": "empty_answer"} if want == "target_failed"
                                     else {"category": "agent_outcome", "code": "gave_up"})


@pytest.mark.parametrize("turn", [None, {}, {"empty_answer": "candidate_failed"},
                                  {"empty_answer": None}, {"empty_answer": []},
                                  {"empty_answer": "ok", "extra": True}, "ok"])
def test_load_config_refuses_malformed_turn_policy(monkeypatch, _import_state, turn):
    launch = import_launch(monkeypatch, _import_state)
    read, write = os.pipe()
    os.write(write, encode_frame({"schema": "lab-target-runtime/2", "turn": turn}))
    os.close(write)
    with pytest.raises(ValueError, match="turn"):
        launch.load_config(read)
