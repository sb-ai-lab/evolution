"""`editor_evidence`'s two projections of `edit_brief/2`: `project_history`, the
closed-attempt history, and `project_observations`, the per-repeat
observations bound to their scorer traces.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.campaign_schema import AttemptState, CampaignSnapshot, RoundState
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.interaction import (
    History,
    Message,
    ScoringView,
    SemanticIdentity,
    Turn,
)
from evolution.tree.execution.journal import Journal
from evolution.tree.execution.journal_identity import RunIdentityV1, scoring_input_digest
from evolution.tree.roles.editor.editor_evidence import (
    TRACE_ROOT,
    TRACE_SUBDIR,
    _scoring_records,
    feedback_of,
    project_history,
    project_observations,
    shown_event,
)
from evolution.tree.roles.editor_contracts import (
    BRIEF_LIMITS_V2,
    AttemptChanges,
    AttemptResult,
    BriefHistory,
    HistoryAttempt,
    HistoryRound,
    Observation,
    history_payload,
    observation_payload,
)
from evolution.tree.roles.reflector.reflect_evidence import project_repeats
from tests.campaign_fakes import PRIMARY, artifact, corpus, policy
from tests.tree.execution.test_journal_identity import cases, identity_payload

POLICY = ArtifactPolicyV1((), (), ())
_SHA = "b" * 64


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class _Store:
    def __init__(self, refs: dict[tuple[str, str], ArtifactRef]) -> None:
        self.refs = dict(refs)

    def published_ref(self, kind: str, artifact_id: str) -> ArtifactRef:
        from evolution.tree.artifacts.artifact_errors import ArtifactAdmissionRefused

        try:
            return self.refs[(kind, artifact_id)]
        except KeyError:
            raise ArtifactAdmissionRefused("not_published") from None


class _Rows:
    def __init__(self, rows: tuple[SampleRow, ...] | Exception) -> None:
        self.rows = rows

    def sample_rows(self, look: int) -> tuple[SampleRow, ...]:
        if isinstance(self.rows, Exception):
            raise self.rows
        return tuple(row for row in self.rows if row.repeat <= look)


def _row(
    case_id: str, repeat: int, score: float | None, *, outcome: str = "completed"
) -> SampleRow:
    return SampleRow(
        case_id=case_id,
        sample_id=f"{case_id}-{repeat}",
        repeat=repeat,
        requested_seed=repeat,
        effective_seed=repeat,
        boot_id="boot",
        dependency_digest="dep",
        outcome=outcome,
        disposition="score" if score is not None else "indeterminate",
        metrics=None if score is None else {PRIMARY: score},
        critical_pass=True if score is not None else None,
    )


def _attempt(round_number: int, index: int, **fields: Any) -> AttemptState:
    base: dict[str, Any] = dict(
        attempt_id=f"r{round_number:02d}-a{index:02d}",
        round=round_number,
        index=index,
        parent_version="v0",
        parent_digest=_SHA,
    )
    base.update(fields)
    return AttemptState(**base)


def _snapshot(
    rounds: dict[int, RoundState], *, linked_from: dict[str, Any] | None = None
) -> CampaignSnapshot:
    return CampaignSnapshot(
        family_version=14,
        identity={"linked_from": linked_from},
        run_id="run",
        alpha_plan={},
        rounds=rounds,
        champion=("v0", _SHA),
        champion_dependency_digest="dep",
        measurements={},
        accounted=frozenset(),
        counts={},
        denominator=0,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=len(rounds),
    )


def _round(
    number: int,
    attempts: tuple[AttemptState, ...],
    *,
    closed: bool = True,
    selected: str | None = None,
) -> RoundState:
    return RoundState(
        round=number,
        baseline_measurement_id=f"m-r{number:02d}-baseline",
        baseline_complete=True,
        attempts={a.attempt_id: a for a in attempts},
        selection=None if not closed else {"attempt_id": selected},
        closed=None if not closed else {"outcome": "promoted" if selected else "stalled"},
    )


def test_history_reduces_closed_rounds_with_changes_and_results(tmp_path: Path) -> None:
    parent = artifact(
        _tree(tmp_path / "parent", {"skills/SKILL.md": "seed\n", "README": "r\n"}), artifact_id="v0"
    )
    child = artifact(
        _tree(
            tmp_path / "child",
            {"skills/SKILL.md": "edited\n", "skills/new.md": "n\n", "README": "r\n"},
        ),
        kind="candidate",
        artifact_id="r01-a01",
    )
    cases = corpus(2)
    sealed = _attempt(
        1,
        1,
        parent_version="v0",
        parent_digest=parent.digest,
        editor="edited",
        candidate_digest=child.digest,
        dependency_digest=child.dependency_digest,
        measurement_id="m-r01-a01",
        outcome="accepted",
        reason="lower_bound_positive",
        look=3,
    )
    refused = _attempt(
        1,
        2,
        parent_version="v0",
        parent_digest=parent.digest,
        editor="edited",
        outcome="abandoned",
        reason="seal_refused",
        code="artifact_unsafe",
    )
    declined = _attempt(
        1,
        3,
        parent_version="v0",
        parent_digest=parent.digest,
        editor="declined",
        outcome="abandoned",
        reason="editor_declined",
    )
    open_round = _round(
        2, (_attempt(2, 1, parent_version="v1", parent_digest=child.digest),), closed=False
    )
    snapshot = _snapshot(
        {1: _round(1, (sealed, refused, declined), selected="r01-a01"), 2: open_round}
    )
    rows = (
        _row("case-01", 1, 0.7),
        _row("case-01", 2, None, outcome="target_failed"),
        _row("case-02", 1, 0.2),
    )

    history = project_history(
        snapshot,
        measurements={"m-r01-a01": _Rows(rows)},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child}),
        artifact_policy=POLICY,
        corpus=cases,
        policy=policy(),
    )

    assert (history.rounds_closed, history.rounds_shown, history.predecessor_rounds) == (1, (1,), 0)
    [round_one] = history.rounds
    assert (round_one.round, round_one.outcome, round_one.selected_attempt) == (
        1,
        "promoted",
        "r01-a01",
    )
    assert [a.attempt_id for a in round_one.attempts] == ["r01-a01", "r01-a02", "r01-a03"]
    first, second, third = round_one.attempts
    assert (first.editor, first.outcome, first.reason, first.look, first.code) == (
        "edited",
        "accepted",
        "lower_bound_positive",
        3,
        None,
    )
    assert first.changes.status == "present" and first.changes.paths == (
        "skills/SKILL.md",
        "skills/new.md",
    )
    assert first.result.status == "present"
    assert [c.case_id for c in first.result.cases] == ["case-01", "case-02"]
    assert [
        (r.repeat, r.terminal, r.primary_score, r.critical_pass)
        for r in first.result.cases[0].repeats
    ] == [
        (1, "completed", 0.7, True),
        (2, "target_failed", None, None),
    ]
    assert (second.outcome, second.reason, second.code) == (
        "abandoned",
        "seal_refused",
        "artifact_unsafe",
    )
    assert second.changes.status == "absent" and second.result.status == "absent"
    assert third.editor == "declined" and third.changes.status == "absent"
    payload = history_payload(history)
    assert set(payload) == {"rounds_closed", "rounds_shown", "predecessor_rounds", "rounds"}
    assert set(payload["rounds"][0]["attempts"][0]) == {
        "attempt_id",
        "index",
        "parent_digest",
        "editor",
        "editor_error",
        "outcome",
        "reason",
        "code",
        "look",
        "candidate_digest",
        "changes",
        "result",
    }
    # No `focus`/`reflections` on this round (a round `RoundState.focus is
    # None` never opened a reflection): `diagnosis` is `None`.
    assert round_one.diagnosis is None
    assert payload["rounds"][0]["diagnosis"] is None


def test_project_history_computes_diagnosis_and_edit_excerpt(tmp_path: Path) -> None:
    """A round's `RoundState.diagnosis` becomes `HistoryRound.diagnosis`,
    and `excerpts=True` fills each shown attempt's `edit_excerpt` from the same
    two published copies `_changes` verifies."""

    parent = artifact(
        _tree(tmp_path / "diag-parent", {"skills/SKILL.md": "seed\n"}), artifact_id="v0"
    )
    child = artifact(
        _tree(tmp_path / "diag-child", {"skills/SKILL.md": "edited\n"}),
        kind="candidate",
        artifact_id="r01-a01",
    )
    cases = corpus(1)
    sealed = _attempt(
        1,
        1,
        parent_version="v0",
        parent_digest=parent.digest,
        editor="edited",
        candidate_digest=child.digest,
        dependency_digest=child.dependency_digest,
        outcome="accepted",
        reason="lower_bound_positive",
        look=1,
    )
    group = {
        "root_cause": "the fixture always answers the same way",
        "suggested_fix": "vary the reply",
        "evidence_ids": ["case:case-01"],
        "affected_cases": ["case-01"],
        "target_paths": ["skills/**"],
        "probe_refs": [],
    }
    round_state = RoundState(
        round=1,
        baseline_measurement_id="m-r01-baseline",
        baseline_complete=True,
        attempts={sealed.attempt_id: sealed},
        selection={"attempt_id": "r01-a01"},
        closed={"outcome": "promoted"},
        focus={"reason": "mass"},
        reflections=(
            {
                "reflection_id": "r01-x01",
                "status": "reflected",
                "groups": [group],
                "error_class": None,
                "probes": (),
                "seq": 5,
                "operation_id": "op-1",
            },
        ),
    )
    snapshot = _snapshot({1: round_state})

    history = project_history(
        snapshot,
        measurements={},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child}),
        artifact_policy=POLICY,
        corpus=cases,
        policy=policy(),
        excerpts=True,
    )

    [round_one] = history.rounds
    assert round_one.diagnosis == {
        "status": "present",
        "reflection_id": "r01-x01",
        "groups": [group],
        "error_class": None,
        "probes": [],
    }
    [attempt] = round_one.attempts
    assert attempt.edit_excerpt is not None
    assert attempt.edit_excerpt.startswith("--- ")

    # Without `excerpts`, the editor's own shape is unchanged: no `edit_excerpt` key.
    plain = project_history(
        snapshot,
        measurements={},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child}),
        artifact_policy=POLICY,
        corpus=cases,
        policy=policy(),
    )
    [plain_attempt] = plain.rounds[0].attempts
    assert plain_attempt.edit_excerpt is None


def test_edit_excerpt_is_none_when_the_diff_itself_fails_after_verification(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A round can verify both published copies and still fail its own
    diff/scan (`_changes`'s own `ArtifactError`/`OSError`/`ValueError`
    guard): `changes.status` becomes `unavailable` with `paths ==`, and
    `edit_excerpt` must gate on that -- `None`, never `""`."""

    parent = artifact(
        _tree(tmp_path / "fail-parent", {"skills/SKILL.md": "seed\n"}), artifact_id="v0"
    )
    child = artifact(
        _tree(tmp_path / "fail-child", {"skills/SKILL.md": "edited\n"}),
        kind="candidate",
        artifact_id="r01-a01",
    )
    sealed = _attempt(
        1,
        1,
        parent_version="v0",
        parent_digest=parent.digest,
        editor="edited",
        candidate_digest=child.digest,
        dependency_digest=child.dependency_digest,
        outcome="accepted",
        reason="lower_bound_positive",
        look=1,
    )
    snapshot = _snapshot({1: _round(1, (sealed,), selected="r01-a01")})

    def raising_changes(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("diff blew up")

    monkeypatch.setattr("evolution.tree.roles.editor.editor_evidence.changes", raising_changes)

    history = project_history(
        snapshot,
        measurements={},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child}),
        artifact_policy=POLICY,
        corpus=corpus(1),
        policy=policy(),
        excerpts=True,
    )

    [attempt] = history.rounds[0].attempts
    assert attempt.changes.status == "unavailable" and attempt.changes.paths == ()
    assert attempt.edit_excerpt is None


def test_project_history_shows_only_the_newest_round_under_history_rounds(
    tmp_path: Path,
) -> None:
    """The Reflector's own `limits["history_rounds"]` cuts the rounds shown,
    overriding `limits["rounds"]` when both keys are present."""

    parent = artifact(
        _tree(tmp_path / "hr-parent", {"skills/SKILL.md": "seed\n"}), artifact_id="v0"
    )
    child1 = artifact(
        _tree(tmp_path / "hr-child1", {"skills/SKILL.md": "one\n"}),
        kind="candidate",
        artifact_id="r01-a01",
    )
    round_one = _round(1, (_attempt(1, 1, parent_version="v0", parent_digest=parent.digest),))
    round_two = _round(2, (_attempt(2, 1, parent_version="v0", parent_digest=parent.digest),))
    snapshot = _snapshot({1: round_one, 2: round_two})

    history = project_history(
        snapshot,
        measurements={},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child1}),
        artifact_policy=POLICY,
        corpus=corpus(1),
        policy=policy(),
        limits={**BRIEF_LIMITS_V2, "history_rounds": 1},
        excerpts=True,
    )

    assert history.rounds_shown == (2,)
    assert [r.round for r in history.rounds] == [2]
    assert history.rounds_closed == 2


def test_history_payload_gates_edit_excerpt_on_excerpts_and_always_shows_diagnosis() -> None:
    attempt = HistoryAttempt(
        "r01-a01",
        1,
        "p" * 64,
        "edited",
        None,
        "rejected",
        "reason",
        None,
        1,
        "c" * 64,
        AttemptChanges("absent"),
        AttemptResult("absent"),
        edit_excerpt="--- a\n+++ b\n",
    )
    diagnosis = {
        "status": "present",
        "reflection_id": "r01-x01",
        "groups": [{"root_cause": "x"}],
        "error_class": None,
        "probes": [],
    }
    round_ = HistoryRound(1, "stalled", None, (attempt,), False, diagnosis=diagnosis)
    history = BriefHistory(1, (1,), 0, (round_,))

    plain = history_payload(history)
    # The editor's projection keeps the diagnosis minus the probe record
    # `tests/tree/roles/editor/test_editor_brief.py` covers the stripping itself.
    assert plain["rounds"][0]["diagnosis"] == {
        key: value for key, value in diagnosis.items() if key != "probes"
    }
    assert "edit_excerpt" not in plain["rounds"][0]["attempts"][0]

    with_excerpts = history_payload(history, excerpts=True)
    assert with_excerpts["rounds"][0]["diagnosis"] == diagnosis
    assert with_excerpts["rounds"][0]["attempts"][0]["edit_excerpt"] == "--- a\n+++ b\n"


def test_history_uses_its_existing_reason_field_for_layered_rejections() -> None:
    attempt = HistoryAttempt(
        "r01-a01",
        1,
        "p" * 64,
        "edited",
        None,
        "rejected",
        "reject_regression",
        None,
        3,
        "c" * 64,
        AttemptChanges("absent"),
        AttemptResult("absent"),
    )
    history = BriefHistory(
        1,
        (1,),
        0,
        (HistoryRound(1, "stalled", None, (attempt,), False),),
    )

    shown = history_payload(history)["rounds"][0]["attempts"][0]

    assert shown["reason"] == "reject_regression"
    assert set(shown) == {
        "attempt_id",
        "index",
        "parent_digest",
        "editor",
        "editor_error",
        "outcome",
        "reason",
        "code",
        "look",
        "candidate_digest",
        "changes",
        "result",
    }


def test_history_marks_unavailable_sources_and_bounds_every_list(tmp_path: Path) -> None:
    parent = artifact(_tree(tmp_path / "parent", {"skills/SKILL.md": "seed\n"}), artifact_id="v0")
    child = artifact(
        _tree(tmp_path / "child", {"skills/SKILL.md": "edited\n"}),
        kind="candidate",
        artifact_id="r01-a01",
    )
    (tmp_path / "child" / "skills" / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    limits = MappingProxyType(
        {**BRIEF_LIMITS_V2, "rounds": 2, "attempts_per_round": 2, "changed_paths": 1}
    )
    attempts = tuple(
        _attempt(
            1,
            i,
            parent_digest=parent.digest,
            editor="edited",
            candidate_digest=child.digest,
            dependency_digest=child.dependency_digest,
            measurement_id=f"m-r01-a0{i}",
            outcome="rejected",
            reason="upper_bound_nonpositive",
            look=3,
        )
        for i in (1, 2, 3)
    )
    rounds = {n: _round(n, attempts if n == 1 else (), selected=None) for n in (1, 2, 3)}
    snapshot = _snapshot(rounds, linked_from={"rounds_closed": 4})

    history = project_history(
        snapshot,
        measurements={"m-r01-a01": _Rows(ValueError("journal not open"))},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child}),
        artifact_policy=POLICY,
        corpus=corpus(1),
        policy=policy(),
        limits=limits,
    )

    assert (history.rounds_closed, history.rounds_shown, history.predecessor_rounds) == (
        3,
        (2, 3),
        4,
    )
    assert [r.round for r in history.rounds] == [2, 3]
    stale = project_history(
        _snapshot({1: rounds[1]}),
        measurements={},
        store=_Store({("champion", "v0"): parent, ("candidate", "r01-a01"): child}),
        artifact_policy=POLICY,
        corpus=corpus(1),
        policy=policy(),
        limits=limits,
    )
    [round_one] = stale.rounds
    assert round_one.attempts_truncated is True and len(round_one.attempts) == 2
    a1, a2 = round_one.attempts
    assert a1.changes.status == "unavailable" and a1.result.status == "unavailable"
    assert a2.changes.status == "unavailable" and a2.result.status == "unavailable"


# -- observations -------------------------------------------------------------


def _turn(index: int, content: str, events: tuple[dict, ...], outcome: str = "ok") -> Turn:
    return Turn(index, Message(content, {"service": "FIELDS-CANARY"}), events, None, 1, outcome)  # type: ignore[arg-type]


def _events() -> tuple[dict, ...]:
    return (
        {"type": "assistant_message", "content": "answer " + "x" * 5000},
        {"type": "tool_call", "name": "search", "arguments": {"q": "ARGS-CANARY"}},
        {"type": "tool_result", "name": "search", "status": "ok", "content": "RESULT-CANARY"},
        {"type": "skill_read", "path": "skills/SKILL.md"},
        {"type": "output_truncated"},
        {"type": "thought", "text": "THOUGHT-CANARY"},
        {"type": "tool_call", "name": "bad name"},
        {"type": "assistant_message", "content": 7},
    )


class _PayloadView:
    """A `view` object the digest recomputation accepts, over raw JSON."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_payload(self) -> dict:
        return self._payload


class _Trace:
    """One scored sample: a journal with its records, a baseline copy and a trace on disk."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        environment: str = "static",
        turns: int = 2,
        feedback: dict | None = None,
        diagnostics: dict | None = None,
        events: tuple[dict, ...] = _events(),
        samples: int = 1,
        mangle: Any = None,
    ) -> None:
        self.run_root = tmp_path / "run"
        self.baseline = artifact(
            _tree(tmp_path / "baseline", {"skills/SKILL.md": "seed\n"}), artifact_id="v0"
        )
        self.case = cases()[0]
        p = identity_payload()
        p["candidate_digest"] = self.baseline.digest
        p["environment"] = dict(kind=environment, digest="envdigest")
        p["schedule"] = [
            dict(case_id="c", sample_id=f"s{index}", repeat=index, requested_seed=42)
            for index in range(1, samples + 1)
        ]
        self.scorer = p["scorer"]
        self.identity = RunIdentityV1(p)
        self.journal = Journal.create(
            tmp_path / "journal",
            "run",
            self.identity,
            cases(),
            version_resolver=lambda _: __version__,
        )
        history = History(tuple(_turn(i, f"turn {i} " + "y" * 3000, events) for i in range(turns)))
        self.view = ScoringView(history, "completed", SemanticIdentity(**p["target"]))
        # Every sample of this case scores the same baseline with the same
        # view, so one input digest binds them all; each keeps its own trace.
        self.request_ids = tuple(f"a1b{index}" * 8 for index in range(1, samples + 1))
        self.request_id = self.request_ids[0]
        # The digest is taken over the same JSON the trace carries, so a
        # `mangle` that rewrites a turn still binds and is read by `_turns`.
        view_payload = json.loads(json.dumps(self.view.to_payload()))
        if mangle is not None:
            mangle(view_payload)
        self.input_digest = scoring_input_digest(
            self.case, _PayloadView(view_payload), self.baseline.candidate_view(), self.scorer
        )
        self.input = {
            "case_id": "c",
            "public": dict(self.case.public),
            "oracle": dict(self.case.oracle),
            "view": view_payload,
        }
        stdout = {
            "metrics": {"accuracy": 1.0},
            "public_feedback": feedback if feedback is not None else {"format": "ok"},
            "details": {"note": "DETAILS-CANARY"},
        }
        if diagnostics is not None:
            # Family 8 only (ADR-0021): an unknown field under families 5-7,
            # so a trace carrying it is unavailable there.
            stdout["diagnostics"] = diagnostics
        self.directory = self.run_root / TRACE_ROOT / TRACE_SUBDIR / self.request_id
        for index, request_id in enumerate(self.request_ids, start=1):
            key = dict(case_id="c", sample_id=f"s{index}", candidate_digest=self.baseline.digest)
            self.journal.append("rollout_started", key, f"rollout{index}")
            self.journal.append(
                "rollout_finished", key | {"outcome": "completed"}, f"rollout{index}"
            )
            self.journal.append(
                "scoring_started",
                key
                | dict(
                    score_request_id=request_id,
                    scorer=self.scorer,
                    input_digest=self.input_digest,
                    input_schema_version=1,
                ),
                f"score{index}",
            )
            self.journal.append(
                "scoring_finished",
                key
                | dict(score_request_id=request_id, metrics={"accuracy": 1.0}, critical_pass=None),
                f"score{index}",
            )
            directory = self.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id
            directory.mkdir(parents=True)
            self.write_request(self.input, trace_id=request_id, directory=directory)
            (directory / "stdout").write_text(json.dumps(stdout), encoding="utf-8")
            (directory / "stderr").write_text("STDERR-CANARY\n", encoding="utf-8")

    def write_request(
        self,
        payload: dict,
        *,
        trace_id: str | None = None,
        sha: str | None = None,
        directory: Path | None = None,
    ) -> None:
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        document = {
            "scorer": self.scorer["digest"],
            "trace_id": self.request_id if trace_id is None else trace_id,
            "input": payload,
            "input_sha256": hashlib.sha256(raw).hexdigest() if sha is None else sha,
        }
        target = self.directory if directory is None else directory
        (target / "request.json").write_text(json.dumps(document), encoding="utf-8")

    def project(self, **overrides: Any) -> dict[tuple[str, str], Any]:
        kwargs: dict[str, Any] = dict(
            records=self.journal.snapshot().records,
            run_root=self.run_root,
            corpus=cases(),
            baseline=self.baseline,
        )
        kwargs.update(overrides)
        return project_observations(**kwargs)

    def close(self) -> None:
        self.journal.close()


def test_a_verified_trace_is_projected_under_the_allowlist_and_the_limits(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=10)
    try:
        observation = trace.project()[("c", "s1")]
    finally:
        trace.close()
    assert observation.status == "present"
    assert len(observation.turns) == 8 and observation.turns_truncated is True
    first = observation.turns[0]
    assert (first.index, first.outcome) == (0, "ok")
    assert first.input.status == "present" and first.input.truncated is True
    assert len(first.input.content) == 2048 and first.input.content.startswith("turn 0 ")
    assert [event["type"] for event in first.events] == [
        "assistant_message",
        "tool_call",
        "tool_result",
        "skill_read",
        "output_truncated",
    ]
    assert first.events[0]["truncated"] is True and len(first.events[0]["content"]) == 4096
    assert first.events[1] == {"type": "tool_call", "name": "search"}
    assert first.events[2] == {"type": "tool_result", "name": "search", "status": "ok"}
    assert first.events[3] == {"type": "skill_read", "path": "skills/SKILL.md"}
    assert first.events[4] == {"type": "output_truncated"}
    assert first.events_omitted == 3 and first.events_truncated is False
    assert observation.feedback == {"format": "ok"} and observation.feedback_status == "present"
    encoded = json.dumps(observation_payload(observation))
    for canary in (
        "FIELDS-CANARY",
        "ARGS-CANARY",
        "RESULT-CANARY",
        "THOUGHT-CANARY",
        "DETAILS-CANARY",
        "STDERR-CANARY",
        "α",
    ):
        assert canary not in encoded


def test_events_per_turn_is_a_cut_with_its_flag(tmp_path: Path) -> None:
    trace = _Trace(
        tmp_path,
        turns=1,
        events=tuple({"type": "tool_call", "name": f"t{i}"} for i in range(40)),
    )
    try:
        observation = trace.project()[("c", "s1")]
    finally:
        trace.close()
    assert observation.status == "present"
    [turn] = observation.turns
    assert len(turn.events) == 32
    assert turn.events_truncated is True
    assert turn.events_omitted == 0


def test_a_replaced_view_is_unavailable_even_when_well_formed(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=1)
    many = tuple({"type": "tool_call", "name": f"t{i}"} for i in range(40))
    trace.view = ScoringView(History((_turn(0, "hello", many),)), "completed", trace.view.target)
    trace.input["view"] = json.loads(json.dumps(trace.view.to_payload()))
    trace.write_request(trace.input)
    try:
        observation = trace.project()[("c", "s1")]
    finally:
        trace.close()
    # The view no longer matches the journaled digest, so the trace is unavailable...
    assert observation.status == "unavailable"


def test_scripted_input_is_withheld_and_static_input_is_present(tmp_path: Path) -> None:
    scripted = _Trace(tmp_path / "scripted", environment="scripted")
    static = _Trace(tmp_path / "static", environment="static")
    try:
        withheld = scripted.project()[("c", "s1")]
        shown = static.project()[("c", "s1")]
    finally:
        scripted.close()
        static.close()
    assert withheld.status == "present"
    assert all(
        turn.input.status == "withheld" and turn.input.content is None for turn in withheld.turns
    )
    assert "turn 0" not in json.dumps(observation_payload(withheld))
    assert shown.turns[0].input.status == "present" and shown.turns[0].input.content.startswith(
        "turn 0"
    )


def test_absent_for_a_sample_that_never_reached_the_scorer(tmp_path: Path) -> None:
    trace = _Trace(tmp_path)
    try:
        records = tuple(
            r for r in trace.journal.snapshot().records if not r.event.startswith("scoring_")
        )
        observation = trace.project(records=records)[("c", "s1")]
    finally:
        trace.close()
    assert observation.status == "absent" and observation.feedback_status == "absent"


def test_a_carried_row_is_read_through_its_source_scoring_records(tmp_path: Path) -> None:
    """ADR-0017, amended 2026-10-02: `carried` offers a sample with no scoring
    of its own the source try's records under its own key; the trace binds to
    the source record unchanged. A sample the journal scored keeps its own."""

    trace = _Trace(tmp_path, samples=2)
    try:
        records = trace.journal.snapshot().records
        scoring = {
            (r.event, r.payload["sample_id"]): r for r in records if r.event.startswith("scoring_")
        }
        own = tuple(
            r
            for r in records
            if not (r.event.startswith("scoring_") and r.payload["sample_id"] == "s2")
        )
        source = (scoring["scoring_started", "s1"], scoring["scoring_finished", "s1"])
        without = trace.project(records=own)
        carried = trace.project(records=own, carried={("c", "s2"): source})
        other = (scoring["scoring_started", "s2"], scoring["scoring_finished", "s2"])
        _, started, finished = _scoring_records(records, {("c", "s1"): other})
    finally:
        trace.close()
    assert without[("c", "s2")].status == "absent"
    assert carried[("c", "s2")].status == "present"
    assert carried[("c", "s1")] == without[("c", "s1")]
    assert started[("c", "s1")] is scoring["scoring_started", "s1"]
    assert finished[("c", "s1")] is scoring["scoring_finished", "s1"]


def test_unavailable_for_every_way_a_trace_fails_to_bind(tmp_path: Path) -> None:
    trace = _Trace(tmp_path)
    try:
        good = trace.project()[("c", "s1")]
        assert good.status == "present"

        trace.write_request(trace.input, trace_id="c3d4" * 8)
        assert trace.project()[("c", "s1")].status == "unavailable"

        trace.write_request(trace.input, sha="0" * 64)
        assert trace.project()[("c", "s1")].status == "unavailable"

        replaced = deepcopy(trace.input)
        replaced["view"]["history"]["turns"][0]["events"] = []
        trace.write_request(replaced)
        assert trace.project()[("c", "s1")].status == "unavailable"

        other = artifact(
            _tree(tmp_path / "other", {"skills/SKILL.md": "other\n"}), artifact_id="v1"
        )
        trace.write_request(trace.input)
        assert trace.project(baseline=other)[("c", "s1")].status == "unavailable"

        (trace.directory / "request.json").write_text("{not json", encoding="utf-8")
        assert trace.project()[("c", "s1")].status == "unavailable"

        (trace.directory / "request.json").unlink()
        assert trace.project()[("c", "s1")].status == "unavailable"

        trace.write_request(trace.input)
        (trace.directory / "stdout").write_text("not json", encoding="utf-8")
        again = trace.project()[("c", "s1")]
        assert again.status == "present" and again.feedback_status == "unavailable"
    finally:
        trace.close()


def test_a_pathologically_nested_trace_is_unavailable_and_never_raises(tmp_path: Path) -> None:
    """A `RecursionError` from the JSON parser is a parse failure like any other."""

    nested = "[" * 100_000 + "]" * 100_000
    trace = _Trace(tmp_path, turns=1)
    try:
        (trace.directory / "request.json").write_text(nested, encoding="utf-8")
        assert trace.project()[("c", "s1")].status == "unavailable"

        trace.write_request(trace.input)
        (trace.directory / "stdout").write_text(nested, encoding="utf-8")
        observation = trace.project()[("c", "s1")]
        assert observation.status == "present" and observation.feedback_status == "unavailable"
    finally:
        trace.close()


def _nested_object(depth: int = 50_000) -> str:
    """JSON text `depth` objects deep.

    It is built by concatenation because, at the depths these tests use,
    `json.dumps` cannot write it at all, which is the case itself. The default
    is deeper than any interpreter's encoder accepts; whether it also parses
    depends on the interpreter (see `_parses_but_cannot_be_canonicalized`).
    """

    return '{"a":' * depth + "1" + "}" * depth


def _parses_but_cannot_be_canonicalized() -> int | None:
    """A nesting depth the decoder accepts and the canonicalizing encoder rejects.

    Both limits belong to the interpreter. Python 3.11 stops both at about 994
    levels and 3.12 and 3.13 at about 9 997, so no such depth exists there;
    3.14 decodes about 52 000 levels and encodes about 29 000. The midpoint
    keeps a wide margin from both limits, which the frames above the product's
    own call shift a little. `None` when the gap is too narrow to use.
    """

    def decodes(depth: int) -> bool:
        try:
            json.loads(_nested_object(depth))
        except RecursionError:
            return False
        return True

    def encodes(depth: int) -> bool:
        value: Any = 1
        for _ in range(depth):
            value = {"a": value}
        try:
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except RecursionError:
            return False
        return True

    def limit(works: Any, high: int = 100_000) -> int:
        if works(high):
            return high
        low = 0
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (middle, high) if works(middle) else (low, middle)
        return low

    decoded, encoded = limit(decodes), limit(encodes)
    if decoded - encoded < 1_000:
        return None
    return (decoded + encoded) // 2


def test_a_trace_that_parses_but_cannot_be_canonicalized_is_unavailable(tmp_path: Path) -> None:
    """The boundary covers the re-serialization and the digest, not only the parse."""

    depth = _parses_but_cannot_be_canonicalized()
    if depth is None:
        pytest.skip(
            "this interpreter's JSON decoder accepts no nesting depth that its "
            "encoder rejects, so a trace that parses but cannot be canonicalized "
            "cannot be built (such a depth exists on Python 3.14)"
        )
    for key in ("public", "view"):
        trace = _Trace(tmp_path / key, turns=1)
        try:
            fields = {name: json.dumps(value) for name, value in trace.input.items()}
            fields[key] = _nested_object(depth)
            payload = "{" + ",".join(f'"{n}":{text}' for n, text in fields.items()) + "}"
            document = "".join(
                (
                    '{"scorer":',
                    json.dumps(trace.scorer["digest"]),
                    ',"trace_id":',
                    json.dumps(trace.request_id),
                    ',"input":',
                    payload,
                    ',"input_sha256":"',
                    "0" * 64,
                    '"}',
                )
            )
            # The premise: this document reaches the canonicalization, so the
            # `unavailable` below is the encoder's failure and not the parser's.
            assert json.loads(document)["input"]["case_id"] == "c"
            (trace.directory / "request.json").write_text(document, encoding="utf-8")
            assert trace.project()[("c", "s1")].status == "unavailable"
        finally:
            trace.close()


def test_a_turn_the_rollout_never_wrote_is_unavailable(tmp_path: Path) -> None:
    """`index` and `outcome` are taken only in the shapes `Turn` can hold."""

    edits = (
        lambda turn: turn.update(outcome="crashed"),
        lambda turn: turn.pop("outcome"),
        lambda turn: turn.update(index="0"),
        lambda turn: turn.update(index=True),
    )
    for number, edit in enumerate(edits):
        trace = _Trace(
            tmp_path / str(number),
            turns=1,
            mangle=lambda payload, edit=edit: edit(payload["history"]["turns"][0]),
        )
        try:
            observation = trace.project()[("c", "s1")]
        finally:
            trace.close()
        assert observation.status == "unavailable"
    ok = _Trace(tmp_path / "ok", turns=1)
    try:
        assert ok.project()[("c", "s1")].status == "present"
    finally:
        ok.close()


def test_feedback_grammar_keeps_codes_and_drops_prose() -> None:
    kept, omitted = feedback_of(
        {
            "format": "ok",
            "missing_field": "date",
            "flag": True,
            "count": 3,
            "ratio": 0.5,
            "sentence": "this is prose",
            "nested": {"a": 1},
            "long": "x" * 70,
            "big": 10**80,
            "bad key!": "x",
            "nan": float("nan"),
        },
        BRIEF_LIMITS_V2,
    )
    assert kept == {"count": 3, "flag": True, "format": "ok", "missing_field": "date", "ratio": 0.5}
    assert omitted == 6
    capped, over = feedback_of({f"k{i:02d}": i for i in range(20)}, BRIEF_LIMITS_V2)
    assert len(capped) == 16 and over == 4 and list(capped) == sorted(capped)


def test_an_integer_too_large_for_a_float_is_dropped_not_raised() -> None:
    """`10 ** 400` is a legal JSON integer; judging it must not reach `isfinite`."""

    assert feedback_of({"x": 10**400, "ok": 1}, BRIEF_LIMITS_V2) == ({"ok": 1}, 1)


def test_feedback_of_drops_and_counts_an_entry_holding_a_run_secret() -> None:
    secret = "scorer-token-7f3a9c2e51"
    mapping = {"judge_key": secret, secret: "ok", "format": "ok", "count": 12345678}
    assert feedback_of(mapping, BRIEF_LIMITS_V2) == (mapping, 0)
    assert feedback_of(mapping, BRIEF_LIMITS_V2, (secret,)) == (
        {"format": "ok", "count": 12345678},
        2,
    )
    assert feedback_of({"n": 12345678}, BRIEF_LIMITS_V2, ("12345678",)) == ({}, 1)


def test_an_unrepresentable_feedback_integer_leaves_the_observation_present(
    tmp_path: Path,
) -> None:
    """The scorer's own output cannot end the campaign: it is dropped and counted."""

    trace = _Trace(tmp_path, turns=1, feedback={"huge": 10**400, "format": "ok"})
    try:
        observation = trace.project()[("c", "s1")]
    finally:
        trace.close()
    assert observation.status == "present"
    assert observation.feedback_status == "present"
    assert observation.feedback == {"format": "ok"} and observation.feedback_omitted == 1


def test_the_baseline_tree_is_scanned_once_per_projection(tmp_path: Path, monkeypatch: Any) -> None:
    """`scoring_input_digest` verifies per sample; the scan is hoisted to the call."""

    scans = 0
    scan = ArtifactRef.verify

    def counted(self: ArtifactRef) -> None:
        nonlocal scans
        scans += 1
        scan(self)

    trace = _Trace(tmp_path, turns=1, samples=3)
    try:
        monkeypatch.setattr(ArtifactRef, "verify", counted)
        observations = trace.project()
    finally:
        trace.close()
    assert len(observations) == 3
    assert {observation.status for observation in observations.values()} == {"present"}
    assert scans == 1


def _reflect_row(sample_id: str, repeat: int, *, disposition: str = "score") -> SampleRow:
    return SampleRow(
        case_id="c",
        sample_id=sample_id,
        repeat=repeat,
        requested_seed=42 if disposition != "not_started" else None,
        effective_seed=42 if disposition != "not_started" else None,
        boot_id="boot",
        dependency_digest="dep",
        outcome="completed" if disposition != "not_started" else None,
        disposition=disposition,
        metrics=None if disposition == "not_started" else {PRIMARY: 1.0},
        critical_pass=None if disposition == "not_started" else True,
    )


def test_project_repeats_reads_verdicts_and_the_whole_feedback_mapping(tmp_path: Path) -> None:
    """`reflect_evidence.project_repeats` reuses `project_observations`'s trace
    authentication, but reads the trace's `verdicts` and its whole
    `public_feedback` mapping -- not the editor's closed scalar grammar, so a
    sentence with spaces survives -- and a tampered `request.json` still
    makes the repeat's trace `unavailable`, exactly as it does for the
    editor's own observation."""

    trace = _Trace(tmp_path, turns=1)
    stdout = {
        "metrics": {"accuracy": 1.0},
        "verdicts": [{"criterion": "checks_x", "passed": False, "note": "missing"}],
        "public_feedback": {"note": "the agent looked at the wrong file entirely", "code": "ok"},
        "details": {"note": "DETAILS-CANARY"},
    }
    (trace.directory / "stdout").write_text(json.dumps(stdout), encoding="utf-8")
    started_row = _reflect_row("s1", 1)
    # A row whose disposition is `not_started` never reached the scorer and is
    # skipped outright -- `project_repeats` returns one `RepeatEvidence` per
    # row that started, not one per row handed to it.
    not_started_row = _reflect_row("s2", 2, disposition="not_started")
    try:
        repeats = project_repeats(
            rows=(started_row, not_started_row),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
        assert len(repeats) == 1
        (repeat,) = repeats
        assert repeat.case_id == "c" and repeat.repeat == 1 and repeat.primary_score == 1.0
        assert repeat.trace == "present"
        assert repeat.verdicts == ({"criterion": "checks_x", "passed": False, "note": "missing"},)
        assert repeat.failed_criteria == ("checks_x",)
        assert set(repeat.feedback) == {"code", "note"}
        assert "the agent looked at the wrong file entirely" in repeat.feedback["note"]
        assert (
            " " in repeat.feedback["note"]
        )  # a sentence survives, unlike the editor's scalar grammar
        assert repeat.observation.status == "present"

        trace.write_request(trace.input, trace_id="c3d4" * 8)
        (tampered,) = project_repeats(
            rows=(started_row,),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
        assert tampered.trace == "unavailable"
        assert tampered.failed_criteria is None
        assert tampered.verdicts == ()
        assert tampered.feedback == {} and tampered.event_count == 0
    finally:
        trace.close()


def test_shown_event_row_by_row() -> None:
    assert shown_event({"type": "assistant_message", "content": "hi", "extra": 1}, 4096) == {
        "type": "assistant_message",
        "content": "hi",
        "truncated": False,
    }
    assert shown_event({"type": "assistant_message"}, 4096) is None
    assert shown_event({"type": "tool_call", "name": "a.b-c_d"}, 4096) == {
        "type": "tool_call",
        "name": "a.b-c_d",
    }
    assert shown_event({"type": "tool_call", "name": "has space"}, 4096) is None
    assert shown_event({"type": "tool_result", "name": "x", "status": "error"}, 4096) == {
        "type": "tool_result",
        "name": "x",
        "status": "error",
    }
    assert shown_event({"type": "tool_result", "name": "x"}, 4096) is None
    assert shown_event({"type": "skill_read", "path": "skills/a/b.md"}, 4096) == {
        "type": "skill_read",
        "path": "skills/a/b.md",
    }
    assert shown_event({"type": "skill_read", "path": "/etc/passwd"}, 4096) is None
    assert shown_event({"type": "skill_read", "path": "../x"}, 4096) is None
    assert shown_event({"type": "skill_read", "path": "skills/a\nb.md"}, 4096) is None
    assert shown_event({"type": "output_truncated", "bytes": 5}, 4096) == {
        "type": "output_truncated"
    }
    assert shown_event({"type": "message", "text": "x"}, 4096) is None


# -- the turn's `reason` (W19, PDR-0014) --------------------------------------


def _observation_of(
    tmp_path: Path, *, turns: list[dict[str, Any]], limits: dict[str, int] | None = None
) -> Observation:
    """One scored sample whose `view.history.turns` is exactly `turns`,
    projected through the real `project_observations` -- the digest-bound,
    journal-backed path `_Trace` drives, not the private `_turns` helper.

    Adapted from `_Trace` (single sample, `static` environment) because that
    class only ever builds turns through `_turn`/`Turn`, which forces a
    contiguous, validated shape (`ok`/`target_failed` outcome, no `reason`).
    This task's fixtures are the raw JSON a trace file actually carries --
    `candidate_failed` and a `reason` included -- so the turns are written
    straight into `view["history"]["turns"]` instead.
    """

    run_root = tmp_path / "run"
    baseline = artifact(
        _tree(tmp_path / "baseline", {"skills/SKILL.md": "seed\n"}), artifact_id="v0"
    )
    case = cases()[0]
    p = identity_payload()
    p["candidate_digest"] = baseline.digest
    p["environment"] = dict(kind="static", digest="envdigest")
    p["schedule"] = [dict(case_id="c", sample_id="s1", repeat=1, requested_seed=42)]
    scorer = p["scorer"]
    identity = RunIdentityV1(p)
    journal = Journal.create(
        tmp_path / "journal", "run", identity, cases(), version_resolver=lambda _: __version__
    )
    try:
        view_payload = {
            "history": {"turns": turns},
            "terminal": "completed",
            "target": dict(p["target"]),
        }
        input_digest = scoring_input_digest(
            case, _PayloadView(view_payload), baseline.candidate_view(), scorer
        )
        request_id = "a1b1" * 8
        request_input = {
            "case_id": "c",
            "public": dict(case.public),
            "oracle": dict(case.oracle),
            "view": view_payload,
        }
        key = dict(case_id="c", sample_id="s1", candidate_digest=baseline.digest)
        journal.append("rollout_started", key, "rollout1")
        journal.append("rollout_finished", key | {"outcome": "completed"}, "rollout1")
        journal.append(
            "scoring_started",
            key
            | dict(
                score_request_id=request_id,
                scorer=scorer,
                input_digest=input_digest,
                input_schema_version=1,
            ),
            "score1",
        )
        journal.append(
            "scoring_finished",
            key | dict(score_request_id=request_id, metrics={"accuracy": 1.0}, critical_pass=None),
            "score1",
        )
        directory = run_root / TRACE_ROOT / TRACE_SUBDIR / request_id
        directory.mkdir(parents=True)
        raw = json.dumps(
            request_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        document = {
            "scorer": scorer["digest"],
            "trace_id": request_id,
            "input": request_input,
            "input_sha256": hashlib.sha256(raw).hexdigest(),
        }
        (directory / "request.json").write_text(json.dumps(document), encoding="utf-8")
        stdout = {
            "metrics": {"accuracy": 1.0},
            "public_feedback": {"format": "ok"},
            "details": {"note": "DETAILS-CANARY"},
        }
        (directory / "stdout").write_text(json.dumps(stdout), encoding="utf-8")
        (directory / "stderr").write_text("", encoding="utf-8")
        merged_limits = {**BRIEF_LIMITS_V2, **(limits or {})}
        observations = project_observations(
            records=journal.snapshot().records,
            run_root=run_root,
            corpus=cases(),
            baseline=baseline,
            limits=merged_limits,
        )
        return observations[("c", "s1")]
    finally:
        journal.close()


def test_the_projection_shows_a_candidate_failed_turn_with_its_reason(tmp_path: Path) -> None:
    observation = _observation_of(
        tmp_path,
        turns=[
            {
                "index": 0,
                "outcome": "candidate_failed",
                "message": {"content": "q"},
                "events": [{"type": "assistant_message", "content": ""}],
                "reason": {"category": "format", "code": "no_json"},
            }
        ],
    )
    turn = observation.turns[0]
    assert turn.outcome == "candidate_failed"
    assert turn.reason == {"category": "format", "code": "no_json"}


def test_the_reason_survives_an_events_cap_that_drops_every_event(tmp_path: Path) -> None:
    # The design choice this test defends: a reason carried as an event would
    # be dropped here; a field cannot be.
    observation = _observation_of(
        tmp_path,
        turns=[
            {
                "index": 0,
                "outcome": "candidate_failed",
                "message": {"content": "q"},
                "events": [{"type": "assistant_message", "content": "x"}],
                "reason": {"category": "format", "code": "no_json"},
            }
        ],
        limits={"events_per_turn": 0},
    )
    turn = observation.turns[0]
    assert turn.events == ()
    assert turn.reason == {"category": "format", "code": "no_json"}


def test_a_turn_without_a_reason_projects_none(tmp_path: Path) -> None:
    observation = _observation_of(
        tmp_path,
        turns=[
            {
                "index": 0,
                "outcome": "ok",
                "message": {"content": "q"},
                "events": [{"type": "assistant_message", "content": "x"}],
            }
        ],
    )
    assert observation.turns[0].reason is None


def test_a_malformed_reason_projects_none_rather_than_raising(tmp_path: Path) -> None:
    # Same rule as every other field of this projection: an unusable value is
    # withheld, never rendered verbatim into the editor's text.
    observation = _observation_of(
        tmp_path,
        turns=[
            {
                "index": 0,
                "outcome": "candidate_failed",
                "message": {"content": "q"},
                "events": [{"type": "assistant_message", "content": "x"}],
                "reason": {"category": "has space", "code": "no_json"},
            }
        ],
    )
    assert observation.turns[0].reason is None


@pytest.mark.parametrize("text", ["", "evidence.", "evidence\n", '{"a": "é"}'])
def test_v16_evidence_end_is_valid_without_trailing_newline(text):
    from evolution.tree.roles.editor.chat_editor import _evidence_chunk

    assert _evidence_chunk(text, len(text), family15=True) == ("", None)


def test_v15_evidence_end_without_newline_keeps_boundary_refusal():
    from evolution.tree.roles.editor.chat_editor import ToolError, _evidence_chunk

    with pytest.raises(ToolError, match="record boundary"):
        _evidence_chunk("evidence.", 9)
    assert _evidence_chunk("evidence\n", 9) == ("", None)


@pytest.mark.parametrize("family15", [False, True])
def test_evidence_offset_inside_a_record_is_still_refused(family15):
    from evolution.tree.roles.editor.chat_editor import ToolError, _evidence_chunk

    with pytest.raises(ToolError, match="record boundary"):
        _evidence_chunk("first line\nsecond.", 3, family15=family15)


@pytest.mark.parametrize("family15", [False, True])
def test_editor_read_evidence_past_end_names_range_only_in_v16(family15):
    from evolution.tree.roles.editor.chat_editor import ToolError, _evidence_chunk

    with pytest.raises(ToolError) as caught:
        _evidence_chunk("evidence\n", 100, family15=family15)
    expected = "offset is past end of evidence"
    if family15:
        expected += "; offset must be between 0 and 9"
    assert str(caught.value) == expected
