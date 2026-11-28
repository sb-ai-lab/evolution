"""Fixed-evaluation focus evidence, private aggregate cache and presentation."""
import asyncio
import json

import pytest

from evolution_lab.owner import core_raw, measurements, readers
from test_owner_projection_equivalence import real_operation

NOTICE = {"code": "scorer_without_focus_signal", "admitted": 2, "not_completed": 2}


def sample(case, *, critical=None, outcome="completed", scoring="finished"):
    return {"case_id": case, "sample_id": "01", "scoring_state": scoring,
            "critical_pass": critical, "execution": {"outcome": outcome},
            "execution_state": "finished", "metrics": {"score": 1.0}, "tries": 1}


def core():
    return {"run_id": "evaluation", "complete": True,
            "samples": [sample("one"), sample("two", outcome="failed"),
                        sample("three", scoring="failed", outcome="failed")]}


def evidence(monkeypatch, outputs=None):
    outputs = outputs or {}
    def scoring(journal, case, sample, **kwargs):
        return {"state": "finished", "score_request_id": case, "critical_pass": None}
    def trace(root, request, case, *, measurement_id, require_output=False):
        value = outputs.get(case, '{"verdicts": []}')
        if isinstance(value, Exception):
            raise value
        return {"stdout": value, "output_reason": None, "input": None, "returncode": 0}
    monkeypatch.setattr(core_raw, "scoring_index", lambda _: {})
    monkeypatch.setattr(core_raw, "scoring_from_index", scoring)
    monkeypatch.setattr(core_raw, "scorer_trace", trace)


@pytest.mark.parametrize("branch", ["absent", "verdict", "true", "false", "missing", "malformed",
                                    "adapter", "unknown_critical", "zero", "nonterminal"])
def test_notice_requires_complete_evidence_for_every_admitted_sample(tmp_path, monkeypatch, branch):
    value = core()
    outputs = {}
    want, state = [NOTICE], "present"
    if branch == "verdict":
        outputs["two"] = '{"verdicts": [{"criterion": "quality", "passed": true}]}'
        want = []
    elif branch in {"true", "false"}:
        value["samples"][1]["critical_pass"] = branch == "true"
        want = []
    elif branch in {"missing", "malformed", "adapter", "unknown_critical"}:
        outputs["two"] = {"missing": None, "malformed": "{", "adapter": core_raw.CoreRawUnavailable("unreadable"),
                          "unknown_critical": '{"verdicts": []}'}[branch]
        if branch == "unknown_critical":
            del value["samples"][1]["critical_pass"]
        want, state = [], "unavailable"
    elif branch == "zero":
        value["samples"] = [sample("one", scoring="failed")]
        want = []
    elif branch == "nonterminal":
        value["complete"] = False
        want = []
    evidence(monkeypatch, outputs)
    if branch in {"true", "false"}:
        original = core_raw.scoring_from_index
        def scoring(*args, **kwargs):
            row = original(*args, **kwargs)
            if args[1] == "two": row["critical_pass"] = branch == "true"
            return row
        monkeypatch.setattr(core_raw, "scoring_from_index", scoring)
    fields = measurements.evaluation_focus_notices(value, tmp_path, tmp_path / "measurement", terminal=branch != "nonterminal")
    assert fields == {"lab_notices": want, "lab_notices_state": state}


def test_unreadable_sample_overrides_another_samples_signal(tmp_path, monkeypatch):
    value = core()
    evidence(monkeypatch, {"two": None, "one": '{"verdicts": [{"criterion": "q", "passed": true}]}'})
    assert measurements.evaluation_focus_notices(value, tmp_path, tmp_path) == {
        "lab_notices": [], "lab_notices_state": "unavailable"}


def test_campaign_rows_do_not_claim_a_focus_check():
    value = {"measurements": [{"measurement_id": "m", "status": {"samples": core()["samples"]}}]}
    row = measurements.measurement_rows(value, "campaign", {}, None)[0]
    assert (row["lab_notices"], row["lab_notices_state"]) == ([], "present")


@pytest.mark.parametrize("change", ["same", "file", "incomplete_signature", "unavailable", "during_read", "drop"])
def test_notice_cache_reuses_only_complete_unchanged_evidence(tmp_path, monkeypatch, change):
    async def scenario():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            owner, ref = stand.owner, stand.ref
            # Fixture's journal is genuine; raw trace reads are isolated at the adapter boundary.
            evidence(monkeypatch)
            journal = stand.run / "measurement/events.jsonl"
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            for record in records:
                if record["event"] == "scoring_finished":
                    record["payload"]["critical_pass"] = None
            journal.write_text("".join(json.dumps(record) + "\n" for record in records))
            calls = []
            original = measurements.evaluation_focus_notices
            def compute(*args, **kwargs):
                calls.append(None)
                result = original(*args, **kwargs)
                if change == "during_read":
                    (stand.run / "changed").write_text(str(len(calls)))
                if change == "unavailable" and len(calls) == 1:
                    return {"lab_notices": [], "lab_notices_state": "unavailable"}
                return result
            monkeypatch.setattr(measurements, "evaluation_focus_notices", compute)
            first = await owner.measurements(ref)
            assert first["schema"] == "lab-measurement-list/4"
            mid = first["measurements"][0]["measurement_id"]
            if change == "file":
                (stand.run / "trace-change").write_text("new evidence")
            elif change == "incomplete_signature":
                monkeypatch.setattr(readers, "run_signature", lambda *args: None)
            elif change == "drop":
                owner._measurements.drop(ref.id)
            second = await owner.measurement(ref, mid)
            fields = {key: second["measurement"][key] for key in ("lab_notices", "lab_notices_state")}
            assert fields == {"lab_notices": [{"code": "scorer_without_focus_signal", "admitted": 2,
                                              "not_completed": 0}], "lab_notices_state": "present"}
            assert len(calls) == (1 if change == "same" else 2)
            # A fresh projection and the cached one agree, including the new fields.
            owner._measurements.drop(ref.id)
            fresh = await owner.measurement(ref, mid)
            assert fresh == second
    asyncio.run(scenario())


def test_report_and_measurement_share_notice_projection(tmp_path, monkeypatch):
    async def scenario():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            evidence(monkeypatch)
            calls = []
            def compute(*args, **kwargs):
                calls.append(None)
                return {"lab_notices": [NOTICE], "lab_notices_state": "present"}
            monkeypatch.setattr(measurements, "evaluation_focus_notices", compute)
            row = (await stand.owner.measurements(stand.ref))["measurements"][0]
            report = (await stand.owner.report(stand.ref)).to_payload()
            assert report["schema"] == "lab-owner-report/15"
            assert report["report"]["lab_notices"] == row["lab_notices"] == [NOTICE]
            markdown = (await stand.owner.report_markdown(stand.ref)).markdown
            assert "метрики фокус не строят" in markdown.lower()
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", [False, True])
def test_cli_and_markdown_explain_focus_notice_or_unavailable(unavailable):
    from evolution_lab.cli.output import _measurement_lines, _report_lines
    from evolution_lab.report import Report, build_report
    from pathlib import Path
    from evolution.tree import inspect_journal, project_result
    projection = project_result(inspect_journal(Path("tests/fixtures/panel/b15/focus-open/run/measurements/m-r00-champion")))
    fields = {"lab_notices": [] if unavailable else [NOTICE],
              "lab_notices_state": "unavailable" if unavailable else "present"}
    data = build_report(projection).data
    data.update(fields)
    text = Report(json.dumps(data)).markdown()
    row = measurements.measurement_rows(core(), "evaluation", {}, None)[0]
    row.update(fields)
    lines = _measurement_lines({"schema": "lab-measurement/4", "measurement": row})
    lines += _report_lines({"schema": "lab-owner-report/15", "report": data})
    if unavailable:
        assert "проверка сигнала фокуса недоступна: вывод скорера не прочитан" in text.lower()
        assert sum("проверка сигнала фокуса недоступна" in line.lower() for line in lines) == 2
    else:
        assert "метрики фокус не строят" in text.lower()
        assert sum("metrics do not build" in line for line in lines) == 2


@pytest.mark.parametrize("bad", ["state", "unavailable_notice", "code", "extra", "bool_count", "negative",
                                 "duplicate", "campaign", "missing", "old_schema"])
def test_measurement_decoder_rejects_invalid_notice_contract(bad):
    from evolution_lab.cli import responses
    row = measurements.measurement_rows(core(), "evaluation", {}, None)[0]
    row.update(lab_notices=[NOTICE], lab_notices_state="present")
    value = {"schema": "lab-measurement-list/4", "operation_id": "op1", "measurements": [row]}
    if bad == "state": row["lab_notices_state"] = "unknown"
    elif bad == "unavailable_notice": row["lab_notices_state"] = "unavailable"
    elif bad == "code": row["lab_notices"] = [{**NOTICE, "code": "unknown"}]
    elif bad == "extra": row["lab_notices"] = [{**NOTICE, "text": "private"}]
    elif bad == "bool_count": row["lab_notices"] = [{**NOTICE, "admitted": True}]
    elif bad == "negative": row["lab_notices"] = [{**NOTICE, "not_completed": -1}]
    elif bad == "duplicate": row["lab_notices"] = [NOTICE, NOTICE]
    elif bad == "campaign": row["role"] = "baseline"
    elif bad == "missing": del row["lab_notices_state"]
    elif bad == "old_schema": value["schema"] = "lab-measurement-list/2"
    with pytest.raises(ValueError):
        responses.validate(value, "lab-measurement-list/4")


def test_measurement_decoder_accepts_notice_contract():
    from evolution_lab.cli import responses
    row = measurements.measurement_rows(core(), "evaluation", {}, None)[0]
    row.update(lab_notices=[NOTICE], lab_notices_state="present")
    value = {"schema": "lab-measurement-list/4", "operation_id": "op1", "measurements": [row]}
    assert responses.validate(value, "lab-measurement-list/4") is value


def test_standalone_report_reads_real_evaluation_scorer_trace():
    from panel_fixtures import load_snapshot, FIXTURES
    from evolution_lab.report import read_evaluation_report
    snapshot = load_snapshot("content")
    root = FIXTURES / "content/run" / snapshot["meta"]["ids"]["evaluation"] / "measurement"
    report = read_evaluation_report(root, lab_result={"stopped_reason": None})
    assert report.data["lab_notices_state"] == "present"
    assert report.data["lab_notices"] == []


def test_owner_notice_uses_the_same_root_and_journal_as_lab_sample(tmp_path, monkeypatch):
    async def scenario():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            original = measurements.evaluation_focus_notices
            seen = []
            def read(core, root, journal, **kwargs):
                seen.append((root, journal))
                return original(core, root, journal, **kwargs)
            monkeypatch.setattr(measurements, "evaluation_focus_notices", read)
            await stand.owner.measurements(stand.ref)
            assert seen == [(stand.run / "measurement", stand.run / "measurement/events.jsonl")]
    asyncio.run(scenario())


@pytest.mark.parametrize("critical", ["missing", "yes", 1])
def test_existing_scoring_reader_refuses_unconfirmed_critical_pass(tmp_path, critical):
    from test_owner_content import _sample_trace
    _, journal = _sample_trace(tmp_path)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    finished = rows[-1]["payload"]
    if critical == "missing":
        del finished["critical_pass"]
    else:
        finished["critical_pass"] = critical
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(core_raw.CoreRawUnavailable):
        core_raw.scoring_of(journal, "case", "00", require_critical=True)


def test_notice_rejects_projection_and_scoring_journal_disagreement(tmp_path, monkeypatch):
    evidence(monkeypatch)
    monkeypatch.setattr(core_raw, "scoring_from_index", lambda *args, **kwargs: {
        "state": "finished", "critical_pass": False, "score_request_id": "case"})
    assert measurements.evaluation_focus_notices(core(), tmp_path, tmp_path, terminal=True) == {
        "lab_notices": [], "lab_notices_state": "unavailable"}


def test_running_evaluation_has_no_terminal_notice_even_with_complete_journal(tmp_path, monkeypatch):
    async def scenario():
        async with real_operation(tmp_path, kind="evaluation") as stand:
            evidence(monkeypatch)
            row = (await stand.owner.measurements(stand.ref))["measurements"][0]
            assert (row["lab_notices"], row["lab_notices_state"]) == ([], "present")
    asyncio.run(scenario())


@pytest.mark.parametrize("stdout,state", [(b'{"metrics":{"score":1},"private":"do-not-disclose"}', "present"),
                                           (None, "unavailable"), (b'{', "unavailable")])
def test_real_adapter_output_drives_aggregate_without_disclosing_content(tmp_path, stdout, state):
    from test_owner_content import _sample_trace
    _, journal = _sample_trace(tmp_path, stdout=stdout)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    rows[-1]["payload"]["critical_pass"] = None
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    value = {"run_id": "m-r01-a01", "samples": [sample("case")], "complete": True}
    value["samples"][0]["sample_id"] = "00"
    fields = measurements.evaluation_focus_notices(value, tmp_path, journal, terminal=True)
    assert fields == {"lab_notices_state": state, "lab_notices": [
        {"code": "scorer_without_focus_signal", "admitted": 1, "not_completed": 0}] if state == "present" else []}
    assert "do-not-disclose" not in json.dumps(fields)
    if state == "present":
        moved = tmp_path.with_name(tmp_path.name + "-moved")
        tmp_path.rename(moved)
        tmp_path.symlink_to(moved, target_is_directory=True)
        assert measurements.evaluation_focus_notices(value, tmp_path, journal, terminal=True) == {
            "lab_notices": [], "lab_notices_state": "unavailable"}


def test_standalone_report_without_terminal_outcome_has_no_check(monkeypatch):
    from panel_fixtures import load_snapshot, FIXTURES
    from evolution_lab.report import read_evaluation_report, build_report
    from evolution.tree import inspect_journal, project_result
    snapshot = load_snapshot("content")
    root = FIXTURES / "content/run" / snapshot["meta"]["ids"]["evaluation"] / "measurement"
    for report in (read_evaluation_report(root, lab_result=None), build_report(project_result(inspect_journal(root)))):
        assert report.data["lab_notices_state"] == "present"
        assert report.data["lab_notices"] == []


def test_stopped_evaluation_retries_failed_evidence_across_report_paths(tmp_path, monkeypatch):
    from evolution_lab.cli import responses
    async def scenario():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            journal = stand.run / "measurement/events.jsonl"
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            for record in records:
                if record["event"] == "scoring_finished":
                    record["payload"]["critical_pass"] = None
            journal.write_text("".join(json.dumps(record) + "\n" for record in records))
            outcome = stand.scope / "evaluation-outcome.json"
            fields = json.loads(outcome.read_text())
            fields["stopped_reason"] = "cancelled"
            outcome.write_text(json.dumps(fields))
            evidence(monkeypatch, {"c2": core_raw.CoreRawUnavailable("unreadable")})
            first = (await stand.owner.report(stand.ref)).to_payload()
            responses.validate(first, "lab-owner-report/15")
            assert first["report"]["lab_notices_state"] == "unavailable"
            assert first["report"]["lab_notices"] == []
            assert stand.ref.id not in stand.owner._measurements._notices
            markdown = (await stand.owner.report_markdown(stand.ref)).markdown
            assert "проверка сигнала фокуса недоступна" in markdown
            # The failed read did not change any signed file. The next read must retry it.
            evidence(monkeypatch)
            second = (await stand.owner.report(stand.ref)).to_payload()
            responses.validate(second, "lab-owner-report/15")
            assert second["report"]["lab_result"]["stopped_reason"] == "cancelled"
            assert second["report"]["lab_notices"] == [
                {"code": "scorer_without_focus_signal", "admitted": 2, "not_completed": 0}]
            assert second["report"]["lab_notices_state"] == "present"
            row = (await stand.owner.measurements(stand.ref))["measurements"][0]
            assert row["lab_notices"] == second["report"]["lab_notices"]
            assert "метрики фокус не строят" in (await stand.owner.report_markdown(stand.ref)).markdown
    asyncio.run(scenario())


def test_new_notice_reads_only_nofollow_signed_run_inputs(tmp_path, monkeypatch):
    import shutil
    from panel_fixtures import load_snapshot, FIXTURES
    from test_owner_projection_open_audit import opened_paths, outside_inputs, signed_files
    # Load the code before auditing data IO; no source-path exemption is needed.
    from evolution_lab.owner import sample_view  # noqa: F401
    async def scenario():
        async with real_operation(tmp_path, kind="evaluation", finished=True) as stand:
            snapshot = load_snapshot("content")
            capture = FIXTURES / "content/run" / snapshot["meta"]["ids"]["evaluation"] / "measurement"
            shutil.rmtree(stand.run / "measurement")
            shutil.copytree(capture, stand.run / "measurement")
            status = await stand.owner.status(stand.ref)
            signed = signed_files(tmp_path, stand.ref.id, None)
            # Status now verifies the same trace components for unanswered.
            # Audit the notice's cold read, rather than those valid cache hits.
            stand.owner._measurements.drop(stand.ref.id)
            import stat
            signature = readers.run_signature(tmp_path, stand.ref.id, None)
            directories = {stand.run / row[0] for row in signature[0]
                           if len(row) > 2 and row[1] == stat.S_IFDIR}
            with opened_paths(monkeypatch) as records:
                import os
                # Core checks callable identity in supports_dir_fd. The audit's
                # os.open proxy forwards dir_fd to the real supported primitive.
                with monkeypatch.context() as capabilities:
                    capabilities.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os.open})
                    result = await stand.owner._evaluation_notices(status, status.result.to_payload()["core"])
            assert result == {"lab_notices": [], "lab_notices_state": "present"}
            # Core probes the next pack segment. Its absence is signed by the
            # exhaustive nofollow directory enumeration, and creation changes it.
            absent = {lexical for lexical, resolved, _ in records if not lexical.exists()
                      and lexical == resolved and lexical.parent in directories}
            assert not outside_inputs(records, signed | absent)
            assert any(path.suffix == ".pack" for path, _, _ in records)
    asyncio.run(scenario())
