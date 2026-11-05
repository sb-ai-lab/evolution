from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_builder():
    path = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "5p3-adapter-specialization"
        / "runner"
        / "build_table5_csv.py"
    )
    spec = importlib.util.spec_from_file_location("build_table5_csv_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(skill: str, condition: str, cost: float) -> dict:
    traces = 0.0 if condition == "base" else (24.0 if condition == "full_evo_24" else 8.0)
    return {
        "skill": skill,
        "condition": condition,
        "pass_rate": 0.5,
        "tokens_in": 10.0,
        "tokens_out": 5.0,
        "llm_time_ms": 100.0,
        "solve_time_ms": 20.0,
        "unique_source_trace_count": traces,
        "source_trace_count": traces,
        "cost_tokens_source_solve": cost,
        "cost_tokens_reflect": 0.0,
        "cost_tokens_distill": 0.0,
        "cost_tokens_rewrite": 0.0,
        "cost_tokens_total": cost,
        "native_evolution": condition == "full_evo_24",
        "body_changed": False,
        "eval_reused_from": "",
    }


def test_table5_view_reports_cost_relative_to_full_evo_24() -> None:
    builder = _load_builder()
    rows = [
        _row("pdf", "base", 0.0),
        _row("pdf", "m_adapter", 100.0),
        _row("pdf", "full_evo_24", 500.0),
        _row("sql", "base", 0.0),
        _row("sql", "m_adapter", 50.0),
        _row("sql", "full_evo_24", 100.0),
    ]

    aggregate = {
        row["condition"]: row for row in builder.build_view(rows, by_skill=False)
    }
    assert aggregate["base"]["cost_relative_to_full_evo_24"] == 0.0
    assert aggregate["m_adapter"]["cost_relative_to_full_evo_24"] == 0.25
    assert aggregate["full_evo_24"]["cost_relative_to_full_evo_24"] == 1.0

    by_skill = {
        (row["skill"], row["condition"]): row for row in builder.build_view(rows, by_skill=True)
    }
    assert by_skill[("pdf", "m_adapter")]["cost_relative_to_full_evo_24"] == 0.2
    assert by_skill[("sql", "m_adapter")]["cost_relative_to_full_evo_24"] == 0.5


def test_table5_denominator_validation_requires_nonzero_full_evo_24() -> None:
    builder = _load_builder()

    assert builder.validate_full_evo_24_denominator([_row("pdf", "m_adapter", 100.0)])
    assert builder.validate_full_evo_24_denominator(
        [_row("pdf", "m_adapter", 100.0), _row("pdf", "full_evo_24", 0.0)]
    )
    assert not builder.validate_full_evo_24_denominator(
        [_row("pdf", "m_adapter", 100.0), _row("pdf", "full_evo_24", 500.0)]
    )


def test_paper_table5_has_exact_paper_columns_and_percent_cost() -> None:
    builder = _load_builder()
    rows = [
        _row("pdf", "base", 0.0),
        _row("pdf", "m_adapter", 100.0),
        _row("pdf", "combined", 250.0),
        _row("pdf", "full_evo_24", 500.0),
    ]
    view = builder.build_view(rows, by_skill=False)
    paper = builder.build_paper_table5(view)

    assert list(paper[0]) == [
        "Condition",
        "Accuracy",
        "Traces",
        "Tokens",
        "Cost (% of Full-evo)",
    ]
    by_condition = {row["Condition"]: row for row in paper}
    assert by_condition["Base"] == {
        "Condition": "Base",
        "Accuracy": 0.5,
        "Traces": 0,
        "Tokens": 0,
        "Cost (% of Full-evo)": 0.0,
    }
    assert by_condition["A_M"]["Cost (% of Full-evo)"] == 20.0
    assert by_condition["A_MRT"]["Cost (% of Full-evo)"] == 50.0
    assert by_condition["Full-evo (N=24)"]["Cost (% of Full-evo)"] == 100.0
