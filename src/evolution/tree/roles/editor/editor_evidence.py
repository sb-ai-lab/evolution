"""The `edit_brief/2` projections: observations from verified scorer traces and
the history of closed attempts.

This module is the one place that reads a scorer trace and the corpus case
whole — to *authenticate* a trace against its `scoring_started` record — and
its only outputs are the brief's fields and a status. No field is assigned
from raw scorer stdin, `private`, `oracle`, `Message.fields`, raw tool
arguments or results, `details`, `verdicts`, non-primary metrics, the scorer's
stderr, the editor note or the target's address. The canaries in
`tests/test_tree_brief2_end_to_end.py` assert the brief's bytes.

Every source is `present`, `absent` (nothing to show), `unavailable` (exists
but could not be read or verified; the reason is shown nowhere, because a
broken trace must not become a channel) or `withheld` (private by origin).
Nothing is invented for an absent or unavailable source; a `truncated` flag
marks every cut.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from math import isfinite
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, TreeScan, changes, scan_tree
from evolution.tree.artifacts.artifacts import ArtifactRef, Kind
from evolution.tree.attempt_feedback import (
    FeedbackIntegrityError,
    FeedbackSource,
    ScorePair,
    build_feedback,
)
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_schema import (
    _CONTINUABLE_ATTEMPT_OUTCOMES,
    AttemptState,
    CampaignSnapshot,
    RoundState,
    effective_attempt_feedback,
)
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.check_modes import (
    ENFORCE_ALL,
    CheckModes,
    HitSink,
    Mode,
    check_predicate,
)
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.interaction import CandidateIntegrityError, CandidateView, EvalCase
from evolution.tree.execution.journal_identity import scoring_input_digest
from evolution.tree.execution.rollout_scoring import ScoringError, parse_scorer_stdout
from evolution.tree.execution.scorer_traces import TRACE_ROOT as TRACE_ROOT
from evolution.tree.execution.scorer_traces import TRACE_SUBDIR as TRACE_SUBDIR
from evolution.tree.execution.scorer_traces import (
    ScorerTraceReader,
    ScorerTraceRecord,
    decode_trace_text,
)
from evolution.tree.roles.editor_contracts import (
    ABSENT,
    ABSENT_OBSERVATION,
    BRIEF_LIMITS_V2,
    EDIT_EXCERPT_CHARS,
    PRESENT,
    UNAVAILABLE,
    UNAVAILABLE_OBSERVATION,
    WITHHELD,
    AttemptChanges,
    AttemptResult,
    AttemptResultCase,
    AttemptResultRepeat,
    BriefHistory,
    HistoryAttempt,
    HistoryRound,
    Observation,
    ObservationInput,
    ObservationTurn,
)
from evolution.tree.roles.role_visibility import visible_observation, visible_paths
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalBoundaryError


class RowSource(Protocol):
    """What the history needs of a measurement: `Measurement.sample_rows`."""

    def sample_rows(self, look: int) -> tuple[SampleRow, ...]: ...


class PublishedRefs(Protocol):
    """What the history needs of the store: `ArtifactStore.published_ref`."""

    def published_ref(self, kind: Kind, artifact_id: str) -> ArtifactRef: ...


def project_attempt_feedback(
    state: RoundState,
    *,
    current_index: int,
    measurements: Mapping[str, RowSource],
    store: PublishedRefs,
    artifact_policy: ArtifactPolicyV1,
    policy: CampaignPolicyV11,
    exam_cases: Sequence[str],
    mode: Literal["none", "sequential"],
    forbidden: Callable[[str], bool],
    redact: Callable[[str], str] | None = None,
    source_refs: Mapping[str, tuple[str, ...]],
    outcomes: Mapping[str, frozenset[str]] = _CONTINUABLE_ATTEMPT_OUTCOMES,
    family_version: int = 0,
    visible: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Project closed lower-index attempts from one admitted round prefix.

    ``outcomes`` is the campaign family's Attempt close vocabulary
    (`build_feedback`).

    ``source_refs`` are bounded display IDs. The campaign separately retains
    actual journal references in its freeze event. Measurements must be the
    verified admitted prefixes for this snapshot, never live mutable runners.
    No workspace path participates in this projection.

    ``visible`` (family 14 only, H11 routine choice 8) is the role's path
    predicate: hidden members leave the changed-path list before any diff is
    read, so neither ``changed_paths`` nor ``diff_excerpt`` names one, and
    `build_feedback`'s caps and the leak guard apply to the visible list.
    ``None`` keeps every earlier family byte for byte.
    """
    if mode == "none":
        return build_feedback((), mode=mode, current_index=current_index, exam_cases=())
    sources = []
    for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
        if (
            attempt.round != state.round
            or attempt.index >= current_index
            or attempt.outcome is None
        ):
            continue
        if attempt.reason is None:
            raise FeedbackIntegrityError("closed attempt has no reason")
        paths: tuple[str, ...] = ()
        excerpt = ""
        status: Literal["available", "unavailable"] = "unavailable"
        # A refused/unsealed attempt has no publication authority, even when
        # incidental workspace files happen to exist at a predictable path.
        if attempt.candidate_digest is not None and attempt.reason != "seal_refused":
            try:
                parent = store.published_ref("champion", attempt.parent_version)
                child = store.published_ref("candidate", attempt.attempt_id)
                parent.verify()
                child.verify()
                if (
                    parent.digest != attempt.parent_digest
                    or child.digest != attempt.candidate_digest
                ):
                    raise FeedbackIntegrityError("published feedback digest mismatch")
                parent_scan = scan_tree(parent.root, artifact_policy)
                child_scan = scan_tree(child.root, artifact_policy)
                paths = tuple(changes(parent_scan, child_scan, parent.root, child.root))
                if visible is not None:
                    paths = visible_paths(paths, visible, _files(parent_scan, child_scan))
                parts: list[str] = []
                for relative in paths:
                    if visible is not None and not visible(relative):
                        # A directory kept for the visible files beneath it
                        # (`visible_paths`): it has no text of its own, and on
                        # a kind change its other side may be a hidden file
                        # whose bytes must not become a diff.
                        continue
                    before, after = (
                        _feedback_lines(parent.root / relative),
                        _feedback_lines(child.root / relative),
                    )
                    if before is None or after is None:
                        parts.append(f"--- {relative}: binary\n")
                    else:
                        parts.extend(
                            difflib.unified_diff(
                                before, after, fromfile=relative, tofile=relative, n=0
                            )
                        )
                excerpt = "".join(parts)
                # Recheck after reading: corruption cannot become a truncated
                # but apparently valid diff of a different publication.
                parent.verify()
                child.verify()
            except (ArtifactError, OSError, ValueError) as exc:
                raise FeedbackIntegrityError("published feedback source is corrupt") from exc
            status = "available"
        pairs: tuple[ScorePair, ...] = ()
        if attempt.measurement_id is not None:
            try:
                parent_rows = measurements[str(state.baseline_measurement_id)].sample_rows(
                    policy.repeats
                )
                child_rows = measurements[attempt.measurement_id].sample_rows(policy.repeats)
                parent_scores = _feedback_scores(parent_rows, policy.metric_primary)
                child_scores = _feedback_scores(child_rows, policy.metric_primary)
                pairs = tuple(
                    ScorePair(
                        case_id,
                        repeat,
                        parent_scores[(case_id, repeat)],
                        child_scores[(case_id, repeat)],
                    )
                    for case_id, repeat in sorted(parent_scores.keys() & child_scores.keys())
                )
            except (KeyError, ValueError, JournalBoundaryError) as exc:
                raise FeedbackIntegrityError(
                    "admitted feedback measurement is unavailable"
                ) from exc
        layer = None
        if attempt.look is not None and attempt.look in attempt.looks:
            layer = attempt.looks[attempt.look].get("rejection_layer")
        reason, _ = effective_attempt_feedback(state, attempt, family_version=family_version)
        assert reason is not None
        not_measured: tuple[str, ...] = ()
        if (
            family_version == 15
            and attempt.family15 is not None
            and attempt.family15.focus_decision is not None
            and attempt.family15.focus_decision.payload["outcome"] != "passed"
        ):
            not_measured = tuple(
                sorted({row.case_id for row in child_rows if row.disposition == "not_started"})
            )
        sources.append(
            FeedbackSource(
                attempt.attempt_id,
                attempt.index,
                bool(
                    attempt.looks
                    or (
                        family_version == 15
                        and attempt.family15 is not None
                        and attempt.family15.comparison is not None
                    )
                ),
                attempt.outcome,
                reason,
                policy.repeats,
                status,
                paths,
                excerpt,
                pairs,
                layer,
                source_refs.get(attempt.attempt_id, ()),
                not_measured,
            )
        )
    return build_feedback(
        sources,
        mode=mode,
        current_index=current_index,
        exam_cases=exam_cases,
        forbidden=forbidden,
        redact=redact,
        outcomes=outcomes,
        allow_not_measured=family_version == 15,
    )


def _files(*scans: TreeScan) -> tuple[str, ...]:
    """Every regular-file path of the scanned trees, for `visible_paths`."""

    return tuple(entry.path for scan in scans for entry in scan.artifact if entry.kind == "file")


def _feedback_lines(path: Path) -> list[str] | None:
    # Directory entries remain in changed_paths, but have no text to diff.
    # Treat the missing side of file/directory replacements as empty too;
    # publication verification before and after the read still detects damage.
    if path.is_dir():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except UnicodeDecodeError:
        return None


def _feedback_scores(rows: Sequence[SampleRow], primary: str) -> dict[tuple[str, int], float]:
    scores: dict[tuple[str, int], float] = {}
    for row in rows:
        if row.disposition != "score" or row.metrics is None or primary not in row.metrics:
            continue
        score = row.metrics[primary]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score):
            raise FeedbackIntegrityError("invalid admitted primary score")
        key = (row.case_id, row.repeat)
        if key in scores:
            raise FeedbackIntegrityError("duplicate admitted score pair")
        scores[key] = float(score)
    return scores


# -- history ----------------------------------------------------------------


def project_history(
    snapshot: CampaignSnapshot,
    *,
    measurements: Mapping[str, RowSource],
    store: PublishedRefs,
    artifact_policy: ArtifactPolicyV1,
    corpus: Sequence[EvalCase],
    policy: CampaignPolicyV11,
    limits: Mapping[str, int] = BRIEF_LIMITS_V2,
    excerpts: bool = False,
    visible: Callable[[str], bool] | None = None,
) -> BriefHistory:
    """The closed rounds of this run, the last `limits["rounds"]` of them
    (`limits["history_rounds"]` instead, when the caller -- the Reflector --
    carries that key).

    Only rounds with a `round_closed` record are read, so the reduction is the
    same for every attempt of the open round whatever the scheduling (the
    round barrier, ADR-0008). A predecessor run's rounds are never read; their
    count is stated so the editor knows earlier rounds exist it cannot see.
    `excerpts` additionally computes each shown attempt's
    `edit_excerpt`; every shown round's `diagnosis` is always computed.
    `visible` (family 14 only) filters each attempt's changed paths before
    the cap and the excerpt (`_changes`); `None` keeps every earlier family
    byte for byte.
    """

    rounds_limit = limits["history_rounds"] if "history_rounds" in limits else limits["rounds"]
    closed = sorted(number for number, state in snapshot.rounds.items() if state.closed is not None)
    shown = tuple(closed[-rounds_limit:]) if rounds_limit > 0 else ()
    linked = snapshot.identity.get("linked_from")
    predecessor_rounds = int(linked["rounds_closed"]) if isinstance(linked, Mapping) else 0
    rounds = []
    for number in shown:
        state = snapshot.rounds[number]
        assert state.closed is not None
        attempts = sorted(state.attempts.values(), key=lambda attempt: attempt.index)
        cut = attempts[: limits["attempts_per_round"]]
        selection = state.selection or {}
        rounds.append(
            HistoryRound(
                round=number,
                outcome=str(state.closed["outcome"]),
                selected_attempt=selection.get("attempt_id"),
                attempts=tuple(
                    _history_attempt(
                        attempt,
                        measurements,
                        store,
                        artifact_policy,
                        corpus,
                        policy,
                        limits,
                        excerpts=excerpts,
                        visible=visible,
                        reason=effective_attempt_feedback(
                            state, attempt, family_version=snapshot.family_version
                        )[0],
                    )
                    for attempt in cut
                ),
                attempts_truncated=len(cut) < len(attempts),
                diagnosis=_diagnosis_payload(state, True),
            )
        )
    return BriefHistory(
        rounds_closed=len(closed),
        rounds_shown=shown,
        predecessor_rounds=predecessor_rounds,
        rounds=tuple(rounds),
    )


def _diagnosis_payload(state: RoundState, resilient: bool) -> dict[str, Any] | None:
    """The round's diagnosis, reduced to the shape a brief can show.

    `None` when the round never opened a reflection at all (`state.focus is
    None`); `status: "absent"` when it did but has no settled diagnosis yet
    (an interrupted reflection is retried, never diagnosed --
    `RoundState.diagnosis`); `status: "unavailable"` when the settled
    reflection failed; otherwise the reflection's own groups, `"present"`
    when there is at least one, `"absent"` when the reflection ran clean.
    `resilient` is the campaign's family-10 flag (`RoundState.diagnosis`).
    """

    entry = state.diagnosis(resilient)
    if entry is None:
        return (
            None
            if state.focus is None
            else {
                "status": "absent",
                "reflection_id": None,
                "groups": [],
                "error_class": None,
                "probes": [],
            }
        )
    if entry["status"] == "failed":
        return {
            "status": "unavailable",
            "reflection_id": entry["reflection_id"],
            "groups": [],
            "error_class": entry["error_class"],
            "probes": [],
        }
    groups = list(entry["groups"])
    cited_probes = {seq for group in groups for seq in group.get("probe_refs", ())}
    return {
        "status": "present" if groups else "absent",
        "reflection_id": entry["reflection_id"],
        "groups": groups,
        "error_class": None,
        "probes": [
            dict(seq=p["seq"], method=p["method"], path=p["path"], status=p["status"] or p["error"])
            for p in entry["probes"]
            if p["seq"] in cited_probes
        ],
    }


def _history_attempt(
    attempt: AttemptState,
    measurements: Mapping[str, RowSource],
    store: PublishedRefs,
    artifact_policy: ArtifactPolicyV1,
    corpus: Sequence[EvalCase],
    policy: CampaignPolicyV11,
    limits: Mapping[str, int],
    *,
    excerpts: bool = False,
    reason: str | None = None,
    visible: Callable[[str], bool] | None = None,
) -> HistoryAttempt:
    status, parent_root, child_root = _verify_published_copies(attempt, store, artifact_policy)
    change = _changes(status, parent_root, child_root, artifact_policy, limits, visible=visible)
    excerpt = (
        # `change.paths` is the already filtered, already capped list: the
        # excerpt reads only visible files and names only visible paths.
        _edit_excerpt(parent_root, child_root, change.paths, visible=visible)
        # Gate on `change.status`, not the roots' own verification `status`:
        # the roots can verify fine and the diff/scan still fail its own way
        # (`_changes`'s `ArtifactError`/`OSError`/`ValueError` guard), which
        # is `changes.status == "unavailable"` with `paths ==` -- an
        # excerpt computed from that empty path list would be `""`, not the
        # `None` an unavailable source must report.
        if excerpts
        and change.status == PRESENT
        and parent_root is not None
        and child_root is not None
        else None
    )
    return HistoryAttempt(
        attempt_id=attempt.attempt_id,
        index=attempt.index,
        parent_digest=attempt.parent_digest,
        # `started` is not a terminal (`null` when no editor terminal
        # was recorded).
        editor=attempt.editor if attempt.editor in ("edited", "declined", "failed") else None,
        editor_error=attempt.editor_error,
        outcome=attempt.outcome,
        reason=attempt.reason if reason is None else reason,
        code=attempt.code,
        look=attempt.look,
        candidate_digest=attempt.candidate_digest,
        changes=change,
        result=_result(attempt, measurements, corpus, policy),
        edit_excerpt=excerpt,
    )


def _verify_published_copies(
    attempt: AttemptState, store: PublishedRefs, artifact_policy: ArtifactPolicyV1
) -> tuple[str, Path | None, Path | None]:
    """The attempt's Parent and Candidate published roots, verified once.

    `(ABSENT, None, None)` when the attempt sealed nothing (its workspace is
    not a source); `(UNAVAILABLE, None, None)` when the store, the
    verification or the digest check fails; else `(PRESENT, parent_root,
    child_root)`. `_changes` and `_history_attempt`'s excerpt computation both
    read from this one verification, so a shown attempt's tree is scanned
    once per projection call, not twice.
    """

    if attempt.candidate_digest is None:
        return ABSENT, None, None
    try:
        parent = store.published_ref("champion", attempt.parent_version)
        child = store.published_ref("candidate", attempt.attempt_id)
        parent.verify()
        child.verify()
        if parent.digest != attempt.parent_digest or child.digest != attempt.candidate_digest:
            return UNAVAILABLE, None, None
    except (ArtifactError, OSError, ValueError):
        return UNAVAILABLE, None, None
    return PRESENT, parent.root, child.root


def _changes(
    status: str,
    parent_root: Path | None,
    child_root: Path | None,
    artifact_policy: ArtifactPolicyV1,
    limits: Mapping[str, int],
    *,
    visible: Callable[[str], bool] | None = None,
) -> AttemptChanges:
    """Paths changed between the two verified roots `_verify_published_copies` names.

    A non-`present` status is passed straight through: `absent` for an
    attempt that published nothing, `unavailable` for a store, verification
    or read failure. The diff itself can still fail its own scan, which is
    `unavailable` too.

    `visible` (family 14) drops hidden members before the cap, so `truncated`
    still means the visible list hit `limits["changed_paths"]`, and a change
    that touched only hidden paths is `present` with no paths -- never
    `unavailable`, which would say the source could not be read.
    """

    return _changes_with_scans(
        status, parent_root, child_root, artifact_policy, limits, visible=visible
    )[0]


def _changes_with_scans(
    status: str,
    parent_root: Path | None,
    child_root: Path | None,
    artifact_policy: ArtifactPolicyV1,
    limits: Mapping[str, int],
    *,
    visible: Callable[[str], bool] | None = None,
) -> tuple[AttemptChanges, TreeScan | None, TreeScan | None]:
    """Paths changed between the two verified roots `_verify_published_copies` names.

    A non-`present` status is passed straight through: `absent` for an
    attempt that published nothing, `unavailable` for a store, verification
    or read failure. The diff itself can still fail its own scan, which is
    `unavailable` too.

    `visible` (family 14) drops hidden members before the cap, so `truncated`
    still means the visible list hit `limits["changed_paths"]`, and a change
    that touched only hidden paths is `present` with no paths -- never
    `unavailable`, which would say the source could not be read.
    """

    if status != PRESENT:
        return AttemptChanges(status), None, None
    assert parent_root is not None and child_root is not None
    try:
        parent_scan = scan_tree(parent_root, artifact_policy)
        child_scan = scan_tree(child_root, artifact_policy)
        paths = changes(parent_scan, child_scan, parent_root, child_root)
    except (ArtifactError, OSError, ValueError):
        return AttemptChanges(UNAVAILABLE), None, None
    if visible is not None:
        paths = visible_paths(paths, visible, _files(parent_scan, child_scan))
    cut = paths[: limits["changed_paths"]]
    return AttemptChanges(PRESENT, tuple(cut), len(cut) < len(paths)), parent_scan, child_scan


def _edit_excerpt(
    parent_root: Path,
    candidate_root: Path,
    paths: Sequence[str],
    *,
    visible: Callable[[str], bool] | None = None,
) -> str:
    """The first `EDIT_EXCERPT_CHARS` characters of a unified diff, per changed
    path in `changes.paths` order, over the two published copies' text
    A binary or unreadable file contributes one `binary` line
    instead of a diff.

    `visible` (family 14): `paths` is already the filtered list, and a member
    that is not itself a visible file -- a directory kept for the visible files
    beneath it -- contributes nothing, so a hidden file on the other side of a
    kind change is never read into the excerpt."""

    parts: list[str] = []
    for relative in paths:
        if visible is not None and not visible(relative):
            continue
        before, after = _lines(parent_root / relative), _lines(candidate_root / relative)
        if before is None or after is None:
            parts.append(f"--- {relative}: binary\n")
            continue
        parts.extend(difflib.unified_diff(before, after, fromfile=relative, tofile=relative, n=0))
        if len("".join(parts)) >= EDIT_EXCERPT_CHARS:
            break
    return "".join(parts)[:EDIT_EXCERPT_CHARS]


def _lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return []
    except (UnicodeDecodeError, OSError):
        return None


def _result(
    attempt: AttemptState,
    measurements: Mapping[str, RowSource],
    corpus: Sequence[EvalCase],
    policy: CampaignPolicyV11,
) -> AttemptResult:
    if attempt.measurement_id is None:
        return AttemptResult(ABSENT)
    source = measurements.get(attempt.measurement_id)
    if source is None:
        return AttemptResult(UNAVAILABLE)
    try:
        rows = source.sample_rows(policy.repeats)
    except (ValueError, JournalBoundaryError):
        # What a measurement raises when its rows cannot be read: a malformed
        # or incomplete reduction, and the journal's own boundary. A wider
        # guard here would turn a bug in this package into `unavailable`.
        return AttemptResult(UNAVAILABLE)
    if (
        attempt.family15 is not None
        and attempt.family15.focus_decision is not None
        and attempt.family15.focus_decision.payload["outcome"] != "passed"
    ):
        rows = tuple(
            replace(row, outcome="not_measured") if row.disposition == "not_started" else row
            for row in rows
        )
    return AttemptResult(PRESENT, result_cases(rows, corpus, policy))


def result_cases(
    rows: Sequence[SampleRow], corpus: Sequence[EvalCase], policy: CampaignPolicyV11
) -> tuple[AttemptResultCase, ...]:
    """Per case in corpus order, per repeat: exactly what `/1` shows of a row."""

    by_case: dict[str, list[SampleRow]] = {}
    for row in rows:
        by_case.setdefault(row.case_id, []).append(row)
    cases = []
    for case in corpus:
        repeats = sorted(by_case.get(case.case_id, ()), key=lambda row: row.repeat)
        cases.append(
            AttemptResultCase(
                case_id=case.case_id,
                repeats=tuple(
                    AttemptResultRepeat(
                        repeat=row.repeat,
                        terminal=row.outcome or "not_started",
                        primary_score=(
                            row.metrics[policy.metric_primary]
                            if row.metrics is not None and policy.metric_primary in row.metrics
                            else None
                        ),
                        critical_pass=row.critical_pass,
                    )
                    for row in repeats
                ),
            )
        )
    return tuple(cases)


# -- observations --------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_FEEDBACK_STRING = re.compile(r"[A-Za-z0-9._:-]+")
_INPUT_KEYS = frozenset({"case_id", "public", "oracle", "view"})
_REQUEST_KEYS = frozenset({"scorer", "trace_id", "input", "input_sha256"})
# What "a relative artifact path" can be on the tree the target loaded: the
# scoring digest's own path grammar plus the filesystem's own name and path
# bounds. Not a brief limit — a path outside these cannot name an artifact.
_PATH_MAX = 4096
_NAME_MAX = 255


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _artifact_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > _PATH_MAX:
        return False
    # A newline, a control character or an invisible separator is not part of
    # any name on the tree the target loaded, and a path is rendered into plain
    # text the editor reads.
    if "\\" in value or value.startswith("/") or not value.isprintable():
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") and len(part) <= _NAME_MAX for part in parts)


def shown_event(event: Mapping[str, Any], text_chars: int) -> dict[str, Any] | None:
    """The table, row by row: the shown keys of a listed event, else `None`.

    A listed type whose shown key is missing or of the wrong type is `None`
    like an unknown type; any other key on a listed event is never shown.
    """

    kind = event.get("type")
    if kind == "assistant_message":
        content = event.get("content")
        if not isinstance(content, str):
            return None
        return {
            "type": kind,
            "content": content[:text_chars],
            "truncated": len(content) > text_chars,
        }
    if kind == "tool_call":
        name = event.get("name")
        return {"type": kind, "name": name} if _identifier(name) else None
    if kind == "tool_result":
        name, status = event.get("name"), event.get("status")
        if not (_identifier(name) and _identifier(status)):
            return None
        return {"type": kind, "name": name, "status": status}
    if kind == "skill_read":
        path = event.get("path")
        return {"type": kind, "path": path} if _artifact_path(path) else None
    if kind == "output_truncated":
        return {"type": kind}
    return None


def _scalar_ok(value: Any, value_chars: int) -> bool:
    if isinstance(value, bool):
        serialized = "true" if value else "false"
    elif isinstance(value, int):
        # A JSON integer is unbounded, and `isfinite` on one too large for a
        # float raises `OverflowError` rather than answering. No float is
        # needed: an integer is always finite, and the serialized length is
        # the only bound this grammar applies, so `10 ** 400` is simply a
        # 401-character value that the cap drops.
        serialized = json.dumps(value)
    elif isinstance(value, float):
        if not isfinite(value):
            return False
        serialized = json.dumps(value)
    elif isinstance(value, str):
        if _FEEDBACK_STRING.fullmatch(value) is None:
            return False
        serialized = json.dumps(value, ensure_ascii=False)
    else:
        return False
    return len(serialized) <= value_chars


def feedback_of(
    mapping: Mapping[str, Any],
    limits: Mapping[str, int],
    secrets: Sequence[str] = (),
    *,
    pin: EvidencePin | None = None,
    hits: HitSink | None = None,
) -> tuple[dict[str, Any], int]:
    """`public_feedback` under the closed scalar grammar.

    Entries are considered in sorted key order, so the kept set is a function
    of the mapping and not of the scorer's serialization order. Every entry
    that fails the grammar, holds a run secret in its key or value, or
    falls past the `feedback_keys` cap, is dropped and counted.
    """

    def contains_secret(value: str) -> bool:
        return any(secret and secret in value for secret in secrets)

    forbidden = check_predicate(
        "input_run_secret_redaction",
        "redacted",
        contains_secret,
        modes=ENFORCE_ALL if pin is None else CheckModes(cast(Mapping[str, Mode], pin.input_modes)),
        hits=hits,
    )
    kept: dict[str, Any] = {}
    omitted = 0
    for key in sorted(mapping, key=str):
        value = mapping[key]
        if (
            _identifier(key)
            and _scalar_ok(value, limits["feedback_value_chars"])
            and not forbidden(f"{key}\n{value}")
            and len(kept) < limits["feedback_keys"]
        ):
            kept[key] = value
        else:
            omitted += 1
    return kept, omitted


class _TraceView:
    """The trace's `input.view` object as the digest recomputation wants it."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def to_payload(self) -> Mapping[str, Any]:
        return self._payload


#: Per carried sample `(case_id, sample_id)` of a measurement, its in-run
#: source try's own `scoring_started` and `scoring_finished`
#: (`baseline_import.carried_traces`; family 12 only, ADR-0017 amended 2026-10-02).
CarriedTraces = Mapping[tuple[str, str], tuple[Envelope, Envelope]]


def _scoring_records(
    records: Sequence[Envelope], carried: CarriedTraces | None = None
) -> tuple[Mapping[str, Any], dict[tuple[str, str], Envelope], dict[tuple[str, str], Envelope]]:
    """The run identity, and per sample its `scoring_started` and `scoring_finished`.

    `carried` offers a carried row's source scoring records under the row's
    own key; a sample the journal scored itself keeps its own. With `None`
    (every family below 12, and any row carried across a link) this is the
    journal's records alone, exactly as before.
    """

    identity = records[0].payload["identity"] if records else {}
    started: dict[tuple[str, str], Envelope] = {}
    finished: dict[tuple[str, str], Envelope] = {}
    for record in records:
        if not record.event.startswith("scoring_"):
            continue
        key = (str(record.payload["case_id"]), str(record.payload["sample_id"]))
        if record.event == "scoring_started":
            started[key] = record
        elif record.event == "scoring_finished":
            finished[key] = record
    for key, (source_started, source_finished) in (carried or {}).items():
        if key not in started and key not in finished:
            started[key] = source_started
            finished[key] = source_finished
    return identity, started, finished


def project_observations(
    *,
    records: Sequence[Envelope],
    run_root: Path,
    corpus: Sequence[EvalCase],
    baseline: ArtifactRef,
    limits: Mapping[str, int] = BRIEF_LIMITS_V2,
    carried: CarriedTraces | None = None,
    visible: Callable[[str], bool] | None = None,
    secrets: Sequence[str] = (),
    pin: EvidencePin | None = None,
    hits: HitSink | None = None,
) -> dict[tuple[str, str], Observation]:
    """One `Observation` per scheduled sample of the baseline measurement.

    `records` is the measurement journal's durable prefix (`Measurement.records`),
    `baseline` the published copy the measurement scored (its digest is the
    measurement identity's `candidate_digest`). A sample with no
    `scoring_started` never reached the scorer: `absent`. Otherwise the trace
    named by `score_request_id` is located and bound to the record before
    anything is read from it; any mismatch is `unavailable`.

    The baseline's `CandidateView` is built and verified once for the whole
    call (`_verified_view`); when that one verification fails, every sample
    that reached the scorer is `unavailable`, because none of their traces can
    be bound to a tree this call cannot prove.

    `carried` (family 12 only) offers a row carried from a measurement of the
    same run its source try's scoring records (`_scoring_records`).

    `visible` (family 14 only) drops each `skill_read` event whose path is
    hidden, counting it in the turn's `events_omitted` (`_turns`); `None`
    keeps every earlier family byte for byte.

    `secrets` is the run's secret set: a feedback entry holding one is
    dropped and counted in `feedback_omitted`.
    """

    identity, started, finished = _scoring_records(records, carried)
    cases_by_id = {case.case_id: case for case in corpus}
    environment_kind = str(identity.get("environment", {}).get("kind", ""))
    scorer = identity.get("scorer", {})
    candidate = _verified_view(baseline)
    from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

    traces = DiagnosticTraceReader(run_root)
    observations: dict[tuple[str, str], Observation] = {}
    for entry in identity.get("schedule", ()):
        key = (str(entry["case_id"]), str(entry["sample_id"]))
        record = started.get(key)
        case = cases_by_id.get(key[0])
        if record is None or case is None:
            observations[key] = ABSENT_OBSERVATION
            continue
        if candidate is None:
            observations[key] = UNAVAILABLE_OBSERVATION
            continue
        observations[key] = _observation(
            run_root,
            record,
            finished.get(key),
            case,
            candidate,
            scorer,
            environment_kind,
            limits,
            visible=visible,
            traces=traces,
            secrets=secrets,
            pin=pin,
            hits=hits,
        )
    return observations


def _verified_view(baseline: ArtifactRef) -> CandidateView | None:
    """The baseline's view, verified once for the whole call, else `None`.

    `scoring_input_digest` calls `candidate.verify()` for every sample it
    binds, and the verifier `ArtifactRef.candidate_view` installs is a full
    published-root scan. The tree cannot change under one call, so the scan is
    hoisted here: the returned view carries a memoizing verifier whose first
    successful call scans and whose later calls return the same digest without
    touching the disk, and it is verified once up front so the scan happens
    here and not inside the first sample. The memo lives exactly as long as
    this call — a later `project_observations` scans again.
    """

    view = baseline.candidate_view()
    verifier = view.verifier
    assert verifier is not None  # `candidate_view` always installs one.
    scanned = False

    def verify_once(root: Path) -> str:
        nonlocal scanned
        if scanned:
            return view.digest
        digest = verifier(root)
        scanned = True
        return digest

    memoized = replace(view, verifier=verify_once)
    try:
        memoized.verify()
    except (CandidateIntegrityError, ArtifactError, OSError, ValueError):
        return None
    return memoized


def _observation(
    run_root: Path,
    started: Envelope,
    finished: Envelope | None,
    case: EvalCase,
    candidate: CandidateView,
    scorer: Mapping[str, Any],
    environment_kind: str,
    limits: Mapping[str, int],
    *,
    visible: Callable[[str], bool] | None = None,
    traces: ScorerTraceReader | None = None,
    secrets: Sequence[str] = (),
    pin: EvidencePin | None = None,
    redact: Callable[[Any], Any] | None = None,
    hits: HitSink | None = None,
) -> Observation:
    request_id = str(started.payload["score_request_id"])
    from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

    reader = DiagnosticTraceReader(run_root) if traces is None else traces
    trace = read_trace(reader, started, ("request",))
    view = _bound_view(
        None if trace is None else trace.request, started, request_id, case, candidate, scorer
    )
    if view is None:
        return UNAVAILABLE_OBSERVATION
    from evolution.tree.evidence_view import render_turns

    if pin is not None:
        from evolution.tree.roles.input_secrets import input_secret_value

        if hits is not None and pin.input_modes["input_run_secret_redaction"] == "shadow":
            input_secret_value(
                view,
                secrets,
                pin=pin,
                hits=hits,
                location=f"trace/{case.case_id}/{started.payload['sample_id']}",
            )

    if pin is not None and redact is None:

        def redact(value: Any) -> Any:
            return input_secret_value(value, secrets, pin=pin)

    effective_limits = limits if pin is None else {**limits, **pin.settings.limits()}
    # Keep b12 turns for grouping's derived fields, independently of the full view.
    turns = _turns(
        view,
        environment_kind,
        limits if pin is None or pin.settings.view == "full" else effective_limits,
        visible=visible,
        secrets=() if pin is None else secrets,
        redact=redact if pin is not None else None,
    )
    if turns is None:
        return UNAVAILABLE_OBSERVATION
    full_turns = None
    if pin is not None and pin.settings.view == "full":
        raw_turns = view.get("history", {}).get("turns")
        if not isinstance(raw_turns, list):
            return UNAVAILABLE_OBSERVATION
        rendered, _ = render_turns(raw_turns, pin.settings, secrets, redact=redact)
        full_turns = tuple(rendered)
    shown, truncated = turns
    grouping_turns = None
    if pin is not None and pin.settings.view == "allowlist":
        # R9: projection limits never change grouping's derived observation fields.
        legacy = _turns(
            view, environment_kind, limits, visible=visible, secrets=secrets, redact=redact
        )
        if legacy is None:
            return UNAVAILABLE_OBSERVATION
        grouping_turns = legacy[0]
    feedback: dict[str, Any] = {}
    feedback_status = ABSENT
    feedback_omitted = 0
    if finished is not None:
        try:
            output = read_trace(reader, started, ("stdout",))
            document = parse_scorer_stdout(
                decode_trace_text(None if output is None else output.stdout)
            )
            public = document.get("public_feedback", {})
            if not isinstance(public, Mapping):
                raise ScoringError("public_feedback is not an object")
            # `feedback_of` runs under the same guard as the read, and the
            # guard is broad for one reason: every value it walks is the
            # scorer's own output, and this module's rule is that a broken
            # source is `unavailable`, never a raise that escapes `brief_for`
            # and ends the campaign. `OSError`, `UnicodeDecodeError` and
            # `ScoringError` are the expected failures — a pathologically
            # nested `stdout` is a `ScoringError` because `parse_scorer_stdout`
            # reads `RecursionError` as the parse failure it is — and the rest
            # is what a value of the scorer's own choosing can still provoke.
            feedback, feedback_omitted = feedback_of(
                public, effective_limits, secrets, pin=pin, hits=hits
            )
        except ArtifactCorruptionError:
            raise
        except Exception:
            feedback_status = UNAVAILABLE
        else:
            feedback_status = PRESENT
    return Observation(
        PRESENT,
        turns=shown,
        turns_truncated=truncated,
        feedback=feedback,
        feedback_status=feedback_status,
        feedback_omitted=feedback_omitted,
        full_turns=full_turns,
        grouping_turns=grouping_turns,
    )


def read_trace(
    traces: ScorerTraceReader, started: Envelope, sections: tuple[str, ...]
) -> ScorerTraceRecord | None:
    """The started try's trace sections, or `None` when they cannot be read.

    The pack id is the `run_id` of the `scoring_started` envelope itself, so
    an in-run carried row reads its source measurement's trace (ADR-0040 D6).
    Every unreadable state is `None`: the callers treat it exactly as they
    treated a missing or unreadable file.
    """

    try:
        return traces.read(
            str(started.payload["score_request_id"]),
            measurement_id=str(started.to_payload()["run_id"]),
            sections=sections,
        )
    except ArtifactCorruptionError:
        raise
    except Exception:
        return None


def _bound_view(
    request: bytes | None,
    started: Envelope,
    request_id: str,
    case: EvalCase,
    candidate: CandidateView,
    scorer: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The trace's `view`, once the trace is proved to be this scoring's.

    Reads `request.json` whole — the scorer's stdin, `oracle` included — and
    the corpus case whole, to recompute `scoring_input_digest`; returns only
    the `view` object, and only when every check holds.

    `candidate` is the call's one verified view (`_verified_view`).
    `scoring_input_digest` still calls `candidate.verify()` — the digest is
    defined over a tree proved at the moment it is taken — but that verifier
    is memoized for the call, so the published root is scanned once per
    `project_observations` and not once per sample.

    The whole read-and-bind path is one failure boundary, and `_read_and_bind`
    below carries no guard of its own. `request.json` is operator-owned but
    externally produced input: the scorer subprocess wrote it, and nothing this
    module knows constrains it before it is read. Reading it, parsing it,
    checking its shape, canonicalizing it and recomputing the digest are each a
    way it can fail, and a trace that fails any of them is `unavailable` —
    never a raise that escapes `project_observations`, `brief_for` and
    `run_editor` and ends the campaign, and never a value that reaches the
    editor unauthenticated.
    """

    try:
        return _read_and_bind(request, started, request_id, case, candidate, scorer)
    except Exception:
        # Deliberately every `Exception`, because every layer this path touches
        # has one of its own: `OSError` and `UnicodeDecodeError` from the read,
        # `ValueError` (`JSONDecodeError` included) from the parse and from the
        # payload's shape, `RecursionError` from a pathologically nested
        # document — the C decoder accepts depths the encoder cannot
        # canonicalize, so the parse and the re-serialization fail
        # independently and neither guards the other — `TypeError` from a value
        # of a type the canonicalizer or the digest does not expect,
        # `MemoryError` from a document large enough to exhaust the process,
        # and `IdentityError`, `ArtifactError` and `CandidateIntegrityError`
        # from the digest recomputation's reach into identity, the artifact
        # layer and the candidate's integrity check. `KeyboardInterrupt` and
        # `SystemExit` are not `Exception` and still stop the run.
        return None


def _read_and_bind(
    request: bytes | None,
    started: Envelope,
    request_id: str,
    case: EvalCase,
    candidate: CandidateView,
    scorer: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """`_bound_view`'s work, unguarded: its caller is the one failure boundary."""

    document = json.loads(decode_trace_text(request))
    if not isinstance(document, Mapping) or set(document) != _REQUEST_KEYS:
        return None
    if document["trace_id"] != request_id or document["scorer"] != scorer.get("digest"):
        return None
    payload = document["input"]
    if not isinstance(payload, Mapping) or set(payload) != _INPUT_KEYS:
        return None
    if payload["case_id"] != case.case_id or not isinstance(payload["view"], Mapping):
        return None
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if hashlib.sha256(raw).hexdigest() != document["input_sha256"]:
        return None
    # `scoring_input_digest` canonicalizes `view` a second time, so it fails on
    # everything the line above can fail on and on identity and the artifact
    # layer besides. It is not guarded here: `_bound_view` is the boundary.
    digest = scoring_input_digest(case, _TraceView(payload["view"]), candidate, scorer)
    if digest != started.payload.get("input_digest"):
        return None
    return payload["view"]


def _turns(
    view: Mapping[str, Any],
    environment_kind: str,
    limits: Mapping[str, int],
    *,
    visible: Callable[[str], bool] | None = None,
    secrets: Sequence[str] = (),
    redact: Callable[[Any], Any] | None = None,
) -> tuple[tuple[ObservationTurn, ...], bool] | None:
    from evolution.tree.execution.input_secrets import redact_run_secrets

    transform = redact if redact is not None else lambda value: redact_run_secrets(value, secrets)
    history = view.get("history")
    turns = history.get("turns") if isinstance(history, Mapping) else None
    if not isinstance(turns, list):
        return None
    shown: list[ObservationTurn] = []
    for turn in turns[: limits["turns"]]:
        if not isinstance(turn, Mapping):
            return None
        message, events = turn.get("message"), turn.get("events")
        if not isinstance(message, Mapping) or not isinstance(events, list):
            return None
        content = transform(message.get("content"))
        if environment_kind == "static" and isinstance(content, str):
            text = ObservationInput(
                PRESENT, content[: limits["input_chars"]], len(content) > limits["input_chars"]
            )
        else:
            # `scripted` delivers `private["script"][i]["content"]`; a kind this
            # projection does not know is withheld until it declares its origin.
            text = ObservationInput(WITHHELD)
        kept: list[Mapping[str, Any]] = []
        omitted = 0
        for event in events:
            projected = (
                shown_event(event, max(limits["text_chars"], len(str(event.get("content", "")))))
                if secrets and isinstance(event, Mapping)
                else shown_event(event, limits["text_chars"])
                if isinstance(event, Mapping)
                else None
            )
            # Family 14: a `skill_read` of a hidden path is omitted like an
            # unshown event, before the `events_per_turn` cap, so the cap and
            # `events_truncated` apply to the visible events only.
            if projected is None or (
                visible is not None and not visible_observation(projected, visible)
            ):
                omitted += 1
            else:
                if secrets:
                    projected = transform(projected)
                    if "content" in projected:
                        content_text = projected["content"]
                        projected["content"] = content_text[: limits["text_chars"]]
                        projected["truncated"] = len(content_text) > limits["text_chars"]
                kept.append(projected)
        # The trace's own `index` and `outcome`, taken only in the shapes the
        # rollout writes: `int` and one of the three terminals. `int(...)` on
        # anything else would raise out of this projection instead of marking
        # the trace `unavailable`, and an unknown `outcome` string would be
        # rendered verbatim into the editor's text.
        index = turn.get("index", len(shown))
        outcome = turn.get("outcome")
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        if outcome not in ("ok", "target_failed", "candidate_failed"):
            return None
        # `reason` (W19, PDR-0014) is a field, not an event, exactly so the
        # `events_per_turn` cap two lines above cannot drop it: an unusable
        # value -- the wrong key set or a value outside the wire's
        # `[A-Za-z0-9._-]{1,64}` grammar -- is withheld as `None` rather than
        # rendered, following this projection's rule for every other field;
        # the wire already refused anything the grammar forbids, so this
        # check only decides whether a value that reached the trace is safe
        # to put in the editor's text.
        raw_reason = turn.get("reason")
        reason: Mapping[str, str] | None = None
        if isinstance(raw_reason, Mapping) and raw_reason.keys() == {"category", "code"}:
            if all(_identifier(value) for value in raw_reason.values()):
                reason = {"category": raw_reason["category"], "code": raw_reason["code"]}
        cut = kept[: limits["events_per_turn"]]
        shown.append(
            ObservationTurn(
                index=index,
                outcome=transform(outcome),
                input=text,
                events=tuple(cut),
                events_omitted=omitted,
                events_truncated=len(cut) < len(kept),
                reason=transform(reason),
            )
        )
    return tuple(shown), len(turns) > limits["turns"]
