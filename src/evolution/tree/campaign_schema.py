"""Closed C1 campaign vocabulary: records, lifecycle admission and the pure reducer.

`campaign.jsonl` is the campaign's own append-only log beside the S1 measurement
journals and the S2 ledger (spec §5). This module owns three things and nothing
else:

* the **record vocabulary** — 29 closed payload shapes plus `CAMPAIGN_FAMILY`,
  the `EnvelopeFamily` the append log validates every line with;
* `CampaignLifecycle`, the `LogState` the append log consults, which enforces
  exactly the **L** half of the spec §5 precondition table — everything the
  campaign prefix alone can decide — so an out-of-order record is refused before
  it reaches the file;
* `CampaignSnapshot` / `reduce`, the pure reduction of a prefix.

**L versus E.** The lifecycle never reads a sibling log. An **E** row's fact
lives in a measurement journal or in the S2 ledger; the executor checks it over
the read-only campaign view and the record carries the `evidence` it relied on —
a list of `DurableReference` payloads. The lifecycle only proves that `evidence`
is present and well formed on an E row and absent from an L row.

Three controller rulings amend the brief and spec §5 for this delivery:

1. `CampaignLifecycle(*, selector)` takes the selector explicitly. There is no
   `DEFAULT_SELECTOR` and this module never imports the statistics side: Task 4
   implements `select` against the `Selection` contract defined here. The
   `selection_decided` L-rule is that the payload's `attempt_id`,
   `candidate_digest`, `dependency_digest` and `ranking` equal
   `selector(snapshot_before_this_record, round)`. `selector=None` is the
   explicit "replay without re-deriving the selection" mode `reduce` uses for a
   projection over a prefix the lifecycle already admitted.
2. A 22nd record, `instance_attested` (an E row), is written by the executor
   right after each successful materialization, once `verify_attestation` has
   passed for that instance; promotion admission later re-verifies the winner's
   rows over the durable `identity` — the whole attested
   `TargetIdentity.to_payload()` — which is why the row carries it beside the
   flat digest and boot keys the lifecycle and the projections compare, and why
   an identity that disagrees with those keys is refused. The snapshot keeps the
   rows under `measurements[id]["instances"]`.
3. `attempt_closed` carries `code`: the S2 refusal code when
   `reason == "seal_refused"` and `null` otherwise.

`CAMPAIGN_FAMILY_VERSION = 3` (the reflector round: spec §3.3, ADR-0010) adds
seven records to the vocabulary and one new outcome. A round now opens with
`focus_selected` (§3.3's cluster focus over the baseline's failing cohort,
`reason == "none"` when there is nothing to focus on) and, unless the reason is
`"none"`, runs at most one open `reflection_started` / `reflection_finished` /
`reflection_failed` cycle -- `probe_checked` and `probe_completed` are its
optional, operationless probe traffic -- before `gate_scope_selected` names the
promotion gate's scope and only then may an attempt open. A `"none"` focus
closes the round directly: `clean` when nothing was left unavailable either,
`stalled` (today's evidence-free rule, generalized) otherwise. `RoundState`
keeps the round's `focus`, its `reflections` (each entry's `diagnosis()`-visible
outcome), and its `gate_scope`; `reflection_started` is a second family of
`EnvelopeFamily` start, paired with `reflection_finished`/`reflection_failed`
the way `editor_started` pairs with its own terminals.

Refusals are `storage.envelope.SchemaError` (a `ValueError`) naming the rule and
never the offending value; `AppendLog.append` turns them into refusals. The
private `storage.envelope` validators `_identifier`, `_integer` and `_keys` are
imported rather than re-implemented here, the precedent `artifact_schema.py`
set, so a C1 record is refused by the very checks an S1 record is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from evolution.tree.family15_lifecycle import Family15Lifecycle
    from evolution.tree.family15_schema import (
        Family15AttemptState,
        Family15RoundState,
        Family15SnapshotState,
    )

import copy
import hashlib
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from math import isfinite
from types import MappingProxyType
from typing import Any, Final, TypeVar

from evolution.tree.affected_cases import _PREFIX_LOCATOR, CHANGED_MAX, CHANGED_MAX_BYTES
from evolution.tree.campaign_epochs import (
    _IN_PLACE_FAMILY_VERSION,
    AGENT_SIDE_FIELDS,
    RefreshDue,
    admissible_effects,
    base_measurement_id,
    family14_next_baseline,
    fresh_baseline_id,
    measurement_id_for_epoch,
    moves_role_visibility,
    policy_contradiction,
    refresh_due,
    restarted_owners,
    rounds_remaining,
)
from evolution.tree.campaign_identity import (
    GROUPING_V2_KEYS,
    MODEL_GROUPING_V2,
    PARTITION_PROFILES,
    AdaptationIdentityV1,
    CampaignIdentityV1,
    final_measurement_id_for,
    focus_case_repeats,
    measurement_id_for,
    observe_case_repeats,
    parent_measurement_id_for,
    probe_continues,
    reflection_group_text_chars,
    refresh_measurement_id_for,
)
from evolution.tree.campaign_policy import (
    GLOBAL_MODES,
    CampaignPolicyV11,
    CampaignPolicyV12,
    Owner,
    OwnerWindow,
    crosses,
    decides_at_once,
    decimal_string,
    policy_class_for_payload,
)
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.confidence_statistics import (
    ATTEMPT_DECISIONS,
    BOUND_NOT_REQUESTED_IN_OBSERVE,
    DEGENERATE_INTERVAL,
    INSUFFICIENT_CALIBRATION_REPEATS,
    INSUFFICIENT_REPEATS,
    NOISE_ALLOWANCE_UNAVAILABLE,
    NOT_REQUESTED_IN_OBSERVE,
    OBSERVE_CALIBRATION_DEGENERATE,
    SELECTION_REASONS,
    CalibrationVerdict,
    Decision,
    Estimate,
    FinalistVerdict,
    PublishedValue,
    binding_digest,
    confidence_fraction,
    decide_binding,
    decide_calibration,
    decide_final_observe,
    decide_final_statistical,
    decide_interim,
    decide_selection,
    global_level,
    method_object,
)
from evolution.tree.execution import durable_reference as _durable_reference
from evolution.tree.execution.authorizations import (
    Authorization,
    AuthorizationError,
    baseline_import_instance_digest,
    comparable_changed,
    comparable_field,
    identity_differences,
    identity_instance_digest,
    identity_record,
    validate_issued_at,
)
from evolution.tree.execution.check_modes import ENFORCE_ALL, parse_checks
from evolution.tree.execution.evidence_settings import EvidencePin, EvidenceSettings
from evolution.tree.execution.interaction import TargetIdentity
from evolution.tree.execution.journal import EVENTS_FILE
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.journal_schema import ordinal_limit
from evolution.tree.execution.sample_outcomes import SAMPLE_REASON_REGISTRY_V1
from evolution.tree.execution.wire_v2 import (
    validate_instance_components,
    validate_instance_description,
)
from evolution.tree.final_statistics import (
    ATTEMPT_DECISIONS14,
    EXACT_CONTRAST,
    GLOBAL_REASONS14,
    NOISE_NOT_OBSERVED,
    UNAVAILABLE14,
    Estimate14,
    decide_calibration14,
    decide_final_observe14,
    decide_final_statistical14,
    decide_global14,
    decide_interim14,
    measured_tolerance14,
    would_pass_point14,
)
from evolution.tree.roles.grouping.reflect_grouping import FAMILY15_DESCRIPTION_CHARS
from evolution.tree.roles.role_failures import (
    EDITOR_BUDGET_REASONS,
    FAILURE_CLASSES,
    GROUPER_REASONS,
    LIFECYCLE_ERRORS,
    FailureClass,
    editor_class,
    grouper_class,
    grouper_suspension_for,
    reflection_class,
)
from evolution.tree.storage.envelope import (
    Envelope,
    EnvelopeFamily,
    LifecycleTransition,
    SchemaError,
    _identifier,
    _integer,
    _keys,
    is_identifier,
)
from evolution.tree.target.connection_probe import (
    CLEANUP_FAILED_CAUSE,
    CLEANUP_STATES,
    CONNECTION_DIR,
    DETAIL_CODES,
    ProbeOutcome,
    probe_seed,
)

SEED_VERSION = "v0"
CHAMPION_SIDE = "champion"
ZERO = decimal_string(Fraction(0))

#: The campaign record family this reader writes and admits (spec §4). A
#: journal whose `campaign_created` carries another value — or none, the
#: previous family — is refused on resume as `campaign_family_unsupported`.
#: Continuable terminals (spec 2026-09-17, ADR-0016): family 5 plus sample
#: tries, `pairs_excluded`, two Attempt close reasons, `linked_run` on every
#: terminal and per-run thresholds.
#: Suspendable campaigns (spec 2026-09-22 I1, ADR-0019): family 6 plus the
#: non-terminal `campaign_suspended` record. Only a `/8` policy
#: (`CampaignPolicyV8`) creates it, and a `/8` policy creates nothing else.
assert _IN_PLACE_FAMILY_VERSION == 7
#: Scorer diagnostics (spec 2026-09-24, ADR-0021): family 7 plus the Reflector-
#: only diagnostics channel and the family-expanded exam. Only a `/9` manifest
#: creates it: a `/8` policy together with adaptation identity `/2`, whose
#: `family_membership` the creation record carries in full.
#: Model error grouping (spec 2026-09-25, ADR-0022): family 8 plus the
#: `reflector.grouping.kind = "model"` lifecycle. Only a `/10` manifest creates
#: it: a `/8` policy together with adaptation identity `/3`, which the creation
#: record carries in full as family 8 carries `/2`.
#: Role resilience (spec 2026-09-27, PDR-0019, ADR-0023): family 9 plus the
#: chat/agent-CLI Reflector and Editor's own retry and consecutive-failure
#: suspension lifecycle. Only a `/11` manifest creates it: a `/8` policy
#: together with adaptation identity `/4`, which the creation record carries
#: in full as family 9 carries `/3`.
#: Affected-cases gate (G2 spec §4, PDR-0020, ADR-0024): family 11 plus stored
#: provenance and (slices b2-b4) the affected-case selection, the two-layer
#: gate and completion by import. Only a `/13` manifest creates it: a `/13`
#: policy (`CampaignPolicyV9`) together with adaptation identity `/6`.

#: Promotion confidence (family-13 implementation design §6, PDR-0022,
#: ADR-0027): family 12 without the alpha ledger -- no `look_evaluated`,
#: `alpha_consumed` or `completion_bound` -- plus the comparison and noise
#: calibration records. Only a `/14` policy (`CampaignPolicyV10`) with
#: adaptation identity `/6` creates it. The reader layer admits it from slice
#: F3; no runtime produces or resumes it until slice F8 admits `/14` (D1).


#: The stable family (family-14 implementation design R13; PDR-0028, ADR-0032,
#: ADR-0034, ADR-0029): family 13 plus the records slices H3 to H11 add. Only
#: a `/15` policy (`CampaignPolicyV11`) with adaptation identity `/7` creates
#: it, and its identity carries `metric_contract` and `role_visibility` (R17).
#: The reader admits its creation from slice H1; `evo tree evolve` writes and
#: resumes it from H12c (M1).
FINAL_CAMPAIGN_FAMILY_VERSION: Final = 14

#: Issue #78 (ADR-0019 as amended 2026-10-01, spec §3.7): the released
#: producers of families 7-12 that resumed a threshold suspension without
#: repair tries. A journal whose producing version in force -- the creation
#: record's, or the latest `identity_rebased`'s -- is one of these is read and
#: audited as it was written; from any other version on, an exclusion or a
#: completion with an owed threshold repair is refused. A closed set rather than
#: an ordering, because producing versions are identifiers, not PEP 440 strings.
#: It starts at `0.3.0a16`, not at the I1 release `0.3.0a17`: family 7 first
#: landed while the source still said `__version__ = "0.3.0a16"`, so a checkout
#: between those commits stamps that version on family-7 journals.
THRESHOLD_REPAIR_EXEMPT_VERSIONS = frozenset(f"0.3.0a{n}" for n in range(16, 24))


def threshold_repair_required(version: str) -> bool:
    """Whether the producing version in force owes threshold repairs (spec §3.7)."""

    return version not in THRESHOLD_REPAIR_EXEMPT_VERSIONS


#: #91/#97 spec §5: producing versions written before the Attempt-level
#: suspension fix. Under them nothing of that fix is required or admitted.
ATTEMPT_SUSPENSION_EXEMPT_VERSIONS = frozenset(f"0.3.0a{n}" for n in range(16, 26))


def attempt_suspension_required(version: str) -> bool:
    """Whether the producing version in force owes the #91/#97 rules (spec §5)."""

    return version not in ATTEMPT_SUSPENSION_EXEMPT_VERSIONS


#: a27 spec §3, §5 (#116): producing versions before held Attempts were repaired
#: after any suspension.
HELD_REPAIR_EXEMPT_VERSIONS = frozenset(f"0.3.0a{n}" for n in range(16, 27))
#: a27 spec §4, §5 (#94): producing versions before `target_changed` could cite
#: its refusal prefix.
REFUSAL_PREFIX_EXEMPT_VERSIONS = frozenset(f"0.3.0a{n}" for n in range(16, 27))


#: a28 spec §4 (#109): producing versions before a stage left open by a drift
#: took its unfilled denominator slots into the window it runs in, through
#: `stage_redispatched`. Under them the record is neither required nor admitted.
STAGE_REDISPATCH_EXEMPT_VERSIONS = frozenset(f"0.3.0a{n}" for n in range(16, 28))


def stage_redispatch_required(version: str) -> bool:
    return version not in STAGE_REDISPATCH_EXEMPT_VERSIONS


#: #194 (spec §3.4): producing versions before a Champion measurement's gaps
#: were listed. Under them `champion_incomplete` is never admitted.
CHAMPION_LISTING_EXEMPT_VERSIONS = frozenset(f"0.3.0a{n}" for n in range(16, 35))


def champion_listing_required(version: str) -> bool:
    return version not in CHAMPION_LISTING_EXEMPT_VERSIONS


def held_repair_required(version: str) -> bool:
    return version not in HELD_REPAIR_EXEMPT_VERSIONS


def refusal_prefix_required(version: str) -> bool:
    return version not in REFUSAL_PREFIX_EXEMPT_VERSIONS


def _no_stage_opened(attempt: AttemptState, measurements: Mapping[str, Mapping[str, Any]]) -> bool:
    """Has neither of the Attempt's measurements (Candidate, fresh Parent) opened a stage?"""

    return not any(
        measurements[measurement_id]["stages"]
        for measurement_id in (attempt.measurement_id, attempt.parent_measurement_id)
        if measurement_id is not None
    )


def latest_look_barred(
    attempt: AttemptState, measurements: Mapping[str, Mapping[str, Any]]
) -> bool:
    """Is the Attempt's latest look already barred (`look_not_started`)?

    The latest look is its Candidate measurement's latest stage's, or -- for a
    measurement a `continue` re-bound that has opened no stage yet -- the
    latest look the Attempt decided or barred (`attempt_action` reads it so).
    #91 spec §5: such a bar was decided when it was written,
    under the version in force then, and nothing runs between a bar and the
    `incomplete_sample` closure that follows it, so the closure is not
    re-checked against a threshold. The lifecycle and the audit both read it.
    """

    entry = None if attempt.measurement_id is None else measurements.get(attempt.measurement_id)
    stages = () if entry is None else entry["stages"]
    if stages:
        look: int | None = int(stages[-1]["look"])
    else:
        look = max((*attempt.looks, *attempt.not_started), default=None)
    return look is not None and look in attempt.not_started


def written_before_fix(version: str | None) -> bool:
    """#97 spec §4.4: was a record written under a version exempt from the fix?

    `None` -- no such record -- is not.
    """

    return version is not None and not attempt_suspension_required(version)


def primary_range_required(family_version: int, version: str) -> bool:
    """Whether a scored primary outside [0, 1] is a fault row (#97 spec §4.1, §5).

    The one rule the executor, the read-only view and the audit share: families
    7-12 under a producing version in force that owes the #91/#97 rules.
    """

    return attempt_suspension_required(version)


OWNERS: tuple[str, ...] = ("target", "environment", "scorer", "rollout")
CAPACITY_OWNERS = frozenset({"campaign_journal", "measurement_journal", "artifact_store"})
# Both S2 capacity codes and S1's one: spec §7 makes every typed capacity
# signal the same resumable terminal, so a `capacity_exhausted` record must be
# able to carry the S2 preflight estimate's code as well.
CAPACITY_CODES = frozenset(
    {"storage_capacity_exhausted", "storage_capacity_insufficient", "storage_inodes_insufficient"}
)
EDITOR_STATUSES = frozenset({"edited", "declined"})
EDITOR_ERRORS = frozenset({"reported", "timeout", "cancelled", "unresponsive"})
# Adaptive families can replay a crashed editor invocation from the immutable
# frozen input. Historical family-3 terminals remain closed to this recovery code.
ADAPTIVE_EDITOR_ERRORS = EDITOR_ERRORS | frozenset({"interrupted"})
LOOK_DECISIONS = frozenset({"promote", "reject", "continue"})
LAYERED_LOOK_DECISIONS = frozenset(
    {"reject_focus", "reject_regression", "reject_global", "promote", "continue"}
)
#: G2b3a: a family-12 look's `layers` keys. `layer_two` is `null` when layer 2
#: is empty, `global` is `null` when the policy's global layer is off.
AFFECTED_LAYER_KEYS = frozenset({"layer_one", "layer_two", "global"})

#: Family 13 (design §6.2): the gate modes and the Parent source per comparison.
GATE_MODES = frozenset({"statistical", "observe"})
PARENT_SOURCES = frozenset({"fresh_parent", "baseline"})
#: Family 13 (design §6.7, §6.8): the final cohort's one interleaving and its
#: members' roles.
COHORT_ORDER: Final = "case_repeat_member"
COHORT_ROLES: Final = frozenset({"champion", "finalist"})
#: Family 13 (design §6.5): an estimate's `unavailable` reasons (F1 plan R3).
ESTIMATE_UNAVAILABLE = frozenset(
    {INSUFFICIENT_REPEATS, DEGENERATE_INTERVAL, NOT_REQUESTED_IN_OBSERVE}
)
TOLERANCE_SOURCES = frozenset({"numeric", "measured_noise"})
#: Design §6.5's method caveats; `look_caveats` says which are in force.
INTERVALS_EMPIRICAL = "intervals_empirical"
NOISE_ALLOWANCE_EMPIRICAL = "noise_allowance_empirical"
CAUSAL_PARENT_NOT_CONTEMPORANEOUS = "causal_parent_not_contemporaneous"
OBSERVE_GATE_NOT_ENFORCED = "observe_gate_not_enforced"
UNCALIBRATED_POINT_ESTIMATES = "uncalibrated_point_estimates"
LOOK_CAVEATS = frozenset(
    {
        INTERVALS_EMPIRICAL,
        NOISE_ALLOWANCE_EMPIRICAL,
        CAUSAL_PARENT_NOT_CONTEMPORANEOUS,
        OBSERVE_GATE_NOT_ENFORCED,
        UNCALIBRATED_POINT_ESTIMATES,
        OBSERVE_CALIBRATION_DEGENERATE,
        NOISE_ALLOWANCE_UNAVAILABLE,
    }
)
#: Family 14's look-only registry; public aggregation remains a separate contract.
LOOK_CAVEATS14 = (LOOK_CAVEATS - {OBSERVE_CALIBRATION_DEGENERATE}) | {
    EXACT_CONTRAST,
    NOISE_NOT_OBSERVED,
}
THRESHOLD_KINDS = frozenset({"count", "rate"})
THRESHOLD_BARRIERS = frozenset({"baseline_stage", "round"})
#: Family 13 (design §8): the barrier after each cohort stage. No family-13
#: record carries it: family 7 and later write no `threshold_reached`, so
#: `threshold_reached.barrier` keeps `THRESHOLD_BARRIERS`, and the barrier of a
#: `threshold` suspension is derived by the lifecycle (`_campaign_suspended`),
#: which is the one place this set is read.
CONFIDENCE_THRESHOLD_BARRIERS: Final = THRESHOLD_BARRIERS | {"cohort_stage"}
ROUND_OUTCOMES = frozenset({"promoted", "stalled", "clean"})
CAMPAIGN_OUTCOMES = frozenset({"completed", "stopped", "budget_exhausted", "infrastructure_failed"})
CONTINUATIONS = frozenset({"linked_run", "none"})

# Spec §5's campaign reason registry, verbatim. `seal_refused` and
# `infrastructure_failed` are already members; ruling 3 only makes the first
# one's S2 code explicit in `attempt_closed`. `reflector_unresponsive` and
# `probe_unavailable` are family 3's own campaign-level terminals.
REASONS = frozenset(
    {
        "target_unstable",
        "environment_unstable",
        "scorer_unstable",
        "rollout_time_budget_exhausted",
        "baseline_incomplete",
        "incomplete_sample",
        "critical_failure",
        "lower_bound_positive",
        "upper_bound_nonpositive",
        "inconclusive",
        "editor_declined",
        "editor_failed",
        "editor_no_change",
        "editor_unresponsive",
        "seal_refused",
        "interrupted",
        "scorer_metric_missing",
        "infrastructure_failed",
        "attestation_mismatch",
        "rounds_exhausted",
        "cancelled",
        "reflector_unresponsive",
        "probe_unavailable",
        "grouping_failed",
        # Family 6 (spec §4.3): an Attempt whose baseline excluded every pair at
        # its look, or every pair of its frozen focus, closes without alpha.
        "no_evaluable_pairs",
        "no_evaluable_focus",
        # Family 6 (comparable-links spec §5.2): the terminal an accepted close
        # under a changed comparable condition writes, and nothing else may.
        "condition_changed",
    }
)
# Family 3's reflector round (spec §3.3): the focus/reflection/gate-scope
# prelude a round runs before any attempt may open.
REFLECTION_START_EVENT = "reflection_started"
REFLECTION_TERMINALS = frozenset({"reflection_finished", "reflection_failed"})
GROUPING_START_EVENT = "grouping_started"
GROUPING_TERMINALS = frozenset({"grouping_finished", "grouping_failed"})
GROUPING_FAILURE_CODES = frozenset(
    {
        "descriptor_too_large",
        "malformed_result",
        "secret_detected",
        "oracle_leak",
        "changed_seal",
        "reader_integrity",
        "command_failed",
        "command_timeout",
        "output_too_large",
        "control_collision",
        "interrupted",
    }
)
#: Family 9 (model error grouping spec §10): why a `model` grouping fell back,
#: a closed non-secret code; `grouping_finished.fallback.reason` names one.
GROUPING_FALLBACK_REASONS = frozenset(
    {
        "provider_unavailable",
        "provider_rejected",
        "deadline_exhausted",
        "input_too_large",
        "invalid_output",
    }
)
GROUPING_FALLBACK_KINDS = frozenset({"fields", "singletons"})
GROUPING_FALLBACK_SCOPES = frozenset({"uncovered", "all"})
#: `grouping_finished.calls.reduce`: `model_grouping_runtime.ReduceState`.
GROUPING_REDUCE_STATES = frozenset({"none", "done", "skipped", "invalid"})
#: Family 14 (ADR-0034, F0 R45): the registries the Grouper's grammar adds,
#: beside the shared ones above, which keep validating families 9-13 unchanged.
#: `grouping_started.grouper_kind` names one of the four `/15` Grouper kinds.
GROUPER_KINDS14 = frozenset({"fields", "command", "chat", "agent_cli"})
#: `role_failed` is a `chat` or `agent_cli` Grouper's own failure, with a
#: `reason` and `error_class` from `role_failures.GROUPER_REASONS`.
GROUPING_FAILURE_CODES14 = GROUPING_FAILURE_CODES | {"role_failed"}
#: ADR-0022 as amended: provider failures and `deadline_exhausted` suspend in
#: family 14, so only the answer's content falls back.
GROUPING_FALLBACK_REASONS14 = frozenset({"invalid_output", "input_too_large", "turns_exhausted"})
#: `grouping_finished.calls` of an `agent_cli` Grouper (A§7); `chat` keeps
#: the family-9 `{map_requests, reduce, attempts, repairs}`.
GROUPING_AGENT_CALL_KEYS = frozenset({"agent_calls", "repairs", "turns"})
#: Family 14 (ADR-0034, A§5.1, plan decision 3): what each Grouper kind may
#: journal, which the lifecycle holds to the identity's grouping kind (the
#: validator above is kind-blind and admits the union). `fields` and `command`
#: call no model, so they have no calls shape, no fallback and no `role_failed`.
GROUPER_CALL_KEYS14: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "chat": frozenset({"map_requests", "reduce", "attempts", "repairs"}),
        "agent_cli": GROUPING_AGENT_CALL_KEYS,
    }
)
GROUPER_FALLBACK_REASONS_BY_KIND14: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "chat": frozenset({"invalid_output", "input_too_large"}),
        "agent_cli": frozenset({"invalid_output", "turns_exhausted"}),
    }
)
GROUPER_ROLE_FAILED_REASONS14: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "chat": frozenset(
            {
                "provider_exhausted",
                "provider_rejected",
                "invalid_output",
                "input_too_large",
                "exception",
            }
        ),
        "agent_cli": frozenset(
            {"invalid_output", "turns_exhausted", "agent_error", "timeout", "exception"}
        ),
    }
)
#: A fallback for one of these reasons never kept a model group, so its scope
#: is `all` (A§5.1).
GROUPER_FULL_FALLBACK_REASONS14 = frozenset({"input_too_large", "turns_exhausted"})
REFLECTION_STATUSES = frozenset({"reflected", "declined"})
REFLECTION_ERRORS = frozenset(
    {
        "reported",
        "timeout",
        "cancelled",
        "unresponsive",
        "invalid_result",
        "oracle_leak",
        "interrupted",
        # The Parent already carries the bound kind's control prefix, so no
        # projection could be made for it: one failed reflection, the way the
        # same collision is one failed attempt for either editor.
        "control_collision",
    }
)
ADAPTIVE_REFLECTION_ERRORS = REFLECTION_ERRORS | frozenset({"reader_integrity"})
#: Family 14 (H11, F0 R67 and §11 `reflection_failed`): the Reflector changed
#: its frozen role projection outside its own control subtree, a `content`
#: failure (`role_failures`). Earlier families keep their registries.
# ADR-0039 D5: family 14 also admits the campaign's local first-request refusal.
FINAL_REFLECTION_ERRORS = ADAPTIVE_REFLECTION_ERRORS | frozenset(
    {"projection_changed", "input_too_large"}
)
FOCUS_REASONS = frozenset({"mass", "exhausted", "none"})
PROBE_STATUSES = frozenset({"ready", "unavailable"})
PROBE_ERRORS = frozenset(
    {"rejected", "transport", "budget_exhausted", "instance_died", "context_exhausted"}
)
GATE_SOURCES = frozenset({"claim", "empty-claim", "single-case", "reflection-failed"})
#: Family 9 (model error grouping spec §14, ADR-0022): a gate scope exists only
#: over a valid Editor input, so neither fallback source is admitted.
MODEL_GROUPING_GATE_SOURCES = frozenset({"claim", "single-case"})
FAMILY15_GATE_SOURCES = MODEL_GROUPING_GATE_SOURCES | {"below-minimum"}
#: Family 9: why a round with a settled reflection closed `unplanned` -- the
#: reflection was a valid terminal answer but no valid Editor input.
UNPLANNED_REASONS = frozenset({"reflection_declined", "reflection_empty", "reflection_failed"})


def editor_input_reason(entry: Mapping[str, Any]) -> str | None:
    """Family 9 (spec §14, ADR-0022): whether a settled diagnosis may drive an Editor.

    `None` for a `reflected` diagnosis whose admitted groups claim at least one
    case, else the `UNPLANNED_REASONS` member the round closes with. A group
    over no case binds no hypothesis to the selected error group, so a
    reflection whose groups claim nothing is `reflection_empty` (controller
    ruling, Task 4 review): family 9 never derives an `empty-claim` scope. `entry` is a reduced
    reflection (`RoundState.reflections`), whose `status` is `failed` for a
    `reflection_failed`. The planner, the executor, the reducer and the audit
    all read this one function, so none of them can disagree on the reason.
    """

    if entry["status"] == "failed":
        return "reflection_failed"
    if entry["status"] == "declined":
        return "reflection_declined"
    if not any(group["affected_cases"] for group in entry.get("groups") or ()):
        return "reflection_empty"
    return None


# R2a's own sample reasons are consumed unchanged in `indeterminate_observed`
# and in a `linked_run` continuation.
SAMPLE_REASONS = frozenset(SAMPLE_REASON_REGISTRY_V1)
INDETERMINATE_REASONS = MappingProxyType(
    {
        reason: disposition.owner
        for reason, disposition in SAMPLE_REASON_REGISTRY_V1.items()
        if disposition.kind == "indeterminate"
    }
)

# One closed key set per record; `evidence` is a member exactly on the rows that
# carry it, so a missing or stray key is refused by the family, not by a rule.
#: The `authorization` slot every family-5 consequence record carries (§6): the
#: decision's id, the operator's words and where the decision came from. The
#: decision file itself stays in `<run_dir>/authorizations/`; the journal keeps
#: only what an audit needs to name it.
_AUTHORIZATION_KEYS = frozenset({"id", "reason", "issued_at", "source"})
#: Family 6 (spec §3.5, §4.2): every indeterminate *try* is its own row, and a
#: baseline or winner-completion stage names the pairs it excludes.
#: Family 7 (I1 spec §5.2): one non-terminal record, whose `evidence` is
#: discriminated by `evidence_kind` rather than a durable-reference list.
#: Family 8 (scorer-diagnostics spec §5): the creation record carries the full
#: adaptation identity beside its slot, and `gate_scope_selected` records the
#: claim, the selection and the unevaluable cases before family expansion.
#: Family 9 (model error grouping spec §10, §12, §14): family 8's grammar, plus
#: the `round_closed.reason` every family-9 round close carries -- the
#: `UNPLANNED_REASONS` member of an `unplanned` round, `null` for every other --
#: and `grouping_finished`'s `fallback` and `calls`.
#: Family 10 (role resilience spec, PDR-0019, ADR-0023): family 9's grammar,
#: plus `editor_finished.continued` (spec §6): required (a boolean) in family
#: 10, absent before -- whether the Editor took its one continuation turn
#: after an empty-diff decline.
#: Family 12 (G2 spec §4.3-§4.6): family 10's grammar, plus the Candidate's
#: inspection and the Attempt's affected-case selection, both written after
#: `attempt_sealed` and before the Attempt's measurement is bound, and the
#: fresh Parent's binding, written after the Candidate's, and the winner's
#: completion, written after the round's promoted close.
#: Family 13's refusals among family 12's records (design §6): the alpha
#: ledger and the winner's completion. Refused by absence, so the envelope
#: says "event is not a campaign record" before any rule runs.
#: `selection_decided` keeps its name with the family-13 payload (D5, slice F6).
#: Family 13 (G11, plan choice 3): the `comparison_bound` keys a superseding
#: binding may change; `evidence` is compared apart, and every other key
#: equals the superseded binding's.
_SUPERSEDING_CHANGES = frozenset(
    {"supersedes", "epoch", "candidate_measurement_id", "parent_measurement_id", "evidence"}
)
#: Family 13 (design §6): family 12's grammar without `_CONFIDENCE_REFUSED`;
#: later tasks of slice F3 add the four gate records here.
#: Family 14's shipped grammar delta; future operations stay unregistered.
_FINAL_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "campaign_created": frozenset(
            [
                "adaptation",
                "adaptation_payload",
                "alpha_attempt",
                "family_version",
                "identity",
                "identity_digest",
                "producing_version",
            ]
        ),
        "baseline_bound": frozenset(
            ["candidate_digest", "champion_version", "measurement_id", "round"]
        ),
        "baseline_complete": frozenset(["evidence", "measurement_id", "round"]),
        "champion_complete": frozenset(["evidence", "measurement_id", "round"]),
        "attempt_opened": frozenset(
            ["attempt_id", "attempt_index", "parent_digest", "parent_version", "round"]
        ),
        "editor_started": frozenset(
            ["attempt_id", "brief_digest", "common_input_digest", "input_digest", "operation_id"]
        ),
        "editor_finished": frozenset(
            ["attempt_id", "continued", "note_digest", "operation_id", "status"]
        ),
        "editor_failed": frozenset(["attempt_id", "error_class", "note_digest", "operation_id"]),
        "attempt_sealed": frozenset(
            ["artifact_locator", "attempt_id", "candidate_digest", "dependency_digest", "evidence"]
        ),
        "measurement_bound": frozenset(
            ["attempt_id", "candidate_digest", "identity_digest", "measurement_id", "round"]
        ),
        "stage_opened": frozenset(["look", "measurement_id", "sample_count", "stage"]),
        "indeterminate_observed": frozenset(
            [
                "case_id",
                "count",
                "evidence",
                "measurement_id",
                "owner",
                "reason",
                "retry_follows",
                "retry_ordinal",
                "sample_id",
            ]
        ),
        "threshold_reached": frozenset(
            [
                "barrier",
                "count",
                "denominator",
                "evidence",
                "kind",
                "max_count",
                "max_rate",
                "owner",
                "round",
            ]
        ),
        "look_not_started": frozenset(["attempt_id", "evidence", "look", "missing"]),
        "capacity_exhausted": frozenset(["code", "measurement_id", "owner"]),
        "attempt_closed": frozenset(["attempt_id", "code", "look", "outcome", "reason"]),
        "selection_decided": frozenset(
            [
                "attempt_id",
                "candidate_digest",
                "dependency_digest",
                "evidence",
                "global",
                "measurement_id",
                "ranking",
                "reason",
                "round",
            ]
        ),
        "round_closed": frozenset(
            [
                "attempts_opened",
                "champion_digest",
                "champion_version",
                "coverage",
                "evidence",
                "outcome",
                "reason",
                "round",
            ]
        ),
        "campaign_finished": frozenset(["continuation", "evidence", "outcome", "reason"]),
        "instance_attested": frozenset(
            [
                "boot_id",
                "dependency_digest",
                "evidence",
                "identity",
                "instance_id",
                "loaded_candidate_digest",
                "measurement_id",
            ]
        ),
        "focus_selected": frozenset(
            [
                "clusters",
                "evidence",
                "focus_fingerprint",
                "policy_digest",
                "reason",
                "round",
                "unavailable",
            ]
        ),
        "grouping_started": frozenset(
            [
                "baseline_binding",
                "grouper_kind",
                "grouping_id",
                "policy_digest",
                "registry_digest",
                "round",
            ]
        ),
        "grouping_finished": frozenset(
            ["calls", "fallback", "grouping_id", "reads", "result", "result_digest", "round"]
        ),
        "grouping_failed": frozenset(["code", "grouping_id", "reads", "round"]),
        "focus_portion_selected": frozenset(
            [
                "available",
                "baseline_binding",
                "baseline_prefix_digest",
                "case_ids",
                "cohort_ids",
                "cycle",
                "evidence",
                "failing",
                "grouping_digest",
                "neighborhood_id",
                "policy_digest",
                "reason",
                "registry_digest",
                "round",
                "unavailable",
            ]
        ),
        "attempt_input_frozen": frozenset(
            [
                "attempt_id",
                "common_input_digest",
                "feedback",
                "feedback_mode",
                "input_digest",
                "round",
                "source_refs",
            ]
        ),
        "probe_checked": frozenset(["reflection_id", "round", "status"]),
        "reflection_started": frozenset(["input_digest", "operation_id", "reflection_id", "round"]),
        "probe_completed": frozenset(
            ["error", "method", "path", "reflection_id", "round", "seq", "status"]
        ),
        "reflection_finished": frozenset(
            [
                "groups",
                "note_digest",
                "operation_id",
                "out_of_scope_paths",
                "probes_used",
                "reads",
                "reflection_id",
                "round",
                "status",
            ]
        ),
        "reflection_failed": frozenset(
            ["error_class", "note_digest", "operation_id", "reads", "reflection_id", "round"]
        ),
        "gate_scope_selected": frozenset(
            [
                "cases",
                "claimed_cases",
                "evidence",
                "round",
                "selected_cases",
                "source",
                "unevaluable_cases",
            ]
        ),
        "identity_rebased": frozenset(
            [
                "adaptation",
                "adaptation_payload",
                "authorization",
                "changed_fields",
                "identity",
                "identity_digest",
                "instance_digest",
                "previous_digest",
                "producing_version",
            ]
        ),
        "authorization_applied": frozenset(["authorization", "code", "instance_digest", "path"]),
        "pairs_excluded": frozenset(
            [
                "by_owner",
                "count",
                "evidence",
                "measurement_id",
                "pairs",
                "pairs_digest",
                "round",
                "stage",
                "truncated",
            ]
        ),
        "campaign_suspended": frozenset(["evidence", "owner", "reason"]),
        "repairs_dispatched": frozenset(["count", "evidence", "measurement_id"]),
        "stage_redispatched": frozenset(["count", "evidence", "measurement_id", "stage"]),
        "measurement_rebound": frozenset(
            [
                "epoch",
                "from_measurement_id",
                "identity_digest",
                "imported",
                "measurement_id",
                "pairs_digest",
                "released",
                "round",
            ]
        ),
        "candidate_inspected": frozenset(["attempt", "components", "inspection", "round"]),
        "parent_measurement_bound": frozenset(
            ["attempt_id", "candidate_digest", "identity_digest", "measurement_id", "round"]
        ),
        "affected_cases_selected": frozenset(
            [
                "attempt",
                "cases",
                "changed",
                "fallback_reason",
                "mode",
                "round",
                "source",
                "unknown_cases",
            ]
        ),
        "noise_calibration_bound": frozenset(
            [
                "attempt_id",
                "comparison",
                "method",
                "prefix",
                "round",
                "sizes",
                "source",
                "source_measurement_id",
                "supersedes",
            ]
        ),
        "noise_calibration_completed": frozenset(
            [
                "allowance",
                "attempt_id",
                "binding",
                "degenerate",
                "prefix",
                "result_digest",
                "round",
                "unavailable",
            ]
        ),
        "comparison_bound": frozenset(
            [
                "attempt_id",
                "baseline_measurement_id",
                "candidate_measurement_id",
                "causal",
                "confidence",
                "epoch",
                "evidence",
                "exclusions",
                "focus",
                "looks",
                "margin",
                "method",
                "mode",
                "parent_measurement_id",
                "parent_source",
                "repeats",
                "retained",
                "round",
                "supersedes",
                "tolerance",
            ]
        ),
        "comparison_look_evaluated": frozenset(
            [
                "attempt_id",
                "causal",
                "caveats",
                "comparison",
                "decision",
                "evidence",
                "final",
                "focus",
                "look",
                "overall",
                "prefixes",
                "reason",
                "round",
                "tolerance",
                "would_pass_point",
            ]
        ),
        "finalists_bound": frozenset(
            [
                "champion",
                "epoch",
                "evidence",
                "finalists",
                "global",
                "looks",
                "members",
                "mode",
                "order",
                "repeats",
                "round",
                "seed_mode",
            ]
        ),
        "global_comparison_completed": frozenset(
            [
                "accepted",
                "attempt_id",
                "champion_measurement_id",
                "champion_side",
                "evidence",
                "exact",
                "finalist_measurement_id",
                "level",
                "lower",
                "pairing",
                "point",
                "reason",
                "removed_cases",
                "removed_pairs",
                "round",
                "unavailable",
            ]
        ),
        "final_measurement_bound": frozenset(
            [
                "attempt_id",
                "candidate_digest",
                "champion_side",
                "identity_digest",
                "measurement_id",
                "role",
                "round",
                "round_stop",
                "stability",
            ]
        ),
        "final_measurement_completed": frozenset(
            [
                "complete",
                "critical",
                "evidence",
                "measurement_id",
                "missing",
                "point",
                "prefix",
                "round",
            ]
        ),
        "baseline_refresh_bound": frozenset(
            [
                "baseline_refresh_after_rounds",
                "counted_rounds",
                "evidence",
                "measurement_id",
                "replaced",
                "round",
            ]
        ),
        "baseline_refresh_completed": frozenset(["evidence", "measurement_id", "round"]),
        "connection_preflight_started": frozenset(
            [
                "binding_digest",
                "candidate_digest",
                "case_id",
                "case_index",
                "dependency_digest",
                "environment",
                "epoch",
                "invocation_id",
                "metric_contract",
                "operation_id",
                "primary_metric",
                "probe_seed",
                "scorer",
                "target",
            ]
        ),
        "connection_preflight_finished": frozenset(
            ["cleanup", "evidence", "status", "trace_digest"]
        ),
        "connection_preflight_failed": frozenset(
            [
                "cause_code",
                "cleanup",
                "detail_code",
                "evidence",
                "manifest_path",
                "observed_path",
                "status",
                "trace_digest",
            ]
        ),
        "connection_preflight_interrupted": frozenset(
            ["cleanup", "evidence", "reason", "trace_digest"]
        ),
    }
)
_REFRESH_EVENTS = frozenset({"baseline_refresh_bound", "baseline_refresh_completed"})
#: H10 (F0 §11, choice 7): the connection preflight's start and its three
#: terminals. Exactly one terminal closes every start (C§4.4).
CONNECTION_START_EVENT: Final = "connection_preflight_started"
CONNECTION_FINISHED_EVENT: Final = "connection_preflight_finished"
CONNECTION_FAILED_EVENT: Final = "connection_preflight_failed"
CONNECTION_INTERRUPTED_EVENT: Final = "connection_preflight_interrupted"
CONNECTION_TERMINALS = frozenset(
    {CONNECTION_FINISHED_EVENT, CONNECTION_FAILED_EVENT, CONNECTION_INTERRUPTED_EVENT}
)
#: The closed `connection_preflight_interrupted.reason` (C§4.4).
CONNECTION_INTERRUPTION_REASONS = frozenset({"cancelled", "interrupted"})
#: The wire's 31-bit seed range the probe seed fits (choice 6).
_MAX_PROBE_SEED = 0x7FFFFFFF
_FINAL_BOUNDED_RECORDS = frozenset(
    {
        CONNECTION_START_EVENT,
        *CONNECTION_TERMINALS,
        "baseline_refresh_bound",
        "baseline_refresh_completed",
        campaign_vocabulary_for(14).measurement_bound,
        campaign_vocabulary_for(14).measurement_completed,
        "global_comparison_completed",
    }
)

#: The only *optional* payload keys in the whole registry, family 6 only
#: (PDR-0014). `_KEYS` is a closed set per record, so a key added to one would
#: refuse every journal written before it -- including a resume of a run this
#: delivery did not start. `description` is therefore admitted rather than
#: required: a family-6 campaign written before `describe()` existed validates
#: unchanged, and families 3, 4 and 5 refuse the key as they always have.
#: Family 10 (role resilience spec §4.1-§4.2, ADR-0023): family 6's optional
#: keys, plus `reflection_failed.reason` -- required on a settled failure and
#: refused on a lifecycle terminal (`_validate_reflection_reason`) -- and
#: `round_closed.visitation`, present exactly on a skip; and (spec §5.3)
#: `editor_failed.reason` under the same rule (`_validate_editor_reason`).
#: Family 12 (G2 spec §4): family 10's optional keys, plus
#: `instance_attested.components_digest` (validated by the lifecycle, G2b1).
#: Family 14 (H8, F0 R45, ADR-0034): family 12's optional keys, plus
#: `grouping_failed.reason` and `.error_class`, present exactly on code
#: `role_failed` (`_validate_grouper_failure`). The pinned family-13 table above
#: is not edited.
_FINAL_OPTIONAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "candidate_inspected": frozenset(["components_withheld"]),
        "instance_attested": frozenset(["components_digest", "description"]),
        "reflection_failed": frozenset(["reason"]),
        "editor_failed": frozenset(["reason"]),
        "round_closed": frozenset(["visitation"]),
        "grouping_failed": frozenset(["error_class", "reason"]),
    }
)
#: Family 6's discriminated `authorization_applied` (comparable-links spec §7):
#: `effect` absent or `hygiene` is today's run-directory record, whose `effect`
#: key the producer never writes; `close` and `terminal` carry no `path` but the
#: full decision and both identity triples the instance digest was computed
#: over, so an audit proves the consequence matched its refusal without the
#: decision file. Family 5 keeps `_LAYERED_KEYS` and so refuses `effect`.
_HYGIENE_KEYS = frozenset({"code", "path", "instance_digest", "authorization"})
_EFFECT_KEYS = frozenset(
    {"effect", "code", "instance_digest", "authorization", "journaled", "observed"}
)
#: Baseline inheritance §7: `fresh_baseline` carries the full decision and the
#: declared `pairs_digest` its instance digest was computed over.
_FRESH_BASELINE_KEYS = frozenset(
    {"effect", "code", "instance_digest", "authorization", "pairs_digest"}
)
_TRIPLE_KEYS = frozenset({"identity_digest", "producing_version", "adaptation_digest"})
#: The listed prefix of a `pairs_excluded` record (spec §4.2).
MAX_EXCLUDED_PAIRS = 256
_ADAPTIVE_ROUND_OUTCOMES = ROUND_OUTCOMES | {"unscored"}
#: Family 6 (spec §4.3, ADR-0016 as amended): the round whose every failing
#: cohort was empty once the excluded baseline pairs left it. Only unmeasurable
#: samples failed, so the round opens no Attempt and closes on its own outcome
#: rather than focusing on a case no Attempt could be judged on. It is not
#: `clean` (something did fail) and not `stalled` (no Attempt was spent).
_CONTINUABLE_ROUND_OUTCOMES = _ADAPTIVE_ROUND_OUTCOMES | {"unmeasurable"}
#: Family 7 (I2 spec §3.2, §5.5): the round a `rebaseline` closes without a
#: selection, as the last record of its block.
_SUSPENDABLE_ROUND_OUTCOMES = _CONTINUABLE_ROUND_OUTCOMES | {"condition_changed"}
#: Family 9 (spec §14, ADR-0022): the round whose settled reflection is no
#: valid Editor input. It selects no gate scope, opens no Attempt, measures no
#: Candidate and spends no alpha.
_MODEL_GROUPING_ROUND_OUTCOMES = _SUSPENDABLE_ROUND_OUTCOMES | {"unplanned"}
#: Family 13 (design §6.12): a cohort that retains the Champion closes its
#: round `retained`; the Champion's fresh cohort measurement is the next
#: baseline.
_CONFIDENCE_ROUND_OUTCOMES = _MODEL_GROUPING_ROUND_OUTCOMES | {"retained"}
#: I2 spec §5.4: the effects of an in-place condition change, family 7 only.
IN_PLACE_EFFECTS = frozenset({"continue", "rebaseline"})
# Historical family 3 remains exact. The adaptive-family vocabulary stays
# separate so each reader admits only its own record shape.
# Every E row of spec §5 plus ruling 2's `instance_attested`: `evidence` is a
# non-empty list of durable references and is never null.
E_RECORDS = frozenset(
    {
        "baseline_complete",
        "champion_complete",
        "attempt_sealed",
        "indeterminate_observed",
        "look_not_started",
        "selection_decided",
        "instance_attested",
        # Amendment 21: the barrier a threshold is written at is a fact about
        # completed *stages*, which live in the sibling journals, so the record
        # carries their prefixes and the audit re-checks them.
        "threshold_reached",
        # Family 3: the round's focus and the promotion gate's scope are both
        # facts the executor derives over sibling evidence (the baseline
        # measurement, the reflection's own diagnosis), never bare L rows.
        "focus_selected",
        "gate_scope_selected",
    }
)
ADAPTIVE_E_RECORDS = E_RECORDS | {"focus_portion_selected"}
# `pairs_excluded` cites the measurement prefix its set is recomputed from, and
# `repairs_dispatched` (family 7, #78) the prefix its owed set is. The key
# tables keep the latter out of families <= 6.
# `stage_redispatched` (#109) cites the prefix its slot count is recomputed from.
CONTINUABLE_E_RECORDS = ADAPTIVE_E_RECORDS | {
    "pairs_excluded",
    "repairs_dispatched",
    "stage_redispatched",
    # `comparison_bound` (family 13) cites its Attempt's selection and the
    # baseline's exclusions, and `comparison_look_evaluated` (family 13) cites
    # the Candidate and Parent-side prefixes it read; the key tables keep both
    # out of every earlier family.
    "comparison_bound",
    "comparison_look_evaluated",
    # `finalists_bound` (family 13) cites each finalist's `attempt_closed`.
    "finalists_bound",
    # `cohort_measurement_completed` (family 13) cites its member's attestations.
    # `global_comparison_completed` (family 13) cites both members' completions.
    "global_comparison_completed",
}
# Two rows are E only in one of their states (`round_closed(promoted)` and the
# proven `campaign_finished` states): the key is present and may be null.
CONDITIONAL_E_RECORDS = frozenset({"round_closed", "campaign_finished"})
# `probe_checked` and `probe_completed` are family 3's own repeatable,
# non-transactional probe traffic (`capacity_exhausted`'s own precedent):
# no operation id pairs them to a start or a terminal.
NO_OPERATION_EVENTS = frozenset(
    {
        "campaign_created",
        "capacity_exhausted",
        "probe_checked",
        "probe_completed",
        # An authorization's consequence is a run-level fact, not a step of any
        # paired operation: neither record has a start or a terminal.
        "identity_rebased",
        "authorization_applied",
        # Family 7: a suspension is a run-level fact written at a barrier, no
        # step of a paired operation.
        "campaign_suspended",
    }
)
#: Family 7 (I2 spec §5.4): `measurement_rebound` belongs to the consequence
#: block of an accepted condition change, which no paired operation owns. Kept
#: out of `NO_OPERATION_EVENTS` so families 3-6 refuse the event exactly as
#: they refuse any unknown one.
SUSPENDABLE_NO_OPERATION_EVENTS = NO_OPERATION_EVENTS | {"measurement_rebound"}
#: The two records an accepted refusal writes (PDR-0010, amended 2026-09-17).
#: Named once, because `CampaignLifecycle.prepare` has to say the same thing of
#: both at every barrier of an unfinished campaign: a rebased identity and an
#: admitted run-directory entry are consequences of a decision, never work the
#: barrier exists to stop. The one place they are refused is after
#: `campaign_finished`, which takes no authorization at all.
CONSEQUENCE_EVENTS = frozenset({"identity_rebased", "authorization_applied"})
START_EVENT = "editor_started"
#: The `(outcome, reason)` of the two stops whose barrier writes a running
#: role `cancelled` (ADR-0008's cancellation clause; spec §5.3, ADR-0023).
CANCELLATION_TERMINALS = frozenset({("stopped", "cancelled"), ("stopped", "condition_changed")})
#: Records that open work. None of them is written by the terminal barrier or
#: its drain after a stop -- attempts take no new step, the drive loop decides
#: no round, running stages only finish -- and a resumed invocation over a
#: family-10 `cancelled` role terminal owes the stop's `campaign_finished`
#: before any work (ADR-0023 amended 2026-09-28), so
#: one of these after such a terminal is refused.
WORK_OPENING_EVENTS = frozenset(
    {
        "baseline_bound",
        "attempt_opened",
        "editor_started",
        "measurement_bound",
        "parent_measurement_bound",
        "focus_selected",
        "grouping_started",
        "focus_portion_selected",
        "probe_checked",
        "reflection_started",
        "round_closed",
        "stage_opened",
        "repairs_dispatched",
        "stage_redispatched",
        "comparison_bound",
        "noise_calibration_bound",
        "finalists_bound",
        "cohort_measurement_bound",
        "baseline_refresh_bound",
    }
)
EDITOR_TERMINALS = frozenset({"editor_finished", "editor_failed"})

_DECIMAL = re.compile(r"[0-9]+(\.[0-9]{1,12})?\Z")
#: One `changed_fields` entry: a dotted path into the identity record, at most
#: three segments deep (`identity.<owner>.<field>`), never a value.
_DOTTED = re.compile(r"[a-z_]+(\.[a-z_]+){0,2}\Z")

_ATTEMPT_OUTCOMES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "accepted": frozenset({"lower_bound_positive"}),
        "rejected": frozenset({"upper_bound_nonpositive", "inconclusive", "critical_failure"}),
        "insufficient_evidence": frozenset(
            {"incomplete_sample", "scorer_metric_missing", "infrastructure_failed"}
        ),
        "abandoned": frozenset(
            {
                "editor_declined",
                "editor_failed",
                "editor_no_change",
                "editor_unresponsive",
                "seal_refused",
                "interrupted",
                "cancelled",
            }
        ),
    }
)
_LAYERED_ATTEMPT_OUTCOMES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        **_ATTEMPT_OUTCOMES,
        "rejected": frozenset(
            {
                "reject_focus",
                "reject_regression",
                "reject_global",
                "inconclusive",
                "critical_failure",
            }
        ),
    }
)
# The pure feedback projector consumes already admitted AttemptState facts from
# either supported adaptive family.  Its union is deliberately separate from
# both envelope maps: family 3/4 records still validate only against
# `_ATTEMPT_OUTCOMES`, while family 5 records validate only against
# `_LAYERED_ATTEMPT_OUTCOMES`.
_FEEDBACK_ATTEMPT_OUTCOMES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        outcome: reasons | _LAYERED_ATTEMPT_OUTCOMES[outcome]
        for outcome, reasons in _ATTEMPT_OUTCOMES.items()
    }
)
# Family 6 adds the two exclusion closes to `insufficient_evidence` (spec §4.3).
_CONTINUABLE_ATTEMPT_OUTCOMES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        **_FEEDBACK_ATTEMPT_OUTCOMES,
        "insufficient_evidence": _FEEDBACK_ATTEMPT_OUTCOMES["insufficient_evidence"]
        | {"no_evaluable_pairs", "no_evaluable_focus"},
    }
)
# Family 7 (I2 spec §3.2, §5.5): a `rebaseline` block abandons every Attempt
# of the round it closes that has no outcome yet.
# Family 12 (G2 spec §4.4): an Attempt whose affected cases miss the exam
# closes before any measurement, with no alpha charged.
# Family 12's frozen feedback (G2b3a): the Attempt closes an editor can be fed
# back about. `condition_changed` is not one -- a `rebaseline` abandons every
# open Attempt of the round it closes, so no later Attempt of that round
# freezes its input -- while `no_exam_impact` closes before one does.


def _decision_reasons(decision: str) -> frozenset[str]:
    return frozenset(str(reason) for reason in ATTEMPT_DECISIONS[decision] if reason is not None)


#: Family 13 (design §6.6, G7): the gate's closes replace family 12's look
#: closes; every other family-12 close keeps its meaning.
# Family 14 reads H2's closed Candidate-gate vocabulary; family 13 keeps its own.
_FINAL_ATTEMPT_DECISIONS: Mapping[str, frozenset[str | None]] = ATTEMPT_DECISIONS14
_FINAL_ATTEMPT_OUTCOMES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "finalist": frozenset(["gate_passed", "observe_finalist"]),
        "rejected": frozenset(["causal_below_tolerance", "critical_failure", "focus_below_margin"]),
        "insufficient_evidence": frozenset(
            [
                "incomplete_sample",
                "infrastructure_failed",
                "insufficient_calibration",
                "insufficient_repeats",
                "no_evaluable_focus",
                "no_evaluable_pairs",
                "scorer_metric_missing",
            ]
        ),
        "abandoned": frozenset(
            [
                "cancelled",
                "condition_changed",
                "editor_declined",
                "editor_failed",
                "editor_no_change",
                "editor_unresponsive",
                "interrupted",
                "no_exam_impact",
                "seal_refused",
            ]
        ),
    }
)

#: Family 13's frozen feedback (G7), without `condition_changed`, as family 12's.
_FINAL_FEEDBACK_OUTCOMES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "finalist": frozenset(
            [
                "critical_failure",
                "final_incomplete",
                "gate_passed",
                "global_bound_not_positive",
                "global_point_not_improved",
                "observe_finalist",
            ]
        ),
        "rejected": frozenset(["causal_below_tolerance", "critical_failure", "focus_below_margin"]),
        "insufficient_evidence": frozenset(
            [
                "incomplete_sample",
                "infrastructure_failed",
                "insufficient_calibration",
                "insufficient_repeats",
                "no_evaluable_focus",
                "no_evaluable_pairs",
                "scorer_metric_missing",
            ]
        ),
        "abandoned": frozenset(
            [
                "cancelled",
                "changed_file_budget",
                "editor_declined",
                "editor_failed",
                "editor_no_change",
                "editor_unresponsive",
                "interrupted",
                "no_exam_impact",
                "seal_refused",
                "written_byte_budget",
            ]
        ),
    }
)
#: Design §6.6: the `(outcome, reason)` a deciding look gives; the close names that look.
_LOOK_DECIDED_CLOSES = frozenset(
    (decision, reason)
    for decision in ("accepted", "rejected", "insufficient_evidence")
    for reason in _decision_reasons(decision)
    if reason not in ("insufficient_calibration", "no_evaluable_focus", "no_evaluable_pairs")
)
#: The layer a rejection names, families 5-12 (`reject_*`) and 13 (G7); the
#: names are disjoint, so one map serves every family's close and feedback.
REJECTION_LAYERS: Mapping[str, str] = MappingProxyType(
    {
        "reject_focus": "focus",
        "reject_regression": "rest",
        "reject_global": "global",
        "focus_below_margin": "focus",
        "focus_upper_nonpositive": "focus",
        "focus_lower_not_positive": "focus",
        "causal_below_tolerance": "causal",
    }
)


def feedback_outcomes(family_version: int) -> Mapping[str, frozenset[str]]:
    """The close vocabulary an Attempt's frozen feedback reads in a producing family."""

    if family_version == 15:
        from evolution.tree.family15_schema import REASONS

        return MappingProxyType(
            {
                "finalist": frozenset(REASONS),
                "rejected": frozenset((REASONS - {"passed"}) | {"structurally_unattainable"}),
                "abandoned": _FINAL_FEEDBACK_OUTCOMES["abandoned"],
                "insufficient_evidence": _FINAL_FEEDBACK_OUTCOMES["insufficient_evidence"]
                - REASONS,
            }
        )
    return _need(
        _FINAL_FEEDBACK_OUTCOMES if family_version == 14 else None,
        "campaign family is not supported",
    )


#: Family 12 (G2 spec §4.4): why an `affected_cases_selected` lists every case.
#: `none` is the `provenance.kind: none` reading; `null` in the record is no
#: fallback.
FALLBACK_REASONS = frozenset(
    {
        "none",
        "components_unstable",
        "components_unavailable",
        "components_withheld",
        "inspection_failed",
        "component_added_or_removed",
        "unaccounted_path",
        "file_added_or_removed",
        "unlabelled_path",
        "changes_unrecorded",
    }
)
#: Family 12: the `provenance.kind` an `affected_cases_selected.source` names,
#: and the modes a `reported` selection reads its rows under.
SELECTION_SOURCES = frozenset({"reported", "metadata", "none"})
SELECTION_MODES = frozenset({"component", "path"})
#: Family 12: the fallback reasons each source can produce; `changes_unrecorded`
#: (the record bound) applies under any source, so it is checked apart.
_FALLBACK_SOURCES: Mapping[str, frozenset[str]] = {
    "reported": frozenset(
        {
            "components_unstable",
            "components_unavailable",
            "components_withheld",
            "inspection_failed",
            "component_added_or_removed",
            "unaccounted_path",
            "file_added_or_removed",
        }
    ),
    "metadata": frozenset({"unlabelled_path"}),
    "none": frozenset({"none"}),
}
#: The abandonments that close an Attempt wherever it stands, editor terminal
#: or not: an interruption, the caller's cancellation (§7) and a `rebaseline`
#: block (I2 spec §5.5). In family 13 only these pre-empt an owed close.
_ANYWHERE_ABANDONS = frozenset({"interrupted", "cancelled", "condition_changed"})
# `abandoned` follows an editor terminal (spec §5) — which one, per reason.
_ABANDON_EDITOR: Mapping[str, str] = MappingProxyType(
    {
        "no_exam_impact": "edited",
        "editor_declined": "declined",
        "editor_no_change": "edited",
        "seal_refused": "edited",
        "editor_failed": "failed",
        "editor_unresponsive": "failed",
    }
)
# The §7 threshold terminals, per owner.
_THRESHOLD_TERMINAL: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "target": ("infrastructure_failed", "target_unstable"),
        "environment": ("infrastructure_failed", "environment_unstable"),
        "scorer": ("infrastructure_failed", "scorer_unstable"),
        "rollout": ("budget_exhausted", "rollout_time_budget_exhausted"),
    }
)
#: Family 10 (role resilience spec §4.2, §5.3; PDR-0019, ADR-0023): the four
#: `stage_record` suspensions an improvement role's failure class decides. Each
#: names the failing role record; the second-skip `reflector_failing` names the
#: `reflection_failed` that is the diagnosis of the round that made the skip.
RESILIENCE_SUSPENSIONS: Final = {
    "reflector_provider_failed": ("reflector", "stage_record"),
    "reflector_failing": ("reflector", "stage_record"),
    "editor_provider_failed": ("editor", "stage_record"),
    "editor_failing": ("editor", "stage_record"),
}
#: Family 12 (G2 spec §4.6, ADR-0024): a Target that renders one tree with
#: two component maps inside the map's reference epoch. Refused below family 12.
AFFECTED_SUSPENSIONS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {"target_components_changed": ("target", "stage_record")}
)
#: Family 14 (ADR-0034, F0 R46): a `chat` or `agent_cli` Grouper's provider
#: failure, and its unknown failure or second consecutive full fallback. Each
#: names the `grouping_failed(role_failed)` that decided it. Refused below 14.
GROUPER_SUSPENSIONS: Final = {
    "grouper_provider_failed": ("grouper", "stage_record"),
    "grouper_failing": ("grouper", "stage_record"),
}
#: Family 14 (H10, C§4.3, F0 §11): a failed connection preflight's class, owned
#: by the connection and naming its failed terminal. Refused below family 14.
CONNECTION_SUSPENSIONS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "connection_incompatible": ("connection", "stage_record"),
        "connection_preflight_unavailable": ("connection", "stage_record"),
        "connection_preflight_inconclusive": ("connection", "stage_record"),
    }
)
#: Family 7 (I1 spec §5.1-§5.2): the reasons a `campaign_suspended` names, each
#: with the owner the operator has to act on and the one `evidence_kind` its
#: record carries. A threshold's reason is its owner's family-6 terminal reason.
SUSPENSION_REASONS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        **{reason: (owner, "threshold") for owner, (_, reason) in _THRESHOLD_TERMINAL.items()},
        "target_contract_violated": ("target", "fault_rows"),
        "target_changed": ("target", "fault_rows"),
        "scorer_metric_missing": ("scorer", "fault_rows"),
        # #97 spec §4.1; the lifecycle admits it only under a non-exempt version.
        "scorer_metric_out_of_range": ("scorer", "fault_rows"),
        "grouping_failed": ("campaign", "stage_record"),
        "probe_unavailable": ("campaign", "stage_record"),
        "editor_unresponsive": ("editor", "stage_record"),
        "reflector_unresponsive": ("reflector", "stage_record"),
        # Family 10 only (`_validate_suspension` refuses them below it).
        **RESILIENCE_SUSPENSIONS,
        # Family 12 only (`_validate_suspension` refuses it below it).
        **AFFECTED_SUSPENSIONS,
        # Family 14 only (`_validate_suspension` refuses them below it).
        **GROUPER_SUSPENSIONS,
        **CONNECTION_SUSPENSIONS,
    }
)
#: Family 13 (design §6.13, ADR-0019 as amended 2026-10-03, D6): the reasons
#: whose owner is not fixed. `None` admits any of `OWNERS`; the lifecycle holds
#: a complete row list's owner to `arbitrate_owner`'s order (which is
#: `OWNERS`'), and the audit recomputes it. Kept out of `SUSPENSION_REASONS`,
#: so every family below 13 refuses the reason as unregistered.
CONFIDENCE_SUSPENSIONS: Final[Mapping[str, tuple[str | None, str]]] = MappingProxyType(
    {"cohort_incomplete": (None, "cohort_rows")}
)
#: #194 (families 13 and 14): the reason a Champion measurement left incomplete
#: by a cancelled gap suspends with. Owner `None` as for `CONFIDENCE_SUSPENSIONS`.
#: Kept out of `SUSPENSION_REASONS` and `CONFIDENCE_SUSPENSIONS` so their frozen
#: pins stay as they are; every family below 13 refuses the reason.
CHAMPION_SUSPENSIONS: Final[Mapping[str, tuple[str | None, str]]] = MappingProxyType(
    {"champion_incomplete": (None, "champion_rows")}
)
#: A reason that may also carry a second `evidence_kind` (controller ruling,
#: I1 Task 8 fix round 1): an attestation refusal is `target_changed` with no
#: fault row to list, so it names the campaign record that binds or attests
#: the refused measurement instead (`stage_record`).
ALTERNATE_SUSPENSION_KINDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"target_changed": frozenset({"stage_record"})}
)
#: The discriminated `evidence` of a suspension (spec §5.2): one closed key set
#: per `evidence_kind`, the payload beside the discriminant.
_SUSPENSION_EVIDENCE_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "threshold": frozenset({"evidence_kind", "crossing", "window_start"}),
        "fault_rows": frozenset({"evidence_kind", "count", "rows_digest", "truncated", "rows"}),
        "stage_record": frozenset({"evidence_kind", "seq", "operation_id"}),
        # Family 13 only (design §6.13): `_validate_suspension` refuses the
        # kind below it.
        "cohort_rows": frozenset({"evidence_kind", "count", "rows_digest", "truncated", "rows"}),
        # Families 13 and 14 only (#194): `_validate_suspension` refuses it below.
        "champion_rows": frozenset(
            {"evidence_kind", "measurement_id", "count", "rows_digest", "truncated", "rows"}
        ),
    }
)
#: The evidence kinds only `CHAMPION_SUSPENSIONS` carry.
_CHAMPION_EVIDENCE_KINDS = frozenset(kind for _, kind in CHAMPION_SUSPENSIONS.values())
#: The evidence kinds only `CONFIDENCE_SUSPENSIONS` carry.
_CONFIDENCE_EVIDENCE_KINDS = frozenset(kind for _, kind in CONFIDENCE_SUSPENSIONS.values())
#: ADR-0019 as amended 2026-10-02: the one reason whose `stage_record` also
#: carries `prefix`, the
#: refused measurement journal's `journal_prefix` locator as it stood at the
#: refusal, so the audit recomputes the drift over exactly those bytes. Family
#: 12 only, because the reason is.
_PREFIXED_SUSPENSIONS = frozenset(AFFECTED_SUSPENSIONS)
#: a27 spec §4.2 (#94): the one reason whose `stage_record` *may* carry
#: `prefix`, its refusal at materialization's; a promotion-admission refusal has
#: none. Admitted only under a producing version that owes it.
_OPTIONALLY_PREFIXED_SUSPENSIONS = frozenset({"target_changed"})
#: `measurements/<measurement_id>/events.jsonl`, the file a refusal prefix names.
_REFUSAL_PREFIX_FILE = re.compile(r"measurements/(?P<measurement_id>[^/]+)/events\.jsonl\Z")
#: The crossing `threshold_reached` carries, which a threshold suspension
#: carries unchanged.
_CROSSING_KEYS = frozenset({"owner", "kind", "count", "denominator", "max_count", "max_rate"})
_COHORT_ROW_KEYS = frozenset({"measurement_id", "case_id", "sample_id", "retry_ordinal", "owner"})
_FAULT_ROW_KEYS = frozenset(
    {"measurement_id", "case_id", "sample_id", "retry_ordinal", "reason", "code"}
)
#: The campaign-level fault reasons of a sample row
#: (`CampaignMeasurement.fault_rows`).
FAULT_ROW_REASONS = frozenset(
    {"infrastructure_failed", "scorer_metric_missing", "scorer_metric_out_of_range"}
)
#: The listed prefix of a `fault_rows` suspension, the bound `pairs_excluded` uses.
MAX_FAULT_ROWS = MAX_EXCLUDED_PAIRS

T = TypeVar("T")


def member_diagnostic(rule: str, *, family_version: int = 13) -> str:
    """Select shared member-error wording from an explicitly recorded family.

    Only diagnostic templates pass here; persisted records are never translated.
    Context-free callers retain the legacy wording, as do all earlier families.
    """
    vocabulary = campaign_vocabulary_for(family_version if family_version in (14, 15) else 13)
    return rule.replace("cohort", vocabulary.registry_role).replace(
        "family-13", f"family-{vocabulary.family_version}"
    )


def _need(value: T | None, rule: str) -> T:
    if value is None:
        raise SchemaError(rule)
    return value


def _decimal(value: object, rule: str = "value is not a decimal string") -> Fraction:
    _durable_reference._refuse(
        isinstance(value, str) and _DECIMAL.fullmatch(value) is not None, rule
    )
    assert isinstance(value, str)
    return Fraction(value)


def _number(value: object, rule: str = "value is not a finite number") -> float:
    _durable_reference._refuse(type(value) in (int, float), rule)
    assert isinstance(value, (int, float))
    try:
        number = float(value)
    except OverflowError:
        # An `int` outside `float` range has no image to test for finiteness. It
        # is exactly as inadmissible as an infinity, so it is refused as one and
        # the caller sees this module's own rule instead of a bare
        # `OverflowError` the envelope would flatten to "invalid journal record".
        number = float("inf")
    _durable_reference._refuse(isfinite(number), rule)
    return number


def _positive(value: object, rule: str = "value is not a positive integer") -> int:
    _durable_reference._refuse(type(value) is int and value >= 1, rule)
    assert isinstance(value, int)
    return value


def _validate_evidence(event: str, value: object) -> None:
    if event in CONDITIONAL_E_RECORDS and value is None:
        return
    _durable_reference._refuse(
        isinstance(value, list) and bool(value), "evidence must be a non-empty list"
    )
    assert isinstance(value, list)
    for item in value:
        _durable_reference.DurableReference.from_payload(item)


def _validate_authorization(value: object) -> None:
    """The `authorization` slot: an id, the operator's reason and its origin."""

    a = _keys(value, _AUTHORIZATION_KEYS)
    _durable_reference._sha(a["id"], "authorization id is not a sha-256 digest")
    _durable_reference._refuse(
        type(a["reason"]) is str and 1 <= len(a["reason"]) <= 1000, "authorization reason"
    )
    _durable_reference._refuse(validate_issued_at(a["issued_at"]), "authorization issued_at")
    _durable_reference._refuse(a["source"] in ("caller", "file"), "authorization source")


def validate_payload(
    event: str, payload: dict[str, Any], *, final: bool, independent: bool = False
) -> None:
    """Validate the shared final grammar, with independent-family deltas upstream."""

    keys = _FINAL_KEYS
    _durable_reference._refuse(event in keys, "event is not a campaign record")
    if event == "authorization_applied":
        _validate_continuable_authorization_applied(payload)
        return
    optional = _FINAL_OPTIONAL
    extra_optional = optional.get(event, frozenset())
    if event == campaign_vocabulary_for(14).measurement_bound:
        extra_optional = extra_optional | {"stored_champion", "evidence"}
    if independent and event == "grouping_finished":
        extra_optional = extra_optional | {"full_descriptions_digest"}
    p = _keys(payload, keys[event], extra_optional)
    if event in _FINAL_BOUNDED_RECORDS:
        _validate_final_bounds(
            p, inherited_evidence=event != campaign_vocabulary_for(14).measurement_bound
        )

    if (
        event in CONTINUABLE_E_RECORDS
        or event in CONDITIONAL_E_RECORDS
        or (event == campaign_vocabulary_for(14).measurement_completed)
        or (event in _REFRESH_EVENTS)
        or ((event == campaign_vocabulary_for(14).measurement_bound) and ("evidence" in p))
    ):
        _validate_evidence(event, p["evidence"])
    family_version = 15 if independent else (FINAL_CAMPAIGN_FAMILY_VERSION)
    if event == "campaign_created":
        _validate_created(p, family_version=family_version)
    elif event in ("baseline_bound", "measurement_bound", "parent_measurement_bound"):
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["measurement_id"])
        _durable_reference._sha(p["candidate_digest"])
        if event == "baseline_bound":
            _identifier(p["champion_version"])
        else:
            _identifier(p["attempt_id"])
            _durable_reference._sha(p["identity_digest"])
    elif event in ("baseline_complete", "champion_complete"):
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["measurement_id"])
    elif event == "attempt_opened":
        _positive(p["round"], "round is not a positive integer")
        _positive(p["attempt_index"], "attempt_index is not a positive integer")
        _identifier(p["attempt_id"])
        _identifier(p["parent_version"])
        _durable_reference._sha(p["parent_digest"])
    elif event == "editor_started":
        _identifier(p["attempt_id"])
        _identifier(p["operation_id"])
        _durable_reference._sha(p["brief_digest"])
    elif event in EDITOR_TERMINALS:
        _identifier(p["attempt_id"])
        _identifier(p["operation_id"])
        if p["note_digest"] is not None:
            _durable_reference._sha(p["note_digest"])
        if event == "editor_finished":
            _durable_reference._refuse(
                p["status"] in EDITOR_STATUSES, "editor status is not in the registry"
            )
            _durable_reference._refuse(
                p["note_digest"] is not None, "editor_finished carries a note digest"
            )
            _durable_reference._refuse(
                type(p["continued"]) is bool, "editor_finished.continued is not a boolean"
            )
        else:
            _validate_error_class(p["error_class"], (ADAPTIVE_EDITOR_ERRORS))
            _validate_editor_reason(p)
    elif event == "attempt_sealed":
        _identifier(p["attempt_id"])
        _durable_reference._sha(p["candidate_digest"])
        _durable_reference._sha(p["dependency_digest"])
        _validate_artifact_locator(p["artifact_locator"])
    elif event == "stage_opened":
        _identifier(p["measurement_id"])
        _positive(p["stage"], "stage is not a positive integer")
        _positive(p["look"], "look is not a positive integer")
        _durable_reference._refuse(
            type(p["sample_count"]) is int and p["sample_count"] >= 0,
            "sample_count is not a non-negative integer",
        )
    elif event == "indeterminate_observed":
        _durable_reference._refuse(p["owner"] in OWNERS, "owner is not in the registry")
        _durable_reference._refuse(
            INDETERMINATE_REASONS.get(p["reason"]) == p["owner"],
            "reason is not an indeterminate reason of that owner",
        )
        for name in ("measurement_id", "case_id", "sample_id"):
            _identifier(p[name])
        _positive(p["count"], "count is not a positive integer")
        _durable_reference._refuse(
            type(p["retry_ordinal"]) is int and p["retry_ordinal"] >= 0,
            "retry_ordinal is not a non-negative integer",
        )
        _durable_reference._refuse(
            type(p["retry_follows"]) is bool, "retry_follows is not a boolean"
        )
    elif event == "pairs_excluded":
        _validate_pairs_excluded(p)
    elif event == "repairs_dispatched":
        _validate_repairs_dispatched(p)
    elif event == "stage_redispatched":
        _validate_stage_redispatched(p)
    elif event == "threshold_reached":
        _validate_crossing(p)
        _durable_reference._refuse(
            p["barrier"] in THRESHOLD_BARRIERS, "barrier is not in the registry"
        )
        _positive(p["round"], "round is not a positive integer")
    elif event == "campaign_suspended":
        _validate_suspension(p)
    elif event == "look_not_started":
        _identifier(p["attempt_id"])
        _positive(p["look"], "look is not a positive integer")
        _durable_reference._refuse(
            isinstance(p["missing"], list) and bool(p["missing"]), "missing must be a list"
        )
        for entry in p["missing"]:
            row = _keys(entry, {"measurement_id", "case_id", "sample_id", "state"})
            for name in ("measurement_id", "case_id", "sample_id", "state"):
                _identifier(row[name])
    elif event == "capacity_exhausted":
        _durable_reference._refuse(
            p["owner"] in CAPACITY_OWNERS, "capacity owner is not in the registry"
        )
        _durable_reference._refuse(
            p["code"] in CAPACITY_CODES, "capacity code is not in the registry"
        )
        if p["measurement_id"] is not None:
            _identifier(p["measurement_id"])
    elif event == "attempt_closed":
        _identifier(p["attempt_id"])
        outcomes = _FINAL_ATTEMPT_OUTCOMES
        _durable_reference._refuse(
            p["outcome"] in outcomes, "attempt outcome is not in the registry"
        )
        _durable_reference._refuse(
            p["reason"] in outcomes[p["outcome"]], "reason does not match the outcome"
        )
        if p["look"] is not None:
            _positive(p["look"], "look is not a positive integer")
        if p["code"] is not None:
            _identifier(p["code"])
        _durable_reference._refuse(
            (p["code"] is not None) == (p["reason"] == "seal_refused"),
            "code is present exactly for seal_refused",
        )
    elif event == "selection_decided":
        _validate_confidence_selection(p)
    elif event == "round_closed":
        _positive(p["round"], "round is not a positive integer")
        _integer(p["attempts_opened"])
        round_outcomes = _CONFIDENCE_ROUND_OUTCOMES
        _durable_reference._refuse(
            p["outcome"] in round_outcomes, "round outcome is not in the registry"
        )
        _identifier(p["champion_version"])
        _durable_reference._sha(p["champion_digest"])
        if p["outcome"] == "unplanned":
            _durable_reference._refuse(
                p["reason"] in UNPLANNED_REASONS, "unplanned reason is not in the registry"
            )
        else:
            _durable_reference._refuse(
                p["reason"] is None, "only an unplanned round carries a reason"
            )
        if "visitation" in p:
            # Family 10 (spec §4.2): only a skip carries it; whether the prefix
            # justifies the skip is the lifecycle's (`skip_due`).
            _durable_reference._refuse(
                p["visitation"] == "skipped", "visitation is not in the registry"
            )
            _durable_reference._refuse(
                p["outcome"] == "unplanned" and p["reason"] == "reflection_failed",
                "only a failed reflection's unplanned round carries visitation",
            )
        coverage = _keys(
            p["coverage"],
            {
                "selected_cohort_visits",
                "claimed_cases",
                "examined_cases",
                "measured_cases",
                "had_assessed_candidate",
                "cycle",
                "closure_refs",
                "omitted_closure_refs",
            },
        )
        _durable_reference._refuse(
            isinstance(coverage["selected_cohort_visits"], list)
            and len(coverage["selected_cohort_visits"]) <= 64
            and all(isinstance(item, str) for item in coverage["selected_cohort_visits"]),
            "selected cohort visits",
        )
        for name in ("claimed_cases", "examined_cases", "measured_cases"):
            _durable_reference._refuse(
                type(coverage[name]) is int and coverage[name] >= 0,
                f"{name} is not a non-negative integer",
            )
        _positive(coverage["cycle"], "coverage cycle is not a positive integer")
        _durable_reference._refuse(
            type(coverage["had_assessed_candidate"]) is bool,
            "coverage assessed candidate",
        )
        _durable_reference._refuse(
            isinstance(coverage["closure_refs"], list) and len(coverage["closure_refs"]) <= 8,
            "closure refs limit",
        )
        for source in coverage["closure_refs"]:
            _durable_reference.DurableReference.from_payload(source)
        _durable_reference._refuse(
            type(coverage["omitted_closure_refs"]) is int and coverage["omitted_closure_refs"] >= 0,
            "closure ref omissions",
        )
    elif event == "campaign_finished":
        _durable_reference._refuse(
            p["outcome"] in CAMPAIGN_OUTCOMES, "campaign outcome is not in the registry"
        )
        _durable_reference._refuse(p["reason"] in REASONS, "reason is not in the campaign registry")
        continuation = _keys(p["continuation"], {"kind", "reason"})
        _durable_reference._refuse(
            continuation["kind"] in CONTINUATIONS, "continuation kind is not in the registry"
        )
        _durable_reference._refuse(
            continuation["kind"] == "linked_run",
            "family 6 terminals continue as linked_run",
        )
        _durable_reference._refuse(
            continuation["reason"] == p["reason"],
            "a linked continuation names the terminal's reason",
        )
    elif event == "instance_attested":
        for name in ("measurement_id", "instance_id", "boot_id"):
            _identifier(p[name])
        _durable_reference._sha(p["loaded_candidate_digest"])
        _durable_reference._sha(p["dependency_digest"])
        # The full attested `TargetIdentity` (spec §8): promotion admission runs
        # R3's `verify_attestation` over it, so it is parsed strictly here and
        # never carried as an opaque blob. The three flat keys stay because
        # projections and the lifecycle read them; the lifecycle also refuses a
        # row whose identity disagrees with them.
        TargetIdentity.from_payload(p["identity"])
        if "description" in p:
            # PDR-0014: the backend's own words about the instance, bounded by
            # the wire grammar rather than a second copy of it here. A present
            # key is a described instance: `None` is written as an absent key,
            # so the record has one spelling of "described nothing".
            _durable_reference._refuse(
                p["description"] is not None, "an absent description is an absent key"
            )
            try:
                validate_instance_description(p["description"])
            except ValueError as exc:
                raise SchemaError("instance description is not a flat scalar object") from exc
        if "components_digest" in p:
            # G2 spec §4.2: a pointer at the measurement's `components_declared`;
            # `_AFFECTED_OPTIONAL` admits the key in family 12 only.
            _durable_reference._sha(
                p["components_digest"], "components_digest is not a sha-256 digest"
            )
    elif event == "focus_selected":
        _positive(p["round"], "round is not a positive integer")
        _durable_reference._sha(p["policy_digest"])
        _durable_reference._refuse(
            p["reason"] in FOCUS_REASONS, "focus reason is not in the registry"
        )
        _durable_reference._refuse(
            type(p["unavailable"]) is int and p["unavailable"] >= 0,
            "unavailable is not a non-negative integer",
        )
        _durable_reference._refuse(isinstance(p["clusters"], list), "clusters must be a list")
        for cluster in p["clusters"]:
            row = _keys(cluster, {"fingerprint", "mass", "cases", "stalled"})
            _durable_reference._sha(row["fingerprint"])
            _durable_reference._refuse(
                type(row["mass"]) is int and row["mass"] >= 1,
                "cluster mass is not a positive integer",
            )
            _durable_reference._refuse(
                isinstance(row["cases"], list)
                and bool(row["cases"])
                and all(isinstance(case, str) for case in row["cases"]),
                "cluster cases must be a non-empty list of identifiers",
            )
            _durable_reference._refuse(
                type(row["stalled"]) is bool, "cluster stalled is not a boolean"
            )
        if p["focus_fingerprint"] is not None:
            _durable_reference._sha(p["focus_fingerprint"])
    elif event == "grouping_started":
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["grouping_id"])
        for name in ("baseline_binding", "policy_digest", "registry_digest"):
            _durable_reference._sha(p[name])
        _durable_reference._refuse(
            isinstance(p["grouper_kind"], str) and p["grouper_kind"] in GROUPER_KINDS14,
            "grouper kind is not in the registry",
        )
    elif event == "grouping_finished":
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["grouping_id"])
        _durable_reference._sha(p["result_digest"])
        _validate_grouping_result(
            p["result"],
            p["result_digest"],
            description_chars=FAMILY15_DESCRIPTION_CHARS if independent else 512,
        )
        _validate_read_activity(p["reads"])
        _validate_grouping_fallback(p["fallback"], p["calls"])
        if "full_descriptions_digest" in p:
            _durable_reference._sha(p["full_descriptions_digest"])
    elif event == "grouping_failed":
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["grouping_id"])
        _durable_reference._refuse(
            p["code"] in (GROUPING_FAILURE_CODES14),
            "grouping failure code is not in the registry",
        )
        _validate_grouper_failure(p)
        _validate_read_activity(p["reads"])
    elif event == "focus_portion_selected":
        _positive(p["round"], "round is not a positive integer")
        for name in (
            "baseline_prefix_digest",
            "baseline_binding",
            "policy_digest",
            "registry_digest",
        ):
            _durable_reference._sha(p[name])
        # `unmeasurable` is family 6's own: something failed, but every failing
        # cohort was empty once the excluded baseline pairs left it (spec §4.3).
        portion_reasons = {"selected", "none", "unmeasurable"}
        _durable_reference._refuse(
            p["reason"] in portion_reasons, "focus portion reason is not in the registry"
        )
        for name in ("available", "unavailable", "failing"):
            _durable_reference._refuse(
                type(p[name]) is int and p[name] >= 0,
                f"{name} is not a non-negative integer",
            )
        _positive(p["cycle"], "cycle is not a positive integer")
        for name, limit in (("cohort_ids", 64), ("case_ids", 64)):
            _durable_reference._refuse(
                isinstance(p[name], list)
                and len(p[name]) <= limit
                and len(set(p[name])) == len(p[name])
                and all(isinstance(item, str) for item in p[name]),
                f"{name} is not a bounded unique list of identifiers",
            )
        if p["reason"] == "none":
            _durable_reference._refuse(
                p["grouping_digest"] is None
                and p["neighborhood_id"] is None
                and not p["cohort_ids"]
                and not p["case_ids"]
                and p["failing"] == 0,
                "focus-less portion carries no grouping or selected cases",
            )
        elif p["reason"] == "unmeasurable":
            # The grouping ran and named the failing records; what the filter
            # left was empty, so there is no neighborhood and no selected case.
            _durable_reference._sha(p["grouping_digest"])
            _durable_reference._refuse(
                p["neighborhood_id"] is None
                and not p["cohort_ids"]
                and not p["case_ids"]
                and p["failing"] > 0,
                "an unmeasurable portion names a neighborhood or a selected case",
            )
        else:
            _durable_reference._sha(p["grouping_digest"])
            _durable_reference._sha(p["neighborhood_id"])
            _durable_reference._refuse(
                bool(p["cohort_ids"]) and bool(p["case_ids"]),
                "selected portion has cohorts and cases",
            )
    elif event == "attempt_input_frozen":
        _identifier(p["attempt_id"])
        _positive(p["round"], "round is not a positive integer")
        _durable_reference._sha(p["common_input_digest"])
        _durable_reference._sha(p["input_digest"])
        _durable_reference._refuse(
            p["feedback_mode"] in {"none", "sequential"}, "feedback mode is not in the registry"
        )
        _validate_frozen_feedback(p["feedback"], p["feedback_mode"], independent=independent)
        _durable_reference._refuse(
            isinstance(p["source_refs"], list) and len(p["source_refs"]) <= (16),
            "source_refs limit",
        )
        for source in p["source_refs"]:
            _durable_reference.DurableReference.from_payload(source)
    elif event == "probe_checked":
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["reflection_id"])
        _durable_reference._refuse(
            p["status"] in PROBE_STATUSES, "probe status is not in the registry"
        )
    elif event == "reflection_started":
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["reflection_id"])
        _identifier(p["operation_id"])
        _durable_reference._sha(p["input_digest"])
    elif event == "probe_completed":
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["reflection_id"])
        _positive(p["seq"], "seq is not a positive integer")
        _identifier(p["method"])
        _durable_reference._refuse(
            isinstance(p["path"], str) and 0 < len(p["path"]) <= 256, "path length"
        )
        _durable_reference._refuse(
            (p["status"] is None) != (p["error"] is None),
            "exactly one of status or error is set",
        )
        if p["status"] is not None:
            _durable_reference._refuse(type(p["status"]) is int, "probe status is not an integer")
        if p["error"] is not None:
            _durable_reference._refuse(
                p["error"] in PROBE_ERRORS, "probe error is not in the registry"
            )
    elif event in ("reflection_finished", "reflection_failed"):
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["reflection_id"])
        _identifier(p["operation_id"])
        if p["note_digest"] is not None:
            _durable_reference._sha(p["note_digest"])
        _validate_read_activity(p["reads"])
        if event == "reflection_finished":
            _durable_reference._refuse(
                p["status"] in REFLECTION_STATUSES, "reflection status is not in the registry"
            )
            _durable_reference._refuse(isinstance(p["groups"], list), "groups is not a list")
            for group in p["groups"]:
                _validate_group_shape(group)
            # The per-field text limit is the round's (C6): `_reflection_terminal`.
            violation = _adaptive_group_violation(p["groups"], None)
            _durable_reference._refuse(violation is None, violation or "")
            _durable_reference._refuse(
                isinstance(p["out_of_scope_paths"], list)
                and all(isinstance(path, str) for path in p["out_of_scope_paths"]),
                "out_of_scope_paths must be a list of strings",
            )
            _durable_reference._refuse(
                type(p["probes_used"]) is int and p["probes_used"] >= 0,
                "probes_used is not a non-negative integer",
            )
        else:
            _validate_error_class(
                p["error_class"],
                (FINAL_REFLECTION_ERRORS),
            )
            _validate_reflection_reason(p, independent=independent)
    elif event == "gate_scope_selected":
        _positive(p["round"], "round is not a positive integer")
        _durable_reference._refuse(
            p["source"] in (FAMILY15_GATE_SOURCES if independent else MODEL_GROUPING_GATE_SOURCES),
            "gate source is not in the registry",
        )
        _durable_reference._refuse(
            isinstance(p["cases"], list)
            and bool(p["cases"])
            and all(isinstance(case, str) for case in p["cases"]),
            "cases must be a non-empty list of identifiers",
        )
        for name in ("claimed_cases", "selected_cases", "unevaluable_cases"):
            _durable_reference._refuse(
                isinstance(p[name], list) and all(isinstance(case, str) for case in p[name]),
                f"{name} must be a list of identifiers",
            )
        _durable_reference._refuse(bool(p["selected_cases"]), "selected_cases must not be empty")
    elif event == "identity_rebased":
        # Shape, digests and the one thing a rebase may never say. Whether the
        # record actually moves *this* prefix's identity is an L-rule, not a
        # shape rule: `CampaignLifecycle._identity_rebased` proves it.
        _adaptive_identity_and_policy(p["identity"])
        _validate_identity_connection_fields(p["identity"], family_version)
        digest = hashlib.sha256(canonical_json(p["identity"])).hexdigest()
        _durable_reference._refuse(digest == p["identity_digest"], "identity digest does not match")
        adaptation = _keys(p["adaptation"], {"version", "digest"})
        _durable_reference._refuse(
            adaptation["version"] == _adaptation_profile_for(),
            "adaptation slot version",
        )
        _durable_reference._sha(adaptation["digest"], "adaptation slot digest")
        _validate_adaptation_payload(
            p["adaptation_payload"], adaptation, family_version=family_version
        )
        _validate_train_partition(p["adaptation_payload"], p["identity"])
        _identifier(p["producing_version"])
        _durable_reference._sha(p["previous_digest"], "previous_digest is not a sha-256 digest")
        _durable_reference._sha(p["instance_digest"], "instance_digest is not a sha-256 digest")
        fields = p["changed_fields"]
        _durable_reference._refuse(
            type(fields) is list
            and bool(fields)
            and all(type(f) is str and _DOTTED.fullmatch(f) is not None for f in fields),
            "changed_fields is not a non-empty list of dotted fields",
        )
        # Family 7 (I2 spec §5.5): a rebase paired with an applied `continue`
        # or `rebaseline` may move comparable fields -- whether this one is so
        # paired is the lifecycle's rule. `artifact_store` never moves in
        # place (data integrity, spec §1.3), so it stays a shape refusal.
        _durable_reference._refuse(
            not any(
                comparable_field(f, family_version=family_version)
                and (f.split(".")[1] == "artifact_store")
                for f in fields
            ),
            "identity_rebased changes a comparable field",
        )
        # Family 14 (PDR-0023, ADR-0029; H11 choice 9): a `role_visibility`
        # change offers only `close`, so no rebase -- plain or the rebase of an
        # in-place `continue`/`rebaseline` -- may move it. The lifecycle then
        # requires `changed_fields` to be the computed diff, so this shape rule
        # covers a record that moves the digest without declaring it too.
        _durable_reference._refuse(
            not (any(moves_role_visibility(f) for f in fields)),
            "identity_rebased changes role_visibility",
        )
        _validate_authorization(p["authorization"])
    elif event == "candidate_inspected":
        # Family 12 only (G2 spec §4.5): every earlier key registry refuses it.
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["attempt"])
        _positive(p["inspection"], "inspection is not a positive integer")
        try:
            validate_instance_components(p["components"])
        except ValueError as exc:
            raise SchemaError("components is not a valid wire map") from exc
        if "components_withheld" in p:
            _positive(p["components_withheld"], "components_withheld is not a positive integer")
            _durable_reference._refuse(
                p["components"] is not None, "components_withheld requires a component map"
            )
    elif event == "affected_cases_selected":
        _validate_affected_selection(p)
    elif event == "comparison_bound":
        _validate_comparison_bound(p, family_version=(14))
    elif event == "noise_calibration_bound":
        _validate_noise_calibration_bound(p)
    elif event == "noise_calibration_completed":
        _validate_noise_calibration_completed(p)
    elif event == "comparison_look_evaluated":
        _validate_comparison_look(p)
    elif event == "finalists_bound":
        _validate_finalists_bound(p)
    elif event == campaign_vocabulary_for(14).measurement_bound:
        _validate_cohort_measurement_bound(p)
    elif event == campaign_vocabulary_for(14).measurement_completed:
        _validate_cohort_measurement_completed(p)
    elif event == "global_comparison_completed":
        _validate_global_comparison_completed(p)
    elif event == "baseline_refresh_bound":
        _validate_baseline_refresh_bound(p)
    elif event == "baseline_refresh_completed":
        _validate_baseline_refresh_completed(p)
    elif event == CONNECTION_START_EVENT:
        _validate_connection_started(p)
    elif event in CONNECTION_TERMINALS:
        _validate_connection_terminal(event, p)
    elif event == "measurement_rebound":
        # Family 7 only: every earlier family's key registry refuses the event.
        _positive(p["round"], "round is not a positive integer")
        _identifier(p["from_measurement_id"])
        _identifier(p["measurement_id"])
        _positive(p["epoch"], "epoch is not a positive integer")
        _durable_reference._sha(p["identity_digest"])
        _durable_reference._refuse(
            type(p["imported"]) is int and p["imported"] >= 0,
            "imported is not a non-negative integer",
        )
        _durable_reference._sha(p["pairs_digest"], "pairs_digest is not a sha-256 digest")
        _durable_reference._refuse(
            type(p["released"]) is int and p["released"] >= 0,
            "released is not a non-negative integer",
        )
    elif event == "authorization_applied":
        _durable_reference._refuse(
            p["code"] == "run_dir_unsafe_entry", "authorization_applied code"
        )
        _identifier(p["path"])
        _durable_reference._sha(p["instance_digest"], "instance_digest is not a sha-256 digest")
        _validate_authorization(p["authorization"])
    else:
        _durable_reference._refuse(False, "event is not a campaign record")


def _index_list(value: object, name: str) -> list[int]:
    _durable_reference._refuse(isinstance(value, list), f"{name} is not a list")
    assert isinstance(value, list)
    _durable_reference._refuse(
        all(type(index) is int and index >= 0 for index in value),
        f"{name} are not non-negative integers",
    )
    _durable_reference._refuse(
        all(a < b for a, b in zip(value, value[1:], strict=False)),
        f"{name} are not sorted and unique",
    )
    return value


def _validate_affected_selection(p: Mapping[str, Any]) -> None:
    """The prefix-free half of `affected_cases_selected` (G2 spec §4.4).

    The upper bound of the indices and the `source` in force need the
    identity, so `CampaignLifecycle._affected_cases_selected` checks them.
    """

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["attempt"])
    _durable_reference._refuse(
        p["source"] in SELECTION_SOURCES, "selection source is not a provenance kind"
    )
    _durable_reference._refuse(
        (p["mode"] in SELECTION_MODES) if p["source"] == "reported" else p["mode"] is None,
        "selection mode is set exactly for a reported source",
    )
    reason = p["fallback_reason"]
    _durable_reference._refuse(
        reason is None or reason in FALLBACK_REASONS, "fallback reason is not in the registry"
    )
    changed = p["changed"]
    if reason == "changes_unrecorded":
        # `_bounded` falls back on either bound, so the count alone may be
        # within `CHANGED_MAX`; it is never zero.
        _durable_reference._refuse(
            type(changed) is int and changed >= 1,
            "changes_unrecorded records only the count of changed parts",
        )
    else:
        _durable_reference._refuse(
            isinstance(changed, list), "changed is a list unless changes_unrecorded"
        )
        assert isinstance(changed, list)
        _durable_reference._refuse(
            all(isinstance(entry, str) and entry for entry in changed),
            "changed entries are non-empty strings",
        )
        _durable_reference._refuse(
            all(a < b for a, b in zip(changed, changed[1:], strict=False)),
            "changed is not sorted and unique",
        )
        _durable_reference._refuse(
            len(changed) <= CHANGED_MAX and len(canonical_json(changed)) <= CHANGED_MAX_BYTES,
            "changed exceeds the record bound",
        )
    cases = _index_list(p["cases"], "cases")
    unknown = _index_list(p["unknown_cases"], "unknown_cases")
    _durable_reference._refuse(
        set(unknown) <= set(cases), "unknown cases are not within the affected cases"
    )
    if reason is not None:
        _durable_reference._refuse(
            not unknown, "a fallback lists every case, so no case is unknown"
        )
        _durable_reference._refuse(
            reason in _FALLBACK_SOURCES[p["source"]] or reason == "changes_unrecorded",
            "fallback reason does not belong to the selection source",
        )
    _durable_reference._refuse(
        p["source"] != "none" or reason in ("none", "changes_unrecorded"),
        "the none source falls back as none or changes_unrecorded",
    )


def affected_layer_one(
    selection: Mapping[str, Any],
    corpus_cases: Sequence[Mapping[str, Any]],
    exam: Sequence[str],
) -> tuple[str, ...]:
    """Layer 1 of a family-12 Attempt (G2 design §3): the selection's affected
    cases that are in the exam, as case ids in corpus order.

    `selection` is an `affected_cases_selected` payload, whose `cases` index
    `corpus_cases` (the identity in force); `exam` is the round's gate scope.
    """

    focus = set(exam)
    ids = (str(corpus_cases[index]["case_id"]) for index in selection["cases"])
    return tuple(case_id for case_id in ids if case_id in focus)


@dataclass(frozen=True)
class AffectedLayers:
    """The case sets of one selected family-12 Attempt (G2 design §3, spec §4.6).

    All three are case ids in corpus order. `layer_one` is exam ∩ affected,
    `layer_two` the other affected cases; the Candidate measures both, or the
    whole corpus when the global layer is on; the fresh Parent measures
    `layer_one`.
    """

    layer_one: tuple[str, ...]
    layer_two: tuple[str, ...]
    corpus: tuple[str, ...]
    global_layer: bool

    @property
    def measured(self) -> tuple[str, ...]:
        chosen = {*self.layer_one, *self.layer_two}
        return tuple(case_id for case_id in self.corpus if case_id in chosen)

    @property
    def candidate_cases(self) -> tuple[str, ...]:
        return self.corpus if self.global_layer else self.measured


def affected_layers(
    selection: Mapping[str, Any],
    corpus_cases: Sequence[Mapping[str, Any]],
    exam: Sequence[str],
    *,
    global_layer: bool,
) -> AffectedLayers:
    """The layer sets of a selection over the identity in force and the round's exam."""

    corpus = tuple(str(case["case_id"]) for case in corpus_cases)
    affected = {corpus[index] for index in selection["cases"]}
    first = set(affected_layer_one(selection, corpus_cases, exam))
    one = tuple(case_id for case_id in corpus if case_id in first)
    two = tuple(case_id for case_id in corpus if case_id in affected and case_id not in first)
    return AffectedLayers(one, two, corpus, global_layer)


def subset_of(
    corpus_cases: Sequence[Mapping[str, Any]], case_ids: Sequence[str]
) -> tuple[int, ...] | None:
    """G2 spec §3.5: the `case_subset` of a case set, or `None` for the whole corpus."""

    names = list(case_ids)
    position = {str(case["case_id"]): index for index, case in enumerate(corpus_cases)}
    if not names or len(set(names)) != len(names) or any(name not in position for name in names):
        raise ValueError("a case set names each corpus case once and is not empty")
    if len(names) == len(position):
        return None
    return tuple(sorted(position[name] for name in names))


def side_of_entry(entry: Mapping[str, Any]) -> str:
    """The schedule side a bound measurement was derived on (G2b3a).

    `champion` for the seed champion's and every fresh baseline's measurement,
    and for a family-12 fresh Parent (it measures the Parent tree); `candidate`
    for every Attempt's own.
    """

    if entry["attempt_id"] == CHAMPION_SIDE or entry.get("role") == "parent":
        return "champion"
    return "candidate"


def _adaptation_profile_for() -> str:
    return "campaign_adaptation/7"


def _validate_train_partition(adaptation: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    if adaptation["profile"] not in PARTITION_PROFILES:
        return
    partition = adaptation["corpus_partition"]
    _durable_reference._refuse(
        partition["train_digest"] == identity["corpus_digest"], "train corpus digest"
    )
    train_ids = [case_id for case_id, split in partition["assignments"] if split == "train"]
    _durable_reference._refuse(
        train_ids == [case["case_id"] for case in identity["corpus_cases"]], "train case set"
    )


def _validate_provenance_labels_at_creation(
    p: Mapping[str, Any], policy: CampaignPolicyV11
) -> None:
    """Family 12 (G2 spec §4.1): labels exactly under `provenance.kind: metadata`,
    each labelled case one of the train corpus cases the identity binds."""

    _durable_reference._refuse(
        isinstance(policy, CampaignPolicyV11), "family 12 requires a /13 policy"
    )
    kind = p["identity"]["policy"]["gate"]["provenance"]["kind"]
    adaptation = p["adaptation_payload"]
    _durable_reference._refuse(
        ("provenance_labels" in adaptation) == (kind == "metadata"),
        "provenance labels follow the provenance kind",
    )
    if "provenance_labels" in adaptation:
        case_ids = {case["case_id"] for case in p["identity"]["corpus_cases"]}
        _durable_reference._refuse(
            all(case_id in case_ids for case_id, _ in adaptation["provenance_labels"]),
            "provenance label case set",
        )


def _validate_adaptation_payload(
    value: object, slot: Mapping[str, Any], *, family_version: int
) -> None:
    """Family 8: the full adaptation identity equals the slot it sits beside.

    `campaign_created` and every `identity_rebased` carry it (ADR-0021), so
    the audit reads the `family_membership` in force at any record.
    """

    try:
        full = AdaptationIdentityV1.from_payload(value)  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SchemaError("adaptation_payload is invalid") from exc
    payload = full.to_payload()
    _durable_reference._refuse(
        family_version != 14
        or not (
            {"evidence", "checks"} & payload.keys()
            or {"max_consecutive_full_fallbacks", "max_repairs"} & payload["grouping"].keys()
            or "max_consecutive_skips" in payload.get("reflection", {})
            # b16 (spec §11): `model_grouping/2` and its portion keys are family
            # 15's. Only a `chat` payload names them: `agent_cli` has its own
            # `max_turns`.
            or (
                payload["grouping"]["kind"] == "chat"
                and (
                    "require_parameters" in payload["grouping"]
                    or payload["grouping"].get("protocol") == MODEL_GROUPING_V2
                    or bool(GROUPING_V2_KEYS.keys() & payload["grouping"].keys())
                )
            )
        ),
        "family 14 adaptation cannot bind evidence, checks, role-loop limits "
        "or the model_grouping/2 Grouper",
    )
    _durable_reference._refuse(full.digest == slot["digest"], "adaptation_payload digest mismatch")
    _durable_reference._refuse(
        full.profile == slot["version"], "adaptation_payload profile mismatch"
    )


def _validate_continuable_authorization_applied(payload: dict[str, Any]) -> None:
    """Family 6's `authorization_applied`, discriminated by `effect` (spec §7).

    An absent `effect` is `hygiene`, so every record `0.3.0a13` wrote reads
    unchanged. Whether a `close`/`terminal` record follows *this* prefix's
    identity and owes the terminal it names is an L-rule
    (`CampaignLifecycle._authorization_applied`); here only its shape and the
    decision's own consistency with the record are proved.
    """

    effect = payload.get("effect", "hygiene") if isinstance(payload, Mapping) else None
    if effect == "hygiene":
        p = _keys(payload, _HYGIENE_KEYS | ({"effect"} if "effect" in payload else set()))
        _durable_reference._refuse(
            p["code"] == "run_dir_unsafe_entry", "authorization_applied code"
        )
        _identifier(p["path"])
        _durable_reference._sha(p["instance_digest"], "instance_digest is not a sha-256 digest")
        _validate_authorization(p["authorization"])
        return
    if effect == "fresh_baseline":
        p = _keys(payload, _FRESH_BASELINE_KEYS)
        _durable_reference._refuse(
            p["code"] == "baseline_import_unavailable", "authorization_applied code"
        )
        _durable_reference._sha(p["instance_digest"], "instance_digest is not a sha-256 digest")
        _durable_reference._sha(p["pairs_digest"], "pairs_digest is not a sha-256 digest")
        _decision_names_the_instance(p)
        return
    # I2 spec §5.4: family 7's in-place effects take the close record's shape.
    _durable_reference._refuse(
        effect in ("close", "terminal") or (effect in IN_PLACE_EFFECTS),
        "authorization_applied effect",
    )
    p = _keys(payload, _EFFECT_KEYS)
    _durable_reference._refuse(p["code"] == "run_identity_changed", "authorization_applied code")
    _durable_reference._sha(p["instance_digest"], "instance_digest is not a sha-256 digest")
    for side in ("journaled", "observed"):
        triple = _keys(p[side], _TRIPLE_KEYS)
        _durable_reference._sha(
            triple["identity_digest"], "identity digest is not a sha-256 digest"
        )
        _identifier(triple["producing_version"])
        if triple["adaptation_digest"] is not None:
            _durable_reference._sha(
                triple["adaptation_digest"], "adaptation digest is not a sha-256 digest"
            )
    _decision_names_the_instance(p)


def _decision_names_the_instance(p: Mapping[str, Any]) -> None:
    try:
        decision = Authorization.from_decision_payload(p["authorization"])
    except AuthorizationError:
        _durable_reference._refuse(False, "authorization_applied decision")
    _durable_reference._refuse(
        decision.code == p["code"] and decision.digest == p["instance_digest"],
        "the decision does not name this instance",
    )


def _validate_crossing(p: Mapping[str, Any]) -> None:
    """The crossing's shape, shared by `threshold_reached` and a threshold
    suspension: a registry owner and kind, integer counts, a decimal rate."""

    _durable_reference._refuse(p["owner"] in OWNERS, "owner is not in the registry")
    _durable_reference._refuse(
        p["kind"] in THRESHOLD_KINDS, "threshold kind is not in the registry"
    )
    for name in ("count", "denominator", "max_count"):
        _integer(p[name])
    _decimal(p["max_rate"], "max_rate is not a decimal string")


def fault_rows_digest(rows: Sequence[tuple[str, str, str, int, str]]) -> str:
    """The `rows_digest` of a `fault_rows` suspension (I1 spec §5.2).

    SHA-256 over the canonical JSON of the sorted `[measurement_id, case_id,
    sample_id, retry_ordinal, reason]` list, so the digest is independent of
    schedule order, as `pairs_digest` is.
    """

    return hashlib.sha256(canonical_json(sorted([list(row) for row in rows]))).hexdigest()


def cohort_rows_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """The `rows_digest` of a `cohort_incomplete` suspension (design §6.13, D6).

    SHA-256 over the canonical JSON of the sorted `[measurement_id, case_id,
    sample_id, retry_ordinal, owner]` lists of the whole set, as
    `fault_rows_digest` is.
    """

    keyed = sorted(
        [
            str(r["measurement_id"]),
            str(r["case_id"]),
            str(r["sample_id"]),
            int(r["retry_ordinal"]),
            str(r["owner"]),
        ]
        for r in rows
    )
    return hashlib.sha256(canonical_json(keyed)).hexdigest()


def requested_pairing(cohort: Mapping[str, Any]) -> str:
    """D7: the pairing a cohort's policy requests -- `exact` under the
    `exact` seed mode, else `unpaired`. An incomplete finalist's
    `global_comparison_completed` carries it, as producer and lifecycle agree."""

    return "exact" if cohort["seed_mode"] == "exact" else "unpaired"


def _validate_reflection_reason(p: Mapping[str, Any], *, independent: bool) -> None:
    """Family 10 (spec §4.1): a settled `reflection_failed` carries a `reason`
    of the closed table, and a lifecycle terminal carries none."""

    if p["error_class"] in LIFECYCLE_ERRORS:
        _durable_reference._refuse("reason" not in p, "a lifecycle terminal carries no reason")
        return
    _durable_reference._refuse("reason" in p, "a settled reflection failure carries a reason")
    _durable_reference._refuse(
        isinstance(p["reason"], str) and (independent or p["reason"] != "program_failed"),
        "reflection reason outside the family-10 table",
    )
    try:
        reflection_class(p["error_class"], p["reason"])
    except KeyError:
        raise SchemaError("reflection reason outside the family-10 table") from None


def _validate_editor_reason(p: Mapping[str, Any]) -> None:
    """Family 10 (spec §5.3): a settled `editor_failed` carries a `reason` of
    the closed Editor table, and a lifecycle terminal carries none."""

    if p["error_class"] in LIFECYCLE_ERRORS:
        _durable_reference._refuse("reason" not in p, "a lifecycle terminal carries no reason")
        return
    _durable_reference._refuse("reason" in p, "a settled editor failure carries a reason")
    _durable_reference._refuse(
        isinstance(p["reason"], str), "editor reason outside the family-10 table"
    )
    try:
        editor_class(p["error_class"], p["reason"])
    except KeyError:
        raise SchemaError("editor reason outside the family-10 table") from None


def _validate_suspension(p: dict[str, Any]) -> None:
    """Family 7's `campaign_suspended` (I1 spec §5.2): a registry reason, the
    owner and `evidence_kind` that reason fixes, and the closed evidence of that
    kind. Whether the evidence matches the prefix is the lifecycle's (a
    threshold's window, a stage record, the cohort rows' members and owner) or
    the audit's (the fault rows, the cohort rows). `RESILIENCE_SUSPENSIONS` are
    family 10's own reasons, `AFFECTED_SUSPENSIONS` family 12's and
    `CONFIDENCE_SUSPENSIONS` family 13's, whose owner may vary.
    `GROUPER_SUSPENSIONS` are family 14's (`final`), owned by the Grouper, and
    so are `CONNECTION_SUSPENSIONS` (`connection`), owned by the connection.
    `CHAMPION_SUSPENSIONS` (`champion`, #194, families 13 and 14) are variable
    like family 13's."""

    reason = p["reason"]
    vocabulary = campaign_vocabulary_for(14)
    champion_reason = reason in CHAMPION_SUSPENSIONS
    variable = (reason == vocabulary.incomplete_reason) or champion_reason
    _durable_reference._refuse(
        variable or (reason in SUSPENSION_REASONS),
        "suspension reason is not in the registry",
    )
    evidence = p["evidence"]
    kind = evidence.get("evidence_kind") if isinstance(evidence, dict) else None
    _durable_reference._refuse(
        (
            kind in _SUSPENSION_EVIDENCE_KEYS
            and kind not in _CONFIDENCE_EVIDENCE_KINDS
            and kind not in _CHAMPION_EVIDENCE_KINDS
        )
        or (kind == vocabulary.suspension_evidence_kind)
        or (kind in _CHAMPION_EVIDENCE_KINDS),
        "evidence_kind is not in the registry",
    )
    assert isinstance(kind, str)
    owner, primary = (
        CHAMPION_SUSPENSIONS[reason]
        if champion_reason
        else (None, vocabulary.suspension_evidence_kind)
        if variable
        else SUSPENSION_REASONS[reason]
    )
    if owner is None:
        # Design §6.13: any threshold owner; which one is the rows' to decide.
        _durable_reference._refuse(
            p["owner"] in OWNERS, f"a {reason} suspension is owned by a threshold owner"
        )
        _durable_reference._refuse(kind == primary, f"reason and owner are not a {kind} suspension")
    else:
        _durable_reference._refuse(
            p["owner"] == owner
            and (kind == primary or kind in ALTERNATE_SUSPENSION_KINDS.get(reason, ())),
            f"reason and owner are not a {kind} suspension",
        )
    keys = _SUSPENSION_EVIDENCE_KEYS[
        "champion_rows" if champion_reason else "cohort_rows" if variable else kind
    ]
    optional: frozenset[str] = frozenset()
    if kind == "stage_record" and reason in _PREFIXED_SUSPENSIONS:
        keys = keys | {"prefix"}
    elif kind == "stage_record" and reason in _OPTIONALLY_PREFIXED_SUSPENSIONS:
        optional = frozenset({"prefix"})
    e = _keys(evidence, keys, optional)
    if kind == "threshold":
        crossing = _keys(e["crossing"], _CROSSING_KEYS)
        _validate_crossing(crossing)
        _durable_reference._refuse(
            crossing["owner"] == p["owner"], "the crossing names another owner"
        )
        if e["window_start"] is not None:
            _positive(e["window_start"], "window_start is not a positive integer")
    elif kind == "fault_rows":
        _validate_fault_rows(e)
    elif champion_reason:
        _validate_champion_rows(e, reason)
    elif kind == vocabulary.suspension_evidence_kind:
        _validate_cohort_rows(e, reason, family_version=vocabulary.family_version)
    else:
        _positive(e["seq"], "seq is not a positive integer")
        if e["operation_id"] is not None:
            _identifier(e["operation_id"])
        if "prefix" in e:
            refusal_prefix_measurement(e["prefix"])


def refusal_prefix_measurement(locator: object) -> str:
    """The measurement a refusal suspension's `prefix` names, or a `SchemaError`.

    A drift (`target_components_changed`) or, from `0.3.0a27`, a
    `target_changed` refused at materialization (a27 spec §4.2). The locator
    is `measurements/<measurement_id>/events.jsonl#bytes=0:<end>`: the
    `journal_prefix` form `instance_attested.evidence` uses, from the head of
    the journal and never empty. Whether those bytes are a prefix of that
    journal ending at the refusal is the audit's to recompute.
    """

    match = _PREFIX_LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    named = _REFUSAL_PREFIX_FILE.fullmatch(match["file"]) if match is not None else None
    _durable_reference._refuse(
        match is not None
        and named is not None
        and is_identifier(named["measurement_id"])
        and int(match["start"]) == 0
        and int(match["end"]) > 0,
        "the refusal prefix is not a measurement journal prefix",
    )
    assert named is not None
    return str(named["measurement_id"])


#: The G2b3b name, kept for callers of the drift-only reader.
drift_prefix_measurement = refusal_prefix_measurement


def _journal_prefix_end(locator: object, measurement_id: str) -> int | None:
    """The end of `measurement_id`'s own journal prefix `locator` cites, or `None`.

    #109 (a28 spec §3.1): `stage_redispatched` cites
    `measurements/<measurement_id>/events.jsonl#bytes=0:<end>`, the
    `journal_prefix` form `repairs_dispatched` uses. Whether those bytes are
    the journal's head is the audit's.
    """

    match = _durable_reference._LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    if match is None or int(match["start"]) != 0:
        return None
    named = _REFUSAL_PREFIX_FILE.fullmatch(match["file"])
    if named is None or named["measurement_id"] != measurement_id:
        return None
    return int(match["end"])


def journal_prefix_measurement(locator: object) -> str | None:
    """The measurement whose own journal head `locator` cites, else `None`.

    Family 13 (design §6.1, "Prefixes"): `measurements/<id>/events.jsonl#bytes=0:<end>`
    with `end > 0`, the `journal_prefix` form `instance_attested.evidence` uses.
    Whether the bytes are that journal's head is the audit's.
    """

    match = _durable_reference._LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    named = _REFUSAL_PREFIX_FILE.fullmatch(match["file"]) if match is not None else None
    if (
        match is None
        or named is None
        or int(match["start"]) != 0
        or int(match["end"]) == 0
        or not is_identifier(named["measurement_id"])
    ):
        return None
    return str(named["measurement_id"])


def confidence_scopes(
    selection: Mapping[str, Any],
    corpus_cases: Sequence[Mapping[str, Any]],
    exam: Sequence[str],
) -> tuple[list[int], list[int]]:
    """Design §6.2: F (layer 1) and C (layer 2) as sorted `corpus_cases` indices.

    Family 13 has no global layer in the gate (G1), so the Candidate measures
    exactly F ∪ C.
    """

    layers = affected_layers(selection, corpus_cases, exam, global_layer=False)
    position = {str(case["case_id"]): index for index, case in enumerate(corpus_cases)}
    return (
        sorted(position[case_id] for case_id in layers.layer_one),
        sorted(position[case_id] for case_id in layers.layer_two),
    )


def comparison_confidence(policy: CampaignPolicyV11) -> dict[str, Any] | None:
    """`comparison_bound.confidence`: `{focus, causal}` in statistical mode, else `None`."""

    if policy.gate_mode != "statistical":
        return None
    return {"focus": policy.focus_confidence, "causal": policy.causal_confidence}


def comparison_tolerance(policy: CampaignPolicyV11) -> dict[str, Any]:
    """`comparison_bound.tolerance`: the measured-noise kind or the numeric value."""

    if policy.causal_tolerance == "measured_noise":
        return {"kind": "measured_noise"}
    return {"kind": "numeric", "value": policy.causal_tolerance}


def finalist_level(policy: CampaignPolicyV11, attempt: AttemptState) -> PublishedValue | None:
    """A finalist's own global confidence (design §6.7, ADR-0027): null in observe.

    It reads only the policy and the Attempt's governing comparison, so the
    executor (which writes `finalists_bound`) and the lifecycle derive it alike.
    """

    if policy.gate_mode == "observe":
        return None
    comparison = _need(attempt.comparison, "an accepted family-13 attempt has a comparison")
    confidence = comparison["confidence"]
    return global_level(
        confidence["focus"], confidence["causal"], causal_empty=not comparison["causal"]
    )


def recorded_complete(entry: Mapping[str, Any], rounds: Mapping[int, RoundState]) -> bool:
    """F7 plan choice 14: whether a measurement was recorded complete.

    Its registry `complete` flag, or -- for a family-13 cohort member, whose
    flag stays `false` until a later round's `baseline_complete` (F6) -- a
    `cohort_measurement_completed` with `complete: true` for it. ADR-0020 keeps
    a frozen measurement a valid baseline only when it was recorded complete
    before it froze. The lifecycle and the executor's `_rebind_sources` read
    this one rule (`rounds` is the lifecycle's, or a snapshot's, rounds).
    """

    if entry["complete"]:
        return True
    if entry.get("role") not in ("cohort", "final"):
        return False
    state = rounds.get(int(entry["round"]))
    completion = None if state is None else state.cohort_completed.get(str(entry["measurement_id"]))
    return completion is not None and completion["complete"] is True


def running_members(state: RoundState) -> tuple[str, ...]:
    """F7 plan choice 8: a bound cohort's current members that are not completed.

    A completed member keeps its stages and a member a `continue` re-bound
    restarts at stage 1, so the cohort stages, their barrier and the planner
    read these. Without a re-bind no member completes before every stage is
    settled, so this is every member until the first completion.
    """

    current = current_members(state)
    return tuple(name for name in current if name not in state.cohort_completed)


def selection_finalists(state: RoundState) -> tuple[tuple[Mapping[str, Any], str], ...]:
    """Each finalist and its own current final member, in binding/member order."""
    bindings = state.finals or (() if state.cohort is None else (state.cohort,))
    return tuple(
        (finalist, finalist_member(binding, position, members=members))
        for binding in bindings
        for members in (
            current_members(state, final=binding) if state.finals else current_members(state),
        )
        for position, finalist in enumerate(binding["finalists"])
    )


def selection_records(state: RoundState) -> tuple[Mapping[str, Any], ...]:
    """All member completions, then comparisons in Attempt order (R39)."""
    bindings = state.finals or (() if state.cohort is None else (state.cohort,))
    members = tuple(
        mid
        for binding in bindings
        for mid in (
            current_members(state, final=binding) if state.finals else current_members(state)
        )
    )
    finalists = sorted(
        selection_finalists(state),
        key=lambda item: state.attempts[str(item[0]["attempt_id"])].index,
    )
    return (
        *(state.cohort_completed[mid] for mid in members),
        *(state.global_comparisons[str(finalist["attempt_id"])] for finalist, _ in finalists),
    )


def cohort_selection(
    state: RoundState, global_mode: str, *, family_version: int = 13
) -> dict[str, Any]:
    """Design §6.11, D5: a family-13 `selection_decided` payload without `evidence`.

    Without a cohort it is the null selection (`no_finalists`, empty ranking,
    `global_mode` the policy's in force). With one, each finalist's score is
    its `cohort_measurement_completed.point` and its verdict its
    `global_comparison_completed`'s; `decide_selection` ranks them. A
    selected finalist names its Attempt, digests and cohort measurement; a
    retained Champion names only its own cohort measurement; under family 14
    stable it names the round's stored baseline instead. The caller
    holds every completion and every comparison (`_selection_decided`
    refuses otherwise). The executor writes it and the lifecycle re-derives it.
    Family 14 ranks every final binding, resolving each member through its
    binding's current IDs: a member re-bound by `continue` is selected under
    its `<id>@e<k>`. Retention uses the latest binding's Champion when fresh.
    """

    cohort = state.cohort
    if cohort is None:
        return {
            "round": state.round,
            "attempt_id": None,
            "candidate_digest": None,
            "dependency_digest": None,
            "measurement_id": None,
            "global": global_mode,
            "reason": "no_finalists",
            "ranking": [],
        }
    members = current_members(state)
    vocabulary = campaign_vocabulary_for(family_version)
    finalists = selection_finalists(state)
    verdict = decide_selection(
        str(cohort["global"]),
        [
            FinalistVerdict(
                str(finalist["attempt_id"]),
                state.cohort_completed[member]["point"],
                bool(
                    state.global_comparisons[str(finalist["attempt_id"])][vocabulary.contrast_key]
                ),
                # Adapt derived selector inputs, never the persisted record. The
                # frozen family-13 ranking kernel retains its own guard literal.
                (
                    "cohort_incomplete"
                    if state.global_comparisons[str(finalist["attempt_id"])]["reason"]
                    == vocabulary.incomplete_reason
                    else str(state.global_comparisons[str(finalist["attempt_id"])]["reason"])
                ),
            )
            for finalist, member in finalists
        ],
    )
    payload: dict[str, Any] = {
        "round": state.round,
        "attempt_id": None,
        "candidate_digest": None,
        "dependency_digest": None,
        "measurement_id": (
            state.baseline_measurement_id
            if family_version == 14 and cohort.get("stability") == "stable"
            else champion_member(cohort, members=members)
        ),
        "global": cohort["global"],
        "reason": (
            vocabulary.selection_retained_reason
            if verdict.reason == "no_eligible_finalist"
            or (family_version == 14 and verdict.winner is None)
            else verdict.reason
        ),
        "ranking": [
            {
                "attempt_id": row["attempt_id"],
                "score": row["score"],
                vocabulary.contrast_key: row["eligible"],
                "reason": vocabulary.incomplete_reason
                if row["reason"] == "cohort_incomplete"
                else row["reason"],
            }
            for row in verdict.ranking
        ],
    }
    if verdict.winner is not None:
        finalist, member = finalists[verdict.winner]
        attempt = state.attempts[str(finalist["attempt_id"])]
        payload.update(
            attempt_id=finalist["attempt_id"],
            candidate_digest=finalist["candidate_digest"],
            dependency_digest=attempt.dependency_digest,
            measurement_id=member,
        )
    return payload


def binding_close(comparison: Mapping[str, Any]) -> Decision | None:
    """G6, D3, R1: the close a comparison's retained cases force at binding, if any."""

    retained = comparison["retained"]
    return decide_binding(
        focus_retained=len(retained["focus"]),
        causal_cases=len(comparison["causal"]),
        causal_retained=len(retained["causal"]),
    )


def retained_cases(
    cases: Sequence[int],
    corpus_ids: Sequence[str],
    schedule: Sequence[tuple[str, int]],
    excluded: Collection[tuple[str, int]],
    repeats: int,
) -> list[int]:
    """Design G6: the cases among `cases` (corpus indices) that keep at least
    one scheduled baseline pair with repeat at most `repeats` outside
    `excluded`, whose pairs are `(case_id, repeat)` (ruling R13's key).

    The executor binds `comparison_bound.retained` with it (F4) and the audit
    recomputes it from the cited exclusions.
    """

    kept = {
        case_id
        for case_id, repeat in schedule
        if repeat <= repeats and (case_id, repeat) not in excluded
    }
    return [index for index in cases if corpus_ids[index] in kept]


def calibration_owed(comparison: Mapping[str, Any]) -> bool:
    """Design §6.3, G10: `measured_noise` with a non-empty C owes a calibration,
    unless the comparison closes the Attempt at binding (plan choice 7)."""

    return (
        comparison["tolerance"]["kind"] == "measured_noise"
        and bool(comparison["causal"])
        and binding_close(comparison) is None
    )


def calibration_verdict(
    comparison: Mapping[str, Any], result: Mapping[str, Any] | None, *, family_version: int = 13
) -> CalibrationVerdict | None:
    """The governing family's consequence of a completed Parent calibration.

    Family 13 keeps D4's degenerate statistical close; family 14's R25 uses
    zero tolerance and a caveat. Unavailability still closes statistical
    Attempts in both families. `None` before the result.
    """

    if result is None:
        return None
    decide = decide_calibration14 if family_version == 14 else decide_calibration
    return decide(
        str(comparison["mode"]),
        degenerate=bool(result["degenerate"]),
        unavailable=result["unavailable"],
    )


def next_scheduled_look(looks: Sequence[int], reached: int) -> int | None:
    """ADR-0027, design §6.5: the entry of a comparison's frozen `looks` after
    `reached` (`0` before the first), or `None` past the last. Family-13
    stages and looks take each entry once, in order, none skipped."""

    return min((int(look) for look in looks if int(look) > reached), default=None)


def _comparison_moved(attempt: AttemptState) -> bool:
    """G11: has a `measurement_rebound` moved either side of the governing
    comparison since it was bound? `False` before a comparison."""

    comparison = attempt.comparison
    return comparison is not None and (
        comparison["candidate_measurement_id"],
        comparison["parent_measurement_id"],
    ) != (attempt.measurement_id, attempt.parent_measurement_id)


def superseding_comparison_owed(attempt: AttemptState, *, family_version: int = 13) -> bool:
    """G11, plan choice 3: does the Attempt owe a superseding `comparison_bound`?

    Yes when a `continue` re-bound either side of its governing comparison
    and the comparison is undecided. An Attempt that owes a close writes it
    instead; its re-bound measurements are not compared again.
    """

    return _comparison_moved(attempt) and owed_close(attempt, family_version=family_version) is None


def owed_close(
    attempt: AttemptState, *, family_version: int = 13
) -> tuple[Decision, int | None] | None:
    """Design §6.6, PDR-0022 Amendments (2) and (3): the close the governing
    family's Attempt state owes, with the look it names; `None` while nothing
    is decided.

    In order: the bind-time close of its retained cases (G6, D3, R1), a
    closing statistical calibration (D4), then a deciding latest look -- one
    whose decision is not `continue`, which no later stage or look can follow.
    The lifecycle admits no other close except an abandonment wherever the
    Attempt stands (`_ANYWHERE_ABANDONS`), and F4's planner writes exactly this.
    """

    comparison = attempt.comparison
    if comparison is None:
        return None
    close = binding_close(comparison)
    if close is not None:
        return close, None
    verdict = calibration_verdict(
        comparison, attempt.calibration_result, family_version=family_version
    )
    if verdict is not None and verdict.close is not None:
        return verdict.close, None
    if not attempt.looks:
        return None
    look = max(attempt.looks)
    entry = attempt.looks[look]
    if entry["decision"] == "continue":
        return None
    return Decision(str(entry["decision"]), entry["reason"]), look


def look_tolerance(
    comparison: Mapping[str, Any], result: Mapping[str, Any] | None, *, family_version: int = 13
) -> dict[str, Any]:
    """Design §6.5 `tolerance`: the numeric value, or the measured allowance in
    force (`null` before the calibration completes, when it is unavailable, or
    when C is empty and nothing is calibrated)."""

    tolerance = comparison["tolerance"]
    if tolerance["kind"] == "numeric":
        return {"source": "numeric", "value": tolerance["value"]}
    value = None if result is None else result["allowance"]
    if family_version == 14 and result is not None:
        value = measured_tolerance14(
            result["allowance"], degenerate=result["degenerate"], unavailable=result["unavailable"]
        )
    return {"source": "measured_noise", "value": value}


def _estimate(value: Mapping[str, Any] | None) -> Estimate | None:
    if value is None:
        return None
    return Estimate(
        value["point"], value["lower"], value["upper"], value["level"], value["unavailable"]
    )


def look_decision(
    comparison: Mapping[str, Any],
    *,
    final: bool,
    focus: Mapping[str, Any],
    causal: Mapping[str, Any] | None,
    tolerance: PublishedValue | None,
    family_version: int = 13,
) -> Decision:
    """Design §6.6 on the published values (G4, F1 plan R6), without the
    critical veto, which reads rows (plan choice 9)."""

    if family_version == 14:
        focus14 = Estimate14.from_record(focus)
        if comparison["mode"] == "observe":
            return decide_final_observe14(critical=False)
        if not final:
            return decide_interim14(focus14, margin=comparison["margin"], critical=False)
        return decide_final_statistical14(
            focus14,
            None if causal is None else Estimate14.from_record(causal),
            margin=comparison["margin"],
            tolerance=tolerance,
            critical=False,
        )
    focus_estimate = _estimate(focus)
    assert focus_estimate is not None
    if comparison["mode"] == "observe":
        return decide_final_observe(critical=False)
    if not final:
        return decide_interim(focus_estimate, critical=False)
    return decide_final_statistical(
        focus_estimate, _estimate(causal), tolerance=tolerance, critical=False
    )


def look_caveats(
    comparison: Mapping[str, Any],
    tolerance: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    *,
    family_version: int = 13,
    focus: Mapping[str, Any] | None = None,
    causal: Mapping[str, Any] | None = None,
) -> list[str]:
    """The sorted method caveats in force on one look (design §6.5, R25-R27).

    Only a family-14 statistical focus or causal flag establishes an exact
    contrast; an overall diagnostic or equal bounds alone do not.
    """

    statistical = comparison["mode"] == "statistical"
    caveats = (
        {INTERVALS_EMPIRICAL}
        if statistical
        else {OBSERVE_GATE_NOT_ENFORCED, UNCALIBRATED_POINT_ESTIMATES}
    )
    if comparison["parent_source"] == "baseline" and comparison["causal"]:
        caveats.add(CAUSAL_PARENT_NOT_CONTEMPORANEOUS)
    if (
        tolerance["source"] == "measured_noise"
        and tolerance["value"] is not None
        and (family_version != 14 or tolerance["value"] > 0)
    ):
        caveats.add(NOISE_ALLOWANCE_EMPIRICAL)
    if (
        family_version == 14
        and statistical
        and any(
            estimate is not None and estimate.get("exact") is True for estimate in (focus, causal)
        )
    ):
        caveats.add(EXACT_CONTRAST)
    verdict = calibration_verdict(comparison, result, family_version=family_version)
    if verdict is not None and verdict.caveat is not None:
        caveats.add(verdict.caveat)
    return sorted(caveats)


def _validate_noise_calibration_completed(p: dict[str, Any]) -> None:
    """Design §6.4's shape (plan choice 13); the audit recomputes D0 (F4)."""

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["attempt_id"])
    _positive(p["binding"], "binding is not a sequence")
    _durable_reference._refuse(
        journal_prefix_measurement(p["prefix"]) is not None,
        "the calibration prefix is a measurement journal prefix",
    )
    _durable_reference._refuse(type(p["degenerate"]) is bool, "degenerate is a boolean")
    unavailable = p["unavailable"]
    if unavailable is None:
        allowance = _number(p["allowance"], "allowance is not a finite number")
        # G13: primaries lie in [0, 1], so a measured allowance never exceeds 1.
        _durable_reference._refuse(0 <= allowance <= 1, "allowance is between 0 and 1")
        _durable_reference._sha(p["result_digest"], "result_digest is not a sha-256 digest")
    else:
        _durable_reference._refuse(
            unavailable == INSUFFICIENT_CALIBRATION_REPEATS, "unavailable is not in the registry"
        )
        _durable_reference._refuse(
            p["allowance"] is None and p["degenerate"] is False and p["result_digest"] is None,
            "an unavailable calibration has no allowance, draws or degeneracy",
        )


def _validate_estimate(value: object, name: str, *, family_version: int = 13) -> None:
    """Design §6.5's `{point, lower, upper, level, unavailable}` (F1 plan R3):
    an insufficient estimate has no bounds, a degenerate one equal bounds when
    two-sided, an observe one no bound and no level."""

    keys = {"point", "lower", "upper", "level", "unavailable"}
    gate14 = family_version == 14 and name != "overall"
    estimate = _keys(value, keys | {"exact"} if gate14 else keys)
    if not (estimate["point"] is None and estimate["unavailable"] == INSUFFICIENT_REPEATS):
        _number(estimate["point"], f"{name} point is not a finite number")  # E1 (a), 2026-10-04
    if estimate["point"] is None:
        # E1 (a): with no observed case there is nothing to bound either.
        _durable_reference._refuse(
            estimate["lower"] is None and estimate["upper"] is None,
            f"a null {name} point has no bounds",
        )
    for bound in ("lower", "upper"):
        if estimate[bound] is not None:
            _number(estimate[bound], f"{name} {bound} is not a finite number")
    if estimate["level"] is not None:
        _published_confidence(estimate["level"], f"{name} level is not a confidence")
    unavailable = estimate["unavailable"]
    _durable_reference._refuse(
        unavailable is None
        or (
            isinstance(unavailable, str)
            and unavailable
            in (
                ESTIMATE_UNAVAILABLE - {DEGENERATE_INTERVAL}
                if family_version == 14
                else ESTIMATE_UNAVAILABLE
            )
        ),
        f"{name} unavailable is not in the registry",
    )
    if gate14:
        try:
            Estimate14.from_record(estimate)
        except ValueError as exc:
            raise SchemaError(str(exc)) from exc
    if unavailable == NOT_REQUESTED_IN_OBSERVE:
        _durable_reference._refuse(
            estimate["lower"] is None and estimate["upper"] is None and estimate["level"] is None,
            f"an observe {name} has no bound or level",
        )
    elif unavailable == INSUFFICIENT_REPEATS:
        _durable_reference._refuse(
            estimate["lower"] is None and estimate["upper"] is None,
            f"an insufficient {name} has no bounds",
        )
    else:
        _durable_reference._refuse(
            estimate["lower"] is not None, f"an available {name} has a lower bound"
        )
        if estimate["upper"] is not None:
            _durable_reference._refuse(
                estimate["lower"] <= estimate["upper"], f"{name} bounds are inverted"
            )
            _durable_reference._refuse(
                unavailable is None or estimate["lower"] == estimate["upper"],
                f"a degenerate {name} has equal bounds",
            )


def _validate_comparison_look(p: dict[str, Any]) -> None:
    """Design §6.5's shape (plan choice 12 for `evidence`); the lifecycle
    re-derives the decision, the tolerance and the caveats."""

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["attempt_id"])
    _positive(p["comparison"], "comparison is not a sequence")
    _positive(p["look"], "look is not a positive integer")
    _durable_reference._refuse(type(p["final"]) is bool, "final is a boolean")
    prefixes = _keys(p["prefixes"], {"candidate", "parent"})
    for side in ("candidate", "parent"):
        _durable_reference._refuse(
            journal_prefix_measurement(prefixes[side]) is not None,
            f"the {side} prefix is not a measurement journal prefix",
        )
    family_version = 14
    _validate_estimate(p["focus"], "focus", family_version=family_version)
    for name in ("causal", "overall"):
        if p[name] is not None:
            _validate_estimate(p[name], name, family_version=family_version)
    tolerance = _keys(p["tolerance"], {"source", "value"})
    _durable_reference._refuse(
        tolerance["source"] in TOLERANCE_SOURCES, "tolerance source is not in the registry"
    )
    if tolerance["value"] is not None:
        _durable_reference._refuse(
            _number(tolerance["value"], "tolerance value is not a finite number") >= 0,
            "a tolerance is at least 0",
        )
    decision, reason = p["decision"], p["reason"]
    decisions = _FINAL_ATTEMPT_DECISIONS
    _durable_reference._refuse(
        isinstance(decision, str)
        and decision in decisions
        and (reason is None or isinstance(reason, str))
        and reason in decisions[decision],
        "decision and reason are not in the registry",
    )
    _durable_reference._refuse(
        p["would_pass_point"] is None or type(p["would_pass_point"]) is bool,
        "would_pass_point is a boolean or null",
    )
    caveats = p["caveats"]
    _durable_reference._refuse(
        isinstance(caveats, list)
        and all(isinstance(caveat, str) for caveat in caveats)
        and caveats == sorted(set(caveats))
        and set(caveats) <= (LOOK_CAVEATS14),
        "caveats are a sorted set from the registry",
    )
    _durable_reference._refuse(
        [reference["locator"] for reference in p["evidence"]]
        == [prefixes["candidate"], prefixes["parent"]]
        and all(reference["kind"] == "journal_prefix" for reference in p["evidence"]),
        "the evidence is the two cited prefixes",
    )


def _case_set(value: object, rule: str) -> list[int]:
    """Design §6.1: a case set is sorted, distinct `corpus_cases` indices."""

    _durable_reference._refuse(
        isinstance(value, list)
        and all(type(index) is int and index >= 0 for index in value)
        and value == sorted(set(value)),
        rule,
    )
    assert isinstance(value, list)
    return value


def _published_confidence(value: object, rule: str) -> None:
    try:
        confidence_fraction(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SchemaError(rule) from None


def _validate_tolerance_kind(value: object) -> None:
    tolerance = _keys(value, {"kind"}, {"value"})
    if tolerance["kind"] == "measured_noise":
        _durable_reference._refuse(
            set(tolerance) == {"kind"}, "tolerance is measured_noise or numeric"
        )
        return
    _durable_reference._refuse(
        tolerance["kind"] == "numeric" and set(tolerance) == {"kind", "value"},
        "tolerance is measured_noise or numeric",
    )
    _durable_reference._refuse(
        _number(tolerance["value"], "tolerance value is not a finite number") >= 0,
        "tolerance value is negative",
    )


def _validate_comparison_bound(p: dict[str, Any], *, family_version: int = 13) -> None:
    """Design §6.2's shape; the lifecycle re-derives every value."""

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["attempt_id"])
    _durable_reference._refuse(
        p["supersedes"] is None or (type(p["supersedes"]) is int and p["supersedes"] >= 1),
        "supersedes is null or a sequence",
    )
    _durable_reference._refuse(p["mode"] in GATE_MODES, "mode is not in the registry")
    if family_version == 14:
        _number(p["margin"], "margin is not a finite number")
    _durable_reference._refuse(
        type(p["epoch"]) is int and p["epoch"] >= 0, "epoch is not a non-negative integer"
    )
    _identifier(p["candidate_measurement_id"])
    _identifier(p["baseline_measurement_id"])
    _durable_reference._refuse(
        p["parent_source"] in PARENT_SOURCES, "parent_source is not in the registry"
    )
    _durable_reference._refuse(
        (p["parent_measurement_id"] is None) == (p["parent_source"] == "baseline"),
        "a fresh Parent source names its measurement",
    )
    if p["parent_measurement_id"] is not None:
        _identifier(p["parent_measurement_id"])
    focus = _case_set(p["focus"], "focus is not a sorted case set")
    causal = _case_set(p["causal"], "causal is not a sorted case set")
    _durable_reference._refuse(
        bool(focus) and not set(focus) & set(causal), "focus is non-empty and disjoint from causal"
    )
    retained = _keys(p["retained"], {"focus", "causal"})
    _durable_reference._refuse(
        set(_case_set(retained["focus"], "retained focus is not a sorted case set")) <= set(focus)
        and set(_case_set(retained["causal"], "retained causal is not a sorted case set"))
        <= set(causal),
        "retained cases are a subset of their scope",
    )
    if p["exclusions"] is not None:
        exclusions = _keys(p["exclusions"], {"prefix", "pairs_digest", "count"})
        _durable_reference._refuse(
            journal_prefix_measurement(exclusions["prefix"]) == p["baseline_measurement_id"],
            "exclusions cite the baseline's journal prefix",
        )
        _durable_reference._sha(
            exclusions["pairs_digest"], "exclusions pairs_digest is not a sha-256 digest"
        )
        _positive(exclusions["count"], "exclusions count is not a positive integer")
    repeats = _positive(p["repeats"], "repeats is not a positive integer")
    looks = p["looks"]
    _durable_reference._refuse(
        isinstance(looks, list)
        and bool(looks)
        and all(type(look) is int and look >= 1 for look in looks)
        and looks == sorted(set(looks))
        and looks[-1] == repeats,
        "looks increase to repeats",
    )
    if p["mode"] == "statistical":
        confidence = _keys(p["confidence"], {"focus", "causal"})
        for name in ("focus", "causal"):
            _published_confidence(
                confidence[name], "a confidence is a fraction strictly between 0 and 1"
            )
    else:
        _durable_reference._refuse(p["confidence"] is None, "observe binds no confidence")
    _validate_tolerance_kind(p["tolerance"])
    _durable_reference._refuse(p["method"] == method_object(), "method is not f13-bootstrap/1")


def _member_stability(final: Mapping[str, Any], stability: str | None) -> str:
    declared = final.get("stability")
    chosen = declared if stability is None and declared is not None else stability
    chosen = "unstable" if chosen is None else chosen
    _durable_reference._refuse(chosen in ("stable", "unstable"), "stability is not in the registry")
    _durable_reference._refuse(
        declared is None or declared == chosen, "member stability differs from the binding"
    )
    return str(chosen)


def _binding_members(final: Mapping[str, Any], stability: str | None) -> tuple[str, ...]:
    chosen = _member_stability(final, stability)
    finalists, members = final.get("finalists"), final.get("members")
    _durable_reference._refuse(
        isinstance(finalists, (list, tuple))
        and bool(finalists)
        and all(isinstance(item, Mapping) for item in finalists),
        "finalists are a nonempty ordered sequence",
    )
    assert isinstance(finalists, (list, tuple))
    _durable_reference._refuse(
        isinstance(members, (list, tuple))
        and len(members) == len(finalists) + int(chosen == "unstable")
        and all(isinstance(name, str) and bool(name) for name in members)
        and len(set(members)) == len(members),
        "final members follow the declared stability",
    )
    assert isinstance(members, (list, tuple))
    return tuple(members)


def _resolved_members(
    final: Mapping[str, Any], stability: str | None, members: Sequence[str] | None
) -> tuple[str, ...]:
    bound = _binding_members(final, stability)
    if members is None:
        return bound
    _durable_reference._refuse(
        not isinstance(members, str)
        and len(members) == len(bound)
        and all(
            isinstance(name, str)
            and (
                name == original
                or re.fullmatch(re.escape(original) + r"@e[1-9][0-9]*", name) is not None
            )
            for original, name in zip(bound, members, strict=True)
        ),
        "current members follow the immutable binding",
    )
    return tuple(members)


def champion_member(
    final: Mapping[str, Any],
    *,
    stability: str | None = None,
    members: Sequence[str] | None = None,
) -> str | None:
    """Optional fresh Champion ID; reduced stability precedes the legacy unstable default.

    An explicit stability must agree with a reduced binding. Supplying current
    members resolves epoch IDs without changing the immutable binding IDs.
    """
    resolved = _resolved_members(final, stability, members)
    return resolved[0] if _member_stability(final, stability) == "unstable" else None


def finalist_member(
    final: Mapping[str, Any],
    position: int,
    *,
    stability: str | None = None,
    members: Sequence[str] | None = None,
) -> str:
    """A finalist ID in binding order, optionally resolved through current members."""
    resolved = _resolved_members(final, stability, members)
    _durable_reference._refuse(
        type(position) is int and 0 <= position < len(final["finalists"]),
        "finalist position is outside the binding",
    )
    return resolved[position + int(_member_stability(final, stability) == "unstable")]


def member_identity(
    final: Mapping[str, Any],
    position: int,
    *,
    stability: str | None = None,
) -> tuple[str, str | None]:
    """A binding member's role and optional Attempt ID, independent of epoch IDs."""
    bound = _binding_members(final, stability)
    _durable_reference._refuse(
        type(position) is int and 0 <= position < len(bound),
        "member position is outside the binding",
    )
    fresh = _member_stability(final, stability) == "unstable"
    if fresh and position == 0:
        return "champion", None
    return "finalist", str(final["finalists"][position - int(fresh)]["attempt_id"])


def current_members(
    state: RoundState, *, final: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    """Current IDs for the latest or a historical binding, including rebounds.

    Family 14 retains the association after later bindings replace `cohort`;
    selection and audit can therefore read every final's settled members.
    """
    if final is None:
        final = _need(state.cohort, "the round has no bound cohort")
        current = _need(state.cohort_current, "a bound cohort has current members")
    else:
        position = next(
            (i for i, entry in enumerate(state.finals) if entry["seq"] == final["seq"]), None
        )
        _durable_reference._refuse(
            position is not None, "the final binding does not belong to this round"
        )
        assert position is not None
        current = state.finals_current[position]
    return _resolved_members(final, None, current)


def _immutable_binding(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_binding(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_binding(item) for item in value)
    return value


def _validate_final_bounds(p: Mapping[str, Any], *, inherited_evidence: bool = False) -> None:
    _durable_reference._refuse(
        len(canonical_json(p)) <= 16 * 1024, "final record exceeds the payload budget"
    )

    def visit(value: object) -> None:
        if isinstance(value, str):
            _durable_reference._refuse(
                len(value.encode("utf-8")) <= 2048, "final record string exceeds the byte budget"
            )
        elif isinstance(value, list):
            _durable_reference._refuse(
                len(value) <= 64, "final record list exceeds the item budget"
            )
            for item in value:
                visit(item)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                visit(key)
                visit(item)

    # The whole-payload byte budget still includes inherited evidence. Its
    # existing closed reference grammar keeps its own item/string limits.
    for key, value in p.items():
        if key != "evidence" or not inherited_evidence:
            visit(value)


def _validate_baseline_refresh_bound(p: dict[str, Any]) -> None:
    """H7 (F0 R42): the refresh's binding. Payload grammar only; that the counted
    rounds closed, and that `replaced` is the previous baseline, are the lifecycle's."""

    number = _positive(p["round"], "round is not a positive integer")
    _positive(
        p["baseline_refresh_after_rounds"],
        "baseline_refresh_after_rounds is not a positive integer",
    )
    _identifier(p["measurement_id"])
    _durable_reference._refuse(
        p["measurement_id"] == refresh_measurement_id_for(number),
        "refresh measurement ID is not the round's canonical refresh ID",
    )
    counted = _keys(p["counted_rounds"], {"first", "last"})
    first = _positive(counted["first"], "counted first round is not a positive integer")
    last = _positive(counted["last"], "counted last round is not a positive integer")
    _durable_reference._refuse(first <= last, "counted rounds are inverted")
    replaced = _keys(p["replaced"], {"measurement_id", "prefix_digest"})
    _identifier(replaced["measurement_id"])
    _durable_reference._sha(replaced["prefix_digest"])
    references = p["evidence"]
    _durable_reference._refuse(
        len(references) == 2, "refresh evidence cites the replaced prefix and one closure"
    )
    prefix, closure = references
    _durable_reference._refuse(
        prefix["kind"] == "journal_prefix"
        and journal_prefix_measurement(prefix["locator"]) == replaced["measurement_id"]
        and prefix["content_digest"] == replaced["prefix_digest"],
        "refresh evidence matches the replaced prefix identity",
    )
    _durable_reference._refuse(
        closure["kind"] == "journal_record", "refresh evidence cites a campaign record"
    )


def _validate_baseline_refresh_completed(p: dict[str, Any]) -> None:
    number = _positive(p["round"], "round is not a positive integer")
    name = p["measurement_id"]
    _identifier(name)
    base = base_measurement_id(name)
    _durable_reference._refuse(
        base == refresh_measurement_id_for(number)
        and (name == base or re.fullmatch(re.escape(base) + r"@e[1-9][0-9]*", name)),
        "refresh measurement ID is not the round's refresh ID or its epoch rebind",
    )
    references = p["evidence"]
    _durable_reference._refuse(len(references) == 1, "refresh completion cites one baseline prefix")
    reference = references[0]
    _durable_reference._refuse(
        reference["kind"] == "journal_prefix"
        and journal_prefix_measurement(reference["locator"]) == name,
        "refresh completion cites the refresh measurement's prefix",
    )


def _validate_connection_started(p: dict[str, Any]) -> None:
    """H10 (C§4.4, choice 7): the preflight's closed start. Payload grammar only;
    that it names the current epoch, Champion and seed is the lifecycle's."""

    _identifier(p["operation_id"])
    _identifier(p["invocation_id"])
    _durable_reference._sha(p["binding_digest"], "binding_digest is not a sha-256 digest")
    _durable_reference._refuse(
        type(p["epoch"]) is int and p["epoch"] >= 0, "epoch is not a non-negative integer"
    )
    _identifier(p["case_id"])
    # C§4.2: the first version probes the first train case.
    _durable_reference._refuse(
        type(p["case_index"]) is int and p["case_index"] == 0,
        "the preflight probes the first train case",
    )
    for name in (
        "target",
        "environment",
        "scorer",
        "metric_contract",
        "candidate_digest",
        "dependency_digest",
    ):
        _durable_reference._sha(p[name], f"{name} is not a sha-256 digest")
    _identifier(p["primary_metric"])
    seed = p["probe_seed"]
    _durable_reference._refuse(
        type(seed) is int and 0 <= seed <= _MAX_PROBE_SEED, "probe_seed is not a 31-bit seed"
    )


def _validate_connection_evidence(value: object, *, required: bool) -> None:
    """At most one `journal_prefix` of the probe's own S1 journal (M9, choice 7):
    `connection/<run_id>/events.jsonl` from byte 0, never empty. Exactly one
    when `required`; none only when no probe journal was born. That `run_id`
    is the terminal's operation is the lifecycle's."""

    _durable_reference._refuse(
        isinstance(value, list) and len(value) <= 1 and (bool(value) or not required),
        "connection evidence cites the probe journal once",
    )
    assert isinstance(value, list)
    for item in value:
        reference = _durable_reference.DurableReference.from_payload(item)
        match = _durable_reference._LOCATOR.fullmatch(reference.locator)
        _durable_reference._refuse(
            reference.kind == "journal_prefix"
            and match is not None
            and match["file"] == f"{CONNECTION_DIR}/{reference.run_id}/{EVENTS_FILE}"
            and int(match["start"]) == 0
            and int(match["end"]) > 0,
            "connection evidence is not the probe journal's prefix",
        )


def _validate_connection_terminal(event: str, p: dict[str, Any]) -> None:
    """H10 (C§4.3, C§4.4, choice 7): one preflight terminal's closed payload.

    - `connection_preflight_finished`: `compatible`, `released`, a trace digest
      and the probe journal's prefix.
    - `connection_preflight_failed`: one of the three failure classes. The
      C§3.2 `detail_code` and both paths are present exactly for
      `connection_incompatible`; the other two carry only a bounded
      `cause_code` (`ProbeOutcome`'s rules, reused). A refused release is
      exactly `connection_preflight_unavailable / cleanup_failed` with
      `cleanup: failed`; only that recovery terminal may lack a trace (a
      crash before the summary was written, C§4.4).
    - `connection_preflight_interrupted`: a closed reason; the trace digest is
      `null` and the evidence empty when nothing was written.
    """

    _durable_reference._refuse(p["cleanup"] in CLEANUP_STATES, "cleanup is not in the registry")
    # A compatible or inconclusive outcome needs a completed rollout, and an
    # incompatible one a completed rollout or an attested instance whose
    # description held a run secret (#287): each a born probe journal. Only an
    # unavailable probe (or an interruption) may have none (fix round 1, minor 3).
    _validate_connection_evidence(
        p["evidence"],
        required=event == CONNECTION_FINISHED_EVENT
        or (
            event == CONNECTION_FAILED_EVENT
            and p.get("status") != "connection_preflight_unavailable"
        ),
    )
    if event == CONNECTION_FINISHED_EVENT:
        _durable_reference._refuse(
            p["status"] == "compatible", "a finished preflight is compatible"
        )
        _durable_reference._refuse(
            p["cleanup"] == "released", "a compatible preflight released its instance"
        )
        _durable_reference._sha(p["trace_digest"], "trace_digest is not a sha-256 digest")
        return
    if event == CONNECTION_INTERRUPTED_EVENT:
        _durable_reference._refuse(
            p["reason"] in CONNECTION_INTERRUPTION_REASONS,
            "interruption reason is not in the registry",
        )
        if p["trace_digest"] is not None:
            _durable_reference._sha(p["trace_digest"], "trace_digest is not a sha-256 digest")
        return
    status = p["status"]
    _durable_reference._refuse(
        isinstance(status, str) and status in CONNECTION_SUSPENSIONS,
        "status is not a connection failure class",
    )
    _durable_reference._refuse(
        (p["detail_code"] is not None) == (status == "connection_incompatible"),
        "detail_code is present exactly for connection_incompatible",
    )
    _durable_reference._refuse(
        status != "connection_incompatible" or p["detail_code"] in DETAIL_CODES,
        "detail_code is not in the registry",
    )
    try:
        ProbeOutcome(
            status, p["detail_code"], p["manifest_path"], p["observed_path"], p["cause_code"]
        )
    except (TypeError, ValueError):
        raise SchemaError("the connection failure detail is not well formed") from None
    unsettled = p["cause_code"] == CLEANUP_FAILED_CAUSE
    _durable_reference._refuse(
        (p["cleanup"] == "failed") == unsettled
        and (not unsettled or status == "connection_preflight_unavailable"),
        "a failed cleanup is the cleanup_failed cause",
    )
    if p["trace_digest"] is not None or not unsettled:
        _durable_reference._sha(p["trace_digest"], "trace_digest is not a sha-256 digest")


def _validate_final_id(value: object, number: int, *, allow_rebound: bool = False) -> re.Match[str]:
    _durable_reference._refuse(isinstance(value, str), "final measurement ID is not a string")
    if allow_rebound:
        name = str(value)
        base = base_measurement_id(name)
        _durable_reference._refuse(
            name == base or re.fullmatch(re.escape(base) + r"@e[1-9][0-9]*", name),
            "final measurement ID has a noncanonical condition epoch",
        )
        value = base
    match = re.fullmatch(r"m-r([0-9]+)-final-([0-9]+)-(champion|a([0-9]+))", str(value))
    _durable_reference._refuse(match is not None, "final measurement ID has no ordinal")
    assert match is not None
    _durable_reference._refuse(
        int(match[1]) == number and int(match[2]) > 0 and (match[4] is None or int(match[4]) > 0),
        "final measurement ID has nonpositive or mismatched components",
    )
    index = None if match[4] is None else int(match[4])
    _durable_reference._refuse(
        value == final_measurement_id_for(number, int(match[2]), index),
        "final measurement ID is not canonical",
    )
    return match


def _validate_final_member_ids(p: Mapping[str, Any]) -> None:
    matches = [_validate_final_id(member, p["round"]) for member in p["members"]]
    _durable_reference._refuse(
        len({int(match[2]) for match in matches}) == 1, "final members share one binding ordinal"
    )
    attempts = p["finalists"]
    offset = len(matches) - len(attempts)
    _durable_reference._refuse(offset in (0, 1), "final members have an optional Champion")
    if offset:
        _durable_reference._refuse(
            matches[0][3] == "champion", "the first final member is the Champion"
        )
    indices: list[int] = []
    for match, entry in zip(matches[offset:], attempts, strict=True):
        attempt = re.fullmatch(r"r([0-9]+)-a([0-9]+)", entry["attempt_id"])
        _durable_reference._refuse(attempt is not None, "a finalist has a round Attempt ID")
        assert attempt is not None
        indices.append(int(attempt[2]))
        _durable_reference._refuse(
            int(attempt[1]) == p["round"]
            and int(attempt[2]) > 0
            and match[4] is not None
            and int(match[4]) == int(attempt[2]),
            "final members follow their finalist Attempt indices",
        )

    _durable_reference._refuse(indices == sorted(indices), "finalists are in Attempt index order")


def _validate_finalists_bound(p: dict[str, Any]) -> None:
    """Design §6.7's shape; the lifecycle re-derives every value.

    `order` is only typed here: its one admitted literal is the lifecycle's
    refusal, so the refusal reaches the writer as the cohort's rule rather
    than as the envelope's "invalid journal record".
    """

    _positive(p["round"], "round is not a positive integer")
    champion = _keys(p["champion"], {"version", "digest"})
    _identifier(champion["version"])
    _durable_reference._sha(champion["digest"], "champion digest is not a sha-256 digest")
    finalists = p["finalists"]
    _durable_reference._refuse(isinstance(finalists, list), "finalists is not a list")
    for value in finalists:
        entry = _keys(value, {"attempt_id", "candidate_digest", "level"})
        _identifier(entry["attempt_id"])
        _durable_reference._sha(
            entry["candidate_digest"], "a finalist's candidate_digest is not a sha-256 digest"
        )
        if entry["level"] is not None:
            _published_confidence(entry["level"], "a finalist's level is null or a fraction")
    members = p["members"]
    _durable_reference._refuse(
        isinstance(members, list)
        and (len(members) in (len(finalists), len(finalists) + 1))
        and len(set(map(str, members))) == len(members),
        "members names the Champion and each finalist once",
    )
    for member in members:
        _identifier(member)
    attempts = [entry["attempt_id"] for entry in finalists]
    _durable_reference._refuse(
        len(attempts) == len(set(attempts)), "finalists name unique Attempts"
    )
    _durable_reference._refuse(bool(finalists), "a final binding has a finalist")
    _validate_final_member_ids(p)
    _durable_reference._refuse(p["global"] in GLOBAL_MODES, "global is not in the registry")
    _durable_reference._refuse(p["mode"] in GATE_MODES, "mode is not in the registry")
    repeats = _positive(p["repeats"], "repeats is not a positive integer")
    looks = p["looks"]
    _durable_reference._refuse(
        isinstance(looks, list)
        and bool(looks)
        and all(type(look) is int and look >= 1 for look in looks)
        and looks == sorted(set(looks))
        and looks[-1] == repeats,
        "looks increase to repeats",
    )
    _durable_reference._refuse(
        p["seed_mode"] in ("exact", "unpaired", "none"), "seed_mode is not in the registry"
    )
    _durable_reference._refuse(
        type(p["epoch"]) is int and p["epoch"] >= 0, "epoch is not a non-negative integer"
    )
    _durable_reference._refuse(type(p["order"]) is str, "order is not a string")


def _validate_cohort_measurement_bound(p: dict[str, Any]) -> None:
    """Design §6.8's shape. Which member it is, and so whether it names an
    Attempt, is the lifecycle's rule (`_cohort_measurement_bound`)."""

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["measurement_id"])
    _durable_reference._refuse(p["role"] in COHORT_ROLES, "role is not in the registry")
    if p["attempt_id"] is not None:
        _identifier(p["attempt_id"])
    _durable_reference._sha(p["candidate_digest"])
    _durable_reference._sha(p["identity_digest"])
    _durable_reference._refuse(
        p["stability"] in ("stable", "unstable"), "stability is not in the registry"
    )
    _durable_reference._refuse(
        p["round_stop"] in ("all_attempts", "first_accepted"),
        "round_stop is not in the registry",
    )
    stable = p["stability"] == "stable"
    _durable_reference._refuse(
        p["champion_side"] == ("stored" if stable else "fresh"),
        "champion_side follows stability",
    )
    _durable_reference._refuse(
        ("stored_champion" in p) == stable, "stored_champion is present exactly under stable"
    )
    _durable_reference._refuse(
        ("evidence" in p) == stable, "stored evidence is present exactly under stable"
    )
    if stable:
        stored = _keys(p["stored_champion"], {"measurement_id", "prefix_digest"})
        _identifier(stored["measurement_id"])
        _durable_reference._sha(stored["prefix_digest"])
        references = p["evidence"]
        _durable_reference._refuse(
            len(references) == 1, "stored evidence cites one Champion prefix"
        )
        reference = references[0]
        _durable_reference._refuse(
            reference["kind"] == "journal_prefix"
            and journal_prefix_measurement(reference["locator"]) == stored["measurement_id"]
            and reference["content_digest"] == stored["prefix_digest"],
            "stored evidence matches the Champion prefix identity",
        )
        _durable_reference._refuse(p["role"] == "finalist", "stable binds no fresh Champion member")
    _durable_reference._refuse(
        (p["attempt_id"] is None) == (p["role"] == "champion"),
        "only a finalist member names its attempt",
    )
    _validate_final_id(p["measurement_id"], p["round"])


def _validate_cohort_measurement_completed(p: dict[str, Any]) -> None:
    """Design §6.9's shape. Whose journal `prefix` names, and D7's pairing of
    `complete`, `point` and `missing`, are the lifecycle's rules
    (`_cohort_measurement_completed`), so they reach the writer by their text."""

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["measurement_id"])
    _validate_final_id(p["measurement_id"], p["round"], allow_rebound=True)
    _durable_reference._refuse(
        journal_prefix_measurement(p["prefix"]) is not None,
        "prefix is not a measurement journal prefix",
    )
    _durable_reference._refuse(type(p["complete"]) is bool, "complete is not a boolean")
    _durable_reference._refuse(type(p["critical"]) is bool, "critical is not a boolean")
    _durable_reference._refuse(
        type(p["missing"]) is int and p["missing"] >= 0, "missing is not a non-negative integer"
    )
    if p["point"] is not None:
        point = _number(p["point"], "point is null or a finite number")
        _durable_reference._refuse(0 <= point <= 1, "point lies in [0, 1]")


#: Design §6.10: why a comparison's bound is null, or null when it is not. A
#: complete statistical comparison always has its bound (plan choice 4).
_GLOBAL_UNAVAILABLE = frozenset({None, BOUND_NOT_REQUESTED_IN_OBSERVE, "cohort_incomplete"})


def _validate_final_removals(
    p: Mapping[str, Any], corpus_cases: Sequence[Mapping[str, Any]], *, repeats: int | None = None
) -> None:
    """Check listed facts against the bound corpus/horizon, without reading sibling rows."""
    position = {str(case["case_id"]): index for index, case in enumerate(corpus_cases)}
    cases = p["removed_cases"]
    pairs = p["removed_pairs"]
    if isinstance(cases, list):
        _durable_reference._refuse(
            all(case in position for case in cases), "final removals name corpus cases"
        )
        _durable_reference._refuse(len(set(cases)) == len(cases), "removed cases are unique")
        _durable_reference._refuse(
            cases == sorted(cases, key=lambda case: position[case]),
            "removed cases follow corpus order",
        )
    if isinstance(pairs, list):
        _durable_reference._refuse(
            all(pair[0] in position for pair in pairs), "final removals name corpus cases"
        )
        _durable_reference._refuse(
            len({tuple(pair) for pair in pairs}) == len(pairs), "removed pairs are unique"
        )
        _durable_reference._refuse(
            all(type(pair[1]) is int and pair[1] >= 1 for pair in pairs),
            "removed repeat IDs are positive integers",
        )
        if repeats is not None:
            _durable_reference._refuse(
                all(pair[1] <= repeats for pair in pairs),
                "removed repeat IDs lie in the bound horizon",
            )
        _durable_reference._refuse(
            pairs == sorted(pairs, key=lambda pair: (position[pair[0]], pair[1])),
            "removed pairs follow corpus and repeat order",
        )


def _validate_global_comparison_completed(p: dict[str, Any]) -> None:
    """Design §6.10's shape; the lifecycle re-derives the verdict, the D7
    state and the level (`_global_comparison_completed`)."""

    _positive(p["round"], "round is not a positive integer")
    for key in ("attempt_id", "finalist_measurement_id", "champion_measurement_id"):
        _identifier(p[key])
    for key in ("point", "lower"):
        if p[key] is not None:
            _number(p[key], f"{key} is null or a finite number")
    if p["level"] is not None:
        _published_confidence(p["level"], "level is null or a fraction")
    _durable_reference._refuse(
        p["unavailable"] in ({None} | UNAVAILABLE14),
        "unavailable is not in the registry",
    )
    _durable_reference._refuse(
        p["pairing"] in ("exact", "unpaired"), "pairing is not in the registry"
    )
    vocabulary = campaign_vocabulary_for(14)
    accepted = p[vocabulary.contrast_key]
    reasons = GLOBAL_REASONS14
    _durable_reference._refuse(
        type(accepted) is bool, f"{vocabulary.contrast_key} is not a boolean"
    )
    _durable_reference._refuse(
        p["reason"] in reasons[accepted], "reason does not match eligibility"
    )
    _validate_final_id(p["finalist_measurement_id"], p["round"], allow_rebound=True)
    if p["champion_side"] == "fresh":
        _validate_final_id(p["champion_measurement_id"], p["round"], allow_rebound=True)
    _durable_reference._refuse(
        p["champion_side"] in ("fresh", "stored"), "champion_side is not in the registry"
    )
    _durable_reference._refuse(
        p["exact"] is None or type(p["exact"]) is bool, "exact is a boolean or null"
    )
    _durable_reference._refuse(
        (p["exact"] is None) == (p["level"] is None), "exact is null exactly without a level"
    )
    if p["exact"]:
        _durable_reference._refuse(
            p["unavailable"] is None
            and p["point"] is not None
            and p["lower"] is not None
            and Fraction(str(p["lower"])) == Fraction(str(p["point"])),
            "an exact global lower equals its point",
        )
    pairs = p["removed_pairs"]
    cases = p["removed_cases"]
    for key in ("removed_pairs", "removed_cases"):
        values = p[key]
        if isinstance(values, Mapping):
            summary = _keys(values, {"count", "digest"})
            _positive(summary["count"], "a removal summary count is a positive integer")
            _durable_reference._sha(summary["digest"])
        else:
            _durable_reference._refuse(
                isinstance(values, list), f"{key} is a list or a closed summary"
            )
    if isinstance(pairs, list):
        for pair in pairs:
            _durable_reference._refuse(
                isinstance(pair, list) and len(pair) == 2,
                "a removed pair has case and repeat",
            )
            _identifier(pair[0])
            _positive(pair[1], "a removed repeat ID is a positive integer")
        _durable_reference._refuse(
            len({tuple(pair) for pair in pairs}) == len(pairs), "removed pairs are unique"
        )
    if isinstance(cases, list):
        for case in cases:
            _identifier(case)
        _durable_reference._refuse(len(set(cases)) == len(cases), "removed cases are unique")
    _durable_reference._refuse(
        p["champion_side"] != "fresh" or (not pairs and not cases),
        "fresh comparisons remove no pairs or cases",
    )
    # M2 separates an incomplete fresh finalist (checked against its
    # completion by the lifecycle) from an unavailable stored contrast.
    if p["point"] is None:
        _durable_reference._refuse(
            p["lower"] is None
            and p["unavailable"] == "final_incomplete"
            and not accepted
            and p["reason"] == "final_incomplete"
            and p["exact"] is not True,
            "an unavailable final point has no bound and is not accepted",
        )
    elif p["level"] is None:
        _durable_reference._refuse(
            p["lower"] is None and p["unavailable"] == BOUND_NOT_REQUESTED_IN_OBSERVE,
            "an observe comparison requests no bound",
        )
    elif p["lower"] is None:
        _durable_reference._refuse(
            p["unavailable"] == "insufficient_repeats" and p["exact"] is False,
            "an unavailable statistical bound keeps its point and declared level",
        )
    else:
        _durable_reference._refuse(
            p["unavailable"] is None, "an available final bound has no unavailable reason"
        )
    if p["reason"] == "final_incomplete":
        _durable_reference._refuse(
            p["point"] is None or p["unavailable"] == "insufficient_repeats",
            "final_incomplete requires unavailable evidence",
        )


def _validate_confidence_selection(p: dict[str, Any]) -> None:
    """Design §6.11's shape; the lifecycle re-derives every value
    (`_check_cohort_selection`)."""

    _positive(p["round"], "round is not a positive integer")
    named = [p["attempt_id"], p["candidate_digest"], p["dependency_digest"]]
    _durable_reference._refuse(
        all(value is None for value in named) or all(value is not None for value in named),
        "the winner fields are present together",
    )
    if p["attempt_id"] is not None:
        _identifier(p["attempt_id"])
        _durable_reference._sha(p["candidate_digest"])
        _durable_reference._sha(p["dependency_digest"])
    if p["measurement_id"] is not None:
        _identifier(p["measurement_id"])
    _durable_reference._refuse(p["global"] in GLOBAL_MODES, "global is not in the registry")
    vocabulary = campaign_vocabulary_for(14)
    reasons = (SELECTION_REASONS - {"no_eligible_finalist"}) | {
        vocabulary.selection_retained_reason
    }
    global_reasons = GLOBAL_REASONS14
    _durable_reference._refuse(p["reason"] in reasons, "reason is not in the registry")
    _durable_reference._refuse(isinstance(p["ranking"], list), "ranking must be a list")
    for entry in p["ranking"]:
        row = _keys(entry, {"attempt_id", "score", vocabulary.ranking_key, "reason"})
        _identifier(row["attempt_id"])
        if row["score"] is not None:
            score = _number(row["score"], "a ranking score is null or a finite number")
            _durable_reference._refuse(0 <= score <= 1, "a ranking score lies in [0, 1]")
        _durable_reference._refuse(
            type(row[vocabulary.ranking_key]) is bool, "a ranking row's eligible is not a boolean"
        )
        _durable_reference._refuse(
            row["reason"] in global_reasons[row[vocabulary.ranking_key]],
            "a ranking reason does not match its eligibility",
        )


def _validate_noise_calibration_bound(p: dict[str, Any]) -> None:
    """Design §6.3's shape; the lifecycle re-derives the values."""

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["attempt_id"])
    _positive(p["comparison"], "comparison is not a sequence")
    _durable_reference._refuse(
        p["supersedes"] is None or (type(p["supersedes"]) is int and p["supersedes"] >= 1),
        "supersedes is null or a sequence",
    )
    _identifier(p["source_measurement_id"])
    _durable_reference._refuse(p["source"] in PARENT_SOURCES, "source is not in the registry")
    if p["source"] == "baseline":
        _durable_reference._refuse(
            journal_prefix_measurement(p["prefix"]) == p["source_measurement_id"],
            "a baseline calibration cites the baseline's journal prefix",
        )
    else:
        _durable_reference._refuse(
            p["prefix"] is None, "a fresh Parent's prefix is cited at completion"
        )
    sizes = _keys(p["sizes"], {"first", "second"})
    _durable_reference._refuse(
        all(type(sizes[name]) is int and sizes[name] >= 1 for name in ("first", "second")),
        "sizes are positive integers",
    )
    _durable_reference._refuse(p["method"] == method_object(), "method is not f13-bootstrap/1")


#: Families 13 and 14: the records that cite a measurement journal
#: outside `evidence`, as key paths to the cited `journal_prefix` locator. A
#: calibration's `prefix` (null for a fresh source at binding) and a
#: comparison's `exclusions.prefix` (null without exclusions), plus each
#: cohort/final member completion's `prefix`. A
#: `comparison_look_evaluated`'s `prefixes` are its evidence (plan choice 12),
#: so they need no entry. No record of families 3 to 12 has one.
_PAYLOAD_CITATIONS: Mapping[str, tuple[tuple[str, ...], ...]] = MappingProxyType(
    {
        "comparison_bound": (("exclusions", "prefix"),),
        "noise_calibration_bound": (("prefix",),),
        "noise_calibration_completed": (("prefix",),),
        campaign_vocabulary_for(14).measurement_completed: (("prefix",),),
    }
)


def journal_citations(
    event: str, p: Mapping[str, Any], *, run_id: str | None = None
) -> list[tuple[object, object]]:
    """`(kind, locator)` of every journal citation a campaign record makes.

    The one place that knows where a record cites a journal: each durable
    reference of an `evidence` list, a suspension's `evidence` `prefix` (its
    `stage_record` drift prefix) and, in families 13 and 14, the key paths
    `_PAYLOAD_CITATIONS` names; the last two are `journal_prefix` locators.
    The #109 floor (`_cited_journal_ends`) and the audit's window placement
    (`campaign_projections._redispatch_windows`) both read it. Nothing is
    validated here: a malformed entry is passed on for the caller to skip.

    With `run_id` (the campaign's), a durable reference to a measurement
    journal is kept only when it names this run: its `run_id` is the campaign's
    or the measurement id its locator's `measurements/<id>/events.jsonl` names
    (the journal's own S1 run, as `Measurement.prefix_reference` writes it).
    A bare `prefix` locator carries no run and always names this run's journal.
    """

    evidence = p.get("evidence")
    citations: list[tuple[object, object]] = []
    if isinstance(evidence, list):
        citations = [
            (ref.get("kind"), ref.get("locator"))
            for ref in evidence
            if isinstance(ref, Mapping) and (run_id is None or _names_this_run(ref, run_id))
        ]
    elif isinstance(evidence, Mapping) and "prefix" in evidence:
        citations = [("journal_prefix", evidence["prefix"])]
    for path in _PAYLOAD_CITATIONS.get(event, ()):
        value: object = p
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if value is not None:
            citations.append(("journal_prefix", value))
    if any(name in p for name in ("verification", "source_ref", "candidate_source")) or event in {
        "comparison_bound",
        "comparison_completed",
        "gate_measurement_completed",
        "candidate_gate_sealed",
        "candidate_completion_completed",
        "final_measurement_completed",
        "global_comparison_completed",
        "selection_decided",
    }:
        for name in (
            "candidate",
            "reference",
            "champion",
            "source",
            "selected_evidence",
            "next_champion",
        ):
            summary = p.get(name)
            ref = summary.get("prefix") if isinstance(summary, Mapping) else None
            if isinstance(ref, Mapping) and (run_id is None or _names_this_run(ref, run_id)):
                citations.append((ref.get("kind"), ref.get("locator")))
    return citations


def _names_this_run(reference: Mapping[str, Any], run_id: str) -> bool:
    """Does a durable reference to a measurement journal name this run?

    Its `run_id` is the campaign's own or the cited measurement journal's own
    (the id its locator names). A reference to any other file (the campaign
    journal, a probe journal) is not a measurement citation and is kept for
    the caller to skip.
    """

    locator = reference.get("locator")
    match = _durable_reference._LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    named = _REFUSAL_PREFIX_FILE.fullmatch(match["file"]) if match is not None else None
    return named is None or reference.get("run_id") in (run_id, named["measurement_id"])


def _cited_journal_ends(
    event: str, p: Mapping[str, Any], *, run_id: str | None = None
) -> dict[str, int]:
    """`measurement_id -> end`: how far a record cites each measurement journal.

    #109 (a28 spec §3.4, ADR-0019 as amended): the lower bound a
    `stage_redispatched`'s prefix must reach. Every record that cites a
    measurement journal does it where `journal_citations` reads: in
    `evidence` -- a durable-reference list (`instance_attested`,
    `indeterminate_observed`, `pairs_excluded`, `repairs_dispatched`,
    `stage_redispatched`, a look, a completion ...) or a suspension's
    `stage_record` `prefix` -- or, in families 13 and 14, at a calibration's
    or cohort/final member completion's `prefix` or a comparison's
    `exclusions.prefix` (codex F3 branch review round 2).
    A record range (`bytes=<start>:<end>`) counts by its end, which the
    journal reached too. Read from the locators alone, never the bytes.

    The lifecycle passes its `run_id`, so a reference naming another run --
    neither this campaign nor the measurement journal its locator names --
    raises no floor of this run's (H10 review r1 item 3; unreachable while family 14
    refuses `linked_from`).
    """

    ends: dict[str, int] = {}
    for _kind, locator in journal_citations(event, p, run_id=run_id):
        match = _durable_reference._LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
        named = _REFUSAL_PREFIX_FILE.fullmatch(match["file"]) if match is not None else None
        if match is not None and named is not None:
            name = str(named["measurement_id"])
            ends[name] = max(int(match["end"]), ends.get(name, 0))
    return ends


def _validate_cohort_rows(e: Mapping[str, Any], reason: str, *, family_version: int = 13) -> None:
    """The bounded rows of a `cohort_incomplete` suspension (design §6.13).

    `count` is the whole set of gaps (each row carries its latest try's
    `retry_ordinal`), `rows` its first `MAX_FAULT_ROWS` in canonical order and
    `truncated` says whether that is all of them. A complete list must recompute to `rows_digest`; a truncated
    one, and every row's owner, are the audit's to recompute from the member
    journals. Modelled on `_validate_fault_rows`.
    """

    count = e["count"]
    _durable_reference._refuse(
        type(count) is int and count >= 1, f"a {reason} suspension names at least one pair"
    )
    rows = e["rows"]
    _durable_reference._refuse(isinstance(rows, list), "rows must be a list")
    _durable_reference._refuse(
        len(rows) <= MAX_FAULT_ROWS,
        member_diagnostic("cohort_rows lists at most 256 rows", family_version=family_version),
    )
    pairs: set[tuple[str, str, str]] = set()
    for entry in rows:
        row = _keys(entry, _COHORT_ROW_KEYS)
        for name in ("measurement_id", "case_id", "sample_id"):
            _identifier(row[name])
        _durable_reference._refuse(
            row["owner"] in OWNERS,
            member_diagnostic(
                "a cohort row's owner is not a threshold owner", family_version=family_version
            ),
        )
        _durable_reference._refuse(
            type(row["retry_ordinal"]) is int and row["retry_ordinal"] >= 0,
            member_diagnostic(
                "a cohort row's retry_ordinal is a non-negative integer",
                family_version=family_version,
            ),
        )
        pair = (row["measurement_id"], row["case_id"], row["sample_id"])
        _durable_reference._refuse(pair not in pairs, "rows repeats a pair")
        pairs.add(pair)
    _durable_reference._refuse(
        len(rows) == min(count, MAX_FAULT_ROWS),
        member_diagnostic("cohort_rows lists the first 256 rows", family_version=family_version),
    )
    _durable_reference._refuse(
        type(e["truncated"]) is bool and e["truncated"] == (count > len(rows)),
        "truncated says whether the list is complete",
    )
    _durable_reference._sha(e["rows_digest"], "rows_digest is not a sha-256 digest")
    if not e["truncated"]:
        _durable_reference._refuse(
            cohort_rows_digest(rows) == e["rows_digest"],
            member_diagnostic(
                "the cohort rows digest covers its rows", family_version=family_version
            ),
        )


def _validate_champion_rows(e: Mapping[str, Any], reason: str) -> None:
    """The bounded rows of a `champion_incomplete` suspension (#194): the cohort
    rows' bounding, plus the one measurement every row belongs to."""

    _identifier(e["measurement_id"])
    _validate_cohort_rows(
        {k: v for k, v in e.items() if k != "measurement_id"},
        reason,
    )
    for row in e["rows"]:
        _durable_reference._refuse(
            row["measurement_id"] == e["measurement_id"],
            "a champion row names another measurement",
        )


def _validate_fault_rows(e: Mapping[str, Any]) -> None:
    """The bounded fault rows of a suspension: the `pairs_excluded` bounding.

    `count` is the stage's whole set, `rows` its first `MAX_FAULT_ROWS` in
    schedule order and `truncated` says whether that is all of them. A complete
    list must recompute to `rows_digest`; a truncated one is recomputed from
    the measurement prefix by the audit.
    """

    count = _positive(e["count"], "count is not a positive integer")
    rows = e["rows"]
    _durable_reference._refuse(isinstance(rows, list), "rows must be a list")
    _durable_reference._refuse(len(rows) <= MAX_FAULT_ROWS, "fault_rows lists at most 256 rows")
    keys: list[tuple[str, str, str, int, str]] = []
    tries: set[tuple[str, str, str, int]] = set()
    for entry in rows:
        row = _keys(entry, _FAULT_ROW_KEYS)
        for name in ("measurement_id", "case_id", "sample_id"):
            _identifier(row[name])
        _durable_reference._refuse(
            type(row["retry_ordinal"]) is int and row["retry_ordinal"] >= 0,
            "retry_ordinal is not a non-negative integer",
        )
        _durable_reference._refuse(
            row["reason"] in FAULT_ROW_REASONS, "fault row reason is not a fault"
        )
        if row["code"] is not None:
            _identifier(row["code"])
        key = (
            row["measurement_id"],
            row["case_id"],
            row["sample_id"],
            row["retry_ordinal"],
            row["reason"],
        )
        _durable_reference._refuse(key[:4] not in tries, "rows repeats a try")
        tries.add(key[:4])
        keys.append(key)
    _durable_reference._refuse(
        len(rows) == min(count, MAX_FAULT_ROWS), "fault_rows lists the first 256 fault rows"
    )
    _durable_reference._refuse(
        type(e["truncated"]) is bool and e["truncated"] == (count > len(rows)),
        "truncated says whether the list is complete",
    )
    _durable_reference._sha(e["rows_digest"], "rows_digest is not a sha-256 digest")
    if not e["truncated"]:
        _durable_reference._refuse(
            fault_rows_digest(keys) == e["rows_digest"],
            "rows_digest does not recompute over the rows",
        )


def pairs_digest(pairs: Sequence[tuple[str, str]]) -> str:
    """The `pairs_digest` of a set of excluded `(case_id, sample_id)` pairs.

    SHA-256 over the canonical JSON of the sorted `[case_id, sample_id]` list, so
    the digest is independent of schedule order (spec §4.2). The executor and the
    audit both compute it over the set they recompute from the measurement prefix.
    """

    return hashlib.sha256(canonical_json(sorted([list(pair) for pair in pairs]))).hexdigest()


def _validate_pairs_excluded(p: dict[str, Any]) -> None:
    """The bounded summary of one stage's excluded pairs (spec §4.2).

    The record carries the exact `count`, `by_owner` and `pairs_digest` of the
    stage's whole set and lists at most its first `MAX_EXCLUDED_PAIRS` pairs. The
    set itself is recomputed from the cited measurement prefix by the executor
    and the audit; this shape only proves the summary is self-consistent.
    """

    _positive(p["round"], "round is not a positive integer")
    _identifier(p["measurement_id"])
    _positive(p["stage"], "stage is not a positive integer")
    count = _positive(p["count"], "count is not a positive integer")
    pairs = p["pairs"]
    _durable_reference._refuse(isinstance(pairs, list), "pairs must be a list")
    _durable_reference._refuse(
        len(pairs) <= MAX_EXCLUDED_PAIRS, "pairs_excluded lists at most 256 pairs"
    )
    seen: set[tuple[str, str]] = set()
    listed: dict[str, int] = {}
    for entry in pairs:
        row = _keys(entry, {"case_id", "sample_id", "owner", "reason", "tries"})
        _identifier(row["case_id"])
        _identifier(row["sample_id"])
        _durable_reference._refuse(row["owner"] in OWNERS, "pair owner is not in the registry")
        _durable_reference._refuse(
            row["reason"] in SAMPLE_REASONS, "pair reason is not a sample reason"
        )
        _positive(row["tries"], "tries is not a positive integer")
        key = (row["case_id"], row["sample_id"])
        _durable_reference._refuse(key not in seen, "pairs repeats a pair")
        seen.add(key)
        listed[row["owner"]] = listed.get(row["owner"], 0) + 1
    _durable_reference._refuse(
        len(pairs) == min(count, MAX_EXCLUDED_PAIRS),
        "pairs lists the first 256 excluded pairs",
    )
    _durable_reference._refuse(
        type(p["truncated"]) is bool and p["truncated"] == (count > len(pairs)),
        "truncated says whether the list is complete",
    )
    by_owner = p["by_owner"]
    _durable_reference._refuse(isinstance(by_owner, dict), "by_owner must be a mapping")
    _durable_reference._refuse(set(by_owner) <= set(OWNERS), "by_owner names an unknown owner")
    _durable_reference._refuse(
        all(type(value) is int and value >= 0 for value in by_owner.values()),
        "by_owner counts are non-negative integers",
    )
    _durable_reference._refuse(sum(by_owner.values()) == count, "by_owner does not sum to count")
    if not p["truncated"]:
        _durable_reference._refuse(
            {owner: value for owner, value in by_owner.items() if value} == listed,
            "by_owner does not count the listed pairs",
        )
    _durable_reference._sha(p["pairs_digest"], "pairs_digest is not a sha-256 digest")
    _durable_reference._refuse(
        len(p["evidence"]) == 1, "pairs_excluded cites one measurement prefix"
    )


def _validate_repairs_dispatched(p: Mapping[str, Any]) -> None:
    """#78 (spec §3.5): the slots one dispatch of threshold repairs reserves."""

    _identifier(p["measurement_id"])
    _positive(p["count"], "count is not a positive integer")
    _durable_reference._refuse(
        len(p["evidence"]) == 1, "repairs_dispatched cites one measurement prefix"
    )


def _validate_stage_redispatched(p: Mapping[str, Any]) -> None:
    """#109 (a28 spec §3.1): the slots a carried stage moves into its new window."""

    _identifier(p["measurement_id"])
    _positive(p["stage"], "stage is not a positive integer")
    _positive(p["count"], "count is not a positive integer")
    _durable_reference._refuse(
        len(p["evidence"]) == 1, "stage_redispatched cites one measurement prefix"
    )


def _validate_grouper_failure(p: Mapping[str, Any]) -> None:
    """Family 14's `grouping_failed(role_failed)` (ADR-0034, A§7, F0 R45).

    `reason` and `error_class` are present exactly on code `role_failed`, so
    every other code keeps its shape; `error_class` is the reason's class in
    `GROUPER_REASONS`. Which reasons a Grouper kind may write is the
    lifecycle's rule, since only the prefix knows the identity's kind.
    """

    role_failed = p["code"] == "role_failed"
    _durable_reference._refuse(
        ("reason" in p) is role_failed and ("error_class" in p) is role_failed,
        "reason and error_class are present exactly on a role_failed grouping",
    )
    if not role_failed:
        return
    reason, error_class = p["reason"], p["error_class"]
    _durable_reference._refuse(
        isinstance(reason, str) and reason in GROUPER_REASONS,
        "grouper reason is not in the registry",
    )
    _durable_reference._refuse(
        isinstance(error_class, str) and error_class in FAILURE_CLASSES,
        "grouper error_class is not a failure class",
    )
    _durable_reference._refuse(
        grouper_class(reason) == error_class, "grouper error_class is not the reason's class"
    )


def _validate_grouping_fallback(fallback: object, calls: object) -> None:
    """Family 9's `grouping_finished.fallback`/`.calls` shapes (spec §10).

    Both are `null` or closed objects here; which policy may carry which is
    the lifecycle's rule (`_grouping_terminal`), since only the prefix knows
    the journaled grouping kind. Family 14 (`final`, F0 R45) narrows the
    fallback reasons to `GROUPING_FALLBACK_REASONS14` and also admits the
    `agent_cli` calls shape `GROUPING_AGENT_CALL_KEYS`; a mixed shape is refused.
    """

    if fallback is not None:
        row = _keys(fallback, {"kind", "scope", "reason"})
        _durable_reference._refuse(row["kind"] in GROUPING_FALLBACK_KINDS, "grouping fallback kind")
        _durable_reference._refuse(
            row["scope"] in GROUPING_FALLBACK_SCOPES, "grouping fallback scope"
        )
        reasons = GROUPING_FALLBACK_REASONS14
        _durable_reference._refuse(row["reason"] in reasons, "grouping fallback reason")
    if (isinstance(calls, dict)) and (calls.keys() == GROUPING_AGENT_CALL_KEYS):
        for name in sorted(GROUPING_AGENT_CALL_KEYS):
            _durable_reference._refuse(
                type(calls[name]) is int and calls[name] >= 0,
                f"grouping calls {name} is not a count",
            )
    elif calls is not None:
        row = _keys(calls, {"map_requests", "reduce", "attempts", "repairs"})
        _durable_reference._refuse(row["reduce"] in GROUPING_REDUCE_STATES, "grouping reduce state")
        for name in ("map_requests", "attempts", "repairs"):
            _durable_reference._refuse(
                type(row[name]) is int and row[name] >= 0, f"grouping calls {name} is not a count"
            )


def _validate_grouping_result(
    value: object, digest: object, *, description_chars: int = 512
) -> None:
    try:
        raw = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemaError("grouping result is not canonical JSON") from exc
    _durable_reference._refuse(len(raw) <= 512 * 1024, "grouping result exceeds the limit")
    _durable_reference._refuse(
        hashlib.sha256(raw).hexdigest() == digest, "grouping result digest does not match"
    )
    row = _keys(value, {"version", "registry_digest", "assignments", "descriptions", "variants"})
    _durable_reference._refuse(
        row["version"] == "reflect_grouping_result/1", "grouping result version"
    )
    _durable_reference._sha(row["registry_digest"])
    _durable_reference._refuse(
        isinstance(row["assignments"], list)
        and len(row["assignments"]) <= 32768
        and all(
            item is None or type(item) is int and 0 <= item <= 32767 for item in row["assignments"]
        ),
        "grouping assignments",
    )
    for name in ("descriptions", "variants"):
        _durable_reference._refuse(isinstance(row[name], dict), f"grouping {name}")
        _durable_reference._refuse(
            len(canonical_json(row[name])) <= 64 * 1024, f"grouping {name} size"
        )
    for key, description in row["descriptions"].items():
        group = _group_number(key)
        _durable_reference._refuse(
            group <= 32767 and group in row["assignments"], "grouping description key"
        )
        entry = _keys(description, {"label", "description"})
        _durable_reference._refuse(
            all(
                isinstance(entry[name], str) and len(entry[name]) <= limit
                for name, limit in (("label", 128), ("description", description_chars))
            ),
            "grouping description entry",
        )
    for key, label in row["variants"].items():
        index = _group_number(key)
        _durable_reference._refuse(index < len(row["assignments"]), "grouping variant index")
        _durable_reference._refuse(
            isinstance(label, str) and len(label) <= 64, "grouping variant entry"
        )
    labels: dict[int, set[str]] = {}
    for key, label in row["variants"].items():
        assignment = row["assignments"][_group_number(key)]
        if assignment is not None:
            labels.setdefault(assignment, set()).add(label)
    _durable_reference._refuse(
        all(len(values) <= 16 for values in labels.values()), "grouping variant labels"
    )


def _group_number(value: object) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise SchemaError("grouping sparse-map key")


def _validate_read_activity(value: object) -> None:
    try:
        raw = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemaError("grouping reads are not canonical JSON") from exc
    row = _keys(value, {"entries", "omitted_reads", "omitted_errors"})
    _durable_reference._refuse(
        isinstance(row["entries"], list) and len(row["entries"]) <= 256, "grouping reads limit"
    )
    for name in ("omitted_reads", "omitted_errors"):
        _durable_reference._refuse(
            type(row[name]) is int and row[name] >= 0, f"{name} is not a non-negative integer"
        )
    _durable_reference._refuse(len(raw) <= 64 * 1024, "grouping reads size")


def _validate_frozen_feedback(value: object, mode: object, *, independent: bool = False) -> None:
    """One frozen feedback section's shape.

    Family 12 adds `no_exam_impact` (G2 spec §4.4): such an Attempt closes
    before a later one of its round freezes its input, so it can be fed back.
    It reads `_AFFECTED_FEEDBACK_OUTCOMES`, never the close vocabulary's
    `condition_changed`. Earlier families keep the vocabulary they always read
    here. Family 13 (`confidence`) reads `_CONFIDENCE_FEEDBACK_OUTCOMES` and its
    rejection layers (G7).
    """

    try:
        raw = canonical_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemaError("frozen feedback is not canonical JSON") from exc
    _durable_reference._refuse(isinstance(value, dict), "frozen feedback is an object")
    _durable_reference._refuse(len(raw) <= 128 * 1024, "frozen feedback exceeds the limit")
    row = _keys(value, {"attempts", "omitted_attempts"})
    _durable_reference._refuse(
        type(row["omitted_attempts"]) is int and row["omitted_attempts"] >= 0,
        "feedback omissions",
    )
    _durable_reference._refuse(
        isinstance(row["attempts"], list) and len(row["attempts"]) <= 8, "feedback attempts"
    )
    for attempt in row["attempts"]:
        required = {
            "attempt_id",
            "index",
            "assessed",
            "outcome",
            "reason",
            "changes_status",
            "changed_paths",
            "changed_paths_truncated",
            "diff_excerpt",
            "diff_excerpt_truncated",
            "cases",
            "omitted_exam_cases",
            "omitted_rest_cases",
        }
        optional = {"rejection_layer", "source_refs"}
        _durable_reference._refuse(
            isinstance(attempt, dict)
            and set(attempt)
            in (
                required,
                required | {"rejection_layer"},
                required | {"source_refs"},
                required | optional,
            ),
            "feedback attempt keys",
        )
        assert isinstance(attempt, dict)
        _identifier(attempt["attempt_id"])
        _positive(attempt["index"], "feedback attempt index")
        _durable_reference._refuse(type(attempt["assessed"]) is bool, "feedback assessed")
        outcomes = feedback_outcomes(15) if independent else (feedback_outcomes(14))
        _durable_reference._refuse(attempt["outcome"] in outcomes, "feedback outcome")
        _durable_reference._refuse(
            attempt["reason"] in outcomes[attempt["outcome"]],
            "feedback reason",
        )
        _durable_reference._refuse(
            attempt["changes_status"] in {"available", "unavailable", "withheld"},
            "feedback changes status",
        )
        _durable_reference._refuse(
            isinstance(attempt["changed_paths"], list)
            and len(attempt["changed_paths"]) <= 8
            and all(
                isinstance(path, str) and 0 < len(path) <= 4096 for path in attempt["changed_paths"]
            ),
            "feedback paths",
        )
        _durable_reference._refuse(
            isinstance(attempt["diff_excerpt"], str) and len(attempt["diff_excerpt"]) <= 2048,
            "feedback excerpt",
        )
        _durable_reference._refuse(
            type(attempt["changed_paths_truncated"]) is bool
            and type(attempt["diff_excerpt_truncated"]) is bool,
            "feedback truncation flags",
        )
        _durable_reference._refuse(
            attempt["changes_status"] == "available"
            or (not attempt["changed_paths"] and attempt["diff_excerpt"] == ""),
            "withheld feedback hides change content",
        )
        _durable_reference._refuse(
            isinstance(attempt["cases"], list) and len(attempt["cases"]) <= 16, "feedback cases"
        )
        for case in attempt["cases"]:
            fields = {"case_id", "delta", "paired_repeats", "expected_repeats", "incomplete"}
            if isinstance(case, dict) and "status" in case:
                _durable_reference._refuse(independent, "unmeasured feedback requires family 15")
                fields.add("status")
            entry = _keys(case, fields)
            _identifier(entry["case_id"])
            _positive(entry["expected_repeats"], "feedback expected repeats")
            if "status" in entry:
                _durable_reference._refuse(
                    entry["status"] == "not_measured"
                    and entry["delta"] is None
                    and type(entry["paired_repeats"]) is int
                    and entry["paired_repeats"] == 0
                    and entry["incomplete"] is False,
                    "unmeasured feedback has no numeric evidence",
                )
            else:
                _number(entry["delta"], "feedback case delta")
                _positive(entry["paired_repeats"], "feedback paired repeats")
                _durable_reference._refuse(
                    entry["paired_repeats"] <= entry["expected_repeats"], "feedback paired repeats"
                )
                _durable_reference._refuse(type(entry["incomplete"]) is bool, "feedback incomplete")
        for name in ("omitted_exam_cases", "omitted_rest_cases"):
            _durable_reference._refuse(
                type(attempt[name]) is int and attempt[name] >= 0, f"feedback {name}"
            )
        if "rejection_layer" in attempt:
            _identifier(attempt["rejection_layer"])
        expected_layer = REJECTION_LAYERS.get(attempt["reason"])
        _durable_reference._refuse(
            "rejection_layer" not in attempt
            or expected_layer is not None
            and attempt["rejection_layer"] == expected_layer,
            "layered reject feedback needs its matching rejection layer",
        )
        if "source_refs" in attempt:
            _durable_reference._refuse(
                isinstance(attempt["source_refs"], list)
                and len(attempt["source_refs"]) <= 8
                and all(
                    isinstance(item, str) and len(item) <= 256 for item in attempt["source_refs"]
                ),
                "feedback source refs",
            )
    _durable_reference._refuse(
        mode != "none" or row == {"attempts": [], "omitted_attempts": 0},
        "none feedback is empty",
    )


def _validate_error_class(value: object, registry: frozenset[str] = EDITOR_ERRORS) -> None:
    if isinstance(value, str) and value.startswith("exception:"):
        _identifier(value.removeprefix("exception:"))
        return
    _durable_reference._refuse(value in registry, "error_class is not in the registry")


def _adaptive_group_violation(groups: list[Any], group_text_chars: int | None) -> str | None:
    """`campaign_reflector.adaptive_group_violation` over the stored groups at
    text limit `group_text_chars`, imported lazily for the reason
    `_validate_group_shape` states: the record's limits are the Reflector's own
    (#282), and a schema that restated them would drift from the tool schema
    and the admission check silently.

    The stateless `validate_payload` passes `None`: it cannot know the round
    (C6). The lifecycle passes the round's own limit at `reflection_finished`.
    """

    from evolution.tree.roles.limits import adaptive_limits
    from evolution.tree.roles.reflector_contracts import adaptive_group_violation

    return adaptive_group_violation(groups, adaptive_limits(group_text_chars))


def _validate_group_shape(group: object) -> None:
    """A diagnosis group's structural shape only (`campaign_reflector.Group`'s
    own keys); `validate_groups` there runs the semantic checks -- known
    evidence, cited cases, writable paths, the oracle-needle scan -- in the
    executor, over corpus and policy state this module never reads. The
    numbers are `_adaptive_group_violation`'s, not this function's (#282).

    The Reflector's checks are imported lazily, `_matches_any`'s own precedent
    in `campaign_reflector`: a module-scope import would close a cycle, since
    `campaign_reflector` imports `campaign_editor`, which imports
    `campaign_statistics`, which imports this module for
    `AttemptState`/`CampaignSnapshot`/`Selection`.
    """

    required = {
        "root_cause",
        "suggested_fix",
        "evidence_ids",
        "affected_cases",
        "target_paths",
        "counterevidence",
        "regression_risks",
        "why_this_layer",
        "probe_refs",
    }
    _durable_reference._refuse(
        isinstance(group, Mapping) and set(group) in (required, required | {"document_ids"}),
        "adaptive group keys",
    )
    assert isinstance(group, Mapping)
    row = dict(group)
    text_fields: tuple[str, ...] = (
        "root_cause",
        "suggested_fix",
        "counterevidence",
        "regression_risks",
        "why_this_layer",
    )
    for name in text_fields:
        _durable_reference._refuse(
            isinstance(row[name], str) and 0 < len(row[name]),
            f"{name} is not a non-blank string",
        )
    for name in ("evidence_ids", "affected_cases", "target_paths"):
        _durable_reference._refuse(
            isinstance(row[name], list) and all(isinstance(item, str) for item in row[name]),
            f"{name} is not a list of strings",
        )
    _durable_reference._refuse(
        isinstance(row["probe_refs"], list)
        and all(type(item) is int for item in row["probe_refs"]),
        "probe_refs is not a list of ints",
    )
    if "document_ids" in row:
        _durable_reference._refuse(
            isinstance(row["document_ids"], list)
            and all(isinstance(item, str) for item in row["document_ids"]),
            "document_ids is not a list of strings",
        )


def _validate_artifact_locator(value: object) -> None:
    """The S2 relative reference of the sealed Candidate; no byte range."""

    _durable_reference._refuse(isinstance(value, str), "artifact_locator is not a string")
    assert isinstance(value, str)
    _durable_reference._refuse(0 < len(value) <= 512, "artifact_locator length")
    _durable_reference._refuse(
        all(0x20 <= ord(char) <= 0x7E for char in value),
        "artifact_locator is not printable ASCII",
    )
    _durable_reference._refuse(
        not value.startswith("/") and ".." not in value.split("/"),
        "artifact_locator escapes the run",
    )


def _validate_identity_connection_fields(identity: Mapping[str, Any], family_version: int) -> None:
    """R17: every journaled identity obeys its family's connection boundary.

    Run after the value object's shape check, preserving malformed identity
    classifications (including a single connection field).
    """

    probe = identity.get("reflector", {}).get("probe")
    _durable_reference._refuse(
        family_version != 14 or probe is None or "string_body" not in probe,
        "family 14 identity cannot bind probe.string_body",
    )
    connection = [name for name in ("metric_contract", "role_visibility") if name in identity]
    _durable_reference._refuse(
        len(connection) == 2,
        "family 14 identity binds metric_contract and role_visibility",
    )
    # ADR-0038: families 14 and 15 admit the optional `verbatim_answers` digest.
    _durable_reference._refuse(
        family_version != 14 or "secret_oracle_paths" not in identity,
        "family 14 identity cannot bind secret_oracle_paths",
    )
    _durable_reference._refuse(
        family_version != 14 or "checks" not in identity, "family 14 identity cannot bind checks"
    )
    _durable_reference._refuse(
        family_version != 14 or "preflight_sample_retries" not in identity,
        "family 14 identity cannot bind preflight_sample_retries",
    )


def _validate_created(p: dict[str, Any], *, family_version: int) -> None:
    _durable_reference._refuse(
        type(p["family_version"]) is int and p["family_version"] == family_version,
        "family version is not the reader's",
    )
    policy: CampaignPolicyV11
    try:
        if family_version == 15:
            _, policy = _confidence_identity_and_policy(p["identity"])
        else:
            _, policy = _adaptive_identity_and_policy(p["identity"])
    except (ValueError, TypeError) as exc:
        raise SchemaError("campaign identity is invalid") from exc
    identity_digest = hashlib.sha256(canonical_json(p["identity"])).hexdigest()
    _durable_reference._refuse(
        identity_digest == p["identity_digest"], "identity digest does not match"
    )
    _durable_reference._refuse(
        policy.sample_retries is not None, "family 6 requires sample_retries"
    )
    _durable_reference._refuse(
        isinstance(policy, CampaignPolicyV11), f"family {family_version} requires a /15 policy"
    )
    # /16 subclasses V11, but can only create its own exact family.
    _durable_reference._refuse(
        (type(policy) is CampaignPolicyV12) == (family_version == 15),
        "family 15 requires exactly a /16 policy; earlier families refuse it",
    )
    # R17, F0 §11: family 14's identity binds both connection digests; no
    # earlier family's does.
    _validate_identity_connection_fields(p["identity"], family_version)

    adaptation = _keys(p["adaptation"], {"version", "digest"})
    # ADR-0021: family 8 is exactly the family whose adaptation identity is
    # `/2`; family 7 (and every earlier one) keeps `/1`. ADR-0022: family 9
    # is exactly the family whose adaptation identity is `/3`. ADR-0023:
    # family 10 is exactly the family whose adaptation identity is `/4`.
    _durable_reference._refuse(
        adaptation["version"] == _adaptation_profile_for(),
        "adaptation slot version",
    )
    _durable_reference._sha(adaptation["digest"], "adaptation slot digest")
    _validate_adaptation_payload(p["adaptation_payload"], adaptation, family_version=family_version)
    _validate_train_partition(p["adaptation_payload"], p["identity"])
    _validate_provenance_labels_at_creation(p, policy)
    _identifier(p["producing_version"])
    _durable_reference._refuse(p["alpha_attempt"] is None, "family 13 records no alpha")
    return


def creation_alpha_attempt(policy: CampaignPolicyV11) -> dict[str, Any] | None:
    """Current creation and rebase payloads retain the null alpha slot."""
    return None


def _confidence_identity_and_policy(
    data: dict[str, Any],
) -> tuple[dict[str, Any], CampaignPolicyV11]:
    """Family-13 design G2: a `/14` or `/15` identity is validated whole.

    `CampaignIdentityV1` reads every policy class through
    `policy_class_for_payload`, so the identity needs no V1 view; a V1 view of
    a policy without `alpha` cannot exist.
    """

    try:
        policy = policy_class_for_payload(data["policy"]).from_payload(data["policy"])
        assert isinstance(policy, CampaignPolicyV11)
        CampaignIdentityV1.from_payload(data)
    except (KeyError, ValueError, TypeError) as exc:
        raise SchemaError("campaign identity is invalid") from exc
    _durable_reference._refuse(
        data.get("policy_digest") == policy.digest, "identity policy digest does not match"
    )
    return data, policy


def _adaptive_identity_and_policy(value: object) -> tuple[dict[str, Any], CampaignPolicyV11]:
    """Validate the whole native identity without a retired-policy projection."""
    _durable_reference._refuse(isinstance(value, Mapping), "campaign identity is invalid")
    assert isinstance(value, Mapping)
    return _confidence_identity_and_policy(dict(value))


FINAL_CAMPAIGN_FAMILY = EnvelopeFamily(
    lambda event, payload: validate_payload(event, payload, final=True),
    SUSPENDABLE_NO_OPERATION_EVENTS,
    # H10 (R62, choice 16): a preflight start is refused, not a fault, when its
    # terminal and a possible suspension no longer fit behind it.
    lambda event: (
        event in (START_EVENT, REFLECTION_START_EVENT, GROUPING_START_EVENT, CONNECTION_START_EVENT)
    ),
)


def campaign_family_for(family_version: int) -> EnvelopeFamily:
    """Return the exact closed reader for one supported campaign family."""
    if type(family_version) is int and family_version == 15:
        from evolution.tree.family15_schema import FAMILY15_CONTRACT

        return FAMILY15_CONTRACT
    family = FINAL_CAMPAIGN_FAMILY if type(family_version) is int and family_version == 14 else None
    return _need(family, "campaign family is not supported")


# -- snapshot ------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """What the pure §8 selector decided for one round; Task 4 computes it."""

    attempt_id: str | None
    candidate_digest: str | None
    dependency_digest: str | None
    ranking: tuple[Mapping[str, Any], ...] = ()


Selector = Callable[["CampaignSnapshot", int], Selection]


@dataclass(frozen=True)
class AttemptState:
    """One attempt's frozen prefix state. `editor` is `None | started | edited |
    declined | failed`; `editor_error` keeps `editor_failed.error_class`, which
    the `editor_unresponsive` terminal of §7 needs. `code` keeps
    `attempt_closed.code`, the S2 refusal code of a `seal_refused` close,
    `None` otherwise (sprint design §4.3: an in-memory field, no record and no
    invariant)."""

    attempt_id: str
    round: int
    index: int
    parent_version: str
    parent_digest: str
    editor: str | None = None
    editor_error: str | None = None
    candidate_digest: str | None = None
    dependency_digest: str | None = None
    measurement_id: str | None = None
    #: Family 12 with `fresh_parent: true` (G2 spec §4.6): the Attempt's fresh
    #: Parent measurement, bound after `measurement_id`; `None` until then, and
    #: in every earlier family.
    parent_measurement_id: str | None = None
    looks: Mapping[int, dict[str, Any]] = field(default_factory=dict)
    not_started: tuple[int, ...] = ()
    outcome: str | None = None
    reason: str | None = None
    look: int | None = None
    code: str | None = None
    #: Sequence of the durable `attempt_closed` terminal, for sequential
    #: feedback provenance inside the same campaign journal.
    closed_seq: int | None = None
    closed_record_digest: str | None = None
    closed_record_locator: str | None = None
    alpha_total: str = ZERO
    #: Adaptive-family input, written once before an editor invocation.
    frozen_input: dict[str, Any] | None = None
    #: Family 10 (spec §5.3): the last `editor_failed.reason`, `None` for a
    #: lifecycle terminal and in every earlier family.
    editor_reason: str | None = None
    #: Family 10: a `campaign_suspended` resolved the last Editor record, a
    #: replayable `provider`/`unknown` failure, so the Editor may run again.
    editor_suspended: bool = False
    #: Family 10 (spec §6.3): the last `editor_finished.continued`, `False`
    #: when the Editor has not finished with that key present (an earlier
    #: family, a failure, or an attempt whose Editor has not finished yet).
    editor_continued: bool = False
    #: Family 12 (G2 spec §4.5): the count of `candidate_inspected` records of
    #: this Attempt and the last one's `components`; `0` and `None` in every
    #: earlier family.
    inspections: int = 0
    inspected_components: Mapping[str, Any] | None = None
    #: ADR-0024 (2026-11-08): omitted IDs bound into the inspected declaration.
    inspected_withheld: int = 0
    #: Family 12 (G2b3b): the condition epoch in force at the last inspection.
    #: Snapshot-only, not journaled; `None` before one and in earlier families.
    inspected_epoch: int | None = None
    #: Family 12 (G2 spec §4.4): the `affected_cases_selected` payload, `None`
    #: until it is written and in every earlier family.
    selection: Mapping[str, Any] | None = None
    #: Family 13 (design §6.2): the governing `comparison_bound` without
    #: `evidence`, plus its `seq` and its binding `digest` (design §6.1, the
    #: input of every seed); `None` until it is written and in earlier families.
    comparison: Mapping[str, Any] | None = None
    #: Family 13 (design §6.3): the `noise_calibration_bound`, plus its `seq`.
    calibration: Mapping[str, Any] | None = None
    #: Family 13 (design §6.4): the `noise_calibration_completed`, plus its `seq`.
    calibration_result: Mapping[str, Any] | None = None
    #: Family 13 (G11, F7): the sequences of the comparisons a superseding
    #: `comparison_bound` replaced, oldest first, so a look citing one is
    #: refused by name; `()` until a `continue` re-binds one.
    superseded_comparisons: tuple[int, ...] = ()
    #: Family 13 (G11, plan choice 4): the sequence of the fresh-Parent
    #: calibration a superseding comparison set aside because a `continue`
    #: re-bound its Parent before it completed. The superseding
    #: `noise_calibration_bound` names it in `supersedes` and clears it.
    superseded_calibration: int | None = None
    family15: Family15AttemptState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "looks", MappingProxyType(dict(self.looks)))
        for name in (
            "inspected_components",
            "selection",
            "comparison",
            "calibration",
            "calibration_result",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, MappingProxyType(dict(value)))


def editor_replayable(attempt: AttemptState, *, adaptive: bool, resilient: bool) -> bool:
    """Whether a failed Editor record is re-run rather than closing its Attempt.

    An adaptive family's `interrupted` terminal re-runs from Parent (spec
    §11); family 10 (`resilient`, role resilience spec §5.3, ADR-0023) adds a
    settled failure of class `provider` or `unknown`, which re-runs once its
    suspension is written (`AttemptState.editor_suspended`).
    """

    if attempt.editor != "failed" or not adaptive:
        return False
    if attempt.editor_error == "interrupted":
        return True
    if not resilient or attempt.editor_reason is None:
        return False
    return editor_class(attempt.editor_error or "", attempt.editor_reason) in (
        "provider",
        "unknown",
    )


def reflection_entry_class(entry: Mapping[str, Any]) -> FailureClass | None:
    """Class of a settled family-10 reflection entry; None for finished or lifecycle entries.

    `entry` is a reduced reflection (`RoundState.reflections`). Only a
    family-10 `reflection_failed` carries a `reason` (the payload grammar
    refuses it in every earlier family), so every other entry has no class.
    """

    if entry.get("status") != "failed" or entry.get("error_class") in LIFECYCLE_ERRORS:
        return None
    reason = entry.get("reason")
    if reason is None:
        return None
    return reflection_class(str(entry["error_class"]), str(reason))


@dataclass(frozen=True)
class RoundState:
    round: int
    baseline_measurement_id: str | None = None
    baseline_complete: bool = False
    attempts: Mapping[str, AttemptState] = field(default_factory=dict)
    selection: dict[str, Any] | None = None
    closed: dict[str, Any] | None = None
    champion_complete: bool = False
    #: G2 spec §4.7 (family 12): the winner's completion, once `completion_bound`
    #: is durable; a `continue` moves it to the re-bound `<id>@e<k>`.
    completion_measurement_id: str | None = None
    #: The round's `focus_selected` payload (minus `evidence`), plus `seq`.
    focus: dict[str, Any] | None = None
    #: Each entry is `{reflection_id, status, groups, reads, error_class, probes,
    #: seq, operation_id}`; `seq` is the start's while `status == "started"`
    #: and the terminal's afterwards, `operation_id` is always the start's.
    reflections: tuple[dict[str, Any], ...] = ()
    #: The round's `gate_scope_selected` payload (minus `evidence`), plus `seq`.
    gate_scope: dict[str, Any] | None = None
    #: Latest health check for the pending reflection ordinal.  This is reduced
    #: state only; the journal payload remains the existing closed shape.
    probe_check: dict[str, Any] | None = None
    #: Adaptive-family grouping terminal/start payload. A prior round can be
    #: reused when its baseline/policy binding matches.
    grouping: dict[str, Any] | None = None
    #: The compact size-first focus selection; no all-membership dump survives.
    focus_portion: dict[str, Any] | None = None
    #: Family 13 (design §6.3): the locator the round's `baseline_complete`
    #: cites, the settled prefix a baseline-source calibration reads; `None`
    #: before it and in every earlier family.
    baseline_prefix: str | None = None
    #: Family 14: the complete baseline's exact durable citation, retained
    #: independently of later bytes or condition epochs. No rows are copied.
    baseline_reference: Mapping[str, Any] | None = None
    #: Family 13 (design §6.7): the round's `finalists_bound` without
    #: `evidence`, plus its `seq`; `None` before it (and in every other family).
    cohort: Mapping[str, Any] | None = None
    #: Family 14 bindings in journal order, deeply immutable, with seq/digest.
    #: Older families keep this empty. Current/rebound IDs live separately.
    finals: tuple[Mapping[str, Any], ...] = ()
    #: Family 14 current member IDs aligned with `finals`, including historic
    #: rebounds. Derived from binding/rebound records, never persisted separately.
    finals_current: tuple[tuple[str, ...], ...] = ()
    #: Family 13 (F7 plan choice 5): the cohort's current member ids, in
    #: `members` order -- `finalists_bound.members` until a `continue` re-binds
    #: an unfinished member under `<id>@e<k>`. `cohort["members"]` stays the
    #: binding. A tuple that is replaced, never mutated; `None` without a cohort.
    cohort_current: tuple[str, ...] | None = None
    #: Family 13 (F8 plan choice 10): the rows of the round's latest
    #: `cohort_incomplete` listing, `retry_ordinal` included -- report state
    #: only; the grants stay `_cohort_listed`'s. Replaced by each family-13
    #: `cohort_rows` suspension, never mutated; `()` when the round never
    #: suspended on its cohort (and in every other family).
    cohort_listed: tuple[Mapping[str, Any], ...] = ()
    #: Family 13 (design §6.9): each member's `cohort_measurement_completed`
    #: without `evidence`, by measurement id.
    cohort_completed: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Family 13 (design §6.10): each finalist's `global_comparison_completed`
    #: without `evidence`, plus its `seq`, by Attempt id. Replaced, never mutated.
    global_comparisons: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: #282 (design §4.4, C6): the Reflector group text limit of the adaptation
    #: identity in force when the round opened (`baseline_bound`), `None` for no
    #: per-field limit. Derived, never journaled; a later `identity_rebased`
    #: leaves it, so a change takes effect at the next round. 512, the limit of
    #: every journal that bound no `group_text_chars` (R2), is the default.
    group_text_chars: int | None = 512
    evidence: EvidencePin | None = None

    family15: Family15RoundState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", MappingProxyType(dict(self.attempts)))
        if self.baseline_reference is not None:
            object.__setattr__(
                self, "baseline_reference", _immutable_binding(self.baseline_reference)
            )
        if self.finals:
            finals = tuple(_immutable_binding(entry) for entry in self.finals)
            object.__setattr__(self, "finals", finals)
            object.__setattr__(self, "cohort", finals[-1])
            _durable_reference._refuse(
                len(self.finals_current) == len(finals), "each final has current members"
            )
            object.__setattr__(
                self, "finals_current", tuple(tuple(members) for members in self.finals_current)
            )

    @property
    def baseline_prefix_digest(self) -> str | None:
        """Family 14's stored prefix content digest, never a current journal head."""
        return (
            None
            if self.baseline_reference is None
            else str(self.baseline_reference["content_digest"])
        )

    def diagnosis(self, resilient: bool = False) -> dict[str, Any] | None:
        """The round's settled reflection outcome: the last entry that is not
        still `started` and was not `interrupted` before it settled, else
        `None` (an interrupted reflection is retried, never diagnosed). A
        family-7 `reflection_failed(unresponsive)` that a `campaign_suspended`
        named is re-run under a new id on resume (I1 spec §5.4), so it is no
        diagnosis either; only family 7 ever marks an entry `suspended`.

        `resilient` is family 10 (spec §4.2, ADR-0023): a failure of class
        `provider` or `unknown` suspends the campaign and re-runs, so it is
        never a diagnosis -- not even before its suspension is written. Nor is
        a `cancelled` one: family 10 writes it only under the caller's
        cancellation, whose terminal ends the campaign (a crash before it
        leaves that terminal owed, `CampaignSnapshot.cancelled_role_seq`), and
        a lifecycle terminal is ignored by every rule (spec §4.1)."""

        for entry in reversed(self.reflections):
            if (
                entry["status"] != "started"
                and entry["error_class"] != "interrupted"
                and not (resilient and entry["error_class"] == "cancelled")
                and not entry.get("suspended", False)
                and not (resilient and reflection_entry_class(entry) in ("provider", "unknown"))
            ):
                return entry
        return None

    def adaptive_scope_cases(self) -> tuple[str, ...]:
        if self.focus_portion is None:
            return ()
        return tuple(self.focus_portion["case_ids"])


def effective_attempt_feedback(
    state: RoundState, attempt: AttemptState, *, family_version: int
) -> tuple[str | None, Mapping[str, Any] | None]:
    """Safe reason and its extra source, without changing the immutable close.

    Only a settled losing family-14 finalist replaces its gate reason. During
    all-attempts input freezing no comparison exists yet, so the gate reason
    remains authoritative. Closed-round history uses the same settled facts.
    A family-14 Attempt closed `abandoned/editor_failed` on an Editor budget
    reports that budget code instead (ADR-0037 D8).
    """
    if family_version == 15 and attempt.family15 is not None:
        result = attempt.family15.global_result
        if result is not None and not result.payload["accepted"]:
            return str(result.payload["reason"]), dict(
                result.payload,
                seq=result.envelope.to_payload()["sequence"],
                record_digest=result.reference.content_digest,
                record_locator=result.reference.locator,
            )
    comparison = state.global_comparisons.get(attempt.attempt_id)
    if (
        family_version == 14
        and attempt.outcome == "finalist"
        and comparison is not None
        and comparison["accepted"] is False
    ):
        return str(comparison["reason"]), comparison
    if (
        family_version in (14, 15)
        and attempt.outcome == "abandoned"
        and attempt.reason == "editor_failed"
        and attempt.editor_reason in EDITOR_BUDGET_REASONS
    ):
        # ADR-0037 D8: the deciding Editor budget code, with no extra source;
        # b1/b2 journals never hold it, so their rows are unchanged (I1).
        return str(attempt.editor_reason), None
    return attempt.reason, None


@dataclass(frozen=True)
class ConnectionView:
    """Family 14 (H10, F0 R57-R62, C§4.1, C§4.4, C§6.3): the connection preflights.

    `operations` holds one immutable entry per `connection_preflight_started`,
    in journal order: `operation_id`, `invocation_id`, `binding_digest`,
    `epoch`, `seq` (the start's), `candidate_digest`, `dependency_digest`,
    `probe_seed`, `outcome` and `terminal`. `outcome` is `None` while the start
    is open, else C§6.3's `compatible`, the failed `status` or `interrupted`;
    `terminal` is `None` or the terminal's payload without `evidence`, plus its
    `event` and `seq`.

    `open` is the open start's entry (at most one is open). `owes_suspension`
    is `{seq, operation_id, reason}` of a failed terminal whose
    `campaign_suspended` is not yet written: the only record the journal then
    admits (choice 17). `latest` maps each binding digest to its latest entry.
    Empty for families up to 13.
    """

    operations: tuple[Mapping[str, Any], ...] = ()
    open: Mapping[str, Any] | None = None
    owes_suspension: Mapping[str, Any] | None = None
    latest: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def attempts(self) -> int:
        """Preflight starts in the run, interrupted ones included (C§6.3)."""

        return len(self.operations)


@dataclass(frozen=True)
class CampaignSnapshot:
    """The pure reduction of a campaign prefix (spec §5); reading it takes no lease.

    `counts`, `denominator` and `alpha_rounds` start from `identity.linked_from`
    when the campaign is a baseline-recovery continuation, so the successor's
    replay needs nothing but its own log (spec §7). In family 6 only
    `alpha_rounds` is inherited: a successor's thresholds count its own tries
    from zero and the predecessor's totals stay beside them as
    `lineage_counts` / `lineage_denominator` (continuable-terminals spec §5.3).
    """

    identity: dict[str, Any]
    run_id: str
    alpha_plan: dict[str, Any]
    rounds: Mapping[int, RoundState]
    champion: tuple[str, str]
    champion_dependency_digest: str
    measurements: Mapping[str, dict[str, Any]]
    #: `(measurement_id, case_id, sample_id)` in families 3-5; family 6 accounts
    #: every try and appends its `retry_ordinal`.
    accounted: frozenset[tuple[Any, ...]]
    counts: Mapping[str, int]
    denominator: int
    alpha_rounds: Mapping[int, str]
    threshold: dict[str, Any] | None
    finished: dict[str, Any] | None
    capacity: dict[str, Any] | None
    first_round: int
    total_rounds: int
    adaptive: bool = False
    family_version: int = field(kw_only=True)
    #: The identity the prefix ends on: the creation record's digest, or the
    #: latest `identity_rebased`'s. Empty before `campaign_created`.
    identity_digest: str = ""
    #: The `evo-skills` version that wrote the latest of those two records.
    producing_version: str = ""
    #: The `evo-skills` version that wrote the creation record.
    creation_version: str = ""
    #: Measurement id -> the producing version in force when its
    #: `baseline_complete` / `champion_complete` was written (#97 spec §4.4).
    completion_versions: Mapping[str, str] = field(default_factory=dict)
    #: Measurement id -> the producing version its carried rows were admitted
    #: under (ADR-0019 as amended 2026-10-03): the creation's for the declared
    #: baseline import, the one in force at `measurement_rebound` or
    #: `completion_bound` for an in-run carry. Absent for a measurement that
    #: carries nothing.
    import_versions: Mapping[str, str] = field(default_factory=dict)
    #: One entry per accepted `identity_rebased`, oldest first (PDR-0010). The
    #: five public keys the projections publish, plus `authorization_id`, which
    #: only the executor reads (§6.2's owed `store_rebased`).
    rebases: tuple[dict[str, Any], ...] = ()
    #: The `run_dir_unsafe_entry` instance digests this run accepted (§6).
    hygiene_admitted: frozenset[str] = frozenset()
    #: Every producing version this prefix was written by. A sibling log stamped
    #: with any of them resumes; ADR-0015 makes this the run-level rebase record.
    admitted_versions: frozenset[str] = frozenset()
    #: Family 6 with `linked_from`: the predecessor lineage's per-owner counts
    #: and denominator, reported but never compared with a threshold. `None`
    #: otherwise.
    lineage_counts: Mapping[str, int] | None = None
    lineage_denominator: int | None = None
    #: Family 6: `measurement_id -> {stage: (count, pairs_digest)}` of every
    #: journaled `pairs_excluded`. The pairs themselves are recomputed from the
    #: measurement prefix by the executor and the audit and checked against it.
    excluded: Mapping[str, Mapping[int, tuple[int, str]]] = field(default_factory=dict)
    #: Family 6: `measurement_id -> {count, by_owner, pairs, truncated}` of its
    #: journaled `pairs_excluded` -- the bounded summary the projections report
    #: (continuable-terminals spec §8). The executor writes one per measurement.
    exclusions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Family 6: the `(outcome, reason)` an applied `close` (`stopped`,
    #: `condition_changed`) or `terminal` (the threshold's pair, or else the
    #: unresponsive role's `editor_unresponsive`/`reflector_unresponsive`) leaves owed.
    #: Set, it is the only record the prefix admits next (comparable-links spec
    #: §5.2-§5.3); it stays set once that terminal is written.
    owed_terminal: tuple[str, str] | None = None
    #: Family 10 (ADR-0023 amended 2026-09-28): the sequence of the first
    #: `reflection_failed`/`editor_failed` written `cancelled`, else `None`.
    #: It is the caller's stop made durable: an unfinished prefix that holds
    #: one owes `stopped`/`cancelled` (or the terminal an applied close owes),
    #: which resume writes before planning any work. Always `None` below
    #: family 10.
    cancelled_role_seq: int | None = None
    corpus_partition: Mapping[str, Any] | None = None
    #: Family 6 with `linked_from.baseline`: set once an accepted
    #: `authorization_applied(effect: fresh_baseline)` abandons the owed
    #: import (spec §5.3), to `{"authorization_id": <decision id>}`. `None`
    #: while no such decision has been accepted (`CampaignLifecycle._fresh_baseline`
    #: sets it).
    baseline_abandoned: Mapping[str, str] | None = None
    #: Family 7 (I1 spec §5.2): one entry per `campaign_suspended`, oldest
    #: first -- `{seq, reason, owner, evidence_kind, evidence}`.
    suspensions: tuple[Mapping[str, Any], ...] = ()
    #: Family 7 (spec §5.3): the sequence of the latest `campaign_suspended`,
    #: where the threshold window starts; `None` before the first.
    window_start: int | None = None
    #: The running totals `counts`/`denominator` stood at when the window
    #: started. Zero in families 3-6 and before a first suspension, so the
    #: window there *is* the campaign totals; read through `window_counts` and
    #: `window_denominator`.
    window_base_counts: Mapping[str, int] = field(default_factory=dict)
    window_base_denominator: int = 0
    #: Family 7 (I2 spec §6.5, ADR-0019 as amended): the owners whose window an
    #: in-place condition change restarted after the latest suspension, `owner
    #: -> (start_seq, count, denominator)`. `start_seq` is the change's
    #: `identity_rebased`; `count` and `denominator` are the window's own,
    #: accumulated from there over the measurements bound in the epoch that
    #: record opened or later -- a try of a measurement the change froze,
    #: accounted after it, measured the old condition and is not the new
    #: one's. A suspension empties it. Read through `window_count`,
    #: `window_denominator_for` and `window_start_for`; an owner absent here
    #: has the suspension window (`window_counts`, `window_denominator`).
    owner_window: Mapping[str, tuple[int, int, int]] = field(default_factory=dict)
    #: Family 7 (spec §5.4): `(measurement_id, case_id, sample_id)` of every row
    #: the latest suspension's `fault_rows` evidence lists; empty otherwise.
    repair_owed: frozenset[tuple[str, str, str]] = frozenset()
    #: Family 7 (#78, spec §3.2): `measurement_id -> {(case_id, sample_id):
    #: (ordinal, naming_seq)}` of every pair a threshold suspension grants a
    #: repair try, frozen measurements included; empty below family 7.
    threshold_named: Mapping[str, Mapping[tuple[str, str], tuple[int, int]]] = field(
        default_factory=dict
    )
    #: Family 7 (#78, spec §3.5): `measurement_id -> (seq, count)` of its latest
    #: `repairs_dispatched`; empty below family 7.
    repairs_reserved: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    #: Family 7 (I2; #109, a28 spec §3.4): `measurement_id -> seq` of its latest
    #: stage's position -- its latest `stage_opened`, or a later
    #: `stage_redispatched` that moved a carried stage's unfilled slots into a
    #: new window. A position before `window_start` is a stage carried across
    #: the latest suspension.
    stage_positions: Mapping[str, int] = field(default_factory=dict)
    #: Family 6+: `(measurement_id, case_id, sample_id, retry_ordinal)` of every
    #: accounted indeterminate try whose record says no further try follows it
    #: -- an exhausted try, a cancelled one, or (family 7) a repair try.
    retries_declined: frozenset[tuple[Any, ...]] = frozenset()
    #: Families 13-14 (pre-F8 spec §2.4; #194 G3, plan choice 7): `(measurement_id,
    #: case_id, sample_id, retry_ordinal)` of every try a listing restored -- a
    #: declined cohort try, or a declined or cancelled Champion try below
    #: `sample_retries` -- so its successor is a D1 retry, not a repair. Only a
    #: `cohort_rows` or `champion_rows` listing fills it; read it per
    #: measurement through `restored_tries`.
    listing_restored: frozenset[tuple[str, str, str, int]] = frozenset()
    #: Family 7 (spec §5.2): the durable records since the latest suspension
    #: that a `stage_record` suspension may still name, `seq -> (reason,
    #: operation_id)` -- an unresponsive role, a completed grouping failure,
    #: an unavailable probe check. Empty in families 3-6.
    suspension_causes: Mapping[int, tuple[str, str | None]] = field(default_factory=dict)
    #: Family 7: `seq -> (measurement_id, instance_id | None, operation_id)` of
    #: every `baseline_bound`, `measurement_bound` and `instance_attested` --
    #: the records an attestation refusal's `target_changed` `stage_record`
    #: names. Empty in families 3-6.
    target_records: Mapping[int, tuple[str, str | None, str | None]] = field(default_factory=dict)
    #: Family 7 (I1 spec §5.2): the prefix ends on a `campaign_suspended` -- the
    #: last barrier record is a suspension and nothing follows it, so the run
    #: stands suspended and the operator's resume is what continues it. A
    #: suspension is not a terminal, so any record clears this. `False` in
    #: families 3-6, which write no such record.
    suspended: bool = False
    #: Condition epochs (ADR-0020, I2 spec §5.1): one identity payload per
    #: epoch, epoch `0` (the creation identity) first. A comparable
    #: `identity_rebased` opens the next epoch; a non-comparable one opens
    #: none. Nothing admits a comparable `identity_rebased` yet (ADR-0015), so
    #: today this always has exactly one entry.
    epochs: tuple[dict[str, Any], ...] = ()
    #: The creation record's `identity_digest` -- epoch `0`'s digest, stable
    #: for the run's whole life (unlike `identity_digest`, which moves with
    #: the latest rebase). Empty before `campaign_created`.
    creation_identity_digest: str = ""
    #: Family 7 (I2 spec §5.4): the open transition block of an accepted
    #: in-place condition change, `{effect, seq, fields, phase}` -- `seq` is
    #: its `authorization_applied`'s, `fields` the changed comparable
    #: top-level fields (empty until its `identity_rebased`), `phase` what the
    #: block admits next: `rebase` (only the matching `identity_rebased`),
    #: `rebind` (`continue`: any `measurement_rebound`, and the first record
    #: of another kind ends the block) or `close_round` (`rebaseline`: the
    #: open round's Attempt closures, then its `round_closed`). `None`
    #: outside a block; a prefix may end inside one (the owed rest is
    #: written by the next invocation).
    pending_change: Mapping[str, Any] | None = None
    #: Family 7 (I2 spec §4.4): one entry per applied in-place change, in
    #: journal order -- `{seq, effect, fields, round, authorization_id,
    #: identity_digest}`; `round` is the round open at the change, `None`
    #: between rounds.
    condition_changes: tuple[Mapping[str, Any], ...] = ()
    #: Family 7 (I2 spec §3.2): the round whose baseline a `rebaseline` made
    #: fresh -- bound under `m-r<NN>-champion@e<k>`, with no owed winner
    #: completion before it. `None` until a `rebaseline` is applied.
    rebaseline_from_round: int | None = None
    #: Family 14 (H7, F0 R42): one entry per `baseline_refresh_bound`, in journal
    #: order -- the payload minus `evidence`, plus `seq` and `completed`
    #: (`None`, or `{measurement_id, seq}`). Filled by the lifecycle.
    refreshes: tuple[Mapping[str, Any], ...] = ()
    #: Family 14 (H8, ADR-0034, plan decision 6): the derived Grouper fallback
    #: chain -- consecutive admitted scope-`all` fallbacks since the latest
    #: fallback-free grouping or `campaign_suspended`. Never journaled; 0 below
    #: family 14.
    grouper_chain: int = 0
    #: Family 14 (H12b, F0 M7, plan choice 9): the Grouper kind in force -- the
    #: `kind` of the adaptation payload's `grouping` (creation's, then the
    #: latest `identity_rebased`'s), as `grouping_started.grouper_kind` must
    #: name it. Derived, never journaled; `None` below family 14.
    grouper_kind: str | None = None
    #: Family 14 (H10, F0 R57-R62): the connection preflights (`ConnectionView`);
    #: empty below family 14. The dispatch gate and the H12 projection read it.
    connection: ConnectionView = field(default_factory=ConnectionView)
    #: Family 14 (H10 ruling 24): `measurement_id -> end`, the highest byte end
    #: of that measurement journal any campaign record cites (the lifecycle's
    #: cited prefix ends). A journal on disk shorter than its cited end was
    #: lost or truncated; the dispatch gate faults rather than probe over it.
    #: Empty below family 14.
    cited_ends: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    #: Pure family15 authority and recovery indexes; absent in earlier families.
    family15: Family15SnapshotState | None = None
    #: Family 10 (spec §4.4, ADR-0023): the adaptation identity's
    #: `reflection.max_consecutive_failures` in force (creation's, then the
    #: latest `identity_rebased`'s); `None` below family 10.
    max_consecutive_failures: int | None = None
    exhausted_window_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def window_counts(self) -> Mapping[str, int]:
        """Per-owner indeterminate tries since the window started (spec §5.3)."""

        return MappingProxyType(
            {
                owner: count - self.window_base_counts.get(owner, 0)
                for owner, count in self.counts.items()
            }
        )

    @property
    def window_denominator(self) -> int:
        """The denominator since the window started (spec §5.3)."""

        return self.denominator - self.window_base_denominator

    def window_start_for(self, owner: str) -> int | None:
        """Where `owner`'s threshold window starts (I2 spec §6.5).

        The latest of the last suspension and the last change of the owner's
        own field: the change's `identity_rebased` sequence when one restarted
        the window after the latest suspension, `window_start` otherwise.
        """

        restarted = self.owner_window.get(owner)
        return self.window_start if restarted is None else restarted[0]

    def round_evidence(self, round: int) -> EvidencePin | None:
        """Settings pinned when this round opened; None retains the b12 path."""
        return self.rounds[round].evidence

    def round_group_text_chars(self, round: int) -> int | None:
        """Round `round`'s Reflector group text limit (#282, C6): the one its
        execution, briefs and `reflection_finished` check all use."""

        return self.rounds[round].group_text_chars

    def window_count(self, owner: str, *, count_recovered: bool = True) -> int:
        """`owner`'s indeterminate tries in its own window (I2 spec §6.5)."""

        if not count_recovered:
            return self.exhausted_window_counts.get(owner, 0)
        restarted = self.owner_window.get(owner)
        if restarted is None:
            return self.window_counts.get(owner, 0)
        return restarted[1]

    def window_denominator_for(self, owner: str) -> int:
        """The denominator of `owner`'s own window (I2 spec §6.5)."""

        restarted = self.owner_window.get(owner)
        return self.window_denominator if restarted is None else restarted[2]

    def __post_init__(self) -> None:
        for name in (
            "rounds",
            "measurements",
            "counts",
            "alpha_rounds",
            "window_base_counts",
            "owner_window",
            "exhausted_window_counts",
            "stage_positions",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        if self.lineage_counts is not None:
            object.__setattr__(self, "lineage_counts", MappingProxyType(dict(self.lineage_counts)))
        if self.baseline_abandoned is not None:
            object.__setattr__(
                self, "baseline_abandoned", MappingProxyType(dict(self.baseline_abandoned))
            )
        object.__setattr__(
            self,
            "suspensions",
            tuple(MappingProxyType(dict(entry)) for entry in self.suspensions),
        )
        object.__setattr__(
            self,
            "condition_changes",
            tuple(MappingProxyType(dict(entry)) for entry in self.condition_changes),
        )
        if self.pending_change is not None:
            object.__setattr__(self, "pending_change", MappingProxyType(dict(self.pending_change)))
        object.__setattr__(
            self,
            "excluded",
            MappingProxyType(
                {name: MappingProxyType(dict(stages)) for name, stages in self.excluded.items()}
            ),
        )
        object.__setattr__(
            self,
            "exclusions",
            MappingProxyType(
                {name: MappingProxyType(dict(entry)) for name, entry in self.exclusions.items()}
            ),
        )

    def current_round(self) -> int | None:
        open_rounds = [number for number, state in self.rounds.items() if state.closed is None]
        return max(open_rounds) if open_rounds else None

    def open_attempts(self, round: int) -> tuple[AttemptState, ...]:
        state = self.rounds.get(round)
        if state is None:
            return ()
        return tuple(
            sorted(
                (a for a in state.attempts.values() if a.outcome is None),
                key=lambda a: a.index,
            )
        )


# -- family-10 visitation skip (role resilience spec §4.2-§4.3) ------------


def _portion_key(state: RoundState) -> tuple[Any, Any, Any] | None:
    """The error group a round's focus portion selected, under the binding and
    policy digest visitation already uses; `None` for a portion with no group."""

    portion = state.focus_portion
    if portion is None or portion.get("neighborhood_id") is None:
        return None
    return (portion["neighborhood_id"], portion["baseline_binding"], portion["policy_digest"])


def _content_failure(entry: Mapping[str, Any] | None) -> bool:
    return entry is not None and reflection_entry_class(entry) == "content"


def _skipped(state: RoundState) -> bool:
    return state.closed is not None and state.closed.get("visitation") == "skipped"


def content_failure_count(snapshot: CampaignSnapshot, round_number: int) -> int:
    """Consecutive family-10 content-class reflection failures on this round's
    neighborhood_id, under its baseline_binding and policy_digest, counted
    backwards over closed rounds and including this round's own settled
    diagnosis; reset by any other settled reflection outcome on that group and
    by a round_closed with visitation "skipped".

    Rounds on other groups neither count nor reset; a round of the group with
    no settled diagnosis (closed before its reflection) is passed over. A
    `provider`/`unknown` failure is never a diagnosis (`RoundState.diagnosis`),
    so it neither counts nor resets either (spec §4.3).
    """

    state = snapshot.rounds.get(round_number)
    if state is None:
        return 0
    key = _portion_key(state)
    if key is None or not _content_failure(state.diagnosis(resilient=True)):
        return 0
    count = 1
    for number in sorted((n for n in snapshot.rounds if n < round_number), reverse=True):
        prior = snapshot.rounds[number]
        if prior.closed is None or _portion_key(prior) != key:
            continue
        if _skipped(prior):
            break
        diagnosis = prior.diagnosis(resilient=True)
        if diagnosis is None:
            continue
        if not _content_failure(diagnosis):
            break
        count += 1
    return count


def skip_due(snapshot: CampaignSnapshot, round_number: int, threshold: int) -> bool:
    """True exactly when this round's diagnosis is a content failure and
    content_failure_count(...) >= threshold.

    A skip resets the count, so under a constant threshold the count never
    passes it and `>=` agrees with `==`; `>=` only keeps a lowered threshold
    from never firing."""

    state = snapshot.rounds.get(round_number)
    if state is None or not _content_failure(state.diagnosis(resilient=True)):
        return False
    return content_failure_count(snapshot, round_number) >= threshold


def grouper_chain_limit(adaptation_payload: Mapping[str, Any]) -> int:
    return int(adaptation_payload.get("grouping", {}).get("max_consecutive_full_fallbacks", 2))


def skip_chain_limit(adaptation_payload: Mapping[str, Any]) -> int:
    return int(adaptation_payload.get("reflection", {}).get("max_consecutive_skips", 2))


def skip_chain_length(snapshot: CampaignSnapshot, round_number: int) -> int:
    """Number of consecutive skips ending at this closed round; content failures do not break it."""
    state = snapshot.rounds.get(round_number)
    if state is None or not _skipped(state):
        return 0
    diagnosis = state.diagnosis(resilient=True)
    if diagnosis is None or any(
        int(entry["seq"]) > int(diagnosis["seq"]) for entry in snapshot.suspensions
    ):
        return 0
    boundary = max((int(entry["seq"]) for entry in snapshot.suspensions), default=0)
    length = 1
    for number in sorted((n for n in snapshot.rounds if n < round_number), reverse=True):
        state = snapshot.rounds[number]
        diagnosis = state.diagnosis(resilient=True)
        if diagnosis is None:
            continue
        if int(diagnosis["seq"]) <= boundary:
            break
        if _skipped(state):
            length += 1
        elif not _content_failure(diagnosis):
            break
    return length


# -- lifecycle -----------------------------------------------------------


class CampaignLifecycle:
    """Contiguous-prefix indexes with atomic, constant-history-cost transitions.

    Preparing a record does not mutate authoritative indexes; the append log
    commits the delta only after fsync, and a pure reducer commits immediately.
    Only the **L** half of spec §5 is enforced here — never a sibling log.
    """

    def _member_rule(self, rule: str) -> str:
        return member_diagnostic(rule, family_version=self._family_version)

    def __init__(
        self,
        *,
        selector: Selector | None,
        adaptive: bool = False,
        family_version: int | None = None,
    ) -> None:
        selected_family = (
            FINAL_CAMPAIGN_FAMILY_VERSION if family_version is None else family_version
        )
        campaign_family_for(selected_family)
        self._selector = selector
        self._family_version = selected_family
        # Each later family extends the one before it: family 6 is layered and
        # adaptive, and adds its own rules under `_continuable`; family 7 keeps
        # every family-6 rule and adds its own under `_suspendable`.
        self._independent = selected_family == 15
        self._confidence = selected_family == 14
        self.sequence = 0
        self.run_id: str | None = None
        self.created: Envelope | None = None
        self.identity: dict[str, Any] = {}
        self.identity_digest = ""
        #: Epoch 0's digest (ADR-0020): the creation record's `identity_digest`,
        #: stable for the run's whole life. `identity_digest` moves with the
        #: latest rebase; this does not.
        self.creation_identity_digest = ""
        self.producing_version = ""
        self._completion_versions: dict[str, str] = {}
        self._import_versions: dict[str, str] = {}
        #: Whether the creation record declares a non-empty baseline import.
        self._declared_import = False
        self.alpha_attempt: dict[str, Any] = {}
        self.pending: dict[str, Envelope] = {}
        self.operations: set[str] = set()
        self.threshold: dict[str, Any] | None = None
        self.finished: dict[str, Any] | None = None
        self.capacity: dict[str, Any] | None = None
        self.denominator = 0
        self.first_round = 1
        self.total_rounds = 0
        self._policy: CampaignPolicyV11 | None = None
        self._rounds: dict[int, RoundState] = {}
        self._attempt_round: dict[str, int] = {}
        self._measurements: dict[str, dict[str, Any]] = {}
        self._accounted: set[tuple[Any, ...]] = set()
        #: Family 6 only: per accounted try, whether the record said a further
        #: try follows it (the one denominator slot it reserves).
        self._retries_reserved: dict[tuple[Any, ...], bool] = {}
        #: #78 (spec §3.2): per `(measurement_id, case_id, sample_id)`, its latest
        #: accounted try -- `(retry_ordinal, owner, retry_follows, seq)` -- the
        #: campaign-side half of a threshold grant.
        self._latest_try: dict[tuple[str, str, str], tuple[int, str, bool, int]] = {}
        #: Family 13 (pre-F8 spec §2.3): each listed cohort gap's grant,
        #: `(ordinal, naming_seq)`. Filled only by a `cohort_rows` suspension.
        self._cohort_listed: dict[tuple[str, str, str], tuple[int, int]] = {}
        #: Families 13-14 (#194 G2, plan choice 4): each listed Champion gap's
        #: grant, `(ordinal, naming_seq)`. Filled only by a `champion_rows`
        #: suspension.
        self._champion_listed: dict[tuple[str, str, str], tuple[int, int]] = {}
        #: Family 13 (pre-F8 spec §2.4): `(mid, case, sample, k)` of each
        #: declined try a listing restored; its successor at `k + 1` is a D1
        #: retry, not a repair. #194 (G3, plan choice 6) adds a Champion
        #: listing's declined or cancelled try below `sample_retries`.
        self._restored: set[tuple[str, str, str, int]] = set()
        self._counts: dict[str, int] = dict.fromkeys(OWNERS, 0)
        self._lineage_counts: dict[str, int] | None = None
        self._lineage_denominator: int | None = None
        self._excluded: dict[str, dict[int, tuple[int, str]]] = {}
        self._exclusions: dict[str, dict[str, Any]] = {}
        #: Family 13 (design §6.2): each measurement's `pairs_excluded` prefix,
        #: the one its comparison's `exclusions.prefix` repeats. Kept apart from
        #: `_exclusions`, whose shape the projections publish.
        self._exclusion_prefixes: dict[str, str] = {}
        #: Family 13 (G11, plan choice 3): each Attempt's governing
        #: `comparison_bound.evidence`, which a superseding binding repeats.
        #: `AttemptState.comparison` keeps no evidence (design §6.1 digest).
        self._comparison_evidence: dict[str, list[Any]] = {}
        self._alpha_rounds: dict[int, str] = {}
        self._champion: tuple[str, str] = ("", "")
        self._champion_dependency = ""
        self._versions: set[str] = set()
        self._instances: set[str] = set()
        self._unresponsive = False
        self._reflector_unresponsive = False
        self._last: Envelope | None = None
        self._valid_bytes = 0
        self._probe_check_locators: dict[int, str] = {}
        # ADR-0039 D5: family-14 starts refused before a probe check may end
        # only input_too_large or interrupted; rebuilt by journal replay.
        self._probeless_reflections: set[tuple[int, str]] = set()
        self._adaptation: dict[str, Any] | None = None
        #: Family 9: the journaled adaptation payload's `grouping` (creation's,
        #: then the latest `identity_rebased`'s), which decides whether a
        #: `grouping_finished` may carry `fallback` and `calls` (spec §10).
        self._grouping_policy: dict[str, Any] | None = None
        #: Family 10: the adaptation payload's `reflection.max_consecutive_failures`
        #: (creation's, then the latest `identity_rebased`'s), which decides
        #: whether an `unplanned` round is a skip (spec §4.2).
        self._reflection_threshold: int | None = None
        self._skip_chain_limit = 2
        #: #282: the adaptation payload's `reflection.group_text_chars` in force
        #: (creation's, then the latest `identity_rebased`'s, 512 when absent);
        #: a round opening copies it into its `RoundState` (C6).
        self._group_text_chars: int | None = 512
        self._evidence: EvidencePin | None = None
        self._creation_version = ""
        self._rebases: list[dict[str, Any]] = []
        #: Condition epochs (ADR-0020): the identity payload of each epoch,
        #: epoch 0 first. `campaign_created` appends the creation identity;
        #: `_identity_rebased` appends only on a comparable change.
        self._epochs: list[dict[str, Any]] = []
        self._hygiene: set[str] = set()
        self._owed_terminal: tuple[str, str] | None = None
        #: Family 10 (spec §5.3, ADR-0023 amended 2026-09-28): the sequence of
        #: the first role terminal written `cancelled`. It is the caller's
        #: stop, durable: nothing but that stop's terminal
        #: (`CANCELLATION_TERMINALS`) may follow it, and a resumed prefix owes
        #: that terminal before any work.
        self._cancelled_role_seq: int | None = None
        #: Family 6 with `linked_from.baseline`: set by an applied
        #: `fresh_baseline` decision (`_fresh_baseline`), never here. `CampaignSnapshot` reports it
        #: as `baseline_abandoned` (spec §5.3).
        self._baseline_abandoned: dict[str, str] | None = None
        #: Family 7 (I1 spec §5.2-§5.4): the suspensions so far, the running
        #: totals the current window started from, and the fault rows the latest
        #: suspension leaves owed a repair try. `_counts`/`denominator` stay the
        #: campaign totals; a family <= 6 never moves the bases off zero.
        self._suspensions: list[dict[str, Any]] = []
        self._last_suspension_seq: int | None = None
        self._window_base_counts: dict[str, int] = {}
        self._window_base_denominator = 0
        #: Family 7 (I2 spec §6.5): the windows an in-place change restarted
        #: since the latest suspension, `owner -> {start, epoch, count,
        #: denominator}` -- `epoch` the one the change's rebase opened; only a
        #: measurement bound in it or later feeds the window.
        self._owner_windows: dict[str, dict[str, int]] = {}
        self._threshold_windows = {owner: OwnerWindow(owner) for owner in OWNERS}
        #: Family 7 (I2): the sequence of each
        #: measurement's latest `stage_opened`, so a `measurement_rebound`'s
        #: `released` slots leave exactly the windows that counted them. From
        #: #109 (a28 spec §3.4) a `stage_redispatched` moves it too: the
        #: carried stage's slots now sit in that record's window. The snapshot
        #: exposes it as `stage_positions`.
        self._stage_seq: dict[str, int] = {}
        #: Family 12 (#109, a28 spec §3.4): the largest end any campaign
        #: record cited in each measurement journal (`journal_citations`),
        #: `measurement_id -> end`, from the locators alone and never the
        #: bytes. A `stage_redispatched` must cite at least this far.
        self._cited_ends: dict[str, int] = {}
        #: #78 (spec §3.5): each measurement's latest `repairs_dispatched`
        #: `(seq, count)`, the threshold-repair slots it reserved in its window.
        self._dispatch: dict[str, tuple[int, int]] = {}
        self._repair_owed: frozenset[tuple[str, str, str]] = frozenset()
        #: Family 7: `seq -> (reason, operation_id)` of each durable record since
        #: the latest suspension that a `stage_record` suspension may name.
        self._suspension_causes: dict[int, tuple[str, str | None]] = {}
        #: Family 7: every campaign record that binds or attests a measurement
        #: (`baseline_bound`, `measurement_bound`, `instance_attested`), `seq ->
        #: (measurement_id, instance_id | None, operation_id)`, which an
        #: attestation refusal's `target_changed` `stage_record` names. Never
        #: consumed: a resumed target that still lies refuses again.
        self._target_records: dict[int, tuple[str, str | None, str | None]] = {}
        #: Family 7 (I2 spec §5.4): the open transition block (see
        #: `CampaignSnapshot.pending_change`), the applied decision it carries
        #: and the observed identity triple its `identity_rebased` must be.
        self._pending_change: dict[str, Any] | None = None
        self._change_decision: Authorization | None = None
        self._change_observed: dict[str, Any] | None = None
        self._change_instance = ""
        self._condition_changes: list[dict[str, Any]] = []
        self._rebaseline_from_round: int | None = None
        #: Family 14 (H7, F0 R42-R43): one entry per `baseline_refresh_bound`,
        #: in journal order (see `CampaignSnapshot.refreshes`). A round's
        #: entry exists before its `baseline_bound`, so it is kept apart from
        #: `RoundState`, whose entries always carry a baseline.
        self._refreshes: list[dict[str, Any]] = []
        #: Family 14 (H8, plan decision 6): see `CampaignSnapshot.grouper_chain`.
        self._grouper_chain = 0
        #: Family 14 (H10, F0 R53-R62): one entry per `connection_preflight_started`,
        #: in journal order (see `ConnectionView.operations`); the index of the
        #: open one; and the failed terminal whose suspension is owed (choice 17).
        self._connections: list[dict[str, Any]] = []
        self._connection_open: int | None = None
        self._connection_owed: dict[str, Any] | None = None
        self._snapshot: CampaignSnapshot | None = None
        self._family15: Family15Lifecycle | None = None
        if self._independent:
            from evolution.tree.family15_lifecycle import Family15Lifecycle

            self._family15 = Family15Lifecycle(self)

    # -- LogState -------------------------------------------------------

    @property
    def reserved_records(self) -> int:
        # Every open editor keeps its terminal, and a live campaign keeps its
        # `campaign_finished`. Family 5 also keeps the mandatory alpha row of a
        # durable evaluated look whose charge has not committed yet.
        return (
            len(self.pending)
            + int(self.finished is None)
            + int(self._connection_open is not None or self._connection_owed is not None)
        )

    def accept(self, record: Envelope) -> None:
        self.prepare(record).commit()

    def prepare(self, record: Envelope) -> LifecycleTransition:
        v = record.to_payload()
        if self._independent:
            campaign_family_for(15).validate_payload(v["event"], v["payload"])
        p, event = v["payload"], v["event"]
        sequence = self.sequence
        _durable_reference._refuse(v["sequence"] == sequence + 1, "sequence is not contiguous")
        if sequence == 0:
            return self._prepare_created(record, v, p, sequence)
        _durable_reference._refuse(v["run_id"] == self.run_id, "run_id is not constant")
        _durable_reference._refuse(
            event != "campaign_created", "campaign_created is the first record only"
        )
        _durable_reference._refuse(self.finished is None, "campaign_finished is the last record")
        # Comparable-links spec §5.2: an applied `close`/`terminal` admits
        # nothing after it but the terminal it owes -- not a second decision,
        # not a hygiene record, not `capacity_exhausted`. Which terminal is
        # `_check_finished`'s rule.
        _durable_reference._refuse(
            self._owed_terminal is None
            or event == "campaign_finished"
            # H10 (ruling 19): recovery settles an open preflight before the
            # owed terminal, which waits for it.
            or ((event == CONNECTION_INTERRUPTED_EVENT) and (self._connection_open is not None)),
            "only the owed terminal follows an applied close",
        )
        self._check_connection_preamble(event, p)
        # I2 spec §5.4: an in-place condition change is one contiguous block.
        # `rebase` and `close_round` admit only their own records; `rebind`
        # admits `measurement_rebound` and ends at the first other record.
        block = self._pending_change
        if block is not None and block["phase"] == "rebase":
            _durable_reference._refuse(
                event == "identity_rebased",
                "only identity_rebased follows an applied condition change",
            )
        elif block is not None and block["phase"] == "close_round":
            _durable_reference._refuse(
                (event == "attempt_closed" and p.get("reason") == "condition_changed")
                or (event == "round_closed" and p.get("outcome") == "condition_changed"),
                "the rebaseline block closes the open round first",
            )
        ends_block = (
            block is not None and block["phase"] == "rebind" and event != "measurement_rebound"
        )
        # PDR-0010 (amended 2026-09-17), said once for every barrier below: the
        # two consequence records of an operator's decision are admissible at
        # *every* boundary of an unfinished campaign, and at none of a finished
        # one -- which is the refusal directly above, not a case any barrier
        # here repeats. Neither record opens work: `identity_rebased` restates
        # the identity the rest of the run is written under and
        # `authorization_applied` records the decision that admitted an entry of
        # the run directory. A campaign that crashed *at* one of these barriers
        # under an older version must still be resumable by a newer one, and
        # that resume is precisely the invocation these records belong to, so a
        # barrier that listed only the terminal records turned an authorized
        # resume into `CampaignInvariantViolated` after admission.
        #
        # The owed-alpha barrier above is deliberately not one of them. An
        # `alpha_consumed` names the look it *immediately* follows, so a record
        # between the two would break the pair rather than admit a decision;
        # PDR-0010 fixes the order for that boundary instead -- `adopt`
        # discharges the owed row and writes its consequences after it.
        consequence = event in CONSEQUENCE_EVENTS or (block is not None and not ends_block)
        # Spec §7: a threshold reached "opens no further stage ... and writes
        # `campaign_finished`". Both barriers are quiet by definition -- at the
        # round barrier every attempt is closed and no stage is open, at a
        # baseline stage no other stage exists -- so no record that opens work
        # may follow one. `capacity_exhausted` is the exception §5's own row
        # carries: "best effort: written only when `campaign.jsonl` itself can
        # still be appended", which is precisely what the §7 barrier after a
        # threshold can still discover while it drains and releases.
        # Family 7 resolves either barrier with a `campaign_suspended` instead
        # (I1 spec §5.2); its rule refuses the record in every earlier family.
        _durable_reference._refuse(
            self.threshold is None
            or consequence
            or event in ("campaign_finished", "capacity_exhausted", "campaign_suspended"),
            "only the campaign terminal follows a threshold",
        )
        unavailable = not probe_continues(self.identity, self._family_version) and any(
            state.probe_check is not None and state.probe_check["status"] == "unavailable"
            for state in self._rounds.values()
        )
        _durable_reference._refuse(
            not unavailable
            or consequence
            or event
            in (
                "campaign_finished",
                "capacity_exhausted",
                "campaign_suspended",
                "probe_checked",
                "reflection_started",
            ),
            "only the probe unavailable terminal follows an unavailable check",
        )
        if (
            event == "campaign_suspended"
            or event in WORK_OPENING_EVENTS
            or (
                self._family_version == 14
                and event == campaign_vocabulary_for(14).measurement_bound
            )
            or (
                event == "campaign_finished"
                and self._owed_terminal is None
                and (p.get("outcome"), p.get("reason")) not in CANCELLATION_TERMINALS
            )
        ):
            # Spec §5.3, ADR-0023 (amended 2026-09-28): the barrier writes a
            # role `cancelled` only for the caller's cancellation or an
            # accepted changed-condition close, so the record is durable
            # evidence of that stop. Only the stop's own `campaign_finished`
            # follows it -- in its invocation, or on resume, which writes it
            # before planning any work (`CampaignSnapshot.cancelled_role_seq`).
            # A suspension, work opened, or another stop's terminal is refused.
            # A terminal an applied decision owes is its own authority
            # (comparable-links spec §5.2) and stands.
            _durable_reference._refuse(
                self._cancelled_role_seq is None,
                "a cancelled role terminal stands only under a cancellation",
            )
        if self._family15 is not None:
            self._family15.preamble(event, p)
        op = v["operation_id"]
        started = event in (START_EVENT, REFLECTION_START_EVENT, GROUPING_START_EVENT) or (
            event == CONNECTION_START_EVENT
        )
        terminal = (
            event in EDITOR_TERMINALS
            or event in REFLECTION_TERMINALS
            or event in GROUPING_TERMINALS
            or (event in CONNECTION_TERMINALS)
        )
        if op is not None:
            if terminal:
                _durable_reference._refuse(op in self.pending, "a terminal needs its start")
            else:
                _durable_reference._refuse(op not in self.operations, "operation id is used once")
        apply = self._rule(event, p, op, v["sequence"], record)
        cited = _cited_journal_ends(event, p, run_id=self.run_id)
        cause = self._suspension_cause(event, p)
        target = self._target_record(event, p)
        pending_after = len(self.pending) + (1 if started else -1 if terminal else 0)
        reserved = (
            pending_after
            + int(event != "campaign_finished")
            + (self._connection_reserved_after(event))
        )

        def commit() -> None:
            _durable_reference._refuse(self.sequence == sequence, "stale campaign transition")
            if ends_block:
                self._pending_change = None
            apply()
            if self._family15 is not None:
                self._family15.remember(record)
            for measurement_id, end in cited.items():
                self._cited_ends[measurement_id] = max(end, self._cited_ends.get(measurement_id, 0))
            if cause is not None:
                self._suspension_causes[sequence + 1] = (cause, op)
            if target is not None:
                self._target_records[sequence + 1] = (*target, op)
            if (
                event in ("editor_failed", "reflection_failed")
                and p.get("error_class") == "cancelled"
            ):
                if self._cancelled_role_seq is None:
                    self._cancelled_role_seq = sequence + 1
            if op is not None:
                if started:
                    self.pending[op] = record
                    self.operations.add(op)
                elif terminal:
                    del self.pending[op]
                else:
                    self.operations.add(op)
            self._last = record
            self.sequence += 1
            self._valid_bytes += len(record.encode())
            self._snapshot = None

        return LifecycleTransition(reserved, commit)

    @property
    def snapshot(self) -> CampaignSnapshot:
        if self._snapshot is None:
            self._snapshot = CampaignSnapshot(
                family15=None if self._family15 is None else self._family15.snapshot(),
                corpus_partition=(
                    self.created.payload.get("adaptation_payload", {}).get("corpus_partition")
                    if self.created is not None
                    else None
                ),
                identity=dict(self.identity),
                run_id=self.run_id or "",
                alpha_plan=dict(self.alpha_attempt),
                rounds=dict(self._rounds),
                champion=self._champion,
                champion_dependency_digest=self._champion_dependency,
                measurements={name: dict(value) for name, value in self._measurements.items()},
                accounted=frozenset(self._accounted),
                counts=dict(self._counts),
                denominator=self.denominator,
                alpha_rounds=dict(self._alpha_rounds),
                threshold=None if self.threshold is None else dict(self.threshold),
                finished=None if self.finished is None else dict(self.finished),
                capacity=None if self.capacity is None else dict(self.capacity),
                first_round=self.first_round,
                total_rounds=self.total_rounds,
                adaptive=True,
                family_version=self._family_version,
                identity_digest=self.identity_digest,
                producing_version=self.producing_version,
                creation_version=self._creation_version,
                completion_versions=dict(self._completion_versions),
                import_versions=dict(self._import_versions),
                rebases=tuple(dict(entry) for entry in self._rebases),
                hygiene_admitted=frozenset(self._hygiene),
                admitted_versions=self._admitted_versions(),
                lineage_counts=(
                    None if self._lineage_counts is None else dict(self._lineage_counts)
                ),
                lineage_denominator=self._lineage_denominator,
                excluded={name: dict(stages) for name, stages in self._excluded.items()},
                exclusions={name: dict(entry) for name, entry in self._exclusions.items()},
                owed_terminal=self._owed_terminal,
                cancelled_role_seq=self._cancelled_role_seq,
                baseline_abandoned=(
                    None if self._baseline_abandoned is None else dict(self._baseline_abandoned)
                ),
                suspensions=tuple(dict(entry) for entry in self._suspensions),
                exhausted_window_counts={
                    owner: window.count(False) for owner, window in self._threshold_windows.items()
                },
                window_start=self._last_suspension_seq,
                window_base_counts=dict(self._window_base_counts),
                window_base_denominator=self._window_base_denominator,
                owner_window={
                    owner: (window["start"], window["count"], window["denominator"])
                    for owner, window in self._owner_windows.items()
                },
                repair_owed=self._repair_owed,
                threshold_named=self._threshold_named(),
                repairs_reserved=dict(self._dispatch),
                stage_positions=dict(self._stage_seq),
                retries_declined=frozenset(
                    key for key, follows in self._retries_reserved.items() if not follows
                ),
                listing_restored=frozenset(self._restored),
                suspension_causes=dict(self._suspension_causes),
                target_records=dict(self._target_records),
                suspended=self._last is not None and self._last.event == "campaign_suspended",
                epochs=tuple(dict(entry) for entry in self._epochs),
                creation_identity_digest=self.creation_identity_digest,
                pending_change=(
                    None
                    if self._pending_change is None
                    else {
                        **self._pending_change,
                        "fields": list(self._pending_change["fields"]),
                    }
                ),
                condition_changes=tuple(
                    {**entry, "fields": list(entry["fields"])} for entry in self._condition_changes
                ),
                max_consecutive_failures=self._reflection_threshold,
                rebaseline_from_round=self._rebaseline_from_round,
                refreshes=tuple(_immutable_binding(entry) for entry in self._refreshes),
                grouper_chain=self._grouper_chain,
                grouper_kind=(
                    str(self._grouping_policy["kind"])
                    if (self._grouping_policy is not None)
                    else None
                ),
                connection=self._connection_view(),
                cited_ends=MappingProxyType(dict(self._cited_ends)),
            )
        return self._snapshot

    # -- helpers --------------------------------------------------------

    @property
    def policy(self) -> CampaignPolicyV11:
        return _need(self._policy, "the campaign is not created")

    @property
    def last_round(self) -> int:
        return self.first_round + self.total_rounds - 1

    def _round(self, number: object, rule: str) -> RoundState:
        _durable_reference._refuse(isinstance(number, int), "round is not an integer")
        assert isinstance(number, int)
        return _need(self._rounds.get(number), rule)

    def _attempt(self, attempt_id: object) -> AttemptState:
        _durable_reference._refuse(isinstance(attempt_id, str), "attempt_id is not a string")
        number = _need(self._attempt_round.get(str(attempt_id)), "attempt is not opened")
        return self._rounds[number].attempts[str(attempt_id)]

    def _put_round(self, state: RoundState) -> None:
        self._rounds[state.round] = state

    def _put_attempt(self, attempt: AttemptState) -> None:
        state = self._rounds[attempt.round]
        attempts = dict(state.attempts)
        attempts[attempt.attempt_id] = attempt
        self._rounds[attempt.round] = replace(state, attempts=attempts)

    def _admitted_versions(self) -> frozenset[str]:
        """Every producing version this prefix was written by (ADR-0015)."""

        versions = {self._creation_version} if self._creation_version else set()
        return frozenset(versions | {entry["producing_version"] for entry in self._rebases})

    def _all_looks_charged(self, attempt: AttemptState) -> bool:
        """Current policies have no alpha ledger; evaluated looks charge nothing."""
        return True

    # -- rules ----------------------------------------------------------

    def _prepare_created(
        self, record: Envelope, v: dict[str, Any], p: dict[str, Any], sequence: int
    ) -> LifecycleTransition:
        _durable_reference._refuse(
            v["event"] == "campaign_created", "campaign_created is the first record"
        )
        _durable_reference._refuse(
            p.get("family_version") == self._family_version,
            "family version is not the lifecycle's",
        )
        policy: CampaignPolicyV11
        data, policy = _adaptive_identity_and_policy(p["identity"])
        store = data["artifact_store"]
        linked = data["linked_from"]
        lineage_counts: dict[str, int] | None = None
        lineage_denominator: int | None = None
        if linked is not None:
            # Continuable-terminals spec §5.2/§5.3: the successor starts after
            # every round its predecessor named, and its thresholds count only
            # its own tries; the inherited totals are lineage, not state.
            _durable_reference._refuse(
                linked.get("first_round") is not None, "family 6 linked_from needs first_round"
            )
            # Ruling R12: the family the predecessor journal was read in, which
            # a family-6 or -7 producer links only from families 5, 6 and 7
            # (spec §6.3; I1 adds family 7).
            _durable_reference._refuse(
                linked.get("predecessor_family") is not None,
                "family 6 linked_from needs predecessor_family",
            )
            # Family-13 D10: a family-13 creation links family 13 only, and no
            # earlier family lists it. The predecessor's mode is not journaled,
            # so the mode refusal is the producer's alone (F7 plan choice 12).
            # ADR-0046: recorded family-13 predecessors remain valid in
            # family-14/15 creations; only the new-link resolver excludes 13.
            _durable_reference._refuse(
                linked["predecessor_family"]
                in ((13, 14, 15) if self._independent else ((13, FINAL_CAMPAIGN_FAMILY_VERSION))),
                "linked_from.predecessor_family is not a linkable family",
            )
            first = int(linked["first_round"])
            counts = dict.fromkeys(OWNERS, 0)
            denominator = 0
            lineage_counts = {owner: int(linked["indeterminate_counts"][owner]) for owner in OWNERS}
            lineage_denominator = int(linked["denominator"])
        else:
            first = 1 if linked is None else int(linked["rounds_closed"]) + 1
            counts = (
                dict.fromkeys(OWNERS, 0)
                if linked is None
                else {owner: int(linked["indeterminate_counts"][owner]) for owner in OWNERS}
            )
            denominator = 0 if linked is None else int(linked["denominator"])
        alpha_rounds = (
            {}
            if linked is None
            else {int(key): value for key, value in linked["alpha_rounds"].items()}
        )
        run_id = v["run_id"]
        alpha_attempt = {} if p["alpha_attempt"] is None else dict(p["alpha_attempt"])
        identity_digest = str(p["identity_digest"])
        version = str(p["producing_version"])
        adaptation = p.get("adaptation")
        adaptation_payload = p.get("adaptation_payload")
        grouping_policy = (
            dict(adaptation_payload["grouping"]) if (isinstance(adaptation_payload, dict)) else None
        )
        reflection_threshold = self._threshold_of(adaptation_payload)
        group_text_chars = self._group_text_chars_of(adaptation_payload)
        evidence = pin_from_adaptation(adaptation_payload or {})

        def create() -> None:
            _durable_reference._refuse(self.sequence == sequence, "stale campaign transition")
            self.created, self.run_id = record, run_id
            if self._family15 is not None:
                self._family15.remember(record)
            self.identity, self._policy = data, policy
            self.identity_digest = identity_digest
            self.creation_identity_digest = identity_digest
            self._epochs = [dict(data)]
            self.producing_version = self._creation_version = version
            declared = (linked or {}).get("baseline") or {}
            self._declared_import = int(declared.get("imported", 0)) > 0
            self._adaptation = None if adaptation is None else dict(adaptation)
            self._grouping_policy = grouping_policy
            self._reflection_threshold = reflection_threshold
            self._skip_chain_limit = skip_chain_limit(p["adaptation_payload"])
            self._group_text_chars = group_text_chars
            self._evidence = evidence
            self.alpha_attempt = alpha_attempt
            self.first_round, self.total_rounds = first, policy.rounds
            self._champion = (SEED_VERSION, store["seed_candidate_digest"])
            self._champion_dependency = store["seed_dependency_digest"]
            self._versions = {SEED_VERSION}
            self._counts, self.denominator = counts, denominator
            self._lineage_counts = lineage_counts
            self._lineage_denominator = lineage_denominator
            self._alpha_rounds = alpha_rounds
            self._last = record
            self.sequence += 1
            self._valid_bytes += len(record.encode())
            self._snapshot = None

        return LifecycleTransition(1, create)

    def _rule(
        self, event: str, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        if self._family15 is not None:
            delta = self._family15.rule(event, p, op, seq, record)
            if delta is not None:
                return delta
        rules: Mapping[str, Callable[[dict[str, Any], str | None], Callable[[], None]]] = {
            "baseline_bound": self._baseline_bound,
            "baseline_complete": self._baseline_complete,
            "baseline_refresh_bound": lambda p, op: self._baseline_refresh_bound(p, op, seq),
            "baseline_refresh_completed": lambda p, op: self._baseline_refresh_completed(
                p, op, seq
            ),
            CONNECTION_START_EVENT: lambda p, op: self._connection_started(p, op, seq),
            CONNECTION_FINISHED_EVENT: lambda p, op: self._connection_terminal(
                CONNECTION_FINISHED_EVENT, p, op, seq
            ),
            CONNECTION_FAILED_EVENT: lambda p, op: self._connection_terminal(
                CONNECTION_FAILED_EVENT, p, op, seq
            ),
            CONNECTION_INTERRUPTED_EVENT: lambda p, op: self._connection_terminal(
                CONNECTION_INTERRUPTED_EVENT, p, op, seq
            ),
            "champion_complete": self._champion_complete,
            "attempt_opened": self._attempt_opened,
            "editor_started": self._editor_started,
            "editor_finished": self._editor_terminal,
            "editor_failed": self._editor_terminal,
            "attempt_sealed": self._attempt_sealed,
            "candidate_inspected": self._candidate_inspected,
            "affected_cases_selected": self._affected_cases_selected,
            "measurement_bound": self._measurement_bound,
            "parent_measurement_bound": self._parent_measurement_bound,
            "comparison_bound": lambda p, op: self._comparison_bound(p, op, seq),
            "noise_calibration_bound": lambda p, op: self._noise_calibration_bound(p, op, seq),
            "noise_calibration_completed": lambda p, op: self._noise_calibration_completed(
                p, op, seq
            ),
            "comparison_look_evaluated": lambda p, op: self._comparison_look_evaluated(
                p, op, seq, record
            ),
            "finalists_bound": lambda p, op: self._finalists_bound(p, op, seq),
            campaign_vocabulary_for(14).measurement_bound: lambda p, op: (
                self._cohort_measurement_bound(p, op, seq)
            ),
            campaign_vocabulary_for(14).measurement_completed: lambda p, op: (
                self._cohort_measurement_completed(p, op, seq, record)
            ),
            "global_comparison_completed": lambda p, op: self._global_comparison_completed(
                p, op, seq, record
            ),
            "stage_opened": lambda p, op: self._stage_opened(p, op, seq),
            "instance_attested": self._instance_attested,
            "indeterminate_observed": lambda p, op: self._indeterminate_observed(p, op, seq),
            "pairs_excluded": self._pairs_excluded,
            "repairs_dispatched": lambda p, op: self._repairs_dispatched(p, op, seq),
            "stage_redispatched": lambda p, op: self._stage_redispatched(p, op, seq),
            "threshold_reached": self._threshold_reached,
            "look_not_started": self._look_not_started,
            "capacity_exhausted": self._capacity_exhausted,
            "attempt_closed": lambda p, op: self._attempt_closed(p, op, seq, record),
            "selection_decided": self._selection_decided,
            "round_closed": self._round_closed,
            "campaign_finished": self._campaign_finished,
            "focus_selected": lambda p, op: self._focus_selected(p, op, seq),
            "grouping_started": lambda p, op: self._grouping_started(p, op, seq),
            "grouping_finished": lambda p, op: self._grouping_terminal(p, op, seq, record),
            "grouping_failed": lambda p, op: self._grouping_terminal(p, op, seq, record),
            "focus_portion_selected": lambda p, op: self._focus_portion_selected(p, op, seq),
            "attempt_input_frozen": self._attempt_input_frozen,
            "probe_checked": lambda p, op: self._probe_checked(p, op, seq, record),
            "reflection_started": lambda p, op: self._reflection_started(p, op, seq),
            "probe_completed": self._probe_completed,
            "reflection_finished": lambda p, op: self._reflection_terminal(p, op, seq),
            "reflection_failed": lambda p, op: self._reflection_terminal(p, op, seq),
            "gate_scope_selected": lambda p, op: self._gate_scope_selected(p, op, seq),
            "identity_rebased": lambda p, op: self._identity_rebased(p, op, seq),
            "authorization_applied": lambda p, op: self._authorization_applied(p, op, seq),
            "measurement_rebound": self._measurement_rebound,
            "campaign_suspended": lambda p, op: self._campaign_suspended(p, op, seq),
        }
        rule = _need(rules.get(event), "event has no campaign rule")
        return rule(p, op)

    def _identity_rebased(self, p: dict[str, Any], op: str | None, seq: int) -> Callable[[], None]:
        """PDR-0010: move the run's identity onto the one the operator accepted.

        The shape is already proved (`validate_payload`); what only the prefix
        knows is whether this record follows *this* identity and describes the
        move honestly. The comparison is the very one the refusal was computed
        from, so a `changed_fields` that hides a comparable change is refused
        here even though the declared list passed the family's own check.
        """

        original_partition = (
            self.created.payload["adaptation_payload"]["corpus_partition"] if self.created else None
        )
        _durable_reference._refuse(
            p["adaptation_payload"]["corpus_partition"] == original_partition,
            "corpus partition cannot be rebased",
        )
        # G2 spec §4: family 12 keeps its `/13` policy, and no earlier family
        # gains one, whatever else the rebase moves.
        _, rebased_policy = _adaptive_identity_and_policy(p["identity"])
        _durable_reference._refuse(
            isinstance(rebased_policy, CampaignPolicyV11),
            "a rebase keeps the family's /13 policy choice",
        )
        _durable_reference._refuse(
            isinstance(rebased_policy, CampaignPolicyV11),
            "a rebase keeps the family's /14 policy choice",
        )
        _durable_reference._refuse(
            isinstance(rebased_policy, CampaignPolicyV11),
            "a rebase keeps the family's /15 policy choice",
        )
        _durable_reference._refuse(
            (type(rebased_policy) is CampaignPolicyV12) == self._independent,
            "a rebase keeps the family's /16 policy choice",
        )
        _validate_provenance_labels_at_creation(p, rebased_policy)
        _durable_reference._refuse(
            p["previous_digest"] == self.identity_digest,
            "previous_digest is not the latest identity",
        )
        journaled = identity_record(
            {
                "identity": self.identity,
                "identity_digest": self.identity_digest,
                "producing_version": self.producing_version,
                "adaptation": self._adaptation,
            }
        )
        # `linked_from` is not comparable -- a continuation's baseline recovery is
        # not part of the measured condition -- but the prefix's first round,
        # counts, denominator and alpha ledger were all derived from it at
        # `campaign_created`. Moving it would leave that accounting stale, so a
        # rebase may not touch it.
        _durable_reference._refuse(
            p["identity"].get("linked_from") == self.identity.get("linked_from"),
            "identity_rebased changes linked_from",
        )
        differences = identity_differences(journaled, identity_record(p))
        _durable_reference._refuse(bool(differences), "identity_rebased changes nothing")
        # Family 7 (I2 spec §5.5): only the rebase directly after an applied
        # `continue`/`rebaseline` may move a comparable field.
        block = self._pending_change
        in_place = block is not None and block["phase"] == "rebase"
        if not in_place:
            _durable_reference._refuse(
                not comparable_changed(differences, family_version=self._family_version),
                "identity_rebased changes a comparable field",
            )
        _durable_reference._refuse(
            sorted(difference.field for difference in differences) == sorted(p["changed_fields"]),
            "changed_fields do not match the identity",
        )
        identity = dict(p["identity"])
        digest = str(p["identity_digest"])
        version = str(p["producing_version"])
        adaptation = dict(p["adaptation"])
        grouping_policy = dict(p["adaptation_payload"]["grouping"])
        reflection_threshold = self._threshold_of(p["adaptation_payload"])
        group_text_chars = self._group_text_chars_of(p["adaptation_payload"])
        evidence = pin_from_adaptation(p["adaptation_payload"])
        entry = dict(
            previous_digest=p["previous_digest"],
            identity_digest=digest,
            producing_version=version,
            changed_fields=list(p["changed_fields"]),
            reason=p["authorization"]["reason"],
            # Not published: §6.2's owed `store_rebased` is completed by a later
            # invocation under *this* record's authorization, so the id has to
            # survive in the snapshot. The projections copy the five public keys.
            authorization_id=p["authorization"]["id"],
        )

        opens_epoch = comparable_changed(differences, family_version=self._family_version)
        change = self._rebase_in_place(p, differences, seq) if in_place else None

        def apply() -> None:
            self.identity = identity
            self.identity_digest = digest
            self.producing_version = version
            self._adaptation = adaptation
            self._grouping_policy = grouping_policy
            self._reflection_threshold = reflection_threshold
            self._skip_chain_limit = skip_chain_limit(p["adaptation_payload"])
            # The open round keeps the limit it opened with (C6).
            self._group_text_chars = group_text_chars
            self._evidence = evidence
            self._rebases.append(entry)
            # ADR-0020: a comparable change opens the next condition epoch; a
            # non-comparable one opens none. Only an in-place change's rebase
            # is comparable (the refusal above), and every measurement bound
            # before it is frozen: never appended to again (ADR-0008's third
            # exception).
            if opens_epoch:
                self._epochs.append(dict(identity))
                for measurement in self._measurements.values():
                    measurement["frozen"] = True
            if change is not None:
                change()

        return apply

    def _open_round(self) -> int | None:
        """The latest round that has begun (its baseline is bound) and is not closed."""

        begun = [
            number
            for number, state in self._rounds.items()
            if state.baseline_measurement_id is not None and state.closed is None
        ]
        return max(begun) if begun else None

    def _rebase_in_place(
        self, p: dict[str, Any], differences: Sequence[Any], seq: int
    ) -> Callable[[], None]:
        """I2 spec §5.4-§5.5: the rebase of an applied `continue`/`rebaseline`.

        It must be the decision's own (its id), move the identity to exactly
        the observed triple the decision's instance was computed over, and
        its changed comparable fields must admit the effect under the new
        policy's round budget (§3.2-§3.3) -- the admissibility is proved here
        because only this record carries the full new identity. It opens the
        next epoch (`_identity_rebased`), records the change and moves the
        block on: `continue` to its rebinding, `rebaseline` to closing the
        open round, or -- between rounds -- ends the block at once.
        """

        block = _need(self._pending_change, "no condition change is applied")
        decision = _need(self._change_decision, "no condition change is applied")
        effect = str(block["effect"])
        _durable_reference._refuse(
            p["authorization"]["id"] == decision.id,
            "the rebase does not carry the applied decision",
        )
        record = identity_record(p)
        _durable_reference._refuse(
            {name: record[name] for name in _TRIPLE_KEYS} == self._change_observed,
            "the rebase is not the observed identity",
        )
        _durable_reference._refuse(
            p["instance_digest"] == self._change_instance,
            "the rebase names another instance",
        )
        fields = sorted(
            {
                str(difference.field).split(".")[1]
                for difference in differences
                if comparable_field(str(difference.field), family_version=self._family_version)
            }
        )
        if effect == "continue":
            _durable_reference._refuse(
                set(fields) <= AGENT_SIDE_FIELDS, "continue moves only the agent's side"
            )
        _, policy = _adaptive_identity_and_policy(p["identity"])
        # Family 7 is created only from a `/8` policy and never leaves it.
        _durable_reference._refuse(
            isinstance(policy, CampaignPolicyV11), "family 7 requires a /8 policy"
        )
        begun = [n for n, s in self._rounds.items() if s.baseline_measurement_id is not None]
        # Spec §3.4, the durable backstop of admission's `policy_contradicts_prefix`
        # and the same rule (`policy_contradiction`): a policy cannot ask for
        # fewer rounds than this run has begun, nor a round alpha budget below
        # what a round of this run already consumed (that round's
        # `alpha_consumed` rows would not replay under it; a linked
        # predecessor's rounds were charged under its own policy).
        _durable_reference._refuse(
            policy_contradiction(
                self.snapshot,
                policy.rounds,
                policy.alpha_budget if policy.declares_alpha else None,
            )
            is None,
            "the new policy asks for less than the run has spent",
        )
        offered = admissible_effects(
            self._family_version, differences, rounds_remaining(self.snapshot, policy.rounds)
        )
        _durable_reference._refuse(
            effect in offered, "the effect is not admissible for this change"
        )
        _durable_reference._refuse(
            decision.resolved_effect(offered) == effect,
            "the decision chose another effect",
        )
        open_round = self._open_round()
        # Ruling R9: before any round of this run has begun there is no round
        # boundary to move -- round 1 binds its ordinary `m-r00-champion` in
        # the new epoch. A declared baseline import is not born under that
        # `rebaseline` (the baseline is measured from empty): it is abandoned
        # as an applied `fresh_baseline` abandons it.
        next_round = max(begun) + 1 if begun else None
        linked = self.identity.get("linked_from")
        declared = linked.get("baseline") if isinstance(linked, Mapping) else None
        abandons_import = (
            effect == "rebaseline"
            and not begun
            and declared is not None
            and declared["imported"] > 0
            and self._baseline_abandoned is None
        )
        change = dict(
            seq=block["seq"],
            effect=effect,
            fields=fields,
            round=open_round,
            authorization_id=decision.id,
            identity_digest=str(p["identity_digest"]),
        )
        # §6.2: a changed policy governs from the next round on (`rebaseline`
        # only -- `continue` never moves `policy_digest`): its rounds, attempts,
        # looks and alpha schedule. Alpha already spent stays spent.
        moves_policy = p["identity"]["policy_digest"] != self.identity["policy_digest"]
        alpha_attempt = creation_alpha_attempt(policy)
        # §6.5 (ADR-0019 as amended): this record restarts the threshold window
        # of each owner whose field it changed, and no other. `apply` runs after
        # `_identity_rebased` appended the epoch it opens.
        restarted = restarted_owners(fields)

        def apply() -> None:
            self._condition_changes.append(change)
            for owner in restarted:
                self._owner_windows[owner] = dict(
                    start=seq, epoch=len(self._epochs) - 1, count=0, denominator=0
                )
                self._threshold_windows[owner].reset(seq)
            if moves_policy:
                self._policy = policy
                self.total_rounds = policy.rounds
                self.alpha_attempt = {} if alpha_attempt is None else alpha_attempt
            if effect == "continue":
                self._pending_change = {**block, "fields": fields, "phase": "rebind"}
            elif open_round is not None:
                self._pending_change = {**block, "fields": fields, "phase": "close_round"}
            else:
                self._pending_change = None
                if next_round is not None:
                    self._rebaseline_from_round = next_round
            if abandons_import:
                self._baseline_abandoned = {"authorization_id": decision.id}

        return apply

    def _authorization_applied(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """Collect one accepted hygiene instance, or (family 6) owe a terminal.

        A hygiene record moves nothing but the admitted set. A family-6
        `close`/`terminal` record (comparable-links spec §5.1-§5.2) must start
        from the identity the prefix ends on -- the triple `identity_record`
        gives it, the one the executor's refusal compared -- and its instance
        digest must recompute over the two triples it carries. It then owes
        exactly one terminal: `stopped`/`condition_changed` for `close`; for
        `terminal`, the recorded threshold's, or else `probe_unavailable` over
        an unavailable probe check (codex r1), or else the unresponsive role's
        (`editor_unresponsive`, then `reflector_unresponsive`; controller
        ruling 6).
        """

        effect = p.get("effect", "hygiene")
        if effect == "hygiene":
            digest = str(p["instance_digest"])

            def apply() -> None:
                self._hygiene.add(digest)

            return apply
        if effect == "fresh_baseline":
            return self._fresh_baseline(p)
        latest = identity_record(
            {
                "identity": self.identity,
                "identity_digest": self.identity_digest,
                "producing_version": self.producing_version,
                "adaptation": self._adaptation,
            }
        )
        current = {name: latest[name] for name in _TRIPLE_KEYS}
        _durable_reference._refuse(
            p["journaled"] == current, "the close does not start from the latest identity"
        )
        _durable_reference._refuse(
            identity_instance_digest(p["journaled"], p["observed"]) == p["instance_digest"],
            "the instance digest does not recompute",
        )
        _durable_reference._refuse(
            p["observed"]["identity_digest"] != current["identity_digest"],
            "a close needs a changed identity",
        )
        # PDR-0012 and controller ruling 6: a
        # durable unavailable probe check, an unresponsive editor and an
        # unresponsive Reflector each determine the campaign's terminal just as
        # a recorded threshold does (`_check_finished` admits
        # `probe_unavailable` -- with the check as its evidence -- against the
        # check, and the role terminals against these very flags). A close
        # would replace that terminal with `condition_changed`, so it is
        # refused; the `terminal` effect owes exactly the determined pair. The
        # precedence is `_check_finished`'s and the resume path's: an
        # unavailable check wins over an unresponsive role (its rule forces
        # `probe_unavailable` whatever else the prefix holds, and `stop`
        # replaces a pending role terminal with it), an unresponsive editor
        # over an unresponsive Reflector, and a recorded threshold over a role.
        # A threshold *and* an unavailable check admit no terminal at all --
        # each rule refuses the other's -- so the effect is refused rather than
        # owing a record the lifecycle would then refuse. The executor never
        # writes that pair: the planner stops at whichever comes first.
        if effect in IN_PLACE_EFFECTS:
            return self._apply_in_place(p, effect, seq)
        owed: tuple[str, str]
        _durable_reference._refuse(effect == "close", "family 7 owes no infrastructure terminal")
        _durable_reference._refuse(
            self.threshold is None,
            "a campaign past its threshold owes its threshold terminal",
        )
        owed = ("stopped", "condition_changed")

        def owe() -> None:
            self._owed_terminal = owed

        return owe

    def _apply_in_place(self, p: dict[str, Any], effect: str, seq: int) -> Callable[[], None]:
        """I2 spec §5.4-§5.5: open the transition block of a `continue`/`rebaseline`.

        Family 7 only (the shape refuses the effects elsewhere). The record's
        start from the latest identity and its instance digest are proved
        above, as for a close. Which effects the change admits needs the full
        new identity, which only the next record carries, so
        `_rebase_in_place` proves it there. A recorded threshold owes its own
        resolution (family 7 suspends on it), and a `rebaseline` over a durable
        selection is written only once that round has closed in the old epoch
        (§3.2), where the block sees no open round.
        """

        _durable_reference._refuse(
            self.threshold is None,
            "a campaign past its threshold owes its threshold terminal",
        )
        open_round = self._open_round()
        if effect == "rebaseline" and open_round is not None:
            state = self._rounds[open_round]
            _durable_reference._refuse(
                state.selection is None,
                "a rebaseline over a durable selection waits for its round_closed",
            )
            # I2 Task 8: the block closes the round, so no role operation of it
            # may still be open -- a started grouping, reflection or editor
            # would never get its terminal. The executor settles each one as
            # `interrupted` (the process that started it is gone) before it
            # writes this record.
            _durable_reference._refuse(
                state.grouping is None or state.grouping["status"] != "started",
                "a rebaseline waits for the open round's grouping to settle",
            )
            _durable_reference._refuse(
                not state.reflections or state.reflections[-1]["status"] != "started",
                "a rebaseline waits for the open round's reflection to settle",
            )
            _durable_reference._refuse(
                not any(
                    attempt.outcome is None and attempt.editor == "started"
                    for attempt in state.attempts.values()
                ),
                "a rebaseline waits for the open round's editors to settle",
            )
        try:
            decision = Authorization.from_decision_payload(p["authorization"])
        except AuthorizationError:  # pragma: no cover - the shape parsed it already
            _durable_reference._refuse(False, "authorization_applied decision")
        observed = dict(p["observed"])
        instance = str(p["instance_digest"])

        def apply() -> None:
            self._pending_change = {"effect": effect, "seq": seq, "fields": [], "phase": "rebase"}
            self._change_decision = decision
            self._change_observed = observed
            self._change_instance = instance

        return apply

    def _fresh_baseline(self, p: dict[str, Any]) -> Callable[[], None]:
        """Baseline inheritance §5.3, §7: release an owed copy, at most once.

        Admitted only when the identity declares `linked_from.baseline`, only
        while the first-round baseline has no recorded stage (the copy is
        still owed: nothing was measured on it), and only for the instance
        this run's own `run_id` and the declared `pairs_digest` name. It owes
        no terminal; the baseline is then born empty and measured fresh.
        """

        linked = self.identity.get("linked_from")
        baseline = linked.get("baseline") if isinstance(linked, Mapping) else None
        declared = _need(baseline, "fresh_baseline needs a declared baseline import")
        _durable_reference._refuse(
            self._baseline_abandoned is None, "the baseline import is already abandoned"
        )
        _durable_reference._refuse(
            p["pairs_digest"] == declared["pairs_digest"],
            "fresh_baseline names another pairs_digest",
        )
        _durable_reference._refuse(
            p["instance_digest"]
            == baseline_import_instance_digest(str(self.run_id), declared["pairs_digest"]),
            "the instance digest does not recompute",
        )
        first = self._rounds.get(self.first_round)
        measurement = None if first is None else first.baseline_measurement_id
        bound = None if measurement is None else self._measurements.get(measurement)
        _durable_reference._refuse(
            bound is None or not bound["stages"],
            "the first-round baseline is already measured",
        )
        authorization_id = str(p["authorization"]["id"])

        def apply() -> None:
            self._baseline_abandoned = {"authorization_id": authorization_id}

        return apply

    def _baseline_bound(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        _durable_reference._refuse(
            self.first_round <= number <= self.last_round,
            "round is outside the campaign's own range",
        )
        existing = self._rounds.get(number)
        _durable_reference._refuse(
            existing is None or existing.baseline_measurement_id is None,
            "the round already has a baseline",
        )
        _durable_reference._refuse(
            existing is None or not any(a.outcome is None for a in existing.attempts.values()),
            "the round has open attempts",
        )
        # I2 spec §3.2: the round a `rebaseline` starts binds a fresh baseline
        # under its epoch id, and an owed winner completion of the round
        # before it is not performed -- the fresh baseline replaces it.
        fresh = self._rebaseline_from_round is not None and number == self._rebaseline_from_round
        completion: str | None = None
        cohort_baseline: str | None = None
        if number > self.first_round:
            previous = self._round(number - 1, "the previous round does not exist")
            closed = _need(previous.closed, "the previous round is not closed")
            # Family 13 completes no winner: the selected cohort measurement
            # is complete before the selection (design §6.12).
            _durable_reference._refuse(
                fresh
                or closed["outcome"] != "promoted"
                or previous.champion_complete
                or self._confidence
                or self._independent,
                "the promoted winner's measurement is not complete",
            )
            if (not fresh) and (closed["outcome"] == "promoted"):
                completion = previous.completion_measurement_id
            if (not fresh) and (closed["outcome"] in ("promoted", "retained")):
                selection = _need(previous.selection, "the previous round has no selection")
                cohort_baseline = selection["measurement_id"]
        _durable_reference._refuse(
            p["champion_version"] == self._champion[0]
            and p["candidate_digest"] == self._champion[1],
            "the baseline must name the current champion",
        )
        measurement = p["measurement_id"]
        refresh = self._refresh_entry(number)
        _durable_reference._refuse(
            refresh is not None or measurement != refresh_measurement_id_for(number),
            "the refresh measurement needs its baseline_refresh_bound",
        )
        if number > self.first_round and not fresh:
            # F0 R43: the shared next-baseline rule, or -- once N rounds
            # closed on it -- the refresh, which replaces the selection's
            # stored measurement (the cohort check above is the same rule).
            cohort_baseline = None
            if self._refresh_due(number) is not None:
                _durable_reference._refuse(
                    refresh is not None, "a due refresh is bound before the round's baseline"
                )
                _durable_reference._refuse(
                    measurement == refresh_measurement_id_for(number),
                    "a refreshed round binds its refresh measurement",
                )
            else:
                _durable_reference._refuse(refresh is None, "the round's refresh is not due")
                _durable_reference._refuse(
                    measurement
                    == family14_next_baseline(
                        self._rounds,
                        first_round=self.first_round,
                        rebaseline_from_round=self._rebaseline_from_round,
                        epoch=len(self._epochs) - 1,
                        round_number=number,
                    ),
                    "a family-14 round's baseline is the derived next baseline",
                )
        if completion is not None:
            # G2 spec §4.7: the completion, not the sealed winner, is the
            # next round's baseline.
            _durable_reference._refuse(
                measurement == completion,
                "a family-12 round's baseline is the previous round's completion",
            )
        if cohort_baseline is not None:
            _durable_reference._refuse(
                measurement == cohort_baseline,
                self._member_rule(
                    "a family-13 round's baseline is the previous round's selected cohort measurement"
                ),
            )
        if fresh:
            _durable_reference._refuse(
                measurement == self._fresh_baseline_id(number),
                "the fresh baseline keeps its epoch id",
            )
        bound = self._measurements.get(measurement)
        _durable_reference._refuse(
            bound is None or bound["candidate_digest"] == self._champion[1],
            "the bound measurement measures another candidate",
        )

        # #282 (C6): the round opens under the adaptation identity in force.
        opened = RoundState(
            round=number, group_text_chars=self._group_text_chars, evidence=self._evidence
        )

        def apply() -> None:
            state = self._rounds.get(number, opened)
            self._put_round(replace(state, baseline_measurement_id=measurement))

        return apply

    def _fresh_baseline_id(self, number: int) -> str:
        """`m-r<NN>-champion@e<k>`: a `rebaseline`'s fresh baseline (I2 spec §5.2)."""

        return fresh_baseline_id(number, len(self._epochs) - 1)

    def _refresh_entry(self, number: int) -> dict[str, Any] | None:
        """Family 14: round `number`'s `baseline_refresh_bound` entry, if any."""

        return next((entry for entry in self._refreshes if entry["round"] == number), None)

    def _refresh_due(self, number: int) -> RefreshDue | None:
        """Family 14 (F0 R42): the refresh round `number` owes, derived from the prefix.

        `None` under N = 0 (and on every older policy, whose N is `None`), at
        the `rebaseline` round (its fresh id matches no earlier round) and
        below N counted closures.
        """

        return refresh_due(
            self._rounds,
            first_round=self.first_round,
            rebaseline_from_round=self._rebaseline_from_round,
            epoch=len(self._epochs) - 1,
            round_number=number,
            after_rounds=self.policy.baseline_refresh_after_rounds,
        )

    def _check_refresh_completed(self, state: RoundState) -> None:
        """Family 14 (F0 R43): nothing reads a refreshed round's baseline --
        grouping, its focus portion, its close -- before `baseline_refresh_completed`.

        A refresh a `rebaseline` superseded (the round bound its fresh id) stays
        uncompleted and holds nothing up. A no-op in every older family.
        """

        refresh = self._refresh_entry(state.round)
        _durable_reference._refuse(
            refresh is None
            or refresh["completed"] is not None
            or base_measurement_id(str(state.baseline_measurement_id)) != refresh["measurement_id"],
            "the round's refresh is not completed",
        )

    def _baseline_refresh_bound(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """F0 R43, ADR-0032 (amended 2026-10-09): open round `number`'s refresh.

        Written before the round's `baseline_bound`, once N rounds closed on
        the same stored Champion measurement. Every value is re-derived from
        the prefix (`refresh_due`), so the payload's N, counted range and
        replaced measurement must be exactly the due refresh's; `evidence[0]`
        is the replaced baseline's own `baseline_reference`. Which campaign
        record `evidence[1]` cites (the last counted `round_closed`) is the
        audit's E fact.
        """

        after = self.policy.baseline_refresh_after_rounds
        _durable_reference._refuse(bool(after), "the policy refreshes no baseline")
        number = p["round"]
        _durable_reference._refuse(
            self.first_round <= number <= self.last_round,
            "round is outside the campaign's own range",
        )
        existing = self._rounds.get(number)
        _durable_reference._refuse(
            existing is None or existing.baseline_measurement_id is None,
            "the round already has a baseline",
        )
        _durable_reference._refuse(
            self._refresh_entry(number) is None, "the round already has a refresh"
        )
        _durable_reference._refuse(number > self.first_round, "no refresh is due for the round")
        previous = self._round(number - 1, "the previous round does not exist")
        _need(previous.closed, "the previous round is not closed")
        _durable_reference._refuse(
            number != self._rebaseline_from_round,
            "the rebaseline round binds a fresh baseline",
        )
        due = _need(self._refresh_due(number), "no refresh is due for the round")
        _durable_reference._refuse(
            p["baseline_refresh_after_rounds"] == due.after_rounds,
            "the refresh names the policy's baseline_refresh_after_rounds",
        )
        _durable_reference._refuse(
            p["counted_rounds"] == {"first": due.first, "last": due.last},
            "the refresh's counted rounds are not the derived count",
        )
        replaced = p["replaced"]
        # When a refresh is due the replaced measurement is the previous
        # round's own baseline: no Promotion or unstable retention moved it.
        _durable_reference._refuse(
            replaced["measurement_id"] == due.replaced == previous.baseline_measurement_id,
            "the refresh replaces the round's derived baseline",
        )
        _durable_reference._refuse(
            replaced["prefix_digest"] == previous.baseline_prefix_digest,
            "the refresh names the replaced baseline's prefix digest",
        )
        reference = previous.baseline_reference
        _durable_reference._refuse(
            reference is not None and p["evidence"][0] == dict(reference),
            "the refresh cites the replaced baseline's reference",
        )
        entry = {key: copy.deepcopy(value) for key, value in p.items() if key != "evidence"}
        entry.update(seq=seq, completed=None)

        def apply() -> None:
            self._refreshes.append(entry)

        return apply

    def _baseline_refresh_completed(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """F0 R43: close round `number`'s refresh once its baseline is complete.

        It names the round's current baseline -- the refresh measurement, or
        its `@e<k>` rebind after a `continue` -- and cites exactly the
        round's `baseline_reference`. A refresh superseded by a `rebaseline`
        (the round bound the fresh id) or left open by a `condition_changed`
        close is never completed.
        """

        _durable_reference._refuse(
            bool(self.policy.baseline_refresh_after_rounds), "the policy refreshes no baseline"
        )
        number = p["round"]
        entry = _need(self._refresh_entry(number), "the round has no refresh")
        _durable_reference._refuse(entry["completed"] is None, "the refresh is already completed")
        state = self._round(number, "the refresh's baseline is not complete")
        _durable_reference._refuse(
            state.baseline_complete, "the refresh's baseline is not complete"
        )
        _durable_reference._refuse(state.closed is None, "the round is already closed")
        current = _need(state.baseline_measurement_id, "the refresh's baseline is not complete")
        _durable_reference._refuse(
            base_measurement_id(current) == entry["measurement_id"],
            "the round's baseline is not its refresh measurement",
        )
        _durable_reference._refuse(
            p["measurement_id"] == current,
            "the refresh completion names the round's baseline",
        )
        reference = state.baseline_reference
        _durable_reference._refuse(
            reference is not None and p["evidence"][0] == dict(reference),
            "the refresh completion cites the round's baseline reference",
        )
        completed = {"measurement_id": current, "seq": seq}

        def apply() -> None:
            entry["completed"] = completed

        return apply

    def _baseline_complete(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(
            state.baseline_measurement_id == p["measurement_id"],
            "the baseline measurement is not bound for that round",
        )
        _durable_reference._refuse(not state.baseline_complete, "the baseline is already complete")
        measurement = p["measurement_id"]
        bound = _need(self._measurements.get(measurement), "the measurement is not declared")
        # A complete frozen measurement stays a valid baseline (ADR-0020): a
        # later round reusing a promoted winner completes its baseline without
        # appending to it. Only an unfinished frozen one is never completed.
        # A family-13 cohort member counts as complete once its completion
        # says so (F7 plan choice 14, `recorded_complete`).
        _durable_reference._refuse(
            not bound.get("frozen", False) or self._recorded_complete(bound),
            "a frozen measurement is never appended to",
        )
        _durable_reference._refuse(
            not self._unreserved_threshold_repair(str(p["measurement_id"])),
            "a threshold repair is owed",
        )

        settled: dict[str, Any] = {"baseline_prefix": str(p["evidence"][0]["locator"])}
        settled["baseline_reference"] = p["evidence"][0]

        def apply() -> None:
            self._put_round(replace(self._rounds[number], baseline_complete=True, **settled))
            self._measurements[measurement]["complete"] = True
            self._completion_versions[measurement] = self.producing_version

        return apply

    def _champion_complete(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        closed = _need(state.closed, "the round is not closed")
        _durable_reference._refuse(closed["outcome"] == "promoted", "the round did not promote")
        _durable_reference._refuse(not state.champion_complete, "the winner is already complete")
        _need(state.selection, "the round has no selection")
        _durable_reference._refuse(
            state.completion_measurement_id == p["measurement_id"],
            "the record does not name the round's completion",
        )
        measurement = p["measurement_id"]
        bound = _need(self._measurements.get(measurement), "the measurement is not declared")
        _durable_reference._refuse(
            not bound.get("frozen", False), "a frozen measurement is never appended to"
        )
        _durable_reference._refuse(
            not self._unreserved_threshold_repair(str(p["measurement_id"])),
            "a threshold repair is owed",
        )

        def apply() -> None:
            self._put_round(replace(self._rounds[number], champion_complete=True))
            self._measurements[measurement]["complete"] = True
            self._completion_versions[measurement] = self.producing_version

        return apply

    def _attempt_opened(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(state.baseline_complete, "the round's baseline is not complete")
        _durable_reference._refuse(
            state.selection is None and state.closed is None, "the round is already decided"
        )
        attempt_id = p["attempt_id"]
        _durable_reference._refuse(attempt_id not in self._attempt_round, "attempt id is used once")
        index = p["attempt_index"]
        _durable_reference._refuse(
            index <= self.policy.attempts_per_round, "attempt index is outside the round"
        )
        _durable_reference._refuse(
            all(a.index != index for a in state.attempts.values()),
            "attempt index is used once in a round",
        )
        _durable_reference._refuse(
            len(state.attempts) < self.policy.attempts_per_round,
            "the round already opened every attempt",
        )
        # #91 spec §3.1: without immediate decisions a held Attempt has no
        # step of its own and takes no slot.
        holds = self._holds_attempts() and not decides_at_once(self.policy)
        _durable_reference._refuse(
            sum(
                1
                for a in state.attempts.values()
                if a.outcome is None and not (holds and self._awaiting_decision(a))
            )
            < self.policy.parallel_attempts,
            "parallel attempts bound",
        )
        _durable_reference._refuse(
            p["parent_version"] == self._champion[0] and p["parent_digest"] == self._champion[1],
            "an attempt starts from the frozen champion",
        )
        _durable_reference._refuse(
            state.gate_scope is not None, "attempt opens before the round's gate scope"
        )
        if self._family_version == 14 and self.policy.round_stop == "first_accepted":
            _durable_reference._refuse(
                all(
                    a.attempt_id in state.global_comparisons
                    for a in state.attempts.values()
                    if a.outcome == "finalist"
                ),
                "a finalist is compared before the next Attempt opens",
            )
            _durable_reference._refuse(
                not any(c["accepted"] for c in state.global_comparisons.values()),
                "an accepted finalist stops the round's Attempts",
            )

        def apply() -> None:
            self._attempt_round[attempt_id] = number
            self._put_attempt(
                AttemptState(
                    attempt_id=attempt_id,
                    round=number,
                    index=index,
                    parent_version=p["parent_version"],
                    parent_digest=p["parent_digest"],
                )
            )

        return apply

    def _editor_started(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        _durable_reference._refuse(
            p["operation_id"] == op, "the payload operation id must match the envelope"
        )
        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        # Spec §11's `interrupted` replay; family 10 (role resilience spec
        # §5.3) also re-runs a `provider`/`unknown` failure, but only once the
        # `campaign_suspended` it owes lies between the two.
        _durable_reference._refuse(
            attempt.editor is None
            or (
                editor_replayable(attempt, adaptive=True, resilient=True)
                and (attempt.editor_error == "interrupted" or attempt.editor_suspended)
            ),
            "the attempt already has an editor record",
        )
        frozen = _need(attempt.frozen_input, "adaptive editor requires frozen input")
        _durable_reference._refuse(
            p["input_digest"] == frozen["input_digest"],
            "editor input digest differs from frozen input",
        )
        _durable_reference._refuse(
            p["common_input_digest"] == frozen["common_input_digest"],
            "editor common input differs from frozen input",
        )

        def apply() -> None:
            self._put_attempt(
                replace(
                    self._attempt(p["attempt_id"]),
                    editor="started",
                    editor_reason=None,
                    editor_suspended=False,
                )
            )

        return apply

    def _editor_terminal(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        start = self.pending[str(op)]
        _durable_reference._refuse(
            start.event == START_EVENT, "the operation is not an editor call"
        )
        _durable_reference._refuse(
            start.payload["attempt_id"] == p["attempt_id"],
            "the editor terminal names another attempt",
        )
        _durable_reference._refuse(
            p["operation_id"] == op, "the payload operation id must match the envelope"
        )
        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(attempt.editor == "started", "the editor call is already closed")
        state = p["status"] if "status" in p else "failed"
        error = p.get("error_class")

        def apply() -> None:
            current = self._attempt(p["attempt_id"])
            self._put_attempt(
                replace(
                    current,
                    editor=state,
                    editor_error=error,
                    # The payload grammar admits it nowhere else.
                    editor_reason=p.get("reason"),
                    # The resilience grammar's `editor_finished.continued`.
                    # When absent, the prior value (`False` by default) is kept
                    # rather than overwritten.
                    editor_continued=(
                        bool(p["continued"]) if "continued" in p else current.editor_continued
                    ),
                )
            )
            if error == "unresponsive":
                self._unresponsive = True

        return apply

    def _open_reflection(
        self, state: RoundState, reflection_id: object
    ) -> tuple[int, dict[str, Any]]:
        """The round's open (`status == "started"`) reflection named *reflection_id*."""

        for index, entry in enumerate(state.reflections):
            if entry["reflection_id"] == reflection_id and entry["status"] == "started":
                return index, entry
        raise SchemaError("no open reflection with that id")

    def _focus_selected(self, p: dict[str, Any], op: str | None, seq: int) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(state.baseline_complete, "the round's baseline is not complete")
        _durable_reference._refuse(state.focus is None, "the round already has a focus")
        reason = p["reason"]
        _durable_reference._refuse(
            (p["focus_fingerprint"] is None) == (reason == "none"),
            "focus_fingerprint is present exactly when the round has a cluster to focus on",
        )
        entry = dict(p)
        entry.pop("evidence", None)
        entry["seq"] = seq

        def apply() -> None:
            self._put_round(replace(self._rounds[number], focus=entry))

        return apply

    def _prior_finished_grouping(
        self, baseline_binding: str, policy_digest: str
    ) -> dict[str, Any] | None:
        for number in sorted(self._rounds, reverse=True):
            grouping = self._rounds[number].grouping
            if (
                grouping is not None
                and grouping["status"] == "finished"
                and grouping["baseline_binding"] == baseline_binding
                and grouping["policy_digest"] == policy_digest
            ):
                return grouping
        return None

    def _grouping_started(self, p: dict[str, Any], op: str | None, seq: int) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(state.baseline_complete, "the round's baseline is not complete")
        self._check_refresh_completed(state)
        _durable_reference._refuse(op is not None, "grouping start has an operation id")
        current = state.grouping
        if current is not None:
            if current.get("status") == "failed" and current.get("code") != "interrupted":
                raise SchemaError("completed grouping failure is terminal")
            _durable_reference._refuse(
                current.get("status") == "failed" and current.get("code") == "interrupted",
                "the round already has grouping",
            )
        _durable_reference._refuse(
            self._prior_finished_grouping(p["baseline_binding"], p["policy_digest"]) is None,
            "one admitted grouping per baseline and policy",
        )
        policy = _need(self._grouping_policy, "the campaign has no grouping policy")
        _durable_reference._refuse(
            p["grouper_kind"] == policy.get("kind"),
            "grouper_kind is not the identity's grouping kind",
        )
        entry = dict(p)
        entry.update(status="started", seq=seq, operation_id=op)

        def apply() -> None:
            self._put_round(replace(self._rounds[number], grouping=entry))

        return apply

    def _grouping_terminal(
        self, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        start = self.pending[str(op)]
        _durable_reference._refuse(
            start.event == GROUPING_START_EVENT, "the operation is not a grouping call"
        )
        _durable_reference._refuse(
            start.payload["grouping_id"] == p["grouping_id"],
            "the grouping terminal names another grouping",
        )
        number = p["round"]
        state = self._round(number, "the round does not exist")
        grouping = _need(state.grouping, "the round has no grouping")
        _durable_reference._refuse(
            grouping["status"] == "started", "the grouping call is already closed"
        )
        _durable_reference._refuse(
            grouping["operation_id"] == op, "the grouping terminal operation differs"
        )
        if "result" in p:
            _durable_reference._refuse(
                p["result"]["registry_digest"] == grouping["registry_digest"],
                "grouping result names another registry",
            )
            entry = {
                **grouping,
                "status": "finished",
                "result": dict(p["result"]),
                "result_digest": p["result_digest"],
                "reads": dict(p["reads"]),
                "seq": seq,
                "record_digest": hashlib.sha256(record.encode()).hexdigest(),
            }
            # ADR-0057: the digest binding the grouping's full-description
            # records, present exactly when the terminal wrote some.
            if "full_descriptions_digest" in p:
                entry["full_descriptions_digest"] = p["full_descriptions_digest"]
            entry.update(self._grouping_outcome(p))
        else:
            entry = {
                **grouping,
                "status": "failed",
                "code": p["code"],
                "reads": dict(p["reads"]),
                "seq": seq,
                "record_digest": hashlib.sha256(record.encode()).hexdigest(),
            }
            if p["code"] == "role_failed":
                self._check_grouper_role_failed(p)
                entry.update(reason=p["reason"], error_class=p["error_class"])
        # Family 14 (plan decision 6): a fallback-free grouping breaks the
        # chain, a scope-`all` fallback extends it, and an `uncovered` one or
        # a `grouping_failed` leaves it.
        chain = self._grouper_chain
        if entry["status"] == "finished":
            fallback = entry["fallback"]
            if fallback is None:
                chain = 0
            elif fallback["scope"] == "all":
                chain += 1

        def apply() -> None:
            self._put_round(replace(self._rounds[number], grouping=entry))
            self._grouper_chain = chain

        return apply

    def _check_grouper_role_failed(self, p: Mapping[str, Any]) -> None:
        """Family 14's `grouping_failed(role_failed)` (ADR-0034, plan decisions 2, 3, 6).

        Only a `chat` or `agent_cli` Grouper fails with it, with one of its
        kind's reasons; `error_class` is that reason's class. A `content`
        failure is written only as the second consecutive full fallback, so the
        chain must stand at one; a provider or unknown failure needs no chain.
        """

        policy = _need(self._grouping_policy, "the campaign has no grouping policy")
        reasons = GROUPER_ROLE_FAILED_REASONS14.get(str(policy.get("kind")))
        _durable_reference._refuse(
            reasons is not None, "only a chat or agent_cli Grouper fails with role_failed"
        )
        assert reasons is not None
        _durable_reference._refuse(
            p["reason"] in reasons, "the role_failed reason is not the Grouper kind's"
        )
        _durable_reference._refuse(
            grouper_class(p["reason"]) == p["error_class"],
            "grouper error_class is not the reason's class",
        )
        _durable_reference._refuse(
            p["error_class"] != "content"
            or self._grouper_chain >= grouper_chain_limit({"grouping": policy}) - 1,
            "a content role_failed is only the second full fallback",
        )

    def _grouping_outcome(self, p: dict[str, Any]) -> dict[str, Any]:
        """Family 9's `fallback` and `calls`, held to the journaled policy (spec §10).

        Only a `model` policy calls a model, so only it carries `calls`, and
        carries them always; `fallback` is `null` without one. A `fields`
        fallback is the one the policy declared; a declared one never falls
        back to singletons.
        """

        policy = _need(self._grouping_policy, "the campaign has no grouping policy")
        return self._grouper_outcome(policy, p)

    def _grouper_outcome(self, policy: Mapping[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        """Family 14's `fallback` and `calls`, held to the identity's Grouper kind.

        ADR-0034, A§5.1, plan decisions 3 and 6: a `chat` or `agent_cli`
        Grouper journals its kind's calls shape always, and falls back only for
        its kind's reasons, with scope `all` for `input_too_large` and
        `turns_exhausted`, to the declared fallback (`fields`) or singletons.
        `fields` and `command` neither journal calls nor fall back. The second
        consecutive full fallback is not a `grouping_finished`: it is written as
        `grouping_failed(role_failed)` (`_check_grouper_role_failed`).
        """

        kind = str(policy.get("kind"))
        fallback, calls = p["fallback"], p["calls"]
        shape = GROUPER_CALL_KEYS14.get(kind)
        if shape is None:
            _durable_reference._refuse(
                fallback is None, "only a chat or agent_cli Grouper falls back"
            )
            _durable_reference._refuse(
                calls is None, "only a chat or agent_cli Grouper journals calls"
            )
        else:
            _durable_reference._refuse(
                calls is not None, "a chat or agent_cli Grouper journals its calls"
            )
            _durable_reference._refuse(
                set(calls) == shape, "the calls are not the Grouper kind's shape"
            )
            if fallback is not None:
                declared = "fields" if policy.get("fallback") is not None else "singletons"
                _durable_reference._refuse(
                    fallback["kind"] == declared, "the fallback is not the declared one"
                )
                _durable_reference._refuse(
                    fallback["reason"] in GROUPER_FALLBACK_REASONS_BY_KIND14[kind],
                    "the fallback reason is not the Grouper kind's",
                )
                _durable_reference._refuse(
                    fallback["reason"] not in GROUPER_FULL_FALLBACK_REASONS14
                    or fallback["scope"] == "all",
                    "an input_too_large or turns_exhausted fallback has scope all",
                )
                _durable_reference._refuse(
                    fallback["scope"] != "all"
                    or self._grouper_chain < grouper_chain_limit({"grouping": policy}) - 1,
                    "the second full fallback is written as role_failed",
                )
        return {
            "fallback": None if fallback is None else dict(fallback),
            "calls": None if calls is None else dict(calls),
        }

    def _focus_portion_selected(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(state.baseline_complete, "the round's baseline is not complete")
        # The no-failing and grouping-reuse paths write no `grouping_started`.
        self._check_refresh_completed(state)
        _durable_reference._refuse(
            state.focus_portion is None, "the round already has a focus portion"
        )
        if p["reason"] in ("selected", "unmeasurable"):
            # Both states are read off a completed grouping: the one that chose
            # a portion, and the one whose cohorts the exclusion filter emptied.
            # b16 §11.1: a round with a finished grouping of its own is bound to
            # it, so its portion names that grouping and its policy digest even
            # after a Grouper rebase; a reuse round's portion names the prior one.
            own = state.grouping
            grouping: dict[str, Any] | None
            if own is not None and own["status"] == "finished":
                _durable_reference._refuse(
                    own["baseline_binding"] == p["baseline_binding"]
                    and own["policy_digest"] == p["policy_digest"],
                    "portion names another grouping",
                )
                grouping = own
            else:
                grouping = self._prior_finished_grouping(p["baseline_binding"], p["policy_digest"])
            _durable_reference._refuse(
                grouping is not None, "selected portion has no completed grouping"
            )
            assert grouping is not None
            _durable_reference._refuse(
                grouping["registry_digest"] == p["registry_digest"],
                "portion names another registry",
            )
            _durable_reference._refuse(
                grouping["result_digest"] == p["grouping_digest"], "portion names another grouping"
            )
        entry = dict(p)
        entry.pop("evidence", None)
        entry["seq"] = seq

        def apply() -> None:
            self._put_round(replace(self._rounds[number], focus_portion=entry))

        return apply

    def _attempt_input_frozen(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(attempt.round == p["round"], "frozen input names another round")
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        _durable_reference._refuse(attempt.editor is None, "frozen input precedes editor")
        _durable_reference._refuse(attempt.frozen_input is None, "attempt input is frozen once")
        canonical_input = canonical_json(
            {
                "common_input_digest": p["common_input_digest"],
                "feedback": p["feedback"],
                "feedback_mode": p["feedback_mode"],
                "source_refs": p["source_refs"],
            }
        )
        _durable_reference._refuse(
            hashlib.sha256(canonical_input).hexdigest() == p["input_digest"],
            "input digest does not bind frozen input",
        )
        state = self._round(p["round"], "the round does not exist")
        _durable_reference._refuse(
            p["feedback_mode"] == (self.policy.attempt_feedback or "none"),
            "feedback mode differs from policy",
        )
        for other in state.attempts.values():
            frozen = other.frozen_input
            if frozen is not None:
                _durable_reference._refuse(
                    frozen["common_input_digest"] == p["common_input_digest"],
                    "round attempts use one common input",
                )
        if self._holds_attempts() and decides_at_once(self.policy):
            # #91 spec §3.1, §5 and family-14 R36: immediate decisions precede
            # the next input, independently of whether feedback is present.
            _durable_reference._refuse(
                not any(
                    other.index < attempt.index and other.outcome is None
                    for other in state.attempts.values()
                ),
                "an earlier attempt is not decided",
            )
        if p["feedback_mode"] == "sequential":
            seen: set[str] = set()
            previous_index = 0
            expected_sources: list[tuple[str | None, str | None]] = []
            for row in p["feedback"]["attempts"]:
                _durable_reference._refuse(
                    row["attempt_id"] not in seen, "feedback repeats an attempt"
                )
                seen.add(row["attempt_id"])
                earlier = self._attempt(row["attempt_id"])
                _durable_reference._refuse(
                    earlier.round == attempt.round, "feedback names another round"
                )
                _durable_reference._refuse(
                    earlier.index < attempt.index, "feedback must name a lower attempt"
                )
                _durable_reference._refuse(
                    earlier.index > previous_index, "feedback attempts are out of order"
                )
                previous_index = earlier.index
                _durable_reference._refuse(
                    earlier.outcome is not None, "feedback attempt is not closed"
                )
                reason, comparison = effective_attempt_feedback(
                    state, earlier, family_version=self._family_version
                )
                _durable_reference._refuse(
                    (row["index"], row["outcome"], row["reason"])
                    == (earlier.index, earlier.outcome, reason),
                    "feedback differs from the closed attempt",
                )
                _durable_reference._refuse(
                    row["assessed"]
                    == bool(
                        earlier.looks
                        or (
                            self._independent
                            and earlier.family15 is not None
                            and earlier.family15.comparison is not None
                        )
                    ),
                    "feedback assessed differs from the closed attempt",
                )
                unmeasured = [case for case in row["cases"] if "status" in case]
                if unmeasured:
                    _durable_reference._refuse(
                        self._independent
                        and getattr(self.policy, "focus_first", False)
                        and earlier.family15 is not None
                        and earlier.family15.focus_decision is not None
                        and earlier.family15.focus_decision.payload["outcome"] != "passed",
                        "unmeasured feedback needs its own focus rejection",
                    )
                    assert earlier.selection is not None and state.gate_scope is not None
                    _, causal = confidence_scopes(
                        earlier.selection, self.identity["corpus_cases"], state.gate_scope["cases"]
                    )
                    causal_ids = {self.identity["corpus_cases"][i]["case_id"] for i in causal}
                    _durable_reference._refuse(
                        all(
                            case["case_id"] in causal_ids
                            and case["expected_repeats"] == self.policy.repeats
                            for case in unmeasured
                        ),
                        "unmeasured feedback cases differ from causal scope",
                    )
                for source in row.get("source_refs", []):
                    _durable_reference._refuse(
                        source in {earlier.attempt_id, str(earlier.closed_seq)},
                        "feedback source does not name the closed attempt",
                    )
                expected_sources.append(
                    (earlier.closed_record_digest, earlier.closed_record_locator)
                )
                if comparison is not None:
                    expected_sources.append(
                        (comparison["record_digest"], comparison["record_locator"])
                    )
            _durable_reference._refuse(
                len(p["source_refs"]) == len(expected_sources),
                "frozen feedback source reference count",
            )
            for source, (digest, locator) in zip(p["source_refs"], expected_sources, strict=True):
                ref = _durable_reference.DurableReference.from_payload(source)
                _durable_reference._refuse(
                    ref.kind == "journal_record"
                    and ref.run_id == self.run_id
                    and ref.locator.startswith("campaign.jsonl#bytes=")
                    and ref.content_digest == digest
                    and (ref.locator == locator),
                    "frozen feedback source does not name the closed attempt",
                )
        else:
            _durable_reference._refuse(
                not p["source_refs"], "none feedback has no source references"
            )
        entry = dict(p)

        def apply() -> None:
            self._put_attempt(replace(self._attempt(p["attempt_id"]), frozen_input=entry))

        return apply

    def _probe_checked(
        self, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        focus = _need(state.focus_portion, "the round has no focus portion")
        _durable_reference._refuse(focus["reason"] != "none", "a focus-less round checks no probe")
        _durable_reference._refuse(
            not any(entry["status"] == "started" for entry in state.reflections),
            "a reflection is already open",
        )
        probe = self.identity["reflector"]["probe"]
        _durable_reference._refuse(probe is not None, "probe check needs a declared probe")
        _durable_reference._refuse(state.closed is None, "a closed round checks no probe")
        _durable_reference._refuse(
            state.diagnosis(True) is None, "a settled reflection checks no probe"
        )
        expected_id = f"r{number:02d}-x{len(state.reflections) + 1:02d}"
        _durable_reference._refuse(
            p["reflection_id"] == expected_id, "probe check is not for the next reflection"
        )
        prior = state.probe_check
        if prior is not None and prior["reflection_id"] == expected_id:
            _durable_reference._refuse(
                prior["status"] == "ready" or probe_continues(self.identity, self._family_version),
                "unavailable probe check is final",
            )
        entry = {
            "reflection_id": p["reflection_id"],
            "status": p["status"],
            "seq": seq,
            "record_digest": hashlib.sha256(record.encode()).hexdigest(),
        }
        locator = (
            f"campaign.jsonl#bytes={self._valid_bytes}:{self._valid_bytes + len(record.encode())}"
        )

        def apply() -> None:
            self._put_round(replace(self._rounds[number], probe_check=entry))
            self._probe_check_locators[number] = locator

        return apply

    def _reflection_started(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        focus_portion = _need(state.focus_portion, "the round has no focus portion")
        _durable_reference._refuse(
            focus_portion["reason"] != "none", "a focus-less round starts no reflection"
        )
        _durable_reference._refuse(
            not any(entry["status"] == "started" for entry in state.reflections),
            "a reflection is already open",
        )
        expected_id = f"r{number:02d}-x{len(state.reflections) + 1:02d}"
        _durable_reference._refuse(
            p["reflection_id"] == expected_id, "reflection_id is not the round's next ordinal"
        )
        _durable_reference._refuse(
            not any(
                self._suspends(entry) and not entry.get("suspended", False)
                for entry in state.reflections
            ),
            "a failing reflection is resolved by its suspension first",
        )
        if state.probe_check is not None:
            _durable_reference._refuse(
                state.probe_check["status"] != "unavailable"
                or probe_continues(self.identity, self._family_version),
                "unavailable probe check starts no reflection",
            )
        probeless = False
        if self.identity["reflector"]["probe"] is not None:
            check = state.probe_check
            ready = (
                check is not None
                and check["reflection_id"] == expected_id
                and (
                    check["status"] == "ready"
                    or probe_continues(self.identity, self._family_version)
                )
            )
            # ADR-0039 D5: the family-14 minimum can be refused before any
            # probe setup, with its terminal restricted below.
            probeless = not ready
            _durable_reference._refuse(
                ready or probeless, "declared probe reflection needs a ready check"
            )
        _durable_reference._refuse(
            p["operation_id"] == op, "the payload operation id must match the envelope"
        )
        entry = dict(
            reflection_id=p["reflection_id"],
            status="started",
            groups=[],
            error_class=None,
            probes=(),
            seq=seq,
            operation_id=op,
        )

        def apply() -> None:
            if probeless:
                self._probeless_reflections.add((number, p["reflection_id"]))
            current = self._rounds[number]
            self._put_round(replace(current, reflections=(*current.reflections, entry)))

        return apply

    def _probe_completed(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        index, open_reflection = self._open_reflection(state, p["reflection_id"])
        _durable_reference._refuse(
            (number, p["reflection_id"]) not in self._probeless_reflections,
            "a reflection without a probe check makes no probe call",
        )
        probe = self.identity["reflector"]["probe"]
        _durable_reference._refuse(probe is not None, "probe call needs a declared probe")
        assert probe is not None
        _durable_reference._refuse(p["method"] in probe["methods"], "probe method is not declared")
        continuing = probe_continues(self.identity, self._family_version)
        _durable_reference._refuse(
            p["error"] != "unreachable" or continuing, "unreachable requires continue"
        )
        charged = sum(entry["error"] != "unreachable" for entry in open_reflection["probes"])
        _durable_reference._refuse(
            charged < probe["budget"] if continuing else p["seq"] <= probe["budget"],
            "probe budget is exhausted",
        )
        _durable_reference._refuse(
            p["status"] is None or 100 <= p["status"] <= 599,
            "probe HTTP status is outside 100..599",
        )
        _durable_reference._refuse(
            p["error"] not in {"rejected", "budget_exhausted"},
            "undispatched probe result cannot be journaled",
        )
        streak = 0
        dead = False
        for prior in open_reflection["probes"]:
            if prior["status"] is not None:
                streak = 0
            elif prior["error"] == "transport":
                streak += 1
            elif prior["error"] == "instance_died":
                dead = True
        _durable_reference._refuse(not dead, "dead probe session records no later call")
        if p["error"] == "transport" and not continuing:
            _durable_reference._refuse(streak < 2, "third transport failure records instance_died")
        if p["error"] == "instance_died":
            _durable_reference._refuse(
                not continuing and streak == 2, "instance_died is the third transport failure"
            )
        _durable_reference._refuse(
            p["seq"] == len(open_reflection["probes"]) + 1, "probe seq is not contiguous"
        )
        entry = dict(p)

        def apply() -> None:
            current = self._rounds[number]
            reflections = list(current.reflections)
            target = reflections[index]
            reflections[index] = {**target, "probes": (*target["probes"], entry)}
            self._put_round(replace(current, reflections=tuple(reflections)))

        return apply

    def _reflection_terminal(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        start = self.pending[str(op)]
        _durable_reference._refuse(
            start.event == REFLECTION_START_EVENT, "the operation is not a reflection call"
        )
        _durable_reference._refuse(
            start.payload["reflection_id"] == p["reflection_id"],
            "the reflection terminal names another reflection",
        )
        _durable_reference._refuse(
            p["operation_id"] == op, "the payload operation id must match the envelope"
        )
        number = p["round"]
        state = self._round(number, "the round does not exist")
        index, open_reflection = self._open_reflection(state, p["reflection_id"])
        key = (number, p["reflection_id"])
        if key in self._probeless_reflections:
            _durable_reference._refuse(
                "groups" not in p and p.get("error_class") in {"input_too_large", "interrupted"},
                "a reflection without a probe check ends input_too_large or interrupted",
            )
        status = p["status"] if "status" in p else "failed"
        error_class = p.get("error_class")
        availability = p.get("probe_availability")
        if (
            probe_continues(self.identity, self._family_version)
            and state.probe_check is not None
            and state.probe_check["reflection_id"] == p["reflection_id"]
        ):
            _durable_reference._refuse(
                availability is not None, "continue reflection needs availability"
            )
        if availability is not None:
            _durable_reference._refuse(
                probe_continues(self.identity, self._family_version),
                "availability requires continue",
            )
            check = _need(state.probe_check, "availability needs a probe check")
            _durable_reference._refuse(
                check["reflection_id"] == p["reflection_id"],
                "availability needs this reflection check",
            )
            _durable_reference._refuse(
                availability["health"] == check["status"], "probe health differs from check"
            )
            _durable_reference._refuse(
                availability["unreachable"]
                == sum(e["error"] == "unreachable" for e in open_reflection["probes"]),
                "unreachable count differs from calls",
            )
            _durable_reference._refuse(
                availability["transport_failures"]
                == sum(e["error"] == "transport" for e in open_reflection["probes"]),
                "transport count differs from calls",
            )
            _durable_reference._refuse(
                availability["mediator"] != "failed"
                or all(e["error"] == "unreachable" for e in open_reflection["probes"]),
                "failed mediator dispatches no calls",
            )
        if "groups" in p:
            _durable_reference._refuse(
                p["probes_used"] == len(open_reflection["probes"]),
                "probes_used is not the reflection's own probe count",
            )
            groups = list(p["groups"])
            current_seqs = {entry["seq"] for entry in open_reflection["probes"]}
            _durable_reference._refuse(
                all(ref in current_seqs for group in groups for ref in group["probe_refs"]),
                "probe_refs do not name current reflection calls",
            )
            # #282 (C6): the text limit is the round's, which the stateless
            # record check cannot know; the other limits it already checked.
            violation = _adaptive_group_violation(groups, state.group_text_chars)
            _durable_reference._refuse(violation is None, violation or "")
        else:
            groups = []

        def apply() -> None:
            self._probeless_reflections.discard(key)
            current = self._rounds[number]
            reflections = list(current.reflections)
            reflections[index] = {
                **reflections[index],
                "status": status,
                "groups": groups,
                "reads": (dict(p["reads"])),
                "error_class": error_class,
                "seq": seq,
                # Family 10 only: the payload grammar admits `reason` nowhere else.
                **({"reason": p["reason"]} if "reason" in p else {}),
                **(
                    {"probe_availability": dict(p["probe_availability"])}
                    if "probe_availability" in p
                    else {}
                ),
            }
            if (
                probe_continues(self.identity, self._family_version)
                and current.probe_check is not None
                and current.probe_check["reflection_id"] == p["reflection_id"]
            ):
                # The terminal owns this check now. A later policy rebase
                # cannot turn a completed continue reflection into probe debt.
                current = replace(current, probe_check=None)
                self._probe_check_locators.pop(number, None)
            self._put_round(replace(current, reflections=tuple(reflections)))
            if error_class == "unresponsive":
                self._reflector_unresponsive = True

        return apply

    def _gate_scope_selected(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        focus_portion = _need(state.focus_portion, "the round has no focus portion")
        _durable_reference._refuse(
            focus_portion["reason"] != "none", "a focus-less round selects no gate scope"
        )
        _durable_reference._refuse(
            not any(entry["status"] == "started" for entry in state.reflections),
            "a reflection is still open",
        )
        _durable_reference._refuse(
            any(entry["status"] != "started" for entry in state.reflections),
            "the round has no settled reflection",
        )
        _durable_reference._refuse(state.gate_scope is None, "the round already has a gate scope")
        diagnosis = _need(state.diagnosis(True), "the round has no settled reflection")
        _durable_reference._refuse(
            editor_input_reason(diagnosis) is None,
            "a gate scope needs a valid Editor input",
        )
        corpus = {entry["case_id"] for entry in self.identity["corpus_cases"]}
        _durable_reference._refuse(
            set(p["cases"]) <= corpus,
            "gate scope contains a case outside the corpus",
        )
        cases = set(p["cases"])
        _durable_reference._refuse(
            set(p["selected_cases"]) <= cases,
            "gate scope selection is not within its cases",
        )
        _durable_reference._refuse(
            set(p["unevaluable_cases"]) <= cases,
            "gate scope unevaluable cases are not within its cases",
        )
        if p["source"] == "claim":
            _durable_reference._refuse(
                set(p["claimed_cases"]) <= set(p["selected_cases"]),
                "gate scope claim is not within its selection",
            )
        entry = dict(p)
        entry.pop("evidence", None)
        entry["seq"] = seq

        def apply() -> None:
            self._put_round(replace(self._rounds[number], gate_scope=entry))

        return apply

    def _attempt_sealed(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        _durable_reference._refuse(
            attempt.editor == "edited", "a seal follows editor_finished(edited)"
        )
        _durable_reference._refuse(
            attempt.candidate_digest is None, "the attempt is already sealed"
        )

        def apply() -> None:
            self._put_attempt(
                replace(
                    self._attempt(p["attempt_id"]),
                    candidate_digest=p["candidate_digest"],
                    dependency_digest=p["dependency_digest"],
                )
            )

        return apply

    def _selectable_attempt(self, p: Mapping[str, Any]) -> AttemptState:
        """G2 spec §4.4-§4.5: the sealed, unbound, unselected Attempt that a
        `candidate_inspected` or an `affected_cases_selected` names."""

        attempt = self._attempt(p["attempt"])
        _durable_reference._refuse(attempt.round == p["round"], "the record names another round")
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        _durable_reference._refuse(
            attempt.candidate_digest is not None, "the attempt is not sealed"
        )
        _durable_reference._refuse(
            attempt.measurement_id is None, "the attempt is already measured"
        )
        _durable_reference._refuse(attempt.selection is None, "the attempt already has a selection")
        return attempt

    def _candidate_inspected(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        attempt = self._selectable_attempt(p)
        _durable_reference._refuse(
            p["inspection"] == attempt.inspections + 1,
            "inspection is not the next inspection of the attempt",
        )

        def apply() -> None:
            self._put_attempt(
                replace(
                    self._attempt(p["attempt"]),
                    inspections=p["inspection"],
                    inspected_components=p["components"],
                    inspected_withheld=p.get("components_withheld", 0),
                    inspected_epoch=len(self._epochs) - 1,
                )
            )

        return apply

    def _affected_cases_selected(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        self._selectable_attempt(p)
        # The identity in force: `_identity_rebased` moves `self.identity`.
        kind = self.identity["policy"]["gate"]["provenance"]["kind"]
        _durable_reference._refuse(
            p["source"] == kind, "selection source is not the provenance kind in force"
        )
        size = len(self.identity["corpus_cases"])
        _durable_reference._refuse(
            all(index < size for index in p["cases"]),
            "selection cases are outside the corpus",
        )
        _durable_reference._refuse(
            p["fallback_reason"] is None or p["cases"] == list(range(size)),
            "a fallback lists every case",
        )
        entry = dict(p)

        def apply() -> None:
            self._put_attempt(replace(self._attempt(p["attempt"]), selection=entry))

        return apply

    def _measurement_bound(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        measurement, number = p["measurement_id"], p["round"]
        _durable_reference._refuse(
            measurement not in self._measurements, "the measurement id is already bound"
        )
        side = p["attempt_id"]
        # G2 spec §3.5: a family-12 Candidate's case set as a selector, `None`
        # for the whole corpus (every baseline, and a full-corpus Candidate).
        case_subset: tuple[int, ...] | None = None
        focus_repeats: list[list[int]] | None = None
        if side == CHAMPION_SIDE:
            state = self._round(number, "the round does not exist")
            _durable_reference._refuse(
                state.baseline_measurement_id == measurement,
                "the baseline is not bound to that measurement",
            )
            if self._rebaseline_from_round is not None and number == self._rebaseline_from_round:
                _durable_reference._refuse(
                    measurement == self._fresh_baseline_id(number),
                    "the fresh baseline keeps its epoch id",
                )
            elif self._refresh_entry(number) is not None:
                # F0 R43: a refresh is a new champion-side baseline measurement.
                _durable_reference._refuse(
                    measurement == refresh_measurement_id_for(number),
                    "the refresh measurement keeps its derived id",
                )
            else:
                _durable_reference._refuse(
                    measurement == measurement_id_for(None),
                    "the seed champion measurement keeps its derived id",
                )
            _durable_reference._refuse(
                p["candidate_digest"] == self._champion[1],
                "the champion measurement measures another candidate",
            )
        else:
            attempt = self._attempt(side)
            _durable_reference._refuse(
                attempt.round == number, "the measurement names another round"
            )
            _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
            _durable_reference._refuse(
                attempt.measurement_id is None, "the attempt is already measured"
            )
            _durable_reference._refuse(
                attempt.candidate_digest is not None, "the attempt is not sealed"
            )
            _durable_reference._refuse(
                p["candidate_digest"] == attempt.candidate_digest,
                "the measurement measures another candidate",
            )
            _durable_reference._refuse(
                measurement == measurement_id_for(side),
                "the attempt measurement keeps its derived id",
            )
            selection = _need(attempt.selection, "a family-12 bind needs the attempt's selection")
            state = self._round(number, "the round does not exist")
            scope = _need(state.gate_scope, "a family-12 bind needs the round's gate scope")
            # §4.6: the Candidate measures layers 1 and 2, or the whole
            # corpus with the global layer. `self.policy` is the policy in
            # force: only a `rebaseline` moves it, together with the
            # identity, and it closes the open round first.
            layers = affected_layers(
                selection,
                self.identity["corpus_cases"],
                scope["cases"],
                global_layer=bool(self.policy.global_layer),
            )
            _durable_reference._refuse(
                bool(layers.layer_one), "a family-12 bind needs a non-empty layer 1"
            )
            case_subset = subset_of(self.identity["corpus_cases"], layers.candidate_cases)
            # b18: `/16` focus cases at R_focus, as the fresh Parent's.
            focus_repeats = focus_case_repeats(
                self.policy,
                confidence_scopes(selection, self.identity["corpus_cases"], scope["cases"])[0],
            )
        # Families 5-11 keep their entries' keys.
        selector: dict[str, Any] = dict(
            role="baseline" if side == CHAMPION_SIDE else "candidate", case_subset=case_subset
        )
        if focus_repeats is not None:
            selector["case_repeats"] = focus_repeats

        def apply() -> None:
            self._register_measurement(
                measurement,
                side=side,
                number=number,
                candidate_digest=p["candidate_digest"],
                identity_digest=p["identity_digest"],
                **selector,
            )
            if (
                self._declared_import
                and number == self.first_round
                and measurement == measurement_id_for(None)
            ):
                # ADR-0019 (amended 2026-10-03): the declared baseline import
                # was admitted when the creation record declared it.
                self._import_versions[measurement] = self._creation_version
            if side != CHAMPION_SIDE:
                self._put_attempt(replace(self._attempt(side), measurement_id=measurement))

        return apply

    def _parent_measurement_bound(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        """G2 spec §4.6: the fresh Parent of a family-12 Attempt, over its layer 1.

        Written after the Candidate's `measurement_bound` and before the
        Attempt's first stage. It measures the Attempt's Parent tree and
        follows Attempt rules.

        Family-13 design G1, §5: a family-13 fresh Parent measures F ∪ C as
        sorted corpus indices, and under the four conditions
        (`observe_case_repeats`) its entry gains `case_repeats`, two
        observations on each C case. No record carries a selector, so this
        rule is the only one that ever puts `case_repeats` in an entry, and
        the audit's three-way binding flags a journal whose identity carries a
        selector its entry lacks: that is how `case_repeats` is refused on any
        other measurement and in any other family.
        """

        _durable_reference._refuse(
            bool(self.policy.fresh_parent), "the policy measures no fresh Parent"
        )
        measurement = p["measurement_id"]
        _durable_reference._refuse(
            measurement not in self._measurements, "the measurement id is already bound"
        )
        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(
            attempt.round == p["round"], "the measurement names another round"
        )
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        _durable_reference._refuse(
            attempt.measurement_id is not None, "the Candidate is not bound yet"
        )
        _durable_reference._refuse(
            attempt.parent_measurement_id is None, "the fresh Parent is already bound"
        )
        _durable_reference._refuse(
            p["candidate_digest"] == attempt.parent_digest,
            "the fresh Parent measures another tree than the Parent",
        )
        _durable_reference._refuse(
            measurement == parent_measurement_id_for(attempt.attempt_id),
            "the fresh Parent keeps its derived id",
        )
        selection = _need(attempt.selection, "a fresh Parent needs the attempt's selection")
        state = self._round(attempt.round, "the round does not exist")
        scope = _need(state.gate_scope, "the round has no gate scope")
        case_subset: Sequence[int] | None
        selector: dict[str, Any] = {}
        focus, causal = confidence_scopes(selection, self.identity["corpus_cases"], scope["cases"])
        case_subset = sorted(focus + causal)
        # Family15 shares F∪C only; its R never invokes legacy observe allocation.
        if self._confidence:
            repeats = observe_case_repeats(self.policy, causal)
            if repeats is not None:
                selector["case_repeats"] = repeats
        else:
            repeats = focus_case_repeats(self.policy, focus)
            if repeats is not None:
                selector["case_repeats"] = repeats

        def apply() -> None:
            self._register_measurement(
                measurement,
                side=attempt.attempt_id,
                number=attempt.round,
                candidate_digest=p["candidate_digest"],
                identity_digest=p["identity_digest"],
                role="parent",
                case_subset=case_subset,
                **selector,
            )
            self._put_attempt(
                replace(self._attempt(attempt.attempt_id), parent_measurement_id=measurement)
            )

        return apply

    def _comparison_bound(self, p: dict[str, Any], op: str | None, seq: int) -> Callable[[], None]:
        """Design §6.2, G6, G10: the Attempt's comparison, bound after its
        measurements and before their first stage, re-derived in full.

        F, C, R, the looks, the confidence, tolerance and family-14 margin come from the
        selection, the round's exam and the policy in force; the exclusions
        are the baseline's one `pairs_excluded`. That `retained` follows them
        needs the baseline rows: the audit recomputes it (`retained_cases`).
        A non-null `supersedes` re-binds the governing comparison after a
        `continue` moved its measurements (G11, `_check_superseding_comparison`);
        it becomes the governing binding and may set its calibration aside.
        """

        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(
            attempt.round == p["round"], "the comparison names another round"
        )
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        governing = attempt.comparison
        if p["supersedes"] is not None:
            self._check_superseding_comparison(attempt, p)
        else:
            _durable_reference._refuse(
                governing is None, "the attempt's comparison is already bound"
            )
        selection = _need(attempt.selection, "a comparison needs the attempt's selection")
        _durable_reference._refuse(
            attempt.measurement_id is not None
            and p["candidate_measurement_id"] == attempt.measurement_id,
            "the comparison names another Candidate measurement",
        )
        policy = self.policy
        if policy.fresh_parent:
            _durable_reference._refuse(
                attempt.parent_measurement_id is not None, "the fresh Parent is not bound yet"
            )
        _durable_reference._refuse(
            p["parent_measurement_id"] == attempt.parent_measurement_id,
            "the comparison names another Parent measurement",
        )
        _durable_reference._refuse(
            p["parent_source"] == ("fresh_parent" if policy.fresh_parent else "baseline"),
            "the Parent source is not the policy's",
        )
        state = self._rounds[attempt.round]
        _durable_reference._refuse(
            p["baseline_measurement_id"] == state.baseline_measurement_id,
            "the comparison names another baseline",
        )
        _durable_reference._refuse(
            _no_stage_opened(attempt, self._measurements),
            "a comparison precedes every stage of its measurements",
        )
        _durable_reference._refuse(p["mode"] == policy.gate_mode, "the mode is not the policy's")
        if p["supersedes"] is None:
            _durable_reference._refuse(
                p["margin"] == policy.focus_margin, "the margin is not the policy's"
            )
        _durable_reference._refuse(
            p["epoch"] == len(self._epochs) - 1, "the comparison names another condition epoch"
        )
        scope = _need(state.gate_scope, "a comparison needs the round's gate scope")
        focus, causal = confidence_scopes(selection, self.identity["corpus_cases"], scope["cases"])
        _durable_reference._refuse(
            (p["focus"], p["causal"]) == (focus, causal), "the scopes are not the selection's"
        )
        _durable_reference._refuse(
            p["repeats"] == policy.repeats and p["looks"] == list(policy.looks),
            "repeats and looks are not the policy's",
        )
        _durable_reference._refuse(
            p["confidence"] == comparison_confidence(policy), "the confidence is not the policy's"
        )
        _durable_reference._refuse(
            p["tolerance"] == comparison_tolerance(policy), "the tolerance is not the policy's"
        )
        baseline = str(state.baseline_measurement_id)
        recorded = self._excluded.get(baseline)
        if recorded is None:
            _durable_reference._refuse(p["exclusions"] is None, "the baseline excluded no pairs")
            _durable_reference._refuse(
                p["retained"] == {"focus": focus, "causal": causal},
                "without exclusions every case is retained",
            )
        else:
            # One record per measurement (`_pairs_excluded`).
            ((count, digest),) = recorded.values()
            exclusions = p["exclusions"]
            _durable_reference._refuse(
                exclusions is not None
                and (exclusions["count"], exclusions["pairs_digest"]) == (count, digest)
                and exclusions["prefix"] == self._exclusion_prefixes.get(baseline),
                "the exclusions are not the baseline's",
            )
        _durable_reference._refuse(
            len(p["evidence"]) == (1 if p["exclusions"] is None else 2)
            and all(reference["kind"] == "journal_record" for reference in p["evidence"]),
            "the comparison cites its selection and the baseline's exclusions",
        )
        entry = {key: value for key, value in p.items() if key != "evidence"}
        entry.update(seq=seq, digest=binding_digest(p))
        evidence = copy.deepcopy(list(p["evidence"]))
        changes: dict[str, Any] = {"comparison": entry}
        if governing is not None:
            changes["superseded_comparisons"] = (
                *attempt.superseded_comparisons,
                int(governing["seq"]),
            )
            calibration = attempt.calibration
            # Plan choice 4: only an incomplete fresh-Parent calibration whose
            # Parent was re-bound is superseded; any other one stands.
            if (
                calibration is not None
                and calibration["source"] == "fresh_parent"
                and calibration["source_measurement_id"] != attempt.parent_measurement_id
                and attempt.calibration_result is None
            ):
                changes.update(calibration=None, superseded_calibration=int(calibration["seq"]))

        def apply() -> None:
            self._put_attempt(replace(self._attempt(p["attempt_id"]), **changes))
            self._comparison_evidence[str(p["attempt_id"])] = evidence

        return apply

    def _check_superseding_comparison(self, attempt: AttemptState, p: Mapping[str, Any]) -> None:
        """G11, plan choice 3: a `continue` re-binds an undecided comparison.

        Admitted only after a `measurement_rebound` moved either side of the
        governing binding, while no close is owed, naming that binding, and
        changing nothing but its measurements and epoch. The equality runs
        before the derivation checks that follow, so a changed value is
        refused by this rule rather than by its derivation's.

        Neither side may be frozen (F7 Task 3): the `rebind` block ends at its
        first other record, so a block that re-bound one side would otherwise
        end here with the other side still owed. A frozen side of an open
        Attempt is exactly an owed re-bind (`_append_target`); before any stage
        no other rule refuses it.
        """

        rebound = "a superseding comparison follows its attempt's re-bind"
        governing = _need(attempt.comparison, rebound)
        _durable_reference._refuse(_comparison_moved(attempt), rebound)
        _durable_reference._refuse(
            not any(
                self._measurements[str(side)].get("frozen", False)
                for side in (attempt.measurement_id, attempt.parent_measurement_id)
                if side is not None
            ),
            "a superseding comparison names no frozen measurement",
        )
        _durable_reference._refuse(
            owed_close(attempt, family_version=self._family_version) is None,
            "an undecided comparison is re-bound",
        )
        _durable_reference._refuse(
            p["supersedes"] == governing["seq"], "supersedes names the governing comparison"
        )
        _durable_reference._refuse(
            all(p[key] == governing[key] for key in p if key not in _SUPERSEDING_CHANGES)
            and p["evidence"] == self._comparison_evidence[attempt.attempt_id],
            "a superseding comparison changes only its measurements and epoch",
        )

    def _register_measurement(
        self,
        measurement: str,
        *,
        side: str,
        number: int,
        candidate_digest: str,
        identity_digest: str,
        **selector: Any,
    ) -> None:
        """Put a newly bound measurement into the registry, with no stage yet.

        `epoch` is the condition epoch it was bound under, the latest one at
        the moment of binding; `frozen` is set on every earlier measurement
        once a later condition epoch opens (`_identity_rebased`; ADR-0020).
        `selector` holds the binding's own keys (`role`, `case_subset`, ...).
        """

        self._measurements[measurement] = dict(
            measurement_id=measurement,
            attempt_id=side,
            round=number,
            candidate_digest=candidate_digest,
            identity_digest=identity_digest,
            epoch=len(self._epochs) - 1,
            stages=(),
            instances=(),
            complete=False,
            frozen=False,
            **selector,
        )

    def _finalists_bound(self, p: dict[str, Any], op: str | None, seq: int) -> Callable[[], None]:
        """Design §6.7 and R38: freeze the finalists at their governing barrier.

        Every value is re-derived: the finalists are the round's `accepted`
        Attempts in index order, each with its own global level; the members
        are the Champion and then each finalist; the policy, seed mode and
        condition epoch are the ones in force; the evidence is each
        finalist's `attempt_closed`.
        First-accepted binds only the latest closed unbound finalist, after
        the preceding final's members and comparison have settled.
        """

        number = int(p["round"])
        state = self._round(number, "the round is not opened")
        _durable_reference._refuse(
            state.closed is None and state.selection is None, "the round is decided"
        )
        _durable_reference._refuse(
            state.baseline_complete,
            self._member_rule("a cohort follows the round's complete baseline"),
        )
        policy = self.policy
        attempts = sorted(state.attempts.values(), key=lambda a: a.index)
        first_accepted = policy.round_stop == "first_accepted"
        if not first_accepted:
            _durable_reference._refuse(
                state.cohort is None, self._member_rule("the round's cohort is already bound")
            )
        _durable_reference._refuse(
            (first_accepted or len(attempts) == policy.attempts_per_round)
            and all(a.outcome is not None for a in attempts),
            "every declared attempt of the round is closed",
        )
        vocabulary = campaign_vocabulary_for(14)
        accepted = [a for a in attempts if a.outcome == vocabulary.gate_pass_outcome]
        if first_accepted:
            _durable_reference._refuse(
                not any(c["accepted"] for c in state.global_comparisons.values()),
                "an accepted finalist stops final bindings",
            )
            _durable_reference._refuse(
                all(
                    str(f["attempt_id"]) in state.global_comparisons
                    for final in state.finals
                    for f in final["finalists"]
                )
                and all(
                    mid in state.cohort_completed
                    for final in state.finals
                    for mid in current_members(state, final=final)
                ),
                "earlier final bindings settle before another binding",
            )
            bound = {str(f["attempt_id"]) for final in state.finals for f in final["finalists"]}
            _durable_reference._refuse(
                bool(attempts)
                and attempts[-1].outcome == "finalist"
                and attempts[-1].attempt_id not in bound,
                "only the latest closed unbound finalist may bind",
            )
            accepted = [attempts[-1]]
        _durable_reference._refuse(
            bool(accepted), self._member_rule("a round without finalists binds no cohort")
        )
        _durable_reference._refuse(
            [(f["attempt_id"], f["candidate_digest"]) for f in p["finalists"]]
            == [(a.attempt_id, a.candidate_digest) for a in accepted],
            "the finalists are the round's accepted attempts in index order",
        )
        for finalist, attempt in zip(p["finalists"], accepted, strict=True):
            _durable_reference._refuse(
                finalist["level"] == finalist_level(policy, attempt),
                "a finalist's level is its own global confidence",
            )
        assert isinstance(policy, CampaignPolicyV11)
        fresh = champion_member(p, stability=str(policy.stability)) is not None
        ordinal = len(state.finals) + 1
        ids = ([final_measurement_id_for(number, ordinal, None)] if fresh else []) + [
            final_measurement_id_for(number, ordinal, attempt.index) for attempt in accepted
        ]
        _durable_reference._refuse(
            p["members"] == ids,
            self._member_rule("the cohort's members are the Champion and then each finalist"),
        )
        version, digest = self._champion
        _durable_reference._refuse(
            p["champion"] == {"version": version, "digest": digest},
            self._member_rule("the cohort binds the incumbent Champion"),
        )
        _durable_reference._refuse(
            p["order"] == COHORT_ORDER,
            self._member_rule("the cohort is interleaved by case, repeat and member"),
        )
        _durable_reference._refuse(
            (p["global"], p["mode"], p["repeats"], p["looks"], p["seed_mode"])
            == (
                policy.global_mode,
                policy.gate_mode,
                policy.repeats,
                list(policy.looks),
                self.identity["target"]["seed_mode"],
            ),
            self._member_rule("the cohort binds the policy in force"),
        )
        _durable_reference._refuse(
            p["epoch"] == len(self._epochs) - 1,
            self._member_rule("the cohort binds the epoch in force"),
        )
        references = [
            _durable_reference.DurableReference.from_payload(ref) for ref in p["evidence"]
        ]
        _durable_reference._refuse(
            len(references) == len(accepted)
            and all(
                ref.kind == "journal_record"
                and ref.run_id == self.run_id
                and ref.locator.startswith("campaign.jsonl#bytes=")
                and ref.content_digest == attempt.closed_record_digest
                for ref, attempt in zip(references, accepted, strict=True)
            ),
            self._member_rule("the cohort cites each finalist's attempt_closed"),
        )
        entry = {key: value for key, value in p.items() if key != "evidence"}
        # Design §6.1: the binding digest every cohort contrast's seed reads,
        # stored as `comparison_bound`'s is (`AttemptState.comparison["digest"]`).
        entry.update(seq=seq, digest=binding_digest(p))

        current = tuple(str(member) for member in p["members"])

        def apply() -> None:
            state = self._rounds[number]
            if self._family_version == 14:
                assert isinstance(policy, CampaignPolicyV11)
                entry["stability"] = policy.stability
                self._put_round(
                    replace(
                        state,
                        finals=(*state.finals, entry),
                        finals_current=(*state.finals_current, current),
                        cohort_current=current,
                    )
                )
            else:
                self._put_round(replace(state, cohort=entry, cohort_current=current))

        return apply

    def _cohort_measurement_bound(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """Design §6.8: one member, declared before its journal exists, in `members` order.

        The registry entry is a campaign-level measurement with role `cohort`:
        the Champion member's `attempt_id` is `CHAMPION_SIDE` (the record's
        null) and a finalist's is its Attempt id, so `side_of_entry` reads them
        as the champion and a candidate. It carries no `case_subset`: every
        member measures the whole corpus.
        """

        number = int(p["round"])
        state = self._round(number, "the round is not opened")
        cohort = _need(state.cohort, self._member_rule("the round has no bound cohort"))
        _durable_reference._refuse(
            state.selection is None and state.closed is None, "the round is decided"
        )
        members = [str(member) for member in cohort["members"]]
        measurement = str(p["measurement_id"])
        _durable_reference._refuse(
            measurement in members, self._member_rule("the measurement is not a cohort member")
        )
        _durable_reference._refuse(
            measurement not in self._measurements, "the member is already bound"
        )
        _durable_reference._refuse(
            (p["attempt_id"] is None) == (p["role"] == "champion"),
            "only a finalist member names its attempt",
        )
        position = members.index(measurement)
        _durable_reference._refuse(
            all(member in self._measurements for member in members[:position]),
            self._member_rule("cohort members bind in order"),
        )
        fresh = (
            champion_member(cohort, stability=str(self.policy.stability)) is not None
            if (isinstance(self.policy, CampaignPolicyV11))
            else True
        )
        assert isinstance(self.policy, CampaignPolicyV11)
        _durable_reference._refuse(
            (p["stability"], p["round_stop"]) == (self.policy.stability, self.policy.round_stop),
            "the final member binds the policy in force",
        )
        if self.policy.stability == "stable":
            reference = _need(
                state.baseline_reference, "the round's complete baseline has no reference"
            )
            _durable_reference._refuse(
                state.baseline_complete
                and p["stored_champion"]
                == {
                    "measurement_id": state.baseline_measurement_id,
                    "prefix_digest": state.baseline_prefix_digest,
                }
                and p["evidence"][0] == reference,
                "the stored Champion cites the round's complete baseline exactly",
            )
        role, attempt_id = member_identity(
            cohort, position, stability="unstable" if fresh else "stable"
        )
        champion = role == "champion"
        if champion:
            _durable_reference._refuse(p["role"] == "champion", "the first member is the Champion")
            expected = cohort["champion"]["digest"]
        else:
            finalist = next(
                finalist for finalist in cohort["finalists"] if finalist["attempt_id"] == attempt_id
            )
            _durable_reference._refuse(
                p["role"] == role and p["attempt_id"] == attempt_id,
                "a later member is its finalist",
            )
            expected = finalist["candidate_digest"]
        _durable_reference._refuse(
            p["candidate_digest"] == expected, "the member measures its bound tree"
        )

        selector: dict[str, Any] = {}
        selector.update(
            stability=p["stability"],
            round_stop=p["round_stop"],
            champion_side=p["champion_side"],
        )
        if p["stability"] == "stable":
            selector.update(
                stored_champion=_immutable_binding(p["stored_champion"]),
                stored_reference=_immutable_binding(p["evidence"][0]),
            )

        def apply() -> None:
            self._register_measurement(
                measurement,
                side=CHAMPION_SIDE if champion else str(p["attempt_id"]),
                number=number,
                candidate_digest=str(p["candidate_digest"]),
                identity_digest=str(p["identity_digest"]),
                role=campaign_vocabulary_for(14).registry_role,
                cohort_role="champion" if champion else "finalist",
                **selector,
            )

        return apply

    def _cohort_member(self, measurement_id: str) -> Mapping[str, Any] | None:
        """The registry entry of a family-13 cohort member, else `None`."""

        entry = self._measurements.get(measurement_id)
        return (
            entry
            if entry is not None
            and entry.get("role")
            == campaign_vocabulary_for(14 if self._family_version == 14 else 13).registry_role
            else None
        )

    def _check_current(self, state: RoundState, names: Sequence[object]) -> None:
        """F7 plan choice 7: a cohort record names each member by its current id.

        A name whose base id is a bound member but which is not current -- the
        frozen id a `continue` re-bound away from -- is refused. Any other name
        is left to the record's own rule.
        """

        cohort = _need(state.cohort, self._member_rule("the round has no bound cohort"))
        current = _need(
            state.cohort_current, self._member_rule("a bound cohort has current members")
        )
        members = {str(name) for name in cohort["members"]}
        _durable_reference._refuse(
            all(
                str(name) in current or base_measurement_id(str(name)) not in members
                for name in names
            ),
            "the record names a member that is not current",
        )

    def _running_members(self, state: RoundState) -> list[str]:
        """The current members that still run cohort stages (`running_members`)."""

        return list(running_members(state))

    def _check_cohort_stage(self, member: Mapping[str, Any], stage: int, look: int) -> None:
        """Design §7, plan choice 7: cohort stage k is look k on every member, in order.

        Over the current members (F7 plan choice 8), and among them only the
        ones still running: no stage opens on a completed member.
        """

        state = self._rounds[int(member["round"])]
        _need(state.cohort, self._member_rule("the round has no bound cohort"))
        _durable_reference._refuse(
            state.selection is None and state.closed is None, "the round is decided"
        )
        current = _need(
            state.cohort_current, self._member_rule("a bound cohort has current members")
        )
        _durable_reference._refuse(
            all(name in self._measurements for name in current),
            self._member_rule("a cohort stage opens once every member is bound"),
        )
        _durable_reference._refuse(
            look == self.policy.looks[stage - 1], self._member_rule("a cohort stage opens its look")
        )
        name = str(member["measurement_id"])
        _durable_reference._refuse(name in current, "the record names a member that is not current")
        _durable_reference._refuse(
            name not in state.cohort_completed, "a completed member opens no stage"
        )
        running = self._running_members(state)
        position = running.index(name)
        opened = [len(self._measurements[other]["stages"]) for other in running]
        _durable_reference._refuse(
            all(count >= stage for count in opened[:position]),
            self._member_rule("a cohort stage opens on every earlier member first"),
        )
        _durable_reference._refuse(
            all(count >= stage - 1 for count in opened[position:]),
            self._member_rule("a cohort stage follows the previous stage on every member"),
        )

    def _cohort_measurement_completed(
        self, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        """Design §6.9: once per member, in order, after its last cohort stage; D7.

        The values themselves (`point`, `missing`, `critical`, and that the
        evidence is exactly the member's attestations) are the audit's to
        recompute from `prefix`; the lifecycle holds the record to its place
        and to D7's pairing: only a finalist may be incomplete, an incomplete
        member has no point, a complete one has one. Its `prefix` ends no
        earlier than any earlier citation of the member's journal (the #109
        floor, `_cited_ends`).
        """

        state = _need(self._rounds.get(int(p["round"])), "the round is not opened")
        cohort = _need(state.cohort, self._member_rule("the round has no bound cohort"))
        _durable_reference._refuse(
            state.selection is None and state.closed is None, "the round is decided"
        )
        members = list(
            _need(state.cohort_current, self._member_rule("a bound cohort has current members"))
        )
        measurement = str(p["measurement_id"])
        member = _need(
            self._cohort_member(measurement),
            self._member_rule("the measurement is not a cohort member"),
        )
        _durable_reference._refuse(
            base_measurement_id(measurement) in {str(name) for name in cohort["members"]},
            self._member_rule("the measurement is not a cohort member"),
        )
        self._check_current(state, [measurement])
        _durable_reference._refuse(
            measurement not in state.cohort_completed, "the member is already completed"
        )
        position = members.index(measurement)
        _durable_reference._refuse(
            all(name in state.cohort_completed for name in members[:position]),
            self._member_rule("cohort members complete in order"),
        )
        _durable_reference._refuse(
            len(member["stages"]) == len(self.policy.looks),
            self._member_rule("a member completes after its last cohort stage"),
        )
        _durable_reference._refuse(
            journal_prefix_measurement(p["prefix"]) == measurement,
            "the completion cites its member's journal",
        )
        # The #109 floor `stage_redispatched` is held to: the member journal is
        # never cited shorter than an earlier record already cited it, so the
        # completion reads at least what the cohort's records read.
        cited = _journal_prefix_end(p["prefix"], measurement)
        if cited is not None:
            _durable_reference._refuse(
                cited >= self._cited_ends.get(measurement, 0),
                "the completion's prefix ends before an earlier citation of its member",
            )
        if member["cohort_role"] == "champion":
            _durable_reference._refuse(
                p["complete"] is True,
                self._member_rule("the Champion's cohort measurement is complete"),
            )
        if p["complete"]:
            _durable_reference._refuse(
                p["point"] is not None and p["missing"] == 0,
                "a complete member has a point and no missing pair",
            )
        else:
            _durable_reference._refuse(
                p["point"] is None and p["missing"] > 0,
                "an incomplete member has no point and names its missing pairs",
            )
        _durable_reference._refuse(
            all(ref["kind"] == "journal_record" for ref in p["evidence"]),
            "the completion cites its member's attestations",
        )
        entry = {key: value for key, value in p.items() if key != "evidence"}
        entry["seq"] = seq
        entry["record_digest"] = hashlib.sha256(record.encode()).hexdigest()
        entry["record_locator"] = (
            f"campaign.jsonl#bytes={self._valid_bytes}:{self._valid_bytes + len(record.encode())}"
        )

        def apply() -> None:
            # `cohort_completed` is shared with earlier snapshots: never mutated.
            current = self._rounds[int(p["round"])]
            done = {**current.cohort_completed, measurement: entry}
            self._put_round(replace(current, cohort_completed=done))

        return apply

    def _global_comparison_completed(
        self, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        """Design §6.10: once per finalist, in finalist order, after every member's completion.

        The lifecycle holds the record to its cohort -- the finalist's and the
        Champion's member ids, the finalist's own level, observe's missing
        bound -- and re-derives `eligible` and `reason` from the published
        `point` and `lower` (`decide_global`, G4). An incomplete finalist's
        record is D7's fixed state, with the pairing the policy requests.
        The contrast's values are the audit's to recompute from the fresh
        completion prefixes, or from the finalist completion and its exact
        immutable stored baseline reference under family-14 stable.
        """

        vocabulary = campaign_vocabulary_for(14)
        number = int(p["round"])
        state = self._round(number, "the round is not opened")
        cohort = _need(state.cohort, self._member_rule("the round has no bound cohort"))
        _durable_reference._refuse(
            state.selection is None and state.closed is None, "the round is decided"
        )
        members = list(
            _need(state.cohort_current, self._member_rule("a bound cohort has current members"))
        )
        _durable_reference._refuse(
            all(name in state.cohort_completed for name in members),
            "a comparison follows every member's completion",
        )
        self._check_current(state, [p["finalist_measurement_id"], p["champion_measurement_id"]])
        finalists = [str(entry["attempt_id"]) for entry in cohort["finalists"]]
        attempt_id = str(p["attempt_id"])
        _durable_reference._refuse(
            attempt_id in finalists,
            self._member_rule("the attempt is not a finalist of the cohort"),
        )
        _durable_reference._refuse(
            attempt_id not in state.global_comparisons, "the finalist is already compared"
        )
        position = finalists.index(attempt_id)
        _durable_reference._refuse(
            all(name in state.global_comparisons for name in finalists[:position]),
            "finalists are compared in order",
        )
        stored = (isinstance(self.policy, CampaignPolicyV11)) and (
            self.policy.stability == "stable"
        )
        member = finalist_member(
            cohort, position, members=members, stability="stable" if stored else "unstable"
        )
        champion = (
            state.baseline_measurement_id
            if stored
            else champion_member(cohort, members=members, stability="unstable")
        )
        _durable_reference._refuse(
            (p["finalist_measurement_id"], p["champion_measurement_id"])
            == (
                member,
                champion,
            ),
            "the comparison names its finalist's and the Champion's members",
        )
        completion = state.cohort_completed[member]
        if stored:
            binding = self._measurements[member]
            _durable_reference._refuse(
                binding.get("stability") == "stable"
                and binding.get("champion_side") == "stored"
                and binding.get("stored_champion")
                == {
                    "measurement_id": champion,
                    "prefix_digest": state.baseline_prefix_digest,
                }
                and binding.get("stored_reference") == state.baseline_reference,
                "a stored comparison follows its governing final binding",
            )
        if not completion["complete"]:
            requested = requested_pairing(cohort)
            _durable_reference._refuse(
                (
                    p["point"],
                    p["lower"],
                    p["level"],
                    p["unavailable"],
                    p[vocabulary.contrast_key],
                    p["reason"],
                    p["pairing"],
                )
                == (
                    None,
                    None,
                    None,
                    vocabulary.incomplete_reason,
                    False,
                    vocabulary.incomplete_reason,
                    requested,
                ),
                "an incomplete finalist's comparison has the fixed non-numerical state",
            )
            _durable_reference._refuse(
                p["exact"] is None and p["removed_pairs"] == [] and p["removed_cases"] == [],
                "an incomplete finalist has no numerical removal metadata",
            )
        else:
            _durable_reference._refuse(
                stored or p["point"] is not None,
                "a complete finalist's comparison has a point",
            )
            if cohort["mode"] == "observe":
                _durable_reference._refuse(
                    (p["lower"], p["level"], p["unavailable"])
                    == (
                        None,
                        None,
                        "final_incomplete"
                        if stored and p["point"] is None
                        else BOUND_NOT_REQUESTED_IN_OBSERVE,
                    ),
                    "an observe comparison requests no bound",
                )
            else:
                _durable_reference._refuse(
                    p["level"] == cohort["finalists"][position]["level"],
                    "a comparison's level is its finalist's own",
                )
                # Fresh comparisons retain their full bound. M2 lets a
                # stored contrast lose its point or bound case set while
                # keeping the finalist's declared level.
                _durable_reference._refuse(
                    (p["lower"] is not None and p["unavailable"] is None)
                    or (
                        stored and p["unavailable"] in ("final_incomplete", "insufficient_repeats")
                    ),
                    "a statistical bound is available or its stored contrast is unavailable"
                    if stored
                    else "a statistical comparison has a lower bound",
                )
            decide = decide_global14
            verdict = decide(
                str(cohort["global"]),
                complete=True,
                critical=bool(completion["critical"]),
                point=p["point"],
                lower=p["lower"],
            )
            _durable_reference._refuse(
                (p[vocabulary.contrast_key], p["reason"])
                == (getattr(verdict, vocabulary.contrast_key), verdict.reason),
                "the comparison's eligibility does not follow its values",
            )
        assert isinstance(self.policy, CampaignPolicyV11)
        _validate_final_removals(p, self.identity["corpus_cases"], repeats=int(cohort["repeats"]))
        _durable_reference._refuse(
            p["champion_side"] == ("fresh" if self.policy.stability == "unstable" else "stored"),
            "the comparison binds the policy's Champion side",
        )
        if stored:
            _durable_reference._refuse(
                len(p["evidence"]) == 2
                and p["evidence"][0]["kind"] == "journal_record"
                and p["evidence"][0]["run_id"] == self.run_id
                and p["evidence"][0]["locator"].startswith("campaign.jsonl#bytes=")
                and p["evidence"][0]["content_digest"] == completion["record_digest"]
                and p["evidence"][1] == state.baseline_reference,
                "a stored comparison cites its finalist completion and exact stored reference",
            )
        else:
            _durable_reference._refuse(
                len(p["evidence"]) == 2
                and all(ref["kind"] == "journal_record" for ref in p["evidence"]),
                "the comparison cites both members' completions",
            )
        entry = {key: value for key, value in p.items() if key != "evidence"}
        entry["seq"] = seq

        entry["record_digest"] = hashlib.sha256(record.encode()).hexdigest()
        entry["record_locator"] = (
            f"campaign.jsonl#bytes={self._valid_bytes}:{self._valid_bytes + len(record.encode())}"
        )

        def apply() -> None:
            # `global_comparisons` is shared with earlier snapshots: never mutated.
            current = self._rounds[number]
            done = {**current.global_comparisons, attempt_id: entry}
            self._put_round(replace(current, global_comparisons=done))

        return apply

    def _noise_calibration_bound(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """Design §6.3, G10: the calibration's source and sizes, bound after
        the comparison and before any stage, and only when it is owed. A
        superseding one (G11) re-binds the calibration a superseding
        comparison set aside (`AttemptState.superseded_calibration`)."""

        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(
            attempt.round == p["round"], "the calibration names another round"
        )
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        if p["supersedes"] is not None or attempt.superseded_calibration is not None:
            # G11, plan choice 4: only the calibration a superseding comparison
            # set aside is re-bound, and only by naming it.
            _durable_reference._refuse(
                attempt.calibration_result is None, "a completed calibration is never re-bound"
            )
            _durable_reference._refuse(
                attempt.superseded_calibration is not None,
                "a superseding calibration follows its Parent's re-bind",
            )
            _durable_reference._refuse(
                p["supersedes"] == attempt.superseded_calibration,
                "supersedes names the governing calibration",
            )
        comparison = _need(attempt.comparison, "a calibration follows its comparison_bound")
        _durable_reference._refuse(
            p["comparison"] == comparison["seq"], "the calibration names another comparison"
        )
        _durable_reference._refuse(
            attempt.calibration is None, "the attempt's calibration is already bound"
        )
        # An Attempt closed at binding owes none (maintainer decision, P1).
        _durable_reference._refuse(
            calibration_owed(comparison), "this comparison owes no noise calibration"
        )
        _durable_reference._refuse(
            _no_stage_opened(attempt, self._measurements),
            "a calibration is bound before every stage",
        )
        _durable_reference._refuse(
            p["source"] == comparison["parent_source"],
            "the calibration source is not the comparison's Parent source",
        )
        source = (
            comparison["parent_measurement_id"]
            if p["source"] == "fresh_parent"
            else comparison["baseline_measurement_id"]
        )
        _durable_reference._refuse(
            p["source_measurement_id"] == source, "the calibration reads another measurement"
        )
        if p["source"] == "baseline":
            _durable_reference._refuse(
                p["prefix"] == self._rounds[attempt.round].baseline_prefix,
                "a baseline calibration reads the settled baseline prefix",
            )
        repeats = comparison["repeats"]
        _durable_reference._refuse(
            p["sizes"] == {"first": repeats, "second": repeats},
            "the virtual sizes are not the comparison's repeats",
        )
        _durable_reference._refuse(
            p["method"] == comparison["method"], "the calibration's method is the comparison's"
        )
        # Plan choice 7: the entry keeps its binding digest, as the comparison's does.
        entry = {**p, "seq": seq, "digest": binding_digest(p)}

        def apply() -> None:
            self._put_attempt(
                replace(
                    self._attempt(p["attempt_id"]), calibration=entry, superseded_calibration=None
                )
            )

        return apply

    def _noise_calibration_completed(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """Design §6.4, G10: once per binding. A baseline source completes
        before the first stage over its bound prefix; a fresh source after the
        Parent's final stage over the Parent's journal; both before the final
        look. Which values the prefix gives is the audit's (F4)."""

        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(
            attempt.round == p["round"], "the calibration names another round"
        )
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        calibration = _need(attempt.calibration, "a calibration result follows its binding")
        _durable_reference._refuse(
            p["binding"] == calibration["seq"], "the result names another calibration binding"
        )
        _durable_reference._refuse(
            attempt.calibration_result is None, "the calibration is already completed"
        )
        comparison = _need(attempt.comparison, "a calibration result follows its comparison")
        final = max(comparison["looks"])
        _durable_reference._refuse(
            final not in attempt.looks, "a calibration completes before the final look"
        )
        if calibration["source"] == "baseline":
            _durable_reference._refuse(
                p["prefix"] == calibration["prefix"],
                "a baseline calibration reads its bound prefix",
            )
            _durable_reference._refuse(
                _no_stage_opened(attempt, self._measurements),
                "a baseline calibration completes before the first stage",
            )
        else:
            parent = str(calibration["source_measurement_id"])
            _durable_reference._refuse(
                journal_prefix_measurement(p["prefix"]) == parent,
                "a fresh calibration reads its Parent's journal",
            )
            _durable_reference._refuse(
                any(stage["look"] == final for stage in self._measurements[parent]["stages"]),
                "a fresh calibration completes after the Parent's final stage",
            )
        entry = {**p, "seq": seq}

        def apply() -> None:
            self._put_attempt(replace(self._attempt(p["attempt_id"]), calibration_result=entry))

        return apply

    def _comparison_look_evaluated(
        self, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        """Design §6.5, §6.6: one look of the governing comparison, in order,
        over the stage both sides opened, with its decision, tolerance,
        `would_pass_point` and caveats re-derived from its published values.

        The critical veto and every number are the audit's (F4). The look entry
        keeps `decision`, so the held, barred and stage rules read it as they
        read a family-12 look (plan choice 5).
        """

        attempt = self._measuring_attempt(p)
        _durable_reference._refuse(attempt.round == p["round"], "the look names another round")
        comparison = _need(attempt.comparison, "a look follows its comparison_bound")
        # G11: a look decided under a superseded binding stays decided, and
        # every later one is the governing binding's.
        _durable_reference._refuse(
            p["comparison"] not in attempt.superseded_comparisons,
            "the look cites the governing comparison",
        )
        _durable_reference._refuse(
            p["comparison"] == comparison["seq"], "the look names another comparison"
        )
        look = p["look"]
        _durable_reference._refuse(look not in attempt.looks, "the look is already evaluated")
        _durable_reference._refuse(
            look == next_scheduled_look(comparison["looks"], max(attempt.looks, default=0)),
            "looks evaluate in schedule order",
        )
        _durable_reference._refuse(
            look not in attempt.not_started, "the look was recorded as not started"
        )
        final = look == max(comparison["looks"])
        _durable_reference._refuse(p["final"] is final, "final marks the comparison's last look")
        parent = comparison["parent_measurement_id"] or comparison["baseline_measurement_id"]
        _durable_reference._refuse(
            journal_prefix_measurement(p["prefixes"]["candidate"])
            == comparison["candidate_measurement_id"]
            and journal_prefix_measurement(p["prefixes"]["parent"]) == parent,
            "the prefixes are not the comparison's measurements",
        )
        verdict = calibration_verdict(
            comparison, attempt.calibration_result, family_version=self._family_version
        )
        _durable_reference._refuse(
            verdict is None or verdict.close is None, "a closing calibration evaluates no look"
        )
        if final and calibration_owed(comparison):
            _need(attempt.calibration_result, "the calibration completes before the final look")
        tolerance = look_tolerance(
            comparison, attempt.calibration_result, family_version=self._family_version
        )
        _durable_reference._refuse(
            p["tolerance"] == tolerance, "the tolerance is not the comparison's"
        )
        self._check_look_estimates(comparison, final, p)
        try:
            expected = look_decision(
                comparison,
                final=final,
                focus=p["focus"],
                causal=p["causal"],
                tolerance=tolerance["value"],
                family_version=self._family_version,
            )
        except ValueError as exc:  # a shape the kernel refuses is a schema fault
            raise SchemaError("the look's published values are not decidable") from exc
        decided = (p["decision"], p["reason"])
        _durable_reference._refuse(
            decided == (expected.decision, expected.reason)
            or decided == ("rejected", "critical_failure"),
            "the decision does not follow the published values",
        )
        if comparison["mode"] == "observe":
            causal = p["causal"]
            points = (
                p["focus"]["point"],
                None if causal is None else causal["point"],
                tolerance["value"],
            )
            expected_pass = would_pass_point14(*points, margin=comparison["margin"])
        else:
            expected_pass = None
        _durable_reference._refuse(
            p["would_pass_point"] == expected_pass,
            "would_pass_point does not follow the point values",
        )
        _durable_reference._refuse(
            p["caveats"]
            == look_caveats(
                comparison,
                tolerance,
                attempt.calibration_result,
                family_version=self._family_version,
                focus=p["focus"],
                causal=p["causal"],
            ),
            "the caveats are not the ones in force",
        )
        entry = {key: value for key, value in p.items() if key != "evidence"}
        entry.update(seq=seq, record_digest=hashlib.sha256(record.encode()).hexdigest())

        def apply() -> None:
            current = self._attempt(p["attempt_id"])
            self._put_attempt(replace(current, looks={**current.looks, look: entry}))

        return apply

    def _check_look_estimates(
        self, comparison: Mapping[str, Any], final: bool, p: Mapping[str, Any]
    ) -> None:
        """Design §6.5: which estimates a look carries, sided and levelled by mode and role."""

        causal_empty = not comparison["causal"]
        estimates = (("focus", p["focus"]), ("causal", p["causal"]), ("overall", p["overall"]))
        # E1 (a), maintainer ruling 2026-10-04 (design §6.5 Amendments): only an
        # interim statistical focus may have no observed retained case. Checked
        # first, so a final or observe look refuses with this rule (plan F12);
        # whether the scope really had none is the audit's, which reads rows.
        _durable_reference._refuse(
            all(
                value is None
                or value["point"] is not None
                or (name == "focus" and comparison["mode"] == "statistical" and not final)
                for name, value in estimates
            ),
            "a null point belongs to an interim statistical focus",
        )
        if comparison["mode"] == "observe":
            _durable_reference._refuse(final, "observe evaluates only its final look")
            _durable_reference._refuse(
                all(
                    value is None or value["unavailable"] == NOT_REQUESTED_IN_OBSERVE
                    for _, value in estimates
                ),
                "an observe look requests no bound",
            )
            _durable_reference._refuse(
                (p["causal"] is None) == causal_empty and p["overall"] is not None,
                "observe reports causal exactly for a non-empty C, and overall",
            )
            return
        _durable_reference._refuse(
            all(
                value is None or value["unavailable"] != NOT_REQUESTED_IN_OBSERVE
                for _, value in estimates
            ),
            "a statistical look is not an observe point",
        )
        confidence = comparison["confidence"]
        _durable_reference._refuse(
            p["focus"]["level"] == confidence["focus"], "the focus level is the focus confidence"
        )
        if not final:
            _durable_reference._refuse(
                p["causal"] is None and p["overall"] is None,
                "an interim look reports the focus only",
            )
            _durable_reference._refuse(
                (p["focus"]["lower"] is None) == (p["focus"]["upper"] is None),
                "an interim focus interval is two-sided",
            )
            return
        _durable_reference._refuse(
            all(value is None or value["upper"] is None for _, value in estimates),
            "a final statistical bound is one-sided",
        )
        _durable_reference._refuse(
            (p["causal"] is None) == causal_empty, "causal is reported exactly for a non-empty C"
        )
        if p["causal"] is not None:
            _durable_reference._refuse(
                p["causal"]["level"] == confidence["causal"],
                "the causal level is the causal confidence",
            )
        _durable_reference._refuse(
            p["overall"] is not None
            and p["overall"]["level"]
            == global_level(confidence["focus"], confidence["causal"], causal_empty=causal_empty),
            "the overall level is the global level",
        )

    def _check_confidence_stage(
        self, attempt: AttemptState, stages: Sequence[Mapping[str, Any]], look: int
    ) -> None:
        """Design G10: a family-13 Candidate stage follows its comparison, at
        the next look of its frozen schedule (ADR-0027, §6.5); the fresh
        Parent's stage follows the Candidate's of the same look."""

        comparison = _need(attempt.comparison, "a family-13 stage follows its comparison_bound")
        self._check_superseded_stage(attempt)
        _durable_reference._refuse(
            binding_close(comparison) is None, "an Attempt closed at binding opens no stage"
        )
        # A measurement a `continue` re-binds starts with no stage; the
        # Attempt's looks say where it stands (I2 spec §5.2).
        reached = int(stages[-1]["look"]) if stages else max(attempt.looks, default=0)
        _durable_reference._refuse(
            look == next_scheduled_look(comparison["looks"], reached),
            "a stage opens the next look of the comparison's schedule",
        )
        if not calibration_owed(comparison):
            return
        calibration = _need(
            attempt.calibration, "an owed calibration is bound before the first stage"
        )
        if calibration["source"] == "baseline":
            _need(
                attempt.calibration_result,
                "a baseline calibration completes before the first stage",
            )
        verdict = calibration_verdict(
            comparison, attempt.calibration_result, family_version=self._family_version
        )
        _durable_reference._refuse(
            verdict is None or verdict.close is None, "a closing calibration opens no stage"
        )

    def _check_superseded_stage(self, attempt: AttemptState) -> None:
        """G11: a re-bound Candidate or Parent opens no stage before the
        superseding comparison that names it ("before further Candidate work")."""

        _durable_reference._refuse(
            not superseding_comparison_owed(attempt, family_version=self._family_version),
            "a superseding comparison precedes further work on a re-bound measurement",
        )

    def _stage_opened(self, p: dict[str, Any], op: str | None, seq: int) -> Callable[[], None]:
        measurement = _need(
            self._measurements.get(p["measurement_id"]), "the measurement is not bound"
        )
        _durable_reference._refuse(
            not measurement.get("frozen", False), "a frozen measurement is never appended to"
        )
        stages: tuple[dict[str, Any], ...] = measurement["stages"]
        _durable_reference._refuse(
            p["stage"] == len(stages) + 1, "stages of a measurement open in order"
        )
        _durable_reference._refuse(
            p["look"] in self.policy.looks, "the look is not declared by the policy"
        )
        scoped_gate = (
            self._family15 is not None
            and self._family15.policy.focus_first
            and measurement.get("role") in {"candidate", "parent"}
        )
        _durable_reference._refuse(
            scoped_gate or not stages or p["look"] > stages[-1]["look"], "stage looks must increase"
        )
        # Family 13 (design §7): a cohort member is a campaign-level
        # measurement. Its Champion's `attempt_id` is `CHAMPION_SIDE` and a
        # finalist's Attempt is closed, so neither reaches the Attempt rules.
        member = self._cohort_member(str(p["measurement_id"]))
        if self._family15 is not None and self._family15.owns_final(measurement):
            self._family15.check_stage(measurement, p)
        elif member is not None:
            self._check_cohort_stage(member, int(p["stage"]), int(p["look"]))
        elif measurement["attempt_id"] != CHAMPION_SIDE:
            attempt = self._attempt(measurement["attempt_id"])
            role = measurement.get("role")
            if attempt.outcome is None:
                # G2 spec §4.6: each stage opens on both measurements, the
                # Candidate's first; a fresh-Parent Attempt opens none before
                # its Parent is bound.
                if self._confidence and role == "candidate":
                    # Design G10: the comparison and an owed
                    # calibration precedes every Candidate stage; the Parent's
                    # stage follows the Candidate's stage of its look.
                    self._check_confidence_stage(attempt, stages, p["look"])
                if role == "candidate":
                    _durable_reference._refuse(
                        not self.policy.fresh_parent or attempt.parent_measurement_id is not None,
                        "a fresh-Parent attempt opens no stage before its fresh Parent is bound",
                    )
                elif role == "parent":
                    if self._confidence:
                        self._check_superseded_stage(attempt)
                    candidate = self._measurements[str(attempt.measurement_id)]
                    _durable_reference._refuse(
                        any(
                            stage["stage"] == p["stage"] and stage["look"] == p["look"]
                            for stage in candidate["stages"]
                        ),
                        "the fresh Parent's stage follows the Candidate's stage of the same look",
                    )
            if attempt.outcome is None:
                if stages and not scoped_gate:
                    previous = attempt.looks.get(stages[-1]["look"])
                    _durable_reference._refuse(
                        self._all_looks_charged(attempt),
                        "a later stage requires all evaluated alpha",
                    )
                    _durable_reference._refuse(
                        previous is not None and previous["decision"] == "continue",
                        "the previous look did not decide continue",
                    )
                elif attempt.looks:
                    # I2 spec §5.2: a measurement re-bound under `continue`
                    # starts with no stage while its Attempt already has
                    # looks; they go on from the next look number.
                    latest = attempt.looks[max(attempt.looks)]
                    _durable_reference._refuse(
                        self._all_looks_charged(attempt),
                        "a later stage requires all evaluated alpha",
                    )
                    _durable_reference._refuse(
                        latest["decision"] == "continue",
                        "the previous look did not decide continue",
                    )
                    _durable_reference._refuse(
                        p["look"] > max(attempt.looks), "stage looks must increase"
                    )
            else:
                _durable_reference._refuse(
                    role != "parent", "a closed attempt's fresh Parent opens no stage"
                )
                # G2 spec §3.2, §4.7: in family 12 a closed Attempt's
                # measurement is sealed; the winner is completed by import.
                _durable_reference._refuse(
                    not True, "a closed family-12 attempt's measurement is sealed"
                )
                state = self._rounds[attempt.round]
                closed = _need(state.closed, "a closed attempt opens no further stage")
                selection = _need(state.selection, "a closed attempt opens no further stage")
                _durable_reference._refuse(
                    closed["outcome"] == "promoted"
                    and selection["attempt_id"] == attempt.attempt_id,
                    "only the promoted winner completes its measurement",
                )
        if p["sample_count"] == 0:
            # Only a carried first-round baseline has a stage that dispatches
            # nothing (baseline inheritance §3.3): `sample_count` counts the
            # rows a stage dispatches, never its carried ones.
            # I2 spec §5.3 widens it to any measurement a `measurement_rebound`
            # created with carried rows.
            linked = self.identity.get("linked_from")
            baseline = linked.get("baseline") if isinstance(linked, Mapping) else None
            _durable_reference._refuse(
                (
                    p["measurement_id"] == measurement_id_for(None)
                    and baseline is not None
                    and baseline["imported"] > 0
                    and self._baseline_abandoned is None
                )
                or (
                    measurement.get("rebound_from") is not None
                    and measurement.get("imported", 0) > 0
                )
                # G2b4: and to a family-12 completion born with the winner's rows.
                or (measurement.get("role") == "completion" and measurement.get("imported", 0) > 0),
                "only a carried first-round baseline opens a stage of no samples",
            )
        entry = dict(stage=p["stage"], look=p["look"], sample_count=p["sample_count"])
        if "scope" in p:
            entry["scope"] = p["scope"]

        def apply() -> None:
            record = self._measurements[p["measurement_id"]]
            record["stages"] = (*record["stages"], entry)
            self._stage_seq[p["measurement_id"]] = seq
            self.denominator += p["sample_count"]
            self._feed_owner_windows(record, None, p["sample_count"])

        return apply

    def _instance_attested(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        measurement = _need(
            self._measurements.get(p["measurement_id"]), "the measurement is not bound"
        )
        _durable_reference._refuse(
            p["instance_id"] not in self._instances, "the instance is already attested"
        )
        _durable_reference._refuse(
            p["loaded_candidate_digest"] == measurement["candidate_digest"],
            "the attested instance loaded another candidate",
        )
        # `admitted()` verifies the durable identity and nothing else (§8), so a
        # row whose identity and flat keys disagree would let a forged journal
        # pass a check the executor believes it made over the row it recorded.
        identity = TargetIdentity.from_payload(p["identity"])
        _durable_reference._refuse(
            identity.instance.loaded_candidate_digest == p["loaded_candidate_digest"],
            "the identity's loaded candidate digest is not the row's",
        )
        _durable_reference._refuse(
            identity.semantic.dependency_digest == p["dependency_digest"],
            "the identity's dependency digest is not the row's",
        )
        _durable_reference._refuse(
            identity.instance.boot_id == p["boot_id"],
            "the identity's boot id is not the row's",
        )
        entry = dict(
            instance_id=p["instance_id"],
            loaded_candidate_digest=p["loaded_candidate_digest"],
            dependency_digest=p["dependency_digest"],
            boot_id=p["boot_id"],
            identity=p["identity"],
        )

        def apply() -> None:
            record = self._measurements[p["measurement_id"]]
            record["instances"] = (*record["instances"], entry)
            self._instances.add(p["instance_id"])

        return apply

    def _indeterminate_observed(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        _durable_reference._refuse(
            p["measurement_id"] in self._measurements, "the measurement is not bound"
        )
        key: tuple[Any, ...] = (p["measurement_id"], p["case_id"], p["sample_id"])
        reserves = 0
        follows = False
        ordinal = p["retry_ordinal"]
        retries = _need(self.policy.sample_retries, "family 6 requires sample_retries")
        # I1 spec §5.4: a repair try is outside `sample_retries`, one more
        # ordinal per fault-row suspension, and reserves no retry (the
        # check below: its ordinal stands at or past the limit, or the
        # executor, which knows it is a repair, says none follows).
        _durable_reference._refuse(
            ordinal <= ordinal_limit(retries, self._granting_suspensions()),
            "retry_ordinal exceeds the policy's sample_retries",
        )
        follows = bool(p["retry_follows"])
        _durable_reference._refuse(
            not follows or ordinal < retries,
            "a try at the retry limit reserves no retry",
        )
        if ordinal > 0:
            reserved = self._retries_reserved.get((*key, ordinal - 1))
            granted = None
            if reserved is False:
                # Only a denied try's successor needs the grant lookup:
                # an automatic grant or (family 13, pre-F8 spec §2.4) a
                # listing's, which starts a repair or a restored D1 retry.
                granted = self._pair_grant(
                    str(p["measurement_id"]), str(p["case_id"]), str(p["sample_id"])
                )
            _durable_reference._refuse(
                reserved is not False or (granted is not None and granted[0] == ordinal - 1),
                "the previous try recorded that no retry follows it",
            )
        key = (*key, ordinal)
        reserves = int(follows)
        _durable_reference._refuse(key not in self._accounted, "the sample is already accounted")
        owner = p["owner"]
        _durable_reference._refuse(
            p["count"] == self._counts[owner] + 1,
            "count is not the owner's campaign-wide running total",
        )

        def apply() -> None:
            self._accounted.add(key)
            self._retries_reserved[key] = follows
            self._latest_try[(str(p["measurement_id"]), str(p["case_id"]), str(p["sample_id"]))] = (
                int(p["retry_ordinal"]),
                str(owner),
                follows,
                seq,
            )
            self._counts[owner] += 1
            self.denominator += reserves
            measurement = self._measurements[p["measurement_id"]]
            self._feed_owner_windows(measurement, owner, reserves)
            for name, window in self._threshold_windows.items():
                restarted = self._owner_windows.get(name)
                if restarted is None or int(measurement["epoch"]) >= restarted["epoch"]:
                    window.observe(p, seq)

        return apply

    def _feed_owner_windows(
        self, measurement: Mapping[str, Any], owner: str | None, slots: int
    ) -> None:
        """Add one accounted row to every restarted window it belongs to (§6.5).

        A window an in-place change restarted counts only what the new
        condition measured: a measurement bound in the epoch the change opened
        or later. `owner` is the accounted try's (`None` for a stage's slots),
        `slots` the denominator slots the row adds.
        """

        for name, window in self._owner_windows.items():
            if int(measurement["epoch"]) < window["epoch"]:
                continue
            window["denominator"] += slots
            if owner == name:
                window["count"] += 1

    def _pairs_excluded(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        """Continuable-terminals spec §4.1/§4.2, the L half.

        Only a champion-side measurement excludes: the round's baseline, or the
        promoted winner's measurement while it completes (it becomes the next
        round's baseline). The record names the measurement's latest stage,
        precedes that measurement's completion and is written once per
        measurement, whatever its stage.
        Whether the summary matches the measurement prefix is an E fact.
        """

        measurement = _need(
            self._measurements.get(p["measurement_id"]), "the measurement is not bound"
        )
        # ADR-0027, ADR-0016: no pair of a family-13 cohort member is excluded.
        _durable_reference._refuse(
            self._cohort_member(str(p["measurement_id"])) is None,
            self._member_rule("cohort measurements never exclude pairs"),
        )
        _durable_reference._refuse(
            not measurement.get("frozen", False), "a frozen measurement is never appended to"
        )
        _durable_reference._refuse(
            not self._unreserved_threshold_repair(str(p["measurement_id"])),
            "a threshold repair is owed",
        )
        _durable_reference._refuse(
            self._champion_measurement(measurement), "attempt measurements never exclude"
        )
        _durable_reference._refuse(
            p["round"] == measurement["round"], "pairs_excluded names another round"
        )
        _durable_reference._refuse(
            not measurement["complete"], "the measurement is already complete"
        )
        stages: tuple[dict[str, Any], ...] = measurement["stages"]
        _durable_reference._refuse(
            bool(stages) and stages[-1]["stage"] == p["stage"],
            "pairs_excluded names another stage",
        )
        name, stage = str(p["measurement_id"]), int(p["stage"])
        # One record per *measurement*, not merely per stage: exhaustion is
        # cumulative over the measurement's stages, so a second record at a
        # later stage would describe the same set twice and leave the
        # measurement's excluded pairs without a single digest to check
        # (`CampaignRun.excluded_pairs`, which reads the one journaled entry).
        # The executor writes it once by construction (`write_pairs_excluded`
        # returns early when the snapshot already carries the measurement).
        _durable_reference._refuse(
            name not in self._excluded,
            "the measurement already recorded its excluded pairs",
        )
        retries = _need(self.policy.sample_retries, "family 6 requires sample_retries")
        tries = ordinal_limit(retries, self._granting_suspensions()) + 1
        _durable_reference._refuse(
            all(pair["tries"] <= tries for pair in p["pairs"]),
            "a pair names more tries than the policy allows",
        )
        entry = (int(p["count"]), str(p["pairs_digest"]))
        prefix = str(p["evidence"][0]["locator"])
        summary = dict(
            count=int(p["count"]),
            by_owner=dict(p["by_owner"]),
            pairs=[dict(pair) for pair in p["pairs"]],
            truncated=bool(p["truncated"]),
        )

        def apply() -> None:
            self._excluded.setdefault(name, {})[stage] = entry
            self._exclusions[name] = summary
            if self._confidence:
                self._exclusion_prefixes[name] = prefix

        return apply

    def _stage_redispatched(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """#109 (a28 spec §3.4): a carried stage's unfilled slots move into this window.

        A stage positioned before the latest suspension -- in practice one a
        drift left open (ADR-0008 as amended 2026-10-02) -- is dispatched again
        after it. Its unfilled slots were booked when it opened, so they sit in
        the window's base; the record takes `count` of them out of the base and
        leaves the campaign total alone, so they count in the window where the
        rows run. It moves slots, never counts (a27 spec §3.3).

        At most one per measurement per window: the record moves the stage's
        position past the latest suspension. `count` is bounded by what the
        stage and its reserved retries booked; that it is exactly the unfilled
        slots over the cited prefix is an E fact, as is that the prefix is the
        journal's head. The lifecycle holds the prefix only to its form and to
        the lower bound of every earlier citation of that journal, from the
        locators alone: without it a record could cite a prefix from before
        rows that already ran and claim their slots again (codex round 1,
        drift-slots-stale-prefix-overcounts-transfer).
        """

        _durable_reference._refuse(
            stage_redispatch_required(self.producing_version),
            "stage_redispatched needs producing version 0.3.0a28 or later",
        )
        measurement_id = str(p["measurement_id"])
        measurement = _need(self._measurements.get(measurement_id), "the measurement is not bound")
        _durable_reference._refuse(
            not measurement.get("frozen", False), "a frozen measurement is never appended to"
        )
        _durable_reference._refuse(
            not measurement["complete"], "the measurement is already complete"
        )
        # Design §8: a member of an undecided cohort may take a same-stage
        # redispatch. `_append_target` names an unfinished current member
        # since F7 (D9); the grant class also holds a completed member, which
        # the next refusal stops. The rule is unchanged.
        _durable_reference._refuse(
            self._append_target(measurement_id) is not None
            or self._cohort_measurement(measurement),
            "the planner appends to no such measurement",
        )
        _durable_reference._refuse(
            not self._completed_member(measurement),
            "a completed member takes no further try",
        )
        stages: tuple[dict[str, Any], ...] = measurement["stages"]
        _durable_reference._refuse(
            bool(stages) and stages[-1]["stage"] == p["stage"],
            "stage_redispatched names another stage",
        )
        suspended = self._last_suspension_seq
        position = self._stage_seq.get(measurement_id)
        _durable_reference._refuse(
            suspended is not None and position is not None and position < suspended,
            "the stage was not carried across the latest suspension",
        )
        count = int(p["count"])
        _durable_reference._refuse(
            count <= int(stages[-1]["sample_count"]) + self._reserved_retries(measurement_id),
            "count exceeds the slots the stage reserved",
        )
        cited = _journal_prefix_end(p["evidence"][0].get("locator"), measurement_id)
        _durable_reference._refuse(
            cited is not None, "stage_redispatched does not cite its measurement's journal prefix"
        )
        assert cited is not None
        _durable_reference._refuse(
            cited >= self._cited_ends.get(measurement_id, 0),
            "the cited prefix ends before an earlier citation of the measurement",
        )

        def apply() -> None:
            self._window_base_denominator -= count
            self._stage_seq[measurement_id] = seq
            # The `stage_opened` epoch rule; a no-op in practice, since an
            # in-place change re-binds every measurement the planner appends
            # to, so a carried stage is never in a restarted window's epoch.
            self._feed_owner_windows(measurement, None, count)

        return apply

    def _reserved_retries(self, measurement_id: str) -> int:
        """The measurement's accounted tries whose record reserved a retry."""

        return sum(
            1
            for key, follows in self._retries_reserved.items()
            if follows and key[0] == measurement_id
        )

    def _repairs_dispatched(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """#78 (spec §3.5): one dispatch's threshold repairs take denominator slots.

        A threshold-granted repair takes a slot whatever it ends in, so a window
        of repairs alone has a denominator a rate threshold can cross. One record
        per measurement per window: a reservation belongs to the window that
        holds it, a suspension starts a new one, and an unspent grant is reserved
        again there. `count` is at most the pairs the lifecycle names; that it is
        exactly the owed set is an E fact.
        """

        measurement_id = str(p["measurement_id"])
        measurement = _need(self._measurements.get(measurement_id), "the measurement is not bound")
        _durable_reference._refuse(
            not measurement.get("frozen", False), "a frozen measurement is never appended to"
        )
        _durable_reference._refuse(
            not measurement["complete"], "the measurement is already complete"
        )
        _durable_reference._refuse(
            self._champion_measurement(measurement)
            or self._open_attempt_measurement(measurement)
            or self._cohort_measurement(measurement),
            "attempt measurements never repair on a threshold",
        )
        _durable_reference._refuse(
            not self._completed_member(measurement),
            "a completed member takes no further try",
        )
        suspended = self._last_suspension_seq
        named = self._threshold_named().get(measurement_id, {})
        _durable_reference._refuse(
            suspended is not None and bool(named), "no threshold suspension grants a repair"
        )
        assert suspended is not None
        previous = self._dispatch.get(measurement_id)
        _durable_reference._refuse(
            previous is None or previous[0] < suspended,
            "the measurement already reserved its repairs in this window",
        )
        count = int(p["count"])
        _durable_reference._refuse(
            count <= len(named), "count exceeds the pairs threshold suspensions grant"
        )

        def apply() -> None:
            self._dispatch[measurement_id] = (seq, count)
            self.denominator += count
            self._feed_owner_windows(measurement, None, count)

        return apply

    def _unreserved_threshold_repair(self, measurement_id: str) -> bool:
        """#78 (spec §3.7): a named pair whose naming follows the latest reservation.

        Only from a producing version that owes threshold repairs. A pair
        `_threshold_grant` names is exhausted (at the retry limit, or a repair
        at any ordinal); in family 13 a listed cohort gap (pre-F8 spec §2.3)
        may instead be a cancelled or a declined try, granted by the listing,
        and in families 13-14 so may a listed Champion gap (#194 G2,
        `_champion_listed_grant`).
        Either way each named pair must have had a `repairs_dispatched` after
        the suspension that named it; whether the try really started is the
        audit's (`_check_measurement_complete`, `_check_pairs_excluded`).
        Frozen measurements are not filtered (pre-F8 spec §2.5): a member
        recorded complete before a `continue` has no gap, so no listing names
        it.
        """

        if not threshold_repair_required(self.producing_version):
            return False
        reserved = self._dispatch.get(measurement_id, (0, 0))[0]
        return any(
            naming > reserved
            for _ordinal, naming in self._threshold_named().get(measurement_id, {}).values()
        )

    def _granting_suspensions(self) -> int:
        """How many suspensions granted repair tries (I1 spec §5.4, #78 spec §3.4).

        A `fault_rows` suspension grants one repair per row it names and a
        `threshold` suspension one per pair its owner exhausted, so each bounds
        how many ordinals past `sample_retries` a sample can have reached; which
        pairs really were granted is the audit's to check. From a version that
        owes held repair, a suspension of any kind can name an open Attempt's
        exhausted pair (a27 spec §3.1, #116), so it counts too. `0` below
        family 7.
        """

        return sum(
            1
            for entry in self._suspensions
            if entry["evidence_kind"] in ("fault_rows", "threshold")
            or held_repair_required(entry["version"])
        )

    def _threshold_named(self) -> dict[str, dict[tuple[str, str], tuple[int, int]]]:
        """#78 (spec §3.2, conditions 1-4): the pairs threshold suspensions grant.

        From a27 (spec §3.1, #116) any suspension can name an open Attempt's
        pair; such a grant is threshold-kind for `repairs_dispatched` (§3.2).

        Family 13 (pre-F8 spec §2.3) adds a second source: a `cohort_rows`
        suspension's listing grant (`_cohort_listed`), so a pair may be named
        with no accounted try (a cancelled one). Families 13-14 (#194 G2, plan
        choice 4) add a third: a `champion_rows` suspension's listing grant
        (`_champion_listed`), the same way.

        `measurement_id -> {(case_id, sample_id): (ordinal, naming_seq)}`, one
        entry per pair `_pair_grant` grants -- the merge of every source.
        Frozen measurements are kept: callers filter them, and a re-bound
        source's unspent grants are released (`measurement_rebound.released`).
        Whether the repair has started is the measurement journal's (condition
        5). The keys are `_latest_try`'s, then any listed pair with no accounted
        try, so families 7-12 (no listing) keep their order.
        """

        named: dict[str, dict[tuple[str, str], tuple[int, int]]] = {}
        if not self._suspensions:
            return named
        for measurement_id, case_id, sample_id in dict.fromkeys(
            [*self._latest_try, *self._cohort_listed, *self._champion_listed]
        ):
            granted = self._pair_grant(measurement_id, case_id, sample_id)
            if granted is not None:
                named.setdefault(measurement_id, {})[(case_id, sample_id)] = granted
        return named

    def _threshold_grant(
        self, measurement_id: str, case_id: str, sample_id: str
    ) -> tuple[int, int] | None:
        """#78 (spec §3.2, conditions 1-4): one pair's threshold grant, if any.

        `(ordinal, naming_seq)` when the pair belongs to an open champion
        measurement with no `pairs_excluded`, its latest accounted try denied a
        retry and is exhausted (at the retry limit, or a repair), and a later
        threshold suspension of that try's owner names it -- the first one
        after the try. Frozen measurements are kept, as in `_threshold_named`.
        An open Attempt's pair is named by the first suspension after the try
        that qualifies under its own producing version (a27 spec §3.1, §5;
        codex branch review round 1): any kind and owner under a version that
        owes held repair; otherwise only a threshold suspension of the try's
        owner under a version that owes Attempt suspension (#91). An exempt
        suspension therefore never masks a later qualifying one. A member of
        an undecided family-13 cohort is the third class (design §8): it is
        named only by the a27 rule, never by the owner-threshold one. Costs
        one pass over the suspensions, not over every pair.

        This is the automatic source only; family 13's listing grants (pre-F8
        spec §2.3) are `_listed_grant`'s, and `_pair_grant` merges the two.
        """

        latest = self._latest_try.get((measurement_id, case_id, sample_id))
        if latest is None:
            return None
        ordinal, owner, follows, seq = latest
        if follows:
            return None
        measurement = self._measurements[measurement_id]
        if measurement["complete"] or measurement_id in self._excluded:
            return None
        champion = self._champion_measurement(measurement)
        cohort = self._cohort_measurement(measurement)
        if not champion and not cohort and not self._open_attempt_measurement(measurement):
            return None
        repaired = self._repaired((measurement_id, case_id, sample_id), ordinal)
        if ordinal < (self.policy.sample_retries or 0) and not repaired:
            return None
        for entry in self._suspensions:
            at, version = int(entry["seq"]), str(entry["version"])
            if at <= seq:
                continue
            owners_threshold = entry["evidence_kind"] == "threshold" and entry["owner"] == owner
            if champion:
                qualifies = owners_threshold
            elif held_repair_required(version):
                # a27 spec §3.1 (#116): any suspension names an open Attempt's
                # exhausted pair, and (design §8) a cohort member's the same way.
                qualifies = True
            elif cohort:
                # Design §8: only the a27 rule names a cohort member's pair,
                # never the owner-threshold rule. Family 13 is produced only
                # where it holds, but the predicate is still evaluated.
                qualifies = False
            else:
                qualifies = owners_threshold and attempt_suspension_required(version)
            if qualifies:
                return (ordinal, at)
        return None

    def _repaired(self, key: tuple[str, str, str], ordinal: int) -> bool:
        """Is the accounted try at `ordinal` a repair? (#78's inference.)

        A D1 retry at `k` always follows an accounted try at `k - 1` that
        reserved it (`retry_follows: true`); anything else at `k > 0` is a
        repair -- including a fault-row repair, whose fault try is never
        accounted as an `indeterminate_observed`. Family 13 (pre-F8 spec §2.4):
        a predecessor a listing restored (`_restored`) counts as reserving, so
        its successor is a restored D1 retry, not a repair.
        """

        return (
            ordinal > 0
            and self._retries_reserved.get((*key, ordinal - 1)) is not True
            and (*key, ordinal - 1) not in self._restored
        )

    def _listed_grant(self, key: tuple[str, str, str]) -> tuple[int, int] | None:
        """Family 13 (pre-F8 spec §2.3, plan choice 2): a pair's listing grant.

        `(ordinal, naming_seq)` while its member is still in the third class
        (`_cohort_measurement`), not recorded complete and with no exclusion;
        evaluated here, at read time, so a `selection_decided` or a round
        close ends it with no bookkeeping. Frozen measurements are kept, as in
        `_threshold_named`.
        """

        entry = self._cohort_listed.get(key)
        if entry is None:
            return None
        measurement = self._measurements[key[0]]
        if (
            measurement["complete"]
            or key[0] in self._excluded
            or not self._cohort_measurement(measurement)
        ):
            return None
        return entry

    def _champion_listed_grant(self, key: tuple[str, str, str]) -> tuple[int, int] | None:
        """Families 13-14 (#194 G2, plan choice 4): a Champion pair's listing grant.

        `_listed_grant`'s twin for a `champion_rows` listing: `(ordinal,
        naming_seq)` while its measurement is still a Champion measurement
        (`_champion_measurement`), not complete and with no exclusion;
        evaluated at read time, so a `baseline_complete` ends it with no
        bookkeeping. Frozen measurements are kept, as in `_threshold_named`
        and `_listed_grant`; callers filter them (controller ruling
        2026-10-13, deviating from plan choice 4's "not frozen"): a
        `continue` freezes the source before its `measurement_rebound`, whose
        `released` must still account the source's reserved and unrun repair
        slots through `threshold_named[source]` (`_unspent_reservation`).
        """

        entry = self._champion_listed.get(key)
        if entry is None:
            return None
        measurement = self._measurements[key[0]]
        if (
            measurement["complete"]
            or key[0] in self._excluded
            or not self._champion_measurement(measurement)
        ):
            return None
        return entry

    def _pair_grant(
        self, measurement_id: str, case_id: str, sample_id: str
    ) -> tuple[int, int] | None:
        """One pair's grant from every source (pre-F8 spec §2.3, plan choice 1).

        The automatic `_threshold_grant`, the family-13 listing grant
        (`_listed_grant`) and the #194 Champion listing grant
        (`_champion_listed_grant`): the one at the highest ordinal, and on an
        equal ordinal the earliest naming -- one grant per `(pair, ordinal)`. A
        lower-ordinal grant is spent, since a try past it has started. Without
        a listing (families 7-12) this is `_threshold_grant` exactly.
        """

        key = (measurement_id, case_id, sample_id)
        found = [
            grant
            for grant in (
                self._threshold_grant(measurement_id, case_id, sample_id),
                self._listed_grant(key),
                self._champion_listed_grant(key),
            )
            if grant is not None
        ]
        return max(found, key=lambda grant: (grant[0], -grant[1])) if found else None

    def _cohort_measurement(self, measurement: Mapping[str, Any]) -> bool:
        """Design §8 and ADR-0019 as amended 2026-10-03: a member of a cohort
        whose round has no `selection_decided` -- the third repair-grant class.

        The class also ends when the round closes (F7, the maintainer's
        answer on point 4): a `rebaseline` closes an undecided cohort's round
        `condition_changed` with no selection, and no grant names its frozen
        members after that. A completed member stays in the class but takes
        no further try (`_completed_member`), so a grant named on it is never
        spent."""

        if self._family15 is not None and self._family15.owns_final(measurement):
            state = self._rounds[int(measurement["round"])]
            return state.selection is None and state.closed is None
        if (
            measurement.get("role")
            != campaign_vocabulary_for(14 if self._family_version == 14 else 13).registry_role
        ):
            return False
        state = self._rounds[int(measurement["round"])]
        return state.selection is None and state.closed is None

    def _recorded_complete(self, measurement: Mapping[str, Any]) -> bool:
        """F7 plan choice 14 over this lifecycle's rounds (`recorded_complete`)."""

        return recorded_complete(measurement, self._rounds)

    def _completed_member(self, measurement: Mapping[str, Any]) -> bool:
        """A cohort member with a `cohort_measurement_completed` (design §6.9)."""

        if self._family15 is not None and self._family15.owns_final(measurement):
            return bool(measurement["complete"])
        if (
            measurement.get("role")
            != campaign_vocabulary_for(14 if self._family_version == 14 else 13).registry_role
        ):
            return False
        state = self._rounds[int(measurement["round"])]
        return str(measurement["measurement_id"]) in state.cohort_completed

    def _open_attempt_measurement(self, measurement: Mapping[str, Any]) -> bool:
        """#91 spec §3.2: a Candidate or fresh-Parent measurement of an open Attempt."""

        side = measurement["attempt_id"]
        if side == CHAMPION_SIDE or measurement.get("role") == "completion":
            return False
        return self._attempt(str(side)).outcome is None

    def _champion_measurement(self, measurement: Mapping[str, Any]) -> bool:
        """A baseline measurement, or the promoted winner's while it completes.

        A family-12 fresh Parent is never one, the promoted winner's included:
        it has no excluded pairs and owes no threshold repair after its Attempt
        closes (ADR-0016 as amended 2026-10-01). In family 12 no Attempt
        measurement is one: the sealed winner is completed by its completion,
        which is a champion-side measurement (G2b4). Nor is a family-13 cohort
        member, the Champion's included: it excludes no pairs (ADR-0027).
        """

        if self._family15 is not None and self._family15.owns_final(measurement):
            return False
        if measurement.get("role") in ("parent", "cohort", "final"):
            return False
        if measurement["attempt_id"] == CHAMPION_SIDE:
            return True
        return False

    def _threshold_reached(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        _durable_reference._refuse(self.threshold is None, "a threshold is recorded once")
        state = self._round(p["round"], "the threshold names an unknown round")
        self._check_barrier_window(state, p["barrier"])
        # The window's counts: the campaign totals until a family-7 suspension
        # moves the window's bases (I1 spec §5.3).
        self._check_crossing(
            p, "count is not the accounted total", "denominator is not the opened stages'"
        )
        entry = dict(p)

        def apply() -> None:
            self.threshold = entry

        return apply

    def _check_barrier_window(self, state: RoundState, barrier: str) -> None:
        """Whether the prefix stands at one of the §7 threshold barriers.

        Spec §5's L rule: "only at a §7 barrier, derived from the accounted set
        and the opened stages". §7 fixes both barriers. The **round** barrier
        is after every attempt of the round is closed and before the round is
        decided ("at a round barrier every attempt is already closed and no
        stage is open"), so a round with no attempt at all has not reached one.
        A **baseline-stage** barrier is the completion of a baseline or
        winner-completion stage, where "no other stage exists": no attempt of
        the round may be open. Whether that stage *completed* is an E-fact the
        executor proves; what the prefix decides is the attempt state.
        """

        if self._family15 is not None and barrier == "final_stage":
            self._family15.check_final_barrier(state)
        elif (
            self._confidence
            and barrier == campaign_vocabulary_for(self._family_version).stage_barrier
        ):
            # Family 13 (design §8, plan choice 8): after a whole cohort stage
            # of the round's undecided cohort -- every member bound, and every
            # member that still runs has opened the same, non-zero number of
            # cohort stages. F7 plan choice 8: over the current members, since
            # a completed member keeps its stages while one a `continue`
            # re-bound restarts at stage 1; once every member has completed,
            # each holds every stage.
            _need(state.cohort, self._member_rule("the round has no bound cohort"))
            _durable_reference._refuse(state.selection is None, "the round is already decided")
            _durable_reference._refuse(state.closed is None, "the round is already closed")
            current = _need(
                state.cohort_current, self._member_rule("a bound cohort has current members")
            )
            _durable_reference._refuse(
                all(name in self._measurements for name in current),
                self._member_rule("a cohort-stage barrier follows every member's binding"),
            )
            running = self._running_members(state) or list(current)
            counts = {len(self._measurements[name]["stages"]) for name in running}
            _durable_reference._refuse(
                len(counts) == 1 and min(counts) >= 1,
                self._member_rule("a cohort-stage barrier follows a whole cohort stage"),
            )
        elif barrier == "round":
            # The same two clauses `_selection_decided` enforces for the record
            # that follows this barrier: every declared attempt opened, and every
            # one of them closed. #91 spec §3.1, §5: from 0.3.0a26 an Attempt
            # awaiting its look's decision (held) counts as settled. Immediate-decision
            # policies decide it before the next Attempt opens.
            holds = self._holds_attempts()
            settled = all(
                attempt.outcome is not None or (holds and self._awaiting_decision(attempt))
                for attempt in state.attempts.values()
            )
            opened = len(state.attempts) == self.policy.attempts_per_round or (
                holds
                and decides_at_once(self.policy)
                and any(self._awaiting_decision(a) for a in state.attempts.values())
            )
            _durable_reference._refuse(opened and settled, "the round barrier follows its attempts")
            _durable_reference._refuse(state.selection is None, "the round is already decided")
            _durable_reference._refuse(state.closed is None, "the round is already closed")
        elif state.closed is not None and state.closed["outcome"] == "promoted":
            # The winner-completion window: §7's other stage barrier, open from
            # the promotion until the winner's own measurement is complete.
            _durable_reference._refuse(
                not state.champion_complete,
                "the winner-completion barrier is past",
            )
        else:
            # The baseline window: from the round's baseline stage until its
            # first attempt opens. "No *open* attempt" was not the barrier -- it
            # is true at every quiet moment of a round, so the same crossing
            # could be relabelled `baseline_stage` and admitted long after the
            # baseline completed.
            _durable_reference._refuse(
                not state.attempts,
                "a baseline-stage barrier precedes the round's attempts",
            )

    def _window_totals(self, owner: str) -> tuple[int, int]:
        """`owner`'s window `(count, denominator)`: its restarted window (I2 spec
        §6.5) or else the one since the latest suspension (I1 spec §5.3)."""

        restarted = self._owner_windows.get(owner)
        count = (
            restarted["count"]
            if restarted is not None
            else self._counts[owner] - self._window_base_counts.get(owner, 0)
        )
        if not self.policy.indeterminate[cast(Owner, owner)].effective_count_recovered:
            count = self._threshold_windows[owner].count(False)
        denominator = (
            restarted["denominator"]
            if restarted is not None
            else self.denominator - self._window_base_denominator
        )
        return count, denominator

    def _check_crossing(self, p: Mapping[str, Any], count_rule: str, denominator_rule: str) -> None:
        """A crossing names the policy's limits, the window's counts, and is reached."""

        owner = p["owner"]
        limits = self.policy.indeterminate[owner]
        _durable_reference._refuse(
            p["max_count"] == limits.max_count, "max_count is not the policy's"
        )
        _durable_reference._refuse(p["max_rate"] == limits.max_rate, "max_rate is not the policy's")
        count, denominator = self._window_totals(owner)
        _durable_reference._refuse(p["count"] == count, count_rule)
        _durable_reference._refuse(p["denominator"] == denominator, denominator_rule)
        if p["kind"] == "count":
            _durable_reference._refuse(
                crosses(count, denominator, limits, "count"), "the count threshold is not reached"
            )
        else:
            _durable_reference._refuse(
                p["count"] >= 1 and p["denominator"] >= 1, "the rate threshold has no samples"
            )
            _durable_reference._refuse(
                crosses(count, denominator, limits, "rate"), "the rate threshold is not reached"
            )

    def _holds_attempts(self) -> bool:
        """Whether the #91 held-Attempt rules apply (spec §3.1, §5): families
        7-12 under a producing version in force that is not exempt."""

        return attempt_suspension_required(self.producing_version)

    def _awaiting_decision(self, attempt: AttemptState) -> bool:
        """#91 spec §3.1: an open Attempt whose latest stage's look is undecided.

        The lifecycle cannot see whether the stage completed or which pairs it
        lacks (the measurement journal's facts); this is the necessary
        condition for a held Attempt, and the audit checks the rest.
        """

        if attempt.outcome is not None or attempt.measurement_id is None:
            return False
        if (
            self._independent
            and attempt.family15 is not None
            and attempt.family15.comparison is not None
        ):
            return False
        stages = self._measurements[attempt.measurement_id]["stages"]
        if not stages:
            return False
        look = int(stages[-1]["look"])
        return look not in attempt.looks and look not in attempt.not_started

    def _threshold_crossed(self) -> bool:
        """Whether any owner's window crosses its `campaign.indeterminate` limit now."""

        return any(
            crosses(*self._window_totals(owner), limits)
            for owner, limits in self.policy.indeterminate.items()
        )

    def _check_not_held(self, attempt: AttemptState) -> None:
        """#91 spec §3.1, §5: an Attempt's look is barred, and it closes
        `incomplete_sample`, only while no threshold is crossed and no
        threshold repair is owed on its measurements; otherwise it stays held."""

        if not self._holds_attempts():
            return
        _durable_reference._refuse(not self._threshold_crossed(), "a threshold is crossed")
        _durable_reference._refuse(
            not any(
                self._unreserved_threshold_repair(measurement_id)
                for measurement_id in (attempt.measurement_id, attempt.parent_measurement_id)
                if measurement_id is not None
            ),
            "a threshold repair is owed",
        )

    def _target_record(self, event: str, p: Mapping[str, Any]) -> tuple[str, str | None] | None:
        """`(measurement_id, instance_id)` of a record an attestation refusal may name."""

        # I2 spec §5.2: a `measurement_rebound` binds its new measurement.
        # G2b3a: a `parent_measurement_bound` binds the fresh Parent.
        # G2b4: a `completion_bound` binds the winner's completion.
        # F5: a `cohort_measurement_bound` binds a family-13 cohort member.
        if self._independent and event in {
            "candidate_completion_bound",
            "champion_final_bound",
            "final_measurement_bound",
        }:
            return (
                str(
                    p["candidate"]["measurement_id"]
                    if event == "final_measurement_bound"
                    else p["measurement_id"]
                ),
                None,
            )
        if event in (
            "baseline_bound",
            "measurement_bound",
            "parent_measurement_bound",
            "measurement_rebound",
        ) or (
            self._confidence
            and event == campaign_vocabulary_for(self._family_version).measurement_bound
        ):
            return (str(p["measurement_id"]), None)
        if event == "instance_attested":
            return (str(p["measurement_id"]), str(p["instance_id"]))
        return None

    @staticmethod
    def _threshold_of(adaptation_payload: object) -> int:
        """Family 10: `reflection.max_consecutive_failures` of an adaptation payload."""

        _durable_reference._refuse(
            isinstance(adaptation_payload, dict), "family 10 needs an adaptation payload"
        )
        assert isinstance(adaptation_payload, dict)
        reflection = adaptation_payload.get("reflection")
        _durable_reference._refuse(
            isinstance(reflection, dict), "family 10 needs the reflection payload"
        )
        assert isinstance(reflection, dict)
        threshold = reflection.get("max_consecutive_failures")
        _durable_reference._refuse(
            isinstance(threshold, int) and not isinstance(threshold, bool),
            "family 10 needs an integer reflection.max_consecutive_failures",
        )
        assert isinstance(threshold, int)
        return threshold

    @staticmethod
    def _group_text_chars_of(adaptation_payload: object) -> int | None:
        """#282: `reflection.group_text_chars` of an adaptation payload that
        `_threshold_of` admitted (512 when absent, R2)."""

        assert isinstance(adaptation_payload, dict)
        return reflection_group_text_chars(adaptation_payload["reflection"])

    # -- family 14: the connection preflight (H10) ----------------------

    def _check_connection_preamble(self, event: str, p: Mapping[str, Any]) -> None:
        """H10 (choice 17, C§4.3, R61): what an owed suspension or an open
        preflight admits next.

        After `connection_preflight_failed` only its `campaign_suspended`
        follows: owner `connection`, the terminal's `status` as the reason,
        and `stage_record` evidence naming the terminal by `seq` and
        operation id. No hygiene record, decision, rebase or second start
        comes between them. While a preflight is open the campaign does not
        finish: its terminal closes it first (C§4.4, R62 recovery).

        After a cancelled role terminal (family 10's durable stop) no preflight
        starts and none fails: its owed suspension would be refused by the
        cancellation rule, and the stop's `campaign_finished` by the open or
        owed preflight. An open preflight closes `interrupted` instead.
        """

        _durable_reference._refuse(
            self._cancelled_role_seq is None
            or event not in (CONNECTION_START_EVENT, CONNECTION_FAILED_EVENT),
            "a cancelled role terminal admits no connection preflight",
        )
        owed = self._connection_owed
        if owed is not None and not self._connection_shadow_settlement(event, p):
            _durable_reference._refuse(
                event == "campaign_suspended",
                "only the owed connection suspension follows a failed preflight",
            )
            _durable_reference._refuse(
                p.get("owner") == "connection" and p.get("reason") == owed["reason"],
                "the owed connection suspension names the failed status",
            )
            evidence = p.get("evidence")
            _durable_reference._refuse(
                isinstance(evidence, Mapping)
                and evidence.get("evidence_kind") == "stage_record"
                and evidence.get("seq") == owed["seq"]
                and evidence.get("operation_id") == owed["operation_id"],
                "the owed connection suspension names the failed preflight",
            )
        _durable_reference._refuse(
            self._connection_open is None or event != "campaign_finished",
            "campaign_finished waits for the open connection preflight",
        )

    def _connection_shadow_settlement(self, event: str, p: Mapping[str, Any]) -> bool:
        owed = self._connection_owed
        return (
            event == "check_shadowed"
            and self._family15 is not None
            and self._family15.snapshot().check_modes.get("connection_preflight") == "shadow"
            and owed is not None
            and owed["reason"]
            in {"connection_preflight_unavailable", "connection_preflight_inconclusive"}
            and p.get("check") == "connection_preflight"
            and p.get("code") == owed["reason"]
            and p.get("operation_id") == owed["operation_id"]
            and p.get("count") == 1
        )

    def _connection_reserved_after(self, event: str) -> int:
        """R62 (choice 16): the slot a possible `campaign_suspended` keeps after
        this record. An open preflight keeps one beside its terminal (which
        `pending` keeps), and a failed terminal keeps one for its owed
        suspension until that suspension is written."""

        if event in (CONNECTION_START_EVENT, CONNECTION_FAILED_EVENT):
            return 1
        if event in (CONNECTION_FINISHED_EVENT, CONNECTION_INTERRUPTED_EVENT):
            return 0
        if event in {"campaign_suspended", "check_shadowed"} and self._connection_owed is not None:
            return 0
        return int(self._connection_open is not None or self._connection_owed is not None)

    def _connection_view(self) -> ConnectionView:
        if not self._connections:
            return ConnectionView()
        operations = tuple(_immutable_binding(entry) for entry in self._connections)
        latest: dict[str, Mapping[str, Any]] = {}
        for entry in operations:
            latest[str(entry["binding_digest"])] = entry
        return ConnectionView(
            operations=operations,
            open=None if self._connection_open is None else operations[self._connection_open],
            owes_suspension=(
                None if self._connection_owed is None else _immutable_binding(self._connection_owed)
            ),
            latest=MappingProxyType(latest),
        )

    def _connection_started(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """C§4.4: one start, under a fresh operation id, while none is open.

        The record binds what the lifecycle can re-derive: the current condition
        epoch, its metric contract and primary metric, the Champion probed, and
        the seed derived from the binding and that Champion (C§4.2, choice 6).
        The binding digest's other inputs are the audit's (F0 R60).
        """

        _durable_reference._refuse(
            op is not None and p["operation_id"] == op,
            "the payload operation id must match the envelope",
        )
        _durable_reference._refuse(
            self._connection_open is None, "a connection preflight is already open"
        )
        epoch = self._epochs[-1]
        _durable_reference._refuse(
            p["epoch"] == len(self._epochs) - 1, "the preflight names another condition epoch"
        )
        _durable_reference._refuse(
            p["metric_contract"] == epoch.get("metric_contract"),
            "the preflight names another metric contract",
        )
        _durable_reference._refuse(
            p["primary_metric"] == epoch["policy"]["metric"]["primary"],
            "the preflight names another primary metric",
        )
        _durable_reference._refuse(
            (p["candidate_digest"], p["dependency_digest"])
            == (self._champion[1], self._champion_dependency),
            "the preflight probes another Champion",
        )
        _durable_reference._refuse(
            p["probe_seed"] == probe_seed(p["binding_digest"], p["candidate_digest"]),
            "the probe seed is not the binding's",
        )
        entry: dict[str, Any] = {
            "operation_id": p["operation_id"],
            "invocation_id": p["invocation_id"],
            "binding_digest": p["binding_digest"],
            "epoch": p["epoch"],
            "seq": seq,
            "candidate_digest": p["candidate_digest"],
            "dependency_digest": p["dependency_digest"],
            "probe_seed": p["probe_seed"],
            "outcome": None,
            "terminal": None,
        }

        def apply() -> None:
            self._connections.append(entry)
            self._connection_open = len(self._connections) - 1

        return apply

    def _connection_terminal(
        self, event: str, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """C§4.4: the one terminal of the open preflight. It cites that
        preflight's own probe journal; a failed one owes its suspension."""

        start = self.pending[str(op)]
        _durable_reference._refuse(
            start.event == CONNECTION_START_EVENT, "the operation is not a connection preflight"
        )
        index = self._connection_open
        if index is None or self._connections[index]["operation_id"] != op:
            raise SchemaError("the operation is not the open connection preflight")
        _durable_reference._refuse(
            all(reference["run_id"] == op for reference in p["evidence"]),
            "the preflight evidence cites another probe journal",
        )
        outcome = (
            "compatible"
            if event == CONNECTION_FINISHED_EVENT
            else "interrupted"
            if event == CONNECTION_INTERRUPTED_EVENT
            else str(p["status"])
        )
        terminal = {
            "event": event,
            "seq": seq,
            **{key: value for key, value in p.items() if key != "evidence"},
        }
        owed = (
            {"seq": seq, "operation_id": op, "reason": p["status"]}
            if event == CONNECTION_FAILED_EVENT
            else None
        )

        def apply() -> None:
            self._connections[index] = {
                **self._connections[index],
                "outcome": outcome,
                "terminal": terminal,
            }
            self._connection_open = None
            self._connection_owed = owed

        return apply

    def _suspension_cause(self, event: str, p: Mapping[str, Any]) -> str | None:
        """The `stage_record` reason a durable record would justify (I1 spec §5.2).

        The grouping's `failed` record (never `interrupted`, which is retried),
        a `probe_checked` with `status: unavailable`, `editor_failed(unresponsive)`
        and `reflection_failed(unresponsive)`. Family 10 adds a settled
        `reflection_failed` of class `provider` (`reflector_provider_failed`)
        or `unknown` (`reflector_failing`), spec §4.2, and a settled
        `editor_failed` of class `provider` (`editor_provider_failed`) or
        `unknown` (`editor_failing`), spec §5.3; the second-skip
        `reflector_failing` is registered by the skip's own close
        (`_unplanned_close`), since it names the round's diagnosis, not the
        close. Family 14 adds a `grouping_failed(role_failed)`, which names its
        Grouper suspension by class (`grouper_suspension_for`, ADR-0034).
        """

        if event == "grouping_failed":
            if p["code"] == "role_failed":
                return grouper_suspension_for(p["error_class"])
            return None if p["code"] == "interrupted" else "grouping_failed"
        if event == "probe_checked":
            if probe_continues(self.identity, self._family_version):
                return None
            # A ready check is a cause only while its probe setup is pending
            # (`_campaign_suspended` checks that): the mediator start-up
            # failure the family-6 probe-setup terminal cites it for.
            return "probe_unavailable" if p["status"] == "unavailable" else "probe_setup"
        if event == "editor_failed":
            if p["error_class"] == "unresponsive":
                return "editor_unresponsive"
            if "reason" in p:
                klass = editor_class(p["error_class"], p["reason"])
                if klass == "provider":
                    return "editor_provider_failed"
                if klass == "unknown":
                    return "editor_failing"
            return None
        if event == "reflection_failed":
            if p["error_class"] == "unresponsive":
                return "reflector_unresponsive"
            if "reason" in p:
                klass = reflection_class(p["error_class"], p["reason"])
                if klass == "provider":
                    return "reflector_provider_failed"
                if klass == "unknown":
                    return "reflector_failing"
            return None
        if event == CONNECTION_FAILED_EVENT:
            # Family 14 (H10, C§4.3): the failed preflight's class is its
            # suspension's reason. Older families refuse the record itself.
            return str(p["status"])
        return None

    def _suspends(self, entry: Mapping[str, Any]) -> bool:
        """A reflection a suspension resolves: an `unresponsive` one, and in
        family 10 a failure of class `provider` or `unknown` (spec §4.2)."""

        return entry["error_class"] == "unresponsive" or (
            reflection_entry_class(entry) in ("provider", "unknown")
        )

    def _campaign_suspended(
        self, p: dict[str, Any], op: str | None, seq: int
    ) -> Callable[[], None]:
        """Family 7's non-terminal suspension (I1 spec §5.2-§5.4, ADR-0019).

        Written where the family-6 infrastructure terminal it replaces is, it
        owes nothing: any record may follow it. Per `evidence_kind`:

        - `threshold`: at a §7 barrier exactly as `threshold_reached`, over the
          window since the previous suspension (`window_start` names it), and
          carrying a recorded `threshold_reached`'s crossing when one stands;
        - `fault_rows`: shape only here; the audit recomputes the rows from the
          measurement prefix. The rows it lists are owed a repair try;
        - `stage_record`: names the one durable record since the previous
          suspension that decided it, by sequence and operation id.

        It then clears every blocking infrastructure state -- a recorded
        threshold, an unresponsive role, a completed grouping failure and an
        unavailable probe check -- so the planner re-runs that step, and moves
        the window's bases to the running totals.
        """

        evidence = p["evidence"]
        kind = evidence["evidence_kind"]
        # #97 spec §5: the out-of-range fault is admitted only under a producing
        # version that owes it; an earlier producer never wrote it.
        _durable_reference._refuse(
            attempt_suspension_required(self.producing_version)
            or (
                p["reason"] != "scorer_metric_out_of_range"
                and not (
                    kind == "fault_rows"
                    and any(
                        row["reason"] == "scorer_metric_out_of_range" for row in evidence["rows"]
                    )
                )
            ),
            "scorer_metric_out_of_range needs producing version 0.3.0a26 or later",
        )
        unavailable = not probe_continues(self.identity, self._family_version) and any(
            state.probe_check is not None and state.probe_check["status"] == "unavailable"
            for state in self._rounds.values()
        )
        repair: frozenset[tuple[str, str, str]] = frozenset()
        # Family 13 (pre-F8 spec §2.3): the gaps a `cohort_rows` listing grants.
        listed: list[tuple[tuple[str, str, str], int]] = []
        # ... and, for the report (F8 plan choice 10), the round it names and its rows.
        cohort_listing: tuple[int, tuple[dict[str, Any], ...]] | None = None
        # Families 13-14 (#194 G2): the Champion gaps a `champion_rows` listing grants.
        champion_listed: list[tuple[tuple[str, str, str], int]] = []
        if kind == "threshold":
            crossing = evidence["crossing"]
            if self.threshold is not None:
                _durable_reference._refuse(
                    {name: self.threshold[name] for name in _CROSSING_KEYS} == crossing,
                    "the suspension does not carry the recorded threshold",
                )
            number = _need(max(self._rounds, default=None), "a threshold needs a round")
            state = self._rounds[number]
            # The one barrier the round can stand at: the winner-completion
            # window of a promoted round, the round barrier once it has
            # attempts, the baseline window before them -- and, in family 13,
            # the cohort-stage barrier while a bound cohort is undecided and
            # its round open (design §8; a `rebaseline` closes it undecided).
            promoted = state.closed is not None and state.closed["outcome"] == "promoted"
            if self._family15 is not None and self._family15.final_members(state):
                barrier = "final_stage"
            elif state.cohort is not None and state.selection is None and state.closed is None:
                barrier = campaign_vocabulary_for(self._family_version).stage_barrier
            else:
                barrier = "round" if state.attempts and not promoted else "baseline_stage"
            assert barrier in (
                THRESHOLD_BARRIERS | {campaign_vocabulary_for(self._family_version).stage_barrier}
            )
            self._check_barrier_window(state, barrier)
            self._check_crossing(
                crossing, "count is not the window's", "denominator is not the window's"
            )
            restarted = self._owner_windows.get(crossing["owner"])
            if restarted is None:
                _durable_reference._refuse(
                    evidence["window_start"] == self._last_suspension_seq,
                    "window_start is not the previous suspension",
                )
            else:
                _durable_reference._refuse(
                    evidence["window_start"] == restarted["start"],
                    "window_start is not the owner's latest change",
                )
        else:
            _durable_reference._refuse(
                self.threshold is None,
                "the suspension does not carry the recorded threshold",
            )
            if kind == "fault_rows":
                repair = frozenset(
                    (row["measurement_id"], row["case_id"], row["sample_id"])
                    for row in evidence["rows"]
                )
            elif self._family15 is not None and kind == "final_rows":
                listed = self._family15.final_listing(p)
            elif (
                self._confidence
                and kind == campaign_vocabulary_for(self._family_version).suspension_evidence_kind
            ):
                # Design §6.13, §8: at a cohort-stage barrier of the open
                # round's undecided cohort. Which pairs the settled stage left
                # without a score (its gaps), and each row's exact ordinal,
                # are the audit's to recompute from the member journals. Each
                # listed gap is granted one try by the listing itself (pre-F8
                # spec §2.3, `_cohort_listed`); an exhausted pair is also
                # named by the existing third-class rule (`_threshold_grant`),
                # listed or not, and `_pair_grant` merges the two.
                _durable_reference._refuse(
                    self._confidence,
                    self._member_rule("cohort_incomplete is a family-13 suspension"),
                )
                number = _need(
                    self._open_round(),
                    self._member_rule("a cohort_incomplete suspension needs a round"),
                )
                state = self._rounds[number]
                _durable_reference._refuse(
                    state.cohort is not None and state.selection is None,
                    self._member_rule("a cohort_incomplete suspension follows an undecided cohort"),
                )
                assert state.cohort is not None
                cohort_listing = (number, tuple(dict(row) for row in evidence["rows"]))
                # The executor's barrier runs only after a whole cohort stage
                # (`cohort_stage_settled`), so the prefix stands at one.
                self._check_barrier_window(
                    state, campaign_vocabulary_for(self._family_version).stage_barrier
                )
                # F7 plan choice 7: by their current ids.
                current = set(
                    _need(
                        state.cohort_current,
                        self._member_rule("a bound cohort has current members"),
                    )
                )
                _durable_reference._refuse(
                    all(str(row["measurement_id"]) in current for row in evidence["rows"]),
                    self._member_rule("cohort rows name cohort members"),
                )
                if not evidence["truncated"]:
                    # `OWNERS` is `arbitrate_owner`'s order: `campaign_statistics`
                    # defines its `_OWNER_ORDER` as it (that module imports this
                    # one, so `arbitrate_owner` cannot be imported here).
                    owners = {str(row["owner"]) for row in evidence["rows"]}
                    _durable_reference._refuse(
                        p["owner"] == next(owner for owner in OWNERS if owner in owners),
                        "the suspension's owner arbitrates its rows",
                    )
                # Pre-F8 spec §2.3, plan choice 11: S cannot see a cancelled
                # try, so it checks what it can of each listed ordinal -- at or
                # past the pair's latest accounted try, which then declined its
                # retry, and within the ordinal bound (this suspension not yet
                # counted). The exact value is the audit's (§2.6).
                retries = self.policy.sample_retries or 0
                limit = ordinal_limit(retries, self._granting_suspensions())
                for row in evidence["rows"]:
                    pair = (str(row["measurement_id"]), str(row["case_id"]), str(row["sample_id"]))
                    ordinal = int(row["retry_ordinal"])
                    latest = self._latest_try.get(pair)
                    if latest is not None:
                        _durable_reference._refuse(
                            ordinal >= latest[0],
                            self._member_rule(
                                "a cohort row's retry_ordinal is not before its pair's accounted try"
                            ),
                        )
                        _durable_reference._refuse(
                            ordinal != latest[0] or not latest[2],
                            self._member_rule(
                                "a cohort row names an accounted try that reserved its retry"
                            ),
                        )
                    _durable_reference._refuse(
                        ordinal <= limit,
                        self._member_rule("a cohort row's retry_ordinal exceeds the ordinal limit"),
                    )
                    listed.append((pair, ordinal))
            elif kind == "champion_rows":
                champion_listed = self._champion_listing(p, evidence)
            else:
                if p["reason"] in {"target_changed", *AFFECTED_SUSPENSIONS}:
                    # An attestation refusal (fix round 1): the record binding
                    # or attesting the refused measurement. Whether its
                    # attestation really fails is the audit's to recompute. A
                    # component-drift refusal cites the refused measurement's
                    # binding record the same way; whether the drift is real
                    # is the audit's to recompute too (G2b3b Task 6).
                    target = self._target_records.get(evidence["seq"])
                    _durable_reference._refuse(
                        target is not None, "stage_record names no target record"
                    )
                    assert target is not None
                    _durable_reference._refuse(
                        target[2] == evidence["operation_id"],
                        "stage_record names another operation",
                    )
                    # ADR-0019 as amended 2026-10-02: a drift's refusal prefix
                    # is that measurement's own journal; from 0.3.0a27 a
                    # `target_changed` may cite one the same way (a27 §4.2),
                    # which an exempt producer never wrote.
                    if "prefix" in evidence:
                        _durable_reference._refuse(
                            p["reason"] != "target_changed"
                            or refusal_prefix_required(self.producing_version),
                            "a target_changed prefix needs producing version 0.3.0a27 or later",
                        )
                        _durable_reference._refuse(
                            refusal_prefix_measurement(evidence["prefix"]) == target[0],
                            "the refusal prefix is not the refused measurement's journal",
                        )
                    cause: tuple[str, str | None] | None = None
                else:
                    if p["reason"] in CONNECTION_SUSPENSIONS:
                        # H10 (C§4.3, R61): written back to back with its
                        # failed terminal, with no intervening record.
                        _durable_reference._refuse(
                            evidence["seq"] == seq - 1,
                            "a connection suspension follows its failed preflight",
                        )
                    cause = self._suspension_causes.get(evidence["seq"])
                    _durable_reference._refuse(
                        cause is not None, "stage_record names no failing record"
                    )
                if cause is not None and cause[0] == "probe_setup":
                    # A ready check whose probe mediator never started: the
                    # probe setup is still pending for the next reflection.
                    _durable_reference._refuse(
                        any(
                            state.probe_check is not None
                            and state.probe_check["seq"] == evidence["seq"]
                            and state.probe_check["status"] == "ready"
                            and state.probe_check["reflection_id"]
                            == f"r{number:02d}-x{len(state.reflections) + 1:02d}"
                            for number, state in self._rounds.items()
                        ),
                        "probe setup is not pending for the next reflection",
                    )
                    cause = ("probe_unavailable", cause[1])
                if cause is not None:
                    _durable_reference._refuse(
                        cause[0] == p["reason"], "stage_record names a record of another kind"
                    )
                    _durable_reference._refuse(
                        cause[1] == evidence["operation_id"],
                        "stage_record names another operation",
                    )
        # Family 6's precedence, kept: an unavailable probe check is resolved
        # by its own cause, as a recorded threshold is by its crossing above.
        _durable_reference._refuse(
            not unavailable or p["reason"] == "probe_unavailable",
            "an unavailable probe check is resolved by its own suspension",
        )
        entry = dict(
            seq=seq,
            reason=p["reason"],
            owner=p["owner"],
            evidence_kind=kind,
            evidence=dict(evidence),
            # Internal: the producing version decides whether this threshold
            # suspension names an open Attempt's pairs (#91 spec §3.2) and,
            # from a27, whether a suspension of any kind does and counts in
            # `_granting_suspensions` (a27 spec §3.1, #116).
            version=self.producing_version,
        )

        def apply() -> None:
            self.threshold = None
            self._unresponsive = False
            self._reflector_unresponsive = False
            for number, state in list(self._rounds.items()):
                grouping = state.grouping
                if (
                    grouping is not None
                    and grouping.get("status") == "failed"
                    and grouping.get("code") != "interrupted"
                ):
                    state = replace(state, grouping=None)
                if state.probe_check is not None and state.probe_check["status"] == "unavailable":
                    state = replace(state, probe_check=None)
                    self._probe_check_locators.pop(number, None)
                if any(self._suspends(entry) for entry in state.reflections):
                    state = replace(
                        state,
                        reflections=tuple(
                            {**entry, "suspended": True} if self._suspends(entry) else entry
                            for entry in state.reflections
                        ),
                    )
                state = replace(
                    state,
                    attempts={
                        attempt_id: replace(attempt, editor_suspended=True)
                        if attempt.outcome is None
                        and attempt.editor_reason is not None
                        and editor_replayable(attempt, adaptive=True, resilient=True)
                        else attempt
                        for attempt_id, attempt in state.attempts.items()
                    },
                )
                self._rounds[number] = state
            self._window_base_counts = dict(self._counts)
            self._window_base_denominator = self.denominator
            self._owner_windows.clear()
            for window in self._threshold_windows.values():
                window.reset(seq)
            self._last_suspension_seq = seq
            self._repair_owed = repair
            self._suspension_causes.clear()
            self._grouper_chain = 0  # Family 14 (H8, plan decision 6): a suspension breaks it.
            self._suspensions.append(entry)
            # H10 (choice 17): the preamble admitted only the owed suspension.
            self._connection_owed = None
            if cohort_listing is not None:
                # Report state only: the latest listing replaces the round's
                # earlier one. It changes no grant (`_cohort_listed` below).
                listing_round, rows = cohort_listing
                self._put_round(replace(self._rounds[listing_round], cohort_listed=rows))
            if listed or champion_listed:
                retries = self.policy.sample_retries or 0
            for pair, ordinal in champion_listed:
                # #194 G2 (plan choice 4): one grant per ordinal, the earliest
                # naming standing; a listing at a higher ordinal replaces it.
                held = self._champion_listed.get(pair)
                if held is None or held[0] < ordinal:
                    self._champion_listed[pair] = (ordinal, seq)
                # G3 (plan choice 6): a stop never costs a Champion pair its
                # retry budget. Every gap is accounted before it is listed, so
                # a listed ordinal with no accounted try there names a
                # cancelled try; one that is the latest accounted try and
                # denied its retry names a declined one. Either is restored
                # below the limit unless it is itself a repair, so its
                # successor is a D1 retry.
                latest = self._latest_try.get(pair)
                unaccounted = latest is None or latest[0] < ordinal
                declined = latest is not None and latest[0] == ordinal and not latest[2]
                if (
                    (unaccounted or declined)
                    and ordinal < retries
                    and not self._repaired(pair, ordinal)
                ):
                    self._restored.add((*pair, ordinal))
            for pair, ordinal in listed:
                # Pre-F8 spec §2.3: one grant per ordinal, the earliest naming
                # standing; a listing at a higher ordinal replaces a spent one.
                held = self._cohort_listed.get(pair)
                if held is None or held[0] < ordinal:
                    self._cohort_listed[pair] = (ordinal, seq)
                # Spec §2.4: a declined D1 try below the limit is restored, so
                # its successor is a D1 retry, not a repair.
                latest = self._latest_try.get(pair)
                if (
                    latest is not None
                    and latest[0] == ordinal
                    and not latest[2]
                    and ordinal < retries
                    and not self._repaired(pair, ordinal)
                ):
                    self._restored.add((*pair, ordinal))

        return apply

    def _champion_listing(
        self, p: Mapping[str, Any], evidence: Mapping[str, Any]
    ) -> list[tuple[tuple[str, str, str], int]]:
        """Families 13-14 (#194 spec §3.3): admit a `champion_incomplete` listing.

        G6 (plan choice 9): nothing is listed while the run owes or holds
        another decision -- an owed terminal, an open I2 block, a durable
        cancelled role terminal, an open or owed connection preflight (H10),
        or an outstanding suspension cause (H8's Grouper among them); a
        pending threshold is refused before this branch. A `probe_setup`
        entry is not outstanding here: every ready probe check registers
        one, and it is a cause only while its round's next reflection is
        pending, which never holds while that round's baseline is open.

        Admission (plan choice 11): the named measurement is a Champion
        measurement (`_champion_measurement`), not complete, not frozen,
        bound in the current epoch, and the baseline of its own open round --
        the seed baseline, a fresh `rebaseline` baseline or (family 14) a
        refresh, which is that round's baseline until `baseline_complete`, so
        an open refresh is never a completed one. Families 13-14 have no
        winner completion (`completion_bound` is refused). The barrier is the
        baseline window (`_check_barrier_window`). Every row naming the
        measurement is the payload's rule (`_validate_champion_rows`). When
        not truncated, the owner arbitrates the rows; each ordinal is checked
        as a `cohort_incomplete` row's is. Returns the `(pair, ordinal)`
        grants.
        """

        _durable_reference._refuse(
            self._owed_terminal is None
            and self._pending_change is None
            and self._cancelled_role_seq is None
            and not any(reason != "probe_setup" for reason, _ in self._suspension_causes.values())
            and (self._connection_open is None and self._connection_owed is None),
            "a champion listing waits for every owed record",
        )
        _durable_reference._refuse(
            champion_listing_required(self.producing_version),
            "champion_incomplete needs producing version 0.3.0a35 or later",
        )
        measurement_id = str(evidence["measurement_id"])
        entry = _need(self._measurements.get(measurement_id), "the listed measurement is not bound")
        _durable_reference._refuse(
            self._champion_measurement(entry),
            "champion_incomplete names a Champion measurement",
        )
        _durable_reference._refuse(
            not entry.get("frozen", False), "a frozen measurement is never appended to"
        )
        _durable_reference._refuse(
            not entry["complete"], "the Champion measurement is already complete"
        )
        _durable_reference._refuse(
            int(entry["epoch"]) == len(self._epochs) - 1,
            "the Champion measurement is bound in an earlier epoch",
        )
        state = self._rounds[int(entry["round"])]
        _durable_reference._refuse(
            state.closed is None
            and state.baseline_measurement_id == measurement_id
            and not state.baseline_complete,
            "champion_incomplete names its open round's baseline",
        )
        self._check_barrier_window(state, "baseline_stage")
        if not evidence["truncated"]:
            # `OWNERS` is `arbitrate_owner`'s order, as for `cohort_incomplete`.
            owners = {str(row["owner"]) for row in evidence["rows"]}
            _durable_reference._refuse(
                p["owner"] == next(owner for owner in OWNERS if owner in owners),
                "the suspension's owner arbitrates its rows",
            )
        # Plan choice 6, as for `cohort_incomplete` (pre-F8 plan choice 11): S
        # cannot see a cancelled try, so it checks what it can of each listed
        # ordinal; the exact value is the audit's.
        retries = self.policy.sample_retries or 0
        limit = ordinal_limit(retries, self._granting_suspensions())
        granted: list[tuple[tuple[str, str, str], int]] = []
        for row in evidence["rows"]:
            pair = (measurement_id, str(row["case_id"]), str(row["sample_id"]))
            ordinal = int(row["retry_ordinal"])
            latest = self._latest_try.get(pair)
            if latest is not None:
                _durable_reference._refuse(
                    ordinal >= latest[0],
                    "a champion row's retry_ordinal is not before its pair's accounted try",
                )
                _durable_reference._refuse(
                    ordinal != latest[0] or not latest[2],
                    "a champion row names an accounted try that reserved its retry",
                )
            _durable_reference._refuse(
                ordinal <= limit, "a champion row's retry_ordinal exceeds the ordinal limit"
            )
            granted.append((pair, ordinal))
        return granted

    def _measuring_attempt(self, p: dict[str, Any]) -> AttemptState:
        """The common head of both look rows: an open, measuring attempt whose
        measurement already opened the stage the look closes (spec §6)."""

        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(attempt.outcome is None, "the attempt is closed")
        _durable_reference._refuse(
            attempt.measurement_id is not None, "the attempt is not measuring"
        )
        look = p["look"]
        _durable_reference._refuse(
            look in self.policy.looks, "the look is not declared by the policy"
        )
        measurement = self._measurements[str(attempt.measurement_id)]
        _durable_reference._refuse(
            not measurement.get("frozen", False), "a frozen measurement is never appended to"
        )
        _durable_reference._refuse(
            any(stage["look"] == look for stage in measurement["stages"]),
            "the look's stage is not opened",
        )
        if attempt.parent_measurement_id is not None:
            # G2 spec §4.6: a look is evaluable only when both measurements
            # have completed its stage, so the fresh Parent opened it too.
            parent = _need(
                self._measurements.get(attempt.parent_measurement_id),
                "the fresh Parent is not bound",
            )
            _durable_reference._refuse(
                not parent.get("frozen", False)
                and any(stage["look"] == look for stage in parent["stages"]),
                "the fresh Parent has not opened the look's stage",
            )
        return attempt

    def _look_not_started(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        attempt = self._measuring_attempt(p)
        look = p["look"]
        _durable_reference._refuse(look not in attempt.looks, "the look is already evaluated")
        _durable_reference._refuse(
            look not in attempt.not_started, "the look is already recorded as not started"
        )
        self._check_not_held(attempt)

        def apply() -> None:
            current = self._attempt(p["attempt_id"])
            self._put_attempt(replace(current, not_started=(*current.not_started, look)))

        return apply

    def _capacity_exhausted(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        # Not terminal: §7 lets a resumed campaign continue once the operator
        # restored capacity, so a later record — including `campaign_finished` —
        # may follow, and a second shortfall may be recorded.
        _durable_reference._refuse(
            p["measurement_id"] is None or p["measurement_id"] in self._measurements,
            "capacity names an unbound measurement",
        )
        entry = dict(p)

        def apply() -> None:
            self.capacity = entry

        return apply

    def _confidence_close(self, attempt: AttemptState, p: Mapping[str, Any]) -> str | None:
        """Design §6.6: a family-13 close, by kind; returns its rejection layer.

        A deciding look's close repeats that look's outcome and reason and
        names it. A bind-time close (G6, D3, R1) follows the comparison's
        retained cases before any stage. `insufficient_calibration` follows a
        closing statistical calibration (D4). Every other close names no look,
        and the shared checks after this branch keep their family-12 meaning.
        Once the Attempt's state owes a close (`owed_close`, PDR-0022), that
        close is the only one, but for an abandonment wherever it stands.
        """

        outcome, reason, look = p["outcome"], p["reason"], p["look"]
        layer = None
        closes = frozenset(
            (decision, reason)
            for decision in ("finalist", "rejected", "insufficient_evidence")
            for reason in ATTEMPT_DECISIONS14[decision]
            if reason
            not in ("insufficient_calibration", "no_evaluable_focus", "no_evaluable_pairs")
        )
        if (outcome, reason) in closes:
            entry = _need(attempt.looks.get(look), "a look-decided close names an evaluated look")
            _durable_reference._refuse(
                (entry["decision"], entry["reason"]) == (outcome, reason),
                "the close does not follow its look's decision",
            )
            layer = REJECTION_LAYERS.get(reason)
        else:
            _durable_reference._refuse(look is None, "only a look-decided outcome names a look")
        if reason in ("no_evaluable_focus", "no_evaluable_pairs"):
            comparison = _need(attempt.comparison, "a bind-time close follows comparison_bound")
            close = binding_close(comparison)
            _durable_reference._refuse(
                close is not None and close.reason == reason,
                "the bind-time close does not follow the retained cases",
            )
            _durable_reference._refuse(
                _no_stage_opened(attempt, self._measurements),
                "a bind-time close precedes every stage",
            )
        elif reason == "insufficient_calibration":
            comparison = _need(attempt.comparison, "insufficient_calibration needs a comparison")
            verdict = calibration_verdict(
                comparison, attempt.calibration_result, family_version=self._family_version
            )
            _durable_reference._refuse(
                verdict is not None and verdict.close is not None,
                "insufficient_calibration needs a closing calibration",
            )
        owed = owed_close(attempt, family_version=self._family_version)
        _durable_reference._refuse(
            owed is None
            or reason in _ANYWHERE_ABANDONS
            or (outcome, reason, look) == (owed[0].decision, owed[0].reason, owed[1]),
            "the close is not the one the Attempt owes",
        )
        return layer

    def _attempt_closed(
        self, p: dict[str, Any], op: str | None, seq: int, record: Envelope
    ) -> Callable[[], None]:
        attempt = self._attempt(p["attempt_id"])
        _durable_reference._refuse(attempt.outcome is None, "the attempt is already closed")
        state = self._rounds[attempt.round]
        _durable_reference._refuse(state.selection is None, "the round is already decided")
        outcome, reason, look = p["outcome"], p["reason"], p["look"]
        if reason == "condition_changed":
            # I2 spec §3.2, §5.5: only a `rebaseline` block over the open round
            # closes its Attempts this way, each without an outcome in order.
            block = self._pending_change
            _durable_reference._refuse(
                block is not None and block["phase"] == "close_round",
                "condition_changed closes an attempt only inside a rebaseline block",
            )
            _durable_reference._refuse(
                attempt.round == self._open_round(),
                "condition_changed closes an attempt of the open round",
            )
            _durable_reference._refuse(
                self.snapshot.open_attempts(attempt.round)[0].attempt_id == attempt.attempt_id,
                "condition_changed closes attempts in attempt order",
            )
        rejection_layer: str | None = None
        _durable_reference._refuse(
            self._all_looks_charged(attempt),
            "attempt closes only after all evaluated alpha is consumed",
        )
        if self._confidence:
            rejection_layer = self._confidence_close(attempt, p)
        else:
            # Family 15 delegates its no-path closes here, without a shared
            # confidence binding. They retain the inherited no-look rule.
            _durable_reference._refuse(look is None, "only a look-decided outcome names a look")
        if (
            (outcome == "insufficient_evidence")
            and (reason == "infrastructure_failed")
            and (attempt_suspension_required(self.producing_version))
        ):
            # #97 spec §4.2, §4.4 (ADR-0019 as amended 2026-10-03): from
            # 0.3.0a26 a gate input is valid unless it was written before the
            # fix -- in a measurement completed under an exempt version, or
            # carried in under one; only that closes `infrastructure_failed`.
            # Which row it was is the audit's.
            baseline = self._rounds[attempt.round].baseline_measurement_id
            sides = (baseline, attempt.measurement_id, attempt.parent_measurement_id)
            _durable_reference._refuse(
                any(
                    written_before_fix(self._completion_versions.get(str(measurement)))
                    or written_before_fix(self._import_versions.get(str(measurement)))
                    for measurement in sides
                    if measurement is not None
                ),
                "an infrastructure_failed closure needs pre-fix evidence",
            )
        if (
            outcome == "insufficient_evidence"
            and reason == "incomplete_sample"
            and not latest_look_barred(attempt, self._measurements)
        ):
            # A barred look was checked (or exempt) when its bar was written.
            self._check_not_held(attempt)
        if outcome == "abandoned":
            # `cancelled` joins `interrupted` as a reason that needs no editor
            # terminal: the §7 barrier abandons an attempt wherever it stands
            # once the caller cancelled, including an editor that finished in
            # the same poll window the token was set in.
            # Family 7's `condition_changed` abandons an Attempt wherever it
            # stands, as a barrier's cancellation does.
            _durable_reference._refuse(
                reason in _ANYWHERE_ABANDONS or attempt.editor in ("edited", "declined", "failed"),
                "abandoned follows an editor terminal, or is interrupted or cancelled",
            )
            expected = _ABANDON_EDITOR.get(reason)
            _durable_reference._refuse(
                expected is None or attempt.editor == expected,
                "the abandonment reason does not match the editor terminal",
            )
            if reason == "editor_unresponsive":
                _durable_reference._refuse(
                    attempt.editor_error == "unresponsive",
                    "editor_unresponsive needs an unresponsive editor terminal",
                )
            if reason == "editor_failed":
                # §11: the unresponsive class has its own abandonment reason.
                _durable_reference._refuse(
                    attempt.editor_error != "unresponsive",
                    "an unresponsive editor abandons as editor_unresponsive",
                )
                # Family 10 (spec §5.3): a replayable Editor record re-runs;
                # only a later terminal can close its Attempt. Earlier
                # families' journals are read exactly as before.
                _durable_reference._refuse(
                    not (editor_replayable(attempt, adaptive=True, resilient=True)),
                    "a replayable editor failure does not close its attempt",
                )
            if reason == "seal_refused":
                _durable_reference._refuse(
                    attempt.candidate_digest is None, "a sealed attempt was not refused"
                )
            if reason == "no_exam_impact":
                # G2 spec §4.4: an empty layer 1 closes the Attempt before
                # any measurement, so no alpha is charged.
                selection = _need(attempt.selection, "no_exam_impact needs a selection")
                _durable_reference._refuse(
                    attempt.measurement_id is None,
                    "no_exam_impact closes an attempt before its measurement",
                )
                scope = _need(state.gate_scope, "no_exam_impact needs the round's gate scope")
                _durable_reference._refuse(
                    not affected_layer_one(
                        selection, self.identity["corpus_cases"], scope["cases"]
                    ),
                    "no_exam_impact needs an empty layer 1",
                )

        def apply() -> None:
            current = self._attempt(p["attempt_id"])
            looks = dict(current.looks)
            if rejection_layer is not None:
                assert look is not None
                looks[look] = {**looks[look], "rejection_layer": rejection_layer}
            self._put_attempt(
                replace(
                    current,
                    looks=looks,
                    outcome=outcome,
                    reason=reason,
                    look=look,
                    code=p["code"],
                    closed_seq=seq,
                    closed_record_digest=hashlib.sha256(record.encode()).hexdigest(),
                    closed_record_locator=(
                        f"campaign.jsonl#bytes={self._valid_bytes}:"
                        f"{self._valid_bytes + len(record.encode())}"
                    ),
                )
            )

        return apply

    def _selection_decided(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(state.baseline_complete, "the round's baseline is not complete")
        _durable_reference._refuse(state.selection is None, "the round is already decided")
        _durable_reference._refuse(state.closed is None, "the round is already closed")
        if self.policy.round_stop == "first_accepted":
            latest = next(reversed(tuple(state.attempts.values())), None)
            accepted = [
                name
                for name, comparison in state.global_comparisons.items()
                if comparison["accepted"]
            ]
            _durable_reference._refuse(
                not accepted or (latest is not None and accepted == [latest.attempt_id]),
                "only the last opened Attempt can win first_accepted",
            )
            _durable_reference._refuse(
                len(state.attempts) == self.policy.attempts_per_round or bool(accepted),
                "first_accepted selection requires a winner or K Attempts",
            )
        else:
            _durable_reference._refuse(
                len(state.attempts) == self.policy.attempts_per_round,
                "the round did not open every declared attempt",
            )
        _durable_reference._refuse(
            all(a.outcome is not None for a in state.attempts.values()),
            "the round still has open attempts",
        )
        self._check_cohort_selection(p, state)
        if p["attempt_id"] is not None:
            winner = self._attempt(p["attempt_id"])
            _durable_reference._refuse(
                winner.round == number, "the winner belongs to another round"
            )
            _durable_reference._refuse(
                winner.outcome == campaign_vocabulary_for(14).gate_pass_outcome,
                "the winner is not an accepted attempt",
            )
        entry = dict(p)
        entry.pop("evidence", None)

        def apply() -> None:
            self._put_round(replace(self._rounds[number], selection=entry))

        return apply

    def _check_cohort_selection(self, p: dict[str, Any], state: RoundState) -> None:
        """Design §6.11, D5 (plan choice 2): the family-13 selection is
        re-derived from the round itself, with or without an injected
        selector, so a replay with `selector=None` checks it too."""

        cohort = state.cohort
        if cohort is None:
            _durable_reference._refuse(
                not any(
                    a.outcome == campaign_vocabulary_for(14).gate_pass_outcome
                    for a in state.attempts.values()
                ),
                self._member_rule("a round with finalists selects over its cohort"),
            )
        else:
            finalists = selection_finalists(state)
            _durable_reference._refuse(
                {str(f["attempt_id"]) for f, _ in finalists}
                == {
                    a.attempt_id
                    for a in state.attempts.values()
                    if a.outcome == campaign_vocabulary_for(14).gate_pass_outcome
                },
                "the selection includes every finalist's binding",
            )
            bindings = state.finals or (cohort,)
            members = [
                mid
                for binding in bindings
                for mid in (
                    current_members(state, final=binding)
                    if state.finals
                    else current_members(state)
                )
            ]
            _durable_reference._refuse(
                all(mid in state.cohort_completed for mid in members),
                "the selection follows every member's completion",
            )
            if p["measurement_id"] is not None:
                self._check_current(state, [p["measurement_id"]])
            _durable_reference._refuse(
                all(str(f["attempt_id"]) in state.global_comparisons for f, _ in finalists),
                "the selection follows every finalist's comparison",
            )
            sources = selection_records(state)
            _durable_reference._refuse(
                len(p["evidence"]) == len(sources)
                and all(ref["kind"] == "journal_record" for ref in p["evidence"]),
                "the selection cites every completion and every comparison",
            )
            for reference, source in zip(p["evidence"], sources, strict=True):
                _durable_reference._refuse(
                    reference["run_id"] == self.run_id
                    and reference["locator"] == source["record_locator"]
                    and reference["content_digest"] == source["record_digest"],
                    "the selection cites every completion and every comparison in order",
                )
        global_mode = _need(self.policy.global_mode, "a family-13 policy has a global mode")
        try:
            expected = cohort_selection(state, global_mode, family_version=(14))
        except ValueError:
            _durable_reference._refuse(
                False, self._member_rule("the cohort's verdicts admit no selection")
            )
            raise  # unreachable: `_refuse(False, ...)` raises
        _durable_reference._refuse(
            p["global"] == expected["global"], "the selection names another global mode"
        )
        _durable_reference._refuse(
            (p["attempt_id"], p["candidate_digest"], p["dependency_digest"], p["measurement_id"])
            == (
                expected["attempt_id"],
                expected["candidate_digest"],
                expected["dependency_digest"],
                expected["measurement_id"],
            ),
            "the selection names another winner",
        )
        _durable_reference._refuse(
            p["reason"] == expected["reason"], "the selection gives another reason"
        )
        _durable_reference._refuse(
            [dict(row) for row in p["ranking"]] == expected["ranking"],
            "the selection carries another ranking",
        )

    def _round_closed(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        number = p["round"]
        state = self._round(number, "the round does not exist")
        _durable_reference._refuse(state.closed is None, "the round is already closed")
        if state.baseline_complete and p["outcome"] != "condition_changed":
            # A `condition_changed` close leaves an open refresh uncompleted
            # (plan decision 7); it is the only family-14 close of a round
            # whose baseline never completed.
            self._check_refresh_completed(state)
        _durable_reference._refuse(
            p["attempts_opened"] == len(state.attempts),
            "attempts_opened matches the round's opened Attempts",
        )
        version, digest = p["champion_version"], p["champion_digest"]
        outcome = p["outcome"]
        condition_changed = outcome == "condition_changed"
        if condition_changed:
            self._check_condition_changed_close(p, state)
        coverage = p["coverage"]
        early_global = (
            self._independent
            and bool(state.attempts)
            and all(
                a.family15 is not None and a.family15.path == "early_global"
                for a in state.attempts.values()
            )
        )
        if (condition_changed or early_global) and state.focus_portion is None:
            # A `rebaseline` may close a round before its focus portion:
            # nothing was selected, and the cycle is the round's first.
            visits: list[Any] = []
            cycle = 1
        else:
            portion = _need(state.focus_portion, "an adaptive round has no focus portion")
            visits, cycle = portion["cohort_ids"], portion["cycle"]
        _durable_reference._refuse(
            coverage["selected_cohort_visits"] == visits,
            "coverage visits differ from the selected portion",
        )
        _durable_reference._refuse(coverage["cycle"] == cycle, "coverage cycle differs from focus")
        diagnosis = state.diagnosis(True)
        claimed = (
            set().union(*(set(group["affected_cases"]) for group in diagnosis["groups"]))
            if diagnosis is not None
            and diagnosis["groups"]
            # Family 9: only a valid Editor input claims anything; a
            # decline claims nothing whatever it carried (spec §14).
            and not (editor_input_reason(diagnosis) is not None)
            else set()
        )
        _durable_reference._refuse(
            coverage["claimed_cases"] == len(claimed),
            "coverage claimed cases differ from diagnosis",
        )
        exam = () if state.gate_scope is None else state.gate_scope["cases"]
        _durable_reference._refuse(
            coverage["examined_cases"] == len(exam),
            "coverage examined cases differ from gate scope",
        )
        assessed = any(
            attempt.looks
            or (
                self._independent
                and attempt.family15 is not None
                and attempt.family15.comparison is not None
            )
            for attempt in state.attempts.values()
        )
        _durable_reference._refuse(
            coverage["had_assessed_candidate"] == assessed,
            "coverage assessed candidate differs from attempts",
        )
        looks = sorted(
            (look for attempt in state.attempts.values() for look in attempt.looks.values()),
            key=lambda look: look["seq"],
        )
        if self._independent:
            looks = sorted(
                (
                    dict(
                        seq=a.family15.comparison.envelope.to_payload()["sequence"],
                        record_digest=a.family15.comparison.reference.content_digest,
                    )
                    for a in state.attempts.values()
                    if a.family15 is not None and a.family15.comparison is not None
                ),
                key=lambda entry: entry["seq"],
            )
        expected_refs = looks[-8:]
        _durable_reference._refuse(
            coverage["omitted_closure_refs"] == max(0, len(looks) - 8),
            "closure reference omissions differ from looks",
        )
        _durable_reference._refuse(
            len(coverage["closure_refs"]) == len(expected_refs), "closure reference count"
        )
        for source, look in zip(coverage["closure_refs"], expected_refs, strict=True):
            if self._family15 is not None:
                self._family15.resolve(source, "comparison_completed")
            ref = _durable_reference.DurableReference.from_payload(source)
            _durable_reference._refuse(
                ref.kind == "journal_record"
                and ref.run_id == self.run_id
                and ref.locator.startswith("campaign.jsonl#bytes=")
                and ref.content_digest == look["record_digest"],
                "closure reference does not name an admitted look",
            )
        if condition_changed:
            closed_entry = dict(p)
            closed_entry.pop("evidence", None)

            def apply_condition_changed() -> None:
                self._put_round(replace(self._rounds[number], closed=closed_entry))
                self._pending_change = None
                self._rebaseline_from_round = number + 1

            return apply_condition_changed
        if outcome == "unplanned":
            return self._unplanned_close(p, state)
        if outcome == "unscored":
            _durable_reference._refuse(
                portion["reason"] == "selected", "only a focused round is unscored"
            )
            _durable_reference._refuse(
                state.diagnosis(True) is not None,
                "a focused round has no completed reflection",
            )
            _durable_reference._refuse(
                state.gate_scope is not None, "a focused round has no gate scope"
            )
            _durable_reference._refuse(
                not any(entry["status"] == "started" for entry in state.reflections),
                "a focused round still has an open reflection",
            )
            selection = _need(state.selection, "an unscored round has no completed selection")
            _durable_reference._refuse(
                not any(
                    attempt.looks
                    or (
                        self._independent
                        and attempt.family15 is not None
                        and attempt.family15.comparison is not None
                    )
                    for attempt in state.attempts.values()
                ),
                "an assessed candidate cannot be unscored",
            )
            _durable_reference._refuse(
                selection["attempt_id"] is None,
                "an unscored round has no winner",
            )
            _durable_reference._refuse(
                (version, digest) == self._champion,
                "an unscored round keeps the current champion",
            )
            _durable_reference._refuse(p["evidence"] is None, "an unscored round commits nothing")
            entry = dict(p)
            entry.pop("evidence", None)

            def apply_unscored() -> None:
                self._put_round(replace(self._rounds[number], closed=entry))

            return apply_unscored
        if not state.attempts:
            # A focus-less (or spent-focus) round closes directly, with no
            # selection at all: `clean` when there was nothing left to chase,
            # `stalled` when the focus named something still unavailable.
            focus = _need(state.focus_portion, "the round has no focus portion")
            _durable_reference._refuse(
                not state.reflections, "an empty round records no reflection"
            )
            _durable_reference._refuse(
                focus["reason"] in ("none", "unmeasurable"),
                "an empty round has no focus cluster",
            )
            _durable_reference._refuse(
                outcome in ("clean", "stalled", "unmeasurable"), "an empty round cannot promote"
            )
            _durable_reference._refuse(
                (version, digest) == self._champion, "an empty round keeps the current champion"
            )
            _durable_reference._refuse(
                p["evidence"] is not None, "an empty round's outcome carries its own evidence"
            )
            # Family 6 (spec §4.3): the round whose failing cohorts the excluded
            # baseline pairs emptied closes on its own outcome, and only that
            # focus state closes that way.
            _durable_reference._refuse(
                (outcome == "unmeasurable") == (focus["reason"] == "unmeasurable"),
                "an unmeasurable round is exactly one whose focus found nothing measurable",
            )
            if outcome == "clean":
                _durable_reference._refuse(
                    focus["unavailable"] == 0, "a clean round has nothing left unavailable"
                )
            elif outcome == "stalled":
                _durable_reference._refuse(
                    focus["unavailable"] > 0, "a stalled empty round has something unavailable"
                )
            empty_entry = dict(p)
            empty_entry.pop("evidence", None)

            def apply_empty() -> None:
                self._put_round(replace(self._rounds[number], closed=empty_entry))

            return apply_empty
        _durable_reference._refuse(outcome != "clean", "a clean round has no attempts")
        selection = _need(state.selection, "the round has no selection")
        if self._confidence:
            # Design §6.12: a family-13 round promotes or retains exactly when
            # it bound a cohort; without one it stalls (or is unscored, above).
            _durable_reference._refuse(
                (outcome in ("promoted", "retained")) == (state.cohort is not None),
                self._member_rule(
                    "a family-13 round closes promoted or retained exactly when it bound a cohort"
                ),
            )
        if outcome == "retained":
            _durable_reference._refuse(
                selection["attempt_id"] is None, "a retained round selects no finalist"
            )
            _durable_reference._refuse(
                (version, digest) == self._champion, "a retained round keeps the current champion"
            )
            _durable_reference._refuse(p["evidence"] is None, "a retained round commits nothing")
        elif outcome == "stalled":
            if portion["reason"] == "selected":
                _durable_reference._refuse(
                    any(attempt.looks for attempt in state.attempts.values()),
                    "a focused unassessed round is unscored",
                )
            _durable_reference._refuse(
                selection["attempt_id"] is None, "a selected winner cannot stall"
            )
            _durable_reference._refuse(
                (version, digest) == self._champion, "a stalled round keeps the current champion"
            )
            _durable_reference._refuse(p["evidence"] is None, "a stalled round commits nothing")
        else:
            _durable_reference._refuse(
                selection["attempt_id"] is not None, "a promotion needs a selected winner"
            )
            _durable_reference._refuse(
                digest == selection["candidate_digest"], "the promotion names another candidate"
            )
            _durable_reference._refuse(
                version not in self._versions, "the champion version is already used"
            )
            _durable_reference._refuse(
                p["evidence"] is not None, "a promotion carries its commit record"
            )
        entry = dict(p)
        entry.pop("evidence", None)
        promoted = outcome == "promoted"
        dependency = selection["dependency_digest"]

        def apply() -> None:
            self._put_round(replace(self._rounds[number], closed=entry))
            if promoted:
                self._champion = (version, digest)
                self._champion_dependency = dependency
                self._versions.add(version)

        return apply

    def _unplanned_close(self, p: dict[str, Any], state: RoundState) -> Callable[[], None]:
        """Family 9 (spec §14, ADR-0022): a settled reflection that is no Editor input.

        The round selected a focus, its reflection settled, and the reason is
        the one `editor_input_reason` derives from that reflection. Nothing
        was dispatched: no gate scope, no Attempt, no selection, and the
        Champion is unchanged. The evidence names the portion and the
        reflection terminal (the audit reads both).
        """

        portion = _need(state.focus_portion, "an unplanned round has no focus portion")
        _durable_reference._refuse(
            portion["reason"] == "selected", "only a focused round closes unplanned"
        )
        _durable_reference._refuse(
            not any(entry["status"] == "started" for entry in state.reflections),
            "an unplanned round still has an open reflection",
        )
        diagnosis = _need(state.diagnosis(True), "an unplanned round has no settled reflection")
        # PDR-0015: an unresponsive reflection suspends; it never closes a round.
        _durable_reference._refuse(
            diagnosis["error_class"] != "unresponsive",
            "an unresponsive reflection does not close a round",
        )
        _durable_reference._refuse(
            editor_input_reason(diagnosis) == p["reason"],
            "the unplanned reason is not the settled reflection's",
        )
        _durable_reference._refuse(state.gate_scope is None, "an unplanned round has no gate scope")
        _durable_reference._refuse(not state.attempts, "an unplanned round opens no attempt")
        _durable_reference._refuse(state.selection is None, "an unplanned round selects nothing")
        _durable_reference._refuse(
            (p["champion_version"], p["champion_digest"]) == self._champion,
            "an unplanned round keeps the current champion",
        )
        _durable_reference._refuse(
            p["evidence"] is not None and len(p["evidence"]) == 2,
            "an unplanned round cites its portion and its reflection",
        )
        entry = dict(p)
        entry.pop("evidence", None)
        number = p["round"]
        cause: tuple[int, str | None] | None = None
        cause = self._resilient_unplanned_close(p, number, diagnosis)

        def apply() -> None:
            self._put_round(replace(self._rounds[number], closed=entry))
            if cause is not None:
                # The second skip in a row (spec §4.2): its suspension names
                # the round's diagnosis, a record already behind this close.
                self._suspension_causes[cause[0]] = ("reflector_failing", cause[1])

        return apply

    def _resilient_unplanned_close(
        self, p: Mapping[str, Any], number: int, diagnosis: Mapping[str, Any]
    ) -> tuple[int, str | None] | None:
        """Family 10 (spec §4.2-§4.3): only a content failure closes a round
        `reflection_failed`; `visitation: "skipped"` is present exactly when
        `skip_due`; a skip in a row with the previous one owes a
        `reflector_failing` suspension naming this round's diagnosis, whose
        `(seq, operation_id)` is returned."""

        if p["reason"] == "reflection_failed":
            # `diagnosis()` never returns a `provider`/`unknown` failure, and a
            # lifecycle terminal is no diagnosis a family-10 round closes on.
            _durable_reference._refuse(
                reflection_entry_class(diagnosis) == "content",
                "only a content failure closes a round",
            )
        threshold = _need(self._reflection_threshold, "family 10 has no failure threshold")
        snapshot = self.snapshot
        due = skip_due(snapshot, number, threshold)
        _durable_reference._refuse(
            ("visitation" in p) == due, "visitation does not match the skip rule"
        )
        if due:
            # Admission is pure: evaluate the proposed close with the same
            # helper the driver and audit use after the close is durable.
            closed = replace(snapshot.rounds[number], closed=dict(p))
            proposed = replace(snapshot, rounds={**snapshot.rounds, number: closed})
            if skip_chain_length(proposed, number) >= self._skip_chain_limit:
                return (int(diagnosis["seq"]), diagnosis["operation_id"])
        return None

    def _check_condition_changed_close(self, p: dict[str, Any], state: RoundState) -> None:
        """I2 spec §3.2, §5.5: `round_closed{condition_changed}` ends a `rebaseline` block.

        Admitted only as the block's last record, for the open round, once
        every Attempt of it has an outcome; the round selects nothing and
        promotes nothing.
        """

        block = self._pending_change
        _durable_reference._refuse(
            block is not None and block["phase"] == "close_round",
            "a round closes condition_changed only inside a rebaseline block",
        )
        _durable_reference._refuse(
            p["round"] == self._open_round(), "condition_changed closes the open round"
        )
        _durable_reference._refuse(
            all(attempt.outcome is not None for attempt in state.attempts.values()),
            "a condition_changed round closes once every attempt has an outcome",
        )
        _durable_reference._refuse(
            state.selection is None, "a condition_changed round selects nothing"
        )
        _durable_reference._refuse(
            (p["champion_version"], p["champion_digest"]) == self._champion,
            "a condition_changed round keeps the current champion",
        )
        _durable_reference._refuse(
            p["evidence"] is None, "a condition_changed round commits nothing"
        )

    def _measurement_rebound(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        """I2 spec §5.2: `continue` re-binds one unfinished frozen measurement.

        Written only inside a `continue` block, for the current epoch, at most
        once per `(from_measurement_id, epoch)` (the derived id is bound
        once), and only for a measurement the planner would still append to:
        the open round's incomplete baseline, an open Attempt's measurement
        or (family 12) its fresh Parent, or a promoted winner's measurement
        still owed its completion (family 12: the winner's unfinished
        completion, never the sealed winner). That
        binding now names the new measurement, which starts with no stage;
        whether its carried rows match `imported`/`pairs_digest` is an E fact.
        """

        block = self._pending_change
        _durable_reference._refuse(
            block is not None and block["phase"] == "rebind",
            "measurement_rebound follows only its continue block",
        )
        epoch = len(self._epochs) - 1
        _durable_reference._refuse(p["epoch"] == epoch, "measurement_rebound names another epoch")
        source_id = str(p["from_measurement_id"])
        source = _need(self._measurements.get(source_id), "the measurement is not bound")
        _durable_reference._refuse(source.get("frozen", False), "the measurement is not frozen")
        # F7 plan choice 14: a cohort member recorded complete is complete.
        _durable_reference._refuse(
            not self._recorded_complete(source), "the measurement is complete"
        )
        measurement = str(p["measurement_id"])
        _durable_reference._refuse(
            measurement == measurement_id_for_epoch(base_measurement_id(source_id), epoch),
            "the rebound measurement keeps its epoch id",
        )
        _durable_reference._refuse(
            measurement not in self._measurements, "the measurement id is already bound"
        )
        _durable_reference._refuse(
            p["round"] == source["round"], "measurement_rebound names another round"
        )
        target = _need(self._append_target(source_id), "the planner appends to no such measurement")
        # PDR-0016: the
        # source's open stage reserved a slot for every row it would dispatch,
        # and the re-bound stage declares the rows it dispatches again. The
        # slots the source reserved and never started (`released`, an E fact
        # the audit recomputes from the frozen journal) leave the denominator
        # here, so every slot counts once. They are at most what the source's
        # latest stage and its reserved retries declared.
        stages = source["stages"]
        reserved = self._reserved_retries(source_id)
        released = int(p["released"])
        # #78 (spec §3.5): plus the threshold repairs the source reserved in the
        # current window and never started.
        dispatch = self._dispatch.get(source_id)
        window_reserved = (
            dispatch[1]
            if dispatch is not None
            and self._last_suspension_seq is not None
            and dispatch[0] > self._last_suspension_seq
            else 0
        )
        _durable_reference._refuse(
            released
            <= (int(stages[-1]["sample_count"]) if stages else 0) + reserved + window_reserved,
            "released exceeds the slots the source reserved",
        )
        opened_at = self._stage_seq.get(source_id)
        if dispatch is not None and (opened_at is None or dispatch[0] > opened_at):
            # #78 (spec §3.5): an unspent repair reservation leaves the window
            # that holds it. A stage opened before the latest suspension ran to
            # completion before it (ADR-0008) and leaves no unfilled slot --
            # except, in family 12, a stage a drift left open: its unfilled
            # slots stay in the base until its re-dispatch's
            # `stage_redispatched` moves them into the window and its position
            # with them (#109, a28 spec §3.6). The executor writes that record
            # before `repairs_dispatched`, so a window that holds the
            # measurement's reservation also holds the carried stage's slots,
            # and a release positioned at the later of the two records is exact.
            opened_at = dispatch[0]
        # I2 Task 7 (snapshot only, never journaled): an open Attempt whose
        # latest look decided `continue` goes on from the next look number
        # (§5.2), so the executor starts the new measurement's first stage past
        # it and the planner counts only the looks after it. `None` for a
        # baseline, a winner completion, and an Attempt whose looks already
        # decided it -- those measure every look again, their carried rows
        # filling the earlier ones.
        after_look: int | None = None
        # G2 spec §4.6: the fresh Parent goes on from its Candidate's next look.
        if target[0] in ("attempt", "parent"):
            attempt = self._attempt(target[1])
            if attempt.outcome is None and attempt.looks and not attempt.not_started:
                latest = max(attempt.looks)
                if attempt.looks[latest]["decision"] == "continue" and latest < max(
                    self.policy.looks
                ):
                    after_look = latest
        entry: dict[str, Any] = dict(
            measurement_id=measurement,
            attempt_id=source["attempt_id"],
            round=source["round"],
            candidate_digest=source["candidate_digest"],
            identity_digest=p["identity_digest"],
            epoch=epoch,
            stages=(),
            instances=(),
            complete=False,
            frozen=False,
            imported=p["imported"],
            rebound_from=source_id,
            rebound_after_look=after_look,
            # Snapshot only: the declared carry an owed birth is checked against.
            rebound_pairs_digest=p["pairs_digest"],
            # G2 spec §3.6: a family-12 measurement keeps its source's role and
            # case set; families 5-11 keep their entries' keys. A family-13
            # Parent keeps its `case_repeats` too (design §5). A cohort member
            # keeps its role and `cohort_role` and has no case set (design D9).
            **(
                {"role": source["role"], "cohort_role": source["cohort_role"]}
                if target[0] == "member"
                else {"role": source["role"], "case_subset": source.get("case_subset")}
                if "role" in source
                else {}
            ),
            **({"case_repeats": source["case_repeats"]} if "case_repeats" in source else {}),
            **(
                {
                    key: source[key]
                    for key in (
                        "stability",
                        "round_stop",
                        "champion_side",
                        "stored_champion",
                        "stored_reference",
                    )
                    if key in source
                }
                if self._family_version == 14 and target[0] == "member"
                else {}
            ),
        )

        def apply() -> None:
            self._measurements[measurement] = entry
            if int(p["imported"]) > 0:
                # ADR-0019 (amended 2026-10-03): the carry was admitted under
                # the version in force now.
                self._import_versions[measurement] = self.producing_version
            if released:
                self._release_slots(source, opened_at, released)
            kind, name = target
            if kind == "family15":
                assert self._family15 is not None
                self._family15.rebound(source_id, measurement)
            elif kind == "baseline":
                number = int(name)
                self._put_round(replace(self._rounds[number], baseline_measurement_id=measurement))
            elif kind == "parent":
                self._put_attempt(replace(self._attempt(name), parent_measurement_id=measurement))
            elif kind == "completion":
                number = int(name)
                self._put_round(
                    replace(self._rounds[number], completion_measurement_id=measurement)
                )
            elif kind == "member":
                # F7 plan choice 5: the member's current id moves; the bound
                # `cohort["members"]` does not. A new tuple, never a mutation.
                current = self._rounds[int(source["round"])]
                moved = tuple(
                    measurement if member == name else member
                    for member in _need(
                        current.cohort_current, self._member_rule("a cohort has current members")
                    )
                )
                self._put_round(
                    replace(
                        current,
                        cohort_current=moved,
                        finals_current=(
                            (*current.finals_current[:-1], moved)
                            if self._family_version == 14
                            else current.finals_current
                        ),
                    )
                )
            else:
                self._put_attempt(replace(self._attempt(name), measurement_id=measurement))

        return apply

    def _release_slots(self, source: Mapping[str, Any], opened_at: int | None, slots: int) -> None:
        """Take a re-bound source's `released` slots out of every total that counted them.

        The campaign denominator always counted them. A restarted window
        counted them when the source was bound in its epoch or later (the
        rule `_feed_owner_windows` feeds by). The window since the latest
        suspension counted them only when the source's stage was positioned
        after that suspension -- opened, or (#109) carried into it by
        `stage_redispatched`; otherwise they sit in the window's base, which
        moves with the total so the window itself is unchanged.
        """

        self.denominator -= slots
        for window in self._owner_windows.values():
            if int(source["epoch"]) >= window["epoch"]:
                window["denominator"] -= slots
        suspended = self._last_suspension_seq
        if suspended is not None and (opened_at is None or opened_at < suspended):
            self._window_base_denominator -= slots

    def _append_target(self, measurement_id: str) -> tuple[str, str] | None:
        """What the planner would still append to `measurement_id` as (I2 spec §5.2).

        `("baseline", round)` for the open round's incomplete baseline,
        `("parent", attempt_id)` for the fresh Parent of an open-round Attempt
        with no outcome (G2b3a), `("attempt", attempt_id)` for the measurement
        of an open-round Attempt
        with no outcome or an `accepted` one (a candidate winner whose
        completion follows the round's close -- every accepted one before the
        round's `selection_decided`, only the selected one after it, ADR-0020
        as amended 2026-09-24), or of a closed round's promoted winner still
        owed completion -- unless a `rebaseline` replaced that completion with
        a fresh baseline. `None` for any other measurement: a rejected,
        abandoned or insufficient-evidence Attempt, or an accepted one another
        was selected over, is never appended to again (ruling R13).
        In family 12, `("completion", round)` for a promoted round's unfinished
        completion; a sealed winner and an accepted Attempt are never appended
        to (G2 spec §4.7).
        In family 13 (F7, D9), `("member", measurement_id)` for a current
        member of the open round's undecided cohort that has no
        `cohort_measurement_completed`; and a baseline recorded complete
        (`recorded_complete`, plan choice 14: a completed cohort member
        reused as the next baseline) is not appended to.
        The executor's `campaign._rebind_sources` is the same rule.
        """

        if self._family15 is not None:
            target = self._family15.append_target(measurement_id)
            if target is not None:
                return target
        open_round = self._open_round()
        if open_round is not None:
            state = self._rounds[open_round]
            entry = self._measurements.get(measurement_id)
            if (
                state.baseline_measurement_id == measurement_id
                and not state.baseline_complete
                and not (entry is not None and self._recorded_complete(entry))
            ):
                return ("baseline", str(open_round))
            if (
                state.cohort is not None
                and state.selection is None
                and state.cohort_current is not None
                and measurement_id in state.cohort_current
                and measurement_id not in state.cohort_completed
            ):
                return ("member", measurement_id)
            for attempt in state.attempts.values():
                if (attempt.outcome is None) and attempt.measurement_id == measurement_id:
                    return ("attempt", attempt.attempt_id)
                # G2 spec §3.6: an open Attempt's fresh Parent. An accepted
                # Attempt's gate is done, so its Parent is never appended to.
                if attempt.outcome is None and attempt.parent_measurement_id == measurement_id:
                    return ("parent", attempt.attempt_id)
        fresh = self._rebaseline_from_round
        for number, state in self._rounds.items():
            if (
                state.closed is not None
                and state.closed["outcome"] == "promoted"
                and not state.champion_complete
                and state.selection is not None
                and (fresh is None or number >= fresh)
            ):
                if state.completion_measurement_id == measurement_id:
                    return ("completion", str(number))
                continue
                winner = self._attempt(state.selection["attempt_id"])
                if winner.measurement_id == measurement_id:
                    return ("attempt", winner.attempt_id)
        return None

    def _campaign_finished(self, p: dict[str, Any], op: str | None) -> Callable[[], None]:
        self._check_finished(p)
        entry = dict(p)
        entry.pop("evidence", None)
        entry["continuation"] = dict(p["continuation"])

        def apply() -> None:
            self.finished = entry

        return apply

    def _check_finished(self, p: dict[str, Any]) -> None:
        """Spec §5: admitted in exactly the listed states and refused otherwise."""

        outcome, reason = p["outcome"], p["reason"]
        kind = p["continuation"]["kind"]
        if self._owed_terminal is not None:
            _durable_reference._refuse(
                (outcome, reason) == self._owed_terminal,
                "the terminal is not the owed one",
            )
        _durable_reference._refuse(
            outcome != "infrastructure_failed"
            and (outcome, reason) != ("budget_exhausted", "rollout_time_budget_exhausted"),
            "family 7 suspends on an infrastructure cause",
        )
        # The continuation a terminal that opens no baseline recovery carries:
        # `none` through family 5, and in family 6 (continuable-terminals spec
        # §5.1) `linked_run` naming the terminal's own reason, for every
        # terminal -- so each branch below compares `kind` with this value.
        continues = "none"
        continues = "linked_run"
        _durable_reference._refuse(
            kind == "linked_run", "family 6 terminals continue as linked_run"
        )
        _durable_reference._refuse(
            p["continuation"]["reason"] == reason,
            "a linked continuation names the terminal's reason",
        )
        # §4.4: an exhausted baseline sample is excluded, never a terminal.
        _durable_reference._refuse(
            reason != "baseline_incomplete",
            "family 6 has no baseline_incomplete terminal",
        )
        if self.threshold is not None:
            # §7: "a threshold reached ... writes `campaign_finished`:
            # `infrastructure_failed` with reason `<owner>_unstable` ...,
            # `budget_exhausted` ... for `rollout`". A recorded crossing is
            # resolved by its own terminal and no other, *whether or not* the
            # record carries evidence: a proven terminal placed after it would
            # otherwise rewrite the campaign's outcome and, for
            # `baseline_incomplete`, open a `linked_run` the crossing never had
            # (round-4 re-run, attacks L9, CF-2, CF-4). The executor writes the
            # threshold's terminal for exactly this reason: at the barrier the
            # crossing is the campaign's answer.
            _durable_reference._refuse(
                _THRESHOLD_TERMINAL[self.threshold["owner"]] == (outcome, reason),
                "the terminal does not resolve the recorded threshold",
            )
        unavailable = tuple(
            (number, state.probe_check)
            for number, state in self._rounds.items()
            if state.probe_check is not None and state.probe_check.get("status") == "unavailable"
        )
        if (
            unavailable
            and not probe_continues(self.identity, self._family_version)
            and not (
                # Family 7: an applied close is admitted over an unsuspended
                # unavailable check (see `_authorization_applied`).
                self._owed_terminal == ("stopped", "condition_changed")
            )
        ):
            _durable_reference._refuse(
                (outcome, reason, kind)
                == ("infrastructure_failed", "probe_unavailable", continues),
                "unavailable probe check requires its terminal",
            )
            _durable_reference._refuse(
                p["evidence"] is not None,
                "probe unavailable terminal needs check evidence",
            )
        if p["evidence"] is not None:
            if (
                (outcome == "infrastructure_failed")
                and (reason == "infrastructure_failed")
                and (any(ref["locator"].startswith("campaign.jsonl#") for ref in p["evidence"]))
            ):
                _durable_reference._refuse(
                    kind == continues and len(p["evidence"]) == 1,
                    "probe setup terminal cites one check",
                )
                _durable_reference._refuse(
                    self.identity["reflector"]["probe"] is not None,
                    "probe setup needs a declared probe",
                )
                ready = tuple(
                    (number, state)
                    for number, state in self._rounds.items()
                    if state.probe_check is not None
                )
                _durable_reference._refuse(bool(ready), "probe setup has no check")
                number, state = ready[-1]
                check = _need(state.probe_check, "probe setup has no check")
                _durable_reference._refuse(
                    check["status"] == "ready"
                    and check["reflection_id"] == f"r{number:02d}-x{len(state.reflections) + 1:02d}"
                    and state.closed is None
                    and state.diagnosis(True) is None
                    and state.focus_portion is not None
                    and state.focus_portion["reason"] != "none",
                    "probe setup is not pending for the next reflection",
                )
                evidence = _durable_reference.DurableReference.from_payload(p["evidence"][0])
                _durable_reference._refuse(
                    evidence.kind == "journal_record"
                    and evidence.run_id == self.run_id
                    and evidence.locator == self._probe_check_locators[number]
                    and evidence.content_digest == check["record_digest"],
                    "probe setup terminal cites another record",
                )
                return
            if outcome == "infrastructure_failed" and reason == "probe_unavailable":
                _durable_reference._refuse(
                    (kind == continues),
                    "probe unavailable terminal is not admissible",
                )
                _durable_reference._refuse(
                    bool(unavailable), "no unavailable probe check is recorded"
                )
                _durable_reference._refuse(
                    len(p["evidence"]) == 1,
                    "probe unavailable terminal cites one check",
                )
                number, check = unavailable[-1]
                assert check is not None
                evidence = _durable_reference.DurableReference.from_payload(p["evidence"][0])
                _durable_reference._refuse(
                    evidence.kind == "journal_record"
                    and evidence.run_id == self.run_id
                    and evidence.locator == self._probe_check_locators[number]
                    and evidence.content_digest == check["record_digest"],
                    "probe unavailable terminal cites another record",
                )
                return
            if outcome == "infrastructure_failed" and reason == "grouping_failed":
                _durable_reference._refuse(
                    (kind == continues),
                    "grouping failure terminal is not admissible",
                )
                failures = tuple(
                    state.grouping
                    for state in self._rounds.values()
                    if state.grouping is not None
                    and state.grouping.get("status") == "failed"
                    and state.grouping.get("code") != "interrupted"
                )
                _durable_reference._refuse(
                    bool(failures), "no completed grouping failure is recorded"
                )
                _durable_reference._refuse(
                    len(p["evidence"]) == 1, "grouping failure terminal cites one failure"
                )
                evidence = _durable_reference.DurableReference.from_payload(p["evidence"][0])
                _durable_reference._refuse(
                    evidence.kind == "journal_record"
                    and evidence.run_id == self.run_id
                    and evidence.locator.startswith("campaign.jsonl#bytes=")
                    and evidence.content_digest == failures[-1].get("record_digest"),
                    "grouping failure terminal cites another record",
                )
                return
            admissible = (
                outcome == "infrastructure_failed"
                and reason in ("scorer_metric_missing", "infrastructure_failed")
                and kind == continues
            ) or (
                outcome == "infrastructure_failed"
                and reason == "attestation_mismatch"
                and kind == continues
            )
            _durable_reference._refuse(admissible, "the proven terminal state is not admissible")
            return
        _durable_reference._refuse(kind == continues, "an unproven terminal opens no continuation")
        if outcome == "stopped":
            _durable_reference._refuse(
                reason == "cancelled"
                or (
                    reason == "condition_changed"
                    and self._owed_terminal == ("stopped", "condition_changed")
                ),
                "a stopped campaign is cancelled or closed under a changed condition",
            )
            return
        if outcome == "completed":
            _durable_reference._refuse(
                reason == "rounds_exhausted", "a completed campaign exhausted its rounds"
            )
            for number in range(self.first_round, self.last_round + 1):
                state = self._round(number, "a round of the campaign is not closed")
                _need(state.closed, "a round of the campaign is not closed")
            final = self._rounds[self.last_round]
            closed = _need(final.closed, "the last round is not closed")
            # Family 13 never completes a winner: its cohort measurement was
            # complete before the selection (design §6.12, plan choice 11).
            _durable_reference._refuse(
                closed["outcome"] != "promoted" or final.champion_complete or self._confidence,
                "the last winner's measurement is not complete",
            )
            return
        if (outcome, reason) in _THRESHOLD_TERMINAL.values():
            threshold = _need(self.threshold, "no threshold is recorded")
            _durable_reference._refuse(
                _THRESHOLD_TERMINAL[threshold["owner"]] == (outcome, reason),
                "the terminal does not name the recorded threshold",
            )
            return
        if outcome == "infrastructure_failed" and reason == "editor_unresponsive":
            _durable_reference._refuse(self._unresponsive, "no unresponsive editor is recorded")
            return
        if outcome == "infrastructure_failed" and reason == "reflector_unresponsive":
            # Family 3's own §7 terminal, admitted exactly as the editor's is
            # and against its own flag: a reflection that survived its grace is
            # the only record that can justify it.
            _durable_reference._refuse(
                self._reflector_unresponsive, "no unresponsive reflector is recorded"
            )
            return
        if outcome == "infrastructure_failed" and reason == "grouping_failed":
            failures = tuple(
                state.grouping
                for state in self._rounds.values()
                if state.grouping is not None
                and state.grouping.get("status") == "failed"
                and state.grouping.get("code") != "interrupted"
            )
            _durable_reference._refuse(bool(failures), "no completed grouping failure is recorded")
            _durable_reference._refuse(False, "grouping failure terminal needs failure evidence")
            return
        if outcome == "infrastructure_failed" and reason == "probe_unavailable":
            _durable_reference._refuse(False, "probe unavailable terminal needs check evidence")
            return
        if outcome == "infrastructure_failed" and reason in (
            "scorer_metric_missing",
            "infrastructure_failed",
        ):
            state = self._round(
                max(self._rounds, default=0), "no round closed with a campaign-level reason"
            )
            _durable_reference._refuse(
                any(
                    a.outcome == "insufficient_evidence" and a.reason == reason
                    for a in state.attempts.values()
                ),
                "no attempt closed with that campaign-level reason",
            )
            _durable_reference._refuse(
                all(a.outcome is not None for a in state.attempts.values()),
                "the round still has open attempts",
            )
            return
        raise SchemaError("the terminal state is not admissible")


def adaptation_position(records: Sequence[Envelope], before: int | None = None) -> int:
    """Family 8: the index of the record whose `adaptation_payload` is in force before `before`.

    `campaign_created` and every `identity_rebased` journal the full adaptation
    identity beside its slot, so the one in force at a position is the latest
    such record before it (`0`, the creation record, until a rebase). With
    `before` omitted it is the latest in `records`. The executor and the audit
    both read it here, so they cannot disagree after a rebase.
    """

    end = len(records) if before is None else before
    position = 0
    for index in range(end):
        if records[index].event == "identity_rebased":
            position = index
    return position


def reduce(
    records: Sequence[Envelope],
    *,
    selector: Selector | None,
    adaptive: bool = False,
    family_version: int | None = None,
) -> CampaignSnapshot:
    """Replay a campaign prefix into its frozen snapshot.

    `selector` is keyword-required and has no default, so every replay states its
    mode: `None` is the explicit "do not re-derive the selection" projection over
    a prefix the lifecycle already admitted, while `resume_campaign` passes the
    real selector so replay re-enforces the §8 rule (ruling 1).
    """

    state = CampaignLifecycle(
        selector=selector,
        adaptive=adaptive,
        family_version=family_version,
    )
    for record in records:
        state.accept(record)
    return state.snapshot


def pin_from_adaptation(payload: Mapping[str, Any]) -> EvidencePin | None:
    """Derive a round pin from a validated adaptation; old journals have none."""
    if "evidence" not in payload:
        return None
    checks = parse_checks(payload["checks"]) if "checks" in payload else ENFORCE_ALL
    return EvidencePin(EvidenceSettings.from_payload(payload["evidence"]), checks.input_modes())
