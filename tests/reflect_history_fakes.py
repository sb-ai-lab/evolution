"""Synthetic family-14 closed rounds for the compact-history tests (#228).

Real published trees on disk and real `ArtifactRef`s (so `_verify_published_copies`
and `scan_tree` run for real), fake measurements and a fake store from
`tests/tree/roles/editor/test_editor_evidence.py`. No campaign runs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.campaign_schema import AttemptState, CampaignSnapshot, RoundState
from tests.campaign_fakes import PRIMARY, artifact
from tests.tree.roles.editor.test_editor_evidence import _row, _Rows, _snapshot, _Store

POLICY = ArtifactPolicyV1((), (), ())
CAMPAIGN_POLICY = SimpleNamespace(repeats=3, metric_primary=PRIMARY)


class World:
    """One synthetic run: published Parents/Candidates, measurements, rounds."""

    def __init__(self, root: Path, *, cases: int = 4, repeats: int = 3) -> None:
        self.root = root
        self.cases = [f"case-{k:03d}" for k in range(1, cases + 1)]
        self.repeats = repeats
        self.refs: dict[tuple[str, str], Any] = {}
        self.rows: dict[str, Any] = {}
        self.rounds: dict[int, RoundState] = {}
        self.champion = (
            "v0",
            self.publish(
                "champion", "v0", {"skills/a/SKILL.md": "seed\n", "skills/b/rules.md": "rule\n"}
            ),
        )

    def publish(
        self,
        kind: str,
        artifact_id: str,
        files: dict[str, str | bytes],
        *,
        modes: dict[str, int] | None = None,
    ) -> str:
        tree = self.root / kind / artifact_id
        for relative, content in files.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        tree.mkdir(parents=True, exist_ok=True)
        for relative, mode in (modes or {}).items():
            (tree / relative).chmod(mode)
        ref = artifact(tree, kind=kind, artifact_id=artifact_id)
        self.refs[(kind, artifact_id)] = ref
        return str(ref.digest)

    def measure(self, measurement_id: str, score: float | dict[str, float]) -> str:
        scores = score if isinstance(score, dict) else {case: score for case in self.cases}
        self.rows[measurement_id] = _Rows(
            tuple(
                _row(case, r, scores[case])
                for case in self.cases
                for r in range(1, self.repeats + 1)
            )
        )
        return measurement_id

    def add_round(
        self,
        number: int,
        attempts: list[AttemptState],
        *,
        outcome: str = "stalled",
        reason: str | None = None,
        selected: str | None = None,
        exam: list[str] | None = None,
        diagnosis: list[dict[str, Any]] | None = None,
        reflection_error: str | None = None,
        global_comparisons: dict[str, Any] | None = None,
    ) -> None:
        baseline = self.measure(f"m-r{number:02d}-baseline", 0.5)
        reflections: tuple[dict[str, Any], ...] = ()
        if diagnosis is not None or reflection_error is not None:
            reflections = (
                {
                    "reflection_id": f"r{number:02d}-x01",
                    "status": "failed" if reflection_error else "reflected",
                    "groups": diagnosis or [],
                    "error_class": reflection_error,
                    "probes": (),
                    **({"reason": reflection_error} if reflection_error else {}),
                },
            )
        self.rounds[number] = RoundState(
            round=number,
            baseline_measurement_id=baseline,
            baseline_complete=True,
            attempts={a.attempt_id: a for a in attempts},
            selection={"attempt_id": selected},
            closed={"outcome": outcome, **({"reason": reason} if reason else {})},
            reflections=reflections,
            gate_scope=None if exam is None else {"cases": exam},
            focus_portion={
                "reason": "selected",
                "case_ids": self.cases[:1],
                "cohort_ids": [],
                "cycle": 1,
            },
            global_comparisons=global_comparisons or {},
        )

    def attempt(
        self,
        number: int,
        index: int,
        files: dict[str, str | bytes] | None,
        *,
        parent: str = "v0",
        remove: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        outcome: str = "rejected",
        reason: str = "focus_below_margin",
        score: float | None = 0.5,
        **fields: Any,
    ) -> AttemptState:
        attempt_id = f"r{number:02d}-a{index:02d}"
        parent_digest = self.refs[("champion", parent)].digest
        digest = None
        if files is not None:
            base = {
                str(p.relative_to(self.refs[("champion", parent)].root)): p.read_bytes()
                for p in self.refs[("champion", parent)].root.rglob("*")
                if p.is_file()
            }
            for relative in remove:
                base.pop(relative, None)
            digest = self.publish("candidate", attempt_id, {**base, **files}, modes=modes)
        measurement = (
            None if score is None or digest is None else self.measure(f"m-{attempt_id}", score)
        )
        return AttemptState(
            attempt_id=attempt_id,
            round=number,
            index=index,
            parent_version=parent,
            parent_digest=parent_digest,
            editor=fields.pop("editor", "edited"),
            candidate_digest=digest,
            measurement_id=measurement,
            outcome=outcome,
            reason=reason,
            **fields,
        )

    def promote(self, attempt: AttemptState, version: str) -> None:
        """Publish a real Champion copy before using it as a later Parent."""
        root = self.refs[("candidate", attempt.attempt_id)].root
        files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        modes = {
            str(p.relative_to(root)): p.stat().st_mode & 0o777
            for p in root.rglob("*")
            if p.is_file()
        }
        self.publish("champion", version, files, modes=modes)

    def snapshot(self) -> CampaignSnapshot:
        return replace(_snapshot(self.rounds), family_version=14, adaptive=True)

    def store(self) -> _Store:
        return _Store(self.refs)

    def sources(self) -> dict[str, Any]:
        return {
            "measurements": self.rows,
            "store": self.store(),
            "artifact_policy": POLICY,
            "corpus": (),
            "policy": CAMPAIGN_POLICY,
        }
