"""H11 Task 8: family-14 role-visibility recovery and crash cuts.

Every tested crash prefix resumes the next owed action with the original frozen
role inputs (ADR-0012): the same `role_projection/1` digest `D`, a fresh sparse
projection for every role run, no reuse of an interrupted role's output and no
second freeze. A replayable Editor terminal restores the Attempt workspace to
its Parent and re-verifies the frozen input (ADR-0023); a projection mismatch
on the Editor side is the existing `workspace_changed` suspension and never a
publication (PDR-0019).

Each cut is compared with an uninterrupted control of the same scenario by
structure only: campaign events and naming keys (minus the interrupted role
operation's own records), the artifact-ledger event sequence and the
measurement row identities. Independently seeded numerical payloads are never
compared.

The seed, the Editor script and `role_visibility` come from the Task 4
module's `_arrange`; Reflector scripts from the Task 5 module; visibility
changes and decisions from the Task 7 module (routine choice 10: the shared
drivers stay untouched).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.campaign import CampaignInvariantViolated, _Campaign
from evolution.tree.campaign_editor import EditorOutcome
from evolution.tree.roles.editor import subprocess_editor
from evolution.tree.roles.editor_contracts import note_digest
from evolution.tree.roles.role_visibility import RoleVisibilityView
from evolution.tree.roles.workspace import capture_tree
from tests.campaign_drivers import EDITED, FACT_KEYS, LENIENT, ROW_EVENTS, _cut_hook, _records
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_visibility_change import (
    AFTER,
    BEFORE,
    _assert_close_only,
    _assert_closed_by_visibility,
    _decision,
    _journal,
    _readable,
    _refused,
)
from tests.test_tree_campaign_family14_visibility_reflector import _reflector
from tests.test_tree_campaign_family14_visibility_run import (
    ACTIONS,
    READABLE,
    WRITABLE,
    _arrange,
    _HiddenAware,
    _listings,
    _parent_projection_digest,
    _visible_entries,
)

pytestmark = pytest.mark.slow

EDITED_SKILL = b"seed skill edited\n"
#: `FACT_KEYS` that name an earlier campaign record by its sequence number.
SEQUENCE_REFERENCES = ("comparison", "binding", "supersedes")


# -- helpers --------------------------------------------------------------------


def _operation_records(run: Path, event: str) -> list[dict]:
    return [r for r in _records(run / "campaign.jsonl") if r["event"] == event]


def _structure(run: Path, *, dropped_operations=frozenset(), dropped_events=frozenset()):
    """What a resumed run shares with its uninterrupted control.

    Campaign records as `(event, naming keys)` in journal order, without the
    records of `dropped_operations` (the interrupted role operation: its start
    and its resume-time `interrupted` terminal); the artifact ledger's event
    sequence; each measurement journal's row identities as a multiset.
    """

    kept = [
        r
        for r in _records(run / "campaign.jsonl")
        if r.get("operation_id") not in dropped_operations and r["event"] not in dropped_events
    ]
    # A reference to an earlier record is its sequence number, which the
    # dropped records shift; it becomes the referenced record's position.
    position = {r["sequence"]: index for index, r in enumerate(kept)}

    def fact(value):
        return ("record", position[value]) if isinstance(value, int) else value

    campaign = [
        (
            r["event"],
            {
                k: fact(r["payload"][k]) if k in SEQUENCE_REFERENCES else r["payload"][k]
                for k in FACT_KEYS
                if k in r["payload"]
            },
        )
        for r in kept
    ]
    ledger = [r["event"] for r in _records(run / "artifacts.jsonl")]
    rows = {
        journal.name: sorted(
            (r["event"], r["payload"].get("case_id"), r["payload"].get("sample_id"))
            for r in _records(journal / "events.jsonl")
            if r["event"] in ROW_EVENTS
        )
        for journal in sorted((run / "measurements").iterdir())
    }
    return campaign, ledger, rows


def _assert_same_structure(
    run: Path, plain: Path, *, dropped_operations=frozenset(), dropped_events=frozenset()
) -> None:
    import difflib

    resumed = _structure(run, dropped_operations=dropped_operations, dropped_events=dropped_events)
    uncut = _structure(plain)
    diff = "\n".join(
        difflib.unified_diff(
            [repr(fact) for fact in uncut[0]], [repr(fact) for fact in resumed[0]], lineterm=""
        )
    )
    assert resumed[0] == uncut[0], diff
    assert resumed[1] == uncut[1]
    assert resumed[2] == uncut[2]


async def _control(tmp_path, monkeypatch, *, arrange=None, **options):
    """An uninterrupted run of the same scenario under `tmp_path/uncut`.

    Called last in a test: it first undoes every patch the interrupted run
    made (its seed and role scripts, spies, cuts), so the control is arranged
    afresh and its role scripts write their own listing files.
    """

    monkeypatch.undo()
    control = tmp_path / "uncut"
    control.mkdir()
    if arrange is not None:
        arrange(control, monkeypatch)
    return await family14_run(control, monkeypatch, **options)


def _spy_editor_projects(monkeypatch) -> list[dict]:
    """Every `project` call the `command` Editor makes: its keywords and root."""

    calls: list[dict] = []
    original = subprocess_editor.project

    def project(workspace, **kwargs):
        call = {"kwargs": kwargs, "root": None}
        calls.append(call)
        projection = original(workspace, **kwargs)
        call["root"] = projection.root
        return projection

    monkeypatch.setattr(subprocess_editor, "project", project)
    return calls


def _spy_reflector_projects(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    original = campaign_module.project

    def project(workspace, **kwargs):
        calls.append(kwargs)
        return original(workspace, **kwargs)

    monkeypatch.setattr(campaign_module, "project", project)
    return calls


def _spy_digests(monkeypatch) -> list[str]:
    """Every `D` the controller computes, first run or resumed."""

    digests: list[str] = []
    original = _Campaign._role_projection_digest

    def digest(self, parent):
        value = original(self, parent)
        digests.append(value)
        return value

    monkeypatch.setattr(_Campaign, "_role_projection_digest", digest)
    return digests


def _editor_action(monkeypatch, name: str, code: str) -> str:
    """Register an Editor `ACTION` body for `_arrange` under `name`."""

    monkeypatch.setitem(ACTIONS, name, code)
    return name


def _fail_once(monkeypatch, tmp_path) -> str:
    """The Editor's first call writes into its projection and exits 3."""

    state = tmp_path / "editor-calls"
    return _editor_action(
        monkeypatch,
        "fail_once",
        f"state = pathlib.Path({str(state)!r})\n"
        "calls = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(calls + 1))\n"
        "if calls == 0:\n"
        '    (root / "skills" / "SKILL.md").write_text("partial\\n")\n'
        "    sys.exit(3)\n",
    )


# -- Reflection cuts ------------------------------------------------------------


@pytest.mark.parametrize("cut", ["after_started", "mid_run"])
async def test_an_interrupted_reflection_reruns_on_a_fresh_projection_under_the_same_digest(
    tmp_path, monkeypatch, cut
):
    """A started reflection the process lost gets `interrupted` and re-runs.

    `mid_run`: the Reflector ran to its answer and rewrote its projection,
    and the process died before any terminal was written. Neither the answer
    nor the rewrite survives: the second run starts from a fresh projection
    under the same `D`, and only the second answer is journaled.
    """

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _arrange(tmp_path, monkeypatch)
    script, listing = _reflector(tmp_path, "rewrite" if cut == "mid_run" else "none", mutations=1)
    projects = _spy_reflector_projects(monkeypatch)
    digests = _spy_digests(monkeypatch)
    options = {"adapter": _HiddenAware(), "reflector": script}
    with monkeypatch.context() as patch:
        if cut == "after_started":
            _cut_hook(patch, "after_reflection_started")
        else:
            real = campaign_module.run_reflector

            async def run_reflector(reflector, request, **kwargs):
                await real(reflector, request, **kwargs)
                raise Crash("power cut after the Reflector answered")

            patch.setattr(campaign_module, "run_reflector", run_reflector)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    (interrupted,) = _operation_records(run, "reflection_started")
    assert not payloads(run, "reflection_failed") and not payloads(run, "reflection_finished")
    assert len(_listings(listing)) == (1 if cut == "mid_run" else 0)
    assert list(scratch.glob("evo-tree-editor-*")) == []

    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)
    first, second = payloads(run, "reflection_started")
    assert first == interrupted["payload"]
    (failed,) = payloads(run, "reflection_failed")
    assert (failed["reflection_id"], failed["error_class"]) == (
        first["reflection_id"],
        "interrupted",
    )
    assert "reason" not in failed and failed["note_digest"] is None
    (finished,) = payloads(run, "reflection_finished")
    assert finished["reflection_id"] == second["reflection_id"] != first["reflection_id"]
    assert finished["status"] == "reflected"
    # The same frozen evidence and the same `D`: one input digest for both.
    projection = _parent_projection_digest(run, READABLE)
    assert second["input_digest"] == first["input_digest"]
    assert set(digests) == {projection}
    expected = [projection] * (2 if cut == "mid_run" else 1)
    assert [call["expected_digest"] for call in projects] == expected
    # Every Reflector run saw a fresh, unmodified projection.
    assert _listings(listing) == [_visible_entries(READABLE)] * len(expected)
    assert list(scratch.glob("evo-tree-editor-*")) == []

    plain, plain_snapshot = await _control(
        tmp_path,
        monkeypatch,
        arrange=_arrange,
        adapter=_HiddenAware(),
        reflector=_reflector(tmp_path / "uncut", "none")[0],
    )
    assert_clean_run(plain, plain_snapshot)
    _assert_same_structure(run, plain, dropped_operations={interrupted["operation_id"]})


async def test_a_cut_after_projection_changed_closes_the_round_without_rerunning(
    tmp_path, monkeypatch
):
    _arrange(tmp_path, monkeypatch)
    script, listing = _reflector(tmp_path, "rewrite")
    options = {"adapter": _HiddenAware(), "reflector": script}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_reflection_failed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    (failed,) = payloads(run, "reflection_failed")
    assert failed["reason"] == "projection_changed"
    assert not payloads(run, "round_closed")
    before = _journal(run)

    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert _journal(run).startswith(before)
    assert payloads(run, "reflection_failed") == [failed]
    assert len(payloads(run, "reflection_started")) == 1
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["reason"], closed["attempts_opened"]) == (
        "unplanned",
        "reflection_failed",
        0,
    )
    # The Reflector ran once, in the first invocation only.
    assert _listings(listing) == [_visible_entries(READABLE)]
    assert (tmp_path / "reflector-calls").read_text() == "1"
    assert not payloads(run, "editor_started")
    assert_clean_run(run, snapshot)

    plain, plain_snapshot = await _control(
        tmp_path,
        monkeypatch,
        arrange=_arrange,
        adapter=_HiddenAware(),
        reflector=_reflector(tmp_path / "uncut", "rewrite")[0],
    )
    assert_clean_run(plain, plain_snapshot)
    _assert_same_structure(run, plain)


def _two_failures_skip(base, patch):
    from tests import test_tree_campaign_family14_run as driver

    _arrange(base, patch)
    document = driver.family14_document

    def family14_document(*args, **kwargs):
        moved = document(*args, **kwargs)
        moved["reflector"]["max_consecutive_failures"] = 2
        return moved

    patch.setattr(driver, "family14_document", family14_document)


#: A focus selection's structure: which cases, in which visitation cycle.
#: Digests over grouping and registry content are seeded per run.
SELECTION_KEYS = ("round", "case_ids", "available", "unavailable", "failing", "cycle", "reason")


def _selection(payload: dict) -> dict:
    return {key: payload[key] for key in SELECTION_KEYS if key in payload}


async def test_a_cut_after_a_projection_changed_skip_advances_visitation_once(
    tmp_path, monkeypatch
):
    """The skip's `round_closed` is durable: the resume neither re-skips nor
    re-runs the skipped round, and the next round selects exactly what the
    uninterrupted run selects."""

    _two_failures_skip(tmp_path, monkeypatch)
    script, listing = _reflector(tmp_path, "rewrite")
    options = {"adapter": _HiddenAware(), "reflector": script, "campaign": {"rounds": 6}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    assert [p.get("visitation") for p in payloads(run, "round_closed")] == [None, "skipped"]
    selected = payloads(run, "focus_portion_selected")
    assert len(selected) == 2

    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    closes = payloads(run, "round_closed")
    assert [p.get("visitation") for p in closes] == [None, "skipped", None, "skipped"]
    assert [p["round"] for p in closes] == [1, 2, 3, 4]
    assert payloads(run, "focus_portion_selected")[:2] == selected
    assert [p["reason"] for p in payloads(run, "reflection_failed")] == ["projection_changed"] * 4
    assert (tmp_path / "reflector-calls").read_text() == "4"
    assert len(_listings(listing)) == 4
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == "reflector_failing"
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []

    plain, plain_snapshot = await _control(
        tmp_path,
        monkeypatch,
        arrange=_two_failures_skip,
        adapter=_HiddenAware(),
        reflector=_reflector(tmp_path / "uncut", "rewrite")[0],
        campaign={"rounds": 6},
    )
    assert journal_consistent(plain).finished == plain_snapshot.finished
    assert audit(plain)["mismatches"] == []
    plain_selected = payloads(plain, "focus_portion_selected")
    resumed_selected = payloads(run, "focus_portion_selected")
    assert len(resumed_selected) == len(plain_selected) == 4
    assert [_selection(p) for p in resumed_selected] == [_selection(p) for p in plain_selected]
    _assert_same_structure(run, plain)


# -- Editor cuts ----------------------------------------------------------------


async def test_a_cut_after_the_freeze_recomputes_d_and_does_not_refreeze(tmp_path, monkeypatch):
    listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    digests = _spy_digests(monkeypatch)
    projects = _spy_editor_projects(monkeypatch)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    computed = len(digests)

    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)
    projection = _parent_projection_digest(run, READABLE)
    # The resume recomputed `D` from the Parent; it reproduced the frozen one.
    assert len(digests) > computed and set(digests) == {projection}
    assert payloads(run, "attempt_input_frozen") == [frozen]
    (started,) = payloads(run, "editor_started")
    assert (started["common_input_digest"], started["input_digest"]) == (
        frozen["common_input_digest"],
        frozen["input_digest"],
    )
    (call,) = projects
    assert call["kwargs"]["expected_digest"] == projection
    assert _listings(listing) == [_visible_entries(READABLE)]

    plain, plain_snapshot = await _control(
        tmp_path, monkeypatch, arrange=_arrange, adapter=_HiddenAware()
    )
    assert_clean_run(plain, plain_snapshot)
    _assert_same_structure(run, plain)


def _cut_editor(patch, cut: str) -> None:
    """`partial`: the process dies at `after_editor_started`, after something
    wrote into the Attempt workspace; `published`: it dies after the Editor's
    whole run, whose edit is already published into the Attempt workspace."""

    if cut == "partial":
        original = _Campaign.hook

        def hook(self, name):
            if name == "after_editor_started":
                for workspace in self.workspaces.values():
                    skill = Path(workspace) / "skills" / "SKILL.md"
                    skill.chmod(0o644)
                    skill.write_bytes(b"partial\n")
                raise Crash("power cut")
            original(self, name)

        patch.setattr(_Campaign, "hook", hook)
    else:
        real = campaign_module.run_editor

        async def run_editor(editor, request, **kwargs):
            outcome = await real(editor, request, **kwargs)
            assert outcome.status == "edited"
            assert (Path(request.workspace) / "skills" / "SKILL.md").read_bytes() == EDITED_SKILL
            raise Crash("power cut after the edit was published")

        patch.setattr(campaign_module, "run_editor", run_editor)


@pytest.mark.parametrize("cut", ["partial", "published"])
async def test_an_interrupted_editor_reruns_from_parent_with_the_same_frozen_input(
    tmp_path, monkeypatch, cut
):
    listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    projects = _spy_editor_projects(monkeypatch)
    with monkeypatch.context() as patch:
        _cut_editor(patch, cut)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    (interrupted,) = _operation_records(run, "editor_started")
    assert not payloads(run, "editor_finished") and not payloads(run, "editor_failed")

    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)
    assert payloads(run, "attempt_input_frozen") == [frozen]
    first, second = payloads(run, "editor_started")
    assert first == interrupted["payload"]
    for started in (first, second):
        assert (started["common_input_digest"], started["input_digest"]) == (
            frozen["common_input_digest"],
            frozen["input_digest"],
        )
    (failed,) = payloads(run, "editor_failed")
    assert (failed["operation_id"], failed["error_class"]) == (
        interrupted["operation_id"],
        "interrupted",
    )
    assert "reason" not in failed
    # The re-run started from the Parent: a seed skill, edited exactly once.
    projection = _parent_projection_digest(run, READABLE)
    runs = _listings(listing)
    assert runs == [_visible_entries(READABLE)] * len(runs)
    assert len(runs) == (2 if cut == "published" else 1)
    assert [call["kwargs"]["expected_digest"] for call in projects] == [projection] * len(runs)
    roots = [call["root"] for call in projects]
    assert len(set(roots)) == len(roots) and not any(root.exists() for root in roots)
    candidate = run / "artifacts" / "candidates" / "r01-a01"
    assert (candidate / "skills" / "SKILL.md").read_bytes() == EDITED_SKILL
    assert (candidate / "prompts" / "system.md").read_bytes() == b"system prompt\n"

    plain, plain_snapshot = await _control(
        tmp_path, monkeypatch, arrange=_arrange, adapter=_HiddenAware()
    )
    assert_clean_run(plain, plain_snapshot)
    _assert_same_structure(run, plain, dropped_operations={interrupted["operation_id"]})


@pytest.mark.parametrize("klass", ["unknown", "provider"])
async def test_a_suspending_editor_failure_reruns_on_a_fresh_projection(
    tmp_path, monkeypatch, klass
):
    """`unknown`: the genuine program exits 3 after writing into its projection.
    `provider`: the program ran and published its edit, and the outcome is
    then reported as `provider_exhausted`. Either way the Attempt stays open,
    the campaign suspends, and the resume re-verifies the frozen input and
    re-runs on a fresh sparse projection of a workspace restored to Parent."""

    if klass == "unknown":
        listing = _arrange(tmp_path, monkeypatch, action=_fail_once(monkeypatch, tmp_path))
    else:
        listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    projects = _spy_editor_projects(monkeypatch)
    digests = _spy_digests(monkeypatch)
    with monkeypatch.context() as patch:
        if klass == "provider":
            real = campaign_module.run_editor

            async def run_editor(editor, request, **kwargs):
                outcome = await real(editor, request, **kwargs)
                assert outcome.status == "edited"
                return EditorOutcome(
                    "failed",
                    None,
                    "reported",
                    "provider_exhausted",
                    False,
                    code="provider_exhausted",
                )

            patch.setattr(campaign_module, "run_editor", run_editor)
        run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    (failed,) = payloads(run, "editor_failed")
    assert failed["reason"] == ("program_failed" if klass == "unknown" else "provider_exhausted")
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == (
        "editor_failing" if klass == "unknown" else "editor_provider_failed"
    )
    assert not payloads(run, "attempt_closed") and not payloads(run, "attempt_sealed")
    (frozen,) = payloads(run, "attempt_input_frozen")
    assert snapshot.finished is None

    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)
    assert payloads(run, "attempt_input_frozen") == [frozen]
    started = payloads(run, "editor_started")
    assert len(started) == 2
    assert {(s["common_input_digest"], s["input_digest"]) for s in started} == {
        (frozen["common_input_digest"], frozen["input_digest"])
    }
    projection = _parent_projection_digest(run, READABLE)
    assert set(digests) == {projection}
    assert [call["kwargs"]["expected_digest"] for call in projects] == [projection] * 2
    roots = [call["root"] for call in projects]
    assert roots[0] != roots[1] and not any(root.exists() for root in roots)
    # Both runs saw the Parent's seed skill: the first run's write is gone.
    assert _listings(listing) == [_visible_entries(READABLE)] * 2
    candidate = run / "artifacts" / "candidates" / "r01-a01"
    assert (candidate / "skills" / "SKILL.md").read_bytes() == EDITED_SKILL
    (finished,) = payloads(run, "editor_finished")
    assert finished["status"] == "edited"

    plain, plain_snapshot = await _control(
        tmp_path, monkeypatch, arrange=_arrange, adapter=_HiddenAware()
    )
    assert_clean_run(plain, plain_snapshot)
    _assert_same_structure(
        run,
        plain,
        dropped_operations={_operation_records(run, "editor_started")[0]["operation_id"]},
        dropped_events={"campaign_suspended"},
    )


async def test_a_cancelled_command_editor_leaves_no_projection_and_an_untouched_workspace(
    tmp_path, monkeypatch
):
    flag = tmp_path / "editor-running"
    action = _editor_action(
        monkeypatch,
        "hang",
        '(root / "skills" / "SKILL.md").write_text("partial\\n")\n'
        f"pathlib.Path({str(flag)!r}).write_text('x')\n"
        "import time\n"
        "time.sleep(120)\n",
    )
    listing = _arrange(tmp_path, monkeypatch, action=action)
    projects = _spy_editor_projects(monkeypatch)
    seen: dict[str, object] = {}
    real = campaign_module.run_editor

    async def run_editor(editor, request, **kwargs):
        seen["before"] = capture_tree(Path(request.workspace))
        outcome = await real(editor, request, **kwargs)
        seen["after"] = capture_tree(Path(request.workspace))
        seen["outcome"] = outcome
        return outcome

    monkeypatch.setattr(campaign_module, "run_editor", run_editor)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), cancelled=flag.exists
    )
    assert flag.exists()
    assert _listings(listing) == [_visible_entries(READABLE)]
    outcome = seen["outcome"]
    assert isinstance(outcome, EditorOutcome)
    assert (outcome.terminal, outcome.error_class) == ("failed", "cancelled")
    # The projection is gone, and the Attempt workspace holds no byte of the write.
    (call,) = projects
    assert call["root"] is not None and not call["root"].exists()
    assert not call["root"].parent.exists()
    assert seen["after"] == seen["before"]
    (failed,) = payloads(run, "editor_failed")
    assert failed["error_class"] == "cancelled"
    assert not payloads(run, "editor_finished") and not payloads(run, "attempt_sealed")
    assert not (run / "artifacts" / "candidates" / "r01-a01").exists()
    assert snapshot.finished["outcome"] == "stopped"
    assert journal_consistent(run).finished == snapshot.finished
    assert audit(run)["mismatches"] == []


# -- A projection-digest mismatch injected on resume ----------------------------


async def test_a_changed_controller_view_on_resume_breaks_the_frozen_input(tmp_path, monkeypatch):
    """The hidden-visible boundary moves only in the controller's view seam
    (the manifest is unchanged): once `D` is frozen the resume refuses before
    the Editor runs, and the untouched view then resumes the same Attempt."""

    listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    before = _journal(run)
    ledger = [r["event"] for r in _records(run / "artifacts.jsonl")]
    moved = RoleVisibilityView(readable_paths=(), writable_paths=WRITABLE)
    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "role_view", property(lambda self: moved))
        with pytest.raises(CampaignInvariantViolated, match="frozen editor common input changed"):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert _journal(run) == before
    # `run_editor` materializes the Attempt workspace before the frozen-common
    # check, so the refused resume leaves exactly that in the artifact ledger.
    assert [r["event"] for r in _records(run / "artifacts.jsonl")] == [
        *ledger,
        "workspace_opened",
        "workspace_ready",
    ]
    assert not payloads(run, "editor_started")
    assert payloads(run, "attempt_input_frozen") == [frozen]
    assert _listings(listing) == []

    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "attempt_input_frozen") == [frozen]
    assert _listings(listing) == [_visible_entries(READABLE)]
    assert_clean_run(run, snapshot)


async def test_a_visible_file_tampered_in_the_resumed_workspace_suspends_and_never_publishes(
    tmp_path, monkeypatch
):
    """On the Editor side the projection is checked against `D` itself: a
    visible file changed in the restored Attempt workspace is the Editor's
    `workspace_changed` (`environment_changed`, class `unknown`), a
    suspension; the next resume restores the workspace from Parent."""

    listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    projects = _spy_editor_projects(monkeypatch)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_editor_started")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, adapter=adapter)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    original = _Campaign.hook

    def tamper(self, name):
        if name == "after_editor_started":
            for workspace in self.workspaces.values():
                target = Path(workspace) / "prompts" / "system.md"
                target.chmod(0o644)
                target.write_bytes(b"tampered\n")
        original(self, name)

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", tamper)
        run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    interrupted, refused = payloads(run, "editor_failed")
    assert interrupted["error_class"] == "interrupted"
    assert (refused["error_class"], refused["reason"]) == ("reported", "environment_changed")
    # The `command` Editor's own projection refusal, not another workspace check.
    assert refused["note_digest"] == note_digest(subprocess_editor.PROJECTION_REFUSED_NOTE)
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == "editor_failing"
    assert snapshot.finished is None
    # Never a publication: the Editor was not spawned and nothing was sealed.
    assert _listings(listing) == []
    assert len(projects) == 1 and projects[0]["root"] is None
    assert not payloads(run, "editor_finished") and not payloads(run, "attempt_sealed")
    assert not (run / "artifacts" / "candidates" / "r01-a01").exists()
    assert not [r for r in _records(run / "artifacts.jsonl") if r["event"].startswith("promotion_")]
    assert audit(run)["mismatches"] == []

    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "attempt_input_frozen") == [frozen]
    assert _listings(listing) == [_visible_entries(READABLE)]
    candidate = run / "artifacts" / "candidates" / "r01-a01"
    assert (candidate / "prompts" / "system.md").read_bytes() == b"system prompt\n"
    assert (candidate / "skills" / "SKILL.md").read_bytes() == EDITED_SKILL
    assert_clean_run(run, snapshot)

    plain, plain_snapshot = await _control(
        tmp_path, monkeypatch, arrange=_arrange, adapter=_HiddenAware()
    )
    assert_clean_run(plain, plain_snapshot)
    _assert_same_structure(
        run,
        plain,
        dropped_operations={
            r["operation_id"] for r in _operation_records(run, "editor_started")[:2]
        },
        dropped_events={"campaign_suspended"},
    )


# -- Authorization interaction --------------------------------------------------


async def test_a_visibility_close_after_an_editor_suspension_does_not_resume_the_editor(
    tmp_path, monkeypatch
):
    listing = _arrange(tmp_path, monkeypatch, action=_fail_once(monkeypatch, tmp_path))
    _readable(monkeypatch, BEFORE)
    adapter = _HiddenAware()
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == "editor_failing"
    before = _journal(run)
    assert len(_listings(listing)) == 1
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, adapter=adapter)
    _assert_close_only(refused)
    assert _journal(run) == before
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=adapter, authorizations=[_decision(refused, None)]
    )
    _assert_closed_by_visibility(run, before, snapshot)
    assert len(_listings(listing)) == 1
    assert len(payloads(run, "editor_started")) == 1
    assert not payloads(run, "attempt_closed")


async def test_a_visibility_close_after_a_threshold_suspension_does_not_resume_the_stage(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import _arm_on_finalists_bound, _CohortFaults

    adapter = _CohortFaults(faults={("candidate", "c2", 1): "target_failed"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {
        "adapter": adapter,
        "campaign": {
            "sample_retries": 0,
            "indeterminate": {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}},
        },
    }
    _readable(monkeypatch, BEFORE)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspensions[-1]["evidence_kind"] == "threshold"
    before = _journal(run)
    rows = {path.parent.name: path.read_bytes() for path in run.glob("measurements/*/events.jsonl")}
    adapter.faults.clear()
    _readable(monkeypatch, AFTER)
    refused = await _refused(tmp_path, monkeypatch, **options)
    _assert_close_only(refused)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, authorizations=[_decision(refused, "default")], **options
    )
    _assert_closed_by_visibility(run, before, snapshot)
    after = {
        path.parent.name: path.read_bytes() for path in run.glob("measurements/*/events.jsonl")
    }
    assert after == rows
    assert not payloads(run, "repairs_dispatched")
