"""The `evo tree` surface with standalone evaluation and the retained campaign contracts.

These are the removal contracts of spec §8 and the CLI contract of spec §6. They
are deliberately structural — the registry, the option set, the banned tokens —
because what W1 removes must stay removed: a convenience flag or a re-imported
donor module is a regression no behavioural test would catch.

The exit-code cases drive the real Click command over a faked `evolve`: the map
from a campaign outcome to a process exit code is the CLI's own contract, and
running a campaign per case to observe it would cost minutes. The two cases that
cannot be faked — a refusal, and the SIGINT that C1 turns into `stopped` — run
the installed entry point as a subprocess, which is the only place a signal and
a real exit code exist.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import evolution
import evolution.tree as tree_package
from evolution.tree.campaign import CampaignResult
from evolution.tree.cli import tree_app
from evolution.tree.evolve import derive_run_id
from evolution.tree.target.reference_target import ReferenceTarget
from evolution.tree.target.reference_target_http import serve
from tests.tree.manifest.test_manifest_v15 import v15_from
from tests.tree_evolve_support import POLICY, write_workspace

pytestmark = pytest.mark.slow

SRC = Path(evolution.__file__).resolve().parent.parent
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": str(SRC)}


def _workspace15(root: Path, **options: Any) -> Path:
    """A `write_workspace` document lifted to `/15` with `v15_from`'s default metric contract.

    The reference target behind `served_target` emits `assistant_message` events,
    which that default contract reads. `tests.test_tree_family14_public_links`'s
    helper pins the fake adapter's `message` contract instead, which this target
    does not serve, so it is not reused here.
    """

    path = write_workspace(root, **options)
    path.write_text(json.dumps(v15_from(json.loads(path.read_text()))))
    return path


EVOLVE_ARGV = [sys.executable, "-m", "evolution.cli.main", "tree", "evolve"]

#: The editor of the SIGINT case: announce that the session is open, then wait
#: to be cancelled. `started` lands in the editor binding's own `cwd`, which is
#: the one directory both the test and the child agree on without an env var.
SLEEPING_EDITOR_SCRIPT = r"""
import json
import pathlib
import sys
import time

json.loads(sys.stdin.read())
pathlib.Path("started").write_text("1", encoding="utf-8")
time.sleep(120)
"""


# -- the surface --------------------------------------------------------------


@pytest.mark.parametrize("name", ["SCHEMA", *(f"SCHEMA_V{n}" for n in range(1, 15))])
def test_retired_manifest_constants_are_removed_without_aliases(name: str) -> None:
    from evolution.tree.manifest import manifest_v2

    assert name not in tree_package.__all__
    assert not hasattr(tree_package, name)
    assert not hasattr(manifest_v2, name)


@pytest.mark.parametrize(
    "name",
    ["read_scorer_trace", "ScorerTraceReader", "ScorerTraceRecord", "ScorerTraceUnreadable"],
)
def test_promised_scorer_trace_exports_keep_their_identity(name: str) -> None:
    from evolution.tree.execution import scorer_traces

    assert name in tree_package.__all__
    assert getattr(tree_package, name) is getattr(scorer_traces, name)


def test_native_module_names_do_not_expand_the_aggregate_surface() -> None:
    from evolution.tree import campaign_policy
    from evolution.tree.manifest import manifest_v2

    assert manifest_v2.SCHEMA_V16 == "evolution-v2-manifest/16"
    assert campaign_policy.CampaignPolicyV12.__name__ == "CampaignPolicyV12"
    for name in ("SCHEMA_V16", "CampaignPolicyV12"):
        assert name not in tree_package.__all__
        assert not hasattr(tree_package, name)


def test_changelog_lists_all_retired_aggregate_and_manifest_names() -> None:
    retired_tree = {
        *(f"SCHEMA_V{n}" for n in range(5, 15)),
        *(f"CampaignPolicyV{n}" for n in (1, 5, 7, 8, 10)),
        *(
            f"{prefix}_CAMPAIGN_FAMILY_VERSION"
            for prefix in (
                "CONTINUABLE",
                "SUSPENDABLE",
                "DIAGNOSTIC",
                "MODEL_GROUPING",
                "RESILIENCE",
                "CONFIDENCE",
            )
        ),
    }
    retired = {
        *(f"evolution.tree:{name}" for name in retired_tree),
        "evolution.prompts:TREE_AGENT_REFLECTOR_SYSTEM_V2",
        "evolution.prompts:TREE_REFLECTOR_SYSTEM_V2",
        "evolution.tree.manifest_v2:SCHEMA",
        *(f"evolution.tree.manifest_v2:SCHEMA_V{n}" for n in range(1, 15)),
    }
    changelog = (SRC.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    # The names were removed in 2.0.0b10; its release section carries the table.
    section = changelog.split("## [2.0.0b10]", 1)[1].split("\n## [", 1)[0]
    rows = re.findall(r"^\| `([^`]+)` \| removed \|$", section, re.MULTILINE)
    assert len(rows) == len(set(rows))
    assert retired <= set(rows)


@pytest.mark.parametrize("version", [1, 4, 5, 7, 8, 9, 10])
def test_retired_policy_classes_are_removed_without_aliases(version):
    from evolution.tree import campaign_policy

    name = f"CampaignPolicyV{version}"
    assert name not in tree_package.__all__
    assert not hasattr(tree_package, name)
    assert not hasattr(campaign_policy, name)


def test_tree_registry_has_the_declared_commands() -> None:
    """PDR-0021 adds standalone evaluation to the campaign commands."""

    assert {command.name for command in tree_app.registered_commands} == {
        "authorize",
        "conformance",
        "evolve",
        "evaluate",
        "trace",
        "watch",
    }


def test_no_module_imports_a_deleted_module() -> None:
    """Spec §8: the old evaluator path is gone from the product, not just unused."""

    banned = (
        "evolution.tree.workflow",
        "evolution.tree.run",
        "evolution.tree.contracts",
        "evolution.tree.corpus",
        "evolution.tree.structure_review",
    )
    package = Path(evolution.__file__).resolve().parent
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
            for module in modules:
                assert module not in banned, f"{path}: {module}"
        for token in ("/eval-case", "RunState"):
            assert token not in text, f"{path}: {token}"


def test_the_deleted_modules_are_not_importable() -> None:
    for name in ("workflow", "run", "contracts", "corpus", "structure_review"):
        assert not (Path(evolution.__file__).resolve().parent / "tree" / f"{name}.py").exists()


def test_public_api_has_no_editor_implementation() -> None:
    """Spec §8: the donor editor stays reachable by module path only."""

    for name in ("CandidateEditor", "FileEditor", "FileEditRequest", "FileEditResult"):
        assert name not in tree_package.__all__
        assert not hasattr(tree_package, name)
    assert "AgentCliEditor" not in tree_package.__all__
    # The v2 protocol keeps its own export name.
    assert "CampaignCandidateEditor" in tree_package.__all__


def test_public_api_has_no_reflector_implementation() -> None:
    """The Reflector's kinds are the editor's rule, for the other role (spec §3.3).

    A caller speaks to a Reflector through the protocol and the two
    request/result types; the three implementations -- and `build_reflector`,
    which binds one from a manifest, exactly as the unexported `build_editor`
    binds an editor -- stay reachable by module path only, so the surface owes
    nothing to which kind a manifest happens to declare.
    """

    for name in (
        "ChatReflector",
        "AgentCliReflector",
        "SubprocessReflector",
        "CandidateReflector",
        "build_reflector",
        "build_editor",
    ):
        assert name not in tree_package.__all__
        assert not hasattr(tree_package, name)
    for name in (
        "CampaignCandidateReflector",
        "ReflectRequest",
        "ReflectResult",
        "ReflectorIdentity",
    ):
        assert name in tree_package.__all__
        assert getattr(tree_package, name, None) is not None


def test_evolve_options_are_the_declared_five() -> None:
    """PDR-0003: a manifest, a run directory and three presentation controls."""

    command = get_command(tree_app).commands["evolve"]  # type: ignore[attr-defined]
    options = {opt for parameter in command.params for opt in parameter.opts}

    # Click adds `--help` itself; every other option here is one W1 declared.
    assert options == {"--manifest", "--run-dir", "--quiet", "--json-indent", "--progress"}
    defaults = {parameter.name: parameter.default for parameter in command.params}
    assert defaults["json_indent"] == 2
    assert defaults["quiet"] is False
    assert defaults["progress"] is False


def test_evolve_does_not_validate_its_paths_in_click(tmp_path: Path) -> None:
    """§9: Click's own `UsageError` would print the absolute path a refusal hides."""

    command = get_command(tree_app).commands["evolve"]  # type: ignore[attr-defined]
    for name in ("manifest", "run_dir"):
        parameter = next(item for item in command.params if item.name == name)
        assert getattr(parameter.type, "exists", False) is False


# -- the exit-code map --------------------------------------------------------


class _Projection:
    """A `CampaignProjection` stand-in: the CLI only ever calls `to_payload`."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_payload(self) -> dict[str, Any]:
        return self._payload


def _result(
    terminal: str,
    outcome: str | None,
    *,
    code: str | None = None,
    suspended: dict[str, Any] | None = None,
) -> CampaignResult:
    return CampaignResult(
        terminal=cast(Any, terminal),
        quiescent=True,
        finished=None if outcome is None else {"outcome": outcome, "reason": "test"},
        owner=None if code is None else "executor",
        code=code,
        result=cast(Any, _Projection({"run_id": "c-run"})),
        status=cast(Any, _Projection({"run_id": "c-run", "state": terminal})),
        suspended=suspended,
    )


#: The `campaign_suspended` payload of the printed cases (I1 spec §5.2): a
#: crossed `target` rate threshold, the example of §5.5.
SUSPENSION: dict[str, Any] = {
    "reason": "target_unstable",
    "owner": "target",
    "evidence": {
        "evidence_kind": "threshold",
        "crossing": {
            "owner": "target",
            "kind": "rate",
            "count": 9,
            "denominator": 30,
            "max_count": 12,
            "max_rate": "0.250000000000",
        },
        "window_start": None,
    },
}


CASES: dict[str, tuple[CampaignResult, int]] = {
    "completed": (_result("finished", "completed"), 0),
    "stopped": (_result("finished", "stopped"), 3),
    "budget_exhausted": (_result("finished", "budget_exhausted"), 4),
    "infrastructure_failed": (_result("finished", "infrastructure_failed"), 1),
    "storage_capacity_exhausted": (
        _result("storage_capacity_exhausted", None, code="capacity"),
        1,
    ),
    "persistence_failed": (_result("persistence_failed", None, code="io"), 1),
    "suspended": (
        _result("suspended", None, code="target_unstable", suspended=SUSPENSION),
        1,
    ),
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return write_workspace(tmp_path / "ws")


@pytest.mark.parametrize("case", sorted(CASES))
def test_exit_codes(case: str, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, expected = CASES[case]

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(workspace), "--run-dir", str(workspace.parent / "run")],
    )

    assert invocation.exit_code == expected, invocation.output
    document = json.loads(invocation.stdout)
    assert list(document) == [
        "terminal",
        "quiescent",
        "finished",
        "suspended",
        "owner",
        "code",
        "result",
        "status",
    ]
    assert document["suspended"] == result.suspended
    assert document["terminal"] == result.terminal
    assert document["finished"] == result.finished
    assert document["code"] == result.code
    assert document["status"] == {"run_id": "c-run", "state": result.terminal}


def test_quiet_prints_the_outcome_and_nothing_else(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _expected = CASES["completed"]

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        [
            "evolve",
            "--manifest",
            str(workspace),
            "--run-dir",
            str(workspace.parent / "run"),
            "--quiet",
            "--json-indent",
            "0",
        ],
    )

    assert invocation.exit_code == 0, invocation.output
    document = json.loads(invocation.stdout)
    assert list(document) == ["terminal", "finished", "code"]
    # §6: the outcome and reason are printed in every mode, `--quiet` included.
    assert document["finished"] == {"outcome": "completed", "reason": "test"}


def test_the_result_document_is_redacted(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The S1 redactor runs over the document, with the manifest's own secrets."""

    monkeypatch.setenv("EVO_TREE_TOKEN", "credential-value")
    manifest = write_workspace(tmp_path / "secret", editor_env=("EVO_TREE_TOKEN",))
    result = CampaignResult(
        terminal="finished",
        quiescent=True,
        finished={"outcome": "completed", "reason": "note: credential-value"},
        owner=None,
        code=None,
        result=None,
        status=cast(Any, _Projection({"run_id": "c-run"})),
    )

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(manifest.parent / "run")],
    )

    assert invocation.exit_code == 0, invocation.output
    assert "credential-value" not in invocation.stdout
    assert "[REDACTED]" in invocation.stdout


def test_a_short_forwarded_value_is_named_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The secret floor's skip is announced before the run, by name (amendment A9)."""

    monkeypatch.setenv("W1_DEBUG", "1")
    manifest = write_workspace(tmp_path / "flag", editor_env=("W1_DEBUG",))
    result, expected = CASES["completed"]

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(manifest.parent / "run")],
    )

    assert invocation.exit_code == expected, invocation.output
    assert invocation.stderr.splitlines() == [
        "evo tree evolve: note: forwarded value W1_DEBUG is shorter than "
        "8 characters and is not redacted"
    ]
    # The note names the variable, not its value: `1` may appear only inside
    # the name and the literal `8 characters`, never as the forwarded value.
    assert "W1_DEBUG=" not in invocation.stderr
    assert "value 1" not in invocation.stderr


@pytest.mark.parametrize("value", ["abcdefgh", None])
def test_no_note_for_a_long_or_absent_forwarded_value(
    value: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value the floor keeps, and a name the environment does not have, are silent."""

    monkeypatch.delenv("W1_DEBUG", raising=False)
    if value is not None:
        monkeypatch.setenv("W1_DEBUG", value)
    manifest = write_workspace(tmp_path / "quiet", editor_env=("W1_DEBUG",))
    result, _expected = CASES["completed"]

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(manifest.parent / "run")],
    )

    assert invocation.stderr == ""


# -- refusals -----------------------------------------------------------------


def run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    """The installed entry point, in its own process: the only real exit code."""

    return subprocess.run(
        [*EVOLVE_ARGV, *argv],
        capture_output=True,
        text=True,
        env=SUBPROCESS_ENV,
        timeout=120,
        check=False,
    )


def test_refusal_exit_2_empty_stdout(tmp_path: Path) -> None:
    """A `/1` manifest carries no campaign: one line on stderr, nothing on stdout."""

    from tests.test_tree_evolve import write_v1_manifest

    (tmp_path / "seed").mkdir()
    manifest = write_v1_manifest(tmp_path)

    completed = run_cli("--manifest", str(manifest), "--run-dir", str(tmp_path / "run"))

    assert completed.returncode == 2
    assert completed.stdout == ""
    # PDR-0038 / ADR-0046: the loader owns the same schema refusal and guidance.
    assert completed.stderr.splitlines() == [
        "evo tree evolve: refused: manifest_schema at schema",
        "evo tree evolve: expected evolution-v2-manifest/15 or /16",
        "evo tree evolve: class: hard",
    ]


def test_a_missing_manifest_is_a_refusal_not_a_usage_error(tmp_path: Path) -> None:
    """The controller ruling: no `exists=True`, so §9's path rule holds."""

    completed = run_cli(
        "--manifest", str(tmp_path / "absent.json"), "--run-dir", str(tmp_path / "run")
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.splitlines() == [
        "evo tree evolve: refused: bad_path at manifest",
        "evo tree evolve: class: hard",
    ]
    assert str(tmp_path) not in completed.stderr


def test_a_15_refusal_names_a_manifest_path_not_a_filesystem_path(tmp_path: Path) -> None:
    """A `/15` refusal names a manifest path, never a filesystem path."""

    manifest = _workspace15(tmp_path / "ws")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["scorer"]["cwd"] = "absent"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    completed = run_cli("--manifest", str(manifest), "--run-dir", str(tmp_path / "ws" / "run"))

    assert completed.returncode == 2
    assert completed.stdout == ""
    lines = completed.stderr.splitlines()
    assert re.fullmatch(r"evo tree evolve: refused: [a-z_]+ at [a-z_.\[\]0-9]+", lines[0]), (
        completed.stderr
    )
    # Every line after the refusal is the class and the prose §3 gives the code:
    # neither may name a filesystem path either.
    assert lines[1] == "evo tree evolve: class: hard"
    assert str(tmp_path) not in completed.stderr


def test_an_unreadable_journal_file_prints_one_document_and_exits_1(tmp_path: Path) -> None:
    """The one-document contract holds over a journal the process cannot read.

    Three readers in the resume path meet the same unreadable file -- the id
    lookup, C1's own `open`, and the projection that describes the run root --
    and any of them escaping ends the command in a traceback with a broken
    stdout. What the operator gets instead is C1's persistence outcome: one JSON
    document on stdout, exit 1.
    """

    if os.geteuid() == 0:
        pytest.skip("root reads a mode 0o000 file")
    from evolution.tree.campaign import CAMPAIGN_FILE

    manifest = _workspace15(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"
    run_dir.mkdir(parents=True)
    journal = run_dir / CAMPAIGN_FILE
    journal.write_bytes(b'{"event": "campaign_created"}\n')
    journal.chmod(0o000)

    try:
        completed = run_cli("--manifest", str(manifest), "--run-dir", str(run_dir))
    finally:
        journal.chmod(0o600)

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["terminal"] == "persistence_failed"
    assert payload["owner"] == "campaign_journal"
    assert payload["status"]["kind"] == "campaign_status"
    assert payload["status"]["run_id"] == derive_run_id(run_dir)
    assert "Traceback" not in completed.stderr


def test_an_unwritable_run_directory_prints_one_document_and_exits_1(
    tmp_path: Path, served_target: int
) -> None:
    """A fresh run whose journal cannot be created keeps the one-document contract.

    The directory is empty, so nothing about the invocation is refusable; the
    failure happens when C1 opens its own journal for writing. The operator gets
    the persistence outcome on stdout and exit 1, never an empty stdout and a
    traceback on stderr.
    """

    if os.geteuid() == 0:
        pytest.skip("root writes into a mode 0o500 directory")
    manifest = _workspace15(tmp_path / "ws", port=served_target)
    run_dir = tmp_path / "ws" / "run"
    run_dir.mkdir(parents=True)
    run_dir.chmod(0o500)

    try:
        completed = run_cli("--manifest", str(manifest), "--run-dir", str(run_dir))
    finally:
        run_dir.chmod(0o700)

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["terminal"] == "persistence_failed"
    assert payload["owner"] == "campaign_journal"
    assert payload["status"]["kind"] == "campaign_status"
    assert "Traceback" not in completed.stderr


def test_a_pythonpath_under_the_seed_is_dropped_and_named_on_stderr(
    tmp_path: Path, served_target: int
) -> None:
    """The `PYTHONPATH` guard announces itself by program, never by path (A18).

    The child environment is built inside the CLI's own process, so the note is
    only observable through a real invocation: the entry is put on the operator's
    `PYTHONPATH`, and the run is expected to complete normally without it.
    """

    manifest = _workspace15(tmp_path / "ws", port=served_target)
    seed = tmp_path / "ws" / "seed"
    env = {**SUBPROCESS_ENV, "PYTHONPATH": str(seed) + os.pathsep + str(SRC)}

    completed = subprocess.run(
        [
            *EVOLVE_ARGV,
            "--manifest",
            str(manifest),
            "--run-dir",
            str(tmp_path / "ws" / "run"),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    note = (
        "evo tree evolve: note: scorer PYTHONPATH entry under the seed tree "
        "or the run directory dropped"
    )
    assert completed.stderr.splitlines().count(note) == 1
    # The programs share one environment, so the editor is named too -- and the
    # dropped path itself is nowhere on stderr (spec §9).
    assert note.replace("scorer", "editor") in completed.stderr
    assert str(seed) not in completed.stderr


# -- cancellation -------------------------------------------------------------


@pytest.fixture
def served_target(tmp_path: Path):
    """A reference target on an ephemeral loopback port, in this process."""

    target = ReferenceTarget(
        allowed_artifact_root=tmp_path.resolve(), policy=POLICY, seed_mode="exact"
    )
    server = serve(target, port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _past_the_notes(stderr: str) -> list[str]:
    """The stderr lines after a run's leading `note:` lines."""

    lines = stderr.splitlines()
    notes = [line for line in lines if line.startswith("evo tree evolve: note: ")]
    assert notes and lines[: len(notes)] == notes, stderr
    return lines[len(notes) :]


#: The run summary a campaign prints after its continuation line.
SUMMARY_LINE = "evo tree evolve: Skipped rounds: 0 · Continuation turns: 0, led to an edit: 0"


def test_sigint_maps_to_cancelled_on_15(tmp_path: Path, served_target: int) -> None:
    """SIGINT is still `stopped`, exit 3, one document; stderr is the corpus notes,
    the continuation and the run summary."""

    manifest = _workspace15(
        tmp_path / "ws", port=served_target, editor_script=SLEEPING_EDITOR_SCRIPT
    )
    started = tmp_path / "ws" / "editor" / "started"
    process = subprocess.Popen(
        [*EVOLVE_ARGV, "--manifest", str(manifest), "--run-dir", str(tmp_path / "ws" / "run")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=SUBPROCESS_ENV,
    )
    try:
        deadline = time.monotonic() + 60
        while not started.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()
            time.sleep(0.05)
        assert started.exists(), "the editor session never opened"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=90)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung child
            process.kill()
            process.communicate()

    assert process.returncode == 3, (stdout, stderr)
    run_dir = os.path.realpath(tmp_path / "ws" / "run")
    assert _past_the_notes(stderr) == [
        f"evo tree evolve: continue: set linked_from to {run_dir} and seed_root to "
        f"{run_dir}/artifacts/champions/v0 (the target must allow trees under it)",
        SUMMARY_LINE,
    ]
    document = json.loads(stdout)
    assert document["terminal"] == "finished"
    assert document["finished"]["outcome"] == "stopped"
    assert document["finished"]["reason"] == "cancelled"


def test_public_api_exports_the_manifest_inventory() -> None:
    """Four names (spec 2026-09-16 §1); the `Literal` aliases and the walk stay by module path."""

    for name in ("manifest_dependencies", "DependencyInventory", "Dependency", "EnvironmentName"):
        assert name in tree_package.__all__
        assert getattr(tree_package, name, None) is not None
    for name in ("DependencyKind", "ResolutionBase", "BoundaryCode", "argv_operands"):
        assert name not in tree_package.__all__


def test_public_api_exports_the_conformance_builder() -> None:
    import inspect
    from collections.abc import Mapping
    from typing import get_type_hints

    from evolution.tree.manifest import manifest_v2

    assert "conformance_manifest" in tree_package.__all__
    assert tree_package.conformance_manifest is manifest_v2.conformance_manifest
    signature = inspect.signature(tree_package.conformance_manifest)
    assert list(signature.parameters) == ["document", "base"]
    assert signature.parameters["document"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["base"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in signature.parameters.values())
    assert get_type_hints(tree_package.conformance_manifest) == {
        "document": Mapping[str, object],
        "base": Path,
        "return": manifest_v2.ManifestV2,
    }
    for name in ("SCHEMA", "SCHEMA_V1"):
        assert name not in tree_package.__all__
        assert not hasattr(tree_package, name)


def test_the_target_call_ledger_is_exported() -> None:
    for name in ("TargetCallLedger", "TargetCallSettlement", "TargetCallRecord"):
        assert name in tree_package.__all__
        assert getattr(tree_package, name).__module__ == "evolution.tree.target.target_calls"
    for name in (
        "network_outcome",
        "Operation",
        "UncertainReason",
        "NetworkOutcome",
        "RecordState",
    ):
        assert name not in tree_package.__all__


def test_the_comparable_links_surface_removes_one_name_and_adds_none() -> None:
    """D2a §3, §6: `LineageConditionChanged` is gone; the findings stay by module path.

    A link across a changed comparable field is no longer refused, so the class
    that refusal raised is not exported any more (a pre-1.0 alpha removal,
    recorded in `CHANGELOG.md`). The collected refusal is still
    `EvolveRefused`, reachable as before through `evolution.tree.execution.output_admission`, and
    its `findings` element, `RefusalFinding`, sits beside it.
    """

    from evolution.tree import campaign
    from evolution.tree.execution import output_admission

    assert "LineageConditionChanged" not in tree_package.__all__
    assert not hasattr(tree_package, "LineageConditionChanged")
    assert not hasattr(campaign, "LineageConditionChanged")
    for name in ("EvolveRefused", "RefusalFinding"):
        assert name in output_admission.__all__
        assert name not in tree_package.__all__


# -- the continuation line (continuable terminals §8) --------------------------


def test_cli_prints_the_continue_line_on_stderr_on_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `/13` twin of the test above: the continuation is followed by the run summary."""

    from tests.campaign_fakes import FakeAdapter

    monkeypatch.setattr(
        "evolution.tree.evolve._http_session_adapter",
        lambda _manifest, _run_id, **_kwargs: FakeAdapter(),
    )
    manifest = write_workspace(tmp_path / "ws")
    run_dir = tmp_path / "ws" / "run"

    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 0, invocation.output
    document = json.loads(invocation.stdout)
    champion = document["status"]["campaign_outcome"]["champion_path"]
    assert champion.startswith("artifacts/champions/")
    assert (run_dir / champion).is_dir()
    absolute = os.path.realpath(run_dir)
    assert _past_the_notes(invocation.stderr) == [
        f"evo tree evolve: continue: set linked_from to {absolute} and seed_root to "
        f"{absolute}/{champion} (the target must allow trees under it)",
        SUMMARY_LINE,
    ]


@pytest.mark.parametrize("quiet", [False, True])
def test_the_continue_line_carries_the_canonical_absolute_run_dir(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet: bool
) -> None:
    """Ruling R15: never `--run-dir` as typed -- both keys resolve against the
    successor manifest's directory, where a relative path names another place."""

    monkeypatch.chdir(tmp_path)
    result = CampaignResult(
        terminal="finished",
        quiescent=True,
        finished={"outcome": "stopped", "reason": "test"},
        owner=None,
        code=None,
        result=None,
        status=cast(
            Any,
            _Projection({"campaign_outcome": {"champion_path": "artifacts/champions/v3"}}),
        ),
    )

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(workspace), "--run-dir", "runs/run-1/"]
        + (["--quiet"] if quiet else []),
    )

    assert invocation.exit_code == 3, invocation.output
    json.loads(invocation.stdout)
    absolute = os.path.realpath(tmp_path / "runs" / "run-1")
    assert os.path.isabs(absolute)
    assert invocation.stderr.splitlines() == [
        f"evo tree evolve: continue: set linked_from to {absolute} and seed_root to "
        f"{absolute}/artifacts/champions/v3 (the target must allow trees under it)"
    ]


def test_the_printed_keys_link_a_13_successor_whose_manifest_lives_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `/13` twin of the test above: the continuation is the line before the run
    summary, and the successor is `/13` too."""

    from tests.campaign_fakes import FakeAdapter

    monkeypatch.setattr(
        "evolution.tree.evolve._http_session_adapter",
        lambda _manifest, _run_id, **_kwargs: FakeAdapter(),
    )
    monkeypatch.chdir(tmp_path)
    write_workspace(tmp_path / "demo")

    first = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", "demo/manifest.json", "--run-dir", "demo/run-1"],
    )

    assert first.exit_code == 0, first.output
    line, summary = _past_the_notes(first.stderr)
    assert summary == SUMMARY_LINE
    match = re.fullmatch(
        r"evo tree evolve: continue: set linked_from to (\S+) and seed_root to (\S+) "
        r"\(the target must allow trees under it\)",
        line,
    )
    assert match is not None, line
    linked_from, seed_root = match.groups()
    assert Path(linked_from).is_absolute()
    # Resolved the way the loader resolves it: against the manifest's directory.
    other = tmp_path / "elsewhere" / "nested"
    assert os.path.realpath(other / linked_from) == os.path.realpath(tmp_path / "demo" / "run-1")
    assert Path(seed_root).is_dir()

    successor = write_workspace(other, linked_from=linked_from, seed_root=seed_root)
    second = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(successor), "--run-dir", str(tmp_path / "run-2")],
    )

    assert second.exit_code == 0, second.output
    created = json.loads((tmp_path / "run-2" / "campaign.jsonl").read_bytes().splitlines()[0])
    assert created["payload"]["identity"]["linked_from"]["run_id"] == derive_run_id(
        tmp_path / "demo" / "run-1"
    )


@pytest.mark.parametrize("case", ["persistence_failed", "storage_capacity_exhausted"])
def test_no_continue_line_without_a_finished_campaign(
    case: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, expected = CASES[case]

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(workspace), "--run-dir", str(workspace.parent / "run")],
    )

    assert invocation.exit_code == expected, invocation.output
    assert "continue:" not in invocation.stderr


@pytest.mark.parametrize("quiet", [False, True])
def test_a_suspension_prints_its_cause_and_the_command_that_continues_it(
    quiet: bool, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1 spec §5.5: the cause, its evidence, and the same `evolve` again --
    on stderr in every mode, `--quiet` included, and exit 1."""

    result, expected = CASES["suspended"]

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    run_dir = workspace.parent / "run"
    invocation = CliRunner().invoke(
        tree_app,
        [
            "evolve",
            "--manifest",
            str(workspace),
            "--run-dir",
            str(run_dir),
            *(["--quiet"] if quiet else []),
        ],
    )

    assert invocation.exit_code == expected == 1, invocation.output
    assert invocation.stderr.splitlines() == [
        "evo tree evolve: suspended: target_unstable — 9 of 30 tries in this window, "
        "threshold 25 %",
        f"evo tree evolve: continue: evo tree evolve --manifest {workspace} --run-dir {run_dir}",
    ]
    # The successor's two keys belong to a finished campaign; a suspension is
    # continued in place, so its own `continue:` line is the only one printed.
    assert "set linked_from" not in invocation.stderr
    document = json.loads(invocation.stdout)
    assert document["terminal"] == "suspended"
    assert document["code"] == "target_unstable"


@pytest.mark.parametrize(
    ("evidence", "detail"),
    [
        (SUSPENSION["evidence"], "9 of 30 tries in this window, threshold 25 %"),
        (
            {
                "evidence_kind": "threshold",
                "crossing": {
                    "owner": "scorer",
                    "kind": "count",
                    "count": 3,
                    "denominator": 8,
                    "max_count": 2,
                    "max_rate": "0.500000000000",
                },
                "window_start": 41,
            },
            "3 of 8 tries in this window, threshold 2 tries",
        ),
        (
            {
                "evidence_kind": "fault_rows",
                "count": 2,
                "rows_digest": "0" * 64,
                "rows": [{"code": "invalid_response"}, {"code": None}],
                "truncated": False,
            },
            "2 fault rows in this stage, the first coded invalid_response",
        ),
        (
            {
                "evidence_kind": "fault_rows",
                "count": 1,
                "rows_digest": "0" * 64,
                "rows": [{"code": None}],
                "truncated": False,
            },
            "1 fault row in this stage",
        ),
        (
            {"evidence_kind": "stage_record", "seq": 17, "operation_id": "op-1"},
            "decided by the record at sequence 17",
        ),
    ],
)
def test_every_evidence_kind_reads_as_a_sentence(evidence: dict[str, Any], detail: str) -> None:
    """Each `evidence_kind` of §5.2 names what the operator has to act on."""

    from evolution.tree.cli import _suspension_detail

    assert _suspension_detail(evidence) == detail


def test_target_components_changed_prints_its_cause(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G2b3b: component drift names its cause in plain words, not only the
    record that decided it, and is continued by the same `evolve`."""

    suspended = {
        "reason": "target_components_changed",
        "owner": "target",
        "evidence": {"evidence_kind": "stage_record", "seq": 17, "operation_id": "op-1"},
    }
    result = _result("suspended", None, code="target_components_changed", suspended=suspended)

    async def fake_evolve(*_args: object, **_kwargs: object) -> CampaignResult:
        return result

    monkeypatch.setattr("evolution.tree.evolve.evolve", fake_evolve)
    run_dir = workspace.parent / "run"
    invocation = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(workspace), "--run-dir", str(run_dir)]
    )

    assert invocation.exit_code == 1, invocation.output
    assert invocation.stderr.splitlines() == [
        "evo tree evolve: suspended: target_components_changed — the Target rendered a "
        "component map other than the one this condition already rendered for the tree",
        f"evo tree evolve: continue: evo tree evolve --manifest {workspace} --run-dir {run_dir}",
    ]


def test_public_api_exports_the_family_14_surface() -> None:
    for name in (
        "SCHEMA_V15",
        "CampaignPolicyV11",
        "FINAL_CAMPAIGN_FAMILY_VERSION",
        "ConnectionContract",
    ):
        assert name in tree_package.__all__
        assert getattr(tree_package, name, None) is not None
    assert tree_package.SCHEMA_V15 == "evolution-v2-manifest/15"
    assert tree_package.FINAL_CAMPAIGN_FAMILY_VERSION == 14


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("evolution.tree.execution.interaction", "EvalCase"),
        ("evolution.tree.execution.interaction", "Turn"),
        ("evolution.tree.execution.rollout_scoring", "parse_verdicts"),
        ("evolution.tree.execution.wire_v2", "is_artifact_path"),
        ("evolution.tree.execution.wire_v2", "PREFIX"),
        ("evolution.tree.execution.wire_v2", "STRATEGY"),
        ("evolution.tree.execution.wire_v2", "validate_instance_description"),
        ("evolution.tree.artifacts.artifact_policy", "scan_tree"),
    ],
)
def test_execution_consumer_exports_keep_their_identity(module: str, name: str) -> None:
    assert name in tree_package.__all__
    assert getattr(tree_package, name) is getattr(importlib.import_module(module), name)


@pytest.mark.parametrize("name", ["evaluate", "inspect_evaluation"])
def test_evaluation_consumer_exports_keep_their_identity(name: str) -> None:
    assert name in tree_package.__all__
    assert getattr(tree_package, name) is getattr(
        importlib.import_module("evolution.tree.evaluation"), name
    )


def test_evaluation_root_imports_are_eager_in_a_fresh_process() -> None:
    program = """
import sys
import evolution.tree as tree

assert {"evaluate", "inspect_evaluation"} <= vars(tree).keys()
assert "evolution.tree.evaluation" in sys.modules
from evolution.tree import evaluate, inspect_evaluation

assert evaluate is sys.modules["evolution.tree.evaluation"].evaluate
assert inspect_evaluation is sys.modules["evolution.tree.evaluation"].inspect_evaluation
"""
    result = subprocess.run(
        [sys.executable, "-c", program], env=SUBPROCESS_ENV, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_evaluation_exports_follow_the_existing_ordered_aggregate() -> None:
    expected = """
EvalCase Turn parse_verdicts is_artifact_path PREFIX STRATEGY validate_instance_description
Journal Snapshot RunIdentityV1 ScorerRecoveryMode JournalRolloutAdapter inspect_journal
read_scorer_trace ScorerTraceReader ScorerTraceRecord ScorerTraceUnreadable
project_events project_result project_status recover_journal recovery_plan durable_rollout
durable_score JournalAdmissionRefused JournalBoundaryError JournalFault JournalRecoveryRequired
ArtifactStore ArtifactPolicyV1 scan_tree ArtifactRef ArtifactReference CandidateEntry ChampionEntry
CapacityEstimate ExpectedAttestation LineageProjection RecoveryReport SelectionDecision project_lineage
ArtifactError ArtifactUnsafeError ArtifactCapacityError ArtifactCorruptionError
ArtifactAdmissionRefused ArtifactStoreStopped AttestationError PromotionCapableAdapter TargetPreflight
verify_attestation HttpInstance HttpSessionAdapter HttpTransport TargetCallLedger TargetCallSettlement
TargetCallRecord Authorization RefusalDifference read_decisions refusal_class write_decision
ManifestError SCHEMA_V15 ManifestV2 TargetBinding load_manifest_v2 conformance_manifest
manifest_dependencies DependencyInventory Dependency EnvironmentName new_instance_id
Check ConformanceReport Scenario adapter_conformance wire_conformance run_conformance
ReferenceTarget serve_reference_target TargetServer TargetBackend BackendInstance BackendSession
BackendReply LoadedTree BackendError serve_target CallRetries Message TurnResult Usage
ArtifactLedgerMissing CampaignCandidateEditor CampaignCandidateReflector CampaignExists
CampaignIdentityV1 CampaignMissing CampaignPolicyV11 FINAL_CAMPAIGN_FAMILY_VERSION ConnectionContract
CampaignProjection CampaignResult CampaignSnapshot CampaignUnreadable CampaignView EditBrief
EditRequest EditResult EditorIdentity IdentityChanged LineageChampionUnverified
LineageFamilyUnsupported LineageModeUnsupported LineageNotContinuable LineageNotFinished LinkedFrom
PolicyError ReflectRequest ReflectResult ReflectorIdentity UndeclaredSibling inspect_campaign
next_actions project_campaign_result project_campaign_status read_run_id resume_campaign run_campaign
evaluate inspect_evaluation
""".split()
    assert tree_package.__all__ == expected
