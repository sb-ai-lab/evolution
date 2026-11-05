"""Probe degradation is optional, durable and replayable in /16."""

import asyncio
import copy
import dataclasses
import json
import sys

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.campaign import inspect_campaign
from evolution.tree.target.probe_client import call
from evolution.tree.target.probe_session import UNAVAILABLE_RESULT
from tests.probe_fakes import ProbeHTTPServer, RefusingOrigin
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow


COPY = " The probe did not answer its health check. Probe calls stay available and each one tries the probe again; a call that fails answers unavailable, so retry later or continue without the probe."
REQUEST = {"method": "GET", "path": "/ok", "body": None}


def configured(origin, mode="continue"):
    def configure(document):
        document["reflector"]["probe"] = {
            "base_url": origin,
            "budget": 1,
            "request_timeout_seconds": 0.1,
        }
        if mode is not None:
            document["reflector"]["probe"]["on_unavailable"] = mode

    return configure


def agent_configured(origin):
    def configure(document):
        document["reflector"] = {
            **{
                k: document["reflector"][k]
                for k in ("selection", "families", "max_consecutive_failures")
            },
            "kind": "agent_cli",
            "model": "codex",
            "agent_bin": sys.executable,
            "environment_docs": None,
            "probe": {"base_url": origin, "on_unavailable": "continue"},
        }

    return configure


@pytest.mark.parametrize("failed_role", [False, True])
@pytest.mark.parametrize("log_kind", ["normal", "malformed", "oversized"])
async def test_disabled_helper_claims_are_settled_before_either_reflection_terminal(
    tmp_path, monkeypatch, failed_role, log_kind
):
    from evolution.tree.roles.reflection_role_helpers import (
        MAX_PROBE_CALL_LOG_BYTES,
        PROBE_CALL_LOG_NAME,
    )
    from evolution.tree.roles.reflector.agent_reflector import (
        CONTROL_PREFIX,
        REFLECTION_FILE,
        AgentCliReflector,
    )

    async def failed(self):
        raise OSError("cannot listen")

    async def agent_call(self, request, root, log, started, *, call, prompt):
        path = root / CONTROL_PREFIX / "probe" / PROBE_CALL_LOG_NAME
        line = json.dumps(REQUEST) + "\n"
        content = line * 2 if log_kind == "normal" else "{bad}\n{}\n" + line
        if log_kind == "oversized":
            content += " " * MAX_PROBE_CALL_LOG_BYTES
        path.write_text(content)
        if failed_role:
            raise RuntimeError("offline role failure")
        (root / CONTROL_PREFIX / REFLECTION_FILE).write_text(
            json.dumps({"status": "declined", "groups": [], "note": "done"})
        )
        return 1

    monkeypatch.setattr(campaign_module.ProbeMediator, "start", failed)
    monkeypatch.setattr(AgentCliReflector, "_call", agent_call)
    with ProbeHTTPServer() as server:
        run, result = await public_run(
            tmp_path, monkeypatch, configure=agent_configured(server.base_url)
        )
    count = {"normal": 2, "malformed": 1, "oversized": 0}[log_kind]
    assert [p["error"] for p in payloads(run, "probe_completed")] == ["unreachable"] * count
    terminal = "reflection_failed" if failed_role else "reflection_finished"
    assert len(payloads(run, terminal)) == 1
    assert payloads(run, terminal)[0]["probe_availability"] == {
        "health": "ready",
        "mediator": "failed",
        "unreachable": count,
        "transport_failures": 0,
    }
    projection = inspect_campaign(run, verify=True).to_payload()
    assert projection["independent"]["probe_availability"] == [
        {"round": 1, **payloads(run, terminal)[0]["probe_availability"]}
    ]
    assert projection["verification"]["mismatches"] == []


@pytest.mark.parametrize("mediator_failed", [False, True])
async def test_interrupted_continue_reflection_records_checked_availability(
    tmp_path, monkeypatch, mediator_failed
):
    async def failed(self):
        raise OSError("cannot listen")

    if mediator_failed:
        monkeypatch.setattr(campaign_module.ProbeMediator, "start", failed)
    with ProbeHTTPServer() as server:
        configure = configured(server.base_url)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path, monkeypatch, configure=configure, cut="after_reflection_started"
            )
        run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    interrupted = payloads(run, "reflection_failed")[0]
    assert interrupted["error_class"] == "interrupted"
    assert interrupted["probe_availability"] == {
        "health": "ready",
        "mediator": "failed" if mediator_failed else "started",
        "unreachable": 0,
        "transport_failures": 0,
    }
    assert result.terminal == "finished"
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.storage.envelope import Envelope, SchemaError
    from tests.campaign_drivers import _records

    family = campaign_family_for(15)
    records = _records(run / "campaign.jsonl")
    index = next(i for i, record in enumerate(records) if record["event"] == "reflection_failed")
    for change in (None, {"health": "unavailable"}, {"unreachable": 1}, {"transport_failures": 1}):
        bad = copy.deepcopy(records[index])
        if change is None:
            del bad["payload"]["probe_availability"]
        else:
            bad["payload"]["probe_availability"].update(change)
        with pytest.raises(SchemaError):
            reduce(
                tuple(
                    Envelope.from_payload(value, family=family) for value in [*records[:index], bad]
                ),
                selector=None,
                family_version=15,
            )


@pytest.mark.parametrize("mode", [None, "suspend"])
async def test_default_failed_reflection_keeps_availability_absent(tmp_path, monkeypatch, mode):
    original = campaign_module.run_reflector

    async def fail(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        return dataclasses.replace(
            outcome,
            terminal="failed",
            status=None,
            error_class="invalid_result",
            groups=(),
            note="invalid result",
        )

    monkeypatch.setattr(campaign_module, "run_reflector", fail)
    with ProbeHTTPServer() as server:
        run, _ = await public_run(
            tmp_path, monkeypatch, configure=configured(server.base_url, mode)
        )
    assert payloads(run, "reflection_failed")
    assert all("probe_availability" not in value for value in payloads(run, "reflection_failed"))


@pytest.mark.parametrize("interrupt", [False, True])
async def test_probeless_continue_failure_has_no_availability(tmp_path, monkeypatch, interrupt):
    def refuse(self, source, reflection_id, brief, inputs):
        return brief, None, True

    monkeypatch.setattr(campaign_module._Campaign, "_bound_reflection", refuse)
    configure = configured("http://localhost:8099")
    if interrupt:
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path, monkeypatch, configure=configure, cut="after_reflection_started"
            )
    run, _ = await public_run(tmp_path, monkeypatch, configure=configure)
    assert not payloads(run, "probe_checked")
    assert payloads(run, "reflection_failed")
    assert all("probe_availability" not in value for value in payloads(run, "reflection_failed"))


@pytest.mark.parametrize("ready", [False, True])
async def test_continue_unavailable_health_runs_reflection(tmp_path, monkeypatch, ready):
    async def health(*args, **kwargs):
        return ready

    monkeypatch.setattr(campaign_module, "check_probe_health", health)
    with ProbeHTTPServer() as server:
        run, result = await public_run(tmp_path, monkeypatch, configure=configured(server.base_url))
    assert result.terminal == "finished"
    assert not payloads(run, "campaign_suspended")
    assert payloads(run, "reflection_started")
    terminal = payloads(run, "reflection_finished")[0]
    assert terminal["probe_availability"] == {
        "health": "ready" if ready else "unavailable",
        "mediator": "started",
        "unreachable": 0,
        "transport_failures": 0,
    }
    assert result.result.to_payload()["independent"]["probe_availability"] == [
        {"round": 1, **terminal["probe_availability"]}
    ]
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


@pytest.mark.parametrize("ready", [False, True])
def test_continue_opening_sentence_only_after_unavailable_check(ready):
    from evolution.tree.roles.reflection_role_helpers import probe_opening
    from evolution.tree.roles.reflector_contracts import ProbeAccess

    access = ProbeAccess("http://127.0.0.1:8099/" + "a" * 32 + "/probe", 1, ("GET",), None)
    assert (COPY in probe_opening(access, health_unavailable=not ready)) is (not ready)


async def test_first_request_bound_covers_both_unavailable_sentences(tmp_path, monkeypatch):
    from evolution.tree.roles.reflection_role_helpers import probe_opening
    from evolution.tree.roles.reflector.reflect_budget import PROBE_URL_PLACEHOLDER

    measure = campaign_module.measure_first_request
    run_reflector = campaign_module.run_reflector
    measured_probes = []
    checked = []

    def capture(*args, **kwargs):
        measured_probes.append(kwargs["probe"])
        return measure(*args, **kwargs)

    async def health(*args, **kwargs):
        return False

    async def failed(self):
        raise OSError("cannot listen")

    async def reflect(reflector, request, **kwargs):
        bound_probe = measured_probes[-1]
        assert bound_probe.url == PROBE_URL_PLACEHOLDER
        assert request.probe.url is None
        assert COPY in probe_opening(request.probe)
        assert "The probe mediator could not start" in probe_opening(request.probe)
        options = {
            "editable_paths": request.editable_paths,
            "editor_max_changed_files": request.editor_max_changed_files,
        }
        bound = measure(reflector, request.evidence, probe=bound_probe, **options)
        actual = measure(reflector, request.evidence, probe=request.probe, **options)
        assert bound.total_chars >= actual.total_chars
        checked.append(True)
        return await run_reflector(reflector, request, **kwargs)

    monkeypatch.setattr(campaign_module, "measure_first_request", capture)
    monkeypatch.setattr(campaign_module, "check_probe_health", health)
    monkeypatch.setattr(campaign_module.ProbeMediator, "start", failed)
    monkeypatch.setattr(campaign_module, "run_reflector", reflect)
    _, result = await public_run(
        tmp_path, monkeypatch, configure=configured("http://localhost:8099")
    )
    assert checked
    assert result.terminal == "finished"


async def test_continue_external_reflector_reaches_recovered_probe_through_mediator(
    tmp_path, monkeypatch
):
    original = campaign_module.run_reflector

    async def health(*args, **kwargs):
        return False

    async def reflect(reflector, request, *args, **kwargs):
        # An external client reaches the session only through its real HTTP mediator.
        result = await asyncio.to_thread(call, request.probe.url, REQUEST, timeout_seconds=1)
        assert result["status"] == 200
        return await original(reflector, request, *args, **kwargs)

    monkeypatch.setattr(campaign_module, "check_probe_health", health)
    monkeypatch.setattr(campaign_module, "run_reflector", reflect)
    with ProbeHTTPServer() as server:
        run, result = await public_run(tmp_path, monkeypatch, configure=configured(server.base_url))
    assert result.terminal == "finished"
    assert payloads(run, "probe_completed")[0]["status"] == 200


@pytest.mark.parametrize("calls", [0, 2])
async def test_continue_mediator_failure_runs_reflection(tmp_path, monkeypatch, calls):
    original = campaign_module.run_reflector

    async def failed(self):
        raise OSError("cannot listen")

    async def reflect(reflector, request, *args, **kwargs):
        for _ in range(calls):
            assert await request.probe_session.request(REQUEST) == UNAVAILABLE_RESULT
        return await original(reflector, request, *args, **kwargs)

    monkeypatch.setattr(campaign_module.ProbeMediator, "start", failed)
    monkeypatch.setattr(campaign_module, "run_reflector", reflect)
    with ProbeHTTPServer() as server:
        run, result = await public_run(tmp_path, monkeypatch, configure=configured(server.base_url))
        assert [c.path for c in server.calls] == ["/health"]
    assert result.terminal == "finished"
    assert list((run / "reflector-logs/probes").glob("*/startup.json"))
    assert payloads(run, "reflection_finished")[0]["probe_availability"] == {
        "health": "ready",
        "mediator": "failed",
        "unreachable": calls,
        "transport_failures": 0,
    }
    assert [p["error"] for p in payloads(run, "probe_completed")] == ["unreachable"] * calls


async def test_continue_failed_mediator_helper_returns_unavailable(tmp_path, monkeypatch):
    from evolution.tree.roles.reflection_role_helpers import install_probe_helper

    original = campaign_module.run_reflector

    async def failed(self):
        raise OSError("cannot listen")

    async def reflect(reflector, request, *args, **kwargs):
        helpers = install_probe_helper(
            tmp_path / "helper", request.probe, deadline_at=request.probe_session.deadline_at
        )
        process = await asyncio.create_subprocess_exec(
            *helpers.descriptors["probe_request"],
            json.dumps(REQUEST),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        assert process.returncode == 0
        assert json.loads(stdout) == UNAVAILABLE_RESULT
        return await original(reflector, request, *args, **kwargs)

    monkeypatch.setattr(campaign_module.ProbeMediator, "start", failed)
    monkeypatch.setattr(campaign_module, "run_reflector", reflect)
    with ProbeHTTPServer() as server:
        run, result = await public_run(tmp_path, monkeypatch, configure=configured(server.base_url))
    assert result.terminal == "finished"
    assert not payloads(run, "probe_completed")  # Unreceived calls cannot create Core evidence.
    assert payloads(run, "reflection_finished")[0]["probe_availability"]["mediator"] == "failed"


async def test_continue_replay_after_crash_between_unreachable_and_finish(tmp_path, monkeypatch):
    original = campaign_module.run_reflector
    crashed = False

    async def reflect(reflector, request, *args, **kwargs):
        nonlocal crashed
        for _ in range(2):
            assert await request.probe_session.request(REQUEST) == UNAVAILABLE_RESULT
        if not crashed:
            crashed = True
            raise PowerCut()
        return await original(reflector, request, *args, **kwargs)

    monkeypatch.setattr(campaign_module, "run_reflector", reflect)
    origin = RefusingOrigin()
    try:
        configure = configured(origin.base_url)
        with pytest.raises(PowerCut):
            await public_run(tmp_path, monkeypatch, configure=configure)
        assert [p["seq"] for p in payloads(tmp_path / "run", "probe_completed")] == [1, 2]
        run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    finally:
        origin.close()
    assert result.terminal == "finished"
    assert payloads(run, "reflection_finished")[0]["probe_availability"] == {
        "health": "unavailable",
        "mediator": "started",
        "unreachable": 2,
        "transport_failures": 0,
    }
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


@pytest.mark.parametrize("mediator_failed", [False, True])
async def test_continue_agent_cli_uses_the_real_sealed_helper(
    tmp_path, monkeypatch, mediator_failed
):
    from evolution.tree.roles.reflector.agent_reflector import (
        CONTROL_PREFIX,
        REFLECTION_FILE,
        AgentCliReflector,
    )

    async def health(*args, **kwargs):
        return False

    async def failed(self):
        raise OSError("cannot listen")

    async def agent_call(self, request, root, log, started, *, call, prompt):
        helper = root / CONTROL_PREFIX / "probe/probe_request.py"
        for _ in range(3):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-I",
                str(helper),
                json.dumps(REQUEST),
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            answer = json.loads(stdout)
            assert answer == UNAVAILABLE_RESULT if mediator_failed else answer["status"] == 200
        assert COPY in prompt
        (root / CONTROL_PREFIX / REFLECTION_FILE).write_text(
            json.dumps({"status": "declined", "groups": [], "note": "done"})
        )
        return 1

    monkeypatch.setattr(campaign_module, "check_probe_health", health)
    monkeypatch.setattr(AgentCliReflector, "_call", agent_call)
    if mediator_failed:
        monkeypatch.setattr(campaign_module.ProbeMediator, "start", failed)
    with ProbeHTTPServer() as server:

        def configure(document):
            document["reflector"] = {
                **{
                    k: document["reflector"][k]
                    for k in ("selection", "families", "max_consecutive_failures")
                },
                "kind": "agent_cli",
                "model": "codex",
                "agent_bin": sys.executable,
                "environment_docs": None,
                "probe": {"base_url": server.base_url, "on_unavailable": "continue"},
            }

        run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished"
    terminal = payloads(run, "reflection_finished")[0]
    assert terminal["probe_availability"]["unreachable"] == (3 if mediator_failed else 0)
    assert [p["seq"] for p in payloads(run, "probe_completed")] == [1, 2, 3]
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
    assert terminal["probe_availability"]["mediator"] == (
        "failed" if mediator_failed else "started"
    )


async def test_reducer_refuses_unreachable_after_budget_and_wrong_accounting(tmp_path, monkeypatch):
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.storage.envelope import Envelope, SchemaError
    from tests.campaign_drivers import _records

    with ProbeHTTPServer() as server:
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                monkeypatch,
                configure=configured(server.base_url),
                cut="after_reflection_started",
            )
    family = campaign_family_for(15)
    records = [
        Envelope.from_payload(r, family=family) for r in _records(tmp_path / "run/campaign.jsonl")
    ]
    row = records[-1].to_payload()

    def probe(seq, status, error):
        return Envelope.from_payload(
            {
                **row,
                "sequence": row["sequence"] + seq,
                "event": "probe_completed",
                "operation_id": None,
                "payload": {
                    "round": 1,
                    "reflection_id": "r01-x01",
                    "seq": seq,
                    "method": "GET",
                    "path": "/ok",
                    "status": status,
                    "error": error,
                },
            },
            family=family,
        )

    # Five free unreachable calls are legal with budget 1.
    reduce(
        (*records, *(probe(i, None, "unreachable") for i in range(1, 6))),
        selector=None,
        family_version=15,
    )
    with pytest.raises(SchemaError, match="budget"):
        reduce(
            (*records, probe(1, 200, None), probe(2, None, "unreachable")),
            selector=None,
            family_version=15,
        )
    bad = copy.deepcopy(probe(1, None, "unreachable").to_payload())
    bad["payload"]["status"] = 200
    with pytest.raises(SchemaError):
        Envelope.from_payload(bad, family=family)


async def test_rebase_to_suspend_does_not_reopen_a_finished_reflections_health_check(
    tmp_path, monkeypatch
):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_visibility_change import _decision

    origin = RefusingOrigin()
    try:
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                monkeypatch,
                configure=configured(origin.base_url),
                cut="after_reflection_finished",
            )
        suspend = configured(origin.base_url, "suspend")
        with pytest.raises(EvolveRefused) as caught:
            await public_run(tmp_path, monkeypatch, configure=suspend)
        run, result = await public_run(
            tmp_path,
            monkeypatch,
            configure=suspend,
            authorizations=[_decision(caught.value, None, reason="change probe policy")],
        )
        assert result.terminal == "finished"
        assert not payloads(run, "campaign_suspended")
        assert len(payloads(run, "reflection_finished")) == 1
    finally:
        origin.close()


@pytest.mark.parametrize("mode", [None, "suspend"])
async def test_suspend_and_absent_unchanged(tmp_path, monkeypatch, mode):
    origin = RefusingOrigin()
    try:
        run, result = await public_run(
            tmp_path, monkeypatch, configure=configured(origin.base_url, mode)
        )
    finally:
        origin.close()
    assert result.terminal == "suspended"
    assert payloads(run, "campaign_suspended")[0]["reason"] == "probe_unavailable"
    assert not payloads(run, "reflection_started")
