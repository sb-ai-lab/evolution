"""Real dev57/b9 content/family14/family15 answers; retained dev55/b8 defaults and dev52/b6 suspension-fault."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "panel"
SNAPSHOTS = ("empty", "running", "suspended-target", "suspended-role", "final", "details", "content",
             "family14", "suspension-fault", "family15")
#: Native dev57/b9 producing snapshots (not current-pair captures).
CURRENT = ("content", "family14", "family15")
#: Retained defaults keep their real dev55/b8 producing metadata and use the adapter.
LEGACY = ("empty", "running", "suspended-target", "suspended-role", "final", "details")


def canonical(path_and_query: str) -> str:
    path, _, query = path_and_query.partition("?")
    path = unquote(path)
    if not query:
        return path
    return path + "?" + urlencode(sorted(parse_qsl(query, keep_blank_values=True)), safe=":@")


def _historical_form(body: dict) -> dict:
    """Adapt captured /8-/9 views and /5-/6 reports for Python tests, preserving producing bytes.

    This is the sole test-only place that
    derives the explicit layers from a historical comparison; runtime projections always read Core's own fields.
    Other schemas, including already-current forms, are unchanged.
    """
    schema = body.get("schema")
    if schema not in ("lab-campaign-view/8", "lab-campaign-view/9", "lab-owner-report/5", "lab-owner-report/6"):
        return body
    result = deepcopy(body)
    if schema in ("lab-campaign-view/8", "lab-campaign-view/9"):
        result["schema"] = "lab-campaign-view/10"
        block = result.get("independent") if result.get("family") == 15 else None
    else:
        result["schema"] = "lab-owner-report/7"
        block = (result.get("report") or {}).get("lab_independent")
    if isinstance(block, dict):
        if isinstance(block.get("settings"), dict):
            block["settings"].setdefault("bound_fallback", None)

        def explanation(value):
            if isinstance(value, dict):
                value.setdefault("bound_fallback", None)
                for scope in value.get("scopes") or ():
                    scope.setdefault("fallback", None)

        for entry in (block.get("attainability") or {}).get("preflight") or ():
            explanation(entry.get("explanation"))
        for attempt in block.get("attempts") or ():
            if schema in ("lab-campaign-view/8", "lab-owner-report/5"):
                comparison = attempt["comparison"]
                if comparison is not None:
                    comparison["reference_measurement_id"] = None
                global_ = comparison is not None and comparison["deciding_layer"] == "global"
                attempt["gate_comparison"] = None if global_ else deepcopy(comparison)
                attempt["global_comparison"] = deepcopy(comparison) if global_ else None
            # Historical captures have no authenticated binding-rule DTO. Never
            # infer a binding from a comparison (including in the adapter).
            attempt["gate_binding"] = attempt["global_binding"] = None
            explanation(attempt.get("preflight"))
            for name in ("comparison", "gate_comparison", "global_comparison"):
                comparison = attempt.get(name)
                if comparison is None:
                    continue
                for key in ("method", "unit", "calibration", "caveats", "bound_fallback"):
                    comparison.setdefault(key, None)
                for scope in comparison.get("scopes") or ():
                    for key in ("fallback", "lower", "exact", "pairing"):
                        scope.setdefault(key, None)
    return result


def _measurement_forms(body: dict) -> dict:
    """Dev69 compatibility only: old producing captures have no scorer check evidence."""
    schema = body.get("schema", "")
    measurement = schema in {"lab-measurement/2", "lab-measurement-list/2"}
    report = body.get("report") if schema.startswith("lab-owner-report/") else None
    old_report = isinstance(report, dict) and report.get("operation") == "evaluation" and "lab_notices_state" not in report
    if not measurement and not old_report:
        return body
    result = deepcopy(body)
    if measurement:
        result["schema"] = schema.replace("/2", "/3")
    rows = ([result["measurement"]] if schema == "lab-measurement/2" else
            result.get("measurements", []) if schema == "lab-measurement-list/2" else [])
    for row in rows:
        if isinstance(row, dict):
            row.setdefault("lab_notices", [])
            row.setdefault("lab_notices_state", "unavailable" if row.get("role") == "evaluation" else "present")
    if old_report:
        result["report"].update(lab_notices=[], lab_notices_state="unavailable")
    return result


def current_form(body: dict) -> dict:
    """Synthetic current forms only; preserve raw captures and their producing metadata.

    Human-approved dev61 adaptation: old checks were absent, so the copied family15
    form records present/null. Dev62 adds unavailable Owner probe evidence because the
    producing captures contain no Owner probe records. This is compatibility input, not a new run.
    """
    historical = _measurement_forms(_historical_form(body))
    schema = historical.get("schema")
    if schema in {"lab-campaign-view/12", "lab-campaign-view/13", "lab-owner-report/10", "lab-owner-report/11"}:
        result = deepcopy(historical)
        result["schema"] = "lab-campaign-view/15" if schema.startswith("lab-campaign-view/") else "lab-owner-report/15"
        if result["schema"] == "lab-owner-report/15":
            for start in result.get("starts") or ():
                start.update(initiated_by="human", auto_resume_attempt=None)
        if result["schema"] == "lab-campaign-view/15":
            result["lab_notices_state"] = "unavailable"
        return result
    if schema not in {"lab-campaign-view/10", "lab-owner-report/7", "lab-owner-report/8"}:
        return historical
    result = deepcopy(historical)
    result.update(reflector_probe=None, reflector_probe_state="unavailable", reflector_probe_reason="unreadable")
    if schema == "lab-campaign-view/10":
        result["schema"] = "lab-campaign-view/15"
        result["lab_notices_state"] = "unavailable"
        if result.get("family") == 15:
            result.update(checks=None, checks_state="present", checks_reason=None)
    else:
        result["schema"] = "lab-owner-report/15"
        # Historical /7 starts predate Core terminal facts; unknown stays null.
        result.setdefault("core_terminal", None)
        for start in result.get("starts") or ():
            start.setdefault("core_terminal", None)
            start.update(initiated_by="human", auto_resume_attempt=None)
        report = result.get("report")
        if isinstance(report, dict) and report.get("operation") == "campaign":
            report.update(reflector_probe=None, reflector_probe_state="unavailable", reflector_probe_reason="unreadable")
        if isinstance(report, dict) and "lab_independent" in report:
            report.update(lab_checks=None, lab_checks_state="present", lab_checks_reason=None)
    return result


def _current_payload(value):
    """An SSE payload may embed a view below its own event envelope."""
    if isinstance(value, dict):
        return {key: _current_payload(item) for key, item in current_form(value).items()}
    if isinstance(value, list):
        return [_current_payload(item) for item in value]
    return value


def _current_event(frame: str) -> str:
    lines = frame.splitlines(keepends=True)
    data_lines = [line for line in lines if line.startswith("data:")]
    if not data_lines:
        return frame
    payload = json.loads("\n".join(line[5:].lstrip(" ").rstrip("\r\n") for line in data_lines))
    adapted = _current_payload(payload)
    if adapted == payload:
        return frame
    replacement = "data: " + json.dumps(adapted, ensure_ascii=False) + "\n"
    first = next(i for i, line in enumerate(lines) if line.startswith("data:"))
    return "".join(replacement if i == first else line for i, line in enumerate(lines)
                   if i == first or not line.startswith("data:"))


@lru_cache(maxsize=None)
def load_snapshot(name: str) -> dict:
    root = FIXTURES / name
    data = {part: json.loads((root / f"{part}.json").read_text(encoding="utf-8"))
            for part in ("routes", "events", "meta")}
    for route in data["routes"].values():
        if isinstance(route.get("body"), dict):
            route["body"] = current_form(route["body"])
    data["events"] = {operation: [_current_event(frame) for frame in frames]
                      for operation, frames in data["events"].items()}
    return data


def mock_upstream(name: str) -> httpx.MockTransport:
    data = load_snapshot(name)

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode("latin-1")
        path = unquote(raw.split("?", 1)[0])
        query = raw.split("?", 1)[1] if "?" in raw else ""
        if path.startswith("/v1/operations/") and path.endswith("/events"):
            op = path.split("/")[3]
            if op in data["events"]:
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content="".join(data["events"][op]).encode())
        entry = data["routes"].get(canonical(path + (f"?{query}" if query else "")))
        if entry is None:
            return httpx.Response(404, json={"error": {"code": "not_found", "stage": "fixture", "message": path}})
        return httpx.Response(entry["status"], json=entry["body"])

    return httpx.MockTransport(handler)
