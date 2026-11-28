"""Optional components cross the real Core server without changing old agents."""
import asyncio
import json

import pytest
from evolution.tree import ArtifactPolicyV1, BackendError, Message, TargetServer
from evolution.tree.execution.wire_v2 import PREFIX, STRATEGY, canonical_body

from evolution_lab.target import AgentTurn, TurnProvenance, backend
from test_target_backend import tree


class Agent:
    def __init__(self):
        self.calls = []
        self.closed = False

    def describe(self):
        self.calls.append(("describe", id(asyncio.get_running_loop())))
        return {"agent": "synthetic"}

    async def open(self, public, *, session_id, seed):
        return None

    async def turn(self, message, state, *, session_id, seed):
        return AgentTurn("ok", "hello", provenance=TurnProvenance(["prompt"]))

    async def close_session(self, session_id):
        pass

    async def aclose(self):
        self.closed = True


class ComponentAgent(Agent):
    def __init__(self, value):
        super().__init__()
        self.value = value

    def components(self):
        self.calls.append(("components", id(asyncio.get_running_loop())))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def make_backend(agent, secrets=()):
    async def factory(view):
        agent.calls.append(("factory", id(asyncio.get_running_loop())))
        return agent
    return backend(factory, secrets=secrets)


def create_server(tmp_path, agent, limit, identifier_length=1, with_file=False):
    root = tmp_path.resolve()
    candidate = root / "candidate"
    candidate.mkdir()
    if with_file:
        (candidate / "data.txt").write_bytes(b"1234567890")
    policy = ArtifactPolicyV1((), (), ())
    server = TargetServer(make_backend(agent), allowed_artifact_root=root, policy=policy,
                          seed_mode="none", target_id="test", contract_digest="c" * identifier_length,
                          settings_hash="s" * identifier_length, runtime_ref_digest="r" * identifier_length,
                          max_output_bytes=limit)
    def post(path, body):
        return server.dispatch("POST", PREFIX + path, json.dumps(body).encode())
    def create():
        return post("/instances", {"request_id": "create", "instance_id": "run-0123456789abcdef0123456789abcdef", "candidate_root": str(candidate), "policy": policy.to_payload(), "strategy": STRATEGY})
    return server, post, create


def test_component_hashes_utf8_and_copies_sources():
    from evolution_lab.target import component
    sources = ["skills/A.md"]
    assert component("hello", sources) == {"sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", "sources": ["skills/A.md"]}
    assert component("é", sources) == component("é".encode(), sources)
    result = component(b"hello", sources)
    sources.append("skills/B.md")
    assert result["sources"] == ["skills/A.md"]


@pytest.mark.parametrize("components", [
    {"prompt": {"sha256": "a" * 64, "sources": ["skills/A.md"]}},
    {},
], ids=["nonempty", "empty"])
def test_components_and_used_cross_server_and_run_once_on_instance_loop(tmp_path, components):
    agent = ComponentAgent(components)
    server, post, create = create_server(tmp_path, agent, 10000)
    try:
        status, body = create()
        assert status == 200
        assert json.loads(body)["components"] == components
        assert [name for name, _ in agent.calls] == ["factory", "describe", "components"]
        assert len({loop for _, loop in agent.calls}) == 1
        iid = json.loads(body)["instance_id"]
        status, body = post(f"/instances/{iid}/sessions", {"request_id": "open", "public": {}, "seed": None})
        assert status == 200
        sid = json.loads(body)["session_id"]
        status, body = post(f"/instances/{iid}/sessions/{sid}/turns", {"request_id": "turn", "message": {"content": "x", "fields": {}}})
        assert status == 200
        assert json.loads(body)["provenance"] == {"used": ["prompt"]}
        assert [name for name, _ in agent.calls] == ["factory", "describe", "components"]
    finally:
        server.close()


def test_no_method_omits_components_from_instance_and_creation(tmp_path, capsys):
    agent = Agent()
    instance = make_backend(agent).load(tree(tmp_path))
    try:
        assert not hasattr(instance, "components")
    finally:
        instance.close()
    server, _, create = create_server(tmp_path, Agent(), 10000)
    try:
        status, body = create()
        assert status == 200
        assert "components" not in json.loads(body)
        assert capsys.readouterr().err == ""
    finally:
        server.close()


@pytest.mark.parametrize("value", [None, {"prompt": {}}, RuntimeError("broken")])
def test_bad_components_fail_load_with_reason(tmp_path, value):
    agent = ComponentAgent(value)
    with pytest.raises(BackendError, match="component"):
        make_backend(agent).load(tree(tmp_path))
    assert agent.closed


def test_secret_in_components_is_rejected_without_echo(tmp_path, capsys):
    value = {"prompt": {"sha256": "a" * 64, "sources": ["secret-token"]}}
    with pytest.raises(BackendError, match="component") as error:
        make_backend(ComponentAgent(value), secrets=("secret-token",)).load(tree(tmp_path))
    assert "secret-token" not in str(error.value)
    assert "secret-token" not in capsys.readouterr().err


@pytest.mark.parametrize("length", [1, 32, 64, 128])
def test_bound_matches_core_at_maximum_and_covers_other_lengths(tmp_path, capsys, length):
    components = {"prompt": {"sha256": "a" * 64, "sources": ["skills/A.md"]}}
    agent = ComponentAgent(components)
    server, _, create = create_server(tmp_path, agent, 1, length, with_file=True)
    try:
        status, _ = create()
        assert status == 500
        line = capsys.readouterr().err.strip()
        assert len(line.splitlines()) == 1
        assert agent.closed
        import re
        match = re.search(r"components=(\d+).*bound=(\d+).*limit=(\d+)", line)
        assert match, line
        assert int(match[1]) == len(canonical_body(components))
        bound = int(match[2])
        assert int(match[3]) == 1
        assert "max_output_bytes" in line and "components" in line
    finally:
        server.close()
    # A second Core server produces its actual body, without changing its inputs.
    root = tmp_path / "large"
    root.mkdir()
    server, _, create = create_server(root, ComponentAgent(components), 10000, length, with_file=True)
    try:
        status, body = create()
        assert status == 200
        assert json.loads(body)["file_count"] == 1
        assert json.loads(body)["byte_count"] == 10
        assert bound - len(body) == 3 * (128 - length)
    finally:
        server.close()


def test_bound_warning_does_not_reject_body_that_fits(tmp_path, capsys):
    components = {"prompt": {"sha256": "a" * 64, "sources": ["skills/A.md"]}}
    server, _, create = create_server(tmp_path, ComponentAgent(components), 800, 1)
    try:
        status, body = create()
        assert status == 200
        assert len(body) <= 800
        assert json.loads(body)["components"] == components
        assert "bound=" in capsys.readouterr().err
    finally:
        server.close()


def test_exact_bound_warns_only_when_limit_is_exceeded(tmp_path, capsys):
    components = {"prompt": {"sha256": "a" * 64, "sources": ["skills/A.md"]}}
    root = tmp_path / "measure"
    root.mkdir()
    server, _, create = create_server(root, ComponentAgent(components), 10000, 128)
    try:
        status, body = create()
        assert status == 200
        size = len(body)
    finally:
        server.close()
    for offset, status_expected in [(0, 200), (-1, 500)]:
        root = tmp_path / str(offset)
        root.mkdir()
        server, _, create = create_server(root, ComponentAgent(components), size + offset, 128)
        try:
            status, _ = create()
            assert status == status_expected
            line = capsys.readouterr().err
            assert bool(line) == (offset < 0)
            if line:
                assert f"bound={size} bytes" in line
                assert f"limit={size - 1} bytes" in line
        finally:
            server.close()


@pytest.mark.parametrize("error", [OSError("broken pipe"), ValueError("closed stream")])
def test_failed_diagnostic_write_does_not_reject_components(tmp_path, monkeypatch, error):
    import sys

    class BrokenStderr:
        def write(self, text):
            raise error

        def flush(self):
            pass

    components = {"prompt": {"sha256": "a" * 64, "sources": ["skills/A.md"]}}
    monkeypatch.setattr(sys, "stderr", BrokenStderr())
    instance = make_backend(ComponentAgent(components)).load(tree(tmp_path, max_output_bytes=1))
    try:
        assert instance.components() == components
    finally:
        instance.close()
