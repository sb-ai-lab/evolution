"""ADR-0040 D7/D8 on the command line: one path-free stderr line names the storage fault."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from typer.testing import CliRunner

from evolution.tree.artifacts.artifact_errors import ArtifactCapacityError
from evolution.tree.campaign import CampaignResult
from evolution.tree.cli import tree_app
from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault
from tests.test_tree_surface_contract import _Projection
from tests.test_tree_train_test import evaluation_manifest
from tests.tree_evolve_support import write_workspace

DETAIL = (
    "scorer trace scorer-traces/scorer_traces/abc/stdout: ENOSPC (write); "
    "free space or inodes on the run directory's filesystem and run the same command again"
)


def test_evolve_prints_the_fault_detail_on_stderr_and_not_in_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = write_workspace(tmp_path / "ws")
    result = CampaignResult(
        terminal="storage_capacity_exhausted",
        quiescent=True,
        finished=None,
        owner="measurement_journal",
        code="storage_capacity_exhausted",
        result=None,
        status=cast(Any, _Projection({"run_id": "c-run"})),
        fault_detail=DETAIL,
    )

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(manifest.parent / "run")],
    )

    assert invocation.exit_code == 1, invocation.output
    assert f"evo tree evolve: {DETAIL}" in invocation.stderr.splitlines()
    assert "fault_detail" not in json.loads(invocation.stdout)
    assert DETAIL not in invocation.stdout


@pytest.mark.parametrize(
    ("raised", "exit_code", "lines"),
    [
        (
            ArtifactCapacityError(
                "storage_inodes_insufficient",
                detail="free inodes 5 are below the 40 this run needs before it measures",
            ),
            2,
            [
                "evo tree evaluate: free inodes 5 are below the 40 this run needs before it measures",
                "evo tree evaluate: refused: storage_inodes_insufficient",
            ],
        ),
        (
            ScorerTraceStorageFault("storage_capacity_exhausted", DETAIL),
            1,
            [f"evo tree evaluate: {DETAIL}", "evo tree evaluate: journal unavailable"],
        ),
    ],
)
def test_evaluate_prints_the_detail_line_before_its_existing_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    exit_code: int,
    lines: list[str],
) -> None:
    manifest = evaluation_manifest(tmp_path)

    async def fake_evaluate(*_args: object, **_kwargs: object) -> NoReturn:
        raise raised

    monkeypatch.setattr("evolution.tree.evaluation.evaluate", fake_evaluate)
    invocation = CliRunner().invoke(
        tree_app, ["evaluate", "--manifest", str(manifest), "--run-dir", str(tmp_path / "run")]
    )

    assert invocation.exit_code == exit_code, invocation.output
    assert invocation.stdout == ""
    assert invocation.stderr.splitlines() == lines
