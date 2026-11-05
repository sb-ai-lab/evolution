"""Missing frozen baseline evidence faults cleanly and remains recoverable."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evolution.tree.campaign import _Campaign
from evolution.tree.execution.scorer_traces import ScorerTraceReader
from evolution.tree.process_log import capture
from evolution.tree.roles.grouping.reflect_grouping import GroupingAdmissionError
from tests.campaign_drivers import _measurement_events, _records
from tests.campaign_fakes import Crash
from tests.scorer_trace_packs import write_directory_trace
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


async def _interrupted_editor(tmp_path, monkeypatch):
    original = _Campaign.hook

    def hook(self, name):
        if name == "after_editor_started":
            for workspace in self.workspaces.values():
                skill = Path(workspace) / "skills" / "SKILL.md"
                skill.chmod(0o644)
                skill.write_bytes(b"partial\n")
            raise Crash("power cut")
        original(self, name)

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", hook)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch)
    return tmp_path / "run"


def _writable_tree(root):
    for path in (root, *root.rglob("*")):
        path.chmod(path.stat().st_mode | 0o200)


async def _classified_fault(tmp_path, monkeypatch, missing, baseline):
    run = tmp_path / "run"
    with capture("evo tree evolve", prefix="evolve") as log:
        log.materialize(run)
        _, fault = await family14_run(
            tmp_path,
            monkeypatch,
            expected_fault=("persistence_failed", "measurement_journal"),
        )
    assert fault.code == "journal_unavailable"
    assert fault.measurement == baseline
    assert fault.detail is not None
    if missing == "measurements":
        assert f"measurements/{baseline}/events.jsonl" in fault.detail
    else:
        assert "scorer-traces/" in fault.detail
        assert baseline in fault.detail
        assert ".pack" not in fault.detail
    assert fault.detail.endswith("restore it from a trusted copy, or start a new or linked run")
    assert payloads(run, "campaign_finished") == []
    logs = list((run / "logs").glob("*"))
    assert logs
    assert all("Traceback" not in path.read_text() for path in logs)
    return fault


@pytest.mark.parametrize("missing", ["measurements", "scorer-traces"])
async def test_resume_mid_editor_without_baseline_evidence_ends_with_a_classified_fault(
    tmp_path, monkeypatch, missing
):
    run = await _interrupted_editor(tmp_path, monkeypatch)
    baseline = payloads(run, "baseline_bound")[0]["measurement_id"]
    _writable_tree(run / missing)
    shutil.rmtree(run / missing)
    await _classified_fault(tmp_path, monkeypatch, missing, baseline)


@pytest.mark.parametrize("missing", ["measurements", "scorer-traces"])
async def test_restoring_the_evidence_lets_the_same_run_finish(tmp_path, monkeypatch, missing):
    run = await _interrupted_editor(tmp_path, monkeypatch)
    baseline = payloads(run, "baseline_bound")[0]["measurement_id"]
    backup = tmp_path / "saved-evidence"
    shutil.copytree(run / missing, backup)
    _writable_tree(run / missing)
    shutil.rmtree(run / missing)
    await _classified_fault(tmp_path, monkeypatch, missing, baseline)
    # Resume may have recreated empty journal heads; replace them with the copy.
    if (run / missing).exists():
        _writable_tree(run / missing)
        shutil.rmtree(run / missing)
    shutil.copytree(backup, run / missing)
    run, snapshot = await family14_run(tmp_path, monkeypatch)
    assert snapshot.finished["outcome"] in {"rounds_exhausted", "completed"}
    assert audit(run)["mismatches"] == []


async def test_a_truncated_baseline_journal_gives_the_same_fault(tmp_path, monkeypatch):
    run = await _interrupted_editor(tmp_path, monkeypatch)
    baseline = payloads(run, "baseline_bound")[0]["measurement_id"]
    journal = _measurement_events(run, baseline)
    journal.chmod(journal.stat().st_mode | 0o200)
    lines = journal.read_bytes().splitlines(keepends=True)
    journal.write_bytes(b"".join(lines[:-1]))
    await _classified_fault(tmp_path, monkeypatch, "measurements", baseline)


async def test_missing_directory_layout_traces_name_the_trace_root_and_baseline(
    tmp_path, monkeypatch
):
    run = await _interrupted_editor(tmp_path, monkeypatch)
    baseline = payloads(run, "baseline_bound")[0]["measurement_id"]
    started = [
        record["payload"]["score_request_id"]
        for record in _records(_measurement_events(run, baseline))
        if record["event"] == "scoring_started"
    ]
    with ScorerTraceReader(run) as reader:
        records = [reader.read(request_id, measurement_id=baseline) for request_id in started]
    for record in records:
        assert record is not None
        write_directory_trace(
            run / "scorer-traces",
            record.score_request_id,
            request=record.request,
            stdout=record.stdout,
            stderr=record.stderr,
        )
    (run / "scorer-traces" / f"{baseline}.pack").unlink()
    with ScorerTraceReader(run) as reader:
        for request_id in started:
            restored = reader.read(request_id, measurement_id=baseline)
            assert restored is not None and restored.layout == "directory"
    shutil.rmtree(run / "scorer-traces" / "scorer_traces")
    await _classified_fault(tmp_path, monkeypatch, "scorer-traces", baseline)


async def _admission_fault(tmp_path, monkeypatch, raised):
    """Replay a stored grouping whose admission raises `raised`; return the fault."""
    run = await _interrupted_editor(tmp_path, monkeypatch)
    baseline = payloads(run, "baseline_bound")[0]["measurement_id"]

    def refuse(*args, **kwargs):
        raise raised

    with monkeypatch.context() as patch:
        patch.setattr("evolution.tree.campaign.admit_grouping_result", refuse)
        _, fault = await family14_run(
            tmp_path,
            patch,
            expected_fault=("persistence_failed", "measurement_journal"),
        )
    assert fault.code == "journal_unavailable"
    assert fault.measurement == baseline
    assert fault.detail is not None
    return fault


async def test_a_registry_dependent_admission_fault_names_scorer_traces(tmp_path, monkeypatch):
    fault = await _admission_fault(tmp_path, monkeypatch, GroupingAdmissionError("registry_digest"))
    assert "registry_digest" in fault.detail
    assert "scorer-traces/" in fault.detail


@pytest.mark.parametrize(
    "code", ["unsupported_grouping_version", "result_size", "invalid_descriptions"]
)
async def test_a_result_shape_admission_fault_does_not_name_scorer_traces(
    tmp_path, monkeypatch, code
):
    fault = await _admission_fault(tmp_path, monkeypatch, GroupingAdmissionError(code))
    assert code in fault.detail
    assert "scorer-traces/" not in fault.detail
    assert "stored grouping result is invalid" in fault.detail


async def test_invalid_policy_identity_is_classified_not_raised(tmp_path, monkeypatch):
    fault = await _admission_fault(tmp_path, monkeypatch, ValueError("secret text"))
    assert "invalid_policy_identity" in fault.detail
    assert "secret text" not in fault.detail
    assert "scorer-traces/" not in fault.detail
    assert "stored grouping result is invalid" in fault.detail


async def test_a_stray_value_error_elsewhere_in_the_replay_is_not_a_grouping_fault(
    tmp_path, monkeypatch
):
    run = await _interrupted_editor(tmp_path, monkeypatch)

    def broken(self, round_number):
        raise ValueError("unrelated programming error")

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "adaptive_inputs_for", broken)
        with pytest.raises(ValueError, match="unrelated programming error"):
            await family14_run(tmp_path, patch)
    assert run.exists()
