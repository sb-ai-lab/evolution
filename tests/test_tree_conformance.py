"""The generic and wire conformance suites and `evo tree conformance` (spec §6).

The generic suite is transport-neutral, so it is exercised twice: against an
in-memory promotion-capable adapter built on a *real* S2 store (so the digests it
attests are the ones the store published) and, through `run_conformance`, against
the reference target over real HTTP. The `Misbehave` hooks prove the suites fail
a non-conforming target on the intended check and nowhere else.
"""

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import evolution.tree.cli  # noqa: F401  (registers `evo tree` on the shared app)
from evolution.cli.app import app
from evolution.tree import conformance
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.artifacts.attestation import TargetPreflight
from evolution.tree.conformance import (
    _OUTCOMES,
    Check,
    ConformanceReport,
    Scenario,
    adapter_conformance,
    run_conformance,
)
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    InstanceProvenance,
    Message,
    RolloutInfrastructureError,
    SemanticIdentity,
    TargetIdentity,
    TurnResult,
)
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import PREFIX, REQUIRED_ATTESTS, new_instance_id
from evolution.tree.manifest.manifest_v2 import load_manifest_v2
from evolution.tree.target.reference_target import serve
from tests.reference_target_faults import FaultInjectingReferenceTarget
from tests.tree.artifacts.test_artifact_policy import write
from tests.tree.target.test_http_adapter import _Servers, health_of

pytestmark = pytest.mark.slow

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
STRATEGY = "artifact_root_injection"

GENERIC_NAMES = [
    "health",
    "store",
    "instance_attested",
    "instance_second",
    "session_opened",
    "turn_shape",
    "turn_idempotent",
    "session_closed",
    "identity_stable",
    "released",
]
WIRE_NAMES = [
    "health_wire",
    "instance_conflict",
    "turn_truncated_flag",
    "turn_idempotent_wire",
    "session_unknown",
    "released_wire",
    "limits",
]


# -- fixtures -----------------------------------------------------------------


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
def seed_root(tmp_path):
    root = Path(os.path.realpath(tmp_path)) / "seed"
    write(root, "skills/SKILL.md", b"champion skill\nsecond line\n")
    write(root, "scripts/run.sh", b"#!/bin/sh\n", executable=True)
    write(root, "uv.lock", b"lock")
    return root


@pytest.fixture
def store(allowed, seed_root):
    opened = ArtifactStore.create(allowed / "generic", seed_root, POLICY, parallel_attempts=1)
    yield opened
    opened.close()


@pytest.fixture
def target(allowed, servers):
    """Start a reference target under the allowed root; returns `(target, port)`."""

    def _make(**kwargs):
        kwargs.setdefault("policy", POLICY)
        made = FaultInjectingReferenceTarget(allowed_artifact_root=allowed, **kwargs)
        return made, servers.run(serve(made))

    return _make


@pytest.fixture
def manifest_file(tmp_path, seed_root):
    def _make(port, **target_overrides):
        from tests.tree.manifest.test_manifest_v15 import v15_document

        payload = {
            **v15_document(),
            "seed_root": str(seed_root),
            "target": dict(
                {"base_url": f"http://127.0.0.1:{port}", "seed_mode": "exact"}, **target_overrides
            ),
            "artifacts": {
                "excludes": [],
                "dependency_paths": ["uv.lock"],
                "writable_paths": ["skills/**"],
            },
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    return _make


# -- an in-memory promotion-capable adapter -----------------------------------


class _FakeSession:
    def __init__(self, session_id: str, requested: int | None, effective: int | None) -> None:
        self.session_id = session_id
        self.requested_seed = requested
        self.effective_seed = effective


class _FakeInstance:
    """Attests exactly what the store published for the ref it materialized."""

    def __init__(
        self,
        instance_id: str,
        ref: Any,
        attested: tuple[str, ...],
        outcome: str = "ok",
    ) -> None:
        self.instance_id = instance_id
        self.target_identity = TargetIdentity(
            SemanticIdentity(
                "contract", ref.dependency_digest, "settings", "exact", "2", "runtime"
            ),
            InstanceProvenance("boot", ref.digest),
            frozenset(attested),
        )
        self.opened: list[dict[str, Any]] = []
        self.closed = 0
        self._outcome = outcome
        self._turns: dict[str, TurnResult] = {}

    async def identity(self) -> TargetIdentity:
        return self.target_identity

    async def open(self, case_public, seed, request_id):
        self.opened.append(dict(case_public))
        return _FakeSession(f"session-{len(self.opened)}", seed, seed)

    async def send(self, session, message: Message, request_id: str) -> TurnResult:
        if request_id not in self._turns:
            self._turns[request_id] = TurnResult(
                ({"type": "assistant_message", "content": message.content.upper()},),
                Usage(1, 1, 0, 2),
                3,
                self._outcome,
            )
        return self._turns[request_id]

    async def close(self, session) -> None:
        self.closed += 1


class _FakeAdapter:
    """The R1 in-memory adapter extended with the `preflight` R3 requires."""

    def __init__(
        self,
        *,
        max_instances: int = 2,
        attested: tuple[str, ...] = REQUIRED_ATTESTS,
        outcome: str = "ok",
    ):
        self.capabilities = AdapterCapabilities(
            "exact", 4, True, frozenset({STRATEGY}), max_instances
        )
        self._attested = attested
        self._outcome = outcome
        self.instances: dict[str, _FakeInstance] = {}
        self.released: list[str] = []

    async def preflight(self, expected) -> TargetPreflight:
        semantic = SemanticIdentity(
            "contract", expected.dependency_digest, "settings", "exact", "2", "runtime"
        )
        return TargetPreflight(semantic, self.capabilities, "boot")

    async def materialize(self, candidate, strategy, instance_id):
        candidate.verify()
        instance = _FakeInstance(instance_id, candidate, self._attested, self._outcome)
        self.instances[instance_id] = instance
        return instance

    async def release(self, instance_id: str) -> None:
        self.instances.pop(instance_id, None)
        self.released.append(instance_id)


class _NoPreflightAdapter:
    """A plain R1 adapter: not promotion-capable, so it cannot pass check 1."""

    capabilities = AdapterCapabilities("exact", 4, True, frozenset({STRATEGY}), 1)

    async def materialize(self, candidate, strategy, instance_id):  # pragma: no cover - unreached
        raise AssertionError("the suite must stop before materializing")

    async def release(self, instance_id: str) -> None:  # pragma: no cover - unreached
        raise AssertionError("the suite must stop before releasing")


def failures(checks) -> dict[str, str]:
    return {check.name: check.got for check in checks if not check.ok}


# -- the generic suite --------------------------------------------------------


def test_generic_suite_passes_against_a_promotion_capable_adapter(store):
    adapter = _FakeAdapter()

    checks, scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    assert [check.name for check in checks] == GENERIC_NAMES
    assert {check.suite for check in checks} == {"G"}
    assert failures(checks) == {}
    assert isinstance(scenario, Scenario)
    assert scenario.run_id == "conf"
    assert scenario.instance_a.startswith("conf-")
    assert scenario.instance_b is not None and scenario.instance_b != scenario.instance_a
    assert scenario.session_id == "session-1"
    assert scenario.root_a == store.champion().root
    assert scenario.root_b is not None
    assert scenario.expected == store.champion().expected_attestation()
    assert scenario.turn_exchange is None
    assert adapter.released == [scenario.instance_a, scenario.instance_b, scenario.instance_a]
    assert adapter.instances == {}


def test_generic_suite_skips_the_second_instance_when_capacity_is_one(store):
    adapter = _FakeAdapter(max_instances=1)

    checks, scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    second = next(check for check in checks if check.name == "instance_second")
    assert second.ok and second.got == "skipped: max_instances=1"
    assert scenario.instance_b is None
    assert failures(checks) == {}


def test_generic_suite_stops_when_the_adapter_has_no_preflight(store):
    checks, scenario = asyncio.run(adapter_conformance(_NoPreflightAdapter(), store, run_id="conf"))

    assert [check.name for check in checks] == ["health"]
    assert checks[0].ok is False
    assert checks[0].expected == "promotion-capable adapter exposes preflight"
    assert scenario.instance_a == ""


def test_conformance_admits_the_three_turn_outcomes():
    assert _OUTCOMES == frozenset({"ok", "target_failed", "candidate_failed"})


@pytest.mark.parametrize("outcome", ["ok", "target_failed", "candidate_failed"])
def test_conformance_passes_against_a_target_answering_each_outcome(store, outcome):
    # Spec section 7.11: a *test-suite* backend, never the packaged reference
    # target — PDR-0004 and PDR-0011 keep reachable fault injection out of the
    # package. `_FakeAdapter` is the in-memory adapter check 6G-10G exercises
    # through `adapter_conformance`, so it is the cheapest real target for the
    # closed outcome set `_check_turn` (via check `turn_shape`) admits.
    adapter = _FakeAdapter(outcome=outcome)

    checks, _scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    assert failures(checks) == {}


def _semantic(digest, **overrides):
    """A valid `SemanticIdentity`, then forced past its own validation.

    `SemanticIdentity` refuses a blank field at construction, so a target that
    declares one can only be modelled by writing it in afterwards — which is
    exactly what check 1 must catch rather than trust.
    """
    semantic = SemanticIdentity("contract", digest, "settings", "exact", "2", "runtime")
    for name, value in overrides.items():
        object.__setattr__(semantic, name, value)
    return semantic


def _foreign_digest(adapter, expected):
    return TargetPreflight(_semantic("f" * 64), adapter.capabilities, "boot")


def _blank_settings_hash(adapter, expected):
    semantic = _semantic(expected.dependency_digest, settings_hash="   ")
    return TargetPreflight(semantic, adapter.capabilities, "boot")


def _no_semantic(adapter, expected):
    preflight = TargetPreflight(_semantic(expected.dependency_digest), adapter.capabilities, "boot")
    object.__setattr__(preflight, "semantic", SimpleNamespace(dependency_digest="d"))
    return preflight


def _blank_epoch(adapter, expected):
    preflight = TargetPreflight(_semantic(expected.dependency_digest), adapter.capabilities, "boot")
    object.__setattr__(preflight, "epoch", "   ")
    return preflight


def _foreign_capabilities(adapter, expected):
    other = AdapterCapabilities("exact", 4, True, frozenset({STRATEGY}), 9)
    return TargetPreflight(_semantic(expected.dependency_digest), other, "boot")


def _not_a_preflight(adapter, expected):
    return SimpleNamespace(
        semantic=_semantic(expected.dependency_digest),
        capabilities=adapter.capabilities,
        epoch="boot",
    )


class _BadPreflightAdapter(_FakeAdapter):
    """Promotion-capable by shape only: its preflight returns something that is
    not the complete declared identity S1 must freeze (ADR-0007)."""

    def __init__(self, make, **kwargs):
        super().__init__(**kwargs)
        self._make = make

    async def preflight(self, expected):
        return self._make(self, expected)


@pytest.mark.parametrize(
    ("make", "got"),
    [
        (_foreign_digest, "preflight.semantic.dependency_digest"),
        (_blank_settings_hash, "preflight.semantic.settings_hash"),
        (_no_semantic, "preflight.semantic"),
        (_blank_epoch, "preflight.epoch"),
        (_foreign_capabilities, "preflight.capabilities"),
        (_not_a_preflight, "preflight is not a TargetPreflight"),
    ],
)
def test_generic_suite_fails_health_when_preflight_declares_no_identity(store, make, got):
    """ADR-0007: check 1 passes only for a real `TargetPreflight` carrying the
    whole `SemanticIdentity`, the epoch and the adapter's frozen capabilities."""
    adapter = _BadPreflightAdapter(make)

    checks, scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    assert failures(checks) == {"health": got}
    # The suite stops at check 1: nothing is materialized and nothing released.
    assert [check.name for check in checks] == ["health", "store"]
    assert scenario.instance_a == ""
    assert adapter.instances == {} and adapter.released == []


class _DriftingIdentityAdapter(_FakeAdapter):
    """Attests exactly the published digests, then reports an identity whose
    semantic fields are not the ones its own preflight froze (ADR-0007)."""

    async def materialize(self, candidate, strategy, instance_id):
        instance = await super().materialize(candidate, strategy, instance_id)
        reported = instance.target_identity
        instance.target_identity = TargetIdentity(
            SemanticIdentity(
                "contract", candidate.dependency_digest, "drifted", "exact", "2", "runtime"
            ),
            reported.instance,
            reported.attested,
        )
        return instance


def test_generic_suite_fails_3g_when_the_identity_left_the_preflighted_semantic(store):
    """ADR-0007: materialization proves the identity preflight froze, so 3G
    compares the attested `semantic` with `preflight.semantic` and not only with
    what the store published. This adapter drifts on every materialization, so
    the second instance reports it too."""
    adapter = _DriftingIdentityAdapter()

    checks, _scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    drifted = "identity.semantic differs from preflight.semantic"
    assert failures(checks) == {"instance_attested": drifted, "instance_second": drifted}
    assert [check.name for check in checks] == GENERIC_NAMES


class _SecondInstanceDriftingAdapter(_FakeAdapter):
    """Materializes a first instance that matches its own preflight and a second
    whose `settings_hash` is another one, both attesting the published digests."""

    async def materialize(self, candidate, strategy, instance_id):
        instance = await super().materialize(candidate, strategy, instance_id)
        if len(self.instances) < 2:
            return instance
        reported = instance.target_identity
        instance.target_identity = TargetIdentity(
            SemanticIdentity(
                "contract", candidate.dependency_digest, "drifted", "exact", "2", "runtime"
            ),
            reported.instance,
            reported.attested,
        )
        return instance


def test_generic_suite_fails_4g_when_the_second_instance_left_the_preflighted_semantic(store):
    """ADR-0007: *every* materialization proves the identity preflight froze, so
    4G binds the second instance to `preflight.semantic` exactly as 3G binds the
    first — an adapter that drifts only on the second one must not pass."""
    adapter = _SecondInstanceDriftingAdapter()

    checks, _scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    assert failures(checks) == {
        "instance_second": "identity.semantic differs from preflight.semantic"
    }
    assert [check.name for check in checks] == GENERIC_NAMES


def test_generic_suite_fails_when_the_identity_drops_attested(store):
    adapter = _FakeAdapter(attested=("loaded_candidate_digest",))

    checks, _scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    # The in-process adapter still hands back an instance, so the session checks
    # run and pass; only the two attestation verdicts fail.
    assert failures(checks) == {
        "instance_attested": "attestation_missing",
        "instance_second": "attestation_missing",
    }
    assert [check.name for check in checks] == GENERIC_NAMES


class _RefusingReleaseAdapter(_FakeAdapter):
    """Refuses the first release it is asked for and accepts every later one."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.refused = False

    async def release(self, instance_id: str) -> None:
        if not self.refused:
            self.refused = True
            raise RolloutInfrastructureError("the target refused a release", "internal")
        await super().release(instance_id)


def test_generic_cleanup_releases_every_instance_after_one_release_fails(store):
    """11G promises the suite releases everything it materialized on every exit
    path, so one refused release may not skip the others: B is still released,
    the retry of A still runs, and the check reports what failed."""
    adapter = _RefusingReleaseAdapter()

    checks, scenario = asyncio.run(adapter_conformance(adapter, store, run_id="conf"))

    assert failures(checks) == {"released": "internal"}
    assert scenario.instance_b is not None
    assert adapter.released == [scenario.instance_b, scenario.instance_a]
    assert adapter.instances == {}


# -- the wire suite and the runner --------------------------------------------


@pytest.mark.parametrize("seed_mode", ["exact", "unpaired", "none"])
def test_builder_runs_conformance_with_real_store_and_http_adapter(
    target, allowed, seed_root, tmp_path, seed_mode
):
    from evolution.tree import conformance_manifest
    from evolution.tree.target.http_adapter import HttpSessionAdapter

    _running, port = target(seed_mode=seed_mode, max_input_bytes=4096)
    manifest = conformance_manifest(
        {
            "seed_root": "seed",
            "target": {
                "base_url": f"http://127.0.0.1:{port}",
                "seed_mode": seed_mode,
                "min_input_bytes": 4096,
            },
            "artifacts": {
                "excludes": [],
                "dependency_paths": ["uv.lock"],
                "writable_paths": ["skills/**"],
            },
        },
        base=tmp_path,
    )
    assert manifest.workflow is None
    assert manifest.seed_root == seed_root
    assert not manifest.path.exists()

    # The runner below uses these same two supported inputs; exercise them
    # directly as well, so neither can silently acquire a workflow requirement.
    with ArtifactStore.create(
        tmp_path / "builder-store", manifest.seed_root, manifest.policy, parallel_attempts=1
    ) as store:
        adapter = HttpSessionAdapter(manifest.target, manifest.policy, run_id="builder")
        preflight = asyncio.run(adapter.preflight(store.champion().expected_attestation()))
        assert preflight.semantic.seed_mode == seed_mode
    report = asyncio.run(run_conformance(manifest))
    assert report.ok is True
    assert failures(report.checks) == {}
    assert report.leaked == ()
    assert [check.name for check in report.checks] == GENERIC_NAMES + WIRE_NAMES
    assert report.health["policy_digest"] == manifest.policy_digest
    assert health_of(port)["instances"] == []
    assert list(allowed.iterdir()) == []
    assert not manifest.path.exists()


@pytest.mark.parametrize("seed_mode", ["exact", "unpaired", "none"])
def test_run_conformance_passes_against_the_reference_target(
    target, manifest_file, allowed, seed_mode
):
    """All three seed modes conform: the suites send a seed only in `exact` and
    the seed rule each mode declares is the one they check (spec §3)."""
    _running, port = target(seed_mode=seed_mode)
    manifest = load_manifest_v2(manifest_file(port, seed_mode=seed_mode))

    report = asyncio.run(run_conformance(manifest))

    assert isinstance(report, ConformanceReport)
    assert failures(report.checks) == {}
    assert report.ok is True
    assert report.leaked == ()
    assert report.version == "conformance/v2"
    assert [check.name for check in report.checks] == GENERIC_NAMES + WIRE_NAMES
    assert [check.suite for check in report.checks] == ["G"] * 10 + ["W"] * 7
    assert report.duration_ms >= 0
    assert report.health is not None and report.health["policy_digest"] == POLICY.digest
    assert report.capabilities is not None and report.capabilities["max_instances"] == 4
    # Nothing is left on the target and nothing is left on disk.
    assert health_of(port)["instances"] == []
    assert [entry.name for entry in allowed.iterdir()] == []


def test_run_conformance_reports_the_probe_instances_a_target_refuses_to_release(
    target, manifest_file, allowed
):
    """Cleanup is a proof, not a gesture (spec §6: "the target keeps no instance
    from the probe"). A target that serves the generic suite's three releases and
    then answers 500 to every later instance `DELETE` keeps both raw-wire probes
    alive; the sweep names them and the report cannot be `ok`."""
    running, port = target()
    running.fault(refuse_release_after=3)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    left = tuple(sorted(health_of(port)["instances"]))
    assert len(left) == 2 and all(name.startswith("conf-") for name in left)
    assert report.leaked == left
    assert report.ok is False
    # A leak is not a check: every check is still named, and the two that issued
    # a refused release report it rather than swallowing it.
    assert [check.name for check in report.checks] == GENERIC_NAMES + WIRE_NAMES
    assert failures(report.checks) == {"released_wire": "internal", "limits": "internal"}
    # The run root still goes, whatever the target kept.
    assert [entry.name for entry in allowed.iterdir()] == []


def test_a_leak_no_check_can_see_still_fails_the_report(target, manifest_file, allowed):
    """The one release no check owns: the wire probe shared by 5/7W/8W/9W is
    released quietly after they pass. A target that keeps exactly that instance
    — refusing its quiet release and the sweep's retry alike — and serves every
    other release leaves every check green; only the sweep's re-read of `health`
    notices, and that alone must make the report not `ok`."""
    running, port = target()
    running.fault(keep_nth_instance=3)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert failures(report.checks) == {}
    left = tuple(health_of(port)["instances"])
    assert len(left) == 1 and left[0].startswith("conf-")
    assert report.leaked == left
    assert report.ok is False
    # The run root still goes, whatever the target kept.
    assert [entry.name for entry in allowed.iterdir()] == []


class _StubTransport:
    """Answers every call from a script of `(status, body, media)` triples."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def request(self, method, path, body=None):
        self.calls.append((method, path))
        status, payload, media = self.answers.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, raw, media


JSON = "application/json"


def test_release_accepts_only_a_proven_release(target, manifest_file):
    """A `DELETE` is the suite's own cleanup, so nothing else reports it: either
    the target proves the instance is gone or the caller learns why not."""
    gone = (200, {"instance_id": "conf-1", "released": False}, JSON)
    asyncio.run(conformance._release(_StubTransport(gone), "conf-1"))
    absent = (404, {"error": {"code": "unknown_instance", "message": "", "retryable": False}}, JSON)
    asyncio.run(conformance._release(_StubTransport(absent), "conf-1"))

    for answer, code in (
        ((200, {"instance_id": "conf-2", "released": True}, JSON), "invalid_response"),
        ((200, {"released": True}, JSON), "invalid_response"),
        ((200, b"not json", JSON), "invalid_response"),
        ((500, b"not json", JSON), "invalid_response"),
        (
            (500, {"error": {"code": "internal", "message": "", "retryable": True}}, JSON),
            "internal",
        ),
        (
            (404, {"error": {"code": "not_ready", "message": "", "retryable": True}}, JSON),
            "not_ready",
        ),
    ):
        with pytest.raises(RolloutInfrastructureError) as caught:
            asyncio.run(conformance._release(_StubTransport(answer), "conf-1"))
        assert caught.value.code == code


def test_the_sweep_returns_a_sentinel_when_health_cannot_be_re_read():
    """The sweep answers a question — "is anything of this run still live?" — so
    a target it cannot re-read is reported as unverified, never as clean. The
    sentinel is not an identifier: `<unverified>` is outside the id grammar."""
    mine = new_instance_id("conf")
    listed = {"instances": [mine, "other-1"]}
    transport = _StubTransport(
        (200, listed, JSON),
        (200, {"instance_id": mine, "released": True}, JSON),
        (500, {"error": {"code": "internal", "message": "", "retryable": True}}, JSON),
    )

    leaked = asyncio.run(conformance._sweep(transport, "conf"))

    assert leaked == ("<unverified>",)
    # Only this run's ids were addressed; another run's instance is untouched.
    assert transport.calls[1] == ("DELETE", f"{PREFIX}/instances/{mine}")
    assert len(transport.calls) == 3


def test_the_sweep_names_only_the_run_scoped_ids_still_listed():
    """Two of this run's ids survive the sweep; another run's id and a name that
    merely wears the run's prefix are not this run's to name (§9)."""
    ours = sorted(new_instance_id("conf") for _ in range(2))
    kept = {"instances": [ours[1], ours[0], "other-1", "conf-not an id"]}
    transport = _StubTransport(
        (200, kept, JSON),
        (500, {"error": {"code": "internal", "message": "", "retryable": True}}, JSON),
        (500, {"error": {"code": "internal", "message": "", "retryable": True}}, JSON),
        (200, kept, JSON),
    )

    leaked = asyncio.run(conformance._sweep(transport, "conf"))

    assert leaked == ("<unverified>", *ours)


def test_run_conformance_refuses_a_non_absolute_allowed_root(
    target, manifest_file, monkeypatch, allowed, tmp_path
):
    """The run root is created inside `allowed_artifact_root`, so a relative one
    would put conformance's filesystem activity under whatever directory the CLI
    was started in. `parse_health` already refuses it on the wire; the runner
    refuses it again before it creates anything at all."""
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    parse = conformance.parse_health

    def relative(obj):
        health = parse(obj)
        object.__setattr__(health, "allowed_artifact_root", "relative-runs")
        return health

    monkeypatch.setattr(conformance, "parse_health", relative)
    monkeypatch.chdir(tmp_path)

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is False
    assert failures(report.checks)["health"] == "allowed_artifact_root not absolute"
    assert [check.name for check in report.checks] == GENERIC_NAMES + WIRE_NAMES
    assert report.health is None and report.capabilities is None
    # Nothing was created: not on the target, not under the allowed root, and
    # not under the working directory the relative root would have named.
    assert health_of(port)["instances"] == []
    assert [entry.name for entry in allowed.iterdir()] == []
    assert not (tmp_path / "relative-runs").exists()


def test_the_wire_health_check_refuses_a_foreign_media_type(target, manifest_file):
    """1W reads the response header the adapter never exposes (§3, §6)."""
    running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    running.fault(response_content_type="text/plain")

    checks = asyncio.run(
        conformance.wire_conformance(
            manifest.target, Scenario(run_id="conf"), recorder_exchanges=()
        )
    )

    assert [check.name for check in checks] == ["health_wire"]
    assert checks[0].ok is False
    assert checks[0].got == "the health response media type is not application/json"


def test_run_conformance_fails_a_target_that_answers_under_a_foreign_media_type(
    target, manifest_file
):
    running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    running.fault(response_content_type="text/plain")

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is False
    assert failures(report.checks)["health"] == "invalid_response"


def test_the_wire_suite_fails_when_an_error_response_uses_a_foreign_media_type(
    target, manifest_file
):
    """§3 requires `application/json` on *every* response, error probes included.
    A target that answers its refusals as `text/plain` still satisfies the
    adapter's happy path, so only the wire checks that probe a refusal can catch
    it — and each of them must."""
    running, port = target()
    running.fault(error_content_type="text/plain")
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is False
    assert failures(report.checks) == {
        "instance_conflict": "invalid_response",
        "turn_idempotent_wire": "invalid_response",
        "session_unknown": "invalid_response",
        "released_wire": "invalid_response",
    }


def test_report_payload_is_plain_json_without_case_data(target, manifest_file):
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))

    payload = asyncio.run(run_conformance(manifest)).to_payload()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["version"] == "conformance/v2"
    assert set(payload) == {
        "version",
        "ok",
        "checks",
        "leaked",
        "health",
        "capabilities",
        "duration_ms",
    }
    assert payload["leaked"] == []
    assert set(payload["checks"][0]) == {"name", "ok", "expected", "got", "suite"}
    # The target answers with the first line of `skills/SKILL.md`; no turn body,
    # no public input and no candidate path may travel in the report (spec §9).
    assert "champion skill" not in json.dumps(payload)
    assert str(POLICY.digest) in json.dumps(payload["health"])


# The generic suite hands the session checks an instance only when materialize
# attested, so a target that lies about its digests leaves 6G-10G unproven. They
# are reported as `not run` rather than dropped (I7).
SESSION_NOT_RUN = {
    "session_opened": "not run",
    "turn_shape": "not run",
    "turn_idempotent": "not run",
    "session_closed": "not run",
    "identity_stable": "not run",
}


def test_a_lying_candidate_digest_fails_only_the_attestation_checks(target, manifest_file, allowed):
    running, port = target()
    running.fault(lie_candidate_digest=True)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is False
    assert failures(report.checks) == {
        "instance_attested": "attestation_mismatch",
        "instance_second": "attestation_mismatch",
        **SESSION_NOT_RUN,
    }
    assert health_of(port)["instances"] == []
    assert [entry.name for entry in allowed.iterdir()] == []


def test_a_dropped_attested_set_fails_the_attestation_checks(target, manifest_file):
    running, port = target()
    running.fault(drop_attested=True)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is False
    assert failures(report.checks) == {
        "instance_attested": "attestation_missing",
        "instance_second": "attestation_missing",
        **SESSION_NOT_RUN,
    }


def test_an_internal_error_fails_health_and_leaves_the_rest_not_run(target, manifest_file, allowed):
    """The run stops before either suite, and the report still names every check:
    a report is never shorter for having proved less."""
    running, port = target()
    running.fault(internal_error=True)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is False
    assert [check.name for check in report.checks] == GENERIC_NAMES + WIRE_NAMES
    assert report.checks[0].name == "health" and report.checks[0].got == "internal"
    assert {check.got for check in report.checks[1:]} == {"not run"}
    assert not any(check.ok for check in report.checks)
    assert [check.expected for check in report.checks] == [
        expected for _name, expected, _suite in conformance.CHECK_NAMES
    ]
    assert report.health is None and report.capabilities is None
    assert [entry.name for entry in allowed.iterdir()] == []


def test_a_single_instance_target_conforms_with_the_second_instance_skipped(
    target, manifest_file, allowed
):
    _running, port = target(max_instances=1)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    # One instance at a time is conforming; the second-instance check is skipped.
    second = next(check for check in report.checks if check.name == "instance_second")
    assert second.ok and second.got == "skipped: max_instances=1"
    assert report.ok is True
    assert [entry.name for entry in allowed.iterdir()] == []


def test_the_limits_probe_sends_exactly_the_declared_input_limit(target, manifest_file):
    """The probe body is built to the byte, so a target that accepts one byte
    fewer than it declares fails check 12 rather than passing by luck."""
    running, port = target(max_input_bytes=4096)
    manifest = load_manifest_v2(manifest_file(port, min_input_bytes=4096))
    seen: list[int] = []
    original = running.dispatch

    def watch(method, path, raw_body=b""):
        if path.endswith("/turns"):
            seen.append(len(raw_body))
        return original(method, path, raw_body)

    running.dispatch = watch  # type: ignore[method-assign]

    report = asyncio.run(run_conformance(manifest))

    assert report.ok is True
    assert max(seen) == 4096


def test_a_min_input_below_the_minimal_turn_body_checks_the_minimal_body(target, manifest_file):
    """#327: `min_input_bytes: 1` is a valid manifest value, so the limits probe
    sends the smallest body the wire has instead of failing on its own request."""
    running, port = target(max_input_bytes=4096)
    manifest = load_manifest_v2(manifest_file(port, min_input_bytes=1))
    seen: list[bytes] = []
    original = running.dispatch

    def watch(method, path, raw_body=b""):
        if path.endswith("/turns"):
            seen.append(raw_body)
        return original(method, path, raw_body)

    running.dispatch = watch  # type: ignore[method-assign]

    report = asyncio.run(run_conformance(manifest))

    limits = next(check for check in report.checks if check.name == "limits")
    assert limits.ok and report.ok is True
    assert any(json.loads(body)["message"]["content"] == "" for body in seen)


def test_check_7w_asserts_the_truncated_flag_became_an_event(target, manifest_file):
    """A target whose answer does not fit its own limit truncates, and §4 makes
    the adapter turn that into exactly one `output_truncated` event; 7W is where
    the wire flag and the adapter's `TurnResult` are compared."""
    _running, port = target(max_answer_chars=8)
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert failures(report.checks) == {}
    truncated = next(check for check in report.checks if check.name == "turn_truncated_flag")
    assert truncated.ok and truncated.got == "ok"


def test_check_7w_fails_when_the_truncated_flag_maps_to_no_event(
    target, manifest_file, monkeypatch
):
    """The mapping is the check, not the boolean: an adapter that drops the
    event on a truncated answer fails 7W and only it."""
    _running, port = target(max_answer_chars=8)
    manifest = load_manifest_v2(manifest_file(port))
    original = conformance.HttpSessionAdapter

    class Silent(original):
        async def materialize(self, candidate, strategy, instance_id):
            instance = await super().materialize(candidate, strategy, instance_id)
            send = instance.send

            async def dropped(session, message, request_id):
                turn = await send(session, message, request_id)
                events = tuple(e for e in turn.events if e.get("type") != "output_truncated")
                return TurnResult(events, turn.usage, turn.latency_ms, turn.outcome)

            instance.send = dropped
            return instance

    monkeypatch.setattr(conformance, "HttpSessionAdapter", Silent)

    report = asyncio.run(run_conformance(manifest))

    assert failures(report.checks) == {
        "turn_truncated_flag": "a truncated response did not become one output_truncated event"
    }


def test_the_wire_suite_fails_when_a_conflicting_root_is_accepted(target, manifest_file):
    """A target that answers 200 where §3 requires 409 fails check 5 and only it."""
    running, port = target()
    original = running.dispatch
    roots: dict[str, str] = {}

    def watch(method, path, raw_body=b""):
        if method == "POST" and path == f"{PREFIX}/instances" and raw_body:
            body = json.loads(raw_body)
            instance_id, root = body["instance_id"], body["candidate_root"]
            if root.endswith("not-a-root") and instance_id in roots:
                # Silently load the root the instance already holds: the target
                # now accepts a conflicting request instead of refusing it.
                body["candidate_root"] = roots[instance_id]
                raw_body = json.dumps(body).encode("utf-8")
            else:
                roots[instance_id] = root
        return original(method, path, raw_body)

    running.dispatch = watch  # type: ignore[method-assign]

    report = asyncio.run(run_conformance(load_manifest_v2(manifest_file(port))))

    assert failures(report.checks) == {"instance_conflict": "status 200, expected 409"}


def test_the_wire_suite_fails_when_a_replay_is_not_byte_identical(target, manifest_file):
    """A replayed turn that differs by one byte fails check 8W and only it."""
    running, port = target()
    original = running.dispatch
    state: dict[str, Any] = {"first": None, "seen": set()}

    def watch(method, path, raw_body=b""):
        if method == "POST" and path == f"{PREFIX}/instances" and raw_body and not state["first"]:
            state["first"] = json.loads(raw_body)["instance_id"]
        status, payload = original(method, path, raw_body)
        # Only the wire suite's own instance: the generic suite's replay goes
        # through the adapter, which would reject a body it cannot parse.
        wire_probe = state["first"] is not None and state["first"] not in path
        if method == "POST" and path.endswith("/turns") and status == 200 and wire_probe:
            request_id = json.loads(raw_body)["request_id"]
            if request_id in state["seen"]:
                payload = payload + b" "
            state["seen"].add(request_id)
        return status, payload

    running.dispatch = watch  # type: ignore[method-assign]

    report = asyncio.run(run_conformance(load_manifest_v2(manifest_file(port))))

    assert failures(report.checks) == {
        "turn_idempotent_wire": "the replayed turn response is not byte-identical"
    }


def test_cleanup_survives_a_cancellation_during_the_sweep(
    target, manifest_file, allowed, monkeypatch
):
    """The sweep is the cleanup's only `await`; a cancellation there must still
    release the store lease and remove the temporary run root."""
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    closed: list[str] = []
    create = ArtifactStore.create

    def capture(*args, **kwargs):
        opened = create(*args, **kwargs)
        close = opened.close

        def tracked() -> None:
            closed.append(opened.run_id)
            close()

        opened.close = tracked
        return opened

    async def cancelled(transport, run_id):
        raise asyncio.CancelledError

    monkeypatch.setattr(conformance.ArtifactStore, "create", capture)
    monkeypatch.setattr(conformance, "_sweep", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_conformance(manifest))

    assert len(closed) == 1
    assert [entry.name for entry in allowed.iterdir()] == []


def test_an_explicit_workdir_is_used_and_removed(target, manifest_file, allowed):
    """A `workdir` that does not exist yet is this run's own: it is created,
    used and removed with the rest of the run root."""
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    workdir = allowed / "explicit"

    report = asyncio.run(run_conformance(manifest, workdir=workdir))

    assert report.ok is True
    assert not workdir.exists()
    assert [entry.name for entry in allowed.iterdir()] == []


def test_a_created_run_root_is_removed_when_the_store_raises_an_unexpected_type(
    target, manifest_file, allowed, monkeypatch
):
    """The root belongs to the cleanup from the moment it exists, not from the
    moment the store opened: a store failure the runner does not classify must
    still leave no directory behind."""
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    workdir = allowed / "fresh"

    def explode(*_args, **_kwargs):
        raise RuntimeError("unclassified store failure")

    monkeypatch.setattr(conformance.ArtifactStore, "create", staticmethod(explode))

    with pytest.raises(RuntimeError):
        asyncio.run(run_conformance(manifest, workdir=workdir))

    assert not workdir.exists()
    assert [entry.name for entry in allowed.iterdir()] == []


def test_an_existing_explicit_workdir_is_refused_and_nothing_is_deleted(
    target, manifest_file, allowed
):
    """Cleanup removes the run root, so an explicit `workdir` must be a fresh
    directory this run owns. An existing one is a recorded `store` failure: the
    suites never run and not a byte under it is touched."""
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    workdir = allowed / "existing"
    workdir.mkdir()
    (workdir / "keep.txt").write_text("keep", encoding="utf-8")

    report = asyncio.run(run_conformance(manifest, workdir=workdir))

    assert report.ok is False
    assert failures(report.checks)["store"] == "workdir already exists"
    assert [check.name for check in report.checks] == GENERIC_NAMES + WIRE_NAMES
    assert {check.got for check in report.checks if check.name != "store"} == {"not run"}
    # Nothing deleted, nothing added, and nothing left on the target.
    assert [entry.name for entry in workdir.iterdir()] == ["keep.txt"]
    assert (workdir / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert health_of(port)["instances"] == []


def test_an_explicit_workdir_outside_the_allowed_root_is_refused_before_anything_exists(
    target, manifest_file, allowed, tmp_path
):
    """The run root is the store the Candidate roots are published from, and
    §3 lets the target accept only roots under its `allowed_artifact_root`: a
    workdir outside it can never conform. It is a recorded `store` failure the
    moment `health` names the root — nothing is created, nothing is deleted and
    the target is never asked for an instance."""
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    workdir = Path(os.path.realpath(tmp_path)) / "elsewhere" / "run"

    report = asyncio.run(run_conformance(manifest, workdir=workdir))

    assert report.ok is False
    assert failures(report.checks)["store"] == "workdir outside allowed_artifact_root"
    assert {check.got for check in report.checks if check.name != "store"} == {"not run"}
    assert not workdir.exists() and not workdir.parent.exists()
    assert [entry.name for entry in allowed.iterdir()] == []
    assert health_of(port)["instances"] == []


# -- the CLI ------------------------------------------------------------------


def invoke(manifest_path):
    return CliRunner().invoke(app, ["tree", "conformance", "--manifest", str(manifest_path)])


def test_cli_prints_the_report_and_exits_zero(target, manifest_file):
    _running, port = target()

    result = invoke(manifest_file(port))

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "conformance/v2"
    assert payload["ok"] is True
    assert [check["name"] for check in payload["checks"]] == GENERIC_NAMES + WIRE_NAMES


def test_run_conformance_never_repeats_a_forged_health_name(target, manifest_file):
    """`leaked` is printed verbatim, so only names that are this run's ids by the
    §3 grammar may appear in it. A target that pads `health` with the run's
    prefix and its own bytes earns the sentinel, never an echo (§9)."""
    running, port = target()
    running.fault(forge_health_instance="\x1b[31mX\r\n")
    manifest = load_manifest_v2(manifest_file(port))

    report = asyncio.run(run_conformance(manifest))

    assert report.leaked == ("<unverified>",)
    assert report.ok is False
    assert "\x1b" not in json.dumps(report.to_payload())


def test_cli_exits_one_when_the_target_lies(target, manifest_file):
    running, port = target()
    running.fault(lie_candidate_digest=True)

    result = invoke(manifest_file(port))

    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


def test_cli_names_the_leaked_instances_on_stderr(target, manifest_file):
    """stdout stays one JSON document; the leak is the operator's problem, so it
    is said in words too. The ids are this run's own, so printing them is safe."""
    running, port = target()
    running.fault(refuse_release_after=3)

    result = invoke(manifest_file(port))

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and len(payload["leaked"]) == 2
    assert "conformance: leaked instances:" in result.output
    for instance_id in payload["leaked"]:
        assert instance_id in result.output


def test_cli_exits_two_on_a_bad_manifest(tmp_path):
    path = tmp_path / "broken.json"
    # PDR-0038 / ADR-0046: any schema other than /15 or /16 is refused at admission.
    path.write_text(json.dumps({"schema": "evolution-v2-manifest/999"}), encoding="utf-8")

    result = invoke(path)

    assert result.exit_code == 2
    assert (
        "conformance: manifest manifest_schema: schema: expected evolution-v2-manifest/15 or /16"
        in result.output
    )


def test_the_tree_cli_still_registers_only_the_public_v2_commands():
    from evolution.tree.cli import tree_app

    assert {command.name for command in tree_app.registered_commands} == {
        "authorize",
        "conformance",
        "evolve",
        "evaluate",
        "trace",
        "watch",
    }


def test_check_is_a_frozen_record():
    check = Check("health", True, "expected", "ok", "G")

    assert check.to_payload() == {
        "name": "health",
        "ok": True,
        "expected": "expected",
        "got": "ok",
        "suite": "G",
    }
    with pytest.raises(Exception):
        check.ok = False  # type: ignore[misc]


def test_prefix_is_the_wire_prefix():
    assert PREFIX == "/evolution/v2"


# -- the target call ledger (ADR-0014) -----------------------------------------


def mutating_call_counter(monkeypatch):
    """Count every POST/DELETE any HttpTransport worker executes, ledger or not."""
    from evolution.tree.target import http_adapter

    seen = {"n": 0}
    original = http_adapter.HttpTransport._request

    def counting(self, method, path, body, call, ledger=None, *, payload):
        if method in ("POST", "DELETE"):
            seen["n"] += 1
        return original(self, method, path, body, call, ledger=ledger, payload=payload)

    monkeypatch.setattr(http_adapter.HttpTransport, "_request", counting)
    return seen


def test_run_conformance_accounts_every_mutating_call_in_the_ledger(
    target, manifest_file, monkeypatch
):
    from evolution.tree.target.target_calls import TargetCallLedger

    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    seen = mutating_call_counter(monkeypatch)
    ledger = TargetCallLedger()

    report = asyncio.run(run_conformance(manifest, ledger=ledger))
    ledger.close()
    snap = ledger.report()

    assert report.ok is True
    assert snap.namespace is not None and snap.namespace.startswith("conf-")
    assert snap.bound_transports == 3
    assert snap.admitted == seen["n"] > 0
    assert snap.pending == 0 and snap.in_flight == 0
    assert snap.quiescent is True
    # The wire suite deliberately sends malformed and conflicting requests; every
    # one of them still received a full reply.
    assert snap.uncertain_any is False
    assert snap.completed == snap.admitted


def test_run_conformance_refuses_a_foreign_ledger_before_any_socket(manifest_file):
    from evolution.tree.target.target_calls import TargetCallLedger

    # A listening socket nobody accepts from: a health GET would leave a
    # connection in its backlog.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    try:
        manifest = load_manifest_v2(manifest_file(listener.getsockname()[1]))
        ledger = TargetCallLedger()
        ledger.bind_namespace("evo-another-operation")
        with pytest.raises(ValueError, match="another namespace"):
            asyncio.run(run_conformance(manifest, ledger=ledger))
        assert ledger.report().bound_transports == 0
        with pytest.raises(BlockingIOError):
            listener.accept()
    finally:
        listener.close()


def test_run_conformance_without_a_ledger_is_unchanged(target, manifest_file):
    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    assert asyncio.run(run_conformance(manifest)).ok is True


def test_a_cancelled_conformance_run_cleans_up_before_close(target, manifest_file, monkeypatch):
    from evolution.tree.target.target_calls import TargetCallLedger

    _running, port = target()
    manifest = load_manifest_v2(manifest_file(port))
    ledger = TargetCallLedger()

    async def main():
        task = asyncio.create_task(run_conformance(manifest, ledger=ledger))
        deadline = time.monotonic() + 30.0
        while ledger.report().completed < 3:
            if task.done():
                # Finished (or failed) before admitting enough calls to exercise
                # cancellation mid-flight: a regression, not a timing fluke. Do
                # not await a cancel the task never received.
                if task.cancelled():
                    pytest.fail("run_conformance was already cancelled before 3 calls completed")
                pytest.fail(
                    "run_conformance finished before 3 target calls completed "
                    f"(exception={task.exception()!r})"
                )
            if time.monotonic() >= deadline:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                pytest.fail("timed out waiting for 3 target calls to complete")
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ledger.close()
        return await ledger.settle(timeout=30)

    snap = asyncio.run(main())
    assert snap.refused == 0  # the sweep ran before close
    assert snap.pending == 0 and snap.in_flight == 0
    assert snap.quiescent is True
    assert health_of(port)["instances"] == []
