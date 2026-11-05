"""Build the per-skill train/test subset manifest for the AFTER 4-way procmem run.

Selects single-skill AFTER tasks for a fixed skill set from the corpus manifest
(no hand-typed IDs): for each skill take the first N_TRAIN ``split=="train"`` and
N_TEST ``split=="test"`` tasks sorted by ``role/task_id``. Emits ``subset.json``
(sha-pinned handcraft seed) consumed by ``afterbench_smoke.py --subset/--skill``.

    python build_subset.py            # refuses if subset.json exists
    python build_subset.py --force    # archives the old subset.json first
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVO_ROOT = HERE.parents[1]  # .../evolution
WS_ROOT = EVO_ROOT.parent  # .../skill_evolutions
AFTER_ROOT = WS_ROOT / "datasets" / "AFTER"
MANIFEST = AFTER_ROOT / "tasks" / "manifest.json"
OUT = HERE / "subset.json"

N_TRAIN = 5
N_TEST = 3


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _select(manifest: list[dict], skill: str, split: str, n: int) -> list[str]:
    rows = [
        e
        for e in manifest
        if e.get("included", True)
        and list(e.get("skills") or []) == [skill]
        and e.get("split") == split
    ]
    rows.sort(key=lambda e: f"{e['role']}/{e['task_id']}")
    return [f"{e['role']}/{e['task_id']}" for e in rows[:n]]


def _all_single_skills(manifest: list[dict]) -> list[str]:
    skills = {
        (e.get("skills") or [None])[0]
        for e in manifest
        if e.get("included", True) and len(list(e.get("skills") or [])) == 1
    }
    return sorted(s for s in skills if s)


def _qualifies(manifest: list[dict], skill: str, n_train: int, n_test: int) -> bool:
    if not (AFTER_ROOT / "skills" / skill / "SKILL_HANDCRAFT.md").is_file():
        return False
    return (
        len(_select(manifest, skill, "train", n_train)) >= n_train
        and len(_select(manifest, skill, "test", n_test)) >= n_test
    )


def _check_task(rel: str) -> None:
    d = AFTER_ROOT / "tasks" / rel
    if not (d / "task.toml").is_file() or not (d / "tests" / "test_outputs.py").is_file():
        raise SystemExit(f"task missing task.toml or tests/test_outputs.py: {rel}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="archive + overwrite existing output")
    ap.add_argument(
        "--n-train", type=int, default=N_TRAIN, help=f"train tasks per skill (default {N_TRAIN})"
    )
    ap.add_argument(
        "--n-test", type=int, default=N_TEST, help=f"test tasks per skill (default {N_TEST})"
    )
    ap.add_argument(
        "--skills", default=None, help="comma list to restrict to (default: all qualifying)"
    )
    ap.add_argument("--out", default=str(OUT), help=f"output path (default {OUT.name})")
    args = ap.parse_args()

    n_train, n_test = args.n_train, args.n_test
    out_path = Path(args.out).resolve()
    want = [s.strip() for s in args.skills.split(",") if s.strip()] if args.skills else None

    if out_path.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {out_path} (use --force)")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    all_single = _all_single_skills(manifest)
    candidates = want if want is not None else all_single
    selected = [s for s in candidates if _qualifies(manifest, s, n_train, n_test)]
    skipped = [s for s in candidates if s not in selected]
    if skipped:
        print(f"skipped (no seed or <{n_train}train/{n_test}test): {', '.join(skipped)}")
    if want is not None and skipped:
        raise SystemExit(f"requested skills do not qualify: {', '.join(skipped)}")
    skills: dict[str, dict] = {}
    for skill in selected:
        train = _select(manifest, skill, "train", n_train)
        test = _select(manifest, skill, "test", n_test)
        if len(train) < n_train or len(test) < n_test:
            raise SystemExit(
                f"{skill}: insufficient tasks (train={len(train)}/{n_train} test={len(test)}/{n_test})"
            )
        if set(train) & set(test):
            raise SystemExit(f"{skill}: train/test overlap {set(train) & set(test)}")
        for rel in train + test:
            _check_task(rel)
        hc = AFTER_ROOT / "skills" / skill / "SKILL_HANDCRAFT.md"
        if not hc.is_file():
            raise SystemExit(f"{skill}: missing handcraft seed {hc}")
        skills[skill] = {
            "handcraft": f"skills/{skill}/SKILL_HANDCRAFT.md",
            "handcraft_sha256": _sha256(hc),
            "train": train,
            "test": test,
        }

    regen = f"python build_subset.py --n-train {n_train} --n-test {n_test}"
    if want is not None:
        regen += f" --skills {','.join(want)}"
    if out_path != OUT:
        regen += f" --out {out_path}"
    payload = {
        "schema": "after-procmem-4way-subset-v1",
        "corpus": {"root": "datasets/AFTER", "manifest": "datasets/AFTER/tasks/manifest.json"},
        "selection": {
            "single_skill_only": True,
            "n_train": n_train,
            "n_test": n_test,
            "sort": "role/task_id",
        },
        "skills": skills,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regen_command": regen,
    }

    if out_path.exists():
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path.rename(out_path.with_name(f"{out_path.stem}_{ts}.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for skill, blk in skills.items():
        print(f"{skill}: train={len(blk['train'])} test={len(blk['test'])}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
