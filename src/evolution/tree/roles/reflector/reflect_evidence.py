"""Deterministic grouping of the baseline's failures: repeats
-> cohorts -> clusters -> focus -> portion.

This module is the Reflector's own read of the baseline measurement: it
re-derives the same authenticated-trace observations `editor_evidence`
projects for the editor (`_observation`, `_verified_view`,
`_scoring_records`), then adds the two things the Reflector needs that the
editor's brief never carries -- the scorer's verdicts and the whole
`public_feedback` mapping, not the editor's closed scalar grammar (spec
the contract's `feedback_of` drops anything that is not a short identifier-shaped
scalar; the Reflector's own grammar below keeps the whole mapping, sentences
included, each value in its own JSON type and every cut flagged).

No field of a `RepeatEvidence` is ever assigned from `oracle`, `private`, the
scorer's `details`, its stderr, raw tool arguments or results: everything
here comes from a `SampleRow` (durable per-sample facts), a verified scorer
trace's `stdout` (verdicts, `public_feedback`) and its authenticated `view`
(turn/event counts only, via `_observation`).

`cohorts_of`'s equivalence key is `(case_id, failed_criteria, (outcome,
critical_pass), scale)` (the cohort key): a cohort's members share one
`event_count` tercile bucket by construction, and `Cohort.scale` is that
shared bucket, not a derived summary. One case's one failure mode can
therefore land in more than one cohort when its repeats' event counts fall in
different terciles -- `clusters_of` still merges those cohorts back into one
cluster (its own key ignores `scale`), so the focus/promotion-gate view stays
one failure mode per cluster; only the finer per-cohort evidence shows
the Reflector is split by scale.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, NamedTuple

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.artifacts.artifacts import ArtifactRef
from evolution.tree.campaign_schema import CampaignSnapshot
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.check_modes import HitSink
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.interaction import CandidateView, EvalCase
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.rollout_scoring import (  # noqa: PLC2701
    _parse_score_result,
    parse_scorer_stdout,
)
from evolution.tree.execution.scorer_traces import ScorerTraceReader, decode_trace_text
from evolution.tree.roles.editor.editor_evidence import (  # noqa: PLC2701
    CarriedTraces,
    _observation,
    _scoring_records,
    _verified_view,
    read_trace,
)
from evolution.tree.roles.editor_contracts import UNAVAILABLE_OBSERVATION, Observation, plain_public
from evolution.tree.roles.input_secrets import (
    criteria_carry_run_secret,
    input_secret_value,
)
from evolution.tree.roles.reflector_contracts import REFLECT_LIMITS_V1
from evolution.tree.storage.envelope import Envelope

GROUPING_VERSION = "reflect_grouping/1"
SCALES = ("low", "mid", "high", "none")


@dataclass(frozen=True)
class RepeatEvidence:
    case_id: str
    repeat: int
    sample_id: str
    outcome: str
    critical_pass: bool | None
    primary_score: float | None
    trace: Literal["present", "unavailable"]
    verdicts: tuple[Mapping[str, Any], ...]
    feedback: Mapping[str, Any]
    feedback_omitted: int  # keys dropped past `feedback_keys`
    feedback_truncated: tuple[str, ...]  # keys whose string value was cut
    observation: Observation
    event_count: int
    failed_criteria: tuple[str, ...] | None  # None == unknown (an unavailable trace)
    #: Family 8 only (ADR-0021): the scorer's Reflector-addressed object, or
    #: `None` when the trace carried none. Already a plain JSON value.
    diagnostics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "feedback", MappingProxyType(dict(self.feedback)))

    @property
    def failing(self) -> bool:
        """PDR-0007: any verdict failed, or `critical_pass` is False, or the
        row's own terminal outcome is not the healthy one -- whether or not its
        trace could be read at all.

        `outcome` is the R2a sample terminal (`sample_outcomes.Terminal`), whose
        healthy value is `completed`; `ok` is the *turn*'s vocabulary and names
        no sample. Comparing against `ok` made every scored repeat of every
        baseline failing, so no round could ever be clean and the focus said
        nothing about which failure mode to chase.
        """

        return (
            bool(self.failed_criteria) or self.critical_pass is False or self.outcome != "completed"
        )


def _unavailable_repeat(
    row: SampleRow, outcome: str, critical_pass: bool | None, primary_score: float | None
) -> RepeatEvidence:
    return RepeatEvidence(
        row.case_id,
        row.repeat,
        row.sample_id,
        outcome,
        critical_pass,
        primary_score,
        "unavailable",
        (),
        {},
        0,
        (),
        UNAVAILABLE_OBSERVATION,
        0,
        None,
    )


class _Feedback(NamedTuple):
    """The projected mapping and what the projection had to leave behind."""

    mapping: dict[str, Any]
    omitted: int
    truncated: tuple[str, ...]


def _reflect_feedback(mapping: Mapping[str, Any], limits: Mapping[str, int]) -> _Feedback:
    """`public_feedback`, whole, under the Reflector's wider grammar.

    PDR-0007 (amended 2026-09-13): the whole mapping within the request's
    published limits, *every value in its own JSON type*, strings cut at the
    published length and the key count at the published count, every cut
    flagged. So a boolean stays a boolean, a number a number, `null` null, and
    a nested object or array passes through unchanged; only a `str` value is
    cut, at `feedback_value_chars * 8` characters -- eight times the editor's
    own scalar bound, wide enough for the sentences a diagnosis needs. The
    editor's closed scalar grammar (`editor_evidence.feedback_of`) does not
    apply here at all.

    Both cuts are reported rather than silent: `omitted` counts the keys
    dropped past `feedback_keys` (sorted-key order decides which survive), and
    `truncated` names the keys whose string value was cut. Before the branch
    review this function re-serialized every value through `canonical_json`
    and handed the Reflector a JSON *string* of it, which lost the type and
    was a lossy transformation of a mapping the record calls whole (round 1,
    finding `reflector-public-feedback-truncated`).

    The serialization check stays, because this grammar is still not closed
    against a scorer's own pathological input: `canonical_json` raises
    `IdentityError` on a `NaN` or a non-string key, and a value nested deep
    enough costs it a `RecursionError` (`parse_scorer_stdout` bounds the outer
    document's own nesting, not the values inside a parsed field). So every
    retained value is put through `canonical_json` and such a value still
    makes the whole trace `unavailable` through the caller's guard -- the same
    guard it reads `stdout` under, for exactly that reason. What comes back is
    the value itself and not the JSON text of it: the round trip is there to
    hand on plain JSON types, because `parse_scorer_stdout` freezes what it
    parses into `MappingProxyType`s and tuples, and `reflect_request/1` is
    written with a plain `json.dumps` that knows neither.
    """

    limit_chars = limits["feedback_value_chars"] * 8
    ordered = sorted(mapping, key=str)
    keys = ordered[: limits["feedback_keys"]]
    projected: dict[str, Any] = {}
    truncated: list[str] = []
    for key in keys:
        value = json.loads(canonical_json(mapping[key]))
        if isinstance(value, str) and len(value) > limit_chars:
            value = value[:limit_chars]
            truncated.append(key)
        projected[key] = value
    return _Feedback(projected, len(ordered) - len(keys), tuple(truncated))


def _repeat_evidence(
    row: SampleRow,
    *,
    started: Mapping[tuple[str, str], Envelope],
    finished: Mapping[tuple[str, str], Envelope],
    cases_by_id: Mapping[str, EvalCase],
    run_root: Path,
    candidate: CandidateView | None,
    scorer: Mapping[str, Any],
    environment_kind: str,
    metric_primary: str,
    limits: Mapping[str, int],
    diagnostics: bool = False,
    visible: Callable[[str], bool] | None = None,
    traces: ScorerTraceReader | None = None,
    secrets: Sequence[str] = (),
    pin: EvidencePin | None = None,
    hits: HitSink | None = None,
) -> RepeatEvidence:
    outcome = row.outcome or "not_started"
    critical_pass = row.critical_pass
    primary_score = None if row.metrics is None else row.metrics.get(metric_primary)
    key = (row.case_id, row.sample_id)
    record = started.get(key)
    case = cases_by_id.get(row.case_id)
    if record is None or case is None or candidate is None:
        return _unavailable_repeat(row, outcome, critical_pass, primary_score)
    # `visible` is family 14's role predicate (`FrozenReflectionInputs`'s role
    # path only): grouping and every earlier family pass `None`.
    from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

    reader = DiagnosticTraceReader(run_root) if traces is None else traces
    observation = _observation(
        run_root,
        record,
        finished.get(key),
        case,
        candidate,
        scorer,
        environment_kind,
        limits,
        visible=visible,
        traces=reader,
        secrets=secrets,
        pin=pin,
        hits=hits,
        redact=lambda value: input_secret_value(value, secrets, pin=pin),
    )
    if observation.status != "present":
        return _unavailable_repeat(row, outcome, critical_pass, primary_score)
    try:
        output = read_trace(reader, record, ("stdout",))
        text = decode_trace_text(None if output is None else output.stdout)
        result = _parse_score_result(parse_scorer_stdout(text), diagnostics=diagnostics)
        if criteria_carry_run_secret(
            result, secrets, pin=pin, hits=hits, location=f"criterion/{row.case_id}/{row.repeat}"
        ):
            return _unavailable_repeat(row, outcome, critical_pass, primary_score)
        verdicts = result.verdicts
        public_feedback = result.public_feedback
        scorer_diagnostics = result.diagnostics
        if secrets:
            # Only a verdict's note is free text: the closed keys and the
            # criterion id (checked above) keep their grammar.
            verdicts = tuple(
                {
                    **verdict,
                    "note": input_secret_value(
                        verdict["note"],
                        secrets,
                        pin=pin,
                        hits=hits,
                        location=f"note/{row.case_id}/{row.repeat}/{verdict['criterion']}",
                    ),
                }
                if "note" in verdict
                else dict(verdict)
                for verdict in verdicts
            )
            public_feedback = input_secret_value(
                public_feedback,
                secrets,
                pin=pin,
                hits=hits,
                location=f"feedback/{row.case_id}/{row.repeat}",
            )
            scorer_diagnostics = input_secret_value(
                scorer_diagnostics,
                secrets,
                pin=pin,
                hits=hits,
                location=f"diagnostics/{row.case_id}/{row.repeat}",
            )
        feedback = _reflect_feedback(public_feedback, limits)
        failed_criteria = tuple(sorted(v["criterion"] for v in verdicts if not v["passed"]))
    except ArtifactCorruptionError:
        raise
    except Exception:
        # The scorer's own stdout is externally produced input this module
        # did not write, read the same way `_read_and_bind` reads
        # `request.json`: a broad guard, because a read failure, a
        # `json.JSONDecodeError`, a pathologically nested document
        # (`RecursionError`, from the parse or from `_reflect_feedback`'s own
        # `canonical_json` re-serialization of a value nested deep enough) and
        # every `ScoringError` the verdict/feedback grammar can raise are each
        # a way this trace turns out unusable, and none of them may escape as
        # a raise that ends the campaign. The feedback projection runs inside
        # this same guard, not after it, for exactly that reason.
        return _unavailable_repeat(row, outcome, critical_pass, primary_score)
    event_count = sum(len(turn.events) for turn in observation.turns)
    return RepeatEvidence(
        row.case_id,
        row.repeat,
        row.sample_id,
        outcome,
        critical_pass,
        primary_score,
        "present",
        verdicts,
        feedback.mapping,
        feedback.omitted,
        feedback.truncated,
        observation,
        event_count,
        failed_criteria,
        plain_public(scorer_diagnostics),
    )


def project_repeats(
    *,
    rows: Sequence[SampleRow],
    records: Sequence[Envelope],
    run_root: Path,
    corpus: Sequence[EvalCase],
    baseline: ArtifactRef,
    environment_kind: str,
    metric_primary: str,
    limits: Mapping[str, int] = REFLECT_LIMITS_V1,
    carried: CarriedTraces | None = None,
) -> tuple[RepeatEvidence, ...]:
    """One `RepeatEvidence` per row of the baseline that ever started (step 1).

    A row whose `disposition` is `not_started` never ran and carries nothing
    a diagnosis can use; every other row is projected, `unavailable` when its
    trace cannot be authenticated or its `stdout` cannot be read as the
    scorer's own closed contract. `carried` (family 12 only) offers a row
    carried from a measurement of the same run its source try's scoring
    records (`editor_evidence._scoring_records`).
    """

    identity, started, finished = _scoring_records(records, carried)
    cases_by_id = {case.case_id: case for case in corpus}
    scorer = identity.get("scorer", {})
    candidate = _verified_view(baseline)
    from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

    traces = DiagnosticTraceReader(run_root)
    return tuple(
        _repeat_evidence(
            row,
            started=started,
            finished=finished,
            cases_by_id=cases_by_id,
            run_root=run_root,
            candidate=candidate,
            scorer=scorer,
            environment_kind=environment_kind,
            metric_primary=metric_primary,
            limits=limits,
            diagnostics=False,  # legacy family-3 path: `diagnostics` stays an unknown key
            traces=traces,
        )
        for row in rows
        if row.disposition != "not_started"
    )


def scale_buckets(repeats: Sequence[RepeatEvidence]) -> dict[tuple[str, int], str]:
    """Nearest-rank thirds of `event_count` over the present repeats (step 2).

    the contract: "a count on a boundary takes the lower bucket", and that rule
    governs *both* boundaries. `low_max` is the count at the nearest-rank low
    boundary, so a repeat whose count equals it is `low`; `high_min` is the
    count at the high boundary, so a repeat whose count equals it takes the
    lower bucket too and is `mid` -- only a strictly larger count is `high`.
    Counts `[1..7]` are therefore low `{1,2,3}`, mid `{4,5}`, high `{6,7}`,
    and in `[1,2,3,4,4,5]` both fours are `mid`.
    """

    present = sorted(r.event_count for r in repeats if r.trace == "present")
    n = len(present)
    third = math.ceil(n / 3) if n else 0
    low_max = present[third - 1] if third else None
    high_min = present[n - third] if third and n - third >= third else None
    buckets: dict[tuple[str, int], str] = {}
    for r in repeats:
        if r.trace != "present":
            buckets[(r.case_id, r.repeat)] = "none"
        elif low_max is not None and r.event_count <= low_max:
            buckets[(r.case_id, r.repeat)] = "low"
        elif high_min is not None and r.event_count > high_min:
            buckets[(r.case_id, r.repeat)] = "high"
        else:
            buckets[(r.case_id, r.repeat)] = "mid"
    return buckets


@dataclass(frozen=True)
class Cohort:
    cohort_id: str
    case_id: str
    failed_criteria: tuple[str, ...] | None
    outcome: tuple[str, bool | None]
    scale: str
    members: tuple[RepeatEvidence, ...]  # in repeat order

    @property
    def mass(self) -> int:
        return len(self.members)


def cohorts_of(repeats: Sequence[RepeatEvidence]) -> tuple[Cohort, ...]:
    """Failing repeats grouped into cohorts, canonically ordered and numbered (
    cohort key: `(case_id, failed_criteria, (outcome, critical_pass), scale)`)."""

    buckets = scale_buckets(repeats)
    groups: dict[
        tuple[str, tuple[str, ...] | None, tuple[str, bool | None], str], list[RepeatEvidence]
    ]
    groups = {}
    order: list[tuple[str, tuple[str, ...] | None, tuple[str, bool | None], str]] = []
    for r in repeats:
        if not r.failing:
            continue
        key = (
            r.case_id,
            r.failed_criteria,
            (r.outcome, r.critical_pass),
            buckets[(r.case_id, r.repeat)],
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    ordered = sorted(order, key=lambda k: (-len(groups[k]), k[0], canonical_json(k)))
    cohorts = []
    for position, key in enumerate(ordered, start=1):
        case_id, failed_criteria, outcome, scale = key
        members = tuple(sorted(groups[key], key=lambda r: r.repeat))
        cohorts.append(
            Cohort(f"c{position:03d}", case_id, failed_criteria, outcome, scale, members)
        )
    return tuple(cohorts)


@dataclass(frozen=True)
class Cluster:
    fingerprint: str
    failed_criteria: tuple[str, ...] | None
    outcome: tuple[str, bool | None]
    case_ids: tuple[str, ...]
    cohorts: tuple[Cohort, ...]
    mass: int


class _ClusterKey(NamedTuple):
    """The cluster equivalence key: a failure mode's criteria, or -- when a
    cohort names none -- that one case alone.

    "Names none" is two states, not one: `None` is a cohort whose trace was
    unavailable, and `()` is a present trace that recorded no verdict at all
    (a terminal or critical failure scored before any criterion ran). Spec
    the contract treats both the same way -- "without verdicts a cluster is one case's
    cohorts" -- so both fall back to the case. Keying `()` as a shared failure
    mode instead would merge every verdict-less failure in the corpus into one
    cluster whose fingerprint names no case.
    """

    failed_criteria: tuple[str, ...] | None
    case_id: str | None
    outcome: tuple[str, bool | None]


def clusters_of(cohorts: Sequence[Cohort]) -> tuple[Cluster, ...]:
    """Cohorts grouped by failure mode, across cases, canonically ordered (step 4)."""

    groups: dict[_ClusterKey, list[Cohort]] = {}
    order: list[_ClusterKey] = []
    for c in cohorts:
        key = _ClusterKey(
            c.failed_criteria, c.case_id if not c.failed_criteria else None, c.outcome
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    clusters = []
    for key in order:
        members = tuple(groups[key])
        payload: dict[str, Any]
        if key.failed_criteria:
            payload = {"criteria": list(key.failed_criteria), "outcome": list(key.outcome)}
        else:
            payload = {"case_id": key.case_id, "outcome": list(key.outcome)}
        fingerprint = hashlib.sha256(canonical_json(payload)).hexdigest()
        case_ids = tuple(sorted({m.case_id for m in members}))
        mass = sum(m.mass for m in members)
        clusters.append(
            Cluster(fingerprint, key.failed_criteria, key.outcome, case_ids, members, mass)
        )
    clusters.sort(key=lambda cl: (-cl.mass, cl.fingerprint))
    return tuple(clusters)


def stalled_fingerprints(snapshot: CampaignSnapshot) -> frozenset[str]:
    """The cluster fingerprints a prior round already tried and stalled on.

    Walks `snapshot.rounds` in round order: a `promoted` round's champion
    changed, so nothing before it constrains this one -- the set clears. A
    `stalled` round's focus fingerprint (when it had one) is remembered so
    `select_focus` does not re-pick a cluster the campaign already spent a
    round on without progress.
    """

    stalled: set[str] = set()
    for number in sorted(snapshot.rounds):
        closed = snapshot.rounds[number].closed
        if closed is None:
            continue
        outcome = closed.get("outcome")
        if outcome == "promoted":
            stalled.clear()
        elif outcome == "stalled":
            focus = snapshot.rounds[number].focus
            fingerprint = focus.get("focus_fingerprint") if focus is not None else None
            if fingerprint is not None:
                stalled.add(str(fingerprint))
    return frozenset(stalled)


@dataclass(frozen=True)
class FocusSelection:
    policy_digest: str
    clusters: tuple[Cluster, ...]
    stalled: frozenset[str]
    focus: Cluster | None
    reason: Literal["mass", "exhausted", "none"]
    unavailable: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "policy_digest": self.policy_digest,
            "clusters": [
                {
                    "fingerprint": cluster.fingerprint,
                    "mass": cluster.mass,
                    "cases": list(cluster.case_ids),
                    "stalled": cluster.fingerprint in self.stalled,
                }
                for cluster in self.clusters
            ],
            "focus_fingerprint": self.focus.fingerprint if self.focus is not None else None,
            "reason": self.reason,
            "unavailable": self.unavailable,
        }


def select_focus(
    repeats: Sequence[RepeatEvidence],
    *,
    snapshot: CampaignSnapshot,
    limits: Mapping[str, int] = REFLECT_LIMITS_V1,
) -> FocusSelection:
    """The round's focus cluster: the highest-mass cluster not already stalled
    on, or the highest-mass cluster again (`exhausted`) when every cluster is,
    or no cluster at all (`none`) when nothing is failing."""

    stalled = stalled_fingerprints(snapshot)
    unavailable = sum(1 for r in repeats if r.trace == "unavailable")
    clusters = clusters_of(cohorts_of(repeats))
    digest = policy_digest(limits)
    if not clusters:
        return FocusSelection(digest, (), stalled, None, "none", unavailable)
    for cluster in clusters:
        if cluster.fingerprint not in stalled:
            return FocusSelection(digest, clusters, stalled, cluster, "mass", unavailable)
    return FocusSelection(digest, clusters, stalled, clusters[0], "exhausted", unavailable)


@dataclass(frozen=True)
class PortionCohort:
    cohort: Cohort
    exemplar: RepeatEvidence | None
    contrast: RepeatEvidence | None
    others: tuple[RepeatEvidence, ...]


def _contrast_of(case_id: str, repeats: Sequence[RepeatEvidence]) -> RepeatEvidence | None:
    same_case = sorted((r for r in repeats if r.case_id == case_id), key=lambda r: r.repeat)
    for r in same_case:
        if r.trace == "present" and not r.failing:
            return r
    return None


def portion_of(
    focus: Cluster,
    repeats: Sequence[RepeatEvidence],
    *,
    limits: Mapping[str, int] = REFLECT_LIMITS_V1,
) -> tuple[PortionCohort, ...]:
    """The focus cluster's cohorts, capped and each fitted with an exemplar,
    a passing contrast and its remaining members (step 2)."""

    portions = []
    for cohort in focus.cohorts[: limits["cohorts"]]:
        present = [m for m in cohort.members if m.trace == "present"]
        exemplar: RepeatEvidence | None = None
        if present:
            counts = sorted(m.event_count for m in present)
            median = counts[len(present) // 2]
            exemplar = min(present, key=lambda m: (abs(m.event_count - median), m.repeat))
        contrast = _contrast_of(cohort.case_id, repeats)
        others = tuple(m for m in cohort.members if m is not exemplar)
        portions.append(PortionCohort(cohort, exemplar, contrast, others))
    return tuple(portions)


def policy_digest(limits: Mapping[str, int] = REFLECT_LIMITS_V1) -> str:
    return hashlib.sha256(
        canonical_json({"grouping": GROUPING_VERSION, "limits": dict(limits)})
    ).hexdigest()
