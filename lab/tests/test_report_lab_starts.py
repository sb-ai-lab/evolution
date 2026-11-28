"""Report evidence of every Lab start of one campaign (ADR-0010)."""
import json

import pytest

from evolution_lab.report import _campaign_lab_results


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_absent_logs_have_no_result_and_no_starts(tmp_path):
    assert _campaign_lab_results(tmp_path / "run-lab-logs") == (None, [])


def test_symlinked_logs_are_an_invalid_result(tmp_path):
    real = tmp_path / "elsewhere"
    _write(real / "process-result.json", {"version": "lab-process-result/1", "exit_code": 0})
    logs = tmp_path / "run-lab-logs"
    logs.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="invalid Lab result file"):
        _campaign_lab_results(logs)


def test_single_start_keeps_the_legacy_result(tmp_path):
    logs = tmp_path / "run-lab-logs"
    _write(logs / "process-result.json", {"version": "lab-process-result/1", "exit_code": 0})
    _write(logs / "supervisor-result.json", {"core_exit_code": 0, "caller_lost": False})
    result, starts = _campaign_lab_results(logs)
    assert result == {"version": "lab-process-result/1", "exit_code": 0}
    assert starts == [{"start": 1, "resume": False, "exit_code": 0, "core_refused": False,
                       "caller_lost": False, "process_result": True}]


def test_latest_start_with_a_result_wins_and_history_is_ordered(tmp_path):
    logs = tmp_path / "run-lab-logs"
    _write(logs / "supervisor-result.json", {"core_exit_code": None, "caller_lost": True})
    _write(logs / "start-2/supervisor-result.json", {"core_exit_code": -9, "caller_lost": True})
    _write(logs / "start-10/process-result.json", {"version": "lab-process-result/2", "exit_code": 0})
    _write(logs / "start-10/supervisor-result.json", {"core_exit_code": 0, "caller_lost": False})
    (logs / "start-1").mkdir()
    (logs / "start-x").mkdir()
    result, starts = _campaign_lab_results(logs)
    assert result["exit_code"] == 0
    assert [row["start"] for row in starts] == [1, 2, 10]
    assert starts[0] == {"start": 1, "resume": False, "exit_code": None, "core_refused": None,
                         "caller_lost": True, "process_result": False}
    assert starts[1]["exit_code"] == -9 and starts[1]["core_refused"] is False and starts[1]["resume"] is True


def test_invalid_start_result_has_no_fallback(tmp_path):
    logs = tmp_path / "run-lab-logs"
    _write(logs / "process-result.json", {"version": "lab-process-result/1", "exit_code": 0})
    (logs / "start-2").mkdir(parents=True)
    (logs / "start-2/process-result.json").write_text("[1]")
    with pytest.raises(ValueError):
        _campaign_lab_results(logs)


def test_start_entry_that_is_a_file_is_invalid_not_skipped(tmp_path):
    logs = tmp_path / "run-lab-logs"
    logs.mkdir(parents=True)
    (logs / "start-2").write_text("not a directory")
    with pytest.raises(ValueError, match="invalid Lab result file"):
        _campaign_lab_results(logs)


def test_start_entry_that_is_a_symlink_to_a_valid_dir_is_invalid(tmp_path):
    logs = tmp_path / "run-lab-logs"
    real = tmp_path / "elsewhere-start-2"
    _write(real / "process-result.json", {"version": "lab-process-result/2", "exit_code": 0})
    logs.mkdir(parents=True)
    (logs / "start-2").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="invalid Lab result file"):
        _campaign_lab_results(logs)


def test_non_matching_name_file_is_still_ignored(tmp_path):
    logs = tmp_path / "run-lab-logs"
    _write(logs / "process-result.json", {"version": "lab-process-result/1", "exit_code": 0})
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "start-x").write_text("not a start directory, name does not match")
    result, starts = _campaign_lab_results(logs)
    assert result == {"version": "lab-process-result/1", "exit_code": 0}
    assert [row["start"] for row in starts] == [1]


def test_caller_lost_prefers_process_result_when_present(tmp_path):
    logs = tmp_path / "run-lab-logs"
    _write(logs / "process-result.json", {"version": "lab-process-result/1", "exit_code": 0})
    _write(logs / "start-2/process-result.json",
           {"version": "lab-process-result/2", "exit_code": 1, "caller_lost": True})
    result, starts = _campaign_lab_results(logs)
    assert result["exit_code"] == 1
    row = next(row for row in starts if row["start"] == 2)
    assert row["caller_lost"] is True and row["process_result"] is True


def test_caller_lost_falls_back_to_supervisor_when_no_process_result_key(tmp_path):
    logs = tmp_path / "run-lab-logs"
    _write(logs / "process-result.json", {"version": "lab-process-result/1", "exit_code": 0})
    _write(logs / "supervisor-result.json", {"core_exit_code": 0, "caller_lost": False})
    result, starts = _campaign_lab_results(logs)
    row = next(row for row in starts if row["start"] == 1)
    assert row["caller_lost"] is False and row["process_result"] is True
