"""H8 genuine family-14 runs with a `fields`, `command` or `chat` Grouper.

Every run here is a real `family14_run` engine invocation (creation, lifecycle
admission, adapters, scoring and journals are real), stopped before H12's
public report assembly; the family-13 contrasts are real `/14` runs through
`_two_case_run(as_family_13=...)` with `reflector.grouping.kind: model`. The
Grouper's provider is a `ScriptedClient` bound in place of
`model_grouping_runtime.new_grouping_client`; a `command` Grouper runs a
sealed program written under `tmp_path`. Fault injections are named where
they are used.

Task 1's diagnostic: at the base a family-14 `chat` grouping could not write
`grouping_finished`, because the lifecycle let only a grouping of kind `model`
journal `calls`, and the engine swallowed the refusal into
`grouping_failed(malformed_result)`. `run_grouping` now lets a refused
append propagate as `CampaignInvariantViolated` (b16 C7); every test here
still asserts the exact terminal records and that `_spy_refusals` saw no
lifecycle refusal.

Task 5 (A§10.1, A§10.3; ADR-0034, plan decisions 8, 9, 11): every H8 kind
groups and completes its round; every `chat` provider class suspends
`grouper_provider_failed` in family 14 and falls back in family 13; content
failures fall back in both; an unexpected exception around the `chat` role
call suspends `grouper_failing`; the existing `GROUPING_FAILURE_CODES` routes
are unchanged for every kind.

Task 6 (A§10.5; ADR-0034, plan decisions 6, 7, 10, 15): the fallback chain on
consecutive refresh groupings. Two full fallbacks in a row (an empty answer
counts) suspend `grouper_failing` with one `grouping_failed(role_failed)` and
no second fallback result; a partial fallback leaves the chain; an admitted
grouping or any suspension breaks it; an authorized Grouper change does not.
"""

from __future__ import annotations

import hashlib
import sys
import time
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.campaign import CampaignInvariantViolated
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.grouping import model_grouping_runtime
from evolution.tree.roles.grouping.grouping_runtime import GroupingRuntime
from evolution.tree.roles.role_failures import GroupingExecutionError
from tests.campaign_drivers import STATISTICAL, WORSE_SCORER, _records
from tests.model_grouping_fakes import (
    ScriptedClient,
    bad_request,
    chat_grouper_section,
    group_everything,
    grouping_answer,
    server_error,
    use_chat_grouper,
)
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

#: A provider's own text: it must never reach the campaign journal.
PROVIDER_TEXT = "provider-said-h8-0b9f"
CHAT_CALLS = {"map_requests": 1, "reduce": "none", "attempts": 1, "repairs": 0}
#: A `chat` map prompt longer than `max_input_chars`' floor (8000): the
#: grouping falls back `("all", "input_too_large")` before any provider call.
OVERSIZED_PROMPT = "Group the failing observations.\n" + "x" * 9000 + "\n"

#: A sealed `command` grouping program: one group over every failing input.
GROUP_ALL_PROGRAM = (
    "import json, subprocess, sys\n"
    "request = json.load(open(sys.argv[-1]))\n"
    "entries = json.loads(subprocess.check_output("
    "request['helpers']['list_grouping_inputs']))['entries']\n"
    "print(json.dumps({'version': 'reflect_grouping_result/1', "
    "'registry_digest': request['registry_digest'], "
    "'assignments': [0 if entry['failing'] else None for entry in entries], "
    "'descriptions': {'0': {'label': 'every failure', 'description': 'every failure'}}}))\n"
)


def _command_grouper(tmp_path, body: str, **overrides: Any) -> dict[str, Any]:
    """A `/15` `command` Grouper section running `body` as `grouping/group.py`."""

    (tmp_path / "grouping").mkdir()
    (tmp_path / "grouping" / "group.py").write_text(body)
    return {
        "kind": "command",
        "argv": [sys.executable, "group.py"],
        "cwd": "grouping",
        "dependency_paths": [],
        "allowed_env": [],
        "timeout_seconds": 60,
        "config": {},
        **overrides,
    }


def _spy_refusals(monkeypatch) -> list[CampaignInvariantViolated]:
    """Every lifecycle refusal `_Campaign.prove` raises, re-raised unchanged.

    `run_grouping` lets a refused append leave the engine unchanged (b16 C7);
    the spy also sees a refusal that a caller up the stack would handle.
    """
    refusals: list[CampaignInvariantViolated] = []
    original = campaign_module._Campaign.prove

    def prove(self, event, payload, operation_id):
        try:
            return original(self, event, payload, operation_id)
        except CampaignInvariantViolated as exc:
            refusals.append(exc)
            raise

    monkeypatch.setattr(campaign_module._Campaign, "prove", prove)
    return refusals


class _Clock:
    """`time.monotonic` plus an offset a script item can move past the deadline."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def _use_clock(monkeypatch, clock: _Clock) -> None:
    """Bind `clock` as the grouping role call's clock (its only seam: the default
    argument of `GroupingRuntime.model`); everything else stays the engine's."""

    model = GroupingRuntime.model

    async def timed(self, *args, **kwargs):
        return await model(self, *args, clock=clock, **kwargs)

    monkeypatch.setattr(GroupingRuntime, "model", timed)


def _provider_script(name: str, monkeypatch) -> list[Any]:
    """The scripted provider of one `chat` provider class.

    `outage`: a transient 503 on every one of the three attempts; `rejected`:
    a 400; `deadline`: a transient 503 answered after the operation deadline
    has passed (the clock jumps while the request is in flight).
    """
    if name == "outage":
        return [server_error(PROVIDER_TEXT) for _ in range(3)]
    if name == "rejected":
        return [bad_request(PROVIDER_TEXT)]
    clock = _Clock()
    _use_clock(monkeypatch, clock)

    def late(_request):
        clock.offset = 1_000_000.0
        return server_error(PROVIDER_TEXT)

    return [late]


def _events(run) -> list[str]:
    return [record["event"] for record in _records(run / "campaign.jsonl")]


def _record(run, event: str) -> dict[str, Any]:
    (record,) = [r for r in _records(run / "campaign.jsonl") if r["event"] == event]
    return record


def _grouper_digest(run) -> str:
    """The Grouper identity digest (A§3.4): its slot in the adaptation identity."""

    created = _records(run / "campaign.jsonl")[0]["payload"]
    grouping = created["adaptation_payload"]["grouping"]
    return hashlib.sha256(
        canonical_json({"version": "reflect_grouping/1", "grouping": grouping})
    ).hexdigest()


def _assert_suspended_on_grouping(
    run, snapshot, *, failed: dict[str, Any], suspension: tuple, audited: bool = True
):
    """The run's one grouping ended in exactly `failed` and the suspension
    `(reason, owner)` naming it, with nothing after; no round closed. With
    `audited`, the journal audits clean (Task 8: the audit holds each Grouper
    suspension to its record's class)."""

    records = _records(run / "campaign.jsonl")
    started = _record(run, "grouping_started")
    terminal = _record(run, "grouping_failed")
    assert terminal["payload"] == {
        "round": 1,
        "grouping_id": started["payload"]["grouping_id"],
        "reads": terminal["payload"]["reads"],
        **failed,
    }
    assert terminal["operation_id"] == started["operation_id"]
    seq = records.index(terminal) + 1
    reason, owner = suspension
    assert records[-1]["event"] == "campaign_suspended"
    assert records[-1]["payload"] == {
        "reason": reason,
        "owner": owner,
        "evidence": {
            "evidence_kind": "stage_record",
            "seq": seq,
            "operation_id": started["operation_id"],
        },
    }
    assert [r["event"] for r in records[records.index(started) :]] == [
        "grouping_started",
        "grouping_failed",
        "campaign_suspended",
    ]
    assert "grouping_finished" not in _events(run) and "round_closed" not in _events(run)
    assert snapshot.finished is None
    if audited:
        assert audit(run)["mismatches"] == []


# -- A§10.1: every H8 Grouper kind groups and completes its round ----------------


@pytest.mark.parametrize("kind", ["fields", "command", "chat"])
async def test_every_h8_grouper_kind_groups_and_completes_the_round(tmp_path, monkeypatch, kind):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.corpus import load_reflection_corpus

    refusals = _spy_refusals(monkeypatch)
    settings = None
    client = None
    if kind == "chat":
        client = use_chat_grouper(monkeypatch, ScriptedClient([group_everything()]))
        settings = {"grouper": chat_grouper_section()}
    elif kind == "command":
        settings = {"grouper": _command_grouper(tmp_path, GROUP_ALL_PROGRAM)}

    run, snapshot = await family14_run(tmp_path, monkeypatch, family14_settings=settings)

    assert refusals == []
    created = _records(run / "campaign.jsonl")[0]["payload"]
    assert created["adaptation_payload"]["grouping"]["kind"] == kind
    started = _record(run, "grouping_started")["payload"]
    assert started["grouper_kind"] == kind
    assert started["policy_digest"] == _grouper_digest(run)
    (finished,) = payloads(run, "grouping_finished")
    assert finished["grouping_id"] == started["grouping_id"]
    assert finished["fallback"] is None
    assert finished["calls"] == (CHAT_CALLS if kind == "chat" else None)
    assert any(index is not None for index in finished["result"]["assignments"])
    if client is not None:
        assert len(client.requests) == 1
    assert "grouping_failed" not in _events(run) and "campaign_suspended" not in _events(run)
    assert [p["round"] for p in payloads(run, "round_closed")] == [1]
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    corpus = load_reflection_corpus(run.parent / "corpus.jsonl").cases
    assert verify_evidence(run, corpus)["mismatches"] == []


# -- A§10.3: `chat` provider classes -------------------------------------------

PROVIDER_CLASSES = {
    # name: (family-14 reason, family-13 fallback reason)
    "outage": ("provider_exhausted", "provider_unavailable"),
    "rejected": ("provider_rejected", "provider_rejected"),
    "deadline": ("provider_exhausted", "deadline_exhausted"),
}


@pytest.mark.parametrize("name", sorted(PROVIDER_CLASSES))
async def test_a_chat_provider_failure_suspends_in_family_14(tmp_path, monkeypatch, name):
    refusals = _spy_refusals(monkeypatch)
    client = use_chat_grouper(monkeypatch, ScriptedClient(_provider_script(name, monkeypatch)))

    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
    )

    assert refusals == [] and client.script == []
    _assert_suspended_on_grouping(
        run,
        snapshot,
        failed={
            "code": "role_failed",
            "reason": PROVIDER_CLASSES[name][0],
            "error_class": "provider",
        },
        suspension=("grouper_provider_failed", "grouper"),
    )
    assert PROVIDER_TEXT.encode() not in (run / "campaign.jsonl").read_bytes()
    # A provider failure is not the Grouper's `exception`: no class is named.
    assert "role_failed" not in _grouping_log_path(run).read_text()


# -- R47: an unexpected exception around the `chat` role call --------------------


async def test_an_unexpected_exception_in_the_chat_role_call_suspends_grouper_failing(
    tmp_path, monkeypatch
):
    refusals = _spy_refusals(monkeypatch)
    client = use_chat_grouper(monkeypatch, ScriptedClient([RuntimeError(PROVIDER_TEXT)]))

    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
    )

    assert refusals == [] and client.script == [] and client.closed
    _assert_suspended_on_grouping(
        run,
        snapshot,
        failed={"code": "role_failed", "reason": "exception", "error_class": "unknown"},
        suspension=("grouper_failing", "grouper"),
    )
    assert PROVIDER_TEXT.encode() not in (run / "campaign.jsonl").read_bytes()
    # Branch review r1 (A4): the private attempt log names the exception's
    # class, never its message; the journal keys are unchanged.
    log = _grouping_log(run)
    assert log[-1] == {"phase": "role_failed", "exception": "RuntimeError"}
    assert PROVIDER_TEXT.encode() not in _grouping_log_path(run).read_bytes()


def _grouping_log_path(run):
    """The private attempt log of the run's one grouping (`GroupingRuntime.model`)."""

    started = _record(run, "grouping_started")
    return run / "reflector-logs" / f"grouping-{started['operation_id']}.jsonl"


def _grouping_log(run) -> list[dict[str, Any]]:
    import json

    return [json.loads(line) for line in _grouping_log_path(run).read_text().splitlines()]


# -- decision 8: existing routes win for every family ---------------------------


def _missing_sdk(_section, _credentials):
    raise GroupingExecutionError("provider_sdk_missing")


COMMAND_FAULTS = {
    "command_failed": ("import sys\nsys.exit(3)\n", {}),
    "command_timeout": ("import time\ntime.sleep(30)\n", {"timeout_seconds": 1}),
}


@pytest.mark.parametrize("code", sorted(COMMAND_FAULTS))
async def test_a_command_grouper_failure_keeps_grouping_failed(tmp_path, monkeypatch, code):
    refusals = _spy_refusals(monkeypatch)
    body, overrides = COMMAND_FAULTS[code]
    grouper = _command_grouper(tmp_path, body, **overrides)

    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": grouper}
    )

    assert refusals == []
    assert _record(run, "grouping_started")["payload"]["grouper_kind"] == "command"
    _assert_suspended_on_grouping(
        run, snapshot, failed={"code": code}, suspension=("grouping_failed", "campaign")
    )


def _log_fault(name: str, monkeypatch) -> type[Exception]:
    """Make the Grouper's attempt log fail: `open` refuses the descriptor
    (`WorkspaceError`), or `write` fails (`OSError`, a full disk)."""
    import errno

    from evolution.tree.execution.private_files import WorkspaceError
    from evolution.tree.roles import workspace as editor_workspace
    from evolution.tree.roles.workspace import AttemptLog

    if name == "open":

        def refuse(_path, _flags, *, refusal):
            raise WorkspaceError(refusal)

        monkeypatch.setattr(editor_workspace, "open_private_file", refuse)
        return WorkspaceError

    def full(self, value):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(AttemptLog, "record", full)
    return OSError


@pytest.mark.parametrize("name", ["open", "write"])
async def test_an_attempt_log_fault_is_the_campaigns_not_the_groupers(tmp_path, monkeypatch, name):
    """Ruling R6: the attempt log is the campaign's own run directory, so its
    `WorkspaceError` or `OSError` propagates as before, never `role_failed`."""

    use_chat_grouper(monkeypatch, ScriptedClient([group_everything()]))
    raised = _log_fault(name, monkeypatch)

    with pytest.raises(raised):
        await family14_run(
            tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
        )

    events = _events(tmp_path / "run")
    assert events[-1] == "grouping_started"
    assert "grouping_failed" not in events and "campaign_suspended" not in events


async def test_a_client_construction_oserror_is_the_groupers_exception(tmp_path, monkeypatch):
    """Building the provider client is part of the role
    call, so its `OSError` (an SDK loading a missing CA bundle) is
    `role_failed / exception`, unlike the attempt log's."""

    refusals = _spy_refusals(monkeypatch)
    use_chat_grouper(monkeypatch, ScriptedClient([]))

    def unbuildable(_section, _credentials):
        raise OSError(2, f"No such file or directory: '{PROVIDER_TEXT}.pem'")

    monkeypatch.setattr(model_grouping_runtime, "new_grouping_client", unbuildable)

    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": chat_grouper_section()}
    )

    assert refusals == []
    _assert_suspended_on_grouping(
        run,
        snapshot,
        failed={"code": "role_failed", "reason": "exception", "error_class": "unknown"},
        suspension=("grouper_failing", "grouper"),
    )
    assert PROVIDER_TEXT.encode() not in (run / "campaign.jsonl").read_bytes()
    # The runtime raised `role_failed / exception` itself: the log names the
    # class of its cause (`OSError(2, ...)` is a `FileNotFoundError`), never
    # the message, which held a path.
    assert _grouping_log(run)[-1] == {"phase": "role_failed", "exception": "FileNotFoundError"}
    assert PROVIDER_TEXT.encode() not in _grouping_log_path(run).read_bytes()


# -- A§10.5: the fallback chain (ADR-0034; plan decisions 6, 7, 10, 15) ----------
#
# `refresh: 1` with no finalists (every edit scores c1 worse): each round binds
# a refresh baseline and groups once (H7), so consecutive rounds are
# consecutive groupings. The provider is scripted one answer list per grouping.

#: No finalists: every round closes `stalled` and the next one refreshes.
NO_FINALISTS = {**STATISTICAL, "scorer": WORSE_SCORER}
#: An answer the map protocol cannot read, repairs included.
GARBAGE = "these are not the groups you are looking for"
FULL_INVALID = {"kind": "singletons", "scope": "all", "reason": "invalid_output"}
#: A `chat` Grouper reads its inputs through the request, never through the
#: reader's helper calls, so every one of its groupings reports no reads.
NO_READS = {"entries": [], "omitted_reads": 0, "omitted_errors": 0}


def _grouping_script(name: str) -> list[Any]:
    """The provider's answers to one grouping.

    `garbage` and `empty` exhaust both repairs with no group, so no failing
    observation is covered: a full (`all`) `invalid_output` fallback.
    `partial` covers one observation of several: an `uncovered` fallback.
    `admitted` groups every failing row. `outage` is a transient 503 on all
    three attempts: a Grouper provider suspension.
    """
    if name == "garbage":
        return [GARBAGE for _ in range(3)]
    if name == "empty":
        return [grouping_answer() for _ in range(3)]
    if name == "partial":
        return [group_everything(limit=1) for _ in range(3)]
    if name == "admitted":
        return [group_everything()]
    assert name == "outage"
    return [server_error(PROVIDER_TEXT) for _ in range(3)]


def _chain_client(monkeypatch, *groupings: str) -> ScriptedClient:
    script = [item for name in groupings for item in _grouping_script(name)]
    return use_chat_grouper(monkeypatch, ScriptedClient(script))


async def _chain_run(tmp_path, monkeypatch, rounds: int, **options: Any):
    """A genuine no-finalist `refresh: 1` family-14 run of `rounds` rounds."""

    grouper = chat_grouper_section(**options.pop("grouper", {}))
    return await family14_run(
        tmp_path,
        monkeypatch,
        NO_FINALISTS,
        family14_settings={"grouper": grouper, "refresh": 1},
        campaign={"rounds": rounds},
        **options,
    )


def _fallbacks(run) -> list[tuple[int, Any]]:
    return [(p["round"], p["fallback"]) for p in payloads(run, "grouping_finished")]


def _caveat_rounds(snapshot) -> list[int]:
    from evolution.tree.campaign_caveats import grouping_caveats

    return [item["round"] for item in grouping_caveats(snapshot)]


def _assert_second_link(run, snapshot, *, number: int, reason: str) -> None:
    """Round `number`'s grouping is the second consecutive full fallback.

    It ends in exactly `grouping_failed{role_failed, reason, content}` and the
    `grouper_failing` suspension naming that record, with nothing after: its
    fallback result is never journaled or applied, it is no caveat source, and
    it closes no round.
    """
    records = _records(run / "campaign.jsonl")
    started = [r for r in records if r["event"] == "grouping_started"][-1]
    at = records.index(started)
    assert started["payload"]["round"] == number
    assert [r["event"] for r in records[at:]] == [
        "grouping_started",
        "grouping_failed",
        "campaign_suspended",
    ]
    terminal, suspended = records[at + 1], records[at + 2]
    assert terminal["operation_id"] == started["operation_id"]
    assert terminal["payload"] == {
        "round": number,
        "grouping_id": started["payload"]["grouping_id"],
        "code": "role_failed",
        "reason": reason,
        "error_class": "content",
        "reads": NO_READS,
    }
    assert suspended["payload"] == {
        "reason": "grouper_failing",
        "owner": "grouper",
        "evidence": {
            "evidence_kind": "stage_record",
            "seq": at + 2,
            "operation_id": started["operation_id"],
        },
    }
    # No second `grouping_finished` for the failing grouping, so no caveat;
    # every earlier link reported the same (empty) reads.
    finished = payloads(run, "grouping_finished")
    assert all(p["round"] < number for p in finished)
    assert [p["reads"] for p in finished] == [NO_READS] * (number - 1)
    assert number not in _caveat_rounds(snapshot)
    assert snapshot.rounds[number].grouping is None
    assert [p["round"] for p in payloads(run, "round_closed")] == list(range(1, number))
    assert snapshot.finished is None
    assert snapshot.grouper_chain == 0  # the suspension broke the chain
    # Task 8: the audit's own chain agrees that this is the second link.
    assert audit(run)["mismatches"] == []


async def test_two_full_fallbacks_in_a_row_suspend_grouper_failing(tmp_path, monkeypatch):
    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "garbage")

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)

    assert refusals == [] and client.script == []
    assert _fallbacks(run) == [(1, FULL_INVALID)]
    assert _caveat_rounds(snapshot) == [1]
    # A scope-`all` result ignores the model's groups: no description or
    # variant text, so no string the secret scan has not already passed in
    # this run's earlier records (the decision-10 order comment).
    (first,) = payloads(run, "grouping_finished")
    assert first["result"]["descriptions"] == first["result"]["variants"] == {}
    _assert_second_link(run, snapshot, number=2, reason="invalid_output")


async def test_an_empty_answer_is_a_full_fallback_that_extends_the_chain(tmp_path, monkeypatch):
    """An answer with `"groups": []` after both repairs records scope `all`
    (decision 9), so it is a link like any full fallback."""

    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "empty", "garbage")

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)

    assert refusals == [] and client.script == []
    (finished,) = payloads(run, "grouping_finished")
    assert finished["fallback"] == FULL_INVALID
    assert finished["calls"]["repairs"] == 2
    _assert_second_link(run, snapshot, number=2, reason="invalid_output")


async def test_a_partial_fallback_leaves_the_chain_standing(tmp_path, monkeypatch):
    """full, partial (`uncovered`), full: the partial neither breaks nor
    extends the chain, so the third grouping is the second link."""

    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "partial", "garbage")

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 3)

    assert refusals == [] and client.script == []
    assert _fallbacks(run) == [
        (1, FULL_INVALID),
        (2, {"kind": "singletons", "scope": "uncovered", "reason": "invalid_output"}),
    ]
    assert _caveat_rounds(snapshot) == [1, 2]
    _assert_second_link(run, snapshot, number=3, reason="invalid_output")


async def test_an_admitted_grouping_breaks_the_chain(tmp_path, monkeypatch):
    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "admitted", "garbage")

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 3)

    assert refusals == [] and client.script == []
    assert _fallbacks(run) == [(1, FULL_INVALID), (2, None), (3, FULL_INVALID)]
    assert _caveat_rounds(snapshot) == [1, 3]
    assert snapshot.grouper_chain == 1
    assert "grouping_failed" not in _events(run) and "campaign_suspended" not in _events(run)
    assert [p["round"] for p in payloads(run, "round_closed")] == [1, 2, 3]
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    assert audit(run)["mismatches"] == []


async def test_an_unrelated_suspension_breaks_the_chain(tmp_path, monkeypatch):
    """full; then a Grouper provider outage suspends round 2's grouping
    (`grouper_provider_failed`); the same command and run directory resume and
    regroup round 2, whose full fallback is the first link again: a caveat,
    not a suspension."""

    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "outage", "garbage")

    run, suspended = await _chain_run(tmp_path, monkeypatch, 2)

    assert suspended.finished is None and suspended.grouper_chain == 0
    (failed,) = payloads(run, "grouping_failed")
    assert (failed["round"], failed["code"], failed["reason"], failed["error_class"]) == (
        2,
        "role_failed",
        "provider_exhausted",
        "provider",
    )
    (suspension,) = payloads(run, "campaign_suspended")
    assert (suspension["reason"], suspension["owner"]) == ("grouper_provider_failed", "grouper")
    assert audit(run)["mismatches"] == []

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2)

    assert refusals == [] and client.script == []
    assert _fallbacks(run) == [(1, FULL_INVALID), (2, FULL_INVALID)]
    assert _caveat_rounds(snapshot) == [1, 2]
    assert snapshot.grouper_chain == 1
    events = _events(run)
    assert events.count("grouping_failed") == events.count("campaign_suspended") == 1
    assert events.index("campaign_suspended") < events.index(
        "grouping_finished", 1 + events.index("grouping_finished")
    )
    assert [p["round"] for p in payloads(run, "round_closed")] == [1, 2]
    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    assert audit(run)["mismatches"] == []


async def test_two_input_too_large_fallbacks_suspend_with_that_reason(tmp_path, monkeypatch):
    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch)
    (tmp_path / "grouper-prompt.md").write_text(OVERSIZED_PROMPT)
    oversized = {"system_prompt": "grouper-prompt.md", "max_input_chars": 8000}

    run, snapshot = await _chain_run(tmp_path, monkeypatch, 2, grouper=oversized)

    assert refusals == [] and client.requests == []
    assert _fallbacks(run) == [
        (1, {"kind": "singletons", "scope": "all", "reason": "input_too_large"})
    ]
    _assert_second_link(run, snapshot, number=2, reason="input_too_large")


async def test_an_authorized_identity_rebase_does_not_break_the_chain(tmp_path, monkeypatch):
    """Only an admitted fallback-free grouping or a suspension resets the chain.
    Round 1 falls back in full,
    the process stops between rounds, and the operator authorizes a Grouper
    change (`max_output_tokens`) with `rebase`; the resumed round 2's full
    fallback is still the second link."""

    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash

    refusals = _spy_refusals(monkeypatch)
    client = _chain_client(monkeypatch, "garbage", "garbage")
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await _chain_run(tmp_path, monkeypatch, 2)
    run = tmp_path / "run"
    assert _fallbacks(run) == [(1, FULL_INVALID)]

    changed = {"max_output_tokens": 4096}
    with pytest.raises(EvolveRefused) as refused:
        await _chain_run(tmp_path, monkeypatch, 2, grouper=changed)
    assert refused.value.code == "run_identity_changed"
    assert refused.value.effects == ("rebase",)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "a smaller Grouper answer budget",
        now="2026-10-12T00:00:00Z",
    )

    run, snapshot = await _chain_run(
        tmp_path, monkeypatch, 2, grouper=changed, authorizations=[decision]
    )

    assert refusals == [] and client.script == []
    (rebased,) = payloads(run, "identity_rebased")
    # The authorized change is the Grouper's: only its adaptation slot moved.
    assert rebased["changed_fields"] == ["adaptation"]
    created = _records(run / "campaign.jsonl")[0]["payload"]["adaptation_payload"]
    assert rebased["adaptation_payload"]["grouping"] != created["grouping"]
    assert {**rebased["adaptation_payload"], "grouping": None} == {**created, "grouping": None}
    events = _events(run)
    assert events.index("grouping_finished") < events.index("identity_rebased")
    assert events.index("identity_rebased") < len(events) - 3
    assert "campaign_suspended" not in events[: len(events) - 1]
    _assert_second_link(run, snapshot, number=2, reason="invalid_output")
