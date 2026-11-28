"""Closed Owner reflector-probe evidence, independent of Core projections."""
from collections.abc import Mapping


def validate_probe(block, state, reason):
    if state == "unavailable":
        if block is None and reason == "unreadable":
            return
        raise ValueError("invalid reflector probe availability")
    if state != "present" or reason is not None or not isinstance(block, Mapping):
        raise ValueError("invalid reflector probe availability")
    if set(block) != {"launched_by", "state", "restarts", "last_exit"}:
        raise ValueError("invalid reflector probe fields")
    if type(block["restarts"]) is not int or block["restarts"] < 0:
        raise ValueError("invalid reflector probe restarts")
    if block["last_exit"] is not None and type(block["last_exit"]) is not int:
        raise ValueError("invalid reflector probe exit")
    if not isinstance(block["state"], str) or not (block["launched_by"] is None or isinstance(block["launched_by"], str)):
        raise ValueError("invalid reflector probe state")
    if block["state"] == "not_declared":
        if dict(block) != {"launched_by": None, "state": "not_declared", "restarts": 0, "last_exit": None}:
            raise ValueError("inconsistent undeclared reflector probe")
    elif block["state"] not in {"running", "stopped", "exhausted"} or block["launched_by"] not in {"lab", "operator"}:
        raise ValueError("invalid reflector probe state")


def probe_fields(block=None):
    """Missing or malformed Owner evidence is unavailable, never an inferred absence."""
    try:
        validate_probe(block, "present", None)
    except (ValueError, TypeError):
        return {"reflector_probe": None, "reflector_probe_state": "unavailable", "reflector_probe_reason": "unreadable"}
    return {"reflector_probe": dict(block), "reflector_probe_state": "present", "reflector_probe_reason": None}


def probe_lines(value):
    state = value.get("reflector_probe_state")
    if state == "unavailable":
        return ["reflector probe: unavailable (unreadable)"]
    block = value.get("reflector_probe")
    if not block or block["state"] == "not_declared":
        return []
    note = " (Owner participation record)" if block["launched_by"] == "operator" else ""
    return [f"reflector probe: launched_by={block['launched_by']} state={block['state']} restarts={block['restarts']}{note}"]
