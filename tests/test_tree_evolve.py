"""Contracts for the W1 orchestrator, `evolution.tree.evolve` (spec §6).

The seven ordered steps, every refusal that precedes them, and the two end-to-end
compositions: over C1's in-memory `FakeAdapter` and over the real
`HttpSessionAdapter` against a served reference target.

The fixture programs are the smallest pair that makes a promotion real. The
editor appends ` edited` to the first (and only) line of `skills/SKILL.md`; the
scorer scores `1.0` when `edited` is visible to it and `0.0` otherwise, and
states the same fact as its one `answered` verdict -- which is what gives the
round's focus a failure mode to chase, and so what lets an attempt open at all
(v2 Reflector X1 §3.3). It looks
in two places on purpose: the serialized `view` — which is where the reference
target puts it, since a turn's answer opens with the attested skill line — and
the Candidate's own `skills/SKILL.md` under `EVO_CANDIDATE_ROOT`, which is the
only place the in-memory `FakeAdapter` can show it (its `send` echoes the
Environment's message and knows nothing of the tree). One scorer, both adapters,
no branch inside the fixture.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shlex
import shutil
import stat
import sys
import threading
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from typer.testing import CliRunner

import evolution.tree.evolve as evolve_module
import evolution.tree.roles.editor.agent_editor as agent_editor_module
import evolution.tree.roles.reflector.agent_reflector as agent_reflector_module
import tests.fake_chat_server as fake_chat_server
from evolution.eval.child_env import _RUNTIME_ENV_KEYS
from evolution.llm.agent_backend import CURATED_ENV_NAMES
from evolution.tree.bindings import build_scorer
from evolution.tree.campaign import (
    CAMPAIGN_FILE,
    CAMPAIGN_LOCK,
    ArtifactLedgerMissing,
    AuthorizedIdentityChanged,
    CampaignInvariantViolated,
    CampaignResult,
    PolicyContradictsPrefix,
    ReboundSourceCorrupt,
    UndeclaredSibling,
)
from evolution.tree.campaign_identity import CapabilitiesInsufficient
from evolution.tree.cli import tree_app
from evolution.tree.evolve import (
    EvolveRefused,
    _canonical_run_dir,
    admit_output_root,
    canonical_run_dir_finding,
    check_path_separation,
    derive_run_id,
    evolve,
    exit_code_for,
    run_dir_state,
)
from evolution.tree.execution.authorizations import (
    Authorization,
    AuthorizationConflict,
    AuthorizationError,
    RefusalDifference,
    hygiene_instance_digest,
    read_decisions,
    refusal_class,
    write_decision,
)
from evolution.tree.execution.check_modes import ENFORCE_ALL
from evolution.tree.execution.interaction import (
    CandidateView,
    EvalCase,
    History,
    ScoringView,
    SemanticIdentity,
)
from evolution.tree.execution.journal import MAX_JOURNAL_BYTES
from evolution.tree.execution.journal_identity import IdentityError
from evolution.tree.execution.output_admission import HygieneFinding
from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault
from evolution.tree.execution.wire_v2 import MAX_RUN_ID, new_instance_id
from evolution.tree.manifest.manifest_v2 import (
    SCHEMA_V15,
    load_manifest_v2,
)
from evolution.tree.storage.append_log import AppendLog
from evolution.tree.storage.journal_errors import JournalFault
from evolution.tree.target.reference_target_http import serve
from evolution.tree.target.target_calls import TargetCallLedger
from tests.campaign_fakes import FakeAdapter
from tests.reference_target_faults import (
    FaultInjectingReferenceTarget,
)
from tests.tree_evolve_support import (
    CAMPAIGN_PAYLOAD,
    EDITOR_SCRIPT,
    POLICY,
    REJECTING_SCORER_SCRIPT,
    SCORER_SCRIPT,
    WRITABLE,
    write_workspace,
)

pytestmark = pytest.mark.slow


def write_v1_manifest(root: Path) -> Path:
    """The same workspace declared as `/1`: four keys, no workflow."""

    document = {
        "schema": "evolution-v2-manifest/1",
        "seed_root": "seed",
        "target": {"base_url": "http://127.0.0.1:8765", "seed_mode": "exact"},
        "artifacts": {
            "excludes": [],
            "dependency_paths": [],
            "writable_paths": list(WRITABLE),
        },
    }
    path = root / "manifest-v1.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def fake_adapters() -> Any:
    """The default `adapter_factory` for the in-memory compositions."""

    return lambda manifest, run_id: FakeAdapter()


def listing(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every entry under `root`, with the `lstat` fields a write would change."""

    entries: dict[str, tuple[int, int, int]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        entries[str(path.relative_to(root.parent))] = (
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
        )
    return entries


def result_payload(result: CampaignResult) -> dict[str, Any]:
    assert result.result is not None
    return cast(dict[str, Any], result.result.to_payload())


def campaign_result(terminal: str, outcome: str | None) -> CampaignResult:
    return CampaignResult(
        terminal=cast(Any, terminal),
        quiescent=True,
        finished=None if outcome is None else {"outcome": outcome, "reason": "test"},
        owner=None,
        code=None,
        result=None,
        status=cast(Any, None),
    )


# -- step 5: the campaign `run_id` -------------------------------------------


def test_derive_run_id() -> None:
    """ADR-0007: `c-` plus the sanitized directory name, inside the wire's bound."""

    assert derive_run_id(Path("/x/My Run")) == "c-my-run"
    assert derive_run_id(Path("/x/run")) == "c-run"
    assert derive_run_id(Path("/x/a.b_c-d")) == "c-a.b_c-d"

    long_id = derive_run_id(Path("/x/" + "a" * 200))
    assert len(long_id) == 95 == MAX_RUN_ID
    assert new_instance_id(long_id).startswith(long_id + "-")


# -- step 2: path separation --------------------------------------------------


@pytest.mark.parametrize("layout", ["equal", "inside_seed", "contains_seed", "inside_scorer_cwd"])
def test_path_separation_refusals(tmp_path: Path, layout: str) -> None:
    seed = tmp_path / "seed"
    scorer = tmp_path / "scorer"
    editor = tmp_path / "editor"
    for directory in (seed, scorer, editor):
        directory.mkdir()
    run_dir = {
        "equal": seed,
        "inside_seed": seed / "run",
        "contains_seed": tmp_path,
        "inside_scorer_cwd": scorer / "run",
    }[layout]
    expected = (
        "run_dir_overlaps_program" if layout == "inside_scorer_cwd" else ("run_dir_overlaps_seed")
    )

    with pytest.raises(EvolveRefused) as refusal:
        check_path_separation(run_dir, seed, (scorer, editor))

    assert refusal.value.code == expected
    assert str(refusal.value) == expected


def test_path_separation_accepts_a_sibling(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    scorer = tmp_path / "scorer"
    editor = tmp_path / "editor"
    for directory in (seed, scorer, editor):
        directory.mkdir()

    check_path_separation(tmp_path / "run", seed, (scorer, editor))


def test_path_separation_refuses_a_run_dir_containing_a_program(tmp_path: Path) -> None:
    """Disjoint is symmetric: a `run_dir` that *contains* a program is refused too."""

    seed = tmp_path / "seed"
    programs = tmp_path / "run" / "editor"
    seed.mkdir()
    programs.mkdir(parents=True)

    with pytest.raises(EvolveRefused) as refusal:
        check_path_separation(tmp_path / "run", seed, (programs,))

    assert refusal.value.code == "run_dir_overlaps_program"


# -- step 3: run-dir state and the two output roots ---------------------------


def test_run_dir_state(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    assert run_dir_state(absent) == "new"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_dir_state(empty) == "new"

    resuming = tmp_path / "resuming"
    resuming.mkdir()
    (resuming / CAMPAIGN_FILE).write_text("{}\n", encoding="utf-8")
    assert run_dir_state(resuming) == "resume"

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(foreign)
    assert refusal.value.code == "run_dir_not_campaign"

    # The workflow's own roots are not foreign entries.
    ours = tmp_path / "ours"
    ours.mkdir()
    (ours / "scorer-traces").mkdir(mode=0o700)
    (ours / "editor-logs").mkdir(mode=0o700)
    assert run_dir_state(ours) == "new"

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "scorer-traces").symlink_to(tmp_path / "empty")
    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(linked)
    assert refusal.value.code == "run_dir_unsafe_entry"

    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "editor-logs").mkdir(mode=0o755)
    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(loose)
    assert refusal.value.code == "run_dir_unsafe_entry"


def test_run_dir_state_refuses_a_run_dir_that_is_not_a_directory(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(not_a_directory)

    assert refusal.value.code == "run_dir_not_campaign"


def test_admit_output_root(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Absent is admissible: the binding creates the root on its first use.
    admit_output_root(run_dir / "scorer-traces")

    private = run_dir / "editor-logs"
    private.mkdir(mode=0o700)
    admit_output_root(private)

    as_file = run_dir / "as-file"
    as_file.write_text("x", encoding="utf-8")
    with pytest.raises(EvolveRefused) as refusal:
        admit_output_root(as_file)
    assert refusal.value.code == "run_dir_unsafe_entry"
    assert refusal.value.path == "as-file"
    assert str(refusal.value) == "run_dir_unsafe_entry at as-file"

    link = run_dir / "as-link"
    link.symlink_to(private)
    with pytest.raises(EvolveRefused) as refusal:
        admit_output_root(link)
    assert refusal.value.code == "run_dir_unsafe_entry"

    loose = run_dir / "loose"
    loose.mkdir(mode=0o755)
    assert stat.S_IMODE(loose.lstat().st_mode) & 0o077
    with pytest.raises(EvolveRefused) as refusal:
        admit_output_root(loose)
    assert refusal.value.code == "run_dir_unsafe_entry"

    # A symlinked component of `run_dir` itself, which would put the traces
    # somewhere other than under the canonical run directory.
    linked_run = tmp_path / "linked-run"
    linked_run.symlink_to(run_dir)
    with pytest.raises(EvolveRefused) as refusal:
        admit_output_root(linked_run / "scorer-traces")
    assert refusal.value.code == "run_dir_unsafe_entry"

    # A root owned by another user is refused too; it is not constructible
    # without privileges, so the ownership branch is asserted on the check
    # itself rather than on a directory this test could make.
    assert private.lstat().st_uid == os.geteuid()


# -- the write-free guarantee -------------------------------------------------


@pytest.mark.parametrize("kind", ["schema", "binding", "corpus", "state"])
async def test_refusals_write_nothing(tmp_path: Path, kind: str) -> None:
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    expected = {
        "schema": "manifest_schema",
        "binding": "scorer_invalid",
        "corpus": "unreadable",
        "state": "run_dir_not_campaign",
    }[kind]
    if kind == "schema":
        manifest = write_v1_manifest(workspace)
    elif kind == "binding":
        (workspace / "scorer" / "score.py").unlink()
        (workspace / "scorer").rmdir()
    elif kind == "corpus":
        (workspace / "corpus.jsonl").unlink()

    run_dir = workspace / "run"
    if kind != "state":
        # A refusal never brings the run directory into being.
        with pytest.raises(EvolveRefused) as refusal:
            await evolve(manifest, run_dir, adapter_factory=fake_adapters())
        assert refusal.value.code == expected
        assert not run_dir.exists()

    run_dir.mkdir()
    if kind == "state":
        (run_dir / "stray.txt").write_text("x", encoding="utf-8")
    before = listing(run_dir)

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == expected
    assert listing(run_dir) == before


async def test_a_malformed_manifest_is_one_refusal_with_the_loaders_code(
    tmp_path: Path,
) -> None:
    """Every refusal, whoever detected it, reaches the caller as `EvolveRefused`.

    The loader's own code and dotted path are kept; a caller never has to catch
    `ManifestError` as well to tell "refused to start" from "ran and failed".
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["scorer"].pop("argv")
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, workspace / "run", adapter_factory=fake_adapters())

    assert refusal.value.code == "missing_key"
    assert str(refusal.value) == "missing_key at scorer.argv"
    assert not (workspace / "run").exists()


async def test_a_missing_manifest_refuses_without_naming_the_path(tmp_path: Path) -> None:
    """The commonest operator typo must not put a filesystem path in a refusal.

    A manifest that cannot be read at all is `bad_path`, and the refusal names
    the document, not where it was looked for: `EvolveRefused.__str__` is the
    line the CLI prints to stderr (spec §6, §9).
    """

    missing = tmp_path / "does-not-exist" / "manifest.json"

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(missing, tmp_path / "run", adapter_factory=fake_adapters())

    assert refusal.value.code == "bad_path"
    assert str(refusal.value) == "bad_path at manifest"
    assert str(tmp_path) not in str(refusal.value)
    assert all(str(tmp_path) not in str(arg) for arg in refusal.value.args)


# -- step 7: the campaign, end to end ----------------------------------------


async def test_evolve_end_to_end_promotes(tmp_path: Path) -> None:
    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    payload = result_payload(result)
    assert payload["final_champion"]["version"] != "v0"
    assert payload["final_champion"]["baseline_origin"] == "promoted_attempt"
    assert payload["selection"][0]["outcome"] == "promoted"
    assert exit_code_for(result) == 0
    # The two output roots are the bindings' own, created under the run dir and
    # owner-only. The run scored more than once, so the roots the bindings made
    # are roots the point-of-use re-check went on admitting.
    for name in ("scorer-traces", "editor-logs"):
        root = run_dir / name
        assert root.is_dir() and not root.is_symlink()
        assert stat.S_IMODE(root.lstat().st_mode) == 0o700
    from evolution.tree import read_scorer_trace

    assert not (run_dir / "scorer-traces" / "scorer_traces").exists()
    counts = []
    for journal in (run_dir / "measurements").glob("*/events.jsonl"):
        started = [
            r
            for r in map(json.loads, journal.read_text().splitlines())
            if r["event"] == "scoring_started"
        ]
        if not started:
            continue
        counts.append(len(started))
        traces = [
            read_scorer_trace(run_dir, r["payload"]["score_request_id"], measurement_id=r["run_id"])
            for r in started
        ]
        assert all(t is not None and t.layout == "pack" for t in traces)
        assert len({t.location for t in traces}) == 1
    assert max(counts) > 1
    assert len(list((run_dir / "scorer-traces").glob("*.pack"))) > 1
    assert (run_dir / CAMPAIGN_FILE).is_file()


async def test_a_run_dir_under_a_symlinked_ancestor_runs(tmp_path: Path) -> None:
    """A symlinked ancestor is canonicalized, not refused (amendment A7).

    `/tmp` is a symlink to `/private/tmp` on macOS and a symlinked `$HOME` is
    common on Linux, so before A7 every `--run-dir` under one was refused
    `run_dir_unsafe_entry at scorer-traces` -- naming an entry that did not
    exist -- by `admit_output_root`'s ancestor walk. The campaign now runs, and
    every file it writes lands under the real directory.
    """

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    manifest = write_workspace(tmp_path / "ws")

    result = await evolve(manifest, link / "run", adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert exit_code_for(result) == 0
    assert (real / "run" / CAMPAIGN_FILE).is_file()
    for name in ("scorer-traces", "editor-logs"):
        assert (real / "run" / name).is_dir()
    # The `run_id` still carries the name the operator wrote, which the resolved
    # path shares: canonicalization changes ancestors, never the last component.
    assert derive_run_id(link / "run") == derive_run_id(real / "run")


async def test_a_run_dir_that_is_itself_a_symlink_is_refused(tmp_path: Path) -> None:
    """The one symlink A7 still refuses, before anything is written or opened."""

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link-run"
    link.symlink_to(real, target_is_directory=True)
    manifest = write_workspace(tmp_path / "ws")

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, link, adapter_factory=fake_adapters())

    assert refusal.value.code == "run_dir_unsafe_entry"
    assert list(real.iterdir()) == []
    assert link.is_symlink()


async def test_evolve_rejecting_scorer_no_promotion(tmp_path: Path) -> None:
    manifest = write_workspace(tmp_path / "ws", scorer_script=REJECTING_SCORER_SCRIPT)
    run_dir = tmp_path / "ws" / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    payload = result_payload(result)
    assert payload["final_champion"]["version"] == "v0"
    assert payload["selection"][0]["outcome"] != "promoted"
    assert exit_code_for(result) == 0


async def test_evolve_resumes_by_fact_and_refuses_changed_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"

    first = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert first.terminal == "finished"

    # No `--resume`: the campaign journal in the run directory is the fact.
    second = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert second.terminal == "finished"
    assert result_payload(second)["run_id"] == derive_run_id(run_dir)

    changed = write_workspace(
        workspace,
        campaign={**CAMPAIGN_PAYLOAD, "rounds": 2},
        name="manifest-rounds-2.json",
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == "run_identity_changed"


@pytest.mark.parametrize("raw", [b'{"event": "campaign_', b""])
async def test_an_unreadable_journal_is_a_persistence_outcome_not_a_refusal(
    tmp_path: Path, raw: bytes
) -> None:
    """A crash between the journal's `open` and its first completed append.

    `run_dir_state` selects resume because `campaign.jsonl` is there, and the id
    the adapter needs is not legible. That is C1's persistence fault to classify
    over the journal it holds -- `persistence_failed`, exit 1 -- never
    `refused: campaign_absent`, which would name a campaign this run root has.
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    run_dir.mkdir(parents=True)
    (run_dir / CAMPAIGN_FILE).write_bytes(raw)

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "persistence_failed"
    assert exit_code_for(result) == 1


@pytest.mark.parametrize(
    "break_journal", ["unreadable", "oversized"], ids=["chmod-000", "oversized"]
)
async def test_a_journal_file_that_cannot_be_read_is_a_persistence_outcome(
    tmp_path: Path, break_journal: str
) -> None:
    """Unreadable is a property of the file too, not only of its contents.

    A `campaign.jsonl` whose permissions are gone, or one past the reader's own
    `MAX_JOURNAL_BYTES`, makes the reader raise before it can say anything about
    the prefix -- in `read_run_id`, again while C1 opens the log, and a third
    time in the projection that describes the run root. All three are the same
    fact, and it is C1's `persistence_failed` (exit 1) over a journal the run
    root really holds, never a traceback out of `evolve`.
    """

    if break_journal == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode 0o000 file")
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    run_dir.mkdir(parents=True)
    journal = run_dir / CAMPAIGN_FILE
    journal.write_bytes(b'{"event": "campaign_created"}\n')
    if break_journal == "unreadable":
        journal.chmod(0o000)
    else:
        # Sparse: the reader refuses on `st_size` before it reads a byte.
        with journal.open("r+b") as handle:
            handle.truncate(MAX_JOURNAL_BYTES + 1)

    try:
        result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    finally:
        journal.chmod(0o600)

    assert result.terminal == "persistence_failed"
    assert exit_code_for(result) == 1


async def test_a_run_directory_that_cannot_be_written_is_a_persistence_outcome(
    tmp_path: Path,
) -> None:
    """Admission passes and the write then fails: a terminal, not a refusal.

    An empty run directory is a valid `--run-dir`, so W1's admission and C1's
    preflight both accept it; mode 0o500 makes the journal's own `open` the first
    call that fails. That is `persistence_failed` with exit 1, and nothing is
    created -- a refusal (exit 2) would claim the invocation was wrong, and an
    escaping fault would end `evolve` in a traceback.
    """

    if os.geteuid() == 0:
        pytest.skip("root writes into a mode 0o500 directory")
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    run_dir.mkdir(parents=True)
    run_dir.chmod(0o500)

    try:
        result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
        assert sorted(path.name for path in run_dir.iterdir()) == []
    finally:
        # Restored so `tmp_path` cleanup can remove the directory.
        run_dir.chmod(0o700)

    assert result.terminal == "persistence_failed"
    assert result.owner == "campaign_journal"
    assert exit_code_for(result) == 1


async def test_an_absent_journal_still_starts_a_fresh_run(tmp_path: Path) -> None:
    """`campaign_absent` now means exactly one thing, and a new run is the other."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"

    assert run_dir_state(run_dir) == "new"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result_payload(result)["run_id"] == derive_run_id(run_dir)


@pytest.mark.parametrize(
    ("message", "refused"),
    [("run_id_mismatch", True), ("invalid journal record", False)],
)
async def test_only_c1s_pre_admission_codes_become_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, refused: bool
) -> None:
    """C1's refusal types also carry executor faults raised after a durable write.

    `CampaignInvariantViolated` is `run_id_mismatch` inside `adopt` -- a refusal
    before anything was written -- but also the `SchemaError` message of a
    journal append that the campaign log refused, which happens with a journal
    already on disk. Mapping by message alone would report that as
    `refused: invalid journal record`, exit 2, over a run directory the
    invocation did change; it propagates as itself instead.
    """

    manifest = write_workspace(tmp_path / "ws")

    async def refusing(policy: Any, **kwargs: Any) -> CampaignResult:
        raise CampaignInvariantViolated(message)

    monkeypatch.setattr(evolve_module, "run_campaign", refusing)

    expected: type[Exception] = EvolveRefused if refused else CampaignInvariantViolated
    with pytest.raises(expected) as failure:
        await evolve(manifest, tmp_path / "ws" / "run", adapter_factory=fake_adapters())

    if refused:
        assert isinstance(failure.value, EvolveRefused)
        assert failure.value.code == message
    else:
        assert not isinstance(failure.value, EvolveRefused)
        assert str(failure.value) == message


def _declare_command_grouping(manifest: Path, *, body: str, allowed_env: list[str]) -> None:
    workspace = manifest.parent
    (workspace / "grouping").mkdir()
    (workspace / "grouping" / "group.py").write_text(body, encoding="utf-8")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["grouper"] = {
        "kind": "command",
        "argv": [sys.executable, "group.py"],
        "cwd": "grouping",
        "timeout_seconds": 60,
        "dependency_paths": [],
        "allowed_env": allowed_env,
        "config": {},
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")


async def test_secrets_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both allowed_env sets arm redaction; no value lands in the run directory."""
    monkeypatch.setenv("EVO_TREE_TOKEN", "hunter2secret")
    monkeypatch.setenv("EVO_TREE_SCORER_TOKEN", "swordfish")
    monkeypatch.delenv("EVO_TREE_ABSENT", raising=False)
    manifest = write_workspace(
        tmp_path / "ws",
        editor_env=("EVO_TREE_TOKEN",),
        scorer_env=("EVO_TREE_SCORER_TOKEN", "EVO_TREE_ABSENT"),
    )
    run_dir = tmp_path / "ws" / "run"
    seen: dict[str, Any] = {}
    started = evolve_module.run_campaign

    async def recording(policy: Any, **kwargs: Any) -> CampaignResult:
        seen["secrets"] = kwargs["secrets"]
        return await started(policy, **kwargs)

    monkeypatch.setattr(evolve_module, "run_campaign", recording)
    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert result.terminal == "finished"
    assert sorted(seen["secrets"]) == ["hunter2secret", "swordfish"]
    events = [json.loads(line)["event"] for line in _journal_lines(run_dir)]
    assert "editor_finished" in events
    for path in run_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            written = path.read_bytes()
            assert b"hunter2secret" not in written, path
            assert b"swordfish" not in written, path


@pytest.mark.parametrize("command_grouping", [False, True])
def test_command_grouping_secrets_use_the_snapshot_and_shared_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_grouping: bool
) -> None:
    """Forwarded grouping values join deduplicated secrets above the shared floor."""
    manifest = write_workspace(
        tmp_path / "ws", scorer_env=("SHARED_SECRET",), editor_env=("SHARED_SECRET",)
    )
    if command_grouping:
        _declare_command_grouping(
            manifest,
            body="print('{}')\n",
            allowed_env=[
                "GROUP_TOKEN",
                "GROUP_FLAG",
                "GROUP_EDGE",
                "SHARED_SECRET",
                "GROUP_ABSENT",
            ],
        )
    monkeypatch.setenv("GROUP_TOKEN", "rotated-live-token")
    snapshot = {
        "GROUP_TOKEN": "grouping-credential",
        "GROUP_FLAG": "true",
        "GROUP_EDGE": "12345678",
        "SHARED_SECRET": "shared-credential",
    }
    secrets = evolve_module.redactions_for(manifest, environ=snapshot)
    assert secrets == (
        ("shared-credential", "12345678", "grouping-credential")
        if command_grouping
        else ("shared-credential",)
    )


@pytest.mark.parametrize("escaped", [False, True])
async def test_a_grouping_command_cannot_journal_its_forwarded_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escaped: bool
) -> None:
    """The command's echoed credential is rejected before it becomes durable."""
    manifest = write_workspace(tmp_path / "ws")
    _declare_command_grouping(
        manifest,
        body=(
            "import json, os, subprocess, sys\n"
            "request = json.load(open(sys.argv[-1]))\n"
            "entries = json.loads(subprocess.check_output("
            "request['helpers']['list_grouping_inputs']))['entries']\n"
            "secret = os.environ['GROUP_TOKEN']\n"
            "output = json.dumps({'version': 'reflect_grouping_result/1', "
            "'registry_digest': request['registry_digest'], "
            "'assignments': [0 if entry['failing'] else None for entry in entries], "
            "'descriptions': {'0': {'label': secret, 'description': 'every failure'}}})\n"
            + (
                "output = output.replace(secret, ''.join('\\\\u%04x' % ord(c) for c in secret))\n"
                if escaped
                else ""
            )
            + "print(output)\n"
        ),
        allowed_env=["GROUP_TOKEN"],
    )
    monkeypatch.setenv("GROUP_TOKEN", "rotated-live-token")
    run_dir = manifest.parent / "run"
    result = await evolve(
        manifest,
        run_dir,
        adapter_factory=fake_adapters(),
        environ={**os.environ, "GROUP_TOKEN": "grouping-credential"},
    )
    assert result.terminal == "suspended"
    events = [json.loads(line) for line in _journal_lines(run_dir)]
    assert [
        event["payload"]["code"] for event in events if event["event"] == "grouping_failed"
    ] == ["secret_detected"]
    assert "grouping_finished" not in [event["event"] for event in events]
    for path in run_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert b"grouping-credential" not in path.read_bytes(), path


@pytest.mark.parametrize(
    "target",
    [
        "missing",
        "not_a_directory",
        "run_dir_itself",
        "inside_run_dir",
        "contains_run_dir",
        "symlink",
    ],
)
async def test_linked_from_bad_path_refuses_before_any_write(tmp_path: Path, target: str) -> None:
    """Every linked_from path check refuses before writing and never leaks the path."""
    predecessor, champion = await finished_predecessor(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = runs / "run-2"
    if target in ("run_dir_itself", "inside_run_dir"):
        run_dir.mkdir(mode=0o700)
    if target == "missing":
        linked = "../gone"
    elif target == "not_a_directory":
        (tmp_path / "a-file").write_text("x", encoding="utf-8")
        linked = "../a-file"
    elif target == "run_dir_itself":
        linked = "../runs/run-2"
    elif target == "inside_run_dir":
        (run_dir / "scorer-traces").mkdir(mode=0o700)
        linked = "../runs/run-2/scorer-traces"
    elif target == "contains_run_dir":
        linked = "../runs"
    else:
        (tmp_path / "link-1").symlink_to(predecessor, target_is_directory=True)
        linked = "../link-1"
    manifest = write_workspace(
        tmp_path / "b",
        linked_from={"run_dir": linked, "baseline": "fresh"},
        seed_root=f"../run-1/{champion}",
    )
    before = listing(runs)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    # /15's object-form link names its run_dir field (ADR-0032 linking).
    assert (refusal.value.code, refusal.value.path) == ("bad_path", "linked_from.run_dir")
    assert refusal.value.instance_digest is None
    assert str(refusal.value) == "bad_path at linked_from.run_dir"
    assert str(tmp_path) not in str(refusal.value)
    assert listing(runs) == before
    assert not (run_dir / CAMPAIGN_FILE).exists()


async def test_bounds_repair_is_selected_for_current_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def build_reflector(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _StopAfterReflector

    monkeypatch.setattr(evolve_module, "build_reflector", build_reflector)
    manifest = write_workspace(tmp_path / "ws", port=1)
    with pytest.raises(_StopAfterReflector):
        await evolve(manifest, tmp_path / "ws" / "run")
    assert captured["bounds_repair"] is True


@pytest.mark.parametrize("family", [2, 16])
async def test_an_unsupported_family_refuses_before_adapter_creation_or_tail_repair(
    tmp_path: Path, family: int
) -> None:
    """A legible unsupported family is a write-free W1 refusal, even with a torn tail."""

    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"
    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert result.terminal == "finished"
    path = run_dir / CAMPAIGN_FILE
    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[0])
    record["payload"]["family_version"] = family
    lines[0] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(b"".join(lines) + b'{"torn":')
    before = listing(run_dir)

    def unexpected_adapter(_manifest: Any, _run_id: str) -> Any:
        pytest.fail("unsupported-family refusal must precede adapter creation")

    with pytest.raises(EvolveRefused) as failure:
        await evolve(manifest, run_dir, adapter_factory=unexpected_adapter)

    assert str(failure.value) == f"campaign_family_unsupported at family/{family}"
    assert listing(run_dir) == before


async def test_a_secret_in_the_corpus_is_a_refusal_at_its_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RecordMatchesSecret` surfaces with the public location, before the run root exists."""

    monkeypatch.setenv("EVO_TREE_TOKEN", "routing-group")
    manifest = write_workspace(tmp_path / "ws", editor_env=("EVO_TREE_TOKEN",))
    # The scaffold's first corpus row (see `write_workspace`) has `group_id`
    # `"g1"`; overwritten here so it contains the forwarded secret value.
    corpus = tmp_path / "ws" / "corpus.jsonl"
    rows = [json.loads(line) for line in corpus.read_text().splitlines() if line.strip()]
    rows[0]["group_id"] = "routing-group-a"
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
    run_dir = tmp_path / "ws" / "run"
    with pytest.raises(EvolveRefused) as failure:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert (failure.value.code, failure.value.path) == (
        "record_matches_secret",
        "identity.corpus_cases[0].group_id",
    )
    assert "routing-group" not in str(failure.value)
    assert not run_dir.exists()


async def test_an_oversized_corpus_is_a_refusal_before_the_run_root(tmp_path: Path) -> None:
    """`CorpusTooLarge` surfaces through `evolve` as its own code, before any write.

    Mirrors `test_a_secret_in_the_corpus_is_a_refusal_at_its_location`'s shape for
    the other pre-admission refusal `admit_creation` can raise: the creation
    record's encoded bytes exceed `MAX_RECORD_BYTES` (`_big_corpus`'s 10 000
    sixty-character-padded cases is the known-oversized shape from the campaign
    fixtures), so `run_campaign` refuses before `create` opens the log at all.
    """

    rows = [
        {
            "case_id": f"case-{index:06d}-" + "x" * 60,
            "group_id": "g1",
            "stratum": "s1",
            "public": {"text": "hello"},
        }
        for index in range(10_000)
    ]
    manifest = write_workspace(tmp_path / "ws", rows=rows)
    run_dir = tmp_path / "ws" / "run"
    with pytest.raises(EvolveRefused) as failure:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert (failure.value.code, failure.value.path) == ("corpus_too_large", None)
    assert not run_dir.exists()


def test_the_cli_prints_the_refusal_through_the_redactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served_target: tuple[Any, int]
) -> None:
    """Spec §6: a forwarded value equal to a token of the line becomes the marker.

    `record_matches_secret` is raised inside `run_campaign`, after C1's own
    preflight opens its loopback connection (step 6), so this is the one CLI
    test in the trio that needs a real served target rather than a fake
    adapter -- the CLI always builds the default `HttpSessionAdapter`.

    `EVO_TREE_TOKEN`'s value is the refusal *code* itself, not a fragment of
    `path`: `secret_location` checks a dict's keys before its values at every
    level (`campaign.secret_location`), and every dotted segment of this
    location -- `identity`, `corpus_cases`, `group_id` -- is itself a real key
    of the creation record, so forwarding any of them as a secret would be
    caught as `record-key` before the walk ever reached the case row, proving
    a different (still real) refusal. The code is not a key or a value
    anywhere in the record, so it cannot change which refusal fires, and it
    still proves the point: the printed line goes through the same redactor
    as the result document, whatever token of it a forwarded value hides in.
    """

    _target, port = served_target
    monkeypatch.setenv("EVO_TREE_TOKEN", "record_matches_secret")
    monkeypatch.setenv("EVO_TREE_OTHER", "routing-group")
    manifest = write_workspace(
        tmp_path / "ws", port=port, editor_env=("EVO_TREE_TOKEN", "EVO_TREE_OTHER")
    )
    corpus = tmp_path / "ws" / "corpus.jsonl"
    rows = [json.loads(line) for line in corpus.read_text().splitlines() if line.strip()]
    rows[0]["group_id"] = "routing-group-a"
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
    run_dir = tmp_path / "ws" / "run"

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 2, invocation.output
    assert invocation.stdout.strip() == ""
    assert (
        "evo tree evolve: refused: [REDACTED] at "
        "identity.corpus_cases[0].group_id" in invocation.stderr
    )
    assert "routing-group" not in invocation.stderr
    assert "record_matches_secret" not in invocation.stderr


def test_the_secret_floor_is_eight_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_secrets` skips a value too short to be a credential (amendment A6).

    A redaction needle is matched against arbitrary text, so a one-character
    value is found in every record: C1's journal would refuse its own envelope
    and the CLI's redactor would rewrite unrelated numbers. Seven characters is
    still a flag, eight is a secret; the boundary is asserted from both sides.
    """

    for value in ("", "1", "true", "abcdefg"):
        monkeypatch.setenv("W1_SECRET_PROBE", value)
        assert evolve_module._secrets(frozenset({"W1_SECRET_PROBE"})) == ()

    monkeypatch.setenv("W1_SECRET_PROBE", "abcdefgh")
    assert evolve_module._secrets(frozenset({"W1_SECRET_PROBE"})) == ("abcdefgh",)

    # The floor filters values, not names: a long one still survives beside a
    # short one, and an undeclared name contributes nothing either way.
    monkeypatch.setenv("W1_SECRET_FLAG", "1")
    assert evolve_module._secrets(
        frozenset({"W1_SECRET_PROBE"}), frozenset({"W1_SECRET_FLAG"})
    ) == ("abcdefgh",)


async def test_a_short_forwarded_value_never_poisons_the_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allowed_env: ["W1_DEBUG"]` with `W1_DEBUG=1` runs a campaign to the end.

    Before amendment A6 this was a `SchemaError` on the campaign's *first*
    append -- `journal_schema` scans `run_id` too, and every `run_id` contains a
    `1` -- surfacing as `CampaignInvariantViolated("invalid journal record")`
    from a run root that had already been created.
    """

    monkeypatch.setenv("W1_DEBUG", "1")
    manifest = write_workspace(tmp_path / "ws", scorer_env=("W1_DEBUG",))
    run_dir = tmp_path / "ws" / "run1"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert exit_code_for(result) == 0
    assert (run_dir / CAMPAIGN_FILE).is_file()
    assert evolve_module.redactions_for(manifest) == ()


# -- the point-of-use re-check ------------------------------------------------


async def test_scorer_re_admits_its_output_root_at_the_point_of_use(tmp_path: Path) -> None:
    """A trace root loosened after admission never receives a trace (spec §6, §9).

    The scorer is bound while the root is still owner-only, exactly as `evolve`
    binds it after step 3 admitted it; the mode changes afterwards, which is the
    window the point-of-use check closes. Since ADR-0040 D7 the refusal reaches
    the caller as a trace storage fault (`journal_unavailable`, a `JournalFault`
    that bypasses the scorer's broad handler), never as a scorer failure, with
    the refusal itself as its cause.
    """

    workspace = tmp_path / "ws"
    manifest_path = write_workspace(workspace)
    manifest = load_manifest_v2(manifest_path)
    assert manifest.workflow is not None
    run_dir = workspace / "run"
    traces = run_dir / "scorer-traces"
    traces.mkdir(parents=True, mode=0o700)

    scorer = build_scorer(manifest.workflow.scorer, seed_root=manifest.seed_root, run_dir=run_dir)
    admit_output_root(traces)  # admissible when the campaign started
    traces.chmod(0o755)

    case = EvalCase("c1", "g1", "s1", (), {"text": "hello"}, {}, {})
    view = ScoringView(
        History(()),
        "completed",
        SemanticIdentity("c", "d", "s", "exact", "2", "r"),
    )
    candidate = CandidateView(workspace / "seed", "d" * 64, (), verifier=lambda root: "d" * 64)

    with pytest.raises(ScorerTraceStorageFault) as failure:
        await scorer.score(case, view, candidate)

    assert failure.value.code == "journal_unavailable"
    assert failure.value.detail.startswith("scorer trace scorer-traces/untraced-")
    assert ".pack: unsafe_entry (admit);" in failure.value.detail
    scorer._close_traces()
    assert isinstance(failure.value.__cause__, EvolveRefused)
    assert failure.value.__cause__.code == "run_dir_unsafe_entry"
    assert list(traces.iterdir()) == []


# -- the real adapter over a served reference target --------------------------


@pytest.fixture
def served_target(tmp_path: Path):
    """A `FaultInjectingReferenceTarget` on an ephemeral loopback port."""

    target = FaultInjectingReferenceTarget(
        allowed_artifact_root=tmp_path.resolve(), policy=POLICY, seed_mode="exact"
    )
    server = serve(target, port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield target, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


async def test_evolve_over_http_adapter_against_reference_target(
    tmp_path: Path, served_target: tuple[Any, int]
) -> None:
    """The manifest, the default `HttpSessionAdapter` and C1 agree on the `run_id`.

    The first `materialize` is the proof: the adapter refuses an instance id that
    does not carry the id it was built on, so a run that reaches a promotion has
    proved the derivation, the journal and the wire are one value.
    """

    _target, port = served_target
    manifest = write_workspace(tmp_path / "ws", port=port)
    run_dir = tmp_path / "ws" / "run"

    result = await evolve(manifest, run_dir)

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    payload = result_payload(result)
    assert payload["run_id"] == "c-run"
    assert payload["final_champion"]["version"] != "v0"
    assert exit_code_for(result) == 0


async def test_preflight_refusal_leaves_no_campaign_file(
    tmp_path: Path, served_target: tuple[Any, int]
) -> None:
    target, port = served_target
    target.fault(internal_error=True)
    manifest = write_workspace(tmp_path / "ws", port=port)
    run_dir = tmp_path / "ws" / "run"

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir)

    # C1's admission failed at `preflight`, so the refusal carries the code the
    # target itself named on `/health` -- not one of W1's own.
    assert refusal.value.code == "internal"
    assert str(refusal.value) == "internal"
    assert not (run_dir / CAMPAIGN_FILE).exists()
    assert not run_dir.exists()


# -- I1, live-shaped: an unstable target over the real adapter (spec §10) -----

#: The `/8` campaign of the live-shaped tests: one turn at a time, so which
#: request ordinal a turn meets is a fact of the pattern and not of the
#: scheduler, and the operator's declaration that a failed turn may be resent.
V8_CAMPAIGN: dict[str, Any] = {
    **CAMPAIGN_PAYLOAD,
    "max_concurrency": 1,
    "budget": {"max_turns": 4, "timeout_seconds": 120, "max_failed_turns": 1},
    "call_retries": {"max_attempts": 4, "turn_max_attempts": 3},
}

V8_ROWS: list[dict[str, Any]] = [
    {
        "case_id": f"c{index}",
        "group_id": "g1",
        "stratum": "s1",
        "public": {"text": f"hello {index}"},
        "oracle": {"critical": True},
    }
    for index in range(1, 5)
]


def write_v8_workspace(
    root: Path,
    *,
    port: int,
    campaign: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
    schema: str = SCHEMA_V15,
) -> Path:
    """`write_workspace`'s document, declared `/8` (or `schema`): the same workspace one schema on."""

    return write_workspace(
        root,
        port=port,
        campaign=V8_CAMPAIGN if campaign is None else campaign,
        rows=V8_ROWS if rows is None else rows,
        schema=schema,
    )


# ~20 seconds, nearly all of it the call-retry back-off of four samples whose
# every turn fails three times. The module is `slow`, so the fast tier skips
# it; the full gate deselects only `live`, `paper` and `external_repo`.


# -- the exit-code map --------------------------------------------------------


def test_exit_code_for() -> None:
    from dataclasses import replace

    assert (
        exit_code_for(replace(campaign_result("finished", "completed"), code="projection_invalid"))
        == 1
    )
    assert exit_code_for(campaign_result("finished", "completed")) == 0
    assert exit_code_for(campaign_result("finished", "stopped")) == 3
    assert exit_code_for(campaign_result("finished", "budget_exhausted")) == 4
    assert exit_code_for(campaign_result("finished", "infrastructure_failed")) == 1
    assert exit_code_for(campaign_result("storage_capacity_exhausted", None)) == 1
    assert exit_code_for(campaign_result("persistence_failed", None)) == 1
    # I1 spec §5.5: a suspension is exit 1 like every other infrastructure
    # report, and its run directory is resumable.
    assert exit_code_for(campaign_result("suspended", None)) == 1


# -- the in-process editor kinds, bound and run (E1-editor design §9) ---------

CHAT_SCRIPT = [
    fake_chat_server.tool_call("list_files"),
    fake_chat_server.tool_call(
        "record_plan",
        {
            "summary": "make the skill say what it does",
            "changes": [
                {"path": "skills/SKILL.md", "action": "update", "purpose": "state the behaviour"}
            ],
        },
    ),
    fake_chat_server.tool_call(
        "write_file", {"path": "skills/SKILL.md", "content": "IMPROVED: seed skill edited\n"}
    ),
    fake_chat_server.tool_call("finish_edit", {"summary": "rewrote the skill"}),
]

CHAT_KEY = "sekret-key-value"


@pytest.fixture
def chat_endpoint():
    """The scripted OpenAI-compatible endpoint one `chat` campaign talks to."""

    base_url, server = fake_chat_server.start(CHAT_SCRIPT)
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()


def chat_editor_section(base_url: str, *, model: str = fake_chat_server.MODEL) -> dict[str, Any]:
    return {
        "kind": "chat",
        "model": model,
        "base_url": base_url,
        "api_key_env": "E1_KEY",
        "max_turns": 8,
    }


class FakeAgentBackend:
    """The agent CLI, replaced at the backend seam: one call, one edit."""

    constructed: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeAgentBackend.constructed.append(kwargs)
        self.last_usage = {"tool_turns": 1}
        self.last_codex_diagnostics: dict[str, Any] = {}

    async def complete_file_edit(
        self, work_dir: Path, prompt: str, system: str | None = None, *, timeout_s: float = 0.0
    ) -> Any:
        skill = Path(work_dir) / "skills" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8").rstrip("\n") + " edited\n", encoding="utf-8"
        )
        return SimpleNamespace(content="rewrote the skill", tokens_in=1, tokens_out=1, time_ms=1)


def agent_editor_section(workspace: Path) -> dict[str, Any]:
    """An `agent_cli` editor over an executable the operator declared."""

    binary = workspace / "agent" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return {"kind": "agent_cli", "model": "codex", "agent_bin": "agent/codex"}


def assert_promoted(result: CampaignResult) -> None:
    """The subprocess happy path's assertions, for an in-process editor."""

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    payload = result_payload(result)
    assert payload["final_champion"]["version"] != "v0"
    assert payload["final_champion"]["baseline_origin"] == "promoted_attempt"
    assert payload["selection"][0]["outcome"] == "promoted"
    assert exit_code_for(result) == 0


async def test_chat_campaign_runs_end_to_end_against_the_fake_chat_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_endpoint: tuple[str, Any]
) -> None:
    """A `chat` manifest binds an `openai` client at the manifest's own endpoint.

    Everything below the binding is real: the campaign, the projection, the tool
    loop, admission, publication and the scorer. Only the provider is scripted,
    which is what a loopback OpenAI-compatible server is for.
    """

    base_url, server = chat_endpoint
    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, editor=chat_editor_section(base_url))
    run_dir = workspace / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert_promoted(result)
    assert server.script.served >= len(CHAT_SCRIPT)
    logs = sorted((run_dir / "editor-logs").glob("*.jsonl"))
    assert logs, "the chat editor writes one transcript per attempt"
    # The key is the run's secret in both directions: the editor redacts its own
    # transcript with it, and C1's journal refuses an envelope that carries it.
    for path in run_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert CHAT_KEY.encode() not in path.read_bytes(), path


class _RotatingEnviron(MutableMapping[str, str]):
    """An `os.environ` whose value for one name changes after the first read.

    It stands in for the thread that rewrites a credential mid-admission. Every
    read of `name` is counted, so a test can say not only *which* value the run
    used but that it was read exactly once.
    """

    def __init__(self, values: dict[str, str], name: str, first: str, later: str) -> None:
        self._values = dict(values)
        self._values[name] = first
        self._name, self._first, self._later = name, first, later
        self.reads = 0

    def __getitem__(self, key: str) -> str:
        if key == self._name:
            self.reads += 1
            return self._first if self.reads == 1 else self._later
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __contains__(self, key: object) -> bool:
        # `Mapping.__contains__` would go through `__getitem__`; a presence
        # test is not a read of the value and must not be counted as one.
        return key in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


async def test_the_environment_is_snapshotted_once_for_the_whole_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_endpoint: tuple[str, Any]
) -> None:
    """Codex branch review round 1: one mapping arms the run, not three reads.

    `_run_secrets`, `editor_program_paths` and `build_editor` each used to read
    the live `os.environ`, so a value another thread rewrote between two of
    those synchronous calls could make the editor hold credential B while the
    run's secret set -- the one the transcript and C1's journal redact with --
    was built from A. `evolve` now takes one `dict(os.environ)` before the first
    derivation and hands that same mapping to all three.

    The `chat` editor is the kind that proves it: its credential is bound
    in-process, so nothing re-checks it later. (A `subprocess` editor's
    `CommandBinding` is bound from the same snapshot, as its `base_env`, and
    `test_the_admitted_document_and_one_snapshot_decide_the_secret_set` proves
    that one.) The environment rotates `E1_KEY` after its first read, so a
    second read anywhere would be visible in both assertions.
    """

    base_url, _server = chat_endpoint
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, editor=chat_editor_section(base_url))
    run_dir = workspace / "run"

    seen: dict[str, Any] = {}
    started = evolve_module.run_campaign

    async def recording(policy: Any, **kwargs: Any) -> CampaignResult:
        seen.update(secrets=kwargs["secrets"], editor=kwargs["editor"])
        return await started(policy, **kwargs)

    monkeypatch.setattr(evolve_module, "run_campaign", recording)
    environ = _RotatingEnviron(dict(os.environ), "E1_KEY", CHAT_KEY, "rotated-key-value")
    monkeypatch.setattr(os, "environ", environ)

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert_promoted(result)
    assert environ.reads == 1, "the snapshot is the run's only read of the name"
    assert seen["secrets"] == (CHAT_KEY,)
    assert seen["editor"].credentials.api_key == CHAT_KEY


async def test_agent_cli_campaign_runs_with_the_fake_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `agent_cli` manifest binds the sealed executable and the bound child env."""

    FakeAgentBackend.constructed = []
    monkeypatch.setattr(agent_editor_module, "AgentBackend", FakeAgentBackend)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, editor=agent_editor_section(workspace))
    run_dir = workspace / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert_promoted(result)
    assert FakeAgentBackend.constructed, "the editor builds one backend per call"
    kwargs = FakeAgentBackend.constructed[0]
    assert kwargs["model"] == "codex"
    assert kwargs["agent_bin"] == str(workspace / "agent" / "codex")
    assert kwargs["explicit"] is True
    # `safe` mode: the CLI receives the curated names, never the whole environment.
    assert set(kwargs["env"]) <= (_RUNTIME_ENV_KEYS | CURATED_ENV_NAMES("codex"))


async def test_resume_under_same_chat_manifest_continues_and_model_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_endpoint: tuple[str, Any]
) -> None:
    """The `chat` identity is the declared configuration: the model is part of it."""

    base_url, _server = chat_endpoint
    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, editor=chat_editor_section(base_url))
    run_dir = workspace / "run"

    first = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert first.terminal == "finished"

    second = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert second.terminal == "finished"
    assert result_payload(second)["run_id"] == derive_run_id(run_dir)

    # A rotated key is not an identity change; the model is.
    monkeypatch.setenv("E1_KEY", "another-key-value")
    third = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert third.terminal == "finished"

    changed = write_workspace(
        workspace,
        editor=chat_editor_section(base_url, model="other-model"),
        name="manifest-other-model.json",
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.code == "run_identity_changed"


async def test_prompt_under_run_dir_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared prompt is a program path: it may not live inside the run directory."""

    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    workspace = tmp_path / "ws"
    run_dir = workspace / "run"
    run_dir.mkdir(parents=True)
    prompt = run_dir / "editor-prompt.md"
    prompt.write_text("be brief\n", encoding="utf-8")
    manifest = write_workspace(
        workspace,
        editor={
            "kind": "chat",
            "model": "m",
            "base_url": "https://gw.example/v1",
            "api_key_env": "E1_KEY",
            "system_prompt": "run/editor-prompt.md",
        },
    )
    before = listing(run_dir)

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == "run_dir_overlaps_program"
    assert listing(run_dir) == before


@pytest.mark.parametrize("kind", ["chat", "agent_cli"])
async def test_editor_env_missing_refuses_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """An in-process editor cannot call its endpoint without the named value."""

    monkeypatch.delenv("E1_ABSENT", raising=False)
    workspace = tmp_path / "ws"
    editor = (
        {
            "kind": "chat",
            "model": "m",
            "base_url": "https://gw.example/v1",
            "api_key_env": "E1_ABSENT",
        }
        if kind == "chat"
        else {**agent_editor_section(workspace), "allowed_env": ["E1_ABSENT"]}
    )
    expected = "editor.api_key_env" if kind == "chat" else "editor.allowed_env"
    manifest = write_workspace(workspace, editor=editor)
    run_dir = workspace / "run"

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == "editor_env_missing"
    assert str(refusal.value) == f"editor_env_missing at {expected}"
    assert not run_dir.exists()


def test_editor_env_missing_is_exit_two_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic refusal line and exit `2`, with no run directory left behind."""

    monkeypatch.delenv("E1_ABSENT", raising=False)
    workspace = tmp_path / "ws"
    manifest = write_workspace(
        workspace,
        editor={
            "kind": "chat",
            "model": "m",
            "base_url": "https://gw.example/v1",
            "api_key_env": "E1_ABSENT",
        },
    )
    run_dir = workspace / "run"

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 2, invocation.output
    assert "evo tree evolve: refused: editor_env_missing at editor.api_key_env" in (
        invocation.output
    )
    assert invocation.stdout.strip() == ""  # a refusal prints no document
    assert not run_dir.exists()


async def test_reflection_capacity_refusal_is_typed_and_precedes_target_preflight(
    tmp_path: Path,
) -> None:
    """An over-capacity adaptive corpus leaves no campaign and never opens the target."""

    repeats = 3
    case_count = 32768 // repeats + 1
    required = case_count * repeats
    rows = [
        {
            "case_id": f"case-{index}",
            "group_id": "group",
            "stratum": "stratum",
            "public": {"text": "question"},
            "oracle": {"critical": True},
        }
        for index in range(case_count)
    ]
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, rows=rows)
    run_dir = workspace / "run"

    class NeverPreflight(FakeAdapter):
        calls = 0

        async def preflight(self, expected: Any) -> Any:
            self.calls += 1
            raise AssertionError("reflection capacity must refuse before target preflight")

    adapter = NeverPreflight()
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=lambda _manifest, _run_id: adapter)

    assert refusal.value.code == "reflection_capacity"
    assert refusal.value.path == str(required)
    assert str(refusal.value) == f"reflection_capacity at {required}"
    assert adapter.calls == 0
    assert not run_dir.exists()


def test_reflection_capacity_refusal_is_exit_two_with_the_required_count(tmp_path: Path) -> None:
    """The public CLI preserves the actionable count without creating a run root."""

    repeats = 3
    case_count = 32768 // repeats + 1
    required = case_count * repeats
    rows = [
        {
            "case_id": f"case-{index}",
            "group_id": "group",
            "stratum": "stratum",
            "public": {"text": "question"},
            "oracle": {"critical": True},
        }
        for index in range(case_count)
    ]
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, rows=rows)
    run_dir = workspace / "run"

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 2, invocation.output
    assert invocation.stdout.strip() == ""
    assert f"evo tree evolve: refused: reflection_capacity at {required}" in invocation.stderr
    assert not run_dir.exists()


def test_declared_files_beside_the_manifest_are_accepted(tmp_path: Path) -> None:
    """The natural layout: a prompt and a CLI beside the manifest, the run dir a sibling.

    A file is refused only when it *lies under* the run directory. Judging it by
    the directory that holds it would refuse every ordinary workspace, because
    the manifest's own directory holds the run directory too.
    """

    workspace = tmp_path / "ws"
    seed = workspace / "seed"
    scorer = workspace / "scorer"
    for directory in (seed, scorer):
        directory.mkdir(parents=True)
    prompt = workspace / "editor-prompt.md"
    prompt.write_text("be brief\n", encoding="utf-8")
    binary = workspace / "agent" / "codex"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    check_path_separation(workspace / "run", seed, (scorer, prompt, binary))


async def test_chat_campaign_with_a_prompt_beside_the_manifest_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_endpoint: tuple[str, Any]
) -> None:
    """The same promotion as the packaged-prompt campaign, with a declared prompt."""

    base_url, _server = chat_endpoint
    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / "editor-prompt.md").write_text(
        "You edit one skill file. Call one tool per turn.\n", encoding="utf-8"
    )
    manifest = write_workspace(
        workspace,
        editor={**chat_editor_section(base_url), "system_prompt": "editor-prompt.md"},
    )

    result = await evolve(manifest, workspace / "run", adapter_factory=fake_adapters())

    assert_promoted(result)


async def test_an_invalid_proxy_url_is_refused_in_the_environment_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy URL is judged before the path check, on a run dir that also fails it.

    The refusal order is "schema, then every refusal that needs no filesystem,
    then path separation": a malformed proxy URL is an environment value, so it
    must be reported as itself and not hidden behind a layout mistake.
    """

    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    monkeypatch.setenv("E1_PROXY", "not a url")
    workspace = tmp_path / "ws"
    manifest = write_workspace(
        workspace,
        editor={
            "kind": "chat",
            "model": "m",
            "base_url": "https://gw.example/v1",
            "api_key_env": "E1_KEY",
            "proxy_env": "E1_PROXY",
        },
    )
    # A run directory inside the seed: `check_path_separation` would refuse it
    # too, and reporting *that* would misname the operator's actual mistake.
    run_dir = workspace / "seed" / "run"
    before = listing(workspace / "seed")

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == "editor_invalid"
    assert str(refusal.value) == "editor_invalid at editor.proxy_env"
    assert "not a url" not in str(refusal.value)
    assert not run_dir.exists()
    assert listing(workspace / "seed") == before


def test_run_dir_state_refuses_a_missing_parent(tmp_path: Path) -> None:
    """W2 finding F1: a new run directory needs an existing parent.

    The journal creates the leaf of `--run-dir` and never an ancestor, so a
    missing parent used to surface as `journal_unavailable` (exit 1) after
    admission. Creating the ancestor is not an option — the workflow writes
    only inside its own run directory — so it is a refusal, before any write.
    """

    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(tmp_path / "runs" / "r1")
    assert refusal.value.code == "run_dir_parent_missing"
    assert not (tmp_path / "runs").exists()

    # An existing parent that is not a directory is the same refusal.
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(EvolveRefused) as refusal:
        run_dir_state(tmp_path / "file" / "r1")
    assert refusal.value.code == "run_dir_parent_missing"


async def test_evolve_refuses_a_run_dir_whose_parent_is_missing(tmp_path: Path) -> None:
    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "runs" / "r1"
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert (refusal.value.code, refusal.value.path) == ("run_dir_parent_missing", None)
    assert not (tmp_path / "ws" / "runs").exists()


async def test_the_admitted_document_and_one_snapshot_decide_the_secret_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2 fix pack: no second read of the manifest or the environment.

    The CLI admits the document once and snapshots the environment once, then
    hands both to `redactions_for`, `short_forwarded_names` and `evolve`. A
    manifest rewritten after admission, or a variable changed after the
    snapshot, changes nothing: the campaign is armed with the admitted
    document's secrets and the printed line is redacted against the same set.
    """

    monkeypatch.setenv("EVO_TREE_TOKEN", "hunter2secret")
    monkeypatch.setenv("EVO_TREE_OTHER", "swordfish-value")
    manifest = write_workspace(tmp_path / "ws", editor_env=("EVO_TREE_TOKEN",))
    document = evolve_module.admit_manifest(manifest)
    environ = dict(os.environ)

    # The file changes after admission: another forwarded name, and the first
    # value changes in the live environment.
    write_workspace(tmp_path / "ws", editor_env=("EVO_TREE_OTHER",))
    monkeypatch.setenv("EVO_TREE_TOKEN", "rotated-value")

    seen: dict[str, Any] = {}
    started = evolve_module.run_campaign

    async def recording(policy: Any, **kwargs: Any) -> CampaignResult:
        seen.update(secrets=kwargs["secrets"], editor=kwargs["editor"])
        return await started(policy, **kwargs)

    monkeypatch.setattr(evolve_module, "run_campaign", recording)

    assert evolve_module.redactions_for(document, environ) == ("hunter2secret",)
    result = await evolve(
        document, tmp_path / "ws" / "run", adapter_factory=fake_adapters(), environ=environ
    )
    assert result.terminal == "finished"
    assert tuple(seen["secrets"]) == ("hunter2secret",)
    # The `subprocess` editor is bound from the same snapshot, so the value its
    # child is spawned with is the value the journal redacts, not the rotated one.
    assert seen["editor"].binding.child_env["EVO_TREE_TOKEN"] == "hunter2secret"
    # And the file as it now stands would arm a different set: the difference
    # is exactly what a single admission removes.
    assert evolve_module.redactions_for(manifest) == ("swordfish-value",)


# -- the Reflector role (v2 Reflector X1 §3.3, §5.5) -------------------------


async def test_evolve_runs_the_declared_reflector_and_snapshots_its_docs(
    tmp_path: Path,
) -> None:
    """The `/3` reflector section, end to end: the role runs, its documentation
    is snapshotted into the run directory, and editing that documentation is a
    changed run identity rather than a silently different brief."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(
        workspace, environment_docs={"api.md": "The target exposes /health.\n"}
    )
    run_dir = workspace / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    events = [json.loads(line)["event"] for line in _journal_lines(run_dir)]
    assert events.index("focus_portion_selected") < events.index("reflection_started")
    assert "reflection_finished" in events
    snapshot = run_dir / "environment-docs"
    assert snapshot.is_dir() and not snapshot.is_symlink()
    assert stat.S_IMODE(snapshot.lstat().st_mode) == 0o700
    assert (snapshot / "api.md").read_text(encoding="utf-8") == "The target exposes /health.\n"
    assert stat.S_IMODE((snapshot / "api.md").lstat().st_mode) == 0o600

    # A resume under the same documentation continues the campaign, and a
    # snapshot the run directory lost is rewritten from the live rendering it
    # still agrees with -- the one disagreement that is recoverable.
    shutil.rmtree(snapshot)
    again = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert again.terminal == "finished"
    assert (snapshot / "api.md").read_text(encoding="utf-8") == "The target exposes /health.\n"

    # The repair replaces the snapshot; it does not merge into it. A stray
    # entry left in the damaged copy would otherwise survive the rewrite and
    # be read back by the next resume's own rendering.
    (snapshot / "stray.md").write_text("not the rendering\n", encoding="utf-8")
    repaired = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert repaired.terminal == "finished"
    assert not (snapshot / "stray.md").exists()
    assert sorted(path.name for path in snapshot.iterdir()) == ["api.md"]

    (workspace / "env-docs" / "api.md").write_text("The target exposes /v2.\n", encoding="utf-8")
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.code == "run_identity_changed"


async def test_unreadable_environment_docs_are_refused_before_any_write(
    tmp_path: Path,
) -> None:
    """A directory that cannot be rendered is `environment_docs_invalid`, named
    at the manifest path the operator wrote and before the run root exists."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, environment_docs={"api.md": "ok\n"})
    (workspace / "env-docs" / "binary.md").write_bytes(b"\xff\xfe\x00")
    run_dir = workspace / "run"

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == "environment_docs_invalid"
    assert str(refusal.value.path) == "reflector.environment_docs"
    assert not (run_dir / CAMPAIGN_FILE).exists()


# -- the in-process Reflector kinds, bound and run (X1 §3.3) ------------------

#: One adaptive reflection: the preflight, one selected observation and one
#: claimed group. The family-4 validator requires the group to cite an actual
#: failing observation for every affected case.
CHAT_REFLECT_SCRIPT = [
    fake_chat_server.tool_call("list_files"),
    fake_chat_server.tool_call("list_evidence", {}),
    fake_chat_server.tool_call("read_evidence", {"evidence_id": "obs:c1:1", "offset": 0}),
    fake_chat_server.tool_call(
        "finish_reflection",
        {
            "groups": [
                {
                    "root_cause": "the skill never states the expected answer shape",
                    "suggested_fix": "state the expected answer shape in SKILL.md",
                    "evidence_ids": ["obs:c1:1"],
                    "affected_cases": ["c1"],
                    "target_paths": ["skills/SKILL.md"],
                    "counterevidence": "No passing observation was selected for this cohort.",
                    "regression_risks": "Keep the existing response format while changing the skill.",
                    "why_this_layer": "The observed failure is caused by the editable skill text.",
                }
            ]
        },
    ),
]


@pytest.fixture
def chat_reflect_endpoint():
    """The scripted OpenAI-compatible endpoint one `chat` Reflector talks to."""

    base_url, server = fake_chat_server.start(CHAT_REFLECT_SCRIPT)
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()


def chat_reflector_section(base_url: str) -> dict[str, Any]:
    return {
        "kind": "chat",
        "model": fake_chat_server.MODEL,
        "base_url": base_url,
        "api_key_env": "E1_KEY",
        "max_turns": 8,
    }


def agent_reflector_section(workspace: Path) -> dict[str, Any]:
    binary = workspace / "agent" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return {"kind": "agent_cli", "model": "codex", "agent_bin": "agent/codex"}


class FakeReflectorBackend:
    """The agent CLI of a Reflector: read the control plane, write the result."""

    constructed: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeReflectorBackend.constructed.append(kwargs)
        self.last_usage = {"tool_turns": 1}
        self.last_codex_diagnostics: dict[str, Any] = {}

    async def complete_file_edit(
        self, work_dir: Path, prompt: str, system: str | None = None, *, timeout_s: float = 0.0
    ) -> Any:
        control = Path(work_dir) / ".evolution"
        (control / "REFLECTION.json").write_text(
            json.dumps(
                {
                    "status": "reflected",
                    "groups": [
                        {
                            "root_cause": "the skill never states the expected answer shape",
                            "suggested_fix": "state the expected answer shape in SKILL.md",
                            "evidence_ids": ["obs:c1:1"],
                            "affected_cases": ["c1"],
                            "target_paths": ["skills/SKILL.md"],
                            "counterevidence": "No passing observation was selected for this cohort.",
                            "regression_risks": "Keep the existing response format while changing the skill.",
                            "why_this_layer": "The observed failure is caused by the editable skill text.",
                        }
                    ],
                    "note": "one hypothesis",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(content="diagnosed", tokens_in=1, tokens_out=1, time_ms=1)


def _reflection_finished(run_dir: Path) -> dict[str, Any]:
    events = [json.loads(line) for line in _journal_lines(run_dir)]
    payload = next(event["payload"] for event in events if event["event"] == "reflection_finished")
    return cast(dict[str, Any], payload)


async def test_chat_reflector_campaign_runs_end_to_end_against_the_fake_chat_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_reflect_endpoint: tuple[str, Any]
) -> None:
    """A `chat` reflector binds an `openai` client at the manifest's own endpoint.

    Everything below the binding is real: the campaign, the frozen read-only
    projection, the tool loop, the catalog, `validate_groups` and the journal.
    Only the provider is scripted.
    """

    base_url, server = chat_reflect_endpoint
    monkeypatch.setenv("E1_KEY", CHAT_KEY)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, reflector=chat_reflector_section(base_url))
    run_dir = workspace / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    assert server.script.served >= len(CHAT_REFLECT_SCRIPT)
    finished = _reflection_finished(run_dir)
    assert finished["status"] == "reflected"
    assert [group["target_paths"] for group in finished["groups"]] == [["skills/SKILL.md"]]
    logs = sorted((run_dir / "reflector-logs").glob("*.jsonl"))
    assert logs, "the chat reflector writes one transcript per reflection"
    for path in run_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert CHAT_KEY.encode() not in path.read_bytes(), path


async def test_agent_cli_reflector_campaign_runs_with_the_fake_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `agent_cli` reflector binds the sealed executable and the bound child env."""

    FakeReflectorBackend.constructed = []
    monkeypatch.setattr(agent_reflector_module, "AgentBackend", FakeReflectorBackend)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, reflector=agent_reflector_section(workspace))
    run_dir = workspace / "run"

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    finished = _reflection_finished(run_dir)
    assert finished["status"] == "reflected"
    assert finished["groups"][0]["evidence_ids"] == ["obs:c1:1"]
    assert FakeReflectorBackend.constructed, "the reflector builds one backend per call"
    kwargs = FakeReflectorBackend.constructed[0]
    assert kwargs["agent_bin"] == str(workspace / "agent" / "codex")
    assert kwargs["explicit"] is True
    assert set(kwargs["env"]) <= (_RUNTIME_ENV_KEYS | CURATED_ENV_NAMES("codex"))


def _journal_lines(run_dir: Path) -> list[bytes]:
    return [line for line in (run_dir / CAMPAIGN_FILE).read_bytes().splitlines() if line]


async def test_evolve_binds_the_campaign_namespace_and_hands_the_default_factory_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"
    handed: list[Any] = []
    fake = fake_adapters()

    def default_factory(manifest: Any, run_id: str, ledger: Any = None) -> Any:
        handed.append((run_id, ledger))
        return fake(manifest, run_id)

    monkeypatch.setattr(evolve_module, "_http_session_adapter", default_factory)
    ledger = TargetCallLedger()

    result = await evolve(manifest, run_dir, ledger=ledger)

    assert result.terminal == "finished"
    assert handed == [(derive_run_id(run_dir), ledger)]
    assert ledger.report().namespace == derive_run_id(run_dir)


async def test_a_custom_factory_gets_no_ledger_but_the_namespace_is_bound(
    tmp_path: Path,
) -> None:
    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"
    ledger = TargetCallLedger()

    await evolve(manifest, run_dir, adapter_factory=fake_adapters(), ledger=ledger)

    snap = ledger.report()
    assert snap.namespace == derive_run_id(run_dir)
    assert snap.bound_transports == 0  # coverage is the factory's to provide


async def test_a_refused_evolve_leaves_the_ledger_unbound(tmp_path: Path) -> None:
    ledger = TargetCallLedger()
    with pytest.raises(EvolveRefused):
        await evolve(tmp_path / "missing.json", tmp_path / "run", ledger=ledger)
    snap = ledger.report()
    assert snap.namespace is None and snap.admitted == 0


def test_the_real_default_factory_binds_one_transport_to_the_ledger(tmp_path: Path) -> None:
    """`_http_session_adapter` itself, not a fake, binds the namespace and the
    transport it builds — no socket is opened by construction (spec §4, §8)."""
    manifest_path = write_workspace(tmp_path / "ws")
    manifest = load_manifest_v2(manifest_path)
    ledger = TargetCallLedger()

    evolve_module._http_session_adapter(manifest, "run-x", ledger)

    snap = ledger.report()
    assert snap.bound_transports == 1
    assert snap.namespace == "run-x"


# -- operator authorizations (PDR-0010, ADR-0015) ----------------------------


def crash_before_finish(run_dir: Path) -> None:
    """Leave the journal as a crash would: a valid prefix with no `campaign_finished`.

    §3 and §4 make a *finished* prefix a hard refusal whatever the difference:
    there is nothing left to continue and no consequence record the lifecycle
    would accept. Every `evolve` fixture campaign runs to its terminal in one
    invocation, and `evolve` has no hook seam to interrupt it, so the
    authorizable half of the class table is reachable only over a run that
    stopped part-way. Dropping the terminal record leaves exactly the prefix a
    killed process leaves, which is the state an operator meets an identity
    change in.
    """

    path = run_dir / CAMPAIGN_FILE
    lines = path.read_bytes().splitlines(keepends=True)
    while lines and json.loads(lines[-1])["event"] == "campaign_finished":
        lines.pop()
    path.write_bytes(b"".join(lines))


def drop_last_record(run_dir: Path) -> None:
    """`crash_before_finish` generalized: drop exactly the journal's last record."""

    path = run_dir / CAMPAIGN_FILE
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-1]))


@contextmanager
def held_campaign_lock(run_dir: Path) -> Iterator[None]:
    """Hold `.campaign.lock` as a second writer would, through another descriptor."""

    fd = os.open(run_dir / CAMPAIGN_LOCK, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-link", "relative-link"])
def test_canonical_run_dir_anchors_the_root_to_the_link_it_hashed(
    tmp_path: Path, relative: bool
) -> None:
    """PDR-0010: the instance the digest quotes is the target the run writes into."""

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to("real" if relative else real)
    finding = canonical_run_dir_finding(link)
    assert finding is not None

    root, found = _canonical_run_dir(link, admitted={finding.instance_digest})

    assert found == finding
    expected = Path(
        os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(link)), finding.fact))
    )
    assert root == expected
    assert root == real.resolve()


def test_canonical_run_dir_never_resolves_the_link_a_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link re-pointed between the reading and the resolution must not be followed.

    The digest is bound to the target that was read; resolving `run_dir` again
    would let an authorization for A be accepted while the campaign writes under
    B. Here the finding reports A while the link on disk already points at B:
    the root has to be A (codex round 1).
    """

    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    link = tmp_path / "link"
    link.symlink_to(second)
    stale = HygieneFinding(
        "link", "symlinked", str(first), hygiene_instance_digest("link", "symlinked", str(first))
    )
    monkeypatch.setattr(evolve_module, "canonical_run_dir_finding", lambda _: stale)

    root, found = _canonical_run_dir(link, admitted={stale.instance_digest})

    assert found is stale
    assert root == first.resolve()


async def test_evolve_reads_the_journal_of_the_root_it_canonicalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5 source 1 is the journal *under the root*, not under the name given.

    Standing authority belongs to one campaign. `evolve` used to read the
    journal through the caller's path and canonicalize afterwards, so a run
    directory that is a link re-pointed from campaign A to campaign B between
    the two would apply A's admitted hygiene while inspecting and running B.
    The root is resolved first now, from the one `readlink` the finding's digest
    quotes, and every journal is read through it (codex round 2).

    The re-point is simulated as in the round-1 anchoring test: the finding
    reports A while the link on disk already points at B. The admitted set must
    be A's -- here A's own `scorer-traces` entry, which only A's journal
    carries, so a resume that needs no decision for it can only have read A's.
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    first, second = workspace / "a", workspace / "b"

    # Two campaigns, each with one accepted unsafe entry of its own, each left
    # as a crash leaves it: a resumable prefix.
    digests: dict[Path, str] = {}
    for run_dir, name in ((first, "scorer-traces"), (second, "editor-logs")):
        run_dir.mkdir()
        entry = run_dir / name
        entry.mkdir()
        os.chmod(entry, 0o755)
        with pytest.raises(EvolveRefused) as refused:
            await evolve(manifest, run_dir, adapter_factory=fake_adapters())
        assert (refused.value.code, refused.value.path) == ("run_dir_unsafe_entry", name)
        assert refused.value.instance_digest is not None
        digests[run_dir] = refused.value.instance_digest
        accepted = Authorization.new(
            "run_dir_unsafe_entry", digests[run_dir], "my own host", now="2026-09-17T10:00:00Z"
        )
        result = await evolve(
            manifest, run_dir, adapter_factory=fake_adapters(), authorizations=[accepted]
        )
        assert result.terminal == "finished"
        crash_before_finish(run_dir)
    assert evolve_module.journaled_hygiene_digests(first) == frozenset({digests[first]})
    assert evolve_module.journaled_hygiene_digests(second) == frozenset({digests[second]})

    link = workspace / "link"
    link.symlink_to(second)
    stale = HygieneFinding(
        "link", "symlinked", str(first), hygiene_instance_digest("link", "symlinked", str(first))
    )
    monkeypatch.setattr(evolve_module, "canonical_run_dir_finding", lambda _: stale)
    accept_link = Authorization.new(
        "run_dir_unsafe_entry", stale.instance_digest, "my own host", now="2026-09-17T10:00:00Z"
    )

    # C1 tightens the mode of an output root it owns, so the accepted fact is
    # restored here: the instance digest is the same one A's journal carries.
    os.chmod(first / "scorer-traces", 0o755)

    # The link on disk points at B; the finding -- and so the root -- is A. The
    # only decision supplied is the link's own, so A's `scorer-traces` is
    # admitted on A's standing authority or not at all.
    resumed = await evolve(
        manifest, link, adapter_factory=fake_adapters(), authorizations=[accept_link]
    )

    assert resumed.terminal == "finished"
    # A is the campaign that continued; B's prefix is untouched.
    events = [json.loads(line)["event"] for line in _journal_lines(first)]
    assert events[-1] == "campaign_finished"
    assert "campaign_finished" not in [json.loads(line)["event"] for line in _journal_lines(second)]


async def test_a_changed_editor_prompt_refuses_with_a_diff_and_continues_once_authorized(
    tmp_path: Path,
) -> None:
    """§4: the refusal is data, and the decision that quotes its digest rebases the run."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    first = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert first.terminal == "finished"
    crash_before_finish(run_dir)

    changed = write_workspace(
        workspace, editor_script=EDITOR_SCRIPT + "\n# v2\n", name="manifest-v2.json"
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.code == "run_identity_changed"
    assert [d.field for d in refusal.value.differences] == ["identity.editor.digest"]
    digest = refusal.value.instance_digest
    assert digest is not None

    auth = Authorization.new(
        "run_identity_changed", digest, "prompt v2", now="2026-09-17T10:00:00Z"
    )
    second = await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[auth])
    assert second.terminal == "finished"
    events = [json.loads(line)["event"] for line in _journal_lines(run_dir)]
    assert "identity_rebased" in events
    assert result_payload(second)["rebases"][0]["changed_fields"] == ["identity.editor.digest"]
    assert result_payload(second)["identity_digest"] != result_payload(first)["identity_digest"]


async def test_the_journaled_source_is_the_keyword_not_the_value(tmp_path: Path) -> None:
    """§5: `source` is set by core, so the audit record's provenance is objective.

    `Authorization` is an exported dataclass whose constructor takes `source`,
    and a library caller could otherwise hand `evolve` a decision claiming a
    decision file that never existed. The keyword decides instead:
    `authorizations` is always `caller` and `file_authorizations` -- what the
    CLI passes from `read_decisions` -- is always `file` (codex round 2).
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)

    changed = write_workspace(
        workspace, editor_script=EDITOR_SCRIPT + "\n# v2\n", name="manifest-v2.json"
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.instance_digest is not None
    forged = Authorization(
        "run_identity_changed",
        refusal.value.instance_digest,
        "prompt v2",
        "2026-09-17T10:00:00Z",
        source="file",  # the caller's claim, which the library boundary overrides
    )
    assert (
        await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[forged])
    ).terminal == "finished"
    crash_before_finish(run_dir)

    # And the other keyword, with a value that claims the other source.
    third = write_workspace(
        workspace, editor_script=EDITOR_SCRIPT + "\n# v3\n", name="manifest-v3.json"
    )
    with pytest.raises(EvolveRefused) as second:
        await evolve(third, run_dir, adapter_factory=fake_adapters())
    assert second.value.instance_digest is not None
    from_file = Authorization(
        "run_identity_changed",
        second.value.instance_digest,
        "prompt v3",
        "2026-09-17T11:00:00Z",
        source="caller",
    )
    assert (
        await evolve(
            third, run_dir, adapter_factory=fake_adapters(), file_authorizations=[from_file]
        )
    ).terminal == "finished"

    rebases = [
        json.loads(line)["payload"]
        for line in _journal_lines(run_dir)
        if json.loads(line)["event"] == "identity_rebased"
    ]
    assert [r["authorization"]["id"] for r in rebases] == [forged.id, from_file.id]
    assert [r["authorization"]["source"] for r in rebases] == ["caller", "file"]


@pytest.mark.parametrize(
    ("decision", "key"),
    [
        (
            Authorization("pin_mismatch", "a" * 64, "let me through", "2026-09-17T10:00:00Z"),
            "code",
        ),
        (
            Authorization("run_identity_changed", "nope", "a reason", "2026-09-17T10:00:00Z"),
            "digest",
        ),
        (Authorization("run_identity_changed", "a" * 64, "", "2026-09-17T10:00:00Z"), "reason"),
        (Authorization("run_identity_changed", "a" * 64, "a reason", "yesterday"), "issued_at"),
    ],
    ids=["hard-code", "malformed-digest", "empty-reason", "free-text-issued-at"],
)
async def test_a_hand_built_decision_is_validated_before_any_other_admission(
    tmp_path: Path, decision: Authorization, key: str
) -> None:
    """§5: a library caller's hand-built decision passes the file reader's own validation.

    `Authorization.from_payload` refuses one read from a file or a payload, but
    the exported frozen dataclass checks nothing, and constructing it directly
    is how Lab's in-process consumers build decisions. A hard code was already
    refused here; so now is every other malformed field, at the key at fault --
    an empty reason or a free-text `issued_at` would otherwise match a real
    refusal and fail much later, as a journal schema violation after the first
    durable write. The manifest path below does not exist: were the check
    anywhere later, this would refuse `bad_path`.
    """

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(tmp_path / "absent.json", tmp_path / "run", authorizations=[decision])

    assert refusal.value.code == "authorization_invalid"
    assert refusal.value.path == key
    assert not (tmp_path / "run").exists()


async def test_a_changed_scorer_names_the_field_and_a_foreign_decision_closes_nothing(
    tmp_path: Path,
) -> None:
    """Comparable-links §5.1: the change is a close, bound to its own instance only.

    A decision for another instance is not one: the refusal stands, still
    offering the close, and the journal is untouched.
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    # Unfinished, so the comparable field is authorizable with effect `close`.
    crash_before_finish(run_dir)
    changed = write_workspace(
        workspace, scorer_script=SCORER_SCRIPT + "\n# v2\n", name="manifest-s2.json"
    )
    before = _journal_lines(run_dir)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(
            changed,
            run_dir,
            adapter_factory=fake_adapters(),
            authorizations=[
                Authorization.new(
                    "run_identity_changed", "a" * 64, "try", now="2026-09-17T10:00:00Z"
                )
            ],
        )
    assert refusal.value.code == "run_identity_changed"
    assert refusal.value.effect == "close"
    assert refusal.value.instance_digest not in (None, "a" * 64)
    assert "identity.scorer.digest" in [d.field for d in refusal.value.differences]
    assert _journal_lines(run_dir) == before


async def test_a_wrong_digest_leaves_the_refusal_standing_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """§4: an authorization is bound to both sides; one for another transition is not one."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    # Unfinished, so the transition really is one a *correct* decision accepts.
    crash_before_finish(run_dir)
    changed = write_workspace(
        workspace, editor_script=EDITOR_SCRIPT + "\n# v2\n", name="manifest-v2.json"
    )
    before = listing(run_dir)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(
            changed,
            run_dir,
            adapter_factory=fake_adapters(),
            authorizations=[
                Authorization.new(
                    "run_identity_changed", "b" * 64, "wrong", now="2026-09-17T10:00:00Z"
                )
            ],
        )
    assert refusal.value.code == "run_identity_changed"
    assert listing(run_dir) == before


async def test_a_reason_holding_a_secret_is_refused_at_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9: a decision is journaled, so its reason obeys the run's own redaction set."""

    monkeypatch.setenv("SCORER_TOKEN", "hunter2hunter2")
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, scorer_env=("SCORER_TOKEN",))
    run_dir = workspace / "run"
    auth = Authorization.new(
        "run_identity_changed", "a" * 64, "token is hunter2hunter2", now="2026-09-17T10:00:00Z"
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters(), authorizations=[auth])
    assert refusal.value.code == "record_matches_secret"
    assert refusal.value.path == f"authorizations/{auth.id}"
    assert not run_dir.exists()


async def test_a_permission_finding_is_admitted_once_and_not_needed_next_time(
    tmp_path: Path,
) -> None:
    """§5: the journal is standing authority for the instance it recorded, and only it."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    run_dir.mkdir()
    traces = run_dir / "scorer-traces"
    traces.mkdir(mode=0o755)
    os.chmod(traces, 0o755)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.code == "run_dir_unsafe_entry"
    assert refusal.value.path == "scorer-traces"
    digest = refusal.value.instance_digest
    assert digest is not None
    auth = Authorization.new(
        "run_dir_unsafe_entry", digest, "shared box", now="2026-09-17T10:00:00Z"
    )

    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters(), authorizations=[auth])
    assert result.terminal == "finished"
    events = [json.loads(line) for line in _journal_lines(run_dir)]
    assert events[1]["event"] == "authorization_applied"
    assert events[1]["payload"]["path"] == "scorer-traces"
    # Journaled: a resume with no authorization at all is admitted; a symlink is not.
    again = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert again.terminal == "finished"
    shutil.rmtree(traces)
    traces.symlink_to(tmp_path)
    with pytest.raises(EvolveRefused) as link:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert link.value.code == "run_dir_unsafe_entry" and link.value.instance_digest != digest


async def test_a_hygiene_digest_supplied_by_both_sources_keeps_the_callers_decision(
    tmp_path: Path,
) -> None:
    """§5: the fixed source order is first-match-wins, for hygiene as for identity.

    ADR-0015 orders the sources "the journals ... the library argument, the
    decision directory". A dictionary built caller-then-file would let the later
    file decision replace the caller's for the same instance, and the
    `authorization_applied` record would then carry the directory's reason and
    `source: file` (codex round 3).
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    run_dir.mkdir()
    traces = run_dir / "scorer-traces"
    traces.mkdir(mode=0o755)
    os.chmod(traces, 0o755)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    digest = refusal.value.instance_digest
    assert digest is not None
    from_caller = Authorization.new(
        "run_dir_unsafe_entry", digest, "the caller's reason", now="2026-09-17T10:00:00Z"
    )
    from_file = Authorization.new(
        "run_dir_unsafe_entry", digest, "the directory's reason", now="2026-09-17T11:00:00Z"
    )
    assert from_caller.id != from_file.id  # the same instance, two decisions

    result = await evolve(
        manifest,
        run_dir,
        adapter_factory=fake_adapters(),
        authorizations=[from_caller],
        file_authorizations=[from_file],
    )

    assert result.terminal == "finished"
    applied = [
        json.loads(line)["payload"]
        for line in _journal_lines(run_dir)
        if json.loads(line)["event"] == "authorization_applied"
    ]
    assert [entry["authorization"]["reason"] for entry in applied] == ["the caller's reason"]
    assert [entry["authorization"]["source"] for entry in applied] == ["caller"]
    assert [entry["authorization"]["id"] for entry in applied] == [from_caller.id]


async def test_evolve_reads_the_decision_directory_of_the_root_it_canonicalized(
    tmp_path: Path,
) -> None:
    """PDR-0010: `decision_directory` reads `<canonical root>/authorizations/`.

    The CLI used to read the directory through the `--run-dir` name before core
    resolved that name to the root it inspects and mutates. A link re-pointed
    between the two reads would hand campaign A's decisions to an invocation
    running campaign B (codex round 3). `evolve` now reads the directory itself,
    after `_resolve_run_dir` and through its result, so the decisions a campaign
    consumes are always the ones under the root it is written to: here the same
    name reaches two campaigns in turn, and each admits its own link only from
    its own directory.
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    first_root, second_root = workspace / "a", workspace / "b"
    first_root.mkdir()
    second_root.mkdir()
    link = workspace / "run"
    link.symlink_to(first_root)

    with pytest.raises(EvolveRefused) as first:
        await evolve(manifest, link, adapter_factory=fake_adapters(), decision_directory=True)
    assert first.value.code == "run_dir_unsafe_entry"
    assert first.value.instance_digest is not None
    write_decision(
        first_root,
        Authorization.new(
            "run_dir_unsafe_entry",
            first.value.instance_digest,
            "campaign A",
            now="2026-09-17T10:00:00Z",
        ),
    )
    assert (
        await evolve(manifest, link, adapter_factory=fake_adapters(), decision_directory=True)
    ).terminal == "finished"

    # The same name, moved to the other campaign: the digest quotes the target,
    # and the decision that admits it has to be under *that* root.
    link.unlink()
    link.symlink_to(second_root)
    with pytest.raises(EvolveRefused) as second:
        await evolve(manifest, link, adapter_factory=fake_adapters(), decision_directory=True)
    assert second.value.code == "run_dir_unsafe_entry"
    assert second.value.instance_digest not in (None, first.value.instance_digest)
    write_decision(
        second_root,
        Authorization.new(
            "run_dir_unsafe_entry",
            second.value.instance_digest,
            "campaign B",
            now="2026-09-17T11:00:00Z",
        ),
    )

    result = await evolve(manifest, link, adapter_factory=fake_adapters(), decision_directory=True)

    assert result.terminal == "finished"
    applied = [
        json.loads(line)["payload"]
        for line in _journal_lines(second_root)
        if json.loads(line)["event"] == "authorization_applied"
    ]
    assert [entry["authorization"]["reason"] for entry in applied] == ["campaign B"]
    assert [entry["authorization"]["source"] for entry in applied] == ["file"]
    reasons = [
        json.loads(line)["payload"]["authorization"]["reason"]
        for line in _journal_lines(first_root)
        if json.loads(line)["event"] == "authorization_applied"
    ]
    assert reasons == ["campaign A"]


async def test_a_previous_producing_version_is_a_diff_on_producing_version(
    tmp_path: Path,
) -> None:
    """§4: an upgraded package is a difference the operator may accept, not a fault."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)
    measurements = sorted((run_dir / "measurements").glob("*/events.jsonl"))
    assert measurements
    for path in [run_dir / CAMPAIGN_FILE, run_dir / "artifacts.jsonl", *measurements]:
        lines = path.read_bytes().splitlines(keepends=True)
        record = json.loads(lines[0])
        # Later records cite earlier ones by byte range, so keep the version's length.
        current = record["payload"]["producing_version"]
        record["payload"]["producing_version"] = "0.3.0a" + "0" * (len(current) - 6)
        lines[0] = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        path.write_bytes(b"".join(lines))
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.code == "run_identity_changed"
    assert [d.field for d in refusal.value.differences] == ["producing_version"]
    assert refusal.value.instance_digest is not None
    auth = Authorization.new(
        "run_identity_changed", refusal.value.instance_digest, "upgrade", now="2026-09-17T10:00:00Z"
    )
    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters(), authorizations=[auth])
    assert result.terminal == "finished"


async def test_admission_never_repairs_the_documentation_snapshot(tmp_path: Path) -> None:
    """ADR-0015: admission is atomic and writes nothing; C1 repairs its own directory.

    `_environment_docs` used to rewrite a missing or damaged snapshot on the
    spot, so an invocation refused a moment later for an unrelated identity
    difference had already written into the run directory. It now reports the
    repair, and C1 performs it after its consequence records (codex round 1).
    """

    workspace = tmp_path / "ws"
    docs = {"api.md": "The target exposes /health.\n"}
    manifest = write_workspace(workspace, environment_docs=docs)
    run_dir = workspace / "run"
    assert (await evolve(manifest, run_dir, adapter_factory=fake_adapters())).terminal == "finished"
    crash_before_finish(run_dir)
    stored = run_dir / "environment-docs"
    shutil.rmtree(stored)

    # The live documentation still has the journaled digest, so the stored copy
    # is merely lost -- but the editor prompt moved, and no decision covers it.
    changed = write_workspace(
        workspace,
        environment_docs=docs,
        editor_script=EDITOR_SCRIPT + "\n# v2\n",
        name="manifest-v2.json",
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())

    assert refusal.value.code == "run_identity_changed"
    assert [d.field for d in refusal.value.differences] == ["identity.editor.digest"]
    assert not stored.exists()  # the refusal left the run directory as it found it

    # The same run directory without the prompt change: nothing refuses, and the
    # campaign repairs the snapshot it owns.
    (workspace / "editor" / "edit.py").write_text(EDITOR_SCRIPT, encoding="utf-8")
    resumed = await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert resumed.terminal == "finished"
    assert (stored / "api.md").read_text(encoding="utf-8") == docs["api.md"]


async def test_an_authorized_documentation_change_re_takes_the_run_snapshot(
    tmp_path: Path,
) -> None:
    """The live-snapshot hand-off: `_environment_docs` stops refusing so C1 can report.

    A changed documentation source used to be `run_identity_changed` raised by
    `_environment_docs` itself -- a refusal with no diff and no digest, which no
    decision could ever accept. `evolve` now hands C1 the *live* snapshot, C1
    names the field, and the accepted rebase re-takes the run directory's own
    copy so the journal and the snapshot agree again.
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(
        workspace, environment_docs={"api.md": "The target exposes /health.\n"}
    )
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)
    snapshot = run_dir / "environment-docs" / "api.md"
    assert snapshot.read_text(encoding="utf-8") == "The target exposes /health.\n"

    (workspace / "env-docs" / "api.md").write_text("The target exposes /v2.\n", encoding="utf-8")
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert refusal.value.code == "run_identity_changed"
    assert "identity.reflector.environment_docs_digest" in [
        d.field for d in refusal.value.differences
    ]
    digest = refusal.value.instance_digest
    assert digest is not None
    # The refusal wrote nothing: the run directory still holds the old rendering.
    assert snapshot.read_text(encoding="utf-8") == "The target exposes /health.\n"

    auth = Authorization.new("run_identity_changed", digest, "docs v2", now="2026-09-17T10:00:00Z")
    result = await evolve(manifest, run_dir, adapter_factory=fake_adapters(), authorizations=[auth])
    assert result.terminal == "finished"
    assert snapshot.read_text(encoding="utf-8") == "The target exposes /v2.\n"
    # And a further resume under the same documentation needs no decision at
    # all, and still repairs a snapshot the run directory lost. Both are
    # `_journaled_docs_digest` reading the *rebased* identity: against the
    # creation record's digest the live source looks changed again, the resume
    # takes the live snapshot instead of loading the stored one, and the
    # repair below never happens.
    shutil.rmtree(run_dir / "environment-docs")
    again = await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    assert again.terminal == "finished"
    assert snapshot.read_text(encoding="utf-8") == "The target exposes /v2.\n"


#: The exception each pre-write code arrives as, and the code `evolve` must
#: give it. `adaptive_runtime_requires_policy_v5` is the one that is not its own
#: name: both adaptive-runtime `ValueError`s are the operator's
#: `adaptive_runtime_required`, with the raised name kept as private detail.
PRE_WRITE_RAISERS: dict[str, Callable[[], Exception]] = {
    "package_version_unavailable": lambda: IdentityError("package_version_unavailable"),
    "stored_identity_digest_mismatch": lambda: IdentityError("stored_identity_digest_mismatch"),
    "undeclared_sibling": lambda: UndeclaredSibling("undeclared_sibling"),
    "artifact_ledger_missing": lambda: ArtifactLedgerMissing(),
    "capabilities_insufficient": lambda: CapabilitiesInsufficient("capabilities_insufficient"),
    "adaptive_runtime_required": lambda: ValueError("adaptive_runtime_requires_policy_v5"),
}


@pytest.mark.parametrize("code", sorted(PRE_WRITE_RAISERS))
async def test_every_pre_write_code_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """§3's class table is the map: every code in it becomes `EvolveRefused`."""

    manifest = write_workspace(tmp_path / "ws")
    raiser = PRE_WRITE_RAISERS[code]

    async def refusing(policy: Any, **kwargs: Any) -> CampaignResult:
        raise raiser()

    monkeypatch.setattr(evolve_module, "run_campaign", refusing)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, tmp_path / "ws" / "run", adapter_factory=fake_adapters())
    assert refusal.value.code == code
    if code == "adaptive_runtime_required":
        # The public code is one; which condition failed stays private.
        assert refusal.value.detail == "adaptive_runtime_requires_policy_v5"
        assert str(refusal.value) == "adaptive_runtime_required"


@pytest.mark.parametrize(
    "raised",
    [
        "adaptive_runtime_required",
        "adaptive_runtime_requires_policy_v4",
        "adaptive_runtime_requires_policy_v5",
        # G2 spec §4: a resume across the `/13` boundary (family 12 given a
        # non-`/13` manifest, or family 11 given a `/13` one).
        "adaptive_runtime_policy_v9_family_mismatch",
        # F8: the same across the `/14` boundary.
        "adaptive_runtime_policy_v10_family_mismatch",
    ],
)
async def test_every_adaptive_runtime_value_error_is_adaptive_runtime_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: str
) -> None:
    """Each adaptive-runtime `ValueError` is one public code; its name is the detail."""

    manifest = write_workspace(tmp_path / "ws")

    async def refusing(policy: Any, **kwargs: Any) -> CampaignResult:
        raise ValueError(raised)

    monkeypatch.setattr(evolve_module, "run_campaign", refusing)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, tmp_path / "ws" / "run", adapter_factory=fake_adapters())
    assert refusal.value.code == "adaptive_runtime_required"
    assert refusal.value.detail == raised
    assert not (tmp_path / "ws" / "run").exists()


#: I2 Task 7: the refusals an in-place condition change raises before any
#: write, and what `evolve` must make of each -- `(code, path)`.
CONDITION_CHANGE_RAISERS: dict[str, tuple[Callable[[], Exception], str | None]] = {
    "authorized_identity_changed": (
        lambda: AuthorizedIdentityChanged(
            (RefusalDifference("identity_digest", "a" * 64, "b" * 64),)
        ),
        None,
    ),
    "rebound_source_corrupt": (lambda: ReboundSourceCorrupt("m-r01-a01"), "m-r01-a01"),
    # I2 Task 8 (§3.4): the key, the journaled spend and the new value.
    "policy_contradicts_prefix": (lambda: PolicyContradictsPrefix("rounds", "2", "1"), "rounds"),
    "authorization_conflict": (lambda: AuthorizationConflict(("id-1", "id-2")), "id-1,id-2"),
    "authorization_invalid": (
        lambda: AuthorizationError("authorization_invalid", "effect"),
        "effect",
    ),
}


@pytest.mark.parametrize("code", sorted(CONDITION_CHANGE_RAISERS))
async def test_a_condition_change_refusal_is_typed_and_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    manifest = write_workspace(tmp_path / "ws")
    raiser, path = CONDITION_CHANGE_RAISERS[code]

    async def refusing(policy: Any, **kwargs: Any) -> CampaignResult:
        raise raiser()

    monkeypatch.setattr(evolve_module, "run_campaign", refusing)
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, tmp_path / "ws" / "run", adapter_factory=fake_adapters())
    assert (refusal.value.code, refusal.value.path) == (code, path)
    assert refusal.value.instance_digest is None
    assert refusal_class(code) == "hard"
    if code == "authorized_identity_changed":
        assert [d.field for d in refusal.value.differences] == ["identity_digest"]
        assert refusal.value.detail == "identity_digest"  # the key that moved
    if code == "policy_contradicts_prefix":
        assert [(d.field, d.journaled, d.observed) for d in refusal.value.differences] == [
            ("rounds", "2", "1")
        ]


@pytest.mark.parametrize("code", sorted(CONDITION_CHANGE_RAISERS))
async def test_a_condition_change_refusal_type_after_the_first_write_is_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """Final fix wave: the I2 refusal types follow `_PRE_WRITE_CODES`'s
    discipline -- raised after the invocation's first durable write (C1 marks
    it) they are faults and propagate unchanged, never `EvolveRefused`."""

    manifest = write_workspace(tmp_path / "ws")
    raiser, _path = CONDITION_CHANGE_RAISERS[code]
    raised = raiser()
    raised.after_first_write = True  # type: ignore[attr-defined]  # what C1 sets

    async def faulting(policy: Any, **kwargs: Any) -> CampaignResult:
        raise raised

    monkeypatch.setattr(evolve_module, "run_campaign", faulting)
    with pytest.raises(type(raised)) as fault:
        await evolve(manifest, tmp_path / "ws" / "run", adapter_factory=fake_adapters())
    assert fault.value is raised
    assert not isinstance(fault.value, EvolveRefused)


class RecordingHandle:
    """A binary file that remembers the size bound of every `readline` it served."""

    def __init__(self, handle: Any, sizes: list[int]) -> None:
        self._handle = handle
        self._sizes = sizes

    def readline(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        return cast(bytes, self._handle.readline(size))

    def __enter__(self) -> RecordingHandle:
        return self

    def __exit__(self, *exc_info: object) -> Literal[False]:
        self._handle.close()
        return False


def record_journal_reads(monkeypatch: pytest.MonkeyPatch, sizes: list[int]) -> None:
    """Make every `campaign.jsonl` opened through `Path.open` record its reads."""

    real_open = Path.open

    def recording(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        return RecordingHandle(handle, sizes) if self.name == CAMPAIGN_FILE else handle

    monkeypatch.setattr(Path, "open", recording)


async def test_the_best_effort_journal_readers_are_bounded_and_resynchronize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither admission reader may materialize a line the journal's own bound rejects.

    A crash leaves torn journals, and a torn journal's long line is exactly what
    `MAX_RECORD_BYTES` exists to refuse -- but a reader that iterates the handle
    line by line has already allocated the whole line before it can apply the
    bound. Both readers must step over such a line in bounded reads and pick the
    next record up, and must decline a file past `MAX_JOURNAL_BYTES` on its
    `st_size` alone.
    """

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    docs_digest, instance = "a" * 64, "c" * 64
    records = [
        {
            "event": "campaign_created",
            "payload": {"identity": {"reflector": {"environment_docs_digest": docs_digest}}},
        },
        {"event": "torn", "payload": {"pad": "p" * 4096}},
        {"event": "authorization_applied", "payload": {"instance_digest": instance}},
    ]
    journal = run_dir / CAMPAIGN_FILE
    journal.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    monkeypatch.setattr(evolve_module, "MAX_RECORD_BYTES", 512)

    sizes: list[int] = []
    record_journal_reads(monkeypatch, sizes)

    # The oversized second record is stepped over; the records on either side of
    # it are still read, and the creation record keeps line number 0.
    assert evolve_module.journaled_hygiene_digests(run_dir) == frozenset({instance})
    assert evolve_module._journaled_docs_digest(run_dir) == docs_digest
    assert sizes and all(size == 513 for size in sizes)

    # And a journal past the whole-file bound answers nothing, unread.
    monkeypatch.setattr(evolve_module, "MAX_JOURNAL_BYTES", 8)
    sizes.clear()
    assert evolve_module.journaled_hygiene_digests(run_dir) == frozenset()
    assert evolve_module._journaled_docs_digest(run_dir) is None
    assert sizes == []


def test_a_record_exactly_at_the_bound_is_whole_and_keeps_its_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminator, not the length alone, tells a whole record from a torn one.

    `readline(MAX_RECORD_BYTES + 1)` returns `MAX_RECORD_BYTES + 1` bytes for a
    *legal* record of exactly the bound plus its newline. Reading that as
    oversized would discard it and then let the resynchronizing skip loop eat
    the record after it -- two records lost to a journal that is well formed.
    """

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bound, instance = 512, "c" * 64
    monkeypatch.setattr(evolve_module, "MAX_RECORD_BYTES", bound)
    exact: dict[str, Any] = {"event": "campaign_created", "payload": {"pad": ""}}
    exact["payload"]["pad"] = "p" * (bound - len(json.dumps(exact)))
    line = json.dumps(exact)
    assert len(line) == bound
    follower = json.dumps(
        {"event": "authorization_applied", "payload": {"instance_digest": instance}}
    )
    (run_dir / CAMPAIGN_FILE).write_bytes(f"{line}\n{follower}\n".encode())

    read = list(evolve_module._journal_records(run_dir))

    assert [number for number, _ in read] == [0, 1]
    assert [raw["event"] for _, raw in read] == ["campaign_created", "authorization_applied"]
    assert evolve_module.journaled_hygiene_digests(run_dir) == frozenset({instance})


def in_memory_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the CLI's own default factory C1's in-memory adapter.

    `evolve` builds `_http_session_adapter` itself unless a caller passes one,
    and only a caller inside the process can. A CLI test of a refusal that fires
    inside `resume_campaign` -- after the adapter exists -- would otherwise need
    a served target for a campaign that never sends a turn.
    """

    monkeypatch.setattr(
        evolve_module,
        "_http_session_adapter",
        lambda _manifest, _run_id, **_kwargs: FakeAdapter(),
    )


async def changed_scorer_refusal(
    tmp_path: Path, *, schema: str = SCHEMA_V15
) -> tuple[Path, Path, EvolveRefused]:
    """An unfinished campaign resumed under a changed scorer (D2a §5.1)."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace, schema=schema)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)
    changed = write_workspace(
        workspace,
        scorer_script=SCORER_SCRIPT + "\n# v2\n",
        name="manifest-s2.json",
        schema=schema,
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    return changed, run_dir, refusal.value


async def test_a_comparable_change_on_an_unfinished_campaign_is_a_close(tmp_path: Path) -> None:
    changed, run_dir, refused = await changed_scorer_refusal(tmp_path)
    assert refused.code == "run_identity_changed" and refused.effect == "close"
    assert refused.instance_digest is not None
    before = _journal_lines(run_dir)

    decision = Authorization.new(
        "run_identity_changed", refused.instance_digest, "scorer v2", now="2026-09-18T10:00:00Z"
    )
    closed = await evolve(
        changed, run_dir, adapter_factory=fake_adapters(), authorizations=[decision]
    )

    after = [json.loads(line) for line in _journal_lines(run_dir)[len(before) :]]
    events = [record["event"] for record in after]
    # An owed alpha row may precede the two records (existing rule); nothing else may.
    assert events[-2:] == ["authorization_applied", "campaign_finished"]
    assert set(events[:-2]) <= {"alpha_consumed"}
    applied, finished = after[-2]["payload"], after[-1]["payload"]
    assert applied["effect"] == "close" and "path" not in applied
    assert applied["authorization"]["code"] == "run_identity_changed"
    assert (finished["outcome"], finished["reason"]) == ("stopped", "condition_changed")
    assert finished["continuation"] == {"kind": "linked_run", "reason": "condition_changed"}
    assert "identity_rebased" not in events
    assert exit_code_for(closed) == 3


async def test_a_closed_campaign_refuses_as_finished_and_links(tmp_path: Path) -> None:
    changed, run_dir, refused = await changed_scorer_refusal(tmp_path)
    assert refused.instance_digest is not None
    decision = Authorization.new(
        "run_identity_changed", refused.instance_digest, "scorer v2", now="2026-09-18T10:00:00Z"
    )
    closed = await evolve(
        changed, run_dir, adapter_factory=fake_adapters(), authorizations=[decision]
    )

    with pytest.raises(EvolveRefused) as again:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    assert again.value.instance_digest is None  # finished: hard

    champion = champion_path_of(closed)
    successor = write_workspace(
        tmp_path / "succ",
        linked_from=str(run_dir),
        seed_root=f"{run_dir}/{champion}",
        scorer_script=SCORER_SCRIPT + "\n# v2\n",
    )
    linked = await evolve(successor, tmp_path / "run-next", adapter_factory=fake_adapters())
    assert linked.terminal == "finished"
    # The closed journal holds the old scorer; the successor declares the new one.
    # ADR-0032: a stable comparison always reports its stored Champion side.
    assert [c["kind"] for c in result_payload(linked)["caveats"]] == [
        "condition_changed",
        "champion_not_contemporaneous",
    ]
    assert "identity.scorer.digest" in result_payload(linked)["caveats"][0]["fields"]
    # ADR-0032: closing preserves the stable comparison's recorded caveats.
    assert result_payload(closed)["caveats"] == [
        {"attempt_id": None, "kind": "champion_not_contemporaneous", "round": None},
        {"attempt_id": "r01-a01", "kind": "focus_selection_bias", "round": 1},
        {"attempt_id": "r01-a01", "kind": "intervals_empirical", "round": 1},
        {"attempt_id": "r01-a01", "kind": "exact_contrast", "round": 1},
    ]


async def test_a_close_record_admits_no_hygiene_instance(tmp_path: Path) -> None:
    """A close's instance digest is an identity instance, never a run-directory entry's."""

    changed, run_dir, refused = await changed_scorer_refusal(tmp_path)
    assert refused.instance_digest is not None
    decision = Authorization.new(
        "run_identity_changed", refused.instance_digest, "x", now="2026-09-18T10:00:00Z"
    )
    await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[decision])
    applied = json.loads(_journal_lines(run_dir)[-2])
    assert applied["event"] == "authorization_applied"
    assert applied["payload"]["instance_digest"] == refused.instance_digest
    assert evolve_module.journaled_hygiene_digests(run_dir) == frozenset()
    assert decision.id in evolve_module.journaled_authorization_ids(run_dir)


async def test_a_comparable_and_a_non_comparable_change_get_one_close(tmp_path: Path) -> None:
    """§5.1: one refusal, effect close; the non-comparable difference is accepted with it."""

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)
    changed = write_workspace(
        workspace,
        scorer_script=SCORER_SCRIPT + "\n# v2\n",
        editor_script=EDITOR_SCRIPT + "\n# v2\n",
        name="manifest-both.json",
    )
    with pytest.raises(EvolveRefused) as refusal:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    refused = refusal.value
    fields = [d.field for d in refused.differences]
    assert "identity.scorer.digest" in fields and "identity.editor.digest" in fields
    assert refused.effect == "close" and refused.instance_digest is not None
    assert len(refused.findings) == 1

    decision = Authorization.new(
        "run_identity_changed", refused.instance_digest, "both", now="2026-09-18T10:00:00Z"
    )
    closed = await evolve(
        changed, run_dir, adapter_factory=fake_adapters(), authorizations=[decision]
    )

    events = [json.loads(line)["event"] for line in _journal_lines(run_dir)]
    assert "identity_rebased" not in events
    assert events[-2:] == ["authorization_applied", "campaign_finished"]
    assert closed.finished is not None
    assert (closed.finished["outcome"], closed.finished["reason"]) == (
        "stopped",
        "condition_changed",
    )


async def test_the_close_writes_no_sibling_ledger_or_docs(tmp_path: Path) -> None:
    changed, run_dir, refused = await changed_scorer_refusal(tmp_path)
    assert refused.instance_digest is not None
    before = listing(run_dir)
    decision = Authorization.new(
        "run_identity_changed", refused.instance_digest, "x", now="2026-09-18T10:00:00Z"
    )
    await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[decision])
    after = listing(run_dir)
    changed_entries = {name for name in after if before.get(name) != after[name]}
    assert changed_entries <= {"run", "run/campaign.jsonl", "run/.campaign.lock"}


CRASHING_SCORER_SCRIPT = "import sys\nsys.exit(3)\n"


# -- the owed terminal, recovered state-first (D2a §5.3) ----------------------


def target_down(manifest: Any, run_id: str) -> Any:
    raise AssertionError("no adapter may be built for an owed terminal")


async def closed_then_torn(tmp_path: Path, *, schema: str = SCHEMA_V15) -> tuple[Path, Path]:
    """An accepted close whose `campaign_finished` a crash never wrote."""

    changed, run_dir, refused = await changed_scorer_refusal(tmp_path, schema=schema)
    assert refused.instance_digest is not None
    decision = Authorization.new(
        "run_identity_changed", refused.instance_digest, "x", now="2026-09-18T10:00:00Z"
    )
    await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[decision])
    drop_last_record(run_dir)
    last = json.loads(_journal_lines(run_dir)[-1])
    assert (last["event"], last["payload"]["effect"]) == ("authorization_applied", "close")
    return changed, run_dir


async def test_a_crash_after_the_close_record_is_finished_without_the_target(
    tmp_path: Path,
) -> None:
    changed, run_dir = await closed_then_torn(tmp_path)
    before = _journal_lines(run_dir)

    result = await evolve(changed, run_dir, adapter_factory=target_down)

    assert result.finished is not None
    assert (result.finished["outcome"], result.finished["reason"]) == (
        "stopped",
        "condition_changed",
    )
    assert exit_code_for(result) == 3
    after = _journal_lines(run_dir)
    assert after[: len(before)] == before and len(after) == len(before) + 1
    finished = json.loads(after[-1])
    assert finished["event"] == "campaign_finished"
    assert finished["payload"]["continuation"] == {
        "kind": "linked_run",
        "reason": "condition_changed",
    }
    # A finished prefix behaves as any finished campaign: nothing is owed twice.
    with pytest.raises(EvolveRefused) as again:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())
    assert again.value.instance_digest is None


async def test_the_owed_terminal_needs_no_bindings(tmp_path: Path) -> None:
    _changed, run_dir = await closed_then_torn(tmp_path)
    # A manifest whose editor needs a variable the environment lacks: any binding
    # would refuse `editor_env_missing`; state-first recovery never builds one.
    unbindable = write_workspace(
        tmp_path / "ws",
        scorer_script=SCORER_SCRIPT + "\n# v2\n",
        editor={
            "kind": "chat",
            "model": "m",
            "base_url": "https://gw.example/v1",
            "api_key_env": "EVO_TEST_ABSENT_KEY",
        },
        name="manifest-unbindable.json",
    )
    with pytest.raises(EvolveRefused) as unbound:
        await evolve(unbindable, tmp_path / "fresh", adapter_factory=target_down, environ={})
    assert unbound.value.code == "editor_env_missing"  # the manifest really cannot bind

    result = await evolve(unbindable, run_dir, adapter_factory=target_down, environ={})

    assert result.finished is not None
    assert result.finished["reason"] == "condition_changed"


async def test_an_owed_terminal_under_a_held_lock_is_journal_busy(tmp_path: Path) -> None:
    changed, run_dir = await closed_then_torn(tmp_path)
    with held_campaign_lock(run_dir):
        with pytest.raises(EvolveRefused) as refused:
            await evolve(changed, run_dir, adapter_factory=target_down)
    assert refused.value.code == "journal_busy"
    assert json.loads(_journal_lines(run_dir)[-1])["event"] == "authorization_applied"


async def test_an_owed_terminal_is_recovered_through_an_unjournaled_run_dir_link(
    tmp_path: Path,
) -> None:
    """Recovery through a supplied, unjournaled link needs no journaled hygiene.

    The CLI resupplies the decision directory on every invocation. Here the
    close was made through the run directory itself, so the link's decision was
    never journaled (Task 9's controller ruling 3 journals hygiene only when it
    is accepted in the closing invocation). Recovery through the link admits it
    as supplied, is state-first and leaves that decision unconsumed.
    """

    changed, run_dir, refused = await changed_scorer_refusal(tmp_path)
    assert refused.instance_digest is not None
    link = tmp_path / "link"
    link.symlink_to(run_dir, target_is_directory=True)
    finding = canonical_run_dir_finding(link)
    assert finding is not None
    hygiene = Authorization.new(
        "run_dir_unsafe_entry", finding.instance_digest, "link", now="2026-09-18T10:00:00Z"
    )
    close = Authorization.new(
        "run_identity_changed", refused.instance_digest, "x", now="2026-09-18T10:00:00Z"
    )
    await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[close])
    assert evolve_module.journaled_hygiene_digests(run_dir) == frozenset()
    drop_last_record(run_dir)
    notes: list[str] = []

    result = await evolve(
        changed,
        link,
        adapter_factory=target_down,
        authorizations=[hygiene, close],
        notes=notes.append,
    )

    assert result.finished is not None
    assert result.finished["reason"] == "condition_changed"
    assert json.loads(_journal_lines(run_dir)[-1])["event"] == "campaign_finished"
    assert f"authorization not needed: run_dir_unsafe_entry {finding.instance_digest}" in notes


async def test_a_storage_fault_on_the_owed_terminal_is_a_result_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed, run_dir = await closed_then_torn(tmp_path)
    before = _journal_lines(run_dir)

    def full(*_args: Any, **_kwargs: Any) -> Any:
        raise JournalFault("storage_capacity_exhausted")

    monkeypatch.setattr(AppendLog, "append", full)
    result = await evolve(changed, run_dir, adapter_factory=target_down)

    assert (result.terminal, result.owner, result.code) == (
        "storage_capacity_exhausted",
        "campaign_journal",
        "storage_capacity_exhausted",
    )
    assert result.status is not None
    assert _journal_lines(run_dir) == before


async def test_a_corrupt_suffix_behind_an_owed_terminal_is_reported_without_the_target(
    tmp_path: Path,
) -> None:
    changed, run_dir = await closed_then_torn(tmp_path)
    with (run_dir / CAMPAIGN_FILE).open("ab") as journal:
        journal.write(b"{not a record}\n")

    result = await evolve(changed, run_dir, adapter_factory=target_down)

    assert (result.terminal, result.owner) == ("persistence_failed", "campaign_journal")
    assert result.status is not None


def test_the_cli_prints_the_close_sentence_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory_adapters(monkeypatch)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    asyncio.run(evolve(manifest, run_dir, adapter_factory=fake_adapters()))
    crash_before_finish(run_dir)
    changed = write_workspace(
        workspace, scorer_script=SCORER_SCRIPT + "\n# v2\n", name="manifest-s2.json"
    )

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 2
    lines = invocation.stderr.splitlines()
    assert "evo tree evolve: class: authorizable" in lines
    assert "evo tree evolve: accepting closes this campaign as stopped(condition_changed);" in lines
    assert "evo tree evolve: continue it with a linked run that declares the new condition" in lines
    # The campaign ends, so the lead line does not say it continues.
    assert lines[-2] == "evo tree evolve: to accept this difference, run:"
    assert lines[-1].startswith("  evo tree authorize ")


def test_no_comparable_change_on_an_unfinished_campaign_is_hard_for_an_owed_terminal() -> None:
    """The `probe_unavailable` dead end is gone.

    An unavailable probe check's terminal is written by the `terminal` effect,
    so no hard sentence for an owed terminal remains to print.
    """

    from evolution.tree.execution import authorizations

    assert "run_identity_changed_terminal_owed" not in authorizations.HARD_REASONS
    assert not hasattr(authorizations, "TERMINAL_OWED_DETAIL")


@pytest.mark.parametrize(("effect", "lead"), [("rebase", " and continue"), ("close", "")])
def test_a_collected_refusal_ending_the_campaign_does_not_promise_it_continues(
    capsys: pytest.CaptureFixture[str], effect: str, lead: str
) -> None:
    """Final review: the multi-finding lead line follows the single one (Ruling 5)."""

    from evolution.tree import cli as cli_module
    from evolution.tree.execution.authorizations import RefusalDifference
    from evolution.tree.execution.output_admission import RefusalFinding

    refused = EvolveRefused.from_findings(
        (
            RefusalFinding("run_dir_unsafe_entry", "scorer-traces", "", (), "a" * 64, "hygiene"),
            RefusalFinding(
                "run_identity_changed",
                None,
                "",
                (RefusalDifference("identity.scorer.digest", "a" * 8, "b" * 8),),
                "b" * 64,
                effect,  # type: ignore[arg-type]
            ),
        )
    )
    cli_module._print_refusal(refused, Path("run"), ())
    lines = capsys.readouterr().err.splitlines()
    assert lines[-2] == f"evo tree evolve: to accept these findings{lead}, run:"
    assert lines[-1].startswith("  evo tree authorize run --accept ")


def test_the_continue_line_of_a_closed_campaign_names_the_new_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory_adapters(monkeypatch)
    changed, run_dir, refused = asyncio.run(changed_scorer_refusal(tmp_path))
    assert refused.instance_digest is not None
    authorized = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "run_identity_changed",
            "--digest",
            refused.instance_digest,
            "--reason",
            "scorer v2",
        ],
    )
    assert authorized.exit_code == 0, authorized.output

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 3, invocation.output
    [line] = [
        text
        for text in invocation.stderr.splitlines()
        if text.startswith("evo tree evolve: continue: ")
    ]
    assert line.endswith("; declare the new condition in the successor's manifest")


def test_the_decision_directory_is_not_a_foreign_entry(tmp_path: Path) -> None:
    """A fresh run directory holding only the operator's own decisions is still new.

    `evo tree authorize` writes `<run_dir>/authorizations/` before the campaign
    exists -- which is exactly the shape a `run_dir_unsafe_entry` decision is
    written in -- and `run_dir_state` must not then call the run directory
    foreign. `read_decisions` is the guard on the directory's own safety.
    """

    run_dir = tmp_path / "run"
    write_decision(run_dir, Authorization.new("run_dir_unsafe_entry", "a" * 64, "shared box"))

    assert run_dir_state(run_dir) == "new"


async def test_a_symlinked_run_dir_is_named_run_dir_whatever_the_operator_called_it(
    tmp_path: Path,
) -> None:
    """PDR-0010: the finding's `path` is a journal identifier, not a file name.

    `run with space` is an ordinary directory name and no identifier at all.
    Deriving the finding's name from it made the refusal printable and the
    decision it published unjournalable: the retry wrote `campaign_created` and
    then failed with `invalid journal record` (codex round 4). The entry is
    named after its role instead -- there is exactly one run directory -- and
    the link target stays where it always was, inside the instance digest.
    """

    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    real = workspace / "real"
    real.mkdir()
    link = workspace / "run with space"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(manifest, link, adapter_factory=fake_adapters())

    assert str(refusal.value) == "run_dir_unsafe_entry at run_dir"
    digest = refusal.value.instance_digest
    assert digest == hygiene_instance_digest("run_dir", "symlinked", str(real))
    auth = Authorization.new(
        "run_dir_unsafe_entry", digest, "my own box", now="2026-09-17T10:00:00Z"
    )

    result = await evolve(manifest, link, adapter_factory=fake_adapters(), authorizations=[auth])

    assert result.terminal == "finished"
    applied = [
        json.loads(line)["payload"]
        for line in _journal_lines(real)
        if json.loads(line)["event"] == "authorization_applied"
    ]
    assert [entry["path"] for entry in applied] == ["run_dir"]
    assert [entry["instance_digest"] for entry in applied] == [digest]


def test_the_cli_exits_1_without_a_traceback_when_try_facts_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measurement-facts read that fails after the durable
    terminal is `persistence_failed`, exit 1, and the CLI prints no traceback."""

    import evolution.tree.campaign as campaign_module
    from evolution.tree.storage.journal_errors import JournalFault

    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"
    fake = fake_adapters()
    monkeypatch.setattr(
        evolve_module,
        "_http_session_adapter",
        lambda manifest, run_id, ledger=None: fake(manifest, run_id),
    )

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise JournalFault("journal_limit_exceeded")

    monkeypatch.setattr(campaign_module, "read_measurement_tries", unreadable)

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert invocation.exception is None or isinstance(invocation.exception, SystemExit)
    assert invocation.exit_code == 1, invocation.output
    assert "Traceback" not in invocation.output
    assert "persistence_failed" in invocation.output


# -- linked runs (continuable terminals §5.4, §6.3, §8) -----------------------


def champion_path_of(result: CampaignResult) -> str:
    """The finished campaign's champion copy, relative to its run directory (§8)."""

    outcome = result.status.to_payload()["campaign_outcome"]
    assert outcome is not None and outcome["champion_path"] is not None
    return str(outcome["champion_path"])


async def finished_predecessor(tmp_path: Path, *, schema: str = SCHEMA_V15) -> tuple[Path, str]:
    """`tmp_path/run-1`, a finished campaign, and its champion copy's relative path."""

    first = write_workspace(tmp_path / "a", schema=schema)
    result = await evolve(first, tmp_path / "run-1", adapter_factory=fake_adapters())
    assert result.finished is not None
    return tmp_path / "run-1", champion_path_of(result)


def successor_workspace(tmp_path: Path, champion: str, **kwargs: Any) -> Path:
    """Workspace `b`, linked from `../run-1` and seeded from its champion copy."""

    return write_workspace(
        tmp_path / "b",
        linked_from="../run-1",
        seed_root=f"../run-1/{champion}",
        **kwargs,
    )


def creation_identity(run_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_journal_lines(run_dir)[0])["payload"]["identity"])


async def test_linked_from_runs_a_successor(tmp_path: Path) -> None:
    """§6.3: a new run directory resolves `linked_from` against the manifest's
    directory and hands it to `run_campaign`; the successor journals the link."""

    predecessor, champion = await finished_predecessor(tmp_path)
    second = successor_workspace(tmp_path, champion)

    result = await evolve(second, tmp_path / "run-2", adapter_factory=fake_adapters())

    assert result.terminal == "finished"
    assert result.finished is not None
    linked = creation_identity(tmp_path / "run-2")["linked_from"]
    assert linked["first_round"] == 2
    assert linked["run_id"] == derive_run_id(predecessor)
    status = result.status.to_payload()
    assert status["linked_from"]["run_id"] == derive_run_id(predecessor)
    assert status["linked_from"]["first_round"] == 2


async def test_the_symlink_check_does_not_refuse_a_linked_from_under_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    """Only the named entry itself is judged a symlink; its ancestors are canonicalized."""

    _predecessor, champion = await finished_predecessor(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    manifest = write_workspace(
        tmp_path / "b", linked_from="../alias/run-1", seed_root=f"../run-1/{champion}"
    )

    result = await evolve(manifest, tmp_path / "run-2", adapter_factory=fake_adapters())

    assert result.finished is not None


async def test_resume_never_reads_the_linked_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.3: the predecessor is not read again on resume -- not even derived."""

    import evolution.tree.campaign as campaign_module

    _predecessor, champion = await finished_predecessor(tmp_path)
    second = successor_workspace(tmp_path, champion)
    run_dir = tmp_path / "run-2"
    await evolve(second, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the predecessor was read on resume")

    monkeypatch.setattr(campaign_module, "derive_linked_from", forbidden)
    resumed = await evolve(second, run_dir, adapter_factory=fake_adapters())
    assert resumed.finished is not None


async def test_an_unfinished_predecessor_is_lineage_not_finished_at_linked_from(
    tmp_path: Path,
) -> None:
    predecessor, champion = await finished_predecessor(tmp_path)
    crash_before_finish(predecessor)

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(
            successor_workspace(tmp_path, champion),
            tmp_path / "run-2",
            adapter_factory=fake_adapters(),
        )

    assert (refusal.value.code, refusal.value.path) == ("lineage_not_finished", "linked_from")
    assert not (tmp_path / "run-2").exists()


def test_the_cli_prints_the_link_note_in_the_refusal_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory_adapters(monkeypatch)
    _predecessor, champion = asyncio.run(finished_predecessor(tmp_path))
    changed = successor_workspace(tmp_path, champion, scorer_script=SCORER_SCRIPT + "\n# v2\n")

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(tmp_path / "run-2")]
    )

    assert invocation.exit_code == 0, invocation.output
    lines = invocation.stderr.splitlines()
    assert "evo tree evolve: note: condition changed across the link" in lines
    assert any(
        line.startswith("evo tree evolve: differs: identity.scorer.digest") for line in lines
    )
    assert not any("note: differs:" in line for line in lines)


def inheriting_workspace(tmp_path: Path, champion: str, **kwargs: Any) -> Path:
    """Workspace `b`, a `/6` successor of `../run-1` declaring `baseline: inherit`."""

    return write_workspace(
        tmp_path / "b",
        linked_from={"run_dir": "../run-1", "baseline": "inherit"},
        seed_root=f"../run-1/{champion}",
        **kwargs,
    )


async def test_an_inheriting_link_across_another_seed_tree_says_so(tmp_path: Path) -> None:
    """§4.3: a changed seed tree is noted beside the carried counts."""

    _predecessor, champion = await finished_predecessor(tmp_path)
    other_seed = tmp_path / "other-seed"
    shutil.copytree(tmp_path / "run-1" / champion, other_seed)
    other_seed.chmod(0o700)
    (other_seed / "notes.txt").write_text("another tree\n", encoding="utf-8")
    said: list[str] = []

    await evolve(
        write_workspace(
            tmp_path / "b",
            linked_from={"run_dir": "../run-1", "baseline": "inherit"},
            seed_root="../other-seed",
        ),
        tmp_path / "run-2",
        adapter_factory=fake_adapters(),
        notes=said.append,
    )

    changed = creation_identity(tmp_path / "run-2")["linked_from"]["condition_changed"]
    assert any(field.startswith("seed_root.") for field in changed)
    assert said[-1] == "carried rows were measured on another seed tree"
    assert said[-2].startswith("baseline inherited: ")


async def test_baseline_source_unavailable_is_hard_at_linked_from(tmp_path: Path) -> None:
    """§5.0: an unreadable source refuses before any write, with no instance
    digest; the same link declared `fresh` runs."""

    predecessor, champion = await finished_predecessor(tmp_path)
    shutil.rmtree(predecessor / "measurements")

    with pytest.raises(EvolveRefused) as refusal:
        await evolve(
            inheriting_workspace(tmp_path, champion),
            tmp_path / "run-2",
            adapter_factory=fake_adapters(),
        )

    assert (refusal.value.code, refusal.value.path) == (
        "baseline_source_unavailable",
        "linked_from",
    )
    assert refusal.value.instance_digest is None
    assert str(tmp_path) not in str(refusal.value)
    assert not (tmp_path / "run-2" / CAMPAIGN_FILE).exists()

    fresh = write_workspace(
        tmp_path / "b",
        linked_from={"run_dir": "../run-1", "baseline": "fresh"},
        seed_root=f"../run-1/{champion}",
        name="manifest-fresh.json",
    )
    result = await evolve(fresh, tmp_path / "run-2", adapter_factory=fake_adapters())
    assert result.terminal == "finished"


def test_the_cli_prints_the_inherited_counts_as_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory_adapters(monkeypatch)
    _predecessor, champion = asyncio.run(finished_predecessor(tmp_path))

    invocation = CliRunner().invoke(
        tree_app,
        [
            "evolve",
            "--manifest",
            str(inheriting_workspace(tmp_path, champion)),
            "--run-dir",
            str(tmp_path / "run-2"),
        ],
    )

    assert invocation.exit_code == 0, invocation.output
    baseline = creation_identity(tmp_path / "run-2")["linked_from"]["baseline"]
    assert (
        "evo tree evolve: note: baseline inherited: "
        f"{baseline['imported']} pairs carried, "
        f"{baseline['scheduled'] - baseline['imported']} to measure"
    ) in invocation.stderr.splitlines()


def test_the_cli_prints_baseline_source_unavailable_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory_adapters(monkeypatch)
    predecessor, champion = asyncio.run(finished_predecessor(tmp_path))
    shutil.rmtree(predecessor / "measurements")

    invocation = CliRunner().invoke(
        tree_app,
        [
            "evolve",
            "--manifest",
            str(inheriting_workspace(tmp_path, champion)),
            "--run-dir",
            str(tmp_path / "run-2"),
        ],
    )

    assert invocation.exit_code == 2, invocation.output
    assert "baseline_source_unavailable at linked_from" in invocation.stderr
    assert "class: hard" in invocation.stderr
    assert '"baseline": "fresh" in linked_from' in invocation.stderr


class _PowerCut(RuntimeError):
    """A simulated crash between the durable declaration and the baseline's birth."""


def _crash_before_the_birth(monkeypatch: pytest.MonkeyPatch, manifest: Path, run_dir: Path) -> None:
    """Leave `run_dir` with its first-round baseline bound but unborn: the copy owed."""

    def cut(_self: Any, _round: int) -> Any:
        raise _PowerCut("power cut")

    import evolution.tree.campaign as campaign_module

    with monkeypatch.context() as patch:
        patch.setattr(campaign_module._Campaign, "birth_import", cut)
        with pytest.raises(_PowerCut):
            asyncio.run(evolve(manifest, run_dir, adapter_factory=fake_adapters()))
    assert not (run_dir / "measurements" / "m-r00-champion" / "events.jsonl").exists()


def _journal_records(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / CAMPAIGN_FILE).read_text(encoding="utf-8").splitlines()
    ]


def test_a_resume_completes_the_owed_copy_from_the_manifests_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline inheritance §5.2: the resume re-reads `linked_from.run_dir`."""

    in_memory_adapters(monkeypatch)
    _predecessor, champion = asyncio.run(finished_predecessor(tmp_path))
    manifest = inheriting_workspace(tmp_path, champion)
    run_dir = tmp_path / "run-2"
    _crash_before_the_birth(monkeypatch, manifest, run_dir)

    result = asyncio.run(evolve(manifest, run_dir, adapter_factory=fake_adapters()))

    assert result.terminal == "finished"
    baseline = creation_identity(run_dir)["linked_from"]["baseline"]
    born = (run_dir / "measurements" / "m-r00-champion" / "events.jsonl").read_text()
    assert born.count('"sample_imported"') == baseline["imported"] > 0


def test_an_owed_copy_and_unaccepted_hygiene_are_one_refusal_and_one_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0015 (codex branch review round 2, ruling R7 reversed): with an owed
    copy whose predecessor is lost and an unaccepted `run_dir_unsafe_entry`,
    one refusal prints both findings and one `--accept` command admits them."""

    in_memory_adapters(monkeypatch)
    predecessor, champion = asyncio.run(finished_predecessor(tmp_path))
    manifest = inheriting_workspace(tmp_path, champion)
    run_dir = tmp_path / "run-2"
    _crash_before_the_birth(monkeypatch, manifest, run_dir)
    shutil.rmtree(predecessor / "measurements")
    _loosen(run_dir, "scorer-traces")
    before = (run_dir / CAMPAIGN_FILE).read_bytes()

    refused = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert refused.exit_code == 2, refused.output
    assert (run_dir / CAMPAIGN_FILE).read_bytes() == before
    lines = refused.stderr.splitlines()
    assert [line for line in lines if line.startswith("evo tree evolve: refused: ")] == [
        "evo tree evolve: refused: run_dir_unsafe_entry at scorer-traces",
        "evo tree evolve: refused: baseline_import_unavailable",
    ]
    assert lines.count("evo tree evolve: class: authorizable") == 2
    assert str(predecessor) not in refused.stderr
    commands = [line for line in lines if "evo tree authorize" in line]
    assert len(commands) == 1
    command = shlex.split(commands[0].strip())
    assert command[:4] == ["evo", "tree", "authorize", str(run_dir)]
    assert command.count("--accept") == 2

    authorized = CliRunner().invoke(
        tree_app, ["authorize", *command[3:-2], "--reason", "shared box, predecessor lost"]
    )
    assert authorized.exit_code == 0, authorized.output
    ran = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert ran.exit_code == 0, ran.output
    applied = [r for r in _journal_records(run_dir) if r["event"] == "authorization_applied"]
    assert sorted((r["payload"]["code"], r["payload"].get("effect")) for r in applied) == [
        ("baseline_import_unavailable", "fresh_baseline"),
        ("run_dir_unsafe_entry", None),
    ]
    born = (run_dir / "measurements" / "m-r00-champion" / "events.jsonl").read_text()
    assert '"sample_imported"' not in born
    listed = CliRunner().invoke(tree_app, ["authorize", str(run_dir), "--list"])
    assert [json.loads(line)["status"] for line in listed.stdout.splitlines()] == ["consumed"] * 2


# -- D2a §6: collected findings and `authorize --accept` ----------------------


def _loosen(run_dir: Path, *names: str) -> None:
    """Give each named output root group/other bits: an authorizable `not owner-only`."""

    for name in names:
        (run_dir / name).mkdir(exist_ok=True)
        (run_dir / name).chmod(0o755)


async def test_two_unsafe_entries_and_a_close_are_one_refusal(tmp_path: Path) -> None:
    changed, run_dir, _ = await changed_scorer_refusal(tmp_path)
    _loosen(run_dir, "scorer-traces", "editor-logs")
    before = _journal_lines(run_dir)

    with pytest.raises(EvolveRefused) as refused:
        await evolve(changed, run_dir, adapter_factory=fake_adapters())

    assert _journal_lines(run_dir) == before
    codes = [(f.code, f.path, f.effect) for f in refused.value.findings]
    assert codes == [
        ("run_dir_unsafe_entry", "scorer-traces", "hygiene"),
        ("run_dir_unsafe_entry", "editor-logs", "hygiene"),
        ("run_identity_changed", None, "close"),
    ]
    # The singular fields are the first finding's (§6).
    assert (refused.value.code, refused.value.path) == ("run_dir_unsafe_entry", "scorer-traces")
    decisions = [
        Authorization.new(f.code, str(f.instance_digest), "accept all", now="2026-09-18T10:00:00Z")
        for f in refused.value.findings
    ]

    closed = await evolve(
        changed, run_dir, adapter_factory=fake_adapters(), authorizations=decisions
    )

    assert closed.finished is not None
    assert closed.finished["reason"] == "condition_changed"
    after = [json.loads(line) for line in _journal_lines(run_dir)[len(before) :]]
    events = [
        (record["event"], record["payload"].get("effect"), record["payload"].get("path"))
        for record in after
        if record["event"] != "alpha_consumed"  # an owed alpha row may lead (existing rule)
    ]
    # Controller ruling 3: the accepted hygiene is journaled before the close record.
    assert events == [
        ("authorization_applied", None, "scorer-traces"),
        ("authorization_applied", None, "editor-logs"),
        ("authorization_applied", "close", None),
        ("campaign_finished", None, None),
    ]
    assert all(
        "effect" not in record["payload"]
        for record in after[:2]
        if record["event"] != "alpha_consumed"
    )
    assert evolve_module.journaled_authorization_ids(run_dir) >= {d.id for d in decisions}


async def test_a_matched_close_with_unaccepted_hygiene_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    changed, run_dir, refused = await changed_scorer_refusal(tmp_path)
    assert refused.instance_digest is not None
    _loosen(run_dir, "scorer-traces")
    close = Authorization.new(
        "run_identity_changed", refused.instance_digest, "x", now="2026-09-18T10:00:00Z"
    )
    before = _journal_lines(run_dir)

    with pytest.raises(EvolveRefused) as again:
        await evolve(changed, run_dir, adapter_factory=fake_adapters(), authorizations=[close])

    assert [(f.code, f.path) for f in again.value.findings] == [
        ("run_dir_unsafe_entry", "scorer-traces")
    ]
    assert _journal_lines(run_dir) == before


async def test_unaccepted_hygiene_on_an_unchanged_resume_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    await evolve(manifest, run_dir, adapter_factory=fake_adapters())
    crash_before_finish(run_dir)
    _loosen(run_dir, "scorer-traces", "reflector-logs")
    before = _journal_lines(run_dir)

    with pytest.raises(EvolveRefused) as refused:
        await evolve(manifest, run_dir, adapter_factory=fake_adapters())

    assert [(f.code, f.path) for f in refused.value.findings] == [
        ("run_dir_unsafe_entry", "scorer-traces"),
        ("run_dir_unsafe_entry", "reflector-logs"),
    ]
    assert _journal_lines(run_dir) == before


async def test_a_hard_finding_after_collected_ones_prints_first(tmp_path: Path) -> None:
    _changed, run_dir, _ = await changed_scorer_refusal(tmp_path)
    _loosen(run_dir, "scorer-traces")
    # A hard check that runs after hygiene: a binding that cannot be built.
    # (`undeclared_sibling` would not do: the close skips sibling checks, §6.)
    unbindable = write_workspace(
        tmp_path / "ws",
        scorer_script=SCORER_SCRIPT + "\n# v2\n",
        editor={
            "kind": "chat",
            "model": "m",
            "base_url": "https://gw.example/v1",
            "api_key_env": "EVO_TEST_ABSENT_KEY",
        },
        name="manifest-unbindable.json",
    )

    with pytest.raises(EvolveRefused) as refused:
        await evolve(unbindable, run_dir, adapter_factory=fake_adapters(), environ={})

    findings = refused.value.findings
    assert [(f.code, f.instance_digest is None) for f in findings] == [
        ("editor_env_missing", True),
        ("run_dir_unsafe_entry", False),
    ]
    assert refused.value.code == "editor_env_missing"


def test_the_cli_prints_every_finding_and_one_accept_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_memory_adapters(monkeypatch)
    changed, run_dir, _ = asyncio.run(changed_scorer_refusal(tmp_path))
    _loosen(run_dir, "scorer-traces", "editor-logs")

    refused = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )

    assert refused.exit_code == 2
    lines = refused.stderr.splitlines()
    assert [line for line in lines if line.startswith("evo tree evolve: refused: ")] == [
        "evo tree evolve: refused: run_dir_unsafe_entry at scorer-traces",
        "evo tree evolve: refused: run_dir_unsafe_entry at editor-logs",
        "evo tree evolve: refused: run_identity_changed",
    ]
    assert lines.count("evo tree evolve: class: authorizable") == 3
    # The identity finding is a close, so the lead line does not say "continue".
    assert "evo tree evolve: to accept these findings, run:" in lines
    command = shlex.split(lines[-1].strip())
    assert command[:4] == ["evo", "tree", "authorize", str(run_dir)]
    assert command.count("--accept") == 3

    authorized = CliRunner().invoke(
        tree_app, ["authorize", *command[3:-2], "--reason", "accept all"]
    )
    assert authorized.exit_code == 0, authorized.output
    assert len(authorized.stdout.splitlines()) == 3  # one id per decision
    ran = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert ran.exit_code == 3, ran.output
    listed = CliRunner().invoke(tree_app, ["authorize", str(run_dir), "--list"])
    assert [json.loads(line)["status"] for line in listed.stdout.splitlines()] == ["consumed"] * 3


def test_accept_default_effect_admits_a_non_comparable_change_with_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1, finding 1 / ruling R4.

    The documented combined `--accept` pattern (docs/cli.md:384) must admit a
    *non-comparable* `run_identity_changed` instance -- an editor digest bump,
    D2a's ordinary "rebase" resume -- exactly as it does a comparable one: the
    decision the operator writes without `--effect` carries `effect:
    "default"`, and `identity_rebased`'s `authorization` slot
    (`journal_payload()`) must accept that without a `SchemaError`.
    """

    in_memory_adapters(monkeypatch)
    workspace = tmp_path / "ws"
    manifest = write_workspace(workspace)
    run_dir = workspace / "run"
    asyncio.run(evolve(manifest, run_dir, adapter_factory=fake_adapters()))
    crash_before_finish(run_dir)
    changed = write_workspace(
        workspace, editor_script=EDITOR_SCRIPT + "\n# v2\n", name="manifest-v2.json"
    )
    _loosen(run_dir, "scorer-traces")

    with pytest.raises(EvolveRefused) as refused:
        asyncio.run(evolve(changed, run_dir, adapter_factory=fake_adapters()))
    assert [(f.code, f.effect) for f in refused.value.findings] == [
        ("run_dir_unsafe_entry", "hygiene"),
        ("run_identity_changed", "rebase"),
    ]

    refusal = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert refusal.exit_code == 2, refusal.output
    lines = refusal.stderr.splitlines()
    command = shlex.split(lines[-1].strip())
    assert command[:4] == ["evo", "tree", "authorize", str(run_dir)]
    assert command.count("--accept") == 2
    assert "--effect" not in command  # a rebase offers only one effect; no choice to print

    authorized = CliRunner().invoke(
        tree_app, ["authorize", *command[3:-2], "--reason", "prompt v2"]
    )
    assert authorized.exit_code == 0, authorized.output
    (rebase_decision,) = [d for d in read_decisions(run_dir) if d.code == "run_identity_changed"]
    assert rebase_decision.effect == "default"

    continued = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert continued.exit_code == 0, continued.output
    events = [json.loads(line)["event"] for line in _journal_lines(run_dir)]
    assert "identity_rebased" in events


def test_accept_default_effect_admits_a_comparable_close_with_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1, finding 1 / ruling R4.

    A comparable `run_identity_changed` instance, collected on one `--accept`
    line together with hygiene findings and so carrying `effect: "default"`
    (not `None` -- `--effect` combines only with a lone `run_identity_changed`
    item, see `test_accept_explicit_effect_admits_a_comparable_close` below),
    still journals `authorization_applied` successfully: `decision_payload()`
    (not `journal_payload()`) carries that decision's `effect` into the
    record, and `_decision_names_the_instance` already tolerates it.
    """

    in_memory_adapters(monkeypatch)
    changed, run_dir, _ = asyncio.run(changed_scorer_refusal(tmp_path))
    _loosen(run_dir, "scorer-traces", "editor-logs")

    refused = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert refused.exit_code == 2, refused.output
    command = shlex.split(refused.stderr.splitlines()[-1].strip())
    assert command[:4] == ["evo", "tree", "authorize", str(run_dir)]
    assert command.count("--accept") == 3

    authorized = CliRunner().invoke(
        tree_app, ["authorize", *command[3:-2], "--reason", "accept all"]
    )
    assert authorized.exit_code == 0, authorized.output
    (close_decision,) = [d for d in read_decisions(run_dir) if d.code == "run_identity_changed"]
    assert close_decision.effect == "default"

    ran = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert ran.exit_code == 3, ran.output
    records = [json.loads(line) for line in _journal_lines(run_dir)]
    (identity_record,) = [
        r
        for r in records
        if r["event"] == "authorization_applied"
        and r["payload"].get("code") == "run_identity_changed"
    ]
    assert identity_record["payload"]["effect"] == "close"
    assert identity_record["payload"]["authorization"]["effect"] == "default"
    assert identity_record["payload"]["authorization"]["id"] == close_decision.id


def test_accept_explicit_effect_admits_a_comparable_close_mixed_with_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `--accept` line collects hygiene findings *and* a comparable
    `run_identity_changed` finding, with an explicit `--effect close`: the
    effect attaches only to the `run_identity_changed` item (the hygiene
    items are written without one, exactly as before), and `evolve` admits
    all of them in one call -- the combined command Task 4's refusal printer
    is expected to print for a mixed refusal must be acceptable as written.
    """

    in_memory_adapters(monkeypatch)
    changed, run_dir, _ = asyncio.run(changed_scorer_refusal(tmp_path))
    _loosen(run_dir, "scorer-traces", "editor-logs")

    refused = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert refused.exit_code == 2, refused.output
    command = shlex.split(refused.stderr.splitlines()[-1].strip())
    assert command[:4] == ["evo", "tree", "authorize", str(run_dir)]
    assert command.count("--accept") == 3

    authorized = CliRunner().invoke(
        tree_app, ["authorize", *command[3:-2], "--effect", "close", "--reason", "accept all"]
    )
    assert authorized.exit_code == 0, authorized.output
    decisions = {d.code: d for d in read_decisions(run_dir)}
    assert decisions["run_identity_changed"].effect == "close"
    assert all(
        decision.effect is None
        for code, decision in decisions.items()
        if code != "run_identity_changed"
    )

    ran = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert ran.exit_code == 3, ran.output
    records = [json.loads(line) for line in _journal_lines(run_dir)]
    applied = [r for r in records if r["event"] == "authorization_applied"]
    assert len(applied) == 3  # two hygiene, one identity -- all admitted in one call
    hygiene = [r for r in applied if r["payload"].get("code") == "run_dir_unsafe_entry"]
    assert len(hygiene) == 2
    assert all("effect" not in r["payload"]["authorization"] for r in hygiene)
    (identity_record,) = [r for r in applied if r["payload"].get("code") == "run_identity_changed"]
    assert identity_record["payload"]["effect"] == "close"
    assert identity_record["payload"]["authorization"]["effect"] == "close"
    assert identity_record["payload"]["authorization"]["id"] == decisions["run_identity_changed"].id


def test_accept_effect_refused_when_no_run_identity_changed_item_is_on_the_line(
    tmp_path: Path,
) -> None:
    """Review round 1, ruling R6: `--effect` needs a `run_identity_changed` item to attach to.

    The refusal is on the code list alone, before any item is otherwise
    validated or written, so two arbitrary (but well-formed) `CODE:DIGEST`
    pairs of a code that is not `run_identity_changed` are enough to prove it
    -- no real campaign or real digest is needed.
    """

    run_dir = tmp_path / "run"
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "--accept",
            f"run_dir_unsafe_entry:{'a' * 64}",
            "--accept",
            f"run_dir_unsafe_entry:{'b' * 64}",
            "--effect",
            "close",
            "--reason",
            "x",
        ],
    )
    assert invocation.exit_code == 2
    assert (
        invocation.stderr.strip()
        == "evo tree authorize: refused: authorization_invalid at --effect"
    )
    assert not run_dir.exists()


def test_list_with_effect_is_refused_at_effect(tmp_path: Path) -> None:
    """`--list` reports; it accepts nothing, so `--effect` has no item to attach to.

    Codex review round 1: `--list --effect X` used to return before `--effect`
    was validated at all, silently ignoring it. It must refuse exactly like
    any other invalid `--effect` use, not like `--list` paired with a code.
    """

    run_dir = tmp_path / "run"
    invocation = CliRunner().invoke(
        tree_app,
        ["authorize", str(run_dir), "--list", "--effect", "close"],
    )
    assert invocation.exit_code == 2
    assert (
        invocation.stderr.strip()
        == "evo tree authorize: refused: authorization_invalid at --effect"
    )
    assert not run_dir.exists()


def test_accept_explicit_effect_admits_a_comparable_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1, findings 1 and 2 / ruling R4.

    A *lone* comparable `run_identity_changed` finding prints the positional
    hint (`hint_command`), which now accepts `--effect` too (finding 2): the
    operator's explicit `--effect close` reaches the journaled
    `authorization_applied` record through `decision_payload()`.
    """

    in_memory_adapters(monkeypatch)
    changed, run_dir, refused = asyncio.run(changed_scorer_refusal(tmp_path))
    digest = refused.instance_digest

    refusal = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert refusal.exit_code == 2, refusal.output
    # A single finding prints the positional hint, not `--accept` (§4, §6).
    assert refusal.stderr.splitlines()[-1].strip() == (
        f'evo tree authorize {run_dir} run_identity_changed --digest {digest} --reason "<why>"'
    )

    authorized = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "run_identity_changed",
            "--digest",
            digest,
            "--reason",
            "accept",
            "--effect",
            "close",
        ],
    )
    assert authorized.exit_code == 0, authorized.output
    (close_decision,) = read_decisions(run_dir)
    assert close_decision.effect == "close"

    ran = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(changed), "--run-dir", str(run_dir)]
    )
    assert ran.exit_code == 3, ran.output
    records = [json.loads(line) for line in _journal_lines(run_dir)]
    (identity_record,) = [r for r in records if r["event"] == "authorization_applied"]
    assert identity_record["payload"]["effect"] == "close"
    assert identity_record["payload"]["authorization"]["effect"] == "close"
    assert identity_record["payload"]["authorization"]["id"] == close_decision.id


def test_authorize_accept_refuses_a_malformed_pair_before_writing(tmp_path: Path) -> None:
    invocation = CliRunner().invoke(
        tree_app, ["authorize", str(tmp_path / "r"), "--accept", "nocolon", "--reason", "x"]
    )
    assert invocation.exit_code == 2
    assert "refused: authorization_invalid at --accept" in invocation.stderr
    assert not (tmp_path / "r").exists()


def test_authorize_refuses_accept_beside_a_code(tmp_path: Path) -> None:
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(tmp_path / "r"),
            "run_identity_changed",
            "--digest",
            "a" * 64,
            "--accept",
            "run_identity_changed:" + "a" * 64,
            "--reason",
            "x",
        ],
    )
    assert invocation.exit_code == 2
    assert "refused: authorization_invalid at --accept" in invocation.stderr
    assert not (tmp_path / "r").exists()


def test_authorize_accept_refuses_a_hard_code_before_any_write(tmp_path: Path) -> None:
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(tmp_path / "r"),
            "--accept",
            "run_identity_changed:" + "a" * 64,
            "--accept",
            "journal_busy:" + "b" * 64,
            "--reason",
            "x",
        ],
    )
    assert invocation.exit_code == 2
    assert "refused: authorization_invalid" in invocation.stderr
    assert not (tmp_path / "r").exists()


async def test_an_unaccepted_run_dir_link_on_an_owed_terminal_is_refused_before_any_write(
    tmp_path: Path,
) -> None:
    """§5.3 and §6: recovery runs only once the link is admitted; otherwise it is refused."""

    changed, run_dir = await closed_then_torn(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(run_dir, target_is_directory=True)
    finding = canonical_run_dir_finding(link)
    assert finding is not None
    before = _journal_lines(run_dir)

    with pytest.raises(EvolveRefused) as refused:
        await evolve(changed, link, adapter_factory=fake_adapters())

    # The link alone: accepting it is all recovery needs, and the identity
    # change is one the journal already decided.
    assert [(f.code, f.instance_digest) for f in refused.value.findings] == [
        ("run_dir_unsafe_entry", finding.instance_digest)
    ]
    assert _journal_lines(run_dir) == before
    hygiene = Authorization.new(
        "run_dir_unsafe_entry", finding.instance_digest, "link", now="2026-09-18T10:00:00Z"
    )
    result = await evolve(changed, link, adapter_factory=target_down, authorizations=[hygiene])
    assert result.finished is not None
    assert result.finished["reason"] == "condition_changed"


# -- `/9` wiring (ADR-0021, scorer-diagnostics Task 3) ------------------------


class _StopAfterAdaptiveConfig(Exception):
    """Ends `evolve` once the bindings and adaptive config exist, before the target."""


# -- model grouping, bound and run (model error grouping spec §7, §10) --------

GROUPER_KEY = "grouper-sekret-key-value"


class _BlockProviderSdks:
    """The meta-path blocker of `test_tree_role_call`: a core install's view."""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("httpx", "openai"):
            raise ImportError(f"blocked {name}")
        return None


def test_grouping_client_without_the_provider_sdks_is_a_controlled_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evolution.tree.roles.editor.chat_editor import ChatCredentials
    from evolution.tree.roles.grouping.model_grouping_runtime import new_grouping_client
    from evolution.tree.roles.role_failures import GroupingExecutionError

    for name in ("openai", "httpx"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockProviderSdks(), *sys.meta_path])

    with pytest.raises(GroupingExecutionError) as failure:
        new_grouping_client(cast(Any, SimpleNamespace()), ChatCredentials("k", None))

    assert failure.value.code == "provider_sdk_missing"


# -- #180 wiring: the chat Reflector's bounds repair ---------------------------


class _StopAfterReflector(Exception):
    """Ends `evolve` once `build_reflector` has been called."""


# -- #287: the manifest path of every run secret -------------------------------


def _command(*names: str) -> Any:
    from evolution.tree.execution.config import CommandSection

    return CommandSection(("prog",), Path("."), (), frozenset(names), None)


def _replace(section: Any, **changes: Any) -> Any:
    from dataclasses import replace

    return replace(section, **changes)


def _chat(key: str | None, proxy: bool = False) -> Any:
    from evolution.tree.roles.config import ChatEditorSection

    return ChatEditorSection(
        model="m",
        base_url="https://llm.example.test/v1",
        api_key_env=key,
        proxy_env="ROLE_PROXY" if proxy else None,
    )


def _agent(*names: str, permission: str = "safe") -> Any:
    from evolution.tree.roles.config import AgentCliEditorSection

    return AgentCliEditorSection(
        model="codex",
        permission=permission,  # type: ignore[arg-type]
        allowed_env=frozenset(names),
    )


def _command_grouper(*names: str) -> Any:
    from evolution.tree.roles.config import CommandGroupingSection

    return CommandGroupingSection(_command(*names), {})


def _agent_grouper(*names: str) -> Any:
    from evolution.tree.roles.config import AgentCliGrouperSection

    return AgentCliGrouperSection(_agent(*names), 60)


def _chat_grouper() -> Any:
    from dataclasses import replace

    from tests.campaign_fakes import model_grouping_section

    return replace(model_grouping_section(), api_key_env="GROUPER_KEY", proxy_env="ROLE_PROXY")


def _workflow(editor: Any, reflector: Any, grouper: Any = None) -> Any:
    from evolution.tree.roles.config import ReflectorSection

    return SimpleNamespace(
        scorer=_command("B2E_LAB_JUDGE_MODEL", "SCORER_FLAG", "SCORER_ABSENT", "A_SCORER_TOKEN"),
        editor=editor,
        reflector=ReflectorSection(role=reflector, environment_docs=None, probe=None),
        grouper=grouper,
    )


_ENVIRON = {
    "B2E_LAB_JUDGE_MODEL": "deepseek/judge-model-x",
    "A_SCORER_TOKEN": "scorer-token-0001",
    "SCORER_FLAG": "1",
    "EDIT_KEY": "sk-edit-key-000001",
    "REFLECT_KEY": "sk-reflect-key-01",
    "GROUPER_KEY": "sk-grouper-key-01",
    "ROLE_PROXY": "https://proxy.example.test:8080",
    "EDIT_TOKEN": "edit-token-000001",
    "REFLECT_TOKEN": "reflect-token-001",
    "SHARED": "deepseek/judge-model-x",
    "OPENAI_API_KEY": "sk-openai-curated-1",
    "HOME": "/home/operator",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "UNRELATED": "unrelated-value-01",
}
_SCORER = [
    ("scorer-token-0001", "scorer.allowed_env.A_SCORER_TOKEN"),
    ("deepseek/judge-model-x", "scorer.allowed_env.B2E_LAB_JUDGE_MODEL"),
]


def _bound(role: str, *declared: str, unsafe: bool = False) -> list[tuple[str, str]]:
    """An `agent_cli` role's bound child environment outside the runtime names:
    a declared name under `<role>.allowed_env`, any other under the bare role."""

    names = set(_ENVIRON) if unsafe else {"OPENAI_API_KEY", "HOME", *declared}
    return [
        (_ENVIRON[name], f"{role}.allowed_env.{name}" if name in declared else role)
        for name in sorted(names - {"PATH"})
    ]


@pytest.mark.parametrize(
    ("workflow", "unfolded"),
    [
        pytest.param(
            _workflow(_command("EDIT_TOKEN", "SHARED"), _command("REFLECT_TOKEN", "ABSENT")),
            [
                *_SCORER,
                ("edit-token-000001", "editor.allowed_env.EDIT_TOKEN"),
                ("deepseek/judge-model-x", "editor.allowed_env.SHARED"),
                ("reflect-token-001", "reflector.allowed_env.REFLECT_TOKEN"),
            ],
            id="subprocess",
        ),
        pytest.param(
            _workflow(_chat("EDIT_KEY", proxy=True), _chat("REFLECT_KEY"), _chat_grouper()),
            [
                *_SCORER,
                ("sk-edit-key-000001", "editor.api_key_env"),
                ("https://proxy.example.test:8080", "editor.proxy_env"),
                ("sk-reflect-key-01", "reflector.api_key_env"),
                ("sk-grouper-key-01", "grouper.api_key_env"),
                ("https://proxy.example.test:8080", "grouper.proxy_env"),
            ],
            id="chat",
        ),
        pytest.param(
            _workflow(
                _chat(None, proxy=True), _chat(None), _replace(_chat_grouper(), api_key_env=None)
            ),
            [
                *_SCORER,
                ("https://proxy.example.test:8080", "editor.proxy_env"),
                ("https://proxy.example.test:8080", "grouper.proxy_env"),
            ],
            id="chat-unauthenticated",
        ),
        pytest.param(
            _workflow(_agent("EDIT_TOKEN"), _agent("REFLECT_TOKEN"), _command_grouper("UNRELATED")),
            [
                *_SCORER,
                *_bound("editor", "EDIT_TOKEN"),
                *_bound("reflector", "REFLECT_TOKEN"),
                ("unrelated-value-01", "grouper.allowed_env.UNRELATED"),
            ],
            id="agent-cli-and-command-grouper",
        ),
        pytest.param(
            _workflow(_agent(permission="unsafe"), _command(), _agent_grouper("UNRELATED")),
            [
                *_SCORER,
                *_bound("editor", unsafe=True),
                *_bound("grouper", "UNRELATED"),
            ],
            id="agent-cli-unsafe-and-agent-grouper",
        ),
    ],
)
def test_run_secret_paths_align_with_run_secrets(workflow: Any, unfolded: Any) -> None:
    """`_run_secret_paths` names `_run_secrets`'s every value, index for index:
    roles Scorer, Editor, Reflector, Grouper, names sorted within each, and the
    short-value floor and the first-occurrence fold applied to both alike."""

    folded: dict[str, str] = {}
    for value, path in unfolded:
        if len(value) >= 8:
            folded.setdefault(value, path)
    environ = dict(_ENVIRON)
    assert evolve_module._run_secrets(workflow, environ) == tuple(folded)
    assert evolve_module._run_secret_paths(workflow, environ) == tuple(folded.values())
    assert len(evolve_module._run_secret_names(workflow, environ)) == len(folded)


def test_run_secret_names_carry_each_declared_name_beside_its_path() -> None:
    """#287 branch review r1: a `chat` role's path does not spell its name, so
    the names travel beside the paths, index for index, for the ordering by
    sorted name within a role (ADR-0029 amendment 2026-10-21). A bare role's
    undeclared value has none."""

    from dataclasses import replace

    workflow = _workflow(
        replace(_chat("Z_AUTH"), proxy_env="A_PROXY"),
        _agent("REFLECT_TOKEN"),
        replace(_chat_grouper(), api_key_env="Z_GROUP_AUTH", proxy_env="A_GROUP_PROXY"),
    )
    environ = {
        **_ENVIRON,
        "Z_AUTH": "sk-z-auth-value-01",
        "A_PROXY": "https://a-proxy.example.test:8080",
        "Z_GROUP_AUTH": "sk-z-group-auth-1",
        "A_GROUP_PROXY": "https://a-group-proxy.example.test:8080",
    }
    paths = evolve_module._run_secret_paths(workflow, environ)
    names = evolve_module._run_secret_names(workflow, environ)
    assert list(zip(paths, names, strict=True)) == [
        ("scorer.allowed_env.A_SCORER_TOKEN", "A_SCORER_TOKEN"),
        ("scorer.allowed_env.B2E_LAB_JUDGE_MODEL", "B2E_LAB_JUDGE_MODEL"),
        ("editor.api_key_env", "Z_AUTH"),
        ("editor.proxy_env", "A_PROXY"),
        ("reflector", None),
        ("reflector", None),
        ("reflector.allowed_env.REFLECT_TOKEN", "REFLECT_TOKEN"),
        ("grouper.api_key_env", "Z_GROUP_AUTH"),
        ("grouper.proxy_env", "A_GROUP_PROXY"),
    ]


def test_unauthenticated_roles_names_each_chat_role_without_a_key_in_role_order() -> None:
    """#290 §4.1: the roles the pre-run note names, Editor, Reflector, Grouper,
    each with its declared `base_url`; a declared key and a non-chat role are
    not unauthenticated."""

    grouper = _replace(_chat_grouper(), api_key_env=None, base_url="https://group.example/v1")
    reflector = _replace(_chat(None), base_url="https://models.example/v1")
    workflow = _workflow(_chat(None), reflector, grouper)
    assert evolve_module.unauthenticated_roles(workflow) == (
        ("editor", "https://llm.example.test/v1"),
        ("reflector", "https://models.example/v1"),
        ("grouper", "https://group.example/v1"),
    )
    assert evolve_module.unauthenticated_roles(
        _workflow(_chat("EDIT_KEY"), reflector, _chat_grouper())
    ) == (("reflector", "https://models.example/v1"),)
    assert evolve_module.unauthenticated_roles(_workflow(_command(), _agent())) == ()


# -- #295: plain HTTP to a model endpoint on any host -------------------------------


def test_unencrypted_model_roles_names_each_non_loopback_http_chat_role_in_role_order() -> None:
    """#295: `(role, base_url, credential declared)` for a chat role on plain
    `http` to a host that is not loopback, Editor, Reflector, Grouper; `https`,
    loopback `http` and a non-chat role are not named."""

    editor = _replace(_chat("EDIT_KEY"), base_url="http://llm.internal:8000/v1")
    reflector = _replace(_chat(None), base_url="http://10.0.0.5:8080/v1")
    grouper = _replace(_chat_grouper(), base_url="http://[fd00::1]:8000/v1")
    assert evolve_module.unencrypted_model_roles(_workflow(editor, reflector, grouper)) == (
        ("editor", "http://llm.internal:8000/v1", True),
        ("reflector", "http://10.0.0.5:8080/v1", False),
        ("grouper", "http://[fd00::1]:8000/v1", True),
    )
    # Per-role isolation: only the `http` role is named.
    assert evolve_module.unencrypted_model_roles(
        _workflow(_chat("EDIT_KEY"), reflector, _chat_grouper())
    ) == (("reflector", "http://10.0.0.5:8080/v1", False),)
    loopback = [
        _replace(_chat("EDIT_KEY"), base_url=url)
        for url in ("http://127.0.0.1:8080/v1", "http://localhost:8080/v1", "http://[::1]:8080/v1")
    ]
    for section in loopback:
        assert evolve_module.unencrypted_model_roles(_workflow(section, _chat(None))) == ()
    assert evolve_module.unencrypted_model_roles(_workflow(_command(), _agent())) == ()


#: #295: computed before `_chat_base_url` admitted `http` to any host, over
#: `tests.tree.manifest.test_manifest_v15.chat_roles_document()` (an `https` document).
VALID_BEFORE_DIGESTS = {
    "editor": "1a165e709f4281cd26a16e0ae7a64284bd7e3ca9bf5e7c1f9a8f9323dfb6a761",
    "reflector": "677867f95482f6659198b0c659f6b3a0c145920bed96be9c41623655feb01fad",
    "grouper": "fd0979b1a99fb81c518a37e2c72d0190bd86454fcc0411702bfc00dac409a4aa",
    "policy": "917d531a4d50c241d9182c48d7d20345e15f4682ce9837139aa75b4ae580df9f",
}


def test_a_document_valid_before_keeps_its_identity_and_policy_digests(tmp_path) -> None:
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles.seals import FileSeal, chat_identity, chat_reflector_identity
    from tests.tree.manifest.test_manifest_v15 import chat_roles_document, load

    workflow = load(tmp_path, chat_roles_document()).workflow
    assert workflow is not None and workflow.grouper is not None
    seal = FileSeal(path=None, sha256="0" * 64, packaged_text="x")
    grouper = evolve_module._chat_grouper_identity(workflow.grouper)  # type: ignore[arg-type]
    assert {
        "editor": chat_identity(workflow.editor, seal).digest,  # type: ignore[arg-type]
        "reflector": chat_reflector_identity(workflow.reflector.role, seal).digest,  # type: ignore[arg-type]
        "grouper": hashlib.sha256(canonical_json(grouper)).hexdigest(),
        "policy": workflow.campaign.digest,
    } == VALID_BEFORE_DIGESTS
    assert evolve_module.unencrypted_model_roles(workflow) == ()


async def test_a_family14_run_against_a_non_loopback_http_model_endpoint_finishes(
    tmp_path, monkeypatch
) -> None:
    """#295: a chat Editor on `http://llm.internal:<port>/v1` -- the resolver
    patched to the fake's loopback socket -- runs a whole family-14 campaign."""

    import socket

    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    script = [
        fake_chat_server.tool_call("list_files"),
        fake_chat_server.tool_call(
            "record_plan",
            {
                "summary": "fix",
                "changes": [{"path": "skills/SKILL.md", "action": "update", "purpose": "fix"}],
            },
        ),
        fake_chat_server.tool_call(
            "write_file", {"path": "skills/SKILL.md", "content": "IMPROVED: seed skill edited\n"}
        ),
        fake_chat_server.tool_call("finish_edit", {"summary": "one file"}),
    ]
    loopback_url, server = fake_chat_server.start(script)
    port = server.server_address[1]
    base_url = f"http://llm.internal:{port}/v1"
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        # anyio passes the host ASCII-encoded, as bytes.
        internal = host in ("llm.internal", b"llm.internal")
        return real_getaddrinfo("127.0.0.1" if internal else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    try:
        monkeypatch.setenv("E1_KEY", "sekret-key-value")
        workspace = fixtures.write_workspace

        def write_workspace(*args, **kwargs):
            return workspace(*args, **{**kwargs, "editor": chat_budget_section(base_url)})

        monkeypatch.setattr(fixtures, "write_workspace", write_workspace)
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, campaign={"rounds": 1, "attempts_per_round": 1}
        )
    finally:
        server.shutdown()
        server.server_close()
    assert loopback_url != base_url
    assert snapshot.finished is not None
    assert [p["status"] for p in payloads(run, "editor_finished")] == ["edited"]
    hosts = {
        value for headers in server.script.headers for name, value in headers if name == "host"
    }
    assert hosts == {f"llm.internal:{port}"}


_SAME = "https://same-value.test:8080"


def _described(workflow: Any, environ: dict[str, str], description: Any) -> tuple[str, str]:
    """The probe's diagnostic path and the campaign's refusal detail for one
    description, both from the provenance `evolve` derives (#287)."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.target.connection_probe import description_secret

    secrets = evolve_module._run_secrets(workflow, environ)
    paths = evolve_module._run_secret_paths(workflow, environ)
    names = evolve_module._run_secret_names(workflow, environ)
    assert len(paths) == len(names) == len(secrets)
    found = description_secret(description, secrets, paths, names)
    assert found is not None
    campaign = SimpleNamespace(
        checks=ENFORCE_ALL,
        attested_ids=lambda: set(),
        secrets=secrets,
        secret_paths=paths,
        secret_names=names,
    )
    with pytest.raises(campaign_module._Fault) as caught:
        campaign_module._Campaign.refuse_description_secret(  # type: ignore[arg-type]
            campaign, "m-r01-a01", {"new": description}
        )
    return found[0].manifest_path, caught.value.detail


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_a_duplicate_value_is_named_by_its_first_name_sorted_within_the_role(role: str) -> None:
    """#287 codex branch review r2 (ADR-0029 amendment 2026-10-21): a `chat`
    role's key `Z_AUTH` and proxy `ROLE_PROXY` hold one value. The fold keeps
    one needle, and its provenance is the name sorted first within the role,
    `ROLE_PROXY`, not the first declared, `Z_AUTH`. `_run_secrets` is unchanged."""

    from dataclasses import replace

    chat_editor = _chat("Z_AUTH", proxy=True)
    chat_grouper = replace(_chat_grouper(), api_key_env="Z_AUTH", proxy_env="ROLE_PROXY")
    workflow = _workflow(
        chat_editor if role == "editor" else _command(),
        chat_editor if role == "reflector" else _command(),
        chat_grouper if role == "grouper" else None,
    )
    environ = {**_ENVIRON, "Z_AUTH": _SAME, "ROLE_PROXY": _SAME}
    assert evolve_module._run_secrets(workflow, environ) == (
        "scorer-token-0001",
        "deepseek/judge-model-x",
        _SAME,
    )
    paths = evolve_module._run_secret_paths(workflow, environ)
    names = evolve_module._run_secret_names(workflow, environ)
    assert list(zip(paths, names, strict=True))[2] == (f"{role}.proxy_env", "ROLE_PROXY")
    path, detail = _described(workflow, environ, {"proxy": _SAME})
    assert path == f"{role}.proxy_env"
    assert detail.startswith(
        f"instance_attested.description.proxy holds the value of {role}.proxy_env;"
    )
    assert _SAME not in detail


def test_a_cross_role_duplicate_value_is_named_by_the_scorer() -> None:
    """Role order dominates the sorted name: the same value under
    `scorer.allowed_env.Z_SCORER` and the editor's `A_KEY` is the Scorer's."""

    workflow = _workflow(_chat("A_KEY"), _command())
    workflow.scorer = _command("Z_SCORER")
    environ = {**_ENVIRON, "Z_SCORER": _SAME, "A_KEY": _SAME}
    assert evolve_module._run_secrets(workflow, environ) == (_SAME,)
    assert evolve_module._run_secret_paths(workflow, environ) == ("scorer.allowed_env.Z_SCORER",)
    assert evolve_module._run_secret_names(workflow, environ) == ("Z_SCORER",)
    path, detail = _described(workflow, environ, {"key": _SAME})
    assert path == "scorer.allowed_env.Z_SCORER"
    assert "holds the value of scorer.allowed_env.Z_SCORER;" in detail


def test_a_duplicate_value_prefers_a_declared_name_to_a_bare_role() -> None:
    """A bare-role path (an undeclared bound value) sorts after the role's
    named ones: the curated `OPENAI_API_KEY` sorts before the declared
    `Z_TOKEN` and holds the same value, and `Z_TOKEN` names it."""

    workflow = _workflow(_agent("Z_TOKEN"), _command())
    environ = {**_ENVIRON, "Z_TOKEN": _ENVIRON["OPENAI_API_KEY"]}
    secrets = evolve_module._run_secrets(workflow, environ)
    paths = evolve_module._run_secret_paths(workflow, environ)
    index = secrets.index(_ENVIRON["OPENAI_API_KEY"])
    assert paths[index] == "editor.allowed_env.Z_TOKEN"
    assert evolve_module._run_secret_names(workflow, environ)[index] == "Z_TOKEN"


@pytest.mark.parametrize("terminal", ["finished", "suspended"])
async def test_cli_projection_invalid_exits_1_without_traceback(tmp_path, monkeypatch, terminal):
    import importlib
    from contextlib import nullcontext

    from evolution.tree import campaign, family15_report
    from evolution.tree.storage.envelope import SchemaError
    from tests.test_tree_campaign_projections import invalid_projection_run

    run, _, adapter = await invalid_projection_run(tmp_path, monkeypatch, terminal)
    real_evolve = evolve_module.evolve
    original_result = campaign._Campaign.result
    calls = []

    async def through_real_evolve(*args, **kwargs):
        calls.append(True)
        kwargs["adapter_factory"] = lambda *a: adapter
        return await real_evolve(*args, **kwargs)

    def result(self):
        assert self.snapshot().finished is not None or self.suspended is not None

        def invalid(*args, **kwargs):
            raise SchemaError("invalid public projection")

        with monkeypatch.context() as patch:
            patch.setattr(family15_report, "validate_campaign15_projection", invalid)
            return original_result(self)

    monkeypatch.setattr(campaign._Campaign, "result", result)
    monkeypatch.setattr(evolve_module, "evolve", through_real_evolve)
    cli = importlib.import_module("evolution.tree.cli")
    monkeypatch.setattr(cli, "_stop_flag", lambda: nullcontext(lambda: False))
    invocation = await asyncio.to_thread(
        CliRunner().invoke,
        tree_app,
        ["evolve", "--manifest", str(tmp_path / "manifest.json"), "--run-dir", str(run)],
    )
    assert calls == [True]
    assert invocation.exit_code == 1, invocation.output
    assert json.loads(invocation.stdout)["code"] == "projection_invalid"
    lines = [line for line in invocation.stderr.splitlines() if ": projection_invalid:" in line]
    assert len(lines) == 1
    assert "Traceback" not in invocation.output
    if terminal == "suspended":
        assert "the run finished" not in invocation.stderr
        assert "suspended: " in invocation.stderr
        assert "continue: evo tree evolve" in invocation.stderr
