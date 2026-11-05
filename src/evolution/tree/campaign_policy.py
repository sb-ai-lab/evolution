"""Frozen native campaign policies and their canonical journaled bytes.

V11 owns /15's comparison, round stop and final measurement; V12 owns /16's
independent acceptance, noise and verification. Neither writes an alpha ledger.
Their inherited field order, serialization, defaults and refusal precedence
are durable identity contracts (PDR-0038, ADR-0046).

Validation checks all missing keys before unknown keys, then validates values
in a fixed order, with typed PolicyError refusals and schema-specific guidance.
Exact decimal and AlphaPlan value helpers remain shared with statistical code.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Literal, NamedTuple, TypeVar, cast

from evolution.tree.confidence_statistics import confidence_looks
from evolution.tree.execution.interaction import CallRetries, RolloutBudget
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.manifest.campaign_policy_help import (
    OBSERVE_GLOBAL,
    ROUND_STOP_PARALLEL,
    SCHEMA_V14_NAME,
    SCHEMA_V15_NAME,
    SCHEMA_V16_NAME,
    expected_text,
)
from evolution.tree.statistics import GatePlan
from evolution.tree.storage.envelope import is_identifier

SCALE = 12

Owner = Literal["target", "environment", "scorer", "rollout"]
_OWNERS: tuple[Owner, ...] = ("target", "environment", "scorer", "rollout")

_DECIMAL_RE = re.compile(r"^-?[0-9]+(\.[0-9]{1,12})?\Z")
_ALLOWED_CONTROL = frozenset({"\t", "\n"})
_MAX_EDITOR_CONTEXT_BYTES = 65536

_TOP_REQUIRED = frozenset(
    {
        "rounds",
        "attempts_per_round",
        "parallel_attempts",
        "alpha",
        "indeterminate",
        "budget",
        "max_concurrency",
        "max_scorer_concurrency",
        "metric",
        "random_seed",
        "editor_timeout_seconds",
        "reflection_timeout_seconds",
        "gate",
    }
)
# `editor_context` is the one optional top-level key (spec §3: "optional
# string ... absent means null"): allowed but not required, so an absent key
# defaults to `None` instead of raising `missing_key`.
_TOP_ALLOWED = _TOP_REQUIRED | {"editor_context"}
_ATTEMPT_FEEDBACK = frozenset({"none", "sequential"})
_ALPHA_REQUIRED = frozenset({"budget", "spend"})
_INDETERMINATE_REQUIRED = frozenset(_OWNERS)
_OWNER_REQUIRED = frozenset({"max_count", "max_rate"})
_BUDGET_REQUIRED = frozenset({"max_turns", "timeout_seconds"})
#: `/7`'s budget (W19): the same two keys plus a *required* tolerance for
#: consecutive Candidate-owned turn failures. Required with no default
#: (PDR-0005): a default here would hand a `/5` or `/6` document a campaign
#: semantic it never wrote, so an older document keeps `RolloutBudget`'s own
#: code-level default of `1` -- terminate on the first failure -- and only a
#: `/7` document states the tolerance. A second, wider set rather than a
#: mutated first one: both validation passes read `_BUDGET_REQUIRED`, and the
#: older schemas must keep refusing the key as unknown.
_BUDGET_REQUIRED_V7 = _BUDGET_REQUIRED | {"max_failed_turns"}
_METRIC_REQUIRED = frozenset({"primary"})
_GATE_REQUIRED = frozenset({"regression_margin"})
#: `/13`'s gate (G2, PDR-0020): the margin plus three required keys with no
#: default (PDR-0005, as `/11` did): a default would hand a `/12` document a
#: gate it never wrote.
_GATE_REQUIRED_V9 = _GATE_REQUIRED | {"provenance", "fresh_parent", "global"}
_PROVENANCE_KINDS = ("reported", "metadata", "none")
#: `/8`'s own top-level key (I1): required with no default, same reasoning
#: as `_BUDGET_REQUIRED_V7` -- a default here would hand a `/5`-`/7` document
#: a semantic it never wrote. `call_retries` sits beside `budget`, not inside
#: it, because it governs the call layer `rollout._external_call` owns, not
#: the rollout's own turn budget.
_CALL_RETRIES_REQUIRED = frozenset({"max_attempts", "turn_max_attempts"})

GateMode = Literal["statistical", "observe", "point", "confidence", "unfiltered"]
GlobalMode = Literal["off", "point", "bound"]
#: `campaign.gate.causal.tolerance` as `/14` stores it (family-13 design G4):
#: the literal `measured_noise`, or a finite float >= 0.
CausalTolerance = float | Literal["measured_noise"]
MEASURED_NOISE: Literal["measured_noise"] = "measured_noise"

#: `/14`'s campaign (family-13 design G2, G12): `/13`'s top-level keys without
#: the alpha ledger, plus `evaluation`. `gate` is closed by mode in
#: `_validate_structure_v10`.
_TOP_REQUIRED_V10 = (_TOP_REQUIRED - {"alpha"}) | {"evaluation", "attempt_feedback", "call_retries"}
_TOP_ALLOWED_V10 = _TOP_REQUIRED_V10 | {"editor_context", "sample_retries"}
_EVALUATION_KEYS = frozenset({"repeats_per_question"})
_GATE_BASE_V10 = frozenset({"mode", "provenance", "fresh_parent", "global"})
_CONFIDENCE_KEYS = frozenset({"confidence"})
GATE_MODES: tuple[GateMode, ...] = ("statistical", "observe")
GLOBAL_MODES: tuple[GlobalMode, ...] = ("off", "point", "bound")

RoundStop = Literal["first_accepted", "all_attempts"]
Stability = Literal["stable", "unstable"]
ROUND_STOPS: tuple[RoundStop, ...] = ("first_accepted", "all_attempts")
STABILITIES: tuple[Stability, ...] = ("stable", "unstable")
#: `/15`'s campaign (family-14 implementation design R2): `/14`'s keys plus the
#: round stop and the refresh count at the top level. The gate adds
#: `stability` and a `focus` that both modes require, closed by mode in
#: `_validate_structure_v11`.
_TOP_REQUIRED_V11 = _TOP_REQUIRED_V10 | {"round_stop", "baseline_refresh_after_rounds"}
_TOP_ALLOWED_V11 = _TOP_REQUIRED_V11 | {"editor_context", "sample_retries"}
_GATE_BASE_V11 = _GATE_BASE_V10 | {"stability", "focus"}
_MARGIN_KEYS = frozenset({"margin"})

_T = TypeVar("_T")
_S = TypeVar("_S", bound=str)


class PolicyError(ValueError):
    """A closed-code campaign policy refusal.

    `str()` is `f"{code}: {path}"`; `path` names the dotted key path that
    failed and never carries the offending value. `expected` is set only for a
    `/14` refusal (family-13 implementation design D2): a fixed statement of the
    accepted domain at `path`, taken from `campaign_policy_help`, never the
    value. It is not part of `str()`, so every older refusal reads as before.
    """

    def __init__(self, code: str, path: str, *, expected: str | None = None) -> None:
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path
        self.expected = expected


class ReflectionCapacityError(ValueError):
    """The frozen corpus/repeat schedule exceeds the grouping registry limit."""


class AlphaLedgerUnavailable(RuntimeError):
    """A caller requested an alpha ledger or regression margin from a native policy.

    This is a programming error, not a ValueError manifest refusal.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"alpha_ledger_unavailable: {name}")
        self.name = name


def _ledger(value: _T | None, name: str) -> _T:
    if value is None:
        raise AlphaLedgerUnavailable(name)
    return value


def round_down_12(value: Fraction) -> Fraction:
    """Truncate `value` to `SCALE` fractional digits, toward zero, never up."""

    scaled = value * 10**SCALE
    return Fraction(int(scaled), 10**SCALE)


def decimal_string(value: Fraction) -> str:
    """Render a nonnegative `Fraction` as a canonical `SCALE`-digit decimal.

    No exponent, no rounding: the caller guarantees `value` is already a
    multiple of `10 ** -SCALE` (every decimal string this module accepts has
    at most `SCALE` fractional digits, and `round_down_12` produces exact
    multiples), so truncation here never changes the value.
    """

    if value < 0:
        raise ValueError("decimal_string requires a nonnegative value")
    scaled = int(value * 10**SCALE)
    whole, frac = divmod(scaled, 10**SCALE)
    return f"{whole}.{frac:0{SCALE}d}"


@dataclass(frozen=True)
class OwnerThreshold:
    max_count: int
    max_rate: str
    min_tries: int | None = None
    count_recovered: bool | None = None

    @property
    def effective_min_tries(self) -> int:
        return 1 if self.min_tries is None else self.min_tries

    @property
    def effective_count_recovered(self) -> bool:
        return True if self.count_recovered is None else self.count_recovered


def crosses(count: int, denominator: int, limits: OwnerThreshold, kind: str | None = None) -> bool:
    """The count test is immediate; the exact rate test waits for enough tries."""
    count_crossed = count > limits.max_count
    rate_crossed = (
        count >= 1
        and denominator >= limits.effective_min_tries
        and Fraction(count, denominator) > Fraction(limits.max_rate)
    )
    if kind == "count":
        return count_crossed
    if kind == "rate":
        return rate_crossed
    return count_crossed or rate_crossed


class OwnerWindow:
    """Count one owner's in-window tries, with exhaustion shared across owners.

    A sample whose latest observation reserves a successor is pending or later
    recovered. Only a latest observation without a successor makes its tries
    eligible under count_recovered=False. Reset forgets pre-window tries.
    """

    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner
        self._base_seq = 0
        self._counts: dict[tuple[str, str, str], int] = {}
        self._exhausted: dict[tuple[str, str, str], bool] = {}

    def observe(self, record: Mapping[str, Any], seq: int) -> None:
        if seq <= self._base_seq:
            return
        key = tuple(str(record[name]) for name in ("measurement_id", "case_id", "sample_id"))
        sample = (key[0], key[1], key[2])
        if self.owner is None or record["owner"] == self.owner:
            self._counts[sample] = self._counts.get(sample, 0) + 1
        self._exhausted[sample] = not record["retry_follows"]

    def reset(self, base_seq: int) -> None:
        self._base_seq = base_seq
        self._counts.clear()
        self._exhausted.clear()

    def count(self, count_recovered: bool) -> int:
        return sum(
            count
            for key, count in self._counts.items()
            if count_recovered or self._exhausted.get(key, False)
        )


@dataclass(frozen=True)
class AlphaPlan:
    """The alpha ledger derived once from the policy, journaled as strings.

    PDR-0003 freezes the journaled configuration, so the two mappings are
    `MappingProxyType` over private copies: a holder of the plan cannot move a
    share after `campaign_created` made the digest durable.
    """

    budget: str
    spend: Mapping[int, str]
    budget_attempt: str
    share_attempt: Mapping[int, str]

    def __post_init__(self) -> None:
        for name in ("spend", "share_attempt"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))

    def gate_plan(self, seed_mode: str, random_seed: int) -> GatePlan:
        """Build the exact `GatePlan` the bootstrap needs (spec §5: no float image).

        `GatePlan.seed_mode` is `statistics.SeedMode` (`exact | unpaired |
        none`); this signature takes the caller's `str` and passes it through
        unchanged, so a mode the target proved never has to be re-narrowed
        here.
        """

        return GatePlan(
            seed_mode=seed_mode,  # type: ignore[arg-type]
            random_seed=random_seed,
            alpha_spend={look: Fraction(share) for look, share in self.share_attempt.items()},
            alpha_budget=Fraction(self.budget_attempt),
        )


def _frozen(mapping: Mapping[Any, Any]) -> Mapping[Any, Any]:
    """A read-only view over a private copy, in the mapping's own key order."""

    return MappingProxyType(dict(mapping))


def _path(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _check_missing(value: Mapping[str, Any], required: frozenset[str], prefix: str) -> None:
    missing = required - value.keys()
    if missing:
        raise PolicyError("missing_key", _path(prefix, sorted(missing)[0]))


def _check_unknown(value: Mapping[str, Any], allowed: frozenset[str], prefix: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise PolicyError("unknown_key", _path(prefix, sorted(unknown)[0]))


def _check_sections_missing(obj: Mapping[str, Any], budget_keys: frozenset[str]) -> None:
    """Pass 1's `missing_key` checks of the sections every schema shares, in order:
    `indeterminate` (then each owner), `budget` (with `budget_keys`), `metric`.

    The caller checks the top level and its own family section (`alpha`, or
    `/14`'s `evaluation`) before this, and `gate` and `call_retries` after it,
    because that is the order `_validate_structure` has always refused in. A
    section that is not a `dict` is skipped here, exactly as the pass-2
    parsers (`_parse_indeterminate`, `_parse_budget`, `_parse_metric_primary`)
    refuse it as `bad_type`.
    """

    indeterminate = obj.get("indeterminate")
    if isinstance(indeterminate, dict):
        _check_missing(indeterminate, _INDETERMINATE_REQUIRED, "indeterminate")
        for owner in _OWNERS:
            spec = indeterminate.get(owner)
            if isinstance(spec, dict):
                _check_missing(spec, _OWNER_REQUIRED, f"indeterminate.{owner}")

    budget = obj.get("budget")
    if isinstance(budget, dict):
        _check_missing(budget, budget_keys, "budget")

    metric = obj.get("metric")
    if isinstance(metric, dict):
        _check_missing(metric, _METRIC_REQUIRED, "metric")


def _check_sections_unknown(
    obj: Mapping[str, Any], budget_keys: frozenset[str], *, extended_indeterminate: bool = False
) -> None:
    """Pass 1's `unknown_key` checks of the same sections, in the same order as
    `_check_sections_missing`."""

    indeterminate = obj.get("indeterminate")
    if isinstance(indeterminate, dict):
        _check_unknown(indeterminate, _INDETERMINATE_REQUIRED, "indeterminate")
        for owner in _OWNERS:
            spec = indeterminate.get(owner)
            if isinstance(spec, dict):
                allowed = _OWNER_REQUIRED | (
                    {"min_tries", "count_recovered"} if extended_indeterminate else set()
                )
                _check_unknown(spec, allowed, f"indeterminate.{owner}")

    budget = obj.get("budget")
    if isinstance(budget, dict):
        _check_unknown(budget, budget_keys, "budget")

    metric = obj.get("metric")
    if isinstance(metric, dict):
        _check_unknown(metric, _METRIC_REQUIRED, "metric")


def _check_provenance_missing(gate: Mapping[str, Any]) -> None:
    provenance = gate.get("provenance")
    if isinstance(provenance, Mapping):
        _check_missing(provenance, frozenset({"kind"}), "gate.provenance")
        if provenance.get("kind") == "metadata":
            _check_missing(provenance, frozenset({"path"}), "gate.provenance")


def _check_provenance_unknown(gate: Mapping[str, Any]) -> None:
    provenance = gate.get("provenance")
    if isinstance(provenance, Mapping):
        allowed = (
            frozenset({"kind", "path"})
            if provenance.get("kind") == "metadata"
            else frozenset({"kind"})
        )
        _check_unknown(provenance, allowed, "gate.provenance")


def _int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise PolicyError("bad_type", path)
    return value


def _positive_int(value: Any, path: str) -> int:
    result = _int(value, path)
    if result < 1:
        raise PolicyError("bad_value", path)
    return result


def _nonneg_int(value: Any, path: str) -> int:
    result = _int(value, path)
    if result < 0:
        raise PolicyError("bad_value", path)
    return result


def _finite(value: Any, path: str) -> float:
    """A JSON number other than a boolean, as a finite float (`bad_value` otherwise)."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError("bad_type", path)
    # An `int` outside `float` range has no `float` image at all, and
    # `math.isfinite` raises `OverflowError` rather than answering for it. A
    # JSON payload can carry one, so the conversion is guarded and the value is
    # refused with this module's typed error like any other out-of-range one --
    # it is a bad value, not a bad type: the JSON type is right.
    try:
        number = float(value)
    except OverflowError:
        raise PolicyError("bad_value", path) from None
    if not math.isfinite(number):
        raise PolicyError("bad_value", path)
    return number


def _positive_number(value: Any, path: str) -> float:
    number = _finite(value, path)
    if number <= 0:
        raise PolicyError("bad_value", path)
    return number


def _decimal(value: Any, path: str) -> Fraction:
    if not isinstance(value, str):
        raise PolicyError("bad_type", path)
    if _DECIMAL_RE.match(value) is None:
        raise PolicyError("bad_value", path)
    return Fraction(value)


def _decimal_closed01(value: Any, path: str) -> Fraction:
    fraction = _decimal(value, path)
    if not (0 <= fraction <= 1):
        raise PolicyError("bad_value", path)
    return fraction


def _parse_indeterminate(
    obj: Mapping[str, Any], *, extended: bool = False
) -> dict[Owner, OwnerThreshold]:
    indeterminate_raw = obj["indeterminate"]
    if not isinstance(indeterminate_raw, dict):
        raise PolicyError("bad_type", "indeterminate")
    result: dict[Owner, OwnerThreshold] = {}
    for owner in _OWNERS:
        spec = indeterminate_raw[owner]
        if not isinstance(spec, dict):
            raise PolicyError("bad_type", f"indeterminate.{owner}")
        max_count = _nonneg_int(spec["max_count"], f"indeterminate.{owner}.max_count")
        max_rate = _decimal_closed01(spec["max_rate"], f"indeterminate.{owner}.max_rate")
        min_tries = (
            _positive_int(spec["min_tries"], f"indeterminate.{owner}.min_tries")
            if extended and "min_tries" in spec
            else None
        )
        count_recovered = (
            _bool(spec["count_recovered"], f"indeterminate.{owner}.count_recovered")
            if extended and "count_recovered" in spec
            else None
        )
        result[owner] = OwnerThreshold(
            max_count, decimal_string(max_rate), min_tries, count_recovered
        )
    return result


def _parse_budget(obj: Mapping[str, Any]) -> RolloutBudget:
    budget_raw = obj["budget"]
    if not isinstance(budget_raw, dict):
        raise PolicyError("bad_type", "budget")
    # Each field is validated on its own dotted path first (type -> bad_type,
    # range -> bad_value), matching every other field in this module.
    # `RolloutBudget` is still constructed afterwards, as the final proof
    # that the validated values also satisfy its own invariants.
    max_turns = _positive_int(budget_raw["max_turns"], "budget.max_turns")
    timeout_seconds = _positive_number(budget_raw["timeout_seconds"], "budget.timeout_seconds")
    max_failed_turns = _positive_int(budget_raw["max_failed_turns"], "budget.max_failed_turns")
    try:
        return RolloutBudget(
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            max_failed_turns=max_failed_turns,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        raise PolicyError("bad_type", "budget") from exc


def _parse_call_retries(obj: Mapping[str, Any]) -> CallRetries:
    call_retries_raw = obj["call_retries"]
    if not isinstance(call_retries_raw, dict):
        raise PolicyError("bad_type", "call_retries")
    max_attempts = _int(call_retries_raw["max_attempts"], "call_retries.max_attempts")
    turn_max_attempts = _int(
        call_retries_raw["turn_max_attempts"], "call_retries.turn_max_attempts"
    )
    try:
        return CallRetries(max_attempts=max_attempts, turn_max_attempts=turn_max_attempts)
    except ValueError as exc:
        # `CallRetries.__post_init__` names the offending key in its message
        # as `call_retries.<key>`, matching this module's dotted paths.
        bad_key = "turn_max_attempts" if "turn_max_attempts" in str(exc) else "max_attempts"
        raise PolicyError("bad_value", f"call_retries.{bad_key}") from exc


def _parse_metric_primary(obj: Mapping[str, Any]) -> str:
    metric = obj["metric"]
    if not isinstance(metric, dict):
        raise PolicyError("bad_type", "metric")
    primary = metric["primary"]
    if not isinstance(primary, str):
        raise PolicyError("bad_type", "metric.primary")
    if not is_identifier(primary):
        raise PolicyError("bad_value", "metric.primary")
    return primary


def _parse_editor_context(
    obj: Mapping[str, Any],
    *,
    key: str = "editor_context",
    limit: int = _MAX_EDITOR_CONTEXT_BYTES,
    configurable: bool = False,
) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError("bad_type", key)
    if len(value.encode("utf-8")) > limit:
        raise PolicyError(
            "bad_value",
            key,
            expected=f"at most {limit} bytes of UTF-8 (campaign.context_max_bytes)"
            if configurable
            else None,
        )
    if any(ord(ch) < 0x20 and ch not in _ALLOWED_CONTROL for ch in value) or any(
        ord(ch) == 0x7F for ch in value
    ):
        raise PolicyError("bad_value", key)
    return value


def decides_at_once(policy: CampaignPolicyV11) -> bool:
    """Decide each Attempt before opening the next, independently of feedback.

    Policies predating round-stop semantics keep their feedback-mode behavior.
    """
    return (
        policy.attempt_feedback == "sequential"
        or getattr(policy, "round_stop", None) == "first_accepted"
    )


def _parse_attempt_feedback(
    obj: Mapping[str, Any], parallel_attempts: int
) -> Literal["none", "sequential"]:
    raw = obj["attempt_feedback"]
    if not isinstance(raw, str):
        raise PolicyError("bad_type", "attempt_feedback")
    if raw not in _ATTEMPT_FEEDBACK:
        raise PolicyError("bad_value", "attempt_feedback")
    if raw == "sequential" and parallel_attempts != 1:
        raise PolicyError("bad_value", "attempt_feedback")
    return cast(Literal["none", "sequential"], raw)


def _parse_sample_retries(obj: Mapping[str, Any]) -> int:
    raw = obj.get("sample_retries", 1)
    if type(raw) is not int:
        raise PolicyError("bad_type", "sample_retries")
    if not (0 <= raw <= 8):
        raise PolicyError("bad_value", "sample_retries")
    return raw


def _v10_mode(obj: object) -> GateMode | None:
    """`gate.mode` when it is one of `GATE_MODES`, else `None`."""

    gate = obj.get("gate") if isinstance(obj, Mapping) else None
    mode = gate.get("mode") if isinstance(gate, Mapping) else None
    return mode if isinstance(mode, str) and mode in GATE_MODES else None


def _check_unknown_v10(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    section: str,
    mode: GateMode | None,
    *,
    schema: str = SCHEMA_V14_NAME,
) -> None:
    """`_check_unknown` with the fixed `/14` (or `/15`) text for `section` (D2, S1, R4)."""

    try:
        _check_unknown(value, allowed, section)
    except PolicyError as exc:
        expected = expected_text(exc.code, exc.path, mode, section=section, schema=schema)
        raise PolicyError(exc.code, exc.path, expected=expected) from None


def _validate_structure_v11(obj: Mapping[str, Any]) -> None:
    """Pass 1 for `/15` (family-14 design R2), in `_validate_structure_v10`'s order.

    `focus` is required in both modes: `{confidence, margin}` in `statistical`
    and `{margin}` in `observe`, where `focus.confidence` is refused as an
    unknown key with the forbidden-key text. `causal` is required in
    `statistical` and optional in `observe`, as in `/14`. While the mode is
    absent or unusable, only the mode-independent keys are required
    (`focus.margin` among them) and both focus keys are allowed; pass 2
    refuses the mode.
    """

    mode = _v10_mode(obj)
    statistical, observe = mode == "statistical", mode == "observe"
    gate_required = _GATE_BASE_V11 | ({"causal"} if statistical else frozenset())
    gate_allowed = _GATE_BASE_V11 | {"causal"}
    focus_required = _MARGIN_KEYS | (_CONFIDENCE_KEYS if statistical else frozenset())
    focus_allowed = _MARGIN_KEYS | (frozenset() if observe else _CONFIDENCE_KEYS)
    causal_required = _CONFIDENCE_KEYS if statistical else frozenset()
    causal_allowed = frozenset({"tolerance"}) | (frozenset() if observe else _CONFIDENCE_KEYS)

    evaluation = obj.get("evaluation")
    call_retries = obj.get("call_retries")
    raw_gate = obj.get("gate")
    gate = raw_gate if isinstance(raw_gate, Mapping) else None
    focus = gate.get("focus") if gate is not None else None
    causal = gate.get("causal") if gate is not None else None

    _check_missing(obj, _TOP_REQUIRED_V11, "")
    if isinstance(evaluation, Mapping):
        _check_missing(evaluation, _EVALUATION_KEYS, "evaluation")
    _check_sections_missing(obj, _BUDGET_REQUIRED_V7)
    if gate is not None:
        _check_missing(gate, gate_required, "gate")
        _check_provenance_missing(gate)
        if isinstance(focus, Mapping):
            _check_missing(focus, focus_required, "gate.focus")
        if statistical and isinstance(causal, Mapping):
            _check_missing(causal, causal_required, "gate.causal")
    if isinstance(call_retries, dict):
        _check_missing(call_retries, _CALL_RETRIES_REQUIRED, "call_retries")

    v15 = SCHEMA_V15_NAME
    _check_unknown_v10(obj, _TOP_ALLOWED_V11, "", mode, schema=v15)
    if isinstance(evaluation, Mapping):
        _check_unknown_v10(evaluation, _EVALUATION_KEYS, "evaluation", mode, schema=v15)
    _check_sections_unknown(obj, _BUDGET_REQUIRED_V7)
    if gate is not None:
        _check_unknown_v10(gate, gate_allowed, "gate", mode, schema=v15)
        try:
            _check_provenance_unknown(gate)
        except PolicyError as exc:
            expected = expected_text(
                exc.code, exc.path, mode, section="gate.provenance", schema=v15
            )
            raise PolicyError(exc.code, exc.path, expected=expected) from None
        if isinstance(focus, Mapping):
            _check_unknown_v10(focus, focus_allowed, "gate.focus", mode, schema=v15)
        if isinstance(causal, Mapping):
            _check_unknown_v10(causal, causal_allowed, "gate.causal", mode, schema=v15)
    if isinstance(call_retries, dict):
        _check_unknown(call_retries, _CALL_RETRIES_REQUIRED, "call_retries")


def _confidence(value: Any, path: str) -> float:
    number = _finite(value, path)
    if not 0 < number < 1:
        raise PolicyError("bad_value", path)
    return number


def _tolerance(value: Any, path: str) -> CausalTolerance:
    if isinstance(value, str):
        if value == MEASURED_NOISE:
            return MEASURED_NOISE
        raise PolicyError("bad_value", path)
    number = _finite(value, path)
    if number < 0:
        raise PolicyError("bad_value", path)
    # `-0.0` and `0` are one allowance, so they are one spelling (G4).
    return 0.0 if number == 0 else number


def _margin(value: Any, path: str) -> float:
    """`gate.focus.margin`: any finite number (R2). `-0.0`, `0` and `0.0` are
    one margin, so they are one spelling, as `_tolerance` does (G4)."""

    number = _finite(value, path)
    return 0.0 if number == 0 else number


def _parse_round_stop(obj: Mapping[str, Any], parallel_attempts: int) -> RoundStop:
    """`round_stop`, and the one new combination refusal (/15 design §3.2, R3)."""

    value = _choice(obj["round_stop"], "round_stop", ROUND_STOPS)
    if value == "first_accepted" and parallel_attempts > 1:
        raise PolicyError("bad_value", "round_stop", expected=ROUND_STOP_PARALLEL)
    return value


def _choice(value: Any, path: str, choices: tuple[_S, ...]) -> _S:
    if not isinstance(value, str):
        raise PolicyError("bad_type", path)
    for choice in choices:
        if value == choice:
            return choice
    raise PolicyError("bad_value", path)


def _parse_v11(payload: object) -> dict[str, Any]:
    """Every field of a /15 policy, or the first refusal.

    The retained pass-2 order starts with `rounds`, `attempts_per_round`, `parallel_attempts`,
    `evaluation.repeats_per_question`, `indeterminate`, `budget`,
    `call_retries`, `max_concurrency`, `max_scorer_concurrency`, `metric`,
    `random_seed`, `editor_timeout_seconds`, `reflection_timeout_seconds`,
    `gate.mode`, the statistical R >= 2 rule, `gate.focus.confidence`,
    `gate.causal.confidence`, `gate.causal.tolerance`, `gate.provenance`,
    `gate.fresh_parent`, `gate.global`, the observe `global: off` rule,
    `editor_context`, `attempt_feedback`, `sample_retries`.

    Family-14 design R2, R3 inserts `round_stop` with its
    `first_accepted` rule and `baseline_refresh_after_rounds` right after
    `parallel_attempts`, so the round-stop refusal precedes
    `attempt_feedback`'s `sequential` rule; `gate.stability` right after the
    R >= 2 rule; and `gate.focus.margin` right after `gate.focus.confidence`.
    """

    if not isinstance(payload, Mapping):
        raise PolicyError("bad_type", "")
    _validate_structure_v11(payload)
    rounds, attempts_per_round, parallel_attempts = _parse_attempts(payload)
    round_stop = _parse_round_stop(payload, parallel_attempts)
    refresh = _nonneg_int(payload["baseline_refresh_after_rounds"], "baseline_refresh_after_rounds")
    evaluation = payload["evaluation"]
    if not isinstance(evaluation, Mapping):
        raise PolicyError("bad_type", "evaluation")
    repeats = _positive_int(evaluation["repeats_per_question"], "evaluation.repeats_per_question")
    settings = _parse_run_settings(payload)

    gate = payload["gate"]
    if not isinstance(gate, Mapping):
        raise PolicyError("bad_type", "gate")
    mode = _choice(gate["mode"], "gate.mode", GATE_MODES)
    statistical = mode == "statistical"
    if statistical and repeats < 2:
        raise PolicyError("bad_value", "evaluation.repeats_per_question")
    stability = _choice(gate["stability"], "gate.stability", STABILITIES)
    focus_confidence: float | None = None
    focus_margin: float | None = None
    causal_confidence: float | None = None
    focus = gate["focus"]
    if not isinstance(focus, Mapping):
        raise PolicyError("bad_type", "gate.focus")
    if statistical:
        focus_confidence = _confidence(focus["confidence"], "gate.focus.confidence")
    focus_margin = _margin(focus["margin"], "gate.focus.margin")
    causal = gate.get("causal", {})
    if not isinstance(causal, Mapping):
        raise PolicyError("bad_type", "gate.causal")
    if statistical:
        causal_confidence = _confidence(causal["confidence"], "gate.causal.confidence")
    tolerance = _tolerance(causal.get("tolerance", MEASURED_NOISE), "gate.causal.tolerance")
    provenance = _parse_provenance(gate)
    fresh_parent = _bool(gate["fresh_parent"], "gate.fresh_parent")
    global_mode = _choice(gate["global"], "gate.global", GLOBAL_MODES)
    if mode == "observe" and global_mode != "off":
        raise PolicyError("bad_value", "gate.global", expected=OBSERVE_GLOBAL)

    editor_context = _parse_editor_context(payload)
    attempt_feedback = _parse_attempt_feedback(payload, parallel_attempts)
    sample_retries = _parse_sample_retries(payload)
    return {
        "rounds": rounds,
        "attempts_per_round": attempts_per_round,
        "parallel_attempts": parallel_attempts,
        "_alpha_budget": None,
        "_alpha_spend": None,
        **settings._asdict(),
        "editor_context": editor_context,
        "_regression_margin": None,
        "gate_provenance": MappingProxyType(provenance),
        "fresh_parent": fresh_parent,
        "global_layer": False,
        "gate_mode": mode,
        "repeats_per_question": repeats,
        "focus_confidence": focus_confidence,
        "causal_confidence": causal_confidence,
        "causal_tolerance": tolerance,
        "global_mode": global_mode,
        "round_stop": round_stop,
        "baseline_refresh_after_rounds": refresh,
        "stability": stability,
        "focus_margin": focus_margin,
        "attempt_feedback": attempt_feedback,
        "sample_retries": sample_retries,
        "declares_failed_turns": True,
        "declares_call_retries": True,
        "_alpha_plan": None,
    }


def _parse_attempts(obj: Mapping[str, Any]) -> tuple[int, int, int]:
    """Pass 2's first three fields: `rounds`, `attempts_per_round`, `parallel_attempts`."""

    rounds = _positive_int(obj["rounds"], "rounds")
    attempts_per_round = _positive_int(obj["attempts_per_round"], "attempts_per_round")
    parallel_attempts = _int(obj["parallel_attempts"], "parallel_attempts")
    if not (1 <= parallel_attempts <= attempts_per_round):
        raise PolicyError("bad_value", "parallel_attempts")
    return rounds, attempts_per_round, parallel_attempts


class _RunSettings(NamedTuple):
    """The pass-2 fields every schema shares between its family section and its gate.

    Each name is the `CampaignPolicyV11` field it fills, so a parser can set
    them by name (`_asdict()`).
    """

    indeterminate: Mapping[Owner, OwnerThreshold]
    budget: RolloutBudget
    max_concurrency: int
    max_scorer_concurrency: int
    metric_primary: str
    random_seed: int
    editor_timeout_seconds: float
    reflection_timeout_seconds: float


def _parse_run_settings(
    obj: Mapping[str, Any], *, extended_indeterminate: bool = False
) -> _RunSettings:
    """Pass 2 from `indeterminate` to `reflection_timeout_seconds`, in the order
    `CampaignPolicyV11.__init__` has always refused in: `indeterminate`,
    `budget`, `call_retries` (when required), `max_concurrency`,
    `max_scorer_concurrency`, `metric`, `random_seed`,
    `editor_timeout_seconds`, `reflection_timeout_seconds`.

    The caller parses `_parse_attempts` and its own family section (`alpha`,
    or `/14`'s `evaluation`) before this, and its gate after it.
    """

    indeterminate = _parse_indeterminate(obj, extended=extended_indeterminate)
    budget = replace(_parse_budget(obj), call_retries=_parse_call_retries(obj))
    max_concurrency = _positive_int(obj["max_concurrency"], "max_concurrency")
    max_scorer_concurrency = _positive_int(obj["max_scorer_concurrency"], "max_scorer_concurrency")
    metric_primary = _parse_metric_primary(obj)
    random_seed = _int(obj["random_seed"], "random_seed")
    editor_timeout_seconds = _positive_number(
        obj["editor_timeout_seconds"], "editor_timeout_seconds"
    )
    reflection_timeout_seconds = _positive_number(
        obj["reflection_timeout_seconds"], "reflection_timeout_seconds"
    )
    return _RunSettings(
        indeterminate=_frozen(indeterminate),
        budget=budget,
        max_concurrency=max_concurrency,
        max_scorer_concurrency=max_scorer_concurrency,
        metric_primary=metric_primary,
        random_seed=random_seed,
        editor_timeout_seconds=editor_timeout_seconds,
        reflection_timeout_seconds=reflection_timeout_seconds,
    )


def _parse_provenance(gate: Mapping[str, Any]) -> dict[str, str]:
    from evolution.tree.execution.json_pointer import _pointer_tokens

    provenance = gate["provenance"]
    if not isinstance(provenance, Mapping):
        raise PolicyError("bad_type", "gate.provenance")
    kind = provenance["kind"]
    if not isinstance(kind, str):
        raise PolicyError("bad_type", "gate.provenance.kind")
    if kind not in _PROVENANCE_KINDS:
        raise PolicyError("bad_value", "gate.provenance.kind")
    parsed = {"kind": kind}
    if kind == "metadata":
        path = provenance["path"]
        if not isinstance(path, str):
            raise PolicyError("bad_type", "gate.provenance.path")
        try:
            if not path.startswith("/"):
                raise ValueError("invalid_json_pointer")
            _pointer_tokens(path)
        except ValueError:
            raise PolicyError("bad_value", "gate.provenance.path") from None
        parsed["path"] = path
    return parsed


def _bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise PolicyError("bad_type", path)
    return value


@dataclass(frozen=True, init=False)
class CampaignPolicyV11:
    """Frozen /15 policy (family 14); field order and canonical bytes are durable."""

    rounds: int
    attempts_per_round: int
    parallel_attempts: int
    #: The alpha ledger and margin of families 1 to 12, read through the
    #: guarded properties below. `None` only for a `/14` policy, which has
    #: neither (family-13 implementation design §3 G2).
    _alpha_budget: str | None
    _alpha_spend: Mapping[int, str] | None
    indeterminate: Mapping[Owner, OwnerThreshold]
    budget: RolloutBudget
    max_concurrency: int
    max_scorer_concurrency: int
    metric_primary: str
    random_seed: int
    editor_timeout_seconds: float
    editor_context: str | None
    reflection_timeout_seconds: float
    _regression_margin: str | None
    #: `/13` gate keys (G2). `None` below V9: those payloads carry no such
    #: keys, so neither serialization nor digest may show them.
    gate_provenance: Mapping[str, str] | None
    fresh_parent: bool | None
    global_layer: bool | None  # `global` is a keyword
    #: `/14` keys (family-13 design G1, G5). `None` below V10: those payloads
    #: carry no such keys, so neither serialization nor digest may show them.
    gate_mode: GateMode | None
    repeats_per_question: int | None
    focus_confidence: float | None
    causal_confidence: float | None
    causal_tolerance: CausalTolerance | None
    global_mode: GlobalMode | None
    #: `/15` keys (family-14 design R2). `None` below V11: those payloads carry
    #: no such keys, so neither serialization nor digest may show them.
    round_stop: RoundStop | None
    baseline_refresh_after_rounds: int | None
    stability: Stability | None
    focus_margin: float | None
    #: Private family-4 addition. `None` is the exact existing /3 shape and
    #: does not appear in /3 serialization or its digest.
    attempt_feedback: Literal["none", "sequential"] | None
    #: Private family-5 addition. `None` is the exact existing /4 shape and
    #: does not appear in /4 serialization or its digest.
    sample_retries: int | None
    #: Whether this policy came from a `/7` document, which is exactly whether
    #: `budget.max_failed_turns` is serialized (W19). Not configuration: it is
    #: the schema that declared this policy, kept because `to_payload` must
    #: reproduce the bytes that schema's digest was taken over. Named
    #: `declares_*`, not `require_*`, because as a stored field on this value
    #: object nothing is being required -- the policy simply declares the key.
    #: It stays in equality -- two policies whose payloads differ are not the
    #: same policy -- and out of `repr`, where the budget already shows the
    #: value.
    declares_failed_turns: bool = field(repr=False)
    #: Whether this policy came from a `/8` document, which is exactly whether
    #: `call_retries` is serialized (I1). Same reasoning as
    #: `declares_failed_turns`: named `declares_*`, kept in equality, out of
    #: `repr` (the budget already shows the value).
    declares_call_retries: bool = field(repr=False)
    #: Derived, not configured: it is neither part of the policy's identity nor
    #: of its `repr`, and `to_payload`/`digest` never see it.
    _alpha_plan: AlphaPlan | None = field(compare=False, repr=False)

    def __init__(self, payload: Mapping[str, Any]) -> None:
        try:
            values = _parse_v11(payload)
        except PolicyError as exc:
            if exc.expected is None:
                expected = expected_text(
                    exc.code, exc.path, _v10_mode(payload), schema=SCHEMA_V15_NAME
                )
                if expected is not None:
                    raise PolicyError(exc.code, exc.path, expected=expected) from exc
            raise
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CampaignPolicyV11:
        return cls(payload)

    @property
    def reflector_operator_context(self) -> str | None:
        return self.editor_context

    def to_payload(self) -> dict[str, Any]:
        """The exact /15 key order and resolved values, including optional defaults."""

        assert self.gate_provenance is not None
        statistical = self.gate_mode == "statistical"
        causal: dict[str, Any] = {"tolerance": self.causal_tolerance}
        if statistical:
            causal["confidence"] = self.causal_confidence
        gate: dict[str, Any] = {"mode": self.gate_mode}
        if statistical:
            gate["focus"] = {"confidence": self.focus_confidence}
        gate["causal"] = causal
        gate["provenance"] = dict(self.gate_provenance)
        gate["fresh_parent"] = self.fresh_parent
        gate["global"] = self.global_mode
        payload = self._payload_around(
            {"evaluation": {"repeats_per_question": self.repeats_per_question}}, gate
        )
        focus: dict[str, Any] = {"margin": self.focus_margin}
        if self.gate_mode == "statistical":
            focus = {"confidence": self.focus_confidence, "margin": self.focus_margin}
        payload["gate"]["focus"] = focus
        payload["gate"]["stability"] = self.stability
        payload["round_stop"] = self.round_stop
        payload["baseline_refresh_after_rounds"] = self.baseline_refresh_after_rounds
        return payload

    def _payload_around(self, family: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
        """The payload every schema shares, around its own two parts.

        `family` holds the section that follows `parallel_attempts` (`alpha`,
        or `/14`'s `evaluation`); `gate` is the `gate` section. Keys keep the
        order `to_payload` has always written (`canonical_json` sorts them
        anyway, so only the content reaches a digest).
        """

        result: dict[str, Any] = {
            "rounds": self.rounds,
            "attempts_per_round": self.attempts_per_round,
            "parallel_attempts": self.parallel_attempts,
            **family,
            "indeterminate": {
                owner: {
                    "max_count": threshold.max_count,
                    "max_rate": threshold.max_rate,
                    **(
                        {"min_tries": threshold.min_tries}
                        if threshold.min_tries is not None
                        else {}
                    ),
                    **(
                        {"count_recovered": threshold.count_recovered}
                        if threshold.count_recovered is not None
                        else {}
                    ),
                }
                for owner, threshold in self.indeterminate.items()
            },
            "budget": {
                "max_turns": self.budget.max_turns,
                "timeout_seconds": self.budget.timeout_seconds,
                "max_failed_turns": self.budget.max_failed_turns,
            },
            "max_concurrency": self.max_concurrency,
            "max_scorer_concurrency": self.max_scorer_concurrency,
            "metric": {"primary": self.metric_primary},
            "random_seed": self.random_seed,
            "editor_timeout_seconds": self.editor_timeout_seconds,
            "editor_context": self.editor_context,
            "reflection_timeout_seconds": self.reflection_timeout_seconds,
            "gate": dict(gate),
        }
        if self.attempt_feedback is not None:
            result["attempt_feedback"] = self.attempt_feedback
        if self.sample_retries is not None:
            result["sample_retries"] = self.sample_retries
        assert self.budget.call_retries is not None
        result["call_retries"] = {
            "max_attempts": self.budget.call_retries.max_attempts,
            "turn_max_attempts": self.budget.call_retries.turn_max_attempts,
        }
        return result

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_payload())).hexdigest()

    @property
    def declares_alpha(self) -> bool:
        """Native policies carry no alpha ledger; guarded access still raises."""
        return False

    @property
    def alpha_budget(self) -> str:
        return _ledger(self._alpha_budget, "alpha_budget")

    @property
    def alpha_spend(self) -> Mapping[int, str]:
        return _ledger(self._alpha_spend, "alpha_spend")

    @property
    def regression_margin(self) -> str:
        return _ledger(self._regression_margin, "regression_margin")

    @property
    def alpha_plan(self) -> AlphaPlan:
        return _ledger(self._alpha_plan, "alpha_plan")

    @property
    def repeats(self) -> int:
        """R, `evaluation.repeats_per_question` (G5)."""

        assert self.repeats_per_question is not None  # every V10 sets it
        return self.repeats_per_question

    @property
    def looks(self) -> tuple[int, ...]:
        """G5, owned by the family-13 kernel: `statistical` checks at each of
        3, 5 and 10 below R, then at R; `observe` evaluates once, at R."""

        assert self.gate_mode is not None  # every V10 sets it
        return confidence_looks(
            cast(Literal["statistical", "observe"], self.gate_mode), self.repeats
        )


def _validate_structure_v12(obj: Mapping[str, Any]) -> None:
    """The closed /16 shape, independent of the historical bootstrap validators."""

    gate = obj.get("gate")
    confidence = isinstance(gate, Mapping) and gate.get("mode") == "confidence"
    intervals = isinstance(gate, Mapping) and gate.get("mode") in ("statistical", "confidence")
    top = _TOP_REQUIRED_V11
    gate_keys = _GATE_BASE_V11 | {"causal", "execution_noise", "verification"}
    if confidence:
        gate_keys |= {"statistics"}
    sections = [
        (
            obj,
            top,
            "",
            top | {"editor_context", "sample_retries", "reflector_context", "context_max_bytes"},
        )
    ]
    evaluation = obj.get("evaluation")
    if isinstance(evaluation, Mapping):
        sections.append((evaluation, _EVALUATION_KEYS, "evaluation", _EVALUATION_KEYS))
    if isinstance(gate, Mapping):
        sections.append((gate, gate_keys, "gate", gate_keys | {"bound_fallback", "stages"}))
        for key, keys in (
            ("focus", {"margin"} | ({"confidence"} if intervals else set())),
            ("causal", {"tolerance"} | ({"confidence"} if intervals else set())),
            ("statistics", {"method", "unit"}),
            (
                "execution_noise",
                {"kind"}
                | (
                    {"allowance", "source"}
                    if isinstance(gate.get("execution_noise"), Mapping)
                    and gate["execution_noise"].get("kind") == "bounded"
                    else set()
                ),
            ),
        ):
            section = gate.get(key)
            if isinstance(section, Mapping) and (key != "statistics" or confidence):
                sections.append((section, frozenset(keys), f"gate.{key}", frozenset(keys)))
    retries = obj.get("call_retries")
    if isinstance(retries, dict):
        sections.append((retries, _CALL_RETRIES_REQUIRED, "call_retries", _CALL_RETRIES_REQUIRED))
    for section, required, path, _allowed in sections:
        _check_missing(section, required, path)
    _check_sections_missing(obj, _BUDGET_REQUIRED_V7)
    if isinstance(gate, Mapping):
        _check_provenance_missing(gate)
    for section, _required, path, allowed in sections:
        _check_unknown(section, allowed, path)
    _check_sections_unknown(obj, _BUDGET_REQUIRED_V7, extended_indeterminate=True)
    if isinstance(gate, Mapping):
        _check_provenance_unknown(gate)


def _parse_v12(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise PolicyError("bad_type", "")
    _validate_structure_v12(payload)
    rounds, attempts, parallel = _parse_attempts(payload)
    stop = _parse_round_stop(payload, parallel)
    refresh = _nonneg_int(payload["baseline_refresh_after_rounds"], "baseline_refresh_after_rounds")
    evaluation = payload["evaluation"]
    if not isinstance(evaluation, Mapping):
        raise PolicyError("bad_type", "evaluation")
    repeats = _positive_int(evaluation["repeats_per_question"], "evaluation.repeats_per_question")
    settings = _parse_run_settings(payload, extended_indeterminate=True)
    gate = payload["gate"]
    if not isinstance(gate, Mapping):
        raise PolicyError("bad_type", "gate")
    mode = _choice(gate["mode"], "gate.mode", ("point", "confidence", "unfiltered", "statistical"))
    if mode == "statistical" and repeats < 2:
        raise PolicyError("bad_value", "evaluation.repeats_per_question")
    stability = _choice(gate["stability"], "gate.stability", STABILITIES)
    for key in ("focus", "causal", "execution_noise"):
        if not isinstance(gate[key], Mapping):
            raise PolicyError("bad_type", f"gate.{key}")
    focus, causal = gate["focus"], gate["causal"]
    margin = _margin(focus["margin"], "gate.focus.margin")
    tolerance = _tolerance(causal["tolerance"], "gate.causal.tolerance")
    if tolerance == MEASURED_NOISE and (repeats < 2 or mode == "unfiltered"):
        raise PolicyError("bad_value", "gate.causal.tolerance")
    focus_confidence = causal_confidence = None
    statistics = None
    if mode in ("statistical", "confidence"):
        focus_confidence = _confidence(focus["confidence"], "gate.focus.confidence")
        causal_confidence = _confidence(causal["confidence"], "gate.causal.confidence")
    if mode == "confidence":
        statistics = gate["statistics"]
        if not isinstance(statistics, Mapping):
            raise PolicyError("bad_type", "gate.statistics")
        statistics = MappingProxyType(
            {
                "method": _choice(
                    statistics["method"], "gate.statistics.method", ("bounded-weighted-mean/1",)
                ),
                "unit": _choice(statistics["unit"], "gate.statistics.unit", ("case", "group")),
            }
        )
    stages = None
    if "stages" in gate:
        value = _choice(gate["stages"], "gate.stages", ("single", "focus_first"))
        if value == "focus_first":
            stages = value
    bound_fallback = None
    if "bound_fallback" in gate:
        if mode != "confidence":
            raise PolicyError("bad_value", "gate.bound_fallback")
        bound_fallback = _choice(gate["bound_fallback"], "gate.bound_fallback", ("point",))
    noise = gate["execution_noise"]
    kind = _choice(
        noise["kind"], "gate.execution_noise.kind", ("negligible", "bounded", "observed")
    )
    parsed_noise: dict[str, Any] = {"kind": kind}
    if kind == "bounded":
        allowance = _margin(noise["allowance"], "gate.execution_noise.allowance")
        if allowance < 0:
            raise PolicyError("bad_value", "gate.execution_noise.allowance")
        source = noise["source"]
        if not isinstance(source, str):
            raise PolicyError("bad_type", "gate.execution_noise.source")
        if not source.strip() or len(source) > 1024:
            raise PolicyError("bad_value", "gate.execution_noise.source")
        try:
            source.encode("utf-8")
        except UnicodeEncodeError:
            raise PolicyError("bad_value", "gate.execution_noise.source") from None
        parsed_noise.update(allowance=allowance, source=source)
    verification = _choice(gate["verification"], "gate.verification", ("reuse", "fresh"))
    global_mode = _choice(gate["global"], "gate.global", GLOBAL_MODES)
    if (mode != "confidence" and global_mode == "bound") or (
        mode == "unfiltered" and global_mode != "off"
    ):
        raise PolicyError("bad_value", "gate.global")
    context_max_bytes = (
        _positive_int(payload["context_max_bytes"], "context_max_bytes")
        if "context_max_bytes" in payload
        else None
    )
    context_limit = _MAX_EDITOR_CONTEXT_BYTES if context_max_bytes is None else context_max_bytes
    return {
        "rounds": rounds,
        "attempts_per_round": attempts,
        "parallel_attempts": parallel,
        "_alpha_budget": None,
        "_alpha_spend": None,
        "_alpha_plan": None,
        **settings._asdict(),
        "editor_context": _parse_editor_context(payload, limit=context_limit, configurable=True),
        "reflector_context": _parse_editor_context(
            payload, key="reflector_context", limit=context_limit, configurable=True
        ),
        "context_max_bytes": context_max_bytes,
        "_regression_margin": None,
        "gate_provenance": MappingProxyType(_parse_provenance(gate)),
        "fresh_parent": _bool(gate["fresh_parent"], "gate.fresh_parent"),
        "global_layer": False,
        "gate_mode": mode,
        "repeats_per_question": repeats,
        "focus_confidence": focus_confidence,
        "causal_confidence": causal_confidence,
        "causal_tolerance": tolerance,
        "global_mode": global_mode,
        "round_stop": stop,
        "baseline_refresh_after_rounds": refresh,
        "stability": stability,
        "focus_margin": margin,
        "attempt_feedback": _parse_attempt_feedback(payload, parallel),
        "sample_retries": _parse_sample_retries(payload),
        "declares_failed_turns": True,
        "declares_call_retries": True,
        "statistics": statistics,
        "execution_noise": MappingProxyType(parsed_noise),
        "verification": verification,
        "bound_fallback": bound_fallback,
        "stages": stages,
    }


@dataclass(frozen=True, init=False)
class CampaignPolicyV12(CampaignPolicyV11):
    """Manifest /16: independent acceptance, noise and verification; automatic promotion.

    Shares V11's runtime protocol, never its bootstrap validator or look schedule.
    Source text is screened with the run's secrets when its identity is admitted.
    """

    statistics: Mapping[str, str] | None
    execution_noise: Mapping[str, Any]
    verification: Literal["reuse", "fresh"]
    #: None means absent: old /16 payloads and digests must not gain a key.
    bound_fallback: Literal["point"] | None
    stages: Literal["focus_first"] | None
    reflector_context: str | None
    context_max_bytes: int | None

    @property
    def reflector_operator_context(self) -> str | None:
        if self.reflector_context is None:
            return self.editor_context
        return self.reflector_context or None

    @property
    def focus_first(self) -> bool:
        """Whether gate stages are cut by scope at the single declared R."""
        return self.stages == "focus_first"

    def __init__(self, payload: Mapping[str, Any]) -> None:
        try:
            values = _parse_v12(payload)
        except PolicyError as exc:
            if exc.expected is None:
                gate = payload.get("gate") if isinstance(payload, Mapping) else None
                mode = gate.get("mode") if isinstance(gate, Mapping) else None
                expected = expected_text(exc.code, exc.path, mode, schema=SCHEMA_V16_NAME)
                if expected is not None:
                    raise PolicyError(exc.code, exc.path, expected=expected) from exc
            raise
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CampaignPolicyV12:
        return cls(payload)

    def to_payload(self) -> dict[str, Any]:
        focus: dict[str, Any] = {"margin": self.focus_margin}
        causal: dict[str, Any] = {"tolerance": self.causal_tolerance}
        if self.gate_mode in ("statistical", "confidence"):
            focus["confidence"] = self.focus_confidence
            causal["confidence"] = self.causal_confidence
        assert self.gate_provenance is not None
        gate: dict[str, Any] = {
            "mode": self.gate_mode,
            "focus": focus,
            "causal": causal,
            "provenance": dict(self.gate_provenance),
            "fresh_parent": self.fresh_parent,
            "global": self.global_mode,
            "stability": self.stability,
            "execution_noise": dict(self.execution_noise),
            "verification": self.verification,
        }
        if self.statistics is not None:
            gate["statistics"] = dict(self.statistics)
        if self.stages is not None:
            gate["stages"] = self.stages
        if self.bound_fallback is not None:
            gate["bound_fallback"] = self.bound_fallback
        result = self._payload_around(
            {
                "evaluation": {"repeats_per_question": self.repeats},
                "round_stop": self.round_stop,
                "baseline_refresh_after_rounds": self.baseline_refresh_after_rounds,
            },
            gate,
        )
        if self.reflector_context is not None:
            result["reflector_context"] = self.reflector_context
        if self.context_max_bytes is not None:
            result["context_max_bytes"] = self.context_max_bytes
        return result

    @property
    def looks(self) -> tuple[int, ...]:
        return (self.repeats,)


def policy_class_for_payload(payload: object) -> type[CampaignPolicyV11]:
    """Select the native parser; damaged current payloads are still validated.

    Independent gate keys win before V11. Missing /15 keys never revive a
    retired constructor: V11 reports their structural defect instead.
    """
    gate = payload.get("gate") if isinstance(payload, Mapping) else None
    if isinstance(gate, Mapping) and any(
        key in gate for key in ("execution_noise", "verification", "statistics")
    ):
        return CampaignPolicyV12
    return CampaignPolicyV11


def comparable_policy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The policy as linked-run comparability sees it (spec §6.4).

    `sample_retries` and `call_retries` (I1) are both operational knobs a
    successor may change; neither is comparable.
    """

    return {
        key: value
        for key, value in payload.items()
        if key not in ("sample_retries", "call_retries")
    }


def admit_reflection_capacity(corpus_count: int, policy: CampaignPolicyV11) -> int:
    """Return the frozen record count or refuse values beyond protocol capacity."""

    if type(corpus_count) is not int or corpus_count < 1:
        raise ReflectionCapacityError("reflection_capacity:invalid_count")
    count = corpus_count * policy.repeats
    if count > 32768:
        raise ReflectionCapacityError(f"reflection_capacity:{count}")
    return count
