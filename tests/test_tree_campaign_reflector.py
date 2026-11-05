"""Behavioral contracts for the Reflector role's contract (spec §3.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign_reflector import run_reflector
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.roles.editor_contracts import note_digest
from evolution.tree.roles.identities import ReflectorIdentity
from evolution.tree.roles.reflector_contracts import (
    REFLECT_LIMITS_V1,
    Group,
    GroupError,
    ReflectRequest,
    ReflectResult,
    find_needle,
    oracle_needles,
    reflection_needles,
    result_note_digest,
    validate_groups,
)

pytestmark = pytest.mark.slow

CATALOG = {
    "summary": None,
    "case:a": "a",
    "obs:a:1": "a",
    "cohort:c001": "a",
    "case:b": "b",
    "attempt:r01-a01": None,
}


def group(**overrides):
    base = {
        "root_cause": "the skill never names the route",
        "suggested_fix": "name it",
        "evidence_ids": ["obs:a:1"],
        "affected_cases": ["a"],
        "target_paths": ["skills/x.md"],
    }
    base.update(overrides)
    return base


def validate(groups, **kw):
    args = dict(
        catalog_ids=CATALOG,
        case_ids={"a", "b"},
        editable_paths=("skills/**",),
        probe_seqs=None,
        needles=("the secret answer",),
    )
    args.update(kw)
    return validate_groups(groups, **args)


def test_valid_group_round_trips():
    (parsed,) = validate([group()])
    assert parsed == Group(
        "the skill never names the route", "name it", ("obs:a:1",), ("a",), ("skills/x.md",)
    )
    assert parsed.to_payload() == group() | {"probe_refs": []}


@pytest.mark.parametrize(
    "bad",
    [
        group(evidence_ids=[]),
        group(evidence_ids=["obs:zzz:1"]),
        group(affected_cases=["b"]),  # b is not the case of a cited record
        group(target_paths=["README.md"]),
        group(root_cause=""),
        group(root_cause="x" * 513),
        group(suggested_fix="line\x00break"),
        group(extra=1),
        group(probe_refs=[1]),  # no probe declared
        [group()] * 9,
    ],
)
def test_invalid_groups(bad):
    groups = bad if isinstance(bad, list) else [bad]
    with pytest.raises(GroupError) as info:
        validate(groups)
    assert info.value.code == "invalid_result"


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md", "a/../b.md"])
def test_target_paths_must_be_relative_candidate_paths(path):
    """The writable match alone admits an escape: `artifact_policy.matches` is
    gitignore-like, so a slashless pattern matches a *basename* at any depth and
    `*.md` would accept all three of these. Membership in the Candidate is
    decided first, by the actor path grammar the editors' tools use (branch
    review round 1, `reflector-target-path-escape`)."""

    with pytest.raises(GroupError) as info:
        validate([group(target_paths=[path])], editable_paths=("*.md",))
    assert info.value.code == "invalid_result"
    assert "not a relative Candidate path" in str(info.value)


def test_a_relative_candidate_path_inside_the_writable_set_is_accepted():
    (parsed,) = validate([group(target_paths=["skills/x.md"])], editable_paths=("skills/**",))
    assert parsed.target_paths == ("skills/x.md",)


def test_probe_refs_validated_against_executed_seqs():
    (parsed,) = validate([group(probe_refs=[2])], probe_seqs={1, 2})
    assert parsed.probe_refs == (2,)
    with pytest.raises(GroupError):
        validate([group(probe_refs=[3])], probe_seqs={1, 2})
    with pytest.raises(GroupError):
        validate([group()], probe_seqs={1})  # probe declared: the key is required


def test_oracle_needle_is_a_leak():
    with pytest.raises(GroupError) as info:
        validate([group(suggested_fix="answer with the secret answer")])
    assert info.value.code == "oracle_leak"


def test_oracle_needle_outranks_a_schema_defect():
    """A leak is reported as `oracle_leak` even when the same group also carries an
    unrelated schema defect (an unknown evidence id here) -- the needle scan runs
    ahead of every other check (module docstring)."""

    with pytest.raises(GroupError) as info:
        validate(
            [
                group(
                    evidence_ids=["obs:zzz:1"],
                    suggested_fix="answer with the secret answer",
                )
            ]
        )
    assert info.value.code == "oracle_leak"


def test_zero_groups_is_valid():
    assert validate([]) == ()


def test_oracle_needles_are_recursive_strings_of_eight_or_more():
    case = EvalCase(
        case_id="a",
        group_id="g",
        stratum="s",
        tags=(),
        public={},
        private={},
        oracle={"expected": "Paris", "nested": {"long": "the secret answer", "n": 7}},
    )
    assert oracle_needles([case]) == ("the secret answer",)
    assert find_needle("prefix the secret answer suffix", ("the secret answer",))
    assert not find_needle("Paris", ("the secret answer",))


# --- #172: oracle strings the Editor already sees are not needles (families 12+) ---

#: A case whose oracle repeats its own structural identifiers -- the case id,
#: group, stratum, a tag and its evidence ids -- next to a criterion id, a
#: reference answer and a sentence that merely contains the case id.
_STRUCTURAL = EvalCase(
    case_id="reference:075",
    group_id="group-payments",
    stratum="stratum-hard",
    tags=("sheet-acquiring",),
    public={"question": "where does the payment go"},
    private={},
    oracle={
        "scenario": "reference:075",
        "group": "group-payments",
        "stratum": "stratum-hard",
        "sheet": "sheet-acquiring",
        "evidence": ["obs:reference:075:1", "case:reference:075"],
        "criteria": ["reference_fidelity"],
        "answer": "route the payment to refund_flow",
        "explained": "the expected route of reference:075 is refund_flow",
    },
)
_OTHER = EvalCase("b-case-02", "group-other", "stratum-easy", (), {}, {}, {})
_MENTIONS = (
    "reference:075 (group-payments, stratum-hard, sheet-acquiring) fails in "
    "obs:reference:075:1 and case:reference:075"
)
_ORACLE_CATALOG = {"obs:reference:075:1": "reference:075"}


def _structural_validate(text, family_version):
    needles = reflection_needles([_STRUCTURAL, _OTHER], family_version=family_version)
    return validate_groups(
        [
            {
                "root_cause": text,
                "suggested_fix": "name the route",
                "evidence_ids": ["obs:reference:075:1"],
                "affected_cases": ["reference:075"],
                "target_paths": ["skills/x.md"],
            }
        ],
        catalog_ids=_ORACLE_CATALOG,
        case_ids={"reference:075", "b-case-02"},
        editable_paths=("skills/**",),
        probe_seqs=None,
        needles=needles,
    )


def test_structural_oracle_strings_are_not_needles_in_family_14():
    (parsed,) = _structural_validate(_MENTIONS, 14)
    assert parsed.root_cause == _MENTIONS
    assert set(reflection_needles([_STRUCTURAL], family_version=14)) == {
        "reference_fidelity",
        "route the payment to refund_flow",
        "the expected route of reference:075 is refund_flow",
    }


@pytest.mark.parametrize(
    "text",
    [
        "say: route the payment to refund_flow",
        "note that the expected route of reference:075 is refund_flow",
        "it fails reference_fidelity",
    ],
)
def test_oracle_material_stays_a_leak_in_family_14(text):
    with pytest.raises(GroupError) as info:
        _structural_validate(text, 14)
    assert info.value.code == "oracle_leak"


def test_the_allowed_set_is_exact_corpus_structure_only():
    """Exactly the case id, group id, stratum, tags and the case's own `case:`/`obs:`
    evidence ids, compared by equality. Public input values are not allowed
    (the Editor sees a multiple-choice option, not which one is right), and
    an evidence-id lookalike for another repeat of the same case is allowed
    while one for an unknown case is not."""

    case = EvalCase(
        "reference:075",
        "group-payments",
        "stratum-hard",
        ("sheet-acquiring",),
        {"options": ["refund_flow_option", "capture_flow_option"]},
        {},
        {
            "answer": "refund_flow_option",
            "other_repeat": "obs:reference:075:12",
            "unknown_case": "obs:reference:076:1",
            "padded_repeat": "obs:reference:075:01",
            "case_spaced": "Reference:075",
        },
    )
    assert set(reflection_needles([case], family_version=14)) == {
        "refund_flow_option",
        "obs:reference:076:1",
        "obs:reference:075:01",
        "Reference:075",
    }


def test_escaped_and_digest_case_ids_are_matched_by_their_rendered_evidence_ids():
    from evolution.tree.roles.editor.editor_brief import _case_evidence_id, _observation_evidence_id

    long_id = "scenario with spaces/" + "x" * 200
    case = EvalCase(
        long_id,
        "group-long",
        "stratum-long",
        (),
        {},
        {},
        {"ids": [_case_evidence_id(long_id), _observation_evidence_id(long_id, 3)]},
    )
    assert reflection_needles([case], family_version=14) == ()


class _Reflector:
    identity = ReflectorIdentity("scripted", "0" * 64)

    def __init__(self, result, sleep=0.0):
        self.result, self.sleep = result, sleep

    async def reflect(self, request):
        await asyncio.sleep(self.sleep)
        return self.result


def request(deadline=5.0):
    return ReflectRequest("r01-x1", 1, Path("/nowhere"), None, None, None, ("skills/**",), deadline)


def test_run_reflector_finished():
    outcome = asyncio.run(
        run_reflector(
            _Reflector(ReflectResult("reflected", (), "ok")), request(), cancelled=lambda: False
        )
    )
    assert (outcome.terminal, outcome.status, outcome.note, outcome.alive) == (
        "finished",
        "reflected",
        "ok",
        False,
    )


def test_run_reflector_timeout_is_operational():
    outcome = asyncio.run(
        run_reflector(
            _Reflector(ReflectResult("reflected", (), ""), sleep=2),
            request(0.05),
            cancelled=lambda: False,
        )
    )
    assert (outcome.terminal, outcome.error_class) == ("failed", "timeout")


def test_result_note_digest_agrees_with_the_editor_s_note_digest():
    """One canonical hashing for a `note`, whichever role wrote it."""

    assert result_note_digest("x") == note_digest("x")


def test_reflect_limits_v1_values():
    assert REFLECT_LIMITS_V1 == {
        "cohorts": 16,
        "groups": 8,
        "sentence_chars": 512,
        "verdicts": 32,
        "verdict_note_chars": 256,
        "history_rounds": 6,
        "environment_docs_chars": 60_000,
        "turns": 8,
        "events_per_turn": 32,
        "text_chars": 4096,
        "input_chars": 2048,
        "feedback_keys": 16,
        "feedback_value_chars": 64,
    }


@pytest.mark.parametrize("backend", ["chat", "subprocess", "agent"])
@pytest.mark.parametrize("probes", [False, True])
async def test_x2_backends_read_evidence_docs_and_live_probe(tmp_path, backend, probes):
    import sys
    from dataclasses import replace
    from types import SimpleNamespace

    from evolution.tree.roles.reflector_contracts import ProbeAccess
    from evolution.tree.target.probe_mediator import ProbeMediator
    from tests.probe_fakes import ProbeHTTPServer
    from tests.test_tree_adaptive_reflector import adaptive_fixture, group
    from tests.tree.target.test_probe_mediator import config, session

    trace, inputs, brief = adaptive_fixture(tmp_path, docs=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "SKILL.md").write_text("Parent")
    events = []
    mediator = None
    try:
        with ProbeHTTPServer() as target:
            native = session(tmp_path, config(target.base_url), events)
            if probes:
                mediator = ProbeMediator(native)
                await mediator.start()
            with inputs.reader_view().open_operation(
                "reflect-1", {"list_evidence", "read_evidence", "list_documents", "read_document"}
            ) as reader:
                access = (
                    ProbeAccess(mediator.url, 4, ("GET", "POST"), "disposable environment")
                    if mediator
                    else None
                )
                request = ReflectRequest(
                    "reflect-1",
                    1,
                    workspace,
                    brief,
                    inputs._documents,
                    access,
                    ("*.md",),
                    30,
                    reader=reader,
                    probe_session=native if probes else None,
                )
                eid = next(iter(brief.observation_ids))
                did = next(iter(brief.document_ids))
                claimed = group(
                    evidence_ids=[eid],
                    affected_cases=["c"],
                    document_ids=[did],
                    **({"probe_refs": [1]} if probes else {}),
                )
                if backend == "chat":
                    from tests.tree.roles.reflector.test_chat_reflector import _reflector, _response

                    responses = [
                        _response("list_files"),
                        _response("list_evidence"),
                        _response("read_evidence", {"evidence_id": eid}),
                        _response("list_documents"),
                        _response("read_document", {"document_id": did}),
                    ]
                    if probes:
                        responses.append(
                            _response(
                                "probe_request",
                                {"method": "GET", "path": "/status/503", "body": None},
                            )
                        )
                    responses.append(_response("finish_reflection", {"groups": [claimed]}))
                    reflector, client = _reflector(tmp_path, responses, max_turns=12)
                else:
                    script = (
                        """import json, pathlib, re, subprocess, sys, urllib.request
request = json.loads(pathlib.Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else json.load(sys.stdin)
keys = {"version", "reflection_id", "round", "workspace", "editable_paths", "deadline_seconds", "probe", "evidence", "opening", "readers", "reader_arguments"}
assert set(request) == keys | ({"role_budget"} if len(sys.argv)>1 else set())
assert "DOC-BODY-CANARY" not in json.dumps(request)
assert "probe_request" not in request["readers"]
for name, args in [("list_evidence", {}), ("read_evidence", {"evidence_id": EID}), ("list_documents", {}), ("read_document", {"document_id": DID})]:
    value = json.loads(subprocess.check_output(request["readers"][name] + [json.dumps(args)]))
    if name == "read_document":
        assert value["content"] == "DOC-BODY-CANARY"
if request["probe"]:
    args = {"method":"GET", "path":"/status/503", "body":None}
    if len(sys.argv)>1:
        command = json.loads(re.search(r"helper argv (\\[.*?\\]) with", request["opening"]).group(1))
        assert request["probe"]["url"] not in json.dumps(command)
        value = json.loads(subprocess.check_output(command + [json.dumps(args)]))
    else:
        req = urllib.request.Request(request["probe"]["url"], data=json.dumps(args).encode(), headers={"Content-Type":"application/json"})
        value = json.load(urllib.request.urlopen(req))
    assert value["seq"] == 1 and set(value) == {"seq", "status", "body", "truncated"}
    print(request["probe"]["url"], file=sys.stderr)
else:
    assert "probe_request" not in request["opening"]
result = json.dumps({"status":"reflected", "groups":[CLAIMED], "note":"done"})
if len(sys.argv)>1:
    pathlib.Path(sys.argv[1]).with_name("REFLECTION.json").write_text(result)
else:
    print(result)
""".replace("EID", repr(eid))
                        .replace("DID", repr(did))
                        .replace("CLAIMED", repr(claimed))
                    )
                    if backend == "subprocess":
                        from tests.tree.roles.reflector.test_subprocess_reflector import (
                            _bind_reflector,
                        )

                        reflector = _bind_reflector(tmp_path, script)
                    else:
                        from tests.tree.roles.reflector.test_agent_reflector import _reflector

                        class Backend:
                            last_usage = {"tool_turns": 1}

                            async def complete_file_edit(self, root, *args, **kwargs):
                                process = await asyncio.create_subprocess_exec(
                                    sys.executable,
                                    "-c",
                                    script,
                                    str(root / ".evolution" / "REFLECT_REQUEST.json"),
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, stderr = await process.communicate()
                                assert process.returncode == 0, stderr.decode()
                                return SimpleNamespace(content=stderr.decode())

                        reflector, _ = _reflector(tmp_path, [])
                        reflector = replace(reflector, backend_factory=lambda **kw: Backend())
                result = await reflector.reflect(request)
                assert result.status == "reflected", result.note
                assert result.groups == (claimed,)
                assert len(reader.summary.entries) == 4
                assert len(events) == int(probes)
                assert len(target.calls) == int(probes)
                assert (workspace / "SKILL.md").read_text() == "Parent"
                if access:
                    for path in reflector.log_root.glob("*"):
                        if path.is_file():
                            log = path.read_text()
                            assert access.url not in log and mediator.capability not in log
                if backend == "agent" and probes:
                    control = workspace / ".evolution" / "probe"
                    assert {path.name for path in control.iterdir()} == {
                        "session.json",
                        "probe_client.py",
                        "probe_wire.py",
                        "probe_request.py",
                    }
                    assert all(path.stat().st_mode & 0o777 == 0o400 for path in control.iterdir())
    finally:
        if mediator:
            await mediator.close()
        trace.close()


# -- H8 Task 9 (F0 R51, A§6, PDR-0017 2026-10-09 amendment): criterion ids ------

#: A scorer criterion id the oracle also holds, exactly and inside a longer string.
_CRITERION = "exact-answer-check"
_CRITERION_CASE = EvalCase(
    "c1",
    "g1",
    "s1",
    (),
    {},
    {},
    {
        "check": _CRITERION,
        "explained": f"{_CRITERION} expects refund_flow",
        "hint": "diagnostic-only-hint",
        "note": "the reference route is refund_flow",
    },
)


def test_family_14_drops_exactly_equal_published_criterion_ids_only():
    full = reflection_needles([_CRITERION_CASE], family_version=14)
    assert _CRITERION in full
    dropped = reflection_needles(
        [_CRITERION_CASE], family_version=14, published_criteria=frozenset({_CRITERION})
    )
    assert dropped == tuple(needle for needle in full if needle != _CRITERION)
    # A needle that merely contains the id stays; so do the note and the
    # diagnostics-only value, which are not criteria.
    assert f"{_CRITERION} expects refund_flow" in dropped
    assert {"diagnostic-only-hint", "the reference route is refund_flow"} <= set(dropped)


def test_family_14_drops_nothing_without_published_criteria():
    # PDR-0017: unpublished criterion ids and diagnostic strings stay private.
    expected = (
        "exact-answer-check expects refund_flow",
        "the reference route is refund_flow",
        "diagnostic-only-hint",
        "exact-answer-check",
    )
    assert reflection_needles([_CRITERION_CASE], family_version=14) == expected
    # A published id that is only a prefix/substring of a needle drops nothing.
    assert (
        reflection_needles(
            [_CRITERION_CASE], family_version=14, published_criteria=frozenset({"exact-answer"})
        )
        == expected
    )


def test_family_14_excludes_structural_identifiers_only():
    # PDR-0017: exact visible identifiers leave the oracle needles; longer strings stay.
    assert set(
        reflection_needles(
            [_STRUCTURAL, _OTHER], family_version=14, published_criteria=frozenset({"other-check"})
        )
    ) == {
        "the expected route of reference:075 is refund_flow",
        "route the payment to refund_flow",
        "reference_fidelity",
    }


CANNED = "This request is not implemented yet."


def _case(case_id: str, oracle: dict[str, Any]) -> EvalCase:
    return EvalCase(case_id, "g1", "s1", (), {"text": "hello"}, {}, oracle)


def test_a_declared_answer_and_its_substrings_leave_every_needle_set():
    corpus = (
        _case(
            "c1",
            {
                "answer": CANNED,
                "part": "not implemented",
                "gold": f"{CANNED} 45",
                "fact": "refund of 45 EUR",
            },
        ),
        _case("c2", {"criterion": "answers_the_request"}),
    )
    full = oracle_needles(corpus)
    assert CANNED in full and "not implemented" in full
    declared = oracle_needles(corpus, (CANNED,))
    # contained in a declared string
    assert CANNED not in declared and "not implemented" not in declared
    assert f"{CANNED} 45" in declared  # a longer value that contains it stays (D13a)
    assert "refund of 45 EUR" in declared  # undeclared stays
    assert find_needle(f"the agent says {CANNED!r}", declared) is False
    assert find_needle(f"the gold is {CANNED} 45", declared) is True
    for family in (12, 13, 14):
        needles = reflection_needles(corpus, family_version=family, declared=(CANNED,))
        assert CANNED not in needles and f"{CANNED} 45" in needles
    per_baseline = reflection_needles(
        corpus,
        family_version=14,
        published_criteria=frozenset({"answers_the_request"}),
        declared=(CANNED,),
    )
    assert "answers_the_request" not in per_baseline and CANNED not in per_baseline


def test_without_a_declaration_the_needle_sets_are_unchanged():
    corpus = (_case("c1", {"answer": CANNED, "gold": f"{CANNED} 45"}),)
    assert oracle_needles(corpus, ()) == oracle_needles(corpus)
    assert reflection_needles(corpus, family_version=14, declared=()) == reflection_needles(
        corpus, family_version=14
    )


def test_matching_is_exact():
    corpus = (_case("c1", {"answer": CANNED}),)
    assert CANNED in oracle_needles(corpus, (CANNED.lower(),))
    # contained the other way round: stays
    assert CANNED in oracle_needles(corpus, (CANNED.rstrip("."),))


@pytest.mark.parametrize("declared", [None, (CANNED,)])
def test_the_campaign_session_builds_its_needles_from_the_declaration(tmp_path, declared):
    from evolution.tree import campaign as campaign_module
    from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
    from evolution.tree.target.connection_contract import (
        ConnectionContract,
        MetricContract,
        RoleVisibility,
        VerbatimAnswers,
    )
    from tests.campaign_fakes import _current_adaptive_config, policy

    corpus = (_case("c1", {"answer": CANNED, "gold": f"{CANNED} 45"}),)
    connection = ConnectionContract(
        MetricContract("pointwise", ()),
        RoleVisibility(()),
        None if declared is None else VerbatimAnswers(declared),
    )
    session = campaign_module._Campaign(
        policy(),
        run_root=tmp_path / "run",
        seed_root=tmp_path,
        artifact_policy=ArtifactPolicyV1((), (), ()),
        adapter=None,  # type: ignore[arg-type]
        environment=None,  # type: ignore[arg-type]
        scorer=None,  # type: ignore[arg-type]
        editor=None,  # type: ignore[arg-type]
        reflector=None,  # type: ignore[arg-type]
        environment_docs=None,
        corpus=corpus,
        adaptive=_current_adaptive_config(corpus, policy()),
        cancelled=lambda: False,
        secrets=(),
        hooks=None,
        family_version=14,
        connection=connection,
    )
    assert session.declared_answers == (() if declared is None else declared)
    assert session.needles == oracle_needles(corpus, session.declared_answers)
    assert session.reflection_needles == reflection_needles(
        corpus, family_version=14, declared=session.declared_answers
    )
    assert (CANNED in session.needles) is (declared is None)
    assert f"{CANNED} 45" in session.needles
    assert not (tmp_path / "run").exists()


# -- #318 (ADR-0048): one admission rule for final and in-role checks ------------

_NEEDLE = "the secret answer"
_SECRET = "sk-forwarded-credential"
_OBSERVATIONS = {"obs:a:1": "a", "obs:b:1": "b"}


def adaptive_group(**overrides: Any) -> dict[str, Any]:
    return {
        "root_cause": "the skill never names the route",
        "suggested_fix": "name it",
        "counterevidence": "the passing case named it",
        "regression_risks": "a longer answer",
        "why_this_layer": "the skill text owns the route",
        "evidence_ids": ["obs:a:1"],
        "affected_cases": ["a"],
        "target_paths": ["skills/x.md"],
        **overrides,
    }


def admit(groups: list[Any], *, leak_first: bool, note: str = "n", **kw: Any) -> tuple[Group, ...]:
    from evolution.tree.roles.reflector_contracts import admit_reflection_result

    args: dict[str, Any] = dict(
        catalog_ids=_OBSERVATIONS,
        observation_ids=_OBSERVATIONS,
        failing_evidence_ids={"obs:a:1"},
        case_ids={"a", "b"},
        editable_paths=("skills/**",),
        probe_seqs=None,
        needles=(_NEEDLE,),
        runtime_secrets=(_SECRET,),
    )
    args.update(kw)
    return admit_reflection_result(
        ReflectResult("reflected", tuple(groups), note), leak_first=leak_first, **args
    )


def b11_admission(groups: list[Any], *, note: str = "n") -> tuple[Group, ...]:
    """`2.0.0b11`'s inline campaign admission block, transcribed as the reference."""

    from evolution.tree.storage.envelope import secret_match

    if find_needle(note, (_NEEDLE,)):
        raise GroupError("oracle_leak", "the note carries an oracle string")
    parsed = validate_groups(
        groups,
        catalog_ids=_OBSERVATIONS,
        case_ids={"a", "b"},
        editable_paths=("skills/**",),
        probe_seqs=None,
        needles=(_NEEDLE,),
        observation_ids=_OBSERVATIONS,
        failing_evidence_ids={"obs:a:1"},
    )
    payload = {"groups": [group.to_payload() for group in parsed], "note": note}
    if secret_match({"payload": payload}, (_SECRET,)) is not None:
        raise GroupError("invalid_result", "the diagnosis carries a run secret")
    return parsed


def _outcome(call: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return ("admitted", call(*args, **kwargs))
    except GroupError as exc:
        return ("refused", exc.code, exc.detail)


#: Results whose b11 code depends on check order: every leak combined with a
#: bounds, shape or semantic defect, plus single defects and valid answers.
ORDER_CASES = {
    "valid": [adaptive_group()],
    "case_handle": [adaptive_group(affected_cases=["case_0123456789abcdef"])],
    "cross_case": [adaptive_group(affected_cases=["b"])],
    "oracle_then_invalid_case": [
        adaptive_group(affected_cases=["zzz"]),
        adaptive_group(root_cause=f"say {_NEEDLE}"),
    ],
    "oracle_and_oversized": [adaptive_group(root_cause=f"say {_NEEDLE}", why_this_layer="x" * 513)],
    "oracle_and_extra_key": [adaptive_group(root_cause=f"say {_NEEDLE}", extra="k")],
    "oracle_in_note": [adaptive_group(affected_cases=["zzz"])],
    "secret_in_target_path": [adaptive_group(target_paths=[f"skills/{_SECRET}.md"])],
    "secret_and_invalid_case": [
        adaptive_group(root_cause=f"send {_SECRET}", affected_cases=["zzz"])
    ],
    "secret_in_extra_key": [adaptive_group(extra=_SECRET)],
}


@pytest.mark.parametrize("name", sorted(ORDER_CASES))
def test_without_leak_first_admission_decides_exactly_as_b11(name: str) -> None:
    """`/15` and non-chat `/16` keep b11's decision and code for every input."""

    groups = ORDER_CASES[name]
    note = f"n {_NEEDLE}" if name == "oracle_in_note" else "n"
    assert _outcome(admit, groups, leak_first=False, note=note) == _outcome(
        lambda: b11_admission(groups, note=note)
    )


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("oracle_then_invalid_case", "oracle_leak"),
        ("oracle_and_oversized", "oracle_leak"),
        ("oracle_and_extra_key", "oracle_leak"),
        ("secret_and_invalid_case", "invalid_result"),
        ("secret_in_extra_key", "invalid_result"),
        ("secret_in_target_path", "invalid_result"),
    ],
)
def test_leak_first_reports_any_leak_as_unrepairable_before_other_defects(
    name: str, code: str
) -> None:
    with pytest.raises(GroupError) as caught:
        admit(ORDER_CASES[name], leak_first=True)
    assert (caught.value.code, caught.value.repair, caught.value.correction) == (code, "none", None)


def test_the_leak_first_order_changes_only_combined_defects() -> None:
    """Where b11 reported another defect first, `/16` chat reports the leak."""

    changed = {
        name
        for name, groups in ORDER_CASES.items()
        if name != "oracle_in_note"
        and _outcome(admit, groups, leak_first=True)[:2]
        != _outcome(admit, groups, leak_first=False)[:2]
    }
    assert changed == {"oracle_then_invalid_case", "oracle_and_oversized", "oracle_and_extra_key"}


@pytest.mark.parametrize(
    ("groups", "correction"),
    [
        (
            [adaptive_group(affected_cases=["case_0123456789abcdef"])],
            "groups[0].affected_cases[0]: expected case_id",
        ),
        (
            [adaptive_group(), adaptive_group(affected_cases=["a", "b"])],
            "groups[1].affected_cases[1]: each affected case needs a citation of its own observation",
        ),
        (
            [adaptive_group(evidence_ids=["obs:a:1", "obs:zz:9"])],
            "groups[0].evidence_ids[1]: unknown evidence ID",
        ),
        (
            [adaptive_group(evidence_ids=[])],
            "groups[0].evidence_ids: cite at least one evidence ID",
        ),
        (
            [adaptive_group(target_paths=["../outside.md"])],
            "groups[0].target_paths[0]: expected a writable Candidate path",
        ),
        (
            [adaptive_group(probe_refs=[7])],
            "groups[0].probe_refs[0]: names a probe that did not run",
        ),
        (
            [adaptive_group(extra="k")],
            "groups[0]: keys do not match the finish_reflection group schema",
        ),
    ],
)
def test_a_semantic_refusal_names_a_field_path_and_never_the_value(
    groups: list[Any], correction: str
) -> None:
    probe = {"probe_seqs": set()} if "probe_refs" in groups[0] else {}
    with pytest.raises(GroupError) as caught:
        admit(groups, leak_first=True, **probe)
    error = caught.value
    assert (error.code, error.repair, error.correction) == (
        "invalid_result",
        "semantic",
        correction,
    )
    for value in ("case_0123456789abcdef", "obs:zz:9", "../outside.md", "extra", "7"):
        assert value not in correction
    # The historical text is kept for every other reader.
    assert error.detail in {
        "affected_cases requires a cited observation of every case",
        "evidence_ids is empty or cites an unknown record",
        "target_paths leaves the writable Candidate set",
        "probe_refs names a probe that did not run",
        "group keys do not match the adaptive schema",
    }


def test_a_bounds_violation_is_a_protocol_repair_in_the_leak_first_order() -> None:
    with pytest.raises(GroupError) as caught:
        admit([adaptive_group(root_cause="x" * 513)], leak_first=True)
    assert (caught.value.detail, caught.value.repair) == (
        "root_cause exceeds 512 characters",
        "protocol",
    )


@pytest.mark.parametrize("needle", ["obs:a:1", "skills/x.md", "skills/"])
def test_an_oracle_value_outside_the_sentence_scope_is_admitted(needle: str) -> None:
    """The oracle scope stays the note and the sentences: an evidence ID or a
    target path that holds an oracle string is not refused (design §3)."""

    groups = [adaptive_group()]
    admitted = admit(groups, leak_first=True, needles=(needle,))
    assert admitted == admit(groups, leak_first=False, needles=(needle,))
    assert admitted[0].target_paths == ("skills/x.md",)


def test_the_in_role_and_final_checks_agree_on_the_same_frozen_inputs() -> None:
    """The campaign calls the same closure twice: equal inputs, equal outcome."""

    for groups in ORDER_CASES.values():
        first = _outcome(admit, groups, leak_first=True)
        assert _outcome(admit, groups, leak_first=True) == first


def test_the_probe_capability_is_a_run_secret_at_admission() -> None:
    capability = "0123456789abcdef0123456789abcdef"
    groups = [adaptive_group(root_cause=f"call /{capability}/probe")]
    for leak_first in (False, True):
        assert admit(groups, leak_first=leak_first) == admit(groups, leak_first=False)
        with pytest.raises(GroupError) as caught:
            admit(groups, leak_first=leak_first, runtime_secrets=(_SECRET, capability))
        assert caught.value.code == "invalid_result"


PATH_CORPUS = (
    EvalCase(
        "c1",
        "g1",
        "s1",
        (),
        {},
        {},
        {
            "answers": ["alpha-answer-1", {"text": "beta-answer-2"}],
            "reference_spec": {"field": "unit_name"},
            "reference": "GOLD-FACT-4242",
            "a/b": "slash-key-val",
            "m~n": "tilde-key-val",
        },
    ),
    EvalCase("c2", "g1", "s1", (), {}, {}, {"answers": "gamma-answer-3"}),
)


def test_absent_key_keeps_the_needle_set_byte_identical():
    expected = (
        "GOLD-FACT-4242",
        "alpha-answer-1",
        "gamma-answer-3",
        "beta-answer-2",
        "slash-key-val",
        "tilde-key-val",
        "unit_name",
    )
    assert oracle_needles(PATH_CORPUS) == expected
    assert oracle_needles(PATH_CORPUS, secret_paths=None) == expected
    for family in (14, 15):
        assert reflection_needles(PATH_CORPUS, family_version=family) == reflection_needles(
            PATH_CORPUS, family_version=family, secret_paths=None
        )


def test_subtree_and_list_coverage():
    assert oracle_needles(PATH_CORPUS, secret_paths=("/answers",)) == (
        "alpha-answer-1",
        "gamma-answer-3",
        "beta-answer-2",
    )
    assert oracle_needles(PATH_CORPUS, secret_paths=("/answers/1",)) == ("beta-answer-2",)
    assert oracle_needles(PATH_CORPUS, secret_paths=("",)) == oracle_needles(PATH_CORPUS)
    assert oracle_needles(PATH_CORPUS, secret_paths=()) == ()


def test_a_pointer_selects_by_token_prefix_not_string_prefix():
    assert oracle_needles(PATH_CORPUS, secret_paths=("/reference",)) == ("GOLD-FACT-4242",)
    assert oracle_needles(PATH_CORPUS, secret_paths=("/a~1b", "/m~0n")) == (
        "slash-key-val",
        "tilde-key-val",
    )
    assert oracle_needles(PATH_CORPUS, secret_paths=("/answers/-", "/answers/01")) == ()


def test_a_pointer_that_does_not_resolve_in_one_case_selects_nothing_there():
    assert oracle_needles(PATH_CORPUS, secret_paths=("/answers/1/text",)) == ("beta-answer-2",)


def test_declared_verbatim_answers_leave_the_declared_set():
    assert oracle_needles(
        PATH_CORPUS, ("GOLD-FACT-4242",), secret_paths=("/reference", "/answers")
    ) == ("alpha-answer-1", "gamma-answer-3", "beta-answer-2")


def test_locate_needle_finds_the_first_selected_location_in_corpus_and_walk_order():
    from evolution.tree.roles.reflector_contracts import encode_pointer, locate_needle

    corpus = (
        EvalCase(
            "c1",
            "g1",
            "s1",
            (),
            {},
            {},
            {"note": "GOLD-FACT-4242", "a": "GOLD-FACT-4242", "reference": "other-value-1"},
        ),
        EvalCase("c2", "g1", "s1", (), {}, {}, {"reference": "GOLD-FACT-4242"}),
    )
    assert locate_needle(corpus, "GOLD-FACT-4242", secret_paths=None) == ("c1", ("note",))
    assert locate_needle(corpus, "GOLD-FACT-4242", secret_paths=("/reference",)) == (
        "c2",
        ("reference",),
    )
    assert locate_needle(corpus, "missing needle", secret_paths=None) is None
    assert encode_pointer(("a/b", "m~n", "0")) == "/a~1b/m~0n/0"


def test_first_needle_is_the_longest_contained_needle():
    from evolution.tree.roles.reflector_contracts import NeedleWitness, first_needle

    needles = ("GOLD-FACT-4242", "unit_name")
    assert first_needle("x GOLD-FACT-4242 unit_name", needles) == "GOLD-FACT-4242"
    assert first_needle("nothing here", needles) is None
    witness = NeedleWitness(needles)
    assert witness("x unit_name") and witness.matched == "unit_name"
    assert not witness("nothing") and witness.matched == "unit_name"


def test_a_leak_group_error_carries_the_needle_but_never_prints_it():
    with pytest.raises(GroupError) as caught:
        admit([adaptive_group(root_cause=_NEEDLE)], leak_first=True)
    assert caught.value.needle == _NEEDLE
    assert _NEEDLE not in str(caught.value)


def group_at_stored_cap(base):
    import json

    empty = {**base, "root_cause": ""}
    stored = [{**empty, "probe_refs": []}]
    overhead = len(
        json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    return {**empty, "root_cause": "x" * (65_536 - overhead)}


def test_only_empty_keys_outside_the_schema_are_dropped():
    from evolution.tree.roles.reflector_contracts import (
        adaptive_group_schema,
        drop_empty_unknown_keys,
    )

    base = adaptive_group(evidence_ids=[])
    raw = [{**base, "a": None, "b": "", "c": [], "d": {}, "e": 0, "f": False, "g": "x", "h": " "}]
    (clean,) = drop_empty_unknown_keys(raw, adaptive_group_schema(probes=False, documents=False))
    assert set(clean) == set(base) | {"e", "f", "g", "h"} and clean["evidence_ids"] == []
    assert "a" in raw[0]


def test_family_15_admission_drops_suggested_fix_family_null():
    (parsed,) = admit(
        [adaptive_group(suggested_fix_family=None)], leak_first=False, normalize_empty_keys=True
    )
    assert parsed.to_payload() == admit([adaptive_group()], leak_first=False)[0].to_payload()


def test_v15_admission_decisions_are_unchanged():
    with pytest.raises(GroupError) as caught:
        admit([adaptive_group(suggested_fix_family=None)], leak_first=False)
    assert caught.value.detail == "group keys do not match the adaptive schema"


def test_a_non_empty_unknown_key_is_still_refused_and_named_by_schema_keys_only():
    with pytest.raises(GroupError) as caught:
        admit(
            [adaptive_group(secretive_key_name="content")],
            leak_first=False,
            normalize_empty_keys=True,
        )
    assert caught.value.correction == (
        "groups[0]: unknown key; allowed keys are root_cause, suggested_fix, evidence_ids, affected_cases, target_paths, counterevidence, regression_risks, why_this_layer"
    )
    assert "secretive_key_name" not in caught.value.correction


def test_a_missing_key_is_named():
    missing = {k: v for k, v in adaptive_group().items() if k != "evidence_ids"}
    with pytest.raises(GroupError) as caught:
        admit([missing], leak_first=False, normalize_empty_keys=True)
    assert caught.value.correction == "groups[0]: missing key evidence_ids"


def test_the_allowed_list_includes_probe_refs_and_document_ids_when_offered():
    with pytest.raises(GroupError) as caught:
        admit(
            [adaptive_group(extra="x")],
            leak_first=False,
            normalize_empty_keys=True,
            probe_seqs={1},
            document_ids={"d1"},
        )
    assert caught.value.correction.endswith("why_this_layer, probe_refs, document_ids")


def test_normalization_runs_before_bounds_and_merging():
    at_limit = group_at_stored_cap(adaptive_group())
    assert (
        len(
            admit(
                [{**at_limit, "x": None}],
                leak_first=False,
                normalize_empty_keys=True,
                group_text_chars=None,
            )
        )
        == 1
    )
    assert (
        len(
            admit(
                [adaptive_group(), adaptive_group(x=[])],
                leak_first=False,
                normalize_empty_keys=True,
            )
        )
        == 1
    )


def test_a_secret_key_name_with_a_null_value_ends_without_correction():
    with pytest.raises(GroupError) as caught:
        admit([{**adaptive_group(), _SECRET: None}], leak_first=False, normalize_empty_keys=True)
    assert caught.value.repair == "none" and caught.value.code == "invalid_result"


def test_a_non_chat_v16_result_with_a_secret_and_an_oracle_string_reports_the_secret():
    with pytest.raises(GroupError) as caught:
        admit(
            [adaptive_group(root_cause=f"{_NEEDLE} {_SECRET}")],
            leak_first=False,
            normalize_empty_keys=True,
        )
    assert caught.value.code == "invalid_result" and caught.value.repair == "none"


@pytest.mark.parametrize(
    ("groups", "code"),
    [
        (ORDER_CASES["oracle_then_invalid_case"], "oracle_leak"),
        (ORDER_CASES["oracle_and_oversized"], "oracle_leak"),
        (ORDER_CASES["oracle_and_extra_key"], "oracle_leak"),
        (ORDER_CASES["secret_and_invalid_case"], "invalid_result"),
        (ORDER_CASES["secret_in_extra_key"], "invalid_result"),
        (ORDER_CASES["secret_in_target_path"], "invalid_result"),
        ([adaptive_group(root_cause=f"{_NEEDLE} {_SECRET}")], "invalid_result"),
    ],
)
def test_a_chat_v16_result_follows_the_raw_secret_screen_then_the_leak_order(groups, code):
    """The production `/16` chat combination: a run secret outranks an oracle string."""

    with pytest.raises(GroupError) as caught:
        admit(groups, leak_first=True, normalize_empty_keys=True)
    assert (caught.value.code, caught.value.repair, caught.value.correction) == (code, "none", None)


@pytest.mark.parametrize(
    "bad,category,path",
    [
        ({"extra": "SECRET-LIKE-VALUE"}, "unknown_key", "groups[0]"),
        ({"evidence_ids": ["obs:VALUE-1:1"]}, "unknown_evidence_id", "groups[0].evidence_ids[0]"),
        ({"affected_cases": ["VALUE-2"]}, "not_a_case_id", "groups[0].affected_cases[0]"),
        ({"affected_cases": ["b"]}, "uncited_affected_case", "groups[0].affected_cases[0]"),
        ({"target_paths": ["../VALUE-3"]}, "unwritable_target_path", "groups[0].target_paths[0]"),
        # Shape/type errors hit the existing bounds pass before listed().
        ({"evidence_ids": "VALUE-4"}, "bounds", None),
        ({"evidence_ids": []}, "missing_evidence_id", "groups[0].evidence_ids"),
        ({"document_ids": ["VALUE-5"]}, "unknown_document_id", "groups[0].document_ids[0]"),
        ({"probe_refs": "VALUE-6"}, "not_a_probe_list", "groups[0].probe_refs"),
        ({"probe_refs": [9]}, "unknown_probe", "groups[0].probe_refs[0]"),
    ],
)
def test_detail_never_holds_a_value(bad, category, path):
    from evolution.tree.execution.journal_identity import canonical_json

    with pytest.raises(GroupError) as caught:
        admit(
            [adaptive_group(**bad)],
            leak_first=False,
            normalize_empty_keys=True,
            document_ids={"doc1"},
            probe_seqs={1},
        )
    detail = caught.value.rejection().to_payload()
    assert detail == {"category": category, **({"path": path} if path is not None else {})}
    text = canonical_json(detail).decode()
    assert not any(
        value in text
        for value in (
            "SECRET-LIKE-VALUE",
            "VALUE-1",
            "VALUE-2",
            "VALUE-3",
            "VALUE-4",
            "VALUE-5",
            "VALUE-6",
            "extra",
        )
    )


@pytest.mark.parametrize(
    "groups,expected",
    [
        ([adaptive_group()] * 9, "bounds"),
        ([adaptive_group(root_cause=_SECRET)], "run_secret"),
        ([adaptive_group(extra=float("nan"))], "protocol"),
        ([None], "protocol"),
    ],
)
def test_every_family_15_admission_refusal_has_a_closed_category(groups, expected):
    from evolution.tree.roles.role_failures import REFLECTION_DETAIL_CATEGORIES

    with pytest.raises(GroupError) as caught:
        admit(groups, leak_first=True, normalize_empty_keys=True)
    detail = caught.value.rejection()
    assert detail.category == expected and detail.category in REFLECTION_DETAIL_CATEGORIES
    assert detail.path is None


def test_an_oracle_leak_has_no_rejection_detail():
    assert GroupError("oracle_leak", "fixed text", needle=_NEEDLE).rejection() is None


@pytest.mark.parametrize("raw", [None, 7, "not-an-array", {"unknown": "value"}])
def test_a_malformed_group_container_has_protocol_detail(raw):
    from evolution.tree.roles.reflector_contracts import admit_reflection_result

    with pytest.raises(GroupError) as caught:
        admit_reflection_result(
            ReflectResult("reflected", raw, "n"),
            catalog_ids={},
            case_ids=set(),
            editable_paths=(),
            probe_seqs=None,
            needles=(),
            runtime_secrets=(),
            observation_ids={},
            normalize_empty_keys=True,
        )
    assert caught.value.rejection().to_payload() == {"category": "protocol"}
    assert caught.value.repair == "protocol"


def test_a_malformed_group_container_still_screens_run_secrets_first():
    from evolution.tree.roles.reflector_contracts import admit_reflection_result

    with pytest.raises(GroupError) as caught:
        admit_reflection_result(
            ReflectResult("reflected", {"unknown": _SECRET}, "n"),
            catalog_ids={},
            case_ids=set(),
            editable_paths=(),
            probe_seqs=None,
            needles=(),
            runtime_secrets=(_SECRET,),
            observation_ids={},
            normalize_empty_keys=True,
        )
    assert caught.value.rejection().to_payload() == {"category": "run_secret"}
    assert caught.value.repair == "none"


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize(
    "check,text,code",
    [
        ("reflection_oracle_leak", _NEEDLE, "oracle_leak"),
        ("reflection_run_secret", _SECRET, "run_secret"),
    ],
)
def test_reflection_output_check_modes(mode, check, text, code):
    from evolution.tree.execution.check_modes import CheckModes, HitSink

    hits = HitSink()
    options = dict(
        leak_first=False, normalize_empty_keys=True, modes=CheckModes({check: mode}), hits=hits
    )
    if mode == "enforce":
        with pytest.raises(GroupError):
            admit([adaptive_group(root_cause=text)], **options)
    else:
        groups = admit([adaptive_group(root_cause=text)], **options)
        assert groups[0].root_cause == text
    assert hits.entries() == (
        [{"check": check, "code": code, "count": 1}] if mode == "shadow" else []
    )


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_journal_redaction_does_not_relax_reflection_text_bounds(mode):
    from evolution.tree.execution.check_modes import CheckModes

    secret = "12345678"
    text = "x" * (512 - len(secret)) + secret
    with pytest.raises(GroupError) as caught:
        admit(
            [adaptive_group(root_cause=text)],
            leak_first=True,
            normalize_empty_keys=True,
            runtime_secrets=(secret,),
            modes=CheckModes({"reflection_run_secret": mode}),
        )
    assert caught.value.rejection().category == "bounds"
