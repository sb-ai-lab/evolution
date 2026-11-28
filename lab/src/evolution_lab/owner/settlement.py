"""Worker settlement evidence from the public core target call ledger (ADR-0008)."""
from __future__ import annotations

from .receipts import ReceiptError, read_json, validate_id

CAMPAIGN_SETTLEMENT_SCHEMA = "lab-campaign-settlement/2"
EVALUATION_SETTLEMENT_SCHEMA = "lab-evaluation-settlement/2"
_COUNTS = ("admitted", "completed", "not_sent", "uncertain", "pending", "in_flight",
           "refused", "bound_transports")
_FLAGS = ("admission_closed", "uncertain_any", "quiescent")
_LEDGER_KEYS = frozenset(("namespace", *_FLAGS, *_COUNTS))
_COMMON = frozenset(("schema", "scope_id", "owner_generation", "operation_id", "run_id",
                     "normal_return", "ledger"))


def ledger_payload(settlement) -> dict:
    """Project a core `evolution.tree.TargetCallSettlement` to a closed evidence dict."""
    return {"namespace": settlement.namespace,
            **{name: getattr(settlement, name) for name in _FLAGS},
            **{name: getattr(settlement, name) for name in _COUNTS}}


def _validate_namespace(namespace):
    # Core namespaces are `c-...` run ids or `conf-<uuid>`: not the lab's 32-hex ids
    # (validate_id), so this only checks shape, not identity.
    if namespace is None:
        return
    if not isinstance(namespace, str) or namespace == "":
        raise ReceiptError("invalid settlement ledger namespace")


def _ledger(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _LEDGER_KEYS:
        raise ReceiptError("invalid settlement ledger shape")
    _validate_namespace(value["namespace"])
    if any(type(value[name]) is not bool for name in _FLAGS):
        raise ReceiptError("invalid settlement ledger flag")
    if any(type(value[name]) is not int or value[name] < 0 for name in _COUNTS):
        raise ReceiptError("invalid settlement ledger count")
    if value["admitted"] != value["completed"] + value["not_sent"] + value["uncertain"] + value["pending"]:
        raise ReceiptError("settlement ledger classes do not sum to admitted")
    if value["in_flight"] > value["admitted"]:
        raise ReceiptError("settlement ledger in_flight exceeds admitted")
    if value["quiescent"] != (value["admission_closed"] and value["in_flight"] == 0 and value["pending"] == 0):
        raise ReceiptError("settlement ledger quiescent disagrees with counts")
    if value["uncertain"] > 0 and not value["uncertain_any"]:
        raise ReceiptError("settlement ledger uncertain_any disagrees with counts")
    return dict(value)


def _validate(value, keys, schema, booleans, *, operation_id, scope_id, owner_generation, run_id):
    try:
        for identifier in (operation_id, scope_id, owner_generation):
            validate_id(identifier)
        if not isinstance(value, dict) or set(value) != keys:
            raise ReceiptError("invalid settlement shape")
        if (value["schema"] != schema or value["operation_id"] != operation_id
                or value["scope_id"] != scope_id or value["owner_generation"] != owner_generation
                or value["run_id"] != run_id):
            raise ReceiptError("settlement identity mismatch")
        if any(type(value[name]) is not bool for name in booleans):
            raise ReceiptError("invalid settlement boolean")
        return {**value, "ledger": _ledger(value["ledger"])}
    except (KeyError, TypeError):
        raise ReceiptError("invalid settlement") from None


def validate_campaign_settlement(value, **ids):
    return _validate(value, _COMMON, CAMPAIGN_SETTLEMENT_SCHEMA, ("normal_return",), **ids)


def validate_evaluation_settlement(value, **ids):
    keys = _COMMON | {"materialize_started", "release_started"}
    return _validate(value, keys, EVALUATION_SETTLEMENT_SCHEMA,
                      ("normal_return", "materialize_started", "release_started"), **ids)


def _read(path, validator, **ids):
    return validator(read_json(path), **ids)


def read_campaign_settlement(path, **ids):
    return _read(path, validate_campaign_settlement, **ids)


def read_evaluation_settlement(path, **ids):
    return _read(path, validate_evaluation_settlement, **ids)


def settle_timeout(binding) -> float:
    timeouts = binding.timeouts
    return 3 * float(timeouts.connect_seconds) + float(timeouts.read_seconds) + 5.0


def positive_settlement(value: dict, *, run_id: str, kind: str) -> bool:
    ledger = value["ledger"]
    if not value["normal_return"] or ledger is None:
        return False
    if kind == "evaluation" and value["materialize_started"] != value["release_started"]:
        return False
    return (ledger["namespace"] == run_id and ledger["admission_closed"]
            and ledger["in_flight"] == 0 and ledger["pending"] == 0
            and ledger["uncertain"] == 0 and not ledger["uncertain_any"]
            and ledger["bound_transports"] >= 1)
