import asyncio
import json
import pytest

from evolution_lab.owner.receipts import ReceiptError
from evolution_lab.owner.settlement import (
    ledger_payload, positive_settlement, read_campaign_settlement,
    validate_campaign_settlement, validate_evaluation_settlement,
)

IDS = dict(operation_id="a" * 32, scope_id="b" * 32, owner_generation="c" * 32, run_id="c-run1")


def ledger(**over):
    value = dict(namespace="c-run1", admission_closed=True, uncertain_any=False, quiescent=True,
                 admitted=3, completed=3, not_sent=0, uncertain=0, pending=0, in_flight=0,
                 refused=0, bound_transports=1)
    value.update(over)
    return value


def campaign(**over):
    value = {"schema": "lab-campaign-settlement/2", "scope_id": "b" * 32, "owner_generation": "c" * 32,
             "operation_id": "a" * 32, "run_id": "c-run1", "normal_return": True, "ledger": ledger()}
    value.update(over)
    return value


def test_positive_campaign_settlement_round_trips(tmp_path):
    path = tmp_path / "settlement.json"
    path.write_text(json.dumps(campaign()))
    value = read_campaign_settlement(path, **IDS)
    assert positive_settlement(value, run_id="c-run1", kind="campaign") is True


@pytest.mark.parametrize("over", [
    {"quiescent": True, "in_flight": 1},
    {"quiescent": True, "pending": 1, "admitted": 4},
    {"quiescent": True, "admission_closed": False},
    {"uncertain": 1, "completed": 2, "uncertain_any": False},
    {"admitted": 5},
    {"in_flight": 4, "quiescent": False},
    {"completed": True},
    {"refused": -1},
    {"namespace": 7},
])
def test_invalid_ledger_payload_is_refused(over):
    with pytest.raises(ReceiptError):
        validate_campaign_settlement(campaign(ledger=ledger(**over)), **IDS)


def test_extra_or_missing_ledger_key_is_refused():
    extra = ledger(); extra["coverage"] = "bound_transports"
    missing = ledger(); del missing["admitted"]
    for value in (extra, missing):
        with pytest.raises(ReceiptError):
            validate_campaign_settlement(campaign(ledger=value), **IDS)


@pytest.mark.parametrize("value", [
    campaign(normal_return=False),
    campaign(ledger=None),
    campaign(ledger=ledger(namespace="other")),
    campaign(ledger=ledger(admission_closed=False, quiescent=False)),
    campaign(ledger=ledger(in_flight=1, quiescent=False)),
    campaign(ledger=ledger(pending=1, admitted=4, quiescent=False)),
    campaign(ledger=ledger(uncertain=1, admitted=4, uncertain_any=True)),
    campaign(ledger=ledger(uncertain_any=True)),
    campaign(ledger=ledger(bound_transports=0)),
])
def test_each_negative_branch_forbids_reuse(value):
    checked = validate_campaign_settlement(value, **IDS)
    assert positive_settlement(checked, run_id="c-run1", kind="campaign") is False


def test_evaluation_requires_matching_materialize_and_release():
    base = campaign(schema="lab-evaluation-settlement/2", materialize_started=True, release_started=True)
    assert positive_settlement(validate_evaluation_settlement(base, **IDS), run_id="c-run1", kind="evaluation")
    unmatched = dict(base, release_started=False)
    assert not positive_settlement(validate_evaluation_settlement(unmatched, **IDS), run_id="c-run1", kind="evaluation")


def test_schema_one_is_refused():
    with pytest.raises(ReceiptError):
        validate_campaign_settlement(campaign(schema="lab-campaign-settlement/1"), **IDS)


def test_namespace_none_is_valid_but_not_positive():
    value = validate_campaign_settlement(campaign(ledger=ledger(namespace=None)), **IDS)
    assert positive_settlement(value, run_id="c-run1", kind="campaign") is False


@pytest.mark.parametrize("bad_namespace", [7, "", 1.5, [], {}])
def test_non_string_or_empty_namespace_is_refused(bad_namespace):
    with pytest.raises(ReceiptError):
        validate_campaign_settlement(campaign(ledger=ledger(namespace=bad_namespace)), **IDS)


def test_ledger_payload_from_real_core_ledger_is_not_positive():
    from evolution.tree import TargetCallLedger

    async def scenario():
        core_ledger = TargetCallLedger()
        core_ledger.close()
        return await core_ledger.settle(1)

    snapshot = asyncio.run(scenario())
    value = validate_campaign_settlement(campaign(ledger=ledger_payload(snapshot)), **IDS)
    assert value["ledger"]["admitted"] == 0
    assert value["ledger"]["quiescent"] is True
    assert value["ledger"]["bound_transports"] == 0
    assert positive_settlement(value, run_id="c-run1", kind="campaign") is False
