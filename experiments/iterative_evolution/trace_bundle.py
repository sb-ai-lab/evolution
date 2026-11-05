"""iter-trace-bundle-v1 — full train solve evidence + editor digests."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA = "iter-trace-bundle-v1"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def _clip_middle(text: str, *, max_chars: int, label: str) -> str:
    """Preserve both head and tail when a model-facing cap is unavoidable."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = (
        f"\n\n[... omitted {len(text) - max_chars} chars from the middle of "
        f"{label}; preserved beginning and end ...]\n\n"
    )
    if len(marker) >= max_chars:
        return text[:max_chars]
    keep = max_chars - len(marker)
    head = max(1, int(keep * 0.65))
    tail = max(1, keep - head)
    return text[:head] + marker + text[-tail:]


def _json_blob(value: Any, *, max_chars: int, label: str) -> str:
    return _clip_middle(
        json.dumps(value, ensure_ascii=False, indent=2),
        max_chars=max_chars,
        label=label,
    )


def _preview_texts(bundle: dict[str, Any]) -> list[tuple[str, str]]:
    previews = bundle.get("workspace_file_previews") or []
    out: list[tuple[str, str]] = []
    if not isinstance(previews, list):
        return out
    for item in previews:
        if not isinstance(item, dict):
            continue
        out.append((str(item.get("path") or ""), str(item.get("preview") or "")))
    return out


def _collect_editor_redactions(bundle: dict[str, Any]) -> set[str]:
    """Collect train-task literals that must not become skill instructions."""
    literals: set[str] = set()

    def add_literal(value: object) -> None:
        text = str(value or "").strip()
        if not text or len(text) < 3:
            return
        literals.add(text)

    text_blobs = [str(bundle.get("instruction") or "")]
    for path, preview in _preview_texts(bundle):
        text_blobs.extend([path, preview])
        for part in Path(path).parts:
            if "." in part:
                add_literal(part)
    for art in bundle.get("produced_artifacts") or []:
        if isinstance(art, dict):
            for part in Path(str(art.get("path") or "")).parts:
                if "." in part:
                    add_literal(part)

    for blob in text_blobs:
        for match in re.findall(
            r"\b[A-Za-z0-9_.-]+\.(?:csv|json|ya?ml|txt|md|xlsx|docx|pdf)\b", blob
        ):
            add_literal(match)

    for path, preview in _preview_texts(bundle):
        if path.endswith(".json"):
            try:
                data = json.loads(preview)
            except Exception:
                continue
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(key, str) and ("-" in key or "_" in key or len(key) >= 6):
                        add_literal(key)
                    if isinstance(value, dict):
                        for subkey in value:
                            if isinstance(subkey, str) and (
                                "duration" in subkey.lower()
                                or "minute" in subkey.lower()
                                or "_" in subkey
                            ):
                                add_literal(subkey)
    return literals


_EDITOR_REDACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"/home/[^\s`\"')]+", "<absolute-path>"),
    (r"\b[A-Za-z]:\\[^\s`\"')]+", "<absolute-path>"),
    (r"\bdir\s+environment(?:/data)?/", "dir <task-input-dir>/"),
    (r"\bdir\s+output/", "dir <requested-output-dir>/"),
    (r"\bdir\s+tests(?:/[^`\s\"')]*)?", "dir <verifier-dir>/"),
    (r"`?\.\./environment/data/[^`\s\"')]+`?", "../<task-input-path>"),
    (r"`?/?(?:workspace/)?environment/data/[^`\s\"')]+`?", "<task-input-path>"),
    (r"\benvironment/data/?\b", "<task-input-dir>"),
    (r"`?(?:\./)?output/[^`\s\"')]+`?", "<requested-output-path>"),
    (r"\boutput/?\b", "<requested-output-dir>"),
    (r"\btests/[^`\s\"')]+", "<verifier-file>"),
    (
        r"\b(?:solve\.sh|solution\.py|_solve\.py|task\.toml|prompt\.md|response\.md|data_generator\.py)\b",
        "<benchmark-runtime-file>",
    ),
    (r"\bavg_duration_minutes\b", "<average-duration-field>"),
    (r"\bp95_duration_minutes\b", "<tail-duration-field>"),
)


def _redact_editor_text(text: str, bundle: dict[str, Any]) -> str:
    """Redact task/runtime literals from editor-facing text only.

    Raw bundles keep the original content. The digest given to frameworks keeps
    the procedural signal while preventing direct copying of train filenames,
    benchmark paths, and exact schema keys into SKILL.md.
    """
    out = str(text or "")
    for pattern, replacement in _EDITOR_REDACTION_PATTERNS:
        out = re.sub(pattern, replacement, out)
    for lit in sorted(_collect_editor_redactions(bundle), key=len, reverse=True):
        if lit in {"json", "yaml", "yml", "csv", "txt", "md"}:
            continue
        replacement = "<task-literal>"
        if "." in lit:
            replacement = "<task-file>"
        elif "duration" in lit.lower() or "minute" in lit.lower():
            replacement = "<task-schema-field>"
        out = re.sub(rf"(?<![A-Za-z0-9_.-]){re.escape(lit)}(?![A-Za-z0-9_.-])", replacement, out)
    return out


def _maybe_redact_editor_text(text: str, bundle: dict[str, Any], *, redact: bool) -> str:
    return _redact_editor_text(text, bundle) if redact else str(text or "")


def bundle_path(run_dir: Path, round_idx: int, task_id: str) -> Path:
    return run_dir / "traces" / f"round{round_idx}" / task_id / "bundle.json"


def write_bundle(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": SCHEMA, **payload}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_bundle(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _produced_artifacts(cache_dir: Path) -> list[dict[str, Any]]:
    workspace = cache_dir / "workspace"
    if not workspace.is_dir():
        return []
    artifacts: list[dict[str, Any]] = []
    skip_roots = {
        "environment",
        "tests",
        "__pycache__",
    }
    skip_names = {
        "_solve.py",
        "solution.py",
        "solve.sh",
        "task.toml",
        "instruction.md",
        "data_generator.py",
    }
    for p in sorted(workspace.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace)
        if rel.parts and rel.parts[0] in skip_roots:
            continue
        if rel.name in skip_names:
            continue
        try:
            artifacts.append(
                {
                    "path": str(rel),
                    "size": p.stat().st_size,
                    "sha256": _sha256_file(p),
                }
            )
        except OSError:
            continue
    return artifacts


def _workspace_snapshot(cache_dir: Path) -> str:
    workspace = cache_dir / "workspace"
    if not workspace.is_dir():
        return "(workspace missing)"
    lines: list[str] = []
    for p in sorted(workspace.rglob("*")):
        try:
            rel = p.relative_to(workspace)
        except ValueError:
            continue
        if not rel.parts:
            continue
        if rel.parts[0] in {"__pycache__", ".pytest_cache"}:
            continue
        if p.is_dir():
            if len(rel.parts) <= 2:
                lines.append(f"dir  {rel}/")
            continue
        if len(rel.parts) > 3:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        lines.append(f"file {rel} ({size} bytes)")
        max_lines = _env_int("EVO_TRACE_WORKSPACE_MAX_LINES", 400)
        if len(lines) >= max_lines:
            lines.append(f"... (omitted remaining workspace listing after {max_lines} entries)")
            break
    return "\n".join(lines) if lines else "(workspace empty)"


def _workspace_file_previews(cache_dir: Path) -> list[dict[str, Any]]:
    workspace = cache_dir / "workspace"
    if not workspace.is_dir():
        return []
    blind = os.environ.get("EVO_REFLECT_BLIND", "1").strip() != "0"
    exts = {".csv", ".json", ".md", ".toml", ".txt", ".yaml", ".yml"}
    skip_names = {
        "_solve.py",
        "solution.py",
        "solve.sh",
        "data_generator.py",
        "task.toml",
    }
    previews: list[dict[str, Any]] = []
    max_files = _env_int("EVO_TRACE_PREVIEW_MAX_FILES", 40)
    per_file_chars = _env_int("EVO_TRACE_FILE_PREVIEW_CHARS", 12000)

    def _priority(path: Path) -> tuple[int, str]:
        rel = path.relative_to(workspace)
        if rel.parts[:2] == ("environment", "data"):
            return (0, str(rel))
        if len(rel.parts) == 1:
            return (1, str(rel))
        if rel.parts and rel.parts[0] == "tests":
            return (3, str(rel))
        return (2, str(rel))

    candidates = [p for p in workspace.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    omitted = 0
    for p in sorted(candidates, key=_priority):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel = p.relative_to(workspace)
        if rel.name in skip_names:
            continue
        if blind and rel.parts and rel.parts[0] == "tests":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        previews.append(
            {
                "path": str(rel),
                "solver_read_path_from_output_cwd": "../" + rel.as_posix(),
                "solver_read_path_from_workspace_root": rel.as_posix(),
                "size": p.stat().st_size,
                "preview": _clip_middle(
                    text,
                    max_chars=per_file_chars,
                    label=f"file preview {rel}",
                ),
                "truncated": len(text) > per_file_chars if per_file_chars else False,
            }
        )
        if len(previews) >= max_files:
            omitted = len(candidates) - max_files
            break
    if omitted > 0:
        previews.append(
            {
                "path": "(omitted)",
                "size": 0,
                "preview": (
                    f"Omitted {omitted} additional previewable files after "
                    f"{max_files}; raise EVO_TRACE_PREVIEW_MAX_FILES if needed."
                ),
                "truncated": True,
            }
        )
    return previews


def enrich_bundle_from_cache(bundle: dict[str, Any], *, cache_dir: Path) -> dict[str, Any]:
    """Backfill cache-derived evidence for old bundles copied into new runs."""
    out = dict(bundle)
    out["produced_artifacts"] = _produced_artifacts(cache_dir)
    out["workspace_snapshot"] = _workspace_snapshot(cache_dir)
    out["workspace_file_previews"] = _workspace_file_previews(cache_dir)
    out["cache_dir"] = str(cache_dir)
    return out


def bundle_from_solve_result(
    *,
    round_idx: int,
    task_id: str,
    role: str,
    skill_id: str,
    skill_version: str,
    skill_sha256: str,
    instruction: str,
    solve_result: dict[str, Any],
    cache_dir: Path,
    leak_findings: int = 0,
) -> dict[str, Any]:
    """Aggregate neutral-solver artifacts into a trace bundle."""
    prompt = ""
    response = ""
    code = ""
    solve_log = ""
    pytest_full = ""
    for name, key in (
        ("prompt.md", "prompt"),
        ("response.md", "response"),
        ("solve.py", "code"),
        ("solve_run.log", "solve_log"),
        ("pytest_full.log", "pytest_full"),
        ("pytest.log", "pytest_full"),
    ):
        p = cache_dir / name
        if p.is_file():
            if key == "pytest_full" and pytest_full:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if key == "prompt":
                prompt = text
            elif key == "response":
                response = text
            elif key == "code":
                code = text
            elif key == "solve_log":
                solve_log = text
            elif key == "pytest_full":
                pytest_full = text

    passed = int(solve_result.get("passed", 0) or 0)
    total = int(solve_result.get("total_tests", 0) or 0)
    pass_rate = float(solve_result.get("pass_rate", 0.0) or 0.0)
    failure_categories: list[str] = []
    if solve_result.get("code_extraction_failed"):
        failure_categories.append("code_extraction_failed")
    if total > 0 and pass_rate < 1.0:
        failure_categories.append("produced_but_failed_verification")
    if solve_result.get("outcome") != "ok":
        failure_categories.append(str(solve_result.get("error", "hard_fail"))[:120])

    return {
        "round": round_idx,
        "task_id": task_id,
        "role": role,
        "skill_id": skill_id,
        "skill_version": skill_version,
        "skill_sha256": skill_sha256,
        "instruction": instruction,
        "prompt": prompt,
        "response": response,
        "code": code,
        "stdout": solve_log,
        "stderr": "",
        "pytest_full": pytest_full,
        "pass_rate": pass_rate,
        "passed": passed,
        "total": total,
        "failure_categories": failure_categories,
        "solve_result": {
            k: solve_result.get(k)
            for k in (
                "outcome",
                "error",
                "elapsed_s",
                "code_extraction_failed",
                "code_extraction_initial_failed",
                "code_extraction_retried",
                "code_repair_error",
                "pytest_status",
            )
        },
        "leak_scan": {"findings": leak_findings},
        **enrich_bundle_from_cache({}, cache_dir=cache_dir),
    }


def _sanitize_for_editor(text: str, *, blind: bool) -> str:
    if not blind:
        return _clip_middle(
            text,
            max_chars=_env_int("EVO_TRACE_FAILURE_CHARS", 24000),
            label="failure evidence",
        )
    # Blind mode: drop pytest nodeids / FAILED lines (lightweight; full sanitizer in evidence.py).
    lines: list[str] = []
    for line in (text or "").splitlines():
        if "FAILED " in line or "ERROR " in line:
            lines.append("[sanitized test line]")
        else:
            lines.append(line)
    return _clip_middle(
        "\n".join(lines),
        max_chars=_env_int("EVO_TRACE_FAILURE_CHARS", 24000),
        label="sanitized failure evidence",
    )


def _runtime_path_contract_section(bundle: dict[str, Any]) -> tuple[str, str]:
    contract = bundle.get("runtime_contract") or {}
    if not isinstance(contract, dict):
        contract = {}
    kind = str(contract.get("kind") or "")
    if kind == "native_workspace_root":
        section = (
            "## Runtime Path Contract\n"
            "- This trace came from a native framework runtime, not the neutral body-injection solver.\n"
            "- The agent/tool process executes with cwd set to the workspace root.\n"
            "- Workspace snapshots and file previews below are shown relative to that workspace root.\n"
            "- File previews include `solver_read_path_from_workspace_root`; that is the exact path form native solver code or file tools should use for reads.\n"
            "- In native solver code or tool calls, read workspace-root inputs with `<path>` such as `environment/data/input.json`.\n"
            "- Write requested deliverables under `output/<filename>` from the workspace root.\n"
            "- Do not teach `../...` input paths or current-directory output writes for this native runtime.\n\n"
        )
        guidance = (
            "preserve native workspace-root paths from the Runtime Path Contract "
            "(`<path>` for reads, `output/...` for writes), not neutral-solver "
            "`../...` reads or `./...` writes"
        )
        return section, guidance
    section = (
        "## Runtime Path Contract\n"
        "- The solver executes generated Python with cwd set to `<workspace>/output`.\n"
        "- Workspace snapshots and file previews below are shown relative to the workspace root, one directory above that cwd.\n"
        "- File previews include `solver_read_path_from_output_cwd`; that is the exact path form solver code should use for reads.\n"
        "- In solver code, read workspace-root inputs with `../<path>` and write requested deliverables to `./<filename>`.\n"
        "- Do not teach workspace-root input paths as directly openable from solver cwd.\n\n"
    )
    guidance = (
        "preserve solver-cwd paths from the Runtime Path Contract (`../...` "
        "for reads, `./...` for writes), not corpus/storage paths or "
        "workspace-root paths"
    )
    return section, guidance


def render_bundle_digest(
    bundle: dict[str, Any],
    *,
    skill_body: str,
    redact: bool = True,
    blind: bool | None = None,
) -> str:
    """Render one bundle into plain evidence for framework editors.

    Do not embed the core Evolution REFLECT_PROMPT here. Hermes/Memento receive
    this digest inside an instruction to edit SKILL.md; a nested "respond as
    JSON" analysis request causes them to analyze instead of using their native
    skill-edit tools.
    """
    if blind is None:
        blind = os.environ.get("EVO_REFLECT_BLIND", "1").strip() != "0"
    passed = int(bundle.get("passed", 0) or 0)
    total = int(bundle.get("total", 0) or 0)
    failure_evidence = _maybe_redact_editor_text(
        _sanitize_for_editor(
            (bundle.get("stdout") or "") + "\n" + (bundle.get("pytest_full") or ""),
            blind=blind,
        ),
        bundle,
        redact=redact,
    )
    produced_blob = _json_blob(
        bundle.get("produced_artifacts") or [],
        max_chars=_env_int("EVO_TRACE_PRODUCED_CHARS", 16000),
        label="produced artifact metadata",
    )
    produced_blob = _maybe_redact_editor_text(produced_blob, bundle, redact=redact)
    previews_blob = _json_blob(
        bundle.get("workspace_file_previews") or [],
        max_chars=_env_int("EVO_TRACE_PREVIEWS_CHARS", 48000),
        label="train file previews",
    )
    previews_blob = _maybe_redact_editor_text(previews_blob, bundle, redact=redact)
    workspace_snapshot = _clip_middle(
        str(bundle.get("workspace_snapshot") or "(not captured)"),
        max_chars=_env_int("EVO_TRACE_WORKSPACE_CHARS", 16000),
        label="workspace snapshot",
    )
    workspace_snapshot = _maybe_redact_editor_text(workspace_snapshot, bundle, redact=redact)
    runtime_section, contract_guidance = _runtime_path_contract_section(bundle)
    workspace_section = (
        runtime_section + "## Workspace Snapshot\n"
        f"{workspace_snapshot}\n\n"
        "## Train File Previews\n"
        f"{previews_blob}\n\n"
    )
    high_signal = (
        "## High-Signal Execution Evidence\n"
        f"{failure_evidence}\n\n"
        "## Produced Artifact Metadata\n"
        f"{produced_blob}\n\n"
    )
    skill_blob = _clip_middle(
        skill_body,
        max_chars=_env_int("EVO_TRACE_SKILL_CHARS", 262144),
        label="skill body",
    )
    skill_blob = _maybe_redact_editor_text(skill_blob, bundle, redact=redact)
    code_blob = _clip_middle(
        bundle.get("code", ""),
        max_chars=_env_int("EVO_TRACE_CODE_CHARS", 48000),
        label="generated code",
    )
    code_blob = _maybe_redact_editor_text(code_blob, bundle, redact=redact)
    solve_result = bundle.get("solve_result") or {}
    return "\n".join(
        [
            "# Trace Bundle",
            "",
            "## Task Instruction",
            _maybe_redact_editor_text(
                str(bundle.get("instruction", "")),
                bundle,
                redact=redact,
            ),
            "",
            workspace_section + high_signal,
            "## Current Skill Body",
            skill_blob,
            "",
            "## Generated Solver Code",
            "```python",
            code_blob,
            "```",
            "",
            "## Result",
            f"Passed: {passed}/{total}",
            f"pass_rate: {bundle.get('pass_rate', 0.0)}",
            f"outcome: {solve_result.get('outcome', 'ok')}",
            f"diagnostic: {solve_result.get('error') or solve_result.get('pytest_status') or ''}",
            "",
            "## Edit Guidance",
            (
                "Update the reusable skill procedure so future solver code avoids "
                "the observed failure. Prefer general procedural rules from the "
                "train evidence: input schema handling, respecting the fixed "
                "runtime contract, artifact checks, and verification-driven "
                "debugging. If you preserve concrete path guidance for a "
                f"single-task diagnostic, {contract_guidance}. Do not copy test "
                "code, exact train filenames, exact schema keys, benchmark paths, "
                "or task answers into the skill."
            ),
        ]
    )


def render_traces_digest(
    bundles: list[dict[str, Any]],
    *,
    skill_body: str,
    redact: bool = True,
    blind: bool | None = None,
) -> str:
    parts = [
        render_bundle_digest(b, skill_body=skill_body, redact=redact, blind=blind) for b in bundles
    ]
    return "\n\n---\n\n".join(parts)


def bundles_to_reflections(bundles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert bundles into reflection-shaped dicts for rewrite_skill."""
    out: list[dict[str, Any]] = []
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


__all__ = [
    "SCHEMA",
    "bundle_from_solve_result",
    "bundle_path",
    "bundles_to_reflections",
    "enrich_bundle_from_cache",
    "load_bundle",
    "render_traces_digest",
    "write_bundle",
]
