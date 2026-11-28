"""Admit operator-supplied route metadata for offline context scenarios."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from fractions import Fraction
from math import ceil, floor
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from evolution_lab.context_report import Measurement, Route

RequestStyle = Literal["modern", "modern_single", "modern_auto", "legacy"]
_STYLES = ("modern", "modern_single", "modern_auto", "legacy")
_ROLE_NAMES = {"reflector", "editor", "grouper"}
_REQUIRED_FIELDS = {"model", "upstream", "source", "styles"}
_OPTIONAL_FIELDS = {"window_tokens", "reserve_tokens", "tokenizer", "page_chars", "bytes_per_token"}
_MAX_SCENARIO_BYTES = 16_384
_MAX_PAGE_CHARS = 1_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_SOURCE_LABEL = re.compile(r"\w[\w .:/@+-]{0,127}")


class ContextScenarioError(ValueError):
    """A scenario refusal whose message contains only bounded field names and reasons."""


@dataclass(frozen=True, kw_only=True)
class ScenarioRoute:
    # Use the upstream model and responding provider identifiers, not the client's model alias.
    model: str
    upstream: str
    source: str
    styles: tuple[RequestStyle, ...]
    window_tokens: int | None = None
    reserve_tokens: int | None = None
    tokenizer: str | None = None
    page_chars: int | None = None
    bytes_per_token: tuple[Fraction, Fraction] | None = None

    def report_route(self, style: str) -> Route:
        if style not in self.styles:
            raise ContextScenarioError("request style is outside the scenario")
        return Route(
            model=self.model,
            upstream=self.upstream,
            request_style=style,
            tokenizer=self.tokenizer,
            source=self.source,
            tokenizer_source=self.source if self.tokenizer else None,
        )

    def window_measurement(self) -> Measurement:
        if self.window_tokens is None:
            return Measurement.unknown(unit="tokens", source="provide scenario.window_tokens")
        return Measurement.exact(
            self.window_tokens,
            unit="tokens",
            provenance="configuration",
            source=f"scenario.window_tokens ({self.source})",
        )

    def estimate_input(self, request_bytes: int) -> Measurement:
        """Estimate only from a complete hypothetical serialized request, including protocol."""
        if type(request_bytes) is not int or request_bytes < 0:
            raise ValueError("serialized request bytes must be a nonnegative integer")
        if self.bytes_per_token is None:
            return Measurement.unknown(unit="tokens", source="provide scenario.bytes_per_token")
        low_rate, high_rate = self.bytes_per_token
        return Measurement(
            "tokens",
            "estimate",
            f"serialized request bytes / scenario.bytes_per_token ({self.source})",
            floor(request_bytes / high_rate),
            ceil(request_bytes / low_rate),
        )


@dataclass(frozen=True)
class ContextScenario:
    roles: Mapping[str, ScenarioRoute]


def observed_window(observed: Route, scenario: ScenarioRoute | None) -> Measurement:
    """Use a declared window only for the recorded responding provider and upstream model."""
    if not observed.model or not observed.upstream:
        return Measurement.unknown(
            unit="tokens", source="identify recorded provider and upstream model"
        )
    if scenario is None or (observed.model, observed.upstream) != (
        scenario.model,
        scenario.upstream,
    ):
        return Measurement.unknown(
            unit="tokens", source="provide window for recorded provider and upstream model"
        )
    return scenario.window_measurement()


def load_context_scenario(path: Path) -> ContextScenario:
    """Read a local scenario with a closed schema and no implicit route defaults."""
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_SCENARIO_BYTES + 1)
    except OSError:
        raise ContextScenarioError("could not read scenario file") from None
    if len(data) > _MAX_SCENARIO_BYTES:
        raise ContextScenarioError("scenario file exceeds the size limit")
    try:
        document = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except ContextScenarioError:
        raise
    except (ValueError, RecursionError, DecimalException):
        raise ContextScenarioError("scenario file must contain valid JSON") from None
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "roles"}
        or document["schema"] != "lab-context-scenario/1"
    ):
        raise ContextScenarioError("expected lab-context-scenario/1 with a roles object")
    roles = document["roles"]
    if not isinstance(roles, dict) or not roles or not roles.keys() <= _ROLE_NAMES:
        raise ContextScenarioError("scenario roles must name a grouper, reflector or editor")
    return ContextScenario(
        MappingProxyType({role: _parse_route(value) for role, value in roles.items()})
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextScenarioError("scenario has a duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ContextScenarioError("scenario numbers must be finite")


def _parse_route(value: object) -> ScenarioRoute:
    if not isinstance(value, dict) or not _REQUIRED_FIELDS <= value.keys():
        raise ContextScenarioError("scenario role requires model, upstream, source and styles")
    if not value.keys() <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS:
        raise ContextScenarioError("scenario role contains unsupported fields")
    styles = value["styles"]
    if (
        not isinstance(styles, list)
        or not styles
        or any(not isinstance(style, str) or style not in _STYLES for style in styles)
        or len(set(styles)) != len(styles)
    ):
        raise ContextScenarioError(
            "scenario styles must be a list of distinct supported request styles"
        )
    return ScenarioRoute(
        model=_label(value["model"], field="model"),
        upstream=_label(value["upstream"], field="upstream"),
        source=_label(value["source"], field="source"),
        styles=tuple(cast(list[RequestStyle], styles)),
        tokenizer=_label(value["tokenizer"], field="tokenizer") if "tokenizer" in value else None,
        window_tokens=_optional_int(value, "window_tokens", minimum=1),
        reserve_tokens=_optional_int(value, "reserve_tokens", minimum=1),
        page_chars=_optional_int(value, "page_chars", minimum=1, maximum=_MAX_PAGE_CHARS),
        bytes_per_token=_parse_ratio(value["bytes_per_token"])
        if "bytes_per_token" in value
        else None,
    )


def _label(value: object, *, field: str) -> str:
    pattern = _SOURCE_LABEL if field == "source" else _IDENTIFIER
    if (
        not isinstance(value, str)
        or not pattern.fullmatch(value)
        or "://" in value
        or value != value.strip()
    ):
        raise ContextScenarioError(f"scenario {field} must be a public label")
    return value


def _optional_int(
    value: dict, field: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    if field not in value:
        return None
    number = value[field]
    if type(number) is not int or number < minimum or (maximum is not None and number > maximum):
        raise ContextScenarioError(f"scenario {field} is outside the supported integer bounds")
    return number


def _parse_ratio(value: object) -> tuple[Fraction, Fraction]:
    error = "scenario bytes_per_token requires two ordered positive numbers"
    if not isinstance(value, list) or len(value) != 2:
        raise ContextScenarioError(error)
    for bound in value:
        if type(bound) not in (int, Decimal) or bound <= 0:
            raise ContextScenarioError(error)
        if isinstance(bound, Decimal) and abs(int(bound.as_tuple().exponent)) > 1_024:
            raise ContextScenarioError("scenario bytes_per_token is outside the numeric limits")
    low, high = (Fraction(bound) for bound in value)
    if low > high:
        raise ContextScenarioError(error)
    return low, high
