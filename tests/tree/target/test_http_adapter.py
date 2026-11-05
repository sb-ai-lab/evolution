"""`HttpSessionAdapter` over the `/evolution/v2` wire.

Every path that can be driven from a conforming target runs against the reference fixture over real HTTP, with a real S2 store publishing the champion
under the fixture's allowed artifact root. The few rows of the table that a
*conforming* target cannot produce — a body that is not ready, a body that
declares the wrong protocol, a redirect, a session that breaks the seed rules —
are driven by `_CannedServer`, which is still a real HTTP server: only the
bodies are hand-written.

Timeout tests use deliberately small `Timeouts` so the whole file stays under a
few seconds.
"""

import asyncio
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifacts import ArtifactStore, ExpectedAttestation
from evolution.tree.execution.interaction import (
    AttestationError,
    EnvironmentIdentity,
    EvalCase,
    InstanceProvenance,
    Message,
    RolloutBudget,
    RolloutInfrastructureError,
    RolloutSample,
    ScorerIdentity,
    SemanticIdentity,
    StaticInputEnvironment,
    TargetIdentity,
    TurnResult,
)
from evolution.tree.execution.journal import Journal
from evolution.tree.execution.journal_identity import RunIdentityV1, corpus_digest
from evolution.tree.execution.journal_rollout import JournalRolloutAdapter, durable_rollout
from evolution.tree.execution.rollout import (
    materialize_target,
    recover_target_instances,
    release_target,
    rollout,
)
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import PREFIX, REQUIRED_ATTESTS, new_instance_id
from evolution.tree.target import reference_target_http
from evolution.tree.target.config import TargetBinding, Timeouts
from evolution.tree.target.http_adapter import (
    AdapterNotPreflighted,
    Exchange,
    HttpInstance,
    HttpSession,
    HttpSessionAdapter,
    HttpTransport,
)
from evolution.tree.target.reference_target import ReferenceBackend, serve
from evolution.tree.target.target_calls import TargetCallLedger
from evolution.tree.target.target_server import BackendReply, TargetServer
from tests.reference_target_faults import FaultInjectingReferenceTarget
from tests.tree.artifacts.test_artifact_policy import write

pytestmark = pytest.mark.slow

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
STRATEGY = "artifact_root_injection"
SKILL = b"champion skill\nsecond line\n"
VR = lambda _: __version__  # noqa: E731


def wire_error(code, message="detail", retryable=False):
    return {"error": {"code": code, "message": message, "retryable": retryable}}


# -- servers ------------------------------------------------------------------


class _Servers:
    """Start servers on ephemeral ports and stop them, individually or at teardown."""

    def __init__(self) -> None:
        self._running: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def run(self, server):
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self._running.append((server, thread))
        return server.server_address[1]

    def stop(self, port):
        for entry in list(self._running):
            if entry[0].server_address[1] == port:
                self._running.remove(entry)
                self._shutdown(entry)

    def close(self):
        for entry in reversed(self._running):
            self._shutdown(entry)
        self._running.clear()

    @staticmethod
    def _shutdown(entry):
        server, thread = entry
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()


class _CannedHandler(BaseHTTPRequestHandler):
    """Replies with whatever the test put in `server.replies` for the path suffix."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        status, body, headers = self.server.reply(self.path)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        # `application/json` unless the test says otherwise; a `None` value drops
        # the header, which is how a target with no media type at all is driven.
        for name, value in {"Content-Type": "application/json", **headers}.items():
            if value is not None:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _reply
    do_POST = _reply
    do_DELETE = _reply


class _CannedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, replies):
        self.replies = replies
        super().__init__(address, _CannedHandler)

    def reply(self, path):
        entry = self.replies.get(path[len(PREFIX) :])
        if entry is None:
            return 404, wire_error("invalid_request", "path"), {}
        status, body = entry[0], entry[1]
        headers = entry[2] if len(entry) > 2 else {}
        return status, body, headers


@pytest.fixture
def servers():
    running = _Servers()
    yield running
    running.close()


@pytest.fixture
def allowed(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "runs"
    root.mkdir()
    return root


@pytest.fixture
def store(allowed, tmp_path):
    seed_root = Path(os.path.realpath(tmp_path)) / "seed"
    write(seed_root, "skills/SKILL.md", SKILL)
    write(seed_root, "scripts/run.sh", b"#!/bin/sh\n", executable=True)
    write(seed_root, "uv.lock", b"lock")
    opened = ArtifactStore.create(
        allowed / "run", seed_root, POLICY, parallel_attempts=1, version_resolver=VR
    )
    yield opened
    opened.close()


@pytest.fixture
def target(allowed, servers):
    """Start a reference target under the allowed root; returns `(target, port)`."""

    def _make(port=0, **kwargs):
        kwargs.setdefault("policy", POLICY)
        made = FaultInjectingReferenceTarget(allowed_artifact_root=allowed, **kwargs)
        return made, servers.run(serve(made, port=port))

    return _make


@pytest.fixture
def canned(servers):
    """Start a server over a mutable `{path_suffix: (status, body[, headers])}` map."""

    def _make(replies):
        return replies, servers.run(_CannedServer(("127.0.0.1", 0), replies))

    return _make


# -- helpers ------------------------------------------------------------------


def binding_for(port, **kwargs):
    kwargs.setdefault("seed_mode", "exact")
    kwargs.setdefault("contract_digest", None)
    kwargs.setdefault("settings_hash", None)
    kwargs.setdefault("timeouts", Timeouts(connect_seconds=2.0, read_seconds=10.0))
    return TargetBinding(base_url=f"http://127.0.0.1:{port}", **kwargs)


def adapter_for(port, *, policy=POLICY, run_id="run", recorder=None, **kwargs):
    return HttpSessionAdapter(binding_for(port, **kwargs), policy, run_id=run_id, recorder=recorder)


class Recorder:
    """The R1 `RolloutJournal` shape, kept in memory."""

    def __init__(self):
        self.records = []

    def append(self, event):
        self.records.append(dict(event))
        return len(self.records)


def health_of(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", f"{PREFIX}/health")
        return json.loads(conn.getresponse().read().decode("utf-8"))
    finally:
        conn.close()


def unused_port():
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


EPOCH = dict(
    boot_id="boot-1",
    contract_digest="contract-1",
    settings_hash="settings-1",
    runtime_ref_digest="runtime-1",
    seed_mode="exact",
    protocol_version="2",
)


def health_body(**overrides):
    body = dict(
        EPOCH,
        ready=True,
        target_id="canned",
        allowed_artifact_root="/canned/allowed",
        policy_digest=POLICY.digest,
        capabilities=dict(
            max_instances=2,
            max_concurrent_sessions=3,
            materialization=[STRATEGY],
            attests=list(REQUIRED_ATTESTS),
            max_input_bytes=1 << 20,
            max_output_bytes=1 << 20,
            instantiable=True,
        ),
        instances=[],
    )
    body.update(overrides)
    return body


def bound_identity(seed_mode="exact"):
    """The expected `TargetIdentity` an `HttpInstance` is built with."""
    return TargetIdentity(
        SemanticIdentity("contract-1", "b" * 64, "settings-1", seed_mode, "2", "runtime-1"),
        InstanceProvenance("boot-1", "a" * 64),
        frozenset(REQUIRED_ATTESTS),
    )


def preflighted(adapter, expected):
    return asyncio.run(adapter.preflight(expected))


# -- the happy path -----------------------------------------------------------


def test_rollout_over_real_http(target, store):
    running, port = target()
    champion = store.champion()
    exchanges: list[Exchange] = []
    adapter = adapter_for(port, recorder=exchanges.append)
    journal = Recorder()
    case = EvalCase("c", "g", "s", (), {"text": "hello", "nested": {"k": [1, 2]}}, {}, {})
    sample = RolloutSample("s0", champion.digest, 7)
    instance_id = new_instance_id("run")

    async def run():
        preflight = await adapter.preflight(champion.expected_attestation())
        instance = await materialize_target(adapter, champion, STRATEGY, instance_id, journal)
        trajectory = await rollout(
            case, sample, instance, StaticInputEnvironment(), RolloutBudget(2, 10.0), journal
        )
        await release_target(adapter, instance_id, champion.digest, STRATEGY, journal)
        return preflight, trajectory

    preflight, trajectory = asyncio.run(run())

    assert preflight.epoch == running.boot_id
    assert preflight.semantic == SemanticIdentity(
        "reference-contract-1",
        champion.dependency_digest,
        "reference-settings-1",
        "exact",
        "2",
        "reference-runtime-1",
    )
    assert preflight.capabilities.max_instances == 4
    assert preflight.capabilities.materialization == frozenset({STRATEGY})
    assert adapter.capabilities is preflight.capabilities
    assert adapter.allowed_artifact_root == running.allowed_artifact_root

    assert trajectory.terminal == "completed"
    assert len(trajectory.history.turns) == 1
    turn = trajectory.history.turns[0]
    assert turn.events[0]["content"] == "champion skill | hello"
    assert turn.usage.total == len("hello") + len("champion skill | hello")
    assert turn.outcome == "ok"
    assert trajectory.provenance.target.instance.loaded_candidate_digest == champion.digest
    assert trajectory.provenance.requested_seed == 7
    assert trajectory.provenance.effective_seed == 7
    assert health_of(port)["instances"] == []

    methods = [(item.method, item.path) for item in exchanges]
    assert methods[0] == ("GET", f"{PREFIX}/health")
    assert methods[1] == ("POST", f"{PREFIX}/instances")
    assert methods[-1] == ("DELETE", f"{PREFIX}/instances/{instance_id}")
    assert all(item.status == 200 for item in exchanges)
    assert b'"content":"hello"' in b"".join(item.request_bytes for item in exchanges)


def test_truncation_appends_an_output_truncated_event(target, store):
    running, port = target(max_answer_chars=5)
    champion = store.champion()
    adapter = adapter_for(port)
    instance_id = new_instance_id("run")

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, instance_id)
        session = await instance.open({"text": "hello"}, 7, "req-open")
        result = await instance.send(session, Message("hello"), "req-send")
        await instance.close(session)
        await adapter.release(instance_id)
        return result

    result = asyncio.run(run())
    assert [event["type"] for event in result.events] == ["assistant_message", "output_truncated"]
    assert result.events[0]["content"] == "champ"
    assert running.misbehave.oversize_bytes == 0


@pytest.mark.parametrize("drain", [True, False], ids=["server-drains", "client-reads-after-reset"])
def test_over_limit_turns_are_the_targets_refusal_under_parallel_load(
    target, store, monkeypatch, drain
):
    """24 turns over the Target's input limit, four at a time, are each
    the Target's `invalid_request` and a completed ledger call. Without the
    drain the server resets as `0.3.0a28` did, so the client's read after a
    broken write is what delivers the answer."""
    if not drain:
        monkeypatch.setattr(reference_target_http, "_DRAIN_BYTES", 0)
    _running, port = target()  # max_input_bytes 1 MiB: the binding's default floor
    champion = store.champion()
    ledger = TargetCallLedger()
    adapter = HttpSessionAdapter(binding_for(port), POLICY, run_id="run", ledger=ledger)
    instance_id = new_instance_id("run")
    oversized = Message("x" * (2 << 20))

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, instance_id)
        sessions = [await instance.open({}, seed, f"open-{seed}") for seed in range(4)]
        codes = []
        for wave in range(6):
            outcomes = await asyncio.gather(
                *(
                    instance.send(session, oversized, f"send-{wave}-{index}")
                    for index, session in enumerate(sessions)
                ),
                return_exceptions=True,
            )
            codes += [getattr(outcome, "code", repr(outcome)) for outcome in outcomes]
        for session in sessions:
            await instance.close(session)
        await adapter.release(instance_id)
        return codes

    codes = asyncio.run(run())
    assert codes == ["invalid_request"] * 24
    report = ledger.report()
    assert report.uncertain == 0 and not report.uncertain_any
    assert report.completed == report.admitted


def test_two_instances_run_concurrent_sessions(target, store):
    _running, port = target()
    champion = store.champion()
    adapter = adapter_for(port)
    ids = [new_instance_id("run"), new_instance_id("run")]

    async def one(instance):
        sessions = [await instance.open({}, seed, f"open-{seed}") for seed in (1, 2)]
        results = await asyncio.gather(
            *(
                instance.send(session, Message(f"m{index}"), f"send-{index}")
                for index, session in enumerate(sessions)
            )
        )
        for session in sessions:
            await instance.close(session)
        return [result.events[0]["content"] for result in results]

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instances = [await adapter.materialize(champion, STRATEGY, one_id) for one_id in ids]
        answers = await asyncio.gather(*(one(instance) for instance in instances))
        for one_id in ids:
            await adapter.release(one_id)
        return answers

    answers = asyncio.run(run())
    assert answers == [["champion skill | m0", "champion skill | m1"]] * 2
    assert health_of(port)["instances"] == []


def test_durable_rollout_leaks_no_bodies_or_paths(target, store, tmp_path):
    _running, port = target()
    champion = store.champion()
    adapter = adapter_for(port)
    case = EvalCase("c", "g", "s", (), {"text": "PUBLIC-CANARY"}, {}, {})
    environment = StaticInputEnvironment()
    journal_path = tmp_path / "journal"
    instance_id = new_instance_id("run")

    async def run():
        preflight = await adapter.preflight(champion.expected_attestation())
        semantic = preflight.semantic
        identity = RunIdentityV1(
            dict(
                profile="rollout_measurement",
                candidate_digest=champion.digest,
                target=dict(
                    contract_digest=semantic.contract_digest,
                    dependency_digest=semantic.dependency_digest,
                    settings_hash=semantic.settings_hash,
                    seed_mode=semantic.seed_mode,
                    protocol_version=semantic.protocol_version,
                    runtime_ref_digest=semantic.runtime_ref_digest,
                ),
                environment=dict(
                    kind=environment.identity.kind, digest=environment.identity.digest
                ),
                scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
                evaluation_set_id="label",
                corpus_digest=corpus_digest([case]),
                metric_plan_digest="a" * 64,
                schedule=[dict(case_id="c", sample_id="s0", repeat=1, requested_seed=7)],
                budget=dict(max_turns=2, timeout_seconds=10.0),
                max_concurrency=1,
                max_scorer_concurrency=1,
                materialization_strategy=STRATEGY,
            )
        )
        journal = Journal.create(journal_path, "run", identity, [case], version_resolver=VR)
        try:
            sink = JournalRolloutAdapter(journal)
            instance = await materialize_target(adapter, champion, STRATEGY, instance_id, sink)
            trajectory = await durable_rollout(
                journal=journal,
                identity=identity,
                corpus=[case],
                case=case,
                sample=RolloutSample("s0", champion.digest, 7),
                instance=instance,
                environment=environment,
                scorer=SimpleNamespace(identity=ScorerIdentity("judge", "scorer")),
                budget=RolloutBudget(2, 10.0),
                expected_target=semantic,
            )
            await release_target(
                adapter, instance_id, champion.digest, STRATEGY, JournalRolloutAdapter(journal)
            )
            return trajectory
        finally:
            journal.close()

    trajectory = asyncio.run(run())
    assert trajectory.terminal == "completed"
    raw = (journal_path / "events.jsonl").read_bytes()
    assert b"PUBLIC-CANARY" not in raw
    assert str(champion.root).encode() not in raw
    assert b"champion skill" not in raw
    assert instance_id.encode() in raw


# -- preflight ----------------------------------------------------------------


def refusal(adapter, expected=None):
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(adapter.preflight(expected or ExpectedAttestation("a" * 64, "b" * 64)))
    return caught.value


@pytest.mark.parametrize(
    ("target_kwargs", "binding_kwargs", "policy", "code"),
    [
        ({"seed_mode": "unpaired"}, {}, POLICY, "pin_mismatch"),
        ({}, {"contract_digest": "other-contract"}, POLICY, "pin_mismatch"),
        ({}, {"settings_hash": "other-settings"}, POLICY, "pin_mismatch"),
        ({}, {"max_response_bytes": 4096}, POLICY, "limits_incompatible"),
        ({}, {"min_input_bytes": 1 << 30}, POLICY, "limits_incompatible"),
        ({}, {}, ArtifactPolicyV1(("build/**",), ("uv.lock",), ("skills/**",)), "policy_mismatch"),
    ],
)
def test_preflight_refuses_a_target_that_does_not_match_the_binding(
    target, target_kwargs, binding_kwargs, policy, code
):
    _running, port = target(**target_kwargs)
    adapter = adapter_for(port, policy=policy, **binding_kwargs)
    assert refusal(adapter).code == code


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"ready": False}, "not_ready"),
        ({"protocol_version": "3"}, "invalid_response"),
        ({"boot_id": ""}, "invalid_response"),
        ({"allowed_artifact_root": "relative/runs"}, "invalid_response"),
        ({"allowed_artifact_root": "."}, "invalid_response"),
        (
            {
                "capabilities": dict(
                    health_body()["capabilities"], materialization=["filesystem_overlay"]
                )
            },
            "strategy_unsupported",
        ),
        (
            {"capabilities": dict(health_body()["capabilities"], attests=["dependency_digest"])},
            "attestation_missing",
        ),
    ],
)
def test_preflight_refuses_a_nonconforming_health_body(canned, overrides, code):
    _replies, port = canned({"/health": (200, health_body(**overrides))})
    assert refusal(adapter_for(port)).code == code


def test_preflight_reports_the_wire_code_of_a_typed_refusal(canned):
    _replies, port = canned({"/health": (503, wire_error("not_ready", "boot", True))})
    assert refusal(adapter_for(port)).code == "not_ready"


def test_preflight_refuses_a_redirect(canned):
    _replies, port = canned({"/health": (302, b"", {"Location": "/elsewhere"})})
    assert refusal(adapter_for(port)).code == "invalid_response"


def test_a_target_that_never_answers_is_a_transport_error():
    adapter = adapter_for(unused_port(), timeouts=Timeouts(0.5, 0.5))
    assert refusal(adapter).code == "transport_error"


def test_the_adapter_refuses_every_call_before_preflight(target, store):
    _running, port = target()
    adapter = adapter_for(port)
    champion = store.champion()
    with pytest.raises(AdapterNotPreflighted) as caught:
        _ = adapter.capabilities
    assert caught.value.code == "not_preflighted"
    assert isinstance(caught.value, RolloutInfrastructureError)
    with pytest.raises(AdapterNotPreflighted):
        _ = adapter.allowed_artifact_root
    with pytest.raises(AdapterNotPreflighted):
        asyncio.run(adapter.materialize(champion, STRATEGY, new_instance_id("run")))
    with pytest.raises(AdapterNotPreflighted):
        asyncio.run(adapter.release(new_instance_id("run")))
    with pytest.raises(AttributeError):
        _ = adapter.no_such_attribute


# -- materialize --------------------------------------------------------------


def materialized(adapter, champion, instance_id=None, **kwargs):
    async def run():
        await adapter.preflight(champion.expected_attestation())
        return await adapter.materialize(
            champion, kwargs.get("strategy", STRATEGY), instance_id or new_instance_id("run")
        )

    return run


@pytest.mark.parametrize(
    ("instance_id", "strategy", "code"),
    [
        ("other-run-" + "0" * 32, STRATEGY, "invalid_request"),
        ("run-not-a-uuid", STRATEGY, "invalid_request"),
        (None, "filesystem_overlay", "strategy_unsupported"),
    ],
)
def test_materialize_refuses_a_bad_request_before_calling_the_target(
    target, store, instance_id, strategy, code
):
    _running, port = target()
    adapter = adapter_for(port)
    champion = store.champion()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(materialized(adapter, champion, instance_id, strategy=strategy)())
    assert caught.value.code == code
    assert health_of(port)["instances"] == []


def test_materialize_refuses_a_candidate_root_outside_the_allowed_root(target, tmp_path):
    """The target would answer 403 `root_not_allowed`; the adapter refuses first,
    so a store built outside the root health published never puts its path on the
    wire and the only exchange is the preflight."""
    _running, port = target()
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    seed_root = outside / "seed"
    write(seed_root, "skills/SKILL.md", SKILL)
    write(seed_root, "uv.lock", b"lock")
    seen = []
    adapter = adapter_for(port, recorder=seen.append)
    with ArtifactStore.create(
        outside / "run", seed_root, POLICY, parallel_attempts=1, version_resolver=VR
    ) as opened:
        with pytest.raises(RolloutInfrastructureError) as caught:
            asyncio.run(materialized(adapter, opened.champion())())
    assert caught.value.code == "root_not_allowed"
    assert [exchange.path for exchange in seen] == [f"{PREFIX}/health"]
    assert health_of(port)["instances"] == []


def test_materialize_refuses_a_candidate_that_left_the_preflighted_identity(
    target, store, tmp_path, allowed
):
    """ADR-0007: preflight freezes the declared identity and materialize proves
    it, so a Candidate sealed against another dependency set is refused before
    the target is ever asked to load it — the returned identity would otherwise
    contradict the one S1 already journalled."""
    _running, port = target()
    other_seed = Path(os.path.realpath(tmp_path)) / "other-seed"
    write(other_seed, "skills/SKILL.md", SKILL)
    write(other_seed, "scripts/run.sh", b"#!/bin/sh\n", executable=True)
    write(other_seed, "uv.lock", b"another lock")
    seen = []
    adapter = adapter_for(port, recorder=seen.append)
    with ArtifactStore.create(
        allowed / "other", other_seed, POLICY, parallel_attempts=1, version_resolver=VR
    ) as other:
        foreign = other.champion()
        assert foreign.dependency_digest != store.champion().dependency_digest

        async def run():
            await adapter.preflight(store.champion().expected_attestation())
            return await adapter.materialize(foreign, STRATEGY, new_instance_id("run"))

        with pytest.raises(AttestationError) as caught:
            asyncio.run(run())
    assert caught.value.code == "attestation_mismatch"
    # Refused before any wire call: the only exchange is the preflight.
    assert [exchange.path for exchange in seen] == [f"{PREFIX}/health"]
    assert health_of(port)["instances"] == []


@pytest.mark.parametrize(
    ("flag", "code"),
    [("lie_candidate_digest", "attestation_mismatch"), ("drop_attested", "attestation_missing")],
)
def test_a_lying_target_fails_attestation_at_materialize(target, store, flag, code):
    running, port = target()
    running.fault(**{flag: True})
    adapter = adapter_for(port)
    with pytest.raises(AttestationError) as caught:
        asyncio.run(materialized(adapter, store.champion())())
    assert caught.value.code == code


def test_a_target_that_starts_lying_fails_attestation_at_identity(target, store):
    running, port = target()
    adapter = adapter_for(port)
    instance = asyncio.run(materialized(adapter, store.champion())())
    assert asyncio.run(instance.identity()).instance.loaded_candidate_digest == (
        store.champion().digest
    )
    running.fault(lie_candidate_digest=True)
    with pytest.raises(AttestationError) as caught:
        asyncio.run(instance.identity())
    assert caught.value.code == "attestation_mismatch"


def test_a_restarted_target_changes_the_epoch(target, store, servers):
    _running, port = target()
    champion = store.champion()
    adapter = adapter_for(port)
    asyncio.run(adapter.preflight(champion.expected_attestation()))
    servers.stop(port)
    _restarted, same_port = target(port=port)
    assert same_port == port
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(adapter.materialize(champion, STRATEGY, new_instance_id("run")))
    assert caught.value.code == "target_epoch_changed"


def test_release_of_an_unknown_instance_succeeds(target, store):
    _running, port = target()
    adapter = adapter_for(port)
    asyncio.run(adapter.preflight(store.champion().expected_attestation()))
    asyncio.run(adapter.release(new_instance_id("run")))


def test_recovery_releases_an_instance_whose_owner_crashed(target, store):
    _running, port = target()
    champion = store.champion()
    journal = Recorder()
    instance_id = new_instance_id("run")

    async def crash():
        adapter = adapter_for(port)
        await adapter.preflight(champion.expected_attestation())
        await materialize_target(adapter, champion, STRATEGY, instance_id, journal)

    asyncio.run(crash())
    assert health_of(port)["instances"] == [instance_id]

    async def recover():
        fresh = adapter_for(port)
        await fresh.preflight(champion.expected_attestation())
        return await recover_target_instances(fresh, tuple(journal.records), journal)

    assert asyncio.run(recover()) == (instance_id,)
    assert health_of(port)["instances"] == []


# -- sessions and seeds -------------------------------------------------------


def instance_over(port, seed_mode="exact", description=None, components=None):
    instance_id = "run-" + "0" * 32
    return HttpInstance(
        HttpTransport(binding_for(port, seed_mode=seed_mode)),
        instance_id,
        bound_identity(seed_mode),
        description,
        components,
    )


def session_path(seed_mode="exact"):
    return f"/instances/run-{'0' * 32}/sessions"


@pytest.mark.parametrize(
    ("seed_mode", "seed", "body"),
    [
        ("exact", 7, dict(session_id="s", requested_seed=7, effective_seed=8)),
        ("exact", 7, dict(session_id="s", requested_seed=6, effective_seed=7)),
        ("unpaired", None, dict(session_id="s", requested_seed=3, effective_seed=3)),
        ("unpaired", None, dict(session_id="s", requested_seed=None, effective_seed=None)),
        ("none", None, dict(session_id="s", requested_seed=None, effective_seed=3)),
    ],
)
def test_a_session_that_breaks_the_seed_rules_is_an_invalid_response(canned, seed_mode, seed, body):
    _replies, port = canned({session_path(): (200, body)})
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(instance_over(port, seed_mode).open({}, seed, "req-1"))
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("seed_mode", "seed", "body"),
    [
        ("exact", 7, dict(session_id="s", requested_seed=7, effective_seed=7)),
        ("unpaired", None, dict(session_id="s", requested_seed=None, effective_seed=12)),
        ("none", None, dict(session_id="s", requested_seed=None, effective_seed=None)),
    ],
)
def test_a_session_that_keeps_the_seed_rules_is_accepted(canned, seed_mode, seed, body):
    _replies, port = canned({session_path(): (200, body)})
    session = asyncio.run(instance_over(port, seed_mode).open({}, seed, "req-1"))
    assert (session.session_id, session.requested_seed, session.effective_seed) == (
        body["session_id"],
        body["requested_seed"],
        body["effective_seed"],
    )


@pytest.mark.parametrize("seed_mode", ["unpaired", "none"])
def test_a_seedless_target_opens_a_seedless_session(target, store, seed_mode):
    _running, port = target(seed_mode=seed_mode)
    champion = store.champion()
    adapter = adapter_for(port, seed_mode=seed_mode)

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, new_instance_id("run"))
        return await instance.open({}, None, "req-open")

    session = asyncio.run(run())
    assert session.requested_seed is None
    assert (session.effective_seed is None) == (seed_mode == "none")


def test_an_unpaired_session_with_no_effective_seed_is_an_invalid_response(target, store):
    """`unpaired` means the target picked the seed and reports which one: a null
    `effective_seed` is malformed provenance, not a seedless session, and the
    adapter refuses it rather than leaving conformance to notice later."""
    running, port = target(seed_mode="unpaired")
    running.fault(drop_effective_seed=True)
    champion = store.champion()
    adapter = adapter_for(port, seed_mode="unpaired")

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, new_instance_id("run"))
        return await instance.open({}, None, "req-open")

    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(run())
    assert caught.value.code == "invalid_response"


# -- the transport ------------------------------------------------------------


def test_the_recorder_sees_request_bytes_and_non_200_exchanges(canned):
    body = wire_error("unknown_instance", "instance_id")
    _replies, port = canned({"/instances/x": (404, body), "/instances": (200, {"ok": True})})
    seen: list[Exchange] = []
    transport = HttpTransport(binding_for(port), recorder=seen.append)
    status, _raw, media = asyncio.run(transport.request("DELETE", f"{PREFIX}/instances/x", None))
    assert (status, media) == (404, "application/json")
    assert (seen[0].method, seen[0].path, seen[0].status) == (
        "DELETE",
        f"{PREFIX}/instances/x",
        404,
    )
    assert seen[0].request_bytes == b""
    assert json.loads(seen[0].response_bytes) == body
    asyncio.run(transport.request("POST", f"{PREFIX}/instances", {"b": 1, "a": 2}))
    assert seen[1].request_bytes == b'{"a":2,"b":1}'


def test_a_stalled_target_is_a_callee_timeout(target):
    running, port = target()
    running.fault(sleep_seconds=0.5)
    transport = HttpTransport(binding_for(port, timeouts=Timeouts(0.2, 0.1)))
    started = time.monotonic()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    elapsed = time.monotonic() - started
    assert caught.value.code == "callee_timeout"
    assert caught.value.retryable is True
    assert elapsed < 0.8


def test_a_dripping_target_cannot_outlive_the_read_deadline(target):
    """One byte per 0.15 s would take ~90 s to deliver the health body; the
    worker must end within `connect_seconds + read_seconds` plus one chunk."""
    running, port = target()
    running.fault(slow_drip_seconds=0.15)
    transport = HttpTransport(binding_for(port, timeouts=Timeouts(0.2, 0.1)))
    started = time.monotonic()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    elapsed = time.monotonic() - started
    running.fault(slow_drip_seconds=0.0)
    assert caught.value.code == "callee_timeout"
    assert elapsed < 0.2 + 0.1 + 0.5


def test_a_target_dripping_its_headers_cannot_outlive_the_deadline(target):
    """`getresponse()` reads the status line and the headers with `readline`
    loops of raw `recv`s, long before `read1` exists to bound anything, and a
    per-byte drip faster than `read_seconds` never trips the socket timeout. Only
    the absolute deadline, consulted on every `recv`, ends this worker."""
    running, port = target()
    running.fault(slow_drip_headers_seconds=0.02)
    transport = HttpTransport(binding_for(port, timeouts=Timeouts(0.1, 0.1)))
    started = time.monotonic()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    elapsed = time.monotonic() - started
    running.fault(slow_drip_headers_seconds=0.0)
    assert caught.value.code == "callee_timeout"
    assert elapsed < 0.1 + 0.1 + 0.5


def test_a_target_dripping_its_chunk_framing_cannot_outlive_the_deadline(target):
    """The same argument for the third `readline` path: a chunked reply's
    size lines are framing, not body, so `read1` never returns them and only the
    deadline on the underlying `recv` bounds them."""
    running, port = target()
    running.fault(chunked_response=True)
    running.fault(slow_drip_seconds=0.05)
    transport = HttpTransport(binding_for(port, timeouts=Timeouts(0.1, 0.1)))
    started = time.monotonic()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    elapsed = time.monotonic() - started
    running.fault(chunked_response=False)
    running.fault(slow_drip_seconds=0.0)
    assert caught.value.code == "callee_timeout"
    assert elapsed < 0.1 + 0.1 + 0.5


def test_a_conforming_chunked_response_still_parses(target):
    """Bounding the framing must not break reading it: the same body delivered
    in several chunks is the same bytes."""
    running, port = target()
    transport = HttpTransport(binding_for(port))
    status, plain, _media = asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    assert status == 200
    running.fault(chunked_response=True)
    chunked_status, chunked, _media = asyncio.run(
        transport.request("GET", f"{PREFIX}/health", None)
    )
    running.fault(chunked_response=False)
    assert (chunked_status, chunked) == (200, plain)
    assert json.loads(chunked)["policy_digest"] == POLICY.digest


def test_the_response_limit_is_exact(target):
    _running, port = target()

    def health(limit):
        binding = binding_for(port, max_response_bytes=limit)
        return asyncio.run(HttpTransport(binding).request("GET", f"{PREFIX}/health", None))

    status, raw, _media = health(1 << 20)
    assert status == 200
    assert health(len(raw))[1] == raw
    with pytest.raises(RolloutInfrastructureError) as caught:
        health(len(raw) - 1)
    assert caught.value.code == "response_too_large"


def test_an_oversize_reply_aborts_the_call_and_leaves_ownership_recoverable(target, store):
    running, port = target(max_output_bytes=2048)
    champion = store.champion()
    adapter = adapter_for(port, max_response_bytes=2048)
    instance_id = new_instance_id("run")

    async def run():
        await adapter.preflight(champion.expected_attestation())
        running.fault(oversize_bytes=4096)
        with pytest.raises(RolloutInfrastructureError) as caught:
            await adapter.materialize(champion, STRATEGY, instance_id)
        assert caught.value.code == "response_too_large"
        running.fault(oversize_bytes=0)
        assert health_of(port)["instances"] == [instance_id]
        await adapter.release(instance_id)

    asyncio.run(run())
    assert health_of(port)["instances"] == []


class StallingEnvironment:
    """Arms the target's stall for the turn, then disarms it a moment later.

    `dispatch` reads `sleep_seconds` when a request arrives, so disarming after
    the turn is in flight leaves that one call stalled and every later call
    (the close, the final identity) fast — which is what makes the rollout's
    terminal observable instead of drowned in cascading timeouts.
    """

    identity = EnvironmentIdentity("stall", "stall")

    def __init__(self, target, seconds, disarm_after):
        self._target = target
        self._seconds = seconds
        self._disarm_after = disarm_after

    async def next(self, case, history):
        if history.turns:
            return None
        self._target.fault(sleep_seconds=self._seconds)
        asyncio.get_running_loop().call_later(self._disarm_after, self._disarm)
        return Message("hello")

    def _disarm(self):
        self._target.fault(sleep_seconds=0.0)


def test_a_stall_past_the_rollout_deadline_is_time_budget(target, store):
    running, port = target()
    champion = store.champion()
    adapter = adapter_for(port)
    journal = Recorder()
    case = EvalCase("c", "g", "s", (), {"text": "hello"}, {}, {})

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, new_instance_id("run"))
        return await rollout(
            case,
            RolloutSample("s0", champion.digest, 7),
            instance,
            StallingEnvironment(running, 1.0, 0.15),
            RolloutBudget(2, 0.4),
            journal,
        )

    trajectory = asyncio.run(run())
    assert trajectory.terminal == "time_budget"
    assert trajectory.history.turns == ()
    failed = [item for item in journal.records if item["event"] == "target_send_failed"]
    assert [item["error_class"] for item in failed] == ["deadline_timeout"]


def test_a_target_failed_turn_is_an_observation_not_an_error(canned):
    body = dict(
        events=[{"type": "assistant_message", "content": "no"}],
        usage=None,
        latency_ms=3,
        outcome="target_failed",
        truncated=True,
    )
    _replies, port = canned({f"{session_path()}/s/turns": (200, body)})
    session = HttpSession("s", 7, 7)
    result = asyncio.run(instance_over(port).send(session, Message("hi"), "req-1"))
    assert result.outcome == "target_failed"
    assert [event["type"] for event in result.events] == ["assistant_message", "output_truncated"]
    assert result.usage is None and result.latency_ms == 3


def _turn_body(**extra):
    return dict(
        events=[{"type": "assistant_message", "content": "ok"}],
        usage=None,
        latency_ms=3,
        outcome="ok",
        truncated=False,
        **extra,
    )


def test_a_turn_provenance_reaches_the_turn_result(canned):
    body = _turn_body(provenance={"used": ["skills/SKILL.md"]})
    _replies, port = canned({f"{session_path()}/s/turns": (200, body)})
    result = asyncio.run(instance_over(port).send(HttpSession("s", 7, 7), Message("hi"), "req-1"))
    assert result.provenance == {"used": ["skills/SKILL.md"]}


def test_a_turn_without_provenance_gives_none(canned):
    _replies, port = canned({f"{session_path()}/s/turns": (200, _turn_body())})
    result = asyncio.run(instance_over(port).send(HttpSession("s", 7, 7), Message("hi"), "req-1"))
    assert result.provenance is None


def test_a_later_get_never_replaces_the_captured_components(canned):
    created = {"skill": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    reported = dict(
        EPOCH,
        instance_id="run-" + "0" * 32,
        strategy=STRATEGY,
        loaded_candidate_digest="a" * 64,
        dependency_digest="b" * 64,
        attested=list(REQUIRED_ATTESTS),
        file_count=1,
        byte_count=1,
        components={"skill": {"sha256": "c" * 64, "sources": ["skills/SKILL.md"]}},
    )
    _replies, port = canned({f"/instances/run-{'0' * 32}": (200, reported)})
    instance = instance_over(port, components=created)
    assert instance.components == created
    assert asyncio.run(instance.identity()) == bound_identity()
    assert instance.components == created


def test_a_creation_body_components_reach_the_instance(target, store, monkeypatch):
    """The real creation path: `materialize` hands `reported.components` to the
    handle. The packaged target does not report components, so the parser the
    adapter uses is wrapped to add them to the body the target answered with."""
    import evolution.tree.target.http_adapter as adapter_module

    components = {"skill": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    real_parse = adapter_module.parse_instance

    def parse_with_components(obj):
        return real_parse(dict(obj, components=components))

    _running, port = target()
    with_components = adapter_for(port)
    monkeypatch.setattr(adapter_module, "parse_instance", parse_with_components)
    instance = asyncio.run(materialized(with_components, store.champion())())
    assert instance.components == components

    monkeypatch.setattr(adapter_module, "parse_instance", real_parse)
    plain = asyncio.run(materialized(adapter_for(port), store.champion())())
    assert plain.components is None


def test_an_instance_get_from_another_epoch_is_refused(canned):
    reported = dict(
        EPOCH,
        boot_id="boot-2",
        instance_id="run-" + "0" * 32,
        strategy=STRATEGY,
        loaded_candidate_digest="a" * 64,
        dependency_digest="b" * 64,
        attested=list(REQUIRED_ATTESTS),
        file_count=1,
        byte_count=1,
    )
    _replies, port = canned({f"/instances/run-{'0' * 32}": (200, reported)})
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(instance_over(port).identity())
    assert caught.value.code == "target_epoch_changed"


def test_an_instance_reporting_another_dependency_digest_has_left_the_epoch(canned):
    """Only `dependency_digest` differs from the frozen preflight identity: the
    same boot, the same contract, the same settings. ADR-0007 makes preflight the
    identity the journal froze, so this is an epoch change, not an attestation
    mismatch (the attestation is checked only after the epoch is proven)."""
    reported = dict(
        EPOCH,
        instance_id="run-" + "0" * 32,
        strategy=STRATEGY,
        loaded_candidate_digest="a" * 64,
        dependency_digest="c" * 64,
        attested=list(REQUIRED_ATTESTS),
        file_count=1,
        byte_count=1,
    )
    _replies, port = canned({f"/instances/run-{'0' * 32}": (200, reported)})
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(instance_over(port).identity())
    assert caught.value.code == "target_epoch_changed"


def test_a_typed_refusal_on_a_turn_carries_the_wire_code(target, store):
    _running, port = target()
    champion = store.champion()
    adapter = adapter_for(port)

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, new_instance_id("run"))
        session = await instance.open({}, 7, "req-open")
        await instance.close(session)
        await instance.send(session, Message("hi"), "req-send")

    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(run())
    assert caught.value.code == "unknown_session"


def _send_fails(canned, status, body, headers=None):
    path = f"{session_path()}/s/turns"
    _replies, port = canned({path: (status, body, headers or {})})
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(instance_over(port).send(HttpSession("s", 7, 7), Message("hi"), "req-1"))
    return caught.value


def test_a_foreign_media_5xx_on_send_is_upstream_unavailable(canned):
    """A `502` under `text/html` is a proxy or gateway answering, not the
    target's own typed refusal: the status is read before the media type, so
    this is `upstream_unavailable` (retryable), never `invalid_response`."""
    error = _send_fails(canned, 502, b"<html>bad gateway</html>", {"Content-Type": "text/html"})
    assert error.code == "upstream_unavailable"
    assert error.retryable is True


def test_a_429_with_a_json_non_wire_body_on_send_is_upstream_unavailable(canned):
    """JSON media, `429`, but no `"error"` key: still not a wire error."""
    error = _send_fails(canned, 429, {"retry_after": 1})
    assert error.code == "upstream_unavailable"
    assert error.retryable is True


def test_a_500_wire_error_on_send_keeps_its_code_and_retryable(canned):
    error = _send_fails(canned, 500, wire_error("internal", "x", True))
    assert error.code == "internal"
    assert error.retryable is True


def test_a_409_wire_error_on_send_is_not_retryable(canned):
    error = _send_fails(canned, 409, wire_error("request_conflict", "x", False))
    assert error.code == "request_conflict"
    assert error.retryable is False


def test_a_200_with_a_malformed_body_on_send_is_still_invalid_response(canned):
    error = _send_fails(canned, 200, b"not-json")
    assert error.code == "invalid_response"


def test_a_fast_dripping_target_cannot_outlive_the_read_deadline(target):
    """The mirror of the test above, and the one that proves the *absolute*
    bound: a drip faster than `read_seconds` never trips the socket timeout, so
    only re-checking the deadline after every raw read ends the worker. The
    inflated body is thousands of bytes — minutes at this drip rate."""
    running, port = target()
    running.fault(oversize_bytes=3000)
    running.fault(slow_drip_seconds=0.005)
    transport = HttpTransport(binding_for(port, timeouts=Timeouts(0.1, 0.1)))
    started = time.monotonic()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    elapsed = time.monotonic() - started
    running.fault(slow_drip_seconds=0.0)
    running.fault(oversize_bytes=0)
    assert caught.value.code == "callee_timeout"
    assert elapsed < 0.1 + 0.1 + 0.5


def test_release_tolerates_only_a_404_that_says_the_instance_is_gone(canned):
    instance_id = "run-" + "0" * 32
    replies = {"/health": (200, health_body())}
    _replies, port = canned(replies)
    adapter = adapter_for(port)
    asyncio.run(adapter.preflight(ExpectedAttestation("a" * 64, "b" * 64)))
    replies[f"/instances/{instance_id}"] = (404, wire_error("unknown_instance"))
    asyncio.run(adapter.release(instance_id))
    # A 404 from a mis-addressed path says nothing about the instance, so
    # reporting success would leave it alive and owned.
    replies[f"/instances/{instance_id}"] = (404, wire_error("invalid_request"))
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(adapter.release(instance_id))
    assert caught.value.code == "invalid_request"


def test_close_tolerates_only_a_404_that_says_the_session_is_gone(canned):
    path = f"{session_path()}/s"
    replies = {path: (404, wire_error("unknown_session"))}
    _replies, port = canned(replies)
    instance = instance_over(port)
    asyncio.run(instance.close(HttpSession("s", 7, 7)))
    replies[path] = (404, wire_error("unknown_instance"))
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(instance.close(HttpSession("s", 7, 7)))
    assert caught.value.code == "unknown_instance"


# -- the closed delete bodies ---------------------------------------

INSTANCE_ID = "run-" + "0" * 32

BAD_RELEASE_BODIES = [
    {"instance_id": "run-" + "1" * 32, "released": True},
    {"instance_id": INSTANCE_ID},
    {"instance_id": INSTANCE_ID, "released": "yes"},
    {"instance_id": INSTANCE_ID, "released": True, "extra": 1},
    {},
    b"not-json",
]


def released_adapter(canned, replies):
    _replies, port = canned(replies)
    adapter = adapter_for(port)
    asyncio.run(adapter.preflight(ExpectedAttestation("a" * 64, "b" * 64)))
    return adapter


@pytest.mark.parametrize("body", BAD_RELEASE_BODIES)
def test_release_refuses_a_200_that_does_not_prove_the_transition(canned, body):
    """A 200 whose body is empty, malformed, wrongly shaped or addressed to
    another instance never showed that the owned instance was released."""
    replies = {"/health": (200, health_body())}
    adapter = released_adapter(canned, replies)
    replies[f"/instances/{INSTANCE_ID}"] = (200, body)

    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(adapter.release(INSTANCE_ID))
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("released", [True, False])
def test_release_accepts_the_closed_release_body(canned, released):
    """`released: false` is the repeat: the instance was already gone, which is
    the outcome the caller wanted."""
    replies = {"/health": (200, health_body())}
    adapter = released_adapter(canned, replies)
    replies[f"/instances/{INSTANCE_ID}"] = (200, {"instance_id": INSTANCE_ID, "released": released})

    asyncio.run(adapter.release(INSTANCE_ID))


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": "other", "closed": True},
        {"session_id": "s"},
        {"session_id": "s", "closed": "yes"},
        {"session_id": "s", "closed": True, "extra": 1},
        b"not-json",
    ],
)
def test_close_refuses_a_200_that_does_not_prove_the_transition(canned, body):
    path = f"{session_path()}/s"
    replies = {path: (200, body)}
    _replies, port = canned(replies)
    instance = instance_over(port)

    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(instance.close(HttpSession("s", 7, 7)))
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("closed", [True, False])
def test_close_accepts_the_closed_session_body(canned, closed):
    path = f"{session_path()}/s"
    _replies, port = canned({path: (200, {"session_id": "s", "closed": closed})})
    instance = instance_over(port)

    asyncio.run(instance.close(HttpSession("s", 7, 7)))


# -- the response media type ----------------------------------------


@pytest.mark.parametrize("media", ["text/plain", "application/octet-stream", "", None])
def test_preflight_refuses_a_body_that_is_not_json_media(canned, media):
    """The contract: every response carries `Content-Type: application/json`. A target
    answering valid JSON under another media type is not speaking this wire."""
    _replies, port = canned({"/health": (200, health_body(), {"Content-Type": media})})
    assert refusal(adapter_for(port)).code == "invalid_response"


@pytest.mark.parametrize(
    "media", ["application/json", "application/json; charset=utf-8", "APPLICATION/JSON "]
)
def test_preflight_accepts_json_media_with_parameters(canned, media):
    _replies, port = canned({"/health": (200, health_body(), {"Content-Type": media})})
    preflight = asyncio.run(adapter_for(port).preflight(ExpectedAttestation("a" * 64, "b" * 64)))
    assert preflight.epoch == "boot-1"


def test_a_typed_refusal_under_a_foreign_media_type_is_an_upstream_failure(canned):
    """The error body is held to the same media type as any other response, so
    a 5xx under the wrong media type is unreadable as a wire error at all — a
    proxy or gateway speaking for the target, not the target's own typed
    refusal — and becomes `upstream_unavailable` (retryable), never the wire
    code that happens to sit, unread, in the body."""
    _replies, port = canned(
        {"/health": (503, wire_error("not_ready"), {"Content-Type": "text/html"})}
    )
    caught = refusal(adapter_for(port))
    assert caught.code == "upstream_unavailable"
    assert caught.retryable is True


def test_the_recorder_sees_the_response_media_type(canned):
    _replies, port = canned({"/health": (200, health_body(), {"Content-Type": "text/plain"})})
    exchanges: list[Exchange] = []
    adapter = adapter_for(port, recorder=exchanges.append)

    assert refusal(adapter).code == "invalid_response"
    # Recorded before the refusal: the wire suite reads the header from here.
    assert [exchange.content_type for exchange in exchanges] == ["text/plain"]


def test_the_reference_target_answers_as_json_media(target):
    running, port = target()
    transport = HttpTransport(binding_for(port))

    _status, _raw, media = asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    assert media == "application/json"

    running.fault(response_content_type="text/plain")
    _status, _raw, media = asyncio.run(transport.request("GET", f"{PREFIX}/health", None))
    assert media == "text/plain"


# -- the target call ledger (ADR-0014) -----------------------------------------


def test_an_adapter_with_a_ledger_accounts_its_whole_lifecycle(target, store):
    from evolution.tree.target.target_calls import TargetCallLedger

    _running, port = target()
    ledger = TargetCallLedger()
    adapter = HttpSessionAdapter(binding_for(port), POLICY, run_id="run", ledger=ledger)
    champion = store.champion()
    expected = champion.expected_attestation()

    async def lifecycle():
        await adapter.preflight(expected)
        instance_id = new_instance_id("run")
        instance = await adapter.materialize(champion, STRATEGY, instance_id)
        session = await instance.open({"q": 1}, 7, "open-1")
        await instance.send(session, Message("hi"), "turn-1")
        await instance.close(session)
        await adapter.release(instance_id)
        return instance_id

    instance_id = asyncio.run(lifecycle())
    ledger.close()
    snap = ledger.report()
    assert snap.namespace == "run"
    assert (snap.admitted, snap.completed, snap.bound_transports) == (5, 5, 1)
    assert snap.quiescent is True and snap.records == ()
    assert instance_id.startswith("run-")


def test_an_adapter_refuses_a_ledger_bound_to_another_namespace(target):
    from evolution.tree.target.target_calls import TargetCallLedger

    _running, port = target()
    ledger = TargetCallLedger()
    ledger.bind_namespace("other")
    with pytest.raises(ValueError):
        HttpSessionAdapter(binding_for(port), POLICY, run_id="run", ledger=ledger)


# -- the backend's instance description (PDR-0014) ----------------------------


class _DescribingBackend(ReferenceBackend):
    """The reference backend whose instances answer the optional `describe()`.

    The seam is the test's, not the packaged target's (PDR-0004, PDR-0011):
    `target_server` reaches the callback with `getattr`, so an attribute on the
    loaded instance is all a describing backend is.
    """

    def __init__(self, description):
        super().__init__()
        self._description = description

    def load(self, tree):
        loaded = super().load(tree)
        loaded.describe = lambda: self._description
        return loaded


def test_materialize_keeps_what_the_creation_body_described(target, store):
    """PDR-0014: the creation body is the only source, and the value it carried
    survives a later `GET /instances/{id}` untouched."""

    described = {"config_hash": "abc", "hermetic": True}
    running, port = target()
    # The same reference target, under a backend that describes itself: the
    # epoch the preflight froze is the one materialization proves.
    running.backend = _DescribingBackend(described)
    adapter = adapter_for(port)
    instance = asyncio.run(materialized(adapter, store.champion())())

    assert instance.description == described
    # The identity call re-reads the instance body -- and the description with
    # it. Core neither replaces nor compares it: a description two answers
    # could disagree about would be identity by another name (ADR-0007).
    asyncio.run(instance.identity())
    assert instance.description == described


def test_a_target_that_describes_nothing_materializes_a_description_of_none(target, store):
    _running, port = target()
    instance = asyncio.run(materialized(adapter_for(port), store.champion())())
    assert instance.description is None


def test_a_later_get_never_replaces_the_captured_description(canned):
    """The GET may report another description entirely; the handle keeps the one
    its creation body carried and the identity comparison never sees either."""

    reported = dict(
        EPOCH,
        instance_id="run-" + "0" * 32,
        strategy=STRATEGY,
        loaded_candidate_digest="a" * 64,
        dependency_digest="b" * 64,
        attested=list(REQUIRED_ATTESTS),
        file_count=1,
        byte_count=1,
        description={"config_hash": "drifted"},
    )
    _replies, port = canned({f"/instances/run-{'0' * 32}": (200, reported)})
    instance = instance_over(port, description={"config_hash": "created"})

    assert asyncio.run(instance.identity()) == bound_identity()
    assert instance.description == {"config_hash": "created"}


# -- the turn `reason` over the wire (PDR-0014, W19) --------------------------

REASON = {"category": "output_format", "code": "unparsable_json"}


class _ReasoningSession:
    """Answers every turn `candidate_failed` with a reason."""

    def __init__(self, seed):
        self.effective_seed = seed

    def turn(self, message):
        return BackendReply(
            TurnResult(
                ({"type": "assistant_message", "content": "{"},),
                Usage(prompt=1, completion=1),
                3,
                "candidate_failed",
                REASON,
            )
        )

    def close(self):
        return None


class _ReasoningInstance:
    def open(self, public, seed):
        return _ReasoningSession(seed)

    def close(self):
        return None


class _ReasoningBackend:
    """A test-module backend, not a seam in the packaged reference target
    (PDR-0004, PDR-0011): the reference backend answers `ok` and never sets a
    reason, so the server-emits/client-parses seam needs its own double."""

    def load(self, tree):
        return _ReasoningInstance()


def test_a_turn_reason_survives_the_round_trip(allowed, servers, store):
    """The one seam nobody owned: the packaged server emits `reason` and
    `HttpInstance.send` parses it back into `TurnResult.reason`."""

    champion = store.champion()
    server = TargetServer(
        _ReasoningBackend(),
        allowed_artifact_root=allowed,
        policy=POLICY,
        seed_mode="exact",
        target_id="reasoning",
        contract_digest="reference-contract-1",
        settings_hash="reference-settings-1",
        runtime_ref_digest="reference-runtime-1",
    )
    port = servers.run(serve(server, port=0))
    adapter = adapter_for(port)

    async def run():
        await adapter.preflight(champion.expected_attestation())
        instance = await adapter.materialize(champion, STRATEGY, new_instance_id("run"))
        session = await instance.open({"q": "x"}, 7, "s-1")
        return await instance.send(session, Message("hi"), "req-1")

    try:
        result = asyncio.run(run())
    finally:
        server.close()
    assert result.outcome == "candidate_failed"
    assert result.reason == REASON


def test_a_materialize_resend_reuses_the_callers_request_id(target, store):
    """I1: `materialize_target` mints the creation `request_id` once and passes
    it to every attempt, so a resend is a replay of one creation, not a second
    one; without the keyword the adapter still mints its own per call."""

    _running, port = target()
    champion = store.champion()
    exchanges: list[Exchange] = []
    adapter = adapter_for(port, recorder=exchanges.append)
    instance_id = new_instance_id("run")

    async def run():
        await adapter.preflight(champion.expected_attestation())
        first = await adapter.materialize(champion, STRATEGY, instance_id, request_id="create-1")
        again = await adapter.materialize(champion, STRATEGY, instance_id, request_id="create-1")
        await adapter.release(instance_id)
        other_id = new_instance_id("run")
        await adapter.materialize(champion, STRATEGY, other_id)
        await adapter.materialize(champion, STRATEGY, other_id)
        await adapter.release(other_id)
        return first, again

    first, again = asyncio.run(run())
    assert first.instance_id == again.instance_id == instance_id
    creations = [
        json.loads(item.request_bytes)
        for item in exchanges
        if (item.method, item.path) == ("POST", f"{PREFIX}/instances")
    ]
    assert [body["request_id"] for body in creations[:2]] == ["create-1", "create-1"]
    assert creations[2]["request_id"] != creations[3]["request_id"]
    assert health_of(port)["instances"] == []
