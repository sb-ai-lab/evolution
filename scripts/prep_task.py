#!/usr/bin/env python3
"""Prepare a benchmark task for an evo solve attempt, with run helpers.

Workflow:
    1. Locate the task source under ``<bench_root>/test/<role>/<task>/`` (or
       ``<bench_root>/tasks/<role>/<task>/`` as a fallback).
    2. Make a safe copy under ``<workdir>/<role>/<task>/`` — solve.sh, tests,
       environment/data, task.toml, instruction.md, etc. Excludes anything
       output-shaped (``output/``, ``solution.py``, ``__pycache__``).
    3. For each skill in the task's ``[task].skills`` list:
         - If ``<store>/<skill>/`` already exists, rename it to
           ``<skill>-deprecated-on-<YYYYMMDD-HHMM>/`` (preserves history).
         - Stage a fresh skill source: copy ``<bench_root>/skills/<skill>/``
           into a temp dir, ensuring ``SKILL.md`` contains the chosen seed
           (``SKILL.md`` by default, or ``SKILL_HANDCRAFT.md`` if requested).
         - Call ``SkillStore.import_skill`` so evo registers the seed as
           the active version (evo internally calls this v1; the user views
           it as "v0 from the benchmark").
         - Read the body back via ``SkillStore.get(name)`` and write it as
           ``<workdir>/.../skills/<name>/SKILL.md`` so a bench.py-style
           runner picks it up.
    4. Return a dict describing what was prepared.

Run helpers mirror bench.py:
    - ``run_solve(workdir, python_exe, ...)`` runs solve.sh patched to use
      the role-specific Python.
    - ``run_pytest(workdir, python_exe, ...)`` runs the task's pytest with
      ``TASK_WORKSPACE`` set and the role's bin dir on PATH.

CLI:
    python prep_task.py de sales-pivot-analysis
    python prep_task.py de sales-pivot-analysis --seed-file SKILL_HANDCRAFT.md
    python prep_task.py de sales-pivot-analysis --solution my_solution.py
        # → prep + drop solution.py + run solve.sh + run pytest

The script never overwrites the evo skill store destructively; existing
``<skill>/`` dirs are renamed, never deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_EVO_SRC = Path(__file__).parent.parent / "src"
if _EVO_SRC.is_dir() and str(_EVO_SRC) not in sys.path:
    sys.path.insert(0, str(_EVO_SRC))

from evolution.core.store import SkillStore  # noqa: E402

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore


# ── constants ───────────────────────────────────────────────────────────────

EVO_REPO = Path(os.environ.get("SKILL_EVOLUTIONS_ROOT", str(Path(__file__).parent.parent)))
DEFAULT_BENCH_ROOT = Path(
    os.environ.get("AFTER_DATA_ROOT", str(EVO_REPO.parent / "datasets" / "sb-bench"))
)

_mlspace = Path.home() / ".mlspace" / "envs"
ROLE_PYTHON_MAP: dict[str, str] = {
    "de": str(_mlspace / "de-env/bin/python"),
    "ds": str(_mlspace / "ds-env/bin/python"),
    "genai": str(_mlspace / "genai-env/bin/python"),
    "infra": str(_mlspace / "infra-env/bin/python"),
    "pm": str(_mlspace / "pm-env/bin/python"),
    "swe": str(_mlspace / "swe-env/bin/python"),
}

# Names we never want to drag from the bench source into the safe copy.
_COPY_IGNORE = shutil.ignore_patterns(
    "output",
    "solution.py",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    "_solve_output.txt",
    "_pytest_output.txt",
)


# ── data classes ────────────────────────────────────────────────────────────


@dataclass
class PrepResult:
    workdir: Path
    role: str
    task_id: str
    role_python: str
    skills_registered: list[str]
    deprecated_skill_dirs: list[Path]
    timeout_sec: int


# ── helpers ─────────────────────────────────────────────────────────────────


def _resolve_task_source(bench_root: Path, role: str, task_id: str) -> Path:
    """Find the canonical source dir for ``<role>/<task_id>``.

    Checks ``test/`` first, then ``tasks/`` (sb-bench-style). Raises if neither.
    """
    for sub in ("test", "tasks"):
        candidate = bench_root / sub / role / task_id
        if (candidate / "task.toml").exists():
            return candidate
    raise FileNotFoundError(f"No task.toml under {bench_root}/{{test,tasks}}/{role}/{task_id}/")


def _read_task_toml(task_dir: Path) -> dict:
    p = task_dir / "task.toml"
    with p.open("rb") as f:
        return tomllib.load(f)


def _default_workdir(role: str, task_id: str) -> Path:
    """`runs/<UTC-timestamp>/<role>/<task_id>/`, anchored at this script's dir."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(__file__).resolve().parent / "runs" / ts / role / task_id


def _deprecation_suffix() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


def _copy_task_tree(src: Path, dst: Path) -> None:
    """Copy task contents into ``dst`` (which is the per-task workdir)."""
    if dst.exists():
        # Fresh start for this workdir.
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=_COPY_IGNORE)
    # Ensure output/ exists so solve.sh can write there.
    (dst / "output").mkdir(exist_ok=True)


def _stage_skill_source(
    bench_root: Path,
    skill_name: str,
    seed_filename: str,
    tmp_root: Path,
) -> Path:
    """Materialize a clean skill source dir whose top-level ``SKILL.md`` is
    the chosen seed. Returns the path passed to ``SkillStore.import_skill``.

    Copies any sibling assets (``scripts/`` / ``references/`` / ``assets/``)
    from the canonical skill dir so import doesn't lose them.
    """
    src_dir = bench_root / "skills" / skill_name
    if not src_dir.is_dir():
        raise FileNotFoundError(f"No canonical skill dir at {src_dir}")
    seed_path = src_dir / seed_filename
    if not seed_path.is_file():
        # Fall back to SKILL.md if the caller asked for HANDCRAFT but it
        # isn't present; surface explicitly.
        raise FileNotFoundError(
            f"Seed file {seed_filename!r} not found in {src_dir} "
            f"(use --seed-file SKILL.md if no handcraft variant exists)"
        )

    stage = tmp_root / skill_name
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed_path, stage / "SKILL.md")
    for sub in ("scripts", "references", "assets"):
        sub_src = src_dir / sub
        if sub_src.is_dir() and any(sub_src.iterdir()):
            shutil.copytree(sub_src, stage / sub)
    return stage


def _deprecate_existing(store_root: Path, skill_name: str) -> Path | None:
    """If ``<store_root>/<skill_name>`` exists, rename it. Returns the new path."""
    current = store_root / skill_name
    if not current.exists():
        return None
    deprecated = store_root / f"{skill_name}-deprecated-on-{_deprecation_suffix()}"
    # Don't clobber an earlier deprecation done within the same minute.
    if deprecated.exists():
        suffix = 2
        while True:
            cand = store_root / f"{skill_name}-deprecated-on-{_deprecation_suffix()}-{suffix}"
            if not cand.exists():
                deprecated = cand
                break
            suffix += 1
    current.rename(deprecated)
    return deprecated


def _store_root_for(store: SkillStore) -> Path:
    """Return the project-scope scan root the store will write into."""
    # SkillStore._scan_roots[0] is the project root (when project_dir is set).
    roots = getattr(store, "_scan_roots", [])
    if not roots:
        raise RuntimeError("SkillStore has no scan roots configured")
    return roots[0]


def _write_skill_into_workdir(
    store: SkillStore,
    skill_name: str,
    workdir: Path,
) -> None:
    """Render the active skill body into ``<workdir>/skills/<name>/SKILL.md``."""
    skill = store.get(skill_name)
    dst_dir = workdir / "skills" / skill_name
    dst_dir.mkdir(parents=True, exist_ok=True)
    # Use parser-aware write so frontmatter round-trips correctly.
    from evolution.core.parser import write_skill_md

    write_skill_md(dst_dir / "SKILL.md", skill.frontmatter, skill.body)


# ── public API ──────────────────────────────────────────────────────────────


def prepare_task(
    role: str,
    task_id: str,
    *,
    bench_root: Path | str = DEFAULT_BENCH_ROOT,
    workdir: Path | str | None = None,
    seed_filename: str = "SKILL.md",
    store: SkillStore | None = None,
) -> PrepResult:
    """Prepare a safe-copy task workspace ready for solve.sh / pytest.

    Args:
        role: Benchmark role (``de`` / ``ds`` / ``genai`` / ``infra`` / ``pm``
            / ``swe``).
        task_id: Task directory name (e.g. ``sales-pivot-analysis``).
        bench_root: sb-bench checkout root.
        workdir: Per-task workspace. Defaults to
            ``<this-script>/runs/<UTC-ts>/<role>/<task>/``.
        seed_filename: Which file under ``<bench>/skills/<name>/`` provides
            the v0 body. Default ``"SKILL.md"``; opt in to
            ``"SKILL_HANDCRAFT.md"`` when needed.
        store: Optional pre-built ``SkillStore``. If omitted, one is built
            against the evolution repo (``<EVO_REPO>/.agents/skills/``).
    """
    bench_root = Path(bench_root).resolve()
    workdir = Path(workdir).resolve() if workdir is not None else _default_workdir(role, task_id)

    if role not in ROLE_PYTHON_MAP:
        raise ValueError(f"Unknown role {role!r}; expected one of {sorted(ROLE_PYTHON_MAP)}")

    src = _resolve_task_source(bench_root, role, task_id)
    toml = _read_task_toml(src)
    task_block = toml.get("task", {})
    declared_skills: list[str] = list(task_block.get("skills", []))
    timeout_sec = int(
        toml.get("verification", {}).get("timeout_seconds")
        or task_block.get("timeout_seconds")
        or 300
    )

    # 1. Copy task tree to safe workspace.
    _copy_task_tree(src, workdir)

    # 2. Set up the evo store and register each declared skill fresh.
    skill_store: SkillStore = store if store is not None else SkillStore(project_dir=str(EVO_REPO))
    store_root = _store_root_for(skill_store)

    # We stage skill sources under a temp tree co-located with the workdir
    # so the on-disk state is self-contained (no /tmp surprises across runs).
    stage_root = workdir / "_skill_seed"
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True)

    registered: list[str] = []
    deprecated_paths: list[Path] = []
    for skill_name in declared_skills:
        # 2a. Move aside any existing lineage.
        deprecated = _deprecate_existing(store_root, skill_name)
        if deprecated is not None:
            deprecated_paths.append(deprecated)
        # 2b. Stage a fresh source with the chosen seed as SKILL.md.
        stage_dir = _stage_skill_source(bench_root, skill_name, seed_filename, stage_root)
        # 2c. Re-discover so import_skill sees the cleared slot.
        skill_store.discover()
        # 2d. Register as v1 in evo (conceptual "v0 from the benchmark").
        # Force the destination dir name to match the bench-declared skill name
        # — handcraft frontmatter sometimes carries an unrelated upstream name
        # (e.g. SKILL_HANDCRAFT.md inside ``pipelines/`` declares
        # ``name: data-pipeline``). Without this, the dir/name we deprecated
        # would stay vacant.
        skill_store.import_skill(stage_dir, name=skill_name)
        # 2e. Write the active body into the task workspace so the runner finds it.
        _write_skill_into_workdir(skill_store, skill_name, workdir)
        registered.append(skill_name)

    # Clean up the staging dir; the store keeps its own copy.
    shutil.rmtree(stage_root, ignore_errors=True)

    # 3. A tiny prep manifest for downstream inspection.
    manifest = {
        "role": role,
        "task_id": task_id,
        "bench_root": str(bench_root),
        "task_source": str(src),
        "workdir": str(workdir),
        "seed_filename": seed_filename,
        "skills_registered": registered,
        "skills_deprecated": [str(p) for p in deprecated_paths],
        "timeout_sec": timeout_sec,
        "role_python": ROLE_PYTHON_MAP[role],
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    (workdir / "_prep_manifest.json").write_text(json.dumps(manifest, indent=2))

    return PrepResult(
        workdir=workdir,
        role=role,
        task_id=task_id,
        role_python=ROLE_PYTHON_MAP[role],
        skills_registered=registered,
        deprecated_skill_dirs=deprecated_paths,
        timeout_sec=timeout_sec,
    )


def clean_task_outputs(workdir: Path) -> None:
    """Wipe ``output/`` and any stale ``solution.py`` before a new attempt."""
    workdir = Path(workdir)
    out_dir = workdir / "output"
    if out_dir.is_dir():
        for child in out_dir.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
            else:
                shutil.rmtree(child)
    sol = workdir / "solution.py"
    if sol.is_file():
        sol.unlink()


def run_solve(
    workdir: Path,
    python_exe: str,
    *,
    timeout: int = 300,
) -> tuple[bool, str, float]:
    """Run ``solve.sh`` patched to use ``python_exe``. Returns (ok, output, elapsed)."""
    workdir = Path(workdir)
    solve_sh = workdir / "solve.sh"
    if not solve_sh.is_file():
        return False, f"solve.sh not found in {workdir}", 0.0
    patched = solve_sh.read_text().replace("python3", python_exe)
    start = time.time()
    try:
        result = subprocess.run(
            ["bash", "-c", patched],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        if result.returncode == 0:
            return True, result.stdout, elapsed
        return (
            False,
            f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            elapsed,
        )
    except subprocess.TimeoutExpired:
        return False, f"solve.sh timed out after {timeout}s", time.time() - start


def _parse_pytest_summary(text: str) -> tuple[int, int, int]:
    import re

    summary = text.strip().splitlines()[-1] if text.strip() else ""
    passed = (
        int((re.search(r"(\d+)\s+passed", summary, re.I) or [0, 0])[1])
        if re.search(r"(\d+)\s+passed", summary, re.I)
        else 0
    )
    failed = (
        int((re.search(r"(\d+)\s+failed", summary, re.I) or [0, 0])[1])
        if re.search(r"(\d+)\s+failed", summary, re.I)
        else 0
    )
    errors = (
        int((re.search(r"(\d+)\s+errors", summary, re.I) or [0, 0])[1])
        if re.search(r"(\d+)\s+errors", summary, re.I)
        else 0
    )
    total = passed + failed + errors
    if total:
        return total, passed, failed
    # Fallback: count " PASSED" / " FAILED" / " ERROR" lines verbatim.
    return (
        text.count(" PASSED") + text.count(" FAILED") + text.count(" ERROR"),
        text.count(" PASSED"),
        text.count(" FAILED"),
    )


def run_pytest(
    workdir: Path,
    python_exe: str,
    *,
    timeout: int = 300,
    test_file: str = "tests/test_outputs.py",
) -> tuple[int, int, int, str, float]:
    """Run pytest with TASK_WORKSPACE set. Returns (collected, passed, failed, output, elapsed)."""
    workdir = Path(workdir)
    env = {**os.environ, "TASK_WORKSPACE": str(workdir)}
    env["PATH"] = os.path.dirname(python_exe) + os.pathsep + env.get("PATH", "")
    start = time.time()
    try:
        result = subprocess.run(
            [
                python_exe,
                "-m",
                "pytest",
                test_file,
                "-v",
                "--tb=short",
                "--capture=no",
                "--color=no",
            ],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.time() - start
        collected, passed, failed = _parse_pytest_summary(result.stdout)
        return collected, passed, failed, result.stdout, elapsed
    except subprocess.TimeoutExpired:
        return 0, 0, 0, f"pytest timed out after {timeout}s", time.time() - start


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("role", help="Role (de / ds / genai / infra / pm / swe)")
    p.add_argument("task_id", help="Task directory name, e.g. sales-pivot-analysis")
    p.add_argument(
        "--bench-root",
        type=Path,
        default=DEFAULT_BENCH_ROOT,
    )
    p.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help=("Per-task workspace. Defaults to runs/<UTC-ts>/<role>/<task>/ next to this script."),
    )
    p.add_argument(
        "--seed-file",
        choices=("SKILL.md", "SKILL_HANDCRAFT.md"),
        default="SKILL.md",
        help="Source file inside <bench>/skills/<name>/ used as v0 body.",
    )
    p.add_argument(
        "--solution",
        type=Path,
        default=None,
        help="If given, copy this solution.py into the workdir and run solve.sh + pytest.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-step timeout (seconds) for solve.sh and pytest.",
    )
    args = p.parse_args()

    print(f"Preparing {args.role}/{args.task_id} ...")
    result = prepare_task(
        args.role,
        args.task_id,
        bench_root=args.bench_root,
        workdir=args.workdir,
        seed_filename=args.seed_file,
    )

    print(f"  workdir         : {result.workdir}")
    print(f"  role python     : {result.role_python}")
    print(f"  skills (fresh)  : {result.skills_registered or '<none>'}")
    if result.deprecated_skill_dirs:
        print("  deprecated lineage:")
        for d in result.deprecated_skill_dirs:
            print(f"    {d}")
    print(f"  task timeout    : {result.timeout_sec}s")

    if args.solution is None:
        return 0

    if not args.solution.is_file():
        print(f"!! --solution {args.solution} not found", file=sys.stderr)
        return 2

    print(f"\nDropping solution: {args.solution}")
    shutil.copy2(args.solution, result.workdir / "solution.py")

    print("Running solve.sh ...")
    ok, out, dt = run_solve(result.workdir, result.role_python, timeout=args.timeout)
    print(f"  solve.sh: {'OK' if ok else 'FAIL'} in {dt:.1f}s")
    if not ok:
        (result.workdir / "_solve_output.txt").write_text(out)
        print(f"  output captured to {result.workdir / '_solve_output.txt'}")

    print("Running pytest ...")
    collected, passed, failed, out, dt = run_pytest(
        result.workdir, result.role_python, timeout=args.timeout
    )
    (result.workdir / "_pytest_output.txt").write_text(out)
    if collected:
        print(f"  pytest: {passed}/{collected} passed ({failed} failed) in {dt:.1f}s")
    else:
        print("  pytest: no tests collected (see _pytest_output.txt)")

    return 0 if (collected and passed == collected) else 1


if __name__ == "__main__":
    sys.exit(main())
