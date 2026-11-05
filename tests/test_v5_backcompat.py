"""rev I1: v3, v4, and v5-without-oracle_evidence packets all render cleanly;
render_oracle_sections is the only new surface and stays empty for legacy."""

from __future__ import annotations

import pytest

from evolution.evolution.reflection_evidence import (
    render_artifacts,
    render_failure_evidence,
    render_oracle_sections,
    render_solve_diag,
)


def _base_failure_evidence() -> dict:
    return {
        "failure_categories": ["no_artifact"],
        "produced_outputs": [],
        "verifier_result": {"passed": 0, "total": 1, "verification_category": "no_artifact"},
        "solve_exception": None,
        "code_generation_status": "ok",
        "required_output_count": 0,
        "required_output_suffixes": [],
    }


def _packet(schema: str) -> dict:
    return {
        "schema": schema,
        "agent_solve": {"exit_code": 1, "timeout": False, "diagnostic": "nonzero_exit"},
        "produced_artifacts": [],
        "failure_evidence": _base_failure_evidence(),
        "trace_context": {"trace_spec_idx": 0, "attempt_seed": 0},
    }


def _v4() -> dict:
    ev = _packet("evolution-trace-evidence-v4")
    ev["failure_evidence"]["source_feedback_included"] = True
    ev["failure_evidence"]["source_feedback"] = {
        "stderr_tail": "sanitized stderr",
        "exception_class": None,
        "exit_code": 1,
        "timeout": False,
    }
    return ev


@pytest.mark.parametrize(
    "ev",
    [
        _packet("evolution-trace-evidence-v3"),
        _v4(),
        _packet("evolution-trace-evidence-v5"),  # v5 without oracle_evidence
    ],
)
def test_legacy_and_v5_no_oracle_render_clean(ev):
    # None of these raise, and the oracle surface is empty for back-compat.
    render_solve_diag(ev)
    render_artifacts(ev)
    render_failure_evidence(ev)
    assert render_oracle_sections(ev) == ""


def test_v5_with_oracle_renders_headed_sections():
    ev = _packet("evolution-trace-evidence-v5")
    ev["failure_evidence"]["oracle_evidence"] = {
        "tests_source": "def test_x():\n    assert v == 99999\n",
        "source_artifacts": [],
        "failed_tests": [
            {"nodeid": "tests/test_x.py::test_x", "message_tail": "assert 0 == 99999"}
        ],
        "pytest_traceback": "E   assert 0 == 99999",
    }
    out = render_oracle_sections(ev)
    assert "## Test Source" in out
    assert "## Failed Tests" in out
    assert "## Pytest Traceback" in out
    # The diagnose-don't-copy guardrail sits next to the literals.
    assert "leak scanner discards" in out
