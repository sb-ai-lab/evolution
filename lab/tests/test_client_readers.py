"""H1 status, execution evidence, and report preserve their separate facts."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from evolution.tree import CampaignUnreadable, JournalFault
from evolution.tree.campaign import CampaignFamilyUnsupported

from evolution_lab._client_models import ExecutionSnapshot, Failure
from evolution_lab._client_operation import RunOperation
from evolution_lab.report import Report


def _operation(run_directory: Path, *, launched=True, result=None) -> RunOperation:
    operation = object.__new__(RunOperation)
    operation._run_directory = run_directory
    fields = {
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "state": "active" if result is None else result.state,
        "stage": "running" if result is None else result.stage,
        "launched": launched,
        "exit_code": None if result is None else result.exit_code,
        "stop_requested": False if result is None else result.stop_requested,
        "cleanup_complete": False if result is None else result.cleanup_complete,
        "result": result,
    }
    operation._status_fields = lambda: dict(fields)
    return operation


def _snapshot(*, state="exited", exit_code=0) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        operation_id="00000000-0000-4000-8000-000000000001",
        state=state,
        stage="done",
        launched=True,
        exit_code=exit_code,
        stop_requested=False,
        stop_reason=None,
        stop_effect="not_requested",
        core_force_requested=False,
        cleanup_complete=True,
        failure=None,
        secondary_failures=(),
    )


def _stored_snapshot() -> dict:
    return {
        "schema": "lab-execution-result/1",
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "state": "exited",
        "stage": "done",
        "launched": True,
        "exit_code": 0,
        "stop_requested": False,
        "stop_reason": None,
        "stop_effect": "not_requested",
        "core_force_requested": False,
        "cleanup_complete": True,
        "failure": None,
        "secondary_failures": [],
    }


def _core_report(*, lab_result=None, integrity="durable") -> Report:
    return Report(json.dumps({
        "operation": "campaign",
        "core": {
            "kind": "campaign_result",
            "complete": False,
            "journal_integrity": integrity,
        },
        "lab_result": lab_result,
    }))


def _write_h1(run_directory: Path, value: dict) -> Path:
    logs = run_directory.with_name(run_directory.name + "-lab-logs")
    logs.mkdir()
    path = logs / "h1-result.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _patch_core_campaign_read(monkeypatch) -> None:
    """Reach the real legacy Lab-result read after stable public core projections."""
    import evolution_lab.report as report_module
    from test_report import campaign

    from evolution_lab.owner.core_raw import CampaignRead

    projection = campaign()
    view = SimpleNamespace(snapshot=SimpleNamespace(family_version=14), siblings={}, tries={})
    monkeypatch.setattr(
        report_module,
        "campaign_read",
        lambda root, **kwargs: CampaignRead(view=view, family=14, status=None, result=projection),
    )
    monkeypatch.setattr("evolution_lab.owner.core_raw.campaign_log", lambda root, **kwargs: SimpleNamespace(
        records=[SimpleNamespace(payload={"producing_version": "2.0.0b4"})],
        journal_integrity="complete"))


def test_publish_execution_uses_stored_schema_and_refuses_overwrite(tmp_path):
    from evolution_lab._client_evidence import publish_execution

    logs = tmp_path / "run-lab-logs"
    logs.mkdir()
    publish_execution(logs, _snapshot())

    path = logs / "h1-result.json"
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first == _stored_snapshot()
    assert "persistence" not in first
    assert sorted(item.name for item in logs.iterdir()) == ["h1-result.json"]

    with pytest.raises(FileExistsError):
        publish_execution(logs, _snapshot(state="failed", exit_code=7))
    assert json.loads(path.read_text(encoding="utf-8")) == _stored_snapshot()
    assert sorted(item.name for item in logs.iterdir()) == ["h1-result.json"]


def test_publish_refusal_removes_only_its_own_temp_file(tmp_path):
    from evolution_lab._client_evidence import publish_execution

    logs = tmp_path / "run-lab-logs"
    logs.mkdir()
    foreign = logs / ".h1-result-foreign.tmp"
    foreign.write_text("keep", encoding="utf-8")
    (logs / "h1-result.json").write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        publish_execution(logs, _snapshot())
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert sorted(item.name for item in logs.iterdir()) == [
        ".h1-result-foreign.tmp", "h1-result.json"
    ]


async def test_status_reads_public_campaign_projection_outside_event_loop(
    tmp_path, monkeypatch
):
    from evolution_lab import _client_readers
    from evolution_lab.client import status

    caller_thread = threading.get_ident()
    observed = {}

    class Projection:
        def to_payload(self):
            return {"schema_version": 1, "kind": "campaign_status", "rounds": []}

    def inspect(run_directory, *, verify, secrets):
        observed.update(
            thread=threading.get_ident(),
            run_directory=run_directory,
            verify=verify,
            secrets=secrets,
        )
        return Projection()

    monkeypatch.setattr(_client_readers, "campaign_status", inspect)
    operation = _operation(tmp_path / "run")
    value = await status(operation, secrets=["TOKEN"])

    assert observed == {
        "thread": observed["thread"],
        "run_directory": operation.run_directory,
        "verify": False,
        "secrets": ("TOKEN",),
    }
    assert observed["thread"] != caller_thread
    assert value.public() == {
        "schema": "lab-status/1",
        "operation_id": "00000000-0000-4000-8000-000000000001",
        "state": "active",
        "stage": "running",
        "launched": True,
        "exit_code": None,
        "stop_requested": False,
        "cleanup_complete": False,
        "result": None,
        "campaign": {
            "availability": "available",
            "projection": {
                "schema_version": 1,
                "kind": "campaign_status",
                "rounds": [],
            },
            "failure": None,
        },
    }
    first = value.public()
    first["campaign"]["projection"]["rounds"].append("changed")
    assert value.public()["campaign"]["projection"]["rounds"] == []


@pytest.mark.parametrize("secrets", ["TOKEN", b"TOKEN", ["ok", 1]])
async def test_status_rejects_invalid_secrets_before_core_read(
    tmp_path, monkeypatch, secrets
):
    from evolution_lab import _client_readers
    from evolution_lab.client import ClientError, status

    def unexpected(*args, **kwargs):
        raise AssertionError("inspect_campaign must not run")

    monkeypatch.setattr(_client_readers, "campaign_status", unexpected)
    with pytest.raises(ClientError) as caught:
        await status(_operation(tmp_path / "run"), secrets=secrets)
    assert caught.value.failure == Failure("invalid_argument", "status")


@pytest.mark.parametrize(
    ("launched", "availability"), [(False, "not_started"), (None, "unknown")]
)
async def test_status_does_not_read_campaign_without_confirmed_launch(
    tmp_path, monkeypatch, launched, availability
):
    from evolution_lab import _client_readers
    from evolution_lab.client import status

    monkeypatch.setattr(
        _client_readers,
        "campaign_status",
        lambda *args, **kwargs: pytest.fail("unexpected campaign read"),
    )
    value = await status(_operation(tmp_path / "run", launched=launched))
    assert value.public()["campaign"] == {
        "availability": availability,
        "projection": None,
        "failure": None,
    }


@pytest.mark.parametrize(
    "code",
    [
        "journal_unavailable",
        "journal_busy",
        "journal_limit_exceeded",
        "storage_capacity_exhausted",
    ],
)
async def test_status_maps_every_public_journal_fault_to_unavailable(
    tmp_path, monkeypatch, code
):
    from evolution_lab import _client_readers
    from evolution_lab.client import status

    def fail(*args, **kwargs):
        raise JournalFault(code)

    monkeypatch.setattr(_client_readers, "campaign_status", fail)
    value = await status(_operation(tmp_path / "run"))
    assert value.public()["campaign"] == {
        "availability": "unavailable",
        "projection": None,
        "failure": {"code": "campaign_unavailable", "stage": "status"},
    }


@pytest.mark.parametrize(
    "error",
    [
        CampaignUnreadable("bad"),
        CampaignFamilyUnsupported(6, reader=5),
        ValueError("schema"),
    ],
)
async def test_status_maps_ambiguous_public_reader_refusals_to_unavailable(
    tmp_path, monkeypatch, error
):
    from evolution_lab import _client_readers
    from evolution_lab.client import status

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(_client_readers, "campaign_status", fail)
    value = await status(_operation(tmp_path / "run"))
    assert value.public()["campaign"]["availability"] == "unavailable"
    assert value.public()["campaign"]["failure"] == {
        "code": "campaign_unavailable",
        "stage": "status",
    }


async def test_status_keeps_unexpected_core_failure_unknown(tmp_path, monkeypatch):
    from evolution_lab import _client_readers
    from evolution_lab.client import status

    def fail(*args, **kwargs):
        raise TypeError("private detail")

    monkeypatch.setattr(_client_readers, "campaign_status", fail)
    value = await status(_operation(tmp_path / "run"))
    assert value.public()["campaign"] == {
        "availability": "unknown",
        "projection": None,
        "failure": {"code": "lab_error", "stage": "status"},
    }


def test_report_attaches_valid_h1_snapshot_without_recomputing_core(tmp_path, monkeypatch):
    from evolution_lab import _client_readers
    from evolution_lab.client import report

    run = tmp_path / "run"
    stored = _stored_snapshot()
    _write_h1(run, stored)
    original = _core_report(lab_result=None, integrity="corrupt_suffix")
    monkeypatch.setattr(_client_readers, "read_campaign_report", lambda path: original)

    value = report(run)
    assert value.data["core"] == original.data["core"]
    assert value.data["lab_result"] == stored
    assert value.data["lab_result"]["schema"] == "lab-execution-result/1"
    first = value.data
    first["lab_result"]["secondary_failures"].append({"code": "io_error"})
    assert value.data["lab_result"]["secondary_failures"] == []


def test_report_without_h1_preserves_legacy_reader_result(tmp_path, monkeypatch):
    from evolution_lab import _client_readers
    from evolution_lab.client import report

    original = _core_report(lab_result={"version": "lab-process-result/1"})
    monkeypatch.setattr(_client_readers, "read_campaign_report", lambda path: original)
    assert report(tmp_path / "run").data == original.data


@pytest.mark.parametrize("legacy_kind", ["malformed", "symlink"])
def test_report_maps_actual_legacy_process_result_format_refusal_to_invalid(
    tmp_path, monkeypatch, legacy_kind
):
    from evolution_lab.client import ReportUnavailable, report

    _patch_core_campaign_read(monkeypatch)
    run = tmp_path / ("run-" + legacy_kind)
    logs = run.with_name(run.name + "-lab-logs")
    logs.mkdir()
    legacy = logs / "process-result.json"
    if legacy_kind == "malformed":
        legacy.write_text("{", encoding="utf-8")
    else:
        target = tmp_path / "legacy-target.json"
        target.write_text("{}", encoding="utf-8")
        legacy.symlink_to(target)

    with pytest.raises(ReportUnavailable) as caught:
        report(run)
    assert caught.value.reason == "invalid"
    assert isinstance(caught.value.__cause__, ValueError)


def test_report_maps_actual_legacy_process_result_read_error_to_io_error(
    tmp_path, monkeypatch
):
    from evolution_lab.client import ReportUnavailable, report

    _patch_core_campaign_read(monkeypatch)
    run = tmp_path / "run-io"
    logs = run.with_name(run.name + "-lab-logs")
    logs.mkdir()
    (logs / "process-result.json").mkdir()

    with pytest.raises(ReportUnavailable) as caught:
        report(run)
    assert caught.value.reason == "io_error"
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize("legacy_name", ["process-result.json", "supervisor-result.json"])
def test_report_rejects_competing_h1_and_legacy_artifacts(
    tmp_path, monkeypatch, legacy_name
):
    from evolution_lab import _client_readers
    from evolution_lab.client import ReportUnavailable, report

    run = tmp_path / "run"
    h1 = _write_h1(run, _stored_snapshot())
    (h1.parent / legacy_name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        _client_readers,
        "read_campaign_report",
        lambda path: pytest.fail("invalid evidence must not fall back"),
    )
    with pytest.raises(ReportUnavailable) as caught:
        report(run)
    assert caught.value.reason == "invalid"


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(schema="lab-run-result/1"),
        lambda value: value.update(state="successful"),
        lambda value: value.update(state=[]),
        lambda value: value.update(persistence={"state": "saved", "failure": None}),
        lambda value: value.update(exit_code=True),
    ],
)
def test_report_rejects_invalid_h1_shape_without_fallback(tmp_path, monkeypatch, change):
    from evolution_lab import _client_readers
    from evolution_lab.client import ReportUnavailable, report

    run = tmp_path / "run"
    stored = _stored_snapshot()
    change(stored)
    _write_h1(run, stored)
    monkeypatch.setattr(
        _client_readers,
        "read_campaign_report",
        lambda path: pytest.fail("invalid evidence must not fall back"),
    )
    with pytest.raises(ReportUnavailable) as caught:
        report(run)
    assert caught.value.reason == "invalid"


def test_report_rejects_symlink_and_excessive_h1_evidence(tmp_path, monkeypatch):
    from evolution_lab import _client_readers
    from evolution_lab.client import ReportUnavailable, report

    monkeypatch.setattr(
        _client_readers,
        "read_campaign_report",
        lambda path: pytest.fail("invalid evidence must not fall back"),
    )
    for suffix, make in (
        ("symlink", lambda path: path.symlink_to(tmp_path / "target.json")),
        ("large", lambda path: path.write_bytes(b" " * 65537)),
    ):
        run = tmp_path / ("run-" + suffix)
        logs = run.with_name(run.name + "-lab-logs")
        logs.mkdir()
        if suffix == "symlink":
            (tmp_path / "target.json").write_text(json.dumps(_stored_snapshot()))
        make(logs / "h1-result.json")
        with pytest.raises(ReportUnavailable) as caught:
            report(run)
        assert caught.value.reason == "invalid"


def test_report_distinguishes_lab_io_from_ambiguous_core_error(tmp_path, monkeypatch):
    from evolution_lab import _client_readers
    from evolution_lab.client import ReportUnavailable, report

    run = tmp_path / "run"
    with monkeypatch.context() as scoped:
        scoped.setattr(
            _client_readers,
            "read_execution",
            lambda path: (_ for _ in ()).throw(OSError("Lab read")),
        )
        with pytest.raises(ReportUnavailable) as caught:
            report(run)
        assert caught.value.reason == "io_error"

    logs = run.with_name(run.name + "-lab-logs")
    logs.mkdir()
    (logs / "h1-result.json").mkdir()
    with pytest.raises(ReportUnavailable) as caught:
        report(run)
    assert caught.value.reason == "invalid"

    (logs / "h1-result.json").rmdir()
    monkeypatch.setattr(
        _client_readers,
        "read_campaign_report",
        lambda path: (_ for _ in ()).throw(OSError("core read")),
    )
    with pytest.raises(ReportUnavailable) as caught:
        report(run)
    assert caught.value.reason == "unknown"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CampaignUnreadable("old family"), "unavailable"),
        (CampaignFamilyUnsupported(6, reader=5), "unavailable"),
        (JournalFault("journal_unavailable"), "unavailable"),
        (JournalFault("journal_busy"), "unavailable"),
        (JournalFault("journal_limit_exceeded"), "invalid"),
        (ValueError("campaign journal has no validated identity"), "unknown"),
        (TypeError("unexpected"), "unknown"),
    ],
)
def test_report_maps_public_core_reader_failures_without_message_parsing(
    tmp_path, monkeypatch, error, reason
):
    from evolution_lab import _client_readers
    from evolution_lab.client import ReportUnavailable, report

    def fail(path):
        raise error

    monkeypatch.setattr(_client_readers, "read_campaign_report", fail)
    with pytest.raises(ReportUnavailable) as caught:
        report(tmp_path / "run")
    assert caught.value.reason == reason


def test_campaign_report_refuses_unsupported_family_before_building(tmp_path, monkeypatch):
    import evolution_lab.report as report_module
    from evolution_lab.owner.core_raw import LEGACY_FAMILIES
    families = []
    def refused(*a, **k):
        families.append(k.get("families"))
        raise CampaignFamilyUnsupported(13, reader=14)
    monkeypatch.setattr(report_module, "campaign_read", refused)
    monkeypatch.setattr(report_module, "build_report", lambda *a, **k: pytest.fail("report after refusal"))
    with pytest.raises(CampaignFamilyUnsupported):
        report_module.read_campaign_report(tmp_path)
    assert families == [LEGACY_FAMILIES]


async def test_h1_wrong_pin_remains_unknown(tmp_path, monkeypatch):
    from evolution_lab.owner import core_raw
    from evolution_lab.client import status, report, ReportUnavailable
    monkeypatch.setattr(core_raw, '_installed_core', lambda: 'wrong')
    value = await status(_operation(tmp_path))
    assert value.campaign_availability == 'unknown'
    assert value.campaign_failure == Failure('lab_error', 'status')
    with pytest.raises(ReportUnavailable) as raised:
        report(tmp_path)
    assert raised.value.reason == 'unknown'


@pytest.mark.parametrize('content', ['family', 'empty', 'corrupt'])
async def test_h1_actual_shared_reader_refusal_is_unavailable(tmp_path, content):
    import shutil
    from evolution_lab.client import status, report, ReportUnavailable
    if content == 'family':
        shutil.copyfile(Path(__file__).parent / 'fixtures/campaign-family-refusal/campaign.jsonl',
                        tmp_path / 'campaign.jsonl')
    else:
        (tmp_path / 'campaign.jsonl').write_text('' if content == 'empty' else 'broken\n')
    value = await status(_operation(tmp_path))
    assert value.campaign_availability == 'unavailable'
    assert value.campaign_projection is None
    assert value.campaign_failure == Failure('campaign_unavailable', 'status')
    with pytest.raises(ReportUnavailable) as raised:
        report(tmp_path)
    assert raised.value.reason == 'unavailable'
