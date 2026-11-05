"""b16 §5.3, §11: a `/16` campaign groups with `model_grouping/2` and reported provenance.

Each scenario is a genuine campaign (`public_run` for `/16`, `family14_public_run`
for `/15`) whose `chat` Grouper is a scripted provider: the `model_grouping/2`
fake `PortionGrouper`, or, for a run created by `2.0.0b15` (a `/16` Grouper
identity that recorded `model_grouping/1`), the `/15` text-answer
`ScriptedClient`. The default two-case run has one failing case (`c1`), so one
grouping is the preflight and one portion request.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

import evolution
from evolution.tree import campaign as campaign_module
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import TWO_CASE_SCORER, audit_mismatches
from tests.model_grouping_fakes import (
    PortionGrouper,
    ScriptedClient,
    chat_grouper_section,
    group_everything,
    grouping_answer,
    map_group,
    use_chat_grouper,
)
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family14_visibility_change import _decision, _version
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

#: The producing version the run created "by 2.0.0b15" stamps: any release
#: other than the installed one, so the resume sees a `producing_version`
#: difference while this branch is still versioned `2.0.0b15`.
PREVIOUS = "2.0.0b15" if evolution.__version__ != "2.0.0b15" else "2.0.0b14"

pytestmark = pytest.mark.slow

#: One `model_grouping/2` grouping of the default run: the preflight and the
#: one portion's `finish_portion` answer (one portion: consolidation skipped).
GROUPING = 2
#: A reported component that is no file of the seed, so it can reach a role
#: input only through what the Grouper writes.
COMPONENT = "retriever:index-7"
#: A Grouper text naming it (PDR-0020's family-15 amendment).
NAMING = f"{COMPONENT} returns stale rows"
#: The scorer of `test_tree_campaign_family15_evidence_upgrade`: the edit never
#: passes a case, so no round promotes and round 2 measures no new baseline.
NONPROMOTING = TWO_CASE_SCORER.replace(
    'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
    'passed = bool(payload["oracle"].get("pass"))',
)
#: Keys that would carry stored provenance as a field (PDR-0020): the `used`
#: unions, the component map and its digest, the affected-case selection.
PROVENANCE_FIELDS = frozenset({"used", "components", "components_digest", "changed", "unchanged"})


def _chat(**overrides: Any):
    def configure(document):
        document["grouper"] = chat_grouper_section(**overrides)

    return configure


def _reported(case: str, turn: int) -> list[str]:
    return [COMPONENT]


def _tools(request: dict[str, Any]) -> list[str]:
    return [tool["function"]["name"] for tool in request.get("tools") or ()]


def _portion(request: dict[str, Any]) -> dict[str, Any]:
    from evolution.tree.roles.grouping.portion_session import PORTION_LEAD

    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    assert user.startswith(PORTION_LEAD)
    return json.loads(user[len(PORTION_LEAD) :])


def _grouping(run) -> dict[str, Any]:
    (created,) = payloads(run, "campaign_created")
    return created["adaptation_payload"]["grouping"]


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, (list, tuple)):
        return {key for child in value for key in _keys(child)}
    return set()


# -- the end-to-end run ------------------------------------------------------------


async def test_v16_campaign_runs_end_to_end_on_v2(tmp_path, monkeypatch):
    from evolution.prompts import TREE_GROUPING_MODEL_V2_SYSTEM

    client = use_chat_grouper(monkeypatch, PortionGrouper(budget=GROUPING))
    run, result = await public_run(tmp_path, monkeypatch, configure=_chat())
    assert result.terminal == "finished", (result.owner, result.code)
    grouping = _grouping(run)
    assert grouping["kind"] == "chat" and grouping["protocol"] == "model_grouping/2"
    assert (
        grouping["prompt_sha256"]
        == hashlib.sha256(TREE_GROUPING_MODEL_V2_SYSTEM.encode()).hexdigest()
    )
    assert len(client.requests) == GROUPING
    preflight, portion = client.requests
    assert _tools(preflight) == ["list_case"]
    assert "finish_portion" in _tools(portion)
    assert len(_portion(portion)["cases"]) == 1
    (finished,) = payloads(run, "grouping_finished")
    assert finished["fallback"] is None
    assert finished["calls"] == {"map_requests": 1, "reduce": "none", "attempts": 2, "repairs": 0}
    assert finished["result"]["version"] == "reflect_grouping_result/1"
    assert payloads(run, "grouping_portion_finished")
    assert payloads(run, "reflection_finished") and payloads(run, "editor_started")
    assert audit_mismatches(run) == []


# -- upgrade from 2.0.0b15 and the rebase --------------------------------------------


async def _b15_run_resumed(tmp_path, monkeypatch):
    """A two-round `/16` chat-Grouper run created by `2.0.0b15` and cut after
    round 1 closes, then resumed by this release: the refusal and the cut journal.

    `2.0.0b15` wrote a `/16` chat Grouper's identity as `/15`'s
    (`model_grouping/1`, the packaged `tree_grouping_model.md`, no portion
    key) and grouped with `/15`'s map/reduce.
    """

    from evolution.tree import evolve as evolve_module
    from evolution.tree.evolve import EvolveRefused

    real = evolve_module._chat_grouper_identity
    old = use_chat_grouper(monkeypatch, ScriptedClient([group_everything()]))
    with monkeypatch.context() as patch:
        patch.setattr(
            evolve_module,
            "_chat_grouper_identity",
            lambda section, *, family15=False: real(section, family15=False),
        )
        _version(patch, PREVIOUS)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                patch,
                configure=_chat(),
                campaign={"rounds": 2},
                scorer=NONPROMOTING,
                cut="after_round_closed",
            )
    run = tmp_path / "run"
    assert old.script == [] and len(old.requests) == 1
    assert _grouping(run)["protocol"] == "model_grouping/1"
    cut = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as caught:
        await public_run(
            tmp_path, monkeypatch, configure=_chat(), campaign={"rounds": 2}, scorer=NONPROMOTING
        )
    assert (run / "campaign.jsonl").read_bytes() == cut
    return caught.value


async def test_unfinished_v16_run_with_v1_payload_refused_with_adaptation_and_producing_version(
    tmp_path, monkeypatch
):
    refused = await _b15_run_resumed(tmp_path, monkeypatch)
    assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
    assert sorted(d.field for d in refused.differences) == ["adaptation", "producing_version"]


async def test_rebase_then_regroups_current_baseline(tmp_path, monkeypatch):
    refused = await _b15_run_resumed(tmp_path, monkeypatch)
    run = tmp_path / "run"
    (before,) = payloads(run, "grouping_started")
    client = use_chat_grouper(monkeypatch, PortionGrouper(budget=GROUPING))
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=_chat(),
        campaign={"rounds": 2},
        scorer=NONPROMOTING,
        authorizations=[_decision(refused, None, "accept model_grouping/2 and b16")],
    )
    assert result.terminal == "finished", (result.owner, result.code)
    (rebased,) = payloads(run, "identity_rebased")
    assert sorted(rebased["changed_fields"]) == ["adaptation", "producing_version"]
    assert rebased["adaptation_payload"]["grouping"]["protocol"] == "model_grouping/2"
    # One new grouping, on the same baseline as round 1's, under the new digest.
    assert len(client.requests) == GROUPING
    first, second = payloads(run, "grouping_started")
    assert (first["round"], second["round"]) == (1, 2)
    assert second["baseline_binding"] == first["baseline_binding"]
    assert second["registry_digest"] == first["registry_digest"]
    assert first == before
    assert second["policy_digest"] != first["policy_digest"]
    # Round 1 keeps its `/15`-protocol grouping; only round 2's checkpoints.
    assert {p["grouping_id"] for p in payloads(run, "grouping_portion_finished")} == {
        second["grouping_id"]
    }
    portions = payloads(run, "focus_portion_selected")
    assert [p["policy_digest"] for p in portions] == [
        first["policy_digest"],
        second["policy_digest"],
    ]
    assert audit_mismatches(run) == []


def test_rollback_b15_refuses_v2_payload(tmp_path):
    """`2.0.0b15` admits only `model_grouping/1` in a `chat` slot (spec §11).

    The rule is b15's `_validate_model_grouping` protocol check, which the
    current validator keeps as its default `protocols`; the caller pops what
    b15 popped (`provider_order`, `allow_fallbacks`, `max_repairs`,
    `max_consecutive_full_fallbacks`) and nothing else, so a portion key
    reaches the closed-field check as it did in b15.
    """

    from evolution.tree.campaign_identity import AdaptationIdentityV1, IdentityError
    from evolution.tree.evolve import _chat_grouper_identity
    from evolution.tree.manifest import manifest_v2 as manifest
    from tests.test_tree_b14_manifest_identity import grouper_document

    def b15_rule(payload: dict[str, Any]) -> None:
        value = dict(payload)
        for popped in (
            "provider_order",
            "allow_fallbacks",
            "max_repairs",
            "max_consecutive_full_fallbacks",
        ):
            value.pop(popped, None)
        AdaptationIdentityV1._validate_model_grouping(value, protocols=("model_grouping/1",))

    def section(**keys):
        return manifest._parse_grouper_section(
            {**grouper_document(), **keys},
            "grouper",
            tmp_path,
            tmp_path / "seed",
            schema=manifest.SCHEMA_V16,
        )

    b15_rule(_chat_grouper_identity(section(), family15=False))
    with pytest.raises(IdentityError):
        b15_rule(_chat_grouper_identity(section(), family15=True))
    declared = _chat_grouper_identity(section(glimpse_chars=900), family15=True)
    with pytest.raises(IdentityError):
        b15_rule({**declared, "protocol": "model_grouping/1"})


# -- reported provenance and role inputs ---------------------------------------------


async def test_reported_provenance_reaches_header_not_role_fields(tmp_path, monkeypatch):
    from evolution.tree.roles.editor_contracts import brief_payload
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload

    briefs: list[dict[str, Any]] = []
    edits: list[dict[str, Any]] = []
    real_reflector = campaign_module.run_reflector
    real_editor = campaign_module.run_editor

    async def capture_reflector(reflector, request, **kwargs):
        briefs.append(reflect_brief_payload(request.evidence))
        return await real_reflector(reflector, request, **kwargs)

    async def capture_editor(editor, request, **kwargs):
        edits.append(brief_payload(request.brief))
        return await real_editor(editor, request, **kwargs)

    monkeypatch.setattr(campaign_module, "run_reflector", capture_reflector)
    monkeypatch.setattr(campaign_module, "run_editor", capture_editor)
    client = use_chat_grouper(
        monkeypatch, PortionGrouper(budget=GROUPING, failure_mode=NAMING, question="q?")
    )
    run, result = await public_run(tmp_path, monkeypatch, configure=_chat(), used=_reported)
    assert result.terminal == "finished", (result.owner, result.code)
    assert len(client.requests) == GROUPING
    (case,) = _portion(client.requests[1])["cases"]
    assert case["header"]["provenance"] == {
        "components": [{"name": COMPONENT, "count": 1}],
        "unknown": 0,
    }
    assert briefs and edits
    for shown in briefs + edits:
        assert not (_keys(shown) & PROVENANCE_FIELDS)
    # The Grouper's text reaches the Reflector's brief; no other field there
    # carries the component.
    text = canonical_json(briefs[0]).decode()
    assert NAMING in text
    assert COMPONENT not in text.replace(NAMING, "")
    for edit in edits:
        edited = canonical_json(edit).decode()
        assert COMPONENT not in edited.replace(NAMING, "")
    assert audit_mismatches(run) == []


async def test_a_run_without_reported_provenance_shows_no_provenance_line(tmp_path, monkeypatch):
    client = use_chat_grouper(monkeypatch, PortionGrouper(budget=GROUPING))
    run, result = await public_run(tmp_path, monkeypatch, configure=_chat())
    assert result.terminal == "finished"
    (case,) = _portion(client.requests[1])["cases"]
    assert "provenance" not in case["header"]


async def test_mixed_reporting_repeats_map_provenance_per_observation(tmp_path, monkeypatch):
    """R=2 with one repeat of `c1` reporting its components and the other not:
    `_grouping_provenance` maps each observation to its own sample's stored
    `used` union, and the case header counts one reported and one unknown."""

    from tests.campaign_drivers import _records

    sessions: dict[str, int] = {}

    def first_session_reports(case: str, turn: int) -> list[str] | None:
        # The baseline's first `c1` sample reports; its second never does.
        if turn == 0:
            sessions[case] = sessions.get(case, 0) + 1
        return [COMPONENT] if case == "c1" and sessions[case] == 1 else None

    real = campaign_module._Campaign._grouping_provenance
    mapped: list[dict[str, Any]] = []
    baselines: list[str] = []

    def capture(self, round_number):
        value = real(self, round_number)
        mapped.append(value)
        baselines.append(str(self.snapshot().rounds[round_number].baseline_measurement_id))
        return value

    monkeypatch.setattr(campaign_module._Campaign, "_grouping_provenance", capture)

    def configure(document):
        document["grouper"] = chat_grouper_section()
        document["campaign"]["evaluation"]["repeats_per_question"] = 2

    client = use_chat_grouper(monkeypatch, PortionGrouper(budget=GROUPING))
    run, result = await public_run(
        tmp_path, monkeypatch, configure=configure, used=first_session_reports
    )
    assert result.terminal == "finished", (result.owner, result.code)
    assert mapped

    # The expectation, read from the baseline journal by sample ID.
    rows = _records(run / "measurements" / baselines[0] / "events.jsonl")
    schedule = rows[0]["payload"]["identity"]["schedule"]
    repeat_of = {(e["case_id"], e["sample_id"]): e["repeat"] for e in schedule}
    expected = {}
    for row in rows:
        if row["event"] == "rollout_finished":
            payload = row["payload"]
            used = payload.get("provenance")
            key = (payload["case_id"], payload["sample_id"])
            expected[f"obs:{key[0]}:{repeat_of[key]}"] = None if used is None else tuple(used)
    assert sorted(repeat for (case, _), repeat in repeat_of.items() if case == "c1") == [1, 2]
    assert mapped[0] == expected
    c1 = {key: value for key, value in mapped[0].items() if key.startswith("obs:c1:")}
    assert sorted(c1.values(), key=lambda value: value is None) == [(COMPONENT,), None]

    (case,) = _portion(client.requests[1])["cases"]
    assert case["header"]["provenance"] == {
        "components": [{"name": COMPONENT, "count": 1}],
        "unknown": 1,
    }
    assert audit_mismatches(run) == []


async def test_v15_keeps_provenance_unavailable(tmp_path, monkeypatch):
    from tests.family14_public import family14_public_run
    from tests.model_grouping_fakes import failing_members

    client = use_chat_grouper(monkeypatch, ScriptedClient([group_everything()]))
    run, result = await family14_public_run(
        tmp_path,
        monkeypatch,
        family14_settings={"grouper": chat_grouper_section()},
        used=_reported,
    )
    assert result.terminal == "finished", (result.owner, result.code)
    assert _grouping(run)["protocol"] == "model_grouping/1"
    (request,) = client.requests
    assert failing_members(request)
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    from evolution.tree.roles.grouping.model_grouping_runtime import MAP_LEAD

    bundles = [json.loads(line) for line in user[len(MAP_LEAD) :].split("\n")]
    assert bundles and all(b["provenance"] == {"state": "unavailable"} for b in bundles)
    assert COMPONENT not in json.dumps(request)
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
async def test_a_component_equal_to_a_run_secret(tmp_path, monkeypatch, mode):
    """Review Focus 3 and the C6 redaction note: enforced, the provider never
    sees the value; shadowed, the one component counts one
    `input_run_secret_redaction` hit, however often headers and cards show it.

    A target cannot get a run secret into a stored `used` union (the
    measurement journal redacts it, and `[REDACTED]` is no component ID), so
    the campaign's provenance mapping is patched to report one for every
    failing observation of two cases.
    """

    secret = "grouper-secret-584396"
    monkeypatch.setenv("GROUPING_TEST_KEY", secret)
    client = use_chat_grouper(monkeypatch, PortionGrouper(budget=GROUPING))
    real = campaign_module._Campaign._grouping_provenance
    reported: list[dict[str, Any]] = []

    def provenance(self, round_number):
        shown = {key: (secret, COMPONENT) for key in real(self, round_number)}
        reported.append(shown)
        return shown

    monkeypatch.setattr(campaign_module._Campaign, "_grouping_provenance", provenance)

    def configure(document):
        document["grouper"] = chat_grouper_section()
        document["scorer"]["allowed_env"] = ["GROUPING_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"input_run_secret_redaction": mode}}

    run, result = await public_run(
        tmp_path, monkeypatch, configure=configure, extra_cases={"c3": {"critical": True}}
    )
    assert result.terminal == "finished", (result.owner, result.code)
    assert reported and reported[0]
    assert len(client.requests) == GROUPING
    cases = _portion(client.requests[1])["cases"]
    assert len(cases) == 2
    shown = {
        entry["name"] for case in cases for entry in case["header"]["provenance"]["components"]
    }
    (finished,) = payloads(run, "grouping_finished")
    hits = [e for e in finished.get("shadowed", []) if e["check"] == "input_run_secret_redaction"]
    if mode == "enforce":
        assert shown == {"[REDACTED]", COMPONENT}
        assert all(secret not in json.dumps(request) for request in client.requests)
        assert hits == []
    else:
        assert shown == {secret, COMPONENT}
        assert hits == [{"check": "input_run_secret_redaction", "code": "redacted", "count": 1}]
    assert secret not in (run / "campaign.jsonl").read_text()
    assert all(secret not in p.read_text() for p in (run / "reflector-logs").glob("*.jsonl"))
    assert audit_mismatches(run) == []


# -- a `/15`-style declared prompt ---------------------------------------------------


class _V15Answers(PortionGrouper):
    """A provider following a `/15` prompt: it answers with `/15`'s `{"groups": ...}`
    text and no tool call; ``probes`` still makes the preflight's `list_case` call,
    which that request asks for in so many words."""

    def __init__(self, *, probes: bool, **options: Any) -> None:
        super().__init__(**options)
        self.probes = probes

    def _answer(self, kw: Any) -> Any:
        if self.probes and _tools(kw) == ["list_case"]:
            return super()._answer(kw)
        return grouping_answer(map_group("g1", [("c1#1", "v")])), []


#: Requests of a grouping whose model never calls a tool: the preflight tries
#: each request style (`modern`, `modern_single`, `modern_auto`, then `legacy` up to its turn
#: limit) and fails, and no portion is asked.
NO_PROBE = 6
#: Requests of a grouping whose model makes the probe call but answers its
#: portion in `/15` text: the preflight, the answer and its protocol repairs.
PROBED = 4


@pytest.mark.parametrize("probes,requests", [(False, NO_PROBE), (True, PROBED)])
async def test_v1_style_declared_prompt_falls_back(tmp_path, monkeypatch, probes, requests):
    """Review Focus 2: a `/16` Grouper declaring a `/15`-style prompt. Every
    answer is `invalid_output` (or the probe fails first), the declared
    fallback groups everything, and the run continues as a full fallback does
    today."""

    from evolution.prompts import TREE_GROUPING_MODEL_SYSTEM

    (tmp_path / "grouper-v15.md").write_text(TREE_GROUPING_MODEL_SYSTEM + "\n")
    client = use_chat_grouper(monkeypatch, _V15Answers(probes=probes, budget=requests))
    fallback = {
        "kind": "fields",
        "features": [
            {
                "name": "terminal",
                "source": "observation",
                "path": "/terminal",
                "description": "Terminal observation status.",
            }
        ],
    }

    def configure(document):
        document["grouper"] = chat_grouper_section(
            system_prompt=str(tmp_path / "grouper-v15.md"), fallback=fallback
        )

    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished", (result.owner, result.code)
    assert len(client.requests) == requests
    (finished,) = payloads(run, "grouping_finished")
    assert finished["fallback"] == {"kind": "fields", "scope": "all", "reason": "invalid_output"}
    assert payloads(run, "reflection_finished")
    assert audit_mismatches(run) == []
