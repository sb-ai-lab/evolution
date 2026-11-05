"""H11 Task 4: genuine family-14 campaigns whose `command` Editor runs on the role projection.

The seed holds four kinds of included file:

- `skills/SKILL.md`, writable (the fixture's `skills/**`);
- `prompts/system.md`, readable through `role_visibility.readable_paths`;
- `runtime/model.bin`, a hidden binary file that is also a declared dependency;
- `tools/run.sh`, a hidden executable.

The Editor script records what it sees (path, kind, executable bit, sha256) and
then makes the usual one-line edit. The Target adapter checks, at every
materialization, that the loaded tree still holds both hidden files with their
bytes and executable bit, and fails every turn of an instance that does not.

Per H11 routine choice 10 the shared drivers stay untouched: the seed, the
Editor script and the `role_visibility` declaration reach the run through
`monkeypatch` of `tests.test_tree_campaign_family12.write_workspace` and
`tests.test_tree_campaign_family14_run.family14_document`.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from evolution.tree.campaign import CampaignInvariantViolated, _Campaign
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor_contracts import brief_digest
from evolution.tree.roles.role_visibility import RoleVisibilityView
from evolution.tree.roles.workspace import capture_tree, role_projection_digest
from tests.campaign_drivers import EDITED, TWO_ATTEMPTS, _cut_hook, _records
from tests.campaign_fakes import Crash, FakeAdapter, RolloutInfrastructureError
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

SKILL = b"seed skill\n"
SYSTEM = b"system prompt\n"
MODEL = bytes(range(256)) * 4
RUN_SH = b"#!/bin/sh\necho run\n"
WRITABLE = ("skills/**",)
READABLE = ("prompts/system.md",)
HIDDEN = ("runtime/model.bin", "tools/run.sh")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _visible_entries(readable: tuple[str, ...]) -> list[dict[str, object]]:
    """The listing the Editor must see, in `role_projection/1` entry shape."""

    entries: list[dict[str, object]] = [
        {"path": "skills", "kind": "directory"},
        {"path": "skills/SKILL.md", "kind": "file", "executable": False, "sha256": _sha(SKILL)},
    ]
    if "prompts/system.md" in readable:
        entries += [
            {"path": "prompts", "kind": "directory"},
            {
                "path": "prompts/system.md",
                "kind": "file",
                "executable": False,
                "sha256": _sha(SYSTEM),
            },
        ]
    return sorted(entries, key=lambda entry: str(entry["path"]))


def _listing_digest(entries: list[dict[str, object]]) -> str:
    return hashlib.sha256(
        canonical_json({"version": "role_projection/1", "entries": entries})
    ).hexdigest()


#: The Editor: record the workspace listing, run `ACTION`, then the usual edit.
EDITOR = r"""
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
ACTION
skill = root / "skills" / "SKILL.md"
skill.write_text(skill.read_text(encoding="utf-8").rstrip("\n") + " edited\n", encoding="utf-8")
print(json.dumps({"status": "edited", "note": "n"}))
"""

ACTIONS = {
    "none": "",
    "hidden_write": (
        '(root / "tools").mkdir(exist_ok=True)\n(root / "tools" / "run.sh").write_text("pwned\\n")\n'
    ),
    "chmod_visible": 'os.chmod(root / "prompts" / "system.md", 0o755)\n',
    "symlink": 'os.symlink("../prompts/system.md", root / "skills" / "link.md")\n',
}


def _hidden_state(root: Path) -> dict[str, object]:
    model = root / "runtime" / "model.bin"
    script = root / "tools" / "run.sh"
    return {
        "model": model.read_bytes() if model.is_file() else None,
        "run_sh": script.read_bytes() if script.is_file() else None,
        "run_sh_executable": bool(script.stat().st_mode & stat.S_IXUSR)
        if script.is_file()
        else None,
    }


HIDDEN_INTACT = {"model": MODEL, "run_sh": RUN_SH, "run_sh_executable": True}


class _HiddenAware(FakeAdapter):
    """Records each loaded tree's hidden files; fails every turn when they are not intact.

    A real Target that needs `runtime/model.bin` would fail its turns without
    it; here that is a contract failure, which stops the campaign, so a run
    that finishes `completed` proves every instance loaded the hidden files.
    """

    def __init__(self) -> None:
        super().__init__(used=EDITED["used"])
        self.loaded: list[tuple[str, Path, dict[str, object]]] = []

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        state = _hidden_state(Path(candidate.root))
        self.loaded.append((candidate.kind, Path(candidate.root), state))
        if state != HIDDEN_INTACT:

            async def refuse(session, message, request_id):
                raise RolloutInfrastructureError("runtime/model.bin is missing", "invalid_request")

            instance.send = refuse
        return instance


def _arrange(
    tmp_path, monkeypatch, *, readable=READABLE, action="none", family14=True, listing=None
):
    """Patch the seed, the Editor script and `role_visibility` for one run."""

    from tests import campaign_drivers as fixtures
    from tests import test_tree_campaign_family14_run as driver

    listing = tmp_path / "listing.jsonl" if listing is None else listing
    editor = EDITOR.replace("LISTING", repr(str(listing))).replace("ACTION\n", ACTIONS[action])
    workspace = fixtures.write_workspace

    def write_workspace(root, *args, **kwargs):
        path = workspace(root, *args, **{**kwargs, "editor_script": editor})
        seed = Path(root) / "seed"
        (seed / "prompts").mkdir(exist_ok=True)
        (seed / "prompts" / "system.md").write_bytes(SYSTEM)
        (seed / "runtime").mkdir(exist_ok=True)
        (seed / "runtime" / "model.bin").write_bytes(MODEL)
        (seed / "tools").mkdir(exist_ok=True)
        (seed / "tools" / "run.sh").write_bytes(RUN_SH)
        (seed / "tools" / "run.sh").chmod(0o755)
        return path

    monkeypatch.setattr(fixtures, "write_workspace", write_workspace)
    if family14:
        document = driver.family14_document

        def family14_document(*args, **kwargs):
            moved = document(*args, **kwargs)
            moved["role_visibility"]["readable_paths"] = list(readable)
            moved["artifacts"]["dependency_paths"] = ["runtime/model.bin"]
            return moved

        monkeypatch.setattr(driver, "family14_document", family14_document)
    return listing


def _listings(listing: Path) -> list[list[dict[str, object]]]:
    if not listing.exists():
        return []
    return [json.loads(line) for line in listing.read_text().splitlines()]


def _spy_measurement_roots(monkeypatch) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    original = _Campaign.candidate_ref

    def candidate_ref(self, measurement_id):
        ref = original(self, measurement_id)
        roots[measurement_id] = Path(ref.root)
        return ref

    monkeypatch.setattr(_Campaign, "candidate_ref", candidate_ref)
    return roots


def _spy_briefs(monkeypatch) -> list[str]:
    digests: list[str] = []
    original = _Campaign.brief_for

    def brief_for(self, attempt):
        brief = original(self, attempt)
        digests.append(brief_digest(brief))
        return brief

    monkeypatch.setattr(_Campaign, "brief_for", brief_for)
    return digests


def _parent_projection_digest(run: Path, readable: tuple[str, ...]) -> str:
    """`D` recomputed by the test from the published seed Champion."""

    (champion,) = sorted((run / "artifacts" / "champions").iterdir())[:1]
    view = RoleVisibilityView(readable_paths=readable, writable_paths=WRITABLE)
    return role_projection_digest(capture_tree(champion), view.visible)


def _expected_common(brief: str, projection: str) -> str:
    return hashlib.sha256(
        canonical_json({"brief": brief, "role_projection": projection})
    ).hexdigest()


async def test_the_editor_sees_only_the_visible_set_and_the_target_every_file(
    tmp_path, monkeypatch
):
    listing = _arrange(tmp_path, monkeypatch)
    roots = _spy_measurement_roots(monkeypatch)
    briefs = _spy_briefs(monkeypatch)
    adapter = _HiddenAware()
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert_clean_run(run, snapshot)

    # The Editor's workspace is exactly the visible set.
    (seen,) = _listings(listing)
    expected = _visible_entries(READABLE)
    assert seen == expected
    assert not any(entry["path"].startswith(("runtime", "tools")) for entry in seen)

    # The frozen common input binds `D`, which equals the Editor's own view.
    projection = _parent_projection_digest(run, READABLE)
    assert _listing_digest(seen) == projection
    (frozen,) = payloads(run, "attempt_input_frozen")
    assert frozen["common_input_digest"] == _expected_common(briefs[0], projection)
    (started,) = payloads(run, "editor_started")
    assert started["common_input_digest"] == frozen["common_input_digest"]

    # The Target loaded both hidden files for every measurement: the
    # baseline Champion, the Attempt's Candidate and both final members.
    finals = payloads(run, "finalists_bound")[0]["members"]
    measured = {"m-r00-champion", "m-r01-a01", *finals}
    assert measured <= set(roots), sorted(roots)
    loaded = {root: state for _, root, state in adapter.loaded}
    for measurement in measured:
        assert loaded[roots[measurement]] == HIDDEN_INTACT, measurement
    assert {kind for kind, _, _ in adapter.loaded} == {"champion", "candidate"}

    # The published Candidate keeps the hidden bytes, modes and executable bit.
    candidate = run / "artifacts" / "candidates" / "r01-a01"
    champion = roots["m-r00-champion"]
    assert _hidden_state(candidate) == HIDDEN_INTACT
    for relative in HIDDEN:
        assert stat.S_IMODE((candidate / relative).stat().st_mode) == stat.S_IMODE(
            (champion / relative).stat().st_mode
        )
    assert (candidate / "skills" / "SKILL.md").read_bytes() == b"seed skill edited\n"
    assert (candidate / "prompts" / "system.md").read_bytes() == SYSTEM


async def test_target_adapter_fails_a_turn_without_the_hidden_dependency(tmp_path):
    """The control for `_HiddenAware`: an instance over a tree without the
    hidden dependency fails its turn, so a completed run proves the Target saw it."""

    adapter = _HiddenAware()
    tree = tmp_path / "tree"
    (tree / "tools").mkdir(parents=True)
    (tree / "tools" / "run.sh").write_bytes(RUN_SH)

    class Ref:
        kind = "champion"
        artifact_id = "seed"
        root = tree
        digest = "0" * 64
        dependency_digest = "1" * 64

    instance = adapter.instance_for(Ref, "i-1", "boot-1")
    with pytest.raises(RolloutInfrastructureError, match="model.bin"):
        await instance.send(None, None, "r-1")
    assert adapter.loaded[0][2]["model"] is None


async def test_visibility_changes_role_input_identity_not_artifact_identity(tmp_path, monkeypatch):
    results = {}
    for readable in (READABLE, ()):
        base = tmp_path / ("readable" if readable else "writable-only")
        base.mkdir()
        with monkeypatch.context() as patch:
            listing = _arrange(base, patch, readable=readable)
            briefs = _spy_briefs(patch)
            run, snapshot = await family14_run(base, patch, adapter=_HiddenAware())
            assert snapshot.finished["outcome"] == "completed"
            assert_clean_run(run, snapshot)
            (seen,) = _listings(listing)
            assert seen == _visible_entries(readable)
            projection = _parent_projection_digest(run, readable)
            (frozen,) = payloads(run, "attempt_input_frozen")
            assert frozen["common_input_digest"] == _expected_common(briefs[0], projection)
            (sealed,) = payloads(run, "attempt_sealed")
            (created,) = [
                row["payload"]
                for row in _records(run / "artifacts.jsonl")
                if row["event"] == "store_created"
            ]
            results[readable] = {
                "candidate": (sealed["candidate_digest"], sealed["dependency_digest"]),
                "seed": (
                    created["identity"]["seed_candidate_digest"],
                    created["identity"]["seed_dependency_digest"],
                ),
                "projection": projection,
                "common": frozen["common_input_digest"],
                "brief": briefs[0],
            }
    with_readable, without = results[READABLE], results[()]
    assert with_readable["candidate"] == without["candidate"]
    assert with_readable["seed"] == without["seed"]
    assert with_readable["projection"] != without["projection"]
    assert with_readable["common"] != without["common"]


async def test_every_attempt_of_a_round_freezes_one_common_input(tmp_path, monkeypatch):
    listing = _arrange(tmp_path, monkeypatch)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, adapter=_HiddenAware(), campaign={**TWO_ATTEMPTS}
    )
    assert snapshot.finished["outcome"] == "completed"
    frozen = payloads(run, "attempt_input_frozen")
    assert len(frozen) == 2
    assert len({row["common_input_digest"] for row in frozen}) == 1
    assert _listings(listing) == [_visible_entries(READABLE)] * 2
    assert_clean_run(run, snapshot)


async def test_a_resume_after_the_freeze_recomputes_the_same_projection(tmp_path, monkeypatch):
    listing = _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, adapter=adapter)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    assert not payloads(run, "editor_started")
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "attempt_input_frozen") == [frozen]
    (started,) = payloads(run, "editor_started")
    assert started["common_input_digest"] == frozen["common_input_digest"]
    assert _listings(listing) == [_visible_entries(READABLE)]
    assert_clean_run(run, snapshot)


async def test_a_changed_projection_on_resume_breaks_the_frozen_input(tmp_path, monkeypatch):
    _arrange(tmp_path, monkeypatch)
    adapter = _HiddenAware()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, adapter=adapter)
    run = tmp_path / "run"
    before = (run / "campaign.jsonl").read_bytes()
    monkeypatch.setattr(_Campaign, "_role_projection_digest", lambda self, parent: "f" * 64)
    with pytest.raises(CampaignInvariantViolated, match="frozen editor common input changed"):
        await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert not payloads(run, "editor_started")
    assert len(payloads(run, "attempt_input_frozen")) == 1
    assert (run / "campaign.jsonl").read_bytes()[: len(before)] == before


@pytest.mark.parametrize(
    "action,detail",
    [
        ("hidden_write", "outside writable set: tools/run.sh"),
        ("chmod_visible", "outside writable set: prompts/system.md"),
        ("symlink", "unsupported entry"),
    ],
)
async def test_an_editor_that_crosses_the_projection_is_an_actor_violation(
    tmp_path, monkeypatch, action, detail
):
    from evolution.tree import campaign as campaign_module
    from evolution.tree.roles.editor_contracts import note_digest

    notes = []
    real_run_editor = campaign_module.run_editor

    async def run_editor(editor, request, **kwargs):
        outcome = await real_run_editor(editor, request, **kwargs)
        notes.append(outcome.note)
        return outcome

    monkeypatch.setattr(campaign_module, "run_editor", run_editor)
    listing = _arrange(tmp_path, monkeypatch, action=action)
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=_HiddenAware())
    (failed,) = payloads(run, "editor_failed")
    assert failed["reason"] == "actor_violation"
    (note,) = notes
    assert note.startswith("actor_violation") and detail in note
    assert failed["note_digest"] == note_digest(note)
    (closed,) = payloads(run, "attempt_closed")
    assert (closed["outcome"], closed["reason"]) == ("abandoned", "editor_failed")
    assert not payloads(run, "attempt_sealed")
    assert not (run / "artifacts" / "candidates" / "r01-a01").exists()
    assert _listings(listing) == [_visible_entries(READABLE)]
    for champion in (run / "artifacts" / "champions").iterdir():
        assert _hidden_state(champion) == HIDDEN_INTACT
    assert snapshot.finished is not None


def _tamper_content(workspace: Path) -> None:
    target = workspace / "runtime" / "model.bin"
    target.chmod(0o644)
    target.write_bytes(b"tampered")


def _tamper_mode(workspace: Path) -> None:
    (workspace / "tools" / "run.sh").chmod(0o644)


@pytest.mark.parametrize("tamper", [_tamper_content, _tamper_mode], ids=["content", "mode"])
async def test_a_hidden_file_tampered_in_the_attempt_workspace_is_refused_at_the_seal(
    tmp_path, monkeypatch, tamper
):
    """A hidden file changed in the Attempt workspace behind the Editor's back
    (after materialization, before the Editor projects) is invisible to the
    Editor and to `admit`, so the Editor's own work publishes; the store's seal
    then refuses the non-writable difference against the Parent."""

    listing = _arrange(tmp_path, monkeypatch)
    original = _Campaign.hook

    def hook(self, name):
        if name == "after_editor_started":
            for workspace in self.workspaces.values():
                tamper(Path(workspace))
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=_HiddenAware())
    # The Editor never saw the hidden file, so its own work is admitted ...
    assert _listings(listing) == [_visible_entries(READABLE)]
    (finished,) = payloads(run, "editor_finished")
    assert finished["status"] == "edited"
    # ... and the seal refuses the non-writable difference the tamper made.
    (closed,) = payloads(run, "attempt_closed")
    assert (closed["outcome"], closed["reason"], closed["code"]) == (
        "abandoned",
        "seal_refused",
        "artifact_unsafe",
    )
    assert not payloads(run, "attempt_sealed")
    assert not [r for r in _records(run / "artifacts.jsonl") if r["event"].startswith("promotion_")]
    for champion in (run / "artifacts" / "champions").iterdir():
        assert _hidden_state(champion) == HIDDEN_INTACT
    assert snapshot.finished is not None
