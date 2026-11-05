"""Task 6: the v2 `CandidateEditor` protocol, the edit brief and the editor runner.

`build_brief` is tested against `SampleRow`s directly (the controller ruling
that amends the task brief: the baseline measurement's rows, not an S1 status
payload) and against `tests.campaign_fakes.corpus()`'s canary strings, to
prove the brief never carries `private`, `oracle`, or a scorer's non-primary
metrics. `run_editor` is tested against `tests.campaign_fakes.ScriptedEditor`
for every outcome the spec §9 / §7 terminal barrier mapping names:
`edited`/`declined`/`failed`, an escaping exception, a cooperative timeout, a
`cancelled()` signal, and a swallowed cancellation that survives the grace
(`EDITOR_GRACE_SECONDS` monkeypatched down so the test stays fast).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.tree import campaign_editor
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.campaign_editor import EditorOutcome, build_brief, run_editor, unchanged
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles import editor_contracts
from evolution.tree.roles.editor_contracts import (
    Diagnosis,
    EditBrief,
    EditRequest,
    brief_digest,
    note_digest,
)
from tests.campaign_fakes import PRIMARY, EditorScript, ScriptedEditor, corpus, policy

# A neutral diagnosis for tests that are not themselves about Task 10's
# diagnosis field: no groups, no focus (the round never opened a reflection).
_DEFAULT_DIAGNOSIS = Diagnosis("absent", None, None, (), None)


def _row(
    case_id: str,
    repeat: int,
    *,
    outcome: str | None = "rollout_finished",
    disposition: str = "score",
    metrics: dict[str, float] | None = None,
    critical_pass: bool | None = None,
) -> SampleRow:
    return SampleRow(
        case_id=case_id,
        sample_id=f"{repeat:02d}",
        repeat=repeat,
        requested_seed=12345 + repeat,
        effective_seed=12345 + repeat,
        boot_id="boot-1",
        dependency_digest="dep-digest",
        outcome=outcome,
        disposition=disposition,
        metrics=metrics,
        critical_pass=critical_pass,
    )


# -- build_brief --------------------------------------------------------------


def test_build_brief_redacts_and_orders() -> None:
    cases = corpus(2)
    campaign_policy = policy(editor_context="ctx-text")
    rows = [
        _row(cases[1].case_id, 2, metrics={PRIMARY: 0.9, "extra_metric": 1.0}),
        _row(cases[1].case_id, 1, metrics={PRIMARY: 0.1}),
        _row(cases[0].case_id, 1, outcome=None, disposition="not_started", metrics=None),
        _row(cases[0].case_id, 2, metrics={"extra_metric": 5.0}, critical_pass=False),
    ]

    brief = build_brief(
        rows, cases, campaign_policy, champion_digest="digest-x", diagnosis=_DEFAULT_DIAGNOSIS
    )

    assert brief.version == "edit_brief/4"
    assert brief.champion_digest == "digest-x"
    assert brief.operator_context == "ctx-text"
    assert [case.case_id for case in brief.cases] == [cases[0].case_id, cases[1].case_id]

    case0, case1 = brief.cases
    assert case0.group_id == cases[0].group_id
    assert case0.stratum == cases[0].stratum
    assert case0.tags == cases[0].tags
    assert case0.public == cases[0].public
    # The brief owns its own copy of the case mapping, never the corpus's.
    assert case0.public is not cases[0].public
    assert [repeat.repeat for repeat in case0.repeats] == [1, 2]
    assert case0.repeats[0].terminal == "not_started"
    assert case0.repeats[0].primary_score is None
    assert case0.repeats[0].critical_pass is None
    # metrics present but missing the primary key -> None, never the extra key.
    assert case0.repeats[1].primary_score is None
    assert case0.repeats[1].critical_pass is False

    assert [repeat.repeat for repeat in case1.repeats] == [1, 2]
    assert case1.repeats[0].primary_score == 0.1
    assert case1.repeats[1].primary_score == 0.9
    assert case1.repeats[1].terminal == "rollout_finished"


def test_build_brief_missing_case_has_no_repeats() -> None:
    cases = corpus(2)
    campaign_policy = policy()
    rows = [_row(cases[0].case_id, 1, metrics={PRIMARY: 0.5})]

    brief = build_brief(
        rows, cases, campaign_policy, champion_digest="digest-x", diagnosis=_DEFAULT_DIAGNOSIS
    )

    assert brief.cases[1].case_id == cases[1].case_id
    assert brief.cases[1].repeats == ()


def test_build_brief_leakage_canaries() -> None:
    cases = corpus(1)
    campaign_policy = policy()
    rows = [_row(cases[0].case_id, 1, metrics={PRIMARY: 0.42, "extra_metric": 9.0})]

    brief = build_brief(
        rows, cases, campaign_policy, champion_digest="digest-y", diagnosis=_DEFAULT_DIAGNOSIS
    )

    payload = editor_contracts._brief_payload(brief)
    encoded = canonical_json(payload).decode()

    assert "PUBLIC-CANARY" in encoded
    assert "PRIVATE-CANARY" not in encoded
    assert "ORACLE-CANARY" not in encoded
    assert "extra_metric" not in encoded
    assert '"primary_score":0.42' in encoded


# -- brief_digest / note_digest -------------------------------------------------


def test_brief_digest_stable_and_sensitive() -> None:
    cases = corpus(1)
    campaign_policy = policy()
    rows = [_row(cases[0].case_id, 1, metrics={PRIMARY: 0.5})]

    brief_a = build_brief(
        rows, cases, campaign_policy, champion_digest="digest-a", diagnosis=_DEFAULT_DIAGNOSIS
    )
    brief_b = build_brief(
        rows, cases, campaign_policy, champion_digest="digest-a", diagnosis=_DEFAULT_DIAGNOSIS
    )
    brief_c = build_brief(
        rows, cases, campaign_policy, champion_digest="digest-b", diagnosis=_DEFAULT_DIAGNOSIS
    )

    assert brief_digest(brief_a) == brief_digest(brief_b)
    assert brief_digest(brief_a) != brief_digest(brief_c)
    assert len(brief_digest(brief_a)) == 64


def test_note_digest_stable_and_sensitive() -> None:
    assert note_digest("hello") == note_digest("hello")
    assert note_digest("hello") != note_digest("world")
    assert len(note_digest("hello")) == 64


# -- run_editor -----------------------------------------------------------------


def _brief() -> EditBrief:
    return EditBrief(
        champion_digest="digest", operator_context=None, cases=(), diagnosis=_DEFAULT_DIAGNOSIS
    )


def _request(tmp_path: Path, attempt_id: str, deadline_seconds: float) -> EditRequest:
    return EditRequest(
        attempt_id=attempt_id,
        round=1,
        workspace=tmp_path,
        writable_paths=(),
        brief=_brief(),
        deadline_seconds=deadline_seconds,
    )


async def test_run_editor_edited(tmp_path: Path) -> None:
    editor = ScriptedEditor({"a1": EditorScript("edit", writes={"SKILL.md": b"new"})})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 5.0), cancelled=lambda: False)

    assert outcome == EditorOutcome("finished", "edited", None, "", False)
    assert (tmp_path / "SKILL.md").read_bytes() == b"new"


async def test_run_editor_declined(tmp_path: Path) -> None:
    editor = ScriptedEditor({"a1": EditorScript("declined", note="not needed")})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 5.0), cancelled=lambda: False)

    assert outcome == EditorOutcome("finished", "declined", None, "not needed", False)


async def test_run_editor_failed_reported(tmp_path: Path) -> None:
    editor = ScriptedEditor({"a1": EditorScript("failed", note="bad shape")})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 5.0), cancelled=lambda: False)

    assert outcome == EditorOutcome("failed", None, "reported", "bad shape", False)


async def test_run_editor_exception_escapes(tmp_path: Path) -> None:
    editor = ScriptedEditor({"a1": EditorScript("raise", note="boom")})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 5.0), cancelled=lambda: False)

    assert outcome.terminal == "failed"
    assert outcome.status is None
    assert outcome.error_class == "exception:RuntimeError"
    assert outcome.note == "boom"
    assert outcome.alive is False


async def test_run_editor_cooperative_timeout(tmp_path: Path) -> None:
    editor = ScriptedEditor({"a1": EditorScript("sleep", seconds=5.0)})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 0.05), cancelled=lambda: False)

    assert outcome == EditorOutcome("failed", None, "timeout", "", False)


async def test_run_editor_cancelled(tmp_path: Path) -> None:
    editor = ScriptedEditor({"a1": EditorScript("sleep", seconds=5.0)})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 5.0), cancelled=lambda: True)

    assert outcome == EditorOutcome("failed", None, "cancelled", "", False)


async def test_run_editor_unresponsive_after_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(campaign_editor, "EDITOR_GRACE_SECONDS", 0.2)
    editor = ScriptedEditor({"a1": EditorScript("swallow_cancel", seconds=0.05)})

    outcome = await run_editor(editor, _request(tmp_path, "a1", 0.05), cancelled=lambda: False)

    assert outcome == EditorOutcome("failed", None, "unresponsive", "", True)


# -- unchanged --------------------------------------------------------------


def test_unchanged_true_then_false_after_one_byte(tmp_path: Path) -> None:
    artifact_policy = ArtifactPolicyV1((), (), ())
    (tmp_path / "SKILL.md").write_bytes(b"hello")
    parent_digest = scan_tree(tmp_path, artifact_policy).candidate_digest

    assert unchanged(tmp_path, artifact_policy, parent_digest) is True

    (tmp_path / "SKILL.md").write_bytes(b"hellp")

    assert unchanged(tmp_path, artifact_policy, parent_digest) is False


# -- the package surface ------------------------------------------------------


def test_the_package_exports_the_v2_editor_protocol() -> None:
    """`run_campaign(editor=...)` wants the v2 protocol, so the package names it.

    W1 unexported the donor v1 protocol (spec §8), so the v2 one is the only
    editor protocol on the package surface -- under the unambiguous name it
    already had, so nothing that imported it has to change.
    """

    import evolution.tree as tree

    assert tree.CampaignCandidateEditor is editor_contracts.CandidateEditor
    assert "CampaignCandidateEditor" in tree.__all__


# -- edit_brief/3 -------------------------------------------------------------


def test_brief_v2_payload_carries_limits_history_and_observations() -> None:
    from evolution.tree.roles.editor_contracts import (
        BRIEF_LIMITS_V3,
        EMPTY_HISTORY,
        Observation,
        ObservationInput,
        ObservationTurn,
        brief_payload,
    )

    cases = corpus(1)
    rows = [
        _row(cases[0].case_id, 1, metrics={PRIMARY: 0.5}),
        _row(cases[0].case_id, 2, metrics={PRIMARY: 0.7}),
    ]
    observation = Observation(
        "present",
        turns=(
            ObservationTurn(
                0,
                "ok",
                ObservationInput("present", "PUBLIC-CANARY", False),
                ({"type": "assistant_message", "content": "answer", "truncated": False},),
                1,
                False,
            ),
        ),
        feedback={"format": "ok"},
        feedback_status="present",
    )

    brief = build_brief(
        rows,
        cases,
        policy(),
        "digest-z",
        diagnosis=_DEFAULT_DIAGNOSIS,
        observations={(cases[0].case_id, "01"): observation},
    )

    payload = brief_payload(brief)
    assert payload["version"] == "edit_brief/4"
    assert payload["limits"] == dict(BRIEF_LIMITS_V3)
    assert payload["history"] == editor_contracts.history_payload(EMPTY_HISTORY)
    first, second = payload["cases"][0]["repeats"]
    assert first["observation"]["status"] == "present"
    assert first["observation"]["turns"][0]["events"] == [
        {"type": "assistant_message", "content": "answer", "truncated": False}
    ]
    assert second["observation"]["status"] == "absent"
    assert set(first) == {
        "repeat",
        "sample_id",
        "terminal",
        "primary_score",
        "critical_pass",
        "observation",
    }
    assert set(payload) == {
        "version",
        "champion_digest",
        "operator_context",
        "limits",
        "cases",
        "history",
        "diagnosis",
    }


def test_brief_digest_covers_history_and_observations() -> None:
    from evolution.tree.roles.editor_contracts import BriefHistory, HistoryRound

    cases = corpus(1)
    rows = [_row(cases[0].case_id, 1, metrics={PRIMARY: 0.5})]
    plain = build_brief(rows, cases, policy(), "digest-z", diagnosis=_DEFAULT_DIAGNOSIS)
    with_history = build_brief(
        rows,
        cases,
        policy(),
        "digest-z",
        diagnosis=_DEFAULT_DIAGNOSIS,
        history=BriefHistory(1, (1,), 0, (HistoryRound(1, "stalled", None, (), False),)),
    )
    assert brief_digest(plain) != brief_digest(with_history)
    assert plain.version == "edit_brief/4"


# -- nested public (acquiring readiness report, 2026-09-11) ---------------------


NESTED_PUBLIC = {
    "text": "hello",
    "history": [{"speaker": "CUSTOMER", "text": "earlier", "turn": 1}],
    "meta": {"channel": {"kind": "chat"}, "flags": [True, None, 1.5]},
}


def test_brief_payload_serializes_nested_public_as_the_corpus_gave_it() -> None:
    """`EvalCase` freezes nested mappings into `mappingproxy`; the payload must
    still be plain JSON, verbatim, and carry nothing from `private`/`oracle`."""

    import hashlib
    import json

    from evolution.tree.execution.interaction import EvalCase

    case = EvalCase(
        "c1", "g1", "s1", (), NESTED_PUBLIC, {"secret": "PRIVATE-CANARY"}, {"e": "ORACLE-CANARY"}
    )
    brief = build_brief([], [case], policy(), "d" * 64, diagnosis=_DEFAULT_DIAGNOSIS)

    encoded = json.dumps(editor_contracts.brief_payload(brief))

    assert json.loads(encoded)["cases"][0]["public"] == NESTED_PUBLIC
    assert "PRIVATE-CANARY" not in encoded and "ORACLE-CANARY" not in encoded
    expected = hashlib.sha256(canonical_json(editor_contracts.brief_payload(brief))).hexdigest()
    assert editor_contracts.brief_digest(brief) == expected
