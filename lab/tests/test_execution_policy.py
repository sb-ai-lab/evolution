"""Closed execution policy: names bind consumers, never upstream key values."""
from dataclasses import FrozenInstanceError
from decimal import Decimal
import hashlib
import json

import pytest

from evolution_lab.execution_policy import (
    ExecutionPolicy, PolicyInvalid, lab_filled_names, parse_policy, policy_digest,
)


def policy_payload():
    return {
        "schema": "lab-execution-policy/1",
        "gateway": {
            "host": "127.0.0.1", "port": 8765,
            "ledger_path": "/tmp/neutral-ledger.sqlite", "limit_units": 1000000,
            "ledger_identity": "neutral-policy", "currency": "RUB",
        },
        "routes": {
            "target": {
                "client_model": "target-client", "endpoint": "http://localhost:9876/v1",
                "api_key_secret": None, "model": "local-model",
                "price": {"input_per_million": 0, "output_per_million": 0, "context_tokens": 4096},
                "max_tokens": 100, "timeout_seconds": 30,
            },
            "editor": {
                "client_model": "editor-client", "endpoint": "https://routerai.ru/api/v1",
                "api_key_secret": "ROUTER_KEY", "model": "upstream-model",
                "price": {"input_per_million": 1.25, "output_per_million": 2, "context_tokens": 4096},
                "max_tokens": 100, "timeout_seconds": 30,
                "provider": {"order": ["provider-a", "provider-b"], "allow_fallbacks": True},
                "balance_floor": 5,
            },
        },
        "target_token_name": "TARGET_TOKEN",
        "scorer_token_names": {"SCORER_TOKEN": "editor", "TARGET_TOKEN": "target"},
        "role_groups": {"runtime": ["target", "editor"]},
    }


def test_valid_policy_round_trip_and_frozen_fields():
    payload = policy_payload()
    policy = parse_policy(payload)
    assert isinstance(policy, ExecutionPolicy)
    assert policy.to_payload() == payload
    assert parse_policy(policy.to_payload()).to_payload() == payload
    assert policy.gateway.ledger_identity == "neutral-policy"
    assert policy.routes["editor"].price.input == Decimal("1.25")
    assert policy.routes["target"].api_key_secret is None
    assert policy.routes["editor"].provider.order == ("provider-a", "provider-b")
    assert policy.routes["editor"].provider.only is None
    assert policy.routes["editor"].provider.allow_fallbacks is True
    assert policy.routes["editor"].balance_floor == 5
    assert policy.role_groups["runtime"] == ("target", "editor")
    assert lab_filled_names(policy) == frozenset({"TARGET_TOKEN", "SCORER_TOKEN"})
    with pytest.raises(FrozenInstanceError):
        policy.gateway.port = 9999
    with pytest.raises(FrozenInstanceError):
        policy.routes["editor"].model = "changed"
    with pytest.raises(TypeError):
        policy.routes["new"] = policy.routes["target"]
    payload["routes"]["editor"]["model"] = "changed"
    public = policy.to_payload()
    public["gateway"]["port"] = 9999
    assert policy.to_payload()["gateway"]["port"] == 8765
    assert policy.routes["editor"].model == "upstream-model"


def test_digest_is_canonical_utf8_without_unicode_normalization():
    payload = policy_payload()
    payload["gateway"]["ledger_identity"] = "нейтральный-e\u0301"
    reversed_keys = {key: payload[key] for key in reversed(payload)}
    reversed_keys["gateway"] = dict(reversed(list(payload["gateway"].items())))
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    assert policy_digest(payload) == hashlib.sha256(encoded).hexdigest()
    assert policy_digest(reversed_keys) == policy_digest(payload)
    from evolution_lab.preparation.storage import canonical_json_bytes
    assert encoded == canonical_json_bytes(payload)


def _set(payload, path, value):
    for key in path[:-1]:
        payload = payload[key]
    payload[path[-1]] = value


@pytest.mark.parametrize("path,value,code", [
    (("extra",), True, "unknown_key"),
    (("gateway", "extra"), True, "unknown_key"),
    (("routes", "editor", "extra"), True, "unknown_key"),
    (("routes", "editor", "price", "extra"), True, "unknown_key"),
    (("routes", "editor", "provider", "extra"), True, "unknown_key"),
    (("schema",), "lab-execution-policy/3", "invalid_value"),
    (("gateway", "host"), "0.0.0.0", "host_not_loopback"),
    (("gateway", "host"), "example.test", "host_not_loopback"),
    (("gateway", "port"), True, "invalid_value"),
    (("gateway", "port"), 0, "invalid_value"),
    (("gateway", "port"), 65536, "invalid_value"),
    (("gateway", "limit_units"), -1, "invalid_value"),
    (("gateway", "limit_units"), 2**62 + 1, "invalid_value"),
    (("gateway", "currency"), "EUR", "invalid_value"),
    (("gateway", "ledger_path"), "relative-ledger.sqlite", "invalid_value"),
    (("routes", "editor", "endpoint"), "file:///tmp/model", "invalid_value"),
    (("routes", "editor", "endpoint"), "http://user:password@localhost/v1", "invalid_value"),
    (("routes", "editor", "endpoint"), "http://localhost/v1?key=value", "invalid_value"),
    (("routes", "editor", "endpoint"), "http://localhost/v1#fragment", "invalid_value"),
    (("routes", "editor", "api_key_secret"), "", "invalid_value"),
    (("routes", "editor", "max_tokens"), 0, "invalid_value"),
    (("routes", "editor", "timeout_seconds"), float("inf"), "invalid_value"),
    (("routes", "editor", "timeout_seconds"), True, "invalid_value"),
    (("routes", "editor", "price", "input_per_million"), -1, "invalid_value"),
    (("routes", "editor", "price", "output_per_million"), float("nan"), "invalid_value"),
    (("routes", "editor", "price", "context_tokens"), False, "invalid_value"),
    (("routes", "editor", "balance_floor"), 0, "invalid_value"),
    (("routes", "editor", "balance_floor"), float("nan"), "invalid_value"),
    (("target_token_name",), "BAD NAME", "invalid_value"),
])
def test_invalid_fields_report_code_and_path_without_values(path, value, code):
    payload = policy_payload()
    _set(payload, path, value)
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert caught.value.code == code
    assert caught.value.path == "/" + "/".join(path)
    assert str(caught.value) == f"{code}: {caught.value.path}"


@pytest.mark.parametrize("path", [(), ("gateway",), ("routes", "editor"),
                                     ("routes", "editor", "price"), ("routes", "editor", "provider")])
def test_reasoning_effort_is_refused_at_every_object_level(path):
    payload = policy_payload()
    _set(payload, (*path, "reasoning_effort"), "high")
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert caught.value.code == "unknown_key"
    assert caught.value.path == "/" + "/".join((*path, "reasoning_effort"))


@pytest.mark.parametrize("field,value", [
    ("routes", {}), ("scorer_token_names", "editor"), ("role_groups", ["editor"]),
])
def test_reasoning_effort_is_refused_even_as_a_dynamic_mapping_key(field, value):
    payload = policy_payload()
    payload[field]["reasoning_effort"] = value
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("unknown_key", f"/{field}/reasoning_effort")


def test_large_integer_numeric_input_has_a_policy_refusal():
    payload = policy_payload()
    payload["routes"]["target"]["timeout_seconds"] = 10**400
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("invalid_value", "/routes/target/timeout_seconds")


@pytest.mark.parametrize("provider", [
    {"only": ["provider-a"], "order": ["provider-b"]},
    {"allow_fallbacks": True}, {"only": ["provider-a"], "allow_fallbacks": False},
    {"order": ["provider-a"]}, {"order": ["provider-a"], "allow_fallbacks": "yes"},
    {"only": []}, {"only": ["provider-a", "provider-a"]}, {"only": ["INVALID NAME"]},
])
def test_provider_pin_requires_one_complete_closed_choice(provider):
    payload = policy_payload()
    payload["routes"]["editor"]["provider"] = provider
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert caught.value.code == "provider_invalid"
    assert caught.value.path.startswith("/routes/editor/provider")


@pytest.mark.parametrize("endpoint", ["https://routerai.ru/api/v1/", "https://openrouter.ai/api/v1",
                                     "http://localhost:9876/routerai/api/v1",
                                     "http://127.0.0.1:9876/openrouter/api/v1"])
def test_provider_only_and_matching_balance_protocol(endpoint):
    payload = policy_payload()
    payload["routes"]["editor"]["endpoint"] = endpoint
    payload["gateway"]["currency"] = "USD" if "openrouter" in endpoint else "RUB"
    payload["routes"]["editor"]["provider"] = {"only": ["provider-a", "provider-b"]}
    pin = parse_policy(payload).routes["editor"].provider
    assert pin.only == ("provider-a", "provider-b") and pin.order is None and pin.allow_fallbacks is False


def test_provider_is_refused_for_non_router_endpoint():
    payload = policy_payload()
    payload["routes"]["editor"]["endpoint"] = "http://localhost:9876/v1"
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("provider_unsupported", "/routes/editor/provider")


@pytest.mark.parametrize("change", ["no_key", "wrong_currency", "no_protocol"])
def test_balance_floor_needs_key_and_protocol_currency(change):
    payload = policy_payload()
    route = payload["routes"]["editor"]
    route.pop("provider")
    if change == "no_key":
        route["api_key_secret"] = None
    elif change == "wrong_currency":
        payload["gateway"]["currency"] = "USD"
    else:
        route["endpoint"] = "http://localhost:9876/v1"
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("balance_floor_unsupported", "/routes/editor/balance_floor")


@pytest.mark.parametrize("name", ["TARGET_TOKEN", "SCORER_TOKEN"])
def test_upstream_secret_names_must_differ_from_all_lab_filled_names(name):
    payload = policy_payload()
    payload["routes"]["editor"]["api_key_secret"] = name
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("secret_name_conflict", "/routes/editor/api_key_secret")


def test_shared_route_across_consumers_is_allowed_but_shared_name_across_routes_is_not():
    payload = policy_payload()
    payload["scorer_token_names"]["ANOTHER_TOKEN"] = "editor"
    assert lab_filled_names(parse_policy(payload)) == frozenset({"TARGET_TOKEN", "SCORER_TOKEN", "ANOTHER_TOKEN"})
    payload["scorer_token_names"]["TARGET_TOKEN"] = "editor"
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("token_name_conflict", "/scorer_token_names/TARGET_TOKEN")


def test_role_group_ambiguity_uses_client_model_not_upstream_model():
    payload = policy_payload()
    payload["routes"]["target"]["model"] = payload["routes"]["editor"]["model"]
    parse_policy(payload)  # same upstream model is unambiguous when client names differ
    payload["routes"]["target"]["client_model"] = "editor-client"
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("ambiguous_role_group", "/role_groups/runtime")
    payload["role_groups"] = {}  # same client name in separate routes is allowed
    parse_policy(payload)


@pytest.mark.parametrize("members", [[], ["missing"], ["target", "target"], "target"])
def test_role_groups_require_unique_existing_members(members):
    payload = policy_payload()
    payload["role_groups"]["runtime"] = members
    with pytest.raises(PolicyInvalid):
        parse_policy(payload)


@pytest.mark.parametrize("payload", [None, [], "policy", 1])
def test_policy_requires_object(payload):
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("invalid_type", "")


def test_optional_bindings_and_route_options_may_be_absent():
    payload = policy_payload()
    del payload["scorer_token_names"], payload["role_groups"]
    del payload["routes"]["editor"]["provider"], payload["routes"]["editor"]["balance_floor"]
    policy = parse_policy(payload)
    assert policy.to_payload() == payload
    assert policy.routes["editor"].provider is None
    assert policy.routes["editor"].balance_floor is None
    assert lab_filled_names(policy) == frozenset({"TARGET_TOKEN"})


@pytest.mark.parametrize("target_token_name", [None, "absent"])
def test_target_without_llm_needs_no_target_route(target_token_name):
    payload = policy_payload()
    del payload["routes"]["target"]
    payload["scorer_token_names"] = {"SCORER_TOKEN": "editor"}
    payload["role_groups"] = {"runtime": ["editor"]}
    if target_token_name == "absent":
        del payload["target_token_name"]
    else:
        payload["target_token_name"] = target_token_name
    assert lab_filled_names(parse_policy(payload)) == frozenset({"SCORER_TOKEN"})


def test_declared_target_token_requires_target_route():
    payload = policy_payload()
    del payload["routes"]["target"]
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("invalid_route_reference", "/target_token_name")


def test_required_field_reports_missing_key():
    payload = policy_payload()
    del payload["routes"]["editor"]["api_key_secret"]
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(payload)
    assert (caught.value.code, caught.value.path) == ("missing_key", "/routes/editor/api_key_secret")


def test_digest_rejects_non_json_and_nonfinite_values():
    for value in ({"bad": float("nan")}, {"bad": object()}):
        with pytest.raises(PolicyInvalid) as caught:
            policy_digest(value)
        assert caught.value.code == "invalid_json"


def test_v2_auto_resume_without_gateway_and_default_interval():
    from evolution_lab.execution_policy import policy_auto_resume
    document = {"schema": "lab-execution-policy/2", "auto_resume": {
        "reasons": ["reflector_failing"], "max_resumes": 2, "delay_seconds": 0}}
    policy = parse_policy(document)
    assert policy.gateway is None
    assert policy.to_payload() == document
    rules = policy_auto_resume(policy)
    assert (rules.reasons, rules.max_resumes, rules.delay_seconds, rules.busy_recheck_seconds) == (
        frozenset({"reflector_failing"}), 2, 0, 60)
    assert policy_auto_resume(parse_policy(policy_payload())) is None


def test_v2_gateway_and_auto_resume_coexist():
    from evolution_lab.execution_policy import policy_auto_resume
    document = policy_payload()
    document.update(schema="lab-execution-policy/2", auto_resume={"reasons": ["editor_failing"],
        "max_resumes": 3, "delay_seconds": 1, "busy_recheck_seconds": 9})
    policy = parse_policy(document)
    assert policy.gateway.port == 8765
    assert policy_auto_resume(policy).busy_recheck_seconds == 9


@pytest.mark.parametrize("field,value,path", [
    ("reasons", [], "/auto_resume/reasons"),
    ("reasons", ["unknown"], "/auto_resume/reasons/0"),
    ("reasons", ["editor_failing", "editor_failing"], "/auto_resume/reasons/1"),
    ("max_resumes", 0, "/auto_resume/max_resumes"),
    ("delay_seconds", -1, "/auto_resume/delay_seconds"),
    ("busy_recheck_seconds", 0, "/auto_resume/busy_recheck_seconds"),
])
def test_v2_invalid_auto_resume_has_pointer(field, value, path):
    document = {"schema": "lab-execution-policy/2", "auto_resume": {
        "reasons": ["editor_failing"], "max_resumes": 1, "delay_seconds": 0}}
    document["auto_resume"][field] = value
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(document)
    assert caught.value.path == path


@pytest.mark.parametrize("missing", ["gateway", "routes"])
def test_v2_gateway_and_routes_must_be_paired(missing):
    document = policy_payload()
    document['schema'] = 'lab-execution-policy/2'
    del document[missing]
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(document)
    assert caught.value.path == '/' + missing


def test_v2_auto_resume_accepts_interval_max_and_unbounded_resume_count():
    from evolution_lab.execution_policy import policy_auto_resume
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 31_536_000, 'busy_recheck_seconds': 31_536_000}}
    rules = policy_auto_resume(parse_policy(document))
    assert (rules.max_resumes, rules.delay_seconds, rules.busy_recheck_seconds) == (2**70, 31_536_000, 31_536_000)


@pytest.mark.parametrize('field', ['delay_seconds', 'busy_recheck_seconds'])
@pytest.mark.parametrize('value', [31_536_001, 2**72])
def test_v2_interval_above_max_is_rejected_with_pointer(field, value):
    document = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 0, 'busy_recheck_seconds': 1}}
    document['auto_resume'][field] = value
    with pytest.raises(PolicyInvalid) as caught:
        parse_policy(document)
    assert caught.value.path == '/auto_resume/' + field
