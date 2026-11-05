"""Sanitize and freeze raw CRAFT/SB-Bench tasks for paper experiments.

The helpers here materialize deterministic task copies under an experiment's
ignored artifact root. Generated private inputs are copied into
``environment/data``; provenance and solver outputs are excluded from the
sanitized task copy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import tomllib

from _shared.pytest_runner import run_pytest_against_workspace
from _shared.sbbench_cell_resolver import fingerprint_task_dir, sha256_file
from _shared.workspace_materializer import write_agent_solve_sh

NOOP_DATA_GENERATOR = """#!/usr/bin/env python3
from pathlib import Path

Path("environment/data").mkdir(parents=True, exist_ok=True)
"""

SKIP_CLEAN = {"source_artifacts", "output", "__pycache__", ".pytest_cache", ".git", "solution.py"}
SKIP_STAGE = {"output", "__pycache__", ".pytest_cache", ".git"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _ignore(names: set[str]):
    def inner(_dir: str, children: list[str]) -> set[str]:
        return names & set(children)

    return inner


def raw_task_dir(corpus_root: Path, role: str, task_id: str) -> Path:
    primary = corpus_root / "tasks" / role / task_id
    if (primary / "task.toml").is_file():
        return primary
    return corpus_root / "deprecated_tasks" / role / task_id


def manifest_rows(corpus_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path in (
        corpus_root / "tasks" / "manifest.json",
        corpus_root / "deprecated_tasks" / "manifest.json",
    ):
        if not path.is_file():
            continue
        for row in load_json(path):
            out.setdefault((str(row["role"]), str(row["task_id"])), row)
    return out


def sanitize_task(
    *,
    raw_task: Path,
    clean_task: Path,
    private_root: Path,
) -> dict[str, Any]:
    if clean_task.exists():
        shutil.rmtree(clean_task)
    if private_root.exists():
        shutil.rmtree(private_root)
    shutil.copytree(
        raw_task,
        private_root,
        ignore=_ignore(SKIP_STAGE),
        symlinks=True,
        ignore_dangling_symlinks=True,
    )
    gen_rc: int | None = None
    gen_err = ""
    dg = private_root / "data_generator.py"
    if dg.is_file():
        proc = subprocess.run(
            [sys.executable, "data_generator.py"],
            cwd=private_root,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        gen_rc = proc.returncode
        gen_err = (proc.stderr or "")[-500:]

    shutil.copytree(
        raw_task,
        clean_task,
        ignore=_ignore(SKIP_CLEAN),
        symlinks=False,
        ignore_dangling_symlinks=True,
    )
    env_data = clean_task / "environment" / "data"
    if env_data.exists():
        shutil.rmtree(env_data)
    env_data.mkdir(parents=True, exist_ok=True)

    generated = private_root / "environment" / "data"
    fallback = raw_task / "environment" / "data"
    source_env = generated if generated.is_dir() and any(generated.iterdir()) else fallback
    copied_inputs = 0
    if source_env.is_dir():
        for item in sorted(source_env.iterdir()):
            target = env_data / item.name
            if item.is_dir():
                shutil.copytree(item, target, symlinks=False)
            else:
                shutil.copy2(item, target)
            copied_inputs += 1
    if copied_inputs == 0:
        (env_data / "README.md").write_text(
            "This task has no static input files; create requested outputs in the workspace.\n",
            encoding="utf-8",
        )
        copied_inputs = 1
    (clean_task / "data_generator.py").write_text(NOOP_DATA_GENERATOR, encoding="utf-8")
    try:
        (clean_task / "data_generator.py").chmod(0o755)
    except OSError:
        pass

    solve_sh = clean_task / "solve.sh"
    if solve_sh.is_file():
        write_agent_solve_sh(solve_sh)

    # When tests look for outputs under output/, surface the convention in
    # the public instruction (paper rule: never inject via framework prompt
    # envelope; task-level documentation is fine).
    tests_py = clean_task / "tests" / "test_outputs.py"
    instruction = clean_task / "instruction.md"
    if tests_py.is_file() and instruction.is_file() and solve_sh.is_file():
        tests_src = tests_py.read_text(encoding="utf-8", errors="replace")
        if re.search(r'(?:Path\(\s*[\'"]output/|["\']output/[A-Za-z0-9_])', tests_src):
            existing = instruction.read_text(encoding="utf-8")
            marker = "The harness runs your solution from `<workspace>/output`."
            if marker not in existing:
                instruction.write_text(
                    existing.rstrip()
                    + "\n\n## Output Location\n\n"
                    + marker
                    + " Verifier paths beginning with `output/` are relative to"
                    + " the workspace root; create those artifacts relative to"
                    + " the current directory. For example, if the required"
                    + " artifact is `output/redacted/paper1.pdf`, write"
                    + " `redacted/paper1.pdf` from your script.\n",
                    encoding="utf-8",
                )

    return {
        "raw_task": str(raw_task),
        "sanitized_task": str(clean_task),
        "private_generation": str(private_root),
        "data_generator_rc": gen_rc,
        "data_generator_stderr_tail": gen_err,
        "frozen_input_count": copied_inputs,
        "sanitized_fingerprint": fingerprint_task_dir(clean_task),
    }


def task_meta(
    *,
    corpus_root: Path,
    manifest_index: dict[tuple[str, str], dict[str, Any]],
    sanitized_root: Path,
    private_root: Path,
    role: str,
    skill: str,
    task_id: str,
    stream: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    issues: list[str] = []
    raw = raw_task_dir(corpus_root, role, task_id)
    if not raw.is_dir():
        issues.append(f"missing raw task dir: {raw}")
    row = manifest_index.get((role, task_id))
    if not row:
        issues.append(f"{role}/{task_id}: absent from manifest")
    elif row.get("included") is not True:
        issues.append(f"{role}/{task_id}: manifest included is not true")
    elif row.get("skills") != [skill]:
        issues.append(f"{role}/{task_id}: expected single skill {[skill]}, got {row.get('skills')}")
    for rel in ("task.toml", "instruction.md", "tests/test_outputs.py", "solve.sh"):
        if not (raw / rel).is_file():
            issues.append(f"{role}/{task_id}: missing {rel}")

    clean = sanitized_root / role / task_id
    private = private_root / role / task_id
    freeze = sanitize_task(raw_task=raw, clean_task=clean, private_root=private)
    raw_toml = tomllib.loads((raw / "task.toml").read_text(encoding="utf-8"))
    difficulty = str(raw_toml.get("task", {}).get("difficulty") or "")
    meta = {
        "id": task_id,
        "role": role,
        "cell_id": f"{role}.{skill}",
        "skill": skill,
        "stream": stream,
        "split": "train" if stream in {"train_a", "train_b", "support_train"} else stream,
        "split_class": stream,
        "eval_skill_source": f"{role}.{skill}",
        "raw_sbbench_path": str(clean.resolve()),
        "original_sbbench_path": str(raw.resolve()),
        "skills": [skill],
        "difficulty": difficulty,
        "task_fingerprint": freeze["sanitized_fingerprint"],
        "instruction_sha256": sha256_file(clean / "instruction.md"),
        "task_toml_sha256": sha256_file(clean / "task.toml"),
        "tests_sha256": sha256_file(clean / "tests" / "test_outputs.py"),
    }
    return meta, freeze, issues


def oracle_check_skill(
    skill_summary: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    skill_dir = Path(skill_summary["artifact_dir"])
    cells = load_json(skill_dir / "cells_resolved.json")
    out_root = skill_dir / "oracle_workspaces"
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    results = []
    for task_id, meta in sorted(cells["tasks"].items()):
        clean_task = Path(meta["raw_sbbench_path"])
        raw_task = Path(meta["original_sbbench_path"])
        ws = out_root / f"{meta['role']}__{task_id}"
        shutil.copytree(clean_task, ws, symlinks=False)
        raw_solution = raw_task / "solution.py"
        if raw_solution.is_file():
            shutil.copy2(raw_solution, ws / "solution.py")
        res = run_pytest_against_workspace(task_dir=clean_task, workspace=ws, timeout_s=timeout_s)
        leaked = any(part == "source_artifacts" for p in ws.rglob("*") for part in p.parts)
        ok = res["pass_rate"] == 1.0 and int(res["total_tests"] or 0) > 0 and not leaked
        results.append(
            {
                "task_id": task_id,
                "role": meta["role"],
                "ok": ok,
                "pass_rate": res["pass_rate"],
                "total_tests": res["total_tests"],
                "passed": res["passed"],
                "failed": res["failed"],
                "errors": res["errors"],
                "source_artifacts_leaked": leaked,
                "log_tail": "" if ok else res["log_tail"][-1000:],
            }
        )
    summary = {
        "skill": skill_summary["skill"],
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
        "valid": all(r["ok"] for r in results),
        "results": results,
    }
    write_json(skill_dir / "oracle_preflight.json", summary)
    return summary
