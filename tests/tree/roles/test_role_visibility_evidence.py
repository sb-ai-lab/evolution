"""Controller-generated repository evidence names no hidden path.

PDR-0023 and ADR-0029 (2026-10-09 amendments): the family-14 Reflector and
Editor see `role_visibility.readable_paths ∪ artifacts.writable_paths`. Every
repository path the controller itself generates for them -- an Attempt
feedback's `changed_paths` and `diff_excerpt`, a history attempt's
`changes.paths` and `edit_excerpt`, an observation's `skill_read` events and
the Reflector's `read_paths` -- is filtered to that set. Routine choice 8:
filtering is silent; a dropped `skill_read` counts in the turn's existing
`events_omitted`, and every cap and `truncated` flag applies to the visible
list. Ordinary Target and scorer log text (an `assistant_message`, feedback)
is not censored, and grouping keeps reading the unfiltered observation
(ADR-0034).

The synthetic Candidates below differ from their Parent in hidden paths too.
A genuine run cannot produce that: admission refuses every change outside
`writable_paths` and the seal refuses any non-writable difference.
The filter is the boundary's second line, and these tests reach it directly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign_schema import RoundState
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor.editor_evidence import (
    project_attempt_feedback,
    project_history,
)
from evolution.tree.roles.editor_contracts import (
    BRIEF_LIMITS_V2,
    AttemptChanges,
    observation_payload,
)
from evolution.tree.roles.reflection_reader import ByteLruCache
from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
from evolution.tree.roles.reflector_contracts import _matches_any
from evolution.tree.roles.role_visibility import (
    RoleVisibilityView,
    visible_observation,
    visible_paths,
)
from tests.campaign_fakes import PRIMARY, artifact, corpus, policy
from tests.test_tree_editor_attempt_feedback import _sources
from tests.tree.execution.test_journal_identity import cases
from tests.tree.roles.editor.test_editor_evidence import (
    POLICY,
    _attempt,
    _reflect_row,
    _round,
    _row,
    _Rows,
    _snapshot,
    _Store,
    _Trace,
)

VIEW = RoleVisibilityView(readable_paths=("prompts/**",), writable_paths=("skills/**",))

#: The Parent: visible files, a readable file the Candidate deletes (with its
#: directory), and hidden files the Candidate rewrites, moves, re-modes or deletes.
PARENT = {
    "skills/SKILL.md": "seed\n",
    "prompts/system.md": "system\n",
    "prompts/old/a.md": "gone\n",
    "tools/run.sh": "HIDDEN-RUN\n",
    "tools/notes.md": "moved\n",
    "tools/mode.sh": "HIDDEN-MODE\n",
    "secret/old.md": "HIDDEN-OLD\n",
}
#: The Candidate: a visible edit, a new visible directory, a hidden file moved
#: into the writable set, a readable edit, a hidden rewrite, a hidden mode-only
#: change (`tools/mode.sh`, set below), a hidden create in a new hidden
#: directory and a hidden delete of a whole directory.
CHILD = {
    "skills/SKILL.md": "seed edited\n",
    "skills/sub/new.md": "new\n",
    "skills/notes.md": "moved\n",
    "prompts/system.md": "system two\n",
    "tools/run.sh": "HIDDEN-PWNED\n",
    "tools/notes.md": None,
    "tools/mode.sh": "HIDDEN-MODE\n",
    "vault/key.md": "HIDDEN-KEY\n",
}
VISIBLE_CHANGES = [
    "prompts/old",
    "prompts/old/a.md",
    "prompts/system.md",
    "skills/SKILL.md",
    "skills/notes.md",
    "skills/sub",
    "skills/sub/new.md",
]
ALL_CHANGES = sorted(
    [
        *VISIBLE_CHANGES,
        "secret",
        "secret/old.md",
        "tools/mode.sh",
        "tools/notes.md",
        "tools/run.sh",
        "vault",
        "vault/key.md",
    ]
)
#: No hidden path, hidden directory or hidden byte may appear in role evidence.
HIDDEN_STRINGS = ("tools", "secret", "vault", "HIDDEN")


def _write(root: Path, files: dict[str, str | None]) -> Path:
    for relative, text in files.items():
        if text is None:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _pair(tmp_path: Path, parent: dict[str, str | None], child: dict[str, str | None]):
    parent_ref = artifact(_write(tmp_path / "parent", parent), artifact_id="v0")
    child_root = _write(tmp_path / "child", child)
    if "tools/mode.sh" in child:
        (child_root / "tools" / "mode.sh").chmod(0o755)
    child_ref = artifact(child_root, kind="candidate", artifact_id="r01-a01")
    return parent_ref, child_ref


def _feedback_kwargs(tmp_path: Path, parent: dict, child: dict) -> tuple[RoundState, dict]:
    state, kwargs, _ = _sources(tmp_path / "base")
    parent_ref, child_ref = _pair(tmp_path, parent, child)
    attempt = next(iter(state.attempts.values()))
    attempt = replace(attempt, parent_digest=parent_ref.digest, candidate_digest=child_ref.digest)
    state = replace(state, attempts={attempt.attempt_id: attempt})
    kwargs["store"] = _Store(
        {("champion", "v0"): parent_ref, ("candidate", attempt.attempt_id): child_ref}
    )
    return state, kwargs


def _history(tmp_path: Path, parent: dict, child: dict, **options: Any):
    parent_ref, child_ref = _pair(tmp_path, parent, child)
    sealed = _attempt(
        1,
        1,
        parent_digest=parent_ref.digest,
        editor="edited",
        candidate_digest=child_ref.digest,
        dependency_digest=child_ref.dependency_digest,
        outcome="accepted",
        reason="lower_bound_positive",
        measurement_id="m-r01-a01",
        look=1,
    )
    return project_history(
        _snapshot({1: _round(1, (sealed,), selected="r01-a01")}),
        measurements={"m-r01-a01": _Rows((_row("case-01", 1, 1.0),))},
        store=_Store({("champion", "v0"): parent_ref, ("candidate", "r01-a01"): child_ref}),
        artifact_policy=POLICY,
        corpus=corpus(1),
        policy=policy(),
        **options,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _assert_no_hidden(text: str) -> None:
    for hidden in HIDDEN_STRINGS:
        assert hidden not in text, hidden


# -- the pure core --------------------------------------------------------------


def test_visible_paths_keeps_visible_files_and_the_directories_leading_to_them() -> None:
    files = [path for path, text in {**PARENT, **CHILD}.items() if text is not None]
    assert list(visible_paths(ALL_CHANGES, VIEW.visible, files)) == VISIBLE_CHANGES
    # A directory with a visible file beneath it in *either* tree is kept; an
    # empty or hidden-only directory is not.
    assert visible_paths(("skills", "vault", "empty"), VIEW.visible, ("skills/a.md",)) == (
        "skills",
    )
    assert visible_paths(("prompts/old",), VIEW.visible, ("prompts/old/a.md",)) == ("prompts/old",)
    # The order of the input list is kept; nothing is added.
    assert visible_paths(
        ("skills/b.md", "skills/a.md"), VIEW.visible, ("skills/a.md", "skills/b.md")
    ) == (
        "skills/b.md",
        "skills/a.md",
    )


def test_visible_observation_drops_only_hidden_skill_reads() -> None:
    assert visible_observation({"type": "skill_read", "path": "skills/SKILL.md"}, VIEW.visible)
    assert not visible_observation({"type": "skill_read", "path": "tools/run.sh"}, VIEW.visible)
    # Log text is not censored (PDR-0023): only the `skill_read` path is a
    # controller-rendered repository path.
    for event in (
        {"type": "assistant_message", "content": "I read tools/run.sh", "truncated": False},
        {"type": "tool_call", "name": "tools"},
        {"type": "tool_result", "name": "tools", "status": "ok"},
        {"type": "output_truncated"},
    ):
        assert visible_observation(event, VIEW.visible)


# -- Attempt feedback -------------------------------------------------------------


def test_feedback_shows_only_visible_changed_paths_and_diffs(tmp_path: Path) -> None:
    state, kwargs = _feedback_kwargs(tmp_path, PARENT, CHILD)
    unfiltered = project_attempt_feedback(state, **kwargs)["attempts"][0]
    # The control: without a view, the same sources name hidden paths.
    assert "secret/old.md" in unfiltered["changed_paths"]
    row = project_attempt_feedback(state, **kwargs, visible=VIEW.visible)["attempts"][0]
    assert row["changes_status"] == "available"
    assert row["changed_paths"] == VISIBLE_CHANGES
    assert row["changed_paths_truncated"] is False
    assert "+seed edited" in row["diff_excerpt"] and "+system two" in row["diff_excerpt"]
    _assert_no_hidden(json.dumps(row))


def test_feedback_hidden_only_changes_are_an_empty_available_list(tmp_path: Path) -> None:
    child = {**PARENT, "tools/run.sh": "HIDDEN-PWNED\n"}
    state, kwargs = _feedback_kwargs(tmp_path, PARENT, child)
    row = project_attempt_feedback(state, **kwargs, visible=VIEW.visible)["attempts"][0]
    assert (row["changes_status"], row["changed_paths"], row["diff_excerpt"]) == (
        "available",
        [],
        "",
    )
    assert (row["changed_paths_truncated"], row["diff_excerpt_truncated"]) == (False, False)


@pytest.mark.parametrize("visible_count,truncated", [(8, False), (9, True)])
def test_feedback_path_cap_counts_only_visible_paths(
    tmp_path: Path, visible_count: int, truncated: bool
) -> None:
    parent = {"skills/keep.md": "k\n", "tools/a.sh": "HIDDEN-A\n"}
    child = {
        "skills/keep.md": "k\n",
        "tools/a.sh": "HIDDEN-B\n",
        "tools/b.sh": "HIDDEN-C\n",
        **{f"skills/f{index}.md": f"{index}\n" for index in range(visible_count)},
    }
    state, kwargs = _feedback_kwargs(tmp_path, parent, child)
    row = project_attempt_feedback(state, **kwargs, visible=VIEW.visible)["attempts"][0]
    assert len(row["changed_paths"]) == 8
    assert row["changed_paths_truncated"] is truncated
    _assert_no_hidden(json.dumps(row))


#: A hidden *file* replaced by a directory that leads to a visible file: the
#: path is kept as a visible directory, and the hidden file's bytes never
#: become a diff (`docs/*.md` makes `docs/a.md` visible, never `docs` itself).
KIND_VIEW = RoleVisibilityView(readable_paths=("docs/*.md",), writable_paths=("skills/**",))
KIND_PARENT = {"skills/SKILL.md": "seed\n", "docs": "HIDDEN-KIND\n"}
KIND_CHILD = {"skills/SKILL.md": "seed\n", "docs/a.md": "visible\n"}


def test_a_hidden_file_replaced_by_a_visible_directory_shows_no_hidden_bytes(
    tmp_path: Path,
) -> None:
    state, kwargs = _feedback_kwargs(tmp_path / "feedback", KIND_PARENT, KIND_CHILD)
    row = project_attempt_feedback(state, **kwargs, visible=KIND_VIEW.visible)["attempts"][0]
    assert row["changed_paths"] == ["docs", "docs/a.md"]
    assert "+visible" in row["diff_excerpt"]
    _assert_no_hidden(json.dumps(row))
    history = _history(
        tmp_path / "history", KIND_PARENT, KIND_CHILD, excerpts=True, visible=KIND_VIEW.visible
    )
    (attempt,) = history.rounds[0].attempts
    assert attempt.changes.paths == ("docs", "docs/a.md")
    # The history excerpt reads the Parent side of `docs/a.md` through the
    # file `docs` and calls it unreadable (`binary`), as for every family.
    assert attempt.edit_excerpt is not None and "docs/a.md" in attempt.edit_excerpt
    _assert_no_hidden(repr(history))


#: `project_attempt_feedback` over PARENT/CHILD without a view, as computed at
#: Pinned pre-visibility identity bytes for families up to 13 (ADR-0029).
FEEDBACK_PIN = "11ac6eaa58f1f0c5252144177ce554c3467d0978bc9f7cc1b794a7f9419927a2"


def test_feedback_without_a_view_is_byte_identical(tmp_path: Path) -> None:
    state, kwargs = _feedback_kwargs(tmp_path, PARENT, CHILD)
    section = project_attempt_feedback(state, **kwargs)
    assert section == project_attempt_feedback(state, **kwargs, visible=None)
    assert _digest(section) == FEEDBACK_PIN


# -- history ----------------------------------------------------------------------


def test_history_shows_only_visible_changes_and_excerpt(tmp_path: Path) -> None:
    unfiltered = _history(tmp_path / "control", PARENT, CHILD, excerpts=True)
    assert "tools/run.sh" in unfiltered.rounds[0].attempts[0].changes.paths
    history = _history(tmp_path / "role", PARENT, CHILD, excerpts=True, visible=VIEW.visible)
    (attempt,) = history.rounds[0].attempts
    assert attempt.changes == AttemptChanges("present", tuple(VISIBLE_CHANGES), False)
    assert attempt.edit_excerpt is not None and "+seed edited" in attempt.edit_excerpt
    _assert_no_hidden(repr(history))


def test_history_hidden_only_changes_are_present_and_empty(tmp_path: Path) -> None:
    child = {**PARENT, "tools/run.sh": "HIDDEN-PWNED\n"}
    history = _history(tmp_path, PARENT, child, excerpts=True, visible=VIEW.visible)
    (attempt,) = history.rounds[0].attempts
    assert attempt.changes == AttemptChanges("present", (), False)
    assert attempt.edit_excerpt == ""


@pytest.mark.parametrize("cap,truncated", [(7, False), (6, True)])
def test_history_cap_and_truncated_flag_apply_to_the_visible_list(
    tmp_path: Path, cap: int, truncated: bool
) -> None:
    history = _history(
        tmp_path,
        PARENT,
        CHILD,
        limits={**BRIEF_LIMITS_V2, "changed_paths": cap},
        visible=VIEW.visible,
    )
    (attempt,) = history.rounds[0].attempts
    assert attempt.changes.paths == tuple(VISIBLE_CHANGES[:cap])
    assert attempt.changes.truncated is truncated


#: `project_history(excerpts=True)` over PARENT/CHILD without a view, as at the base.
HISTORY_PIN = "9c6faa2b2b6069d30c8f0d57c73e3df875d8dbb4c8a44d0c62397cbba48a85b5"


def test_history_without_a_view_is_byte_identical(tmp_path: Path) -> None:
    history = _history(tmp_path / "a", PARENT, CHILD, excerpts=True)
    explicit = _history(tmp_path / "b", PARENT, CHILD, excerpts=True, visible=None)
    assert history == explicit
    assert _digest(asdict(history)) == HISTORY_PIN


# -- observations ----------------------------------------------------------------

ANSWER = "I read tools/run.sh before answering"
#: One turn: a visible read, a hidden read, an answer that names the hidden
#: file (log text), and an unknown event the projection always omits.
EVENTS = (
    {"type": "skill_read", "path": "skills/SKILL.md"},
    {"type": "skill_read", "path": "tools/run.sh"},
    {"type": "assistant_message", "content": ANSWER},
    {"type": "thought", "text": "not shown"},
)


def test_observations_drop_hidden_skill_reads_and_count_them(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=1, events=EVENTS)
    try:
        unfiltered = trace.project()[("c", "s1")]
        observation = trace.project(visible=VIEW.visible)[("c", "s1")]
    finally:
        trace.close()
    (control,) = unfiltered.turns
    assert {"type": "skill_read", "path": "tools/run.sh"} in control.events
    (turn,) = observation.turns
    assert turn.events == (
        {"type": "skill_read", "path": "skills/SKILL.md"},
        {"type": "assistant_message", "content": ANSWER, "truncated": False},
    )
    assert turn.events_omitted == control.events_omitted + 1 == 2
    assert turn.events_truncated is False
    # The answer keeps the hidden path string: log text is not censored.
    encoded = json.dumps(observation_payload(observation))
    assert encoded.count("tools/run.sh") == 1 and ANSWER in encoded


def test_hidden_reads_do_not_consume_the_events_cap(tmp_path: Path) -> None:
    hidden = tuple({"type": "skill_read", "path": f"tools/t{index}.sh"} for index in range(10))
    shown = tuple({"type": "skill_read", "path": f"skills/s{index}.md"} for index in range(32))
    trace = _Trace(tmp_path, turns=1, events=hidden + shown)
    try:
        (turn,) = trace.project(visible=VIEW.visible)[("c", "s1")].turns
    finally:
        trace.close()
    # The cap (32) applies to the visible list, which fits exactly.
    assert turn.events == shown
    assert (turn.events_omitted, turn.events_truncated) == (10, False)


#: `project_observations` over EVENTS without a view, as at the base.
OBSERVATIONS_PIN = "96fbc0081b0531def43ee677cf6ead74dadcc6f1b0b3bbf83b824ab1978561ce"


def test_observations_without_a_view_are_byte_identical(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=2, events=EVENTS)
    try:
        observations = trace.project()
        assert observations == trace.project(visible=None)
    finally:
        trace.close()
    payload = {
        f"{key[0]}/{key[1]}": observation_payload(value) for key, value in observations.items()
    }
    assert _digest(payload) == OBSERVATIONS_PIN


# -- the Reflector's evidence and grouping ---------------------------------------


def _inputs(trace: _Trace, *, body_cache: ByteLruCache | None = None, **role: Any):
    """`test_tree_reflection_inputs._inputs`, with the family-14 keyword only when given."""

    return FrozenReflectionInputs(
        baseline_binding=trace.baseline.digest,
        rows=(_reflect_row("s1", 1),),
        records=trace.journal.snapshot().records,
        run_root=trace.run_root,
        corpus=cases(),
        baseline=trace.baseline,
        environment_kind="static",
        metric_primary=PRIMARY,
        reflection_metadata={"c": {"topic": "routing"}},
        families={"c": "family-a"},
        body_cache=body_cache,
        **role,
    )


def _evidence(inputs) -> dict[str, Any]:
    (record,) = inputs.registry.records
    page = inputs.read_evidence({"evidence_id": record.evidence_id})
    assert page["next_offset"] is None
    return json.loads(page["content"])


def _grouping_texts(inputs) -> list[str]:
    """Every way grouping reads the observation: the paged reader, the stream
    and the registry record's own lazy source."""

    (record,) = inputs.registry.records
    paged = inputs.read_grouping_input({"index": 0, "section": "observation"})["content"]
    streamed = "".join(
        frame["content"]
        for frame in inputs.stream_grouping_inputs({"sections": ["observation"]})
        if frame["kind"] == "data"
    )
    source = json.dumps(dict(record.sources["observation"]), sort_keys=True)
    return [paged, streamed, source]


def test_reflector_evidence_drops_hidden_reads(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=1, events=EVENTS)
    try:
        body = _evidence(_inputs(trace, role_visible=VIEW.visible))
    finally:
        trace.close()
    observation = body["observation"]
    assert observation["read_paths"] == ["skills/SKILL.md"]
    (turn,) = observation["behavior"]["turns"]
    assert {"type": "skill_read", "path": "tools/run.sh"} not in turn["events"]
    assert turn["events_omitted"] == 2
    # The answer text is log text and stays verbatim.
    assert observation["answer_text"] == [ANSWER]
    # Twice, both the answer: in `answer_text` and in the behavior's
    # `assistant_message`; never as a read.
    assert json.dumps(body).count("tools/run.sh") == 2


@pytest.mark.parametrize("grouping_first", [True, False], ids=["grouping-first", "role-first"])
def test_grouping_keeps_the_unfiltered_observation_in_either_read_order(
    tmp_path: Path, grouping_first: bool
) -> None:
    trace = _Trace(tmp_path, turns=1, events=EVENTS)
    try:
        inputs = _inputs(trace, role_visible=VIEW.visible, body_cache=ByteLruCache(1 << 20))
        if grouping_first:
            grouping = _grouping_texts(inputs)
            body = _evidence(inputs)
        else:
            body = _evidence(inputs)
            grouping = _grouping_texts(inputs)
        # Twice more, now from the warm cache.
        assert _grouping_texts(inputs) == grouping
        assert _evidence(inputs) == body
    finally:
        trace.close()
    for text in grouping:
        assert '"path": "tools/run.sh"' in text or '"path":"tools/run.sh"' in text
        assert "tools/run.sh" in json.loads(text)["read_paths"]
    assert body["observation"]["read_paths"] == ["skills/SKILL.md"]


#: `read_evidence` over EVENTS without `role_visible`, as at the base.
READ_EVIDENCE_PIN = "d4780b269123740386d9dee82ade7b3522b8830e03af3f6f13936e328c7e79e7"


def test_read_evidence_without_role_visible_is_byte_identical(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=1, events=EVENTS)
    try:
        default = _inputs(trace)
        (record,) = default.registry.records
        page = default.read_evidence({"evidence_id": record.evidence_id})
        explicit = _inputs(trace, role_visible=None).read_evidence(
            {"evidence_id": record.evidence_id}
        )
    finally:
        trace.close()
    assert page == explicit
    assert "tools/run.sh" in json.loads(page["content"])["observation"]["read_paths"]
    assert page["content_digest"] == READ_EVIDENCE_PIN


# -- PDR-0017's asymmetry is unchanged -------------------------------------------


def test_editor_observations_never_carry_verdicts_or_diagnostics(tmp_path: Path) -> None:
    trace = _Trace(tmp_path, turns=1, events=EVENTS, diagnostics={"note": "DIAGNOSTIC-CANARY"})
    try:
        observation = trace.project(visible=VIEW.visible)[("c", "s1")]
    finally:
        trace.close()
    encoded = json.dumps(observation_payload(observation))
    assert observation.status == "present"
    for canary in ("DIAGNOSTIC-CANARY", "verdicts", "DETAILS-CANARY", "diagnostics"):
        assert canary not in encoded


def test_reflector_evidence_never_carries_the_editor_turn_reason(tmp_path: Path) -> None:
    def mangle(view: dict) -> None:
        view["history"]["turns"][0].update(
            outcome="candidate_failed", reason={"category": "candidate", "code": "reasoncanary"}
        )

    trace = _Trace(tmp_path, turns=1, events=EVENTS, mangle=mangle)
    try:
        editor = trace.project(visible=VIEW.visible)[("c", "s1")]
        body = _evidence(_inputs(trace, role_visible=VIEW.visible))
    finally:
        trace.close()
    # The control: the Editor's projection carries the reason...
    assert editor.turns[0].reason == {"category": "candidate", "code": "reasoncanary"}
    # ... and the Reflector's role-rendered evidence does not.
    assert "reasoncanary" not in json.dumps(body)
    assert "reason" not in body["observation"]["behavior"]["turns"][0]


# -- target paths ----------------------------------------------------------------


def test_the_target_path_guard_refuses_an_invisible_path() -> None:
    from evolution.tree.campaign import CampaignInvariantViolated, _check_visible_targets
    from evolution.tree.roles.editor_contracts import Diagnosis, DiagnosisGroup

    def diagnosis(*targets: str) -> Diagnosis:
        group = DiagnosisGroup("h1", "r", "f", ("obs:c:1",), ("c",), targets, 1)
        return Diagnosis("present", "r01-x01", None, (group,), None)

    _check_visible_targets(diagnosis("skills/SKILL.md", "prompts/system.md"), VIEW.visible)
    with pytest.raises(CampaignInvariantViolated) as refused:
        _check_visible_targets(diagnosis("skills/SKILL.md", "tools/run.sh"), VIEW.visible)
    assert str(refused.value) == "diagnosis target path is outside role visibility"
    assert "tools" not in str(refused.value)


def test_the_target_path_guard_refuses_an_invisible_history_diagnosis_path() -> None:
    """The guard's second branch: a shown history round's diagnosis is checked
    too, while the current diagnosis names only visible paths."""

    from evolution.tree.campaign import CampaignInvariantViolated, _check_visible_targets
    from evolution.tree.roles.editor_contracts import (
        BriefHistory,
        Diagnosis,
        DiagnosisGroup,
        HistoryRound,
    )

    group = DiagnosisGroup("h1", "r", "f", ("obs:c:1",), ("c",), ("skills/SKILL.md",), 1)
    current = Diagnosis("present", "r02-x01", None, (group,), None)

    def history(*targets: str) -> BriefHistory:
        shown = HistoryRound(
            round=1,
            outcome="rejected",
            selected_attempt=None,
            attempts=(),
            attempts_truncated=False,
            diagnosis={"status": "present", "groups": [{"target_paths": list(targets)}]},
        )
        return BriefHistory(1, (1,), 0, (shown,))

    # The control: the same history over visible paths passes.
    _check_visible_targets(current, VIEW.visible, history("prompts/system.md"))
    with pytest.raises(CampaignInvariantViolated) as refused:
        _check_visible_targets(current, VIEW.visible, history("tools/run.sh"))
    assert str(refused.value) == "diagnosis target path is outside role visibility"
    assert "tools" not in str(refused.value)


@pytest.mark.parametrize(
    "writable",
    [("skills/**",), ("SKILL.md",), ("skills/*.md", "docs/guide.md"), ("**/*.py",)],
)
def test_the_target_path_guard_cannot_fire_for_an_admitted_diagnosis(writable) -> None:
    """`validate_groups` admits a target path only when the artifact matcher
    puts it in `writable_paths`; the view applies the same matcher to
    `readable ∪ writable`, so every admitted target is visible."""

    view = RoleVisibilityView(readable_paths=("prompts/**",), writable_paths=writable)
    candidates = (
        "skills/SKILL.md",
        "skills/sub/a.md",
        "SKILL.md",
        "a/SKILL.md",
        "docs/guide.md",
        "src/x.py",
        "tools/run.sh",
        "prompts/system.md",
    )
    admitted = [path for path in candidates if _matches_any(path, writable)]
    assert admitted
    for path in admitted:
        assert view.visible(path), path


# -- a genuine run ----------------------------------------------------------------

#: The hidden file the Target reports reading (`tests/...visibility_run`'s
#: hidden dependency); the readable file it also reads.
CANARY = "runtime/model.bin"
RUN_ANSWER = "the answer cites runtime/model.bin as its source"
CANARY_EVENTS = (
    {"type": "skill_read", "path": "prompts/system.md"},
    {"type": "skill_read", "path": CANARY},
    {"type": "assistant_message", "content": RUN_ANSWER},
)


def _run_fixtures():
    from tests.test_tree_campaign_family14_visibility_run import _HiddenAware

    class _ReadingTarget(_HiddenAware):
        """`_HiddenAware`, whose every turn also reports reading the readable
        prompt and the hidden dependency, and answers naming the hidden file."""

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            send = instance.send

            async def reading(session, message, request_id):
                result = await send(session, message, request_id)
                return replace(result, events=(*result.events, *CANARY_EVENTS))

            instance.send = reading
            return instance

    return _ReadingTarget


def _spy_roles(monkeypatch) -> dict[str, list[Any]]:
    """Record every Editor brief, every Reflector read and every evidence object."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.campaign import _Campaign
    from evolution.tree.roles.editor_contracts import brief_payload
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload

    seen: dict[str, list[Any]] = {"editor": [], "reflector": [], "reads": [], "inputs": []}
    real_editor = campaign_module.run_editor
    real_reflector = campaign_module.run_reflector
    real_inputs = _Campaign.adaptive_inputs_for

    async def run_editor(editor, request, **kwargs):
        seen["editor"].append(canonical_json(brief_payload(request.brief)).decode())
        return await real_editor(editor, request, **kwargs)

    async def run_reflector(reflector, request, *args, **kwargs):
        seen["reflector"].append(canonical_json(reflect_brief_payload(request.evidence)).decode())
        # A real dispatch through the Reflector's own reader session, so the
        # reader's admission and integrity rules apply as for a live role.
        for evidence_id in getattr(request.evidence, "observation_ids", {}) or {}:
            page = request.reader.dispatch("read_evidence", {"evidence_id": evidence_id})
            seen["reads"].append(page["content"])
        return await real_reflector(reflector, request, *args, **kwargs)

    def adaptive_inputs_for(self, round_number):
        inputs = real_inputs(self, round_number)
        seen["inputs"].append(inputs)
        return inputs

    monkeypatch.setattr(campaign_module, "run_editor", run_editor)
    monkeypatch.setattr(campaign_module, "run_reflector", run_reflector)
    monkeypatch.setattr(_Campaign, "adaptive_inputs_for", adaptive_inputs_for)
    return seen


def _without_answer(text: str) -> str:
    """The text with every copy of the Target's answer removed: the answer is
    log text and keeps the canary (PDR-0023), nothing else may."""

    return text.replace(RUN_ANSWER, "")


@pytest.mark.slow
@pytest.mark.parametrize("family14", [True], ids=["family-14"])
async def test_a_genuine_run_names_no_hidden_path_to_either_role(
    tmp_path: Path, monkeypatch: Any, family14: bool
) -> None:
    from tests.campaign_drivers import OBSERVE, TWO_ATTEMPTS, TWO_CASE_SCORER
    from tests.campaign_drivers import two_case_run as _two_case_run
    from tests.test_tree_campaign_family14_run import family14_run, payloads
    from tests.test_tree_campaign_family14_visibility_run import _arrange

    _arrange(tmp_path, monkeypatch, family14=family14)
    seen = _spy_roles(monkeypatch)
    # Two rounds of two Attempts with sequential feedback; `c1` keeps failing,
    # so round 2 reflects and edits again over round 1's history.
    campaign = {**TWO_ATTEMPTS, "attempt_feedback": "sequential", "rounds": 2}
    scorer = TWO_CASE_SCORER.replace('"edited" in view or "edited" in skill or ', "")
    assert scorer != TWO_CASE_SCORER
    adapter = _run_fixtures()()
    if family14:
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, {**OBSERVE, "scorer": scorer}, adapter=adapter, campaign=campaign
        )
        assert snapshot.finished["outcome"] == "completed"
    else:
        run = await _two_case_run(
            tmp_path,
            as_family_13={"gate": {"mode": "observe"}, "repeats": 1},
            terminal=None,
            adapter=adapter,
            campaign=campaign,
            scorer=scorer,
        )
    frozen = payloads(run, "attempt_input_frozen")
    assert len(frozen) == 4 and len(seen["editor"]) == 4
    # Each round's second Attempt's frozen feedback shows its first Attempt's change.
    for row in (frozen[1], frozen[3]):
        assert row["feedback"]["attempts"][0]["changed_paths"] == ["skills/SKILL.md"]
    assert len(seen["reflector"]) == 2 and seen["reads"] and seen["inputs"]
    # Round 2's Editor and Reflector both read round 1's history, its changes
    # and (the Reflector's) edit excerpts.
    editor_history = json.loads(seen["editor"][2])["history"]["rounds"][0]["attempts"]
    assert editor_history[0]["changes"]["paths"] == ["skills/SKILL.md"]
    reflector_history = json.loads(seen["reflector"][1])["history"]["rounds"][0]["attempts"]
    if family14:
        assert reflector_history[0]["changed_paths"] == ["skills/SKILL.md"]
        assert reflector_history[0]["details"] == "attempt:" + reflector_history[0]["attempt_id"]
    else:
        assert "+seed skill edited" in reflector_history[0]["edit_excerpt"]

    # Grouping reads the unfiltered observation in both families (ADR-0034).
    inputs = seen["inputs"][-1]
    for index, record in enumerate(inputs.registry.records):
        if not record.available:
            continue
        grouping = inputs.read_grouping_input({"index": index, "section": "observation"})
        assert CANARY in json.loads(grouping["content"])["read_paths"]
        assert CANARY in dict(record.sources["observation"])["read_paths"]

    role_texts = [
        *seen["editor"],
        *seen["reflector"],
        *seen["reads"],
        *(canonical_json(row["feedback"]).decode() for row in frozen),
    ]
    # The Target's answer reaches both roles verbatim in both families.
    assert any(RUN_ANSWER in text for text in seen["editor"])
    assert any(RUN_ANSWER in text for text in seen["reads"])
    if family14:
        for text in role_texts:
            assert CANARY not in _without_answer(text)
        for page in seen["reads"]:
            observation = json.loads(page)["observation"]
            if observation["status"] == "present":
                assert observation["read_paths"] == ["prompts/system.md"]
    else:
        # Families up to 13 are unchanged: the Editor brief and the
        # Reflector's evidence still render the hidden read.
        assert any(CANARY in _without_answer(text) for text in seen["editor"])
        assert any(
            CANARY in json.loads(page)["observation"]["read_paths"] for page in seen["reads"]
        )


@pytest.mark.parametrize("kind", ["builtin", "fields-fallback"])
@pytest.mark.parametrize(
    "path,has_text",
    [
        ("/read_paths", False),
        ("/behavior", False),
        ("", False),
        ("/behavior/turns/0/events/1/path", False),
        ("/behavior/turns/0/events_omitted", False),
        ("/terminal", True),
        ("/failed_criteria", True),
        ("/answer_text", True),
    ],
)
def test_family15_fields_texts_use_the_reflector_feature_boundary(tmp_path, kind, path, has_text):
    from evolution.tree.roles.grouping.model_grouping import project
    from evolution.tree.roles.grouping.reflect_grouping import FieldFeature, builtin_grouping

    trace = _Trace(tmp_path, turns=1, events=EVENTS)
    try:
        stdout = trace.directory / "stdout"
        score = json.loads(stdout.read_text())
        score["verdicts"] = [{"criterion": "route", "passed": False}]
        stdout.write_text(json.dumps(score))
        inputs = _inputs(trace, role_visible=VIEW.visible, family15=True)
        features = (FieldFeature("feature", "observation", path, ""),)
        before = builtin_grouping(inputs.registry, features, policy_identity="p")
        assert inputs.registry.records[0].sources["observation"]["read_paths"] == [
            "skills/SKILL.md",
            "tools/run.sh",
        ]
        assert _evidence(inputs)["observation"]["read_paths"] == ["skills/SKILL.md"]
        if kind == "builtin":
            after = builtin_grouping(inputs.registry, features, policy_identity="p", describe=True)
        else:
            after, outcome = project(
                inputs.registry,
                (),
                policy_identity="p",
                fallback_features=features,
                fallback=("all", "invalid_output"),
                forbidden_string_predicates=(),
                describe_fields=True,
            )
            assert outcome.kind == "fields"
        assert after.result.assignments == before.result.assignments
        assert [n.group_id for n in after.neighborhoods] == [
            n.group_id for n in before.neighborhoods
        ]
        (group,) = after.neighborhoods
        assert (group.description is not None) is has_text
        if path == "/answer_text":
            # Arbitrary Target log text is still evidence under PDR-0023 C5.3.
            assert "tools/run.sh" in group.description.label
    finally:
        trace.close()


def test_family15_default_fields_texts_keep_header_reads_lazy(tmp_path, monkeypatch):
    from evolution.tree.roles.grouping.reflect_grouping import builtin_grouping

    trace = _Trace(tmp_path, turns=1, events=EVENTS)
    try:
        stdout = trace.directory / "stdout"
        score = json.loads(stdout.read_text())
        score["verdicts"] = [{"criterion": "route", "passed": False}]
        stdout.write_text(json.dumps(score))
        inputs = _inputs(trace, role_visible=VIEW.visible, family15=True)

        def no_body(*args, **kwargs):
            raise AssertionError("default fields realized a role observation body")

        monkeypatch.setattr(inputs, "_role_section", no_body)
        grouped = builtin_grouping(inputs.registry, policy_identity="p", describe=True)
        assert grouped.neighborhoods[0].description is not None
    finally:
        trace.close()
