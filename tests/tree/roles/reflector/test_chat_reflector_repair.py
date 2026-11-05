"""A `/16` chat Reflector repairs its own result.

`finish_reflection` is checked inside the role by the campaign's admission
closure, here built over a small frozen catalog of two cases (`c` with
`obs:c:1`, `d` with `obs:d:1`). Only the provider is fake: the scripted
client of `tests/tree/roles/reflector/test_chat_reflector.py` (which repeats its script) or
the popping client of `tests/test_tree_chat_role_retry.py`. A semantic
refusal must come back as a value-free field-path correction within the
effective turn limit and without spending protocol repairs; a leak must end
the role with no correction; every other path keeps its existing handling.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evolution.prompts import TREE_REFLECTOR_SYSTEM_V3, TREE_REFLECTOR_SYSTEM_V4
from evolution.tree.roles.editor.chat_editor import MAX_PROTOCOL_REPAIRS, ChatCredentials
from evolution.tree.roles.reflector.chat_reflector import (
    FINISH_REFLECTION_REPAIR_DESCRIPTION,
    ChatReflector,
    reflect_tools,
)
from evolution.tree.roles.reflector_contracts import (
    GroupError,
    ProbeAccess,
    ReflectRequest,
    ReflectResult,
    admit_reflection_result,
)
from evolution.tree.roles.seals import chat_reflector_identity, seal_prompt
from tests.test_tree_adaptive_reflector import adaptive_fixture, group
from tests.test_tree_chat_role_retry import (
    RETRY,
    _ScriptedClient,
    _server_error,
    _unauthorized,
)
from tests.tree.roles.reflector.test_chat_reflector import (
    SECRET,
    _log_lines,
    _reflector,
    _response,
    _section,
)

NEEDLE = "the oracle route answer"
OBSERVATIONS = {"obs:c:1": "c", "obs:d:1": "d"}
VALID = group(evidence_ids=["obs:c:1"], affected_cases=["c"])
CROSS_CASE = group(evidence_ids=["obs:c:1"], affected_cases=["d"])


def admission(
    *,
    needles: tuple[str, ...] = (NEEDLE,),
    probe_seqs: set[int] | None = None,
    normalize_empty_keys: bool = False,
    group_text_chars: int | None = 512,
) -> Any:
    """The campaign's closure over one frozen catalog, as `/16` chat builds it."""

    def admit(result: ReflectResult) -> Any:
        return admit_reflection_result(
            result,
            catalog_ids=OBSERVATIONS,
            observation_ids=OBSERVATIONS,
            failing_evidence_ids={"obs:c:1"},
            case_ids={"c", "d"},
            editable_paths=("*.md",),
            probe_seqs=probe_seqs,
            needles=needles,
            runtime_secrets=(SECRET,),
            leak_first=True,
            group_text_chars=group_text_chars,
            **({"normalize_empty_keys": True} if normalize_empty_keys else {}),
        )

    return admit


class _NoProbe:
    async def request(self, value: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("no probe call is scripted")


@pytest.fixture
def adaptive(tmp_path: Path) -> Iterator[Any]:
    """A request factory over one real adaptive brief and its live reader."""

    trace, inputs, brief = adaptive_fixture(tmp_path)
    try:
        with inputs.reader_view().open_operation(
            "chat-1", {"list_evidence", "read_evidence"}
        ) as reader:

            def build(*, admit: Any = None, probe: ProbeAccess | None = None) -> ReflectRequest:
                return ReflectRequest(
                    "reflect-1",
                    1,
                    tmp_path,
                    brief,
                    None,
                    probe,
                    ("*.md",),
                    30,
                    reader=reader,
                    probe_session=_NoProbe() if probe is not None else None,
                    result_admission=admission() if admit is None else admit,
                )

            build.handle = inputs.case_identity_summary("c")["case_handle"]  # type: ignore[attr-defined]
            yield build
    finally:
        trace.close()


def finish(*groups: dict[str, Any], **extra: Any) -> Any:
    return _response("finish_reflection", {"groups": list(groups), **extra})


def repairing(tmp_path: Path, responses: list[Any], **kw: Any) -> Any:
    kw.setdefault("max_turns", 8)
    return _reflector(tmp_path, responses, bounds_repair=True, result_repair=True, **kw)


def rejected(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in lines if line["phase"] == "finish_reflection_rejected"]


def protocol(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in lines if line["phase"] == "protocol_repair_requested"]


def sent_to_model(client: Any) -> str:
    """Every message after the opening that the model did not write itself:
    the corrections and tool results, where no rejected value may appear."""

    messages = client.calls[-1]["messages"][2:]
    return "\n".join(str(m["content"]) for m in messages if m.get("role") != "assistant")


# -- semantic corrections -----------------------------------------------------------


async def test_a_case_handle_in_affected_cases_is_corrected_in_the_same_dialogue(
    tmp_path: Path, adaptive: Any
) -> None:
    handle = adaptive.handle
    bad = group(evidence_ids=["obs:c:1"], affected_cases=[handle])
    reflector, client = repairing(tmp_path, [_response("list_files"), finish(bad), finish(VALID)])

    result = await reflector.reflect(adaptive())

    assert (result.status, result.groups) == ("reflected", (VALID,))
    assert len(client.calls) == 3
    lines = _log_lines(reflector, "reflect-1")
    (refusal,) = rejected(lines)
    assert refusal["correction"] == "groups[0].affected_cases[0]: expected case_id"
    assert refusal["arguments"]["groups"][0]["affected_cases"] == [handle]
    assert not protocol(lines)
    sent = sent_to_model(client)
    assert "groups[0].affected_cases[0]: expected case_id" in sent
    assert handle not in sent


async def test_an_observation_of_another_case_is_corrected_in_the_same_dialogue(
    tmp_path: Path, adaptive: Any
) -> None:
    reflector, client = repairing(
        tmp_path, [_response("list_files"), finish(CROSS_CASE), finish(VALID)]
    )

    result = await reflector.reflect(adaptive())

    assert (result.status, result.groups) == ("reflected", (VALID,))
    (refusal,) = rejected(_log_lines(reflector, "reflect-1"))
    assert refusal["correction"] == (
        "groups[0].affected_cases[0]: each affected case needs a citation of its own observation"
    )
    sent = sent_to_model(client)
    assert "needs a citation of its own observation" in sent
    # The correction names no valid answer: neither the case nor its observation.
    assert '"d"' not in sent and "obs:d:1" not in sent


async def test_semantic_rejections_never_spend_protocol_repairs(
    tmp_path: Path, adaptive: Any
) -> None:
    """More semantic rejections than `MAX_PROTOCOL_REPAIRS`, then one protocol
    repair that still finds its whole budget, then a valid finish."""

    script = [
        _response("list_files"),
        *[finish(CROSS_CASE)] * (MAX_PROTOCOL_REPAIRS + 2),
        finish(VALID, declined="no"),
        finish(VALID),
    ]
    reflector, client = repairing(tmp_path, script, max_turns=len(script))

    result = await reflector.reflect(adaptive())

    assert result.status == "reflected", result
    lines = _log_lines(reflector, "reflect-1")
    assert len(rejected(lines)) == MAX_PROTOCOL_REPAIRS + 2
    assert [line["repair"] for line in protocol(lines)] == [1]


async def test_malformed_and_oversized_arguments_keep_their_protocol_repair(
    tmp_path: Path, adaptive: Any
) -> None:
    oversized = group(evidence_ids=["obs:c:1"], affected_cases=["c"], root_cause="x" * 513)
    script = [
        _response("list_files"),
        finish(VALID, unexpected=True),
        finish(oversized),
        finish(VALID),
    ]
    reflector, _client = repairing(tmp_path, script)

    result = await reflector.reflect(adaptive())

    assert result.status == "reflected", result
    lines = _log_lines(reflector, "reflect-1")
    repairs = protocol(lines)
    assert [line["repair"] for line in repairs] == [1, 2]
    assert repairs[0]["error"] == "invalid tool arguments"
    assert "root_cause exceeds 512 characters" in repairs[1]["error"]
    (bounds,) = rejected(lines)
    assert bounds["error"] == "root_cause exceeds 512 characters" and "correction" not in bounds


async def test_protocol_repairs_run_out_as_before(tmp_path: Path, adaptive: Any) -> None:
    malformed = finish(VALID, declined="no")
    reflector, _client = repairing(tmp_path, [_response("list_files"), malformed])

    result = await reflector.reflect(adaptive())

    assert (result.status, result.code) == ("failed", "protocol")


# -- the effective turn limit ---------------------------------------------------------


@pytest.mark.parametrize("valid_last", [True, False])
async def test_without_a_probe_the_limit_is_max_turns(
    tmp_path: Path, adaptive: Any, valid_last: bool
) -> None:
    """The preflight is turn 1; every rejected finish uses one more. A valid
    finish on the last turn finishes; a rejected one there is `turns_exhausted`."""

    max_turns = MAX_PROTOCOL_REPAIRS + 4
    last = finish(VALID) if valid_last else finish(CROSS_CASE)
    script = [_response("list_files"), *[finish(CROSS_CASE)] * (max_turns - 2), last]
    reflector, client = repairing(tmp_path, script, max_turns=max_turns)

    result = await reflector.reflect(adaptive())

    assert len(client.calls) == max_turns
    if valid_last:
        assert (result.status, result.groups) == ("reflected", (VALID,))
    else:
        assert (result.status, result.code) == ("failed", "turns_exhausted")
    assert not protocol(_log_lines(reflector, "reflect-1"))


@pytest.mark.parametrize("valid_last", [True, False])
async def test_with_a_probe_the_limit_is_the_probe_ceiling(
    tmp_path: Path, adaptive: Any, valid_last: bool
) -> None:
    """`reflector.max_turns` 3 is below `2 * probe.budget + 8` = 12, which then
    bounds the rejections whether or not the model calls the probe."""

    probe = ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 2, ("GET",), None)
    ceiling = 2 * probe.budget + 8
    last = finish(VALID) if valid_last else finish(CROSS_CASE)
    script = [_response("list_files"), *[finish(CROSS_CASE)] * (ceiling - 2), last]
    reflector, client = repairing(tmp_path, script, max_turns=3)

    result = await reflector.reflect(adaptive(admit=admission(probe_seqs=set()), probe=probe))

    assert reflector.section.max_turns == 3
    assert len(client.calls) == ceiling
    if valid_last:
        assert (result.status, result.groups) == ("reflected", (VALID,))
    else:
        assert (result.status, result.code) == ("failed", "turns_exhausted")


# -- leaks are never repaired ----------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        # A leak beside an oversized group: the bounds repair would echo it.
        (
            {
                "groups": [
                    group(evidence_ids=["obs:c:1"], affected_cases=["c"], root_cause=NEEDLE),
                    group(evidence_ids=["obs:c:1"], affected_cases=["c"], why_this_layer="x" * 600),
                ]
            },
            "oracle_leak",
        ),
        # A leak beside a separate semantic defect.
        ({"groups": [CROSS_CASE, {**VALID, "suggested_fix": f"say {NEEDLE}"}]}, "oracle_leak"),
        # A run secret in a target path beside a semantic defect.
        ({"groups": [{**CROSS_CASE, "target_paths": [f"{SECRET}.md"]}]}, "invalid_result"),
        # A malformed call whose groups leak: screened before the protocol repair.
        ({"groups": [{**VALID, "root_cause": NEEDLE}], "unexpected": True}, "oracle_leak"),
    ],
    ids=["oracle-and-oversized", "oracle-and-semantic", "secret-and-semantic", "malformed"],
)
async def test_a_leak_ends_the_role_without_any_correction(
    tmp_path: Path, adaptive: Any, arguments: dict[str, Any], code: str
) -> None:
    """The role ends with its attempted result and no further turn; the campaign's
    final admission (the same closure) then refuses it as the existing failure."""

    script = [_response("list_files"), _response("finish_reflection", arguments), finish(VALID)]
    reflector, client = repairing(tmp_path, script)
    admit = admission()

    result = await reflector.reflect(adaptive(admit=admit))

    assert len(client.calls) == 2
    assert result.status == "reflected"
    with pytest.raises(GroupError) as caught:
        admit(result)
    assert (caught.value.code, caught.value.repair) == (code, "none")
    lines = _log_lines(reflector, "reflect-1")
    assert not protocol(lines)
    (refusal,) = rejected(lines)
    assert "correction" not in refusal
    log = (Path(reflector.log_root) / "reflect-1.jsonl").read_text()
    assert SECRET not in log


@pytest.mark.parametrize("needle", ["obs:c:1", "SKILL.md"])
async def test_an_identifier_or_path_outside_the_oracle_scope_is_not_refused(
    tmp_path: Path, adaptive: Any, needle: str
) -> None:
    reflector, client = repairing(tmp_path, [_response("list_files"), finish(VALID)])

    result = await reflector.reflect(adaptive(admit=admission(needles=(needle,))))

    assert (result.status, result.groups) == ("reflected", (VALID,))
    assert len(client.calls) == 2
    assert not rejected(_log_lines(reflector, "reflect-1"))


# -- other failures keep their classification ------------------------------------------


@pytest.mark.parametrize(
    ("fault", "code", "calls"),
    [
        ([_unauthorized()], "provider_rejected", 3),
        ([_server_error(), _server_error(), _server_error()], "provider_exhausted", 5),
    ],
)
async def test_a_provider_fault_during_correction_keeps_its_classification(
    tmp_path: Path, adaptive: Any, fault: list[Any], code: str, calls: int
) -> None:
    client = _ScriptedClient([_response("list_files"), finish(CROSS_CASE), *fault])
    reflector = ChatReflector(
        section=_section(retry=RETRY, max_turns=8),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM_V4),
        log_root=tmp_path / "run" / "reflector-logs",
        secrets=(SECRET,),
        client_factory=lambda section, credentials: client,
        role_resilience=True,
        bounds_repair=True,
        result_repair=True,
    )

    result = await reflector.reflect(adaptive())

    assert (result.status, result.code) == ("failed", code)
    assert len(client.calls) == calls
    assert len(rejected(_log_lines(reflector, "reflect-1"))) == 1


# -- `/15` and non-repairing reflectors are unchanged ----------------------------------


async def test_a_reflector_not_built_for_repair_ignores_the_closure(
    tmp_path: Path, adaptive: Any
) -> None:
    """`/15` chat: no in-role check, so the first finish is the role's result."""

    def forbidden(result: ReflectResult) -> Any:
        raise AssertionError("a /15 chat Reflector never calls the closure")

    reflector, client = _reflector(
        tmp_path, [_response("list_files"), finish(CROSS_CASE)], bounds_repair=True, max_turns=8
    )

    result = await reflector.reflect(adaptive(admit=forbidden))

    assert (result.status, result.groups) == ("reflected", (CROSS_CASE,))
    assert len(client.calls) == 2


def test_only_the_repairing_reflector_changes_its_tools_and_identity(tmp_path: Path) -> None:
    def finish_tool(**kw: Any) -> dict[str, Any]:
        tools = reflect_tools(probe=False, adaptive=True, history=True, **kw)
        return next(t for t in tools if t["function"]["name"] == "finish_reflection")

    assert finish_tool() == finish_tool(result_repair=False)
    assert finish_tool()["function"]["description"] == "finish reflection"
    repaired = finish_tool(result_repair=True)
    assert repaired["function"]["description"] == FINISH_REFLECTION_REPAIR_DESCRIPTION
    assert repaired["function"]["parameters"] == finish_tool()["function"]["parameters"]
    assert "case_handle" in FINISH_REFLECTION_REPAIR_DESCRIPTION

    section, prompt = _section(), seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM_V3)
    plain, _ = _reflector(tmp_path, [])
    repairing_one, _ = _reflector(tmp_path, [], result_repair=True)
    assert plain.identity == chat_reflector_identity(plain.section, plain.prompt)
    assert repairing_one.identity != plain.identity
    # Even under one declared prompt the repair contract changes the identity.
    assert chat_reflector_identity(section, prompt, result_repair=True) != (
        chat_reflector_identity(section, prompt)
    )


#: The identity of a chat Reflector built as `evolve` builds a `/15` one, as
#: Pinned tool-schema digest from before result repair (ADR-0048).
B11_V15_CHAT_REFLECTOR_DIGEST = "4b4c3311df142d986e09fd4967d95cd7b7de777ef0151593779b5498bb8921d7"


@pytest.mark.parametrize("v16", [False, True])
def test_a_v15_chat_reflector_identity_equals_b11s_and_v16_differs(
    tmp_path: Path, v16: bool
) -> None:
    from evolution.tree.bindings import build_reflector
    from evolution.tree.roles.config import ChatEditorSection, ReflectorSection

    role = ChatEditorSection(
        model="m", base_url="https://gw.example/v1", api_key_env="REFLECT_KEY", retry=RETRY
    )
    reflector = build_reflector(
        ReflectorSection(role=role, environment_docs=None, probe=None),
        seed_root=tmp_path / "seed",
        run_dir=tmp_path / "run",
        environ={"REFLECT_KEY": SECRET},
        model_grouping_family=True,
        role_resilience=True,
        bounds_repair=True,
        result_repair=v16,
    )
    packaged = TREE_REFLECTOR_SYSTEM_V4 if v16 else TREE_REFLECTOR_SYSTEM_V3
    assert reflector.prompt == seal_prompt(None, packaged=packaged)
    assert (reflector.identity.digest == B11_V15_CHAT_REFLECTOR_DIGEST) is not v16


async def test_reflection_timeout_still_bounds_repeated_rejections(
    tmp_path: Path, adaptive: Any
) -> None:
    """Without a turn limit in reach, the deadline ends the corrections with the
    same classification a reflection that never finishes gets."""

    import asyncio
    from dataclasses import replace

    class Slow:
        def __init__(self, responses: list[Any]) -> None:
            self.inner = _reflector(tmp_path, responses)[1]
            self.chat = self
            self.completions = self

        async def create(self, **kwargs: Any) -> Any:
            await asyncio.sleep(0.05)
            return await self.inner.create(**kwargs)

    outcomes = []
    for repair, responses in (
        (True, [_response("list_files"), finish(CROSS_CASE)]),
        (False, [_response("list_files")]),
    ):
        client = Slow(responses)
        reflector = ChatReflector(
            section=_section(max_turns=1000),
            credentials=ChatCredentials(SECRET, None),
            prompt=seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM_V4),
            log_root=tmp_path / f"run-{repair}" / "reflector-logs",
            secrets=(SECRET,),
            client_factory=lambda section, credentials, client=client: client,
            bounds_repair=True,
            result_repair=repair,
        )
        result = await reflector.reflect(replace(adaptive(), deadline_seconds=0.4))
        outcomes.append((result.status, result.code))
        assert len(client.inner.calls) < 20
    # The existing deadline failure (`EditFailure("provider_error",...)`).
    assert outcomes == [("failed", "provider_error")] * 2


async def test_a_v16_chat_reflector_null_extra_key_is_admitted_without_a_correction(
    tmp_path, adaptive
):
    reflector, client = repairing(
        tmp_path, [_response("list_files"), finish({**VALID, "suggested_fix_family": None})]
    )
    result = await reflector.reflect(adaptive(admit=admission(normalize_empty_keys=True)))
    assert result.status == "reflected" and "suggested_fix_family" not in result.groups[0]
    assert len(client.calls) == 2
    assert not rejected(_log_lines(reflector, "reflect-1"))


async def test_a_v16_chat_reflector_non_empty_extra_key_is_corrected_with_schema_keys(
    tmp_path, adaptive
):
    reflector, client = repairing(
        tmp_path,
        [_response("list_files"), finish({**VALID, "secretive_key_name": "x"}), finish(VALID)],
    )
    result = await reflector.reflect(adaptive(admit=admission(normalize_empty_keys=True)))
    assert result.status == "reflected" and len(client.calls) == 3
    sent = sent_to_model(client)
    assert "unknown key; allowed keys are root_cause, suggested_fix, evidence_ids" in sent
    assert "secretive_key_name" not in sent


async def test_a_secret_key_name_with_a_null_value_ends_the_chat_role_without_correction(
    tmp_path, adaptive
):
    admit = admission(normalize_empty_keys=True)
    reflector, client = repairing(
        tmp_path, [_response("list_files"), finish({**VALID, SECRET: None})]
    )
    result = await reflector.reflect(adaptive(admit=admit))
    assert len(client.calls) == 2
    with pytest.raises(GroupError) as caught:
        admit(result)
    assert caught.value.code == "invalid_result" and caught.value.repair == "none"
    assert not protocol(_log_lines(reflector, "reflect-1"))


async def test_chat_empty_key_normalization_runs_before_its_own_bounds_check(tmp_path, adaptive):
    from dataclasses import replace

    from tests.test_tree_campaign_reflector import group_at_stored_cap

    at_limit = group_at_stored_cap(VALID)
    reflector, client = repairing(
        tmp_path, [_response("list_files"), finish({**at_limit, "x": None})]
    )
    request = adaptive(admit=admission(normalize_empty_keys=True, group_text_chars=None))
    payload = dict(request.evidence.payload)
    payload["limits"] = {k: v for k, v in payload["limits"].items() if k != "sentence_chars"}
    request = replace(request, evidence=replace(request.evidence, payload=payload))
    result = await reflector.reflect(request)
    assert result.status == "reflected" and len(client.calls) == 2


async def test_a_missing_key_is_named_without_changing_its_protocol_repair_budget(
    tmp_path, adaptive
):
    bad = {key: value for key, value in VALID.items() if key != "evidence_ids"}
    reflector, client = repairing(tmp_path, [_response("list_files"), finish(bad), finish(VALID)])
    result = await reflector.reflect(adaptive(admit=admission(normalize_empty_keys=True)))
    assert result.status == "reflected" and len(client.calls) == 3
    assert len(protocol(_log_lines(reflector, "reflect-1"))) == 1
    assert "groups[0]: missing key evidence_ids" in sent_to_model(client)


async def test_turns_exhausted_carries_the_last_semantic_rejection_detail(tmp_path, adaptive):
    reflector, _ = repairing(
        tmp_path,
        [_response("list_files"), finish(CROSS_CASE), _response("list_files")],
        max_turns=3,
    )
    result = await reflector.reflect(adaptive())
    assert result.status == "failed" and result.code == "turns_exhausted"
    assert result.rejection.to_payload() == {
        "category": "uncited_affected_case",
        "path": "groups[0].affected_cases[0]",
    }


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize(
    "check,text", [("reflection_oracle_leak", NEEDLE), ("reflection_run_secret", SECRET)]
)
async def test_chat_terminal_obeys_output_modes_without_duplicate_preview_hits(
    tmp_path, adaptive, mode, check, text
):
    from dataclasses import replace

    from evolution.tree.execution.check_modes import CheckModes, HitSink

    hits = HitSink()
    modes = CheckModes({check: mode})
    leaking = {**VALID, "root_cause": text}

    def admit(result, *, collect=True):
        return admit_reflection_result(
            result,
            catalog_ids=OBSERVATIONS,
            observation_ids=OBSERVATIONS,
            failing_evidence_ids={"obs:c:1"},
            case_ids={"c", "d"},
            editable_paths=("*.md",),
            probe_seqs=None,
            needles=(NEEDLE,),
            runtime_secrets=(SECRET,),
            leak_first=True,
            normalize_empty_keys=True,
            modes=modes,
            hits=hits if collect else None,
        )

    reflector, client = repairing(tmp_path, [_response("list_files"), finish(leaking)])
    request = replace(
        adaptive(admit=admit), preview_admission=lambda result: admit(result, collect=False)
    )
    result = await reflector.reflect(request)
    if mode == "enforce":
        with pytest.raises(GroupError):
            admit(result)
    else:
        assert result.status == "reflected"
        assert result.groups == (leaking,)
    assert hits.entries() == (
        [
            {
                "check": check,
                "code": "oracle_leak" if check == "reflection_oracle_leak" else "run_secret",
                "count": 1,
            }
        ]
        if mode == "shadow"
        else []
    )


@pytest.mark.parametrize("limit", [0, 5])
async def test_declared_reflector_protocol_repair_limit(tmp_path, adaptive, limit):
    malformed = finish(VALID, declined="no")
    reflector, client = repairing(
        tmp_path,
        [_response("list_files"), *[malformed for _ in range(4)], finish(VALID)],
        max_protocol_repairs=limit,
    )
    result = await reflector.reflect(adaptive())
    assert result.status == ("failed" if limit == 0 else "reflected")
    if limit == 0:
        assert result.code == "protocol"
        assert len(client.calls) == 2


@pytest.mark.parametrize("limit", [1, 7])
async def test_declared_reflector_tool_failure_limit(tmp_path, adaptive, limit):
    bad = _response("read_file", {"path": "missing.txt"})
    reflector, client = repairing(
        tmp_path,
        [_response("list_files"), *[bad for _ in range(limit)], finish(VALID)],
        max_consecutive_tool_failures=limit,
        max_turns=16,
    )
    result = await reflector.reflect(adaptive())
    assert result.status == "reflected"
    offered = [{t["function"]["name"] for t in call["tools"]} for call in client.calls]
    assert "read_file" in offered[limit]
    assert "read_file" not in offered[limit + 1]


async def test_zero_protocol_repairs_still_allows_result_corrections(tmp_path, adaptive):
    reflector, client = repairing(
        tmp_path,
        [
            _response("list_files"),
            *[finish(CROSS_CASE) for _ in range(4)],
            finish(VALID),
        ],
        max_protocol_repairs=0,
    )
    result = await reflector.reflect(adaptive())
    assert result.status == "reflected"
    assert len(client.calls) == 6
    lines = _log_lines(reflector, "reflect-1")
    assert len(rejected(lines)) == 4
    assert protocol(lines) == []
