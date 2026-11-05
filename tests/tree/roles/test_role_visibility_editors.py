"""The three bound Editor kinds run on a family-14 role projection.

Family 14 (F0 R64/R65, ADR-0029, PDR-0023) hands an Editor a sparse workspace:
the files matched by `role_visibility.readable_paths ∪ artifacts.writable_paths`
and their ancestor directories, never a hidden file. `EditRequest.projection`
carries the `visible` predicate and the frozen `role_projection/1` digest; a
request without it is a family-13-or-earlier request and must behave exactly as
before.

Every backend is exercised over one workspace holding writable, readable-only
and hidden entries: `command` through a real declared program that
lists, stats and reads its workspace and attempts one operation; `chat`
through the fake-provider harness of `test_tree_chat_editor`; `agent_cli`
through the fake-CLI harness of `test_tree_agent_editor`.

Trusted-program limit (PDR-0005, PDR-0023 "executables are operator-trusted,
not sandboxed"): the family-14 `command` document no longer carries the real
Attempt workspace path, but a declared program runs under the operator's
account and may still guess or discover it -- the `tamper` operation below does
exactly that with a path the test hands it. The guarantee is about what the
program is given and what publication accepts, not an OS sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.roles.editor.agent_editor import CONTROL_PREFIX
from evolution.tree.roles.editor.subprocess_editor import REQUEST_VERSION, SubprocessEditor
from evolution.tree.roles.editor_contracts import (
    PROJECTION_REFUSED_NOTE,
    PUBLICATION_REFUSED_NOTE,
    Diagnosis,
    EditBrief,
    EditBriefCase,
    EditBriefRepeat,
    EditRequest,
    EditResult,
    RoleProjection,
    brief_payload,
)
from evolution.tree.roles.role_visibility import RoleVisibilityView
from evolution.tree.roles.workspace import (
    MAX_CAPTURE_BYTES,
    Budgets,
    capture_tree,
    role_projection_digest,
)
from tests.tree.roles.editor.test_agent_editor import _editor as _agent_editor
from tests.tree.roles.editor.test_chat_editor import _Client, _plan, _response
from tests.tree.roles.editor.test_chat_editor import _editor as _chat_editor

pytestmark = pytest.mark.slow

# -- the shared workspace --------------------------------------------------------

READABLE = ("docs/**", "public/api/*.md", "skills/SKILL.md")  # overlaps the writable glob
WRITABLE = ("skills/**",)

VISIBLE_FILES: dict[str, bytes] = {
    "skills/SKILL.md": b"seed\n",
    "skills/tool.py": b"print('tool')\n",
    "docs/guide.md": b"guide\n",
    "docs/deep/note.txt": b"note\n",
    # A narrow allow inside a directory that is otherwise hidden.
    "public/api/spec.md": b"spec\n",
}
HIDDEN_FILES: dict[str, tuple[bytes, int]] = {
    "public/internal.md": (b"internal-hidden-bytes\n", 0o644),
    # A hidden Target dependency: binary, never visible to an Editor.
    "runtime/model.bin": (b"\x00\xffmodel-hidden-bytes", 0o644),
    "tools/run.sh": (b"#!/bin/sh\necho run-hidden-bytes\n", 0o755),
    "vault/inner/key.txt": (b"key-hidden-bytes\n", 0o600),
    "README": (b"readme-hidden-bytes\n", 0o644),
}
HIDDEN_DIRECTORY_MODES = {"vault": 0o750, "vault/inner": 0o711}
HIDDEN_ENTRIES = frozenset(
    {*HIDDEN_FILES, "runtime", "tools", "vault", "vault/inner"},
)
HIDDEN_MARKER = "hidden-bytes"
HIDDEN_NAMES = ("internal.md", "model.bin", "run.sh", "vault", "key.txt")

VISIBLE_ENTRIES = sorted(
    {
        *VISIBLE_FILES,
        "skills",
        "docs",
        "docs/deep",
        "public",
        "public/api",
    }
)

_DIAGNOSIS = Diagnosis("absent", None, None, (), None)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "attempt" / "workspace"
    workspace.mkdir(parents=True)
    for relative, content in VISIBLE_FILES.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, 0o644)
    for relative, (content, mode) in HIDDEN_FILES.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, mode)
    for relative, mode in HIDDEN_DIRECTORY_MODES.items():
        os.chmod(workspace / relative, mode)
    return workspace


def _tree(root: Path) -> dict[str, tuple[str, int, bytes]]:
    """Every entry's kind, mode and bytes: what "untouched" and "preserved" mean."""

    entries: dict[str, tuple[str, int, bytes]] = {
        ".": ("dir", stat.S_IMODE(root.stat().st_mode), b"")
    }
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            details = path.lstat()
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(details.st_mode)
            if stat.S_ISDIR(details.st_mode):
                entries[relative] = ("dir", mode, b"")
            elif stat.S_ISREG(details.st_mode):
                entries[relative] = ("file", mode, path.read_bytes())
            else:
                entries[relative] = ("other", mode, b"")
    return entries


def _hidden(tree: dict[str, tuple[str, int, bytes]]) -> dict[str, tuple[str, int, bytes]]:
    return {path: value for path, value in tree.items() if path in HIDDEN_ENTRIES}


def _projection(
    workspace: Path,
    *,
    readable: tuple[str, ...] = READABLE,
    writable: tuple[str, ...] = WRITABLE,
    digest: str | None = None,
) -> RoleProjection:
    view = RoleVisibilityView(readable_paths=readable, writable_paths=writable)
    return RoleProjection(
        visible=view.visible,
        expected_digest=(
            digest
            if digest is not None
            else role_projection_digest(capture_tree(workspace), view.visible)
        ),
    )


def _brief() -> EditBrief:
    case = EditBriefCase(
        case_id="c1",
        group_id="g1",
        stratum="s1",
        tags=("t",),
        public={"text": "hello"},
        repeats=(EditBriefRepeat(1, "c1-r1", "rollout_finished", 0.5, True),),
    )
    return EditBrief(
        champion_digest="a" * 64,
        operator_context="context",
        cases=(case,),
        diagnosis=_DIAGNOSIS,
    )


def _request(
    workspace: Path,
    projection: RoleProjection | None,
    *,
    writable: tuple[str, ...] = WRITABLE,
    deadline: float = 30.0,
) -> EditRequest:
    return EditRequest(
        attempt_id="a1",
        round=1,
        workspace=workspace,
        writable_paths=writable,
        brief=_brief(),
        deadline_seconds=deadline,
        projection=projection,
    )


def test_a_request_without_a_projection_defaults_to_none(tmp_path: Path) -> None:
    request = EditRequest("a1", 1, tmp_path, WRITABLE, _brief(), 30.0)

    assert request.projection is None


# -- command --------------------------------------------------------------------

#: One declared program: list, probe and read its workspace, write a report
#: outside it, then perform one operation and print the requested status.
PROBE = r"""
import json, os, sys, time
raw = sys.stdin.read()
request = json.loads(raw)
root = request["workspace"]
report_path, op, status = sys.argv[1], sys.argv[2], sys.argv[3]
def at(relative):
    return os.path.join(root, relative)
entries = []
for dirpath, dirnames, filenames in os.walk(root):
    for name in dirnames + filenames:
        entries.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
probes = {}
for relative in ("public/internal.md", "runtime/model.bin", "tools/run.sh",
                 "vault/inner/key.txt", "README", "runtime", "tools", "vault"):
    probes[relative] = os.path.lexists(at(relative))
reads = {}
for relative in ("docs/guide.md", "docs/deep/note.txt", "public/api/spec.md", "skills/SKILL.md"):
    if os.path.lexists(at(relative)):
        with open(at(relative)) as handle:
            reads[relative] = handle.read()
with open(report_path, "w") as handle:
    json.dump({"raw": raw, "workspace": root, "entries": sorted(entries),
               "probes": probes, "reads": reads}, handle)
def write(relative, text):
    os.makedirs(os.path.dirname(at(relative)), exist_ok=True)
    with open(at(relative), "w") as handle:
        handle.write(text)
if op in ("update", "all", "tamper", "rewrite_self"):
    write("skills/SKILL.md", "updated\n")
if op == "all":
    write("skills/new.md", "new\n")
    os.remove(at("skills/tool.py"))
if op == "readonly_update":
    write("docs/guide.md", "changed\n")
if op == "readonly_delete":
    os.remove(at("docs/guide.md"))
if op == "readonly_mode":
    os.chmod(at("docs/guide.md"), 0o700)
if op == "writable_mode":
    os.chmod(at("skills/SKILL.md"), 0o700)
if op == "hidden_create":
    write("runtime/model.bin", "replaced\n")
if op == "hidden_kind":
    write("README/x.md", "over a hidden file\n")
if op == "symlink":
    os.symlink("../docs/guide.md", at("skills/link"))
if op == "fifo":
    os.mkfifo(at("skills/pipe"))
if op == "hardlink":
    os.link(at("skills/SKILL.md"), at("skills/hard.md"))
if op == "binary":
    with open(at("skills/blob.bin"), "wb") as handle:
        handle.write(b"\xff\xfe\x00")
if op == "tamper":
    os.chmod(sys.argv[4], 0o700)
if op == "direct_write":
    with open(sys.argv[4][1:], "wb") as handle:
        handle.write(b"\xff\xfe\x00")
if op == "direct_mode":
    os.chmod(sys.argv[4][1:], 0o700)
if op == "rewrite_self":
    with open(__file__, "a") as handle:
        handle.write("# replaced\n")
if op == "deep":
    deep = at("skills")
    for _ in range(1400):
        deep = os.path.join(deep, "d")
        os.mkdir(deep)
if op == "sleep":
    time.sleep(60)
print(json.dumps({"status": status, "note": op}))
"""


def _command(
    tmp_path: Path,
    op: str,
    status: str = "edited",
    *extra: str,
    workspace: Path | None = None,
    projection: RoleProjection | None | str = "default",
    writable: tuple[str, ...] = WRITABLE,
    readable: tuple[str, ...] = READABLE,
) -> tuple[SubprocessEditor, EditRequest, Path]:
    script = tmp_path / "editor" / "main.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(PROBE, encoding="utf-8")
    report = tmp_path / "report.json"
    binding = CommandBinding(
        argv=(sys.executable, script.name, str(report), op, status, *extra),
        cwd=script.parent,
        allowed_env=frozenset(),
    )
    editor = SubprocessEditor(binding=binding, log_root=tmp_path / "run" / "editor-logs")
    root = workspace if workspace is not None else _workspace(tmp_path)
    chosen = (
        _projection(root, readable=readable, writable=writable)
        if projection == "default"
        else projection
    )
    assert chosen is None or isinstance(chosen, RoleProjection)
    return editor, _request(root, chosen, writable=writable), report


def _report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def test_command_sees_only_the_visible_set_and_publishes_onto_the_full_capture(
    tmp_path: Path,
) -> None:
    editor, request, report_path = _command(tmp_path, "all")
    workspace = request.workspace
    before = _tree(workspace)

    result = await editor.edit(request)

    assert result == EditResult("edited", "all")
    report = _report(report_path)
    # List and stat: only visible files and the directories that lead to them.
    assert report["entries"] == VISIBLE_ENTRIES
    assert report["probes"] == dict.fromkeys(report["probes"], False)
    # Read: readable-only and writable files carry the Candidate's bytes.
    assert report["reads"] == {
        "docs/guide.md": "guide\n",
        "docs/deep/note.txt": "note\n",
        "public/api/spec.md": "spec\n",
        "skills/SKILL.md": "seed\n",
    }
    # The document names the projection, never the real Attempt workspace.
    projected = Path(report["workspace"])
    assert projected != workspace
    assert workspace not in projected.parents
    assert str(workspace) not in report["raw"]
    assert str(workspace.parent) not in report["raw"]
    assert HIDDEN_MARKER not in report["raw"]
    # The projection is gone after the attempt.
    assert not projected.exists()
    # Create, update and delete reached the real Candidate; hidden entries,
    # their bytes and modes (the executable bit included) are untouched.
    after = _tree(workspace)
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"updated\n"
    assert (workspace / "skills" / "new.md").read_bytes() == b"new\n"
    assert not (workspace / "skills" / "tool.py").exists()
    assert _hidden(after) == _hidden(before)
    assert after["tools/run.sh"][1] == 0o755
    assert after["docs/guide.md"] == before["docs/guide.md"]


@pytest.mark.parametrize(
    ("op", "detail"),
    [
        ("readonly_update", "outside writable set: docs/guide.md"),
        ("readonly_delete", "outside writable set: docs/guide.md"),
        ("readonly_mode", "outside writable set: docs/guide.md"),
        ("writable_mode", "mode changed: skills/SKILL.md"),
        ("hidden_create", "outside writable set: runtime/model.bin"),
        ("symlink", "unsupported entry: skills/link"),
        ("fifo", "unsupported entry: skills/pipe"),
        ("hardlink", "unsupported entry: skills/"),
        # `admit`'s UTF-8 rule applies to the family-14 `command` Editor
        # unchanged, so it cannot publish a binary file.
        ("binary", "not UTF-8: skills/blob.bin"),
    ],
)
async def test_command_mutation_outside_its_authority_is_an_actor_violation(
    tmp_path: Path, op: str, detail: str
) -> None:
    editor, request, _report_path = _command(tmp_path, op)
    before = _tree(request.workspace)

    result = await editor.edit(request)

    assert result.status == "failed"
    assert result.code == "actor_violation"
    assert result.note.startswith(f"actor_violation {detail}")
    assert _tree(request.workspace) == before


async def test_command_directory_over_a_hidden_file_is_refused(tmp_path: Path) -> None:
    # A basename rule: `README/x.md` is writable, the file `README` is not.
    writable = ("skills/**", "*.md")
    editor, request, _report_path = _command(tmp_path, "hidden_kind", writable=writable)
    before = _tree(request.workspace)

    result = await editor.edit(request)

    assert result == EditResult(
        "failed", "actor_violation entry kind changed: README", code="actor_violation"
    )
    assert _tree(request.workspace) == before


async def test_command_with_empty_readable_paths_sees_only_writable_files(
    tmp_path: Path,
) -> None:
    editor, request, report_path = _command(tmp_path, "none", "declined", readable=())

    result = await editor.edit(request)

    assert result == EditResult("declined", "none")
    assert _report(report_path)["entries"] == ["skills", "skills/SKILL.md", "skills/tool.py"]


async def test_command_digest_mismatch_never_spawns_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    editor, request, report_path = _command(
        tmp_path, "update", workspace=workspace, projection=_projection(workspace, digest="0" * 64)
    )
    before = _tree(workspace)

    result = await editor.edit(request)

    assert result == EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
    assert not report_path.exists()
    assert _tree(workspace) == before


async def test_command_real_workspace_refusal_names_no_hidden_entry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    projection = _projection(workspace)
    os.mkfifo(workspace / "vault" / "inner" / "pipe")
    editor, request, report_path = _command(
        tmp_path, "update", workspace=workspace, projection=projection
    )

    result = await editor.edit(request)

    assert result == EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
    assert not any(name in result.note for name in (*HIDDEN_NAMES, "pipe"))
    assert not report_path.exists()


@pytest.mark.parametrize("status", ["declined", "failed"])
async def test_command_non_edited_outcome_publishes_nothing(tmp_path: Path, status: str) -> None:
    editor, request, report_path = _command(tmp_path, "update", status)
    before = _tree(request.workspace)

    result = await editor.edit(request)

    assert result.status == status
    assert _tree(request.workspace) == before
    assert not Path(_report(report_path)["workspace"]).exists()


async def test_command_concurrent_hidden_change_refuses_publication(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    hidden = workspace / "tools" / "run.sh"
    editor, request, _report_path = _command(
        tmp_path, "tamper", "edited", str(hidden), workspace=workspace
    )

    result = await editor.edit(request)

    assert result == EditResult("failed", PUBLICATION_REFUSED_NOTE, code="workspace_changed")
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"seed\n"
    assert hidden.read_bytes() == HIDDEN_FILES["tools/run.sh"][0]


@pytest.mark.parametrize(
    ("op", "target"),
    [
        ("direct_write", "skills/blob.bin"),
        ("direct_write", "vault/inner/key.txt"),
        ("direct_mode", "tools/run.sh"),
    ],
)
async def test_command_direct_real_workspace_change_is_refused_without_a_projection_diff(
    tmp_path: Path, op: str, target: str
) -> None:
    """`edited` with an empty projection diff still goes through `publish`'s check.

    The program guesses the real path (the trusted-program limit) and writes
    there, leaving its projection untouched. Publication refuses, so the
    Attempt is not taken as an edited Candidate; nothing beyond the program's
    own direct write changes in the real tree.
    """

    workspace = _workspace(tmp_path)
    editor, request, _report_path = _command(
        # `@`-prefixed: an argv entry naming an existing file is a sealed
        # binding input, and this one is meant to change.
        tmp_path,
        op,
        "edited",
        f"@{workspace / target}",
        workspace=workspace,
    )
    before = _tree(workspace)

    result = await editor.edit(request)

    assert result == EditResult("failed", PUBLICATION_REFUSED_NOTE, code="workspace_changed")
    after = _tree(workspace)
    assert {path: value for path, value in after.items() if path != target} == {
        path: value for path, value in before.items() if path != target
    }


async def test_command_changed_during_the_run_publishes_nothing(tmp_path: Path) -> None:
    editor, request, _report_path = _command(tmp_path, "rewrite_self")
    before = _tree(request.workspace)

    result = await editor.edit(request)

    assert result == EditResult("failed", "command_changed", code="command_changed")
    assert _tree(request.workspace) == before


async def test_command_nesting_deeper_than_the_recursion_limit_is_a_classified_failure(
    tmp_path: Path,
) -> None:
    """A program that nests 1,400 directories deep in its projection publishes nothing.

    `admit`'s recursive no-follow capture holds one descriptor per level, so
    it stops at whichever comes first: the recursion limit (`RecursionError`,
    classified by the publication catch-all as `exception:RecursionError`) or
    the open-file limit (a refused entry, `actor_violation`). Either way the
    projection is then removed without re-raising (on Python 3.11 the
    recursive `os.walk`/`shutil.rmtree` cleanup used to, H11 review A1).
    """

    editor, request, report_path = _command(tmp_path, "deep")
    before = _tree(request.workspace)

    result = await editor.edit(request)

    assert result.status == "failed"
    # The capture's own failures only: a directory it could not open names
    # the nested `d` path itself, a directory it could not list names
    # nothing. A refused symlink or FIFO would name another entry.
    assert (result.code, result.note) == ("exception", "exception:RecursionError") or (
        result.code == "actor_violation"
        and (
            re.fullmatch(r"actor_violation unsupported entry: skills(/d){100,}", result.note)
            or result.note
            == "actor_violation unsupported entry: cannot inspect Candidate directory"
        )
    )
    assert _tree(request.workspace) == before
    projected = Path(_report(report_path)["workspace"])
    assert not projected.exists()
    assert not projected.parent.exists()


async def test_command_cancellation_discards_the_projection(tmp_path: Path) -> None:
    editor, request, report_path = _command(tmp_path, "sleep")
    before = _tree(request.workspace)
    task = asyncio.ensure_future(editor.edit(request))
    deadline = time.monotonic() + 15.0
    while not report_path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)  # the report is fully written before the sleep starts
    projected = Path(_report(report_path)["workspace"])
    assert projected.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not projected.exists()
    assert _tree(request.workspace) == before


async def test_command_admits_with_the_capture_ceilings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.editor import subprocess_editor

    seen: list[Budgets] = []
    real = subprocess_editor.admit

    def spy(projection: Any, *, writable_paths: Any, budgets: Budgets) -> Any:
        seen.append(budgets)
        return real(projection, writable_paths=writable_paths, budgets=budgets)

    monkeypatch.setattr(subprocess_editor, "admit", spy)
    editor, request, _report_path = _command(tmp_path, "update")

    assert (await editor.edit(request)).status == "edited"
    assert seen == [Budgets(None, MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES)]


async def test_command_written_byte_refusal_carries_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.editor import subprocess_editor

    monkeypatch.setattr(subprocess_editor, "PROJECTED_BUDGETS", Budgets(None, 4, MAX_CAPTURE_BYTES))
    editor, request, _report_path = _command(tmp_path, "update")

    result = await editor.edit(request)

    assert (result.status, result.code) == ("failed", "written_byte_budget")
    assert result.note.startswith("actor_violation")


async def test_command_edited_without_a_projection_change_publishes_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`edited` with an empty projection diff on an untouched real workspace.

    `publish` still runs (it is the real-workspace check) and succeeds with no
    change, and the program's own result is returned as is: the campaign then
    closes the unchanged Candidate `abandoned(editor_no_change)`, as it closes
    any other no-change edit.
    """

    from evolution.tree.roles.editor import subprocess_editor

    published: list[tuple[Any, ...]] = []
    real = subprocess_editor.publish

    def spy(admitted: Any, projection: Any) -> None:
        published.append(tuple(admitted))
        real(admitted, projection)

    monkeypatch.setattr(subprocess_editor, "publish", spy)
    editor, request, report_path = _command(tmp_path, "none", "edited")
    before = _tree(request.workspace)

    result = await editor.edit(request)

    assert result == EditResult("edited", "none")
    assert published == [()]
    assert _tree(request.workspace) == before
    assert not Path(_report(report_path)["workspace"]).exists()


async def test_command_request_without_projection_is_unchanged(tmp_path: Path) -> None:
    """Families up to 13: the real workspace, the same document bytes, no admission."""

    editor, request, report_path = _command(tmp_path, "binary", projection=None)
    workspace = request.workspace

    result = await editor.edit(request)

    assert result == EditResult("edited", "binary")
    report = _report(report_path)
    expected = {
        "version": REQUEST_VERSION,
        "attempt_id": "a1",
        "round": 1,
        "workspace": str(workspace),
        "writable_paths": list(WRITABLE),
        "deadline_seconds": 30.0,
        "brief": brief_payload(request.brief),
    }
    assert report["raw"] == json.dumps(expected, ensure_ascii=False) + "\n"
    assert report["workspace"] == str(workspace)
    assert set(HIDDEN_ENTRIES) <= set(report["entries"])
    # No admission: a binary write lands in the real Candidate in place.
    assert (workspace / "skills" / "blob.bin").read_bytes() == b"\xff\xfe\x00"


# -- chat -----------------------------------------------------------------------


def _tool_outputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool":
            outputs.append(json.loads(message["content"]))
    return outputs


def _chat_plan(*changes: tuple[str, str]) -> Any:
    return _response(
        "record_plan",
        json.dumps(
            {
                "summary": "smallest useful change",
                "changes": [
                    {"path": path, "action": action, "purpose": "repair behavior"}
                    for path, action in changes
                ],
            }
        ),
    )


async def test_chat_tools_see_only_the_visible_set_and_publish_onto_the_full_capture(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    before = _tree(workspace)
    editor, client = _chat_editor(
        tmp_path,
        [
            _response("list_files"),  # the preflight
            _response("list_files"),
            _response("read_file", '{"path":"docs/guide.md"}'),
            _response("read_file", '{"path":"vault/inner/key.txt"}'),
            _response("inspect_file", '{"path":"tools/run.sh"}'),
            _chat_plan(
                ("skills/SKILL.md", "update"),
                ("skills/new.md", "create"),
                ("skills/tool.py", "delete"),
            ),
            _response("write_file", '{"path":"skills/SKILL.md","content":"updated\\n"}'),
            _response("write_file", '{"path":"skills/new.md","content":"new\\n"}'),
            _response("delete_file", '{"path":"skills/tool.py"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ],
    )

    result = await editor.edit(_request(workspace, _projection(workspace)))

    assert (result.status, result.note) == ("edited", "done")
    outputs = _tool_outputs(client.calls[-1]["messages"])
    listing = next(output for output in outputs if "files" in output)
    assert listing["files"] == sorted(VISIBLE_FILES)
    reads = [output for output in outputs if output.get("path") == "docs/guide.md"]
    assert reads and reads[0]["content"] == "guide\n"
    # Read and stat of a hidden path fail exactly as an absent path does.
    assert {"error": "cannot read file: key.txt."} in outputs
    assert {"error": "cannot read file: run.sh."} in outputs
    # The hidden probes failed; no hidden byte reached the provider.
    transcript = json.dumps(client.calls[-1]["messages"])
    assert HIDDEN_MARKER not in transcript
    for name in ("internal.md", "model.bin"):
        assert name not in transcript
    after = _tree(workspace)
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"updated\n"
    assert (workspace / "skills" / "new.md").read_bytes() == b"new\n"
    assert not (workspace / "skills" / "tool.py").exists()
    assert _hidden(after) == _hidden(before)
    assert after["docs/guide.md"] == before["docs/guide.md"]


async def test_chat_tools_refuse_read_only_and_hidden_writes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = _tree(workspace)
    editor, _client = _chat_editor(
        tmp_path,
        [
            _response("list_files"),
            _plan(),
            _response("write_file", '{"path":"docs/guide.md","content":"changed\\n"}'),
            _response("write_file", '{"path":"runtime/model.bin","content":"replaced\\n"}'),
            _response("finish_edit", '{"summary":"nothing admissible"}'),
        ],
    )

    result = await editor.edit(_request(workspace, _projection(workspace)))

    assert result.status == "declined"
    assert _tree(workspace) == before


async def test_chat_digest_mismatch_never_calls_the_provider(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = _tree(workspace)
    editor, client = _chat_editor(tmp_path, [_response("list_files")])

    result = await editor.edit(_request(workspace, _projection(workspace, digest="0" * 64)))

    assert result == EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
    assert client.calls == []
    assert _tree(workspace) == before


async def test_chat_real_workspace_refusal_names_no_hidden_entry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    projection = _projection(workspace)
    os.mkfifo(workspace / "vault" / "inner" / "pipe")
    editor, client = _chat_editor(tmp_path, [_response("list_files")])

    result = await editor.edit(_request(workspace, projection))

    assert result == EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
    assert client.calls == []


async def test_chat_concurrent_hidden_change_refuses_publication(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    hidden = workspace / "tools" / "run.sh"

    class _TamperingClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            response = await super().create(**kwargs)
            if len(self.calls) == 4:
                os.chmod(hidden, 0o700)
            return response

    client = _TamperingClient(
        [
            _response("list_files"),
            _chat_plan(("skills/SKILL.md", "update")),
            _response("write_file", '{"path":"skills/SKILL.md","content":"updated\\n"}'),
            _response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor, _client = _chat_editor(tmp_path, client=client)

    result = await editor.edit(_request(workspace, _projection(workspace)))

    assert result == EditResult("failed", PUBLICATION_REFUSED_NOTE, code="workspace_changed")
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"seed\n"


async def test_chat_request_without_projection_is_unchanged(tmp_path: Path) -> None:
    """Families up to 13: the whole Candidate is listed, and refusals keep their text."""

    workspace = _workspace(tmp_path)
    editor, client = _chat_editor(
        tmp_path,
        [
            _response("list_files"),
            _response("list_files"),
            _plan(),
            _response("finish_edit", '{"summary":"nothing"}'),
        ],
    )

    result = await editor.edit(_request(workspace, None))

    assert result.status == "declined"
    listing = next(
        output for output in _tool_outputs(client.calls[-1]["messages"]) if "files" in output
    )
    # Every UTF-8 file, the hidden ones included (`readable_files` skips binaries).
    assert listing["files"] == sorted(
        [*VISIBLE_FILES, *(path for path in HIDDEN_FILES if path != "runtime/model.bin")]
    )

    os.mkfifo(workspace / "vault" / "inner" / "pipe")
    editor, _client = _chat_editor(tmp_path / "second", [_response("list_files")])
    refused = await editor.edit(_request(workspace, None))

    assert refused == EditResult(
        "failed",
        "workspace_changed Candidate workspace holds an unsupported entry: "
        f"{workspace / 'vault' / 'inner' / 'pipe'}",
        code="workspace_changed",
    )


# -- agent_cli ------------------------------------------------------------------


def _observe(seen: dict[str, Any], *, then: Any = None) -> Any:
    """A fake CLI call that records what it can list, stat and read, then edits."""

    def edit(work_dir: Path) -> None:
        entries = []
        for dirpath, dirnames, filenames in os.walk(work_dir):
            for name in (*dirnames, *filenames):
                relative = (Path(dirpath) / name).relative_to(work_dir).as_posix()
                if relative == CONTROL_PREFIX or relative.startswith(f"{CONTROL_PREFIX}/"):
                    continue
                entries.append(relative)
        seen["work_dir"] = work_dir
        seen["entries"] = sorted(entries)
        seen["probes"] = {path: os.path.lexists(work_dir / path) for path in HIDDEN_ENTRIES}
        seen["request"] = (work_dir / CONTROL_PREFIX / "EDIT_REQUEST.json").read_text(
            encoding="utf-8"
        )
        seen["reads"] = {
            path: (work_dir / path).read_bytes()
            for path in VISIBLE_FILES
            if path != "skills/tool.py"
        }
        if then is not None:
            then(work_dir)

    return edit


def _agent_changes(work_dir: Path) -> None:
    (work_dir / "skills" / "SKILL.md").write_text("updated\n", encoding="utf-8")
    (work_dir / "skills" / "new.md").write_text("new\n", encoding="utf-8")
    (work_dir / "skills" / "tool.py").unlink()


async def test_agent_cli_sees_only_the_visible_set_and_publishes_onto_the_full_capture(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    before = _tree(workspace)
    seen: dict[str, Any] = {}
    editor, instances = _agent_editor(tmp_path, [_observe(seen, then=_agent_changes)])

    result = await editor.edit(_request(workspace, _projection(workspace)))

    assert result.status == "edited"
    assert len(instances) == 1
    assert seen["entries"] == VISIBLE_ENTRIES
    assert seen["probes"] == dict.fromkeys(HIDDEN_ENTRIES, False)
    assert str(workspace) not in seen["request"]
    assert HIDDEN_MARKER not in seen["request"]
    assert seen["reads"]["docs/guide.md"] == b"guide\n"
    assert not seen["work_dir"].exists()
    after = _tree(workspace)
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"updated\n"
    assert (workspace / "skills" / "new.md").read_bytes() == b"new\n"
    assert not (workspace / "skills" / "tool.py").exists()
    assert _hidden(after) == _hidden(before)
    assert after["tools/run.sh"][1] == 0o755


def _readonly_write(work_dir: Path) -> None:
    (work_dir / "docs" / "guide.md").write_text("changed\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (_readonly_write, "outside writable set: docs/guide.md"),
        ("symlink", "unsupported entry: skills/link"),
    ],
)
async def test_agent_cli_mutation_outside_its_authority_is_an_actor_violation(
    tmp_path: Path, change: Any, detail: str
) -> None:
    workspace = _workspace(tmp_path)
    before = _tree(workspace)
    if change == "symlink":
        # A link aimed at the real hidden file: the CLI guessed the path.
        def change(work_dir: Path) -> None:
            (work_dir / "skills" / "link").symlink_to(workspace / "vault" / "inner" / "key.txt")

    editor, _instances = _agent_editor(tmp_path, [change])

    result = await editor.edit(_request(workspace, _projection(workspace)))

    assert result == EditResult("failed", f"actor_violation {detail}", code="actor_violation")
    assert _tree(workspace) == before


async def test_agent_cli_hidden_control_prefix_still_collides(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / CONTROL_PREFIX).mkdir()
    (workspace / CONTROL_PREFIX / "planted.txt").write_text("planted\n", encoding="utf-8")
    projection = _projection(workspace)
    assert not projection.visible(f"{CONTROL_PREFIX}/planted.txt")
    editor, instances = _agent_editor(tmp_path, [_agent_changes])

    result = await editor.edit(_request(workspace, projection))

    assert result.status == "failed"
    assert result.code == "control_collision"
    assert instances == []


async def test_agent_cli_digest_mismatch_never_runs_the_cli(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    before = _tree(workspace)
    editor, instances = _agent_editor(tmp_path, [_agent_changes])

    result = await editor.edit(_request(workspace, _projection(workspace, digest="0" * 64)))

    assert result == EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
    assert instances == []
    assert _tree(workspace) == before


async def test_agent_cli_real_workspace_refusal_names_no_hidden_entry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    projection = _projection(workspace)
    os.mkfifo(workspace / "vault" / "inner" / "pipe")
    editor, instances = _agent_editor(tmp_path, [_agent_changes])

    result = await editor.edit(_request(workspace, projection))

    assert result == EditResult("failed", PROJECTION_REFUSED_NOTE, code="workspace_changed")
    assert instances == []


async def test_agent_cli_concurrent_hidden_change_refuses_publication(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    hidden = workspace / "tools" / "run.sh"

    def change(work_dir: Path) -> None:
        _agent_changes(work_dir)
        os.chmod(hidden, 0o700)

    editor, _instances = _agent_editor(tmp_path, [change])

    result = await editor.edit(_request(workspace, _projection(workspace)))

    assert result == EditResult("failed", PUBLICATION_REFUSED_NOTE, code="workspace_changed")
    assert (workspace / "skills" / "SKILL.md").read_bytes() == b"seed\n"
    assert (workspace / "skills" / "tool.py").exists()


async def test_agent_cli_request_without_projection_is_unchanged(tmp_path: Path) -> None:
    """Families up to 13: the CLI sees the whole Candidate."""

    workspace = _workspace(tmp_path)
    full_listing = sorted(path for path in _tree(workspace) if path != ".")
    seen: dict[str, Any] = {}
    editor, _instances = _agent_editor(tmp_path, [_observe(seen)])

    result = await editor.edit(_request(workspace, None))

    assert result.status == "declined"
    assert seen["entries"] == full_listing
    assert seen["probes"] == dict.fromkeys(HIDDEN_ENTRIES, True)
