"""`evolve` up to the campaign entry for a `/15` manifest (family-14 design
R15, R17-R19, M1; A§10.6; C§9.3). No family-14 campaign runs in slice H1:
these tests stop at `run_campaign`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pytest

from evolution.tree import evolve as evolve_module
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.execution.authorizations import (
    comparable_changed,
    hygiene_instance_digest,
    identity_differences,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.output_admission import EvolveRefused
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15, SCHEMA_V16, load_manifest_v2
from evolution.tree.roles.config import ModelGroupingSection
from tests.campaign_fakes import family14_admitted
from tests.tree.manifest.test_manifest_v15 import CHAT_GROUPER, COMMAND_GROUPER, v15_from
from tests.tree_evolve_support import write_workspace

KEY = "sk-grouper-0123456789abcdef"


class Reached(Exception):
    """The fake `run_campaign` was called: every step before it passed."""


def workspace(tmp_path, **kwargs):
    path = write_workspace(tmp_path)
    document = v15_from(json.loads(path.read_text()), **kwargs)
    path.write_text(json.dumps(document))
    return path


async def captured(tmp_path, monkeypatch, path, *, environ=None, run="run"):
    seen: dict = {}

    async def entry(policy, **kwargs):
        seen.update(kwargs, policy=policy)
        raise Reached

    monkeypatch.setattr(evolve_module, "run_campaign", entry)
    with pytest.raises(Reached):
        await evolve_module.evolve(
            path,
            tmp_path / run,
            adapter_factory=lambda *a, **k: object(),
            environ={**os.environ, **(environ or {})},
        )
    assert not (tmp_path / run / "campaign.jsonl").exists()
    return seen


# -- R15: admission and current configuration wiring --------------------------------


def test_current_campaign_admission():
    # PDR-0038: the real producer admits exactly the two current manifests.
    assert evolve_module.CAMPAIGN_SCHEMAS == (SCHEMA_V15, SCHEMA_V16)


@pytest.mark.parametrize("schema", [SCHEMA_V15, SCHEMA_V16])
async def test_current_campaigns_enable_diagnostics_resilience_and_bounds_repair(
    tmp_path, monkeypatch, schema
):
    from evolution.prompts import TREE_REFLECTOR_SYSTEM_V3, TREE_REFLECTOR_SYSTEM_V5
    from tests.family15_fakes import v16_document
    from tests.tree.manifest.test_manifest_v15 import BOTH, chat_document

    path = workspace(tmp_path)
    document = json.loads(path.read_text())
    chat = chat_document(**BOTH)
    document.update(schema=schema, editor=chat["editor"], reflector=chat["reflector"])
    if schema == SCHEMA_V16:
        document["campaign"] = v16_document()["campaign"]
    path.write_text(json.dumps(document))
    seen = await captured(
        tmp_path,
        monkeypatch,
        path,
        environ={document[role]["api_key_env"]: KEY for role in ("editor", "reflector")},
    )
    assert seen["scorer"].accepts_diagnostics is True
    assert seen["editor"].role_resilience is True
    assert seen["reflector"].role_resilience is True
    assert seen["reflector"].bounds_repair is True
    # #318 (ADR-0048): only `/16` chat checks `finish_reflection` in role and
    # b17 sends V5 with that contract; `/15` keeps V3 and its identity.
    assert seen["reflector"].result_repair is (schema == SCHEMA_V16)
    packaged = TREE_REFLECTOR_SYSTEM_V5 if schema == SCHEMA_V16 else TREE_REFLECTOR_SYSTEM_V3
    assert seen["reflector"].prompt.sha256 == hashlib.sha256(packaged.encode()).hexdigest()
    assert seen["adaptive"].identity.profile == "campaign_adaptation/7"
    assert seen["adaptive"].identity.to_payload()["corpus_partition"]["mode"] == "implicit_train"


async def test_evolve_produces_family_14_from_a_v15_manifest(tmp_path, monkeypatch):
    from tests.family14_public import family14_public_run

    run, _ = await family14_public_run(tmp_path, monkeypatch)
    first = json.loads((run / "campaign.jsonl").read_text().splitlines()[0])
    assert first["payload"]["family_version"] == 14


# -- R17, R18: the campaign entry receives a family-14 input set ------------------------


async def test_evolve_builds_the_family_14_inputs(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    path = workspace(tmp_path)
    seen = await captured(tmp_path, monkeypatch, path)
    assert type(seen["policy"]) is CampaignPolicyV11
    adaptive = seen["adaptive"]
    assert adaptive.identity.profile == "campaign_adaptation/7"
    grouping = adaptive.identity.to_payload()["grouping"]
    assert grouping["kind"] == "fields"
    assert (
        adaptive.policy_digest
        == hashlib.sha256(
            canonical_json({"version": "reflect_grouping/1", "grouping": grouping})
        ).hexdigest()
    )
    manifest = load_manifest_v2(path)
    assert manifest.workflow is not None
    assert seen["connection"] == manifest.workflow.connection


# -- H10 review r1 item 2: `connection-logs` is a family-14 output root only ---------------


@pytest.mark.parametrize("root_name", ["connection-logs", "logs"])
async def test_a_family_14_run_dir_admits_and_guards_log_roots(tmp_path, monkeypatch, root_name):
    family14_admitted(monkeypatch)
    path = workspace(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    logs = run / root_name
    logs.mkdir()
    os.chmod(logs, 0o750)
    from tests.test_tree_evolve import listing

    before = listing(run)
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(path, run, adapter_factory=lambda *a, **k: object())
    assert (str(caught.value), caught.value.instance_digest) == (
        f"run_dir_unsafe_entry at {root_name}",
        hygiene_instance_digest(root_name, "not owner-only", "0o750"),
    )
    assert listing(run) == before
    os.chmod(logs, 0o700)
    await captured(tmp_path, monkeypatch, path)


# -- R19: the Grouper's values join the secret set ------------------------------------------


async def test_a_chat_grouper_key_is_a_run_secret(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    path = workspace(tmp_path, grouper=CHAT_GROUPER)
    seen = await captured(tmp_path, monkeypatch, path, environ={"GROUPER_API_KEY": KEY})
    assert KEY in seen["secrets"]
    adaptive = seen["adaptive"]
    assert isinstance(adaptive.model_grouping, ModelGroupingSection)
    assert adaptive.model_credentials.api_key == KEY
    assert adaptive.identity.to_payload()["grouping"]["kind"] == "chat"


async def test_a_chat_grouper_without_its_key_is_refused_before_any_write(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    path = workspace(tmp_path, grouper=CHAT_GROUPER)
    environ = {name: value for name, value in os.environ.items() if name != "GROUPER_API_KEY"}
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(path, tmp_path / "run", environ=environ)
    assert (caught.value.code, caught.value.path) == ("grouper_env_missing", "grouper.api_key_env")
    assert not (tmp_path / "run").exists()


# -- #290: a chat role without `api_key_env` runs unauthenticated -------------------------


def _chat_roles(tmp_path, *, undeclared=()):
    """`/15` with a chat Editor (`EDIT_KEY`) and a chat Reflector (`REFLECT_KEY`,
    at `https://models.example/v1`); `undeclared` roles omit `api_key_env`."""

    from tests.tree.manifest.test_manifest_v15 import BOTH, chat_document

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = workspace(tmp_path)
    document = json.loads(path.read_text())
    chat = chat_document(**BOTH)
    document.update(editor=chat["editor"], reflector=chat["reflector"])
    document["editor"]["api_key_env"] = "EDIT_KEY"
    document["reflector"].update(api_key_env="REFLECT_KEY", base_url="https://models.example/v1")
    for role in undeclared:
        del document[role]["api_key_env"]
    path.write_text(json.dumps(document))
    return path, document


async def test_chat_roles_without_api_key_env_are_admitted_without_credentials(
    tmp_path, monkeypatch
):
    """Design §4.1, C6: no `*_env_missing`, `api_key is None`, and no key value in
    the run's secret set, even with the variables the document used to name set."""

    path, document = _chat_roles(tmp_path, undeclared=("editor", "reflector"))
    seen = await captured(
        tmp_path, monkeypatch, path, environ={"EDIT_KEY": KEY, "REFLECT_KEY": KEY + "-r"}
    )
    assert seen["editor"].credentials.api_key is None
    assert seen["reflector"].credentials.api_key is None
    assert KEY not in seen["secrets"] and KEY + "-r" not in seen["secrets"]
    assert evolve_module.unauthenticated_roles(load_manifest_v2(path).workflow) == (
        ("editor", document["editor"]["base_url"]),
        ("reflector", "https://models.example/v1"),
    )


async def test_declared_chat_keys_keep_their_credentials_and_secrets(tmp_path, monkeypatch):
    path, _document = _chat_roles(tmp_path)
    seen = await captured(
        tmp_path, monkeypatch, path, environ={"EDIT_KEY": KEY, "REFLECT_KEY": KEY + "-r"}
    )
    assert seen["editor"].credentials.api_key == KEY
    assert seen["reflector"].credentials.api_key == KEY + "-r"
    assert KEY in seen["secrets"] and KEY + "-r" in seen["secrets"]
    assert evolve_module.unauthenticated_roles(load_manifest_v2(path).workflow) == ()


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
async def test_a_declared_key_without_a_value_is_still_refused_before_any_write(
    tmp_path, monkeypatch, role, value
):
    """G3, maintainer decision 3: absent and empty both refuse, exit 2, no write."""

    family14_admitted(monkeypatch)
    path, document = _chat_roles(tmp_path)
    if role == "grouper":
        document["grouper"] = CHAT_GROUPER
        path.write_text(json.dumps(document))
    name = document[role]["api_key_env"]
    environ = {
        **os.environ,
        "EDIT_KEY": KEY,
        "REFLECT_KEY": KEY,
        "GROUPER_API_KEY": KEY,
    }
    del environ[name]
    if value is not None:
        environ[name] = value
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(path, tmp_path / "run", environ=environ)
    assert str(caught.value) == f"{role}_env_missing at {role}.api_key_env"
    assert not (tmp_path / "run").exists()


def test_the_short_value_note_covers_the_reflector_names(tmp_path):
    """C6: a 6-character Reflector key gets the short-value note, as the
    Editor's and the Grouper's names already do."""

    path, _document = _chat_roles(tmp_path)
    assert "REFLECT_KEY" in evolve_module.short_forwarded_names(path, {"REFLECT_KEY": "abcdef"})
    assert "REFLECT_KEY" not in evolve_module.short_forwarded_names(path, {"REFLECT_KEY": KEY})


async def test_a_command_grouper_forwarded_value_is_a_run_secret(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    (tmp_path / "grouping").mkdir()
    (tmp_path / "grouping" / "group.py").write_text("print('{}')\n")
    path = workspace(tmp_path, grouper={**COMMAND_GROUPER, "dependency_paths": []})
    seen = await captured(tmp_path, monkeypatch, path, environ={"GROUPER_TOKEN": KEY})
    assert KEY in seen["secrets"]
    assert seen["adaptive"].identity.to_payload()["grouping"]["kind"] == "command"
    assert "GROUPER_TOKEN" not in evolve_module.short_forwarded_names(path, {"GROUPER_TOKEN": KEY})
    assert "GROUPER_TOKEN" in evolve_module.short_forwarded_names(path, {"GROUPER_TOKEN": "1"})


async def test_an_agent_grouper_program_and_values_are_bound(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    (tmp_path / "bin").mkdir()
    program = tmp_path / "bin" / "codex"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    grouper = {
        "kind": "agent_cli",
        "model": "codex",
        "timeout_seconds": 600,
        "agent_bin": "bin/codex",
        "allowed_env": ["AGENT_TOKEN"],
    }
    seen = await captured(
        tmp_path, monkeypatch, workspace(tmp_path, grouper=grouper), environ={"AGENT_TOKEN": KEY}
    )
    assert KEY in seen["secrets"]
    slot = seen["adaptive"].identity.to_payload()["grouping"]
    assert slot["program_sha256"] == hashlib.sha256(program.read_bytes()).hexdigest()
    assert seen["adaptive"].agent_grouping is not None


# -- A§10.6: a Grouper change is an adaptation difference -----------------------------------


async def test_changing_a_grouper_key_changes_only_the_adaptation(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    first = await captured(
        tmp_path,
        monkeypatch,
        workspace(tmp_path, grouper=CHAT_GROUPER),
        environ={"GROUPER_API_KEY": KEY},
        run="a",
    )
    changed = {**CHAT_GROUPER, "concurrency": 2}
    second = await captured(
        tmp_path,
        monkeypatch,
        workspace(tmp_path, grouper=changed),
        environ={"GROUPER_API_KEY": KEY},
        run="b",
    )
    before = {
        "identity": {"policy_digest": first["policy"].digest},
        "adaptation_digest": first["adaptive"].identity.digest,
    }
    after = {
        "identity": {"policy_digest": second["policy"].digest},
        "adaptation_digest": second["adaptive"].identity.digest,
    }
    differences = identity_differences(before, after)
    assert [difference.field for difference in differences] == ["adaptation"]
    assert not comparable_changed(differences)
    assert first["adaptive"].policy_digest != second["adaptive"].policy_digest


async def test_changing_the_prompt_or_the_seal_changes_the_grouper_identity(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    (tmp_path / "group.md").write_text("Group one way.\n")
    path = workspace(tmp_path, grouper={**CHAT_GROUPER, "system_prompt": "group.md"})
    environ = {"GROUPER_API_KEY": KEY}
    first = await captured(tmp_path, monkeypatch, path, environ=environ, run="a")
    (tmp_path / "group.md").write_text("Group another way.\n")
    second = await captured(tmp_path, monkeypatch, path, environ=environ, run="b")
    assert first["adaptive"].identity.digest != second["adaptive"].identity.digest


# -- C§9.3: role_visibility changes no artifact or dependency digest -----------------------


async def test_role_visibility_moves_only_its_own_digest(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    path_a = workspace(tmp_path)
    first = await captured(tmp_path, monkeypatch, path_a, run="a")
    visibility = {"version": "role_visibility/1", "readable_paths": ["docs/**"]}
    path_b = workspace(tmp_path, role_visibility=visibility)
    second = await captured(tmp_path, monkeypatch, path_b, run="b")
    assert first["connection"].metric_contract == second["connection"].metric_contract
    assert first["connection"].role_visibility != second["connection"].role_visibility
    assert first["artifact_policy"] == second["artifact_policy"]
    one = scan_tree(first["seed_root"], first["artifact_policy"])
    two = scan_tree(second["seed_root"], second["artifact_policy"])
    assert (one.candidate_digest, one.dependency_digest) == (
        two.candidate_digest,
        two.dependency_digest,
    )
    assert first["adaptive"].identity.digest == second["adaptive"].identity.digest
    assert first["policy"].digest == second["policy"].digest


# -- The runtime's family refusals are adaptive_runtime_required ------------------------------


@pytest.mark.parametrize(
    "raised",
    [
        "adaptive_runtime_policy_v11_family_mismatch",
        "connection_contract_required",
        "connection_contract_unexpected",
    ],
)
async def test_the_new_runtime_refusals_are_adaptive_runtime_required(
    tmp_path, monkeypatch, raised
):
    family14_admitted(monkeypatch)

    async def entry(policy, **kwargs):
        raise ValueError(raised)

    monkeypatch.setattr(evolve_module, "run_campaign", entry)
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(
            workspace(tmp_path), tmp_path / "run", adapter_factory=lambda *a, **k: object()
        )
    assert (caught.value.code, caught.value.detail) == ("adaptive_runtime_required", raised)


async def test_a_family_14_creation_leaves_linked_from_presence_to_the_runtime(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from tests.campaign_fakes import Rig

    rig = Rig(tmp_path / "native")
    await rig.run(crash_after="campaign_created")
    manifest = replace(load_manifest_v2(workspace(tmp_path)), linked_from="missing-predecessor")
    seen = {}

    async def entry(policy, **kwargs):
        seen.update(kwargs)
        raise Reached

    monkeypatch.setattr(evolve_module, "resume_campaign", entry)
    with pytest.raises(Reached):
        await evolve_module.evolve(manifest, rig.run_root, adapter_factory=lambda *a, **k: object())
    assert seen["linked_from"] is None


async def test_agent_process_bindings_follow_snapshot_without_changing_identity(
    tmp_path, monkeypatch
):
    from dataclasses import fields, replace

    from tests.agent_grouping_fakes import sealed_grouping_cli

    family14_admitted(monkeypatch)
    path = workspace(tmp_path, grouper=sealed_grouping_cli(tmp_path, allowed_env=["AGENT_TOKEN"]))
    first = await captured(tmp_path, monkeypatch, path, environ={"AGENT_TOKEN": KEY}, run="first")
    rotated = "sk-rotated-grouper-0123456789"
    second = await captured(
        tmp_path, monkeypatch, path, environ={"AGENT_TOKEN": rotated}, run="second"
    )
    before, after = first["adaptive"], second["adaptive"]
    assert before.identity == after.identity
    assert before.agent_child_environment["AGENT_TOKEN"] == KEY
    assert after.agent_child_environment["AGENT_TOKEN"] == rotated
    assert KEY in first["secrets"] and rotated in second["secrets"]
    assert KEY not in repr(before) and rotated not in repr(after)
    assert KEY not in json.dumps(before.identity.to_payload())
    assert before == replace(before, agent_child_environment=after.agent_child_environment)
    for item in fields(before):
        if item.name in {"agent_program", "agent_prompt", "agent_child_environment"}:
            assert not item.repr and not item.compare
    slot = before.identity.to_payload()["grouping"]
    assert before.agent_program.sha256 == slot["program_sha256"]
    assert before.agent_prompt.sha256 == slot["prompt_sha256"]
    with pytest.raises(TypeError):
        before.agent_child_environment["AGENT_TOKEN"] = rotated


# -- ADR-0038: the declared verbatim answers, checked at bind ----------------------------

CANNED = "This request is not implemented yet."
SECRET = "sekret-b3-forwarded-value"


def _declared_workspace(tmp_path, strings, **kwargs):
    path = write_workspace(
        tmp_path,
        rows=[
            {
                "case_id": "c1",
                "group_id": "g1",
                "stratum": "s1",
                "public": {"text": "hello"},
                "oracle": {"critical": True, "answer": CANNED, "short": "N/A"},
            },
        ],
        **kwargs,
    )
    document = v15_from(json.loads(path.read_text()))
    document["verbatim_answers"] = {"version": "verbatim_answers/1", "strings": list(strings)}
    path.write_text(json.dumps(document))
    return path


async def test_a_declared_answer_that_matches_an_oracle_value_binds(tmp_path, monkeypatch):
    path = _declared_workspace(tmp_path, [CANNED])
    seen = await captured(tmp_path, monkeypatch, path)
    assert seen["connection"].verbatim_answers.strings == (CANNED,)


@pytest.mark.parametrize(
    "strings,index",
    [
        (["not in the corpus at all"], 0),
        ([CANNED, "N/A"], 1),
        ([CANNED + " "], 0),
        ([CANNED[:-1]], 0),
    ],
)
async def test_an_unmatched_answer_is_refused_before_the_run_directory(tmp_path, strings, index):
    path = _declared_workspace(tmp_path, strings)
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(path, tmp_path / "run", adapter_factory=lambda *a, **k: object())
    assert (caught.value.code, caught.value.path) == (
        "verbatim_answer_unmatched",
        f"verbatim_answers.strings[{index}]",
    )
    assert not (tmp_path / "run").exists()


async def test_an_answer_holding_a_run_secret_is_refused_first(tmp_path, monkeypatch):
    monkeypatch.setenv("B3_TOKEN", SECRET)
    path = _declared_workspace(tmp_path, [CANNED, f"call {SECRET} now"], editor_env=("B3_TOKEN",))
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(path, tmp_path / "run", adapter_factory=lambda *a, **k: object())
    assert (caught.value.code, caught.value.path) == (
        "record_matches_secret",
        "verbatim_answers.strings[1]",
    )
    assert SECRET not in str(caught.value)
    assert not (tmp_path / "run").exists()


def _fingerprint(root):
    """Every entry under `root` with its kind and, for a file, its content digest."""

    return sorted(
        (
            path.relative_to(root).as_posix(),
            "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "strings,code",
    [
        (["nothing like the oracle"], "verbatim_answer_unmatched"),
        ([f"call {SECRET} now"], "record_matches_secret"),
    ],
    ids=["unmatched", "secret"],
)
def test_a_resume_with_a_refused_answer_exits_2_and_leaves_the_run_untouched(
    tmp_path, monkeypatch, strings, code
):
    """ADR-0038: the bind check runs on a resume too, before anything is written.

    Synchronous, because the CLI runs its own event loop in the main thread."""

    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run

    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            asyncio.run(family14_run(tmp_path, patch))
    run = tmp_path / "run"
    assert (run / "campaign.jsonl").is_file()
    before = _fingerprint(run)
    manifest = tmp_path / "manifest.json"
    document = json.loads(manifest.read_text())
    document["verbatim_answers"] = {"version": "verbatim_answers/1", "strings": strings}
    document["editor"]["allowed_env"] = ["B3_TOKEN"]
    manifest.write_text(json.dumps(document))
    monkeypatch.setenv("B3_TOKEN", SECRET)
    family14_admitted(monkeypatch)

    result = CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run)]
    )

    assert result.exit_code == 2, result.output
    assert f"refused: {code} at verbatim_answers.strings[0]" in result.output
    assert SECRET not in result.output and strings[0] not in result.output
    assert _fingerprint(run) == before


async def test_evolve_hands_its_notes_sink_to_the_campaign(tmp_path, monkeypatch):
    """ADR-0040 D8: the whole-run inode note travels through `evolve`'s `notes`."""

    family14_admitted(monkeypatch)
    path = workspace(tmp_path)
    seen: dict = {}

    async def entry(policy, **kwargs):
        seen.update(kwargs)
        raise Reached

    monkeypatch.setattr(evolve_module, "run_campaign", entry)
    said: list[str] = []
    with pytest.raises(Reached):
        await evolve_module.evolve(
            path, tmp_path / "run", adapter_factory=lambda *a, **k: object(), notes=said.append
        )
    assert seen["notes"] == said.append


# -- #282: `reflector.group_text_chars` in the adaptation identity -------------------------

#: The adaptation digest a `/15` workspace without the key had before the key
#: existed (captured at `6c6de9007`): an absent `/15` key changes no byte.
V15_ABSENT_ADAPTATION_DIGEST = "64086b6a1712a2d72ee49743e03847878a55042ee3dc050600fa039731333226"


def _limit_workspace(tmp_path, schema, limit=...):
    from tests.family15_fakes import v16_document

    path = workspace(tmp_path)
    document = json.loads(path.read_text())
    if schema == SCHEMA_V16:
        document.update(schema=schema, campaign=v16_document()["campaign"])
    if limit is not ...:
        document["reflector"]["group_text_chars"] = limit
    path.write_text(json.dumps(document))
    return path


async def _reflection(tmp_path, monkeypatch, schema, limit=...):
    family14_admitted(monkeypatch)
    seen = await captured(tmp_path, monkeypatch, _limit_workspace(tmp_path, schema, limit))
    return seen["adaptive"].identity


async def test_an_absent_v15_limit_keeps_the_adaptation_bytes(tmp_path, monkeypatch):
    identity = await _reflection(tmp_path, monkeypatch, SCHEMA_V15)
    assert "group_text_chars" not in identity.to_payload()["reflection"]
    assert identity.digest == V15_ABSENT_ADAPTATION_DIGEST


@pytest.mark.parametrize(
    ("schema", "limit", "emitted"),
    [
        (SCHEMA_V15, 4096, 4096),
        (SCHEMA_V15, 512, 512),
        (SCHEMA_V15, None, None),
        (SCHEMA_V16, ..., 4096),
        (SCHEMA_V16, None, None),
        (SCHEMA_V16, 512, 512),
    ],
)
async def test_the_effective_limit_is_bound_when_declared_or_v16(
    tmp_path, monkeypatch, schema, limit, emitted
):
    identity = await _reflection(tmp_path, monkeypatch, schema, limit)
    assert identity.to_payload()["reflection"]["group_text_chars"] == emitted


async def test_a_declared_v15_limit_is_an_adaptation_difference(tmp_path, monkeypatch):
    family14_admitted(monkeypatch)
    first = await captured(tmp_path, monkeypatch, _limit_workspace(tmp_path, SCHEMA_V15), run="a")
    second = await captured(
        tmp_path, monkeypatch, _limit_workspace(tmp_path, SCHEMA_V15, 512), run="b"
    )
    differences = identity_differences(
        {
            "identity": {"policy_digest": first["policy"].digest},
            "adaptation_digest": first["adaptive"].identity.digest,
        },
        {
            "identity": {"policy_digest": second["policy"].digest},
            "adaptation_digest": second["adaptive"].identity.digest,
        },
    )
    assert [difference.field for difference in differences] == ["adaptation"]
    assert not comparable_changed(differences)


# /16 secret subtree declarations (ADR-0049), using real write-free bind.
PATH_ORACLE = {
    "critical": True,
    "reference": "GOLD-FACT-4242",
    "reference_spec": {"field": "unit_name"},
}


async def bind_v16(
    tmp_path,
    monkeypatch,
    *,
    oracle=PATH_ORACLE,
    pointers=None,
    test_oracle=None,
    verbatim=None,
    expect_refusal=True,
    editor_env=(),
):
    from tests.family15_fakes import v16_document

    rows = [
        {
            "case_id": "c1",
            "group_id": "g1",
            "stratum": "s1",
            "public": {"text": "hello"},
            "oracle": oracle,
        }
    ]
    if test_oracle is not None:
        rows[0]["split"] = "train"
        rows.append(
            {**rows[0], "case_id": "c2", "group_id": "g2", "split": "test", "oracle": test_oracle}
        )
    path = write_workspace(tmp_path, rows=rows, editor_env=editor_env)
    document = v15_from(json.loads(path.read_text()))
    document["schema"] = SCHEMA_V16
    document["campaign"]["gate"] = v16_document()["campaign"]["gate"]
    document["secret_oracle_paths"] = {
        "version": "secret_oracle_paths/1",
        "pointers": pointers or [],
    }
    if verbatim is not None:
        document["verbatim_answers"] = {"version": "verbatim_answers/1", "strings": verbatim}
    path.write_text(json.dumps(document))
    if not expect_refusal:
        return await captured(tmp_path, monkeypatch, path)
    with pytest.raises(EvolveRefused) as caught:
        await evolve_module.evolve(path, tmp_path / "run", adapter_factory=lambda *a, **k: object())
    assert not (tmp_path / "run" / "campaign.jsonl").exists()
    return caught.value


@pytest.mark.parametrize(
    "pointers,index", [(["/reference", "/refrence"], 1), (["/reference_spec/field/x"], 0)]
)
async def test_a_typo_pointer_is_secret_oracle_path_unmatched(
    tmp_path, monkeypatch, pointers, index
):
    refused = await bind_v16(tmp_path, monkeypatch, pointers=pointers)
    assert (refused.code, refused.path) == (
        "secret_oracle_path_unmatched",
        f"secret_oracle_paths.pointers[{index}]",
    )


async def test_a_pointer_that_matches_only_a_test_case_is_unmatched(tmp_path, monkeypatch):
    refused = await bind_v16(
        tmp_path,
        monkeypatch,
        test_oracle={**PATH_ORACLE, "only_test": "TEST-ONLY-VALUE"},
        pointers=["/only_test"],
    )
    assert (refused.code, refused.path) == (
        "secret_oracle_path_unmatched",
        "secret_oracle_paths.pointers[0]",
    )


async def test_a_pointer_holding_a_run_secret_is_record_matches_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("B12_TOKEN", SECRET)
    refused = await bind_v16(
        tmp_path, monkeypatch, pointers=["/refrence", f"/{SECRET}"], editor_env=("B12_TOKEN",)
    )
    assert (refused.code, refused.path) == (
        "record_matches_secret",
        "secret_oracle_paths.pointers[1]",
    )
    assert SECRET not in str(refused)


async def test_a_short_only_selection_is_unmatched(tmp_path, monkeypatch):
    refused = await bind_v16(
        tmp_path, monkeypatch, oracle={**PATH_ORACLE, "short": "seven77"}, pointers=["/short"]
    )
    assert refused.code == "secret_oracle_path_unmatched"


async def test_an_empty_pointer_list_binds(tmp_path, monkeypatch):
    seen = await bind_v16(tmp_path, monkeypatch, pointers=[], expect_refusal=False)
    assert seen["connection"].secret_oracle_paths.pointers == ()


async def test_verbatim_unmatched_still_checks_every_oracle_string_beside_paths(
    tmp_path, monkeypatch
):
    seen = await bind_v16(
        tmp_path, monkeypatch, pointers=["/reference"], verbatim=["unit_name"], expect_refusal=False
    )
    assert seen["connection"].verbatim_answers.strings == ("unit_name",)
