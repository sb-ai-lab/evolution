"""Condition epochs (ADR-0020): which campaign identity a measurement was bound under."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from evolution.tree.campaign_identity import CampaignIdentityV1, measurement_id_for
from evolution.tree.execution.authorizations import Effect, RefusalDifference, comparable_field

#: `campaign_schema.SUSPENDABLE_CAMPAIGN_FAMILY_VERSION`, repeated rather than
#: imported: the lifecycle in `campaign_schema` reads this module's rules, so
#: importing it back would be circular. `campaign_schema` asserts the two agree.
_IN_PLACE_FAMILY_VERSION = 7

EPOCH_SEPARATOR = "@e"

#: The first family whose identity carries `role_visibility` (F0 R17): the
#: digest of the canonical `role_visibility/1` object, journaled as a string,
#: so `identity_differences` names it `identity.role_visibility`.
_ROLE_VISIBILITY_FAMILY_VERSION = 14
ROLE_VISIBILITY_FIELD = "identity.role_visibility"

#: The comparable fields whose change is the agent's side (I2 spec §3.3): the
#: question, the scale and the plan are the same, so measurements may stand.
AGENT_SIDE_FIELDS = frozenset({"target", "environment", "materialization_strategy"})

#: I2 spec §6.5 (ADR-0019 as amended): the threshold-window owner whose window
#: a change of each comparable top-level field restarts. Failures counted under
#: the old provider say nothing about the new one; a change of any other field
#: restarts nothing, and no change restarts the `rollout` window.
WINDOW_OWNER_OF_FIELD: Mapping[str, str] = MappingProxyType(
    {
        "target": "target",
        "materialization_strategy": "target",
        "environment": "environment",
        "scorer": "scorer",
        # M8: the Scorer's comparison condition owns the metric contract.
        "metric_contract": "scorer",
    }
)


def restarted_owners(fields: Iterable[str]) -> frozenset[str]:
    """The owners whose threshold window a change of `fields` restarts (§6.5).

    `fields` are top-level identity fields (`condition_changes[].fields`) or
    the dotted `identity.<field>...` paths an `identity_rebased` journals in
    its `changed_fields`; a path outside `identity` (`producing_version`)
    names no owner.
    """

    owners: set[str] = set()
    for name in fields:
        parts = str(name).split(".")
        top = parts[1] if parts[0] == "identity" and len(parts) > 1 else parts[0]
        owner = WINDOW_OWNER_OF_FIELD.get(top)
        if owner is not None:
            owners.add(owner)
    return frozenset(owners)


def moves_role_visibility(field: str) -> bool:
    """Does this dotted difference or `changed_fields` entry name `role_visibility`?

    The field itself or any dotted descendant, so a structured value could
    never slip past as `identity.role_visibility.<key>`.
    """

    return field == ROLE_VISIBILITY_FIELD or field.startswith(ROLE_VISIBILITY_FIELD + ".")


def visibility_forces_close(family_version: int, differences: Iterable[RefusalDifference]) -> bool:
    """A family-14 `role_visibility` change offers only `close` (PDR-0023, ADR-0029).

    The Reflector's and the Editor's frozen inputs are bound to the role
    projection the declaration derives, so no in-place effect and no rebase
    may carry them under another one -- whatever else changed beside it. This
    narrows PDR-0010's `rebase` for this one field; older families carry no
    `role_visibility` and keep their effects.
    """

    return family_version >= _ROLE_VISIBILITY_FAMILY_VERSION and any(
        moves_role_visibility(difference.field) for difference in differences
    )


def measurement_id_for_epoch(base_id: str, epoch: int) -> str:
    if epoch < 0:
        raise ValueError("epoch is negative")
    return base_id if epoch == 0 else f"{base_id}{EPOCH_SEPARATOR}{epoch}"


def fresh_baseline_id(round_number: int, epoch: int) -> str:
    """`m-r<NN>-champion@e<k>`: the fresh baseline a `rebaseline` binds (I2 spec §5.2).

    The planner (`campaign_view._baseline_measurement`) and the lifecycle
    (`CampaignLifecycle._fresh_baseline_id`) both name it through this helper.
    """

    return measurement_id_for_epoch(f"m-r{round_number:02d}-champion", epoch)


def base_measurement_id(measurement_id: str) -> str:
    return measurement_id.split(EPOCH_SEPARATOR, 1)[0]


def epoch_of(snapshot: Any, measurement_id: str) -> int:
    return int(snapshot.measurements[measurement_id]["epoch"])


def epoch_identity(snapshot: Any, measurement_id: str) -> CampaignIdentityV1:
    payload: Mapping[str, Any] = snapshot.epochs[epoch_of(snapshot, measurement_id)]
    return CampaignIdentityV1.from_payload(dict(payload))


def rounds_begun(snapshot: Any) -> int:
    """The rounds of *this* run whose baseline is bound (I2 spec §3.2).

    An open round -- baseline bound, no `round_closed` -- counts: `rebaseline`
    would close it. A linked run's rounds start at `snapshot.first_round`,
    which may be above `1`; only the rounds from there on are this run's.
    """

    return sum(
        1
        for number, state in snapshot.rounds.items()
        if number >= snapshot.first_round and state.baseline_measurement_id is not None
    )


def family14_next_baseline(
    rounds: Mapping[int, Any],
    *,
    first_round: int,
    rebaseline_from_round: int | None,
    epoch: int,
    round_number: int,
) -> str:
    """The family-14 next-baseline rule without refresh (planner and lifecycle agree on it).

    The first round measures the seed Champion (`measurement_id_for(None)`);
    the round a `rebaseline` starts measures its Champion afresh under the
    latest epoch (`fresh_baseline_id`); a round after a `promoted` or
    `retained` close continues the selection's stored measurement; any other
    round continues the previous round's current baseline.
    """

    if round_number == first_round:
        return measurement_id_for(None)
    if round_number == rebaseline_from_round:
        return fresh_baseline_id(round_number, epoch)
    previous = rounds[round_number - 1]
    selection = previous.selection
    selected = None if selection is None else selection.get("measurement_id")
    if (
        selected is not None
        and previous.closed is not None
        and previous.closed["outcome"] in ("promoted", "retained")
    ):
        return str(selected)
    return str(previous.baseline_measurement_id)


def refresh_count(
    rounds: Mapping[int, Any], *, first_round: int, round_number: int, next_baseline: str
) -> int:
    """Closed rounds in [first_round, round_number) whose base baseline id equals base(next_baseline).

    Base ids never recur after a change (a Promotion, an unstable retention, a
    refresh and a `rebaseline` each bind a fresh id), so the counted rounds are
    the contiguous run ending at `round_number - 1`. A `continue` only adds
    `@e<k>`, which the base id drops. Open rounds do not count.
    """

    base = base_measurement_id(next_baseline)
    return sum(
        1
        for number in range(first_round, round_number)
        if (state := rounds.get(number)) is not None
        and state.closed is not None
        and state.baseline_measurement_id is not None
        and base_measurement_id(state.baseline_measurement_id) == base
    )


@dataclass(frozen=True)
class RefreshDue:
    round: int
    replaced: str  # the measurement the refresh replaces (current id, may carry @e<k>)
    first: int  # first counted round
    last: int  # last counted round == round - 1
    after_rounds: int  # N


def refresh_due(
    rounds: Mapping[int, Any],
    *,
    first_round: int,
    rebaseline_from_round: int | None,
    epoch: int,
    round_number: int,
    after_rounds: int | None,
) -> RefreshDue | None:
    """None when after_rounds is None or 0, or the count is below N; else the due refresh."""

    if not after_rounds:
        return None
    replaced = family14_next_baseline(
        rounds,
        first_round=first_round,
        rebaseline_from_round=rebaseline_from_round,
        epoch=epoch,
        round_number=round_number,
    )
    count = refresh_count(
        rounds, first_round=first_round, round_number=round_number, next_baseline=replaced
    )
    if count < after_rounds:
        return None
    # The counted run equals N exactly when due: N is part of the policy
    # digest, so changing it admits only `rebaseline` or `close` (never
    # `continue`), and both reset the count. `>=` above is defensive.
    return RefreshDue(
        round=round_number,
        replaced=replaced,
        first=round_number - after_rounds,
        last=round_number - 1,
        after_rounds=after_rounds,
    )


def rounds_remaining(snapshot: Any, policy_rounds: int) -> int:
    """The rounds the (new) policy's budget still leaves this run to begin (§3.2).

    `policy_rounds` is the rebuilt policy's `rounds` -- the budget the run
    would continue under -- never an absolute round number. A budget the run
    has already spent or outgrown leaves `0`, not a negative count.
    """

    return max(0, policy_rounds - rounds_begun(snapshot))


def policy_contradiction(
    snapshot: Any, policy_rounds: int, alpha_budget: str | None
) -> tuple[str, str, str] | None:
    """The campaign-wide budget a new policy sets below this run's spend (I2 spec §3.4).

    `(key, spent, value)` for the first contradiction, else `None`. `rounds`
    is compared with the rounds this run has begun (`rounds_begun`). The
    alpha budget is per round (ruling R10), so it is compared with the
    largest single-round `alpha_consumed` total of this run; a linked
    predecessor's rounds were charged under its own policy and do not count.
    The policy has no other campaign-wide budget: `budget` is per rollout and
    the `indeterminate` thresholds count per window. Admission's hard
    `policy_contradicts_prefix` and the lifecycle's backstop at the block's
    `identity_rebased` apply this one rule. A policy without an alpha ledger
    (family 13) passes `None`, and no alpha is compared.
    """

    begun = rounds_begun(snapshot)
    if policy_rounds < begun:
        return ("rounds", str(begun), str(policy_rounds))
    spent = [
        (Fraction(total), str(total))
        for number, total in snapshot.alpha_rounds.items()
        if int(number) >= snapshot.first_round
    ]
    if spent and alpha_budget is not None:
        largest, text = max(spent, key=lambda item: item[0])
        if Fraction(alpha_budget) < largest:
            return ("alpha.budget", text, str(alpha_budget))
    return None


def admissible_effects(
    family_version: int, differences: Sequence[RefusalDifference], rounds_remaining: int
) -> tuple[Effect, ...]:
    """The effects a comparable change admits, default first (I2 spec §3.2-§3.3).

    Before family 7 a comparable change can only close the campaign. An
    `artifact_store` change is data integrity and only closes. A change of the
    agent's side alone (`AGENT_SIDE_FIELDS`) keeps the measurements
    (`continue`); any other comparable field changed the scale or the question,
    so the default is a fresh baseline in the next round (`rebaseline`).
    `rebaseline` spends a round and is offered only while one remains. A set
    of differences with no comparable field is no condition change at all
    (it rebases), so it admits no in-place effect either. A family-14
    `role_visibility` difference closes, whatever else changed
    (`visibility_forces_close`).
    """

    if family_version < _IN_PLACE_FAMILY_VERSION:
        return ("close",)
    if visibility_forces_close(family_version, differences):
        return ("close",)
    changed = {
        difference.field.split(".")[1]
        for difference in differences
        if comparable_field(difference.field, family_version=family_version)
    }
    if not changed or "artifact_store" in changed:
        return ("close",)
    if changed <= AGENT_SIDE_FIELDS:
        return (
            ("continue", "rebaseline", "close") if rounds_remaining > 0 else ("continue", "close")
        )
    return ("rebaseline", "close") if rounds_remaining > 0 else ("close",)
