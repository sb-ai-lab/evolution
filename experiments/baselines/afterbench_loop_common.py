"""Shared utilities for the AFTER iterative loop harness (loop-v1)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "loop-v1"
DIGEST_CAP = int(os.environ.get("EVO_TRACE_DIGEST_CAP", "200000"))
TRACEBACK_TAIL = int(os.environ.get("EVO_TRACEBACK_TAIL", "12000"))
CODE_HEAD = int(os.environ.get("EVO_TRACE_CODE_HEAD", "12000"))
INSTRUCTION_HEAD = int(os.environ.get("EVO_TRACE_INSTRUCTION_HEAD", "4000"))
STDOUT_TAIL = int(os.environ.get("EVO_TRACE_STDOUT_TAIL", "4000"))
ARTIFACT_PREVIEW_CHARS = int(os.environ.get("EVO_TRACE_ARTIFACT_PREVIEW_CHARS", "8000"))
SHARED_EDITOR_PROMPT_VERSION = "shared-train-evidence-v1"
SHARED_EDITOR_POLICY_TEMPLATE = """\
You are updating reusable procedural memory for anchor skill "{skill_id}".

Use only train-split evidence in this packet. Held-out/test tasks are
unavailable. The goal is better reusable procedures for similar future tasks,
not memorized benchmark answers.

Keep the framework's native memory format and runtime contract. Prefer durable
rules, data extraction patterns, validation checks, and failure-prevention steps.
Do not copy pytest source, hidden expected answers, or benchmark-only paths. If
train evidence contains concrete filenames or schemas, include them only when
they describe the reusable task-family contract.
"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8", errors="replace"))


def sha256_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root))
            try:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                out[rel] = "unreadable"
    return out


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def clip_middle_text(text: str, limit: int, *, label: str = "text") -> tuple[str, bool]:
    """Bound prompt text while preserving both the beginning and the end."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = f"\n\n[... {label} clipped: {len(text) - limit} chars omitted from middle ...]\n\n"
    if len(marker) >= limit:
        return text[:limit], True
    head = max(1, (limit - len(marker)) // 2)
    tail = max(1, limit - len(marker) - head)
    return text[:head] + marker + text[-tail:], True


def text_artifact_meta(path: Path, *, preview_chars: int = ARTIFACT_PREVIEW_CHARS) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    preview, truncated = clip_middle_text(text, preview_chars, label=path.name)
    return {
        "path": str(path),
        "chars": len(text),
        "sha256": sha256_text(text),
        "preview": preview,
        "preview_truncated": truncated,
    }


def write_text_artifact(path: Path, text: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text_artifact_meta(path)


def bundle_from_solve_result(
    *,
    task_key: str,
    instruction: str,
    role: str,
    cache_dir: Path,
    solve_result: dict[str, Any],
) -> dict[str, Any]:
    solve_py = cache_dir / "solve.py"
    solve_log = cache_dir / "solve_run.log"
    pytest_full = cache_dir / "pytest_full.log"
    if not pytest_full.is_file():
        pytest_full = cache_dir / "pytest.log"
    code = solve_py.read_text(encoding="utf-8", errors="replace") if solve_py.is_file() else ""
    stdout_meta = text_artifact_meta(solve_log) if solve_log.is_file() else {}
    stdout = str(stdout_meta.get("preview") or "")
    pytest_text = (
        pytest_full.read_text(encoding="utf-8", errors="replace") if pytest_full.is_file() else ""
    )
    passed = int(solve_result.get("passed", 0) or 0)
    total = int(solve_result.get("total_tests", 0) or 0)
    pr = float(solve_result.get("pass_rate", 0.0) or 0.0)
    failed_names = re.findall(r"FAILED\s+(\S+)", pytest_text)
    return {
        "task_id": task_key,
        "instruction": instruction,
        "code": code,
        "stdout": stdout,
        "stdout_artifact": stdout_meta,
        "pytest_full": pytest_text,
        "passed": passed,
        "total": total,
        "pass_rate": pr,
        "failure_categories": failed_names[:20],
        "workspace_file_previews": [],
        "produced_artifacts": [],
        "role": role,
    }


def bundle_from_episode(
    *,
    task_key: str,
    instruction: str,
    role: str,
    trajectory: dict[str, Any],
    pytest_result: dict[str, Any],
    cache_dir: Path,
) -> dict[str, Any]:
    solve_py = cache_dir / "workspace" / "_solve.py"
    code = solve_py.read_text(encoding="utf-8", errors="replace") if solve_py.is_file() else ""
    pytest_text = str(pytest_result.get("log_full") or pytest_result.get("log_tail") or "")
    passed = int(pytest_result.get("passed", 0) or 0)
    total = int(pytest_result.get("total_tests", 0) or 0)
    pr = float(pytest_result.get("pass_rate", 0.0) or 0.0)
    failed_names = re.findall(r"FAILED\s+(\S+)", pytest_text)
    transcript_path = cache_dir / "transcript.txt"
    stdout_meta = text_artifact_meta(transcript_path) if transcript_path.is_file() else {}
    stdout = str(stdout_meta.get("preview") or "")
    steps = trajectory.get("steps") if isinstance(trajectory, dict) else []
    try:
        trajectory_full = json.dumps(steps, default=str) if steps else ""
    except Exception:
        trajectory_full = str(steps)
    trajectory_preview, trajectory_truncated = clip_middle_text(
        trajectory_full,
        ARTIFACT_PREVIEW_CHARS,
        label="trajectory",
    )
    return {
        "task_id": task_key,
        "instruction": instruction,
        "code": code,
        "stdout": stdout,
        "stdout_artifact": stdout_meta,
        "trajectory_preview": trajectory_preview,
        "trajectory_chars": len(trajectory_full),
        "trajectory_preview_truncated": trajectory_truncated,
        "pytest_full": pytest_text,
        "passed": passed,
        "total": total,
        "pass_rate": pr,
        "final_reward": trajectory.get("final_reward") if isinstance(trajectory, dict) else None,
        "failure_categories": failed_names[:20],
        "workspace_file_previews": [],
        "produced_artifacts": [],
        "role": role,
    }


def _traceback_preview(text: str, limit: int) -> tuple[str, bool]:
    preview, truncated = clip_middle_text(text, limit, label="pytest output")
    return preview, not truncated


def build_digest_from_bundles(bundles: list[dict]) -> tuple[str, dict[str, dict]]:
    """Build an editor digest with both failure traces and successful exemplars.

    Earlier versions donated all passed-task budget to failures. That made the
    digest good at patching obvious failures but weak at preserving successful
    patterns: a passed task was represented by a one-line summary and no solver
    code. Hermes needs those successful programs too, otherwise edits can fix
    one failure mode while erasing behavior that already worked.
    """
    n = max(1, len(bundles))
    base_budget = max(1, DIGEST_CAP // n)
    failing = [
        b for b in bundles if int(b.get("total", 0) or 0) > 0 and float(b.get("pass_rate", 0)) < 1.0
    ]
    passing = [b for b in bundles if b not in failing]
    fail_budget = int(base_budget * 1.35) if failing and passing else base_budget
    pass_budget = max(int(base_budget * 0.65), min(base_budget, CODE_HEAD // 2))
    parts: list[str] = []
    audit: dict[str, dict] = {}

    def _task_context(b: dict) -> list[str]:
        instr, instr_truncated = clip_middle_text(
            str(b.get("instruction") or ""),
            INSTRUCTION_HEAD,
            label="task instruction",
        )
        role = str(b.get("role") or "")
        ctx = []
        if role:
            ctx.append(f"Role: {role}")
        if instr:
            title = "### Task instruction"
            if instr_truncated:
                title += " (head+tail preview)"
            ctx.extend([title, instr])
        return ctx

    for b in failing:
        tid = str(b.get("task_id", ""))
        passed = int(b.get("passed", 0) or 0)
        total = int(b.get("total", 0) or 0)
        pytest_full = str(b.get("pytest_full") or "")
        code, code_truncated = clip_middle_text(str(b.get("code") or ""), CODE_HEAD, label="solver code")
        stdout, stdout_truncated = clip_middle_text(
            str(b.get("stdout") or ""),
            STDOUT_TAIL,
            label="stdout/stderr",
        )
        tb, tb_ok = _traceback_preview(pytest_full, TRACEBACK_TAIL)
        failed_names = b.get("failure_categories") or re.findall(r"FAILED\s+(\S+)", pytest_full)
        section = [
            f"## {tid} — {passed}/{total} passed",
            *_task_context(b),
            f"Failing tests: {', '.join(failed_names[:30])}",
            "### Traceback (tail)",
            tb,
        ]
        if stdout.strip():
            title = "### Solver stdout/stderr"
            if stdout_truncated:
                title += " (head+tail preview)"
            section.extend([title, stdout])
        stderr, stderr_truncated = clip_middle_text(
            str(b.get("stderr_tail") or b.get("native_stderr_tail") or ""),
            STDOUT_TAIL,
            label="native stderr",
        )
        if stderr.strip():
            title = "### Native stderr"
            if stderr_truncated:
                title += " (head+tail preview)"
            section.extend([title, stderr])
        trajectory_preview, traj_truncated = clip_middle_text(
            str(b.get("trajectory_preview") or ""),
            STDOUT_TAIL,
            label="native trajectory preview",
        )
        if trajectory_preview.strip() and trajectory_preview.strip() not in stdout:
            title = "### Native trajectory preview"
            if traj_truncated or b.get("trajectory_preview_truncated"):
                title += " (head+tail preview)"
            section.extend([title, trajectory_preview])
        section.extend(
            [
                "### Solver code" + (" (head+tail preview)" if code_truncated else ""),
                f"```python\n{code}\n```",
            ]
        )
        text = "\n".join(section)
        if len(text) > fail_budget:
            header = "\n".join(section[:3]) + "\n"
            code_block = "\n### Solver code\n```python\n" + code + "\n```"
            remaining = max(0, fail_budget - len(header) - len(code_block))
            if remaining:
                tb_preview, _tb_truncated = clip_middle_text(
                    tb,
                    remaining,
                    label="pytest output",
                )
                text = header + tb_preview + code_block
            else:
                text, _section_truncated = clip_middle_text(
                    header + code_block,
                    fail_budget,
                    label="task digest section",
                )
        parts.append(text)
        audit[tid] = {
            "chars": len(text),
            "code_included": bool(code.strip()),
            "instruction_included": bool(str(b.get("instruction") or "").strip()),
            "traceback_complete": tb_ok and "Traceback" in tb,
            "code_truncated": code_truncated,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "trajectory_preview_truncated": bool(traj_truncated or b.get("trajectory_preview_truncated")),
            "stdout_artifact": b.get("stdout_artifact") or {},
            "passed": False,
        }

    for b in passing:
        tid = str(b.get("task_id", ""))
        code, code_truncated = clip_middle_text(str(b.get("code") or ""), CODE_HEAD, label="solver code")
        pytest_full = str(b.get("pytest_full") or "")
        stdout, stdout_truncated = clip_middle_text(
            str(b.get("stdout") or ""),
            STDOUT_TAIL,
            label="stdout/stderr",
        )
        section = [
            f"## {tid} — {b.get('passed', 0)}/{b.get('total', 0)} passed (all tests passed)",
            *_task_context(b),
        ]
        if pytest_full.strip():
            pytest_preview, pytest_truncated = clip_middle_text(
                pytest_full,
                TRACEBACK_TAIL,
                label="pytest output",
            )
            section.extend(
                [
                    "### Pytest summary"
                    + (" (head+tail preview)" if pytest_truncated else ""),
                    pytest_preview,
                ]
            )
        if stdout.strip():
            title = "### Solver stdout/stderr"
            if stdout_truncated:
                title += " (head+tail preview)"
            section.extend([title, stdout])
        stderr, stderr_truncated = clip_middle_text(
            str(b.get("stderr_tail") or b.get("native_stderr_tail") or ""),
            STDOUT_TAIL,
            label="native stderr",
        )
        if stderr.strip():
            title = "### Native stderr"
            if stderr_truncated:
                title += " (head+tail preview)"
            section.extend([title, stderr])
        trajectory_preview, traj_truncated = clip_middle_text(
            str(b.get("trajectory_preview") or ""),
            STDOUT_TAIL,
            label="native trajectory preview",
        )
        if trajectory_preview.strip() and trajectory_preview.strip() not in stdout:
            title = "### Native trajectory preview"
            if traj_truncated or b.get("trajectory_preview_truncated"):
                title += " (head+tail preview)"
            section.extend([title, trajectory_preview])
        if code.strip():
            section.extend(
                [
                    "### Successful solver code (reference pattern)"
                    + (" (head+tail preview)" if code_truncated else ""),
                    f"```python\n{code}\n```",
                ]
            )
        text = "\n".join(section)
        if len(text) > pass_budget:
            code_block = (
                "\n### Successful solver code (reference pattern)\n```python\n" + code + "\n```"
                if code.strip()
                else ""
            )
            header = "\n".join(section[: min(4, len(section))]) + "\n"
            remaining = max(0, pass_budget - len(header) - len(code_block))
            if remaining:
                pytest_preview, _pytest_truncated = clip_middle_text(
                    pytest_full,
                    remaining,
                    label="pytest output",
                )
                text = header + pytest_preview + code_block
            else:
                text = header + code_block
            text, _section_truncated = clip_middle_text(
                text,
                pass_budget,
                label="task digest section",
            )
        parts.append(text)
        audit[tid] = {
            "chars": len(text),
            "code_included": bool(code.strip()),
            "instruction_included": bool(str(b.get("instruction") or "").strip()),
            "traceback_complete": True,
            "code_truncated": code_truncated,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "trajectory_preview_truncated": bool(traj_truncated or b.get("trajectory_preview_truncated")),
            "stdout_artifact": b.get("stdout_artifact") or {},
            "passed": True,
        }

    full_digest = "\n\n".join(parts)
    digest, digest_truncated = clip_middle_text(full_digest, DIGEST_CAP, label="full train digest")
    audit["__digest__"] = {
        "chars_full": len(full_digest),
        "chars": len(digest),
        "truncated": digest_truncated,
    }
    for b in failing:
        tid = str(b.get("task_id", ""))
        if tid and tid not in digest:
            audit[tid]["absent_from_digest"] = True
    return digest, audit


def build_editor_prompt_packet(
    *,
    framework: str,
    skill_id: str,
    digest: str,
) -> tuple[str, dict[str, Any]]:
    """Wrap train evidence in a framework-neutral editor policy.

    The baseline still lets each framework use its native update runtime. This
    packet makes the evidence policy comparable for frameworks that accept text
    traces, and gives native-memory frameworks an auditable prompt artifact even
    when their update signal is trajectory/state rather than a prompt.
    """
    policy = SHARED_EDITOR_POLICY_TEMPLATE.format(skill_id=skill_id)
    packet = (
        f"# Shared Train Evidence Packet\n\n"
        f"Version: {SHARED_EDITOR_PROMPT_VERSION}\n"
        f"Framework: {framework}\n"
        f"Skill: {skill_id}\n\n"
        f"## Update Policy\n{policy}\n"
        f"## Train Evidence\n{digest}"
    )
    meta = {
        "version": SHARED_EDITOR_PROMPT_VERSION,
        "framework": framework,
        "skill_id": skill_id,
        "shared_policy_sha256": sha256_text(policy),
        "shared_policy_template_sha256": sha256_text(SHARED_EDITOR_POLICY_TEMPLATE),
        "digest_chars": len(digest),
        "digest_sha256": sha256_text(digest),
        "packet_chars": len(packet),
        "packet_sha256": sha256_text(packet),
    }
    return packet, meta


def editor_cfg_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    editor = dict(manifest["models"]["editor"])
    embedding = manifest["models"]["embedding"]
    reflector = manifest["models"]["reflector"]
    return {
        **editor,
        "embedding_id": embedding["id"],
        "embedding_model": embedding["id"],
        "embedding_api_base": embedding["api_base"],
        "embedding_base": embedding["api_base"],
        "reflector_id": reflector["id"],
        "reflector_model": reflector["id"],
        "reflector_api_base": reflector["api_base"],
        "reflector_base": reflector["api_base"],
    }


def roles_snapshot(manifest: dict[str, Any]) -> dict[str, str]:
    m = manifest["models"]
    return {
        "solver": m["primary_eval_solver"]["id"],
        "solver_served": m["primary_eval_solver"].get("served_id_hint", ""),
        "evolver_editor": m["editor"]["id"],
        "evolver_reflector": m["reflector"]["id"],
        "embedding": m["embedding"]["id"],
        "produce_source": m["source_models_single"][0]["id"],
    }


def assert_role_manifest(manifest: dict[str, Any]) -> None:
    snap = roles_snapshot(manifest)
    solver = snap["solver"]
    evolver = snap["evolver_editor"]
    if solver == evolver:
        raise RuntimeError(f"role split failed: solver and evolver are both {solver!r}")
    if snap["produce_source"] != solver:
        raise RuntimeError(f"produce source model {snap['produce_source']!r} != solver {solver!r}")


def assert_prompt_audit_model(audit_path: Path, expected_model_substr: str, *, label: str) -> None:
    if not audit_path.is_file():
        raise RuntimeError(f"missing prompt_audit for {label}: {audit_path}")
    audit = read_json(audit_path)
    model_id = str(audit.get("model_id") or audit.get("model") or "")
    if expected_model_substr not in model_id and model_id not in expected_model_substr:
        raise RuntimeError(
            f"{label} model mismatch: expected {expected_model_substr!r} in audit, got {model_id!r}"
        )


def mean_pass_rate(results: list[dict[str, Any]]) -> float:
    vals = [float(r.get("pass_rate", 0.0) or 0.0) for r in results if r.get("outcome") == "ok"]
    return sum(vals) / len(vals) if vals else 0.0


def snapshot_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    if src.is_dir():
        shutil.copytree(src, dst)
