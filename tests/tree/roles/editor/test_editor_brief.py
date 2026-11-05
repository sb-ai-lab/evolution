"""Tests for `roles/editor/editor_brief.py`: the brief-rendering seam."""

from __future__ import annotations

import json
import random
import re
from typing import Any

import pytest

from evolution.tree.roles.editor.editor_brief import EvidenceRecord, render_catalog, render_opening
from evolution.tree.roles.editor_contracts import (
    BRIEF_LIMITS_V3,
    BRIEF_VERSION,
    EMPTY_HISTORY,
    AttemptChanges,
    AttemptResult,
    BriefHistory,
    Diagnosis,
    DiagnosisFocus,
    DiagnosisGroup,
    EditBrief,
    EditBriefCase,
    EditBriefRepeat,
    HistoryAttempt,
    HistoryRound,
    brief_payload,
    diagnosis_for_brief,
    history_payload,
    with_attempt_feedback,
)

_CHAMPION = "a" * 64

_DEFAULT_DIAGNOSIS = Diagnosis("absent", None, DiagnosisFocus("mass", ("a",)), (), None)


def test_default_brief_limits_remain_immutable() -> None:
    brief = EditBrief(_CHAMPION, None, (), _DEFAULT_DIAGNOSIS)
    assert brief.limits == BRIEF_LIMITS_V3
    with pytest.raises(TypeError):
        brief.limits["groups"] = 0  # type: ignore[index]


_TERMINALS = (
    "completed",
    "turn_budget",
    "time_budget",
    "target_failed",
    "cancelled",
    "interrupted",
    "environment_failed",
    "scorer_failed",
    "infrastructure_failed",
)


def _brief(
    *,
    ctx: str | None = None,
    scale: float = 1.0,
    clean: bool = False,
    diagnosis: Diagnosis = _DEFAULT_DIAGNOSIS,
    history: BriefHistory = EMPTY_HISTORY,
) -> EditBrief:
    if clean:
        c1_repeats = (
            EditBriefRepeat(
                repeat=0,
                sample_id="c1-0",
                terminal="completed",
                primary_score=0.2 * scale,
                critical_pass=True,
            ),
            EditBriefRepeat(
                repeat=1,
                sample_id="c1-1",
                terminal="completed",
                primary_score=0.9 * scale,
                critical_pass=True,
            ),
        )
        c2_repeats = (
            EditBriefRepeat(
                repeat=0,
                sample_id="c2-0",
                terminal="completed",
                primary_score=0.5 * scale,
                critical_pass=True,
            ),
        )
    else:
        c1_repeats = (
            EditBriefRepeat(
                repeat=0,
                sample_id="c1-0",
                terminal="scored",
                primary_score=0.2 * scale,
                critical_pass=True,
            ),
            EditBriefRepeat(
                repeat=1,
                sample_id="c1-1",
                terminal="scored",
                primary_score=0.9 * scale,
                critical_pass=False,
            ),
        )
        c2_repeats = (
            EditBriefRepeat(
                repeat=0,
                sample_id="c2-0",
                terminal="time_budget",
                primary_score=None,
                critical_pass=None,
            ),
        )
    return EditBrief(
        champion_digest=_CHAMPION,
        operator_context=ctx,
        cases=(
            EditBriefCase(
                case_id="c1",
                group_id="g1",
                stratum="s",
                tags=("t1",),
                public={"prompt": "c1-public"},
                repeats=c1_repeats,
            ),
            EditBriefCase(
                case_id="c2",
                group_id="g2",
                stratum="s",
                tags=(),
                public={"prompt": "c2-public"},
                repeats=c2_repeats,
            ),
        ),
        diagnosis=diagnosis,
        history=history,
    )


def test_catalog_order_and_kinds() -> None:
    catalog = render_catalog(_brief())
    assert [record.kind for record in catalog] == ["summary", "diagnosis", "case", "case"]
    assert catalog[0].evidence_id == "summary"
    assert catalog[1].evidence_id == "diagnosis"
    assert catalog[2].evidence_id == "case:c1"
    assert catalog[3].evidence_id == "case:c2"


def test_summary_reports_facts_without_thresholds() -> None:
    text = render_catalog(_brief())[0].content
    assert "critical_failed 1" in text
    assert "unscored 1 (time_budget" in text
    assert "min/max/mean 0.2/0.9/0.55" in text
    assert not re.search(r"\b(pass|fail|failing|threshold)\b", text)


@pytest.mark.parametrize("scale", [1.0, 100.0])
def test_summary_shape_is_scale_independent(scale: float) -> None:
    reference = render_catalog(_brief(scale=1.0))[0].content
    scaled = render_catalog(_brief(scale=scale))[0].content

    def strip_digits(text: str) -> str:
        return re.sub(r"\d+(\.\d+)?", "", text)

    assert strip_digits(reference) == strip_digits(scaled)


def test_summary_says_when_nothing_is_critical_failed_or_unscored() -> None:
    text = render_catalog(_brief(clean=True))[0].content
    assert text.strip().splitlines()[-1] == "no critical-failed or unscored repeat"


def test_summary_omits_the_clean_line_when_something_is_wrong() -> None:
    text = render_catalog(_brief())[0].content
    assert "no critical-failed or unscored repeat" not in text


def test_case_record_carries_public_and_repeats_and_nothing_else() -> None:
    catalog = render_catalog(_brief())
    payload = json.loads(catalog[2].content)
    assert payload.keys() == {
        "case_id",
        "group_id",
        "stratum",
        "tags",
        "public",
        "repeats",
        "observations",
    }
    assert payload["observations"] == []
    assert payload["case_id"] == "c1"
    assert payload["group_id"] == "g1"
    assert payload["stratum"] == "s"
    assert payload["tags"] == ["t1"]
    assert payload["public"] == {"prompt": "c1-public"}
    assert payload["repeats"] == [
        {
            "repeat": 0,
            "sample_id": "c1-0",
            "terminal": "scored",
            "primary_score": 0.2,
            "critical_pass": True,
        },
        {
            "repeat": 1,
            "sample_id": "c1-1",
            "terminal": "scored",
            "primary_score": 0.9,
            "critical_pass": False,
        },
    ]


def test_opening_has_operator_context_verbatim_and_direction_sentence() -> None:
    brief = _brief(ctx="Operator says: read the flaky cases first.")
    catalog = render_catalog(brief)
    opening = render_opening(brief, catalog, ("scripts/**", "SKILL.md"))
    assert opening.startswith("Operator says: read the flaky cases first.")
    assert "Writable paths: scripts/**, SKILL.md" in opening
    assert f"Catalog: {len(catalog)} records" in opening
    assert 'read "summary" first' in opening
    assert (
        "Scores are reported exactly as the scorer emitted them; "
        "their scale and direction are not part of this brief." in opening
    )


def test_opening_without_operator_context_starts_with_writable_paths() -> None:
    brief = _brief(ctx=None)
    catalog = render_catalog(brief)
    opening = render_opening(brief, catalog, ("scripts/**",))
    assert opening.startswith("Writable paths: scripts/**")


@pytest.mark.parametrize("seed", range(20))
def test_rendered_text_never_contains_private_or_oracle_markers(seed: int) -> None:
    rng = random.Random(seed)
    n_cases = rng.randint(1, 5)
    cases = []
    for i in range(n_cases):
        n_repeats = rng.randint(0, 4)
        repeats = []
        for r in range(n_repeats):
            scored = rng.random() < 0.5
            repeats.append(
                EditBriefRepeat(
                    repeat=r,
                    sample_id=f"c{i}-{r}",
                    terminal=rng.choice(_TERMINALS),
                    primary_score=rng.uniform(0.0, 1.0) if scored else None,
                    critical_pass=rng.choice([True, False, None]),
                )
            )
        cases.append(
            EditBriefCase(
                case_id=f"c{i}",
                group_id=f"g{i % 2}",
                stratum=f"s{i % 3}",
                tags=(f"tag{i}",),
                public={"text": f"PUBLIC-{i}"},
                repeats=tuple(repeats),
            )
        )
    brief = EditBrief(
        champion_digest=_CHAMPION,
        operator_context=rng.choice([None, "operator note"]),
        cases=tuple(cases),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )
    catalog = render_catalog(brief)
    opening = render_opening(brief, catalog, ("scripts/**",))
    full_text = opening + "\n" + "\n".join(record.content for record in catalog)

    assert "PRIVATE-" not in full_text
    assert "ORACLE-" not in full_text
    for i, case_record in enumerate(catalog[2:]):
        assert f"PUBLIC-{i}" in case_record.content


def test_evidence_record_grammar() -> None:
    with pytest.raises(ValueError):
        EvidenceRecord("with space", "content")
    with pytest.raises(ValueError):
        EvidenceRecord("ok-id", "content", kind="x y")


def _brief_with_case_ids(case_ids: tuple[str, ...]) -> EditBrief:
    return EditBrief(
        champion_digest=_CHAMPION,
        operator_context=None,
        cases=tuple(
            EditBriefCase(
                case_id=case_id,
                group_id="g",
                stratum="s",
                tags=(),
                public={},
                repeats=(),
            )
            for case_id in case_ids
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )


def test_case_evidence_id_escape_is_injective_across_the_marker_character() -> None:
    # `.` is the escape marker: a naive scheme that leaves `.` verbatim-safe
    # would collide "a@b" (`.` + hex of `@`) with "a.40.b" (a literal `.40.`).
    brief = _brief_with_case_ids(("a@b", "a.40.b"))
    catalog = render_catalog(brief)
    ids = [record.evidence_id for record in catalog[2:]]
    assert len(ids) == len(set(ids)), f"case ids collided: {ids}"
    assert ids[0] != ids[1]


def _decode_case_evidence_id(evidence_id: str) -> str:
    """Test-only decoder for `_case_evidence_id`'s escape scheme (not present
    in the module -- nothing there needs to reverse the encoding). Every `.`
    starts a two-hex-digit UTF-8 byte; every other character is a literal
    that passes through unchanged; consecutive escaped bytes are grouped and
    UTF-8-decoded together so a multi-byte character round-trips.
    """

    assert evidence_id.startswith("case:")
    encoded = evidence_id[len("case:") :]
    parts: list[str] = []
    pending = bytearray()
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == ".":
            pending.append(int(encoded[index + 1 : index + 3], 16))
            index += 3
        else:
            if pending:
                parts.append(bytes(pending).decode("utf-8"))
                pending = bytearray()
            parts.append(char)
            index += 1
    if pending:
        parts.append(bytes(pending).decode("utf-8"))
    return "".join(parts)


@pytest.mark.parametrize(
    "case_id",
    ["a.40.b", "a@b", "a b", "café", "plain-id_ok:1", "a@b.c d"],
)
def test_case_evidence_id_escape_round_trips(case_id: str) -> None:
    catalog = render_catalog(_brief_with_case_ids((case_id,)))
    evidence_id = catalog[2].evidence_id
    assert _decode_case_evidence_id(evidence_id) == case_id


# --------------------------------------------------------------------------
# Bounded evidence ids (F1)
# --------------------------------------------------------------------------


_ID_GRAMMAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


@pytest.mark.parametrize(
    ("label", "case_id"),
    [
        ("ascii", "c" * 124),
        ("cyrillic", "ссылка" * 6 + "ссыл"),
    ],
)
def test_a_long_case_id_renders_within_the_evidence_id_cap(label: str, case_id: str) -> None:
    # `EvidenceRecord` caps an id at 128 characters and the escape expands a
    # non-ASCII character to three characters per UTF-8 byte, so an unbounded
    # id would raise `ValueError` here and fail every attempt of the round.
    assert len(case_id) == (124 if label == "ascii" else 40)

    catalog = render_catalog(_brief_with_case_ids((case_id,)))

    evidence_id = catalog[2].evidence_id
    assert len(evidence_id) <= 128
    assert _ID_GRAMMAR.fullmatch(evidence_id)
    assert evidence_id.startswith("case:")


def test_two_long_case_ids_differing_only_in_their_tail_get_different_ids() -> None:
    first, second = "d" * 200 + "-one", "d" * 200 + "-two"

    catalog = render_catalog(_brief_with_case_ids((first, second)))

    ids = [record.evidence_id for record in catalog[2:]]
    assert len(ids) == len(set(ids)), f"case ids collided: {ids}"
    assert all(len(evidence_id) <= 128 for evidence_id in ids)


def test_a_case_id_short_enough_to_fit_is_untouched_by_the_bound() -> None:
    # 123 escaped characters plus `case:` is exactly the cap: the digest form
    # starts only above it, so nothing that fits today changes shape.
    short, at_the_cap = "plain-id_ok:1", "e" * 123

    catalog = render_catalog(_brief_with_case_ids((short, at_the_cap)))

    assert catalog[2].evidence_id == f"case:{short}"
    assert catalog[3].evidence_id == f"case:{at_the_cap}"
    assert len(catalog[3].evidence_id) == 128


# --------------------------------------------------------------------------
# edit_brief/3: observation and attempt records
# --------------------------------------------------------------------------


def _brief_v2() -> EditBrief:
    from evolution.tree.roles.editor_contracts import (
        AttemptChanges,
        AttemptResult,
        BriefHistory,
        HistoryAttempt,
        HistoryRound,
        Observation,
        ObservationInput,
        ObservationTurn,
    )

    observation = Observation(
        "present",
        turns=(
            ObservationTurn(
                0,
                "ok",
                ObservationInput("present", "PUBLIC-0", False),
                ({"type": "assistant_message", "content": "TARGET-SAID-THIS", "truncated": False},),
                0,
                False,
            ),
        ),
        feedback={"format": "ok"},
        feedback_status="present",
    )
    case = EditBriefCase(
        case_id="c.1",
        group_id="g",
        stratum="s",
        tags=(),
        public={"text": "PUBLIC-0"},
        repeats=(
            EditBriefRepeat(0, "c1-0", "completed", 0.5, True, observation),
            EditBriefRepeat(1, "c1-1", "target_failed", None, None),
        ),
    )
    attempt = HistoryAttempt(
        "r01-a01",
        1,
        _CHAMPION,
        "edited",
        None,
        "rejected",
        "upper_bound_nonpositive",
        None,
        3,
        "c" * 64,
        AttemptChanges("present", ("skills/SKILL.md",), False),
        AttemptResult("absent"),
    )
    history = BriefHistory(1, (1,), 0, (HistoryRound(1, "stalled", None, (attempt,), False),))
    return EditBrief(
        champion_digest=_CHAMPION,
        operator_context=None,
        cases=(case,),
        diagnosis=_DEFAULT_DIAGNOSIS,
        history=history,
    )


def test_catalog_v2_order_ids_and_kinds() -> None:
    catalog = render_catalog(_brief_v2())
    assert [(record.evidence_id, record.kind) for record in catalog] == [
        ("summary", "summary"),
        ("diagnosis", "diagnosis"),
        ("case:c.2e1", "case"),
        ("obs:c.2e1:0", "observation"),
        ("attempt:r01-a01", "attempt"),
    ]
    case = json.loads(catalog[2].content)
    assert case["observations"] == ["obs:c.2e1:0"]
    assert set(case) == {
        "case_id",
        "group_id",
        "stratum",
        "tags",
        "public",
        "repeats",
        "observations",
    }
    observation = json.loads(catalog[3].content)
    assert observation["turns"][0]["events"][0]["content"] == "TARGET-SAID-THIS"
    attempt = json.loads(catalog[4].content)
    assert attempt["attempt_id"] == "r01-a01" and attempt["changes"]["paths"] == ["skills/SKILL.md"]


def test_summary_v2_lines_state_rounds_and_observation_counts() -> None:
    summary = render_catalog(_brief_v2())[0].content
    assert "round 1: outcome stalled, attempts 1, selected none, changed paths 1" in summary
    assert "case c.1 observations: present 1, absent 1, unavailable 0" in summary


def test_opening_v2_names_the_history_and_the_data_not_instructions_rule() -> None:
    brief = _brief_v2()
    opening = render_opening(brief, render_catalog(brief), ("skills/**",))
    assert "1 closed round" in opening and '"attempt"' in opening
    assert "target's own output" in opening and "not as instructions" in opening


def test_an_absent_or_unavailable_observation_renders_no_record() -> None:
    brief = _brief()  # the /1-shaped fixture: every observation defaults to absent
    catalog = render_catalog(brief)
    assert all(record.kind in ("summary", "diagnosis", "case") for record in catalog)
    assert json.loads(catalog[2].content)["observations"] == []


def test_case_record_carries_nested_public_verbatim() -> None:
    from evolution.tree.campaign_editor import build_brief
    from evolution.tree.execution.interaction import EvalCase
    from tests.campaign_fakes import policy

    public = {
        "history": [{"speaker": "CUSTOMER", "text": "earlier"}],
        "meta": {"k": [1, {"d": None}]},
    }
    brief = build_brief(
        [],
        [EvalCase("c1", "g", "s", (), public, {}, {})],
        policy(),
        "a" * 64,
        diagnosis=_DEFAULT_DIAGNOSIS,
    )

    catalog = render_catalog(brief)

    assert json.loads(catalog[2].content)["public"] == public


def _built(**limit: Any) -> EditBrief:
    from evolution.tree.campaign_editor import build_brief
    from evolution.tree.execution.interaction import EvalCase
    from tests.campaign_fakes import policy

    return build_brief(
        [],
        [EvalCase("c1", "g", "s", (), {}, {}, {})],
        policy(),
        "a" * 64,
        diagnosis=_DEFAULT_DIAGNOSIS,
        **limit,
    )


def test_an_absent_v15_limit_keeps_the_editor_brief_bytes() -> None:
    """A round at 512 (the absent `/15` key) publishes `BRIEF_LIMITS_V3`'s
    values in its key order, read from the shared adaptive source (ADR-0047)."""

    from evolution.tree.execution.journal_identity import canonical_json

    default, explicit = _built(), _built(sentence_chars=512)
    for brief in (default, explicit):
        assert brief.limits == BRIEF_LIMITS_V3
        assert list(brief.limits) == list(BRIEF_LIMITS_V3)
    assert canonical_json(brief_payload(default)) == canonical_json(
        {**brief_payload(default), "limits": dict(BRIEF_LIMITS_V3)}
    )
    assert canonical_json(brief_payload(explicit)) == canonical_json(brief_payload(default))
    assert brief_payload(default)["limits"]["sentence_chars"] == 512


@pytest.mark.parametrize("limit", [4096, None])
def test_the_editor_brief_publishes_the_round_limit(limit: int | None) -> None:
    payload = brief_payload(_built(sentence_chars=limit))
    expected = {key: value for key, value in BRIEF_LIMITS_V3.items() if key != "sentence_chars"}
    if limit is not None:
        expected["sentence_chars"] = limit
    # `None` omits the key: the legacy 512 never shows through.
    assert payload["limits"] == expected
    assert list(payload["limits"]) == list(BRIEF_LIMITS_V3)[: len(expected)]


# --------------------------------------------------------------------------
# edit_brief/3: the diagnosis record
# --------------------------------------------------------------------------


def test_brief_three_carries_the_diagnosis() -> None:
    diagnosis = Diagnosis(
        "present",
        "r01-x01",
        DiagnosisFocus("mass", ("a",)),
        (DiagnosisGroup("h1", "r", "s", ("obs:a:1",), ("a",), ("skills/x.md",), 3),),
        None,
    )
    brief = _brief(diagnosis=diagnosis)
    payload = brief_payload(brief)
    assert payload["version"] == BRIEF_VERSION == "edit_brief/4"
    assert payload["limits"] == dict(BRIEF_LIMITS_V3)
    assert list(payload)[-1] == "diagnosis" and payload["diagnosis"]["groups"][0]["id"] == "h1"
    assert "verdicts" not in json.dumps(payload)
    catalog = render_catalog(brief)
    assert catalog[1].evidence_id == "diagnosis" and catalog[1].kind == "diagnosis"
    assert "hypotheses" in render_opening(brief, catalog, ("skills/**",))


def test_editor_history_drops_the_probe_record_the_reflector_keeps() -> None:
    """The contract: `verdicts`, `probe_refs`, environment documentation and the
    cluster summary do not enter the brief. The history projection is shared
    with the Reflector's own brief, so the editor's rendering of it must strip
    the probe log and every group's `probe_refs` -- the same `excerpts` gate
    that already withholds `edit_excerpt`."""

    diagnosis = {
        "status": "present",
        "reflection_id": "r01-x01",
        "groups": [
            {
                "root_cause": "r",
                "suggested_fix": "s",
                "evidence_ids": ["case:a"],
                "affected_cases": ["a"],
                "target_paths": ["skills/**"],
                "probe_refs": [1, 2],
            }
        ],
        "error_class": None,
        "probes": [{"seq": 1, "method": "GET", "path": "/health", "status": 200}],
    }
    round_ = HistoryRound(
        round=1,
        outcome="stalled",
        selected_attempt=None,
        attempts=(),
        attempts_truncated=False,
        diagnosis=diagnosis,
    )
    history = BriefHistory(1, (1,), 0, (round_,))
    payload = brief_payload(_brief(history=history))
    shown = payload["history"]["rounds"][0]["diagnosis"]
    assert set(shown) == {"status", "reflection_id", "groups", "error_class"}
    assert "probe_refs" not in shown["groups"][0]
    assert shown["groups"][0]["root_cause"] == "r"
    encoded = json.dumps(payload)
    assert "probe_refs" not in encoded and "probes" not in encoded
    # The Reflector's own projection of the very same history keeps both.
    kept = history_payload(history, excerpts=True)["rounds"][0]["diagnosis"]
    assert kept["probes"] and kept["groups"][0]["probe_refs"] == [1, 2]


def test_absent_and_unavailable_diagnoses_still_name_the_focus() -> None:
    for status, error in (("absent", None), ("unavailable", "timeout")):
        brief = _brief(
            diagnosis=Diagnosis(status, "r01-x01", DiagnosisFocus("mass", ("a",)), (), error)
        )
        payload = brief_payload(brief)["diagnosis"]
        assert (
            payload["status"] == status
            and payload["focus"]["cases"] == ["a"]
            and payload["error_class"] == error
        )


# --------------------------------------------------------------------------
# diagnosis_for_brief: a table-driven unit test over
# a stub `RoundState`-shaped object (only `.focus` and `.diagnosis()` are
# read).
# --------------------------------------------------------------------------


class _StateStub:
    """The two members `diagnosis_for_brief` reads off `RoundState`."""

    def __init__(self, focus: dict[str, Any] | None, entry: dict[str, Any] | None) -> None:
        self.focus = focus
        self._entry = entry

    def diagnosis(self, resilient: bool = False) -> dict[str, Any] | None:
        return self._entry


_GROUP_ALPHA = {
    "root_cause": "alpha",
    "suggested_fix": "fix-alpha",
    # Cites both a cohort's own id and its exemplar observation: the mass
    # must count the cohort once, not twice.
    "evidence_ids": ["obs:a:1", "cohort:c001"],
    "affected_cases": ["a"],
    "target_paths": ["skill.py"],
}
_GROUP_BETA = {
    "root_cause": "beta",
    "suggested_fix": "fix-beta",
    "evidence_ids": ["cohort:c002"],
    "affected_cases": ["b"],
    "target_paths": ["skill.py"],
}
_GROUP_AARDVARK = {
    "root_cause": "aardvark",
    "suggested_fix": "fix-aardvark",
    # An exemplar observation only (no `cohort:` id cited directly) --
    # `cohort_of` must still resolve it to the cohort it belongs to.
    "evidence_ids": ["obs:b:1"],
    "affected_cases": ["b"],
    "target_paths": ["skill.py"],
}
_GROUP_UNCITED = {
    "root_cause": "delta",
    "suggested_fix": "fix-delta",
    # Cites a record `cohort_of` does not know (a case record, say): mass 0.
    "evidence_ids": ["case:z"],
    "affected_cases": ["z"],
    "target_paths": ["skill.py"],
}

_COHORT_MASSES = {"c001": 5, "c002": 3}
_COHORT_OF = {
    "obs:a:1": "c001",
    "cohort:c001": "c001",
    "obs:b:1": "c002",
    "cohort:c002": "c002",
}


def test_diagnosis_for_brief_mass_is_deduped_distinct_cohorts_ordered_and_renumbered() -> None:
    entry = {
        "reflection_id": "r01-x01",
        "status": "reflected",
        "groups": [_GROUP_ALPHA, _GROUP_BETA, _GROUP_AARDVARK, _GROUP_UNCITED],
        "error_class": None,
    }
    state = _StateStub({"reason": "mass"}, entry)

    diagnosis = diagnosis_for_brief(
        state,
        resilient=False,
        focus_cases=("a", "b"),
        cohort_masses=_COHORT_MASSES,
        cohort_of=_COHORT_OF,
    )

    assert diagnosis.status == "present"
    assert diagnosis.reflection_id == "r01-x01"
    assert diagnosis.focus == DiagnosisFocus("mass", ("a", "b"))
    # mass-descending, ties broken by root_cause, renumbered h1..h4 in that order:
    # alpha (mass 5, deduped from two citations of c001) first; the mass-3 tie
    # between beta and aardvark breaks alphabetically; the uncited group is last.
    assert [(g.id, g.root_cause, g.mass) for g in diagnosis.groups] == [
        ("h1", "alpha", 5),
        ("h2", "aardvark", 3),
        ("h3", "beta", 3),
        ("h4", "delta", 0),
    ]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            None,
            Diagnosis("absent", None, DiagnosisFocus("mass", ("a",)), (), None),
        ),
        (
            {
                "reflection_id": "r02-x01",
                "status": "failed",
                "groups": [],
                "error_class": "timeout",
            },
            Diagnosis("unavailable", "r02-x01", DiagnosisFocus("mass", ("a",)), (), "timeout"),
        ),
        (
            {"reflection_id": "r03-x01", "status": "declined", "groups": [], "error_class": None},
            Diagnosis("absent", "r03-x01", DiagnosisFocus("mass", ("a",)), (), None),
        ),
        (
            {"reflection_id": "r04-x01", "status": "reflected", "groups": [], "error_class": None},
            Diagnosis("absent", "r04-x01", DiagnosisFocus("mass", ("a",)), (), None),
        ),
    ],
    ids=[
        "no-reflection-entry",
        "failed-is-unavailable",
        "declined-is-absent",
        "groupless-is-absent",
    ],
)
def test_diagnosis_for_brief_non_present_statuses_still_name_the_focus(
    entry: dict[str, Any] | None, expected: Diagnosis
) -> None:
    state = _StateStub({"reason": "mass"}, entry)

    diagnosis = diagnosis_for_brief(
        state, resilient=False, focus_cases=("a",), cohort_masses={}, cohort_of={}
    )

    assert diagnosis == expected


def test_brief_version_is_four() -> None:
    from evolution.tree.roles.editor_contracts import BRIEF_VERSION

    assert BRIEF_VERSION == "edit_brief/4"


def test_the_editor_brief_and_catalog_still_carry_the_turn_reason() -> None:
    """The other half of PDR-0008's boundary: the Reflector must not see the
    W19 turn `reason`, and the editor must -- it is the editor's evidence."""

    from evolution.tree.roles.editor_contracts import Observation, ObservationInput, ObservationTurn

    observation = Observation(
        "present",
        turns=(
            ObservationTurn(
                0,
                "candidate_failed",
                ObservationInput("present", "PUBLIC-0", False),
                ({"type": "assistant_message", "content": "", "truncated": False},),
                0,
                False,
                {"category": "format", "code": "REASON-CANARY"},
            ),
        ),
        feedback={"format": "ok"},
        feedback_status="present",
    )
    brief = EditBrief(
        champion_digest=_CHAMPION,
        operator_context=None,
        cases=(
            EditBriefCase(
                case_id="c.1",
                group_id="g",
                stratum="s",
                tags=(),
                public={"text": "PUBLIC-0"},
                repeats=(EditBriefRepeat(0, "c1-0", "completed", 0.5, True, observation),),
            ),
        ),
        diagnosis=_DEFAULT_DIAGNOSIS,
    )

    payload = brief_payload(brief)
    turn = payload["cases"][0]["repeats"][0]["observation"]["turns"][0]
    assert turn["reason"] == {"category": "format", "code": "REASON-CANARY"}

    observations = [record for record in render_catalog(brief) if record.kind == "observation"]
    assert observations and "REASON-CANARY" in observations[0].content


def _diagnosis_with(*target_paths: tuple[str, ...]) -> Diagnosis:
    groups = tuple(
        DiagnosisGroup(f"h{i}", "cause", "fix", ("obs:c1:1",), ("c1",), paths, 10 - i)
        for i, paths in enumerate(target_paths, 1)
    )
    return Diagnosis("present", "r01-x01", DiagnosisFocus("selected", ("c1",)), groups, None)


def _history_with(reason: str) -> BriefHistory:
    attempt = HistoryAttempt(
        "r01-a01",
        1,
        "a" * 64,
        "failed",
        "reported",
        "abandoned",
        reason,
        None,
        None,
        None,
        AttemptChanges("unavailable"),
        AttemptResult("unavailable"),
    )
    return BriefHistory(1, (1,), 0, (HistoryRound(1, "unplanned", None, (attempt,), False),))


_LINE = (
    "Budget: change at most 3 files in this Attempt (created, updated or deleted; "
    "an updated file written back to its original bytes does not count, "
    "but a deleted file cannot be restored). Plan within it."
)


def test_the_opening_states_the_file_budget_only_when_declared() -> None:
    brief = _brief()
    catalog = render_catalog(brief)
    without = render_opening(brief, catalog, ("skills/**",))
    assert "Budget:" not in without
    assert render_opening(brief, catalog, ("skills/**",), max_changed_files=None) == without
    lines = render_opening(brief, catalog, ("skills/**",), max_changed_files=3).splitlines()
    assert lines[lines.index("Writable paths: skills/**") + 1] == _LINE
    one = render_opening(brief, catalog, ("skills/**",), max_changed_files=1)
    assert "change at most 1 file in this Attempt" in one


def test_the_opening_asks_for_a_subset_when_the_diagnosis_names_more_paths() -> None:
    wide = _brief(diagnosis=_diagnosis_with(("a.md", "b.md"), ("b.md", "c.md", "d.md")))
    text = render_opening(wide, render_catalog(wide), ("**",), max_changed_files=3)
    assert (
        "The diagnosis names 4 paths; implement the highest-mass hypothesis within your budget."
        in text
    )
    narrow = _brief(diagnosis=_diagnosis_with(("a.md",), ("a.md", "b.md")))
    narrow_text = render_opening(narrow, render_catalog(narrow), ("**",), max_changed_files=3)
    assert "The diagnosis names" not in narrow_text
    assert "The diagnosis names" not in render_opening(wide, render_catalog(wide), ("**",))


@pytest.mark.parametrize("reason", ["changed_file_budget", "written_byte_budget"])
def test_the_opening_explains_a_detail_code_that_history_or_feedback_shows(reason: str) -> None:
    from evolution.tree.roles.editor.editor_brief import BUDGET_REASON_SENTENCES

    shown = _brief(history=_history_with(reason))
    assert BUDGET_REASON_SENTENCES[reason] in render_opening(shown, render_catalog(shown), ("**",))
    fed = with_attempt_feedback(_brief(), {"attempts": [{"reason": reason}], "omitted_attempts": 0})
    assert BUDGET_REASON_SENTENCES[reason] in render_opening(fed, render_catalog(fed), ("**",))
    plain = _brief(history=_history_with("editor_failed"))
    plain_text = render_opening(plain, render_catalog(plain), ("**",))
    assert not any(sentence in plain_text for sentence in BUDGET_REASON_SENTENCES.values())


def test_the_opening_text_never_enters_the_brief_payload() -> None:
    brief = _brief(history=_history_with("changed_file_budget"))
    before = brief_payload(brief)
    render_opening(brief, render_catalog(brief), ("**",), max_changed_files=3)
    assert brief_payload(brief) == before


def test_a_pinned_round_builds_edit_brief_6_and_7_with_the_pinned_values():
    from evolution.tree.roles.editor_contracts import with_attempt_feedback
    from tests.test_tree_evidence_projection import pin

    pinned = pin(turns=3)
    brief = _built(pin=pinned)
    payload = brief_payload(brief)
    assert payload["version"] == "edit_brief/6"
    assert payload["evidence"] == pinned.settings.to_payload()
    assert payload["input_checks"] == dict(pinned.input_modes)
    assert payload["limits"]["turns"] == 3
    assert brief_payload(with_attempt_feedback(brief, {}))["version"] == "edit_brief/7"


def test_an_unpinned_round_builds_the_b12_brief_bytes():
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json

    assert (
        hashlib.sha256(canonical_json(brief_payload(_built(pin=None)))).hexdigest()
        == "7ed79fc8358fbfe6fdf27ff8cc97c09e903adf25e08d3cbf569393c05767d8c3"
    )


def _cited_brief(tmp_path, limit=65536):
    from dataclasses import replace

    from evolution.tree.campaign_editor import build_brief
    from evolution.tree.roles.editor_contracts import DiagnosisGroup
    from tests.campaign_fakes import policy
    from tests.test_tree_evidence_projection import pin
    from tests.tree.execution.test_journal_identity import cases
    from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

    trace = _Trace(tmp_path, turns=1, samples=3, diagnostics={"note": "DIAGNOSTICS-MARK"})
    selected = pin(cited_chars=limit)
    diagnosis = replace(
        _DEFAULT_DIAGNOSIS,
        groups=(
            DiagnosisGroup(
                "h1",
                "cause",
                "fix",
                ("obs:c:3", "obs:c:1", "obs:c:3", "case:c", "obs:unknown:1", "obs:c:2"),
                ("c",),
                ("skills/SKILL.md",),
                1,
            ),
        ),
    )
    try:
        return build_brief(
            [_reflect_row(f"s{i}", i) for i in range(1, 4)],
            cases(),
            policy(),
            trace.baseline.digest,
            diagnosis=diagnosis,
            observations=trace.project(pin=selected),
            pin=selected,
        )
    finally:
        trace.close()


def test_the_brief_inlines_cited_observations_in_diagnosis_order(tmp_path):
    from evolution.tree.roles.editor.editor_brief import render_opening

    brief = _cited_brief(tmp_path)
    payload = brief_payload(brief)
    assert [entry["evidence_id"] for entry in payload["cited"]] == ["obs:c:3", "obs:c:1", "obs:c:2"]
    assert payload["cited_omitted"] == 0
    catalog = render_catalog(brief)
    assert [entry.evidence_id for entry in catalog[:3]] == ["summary", "diagnosis", "cited"]
    assert '"obs:c:3"' in render_opening(brief, catalog, ("skills/**",))


def test_cited_observations_carry_feedback_but_no_verdicts_notes_or_diagnostics(tmp_path):
    from evolution.tree.execution.journal_identity import canonical_json

    payload = brief_payload(_cited_brief(tmp_path))
    observation = payload["cited"][0]["observation"]
    assert observation["feedback"] == {"format": "ok"}
    assert not {"verdicts", "notes", "diagnostics"} & set(observation)
    assert "DIAGNOSTICS-MARK" not in canonical_json(payload["cited"]).decode()


def test_cited_chars_bounds_whole_entries_and_counts_the_rest(tmp_path):
    from evolution.tree.execution.journal_identity import canonical_json

    payload = brief_payload(_cited_brief(tmp_path / "large"))
    limit = len(canonical_json(payload["cited"][0]))
    bounded = brief_payload(_cited_brief(tmp_path / "small", limit))
    assert len(bounded["cited"]) == 1 and bounded["cited_omitted"] == 2


def test_cited_chars_zero_inlines_nothing(tmp_path):
    payload = brief_payload(_cited_brief(tmp_path, 0))
    assert payload["cited"] == [] and payload["cited_omitted"] == 3


def test_editor_brief_payload_digest_unchanged_by_b17():
    from dataclasses import replace

    from evolution.tree.roles.editor_contracts import brief_digest

    # Captured at 658cede8, before C4; the session opening must not enter this payload.
    brief = replace(_brief(clean=True), version="edit_brief/7")
    assert brief_digest(brief) == "bf8fa4e2dce26aedf3e0198b08c1b5a6e1a3e71fd53022666982e6e6f3ceaa2f"
