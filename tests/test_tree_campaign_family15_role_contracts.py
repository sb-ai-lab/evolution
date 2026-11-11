"""Family-15 role settings, refusals, privacy and frozen-input upgrades."""

from contextlib import contextmanager

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.evolve import EvolveRefused
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize(
    "extra,want",
    [
        ({}, "editor context"),
        ({"reflector_context": None}, "editor context"),
        ({"reflector_context": "reflector context"}, "reflector context"),
        ({"reflector_context": ""}, None),
    ],
)
async def test_reflector_context_reaches_reflector_brief_not_editor_brief(
    tmp_path, monkeypatch, extra, want
):
    editor_contexts, reflector_contexts = [], []
    real_brief = campaign_module._Campaign.brief_for
    real_reflect = campaign_module.run_reflector

    def capture_brief(self, attempt, **kwargs):
        brief = real_brief(self, attempt, **kwargs)
        editor_contexts.append(brief.operator_context)
        return brief

    async def capture_reflect(reflector, request, **kwargs):
        reflector_contexts.append(request.evidence.operator_context)
        return await real_reflect(reflector, request, **kwargs)

    monkeypatch.setattr(campaign_module._Campaign, "brief_for", capture_brief)
    monkeypatch.setattr(campaign_module, "run_reflector", capture_reflect)
    _, result = await public_run(
        tmp_path, monkeypatch, campaign={"editor_context": "editor context", **extra}
    )
    assert result.terminal == "finished"
    assert reflector_contexts and set(reflector_contexts) == {want}
    assert editor_contexts and set(editor_contexts) == {"editor context"}


async def test_changing_reflector_context_on_resume_is_classified_like_editor_context(
    tmp_path, monkeypatch
):
    effects = []
    for key in ("editor_context", "reflector_context", "context_max_bytes"):
        root = tmp_path / key
        with pytest.raises(PowerCut):
            await public_run(root, monkeypatch, cut="after_attempt_input_frozen")
        journal = root / "run/campaign.jsonl"
        before = journal.read_bytes()
        with pytest.raises(EvolveRefused) as caught:
            await public_run(
                root,
                monkeypatch,
                campaign={key: 100000 if key == "context_max_bytes" else "changed"},
            )
        assert caught.value.code == "run_identity_changed"
        assert journal.read_bytes() == before
        effects.append(caught.value.effects)
    assert effects[0] == effects[1] == effects[2]
    assert effects[0] == ("close",)


@pytest.mark.parametrize("family", [14, 15])
async def test_fields_descriptions_reach_reflector_only_in_family15(tmp_path, monkeypatch, family):
    from tests.test_tree_campaign_family15_role_failures import campaign_run

    briefs = []
    real = campaign_module.run_reflector

    async def capture(reflector, request, **kw):
        briefs.append(request.evidence.payload["groups"])
        return await real(reflector, request, **kw)

    monkeypatch.setattr(campaign_module, "run_reflector", capture)
    _, result = await campaign_run(family, tmp_path, monkeypatch, campaign={"rounds": 1})
    assert result.terminal == "finished" and briefs
    assert all(
        (g["description"] is not None) is (family == 15) for groups in briefs for g in groups
    )


async def test_finished_pre_upgrade_fields_grouping_keeps_digest_after_rebase(
    tmp_path, monkeypatch
):
    import evolution
    from evolution.tree.execution import journal_identity
    from evolution.tree.execution.authorizations import Authorization
    from evolution.tree.roles.grouping.grouping_runtime import GroupingRuntime
    from tests.campaign_drivers import _records
    from tests.test_tree_campaign_grouping_rebase_binding import NEVER_PASSES

    installed = evolution.__version__
    real = GroupingRuntime.builtin

    async def run(version, **options):
        with monkeypatch.context() as patch:
            patch.setattr(campaign_module, "resolve_package_version", lambda: version)
            patch.setattr(journal_identity.metadata, "version", lambda name: installed)
            return await public_run(
                tmp_path,
                patch,
                campaign={"rounds": 2, "baseline_refresh_after_rounds": 1},
                scorer=NEVER_PASSES,
                **options,
            )

    with monkeypatch.context() as patch:
        patch.setattr(
            GroupingRuntime,
            "builtin",
            lambda self, *a, **kw: real(self, *a, **{**kw, "family15": False}),
        )
        with pytest.raises(PowerCut):
            await run("2.0.0b15", cut="after_grouping_finished")
    before = _records(tmp_path / "run/campaign.jsonl")
    old = next(r["payload"] for r in before if r["event"] == "grouping_finished")
    assert old["result"]["descriptions"] == {}
    with pytest.raises(EvolveRefused) as caught:
        await run(installed)
    assert caught.value.effects == ("rebase",)
    accepted = Authorization.new(
        "run_identity_changed",
        caught.value.instance_digest,
        "accept producing version",
        now="2026-11-02T12:00:00Z",
    )
    directory, result = await run(installed, authorizations=[accepted])
    records = _records(directory / "campaign.jsonl")
    assert result.terminal == "finished"
    assert (
        sum(r["event"] == "grouping_started" and r["payload"]["round"] == 1 for r in records) == 1
    )
    finished = [r["payload"] for r in records if r["event"] == "grouping_finished"]
    assert finished[0] == old
    assert finished[-1]["round"] == 2 and finished[-1]["result"]["descriptions"]


B17_SENTINEL = "B17-PRIVATE-ROUTER-MESSAGE-720493"


def b17_retry():
    return {
        "version": "role_call_retry/1",
        "max_attempts": 2,
        "total_budget_seconds": 30,
        "backoff": {"initial_seconds": 0, "multiplier": 1, "max_seconds": 0, "jitter": "full"},
    }


def b17_role(section, role, **extra):
    return {
        **section,
        "model": f"b17-{role}",
        "provider_only": ["Pinned"],
        "retry": b17_retry(),
        **extra,
    }


def b17_reflector(document, section):
    common = {
        key: value
        for key, value in document["reflector"].items()
        if key in {"environment_docs", "families", "max_consecutive_failures", "probe", "selection"}
    }
    document["reflector"] = {**common, **section}


#: A DeepSeek-style markup call (#399): `arguments` that are not JSON at all.
DSML = (
    '<｜DSML｜function_calls><｜DSML｜invoke name="list_files">'
    "</｜DSML｜invoke></｜DSML｜function_calls>"
)


def broken_first_answer(body):
    """The first session answer of a role, holding a broken call (b18 §3).

    The Editor answers with DSML, the Reflector with a valid call followed by
    a skipped broken one, a Grouper portion with `NaN` in its arguments.
    """
    from tests import fake_chat_server as fake

    calls = {
        "b17-editor": [("call-dsml", "list_files", DSML)],
        "b17-reflector": [
            ("call-first", "list_files", "{}"),
            ("call-skipped", "read_file", '{"path": "a'),
        ],
        "b17-grouper": [("call-nan", "finish_portion", '{"groups": NaN}')],
    }[body["model"]]
    answer = fake.tool_call("list_files")
    answer["choices"][0]["message"]["tool_calls"] = [
        {"id": id_, "type": "function", "function": {"name": name, "arguments": raw}}
        for id_, name, raw in calls
    ]
    return answer


def b17_provider(*, refuse=True, all_refused=False, strict=None, broken_first=False):
    """The b17 chat roles' provider; b18 adds a strict mode and broken first answers.

    `strict` is a `tests/strict_history.py` mode; `broken_first` (`True`, or
    the role models it applies to) answers each role's first session request
    (not its preflight, not a consolidation batch) with `broken_first_answer`,
    before the ordinary script is consulted.
    """
    import threading
    from itertools import cycle

    from tests import fake_chat_server as fake
    from tests.model_grouping_fakes import PortionGrouper
    from tests.test_tree_campaign_family14_role_feedback import _chat_plan, _chat_write
    from tests.test_tree_campaign_family15_role_failures import CHAT_GROUP

    edit = cycle(
        [
            _chat_plan(["skills/SKILL.md"]),
            _chat_write("skills/SKILL.md", "edited\n"),
            fake.tool_call("finish_edit", {"summary": "done"}),
        ]
    )
    grouper = PortionGrouper()
    refusal = fake.router_refusal(503, B17_SENTINEL, "404")

    first = set()
    lock = threading.Lock()

    def normal(body):
        tools = [t["function"]["name"] for t in body.get("tools", [])]
        chosen = broken_first is True or body["model"] in (broken_first or ())
        if chosen and tools not in ([], ["list_files"], ["list_case"]):
            with lock:
                fresh = body["model"] not in first
                first.add(body["model"])
            if fresh:
                return {**broken_first_answer(body), "model": body["model"], "provider": "Pinned"}
        if body["model"] == "b17-grouper":
            content, calls = grouper._answer(body)
            answer = fake.tool_call(*calls[0]) if calls else fake.completion(content)
        elif tools == ["list_files"]:
            answer = fake.tool_call("list_files")
        elif body["model"] == "b17-editor":
            answer = next(edit)
        else:
            answer = fake.tool_call("finish_reflection", {"groups": [CHAT_GROUP]})
        return {**answer, "model": body["model"], "provider": "Pinned"}

    handler = (
        (lambda body: refusal)
        if all_refused
        else fake.refuse_parallel(normal, refusal)
        if refuse
        else normal
    )
    return fake.start([handler], strict=strict)


def b17_logs(run, directory):
    import json

    return [
        json.loads(line)
        for path in (run / directory).rglob("*.jsonl")
        for line in path.read_text().splitlines()
        if line
    ]


@pytest.mark.parametrize("role", ["reflector", "editor", "grouper"])
async def test_v16_chat_role_settles_in_modern_single_on_383_body(tmp_path, monkeypatch, role):
    from tests.model_grouping_fakes import chat_grouper_section
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section

    base, server = b17_provider()
    monkeypatch.setenv("E1_KEY", "test-key")
    monkeypatch.setenv("GROUPER_KEY", "test-key")

    def configure(document):
        if role == "reflector":
            b17_reflector(document, b17_role(chat_reflector(base), role))
        elif role == "editor":
            document[role] = b17_role(chat_budget_section(base), role)
        else:
            document[role] = b17_role(
                chat_grouper_section(base_url=base, api_key_env="GROUPER_KEY"), role
            )

    try:
        run, result = await public_run(
            tmp_path, monkeypatch, configure=configure, campaign={"rounds": 1}
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.terminal == "finished", (result.owner, result.code, result.suspended)
    records = b17_logs(run, "editor-logs" if role == "editor" else "reflector-logs")
    assert any(
        r.get("phase") == "preflight" and r.get("request_style") == "modern_single" for r in records
    )
    assert sum(r.get("phase") == "provider_refused" for r in records) == 2
    assert not any(r.get("phase") == "identity_mismatch" for r in records)


def b17_observe_reflections(monkeypatch):
    notes = []
    real = campaign_module.run_reflector

    async def observe(*a, **kw):
        outcome = await real(*a, **kw)
        notes.append(outcome.note)
        return outcome

    monkeypatch.setattr(campaign_module, "run_reflector", observe)
    return notes


async def test_v15_chat_reflector_keeps_provider_changed_on_383_body(tmp_path, monkeypatch):
    from tests import fake_chat_server as fake
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family15_role_failures import campaign_run

    role_notes = b17_observe_reflections(monkeypatch)
    base, server = fake.start([fake.router_refusal(503, B17_SENTINEL)])
    monkeypatch.setenv("E1_KEY", "test-key")
    try:
        run, result = await campaign_run(
            14,
            tmp_path,
            monkeypatch,
            reflector_section=b17_role(chat_reflector(base), "reflector"),
            campaign={"rounds": 1},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.terminal == "suspended" and result.code == "reflector_failing"
    assert any("provider_changed" in note for note in role_notes)


async def test_router_message_stays_in_private_role_log(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from evolution.tree import evolve as evolve_module
    from evolution.tree.cli import tree_app
    from evolution.tree.process_log import capture
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector

    base, server = b17_provider(all_refused=True)
    monkeypatch.setenv("E1_KEY", "test-key")
    real = evolve_module.evolve
    role_notes = b17_observe_reflections(monkeypatch)
    # Capture a genuine process-log sink and the engine's notes, as the CLI does.
    with capture("evolve", prefix="evo tree evolve") as process:

        async def with_log(path, *a, **kw):
            return await real(
                path, *a, **kw, notes=lambda text: print(text), process_log=process.materialize
            )

        monkeypatch.setattr("tests.test_tree_campaign_family15_delivery.evolve", with_log)
        try:
            run, result = await public_run(
                tmp_path,
                monkeypatch,
                configure=lambda d: b17_reflector(d, b17_role(chat_reflector(base), "reflector")),
                campaign={"rounds": 1},
            )
        finally:
            server.shutdown()
            server.server_close()
    assert result.terminal == "suspended"
    records = b17_logs(run, "reflector-logs")
    assert any(B17_SENTINEL in json.dumps(r) for r in records)
    assert all(r["phase"] == "provider_refused" for r in records if B17_SENTINEL in json.dumps(r))
    assert any("router refused the request (status 200, code 503" in note for note in role_notes)
    assert B17_SENTINEL not in (run / "campaign.jsonl").read_text()
    process_files = list(run.rglob("*.log"))
    assert process_files
    assert all(B17_SENTINEL not in p.read_text() for p in process_files)
    result_json = json.dumps(
        dict(
            terminal=result.terminal,
            quiescent=result.quiescent,
            finished=result.finished,
            suspended=result.suspended,
            owner=result.owner,
            code=result.code,
            result=None if result.result is None else result.result.to_payload(),
            status=result.status.to_payload(),
        )
    )
    assert B17_SENTINEL not in result_json
    assert B17_SENTINEL not in str(result.provider_attempt_lines)
    watched = CliRunner().invoke(tree_app, ["watch", "--run-dir", str(run), "--once"])
    assert watched.exit_code == 0 and B17_SENTINEL not in watched.stdout


async def test_require_parameters_false_reaches_every_provider_object(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.model_grouping_fakes import chat_grouper_section
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section

    base, server = b17_provider(refuse=False)
    monkeypatch.setenv("E1_KEY", "test-key")
    monkeypatch.setenv("GROUPER_KEY", "test-key")

    def configure(d):
        d["editor"] = b17_role(chat_budget_section(base), "editor", require_parameters=False)
        b17_reflector(d, b17_role(chat_reflector(base), "reflector", require_parameters=False))
        d["grouper"] = b17_role(
            chat_grouper_section(base_url=base, api_key_env="GROUPER_KEY"),
            "grouper",
            require_parameters=False,
            group_output_tokens=8192,
        )

    # Both cases fail on the baseline; capacity=1 forces two portions and a batch.
    scorer = TWO_CASE_SCORER.replace('or bool(payload["oracle"].get("pass"))', "")
    try:
        _, result = await public_run(
            tmp_path, monkeypatch, configure=configure, scorer=scorer, campaign={"rounds": 1}
        )
    finally:
        server.shutdown()
        server.server_close()
    requests = server.script.requests
    assert result.terminal == "finished", result.suspended
    assert {r["model"] for r in requests} == {"b17-editor", "b17-reflector", "b17-grouper"}
    assert any(r["model"] == "b17-grouper" and not r.get("tools") for r in requests)
    assert all(r["provider"]["require_parameters"] is False for r in requests)


async def test_frozen_editor_common_input_survives_prompt_identity_rebase(tmp_path, monkeypatch):
    """A declared-to-packaged prompt rebase preserves a frozen common input.

    Both legs run current code; only the producing-version stamp is synthetic.
    """
    import evolution
    from evolution.prompts import TREE_EDITOR_SYSTEM
    from evolution.tree.execution import journal_identity
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _records
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section

    base, server = b17_provider(refuse=False)
    monkeypatch.setenv("E1_KEY", "test-key")
    prompt = tmp_path / "declared-editor.md"
    prompt.write_text(TREE_EDITOR_SYSTEM + "\n")
    installed = evolution.__version__

    async def run(declared, version, **options):
        def configure(d):
            d["editor"] = b17_role(chat_budget_section(base), "editor")
            if declared:
                d["editor"]["system_prompt"] = str(prompt)

        with monkeypatch.context() as patch:
            patch.setattr(campaign_module, "resolve_package_version", lambda: version)
            patch.setattr(journal_identity.metadata, "version", lambda name: installed)
            return await public_run(
                tmp_path, patch, configure=configure, campaign={"rounds": 1}, **options
            )

    try:
        with pytest.raises(PowerCut):
            await run(True, "2.0.0b15", cut="after_editor_started")
        before = _records(tmp_path / "run/campaign.jsonl")
        frozen = next(
            r["payload"]["common_input_digest"] for r in before if r["event"] == "editor_started"
        )
        assert server.script.requests == []
        with pytest.raises(EvolveRefused) as caught:
            await run(False, installed)
        assert caught.value.effects == ("rebase",)
        assert sorted(d.field for d in caught.value.differences) == [
            "identity.editor.digest",
            "producing_version",
        ]
        accepted = Authorization.new(
            "run_identity_changed",
            caught.value.instance_digest,
            "accept packaged editor prompt",
            now="2026-11-02T12:00:00Z",
        )
        directory, result = await run(False, installed, authorizations=[accepted])
    finally:
        server.shutdown()
        server.server_close()
    assert result.terminal == "finished", result.suspended
    records = _records(directory / "campaign.jsonl")
    starts = [r["payload"] for r in records if r["event"] == "editor_started"]
    assert starts and all(start["common_input_digest"] == frozen for start in starts)
    assert sum(r["event"] == "identity_rebased" for r in records) == 1


async def test_reflection_started_before_b17_identity_resumes_after_rebase(tmp_path, monkeypatch):
    import hashlib

    from evolution.prompts import TREE_REFLECTOR_SYSTEM_V4
    from evolution.tree import evolve as evolve_module
    from evolution.tree.execution.authorizations import Authorization
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles import seals
    from evolution.tree.roles.seals import FileSeal
    from tests.campaign_drivers import _records
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family15_role_failures import (
        PowerCut as ReflectionCut,
    )
    from tests.test_tree_campaign_family15_role_failures import (
        chat_campaign,
    )

    base, server = b17_provider(refuse=False)
    real_section = chat_reflector
    monkeypatch.setattr(
        "tests.test_tree_campaign_family14_reflector_history.chat_reflector",
        lambda url: b17_role(real_section(url), "reflector"),
    )
    real_build = evolve_module.build_reflector

    def old_identity(section, prompt, *, result_repair=False, family15=False):
        # Exact b16 payload: no role_contracts/1. Other versioned keys stay.
        value = {
            "kind": "chat",
            "section": seals.section_payload(section),
            "system_prompt_sha256": prompt.sha256,
            "tool_schema": seals.REFLECT_TOOL_SCHEMA_V16,
            "request_version": seals.REFLECT_REQUEST_VERSION,
            "reflect_brief": "reflect_brief/6",
            "finish_reflection": "result_repair/1",
        }
        return seals.ReflectorIdentity(
            kind="chat", digest=hashlib.sha256(canonical_json(value)).hexdigest()
        )

    def old_build(*a, **kw):
        from dataclasses import replace

        role = real_build(*a, **kw)
        text = TREE_REFLECTOR_SYSTEM_V4
        return replace(role, prompt=FileSeal(None, hashlib.sha256(text.encode()).hexdigest(), text))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(evolve_module, "build_reflector", old_build)
            patch.setattr(
                "evolution.tree.roles.reflector.chat_reflector.chat_reflector_identity",
                old_identity,
            )
            with pytest.raises(ReflectionCut):
                await chat_campaign(
                    15,
                    tmp_path,
                    monkeypatch,
                    [],
                    version="2.0.0b15",
                    server=(base, server),
                    cut="after_reflection_started",
                )
        assert not server.script.requests
        with pytest.raises(EvolveRefused) as caught:
            await chat_campaign(15, tmp_path, monkeypatch, [], server=(base, server))
        assert caught.value.effects == ("rebase",)
        assert sorted(d.field for d in caught.value.differences) == [
            "identity.reflector.digest",
            "producing_version",
        ]
        accepted = Authorization.new(
            "run_identity_changed",
            caught.value.instance_digest,
            "accept b17 reflector contracts",
            now="2026-11-02T12:00:00Z",
        )
        directory, result, _ = await chat_campaign(
            15, tmp_path, monkeypatch, [], server=(base, server), authorizations=[accepted]
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.terminal == "finished", result.suspended
    records = _records(directory / "campaign.jsonl")
    assert sum(r["event"] == "identity_rebased" for r in records) == 1


# -- b18 (ADR-0061, spec §3 and §7): strict providers, the upgrade and its rebase --


def b18_chat_roles(base):
    """`configure` for a chat Editor, chat Reflector and chat Grouper on `base`."""
    from tests.model_grouping_fakes import chat_grouper_section
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section

    def configure(d):
        d["editor"] = b17_role(chat_budget_section(base), "editor")
        b17_reflector(d, b17_role(chat_reflector(base), "reflector"))
        d["grouper"] = b17_role(
            chat_grouper_section(base_url=base, api_key_env="GROUPER_KEY"),
            "grouper",
            group_output_tokens=8192,
        )

    return configure


def b18_role_records(run):
    """Every private role-log record of `run`, keyed by its log path under `run`."""
    import json

    return {
        path.relative_to(run).as_posix(): [
            json.loads(line) for line in path.read_text().splitlines() if line
        ]
        for directory in ("editor-logs", "reflector-logs")
        for path in sorted((run / directory).rglob("*.jsonl"))
    }


@pytest.mark.parametrize("mode", ["http_400", "error_body"])
async def test_v16_chat_roles_finish_against_strict_provider(tmp_path, monkeypatch, mode):
    from tests.strict_history import broken_history_calls, measures

    base, server = b17_provider(refuse=False, strict=mode, broken_first=True)
    monkeypatch.setenv("E1_KEY", "test-key")
    monkeypatch.setenv("GROUPER_KEY", "test-key")
    try:
        run, result = await public_run(
            tmp_path, monkeypatch, configure=b18_chat_roles(base), campaign={"rounds": 1}
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.terminal == "finished", (result.owner, result.code, result.suspended)
    # The strict provider never saw a broken call, so it refused nothing.
    assert server.script.refusals == []
    assert server.script.requests
    assert all(broken_history_calls(r) == [] for r in server.script.requests)
    logs = b18_role_records(run)
    replaced = {
        path: [(r["phase"], r["replaced_calls"]) for r in records if "replaced_calls" in r]
        for path, records in logs.items()
    }
    repairs = {
        path: [r for r in records if r.get("phase") == "protocol_repair_requested"]
        for path, records in logs.items()
    }
    (editor,) = [path for path in logs if path.startswith("editor-logs/")]
    (grouping,) = [path for path in logs if "/grouping-" in path]
    (reflection,) = [path for path in logs if path.startswith("reflector-logs/r01-")]

    def call(id_, name, raw):
        return [{"id": id_, "name": name, **measures(raw)}]

    # A broken first call ends its turn in one repair whose record names it.
    assert len(repairs[editor]) == 1
    assert replaced[editor] == [
        ("protocol_repair_requested", call("call-dsml", "list_files", DSML))
    ]
    assert len(repairs[grouping]) == 1
    assert replaced[grouping] == [
        ("protocol_repair_requested", call("call-nan", "finish_portion", '{"groups": NaN}'))
    ]
    # A skipped broken call needs no repair: the records after the turn name it.
    assert repairs[reflection] == []
    skipped = call("call-skipped", "read_file", '{"path": "a')
    assert replaced[reflection] == [("parallel_calls_dropped", skipped), ("tool_call", skipped)]


@pytest.mark.parametrize("role", ["reflector", "editor"])
async def test_v15_chat_roles_against_strict_provider_fail_as_b17(tmp_path, monkeypatch, role):
    from evolution.tree.roles.editor.chat_editor import ChatEditor
    from evolution.tree.roles.editor_contracts import outcome_class
    from evolution.tree.roles.reflector.chat_reflector import ChatReflector
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section
    from tests.test_tree_campaign_family15_role_failures import campaign_run
    from tests.tree.roles.test_chat_loop_broken_calls_golden import (
        V15_STRICT_REFUSAL_CLASS_EDITOR,
        V15_STRICT_REFUSAL_CLASS_REFLECTOR,
    )

    ended = {}
    for cls, name in ((ChatReflector, "reflect"), (ChatEditor, "edit")):
        real = getattr(cls, name)

        async def observe(self, request, *, _real=real, _role=cls.__name__):
            result = await _real(self, request)
            ended.setdefault(_role, []).append((outcome_class(result), result.code))
            return result

        monkeypatch.setattr(cls, name, observe)
    base, server = b17_provider(refuse=False, strict="http_400", broken_first={f"b17-{role}"})
    monkeypatch.setenv("E1_KEY", "test-key")
    try:
        run, result = await campaign_run(
            14,
            tmp_path,
            monkeypatch,
            editor_section=b17_role(chat_budget_section(base), "editor"),
            reflector_section=b17_role(chat_reflector(base), "reflector"),
            campaign={"rounds": 1},
        )
    finally:
        server.shutdown()
        server.server_close()
    # `/15` sends the broken call back as is; the strict provider refuses it.
    broken = {"reflector": "call-skipped", "editor": "call-dsml"}[role]
    assert server.script.refusals == [[broken]]
    pinned_class = {
        "reflector": V15_STRICT_REFUSAL_CLASS_REFLECTOR,
        "editor": V15_STRICT_REFUSAL_CLASS_EDITOR,
    }[role]
    # A0 pinned the code of a role section without `retry`; a campaign role
    # always declares one (`role_call_retry/1`, required by `/15`), and a
    # non-transient HTTP 400 then ends the retry as `provider_rejected`.
    failed = "ChatReflector" if role == "reflector" else "ChatEditor"
    assert ended[failed] == [(pinned_class, "provider_rejected")]
    assert (result.terminal, result.owner, result.code) == (
        "suspended",
        role,
        f"{role}_provider_failed",
    )


#: The producing versions of the two sides of the upgrade; the installed
#: package still says b17 on this branch, so both are stamped.
B17_VERSION = "2.0.0b17"
B18_VERSION = "2.0.0b18"
#: The three `/16` chat role identity fields spec §7 expects a b17 run to list.
CHAT_ROLE_FIELDS = ["identity.editor.digest", "identity.reflector.digest", "producing_version"]


@contextmanager
def b18_leg(monkeypatch, *, b17):
    """Run as b17 (its role identities and version) or as b18.

    b17 is simulated by the three module globals b18 changed (each read at
    call time): `role_contracts/1` and the b17 `/16` default Editor prompts.
    """
    import evolution
    from evolution import prompts
    from evolution.tree import bindings
    from evolution.tree.execution import journal_identity
    from evolution.tree.roles import seals

    installed = evolution.__version__
    with monkeypatch.context() as patch:
        if b17:
            patch.setattr(seals, "ROLE_CONTRACTS_V16", "role_contracts/1")
            patch.setattr(bindings, "TREE_EDITOR_SYSTEM_V3", prompts.TREE_EDITOR_SYSTEM_V2)
            patch.setattr(bindings, "TREE_AGENT_EDITOR_SYSTEM_V2", prompts.TREE_AGENT_EDITOR_SYSTEM)
        version = B17_VERSION if b17 else B18_VERSION
        patch.setattr(campaign_module, "resolve_package_version", lambda: version)
        patch.setattr(journal_identity.metadata, "version", lambda name: installed)
        yield patch


async def b18_upgrade(monkeypatch, root, run, *, cut, power_cut=PowerCut):
    """Cut `run` under b17, have b18 refuse it once, accept one `rebase` and finish.

    `run(patch, **options)` drives the campaign at `root`; `cut` is a hook
    name (passed as `cut=`) or a callable installing its own cut on `patch`.
    Returns the refused fields, the journal records before the upgrade and
    the finished run's records.
    """
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _records

    with b18_leg(monkeypatch, b17=True) as patch:
        options = {"cut": cut} if isinstance(cut, str) else (cut(patch) or {})
        with pytest.raises(power_cut):
            await run(patch, **options)
    journal = root / "run/campaign.jsonl"
    before = journal.read_bytes()
    with b18_leg(monkeypatch, b17=False) as patch:
        with pytest.raises(EvolveRefused) as caught:
            await run(patch)
    assert journal.read_bytes() == before
    refused = caught.value
    assert refused.code == "run_identity_changed"
    assert refused.effects == ("rebase",)
    accepted = Authorization.new(
        "run_identity_changed", refused.instance_digest, "accept b18", now="2026-11-08T12:00:00Z"
    )
    with b18_leg(monkeypatch, b17=False) as patch:
        directory, result = await run(patch, authorizations=[accepted])
    assert result.terminal == "finished", (result.owner, result.code, result.suspended)
    records = _records(directory / "campaign.jsonl")
    assert sum(r["event"] == "identity_rebased" for r in records) == 1
    fields = sorted(d.field for d in refused.differences)
    return fields, _records_of(before), records


def _records_of(raw):
    import json

    return [json.loads(line) for line in raw.decode().splitlines() if line]


def b18_portion_requests(server):
    """The chat Grouper's portion-session requests the server received, in order."""
    return [
        r
        for r in server.script.requests
        if r["model"] == "b17-grouper"
        and "finish_portion" in [t["function"]["name"] for t in r.get("tools") or ()]
    ]


async def test_b17_run_rebases_once_with_expected_fields(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family15_grouping_checkpoints import (
        FAILING,
        PORTION_EVENT,
        PORTIONS,
        _cut_after,
    )

    base, server = b17_provider(refuse=False)
    monkeypatch.setenv("E1_KEY", "test-key")
    monkeypatch.setenv("GROUPER_KEY", "test-key")
    asked = []

    async def run(patch, **options):
        try:
            return await public_run(
                tmp_path,
                patch,
                configure=b18_chat_roles(base),
                extra_cases=FAILING,
                campaign={"rounds": 1},
                **options,
            )
        finally:
            asked.append(len(b18_portion_requests(server)))

    try:
        fields, before, records = await b18_upgrade(
            monkeypatch,
            tmp_path,
            run,
            cut=lambda patch: _cut_after(patch, "after_grouping_portion_finished", 2),
        )
    finally:
        server.shutdown()
        server.server_close()
    # Spec §7: the chat Editor and chat Reflector identities, no Grouper field.
    assert fields == CHAT_ROLE_FIELDS
    cut = [r["payload"]["portion_id"] for r in before if r["event"] == PORTION_EVENT]
    assert sorted(cut) == ["p1", "p2"]
    assert not any(r["event"] == "grouping_finished" for r in before)
    # The interrupted grouping keeps its checkpoints: no finished portion is
    # requested again, by the refused leg or the resumed one.
    first, second = [
        r["payload"]["grouping_id"] for r in records if r["event"] == "grouping_started"
    ]
    resumed = [
        r["payload"]["portion_id"]
        for r in records
        if r["event"] == PORTION_EVENT and r["payload"]["grouping_id"] == second
    ]
    assert sorted(resumed) == ["p3", "p4"]
    assert asked == [2, 2, PORTIONS]
    (failed,) = [r["payload"] for r in records if r["event"] == "grouping_failed"]
    assert (failed["grouping_id"], failed["code"]) == (first, "interrupted")
    # The resumed grouping's terminal asked two portions itself and still
    # assigns every failing case, `c1` and `c3` from the adopted p1 and p2;
    # only `c2`, which passes, is unassigned.
    (finished,) = [r["payload"] for r in records if r["event"] == "grouping_finished"]
    assert finished["grouping_id"] == second
    assert finished["calls"]["map_requests"] == 2
    assignments = finished["result"]["assignments"]
    assert [index for index, group in enumerate(assignments) if group is None] == [1]


async def test_attempt_frozen_mid_editor_resumes_after_b18_rebase(tmp_path, monkeypatch):
    base, server = b17_provider(refuse=False)
    monkeypatch.setenv("E1_KEY", "test-key")
    monkeypatch.setenv("GROUPER_KEY", "test-key")

    async def run(patch, **options):
        return await public_run(
            tmp_path, patch, configure=b18_chat_roles(base), campaign={"rounds": 1}, **options
        )

    try:
        fields, before, records = await b18_upgrade(
            monkeypatch, tmp_path, run, cut="after_editor_started"
        )
    finally:
        server.shutdown()
        server.server_close()
    assert fields == CHAT_ROLE_FIELDS
    (frozen,) = [r["payload"] for r in before if r["event"] == "editor_started"]
    attempt = frozen["attempt_id"]
    # The same Attempt is dispatched again with its frozen inputs; it is not
    # abandoned for a fresh one.
    starts = [r["payload"] for r in records if r["event"] == "editor_started"]
    assert len(starts) == 2
    resumed = starts[1]
    assert resumed["attempt_id"] == attempt
    for key in ("input_digest", "common_input_digest", "brief_digest"):
        assert resumed[key] == frozen[key], key
    assert resumed["operation_id"] != frozen["operation_id"]
    inputs = [r["payload"] for r in records if r["event"] == "attempt_input_frozen"]
    assert [p["attempt_id"] for p in inputs] == [attempt]
    assert inputs[0]["input_digest"] == frozen["input_digest"]
    assert [r["payload"]["attempt_id"] for r in records if r["event"] == "attempt_opened"] == [
        attempt
    ]
    assert [
        r["payload"]["operation_id"]
        for r in records
        if r["event"] == "editor_finished" and r["payload"]["attempt_id"] == attempt
    ] == [resumed["operation_id"]]


async def test_reflection_open_before_b18_resumes_after_rebase(tmp_path, monkeypatch):
    base, server = b17_provider(refuse=False)
    monkeypatch.setenv("E1_KEY", "test-key")
    monkeypatch.setenv("GROUPER_KEY", "test-key")

    async def run(patch, **options):
        return await public_run(
            tmp_path, patch, configure=b18_chat_roles(base), campaign={"rounds": 1}, **options
        )

    try:
        fields, before, records = await b18_upgrade(
            monkeypatch, tmp_path, run, cut="after_reflection_started"
        )
    finally:
        server.shutdown()
        server.server_close()
    assert fields == CHAT_ROLE_FIELDS
    (opened,) = [r["payload"] for r in before if r["event"] == "reflection_started"]
    # The engine settles the open reflection `interrupted` and reflects again,
    # in the same round, over the same frozen input; the grouping it reflects
    # on is not redone.
    started = [r["payload"] for r in records if r["event"] == "reflection_started"]
    assert len(started) == 2 and started[0] == opened
    again = started[1]
    assert (again["round"], again["input_digest"]) == (opened["round"], opened["input_digest"])
    assert again["reflection_id"] != opened["reflection_id"]
    settled = {
        r["payload"]["reflection_id"]: (r["event"], r["payload"].get("error_class"))
        for r in records
        if r["event"] in ("reflection_failed", "reflection_finished")
    }
    assert settled == {
        opened["reflection_id"]: ("reflection_failed", "interrupted"),
        again["reflection_id"]: ("reflection_finished", None),
    }
    assert sum(r["event"] == "grouping_started" for r in records) == 1


async def test_v16_agent_editor_default_prompt_rebase_lists_editor_digest(tmp_path, monkeypatch):
    from evolution.tree.roles.editor import agent_editor as agent_editor_module
    from tests.test_tree_evolve import FakeAgentBackend, agent_editor_section

    monkeypatch.setattr(agent_editor_module, "AgentBackend", FakeAgentBackend)

    def configure(d):
        d["editor"] = agent_editor_section(tmp_path)

    async def run(patch, **options):
        return await public_run(
            tmp_path, patch, configure=configure, campaign={"rounds": 1}, **options
        )

    fields, _, records = await b18_upgrade(
        monkeypatch, tmp_path, run, cut="after_attempt_input_frozen"
    )
    assert fields == ["identity.editor.digest", "producing_version"]
    assert any(r["event"] == "editor_finished" for r in records)


async def test_v16_subprocess_roles_rebase_lists_producing_version_only(tmp_path, monkeypatch):
    async def run(patch, **options):
        return await public_run(tmp_path, patch, campaign={"rounds": 1}, **options)

    fields, _, _ = await b18_upgrade(monkeypatch, tmp_path, run, cut="after_attempt_input_frozen")
    assert fields == ["producing_version"]


async def test_v15_run_rebase_lists_producing_version_only(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family14_role_feedback import chat_budget_section
    from tests.test_tree_campaign_family15_role_failures import PowerCut as V15Cut
    from tests.test_tree_campaign_family15_role_failures import campaign_run

    base, server = b17_provider(refuse=False)
    monkeypatch.setenv("E1_KEY", "test-key")

    async def run(patch, **options):
        return await campaign_run(
            14,
            tmp_path,
            patch,
            editor_section=b17_role(chat_budget_section(base), "editor"),
            reflector_section=b17_role(chat_reflector(base), "reflector"),
            campaign={"rounds": 1},
            **options,
        )

    try:
        fields, _, _ = await b18_upgrade(
            monkeypatch, tmp_path, run, cut="after_attempt_input_frozen", power_cut=V15Cut
        )
    finally:
        server.shutdown()
        server.server_close()
    # The chat roles of a `/15` run keep their b17 identities (Parts A and D are `/16` only).
    assert fields == ["producing_version"]
