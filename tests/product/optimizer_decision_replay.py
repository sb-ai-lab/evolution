#!/usr/bin/env python3
"""Decision-replay arm for the product optimizer, ``flat_paired_v1``.

Why this exists: the running pass-rate arm does not import
``evolution.evolve.optimizer``. So the optimizer that ships in the wheel is measured by
no arm at all. A package move could rewire its promote/reject behaviour and every
existing arm would stay green.

What this measures, and what it deliberately does not. It replays *decisions*, not
quality. Every model call is replaced by a deterministic fake, so the arm answers one
question exactly: **given identical evidence, does the optimizer still reach the same
verdicts, with the same gate reasons, in the same order?** It cannot detect a quality
regression; the pass-rate arm does that, and costs a day of wall clock to say so. This
costs seconds and is the right shape for the merge's "no semantic changes" claim.

Why the fakes sit at ``_collect_one`` / ``_reflect_round`` / ``_rewrite_round`` /
``_validate_one`` rather than at the LLM backend: a backend fake still leaves solver
scheduling, workspace setup and pytest execution in the loop, none of which is a
decision, all of which is slow and environment-sensitive. Cutting at these four seams
keeps the whole decision path -- paired scheduling, the saturation preflight, the edit
brake, ``conservative_paired_gate``, version reservation and lineage promotion -- under
measurement, which is the part a rename can break.

**The determinism self-check is the load-bearing part.** Each scenario runs twice inside
one capture and the two normalised records must be identical. A denylist of volatile
fields rots silently: the day the optimizer adds a timestamp to a round record, a
denylist keeps reporting equality it is no longer entitled to. The double run catches a
new volatile field in the same command that introduced it, and needs no maintenance.
``round_id`` is redacted because it is known-volatile by construction (a run stamp plus
random hex); everything else is compared byte-for-byte, including seeds, content hashes
and full gate telemetry.

Usage::

    python tests/product/optimizer_decision_replay.py capture --out before.json
    python tests/product/optimizer_decision_replay.py verify --ref before.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.core.store import SkillStore
from evolution.evolve import optimizer
from evolution.evolve.optimizer import optimize_skill
from evolution.evolve.optimizer_validation import ValidationMeasurement
from evolution.evolve.rewrite_outcome import PROMOTED_ELIGIBLE, RewriteOutcome
from evolution.llm.mock_backend import MockBackend

SCHEMA = "evolution-optimizer-decision-replay-v1"

#: Keys whose values are volatile by construction and carry no decision.
REDACTED = ("round_id",)

_REDACTION = "<redacted>"


@dataclass(frozen=True)
class Scenario:
    """One pinned optimizer run, chosen to reach a distinct decision branch."""

    summary: str
    candidate_pass_rate: float = 1.0
    parent_pass_rate: float = 0.0
    saturation_pass_rate: float = 0.5
    rounds: int = 1
    trials: int = 4
    #: When set, the candidate wins only the first N paired seeds and loses the rest.
    #: This is how a positive-but-underpowered gain is produced without a live model.
    candidate_winning_pairs: int | None = None


SCENARIOS: dict[str, Scenario] = {
    "promoted": Scenario("clean paired win; expect promote and paired_improvement"),
    "regressed": Scenario(
        "candidate loses every pair; expect the parent kept",
        candidate_pass_rate=0.0,
        parent_pass_rate=1.0,
    ),
    "no_gain": Scenario(
        "candidate ties the parent; expect no observed gain",
        candidate_pass_rate=1.0,
        parent_pass_rate=1.0,
    ),
    "underpowered": Scenario(
        "candidate wins two pairs of four; expect inconclusive rather than promote",
        candidate_winning_pairs=2,
    ),
    "saturated_parent": Scenario(
        "parent already passes validation; expect the saturation preflight to stop the round",
        saturation_pass_rate=1.0,
    ),
    "two_rounds": Scenario(
        "two promoting rounds; locks the ordering of a decision sequence, not one verdict",
        rounds=2,
    ),
}


def _write_task(tasks: Path, task_id: str) -> None:
    task = tasks / task_id
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        'version = "1.0"\n'
        "[metadata]\n"
        f'name = "{task_id}"\n'
        'difficulty = "easy"\n'
        'category = "test"\n'
        "[skills]\n"
        'required = ["target-id"]\n'
        "[evaluation]\n"
        'test_file = "tests/test_outputs.py"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Write output.txt.", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(
        "def test_output(): assert True\n", encoding="utf-8"
    )


def make_project(root: Path) -> tuple[Path, Path]:
    """A minimal skill and a train/validate task pair, built fresh under *root*."""
    project = root / "project"
    tasks = root / "tasks"
    SkillStore(project_dir=project).create("target-id", "Optimizer target")
    _write_task(tasks, "train")
    _write_task(tasks, "validate")
    return project, tasks


def _eligible_rewrite(skill: Any) -> RewriteOutcome:
    """A rewrite the edit brake admits, differing from its parent by one line."""
    body = f"{skill.body}\n\ncandidate\n"
    return RewriteOutcome(
        body=body,
        candidate_status=PROMOTED_ELIGIBLE,
        reason="eligible",
        preservation=None,
        codex_usage=None,
        candidate_debug_dir=None,
        model_id="editor",
        parent_body=skill.body,
        candidate_body_raw=body,
    )


@contextmanager
def _execution_fakes(scenario: Scenario) -> Iterator[list[tuple[str, int]]]:
    """Replace the four model-facing seams for the duration of one run.

    The saved-and-restored form matters: this module is imported by a test that also
    runs the real optimizer elsewhere in the same interpreter, so leaking a fake would
    turn an unrelated failure into a mystery.
    """
    originals = {
        name: getattr(optimizer, name)
        for name in ("_collect_one", "_reflect_round", "_rewrite_round", "_validate_one")
    }
    order: list[tuple[str, int]] = []
    winning: set[int] = set()
    if scenario.candidate_winning_pairs is not None:
        pairs = optimizer.paired_schedule(
            ["validate"], trials=scenario.trials, base_seed=0, round_index=0
        )
        winning = {pair.seed for pair in pairs[: scenario.candidate_winning_pairs]}

    def validate(
        *, arm: str, seed: int, phase: str = "promotion_validation", **_kwargs: Any
    ) -> ValidationMeasurement:
        if phase == "validation_saturation":
            rate = scenario.saturation_pass_rate
            return ValidationMeasurement(seed=seed, passed=int(rate * 2), total=2, pass_rate=rate)
        if phase == "candidate_train_diagnostic":
            return ValidationMeasurement(seed=seed, passed=1, total=1, pass_rate=1.0)
        order.append((arm, seed))
        if scenario.candidate_winning_pairs is not None:
            rate = 1.0 if (arm == "candidate" and seed in winning) else 0.0
        else:
            rate = scenario.candidate_pass_rate if arm == "candidate" else scenario.parent_pass_rate
        return ValidationMeasurement(seed=seed, passed=int(rate), total=1, pass_rate=rate)

    optimizer._collect_one = lambda **kwargs: f"trace-{kwargs['trial']}"
    optimizer._reflect_round = lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}]
    optimizer._rewrite_round = lambda skill, **_kwargs: _eligible_rewrite(skill)
    optimizer._validate_one = validate
    try:
        yield order
    finally:
        for name, value in originals.items():
            setattr(optimizer, name, value)


def _redact(value: Any) -> Any:
    """Replace known-volatile values, recursively, leaving every decision intact."""
    if isinstance(value, dict):
        return {
            key: _REDACTION if key in REDACTED else _redact(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def run_scenario(name: str) -> dict[str, Any]:
    """Execute one scenario in a throwaway tree and return its normalised record."""
    scenario = SCENARIOS[name]
    root = Path(tempfile.mkdtemp(prefix="evo-replay-"))
    try:
        project, tasks = make_project(root)
        with _execution_fakes(scenario) as order:
            result = optimize_skill(
                "target-id",
                train_tasks=["train"],
                validate_tasks=["validate"],
                solver_model="fake",
                project_dir=project,
                tasks_dir=tasks,
                solver_backend=MockBackend(),
                editor_backend=object(),  # type: ignore[arg-type]
                rounds=scenario.rounds,
                trials=scenario.trials,
            )
        return {
            "summary": scenario.summary,
            "status": result.status,
            "reason": result.reason,
            "recommendation": result.recommendation,
            "initial_version": result.initial_version,
            "final_version": result.final_version,
            "validation_order": [list(pair) for pair in order],
            "rounds": _redact(result.rounds),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def capture() -> dict[str, Any]:
    """Every scenario, each run twice, with determinism asserted before returning.

    Running twice here rather than trusting a redaction list is the whole design: an
    equality claim that has not been tested against a second execution is a claim about
    one execution.
    """
    scenarios: dict[str, Any] = {}
    for name in SCENARIOS:
        first = run_scenario(name)
        second = run_scenario(name)
        if first != second:
            raise SystemExit(
                f"scenario {name!r} is not deterministic across two runs in one process; "
                "a new volatile field has entered the round record, and every replay "
                "verdict is meaningless until it is redacted or removed"
            )
        scenarios[name] = first
    return {"schema": SCHEMA, "scenarios": scenarios}


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")
    else:
        yield path, value


def compare(reference: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Every divergence between two captures, as leaf paths a reader can act on."""
    if reference.get("schema") != current.get("schema"):
        return [f"schema: {reference.get('schema')!r} -> {current.get('schema')!r}"]
    old = dict(_walk(reference.get("scenarios", {})))
    new = dict(_walk(current.get("scenarios", {})))
    divergences = [f"removed: {path}" for path in sorted(set(old) - set(new))]
    divergences += [f"added: {path}" for path in sorted(set(new) - set(old))]
    divergences += [
        f"changed: {path}: {old[path]!r} -> {new[path]!r}"
        for path in sorted(set(old) & set(new))
        if old[path] != new[path]
    ]
    return divergences


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("capture", help="record the decision sequence")
    capture_parser.add_argument("--out", required=True, type=Path)
    verify_parser = sub.add_parser("verify", help="compare against a recorded capture")
    verify_parser.add_argument("--ref", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture":
        payload = capture()
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"captured {len(payload['scenarios'])} scenarios -> {args.out}")
        return 0

    reference = json.loads(args.ref.read_text(encoding="utf-8"))
    divergences = compare(reference, capture())
    if not divergences:
        print(f"decision replay: identical to {args.ref}")
        return 0
    print(f"decision replay: {len(divergences)} divergence(s) against {args.ref}", file=sys.stderr)
    for line in divergences:
        print(f"  {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
