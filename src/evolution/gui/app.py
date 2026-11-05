"""Evolution GUI — Streamlit app for managing LLM agent skills."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Evolution — Skills Manager",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Project root — resolve from this file or fall back to cwd
_HERE = Path(__file__).resolve().parent
PROJECT_DIR = _HERE.parents[2]  # .../evolution/src/evolution/gui -> .../evolution
if not (PROJECT_DIR / ".agents").exists():
    PROJECT_DIR = Path.cwd()


def get_store():
    """Cached SkillStore instance."""
    from evolution.core.store import SkillStore

    return SkillStore(project_dir=PROJECT_DIR, user_dir=Path.home())


# ── Sidebar navigation ─────────────────────────────────────────────
st.sidebar.title("Evolution")
page = st.sidebar.radio(
    "Navigate",
    ["Skills", "Tasks", "Benchmarks", "Traces", "Experiments"],
    label_visibility="collapsed",
)

# ── Page routing ────────────────────────────────────────────────────
if page == "Skills":
    from evolution.gui.views.skills import render

    render(get_store, PROJECT_DIR)
elif page == "Tasks":
    from evolution.gui.views.tasks import render as render_tasks

    render_tasks(get_store, PROJECT_DIR)
elif page == "Benchmarks":
    from evolution.gui.views.benchmarks import render as render_benchmarks

    render_benchmarks(get_store, PROJECT_DIR)
elif page == "Traces":
    from evolution.gui.views.traces import render as render_traces

    render_traces(get_store, PROJECT_DIR)
elif page == "Experiments":
    from evolution.gui.views.experiments import render as render_experiments

    render_experiments(get_store, PROJECT_DIR)
