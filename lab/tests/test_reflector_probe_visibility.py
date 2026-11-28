"""Owner facts stay independently visible; missing records never mean no probe."""
import json
from copy import deepcopy

import pytest

from evolution_lab.cli import responses
from evolution_lab.cli.output import _campaign_lines, _report_lines
from evolution_lab.owner.campaign_views import campaign_view
from evolution_lab.owner.models import ReportResult
from evolution_lab.report import Report, build_report
from test_owner_campaign_views import _CORE, _PLAN, _DETAILS, _core15
from test_report import campaign, _result15
from test_owner_independent_view import BLOCK

PROBE = {"launched_by": "lab", "state": "exhausted", "restarts": 2, "last_exit": 3}
ABSENT = {"launched_by": None, "state": "not_declared", "restarts": 0, "last_exit": None}


def view(core, probe):
    return campaign_view("op1", core, _PLAN, None, [], frozenset(), [], _DETAILS,
                         reflector_probe=probe)


@pytest.mark.parametrize("core", [_CORE, _core15()])
@pytest.mark.parametrize("probe", [PROBE, ABSENT, None])
def test_view_has_independent_owner_evidence(core, probe):
    core = deepcopy(core)
    if core.get("family_version") == 15:
        core["independent"] = None
    result = view(core, probe)
    assert result["schema"] == "lab-campaign-view/15"
    assert result["reflector_probe"] == probe
    assert result["reflector_probe_state"] == ("present" if probe else "unavailable")
    assert result["reflector_probe_reason"] == (None if probe else "unreadable")
    responses.validate(result, "lab-campaign-view/15")
    assert _report_lines(result) == []  # generic CLI output also invokes this renderer
    lines = _campaign_lines(result)
    if probe == PROBE:
        assert "reflector probe: launched_by=lab state=exhausted restarts=2" in lines
    elif probe == ABSENT:
        assert not any("reflector probe" in line for line in lines)
    else:
        assert "reflector probe: unavailable (unreadable)" in lines


@pytest.mark.parametrize("patch", [{"restarts": True}, {"restarts": -1}, {"last_exit": False},
    {"launched_by": None}, {"state": "future"}, {"extra": 1}, {"last_exit": "3"}])
def test_invalid_owner_evidence_is_unavailable_and_client_refuses_corruption(patch):
    bad = {**PROBE, **patch}
    result = view(_CORE, bad)
    assert result["reflector_probe"] is None
    assert result["reflector_probe_state"] == "unavailable"
    result.update(reflector_probe=bad, reflector_probe_state="present", reflector_probe_reason=None)
    with pytest.raises(ValueError):
        responses.validate(result, "lab-campaign-view/15")


@pytest.mark.parametrize("probe", [PROBE, ABSENT])
@pytest.mark.parametrize("family", [14, 15])
def test_report_owner_facts_survive_unavailable_core_and_render(probe, family):
    result = ReportResult("op1", "unavailable", None, "unavailable", reflector_probe=probe,
                          reflector_probe_state="present", reflector_probe_reason=None).to_payload()
    assert result["schema"] == "lab-owner-report/15"
    assert result["reflector_probe"] == probe
    responses.validate(result, "lab-owner-report/15")
    lines = _report_lines(result)
    assert ("reflector probe: launched_by=lab state=exhausted restarts=2" in lines) == (probe == PROBE)
    projection = _result15(BLOCK) if family == 15 else campaign()
    data = build_report(projection, comparison_scope="in-sample").data
    data.update({key: result[key] for key in ("reflector_probe", "reflector_probe_state", "reflector_probe_reason")})
    markdown = Report(json.dumps(data)).markdown()
    assert ("reflector probe: launched_by=lab state=exhausted restarts=2" in markdown) == (probe == PROBE)


@pytest.mark.parametrize("probe", [PROBE, ABSENT])
@pytest.mark.parametrize("unreadable", ["missing", None, [], 1, {"probe_availability": []}],
                         ids=["missing", "none", "list", "scalar", "malformed-mapping"])
def test_report_unreadable_family15_independent_preserves_owner_facts(probe, unreadable):
    """Deliberately unreadable blocks retain diagnostics instead of invalidating the report."""
    from types import SimpleNamespace
    from evolution_lab.report import _lab_independent
    from evolution_lab._reflector_probe import probe_fields

    data = build_report(_result15(BLOCK), comparison_scope="in-sample").data
    if unreadable == "missing":
        data["core"].pop("independent")
    else:
        data["core"]["independent"] = unreadable
    data.update(_lab_independent(data["core"], SimpleNamespace(bindings_reason=None, bindings=None)))
    data.update(probe_fields(probe))
    report = Report(json.dumps(data))
    markdown = report.markdown()
    assert report.data["reflector_probe"] == probe
    assert (report.data["reflector_probe_state"], report.data["reflector_probe_reason"]) == ("present", None)
    assert (data["lab_independent"], data["lab_independent_state"], data["lab_independent_reason"]) == (
        None, "unavailable", "unreadable")
    assert "блок ядра не читается этой версией Lab (`unreadable`); сравнения не показаны." in markdown
    assert "checks: unavailable (unreadable)" in markdown
    assert ("reflector probe: launched_by=lab state=exhausted restarts=2" in markdown) == (probe == PROBE)
    assert "reflector probe: unavailable" not in markdown
    assert "Исход core:" in markdown


def test_previous_majors_are_refused():
    for schema in ["lab-campaign-view/11", "lab-owner-report/9"]:
        with pytest.raises(ValueError):
            responses.validate({"schema": schema}, schema)


def test_standalone_campaign_report_marks_owner_evidence_unavailable():
    data = build_report(campaign()).data
    assert data['reflector_probe'] is None
    assert (data['reflector_probe_state'], data['reflector_probe_reason']) == ('unavailable', 'unreadable')


@pytest.mark.parametrize('probe', [None, {**ABSENT, 'restarts': 1}, {**ABSENT, 'last_exit': 0},
    {**ABSENT, 'launched_by': 'operator'}, {**PROBE, 'state': []}, {**PROBE, 'launched_by': {}},
    {**PROBE, 'restarts': 1.0}, {**PROBE, 'last_exit': 1.0}])
def test_report_closed_probe_validation(probe):
    payload = ReportResult('op1', 'unavailable', None, 'unavailable').to_payload()
    payload.update(reflector_probe=probe, reflector_probe_state='present', reflector_probe_reason=None)
    with pytest.raises(ValueError):
        responses.validate(payload, 'lab-owner-report/15')


def test_evaluation_output_does_not_claim_a_reflector_probe():
    payload = ReportResult("op1", "available", {"operation": "evaluation", "core": {}, "measurements": []}, None).to_payload()
    assert payload["reflector_probe_state"] == "unavailable"
    assert _report_lines(payload) == []
