"""Campaign editor orchestration, deadlines and brief assembly.

Role values and serialization live in roles.editor_contracts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.roles.editor_contracts import (
    ABSENT_OBSERVATION,
    EMPTY_HISTORY,
    BriefHistory,
    CandidateEditor,
    Diagnosis,
    EditBrief,
    EditBriefCase,
    EditBriefRepeat,
    EditRequest,
    EditResult,
    Observation,
    _brief_limits,
    brief_version_for,
    cite_observations,
    with_attempt_feedback,
)

EDITOR_GRACE_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.05


if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class EditorOutcome:
    """The executor's mapping of one `run_editor` call (spec §9 / §7 terminal barrier)."""

    terminal: Literal["finished", "failed"]
    status: str | None
    error_class: str | None
    note: str
    alive: bool
    code: str | None = None
    continued: bool = False


def build_brief(
    rows: Sequence[SampleRow],
    corpus: Sequence[EvalCase],
    policy: CampaignPolicyV11,
    champion_digest: str,
    *,
    diagnosis: Diagnosis,
    observations: Mapping[tuple[str, str], Observation] | None = None,
    history: BriefHistory = EMPTY_HISTORY,
    attempt_feedback: Mapping[str, Any] | None = None,
    sentence_chars: int | None = 512,
    pin: EvidencePin | None = None,
) -> EditBrief:
    """Build the round's edit brief from the baseline measurement's rows.

    One `EditBriefCase` per corpus case in corpus order; one `EditBriefRepeat`
    per row of that case in repeat order. `metrics` itself, `private` and
    `oracle` never travel here -- only `metrics[policy.metric_primary]` (or
    `None` when the row has no metrics, or lacks that key) and
    `critical_pass`.

    `observations` is keyed by `(case_id, sample_id)` (`editor_evidence.project_observations`);
    a row without one is `absent`. `history` is `editor_evidence.project_history`'s
    reduction; a caller without one hands an empty history, which is what the
    first round's editor sees. `diagnosis` is required -- a brief is built
    only after a gate scope (Task 10), so `campaign.brief_for` always has one
    to hand it (`diagnosis_for_brief`).

    `sentence_chars` is the round's Reflector group text limit (#282), which
    the brief's `limits` publish; `None` (no per-field limit) omits the key,
    and 512 keeps `BRIEF_LIMITS_V3` byte for byte.
    """

    rows_by_case: dict[str, list[SampleRow]] = {}
    for row in rows:
        rows_by_case.setdefault(row.case_id, []).append(row)

    cases: list[EditBriefCase] = []
    for case in corpus:
        case_rows = sorted(rows_by_case.get(case.case_id, ()), key=lambda row: row.repeat)
        repeats = tuple(
            EditBriefRepeat(
                repeat=row.repeat,
                sample_id=row.sample_id,
                terminal=row.outcome or "not_started",
                primary_score=(
                    row.metrics[policy.metric_primary]
                    if row.metrics is not None and policy.metric_primary in row.metrics
                    else None
                ),
                critical_pass=row.critical_pass,
                observation=(observations or {}).get(
                    (row.case_id, row.sample_id), ABSENT_OBSERVATION
                ),
            )
            for row in case_rows
        )
        cases.append(
            EditBriefCase(
                case_id=case.case_id,
                group_id=case.group_id,
                stratum=case.stratum,
                tags=case.tags,
                # A read-only copy, so an `EditBriefCase` never aliases the
                # corpus case it was projected from, and the one brief object
                # every attempt of a round is handed cannot be mutated through
                # this field by one of them; the values below the copy are
                # the corpus case's own frozen ones. `brief_payload` and
                # `render_catalog` serialize it through `plain_public`.
                public=MappingProxyType(dict(case.public)),
                repeats=repeats,
            )
        )
    brief = EditBrief(
        champion_digest=champion_digest,
        operator_context=policy.editor_context,
        cases=tuple(cases),
        diagnosis=diagnosis,
        limits=_brief_limits(sentence_chars)
        if pin is None
        else MappingProxyType({**_brief_limits(sentence_chars), **pin.settings.limits()}),
        pin=pin,
        version=brief_version_for(pin, feedback=False),
        history=history,
    )
    if pin is not None:
        from evolution.tree.roles.editor.editor_brief import _observation_evidence_id

        by_id = {
            _observation_evidence_id(case.case_id, repeat.repeat): repeat.observation
            for case in brief.cases
            for repeat in case.repeats
        }
        cited, omitted = cite_observations(diagnosis, by_id, pin.settings.cited_chars)
        brief = replace(brief, cited=tuple(cited), cited_omitted=omitted)
    return brief if attempt_feedback is None else with_attempt_feedback(brief, attempt_feedback)


def _outcome_for(task: asyncio.Task[EditResult], *, cancelled_reason: str | None) -> EditorOutcome:
    if task.cancelled():
        return EditorOutcome("failed", None, cancelled_reason or "cancelled", "", False)
    exc = task.exception()
    if exc is not None:
        return EditorOutcome("failed", None, f"exception:{type(exc).__name__}", str(exc), False)
    result = task.result()
    if cancelled_reason is not None:
        return EditorOutcome("failed", None, cancelled_reason, result.note or "", False)
    if result.status == "failed":
        return EditorOutcome("failed", None, "reported", result.note, False, result.code)
    return EditorOutcome(
        "finished", result.status, None, result.note, False, None, result.continued
    )


async def run_editor(
    editor: CandidateEditor, request: EditRequest, *, cancelled: Callable[[], bool]
) -> EditorOutcome:
    """Run `editor.edit(request)`, enforcing the deadline and cooperative cancellation.

    On a deadline or a `cancelled()` signal the task is cancelled and awaited
    for at most `EDITOR_GRACE_SECONDS` (read from the module at call time, so
    a caller that monkeypatches the module attribute is honoured immediately).
    A task still alive after the grace is reported `alive=True`; ending the
    campaign over it is the executor's decision (spec §7 terminal barrier),
    not this function's.
    """

    task: asyncio.Task[EditResult] = asyncio.ensure_future(editor.edit(request))
    deadline_at = time.monotonic() + request.deadline_seconds
    reason: str | None = None
    while not task.done():
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            reason = "timeout"
            break
        if cancelled():
            reason = "cancelled"
            break
        await asyncio.wait({task}, timeout=min(_POLL_INTERVAL_SECONDS, remaining))
    if reason is None:
        return _outcome_for(task, cancelled_reason=None)
    task.cancel()
    _done, pending = await asyncio.wait({task}, timeout=EDITOR_GRACE_SECONDS)
    if task in pending:
        return EditorOutcome("failed", None, "unresponsive", "", True)
    return _outcome_for(task, cancelled_reason=reason)


def unchanged(workspace: Path, policy: ArtifactPolicyV1, parent_digest: str) -> bool:
    """S2's "produces no change" case: the workspace's candidate digest equals the parent's."""

    return scan_tree(workspace, policy).candidate_digest == parent_digest
