"""Safe feedback is paired, deterministic and cannot carry refused changes."""

from dataclasses import replace

import pytest

from evolution.tree import attempt_feedback as feedback


def source(index=1, **kwargs):
    values = dict(
        attempt_id=f"r1-a{index}",
        index=index,
        assessed=False,
        outcome="abandoned",
        reason="editor_no_change",
        expected_repeats=2,
    )
    values.update(kwargs)
    return feedback.FeedbackSource(**values)


def test_none_mode_ignores_sibling_history():
    assert feedback.build_feedback(
        [source(changes_status="corrupt")], mode="none", current_index=1, exam_cases={}
    ) == {
        "attempts": [],
        "omitted_attempts": 0,
    }


def test_unpublished_changes_never_enter_feedback():
    row = source(changed_paths=("secret.md",), excerpt="forbidden", changes_status="unavailable")
    result = feedback.build_feedback([row], current_index=2, exam_cases=set())
    shown = result["attempts"][0]
    assert shown["outcome"] == "abandoned"
    assert shown["reason"] == "editor_no_change"
    assert shown["changes_status"] == "unavailable"
    assert shown["changed_paths"] == []
    assert shown["diff_excerpt"] == ""
    assert shown["assessed"] is False


def test_guard_checks_before_truncation_and_withholds_entire_change():
    row = source(
        changes_status="available",
        changed_paths=("safe.md",),
        excerpt="a" * 2100 + "private-reference",
    )
    result = feedback.build_feedback(
        [row],
        current_index=2,
        exam_cases=set(),
        forbidden=lambda text: "private-reference" in text,
    )["attempts"][0]
    assert result["changes_status"] == "withheld"
    assert result["diff_excerpt"] == ""
    assert result["changed_paths"] == []


def test_case_cap_keeps_exam_then_worst_rest_and_reports_omissions():
    pairs = tuple(feedback.ScorePair(f"r{i:02}", 1, 0.5, 0.5 - i / 100) for i in range(20))
    pairs += (feedback.ScorePair("exam-z", 1, 0.5, 0.9),)
    row = source(assessed=True, outcome="rejected", reason="inconclusive", pairs=pairs)
    result = feedback.build_feedback([row], current_index=2, exam_cases={"exam-z"})["attempts"][0]
    assert [r["case_id"] for r in result["cases"]] == ["exam-z"] + [
        f"r{i:02}" for i in range(5, 20)
    ]
    assert result["omitted_exam_cases"] == 0
    assert result["omitted_rest_cases"] == 5
    exam = result["cases"][0]
    assert exam["paired_repeats"] == 1
    assert exam["incomplete"] is True
    assert exam["delta"] == pytest.approx(0.4)


def test_pairs_use_identical_repeat_members_and_reject_duplicates():
    row = source(pairs=(feedback.ScorePair("x", 1, 0.2, 0.6), feedback.ScorePair("x", 2, 0.4, 0.6)))
    shown = feedback.build_feedback([row], current_index=2, exam_cases={"x"})["attempts"][0][
        "cases"
    ][0]
    assert shown["delta"] == pytest.approx(0.3)
    assert shown["paired_repeats"] == 2
    assert shown["incomplete"] is False
    with pytest.raises(ValueError, match="duplicate"):
        feedback.build_feedback(
            [replace(row, pairs=row.pairs + row.pairs[:1])], current_index=2, exam_cases=set()
        )


def test_only_prior_attempts_and_latest_eight_are_frozen_in_index_order():
    result = feedback.build_feedback(
        [source(i) for i in range(12, 0, -1)], current_index=11, exam_cases=set()
    )
    assert [a["attempt_id"] for a in result["attempts"]] == [f"r1-a{i}" for i in range(3, 11)]
    assert result["omitted_attempts"] == 2


def test_corrupt_published_source_is_an_integrity_failure():
    with pytest.raises(feedback.FeedbackIntegrityError):
        feedback.build_feedback(
            [source(changes_status="corrupt")], current_index=2, exam_cases=set()
        )


def test_invalid_reason_cannot_smuggle_raw_terminal_text():
    with pytest.raises(ValueError, match="reason"):
        feedback.build_feedback(
            [source(reason="raw stderr secret")], current_index=2, exam_cases=set()
        )


@pytest.mark.parametrize(
    ("reason", "rejection_layer"),
    [
        ("reject_focus", "focus"),
        ("reject_regression", "rest"),
        ("reject_global", "global"),
    ],
)
def test_current_rejection_reasons_keep_their_optional_layer(reason, rejection_layer):
    row = source(
        assessed=True,
        outcome="rejected",
        reason=reason,
        rejection_layer=rejection_layer,
    )

    shown = feedback.build_feedback([row], current_index=2, exam_cases=set())["attempts"][0]

    assert shown["reason"] == reason
    assert shown["rejection_layer"] == rejection_layer


def test_final_inconclusive_feedback_does_not_invent_a_rejection_layer():
    row = source(assessed=True, outcome="rejected", reason="inconclusive")

    shown = feedback.build_feedback([row], current_index=2, exam_cases=set())["attempts"][0]

    assert shown["reason"] == "inconclusive"
    assert "rejection_layer" not in shown


def test_current_rejection_layer_is_optional_but_must_match_when_present():
    without_layer = source(
        assessed=True,
        outcome="rejected",
        reason="reject_regression",
    )
    shown = feedback.build_feedback([without_layer], current_index=2, exam_cases=set())["attempts"][
        0
    ]
    assert "rejection_layer" not in shown

    with pytest.raises(ValueError, match="rejection layer"):
        feedback.build_feedback(
            [replace(without_layer, rejection_layer="focus")],
            current_index=2,
            exam_cases=set(),
        )

    with pytest.raises(ValueError, match="rejection layer"):
        feedback.build_feedback(
            [
                source(
                    assessed=True,
                    outcome="rejected",
                    reason="inconclusive",
                    rejection_layer="global",
                )
            ],
            current_index=2,
            exam_cases=set(),
        )


def test_byte_cap_retains_newest_whole_rows_and_omission_count():
    long_path = "/".join(["x" * 255] * 16)
    result = feedback.build_feedback(
        [
            source(i, changes_status="available", changed_paths=(long_path,) * 8)
            for i in range(1, 5)
        ],
        current_index=5,
        exam_cases=set(),
    )
    assert [row["index"] for row in result["attempts"]] == [2, 3, 4]
    assert result["omitted_attempts"] == 1


def test_only_safe_bounded_identifiers_and_finite_scores_are_accepted():
    with pytest.raises(ValueError, match="case_id"):
        feedback.build_feedback(
            [source(pairs=(feedback.ScorePair("case\nsecret", 1, 0.2, 0.3),))],
            current_index=2,
            exam_cases=set(),
        )
    with pytest.raises(ValueError, match="finite"):
        feedback.build_feedback(
            [source(pairs=(feedback.ScorePair("case", 1, float("nan"), 0.3),))],
            current_index=2,
            exam_cases=set(),
        )


def test_feedback_accepts_journal_valid_unicode_case_ids():
    case_id = "пример-1"
    assert feedback.build_feedback((), current_index=1, exam_cases={case_id}) == {
        "attempts": [],
        "omitted_attempts": 0,
    }

    row = source(assessed=True, pairs=(feedback.ScorePair(case_id, 1, 0.2, 0.3),))
    shown = feedback.build_feedback([row], current_index=2, exam_cases={case_id})["attempts"][0]
    assert shown["cases"][0]["case_id"] == case_id

    with pytest.raises(ValueError, match="exam case"):
        feedback.build_feedback((), current_index=1, exam_cases={"пример\n1"})


def test_result_is_detached_and_deterministic():
    result = feedback.build_feedback(
        [source(assessed=True, pairs=(feedback.ScorePair("case", 1, 0.2, 0.3),))],
        current_index=2,
        exam_cases={"case"},
    )
    result["attempts"][0]["cases"][0]["case_id"] = "changed"
    repeat = feedback.build_feedback(
        [source(assessed=True, pairs=(feedback.ScorePair("case", 1, 0.2, 0.3),))],
        current_index=2,
        exam_cases={"case"},
    )
    assert repeat["attempts"][0]["cases"][0]["case_id"] == "case"


def test_unmeasured_feedback_has_no_numeric_contrast_and_keeps_caps():
    source_row = source(
        outcome="rejected",
        reason="threshold_not_passed",
        not_measured=("c2",),
        pairs=(feedback.ScorePair("c1", 1, 0, 0.25),),
    )
    result = feedback.build_feedback(
        [source_row],
        current_index=2,
        exam_cases={"c1", "c2"},
        outcomes={"rejected": frozenset({"threshold_not_passed"})},
        allow_not_measured=True,
    )
    assert result["attempts"][0]["cases"][1] == dict(
        case_id="c2",
        status="not_measured",
        delta=None,
        paired_repeats=0,
        expected_repeats=2,
        incomplete=False,
    )
    with pytest.raises(ValueError):
        feedback.build_feedback([source_row], current_index=2, exam_cases=())
    with pytest.raises(ValueError):
        feedback.build_feedback(
            [replace(source_row, not_measured=("c1",))],
            current_index=2,
            exam_cases=(),
            outcomes={"rejected": frozenset({"threshold_not_passed"})},
            allow_not_measured=True,
        )


@pytest.mark.parametrize(
    "change", [{"delta": 0.0}, {"paired_repeats": 1}, {"incomplete": True}, {"status": "available"}]
)
def test_frozen_unmeasured_feedback_refuses_numeric_or_incomplete_variants(change):
    from copy import deepcopy

    from evolution.tree.campaign_schema import _validate_frozen_feedback
    from evolution.tree.storage.envelope import SchemaError

    section = feedback.build_feedback(
        [source(outcome="rejected", reason="threshold_not_passed", not_measured=("c2",))],
        current_index=2,
        exam_cases=(),
        allow_not_measured=True,
        outcomes={"rejected": frozenset({"threshold_not_passed"})},
    )
    _validate_frozen_feedback(section, "sequential", independent=True)
    with pytest.raises(SchemaError):
        _validate_frozen_feedback(section, "sequential")
    broken = deepcopy(section)
    broken["attempts"][0]["cases"][0].update(change)
    with pytest.raises(SchemaError):
        _validate_frozen_feedback(broken, "sequential", independent=True)


def test_unmeasured_cases_share_the_existing_feedback_case_cap():
    row = source(
        outcome="rejected",
        reason="threshold_not_passed",
        not_measured=tuple(f"c{i:02}" for i in range(20)),
    )
    result = feedback.build_feedback(
        [row],
        current_index=2,
        exam_cases=(),
        allow_not_measured=True,
        outcomes={"rejected": frozenset({"threshold_not_passed"})},
    )["attempts"][0]
    assert len(result["cases"]) == 16
    assert result["omitted_rest_cases"] == 4
    assert all(c["delta"] is None and not c["incomplete"] for c in result["cases"])


def test_invalid_pairs_are_rejected_before_unmeasured_overlap_is_read():
    with pytest.raises(ValueError, match="score pair is invalid"):
        feedback.build_feedback([source(pairs=("invalid",))], current_index=2, exam_cases=())
