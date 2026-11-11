"""The family-14 agent Grouper's lazy, operation-scoped control plane."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from evolution.llm.agent_backend import AgentBackendError
from evolution.tree.roles.config import AgentCliEditorSection, AgentCliGrouperSection
from evolution.tree.roles.grouping.agent_grouper import (
    GROUPING_METHODS,
    GROUPS_FILE,
    AgentCliGrouper,
    GroupingControlError,
    read_grouping_output,
    write_grouping_control,
)
from evolution.tree.roles.grouping.reflect_grouping import GroupingRegistry
from evolution.tree.roles.reflection_reader import (
    FrozenReflectionView,
    ReaderArgumentError,
    ReaderIntegrityError,
)
from evolution.tree.roles.role_failures import GrouperRoleFailure, GroupingExecutionError
from evolution.tree.roles.seals import seal_program, seal_prompt
from evolution.tree.roles.workspace import Budgets
from tests.agent_grouping_fakes import (
    TEST_PRIVATE_SENTINEL,
    TEST_PUBLIC_SENTINEL,
    TEST_SENTINEL,
    ScriptedGroupingAgent,
    grouping_inputs,
)
from tests.model_grouping_fakes import grouping_answer, map_group
from tests.tree.roles.grouping.test_model_grouping_runtime import REG


def _section(*, payload_bytes: int = 8192) -> AgentCliGrouperSection:
    role = AgentCliEditorSection(
        model="codex",
        budgets=Budgets(None, 1 << 20, payload_bytes),
        max_turns=5,
    )
    return AgentCliGrouperSection(role=role, timeout_seconds=30)


def _write(tmp_path: Path, inputs, reader):
    root = tmp_path / "projection"
    root.mkdir()
    control = write_grouping_control(
        root,
        registry=inputs.registry,
        reader=reader,
        section=_section(),
        policy_digest="policy-sha",
        opening="Group the failing train observations.",
    )
    return root, control


def test_control_projection_is_lazy_and_sealed(tmp_path: Path, monkeypatch) -> None:
    with grouping_inputs(tmp_path) as inputs:

        def no_body_read(*_args, **_kwargs):
            raise AssertionError("control-plane creation read a source body")

        monkeypatch.setattr(inputs, "_section", no_body_read)
        with inputs.reader_view().open_operation(
            "grouping-1",
            {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"},
        ) as reader:
            root, control = _write(tmp_path, inputs, reader)
            assert {p.name for p in root.iterdir()} == {".evolution"}
            names = {p.name for p in (root / ".evolution").iterdir()}
            assert names == {"request.json", "OPENING.md", "readers"}
            request = json.loads((root / ".evolution/request.json").read_text())
            assert request["version"] == "agent_grouping_request/1"
            assert request["baseline_binding"] == inputs.registry.baseline_binding
            assert request["registry_digest"] == inputs.registry.digest
            assert request["record_count"] == 1
            assert request["policy_digest"] == "policy-sha"
            assert request["limits"]["max_payload_bytes"] == 8192
            assert request["limits"]["max_turns"] == 5
            assert request["limits"]["timeout_seconds"] == 30
            assert request["output"]["path"] == ".evolution/GROUPS.json"
            assert set(request["helpers"]) == {
                "list_grouping_inputs",
                "read_grouping_input",
                "stream_grouping_inputs",
            }
            assert (root / ".evolution/OPENING.md").read_text() == request["opening"]
            assert "secret" not in json.dumps(request)
            assert "oracle" not in json.dumps(request)
            source = (tmp_path / "partition.jsonl").read_text()
            for sentinel in (TEST_SENTINEL, TEST_PUBLIC_SENTINEL, TEST_PRIVATE_SENTINEL):
                assert sentinel in source
                assert sentinel not in json.dumps(request)
                assert sentinel not in (root / ".evolution/OPENING.md").read_text()
            assert not (root / ".evolution/REFLECT_REQUEST.json").exists()
            assert not (root / ".evolution/GROUPS.json").exists()
            for path in control.sealed_paths:
                assert stat.S_ISREG(path.lstat().st_mode)
                assert not path.stat().st_mode & 0o222
            control.verify()
            (root / ".evolution/GROUPS.json").write_text('{"groups": []}')
            (root / ".evolution/VALIDATION.json").write_text('{"issues": []}')
            control.verify()


def _launch(argv, arguments):
    return subprocess.run(
        [*argv, json.dumps(arguments)], capture_output=True, text=True, check=False, timeout=10
    )


def test_real_loopback_launchers_page_chunk_stream_train_only(tmp_path: Path) -> None:
    with grouping_inputs(tmp_path, samples=129, turns=8) as inputs:
        with inputs.reader_view().open_operation(
            "grouping-2",
            {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"},
        ) as reader:
            _root, control = _write(tmp_path, inputs, reader)
            commands = json.loads(control.request_path.read_text())["helpers"]
            page = json.loads(_launch(commands["list_grouping_inputs"], {"offset": 0}).stdout)
            assert page["total"] == 129
            assert len(page["entries"]) == 128
            assert page["entries"][0]["evidence_id"].startswith("obs:c:")
            for sentinel in (TEST_SENTINEL, TEST_PUBLIC_SENTINEL, TEST_PRIVATE_SENTINEL):
                assert sentinel in (tmp_path / "partition.jsonl").read_text()
                assert sentinel not in json.dumps(page)
            next_page = json.loads(
                _launch(commands["list_grouping_inputs"], {"offset": page["next_offset"]}).stdout
            )
            assert len(next_page["entries"]) == 1
            assert next_page["next_offset"] is None
            for section in (
                "identity",
                "metadata",
                "public",
                "observation",
                "feedback",
                "diagnostics",
            ):
                result = _launch(
                    commands["read_grouping_input"], {"index": 0, "section": section, "offset": 0}
                )
                assert result.returncode == 0, result.stdout + result.stderr
                chunk = json.loads(result.stdout)
                assert chunk["content_digest"]
                assert chunk["total_characters"] >= len(chunk["content"])
                for sentinel in (TEST_SENTINEL, TEST_PUBLIC_SENTINEL, TEST_PRIVATE_SENTINEL):
                    assert sentinel not in chunk["content"]
                if section == "observation":
                    assert chunk["next_offset"] is not None
                    second = json.loads(
                        _launch(
                            commands["read_grouping_input"],
                            {"index": 0, "section": section, "offset": chunk["next_offset"]},
                        ).stdout
                    )
                    assert second["offset"] == chunk["next_offset"]
                    assert second["content_digest"] == chunk["content_digest"]
            stream = _launch(
                commands["stream_grouping_inputs"],
                {
                    "start": 0,
                    "stop": 1,
                    "sections": [
                        "identity",
                        "metadata",
                        "public",
                        "observation",
                        "feedback",
                        "diagnostics",
                    ],
                },
            )
            assert stream.returncode == 0, stream.stdout + stream.stderr
            frames = [json.loads(line) for line in stream.stdout.splitlines()]
            assert frames[0]["kind"] == "header"
            assert frames[-1]["kind"] == "final"
            assert {
                f["value"]["section"]
                for f in frames
                if f["kind"] == "data" and f["value"]["kind"] == "data"
            } == {"identity", "metadata", "public", "observation", "feedback", "diagnostics"}
            for sentinel in (TEST_SENTINEL, TEST_PUBLIC_SENTINEL, TEST_PRIVATE_SENTINEL):
                assert sentinel not in stream.stdout
            with pytest.raises(ReaderArgumentError, match="unknown_operation"):
                reader.dispatch("list_evidence", {})
            control.verify()
        expired = _launch(commands["list_grouping_inputs"], {"offset": 0})
        assert expired.returncode != 0
        assert json.loads(expired.stdout)["error"]["code"] in {
            "reader_unavailable",
            "invalid_capability",
            "transport_failed",
        }


def test_collisions_and_changed_stable_inputs(tmp_path: Path) -> None:
    with grouping_inputs(tmp_path) as inputs:
        with inputs.reader_view().open_operation(
            "grouping-3", {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"}
        ) as reader:
            root, control = _write(tmp_path, inputs, reader)
            original_request = control.request_path.read_bytes()
            control.request_path.chmod(0o600)
            control.request_path.write_text("changed")
            with pytest.raises(GroupingControlError, match="changed_seal"):
                control.verify()
            with pytest.raises(GroupingControlError, match="control_collision"):
                write_grouping_control(
                    root,
                    registry=inputs.registry,
                    reader=reader,
                    section=_section(),
                    policy_digest="policy-sha",
                    opening="opening",
                )
            control.request_path.write_bytes(original_request)
            control.request_path.chmod(0o400)
            helper = root / ".evolution/readers/list_grouping_inputs.py"
            helper.chmod(0o600)
            helper.write_text("changed")
            with pytest.raises(ReaderIntegrityError, match="helper_changed"):
                control.verify()


@pytest.mark.parametrize("entry", ["top_level", "control_file", "control_symlink"])
def test_preexisting_projection_entries_are_left_intact(tmp_path: Path, entry: str) -> None:
    root = tmp_path / "projection"
    root.mkdir()
    source = tmp_path / "source"
    source.write_text("original", encoding="utf-8")
    if entry == "top_level":
        (root / "seed.txt").write_text("original", encoding="utf-8")
    elif entry == "control_file":
        control = root / ".evolution"
        control.mkdir()
        (control / "request.json").write_text("original", encoding="utf-8")
    else:
        (root / ".evolution").symlink_to(source)
    methods = {
        name: lambda _args: {}
        for name in ("list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs")
    }
    with FrozenReflectionView(methods).open_operation("collision", set(methods)) as reader:
        with pytest.raises(GroupingControlError, match="control_collision"):
            write_grouping_control(
                root,
                registry=GroupingRegistry("baseline", (), "registry"),
                reader=reader,
                section=_section(),
                policy_digest="policy",
                opening="opening",
            )
    assert source.read_text() == "original"
    if entry == "top_level":
        assert (root / "seed.txt").read_text() == "original"
    elif entry == "control_file":
        assert (root / ".evolution/request.json").read_text() == "original"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_output_reader_refuses_nonregular_without_following(tmp_path: Path, kind: str) -> None:
    control = tmp_path / ".evolution"
    control.mkdir()
    output = control / GROUPS_FILE
    source = tmp_path / "source"
    source.write_text('{"groups":[]}', encoding="utf-8")
    if kind == "symlink":
        output.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(output)
    else:
        output.mkdir()
    with pytest.raises(GroupingControlError, match="control_collision"):
        read_grouping_output(tmp_path, max_payload_bytes=16)
    assert source.read_text() == '{"groups":[]}'


def test_output_reader_bounds_and_reports_content_issues(tmp_path: Path) -> None:
    control = tmp_path / ".evolution"
    control.mkdir()
    assert read_grouping_output(tmp_path, max_payload_bytes=8) == (
        None,
        (".evolution/GROUPS.json is missing",),
    )
    output = control / GROUPS_FILE
    output.write_bytes(b"\xff")
    assert read_grouping_output(tmp_path, max_payload_bytes=8) == (
        None,
        (".evolution/GROUPS.json is not UTF-8 text",),
    )
    output.write_bytes(b"x" * 9)
    with pytest.raises(GroupingControlError, match="output_too_large"):
        read_grouping_output(tmp_path, max_payload_bytes=8)
    output.write_text("{}")
    assert read_grouping_output(tmp_path, max_payload_bytes=8) == ("{}", ())


def test_output_storage_error_is_not_a_repair_issue(tmp_path: Path, monkeypatch) -> None:
    control = tmp_path / ".evolution"
    control.mkdir()
    (control / GROUPS_FILE).write_text("{}")

    def failed_read(*_args):
        raise OSError(errno.EIO, "injected storage error")

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", failed_read)
        with pytest.raises(OSError) as error:
            read_grouping_output(tmp_path, max_payload_bytes=8)
    assert error.value.errno == errno.EIO


# Runtime tests use a tiny registry, real control files and the real mediator.
# The lazy-source body tests above cover the campaign's genuine frozen reader.

VALID_GROUPS = grouping_answer(map_group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")]))


@contextmanager
def runtime_case(tmp_path, script, *, turns=None, max_turns=5, verify=None, **kwargs):
    program = tmp_path / "agent"
    program.write_text("executable")
    prompt = tmp_path / "prompt"
    prompt.write_text("Group failures; never propose repairs.")
    root = tmp_path / "runtime"
    root.mkdir()
    fake = ScriptedGroupingAgent(script, turns=turns)
    section = _section()
    section = replace(section, role=replace(section.role, max_turns=max_turns))
    agent = AgentCliGrouper(
        section=section,
        program=seal_program(program),
        prompt=seal_prompt(prompt, packaged=""),
        child_env={"ONLY_THIS": "value"},
        log_root=tmp_path / "logs",
        secrets=("RUN-SECRET",),
        backend_factory=fake.factory,
    )
    handlers = {name: lambda _args: {} for name in GROUPING_METHODS}
    with FrozenReflectionView(handlers).open_operation("runtime", set(handlers)) as reader:
        arguments = dict(
            reader=reader,
            control_root=root,
            operation="runtime",
            policy_identity="policy",
            parent_digest="parent",
            verify_admission=verify or (lambda: None),
            cancelled=lambda: False,
            forbidden_secret=lambda text: "RUN-SECRET" in text,
            oracle_needle=lambda text: "ORACLE" in text,
        )
        arguments.update(kwargs)
        yield agent, fake, arguments


@pytest.mark.asyncio
async def test_agent_valid_output_one_call(tmp_path):
    with runtime_case(tmp_path, [VALID_GROUPS]) as (agent, fake, args):
        result = await agent.run(REG, **args)
        assert result.fallback is None
        assert result.calls == {"agent_calls": 1, "repairs": 0, "turns": 1}
        assert len(fake.calls) == 1
        assert fake.factories[0]["env"] == {"ONLY_THIS": "value"}
        assert fake.factories[0]["sandbox"] is None
        assert result.grouped.result.assignments[2] is None  # passing observation


@pytest.mark.parametrize(
    "initial", [None, "{", '{"groups":[]}', grouping_answer(map_group("partial", [("a#1", "v")]))]
)
@pytest.mark.asyncio
async def test_agent_one_repair_parses_fresh(tmp_path, initial):
    def repair(root):
        validation = json.loads((root / ".evolution/VALIDATION.json").read_text())
        assert validation["issues"]
        assert len(json.dumps(validation)) < 8192
        return VALID_GROUPS

    with runtime_case(tmp_path, [initial, repair], turns=[2, 3]) as (agent, fake, args):
        result = await agent.run(REG, **args)
        assert result.fallback is None
        assert result.calls == {"agent_calls": 2, "repairs": 1, "turns": 5}
        assert "VALIDATION.json" in fake.calls[1][1]
        assert len(result.grouped.neighborhoods) == 1


@pytest.mark.parametrize(
    "answer", ['{"groups":[]}', grouping_answer(map_group("bad", [("a#3", "v")]))]
)
@pytest.mark.asyncio
async def test_agent_all_rejected_is_full_fallback(tmp_path, answer):
    with runtime_case(tmp_path, [answer, answer]) as (agent, _fake, args):
        result = await agent.run(REG, **args)
        assert result.fallback.scope == "all"
        assert result.fallback.reason == "invalid_output"
        assert result.fallback.kind == "singletons"
        assert len(result.grouped.neighborhoods) == 3


@pytest.mark.asyncio
async def test_agent_partial_groups_and_overlap_survive_repair(tmp_path):
    partial = grouping_answer(
        map_group("g1", [("a#1", "v")]), map_group("g2", [("a#1", "v"), ("a#2", "v")])
    )
    with runtime_case(tmp_path, [partial, partial]) as (agent, _fake, args):
        result = await agent.run(REG, **args)
        assert result.fallback.scope == "uncovered"
        assert result.fallback.reason == "invalid_output"
        assert len(result.grouped.result.assignments) == 4
        assert result.grouped.result.assignments[2] is None


@pytest.mark.parametrize(
    "turns,answer,expected",
    [
        (5, VALID_GROUPS, None),
        (6, VALID_GROUPS, "turns_exhausted"),
        (5, '{"groups":[]}', "turns_exhausted"),
        (5, "{", "turns_exhausted"),
        (
            5,
            grouping_answer(map_group("partial", [("a#1", "v"), ("a#2", "v")])),
            "turns_exhausted",
        ),
    ],
)
@pytest.mark.asyncio
async def test_agent_turn_acceptance_boundary_and_no_spent_repair(
    tmp_path, turns, answer, expected
):
    with runtime_case(tmp_path, [answer], turns=[turns]) as (agent, fake, args):
        result = await agent.run(REG, **args)
        assert (result.fallback.reason if result.fallback else None) == expected
        assert len(fake.calls) == 1
        assert result.calls == {"agent_calls": 1, "repairs": 0, "turns": turns}
        assert not (args["control_root"] / ".evolution/VALIDATION.json").exists()
        if expected is not None:
            assert result.fallback.scope == "all"
            assert {frozenset(n.member_ids) for n in result.grouped.neighborhoods} == {
                frozenset({"a#1"}),
                frozenset({"a#2"}),
                frozenset({"b#1"}),
            }


@pytest.mark.parametrize(
    "failure,reason",
    [
        (AgentBackendError("unsafe message"), "agent_error"),
        (AgentBackendError("unsafe", timed_out=True), "timeout"),
        (TimeoutError("unsafe"), "timeout"),
        (RuntimeError("unsafe message"), "exception"),
    ],
)
@pytest.mark.asyncio
async def test_agent_unknown_failure_closed_reason(tmp_path, failure, reason):
    with runtime_case(tmp_path, [failure]) as (agent, fake, args):
        with pytest.raises(GrouperRoleFailure) as caught:
            await agent.run(REG, **args)
        assert caught.value.reason == reason
        assert caught.value.error_class == "unknown"
        assert len(fake.calls) == 1
    logged = (tmp_path / "logs/grouping-runtime.jsonl").read_text()
    assert "unsafe" not in logged
    if reason == "exception":
        assert "RuntimeError" in logged


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("malformed"),
        TypeError("malformed"),
        KeyError("malformed"),
        ReaderIntegrityError("damaged"),
        GroupingExecutionError("invalid_result"),
    ],
)
@pytest.mark.asyncio
async def test_agent_existing_fault_routes_survive(tmp_path, failure):
    with runtime_case(tmp_path, [failure]) as (agent, _fake, args):
        with pytest.raises(type(failure)) as caught:
            await agent.run(REG, **args)
        assert caught.value is failure


@pytest.mark.parametrize("failed_call", [False, True])
@pytest.mark.parametrize(
    "damage,code",
    [
        ("prompt", "command_changed"),
        ("agent", "command_changed"),
        ("request", "command_changed"),
        ("helper", "reader_integrity"),
        ("secret", "secret_detected"),
        ("oversize", "result_too_large"),
        ("symlink", "helper_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_agent_integrity_beats_success_and_unknown_failure(
    tmp_path, failed_call, damage, code
):
    def action(root):
        if damage in {"prompt", "agent"}:
            (tmp_path / damage).write_text("changed")
        elif damage in {"request", "helper"}:
            target = root / (
                ".evolution/request.json"
                if damage == "request"
                else ".evolution/readers/list_grouping_inputs.py"
            )
            target.chmod(0o600)
            target.write_text("changed")
        else:
            output = root / ".evolution/GROUPS.json"
            if damage == "secret":
                output.write_text('{"discarded": "RUN-SECRET", "groups": []}')
            elif damage == "oversize":
                output.write_text("x" * 8193)
            else:
                output.symlink_to(tmp_path / "prompt")
        return AgentBackendError("failed") if failed_call else None

    with runtime_case(tmp_path, [action]) as (agent, _fake, args):
        exception = ReaderIntegrityError if damage == "helper" else GroupingExecutionError
        with pytest.raises(exception) as caught:
            await agent.run(REG, **args)
        if damage != "helper":
            assert caught.value.code == code


@pytest.mark.parametrize(
    "where", ["raw_invalid", "json_escaped", "escaped_duplicate", "escaped_malformed", "transcript"]
)
@pytest.mark.asyncio
async def test_agent_secret_refusal_before_discard_and_logging(tmp_path, where):
    answer = '{"discarded":"RUN-SECRET","groups":[]}'
    if where.startswith("escaped") or where == "json_escaped":
        answer = answer.replace("RUN-SECRET", r"RUN-\u0053ECRET")
    if where == "escaped_duplicate":
        answer = answer[:-1] + ',"discarded":"safe"}'
    elif where == "escaped_malformed":
        answer += " trailing garbage"
    with runtime_case(tmp_path, [answer]) as (agent, fake, args):
        if where == "transcript":
            fake.script = [VALID_GROUPS]
            fake.content = "RUN-SECRET"
        with pytest.raises(GroupingExecutionError, match="secret_detected"):
            await agent.run(REG, **args)
        assert len(fake.calls) == 1
    assert "RUN-SECRET" not in (tmp_path / "logs/grouping-runtime.jsonl").read_text()


@pytest.mark.asyncio
async def test_agent_log_redacts_path_capability_endpoint(tmp_path):
    def action(root):
        session = json.loads((root / ".evolution/readers/session.json").read_text())
        fake.content = f"{tmp_path / 'agent'} {session['capability']} {session['endpoint']}"
        return VALID_GROUPS

    with runtime_case(tmp_path, [action]) as (agent, fake, args):
        await agent.run(REG, **args)
        runtime_secrets = args["reader"].client_descriptor()
    logged = (tmp_path / "logs/grouping-runtime.jsonl").read_text()
    assert str(tmp_path / "agent") not in logged
    assert runtime_secrets.capability not in logged
    assert runtime_secrets.url not in logged


@pytest.mark.parametrize("failed_call", [False, True])
@pytest.mark.asyncio
async def test_agent_frozen_admission_failure_wins(tmp_path, failed_call):
    damaged = False

    def verify():
        if damaged:
            raise ReaderIntegrityError("changed_frozen_source")

    def action(_root):
        nonlocal damaged
        damaged = True
        return AgentBackendError("failed") if failed_call else VALID_GROUPS

    with runtime_case(tmp_path, [action], verify=verify) as (agent, _fake, args):
        with pytest.raises(ReaderIntegrityError, match="changed_frozen_source"):
            await agent.run(REG, **args)
        assert args["reader"].integrity_fault == "changed_frozen_source"


@pytest.mark.parametrize("failed_call", [False, True])
@pytest.mark.asyncio
async def test_agent_sticky_reader_fault_wins(tmp_path, failed_call):
    def action(_root):
        def fault():
            raise ReaderIntegrityError("sticky_source")

        try:
            args["reader"].verify_admission(fault)
        except ReaderIntegrityError:
            pass  # the agent ignored the reader error
        return AgentBackendError("failed") if failed_call else VALID_GROUPS

    with runtime_case(tmp_path, [action]) as (agent, _fake, args):
        with pytest.raises(ReaderIntegrityError, match="sticky_source"):
            await agent.run(REG, **args)


@pytest.mark.parametrize("phase", ["open", "request", "response", "close", "validation", "output"])
@pytest.mark.asyncio
async def test_agent_storage_faults_keep_original_route(tmp_path, monkeypatch, phase):
    import evolution.tree.roles.grouping.agent_grouper as module
    from evolution.tree.roles.workspace import AttemptLog

    fault = OSError(errno.ENOSPC, "disk full")

    def fail(*_args, **_kwargs):
        raise fault

    with runtime_case(tmp_path, ['{"groups":[]}', VALID_GROUPS]) as (agent, fake, args):
        if phase == "open":
            monkeypatch.setattr(module, "open_attempt_log", fail)
        elif phase in {"request", "response"}:
            original = AttemptLog.record

            def record(self, value):
                if value["phase"] == phase:
                    raise fault
                original(self, value)

            monkeypatch.setattr(AttemptLog, "record", record)
        elif phase == "close":
            original_close = AttemptLog.close

            def close(self):
                original_close(self)
                raise fault

            monkeypatch.setattr(AttemptLog, "close", close)
        elif phase == "validation":
            monkeypatch.setattr(module, "_write_validation", fail)
        else:
            monkeypatch.setattr(module, "read_grouping_output", fail)
        with pytest.raises(OSError) as caught:
            await agent.run(REG, **args)
        assert caught.value is fault
        assert len(fake.calls) <= (2 if phase == "close" else 1)


@pytest.mark.asyncio
async def test_agent_cancellation_propagates_and_never_repairs(tmp_path):
    import asyncio

    with runtime_case(tmp_path, [asyncio.CancelledError()]) as (agent, fake, args):
        with pytest.raises(asyncio.CancelledError):
            await agent.run(REG, **args)
        assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_agent_cancellation_flag_during_failed_call_is_existing_route(tmp_path):
    stop = False

    def action(_root):
        nonlocal stop
        stop = True
        return AgentBackendError("failed")

    with runtime_case(tmp_path, [action], cancelled=lambda: stop) as (agent, fake, args):
        with pytest.raises(GroupingExecutionError, match="cancelled"):
            await agent.run(REG, **args)
        assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_agent_deadline_spent_before_repair(tmp_path, monkeypatch):
    import evolution.tree.roles.grouping.agent_grouper as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def action(_root):
        clock[0] = 31.0
        return '{"groups":[]}'

    with runtime_case(tmp_path, [action]) as (agent, fake, args):
        with pytest.raises(GrouperRoleFailure) as caught:
            await agent.run(REG, **args)
        assert caught.value.reason == "timeout"
        assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_agent_second_call_turns_override_usable_groups(tmp_path):
    with runtime_case(tmp_path, ["{", VALID_GROUPS], turns=[2, 4]) as (agent, _fake, args):
        result = await agent.run(REG, **args)
        assert result.calls == {"agent_calls": 2, "repairs": 1, "turns": 6}
        assert result.fallback.scope == "all"
        assert result.fallback.reason == "turns_exhausted"
        assert len(result.grouped.neighborhoods) == 3


@pytest.mark.parametrize("failure", [None, AgentBackendError("failure")])
@pytest.mark.asyncio
async def test_agent_second_call_integrity_is_checked(tmp_path, failure):
    def repair(_root):
        (tmp_path / "prompt").write_text("changed before repair settled")
        return failure or VALID_GROUPS

    with runtime_case(tmp_path, ["{", repair]) as (agent, fake, args):
        with pytest.raises(GroupingExecutionError, match="command_changed"):
            await agent.run(REG, **args)
        assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_agent_validation_defects_are_parser_bounded_safe(tmp_path):
    from evolution.tree.roles.grouping.model_grouping import parse_map_response

    bad = json.dumps(
        {"groups": [{f"unsafe\nkey{index}": "arbitrary prose"} for index in range(100)]}
    )
    expected = parse_map_response(
        bad,
        chunk_failing=["a#1", "a#2", "b#1"],
        case_of={r.evidence_id: r.case_id for r in REG.records},
        provenance_available=False,
    )

    def repair(root):
        validation = (root / ".evolution/VALIDATION.json").read_text()
        assert json.loads(validation)["issues"] == list(expected.defects)
        assert len(json.loads(validation)["issues"]) <= 32
        assert "arbitrary prose" not in validation
        assert "unsafe" not in validation
        return VALID_GROUPS

    with runtime_case(tmp_path, [bad, repair]) as (agent, _fake, args):
        result = await agent.run(REG, **args)
        assert result.fallback is None


@pytest.mark.parametrize("entry", ["directory", "symlink"])
@pytest.mark.asyncio
async def test_agent_mutable_validation_collision_does_not_touch_link_target(tmp_path, entry):
    target = tmp_path / "valuable"
    target.write_text("keep")

    def first(root):
        validation = root / ".evolution/VALIDATION.json"
        if entry == "directory":
            validation.mkdir()
        else:
            validation.symlink_to(target)
        return "{"

    with runtime_case(tmp_path, [first, VALID_GROUPS]) as (agent, fake, args):
        if entry == "directory":
            with pytest.raises(GroupingExecutionError, match="helper_unavailable"):
                await agent.run(REG, **args)
            assert len(fake.calls) == 1
        else:
            result = await agent.run(REG, **args)
            assert result.fallback is None
            assert len(fake.calls) == 2
    assert target.read_text() == "keep"


@pytest.mark.asyncio
async def test_agent_fields_fallback_is_only_for_uncovered(tmp_path):
    from evolution.tree.roles.config import FieldsGroupingSection
    from evolution.tree.roles.grouping.reflect_grouping import FieldFeature

    partial = grouping_answer(map_group("g", [("a#1", "v")]))
    with runtime_case(tmp_path, [partial, partial]) as (agent, _fake, args):
        fallback = FieldsGroupingSection(
            (FieldFeature("terminal", "observation", "/terminal", "status"),)
        )
        agent = replace(agent, section=replace(agent.section, fallback=fallback))
        result = await agent.run(REG, **args)
        assert result.fallback.kind == "fields"
        assert result.fallback.scope == "uncovered"
        assert {frozenset(n.member_ids) for n in result.grouped.neighborhoods} == {
            frozenset({"a#1"}),
            frozenset({"a#2", "b#1"}),
        }


@pytest.mark.asyncio
async def test_agent_unavailable_observations_never_join_fallback(tmp_path):
    from evolution.tree.roles.grouping.reflect_grouping import build_registry

    registry = build_registry(
        REG.baseline_binding,
        [replace(row, available=False) if row.evidence_id == "b#1" else row for row in REG.records],
    )
    with runtime_case(tmp_path, ['{"groups":[]}', '{"groups":[]}']) as (agent, _fake, args):
        result = await agent.run(registry, **args)
        assert result.grouped.result.assignments[2:] == (None, None)
        assert {member for n in result.grouped.neighborhoods for member in n.member_ids} == {
            "a#1",
            "a#2",
        }


@pytest.mark.asyncio
async def test_agent_canonical_identity_and_oracle_scrubbing(tmp_path):
    other = grouping_answer(
        map_group("other-local-id", [("b#1", "ORACLE"), ("a#2", "v"), ("a#1", "v")], title="ORACLE")
    )
    results = []
    for index, answer in enumerate((VALID_GROUPS, other)):
        folder = tmp_path / str(index)
        folder.mkdir()
        with runtime_case(folder, [answer]) as (agent, _fake, args):
            results.append(await agent.run(REG, **args))
    assert (
        results[0].grouped.neighborhoods[0].group_id == results[1].grouped.neighborhoods[0].group_id
    )
    assert results[1].fallback is None
    assert results[1].grouped.neighborhoods[0].description is None
    assert "ORACLE" not in str(results[1].grouped.result)


@pytest.mark.parametrize("kind", ["descriptions", "variants"])
@pytest.mark.asyncio
async def test_agent_shared_admission_caps_large_output(tmp_path, kind):
    from tests.tree.roles.grouping.test_model_grouping_runtime import _large_registry, _long

    if kind == "descriptions":
        registry = _large_registry(100, 1)
        answer = grouping_answer(
            *(
                map_group(
                    f"g{case}",
                    [(f"c{case:03d}#1", "variant")],
                    title=_long(128, f"Группа {case}"),
                    failure_mode=_long(512, "fm"),
                    expected_behaviour=_long(512, "eb"),
                    question=_long(512, "q"),
                )
                for case in range(100)
            )
        )
    else:
        registry = _large_registry(40, 16)
        answer = grouping_answer(
            *(
                map_group(
                    f"g{case}",
                    [
                        (f"c{case:03d}#{repeat}", _long(64, f"вариант {repeat}"))
                        for repeat in range(1, 17)
                    ],
                )
                for case in range(40)
            )
        )
    with runtime_case(tmp_path, [answer]) as (agent, _fake, args):
        role = replace(agent.section.role, budgets=Budgets(None, 1 << 20, 1 << 20))
        agent = replace(agent, section=replace(agent.section, role=role))
        result = await agent.run(registry, **args)
        assert result.fallback is None
        if kind == "descriptions":
            described = [n for n in result.grouped.neighborhoods if n.description is not None]
            assert 0 < len(described) < 100
        else:
            assert len(result.grouped.variants) == 640
            assert any(len(label) <= 12 for label in result.grouped.variants.values())
        assert {m for n in result.grouped.neighborhoods for m in n.member_ids} == {
            r.evidence_id for r in registry.records
        }
    assert "grouping_trimmed" in (tmp_path / "logs/grouping-runtime.jsonl").read_text()


@pytest.mark.parametrize("reported", [None, -1, 0, 1.5, True, 2])
@pytest.mark.asyncio
async def test_agent_counts_are_nonnegative_integers(tmp_path, reported):
    with runtime_case(tmp_path, [VALID_GROUPS], turns=[reported]) as (agent, _fake, args):
        result = await agent.run(REG, **args)
        assert result.calls == {"agent_calls": 1, "repairs": 0, "turns": 2 if reported == 2 else 1}
        assert all(type(count) is int and count >= 0 for count in result.calls.values())


@pytest.mark.asyncio
async def test_agent_no_failing_available_observations_skips_calls(tmp_path):
    from evolution.tree.roles.grouping.reflect_grouping import build_registry

    registry = build_registry(
        REG.baseline_binding, [replace(row, failing=False) for row in REG.records]
    )
    with runtime_case(tmp_path, []) as (agent, fake, args):
        result = await agent.run(registry, **args)
        assert result.calls == {"agent_calls": 0, "repairs": 0, "turns": 0}
        assert result.fallback is None
        assert not fake.calls


@pytest.mark.asyncio
async def test_agent_admission_does_not_swallow_log_value_error(tmp_path, monkeypatch):
    from evolution.tree.roles.workspace import AttemptLog
    from tests.tree.roles.grouping.test_model_grouping_runtime import _large_registry, _long

    fault = ValueError("write to closed log")
    original = AttemptLog.record

    def record(self, value):
        if value["phase"] == "grouping_trimmed":
            raise fault
        original(self, value)

    monkeypatch.setattr(AttemptLog, "record", record)
    registry = _large_registry(100, 1)
    answer = grouping_answer(
        *(
            map_group(
                f"g{case}",
                [(f"c{case:03d}#1", "v")],
                title=_long(128, "группа"),
                failure_mode=_long(512, "fm"),
                expected_behaviour=_long(512, "eb"),
                question=_long(512, "q"),
            )
            for case in range(100)
        )
    )
    with runtime_case(tmp_path, [answer]) as (agent, _fake, args):
        role = replace(agent.section.role, budgets=Budgets(None, 1 << 20, 1 << 20))
        agent = replace(agent, section=replace(agent.section, role=role))
        with pytest.raises(ValueError) as caught:
            await agent.run(registry, **args)
        assert caught.value is fault


@pytest.mark.parametrize("path", ["output", "transcript"])
def test_agent_secret_scan_unterminated_escaped_quotes_is_bounded(tmp_path, path):
    # A near-default-limit payload used to keep re.finditer rescanning suffixes
    # for hours. A subprocess deadline bounds a regression without hanging pytest.
    program = r"""import io, json, sys
from pathlib import Path
from evolution.tree.roles.grouping.agent_grouper import _CallLog, _refuse_secret, read_grouping_output
root, path = Path(sys.argv[1]), sys.argv[2]
text = '"' + '\\"' * 524287
assert len(text.encode()) == (1 << 20) - 1
if path == "output":
    (root / ".evolution").mkdir()
    (root / ".evolution/GROUPS.json").write_text(text)
    value, issues = read_grouping_output(root, max_payload_bytes=1 << 20)
    assert value == text and not issues
    _refuse_secret(value, lambda value: "RUN-SECRET" in value)
else:
    handle = io.BytesIO()
    log = _CallLog(root / "log", handle, (), forbidden_secret=lambda value: "RUN-SECRET" in value)
    log.record({"phase": "response", "content": text})
    assert json.loads(handle.getvalue())["content"] == text
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), path],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "text",
    [
        r'```json {"discarded":"RUN-\u0053ECRET"} ```',
        r'{"bad":"\q", "discarded":"RUN-\u0053ECRET"} trailing',
        r'{"bad":"\uZZZZ", "discarded":"RUN-\u0053ECRET", "discarded":"safe"}',
        r'{"discarded":"escaped quote \" RUN-\u0053ECRET"}',
        r'{"discarded":"backslash \\ RUN-\u0053ECRET"}',
        '"\\\n' + r'"RUN-\u0053ECRET"',
        '"unterminated RUN-SECRET',
    ],
)
def test_agent_secret_scan_keeps_independent_tokens_and_raw_check(text):
    from evolution.tree.roles.grouping.agent_grouper import _refuse_secret

    with pytest.raises(GroupingExecutionError, match="secret_detected"):
        _refuse_secret(text, lambda value: "RUN-SECRET" in value)


@pytest.mark.parametrize("entry", ["output_file", "output_directory", "control_readonly"])
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission checks")
@pytest.mark.asyncio
async def test_agent_owned_permissions_have_control_collision_route(tmp_path, entry):
    damaged = None

    def action(root):
        nonlocal damaged
        output = root / ".evolution/GROUPS.json"
        if entry == "output_directory":
            output.mkdir()
        else:
            output.write_text("{")
        damaged = root / ".evolution" if entry == "control_readonly" else output
        damaged.chmod(0o500 if entry == "control_readonly" else 0)

    try:
        with runtime_case(tmp_path, [action]) as (agent, fake, args):
            with pytest.raises(GroupingExecutionError, match="helper_unavailable"):
                await agent.run(REG, **args)
            assert len(fake.calls) == 1
    finally:
        if damaged is not None:
            damaged.chmod(0o700)


@pytest.mark.parametrize("entry", ["GROUPS.json", "VALIDATION.json"])
@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.EIO, errno.ENOSPC])
@pytest.mark.asyncio
async def test_agent_mutable_entry_errno_routes(tmp_path, monkeypatch, entry, error):
    original_open = os.open
    fault = OSError(error, "injected mutable entry error")

    def open_entry(path, flags, *args, **kwargs):
        if path == entry:
            raise fault
        return original_open(path, flags, *args, **kwargs)

    with runtime_case(tmp_path, ["{"]) as (agent, fake, args):
        monkeypatch.setattr(os, "open", open_entry)
        if error in (errno.EACCES, errno.EPERM):
            with pytest.raises(GroupingExecutionError, match="helper_unavailable"):
                await agent.run(REG, **args)
        else:
            with pytest.raises(OSError) as caught:
                await agent.run(REG, **args)
            assert caught.value is fault
        assert len(fake.calls) == 1


@pytest.mark.parametrize("family15", [False, True])
async def test_agent_fields_fallback_descriptions_only_in_family15(tmp_path, family15):
    from evolution.tree.roles.config import FieldsGroupingSection
    from evolution.tree.roles.grouping.reflect_grouping import FieldFeature

    partial = grouping_answer(map_group("g", [("a#1", "v")]))
    with runtime_case(tmp_path, [partial, partial]) as (agent, _fake, args):
        fallback = FieldsGroupingSection(
            (FieldFeature("terminal", "observation", "/terminal", "status"),)
        )
        agent = replace(agent, section=replace(agent.section, fallback=fallback), family15=family15)
        result = await agent.run(REG, **args)
        fields = next(
            n for n in result.grouped.neighborhoods if set(n.member_ids) == {"a#2", "b#1"}
        )
        assert (fields.description is not None) is family15
        if family15:
            assert fields.description.description.endswith(
                "What do they have in common, and why do they fail?"
            )


@pytest.mark.parametrize("family15", [False, True])
async def test_agent_model_group_descriptions_whole_only_in_family15(tmp_path, family15):
    """An `agent_cli` Grouper's model groups go through the same projection
    (ADR-0057 §3.1): a `/16` run carries every field and the whole title; a
    family-14 run keeps today's 512-character cut."""

    from dataclasses import replace

    header = {
        "title": "T" * 128,
        "failure_mode": "F" * 512,
        "expected_behaviour": "E" * 512,
        "question": "Q" * 512,
    }
    partial = grouping_answer(map_group("g", [("a#1", "v")], **header))
    with runtime_case(tmp_path, [partial, partial]) as (agent, _fake, args):
        agent = replace(agent, family15=family15)
        result = await agent.run(REG, **args)
        (described,) = [
            n.description for n in result.grouped.neighborhoods if n.description is not None
        ]
        whole = f"Observed: {'F' * 512}\nExpected: {'E' * 512}\nQuestion: {'Q' * 512}"
        if family15:
            assert described.label == "T" * 128
            assert described.description == whole
        else:
            assert described.description == whole[:512]
            assert described.label == "T" * 128
