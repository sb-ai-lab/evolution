"""H11 Task 5: the family-14 Reflector reads the role projection and may not change it.

F0 R64/R67 and routine choices 1, 2 and 7. The Reflector gets the same sparse
projection as the round's Editors (`readable_paths ∪ writable_paths`), its
`reflection_started.input_digest` binds the `role_projection/1` digest `D`, and
any change it makes to its frozen projection outside its own control subtree is
`reflection_failed(error_class=projection_changed, reason=projection_changed)`,
class `content`: the round closes `unplanned` and the failure counts toward
`reflector.max_consecutive_failures`.

The seed, the Editor script and `role_visibility` come from the Task 4 module's
`_arrange` (routine choice 10: the shared drivers stay untouched); the Reflector
is a declared `subprocess` program handed to `_two_case_run` as its script text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.artifacts.artifact_policy import matches
from evolution.tree.campaign_reflector import ReflectorOutcome
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload
from evolution.tree.roles.reflector_contracts import result_note_digest
from evolution.tree.roles.role_visibility import RoleVisibilityView
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_visibility_run import (
    READABLE,
    WRITABLE,
    _arrange,
    _HiddenAware,
    _listing_digest,
    _listings,
    _parent_projection_digest,
    _visible_entries,
)

pytestmark = pytest.mark.slow

#: The diagnosis `_two_case_run`'s default Reflector prints: one group on `c1`.
GROUP = {
    "root_cause": "r",
    "suggested_fix": "f",
    "evidence_ids": ["obs:c1:1"],
    "affected_cases": ["c1"],
    "target_paths": ["skills/SKILL.md"],
    "counterevidence": "c",
    "regression_risks": "r",
    "why_this_layer": "w",
}
REFLECTED = json.dumps({"status": "reflected", "note": "n", "groups": [GROUP]})
DECLINED = json.dumps({"status": "declined", "note": "n", "groups": []})

#: The Reflector: record the projection outside its control subtree, run
#: `ACTION` on its first `MUTATIONS` calls, then answer with `TAIL`.
REFLECTOR = r"""
import hashlib
import json
import os
import pathlib
import sys

request = json.loads(sys.stdin.read())
root = pathlib.Path(request["workspace"])
entries = []
for directory, names, files in os.walk(root):
    base = pathlib.Path(directory)
    if base == root:
        names[:] = [name for name in names if name != ".evolution"]
    for name in names:
        entries.append({"path": (base / name).relative_to(root).as_posix(), "kind": "directory"})
    for name in files:
        path = base / name
        status = os.lstat(path)
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "kind": "file",
                "executable": bool(status.st_mode & 0o111),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
with open(LISTING, "a", encoding="utf-8") as sink:
    sink.write(json.dumps(sorted(entries, key=lambda entry: entry["path"])) + "\n")
state = pathlib.Path(STATE)
calls = int(state.read_text()) if state.exists() else 0
state.write_text(str(calls + 1))
if calls < MUTATIONS:
    ACTION
TAIL
"""

#: Each body runs at the indentation of `ACTION`. Write bits are restored and
#: then removed again where a change needs them, so only the change remains.
ACTIONS = {
    "none": ["pass"],
    # (a) rewrite a visible file
    "rewrite": [
        'target = root / "prompts" / "system.md"',
        "os.chmod(target, 0o600)",
        'target.write_bytes(b"changed\\n")',
        "os.chmod(target, 0o400)",
    ],
    # (b) a mode-only change of a visible file
    "chmod": ['os.chmod(root / "prompts" / "system.md", 0o600)'],
    # (c) a symlink to the hidden file's real path in the published Parent
    "symlink": [
        'folder = root / "prompts"',
        "os.chmod(folder, 0o700)",
        'real = sorted(pathlib.Path(CHAMPIONS).glob("*/runtime/model.bin"))[0]',
        'os.symlink(real, folder / "model.bin")',
        "os.chmod(folder, 0o500)",
    ],
    # (d) delete a visible file
    "delete": [
        'folder = root / "prompts"',
        "os.chmod(folder, 0o700)",
        '(folder / "system.md").unlink()',
        "os.chmod(folder, 0o500)",
    ],
    # (e) create a file in the frozen tree after restoring write bits
    "create": [
        'folder = root / "skills"',
        "os.chmod(folder, 0o700)",
        '(folder / "NEW.md").write_bytes(b"new\\n")',
        'os.chmod(folder / "NEW.md", 0o400)',
        "os.chmod(folder, 0o500)",
    ],
    "fifo": [
        'folder = root / "skills"',
        "os.chmod(folder, 0o700)",
        'os.mkfifo(folder / "pipe", 0o400)',
        "os.chmod(folder, 0o500)",
    ],
    "hardlink": [
        'folder = root / "skills"',
        "os.chmod(folder, 0o700)",
        'os.link(folder / "SKILL.md", folder / "LINK.md")',
        "os.chmod(folder, 0o500)",
    ],
    "kind": [
        'folder = root / "prompts"',
        "os.chmod(folder, 0o700)",
        '(folder / "system.md").unlink()',
        '(folder / "system.md").mkdir(mode=0o500)',
        "os.chmod(folder, 0o500)",
    ],
    "empty_directory": [
        'folder = root / "skills"',
        "os.chmod(folder, 0o700)",
        '(folder / "empty").mkdir(mode=0o500)',
        "os.chmod(folder, 0o500)",
    ],
    "directory_chmod": ['os.chmod(root / "skills", 0o700)'],
    "root_chmod": ["os.chmod(root, 0o700)"],
    # A file at a hidden path: the projection lacks it, so it is a create.
    "hidden_name": [
        "os.chmod(root, 0o700)",
        '(root / "runtime").mkdir(mode=0o700)',
        '(root / "runtime" / "model.bin").write_bytes(b"planted")',
        'os.chmod(root / "runtime" / "model.bin", 0o400)',
        'os.chmod(root / "runtime", 0o500)',
        "os.chmod(root, 0o500)",
    ],
    # Deeper than the recursive no-follow walk descends: the walk's
    # `RecursionError` is the Reflector's doing, a change and no crash.
    "deep_visible": [
        'folder = root / "skills"',
        "os.chmod(folder, 0o700)",
        "deep = folder",
        "for _ in range(1400):",
        '    deep = deep / "d"',
        "    deep.mkdir()",
        "os.chmod(folder, 0o500)",
    ],
    # (f) the projection untouched, the Reflector's own control subtree written
    "control": [
        'control = root / ".evolution"',
        '(control / "scratch").mkdir()',
        '(control / "scratch" / "notes.txt").write_text("mine\\n")',
        'os.chmod(control / "scratch" / "notes.txt", 0o755)',
    ],
    # The control subtree is exempt from the comparison, but not from the
    # no-follow walk the Editor's admission makes too: a symlink there is
    # refused, and a tree too deep to walk is a change, not a crash.
    "control_deep": [
        'deep = root / ".evolution"',
        "for _ in range(1400):",
        '    deep = deep / "d"',
        "    deep.mkdir()",
    ],
    "control_symlink": [
        'os.symlink("../skills/SKILL.md", root / ".evolution" / "link")',
    ],
}
MUTATING = (
    "rewrite",
    "chmod",
    "symlink",
    "delete",
    "create",
    "fifo",
    "hardlink",
    "kind",
    "empty_directory",
    "directory_chmod",
    "root_chmod",
    "hidden_name",
    "deep_visible",
    "control_symlink",
    "control_deep",
)


def _reflector(
    tmp_path: Path, action: str, *, mutations: int = 1_000, tail: str | None = None
) -> tuple[str, Path]:
    """The Reflector script's text and the file its listings go to."""

    listing = tmp_path / "reflector-listing.jsonl"
    body = "\n    ".join(ACTIONS[action])
    script = (
        REFLECTOR.replace("ACTION", body)
        .replace("TAIL", tail if tail is not None else f"print({REFLECTED!r})")
        .replace("LISTING", repr(str(listing)))
        .replace("STATE", repr(str(tmp_path / "reflector-calls")))
        .replace("MUTATIONS", str(mutations))
        .replace("CHAMPIONS", repr(str(tmp_path / "run" / "artifacts" / "champions")))
    )
    return script, listing


def _spy_reflect_briefs(monkeypatch) -> list[object]:
    briefs: list[object] = []
    original = campaign_module.build_adaptive_reflect_brief

    def build(*args, **kwargs):
        brief = original(*args, **kwargs)
        briefs.append(brief)
        return brief

    monkeypatch.setattr(campaign_module, "build_adaptive_reflect_brief", build)
    return briefs


def _spy_projects(monkeypatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    original = campaign_module.project

    def project(workspace, **kwargs):
        calls.append(kwargs)
        return original(workspace, **kwargs)

    monkeypatch.setattr(campaign_module, "project", project)
    return calls


def _started_digest(brief: object, projection: str | None) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "evidence": reflect_brief_payload(brief),
                "environment_docs": None,
                **({"role_projection": projection} if projection is not None else {}),
            }
        )
    ).hexdigest()


def _assert_projection_changed(run: Path, snapshot, *, note: str | None = "n") -> None:
    (failed,) = payloads(run, "reflection_failed")
    assert (failed["error_class"], failed["reason"]) == ("projection_changed", "projection_changed")
    assert failed["note_digest"] == (None if note is None else result_note_digest(note))
    assert not payloads(run, "reflection_finished")
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["reason"], closed["attempts_opened"]) == (
        "unplanned",
        "reflection_failed",
        0,
    )
    assert "visitation" not in closed
    assert not payloads(run, "editor_started")
    assert not payloads(run, "attempt_opened")
    assert not payloads(run, "campaign_suspended")
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)


# --------------------------------------------------------------------------
# Equality with the Editor and the digest binding
# --------------------------------------------------------------------------


async def test_the_reflector_and_the_editor_start_from_one_projection(tmp_path, monkeypatch):
    editor_listing = _arrange(tmp_path, monkeypatch)
    script, reflector_listing = _reflector(tmp_path, "none")
    briefs = _spy_reflect_briefs(monkeypatch)
    projects = _spy_projects(monkeypatch)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)

    # Both roles saw exactly the visible set, and so the same set.
    (reflected,) = _listings(reflector_listing)
    (edited,) = _listings(editor_listing)
    visible = _visible_entries(READABLE)
    assert reflected == edited == visible
    assert not any(entry["path"].startswith(("runtime", "tools")) for entry in reflected)

    # `D`, recomputed from the Parent, is both listings' digest ...
    projection = _parent_projection_digest(run, READABLE)
    assert _listing_digest(reflected) == _listing_digest(edited) == projection
    # ... the controller projected the Reflector against it ...
    (call,) = projects
    assert call["expected_digest"] == projection
    assert call["control_prefix"] == ".evolution"
    view = RoleVisibilityView(readable_paths=READABLE, writable_paths=WRITABLE)
    assert [call["visible"](path) for path in ("prompts/system.md", "tools/run.sh")] == [
        True,
        False,
    ]
    assert view.visible("prompts/system.md") and not view.visible("tools/run.sh")
    # ... and `reflection_started` binds it in its one digest object.
    (started,) = payloads(run, "reflection_started")
    assert set(started) == {"round", "reflection_id", "operation_id", "input_digest"}
    (brief,) = briefs
    assert started["input_digest"] == _started_digest(brief, projection)
    assert started["input_digest"] != _started_digest(brief, None)

    # The diagnosis still validates; every target path is visible.
    (finished,) = payloads(run, "reflection_finished")
    assert finished["groups"][0]["evidence_ids"] == ["obs:c1:1"]
    targets = [path for group in finished["groups"] for path in group["target_paths"]]
    assert targets and all(view.visible(path) for path in targets)
    assert payloads(run, "editor_started")


@pytest.mark.parametrize(
    "writable,path",
    [
        (("skills/**",), "skills/SKILL.md"),
        (("skills/**",), "skills/deep/x.md"),
        (("*.md",), "notes/a.md"),
        (("prompts/system.md",), "prompts/system.md"),
        ((".github/**",), ".github/workflows/ci.yml"),
    ],
)
@pytest.mark.parametrize("readable", [(), ("docs/**",)])
def test_every_admissible_target_path_is_visible(writable, path, readable):
    """Invariant: `validate_groups` admits a target path by the artifact
    matcher over the writable set, and the view uses the same matcher over
    `readable ∪ writable`, so an admissible target path is always visible."""

    assert any(matches(rule, PurePosixPath(path), False) for rule in writable)
    view = RoleVisibilityView(readable_paths=readable, writable_paths=writable)
    assert view.visible(path)


# --------------------------------------------------------------------------
# A changed projection is a counted content failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", MUTATING)
async def test_a_reflector_that_changes_its_projection_fails_projection_changed(
    tmp_path, monkeypatch, action
):
    editor_listing = _arrange(tmp_path, monkeypatch)
    script, reflector_listing = _reflector(tmp_path, action)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    _assert_projection_changed(run, snapshot)
    assert _listings(reflector_listing) == [_visible_entries(READABLE)]
    assert _listings(editor_listing) == []
    # The published Parent is untouched, and the private copy is gone.
    for champion in (run / "artifacts" / "champions").iterdir():
        assert (champion / "prompts" / "system.md").read_bytes() == b"system prompt\n"
    reflections = run / ".reflections"
    assert not reflections.exists() or not any(reflections.iterdir())


async def test_the_reflectors_own_control_subtree_is_exempt(tmp_path, monkeypatch):
    """(f): writing the control subtree -- files, directories, modes -- is the
    Reflector's own control-plane traffic, not a change of its projection."""

    editor_listing = _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "control")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    assert snapshot.finished["outcome"] == "completed"
    assert not payloads(run, "reflection_failed")
    (finished,) = payloads(run, "reflection_finished")
    assert finished["status"] == "reflected"
    assert payloads(run, "editor_started")
    assert _listings(editor_listing) == [_visible_entries(READABLE)]
    assert_clean_run(run, snapshot)


async def test_projection_changed_wins_over_a_declined_answer(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "rewrite", tail=f"print({DECLINED!r})")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    _assert_projection_changed(run, snapshot)


async def test_projection_changed_wins_over_a_settled_failure(tmp_path, monkeypatch):
    """A program exit is `reported/unclassified`, class `unknown`, which would
    suspend the campaign; the changed projection is the content failure."""

    _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "rewrite", tail="sys.exit(3)")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    _assert_projection_changed(run, snapshot, note="exit_status")


async def test_a_settled_failure_on_an_unchanged_projection_keeps_its_class(tmp_path, monkeypatch):
    """The control for the test above: without a change the exit suspends."""

    _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "none", tail="sys.exit(3)")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    (failed,) = payloads(run, "reflection_failed")
    assert (failed["error_class"], failed["reason"]) == ("reported", "unclassified")
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == "reflector_failing"


async def test_a_lifecycle_terminal_wins_over_projection_changed(tmp_path, monkeypatch):
    """A lifecycle outcome is not the role's answer: the reflection re-runs
    from a fresh projection, which this time the Reflector leaves alone."""

    _arrange(tmp_path, monkeypatch)
    script, listing = _reflector(tmp_path, "rewrite", mutations=1)
    real = campaign_module.run_reflector
    calls = []

    async def run_reflector(reflector, request, **kwargs):
        outcome = await real(reflector, request, **kwargs)
        calls.append(outcome)
        if len(calls) == 1:
            return ReflectorOutcome("failed", None, "cancelled", "", False)
        return outcome

    monkeypatch.setattr(campaign_module, "run_reflector", run_reflector)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "interrupted" and "reason" not in failed
    (finished,) = payloads(run, "reflection_finished")
    assert finished["status"] == "reflected"
    # The second projection was fresh: the first one's rewrite is not in it.
    assert _listings(listing) == [_visible_entries(READABLE)] * 2
    assert payloads(run, "editor_started")
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)


async def test_a_stopping_campaign_wins_over_projection_changed(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "rewrite")
    real = campaign_module.run_reflector
    stop = []

    async def run_reflector(reflector, request, **kwargs):
        outcome = await real(reflector, request, **kwargs)
        stop.append(True)
        return outcome

    monkeypatch.setattr(campaign_module, "run_reflector", run_reflector)
    run, _snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        adapter=_HiddenAware(),
        reflector=script,
        cancelled=lambda: bool(stop),
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "cancelled" and "reason" not in failed
    assert not payloads(run, "round_closed")


async def test_an_unresponsive_reflector_wins_over_projection_changed(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "rewrite")
    real = campaign_module.run_reflector

    async def run_reflector(reflector, request, **kwargs):
        outcome = await real(reflector, request, **kwargs)
        return replace(outcome, alive=True)

    monkeypatch.setattr(campaign_module, "run_reflector", run_reflector)
    run, _snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "unresponsive" and "reason" not in failed


async def test_a_reader_integrity_failure_wins_over_projection_changed(tmp_path, monkeypatch):
    from evolution.tree.roles.reflection_reader import ReaderIntegrityError, ReflectionReadSession

    reflected = []
    original = ReflectionReadSession.verify_admission

    def verify_admission(self, check):
        if reflected:
            raise ReaderIntegrityError("reflection_source_changed")
        return original(self, check)

    monkeypatch.setattr(ReflectionReadSession, "verify_admission", verify_admission)
    real = campaign_module.run_reflector

    async def run_reflector(reflector, request, **kwargs):
        outcome = await real(reflector, request, **kwargs)
        reflected.append(True)
        return outcome

    monkeypatch.setattr(campaign_module, "run_reflector", run_reflector)
    _arrange(tmp_path, monkeypatch)
    script, _ = _reflector(tmp_path, "rewrite")
    run, _snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script
    )
    (failed,) = payloads(run, "reflection_failed")
    assert (failed["error_class"], failed["reason"]) == ("reader_integrity", "reader_integrity")
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == "reflector_failing"
    assert not payloads(run, "round_closed")


async def test_projection_changed_counts_toward_the_skip_and_the_suspension(tmp_path, monkeypatch):
    """Family-10 counting, now reached by the new reason: the second
    consecutive `projection_changed` on the group is a skip, and the second
    skip in a row suspends `reflector_failing`."""

    from tests import test_tree_campaign_family14_run as driver

    _arrange(tmp_path, monkeypatch)
    document = driver.family14_document

    def family14_document(*args, **kwargs):
        moved = document(*args, **kwargs)
        moved["reflector"]["max_consecutive_failures"] = 2
        return moved

    monkeypatch.setattr(driver, "family14_document", family14_document)
    script, _ = _reflector(tmp_path, "rewrite")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), reflector=script, campaign={"rounds": 6}
    )
    assert snapshot.max_consecutive_failures == 2
    failed = payloads(run, "reflection_failed")
    assert [row["reason"] for row in failed] == ["projection_changed"] * 4
    closes = payloads(run, "round_closed")
    assert [row["outcome"] for row in closes] == ["unplanned"] * 4
    assert [row.get("visitation") for row in closes] == [None, "skipped", None, "skipped"]
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["reason"] == "reflector_failing"
    assert not payloads(run, "editor_started")


# --------------------------------------------------------------------------
# Hidden files are absent for every Reflector kind
# --------------------------------------------------------------------------


async def test_hidden_files_are_absent_through_the_chat_and_agent_cli_harnesses(
    tmp_path, monkeypatch
):
    """The controller's own projection, read through each kind's harness.

    While the bound `subprocess` Reflector runs, the projection the controller
    made for it is handed to a `chat` Reflector (scripted provider: list, then
    read the hidden and the readable files) and to an `agent_cli` Reflector
    (fake CLI: list its workspace root). The kinds differ only in how they
    read the one projection the controller makes (`project` is kind-blind).
    """

    from tests.tree.roles.reflector import test_agent_reflector as agent_harness
    from tests.tree.roles.reflector import test_chat_reflector as chat_harness

    _arrange(tmp_path, monkeypatch)
    seen: dict[str, object] = {}
    real = campaign_module.run_reflector

    async def run_reflector(reflector, request, **kwargs):
        legacy = replace(
            request,
            evidence=chat_harness._evidence(),
            reader=None,
            probe=None,
            probe_session=None,
            environment_docs=None,
        )
        chat, _client = chat_harness._reflector(
            tmp_path / "chat",
            [
                chat_harness._response("list_files"),  # the preflight
                chat_harness._response("list_files"),
                chat_harness._response("read_file", {"path": "tools/run.sh"}),
                chat_harness._response("read_file", {"path": "runtime/model.bin"}),
                chat_harness._response("read_file", {"path": "prompts/system.md"}),
                chat_harness._response("finish_reflection", {"groups": [], "declined": True}),
            ],
        )
        seen["chat"] = await chat.reflect(legacy)
        seen["chat_log"] = chat_harness._log_lines(chat)

        def cli(root: Path) -> None:
            seen["agent_root"] = root
            seen["agent_listing"] = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.relative_to(root).parts[0] != ".evolution"
            )
            agent_harness._writes(agent_harness._result("declined", []))(root)

        agent, _instances = agent_harness._reflector(tmp_path / "agent", [cli])
        seen["agent"] = await agent.reflect(legacy)
        seen["workspace"] = request.workspace
        return await real(reflector, request, **kwargs)

    monkeypatch.setattr(campaign_module, "run_reflector", run_reflector)
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=_HiddenAware())
    assert snapshot.finished["outcome"] == "completed"
    assert not payloads(run, "reflection_failed")

    # chat: `list_files` lists only visible files; the hidden ones do not read.
    assert seen["chat"].status == "declined"
    tools = [line for line in seen["chat_log"] if line.get("phase") in ("tool_call", "tool_error")]
    assert [line["tool"] for line in tools] == [
        "list_files",
        "read_file",
        "read_file",
        "read_file",
        "finish_reflection",
    ]
    listed, hidden_script, hidden_binary, readable = tools[:4]
    assert listed["result"]["files"] == ["prompts/system.md", "skills/SKILL.md"]
    assert [summary["path"] for summary in listed["result"]["file_summaries"]] == [
        "prompts/system.md",
        "skills/SKILL.md",
    ]
    for line in (hidden_script, hidden_binary):
        assert line["phase"] == "tool_error", line
        assert "echo run" not in json.dumps(line)
    assert readable["result"]["content"] == "system prompt\n"

    # agent_cli: its workspace root is the projection and holds no hidden entry.
    assert seen["agent"].status == "declined"
    assert seen["agent_root"] == seen["workspace"]
    assert seen["agent_listing"] == ["prompts", "prompts/system.md", "skills", "skills/SKILL.md"]


# --------------------------------------------------------------------------
# Family 13 is unchanged
# --------------------------------------------------------------------------


def test_the_reflector_listing_script_is_valid_python(tmp_path):
    for action in ACTIONS:
        script, _ = _reflector(tmp_path, action)
        compile(script, f"<reflector {action}>", "exec")
