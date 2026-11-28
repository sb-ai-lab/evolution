"""ADR-0019: core's raw campaign journal only through the pinned adapter; journal facts."""
import asyncio
import hashlib
import json

import pytest

from evolution_lab.owner import core_raw
from evolution_lab.owner.models import ATTEMPT_ID, CORE_ID, REFLECTION_ID
from evolution_lab.owner.measurements import read_journal_facts, usage_of_events

from test_owner_cleanup import finished, prepared_target
from test_owner_target import ready_target
from b10_campaigns import offline_b10_campaigns  # noqa: F401 — five approved offline /16 fixtures
from b10_campaigns import offline_unplanned_campaign  # noqa: F401 — Core-authored invalid-reflection rounds


def run_finished_campaign(directory):
    """One real /15 scaffold campaign through Owner, as in ``test_owner_cleanup``.

    Returns ``(state_directory, operation_id, status)`` with the status read by the
    Owner that ran the campaign, after it left ``reserved``/``running``/``stopping``."""
    from evolution_lab.owner import Owner, ResourceRef

    state, bundle, prepared = prepared_target(directory)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            # The retained full-suite run spent the old 20s budget on round one;
            # allow the configured second round and settlement to finish, still bounded.
            return operation.id, await finished(owner, ResourceRef("operation", operation.id), seconds=120)

    operation_id, status = asyncio.run(scenario())
    return state, operation_id, status


@pytest.fixture(scope="session")
def finished_campaign(tmp_path_factory):
    return run_finished_campaign(tmp_path_factory.mktemp("campaign"))


@pytest.fixture
def finished_campaign_run(finished_campaign):
    state, operation_id, _ = finished_campaign
    return state / "artifacts" / operation_id / operation_id


def test_campaign_times_are_first_and_last_record(finished_campaign_run):
    first, last = core_raw.campaign_journal_times(finished_campaign_run)
    assert first.endswith("Z") and last.endswith("Z") and first <= last


def test_missing_campaign_journal_has_no_times(tmp_path):
    assert core_raw.campaign_journal_times(tmp_path / "absent") is None


def test_unsupported_core_is_refused(monkeypatch, finished_campaign_run):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        core_raw.campaign_journal_times(finished_campaign_run)
    assert caught.value.code == "core_raw_unsupported"


def test_journal_facts_sum_target_usage(finished_campaign_run):
    measurement = next((finished_campaign_run / "measurements").iterdir())
    facts = read_journal_facts(measurement)
    assert facts.first_at <= facts.last_at and facts.last_sequence > 0
    assert set(facts.usage) == {"input_tokens", "output_tokens", "unknown_calls", "coverage"}
    assert facts.usage["coverage"] == "target_calls_only"


def test_report_uses_the_adapter():
    import inspect
    import evolution_lab.report as report
    assert "append_log" not in inspect.getsource(report)


def _finished(usage=None):
    payload = {} if usage is None else {"usage": usage}
    return {"event": "target_send_finished", "timestamp": "2026-09-29T00:00:00Z", "payload": payload}


def test_usage_counts_unknown_and_missing_as_unknown_calls():
    events = [_finished({"input_tokens": 10, "output_tokens": 3, "unknown": False}),
              _finished({"input_tokens": None, "output_tokens": 2, "unknown": True}),
              _finished(), {"event": "target_send_started", "timestamp": "2026-09-29T00:00:00Z", "payload": {}}]
    assert usage_of_events(events) == {"input_tokens": 10, "output_tokens": 3, "unknown_calls": 2,
                                       "coverage": "target_calls_only"}


def test_attempt_outcome_reasons_of_family14():
    reasons = core_raw.attempt_outcome_reasons()
    assert "condition_changed" in reasons and "interrupted" in reasons
    assert {"gate_passed", "observe_finalist", "focus_below_margin",
            "causal_below_tolerance", "insufficient_repeats", "insufficient_calibration",
            "no_exam_impact"} <= reasons


def test_champion_suspensions_come_from_core_and_join_every_reason():
    """Spec 2026-10-13 §2.3: core a35 #194 - `champion_incomplete`, kept apart in core's CHAMPION_SUSPENSIONS."""
    from evolution.tree.campaign_schema import CHAMPION_SUSPENSIONS
    assert core_raw.champion_suspension_reasons() == {"champion_incomplete"} == set(CHAMPION_SUSPENSIONS)
    assert core_raw.champion_suspension_reasons() <= core_raw.suspension_reasons()


def test_champion_suspension_reasons_refuse_another_core(monkeypatch):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.champion_suspension_reasons()
    assert raised.value.code == "core_raw_unsupported"


def test_family14_vocabularies_are_complete():
    from evolution.tree.campaign_schema import (
        CHAMPION_SUSPENSIONS, SUSPENSION_REASONS, _FINAL_ATTEMPT_OUTCOMES, _FINAL_FEEDBACK_OUTCOMES)
    from evolution.tree.final_statistics import ATTEMPT_DECISIONS14
    from evolution.tree.final_report import SELECTION_REASONS14
    from evolution.tree.campaign_caveats import CAVEAT_KINDS14
    expected = {outcome: frozenset(reasons) | frozenset(_FINAL_FEEDBACK_OUTCOMES[outcome])
                for outcome, reasons in _FINAL_ATTEMPT_OUTCOMES.items()}
    assert core_raw.LEGACY_FAMILIES == (14,)
    assert core_raw.attempt_outcome_table(14) == expected
    assert core_raw.attempt_outcome_reasons() == frozenset().union(*expected.values())
    assert core_raw.confidence_codes()["decisions"] == set(ATTEMPT_DECISIONS14)
    assert core_raw.selection_reasons() == set(SELECTION_REASONS14)
    assert core_raw.suspension_reasons() == set(SUSPENSION_REASONS) | set(CHAMPION_SUSPENSIONS) | {"final_incomplete"}
    assert "cohort_incomplete" not in core_raw.suspension_reasons()
    assert core_raw.caveat_kinds() == set(CAVEAT_KINDS14)
    assert core_raw.final_vocabulary() == {"gate_pass_outcome": "finalist", "contrast_key": "accepted",
                                           "incomplete_reason": "final_incomplete",
                                           "focus_rejection_reason": "focus_below_margin"}


def test_caveat_kinds_are_family14s_whole_vocabulary():
    lineage = {"condition_changed", "predecessor_unfinished", "baseline_inherited", "condition_changed_in_place"}
    assert lineage | {"grouping_fallback", "stored_pairs_excluded", "exact_contrast", "noise_not_observed"} <= core_raw.caveat_kinds()


def test_cohort_roles_of_the_pin():
    assert core_raw.confidence_codes()["cohort_roles"] == {"champion", "finalist"}


def test_confidence_codes_refused_on_other_core(monkeypatch):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.confidence_codes()
    assert raised.value.code == "core_raw_unsupported"

    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.caveat_kinds()
    assert raised.value.code == "core_raw_unsupported"

    for call in (core_raw.selection_reasons, core_raw.status_action_names, core_raw.final_vocabulary,
                 lambda: core_raw.attempt_outcome_table(14)):
        with pytest.raises(core_raw.CoreRawUnavailable) as raised:
            call()
        assert raised.value.code == "core_raw_unsupported"


def test_attempt_outcome_reasons_refused_on_other_core(monkeypatch):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.attempt_outcome_reasons()
    assert raised.value.code == "core_raw_unsupported"


def test_reflection_terminal_statuses_of_a21():
    # core's REFLECTION_STATUSES (reflection_finished) plus the failed terminal of a failed reflection.
    assert core_raw.reflection_terminal_statuses() == {"reflected", "declined", "failed"}


def test_reflection_terminal_statuses_refused_on_other_core(monkeypatch):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.reflection_terminal_statuses()
    assert raised.value.code == "core_raw_unsupported"


def test_role_suspension_reasons_match_lab_role_hints():
    from evolution_lab._suspension import ROLE_SUSPENSIONS
    # A new family-10 reason in core must reach `_suspension.ROLE_SUSPENSIONS` (a hint) on a re-pin.
    assert set(ROLE_SUSPENSIONS) == core_raw.role_suspension_reasons()


def test_scorer_hints_name_reasons_of_the_pin():
    from evolution_lab._suspension import SCORER_SUSPENSIONS
    # A hint must not outlive its core reason on a re-pin (spec 2026-10-03 §3.3).
    assert set(SCORER_SUSPENSIONS) <= core_raw.suspension_reasons()


def test_role_suspension_reasons_refused_on_other_core(monkeypatch):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.role_suspension_reasons()
    assert raised.value.code == "core_raw_unsupported"


def _journal_copy(run, tmp_path, mutate=None):
    """A copy of a real run's campaign journal (and creation facts) that a test may damage."""
    import shutil
    copy = tmp_path / "run"
    shutil.copytree(run, copy, ignore=shutil.ignore_patterns("measurements", "artifacts", "*-logs", "scorer-traces"))
    journal = copy / "campaign.jsonl"
    if mutate is not None:
        journal.write_bytes(mutate(journal.read_bytes()))
    return copy


def test_campaign_details_of_a_real_run(finished_campaign_run):
    details = core_raw.campaign_details(finished_campaign_run)
    assert details["journal"] == "present"
    rounds = details["rounds"]
    assert rounds and rounds[min(rounds)]["baseline_version"] == "v0"
    for row in rounds.values():
        assert set(row) == {"baseline_version", "diagnosis", "last_reflection", "attempts"}
        for entry in (row["diagnosis"], row["last_reflection"]):
            assert entry is None or set(entry) == {"reflection_id", "status", "groups", "error_class", "reason"}
            assert entry is None or entry["status"] in {"started", "reflected", "declined", "failed"}
        if row["diagnosis"] is not None and row["diagnosis"]["status"] == "reflected":
            for group in row["diagnosis"]["groups"]:
                assert isinstance(group["root_cause"], str) and isinstance(group["suggested_fix"], str)
        for attempt in row["attempts"].values():
            assert set(attempt) == {"editor_error", "editor_reason", "code"}


def test_campaign_details_missing_journal(tmp_path):
    assert core_raw.campaign_details(tmp_path / "absent") == {"journal": "missing", "rounds": {}}


@pytest.mark.parametrize("family", [14, 15])
@pytest.mark.parametrize("error_class,reason", [("cancelled", None), ("reported", "provider_exhausted"),
                                                ("reported", "unclassified")])
def test_campaign_details_preserves_resilient_diagnosis(tmp_path, monkeypatch, family, error_class, reason):
    """Both supported families ignore lifecycle/provider failures, but retain the last reflection."""
    from types import SimpleNamespace
    from evolution.tree import campaign_schema

    settled = {"reflection_id": "r01-x01", "status": "reflected", "groups": [],
               "error_class": None, "reason": None}
    failed = {"reflection_id": "r01-x02", "status": "failed", "groups": [],
              "error_class": error_class, "reason": reason}
    state = campaign_schema.RoundState(round=1, reflections=(settled, failed))
    assert state.diagnosis(True) == settled  # Core's real diagnosis, not a Lab imitation.
    assert state.diagnosis(False) == failed
    (tmp_path / "campaign.jsonl").touch()
    record = SimpleNamespace(to_payload=lambda: {"event": "baseline_bound", "payload": {"round": 1, "champion_version": "v0"}})
    monkeypatch.setattr(core_raw, "campaign_log", lambda *a, **kw: SimpleNamespace(records=[record], journal_integrity="valid"))
    monkeypatch.setattr(core_raw, "_journal_family", lambda run: family)
    monkeypatch.setattr(campaign_schema, "reduce", lambda *a, **kw: SimpleNamespace(family_version=family, rounds={1: state}))
    row = core_raw.campaign_details(tmp_path)["rounds"][1]
    assert row["diagnosis"] == settled
    assert row["last_reflection"] == failed


@pytest.mark.parametrize("mutate", [
    lambda raw: b"",                                            # empty file: no valid record
    lambda raw: b"{not json}\n" + raw,                          # broken first line
    lambda raw: raw + b"{not json}\n",                          # corrupt suffix after valid lines
])
def test_campaign_details_invalid_journal_is_unreadable(finished_campaign_run, tmp_path, mutate):
    run = _journal_copy(finished_campaign_run, tmp_path, mutate)
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.campaign_details(run)
    assert raised.value.code == "unreadable"


def test_campaign_details_incomplete_tail_reads_the_valid_prefix(finished_campaign_run, tmp_path):
    whole = core_raw.campaign_details(finished_campaign_run)
    run = _journal_copy(finished_campaign_run, tmp_path, lambda raw: raw + b'{"schema_version":1')
    assert core_raw.campaign_details(run) == whole


def test_campaign_details_refused_on_other_core(monkeypatch, finished_campaign_run):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.campaign_details(finished_campaign_run)
    assert raised.value.code == "core_raw_unsupported"


def test_campaign_details_reads_a_journal_with_repairs_dispatched(tmp_path):
    """Core a24 (#78) writes `repairs_dispatched` before a threshold resume's repairs; the adapter's
    reducer path must read such a journal (ADR-0019 contract on the pin)."""
    import json
    from evolution.tree.campaign import CAMPAIGN_FILE
    from evolution_lab.owner import Owner
    from test_owner_cleanup import finished
    from test_owner_resume import _replace_target, _suspended_operation, _suspending
    state, bundle, prepared, flag = _suspending(tmp_path)

    async def scenario():
        async with Owner.open(state) as owner:
            ref, first, _ = await _suspended_operation(owner, bundle, prepared)
            second = await _replace_target(owner, bundle, first)
            flag.unlink()
            await owner.resume(ref, target_ref=second, secrets={})
            done = await finished(owner, ref, seconds=120)
            assert done.control_state == "finished", done.to_payload()
            return state / "artifacts" / ref.id / ref.id

    run = asyncio.run(scenario())
    events = [json.loads(line)["event"] for line in (run / CAMPAIGN_FILE).read_text().splitlines() if line]
    assert "repairs_dispatched" in events
    details = core_raw.campaign_details(run)
    assert details["journal"] == "present" and details["rounds"], details


def test_suspension_reasons_of_the_pin_include_the_scorer_reasons():
    reasons = core_raw.suspension_reasons()
    assert {"scorer_metric_missing", "scorer_metric_out_of_range", "target_unstable"} <= reasons
    assert core_raw.role_suspension_reasons() <= reasons


def test_suspension_reasons_refused_on_other_core(monkeypatch):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.suspension_reasons()
    assert raised.value.code == "core_raw_unsupported"


def test_corpus_cases_read_a_partition_as_core_does(tmp_path):
    from evolution.tree.execution.corpus import CorpusError
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"case_id": "q1", "group_id": "g1", "stratum": "s", "public": {}, "split": "train"}\n'
                      '{"case_id": "q2", "group_id": "g2", "stratum": "s", "public": {}, "split": "test"}\n')
    assert [case.case_id for case in core_raw.corpus_cases(corpus)] == ["q1", "q2"]
    assert [case.case_id for case in core_raw.corpus_cases(corpus, split="test")] == ["q2"]
    corpus.write_text('{"case_id": "q1", "group_id": "g1", "stratum": "s", "public": {}}\n')
    assert [case.case_id for case in core_raw.corpus_cases(corpus, split="train")] == ["q1"]
    with pytest.raises(CorpusError) as caught:
        core_raw.corpus_cases(corpus, split="test")
    assert caught.value.code == "empty_split"


def test_corpus_cases_refused_on_other_core(monkeypatch, tmp_path):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.corpus_cases(tmp_path / "corpus.jsonl")
    assert raised.value.code == "core_raw_unsupported"


@pytest.mark.parametrize("value", ["case-1", "кейс-1", "case+1", "case@1", "a:b.c", "00", "a-00"])
def test_core_id_accepts_core_identifiers(value):
    assert CORE_ID.match(value)


@pytest.mark.parametrize("value", ["", ".hidden", "..", "a/b", "a%2Fb", "-x", "a" * 257, "a b"])
def test_core_id_refuses_others(value):
    assert CORE_ID.match(value) is None


def test_attempt_and_reflection_ids():
    assert ATTEMPT_ID.match("r01-a02") and ATTEMPT_ID.match("r100-a12")
    assert REFLECTION_ID.match("r01-x01")
    assert ATTEMPT_ID.match("r01-x01") is None and REFLECTION_ID.match("r1-a1") is None


def test_read_inside_reads_regular_files_only_inside_the_run(tmp_path):
    run = tmp_path / "run"; (run / "editor-logs").mkdir(parents=True)
    (run / "editor-logs" / "r01-a01.jsonl").write_text('{"phase":"failed"}\n')
    assert core_raw.read_inside(run, "editor-logs", "r01-a01.jsonl") == b'{"phase":"failed"}\n'
    outside = tmp_path / "secret.txt"; outside.write_text("x")
    (run / "editor-logs" / "link.jsonl").symlink_to(outside)
    (run / "reflector-logs").symlink_to(tmp_path)
    for parts in (("editor-logs", "link.jsonl"), ("reflector-logs", "secret.txt"), ("..", "secret.txt")):
        with pytest.raises(core_raw.CoreRawUnavailable) as raised:
            core_raw.read_inside(run, *parts)
        assert raised.value.code == "unreadable", parts
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.read_inside(run, "editor-logs", "absent.jsonl")
    assert raised.value.code == "missing"


def test_read_inside_refuses_large_files(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    (run / "big").write_bytes(b"x" * 11)
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.read_inside(run, "big", limit=10)
    assert raised.value.code == "too_large"


def test_read_jsonl_keeps_records_before_a_broken_last_line(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    (run / "log.jsonl").write_text('{"phase":"a"}\n{"phase":"b"}\n{"pha')
    assert core_raw.read_jsonl(run, "log.jsonl") == ([{"phase": "a"}, {"phase": "b"}], True)
    (run / "mid.jsonl").write_text('{"phase":"a"}\nnot json\n{"phase":"b"}\n')
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.read_jsonl(run, "mid.jsonl")
    assert raised.value.code == "unreadable"


def test_content_readers_refused_on_other_core(monkeypatch, tmp_path):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.read_inside(tmp_path, "x")
    assert raised.value.code == "core_raw_unsupported"


def _journal(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"event": e, "payload": p}) + "\n" for e, p in events))


def test_scoring_of_takes_the_last_finished_try(tmp_path):
    journal = tmp_path / "measurements" / "m-r01-a01" / "events.jsonl"
    base = {"case_id": "кейс+1", "sample_id": "00"}
    _journal(journal, [
        ("scoring_started", {**base, "score_request_id": "a" * 32, "retry_ordinal": 0}),
        ("scoring_failed", {**base, "score_request_id": "a" * 32, "failure": "scorer_error"}),
        ("scoring_started", {**base, "score_request_id": "b" * 32, "retry_ordinal": 1}),
        ("scoring_finished", {**base, "score_request_id": "b" * 32, "metrics": {"m": 1.0}, "critical_pass": True}),
        ("scoring_started", {"case_id": "other", "sample_id": "00", "score_request_id": "c" * 32}),
    ])
    assert core_raw.scoring_of(journal, "кейс+1", "00") == {
        "state": "finished", "score_request_id": "b" * 32, "retry_ordinal": 1, "tries_total": 2,
        "metrics": {"m": 1.0}, "critical_pass": True}
    assert core_raw.scoring_of(journal, "other", "00")["state"] == "incomplete"
    assert core_raw.scoring_of(journal, "absent", "00")["state"] == "not_scored"


def _trace(root, rid, case_id="c1", *, tamper=False):
    payload = {"case_id": case_id, "public": {"q": "1"}, "oracle": {"a": "2"}, "view": {"history": {"turns": []},
               "terminal": "completed", "target": {}}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    directory = root / "scorer-traces" / "scorer_traces" / rid
    directory.mkdir(parents=True)
    if tamper:
        payload["public"] = {"q": "changed"}
    (directory / "request.json").write_text(json.dumps({"scorer": "s", "trace_id": rid, "input": payload,
                                                        "input_sha256": digest}, indent=2))
    (directory / "stdout").write_text('{"metrics": {"m": 1}}')


def test_scorer_trace_checks_integrity(tmp_path):
    _trace(tmp_path, "a" * 32)
    trace = core_raw.scorer_trace(tmp_path, "a" * 32, "c1", measurement_id="m-campaign")
    assert trace["input"]["oracle"] == {"a": "2"} and trace["stdout"] == '{"metrics": {"m": 1}}'
    for rid, case_id, code in (("a" * 32, "c2", "unreadable"), ("f" * 32, "c1", "missing"),
                               ("../x", "c1", "unreadable")):
        with pytest.raises(core_raw.CoreRawUnavailable) as raised:
            core_raw.scorer_trace(tmp_path, rid, case_id, measurement_id="m-campaign")
        assert raised.value.code == code
    _trace(tmp_path, "b" * 32, tamper=True)
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.scorer_trace(tmp_path, "b" * 32, "c1", measurement_id="m-campaign")
    assert raised.value.code == "unreadable"


def test_role_files(tmp_path):
    (tmp_path / "editor-logs").mkdir()
    (tmp_path / "editor-logs" / "r01-a01.jsonl").write_text('{"phase":"failed","note":"x"}\n')
    (tmp_path / "reflector-logs").mkdir()
    (tmp_path / "reflector-logs" / "r01-x01.stderr").write_text("e" * 20000 + "END")
    assert core_raw.role_log(tmp_path, "editor", "r01-a01") == ([{"phase": "failed", "note": "x"}], False)
    tail = core_raw.role_stderr(tmp_path, "reflector", "r01-x01")
    assert tail.endswith("END") and len(tail) == 20003
    with pytest.raises(core_raw.CoreRawUnavailable):
        core_raw.role_log(tmp_path, "scorer", "r01-a01")


def test_campaign_content_of_a_finished_campaign(finished_campaign_run):
    found = core_raw.campaign_content(finished_campaign_run)
    assert found["closed"] and all(ATTEMPT_ID.match(a) for a in found["closed"])
    assert all(REFLECTION_ID.match(r) for r in found["reflections"])
    assert all(isinstance(v, (list, int)) for v in found["changed"].values())


def test_instance_demand_is_cores_admission_for_family_14(tmp_path):
    """Spec 2026-10-13 §5.2: the /15 scaffold needs 2 instances (core admits 2; Lab's old family-13 formula asked 4);
    the observe smoke uses fresh_parent and needs 4 instances."""
    from evolution.tree import load_manifest_v2
    from evolution.tree.reference_target_cli import scaffold
    from scaffold_15 import scaffold_15
    raw = load_manifest_v2(scaffold(tmp_path / "raw").manifest).workflow.campaign
    smoke = load_manifest_v2(scaffold_15(tmp_path / "smoke").manifest).workflow.campaign
    assert core_raw.instance_demand(raw, 1) == 2
    n = core_raw.instance_demand(smoke, 1)
    assert n == 4
    assert core_raw.capacity_admitted(smoke, max_instances=n, max_concurrent_sessions=1)
    assert not core_raw.capacity_admitted(smoke, max_instances=n - 1, max_concurrent_sessions=1)


def test_family_reader_refuses_real_old_prefix():
    from pathlib import Path
    from evolution.tree.campaign import CampaignFamilyUnsupported
    run = Path(__file__).parent / "fixtures/campaign-family-refusal"
    with pytest.raises(CampaignFamilyUnsupported) as raised:
        core_raw.inspect_family14_view(run)
    assert raised.value.journaled == 13
    with pytest.raises(CampaignFamilyUnsupported):
        core_raw.campaign_status(run)


@pytest.mark.parametrize("content", [b"", b"broken\n", b'{"event":"campaign_created"'])
def test_family_reader_requires_validated_identity(tmp_path, content):
    from evolution.tree import CampaignUnreadable
    (tmp_path / "campaign.jsonl").write_bytes(content)
    with pytest.raises(CampaignUnreadable):
        core_raw.campaign_status(tmp_path)


def test_shared_reader_does_not_wrap_unexpected_error(tmp_path, monkeypatch):
    def fail(*a, **k):
        raise TypeError("unexpected")
    monkeypatch.setattr("evolution.tree.campaign_projections.inspect_view", fail)
    with pytest.raises(TypeError, match="unexpected"):
        core_raw.campaign_status(tmp_path)


def test_status_uses_single_admitted_view(finished_campaign_run, monkeypatch):
    from evolution.tree import campaign_projections
    real = campaign_projections.inspect_view
    seen = []
    def read(*a, **k):
        seen.append(1)
        return real(*a, **k)
    monkeypatch.setattr(campaign_projections, "inspect_view", read)
    payload = core_raw.campaign_status(finished_campaign_run).to_payload()
    assert payload["run_id"] and seen == [1]


# Family 15: Core calls monkeypatched with neutral payloads. Family-15 run directories created before Core
# dc43169507a8 are unreadable by the new Core, so no recorded run stands in for one here.
INDEPENDENT = {"evidence": "authenticated_publications", "next_action": "continue_evolve", "attempts": []}
STATUS_PAYLOAD = {"kind": "campaign_status", "run_id": "run-1", "family_version": 15, "independent": INDEPENDENT}


class _Projection:
    def __init__(self, payload):
        self._payload = payload

    def to_payload(self):
        return self._payload


def _core_calls(monkeypatch, family, status_payload, calls):
    """Replace Core's view, status and result projections; record each call's keyword arguments."""
    from types import SimpleNamespace
    import evolution.tree as tree
    from evolution.tree import campaign_projections, family15_report

    view = SimpleNamespace(snapshot=SimpleNamespace(identity={"run": "r-1"}, family_version=family, rounds={}),
                           tries={"m-1": object()}, affected_items={1: ()})

    def inspect_view(run, *, verify=False):
        calls.append({"fn": "inspect", "run": run, "verify": verify})
        return view

    def project_status(seen, **kwargs):
        assert seen is view
        calls.append({"fn": "status", **kwargs})
        return _Projection(status_payload)

    def project_result(snapshot, **kwargs):
        assert snapshot is view.snapshot
        calls.append({"fn": "result", **kwargs})
        return _Projection({"kind": "campaign_result"})

    monkeypatch.setattr(campaign_projections, "inspect_view", inspect_view)
    monkeypatch.setattr(campaign_projections, "project_status", project_status)
    monkeypatch.setattr(tree, "project_campaign_result", project_result)
    monkeypatch.setattr(family15_report, "campaign15_projection",
                        lambda *a, **k: pytest.fail("a second independent projection"))
    monkeypatch.setattr(core_raw, "_family15_prefix", lambda *a: ((), "complete"), raising=False)
    return view


def test_family15_read_authenticates_once_and_feeds_the_result(monkeypatch, tmp_path):
    calls = []
    view = _core_calls(monkeypatch, 15, STATUS_PAYLOAD, calls)
    read = core_raw.campaign_read(tmp_path, with_result=True)
    assert read.family == 15 and read.view is view
    assert read.status.to_payload() is STATUS_PAYLOAD and read.result.to_payload() == {"kind": "campaign_result"}
    assert [c["fn"] for c in calls] == ["inspect", "status", "result"]
    assert [c["run_root"] for c in calls if c["fn"] == "status"] == [tmp_path]          # exactly one
    assert calls[-1] == {"fn": "result", "tries": view.tries, "affected_items": view.affected_items,
                         "independent_report": INDEPENDENT}
    assert calls[-1]["independent_report"] is STATUS_PAYLOAD["independent"]
    assert read.bindings == {} and read.bindings_reason is None


def test_family15_read_without_result_projects_status_only(monkeypatch, tmp_path):
    calls = []
    _core_calls(monkeypatch, 15, STATUS_PAYLOAD, calls)
    read = core_raw.campaign_read(tmp_path)
    assert read.result is None
    assert [c["fn"] for c in calls] == ["inspect", "status"]


def test_family14_read_keeps_todays_calls(monkeypatch, tmp_path):
    calls = []
    view = _core_calls(monkeypatch, 14, {"kind": "campaign_status", "run_id": "run-1"}, calls)
    read = core_raw.campaign_read(tmp_path, with_result=True)
    assert read.family == 14
    assert calls == [{"fn": "inspect", "run": tmp_path, "verify": False},
                     {"fn": "status", "secrets": (), "verification": None},
                     {"fn": "result", "tries": view.tries}]


def test_legacy_entry_points_refuse_family_15(monkeypatch, tmp_path):
    from evolution.tree.campaign import CampaignFamilyUnsupported
    calls = []
    view = _core_calls(monkeypatch, 15, STATUS_PAYLOAD, calls)
    with pytest.raises(CampaignFamilyUnsupported):
        core_raw.campaign_status(tmp_path)          # H1 and runner stance
    with pytest.raises(CampaignFamilyUnsupported):
        core_raw.campaign_read(tmp_path, families=core_raw.LEGACY_FAMILIES)
    with pytest.raises(CampaignFamilyUnsupported):
        core_raw.result_of_view(view)
    assert core_raw.FAMILIES == (14, 15) and core_raw.LEGACY_FAMILIES == (14,)
    assert not [c for c in calls if c["fn"] != "inspect"]


def test_family_read_requires_a_validated_identity(monkeypatch, tmp_path):
    from evolution.tree import CampaignUnreadable
    view = _core_calls(monkeypatch, 15, STATUS_PAYLOAD, [])
    view.snapshot.identity = {}
    with pytest.raises(CampaignUnreadable):
        core_raw.campaign_read(tmp_path)


def test_unauthenticated_family15_is_a_state(monkeypatch, tmp_path):
    unavailable = {"evidence": "unavailable", "next_action": "repair_evidence", "attempts": []}
    calls = []
    _core_calls(monkeypatch, 15, {**STATUS_PAYLOAD, "independent": unavailable}, calls)
    read = core_raw.campaign_read(tmp_path, with_result=True)
    assert read.status.to_payload()["independent"]["evidence"] == "unavailable"
    assert calls[-1]["independent_report"] is unavailable


def test_family15_read_refused_on_other_core(monkeypatch, tmp_path):
    _core_calls(monkeypatch, 15, STATUS_PAYLOAD, [])
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    with pytest.raises(core_raw.CoreRawUnavailable) as raised:
        core_raw.campaign_read(tmp_path)
    assert raised.value.code == "core_raw_unsupported"


def test_decode_rational_is_cores():
    from fractions import Fraction
    from evolution.tree.family15_schema import encode_rational
    assert core_raw.decode_rational(encode_rational(Fraction(-3, 25))) == Fraction(-3, 25)


def test_binding_and_projection_helpers_refuse_another_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    for call in (lambda: core_raw.family15_bindings(tmp_path, None),
                 lambda: core_raw.family15_stage_facts(tmp_path, None),
                 lambda: core_raw.validate_family15_projection({})):
        with pytest.raises(core_raw.CoreRawUnavailable, match="core_raw_unsupported"):
            call()


@pytest.mark.parametrize("modes, expected", [
    (None, []),
    ({}, []),
    ({"seal_oracle_leak": "off", "connection_preflight": "shadow",
      "reflection_oracle_leak": "enforce"}, [
        "check seal_oracle_leak is off: a Candidate that writes an oracle string into the skill is measured and can be promoted",
        "check connection_preflight is shadow: the campaign starts without a passed connection preflight",
    ]),
])
def test_check_mode_notes_copy_core_consequences(tmp_path, modes, expected):
    from evolution.tree import load_manifest_v2
    from scaffold_16 import scaffold_16

    manifest = scaffold_16(tmp_path / "manifest").manifest
    document = json.loads(manifest.read_text())
    if modes is not None:
        document["checks"] = {"version": "checks/1", "modes": modes}
    manifest.write_text(json.dumps(document))
    workflow = load_manifest_v2(manifest).workflow

    assert core_raw.check_mode_notes(workflow) == expected


@pytest.mark.parametrize("extra, expected", [
    ({}, None),
    ({"check_modes": {"seal_oracle_leak": "off"}},
     {"modes": {"seal_oracle_leak": "off"}, "shadow_hits": {}}),
    ({"shadow_hits": {"connection_preflight": 2}},
     {"modes": {}, "shadow_hits": {"connection_preflight": 2}}),
    ({"check_modes": {"seal_oracle_leak": "off", "connection_preflight": "shadow"},
      "shadow_hits": {"connection_preflight": 3}},
     {"modes": {"seal_oracle_leak": "off", "connection_preflight": "shadow"},
      "shadow_hits": {"connection_preflight": 3}}),
])
def test_family15_checks_copy_optional_projection_keys(extra, expected):
    from test_owner_independent_view import BLOCK

    block = {**BLOCK, **extra}
    result = core_raw.family15_checks(block)
    assert result == expected
    if result is not None:
        result["modes"]["seal_oracle_leak"] = "shadow"
        result["shadow_hits"]["connection_preflight"] = 99
        assert block.get("check_modes", {}) == expected["modes"]
        assert block.get("shadow_hits", {}) == expected["shadow_hits"]


@pytest.mark.parametrize("extra", [
    {"check_modes": {"unknown_check": "off"}},
    {"check_modes": {"seal_oracle_leak": "enforce"}},
    {"shadow_hits": {"unknown_check": 1}},
])
def test_family15_checks_refuse_projection_the_pin_does_not_describe(extra):
    from test_owner_independent_view import BLOCK

    with pytest.raises(ValueError, match="family-15 projection not described by this pin"):
        core_raw.family15_checks({**BLOCK, **extra})


def test_check_helpers_refuse_another_pin(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(core_raw, "_installed_core", lambda: "0.0.0")
    for call in (lambda: core_raw.check_mode_notes(SimpleNamespace(checks=None)),
                 lambda: core_raw.family15_checks({})):
        with pytest.raises(core_raw.CoreRawUnavailable, match="core_raw_unsupported"):
            call()


def test_invalid_reflection_round_reads_as_unplanned(offline_unplanned_campaign):
    from evolution_lab.owner.campaign_views import campaign_view
    from evolution_lab.owner.progress import CampaignPlan
    from evolution_lab.cli.responses import validate
    from evolution_lab.report import read_campaign_report

    expected_rounds = {"unplanned-then-continue": 2, "only-unplanned": 1}
    assert set(offline_unplanned_campaign) == set(expected_rounds)
    for name, run in offline_unplanned_campaign.items():
        read = core_raw.campaign_read(run, with_result=True)
        core = read.status.to_payload()
        assert len(core["rounds"]) == expected_rounds[name], name
        first = core["rounds"][0]
        assert first["round"] == 1 and first["outcome"] == "unplanned", name
        assert first["attempts"] == [], name
        content = core_raw.campaign_content(run)
        assert any(row["event"] == "reflection_failed" and row["reason"] == "invalid_result"
                   for row in content["reflections"].values()), name
        view = campaign_view("o-unplanned", core,
                             CampaignPlan("manifest.json", expected_rounds[name], 1, "score"),
                             None, None, None, [], core_raw.campaign_details(run, read=read))
        assert view["details_state"] == "present", (name, view["details_reason"])
        validate(view, "lab-campaign-view/15")
        assert view["rounds"][0]["attempts"] == [], name
        report = read_campaign_report(run, families=core_raw.FAMILIES, read=read)
        assert "не читается" not in report.markdown(), name


def test_b10_options_read_through_views_and_report(offline_b10_campaigns):
    from evolution_lab.owner.campaign_views import campaign_view
    from evolution_lab.owner.progress import CampaignPlan
    from evolution_lab.cli.responses import validate
    from evolution_lab.report import read_campaign_report
    for name, run in offline_b10_campaigns.items():
        read = core_raw.campaign_read(run, with_result=True)
        core = read.status.to_payload()
        assert core["independent"]["evidence"] == "authenticated_publications", name
        details = core_raw.campaign_details(run, read=read)
        view = campaign_view("o-b10", core, CampaignPlan("manifest.json", 1, 1, "score"),
                             None, None, None, [], details)
        assert view["details_state"] == "present", (name, view["details_reason"])
        assert view["schema"] == "lab-campaign-view/15"
        validate(view, "lab-campaign-view/15")
        report = read_campaign_report(run, families=core_raw.FAMILIES, read=read)
        assert report.data["lab_independent_state"] == "present", name
        assert report.data["lab_independent"] == view["independent"]
        assert "не читается" not in report.markdown()
        for raw, projected in zip(core["independent"]["attempts"], view["independent"]["attempts"], strict=True):
            for layer in ("gate_comparison", "global_comparison"):
                before, after = raw[layer], projected[layer]
                if before is None:
                    assert after is None
                    continue
                assert after["caveats"] == before.get("caveats")
                assert after["bound_fallback"] == before.get("bound_fallback")
                assert after["method"] == before["method"] and after["unit"] == before["unit"]
                if "calibration" in before:
                    assert after["calibration"]["prefix"] == before["calibration"]["prefix"]
                    for key in ("level", "allowance"):
                        assert after["calibration"][key]["exact"] == before["calibration"][key]
                for a, b in zip(before["scopes"], after["scopes"], strict=True):
                    assert b["fallback"] == a.get("fallback")
                    assert b["exact"] == a.get("exact") and b["pairing"] == a.get("pairing")
                    assert (None if b["lower"] is None else b["lower"]["exact"]) == a.get("lower")
        if name == "statistical":
            assert view["independent"]["settings"]["mode"] == "statistical"
            assert any(s["rule"] == "bootstrap" for a in view["independent"]["attempts"]
                       for s in a["gate_comparison"]["scopes"])
            from evolution_lab.cli.output import independent_lines
            assert any("bootstrap 10%, lower" in line for line in independent_lines(view["independent"]))
            assert "bootstrap" in report.markdown()
        if name in ("noise-stored", "noise-fresh"):
            assert any(a["gate_comparison"]["calibration"] is not None for a in view["independent"]["attempts"])
        if name == "noise-fresh":
            assert "causal=bound" in report.markdown()
            assert "causal: правило результата point;" in report.markdown()
            assert "causal_unconfirmable" in report.markdown()
        if name == "fallback-point":
            assert "слабее: точечное решение вместо границы" in report.markdown()


def test_fresh_parent_causal_rule_changes_between_binding_and_result(offline_b10_campaigns):
    from evolution_lab.owner.independent_view import independent_view
    run = offline_b10_campaigns["noise-fresh"]
    read = core_raw.campaign_read(run, with_result=True)
    block = independent_view(read.status.to_payload()["independent"], bindings=read.bindings)
    attempt, = block["attempts"]
    binding = next(s for s in attempt["gate_binding"]["scopes"] if s["name"] == "causal")
    result = next(s for s in attempt["gate_comparison"]["scopes"] if s["name"] == "causal")
    assert binding == {"name": "causal", "rule": "bound"}
    assert result["rule"] == "point" and result["fallback"] == {"reason": "causal_unconfirmable"}
    from evolution_lab.cli.output import independent_lines
    lines = independent_lines(block)
    assert any("gate binding (core_raw):" in line and "causal=bound" in line for line in lines)
    assert any("causal: result rule=point" in line and "causal_unconfirmable" in line for line in lines)


def test_recorded_binding_authentication_failure_is_not_an_absent_binding(offline_b10_campaigns, monkeypatch):
    from evolution_lab.report import read_campaign_report
    from evolution.tree.family15_audit import Family15Audit, EvidenceMismatch
    run = offline_b10_campaigns["noise-fresh"]
    read = core_raw.campaign_read(run, with_result=True)
    assert read.bindings and read.bindings_reason is None

    def failed(*args, **kwargs):
        raise EvidenceMismatch("unavailable recorded binding")
    monkeypatch.setattr(Family15Audit, "publication", failed)
    with pytest.raises(EvidenceMismatch):
        core_raw.family15_bindings(run, read.view.snapshot)
    # Preserve the public snapshot and change only its raw binding read failure.
    from dataclasses import replace
    damaged = replace(read, bindings=None, bindings_reason="unreadable")
    details = core_raw.campaign_details(run, read=damaged)
    assert details["bindings"] is None and details["bindings_reason"] == "unreadable"
    report = read_campaign_report(run, families=core_raw.FAMILIES, read=damaged)
    assert report.data["lab_independent"] is None
    assert report.data["lab_independent_reason"] == "unreadable"


def test_binding_reader_rejects_a_different_snapshot(offline_b10_campaigns):
    from dataclasses import replace
    run = offline_b10_campaigns["no-option"]
    read = core_raw.campaign_read(run)
    with pytest.raises(core_raw.CoreRawUnavailable, match="unreadable"):
        core_raw.family15_bindings(run, replace(read.view.snapshot, producing_version="different"))


def test_c58r1_status_acquisition_does_not_authenticate_unused_bindings(monkeypatch, tmp_path):
    _core_calls(monkeypatch, 15, STATUS_PAYLOAD, [])
    calls = []
    monkeypatch.setattr(core_raw, "family15_bindings", lambda *a, **k: calls.append(k) or {})
    read = core_raw.campaign_read(tmp_path)
    assert read.result is None and calls == []


@pytest.mark.parametrize("method", ["status", "list_summaries"])
def test_c58r1_status_and_list_paths_do_not_authenticate_bindings(monkeypatch, tmp_path, method):
    from dataclasses import dataclass
    from types import SimpleNamespace
    from evolution_lab.owner import readers, summary
    from evolution_lab.owner.models import ProcessFacts, ResourceRef

    _core_calls(monkeypatch, 15, STATUS_PAYLOAD, [])
    calls = []
    monkeypatch.setattr(core_raw, "family15_bindings", lambda *a, **k: calls.append(k) or {})
    monkeypatch.setattr(summary, "operation_summary", lambda value: {"id": value.id})

    @dataclass
    class Status:
        id: str = "op15"
        kind: str = "campaign"
        prepared_digest: str = "d" * 64
        process: object = ProcessFacts()
        result: object = None
        results_state: str = "present"
        auto_resume: object = None

    class AutoResume:
        async def project(self, status):
            return None

    class Reader(readers.ReaderSupport):
        _result_io = SimpleNamespace(writing=lambda operation: False, acquire_read=lambda *a: None)
        _summary_cache = {}
        _auto_resume = AutoResume()

        def _assert_loop(self):
            pass

        async def _db(self, name):
            assert name == "list_operations"
            return [Status()]

        async def _status_projection(self, ref):
            return readers._project_campaign(tmp_path, Status())

        async def _result_work(self, work, lease):
            return await work()

    reader = Reader()
    asyncio.run(reader.status(ResourceRef("operation", "op15")) if method == "status" else reader.list_summaries())
    assert calls == []


def test_c58r1_binding_read_accepts_only_an_unchanged_acquired_prefix(offline_b10_campaigns):
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.storage.envelope import Envelope
    run = offline_b10_campaigns["noise-fresh"]
    context, _ = _evidence_of(run)
    count = next(i for i, row in enumerate(context.records, 1) if row.event == "comparison_bound")
    prefix = tuple(context.records[:count])
    snapshot = context.snapshot_before(count)
    # The actual Core-authored journal has since appended all remaining work.
    assert len(context.records) > len(prefix)
    acquired, integrity = core_raw._family15_prefix(run, snapshot)
    assert acquired == prefix and integrity == "complete"
    bindings = core_raw.family15_bindings(run, snapshot, records=prefix)
    row, = bindings.values()
    assert row["gate_binding"]["source"] == "core_raw" and row["global_binding"] is None
    assert {s["name"]: s["rule"] for s in row["gate_binding"]["scopes"]}["causal"] == "bound"
    changed = prefix[0].to_payload()
    changed["timestamp"] = "2026-01-01T00:00:00Z"
    rewritten = Envelope.from_payload(changed, family=campaign_family_for(15))
    with pytest.raises(core_raw.CoreRawUnavailable, match="unreadable"):
        core_raw.family15_bindings(run, snapshot, records=(rewritten, *prefix[1:]))


@pytest.mark.parametrize("matching_index", [1, 3, None])
def test_bf2_prefix_search_uses_one_monotonic_core_replay(monkeypatch, tmp_path, matching_index):
    from types import SimpleNamespace
    from evolution.tree import campaign_projections
    calls = []
    records = tuple(range(5))
    wanted = object()

    def snapshot_before(index):
        calls.append(index)
        return wanted if index == matching_index else object()

    context = SimpleNamespace(records=records, snapshot_before=snapshot_before)
    monkeypatch.setattr(campaign_projections, "_evidence_of", lambda run:
                        (context, SimpleNamespace(journal_integrity="complete")))
    if matching_index is None:
        with pytest.raises(core_raw.CoreRawUnavailable, match="unreadable"):
            core_raw._family15_prefix(tmp_path, wanted)
    else:
        assert core_raw._family15_prefix(tmp_path, wanted) == (records[:matching_index], "complete")
    assert calls == list(range(1, (matching_index or len(records)) + 1))


def test_c58r1_unknown_required_binding_keeps_other_admitted_facts(offline_b10_campaigns, monkeypatch):
    from evolution.tree.family15_audit import Family15Audit
    from evolution_lab.owner.campaign_views import campaign_view
    from evolution_lab.owner.progress import CampaignPlan
    from evolution_lab.report import read_campaign_report
    from evolution_lab.cli.responses import validate
    run = offline_b10_campaigns["noise-fresh"]
    original = core_raw.family15_bindings

    def unknown(*args, **kwargs):
        # Only the supplementary binding reader sees a shape the pin does not
        # describe. Core's independently valid public comparison stays intact.
        with monkeypatch.context() as patch:
            patch.setattr(Family15Audit, "publication", lambda *a, **k: {
                "scopes": [{"name": "causal", "rule": "future_rule"}]})
            return original(*args, **kwargs)

    monkeypatch.setattr(core_raw, "family15_bindings", unknown)
    read = core_raw.campaign_read(run, with_result=True)
    core = read.status.to_payload()
    assert core["independent"]["evidence"] == "authenticated_publications"
    assert core["independent"]["attempts"] and read.bindings_reason == "unreadable"
    details = core_raw.campaign_details(run, read=read)
    view = campaign_view("o-b10", core, CampaignPlan("manifest.json", 1, 1, "score"),
                         None, None, None, [], details)
    assert view["independent"] is None and view["details_reason"] == "unreadable"
    assert view["rounds"][0]["reflection"] is not None
    assert view["rounds"][0]["baseline_version"] is not None
    validate(view, "lab-campaign-view/15")
    report = read_campaign_report(run, families=core_raw.FAMILIES, read=read)
    assert report.data["lab_independent"] is None and report.data["lab_independent_reason"] == "unreadable"
    assert report.data["lab_rounds_state"] == "present"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_c58r1_real_unavailable_publication_preserves_core_state(offline_b10_campaigns, tmp_path, damage):
    import shutil
    from evolution_lab.owner.campaign_views import campaign_view
    from evolution_lab.owner.progress import CampaignPlan
    from evolution_lab.report import read_campaign_report
    from evolution_lab.cli.responses import validate
    run = tmp_path / "damaged"
    shutil.copytree(offline_b10_campaigns["noise-fresh"], run)
    publication = next(path for path in (run / "artifacts/auxiliary").glob("*/payload.json")
                       if json.loads(path.read_bytes()).get("kind") == "binding")
    publication.parent.chmod(0o700)  # mutate only the disposable copy, not the producing fixture
    if damage == "missing":
        publication.unlink()
    else:
        publication.chmod(0o600)
        publication.write_bytes(b"not a publication\n")
    read = core_raw.campaign_read(run, with_result=True)
    core = read.status.to_payload()
    block = core["independent"]
    assert block["evidence"] == "unavailable" and block["attempts"] == []
    # Correlated error metadata was the old regression: it must be irrelevant
    # when Core has no public attempts requiring binding facts.
    from dataclasses import replace
    correlated = replace(read, bindings=None, bindings_reason="unreadable")
    details = core_raw.campaign_details(run, read=correlated)
    view = campaign_view("o-b10", core, CampaignPlan("manifest.json", 1, 1, "score"),
                         None, None, None, [], details)
    assert view["independent"]["evidence"] == "unavailable"
    assert view["independent"]["next_action"] == block["next_action"]
    assert view["rounds"][0]["reflection"] is not None
    validate(view, "lab-campaign-view/15")
    report = read_campaign_report(run, families=core_raw.FAMILIES, read=correlated)
    assert report.data["lab_independent_state"] == "present"
    assert report.data["lab_independent"] == view["independent"]
    assert report.data["lab_rounds_state"] == "present"


def test_no_option_campaign_reads_as_on_b9(offline_b10_campaigns):
    """Old-key projection remains intact, independent of offsets and editor digests."""
    from evolution_lab.owner.independent_view import independent_view
    read = core_raw.campaign_read(offline_b10_campaigns["no-option"], with_result=True)
    block = read.status.to_payload()["independent"]
    view = independent_view(block, bindings=read.bindings)
    assert view["settings"]["mode"] == "confidence"
    assert "bound_fallback" not in block["settings"]
    assert view["settings"]["bound_fallback"] is None
    for attempt, row in zip(block["attempts"], view["attempts"], strict=True):
        for name in ("gate_comparison", "global_comparison"):
            if attempt[name] is not None:
                assert not {"calibration", "bound_fallback", "caveats"} & set(attempt[name])
                assert row[name]["outcome"] == attempt[name]["outcome"]
                assert row[name]["reference_measurement_id"] == attempt[name]["reference_measurement_id"]
                for before, after in zip(attempt[name]["scopes"], row[name]["scopes"], strict=True):
                    assert after["rule"] == before["rule"]
                    assert (None if after["point"] is None else after["point"]["exact"]) == before["point"]


_SCORER_RID = "a" * 32
_SCORER_MID = "m-campaign"


def _scorer_read(root, *, mid=_SCORER_MID, case_id="c1"):
    return core_raw.scorer_trace(root, _SCORER_RID, case_id, measurement_id=mid)


def _scorer_request(root):
    _trace(root, _SCORER_RID)
    return root / "scorer-traces" / "scorer_traces" / _SCORER_RID


def _scorer_pack_request(tmp_path, *, mid=_SCORER_MID, stdout=b"output", returncode=None):
    from scorer_trace_pack import scorer_pack

    directory = _scorer_request(tmp_path / "source")
    request = (directory / "request.json").read_bytes()
    root = tmp_path / "pack"
    path = scorer_pack(root, mid, _SCORER_RID, request=request, stdout=stdout,
                       returncode=returncode, status="failed" if returncode else "completed")
    return root, path


def test_scorer_directory_removed_stream_is_available_empty(tmp_path):
    directory = _scorer_request(tmp_path)
    (directory / "stdout").unlink()
    result = _scorer_read(tmp_path)
    assert result["stdout"] == ""
    assert result["output_reason"] is None and result["returncode"] is None


@pytest.mark.parametrize("root_kind", ["campaign", "evaluation"])
def test_scorer_pack_addresses_resolved_measurement_and_returncode(tmp_path, root_kind):
    from scorer_trace_pack import scorer_pack

    source = _scorer_request(tmp_path / "source")
    root = tmp_path / "run"
    mid = "m-r01-a01" if root_kind == "campaign" else "c-operation"
    if root_kind == "evaluation":
        root = root / "measurement"
    scorer_pack(root, mid, _SCORER_RID, request=(source / "request.json").read_bytes(),
                stdout=b"failure\xff\r\n", status="failed", returncode=7)
    result = _scorer_read(root, mid=mid)
    assert result["stdout"] == "failure\ufffd\r\n"
    assert result["input"]["case_id"] == "c1"
    assert result["returncode"] == 7 and result["output_reason"] is None
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(root, mid="other")
    assert caught.value.code == "missing"


def test_scorer_pack_request_without_output_preserves_verified_input(tmp_path):
    root, _ = _scorer_pack_request(tmp_path, stdout=None)
    result = _scorer_read(root)
    assert result["input"]["oracle"] == {"a": "2"}
    assert result["stdout"] is None and result["output_reason"] == "trace_missing"
    assert result["returncode"] is None


@pytest.mark.parametrize("stdout", [None, b"orphan"])
def test_scorer_pack_missing_request_refuses_payload(tmp_path, stdout):
    from scorer_trace_pack import scorer_pack

    scorer_pack(tmp_path, _SCORER_MID, _SCORER_RID, stdout=stdout)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(tmp_path)
    assert caught.value.code == "missing"


@pytest.mark.parametrize("entry", ["request.json", "stdout"])
def test_scorer_directory_unsafe_section_is_not_read(tmp_path, entry):
    directory = _scorer_request(tmp_path)
    (directory / entry).unlink()
    (directory / entry).symlink_to(tmp_path / "private")
    (tmp_path / "private").write_bytes(b"secret")
    if entry == "request.json":
        with pytest.raises(core_raw.CoreRawUnavailable) as caught:
            _scorer_read(tmp_path)
        assert caught.value.code == "unreadable"
    else:
        result = _scorer_read(tmp_path)
        assert result["input"]["case_id"] == "c1"
        assert result["stdout"] is None and result["output_reason"] == "unreadable"


@pytest.mark.parametrize("keep_stdout", [False, True])
def test_scorer_directory_without_request_is_unreadable(tmp_path, keep_stdout):
    directory = _scorer_request(tmp_path)
    (directory / "request.json").unlink()
    if not keep_stdout:
        (directory / "stdout").unlink()
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(tmp_path)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("layout", ["directory", "pack"])
def test_scorer_corrupt_request_is_unreadable(tmp_path, layout):
    if layout == "directory":
        directory = _scorer_request(tmp_path)
        (directory / "request.json").write_bytes(b"not json")
        root = tmp_path
    else:
        root, path = _scorer_pack_request(tmp_path)
        # Corrupt section bytes without touching header or record lengths.
        data = path.read_bytes().replace(b'"scorer": "s"', b'"scorer": "z"')
        path.write_bytes(data)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(root)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("change", ["extra_request", "extra_input", "trace_id", "case_id", "view", "hash"])
def test_scorer_request_rejects_wrong_identity_or_shape(tmp_path, change):
    directory = _scorer_request(tmp_path)
    path = directory / "request.json"
    request = json.loads(path.read_bytes())
    if change == "extra_request":
        request["extra"] = True
    elif change == "extra_input":
        request["input"]["extra"] = True
    elif change == "trace_id":
        request["trace_id"] = "b" * 32
    elif change == "case_id":
        request["input"]["case_id"] = "other"
    elif change == "view":
        request["input"]["view"] = []
    else:
        request["input_sha256"] = "0" * 64
    path.write_text(json.dumps(request))
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(tmp_path)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("root_kind,code", [("missing", "missing"), ("symlink", "unreadable"),
                                           ("ancestor", "unreadable"), ("dotdot", "unreadable"),
                                           ("nul", "unreadable")])
def test_scorer_original_root_is_checked_before_public_reader(tmp_path, monkeypatch, root_kind, code):
    import evolution.tree

    real = tmp_path / "real"
    _scorer_request(real)
    root = tmp_path / "absent"
    if root_kind == "symlink":
        root.symlink_to(real, target_is_directory=True)
    elif root_kind == "ancestor":
        root.symlink_to(tmp_path, target_is_directory=True)
        root = root / "real"
    elif root_kind == "dotdot":
        root = real / ".." / "real"
    elif root_kind == "nul":
        root = str(real) + "\0"
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe or missing original root reached the public reader")
    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", forbidden)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(root)
    assert caught.value.code == code


def test_scorer_invalid_pack_id_keeps_directory_fallback(tmp_path):
    _scorer_request(tmp_path)
    assert _scorer_read(tmp_path, mid="not:a:pack")["stdout"] == '{"metrics": {"m": 1}}'


@pytest.mark.parametrize("entry", ["request.json", "stdout"])
def test_scorer_oversized_metadata_refuses_whole_trace_before_payload(tmp_path, monkeypatch, entry):
    import evolution.tree

    directory = _scorer_request(tmp_path)
    with (directory / entry).open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    reader_type = evolution.tree.ScorerTraceReader
    class MetadataOnly(reader_type):
        def read(self, *args, sections, **kwargs):
            assert sections == (), "oversized trace must not read any payload"
            return super().read(*args, sections=sections, **kwargs)
    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", MetadataOnly)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(tmp_path)
    assert caught.value.code == "too_large"


def test_scorer_exact_stdout_limit_is_available_and_stderr_unrequested(tmp_path):
    directory = _scorer_request(tmp_path)
    with (directory / "stdout").open("wb") as stream:
        stream.truncate(32 * 1024 * 1024)
    # Unrequested stderr size must not reject request/stdout.
    with (directory / "stderr").open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    result = _scorer_read(tmp_path)
    assert len(result["stdout"]) == 32 * 1024 * 1024
    assert result["output_reason"] is None


@pytest.mark.parametrize("change", ["grow_request", "grow_stdout", "lose_request", "lose_stdout", "typed"])
@pytest.mark.parametrize("layout", ["directory", "pack"])
def test_scorer_one_reader_closes_after_between_call_change(tmp_path, monkeypatch, change, layout):
    import evolution.tree
    from dataclasses import replace

    if layout == "pack":
        root, _ = _scorer_pack_request(tmp_path)
    else:
        root = tmp_path
        _scorer_request(root)
    real_type = evolution.tree.ScorerTraceReader
    instances = []
    monkeypatch.setattr(core_raw, "FILE_MAX", 1024)
    class ChangingReader(real_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = []
            self.closed = False
            instances.append(self)
        def read(self, *args, sections, **kwargs):
            self.calls.append((sections, kwargs))
            record = super().read(*args, sections=sections, **kwargs)
            if not sections:
                return record
            if change == "typed":
                raise evolution.tree.ScorerTraceUnreadable("corrupt")
            if change.startswith("grow_"):
                section = change.removeprefix("grow_")
                return replace(record, **{section: b"x" * 1025})
            section = change.removeprefix("lose_")
            return replace(record, sizes={k: v for k, v in record.sizes.items() if k != section},
                           **{section: None})
        def close(self):
            self.closed = True
            super().close()
    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", ChangingReader)
    if change.startswith("grow_") or change in ("lose_request", "typed"):
        with pytest.raises(core_raw.CoreRawUnavailable) as caught:
            _scorer_read(root)
        want = "too_large" if change.startswith("grow_") else "missing" if change == "lose_request" and layout == "pack" else "unreadable"
        assert caught.value.code == want
    else:
        result = _scorer_read(root)
        assert result["input"]["case_id"] == "c1"
        assert result["stdout"] is None
        assert result["output_reason"] == ("trace_missing" if layout == "pack" else "unreadable")
    assert len(instances) == 1 and instances[0].closed
    assert instances[0].calls == [((), {"measurement_id": _SCORER_MID}),
                                  (("request", "stdout"), {"measurement_id": _SCORER_MID, "max_section_bytes": 1024})]


def test_scorer_unsupported_pin_precedes_path_read(monkeypatch, tmp_path):
    monkeypatch.setattr(core_raw, "_installed_core", lambda: "2.0.0b4")
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(tmp_path / "missing")
    assert caught.value.code == "core_raw_unsupported"


def test_scorer_pack_empty_stdout_is_available(tmp_path):
    root, _ = _scorer_pack_request(tmp_path, stdout=b"")
    result = _scorer_read(root)
    assert result["stdout"] == "" and result["output_reason"] is None
    assert result["returncode"] is None


def test_scorer_unsafe_pack_is_unreadable(tmp_path):
    root, path = _scorer_pack_request(tmp_path)
    outside = tmp_path / "outside.pack"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(root)
    assert caught.value.code == "unreadable"


def test_scorer_corrupt_pack_header_is_unreadable(tmp_path):
    root, path = _scorer_pack_request(tmp_path)
    path.write_bytes(b"not a header\n")
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(root)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("code,want", [("too_large", "too_large"), ("unsafe_entry", "unreadable"),
                                      ("corrupt", "unreadable"), ("ambiguous", "unreadable"),
                                      ("invalid_id", "unreadable")])
def test_scorer_constructor_typed_refusal_keeps_mapping(tmp_path, monkeypatch, code, want):
    import evolution.tree

    def refused(*args, **kwargs):
        raise evolution.tree.ScorerTraceUnreadable(code)

    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", refused)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(tmp_path)
    assert caught.value.code == want


def test_scorer_root_swap_after_precheck_is_refused(tmp_path, monkeypatch):
    root, outside = tmp_path / "run", tmp_path / "outside"
    _scorer_request(root)
    _scorer_request(outside)
    real_precheck = core_raw._scorer_root_precheck

    def swapped(path):
        real_precheck(path)
        root.rename(tmp_path / "retained")
        root.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(core_raw, "_scorer_root_precheck", swapped)
    with pytest.raises(core_raw.CoreRawUnavailable) as caught:
        _scorer_read(root)
    assert caught.value.code == "unreadable"


def test_scorer_root_swap_after_constructor_keeps_admitted_run(tmp_path, monkeypatch):
    import evolution.tree

    root, outside = tmp_path / "run", tmp_path / "outside"
    _scorer_request(root)
    _trace(outside, _SCORER_RID, case_id="outside")
    reader_type = evolution.tree.ScorerTraceReader

    class SwappedReader(reader_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            root.rename(tmp_path / "retained")
            root.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", SwappedReader)
    assert _scorer_read(root)["input"]["case_id"] == "c1"


@pytest.mark.parametrize("entry", ["request.json", "stdout"])
@pytest.mark.parametrize("disclose", [False, True])
def test_scorer_growth_after_metadata_refuses_all_sample_content(tmp_path, monkeypatch, entry, disclose):
    import errno
    import os
    import evolution.tree
    from evolution_lab.owner.sample_view import sample_view
    from test_owner_content import _sample_trace

    row, journal = _sample_trace(tmp_path, layout="directory")
    directory = tmp_path / "scorer-traces" / "scorer_traces" / _SCORER_RID
    reader_type = evolution.tree.ScorerTraceReader
    instances, descriptors = [], []
    monkeypatch.setattr(core_raw, "FILE_MAX", 1024)

    class GrowingReader(reader_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)
            if self._root_fd is not None:
                descriptors.append(self._root_fd)

        def read(self, *args, sections, **kwargs):
            if sections:
                assert kwargs.get("max_section_bytes") == 1024
            record = super().read(*args, sections=sections, **kwargs)
            if not sections:
                with (directory / entry).open("r+b") as stream:
                    stream.truncate(1025)
            return record

    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", GrowingReader)
    body = sample_view("op", "m-r01-a01", row, tmp_path, journal, disclose)
    assert body["trace"] == {"state": "unavailable", "reason": "too_large"}
    assert body["input"] is None and body["turns"] == [] and body["terminal"] is None
    if disclose:
        assert body["private"]["oracle"] is None
    assert body["scoring"]["metrics"] == {"score": 1.0}
    assert len(instances) == len(descriptors) == 1
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("phase", ["metadata", "payload", "success"])
def test_scorer_strict_descriptor_closes_after_request(tmp_path, monkeypatch, phase):
    import errno
    import os
    import evolution.tree

    _scorer_request(tmp_path)
    reader_type = evolution.tree.ScorerTraceReader
    descriptors = []

    class ObservedReader(reader_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            assert kwargs == {"run_boundary": "nofollow"}
            descriptors.append(self._root_fd)

        def read(self, *args, sections, **kwargs):
            if (phase == "metadata" and not sections) or (phase == "payload" and sections):
                raise evolution.tree.ScorerTraceUnreadable("corrupt")
            return super().read(*args, sections=sections, **kwargs)

    monkeypatch.setattr(evolution.tree, "ScorerTraceReader", ObservedReader)
    if phase == "success":
        assert _scorer_read(tmp_path)["input"]["case_id"] == "c1"
    else:
        with pytest.raises(core_raw.CoreRawUnavailable) as caught:
            _scorer_read(tmp_path)
        assert caught.value.code == "unreadable"
    assert len(descriptors) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(descriptors[0])
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize('failure', ['corrupt_suffix', 'prefix_mismatch', 'stage_reader'])
def test_stage_facts_unreadable_is_a_state(tmp_path, monkeypatch, failure):
    from pathlib import Path
    import shutil

    source = Path(__file__).parent / 'fixtures/panel/b15/focus-first/run'
    run = tmp_path / 'run'
    shutil.copytree(source, run)
    original = core_raw.campaign_read(run, with_bindings=True)
    if failure == 'corrupt_suffix':
        with (run / 'campaign.jsonl').open('ab') as stream:
            stream.write(b'{"invalid":true}\n')
    elif failure == 'prefix_mismatch':
        monkeypatch.setattr(core_raw, '_family15_prefix',
                            lambda *a: (original.records[:-1], original.journal_integrity))
        # Isolate the supplementary stage failure from the successful binding read.
        monkeypatch.setattr(core_raw, 'family15_bindings', lambda *a, **k: original.bindings)
    else:
        def unreadable(*a, **k):
            raise core_raw.CoreRawUnavailable('unreadable')
        monkeypatch.setattr(core_raw, 'family15_stage_facts', unreadable)
    read = core_raw.campaign_read(run, with_bindings=True)
    assert read.stages is None and read.stages_reason == 'unreadable'
    if failure != 'corrupt_suffix':
        assert read.bindings == original.bindings and read.bindings_reason is None


def test_stage_facts_use_only_the_admitted_prefix():
    from dataclasses import replace
    from pathlib import Path
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.storage.envelope import Envelope

    run = Path(__file__).parent / 'fixtures/panel/b15/focus-first/run'
    context, _ = _evidence_of(run)
    count = next(i for i, row in enumerate(context.records, 1) if row.event == 'stage_opened'
                 and row.payload.get('scope') == 'focus')
    prefix = tuple(context.records[:count])
    snapshot = context.snapshot_before(count)
    assert core_raw.family15_stage_facts(run, snapshot, records=prefix) == {
        'r01-a01': {'focus_opened': True, 'causal_opened': False, 'focus_decided': None},
    }
    changed = prefix[0].to_payload()
    changed['timestamp'] = '2026-01-01T00:00:00Z'
    rewritten = Envelope.from_payload(changed, family=campaign_family_for(15))
    for records, seen in [((rewritten, *prefix[1:]), snapshot),
                          (prefix, replace(snapshot, producing_version="different"))]:
        with pytest.raises(core_raw.CoreRawUnavailable, match='unreadable'):
            core_raw.family15_stage_facts(run, seen, records=records)


def test_stage_facts_incomplete_tail_preserves_the_admitted_prefix(tmp_path):
    from pathlib import Path
    import shutil
    source = Path(__file__).parent / 'fixtures/panel/b15/causal-open/run'
    run = tmp_path / 'run'
    shutil.copytree(source, run)
    original = core_raw.campaign_read(run, with_bindings=True)
    with (run / 'campaign.jsonl').open('ab') as stream:
        stream.write(b'{"event":')
    read = core_raw.campaign_read(run, with_bindings=True)
    assert read.journal_integrity == 'incomplete_tail'
    assert read.stages == original.stages == {
        'r01-a01': {'focus_opened': True, 'causal_opened': True, 'focus_decided': 'passed'},
    }
    assert read.stages_reason is None


@pytest.mark.parametrize('outcome', ['comparison_unavailable', 'future_outcome'])
def test_stage_facts_accept_only_cores_closed_focus_outcomes(monkeypatch, outcome):
    from pathlib import Path
    from types import SimpleNamespace
    from evolution.tree import campaign_projections

    # Unit fault injection at Core's evidence boundary, never a contract journal.
    run = Path(__file__).parent / 'fixtures/panel/b15/focus-decided-open/run'
    read = core_raw.campaign_read(run, with_bindings=True)
    changed = read.records[-1].to_payload()
    assert changed['event'] == 'focus_decided'
    changed['payload']['outcome'] = outcome
    changed_record = SimpleNamespace(event=changed["event"], payload=changed["payload"])
    records = (*read.records[:-1], changed_record)
    context = SimpleNamespace(records=records, snapshot_before=lambda count: read.view.snapshot)
    monkeypatch.setattr(campaign_projections, '_evidence_of',
                        lambda path: (context, SimpleNamespace(journal_integrity='complete')))
    if outcome == 'future_outcome':
        with pytest.raises(core_raw.CoreRawUnavailable, match='unreadable'):
            core_raw.family15_stage_facts(run, read.view.snapshot, records=records)
    else:
        assert core_raw.family15_stage_facts(run, read.view.snapshot, records=records) == {
            'r01-a01': {'focus_opened': True, 'causal_opened': False,
                        'focus_decided': 'comparison_unavailable'},
        }
