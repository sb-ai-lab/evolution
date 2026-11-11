"""b18 Part B: scratch I/O moves change nothing durable.

`test_durable_facts_match_b17` compares `durable_facts(run)` of one fixed
`/15` and one fixed `/16` two-case run with fixtures captured before any
Part B production change (`tests/fixtures/b18/durable_facts_v15.json`,
`durable_facts_v16.json`). Later Part B tasks append tests here and reuse
`_run_v15` / `_run_v16`.

Both runs use the two-case driver's subprocess Reflector, whose
`control_prefix` (`.evolution`) makes the campaign call `project(...,
control_prefix=...)`, the seam Part B moves.

Fixture shape (a JSON object, keys sorted, no UUIDs, clocks or paths):
`events` (journal event -> count, without `instance_attested` and
`authorization_applied`), `measurements` (case-run directory name -> `paid`,
`imports`, `scores` as sorted `[sample_id, metrics-json]` pairs),
`decisions` (ordered `[event, outcome, reason]` triples) and `ledger`
(committed artifact event -> count).
`EVOLUTION_REGEN_B18_FACTS=1` rewrites the fixtures.
"""

import json
import os
from pathlib import Path

import pytest

from tests.campaign_drivers import two_case_run
from tests.family14_b2_scenario import journal_records
from tests.test_tree_campaign_family15_delivery import durable_facts, public_run

pytestmark = pytest.mark.slow

FIXTURES = Path(__file__).parent / "fixtures" / "b18"


def _shape(facts) -> dict:
    events, measurements, decisions, ledger = facts
    return {
        "events": dict(sorted(events.items())),
        "measurements": {
            name: {
                "paid": row["paid"],
                "imports": row["imports"],
                "scores": [list(score) for score in row["scores"]],
            }
            for name, row in sorted(measurements.items())
        },
        "decisions": [list(decision) for decision in decisions],
        "ledger": dict(sorted(ledger.items())),
    }


async def _run_v15(tmp_path, monkeypatch, **options):
    """A finished two-case `/15` run (subprocess Reflector, control prefix)."""

    return await two_case_run(tmp_path, **options)


async def _run_v16(tmp_path, monkeypatch, **options):
    """A finished two-case `/16` run (subprocess Reflector, control prefix)."""

    run, _result = await public_run(tmp_path, monkeypatch, **options)
    return run


@pytest.mark.parametrize("family", ["15", "16"])
async def test_durable_facts_match_b17(family, tmp_path, monkeypatch):
    build = _run_v15 if family == "15" else _run_v16
    run = await build(tmp_path, monkeypatch)
    actual = _shape(durable_facts(run))
    path = FIXTURES / f"durable_facts_v{family}.json"
    if os.environ.get("EVOLUTION_REGEN_B18_FACTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert actual == json.loads(path.read_text(encoding="utf-8"))


# -- Task B2: the Reflector's Parent projection lives in the default temporary directory --


def _scratch(tmp_path, monkeypatch) -> Path:
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


def _spy_project(monkeypatch):
    from evolution.tree import campaign as campaign_module

    calls: list[dict] = []
    original = campaign_module.project

    def project(workspace, **kwargs):
        projection = original(workspace, **kwargs)
        calls.append({**kwargs, "root": projection.root})
        return projection

    monkeypatch.setattr(campaign_module, "project", project)
    return calls


def _no_leftovers(scratch: Path, run: Path) -> None:
    assert list(scratch.glob("evo-tree-editor-*")) == []
    assert not (run / ".reflections").exists()


@pytest.mark.parametrize("family", ["15", "16"])
async def test_reflector_projection_in_temporary_directory(family, tmp_path, monkeypatch):
    scratch = _scratch(tmp_path, monkeypatch)
    calls = _spy_project(monkeypatch)
    build = _run_v15 if family == "15" else _run_v16
    run = await build(tmp_path, monkeypatch)
    assert calls
    for call in calls:
        assert "tmp_root" not in call
        assert call["control_prefix"] is not None
        assert scratch in call["root"].parents
    _no_leftovers(scratch, run)


async def test_reflector_projection_discarded_after_failure(tmp_path, monkeypatch):
    from evolution.tree.roles.reflector_contracts import result_note_digest

    scratch = _scratch(tmp_path, monkeypatch)
    calls = _spy_project(monkeypatch)
    run = await two_case_run(tmp_path, reflector="print('not a diagnosis')\n", terminal=None)
    assert calls and all(scratch in call["root"].parents for call in calls)
    failures = [
        record["payload"]
        for record in journal_records(run)
        if record["event"] == "reflection_failed"
    ]
    # A subprocess Reflector whose last line is not a result object fails
    # `bad_result`: a reported failure of class `unknown` (reason
    # `unclassified` in this `/15` run), not a storage or infrastructure one.
    assert failures
    for failure in failures:
        assert failure["error_class"] == "reported"
        assert failure["reason"] == "unclassified"
        assert failure["note_digest"] == result_note_digest("bad_result")
    _no_leftovers(scratch, run)


def _agent_cli(probe_url=None):
    import sys

    def configure(document):
        document["reflector"] = {
            **{
                k: document["reflector"][k]
                for k in ("selection", "families", "max_consecutive_failures")
            },
            "kind": "agent_cli",
            "model": "codex",
            "agent_bin": sys.executable,
            "environment_docs": None,
            "probe": ({"base_url": probe_url, "on_unavailable": "continue"} if probe_url else None),
        }

    return configure


async def test_agent_cli_reflector_result_read_from_temporary_projection(tmp_path, monkeypatch):
    from evolution.tree.roles.reflector.agent_reflector import (
        CONTROL_PREFIX,
        REFLECTION_FILE,
        AgentCliReflector,
    )

    scratch = _scratch(tmp_path, monkeypatch)
    roots = []

    async def agent_call(self, request, root, log, started, *, call, prompt):
        roots.append(root)
        (root / CONTROL_PREFIX / REFLECTION_FILE).write_text(
            json.dumps({"status": "declined", "groups": [], "note": "done"})
        )
        return 1

    monkeypatch.setattr(AgentCliReflector, "_call", agent_call)
    run, result = await public_run(tmp_path, monkeypatch, configure=_agent_cli())
    assert result.terminal == "finished"
    assert roots and all(scratch in root.parents for root in roots)
    assert [r["event"] for r in journal_records(run)].count("reflection_finished") >= 1
    _no_leftovers(scratch, run)


async def test_probe_and_reader_helpers_live_in_projection_control_prefix(tmp_path, monkeypatch):
    import asyncio
    import sys

    from evolution.tree.roles.reflector.agent_reflector import (
        CONTROL_PREFIX,
        REFLECTION_FILE,
        AgentCliReflector,
    )
    from tests.probe_fakes import ProbeHTTPServer

    scratch = _scratch(tmp_path, monkeypatch)
    seen = []

    async def agent_call(self, request, root, log, started, *, call, prompt):
        control = root / CONTROL_PREFIX
        sessions = sorted(p.relative_to(root).as_posix() for p in control.rglob("session.json"))
        assert scratch in control.parents
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-I",
            str(control / "probe" / "probe_request.py"),
            json.dumps({"method": "GET", "path": "/ok", "body": None}),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        seen.append((sessions, json.loads(stdout)["status"]))
        (control / REFLECTION_FILE).write_text(
            json.dumps({"status": "declined", "groups": [], "note": "done"})
        )
        return 1

    monkeypatch.setattr(AgentCliReflector, "_call", agent_call)
    with ProbeHTTPServer() as server:
        run, result = await public_run(tmp_path, monkeypatch, configure=_agent_cli(server.base_url))
    assert result.terminal == "finished"
    assert seen
    for sessions, status in seen:
        assert f"{CONTROL_PREFIX}/probe/session.json" in sessions
        assert f"{CONTROL_PREFIX}/readers/session.json" in sessions
        assert status == 200
    _no_leftovers(scratch, run)


# -- Task B3: per-attempt workspace copies are not fsynced; recovery never trusts them --


class _WorkspaceCut(BaseException):
    """A process death inside the artifact store."""


def _tear_workspaces(run: Path) -> int:
    """Lose the page cache of every workspace: truncate one file, zero another."""

    torn = 0
    for workspace in sorted((run / "artifacts" / ".workspaces").iterdir()):
        files = sorted(p for p in workspace.rglob("*") if p.is_file())
        assert files
        files[0].write_bytes(b"")  # the campaign fixture's workspace may hold one file
        if len(files) > 1:
            files[1].write_bytes(b"\0" * max(1, files[1].stat().st_size))
        torn += 1
    return torn


@pytest.mark.parametrize("family", ["15", "16"])
@pytest.mark.parametrize("cut", ["after_workspace_ready", "after_started"])
async def test_run_finishes_after_torn_workspace(family, cut, tmp_path, monkeypatch):
    from evolution.tree.artifacts.artifacts import ArtifactStore
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import audit_mismatches

    original = ArtifactStore._hook
    state = {"fired": False}

    def interrupted(self, name):
        original(self, name)
        # `after_started` also fires for v0 and promotion copies: cut the seal's.
        workspaces = self._run_root / "artifacts" / ".workspaces"
        if name == cut and not state["fired"] and workspaces.is_dir() and any(workspaces.iterdir()):
            state["fired"] = True
            raise _WorkspaceCut()

    build = _run_v15 if family == "15" else _run_v16
    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "_hook", interrupted)
        with pytest.raises(_WorkspaceCut):
            await build(tmp_path, monkeypatch)
    run = tmp_path / "run"
    assert _tear_workspaces(run) >= 1
    await build(tmp_path, monkeypatch)  # reopen the same run directory
    assert audit_mismatches(run) == []
    assert verify_evidence(run)["mismatches"] == []
    verification = inspect_campaign(run, verify=True).to_payload()["verification"]
    assert verification["mismatches"] == []
    records = journal_records(run)
    finished = [r for r in records if r["event"] == "campaign_finished"]
    assert len(finished) == 1
    assert finished[0]["payload"]["outcome"] == "completed"
    ledger = [
        json.loads(line) for line in (run / "artifacts.jsonl").read_text().splitlines() if line
    ]
    abandoned = [r["payload"]["reason"] for r in ledger if r["event"] == "candidate_abandoned"]
    if cut == "after_started":
        # The seal was cut open and the workspace torn: never published.
        assert "seal_not_published" in abandoned
    else:
        # The cut landed right after `workspace_ready` of one Attempt. On resume the
        # campaign keeps that workspace for the Editor that re-runs and restores it
        # from the Parent: nothing is abandoned, the same Attempt is sealed and
        # promoted from the re-run's bytes, never from the torn ones.
        assert state["fired"]
        events = [(r["event"], r["payload"]) for r in ledger]
        assert abandoned == []
        attempts = [p["attempt_id"] for e, p in events if e == "workspace_opened"]
        assert attempts == ["r01-a01"]
        assert [e for e, _ in events].count("workspace_ready") == 1
        sealed = [
            p for e, p in events if e == "artifact_copy_started" and p["artifact_id"] == attempts[0]
        ]
        assert len(sealed) == 1
        committed = [p for e, p in events if e == "promotion_committed"]
        assert [p["attempt_id"] for p in committed] == attempts
        assert committed[0]["candidate_digest"] == sealed[0]["candidate_digest"]
        skill = run / "artifacts" / "candidates" / attempts[0] / "skills" / "SKILL.md"
        assert skill.read_bytes() and b"\0" not in skill.read_bytes()
