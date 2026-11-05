"""Family-15 role settings, refusals, privacy and frozen-input upgrades."""

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


def b17_provider(*, refuse=True, all_refused=False):
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

    def normal(body):
        tools = [t["function"]["name"] for t in body.get("tools", [])]
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
    return fake.start([handler])


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
