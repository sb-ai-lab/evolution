import pytest

from evolution_lab.owner.models import (
    CleanupFacts, ExecutorIdentity, Failure, OperationStatus, ProcessFacts, Refusal, StartFacts,
    TargetRef,
)

D = "d" * 64


@pytest.fixture
def status_factory():
    def make(**kw):
        executor = ExecutorIdentity(environment_digest="0" * 64, lab_wheel_sha256="1" * 64,
            core_wheel_sha256="2" * 64, module_hashes={"m": "2" * 64}, kind="lab-campaign-worker/1")
        process, cleanup = ProcessFacts(True, 1, True, False), CleanupFacts("confirmed", True)
        start = StartFacts(1, "scope1", "gen1", TargetRef("t1", "gen1"), process, cleanup, None, "unknown")
        return OperationStatus("op1", "gen1", D, TargetRef("t1", "gen1"), "finished", process, cleanup,
                               None, None, "present", executor, starts=(start,), **kw)
    return make


def test_core_refused_has_a_fixed_message():
    assert Failure.make("core_refused", "running").message == "The core refused the campaign."


def test_refusal_round_trips_with_a_closed_key_set():
    payload = {"code": "corpus_inside_role_workspace", "path": None, "class": "hard", "instance_digest": None}
    assert Refusal.from_payload(payload).to_payload() == payload
    with pytest.raises(ValueError):
        Refusal.from_payload({**payload, "extra": 1})
    with pytest.raises(ValueError):
        Refusal.from_payload({**payload, "class": "soft"})
    with pytest.raises(ValueError):
        Refusal.from_payload({**payload, "code": ""})


def test_status_copies_refusal_into_the_last_start(status_factory):
    refusal = Refusal("journal_busy", None, "hard", None)
    status = status_factory(refusal=refusal)
    payload = status.to_payload()
    assert payload["schema"] == "lab-operation-status/8"
    assert payload["refusal"] == refusal.to_payload()
    assert payload["starts"][-1]["refusal"] == refusal.to_payload()


def test_core_terminal_round_trips_with_a_closed_key_set(status_factory):
    from evolution_lab.owner.models import CoreTerminal
    payload = {"owner": "campaign_journal", "terminal": "persistence_failed", "code": "record_matches_secret",
               "hint": {"ru": "подсказка", "en": "hint"}}
    terminal = CoreTerminal.from_payload(payload)
    assert terminal.to_payload() == payload
    payload["hint"]["en"] = "changed"
    assert terminal.to_payload()["hint"]["en"] == "hint"
    status = status_factory(core_terminal=terminal).to_payload()
    assert status["core_terminal"] == status["starts"][-1]["core_terminal"] == terminal.to_payload()
    for bad in ({**terminal.to_payload(), "extra": 1}, {**terminal.to_payload(), "owner": None},
                {**terminal.to_payload(), "hint": {"ru": "x"}}):
        with pytest.raises(ValueError):
            CoreTerminal.from_payload(bad)
