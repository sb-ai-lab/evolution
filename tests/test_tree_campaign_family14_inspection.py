"""H12b Task 8: verified inspection at every reachable family-14 state.

Every cell here is a genuine engine invocation through the public driver
`tests.family14_public.family14_public_run` (no `_Campaign.result`
interception), reusing the suspension, crash and fault scenarios the
delivery slices built (H5/H6 final gaps, H7 refresh threshold, H8/H9
Grouper, H10 connection preflight, #194 Champion gap). Each state is read
with `inspect_campaign(run, verify=True)`: the status validates and
round-trips through its bytes, the evidence audit finds no mismatch, and
the `suspensions`, `connection` and `actions` blocks are the state's. A
suspended invocation's own `CampaignResult.status` is checked beside it.

The last sections hold `evo tree evaluate` over a family-14 source run,
the U3 carry (a `first_accepted` round with several final entries) and
H6-F3: a cut after an applied comparable-change `close` -- between rounds,
inside a final stage, or over an open connection preflight -- that the same
command finishes with exactly its owed `campaign_finished`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign_projections import CampaignProjection
from evolution.tree.final_report import validate_connection_block
from tests.campaign_drivers import LENIENT, _cut_hook
from tests.campaign_fakes import Crash
from tests.family14_public import family14_public_run
from tests.test_tree_campaign_family14_reports import _documents, _suspended_status

pytestmark = pytest.mark.slow


# -- helpers ----------------------------------------------------------------------------


def _inspected(run: Path) -> dict[str, Any]:
    """`inspect_campaign(run, verify=True)`: a validated family-14 status with a
    clean audit, whose bytes round-trip and whose `actions` serialize."""

    from evolution.tree.campaign import inspect_campaign

    projection = inspect_campaign(run, verify=True)
    raw = projection.encode()
    assert CampaignProjection.from_bytes(raw).encode() == raw
    status = projection.to_payload()
    assert status["kind"] == "campaign_status"
    assert "connection" in status, "not a family-14 document"
    validate_connection_block(status["connection"])
    assert status["verification"]["mismatches"] == []
    # Every next-action payload is plain JSON (the planner's dataclass fields).
    assert json.loads(json.dumps(status["actions"])) == status["actions"]
    # "Final everywhere" (PDR-0028, F0 M4): no family-14 action says "cohort".
    assert not [kind for kind in _kinds(status) if "Cohort" in kind]
    return status


def _kinds(status: dict[str, Any]) -> list[str]:
    return [action["kind"] for action in status["actions"]]


def _reasons(status: dict[str, Any]) -> list[str]:
    return [entry["reason"] for entry in status["suspensions"]]


def _connection(status: dict[str, Any]) -> tuple[Any, ...]:
    connection = status["connection"]
    return connection["state"], connection["outcome"], connection["attempts"]


def _same_as_inspected(result_status: dict[str, Any], inspected: dict[str, Any]) -> None:
    """The suspending invocation's status is the inspection's, but for
    `verification` and each measurement status's `consistency`: the live run
    reads its journals `durable` under its lease, an inspection `observed`."""

    def comparable(status: dict[str, Any]) -> dict[str, Any]:
        measurements = []
        for entry in status["measurements"]:
            if entry["status"] is not None:
                entry = {**entry, "status": {**entry["status"], "consistency": None}}
            measurements.append(entry)
        return {
            **{key: value for key, value in status.items() if key != "verification"},
            "measurements": measurements,
        }

    def consistencies(status: dict[str, Any]) -> set[str]:
        return {
            entry["status"]["consistency"]
            for entry in status["measurements"]
            if entry["status"] is not None
        }

    assert consistencies(result_status) <= {"durable"}
    assert consistencies(inspected) <= {"observed"}
    assert comparable(result_status) == comparable(inspected)


def _assert_finished(run: Path, result: Any, reasons: list[str]) -> dict[str, Any]:
    """A resumed run finished; its inspection keeps every earlier suspension."""

    result_document, status_document = _documents(result)
    status = _inspected(run)
    assert status["complete"] is True and status["suspended"] is False
    assert status["actions"] == []
    assert _reasons(status) == reasons
    for document in (result_document, status_document):
        assert _reasons(document) == reasons
        assert document["connection"] == status["connection"]
    return status


# -- final_incomplete (H5/H6 final gap) ---------------------------------------------------


async def test_a_final_incomplete_suspension_and_its_resume(tmp_path, monkeypatch):
    from tests.campaign_drivers import EDITED, _arm_on_finalists_bound, _CohortFaults

    adapter = _CohortFaults(faults={("champion", "c2", 1): "time_budget"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {"indeterminate": LENIENT, "sample_retries": 0}}
    run, result = await family14_public_run(tmp_path, monkeypatch, **options)
    suspended = _suspended_status(result, "final_incomplete", "rollout", "final_rows")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    assert status["suspended"] is True and status["complete"] is False
    assert _connection(status) == ("compatible", "compatible", 1)
    (final,) = status["rounds"][0]["final"]
    assert [row["measurement_id"] for row in final["listed"]] == ["m-r01-final-01-champion"]
    assert _kinds(status) == ["RunFinalStage"]  # the stage runs again to repair

    adapter.faults.clear()
    run, result = await family14_public_run(tmp_path, monkeypatch, **options)
    status = _assert_finished(run, result, ["final_incomplete"])
    assert _connection(status) == ("compatible", "compatible", 1)


# -- Grouper suspensions (H8) and an agent_cli full fallback (H9) --------------------------


@pytest.mark.parametrize(
    ("name", "reason"), [("outage", "grouper_provider_failed"), ("exception", "grouper_failing")]
)
async def test_a_grouper_suspension_and_its_resume(tmp_path, monkeypatch, name, reason):
    from tests.model_grouping_fakes import chat_grouper_section
    from tests.test_tree_campaign_family14_grouper_recovery import _grouping_script, _scripted

    client = _scripted(monkeypatch, name)
    settings = {"grouper": chat_grouper_section()}
    run, result = await family14_public_run(tmp_path, monkeypatch, family14_settings=settings)
    suspended = _suspended_status(result, reason, "grouper", "stage_record")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    assert status["roles"]["grouper"] == {"kind": "chat"}
    assert _reasons(status) == [reason]
    # The baseline was measured, so the probe ran and found the Target compatible.
    assert _connection(status) == ("compatible", "compatible", 1)
    assert _kinds(status) == ["RunGrouping"]
    assert client.script == []  # the scripted failure was consumed

    client.script.extend(_grouping_script("admitted"))
    run, result = await family14_public_run(tmp_path, monkeypatch, family14_settings=settings)
    _assert_finished(run, result, [reason])


async def test_an_agent_turns_exhausted_fallback_caveat_survives_the_suspension_and_resume(
    tmp_path, monkeypatch
):
    """H9 handoff: a full `turns_exhausted` fallback (round 1) is a
    `grouping_fallback` caveat in the suspended status of the second link
    (round 2, `grouper_failing`) and stays after the resume."""

    from tests.agent_grouping_fakes import scripted_grouping_cli
    from tests.test_tree_campaign_family14_grouper_run import NO_FINALISTS

    section = scripted_grouping_cli(tmp_path, "turns_exhausted", "turns_exhausted", "admitted")
    options = {
        "family14_settings": {"grouper": section, "refresh": 1},
        "campaign": {"rounds": 2},
    }
    caveat = {
        "kind": "grouping_fallback",
        "round": 1,
        "reason": "turns_exhausted",
        "fallback_kind": "singletons",
        "scope": "all",
    }

    def fallbacks(document):
        return [item for item in document["caveats"] if item["kind"] == "grouping_fallback"]

    run, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    suspended = _suspended_status(result, "grouper_failing", "grouper", "stage_record")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    assert status["roles"]["grouper"] == {"kind": "agent_cli"}
    assert fallbacks(suspended) == fallbacks(status) == [caveat]

    run, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    status = _assert_finished(run, result, ["grouper_failing"])
    for document in (*_documents(result), status):
        assert fallbacks(document) == [caveat]


# -- connection suspensions (H10) -----------------------------------------------------------


def _connected(monkeypatch) -> None:
    """Bind `METRIC_CONTRACT` into the `/15` document both drivers write (H10's
    `connected_run`, without its pre-assembly driver)."""

    from tests import campaign_fakes
    from tests import test_tree_campaign_family14_run as driver
    from tests.tree.manifest.test_manifest_v15 import METRIC_CONTRACT

    real_document = campaign_fakes.family14_document

    def document(base: Any, **options: Any) -> dict[str, Any]:
        return real_document(base, **options, metric_contract=METRIC_CONTRACT)

    monkeypatch.setattr(driver, "family14_document", document)


CONNECTION_CELLS = {
    # case: (reason, detail)
    "event_missing": (
        "connection_incompatible",
        {
            "code": "connection_event_missing",
            "metric": "score",
            "manifest_path": "scorer.metric_contract.target_fields[0]",
            "observed_path": "events[event=assistant_message,last]/content",
        },
    ),
    # A Target-side cause, so the repair changes no bound input.
    "send_infrastructure": (
        "connection_preflight_unavailable",
        {"cause_code": "infrastructure_failed"},
    ),
    "turn_budget": ("connection_preflight_inconclusive", {"cause_code": "turn_budget"}),
}


@pytest.mark.parametrize("case", list(CONNECTION_CELLS))
async def test_a_connection_suspension_and_the_resume_that_probes_again(
    tmp_path, monkeypatch, case
):
    from tests.campaign_drivers import EDITED
    from tests.test_tree_campaign_family14_connection_run import (
        PROBE_MARK,
        ConnectedAdapter,
        _probe_environment,
        _script,
        probe_scorer,
    )

    reason, detail = CONNECTION_CELLS[case]
    _connected(monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    options = _script(adapter, case)
    assert set(options) <= {"scorer_mode"}
    with monkeypatch.context() as patch:
        if case == "turn_budget":
            _probe_environment(patch)
        run, result = await family14_public_run(
            tmp_path,
            patch,
            adapter=adapter,
            scorer=probe_scorer(options.get("scorer_mode")),
        )
    suspended = _suspended_status(result, reason, "connection", "stage_record")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    connection = status["connection"]
    assert (connection["state"], connection["outcome"], connection["attempts"]) == (
        "suspended",
        reason,
        1,
    )
    assert connection["detail"] == detail
    # No observed value reaches the public document (C§3.2).
    assert PROBE_MARK not in json.dumps(status)
    assert status["rounds"][0]["final"] == [] and status["rounds"][0]["attempts"] == []
    assert _kinds(status) == ["MeasureBaselineStage"]

    # The operator repairs the Target and the Scorer; the same command probes again.
    repaired = ConnectedAdapter(used=EDITED["used"])
    run, result = await family14_public_run(
        tmp_path, monkeypatch, adapter=repaired, scorer=probe_scorer(None)
    )
    status = _assert_finished(run, result, [reason])
    assert _connection(status) == ("compatible", "compatible", 2)
    assert status["connection"]["operation_id"] != connection["operation_id"]
    assert len(repaired.probes) == 1


async def test_a_failed_preflight_whose_suspension_is_owed(tmp_path, monkeypatch):
    """A cut after the failed preflight terminal and before its `campaign_suspended`
    (H10's `after_connection_preflight_terminal`, R61): the status shows the
    connection `running` with the failed outcome and its full detail, the audit
    is clean, the resume writes the owed suspension first, and `evo tree
    evolve`'s sentence renders for it."""

    from evolution.tree.cli import _suspension_detail
    from tests.campaign_drivers import EDITED
    from tests.test_tree_campaign_family14_connection_run import (
        PROBE_MARK,
        ConnectedAdapter,
        _script,
        campaign,
        probe_scorer,
    )
    from tests.test_tree_cli_family14 import INCOMPATIBLE

    reason, detail = CONNECTION_CELLS["event_missing"]
    _connected(monkeypatch)
    adapter = ConnectedAdapter(used=EDITED["used"])
    assert _script(adapter, "event_missing") == {}
    options = {"adapter": adapter, "scorer": probe_scorer(None)}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_preflight_terminal")
        with pytest.raises(Crash):
            await family14_public_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    records = campaign(run)
    assert records[-1]["event"] == "connection_preflight_failed"
    owed = len(records)

    status = _inspected(run)
    assert status["complete"] is False and status["suspended"] is False
    assert status["suspensions"] == []
    connection = status["connection"]
    assert (connection["state"], connection["outcome"], connection["attempts"]) == (
        "running",
        reason,
        1,
    )
    assert connection["detail"] == detail
    assert PROBE_MARK not in json.dumps(status)
    # The owed status already carries what the suspension's sentence reads.
    evidence = {"evidence_kind": "stage_record", "seq": records[-1]["sequence"]}
    assert _suspension_detail(evidence, reason=reason, connection=connection) == INCOMPATIBLE

    # The same command writes the owed suspension first and ends suspended.
    run, result = await family14_public_run(tmp_path, monkeypatch, **options)
    records = campaign(run)
    assert records[owed]["event"] == "campaign_suspended"
    assert [r["event"] for r in records[owed:]].count("connection_preflight_started") == 0
    suspended = _suspended_status(result, reason, "connection", "stage_record")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    assert _connection(status) == ("suspended", reason, 1)
    assert status["connection"]["operation_id"] == connection["operation_id"]
    assert status["connection"]["detail"] == detail
    assert _reasons(status) == [reason]
    sentence = _suspension_detail(
        result.suspended["evidence"],
        reason=result.suspended["reason"],
        connection=result.status.to_payload()["connection"],
    )
    assert sentence == INCOMPATIBLE


# -- a threshold crossing during a refresh (H7) ---------------------------------------------


async def test_a_threshold_crossing_during_a_refresh_shows_it_open(tmp_path, monkeypatch):
    from tests.campaign_drivers import SERIAL
    from tests.test_tree_campaign_family14_refresh_recovery import _target
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    adapter = _target(tmp_path, faults={("c2", 1): "target_failed"})
    options = {
        "adapter": adapter,
        "family14_settings": {"refresh": 1},
        "campaign": {
            "rounds": 2,
            "sample_retries": 0,
            "indeterminate": {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}},
            **SERIAL,
        },
    }
    run, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    suspended = _suspended_status(result, "target_unstable", "target", "threshold")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    assert _connection(status) == ("compatible", "compatible", 1)
    refresh = {
        "round": 2,
        "measurement_id": "m-r02-refresh",
        "replaced_measurement_id": "m-r00-champion",
        "completed": False,
    }
    assert status["settings"]["baseline_refresh"] == {
        "every_rounds": 1,
        "count": 0,
        "performed": [refresh],
    }
    assert [entry["baseline_measurement_id"] for entry in status["rounds"]] == [
        "m-r00-champion",
        "m-r02-refresh",
    ]
    # The refresh round is open: its baseline is incomplete and nothing closed it.
    assert [(e["outcome"], e["baseline_complete"]) for e in status["rounds"]] == [
        ("stalled", True),
        (None, False),
    ]
    assert _kinds(status) == ["MeasureBaselineStage"]

    adapter.faults.clear()
    run, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    status = _assert_finished(run, result, ["target_unstable"])
    assert status["settings"]["baseline_refresh"]["performed"] == [{**refresh, "completed": True}]


# -- champion_incomplete (#194) ---------------------------------------------------------------


async def test_a_champion_incomplete_suspension_and_its_resume(tmp_path, monkeypatch):
    from tests.campaign_drivers import SERIAL
    from tests.test_tree_campaign_family14_champion_gap import _cut_stop, _GapTarget
    from tests.test_tree_campaign_family14_refresh_run import CHAMPION0, NO_FINALISTS

    adapter = _GapTarget(tmp_path / "run", CHAMPION0)
    await _cut_stop(tmp_path, monkeypatch, adapter)
    options = {
        "adapter": adapter,
        "family14_settings": {"refresh": 0},
        "campaign": {"rounds": 1, "indeterminate": LENIENT, **SERIAL},
    }
    run, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    suspended = _suspended_status(result, "champion_incomplete", "target", "champion_rows")
    status = _inspected(run)
    _same_as_inspected(suspended, status)
    assert status["rounds"][0]["baseline_measurement_id"] == "m-r00-champion"
    assert _connection(status) == ("compatible", "compatible", 1)
    assert _kinds(status) == ["MeasureBaselineStage"]

    adapter.faults.clear()
    run, result = await family14_public_run(tmp_path, monkeypatch, NO_FINALISTS, **options)
    _assert_finished(run, result, ["champion_incomplete"])


# -- cuts: the status of an unfinished prefix -------------------------------------------------


async def _cut_at(tmp_path, monkeypatch, arm, **options) -> dict[str, Any]:
    with monkeypatch.context() as patch:
        arm(patch)
        with pytest.raises(Crash):
            await family14_public_run(tmp_path, patch, **options)
    status = _inspected(tmp_path / "run")
    assert status["complete"] is False and status["suspended"] is False
    assert status["suspensions"] == []
    return status


async def test_a_cut_with_the_connection_preflight_open(tmp_path, monkeypatch):
    status = await _cut_at(
        tmp_path,
        monkeypatch,
        lambda patch: _cut_hook(patch, "after_connection_preflight_started"),
    )
    connection = status["connection"]
    assert (connection["state"], connection["outcome"], connection["detail"]) == (
        "running",
        None,
        None,
    )
    assert connection["attempts"] == 1 and connection["operation_id"] is not None
    assert _kinds(status) == ["MeasureBaselineStage"]
    run, result = await family14_public_run(tmp_path, monkeypatch)
    status = _assert_finished(run, result, [])
    assert _connection(status) == ("compatible", "compatible", 2)


async def test_a_cut_with_a_grouping_open(tmp_path, monkeypatch):
    status = await _cut_at(
        tmp_path, monkeypatch, lambda patch: _cut_hook(patch, "after_grouping_started")
    )
    assert _connection(status) == ("compatible", "compatible", 1)
    (entry,) = status["rounds"]
    assert entry["attempts"] == [] and entry["final"] == []
    assert _kinds(status) == ["RunGrouping"]
    run, result = await family14_public_run(tmp_path, monkeypatch)
    _assert_finished(run, result, [])


async def test_a_cut_with_a_final_stage_open(tmp_path, monkeypatch):
    from tests.campaign_fakes import crash_after_the_joint_stage_once

    status = await _cut_at(
        tmp_path,
        monkeypatch,
        lambda patch: crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final"),
    )
    (entry,) = status["rounds"]
    (final,) = entry["final"]
    assert final["selection"] is None and final["contrasts"] == []
    assert [member["complete"] for member in final["members"]] == [None, None]
    # The stage's rows are durable and its barrier never ran: the member's
    # completion (which runs the barrier first) is next.
    assert _kinds(status) == ["CompleteFinalMember"]
    run, result = await family14_public_run(tmp_path, monkeypatch)
    _assert_finished(run, result, [])


# -- `evo tree evaluate` from a family-14 source ------------------------------------------------


async def test_evaluation_of_a_family_14_campaign_keeps_its_source_unchanged(tmp_path, monkeypatch):
    """`test_tree_train_test.py::test_evaluation_of_campaign_v0_keeps_source_unchanged`
    over a genuine family-14 source: `evaluate` reads the source's verified
    inspection and its promoted `v1`, and writes nothing under it. `/15`
    admission is withdrawn again before the evaluation, so the reader does
    not depend on the runtime cap."""

    from evolution.tree.evaluation import evaluate
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_train_test import evaluation_manifest

    with monkeypatch.context() as patch:
        source, result = await family14_public_run(tmp_path / "source", patch)
    result_document, _ = _documents(result)
    assert result_document["campaign_outcome"]["champion_path"] is not None
    status = _inspected(source)
    assert status["lineage"]["champions"] == ["v0", "v1"]
    before = {
        str(file.relative_to(source)): file.read_bytes()
        for file in source.rglob("*")
        if file.is_file()
    }
    path = evaluation_manifest(tmp_path)
    document = json.loads(path.read_text())
    document["snapshot"] = {"run_dir": "source/run", "version": "v1"}
    path.write_text(json.dumps(document))

    evaluated = await evaluate(path, tmp_path / "run", adapter_factory=lambda *args: FakeAdapter())

    assert evaluated["state"] == "completed"
    assert evaluated["origin"]["version"] == "v1"
    after = {
        str(file.relative_to(source)): file.read_bytes()
        for file in source.rglob("*")
        if file.is_file()
    }
    assert after == before


# -- U3 carry: a `first_accepted` round with several final entries ---------------------------------


@pytest.mark.parametrize("stability", ["unstable", "stable"])
async def test_a_first_accepted_round_reports_every_final_entry(tmp_path, monkeypatch, stability):
    """Two losing finalists, then a winner (`scenario` of the first-accepted
    run module): one `final` entry per binding, ordinals 1..3, the selection
    and the next baseline on the last entry only (plan choice 6), one
    contrast per entry and each entry's own `listed` rows (choice 7)."""

    from tests.test_tree_campaign_family14_first_accepted_run import scenario

    options = scenario(tmp_path, monkeypatch, stability=stability)
    config = options.pop("config")
    run, result = await family14_public_run(tmp_path, monkeypatch, config, **options)
    documents = (*_documents(result), _inspected(run))
    for document in documents:
        (entry,) = document["rounds"]
        assert entry["outcome"] == "promoted"
        finals = entry["final"]
        assert [final["ordinal"] for final in finals] == [1, 2, 3]
        finalists = [attempt["attempt_id"] for attempt in entry["attempts"]]
        assert len(finalists) == 3
        for ordinal, final in enumerate(finals, 1):
            last = ordinal == len(finals)
            assert final["stability"] == stability
            finalist = f"m-r01-final-{ordinal:02d}-{finalists[ordinal - 1].split('-')[1]}"
            members = [(m["role"], m["measurement_id"]) for m in final["members"]]
            if stability == "stable":
                assert final["stored_measurement_id"] == "m-r00-champion"
                assert members == [("finalist", finalist)]
            else:
                assert final["stored_measurement_id"] is None
                assert members == [
                    ("champion", f"m-r01-final-{ordinal:02d}-champion"),
                    ("finalist", finalist),
                ]
            (contrast,) = final["contrasts"]
            assert contrast["accepted"] is last
            assert (final["selection"] is not None) is last
            assert (final["next_baseline_measurement_id"] is not None) is last
            # No gap was listed, so no entry lists a row.
            assert final["listed"] == []
        # The winner is the last finalist; its fresh final measurement is the
        # next baseline under both stabilities.
        winner = f"m-r01-final-03-{finalists[2].split('-')[1]}"
        assert finals[-1]["selection"] == {
            "attempt_id": finalists[2],
            "measurement_id": winner,
            "reason": "finalist_selected",
        }
        assert finals[-1]["next_baseline_measurement_id"] == winner
    result_document, status_document = documents[:2]
    assert result_document["rounds"] == status_document["rounds"] == documents[2]["rounds"]


# -- H6-F3: a cut after an applied comparable-change close -------------------------------------
#
# Plan choice 17: `_applied_terminal_prefix` admits family 14, so the same
# command finishes the owed terminal state-first (`finish_owed_terminal`).
# The comparable change is the Target's settings (`_Resettable`), which offers
# `close` beside the in-place effects; the decision names `close`. A family-14
# `authorization_applied(effect: terminal)` is not reachable: `_Campaign.adopt`
# picks `terminal` only below family 7, so there is no such cell.


def _through_link(patch, link: Path) -> None:
    """Every later `_two_case_run` evolves `link` instead of its run directory."""

    from tests import campaign_drivers as fixtures

    # Assumes `_two_case_run` reads `fixtures.evolve` at call time, run directory second.
    real = fixtures.evolve

    async def evolve(path, _run, *args, **options):
        return await real(path, link, *args, **options)

    patch.setattr(fixtures, "evolve", evolve)


def _resettable_options(**family14_settings: Any) -> dict[str, Any]:
    from tests.campaign_drivers import EDITED, _Resettable

    return {
        "adapter": _Resettable(used=EDITED["used"]),
        "campaign": {"rounds": 2},
        "family14_settings": family14_settings or None,
    }


async def _close_cut_then_resume(
    tmp_path, monkeypatch, options: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Over a cut prefix: change the Target's settings, accept `close` and cut
    after `authorization_applied`; an unaccepted link is refused alone; the
    same command then appends exactly `campaign_finished`.

    Returns the cut prefix's records, the result and status documents and
    the verified inspection.
    """

    from evolution.tree.campaign import owes_applied_terminal
    from evolution.tree.evolve import EvolveRefused, canonical_run_dir_finding
    from tests.campaign_drivers import _records
    from tests.test_tree_campaign_family14_visibility_change import _decision

    run = tmp_path / "run"
    options["adapter"].settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_public_run(tmp_path, monkeypatch, **options)
    assert refused.value.code == "run_identity_changed"
    assert "close" in refused.value.effects
    assert [d.field for d in refused.value.differences] == ["identity.target.settings_hash"]
    decision = _decision(refused.value, "close", reason="close on the changed target")
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_close_applied")
        with pytest.raises(Crash):
            await family14_public_run(tmp_path, patch, authorizations=[decision], **options)
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "authorization_applied"
    assert records[-1]["payload"]["effect"] == "close"
    assert owes_applied_terminal(run) is True
    prefix = (run / "campaign.jsonl").read_bytes()

    # An unaccepted link to the run directory is the whole refusal (§5.3, §6):
    # no identity finding beside it, and nothing is written.
    link = tmp_path / "link"
    link.symlink_to(run, target_is_directory=True)
    finding = canonical_run_dir_finding(link)
    assert finding is not None
    with monkeypatch.context() as patch:
        _through_link(patch, link)
        with pytest.raises(EvolveRefused) as linked:
            await family14_public_run(tmp_path, patch, authorizations=[decision], **options)
    assert [(f.code, f.instance_digest) for f in linked.value.findings] == [
        ("run_dir_unsafe_entry", finding.instance_digest)
    ]
    assert (run / "campaign.jsonl").read_bytes() == prefix
    assert owes_applied_terminal(run) is True

    # The same command: exactly the owed terminal is appended, state-first.
    run, result = await family14_public_run(
        tmp_path, monkeypatch, authorizations=[decision], **options
    )
    assert (run / "campaign.jsonl").read_bytes().startswith(prefix)
    after = _records(run / "campaign.jsonl")
    assert [record["event"] for record in after[len(records) :]] == ["campaign_finished"]
    assert sum(record["event"] == "authorization_applied" for record in after) == 1
    finished = after[-1]["payload"]
    assert (finished["outcome"], finished["reason"]) == ("stopped", "condition_changed")
    assert owes_applied_terminal(run) is False
    result_document, status_document = _documents(result)
    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == (
        "stopped",
        "condition_changed",
    )
    status = _inspected(run)
    assert status["complete"] is True and status["suspended"] is False
    assert status["actions"] == [] and status["suspensions"] == []
    assert status_document["rounds"] == status["rounds"]
    # One connection block in all three documents.
    assert result_document["connection"] == status_document["connection"] == status["connection"]
    return records, result_document, status_document, status


@pytest.mark.parametrize("stability", ["unstable", "stable"])
@pytest.mark.parametrize("position", ["between_rounds", "final_stage"])
async def test_a_cut_after_an_applied_close_finishes_its_owed_terminal(
    tmp_path, monkeypatch, position, stability
):
    """H6-F3 over a comparable change, between rounds or inside a final stage."""

    from tests.campaign_fakes import crash_after_the_joint_stage_once

    options = _resettable_options(stability=stability)
    with monkeypatch.context() as patch:
        if position == "between_rounds":
            _cut_hook(patch, "after_round_closed")
        else:
            # The first final stage's rows durable, its barrier unrun.
            # `cohort_stage` runs every final stage jointly: the Champion
            # beside the finalist under `unstable`, the finalist alone under
            # `stable`.
            cut = crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final")
        with pytest.raises(Crash):
            await family14_public_run(tmp_path, patch, **options)
    status = _inspected(tmp_path / "run")
    (entry, *later) = status["rounds"]
    if position == "between_rounds":
        assert entry["outcome"] is not None and later == []
    else:
        assert len(cut) == 1 and entry["outcome"] is None
        (final,) = entry["final"]
        assert final["stability"] == stability and final["selection"] is None

    _prefix, result_document, status_document, status = await _close_cut_then_resume(
        tmp_path, monkeypatch, options
    )
    for document in (result_document, status_document):
        assert document["settings"]["stability"] == stability
    # The preflight passed before the cut; the close makes no new probe.
    assert _connection(status) == ("compatible", "compatible", 1)


async def test_a_close_applied_over_an_open_preflight_finishes_its_owed_terminal(
    tmp_path, monkeypatch
):
    """H6-F3 with the connection preflight open at the first cut: the close
    settles it `interrupted` first (ruling 19), so the owed terminal follows
    no open preflight, and the finished run keeps that outcome (ruling Q1)."""

    options = _resettable_options()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_connection_preflight_started")
        with pytest.raises(Crash):
            await family14_public_run(tmp_path, patch, **options)

    records, result_document, status_document, status = await _close_cut_then_resume(
        tmp_path, monkeypatch, options
    )
    assert [record["event"] for record in records[-3:]] == [
        "connection_preflight_started",
        "connection_preflight_interrupted",
        "authorization_applied",
    ]
    for document in (result_document, status_document, status):
        connection = document["connection"]
        assert {key: connection[key] for key in ("state", "outcome", "attempts", "detail")} == {
            "state": "interrupted",
            "outcome": "interrupted",
            "attempts": 1,
            "detail": None,
        }
