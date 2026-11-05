"""Traces page — global cross-skill trace explorer."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st


def render(get_store, project_dir: Path) -> None:
    st.title("Traces Explorer")

    store = get_store()
    entries = store.list()

    if not entries:
        st.warning("No skills found.")
        return

    from evolution.lineage.traces import TraceStore

    # Load all traces across all skills
    all_traces = []
    trace_stores = {}
    for entry in entries:
        skill = store.get(entry.name)
        ts = TraceStore(skill)
        trace_stores[entry.name] = ts
        for t in ts.list():
            t["skill_name"] = entry.name
            all_traces.append(t)

    if not all_traces:
        st.info("No traces recorded yet. Run `evo solve` to generate traces.")
        return

    # ── Metrics ─────────────────────────────────────────────────────
    total = len(all_traces)
    passing = sum(1 for t in all_traces if t.get("success", False))
    avg_rate = sum(t.get("pass_rate", 0) for t in all_traces) / total

    cols = st.columns(4)
    cols[0].metric("Total Traces", total)
    cols[1].metric("Passing", f"{passing}/{total}")
    cols[2].metric("Avg Pass Rate", f"{avg_rate:.0%}")
    cols[3].metric("Skills with Traces", len(trace_stores))

    # ── Filters ─────────────────────────────────────────────────────
    all_skills = sorted(set(t["skill_name"] for t in all_traces))
    all_models = sorted(set(t.get("model", "") for t in all_traces))
    all_tasks = sorted(set(t.get("task", "") for t in all_traces))
    all_versions = sorted(set(t.get("skill_version", "") for t in all_traces))

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        sel_skill = st.selectbox("Skill", ["All"] + all_skills, key="tr_skill")
    with col2:
        sel_model = st.selectbox("Model", ["All"] + all_models, key="tr_model")
    with col3:
        sel_task = st.selectbox("Task", ["All"] + all_tasks, key="tr_task")
    with col4:
        sel_version = st.selectbox("Version", ["All"] + all_versions, key="tr_version")

    filtered = all_traces
    if sel_skill != "All":
        filtered = [t for t in filtered if t["skill_name"] == sel_skill]
    if sel_model != "All":
        filtered = [t for t in filtered if t.get("model", "") == sel_model]
    if sel_task != "All":
        filtered = [t for t in filtered if t.get("task", "") == sel_task]
    if sel_version != "All":
        filtered = [t for t in filtered if t.get("skill_version", "") == sel_version]

    if not filtered:
        st.warning("No traces match filters.")
        return

    # ── Traces table ────────────────────────────────────────────────
    rows = []
    for i, t in enumerate(filtered, 1):
        passed = t.get("passed", 0)
        total_tests = t.get("total", 0)
        rows.append(
            {
                "#": i,
                "Skill": t["skill_name"],
                "Version": t.get("skill_version", ""),
                "Model": _short_model(t.get("model", "")),
                "Task": t.get("task", ""),
                "Result": f"{passed}/{total_tests}",
                "Pass Rate": t.get("pass_rate", 0),
                "Tokens In": t.get("tokens_in", 0),
                "Tokens Out": t.get("tokens_out", 0),
                "Time (ms)": t.get("time_ms", 0),
            }
        )

    df = pd.DataFrame(rows)

    def _color_result(val):
        if "/" in str(val):
            parts = str(val).split("/")
            if parts[0] == parts[1] and parts[1] != "0":
                return "color: #4caf50"
            elif parts[0] == "0":
                return "color: #f44336"
        return ""

    st.dataframe(
        df.style.map(_color_result, subset=["Result"]),
        width="stretch",
        hide_index=True,
    )

    st.caption(f"{len(filtered)} traces")

    # ── Summary charts ──────────────────────────────────────────────
    tab_by_skill, tab_by_model, tab_detail = st.tabs(["By Skill", "By Model", "Trace Detail"])

    with tab_by_skill:
        _render_by_skill(filtered)

    with tab_by_model:
        _render_by_model(filtered)

    with tab_detail:
        _render_detail(filtered, trace_stores)


# ── By Skill chart ──────────────────────────────────────────────────


def _render_by_skill(traces: list[dict]) -> None:
    """Average pass rate per skill."""
    from collections import defaultdict

    agg = defaultdict(list)
    for t in traces:
        agg[t["skill_name"]].append(t.get("pass_rate", 0))

    rows = [
        {"Skill": name, "Avg Pass Rate": sum(rates) / len(rates), "Traces": len(rates)}
        for name, rates in sorted(agg.items())
    ]
    df = pd.DataFrame(rows)

    import plotly.express as px

    fig = px.bar(
        df,
        x="Skill",
        y="Avg Pass Rate",
        text_auto=".0%",
        color="Avg Pass Rate",
        color_continuous_scale="RdYlGn",
        range_color=[0, 1],
    )
    fig.update_layout(
        yaxis_tickformat=".0%",
        yaxis_range=[0, 1.05],
        height=350,
        showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")


# ── By Model chart ──────────────────────────────────────────────────


def _render_by_model(traces: list[dict]) -> None:
    """Average pass rate per model."""
    from collections import defaultdict

    agg = defaultdict(list)
    for t in traces:
        model = _short_model(t.get("model", "unknown"))
        agg[model].append(t.get("pass_rate", 0))

    rows = [
        {"Model": name, "Avg Pass Rate": sum(rates) / len(rates), "Traces": len(rates)}
        for name, rates in sorted(agg.items())
    ]
    df = pd.DataFrame(rows)

    import plotly.express as px

    fig = px.bar(
        df,
        x="Model",
        y="Avg Pass Rate",
        text_auto=".0%",
        color="Avg Pass Rate",
        color_continuous_scale="RdYlGn",
        range_color=[0, 1],
    )
    fig.update_layout(
        yaxis_tickformat=".0%",
        yaxis_range=[0, 1.05],
        height=350,
        showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")


# ── Trace Detail ────────────────────────────────────────────────────


def _render_detail(traces: list[dict], trace_stores: dict) -> None:
    """View individual trace files."""
    if not traces:
        return

    trace_idx = st.selectbox(
        "Select trace",
        range(1, len(traces) + 1),
        format_func=lambda i: (
            f"#{i} — {traces[i - 1]['skill_name']} / "
            f"{_short_model(traces[i - 1].get('model', ''))} / "
            f"{traces[i - 1].get('task', '')}"
        ),
        key="tr_detail_idx",
    )

    trace = traces[trace_idx - 1]
    skill_name = trace["skill_name"]
    ts = trace_stores.get(skill_name)
    if not ts:
        st.error(f"TraceStore not found for {skill_name}")
        return

    trace_dir = ts.traces_dir / trace["id"]
    if not trace_dir.is_dir():
        st.warning(f"Trace directory not found: {trace_dir}")
        return

    # Header
    passed = trace.get("passed", 0)
    total_tests = trace.get("total", 0)
    success = trace.get("success", False)
    status = f"PASS {passed}/{total_tests}" if success else f"FAIL {passed}/{total_tests}"

    st.markdown(
        f"**Skill:** {skill_name} ({trace.get('skill_version', '')}) | "
        f"**Model:** {trace.get('model', '')} | "
        f"**Task:** {trace.get('task', '')} | "
        f"**Result:** {status} | "
        f"**Tokens:** {trace.get('tokens_in', 0)}+{trace.get('tokens_out', 0)} | "
        f"**Time:** {trace.get('time_ms', 0)}ms"
    )

    sub_tabs = st.tabs(["Response", "Code", "Prompt", "Result"])

    with sub_tabs[0]:
        _show_file(trace_dir / "response.md", "markdown")
    with sub_tabs[1]:
        _show_file(trace_dir / "solve.py", "python")
    with sub_tabs[2]:
        _show_file(trace_dir / "prompt.md", "markdown")
    with sub_tabs[3]:
        _show_file(trace_dir / "result.json", "json")


# ── Helpers ─────────────────────────────────────────────────────────


def _short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1] if "/" in model else model


def _show_file(path: Path, language: str) -> None:
    if not path.exists():
        st.info(f"{path.name} not found.")
        return

    content = path.read_text(encoding="utf-8")

    if language == "json":
        import json

        try:
            st.json(json.loads(content))
        except json.JSONDecodeError:
            st.code(content, language="json")
    elif language == "python":
        st.code(content, language="python")
    else:
        if len(content) > 5000:
            st.markdown(content[:5000] + "\n\n*... truncated ...*")
            with st.expander("Full content"):
                st.code(content, language="markdown")
        else:
            st.markdown(content)
