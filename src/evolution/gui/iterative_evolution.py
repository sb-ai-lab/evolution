"""Read-only helpers for browsing iterative-evolution experiment artifacts."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactVersion:
    """One text artifact snapshot for a specific skill version."""

    version: str
    text: str
    path: Path


@dataclass
class SkillArtifact:
    """A logical skill artifact with text snapshots across versions."""

    label: str
    kind: str
    versions: dict[str, ArtifactVersion] = field(default_factory=dict)


@dataclass
class CellRecord:
    """One framework/skill cell inside an iterative-evolution run."""

    run_name: str
    schema: str
    framework: str
    skill: str
    path: Path
    best_version: str
    primary_eval_lane: str
    metrics: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)


@dataclass
class IterativeRun:
    """A direct run or a sweep root under experiments/iterative_evolution/runs."""

    name: str
    path: Path
    kind: str
    cells: list[CellRecord]
    progress: dict[str, Any] = field(default_factory=dict)
    metrics_latest: dict[str, Any] = field(default_factory=dict)


def discover_iterative_runs(runs_root: Path) -> list[IterativeRun]:
    """Discover direct iterative runs and sweep roots under ``runs_root``."""
    root = Path(runs_root)
    if not root.is_dir():
        return []

    runs: list[IterativeRun] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("__pycache__"):
            continue
        cells = _discover_sweep_cells(child)
        kind = "sweep" if cells else ""
        if not cells:
            direct = _discover_direct_cell(child)
            if direct is not None:
                cells = [direct]
                kind = "direct"
        if cells:
            runs.append(
                IterativeRun(
                    name=child.name,
                    path=child,
                    kind=kind,
                    cells=cells,
                    progress=_read_json_obj(child / "progress.json"),
                    metrics_latest=_read_json_obj(child / "metrics_latest.json"),
                )
            )

    return sorted(
        runs,
        key=lambda run: (0 if run.kind == "sweep" else 1, -_mtime(run.path), run.name),
    )


def discover_skill_artifacts(
    cell_path: Path,
    *,
    skill: str,
    best_version: str = "",
) -> list[SkillArtifact]:
    """Return canonical and native skill-text artifacts for a cell."""
    cell = Path(cell_path)
    artifacts: dict[str, SkillArtifact] = {}

    canonical_root = cell / "skills" / skill / "versions"
    canonical = SkillArtifact(label=f"canonical/{skill}/SKILL.md", kind="canonical")
    for version_dir in sorted(canonical_root.glob("v*"), key=lambda p: _version_sort_key(p.name)):
        skill_md = version_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        canonical.versions[version_dir.name] = ArtifactVersion(
            version=version_dir.name,
            text=_read_text(skill_md),
            path=skill_md,
        )
    if canonical.versions:
        artifacts[canonical.label] = canonical

    current = cell / "skills" / skill / "SKILL.md"
    if current.is_file():
        label = f"canonical/{skill}/SKILL.md"
        artifact = artifacts.setdefault(label, SkillArtifact(label=label, kind="canonical"))
        current_version = best_version or "current"
        artifact.versions.setdefault(
            current_version,
            ArtifactVersion(version=current_version, text=_read_text(current), path=current),
        )

    native_root = cell / "native_state_versions"
    if native_root.is_dir():
        _add_native_skill_md_artifacts(native_root, artifacts)
        _add_native_document_artifacts(native_root, artifacts)

    return sorted(
        artifacts.values(),
        key=lambda item: (0 if item.kind == "canonical" else 1, item.label),
    )


def default_diff_versions(artifact: SkillArtifact, best_version: str = "") -> tuple[str, str]:
    """Choose a useful default diff pair, preferring seed to selected best."""
    versions = sorted(artifact.versions, key=_version_sort_key)
    if not versions:
        return "", ""
    left = "v0" if "v0" in artifact.versions else versions[0]
    if best_version and best_version in artifact.versions:
        right = best_version
    else:
        right = versions[-1]
    return left, right


def build_unified_diff(
    *,
    old_text: str,
    new_text: str,
    old_label: str,
    new_label: str,
    context_lines: int = 3,
) -> str:
    """Build a unified text diff for display/copying."""
    lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=old_label,
        tofile=new_label,
        lineterm="",
        n=context_lines,
    )
    return "\n".join(lines)


def build_side_by_side_rows(
    old_text: str,
    new_text: str,
    *,
    context_lines: int = 3,
) -> list[dict[str, Any]]:
    """Return changed-line rows suitable for a Streamlit dataframe."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    rows: list[dict[str, Any]] = []

    for group_idx, group in enumerate(matcher.get_grouped_opcodes(context_lines)):
        if group_idx:
            rows.append(_gap_row())
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for old_idx, new_idx in zip(range(i1, i2), range(j1, j2), strict=False):
                    rows.append(
                        {
                            "kind": "context",
                            "old_line": str(old_idx + 1),
                            "old_text": old_lines[old_idx],
                            "new_line": str(new_idx + 1),
                            "new_text": new_lines[new_idx],
                        }
                    )
                continue
            max_len = max(i2 - i1, j2 - j1)
            for offset in range(max_len):
                old_idx = i1 + offset
                new_idx = j1 + offset
                has_old = old_idx < i2
                has_new = new_idx < j2
                rows.append(
                    {
                        "kind": tag,
                        "old_line": str(old_idx + 1) if has_old else "",
                        "old_text": old_lines[old_idx] if has_old else "",
                        "new_line": str(new_idx + 1) if has_new else "",
                        "new_text": new_lines[new_idx] if has_new else "",
                    }
                )

    return rows


_SXS_DIFF_CSS = """
<style>
  html, body { margin: 0; padding: 0; }
  .sxs-diff-wrap { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; width: 100%; }
  table.diff { border-collapse: collapse; width: 100%; table-layout: fixed; }
  table.diff td, table.diff th { padding: 2px 6px; vertical-align: top; border: none; }
  table.diff thead th { background: #f3f4f6; color: #111827; text-align: left; font-weight: 600; border-bottom: 1px solid #d1d5db; }
  table.diff td.diff_header { background: #f9fafb; color: #6b7280; text-align: right; user-select: none; font-variant-numeric: tabular-nums; }
  table.diff td.diff_add { background: #dcfce7; }
  table.diff td.diff_chg { background: #fef9c3; }
  table.diff td.diff_sub { background: #fee2e2; }
  table.diff span.diff_add { background: #86efac; color: #052e16; }
  table.diff span.diff_chg { background: #fde047; color: #422006; }
  table.diff span.diff_sub { background: #fca5a5; color: #450a0a; }
  table.diff td:nth-child(2), table.diff td:nth-child(4) { white-space: pre-wrap; word-break: break-word; }
  table.diff td:nth-child(3), table.diff thead th:nth-child(2) { border-left: 1px solid #d1d5db; }
</style>
"""

_DIFF_NEXT_CELL_RE = re.compile(
    r'<t[hd][^>]*class="diff_next"[^>]*>.*?</t[hd]>',
    flags=re.DOTALL | re.IGNORECASE,
)


def render_side_by_side_html(
    left_text: str,
    right_text: str,
    left_label: str,
    right_label: str,
    *,
    context: bool = False,
) -> tuple[str, int]:
    """Build a GitHub-style side-by-side HTML diff with line numbers and
    intra-line (word-level) highlights. Returns ``(html, height_px)`` for
    ``streamlit.components.v1.html``."""
    left_lines = (left_text or "").splitlines() or [""]
    right_lines = (right_text or "").splitlines() or [""]
    table = difflib.HtmlDiff(wrapcolumn=80).make_table(
        left_lines,
        right_lines,
        fromdesc=left_label,
        todesc=right_label,
        context=context,
        numlines=3,
    )
    # Drop the "n"-navigation cells -> 4 columns: line# / text / line# / text.
    table = _DIFF_NEXT_CELL_RE.sub("", table)
    # HtmlDiff hard-codes 6 empty colgroups (phantom columns); replace with ours.
    table = re.sub(r"<colgroup>\s*</colgroup>\s*", "", table)
    table = re.sub(r'\s+rules="groups"', "", table)
    colgroup = "<colgroup><col style='width:3.5em'><col><col style='width:3.5em'><col></colgroup>"
    table = re.sub(r"(<table[^>]*>)", r"\1" + colgroup, table, count=1)
    html = f"{_SXS_DIFF_CSS}<div class='sxs-diff-wrap'>{table}</div>"
    height = max(400, min(1600, max(len(left_lines), len(right_lines)) * 22 + 140))
    return html, height


def metric_value(cell: CellRecord, split: str, condition: str, key: str) -> Any:
    """Safely read a nested cell metric."""
    block = cell.metrics.get(split)
    if not isinstance(block, dict):
        return None
    condition_block = block.get(condition)
    if not isinstance(condition_block, dict):
        return None
    return condition_block.get(key)


def metric_delta(cell: CellRecord, split: str, key: str) -> Any:
    """Safely read a nested delta metric."""
    block = cell.metrics.get(split)
    if not isinstance(block, dict):
        return None
    delta = block.get("delta")
    if not isinstance(delta, dict):
        return None
    return delta.get(key)


def _discover_sweep_cells(run_dir: Path) -> list[CellRecord]:
    cells: list[CellRecord] = []
    for metrics_path in sorted(run_dir.glob("*/cells/*/*/cell_metrics.json")):
        cell_dir = metrics_path.parent
        metrics = _read_json_obj(metrics_path)
        summary = _read_json_obj(cell_dir / "summary.json")
        manifest = _read_json_obj(cell_dir / "cell_manifest.json")
        schema = str(metrics.get("schema") or cell_dir.parents[2].name)
        framework = str(metrics.get("framework") or cell_dir.parent.name)
        skill = str(metrics.get("skill") or cell_dir.name)
        cells.append(
            CellRecord(
                run_name=run_dir.name,
                schema=schema,
                framework=framework,
                skill=skill,
                path=cell_dir,
                best_version=str(
                    metrics.get("best_version") or summary.get("best_version") or "v0"
                ),
                primary_eval_lane=str(
                    metrics.get("primary_eval_lane") or summary.get("primary_eval_lane") or ""
                ),
                metrics=metrics,
                summary=summary,
                manifest=manifest,
            )
        )
    return cells


def _discover_direct_cell(run_dir: Path) -> CellRecord | None:
    summary = _read_json_obj(run_dir / "summary.json")
    manifest = _read_json_obj(run_dir / "cell_manifest.json")
    seed = _read_json_obj(run_dir / "seed_meta.json")
    if not summary and not manifest and not seed:
        return None
    framework = str(summary.get("framework") or manifest.get("framework") or "")
    skill = str(
        summary.get("skill_id")
        or manifest.get("skill_id")
        or seed.get("skill_id")
        or _infer_single_skill(run_dir)
        or ""
    )
    if not framework and not skill:
        return None
    return CellRecord(
        run_name=run_dir.name,
        schema="direct",
        framework=framework or "unknown",
        skill=skill or "unknown",
        path=run_dir,
        best_version=str(summary.get("best_version") or "v0"),
        primary_eval_lane=str(summary.get("primary_eval_lane") or ""),
        metrics={},
        summary=summary,
        manifest=manifest,
    )


def _add_native_skill_md_artifacts(
    native_root: Path,
    artifacts: dict[str, SkillArtifact],
) -> None:
    for skill_md in sorted(native_root.glob("*/v*/**/SKILL.md")):
        version_dir = _native_version_dir(native_root, skill_md)
        if version_dir is None:
            continue
        framework = version_dir.parent.name
        version = version_dir.name
        rel = skill_md.relative_to(version_dir).as_posix()
        label = f"native/{framework}/{rel}"
        artifact = artifacts.setdefault(label, SkillArtifact(label=label, kind="native"))
        artifact.versions[version] = ArtifactVersion(
            version=version,
            text=_read_text(skill_md),
            path=skill_md,
        )


def _add_native_document_artifacts(
    native_root: Path,
    artifacts: dict[str, SkillArtifact],
) -> None:
    for doc_path in sorted(native_root.glob("*/v*/**/documents.json")):
        version_dir = _native_version_dir(native_root, doc_path)
        if version_dir is None:
            continue
        framework = version_dir.parent.name
        version = version_dir.name
        rel = doc_path.relative_to(version_dir).as_posix()
        payload = _read_json(doc_path)
        if not isinstance(payload, list):
            continue
        for idx, entry in enumerate(payload):
            text = _document_text(entry)
            if not text:
                continue
            label = f"native/{framework}/{rel}#{idx}"
            artifact = artifacts.setdefault(label, SkillArtifact(label=label, kind="native"))
            artifact.versions[version] = ArtifactVersion(
                version=version,
                text=text,
                path=doc_path,
            )


def _document_text(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    page_content = entry.get("page_content")
    if isinstance(page_content, str) and page_content.strip():
        return page_content
    metadata = entry.get("metadata")
    if isinstance(metadata, dict):
        workflow = metadata.get("workflow")
        if isinstance(workflow, str) and workflow.strip():
            return workflow
    return ""


def _native_version_dir(native_root: Path, path: Path) -> Path | None:
    rel = path.relative_to(native_root)
    parts = rel.parts
    if len(parts) < 2 or not _is_version(parts[1]):
        return None
    return native_root / parts[0] / parts[1]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def _read_json_obj(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    return payload if isinstance(payload, dict) else {}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _infer_single_skill(run_dir: Path) -> str:
    skills_dir = run_dir / "skills"
    if not skills_dir.is_dir():
        return ""
    children = [p.name for p in skills_dir.iterdir() if p.is_dir()]
    return children[0] if len(children) == 1 else ""


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_version(text: str) -> bool:
    return bool(re.fullmatch(r"v\d+", text))


def _version_sort_key(version: str) -> tuple[int, str]:
    if _is_version(version):
        return (int(version[1:]), version)
    return (10**9, version)


def _gap_row() -> dict[str, Any]:
    return {
        "kind": "gap",
        "old_line": "",
        "old_text": "...",
        "new_line": "",
        "new_text": "...",
    }


__all__ = [
    "ArtifactVersion",
    "CellRecord",
    "IterativeRun",
    "SkillArtifact",
    "build_side_by_side_rows",
    "build_unified_diff",
    "default_diff_versions",
    "discover_iterative_runs",
    "discover_skill_artifacts",
    "metric_delta",
    "metric_value",
    "render_side_by_side_html",
]
