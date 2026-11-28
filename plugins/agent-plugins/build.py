"""Assemble the portable Agent Plugins package from the Evolution guides."""

from pathlib import Path
from runpy import run_path

if __name__ == "__main__":
    shared_builder = Path(__file__).resolve().parents[1] / "qwen" / "build.py"
    run_path(str(shared_builder))["main"](host="agent-plugins")
