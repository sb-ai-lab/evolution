"""Shared genuine-run factories and journal replay for the run-observability tasks.

The observability test modules (Task 2's `test_tree_run_reader.py`, and Tasks
4, 6 and 7) import these helpers instead of building their own campaigns:

* `genuine_*_run(work)` — async factories, each running one real engine
  invocation (or a small honest sequence of them, e.g. crash then resume) in
  `work` and returning the finished run directory `work / "run"`. They take a
  directory, not `tmp_path`/`tmp_path_factory`, so a test module scopes them
  (module-scoped fixtures built with `tmp_path_factory`, family 14 inside
  `pytest.MonkeyPatch.context()`).
* `JournalReplayer` — copies a finished run's journal into an empty directory
  one record at a time, optionally holding a record's tail back as a half
  line, so `run_reader.JournalTail`'s incremental reading is exercised over
  genuine bytes.
* `copy_retired_run(name, target)` — a read-only copy of one frozen retired
  family fixture (the fixtures are committed read-only; readers work on the
  copy).

`tests/campaign_fakes.py` is deliberately not touched (parallel-lane
conflicts); the factories below compose the existing genuine helpers the
fixture inventory names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.campaign_drivers import (
    C2_FAILS_SCORER,
    _continue,
    _crash_once_and_resume,
    _cut_hook,
    _every_case,
    _only_c1,
    _rebaseline,
    _records,
    _reflector_classifying,
)
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import FakeAdapter, FakeInstance
from tests.test_tree_train_test import evaluation_manifest
from tests.tree_evolve_support import AFFECTED_GATE

#: The default `/14`-written family-13 settings that bind a cohort: the
#: observe gate accepts the round's edited candidate, so finalists exist.
CAMPAIGN_SETTINGS = {"gate": {"mode": "observe"}, "repeats": 1}


def _edited_used(case_id, turn):
    return ["skills/SKILL.md"]


async def genuine_campaign_run(work: Path) -> Path:
    """One finished family-13 campaign (observe gate): baseline, attempt,
    fresh parent and the round's cohort (champion + finalist) all bound."""
    return await _two_case_run(work, used=_edited_used, as_family_13=dict(CAMPAIGN_SETTINGS))


async def genuine_family14_run(work: Path) -> Path:
    """One finished family-14 campaign: baseline, attempt, fresh parent and
    the round's `final` members (champion + finalist) all bound."""
    from tests.test_tree_campaign_family14_run import family14_run

    with pytest.MonkeyPatch.context() as monkeypatch:
        run, _snapshot = await family14_run(work, monkeypatch)
    return run


async def genuine_family14_refresh_run(work: Path) -> Path:
    """A family-14 campaign whose stale rounds refresh: rounds 2 and 3 each
    bind an `m-r<NN>-refresh` baseline measurement (registry role `baseline`,
    listed in `snapshot.refreshes`)."""
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS
    from tests.test_tree_campaign_family14_run import family14_run

    with pytest.MonkeyPatch.context() as monkeypatch:
        run, _snapshot = await family14_run(
            work,
            monkeypatch,
            NO_FINALISTS,
            family14_settings={"refresh": 1},
            campaign={"rounds": 3},
        )
    return run


async def genuine_repeated_campaign_run(work: Path) -> Path:
    """One finished family-12 campaign: the promoted winner's completion is
    bound as `m-r01-completion`, whose journal carries `sample_imported`
    rows (the in-run carry)."""
    return await _two_case_run(work)


async def genuine_continued_run(work: Path) -> Path:
    """A family-12 campaign whose target moved mid-round: the crashed run is
    refused, an accepted `continue` re-binds the unfinished measurements
    under `m-<id>@e1` (registry `epoch` 1, `rebound_from` the source id)."""
    from evolution.tree.campaign import _Campaign
    from evolution.tree.execution.output_admission import EvolveRefused
    from tests.campaign_drivers import _Resettable

    adapter = _Resettable(used=_only_c1)
    original = _Campaign.evaluate_confidence

    async def cut(self, attempt_id, look):
        # Both measurements completed stage 1; the look is not yet evaluated.
        from tests.campaign_fakes import Crash

        raise Crash("power cut")

    def run(**extra):
        return _two_case_run(work, adapter=adapter, used=_only_c1, terminal=None, **extra)

    from tests.campaign_fakes import Crash

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(_Campaign, "evaluate_confidence", cut)
        with pytest.raises(Crash, match="power cut"):
            await run()
        monkeypatch.setattr(_Campaign, "evaluate_confidence", original)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await run()
    kept = []
    run = await run(authorizations=[_continue(refused.value)], keep=kept)
    (live,) = kept
    assert live.terminal == "finished"
    return run


async def genuine_rebaselined_run(work: Path) -> Path:
    """A family-12 campaign rebaselined after round 1: round 2 binds
    `m-r02-champion@e1` and plain-id attempt measurements, all with registry
    `epoch` 1 (the epoch is a binding fact, not parsed from the id suffix)."""
    from tests.campaign_fakes import Crash

    off = {**AFFECTED_GATE, "fresh_parent": False, "global": False}

    def run_with(gate, **extra):
        return _two_case_run(
            work,
            used=_every_case,
            campaign={"rounds": 2, "gate": gate},
            scorer=C2_FAILS_SCORER,
            reflector=_reflector_classifying(work / "reflections", ("c1",), ("c2",)),
            terminal=None,
            **extra,
        )

    with pytest.MonkeyPatch.context() as monkeypatch:
        _cut_hook(monkeypatch, "after_round_closed")
        with pytest.raises(Crash):
            await run_with(off)
    from evolution.tree.execution.output_admission import EvolveRefused

    with pytest.raises(EvolveRefused) as refused:
        await run_with(AFFECTED_GATE)
    kept = []
    run = await run_with(AFFECTED_GATE, authorizations=[_rebaseline(refused.value)], keep=kept)
    (live,) = kept
    assert live.terminal == "finished"
    return run


async def genuine_evaluation_run(work: Path) -> Path:
    """One completed standalone evaluation run (one child measurement,
    `measurements/evaluation`)."""
    from evolution.tree.evaluation import evaluate

    path = evaluation_manifest(work)
    result = await evaluate(path, work / "run", adapter_factory=lambda *args: FakeAdapter())
    assert result["state"] == "completed"
    return work / "run"


async def genuine_retried_evaluation_run(work: Path) -> Path:
    """An evaluation whose only sample keeps declining (target instability),
    suspended and resumed until `sample_retries` are spent, then completed.

    The child journal carries one try per `(case, sample, retry_ordinal)`
    with ordinals ascending from 0 in journal order.
    """
    from evolution.tree.evaluation import evaluate

    path = evaluation_manifest(work)
    document = json.loads(path.read_text())
    document.update(repeats=1, sample_retries=2)
    path.write_text(json.dumps(document))
    run = work / "run"
    current = "target_failed"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(FakeInstance, "_stored", lambda self, session: current)
        for _ in range(3):
            report = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
            assert report["state"] == "suspended"
        current = "completed"
        report = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert report["state"] == "completed"
    return run


async def genuine_repair_evaluation_run(work: Path) -> Path:
    """An evaluation whose scorer fails after a completed rollout: the run
    suspends, and the resume re-measures the unscored sample with a
    `repair: true` try."""
    from evolution.tree.evaluation import evaluate

    path = evaluation_manifest(work)
    document = json.loads(path.read_text())
    document["repeats"] = 1
    path.write_text(json.dumps(document))
    marker = work / "scorer-unavailable"
    marker.touch()
    script = work / "scorer" / "score.py"
    script.write_text(
        "import pathlib, sys\nif pathlib.Path("
        + repr(str(marker))
        + ").exists(): sys.exit(1)\n"
        + script.read_text()
    )
    run = work / "run"
    first = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert first["state"] == "suspended"
    marker.unlink()
    second = await evaluate(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert second["state"] == "completed"
    return run


class _FlakySendAdapter(FakeAdapter):
    """Fails each instance's first `send` with a transient, retryable error,
    then behaves: the rollout retries the call and the try completes, so the
    journal holds `target_send_failed` (with `error_code`) and a second
    `target_send_started` (`call_attempt` 2)."""

    def instance_for(self, candidate, instance_id, boot_id):
        from evolution.tree.execution.interaction import RolloutInfrastructureError

        instance = super().instance_for(candidate, instance_id, boot_id)
        original = instance.send
        state = {"failed": False}

        async def send(session, message, request_id):
            if not state["failed"]:
                state["failed"] = True
                raise RolloutInfrastructureError(
                    "scripted transient failure", "internal", retryable=True
                )
            return await original(session, message, request_id)

        instance.send = send
        return instance


async def genuine_call_failure_evaluation_run(work: Path) -> Path:
    """An evaluation with `turn_max_attempts: 2` whose instance fails its
    first send transiently: the completed try's `target_calls` count both
    call attempts and its `error_code` is the journaled failure code."""
    from evolution.tree.evaluation import evaluate

    path = evaluation_manifest(work)
    document = json.loads(path.read_text())
    document.update(repeats=1, call_retries={"max_attempts": 1, "turn_max_attempts": 2})
    path.write_text(json.dumps(document))
    adapter = _FlakySendAdapter()
    result = await evaluate(path, work / "run", adapter_factory=lambda *args: adapter)
    assert result["state"] == "completed"
    return work / "run"


async def genuine_crashed_resumed_run(work: Path) -> Path:
    """A family-12 campaign cut after its attempt's look was evaluated (both
    joint stages' tries are journaled) and resumed to completion: the
    journals hold every try exactly once."""
    from tests.campaign_drivers import _attempt_of

    def crashed(run: Path) -> None:
        # ADR-0032: the Candidate confidence comparison owns this quality-look boundary.
        assert _records(run / "campaign.jsonl")[-1]["event"] == "comparison_look_evaluated"
        assert _attempt_of(run)

    with pytest.MonkeyPatch.context() as monkeypatch:
        _cut_hook(monkeypatch, "after_comparison_look_evaluated")
        return await _crash_once_and_resume(work, crashed)


class JournalReplayer:
    """Replays a finished run's journals into an empty directory.

    `lines(journal)` returns the source journal's raw lines in order; the
    `append_*` methods write those bytes into `target / journal` (creating
    parent directories), one record at a time. A test holds a record's tail
    back by appending only part of a line, then completes it later — the
    reader under test must not consume a half-written line, and must return
    the record whole once its newline lands.
    """

    def __init__(self, source: Path, target: Path) -> None:
        self._source = Path(source)
        self.target = Path(target)

    def lines(self, journal: str) -> tuple[bytes, ...]:
        """The source `journal`'s raw lines, each ending in `\\n`, in order."""
        return tuple((self._source / journal).read_bytes().splitlines(keepends=True))

    def append_bytes(self, journal: str, data: bytes) -> None:
        """Append arbitrary bytes (a partial record or a corrupt line)."""
        path = self.target / journal
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(data)

    def append_records(self, journal: str, records: tuple[bytes, ...] | list[bytes]) -> None:
        """Append whole records (raw lines) to the target journal."""
        self.append_bytes(journal, b"".join(records))


def measurement_journal(run: Path, measurement_id: str) -> Path:
    """The `events.jsonl` path of one bound measurement."""
    return run / "measurements" / measurement_id / "events.jsonl"


def task_data_needles(run: Path) -> set[str]:
    """Collect actual corpus, request, scorer and role text for O8 checks.

    Fixture corpora live beside the run (or in workspace for retired runs).
    All public/oracle string leaves count, including short values. Journal
    and trace metadata are traversed only at task-text fields, so ids and
    outcome codes do not masquerade as private task data.
    """
    found: set[str] = set()
    text_fields = {
        "note",
        "notes",
        "description",
        "descriptions",
        "text",
        "content",
        "diagnosis",
        "feedback",
        "public",
        "oracle",
        "message",
        "messages",
        "root_cause",
        "suggested_fix",
        "counterevidence",
        "regression_risks",
        "why_this_layer",
        "verdicts",
        "details",
    }

    def strings(value: object) -> None:
        if isinstance(value, str):
            if value:
                found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                strings(item)
        elif isinstance(value, list):
            for item in value:
                strings(item)

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in text_fields:
                    strings(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for corpus in (run.parent / "corpus.jsonl", run.parent / "workspace" / "corpus.jsonl"):
        if corpus.exists():
            for line in corpus.read_text().splitlines():
                row = json.loads(line)
                strings(row.get("public"))
                strings(row.get("oracle"))
    for path in run.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            collect(json.loads(line).get("payload", {}))
    for path in run.rglob("request.json"):
        collect(json.loads(path.read_text()))
    for path in run.rglob("stdout"):
        try:
            collect(json.loads(path.read_text()))
        except json.JSONDecodeError:
            # A deliberately failed scorer may have produced no JSON.
            continue
    return found
