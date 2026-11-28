"""Closed, secret-name-only execution policy outside Bundle and Core identity.

``target_token_name`` optionally binds a token to the ``target`` route;
``scorer_token_names`` maps environment names to routes. Multiple consumers may
share a route (and its token name), but a name cannot designate different routes.
``role_groups`` maps gateway group names to route names, selected by client model.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .budget import Price


class PolicyInvalid(ValueError):
    """A refusal code and JSON pointer, without input values or secret material."""

    def __init__(self, code: str, path: str):
        self.code, self.path = code, path
        super().__init__(f"{code}: {path}")


@dataclass(frozen=True)
class GatewayBlock:
    host: str
    port: int
    ledger_path: str
    limit_units: int
    ledger_identity: str
    currency: str


@dataclass(frozen=True)
class ProviderPin:
    only: tuple[str, ...] | None = None
    order: tuple[str, ...] | None = None
    allow_fallbacks: bool = False


@dataclass(frozen=True)
class PolicyRoute:
    client_model: str
    endpoint: str
    api_key_secret: str | None
    model: str
    price: Price
    max_tokens: int
    timeout_seconds: float
    provider: ProviderPin | None = None
    balance_floor: float | None = None


@dataclass(frozen=True)
class AutoResumeRules:
    reasons: frozenset[str]
    max_resumes: int
    delay_seconds: int
    busy_recheck_seconds: int


@dataclass(frozen=True)
class ExecutionPolicy:
    gateway: GatewayBlock | None
    routes: Mapping[str, PolicyRoute]
    target_token_name: str | None
    scorer_token_names: Mapping[str, str]
    role_groups: Mapping[str, tuple[str, ...]]
    _canonical: bytes = field(repr=False, compare=False)
    auto_resume: AutoResumeRules | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return detached JSON, retaining optional-field absence for its digest."""
        return json.loads(self._canonical)


def safe_policy_pointer(path: str) -> str:
    """Expose only static schema names and list indices, never caller-provided keys."""
    if re.fullmatch(r"/(?:gateway|routes|schema|auto_resume(?:/(?:reasons(?:/[0-9]+)?|max_resumes|delay_seconds|busy_recheck_seconds))?)", path):
        return path
    return ""


def _canonical_bytes(payload: Any) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PolicyInvalid("invalid_json", "") from None


def policy_digest(payload: Mapping) -> str:
    """SHA256 of canonical UTF-8 JSON, without normalizing Unicode."""
    if not isinstance(payload, Mapping):
        raise PolicyInvalid("invalid_type", "")
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def lab_filled_names(policy: ExecutionPolicy) -> frozenset[str]:
    names = set(policy.scorer_token_names)
    if policy.target_token_name is not None:
        names.add(policy.target_token_name)
    return frozenset(names)


def _at(path: str, key: str) -> str:
    return path + "/" + key.replace("~", "~0").replace("/", "~1")


def _object(value: Any, path: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PolicyInvalid("invalid_type", path)
    if "reasoning_effort" in value:
        raise PolicyInvalid("unknown_key", _at(path, "reasoning_effort"))
    return value


def _closed(value: Any, path: str, required: set[str], optional: set[str] = frozenset()) -> dict:
    obj = _object(value, path)
    for key in obj:
        if key not in required | optional:
            raise PolicyInvalid("unknown_key", _at(path, key))
    for key in sorted(required - obj.keys()):
        raise PolicyInvalid("missing_key", _at(path, key))
    return obj


def _text(value: Any, path: str, pattern: str | None = None) -> str:
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or (pattern is not None and re.fullmatch(pattern, value) is None)):
        raise PolicyInvalid("invalid_value", path)
    return value


def _integer(value: Any, path: str, minimum: int = 1, maximum: int = 2**62) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PolicyInvalid("invalid_value", path)
    return value


def _number(value: Any, path: str, *, positive: bool = False) -> float:
    try:
        finite = isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        finite = False
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not finite or value < 0 or (positive and value == 0)):
        raise PolicyInvalid("invalid_value", path)
    return value


_ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ROLE_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_PROVIDER_NAME = r"[a-z0-9][a-z0-9/._-]*"


def _gateway(value: Any) -> GatewayBlock:
    path = "/gateway"
    obj = _closed(value, path, {"host", "port", "ledger_path", "limit_units", "ledger_identity", "currency"})
    host = _text(obj["host"], path + "/host")
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise PolicyInvalid("host_not_loopback", path + "/host")
    currency = obj["currency"]
    if currency not in ("RUB", "USD"):
        raise PolicyInvalid("invalid_value", path + "/currency")
    ledger_path = _text(obj["ledger_path"], path + "/ledger_path")
    if not Path(ledger_path).is_absolute():
        raise PolicyInvalid("invalid_value", path + "/ledger_path")
    return GatewayBlock(
        host, _integer(obj["port"], path + "/port", maximum=65535),
        ledger_path,
        _integer(obj["limit_units"], path + "/limit_units", minimum=0),
        _text(obj["ledger_identity"], path + "/ledger_identity"), currency,
    )


def _provider(value: Any, path: str) -> ProviderPin:
    obj = _closed(value, path, set(), {"only", "order", "allow_fallbacks"})
    if set(obj) not in ({"only"}, {"order", "allow_fallbacks"}):
        raise PolicyInvalid("provider_invalid", path)
    key = "only" if "only" in obj else "order"
    names = obj[key]
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or re.fullmatch(_PROVIDER_NAME, name) is None for name in names)
            or len(set(names)) != len(names)):
        raise PolicyInvalid("provider_invalid", _at(path, key))
    fallbacks = obj.get("allow_fallbacks", False)
    if not isinstance(fallbacks, bool):
        raise PolicyInvalid("provider_invalid", path + "/allow_fallbacks")
    return ProviderPin(only=tuple(names)) if key == "only" else ProviderPin(order=tuple(names), allow_fallbacks=fallbacks)


def _route(value: Any, path: str, currency: str) -> PolicyRoute:
    # Local import keeps the policy interface available to budget without a cycle.
    from .budget import Price, _route_protocol

    obj = _closed(value, path,
                  {"client_model", "endpoint", "api_key_secret", "model", "price", "max_tokens", "timeout_seconds"},
                  {"provider", "balance_floor"})
    client_model = _text(obj["client_model"], path + "/client_model")
    model = _text(obj["model"], path + "/model")
    endpoint = _text(obj["endpoint"], path + "/endpoint")
    try:
        url = httpx.URL(endpoint)
        valid = url.scheme in {"http", "https"} and url.host and not (url.userinfo or url.query or url.fragment)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        raise PolicyInvalid("invalid_value", path + "/endpoint")
    secret = obj["api_key_secret"]
    if secret is not None:
        secret = _text(secret, path + "/api_key_secret", _ENV_NAME)
    price_obj = _closed(obj["price"], path + "/price", {"input_per_million", "output_per_million", "context_tokens"})
    price = Price(
        _number(price_obj["input_per_million"], path + "/price/input_per_million"),
        _number(price_obj["output_per_million"], path + "/price/output_per_million"),
        _integer(price_obj["context_tokens"], path + "/price/context_tokens"),
    )
    provider = None
    if "provider" in obj:
        provider = _provider(obj["provider"], path + "/provider")
        if _route_protocol(endpoint) not in {"routerai", "openrouter"}:
            raise PolicyInvalid("provider_unsupported", path + "/provider")
        if "@" in model:
            raise PolicyInvalid("provider_invalid", path + "/model")
    floor = None
    if "balance_floor" in obj:
        floor = _number(obj["balance_floor"], path + "/balance_floor", positive=True)
        protocol_currency = {"routerai": "RUB", "openrouter": "USD"}.get(_route_protocol(endpoint))
        if secret is None or protocol_currency != currency:
            raise PolicyInvalid("balance_floor_unsupported", path + "/balance_floor")
    return PolicyRoute(client_model, endpoint, secret, model, price,
                       _integer(obj["max_tokens"], path + "/max_tokens"),
                       _number(obj["timeout_seconds"], path + "/timeout_seconds", positive=True),
                       provider, floor)


def policy_auto_resume(policy: ExecutionPolicy) -> AutoResumeRules | None:
    return policy.auto_resume


def _auto_resume(value: Any) -> AutoResumeRules:
    from .owner.core_raw import suspension_reasons
    path = "/auto_resume"
    obj = _closed(value, path, {"reasons", "max_resumes", "delay_seconds"}, {"busy_recheck_seconds"})
    reasons = obj["reasons"]
    if not isinstance(reasons, list) or not reasons:
        raise PolicyInvalid("invalid_value", path + "/reasons")
    valid, seen = suspension_reasons(), set()
    for index, reason in enumerate(reasons):
        if not isinstance(reason, str) or reason not in valid or reason in seen:
            raise PolicyInvalid("invalid_value", f"{path}/reasons/{index}")
        seen.add(reason)
    def integer(key: str, minimum: int, default: int | None = None, *, maximum: int | None = None) -> int:
        value = obj.get(key, default)
        if (isinstance(value, bool) or not isinstance(value, int) or value < minimum
                or (maximum is not None and value > maximum)):
            raise PolicyInvalid("invalid_value", path + "/" + key)
        return value
    max_interval = 31_536_000  # 365 days, ADR-0031
    return AutoResumeRules(frozenset(seen), integer("max_resumes", 1),
                           integer("delay_seconds", 0, maximum=max_interval),
                           integer("busy_recheck_seconds", 1, 60, maximum=max_interval))


def parse_policy(payload: Any) -> ExecutionPolicy:
    preliminary = _object(payload, "")
    schema = preliminary.get("schema")
    if schema == "lab-execution-policy/2":
        obj = _closed(payload, "", {"schema"}, {"gateway", "routes", "auto_resume", "target_token_name", "scorer_token_names", "role_groups"})
        if ("gateway" in obj) != ("routes" in obj):
            raise PolicyInvalid("missing_key", "/routes" if "gateway" in obj else "/gateway")
    else:
        obj = _closed(payload, "", {"schema", "gateway", "routes"},
                      {"target_token_name", "scorer_token_names", "role_groups"})
        if obj["schema"] != "lab-execution-policy/1":
            raise PolicyInvalid("invalid_value", "/schema")
    gateway = _gateway(obj["gateway"]) if "gateway" in obj else None
    routes = {}
    for name, value in _object(obj.get("routes", {}), "/routes").items():
        _text(name, _at("/routes", name), _ROLE_NAME)
        routes[name] = _route(value, _at("/routes", name), gateway.currency)
    if gateway is not None and not routes:
        raise PolicyInvalid("invalid_value", "/routes")

    target = obj.get("target_token_name")
    if target is not None:
        _text(target, "/target_token_name", _ENV_NAME)
        if "target" not in routes:
            raise PolicyInvalid("invalid_route_reference", "/target_token_name")
    scorers = {}
    for name, route in _object(obj.get("scorer_token_names", {}), "/scorer_token_names").items():
        path = _at("/scorer_token_names", name)
        _text(name, path, _ENV_NAME)
        if not isinstance(route, str) or route not in routes:
            raise PolicyInvalid("invalid_route_reference", path)
        if name == target and route != "target":
            raise PolicyInvalid("token_name_conflict", path)
        scorers[name] = route

    groups = {}
    for name, members in _object(obj.get("role_groups", {}), "/role_groups").items():
        path = _at("/role_groups", name)
        _text(name, path, _ROLE_NAME)
        if (name in routes or not isinstance(members, list) or not members
                or any(not isinstance(member, str) or member not in routes for member in members)
                or len(set(members)) != len(members)):
            raise PolicyInvalid("invalid_role_group", path)
        if len({routes[member].client_model for member in members}) != len(members):
            raise PolicyInvalid("ambiguous_role_group", path)
        groups[name] = tuple(members)
    filled = set(scorers) | ({target} if target is not None else set())
    for name, route in routes.items():
        if route.api_key_secret in filled:
            raise PolicyInvalid("secret_name_conflict", _at("/routes", name) + "/api_key_secret")
    return ExecutionPolicy(gateway, MappingProxyType(routes), target,
                           MappingProxyType(scorers), MappingProxyType(groups), _canonical_bytes(obj),
                           _auto_resume(obj["auto_resume"]) if "auto_resume" in obj else None)


def bound_chat_route(policy: ExecutionPolicy, base_url: str, client_model: str) -> str | None:
    """Resolve the same exact local gateway group and client model used at dispatch."""
    from urllib.parse import urlsplit

    if policy.gateway is None:
        return None
    path = urlsplit(base_url).path.rstrip("/").split("/")
    if len(path) != 3 or path[-1] != "v1":
        return None
    group = path[1]
    host = policy.gateway.host
    authority = f"[{host}]" if ":" in host else host
    if base_url.rstrip("/") != f"http://{authority}:{policy.gateway.port}/{group}/v1":
        return None
    candidates = policy.role_groups.get(group, (group,) if group in policy.routes else ())
    return next((name for name in candidates if policy.routes[name].client_model == client_model), None)
