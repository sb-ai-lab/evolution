"""Public H1 status/report adapters over core projections and Lab evidence."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evolution.tree import CampaignUnreadable, JournalFault
from evolution.tree.campaign import CampaignFamilyUnsupported

from evolution_lab.owner.core_raw import campaign_status

from evolution_lab._client_evidence import EvidenceInvalid, read_execution
from evolution_lab._client_models import ClientError, Failure, StatusSnapshot
from evolution_lab._client_operation import RunOperation
from evolution_lab.report import (
    Report, _LabResultIOError, _LabResultInvalid, _with_lab_result,
    read_campaign_report,
)


class ReportUnavailable(RuntimeError):
    """A closed reason that the requested authorized report cannot be returned."""

    def __init__(self, reason: str) -> None:
        if reason not in {"unavailable", "invalid", "io_error", "unknown"}:
            raise ValueError("invalid report unavailability reason")
        self.reason = reason
        super().__init__(reason)


def _status_error() -> ClientError:
    return ClientError(Failure("invalid_argument", "status"))


def _secrets(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _status_error()
    result = tuple(value)
    if any(not isinstance(item, str) for item in result):
        raise _status_error()
    return result


def _inspect(run_directory: Path, secrets: tuple[str, ...]) -> dict[str, Any]:
    return campaign_status(
        run_directory, verify=False, secrets=secrets
    ).to_payload()


async def status(operation: RunOperation, *, secrets: Sequence[str] = ()) -> StatusSnapshot:
    """Combine a fresh process snapshot with a read-only public campaign view."""
    if not isinstance(operation, RunOperation):
        raise _status_error()
    protected = _secrets(secrets)
    fields = operation._status_fields()
    launched = fields["launched"]
    if launched is False:
        availability, projection, failure = "not_started", None, None
    elif launched is None:
        availability, projection, failure = "unknown", None, None
    else:
        try:
            projection = await asyncio.to_thread(
                _inspect, operation.run_directory, protected
            )
            availability, failure = "available", None
        except (JournalFault, CampaignUnreadable, ValueError):
            availability, projection = "unavailable", None
            failure = Failure("campaign_unavailable", "status")
        except Exception:
            availability, projection = "unknown", None
            failure = Failure("lab_error", "status")
    return StatusSnapshot(
        **fields,
        campaign_availability=availability,
        campaign_projection=projection,
        campaign_failure=failure,
    )


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _h1_result(run_directory: Path) -> dict[str, Any] | None:
    logs = run_directory.with_name(run_directory.name + "-lab-logs")
    value = read_execution(logs)
    if value is not None and any(
        _present(logs / name)
        for name in ("process-result.json", "supervisor-result.json")
    ):
        raise EvidenceInvalid()
    return value


def _core_report_reason(exc: BaseException) -> str:
    if isinstance(exc, _LabResultIOError):
        return "io_error"
    if isinstance(exc, _LabResultInvalid):
        return "invalid"
    if isinstance(exc, CampaignUnreadable):
        return "unavailable"
    if isinstance(exc, CampaignFamilyUnsupported):
        return "unavailable"
    if isinstance(exc, JournalFault):
        if exc.code in {"journal_unavailable", "journal_busy", "storage_capacity_exhausted"}:
            return "unavailable"
        if exc.code == "journal_limit_exceeded":
            return "invalid"
    return "unknown"


def report(run_directory: Path) -> Report:
    """Read core's report, adding H1 evidence without any campaign recomputation."""
    try:
        root = Path(run_directory)
        lab_result = _h1_result(root)
    except EvidenceInvalid as exc:
        raise ReportUnavailable("invalid") from exc
    except OSError as exc:
        raise ReportUnavailable("io_error") from exc
    try:
        value = read_campaign_report(root)
    except Exception as exc:
        raise ReportUnavailable(_core_report_reason(exc)) from exc
    if lab_result is None:
        return value
    return _with_lab_result(value, lab_result)
