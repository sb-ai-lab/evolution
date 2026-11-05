"""H12b Task 9: `evo tree evolve` renders family-14 results and suspensions.

The command admits a native `/15` fixture manifest, with
`family14_admitted` marking only the connection probe for scripted faults,
and prints what `evolve` returned. `evolve` is replaced by a function that
returns a `CampaignResult` captured beforehand from a genuine family-14
engine invocation through `tests.family14_public.family14_public_run`, so
the CLI renders real `result`/`status` documents: a finished run, and a
genuine `connection_incompatible` suspension (H10's `event_missing` probe).
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evolution.tree import evolve as evolve_module
from evolution.tree.campaign import CampaignResult
from evolution.tree.cli import tree_app
from tests.campaign_fakes import family14_admitted
from tests.family14_public import family14_public_run
from tests.test_tree_campaign_family14_connection_run import PROBE_MARK

pytestmark = pytest.mark.slow

INCOMPATIBLE = (
    "the connection probe found the Target or Scorer incompatible: connection_event_missing "
    "at scorer.metric_contract.target_fields[0] (observed "
    "events[event=assistant_message,last]/content); fix the Target or the Scorer, then run "
    "the same command (a changed Scorer or metric_contract is a comparable change: accept "
    "it with evo tree authorize)"
)


def _finished(root: Path, monkeypatch: pytest.MonkeyPatch) -> CampaignResult:
    with monkeypatch.context() as patch:
        _, result = asyncio.run(family14_public_run(root, patch))
    assert result.terminal == "finished"
    return result


def _incompatible(root: Path, monkeypatch: pytest.MonkeyPatch) -> CampaignResult:
    """A genuine run whose connection probe sees no `assistant_message` event."""

    from tests.campaign_drivers import EDITED
    from tests.test_tree_campaign_family14_connection_run import (
        ConnectedAdapter,
        _script,
        probe_scorer,
    )
    from tests.test_tree_campaign_family14_inspection import _connected

    adapter = ConnectedAdapter(used=EDITED["used"])
    assert _script(adapter, "event_missing") == {}
    with monkeypatch.context() as patch:
        _connected(patch)
        _, result = asyncio.run(
            family14_public_run(root, patch, adapter=adapter, scorer=probe_scorer(None))
        )
    assert (result.terminal, result.code) == ("suspended", "connection_incompatible")
    return result


def _manifest(root: Path) -> Path:
    """A `/15` manifest: `v15_from` over the family-12 fixture document."""

    from tests.campaign_drivers import two_case_manifest as family12_manifest
    from tests.tree.manifest.test_manifest_v15 import v15_from

    path = family12_manifest(root)
    path.write_text(json.dumps(v15_from(json.loads(path.read_text()))))
    return path


def _invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: CampaignResult, *options: str
) -> Any:
    family14_admitted(monkeypatch)

    async def evolved(*args: Any, **kwargs: Any) -> CampaignResult:
        return result

    monkeypatch.setattr(evolve_module, "evolve", evolved)
    manifest = _manifest(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir), *options],
    )
    assert invocation.exception is None or isinstance(invocation.exception, SystemExit)
    assert "Traceback" not in invocation.output
    invocation.paths = (manifest, run_dir)
    return invocation


#: Family 10's status line: every family-14 status carries `resilience`, so
#: it closes stderr whatever the terminal (role resilience spec §6.3).
RESILIENCE = "evo tree evolve: Skipped rounds: 0 · Continuation turns: 0, led to an edit: 0"


def _stderr(invocation: Any) -> list[str]:
    return invocation.stderr.splitlines()


def test_a_finished_run_prints_its_family_14_documents_and_the_continue_line(tmp_path, monkeypatch):
    result = _finished(tmp_path / "source", monkeypatch)
    invocation = _invoke(tmp_path, monkeypatch, result)

    assert invocation.exit_code == 0, invocation.output
    document = json.loads(invocation.stdout)
    assert (document["terminal"], document["finished"]["outcome"]) == ("finished", "completed")
    assert document["result"] == result.result.to_payload()
    assert document["status"] == result.status.to_payload()
    for name in ("result", "status"):
        projection = document[name]
        assert projection["connection"]["state"] == "compatible"
        assert projection["roles"]["grouper"] == {"kind": "fields"}
        assert projection["settings"]["baseline_refresh"] == "off"
        assert all("final" in entry and "cohort" not in entry for entry in projection["rounds"])
    _, run_dir = invocation.paths
    champion = document["status"]["campaign_outcome"]["champion_path"]
    absolute = Path(run_dir).resolve()
    # The successor keys and family 10's line; no connection line (ruling Q2).
    assert _stderr(invocation) == [
        f"evo tree evolve: continue: set linked_from to {absolute} and seed_root to "
        f"{absolute}/{champion} (the target must allow trees under it)",
        RESILIENCE,
    ]
    assert "connection" not in invocation.stderr


def test_a_connection_incompatibility_prints_its_sentence_and_the_continue_line(
    tmp_path, monkeypatch
):
    result = _incompatible(tmp_path / "source", monkeypatch)
    invocation = _invoke(tmp_path, monkeypatch, result)

    assert invocation.exit_code == 1, invocation.output
    document = json.loads(invocation.stdout)
    assert (document["terminal"], document["code"]) == ("suspended", "connection_incompatible")
    assert document["result"] is None
    connection = document["status"]["connection"]
    assert (connection["state"], connection["outcome"]) == ("suspended", "connection_incompatible")
    manifest, run_dir = invocation.paths
    assert _stderr(invocation) == [
        f"evo tree evolve: suspended: connection_incompatible — {INCOMPATIBLE}",
        "evo tree evolve: continue: evo tree evolve --manifest "
        f"{shlex.quote(str(manifest))} --run-dir {shlex.quote(str(run_dir))}",
        RESILIENCE,
    ]
    # The probe's observed value is printed nowhere (C spec §3.2).
    assert PROBE_MARK not in invocation.stderr
    assert PROBE_MARK not in invocation.stdout


@pytest.mark.parametrize("case", ["finished", "incompatible"])
def test_quiet_prints_the_three_keys(tmp_path, monkeypatch, case):
    capture = _finished if case == "finished" else _incompatible
    result = capture(tmp_path / "source", monkeypatch)
    invocation = _invoke(tmp_path, monkeypatch, result, "--quiet")

    document = json.loads(invocation.stdout)
    assert set(document) == {"terminal", "finished", "code"}
    assert document["terminal"] == result.terminal
    assert invocation.exit_code == (0 if case == "finished" else 1)
    lines = _stderr(invocation)
    if case == "incompatible":
        assert lines[0] == f"evo tree evolve: suspended: connection_incompatible — {INCOMPATIBLE}"
        assert lines[1].startswith("evo tree evolve: continue: evo tree evolve --manifest ")
        assert lines[2:] == [RESILIENCE]
    else:
        assert lines[0].startswith("evo tree evolve: continue: set linked_from to ")
        assert lines[1:] == [RESILIENCE]
    assert PROBE_MARK not in invocation.stderr
