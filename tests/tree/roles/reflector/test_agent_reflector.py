"""`AgentCliReflector`, the `agent_cli` `CandidateReflector`.

The fake backend is `tests/tree/roles/editor/test_agent_editor.py`'s on the reflector's
constructor: it records the keyword arguments the kind built it with, runs an
injected callable over the *real* frozen projection, and is rebuilt once per
call, which is how a test sees that the repair call is a second CLI run that
reads the `VALIDATION.json` the first one earned.

The projection these tests hand the kind is the one the campaign hands it: a
`project(..., control_prefix=".evolution")` whose control directory exists and
whose every other entry is read-only.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.prompts import TREE_AGENT_REFLECTOR_SYSTEM
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.editor_contracts import EMPTY_HISTORY
from evolution.tree.roles.environment_docs import EnvironmentDocs
from evolution.tree.roles.reflector.agent_reflector import (
    CONTROL_PREFIX,
    REFLECT_REQUEST_FILE,
    REFLECTION_FILE,
    VALIDATION_FILE,
    AgentCliReflector,
)
from evolution.tree.roles.reflector.reflect_brief import (
    build_reflect_brief,
    reflect_brief_payload,
    render_reflect_catalog,
    render_reflect_opening,
)
from evolution.tree.roles.reflector.reflect_evidence import select_focus
from evolution.tree.roles.reflector_contracts import (
    REFLECT_REQUEST_VERSION,
    ProbeAccess,
    ReflectRequest,
)
from evolution.tree.roles.seals import agent_reflector_identity, seal_program, seal_prompt
from evolution.tree.roles.workspace import discard, freeze_projection, project
from tests.tree.roles.reflector.test_reflect_brief import _CHAMPION, _corpus
from tests.tree.roles.reflector.test_reflect_evidence import _snapshot, rep

pytestmark = pytest.mark.slow

SECRET = "sekret-key-value"
CHILD_ENV = {"PATH": "/usr/bin", "OPENAI_API_KEY": SECRET}


# --------------------------------------------------------------------------
# Fakes and fixtures
# --------------------------------------------------------------------------


class FakeBackend:
    """One CLI call: the kind's kwargs, the projection it is given, one run."""

    def __init__(self, *, run: Any, usage: Any, content: str, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._run = run
        self._content = content
        self.work_dir: Path | None = None
        self.opening: str | None = None
        self.system: str | None = None
        self.timeout_s: float | None = None
        self.validation: Any = None
        self.last_usage = dict(usage)
        self.last_codex_diagnostics: dict[str, Any] = {"last_message": "done"}

    async def complete_file_edit(
        self,
        work_dir: Path,
        prompt: str,
        system: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        root = Path(work_dir)
        self.work_dir = root
        self.opening = prompt
        self.system = system
        self.timeout_s = timeout_s
        validation = root / CONTROL_PREFIX / VALIDATION_FILE
        self.validation = (
            json.loads(validation.read_text(encoding="utf-8")) if validation.exists() else None
        )
        self._run(root)
        return SimpleNamespace(content=self._content, tokens_in=1, tokens_out=1, time_ms=1)


def factory(
    *, runs: list[Any], usage: list[dict[str, Any]] | None = None, content: str = "diagnosed"
) -> tuple[Any, list[FakeBackend]]:
    instances: list[FakeBackend] = []
    usages = list(usage or [{"tool_turns": 1}])

    def build(**kwargs: Any) -> FakeBackend:
        index = len(instances)
        backend = FakeBackend(
            run=runs[min(index, len(runs) - 1)],
            usage=usages[min(index, len(usages) - 1)],
            content=content,
            **kwargs,
        )
        instances.append(backend)
        return backend

    return build, instances


def _program(tmp_path: Path) -> Path:
    program = tmp_path / "bin" / "codex"
    if not program.exists():
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        program.chmod(0o755)
    return program


def _reflector(
    tmp_path: Path,
    runs: list[Any],
    *,
    usage: list[dict[str, Any]] | None = None,
    content: str = "diagnosed",
    **section_kw: Any,
) -> tuple[AgentCliReflector, list[FakeBackend]]:
    build, instances = factory(runs=runs, usage=usage, content=content)
    section_kw.setdefault("model", "codex")
    reflector = AgentCliReflector(
        section=AgentCliEditorSection(**section_kw),
        program=seal_program(_program(tmp_path)),
        prompt=seal_prompt(None, packaged=TREE_AGENT_REFLECTOR_SYSTEM),
        child_env=dict(CHILD_ENV),
        log_root=tmp_path / "run" / "reflector-logs",
        secrets=(SECRET,),
        backend_factory=build,
    )
    return reflector, instances


def _evidence():
    reps = (
        rep("case-01", 1, failed=("answers",)),
        rep("case-01", 2),
        rep("case-02", 1),
    )
    focus = select_focus(reps, snapshot=_snapshot({}))
    return build_reflect_brief(
        focus=focus,
        repeats=reps,
        corpus=_corpus(),
        champion_digest=_CHAMPION,
        operator_context="operator says hello",
        history=EMPTY_HISTORY,
        editable_paths=("skills/**",),
    )


def _seed(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / "skills").mkdir(parents=True, exist_ok=True)
    (seed / "skills" / "SKILL.md").write_text("seed\n", encoding="utf-8")
    return seed


@pytest.fixture
def reflect_request(tmp_path: Path):
    """The campaign's own projection for an `agent_cli` Reflector."""

    tmp_root = tmp_path / "run" / ".reflections"
    tmp_root.mkdir(parents=True, exist_ok=True)
    projection = project(_seed(tmp_path), control_prefix=CONTROL_PREFIX, tmp_root=tmp_root)
    # The freeze creates the control plane and leaves it -- and only it -- writable.
    freeze_projection(projection)
    try:
        yield ReflectRequest(
            reflection_id="r01-x01",
            round=1,
            workspace=projection.root,
            evidence=_evidence(),
            environment_docs=None,
            probe=None,
            editable_paths=("skills/**",),
            deadline_seconds=30.0,
        )
    finally:
        discard(projection)


def _docs(text: str, files: tuple[tuple[str, bytes], ...] = ()) -> EnvironmentDocs:
    return EnvironmentDocs(text, hashlib.sha256(text.encode()).hexdigest(), files)


def _with_docs(request: ReflectRequest, docs: EnvironmentDocs | None) -> ReflectRequest:
    return ReflectRequest(
        request.reflection_id,
        request.round,
        request.workspace,
        request.evidence,
        docs,
        request.probe,
        request.editable_paths,
        request.deadline_seconds,
    )


def _obs_id(request: ReflectRequest) -> str:
    catalog = render_reflect_catalog(request.evidence)
    return next(record.evidence_id for record in catalog if record.kind == "observation")


def _group(evidence_id: str) -> dict[str, Any]:
    return {
        "root_cause": "the skill never states the required output shape",
        "suggested_fix": "state the output shape in SKILL.md",
        "evidence_ids": [evidence_id],
        "affected_cases": ["case-01"],
        "target_paths": ["skills/SKILL.md"],
    }


def _writes(payload: Any) -> Any:
    """A backend run that writes `payload` as the reflection result file."""

    def run(root: Path) -> None:
        path = root / CONTROL_PREFIX / REFLECTION_FILE
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    return run


def _result(status: str, groups: list[Any], note: str = "done") -> dict[str, Any]:
    return {"status": status, "groups": groups, "note": note}


def _listing(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(root).as_posix(): (stat.S_IMODE(path.lstat().st_mode), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and CONTROL_PREFIX not in path.parts
    }


def _log_lines(
    reflector: AgentCliReflector, reflection_id: str = "r01-x01"
) -> list[dict[str, Any]]:
    path = Path(reflector.log_root) / f"{reflection_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------------------
# Identity and binding
# --------------------------------------------------------------------------


def test_identity_is_the_declared_configuration(tmp_path: Path) -> None:
    reflector, _instances = _reflector(tmp_path, [lambda root: None])

    assert reflector.identity == agent_reflector_identity(
        reflector.section, reflector.prompt, reflector.program
    )
    assert reflector.identity.kind == "agent_cli"
    # The campaign reads this to project a writable control plane for the kind.
    assert AgentCliReflector.control_prefix == CONTROL_PREFIX == ".evolution"


# --------------------------------------------------------------------------
# The control plane and the happy path
# --------------------------------------------------------------------------


async def test_reflected_carries_the_control_plane_and_returns_the_group_raw(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    catalog = render_reflect_catalog(reflect_request.evidence)
    group = _group(_obs_id(reflect_request))
    observed: dict[str, Any] = {}

    def run(root: Path) -> None:
        control = root / CONTROL_PREFIX
        observed["request"] = json.loads(
            (control / REFLECT_REQUEST_FILE).read_text(encoding="utf-8")
        )
        observed["index"] = json.loads(
            (control / "evidence" / "index.json").read_text(encoding="utf-8")
        )
        observed["first"] = (control / "evidence" / "000000.txt").read_text(encoding="utf-8")
        observed["docs"] = (control / "environment-docs").exists()
        _writes(_result("reflected", [group]))(root)

    reflector, instances = _reflector(tmp_path, [run])

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.groups, result.note) == ("reflected", (group,), "done")
    document = observed["request"]
    assert set(document) == {
        "version",
        "reflection_id",
        "round",
        "workspace",
        "editable_paths",
        "deadline_seconds",
        "environment_docs",
        "probe",
        "evidence",
        "opening",
        "limits",
    }
    assert document["version"] == REFLECT_REQUEST_VERSION == "reflect_request/2"
    assert document["reflection_id"] == "r01-x01"
    assert document["evidence"] == reflect_brief_payload(reflect_request.evidence)
    assert document["opening"] == render_reflect_opening(
        reflect_request.evidence, catalog, ("skills/**",), None
    )
    assert document["limits"] == {"max_turns": None, "deadline_seconds": 30.0}
    assert observed["index"] == [
        {"id": record.evidence_id, "kind": record.kind, "path": f"evidence/{index:06d}.txt"}
        for index, record in enumerate(catalog)
    ]
    assert observed["first"] == catalog[0].content
    assert observed["docs"] is False, "no documentation directory without documentation"

    backend = instances[0]
    assert backend.opening == document["opening"]
    assert backend.system == TREE_AGENT_REFLECTOR_SYSTEM
    assert backend.validation is None
    assert backend.kwargs["agent_bin"] == str(_program(tmp_path))
    assert backend.kwargs["env"] == CHILD_ENV
    assert SECRET not in (Path(reflector.log_root) / "r01-x01.jsonl").read_text(encoding="utf-8")


async def test_probe_access_is_control_only_and_echoes_are_redacted(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """Access stays in the sealed control plane, including the legacy private path."""

    request = ReflectRequest(
        reflect_request.reflection_id,
        reflect_request.round,
        reflect_request.workspace,
        reflect_request.evidence,
        reflect_request.environment_docs,
        ProbeAccess("http://127.0.0.1:9/health", 4, ("GET",), None),
        reflect_request.editable_paths,
        reflect_request.deadline_seconds,
    )
    observed: dict[str, Any] = {}

    def run(root: Path) -> None:
        observed["request"] = json.loads(
            (root / CONTROL_PREFIX / REFLECT_REQUEST_FILE).read_text(encoding="utf-8")
        )
        _writes(_result("declined", []))(root)

    reflector, _instances = _reflector(tmp_path, [run], content=request.probe.url)

    result = await reflector.reflect(request)

    assert result.status == "declined"
    ignored = [line for line in _log_lines(reflector) if line["phase"] == "probe_ignored"]
    assert ignored == []
    assert request.probe.url not in json.dumps(_log_lines(reflector))
    assert observed["request"]["probe"]["url"] == "http://127.0.0.1:9/health"
    assert (request.workspace / CONTROL_PREFIX / "probe" / "probe_request.py").is_file()
    assert (
        request.workspace / CONTROL_PREFIX / REFLECT_REQUEST_FILE
    ).stat().st_mode & 0o777 == 0o400


async def test_environment_documentation_is_rendered_into_the_control_plane(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """The contract: `environment-docs/` carries *the snapshot's files*, and
    `RENDERED.txt` stays beside them as the whole rendering. Before the branch
    review only `RENDERED.txt` was written, so an agent could not read one
    document by its own path (round 1, `agent-reflector-environment-files-missing`)."""

    observed: dict[str, Any] = {}
    files = (("catalog.md", b"The target exposes /health.\n"), ("nested/api.md", b"GET /people\n"))

    def run(root: Path) -> None:
        docs = root / CONTROL_PREFIX / "environment-docs"
        observed["rendered"] = (docs / "RENDERED.txt").read_text(encoding="utf-8")
        observed["files"] = {
            str(path.relative_to(docs)): path.read_bytes()
            for path in sorted(docs.rglob("*"))
            if path.is_file() and path.name != "RENDERED.txt"
        }
        _writes(_result("declined", []))(root)

    reflector, _instances = _reflector(tmp_path, [run])

    result = await reflector.reflect(
        _with_docs(reflect_request, _docs("The target exposes /health.\n", files))
    )

    assert result.status == "declined"
    assert observed["rendered"] == "The target exposes /health.\n"
    assert observed["files"] == dict(files)


# --------------------------------------------------------------------------
# The one repair, and the second failure
# --------------------------------------------------------------------------


async def test_a_malformed_result_buys_one_repair_call(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    group = _group(_obs_id(reflect_request))
    reflector, instances = _reflector(
        tmp_path, [_writes("not json at all"), _writes(_result("reflected", [group]))]
    )

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.groups) == ("reflected", (group,))
    assert len(instances) == 2, "the repair is a second CLI call"
    assert instances[0].validation is None
    assert instances[1].validation is not None and instances[1].validation["issues"]
    checks = [line for line in _log_lines(reflector) if line["phase"] == "self_check"]
    assert [check["call"] for check in checks] == [1]


async def test_a_group_that_fails_the_structural_check_buys_the_same_repair(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """The kind's own pre-check is `validate_groups` with no needles."""

    good = _group(_obs_id(reflect_request))
    bad = {**good, "evidence_ids": ["obs:not-in-the-catalog:9"]}
    reflector, instances = _reflector(
        tmp_path, [_writes(_result("reflected", [bad])), _writes(_result("reflected", [good]))]
    )

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.groups) == ("reflected", (good,))
    assert "unknown record" in " ".join(instances[1].validation["issues"])


async def test_two_unusable_results_are_self_check_failed(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, instances = _reflector(tmp_path, [_writes("garbage"), _writes("garbage again")])

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.groups, result.note) == ("failed", (), "self_check_failed")
    assert result.code == "self_check_failed"
    assert len(instances) == 2
    checks = [line for line in _log_lines(reflector) if line["phase"] == "self_check"]
    assert [check["call"] for check in checks] == [1, 2]


async def test_a_missing_result_file_is_the_same_failure(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, instances = _reflector(tmp_path, [lambda root: None])

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.note) == ("failed", "self_check_failed")
    assert result.code == "self_check_failed"
    assert len(instances) == 2


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


async def test_an_edit_to_the_tree_changes_nothing_and_the_reflection_still_lands(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """The Reflector's projection is frozen: a CLI that edits the tree edits nothing.

    The campaign discards the projection either way -- nothing a Reflector
    writes is ever admitted or published -- so the kind neither diffs the tree
    nor refuses over it; the file is simply still the Parent's own.
    """

    group = _group(_obs_id(reflect_request))
    before = _listing(reflect_request.workspace)

    def run(root: Path) -> None:
        try:
            (root / "skills" / "SKILL.md").write_text("edited by the reflector\n", encoding="utf-8")
        except OSError:
            pass  # what a real CLI meets: the projection is read-only
        _writes(_result("reflected", [group]))(root)

    reflector, _instances = _reflector(tmp_path, [run])

    result = await reflector.reflect(reflect_request)

    assert result.status == "reflected"
    assert _listing(reflect_request.workspace) == before
    assert (reflect_request.workspace / "skills" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "seed\n"


async def test_max_turns_is_a_post_run_acceptance_limit(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    group = _group(_obs_id(reflect_request))
    reflector, instances = _reflector(
        tmp_path,
        [_writes(_result("reflected", [group]))],
        usage=[{"tool_turns": 5}],
        max_turns=2,
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "failed"
    assert result.note.startswith("budget ")
    assert result.code == "budget"
    assert len(instances) == 1, "the limit is checked after the run, never sent to the CLI"


async def test_a_backend_failure_is_an_agent_error(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    def run(root: Path) -> None:
        raise RuntimeError("the CLI died")

    reflector, _instances = _reflector(tmp_path, [run])

    result = await reflector.reflect(reflect_request)

    assert result.status == "failed"
    assert result.note.startswith("agent_error ")
    assert result.code == "agent_error"
    assert result.groups == ()


async def test_reflection_id_must_be_one_path_segment(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, instances = _reflector(tmp_path, [lambda root: None])
    escaping = ReflectRequest(
        "../escape",
        reflect_request.round,
        reflect_request.workspace,
        reflect_request.evidence,
        None,
        None,
        reflect_request.editable_paths,
        reflect_request.deadline_seconds,
    )

    with pytest.raises(ValueError):
        await reflector.reflect(escaping)
    assert instances == []


@pytest.mark.parametrize(
    "refs,valid",
    [([999], True), ([], True), ([True], False), ([0], False), ([1.0], False), ("1", False)],
)
async def test_adaptive_probe_refs_check_shape_without_inventing_authority(tmp_path, refs, valid):
    from tests.test_tree_adaptive_reflector import adaptive_fixture, group

    trace, inputs, brief = adaptive_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        with inputs.reader_view().open_operation(
            "reflect-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1",
                1,
                workspace,
                brief,
                None,
                ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 1, ("GET",), None),
                ("*.md",),
                30,
                reader=reader,
            )
            claimed = group(
                evidence_ids=list(brief.observation_ids), affected_cases=["c"], probe_refs=refs
            )
            reflector, instances = _reflector(tmp_path, [_writes(_result("reflected", [claimed]))])
            result = await reflector.reflect(request)
            assert result.status == ("reflected" if valid else "failed"), result.note
            assert len(instances) == 1
            if valid:
                assert result.groups[0]["probe_refs"] == refs
            else:
                assert result.note == "invalid_result"
                assert result.code == "invalid_result"
    finally:
        trace.close()


@pytest.mark.parametrize(
    "filename",
    [
        "probe/session.json",
        "probe/probe_client.py",
        "probe/probe_wire.py",
        "probe/probe_request.py",
        "REFLECT_REQUEST.json",
    ],
)
async def test_adaptive_probe_control_files_are_sealed(tmp_path, filename):
    from tests.test_tree_adaptive_reflector import adaptive_fixture

    trace, inputs, brief = adaptive_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        with inputs.reader_view().open_operation(
            "reflect-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1",
                1,
                workspace,
                brief,
                None,
                ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 1, ("GET",), None),
                ("*.md",),
                30,
                reader=reader,
            )

            def run(root):
                assert (root / ".evolution" / REFLECT_REQUEST_FILE).stat().st_mode & 0o777 == 0o400
                path = root / ".evolution" / filename
                path.chmod(0o600)
                path.write_text("changed")
                _writes(_result("declined", []))(root)

            reflector, instances = _reflector(tmp_path, [run])
            result = await reflector.reflect(request)
            assert result.status == "failed"
            assert len(instances) == 1
    finally:
        trace.close()


async def test_probe_helper_malformed_arguments_are_fixed_and_url_not_argv(tmp_path):
    import asyncio
    import time

    from evolution.tree.roles.reflection_role_helpers import install_probe_helper

    access = ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 1, ("GET",), None)
    helpers = install_probe_helper(tmp_path / "probe", access, deadline_at=time.monotonic() + 30)
    command = helpers.descriptors["probe_request"]
    assert access.url not in str(command)
    for arguments in [[], ['{"method":"GET"}'], ["not-json"], ["{}", access.url]]:
        process = await asyncio.create_subprocess_exec(
            *command, *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        assert json.loads(stdout) == {
            "error": "invalid_request",
            "detail": "probe helper request failed",
        }
        assert stderr == b""
    helpers.verify()


@pytest.mark.parametrize("bound_session", [True, False])
async def test_probe_helper_seals_remaining_operation_deadline(
    tmp_path, reflect_request, bound_session
):
    import asyncio
    import time
    from dataclasses import replace

    from tests.tree.target.test_probe_mediator import config, session

    loop = asyncio.get_running_loop()
    owner = session(tmp_path, config("http://127.0.0.1:9"), [], deadline_at=loop.time() + 120)
    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 4, ("GET",), None),
        probe_session=owner if bound_session else None,
        deadline_seconds=300,
    )
    before = time.monotonic()
    expected_remaining = 120 if bound_session else 300
    captured = []

    def run(root):
        configuration = json.loads((root / CONTROL_PREFIX / "probe" / "session.json").read_text())
        captured.append(configuration)
        _writes(_result("declined", []))(root)

    reflector, _instances = _reflector(tmp_path, [run])
    try:
        assert (await reflector.reflect(request)).status == "declined"
        deadline = captured[0]["deadline_at"]
        assert abs(deadline - (before + expected_remaining)) < 1
        assert set(captured[0]) == {"url", "deadline_at"}
    finally:
        assert await owner.close()


async def test_call_preserves_backend_kwargs_deadline_and_logs(
    tmp_path, reflect_request, monkeypatch
):
    import time

    from evolution.tree.roles.workspace import open_attempt_log

    reflector, instances = _reflector(
        tmp_path,
        [_writes("bad"), _writes("bad")],
        usage=[{"tool_turns": 3}, {"tool_turns": 2}],
        permission="unsafe",
        reasoning_effort="high",
    )
    monkeypatch.setattr(time, "monotonic", lambda: 105.0)
    with open_attempt_log(reflector.log_root, "calls", secrets=reflector._log_secrets()) as log:
        assert (
            await reflector._call(
                reflect_request, reflect_request.workspace, log, 100.0, call=1, prompt="opening"
            )
            == 3
        )
        assert (
            await reflector._call(
                reflect_request, reflect_request.workspace, log, 102.0, call=2, prompt="repair"
            )
            == 2
        )
    assert len(instances) == 2
    assert instances[0] is not instances[1]
    for backend, remaining in zip(instances, (25.0, 27.0), strict=True):
        assert backend.kwargs == {
            "model": "codex",
            "agent_permission": "unsafe",
            "reasoning_effort": "high",
            "timeout_s": remaining,
            "sandbox": None,
            "agent_bin": str(reflector.program.path),
            "env": CHILD_ENV,
            "explicit": True,
        }
        assert backend.timeout_s == remaining
        assert backend.system == TREE_AGENT_REFLECTOR_SYSTEM
        assert backend.work_dir == reflect_request.workspace
    lines = _log_lines(reflector, "calls")
    assert [line["phase"] for line in lines] == ["request", "response", "request", "response"]
    assert lines[0] == {
        "phase": "request",
        "call": 1,
        "model": "codex",
        "system_sha256": reflector.prompt.sha256,
        "opening": "opening",
        "timeout_s": 25.0,
    }
    assert lines[1] == {
        "phase": "response",
        "call": 1,
        "content": "diagnosed",
        "usage": {"tool_turns": 3},
        "diagnostics": {"last_message": "done"},
        "tool_turns": 3,
    }


@pytest.mark.parametrize(
    "reported,expected", [(None, 1), (0, 1), (-2, 1), ("4", 1), (4, 4), (True, True)]
)
async def test_call_preserves_turn_telemetry_defaults(
    tmp_path, reflect_request, reported, expected
):
    reflector, _ = _reflector(
        tmp_path, [_writes(_result("declined", []))], usage=[{"tool_turns": reported}]
    )
    assert (await reflector.reflect(reflect_request)).status == "declined"
    response = next(line for line in _log_lines(reflector) if line["phase"] == "response")
    assert response["tool_turns"] == expected


async def test_prompt_is_verified_before_repair(tmp_path, reflect_request):
    from dataclasses import replace

    path = tmp_path / "prompt.md"
    path.write_text("original")

    def run(root):
        path.write_text("changed")
        _writes("bad")(root)

    reflector, instances = _reflector(tmp_path, [run])
    reflector = replace(reflector, prompt=seal_prompt(path, packaged=""))
    result = await reflector.reflect(reflect_request)
    assert (result.status, result.code, result.note) == (
        "failed",
        "prompt_changed",
        "prompt_changed",
    )
    assert len(instances) == 1
    assert [line["phase"] for line in _log_lines(reflector)] == [
        "request",
        "response",
        "self_check",
        "refused",
    ]


@pytest.mark.parametrize("kind", ["runtime", "timeout", "reader"])
async def test_legacy_call_exception_translation_and_redaction(tmp_path, reflect_request, kind):
    from evolution.llm.agent_backend import AgentBackendError
    from evolution.tree.roles.reflection_reader import ReaderIntegrityError

    message = f"{SECRET} {str(_program(tmp_path))}"
    error = {
        "runtime": RuntimeError(message),
        "timeout": AgentBackendError(message),
        "reader": ReaderIntegrityError(message),
    }[kind]

    def run(root):
        raise error

    reflector, _ = _reflector(tmp_path, [run])
    result = await reflector.reflect(reflect_request)
    assert result.code == "agent_error"
    assert result.note == f"agent_error {type(error).__name__}: {message}"
    lines = _log_lines(reflector)
    refused = [line for line in lines if line["phase"] == "refused"]
    assert refused[0]["error_class"] == "agent_error"
    assert refused[0]["error_type"] == type(error).__name__
    assert refused[0]["diagnostics"] == {"last_message": "done"}
    assert SECRET not in json.dumps(lines)
    assert str(reflector.program.path) not in json.dumps(lines)


async def test_legacy_call_cancellation_propagates_without_refusal(tmp_path, reflect_request):
    import asyncio

    def run(root):
        raise asyncio.CancelledError

    reflector, instances = _reflector(tmp_path, [run])
    with pytest.raises(asyncio.CancelledError):
        await reflector.reflect(reflect_request)
    assert len(instances) == 1
    assert [line["phase"] for line in _log_lines(reflector)] == ["request"]


async def test_backend_factory_failure_retains_outer_exception_route(
    tmp_path, reflect_request, monkeypatch
):
    def build(**kwargs):
        raise RuntimeError("factory broke")

    monkeypatch.setattr("evolution.tree.roles.reflector.agent_reflector.AgentBackend", build)
    from dataclasses import replace

    reflector, instances = _reflector(tmp_path, [lambda root: None])
    reflector = replace(reflector, backend_factory=None)
    result = await reflector.reflect(reflect_request)
    assert (result.code, result.note) == ("exception", "exception:RuntimeError")
    assert instances == []
    assert _log_lines(reflector)[-1] == {
        "phase": "exception",
        "error_type": "RuntimeError",
        "error": "factory broke",
    }


async def test_backend_factory_seal_changed_keeps_outer_exception_route(
    tmp_path, reflect_request, monkeypatch
):
    from dataclasses import replace

    from evolution.tree.roles.seals import SealChanged

    def build(**kwargs):
        raise SealChanged("backend seal")

    monkeypatch.setattr("evolution.tree.roles.reflector.agent_reflector.AgentBackend", build)
    reflector, instances = _reflector(tmp_path, [lambda root: None])
    reflector = replace(reflector, backend_factory=None)
    result = await reflector.reflect(reflect_request)
    assert (result.code, result.note) == ("exception", "exception:SealChanged")
    assert instances == []
    assert _log_lines(reflector)[-1] == {
        "phase": "exception",
        "error_type": "SealChanged",
        "error": "backend seal",
    }


def test_the_v16_self_check_admits_an_empty_extra_key(tmp_path):
    from dataclasses import replace

    from evolution.tree.roles.reflector.agent_reflector import _read_result
    from tests.test_tree_adaptive_reflector import adaptive_fixture, group

    trace, inputs, brief = adaptive_fixture(tmp_path)
    try:
        root = tmp_path / "check"
        (root / CONTROL_PREFIX).mkdir(parents=True)
        raw = {
            **group(evidence_ids=["obs:c:1"], affected_cases=["c"]),
            "suggested_fix_family": None,
        }
        (root / CONTROL_PREFIX / REFLECTION_FILE).write_text(
            json.dumps({"status": "reflected", "note": "n", "groups": [raw]})
        )
        request = ReflectRequest(
            "r1", 1, root, brief, None, None, ("*.md",), 30, normalize_empty_keys=True
        )
        result, issues = _read_result(root, request, [])
        assert issues == () and result.groups == (raw,)
        assert _read_result(root, replace(request, normalize_empty_keys=False), [])[1] != ()
    finally:
        trace.close()
