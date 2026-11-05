#!/usr/bin/env python3
"""Diff two baseline-stability run folders' scorecards (read-only).

    python compare_stability.py --a stability/<runA> --b stability/<runB> [--out compare.md]

Prints per (skill,split,condition) cell deltas (B - A) for the headline
stability metrics, so two configs/models can be compared directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

KEY = ("skill", "split", "condition")
METRICS = ["mean_pass_rate", "std_pass_rate", "full_solve_rate", "best_of_n", "mean_solved_tests"]


def load_cards(run_dir: Path) -> dict[tuple, dict]:
    data = json.loads((run_dir / "results" / "scorecard.json").read_text(encoding="utf-8"))
    return {tuple(c[k] for k in KEY): c for c in data["cards"]}


def _d(a: Any, b: Any) -> str:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return f"{b - a:+.3f}"
    return "-"


def _f(x: Any) -> str:
    return f"{x:.3f}" if isinstance(x, (int, float)) else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--a", type=Path, required=True, help="baseline run dir A")
    ap.add_argument("--b", type=Path, required=True, help="run dir B (compared against A)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    A, B = load_cards(args.a), load_cards(args.b)
    rows = [
        "# Stability compare (B - A)",
        f"- A: `{args.a}`",
        f"- B: `{args.b}`",
        "",
        "| skill | split | condition | " + " | ".join(f"Δ{m}" for m in METRICS) + " | (A→B mean) |",
        "|" + "---|" * (len(METRICS) + 4),
    ]
    for key in sorted(set(A) | set(B)):
        ca, cb = A.get(key, {}), B.get(key, {})
        deltas = " | ".join(_d(ca.get(m), cb.get(m)) for m in METRICS)
        trend = f"{_f(ca.get('mean_pass_rate'))}→{_f(cb.get('mean_pass_rate'))}"
        rows.append(f"| {key[0]} | {key[1]} | {key[2]} | {deltas} | {trend} |")
    text = "\n".join(rows)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
