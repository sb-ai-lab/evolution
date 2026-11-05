"""The round's exam scope, derived once for the executor and the audit (ADR-0021).

`select_gate_scope` writes `gate_scope_selected` from this function and the
read-only audit re-derives the record through it, so the two can never read a
diagnosis differently. Families 5-7 pass no membership and no evaluable set,
which reproduces their scope exactly; family 8 widens the selection to every
case of each family it touches and names the cases the baseline cannot
evaluate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateScope:
    """One round's gate scope.

    `claimed_cases` is the diagnosis' claim, sorted (empty on
    `reflection-failed`); `selected_cases` is the claim on `claim` and the
    fallback otherwise; `cases` is the selection followed by its family
    companions in corpus order; `unevaluable_cases` are the members of `cases`
    the baseline cannot evaluate.
    """

    source: str
    claimed_cases: tuple[str, ...]
    selected_cases: tuple[str, ...]
    cases: tuple[str, ...]
    unevaluable_cases: tuple[str, ...]


def derive_gate_scope(
    *,
    reflection_failed: bool,
    groups: Sequence[Mapping[str, Any]],
    fallback: Sequence[str],
    family_membership: Mapping[str, str] | None,
    corpus_order: Sequence[str],
    evaluable_cases: frozenset[str] | None,
) -> GateScope:
    """The gate scope a settled diagnosis selects.

    `family_membership=None` (or empty) expands nothing; `evaluable_cases=None`
    accounts for no exclusion, so `unevaluable_cases` is empty. The
    single-case rule counts the claim before any expansion.
    """

    claimed = tuple(sorted({case for group in groups for case in group["affected_cases"]}))
    if reflection_failed:
        source, selected = "reflection-failed", tuple(fallback)
    elif not claimed:
        source, selected = "empty-claim", tuple(fallback)
    elif len(claimed) == 1:
        source, selected = "single-case", tuple(fallback)
    else:
        source, selected = "claim", claimed
    cases = list(selected)
    if family_membership:
        touched = {family_membership[case] for case in selected if case in family_membership}
        chosen = set(selected)
        cases.extend(
            case
            for case in corpus_order
            if case not in chosen and family_membership.get(case) in touched
        )
    unevaluable = (
        ()
        if evaluable_cases is None
        else tuple(case for case in cases if case not in evaluable_cases)
    )
    return GateScope(
        source=source,
        claimed_cases=() if reflection_failed else claimed,
        selected_cases=selected,
        cases=tuple(cases),
        unevaluable_cases=unevaluable,
    )
