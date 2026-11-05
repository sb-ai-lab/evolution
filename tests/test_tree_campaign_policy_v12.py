"""The /16 acceptance choices are explicit and independent of repeat count."""

from __future__ import annotations

import itertools
import math

import pytest

from evolution.tree import campaign_policy as policies
from tests.family15_fakes import v16_document


def payload(mode="point"):
    result = v16_document()["campaign"]
    gate = result["gate"]
    gate["mode"] = mode
    if mode in ("statistical", "confidence"):
        gate["focus"]["confidence"] = 0.95
        gate["causal"]["confidence"] = 0.9
    if mode == "statistical":
        result["evaluation"]["repeats_per_question"] = 10
    if mode == "confidence":
        gate["statistics"] = {"method": "bounded-weighted-mean/1", "unit": "case"}
        gate["global"] = "bound"
    if mode == "unfiltered":
        gate["global"] = "off"
    return result


def parse(document):
    assert hasattr(policies, "CampaignPolicyV12"), "missing independent acceptance policy"
    return policies.CampaignPolicyV12.from_payload(document)


@pytest.mark.parametrize(
    "mode,repeats,noise,verification,stability,stop",
    tuple(
        itertools.product(
            ("point", "confidence", "unfiltered"),
            (1, 3),
            (
                {"kind": "negligible"},
                {"kind": "observed"},
                {"kind": "bounded", "allowance": 0.1, "source": "External calibration"},
            ),
            ("reuse", "fresh"),
            ("stable", "unstable"),
            ("first_accepted", "all_attempts"),
        )
    ),
)
def test_independent_choices_round_trip(mode, repeats, noise, verification, stability, stop):
    document = payload(mode)
    document["evaluation"]["repeats_per_question"] = repeats
    document["gate"].update(execution_noise=noise, verification=verification, stability=stability)
    document.update(round_stop=stop)
    policy = parse(document)
    assert policy.to_payload() == document
    assert policy.repeats == repeats
    assert policy.looks == (repeats,)
    assert policies.policy_class_for_payload(document) is policies.CampaignPolicyV12
    assert parse(policy.to_payload()).digest == policy.digest


REQUIRED = (
    "round_stop",
    "baseline_refresh_after_rounds",
    "evaluation.repeats_per_question",
    "gate.mode",
    "gate.stability",
    "gate.focus",
    "gate.focus.margin",
    "gate.causal",
    "gate.causal.tolerance",
    "gate.execution_noise",
    "gate.execution_noise.kind",
    "gate.verification",
    "gate.provenance",
    "gate.fresh_parent",
    "gate.global",
)


def change(document, path, value=None, *, remove=False):
    parts = path.split(".")
    section = document
    for part in parts[:-1]:
        section = section[part]
    if remove:
        del section[parts[-1]]
    else:
        section[parts[-1]] = value


@pytest.mark.parametrize("path", REQUIRED)
def test_new_policy_requires_explicit_fields_with_help(path):
    document = payload()
    change(document, path, remove=True)
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("missing_key", path)
    assert caught.value.expected


@pytest.mark.parametrize(
    "mode,path",
    [
        ("confidence", "gate.focus.confidence"),
        ("confidence", "gate.causal.confidence"),
        ("confidence", "gate.statistics"),
        ("confidence", "gate.statistics.method"),
        ("confidence", "gate.statistics.unit"),
        ("statistical", "gate.focus.confidence"),
        ("statistical", "gate.causal.confidence"),
    ],
)
def test_interval_modes_require_their_explicit_fields(mode, path):
    document = payload(mode)
    change(document, path, remove=True)
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("missing_key", path)
    assert caught.value.expected


@pytest.mark.parametrize(
    "mode,path,value",
    [
        ("point", "gate.mode", "observe"),
        ("point", "evaluation.repeats_per_question", True),
        ("point", "evaluation.repeats_per_question", 0),
        ("point", "evaluation.repeats_per_question", 1.5),
        ("point", "gate.focus.margin", math.inf),
        ("point", "gate.causal.tolerance", "measured_noise"),
        ("point", "gate.causal.tolerance", -0.1),
        ("point", "gate.causal.tolerance", math.nan),
        ("point", "gate.global", "bound"),
        ("unfiltered", "gate.global", "point"),
        ("confidence", "gate.focus.confidence", 0),
        ("confidence", "gate.causal.confidence", 1),
        ("confidence", "gate.focus.confidence", math.inf),
        ("confidence", "gate.statistics.method", "bootstrap"),
        ("confidence", "gate.statistics.unit", "repeat"),
        ("statistical", "gate.focus.confidence", 0),
        ("statistical", "gate.causal.confidence", 1),
        ("statistical", "gate.global", "bound"),
        ("point", "gate.execution_noise.kind", "measured_noise"),
        ("point", "gate.verification", "auto"),
        ("point", "gate.focus.margin", True),
    ],
)
def test_invalid_values_name_only_the_exact_path(mode, path, value):
    document = payload(mode)
    change(document, path, value)
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert caught.value.path == path
    assert caught.value.expected


@pytest.mark.parametrize("mode", ("point", "unfiltered"))
@pytest.mark.parametrize(
    "path,value",
    [
        ("gate.focus.confidence", 0.95),
        ("gate.causal.confidence", 0.95),
        ("gate.statistics", {"method": "bounded-weighted-mean/1", "unit": "case"}),
    ],
)
def test_mode_only_fields_are_forbidden(mode, path, value):
    document = payload(mode)
    change(document, path, value)
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("unknown_key", path)


@pytest.mark.parametrize(
    "path",
    (
        "extra",
        "promotion",
        "gate.extra",
        "evaluation.extra",
        "gate.focus.extra",
        "gate.causal.extra",
        "gate.execution_noise.extra",
        "gate.statistics.extra",
    ),
)
def test_objects_have_closed_key_sets(path):
    document = payload("confidence")
    change(document, path, 3)
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("unknown_key", path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowance", -0.1),
        ("allowance", math.inf),
        ("allowance", True),
        ("source", ""),
        ("source", "  "),
        ("source", "x" * 1025),
        ("source", 4),
    ],
)
def test_bounded_noise_has_finite_allowance_and_bounded_public_source(field, value):
    document = payload()
    document["gate"]["execution_noise"] = {
        "kind": "bounded",
        "allowance": 0.1,
        "source": "Calibration",
    }
    document["gate"]["execution_noise"][field] = value
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert caught.value.path == f"gate.execution_noise.{field}"


@pytest.mark.parametrize("kind", ("negligible", "observed"))
@pytest.mark.parametrize("field,value", (("allowance", 0), ("source", "Calibration")))
def test_only_bounded_noise_has_allowance_and_source(kind, field, value):
    document = payload()
    document["gate"]["execution_noise"] = {"kind": kind, field: value}
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == (
        "unknown_key",
        f"gate.execution_noise.{field}",
    )


def test_negative_focus_margin_group_unit_and_parallel_all_attempts_are_valid():
    document = payload("confidence")
    document["gate"]["focus"]["margin"] = -0.25
    document["gate"]["statistics"]["unit"] = "group"
    document.update(
        round_stop="all_attempts",
        attempts_per_round=2,
        parallel_attempts=2,
        attempt_feedback="none",
    )
    assert parse(document).to_payload() == document
    document["round_stop"] = "first_accepted"
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert caught.value.path == "round_stop"


def test_noise_source_is_bound_and_existing_secret_screen_names_its_path():
    from evolution.tree.campaign import secret_location

    document = payload()
    document["gate"]["execution_noise"] = {
        "kind": "bounded",
        "allowance": 0.1,
        "source": "Calibration PRIVATE-CANARY",
    }
    policy = parse(document)
    assert (
        secret_location({"payload": {"policy": policy.to_payload()}}, ["PRIVATE-CANARY"])
        == "policy.gate.execution_noise.source"
    )


@pytest.mark.parametrize(
    "mode,global_mode",
    [
        ("point", "off"),
        ("point", "point"),
        ("statistical", "off"),
        ("statistical", "point"),
        ("confidence", "off"),
        ("confidence", "point"),
        ("confidence", "bound"),
        ("unfiltered", "off"),
    ],
)
@pytest.mark.parametrize("fresh_parent", (False, True))
def test_global_choices_and_fresh_parent_are_independent(mode, global_mode, fresh_parent):
    document = payload(mode)
    document["gate"].update({"global": global_mode, "fresh_parent": fresh_parent})
    assert parse(document).to_payload() == document


@pytest.mark.parametrize("field", ("allowance", "source"))
def test_bounded_noise_requires_both_explicit_fields(field):
    document = payload()
    document["gate"]["execution_noise"] = {
        "kind": "bounded",
        "allowance": 0,
        "source": "Calibration",
    }
    del document["gate"]["execution_noise"][field]
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == (
        "missing_key",
        f"gate.execution_noise.{field}",
    )
    assert caught.value.expected


def test_source_limit_is_characters_and_inclusive_and_policy_has_no_mutable_aliases():
    document = payload("confidence")
    document["gate"]["execution_noise"] = {"kind": "bounded", "allowance": 0, "source": "é" * 1024}
    policy = parse(document)
    original = policy.to_payload()
    document["gate"]["execution_noise"]["source"] = "changed"
    document["gate"]["statistics"]["unit"] = "group"
    emitted = policy.to_payload()
    emitted["gate"]["execution_noise"]["source"] = "changed again"
    assert policy.to_payload() == original
    with pytest.raises(TypeError):
        policy.execution_noise["source"] = "mutate"


@pytest.mark.parametrize(
    "path",
    ("evaluation", "gate", "gate.focus", "gate.causal", "gate.statistics", "gate.execution_noise"),
)
def test_new_sections_require_objects(path):
    document = payload("confidence")
    change(document, path, [])
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("bad_type", path)


@pytest.mark.parametrize("mode", ("point", "unfiltered", "statistical"))
def test_forbidden_statistics_is_refused_at_section_even_when_incomplete(mode):
    document = payload(mode)
    document["gate"]["statistics"] = {}
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("unknown_key", "gate.statistics")
    assert "statistics" in caught.value.expected


@pytest.mark.parametrize(
    "mode,digest",
    [
        ("point", "d1b0f82021dd439eac4e4b6a87786c070abc5be65100575890b47a2b9956602b"),
        ("confidence", "ca5a8fc44bf4851a204fb9b2fb7748aaf1920d5ca1e242db514138a142cabc3a"),
    ],
)
def test_absent_gate_options_keep_baseline_policy_digests(mode, digest):
    # Captured at 85950dedf before admitting the optional b10 gate choices.
    policy = parse(payload(mode))
    assert policy.digest == digest
    assert policy.bound_fallback is None
    assert "bound_fallback" not in policy.to_payload()["gate"]
    assert type(policy.causal_tolerance) is float


@pytest.mark.parametrize("repeats", (2, 10))
@pytest.mark.parametrize("fresh_parent", (False, True))
@pytest.mark.parametrize(
    "noise",
    [
        {"kind": "negligible"},
        {"kind": "observed"},
        {"kind": "bounded", "allowance": 0.1, "source": "Calibration"},
    ],
)
def test_statistical_is_one_fixed_stage_without_a_statistics_block(repeats, fresh_parent, noise):
    document = payload("statistical")
    document["evaluation"]["repeats_per_question"] = repeats
    document["gate"].update(fresh_parent=fresh_parent, execution_noise=noise)
    policy = parse(document)
    assert policy.gate_mode == "statistical"
    assert policy.focus_confidence == 0.95
    assert policy.causal_confidence == 0.9
    assert policy.statistics is None
    assert policy.looks == (repeats,)
    assert policy.to_payload() == document
    assert parse(policy.to_payload()).digest == policy.digest


@pytest.mark.parametrize("mode", ("statistical", "point", "confidence"))
@pytest.mark.parametrize("repeats", (2, 10))
def test_measured_noise_is_an_explicit_tolerance_with_repeats(mode, repeats):
    document = payload(mode)
    document["evaluation"]["repeats_per_question"] = repeats
    document["gate"]["causal"]["tolerance"] = "measured_noise"
    policy = parse(document)
    assert policy.causal_tolerance == "measured_noise"
    assert policy.to_payload() == document
    assert parse(policy.to_payload()).digest == policy.digest


@pytest.mark.parametrize("repeats,tolerance", [(1, 0), (2, 0), (2, "measured_noise")])
def test_bound_fallback_is_optional_and_emitted_only_when_declared(repeats, tolerance):
    document = payload("confidence")
    document["evaluation"]["repeats_per_question"] = repeats
    document["gate"]["causal"]["tolerance"] = tolerance
    without = parse(document)
    document["gate"]["bound_fallback"] = "point"
    policy = parse(document)
    assert policy.bound_fallback == "point"
    assert policy.to_payload() == document
    assert policy.digest != without.digest
    assert parse(policy.to_payload()).digest == policy.digest


@pytest.mark.parametrize("mode", ("point", "statistical", "unfiltered"))
def test_bound_fallback_is_refused_outside_confidence(mode):
    document = payload(mode)
    document["gate"]["bound_fallback"] = "point"
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == ("bad_value", "gate.bound_fallback")
    assert "confidence" in caught.value.expected


@pytest.mark.parametrize(
    "value,code", [(None, "bad_type"), (True, "bad_type"), ("bound", "bad_value")]
)
def test_declared_bound_fallback_requires_the_literal_point(value, code):
    document = payload("confidence")
    document["gate"]["bound_fallback"] = value
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == (code, "gate.bound_fallback")
    assert "point" in caught.value.expected


def test_absent_indeterminate_keys_keep_the_policy_digest():
    policy = parse(payload())
    assert policy.digest == "d1b0f82021dd439eac4e4b6a87786c070abc5be65100575890b47a2b9956602b"
    assert policy.indeterminate["target"].effective_min_tries == 1
    assert policy.indeterminate["target"].effective_count_recovered is True
    assert "min_tries" not in policy.to_payload()["indeterminate"]["target"]


def test_declared_indeterminate_keys_enter_the_policy_payload():
    document = payload()
    document["indeterminate"]["target"] = {
        "max_count": 3,
        "max_rate": "0.1",
        "min_tries": 20,
        "count_recovered": False,
    }
    policy = parse(document)
    assert policy.to_payload()["indeterminate"]["target"] == {
        "max_count": 3,
        "max_rate": "0.100000000000",
        "min_tries": 20,
        "count_recovered": False,
    }
    assert policy.indeterminate["target"].effective_min_tries == 20
    assert policy.indeterminate["target"].effective_count_recovered is False
    assert parse(policy.to_payload()).digest == policy.digest


@pytest.mark.parametrize(
    "key,value,code",
    [
        ("min_tries", 0, "bad_value"),
        ("min_tries", True, "bad_type"),
        ("min_tries", 1.5, "bad_type"),
        ("min_tries", None, "bad_type"),
        ("count_recovered", 0, "bad_type"),
        ("count_recovered", "false", "bad_type"),
        ("count_recovered", None, "bad_type"),
    ],
)
def test_indeterminate_controls_refuse_invalid_values(key, value, code):
    document = payload()
    document["indeterminate"]["target"][key] = value
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == (code, f"indeterminate.target.{key}")


def test_indeterminate_min_tries_has_no_upper_bound():
    document = payload()
    document["indeterminate"]["target"]["min_tries"] = 10**30
    assert parse(document).indeterminate["target"].effective_min_tries == 10**30


def test_stages_absent_keeps_payload_and_digest():
    document = v16_document()["campaign"]
    policy = parse(document)
    assert policy.digest == "d1b0f82021dd439eac4e4b6a87786c070abc5be65100575890b47a2b9956602b"
    assert policy.to_payload() == document
    assert policy.stages is None and policy.focus_first is False


def test_stages_single_equals_absent():
    document = payload()
    absent = parse(document)
    document["gate"]["stages"] = "single"
    single = parse(document)
    assert single.stages is None and single.focus_first is False
    assert single.to_payload() == absent.to_payload()
    assert single.digest == absent.digest
    assert "stages" not in single.to_payload()["gate"]


@pytest.mark.parametrize("mode", ["point", "confidence", "statistical", "unfiltered"])
def test_stages_focus_first_enters_digest(mode):
    document = payload(mode)
    absent = parse(document)
    document["gate"]["stages"] = "focus_first"
    staged = parse(document)
    assert staged.stages == "focus_first" and staged.focus_first is True
    assert staged.to_payload() == document
    assert staged.digest != absent.digest
    assert parse(staged.to_payload()).digest == staged.digest


@pytest.mark.parametrize(
    "value,code", [("both", "bad_value"), (None, "bad_type"), (True, "bad_type")]
)
def test_stages_bad_value_refused(value, code):
    document = payload()
    document["gate"]["stages"] = value
    with pytest.raises(policies.PolicyError) as refused:
        parse(document)
    assert (refused.value.code, refused.value.path) == (code, "gate.stages")
    assert "single" in refused.value.expected and "focus_first" in refused.value.expected


@pytest.mark.slow
async def test_stages_change_is_policy_change(tmp_path, monkeypatch):
    import json

    from evolution.tree.evolve import EvolveRefused, evolve
    from tests.campaign_drivers import _rebaseline, _Resettable
    from tests.test_tree_campaign_family15_condition_change import _interrupted

    before = payload()
    after = payload()
    after["gate"]["stages"] = "focus_first"
    assert policies.comparable_policy_payload(parse(before).to_payload()) != (
        policies.comparable_policy_payload(parse(after).to_payload())
    )
    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_baseline_complete")
    manifest = tmp_path / "manifest.json"
    document = json.loads(manifest.read_text())
    document["campaign"]["gate"]["stages"] = "focus_first"
    manifest.write_text(json.dumps(document))
    with pytest.raises(EvolveRefused) as refused:
        await evolve(manifest, run, adapter_factory=lambda *a: adapter)
    assert refused.value.code == "run_identity_changed"
    assert _rebaseline(refused.value).effect == "rebaseline"


@pytest.mark.parametrize("value", [None, "", "context\nwith\ttabs", "é"])
def test_v16_reflector_context_validation_matches_editor_context(value):
    document = payload()
    document["reflector_context"] = value
    policy = parse(document)
    assert policy.reflector_context == value


@pytest.mark.parametrize(
    "value,code",
    [
        (1, "bad_type"),
        (False, "bad_type"),
        ("bad\x00context", "bad_value"),
        ("bad\x7fcontext", "bad_value"),
    ],
)
def test_v16_reflector_context_refuses_bad_values(value, code):
    document = payload()
    document["reflector_context"] = value
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == (code, "reflector_context")


@pytest.mark.parametrize("key", ["editor_context", "reflector_context"])
def test_v16_context_max_bytes_bounds_both_contexts(key):
    document = payload()
    document.update(context_max_bytes=10, **{key: "é" * 5})
    assert getattr(parse(document), key) == "é" * 5
    document[key] += "x"
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path, caught.value.expected) == (
        "bad_value",
        key,
        "at most 10 bytes of UTF-8 (campaign.context_max_bytes)",
    )
    document.update(context_max_bytes=10**9, **{key: "x" * 70000})
    assert getattr(parse(document), key) == "x" * 70000


@pytest.mark.parametrize("key", ["editor_context", "reflector_context"])
def test_v16_default_context_limit_names_setting(key):
    document = payload()
    document[key] = "x" * 65537
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path, caught.value.expected) == (
        "bad_value",
        key,
        "at most 65536 bytes of UTF-8 (campaign.context_max_bytes)",
    )


@pytest.mark.parametrize(
    "value,code",
    [
        (0, "bad_value"),
        (-1, "bad_value"),
        (1.5, "bad_type"),
        (True, "bad_type"),
        (None, "bad_type"),
    ],
)
def test_v16_context_max_bytes_must_be_positive_int(value, code):
    document = payload()
    document["context_max_bytes"] = value
    with pytest.raises(policies.PolicyError) as caught:
        parse(document)
    assert (caught.value.code, caught.value.path) == (code, "context_max_bytes")


def test_absent_b17_campaign_keys_keep_v16_policy_digest():
    document = payload()
    for extra in ({}, {"reflector_context": None}):
        policy = parse(document | extra)
        assert policy.digest == "d1b0f82021dd439eac4e4b6a87786c070abc5be65100575890b47a2b9956602b"
        assert "reflector_context" not in policy.to_payload()
        assert "context_max_bytes" not in policy.to_payload()


def test_declared_b17_campaign_keys_round_trip_through_payload():
    document = payload() | {"reflector_context": "investigate", "context_max_bytes": 100000}
    policy = parse(document)
    assert policy.to_payload() == document
    assert parse(policy.to_payload()).digest == policy.digest
    assert policy.digest != parse(payload()).digest


@pytest.mark.parametrize(
    "extra,want",
    [
        ({}, "editor"),
        ({"reflector_context": None}, "editor"),
        ({"reflector_context": ""}, None),
        ({"reflector_context": "r"}, "r"),
    ],
)
def test_reflector_operator_context_resolution(extra, want):
    policy = parse(payload() | {"editor_context": "editor"} | extra)
    assert policy.reflector_operator_context == want
