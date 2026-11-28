"""Assemble the GigaCode extension from the shared Evolution guides and commands."""

from pathlib import Path
from runpy import run_path

if __name__ == "__main__":
    shared_builder = Path(__file__).resolve().parents[1] / "qwen" / "build.py"
    run_path(str(shared_builder))["main"](host="gigacode")
