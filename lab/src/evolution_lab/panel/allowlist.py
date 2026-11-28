"""GET routes of the service the panel forwards (ADR-0018 p. 1, spec §2.8).

Templates are the service's own FastAPI paths; a test pins that every service GET route is in
exactly one of ALLOWED/EXCLUDED, so a new read route is a conscious decision here.
Matching is on the raw request path: one segment is an Owner/project identifier
(``owner/models.py`` _ID), so ``%``, ``.``/``..`` segments and empty segments never match;
``%`` is admitted as ``%40`` in the measurement-id segment (epoch suffix ``@e<k>``)
and as ``%XX`` in the core case-id/sample-id segments.
``report.md`` (spec 2026-10-14 report-markdown §5): a literal dot in a template is escaped.
"""
from __future__ import annotations

import re

ALLOWED = (
    "/v1/health",
    "/v1/projects", "/v1/projects/{project_id}", "/v1/projects/{project_id}/operations",
    "/v1/projects/{project_id}/facts", "/v1/projects/{project_id}/status",
    "/v1/projects/{project_id}/confirmations",
    "/v1/scenarios", "/v1/scenarios/{scenario_id}", "/v1/scenarios/{scenario_id}/schemas/{name}/{version}",
    "/v1/guides/requirements",
    "/v1/targets", "/v1/targets/{target_id}",
    "/v1/operations", "/v1/operations/{operation_id}", "/v1/operations/{operation_id}/events",
    "/v1/operations/{operation_id}/report", "/v1/operations/{operation_id}/report.md", "/v1/operations/{operation_id}/campaign",
    "/v1/operations/{operation_id}/suspensions",
    "/v1/operations/{operation_id}/routing",
    "/v1/operations/{operation_id}/measurements",
    "/v1/operations/{operation_id}/measurements/{measurement_id}",
    "/v1/operations/{operation_id}/measurements/{measurement_id}/cases/{case_id:path}/samples/{sample_id:path}",
    "/v1/operations/{operation_id}/attempts/{attempt_id}",
    "/v1/operations/{operation_id}/reflections/{reflection_id}",
    "/v1/operations/{operation_id}/diff",
    "/v1/operations/{operation_id}/failure",
)
EXCLUDED = (
    # Continuation details have service/CLI consumers; the panel has no view for them.
    "/v1/operations/{operation_id}/refusal", "/v1/operations/{operation_id}/inputs",
    # Tree reading is a service API; create-skill does not add a panel view (PDR-0032).
    "/v1/projects/{project_id}/tree",
    # Policy documents/confirmation are outside the read-only routing display (PDR-0017).
    "/v1/execution-policies", "/v1/execution-policies/{digest}",
    # Approval records have a service/CLI read and no panel view.
    "/v1/approvals", "/v1/projects/{project_id}/approvals",
    "/v1/bundles/{bundle_digest}",
    "/v1/bundles/{bundle_digest}/measurements",
    "/v1/prepared-executions/{prepared_digest}",
    "/v1/tasks/{task_id}",
    "/v1/operations/{operation_id}/exports/{export_digest}/content",  # results archive (spec §2.8)
    # Probe history holds raw dialog text: no probe in the Lab panel (PDR-0019 non-goal), and
    # disclosing dialog content needs its own rules (PDR-0017).
    "/v1/projects/{project_id}/probes", "/v1/probes/{session_id}",
)
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
# The one exception (spec 2026-09-30 §2.5): a measurement id may carry core's condition epoch,
# ``@e<k>``, or its single percent-encoded form ``%40e<k>`` (clients quote ``@``).
_MEASUREMENT_SEGMENT = _SEGMENT + r"(?:(?:@|%40)e[1-9][0-9]*)?"
# Core case/sample ids travel percent-encoded (spec 2026-10-13 §3.1): core grammar
# characters or %XX, but never encoded "/" (%2F), "." or ".." alone. The encoded
# cap is 1024 regex units (256 code points * four UTF-8 bytes), up to 3072
# encoded characters: each %XX counts as one regex unit.
_CORE_SEGMENT = r"(?!\.{1,2}(?:/|\Z))(?:(?!%2[Ff])(?:[\w.:@+\-]|%[0-9A-Fa-f]{2})){1,1024}"
_ATTEMPT_SEGMENT = r"r[0-9]{2,}-a[0-9]{2,}"
_REFLECTION_SEGMENT = r"r[0-9]{2,}-x[0-9]{2,}"


def _pattern(template: str) -> re.Pattern:
    template = template.replace(".", r"\.")  # a literal dot (report.md); segment patterns are substituted after
    template = template.replace("{measurement_id}", _MEASUREMENT_SEGMENT)
    for name, segment in (("{case_id:path}", _CORE_SEGMENT), ("{sample_id:path}", _CORE_SEGMENT),
                          ("{attempt_id}", _ATTEMPT_SEGMENT), ("{reflection_id}", _REFLECTION_SEGMENT)):
        template = template.replace(name, segment)
    return re.compile(re.sub(r"\{[a-z_]+\}", _SEGMENT, template) + r"\Z")


_PATTERNS = tuple(_pattern(template) for template in ALLOWED)


def allowed(raw_path: str) -> bool:
    return any(pattern.match(raw_path) for pattern in _PATTERNS)
