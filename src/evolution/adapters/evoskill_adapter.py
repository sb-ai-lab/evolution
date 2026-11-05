"""EvoSkill adapter - real pipeline via ``evoskill run`` with openai_compat shim.

EvoSkill's 5 bundled harnesses (claude, opencode, codex, goose, openhands)
all require external tooling (Anthropic CLI, opencode binary, codex binary,
Goose CLI, OpenHands server) that is unavailable in this environment. We
added a minimal ``openai_compat`` harness inside EvoSkill - a thin shim
around ``openai.AsyncOpenAI`` that talks to any OpenAI-compatible
/v1/chat/completions endpoint. See
``EvoSkill/src/harness/openai_compat/`` for the shim.

This adapter:

1. Scaffolds an EvoSkill project at
   ``evolution/experiments/smoke-4way/evoskill_workdir/`` with
   ``.evoskill/config.toml``, ``task.md``, dataset CSV, and a script
   scorer that drives Evolution's solver on SkillsBench train tasks.
2. Invokes ``evoskill run --max-iterations N --quiet``. Set via
   ``max_iterations`` argument (default 2 for pilot).
3. Extracts the winning program's ``.claude/skills/*`` files from the
   winning git branch and normalises them into ``.agents/skills/evoskill-*/SKILL.md``.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ._paths import evoskill_bin, evoskill_root, python_bin, tasks_root
from .common import (
    REPO_ROOT,
    SKILLS_ROOT,
    PilotManifest,
    compose_per_task_skills,
    compose_test_time_skills,
    resolve_baseline_mode,
    write_skill_md,
)
from .common import (
    slug as _slug,
)

logger = logging.getLogger(__name__)

FRAMEWORK_TAG = "evoskill"
WORKDIR = REPO_ROOT / "experiments" / "smoke-4way" / "evoskill_workdir"


TASK_MD = """\
# Task

Solve the given SkillsBench-style instruction by emitting a Python script
that, when run in a workspace directory containing the task inputs, produces
the expected output files. The agent's answer is the script it proposes to
write - EvoSkill's script scorer will execute the script in a scratch
workspace and return the pytest pass rate.

## Examples
- "Write an offer letter using the Word template..." -> emit the exact python
  script that fills the placeholders and saves offer_letter_filled.docx.

## Output format
Return ONLY a valid Python script in a ```python fenced block. No prose.

---

# Constraints
- No external APIs
- Standard library + openpyxl / python-docx / python-pptx / pdfplumber / PyMuPDF / pandas / polars
- Output files go to the current working directory
"""


CONFIG_TEMPLATE = {
    "harness": {
        # Default to the gigachat harness (added in EvoSkill/src/harness/gigachat/);
        # callers can override harness.name + harness.model via PRODUCE_KWARGS.
        "name": "gigachat",
        "model": "gigachat-max",
        "data_dirs": [],
        "timeout_seconds": 600,
        "max_retries": 2,
    },
    "evolution": {
        "mode": "skill_only",
        "iterations": 4,
        "frontier_size": 2,
        "concurrency": 16,
        "no_improvement_limit": 5,
        "failure_samples": 2,
    },
    "dataset": {
        "path": "./data/skillsbench_train.csv",
        "question_column": "question",
        "ground_truth_column": "ground_truth",
        "category_column": "category",
        # With only 5 training rows, we need nearly all of them in the train
        # pool; the default 18%/12% split is far too aggressive for a pilot.
        "train_ratio": 0.8,
        "val_ratio": 0.2,
    },
    "scorer": {
        "type": "script",
        "command": "./scorer.sh",
    },
}


_SCORER_SCRIPT_BODY = """#!/usr/bin/env bash
# EvoSkill script scorer: extract a python code block from $PREDICTED, write
# it to a temp workspace alongside the task inputs, execute, then print the
# pytest pass_rate on stdout (0.0-1.0).
#
# Environment from EvoSkill (src/cli/shared.py): QUESTION, PREDICTED,
# GROUND_TRUTH. GROUND_TRUTH is the task id (we put the task id into the
# dataset's ground_truth column so the scorer can locate the real fixture).
set -u
PYTHON="__PYTHON_BIN__"
TASK_ID="${GROUND_TRUTH:-}"
if [ -z "$TASK_ID" ]; then
    echo 0.0
    exit 0
fi
TASKS_ROOT="__TASKS_ROOT__"
TASK_DIR="$TASKS_ROOT/$TASK_ID"
if [ ! -d "$TASK_DIR" ]; then
    echo 0.0
    exit 0
fi

WS=$(mktemp -d -t evoskill-score-XXXXXX)
cleanup() { rm -rf "$WS"; }
trap cleanup EXIT
ART_DIR="${EVOSKILL_SCORER_ARTIFACT_DIR:-.evoskill/scorer_artifacts}"
mkdir -p "$ART_DIR" 2>/dev/null || true
SAFE_TASK_ID=$(printf '%s' "$TASK_ID" | tr -c 'A-Za-z0-9_.-' '_')
ART_PATH="$ART_DIR/${SAFE_TASK_ID}__$(date +%s%N)__$$.json"

# Materialise task inputs.
if [ -d "$TASK_DIR/inputs" ]; then
    cp -r "$TASK_DIR/inputs/." "$WS/"
fi

# Extract python code block from PREDICTED, fallback to PREDICTED as-is.
CODE_FILE="$WS/_solve.py"
$PYTHON - "$WS" <<'PY' 2>/dev/null || true
import os, re, sys, pathlib
ws = pathlib.Path(sys.argv[1])
predicted = os.environ.get("PREDICTED") or ""
m = re.search(r"```(?:python|py)?\\s*\\n(.+?)```", predicted, flags=re.DOTALL)
code = m.group(1).strip() if m else predicted.strip()
(ws / "_solve.py").write_text(code, encoding="utf-8")
PY

# Execute the candidate script inside the workspace.
(cd "$WS" && timeout 120 "$PYTHON" _solve.py) > "$WS/_solver.out" 2>&1 || true

# Run pytest against the task's test file with TASK_WORKSPACE=$WS.
TEST_FILE="$TASK_DIR/tests/test_outputs.py"
if [ ! -f "$TEST_FILE" ]; then
    echo 0.0
    exit 0
fi
export TASK_WORKSPACE="$WS"
export TEST_FILE="$TEST_FILE"
timeout 60 "$PYTHON" -m pytest -q --no-header --no-summary "$TEST_FILE" > "$WS/_pytest.out" 2>&1
PYTEST_RC=$?

# Parse "X passed, Y failed" from the output.
$PYTHON - "$WS/_pytest.out" "$WS/_solver.out" "$ART_PATH" "$TASK_ID" "$PYTEST_RC" <<'PY' 2>/dev/null || echo 0.0
import json, os, re, sys, pathlib
try:
    text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore")
except Exception:
    print(0.0); raise SystemExit
solver = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8", errors="ignore") if pathlib.Path(sys.argv[2]).exists() else ""
m = re.search(r"(\\d+) passed", text)
passed = int(m.group(1)) if m else 0
m = re.search(r"(\\d+) failed", text)
failed = int(m.group(1)) if m else 0
m = re.search(r"(\\d+) error", text)
errors = int(m.group(1)) if m else 0
total = passed + failed + errors
score = (passed / total) if total else 0.0
artifact = {
    "framework": "evoskill",
    "task_id": sys.argv[4],
    "feedback_policy": os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY", "hidden_summary"),
    "oracle_visible": os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY") == "oracle_diagnostic",
    "tests_visible_to_agent": False,
    "score": score,
    "passed": passed,
    "failed": failed,
    "errors": errors,
    "total_tests": total,
    "pytest_rc": int(sys.argv[5]),
    "solver_stdout_stderr_tail": solver[-8000:],
    "pytest_stdout_tail": text[-8000:],
    "test_assertions": pathlib.Path(os.environ.get("TEST_FILE", "")).read_text(encoding="utf-8", errors="ignore")[:4000] if os.environ.get("TEST_FILE") and pathlib.Path(os.environ.get("TEST_FILE", "")).exists() else "",
    "workspace_artifacts": [
        {"path": str(p.relative_to(pathlib.Path(os.environ.get("TASK_WORKSPACE", ".")))), "size": p.stat().st_size, "suffix": p.suffix}
        for p in pathlib.Path(os.environ.get("TASK_WORKSPACE", ".")).rglob("*")
        if p.is_file()
    ][:200],
}
try:
    pathlib.Path(sys.argv[3]).write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
except Exception:
    pass
print(f"{score:.3f}")
PY
"""


def render_scorer_script() -> str:
    """Materialize the EvoSkill scorer with current PYTHON / TASKS_ROOT."""
    return _SCORER_SCRIPT_BODY.replace("__PYTHON_BIN__", python_bin()).replace(
        "__TASKS_ROOT__", tasks_root()
    )


@dataclass
class EvoSkillProducePlan:
    framework: str = FRAMEWORK_TAG
    per_task_skills: dict[str, list[str]] = field(default_factory=dict)
    all_produced_skill_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    run_stdout_tail: str = ""
    run_stderr_tail: str = ""
    run_rc: int | None = None
    #: Number of .claude/skills/*/SKILL.md (or *.py) files found on the
    #: best program branch after ``evoskill run`` completed.
    skill_files_written: int = 0
    #: sha256(body) list for each produced Evolution-store SKILL.md.
    body_sha256: list[str] = field(default_factory=list)
    #: Honest-zero: True iff EvoSkill produced no writable skills under
    #: the selected B-mode. The row-count gate still emits 24 eval rows,
    #: rendered in a separate "Model-only baseline" row in the report.
    skipped: bool = False
    skipped_reason: str = ""
    #: B1 / B2 / B3 dispatch decision from preflight_result.json.
    selected_branch: str = "B3"
    iterations_requested: int = 0
    iterations_completed: int | None = None
    best_program_branch: str | None = None
    #: ``BaselineMode`` value used to compose final per_task / test-time
    #: skill maps; recorded so the report can label rows accurately.
    baseline_mode: str = ""

    def to_jsonable(self) -> dict:
        return {
            "framework": self.framework,
            "per_task_skills": self.per_task_skills,
            "all_produced_skill_names": self.all_produced_skill_names,
            "notes": self.notes,
            "run_stdout_tail": self.run_stdout_tail,
            "run_stderr_tail": self.run_stderr_tail,
            "run_rc": self.run_rc,
            "skill_files_written": self.skill_files_written,
            "body_sha256": self.body_sha256,
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
            "selected_branch": self.selected_branch,
            "iterations_requested": self.iterations_requested,
            "iterations_completed": self.iterations_completed,
            "best_program_branch": self.best_program_branch,
            "baseline_mode": self.baseline_mode,
        }


def _tomldump(cfg: dict) -> str:
    """Minimal TOML writer for our known shape (EvoSkill uses tomllib-only)."""
    lines: list[str] = []

    def _val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return '"' + v.replace('"', '\\"') + '"'
        if isinstance(v, list):
            return "[" + ", ".join(_val(x) for x in v) + "]"
        raise TypeError(v)

    for section, body in cfg.items():
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {_val(v)}")
        lines.append("")
    return "\n".join(lines)


def scaffold_workdir(
    pilot: PilotManifest, *, wipe: bool = True, config: dict | None = None
) -> None:
    """Create a fresh EvoSkill workdir with config, task, dataset, scorer.

    Setting ``wipe=True`` (the default) removes the existing
    ``evoskill_workdir/`` in full. This is required for re-runs because
    EvoSkill's ``ProgramManager`` calls ``git checkout -b program/iter-
    skill-<n>`` and that fails with rc=128 when the branch already
    exists from a prior run. A clean directory sidesteps the branch
    collision rather than depending on ``git checkout -B`` surgery.
    """
    if wipe and WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    (WORKDIR / ".evoskill").mkdir(exist_ok=True)
    # EvoSkill resolves ``dataset.path`` relative to ``.evoskill/`` so the
    # data directory must live under it.
    (WORKDIR / ".evoskill" / "data").mkdir(exist_ok=True)

    # config.toml
    (WORKDIR / ".evoskill" / "config.toml").write_text(
        _tomldump(config if config is not None else CONFIG_TEMPLATE),
        encoding="utf-8",
    )
    # task.md
    (WORKDIR / ".evoskill" / "task.md").write_text(TASK_MD, encoding="utf-8")
    # scorer.sh (sits at workdir root; EvoSkill runs it with cwd at workdir).
    scorer_path = WORKDIR / "scorer.sh"
    scorer_path.write_text(render_scorer_script(), encoding="utf-8")
    scorer_path.chmod(0o755)
    # dataset: one row per train task, category derived from domain_norm.
    dataset_path = WORKDIR / ".evoskill" / "data" / "skillsbench_train.csv"
    with dataset_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["question", "ground_truth", "category"])
        for t in pilot.train:
            inst_path = REPO_ROOT / "tasks" / t.id / "instruction.md"
            instruction = inst_path.read_text(encoding="utf-8") if inst_path.exists() else t.id
            w.writerow([instruction, t.id, t.domain_norm or "default"])

    # Make the directory a git repo so EvoSkill's ProgramManager can create
    # program branches; the scaffolder expects ``.git``.
    if not (WORKDIR / ".git").is_dir():
        subprocess.run(
            ["git", "init", "-q", str(WORKDIR)],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(WORKDIR), "config", "user.email", "pilot@example.com"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(WORKDIR), "config", "user.name", "pilot"],
            check=False,
            capture_output=True,
        )
        # Seed an initial commit so program/* branches can fork from it.
        subprocess.run(
            ["git", "-C", str(WORKDIR), "add", "."],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(WORKDIR), "commit", "-q", "-m", "pilot scaffold"],
            check=False,
            capture_output=True,
        )


def _invoke_evoskill_run(
    *,
    timeout_s: float = 1500.0,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update(extra_env or {})
    # Ensure EvoSkill finds our custom openai_compat harness.
    env.setdefault("PYTHONPATH", evoskill_root())
    try:
        proc = subprocess.run(
            [evoskill_bin(), "run", "--quiet"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(WORKDIR),
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, "", f"evoskill run timeout after {timeout_s}s: {exc}"


def _extract_best_branch_skills(plan: EvoSkillProducePlan) -> list[Path]:
    """Enumerate all program/* branches, pick the latest one, and return
    paths to ``.claude/skills/*/SKILL.md`` (B3 output shape) or ``*.py``
    files on that branch.
    """
    if not (WORKDIR / ".git").is_dir():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(WORKDIR), "branch", "--list", "program/*"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return []
    branches = [line.strip().lstrip("*").strip() for line in out.splitlines() if line.strip()]
    if not branches:
        return []
    try:
        ordered = subprocess.run(
            [
                "git",
                "-C",
                str(WORKDIR),
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                "refs/heads/program",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        ordered = [b.strip() for b in ordered if b.strip()]
        branch = ordered[0] if ordered else branches[-1]
    except subprocess.CalledProcessError:
        branch = branches[-1]
    plan.notes.append(f"evoskill: best branch = {branch}")
    plan.best_program_branch = branch

    try:
        subprocess.run(
            ["git", "-C", str(WORKDIR), "checkout", "-q", "-f", branch],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        plan.notes.append(f"evoskill: failed to checkout {branch}: {exc}")
        return []

    skills_dir = WORKDIR / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    # B3 shape: .claude/skills/<name>/SKILL.md.
    skill_md_files = sorted(skills_dir.glob("*/SKILL.md"))
    # Legacy (B1) shape: .claude/skills/*.py.
    py_files = sorted(skills_dir.glob("*.py"))
    return skill_md_files + py_files


def _import_skill_md(py_or_md: Path, plan: EvoSkillProducePlan) -> None:
    """Import a single `.claude/skills/<name>/SKILL.md` or `*.py` file.

    For SKILL.md files (B3 shape): reads frontmatter and body, writes a
    namespaced `evoskill-<slug>` entry into Evolution's SkillStore.
    For `.py` files (legacy B1 shape): wraps the source in a markdown
    body with a synthesised description.
    """
    import hashlib

    if py_or_md.name == "SKILL.md":
        text = py_or_md.read_text(encoding="utf-8")
        # Extract frontmatter name/description.
        fm_match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
        if not fm_match:
            plan.notes.append(
                f"evoskill: skill {py_or_md.parent.name} has no YAML frontmatter; synthesising."
            )
            base_name = py_or_md.parent.name
            body = text
        else:
            fm_block = fm_match.group(1)
            base_name = py_or_md.parent.name
            # Try to pull name/description from frontmatter.
            name_m = re.search(r"^name:\s*(\S+)", fm_block, flags=re.MULTILINE)
            if name_m:
                base_name = name_m.group(1).strip()
            body = text[fm_match.end() :]
        namespaced = f"evoskill-{_slug(base_name)}"
        desc_m = re.search(r"^description:\s*(.+)$", text, flags=re.MULTILINE)
        description = (desc_m.group(1).strip() if desc_m else base_name)[:300]
        body_md = body.strip() + "\n"
    else:  # .py legacy shape
        base = py_or_md.stem
        namespaced = f"evoskill-{_slug(base)}"
        body_code = py_or_md.read_text(encoding="utf-8")
        desc_match = re.search(r'"""(.+?)"""', body_code, flags=re.DOTALL)
        description = (desc_match.group(1).strip().splitlines() or [base])[0][:300]
        body_md = (
            f"# {namespaced}\n\n"
            f"_EvoSkill-evolved skill ({base}); source code below._\n\n"
            f"```python\n{body_code}\n```\n"
        )
    write_skill_md(
        name=namespaced,
        description=description or f"EvoSkill-evolved skill {namespaced}",
        body=body_md,
        source="evoskill-frontier-v1",
        extra_metadata={
            "framework": FRAMEWORK_TAG,
            "source_path": str(py_or_md),
            "produced_by_model": "openrouter/inclusionai/ling-2.6-flash:free",
        },
        skills_root=SKILLS_ROOT,
    )
    plan.all_produced_skill_names.append(namespaced)
    plan.body_sha256.append(hashlib.sha256(body_md.encode("utf-8")).hexdigest())


async def _synthesize_skill_from_program(
    workdir: Path,
    best_branch: str,
    model_id: str,
    api_base: str,
    api_key: str,
) -> Path | None:
    """Read winning program from best_branch, ask LLM to write a SKILL.md memory body."""
    from evolution.llm.litellm_backend import LiteLLMBackend

    result = subprocess.run(
        ["git", "-C", str(workdir), "show", f"{best_branch}:_solve.py"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    program_text = result.stdout
    prompt = (
        "Given this Python script that solved a SkillsBench coding task, write a "
        "concise SKILL.md memory section (plain markdown, no YAML frontmatter) "
        "listing generalizable libraries, API patterns, and edge cases used.\n"
        "Focus on transferable technique — NOT task-specific values, filenames, or paths.\n\n"
        f"Task description:\n{TASK_MD}\n\n"
        f"Winning script:\n```python\n{program_text}\n```\n\n"
        "Output ONLY the markdown body — no frontmatter, no fenced code block."
    )
    backend = LiteLLMBackend(model=model_id, api_base=api_base, api_key=api_key, max_tokens=2048)
    response = await backend.complete(messages=[{"role": "user", "content": prompt}])
    body = response.content.strip()
    if not body or len(body) < 50:
        return None
    skill_dir = workdir / ".claude" / "skills" / "evoskill-synthesized"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: evoskill-synthesized\ndescription: Synthesized from best evoskill program\n---\n\n{body}",
        encoding="utf-8",
    )
    return skill_md


def produce(
    *,
    pilot: PilotManifest,
    model_id: str,
    api_base: str,
    api_key: str,
    max_iterations: int = 2,
    branch_mode: str = "B3",
) -> EvoSkillProducePlan:
    plan = EvoSkillProducePlan()
    plan.selected_branch = branch_mode
    plan.iterations_requested = int(max_iterations)

    try:
        evoskill_path = evoskill_bin()
    except RuntimeError as exc:
        plan.notes.append(f"evoskill: {exc}")
        plan.skipped = True
        plan.skipped_reason = str(exc)
        return plan
    if not Path(evoskill_path).exists():
        plan.notes.append(f"evoskill: CLI not installed at {evoskill_path}; adapter cannot run.")
        plan.skipped = True
        plan.skipped_reason = f"evoskill CLI not installed at {evoskill_path}"
        return plan

    # Decide harness up front from model_id so credential checks are
    # routed correctly. Gigachat models use the upstream gigachat harness
    # (OAuth2 via GIGACHAT_CREDENTIALS); everything else goes through
    # Evolution's openai_compat shim (OpenRouter, OpenAI, local vLLM).
    normalised = model_id.split("/")[-1].lower()
    is_gigachat_model = model_id.lower().startswith("gigachat") or normalised.startswith("gigachat")

    or_key = os.environ.get("OPENROUTER_API_KEY")
    honest_equal = os.environ.get("SMOKE4WAY_HONEST_EQUAL") == "1"

    if is_gigachat_model:
        if not (api_key or os.environ.get("GIGACHAT_CREDENTIALS")):
            plan.notes.append(
                "evoskill: gigachat model selected but GIGACHAT_CREDENTIALS "
                "missing; upstream gigachat harness cannot authenticate. Skipping."
            )
            plan.skipped = True
            plan.skipped_reason = "GIGACHAT_CREDENTIALS missing for gigachat harness"
            return plan
    elif not honest_equal and not or_key:
        # Non-gigachat, non-honest-equal: legacy strong-to-weak path needs
        # OpenRouter to reach the hardcoded substitute model.
        plan.notes.append(
            "evoskill: OPENROUTER_API_KEY missing and SMOKE4WAY_HONEST_EQUAL "
            "not set; EvoSkill needs an OpenAI-compatible endpoint reachable "
            "by the openai_compat shim. Skipping."
        )
        plan.skipped = True
        plan.skipped_reason = "OPENROUTER_API_KEY missing"
        return plan

    # B2 honest-zero: skip the run but still emit a model-only baseline.
    # The adapter returns a 'skipped' plan; run_pilot emits 24 rows with
    # skills_used=[] so row-count gate holds.
    if branch_mode == "B2":
        plan.notes.append(
            "evoskill: B2 (honest-zero) selected by preflight — skipping "
            "evoskill run; row-count gate still emits model-only baseline."
        )
        plan.skipped = True
        plan.skipped_reason = (
            "openai_compat cannot drive EvoSkill's agentic skill generation "
            "under B2 mode; preflight Phase A decided JSON-only (B3) and "
            "full tool-loop (B1) are not viable — see preflight_result.json."
        )
        for t in pilot.train + pilot.test:
            plan.per_task_skills.setdefault(t.id, [])
        return plan

    # B1 and B3: run the full evoskill loop. For B3, the openai_compat
    # executor handles the SKILL_PATH file-write inside execute_query
    # (see EvoSkill/src/harness/openai_compat/executor.py); the upstream
    # gigachat executor implements the same SKILL_PATH side-effect.
    cfg = copy.deepcopy(CONFIG_TEMPLATE)
    cfg["evolution"]["iterations"] = int(max_iterations)

    if is_gigachat_model:
        # Honest path for gigachat: upstream gigachat harness, OAuth2 via
        # GIGACHAT_CREDENTIALS. No model substitution — the same
        # gigachat-* model that produced the row is also evaluating it.
        cfg["harness"]["name"] = "gigachat"
        cfg["harness"]["model"] = model_id
        harness_model = model_id
    elif honest_equal:
        # Same model end-to-end via openai_compat shim.
        cfg["harness"]["name"] = "openai_compat"
        cfg["harness"]["model"] = model_id
        harness_model = model_id
    else:
        # Legacy strong-to-weak: hardcoded gemini-3-flash-preview as the
        # strong builder for the frontier loop. Kept for back-compat with
        # earlier 4-way runs; honest comparison should set
        # SMOKE4WAY_HONEST_EQUAL=1 or use a gigachat model directly.
        cfg["harness"]["name"] = "openai_compat"
        cfg["harness"]["model"] = "openrouter/google/gemini-3-flash-preview"
        harness_model = "openrouter/google/gemini-3-flash-preview"

    scaffold_workdir(pilot, wipe=True, config=cfg)
    plan.notes.append(
        f"evoskill: branch_mode={branch_mode}; scaffolded clean workdir at {WORKDIR} "
        f"with harness={cfg['harness']['name']} pointed at {harness_model}"
        + (
            " (gigachat model — upstream harness)"
            if is_gigachat_model
            else (" (SMOKE4WAY_HONEST_EQUAL=1)" if honest_equal else "")
        )
    )

    extra_env: dict[str, str] = {}
    if or_key:
        extra_env["OPENROUTER_API_KEY"] = or_key
    if is_gigachat_model:
        # Forward GigaChat creds so upstream harness can read them.
        creds = api_key or os.environ.get("GIGACHAT_CREDENTIALS", "")
        if creds:
            extra_env["GIGACHAT_CREDENTIALS"] = creds
        extra_env.setdefault(
            "GIGACHAT_SCOPE",
            os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_CORP"),
        )

    rc, out, err = _invoke_evoskill_run(
        timeout_s=1500.0,
        extra_env=extra_env,
    )
    plan.run_rc = rc
    plan.run_stdout_tail = (out or "")[-2000:]
    plan.run_stderr_tail = (err or "")[-2000:]

    if rc != 0:
        plan.notes.append(f"evoskill: run rc={rc}; stderr tail: {(err or '')[-500:]}")

    # Attempt extraction even on rc!=0 — EvoSkill may have produced some
    # skills in partial program branches before failing.
    skill_files = _extract_best_branch_skills(plan)
    if not skill_files and plan.best_program_branch:
        try:
            synth_path = asyncio.run(
                _synthesize_skill_from_program(
                    WORKDIR,
                    plan.best_program_branch,
                    model_id=model_id,
                    api_base=api_base,
                    api_key=api_key,
                )
            )
            if synth_path:
                skill_files = [synth_path]
                plan.notes.append("evoskill: synthesized SKILL.md from best program branch")
        except Exception as exc:
            plan.notes.append(f"evoskill: synthesis failed: {exc}")
    plan.skill_files_written = len(skill_files)
    if not skill_files:
        plan.notes.append(
            "evoskill: no .claude/skills/*/SKILL.md or *.py files in best "
            "program branch (B3 shape: dir-with-SKILL.md; B1 shape: py files)."
        )
        plan.skipped = True
        plan.skipped_reason = (
            f"evoskill run (branch_mode={branch_mode}) completed but no skill "
            "files landed on disk; openai_compat + SKILL_PATH directive did "
            "not write any SKILL.md files."
        )
    for fpath in skill_files:
        _import_skill_md(fpath, plan)

    # Set default per-train-task skills mapping to the full produced set
    # (EvoSkill produces global skills, not per-task).
    for t in pilot.train:
        plan.per_task_skills[t.id] = list(plan.all_produced_skill_names)

    # Compose final per_task_skills via the shared baseline helper. Mode
    # is read from SKILL_BASELINE_MODE (default NONE = produced-only).
    mode = resolve_baseline_mode()
    plan.baseline_mode = mode.value
    plan.per_task_skills = compose_per_task_skills(
        plan.per_task_skills,
        tasks=pilot.train,
        mode=mode,
    )
    plan.notes.append(f"evoskill: per-task skills composed with SKILL_BASELINE_MODE={mode.value}.")
    return plan


def _bundles_to_reflections(bundles: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in bundles:
        passed = int(b.get("passed", 0) or 0)
        total = int(b.get("total", 0) or 0)
        ok = total > 0 and passed == total
        cats = b.get("failure_categories") or []
        out.append(
            {
                "task": b.get("task_id", ""),
                "passed": passed,
                "total": total,
                "pass_rate": b.get("pass_rate", 0.0),
                "root_cause": cats[0] if cats else ("success" if ok else "unknown"),
                "skill_gap": "procedure incomplete or wrong for this task family",
                "suggested_fix": "update skill steps based on trace evidence",
                "severity": "minor" if ok else "major",
                "pattern": "other",
                "evidence_sufficiency": "sufficient",
                "rewrite_recommended": not ok,
            }
        )
    return out


def _backfill_candidate_siblings(
    *,
    repo: Path,
    produced_state: Path,
    skill_id: str,
    seed_body: str,
) -> None:
    """Persist evolved SKILL.md variants as sibling dirs for list_evolved_skill_dirs."""
    import hashlib as _hl

    seed_hash = _hl.sha256(seed_body.strip().encode("utf-8")).hexdigest()
    written: set[str] = set()
    cap = 3

    # Pass 1: anchor may have evolved in-place (best program != base).
    anchor = produced_state / skill_id / "SKILL.md"
    if anchor.is_file():
        anchor_body = anchor.read_text(encoding="utf-8", errors="replace")
        h = _hl.sha256(anchor_body.strip().encode("utf-8")).hexdigest()
        if h != seed_hash:
            sib = produced_state / f"{skill_id}-best"
            sib.mkdir(parents=True, exist_ok=True)
            (sib / "SKILL.md").write_text(anchor_body, encoding="utf-8")
            written.add(h)

    # Pass 2: scan program/* branches for distinct candidates.
    if len(written) >= cap or not (repo / ".git").is_dir():
        return
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", "program/*"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        branches = [b.strip().lstrip("*").strip() for b in out.splitlines() if b.strip()]
        for branch in sorted(branches):
            if len(written) >= cap:
                break
            r = subprocess.run(
                ["git", "-C", str(repo), "show", f"{branch}:.claude/skills/{skill_id}/SKILL.md"],
                capture_output=True,
                text=True,
                check=False,
            )
            if r.returncode != 0 or not r.stdout.strip():
                continue
            body = r.stdout
            h = _hl.sha256(body.strip().encode("utf-8")).hexdigest()
            if h == seed_hash or h in written:
                continue
            safe = branch.replace("/", "_").replace("\\", "_")
            sib = produced_state / f"{skill_id}-{safe}"
            sib.mkdir(parents=True, exist_ok=True)
            (sib / "SKILL.md").write_text(body, encoding="utf-8")
            written.add(h)
    except Exception as exc:
        logger.debug("evoskill: _backfill_candidate_siblings failed: %s", exc)


def edit_skill_from_traces(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    bundles: list[dict],
    run_dir: Path,
    editor_cfg: dict,
    api_key: str,
    round_idx: int = 0,
) -> tuple[str, dict]:
    """Iterative-loop editor: anchor-only rewrite (not ``evoskill-<slug>/``)."""
    import hashlib
    import tempfile

    from evolution.core.parser import load_skill, write_skill_md
    from evolution.evolution.reflect import rewrite_skill
    from evolution.llm.litellm_backend import LiteLLMBackend

    _ = traces_digest
    native_requested = str(
        editor_cfg.get("use_native_evoskill", os.environ.get("EVO_ITERATIVE_NATIVE_EVOSKILL", "1"))
    ).strip().lower() not in {"0", "false", "no"}
    native_error = ""
    if native_requested and editor_cfg.get("corpus_root") and editor_cfg.get("train_ids"):
        try:
            exp_path = REPO_ROOT / "experiments"
            if str(exp_path) not in sys.path:
                sys.path.insert(0, str(exp_path))
            from _shared.evoskill_bridge import TaskRef, evolve_skill
            from _shared.table3_base_seed import BaseSkillSeed, repair_seed_skill_name

            corpus_root = Path(str(editor_cfg["corpus_root"])).resolve()
            role = str(editor_cfg.get("role") or (bundles[0].get("role") if bundles else ""))
            train_ids = [str(t) for t in editor_cfg.get("train_ids") or []]
            if not role:
                raise ValueError("native EvoSkill editor needs role in editor_cfg")
            train_refs: list[TaskRef] = []
            for tid in train_ids:
                # train_ids are role-prefixed refs ("de/sonnet-..."); don't double the role.
                ref_role, plain = tid.split("/", 1) if "/" in tid else (role, tid)
                task_dir = corpus_root / "tasks" / ref_role / plain
                instr = task_dir.joinpath("instruction.md").read_text(
                    encoding="utf-8", errors="replace"
                )
                train_refs.append(TaskRef(task_key=tid, task_dir=task_dir, instruction=instr))
            with tempfile.TemporaryDirectory() as td:
                seed_root = Path(td)
                skill_dir = seed_root / "skills" / skill_id
                skill_dir.mkdir(parents=True)
                seed_body = repair_seed_skill_name(skill_id, skill_body)
                (skill_dir / "SKILL.md").write_text(seed_body, encoding="utf-8")
                seed = BaseSkillSeed(
                    policy="iterative_current_skill_v1",
                    corpus_root=seed_root,
                    source_task_ids=train_ids,
                    skill_names=[skill_id],
                    skill_dirs={skill_id: skill_dir},
                    skill_files={skill_id: "SKILL.md"},
                    skill_hashes={skill_id: hashlib.sha256(seed_body.encode("utf-8")).hexdigest()},
                )
                produced_state = run_dir / "evoskill_state" / f"round{round_idx}"
                result = evolve_skill(
                    skill_names=[skill_id],
                    train_refs=train_refs,
                    base_seed=seed,
                    repo=run_dir / "evoskill_repo" / f"round{round_idx}",
                    solve_root=run_dir / "evoskill_solves" / f"round{round_idx}",
                    produced_state=produced_state,
                    solver_cfg=dict(editor_cfg.get("solver_cfg") or editor_cfg),
                    api_key=str(editor_cfg.get("solver_api_key") or api_key or ""),
                    timeout_s=int(editor_cfg.get("timeout_s", 900)),
                    max_iterations=int(
                        editor_cfg.get("evoskill_iterations")
                        or os.environ.get("EVO_EVOSKILL_ITERATIONS", "4")
                    ),
                    concurrency=int(editor_cfg.get("evoskill_concurrency", 1)),
                    model=(
                        editor_cfg.get("evoskill_model")
                        or editor_cfg.get("served_id_hint")
                        or editor_cfg.get("id")
                    ),
                )
            anchor_md = produced_state / skill_id / "SKILL.md"
            if not anchor_md.is_file():
                raise FileNotFoundError(f"EvoSkill produced no anchor SKILL.md: {anchor_md}")
            from evolution.adapters.skill_artifact_export import (
                fold_evolved_skills,
                list_evolved_skill_dirs,
            )

            evolved_dirs = list_evolved_skill_dirs(produced_state, skill_id)
            if not evolved_dirs:
                _backfill_candidate_siblings(
                    repo=run_dir / "evoskill_repo" / f"round{round_idx}",
                    produced_state=produced_state,
                    skill_id=skill_id,
                    seed_body=seed_body,  # repaired body actually written to repo
                )
                evolved_dirs = list_evolved_skill_dirs(produced_state, skill_id)
            new_body = fold_evolved_skills(
                anchor_md.read_text(encoding="utf-8", errors="replace"), evolved_dirs
            )
            return new_body, {
                "editor": "evoskill",
                "anchor_skill_id": skill_id,
                "import_mode": "native_evoskill_bridge",
                "best_program": result.best_program,
                "best_score": result.best_score,
                "native_score": result.best_score,
                "native_state_dir": str(produced_state),
                "iterations_completed": result.iterations_completed,
                "produced_skill_names": result.produced_skill_names,
                "exported_sibling_skills": [d.name for d in evolved_dirs],
                "exported_sibling_count": len(evolved_dirs),
                "export_body_sha256": hashlib.sha256(new_body.encode("utf-8")).hexdigest(),
                "notes": result.notes,
            }
        except Exception as exc:
            native_error = f"{type(exc).__name__}: {exc}"

    # Falling back to the in-repo rewriter would fake a "native" EvoSkill result; opt-in only.
    allow_fallback = str(
        os.environ.get("EVO_EVOSKILL_ALLOW_EVOLUTION_REWRITE_FALLBACK", "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not allow_fallback:
        return skill_body, {
            "editor": "evoskill",
            "anchor_skill_id": skill_id,
            "import_mode": "native_evoskill_error_no_fallback",
            "native_evoskill_error": native_error,
        }

    async def _run() -> str:
        with tempfile.TemporaryDirectory() as td:
            anchor_dir = Path(td) / skill_id
            anchor_dir.mkdir()
            (anchor_dir / "SKILL.md").write_text(skill_body, encoding="utf-8")
            skill = load_skill(anchor_dir)
            backend_kwargs = {
                "api_base": editor_cfg.get("api_base", ""),
                "api_key": api_key or "EMPTY",
            }
            if isinstance(editor_cfg.get("extra_body"), dict):
                backend_kwargs["extra_body"] = dict(editor_cfg["extra_body"])
            if editor_cfg.get("max_output_tokens"):
                backend_kwargs["max_tokens"] = int(editor_cfg["max_output_tokens"])
            if editor_cfg.get("temperature") is not None:
                backend_kwargs["temperature"] = float(editor_cfg["temperature"])
            backend = LiteLLMBackend(
                model=editor_cfg.get("served_id_hint") or editor_cfg.get("id", ""),
                **backend_kwargs,
            )
            reflections = _bundles_to_reflections(bundles)
            new_body = await rewrite_skill(
                backend,
                skill,
                reflections,
                task_instruction=bundles[0].get("instruction", "") if bundles else "",
                include_best_trace_code=True,
            )
            write_skill_md(skill.skill_md_path, skill.frontmatter, new_body)
            return skill.skill_md_path.read_text(encoding="utf-8")

    new_full = asyncio.run(_run())
    return new_full, {
        "editor": "evoskill",
        "anchor_skill_id": skill_id,
        "import_mode": "evolution_rewrite_fallback",
        "native_evoskill_error": native_error,
    }


def test_time_skills(pilot: PilotManifest, plan: EvoSkillProducePlan) -> dict[str, list[str]]:
    """Test tasks receive framework evolutions (capped at 5), composed
    with the curated baseline per ``SKILL_BASELINE_MODE`` (see
    :func:`compose_test_time_skills`)."""
    extras = list(plan.all_produced_skill_names)[:5] if plan.all_produced_skill_names else []
    return compose_test_time_skills(extras, tasks=pilot.test)


__all__ = [
    "FRAMEWORK_TAG",
    "EvoSkillProducePlan",
    "edit_skill_from_traces",
    "produce",
    "test_time_skills",
    "scaffold_workdir",
]
