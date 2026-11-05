"""Evolution framework adapter.

Evolution's baseline is the human-curated SkillsBench skill set that
``evo import-skillsbench`` has already placed in ``.agents/skills/``. The
adapter therefore has two responsibilities:

1. ``produce_baseline()`` reports, for each train task, which already-on-disk
   skills should be injected when the task is solved. This is a no-op copy
   into a framework-tagged directory so the downstream report can label
   rows consistently.
2. ``evolve_skills_one_round()`` optionally runs one full
   ``evo reflect → rewrite → commit`` pass per (skill, failing-trace) pair,
   driven by the ``--enable-evolve`` flag in ``run_pilot.py``. The
   orchestrator handles per-model isolation (snapshot baseline versions on
   entry, restore on exit) so secondary-model passes don't start from
   primary-evolved skills.

Task skill requirements are read via :func:`evolution.adapters.evaluator.required_skills`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from evolution.core.store import SkillStore
from evolution.eval._proc import run_capture
from evolution.evolution.meta_skill import (
    PROMOTED_ELIGIBLE,
    build_rejected_entry,
    load_round_records,
    summarize_reflection_audit,
    write_round_record,
)
from evolution.evolution.reflect import load_reflection
from evolution.lineage.traces import TraceStore, load_trace_evidence
from evolution.splits import SplitManifest

from ._paths import evo_bin, tasks_root
from .common import PROJECT_ROOT, REPO_ROOT, SKILLS_ROOT, PilotManifest
from .evaluator import required_skills

logger = logging.getLogger(__name__)

FRAMEWORK_TAG = "evolution"
REWRITE_AUDIT_RECORD_KEYS = (
    "rewrite_mode",
    "candidate_status",
    "gate_reason",
    "reason",
    "parent_preservation",
    "codex_usage",
    "codex_diagnostics",
    "tokens_used",
    "candidate_debug_dir",
    "parent_skill_path",
    "parent_body_sha256",
    "candidate_body_sha256",
    "computed_churn",
    "new_headings",
    "edit_budget_frac",
)

_DEFAULT_PAPER_MANIFEST_REGEX = r"table3|table4|publication"


class OracleOnPaperError(RuntimeError):
    """Raised when an oracle-visible reflector would run a paper manifest."""


def _is_paper_manifest(manifest_name: str | None) -> bool:
    if not manifest_name:
        return False
    pattern = os.environ.get("EVO_PAPER_MANIFEST_REGEX") or _DEFAULT_PAPER_MANIFEST_REGEX
    try:
        return re.search(pattern, manifest_name, re.IGNORECASE) is not None
    except re.error:
        return re.search(_DEFAULT_PAPER_MANIFEST_REGEX, manifest_name, re.IGNORECASE) is not None


def oracle_visibility_mode(manifest_name: str | None = None) -> str:
    """Classify reflector oracle visibility (rev A2): blind/oracle/forced_oracle."""
    if os.environ.get("EVO_REFLECT_BLIND") == "1":
        return "blind"
    if (
        _is_paper_manifest(manifest_name)
        and os.environ.get("EVO_REFLECT_ALLOW_ORACLE_ON_PAPER") == "1"
    ):
        return "forced_oracle"
    return "oracle"


def _manifest_name_from_path(path: Path | str | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("name"):
            return str(raw["name"])
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return p.stem


def _publication_preflight(manifest_name: str | None) -> str:
    """Fail-loud guard (rev A1) + visibility label (rev A2).

    Aborts when an oracle-visible reflector targets a paper-profile manifest
    without the explicit ``EVO_REFLECT_ALLOW_ORACLE_ON_PAPER=1`` override.
    """
    mode = oracle_visibility_mode(manifest_name)
    if mode == "oracle" and _is_paper_manifest(manifest_name):
        raise OracleOnPaperError(
            f"Oracle-visible reflector blocked on paper manifest {manifest_name!r}: "
            "set EVO_REFLECT_BLIND=1 for publication runs, or override with "
            "EVO_REFLECT_ALLOW_ORACLE_ON_PAPER=1 to run oracle-visible deliberately."
        )
    return mode


@dataclass
class ProducePlan:
    """Skills to inject per task, keyed by task_id.

    Evolution imports SkillsBench's curated skills via ``evo import-
    skillsbench`` before the pilot runs, so those skills are **preloaded**,
    not produced by this run. We track them separately from any skills the
    optional evolve loop mints, so the report can distinguish "preloaded
    baseline" from "new skills produced".
    """

    framework: str = FRAMEWORK_TAG
    per_task_skills: dict[str, list[str]] = field(default_factory=dict)
    #: Preloaded SkillsBench baseline skills used during evaluation.
    preloaded_skill_names: list[str] = field(default_factory=list)
    #: Skills genuinely produced/mutated by Evolution's evolve loop
    #: (``evo rewrite`` + ``evo commit``). Empty unless ``evolve_skills_one_round``
    #: was called by the orchestrator.
    newly_produced_skill_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all_produced_skill_names(self) -> list[str]:
        """Legacy alias = preloaded + newly produced. Prefer the split
        fields in new code; this alias keeps old callers working."""
        return list(self.preloaded_skill_names) + list(self.newly_produced_skill_names)


def _store_has(name: str) -> bool:
    # Mutable skill state lives under the per-worker PROJECT_ROOT, not the
    # shared code checkout.
    return SkillStore(project_dir=PROJECT_ROOT).has(name)


def _resolved_skill_name(name: str) -> str | None:
    """Return the on-disk skill name matching ``name``, or ``None``.

    sb-bench's runtime manifest carries skills with underscores
    (``executable_schema_contracting``). Evolution's
    ``SkillFrontmatter.validate_name`` rejects underscores, so
    ``seed_evolution_skills.py`` writes the SKILL.md under the
    hyphenated form (``executable-schema-contracting``). This helper
    lets callers normalise either way and find the on-disk skill.
    """
    if _store_has(name):
        return name
    norm = name.replace("_", "-").lower()
    if norm != name and _store_has(norm):
        return norm
    return None


def produce_baseline(pilot: PilotManifest) -> ProducePlan:
    """Plan skills for each train task using the human-curated baseline.

    Populates ``preloaded_skill_names`` (from ``evo import-skillsbench``).
    Does NOT populate ``newly_produced_skill_names`` — Evolution's evolve
    loop mutates *existing* preloaded skills (via
    ``evo reflect → rewrite → commit`` in :func:`evolve_skills_one_round`)
    rather than creating new skill names. The version bump is recorded
    in ``evolve_versions`` instead.
    """
    plan = ProducePlan()
    seen: list[str] = []
    for t in pilot.train:
        declared = required_skills(t.id) or list(t.skills)
        # Resolve each declared name (underscored or hyphenated) to its
        # on-disk equivalent so downstream eval loads the right SKILL.md.
        resolved_pairs = [(s, _resolved_skill_name(s)) for s in declared]
        usable = [r for _s, r in resolved_pairs if r is not None]
        missing = [s for s, r in resolved_pairs if r is None]
        plan.per_task_skills[t.id] = usable
        for r in usable:
            if r not in seen:
                seen.append(r)
        if missing:
            plan.notes.append(f"{t.id}: skipped unknown skills {missing} (not in store)")
    plan.preloaded_skill_names = seen
    # Evolve mutates existing skills (version bump), never creates new names.
    plan.notes.append(
        "evolution: produce_baseline imports SkillsBench preloaded skills. "
        "Evolution does not create new skill names; the orchestrator calls "
        "`evolve_skills_one_round` to bump existing skills' active versions via "
        "`evo reflect → rewrite → commit` (see `evolve_versions` field)."
    )
    return plan


def _restrict_plan_to_allowed_skills(
    plan: ProducePlan,
    allowed_skill_names: list[str] | tuple[str, ...] | set[str] | None,
) -> None:
    """Restrict a produce plan to an explicit skill allow-list in place."""

    if allowed_skill_names is None:
        return
    allowed = {str(s) for s in allowed_skill_names}
    seen: list[str] = []
    for task_id, skills in list(plan.per_task_skills.items()):
        kept = [s for s in skills if s in allowed]
        dropped = [s for s in skills if s not in allowed]
        plan.per_task_skills[task_id] = kept
        for s in kept:
            if s not in seen:
                seen.append(s)
        if dropped:
            plan.notes.append(f"{task_id}: anchor_only dropped non-anchor skills {dropped}")
    plan.preloaded_skill_names = seen
    plan.notes.append(
        f"evolution: restricted skill selection to explicit anchor allow-list {sorted(allowed)}"
    )


def test_time_skills(pilot: PilotManifest, plan: ProducePlan) -> dict[str, list[str]]:
    """Map each held-out test task to the skills it should receive.

    Evolution operates on the full human-curated skill library already
    present in ``.agents/skills/``. For a held-out test task, use the
    task's declared skill list (from ``task.toml``) intersected with what
    the store currently has. This matches Evolution's normal operating
    mode and is consistent with the other frameworks, which each expose a
    namespace-prefixed variant of the same skill names.
    """
    out: dict[str, list[str]] = {}
    for t in pilot.test:
        declared = required_skills(t.id) or list(t.skills)
        out[t.id] = [r for r in (_resolved_skill_name(s) for s in declared) if r is not None]
    return out


def _current_skill_version(skill_name: str) -> str | None:
    """Read .agents/skills/<skill>/.evolution/lineage.json → current_version."""
    p = SKILLS_ROOT / skill_name / ".evolution" / "lineage.json"
    if not p.exists():
        return None
    try:
        return str(json.loads(p.read_text()).get("current_version") or "") or None
    except Exception:
        return None


def _max_committed_version(skill_name: str) -> str | None:
    """Highest ``vN`` label in lineage.json (the just-committed candidate under
    ``commit --no-promote``, where ``current_version`` still points at parent)."""
    p = SKILLS_ROOT / skill_name / ".evolution" / "lineage.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    best = 0
    for e in data.get("lineage", []) or []:
        v = str(e.get("version") or "")
        if v.startswith("v") and v[1:].isdigit():
            best = max(best, int(v[1:]))
    return f"v{best}" if best else None


def _model_substring(model_id: str) -> str:
    """Pick a substring of the model id useful for `--trace-model` filter.

    Filter substrings match what's stored in trace `result.json["model"]`.
    Examples: ``openai/Qwen/Qwen3.6-35B-A3B`` -> ``Qwen3.6``,
    ``openrouter/inclusionai/ling-2.6-flash:free`` -> ``ling-2.6``.
    """
    m = model_id
    if "Qwen3" in m:
        match = re.search(r"Qwen3\.\d+", m)
        return match.group(0) if match else "Qwen3"
    if "ling" in m.lower():
        match = re.search(r"ling-\d+\.\d+", m)
        return match.group(0) if match else "ling"
    # Fallback: strip prefix and take first hyphenated segment.
    for prefix in ("openrouter/", "openai/"):
        if m.startswith(prefix):
            m = m[len(prefix) :]
    return m.split("/")[-1].split(":")[0]


def _archive_stale_reflections(skill_name: str) -> list[Path]:
    """Temporarily move reflection.json files aside before current-round reflect."""
    archived: list[Path] = []
    traces_dir = SKILLS_ROOT / skill_name / ".traces"
    if not traces_dir.is_dir():
        return archived
    for refl in traces_dir.glob("*/reflection.json"):
        target = refl.with_suffix(".json.archived")
        if target.exists():
            target.unlink()
        refl.rename(target)
        archived.append(target)
    return archived


def _restore_archived_reflections(archived: list[Path]) -> None:
    """Reverse of _archive_stale_reflections."""
    for p in archived:
        if p.exists() and p.name.endswith(".json.archived"):
            target = p.with_suffix("")  # drop .archived
            if target.exists():
                target.unlink()
            p.rename(target)


def _select_all_failing_traces(
    train_results: list,
    skill_name: str,
) -> list[tuple[str, str | None]]:
    """Return every verifier-covered failing trace for ``skill_name``.

    Items are sorted by worst pass rate first, then task id so caller can use
    a deterministic first task for rewrite while still reflecting on all traces.
    """
    candidates: list[tuple[float, str, str | None]] = []
    for r in train_results:
        if getattr(r, "success", True):
            continue
        pr = float(getattr(r, "pass_rate", 0.0))
        total_tests = int(getattr(r, "total_tests", 0) or 0)
        if total_tests <= 0:
            continue
        skills = list(getattr(r, "skills_used", []) or [])
        if skill_name in skills:
            candidates.append((pr, r.task_id, getattr(r, "source_model_id", None)))
    candidates.sort(key=lambda x: (x[0], x[1]))
    return [(task_id, source_model_id) for _pr, task_id, source_model_id in candidates]


def _select_one_failing_trace(
    train_results: list,
    skill_name: str,
) -> tuple[str, str | None] | None:
    """Backward-compatible selector for older tests and callers."""

    selected = _select_all_failing_traces(train_results, skill_name)
    return selected[0] if selected else None


# Hard-abort: a retry cannot help; surface immediately so a long unattended
# run fails loudly instead of burning `attempts` on a misconfig.
_HARD_ABORT_MARKERS = (
    "codex executable not found",
    "invalid_grant",
)

_EXISTING_TRANSIENT_MARKERS = (
    "cannot connect to host",
    "connect call failed",
    "connection timed out",
    "connection reset",
    "temporarily unavailable",
    "rate limit",
    "openrouterexception",
    "litellm.apierror",
    "apierror:",
    "timeout",
)

# Coding-agent CLI (codex/claude-code/hermes) transient failures under concurrency.
_CODEX_TRANSIENT_MARKERS = (
    "403",
    "forbidden",
    "429",
    "codexbackenderror",
    "agentbackenderror",
    "produced no output",
    "empty completion",
)


def _should_retry_evo_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    """Retry predicate = union(existing, codex-transient) minus hard-aborts.

    Hard-abort markers win: a missing codex binary or a bad credential
    (invalid_grant) is never transient, so retrying just wastes the run's
    time budget. Everything else that matches a known transient marker is
    retried with the existing backoff/attempt cap.
    """
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    if any(marker in text for marker in _HARD_ABORT_MARKERS):
        return False
    return any(
        marker in text for marker in (*_EXISTING_TRANSIENT_MARKERS, *_CODEX_TRANSIENT_MARKERS)
    )


def _run_evo_cli_with_retries(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout_s: float,
    attempts: int | None = None,
) -> subprocess.CompletedProcess[str]:
    if attempts is None:
        attempts = max(1, int(os.environ.get("EVO_EVOLVE_RETRIES", "3") or "3"))
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, attempts + 1):
        proc = run_capture(
            cmd,
            cwd=str(PROJECT_ROOT),
            timeout=timeout_s,
            env=env,
        )
        last = proc
        if proc.returncode == 0:
            return proc
        if attempt >= attempts or not _should_retry_evo_failure(proc):
            return proc
        delay = min(15 * attempt, 90)
        logger.warning(
            "evolve CLI transient failure rc=%s attempt=%s/%s; retrying in %ss: %s",
            proc.returncode,
            attempt,
            attempts,
            delay,
            " ".join(cmd),
        )
        time.sleep(delay)
    assert last is not None
    return last


def evolve_skills_one_round(
    *,
    skill_to_failing_tasks: dict[str, list[str]],
    model_id: str,
    api_base: str,
    api_key: str,
    manifest_path: Path,
    trace_model_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
    evolution_tasks_dir: Path | str | None = None,
    timeout_reflect_s: float | None = None,
    timeout_rewrite_s: float | None = None,
    timeout_commit_s: float | None = None,
    promotion_validate=None,
    promotion_validation_count: int = 0,
    rewrite_audit_root: Path | None = None,
    row_id: str = "",
) -> dict[str, dict]:
    """Drive `evo reflect → rewrite → commit` once per skill.

    ``model_id`` drives ``-m`` (the reflector LLM). ``trace_model_id`` drives
    ``--trace-model`` (a scalar substring filter over on-disk traces); fall
    back to ``model_id`` only when caller leaves it None.
    """
    out: dict[str, dict] = {}
    oracle_visibility = _publication_preflight(_manifest_name_from_path(manifest_path))
    timeout_reflect_s = (
        timeout_reflect_s
        if timeout_reflect_s is not None
        else float(os.environ.get("EVO_REFLECT_TIMEOUT_S", "300"))
    )
    timeout_rewrite_s = (
        timeout_rewrite_s
        if timeout_rewrite_s is not None
        else float(os.environ.get("EVO_REWRITE_TIMEOUT_S", "600"))
    )
    timeout_commit_s = (
        timeout_commit_s
        if timeout_commit_s is not None
        else float(os.environ.get("EVO_COMMIT_TIMEOUT_S", "60"))
    )
    model_substring = _model_substring(trace_model_id) if trace_model_id else None

    # PROJECT_ROOT has no tasks/ checkout; pass the read-only tasks dir.
    cli_tasks_dir = str(Path(evolution_tasks_dir or tasks_root()).resolve())

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = api_key or env.get("OPENAI_API_KEY") or "dummy-local"
    if api_key and "openrouter" in (model_id or "").lower():
        env["OPENROUTER_API_KEY"] = api_key
    if temperature is not None:
        env["EVO_LLM_TEMPERATURE"] = str(float(temperature))
    if max_tokens is not None:
        env["EVO_LLM_MAX_TOKENS"] = str(int(max_tokens))
    if isinstance(extra_body, dict) and extra_body.get("reasoning_effort"):
        env["EVO_LLM_REASONING_EFFORT"] = str(extra_body["reasoning_effort"])
    env["LITELLM_LOG"] = "ERROR"
    # Lets the reflect subprocess scope the rejected-edit buffer to this row's
    # own prior-round rejections (rev E2 cross-row isolation).
    if row_id:
        env["EVO_REFLECT_ROW_ID"] = row_id

    session_mode = os.environ.get("EVO_REWRITE_MODE", "text-return") == "agentic-session"
    for skill, failing_tasks in skill_to_failing_tasks.items():
        failing_tasks = list(dict.fromkeys(failing_tasks))
        failing_task = failing_tasks[0] if failing_tasks else None
        before_version = _current_skill_version(skill)
        record: dict = {
            "evolve_rounds": 0,
            "row_id": row_id,
            "oracle_visibility": oracle_visibility,
            "before_version": before_version,
            "after_version": before_version,
            "failing_task": failing_task,
            "failing_tasks": failing_tasks,
            "trace_ids": [],
            "reflect_rc": None,
            "rewrite_rc": None,
            "commit_rc": None,
            "reflect_stderr_tail": "",
            "rewrite_stderr_tail": "",
            "rewrite_stdout_tail": "",
            "commit_stderr_tail": "",
            "notes": [],
        }
        # Registered up front so per-skill telemetry survives early `continue`.
        out[skill] = record
        if not failing_task:
            record["notes"].append(
                f"{skill}: no failing train trace under model={model_substring}; skipping evolve."
            )
            continue

        # 1. Trace isolation pre-sweep.
        archived = _archive_stale_reflections(skill_name=skill)
        record["notes"].append(
            f"{skill}: archived {len(archived)} stale reflection(s) before evolve."
        )

        try:
            # 2. evo reflect — scalar --task only, one call per failing task.
            record["trace_ids"] = list(failing_tasks)
            reflect_stdout_parts: list[str] = []
            reflect_stderr_parts: list[str] = []
            reflect_failed = False
            # agentic-session mode unifies reflect+rewrite inside one session that
            # diagnoses from the raw traces and writes DIAGNOSIS.json; the per-trace
            # reflect pass is redundant (and, with a paid reflector, would be a
            # wasteful single-shot call whose failure would skip the rewrite).
            reflect_tasks = [] if session_mode else failing_tasks
            if session_mode:
                record["notes"].append(
                    f"{skill}: agentic-session mode — reflect subsumed into the session; "
                    f"skipping per-trace reflect."
                )
            for task_name in reflect_tasks:
                reflect_cmd = [
                    evo_bin(),
                    "reflect",
                    skill,
                    "-m",
                    model_id,
                    "--api-base",
                    api_base,
                    "-t",
                    task_name,
                    "--manifest",
                    str(manifest_path),
                    "--tasks-dir",
                    cli_tasks_dir,
                ]
                if model_substring:
                    reflect_cmd.extend(["--trace-model", model_substring])
                logger.info("evolve: %s", " ".join(reflect_cmd))
                try:
                    proc = _run_evo_cli_with_retries(
                        reflect_cmd,
                        timeout_s=timeout_reflect_s,
                        env=env,
                    )
                    record["reflect_rc"] = proc.returncode
                    reflect_stdout_parts.append(proc.stdout or "")
                    reflect_stderr_parts.append(proc.stderr or "")
                    if proc.returncode != 0:
                        reflect_failed = True
                        record["notes"].append(
                            f"{skill}: reflect task={task_name} rc={proc.returncode}; "
                            f"stderr tail: {(proc.stderr or '')[-300:]}"
                        )
                except subprocess.TimeoutExpired:
                    record["reflect_rc"] = -1
                    reflect_failed = True
                    record["notes"].append(
                        f"{skill}: reflect task={task_name} TIMEOUT ({timeout_reflect_s}s)"
                    )
            record["reflect_stderr_tail"] = "\n".join(reflect_stderr_parts)[-400:]
            record["reflect_stdout_tail"] = "\n".join(reflect_stdout_parts)[-400:]
            if reflect_failed:
                if os.environ.get("EVO_TOLERATE_PARTIAL_REFLECT", "").lower() in (
                    "1",
                    "true",
                    "yes",
                ):
                    record["notes"].append(
                        f"{skill}: EVO_TOLERATE_PARTIAL_REFLECT=1 — proceeding despite "
                        f"partial reflect failures."
                    )
                    record["reflect_rc"] = 0
                else:
                    continue
            if record["reflect_rc"] is None:
                record["reflect_rc"] = 0
            record["notes"].append(
                f"{skill}: reflect rc=0 across {len(failing_tasks)} task(s); "
                f"stdout tail: {record['reflect_stdout_tail'][-200:]}"
            )

            try:
                sk = SkillStore(project_dir=PROJECT_ROOT).get(skill)
                trace_store = TraceStore(sk)
                audit: list[dict] = []
                anomalies: list[str] = []
                n_act = n_low = 0
                for trace in trace_store.list():
                    trace_dir = trace_store.traces_dir / trace["id"]
                    refl = load_reflection(trace_dir)
                    if not refl:
                        continue
                    ev = load_trace_evidence(trace_dir) or {}
                    ctx = ev.get("trace_context") or trace.get("trace_context") or {}
                    if not ctx:
                        anomalies.append(f"{trace['id']}: missing trace_context")
                    suf = refl.get("evidence_sufficiency")
                    rec_ok = refl.get("rewrite_recommended")
                    if suf == "insufficient" or rec_ok is False:
                        n_low += 1
                    else:
                        n_act += 1
                    audit.append(
                        {
                            "trace_id": refl.get("trace_id", trace["id"]),
                            "trace_context": ctx,
                            "evidence_schema": ev.get("schema"),
                            "failure_categories": (ev.get("failure_evidence") or {}).get(
                                "failure_categories"
                            ),
                            "kind": refl.get("kind") or "failure",
                            "severity": refl.get("severity"),
                            "pattern": refl.get("pattern") or refl.get("reinforcement_pattern"),
                            "root_cause": str(
                                refl.get("root_cause") or refl.get("reinforcement_pattern") or ""
                            )[:120],
                            "evidence_sufficiency": suf,
                            "rewrite_recommended": rec_ok,
                            "preserve_recommended": refl.get("preserve_recommended"),
                        }
                    )
                record["reflection_audit"] = {
                    "n_failing_tasks": len(failing_tasks),
                    "n_failing_traces": len(audit),
                    "n_reflections_used": len(audit),
                    "n_actionable": n_act,
                    "n_low_confidence": n_low,
                    "anomalies": anomalies,
                    "reflections": audit,
                }
            except Exception as exc:
                record["notes"].append(f"{skill}: reflection_audit skipped: {exc!r}")

            # 3. evo rewrite — no --skill-version; rewrite picks the best trace itself.
            rewrite_cmd = [
                evo_bin(),
                "rewrite",
                skill,
                "-m",
                model_id,
                "--api-base",
                api_base,
                "--manifest",
                str(manifest_path),
                "--tasks-dir",
                cli_tasks_dir,
            ]
            if failing_task and (len(failing_tasks) == 1 or session_mode):
                rewrite_cmd.extend(["-t", failing_task])
            if model_substring:
                rewrite_cmd.extend(["--trace-model", model_substring])
            # C1 audit-out contract: durable per-row JSON sidecar carries the
            # structured rewrite outcome.  See plan §1.2 and memory
            # ``feedback_subprocess_audit_out_json``.
            audit_path: Path | None = None
            if rewrite_audit_root is not None and row_id:
                audit_path = rewrite_audit_root / skill / f"{row_id}_rewrite_audit.json"
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                rewrite_cmd.extend(["--rewrite-audit-out", str(audit_path)])
                rewrite_cmd.extend(["--rewrite-row-id", row_id])
            rewrite_mode_env = os.environ.get("EVO_REWRITE_MODE", "text-return")
            rewrite_cmd.extend(["--rewrite-mode", rewrite_mode_env])
            logger.info("evolve: %s", " ".join(rewrite_cmd))
            try:
                proc = _run_evo_cli_with_retries(
                    rewrite_cmd,
                    timeout_s=timeout_rewrite_s,
                    env=env,
                )
                record["rewrite_rc"] = proc.returncode
                record["rewrite_stderr_tail"] = (proc.stderr or "")[-400:]
                record["rewrite_stdout_tail"] = (proc.stdout or "")[-400:]
                # Read the audit JSON regardless of rc — controlled rejections
                # come back as rc=0 with a structured ``candidate_status``;
                # contract violations come back as rc!=0 with the same key set.
                if audit_path is not None:
                    if audit_path.exists():
                        try:
                            audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            audit_payload = {
                                "candidate_status": "rewrite_error_internal",
                                "reason": "audit json unparseable",
                            }
                    else:
                        audit_payload = {
                            "candidate_status": (
                                "rewrite_error_internal"
                                if proc.returncode == 0
                                else "rewrite_error_subprocess"
                            ),
                            "reason": "audit json not written",
                        }
                    for key in REWRITE_AUDIT_RECORD_KEYS:
                        record[key] = audit_payload.get(key)
                if proc.returncode != 0:
                    record["notes"].append(
                        f"{skill}: rewrite rc={proc.returncode}; "
                        f"stderr tail: {(proc.stderr or '')[-300:]}"
                    )
                    continue
                record["notes"].append(
                    f"{skill}: rewrite rc=0; stdout tail: {(proc.stdout or '')[-200:]}"
                )
            except subprocess.TimeoutExpired:
                record["notes"].append(f"{skill}: rewrite TIMEOUT ({timeout_rewrite_s}s)")
                # The CLI's try/finally usually lands the audit JSON before
                # the subprocess wrapper kills it (review fix #4: launcher
                # holds EVO_REWRITE_TIMEOUT_S > EVO_CODEX_TIMEOUT_S).  If the
                # JSON is present, read it; otherwise synthesise a structured
                # ``rewrite_error_codex`` so the row never silently loses
                # candidate_status / gate_reason.
                if audit_path is not None:
                    timeout_audit: dict | None = None
                    if audit_path.exists():
                        try:
                            timeout_audit = json.loads(audit_path.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            timeout_audit = None
                    if timeout_audit is None:
                        timeout_audit = {
                            "rewrite_mode": rewrite_mode_env,
                            "candidate_status": "rewrite_error_codex",
                            "gate_reason": "rewrite_error_codex",
                            "reason": (
                                f"subprocess wrapper timed out after "
                                f"{timeout_rewrite_s}s; CLI did not write audit"
                            ),
                            "parent_preservation": None,
                            "codex_usage": None,
                            "candidate_debug_dir": None,
                        }
                    for key in REWRITE_AUDIT_RECORD_KEYS:
                        record[key] = timeout_audit.get(key)
                continue

            # 4. evo commit. Under the δ-gate (paper §4.2) we commit the
            #    candidate WITHOUT promoting it; PROMOTE/ROLLBACK is decided
            #    after matched parent-vs-candidate validation below.
            gated = promotion_validate is not None
            # Snapshot the highest committed version BEFORE this commit so the
            # candidate is "the version this commit created", not the global
            # max (a prior rejected candidate survives in lineage.json).
            pre_commit_max = _max_committed_version(skill) if gated else None
            commit_msg = f"smoke-4way evolve r1 (model={model_substring}, task={failing_task})"
            commit_cmd = [evo_bin(), "commit", skill, "-m", commit_msg]
            if gated:
                commit_cmd.append("--no-promote")
            logger.info("evolve: %s", " ".join(commit_cmd))
            try:
                proc = run_capture(
                    commit_cmd,
                    cwd=str(PROJECT_ROOT),
                    timeout=timeout_commit_s,
                    env=env,
                )
                record["commit_rc"] = proc.returncode
                if proc.returncode != 0:
                    record["notes"].append(
                        f"{skill}: commit rc={proc.returncode}; "
                        f"stderr tail: {(proc.stderr or '')[-300:]}"
                    )
                    continue
            except subprocess.TimeoutExpired:
                record["notes"].append(f"{skill}: commit TIMEOUT ({timeout_commit_s}s)")
                continue

            if not gated:
                after_version = _current_skill_version(skill)
                record["after_version"] = after_version
                if after_version and after_version != before_version:
                    record["evolve_rounds"] = 1
                    record["notes"].append(
                        f"{skill}: evolved {before_version}→{after_version} on {failing_task}"
                    )
                else:
                    record["notes"].append(
                        f"{skill}: commit ran but version unchanged "
                        f"(before={before_version}, after={after_version})"
                    )
            else:
                from evolution.core.store import SkillStore as _SS
                from evolution.lineage.tracker import LineageTracker as _LT

                def _tracker(_skill: str = skill):
                    return _LT(_SS(project_dir=PROJECT_ROOT).get(_skill))

                candidate_version = _max_committed_version(skill)
                if not candidate_version or candidate_version == pre_commit_max:
                    # commit produced no new version (no body change / leak
                    # keep-parent): honest no-op, parent stays active. Compared
                    # against pre_commit_max so a stale rejected candidate from
                    # an earlier rollback is never re-validated.
                    record["after_version"] = before_version
                    record["gate_status"] = "no_op"
                    record["promotion_gate"] = {
                        "promoted": False,
                        "reason": "no_candidate_version",
                        "parent_version": before_version,
                    }
                    record["notes"].append(
                        f"{skill}: no_op — commit produced no new version (kept {before_version})"
                    )
                elif promotion_validation_count <= 0:
                    # Thin validation pool: the δ-gate cannot run honestly.
                    if os.environ.get("EVO_PROMOTE_WITHOUT_VALIDATION") == "1":
                        _tracker().promote(candidate_version)
                        record["after_version"] = candidate_version
                        record["evolve_rounds"] = 1
                        record["gate_status"] = "promoted"
                        record["promotion_gate"] = {
                            "promoted": True,
                            "no_validation_override": True,
                            "parent_version": before_version,
                            "candidate_version": candidate_version,
                            "n_val_specs": 0,
                        }
                        record["notes"].append(
                            f"{skill}: promoted {candidate_version} WITHOUT validation "
                            f"(EVO_PROMOTE_WITHOUT_VALIDATION=1; thin pool)"
                        )
                    else:
                        _tracker().promote(before_version)
                        _tracker().reject(candidate_version)
                        record["after_version"] = before_version
                        record["gate_status"] = "rolled_back_no_validation"
                        record["promotion_gate"] = {
                            "promoted": False,
                            "reason": "thin_validation_pool",
                            "parent_version": before_version,
                            "candidate_version": candidate_version,
                            "n_val_specs": 0,
                        }
                        record["notes"].append(
                            f"{skill}: rolled_back_no_validation — no disjoint validation "
                            f"task; kept parent {before_version}"
                        )
                else:
                    try:
                        _tracker().promote(before_version)
                        parent_res = promotion_validate(skill)
                        _tracker().promote(candidate_version)
                        cand_res = promotion_validate(skill)
                    except Exception as exc:
                        # Validation crash = mechanism failure (plan §A): restore
                        # parent so the active skill is safe, then fail loudly.
                        try:
                            _tracker().promote(before_version)
                        except Exception:
                            pass
                        record["after_version"] = before_version
                        record["gate_status"] = "error"
                        record["promotion_gate"] = {
                            "promoted": False,
                            "reason": "validation_crash",
                            "parent_version": before_version,
                            "candidate_version": candidate_version,
                            "error": repr(exc)[:300],
                        }
                        record["notes"].append(f"{skill}: promotion-validation crashed: {exc!r}")
                        raise
                    delta = float(os.environ.get("EVO_PROMOTE_DELTA", "0.0") or "0.0")
                    pm = float(parent_res.get("mean", 0.0) or 0.0)
                    cm = float(cand_res.get("mean", 0.0) or 0.0)
                    promote_it = cm > pm + delta
                    if promote_it:
                        _tracker().promote(candidate_version)
                        record["after_version"] = candidate_version
                        record["evolve_rounds"] = 1
                        record["gate_status"] = "promoted"
                    else:
                        _tracker().promote(before_version)
                        _tracker().reject(candidate_version)
                        record["after_version"] = before_version
                        record["gate_status"] = "rolled_back"
                    record["promotion_gate"] = {
                        "promoted": promote_it,
                        "parent_version": before_version,
                        "candidate_version": candidate_version,
                        "parent_mean": pm,
                        "candidate_mean": cm,
                        "delta": delta,
                        "n_val_specs": int(promotion_validation_count),
                        "val_task_ids": sorted(
                            set(parent_res.get("task_ids", [])) | set(cand_res.get("task_ids", []))
                        ),
                        "n_promotion_validation_solves": int(parent_res.get("n", 0))
                        + int(cand_res.get("n", 0)),
                    }
                    record["notes"].append(
                        f"{skill}: δ-gate parent={pm:.4f} candidate={cm:.4f} "
                        f"δ={delta} → {'PROMOTE ' + str(candidate_version) if promote_it else 'ROLLBACK (kept ' + str(before_version) + ')'}"
                    )
        finally:
            try:
                _persist_round_record(skill, record)
            except Exception as _re:
                record["notes"].append(f"{skill}: round_record write skipped: {_re!r}")
            _restore_archived_reflections(archived)
        out[skill] = record
    return out


def _persist_round_record(skill: str, record: dict) -> None:
    """Write one ``round_record_<ts>.json`` distillation per evolve cycle.

    Persisted when the cycle reached the δ-gate (``promotion_gate`` present) or
    when the rewrite was rejected before promotion (``candidate_status`` is a
    non-promoted controlled status). Consumed next round by
    :func:`evolution.evolution.meta_skill.build_meta_skill` (PROMOTE/ROLLBACK
    history) and :func:`build_rejected_edits_context` (prior-round rejections).
    """
    from datetime import datetime

    gate = record.get("promotion_gate")
    cand_status = record.get("candidate_status")
    is_rejection = bool(cand_status) and cand_status != PROMOTED_ELIGIBLE
    if not isinstance(gate, dict) and not is_rejection:
        return
    skill_path = SKILLS_ROOT / skill
    if not skill_path.is_dir():
        return
    audit = (record.get("reflection_audit") or {}).get("reflections") or []
    summary = summarize_reflection_audit(audit)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    row_id = record.get("row_id") or ""
    prior = load_round_records(skill_path)
    round_no = 1 + (sum(1 for r in prior if r.get("row_id") == row_id) if row_id else len(prior))
    payload: dict = {
        "timestamp": ts,
        "skill_name": skill,
        "row_id": row_id,
        "round": round_no,
        "gate_status": record.get("gate_status"),
        "oracle_visibility": record.get("oracle_visibility"),
        **summary,
    }
    if isinstance(gate, dict):
        payload.update(
            {
                "parent_version": gate.get("parent_version"),
                "candidate_version": gate.get("candidate_version"),
                "promoted": bool(gate.get("promoted")),
                "delta": gate.get("delta"),
                "parent_mean": gate.get("parent_mean"),
                "candidate_mean": gate.get("candidate_mean"),
                "reason": gate.get("reason"),
            }
        )
    if is_rejection:
        refl_audit = record.get("reflection_audit") or {}
        payload["rejected"] = build_rejected_entry(
            row_id=row_id,
            candidate_status=cand_status,
            computed_churn=record.get("computed_churn"),
            new_headings=record.get("new_headings"),
            n_actionable=int(refl_audit.get("n_actionable") or 0),
            n_low_confidence=int(refl_audit.get("n_low_confidence") or 0),
        )
    write_round_record(skill_path, payload)


def restore_baseline_versions(versions: dict[str, str]) -> dict[str, dict]:
    """Run `evo promote <skill> <baseline_version>` for each entry.

    Used between primary and secondary model passes so secondary's evolve
    starts from the same active version as primary did, not from a
    primary-evolved version.
    """
    out: dict[str, dict] = {}
    env = os.environ.copy()
    for skill, version in versions.items():
        if not version:
            out[skill] = {"rc": None, "notes": ["no baseline version recorded"]}
            continue
        cmd = [
            evo_bin(),
            "promote",
            skill,
            version,
        ]
        try:
            proc = run_capture(
                cmd,
                cwd=str(PROJECT_ROOT),
                timeout=60,
                env=env,
            )
            out[skill] = {
                "rc": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-200:],
                "stderr_tail": (proc.stderr or "")[-200:],
            }
        except subprocess.TimeoutExpired:
            out[skill] = {"rc": -1, "notes": ["promote TIMEOUT"]}
    return out


def _read_task_toml_name(tasks_dir: Path | None, task_id: str) -> str | None:
    """Return the ``name`` field of ``<tasks_dir>/<task_id>/task.toml``.

    Used by ``produce_with_trace_spec`` to translate a directory-id-style
    task_id into the human-readable name that the trace store keys by.
    """
    if tasks_dir is None:
        return None
    p = Path(tasks_dir) / task_id / "task.toml"
    if not p.is_file():
        return None
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("["):
                break
            m = re.match(r"name\s*=\s*(.+)", line)
            if m:
                v = m.group(1).strip().strip('"').strip("'")
                if v:
                    return v
    except Exception:
        return None
    return None


def _build_litellm_backend(
    *,
    model_id: str,
    api_base: str,
    api_key: str,
    use_qwen_gate: bool,
    temperature: float | None = None,
    frequency_penalty: float | None = None,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
):
    """Build a LiteLLM backend for the given (model_id, api_base).

    Local sglang routes through the global ``qwen_gate`` (3 permits at
    localhost:30004); cloud calls bypass the gate. ``use_qwen_gate`` is
    decided by the caller based on ``api_base``.

    Sampling kwargs are forwarded into the backend on every call.
    """
    from evolution.llm.litellm_backend import LiteLLMBackend

    extra: dict = {}
    if temperature is not None:
        extra["temperature"] = float(temperature)
    if frequency_penalty is not None:
        extra["frequency_penalty"] = float(frequency_penalty)
    if max_tokens is not None:
        extra["max_tokens"] = int(max_tokens)
    if extra_body:
        extra["extra_body"] = dict(extra_body)
    backend = LiteLLMBackend(model=model_id, api_base=api_base, api_key=api_key or "EMPTY", **extra)
    backend._use_qwen_gate = use_qwen_gate  # type: ignore[attr-defined]
    # Real api_base for the keyed cross-process endpoint gate, so produce
    # and eval share ONE per-endpoint budget (not a default-base singleton).
    backend._gate_api_base = api_base  # type: ignore[attr-defined]
    return backend


def produce_with_trace_spec(
    *,
    pilot,
    trace_spec: list,
    trace_budget: int,
    seed: int,
    task_diversity: str,
    model_dispatch: dict[str, dict],
    reflector_model_id: str,
    reflector_api_base: str,
    reflector_api_key: str,
    manifest_path: Path,
    evolution_tasks_dir: Path,
    work_root: Path,
    reflector_temperature: float | None = None,
    reflector_max_tokens: int | None = None,
    reflector_extra_body: dict | None = None,
    timeout_per_solve_s: float = 300.0,
    do_evolve: bool = True,
    allowed_skill_names: list[str] | tuple[str, ...] | set[str] | None = None,
    promotion_validation_spec: list | None = None,
    rewrite_audit_root: Path | None = None,
    row_id: str = "",
) -> dict:
    """N-solves driven by a TraceSpec list, then one evolve round per skill.

    Args:
      pilot: §5.1 ``_MinPilot`` (has .train list of tasks).
      trace_spec: list of ``TraceSpec(task_id, source_model_id, attempt_seed)``;
        length MUST equal ``trace_budget``.
      trace_budget: paper-mandated N (default 24 for §5.2). Refused if
        len(trace_spec) != trace_budget.
      seed: top-level seed (recorded for replay; per-spec seeds carry
        their own attempt_seed).
      task_diversity: ``"T1" | "T2" | "T3"``; recorded in plan only.
      model_dispatch: ``{model_id: {api_base, api_key, served_id_hint?,
        extra_body?}}``. Held-out model id MUST NOT appear here; the
        selector enforces this earlier.
      reflector_model_id: the ``-m`` arg to ``evo reflect`` / ``evo rewrite``.
      reflector_* sampling fields: passed through the subprocess boundary via
        ``EVO_LLM_*`` environment variables so CLI-backed reflect/rewrite calls
        use the same manifest knobs as in-process solver calls.
      manifest_path: split manifest passed to ``evo reflect``.
      evolution_tasks_dir: tasks_evo/ tree.
      work_root: per-row workspace root (``work_root/evolution_workspaces/<spec_idx>``).
      do_evolve: if False, only the solve traces are produced (skip evolve);
        useful for debugging.
      allowed_skill_names: optional explicit skill allow-list for anchor-only
        runs. Non-allowed auxiliary skills are removed from the plan before
        solving, evolving, and persisting state.

    Returns:
      a JSON-able plan dict with:
        ``per_task_skills``, ``preloaded_skill_names``,
        ``newly_produced_skill_names``, ``notes``, ``evolve_records``,
        ``baseline_versions``, ``train_solve_summary`` (per-spec entry),
        ``n_traces_used`` (= len(train_solve_summary)),
        ``trace_budget`` (= input N),
        ``task_diversity`` (= input),
        ``source_model_ids`` (deduped list, sorted),
        ``runtime_seconds``.
    """
    import shutil
    import time
    from concurrent.futures import ThreadPoolExecutor

    from evolution.core.store import SkillStore
    from evolution.eval.solver import solve_task
    from evolution.eval.task import load_task
    from evolution.eval.workspace import setup_workspace

    if len(trace_spec) != trace_budget:
        raise AssertionError(
            f"trace_budget invariant: spec has {len(trace_spec)} entries, "
            f"trace_budget={trace_budget}. Refusing to run — varying N "
            "confounds task-diversity (paper §4.2 holds N fixed)."
        )

    split_manifest = SplitManifest.load(manifest_path)
    started = time.time()

    plan = produce_baseline(pilot)
    _restrict_plan_to_allowed_skills(plan, allowed_skill_names)

    # Snapshot baseline versions so we can restore later if needed.
    all_skills = sorted({s for skills in plan.per_task_skills.values() for s in skills})
    baseline_versions: dict[str, str] = {}
    for s in all_skills:
        v = _current_skill_version(s)
        if v:
            baseline_versions[s] = v

    # _shared lives outside the evolution.* package — inject sys.path.
    import sys

    experiments_dir = REPO_ROOT / "experiments"
    if str(experiments_dir) not in sys.path:
        sys.path.insert(0, str(experiments_dir))
    from _shared.local_endpoint_gate import gate_for, is_local_endpoint

    backends: dict[str, object] = {}
    for mid, cfg in model_dispatch.items():
        # LiteLLM prefix-dispatches; add openrouter/ when the api_base says so.
        api_base = cfg["api_base"]
        litellm_model = mid
        if "openrouter.ai" in api_base and not mid.startswith("openrouter/"):
            litellm_model = f"openrouter/{mid}"
        backends[mid] = _build_litellm_backend(
            model_id=litellm_model,
            api_base=api_base,
            api_key=cfg.get("api_key", ""),
            use_qwen_gate=is_local_endpoint(api_base),
            temperature=cfg.get("temperature"),
            frequency_penalty=cfg.get("frequency_penalty"),
            max_tokens=cfg.get("max_output_tokens") or cfg.get("max_tokens"),
            extra_body=cfg.get("extra_body") or None,
        )

    # Resolve the SkillStore once; reuse across solves. Mutable skill state
    # is keyed on the per-worker PROJECT_ROOT, not the code checkout.
    store = SkillStore(project_dir=PROJECT_ROOT)
    skills_by_name: dict[str, object] = {}
    for skill_names in plan.per_task_skills.values():
        for name in skill_names:
            if name not in skills_by_name:
                try:
                    skills_by_name[name] = store.get(name)
                except KeyError:
                    plan.notes.append(
                        f"evolution: skill {name!r} resolved at produce_baseline but absent at "
                        "solve time — skipping"
                    )

    train_index = {t.id: t for t in pilot.train}

    @dataclass
    class _SpecResult:
        spec_idx: int
        task_id: str
        source_model_id: str
        attempt_seed: int
        success: bool
        error: str
        pass_rate: float
        total_tests: int
        skills_used: list
        code_extraction_failed: bool = False
        code_extraction_retried: bool = False
        llm_transport_failed: bool = False

    def run_one(idx: int, spec) -> _SpecResult:
        tid = spec.task_id
        mid = spec.source_model_id
        t = train_index.get(tid)
        if t is None:
            return _SpecResult(
                idx,
                tid,
                mid,
                spec.attempt_seed,
                False,
                f"task {tid!r} not in pilot.train",
                0.0,
                0,
                [],
            )
        skill_names = plan.per_task_skills.get(tid, [])
        skills = [skills_by_name[n] for n in skill_names if n in skills_by_name]

        task_dir = evolution_tasks_dir / tid
        if not task_dir.is_dir():
            return _SpecResult(
                idx,
                tid,
                mid,
                spec.attempt_seed,
                False,
                f"task dir missing: {task_dir}",
                0.0,
                0,
                list(skill_names),
            )
        try:
            task = load_task(task_dir)
        except Exception as exc:
            return _SpecResult(
                idx,
                tid,
                mid,
                spec.attempt_seed,
                False,
                f"load_task: {exc!r}",
                0.0,
                0,
                list(skill_names),
            )

        try:
            ws = work_root / "evolution_workspaces" / f"spec_{idx:03d}"
            if ws.exists():
                shutil.rmtree(ws)
            ws.mkdir(parents=True, exist_ok=True)
            setup_workspace(task, ws)
            backend = backends[mid]
        except Exception as exc:
            return _SpecResult(
                idx,
                tid,
                mid,
                spec.attempt_seed,
                False,
                f"workspace/backend setup: {exc!r}",
                0.0,
                0,
                list(skill_names),
            )

        is_local = bool(getattr(backend, "_use_qwen_gate", False))
        try:
            if is_local:
                gate_base = getattr(backend, "_gate_api_base", None)
                with gate_for(gate_base).permit():
                    sr = solve_task(
                        task=task,
                        backend=backend,
                        workspace=ws,
                        skills=skills if skills else None,
                        timeout=timeout_per_solve_s,
                        evaluate=True,
                        model_name=mid,
                        manifest=split_manifest,
                        manifest_scope="skill_evolution",
                        attempt_seed=spec.attempt_seed,
                        trace_spec_idx=idx,
                        round_id=os.environ.get("EVO_ROUND_ID"),
                    )
            else:
                sr = solve_task(
                    task=task,
                    backend=backend,
                    workspace=ws,
                    skills=skills if skills else None,
                    timeout=timeout_per_solve_s,
                    evaluate=True,
                    model_name=mid,
                    manifest=split_manifest,
                    manifest_scope="skill_evolution",
                    attempt_seed=spec.attempt_seed,
                    trace_spec_idx=idx,
                    round_id=os.environ.get("EVO_ROUND_ID"),
                )
        except Exception as exc:
            return _SpecResult(
                idx,
                tid,
                mid,
                spec.attempt_seed,
                False,
                f"solve_task: {exc!r}",
                0.0,
                0,
                list(skill_names),
            )

        er = sr.eval_result
        if er is None:
            return _SpecResult(
                spec_idx=idx,
                task_id=tid,
                source_model_id=mid,
                attempt_seed=spec.attempt_seed,
                success=False,
                error=("; ".join(sr.errors) or "no eval_result")[:300],
                pass_rate=0.0,
                total_tests=0,
                skills_used=list(skill_names),
                code_extraction_failed=bool(sr.code_extraction_failed),
                code_extraction_retried=bool(sr.code_extraction_retried),
                llm_transport_failed=bool(getattr(sr, "llm_transport_failed", False)),
            )
        return _SpecResult(
            spec_idx=idx,
            task_id=tid,
            source_model_id=mid,
            attempt_seed=spec.attempt_seed,
            success=er.success,
            error="; ".join(er.errors)[:300] if er.errors and not er.success else "",
            pass_rate=er.pass_rate,
            total_tests=er.total_tests,
            skills_used=list(skill_names),
            code_extraction_failed=bool(sr.code_extraction_failed),
            code_extraction_retried=bool(sr.code_extraction_retried),
            llm_transport_failed=bool(getattr(sr, "llm_transport_failed", False)),
        )

    def _run_promotion_validation(skill_name: str) -> dict:
        # Solve the CURRENTLY active skill version on the row's held-back
        # validation specs (paper §4.2 PROMOTE/ROLLBACK). The caller promotes
        # parent / candidate before each call; skills are re-resolved from a
        # fresh store so the active version's body is what runs.
        pv = list(promotion_validation_spec or [])
        if not pv:
            return {"mean": 0.0, "n": 0, "task_ids": []}
        from evolution.core.store import SkillStore as _PVStore

        val_ids = sorted({str(sp["task_id"]) for sp in pv})
        split_manifest.assert_no_leakage(
            val_ids, scope="skill_evolution", context="promotion_validation"
        )
        fresh = _PVStore(project_dir=PROJECT_ROOT)
        # Unique per CALL: this fn runs once for the parent pass and once for
        # the candidate pass (same j indices). A shared v_{j} path made the
        # two passes' rmtree/mkdir race → OSError(39) killed whole anchors.
        import uuid as _uuid

        call_token = _uuid.uuid4().hex[:8]

        def _one(j: int, sp: dict) -> float:
            # Wiring / mechanism failures (missing task, load failure, missing
            # backend, solve crash) are NOT honest 0-scores — they corrupt the
            # matched parent-vs-candidate comparison. Raise so the caller
            # restores the parent and fails the row loudly (plan §A). Only an
            # honest skill failure (solver returns, no eval_result / low pass
            # rate) scores 0.0.
            tid = str(sp["task_id"])
            mid = str(sp["source_model_id"])
            a_seed = int(sp.get("attempt_seed", 0) or 0)
            task_dir = evolution_tasks_dir / tid
            if not task_dir.is_dir():
                raise RuntimeError(f"promotion-validation task dir missing: {task_dir}")
            task = load_task(task_dir)
            sk_objs = []
            for n in plan.per_task_skills.get(tid, []):
                try:
                    sk_objs.append(fresh.get(n))
                except KeyError:
                    pass
            ws = work_root / "promotion_validation" / skill_name / call_token / f"v_{j:03d}"
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
            ws.mkdir(parents=True, exist_ok=True)
            setup_workspace(task, ws)
            backend = backends.get(mid)
            if backend is None:
                raise RuntimeError(f"promotion-validation backend missing for source model {mid!r}")
            is_local = bool(getattr(backend, "_use_qwen_gate", False))
            kw = dict(
                task=task,
                backend=backend,
                workspace=ws,
                skills=sk_objs if sk_objs else None,
                timeout=timeout_per_solve_s,
                evaluate=True,
                model_name=mid,
                manifest=split_manifest,
                manifest_scope="skill_evolution",
                attempt_seed=a_seed,
                trace_spec_idx=a_seed,
                round_id=os.environ.get("EVO_ROUND_ID"),
                phase="promotion_validation",
            )
            try:
                if is_local:
                    with gate_for(getattr(backend, "_gate_api_base", None)).permit():
                        sr = solve_task(**kw)
                else:
                    sr = solve_task(**kw)
            except Exception as exc:
                raise RuntimeError(
                    f"promotion-validation solve crashed (task={tid}, model={mid}): {exc!r}"
                ) from exc
            if getattr(sr, "llm_transport_failed", False):
                raise RuntimeError(
                    f"promotion-validation LLM transport failure (task={tid}, model={mid}) — "
                    "stub solve would corrupt the matched parent-vs-candidate δ-gate"
                )
            er = sr.eval_result
            return float(er.pass_rate) if er is not None else 0.0

        pv_workers = int(
            min(max(1, int(os.environ.get("EVOLUTION_TRACE_WORKERS", "8") or "8")), len(pv))
        )
        rates: list[float] = []
        pv_cap = float(timeout_per_solve_s) * 4 + 300
        pool = ThreadPoolExecutor(max_workers=pv_workers)
        try:
            for f in [pool.submit(_one, j, sp) for j, sp in enumerate(pv)]:
                try:
                    rates.append(f.result(timeout=pv_cap))
                except TimeoutError:
                    # A wedged validation solve is a mechanism failure, never an
                    # honest 0.0 averaged into the matched parent/candidate mean.
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError(
                        f"promotion-validation wedged > {pv_cap:.0f}s "
                        f"(skill={skill_name}); δ-gate cannot score honestly"
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return {
            "mean": (sum(rates) / len(rates)) if rates else 0.0,
            "n": len(rates),
            "task_ids": val_ids,
        }

    # Solver concurrency cap; local Qwen gate enforces its own 3-permit limit.
    max_workers = int(os.environ.get("EVOLUTION_TRACE_WORKERS", "8") or "8")
    n_workers = int(min(max(1, max_workers), len(trace_spec)))
    spec_results: list[_SpecResult] = []
    fut_cap = float(timeout_per_solve_s) * 4 + 300
    # f.result(timeout) only REPORTS a wedge (a thread can't be cancelled); the
    # real hard bound is the per-anchor process kill in run_publication._run.
    pool = ThreadPoolExecutor(max_workers=n_workers)
    try:
        futs = {pool.submit(run_one, i, s): (i, s) for i, s in enumerate(trace_spec)}
        for f, (i, s) in futs.items():
            try:
                spec_results.append(f.result(timeout=fut_cap))
            except TimeoutError:
                spec_results.append(
                    _SpecResult(
                        spec_idx=i,
                        task_id=s.task_id,
                        source_model_id=s.source_model_id,
                        attempt_seed=s.attempt_seed,
                        success=False,
                        error=f"produce solve wedged > {fut_cap:.0f}s (deadlock guard)",
                        pass_rate=0.0,
                        total_tests=0,
                        skills_used=[],
                        llm_transport_failed=True,
                    )
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Maintain spec order in the output.
    spec_results.sort(key=lambda r: r.spec_idx)

    train_solve_summary = [
        {
            "spec_idx": r.spec_idx,
            "task_id": r.task_id,
            "source_model_id": r.source_model_id,
            "attempt_seed": r.attempt_seed,
            "success": r.success,
            "pass_rate": r.pass_rate,
            "total_tests": r.total_tests,
            "skills_used": r.skills_used,
            "error_tail": (r.error or "")[:200],
            "code_extraction_failed": r.code_extraction_failed,
            "code_extraction_retried": r.code_extraction_retried,
            "llm_transport_failed": r.llm_transport_failed,
        }
        for r in spec_results
    ]
    if spec_results and all(r.llm_transport_failed for r in spec_results):
        raise RuntimeError(
            "all produce solves hit an LLM transport failure (e.g. proxy leak / "
            "endpoint down) — every trace is a stub; refusing to score this row. "
            f"task_diversity={task_diversity} "
            f"models={sorted({r.source_model_id for r in spec_results})}"
        )
    n_traces_used = len(train_solve_summary)
    plan.notes.append(
        f"evolution: ran {n_traces_used} produce solves "
        f"(task_diversity={task_diversity}, models={sorted({r.source_model_id for r in spec_results})})"
    )

    # One reflect→rewrite→commit per skill per row (not per cell — avoids
    # double-rewriting the same anchor inside one round).
    # evolve_status ∈ {"evolved", "no_failure_signal", "no_op", "error"};
    # only "error" aborts the row.
    evolve_records: dict[str, dict] = {}
    evolve_summary = {
        "evolved": 0,
        "promoted": 0,
        "rolled_back": 0,
        "rolled_back_no_validation": 0,
        "no_failure_signal": 0,
        "no_op": 0,
        "error": 0,
    }
    evolve_error_reason: str = ""
    if do_evolve:
        row_skills = sorted({s for skills in plan.per_task_skills.values() for s in skills})
        if not row_skills:
            plan.notes.append("evolution: row has no skills to evolve; skipping.")
        else:
            synth = []
            for r in spec_results:
                if r.llm_transport_failed:
                    continue
                synth.append(
                    type(
                        "R",
                        (),
                        {
                            "task_id": r.task_id,
                            "success": r.success,
                            "error": r.error,
                            "pass_rate": r.pass_rate,
                            "total_tests": r.total_tests,
                            "source_model_id": r.source_model_id,
                            "skills_used": r.skills_used,
                        },
                    )()
                )

            skill_to_failing: dict[str, list[str]] = {}
            skill_to_trace_models: dict[str, set[str]] = {}
            skill_to_contributing_cells: dict[str, set[str]] = {}
            for skill in row_skills:
                selected = _select_all_failing_traces(synth, skill)
                picked = [task_id for task_id, _ in selected]
                skill_to_trace_models[skill] = {mid for _t, mid in selected if mid is not None}
                skill_to_contributing_cells[skill] = {
                    train_index[task_id].cell_id
                    for task_id, _mid in selected
                    if task_id in train_index and train_index[task_id].cell_id
                }
                # The downstream `evo reflect --task <X>` filter matches
                # against ``result.json["task"]``, which is the human-
                # readable ``task.name`` from task.toml — NOT the task
                # directory id. Translate so reflect actually finds the
                # trace.
                mapped: list[str] = []
                for picked_task in picked:
                    picked_name = _read_task_toml_name(evolution_tasks_dir, picked_task)
                    mapped.append(picked_name or picked_task)
                skill_to_failing[skill] = mapped

            distinct_source_models: set[str] = set()
            for models in skill_to_trace_models.values():
                distinct_source_models |= models
            if len(distinct_source_models) == 1:
                trace_model_id = next(iter(distinct_source_models))
            else:
                # Cross-source rows cannot safely use a scalar
                # ``--trace-model`` filter. Omitting it reflects all selected
                # failing tasks while manifest + per-round trace isolation
                # keep the scope to this row's train traces.
                trace_model_id = None

            if all(not v for v in skill_to_failing.values()):
                rec_for_row: dict = {}
                for skill in row_skills:
                    rec_for_row[skill] = {
                        "evolve_rounds": 0,
                        "before_version": _current_skill_version(skill),
                        "after_version": _current_skill_version(skill),
                        "failing_task": None,
                        "failing_tasks": [],
                        "trace_ids": [],
                        "reflect_rc": None,
                        "rewrite_rc": None,
                        "commit_rc": None,
                        "selected_source_models": [],
                        "source_model_filter": "",
                        "contributing_cells": [],
                        "evolve_scope": "aggregate",
                        "notes": [
                            f"{skill}: no_failure_signal — no verifier-covered "
                            "failing trace matched this skill."
                        ],
                        "evolve_status": "no_failure_signal",
                    }
                    evolve_summary["no_failure_signal"] += 1
                evolve_records["aggregate"] = rec_for_row
                plan.notes.append("evolution: aggregate row all skills no_failure_signal")
            else:
                # Some skill has a failing trace; exceptions mark the row as
                # error and let the orchestrator abort.
                #
                # LiteLLM dispatches by model-id prefix; align prefix to api_base.
                provider_prefixes = (
                    "openrouter/",
                    "openai/",
                    "anthropic/",
                    "gemini/",
                    "google/",
                    "together_ai/",
                    "vertex_ai/",
                    "codex/",
                    "claude-code/",
                    "hermes/",
                )
                if "openrouter.ai" in (reflector_api_base or ""):
                    if reflector_model_id.startswith("openrouter/"):
                        refl_id = reflector_model_id
                    else:
                        refl_id = f"openrouter/{reflector_model_id}"
                elif reflector_model_id.startswith(provider_prefixes):
                    refl_id = reflector_model_id
                else:
                    refl_id = f"openrouter/{reflector_model_id}"
                try:
                    rec = evolve_skills_one_round(
                        skill_to_failing_tasks=skill_to_failing,
                        model_id=refl_id,
                        api_base=reflector_api_base,
                        api_key=reflector_api_key,
                        temperature=reflector_temperature,
                        max_tokens=reflector_max_tokens,
                        extra_body=reflector_extra_body,
                        manifest_path=manifest_path,
                        trace_model_id=trace_model_id,
                        evolution_tasks_dir=evolution_tasks_dir,
                        promotion_validate=(
                            _run_promotion_validation
                            if promotion_validation_spec is not None
                            else None
                        ),
                        promotion_validation_count=len(promotion_validation_spec or []),
                        rewrite_audit_root=rewrite_audit_root,
                        row_id=row_id,
                    )
                except Exception as exc:
                    evolve_error_reason = f"evolve_skills_one_round raised on aggregate: {exc!r}"
                    evolve_summary["error"] += 1
                    evolve_records["aggregate"] = {
                        skill: {
                            "evolve_rounds": 0,
                            "before_version": _current_skill_version(skill),
                            "after_version": _current_skill_version(skill),
                            "failing_task": (skill_to_failing.get(skill) or [None])[0],
                            "failing_tasks": skill_to_failing.get(skill) or [],
                            "trace_ids": [],
                            "reflect_rc": None,
                            "rewrite_rc": None,
                            "commit_rc": None,
                            "selected_source_models": sorted(
                                skill_to_trace_models.get(skill) or []
                            ),
                            "source_model_filter": (
                                _model_substring(trace_model_id) if trace_model_id else ""
                            ),
                            "contributing_cells": sorted(
                                skill_to_contributing_cells.get(skill) or []
                            ),
                            "evolve_scope": "aggregate",
                            "notes": [f"{skill}: exception in evolve: {exc!r}"],
                            "evolve_status": "error",
                        }
                        for skill in row_skills
                    }
                    raise

                for skill, r in rec.items():
                    reflect_rc = r.get("reflect_rc")
                    rewrite_rc = r.get("rewrite_rc")
                    commit_rc = r.get("commit_rc")
                    before_v = r.get("before_version")
                    after_v = r.get("after_version")
                    bumped = before_v != after_v and after_v is not None
                    r["selected_source_models"] = sorted(skill_to_trace_models.get(skill) or [])
                    r["source_model_filter"] = (
                        _model_substring(trace_model_id) if trace_model_id else ""
                    )
                    r["contributing_cells"] = sorted(skill_to_contributing_cells.get(skill) or [])
                    r["evolve_scope"] = "aggregate"
                    gate_status = r.get("gate_status")
                    if not (skill_to_failing.get(skill) or []):
                        status = "no_failure_signal"
                        evolve_summary["no_failure_signal"] += 1
                    elif gate_status:
                        # δ-gate path: evolve_skills_one_round already decided
                        # PROMOTE/ROLLBACK; its status is authoritative.
                        status = gate_status
                        evolve_summary[status] = evolve_summary.get(status, 0) + 1
                        if status == "promoted":
                            if skill not in plan.newly_produced_skill_names:
                                plan.newly_produced_skill_names.append(skill)
                        elif status == "error":
                            evolve_error_reason = (
                                f"δ-gate error on aggregate skill={skill}: "
                                f"{(r.get('promotion_gate') or {}).get('error')}"
                            )
                        plan.notes.append(
                            f"evolution: aggregate skill={skill} gate={status} "
                            f"{(r.get('promotion_gate') or {})}"
                        )
                    elif reflect_rc == 0 and rewrite_rc == 0 and commit_rc == 0 and bumped:
                        status = "evolved"
                        evolve_summary["evolved"] += 1
                        if skill not in plan.newly_produced_skill_names:
                            plan.newly_produced_skill_names.append(skill)
                        plan.notes.append(
                            f"evolution: aggregate skill={skill} "
                            f"{before_v}→{after_v} on {r.get('failing_task')}"
                        )
                    elif reflect_rc == 0 and rewrite_rc == 0 and commit_rc == 0:
                        # Clean reflect→rewrite→commit but the version did
                        # NOT advance. This is a legitimate honest no-op
                        # round (the leak scanner rejected the candidate and
                        # kept the parent, or the rewrite produced no usable
                        # change), NOT an error — genuine errors have
                        # non-zero rcs or take the exception path above. The
                        # skill stays at a valid prior version and the seed
                        # chain continues from it; two consecutive no-ops
                        # gracefully stop the anchor's evolution loop.
                        status = "no_op"
                        evolve_summary["no_op"] += 1
                        plan.notes.append(
                            f"evolution: aggregate skill={skill} no_op "
                            f"(kept {before_v}; rewrite produced no usable "
                            f"improvement) on {r.get('failing_task')}"
                        )
                    else:
                        status = "error"
                        evolve_summary["error"] += 1
                        evolve_error_reason = (
                            f"non-clean rcs on aggregate skill={skill}: "
                            f"reflect_rc={reflect_rc} rewrite_rc={rewrite_rc} "
                            f"commit_rc={commit_rc} bumped={bumped}"
                        )
                    r["evolve_status"] = status
                evolve_records["aggregate"] = rec

    n_pv_solves = 0
    for agg in evolve_records.values():
        if isinstance(agg, dict):
            for rr in agg.values():
                if isinstance(rr, dict):
                    n_pv_solves += int(
                        (rr.get("promotion_gate") or {}).get("n_promotion_validation_solves", 0)
                        or 0
                    )

    return {
        "framework": "evolution",
        "per_task_skills": dict(plan.per_task_skills),
        "preloaded_skill_names": list(plan.preloaded_skill_names),
        "newly_produced_skill_names": list(plan.newly_produced_skill_names),
        "all_produced_skill_names": list(plan.preloaded_skill_names),
        "notes": list(plan.notes),
        "skipped": False,
        "skip_reason": "",
        "evolve_records": evolve_records,
        "evolve_summary": evolve_summary,
        "evolve_error_reason": evolve_error_reason,
        "baseline_versions": baseline_versions,
        "train_solve_summary": train_solve_summary,
        "n_traces_used": n_traces_used,
        "n_source_traces_used_for_diagnose": n_traces_used,
        "n_promotion_validation_solves": n_pv_solves,
        "promotion_validation_budget": len(promotion_validation_spec or []),
        "trace_budget": trace_budget,
        "seed": seed,
        "task_diversity": task_diversity,
        "source_model_ids": sorted({r.source_model_id for r in spec_results}),
        "runtime_seconds": round(time.time() - started, 1),
    }


__all__ = [
    "FRAMEWORK_TAG",
    "ProducePlan",
    "produce_baseline",
    "produce_with_trace_spec",
    "test_time_skills",
    "evolve_skills_one_round",
    "restore_baseline_versions",
]
