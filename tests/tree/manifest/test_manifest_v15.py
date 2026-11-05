"""Manifest `/15`: parsed, not admitted."""

from __future__ import annotations

import copy
import json

import pytest
from typer.testing import CliRunner

from evolution.tree import cli as cli_module
from evolution.tree import evolve as evolve_module
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.evolve import admit_manifest
from evolution.tree.execution.config import CommandSection, ManifestError
from evolution.tree.manifest.campaign_policy_help import (
    EXPECTED_V15,
    SCHEMA_V14_NAME,
    SCHEMA_V15_NAME,
)
from evolution.tree.manifest.manifest_inventory import EnvironmentName, manifest_dependencies
from evolution.tree.manifest.manifest_v2 import (
    SCHEMA_V15,
    effective_group_text_chars,
    load_manifest_v2,
)
from evolution.tree.manifest.manifest_v15_help import (
    COMPACTION_EXPECTED,
    GROUPER_BY_KIND,
    GROUPER_COMMAND_TIMEOUT,
    GROUPER_FALLBACK,
    GROUPER_KIND,
    GROUPER_MODEL_KIND,
    GROUPER_SECTION,
    GROUPER_TIMEOUT,
    REFLECTOR_GROUP_TEXT_CHARS,
    REFLECTOR_GROUPING_V15,
    TOP_LEVEL_V15_EXPECTED,
    VERBATIM_ANSWERS_EXPECTED,
)
from evolution.tree.roles.config import (
    AgentCliGrouperSection,
    CommandGroupingSection,
    FieldsGroupingSection,
    ModelGroupingSection,
)
from evolution.tree.target.connection_contract import parse_metric_contract, parse_role_visibility
from tests.campaign_drivers import v14_document
from tests.tree.manifest.test_manifest_v2 import (
    RETRY,
    v11_chat_document,
    v11_document,
    write_v5_document,
)

V15_GATE = {
    "mode": "statistical",
    "stability": "stable",
    "focus": {"confidence": 0.9, "margin": 0},
    "causal": {"confidence": 0.9, "tolerance": "measured_noise"},
    "provenance": {"kind": "reported"},
    "fresh_parent": False,
    "global": "point",
}
V15_OBSERVE_GATE = {
    "mode": "observe",
    "stability": "stable",
    "focus": {"margin": 0},
    "causal": {"tolerance": "measured_noise"},
    "provenance": {"kind": "reported"},
    "fresh_parent": False,
    "global": "off",
}
METRIC_CONTRACT = {
    "version": "metric_contract/1",
    "kind": "pointwise",
    "target_fields": [
        {"event": "assistant_message", "pointer": "/content", "value": "nonempty_string"}
    ],
}
ROLE_VISIBILITY = {"version": "role_visibility/1", "readable_paths": []}


def v15_from(
    document,
    *,
    gate=None,
    repeats=3,
    round_stop="first_accepted",
    refresh=3,
    grouper=None,
    metric_contract=None,
    role_visibility=None,
) -> dict:
    """Any `/13`-shaped document as `/15`: the `/15` campaign keys, the gate,
    `reflector.grouping` moved to the top-level `grouper` (unless `grouper` is
    given), the connection declarations. Not validated."""

    moved = copy.deepcopy(document)
    moved["schema"] = SCHEMA_V15
    campaign = moved["campaign"]
    campaign.pop("alpha", None)
    campaign["evaluation"] = {"repeats_per_question": repeats}
    campaign["round_stop"] = round_stop
    campaign["baseline_refresh_after_rounds"] = refresh
    campaign["gate"] = copy.deepcopy(V15_GATE if gate is None else gate)
    old = moved["reflector"].pop("grouping", moved.get("grouper"))
    moved["grouper"] = copy.deepcopy(old if grouper is None else grouper)
    moved["scorer"]["metric_contract"] = copy.deepcopy(
        METRIC_CONTRACT if metric_contract is None else metric_contract
    )
    moved["role_visibility"] = copy.deepcopy(
        ROLE_VISIBILITY if role_visibility is None else role_visibility
    )
    return moved


def v15_document(**kwargs) -> dict:
    """The scaffold's sample document (subprocess roles, `fields` grouping) as `/15`."""

    return v15_from(v11_document(), **kwargs)


def load(tmp_path, document):
    return load_manifest_v2(write_v5_document(tmp_path, document))


def refused(tmp_path, document):
    with pytest.raises(ManifestError) as caught:
        load(tmp_path, document)
    return caught.value.code, caught.value.detail, caught.value.expected


# -- The schema constant and the M1 gate -----------------------------------------


def test_the_help_module_names_the_loaders_schemas():
    assert (SCHEMA_V14_NAME, SCHEMA_V15_NAME) == ("evolution-v2-manifest/14", SCHEMA_V15)


def test_a_v15_document_loads_its_campaign_and_connection(tmp_path):
    manifest = load(tmp_path, v15_document())
    assert manifest.schema == SCHEMA_V15 and manifest.workflow is not None
    workflow = manifest.workflow
    assert type(workflow.campaign) is CampaignPolicyV11
    assert workflow.campaign.round_stop == "first_accepted"
    assert workflow.metric_contract == parse_metric_contract(METRIC_CONTRACT)
    assert workflow.role_visibility == parse_role_visibility(ROLE_VISIBILITY)
    assert workflow.connection is not None
    assert workflow.connection.identity_fields() == {
        "metric_contract": parse_metric_contract(METRIC_CONTRACT).digest,
        "role_visibility": parse_role_visibility(ROLE_VISIBILITY).digest,
    }
    assert isinstance(workflow.scorer, CommandSection)
    observe = load(tmp_path, v15_document(gate=V15_OBSERVE_GATE, repeats=1))
    assert observe.workflow is not None and observe.workflow.campaign.focus_margin == 0.0


def test_evolve_admits_a_valid_v15_document(tmp_path):
    path = write_v5_document(tmp_path, v15_document())
    assert SCHEMA_V15 in evolve_module.CAMPAIGN_SCHEMAS
    assert admit_manifest(path).schema == SCHEMA_V15


# -- Structure: required top-level keys, older schemas refuse the new fields ------


@pytest.mark.parametrize("key", ["grouper", "role_visibility"])
def test_a_missing_top_level_key_is_told_its_domain(tmp_path, key):
    document = v15_document()
    del document[key]
    assert refused(tmp_path, document) == ("missing_key", key, TOP_LEVEL_V15_EXPECTED[key])


def test_a_missing_metric_contract_is_told_its_domain(tmp_path):
    document = v15_document()
    del document["scorer"]["metric_contract"]
    code, detail, expected = refused(tmp_path, document)
    assert (code, detail) == ("missing_key", "scorer.metric_contract")
    assert expected is not None and "metric_contract/1" in expected


# -- The campaign carries /15 texts through the loader -------------------------------


@pytest.mark.parametrize(
    ("edit", "detail", "expected"),
    [
        (lambda c: c.pop("round_stop"), "campaign.round_stop", EXPECTED_V15["round_stop"]),
        (
            lambda c: c["gate"]["focus"].pop("margin"),
            "campaign.gate.focus.margin",
            EXPECTED_V15["gate.focus.margin"],
        ),
        (
            lambda c: c["gate"].pop("stability"),
            "campaign.gate.stability",
            EXPECTED_V15["gate.stability"],
        ),
    ],
)
def test_a_campaign_refusal_reaches_the_manifest_with_its_v15_text(
    tmp_path, edit, detail, expected
):
    document = v15_document()
    edit(document["campaign"])
    assert refused(tmp_path, document) == ("missing_key", detail, expected)


# -- R5, R7: scorer.metric_contract and role_visibility through the loader ------------


@pytest.mark.parametrize(
    ("edit", "code", "detail"),
    [
        (lambda m: m.__setitem__("kind", "collection"), "bad_value", "scorer.metric_contract.kind"),
        (
            lambda m: m.__setitem__("zz", "zz-bad-value-37"),
            "unknown_key",
            "scorer.metric_contract.zz",
        ),
        (
            lambda m: m["target_fields"].append(dict(m["target_fields"][0])),
            "bad_value",
            "scorer.metric_contract.target_fields[1]",
        ),
        (
            lambda m: m["target_fields"][0].__setitem__("pointer", "zz-bad-value-37"),
            "bad_value",
            "scorer.metric_contract.target_fields[0].pointer",
        ),
        (
            lambda m: m["target_fields"][0].__setitem__("value", "zz-bad-value-37"),
            "bad_value",
            "scorer.metric_contract.target_fields[0].value",
        ),
        (lambda m: m.pop("version"), "missing_key", "scorer.metric_contract.version"),
    ],
)
def test_metric_contract_refusals_name_path_and_sentence(tmp_path, edit, code, detail):
    contract = copy.deepcopy(METRIC_CONTRACT)
    edit(contract)
    got = refused(tmp_path, v15_document(metric_contract=contract))
    assert got[:2] == (code, detail)
    assert got[2] is not None and "zz-bad" not in got[2] and "37" not in got[2]


def test_a_scorer_key_refusal_outside_metric_contract_keeps_its_plain_form(tmp_path):
    document = v15_document()
    document["scorer"]["zz"] = 1
    assert refused(tmp_path, document) == ("unknown_key", "scorer.zz", None)


@pytest.mark.parametrize(
    ("visibility", "code", "detail"),
    [
        ("zz-bad-value-37", "bad_type", "role_visibility"),
        (
            {"version": "role_visibility/1", "readable_paths": ["../zz37"]},
            "bad_value",
            "role_visibility.readable_paths[0]",
        ),
        (
            {"version": "role_visibility/1", "readable_paths": [], "zz": 1},
            "unknown_key",
            "role_visibility.zz",
        ),
    ],
)
def test_role_visibility_refusals_name_path_and_sentence(tmp_path, visibility, code, detail):
    got = refused(tmp_path, v15_document(role_visibility=visibility))
    assert got[:2] == (code, detail)
    assert got[2] is not None and "zz" not in got[2] and "37" not in got[2]


def test_role_visibility_keeps_the_written_patterns(tmp_path):
    visibility = {"version": "role_visibility/1", "readable_paths": ["docs/**/*.md", "uv.lock"]}
    manifest = load(tmp_path, v15_document(role_visibility=visibility))
    assert manifest.workflow is not None
    assert manifest.workflow.role_visibility is not None
    assert manifest.workflow.role_visibility.readable_paths == ("docs/**/*.md", "uv.lock")


# -- Stage order: campaign, scorer, editor, reflector, grouper, role_visibility -----


def test_the_scorer_contract_is_checked_before_the_editor(tmp_path):
    document = v15_document()
    document["scorer"]["metric_contract"]["kind"] = "collection"
    document["editor"]["kind"] = "zz"
    assert refused(tmp_path, document)[:2] == ("bad_value", "scorer.metric_contract.kind")


def test_the_campaign_is_checked_before_role_visibility(tmp_path):
    document = v15_document(role_visibility="x")
    del document["campaign"]["round_stop"]
    assert refused(tmp_path, document)[:2] == ("missing_key", "campaign.round_stop")


# -- Conformance reads /15 ------------------------------------------------------------


def test_conformance_reports_a_v15_refusal_with_its_sentence(tmp_path):
    document = v15_document()
    del document["grouper"]
    path = write_v5_document(tmp_path, document)
    result = CliRunner().invoke(cli_module.tree_app, ["conformance", "--manifest", str(path)])
    assert result.exit_code == 2
    assert (
        f"conformance: manifest missing_key: grouper: {TOP_LEVEL_V15_EXPECTED['grouper']}\n"
        in result.output
    )


def test_a_non_finite_v15_number_refuses_the_whole_document(tmp_path):
    path = write_v5_document(tmp_path, v15_document())
    text = path.read_text()
    assert '"margin": 0' in text
    path.write_text(text.replace('"margin": 0', '"margin": NaN', 1))
    with pytest.raises(ManifestError) as caught:
        load_manifest_v2(path)
    assert (caught.value.code, caught.value.detail) == ("bad_type", "$")


def test_the_document_is_json(tmp_path):
    # `write_v5_document` writes the dict verbatim; a builder bug that put a
    # non-JSON value in the document would fail here, not in a loader test.
    json.dumps(v15_document())


def test_v15_reflector_omits_grouping(tmp_path):
    manifest = load(tmp_path, v15_document())
    assert manifest.workflow is not None
    assert manifest.workflow.reflector.grouping is None


@pytest.mark.parametrize("value", [None, "zz-bad-value-37", {"kind": "fields", "features": []}])
def test_reflector_grouping_is_refused_and_names_grouper(tmp_path, value):
    document = v15_document()
    document["reflector"]["grouping"] = value
    assert refused(tmp_path, document) == (
        "unknown_key",
        "reflector.grouping",
        REFLECTOR_GROUPING_V15,
    )
    assert "grouper" in REFLECTOR_GROUPING_V15
    assert "zz-bad-value-37" not in REFLECTOR_GROUPING_V15


def test_reflector_grouping_is_refused_before_a_chat_reflectors_own_parse(tmp_path):
    document = v15_document()
    # Keep the shared keys and omit the required chat keys. Grouping must be
    # refused before the role parser can collapse its missing-key refusal.
    document["reflector"]["kind"] = "chat"
    document["reflector"]["grouping"] = "zz-bad-value-37"
    assert refused(tmp_path, document) == (
        "unknown_key",
        "reflector.grouping",
        REFLECTOR_GROUPING_V15,
    )


# -- R8: the grouper section ----------------------------------------------------------

FIELDS_GROUPER = {
    "kind": "fields",
    "features": [
        {
            "name": "failed_criteria",
            "source": "observation",
            "path": "/failed_criteria",
            "description": "Failed criteria.",
        }
    ],
}
#: a complete `chat` Grouper. The accepted example is not a valid manifest
#: (its `retry` lacks the version and it omits four required keys), so the
#: tests and the documentation use this one.
CHAT_GROUPER = {
    "kind": "chat",
    "backend": "openai_chat",
    "model": "your-grouping-model",
    "base_url": "https://provider.example/v1",
    "api_key_env": "GROUPER_API_KEY",
    "timeout_seconds": 600,
    "concurrency": 4,
    "max_input_chars": 200000,
    "max_output_tokens": 8192,
    "retry": {
        "version": "role_call_retry/1",
        "max_attempts": 4,
        "total_budget_seconds": 300,
        "backoff": {"initial_seconds": 2, "multiplier": 2, "max_seconds": 30, "jitter": "full"},
    },
    "fallback": copy.deepcopy(FIELDS_GROUPER),
}
COMMAND_GROUPER = {
    "kind": "command",
    "argv": ["python3", "group.py"],
    "cwd": "grouping",
    "dependency_paths": ["grouping/lib.py"],
    "allowed_env": ["GROUPER_TOKEN"],
    "timeout_seconds": 30,
    "config": {"threshold": 2},
}
AGENT_GROUPER = {"kind": "agent_cli", "model": "codex", "timeout_seconds": 600}
#: The accepted example, verbatim.
ADDENDUM_CHAT_EXAMPLE = {
    "kind": "chat",
    "model": "your-grouping-model",
    "base_url": "https://provider.example/v1",
    "api_key_env": "GROUPER_API_KEY",
    "timeout_seconds": 600,
    "retry": {
        "max_attempts": 4,
        "total_budget_seconds": 300,
        "backoff": {"initial_seconds": 2, "multiplier": 2, "max_seconds": 30, "jitter": "full"},
    },
    "fallback": {"kind": "fields", "features": ["..."]},
}


def grouper(tmp_path, section):
    manifest = load(tmp_path, v15_document(grouper=section))
    assert manifest.workflow is not None
    assert manifest.workflow.reflector.grouping is None
    return manifest.workflow.grouper


def test_each_grouper_kind_parses(tmp_path):
    fields = grouper(tmp_path, FIELDS_GROUPER)
    assert (
        isinstance(fields, FieldsGroupingSection) and fields.features[0].name == "failed_criteria"
    )
    command = grouper(tmp_path, COMMAND_GROUPER)
    assert isinstance(command, CommandGroupingSection)
    assert command.command.cwd == (tmp_path / "grouping").resolve()
    assert command.command.allowed_env == frozenset({"GROUPER_TOKEN"})
    assert dict(command.config) == {"threshold": 2}
    chat = grouper(tmp_path, CHAT_GROUPER)
    assert isinstance(chat, ModelGroupingSection)
    assert (chat.model, chat.timeout_seconds, chat.concurrency) == ("your-grouping-model", 600, 4)
    assert chat.fallback is not None and chat.fallback.features[0].name == "failed_criteria"
    agent = grouper(tmp_path, AGENT_GROUPER)
    assert isinstance(agent, AgentCliGrouperSection)
    assert (agent.role.model, agent.role.permission, agent.timeout_seconds, agent.fallback) == (
        "codex",
        "safe",
        600,
        None,
    )
    with_fallback = grouper(tmp_path, {**AGENT_GROUPER, "fallback": FIELDS_GROUPER})
    assert isinstance(with_fallback, AgentCliGrouperSection)
    assert with_fallback.fallback is not None


def test_a_chat_grouper_may_fall_back_to_singletons(tmp_path):
    chat = grouper(tmp_path, {**CHAT_GROUPER, "fallback": None})
    assert isinstance(chat, ModelGroupingSection) and chat.fallback is None


def _without(section, key):
    return {name: value for name, value in section.items() if name != key}


GROUPER_MATRIX = [
    ("not_object", "zz-bad-value-37", ("bad_type", "grouper", GROUPER_SECTION)),
    ("no_kind", _without(FIELDS_GROUPER, "kind"), ("missing_key", "grouper", GROUPER_SECTION)),
    ("kind_type", {**FIELDS_GROUPER, "kind": 37}, ("bad_type", "grouper.kind", GROUPER_KIND)),
    ("kind_value", {**FIELDS_GROUPER, "kind": "zz37"}, ("bad_value", "grouper.kind", GROUPER_KIND)),
    (
        "kind_model",
        {**CHAT_GROUPER, "kind": "model"},
        ("bad_value", "grouper.kind", GROUPER_MODEL_KIND),
    ),
    (
        "other_kinds_key",
        {**FIELDS_GROUPER, "model": "zz37"},
        ("unknown_key", "grouper.model", GROUPER_BY_KIND["fields"]),
    ),
    (
        "addendum_example",
        ADDENDUM_CHAT_EXAMPLE,
        ("missing_key", "grouper.backend", GROUPER_BY_KIND["chat"]),
    ),
    (
        "chat_range",
        {**CHAT_GROUPER, "concurrency": 37},
        ("bad_value", "grouper.concurrency", GROUPER_BY_KIND["chat"]),
    ),
    (
        "chat_timeout",
        {**CHAT_GROUPER, "timeout_seconds": 7201},
        ("bad_value", "grouper.timeout_seconds", GROUPER_TIMEOUT),
    ),
    (
        "chat_fallback_kind",
        {**CHAT_GROUPER, "fallback": {"kind": "model"}},
        ("bad_value", "grouper.fallback.kind", GROUPER_FALLBACK),
    ),
    (
        "command_config",
        _without(COMMAND_GROUPER, "config"),
        ("missing_key", "grouper.config", GROUPER_BY_KIND["command"]),
    ),
    (
        "agent_timeout_missing",
        _without(AGENT_GROUPER, "timeout_seconds"),
        ("missing_key", "grouper.timeout_seconds", GROUPER_TIMEOUT),
    ),
    (
        "agent_timeout_low",
        {**AGENT_GROUPER, "timeout_seconds": 9},
        ("bad_value", "grouper.timeout_seconds", GROUPER_TIMEOUT),
    ),
    (
        "agent_timeout_bool",
        {**AGENT_GROUPER, "timeout_seconds": True},
        ("bad_type", "grouper.timeout_seconds", GROUPER_TIMEOUT),
    ),
    (
        "agent_model",
        {**AGENT_GROUPER, "model": "gpt-5.5"},
        ("bad_value", "grouper.model", GROUPER_BY_KIND["agent_cli"]),
    ),
    (
        "hermes_max_turns",
        {**AGENT_GROUPER, "model": "hermes/some-model", "max_turns": 8},
        ("bad_value", "grouper.max_turns", GROUPER_BY_KIND["agent_cli"]),
    ),
    (
        "agent_retry",
        {**AGENT_GROUPER, "retry": CHAT_GROUPER["retry"]},
        ("unknown_key", "grouper.retry", GROUPER_BY_KIND["agent_cli"]),
    ),
    (
        "agent_write_budget",
        {**AGENT_GROUPER, "max_written_bytes": 37},
        ("unknown_key", "grouper.max_written_bytes", GROUPER_BY_KIND["agent_cli"]),
    ),
    (
        "agent_fallback",
        {**AGENT_GROUPER, "fallback": "zz37"},
        ("bad_type", "grouper.fallback", GROUPER_FALLBACK),
    ),
    (
        "command_timeout_zero",
        {**COMMAND_GROUPER, "timeout_seconds": 0},
        ("bad_value", "grouper.timeout_seconds", GROUPER_COMMAND_TIMEOUT),
    ),
    (
        "command_timeout_missing",
        _without(COMMAND_GROUPER, "timeout_seconds"),
        ("missing_key", "grouper.timeout_seconds", GROUPER_COMMAND_TIMEOUT),
    ),
    (
        "chat_fallback_unknown_key",
        {**CHAT_GROUPER, "fallback": {**FIELDS_GROUPER, "zz37": 1}},
        ("unknown_key", "grouper.fallback.zz37", GROUPER_FALLBACK),
    ),
]


@pytest.mark.parametrize(
    ("name", "section", "expected"), GROUPER_MATRIX, ids=[row[0] for row in GROUPER_MATRIX]
)
def test_grouper_refusals(tmp_path, name, section, expected):
    got = refused(tmp_path, v15_document(grouper=section))
    assert got == expected
    assert "zz" not in got[2] and "37" not in got[2]


def test_a_command_grouper_admits_a_fractional_timeout(tmp_path):
    manifest = load(tmp_path, v15_document(grouper={**COMMAND_GROUPER, "timeout_seconds": 2.5}))
    assert manifest.workflow.grouper.command.timeout_seconds == 2.5


def test_a_path_refusal_inside_the_grouper_keeps_its_plain_form(tmp_path):
    document = v15_document(grouper={**COMMAND_GROUPER, "cwd": "seed"})
    assert refused(tmp_path, document) == ("program_inside_seed", "grouper.cwd", None)


def test_a_v14_document_relabelled_v15_is_refused_at_grouper_first(tmp_path):
    document = v14_document()
    document["schema"] = SCHEMA_V15
    assert refused(tmp_path, document) == ("missing_key", "grouper", GROUPER_SECTION)


def test_a_v15_reflector_without_selection_is_still_the_collapsed_refusal(tmp_path):
    document = v15_document()
    del document["reflector"]["selection"]
    assert refused(tmp_path, document) == ("missing_key", "reflector", None)


# -- `reflector.group_text_chars` ---------------------------------------------------


@pytest.mark.parametrize("value", [1, 4096, None])
def test_a_v15_reflector_declares_its_group_text_limit(tmp_path, value):
    document = v15_document()
    document["reflector"]["group_text_chars"] = value
    reflector = load(tmp_path, document).workflow.reflector
    assert (reflector.group_text_chars, reflector.group_text_chars_declared) == (value, True)
    assert effective_group_text_chars(reflector, SCHEMA_V15) == value


def test_an_absent_v15_group_text_limit_is_undeclared_and_512(tmp_path):
    reflector = load(tmp_path, v15_document()).workflow.reflector
    assert (reflector.group_text_chars, reflector.group_text_chars_declared) == (None, False)
    assert effective_group_text_chars(reflector, SCHEMA_V15) == 512


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (0, "bad_value"),
        (-1, "bad_value"),
        (1.5, "bad_type"),
        ("512", "bad_type"),
        (True, "bad_type"),
    ],
)
def test_a_bad_v15_group_text_limit_is_told_its_domain(tmp_path, value, code):
    document = v15_document()
    document["reflector"]["group_text_chars"] = value
    assert refused(tmp_path, document) == (
        code,
        "reflector.group_text_chars",
        REFLECTOR_GROUP_TEXT_CHARS,
    )
    assert "null" in REFLECTOR_GROUP_TEXT_CHARS


# -- Stage order around the grouper (R8: pinned) ------------------------------------------


def test_the_reflector_is_checked_before_the_grouper(tmp_path):
    document = v15_document(grouper={"kind": "zz"})
    document["reflector"]["kind"] = "zz"
    assert refused(tmp_path, document)[:2] == ("bad_value", "reflector.kind")


def test_the_grouper_is_checked_before_role_visibility(tmp_path):
    document = v15_document(grouper={"kind": "zz"}, role_visibility="x")
    assert refused(tmp_path, document)[:2] == ("bad_value", "grouper.kind")


# -- compaction keys -------------------------------------------------


def chat_document(**compaction):
    """`/15` with a chat Editor and a chat Reflector; `compaction` sets both roles' keys."""

    document = v15_from(v11_chat_document(editor_retry=RETRY, reflector_retry=RETRY))
    for role in ("editor", "reflector"):
        document[role].update(compaction)
    return document


BOTH = {"context_compact_chars": 800000, "context_message_max_chars": 64000}

#: plain `http` to a host that is not loopback -- by name, IPv4 and IPv6.
INTERNAL_HTTP_URLS = (
    "http://llm.internal:8000/v1",
    "http://10.0.0.5:8080/v1",
    "http://[fd00::1]:8000/v1",
)


def chat_roles_document(base: dict | None = None) -> dict:
    """`base` (default `/15`) with a chat Editor, a chat Reflector and a chat Grouper."""

    document = copy.deepcopy(chat_document(**BOTH) if base is None else base)
    roles = chat_document(**BOTH)
    document.update(editor=roles["editor"], reflector=roles["reflector"])
    document["grouper"] = copy.deepcopy(CHAT_GROUPER)
    return document


def role_section(manifest, role):
    workflow = manifest.workflow
    assert workflow is not None
    return {
        "editor": workflow.editor,
        "reflector": workflow.reflector.role,
        "grouper": workflow.grouper,
    }[role]


@pytest.mark.parametrize("base_url", INTERNAL_HTTP_URLS)
@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_a_v15_chat_role_accepts_plain_http_to_any_host(tmp_path, role, base_url):
    """An internal model endpoint over plain `http` loads, verbatim."""

    document = chat_roles_document()
    document[role]["base_url"] = base_url
    assert role_section(load(tmp_path, document), role).base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://u:p@llm.internal/v1",
        "http://llm.internal/v1?x=1",
        "http://llm.internal/v1#part",
        "http://",
        "http://llm.internal:99999/v1",
        "http://llm internal/v1",
        "ftp://llm.internal/v1",
    ],
)
@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_a_v15_chat_role_still_refuses_a_malformed_http_url(tmp_path, role, base_url):
    document = chat_roles_document()
    document[role]["base_url"] = base_url
    assert refused(tmp_path, document)[:2] == ("bad_value", f"{role}.base_url")


@pytest.mark.parametrize("role", ["editor", "reflector"])
@pytest.mark.parametrize("key", ["context_compact_chars", "context_message_max_chars"])
def test_a_v15_chat_role_without_a_compaction_key_is_refused(tmp_path, role, key):
    document = chat_document(**BOTH)
    del document[role][key]
    assert refused(tmp_path, document) == ("missing_key", f"{role}.{key}", COMPACTION_EXPECTED[key])


def test_the_compaction_sentences_name_the_recommended_values():
    assert "800000" in COMPACTION_EXPECTED["context_compact_chars"]
    assert "400000 tokens" in COMPACTION_EXPECTED["context_compact_chars"]
    assert "64000" in COMPACTION_EXPECTED["context_message_max_chars"]
    assert "half" in COMPACTION_EXPECTED["context_message_max_chars"]


def test_a_v15_chat_role_with_both_keys_parses_them(tmp_path):
    manifest = load(tmp_path, chat_document(**BOTH))
    assert manifest.workflow is not None
    for section in (manifest.workflow.editor, manifest.workflow.reflector.role):
        assert (section.context_compact_chars, section.context_message_max_chars) == (
            800000,
            64000,
        )


def test_the_half_rule_still_applies_in_v15(tmp_path):
    document = chat_document(context_compact_chars=100000, context_message_max_chars=60000)
    assert refused(tmp_path, document) == ("bad_value", "editor.context_message_max_chars", None)


def test_agent_and_subprocess_roles_need_no_compaction_keys(tmp_path):
    from tests.tree.manifest.test_manifest_v2 import AGENT

    document = v15_document()
    document["editor"] = dict(AGENT)
    manifest = load(tmp_path, document)
    assert manifest.workflow is not None


# -- R11: declared spellings and the inventory agree --------------------------------


def _workspace_files(tmp_path):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "group.md").write_text("Group failures.\n")
    (tmp_path / "bin").mkdir(exist_ok=True)
    agent = tmp_path / "bin" / "codex"
    agent.write_text("#!/bin/sh\nexit 0\n")
    agent.chmod(0o755)


@pytest.mark.parametrize(
    ("section", "spelled"),
    [
        (
            COMMAND_GROUPER,
            {"grouper.cwd": "grouping", "grouper.dependency_paths[0]": "grouping/lib.py"},
        ),
        (
            {**CHAT_GROUPER, "system_prompt": "prompts/group.md"},
            {"grouper.system_prompt": "prompts/group.md"},
        ),
        (CHAT_GROUPER, {"grouper.system_prompt": None}),
        (
            {**AGENT_GROUPER, "system_prompt": "prompts/group.md", "agent_bin": "bin/codex"},
            {"grouper.system_prompt": "prompts/group.md", "grouper.agent_bin": "bin/codex"},
        ),
        (AGENT_GROUPER, {"grouper.system_prompt": None, "grouper.agent_bin": None}),
        (FIELDS_GROUPER, {}),
    ],
)
def test_the_grouper_spellings_are_declared_and_inventoried(tmp_path, section, spelled):
    _workspace_files(tmp_path)
    manifest = load(tmp_path, v15_document(grouper=section))
    declared = {key: value for key, value in manifest.declared.items() if key.startswith("grouper")}
    assert declared == spelled
    inventory = manifest_dependencies(manifest)
    assert "declared_spellings_unavailable" not in inventory.boundary
    fields = {
        dependency.field for dependency in inventory.dependencies if dependency.role == "grouper"
    }
    assert set(spelled) <= fields


def test_the_inventory_names_the_grouper_environment(tmp_path):
    chat = manifest_dependencies(
        load(tmp_path, v15_document(grouper={**CHAT_GROUPER, "proxy_env": "GROUPER_PROXY"}))
    )
    assert (
        EnvironmentName("grouper", "grouper.api_key_env", "GROUPER_API_KEY", "credential")
        in chat.environment
    )
    assert (
        EnvironmentName("grouper", "grouper.proxy_env", "GROUPER_PROXY", "credential")
        in chat.environment
    )
    command = manifest_dependencies(load(tmp_path, v15_document(grouper=COMMAND_GROUPER)))
    assert (
        EnvironmentName("grouper", "grouper.allowed_env", "GROUPER_TOKEN", "forwarded")
        in command.environment
    )
    agent = manifest_dependencies(
        load(tmp_path, v15_document(grouper={**AGENT_GROUPER, "allowed_env": ["AGENT_TOKEN"]}))
    )
    assert (
        EnvironmentName("grouper", "grouper.allowed_env", "AGENT_TOKEN", "forwarded")
        in agent.environment
    )


# -- verbatim_answers/1 (ADR-0038) -----------------------------------------------

CANNED = "This request is not implemented yet."
DECLARED = {"version": "verbatim_answers/1", "strings": [CANNED, "line one\nline two"]}


def test_v15_admits_declared_verbatim_answers(tmp_path):
    document = {**v15_document(), "verbatim_answers": DECLARED}
    workflow = load(tmp_path, document).workflow
    assert workflow.verbatim_answers.strings == (CANNED, "line one\nline two")
    assert workflow.connection.verbatim_answers == workflow.verbatim_answers


def test_without_the_key_nothing_is_declared(tmp_path):
    workflow = load(tmp_path, v15_document()).workflow
    assert workflow.verbatim_answers is None and workflow.connection.verbatim_answers is None


@pytest.mark.parametrize(
    "value,code,detail",
    [
        ([CANNED], "bad_type", "verbatim_answers"),
        ({"strings": [CANNED]}, "missing_key", "verbatim_answers.version"),
        ({"version": "verbatim_answers/1"}, "missing_key", "verbatim_answers.strings"),
        ({**DECLARED, "path": "x.json"}, "unknown_key", "verbatim_answers.path"),
        ({**DECLARED, "version": "verbatim_answers/2"}, "bad_value", "verbatim_answers.version"),
        ({**DECLARED, "version": 1}, "bad_type", "verbatim_answers.version"),
        ({**DECLARED, "strings": CANNED}, "bad_type", "verbatim_answers.strings"),
        ({**DECLARED, "strings": []}, "bad_value", "verbatim_answers.strings"),
        (
            {**DECLARED, "strings": [f"answer {i:02d} text" for i in range(65)]},
            "bad_value",
            "verbatim_answers.strings",
        ),
        ({**DECLARED, "strings": [CANNED, 7]}, "bad_type", "verbatim_answers.strings[1]"),
        ({**DECLARED, "strings": [CANNED, ""]}, "bad_value", "verbatim_answers.strings[1]"),
        ({**DECLARED, "strings": ["a" * 4097]}, "bad_value", "verbatim_answers.strings[0]"),
        ({**DECLARED, "strings": ["\u00e9" * 2049]}, "bad_value", "verbatim_answers.strings[0]"),
        ({**DECLARED, "strings": [CANNED, CANNED]}, "bad_value", "verbatim_answers.strings[1]"),
        ({**DECLARED, "strings": ["\ud800 lone"]}, "bad_value", "verbatim_answers.strings[0]"),
    ],
)
def test_verbatim_answer_shape_refusals_name_the_path_never_the_value(
    tmp_path, value, code, detail
):
    got_code, got_detail, expected = refused(
        tmp_path, {**v15_document(), "verbatim_answers": value}
    )
    assert (got_code, got_detail) == (code, detail)
    assert expected == VERBATIM_ANSWERS_EXPECTED
    assert CANNED not in got_detail and CANNED not in (expected or "")


def test_the_byte_limits_are_inclusive(tmp_path):
    exact = {**DECLARED, "strings": ["a" * 4096, *[f"answer {i:02d} text" for i in range(63)]]}
    document = {**v15_document(), "verbatim_answers": exact}
    assert len(load(tmp_path, document).workflow.verbatim_answers.strings) == 64


# -- `api_key_env` is optional for chat roles ----------------------------------------


def _chat_roles(tmp_path, document):
    workflow = load(tmp_path, document).workflow
    assert workflow is not None
    return {
        "editor": workflow.editor,
        "reflector": workflow.reflector.role,
        "grouper": workflow.grouper,
    }


def _chat_role_document(role, key_edit):
    document = chat_document(**BOTH)
    document["grouper"] = copy.deepcopy(CHAT_GROUPER)
    key_edit(document[role])
    return document


def _drop_key(section):
    section.pop("api_key_env", None)


def _null_key(section):
    section["api_key_env"] = None


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize("edit", [_drop_key, _null_key], ids=["omitted", "null"])
def test_a_chat_role_without_api_key_env_loads_it_as_undeclared(tmp_path, role, edit):
    sections = _chat_roles(tmp_path, _chat_role_document(role, edit))
    assert sections[role].api_key_env is None


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_a_declared_api_key_env_loads_unchanged(tmp_path, role):
    document = _chat_role_document(role, lambda section: section.update(api_key_env="MY_KEY"))
    assert _chat_roles(tmp_path, document)[role].api_key_env == "MY_KEY"


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize(
    ("value", "code"),
    [("", "bad_value"), ("A=B", "bad_value"), (1, "bad_type")],
)
def test_a_malformed_api_key_env_is_still_refused(tmp_path, role, value, code):
    document = _chat_role_document(role, lambda section: section.update(api_key_env=value))
    assert refused(tmp_path, document)[:2] == (code, f"{role}.api_key_env")


def test_a_bare_chat_grouper_is_refused_at_its_next_required_key(tmp_path):
    code, detail, _ = refused(tmp_path, v15_document(grouper={"kind": "chat"}))
    assert (code, detail) == ("missing_key", "grouper.backend")


def test_the_chat_grouper_help_lists_api_key_env_as_optional():
    text = GROUPER_BY_KIND["chat"]
    required, optional = text.split("and optionally")
    assert "api_key_env" not in required
    assert "api_key_env" in optional


def test_the_inventory_lists_api_key_env_only_when_declared(tmp_path):
    def names(document):
        inventory = manifest_dependencies(load(tmp_path, document))
        return {item.field for item in inventory.environment}

    undeclared = _chat_role_document("grouper", _drop_key)
    for role in ("editor", "reflector"):
        undeclared[role].pop("api_key_env", None)
    assert not {name for name in names(undeclared) if name.endswith(".api_key_env")}
    declared = _chat_role_document("grouper", lambda section: None)
    assert {"editor.api_key_env", "reflector.api_key_env", "grouper.api_key_env"} <= names(declared)


def test_v15_refuses_secret_oracle_paths_as_an_unknown_key(tmp_path):
    code, detail, _ = refused(
        tmp_path,
        {
            **v15_document(),
            "secret_oracle_paths": {"version": "secret_oracle_paths/1", "pointers": []},
        },
    )
    assert (code, detail) == ("unknown_key", "secret_oracle_paths")


def test_v15_refuses_evidence_as_an_unknown_key(tmp_path):
    error = refused(tmp_path, {**v15_document(), "evidence": {"version": "evidence/1"}})
    assert error[:2] == ("unknown_key", "evidence")


def test_v15_refuses_checks_as_an_unknown_key(tmp_path):
    assert refused(
        tmp_path,
        {
            **v15_document(),
            "checks": {
                "version": "checks/1",
                "modes": {},
            },
        },
    )[:2] == ("unknown_key", "checks")


@pytest.mark.parametrize(
    "section,key",
    [
        ("editor", "max_protocol_repairs"),
        ("editor", "max_consecutive_tool_failures"),
        ("reflector", "max_protocol_repairs"),
        ("reflector", "max_consecutive_tool_failures"),
        ("reflector", "max_consecutive_skips"),
        ("grouper", "max_repairs"),
        ("grouper", "max_consecutive_full_fallbacks"),
    ],
)
def test_v15_refuses_each_role_loop_key(tmp_path, section, key):
    document = chat_roles_document(v15_document())
    document[section][key] = 2
    assert refused(tmp_path, document)[:2] == ("unknown_key", f"{section}.{key}")


@pytest.mark.parametrize(
    "key",
    [
        "group_output_tokens",
        "glimpse_chars",
        "max_set_members",
        "read_chars",
        "max_turns",
        "card_examples",
        "max_reduce_levels",
        "max_requests",
    ],
)
def test_v15_refuses_v16_grouping_keys(tmp_path, key):
    from evolution.tree.manifest.manifest_v15_help import grouper_expected

    document = chat_roles_document(v15_document())
    document["grouper"][key] = 4
    assert refused(tmp_path, document) == (
        "unknown_key",
        f"grouper.{key}",
        grouper_expected("unknown_key", f"grouper.{key}", "chat"),
    )


@pytest.mark.parametrize("key,value", [("min_tries", 20), ("count_recovered", False)])
def test_v15_refuses_min_tries_and_count_recovered(tmp_path, key, value):
    document = v15_document()
    document["campaign"]["indeterminate"]["target"][key] = value
    assert refused(tmp_path, document)[:2] == (
        "unknown_key",
        f"campaign.indeterminate.target.{key}",
    )


def test_v15_refuses_preflight_sample_retries(tmp_path):
    document = v15_document()
    document["target"]["preflight_sample_retries"] = 0
    assert refused(tmp_path, document)[:2] == ("unknown_key", "target.preflight_sample_retries")


def test_stages_refused_in_v15(tmp_path):
    document = v15_document()
    document["campaign"]["gate"]["stages"] = "focus_first"
    got = refused(tmp_path, document)
    assert got[:2] == ("unknown_key", "campaign.gate.stages")
