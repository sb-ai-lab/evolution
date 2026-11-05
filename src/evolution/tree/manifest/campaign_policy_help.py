"""Fixed refusal texts and parameter help for the `/14` campaign policy.

Each `/14` refusal names the exact key path and a fixed "expected" text for
that path, and never repeats the invalid value. Every text here is a
constant, so no refusal can echo a manifest value. Keys are paths relative to
`campaign`, as `PolicyError.path` is; the texts name other keys by their full
manifest path.

Manifest `/15` has its own tables,
selected by `expected_text(..., schema=SCHEMA_V15_NAME)`: several `/14` texts
are wrong for `/15` (its gate has `stability` and a `focus` that both modes
require, and its legacy keys name `/15`), and the four new campaign keys name
their allowed values and the recommended one. `PARAMETER_HELP` stays the
seven `/14` settings, which `docs/manifest-v2.md` renders; `/15`'s eleven are
`PARAMETER_HELP_V15`.

Every refusal inside the two subtrees `/14` introduces, `campaign.evaluation`
and `campaign.gate` with all their sub-objects, has a text: a missing key, an
unknown key, a bad type and a bad value (maintainer decision S1 of the F2
plan, reading PDR-0022 Amendment (1) as covering the settings `/14`
introduces). The two legacy keys `/14` drops (G12) have a text too. A key
`/14` shares with `/13` outside those subtrees (`rounds`, `budget.*`,
`call_retries.*`,...) keeps its plain refusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

_FRACTION = "expected a finite fraction strictly between 0 and 1 (0.90 means 90%)"
_OBSERVE_NO_CONFIDENCE = (
    "expected no confidence when campaign.gate.mode is 'observe'; remove the field"
)

#: `missing_key`, `bad_type` and `bad_value` at a path inside the `/14`
#: subtrees, in any mode.
EXPECTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "evaluation": "expected an object with repeats_per_question",
        "evaluation.repeats_per_question": (
            "expected an integer, at least 2 when campaign.gate.mode is 'statistical' "
            "and at least 1 when it is 'observe'"
        ),
        "gate": (
            "expected an object with mode, provenance, fresh_parent and global, "
            "and also focus and causal when campaign.gate.mode is 'statistical'"
        ),
        "gate.mode": "expected exactly 'statistical' or 'observe'",
        "gate.focus": "expected an object with confidence when campaign.gate.mode is 'statistical'",
        "gate.focus.confidence": _FRACTION,
        "gate.causal": (
            "expected an object with confidence when campaign.gate.mode is 'statistical', "
            "and an optional tolerance"
        ),
        "gate.causal.confidence": _FRACTION,
        "gate.causal.tolerance": "expected 'measured_noise' or a finite number >= 0",
        "gate.provenance": (
            "expected an object with kind, and also path when campaign.gate.provenance.kind "
            "is 'metadata'"
        ),
        "gate.provenance.kind": "expected exactly 'reported', 'metadata' or 'none'",
        "gate.provenance.path": (
            "expected a JSON Pointer string starting with '/' "
            "when campaign.gate.provenance.kind is 'metadata'"
        ),
        "gate.fresh_parent": "expected true or false",
        "gate.global": "expected exactly 'off', 'point' or 'bound' (the /13 boolean is not converted)",
    }
)

#: `unknown_key` inside a `/14` section, keyed by the section that holds the
#: unknown key (a `statistical` gate, or a gate whose mode is not yet usable).
UNKNOWN: Final[Mapping[str, str]] = MappingProxyType(
    {
        "evaluation": "expected only repeats_per_question",
        "gate": "expected only mode, focus, causal, provenance, fresh_parent and global",
        "gate.focus": "expected only confidence",
        "gate.causal": "expected only confidence and tolerance",
        "gate.provenance": (
            "expected only kind, and also path when campaign.gate.provenance.kind is 'metadata'"
        ),
    }
)

#: `unknown_key` inside a section whose key set `observe` narrows.
OBSERVE_UNKNOWN: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gate": (
            "expected only mode, causal, provenance, fresh_parent and global "
            "when campaign.gate.mode is 'observe'"
        ),
        "gate.causal": "expected only tolerance when campaign.gate.mode is 'observe'",
    }
)

#: `unknown_key` at a key that `observe` forbids (design choice 4 of the F2 plan).
OBSERVE_FORBIDDEN: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gate.focus": "expected no focus section when campaign.gate.mode is 'observe'; remove it",
        "gate.focus.confidence": _OBSERVE_NO_CONFIDENCE,
        "gate.causal.confidence": _OBSERVE_NO_CONFIDENCE,
    }
)

#: `unknown_key` at a `/13` statistical authority that `/14` replaces (G12).
LEGACY_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "alpha": (
            "expected no alpha in /14; set campaign.gate.focus.confidence "
            "and campaign.gate.causal.confidence instead"
        ),
        "gate.regression_margin": (
            "expected no regression_margin in /14; set campaign.gate.causal.tolerance instead"
        ),
    }
)

#: `bad_value` at `gate.global` when an `observe` gate names a valid mode other than `off`.
OBSERVE_GLOBAL: Final = "expected 'off' when campaign.gate.mode is 'observe'"


#: The schema names the tables are keyed by. Literals, not imports:
#: `manifest_v2` imports this module through `campaign_policy`.
#: `tests/tree/manifest/test_manifest_v15.py` pins both against the loader's constants.
SCHEMA_V14_NAME: Final = "evolution-v2-manifest/14"
SCHEMA_V15_NAME: Final = "evolution-v2-manifest/15"
SCHEMA_V16_NAME: Final = "evolution-v2-manifest/16"

#: `/15`: `/14`'s texts with the gate,
#: the focus section and the four new keys stated for `/15`.
EXPECTED_V15: Final[Mapping[str, str]] = MappingProxyType(
    {
        **EXPECTED,
        "round_stop": "expected exactly 'first_accepted' or 'all_attempts'; 'first_accepted' is recommended",
        "baseline_refresh_after_rounds": (
            "expected an integer: a positive integer N refreshes the baseline every N rounds "
            "(3 is recommended); 0 means no baseline refresh"
        ),
        "gate": (
            "expected an object with mode, stability, focus, provenance, fresh_parent and "
            "global, and also causal when campaign.gate.mode is 'statistical'"
        ),
        "gate.stability": "expected exactly 'stable' or 'unstable'; 'stable' is recommended",
        "gate.focus": (
            "expected an object with margin, and also confidence when campaign.gate.mode "
            "is 'statistical'"
        ),
        "gate.focus.margin": "expected a finite number; 0 is recommended",
    }
)

UNKNOWN_V15: Final[Mapping[str, str]] = MappingProxyType(
    {
        **UNKNOWN,
        "gate": (
            "expected only mode, stability, focus, causal, provenance, fresh_parent and global"
        ),
        "gate.focus": "expected only confidence and margin",
    }
)

#: `/15`'s `observe` narrows `focus` to `margin` and `causal` to `tolerance`;
#: its gate admits the same keys in both modes.
OBSERVE_UNKNOWN_V15: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gate.focus": "expected only margin when campaign.gate.mode is 'observe'",
        "gate.causal": OBSERVE_UNKNOWN["gate.causal"],
    }
)

OBSERVE_FORBIDDEN_V15: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gate.focus.confidence": _OBSERVE_NO_CONFIDENCE,
        "gate.causal.confidence": _OBSERVE_NO_CONFIDENCE,
    }
)

LEGACY_KEYS_V15: Final[Mapping[str, str]] = MappingProxyType(
    {
        "alpha": (
            "expected no alpha in /15; set campaign.gate.focus.confidence "
            "and campaign.gate.causal.confidence instead"
        ),
        "gate.regression_margin": (
            "expected no regression_margin in /15; set campaign.gate.causal.tolerance, "
            "and campaign.gate.focus.margin for a minimum focus effect, instead"
        ),
    }
)

#: `bad_value at round_stop` for `first_accepted` with `parallel_attempts`
#: above 1, the one new combination refusal.
ROUND_STOP_PARALLEL: Final = (
    "expected campaign.parallel_attempts: 1 with 'first_accepted', which opens one Attempt "
    "at a time; set parallel_attempts to 1 or use 'all_attempts'"
)


# /16 recommendations are refusal guidance, never implicit parser defaults.
EXPECTED_V16: Final[Mapping[str, str]] = MappingProxyType(
    {
        **EXPECTED_V15,
        "evaluation.repeats_per_question": "expected an integer >= 1, at least 2 in statistical mode or with measured_noise; 1 is recommended for point mode",
        "baseline_refresh_after_rounds": "expected an integer >= 0; 0 is recommended",
        "gate": "expected mode, stability, focus, causal, provenance, fresh_parent, global, execution_noise and verification; stages is optional; confidence also requires statistics and permits optional bound_fallback",
        "gate.mode": "expected exactly 'point', 'confidence', 'unfiltered' or 'statistical'; 'point' is recommended",
        "gate.focus": "expected margin, and confidence in statistical or confidence mode; margin: 0 is recommended",
        "gate.causal": "expected tolerance, and confidence in statistical or confidence mode; tolerance: 0 is recommended",
        "gate.causal.tolerance": "expected a finite number >= 0, or 'measured_noise' in statistical, point or confidence mode with repeats_per_question >= 2; 0 is recommended",
        "gate.stages": "expected exactly 'single' or 'focus_first'; optional, absent means 'single'; 'focus_first' is recommended",
        "gate.bound_fallback": "expected 'point' only in confidence mode; optional, with no fallback when absent",
        "gate.execution_noise": "expected a tagged object; {kind: negligible} is recommended",
        "gate.execution_noise.kind": "expected exactly 'negligible', 'bounded' or 'observed'; 'negligible' is recommended",
        "gate.execution_noise.allowance": "expected a finite number >= 0 in primary-score units",
        "gate.execution_noise.source": "expected nonempty public provenance text of at most 1024 characters",
        "gate.verification": "expected exactly 'reuse' or 'fresh'; 'reuse' is recommended",
        "gate.statistics": "expected method and unit only in confidence mode; case is recommended",
        "gate.statistics.method": "expected exactly 'bounded-weighted-mean/1'",
        "gate.statistics.unit": "expected exactly 'case' or 'group'; 'case' is recommended",
        "gate.global": "expected 'off' or 'point', or 'bound' in confidence mode; unfiltered requires 'off'; 'point' is recommended",
    }
)


def _tables(
    schema: str,
) -> tuple[
    Mapping[str, str], Mapping[str, str], Mapping[str, str], Mapping[str, str], Mapping[str, str]
]:
    """`(expected, unknown, observe_unknown, observe_forbidden, legacy)` for `schema`."""

    if schema == SCHEMA_V15_NAME:
        return (
            EXPECTED_V15,
            UNKNOWN_V15,
            OBSERVE_UNKNOWN_V15,
            OBSERVE_FORBIDDEN_V15,
            LEGACY_KEYS_V15,
        )
    return EXPECTED, UNKNOWN, OBSERVE_UNKNOWN, OBSERVE_FORBIDDEN, LEGACY_KEYS


def expected_text(
    code: str,
    path: str,
    mode: str | None,
    *,
    section: str | None = None,
    schema: str = SCHEMA_V14_NAME,
) -> str | None:
    """The fixed text for a `/14` or `/15` refusal, or `None` for a shared key.

    `schema` picks the tables (`/15`'s for `SCHEMA_V15_NAME`, `/14`'s for any
    other value, so every existing caller reads exactly what it read before).
    `mode` is the payload's `gate.mode` when it is one of the two modes, else
    `None`. For `unknown_key`, `section` is the dotted path of the object that
    holds the unknown key (`""` for the top level), as the structure pass
    knows it; without it the text is `None`. The section, not the path, picks
    the text, so an unknown key whose own name contains a dot still gets its
    section's text, and a top-level key spelled like a nested path gets none.
    """

    if schema == SCHEMA_V16_NAME:
        if code == "unknown_key":
            if path == "gate.statistics" and mode != "confidence":
                return "expected no statistics section outside confidence mode; remove statistics"
            if path.startswith(("gate.focus.", "gate.causal.")):
                name = "margin" if path.startswith("gate.focus.") else "tolerance"
                if mode in ("statistical", "confidence"):
                    return f"expected only {name} and confidence in statistical or confidence mode"
                return f"expected only {name} in point or unfiltered mode"
            return "expected only the explicit /16 fields allowed in this mode"
        return EXPECTED_V16.get(path)
    expected, unknown, observe_unknown, observe_forbidden, legacy = _tables(schema)
    if code != "unknown_key":
        return expected.get(path)
    if section is None:
        return None
    key = path[len(section) + 1 :] if section else path
    if "." not in key:
        if path in legacy:
            return legacy[path]
        if mode == "observe" and path in observe_forbidden:
            return observe_forbidden[path]
    if mode == "observe" and section in observe_unknown:
        return observe_unknown[section]
    return unknown.get(section)


@dataclass(frozen=True)
class ParameterHelp:
    """Help for one `/14` setting."""

    path: str
    accepted: str
    units: str
    default: str
    scope: str
    effect: str
    example: str
    notes: tuple[str, ...] = ()


PARAMETER_HELP: Final[tuple[ParameterHelp, ...]] = (
    ParameterHelp(
        path="campaign.gate.mode",
        accepted="Exactly 'statistical' or 'observe'; case matters.",
        units="None.",
        default="Required; no default.",
        scope="The whole campaign, fixed when it is created.",
        effect=(
            "'statistical' enforces empirical confidence bounds on every Candidate and, through "
            "campaign.gate.global, on every finalist. 'observe' runs the same measurement, final "
            "cohort and Promotion pipeline with point diagnostics only and filters nothing "
            "numerically; critical, completeness and attestation checks still apply."
        ),
        example="mode: statistical",
        notes=(
            "'observe' can promote a worse Candidate. Use it only to check the pipeline, never "
            "as evidence of quality.",
            "'observe' results permanently carry observe_gate_not_enforced and "
            "uncalibrated_point_estimates.",
        ),
    ),
    ParameterHelp(
        path="campaign.evaluation.repeats_per_question",
        accepted=(
            "An integer, not a boolean: at least 2 in 'statistical' and at least 1 in 'observe'."
        ),
        units="Executions per measured question and tree.",
        default="Required; no default.",
        scope=(
            "Every Candidate and fresh Parent measurement, and every member of the final cohort."
        ),
        effect=(
            "R, the final count. 'statistical' may reject early at 3, 5 and 10 repeats when "
            "they are below R, and accepts only at R. 'observe' evaluates once, at R."
        ),
        example="repeats_per_question: 10",
        notes=(
            "Each early check uses a two-sided focus interval at the focus confidence. If every "
            "early upper end had its nominal coverage, K early checks would reject a good "
            "Candidate with probability at most K * (1 - confidence) / 2. The intervals are "
            "empirical and do not guarantee that coverage, least of all at few repeats, so this "
            "is a design figure, not a bound or a guarantee.",
            "'observe' at 1 is the smoke profile and gives no confidence guarantee.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.focus.confidence",
        accepted=(
            "A finite number strictly between 0 and 1, not a boolean or a quoted string. "
            "Required in 'statistical'; refused in 'observe'."
        ),
        units="A fraction: 0.90 means 90%; 90 is refused.",
        default="Required in 'statistical'; no default.",
        scope=(
            "Each comparison on its own; not divided across Attempts or rounds, and no "
            "campaign-wide guarantee."
        ),
        effect=(
            "The one-sided confidence of the final test that the focus effect is positive, and "
            "the two-sided confidence of each early focus interval. Higher makes acceptance "
            "harder and early rejection less aggressive."
        ),
        example="confidence: 0.90",
        notes=(
            "The intervals are empirical (a percentile bootstrap); the nominal confidence is "
            "not a proven finite-sample guarantee.",
            "Under global: bound, a finalist's cohort bound uses the larger of the two "
            "confidences, or this one alone when the causal scope is empty.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.causal.confidence",
        accepted=(
            "A finite number strictly between 0 and 1, not a boolean or a quoted string. "
            "Required in 'statistical'; refused in 'observe'."
        ),
        units="A fraction: 0.90 means 90%; 90 is refused.",
        default="Required in 'statistical'; no default.",
        scope=(
            "Each comparison on its own; not divided across Attempts or rounds, and no "
            "campaign-wide guarantee."
        ),
        effect=(
            "The one-sided confidence of the test that the causal lower bound is at least minus "
            "the tolerance. Higher is stricter at a fixed tolerance."
        ),
        example="confidence: 0.90",
        notes=("It does not change the measured noise allowance, whose level is fixed at 0.995.",),
    ),
    ParameterHelp(
        path="campaign.gate.causal.tolerance",
        accepted=(
            "Exactly 'measured_noise', or a finite number at least 0. A boolean, null, a quoted "
            "number or another word such as 'zero' or 'off' is refused."
        ),
        units=(
            "The primary metric's units, with improvement positive. On a [0, 1] metric, 0.01 is "
            "one percentage point."
        ),
        default="Optional; 'measured_noise' when omitted.",
        scope="The causal cases (C) of every Candidate.",
        effect=(
            "'statistical' requires the causal lower bound to be at least minus this allowance. "
            "measured_noise derives the allowance from the Parent's own repeat noise, before "
            "any causal decision. A number skips that calibration only; the comparison and its "
            "bounds still run. A numeric value of 0 allows no negative lower bound "
            "(L_C >= 0); a positive value allows a decrease of that size (L_C >= -tolerance)."
        ),
        example="tolerance: measured_noise",
        notes=(
            "0 does not switch the check off, and no value does; only an empty causal scope "
            "skips it.",
            "The allowance is absolute, not a multiplier of the noise.",
            "There is no upper limit without a declared metric range. A value as large as the "
            "metric's whole range (1 on a [0, 1] metric) permits any loss; a very large value "
            "makes the protection ineffective.",
            "A numeric example: tolerance: 0.01 allows a causal decrease of up to 0.01.",
            "A deterministic target gives a degenerate measured allowance and closes every "
            "'statistical' Attempt as insufficient_calibration; give it a numeric tolerance.",
            "measured_noise needs at least two Parent observations per causal case. In "
            "'observe' at R = 1 with fresh_parent: true, a second Parent observation is "
            "scheduled on each causal case.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.fresh_parent",
        accepted="true or false.",
        units="None.",
        default="Required, as in /13; no default.",
        scope="Every Attempt.",
        effect=(
            "true measures one Parent per Attempt over the focus and causal cases, interleaved "
            "with the Candidate, and uses it for both comparisons and the noise allowance. "
            "false uses the stored baseline rows."
        ),
        example="fresh_parent: true",
        notes=(
            "true costs extra target calls: one Parent execution per focus and causal case and "
            "repeat.",
            "false does not control for drift between the stored baseline and the Candidate, so "
            "every result "
            "with causal cases carries causal_parent_not_contemporaneous.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.global",
        accepted=(
            "Exactly 'off', 'point' or 'bound'. The /13 boolean is refused, not converted. "
            "'observe' requires 'off'."
        ),
        units="None.",
        default="Required; no default.",
        scope="Each finalist against the incumbent Champion, in the fresh final cohort.",
        effect=(
            "'off' records the contrast without filtering. 'point' requires the finalist's "
            "fresh full point score strictly above the Champion's. 'bound' requires the "
            "one-sided lower bound of the difference strictly above 0."
        ),
        example="global: point",
        notes=(
            "Every mode measures the complete cohort of the Champion and each finalist, for the "
            "report and for the next baseline.",
            "Equality is not eligible under 'point' or 'bound'.",
            "A neutral finalist passes a noisy 'point' comparison about half the time.",
            "'bound' is empirical, like the gate's intervals.",
            "The overall change beside the 'statistical' Candidate gate is a diagnostic; only "
            "this setting filters the final cohort.",
        ),
    ),
)

_HELP_BY_PATH: Final[Mapping[str, ParameterHelp]] = MappingProxyType(
    {entry.path: entry for entry in PARAMETER_HELP}
)


def help_for(path: str) -> ParameterHelp:
    """The help for a full manifest path such as `campaign.gate.mode`; `KeyError` otherwise."""

    return _HELP_BY_PATH[path]


PARAMETER_HELP_V15: Final[tuple[ParameterHelp, ...]] = (
    ParameterHelp(
        path="campaign.gate.mode",
        accepted="Exactly 'statistical' or 'observe'; case matters.",
        units="None.",
        default="Required; no default.",
        scope="The whole campaign, fixed when it is created.",
        effect=(
            "'statistical' enforces empirical confidence bounds on every Candidate and, through "
            "campaign.gate.global, on every finalist's final measurement. 'observe' runs the "
            "same measurement, final measurement and Promotion pipeline with point diagnostics "
            "only and filters nothing numerically; critical, completeness and attestation checks "
            "still apply."
        ),
        example="mode: statistical",
        notes=_HELP_BY_PATH["campaign.gate.mode"].notes,
    ),
    ParameterHelp(
        path="campaign.evaluation.repeats_per_question",
        accepted=(
            "An integer, not a boolean: at least 2 in 'statistical' and at least 1 in 'observe'."
        ),
        units="Executions per measured question and tree.",
        default="Required; no default.",
        scope=(
            "Every Candidate and fresh Parent measurement, every final measurement and every "
            "baseline refresh."
        ),
        effect=(
            "R, the final count. 'statistical' may reject early at 3, 5 and 10 repeats when "
            "they are below R, and accepts only at R. 'observe' evaluates once, at R. A "
            "comparison with no within-case variation in R repeats is decided on its point."
        ),
        example="repeats_per_question: 3",
        notes=_HELP_BY_PATH["campaign.evaluation.repeats_per_question"].notes,
    ),
    ParameterHelp(
        path="campaign.round_stop",
        accepted="Exactly 'first_accepted' or 'all_attempts'.",
        units="None.",
        default="Required; no default. 'first_accepted' is recommended.",
        scope="Every round.",
        effect=(
            "'first_accepted' opens one Attempt at a time, decides each at once, and closes the "
            "round at the first finalist accepted on its final measurement. 'all_attempts' runs "
            "every Attempt of the round and then measures its finalists together."
        ),
        example="round_stop: first_accepted",
        notes=(
            "'first_accepted' requires parallel_attempts: 1; any other value is refused.",
            "With attempt_feedback: none, the Attempts of a 'first_accepted' round learn nothing "
            "from each other.",
        ),
    ),
    ParameterHelp(
        path="campaign.baseline_refresh_after_rounds",
        accepted="An integer of at least 0, not a boolean or a quoted string.",
        units="Rounds.",
        default="Required; no default. 3 is recommended.",
        scope="The stored Champion measurement, in every cell and both modes.",
        effect=(
            "A positive N measures the Champion again on the full corpus after N rounds closed "
            "on the same stored measurement, and that measurement replaces it."
        ),
        example="baseline_refresh_after_rounds: 3",
        notes=(
            "0 is the no-refresh mode: the baseline changes only through a Promotion or another "
            "new baseline.",
            "A refresh costs one full Champion measurement whether or not anything changed.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.stability",
        accepted="Exactly 'stable' or 'unstable'.",
        units="None.",
        default="Required; no default. 'stable' is recommended.",
        scope="Every final measurement of the campaign.",
        effect=(
            "'stable' compares each finalist's fresh full-corpus measurement with the stored "
            "Champion measurement, so the Champion needs no Target instance of its own. "
            "'unstable' measures the Champion again in every final measurement."
        ),
        example="stability: stable",
        notes=(
            "It is the operator's declaration that the model, the judge and the Target keep the "
            "same behavior over the campaign; Evolution does not check it.",
            "Every 'stable' result carries champion_not_contemporaneous.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.focus.confidence",
        accepted=_HELP_BY_PATH["campaign.gate.focus.confidence"].accepted,
        units=_HELP_BY_PATH["campaign.gate.focus.confidence"].units,
        default="Required in 'statistical'; no default.",
        scope=_HELP_BY_PATH["campaign.gate.focus.confidence"].scope,
        effect=(
            "The one-sided confidence of the final test that the focus effect exceeds "
            "campaign.gate.focus.margin, and the two-sided confidence of each early focus "
            "interval. Higher makes acceptance harder and early rejection less aggressive."
        ),
        example="confidence: 0.90",
        notes=(
            "The intervals are empirical (a percentile bootstrap); the nominal confidence is "
            "not a proven finite-sample guarantee.",
            "Under global: bound, a finalist's final-measurement bound uses the larger of the "
            "two confidences, or this one alone when the causal scope is empty.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.focus.margin",
        accepted="A finite number, not a boolean or a quoted string. Required in both modes.",
        units="The primary metric's units, with improvement positive.",
        default="Required; no default. 0 is recommended.",
        scope="The focus comparison of every Candidate.",
        effect=(
            "'statistical' accepts the focus effect only when its lower bound exceeds the "
            "margin, and an early look rejects when the upper end is at most the margin. "
            "'observe' uses it for the would_pass_point diagnostic."
        ),
        example="margin: 0",
        notes=(
            "A negative margin can promote an edit that made the focus worse.",
            "Non-inferiority on the causal cases stays campaign.gate.causal.tolerance.",
        ),
    ),
    _HELP_BY_PATH["campaign.gate.causal.confidence"],
    ParameterHelp(
        path="campaign.gate.causal.tolerance",
        accepted=_HELP_BY_PATH["campaign.gate.causal.tolerance"].accepted,
        units=_HELP_BY_PATH["campaign.gate.causal.tolerance"].units,
        default="Optional; 'measured_noise' when omitted.",
        scope="The causal cases (C) of every Candidate.",
        effect=_HELP_BY_PATH["campaign.gate.causal.tolerance"].effect,
        example="tolerance: measured_noise",
        notes=(
            "0 does not switch the check off, and no value does; only an empty causal scope "
            "skips it.",
            "The allowance is absolute, not a multiplier of the noise.",
            "There is no upper limit without a declared metric range. A value as large as the "
            "metric's whole range (1 on a [0, 1] metric) permits any loss; a very large value "
            "makes the protection ineffective.",
            "A Parent with no observed variation gives a measured allowance of exactly 0 and the "
            "caveat noise_not_observed; it does not close the Attempt.",
            "measured_noise needs at least two Parent observations per causal case.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.fresh_parent",
        accepted="true or false.",
        units="None.",
        default="Required; no default.",
        scope="Every Attempt.",
        effect=_HELP_BY_PATH["campaign.gate.fresh_parent"].effect,
        example="fresh_parent: false",
        notes=(
            "true costs extra target calls: one Parent execution per focus and causal case and "
            "repeat.",
            "false does not control for drift between the stored baseline and the Candidate, so "
            "every result with causal cases carries causal_parent_not_contemporaneous.",
            "false with stability: stable, round_stop: first_accepted and parallel_attempts: 1 "
            "loads one tree at a time.",
        ),
    ),
    ParameterHelp(
        path="campaign.gate.global",
        accepted=(
            "Exactly 'off', 'point' or 'bound'. A boolean is refused, not converted. "
            "'observe' requires 'off'."
        ),
        units="None.",
        default="Required; no default.",
        scope="Each finalist against the Champion, in its final measurement.",
        effect=(
            "'off' records the contrast without filtering. 'point' requires the finalist's "
            "fresh full point score strictly above the Champion's. 'bound' requires the "
            "one-sided lower bound of the difference strictly above 0."
        ),
        example="global: point",
        notes=(
            "Under stability: stable the Champion side is the stored Champion measurement; "
            "under 'unstable' it is measured again.",
            "Equality is not accepted under 'point' or 'bound'.",
            "A neutral finalist passes a noisy 'point' comparison about half the time.",
            "'bound' is empirical, like the gate's intervals.",
        ),
    ),
)

_HELP_V15_BY_PATH: Final[Mapping[str, ParameterHelp]] = MappingProxyType(
    {entry.path: entry for entry in PARAMETER_HELP_V15}
)


def help_for_v15(path: str) -> ParameterHelp:
    """The `/15` help for a full manifest path; `KeyError` otherwise."""

    return _HELP_V15_BY_PATH[path]


_TABLE_COLUMNS: Final = ("Setting", "Accepted", "Units", "Default", "Scope", "Effect", "Example")


def _cell(text: str) -> str:
    """One table cell: a single line with no bare pipe."""

    return " ".join(text.split()).replace("|", "\\|")


def _render(entries: tuple[ParameterHelp, ...]) -> str:
    """One table row per entry, then one notes list per entry that has notes."""

    lines = [
        "| " + " | ".join(_TABLE_COLUMNS) + " |",
        "|" + "|".join("---" for _ in _TABLE_COLUMNS) + "|",
    ]
    for entry in entries:
        cells = (
            f"`{entry.path}`",
            entry.accepted,
            entry.units,
            entry.default,
            entry.scope,
            entry.effect,
            f"`{entry.example}`",
        )
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    for entry in entries:
        if not entry.notes:
            continue
        lines.append("")
        lines.append(f"Notes for `{entry.path}`:")
        lines.append("")
        lines.extend(f"- {_cell(note)}" for note in entry.notes)
    return "\n".join(lines)


def render_v14_parameter_table() -> str:
    """The `/14` parameter reference as Markdown.

    `docs/manifest-v2.md` pastes this text between its `v14-parameters` markers,
    and `test_the_v14_parameter_table_is_generated_from_parameter_help` pins it,
    so the page and `evolve --help` read one source.
    """

    return _render(PARAMETER_HELP)


def render_v15_parameter_table() -> str:
    """The `/15` parameter reference as Markdown; H12 pastes it into the manual."""

    return _render(PARAMETER_HELP_V15)
