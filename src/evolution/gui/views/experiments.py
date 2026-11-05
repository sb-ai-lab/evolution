"""Experiments page — view experiment runs and skill evolution diffs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from evolution.gui.iterative_evolution import (
    build_unified_diff,
    default_diff_versions,
    discover_iterative_runs,
    discover_skill_artifacts,
    metric_delta,
    metric_value,
    render_side_by_side_html,
)


def render(get_store, project_dir: Path) -> None:
    st.title("Experiments")

    tab_iterative, tab_legacy = st.tabs(["Iterative Evolution", "Legacy Experiments"])
    with tab_iterative:
        _render_iterative_evolution(project_dir)
    with tab_legacy:
        _render_legacy_experiments(get_store, project_dir)


def _render_legacy_experiments(get_store, project_dir: Path) -> None:
    from evolution.core.experiments import experiments_root, list_experiments

    exps = list_experiments(project_dir)
    if not exps:
        st.warning("No experiments found. Create one with `evo create-experiment`.")
        return

    # ── Experiment selector ────────────────────────────────────────
    exp_names = [e["name"] for e in exps]
    selected = st.selectbox("Experiment", exp_names, key="exp_select")
    exp = next(e for e in exps if e["name"] == selected)

    # ── Metadata ───────────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)
    col1.metric("Skills", exp.get("skill_count", 0))
    forked = exp.get("forked_from", {})
    col2.metric("Forked from", ", ".join(f"{k}@{v}" for k, v in forked.items()) or "-")
    col3.metric("Created", exp.get("created_at", "")[:10])

    if exp.get("description"):
        st.caption(exp["description"])

    # ── Lineage ────────────────────────────────────────────────────
    exp_dir = experiments_root(project_dir) / selected
    _render_lineage(exp_dir)

    st.divider()

    # ── Evolution chart ────────────────────────────────────────────
    _render_evolution_chart(selected, project_dir)

    # ── Reflections summary ────────────────────────────────────────
    _render_reflections(exp_dir)


# ── Iterative evolution tab ─────────────────────────────────────────


def _render_iterative_evolution(project_dir: Path) -> None:
    """Show diffs for experiments/iterative_evolution run artifacts."""
    runs_root = project_dir / "experiments" / "iterative_evolution" / "runs"
    runs = discover_iterative_runs(runs_root)
    if not runs:
        st.warning(f"No iterative-evolution runs found under `{runs_root}`.")
        return

    selected_run = st.selectbox(
        "Run / configuration",
        runs,
        format_func=_format_run_label,
        key="iter_run",
    )
    _render_iterative_run_summary(selected_run)

    cells = selected_run.cells
    filtered_cells = _filter_iterative_cells(cells)
    if not filtered_cells:
        st.info("No cells match the selected filters.")
        return

    _render_cell_table(filtered_cells)
    selected_cell = st.selectbox(
        "Cell",
        filtered_cells,
        format_func=_format_cell_label,
        key=f"iter_cell_{selected_run.name}",
    )
    _render_cell_metrics(selected_cell)

    artifacts = discover_skill_artifacts(
        selected_cell.path,
        skill=selected_cell.skill,
        best_version=selected_cell.best_version,
    )
    if not artifacts:
        st.warning("No versioned skill-text artifacts found for this cell.")
        st.caption(str(selected_cell.path))
        return

    st.subheader("Skill Diffs")
    artifact = st.selectbox(
        "Artifact",
        artifacts,
        format_func=lambda item: f"{item.label} ({len(item.versions)} versions)",
        key=(
            f"iter_artifact_{selected_run.name}_{selected_cell.schema}_"
            f"{selected_cell.framework}_{selected_cell.skill}"
        ),
    )
    versions = sorted(artifact.versions, key=_version_sort_key)
    left_default, right_default = default_diff_versions(artifact, selected_cell.best_version)
    left, right = _render_version_selectors(
        versions=versions,
        left_default=left_default,
        right_default=right_default,
        key_prefix=(
            f"iter_diff_{selected_run.name}_{selected_cell.schema}_"
            f"{selected_cell.framework}_{selected_cell.skill}_{artifact.label}"
        ),
    )
    if not left or not right:
        st.info("This artifact does not have enough readable versions to diff.")
        return

    old = artifact.versions[left]
    new = artifact.versions[right]
    st.caption(f"From `{_rel_to_project(old.path, project_dir)}`")
    st.caption(f"To `{_rel_to_project(new.path, project_dir)}`")

    left_label = f"{artifact.label}@{left}"
    right_label = f"{artifact.label}@{right}"

    if old.text == new.text:
        st.success("No text changes between the selected versions.")
        st.code(old.text or "(empty)", language="markdown")
        return

    diff_key = (
        f"iter_diffview_{selected_run.name}_{selected_cell.schema}_"
        f"{selected_cell.framework}_{selected_cell.skill}_{artifact.label}_{left}_{right}"
    )
    context_only = st.checkbox("Context only (±3 lines)", value=False, key=f"{diff_key}_ctx")
    sxs_tab, uni_tab, left_tab, right_tab = st.tabs(
        ["Side-by-side", "Unified diff", f"{left} raw", f"{right} raw"]
    )
    with sxs_tab:
        html, height = render_side_by_side_html(
            old.text, new.text, left_label, right_label, context=context_only
        )
        components.html(html, height=height, scrolling=True)
    with uni_tab:
        unified = build_unified_diff(
            old_text=old.text,
            new_text=new.text,
            old_label=left_label,
            new_label=right_label,
        )
        st.code(unified or "(no diff)", language="diff")
    with left_tab:
        st.code(old.text or "(empty)", language="markdown")
    with right_tab:
        st.code(new.text or "(empty)", language="markdown")


def _render_iterative_run_summary(run) -> None:
    progress = run.progress or {}
    metrics_latest = run.metrics_latest or {}
    cols = st.columns(5)
    cols[0].metric("Kind", run.kind)
    cols[1].metric("Cells", len(run.cells))
    cols[2].metric("Complete", progress.get("complete", "-"))
    cols[3].metric("Failed", progress.get("failed_count", "-"))
    updated_at = metrics_latest.get("updated_at") or progress.get("updated_at")
    cols[4].metric("Updated", _short_date(updated_at))
    st.caption(str(run.path))

    summary = metrics_latest.get("summary")
    if isinstance(summary, list) and summary:
        import pandas as pd

        df = pd.DataFrame(summary)
        preferred = [
            "schema",
            "framework",
            "split",
            "n_cells",
            "seed_mean_pass_rate",
            "evolved_mean_pass_rate",
            "delta_mean_pass_rate",
            "seed_pass@5",
            "evolved_pass@5",
            "delta_pass@5",
        ]
        cols = [col for col in preferred if col in df.columns]
        with st.expander("Run summary", expanded=False):
            st.dataframe(df[cols] if cols else df, width="stretch", hide_index=True)


def _filter_iterative_cells(cells: list[Any]) -> list[Any]:
    schemas = sorted({cell.schema for cell in cells})
    frameworks = sorted({cell.framework for cell in cells})
    skills = sorted({cell.skill for cell in cells})

    c_schema, c_framework, c_skill, c_search = st.columns([1, 1, 1, 1.5])
    with c_schema:
        schema_filter = st.selectbox("Schema", ["All"] + schemas, key="iter_schema")
    with c_framework:
        framework_filter = st.selectbox("Framework", ["All"] + frameworks, key="iter_framework")
    with c_skill:
        skill_filter = st.selectbox("Skill", ["All"] + skills, key="iter_skill")
    with c_search:
        query = st.text_input("Search", key="iter_search").strip().lower()

    out = []
    for cell in cells:
        if schema_filter != "All" and cell.schema != schema_filter:
            continue
        if framework_filter != "All" and cell.framework != framework_filter:
            continue
        if skill_filter != "All" and cell.skill != skill_filter:
            continue
        haystack = f"{cell.schema} {cell.framework} {cell.skill} {cell.path}".lower()
        if query and query not in haystack:
            continue
        out.append(cell)
    return out


def _render_cell_table(cells: list[Any]) -> None:
    import pandas as pd

    rows = []
    for cell in cells:
        rows.append(
            {
                "Schema": cell.schema,
                "Framework": cell.framework,
                "Skill": cell.skill,
                "Lane": cell.primary_eval_lane,
                "Best": cell.best_version,
                "Train Seed": _format_pct(metric_value(cell, "train", "seed", "mean_pass_rate")),
                "Train Evolved": _format_pct(
                    metric_value(cell, "train", "evolved", "mean_pass_rate")
                ),
                "Train Delta": _format_delta(metric_delta(cell, "train", "delta_mean_pass_rate")),
                "Test Seed": _format_pct(metric_value(cell, "test", "seed", "mean_pass_rate")),
                "Test Evolved": _format_pct(
                    metric_value(cell, "test", "evolved", "mean_pass_rate")
                ),
                "Test Delta": _format_delta(metric_delta(cell, "test", "delta_mean_pass_rate")),
            }
        )
    with st.expander("Matching cells", expanded=True):
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_cell_metrics(cell) -> None:
    st.subheader(f"{cell.framework} / {cell.skill}")
    cols = st.columns(5)
    cols[0].metric("Schema", cell.schema)
    cols[1].metric("Best version", cell.best_version)
    cols[2].metric("Eval lane", cell.primary_eval_lane or "-")
    cols[3].metric(
        "Train delta",
        _format_delta(metric_delta(cell, "train", "delta_mean_pass_rate")),
    )
    cols[4].metric(
        "Test delta",
        _format_delta(metric_delta(cell, "test", "delta_mean_pass_rate")),
    )

    if cell.metrics:
        with st.expander("Cell metadata"):
            meta = {
                "path": str(cell.path),
                "train_refs": cell.metrics.get("train_refs"),
                "test_refs": cell.metrics.get("test_refs"),
                "n_train": cell.metrics.get("n_train"),
                "n_test": cell.metrics.get("n_test"),
                "n_trials": cell.metrics.get("n_trials"),
                "rounds": cell.metrics.get("rounds"),
            }
            st.json({k: v for k, v in meta.items() if v is not None})


def _render_version_selectors(
    *,
    versions: list[str],
    left_default: str,
    right_default: str,
    key_prefix: str,
) -> tuple[str, str]:
    c_left, c_right = st.columns(2)
    left_index = versions.index(left_default) if left_default in versions else 0
    right_index = versions.index(right_default) if right_default in versions else len(versions) - 1
    with c_left:
        left = st.selectbox("From", versions, index=left_index, key=f"{key_prefix}_left")
    with c_right:
        right = st.selectbox("To", versions, index=right_index, key=f"{key_prefix}_right")
    return left, right


def _render_lineage(exp_dir: Path):
    """Show skill lineage within the experiment."""
    st.subheader("Skill Lineage")

    skills_dir = exp_dir / "skills"
    if not skills_dir.is_dir():
        st.info("No skills in this experiment.")
        return

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        lineage_path = skill_dir / ".evolution" / "lineage.json"
        if not lineage_path.exists():
            continue

        data = json.loads(lineage_path.read_text())
        skill_name = data.get("skill_id", skill_dir.name)
        current = data.get("current_version", "?")
        entries = data.get("lineage", [])

        versions_str = " → ".join(
            f"**{e['version']}**" if e.get("status") == "active" else e["version"] for e in entries
        )
        st.markdown(f"**{skill_name}**: {versions_str} (current: {current})")

        # Show messages
        for e in entries:
            msg = e.get("message", "")
            origin = e.get("origin", "")
            if msg:
                st.caption(f"  {e['version']}: {msg}")
            elif origin:
                st.caption(f"  {e['version']}: ({origin})")


def _render_evolution_chart(exp_name: str, project_dir: Path):
    """Show pass rate per round per model as a chart."""
    st.subheader("Evolution Progress")

    # Collect results from all collections matching this experiment
    results_dir = project_dir / "results"
    if not results_dir.is_dir():
        st.info("No results found.")
        return

    rows = []
    for model_dir in results_dir.iterdir():
        if not model_dir.is_dir():
            continue
        csv_path = model_dir / "results.csv"
        if not csv_path.exists():
            continue

        model_tag = model_dir.name
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                collection = row.get("collection", "")
                if not collection.startswith(exp_name):
                    continue
                row["model"] = model_tag
                rows.append(row)

    if not rows:
        st.info("No benchmark results for this experiment yet.")
        return

    import pandas as pd

    df = pd.DataFrame(rows)
    df["pass_rate"] = pd.to_numeric(df["pass_rate"], errors="coerce")

    # Extract round from collection name (e.g., "reflect-ablation-r0" -> "r0")
    df["round"] = df["collection"].str.extract(r"(r\d+)$")
    df["round"] = df["round"].fillna(df["collection"])

    # Short model name
    df["model"] = df.apply(
        lambda r: _model_short(r.get("model", "") or _infer_model(r)),
        axis=1,
    )

    # Filter to with-skill only for evolution chart
    skill_df = df[df["condition"] == "with-skill"].copy()

    if skill_df.empty:
        st.info("No with-skill results found.")
        return

    # Aggregate: mean pass rate per round per model
    agg = skill_df.groupby(["round", "model"])["pass_rate"].mean().reset_index()
    agg["pass_rate_pct"] = (agg["pass_rate"] * 100).round(1)

    # Sort rounds naturally
    round_order = sorted(
        agg["round"].unique(), key=lambda x: int(x[1:]) if x[1:].isdigit() else 999
    )
    agg["round"] = pd.Categorical(agg["round"], categories=round_order, ordered=True)

    # Pivot for chart
    pivot = agg.pivot(index="round", columns="model", values="pass_rate_pct")
    pivot = pivot.sort_index()

    st.line_chart(pivot, height=400)

    # Also show as table
    with st.expander("Results table"):
        # Detailed table: round × model with mean ± individual trials
        detail_rows = []
        for _, row in skill_df.iterrows():
            detail_rows.append(
                {
                    "Round": row["round"],
                    "Model": row["model"],
                    "Trial": row.get("trial", ""),
                    "Passed": row.get("passed", ""),
                    "Total": row.get("total", ""),
                    "Pass Rate": f"{row['pass_rate']:.0%}",
                    "Errors": (row.get("errors", "") or "")[:60],
                }
            )
        detail_df = pd.DataFrame(detail_rows)
        st.dataframe(detail_df, width="stretch", hide_index=True)

    # No-skill baseline comparison
    baseline_df = df[df["condition"] == "no-skill"]
    if not baseline_df.empty:
        with st.expander("No-skill baseline (r0)"):
            base_agg = baseline_df.groupby("model")["pass_rate"].mean().reset_index()
            base_agg["pass_rate"] = (base_agg["pass_rate"] * 100).round(1)
            base_agg.columns = ["Model", "Baseline Pass Rate (%)"]
            st.dataframe(base_agg, width="stretch", hide_index=True)


def _render_reflections(exp_dir: Path):
    """Show reflection summaries from traces."""
    st.subheader("Reflections")

    skills_dir = exp_dir / "skills"
    if not skills_dir.is_dir():
        return

    all_reflections = []
    for skill_dir in sorted(skills_dir.iterdir()):
        traces_dir = skill_dir / ".traces"
        if not traces_dir.is_dir():
            continue

        for trace_dir in sorted(traces_dir.iterdir()):
            if not trace_dir.is_dir():
                continue
            ref_path = trace_dir / "reflection.json"
            if not ref_path.exists():
                continue

            try:
                ref = json.loads(ref_path.read_text())
                # Get skill version from result.json
                result_path = trace_dir / "result.json"
                if result_path.exists():
                    result = json.loads(result_path.read_text())
                    ref["_skill_version"] = result.get("skill_version", "?")
                    ref["_pass_rate_actual"] = result.get("pass_rate", 0)
                all_reflections.append(ref)
            except Exception:
                continue

    if not all_reflections:
        st.info("No reflections yet. Run `evo reflect` to analyze failing traces.")
        return

    # Summary by pattern
    from collections import Counter

    import pandas as pd

    patterns = Counter(r.get("pattern", "unknown") for r in all_reflections if "error" not in r)
    severities = Counter(r.get("severity", "unknown") for r in all_reflections if "error" not in r)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Reflections", len(all_reflections))
    col2.metric("Critical", severities.get("critical", 0))
    col3.metric("Major", severities.get("major", 0))

    # Pattern breakdown
    with st.expander("Failure patterns"):
        pattern_df = pd.DataFrame(
            [{"Pattern": p, "Count": c} for p, c in patterns.most_common()],
        )
        st.dataframe(pattern_df, width="stretch", hide_index=True)

    # Group by skill version
    by_version = {}
    for r in all_reflections:
        ver = r.get("_skill_version", "?")
        by_version.setdefault(ver, []).append(r)

    version_filter = st.selectbox(
        "Filter by skill version",
        ["All"] + sorted(by_version.keys()),
        key="ref_version_filter",
    )

    filtered = all_reflections if version_filter == "All" else by_version.get(version_filter, [])

    # Table
    ref_rows = []
    for r in filtered:
        if "error" in r:
            continue
        model = r.get("model", "?")
        ref_rows.append(
            {
                "Version": r.get("_skill_version", "?"),
                "Model": model.split("/")[-1] if "/" in model else model,
                "Severity": r.get("severity", "?"),
                "Pattern": r.get("pattern", "?"),
                "Root Cause": (r.get("root_cause", "?"))[:100],
                "Fix": (r.get("suggested_fix", "?"))[:100],
            }
        )

    if ref_rows:
        ref_df = pd.DataFrame(ref_rows)

        def _color_severity(val):
            colors = {"critical": "#f44336", "major": "#ff9800", "minor": "#4caf50"}
            return f"color: {colors.get(val, '')}"

        st.dataframe(
            ref_df.style.map(_color_severity, subset=["Severity"]),
            width="stretch",
            hide_index=True,
        )


# ── Helpers ─────────────────────────────────────────────────────────


def _format_run_label(run) -> str:
    return f"{run.name} | {run.kind} | {len(run.cells)} cells"


def _format_cell_label(cell) -> str:
    lane = f", {cell.primary_eval_lane}" if cell.primary_eval_lane else ""
    return f"{cell.schema} / {cell.framework} / {cell.skill} (best {cell.best_version}{lane})"


def _short_date(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "-"
    return text[:19].replace("T", " ")


def _format_pct(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.1%}"
    return "-"


def _format_delta(value: Any) -> str:
    if isinstance(value, (int, float)):
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.1%}"
    return "-"


def _rel_to_project(path: Path, project_dir: Path) -> str:
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


def _version_sort_key(version: str) -> tuple[int, str]:
    if version.startswith("v") and version[1:].isdigit():
        return (int(version[1:]), version)
    return (10**9, version)


def _model_short(model_str: str) -> str:
    """Shorten model name for display."""
    if "/" in model_str:
        return model_str.rsplit("/", 1)[-1]
    return model_str


def _infer_model(row) -> str:
    """Try to infer model from directory structure if not in CSV."""
    return row.get("model", "unknown")
