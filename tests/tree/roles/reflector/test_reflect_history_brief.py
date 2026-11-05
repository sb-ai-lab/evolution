"""`reflect_brief/5`."""

from __future__ import annotations

from typing import Any

import pytest

from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor.editor_brief import BUDGET_REASON_SENTENCES
from evolution.tree.roles.editor_contracts import EMPTY_HISTORY
from evolution.tree.roles.reflector.reflect_brief import (
    REFLECTOR_BUDGET_LINE,
    AdaptiveReflectBrief,
    reflect_brief_payload,
    render_reflect_opening,
    with_history,
)
from evolution.tree.roles.reflector.reflect_history import (
    AGGREGATE_SENTENCE,
    HISTORY_COMPACT_SENTENCE,
)


def _history(
    reason: str | None = None, form: str = "full", reasons: list[str] | None = None
) -> dict[str, Any]:
    return {
        "version": "reflect_history/1",
        "rounds_closed": 0,
        "predecessor_rounds": 0,
        "detail_rounds": {"count": 0, "first": None, "last": None},
        "summary_rounds": {"count": 0, "first": None, "last": None},
        "aggregated_rounds": {"count": 0, "first": None, "last": None},
        "elided": {
            "reason": reason,
            "window_to_aggregate": 0,
            "detail_to_summary": 0,
            "summary_to_aggregate": 0,
            "aggregate_form": form,
        },
        "rounds": [],
        "aggregate": {"form": form, "reasons": [{"reason": r} for r in reasons or []]},
    }


def _brief(version: str, history: dict[str, Any]) -> AdaptiveReflectBrief:
    payload = {
        "version": version,
        "groups": [{"description": None}],
        "families": [],
        "history": history,
        "evidence_catalog": {"entries": []},
    }
    return AdaptiveReflectBrief(
        "d" * 64,
        None,
        EMPTY_HISTORY,
        ("skills/**",),
        {},
        payload,
        {},
        frozenset(),
        None,
        version=version,
    )


def test_reflect_brief_4_is_unchanged() -> None:
    import hashlib

    brief = _brief("reflect_brief/4", {})
    payload = hashlib.sha256(canonical_json(reflect_brief_payload(brief))).hexdigest()
    opening = hashlib.sha256(
        render_reflect_opening(brief, (), ("skills/**",), editor_max_changed_files=3).encode()
    ).hexdigest()
    assert payload == "56577f832f9593604571b58134f9b05f24b1e693427018f53386417c16b341ea"
    assert opening == "53e003490239623f311cead611006ddde6919f7834e2ee72cc611e098e78977b"


def test_the_history_sentences_sit_before_the_budget_line_and_the_payload() -> None:
    brief = _brief("reflect_brief/5", _history(reasons=["changed_file_budget"]))
    lines = render_reflect_opening(
        brief, (), ("skills/**",), editor_max_changed_files=3
    ).splitlines()
    assert lines[-1] == canonical_json(reflect_brief_payload(brief)).decode("utf-8")
    assert lines[-2] == REFLECTOR_BUDGET_LINE.format(limit=3)
    assert lines.index(HISTORY_COMPACT_SENTENCE) < lines.index(AGGREGATE_SENTENCE) < len(lines) - 2
    assert lines.index(BUDGET_REASON_SENTENCES["changed_file_budget"]) > lines.index(
        AGGREGATE_SENTENCE
    )


def test_reflect_brief_5_keeps_the_family_14_sentences() -> None:
    four = render_reflect_opening(_brief("reflect_brief/4", {}), (), ("skills/**",)).splitlines()
    five = render_reflect_opening(
        _brief("reflect_brief/5", _history()), (), ("skills/**",)
    ).splitlines()
    for line in four[:-1]:
        if "history" not in line.lower():
            assert line in five


def test_with_history_changes_only_the_history() -> None:
    brief = _brief("reflect_brief/5", _history())
    other = with_history(brief, _history(reason="budget", form="core"))
    old, new = reflect_brief_payload(brief), reflect_brief_payload(other)
    assert {k: v for k, v in old.items() if k != "history"} == {
        k: v for k, v in new.items() if k != "history"
    }
    assert new["history"]["elided"]["reason"] == "budget"
    with pytest.raises(ValueError, match="reflect_brief_5"):
        with_history(_brief("reflect_brief/4", {}), _history())


def test_builder_compact_history_is_optional_and_keeps_legacy_history(tmp_path) -> None:
    from evolution.tree.roles.grouping.reflect_grouping import (
        GroupingResult,
        admit_grouping_result,
        cohorts_from_grouping,
        select_portion,
    )
    from evolution.tree.roles.reflector.reflect_brief import build_adaptive_reflect_brief
    from tests.test_tree_adaptive_reflector import adaptive_fixture

    trace, inputs, _ = adaptive_fixture(tmp_path)
    try:
        from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
        from tests.campaign_fakes import PRIMARY
        from tests.tree.execution.test_journal_identity import cases
        from tests.tree.roles.editor.test_editor_evidence import _reflect_row

        inputs = FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=(_reflect_row("s1", 1),),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
            scorer_diagnostics=True,
            model_grouping_family=True,
        )
        grouping = admit_grouping_result(
            inputs.registry,
            GroupingResult(inputs.registry.digest, (None,)),
            policy_identity="policy",
        )
        arguments = dict(
            champion_digest=trace.baseline.digest,
            operator_context=None,
            history=EMPTY_HISTORY,
            editable_paths=("*.md",),
            grouping=grouping,
            selection=select_portion(cohorts_from_grouping(grouping), registry=inputs.registry),
            inputs=inputs,
        )
        legacy = build_adaptive_reflect_brief(**arguments)
        before = canonical_json(reflect_brief_payload(legacy))
        history = _history()
        compact = build_adaptive_reflect_brief(**arguments, compact_history=history)
        assert compact.version == "reflect_brief/5"
        assert reflect_brief_payload(compact)["history"] == history
        assert reflect_brief_payload(compact)["limits"]["history_chars"] == 96000
        assert compact.history is EMPTY_HISTORY
        assert (
            canonical_json(reflect_brief_payload(build_adaptive_reflect_brief(**arguments)))
            == before
        )
        inputs._model_grouping_family = False
        with pytest.raises(ValueError, match="reflect_brief_5_needs_family_14"):
            build_adaptive_reflect_brief(**arguments, compact_history=history)
    finally:
        trace.close()
