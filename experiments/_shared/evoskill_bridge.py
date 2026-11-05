"""EvoSkill <-> AFTER bridge (experiments side; zero EvoSkill source edits).

Runs EvoSkill's native skill-evolution loop (local gpt-oss-120b proposer +
generator via the ``openai_compat`` SDK) with an EvoSkill-native train solver:
candidate branch skills are copied into ``.claude/skills`` and the EvoSkill
openai_compat base agent solves the task on gpt-oss-20b. Pytest scores the
artifacts written by that native runtime. EvoSkill is driven entirely through
its public ``LoopAgents`` / ``SelfImprovingLoop`` API:

  * base agent  -> ``NativeEvoSkillScorerAgent`` (duck-typed: ``run`` +
    ``_get_options`` + ``response_model``); no SDK subclass.
  * scorer      -> ``make_passthrough_scorer`` -> ``float(final_answer)`` so the
    loop optimises the pytest pass-rate directly.
  * proposer/generator -> EvoSkill's native agents on local gpt-oss-120b
    (``set_sdk("openai_compat")``), pointed at an isolated git repo via their
    ``project_root`` option so ``program/*`` branches never touch the live tree.

Held-out integrity: only TRAIN task refs ever enter ``train_pools``/``val_data``;
test tasks live solely in afterbench's eval loop.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_WS_ROOT = Path(__file__).resolve().parents[3]  # .../skill_evolutions
EVOSKILL_ROOT = _WS_ROOT / "EvoSkill"


def _ensure_evoskill_path() -> None:
    p = str(EVOSKILL_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


@dataclass(frozen=True)
class TaskRef:
    task_key: str
    task_dir: Path
    instruction: str


@dataclass
class EvoSkillProduceResult:
    best_program: str
    best_score: float
    iterations_completed: int
    total_cost_usd: float
    frontier: list[tuple[str, float]]
    evolved_skills_dir: Path
    base_seed_check: dict[str, Any]
    train_solve_records: list[dict[str, Any]] = field(default_factory=list)
    produced_skill_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def make_passthrough_scorer() -> Callable[[str, str, str], float]:
    """Scorer: the base agent's ``final_answer`` IS the pytest pass-rate.

    Clamped to [0,1] and tolerant of a non-numeric ``final_answer`` (a parse
    failure scores 0, never raises into the loop)."""

    def _passthrough(question: str, predicted: str, ground_truth: str) -> float:
        try:
            return max(0.0, min(1.0, float(str(predicted).strip())))
        except (TypeError, ValueError):
            return 0.0

    return _passthrough


class NativeEvoSkillScorerAgent:
    """Base agent that scores a train task through EvoSkill's native runtime.

    ``run(question)`` maps the question (a train task's instruction) back to its
    task dir, copies the CURRENTLY checked-out ``.claude/skills`` tree from the
    isolated repo into a task workspace, solves with EvoSkill's openai_compat
    agent, and returns the pytest pass-rate as ``final_answer``. The skill tree
    is copied every call so the score reflects the candidate program branch
    EvoSkill is testing.
    """

    def __init__(
        self,
        *,
        skill_names: list[str],
        repo: Path,
        lookup: dict[str, TaskRef],
        solver_cfg: dict[str, Any],
        api_key: str,
        timeout_s: int,
        solve_root: Path,
    ) -> None:
        _ensure_evoskill_path()
        from src.schemas import AgentResponse

        self.response_model = AgentResponse
        self._skill_names = list(skill_names)
        self._repo = Path(repo)
        self._lookup = dict(lookup)
        self._solver_cfg = dict(solver_cfg)
        self._api_key = api_key
        # Stay under EvoSkill's 17-min (1020s) per-eval asyncio cap so the solve
        # subprocess is killed by its own timeout before the coroutine is.
        self._timeout_s = int(min(int(timeout_s), 900))
        self._solve_root = Path(solve_root)
        self._counter = itertools.count()

    def _write_scorer_artifact(self, ref: TaskRef, res: dict[str, Any], pass_rate: float) -> Path:
        art_dir = self._repo / ".evoskill" / "scorer_artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in ref.task_key)
        path = art_dir / f"{safe}__{time.time_ns()}.json"
        artifact = {
            "framework": "evoskill",
            "task_id": ref.task_key,
            "feedback_policy": os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY", "hidden_summary"),
            "oracle_visible": os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY") == "oracle_diagnostic",
            "tests_visible_to_agent": False,
            "score": pass_rate,
            "passed": res.get("passed"),
            "failed": res.get("failed"),
            "errors": res.get("errors"),
            "total_tests": res.get("total_tests"),
            "outcome": res.get("outcome"),
            "solver_stdout_stderr_tail": str(res.get("response_tail") or res.get("error") or "")[
                -4000:
            ],
            "pytest_stdout_tail": str(res.get("error_tail") or res.get("pytest_log_tail") or "")[
                -4000:
            ],
            "cache_dir": str(res.get("cache_dir") or ""),
        }
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _get_options(self) -> dict[str, Any]:
        served = (
            self._solver_cfg.get("served_id_hint") or self._solver_cfg.get("id") or "gpt-oss-120b"
        )
        return {
            "sdk": "opencode",
            "model": served,
            "model_id": served,
            "cwd": str(self._repo),
            "system": "",
            "tools": [],
        }

    def _solve_blocking(self, ref: TaskRef) -> dict[str, Any]:
        from _shared.native_framework_runners import run_evoskill_skill_dir_test

        cache_dir = self._solve_root / f"{ref.task_key}_{next(self._counter)}"
        return run_evoskill_skill_dir_test(
            task_dir=ref.task_dir,
            instruction=ref.instruction,
            cache_dir=cache_dir,
            skills_dir=self._repo / ".claude" / "skills",
            solver_cfg=self._solver_cfg,
            api_key=self._api_key,
            timeout_s=self._timeout_s,
            native_metrics_extra={
                "train_scorer": "evoskill_native_agent",
                "repo": str(self._repo),
            },
        )

    async def run(self, query: str):
        _ensure_evoskill_path()
        from src.harness import AgentTrace
        from src.schemas import AgentResponse

        started = time.time()
        ref = self._lookup.get((query or "").strip())
        if ref is None:
            return AgentTrace(
                duration_ms=0,
                total_cost_usd=0.0,
                num_turns=1,
                usage={},
                result="no-task-mapping",
                is_error=True,
                output=AgentResponse(final_answer="0.0", reasoning="unmapped question"),
                messages=[],
            )
        try:
            res = await asyncio.to_thread(self._solve_blocking, ref)
        except Exception as exc:  # noqa: BLE001
            res = {"outcome": "hard_fail", "error": f"{type(exc).__name__}: {exc}"[:300]}
        ok = res.get("outcome") == "ok"
        pass_rate = float(res.get("pass_rate") or 0.0) if ok else 0.0
        artifact_path = self._write_scorer_artifact(ref, res, pass_rate)
        summary = (
            f"pass_rate={pass_rate} total_tests={res.get('total_tests')} "
            f"outcome={res.get('outcome')} scorer_artifact={artifact_path}"
        )
        return AgentTrace(
            duration_ms=int((time.time() - started) * 1000),
            total_cost_usd=0.0,
            num_turns=1,
            usage={"tokens_in": 0, "tokens_out": 0},
            result=summary,
            is_error=not ok,
            output=AgentResponse(final_answer=str(pass_rate), reasoning=summary),
            messages=[
                {
                    "role": "tool",
                    "name": "after_pytest_scorer",
                    "content": summary,
                    "artifact_path": str(artifact_path),
                },
                str(res.get("error_tail") or res.get("error") or "")[-4000:],
            ],
        )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _should_reseed_existing_repo(repo: Path) -> bool:
    """Afterbench EvoSkill repos are generated scratch dirs, not durable memory."""
    reuse = os.environ.get("EVO_EVOSKILL_REUSE_REPO", "").strip().lower()
    if reuse in {"1", "true", "yes", "on"}:
        return False
    return repo.name == "evoskill_repo"


def init_isolated_repo(repo: Path, base_seed: Any) -> dict[str, Any]:
    """Seed an isolated scratch git repo with the handcraft skill + an initial
    commit so ``ProgramManager`` can branch ``program/*`` without touching the
    live tree. Local commit identity is scoped to this throwaway repo only."""
    from _shared.table3_base_seed import copy_base_skill_dirs, verify_base_skill_dirs

    repo.mkdir(parents=True, exist_ok=True)
    if (repo / ".git").is_dir():
        if _should_reseed_existing_repo(repo):
            shutil.rmtree(repo)
            repo.mkdir(parents=True, exist_ok=True)
        else:
            check = verify_base_skill_dirs(base_seed, repo / ".claude" / "skills")
            check["existing_repo"] = True
            check["reused_repo"] = True
            return check

    if (repo / ".git").is_dir():
        check = verify_base_skill_dirs(base_seed, repo / ".claude" / "skills")
        check["existing_repo"] = True
        return check

    (repo / ".evoskill").mkdir(
        parents=True, exist_ok=True
    )  # loop writes feedback here before its own lazy mkdir
    skills_root = repo / ".claude" / "skills"
    copy_base_skill_dirs(base_seed, skills_root)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "evoskill")
    _git(repo, "config", "user.email", "evoskill@local")
    _git(repo, "config", "commit.gpgsign", "false")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing_exclude = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    ignore_lines = [
        ".evoskill/scorer_artifacts/",
        ".evoskill/loop_checkpoint.json",
        ".evoskill/feedback_history.md",
    ]
    with exclude.open("a", encoding="utf-8") as fh:
        for line in ignore_lines:
            if line not in existing_exclude:
                fh.write(f"{line}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "seed: handcraft skills")
    return verify_base_skill_dirs(base_seed, skills_root)


def _assert_seed_names(repo: Path, skill_names: list[str], notes: list[str]) -> None:
    """Record missing staged anchors.

    Skill identity is the folder name under ``.claude/skills``. Handcraft
    frontmatter may use a display/category name and is not authoritative here.
    """
    for s in skill_names:
        p = repo / ".claude" / "skills" / s / "SKILL.md"
        if not p.is_file():
            notes.append(f"WARN seed anchor missing: skill={s!r} path={str(p)!r}")


def init_seed_program_repo(
    *, repo: Path, skill_id: str, skill_body: str, model: str = ""
) -> dict[str, Any]:
    """v0 repo with a scored ``program/base`` so best_program eval works pre-evolution.

    Lets the seed condition run through the same EvoSkill program runtime as the evolved
    condition, instead of bare skill-text injection (seed/evolved runtime symmetry)."""
    import hashlib
    import tempfile

    from _shared.table3_base_seed import BaseSkillSeed, repair_seed_skill_name

    seed_body = repair_seed_skill_name(skill_id, skill_body)
    with tempfile.TemporaryDirectory() as td:
        skill_dir = Path(td) / "skills" / skill_id
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(seed_body, encoding="utf-8")
        seed = BaseSkillSeed(
            policy="iterative_v0_seed_repo",
            corpus_root=Path(td),
            source_task_ids=[],
            skill_names=[skill_id],
            skill_dirs={skill_id: skill_dir},
            skill_files={skill_id: "SKILL.md"},
            skill_hashes={skill_id: hashlib.sha256(seed_body.encode("utf-8")).hexdigest()},
        )
        check = init_isolated_repo(repo, seed)
    _ensure_evoskill_path()
    from src.registry import ProgramManager
    from src.registry.models import ProgramConfig

    manager = ProgramManager(cwd=repo)
    if "base" not in manager.list_programs():
        manager.create_program(
            "base",
            ProgramConfig(
                name="base",
                system_prompt={"type": "text", "content": ""},
                allowed_tools=[],
                metadata={"model_id": model or "seed"},
            ),
        )
    if manager.get_best_from_frontier() is None:
        manager.update_frontier("base", 0.0)
    check["v0_base_program"] = "base"
    return check


def evolve_skill(
    *,
    skill_names: list[str],
    train_refs: list[TaskRef],
    base_seed: Any,
    repo: Path,
    solve_root: Path,
    produced_state: Path,
    solver_cfg: dict[str, Any],
    api_key: str,
    timeout_s: int,
    max_iterations: int,
    concurrency: int,
    model: str | None = None,
) -> EvoSkillProduceResult:
    """Run the EvoSkill loop on TRAIN tasks; return the evolved skill state.

    ``produced_state`` receives a copy of the best branch's ``.claude/skills``
    so the eval seam's ``_evolution_skill_bodies`` contract is satisfied
    (``state["skills_dir"]/<skill>/SKILL.md``)."""
    import shutil

    _ensure_evoskill_path()
    from src.agent_profiles.prompt_generator import get_prompt_generator_options
    from src.agent_profiles.prompt_proposer import get_prompt_proposer_options
    from src.agent_profiles.skill_generator import get_skill_generator_options
    from src.agent_profiles.skill_proposer import get_skill_proposer_options
    from src.harness import Agent, set_sdk
    from src.loop import LoopAgents, LoopConfig, SelfImprovingLoop
    from src.registry import ProgramManager
    from src.schemas import (
        PromptGeneratorResponse,
        PromptProposerResponse,
        SkillProposerResponse,
        ToolGeneratorResponse,
    )

    set_sdk("openai_compat")
    notes: list[str] = []
    base_seed_check = init_isolated_repo(repo, base_seed)
    _assert_seed_names(repo, skill_names, notes)

    lookup = {r.instruction.strip(): r for r in train_refs}
    if len(lookup) != len(train_refs):
        notes.append("WARN duplicate train instructions collapsed in lookup")
    category = skill_names[0] if skill_names else "skill"
    train_pools = {category: [(r.instruction, r.task_key) for r in train_refs]}
    val_data = [(r.instruction, r.task_key, category) for r in train_refs]

    base = NativeEvoSkillScorerAgent(
        skill_names=skill_names,
        repo=repo,
        lookup=lookup,
        solver_cfg=solver_cfg,
        api_key=api_key,
        timeout_s=timeout_s,
        solve_root=solve_root,
    )
    rp = str(repo)
    agent_timeout_s = max(
        30,
        min(
            int(timeout_s),
            int(os.environ.get("EVO_EVOSKILL_AGENT_TIMEOUT_S", "300")),
        ),
    )
    agent_retries = max(1, int(os.environ.get("EVO_EVOSKILL_AGENT_RETRIES", "1")))
    agents = LoopAgents(
        base=base,
        skill_proposer=Agent(
            get_skill_proposer_options(model=model, project_root=rp),
            SkillProposerResponse,
            timeout_seconds=agent_timeout_s,
            max_retries=agent_retries,
        ),
        prompt_proposer=Agent(
            get_prompt_proposer_options(model=model, project_root=rp),
            PromptProposerResponse,
            timeout_seconds=agent_timeout_s,
            max_retries=agent_retries,
        ),
        skill_generator=Agent(
            get_skill_generator_options(model=model, project_root=rp),
            ToolGeneratorResponse,
            timeout_seconds=agent_timeout_s,
            max_retries=agent_retries,
        ),
        prompt_generator=Agent(
            get_prompt_generator_options(model=model, project_root=rp),
            PromptGeneratorResponse,
            timeout_seconds=agent_timeout_s,
            max_retries=agent_retries,
        ),
    )
    config = LoopConfig(
        max_iterations=max_iterations,
        evolution_mode="skill_only",
        cache_enabled=False,
        concurrency=max(1, int(concurrency)),
    )
    manager = ProgramManager(cwd=repo)
    loop = SelfImprovingLoop(
        config, agents, manager, train_pools, val_data, scorer=make_passthrough_scorer()
    )

    try:
        result = asyncio.run(loop.run())
    except Exception as exc:  # noqa: BLE001
        notes.append(
            "WARN evoskill loop raised after native execution; salvaging frontier: "
            f"{type(exc).__name__}: {str(exc)[:300]}"
        )
        frontier = manager.get_frontier_with_scores()
        best = manager.get_best_from_frontier()
        programs = set(manager.list_programs())
        if not best and "base" in programs:
            best = "base"
        if best and best in programs:
            with suppress(Exception):
                manager.switch_to(best)
        result = SimpleNamespace(
            best_program=best or "working_tree",
            best_score=float(frontier[0][1]) if frontier else 0.0,
            iterations_completed=0,
            total_cost_usd=0.0,
            frontier=frontier,
        )
    else:
        if result.best_program in set(manager.list_programs()):
            manager.switch_to(result.best_program)
        else:
            notes.append(
                f"WARN evoskill best program {result.best_program!r} missing; using current tree"
            )

    if produced_state.exists():
        shutil.rmtree(produced_state)
    produced_state.mkdir(parents=True, exist_ok=True)
    src_skills = repo / ".claude" / "skills"
    produced_skill_names: list[str] = []
    for live in sorted(src_skills.iterdir()):
        if live.is_dir() and (live / "SKILL.md").is_file():
            shutil.copytree(live, produced_state / live.name, symlinks=False)
            produced_skill_names.append(live.name)

    records = _final_train_solves(base, train_refs, solver_cfg)
    notes.append(
        f"evoskill: best={result.best_program} score={result.best_score:.4f} "
        f"iters={result.iterations_completed} frontier={result.frontier}"
    )
    return EvoSkillProduceResult(
        best_program=result.best_program,
        best_score=float(result.best_score),
        iterations_completed=int(result.iterations_completed),
        total_cost_usd=float(result.total_cost_usd),
        frontier=list(result.frontier),
        evolved_skills_dir=produced_state,
        base_seed_check=base_seed_check,
        train_solve_records=records,
        produced_skill_names=produced_skill_names,
        notes=notes,
    )


def _final_train_solves(
    base: NativeEvoSkillScorerAgent, train_refs: list[TaskRef], solver_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """One solve per train task with the best evolved skill (the active branch),
    shaped like ``_produce_evolution``'s ``train_solve_summary`` records so the
    produce contract (>=1 completed solve+pytest) can gate ``valid_for_eval``."""
    records: list[dict[str, Any]] = []
    for idx, ref in enumerate(train_refs):
        try:
            res = base._solve_blocking(ref)
        except Exception as exc:  # noqa: BLE001
            res = {"outcome": "hard_fail", "error": f"{type(exc).__name__}: {exc}"[:300]}
        total = int(res.get("total_tests", 0) or 0)
        records.append(
            {
                "spec_idx": idx,
                "task_id": ref.task_key,
                "source_model_id": solver_cfg.get("id"),
                "attempt_seed": 0,
                "solve_status": "completed" if total > 0 else "hard_fail",
                "pytest_status": "completed" if total > 0 else "not_run",
                "pytest_pass_rate": float(res.get("pass_rate", 0.0) or 0.0),
                "pytest_total_tests": total,
                "api_base": solver_cfg.get("api_base", ""),
                "error_tail": str(res.get("error_tail") or res.get("error") or ""),
                "code_extraction_failed": bool(res.get("code_extraction_failed", False)),
                "code_extraction_retried": bool(res.get("code_extraction_retried", False)),
            }
        )
    return records
