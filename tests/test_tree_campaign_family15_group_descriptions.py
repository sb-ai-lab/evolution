"""b17 wave 2 (ADR-0057, PDR-0049): family-15 group descriptions reach the
Reflector whole, stand alone and stay short.

Every scenario is a genuine `/16` campaign (`public_run`) whose `chat` Grouper
is the scripted `model_grouping/2` provider, or a genuine family-14 run
(`family14_run`) for the unchanged-side contrast. The Reflector's first
request is captured where the campaign dispatches it (`run_reflector`): the
brief's groups payload is what the Reflector reads, never cut.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import LENIENT, audit_mismatches
from tests.model_grouping_fakes import (
    PortionGrouper,
    ScriptedClient,
    chat_grouper_section,
    group_everything,
    use_chat_grouper,
)
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow

#: Near-bound Cyrillic header texts, each at its 512-character admitted bound,
#: so the whole description (1,568 characters) is far over today's 512 cut and
#: a handful of groups fill the 64 KiB descriptions map.
OBSERVED = "ж" * 512
EXPECTED = "ф" * 512
QUESTION = "й" * 512
TITLE = "т" * 128
HEADER = {
    "title": TITLE,
    "failure_mode": OBSERVED,
    "expected_behaviour": EXPECTED,
    "question": QUESTION,
}
WHOLE = f"Observed: {OBSERVED}\nExpected: {EXPECTED}\nQuestion: {QUESTION}"


class OneGroupPerUnit(PortionGrouper):
    """A portion answer of one group per unit, each carrying `HEADER`'s texts.

    Forty-five scored failing cases fill the map even in its trimmed form.
    The first selected group therefore needs its full-description record too.
    """

    def _answer(self, kw: Any) -> tuple[str | None, list[tuple[str, Any]]]:
        from evolution.tree.roles.grouping.portion_session import PORTION_LEAD

        tools = [tool["function"]["name"] for tool in kw.get("tools") or ()]
        tools += [function["name"] for function in kw.get("functions") or ()]
        if not tools:
            return json.dumps({"merges": []}), []
        if tools == ["list_case"]:
            return None, [("list_case", {"case": "c1"})]
        user = next(m["content"] for m in kw["messages"] if m["role"] == "user")
        portion = json.loads(user[len(PORTION_LEAD) :])
        groups = []
        index = 0
        for case in portion["cases"]:
            for unit in case["units"]:
                index += 1
                groups.append(
                    {
                        "id": f"g{index}",
                        **HEADER,
                        "members": [{"ref": unit["ref"], "variant": "v"}],
                        "citations": [],
                        "priority": "p",
                    }
                )
        return None, [("finish_portion", {"groups": groups})]


def _capture_reflector(monkeypatch: Any) -> list[Any]:
    """The groups payload of every Reflector request the campaign dispatches."""

    briefs: list[Any] = []
    real = campaign_module.run_reflector

    async def capture(reflector: Any, request: Any, **kw: Any):
        briefs.append(request.evidence.payload["groups"])
        return await real(reflector, request, **kw)

    monkeypatch.setattr(campaign_module, "run_reflector", capture)
    return briefs


def _brief_descriptions(briefs: list[Any]) -> list[str]:
    """The description text of every group the briefs carry."""

    return [
        g["description"]["description"]
        for groups in briefs
        for g in groups
        if g.get("description") is not None
    ]


def _capture_group_numbers(monkeypatch):
    numbers = {}
    real = campaign_module._Campaign._with_full_descriptions

    def capture(self, stored, admitted):
        result = real(self, stored, admitted)
        numbers.update((group.group_id, group.group_number) for group in result.neighborhoods)
        return result

    monkeypatch.setattr(campaign_module._Campaign, "_with_full_descriptions", capture)
    return numbers


def _chat(*, reflector_url=None, **overrides: Any):
    def configure(document: Any) -> None:
        document["grouper"] = chat_grouper_section(**overrides)
        if reflector_url is not None:
            from tests.test_tree_campaign_family14_reflector_history import chat_reflector
            from tests.test_tree_campaign_family15_role_contracts import b17_reflector

            b17_reflector(document, chat_reflector(reflector_url))

    return configure


def _overflow_cases() -> dict[str, Any]:
    """ "c3" to "c46" failing beside "c1": forty-five failing cases in all."""

    return {f"c{number}": {"pass": False, "critical": True} for number in range(3, 47)}


@pytest.fixture
def reflector_provider(monkeypatch):
    from tests import fake_chat_server as fake

    monkeypatch.setenv("E1_KEY", "test-key")

    def reply(body):
        tools = [t["function"]["name"] for t in body.get("tools", [])]
        return (
            fake.tool_call("list_files")
            if tools == ["list_files"]
            else fake.tool_call("finish_reflection", {"groups": [], "declined": True})
        )

    base, server = fake.start([reply])
    try:
        yield base, server.script.requests
    finally:
        server.shutdown()
        server.server_close()


def _first_opening(requests):
    request = next(r for r in requests if len(r.get("tools", [])) > 1)
    return next(
        json.loads(line)
        for line in request["messages"][1]["content"].splitlines()
        if line.startswith("{")
    )


async def test_near_bound_group_reaches_the_reflectors_first_request_whole(
    tmp_path, monkeypatch, reflector_provider
):
    """#388's example: a group whose three fields sit at their admitted bounds
    reaches the Reflector whole -- no cut, the question present -- and a map
    that fits writes no full-description record."""

    briefs = _capture_reflector(monkeypatch)
    use_chat_grouper(monkeypatch, PortionGrouper(budget=2, **HEADER))
    base, requests = reflector_provider
    run, result = await public_run(tmp_path, monkeypatch, configure=_chat(reflector_url=base))
    assert result.terminal == "finished", (result.owner, result.code)
    (finished,) = payloads(run, "grouping_finished")
    assert "full_descriptions_digest" not in finished
    assert payloads(run, "grouping_full_description") == []
    assert _brief_descriptions(briefs) == [WHOLE]
    assert _first_opening(requests)["groups"][0]["description"]["description"] == WHOLE


async def test_family14_chat_grouping_keeps_the_512_cut(tmp_path, monkeypatch):
    """The same group through a family-14 `chat` Grouper is byte-identical to
    today's: the whole template cut to 512 characters, the question lost."""

    briefs = _capture_reflector(monkeypatch)
    use_chat_grouper(monkeypatch, ScriptedClient([group_everything(**HEADER)]))
    run, _ = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
    )
    assert _brief_descriptions(briefs) == [WHOLE[:512]]
    (finished,) = payloads(run, "grouping_finished")
    assert "full_descriptions_digest" not in finished


async def test_map_overflow_records_whole_descriptions_and_the_reflector_reads_one(
    tmp_path, monkeypatch, reflector_provider
):
    """A descriptions map over its 64 KiB bound, in Cyrillic text: the fit
    trims lower-ranked entries (question whole, cut fields marked), the
    campaign journals one full-description record per shortened or dropped
    entry and the terminal binds them by one digest, and the Reflector reads
    a selected group's whole text."""

    group_numbers = _capture_group_numbers(monkeypatch)
    base, requests = reflector_provider
    briefs = _capture_reflector(monkeypatch)
    use_chat_grouper(monkeypatch, OneGroupPerUnit(budget=256))
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=_chat(reflector_url=base),
        extra_cases=_overflow_cases(),
        campaign={"indeterminate": LENIENT},
    )
    assert result.terminal == "finished", (result.owner, result.code)
    (finished,) = payloads(run, "grouping_finished")
    assert finished["fallback"] is None
    records = payloads(run, "grouping_full_description")
    assert records, "an over-bound map trims entries and journals their records"
    # The private grouping log counts the records written.
    logged = "".join(p.read_text() for p in (run / "reflector-logs").glob("grouping-*.jsonl"))
    trimmed = [json.loads(line) for line in logged.splitlines() if "grouping_trimmed" in line]
    assert trimmed and sum(item["full_descriptions"] for item in trimmed) == len(records)
    entries = {
        int(record["group"]): {"label": record["label"], "description": record["description"]}
        for record in records
    }
    # The terminal binds exactly the journaled records.
    assert (
        finished["full_descriptions_digest"]
        == hashlib.sha256(
            canonical_json(
                [
                    {
                        "description": entries[number]["description"],
                        "group": number,
                        "label": entries[number]["label"],
                    }
                    for number in sorted(entries)
                ]
            )
        ).hexdigest()
    )
    # A record is the group's whole description, exactly as §3.1 builds it.
    assert {entry["description"] for entry in entries.values()} == {WHOLE}
    assert all(entry["label"] == TITLE for entry in entries.values())
    # Every surviving map entry keeps its question whole; a shortened one
    # marks both shortened fields and nothing else.
    for entry in finished["result"]["descriptions"].values():
        assert entry["description"].endswith("Question: " + QUESTION)
        if entry["description"] != WHOLE:
            assert entry["description"].count("…[cut]") == 2
            assert entry["label"] == TITLE[:63] + "…"
    # The group the round selected reaches the Reflector whole, whether or
    # not its map entry was trimmed.
    described = _brief_descriptions(briefs)
    assert described and all(text == WHOLE for text in described)
    opening = _first_opening(requests)
    selected_numbers = {group_numbers[group["group_id"]] for group in opening["groups"]}
    assert selected_numbers & entries.keys(), "the selected group must have overflowed"
    assert opening["groups"][0]["description"]["description"] == WHOLE
    assert audit_mismatches(run) == []


async def test_crash_between_record_and_terminal_ignores_the_records(
    tmp_path, monkeypatch, reflector_provider
):
    """A crash between a full-description record and `grouping_finished`
    leaves an interrupted grouping: on resume it regroups, the orphan records
    are ignored, and the new grouping binds its own."""

    base, requests = reflector_provider
    extra = _overflow_cases()
    use_chat_grouper(monkeypatch, OneGroupPerUnit(budget=256))
    real = campaign_module._Campaign._write_full_descriptions

    def cut_after_records(self, grouping_id, full, *args):
        real(self, grouping_id, full, *args)
        raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(campaign_module._Campaign, "_write_full_descriptions", cut_after_records)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                monkeypatch,
                configure=_chat(reflector_url=base),
                extra_cases=extra,
                campaign={"indeterminate": LENIENT},
            )
    run = tmp_path / "run"
    (started,) = payloads(run, "grouping_started")
    stale = payloads(run, "grouping_full_description")
    assert stale and payloads(run, "grouping_finished") == []

    use_chat_grouper(monkeypatch, OneGroupPerUnit(budget=256))
    _, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=_chat(reflector_url=base),
        extra_cases=_overflow_cases(),
        campaign={"indeterminate": LENIENT},
    )
    assert result.terminal == "finished", (result.owner, result.code)
    # The orphan records of the interrupted grouping stay on the journal,
    # unbound by any terminal; the new grouping wrote and bound its own.
    assert payloads(run, "grouping_full_description")[: len(stale)] == stale
    assert len(payloads(run, "grouping_full_description")) > len(stale)
    (finished,) = payloads(run, "grouping_finished")
    assert started["grouping_id"] != finished["grouping_id"]
    assert _first_opening(requests)["groups"][0]["description"]["description"] == WHOLE
    assert audit_mismatches(run) == []


async def test_resume_after_grouping_finished_keeps_the_binding(
    tmp_path, monkeypatch, reflector_provider
):
    """A crash after the terminal: the resume re-admits the recorded grouping
    -- records and digest -- and the verified inspection reads the run."""

    base, requests = reflector_provider
    use_chat_grouper(monkeypatch, OneGroupPerUnit(budget=256))
    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=_chat(reflector_url=base),
            extra_cases=_overflow_cases(),
            cut="after_grouping_finished",
            campaign={"indeterminate": LENIENT},
        )
    run = tmp_path / "run"
    records = payloads(run, "grouping_full_description")
    (finished,) = payloads(run, "grouping_finished")
    assert records and "full_descriptions_digest" in finished

    use_chat_grouper(monkeypatch, OneGroupPerUnit(budget=256))
    _, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=_chat(reflector_url=base),
        extra_cases=_overflow_cases(),
        campaign={"indeterminate": LENIENT},
    )
    assert result.terminal == "finished", (result.owner, result.code)
    from evolution.tree.campaign import inspect_campaign

    report = inspect_campaign(run, verify=True).to_payload()
    assert report["verification"]["mismatches"] == []
    assert _first_opening(requests)["groups"][0]["description"]["description"] == WHOLE
    assert audit_mismatches(run) == []


async def test_declared_system_prompt_wins_and_gets_no_rules(tmp_path, monkeypatch):
    """A declared `system_prompt` wins over the packaged default and does not
    get the two rules: the identity seals the file's own digest and the role
    call sends the file's bytes."""

    from evolution.prompts import TREE_GROUPING_MODEL_V3_SYSTEM

    prompt = tmp_path / "grouper-system.md"
    prompt.write_text("Group the failures your own way.\n")
    client = use_chat_grouper(monkeypatch, PortionGrouper(budget=64, **HEADER))
    run, result = await public_run(
        tmp_path, monkeypatch, configure=_chat(system_prompt=str(prompt))
    )
    assert result.terminal == "finished", (result.owner, result.code)
    (created,) = payloads(run, "campaign_created")
    grouping = created["adaptation_payload"]["grouping"]
    assert grouping["prompt_sha256"] == hashlib.sha256(prompt.read_bytes()).hexdigest()
    assert (
        grouping["prompt_sha256"]
        != hashlib.sha256(TREE_GROUPING_MODEL_V3_SYSTEM.encode()).hexdigest()
    )
    # The portion session's system message is the declared file, not the
    # packaged default (requests[0] is the preflight, with its own ask). The
    # runtime strips a declared prompt's surrounding whitespace before the
    # send, exactly as it strips the packaged one.
    sent = client.requests[1]["messages"][0]["content"]
    assert sent == "Group the failures your own way."


@pytest.mark.parametrize(
    "damage", ["digest", "missing_digest", "unknown_group", "unknown_grouping", "duplicate"]
)
async def test_full_description_binding_is_checked_by_replay_and_independent_audit(
    tmp_path, monkeypatch, damage
):
    from types import SimpleNamespace

    from evolution.tree.campaign_schema import CampaignLifecycle, SchemaError
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from evolution.tree.storage.envelope import Envelope

    use_chat_grouper(monkeypatch, OneGroupPerUnit(budget=256))
    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        configure=_chat(),
        extra_cases=_overflow_cases(),
        campaign={"indeterminate": LENIENT},
    )
    rows = [json.loads(line) for line in (run / "campaign.jsonl").read_text().splitlines()]
    terminal = next(row for row in rows if row["event"] == "grouping_finished")
    records = [row for row in rows if row["event"] == "grouping_full_description"]
    assert records
    if damage == "digest":
        terminal["payload"]["full_descriptions_digest"] = "f" * 64
    elif damage == "missing_digest":
        terminal["payload"].pop("full_descriptions_digest")
    elif damage == "unknown_group":
        records[0]["payload"]["group"] = 32767
    elif damage == "unknown_grouping":
        terminal["payload"]["grouping_id"] = "not-started"
    else:
        duplicate = json.loads(json.dumps(records[0]))
        rows.insert(rows.index(terminal), duplicate)
        for sequence, row in enumerate(rows, 1):
            row["sequence"] = sequence
    envelopes = tuple(
        Envelope.from_payload(row, family=campaign_module.campaign_family_for(15))
        for row in rows[: rows.index(terminal) + 1]
    )
    life = CampaignLifecycle(selector=None, family_version=15)
    with pytest.raises(SchemaError):
        for envelope in envelopes:
            transition = life.prepare(envelope)
            transition.commit()
    # Deliberately bypass replay: the audit must establish the binding itself.
    audit = Family15Audit.__new__(Family15Audit)
    audit.context = SimpleNamespace(records=envelopes)
    with pytest.raises(EvidenceMismatch):
        audit.check(envelopes[-1])


async def test_pre_wave_recorded_512_description_resumes_unchanged(
    tmp_path, monkeypatch, reflector_provider
):
    from evolution.tree.roles.grouping import model_grouping_runtime

    base, requests = reflector_provider
    real = model_grouping_runtime.project

    def old_projection(*args, **kwargs):
        return real(*args, **{**kwargs, "family15": False})

    use_chat_grouper(monkeypatch, PortionGrouper(budget=2, **HEADER))
    with monkeypatch.context() as patch:
        patch.setattr(model_grouping_runtime, "project", old_projection)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                monkeypatch,
                configure=_chat(reflector_url=base),
                cut="after_grouping_finished",
            )
    run = tmp_path / "run"
    (before,) = payloads(run, "grouping_finished")
    assert not payloads(run, "grouping_full_description")
    assert next(iter(before["result"]["descriptions"].values()))["description"] == WHOLE[:512]
    _, result = await public_run(tmp_path, monkeypatch, configure=_chat(reflector_url=base))
    assert result.terminal == "finished"
    assert payloads(run, "grouping_finished") == [before]
    assert _first_opening(requests)["groups"][0]["description"]["description"] == WHOLE[:512]


async def test_issue388_docx_question_and_expectation_reach_actual_request(
    tmp_path, monkeypatch, reflector_provider
):
    # The published #388 question and expectation prefix, with synthetic
    # surrounding prose; no operator transcript or private case data is copied.
    header = {
        "title": "Template substitution leaves placeholders",
        "failure_mode": (
            "The generated document retains placeholders instead of actual values. "
            "Its summary counts and accuracy are unresolved, rows are duplicated, "
            "and case identifiers no longer match their statuses. "
        )
        * 2,
        "expected_behaviour": (
            "The report should contain actual values for PASS_COUNT, FAIL_COUNT, "
            "ACCURACY, case IDs, statuses, and expected results. Each row should "
            "preserve its input case identity without extra rows or unresolved placeholders."
        ),
        "question": (
            "Why did the template substitution process fail to replace placeholders with actual values, "
            "and why did it introduce data integrity issues like extra rows and mismatched case IDs?"
        ),
    }
    assert len(header["failure_mode"]) <= 512
    expected = f"Observed: {header['failure_mode']}\nExpected: {header['expected_behaviour']}\nQuestion: {header['question']}"
    assert len(expected) > 512
    base, requests = reflector_provider
    use_chat_grouper(monkeypatch, PortionGrouper(budget=2, **header))
    _, result = await public_run(tmp_path, monkeypatch, configure=_chat(reflector_url=base))
    assert result.terminal == "finished"
    assert _first_opening(requests)["groups"][0]["description"]["description"] == expected
