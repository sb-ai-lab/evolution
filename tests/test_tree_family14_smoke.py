"""The scaffolded `/15` demo end to end on the reference target (H12c, A§10.9).

From `2.0.0b1` `evo-target init` writes `evolution-v2-manifest/15` with the
recommended values (PDR-0028, R72). These tests run that manifest exactly as
`scaffold()` writes it -- only the target's port, which is ephemeral here, and
the programs' interpreter, which is this test's own, change -- with no test
seam: no `family14_admitted`, no profile. They hold the outcome QUICKSTART
promises (round 1 promotes, round 2 closes clean, Champion `v1`, exit 0), the
reopening of a finished run (Review Focus 3), and the predicates the preview
rehearsal's checks 8, 13 and 14 assert on the same three campaigns, so the
rehearsal's caveat constants cannot lag the product (H12c choice 8).

What they prove is the wiring of the public command over the scaffold. They
prove nothing about the quality of a Promotion.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.evolve import evolve, exit_code_for
from tests.campaign_drivers import _Workspace, audit_mismatches, result_payload

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v2_preview_rehearsal", ROOT / "scripts" / "v2_preview_rehearsal.py"
)
assert SPEC and SPEC.loader
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)


def point_scaffold_at(manifest: Path, *, port: int) -> None:
    """Point the scaffold's `/15` manifest at `port` and this interpreter; nothing else."""

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["schema"] == "evolution-v2-manifest/15"
    document["target"]["base_url"] = f"http://127.0.0.1:{port}"
    for role in ("scorer", "editor", "reflector"):
        document[role]["argv"][0] = sys.executable
    manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """The scaffold's two-case `/15` workspace, served on a loopback port."""

    served = _Workspace(tmp_path)
    try:
        point_scaffold_at(served.manifest, port=served.port)
        yield served.manifest
    finally:
        served.close()


async def test_the_scaffold_runs_a_15_campaign_as_quickstart_says(workspace: Path) -> None:
    run_dir = workspace.parent / "run-1"
    said: list[str] = []
    result = await evolve(workspace, run_dir, notes=said.append)
    payload = result_payload(result)
    assert result.terminal == "finished" and exit_code_for(result) == 0
    assert result.finished is not None
    assert result.finished["outcome"] == "completed"
    assert result.finished["reason"] == "rounds_exhausted"
    assert [(r["round"], r["outcome"]) for r in payload["rounds"]] == [
        (1, "promoted"),
        (2, "clean"),
    ]
    champion = payload["final_champion"]
    assert champion["version"] == "v1" and champion["baseline_origin"] == "promoted_attempt"
    assert champion["measurement_complete"] is True
    assert champion["measurement_id"] == "m-r01-final-01-a01"
    assert payload["settings"] == {
        "stability": "stable",
        "round_stop": "first_accepted",
        "baseline_refresh": {"every_rounds": 3, "count": 1, "performed": []},
    }
    assert payload["roles"]["grouper"] == {"kind": "fields"}
    assert payload["connection"]["state"] == "compatible"
    assert not [line for line in said if "deprecated" in line]
    assert tuple(c["kind"] for c in payload["caveats"]) == rehearsal.CAMPAIGN_CAVEAT_KINDS
    assert audit_mismatches(run_dir) == []
    skill = run_dir / "artifacts" / "champions" / "v1" / "skills" / "SKILL.md"
    assert skill.read_text(encoding="utf-8").startswith("IMPROVED")


async def test_the_scaffold_reopens_a_finished_15_run_unchanged(workspace: Path) -> None:
    """Review Focus 3: the same command on the finished run prints the same result, writes nothing."""

    run_dir = workspace.parent / "run-1"
    first = await evolve(workspace, run_dir)
    assert first.terminal == "finished"
    before = rehearsal.fingerprint(run_dir)

    again = await evolve(workspace, run_dir)

    assert again.terminal == "finished" and exit_code_for(again) == 0
    assert again.finished == first.finished
    assert result_payload(again) == result_payload(first)
    assert rehearsal.fingerprint(run_dir) == before


async def test_the_rehearsal_successors_behave_as_its_checks_say(workspace: Path) -> None:
    """Checks 14 and 15 of the preview rehearsal, in process, over the same scaffold."""

    demo = workspace.parent
    run_dir = demo / "run-1"
    first = await evolve(workspace, run_dir)
    assert first.terminal == "finished" and exit_code_for(first) == 0
    predecessor = rehearsal.fingerprint(run_dir)
    champion_path = first.status.to_payload()["campaign_outcome"]["champion_path"]
    assert isinstance(champion_path, str) and champion_path.startswith("artifacts/champions/")

    def successor(baseline: str, name: str) -> Path:
        document = rehearsal.successor_manifest(
            json.loads(workspace.read_text(encoding="utf-8")),
            run_dir=run_dir,
            champion_path=champion_path,
            baseline=baseline,
        )
        path = demo / name
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    # Check 14: a `fresh` successor finishes and starts at round 3.
    linked_dir = demo / "run-2"
    linked = await evolve(successor("fresh", "manifest-linked.json"), linked_dir)
    assert linked.terminal == "finished" and exit_code_for(linked) == 0
    linked_from = result_payload(linked)["linked_from"]
    assert linked_from is not None and linked_from["first_round"] == 3
    assert audit_mismatches(linked_dir) == []
    assert rehearsal.fingerprint(run_dir) == predecessor

    # Check 15: an `inherit` successor carries the whole Champion measurement.
    inherit_dir = demo / "run-3"
    inheriting = await evolve(successor("inherit", "manifest-inherit.json"), inherit_dir)
    assert inheriting.terminal == "finished" and exit_code_for(inheriting) == 0
    caveats: list[dict[str, Any]] = result_payload(inheriting)["caveats"]
    carried = next((c for c in caveats if c["kind"] == "baseline_inherited"), None)
    assert carried is not None, caveats
    assert carried["imported"] == carried["scheduled"] and carried["imported"] > 0
    assert carried["fields"] == []
    assert carried["origin_runs"] == 1
    assert tuple(c["kind"] for c in caveats) == rehearsal.INHERIT_CAVEAT_KINDS
    assert audit_mismatches(inherit_dir) == []
    assert rehearsal.fingerprint(run_dir) == predecessor
