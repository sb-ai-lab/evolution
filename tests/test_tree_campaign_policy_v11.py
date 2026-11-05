"""`CampaignPolicyV11`: the `/15` campaign policy (family-14 implementation design, slice H1)."""

from __future__ import annotations

import copy
import math
from dataclasses import fields

import pytest

from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    PolicyError,
    policy_class_for_payload,
)
from evolution.tree.manifest.campaign_policy_help import (
    EXPECTED_V15,
    LEGACY_KEYS_V15,
    OBSERVE_FORBIDDEN_V15,
    OBSERVE_UNKNOWN_V15,
    PARAMETER_HELP_V15,
    ROUND_STOP_PARALLEL,
    SCHEMA_V15_NAME,
    UNKNOWN_V15,
    expected_text,
    help_for_v15,
    render_v15_parameter_table,
)
from tests.campaign_fakes import (
    v11_payload,
    v11_policy,
)

V15_PARAMETERS = (
    "campaign.gate.mode",
    "campaign.evaluation.repeats_per_question",
    "campaign.round_stop",
    "campaign.baseline_refresh_after_rounds",
    "campaign.gate.stability",
    "campaign.gate.focus.confidence",
    "campaign.gate.focus.margin",
    "campaign.gate.causal.confidence",
    "campaign.gate.causal.tolerance",
    "campaign.gate.fresh_parent",
    "campaign.gate.global",
)


# -- R4: the /15 texts are their own tables -------------------------------------


@pytest.mark.parametrize(
    ("code", "path", "mode", "section", "text"),
    [
        ("missing_key", "round_stop", None, None, EXPECTED_V15["round_stop"]),
        (
            "bad_type",
            "baseline_refresh_after_rounds",
            "statistical",
            None,
            EXPECTED_V15["baseline_refresh_after_rounds"],
        ),
        ("missing_key", "gate", "observe", None, EXPECTED_V15["gate"]),
        ("missing_key", "gate.focus", "observe", None, EXPECTED_V15["gate.focus"]),
        ("bad_value", "gate.focus.margin", "statistical", None, EXPECTED_V15["gate.focus.margin"]),
        ("bad_value", "gate.stability", None, None, EXPECTED_V15["gate.stability"]),
        ("unknown_key", "gate.zz", "statistical", "gate", UNKNOWN_V15["gate"]),
        ("unknown_key", "gate.zz", "observe", "gate", UNKNOWN_V15["gate"]),
        ("unknown_key", "gate.focus.zz", "statistical", "gate.focus", UNKNOWN_V15["gate.focus"]),
        (
            "unknown_key",
            "gate.focus.zz",
            "observe",
            "gate.focus",
            OBSERVE_UNKNOWN_V15["gate.focus"],
        ),
        (
            "unknown_key",
            "gate.focus.confidence",
            "observe",
            "gate.focus",
            OBSERVE_FORBIDDEN_V15["gate.focus.confidence"],
        ),
        ("unknown_key", "alpha", "statistical", "", LEGACY_KEYS_V15["alpha"]),
        (
            "unknown_key",
            "gate.regression_margin",
            None,
            "gate",
            LEGACY_KEYS_V15["gate.regression_margin"],
        ),
    ],
)
def test_v15_refusals_read_the_v15_tables(code, path, mode, section, text):
    assert expected_text(code, path, mode, section=section, schema=SCHEMA_V15_NAME) == text


def test_the_v15_texts_name_the_allowed_and_recommended_values():
    for key, phrase in (
        ("round_stop", "'first_accepted' is recommended"),
        ("round_stop", "'all_attempts'"),
        ("baseline_refresh_after_rounds", "3 is recommended"),
        ("baseline_refresh_after_rounds", "0 means no baseline refresh"),
        ("gate.stability", "'stable' is recommended"),
        ("gate.stability", "'unstable'"),
        ("gate.focus.margin", "0 is recommended"),
        ("gate.focus.margin", "finite number"),
    ):
        assert phrase in EXPECTED_V15[key], (key, phrase)
    assert "one Attempt at a time" in ROUND_STOP_PARALLEL
    assert "parallel_attempts" in ROUND_STOP_PARALLEL and "'all_attempts'" in ROUND_STOP_PARALLEL
    for text in LEGACY_KEYS_V15.values():
        assert "/15" in text and "/14" not in text
    # /15's observe gate admits `focus` (with `margin` only): no forbidden-section text.
    assert "gate.focus" not in OBSERVE_FORBIDDEN_V15
    assert "gate" not in OBSERVE_UNKNOWN_V15


# -- R4: /15 parameter help -------------------------------------------------------


def test_every_v15_parameter_has_complete_help():
    assert tuple(entry.path for entry in PARAMETER_HELP_V15) == V15_PARAMETERS
    for entry in PARAMETER_HELP_V15:
        for name in ("accepted", "units", "default", "scope", "effect", "example"):
            assert getattr(entry, name).strip(), (entry.path, name)
    assert help_for_v15("campaign.round_stop").example == "round_stop: first_accepted"
    with pytest.raises(KeyError):
        help_for_v15("campaign.alpha")


V15_NOT_PARAMETERS = frozenset(
    {"gate", "gate.provenance", "gate.provenance.kind", "gate.provenance.path"}
)


def test_every_v15_refusal_text_belongs_to_a_documented_parameter():
    documented = {entry.path for entry in PARAMETER_HELP_V15}
    texts = {*EXPECTED_V15, *OBSERVE_FORBIDDEN_V15, *OBSERVE_UNKNOWN_V15, *UNKNOWN_V15}
    assert V15_NOT_PARAMETERS <= texts, "stale exemption"
    for path in sorted(texts - V15_NOT_PARAMETERS):
        full = f"campaign.{path}"
        assert any(p == full or p.startswith(full + ".") for p in documented), path


def _text(entry):
    return " ".join(
        (
            entry.accepted,
            entry.units,
            entry.default,
            entry.scope,
            entry.effect,
            entry.example,
            *entry.notes,
        )
    )


def test_the_v15_help_says_what_the_design_requires():
    text = {entry.path: _text(entry) for entry in PARAMETER_HELP_V15}
    assert "does not check" in text["campaign.gate.stability"]
    assert "champion_not_contemporaneous" in text["campaign.gate.stability"]
    assert "worse" in text["campaign.gate.focus.margin"]
    assert "parallel_attempts: 1" in text["campaign.round_stop"]
    refresh = text["campaign.baseline_refresh_after_rounds"]
    assert "0 is the no-refresh mode" in refresh and "one full Champion measurement" in refresh
    tolerance = text["campaign.gate.causal.tolerance"]
    assert "noise_not_observed" in tolerance and "insufficient_calibration" not in tolerance
    assert "one tree at a time" in text["campaign.gate.fresh_parent"]
    for path, entry_text in text.items():
        assert "cohort" not in entry_text, path


def test_the_v14_help_and_table_are_unchanged():
    v15 = render_v15_parameter_table()
    for entry in PARAMETER_HELP_V15:
        assert f"`{entry.path}`" in v15


def test_evolve_help_shows_the_v15_settings():
    from typer.main import get_command

    from evolution.tree import cli as cli_module

    v15_help, separator, _ = cli_module.v15_help_epilog().partition("\n\nManifest /16 settings:\n")
    assert separator
    lines = v15_help.splitlines()
    assert lines[0] == "Manifest /15 settings:"
    for entry in PARAMETER_HELP_V15:
        assert f"{entry.path}: {entry.accepted} {entry.default}" in lines, entry.path
    assert lines[-1] == (
        "Full help: docs/manifest-v2.md, section 'Stable family keys (manifest /15)'."
    )
    evolve = get_command(cli_module.tree_app).commands["evolve"]  # type: ignore[attr-defined]
    assert evolve.epilog == cli_module.v15_help_epilog()
    assert not hasattr(cli_module, "v14_help_epilog")


# -- R2: the /15 campaign policy --------------------------------------------------


def _at(payload, dotted):
    *parents, leaf = dotted.split(".")
    node = payload
    for name in parents:
        node = node[name]
    return node, leaf


def setting(dotted, value):
    def mutate(payload):
        node, leaf = _at(payload, dotted)
        node[leaf] = value

    return mutate


def dropping(dotted):
    def mutate(payload):
        node, leaf = _at(payload, dotted)
        del node[leaf]

    return mutate


def refusal(payload):
    with pytest.raises(PolicyError) as caught:
        CampaignPolicyV11(payload)
    return caught.value.code, caught.value.path, caught.value.expected


def test_a_statistical_v11_policy_carries_the_four_keys():
    policy = v11_policy()
    assert type(policy) is CampaignPolicyV11
    assert (
        policy.round_stop,
        policy.baseline_refresh_after_rounds,
        policy.stability,
        policy.focus_margin,
    ) == ("first_accepted", 3, "stable", 0.0)
    assert (policy.focus_confidence, policy.gate_mode, policy.global_mode) == (
        0.9,
        "statistical",
        "point",
    )
    assert policy.fresh_parent is False and policy.declares_alpha is False
    assert all(hasattr(policy, field.name) for field in fields(policy))


def test_an_observe_v11_policy_keeps_focus_with_margin_only():
    policy = v11_policy(mode="observe", repeats=1, margin=-0.05)
    assert (policy.focus_confidence, policy.focus_margin) == (None, -0.05)
    payload = policy.to_payload()
    assert payload["gate"]["focus"] == {"margin": -0.05}
    assert "confidence" not in payload["gate"]["causal"]


def test_a_v11_payload_round_trips_and_selects_v11():
    policy = v11_policy(round_stop="all_attempts", refresh=0, stability="unstable", margin=0.02)
    payload = policy.to_payload()
    assert payload["round_stop"] == "all_attempts"
    assert payload["baseline_refresh_after_rounds"] == 0
    assert payload["gate"]["stability"] == "unstable"
    assert payload["gate"]["focus"] == {"confidence": 0.9, "margin": 0.02}
    assert policy_class_for_payload(payload) is CampaignPolicyV11
    assert CampaignPolicyV11.from_payload(payload) == policy
    assert CampaignPolicyV11.from_payload(payload).digest == policy.digest


@pytest.mark.parametrize(
    ("value", "stored"), [(0, 0.0), (-0.0, 0.0), (0.0, 0.0), (1, 1.0), (-0.25, -0.25)]
)
def test_equal_margins_share_one_spelling(value, stored):
    policy = v11_policy(margin=value)
    assert policy.focus_margin == stored and type(policy.focus_margin) is float
    assert math.copysign(1.0, policy.focus_margin) == math.copysign(1.0, stored)


def test_equal_values_share_a_digest_and_new_keys_move_it():
    base = v11_policy().digest
    assert v11_policy(margin=-0.0).digest == v11_policy(margin=0).digest == base
    for changed in (
        v11_policy(round_stop="all_attempts"),
        v11_policy(refresh=4),
        v11_policy(stability="unstable"),
        v11_policy(margin=0.01),
    ):
        assert changed.digest != base


MARGIN = EXPECTED_V15["gate.focus.margin"]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (True, "bad_type"),
        ("0", "bad_type"),
        (None, "bad_type"),
        (10**400, "bad_value"),
        (float("inf"), "bad_value"),
        (float("nan"), "bad_value"),
    ],
)
def test_margin_types_and_ranges(value, code):
    assert refusal(v11_payload(margin=value)) == (code, "gate.focus.margin", MARGIN)


STRUCTURE = [
    (
        "statistical",
        dropping("round_stop"),
        ("missing_key", "round_stop", EXPECTED_V15["round_stop"]),
    ),
    (
        "statistical",
        dropping("baseline_refresh_after_rounds"),
        (
            "missing_key",
            "baseline_refresh_after_rounds",
            EXPECTED_V15["baseline_refresh_after_rounds"],
        ),
    ),
    (
        "statistical",
        dropping("gate.stability"),
        ("missing_key", "gate.stability", EXPECTED_V15["gate.stability"]),
    ),
    (
        "statistical",
        dropping("gate.focus"),
        ("missing_key", "gate.focus", EXPECTED_V15["gate.focus"]),
    ),
    ("observe", dropping("gate.focus"), ("missing_key", "gate.focus", EXPECTED_V15["gate.focus"])),
    ("statistical", dropping("gate.focus.margin"), ("missing_key", "gate.focus.margin", MARGIN)),
    ("observe", dropping("gate.focus.margin"), ("missing_key", "gate.focus.margin", MARGIN)),
    (
        "statistical",
        dropping("gate.focus.confidence"),
        ("missing_key", "gate.focus.confidence", EXPECTED_V15["gate.focus.confidence"]),
    ),
    ("statistical", setting("gate.zz", 1), ("unknown_key", "gate.zz", UNKNOWN_V15["gate"])),
    ("observe", setting("gate.zz", 1), ("unknown_key", "gate.zz", UNKNOWN_V15["gate"])),
    (
        "statistical",
        setting("gate.focus.zz", 1),
        ("unknown_key", "gate.focus.zz", UNKNOWN_V15["gate.focus"]),
    ),
    (
        "observe",
        setting("gate.focus.zz", 1),
        ("unknown_key", "gate.focus.zz", OBSERVE_UNKNOWN_V15["gate.focus"]),
    ),
    (
        "observe",
        setting("gate.focus.confidence", 0.9),
        ("unknown_key", "gate.focus.confidence", OBSERVE_FORBIDDEN_V15["gate.focus.confidence"]),
    ),
    (
        "statistical",
        setting("alpha", {"budget": "0.05", "spend": {"3": "0.01"}}),
        ("unknown_key", "alpha", LEGACY_KEYS_V15["alpha"]),
    ),
    (
        "statistical",
        setting("gate.regression_margin", "0"),
        ("unknown_key", "gate.regression_margin", LEGACY_KEYS_V15["gate.regression_margin"]),
    ),
]


@pytest.mark.parametrize(("mode", "edit", "expected"), STRUCTURE)
def test_structure_refusals(mode, edit, expected):
    payload = v11_payload(mode=mode, repeats=1 if mode == "observe" else 3)
    edit(payload)
    assert refusal(payload) == expected


VALUES = [
    (setting("round_stop", "first"), ("bad_value", "round_stop", EXPECTED_V15["round_stop"])),
    (setting("round_stop", 3), ("bad_type", "round_stop", EXPECTED_V15["round_stop"])),
    (
        setting("baseline_refresh_after_rounds", -1),
        (
            "bad_value",
            "baseline_refresh_after_rounds",
            EXPECTED_V15["baseline_refresh_after_rounds"],
        ),
    ),
    (
        setting("baseline_refresh_after_rounds", True),
        (
            "bad_type",
            "baseline_refresh_after_rounds",
            EXPECTED_V15["baseline_refresh_after_rounds"],
        ),
    ),
    (
        setting("baseline_refresh_after_rounds", 1.5),
        (
            "bad_type",
            "baseline_refresh_after_rounds",
            EXPECTED_V15["baseline_refresh_after_rounds"],
        ),
    ),
    (
        setting("baseline_refresh_after_rounds", "3"),
        (
            "bad_type",
            "baseline_refresh_after_rounds",
            EXPECTED_V15["baseline_refresh_after_rounds"],
        ),
    ),
    (
        setting("gate.stability", "stabl"),
        ("bad_value", "gate.stability", EXPECTED_V15["gate.stability"]),
    ),
    (
        setting("gate.stability", None),
        ("bad_type", "gate.stability", EXPECTED_V15["gate.stability"]),
    ),
    (setting("gate.focus", 0), ("bad_type", "gate.focus", EXPECTED_V15["gate.focus"])),
]


@pytest.mark.parametrize(("edit", "expected"), VALUES)
def test_value_refusals(edit, expected):
    payload = v11_payload()
    edit(payload)
    assert refusal(payload) == expected


def test_refresh_zero_is_the_no_refresh_mode():
    assert v11_policy(refresh=0).baseline_refresh_after_rounds == 0


# -- R3: the one new combination refusal and its order ------------------------------


def test_first_accepted_refuses_parallel_attempts_above_one():
    payload = v11_payload(parallel_attempts=2, attempts_per_round=3)
    assert refusal(payload) == ("bad_value", "round_stop", ROUND_STOP_PARALLEL)
    payload["round_stop"] = "all_attempts"
    assert CampaignPolicyV11(payload).parallel_attempts == 2


def test_the_round_stop_refusal_precedes_the_sequential_rule():
    payload = v11_payload(parallel_attempts=2, attempts_per_round=3)
    payload["attempt_feedback"] = "sequential"
    assert refusal(payload) == ("bad_value", "round_stop", ROUND_STOP_PARALLEL)
    payload["round_stop"] = "all_attempts"
    assert refusal(payload) == ("bad_value", "attempt_feedback", None)


@pytest.mark.parametrize("feedback", ["none", "sequential"])
def test_first_accepted_admits_both_feedback_values(feedback):
    payload = v11_payload()
    payload["attempt_feedback"] = feedback
    assert CampaignPolicyV11(payload).attempt_feedback == feedback


@pytest.mark.parametrize(
    ("dotted", "value"),
    [
        ("round_stop", "zz-bad-value-37"),
        ("baseline_refresh_after_rounds", -37),
        ("gate.stability", "zz-bad-value-37"),
        ("gate.focus.margin", "zz-bad-value-37"),
    ],
)
def test_a_v15_refusal_never_repeats_the_value(dotted, value):
    payload = v11_payload()
    setting(dotted, value)(payload)
    code, path, expected = refusal(payload)
    assert expected is not None
    assert "37" not in expected and "zz-bad" not in expected and "37" not in path


def test_the_v11_payload_builder_is_not_validated():
    raw = v11_payload(round_stop=object())
    assert not isinstance(raw["round_stop"], str)
    assert copy.deepcopy(v11_payload()) == v11_payload()


@pytest.mark.parametrize("key,value", [("reflector_context", "r"), ("context_max_bytes", 100000)])
def test_v15_refuses_reflector_context_and_context_max_bytes(key, value):
    document = v11_payload()
    document[key] = value
    with pytest.raises(PolicyError) as caught:
        CampaignPolicyV11.from_payload(document)
    assert (caught.value.code, caught.value.path) == ("unknown_key", key)


def test_v15_editor_context_limit_stays_65536():
    document = v11_payload()
    document["editor_context"] = "x" * 65536
    policy = CampaignPolicyV11.from_payload(document)
    assert policy.reflector_operator_context == document["editor_context"]
    document["editor_context"] += "x"
    with pytest.raises(PolicyError) as caught:
        CampaignPolicyV11.from_payload(document)
    assert (caught.value.code, caught.value.path) == ("bad_value", "editor_context")
