"""Family 13's final cohort: the derived fields of its records (design §6.7-§6.13; slices F5, F6).

The executor computes them over live measurement rows and the audit over the
rows of the cited prefixes, so the two read one implementation, as
`confidence_gate` does for the gate. The module writes no file and imports
nothing from the executor.
"""

from __future__ import annotations

from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from typing import Any

from evolution.tree.campaign_schema import (
    MAX_FAULT_ROWS,
    champion_member,
    cohort_rows_digest,
    finalist_member,
    member_diagnostic,
    requested_pairing,
)
from evolution.tree.campaign_statistics import (
    MissingMetric,
    SampleRow,
    arbitrate_owner,
    row_owes_a_try,
    score_samples_of,
)
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.confidence_gate import GateInvariant, case_evidence
from evolution.tree.confidence_statistics import (
    DRAWS,
    cohort_contrast,
    decide_global,
    derive_seed,
    pairing_of,
    publish,
    scope_weights,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.sample_outcomes import disposition_for
from evolution.tree.final_statistics import (
    FinalCase,
    decide_global14,
    final_contrast,
    final_pairing,
)

#: Design §8 and D7: owners whose gap is infrastructure -- those whose
#: threshold terminal is `infrastructure_failed`. `rollout` (time budget,
#: interruption, cancellation) is not one (plan choice 9).
INFRASTRUCTURE_OWNERS: frozenset[str] = frozenset({"target", "environment", "scorer"})


@dataclass(frozen=True)
class Member:
    """One member's rows: the latest try of each scheduled pair up to the horizon read.

    `declined` holds the member's tries whose retry the cancelled run declined,
    `(case_id, sample_id, retry_ordinal)` (`declined_tries` over the campaign's
    `retries_declined`): such a try owes no D1 try, so it is a gap (#120).
    """

    measurement_id: str
    role: str
    rows: tuple[SampleRow, ...]
    declined: frozenset[tuple[str, str, int]] = frozenset()


@dataclass(frozen=True)
class Completion:
    """`cohort_measurement_completed`'s computed fields (design §6.9)."""

    complete: bool
    point: float | None
    missing: int
    critical: bool


def gap_owner(row: SampleRow) -> str:
    """The owner a gap's outcome gives (`rollout` for `cancelled`)."""

    if row.disposition == "unscored":
        # A scoreable execution whose scoring never finished: the measurement
        # layer's `_owner_reason` gives it to `rollout` (rollout_interrupted).
        return "rollout"
    if row.outcome is None:
        raise ValueError("a gap has a terminal outcome")
    owner = disposition_for(row.outcome).owner  # type: ignore[arg-type]
    if owner is None:
        raise ValueError("a gap has an owner")
    return str(owner)


_owner = gap_owner


def cohort_gap(row: SampleRow, declined: Container[tuple[str, str, int]] = ()) -> bool:
    """PDR-0022 / ADR-0027 (2026-10-05): is `row` a pair its settled stage left without a score?

    A row that did not score and is owed no try (`row_owes_a_try`): an
    exhausted row, a `cancelled` one (the token's `exclude`, never retried), or
    a try whose retry the cancelled run declined (`declined`). The same set
    `stage_lacking` reads for an Attempt (#120). A row still owed a try is not
    one: its stage is not settled.
    """

    return row.disposition != "score" and not row_owes_a_try(row, declined)


def rollout_owned_gap(row: SampleRow, declined: Container[tuple[str, str, int]] = ()) -> bool:
    """D7: is `row` a pair a finalist may complete without -- a gap, owner `rollout`?

    Never raises: a row with no terminal or no owner is simply not one.
    """

    if not cohort_gap(row, declined):
        return False
    try:
        return _owner(row) == "rollout"
    except ValueError:
        return False


def incomplete_rows(members: Sequence[Member], corpus: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """Design §8 (3): the pairs that suspend `cohort_incomplete`, in canonical order.

    Every gap of the Champion (`cohort_gap`), whatever its owner, each row
    carrying its latest try's `retry_ordinal`; a finalist's
    only when its owner is infrastructure (D7 leaves the others to
    completion). Called only on settled stages -- the cohort barrier runs after
    a stage owes no try, and the audit reads the prefix the suspension read --
    so every row that did not score is a gap; a row still owed a try (only on
    an unsettled stage) is not one and is not listed.
    """

    position = {case_id: index for index, case_id in enumerate(corpus)}
    found: list[dict[str, Any]] = []
    for member in members:
        for row in sorted(member.rows, key=lambda r: (position[r.case_id], r.repeat)):
            if not cohort_gap(row, member.declined):
                continue
            owner = _owner(row)
            # A Champion row may own `infrastructure` (`infrastructure_failed`),
            # which `THRESHOLD_TERMINAL` has no entry for: only barrier step 1
            # (fault rows suspend first) keeps a lookup of it unreachable.
            if member.role == "champion" or owner in INFRASTRUCTURE_OWNERS:
                found.append(
                    {
                        "measurement_id": member.measurement_id,
                        "case_id": row.case_id,
                        "sample_id": row.sample_id,
                        "retry_ordinal": row.retry_ordinal,
                        "owner": owner,
                    }
                )
    return tuple(found)


def cohort_rows_evidence(
    rows: Sequence[Mapping[str, Any]], *, family_version: int = 13
) -> dict[str, Any]:
    """The selected final-stage evidence: first 256 rows, whole count and digest.

    Standalone callers keep family 13's `cohort_rows` default.
    """

    vocabulary = campaign_vocabulary_for(family_version)
    if not rows:
        raise ValueError(
            f"a {vocabulary.suspension_evidence_kind} suspension names at least one pair"
        )
    return {
        "evidence_kind": vocabulary.suspension_evidence_kind,
        "count": len(rows),
        "rows_digest": cohort_rows_digest(rows),
        "truncated": len(rows) > MAX_FAULT_ROWS,
        "rows": [dict(row) for row in rows[:MAX_FAULT_ROWS]],
    }


def cohort_owner(rows: Sequence[Mapping[str, Any]]) -> str:
    """The suspension's owner: `arbitrate_owner` over the rows' owners (D6)."""

    return arbitrate_owner([str(row["owner"]) for row in rows])


def member_completion(
    rows: Sequence[SampleRow],
    *,
    groups: Mapping[str, tuple[str, str]],
    corpus: Sequence[str],
    primary: str,
    repeats: int,
) -> Completion | MissingMetric:
    """Design §6.9 over a member's rows at R; `point` uses F6's full-corpus weights.

    `missing` counts every row that did not score. Completion follows the last
    stage's barrier, so on a genuine journal each is a gap (`cohort_gap`); the
    audit holds an incomplete completion to D7 (`rollout_owned_gap`).
    """

    samples = score_samples_of(rows, groups, primary, repeats)
    if isinstance(samples, MissingMetric):
        return samples
    missing = sum(1 for row in rows if row.disposition != "score")
    critical = any(row.critical_pass is False for row in rows if row.disposition == "score")
    if missing:
        return Completion(False, None, missing, critical)
    by_case: dict[str, list[Fraction]] = {}
    for sample in samples:
        by_case.setdefault(sample.case_id, []).append(Fraction(sample.score))
    if set(by_case) != set(corpus):
        raise ValueError("a complete member scores every corpus case")
    weights = scope_weights(list(corpus), groups)
    point = sum(
        (
            weights[case_id] * sum(values, Fraction(0)) / len(values)
            for case_id, values in by_case.items()
        ),
        Fraction(0),
    )
    return Completion(True, publish(point), 0, critical)


def encode_final_removals(
    payload: Mapping[str, Any],
    *,
    corpus_ids: Sequence[str],
    evidence: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Encode complete removal sets in corpus/repeat order, outside the kernel.

    Lists of at most 64 items remain lists unless the full persisted payload
    exceeds 16 KiB. In that case both nonempty collections become summaries.
    Hashes always cover the complete canonical lists, never a retained prefix.
    ``evidence`` budgets the exact references the caller will append, without
    changing this producer's evidence-free result contract.
    """
    position = {case_id: index for index, case_id in enumerate(corpus_ids)}
    pairs = sorted(payload["removed_pairs"], key=lambda pair: (position[pair[0]], pair[1]))
    cases = sorted(payload["removed_cases"], key=lambda case: position[case])
    complete = {"removed_pairs": [list(pair) for pair in pairs], "removed_cases": cases}

    def summary(values: list[Any]) -> dict[str, Any]:
        return {"count": len(values), "digest": sha256(canonical_json(values)).hexdigest()}

    result = {
        **payload,
        **{
            key: summary(values) if len(values) > 64 else values for key, values in complete.items()
        },
    }

    def size() -> int:
        full = result if evidence is None else {**result, "evidence": list(evidence)}
        return len(canonical_json(full))

    if size() > 16 * 1024:
        result.update({key: summary(values) for key, values in complete.items() if values})
    if size() > 16 * 1024:
        raise ValueError("a final comparison exceeds the payload budget even with summaries")
    return result


def global_comparison_record(
    cohort: Mapping[str, Any],
    completed: Mapping[str, Mapping[str, Any]],
    *,
    members: Sequence[str],
    position: int,
    finalist_rows: Sequence[SampleRow],
    champion_rows: Sequence[SampleRow],
    groups: Mapping[str, tuple[str, str]],
    corpus_ids: Sequence[str],
    primary: str,
    campaign_seed: int,
    draws: int = DRAWS,
    family_version: int = 13,
    stability: str = "unstable",
    stored_champion: Mapping[str, Any] | None = None,
    excluded: frozenset[tuple[str, int]] = frozenset(),
    removal_evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any] | MissingMetric:
    """Design §6.10: one finalist's `global_comparison_completed` without `evidence`.

    `cohort` is the round's bound cohort (with its binding `digest`),
    `completed` its members' completions, `members` its current member ids
    (`RoundState.cohort_current`: a member a `continue` re-bound is named by
    its `<id>@e<k>`, F7 plan choice 7), `position` the finalist's place in
    `cohort["finalists"]`. Both members' rows are cut at the bound R by the
    caller on the fresh path; the stored path cuts both sides itself. An
    incomplete finalist (D7) takes the fixed non-numerical state and reads no row; its `pairing` is the one the policy requests. Otherwise
    the contrast is the finalist minus the Champion over the whole corpus,
    weighted as `member_completion` weighs it, at the finalist's own level
    (none in observe), drawn from a seed bound to the cohort and to both
    completion prefixes, finalist first. Only the finalist's `critical`
    vetoes; the Champion is the reference (§7).

    `family_version` explicitly selects the record vocabulary and numerical
    kernel; its compatibility default keeps family 13's path unchanged.
    Family 14 uses H2's `final_contrast`. Explicit `stable` reads the stored
    Champion rows and exclusions at R; its second seed prefix is the stored
    content digest. The caller resolves that immutable citation. Every corpus
    case reaches the kernel, including empty sides, and pairing uses the same
    full point set as the contrast. Unstable keeps the fresh completion prefixes.
    Family-14 removal metadata is encoded from the complete sets; callers pass
    their exact ``removal_evidence`` references to budget the persisted payload.

    On the fresh path, raises `CoverageMissing` (from `case_evidence`) when a
    complete member lacks a corpus case, and `GateInvariant` for the remaining invariant
    cases: a case left unobserved, or a statistical bound that is unavailable.
    """

    vocabulary = campaign_vocabulary_for(family_version)
    if stability not in ("stable", "unstable") or (stability == "stable" and family_version != 14):
        raise GateInvariant("stored comparisons require family 14 and declared stability")
    stable = stability == "stable"
    champion = champion_member(cohort, members=members, stability=stability)
    member = finalist_member(cohort, position, members=members, stability=stability)
    if stable:
        if stored_champion is None:
            raise GateInvariant("a stored comparison requires its bound Champion citation")
        champion = str(stored_champion["measurement_id"])
    elif champion is None or stored_champion is not None or excluded:
        raise GateInvariant("a fresh comparison requires a Champion member and no exclusions")
    champion_side = "stored" if stable else "fresh"
    finalist = cohort["finalists"][position]
    done = completed[member]
    base = {
        "round": cohort["round"],
        "attempt_id": finalist["attempt_id"],
        "finalist_measurement_id": member,
        "champion_measurement_id": champion,
    }
    if not done["complete"]:
        return {
            **base,
            "point": None,
            "lower": None,
            "level": None,
            "unavailable": vocabulary.incomplete_reason,
            "pairing": requested_pairing(cohort),
            vocabulary.contrast_key: False,
            "reason": vocabulary.incomplete_reason,
            **(
                dict(exact=None, champion_side=champion_side, removed_pairs=[], removed_cases=[])
                if family_version == 14
                else {}
            ),
        }
    repeats = int(cohort["repeats"])
    if stable:
        for row in (*finalist_rows, *champion_rows):
            if type(row.repeat) is not int or row.repeat < 1:
                raise ValueError("a final row has a positive integer repeat")
        for case_id, repeat in excluded:
            if case_id not in corpus_ids or type(repeat) is not int or repeat < 1:
                raise ValueError("an exclusion names a corpus case and positive integer repeat")
        excluded = frozenset((case_id, repeat) for case_id, repeat in excluded if repeat <= repeats)
        sides = []
        for rows in (finalist_rows, champion_rows):
            samples = score_samples_of(rows, groups, primary, repeats)
            if isinstance(samples, MissingMetric):
                return samples
            sides.append(samples)
        cases = [
            FinalCase(
                case_id,
                tuple(sample for sample in sides[0] if sample.case_id == case_id),
                tuple(sample for sample in sides[1] if sample.case_id == case_id),
            )
            for case_id in corpus_ids
        ]
        assert stored_champion is not None
        champion_prefix = str(stored_champion["prefix_digest"])
    else:
        built = case_evidence(
            finalist_rows,
            champion_rows,
            int(cohort["repeats"]),
            cases=list(corpus_ids),
            groups=groups,
            primary=primary,
            excluded=frozenset(),
        )
        if isinstance(built, MissingMetric):
            return built
        evidence, unobserved = built
        if unobserved is not None:
            raise GateInvariant(
                member_diagnostic(
                    "a complete cohort member leaves a corpus case unobserved",
                    family_version=family_version,
                )
            )
        pairing = pairing_of(evidence)
        if family_version == 14:
            cases = [FinalCase(case.case_id, case.candidate, case.parent) for case in evidence]
        champion_prefix = str(completed[champion]["prefix"])
    seed = derive_seed(
        campaign_seed=campaign_seed,
        binding=str(cohort["digest"]),
        scope="global",
        prefixes=[str(done["prefix"]), champion_prefix],
        look=None,
        purpose="global",
    )
    level = None if cohort["mode"] == "observe" else finalist["level"]
    if family_version == 14:
        pairing = final_pairing(cases, excluded=excluded)
        contrast = final_contrast(
            cases,
            scope_weights(list(corpus_ids), groups),
            excluded=excluded,
            level=level,
            pairing=pairing,
            seed=seed,
            draws=draws,
        )
        estimate14 = contrast.estimate
        verdict14 = decide_global14(
            str(cohort["global"]),
            complete=True,
            critical=bool(done["critical"]),
            point=estimate14.point,
            lower=estimate14.lower,
        )
        return encode_final_removals(
            {
                **base,
                "point": estimate14.point,
                "lower": estimate14.lower,
                "level": estimate14.level,
                "unavailable": estimate14.unavailable,
                "exact": estimate14.exact,
                "pairing": pairing,
                "accepted": verdict14.accepted,
                "reason": verdict14.reason,
                "champion_side": champion_side,
                "removed_pairs": [list(p) for p in contrast.removed_pairs],
                "removed_cases": list(contrast.removed_cases),
            },
            corpus_ids=corpus_ids,
            evidence=removal_evidence,
        )
    estimate = cohort_contrast(
        evidence,
        scope_weights(list(corpus_ids), groups),
        level=level,
        pairing=pairing,
        seed=seed,
        draws=draws,
    )
    if level is not None and estimate.unavailable is not None:
        # F1 hand-off: §6.10 has no unavailable statistical bound. A complete
        # member has R >= 2 scored rows of every case, so this cannot happen
        # over a genuine cohort; `decide_global("bound")` would raise on it.
        raise GateInvariant("a complete statistical member has two observations of every case")
    verdict = decide_global(
        str(cohort["global"]),
        complete=True,
        critical=bool(done["critical"]),
        point=estimate.point,
        lower=estimate.lower,
    )
    return {
        **base,
        "point": estimate.point,
        "lower": estimate.lower,
        "level": estimate.level,
        "unavailable": estimate.unavailable,
        "pairing": pairing,
        "eligible": verdict.eligible,
        "reason": verdict.reason,
    }
