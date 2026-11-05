"""What family 12 adds to the status and result documents (G2 spec §4.6, §4.7, §4.9),
and family 13's caveat items: its method caveats, its campaign-level observe
items, its focus bias and its `provenance_drift` over each finalist's cohort
rows (design §10, D8; PDR-0022). Family 14 (`final=`, H12b plan choices
12-14) reads its own look vocabulary, adds the global `exact_contrast`, the
campaign-level `champion_not_contemporaneous` and the per-finalist
`stored_pairs_excluded`, says `final_*` for the drift sides and checks every
final of a round.

Everything here is derived from durable facts -- the snapshot, each round's
own condition-epoch policy (`round_policy`), the measurement journals for the
completion checks, and the campaign's `instance_attested` records for
component drift across an accepted change -- and nothing is journaled. Nothing here changes a statistic, a gate decision, an outcome or
an exit code, and nothing here reaches the editor's or the Reflector's inputs.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from evolution.tree.affected_cases import (
    BaselineUnavailable,
    SampleProvenance,
    baseline_provenance,
    cited_baseline_prefix,
)
from evolution.tree.campaign_caveats import (
    AFFECTED_CAVEAT_KINDS,
    AFFECTED_CAVEAT_KINDS14,
    CHAMPION_NOT_CONTEMPORANEOUS_KIND,
    COMPONENTS_CHANGED_KIND,
    EXACT_CONTRAST_KIND,
    FOCUS_BIAS_KIND,
    LOOK_CAVEAT_KINDS,
    LOOK_CAVEAT_KINDS14,
    OBSERVE_CAMPAIGN_KINDS,
    PROVENANCE_DRIFT_KIND,
    STORED_PAIRS_EXCLUDED_KIND,
)
from evolution.tree.campaign_epochs import epoch_identity
from evolution.tree.campaign_measurement import MEASUREMENTS_DIR
from evolution.tree.campaign_policy import CampaignPolicyV11, policy_class_for_payload
from evolution.tree.campaign_schema import (
    AttemptState,
    CampaignLifecycle,
    CampaignSnapshot,
    affected_layers,
    campaign_family_for,
    current_members,
    finalist_member,
)
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.component_drift import (
    DriftReference,
    baseline_parent_reference,
    candidate_reference,
    champion_reference,
    checked_selection,
)
from evolution.tree.execution.journal import (
    EVENTS_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    inspect_journal,
)
from evolution.tree.execution.journal_schema import Lifecycle
from evolution.tree.final_report import removed_count
from evolution.tree.storage.append_log import inspect_log
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalFault

AFFECTED_REPORT_KEYS = frozenset(
    {
        "source",
        "mode",
        "fallback_reason",
        "layer_one",
        "layer_two",
        "fresh_parent",
        "alpha_statement",
    }
)


def alpha_statement(fresh_parent: bool, global_layer: bool) -> str | None:
    """Which layer the alpha share covers (design §5.4); `None` means no layer does."""

    if fresh_parent:
        return "layer_one_with_global_filter" if global_layer else "layer_one"
    return "global" if global_layer else None


def round_policy(snapshot: CampaignSnapshot, number: int) -> CampaignPolicyV11:
    """The policy round `number` was judged under: its condition epoch's (ADR-0020).

    The epoch is the one the round's baseline measurement was bound in. Only a
    `rebaseline` moves `policy_digest` (`continue` changes the agent's side
    alone), and a `rebaseline` closes the open round and starts the next one
    from a fresh baseline bound in the new epoch, so every Attempt of a round
    -- and its completion, which a rebaseline never lets serve as the next
    baseline -- ran under the policy of its baseline's epoch. A `continue`
    re-binding the baseline opens an epoch with the same policy. A round with
    no baseline yet has no Attempt; it reads the policy in force. So does a
    round whose baseline is bound but whose measurement is not yet (a run cut
    between `baseline_bound` and `measurement_bound`): the very next record
    binds that measurement in the latest epoch.
    """

    baseline = snapshot.rounds[number].baseline_measurement_id
    if baseline is None or baseline not in snapshot.measurements:
        raw = snapshot.identity["policy"]
    else:
        raw = epoch_identity(snapshot, baseline).to_payload()["policy"]
    return policy_class_for_payload(raw).from_payload(raw)


def round_policies(snapshot: CampaignSnapshot) -> dict[int, CampaignPolicyV11]:
    """`round_policy` of every round of the snapshot."""

    return {number: round_policy(snapshot, number) for number in snapshot.rounds}


def attempt_affected_entry(
    snapshot: CampaignSnapshot,
    attempt: AttemptState,
    policy: CampaignPolicyV11,
    *,
    confidence: bool = False,
) -> dict[str, Any] | None:
    """The §4.9 facts of one Attempt, or `None` before its selection is journaled.

    `policy` is the Attempt's round's own (`round_policy`), never the policy
    in force: a later `rebaseline` must not rewrite an earlier round's claim.
    `confidence` is the caller's family-13 gate: family 13 spends no alpha, so
    `alpha_statement` is `None` there rather than naming a share that does
    not exist (F8 plan choice 3). Family 12 is unchanged (ADR-0024).
    """

    selection = attempt.selection
    if selection is None:
        return None
    scope = snapshot.rounds[attempt.round].gate_scope
    assert scope is not None  # a selection is written after the round's exam
    layers = affected_layers(
        selection,
        snapshot.identity["corpus_cases"],
        scope["cases"],
        global_layer=bool(policy.global_layer),
    )
    return {
        "source": selection["source"],
        "mode": selection["mode"],
        "fallback_reason": selection["fallback_reason"],
        "layer_one": len(layers.layer_one),
        "layer_two": len(layers.layer_two),
        "fresh_parent": attempt.parent_measurement_id is not None,
        "alpha_statement": (
            None
            if confidence
            else alpha_statement(bool(policy.fresh_parent), bool(policy.global_layer))
        ),
    }


def _biased(policy: CampaignPolicyV11, confidence: bool) -> bool:
    """Whether a round's own policy leaves its focus selection biased.

    Family 12 (design §5.4): both switches off. Family 13 (`confidence`, F8
    plan choice 6, maintainer decision E2): `fresh_parent: false`, whatever
    the `global` mode -- the gate's own admission stays biased even when a
    `point` or `bound` cohort filters Promotion. It reads `fresh_parent`
    alone, never the V10 policy's always-false `global_layer`.
    """

    if confidence:
        return not policy.fresh_parent
    return not (policy.fresh_parent or policy.global_layer)


def focus_bias_caveats(
    snapshot: CampaignSnapshot,
    policies: Mapping[int, CampaignPolicyV11],
    *,
    confidence: bool = False,
) -> list[dict[str, Any]]:
    """`focus_selection_bias` per measured Attempt of each round whose own
    policy (`policies`, from `round_policies`) leaves the focus selection
    biased (`_biased`). `confidence` is the caller's family-13 gate."""

    items: list[dict[str, Any]] = []
    for number in sorted(snapshot.rounds):
        if not _biased(policies[number], confidence):
            continue
        attempts = sorted(snapshot.rounds[number].attempts.values(), key=lambda a: a.index)
        items.extend(
            {"kind": FOCUS_BIAS_KIND, "round": number, "attempt_id": attempt.attempt_id}
            for attempt in attempts
            if attempt.measurement_id is not None
        )
    return items


def method_caveats(snapshot: CampaignSnapshot, *, final: bool = False) -> list[dict[str, Any]]:
    """Family 13's method caveats (design §6.5, §10; F8 plan choice 5).

    One `{kind, round, attempt_id}` item per kind in the union of the
    `caveats` of the Attempt's evaluated looks (`AttemptState.looks`), in
    `LOOK_CAVEAT_KINDS` order, Attempts in round and index order. A look a
    `continue` superseded stays decided and counts. An Attempt with no
    evaluated look gets none.

    Family 14 (`final`, H12b plan choice 13) reads `LOOK_CAVEAT_KINDS14`, and
    an Attempt whose global contrast is `exact` has the `exact_contrast` item
    too: one item per Attempt, whether a look, the contrast or both say so.
    """

    kinds = LOOK_CAVEAT_KINDS14 if final else LOOK_CAVEAT_KINDS
    items: list[dict[str, Any]] = []
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        attempts = sorted(state.attempts.values(), key=lambda a: a.index)
        for attempt in attempts:
            found = {str(kind) for look in attempt.looks.values() for kind in look["caveats"]}
            if final:
                contrast = state.global_comparisons.get(attempt.attempt_id)
                if contrast is not None and contrast["exact"] is True:
                    found.add(EXACT_CONTRAST_KIND)
            items.extend(
                {"kind": kind, "round": number, "attempt_id": attempt.attempt_id}
                for kind in kinds
                if kind in found
            )
    return items


def _gate_mode(identity: Mapping[str, Any], name: str = "mode") -> object:
    """An identity payload's `gate.<name>` (`mode` by default); `None` before
    `campaign_created` or when the gate has no such key."""

    policy = identity.get("policy")
    gate = policy.get("gate") if isinstance(policy, Mapping) else None
    return gate.get(name) if isinstance(gate, Mapping) else None


def ran_observe(snapshot: CampaignSnapshot) -> bool:
    """Whether a family-13 campaign ran `observe` under any identity of its own.

    The creation identity and every comparable epoch (`snapshot.epochs`),
    the policy in force (`snapshot.identity`), and what an identity the
    snapshot keeps no copy of (a `rebaseline` between two others) bound: an
    Attempt's comparison or a round's cohort. A predecessor's mode is not
    read: an observe predecessor admits only an observe successor (D10).
    """

    identities = (*snapshot.epochs, snapshot.identity)
    if any(_gate_mode(identity) == "observe" for identity in identities):
        return True
    for state in snapshot.rounds.values():
        if state.cohort is not None and state.cohort["mode"] == "observe":
            return True
        if any(
            attempt.comparison is not None and attempt.comparison["mode"] == "observe"
            for attempt in state.attempts.values()
        ):
            return True
    return False


def observe_campaign_caveats(snapshot: CampaignSnapshot) -> list[dict[str, Any]]:
    """Family 13's campaign-level observe items (PDR-0022; codex F8 round 1).

    `{kind, round: null, attempt_id: null}` for each `OBSERVE_CAMPAIGN_KINDS`
    kind when the campaign ran `observe` (`ran_observe`), independent of any
    evaluated Attempt; `[]` otherwise. They survive a Promotion, a link and
    an export because they come from the campaign's own identities.
    """

    if not ran_observe(snapshot):
        return []
    return [{"kind": kind, "round": None, "attempt_id": None} for kind in OBSERVE_CAMPAIGN_KINDS]


def ran_stable(snapshot: CampaignSnapshot) -> bool:
    """Whether a family-14 campaign compared against a stored Champion (F0 §6.2).

    As `ran_observe` reads the mode: any identity of the run (its creation,
    every comparable epoch and the policy in force) declares `stable`, or a
    round bound a `stable` final.
    """

    identities = (*snapshot.epochs, snapshot.identity)
    if any(_gate_mode(identity, "stability") == "stable" for identity in identities):
        return True
    return any(
        final["stability"] == "stable"
        for state in snapshot.rounds.values()
        for final in state.finals
    )


def final_campaign_caveats(snapshot: CampaignSnapshot) -> list[dict[str, Any]]:
    """Family 14's campaign-level items (H12b plan choice 13; PDR-0028 amendment).

    The observe pair when the campaign ran `observe`, then
    `champion_not_contemporaneous` when it ran `stable` (`ran_stable`), each
    `{kind, round: null, attempt_id: null}`.
    """

    items = observe_campaign_caveats(snapshot)
    if ran_stable(snapshot):
        items.append({"kind": CHAMPION_NOT_CONTEMPORANEOUS_KIND, "round": None, "attempt_id": None})
    return items


def stored_caveats(snapshot: CampaignSnapshot) -> list[dict[str, Any]]:
    """Family 14's `stored_pairs_excluded` (F0 R27, M2; ADR-0032 amendment).

    One `{kind, round, attempt_id, measurement_id, count}` per stored global
    contrast that removed pairs: `measurement_id` is the stored Champion
    measurement and `count` its removed pairs (a list or the closed summary,
    plan choice 5). A fresh contrast removes none. Attempts in round and
    index order.
    """

    items: list[dict[str, Any]] = []
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        for attempt in sorted(state.attempts.values(), key=lambda a: a.index):
            contrast = state.global_comparisons.get(attempt.attempt_id)
            if contrast is None or contrast["champion_side"] != "stored":
                continue
            count = removed_count(contrast["removed_pairs"])
            if count:
                items.append(
                    {
                        "kind": STORED_PAIRS_EXCLUDED_KIND,
                        "round": number,
                        "attempt_id": attempt.attempt_id,
                        "measurement_id": contrast["champion_measurement_id"],
                        "count": count,
                    }
                )
    return items


def affected_caveats(
    snapshot: CampaignSnapshot,
    policies: Mapping[int, CampaignPolicyV11],
    completion: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    confidence: bool = False,
    final: bool = False,
) -> list[dict[str, Any]]:
    """Every caveat of family 12, 13 and 14, in `(round, attempt index, kind)` order.

    A family-13 campaign that ran `observe` leads with its two campaign-level
    items (`observe_campaign_caveats`), which name no round and no Attempt.

    `policies` is `round_policies(snapshot)`; `completion` holds each round's
    already computed items (`read_affected_items`): its closed completion's
    checks, its finalists' cohort `provenance_drift` (family 13) and its
    Attempts' `components_changed_by_condition`. `confidence` is the
    caller's family-13 gate: the focus bias follows `fresh_parent` alone
    and the method caveats join (`method_caveats`). The family-13 items are
    the method caveats, the focus bias, the D8 `provenance_drift` and the
    cohort drift sides.

    `final` marks a family-14 campaign (and implies `confidence`, H12b plan
    choices 12-13): its method items (`method_caveats(final=True)`) and its
    `stored_pairs_excluded` items (`stored_caveats`) sort by
    `AFFECTED_CAVEAT_KINDS14`, and `final_campaign_caveats` lead.
    """

    confidence = confidence or final
    kinds = AFFECTED_CAVEAT_KINDS14 if final else AFFECTED_CAVEAT_KINDS

    index: dict[str | None, float] = {
        attempt.attempt_id: attempt.index
        for state in snapshot.rounds.values()
        for attempt in state.attempts.values()
    }
    # The Champion member's drift item names no Attempt (family 13, F8 plan
    # choice 8): it sorts after every Attempt item of its round.
    index[None] = math.inf
    items = [*focus_bias_caveats(snapshot, policies, confidence=confidence)]
    if confidence:
        items.extend(method_caveats(snapshot, final=final))
    if final:
        items.extend(stored_caveats(snapshot))
    for number in sorted(completion):
        items.extend(dict(item) for item in completion[number])
    ordered = sorted(
        items,
        key=lambda item: (
            item["round"],
            index[item["attempt_id"]],
            kinds.index(item["kind"]),
        ),
    )
    if not confidence:
        return ordered
    leading = final_campaign_caveats(snapshot) if final else observe_campaign_caveats(snapshot)
    return [*leading, *ordered]


# -- the completion checks (G2 spec §4.7) -----------------------------------


def _winner_chain(snapshot: CampaignSnapshot, attempt_id: str) -> frozenset[str]:
    """The winner Attempt's Candidate measurements: the base id and every `@e<k>` re-bind."""

    return frozenset(
        measurement_id
        for measurement_id, entry in snapshot.measurements.items()
        if entry.get("attempt_id") == attempt_id and entry.get("role") == "candidate"
    )


def _from_chain(snapshot: CampaignSnapshot, chain: frozenset[str], source: tuple[str, str]) -> bool:
    """Whether a carried row's `(run_id, measurement_id)` source is this run's winner chain."""

    run_id, measurement_id = source
    return run_id == snapshot.run_id and measurement_id in chain


def winner_sourced_cases(
    snapshot: CampaignSnapshot,
    attempt_id: str,
    imported_sources: Mapping[str, Collection[tuple[str, str]]],
) -> frozenset[str]:
    """Cases whose completion row was carried from the winner's Candidate chain (ADR-0017).

    `imported_sources` maps a case id to the `(source.run_id,
    source.measurement_id)` of its `sample_imported` rows. A case is
    winner-sourced when any of them names this run and a Candidate measurement
    of the winner Attempt -- the base id or an `@e<k>` re-bind, i.e. every
    snapshot measurement entry with the winner's `attempt_id` and `role:
    "candidate"`. A transitively carried row keeps its oldest source, which is
    in that chain; a row the completion itself measured and a re-bound
    completion carried names the completion, so it is first measured.
    """

    chain = _winner_chain(snapshot, attempt_id)
    return frozenset(
        case_id
        for case_id, sources in imported_sources.items()
        if any(_from_chain(snapshot, chain, source) for source in sources)
    )


def completion_row_facts(
    snapshot: CampaignSnapshot,
    attempt_id: str,
    samples: Mapping[str, Sequence[SampleProvenance]],
) -> tuple[frozenset[str], Mapping[str, tuple[tuple[str, ...] | None, ...]]]:
    """`(winner_sourced, executed_provenance)` of a completion's stored rows.

    `samples` is `baseline_provenance` over the completion journal. A row
    counts as executed by the completion -- and so for `provenance_drift` --
    when it ran here (`source` is `None`) or was carried from anywhere but the
    winner's Candidate chain: a `continue` re-binding an unfinished completion
    carries the rows the completion already ran as `sample_imported` rows
    naming the earlier completion, with their provenance. A row that never
    finished carries no provenance and is skipped. A finished row whose
    scoring then failed still counts: it used what it used, and over-reporting
    is the safe direction for a report-only caveat.
    """

    chain = _winner_chain(snapshot, attempt_id)
    sources: dict[str, set[tuple[str, str]]] = {}
    executed: dict[str, list[tuple[str, ...] | None]] = {}
    for case_id, rows in samples.items():
        for sample in rows:
            if sample.source is not None:
                sources.setdefault(case_id, set()).add(sample.source)
                if _from_chain(snapshot, chain, sample.source):
                    continue
            elif not sample.carries_digest:
                continue
            executed.setdefault(case_id, []).append(sample.used)
    return (
        winner_sourced_cases(snapshot, attempt_id, sources),
        MappingProxyType({case_id: tuple(used) for case_id, used in executed.items()}),
    )


def _measured_cases(snapshot: CampaignSnapshot, winner: AttemptState) -> frozenset[str]:
    """The cases the winner's Candidate measurement measured (`case_subset`)."""

    corpus = [str(case["case_id"]) for case in snapshot.identity["corpus_cases"]]
    assert winner.measurement_id is not None
    subset = snapshot.measurements[winner.measurement_id].get("case_subset")
    if subset is None:
        return frozenset(corpus)
    return frozenset(corpus[index] for index in subset)


def _drift_checked(policy: CampaignPolicyV11, attempt: AttemptState) -> bool:
    """Whether `provenance_drift` applies: `reported` provenance and a `changed` list."""

    provenance = policy.gate_provenance
    selection = attempt.selection
    return (
        provenance is not None
        and provenance["kind"] == "reported"
        and selection is not None
        and isinstance(selection["changed"], list)
    )


def provenance_drift_item(
    snapshot: CampaignSnapshot,
    policy: CampaignPolicyV11,
    attempt: AttemptState,
    measurement_id: str,
    executed_provenance: Mapping[str, Sequence[Sequence[str] | None]],
) -> dict[str, Any] | None:
    """The `provenance_drift` item over one measurement's executed rows, or `None`.

    It names, in corpus order, the cases outside the Attempt's measured set
    (`_measured_cases`) whose rows' recorded provenance intersects the
    Attempt's changed paths; `measurement_id` is the measurement those rows
    belong to. Only `reported` provenance with a `changed` list applies.
    Family 12 reads the winner's completion (`completion_caveats`); family 13
    each finalist's current cohort member (`read_cohort_drift_items`, D8).
    `executed_provenance` is `completion_row_facts`' second value. `policy`
    is the round's own (`round_policy`).
    """

    if not _drift_checked(policy, attempt):
        return None
    assert attempt.selection is not None  # `_drift_checked`
    measured = _measured_cases(snapshot, attempt)
    changed = set(attempt.selection["changed"])
    corpus = [str(case["case_id"]) for case in snapshot.identity["corpus_cases"]]
    hits = [
        case_id
        for case_id in corpus
        if case_id not in measured
        and any(
            used is not None and changed.intersection(used)
            for used in executed_provenance.get(case_id, ())
        )
    ]
    if not hits:
        return None
    return {
        "kind": PROVENANCE_DRIFT_KIND,
        "round": attempt.round,
        "attempt_id": attempt.attempt_id,
        "measurement_id": measurement_id,
        "cases": hits,
    }


def _measurement_records(
    run_root: Path, measurement_id: str
) -> tuple[tuple[Envelope, ...], Lifecycle]:
    journal = inspect_journal(Path(run_root) / MEASUREMENTS_DIR / measurement_id)
    records = tuple(journal.records)
    state = Lifecycle()
    try:
        for record in records:
            state.accept(record)
    except ValueError as exc:
        # As `read_measurement_tries`: a prefix S1's lifecycle refuses to
        # reduce is an unreadable measurement journal.
        raise JournalFault("journal_unavailable") from exc
    return records, state


def _target_of(records: Sequence[Envelope]) -> tuple[str, str]:
    """A measurement journal's own `(target.seed_mode, target.dependency_digest)`."""

    target = records[0].payload["identity"]["target"]
    return str(target["seed_mode"]), str(target["dependency_digest"])


def scheduled_repeats(records: Sequence[Envelope]) -> int:
    """A measurement journal's own scheduled repeat count (`FrozenMeasurement._max_repeat`)."""

    schedule = records[0].payload["identity"]["schedule"]
    return max((int(row["repeat"]) for row in schedule), default=0)


def read_cohort_drift_items(
    run_root: Path, snapshot: CampaignSnapshot, siblings: Mapping[str, str]
) -> dict[int, tuple[dict[str, Any], ...]]:
    """Family 13's `provenance_drift`, one item per drifting finalist (D8, plan choice 7).

    Family 13 has no completion step, so the check reads each finalist's
    cohort rows instead (PDR-0022 Amendment (5)): for every finalist of a
    round with a cohort, its current member (`RoundState.cohort_current`,
    an `<id>@e<k>` after a `continue`) is read through `_measurement_records`
    and `baseline_provenance`, and `completion_row_facts` keeps the rows the
    member executed or carried from its own earlier epoch. The item names the
    member and the corpus cases outside the Attempt's measured set (F ∪ C)
    whose rows used a changed path; finalists in `finalists_bound` order. A
    member whose journal is not born yet is skipped, and so is a round whose
    policy (`round_policy`) does not check drift, before any journal is read.
    `{}` below family 13.

    Family 14 (H12b plan choice 14) reads every final of the round
    (`RoundState.finals`, journal order), not only the latest, so each
    earlier `first_accepted` finalist is checked too.
    """

    found: dict[int, tuple[dict[str, Any], ...]] = {}
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        if state.cohort is None or state.cohort_current is None:
            continue
        policy = round_policy(snapshot, number)
        items = []
        bindings = state.finals
        finalists = [
            (binding, position, finalist, current_members(state, final=(binding)))
            for binding in bindings
            for position, finalist in enumerate(binding["finalists"])
        ]
        for binding, position, finalist, members in finalists:
            member = finalist_member(binding, position, members=members)
            attempt = state.attempts[str(finalist["attempt_id"])]
            if not _drift_checked(policy, attempt) or siblings.get(member) != "born":
                continue
            records, _ = _measurement_records(run_root, member)
            if not records:
                continue  # a born journal holds its creation; nothing to read otherwise
            _, executed = completion_row_facts(
                snapshot, attempt.attempt_id, baseline_provenance(records)
            )
            item = provenance_drift_item(snapshot, policy, attempt, member, executed)
            if item is not None:
                items.append(item)
        if items:
            found[number] = tuple(items)
    return found


# -- component drift across an accepted change (G2 spec §4.6, G2b3b) -------------

#: The sides a component-mode Attempt relied on a map for, in the caveat's order.
DRIFT_SIDES = ("candidate", "parent")
#: Family 13 (F7 plan choice 10): a finalist cohort member's side, after its
#: Attempt's own, and the Champion member's, in a round-level item.
FINALIST_MEMBER_SIDE = "cohort_finalist"
CHAMPION_MEMBER_SIDE = "cohort_champion"
#: Family 14 (M4, H12b plan choice 14): "final" where family 13 says "cohort".
FINAL_FINALIST_SIDE = "final_finalist"
FINAL_CHAMPION_SIDE = "final_champion"


def _drifted(
    measurement_ids: Sequence[str],
    attested: Mapping[str, Sequence[Mapping[str, Any]]],
    reference: DriftReference,
) -> bool:
    digests = {
        payload.get("components_digest")
        for measurement_id in measurement_ids
        for payload in attested.get(measurement_id, ())
    }
    return bool(digests - {None, reference.digest})


def drift_caveats(
    snapshot: CampaignSnapshot,
    attested: Mapping[str, Sequence[Mapping[str, Any]]],
    references: Mapping[tuple[str, str], DriftReference],
    champions: Mapping[int, DriftReference] = MappingProxyType({}),
    *,
    final: bool = False,
) -> list[dict[str, Any]]:
    """One `components_changed_by_condition` per Attempt whose sides rendered another map.

    `attested` maps a measurement id to its `instance_attested` payloads;
    `references` maps `(attempt_id, side)` to that side's reference
    (`candidate_reference`, `baseline_parent_reference`). Only an Attempt
    selected in component mode without a fallback is compared, and on each
    side only its own measurements -- the snapshot entries with its
    `attempt_id` and the side's role, `@e<k>` re-binds included. A side is
    named when one of their attested instances declared a map whose digest
    is not the reference's; an instance that declared no map is not compared
    (G2b3b Task 3 ruling). Inside the reference epoch such a map suspends and
    is never attested, so an attested one is a difference an accepted
    condition change may have caused: report-only, it changes nothing.

    Family 13 (F7 plan choice 10): a finalist cohort member is the side
    `cohort_finalist` of its Attempt, after `candidate` and `parent`, under
    the key `(attempt_id, "cohort_finalist")`; `champions` maps a round to
    its Champion member's reference (`champion_reference`), whose drift is
    one item with no Attempt, after the round's Attempt items -- also in a
    round where no Attempt was compared.

    Family 14 (`final`, H12b plan choice 14) names the sides `final_finalist`
    (also its `references` key) and `final_champion`.
    """

    finalist_side = FINAL_FINALIST_SIDE if final else FINALIST_MEMBER_SIDE
    champion_side = FINAL_CHAMPION_SIDE if final else CHAMPION_MEMBER_SIDE
    sides_of: dict[tuple[str, str], list[str]] = {}
    champion_members: dict[int, list[str]] = {}
    member_role = campaign_vocabulary_for(
        14 if getattr(snapshot, "family_version", 13) == 14 else 13
    ).registry_role
    for measurement_id, entry in snapshot.measurements.items():
        role = entry.get("role")
        if role in DRIFT_SIDES:
            sides_of.setdefault((str(entry["attempt_id"]), str(role)), []).append(measurement_id)
        elif role == member_role and entry["cohort_role"] == "finalist":
            key = (str(entry["attempt_id"]), finalist_side)
            sides_of.setdefault(key, []).append(measurement_id)
        elif role == member_role:
            champion_members.setdefault(int(entry["round"]), []).append(measurement_id)
    items: list[dict[str, Any]] = []
    for number in sorted(snapshot.rounds):
        attempts = sorted(snapshot.rounds[number].attempts.values(), key=lambda a: a.index)
        for attempt in attempts:
            if not checked_selection(attempt):
                continue
            sides = []
            for side in (*DRIFT_SIDES, finalist_side):
                reference = references.get((attempt.attempt_id, side))
                if reference is not None and _drifted(
                    sides_of.get((attempt.attempt_id, side), ()), attested, reference
                ):
                    sides.append(side)
            if sides:
                items.append(
                    {
                        "kind": COMPONENTS_CHANGED_KIND,
                        "round": number,
                        "attempt_id": attempt.attempt_id,
                        "sides": sides,
                    }
                )
        champion = champions.get(number)
        if champion is not None and _drifted(champion_members.get(number, ()), attested, champion):
            items.append(
                {
                    "kind": COMPONENTS_CHANGED_KIND,
                    "round": number,
                    "attempt_id": None,
                    "sides": [champion_side],
                }
            )
    return items


def _campaign_records(run_root: Path, snapshot: CampaignSnapshot) -> tuple[Envelope, ...]:
    """The campaign journal's valid prefix, read as `inspect_view` reads it."""

    # Imported here: `campaign_projections` imports this module.
    from evolution.tree.campaign_projections import CAMPAIGN_FILE

    family_version = snapshot.family_version
    log = inspect_log(
        run_root,
        file_name=CAMPAIGN_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=lambda: CampaignLifecycle(selector=None, family_version=family_version),
        envelope_family=campaign_family_for(family_version),
    )
    return tuple(log.records)


def read_drift_items(
    run_root: Path,
    snapshot: CampaignSnapshot,
    *,
    records: Sequence[Envelope] | None = None,
) -> dict[int, tuple[dict[str, Any], ...]]:
    """Each round's `components_changed_by_condition` items, read from the durable journals.

    `{}` below family 12 or when no Attempt selected in component mode
    without a fallback; otherwise `campaign.jsonl` is read once for the
    `instance_attested` payloads and the rounds' `baseline_complete`
    citations -- or `records` is used, when the caller already read that
    valid prefix as `_campaign_records` does (`inspect_view`, #95). The
    Candidate's reference is its inspection's; the Parent's is
    `baseline_parent_reference` over the cited baseline prefix and its
    same-run sources, read through `_measurement_records` -- the one reading
    the executor and the audit share.

    Family 13 (F7 plan choice 10): a finalist cohort member's reference is
    its Attempt's Candidate reference; a round with a cohort reads the
    Champion member's `champion_reference` over the same cited baseline,
    whether or not any of its Attempts was compared.
    """

    checked = [
        attempt
        for state in snapshot.rounds.values()
        for attempt in state.attempts.values()
        if checked_selection(attempt)
    ]
    cohorts = [number for number, state in sorted(snapshot.rounds.items()) if state.cohort]
    if not checked and not cohorts:
        return {}
    run_root = Path(run_root)
    if records is None:
        records = _campaign_records(run_root, snapshot)
    attested: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        if record.event == "instance_attested":
            attested.setdefault(str(record.payload["measurement_id"]), []).append(record.payload)
    parents = {
        str(entry["attempt_id"])
        for entry in snapshot.measurements.values()
        if entry.get("role") == "parent"
    }
    sources: dict[str, tuple[Envelope, ...] | None] = {}

    def source_records(measurement_id: str) -> tuple[Envelope, ...] | None:
        # `_Campaign.same_run_records`: a born measurement of this run, else `None`.
        if measurement_id not in sources:
            journal = run_root / MEASUREMENTS_DIR / measurement_id / EVENTS_FILE
            found = None
            if measurement_id in snapshot.measurements and journal.is_file():
                found = _measurement_records(run_root, measurement_id)[0] or None
            sources[measurement_id] = found
        return sources[measurement_id]

    def read_file(relative: str) -> bytes:
        try:
            return (run_root / relative).read_bytes()
        except OSError as exc:
            raise JournalFault("journal_unavailable") from exc

    def cited_baseline(number: int, baseline_id: str) -> Sequence[Envelope]:
        try:
            return cited_baseline_prefix(records, number, baseline_id, read_file)
        except BaselineUnavailable as exc:
            raise JournalFault("journal_unavailable") from exc

    finalist_side = FINAL_FINALIST_SIDE
    references: dict[tuple[str, str], DriftReference] = {}
    for attempt in checked:
        candidate = candidate_reference(attempt)
        if candidate is not None:
            references[(attempt.attempt_id, "candidate")] = candidate
            # A finalist member renders its Attempt's Candidate (choice 10).
            references[(attempt.attempt_id, finalist_side)] = candidate
        baseline_id = snapshot.rounds[attempt.round].baseline_measurement_id
        if attempt.attempt_id not in parents or baseline_id is None:
            continue  # no fresh Parent measured this Attempt: nothing to compare
        parent = baseline_parent_reference(
            snapshot,
            attempt,
            str(baseline_id),
            cited_baseline(attempt.round, str(baseline_id)),
            source_records,
        )
        if parent is not None:
            references[(attempt.attempt_id, "parent")] = parent
    champions: dict[int, DriftReference] = {}
    for number in cohorts:
        baseline_id = snapshot.rounds[number].baseline_measurement_id
        assert baseline_id is not None  # a cohort follows the round's complete baseline
        champion = champion_reference(
            snapshot,
            number,
            str(baseline_id),
            cited_baseline(number, str(baseline_id)),
            source_records,
        )
        if champion is not None:
            champions[number] = champion
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in drift_caveats(snapshot, attested, references, champions, final=True):
        grouped.setdefault(int(item["round"]), []).append(item)
    return {number: tuple(items) for number, items in sorted(grouped.items())}


def read_affected_items(
    run_root: Path,
    snapshot: CampaignSnapshot,
    siblings: Mapping[str, str],
    *,
    records: Sequence[Envelope] | None = None,
) -> dict[int, tuple[dict[str, Any], ...]]:
    """`read_completion_items` and `read_drift_items`, merged per round.

    The one source the executor's result and `inspect_view` hand to
    `project_status` and `project_result`, so a live result and a read-back
    result report the same caveats. `{}` below family 12. `records`, when
    given, is the campaign journal's valid prefix the caller already read
    (`read_drift_items`). Family 13 has no completion, so its rounds carry
    `read_cohort_drift_items` instead (D8).
    """

    cohort = read_cohort_drift_items(run_root, snapshot, siblings)
    drift = read_drift_items(run_root, snapshot, records=records)
    return {
        number: (*cohort.get(number, ()), *drift.get(number, ()))
        for number in sorted({*cohort, *drift})
    }
