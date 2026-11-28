"""Reports preserve core verdicts, coverage gaps and unknown cost."""
import json

import pytest

from evolution.tree.campaign_projections import CampaignProjection

from test_evaluation import bindings


async def test_standalone_report_has_scores_without_inventing_promotion(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    from evolution_lab.report import read_evaluation_report
    kwargs = bindings(tmp_path, repeats=2)
    await evaluate_fixed(**kwargs)
    report = read_evaluation_report(kwargs["run_directory"])
    assert report.data["measurements"][0]["coverage"] == {"expected": 2, "scored": 2, "state": "complete"}
    assert report.data["cost"] is None
    markdown = report.markdown()
    assert "0.75" in markdown and "неизвестен" in markdown
    assert "Promotion" not in markdown and "Изменения кандидата" not in markdown
    assert "ORACLE-CANARY" not in markdown and "ORACLE-CANARY" not in json.dumps(report.data)


async def test_failed_measurement_is_not_rendered_as_completed_or_zero_quality(tmp_path):
    from evolution_lab.evaluation import evaluate_fixed
    from evolution_lab.report import read_evaluation_report
    kwargs = bindings(tmp_path, fail=True, repeats=2)
    await evaluate_fixed(**kwargs)
    report = read_evaluation_report(kwargs["run_directory"])
    measurement = report.data["measurements"][0]
    assert measurement["coverage"] == {"expected": 2, "scored": 0, "state": "incomplete"}
    assert measurement["metrics"] == {}
    assert "PRIVATE-ERROR" not in report.markdown()


def campaign():
    """An admitted family14 result with one unmeasured Editor no-change attempt."""
    from pathlib import Path
    payload = json.loads((Path(__file__).parent / "fixtures/family14/observe-result.json").read_text())
    payload.update(run_id="campaign", campaign_outcome={"outcome": "stopped", "reason": "budget",
                                                       "continuation": None, "champion_path": None},
                   measurements=[], caveats=[{"kind": "champion_not_contemporaneous", "round": None, "attempt_id": None}],
                   selection=[])
    first = payload["rounds"][0]
    payload["rounds"] = [first]
    first.update(final=[], outcome="stopped", baseline_measurement_id=None)
    first["attempts"][0].update(outcome="abandoned", reason="editor_no_change", confidence=None,
                                affected=None, look=None, measurement_id=None, artifact_locator=None)
    return CampaignProjection.from_payload(payload)


def test_markdown_lists_rounds_without_attempts_and_the_notice():
    from evolution_lab.report import Report, build_report
    data = build_report(campaign(), comparison_scope="in-sample").data
    data["core"]["rounds"] = [{**data["core"]["rounds"][0], "round": 1, "outcome": "unplanned",
                               "reason": "reflection_failed", "attempts": []}]
    data["lab_rounds"] = [{"round": 1, "source": "core_raw", "reflection": {
        "state": "failed", "error_class": "oracle_leak", "reason": "oracle_leak"}}]
    data["lab_notices"] = [{"code": "no_candidate_measured", "rounds": 1, "by_reason": {"reflection_failed": 1},
                            "reflection_errors": {"oracle_leak": 1}}]
    text = Report(json.dumps(data, ensure_ascii=False, sort_keys=True)).markdown()
    assert "Раунды без попыток" in text
    assert "| 1 | unplanned | reflection_failed | oracle_leak |" in text
    assert "Ни один кандидат не замерен" in text


_TEXT_GROUP = {"root_cause": "PRIVATE-ROOT-CAUSE", "suggested_fix": "PRIVATE-FIX", "evidence_ids": [],
               "affected_cases": ["case-7"], "target_paths": ["prompts/main.md"], "probe_refs": [],
               "counterevidence": "PRIVATE-COUNTER", "regression_risks": "PRIVATE-RISK",
               "why_this_layer": "PRIVATE-WHY", "document_ids": []}


def test_lab_rounds_carry_codes_only(tmp_path, monkeypatch):
    """Final review I1 (spec dev46 §4.2, ADR-0019): `lab_rounds[].reflection` is `{state, error_class, reason}` --
    no hypotheses, no reflection id, none of the Reflector's texts."""
    from evolution_lab.owner import core_raw
    from evolution_lab.report import _lab_fields, build_report
    core = build_report(campaign(), comparison_scope="in-sample").data["core"]
    core["rounds"] = [{**core["rounds"][0], "round": 1}, {**core["rounds"][0], "round": 2, "attempts": []}]
    entry = lambda status, groups=(), error_class=None, reason=None: {  # noqa: E731
        "reflection_id": "x", "status": status, "groups": list(groups), "error_class": error_class, "reason": reason}
    details = {"journal": "present", "rounds": {
        1: {"baseline_version": "v0", "diagnosis": entry("reflected", [_TEXT_GROUP]), "last_reflection": None,
            "attempts": {}},
        2: {"baseline_version": "v0", "diagnosis": entry("failed", (), "oracle_leak", "oracle_leak"),
            "last_reflection": None, "attempts": {}}}}
    monkeypatch.setattr(core_raw, "campaign_details", lambda root: details)
    fields = _lab_fields(tmp_path, core)
    assert fields["lab_rounds"] == [
        {"round": 1, "source": "core_raw", "reflection": {"state": "present", "error_class": None, "reason": None}},
        {"round": 2, "source": "core_raw",
         "reflection": {"state": "failed", "error_class": "oracle_leak", "reason": "oracle_leak"}}]
    assert "PRIVATE" not in json.dumps(fields)


def test_lab_rounds_name_their_read_state(tmp_path, monkeypatch):
    """ADR-0019 p.3 (codex r1 #2): a failed read of the round details is an explicit state next to the empty
    `lab_rounds` -- `lab_rounds_state` and `lab_rounds_reason` (the adapter's code; `unreadable` for any other
    failure); a read that worked is `present` with no reason."""
    from evolution_lab.owner import core_raw
    from evolution_lab.report import _lab_fields, build_report
    core = build_report(campaign(), comparison_scope="in-sample").data["core"]
    monkeypatch.setattr(core_raw, "campaign_details", lambda root: {"journal": "present", "rounds": {}})
    fields = _lab_fields(tmp_path, core)
    assert (fields["lab_rounds_state"], fields["lab_rounds_reason"]) == ("present", None)
    assert fields["lab_rounds"] is not None

    def unreadable(root):
        raise core_raw.CoreRawUnavailable("unreadable")
    monkeypatch.setattr(core_raw, "campaign_details", unreadable)
    fields = _lab_fields(tmp_path, core)
    assert (fields["lab_rounds"], fields["lab_rounds_state"], fields["lab_rounds_reason"]) == (
        None, "unavailable", "unreadable")

    def other_code(root):
        raise core_raw.CoreRawUnavailable("journal_torn")
    monkeypatch.setattr(core_raw, "campaign_details", other_code)
    assert _lab_fields(tmp_path, core)["lab_rounds_reason"] == "journal_torn"

    def broken(root):
        raise KeyError("rounds")
    monkeypatch.setattr(core_raw, "campaign_details", broken)
    fields = _lab_fields(tmp_path, core)
    assert (fields["lab_rounds_state"], fields["lab_rounds_reason"]) == ("unavailable", "unreadable")


def test_markdown_says_reflection_details_are_unavailable():
    """ADR-0019 p.3 (codex r1 #2): `report.md` names the failed read instead of leaving a bare dash."""
    from evolution_lab.report import Report, build_report
    data = build_report(campaign(), comparison_scope="in-sample").data
    data["core"]["rounds"] = [{**data["core"]["rounds"][0], "round": 1, "outcome": "unplanned",
                               "reason": "reflection_failed", "attempts": []}]
    data.update(lab_rounds=None, lab_rounds_state="unavailable", lab_rounds_reason="unreadable",
                lab_notices=[{"code": "no_candidate_measured", "rounds": 1, "by_reason": {"reflection_failed": 1}}])
    text = Report(json.dumps(data, ensure_ascii=False, sort_keys=True)).markdown()
    assert text.count("Подробности рефлексии недоступны (unreadable)") == 1
    table = text.index("Раунды без попыток")
    assert text.index("Подробности рефлексии недоступны (unreadable)") > table
    data["core"]["rounds"] = [{**data["core"]["rounds"][0], "attempts": [
        {"attempt_id": "r1-a1", "index": 1, "outcome": "abandoned", "reason": "editor_no_change", "look": None,
         "confidence": None, "measurement_id": None, "artifact_locator": None}]}]
    text = Report(json.dumps(data, ensure_ascii=False, sort_keys=True)).markdown()
    assert "Раунды без попыток" not in text
    assert text.index("Подробности рефлексии недоступны (unreadable)") > text.index("Ни один кандидат не замерен")
    data.update(lab_rounds=[], lab_rounds_state="present", lab_rounds_reason=None)
    assert "Подробности рефлексии недоступны" not in Report(json.dumps(data, ensure_ascii=False)).markdown()


def test_markdown_notice_with_no_rounds_prints_a_dash():
    from evolution_lab.report import _notice_md
    text = _notice_md({"code": "no_candidate_measured", "rounds": 0, "by_reason": {}, "reflection_errors": {}})
    assert text.endswith("итог остаётся словом ядра): —.") and ": ." not in text


@pytest.mark.parametrize(("rounds", "reasons", "explained"), [
    (5, {"clean": 5}, True),
    (4, {"clean": 3, "editor_failed": 1}, False),
    (0, {}, False),
    (0, {"clean": 0}, False),
])
def test_clean_round_notice_explanation_preserves_json(tmp_path, rounds, reasons, explained):
    """Missing or overbroad clean-focus text must fail without changing either wire payload."""
    from pathlib import Path
    from evolution_lab.cli.output import _report_lines, campaign_keyed_lines
    from evolution_lab.report import Report, build_report

    notice = {"code": "no_candidate_measured", "rounds": rounds, "by_reason": reasons,
              "reflection_errors": {}}
    data = build_report(campaign(), comparison_scope="in-sample").data
    data["lab_notices"] = [notice]
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
    report = Report(encoded)
    markdown = report.markdown()
    cli_report = "\n".join(_report_lines({"schema": "lab-owner-report/15", "report": data}))
    routes = json.loads((Path(__file__).parent / "fixtures/panel/family14/routes.json").read_text())
    captured_view = next(row["body"] for path, row in routes.items() if path.endswith("/campaign"))
    assert captured_view["schema"] == "lab-campaign-view/10"
    view = {**captured_view, "schema": "lab-campaign-view/15"}
    view["lab_notices"] = [notice]
    view_bytes = json.dumps(view, ensure_ascii=False, sort_keys=True)
    cli_campaign = "\n".join(line for _, line in campaign_keyed_lines(view))
    for text in (cli_report, cli_campaign):
        assert ("the baseline had no failures" in text) is explained
        assert ("the scorer supplied neither verdicts nor critical_pass" in text) is explained
        assert ("Metrics do not build campaign focus" in text) is explained
    assert ("baseline без провалов" in markdown) is explained
    assert ("скорер не отдаёт ни verdicts, ни critical_pass" in markdown) is explained
    assert ("Метрики не строят фокус кампании" in markdown) is explained
    report.write(tmp_path / "report")
    assert (tmp_path / "report/report-data.json").read_text() == encoded + "\n"
    assert report.data == data
    assert json.dumps(view, ensure_ascii=False, sort_keys=True) == view_bytes


def test_markdown_without_lab_fields_is_unchanged():
    from evolution_lab.report import build_report
    text = build_report(campaign(), comparison_scope="in-sample").markdown()
    assert "Ни один кандидат не замерен" not in text
    assert "Раунды без попыток" not in text


def test_campaign_report_keeps_all_attempts_and_core_stop_verdict():
    from evolution_lab.report import build_report
    projection = campaign()
    report = build_report(projection, comparison_scope="in-sample")
    assert report.data["core"]["campaign_outcome"] == projection.to_payload()["campaign_outcome"]
    assert "stopped" in report.markdown() and "editor_no_change" in report.markdown()
    assert "in-sample" in report.markdown()


def test_unknown_requests_are_reported_as_reserved_upper_bound():
    from evolution_lab.report import CostSummary, build_report
    cost = CostSummary.from_budget({"limit_units": 1000000000, "reserved_upper_units": 500000000,
                                   "requests": 2, "unknown_requests": 1, "refusals": 1}, currency="USD")
    report = build_report(campaign(), cost=cost)
    assert report.data["cost"]["unknown_requests"] == 1
    assert "0.5 USD" in report.markdown() and "верхняя оценка" in report.markdown()


def test_report_output_does_not_overwrite_existing_files(tmp_path):
    import pytest
    from evolution_lab.report import build_report
    report = build_report(campaign())
    report.write(tmp_path / "report")
    with pytest.raises(FileExistsError): report.write(tmp_path / "report")


def measurement_campaign(*, reason="budget", rebases=(), excluded=None):
    """Family14 coverage and authorization facts independent of its final measurement."""
    payload = campaign().to_payload()
    payload.update(linked_from=None, rebases=[dict(row) for row in rebases],
                   identity_digest="b" * 64 if rebases else None)
    payload["campaign_outcome"] = {"outcome": "stopped", "reason": reason,
                                   "continuation": {"kind": "linked_run", "reason": reason},
                                   "champion_path": "artifacts/champions/v0"}
    payload["indeterminate"]["lineage"] = None
    for row in payload["rounds"]:
        for attempt in row["attempts"]:
            attempt["looks"] = []
    payload["measurements"] = [{"call_retries": 0, "call_failures_by_code": {}, "condition_epoch": 0,
                                "repair_tries": 0, "repairs_scored": 0, "measurement_id": "baseline-1", "candidate_digest": "seed",
                                "locator": "measurements/baseline-1/events.jsonl",
                                "tries": 3, "imported": 0, "excluded_pairs": excluded}]
    return CampaignProjection.from_payload(payload)


def journal(run_id, candidate, samples):
    """A hand-built core a16 measurement result (journal_projections.py:17-44, :77-200)."""
    from evolution.tree.execution.journal_projections import Projection
    return Projection.from_payload({
        "schema_version": 1, "kind": "result", "run_id": run_id,
        "run_identity_digest": "a" * 64, "candidate_digest": candidate,
        "last_sequence": 9, "prefix_digest": "c" * 64, "valid_bytes": 512,
        "consistency": "durable", "complete": True, "journal_integrity": "complete",
        "diagnostics": [], "lower_bound": False,
        "references": [{"version": 1, "kind": "journal_record", "run_id": run_id,
                        "locator": "events.jsonl#bytes=0:512", "content_digest": "c" * 64}],
        "samples": samples, "claim_scope": "rollout_measurement",
        "campaign_outcome": None, "selection": None, "initial_champion": None, "final_champion": None,
    })


def sample_row(case_id, outcome, *, tries, metrics=None, candidate="seed"):
    from dataclasses import asdict
    from evolution.tree.execution.sample_outcomes import disposition_for
    execution = asdict(disposition_for(outcome))
    scored = execution["kind"] == "score"
    return {"case_id": case_id, "sample_id": "s1", "candidate_digest": candidate,
            "execution_state": "finished", "execution": execution,
            "scoring_state": "finished" if scored else "not_started",
            "evaluation": execution, "metrics": metrics if scored else None,
            "critical_pass": None, "tries": tries}


def test_campaign_coverage_excludes_exhausted_baseline_pairs_and_shows_tries():
    from evolution_lab.report import build_report
    excluded = {"count": 1, "by_owner": {"target": 1}, "truncated": False,
                "pairs": [{"case_id": "c2", "sample_id": "s1", "owner": "target",
                           "reason": "target_unstable", "tries": 2}]}
    measurement = journal("baseline-1", "seed", [
        sample_row("c1", "completed", tries=1, metrics={"score": 1.0, "verified": 0.5}),
        sample_row("c2", "target_failed", tries=2)])
    report = build_report(measurement_campaign(excluded=excluded), measurements={"baseline-1": measurement},
                          expected_samples={"baseline-1": 2})
    row = report.data["measurements"][0]
    assert row["coverage"] == {"expected": 1, "scored": 1, "state": "complete"}
    assert row["metrics"] == {"score": {"mean": 1.0, "count": 1}, "verified": {"mean": 0.5, "count": 1}}
    assert row["tries"] == 3 and row["excluded_pairs"] == excluded
    markdown = report.markdown()
    assert "Попыток исполнения: 3." in markdown and "Исключено базовых пар: 1" in markdown
    assert "target_unstable" in markdown
    assert "| score | 1 | 1 |" in markdown and "| verified | 0.5 | 1 |" in markdown


def test_report_shows_operator_authorization_trace():
    from evolution_lab.report import build_report
    rebase = {"previous_digest": "a" * 64, "identity_digest": "b" * 64,
              "producing_version": "0.3.0a16", "changed_fields": ["target.settings_hash"],
              "reason": "operator accepted"}
    rebased = build_report(measurement_campaign(rebases=[rebase])).markdown()
    assert "lab resume" in rebased and "target.settings_hash" in rebased and "b" * 64 in rebased
    closed = build_report(measurement_campaign(reason="condition_changed"))
    assert closed.data["core"]["campaign_outcome"]["champion_path"] == "artifacts/champions/v0"
    assert "condition_changed" in closed.markdown() and "авторизацией оператора" in closed.markdown()
    assert "evo tree authorize" not in build_report(measurement_campaign()).markdown()


SEED = "fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4"


def suspended_campaign(reason, owner, evidence_kind):
    """A suspended family14 result for report continuation and coverage rendering."""
    payload = {
        "schema_version": 1, "kind": "campaign_result", "run_id": "c-run",
        "consistency": "durable", "complete": False, "claim_scope": "campaign",
        "identity_digest": "063d55643fd0f6253cff5c27fc3428b8708e7b5bdcac26bb92d36359964b0957",
        "rebases": [], "linked_from": None, "caveats": [], "campaign_outcome": None,
        "selection": [], "alpha": {"per_round": {}, "per_attempt": {}},
        "initial_champion": {"version": "v0", "digest": SEED, "measurement_id": "m-r00-champion"},
        "final_champion": {"version": "v0", "digest": SEED, "measurement_id": "m-r00-champion",
                           "baseline_origin": "seed", "measurement_complete": True},
        "rounds": [{"round": 1, "baseline_measurement_id": "m-r00-champion", "baseline_complete": True,
                    "outcome": "unmeasurable", "champion_complete": False, "attempts": []}],
        "indeterminate": {
            "counts": {"environment": 0, "rollout": 0, "scorer": 0, "target": 12}, "denominator": 12,
            "lineage": None, "threshold_reached": None,
            "window": {"counts": {"environment": 0, "rollout": 0, "scorer": 0, "target": 0},
                       "denominator": 0, "start": 18}},
        "measurements": [{
            "measurement_id": "m-r00-champion", "candidate_digest": SEED,
            "locator": "measurements/m-r00-champion/events.jsonl", "tries": 12, "imported": 0,
            "excluded_pairs": {"count": 1, "by_owner": {"target": 1}, "truncated": False,
                               "pairs": [{"case_id": "q1", "sample_id": "01", "owner": "target",
                                          "reason": "target_unstable", "tries": 2}]},
            "call_retries": 0, "call_failures_by_code": {"internal": 12}, "condition_epoch": 0,
            # Core a24 (#78): every measurement of a family >= 7 projection carries its repair tries
            # (campaign_projections.py `_SUSPENDABLE_MEASUREMENT`); none yet at the suspension itself.
            "repair_tries": 0, "repairs_scored": 0}],
        "suspended": True, "condition_changes": [],
        "suspensions": [{"seq": 18, "reason": reason, "owner": owner, "evidence_kind": evidence_kind}],
    }
    reference = campaign().to_payload()
    for name in ("roles", "settings", "connection", "resilience"):
        payload[name] = reference[name]
    payload["alpha"] = None
    payload["caveats"] = reference["caveats"]
    payload["rounds"][0].update(gate_scope=None, reason=None, visitation=None, final=[])
    return CampaignProjection.from_payload(payload)


def suspended_report(projection, legacy_run):
    """`read_campaign_report` adds `legacy_run` after `build_report`; so does this."""
    from evolution_lab.report import Report, build_report
    measurement = journal("m-r00-champion", SEED, [sample_row("q1", "target_failed", tries=2, candidate=SEED)])
    data = build_report(projection, measurements={"m-r00-champion": measurement}).data
    if legacy_run is not None:
        data["legacy_run"] = legacy_run
    return Report(json.dumps(data, ensure_ascii=False, sort_keys=True))


LEGACY = "Прогон legacy runner продолжается `resume_native` в той же папке."
NOT_LEGACY = "Продолжение средствами Lab в этой версии недоступно; папка прогона сохранена."


@pytest.mark.parametrize("reason, owner, evidence_kind", [
    ("target_unstable", "target", "threshold"),
    ("target_contract_violated", "target", "fault_rows"),
    ("probe_unavailable", "campaign", "stage_record"),
])
@pytest.mark.parametrize("legacy_run, continuation", [(True, LEGACY), (False, NOT_LEGACY), (None, NOT_LEGACY)],
                         ids=["legacy", "not-legacy", "absent"])
def test_family14_suspension_is_rendered_per_evidence_kind(reason, owner, evidence_kind, legacy_run, continuation):
    lines = suspended_report(suspended_campaign(reason, owner, evidence_kind), legacy_run).markdown().splitlines()
    assert f"Кампания приостановлена: {reason} (владелец {owner}, evidence {evidence_kind})." in lines
    assert continuation in lines
    assert (LEGACY if continuation == NOT_LEGACY else NOT_LEGACY) not in lines
    assert "| Запись | Причина | Владелец | Evidence |" in lines
    assert f"| 18 | {reason} | {owner} | {evidence_kind} |" in lines
    [window] = [line for line in lines if line.startswith("Окно неопределённости: ")]
    assert '"start": 18' in window and '"denominator": 0' in window
    assert "Сбои вызовов цели по кодам: internal — 12." in lines
    assert "Повторы вызовов: 0." in lines


def test_campaign_without_suspensions_has_no_continuation_lines():
    measurement = journal("baseline-1", "seed", [sample_row("c1", "completed", tries=1, metrics={"score": 1.0})])
    from evolution_lab.report import build_report
    markdown = build_report(measurement_campaign(), measurements={"baseline-1": measurement}).markdown()
    for text in ("Кампания приостановлена", "| Запись | Причина | Владелец | Evidence |",
                 "Окно неопределённости", LEGACY, NOT_LEGACY):
        assert text not in markdown


from test_runner_suspension import unstable_sample  # noqa: E402  (pt_inst only: real suspended run)


@pytest.fixture
def suspended_run(unstable_sample, tmp_path):
    """A suspended family14 run directory without `supervisor.lock`: built as in
    tests/test_runner_suspension.py (`run_native` on `unstable_sample`), but the
    lock is removed rather than kept, so `read_campaign_report` sees an
    Owner-shaped run (no legacy H1 marker) unless told `owner_run=True`.
    """
    import asyncio
    from evolution_lab.runner import run_native
    inputs, _faults = unstable_sample
    run = tmp_path / "run"
    first = asyncio.run(asyncio.wait_for(run_native(inputs, run, dispatch_guard=lambda: None), 120))
    assert first.exit_code == 1, first.stderr.read_text()
    logs = run.with_name(run.name + "-lab-logs")
    (logs / "supervisor.lock").unlink()
    return run


def test_owner_run_names_lab_resume_and_h1_report_is_unchanged(suspended_run):
    from evolution_lab.report import read_campaign_report
    plain = read_campaign_report(suspended_run)
    assert "owner_run" not in plain.data
    assert "Продолжение средствами Lab в этой версии недоступно" in plain.markdown()
    owner = read_campaign_report(suspended_run, owner_run=True)
    assert owner.data["owner_run"] is True
    text = owner.markdown()
    assert "`lab resume`" in text and "`lab cleanup`" in text
    assert "после ручного `lab release-operation` продолжение недоступно" in text
    assert "Продолжение средствами Lab в этой версии недоступно" not in text


def test_owner_report_names_a_fresh_target_after_target_unstable():
    report = suspended_report(suspended_campaign("target_unstable", "target", "threshold"), None)
    data = report.data
    data["owner_run"] = True
    data["owner_resume_target"] = {
        "requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
        "excluded": [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g1"}]}
    from evolution_lab.report import Report
    lines = Report(json.dumps(data)).markdown().splitlines()
    assert ("Операция Owner продолжается `lab resume` (Python: `Owner.resume`) на свежей цели того же "
            "Bundle — не на цели, которую кампания уже использовала (`t1`): последняя приостановка — "
            "`target_unstable`; если операция требует очистки — сначала `lab cleanup`; после ручного "
            "`lab release-operation` продолжение недоступно.") in lines
    data["owner_resume_target"]["ready_target"] = {"id": "t9", "owner_generation": "g1"}
    ready = Report(json.dumps(data)).markdown()
    assert "на готовой цели `t9` того же Bundle, которую кампания не использовала" in ready
    assert "свежей" not in ready
    del data["owner_resume_target"]
    plain = Report(json.dumps(data)).markdown()
    assert "на готовой цели того же Bundle" in plain and "свежей" not in plain


def test_owner_report_gives_the_role_hint_and_no_fresh_target_for_a_role_suspension():
    from evolution_lab._suspension import ROLE_SUSPENSIONS
    from evolution_lab.report import Report
    report = suspended_report(suspended_campaign("reflector_provider_failed", "reflector", "stage_record"), None)
    data = report.data
    data["owner_run"] = True
    lines = Report(json.dumps(data)).markdown().splitlines()
    heading = "Кампания приостановлена: reflector_provider_failed (владелец reflector, evidence stage_record)."
    assert heading in lines
    assert lines[lines.index(heading) + 1] == ROLE_SUSPENSIONS["reflector_provider_failed"][1]
    [continuation] = [line for line in lines if line.startswith("Операция Owner продолжается")]
    assert "свежей" not in continuation and "target_unstable" not in continuation
    assert "Операция Owner продолжается `lab resume`" in continuation


def test_owner_report_gives_the_scorer_hint_and_no_fresh_target_for_a_scorer_suspension():
    from evolution_lab._suspension import SCORER_SUSPENSIONS
    from evolution_lab.report import Report
    report = suspended_report(suspended_campaign("scorer_metric_out_of_range", "scorer", "fault_rows"), None)
    data = report.data
    data["owner_run"] = True
    text = Report(json.dumps(data)).markdown()
    lines = text.splitlines()
    heading = "Кампания приостановлена: scorer_metric_out_of_range (владелец scorer, evidence fault_rows)."
    assert heading in lines
    assert lines[lines.index(heading) + 1] == SCORER_SUSPENSIONS["scorer_metric_out_of_range"][1]
    [continuation] = [line for line in lines if line.startswith("Операция Owner продолжается")]
    assert "свежей" not in continuation and "target_unstable" not in continuation
    assert "evo tree authorize" not in text and "rebaseline" not in text


def test_legacy_and_h1_reports_show_the_role_hint_without_naming_lab_resume():
    from evolution_lab._suspension import ROLE_SUSPENSIONS
    from evolution_lab.report import Report
    report = suspended_report(suspended_campaign("editor_failing", "editor", "stage_record"), None)
    plain = report.markdown()
    assert ROLE_SUSPENSIONS["editor_failing"][1] in plain
    assert "lab resume" not in plain
    assert "Продолжение средствами Lab в этой версии недоступно" in plain
    data = report.data
    data["legacy_run"] = True
    legacy = Report(json.dumps(data)).markdown()
    assert ROLE_SUSPENSIONS["editor_failing"][1] in legacy
    assert "lab resume" not in legacy and "`resume_native`" in legacy


def test_report_has_no_role_hint_for_target_unstable():
    from evolution_lab._suspension import ROLE_SUSPENSIONS
    markdown = suspended_report(suspended_campaign("target_unstable", "target", "threshold"), None).markdown()
    for _english, russian in ROLE_SUSPENSIONS.values():
        assert russian not in markdown


from pathlib import Path  # noqa: E402

FAMILY14 = Path(__file__).parent / "fixtures" / "family14"


def _observe_report():
    from evolution_lab.report import build_report
    payload = json.loads((FAMILY14 / "observe-result.json").read_text(encoding="utf-8"))
    return build_report(CampaignProjection.from_payload(payload), comparison_scope="in-sample"), payload


def test_observe_report_says_the_gate_was_not_applied():
    report, payload = _observe_report()
    markdown = report.markdown()
    assert "Гейт уверенности: observe (семья 14)." in markdown
    assert "Гейт не применялся (observe): результат не является свидетельством качества" in markdown
    assert "`observe_gate_not_enforced`, `uncalibrated_point_estimates`, `champion_not_contemporaneous`" in markdown
    assert ("Caveat-ы попыток: раунд 1, r01-a01: `observe_gate_not_enforced`; "
            "раунд 1, r01-a01: `uncalibrated_point_estimates`.") in markdown
    assert "Корпус: разметка implicit_train; train 2, test 0." in markdown
    assert "| Раунд | Попытка | Исход | Причина | Гейт | Фокус | Артефакт | Измерение baseline | Измерение попытки |" in markdown
    assert (r"| 1 | r01-a01 | finalist | observe_finalist | finalist/observe_finalist | 1 \[—, —\] | "
            "artifacts/candidates/r01-a01 | m-r00-champion | m-r01-a01 |") in markdown
    assert "финалисты r01-a01 (1); выбран r01-a01 (`finalist_selected`)." in markdown
    assert report.data["core"]["caveats"] == payload["caveats"]
    assert report.data["core"]["corpus_split"] == payload["corpus_split"]


def test_gate_lines_with_unreadable_caveats_do_not_raise():
    """ADR-0019 p.3: family14 without readable caveats is an unknown gate, said so (not a crash, not statistical)."""
    from evolution_lab.report import _gate_lines
    _, payload = _observe_report()
    for bad in (None, 5):
        core = dict(payload, caveats=bad)
        text = "\n".join(_gate_lines(core))
        assert "неизвестен" in text and "statistical" not in text and "(семья 14)" in text
    core = {k: v for k, v in payload.items() if k != "caveats"}
    assert "неизвестен" in "\n".join(_gate_lines(core))
    core14 = {k: v for k, v in _family14_report().data["core"].items() if k != "caveats"}
    assert "(семья 14)" in "\n".join(_gate_lines(core14))


def _family14_report():
    from evolution_lab.report import build_report
    payload = json.loads((FAMILY14 / "observe-result.json").read_text(encoding="utf-8"))
    return build_report(CampaignProjection.from_payload(payload), comparison_scope="in-sample")


def test_report_markdown_of_family_14():
    text = _family14_report().markdown()
    assert "Гейт уверенности: observe (семья 14)." in text
    assert "Настройки: стабильность stable, остановка раунда first_accepted, обновление baseline: раундов между обновлениями " in text
    assert "Роли: Editor subprocess, Reflector subprocess, Grouper fields." in text
    assert "Проверка подключения: compatible (попыток " in text
    assert "— финал (запись " in text and "когорта" not in text


def test_report_lab_notices_of_family_14_count_finalists(tmp_path, monkeypatch):
    from evolution_lab.owner import campaign_views, core_raw
    from evolution_lab.report import _lab_fields
    core = _family14_report().data["core"]
    monkeypatch.setattr(core_raw, "campaign_details", lambda root: {"journal": "present", "rounds": {}})
    seen = []
    real = campaign_views.lab_notices
    monkeypatch.setattr(campaign_views, "lab_notices", lambda *a, **k: (seen.append(k), real(*a, **k))[1])
    assert _lab_fields(tmp_path, core)["lab_notices"] == []
    finalists = [a["attempt_id"] for r in core["rounds"] for a in r["attempts"] if a["outcome"] == "finalist"]
    assert finalists and set(finalists) <= set(seen[-1]["decisions"])

    def broken(_core):
        raise KeyError("final")
    monkeypatch.setattr(campaign_views, "final_decisions", broken)
    assert _lab_fields(tmp_path, core)["lab_notices"] == []
    assert seen[-1]["decisions"] is None


def test_report_markdown_of_a_family_14_connection_pause():
    from evolution_lab.report import Report
    data = _family14_report().data  # the strict projection cannot be built paused by hand: pause its data
    data["core"].update(suspended=True, suspensions=[{"seq": 3, "reason": "connection_incompatible",
                                                      "owner": "connection", "evidence_kind": "stage_record"}])
    data["core"]["indeterminate"] = {"window": None}
    assert "начните новую кампанию" in Report(json.dumps(data)).markdown()


def test_final_lines_without_a_selected_attempt_say_none_chosen(monkeypatch):
    from evolution_lab.owner import campaign_views
    from evolution_lab.report import _final_lines
    entry = {"members": [], "contrasts": [], "ordinal": 1, "mode": "observe", "global": "off", "stability": "stable",
             "listed": 0, "selection": {"reason": "no_accepted_finalist", "attempt_id": None, "measurement_id": None}}
    monkeypatch.setattr(campaign_views, "final_of", lambda final: [entry])
    text = "\n".join(_final_lines([{"round": 1, "final": object()}]))
    assert "; выбранного нет (`no_accepted_finalist`)" in text and "выбран —" not in text


# Family 15 (spec 2026-10-19 D7, Q2 as corrected, Q3, Q5): the report gets the same `independent` block as the
# view, from one authenticated Core read; `report.md` states it in Russian. Agent-neutral literal Core payloads.

@pytest.mark.parametrize("unreadable_bindings", [False, True])
def test_b13_report_checks_survive_missing_independent(monkeypatch, tmp_path, unreadable_bindings):
    from types import SimpleNamespace
    from evolution_lab.report import _lab_independent
    from evolution_lab.cli.output import _report_lines
    from test_owner_independent_view import BLOCK
    import copy
    block = copy.deepcopy(BLOCK)
    block.update(check_modes={"seal_oracle_leak": "off"}, shadow_hits={"seal_oracle_leak": 3})
    report = _report15(monkeypatch, tmp_path, block)
    data = report.data
    data.update(_lab_independent({"independent": block, "rounds": []}, SimpleNamespace(
        bindings_reason="unreadable" if unreadable_bindings else None, bindings=None, stages=None)))
    from evolution_lab.report import Report
    report = Report(json.dumps(data))
    assert data["lab_checks"] == {"modes": {"seal_oracle_leak": "off"},
                                  "shadow_hits": {"seal_oracle_leak": 3}}
    assert (data["lab_checks_state"], data["lab_checks_reason"]) == ("present", None)
    assert (data["lab_independent"] is None) == unreadable_bindings
    md = report.markdown()
    assert "checks: seal_oracle_leak=off" in md
    assert "shadow hits: seal_oracle_leak · count 3" in md
    lines = _report_lines({"schema": "lab-owner-report/15", "report": data})
    index = lines.index("checks: seal_oracle_leak=off")
    assert lines[index + 1].startswith("independent:")
    assert lines[index + 2] == "shadow hits: seal_oracle_leak · count 3"


def test_b13_report_checks_unreadable_projection():
    from types import SimpleNamespace
    from evolution_lab.report import _lab_independent, _family_15_lines
    data = _lab_independent({"independent": {}}, SimpleNamespace(bindings_reason=None, bindings=None))
    assert (data["lab_checks"], data["lab_checks_state"], data["lab_checks_reason"]) == (
        None, "unavailable", "unreadable")
    lines = _family_15_lines(None, checks=None, checks_state="unavailable")
    assert lines.count("checks: unavailable (unreadable)") == 1


def _result15(block, change=None):
    """A family-15 Core result: the family-14 fixture's shape plus ``family_version``/``independent``; Core leaves
    ``final`` empty, ``look``/``confidence`` null and keeps the selection's publication (none here). ``change`` edits
    the payload before Core's validator reads it."""
    import copy
    payload = json.loads((FAMILY14 / "observe-result.json").read_text(encoding="utf-8"))
    payload.update(family_version=15, independent=copy.deepcopy(block), selection=[],
                   caveats=[{"kind": "champion_not_contemporaneous", "round": None, "attempt_id": None}],
                   campaign_outcome={"outcome": "completed", "reason": "rounds_exhausted", "continuation": None,
                                     "champion_path": None})
    for row in payload["rounds"]:
        row["final"] = []
        for attempt in row["attempts"]:
            attempt.update(outcome="finalist", reason="passed", look=None, confidence=None)
    for name in ("check_modes", "shadow_hits"):
        if name in block:
            payload[name] = copy.deepcopy(block[name])
    if change is not None:
        change(payload)
    return CampaignProjection.from_payload(payload)


def _report15(monkeypatch, tmp_path, block, change=None):
    from types import SimpleNamespace
    import evolution_lab.report as report_module
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.core_raw import FAMILIES, CampaignRead
    projection, seen = _result15(block, change), []

    def read(root, **kwargs):
        seen.append(kwargs)
        return CampaignRead(view=SimpleNamespace(siblings={}), family=15, status=None, result=projection)
    monkeypatch.setattr(report_module, "campaign_read", read)
    monkeypatch.setattr(core_raw, "campaign_log", lambda root, **kwargs: SimpleNamespace(
        records=[SimpleNamespace(payload={"producing_version": "2.0.0b8"})], journal_integrity="complete"))
    monkeypatch.setattr(core_raw, "campaign_details", lambda root, **kwargs: {"journal": "present", "rounds": {}})
    report = report_module.read_campaign_report(tmp_path, families=FAMILIES)
    assert report.data["lab_rounds_state"] == "present"  # fixture must not hide a shared-read keyword mismatch
    assert seen == [{"families": FAMILIES, "with_result": True}]       # the one authenticated read (D3)
    return report


@pytest.mark.parametrize('availability,line', [
    ({'round': 1, 'health': 'unavailable', 'mediator': 'failed', 'unreachable': 2,
      'transport_failures': 0}, 'round 1: probe mediator failed to start; 2 calls unreachable'),
    ({'round': 1, 'health': 'unavailable', 'mediator': 'started', 'unreachable': 1,
      'transport_failures': 1}, 'round 1: probe unavailable at health check; 1 call unreachable, 1 transport failure'),
    ({'round': 1, 'health': 'ready', 'mediator': 'started', 'unreachable': 1,
      'transport_failures': 0}, 'round 1: probe calls failed: 1 unreachable'),
])
def test_report_preserves_core_per_reflection_probe_wording(monkeypatch, tmp_path, availability, line):
    from test_owner_independent_view import BLOCK
    report = _report15(monkeypatch, tmp_path, {**BLOCK, 'probe_availability': [availability]})
    assert report.data['lab_independent_state'] == 'present'
    assert line in report.markdown()


def test_report_markdown_of_family_15_confidence_statement(monkeypatch, tmp_path):
    import copy
    from evolution_lab.owner.independent_view import closed_rounds, independent_view
    from test_owner_independent_view import BLOCK
    report = _report15(monkeypatch, tmp_path, BLOCK)
    # the view's own block, grouped with the rounds Core has closed (this fixture's rounds are all closed)
    closed = closed_rounds(report.data["core"]["rounds"])
    assert closed and report.data["lab_independent"] == independent_view(BLOCK, closed_rounds=closed)
    md = report.markdown()
    assert "- focus: прирост 0.12 (оценка); с уверенностью 90% не ниже 0.03; порог ≥ 0.00 → passed" in md
    assert "- global: прирост 0.05; порог ≥ 0.00" in md
    assert ("Настройки: R 1, режим confidence, шум исполнения negligible (допуск 0.00, assumed_negligible), "
            "проверка reuse, fresh_parent нет.") in md
    assert "0.03000000000000000" not in md      # enclosure.upper never printed
    assert "Гейт уверенности" not in md and "| Гейт | Фокус |" not in md   # no family-14 gate words for 15
    assert ("- Условная стоимость в слотах замеров (не деньги): кандидат 8 (гейт 6, финал 2), baseline 0, "
            "свежий замер чемпиона: гейт 0, финал 0 (основа: per_reached_finalist); простейший путь к прохождению 8.") in md
    wide = copy.deepcopy(report.data)
    wide["lab_independent"]["attempts"][0]["gate_comparison"]["scopes"][1]["threshold"]["value"] = None
    from evolution_lab.report import Report
    assert ("- global: прирост 0.05; порог ≥ вне диапазона отображения (точное значение сохранено)"
            in Report(json.dumps(wide)).markdown())
    assert "Infinity" not in Report(json.dumps(wide)).markdown()


@pytest.mark.parametrize("legacy", [False, True])
def test_family15_operator_report_copies_stage_and_observed_variance(monkeypatch, tmp_path, legacy):
    from copy import deepcopy
    from evolution_lab.cli.output import independent_lines
    from evolution_lab.cli.responses import _independent
    from test_owner_independent_view import BLOCK, R

    block = deepcopy(BLOCK)
    noise = block["attempts"][0]["gate_comparison"]["noise"]
    noise.update(kind="observed", source_kind="diagnostic_only", variation="unavailable", observed_cases=1)
    if not legacy:
        block["round_progress"] = [{"round": 1, "outcome": None, "reason": None, "attempts_opened": 1,
                                    "candidates_sealed": 1, "attempts_measured": 1, "reflections": []}]
        block["attempts"][0]["progress"] = {"editor": "edited", "editor_failures": [], "candidate_sealed": True,
                                             "measurement": "scored", "parent_measurement": "started"}
        noise["repeat_variance"] = {"minimum": R(1, 8), "maximum": R(1, 8),
                                    "unavailable_counts": {"incomplete": 1}}
    report = _report15(monkeypatch, tmp_path, block)
    copied = report.data["lab_independent"]
    _independent(copied)
    md, plain = report.markdown(), "\n".join(independent_lines(copied))
    assert "в квадрате балла" in md and "score squared" in plain
    assert ("incomplete" in md) is not legacy
    if legacy:
        assert "round_progress" not in copied and "нет подробностей" in md
    else:
        assert copied["round_progress"] == block["round_progress"]
        assert "открыто попыток 1" in md and "Candidate measurement scored" in plain
        variance = copied["attempts"][0]["gate_comparison"]["noise"]["repeat_variance"]
        assert variance["minimum"] == {"value": 0.125, "exact": R(1, 8)}
        assert variance["unavailable_counts"] == {"incomplete": 1}
        variance["unavailable_counts"] = {"private text": 1}
        with pytest.raises(ValueError):
            _independent(copied)


def test_report_family_15_reference_names_the_champion_measurement(monkeypatch, tmp_path):
    import copy
    from test_owner_independent_view import BLOCK
    block = copy.deepcopy(BLOCK)
    block["settings"]["fresh_parent"] = True
    block["attempts"][0]["gate_comparison"]["reference"] = "fresh"
    md = _report15(monkeypatch, tmp_path, block).markdown()
    head, = [row for row in md.splitlines() if row.startswith("Попытка r01-a01")]
    line, = [row for row in md.splitlines() if "сравнение со шлюзом" in row]
    assert "против чемпиона раунда · свежий замер" in line and "Parent" not in line
    assert "исход finalist, причина passed" in head
    assert "сравнение со шлюзом passed (passed), решающий слой focus" in line


def _two_layer_report15(monkeypatch, tmp_path):
    import copy
    from test_owner_independent_view import BLOCK
    data = _report15(monkeypatch, tmp_path, BLOCK).data
    attempt = data["lab_independent"]["attempts"][0]
    gate = copy.deepcopy(attempt["gate_comparison"])
    gate.update(scopes=[gate["scopes"][0]], reference_measurement_id="m-gate-reference-123")
    global_ = copy.deepcopy(attempt["gate_comparison"])
    global_.update(scopes=[global_["scopes"][1]], deciding_layer="global", outcome="rejected",
                   reason="global_below_threshold", reference="fresh", reference_measurement_id="m-global-reference-456")
    attempt.update(gate_comparison=gate, global_comparison=global_)
    return data


def test_md_attempt_shows_gate_and_global_layers(monkeypatch, tmp_path):
    from evolution_lab.report import Report
    data = _two_layer_report15(monkeypatch, tmp_path)
    md = Report(json.dumps(data)).markdown()
    gate, global_ = md.split("сравнение со шлюзом", 1)[1].split("глобальное сравнение", 1)
    assert "passed (passed), решающий слой focus" in gate
    assert "- focus:" in gate and "- global:" not in gate
    assert "rejected (global_below_threshold), решающий слой global" in global_
    assert "- global:" in global_ and "- focus:" not in global_
    assert md.count("решающий слой") == 2  # no compatibility-summary duplicate
    assert "сравнения нет" not in md


@pytest.mark.parametrize("missing,present,absent", [
    ("global_comparison", "сравнение со шлюзом", "глобальное сравнение"),
    ("gate_comparison", "глобальное сравнение", "сравнение со шлюзом"),
])
def test_md_attempt_with_one_layer_omits_the_other(monkeypatch, tmp_path, missing, present, absent):
    from evolution_lab.report import Report
    data = _two_layer_report15(monkeypatch, tmp_path)
    data["lab_independent"]["attempts"][0][missing] = None
    md = Report(json.dumps(data)).markdown()
    assert present in md and absent not in md
    assert md.count("решающий слой") == 1
    assert "сравнения нет" not in md


@pytest.mark.parametrize("reference_id,shown", [("m-gate-reference-123", "m-gate-refer"), (None, "—")])
def test_md_layer_names_its_reference_measurement(monkeypatch, tmp_path, reference_id, shown):
    from evolution_lab.report import Report
    data = _two_layer_report15(monkeypatch, tmp_path)
    data["lab_independent"]["attempts"][0]["gate_comparison"]["reference_measurement_id"] = reference_id
    md = Report(json.dumps(data)).markdown()
    gate, = [line for line in md.splitlines() if "сравнение со шлюзом" in line]
    global_, = [line for line in md.splitlines() if "глобальное сравнение" in line]
    assert "против чемпиона раунда · сохранённый замер" in gate and f"эталон {shown}" in gate
    assert "против чемпиона раунда · свежий замер" in global_ and "эталон m-global-ref" in global_
    assert "m-gate-reference-123" not in md and "m-global-reference-456" not in md


def test_md_attempt_without_layers_keeps_no_comparison_and_preflight(monkeypatch, tmp_path):
    from test_owner_independent_view import PREFLIGHT_BLOCK
    report = _report15(monkeypatch, tmp_path, PREFLIGHT_BLOCK)
    md = report.markdown()
    assert "сравнение со шлюзом" not in md and "глобальное сравнение" not in md
    assert "Попытка r01-a02 (раунд 1): исход rejected, причина structurally_unattainable; сравнения нет." in md
    assert ("- Предварительная проверка: impossible (stored_reference): focus: наибольшая "
            "уверенность 50% при запрошенной 90% (не может пройти); global: наибольший прирост 1.00; "
            "порог > 0.00 (может пройти).") in md
    assert "Раунд 1 — предварительная проверка достижимости: impossible (stored_reference)" in md
    assert "Ориентир ядра: не меньше 4 единиц (conditional_guidance, not_an_admission_or_power_claim)." in md


def test_report_family_15_unavailable_evidence_says_so(monkeypatch, tmp_path):
    from test_owner_independent_view import BLOCK
    for evidence in ("unavailable", "snapshot_only"):
        block = {**BLOCK, "evidence": evidence, "attempts": [], "committed_work": None, "remaining_work": None}
        md = _report15(monkeypatch, tmp_path, block).markdown()
        assert (f"Сравнения недоступны (свидетельство ядра `{evidence}`): публикации попыток не подтверждены; "
                "это не значит, что попыток не было.") in md
        assert "попыток нет" not in md.lower() and "Попытка r01" not in md


def test_c58r1_report_preserves_core_unavailable_with_binding_failure():
    from types import SimpleNamespace
    from evolution_lab.owner.core_raw import CampaignRead
    from evolution_lab.report import _lab_independent, _family_15_lines
    from test_owner_independent_view import BLOCK
    block = {**BLOCK, "evidence": "unavailable", "attempts": [], "committed_work": None,
             "remaining_work": None, "next_action": "repair_evidence"}
    read = CampaignRead(SimpleNamespace(), 15, None, None, bindings_reason="unreadable")
    data = _lab_independent({"independent": block, "rounds": []}, read)
    assert data["lab_independent_state"] == "present"
    assert data["lab_independent"]["next_action"] == "repair_evidence"
    assert data["lab_independent"]["attempts"] == []
    assert any("Сравнения недоступны" in line for line in _family_15_lines(data["lab_independent"]))


def test_report_family_15_unreadable_block_is_said_so():
    from evolution_lab.report import _family_15_lines
    assert _family_15_lines(None) == ["Независимая проверка (семья 15): блок ядра не читается этой версией Lab; "
                                      "сравнения не показаны.", ""]
    assert _family_15_lines(None, "unreadable") == [
        "Независимая проверка (семья 15): блок ядра не читается этой версией Lab (`unreadable`); "
        "сравнения не показаны.", ""]


def test_report_family_15_unreadable_block_has_an_explicit_state(monkeypatch, tmp_path):
    """ADR-0019 p.3: a failed read is an explicit state next to the empty value. The view says
    ``details_state``/``details_reason`` (``details_unavailable("unreadable")``); the report says
    ``lab_independent_state``/``lab_independent_reason`` beside ``lab_independent``."""
    from test_owner_independent_view import BLOCK
    from evolution_lab.owner import independent_view as iv
    readable = _report15(monkeypatch, tmp_path, BLOCK).data
    assert readable["lab_independent"] is not None
    assert (readable["lab_independent_state"], readable["lab_independent_reason"]) == ("present", None)

    def undescribed(block, **kwargs):     # Core's validator admits the block; this pin's view cannot read it
        raise ValueError("value not described by this pin")
    monkeypatch.setattr(iv, "independent_view", undescribed)
    report = _report15(monkeypatch, tmp_path, BLOCK)
    data = report.data
    assert data["lab_independent"] is None
    assert (data["lab_independent_state"], data["lab_independent_reason"]) == ("unavailable", "unreadable")
    assert ("Независимая проверка (семья 15): блок ядра не читается этой версией Lab (`unreadable`); "
            "сравнения не показаны.") in report.markdown()


def test_report_family_14_has_no_lab_independent_keys(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import evolution_lab.report as report_module
    from evolution_lab.owner import core_raw
    from evolution_lab.owner.core_raw import FAMILIES, CampaignRead
    projection = CampaignProjection.from_payload(
        json.loads((FAMILY14 / "observe-result.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(report_module, "campaign_read", lambda root, **kwargs: CampaignRead(
        view=SimpleNamespace(siblings={}), family=14, status=None, result=projection))
    monkeypatch.setattr(core_raw, "campaign_log", lambda root, **kwargs: SimpleNamespace(
        records=[SimpleNamespace(payload={"producing_version": "2.0.0b8"})], journal_integrity="complete"))
    monkeypatch.setattr(core_raw, "campaign_details", lambda root: {"journal": "present", "rounds": {}})
    data = report_module.read_campaign_report(tmp_path, families=FAMILIES).data
    assert not {"lab_independent", "lab_independent_state", "lab_independent_reason"} & set(data)


def test_report_family_15_open_round_of_a_finished_campaign_is_unknown_as_in_the_view(monkeypatch, tmp_path):
    """The report classifies with the view's classifier and the campaign's recorded terminal: a gate finalist of the
    round a cancellation left open is ``unknown`` in both, so ``lab_independent`` equals the view's block."""
    import copy
    from evolution_lab.owner.independent_view import independent_view
    from test_owner_independent_view import BLOCK
    block = copy.deepcopy(BLOCK)
    block["attempts"][0].update(round=2, comparison=None)

    def cancelled(payload):
        payload["rounds"][-1]["outcome"] = None               # round 2 left open by the cancellation
        payload["campaign_outcome"] = dict(payload["campaign_outcome"], outcome="stopped", reason="cancelled",
                                           continuation={"kind": "linked_run", "reason": "cancelled"})

    data = _report15(monkeypatch, tmp_path, block, cancelled).data
    assert data["core"]["rounds"][-1]["outcome"] is None and data["core"]["complete"] is True
    assert data["lab_independent"]["attempts"][0]["group"] == "unknown"
    assert data["lab_independent"] == independent_view(block, closed_rounds=frozenset({1}), campaign_finished=True)


def test_owner_report_schema_is_10():
    from evolution_lab.owner.models import ReportResult
    assert ReportResult("o-1", "available", {}, None).to_payload()["schema"] == "lab-owner-report/15"


def test_h1_report_of_family_15_stays_unsupported(monkeypatch, tmp_path):
    """H1 keeps the default families (14): Core's family-15 snapshot is refused before any projection."""
    from evolution.tree.campaign import CampaignFamilyUnsupported
    from test_owner_core_raw import STATUS_PAYLOAD, _core_calls
    from evolution_lab.report import read_campaign_report
    calls = []
    _core_calls(monkeypatch, 15, STATUS_PAYLOAD, calls)
    with pytest.raises(CampaignFamilyUnsupported):
        read_campaign_report(tmp_path)
    assert [c["fn"] for c in calls] == ["inspect"]


@pytest.mark.parametrize(("passes", "verdict"), [(False, "не может пройти"), (True, "может пройти")])
def test_ru_preflight_bound_verdict_follows_confidence(monkeypatch, tmp_path, passes, verdict):
    import copy
    from test_owner_independent_view import PREFLIGHT_BLOCK, R
    block = copy.deepcopy(PREFLIGHT_BLOCK)
    for explanation in (block["attempts"][0]["preflight"], block["preflight"][0]["explanation"]):
        explanation["scopes"][0].update(c_max=R(4, 5), passes=passes)
        explanation["scopes"] = explanation["scopes"][:1]
    md = _report15(monkeypatch, tmp_path, block).markdown()
    bound_lines = [line for line in md.splitlines() if "наибольшая уверенность" in line]
    assert len(bound_lines) == 2  # both round-level and attempt-level preflight
    for bound_line in bound_lines:
        assert f"наибольшая уверенность 80% при запрошенной 90% ({verdict})" in bound_line
        assert "наибольший прирост" not in bound_line and "порог" not in bound_line


def test_ru_and_en_preflight_bound_show_the_same_fields():
    from evolution_lab.cli.output import _preflight_text
    from evolution_lab.owner.independent_view import independent_view
    from evolution_lab.report import _preflight_md
    from test_owner_independent_view import PREFLIGHT_BLOCK
    explanation = independent_view(PREFLIGHT_BLOCK)["attempts"][0]["preflight"]
    assert _preflight_md(explanation) == (
        "impossible (stored_reference): focus: наибольшая уверенность 50% при запрошенной 90% (не может пройти); "
        "global: наибольший прирост 1.00; порог > 0.00 (может пройти)")
    assert _preflight_text(explanation) == (
        "  preflight impossible: focus maximal confidence 50% vs requested 90% (cannot pass); "
        "global maximal gain 1.00 vs threshold > 0.00 (can pass)")


def test_ru_preflight_status_and_no_threshold(monkeypatch, tmp_path):
    from evolution_lab.report import Report
    from test_owner_independent_view import BLOCK, PREFLIGHT_BLOCK
    preflight_md = _report15(monkeypatch, tmp_path, PREFLIGHT_BLOCK).markdown()
    assert "impossible (stored_reference)" in preflight_md
    assert "causal:" not in preflight_md  # skipped scopes have no ceilings or fabricated verdict
    data = _report15(monkeypatch, tmp_path, BLOCK).data
    scopes = data["lab_independent"]["attempts"][0]["gate_comparison"]["scopes"]
    scopes[0].update(display="status", status="unavailable", unavailable="insufficient_units")
    scopes[1].update(display="no_threshold", point={"value": 0.05, "exact": "1/20"})
    md = Report(json.dumps(data)).markdown()
    assert "- focus: unavailable (insufficient_units) → passed" in md
    assert "- global: прирост 0.05; порога нет" in md
    assert "порог ≥" not in md and "с уверенностью" not in md


def test_cost_shows_champion_basis(monkeypatch, tmp_path):
    from test_owner_independent_view import BLOCK
    md = _report15(monkeypatch, tmp_path, BLOCK).markdown()
    assert "финал 0 (основа: per_reached_finalist)" in md


@pytest.mark.parametrize("stopped_reason", [None, "budget_exhausted"])
async def test_evaluation_report_cli_shows_coverage(tmp_path, stopped_reason):
    """Ordinary report's decoder/renderer must retain Lab facts and partial acceptance."""
    from io import StringIO

    import httpx

    from evolution_lab.cli import main
    from evolution_lab.cli.client import ServiceClient
    from evolution_lab.evaluation import evaluate_fixed
    from evolution_lab.owner import ReportResult
    from evolution_lab.report import read_evaluation_report

    kwargs = bindings(tmp_path, repeats=20)
    scorer = kwargs["scorer"]
    real_score = scorer.score

    async def score(case, view, candidate):
        if scorer.calls == 0:
            scorer.calls += 1
            raise ValueError("PRIVATE-SCORER-ERROR")
        return await real_score(case, view, candidate)

    scorer.score = score
    result = await evaluate_fixed(**kwargs, min_coverage=0.95)
    lab_result = {**result, "stopped_reason": stopped_reason,
                  "persistence": {"state": "saved", "failure": None}}
    report = read_evaluation_report(kwargs["run_directory"], lab_result=lab_result)
    payload = ReportResult("evaluation", "available", report.data, None).to_payload()
    assert payload["report"]["measurements"][0]["coverage"] == {
        "expected": 20, "scored": 19, "state": "accepted" if stopped_reason is None else "incomplete",
    }

    def respond(request):
        assert request.method == "GET" and request.url.path == "/v1/operations/evaluation/report"
        return httpx.Response(200, json=payload)

    outputs = {}
    for machine in (True, False):
        stdout, stderr = StringIO(), StringIO()
        client = ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(respond))
        code = main(["--socket", "/tmp/lab.sock", *(["--json"] if machine else []),
                     "report", "evaluation"], stdout=stdout, stderr=stderr,
                    client_factory=lambda **_: client)
        assert code == 0 and stderr.getvalue() == ""
        outputs[machine] = stdout.getvalue()
    assert json.loads(outputs[True]) == payload
    text = outputs[False]
    assert "19 of 20 scored" in text
    assert "unmeasured=1" in text and "min_coverage=0.95" in text
    assert f"stopped_reason={stopped_reason}" in text
    assert ("accepted" in text) is (stopped_reason is None)
    assert "PRIVATE-SCORER-ERROR" not in text and "PRIVATE-SCORER-ERROR" not in outputs[True]


@pytest.mark.parametrize("evidence_count", [1, 400, 1200])
async def test_report_accepted_partial_shows_coverage(tmp_path, evidence_count):
    from evolution_lab.evaluation import evaluate_fixed
    from evolution_lab.report import read_evaluation_report
    kwargs = bindings(tmp_path, repeats=20)
    scorer = kwargs["scorer"]
    real_score = scorer.score
    async def score(case, view, candidate):
        if scorer.calls == 0:
            scorer.calls += 1
            raise ValueError("PRIVATE-SCORER-ERROR")
        return await real_score(case, view, candidate)
    scorer.score = score
    result = await evaluate_fixed(**kwargs, min_coverage=0.95)
    assert result["min_coverage"] == 0.95 and len(result["unmeasured"]) == 1
    report = read_evaluation_report(kwargs["run_directory"])
    assert report.data["measurements"][0]["coverage"] == {"expected": 20, "scored": 19, "state": "accepted"}
    assert report.data["lab_result"]["unmeasured"] == result["unmeasured"]
    text = report.markdown()
    assert "19 из 20" in text and "accepted" in text and "Неоценённых сэмплов: 1" in text
    assert "scorer_failed" in text and "PRIVATE-SCORER-ERROR" not in text

    # Exercise diagnostic size without replaying hundreds of synthetic rollouts.
    # The real 19/20 projection above establishes the accepted-partial presentation;
    # this saved evidence list establishes complete diagnostic writer/reader retention.
    from evolution_lab._outcomes import evaluation_outcome
    evidence = [{"case_id": "neutral-case", "sample_id": f"sample-{index}",
                 "outcome": "scorer_failed", "tries": 1} for index in range(evidence_count)]
    diagnostic = tmp_path / "diagnostic"
    diagnostic.mkdir()
    with evaluation_outcome(diagnostic, min_coverage=0.95) as lab_result:
        lab_result.update(run_id="synthetic", coverage={"expected": 20 * evidence_count,
            "scored": 19 * evidence_count}, unmeasured=evidence)
    from evolution_lab.report import _read_lab_result
    decoded = _read_lab_result(diagnostic / "evaluation-result.json")
    assert decoded["unmeasured"] == evidence and decoded["min_coverage"] == 0.95

    for changes in ({"stopped_reason": "budget_exhausted"}, {"min_coverage": 1.0},
                    {"persistence": {"state": "failed"}}):
        refused = read_evaluation_report(kwargs["run_directory"], lab_result={**report.data["lab_result"], **changes})
        assert refused.data["measurements"][0]["coverage"]["state"] == "incomplete"


def test_campaign_markdown_formats_fields_safely_and_preserves_json_bytes(tmp_path):
    from pathlib import Path
    from evolution_lab.report import Report, build_report

    report = build_report(campaign(), lab_result={"stopped_reason": "done", "exit_code": 0})
    report.write(tmp_path / "report")
    assert (tmp_path / "report/report-data.json").read_bytes() == (
        Path(__file__).parent / "fixtures/dev72-report-data.json").read_bytes()
    text = report.markdown()
    assert "Исходная версия: digest=" in text
    assert "Текущая версия: baseline_origin=" in text
    assert "Остановка / завершение Lab: exit_code=0, stopped_reason=done" in text
    assert not any(fragment in text for fragment in ("&quot;", "&#x27;", "{'"))

    data = report.data
    data["core"]["initial_champion"]["version"] = '<script>*bad* [link](https://evil) `x` | &quot;'
    text = Report(json.dumps(data, ensure_ascii=False)).markdown()
    assert r'\<script\>\*bad\* \[link\](https://evil) \`x\` \| \&quot;' in text
    assert '<script>' not in text


def test_evaluation_markdown_formats_lab_result_and_keeps_unanswered():
    from evolution_lab.report import Report, build_report
    data = build_report(campaign(), lab_result={"exit_code": 0, "message": 'a "quote"'}).data
    data["operation"] = "evaluation"
    data["measurements"] = [{"run_id": "m", "candidate_digest": "v0", "coverage": {"scored": 2, "expected": 2, "state": "complete"}, "unanswered_state": "present", "unanswered": 1, "tries": None, "excluded_pairs": None, "journal_integrity": "ok", "metrics": {}, "samples": []}]
    text = Report(json.dumps(data, ensure_ascii=False)).markdown()
    assert 'Остановка / завершение Lab: exit_code=0, message=a "quote"' in text
    assert "без ответа: 1 из 2." in text
    assert not any(fragment in text for fragment in ("&quot;", "&#x27;", "{'"))


def baseline_import_status():
    counts = dict.fromkeys(
        ("not_scheduled", "seed_differs", "not_scored", "primary_missing", "out_of_range", "dependency_differs"), 0,
    )
    counts["seed_differs"] = 3
    return {
        "source_kind": "measurement", "from_run": "evaluation-run", "source_measurement_id": "evaluation",
        "imported": 1, "scheduled": 4, "fields": ["identity.scorer.digest"], "not_imported": counts,
        "components_carried": 0, "provenance": {"reported": 0, "unknown": 1, "source_recorded": False},
        "abandoned": False, "authorization_id": "a" * 64, "held_out": None,
        "held_out_authorization_id": None,
    }


def test_lab_reads_core_status_with_baseline_import():
    from evolution_lab.owner.campaign_views import caveats_of
    from evolution_lab.report import build_report

    legacy = campaign().to_payload()
    for block in (None, baseline_import_status()):
        payload = {**legacy, "baseline_import": block}
        if block is not None:
            payload["caveats"] = [{
                "kind": "baseline_imported", "from_run": block["from_run"], "source_kind": block["source_kind"],
                "source_measurement_id": block["source_measurement_id"], "imported": block["imported"],
                "scheduled": block["scheduled"], "fields": block["fields"], "authorization_id": block["authorization_id"],
            }, {
                "kind": "imported_provenance_unknown", "from_run": block["from_run"],
                "imported": block["imported"], "unknown": 1, "source_recorded": False,
            }, *legacy["caveats"]]
        admitted = CampaignProjection.from_payload(payload)
        assert admitted.to_payload() == payload
        report = build_report(admitted, comparison_scope="in-sample")
        assert report.data["core"]["baseline_import"] == block
        if block is not None:
            assert {item["kind"] for item in caveats_of(payload)} >= {
                "baseline_imported", "imported_provenance_unknown",
            }
            for text in ("evaluation-run", "seed_differs", "identity.scorer.digest",
                         "a" * 64, "imported_provenance_unknown"):
                assert text in report.markdown()
