#!/usr/bin/env python3
"""Baseline-only stability probe for the AFTER train/test subset.

Solve each (skill, split, condition, task, trial) cell once through
``_shared.native_framework_runners.run_evolution_test`` (which drives AFTER
tasks via materialize -> solve.sh -> role-python pytest and calls
``evolution.llm.litellm_backend.LiteLLMBackend`` internally), then emit a
stability scorecard + a reusable solver-params artifact so a later
framework-evolution run can inherit the exact solver config probed here.

No reflect / rewrite / skill mutation — this measures the raw model only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

HERE = Path(__file__).resolve().parent
EVO_ROOT = HERE.parents[1]  # .../evolution
AFTER_ROOT = EVO_ROOT.parent / "datasets" / "AFTER"
STABILITY_ROOT = HERE / "stability"
DEFAULT_SUBSET = HERE / "subset_rich.json"

sys.path.insert(0, str(EVO_ROOT / "src"))
sys.path.insert(0, str(EVO_ROOT / "experiments"))

from _shared import native_framework_runners as nfr  # noqa: E402
from afterbench_smoke import _task_skill_role  # noqa: E402

DEFAULT_SKILLS = ["pdf", "sql", "xlsx"]
CONDITIONS_DEFAULT = ["no_skill", "handcraft"]
EPS = 1e-6
STD_HI = 0.15  # per-trial std above this = "high variance" in the report read


# ── reasoning: one knob, mapped per model family ──────────────────────────────
def reasoning_extra_body(model_id: str, reasoning: str) -> tuple[dict[str, Any], str]:
    """Map a single --reasoning value to the right extra_body for the model.

    Returns (extra_body, note). ``note`` records any lossy mapping so the run
    config is honest about what the model actually supports.
    """
    m = (model_id or "").lower()
    r = reasoning.lower()
    if "qwen3" in m:
        # Qwen3 has no effort levels; only enable_thinking on/off is real.
        on = r != "off"
        note = "" if r in ("off", "on") else f"qwen3 has no effort levels; '{r}' treated as on"
        return {"chat_template_kwargs": {"enable_thinking": on}}, note
    if "gpt-oss" in m:
        if r == "off":
            return {
                "reasoning_effort": "low"
            }, "gpt-oss cannot hard-disable reasoning; using effort=low"
        return {"reasoning_effort": "medium" if r == "on" else r}, ""
    # generic remote (OpenRouter): use the unified `reasoning` object so off/on
    # actually toggle thinking — GLM and other hybrids default thinking ON, so a
    # bare "no key" would not disable it. effort levels pass through as-is.
    if r == "off":
        return {"reasoning": {"enabled": False}}, ""
    if r == "on":
        return {"reasoning": {"enabled": True}}, ""
    return {"reasoning": {"effort": r}}, ""


def reasoning_to_evo_effort(reasoning: str) -> str:
    """Map --reasoning to the evolution launcher's --solver-reasoning-effort."""
    return {"off": "none", "on": "medium"}.get(reasoning.lower(), reasoning.lower())


def is_local(api_base: str) -> bool:
    return any(h in (api_base or "") for h in ("localhost", "127.0.0.1", "0.0.0.0"))


def sanitize(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in text).strip("-")


# ── config identity (resume guard) ────────────────────────────────────────────
# Fields that define what a trial *does*. Two runs with the same fingerprint are
# the same configuration, so --resume may add more trials to the same folder.
# Scope (skills/n_train/n_test/n_trials/conditions) may grow freely — it only
# adds cells, never changes existing ones.
_IDENTITY_KEYS = (
    "model",
    "served_id",
    "api_base",
    "api_key_env",
    "temperature",
    "top_p",
    "max_tokens",
    "reasoning",
    "extra_body",
    "base_seed",
)


def solver_fingerprint(cfg: dict) -> str:
    blob = json.dumps({k: cfg[k] for k in _IDENTITY_KEYS}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def identity_diff(prior: dict, cfg: dict) -> dict:
    return {k: (prior.get(k), cfg.get(k)) for k in _IDENTITY_KEYS if prior.get(k) != cfg.get(k)}


# ── paid-API cost estimate ──────────────────────────────────────────────────────
CHARS_PER_TOK = 4
EST_LISTING_CHARS = 1500  # rough workspace-listing block added to each prompt
DEFAULT_EST_TOKENS_OUT = 1200


def _http_get_json(url: str) -> dict | None:
    """GET JSON via urllib, falling back to curl. This box's proxy is an
    https:// scheme proxy that urllib can't CONNECT-tunnel but curl can."""
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    try:
        import subprocess

        out = subprocess.run(
            ["curl", "-s", "--max-time", "20", url], capture_output=True, text=True, timeout=25
        )
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except Exception:
        pass
    return None


def fetch_openrouter_pricing(model_id: str) -> tuple[float, float] | None:
    """(prompt_$/tok, completion_$/tok) from OpenRouter's public model list, or None."""
    mid = model_id[len("openrouter/") :] if model_id.startswith("openrouter/") else model_id
    data = _http_get_json("https://openrouter.ai/api/v1/models")
    if not data:
        return None
    for m in data.get("data", []):
        if m.get("id") == mid:
            pr = m.get("pricing") or {}
            try:
                return float(pr["prompt"]), float(pr["completion"])
            except (KeyError, ValueError, TypeError):
                return None
    return None


def estimate_tokens(work: list, est_tokens_out: int) -> tuple[int, int]:
    sys_chars = len(getattr(nfr, "EVOLUTION_SYSTEM", ""))
    instr_cache: dict[str, int] = {}
    tin = tout = 0.0
    for _s, _sp, _c, task_rel, _t, body, _cd in work:
        if task_rel not in instr_cache:
            td = _task_skill_role(task_rel)[4]
            instr_cache[task_rel] = len((td / "instruction.md").read_text(encoding="utf-8"))
        tin += (
            sys_chars + instr_cache[task_rel] + len(body or "") + EST_LISTING_CHARS
        ) / CHARS_PER_TOK
        tout += est_tokens_out
    return int(tin), int(tout)


def cost_gate(
    cfg: dict,
    work: list,
    *,
    est_tokens_out: int,
    estimate_only: bool,
    assume_yes: bool,
    max_cost_usd: float,
) -> bool:
    """Return True to proceed. Local endpoints are free; paid endpoints show an
    estimate, enforce the --max-cost-usd cap, and (unless --yes) require
    interactive confirmation."""
    if is_local(cfg["api_base"]):
        if estimate_only:
            print(f"local endpoint {cfg['api_base']} — no API cost ({len(work)} cells).")
            return False
        return True
    tin, tout = estimate_tokens(work, est_tokens_out)
    print(f"\nAPI cost estimate — {cfg['model']} @ {cfg['api_base']}")
    print(f"  cells to run : {len(work)}")
    print(
        f"  prompt tokens: ~{tin:,}   completion tokens: ~{tout:,} (assumes {est_tokens_out}/call)"
    )
    ceil_cost = None
    pricing = fetch_openrouter_pricing(cfg["model"]) if "openrouter" in cfg["api_base"] else None
    if pricing:
        pin, pout = pricing
        est_cost = tin * pin + tout * pout
        ceil_cost = tin * pin + (cfg["max_tokens"] * len(work)) * pout
        print(f"  pricing      : ${pin * 1e6:.3f}/M prompt, ${pout * 1e6:.3f}/M completion")
        print(f"  ESTIMATED $  : ~${est_cost:.2f}   worst-case@max_tokens: ~${ceil_cost:.2f}")
    else:
        print(
            "  pricing      : UNKNOWN (no OpenRouter price for this model) — $ cannot be estimated"
        )
    if estimate_only:
        return False
    # Hard spend cap on the WORST case (max_tokens), not the rough estimate, so
    # the run genuinely cannot exceed it. --yes does not bypass a known overage;
    # raise --max-cost-usd to override (0 disables the cap).
    if max_cost_usd and max_cost_usd > 0:
        if ceil_cost is not None and ceil_cost > max_cost_usd:
            print(
                f"  ABORT: worst-case ~${ceil_cost:.2f} exceeds --max-cost-usd ${max_cost_usd:.2f}. "
                f"Raise --max-cost-usd to override.",
                file=sys.stderr,
            )
            return False
        if ceil_cost is None and not assume_yes:
            print(
                f"  ABORT: no pricing to verify the ${max_cost_usd:.2f} cap. "
                f"Pass --yes to override or set --max-cost-usd 0.",
                file=sys.stderr,
            )
            return False
    if assume_yes:
        print("  --yes given: proceeding.")
        return True
    try:
        ans = input("Proceed with this paid run? [y/N] ").strip().lower()
    except EOFError:
        print(
            "no TTY for confirmation; pass --yes to run non-interactively. Aborting.",
            file=sys.stderr,
        )
        return False
    return ans in ("y", "yes")


# ── config / folder ───────────────────────────────────────────────────────────
def derive_served_id(model: str, served_id: str | None, api_base: str) -> str | None:
    """served_id_hint must be the BARE served name for local sglang/vLLM.

    _evolution_chat wraps it to ``openai/<name>``; passing an already-prefixed
    id would double-prefix. For remote (openrouter) leave it unset.
    """
    if served_id:
        return served_id
    if is_local(api_base):
        return model[len("openai/") :] if model.startswith("openai/") else model
    return None


def run_name(args: argparse.Namespace) -> str:
    if args.name:
        return sanitize(args.name)
    tag = sanitize(args.model.split("/")[-1])
    loc = "local" if is_local(args.api_base) else "or"
    temp = f"t{args.temperature:g}".replace(".", "")
    # No n_trials in the name: the same config maps to one folder so --resume
    # can extend it with more trials.
    return f"{tag}_{loc}_reas{args.reasoning}_{temp}"


def prepare_run_dir(run_dir: Path, resume: bool) -> None:
    if not run_dir.exists():
        run_dir.mkdir(parents=True)
        return
    if resume:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = run_dir.with_name(f"{run_dir.name}_{stamp}")
    if archive.exists():
        raise SystemExit(f"refusing to clobber existing archive {archive}")
    run_dir.rename(archive)
    run_dir.mkdir(parents=True)
    print(f"archived previous run -> {archive}")


# ── skill bodies ───────────────────────────────────────────────────────────────
def skill_body(after_root: Path, rel_path: str) -> str:
    p = after_root / rel_path
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def condition_body(after_root: Path, skill: str, condition: str, subset_skill: dict) -> str | None:
    if condition == "no_skill":
        return None
    if condition == "handcraft":
        return skill_body(after_root, subset_skill["handcraft"])
    if condition == "skill_md":
        return skill_body(after_root, f"skills/{skill}/SKILL.md")
    raise ValueError(condition)


# ── one cell ────────────────────────────────────────────────────────────────────
def run_cell(
    *,
    skill: str,
    split: str,
    condition: str,
    task_rel: str,
    trial: int,
    body: str | None,
    solver_cfg: dict,
    api_key: str,
    cache_dir: Path,
    timeout_s: int,
) -> dict[str, Any]:
    task_id, _role, _skills, _diff, task_dir = _task_skill_role(task_rel)  # pyright: ignore[reportAssignmentType]
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    sc = dict(solver_cfg)
    sc["seed"] = solver_cfg["base_seed"] + trial
    cond_arg = "no_skill" if condition == "no_skill" else "post_train"
    res = nfr.run_evolution_test(
        task_dir=task_dir,
        raw_sbbench_path=task_dir,
        instruction=instruction,
        condition=cond_arg,
        cache_dir=cache_dir,
        solver_cfg=sc,
        api_key=api_key,
        post_train_skill_bodies=[body] if body is not None else None,
        timeout_s=timeout_s,
    )
    record = {
        "skill": skill,
        "split": split,
        "condition": condition,
        "task": task_id,
        "task_rel": task_rel,
        "trial": trial,
        "seed": sc["seed"],
        "outcome": res.get("outcome"),
        "pass_rate": res.get("pass_rate"),
        "passed": res.get("passed"),
        "total": res.get("total_tests"),
        "tokens_in": res.get("tokens_in_total"),
        "tokens_out": res.get("tokens_out_total"),
        "latency_ms": res.get("latency_ms_total"),
        "elapsed_s": res.get("elapsed_s"),
        "code_extraction_failed": res.get("code_extraction_failed"),
        "error": res.get("error"),
    }
    (cache_dir / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


# ── matrix enumeration ────────────────────────────────────────────────────────
def enumerate_cells(
    subset: dict, skills: list[str], conditions: list[str], n_train: int, n_test: int
):
    sk = subset["skills"]
    for skill in skills:
        spec = sk[skill]
        for split, cap in (("train", n_train), ("test", n_test)):
            for task_rel in spec.get(split, [])[:cap]:
                for condition in conditions:
                    yield skill, split, condition, task_rel


# ── aggregation ─────────────────────────────────────────────────────────────────
def scorecard(trials: list[dict]) -> list[dict]:
    cells: dict[tuple, list[dict]] = {}
    for t in trials:
        cells.setdefault((t["skill"], t["split"], t["condition"]), []).append(t)
    out = []
    for (skill, split, condition), rows in sorted(cells.items()):
        by_task: dict[str, list[float]] = {}
        for r in rows:
            if isinstance(r.get("pass_rate"), (int, float)):
                by_task.setdefault(r["task"], []).append(float(r["pass_rate"]))
        rates = [v for vals in by_task.values() for v in vals]
        n_hard_fail = sum(1 for r in rows if r.get("outcome") != "ok")
        n_stub = sum(1 for r in rows if r.get("code_extraction_failed"))
        m = mean(rates) if rates else None
        n_obs = len(rates)
        std_pr = pstdev(rates) if n_obs > 1 else 0.0
        # SEM = std/sqrt(n): uncertainty of the mean, shrinks ~1/sqrt(n_trials).
        sem_pr = (std_pr / (n_obs**0.5)) if n_obs > 1 else 0.0
        fs = (sum(1 for v in rates if v >= 1.0 - EPS) / n_obs) if n_obs else None
        # Binomial SE of the all-tests-pass proportion, also shrinks ~1/sqrt(n).
        fs_se = ((fs * (1.0 - fs) / n_obs) ** 0.5) if (fs is not None and n_obs > 0) else None
        per_task_std = [pstdev(v) for v in by_task.values() if len(v) > 1]
        passed = [r["passed"] for r in rows if isinstance(r.get("passed"), (int, float))]
        total = [r["total"] for r in rows if isinstance(r.get("total"), (int, float))]
        tok = [r["tokens_out"] for r in rows if isinstance(r.get("tokens_out"), (int, float))]
        lat = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
        out.append(
            {
                "skill": skill,
                "split": split,
                "condition": condition,
                "n_tasks": len(by_task),
                "n_trials": max((len(v) for v in by_task.values()), default=0),
                "n_obs": n_obs,
                "mean_pass_rate": m,
                "std_pass_rate": std_pr,
                "sem_pass_rate": sem_pr,
                "mean_per_task_std": mean(per_task_std) if per_task_std else 0.0,
                "min_pass_rate": min(rates) if rates else None,
                "max_pass_rate": max(rates) if rates else None,
                "best_of_n": mean([max(v) for v in by_task.values()]) if by_task else None,
                "full_solve_rate": fs,
                "full_solve_se": fs_se,
                "mean_solved_tests": mean(passed) if passed else None,
                "mean_total_tests": mean(total) if total else None,
                "headroom": (m is not None and EPS < m < 1.0 - EPS),
                "mean_tokens_out": mean(tok) if tok else None,
                "mean_latency_ms": mean(lat) if lat else None,
                "n_hard_fail": n_hard_fail,
                "n_stub": n_stub,
            }
        )
    return out


# ── report ────────────────────────────────────────────────────────────────────
def _fmt(x: Any, nd: int = 3) -> str:
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "-"


def render_report(cfg: dict, cards: list[dict]) -> str:
    L = [f"# Baseline stability — {cfg['run_name']}", ""]
    L += [
        "## Run summary",
        f"- model: `{cfg['model']}` (served `{cfg['served_id']}`)",
        f"- endpoint: `{cfg['api_base']}` ({'local' if is_local(cfg['api_base']) else 'remote'})",
        f"- reasoning: `{cfg['reasoning']}` -> extra_body `{json.dumps(cfg['extra_body'])}`"
        + (f"  ⚠ {cfg['reasoning_note']}" if cfg.get("reasoning_note") else ""),
        f"- temperature: {cfg['temperature']}  max_tokens: {cfg['max_tokens']}  base_seed: {cfg['base_seed']}",
        f"- conditions: {', '.join(cfg['conditions'])}",
        f"- scope: skills={','.join(cfg['skills'])} n_train={cfg['n_train']} n_test={cfg['n_test']} n_trials={cfg['n_trials']}",
        f"- subset: `{cfg['subset']}`  corpus: `{cfg['corpus']}`",
        f"- solver fingerprint: `{cfg.get('solver_fingerprint', '?')}` (resume target)",
        f"- generated: {cfg['generated_utc']}",
        "",
        "## Scorecard",
        "headroom flag: `*`=room to improve (0<mean<1), `=1.0`/`=0.0`=saturated",
        "`mean±sem` / `fullSolve±se`: ± is the **standard error of the aggregate**"
        " (SEM=std/√n, binomial √(p(1-p)/n)) — shrinks ~1/√n with more trials.",
        "`std`/`ptStd` are **per-trial dispersion** (run-to-run noise) and do NOT"
        " shrink with more trials; they estimate the model+verifier's intrinsic spread.",
        "",
    ]
    cols = [
        "mean±sem",
        "std",
        "ptStd",
        "min",
        "max",
        "bestN",
        "fullSolve±se",
        "solved/total",
        "head",
        "hard",
        "stub",
    ]
    by_skill: dict[str, list[dict]] = {}
    for c in cards:
        by_skill.setdefault(c["skill"], []).append(c)
    for skill, rows in by_skill.items():
        L += [
            f"### {skill}",
            "",
            "| split | condition | " + " | ".join(cols) + " |",
            "|" + "---|" * (len(cols) + 2),
        ]
        for c in rows:
            m = c["mean_pass_rate"]
            head = (
                "*"
                if c["headroom"]
                else (
                    "=1.0"
                    if (m is not None and m >= 1 - EPS)
                    else "=0.0"
                    if (m is not None and m <= EPS)
                    else "-"
                )
            )
            solved = f"{_fmt(c['mean_solved_tests'], 1)}/{_fmt(c['mean_total_tests'], 1)}"
            mean_s = f"{_fmt(m)}±{_fmt(c['sem_pass_rate'])}"
            fs_s = f"{_fmt(c['full_solve_rate'])}±{_fmt(c['full_solve_se'])}"
            L.append(
                f"| {c['split']} | {c['condition']} | "
                + " | ".join(
                    [
                        mean_s,
                        _fmt(c["std_pass_rate"]),
                        _fmt(c["mean_per_task_std"]),
                        _fmt(c["min_pass_rate"]),
                        _fmt(c["max_pass_rate"]),
                        _fmt(c["best_of_n"]),
                        fs_s,
                        solved,
                        head,
                        str(c["n_hard_fail"]),
                        str(c["n_stub"]),
                    ]
                )
                + " |"
            )
        L.append("")
    # stability read (descriptive, not a verdict)
    saturated = [
        f"{c['skill']}/{c['split']}/{c['condition']}"
        for c in cards
        if c["mean_pass_rate"] is not None and not c["headroom"]
    ]
    noisy = [
        f"{c['skill']}/{c['split']}/{c['condition']} (std={_fmt(c['std_pass_rate'])})"
        for c in cards
        if c["std_pass_rate"] > STD_HI
    ]
    targets = [
        f"{c['skill']}/{c['split']}/{c['condition']}"
        for c in cards
        if c["headroom"] and c["std_pass_rate"] <= STD_HI
    ]
    L += [
        "## Stability read",
        f"- saturated (no headroom): {', '.join(saturated) or 'none'}",
        f"- high variance (std>{STD_HI}): {', '.join(noisy) or 'none'}",
        f"- good evolution target (headroom + low variance): {', '.join(targets) or 'none'}",
        "",
    ]
    return "\n".join(L)


# ── reusable artifacts ──────────────────────────────────────────────────────────
def write_reuse_artifacts(run_dir: Path, cfg: dict) -> None:
    evo_env = {
        "EVO_SOLVER_MODEL": cfg["model"],
        "EVO_SOLVER_SERVED_ID": cfg["served_id"] or "",
        "EVO_SOLVER_API_BASE": cfg["api_base"],
        "EVO_SOLVER_API_KEY_ENV": cfg["api_key_env"],
        "EVO_SOLVER_REASONING_EFFORT": reasoning_to_evo_effort(cfg["reasoning"]),
        "EVO_TEMPERATURE": str(cfg["temperature"]),
        "EVO_MAX_TOKENS": str(cfg["max_tokens"]),
    }
    params = {
        "litellm": {
            "model": cfg["model"],
            "served_id_hint": cfg["served_id"],
            "api_base": cfg["api_base"],
            "api_key_env": cfg["api_key_env"],
            "temperature": cfg["temperature"],
            "max_output_tokens": cfg["max_tokens"],
            "top_p": cfg["top_p"],
            "extra_body": cfg["extra_body"],
            "base_seed": cfg["base_seed"],
            "reasoning": cfg["reasoning"],
        },
        "evolution_env": evo_env,
        "subset": cfg["subset"],
        "corpus": cfg["corpus"],
        "skills": cfg["skills"],
        "n_train": cfg["n_train"],
        "n_test": cfg["n_test"],
    }
    (run_dir / "solver_params.json").write_text(json.dumps(params, indent=2), encoding="utf-8")
    lines = [
        "#!/usr/bin/env bash",
        "# Relaunch a framework-evolution run reusing the solver params probed by this baseline.",
        "set -euo pipefail",
        f'cd "{EVO_ROOT}"',
    ]
    for k, v in evo_env.items():
        lines.append(f'export {k}="{v}"')
    lines += [
        f'export EVO_SUBSET="{cfg["subset"]}"',
        f'export EVO_CORPUS="{cfg["corpus"]}"',
        f'export EVO_ANCHORS="{",".join(cfg["skills"])}"',
        "exec bash experiments/baselines/run_after_evolution_litellm.sh",
        "",
    ]
    sh = run_dir / "relaunch_evolution.sh"
    sh.write_text("\n".join(lines), encoding="utf-8")
    sh.chmod(0o755)


# ── output writers ──────────────────────────────────────────────────────────────
_TRIAL_COLS = [
    "skill",
    "split",
    "condition",
    "task",
    "trial",
    "seed",
    "outcome",
    "pass_rate",
    "passed",
    "total",
    "tokens_in",
    "tokens_out",
    "latency_ms",
    "elapsed_s",
    "code_extraction_failed",
    "error",
]


def write_outputs(run_dir: Path, cfg: dict, trials: list[dict]) -> None:
    res = run_dir / "results"
    res.mkdir(exist_ok=True)
    with (res / "trials.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_TRIAL_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(trials)
    cards = scorecard(trials)
    with (res / "scorecard.csv").open("w", newline="", encoding="utf-8") as fh:
        if cards:
            w = csv.DictWriter(fh, fieldnames=list(cards[0].keys()))
            w.writeheader()
            w.writerows(cards)
    (res / "scorecard.json").write_text(
        json.dumps({"config": cfg, "cards": cards}, indent=2), encoding="utf-8"
    )
    (res / "REPORT.md").write_text(render_report(cfg, cards), encoding="utf-8")


# ── cross-run ranking ───────────────────────────────────────────────────────────
def _run_aggregate(run_dir: Path) -> dict | None:
    sc = run_dir / "results" / "scorecard.json"
    if not sc.is_file():
        return None
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
    except Exception:
        return None
    cfg, cards = data.get("config", {}), data.get("cards", [])
    if not cards:
        return None

    def avg(key: str) -> float | None:
        vals = [c[key] for c in cards if isinstance(c.get(key), (int, float))]
        return mean(vals) if vals else None

    return {
        "run": run_dir.name,
        "model": cfg.get("model", "?"),
        "reasoning": cfg.get("reasoning", "?"),
        "temperature": cfg.get("temperature"),
        "n_trials": cfg.get("n_trials"),
        "endpoint": "local" if is_local(cfg.get("api_base", "")) else "remote",
        "n_cells": len(cards),
        "mean_pass_rate": avg("mean_pass_rate"),
        "ptStd": avg("mean_per_task_std"),
        "std": avg("std_pass_rate"),
        "full_solve_rate": avg("full_solve_rate"),
        "headroom_frac": sum(1 for c in cards if c.get("headroom")) / len(cards),
        "hard": sum(int(c.get("n_hard_fail", 0) or 0) for c in cards),
        "stub": sum(int(c.get("n_stub", 0) or 0) for c in cards),
    }


def build_ranking_table(stability_root: Path) -> str:
    rows = [
        r for d in sorted(stability_root.iterdir()) if d.is_dir() for r in [_run_aggregate(d)] if r
    ]
    # Sort by capability (mean pass-rate) desc; runs with no numeric mean sink.
    rows.sort(
        key=lambda r: (r["mean_pass_rate"] is not None, r["mean_pass_rate"] or 0.0), reverse=True
    )
    cols = [
        "#",
        "run",
        "model",
        "reason",
        "temp",
        "trials",
        "cells",
        "mean",
        "ptStd",
        "std",
        "fullSolve",
        "head%",
        "hard/stub",
        "endpoint",
    ]
    L = [
        "# Stability ranking",
        "",
        "Auto-generated from every `stability/*/results/scorecard.json` (refreshed after each run; "
        "regenerate with `run_baseline_stability.py --rank-only`). **Sorted by mean pass-rate (desc).**",
        "",
        "- `mean` = avg pass-rate over all cells · `ptStd` = avg per-trial within-task std "
        "(**trial-economy metric — lower ⇒ fewer trials needed**) · `std` includes task-to-task spread.",
        "- `fullSolve` = avg fraction of trials passing all tests · `head%` = fraction of cells with "
        "headroom (0<mean<1) · `hard/stub` = total hard-fails / stub solves.",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for i, r in enumerate(rows, 1):
        L.append(
            "| "
            + " | ".join(
                [
                    str(i),
                    r["run"],
                    r["model"],
                    r["reasoning"],
                    f"{r['temperature']:g}" if isinstance(r["temperature"], (int, float)) else "-",
                    str(r["n_trials"]),
                    str(r["n_cells"]),
                    _fmt(r["mean_pass_rate"]),
                    _fmt(r["ptStd"]),
                    _fmt(r["std"]),
                    _fmt(r["full_solve_rate"]),
                    f"{r['headroom_frac'] * 100:.0f}%",
                    f"{r['hard']}/{r['stub']}",
                    r["endpoint"],
                ]
            )
            + " |"
        )
    L += [
        "",
        f"_generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} · {len(rows)} runs_",
        "",
    ]
    return "\n".join(L)


def write_ranking(stability_root: Path) -> Path:
    stability_root.mkdir(parents=True, exist_ok=True)
    out = stability_root / "ranking.md"
    out.write_text(build_ranking_table(stability_root), encoding="utf-8")
    return out


# ── main ─────────────────────────────────────────────────────────────────────────
def build_cfg(args: argparse.Namespace, subset: dict) -> dict:
    skills = (
        list(subset["skills"])
        if args.skills == "all"
        else [s.strip() for s in args.skills.split(",") if s.strip()]
    )
    missing = [s for s in skills if s not in subset["skills"]]
    if missing:
        raise SystemExit(f"unknown skills (not in subset): {missing}")
    extra_body, note = reasoning_extra_body(args.served_id or args.model, args.reasoning)
    cfg = {
        "run_name": run_name(args),
        "model": args.model,
        "served_id": derive_served_id(args.model, args.served_id, args.api_base),
        "api_base": args.api_base,
        "api_key_env": args.api_key_env,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "base_seed": args.seed,
        "reasoning": args.reasoning,
        "extra_body": extra_body,
        "reasoning_note": note,
        "conditions": (CONDITIONS_DEFAULT + ["skill_md"])
        if args.with_skill_md
        else CONDITIONS_DEFAULT,
        "skills": skills,
        "n_train": args.n_train,
        "n_test": args.n_test,
        "n_trials": args.n_trials,
        "subset": str(args.subset),
        "corpus": str(AFTER_ROOT),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    cfg["solver_fingerprint"] = solver_fingerprint(cfg)
    return cfg


def build_work(subset: dict, cfg: dict, cells_root: Path, resume: bool) -> list:
    """Enumerate (cell, trial) work units, skipping any already done on resume."""
    work = []
    for skill, split, condition, task_rel in enumerate_cells(
        subset, cfg["skills"], cfg["conditions"], cfg["n_train"], cfg["n_test"]
    ):
        body = condition_body(AFTER_ROOT, skill, condition, subset["skills"][skill])
        task_id = _task_skill_role(task_rel)[0]
        for trial in range(cfg["n_trials"]):
            cache_dir = cells_root / skill / split / condition / task_id / f"trial_{trial}"
            if resume and (cache_dir / "result.json").is_file():
                continue
            work.append((skill, split, condition, task_rel, trial, body, cache_dir))
    return work


def solver_cfg_from(cfg: dict) -> dict:
    # top_p has no dedicated solver_cfg slot; vLLM/OpenAI read it from extra_body.
    extra = dict(cfg["extra_body"])
    if cfg["top_p"] is not None:
        extra["top_p"] = cfg["top_p"]
    sc = {
        "id": cfg["model"],
        "api_base": cfg["api_base"],
        "temperature": cfg["temperature"],
        "max_output_tokens": cfg["max_tokens"],
        "extra_body": extra or None,
        "base_seed": cfg["base_seed"],
    }
    if cfg["served_id"]:
        sc["served_id_hint"] = cfg["served_id"]
    return sc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skills", default=",".join(DEFAULT_SKILLS), help='comma list, or "all"')
    ap.add_argument("--n-train", type=int, default=5)
    ap.add_argument("--n-test", type=int, default=3)
    ap.add_argument("--n-trials", type=int, default=5)
    ap.add_argument("--model", default="openai/Qwen/Qwen3.5-35B-A3B")
    ap.add_argument("--api-base", default="http://localhost:30110/v1")
    ap.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--served-id", default=None)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top-p", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--reasoning", choices=["off", "low", "medium", "high", "on"], default="off")
    ap.add_argument("--with-skill-md", action="store_true")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--timeout-s", type=int, default=900)
    ap.add_argument("--name", default=None)
    ap.add_argument("--subset", type=Path, default=DEFAULT_SUBSET)
    ap.add_argument(
        "--resume",
        action="store_true",
        help="reuse the same-config folder, skip done cells, add any new trials",
    )
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the paid-API cost confirmation prompt")
    ap.add_argument(
        "--estimate-only", action="store_true", help="print the API cost estimate and exit"
    )
    ap.add_argument(
        "--est-tokens-out",
        type=int,
        default=DEFAULT_EST_TOKENS_OUT,
        help="assumed completion tokens/call for the cost estimate",
    )
    ap.add_argument(
        "--max-cost-usd",
        type=float,
        default=2.0,
        help="hard cap: abort if worst-case paid cost exceeds this (0 disables)",
    )
    ap.add_argument(
        "--rank-only",
        action="store_true",
        help="(re)generate stability/ranking.md from existing runs and exit",
    )
    args = ap.parse_args()

    if args.rank_only:
        print(f"ranking: {write_ranking(STABILITY_ROOT)}")
        return 0

    subset = json.loads(args.subset.read_text(encoding="utf-8"))
    cfg = build_cfg(args, subset)
    api_key = os.environ.get(args.api_key_env, "") or "EMPTY"
    solver_cfg = solver_cfg_from(cfg)

    if args.self_check:
        return _self_check(cfg, subset, solver_cfg, api_key, args.timeout_s)

    run_dir = STABILITY_ROOT / cfg["run_name"]
    cells_root = run_dir / "cells"

    # Resume guard: same folder must be the same solver configuration.
    prior_cfg_path = run_dir / "RUN_CONFIG.json"
    if args.resume and prior_cfg_path.is_file():
        prior = json.loads(prior_cfg_path.read_text(encoding="utf-8"))
        diffs = identity_diff(prior, cfg)
        if diffs:
            lines = "\n".join(f"    {k}: {old!r} -> {new!r}" for k, (old, new) in diffs.items())
            raise SystemExit(
                f"resume refused: '{run_dir.name}' was run with a different solver config:\n{lines}\n"
                f"  start a fresh run (drop --resume) or use --name for a separate folder."
            )
        print(
            f"resuming {run_dir.name} (fingerprint {cfg['solver_fingerprint']}) — adding trials/cells"
        )

    work = build_work(subset, cfg, cells_root, args.resume)

    if not cost_gate(
        cfg,
        work,
        est_tokens_out=args.est_tokens_out,
        estimate_only=args.estimate_only,
        assume_yes=args.yes,
        max_cost_usd=args.max_cost_usd,
    ):
        return 0

    prepare_run_dir(run_dir, args.resume)
    prior_cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    write_reuse_artifacts(run_dir, cfg)

    print(f"{cfg['run_name']}: {len(work)} cells to run (parallel={args.parallel})")
    trials: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {
            pool.submit(
                run_cell,
                skill=s,
                split=sp,
                condition=c,
                task_rel=tr,
                trial=t,
                body=b,
                solver_cfg=solver_cfg,
                api_key=api_key,
                cache_dir=cd,
                timeout_s=args.timeout_s,
            ): (s, sp, c, tr, t)
            for (s, sp, c, tr, t, b, cd) in work
        }
        done = 0
        for fut in as_completed(futs):
            coords = futs[fut]
            try:
                trials.append(fut.result())
            except Exception as exc:  # noqa: BLE001 — keep the matrix going, record the gap
                trials.append(
                    {
                        "skill": coords[0],
                        "split": coords[1],
                        "condition": coords[2],
                        "task": coords[3],
                        "trial": coords[4],
                        "outcome": "driver_error",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
            done += 1
            if done % 10 == 0 or done == len(work):
                print(f"  {done}/{len(work)} done ({time.time() - t0:.0f}s)")

    # On resume, fold in prior trials so the scorecard covers the whole matrix.
    if args.resume:
        trials.extend(
            _load_prior_trials(
                cells_root,
                {
                    (
                        t.get("skill"),
                        t.get("split"),
                        t.get("condition"),
                        t.get("task"),
                        t.get("trial"),
                    )
                    for t in trials
                },
            )
        )

    write_outputs(run_dir, cfg, trials)
    write_ranking(STABILITY_ROOT)
    print(f"done: {run_dir / 'results' / 'REPORT.md'}")
    print(f"ranking: {STABILITY_ROOT / 'ranking.md'}")
    return 0


def _load_prior_trials(cells_root: Path, seen: set) -> list[dict]:
    out = []
    for rj in cells_root.rglob("result.json"):
        try:
            rec = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            continue
        key = (
            rec.get("skill"),
            rec.get("split"),
            rec.get("condition"),
            rec.get("task"),
            rec.get("trial"),
        )
        if key not in seen:
            out.append(rec)
            seen.add(key)
    return out


def _self_check(cfg: dict, subset: dict, solver_cfg: dict, api_key: str, timeout_s: int) -> int:
    skill = cfg["skills"][0]
    task_rel = subset["skills"][skill]["train"][0]
    tmp = STABILITY_ROOT / "_self_check" / cfg["run_name"]
    if tmp.exists():
        shutil.rmtree(tmp)
    rec = run_cell(
        skill=skill,
        split="train",
        condition="no_skill",
        task_rel=task_rel,
        trial=0,
        body=None,
        solver_cfg=solver_cfg,
        api_key=api_key,
        cache_dir=tmp,
        timeout_s=timeout_s,
    )
    print(json.dumps(rec, indent=2))
    if rec.get("outcome") != "ok":
        print(
            f"SELF-CHECK FAIL: outcome={rec.get('outcome')} error={rec.get('error')}",
            file=sys.stderr,
        )
        return 1
    if rec.get("code_extraction_failed"):
        print("SELF-CHECK FAIL: model produced no code (stub solve)", file=sys.stderr)
        return 1
    print(
        f"SELF-CHECK OK: pass_rate={rec.get('pass_rate')} ({rec.get('passed')}/{rec.get('total')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
