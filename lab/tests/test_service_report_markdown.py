"""report.md over the service (spec 2026-10-14 report-markdown §3, §8): the route, its type and refusals."""
import httpx
import pytest

from evolution_lab.owner import ReportMarkdownResult
from evolution_lab.service.app import create_app

TEXT = "# Кампания\n\nRun: r1\n"


class Runtime:
    service_environment = None

    def __init__(self, value):
        self.value, self.calls = value, []

    async def call_owner(self, method, *args, **kwargs):
        self.calls.append((method, args))
        return self.value


async def _get(value, *, headers=None, path="/v1/operations/op-1/report.md"):
    runtime = Runtime(value)
    transport = httpx.ASGITransport(app=create_app(runtime, event_handler=lambda *_: None))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        return runtime, await client.get(path, headers=headers or {})


async def test_an_available_report_is_a_utf8_markdown_attachment():
    runtime, response = await _get(ReportMarkdownResult("op-1", "available", TEXT, None))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="report-op-1.md"'
    assert response.headers["cache-control"] == "no-store"
    assert response.content == TEXT.encode("utf-8")
    assert [(method, args[0].kind, args[0].id) for method, args in runtime.calls] == [
        ("report_markdown", "operation", "op-1")]


async def test_deleted_results_answer_results_deleted():
    _, response = await _get(ReportMarkdownResult("op-1", "unavailable", None, "results_deleted"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "results_deleted"


@pytest.mark.parametrize("reason", ["io_error", "unavailable"])
async def test_other_reasons_answer_report_unavailable_naming_the_reason(reason):
    _, response = await _get(ReportMarkdownResult("op-1", "unavailable", None, reason))
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "report_unavailable", "stage": "transport",
                                         "message": f"The operation report is unavailable. Owner reason: {reason}."}}


async def test_a_mutation_key_is_refused_before_the_owner():
    runtime, response = await _get(ReportMarkdownResult("op-1", "available", TEXT, None),
                                   headers={"lab-request-id": "r" * 24})
    assert response.status_code == 400 and runtime.calls == []


async def test_an_invalid_operation_id_is_refused_before_the_owner():
    runtime, response = await _get(ReportMarkdownResult("op-1", "available", TEXT, None),
                                   path="/v1/operations/bad%20id/report.md")
    assert response.status_code == 400 and runtime.calls == []


def test_the_runtime_admits_report_markdown():
    from evolution_lab.service.runtime import _OWNER_METHODS
    assert "report_markdown" in _OWNER_METHODS
