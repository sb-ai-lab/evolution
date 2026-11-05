"""Frozen editor feedback from admitted same-round publications and measurements."""

from dataclasses import replace

import pytest

from evolution.tree.attempt_feedback import FeedbackIntegrityError
from evolution.tree.campaign_schema import RoundState
from evolution.tree.roles.editor.editor_brief import render_catalog, render_opening
from evolution.tree.roles.editor.editor_evidence import project_attempt_feedback
from evolution.tree.roles.editor_contracts import (
    BriefHistory,
    Diagnosis,
    EditBrief,
    HistoryRound,
    brief_payload,
    diagnosis_for_brief,
    diagnosis_payload,
    history_payload,
    with_attempt_feedback,
)
from tests.campaign_fakes import artifact, policy
from tests.tree.roles.editor.test_editor_evidence import (
    POLICY,
    _attempt,
    _row,
    _Rows,
    _Store,
    _tree,
)


def _sources(tmp_path):
    parent = artifact(_tree(tmp_path / "parent", {"skill.md": "before\n"}), artifact_id="v0")
    child = artifact(
        _tree(tmp_path / "child", {"skill.md": "after\n"}), kind="candidate", artifact_id="r01-a01"
    )
    attempt = _attempt(
        1,
        1,
        parent_digest=parent.digest,
        candidate_digest=child.digest,
        measurement_id="child",
        outcome="rejected",
        reason="upper_bound_nonpositive",
        looks={1: {"decision": "reject"}},
    )
    state = RoundState(1, baseline_measurement_id="parent", attempts={attempt.attempt_id: attempt})
    kwargs = dict(
        current_index=2,
        measurements={
            "parent": _Rows((_row("c1", 1, 2), _row("c1", 2, 4))),
            "child": _Rows((_row("c1", 1, 1), _row("c1", 3, 10))),
        },
        store=_Store({("champion", "v0"): parent, ("candidate", attempt.attempt_id): child}),
        artifact_policy=POLICY,
        policy=policy(),
        exam_cases=("c1",),
        mode="sequential",
        forbidden=lambda text: False,
        source_refs={attempt.attempt_id: ("terminal-1",)},
    )
    return state, kwargs, child


def test_feedback_matches_repeat_pairs_and_freezes_detached_brief(tmp_path):
    state, kwargs, child = _sources(tmp_path)
    section = project_attempt_feedback(state, **kwargs)
    row = section["attempts"][0]
    assert row["assessed"] is True
    assert row["changed_paths"] == ["skill.md"]
    assert "+after" in row["diff_excerpt"]
    assert row["cases"] == [
        {
            "case_id": "c1",
            "delta": -1.0,
            "paired_repeats": 1,
            "expected_repeats": kwargs["policy"].repeats,
            "incomplete": True,
        }
    ]
    assert row["source_refs"] == ["terminal-1"]
    brief = with_attempt_feedback(
        EditBrief("a" * 64, None, (), Diagnosis("absent", None, None, (), None)), section
    )
    section["attempts"][0]["diff_excerpt"] = "MUTATED"
    (child.root / "skill.md").write_text("workspace-like mutation")
    payload = brief_payload(brief)
    assert payload["version"] == "edit_brief/5"
    assert "MUTATED" not in str(payload)
    with pytest.raises(TypeError):
        brief.attempt_feedback["attempts"][0]["reason"] = "bad"
    catalog = render_catalog(brief)
    assert next(record for record in catalog if record.evidence_id == "attempt_feedback").content
    assert "earlier failures" in render_opening(brief, catalog, ())


def test_feedback_corrupt_admitted_publication_is_integrity_failure(tmp_path):
    state, kwargs, child = _sources(tmp_path)
    (child.root / "skill.md").unlink()
    with pytest.raises(FeedbackIntegrityError):
        project_attempt_feedback(state, **kwargs)


def test_unsealed_and_none_feedback_never_read_publications(tmp_path):
    state, kwargs, _ = _sources(tmp_path)
    attempt = next(iter(state.attempts.values()))
    state = replace(
        state,
        attempts={
            attempt.attempt_id: replace(
                attempt, candidate_digest=None, measurement_id=None, looks={}
            )
        },
    )
    kwargs["store"] = _Store({})
    assert (
        project_attempt_feedback(state, **kwargs)["attempts"][0]["changes_status"] == "unavailable"
    )
    kwargs["mode"] = "none"
    kwargs["measurements"] = {}
    assert project_attempt_feedback(state, **kwargs) == {"attempts": [], "omitted_attempts": 0}


def test_guard_sees_full_diff_before_excerpt_truncation(tmp_path):
    state, kwargs, child = _sources(tmp_path)
    (child.root / "skill.md").write_text("x" * 3000 + "SECRET_CANARY\n")
    updated = artifact(child.root, kind="candidate", artifact_id=child.artifact_id)
    attempt = next(iter(state.attempts.values()))
    kwargs["store"].refs[("candidate", attempt.attempt_id)] = updated
    state = replace(
        state, attempts={attempt.attempt_id: replace(attempt, candidate_digest=updated.digest)}
    )
    kwargs["forbidden"] = lambda text: "SECRET_CANARY" in text
    row = project_attempt_feedback(state, **kwargs)["attempts"][0]
    assert row["changes_status"] == "withheld"
    assert row["changed_paths"] == [] and row["diff_excerpt"] == ""


def test_current_and_history_diagnosis_strip_docs_and_probe_refs():
    group = dict(
        root_cause="cause",
        suggested_fix="fix",
        evidence_ids=["obs:c1:1"],
        affected_cases=["c1"],
        target_paths=["skill.md"],
        counterevidence="counter",
        regression_risks="risk",
        why_this_layer="layer",
        document_ids=["SECRET_DOC"],
        probe_refs=["SECRET_PROBE"],
    )
    state = RoundState(
        1,
        reflections=(
            {"reflection_id": "r1", "status": "reflected", "error_class": None, "groups": [group]},
        ),
    )
    diagnosis = diagnosis_for_brief(
        state, resilient=False, focus_cases=("c1",), cohort_masses={}, cohort_of={}
    )
    payload = diagnosis_payload(diagnosis)
    assert payload["groups"][0]["counterevidence"] == "counter"
    assert "SECRET" not in str(payload)
    history = BriefHistory(
        1,
        (1,),
        0,
        (
            HistoryRound(
                1, "stalled", None, (), False, dict(groups=[group], probes=["SECRET_PROBE"])
            ),
        ),
    )
    assert "SECRET" not in str(history_payload(history))
    assert "SECRET_DOC" in str(history_payload(history, excerpts=True))


def test_feedback_filters_other_round_open_and_later_attempts(tmp_path):
    state, kwargs, _ = _sources(tmp_path)
    first = next(iter(state.attempts.values()))
    others = [
        replace(first, attempt_id="other", round=2),
        replace(first, attempt_id="open", index=0, outcome=None),
        replace(first, attempt_id="later", index=3),
    ]
    state = replace(state, attempts={item.attempt_id: item for item in [first, *others]})
    assert [row["attempt_id"] for row in project_attempt_feedback(state, **kwargs)["attempts"]] == [
        first.attempt_id
    ]


@pytest.mark.parametrize(
    ("reason", "rejection_layer"),
    [
        ("reject_focus", "focus"),
        ("reject_regression", "rest"),
        ("reject_global", "global"),
    ],
)
def test_terminal_layered_rejection_reaches_the_existing_feedback_slot(
    tmp_path, reason, rejection_layer
):
    state, kwargs, _ = _sources(tmp_path)
    attempt = next(iter(state.attempts.values()))
    attempt = replace(
        attempt,
        reason=reason,
        looks={
            3: {
                "decision": reason,
                "rejection_layer": rejection_layer,
            }
        },
        look=3,
    )
    state = replace(state, attempts={attempt.attempt_id: attempt})

    section = project_attempt_feedback(state, **kwargs)
    brief = with_attempt_feedback(
        EditBrief("a" * 64, None, (), Diagnosis("absent", None, None, (), None)),
        section,
    )
    row = brief_payload(brief)["attempt_feedback"]["attempts"][0]

    assert row["reason"] == reason
    assert row["rejection_layer"] == rejection_layer


def test_final_inconclusive_feedback_has_no_rejection_layer(tmp_path):
    state, kwargs, _ = _sources(tmp_path)
    attempt = next(iter(state.attempts.values()))
    attempt = replace(
        attempt,
        reason="inconclusive",
        looks={3: {"decision": "continue"}},
        look=3,
    )
    state = replace(state, attempts={attempt.attempt_id: attempt})

    row = project_attempt_feedback(state, **kwargs)["attempts"][0]

    assert row["reason"] == "inconclusive"
    assert "rejection_layer" not in row


def test_admitted_missing_measurement_is_not_silent_no_scores(tmp_path):
    state, kwargs, _ = _sources(tmp_path)
    kwargs["measurements"] = {}
    with pytest.raises(FeedbackIntegrityError):
        project_attempt_feedback(state, **kwargs)


def test_enriched_diagnosis_keeps_admitted_rank_and_none_feedback_section():
    groups = [
        dict(
            root_cause=name,
            suggested_fix="fix",
            evidence_ids=[f"obs:{name}:1"],
            affected_cases=[name],
            target_paths=[],
            counterevidence="counter",
            regression_risks="risk",
            why_this_layer="layer",
        )
        for name in ("z", "a")
    ]
    state = RoundState(
        1,
        reflections=(
            {"reflection_id": "r1", "status": "reflected", "error_class": None, "groups": groups},
        ),
    )
    diagnosis = diagnosis_for_brief(
        state, resilient=False, focus_cases=(), cohort_masses={"c": 99}, cohort_of={"obs:a:1": "c"}
    )
    brief = EditBrief("a" * 64, None, (), diagnosis)
    one = with_attempt_feedback(brief, {"attempts": [], "omitted_attempts": 0})
    two = with_attempt_feedback(brief, {"attempts": [], "omitted_attempts": 1})
    assert diagnosis_payload(one.diagnosis) == diagnosis_payload(two.diagnosis)
    assert [group.root_cause for group in one.diagnosis.groups] == ["z", "a"]
    assert brief_payload(one)["attempt_feedback"] == {"attempts": [], "omitted_attempts": 0}
    catalog = render_catalog(one)
    rendered = next(row.content for row in catalog if row.evidence_id == "diagnosis")
    assert all(text in rendered for text in ("counter", "risk", "layer"))


@pytest.mark.parametrize(
    "edge", ["add_directory", "remove_directory", "file_to_directory", "directory_to_file"]
)
def test_feedback_handles_published_directory_changes(tmp_path, edge):
    state, kwargs, child = _sources(tmp_path)
    parent = kwargs["store"].refs[("champion", "v0")]
    if edge in {"remove_directory", "directory_to_file"}:
        _tree(parent.root, {"newdir/skill.md": "old nested\n"})
    if edge in {"add_directory", "file_to_directory"}:
        _tree(child.root, {"newdir/skill.md": "new nested\n"})
    if edge == "file_to_directory":
        (parent.root / "newdir").write_text("old file\n")
    if edge == "directory_to_file":
        (child.root / "newdir").write_text("new file\n")
    parent = artifact(parent.root, artifact_id="v0")
    child = artifact(child.root, kind="candidate", artifact_id=child.artifact_id)
    attempt = next(iter(state.attempts.values()))
    kwargs["store"].refs.update(
        {("champion", "v0"): parent, ("candidate", attempt.attempt_id): child}
    )
    state = replace(
        state,
        attempts={
            attempt.attempt_id: replace(
                attempt, parent_digest=parent.digest, candidate_digest=child.digest
            )
        },
    )

    row = project_attempt_feedback(state, **kwargs)["attempts"][0]

    assert row["changes_status"] == "available"
    assert "newdir" in row["changed_paths"]
    assert "newdir/skill.md" in row["changed_paths"]
    if edge in {"add_directory", "file_to_directory"}:
        assert "+new nested" in row["diff_excerpt"]
    else:
        assert "-old nested" in row["diff_excerpt"]
    if edge == "file_to_directory":
        assert "-old file" in row["diff_excerpt"]
    if edge == "directory_to_file":
        assert "+new file" in row["diff_excerpt"]
