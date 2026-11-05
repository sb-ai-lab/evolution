"""`reflect_brief/1`: the Reflector's own brief and its catalog."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor_contracts import (
    EMPTY_HISTORY,
    AttemptChanges,
    AttemptResult,
    BriefHistory,
    HistoryAttempt,
    HistoryRound,
    Observation,
    ObservationInput,
    ObservationTurn,
    observation_payload,
    reflector_observation_payload,
)
from evolution.tree.roles.reflector.reflect_brief import (
    DIAGNOSTICS_INLINE_CHARS,
    REFLECT_BRIEF_VERSION,
    AdaptiveReflectBrief,
    build_adaptive_reflect_brief,
    build_reflect_brief,
    catalog_case_ids,
    diagnostics_opening_form,
    reflect_brief_digest,
    reflect_brief_payload,
    render_reflect_catalog,
    render_reflect_opening,
)
from evolution.tree.roles.reflector.reflect_evidence import select_focus
from tests.tree.roles.reflector.test_reflect_evidence import _snapshot, rep


def build_brief_for_test(
    tmp_path,
    *,
    scorer_diagnostics: bool = False,
    diagnostics: dict | None = None,
    families: bool = False,
    family_values: dict | None = None,
    model_grouping_family: bool = False,
    description: dict | None = None,
    pin=None,
) -> AdaptiveReflectBrief:
    """One `AdaptiveReflectBrief` over a single-case, single-repeat trace.

    Adapts the plan's `build_brief_for_test(scorer_diagnostics=, diagnostics=)`
    to this module's real helpers -- `_Trace`/`FrozenReflectionInputs` plus the
    builtin one-neighborhood grouping, the same wiring
    `test_tree_adaptive_reflector.adaptive_fixture` uses -- rather than a
    fixture this module never had.

    `description` (a `{"label", "description"}` mapping) assigns the one
    failing row to group 1 carrying that admitted description, as a model
    or command grouping would; without it the row stays unassigned and its
    builtin neighborhood carries none.
    """

    from evolution.tree.roles.grouping.reflect_grouping import (
        GroupingResult,
        admit_grouping_result,
        cohorts_from_grouping,
        select_portion,
    )
    from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
    from tests.campaign_fakes import PRIMARY
    from tests.tree.execution.test_journal_identity import cases
    from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

    trace = _Trace(tmp_path, turns=1, diagnostics=diagnostics)
    # A cohort (and so a `portion` entry) needs a failing observation, which
    # `failing` reads from `verdicts`, not `metrics` -- rewrite the trace's
    # stdout the way `test_tree_reflection_inputs.py`'s own fixtures do.
    stdout_path = trace.directory / "stdout"
    stdout = json.loads(stdout_path.read_text(encoding="utf-8"))
    stdout["verdicts"] = [{"criterion": "checks_x", "passed": False}]
    stdout_path.write_text(json.dumps(stdout), encoding="utf-8")
    inputs = FrozenReflectionInputs(
        baseline_binding=trace.baseline.digest,
        rows=(_reflect_row("s1", 1),),
        records=trace.journal.snapshot().records,
        run_root=trace.run_root,
        corpus=cases(),
        baseline=trace.baseline,
        environment_kind="static",
        metric_primary=PRIMARY,
        families=(family_values or {"c": "family-a"}) if families else None,
        families_configured=families,
        scorer_diagnostics=scorer_diagnostics,
        model_grouping_family=model_grouping_family,
        pin=pin,
    )
    result = (
        GroupingResult(inputs.registry.digest, (None,))
        if description is None
        else GroupingResult(inputs.registry.digest, (1,), descriptions={1: description})
    )
    grouping = admit_grouping_result(inputs.registry, result, policy_identity="policy")
    selection = select_portion(cohorts_from_grouping(grouping), registry=inputs.registry)
    return build_adaptive_reflect_brief(
        champion_digest=trace.baseline.digest,
        operator_context=None,
        history=EMPTY_HISTORY,
        editable_paths=("*.md",),
        grouping=grouping,
        selection=selection,
        inputs=inputs,
    )


_CHAMPION = "d" * 64


def _corpus() -> tuple[EvalCase, ...]:
    return (
        EvalCase(
            "case-01",
            "group-01",
            "stratum-01",
            ("tag",),
            {"text": "PUBLIC-CANARY-1"},
            {"secret": "PRIVATE-CANARY"},
            {"expected": "ORACLE-CANARY"},
        ),
        EvalCase(
            "case-02",
            "group-01",
            "stratum-01",
            ("tag",),
            {"text": "PUBLIC-CANARY-2"},
            {"secret": "PRIVATE-CANARY"},
            {"expected": "ORACLE-CANARY"},
        ),
    )


@pytest.fixture
def reflect_fixture() -> dict:
    reps = (
        rep("case-01", 1, failed=("answers",)),
        rep("case-01", 2),
        rep("case-02", 1),
    )
    focus = select_focus(reps, snapshot=_snapshot({}))
    return dict(
        focus=focus,
        repeats=reps,
        corpus=_corpus(),
        champion_digest=_CHAMPION,
        operator_context=None,
        history=EMPTY_HISTORY,
        editable_paths=("skills/**",),
    )


@pytest.fixture
def reflect_fixture_with_history(reflect_fixture: dict) -> dict:
    attempt = HistoryAttempt(
        attempt_id="r01-a01",
        index=1,
        parent_digest="a" * 64,
        editor="edited",
        editor_error=None,
        outcome="rejected",
        reason="reject_regression",
        code=None,
        look=3,
        candidate_digest="b" * 64,
        changes=AttemptChanges("present", ("skills/SKILL.md",), False),
        result=AttemptResult("absent"),
        edit_excerpt="--- a\n+++ b\n",
    )
    round_ = HistoryRound(
        round=1,
        outcome="stalled",
        selected_attempt=None,
        attempts=(attempt,),
        attempts_truncated=False,
        diagnosis={
            "status": "present",
            "reflection_id": "r01-x01",
            "groups": [
                {
                    "root_cause": "the fixture always answers the same way",
                    "suggested_fix": "vary the reply",
                    "evidence_ids": ["case:case-01"],
                    "affected_cases": ["case-01"],
                    "target_paths": ["skills/**"],
                    "probe_refs": [1],
                }
            ],
            "error_class": None,
            "probes": [{"seq": 1, "method": "GET", "path": "/health", "status": 200}],
        },
    )
    history = BriefHistory(1, (1,), 0, (round_,))
    fixture = dict(reflect_fixture)
    fixture["history"] = history
    return fixture


def test_payload_keys_and_digest(reflect_fixture: dict) -> None:
    brief = build_reflect_brief(**reflect_fixture)
    payload = reflect_brief_payload(brief)
    assert list(payload) == [
        "version",
        "champion_digest",
        "operator_context",
        "limits",
        "focus",
        "clusters",
        "cases",
        "portion",
        "history",
        "editable_paths",
    ]
    assert payload["version"] == REFLECT_BRIEF_VERSION
    assert reflect_brief_digest(brief) == reflect_brief_digest(
        build_reflect_brief(**reflect_fixture)
    )
    assert payload["portion"][0]["exemplar"]["verdicts"][0]["criterion"] == "answers"
    serialized = json.dumps(payload)
    assert "private" not in serialized and "oracle" not in serialized
    assert "PRIVATE-CANARY" not in serialized and "ORACLE-CANARY" not in serialized


def test_catalog_namespace_and_case_map(reflect_fixture: dict) -> None:
    brief = build_reflect_brief(**reflect_fixture)
    catalog = render_reflect_catalog(brief)
    ids = [record.evidence_id for record in catalog]
    assert ids[0] == "summary"
    assert (
        any(i.startswith("case:") for i in ids)
        and any(i.startswith("obs:") for i in ids)
        and any(i.startswith("cohort:") for i in ids)
    )
    mapping = catalog_case_ids(catalog)
    assert mapping["summary"] is None
    assert all(mapping[i] is not None for i in ids if i.startswith(("case:", "obs:", "cohort:")))


def test_history_carries_diagnosis_and_excerpt(reflect_fixture_with_history: dict) -> None:
    brief = build_reflect_brief(**reflect_fixture_with_history)
    payload = reflect_brief_payload(brief)
    shown = payload["history"]["rounds"][0]
    assert shown["attempts"][0]["reason"] == "reject_regression"
    assert shown["diagnosis"]["status"] == "present" and shown["diagnosis"]["groups"]
    assert shown["attempts"][0]["edit_excerpt"].startswith("--- ")
    assert len(shown["attempts"][0]["edit_excerpt"]) <= 600
    # The Reflector's own history keeps what the editor's drops: the
    # probe log and each group's `probe_refs` are its own record to read back.
    assert shown["diagnosis"]["probes"] == [
        {"seq": 1, "method": "GET", "path": "/health", "status": 200}
    ]
    assert shown["diagnosis"]["groups"][0]["probe_refs"] == [1]


def test_the_reflector_brief_version_does_not_move() -> None:
    # PDR-0008. On its own this assertion proves nothing -- it once stood
    # beside a claim about `reflect_evidence`'s event count while
    # `_repeat_payload` was serializing the whole observation, `reason`
    # included. The two byte-identity tests at the end of this module are
    # what make the version string true; this one only pins the string.
    from evolution.tree.roles.reflector.reflect_brief import REFLECT_BRIEF_VERSION

    assert REFLECT_BRIEF_VERSION == "reflect_brief/1"


# -- PDR-0008: the turn `reason` is editor-facing, not Reflector-facing ------


def _observation_with(reason: dict[str, str] | None) -> Observation:
    """One `present` observation whose single turn differs between the two
    fixtures below *only* in its `reason` -- same outcome, same events, same
    input, so a byte difference downstream can only be the reason."""

    return Observation(
        "present",
        turns=(
            ObservationTurn(
                0,
                "candidate_failed",
                ObservationInput("present", "PUBLIC-CANARY-1", False),
                ({"type": "assistant_message", "content": "x", "truncated": False},),
                0,
                False,
                reason,
            ),
        ),
        feedback={"format": "ok"},
        feedback_status="present",
    )


def _fixture_with_reason(reason: dict[str, str] | None) -> dict:
    observation = _observation_with(reason)
    reps = tuple(
        replace(r, observation=observation)
        for r in (
            rep("case-01", 1, failed=("answers",)),
            rep("case-01", 2),
            rep("case-02", 1),
        )
    )
    return dict(
        focus=select_focus(reps, snapshot=_snapshot({})),
        repeats=reps,
        corpus=_corpus(),
        champion_digest=_CHAMPION,
        operator_context=None,
        history=EMPTY_HISTORY,
        editable_paths=("skills/**",),
    )


def test_a_turn_reason_never_moves_the_reflect_brief_payload() -> None:
    """The executable form of "`reflect_brief/1` did not move": the same turns
    with and without a `reason` must serialize to the same bytes."""

    with_reason = build_reflect_brief(
        **_fixture_with_reason({"category": "format", "code": "REASON-CANARY"})
    )
    without = build_reflect_brief(**_fixture_with_reason(None))

    assert canonical_json(reflect_brief_payload(with_reason)) == canonical_json(
        reflect_brief_payload(without)
    )
    assert reflect_brief_digest(with_reason) == reflect_brief_digest(without)
    assert "REASON-CANARY" not in json.dumps(reflect_brief_payload(with_reason))


def test_a_turn_reason_never_moves_the_reflect_catalog() -> None:
    with_reason = build_reflect_brief(
        **_fixture_with_reason({"category": "format", "code": "REASON-CANARY"})
    )
    without = build_reflect_brief(**_fixture_with_reason(None))

    assert render_reflect_catalog(with_reason) == render_reflect_catalog(without)
    joined = "\n".join(record.content for record in render_reflect_catalog(with_reason))
    assert "REASON-CANARY" not in joined


def test_the_reflector_projection_is_an_allowlist_of_the_editor_payload() -> None:
    """A field the editor's serializer gains stays out of the Reflector's copy
    until it is named -- fail closed, not leak by default."""

    observation = _observation_with({"category": "format", "code": "REASON-CANARY"})
    editor = observation_payload(observation)
    reflector = reflector_observation_payload(observation)

    assert set(editor) - set(reflector) == set()
    assert list(editor) == list(reflector)
    assert set(editor["turns"][0]) - set(reflector["turns"][0]) == {"reason"}
    assert editor["turns"][0]["reason"] == {"category": "format", "code": "REASON-CANARY"}


# -- reflect_brief/3: diagnostics in the opening (ADR-0021) ----------


def test_small_diagnostics_are_inlined_whole() -> None:
    value = {"route": "handoff", "why": "x" * 100}
    assert diagnostics_opening_form(value) == value


def test_inline_boundary_is_canonical_characters() -> None:
    filler = "y" * (DIAGNOSTICS_INLINE_CHARS - len(canonical_json({"k": ""})))
    at_limit = {"k": filler}
    assert len(canonical_json(at_limit)) == DIAGNOSTICS_INLINE_CHARS
    assert diagnostics_opening_form(at_limit) == at_limit
    over = {"k": filler + "z"}
    form = diagnostics_opening_form(over)
    assert form["omitted"] is True and form["chars"] == DIAGNOSTICS_INLINE_CHARS + 1
    assert form["keys"] == [["k", len(canonical_json(filler + "z"))]]
    assert form["keys_omitted"] == 0


def test_omitted_form_is_bounded_and_keeps_colliding_prefixes() -> None:
    many = {f"k{i:05d}": i for i in range(5000)}
    form = diagnostics_opening_form(many)
    assert len(form["keys"]) == 32 and form["keys_omitted"] == 4968
    prefix = "p" * 64
    pair = {prefix + "x": "a" * 3000, prefix + "y": "b" * 3000}
    form = diagnostics_opening_form(pair)
    assert [key for key, _ in form["keys"]] == [prefix, prefix]
    assert len(canonical_json(diagnostics_opening_form(many))) < 3 * 1024


def test_absent_diagnostics_open_as_null() -> None:
    assert diagnostics_opening_form(None) is None


def test_family_seven_brief_version_does_not_move(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=False)
    assert brief.payload["version"] == "reflect_brief/2"
    assert all("exemplar_diagnostics" not in entry for entry in brief.payload["portion"])


def test_family_eight_brief_carries_exemplar_diagnostics(tmp_path) -> None:
    brief = build_brief_for_test(
        tmp_path, scorer_diagnostics=True, diagnostics={"route": "handoff"}
    )
    assert brief.payload["version"] == "reflect_brief/3"
    entry = brief.payload["portion"][0]
    assert set(entry["exemplar_diagnostics"]) == set(entry["exemplars"])
    assert next(iter(entry["exemplar_diagnostics"].values())) == {"route": "handoff"}
    assert brief.payload["limits"]["diagnostics_inline_chars"] == DIAGNOSTICS_INLINE_CHARS


def test_family_eight_opening_names_diagnostics_and_family_expansion(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=True, families=True)
    text = render_reflect_opening(brief, (), ("*.md",))
    assert "diagnostics is the scorer's explanation" in text
    assert "Cases of the same family enter the exam" in text


_EXPANSION = "Cases of the same family enter the exam"


def test_configured_families_name_expansion_when_no_case_has_a_value(tmp_path) -> None:
    """Codex `families: metadata` where every case lacks the optional
    value still widens the exam, so the opening still names it -- the
    sentence follows the configured policy, not the non-null membership."""

    brief = build_brief_for_test(
        tmp_path, scorer_diagnostics=True, families=True, family_values={"c": None}
    )
    assert not brief.payload["families"]
    assert _EXPANSION in render_reflect_opening(brief, (), ("*.md",))


def test_families_none_omits_the_expansion_sentence(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=True, families=False)
    assert _EXPANSION not in render_reflect_opening(brief, (), ("*.md",))


def test_family_seven_opening_never_names_expansion(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=False, families=True)
    assert brief.version == "reflect_brief/2"
    assert _EXPANSION not in render_reflect_opening(brief, (), ("*.md",))


def test_family_seven_opening_is_unchanged(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=False)
    text = render_reflect_opening(brief, (), ("*.md",))
    assert "diagnostics" not in text


# -- Golden: families 7 and 8 open byte for byte as before family 9 ----------

#: sha256 of `render_reflect_opening` over `build_brief_for_test` at 471c42db,
#: before `reflect_brief/4` existed: family 7 (`scorer_diagnostics=False`) and
#: family 8 (`scorer_diagnostics=True`, `diagnostics={"route": "handoff"}`,
#: `families=True`).
_FAMILY_SEVEN_OPENING_SHA256 = "f6f91e20cfa34562df332198472e5fcdcff10105895fd885b0b2c0ce7e5dff59"
_FAMILY_EIGHT_OPENING_SHA256 = "c123dfc75476ccaf45b76c2a39b410084fb745039714e8c625ce8ac9e7ff6c2e"


def _opening_sha256(brief: AdaptiveReflectBrief) -> str:
    import hashlib

    return hashlib.sha256(render_reflect_opening(brief, (), ("*.md",)).encode()).hexdigest()


def test_family_seven_opening_bytes_are_pinned(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=False)
    assert _opening_sha256(brief) == _FAMILY_SEVEN_OPENING_SHA256


def test_family_eight_opening_bytes_are_pinned(tmp_path) -> None:
    brief = build_brief_for_test(
        tmp_path, scorer_diagnostics=True, diagnostics={"route": "handoff"}, families=True
    )
    assert brief.version == "reflect_brief/3"
    assert _opening_sha256(brief) == _FAMILY_EIGHT_OPENING_SHA256


# -- reflect_brief/4: the selected group's admitted description (family 9) ---

_MODEL_DESCRIPTION = {
    "label": "Wrong route",
    "description": "Observed: answered directly\nExpected: a handoff\nQuestion: why no handoff?",
}


def test_family_nine_brief_carries_the_selected_group_description(tmp_path) -> None:
    brief = build_brief_for_test(
        tmp_path,
        scorer_diagnostics=True,
        model_grouping_family=True,
        description=_MODEL_DESCRIPTION,
    )
    assert brief.version == "reflect_brief/4"
    assert brief.payload["version"] == "reflect_brief/4"
    (group,) = brief.payload["groups"]
    assert group["description"] == _MODEL_DESCRIPTION
    # Family 8's additions are inherited.
    assert "exemplar_diagnostics" in brief.payload["portion"][0]
    text = render_reflect_opening(brief, (), ("*.md",))
    assert "Wrong route" in text
    assert "Question: why no handoff?" in text
    assert "diagnostics is the scorer's explanation" in text
    assert _GROUP_DESCRIPTION_SENTENCE in text


def test_family_nine_group_without_a_description_opens_as_null(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=True, model_grouping_family=True)
    assert brief.version == "reflect_brief/4"
    (group,) = brief.payload["groups"]
    assert group["description"] is None
    assert _GROUP_DESCRIPTION_SENTENCE not in render_reflect_opening(brief, (), ("*.md",))


_GROUP_DESCRIPTION_SENTENCE = "A group's description comes from the grouping stage"


_BUDGET = (
    "Each Editor Attempt may change at most 3 files; prefer hypotheses whose target_paths fit, "
    "or rank target_paths so that the first 3 carry the change."
)


def test_the_reflector_opening_states_the_editor_budget_before_the_payload(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=True)
    plain = render_reflect_opening(brief, (), ("*.md",))
    stated = render_reflect_opening(brief, (), ("*.md",), editor_max_changed_files=3)
    assert _BUDGET not in plain
    assert stated.splitlines()[-1] == plain.splitlines()[-1]  # the payload JSON, unchanged
    assert stated.splitlines()[-2] == _BUDGET
    assert render_reflect_opening(brief, (), ("*.md",), editor_max_changed_files=None) == plain


def test_the_campaign_hands_the_bound_editor_budget_to_the_reflector() -> None:
    """ADR-0037 D10: `_Campaign._editor_file_budget` reads the bound Editor's limit
    in current campaigns, but not for an Editor kind without the key."""

    from types import SimpleNamespace

    from evolution.tree.campaign import _Campaign

    def budget(family: int, editor: object) -> int | None:
        stub = SimpleNamespace(family_version=family, editor=editor)
        return _Campaign._editor_file_budget(stub)

    limited = SimpleNamespace(max_changed_files=3)
    assert budget(14, limited) == 3
    assert budget(14, SimpleNamespace(max_changed_files=None)) is None
    assert budget(14, SimpleNamespace()) is None


def test_the_editor_budget_never_enters_the_reflector_payload(tmp_path) -> None:
    brief = build_brief_for_test(tmp_path, scorer_diagnostics=True)
    payload = reflect_brief_payload(brief)
    render_reflect_opening(brief, (), ("*.md",), editor_max_changed_files=3)
    assert reflect_brief_payload(brief) == payload
    assert b"max_changed_files" not in canonical_json(payload)


def test_the_reflector_opening_explains_a_detail_code_in_history(tmp_path) -> None:
    from dataclasses import replace

    from evolution.tree.roles.editor.editor_brief import BUDGET_REASON_SENTENCES
    from tests.tree.roles.editor.test_editor_brief import _history_with

    brief = replace(
        build_brief_for_test(tmp_path, scorer_diagnostics=True),
        history=_history_with("changed_file_budget"),
    )
    assert BUDGET_REASON_SENTENCES["changed_file_budget"] in render_reflect_opening(
        brief, (), ("*.md",)
    )


def test_a_pinned_reflector_brief_hashes_evidence_and_modes(tmp_path):
    from tests.test_tree_evidence_projection import pin

    selected = pin(turns=3)
    brief = build_brief_for_test(
        tmp_path, pin=selected, scorer_diagnostics=True, model_grouping_family=True
    )
    payload = reflect_brief_payload(brief)
    assert payload["version"] == "reflect_brief/6"
    assert payload["evidence"] == selected.settings.to_payload()
    assert payload["input_checks"] == dict(selected.input_modes)
