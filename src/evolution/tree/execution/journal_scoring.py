"""Durable, identity-bound scoring with no post-crash input replay.

A scorer that offers `score_traced` (`rollout.TracingScorer`) is handed the
journaled `score_request_id` as its trace id and `journal.run_id` as its
pack id, so its private trace can be found without a new journal field.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from evolution.tree.execution.interaction import (
    CandidateView,
    EvalCase,
    Scorer,
    ScoreResult,
    SemanticIdentity,
    Trajectory,
    validate_score_result,
)
from evolution.tree.execution.journal import Journal
from evolution.tree.execution.journal_identity import (
    IdentityError,
    canonical_json,
    case_payload,
    scorer_binding,
    scoring_input_digest,
)
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.execution.rollout_scoring import ScoringError
from evolution.tree.execution.scorer_traces import PACK_ID
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalBoundaryError, JournalRecoveryRequired


async def durable_score(
    journal: Journal,
    scorer: Scorer,
    case: EvalCase,
    trajectory: Trajectory,
    candidate: CandidateView,
    *,
    retry_ordinal: int | None = None,
) -> ScoreResult:
    """Score an admitted execution once, or return its durable metric summary.

    Reads the sample's latest try; `retry_ordinal` numbers the scoring records
    and is `None` exactly when the identity declares no `sample_retries`.
    """
    if journal.recovery_required:
        raise JournalRecoveryRequired()
    journal.identity.require_retry_ordinal(
        retry_ordinal,
        repair=journal.sample_repairs(case.case_id, trajectory.sample_id) > 0,
    )
    identity = journal.identity.to_payload()
    corpus = journal.identity.validate_corpus(journal.corpus)
    binding = scorer_binding(scorer)
    if (
        binding != identity["scorer"]
        or case.case_id not in corpus
        or canonical_json(case_payload(case)) != canonical_json(case_payload(corpus[case.case_id]))
        or candidate.digest != identity["candidate_digest"]
        or trajectory.case_id != case.case_id
        or trajectory.candidate_digest != candidate.digest
        or trajectory.environment_id != identity["environment"]["digest"]
        or trajectory.provenance.target.semantic != SemanticIdentity(**identity["target"])
    ):
        raise IdentityError("run_identity_changed")
    execution, prior = journal.sample_records(case.case_id, trajectory.sample_id)
    if (
        execution is None
        or execution.payload.get("outcome") != trajectory.terminal
        or trajectory.disposition.kind != "score"
    ):
        raise ValueError("execution is not durably scoreable")
    if prior is not None:
        if prior.event == "scoring_finished":
            return ScoreResult(prior.payload["metrics"], prior.payload["critical_pass"])
        raise ValueError("scoring already started or closed without a score")
    if callable(getattr(scorer, "score_traced", None)) and not PACK_ID.fullmatch(journal.run_id):
        # ADR-0040 D1 (L6): a tracing scorer's pack is named by this journal's
        # `run_id`, for every caller. A caller bug, refused before anything is
        # journaled for the sample.
        raise ValueError("pack id is not a safe file name")
    view = trajectory.scoring_view()
    input_digest = scoring_input_digest(case, view, candidate, binding)
    operation_id = uuid4().hex
    score_request_id = uuid4().hex
    base: dict[str, Any] = dict(
        case_id=case.case_id,
        sample_id=trajectory.sample_id,
        candidate_digest=candidate.digest,
        score_request_id=score_request_id,
    )
    if retry_ordinal is not None:
        base["retry_ordinal"] = retry_ordinal
    journal.append(
        "scoring_started",
        base | dict(scorer=binding, input_digest=input_digest, input_schema_version=2),
        operation_id,
    )
    try:
        if scorer_binding(scorer) != binding:
            raise ScoringError("scorer identity changed")
        score_traced = getattr(scorer, "score_traced", None)
        if callable(score_traced):
            result = await score_traced(
                case, view, candidate, trace_id=score_request_id, pack_id=journal.run_id
            )
        else:
            result = await scorer.score(case, view, candidate)
        if not isinstance(result, ScoreResult):
            raise ScoringError("invalid scoring result")
        validate_score_result(case, result)
        candidate.verify()
        terminal = base | dict(metrics=dict(result.metrics), critical_pass=result.critical_pass)
        # Invalid callee summaries fail as scoring, before a writer sees them. The
        # writer's own secret set is part of that rule: a metric name colliding with
        # a secret is the callee's fault, not an unusable writer (S1IMPL-05).
        try:
            Envelope.from_payload(
                dict(
                    schema_version=1,
                    run_id=journal.run_id,
                    sequence=1,
                    timestamp="2026-01-01T00:00:00Z",
                    event="scoring_finished",
                    operation_id=operation_id,
                    payload=terminal,
                ),
                secrets=journal.secrets,
                family=S1_FAMILY,
            )
        except ValueError:
            raise ScoringError("invalid scoring summary") from None
    except JournalBoundaryError:
        raise
    except asyncio.CancelledError:
        journal.append("scoring_interrupted", base, operation_id)
        raise
    except Exception as exc:
        journal.append("scoring_failed", base | {"failure": "scorer_error"}, operation_id)
        # Only the typed class and metric name survive; never the text.
        code = exc.code if isinstance(exc, ScoringError) else None
        metric = exc.metric if isinstance(exc, ScoringError) else None
        raise ScoringError("scorer failed", code=code, metric=metric) from None
    journal.append("scoring_finished", terminal, operation_id)
    return result
