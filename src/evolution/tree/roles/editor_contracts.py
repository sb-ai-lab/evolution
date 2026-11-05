"""Editor inputs, results and bounded evidence serialization."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.roles.role_call import AttemptRecorder

if TYPE_CHECKING:
    from evolution.tree.baseline_traces import BaselineTraces
    from evolution.tree.campaign_schema import RoundState


BRIEF_VERSION = "edit_brief/4"


BRIEF_VERSION_V16 = "edit_brief/6"


BRIEF_FEEDBACK_VERSION_V16 = "edit_brief/7"


# The versioned constants of `edit_brief/2` (PDR-0006). Published in every
# payload as `limits` so a parser can check what it was given; changing any
# value is a new brief version. Kept: `editor_evidence`'s observation
# projection still reads this set, not `BRIEF_LIMITS_V3`.
BRIEF_LIMITS_V2: Mapping[str, int] = MappingProxyType(
    {
        "turns": 8,
        "events_per_turn": 32,
        "text_chars": 4096,
        "input_chars": 2048,
        "feedback_keys": 16,
        "feedback_value_chars": 64,
        "rounds": 3,
        "attempts_per_round": 8,
        "changed_paths": 32,
    }
)


# `edit_brief/3`: `BRIEF_LIMITS_V2` plus the diagnosis's own caps --
# `groups` bounds how many of a reflection's groups the brief carries (mass
# order, ties by `root_cause`) and `sentence_chars` bounds each group's
# `root_cause`/`suggested_fix` text, mirroring the Reflector's own limit.
# The published `edit_brief/3` value at the default limits: a brief reads both
# caps from `campaign_reflector.adaptive_limits` (`_brief_limits`, ADR-0047),
# and a `/15` round at 512 publishes exactly this map.
BRIEF_LIMITS_V3: Mapping[str, int] = MappingProxyType(
    {**BRIEF_LIMITS_V2, "groups": 8, "sentence_chars": 512}
)


# Every `status` field of the brief is one of these four.
PRESENT = "present"


ABSENT = "absent"


UNAVAILABLE = "unavailable"


WITHHELD = "withheld"


# The cap on `HistoryAttempt.edit_excerpt`: a unified diff excerpt is
# cut at this many characters, whole diff lines dropped rather than split.
EDIT_EXCERPT_CHARS = 600


@dataclass(frozen=True)
class AttemptChanges:
    """The paths a sealed attempt changed against its Parent, from two published copies."""

    status: str
    paths: tuple[str, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class AttemptResultRepeat:
    repeat: int
    terminal: str
    primary_score: float | None
    critical_pass: bool | None


@dataclass(frozen=True)
class AttemptResultCase:
    case_id: str
    repeats: tuple[AttemptResultRepeat, ...]


@dataclass(frozen=True)
class AttemptResult:
    """An attempt's measurement, projected exactly as `/1` projects the baseline."""

    status: str
    cases: tuple[AttemptResultCase, ...] = ()


@dataclass(frozen=True)
class HistoryAttempt:
    attempt_id: str
    index: int
    parent_digest: str
    editor: str | None
    editor_error: str | None
    outcome: str | None
    reason: str | None
    code: str | None
    look: int | None
    candidate_digest: str | None
    changes: AttemptChanges
    result: AttemptResult
    #: The first `EDIT_EXCERPT_CHARS` characters of a unified diff between the
    #: two published copies; `None` when the attempt has no
    #: published copy, or when the caller did not ask for excerpts.
    edit_excerpt: str | None = None


@dataclass(frozen=True)
class HistoryRound:
    round: int
    outcome: str
    selected_attempt: str | None
    attempts: tuple[HistoryAttempt, ...]
    attempts_truncated: bool
    #: `RoundState.diagnosis()`'s payload, reduced to the shape `_diagnosis_payload`
    #: (`editor_evidence.py`) produces; `None` when the round never opened a
    #: reflection.
    diagnosis: dict[str, Any] | None = None


@dataclass(frozen=True)
class BriefHistory:
    """The closed rounds available to the editor."""

    rounds_closed: int
    rounds_shown: tuple[int, ...]
    predecessor_rounds: int
    rounds: tuple[HistoryRound, ...]


EMPTY_HISTORY = BriefHistory(0, (), 0, ())


def _diagnosis_for(diagnosis: dict[str, Any] | None, *, excerpts: bool) -> dict[str, Any] | None:
    """The round's diagnosis as the caller may see it.

    The Reflector's own history (`excerpts=True`) carries the whole record.
    The editor's does not: the contract lists `probe_refs` among the things that
    "do not enter the brief", beside `verdicts`, the environment documentation
    and the cluster summary, so the probe log goes and each group is stripped
    of its `probe_refs`. The gate is `excerpts`, the flag that already
    separates the two readers of this one projection.
    """

    if diagnosis is None or excerpts:
        return diagnosis
    return {
        key: (
            [
                {
                    name: value
                    for name, value in group.items()
                    if name not in {"probe_refs", "document_ids"}
                }
                for group in raw
            ]
            if key == "groups"
            else raw
        )
        for key, raw in diagnosis.items()
        if key != "probes"
    }


def history_attempt_payload(attempt: HistoryAttempt, *, excerpts: bool = False) -> dict[str, Any]:
    """One attempt of history_payload, preserving its exact projection."""

    return {
        "attempt_id": attempt.attempt_id,
        "index": attempt.index,
        "parent_digest": attempt.parent_digest,
        "editor": attempt.editor,
        "editor_error": attempt.editor_error,
        "outcome": attempt.outcome,
        "reason": attempt.reason,
        "code": attempt.code,
        "look": attempt.look,
        "candidate_digest": attempt.candidate_digest,
        "changes": {
            "status": attempt.changes.status,
            "paths": list(attempt.changes.paths),
            "truncated": attempt.changes.truncated,
        },
        "result": {
            "status": attempt.result.status,
            "cases": [
                {
                    "case_id": case.case_id,
                    "repeats": [
                        {
                            "repeat": repeat.repeat,
                            "terminal": repeat.terminal,
                            "primary_score": repeat.primary_score,
                            "critical_pass": repeat.critical_pass,
                        }
                        for repeat in case.repeats
                    ],
                }
                for case in attempt.result.cases
            ],
        },
        **({"edit_excerpt": attempt.edit_excerpt} if excerpts else {}),
    }


def history_payload(history: BriefHistory, *, excerpts: bool = False) -> dict[str, Any]:
    """`BriefHistory` as JSON. `diagnosis` is always emitted per round (Task
    7); `edit_excerpt` is emitted per attempt only when `excerpts` is true --
    the editor never sees it, the Reflector always does. The same flag decides
    whether the diagnosis keeps its probe record (`_diagnosis_for`)."""

    return {
        "rounds_closed": history.rounds_closed,
        "rounds_shown": list(history.rounds_shown),
        "predecessor_rounds": history.predecessor_rounds,
        "rounds": [
            {
                "round": round_.round,
                "outcome": round_.outcome,
                "selected_attempt": round_.selected_attempt,
                "attempts": [
                    history_attempt_payload(attempt, excerpts=excerpts)
                    for attempt in round_.attempts
                ],
                "attempts_truncated": round_.attempts_truncated,
                "diagnosis": _diagnosis_for(round_.diagnosis, excerpts=excerpts),
            }
            for round_ in history.rounds
        ],
    }


@dataclass(frozen=True)
class ObservationInput:
    """The Environment message's text: `present` under `static`, `withheld` under `scripted`."""

    status: str
    content: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class ObservationTurn:
    index: int
    outcome: str
    input: ObservationInput
    events: tuple[Mapping[str, Any], ...]
    events_omitted: int
    events_truncated: bool
    #: The wire's structural `reason` (`{"category", "code"}`, W19/PDR-0014),
    #: carried as a field rather than an event precisely because this
    #: module's own events cap can drop every event of a turn -- a reason
    #: an editor must see for a `candidate_failed` turn cannot ride on
    #: something this same projection is allowed to omit. `None` when the
    #: trace has none, or when what it has does not pass `editor_evidence`'s
    #: withhold-on-bad rule (never raised, per that rule).
    reason: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Observation:
    """One repeat of the baseline, as its verified scorer trace shows it."""

    status: str
    turns: tuple[ObservationTurn, ...] = ()
    turns_truncated: bool = False
    feedback: Mapping[str, Any] = field(default_factory=dict)
    feedback_status: str = ABSENT
    feedback_omitted: int = 0
    full_turns: tuple[Mapping[str, Any], ...] | None = None
    grouping_turns: tuple[ObservationTurn, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "feedback", MappingProxyType(dict(self.feedback)))
        for name in ("turns", "grouping_turns"):
            turns = getattr(self, name)
            if turns is not None:
                object.__setattr__(
                    self,
                    name,
                    tuple(
                        replace(turn, events=tuple(MappingProxyType(dict(e)) for e in turn.events))
                        for turn in turns
                    ),
                )


ABSENT_OBSERVATION = Observation(ABSENT)


UNAVAILABLE_OBSERVATION = Observation(UNAVAILABLE, feedback_status=UNAVAILABLE)


def observation_payload(observation: Observation) -> dict[str, Any]:
    """The **editor-facing** serialization of one observation.

    Everything this function emits is admitted to the editor's brief. It is
    not the Reflector's serializer: `reflector_observation_payload` below is,
    and a Reflector-side caller that reaches for this one moves PDR-0008's
    visibility boundary without saying so.
    """

    if observation.full_turns is not None:
        return {
            "status": observation.status,
            "view": "full",
            "turns": [dict(turn) for turn in observation.full_turns],
            "feedback": dict(observation.feedback),
            "feedback_status": observation.feedback_status,
            "feedback_omitted": observation.feedback_omitted,
        }

    return {
        "status": observation.status,
        "turns": [
            {
                "index": turn.index,
                "outcome": turn.outcome,
                "input": {
                    "status": turn.input.status,
                    "content": turn.input.content,
                    "truncated": turn.input.truncated,
                },
                "events": [dict(event) for event in turn.events],
                "events_omitted": turn.events_omitted,
                "events_truncated": turn.events_truncated,
                "reason": dict(turn.reason) if turn.reason is not None else None,
            }
            for turn in observation.turns
        ],
        "turns_truncated": observation.turns_truncated,
        "feedback": dict(observation.feedback),
        "feedback_status": observation.feedback_status,
        "feedback_omitted": observation.feedback_omitted,
    }


#: The keys of `observation_payload` a Reflector is allowed to see (PDR-0008).
#: An **allowlist**, not a denylist, and that is the whole point: the editor's
#: payload is the one that grows, so a field added to it stays out of every
#: Reflector brief until someone names it here. The W19 `reason` is the first
#: field to be editor-only -- `reflect_brief/1` predates it and must keep
#: serializing to the same bytes it did before W19, which is what
#: `tests/tree/roles/reflector/test_reflect_brief.py` pins.
_REFLECTOR_OBSERVATION_KEYS: frozenset[str] = frozenset(
    {"status", "turns", "turns_truncated", "feedback", "feedback_status", "feedback_omitted"}
)


_REFLECTOR_TURN_KEYS: frozenset[str] = frozenset(
    {"index", "outcome", "input", "events", "events_omitted", "events_truncated"}
)


def reflector_observation_payload(observation: Observation) -> dict[str, Any]:
    """The **Reflector-facing** serialization: `observation_payload` projected
    onto the two allowlists above, in the editor payload's own key order.

    The Reflector's boundary (PDR-0008) is not "the same evidence minus a
    field we remembered to drop": it is a named set of fields, so a future
    editor-only addition fails closed here rather than leaking by default.
    """

    payload = observation_payload(observation)
    if observation.full_turns is not None:
        return payload
    projected = {key: value for key, value in payload.items() if key in _REFLECTOR_OBSERVATION_KEYS}
    projected["turns"] = [
        {key: value for key, value in turn.items() if key in _REFLECTOR_TURN_KEYS}
        for turn in payload["turns"]
    ]
    return projected


@dataclass(frozen=True)
class EditBriefRepeat:
    """One R1 terminal of the champion's sample, redacted to the primary metric."""

    repeat: int
    sample_id: str
    terminal: str
    primary_score: float | None
    critical_pass: bool | None
    observation: Observation = ABSENT_OBSERVATION


@dataclass(frozen=True)
class EditBriefCase:
    """One corpus case, in corpus order; `public` only, never `private`/`oracle`."""

    case_id: str
    group_id: str
    stratum: str
    tags: tuple[str, ...]
    public: Mapping[str, Any]
    repeats: tuple[EditBriefRepeat, ...]


@dataclass(frozen=True)
class DiagnosisFocus:
    """The round's focus, as the diagnosis names it: the cluster-selection
    reason and the case ids the round is scoped to."""

    reason: str
    cases: tuple[str, ...]


@dataclass(frozen=True)
class DiagnosisGroup:
    """One of the Reflector's claimed groups, reduced for the brief.

    `id` is `h1..hN` in mass order (ties by `root_cause`), `mass` is the sum
    of the cited cohorts' masses -- never the raw evidence count, which would
    let the editor infer a case's own repeat count from a group it never saw
    the catalog for."""

    id: str
    root_cause: str
    suggested_fix: str
    evidence_ids: tuple[str, ...]
    affected_cases: tuple[str, ...]
    target_paths: tuple[str, ...]
    mass: int
    counterevidence: str | None = None
    regression_risks: str | None = None
    why_this_layer: str | None = None


@dataclass(frozen=True)
class Diagnosis:
    """The round's diagnosis as `edit_brief/3` shows it (PDR-0007).

    `status` is one of `present` (at least one group), `absent` (a settled
    reflection with none, or no reflection at all), or `unavailable` (the
    settled reflection failed); `error_class` is set only for `unavailable`.
    `focus` is `None` only when the round never opened a reflection --
    a brief is built only after a gate scope, so in practice it is always set.
    Never carries `verdicts`, probe results, environment docs or the cluster
    summary: those stay the Reflector's own view (PDR-0007)."""

    status: str
    reflection_id: str | None
    focus: DiagnosisFocus | None
    groups: tuple[DiagnosisGroup, ...]
    error_class: str | None


def diagnosis_for_brief(
    state: RoundState,
    *,
    resilient: bool,
    focus_cases: Sequence[str],
    cohort_masses: Mapping[str, int],
    cohort_of: Mapping[str, str],
) -> Diagnosis:
    """The round's `Diagnosis`, reduced from `RoundState.diagnosis`.

    Groups are ordered by mass descending, ties by `root_cause`, and renumbered
    `h1..hN` in that order -- the id is positional, not the Reflector's own
    evidence id, so two brief builds of the same round produce the same ids.

    `mass` is the sum of the masses of the *distinct* cohorts a group's
    `evidence_ids` cite (for example: `evidence_ids: ["obs:route-03:2"]`
    with `mass: 5` -- an exemplar or contrast observation counts its whole
    cohort, not zero). `cohort_of` (`campaign.brief_for`'s own, built from
    `reflect_evidence.portion_of`) maps every citable evidence id -- a
    cohort's own `cohort:<id>` and its exemplar's/contrast's `obs:<case>:<repeat>`
    ids -- to that cohort's id; `cohort_masses` then looks up each distinct
    cohort id's mass. An evidence id `cohort_of` does not know (a case, an
    attempt, `summary`, `diagnosis`, or another cohort's own citation this
    focus never portioned) contributes nothing, and citing both a cohort's
    `cohort:<id>` and one of its own observations counts that cohort's mass
    once, not twice.

    `resilient` is the campaign's family-10 flag, passed on to
    `RoundState.diagnosis` (ADR-0023).
    """

    entry = state.diagnosis(resilient)
    focus = DiagnosisFocus(str(state.focus["reason"]), tuple(focus_cases)) if state.focus else None
    if entry is None or entry["status"] == "failed":
        return Diagnosis(
            "unavailable" if entry else "absent",
            None if entry is None else entry["reflection_id"],
            focus,
            (),
            None if entry is None else entry["error_class"],
        )
    groups = []
    for group in entry["groups"]:
        cited_cohorts = {cohort_of[e] for e in group["evidence_ids"] if e in cohort_of}
        mass = sum(cohort_masses.get(cohort_id, 0) for cohort_id in cited_cohorts)
        groups.append((mass, group))
    ordered = (
        groups
        if any("counterevidence" in group for _, group in groups)
        else sorted(groups, key=lambda item: (-item[0], item[1]["root_cause"]))
    )
    return Diagnosis(
        "present" if ordered else "absent",
        entry["reflection_id"],
        focus,
        tuple(
            DiagnosisGroup(
                f"h{i}",
                g["root_cause"],
                g["suggested_fix"],
                tuple(g["evidence_ids"]),
                tuple(g["affected_cases"]),
                tuple(g["target_paths"]),
                mass,
                g.get("counterevidence"),
                g.get("regression_risks"),
                g.get("why_this_layer"),
            )
            for i, (mass, g) in enumerate(ordered, start=1)
        ),
        None,
    )


def diagnosis_payload(diagnosis: Diagnosis) -> dict[str, Any]:
    return {
        "status": diagnosis.status,
        "reflection_id": diagnosis.reflection_id,
        "focus": (
            None
            if diagnosis.focus is None
            else {"reason": diagnosis.focus.reason, "cases": list(diagnosis.focus.cases)}
        ),
        "groups": [
            {
                "id": group.id,
                "root_cause": group.root_cause,
                "suggested_fix": group.suggested_fix,
                "evidence_ids": list(group.evidence_ids),
                "affected_cases": list(group.affected_cases),
                "target_paths": list(group.target_paths),
                "mass": group.mass,
                **{
                    name: getattr(group, name)
                    for name in ("counterevidence", "regression_risks", "why_this_layer")
                    if getattr(group, name) is not None
                },
            }
            for group in diagnosis.groups
        ],
        "error_class": diagnosis.error_class,
    }


@dataclass(frozen=True)
class EditBrief:
    """Version `edit_brief/3` (PDR-0006, PDR-0007): the round's baseline
    measurement with per-repeat observations, the round's diagnosis, the
    versioned limits and the history of closed attempts, typed and redacted."""

    champion_digest: str
    operator_context: str | None
    cases: tuple[EditBriefCase, ...]
    diagnosis: Diagnosis
    limits: Mapping[str, int] = field(default_factory=lambda: _brief_limits(512))
    history: BriefHistory = EMPTY_HISTORY
    version: str = BRIEF_VERSION
    attempt_feedback: Mapping[str, Any] | None = None
    pin: EvidencePin | None = None
    cited: tuple[Mapping[str, Any], ...] = ()
    cited_omitted: int = 0


def with_attempt_feedback(brief: EditBrief, frozen_feedback: Mapping[str, Any]) -> EditBrief:
    """Attach an admitted freeze-record section without rereading later history.

    The campaign validates and guards this section before journaling. This
    boundary detaches and freezes every container so editor attempts cannot
    modify their own input or the caller's replay record.
    """

    def freeze(value: Any) -> Any:
        if isinstance(value, Mapping):
            return MappingProxyType({key: freeze(item) for key, item in value.items()})
        if isinstance(value, (tuple, list)):
            return tuple(freeze(item) for item in value)
        return value

    return replace(
        brief,
        version=brief_version_for(brief.pin, feedback=True),
        attempt_feedback=freeze(frozen_feedback),
    )


#: Family 14 (H11, F0 R64/R65): the fixed notes of a `workspace_changed`
#: refusal over a role projection. A refusal of the real Attempt workspace --
#: its admission, capture or publication -- can name any entry in it, hidden
#: ones included, so its text never reaches a family-14 note; the `chat` and
#: `agent_cli` attempt transcripts, which are operator-private, keep it.
PROJECTION_REFUSED_NOTE = "workspace_changed role projection refused"


PUBLICATION_REFUSED_NOTE = "workspace_changed Candidate changed before publication"


@dataclass(frozen=True)
class RoleProjection:
    """Family 14's sparse Editor workspace (ADR-0029).

    `visible` is the role-visibility predicate over relative POSIX file paths
    (`RoleVisibilityView.visible`) and `expected_digest` the frozen
    `role_projection/1` digest the projection must reproduce
    (`workspace.project(..., visible=, expected_digest=)`). A bound
    Editor kind runs on that projection and publishes onto the full capture; a
    library-supplied `CandidateEditor` receives it too and is operator-trusted
    to honour it, as a declared program is.
    """

    visible: Callable[[str], bool]
    expected_digest: str


@dataclass(frozen=True)
class EditRequest:
    attempt_id: str
    round: int
    workspace: Path
    writable_paths: tuple[str, ...]
    brief: EditBrief
    deadline_seconds: float
    #: Family 14 only; `None` keeps every earlier family's request, and the
    #: `command` Editor's `edit_request/1` document, byte for byte.
    projection: RoleProjection | None = None
    #: The executor's recorder of this Attempt's provider attempts,
    #: which a chat Editor feeds. Owned outside the role task, so a deadline or
    #: a stop that cancels the task still leaves them. Python only: never in
    #: the `edit_request/1` document, never journaled.
    attempts: AttemptRecorder | None = field(default=None, repr=False, compare=False)
    traces: BaselineTraces | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class EditResult:
    status: Literal["edited", "declined", "failed"]
    note: str
    code: str | None = None
    continued: bool = False


def outcome_class(result: EditResult) -> str:
    """The class an outcome carries: a `failed` note's first token, else the status.

    A `failed` note is a stable class followed by a redacted detail , so its first token is the class; `edited` and `declined` are their own.
    An editor that replaces one outcome with another -- a seal that changed
    after a run that declined, say -- names the outcome it replaced with this,
    so the run that was overridden stays legible in the note.
    """

    if result.status != "failed":
        return result.status
    head = result.note.split(maxsplit=1)
    return head[0] if head else "failed"


class CandidateEditor(Protocol):
    """The campaign's one caller-supplied surface that touches a Candidate tree.

    `edit` may be called concurrently for up to `parallel_attempts` attempts;
    an editor must not share mutable state across attempts, sees only its own
    request, may touch only its own `workspace`, must return within
    `deadline_seconds`, and must honour `asyncio` cancellation promptly.
    """

    @property
    def identity(self) -> EditorIdentity: ...

    async def edit(self, request: EditRequest) -> EditResult: ...


def plain_public(value: Any) -> Any:
    """A corpus case's `public` as plain JSON: `dict`s, `list`s and scalars.

    `EvalCase` freezes `public` recursively — nested mappings become
    `mappingproxy`, lists become tuples — and `json.dumps` accepts neither, so
    a brief with a nested `public` (a history of turns, say) failed every
    editor kind before it ran (the acquiring readiness report, 2026-09-11).
    The digest never noticed because `canonical_json` walks any `Mapping`;
    the payload the editors serialize must be plain. Values are carried
    verbatim: this converts containers only.
    """

    if isinstance(value, Mapping):
        return {str(key): plain_public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_public(item) for item in value]
    return value


def brief_payload(brief: EditBrief) -> dict[str, Any]:
    """The brief as the JSON document C1 hashes and a subprocess editor reads.

    Public since W1: `SubprocessEditor` sends exactly these bytes as the
    request's `brief`, so the editor sees the same redacted projection
    `brief_digest` hashes and nothing more.
    """

    return {
        "version": brief.version,
        **(
            {}
            if brief.pin is None
            else {
                **brief.pin.payload(),
                "cited": plain_public(brief.cited),
                "cited_omitted": brief.cited_omitted,
            }
        ),
        "champion_digest": brief.champion_digest,
        "operator_context": brief.operator_context,
        "limits": dict(brief.limits),
        "cases": [
            {
                "case_id": case.case_id,
                "group_id": case.group_id,
                "stratum": case.stratum,
                "tags": list(case.tags),
                "public": plain_public(case.public),
                "repeats": [
                    {
                        "repeat": repeat.repeat,
                        "sample_id": repeat.sample_id,
                        "terminal": repeat.terminal,
                        "primary_score": repeat.primary_score,
                        "critical_pass": repeat.critical_pass,
                        "observation": observation_payload(repeat.observation),
                    }
                    for repeat in case.repeats
                ],
            }
            for case in brief.cases
        ],
        "history": history_payload(brief.history),
        "diagnosis": diagnosis_payload(brief.diagnosis),
        **(
            {"attempt_feedback": plain_public(brief.attempt_feedback)}
            if brief.attempt_feedback is not None
            else {}
        ),
    }


# The pre-W1 private name, kept for the callers (and tests) that hold it.
_brief_payload = brief_payload


def brief_digest(brief: EditBrief) -> str:
    return hashlib.sha256(canonical_json(brief_payload(brief))).hexdigest()


def note_digest(note: str) -> str:
    return hashlib.sha256(canonical_json(note)).hexdigest()


def brief_version_for(pin: EvidencePin | None, *, feedback: bool) -> str:
    if pin is not None:
        return BRIEF_FEEDBACK_VERSION_V16 if feedback else BRIEF_VERSION_V16
    return "edit_brief/5" if feedback else BRIEF_VERSION


def cited_evidence_ids(diagnosis: Diagnosis) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            id_
            for group in diagnosis.groups
            for id_ in group.evidence_ids
            if id_.startswith("obs:")
        )
    )


def cite_observations(
    diagnosis: Diagnosis,
    observations: Mapping[str, Observation],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    size = 0
    omitted = 0
    full = False
    for id_ in cited_evidence_ids(diagnosis):
        if id_ not in observations:
            continue
        entry = {"evidence_id": id_, "observation": observation_payload(observations[id_])}
        entry_size = len(canonical_json(entry))
        if full or size + entry_size > limit:
            full = True
            omitted += 1
        else:
            entries.append(entry)
            size += entry_size
    return entries, omitted


def _brief_limits(sentence_chars: int | None) -> Mapping[str, int]:
    """The brief's `limits` at the round's group text limit.

    `groups` and `sentence_chars` are the Reflector's adaptive limits resolved
    with the round's limit (`campaign_reflector.adaptive_limits`, ADR-0047), not
    copies; at 512 the result equals `BRIEF_LIMITS_V3` key for key, and `None`
    omits `sentence_chars`.
    """

    # Imported here: `campaign_reflector` imports this module.
    from evolution.tree.roles.limits import adaptive_limits

    shared = adaptive_limits(sentence_chars)
    groups = shared["groups"]
    assert groups is not None
    limits = {**BRIEF_LIMITS_V2, "groups": groups}
    text = shared["sentence_chars"]
    if text is not None:
        limits["sentence_chars"] = text
    return MappingProxyType(limits)
