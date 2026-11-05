"""H12c Task 7: every promised command on a family-14 run (Review Focus 5;
PDR-0026 amendment 2026-10-12).

Two runs, each read through the real CLI (`evolution.cli.main.app` under
`typer.testing.CliRunner`):

* a finished `/15` run: the scaffold's workspace exactly as `evo-target init`
  writes it (only the port and the programs' interpreter change,
  `point_scaffold_at`), served by the reference target over HTTP, and run by
  `evo tree evolve` itself;
* a run suspended `connection_preflight_unavailable`: the H10 run module's
  `ConnectedAdapter` whose probe instance cannot be materialized
  (`connected_run`, case `materialize`). That driver calls
  `family14_admitted`, which marks the probe's task so the adapter can
  script the probe; it patches no admission gate.

The commands: `evo tree conformance`, the `evo tree evolve` reopen of the
finished run, `evo tree authorize --list`, `evo tree evaluate` (a train
evaluation of the finished run's Champion), `evo tree trace` and
`evo tree watch --once`. Each exits 0 and prints no traceback.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from evolution.cli.main import app
from tests.campaign_drivers import _Workspace, audit_mismatches
from tests.test_tree_family14_smoke import point_scaffold_at

pytestmark = pytest.mark.slow

RUNNER = CliRunner()


def _evo(*args: object) -> Result:
    result = RUNNER.invoke(app, [str(arg) for arg in args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert "Traceback" not in result.output
    return result


def _trace(run: Path) -> list[dict[str, Any]]:
    """`evo tree trace --out -`: valid `evolution-v2-trace/1` lines on stdout."""

    result = _evo("tree", "trace", "--run-dir", run, "--out", "-")
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    (header,) = [line for line in lines if line["record"] == "header"]
    assert header["schema"] == "evolution-v2-trace/1"
    assert (header["kind"], header["family"]) == ("campaign", 14)
    assert {line["record"] for line in lines} <= {"header", "try"}
    return lines


def _watch_once(run: Path) -> str:
    result = _evo("tree", "watch", "--run-dir", run, "--once")
    assert result.exit_code == 0, result.output
    assert "campaign created · family 14" in result.stdout
    return result.stdout


def _evaluation_manifest(campaign: Path, run_dir: Path, version: str) -> Path:
    """A train evaluation of `run_dir`'s Champion `version`, from the campaign
    manifest's own sections (as `tests/test_tree_train_test.py` builds one).

    The evaluation scorer section has no `metric_contract` (a `/15` key)."""

    original = json.loads(campaign.read_text(encoding="utf-8"))
    document = {
        name: original[name] for name in ("target", "artifacts", "environment", "scorer", "corpus")
    }
    document["scorer"] = {
        key: value for key, value in document["scorer"].items() if key != "metric_contract"
    }
    document.update(
        schema="evolution-v2-evaluation/1",
        snapshot={"run_dir": str(run_dir.relative_to(campaign.parent)), "version": version},
        split="train",
        repeats=1,
        random_seed=1,
        max_concurrency=1,
        max_scorer_concurrency=1,
        metric=original["campaign"]["metric"],
        budget={"max_turns": 2, "timeout_seconds": 20, "max_failed_turns": 1},
        sample_retries=0,
        call_retries={"max_attempts": 1, "turn_max_attempts": 1},
    )
    path = campaign.parent / "evaluation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def scaffold15(tmp_path: Path) -> Iterator[Path]:
    """The scaffold's `/15` workspace, served by the reference target on a loopback port."""

    served = _Workspace(tmp_path)
    try:
        point_scaffold_at(served.manifest, port=served.port)
        yield served.manifest
    finally:
        served.close()


def test_every_promised_command_reads_a_finished_family_14_run(scaffold15: Path) -> None:
    manifest = scaffold15
    demo = manifest.parent
    run = demo / "run-1"

    conformance = _evo("tree", "conformance", "--manifest", manifest)
    assert conformance.exit_code == 0, conformance.output
    assert json.loads(conformance.stdout)["ok"] is True

    command = ("tree", "evolve", "--manifest", manifest, "--run-dir", run)
    first = _evo(*command)
    assert first.exit_code == 0, first.output
    document = json.loads(first.stdout)
    assert document["terminal"] == "finished"
    assert document["result"]["connection"]["state"] == "compatible"
    assert "is deprecated" not in first.stderr
    again = _evo(*command)  # reopens the finished run
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout) == document

    # The finished run's own Champion, as its result document names it.
    champion = document["result"]["final_champion"]
    assert champion["baseline_origin"] == "promoted_attempt"
    version = champion["version"]
    evaluation = _evaluation_manifest(manifest, run, version)
    evaluated = _evo(
        "tree", "evaluate", "--manifest", evaluation, "--run-dir", demo / "evaluation-run"
    )
    assert evaluated.exit_code == 0, evaluated.output
    report = json.loads(evaluated.stdout)
    assert (report["state"], report["split"]) == ("completed", "train")
    assert report["origin"]["version"] == version

    tries = [line for line in _trace(run) if line["record"] == "try"]
    assert tries
    assert "finished: completed" in _watch_once(run)
    assert audit_mismatches(run) == []


def test_every_promised_command_reads_a_suspended_family_14_run(tmp_path, monkeypatch) -> None:
    from tests.campaign_drivers import EDITED
    from tests.test_tree_campaign_family14_connection_run import (
        ConnectedAdapter,
        _script,
        connected_run,
        payloads,
    )

    adapter = ConnectedAdapter(used=EDITED["used"])
    with monkeypatch.context() as patch:
        run, snapshot, _ = asyncio.run(
            connected_run(tmp_path, patch, adapter, **_script(adapter, "materialize"))
        )
    assert snapshot.finished is None
    (suspension,) = payloads(run, "campaign_suspended")
    assert suspension["reason"] == "connection_preflight_unavailable"

    # `--list` prints one JSON object per decision (docs/cli.md); a
    # connection suspension resumes by the same command, so there is none.
    listed = _evo("tree", "authorize", run, "--list")
    assert listed.exit_code == 0, listed.output
    assert listed.stdout == ""
    _trace(run)
    watched = _watch_once(run)
    assert "suspended: connection_preflight_unavailable (connection)" in watched
    assert audit_mismatches(run) == []
