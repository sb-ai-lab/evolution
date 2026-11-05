"""Advisory free-inode estimates (ADR-0040, PDR-0033).

Pure arithmetic over bound inputs and existing paths. Estimates are not
reservations or completion guarantees; tree growth and corruption segments
can require additional entries.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: One normal pack per measurement journal, independent of its sample count.
TRY_INODES: Final = 1
#: `measurements/<id>/`, `events.jsonl`, `.owner.lock`.
MEASUREMENT_INODES: Final = 3
#: Connection journal directory, journal, lock, trace directory, summary, pack.
PREFLIGHT_INODES: Final = 6
#: Transient role control files per concurrent role operation (ADR-0040 L).
ROLE_CONTROL_INODES: Final = 16
#: A fresh campaign run directory's fixed entries (ADR-0040).
CAMPAIGN_FIXED_ENTRIES: Final = 16
#: A standalone evaluation's fixed entries (ADR-0040), by run-relative path.
EVALUATION_FIXED_ENTRIES: Final = (
    "evaluation.jsonl",
    "evaluation.lock",
    "artifacts",
    "artifacts/artifacts.jsonl",
    "artifacts/.artifacts.lock",
    "artifacts/artifacts",
    "artifacts/artifacts/champions",
    "artifacts/artifacts/candidates",
    "artifacts/artifacts/.workspaces",
    "artifacts/artifacts/.staging",
    "measurements",
    "scorer-traces",
    "logs",
)
#: The evaluation's measurement: its directory, journal and lock.
EVALUATION_MEASUREMENT_ENTRIES: Final = (
    "measurements/evaluation",
    "measurements/evaluation/events.jsonl",
    "measurements/evaluation/.owner.lock",
)


@dataclass(frozen=True)
class InodeNeed:
    """What a run needs in free inodes before it measures.

    `floor` estimates next work; `whole_run`, when set, estimates all rounds.
    `per_round` and `start` let the note estimate the rounds covered.
    """

    floor: int
    whole_run: int | None = None
    per_round: int | None = None
    start: int = 0


@dataclass(frozen=True)
class CampaignTerms:
    """ADR-0040's campaign symbols, read from the bound manifest."""

    attempts: int  # A, `attempts_per_round`
    parallel: int  # P, `parallel_attempts`
    rounds: int
    cases: int  # N, the train cases measured
    repeats: int  # K, `repeats_per_question`
    fresh_parent: bool  # fp
    refresh: bool  # r, `baseline_refresh_after_rounds > 0`
    stability: str  # `stable` | `unstable`
    round_stop: str  # `first_accepted` | `all_attempts`
    probe_budget: int | None  # B, `None` without a declared probe
    growth: int  # g, `max_changed_files` or 0
    shared_tmp: bool  # s, `$TMPDIR` on the run directory's filesystem
    scorer_concurrency: int  # `max_scorer_concurrency`


def count_tree(root: Path) -> int:
    """F of a published copy, by listing entries without reading contents.

    Symlinks are counted, never followed. An unreadable copy counts 0: the
    floor is a floor, and the store's own checks report a damaged copy.
    """

    try:
        if not root.is_dir() or root.is_symlink():
            return 0
    except OSError:
        return 0
    count = 1
    pending = [root]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    count += 1
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError:
            continue
    return count


def shares_tmp(probe: Path) -> bool:
    """s: whether `$TMPDIR` is on `probe`'s filesystem; `True` when unknown."""

    try:
        return os.stat(tempfile.gettempdir()).st_dev == os.stat(probe).st_dev
    except OSError:
        return True


def existing_ancestor(path: Path) -> Path:
    """The nearest existing ancestor, as the byte check finds it."""

    probe = path if path.parts else Path(".")
    while not os.path.exists(probe) and probe != probe.parent:
        probe = probe.parent
    return probe


def measurements_per_round(terms: CampaignTerms) -> int:
    """Mr: the measurements one family-14 round can bind."""

    attempts, fresh = terms.attempts, int(terms.fresh_parent)
    refresh = int(terms.refresh)
    if terms.stability == "unstable" and terms.round_stop == "first_accepted":
        return attempts * (3 + fresh) + refresh
    if terms.stability == "unstable":
        return attempts * (2 + fresh) + 1 + refresh
    return attempts * (2 + fresh) + refresh


def trace_inodes(terms: CampaignTerms) -> int:
    """t: one normal trace pack per measurement journal."""

    return TRY_INODES


def other_entries_per_round(terms: CampaignTerms) -> int:
    """L: everything else one round writes."""

    probe = 0 if terms.probe_budget is None else 2 + terms.probe_budget
    return (
        terms.attempts + 5 + probe + PREFLIGHT_INODES + ROLE_CONTROL_INODES * (terms.parallel + 1)
    )


def scorer_slots(terms: CampaignTerms) -> int:
    """Q: the most scorer processes running at once in a campaign."""

    return terms.scorer_concurrency * (terms.parallel + 1)


def round_need(terms: CampaignTerms, tree: int) -> int:
    """`F + (F + g)(A + 1 + P(2 + s)) + Mr(3 + t) + L + (2 + s)Q`."""

    shared = int(terms.shared_tmp)
    copies = terms.attempts + 1 + terms.parallel * (2 + shared)
    return (
        tree
        + (tree + terms.growth) * copies
        + measurements_per_round(terms) * (MEASUREMENT_INODES + trace_inodes(terms))
        + other_entries_per_round(terms)
        + (2 + shared) * scorer_slots(terms)
    )


def start_need(terms: CampaignTerms, tree: int, documents: int) -> int:
    """What a fresh run writes before round 1: v0, `m-r00-champion`, fixed entries."""

    return tree + MEASUREMENT_INODES + trace_inodes(terms) + CAMPAIGN_FIXED_ENTRIES + documents


def campaign_need(terms: CampaignTerms, tree: int, *, fresh: bool, documents: int = 0) -> InodeNeed:
    """The campaign floor: one round, plus the start terms on a fresh run."""

    per_round = round_need(terms, tree)
    start = start_need(terms, tree, documents) if fresh else 0
    return InodeNeed(
        floor=per_round + start,
        whole_run=terms.rounds * per_round + start,
        per_round=per_round,
        start=start,
    )


def evaluation_need(*, tree: int, samples: int, scorer_concurrency: int, shared_tmp: bool) -> int:
    """`F + (3 + t_e) + 13 + 1 + (2 + s)Q`, the evaluation's creation floor."""

    return (
        tree
        + MEASUREMENT_INODES
        + TRY_INODES
        + len(EVALUATION_FIXED_ENTRIES)
        + 1
        + (2 + int(shared_tmp)) * scorer_concurrency
    )


def evaluation_resume_need(
    root: Path,
    *,
    tree: int | None,
    unscored: int,
    scorer_concurrency: int,
    shared_tmp: bool,
) -> int:
    """The creation floor minus what already exists, item by item.

    `tree` is F when the v0 copy is not complete yet, else `None`; `unscored`
    counts the samples without a durable score.
    """

    missing = _missing(root, (*EVALUATION_FIXED_ENTRIES, *EVALUATION_MEASUREMENT_ENTRIES))
    pack = int(unscored > 0 and not os.path.lexists(root / "scorer-traces/evaluation.pack"))
    return (tree or 0) + missing + pack + 1 + (2 + int(shared_tmp)) * scorer_concurrency


def _low_floor_note(free: int, floor: int) -> str | None:
    """A forecast informs requested work without vetoing it."""

    if free >= floor:
        return None
    return (
        f"free inodes {free} are below the estimated {floor} for the requested next work; "
        "this estimate does not block requested work, but actual storage exhaustion "
        "may still fail Evolution or external programs"
    )


def whole_run_note(free: int, need: InodeNeed) -> str | None:
    """The note for a run that clears the floor but not its whole-run estimate."""

    if need.whole_run is None or not need.per_round or free >= need.whole_run:
        return None
    covered = max(0, (free - need.start) // need.per_round)
    return (
        f"free inodes {free} are below this run's estimate of {need.whole_run} for all its "
        f"rounds; by the estimate they last {covered} round(s)"
    )


def _missing(root: Path, entries: Iterable[str]) -> int:
    return sum(not os.path.lexists(root / entry) for entry in entries)
