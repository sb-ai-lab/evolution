#!/usr/bin/env python3
"""Baseline entrypoint for the AFTER train/test subset sweep.

The implementation lives in ``experiments/iterative_evolution`` because it is
the shared iterative loop used by these baselines. This wrapper keeps the
reproducible baseline command under ``experiments/baselines`` and makes child
cell retries relaunch through the same baseline entrypoint.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "experiments"
if str(EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS))

from iterative_evolution.subset_sweep import main as _main  # noqa: E402


def _arg_value(args: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for idx, arg in enumerate(args):
        if arg == name and idx + 1 < len(args):
            return args[idx + 1]
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def _write_metrics_report(args: list[str]) -> None:
    if "--run-cell" in args:
        return
    root_arg = _arg_value(args, "--root")
    if not root_arg:
        return
    try:
        from write_metrics_report import render_report

        root = Path(root_arg).resolve()
        text = render_report(root)
        (root / "metrics_partial.md").write_text(text, encoding="utf-8")
        iterative_runs = REPO_ROOT / "experiments" / "iterative_evolution" / "runs"
        if iterative_runs in root.parents:
            mirror = REPO_ROOT / "experiments" / "baselines" / "runs" / root.name / "metrics_partial.md"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(text, encoding="utf-8")
    except Exception as exc:
        print(f"warning: failed to write baselines metrics markdown: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--entry-script" not in args:
        args.extend(["--entry-script", str(Path(__file__).resolve())])
    rc = _main(args)
    _write_metrics_report(args)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
