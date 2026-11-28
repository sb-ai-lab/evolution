"""Resolve a project evaluation without deciding its compatibility with a campaign."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evolution_lab.owner import BaselineImportChoice, OwnerError
from evolution_lab.owner.linked import require_evaluation_source
from evolution_lab.owner.store import read_saved_operation_state

from ._errors import QuickstartError
from ._evaluate import SUMMARY_SCHEMA

_MANIFEST_15_WARNING = (
    "On /15, imported verdict-only failures cannot be diagnosed until the first baseline refresh (#208)."
)
_NO_TRACES_WARNING = "The source has no scorer traces and imported rows carry no source verdicts."
SAVED_STATE_WARNING = "This pair state could not be read. Lab used the evaluation summary and saved journal."


def baseline_summary(source, *, run=None, operation_id=None, reason=None, used_saved_summary=False):
    summary = {
        "source": source, "run": run, "operation_id": operation_id, "reason": reason,
        "imported": None, "to_measure": None, "not_imported": None, "fields": [],
        "authorization_id": None, "held_out_authorization_id": None,
    }
    if used_saved_summary:
        summary["used_saved_summary"] = True
    return summary


def _evaluation_summaries(runs):
    paths = (p for p in runs.glob("[0-9]*/summary.json") if p.parent.name.isascii()
             and p.parent.name.isdecimal())
    for path in sorted(paths, key=lambda p: int(p.parent.name), reverse=True):
        try:
            document = json.loads(path.read_bytes())
            if (isinstance(document, dict) and document.get("schema") == SUMMARY_SCHEMA
                    and isinstance(document.get("evaluation"), dict)):
                yield path.parent.name, document
        except (OSError, ValueError):
            continue


async def _evaluation_choice(owner, lab_directory, document):
    evaluation = document["evaluation"]
    identifier = evaluation.get("operation_id")
    try:
        state = Path(document["state"])
        if (not state.is_absolute() or state.parent != lab_directory
                or not state.name.startswith("state-") or state.is_symlink()):
            raise ValueError
        choice = await asyncio.to_thread(BaselineImportChoice.for_operation, state, identifier)
        if not choice.source.is_relative_to(state):
            raise ValueError
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise OwnerError("baseline_source_unknown", "before_start") from None

    from_summary = False
    if choice.source_state == owner.state_directory:
        try:
            status = await owner._db("operation_status", identifier)
        except OwnerError as error:
            if error.code in {"not_found", "invalid_argument"}:
                raise OwnerError("baseline_source_unknown", "before_start") from None
            raise
        kind, control, results = status.kind, status.control_state, status.results_state
    else:
        try:
            saved = await asyncio.to_thread(read_saved_operation_state, choice.source_state, identifier)
        except OwnerError:
            from_summary = True
            saved = ("evaluation", evaluation.get("control_state"), evaluation.get("results_state"))
        if saved is None:
            raise OwnerError("baseline_source_unknown", "before_start")
        kind, control, results = saved
    await asyncio.to_thread(require_evaluation_source, kind, control, results, choice.source)
    return choice, from_summary


def _has_scorer_traces(source: Path) -> bool:
    from evolution.tree import (
        JournalBoundaryError, ScorerTraceUnreadable, inspect_journal, read_scorer_trace,
    )

    try:
        records = inspect_journal(source).records
    except (JournalBoundaryError, OSError, ValueError):
        return False
    for record in records:
        if record.event != "scoring_finished":
            continue
        envelope = record.to_payload()
        try:
            trace = read_scorer_trace(
                source, envelope["payload"]["score_request_id"], measurement_id=envelope["run_id"],
                sections=(), run_boundary="nofollow",
            )
        except (ScorerTraceUnreadable, OSError, ValueError):
            continue
        if trace is not None:
            return True
    return False


async def select_baseline(owner, runs, *, schema, baseline, baseline_from, linked_from):
    """Select the latest eligible evaluation first, then assess its trace availability."""
    if baseline == "fresh":
        return None, baseline_summary("fresh", reason="requested"), []
    if linked_from is not None:
        return None, baseline_summary("linked", operation_id=linked_from), []
    if schema != "evolution-v2-manifest/16" and baseline_from is None:
        return None, baseline_summary("fresh", reason="manifest_15"), []

    summaries = await asyncio.to_thread(lambda: list(_evaluation_summaries(runs)))
    for run, document in summaries:
        if baseline_from is not None and baseline_from not in (run, document["evaluation"].get("operation_id")):
            continue
        try:
            choice, from_summary = await _evaluation_choice(owner, runs.parent, document)
        except OwnerError as error:
            if error.code not in {"baseline_source_unknown", "baseline_source_deleted"}:
                raise
            if baseline_from is not None:
                raise QuickstartError(error.code, str(error)) from None
            continue
        has_traces = await asyncio.to_thread(_has_scorer_traces, choice.source)
        warnings = [SAVED_STATE_WARNING] if from_summary else []
        if not has_traces and baseline_from is None:
            summary = baseline_summary("fresh", reason="no_scorer_traces", used_saved_summary=from_summary)
            return None, summary, warnings
        if schema != "evolution-v2-manifest/16":
            warnings.append(_MANIFEST_15_WARNING)
        if not has_traces:
            warnings.append(_NO_TRACES_WARNING)
        summary = baseline_summary("evaluation", run=run, operation_id=choice.source_operation_id,
                                   used_saved_summary=from_summary)
        return choice, summary, warnings
    if baseline_from is not None:
        raise QuickstartError("baseline_source_unknown", "The project has no such evaluation run.")
    return None, baseline_summary("fresh", reason="no_evaluation"), []
