"""Numerical context preflight output for both the terminal and JSON."""

from __future__ import annotations

from dataclasses import KW_ONLY, asdict, dataclass
from typing import Literal

Unit = Literal[
    "tokens",
    "chars",
    "bytes",
    "events",
    "items",
    "turns",
    "keys",
    "calls",
    "assignments",
    "attempts",
    "repairs",
    "matches",
]
Provenance = Literal[
    "measured",
    "provider_usage",
    "reproducible_count",
    "estimate",
    "configuration",
    "derived",
    "unknown",
]
Basis = Literal["observed", "reconstructed", "hypothetical", "unavailable", "skipped", "configured"]
BudgetStatus = Literal["within_window", "zero_headroom", "over_window", "uncertain", "unknown"]


@dataclass(frozen=True)
class Measurement:
    unit: Unit
    provenance: Provenance
    source: str
    lower: int | None = None
    upper: int | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("measurement source is required")
        if self.provenance == "unknown":
            if self.lower is not None or self.upper is not None:
                raise ValueError("unknown measurements cannot have bounds")
        elif type(self.lower) is not int or type(self.upper) is not int or self.lower > self.upper:
            raise ValueError("known measurements require ordered integer bounds")

    @classmethod
    def exact(cls, value: int, *, unit: Unit, provenance: Provenance, source: str) -> Measurement:
        return cls(unit, provenance, source, value, value)

    @classmethod
    def unknown(cls, *, unit: Unit, source: str) -> Measurement:
        return cls(unit, "unknown", source)


@dataclass(frozen=True)
class Metric:
    name: str
    measurement: Measurement


@dataclass(frozen=True)
class TokenBudget:
    window: Measurement
    input_tokens: Measurement
    response_reserve: Measurement
    headroom: Measurement
    status: BudgetStatus


def calculate_token_budget(
    window: Measurement, input_tokens: Measurement, response_reserve: Measurement
) -> TokenBudget:
    """Subtract intervals conservatively, preserving the provenance of every operand."""
    operands = {
        "window": window,
        "input_tokens": input_tokens,
        "response_reserve": response_reserve,
    }
    for measurement in operands.values():
        if measurement.unit != "tokens":
            raise ValueError("all token budget measurements must use tokens")
        if measurement.lower is not None and measurement.lower < 0:
            raise ValueError("token budget inputs must be nonnegative")

    missing = [
        f"{name} ({value.source})" for name, value in operands.items() if value.lower is None
    ]
    if missing:
        headroom = Measurement.unknown(unit="tokens", source="missing " + ", ".join(missing))
        return TokenBudget(window, input_tokens, response_reserve, headroom, "unknown")

    assert window.lower is not None and window.upper is not None
    assert input_tokens.lower is not None and input_tokens.upper is not None
    assert response_reserve.lower is not None and response_reserve.upper is not None
    lower = window.lower - input_tokens.upper - response_reserve.upper
    upper = window.upper - input_tokens.lower - response_reserve.lower
    headroom = Measurement(
        "tokens",
        "derived",
        "window - input_tokens - response_reserve",
        lower,
        upper,
    )
    status: BudgetStatus
    if upper < 0:
        status = "over_window"
    elif lower > 0:
        status = "within_window"
    elif lower == upper == 0:
        status = "zero_headroom"
    else:
        status = "uncertain"
    return TokenBudget(window, input_tokens, response_reserve, headroom, status)


@dataclass(frozen=True, kw_only=True)
class Route:
    model: str | None = None
    upstream: str | None = None
    request_style: str | None = None
    policy_version: str | None = None
    policy_digest: str | None = None
    tokenizer: str | None = None
    source: str | None = None
    tokenizer_source: str | None = None


@dataclass(frozen=True)
class ReportRow:
    """A stage or role turn carrying only aggregates and metadata labels."""

    stage: str
    basis: Basis
    _: KW_ONLY
    role: str | None = None
    turn: int | None = None
    session: str | None = None
    scenario: str | None = None
    reason: str | None = None
    metrics: tuple[Metric, ...] = ()
    budget: TokenBudget | None = None
    route: Route | None = None


@dataclass(frozen=True)
class ContextReport:
    rows: tuple[ReportRow, ...]
    _: KW_ONLY
    prepared_digest: str | None = None
    bundle_digest: str | None = None
    core_version: str | None = None
    operation: str | None = None
    baseline_operation: str | None = None
    baseline_version: str | None = None

    def _identity(self) -> dict[str, str | None]:
        return {
            "prepared_digest": self.prepared_digest,
            "bundle_digest": self.bundle_digest,
            "core_version": self.core_version,
            "operation": self.operation,
            "baseline_operation": self.baseline_operation,
            "baseline_version": self.baseline_version,
        }

    def as_dict(self) -> dict:
        rows = []
        for row in self.rows:
            values = asdict(row)
            values["metrics"] = list(values["metrics"])
            rows.append(values)
        return {"schema": "lab-context-report/1", **self._identity(), "rows": rows}


def render_table(report: ContextReport) -> str:
    """Render the same measurements and metadata in a terminal table."""
    lines = [
        ("Stage", "Role", "Session", "Turn", "Basis", "Field", "Value", "Provenance", "Source")
    ]
    overview: list[str] = []
    for row in report.rows:
        prefix = (
            row.stage,
            row.role or "",
            row.session or "",
            "" if row.turn is None else str(row.turn),
            row.basis,
        )
        if row.scenario is not None:
            lines.append((*prefix, "scenario", row.scenario, "", ""))
        if row.reason is not None:
            lines.append((*prefix, "reason", row.reason, "", ""))
        if row.route is not None:
            route = row.route
            details = (
                ("model", route.model, route.source),
                ("upstream", route.upstream, route.source),
                ("request_style", route.request_style, route.source),
                ("policy_version", route.policy_version, route.source),
                ("policy_digest", route.policy_digest, route.source),
                ("tokenizer", route.tokenizer, route.tokenizer_source),
            )
            lines.extend(
                (*prefix, field, value or "unknown", "", source or "")
                for field, value, source in details
            )
        metrics = row.metrics
        if row.budget is not None:
            budget = row.budget
            summary = (
                row.role or row.stage,
                row.session,
                None if row.turn is None else f"turn {row.turn}",
                row.basis,
                row.scenario,
                row.route.request_style if row.route else None,
                budget.status,
                _measurement_text(budget.headroom, signed=True),
            )
            overview.append(" | ".join(_table_cell(part) for part in summary if part))
            metrics = (
                *metrics,
                Metric("window", budget.window),
                Metric("input_tokens", budget.input_tokens),
                Metric("response_reserve", budget.response_reserve),
                Metric("headroom", budget.headroom),
            )
        for metric in metrics:
            value = metric.measurement
            lines.append(
                (*prefix, metric.name, _measurement_text(value), value.provenance, value.source)
            )
        if row.scenario is None and row.reason is None and row.route is None and not metrics:
            lines.append((*prefix, "", "", "", ""))

    cells = [tuple(_table_cell(cell) for cell in line) for line in lines]
    widths = tuple(max(map(len, column)) for column in zip(*cells, strict=True))
    formatted = [
        " | ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in cells
    ]
    formatted.insert(1, "-+-".join("-" * width for width in widths))
    identity = [
        f"{field}={_table_cell(value)}"
        for field, value in report._identity().items()
        if value is not None
    ]
    if overview:
        overview = ["Request headroom", *overview, ""]
    return "\n".join([*identity, *overview, *formatted]) + "\n"


def _measurement_text(measurement: Measurement, *, signed: bool = False) -> str:
    if measurement.lower is None:
        return f"unknown {measurement.unit}"
    number_format = "+d" if signed else "d"
    lower = format(measurement.lower, number_format)
    if measurement.lower == measurement.upper:
        return f"{lower} {measurement.unit}"
    return f"{lower}..{measurement.upper:{number_format}} {measurement.unit}"


def _table_cell(value: str) -> str:
    return "".join(character if character.isprintable() else "?" for character in value).replace(
        "|", r"\|"
    )
