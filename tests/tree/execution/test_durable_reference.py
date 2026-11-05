"""Shared reference identity, payload order and closed validation boundaries."""

import importlib
import json
import re
import subprocess
import sys

import pytest

from evolution.tree.execution.durable_reference import DurableReference
from evolution.tree.storage.envelope import SchemaError


@pytest.mark.parametrize(
    "module_name",
    [
        "evolution.tree.campaign",
        "evolution.tree.campaign_measurement",
        "evolution.tree.campaign_schema",
        "evolution.tree.family15_lifecycle",
        "evolution.tree.family15_schema",
        "evolution.tree.target.connection_probe",
    ],
)
def test_consumers_do_not_reexport_the_reference(module_name: str) -> None:
    assert not hasattr(importlib.import_module(module_name), "DurableReference")


def test_fresh_import_exposes_reference_names_only_at_their_owner() -> None:
    script = """
import importlib
import json

owner = importlib.import_module("evolution.tree.execution.durable_reference")
old_module = importlib.import_module("evolution.tree.campaign_schema")
moved_names = (
    "DurableReference", "EVIDENCE_KINDS", "_LOCATOR", "_SHA256",
    "_refuse", "_sha", "_validate_locator",
)
print(json.dumps({
    "owner": owner.DurableReference.__module__,
    "old_exports": [name for name in moved_names if hasattr(old_module, name)],
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {
        "owner": "evolution.tree.execution.durable_reference",
        "old_exports": [],
    }


@pytest.mark.parametrize("kind", ["journal_record", "journal_prefix", "ledger_record"])
def test_reference_preserves_positional_fields_and_payload_order(kind):
    reference = DurableReference(1, kind, "run", "events.jsonl#bytes=0:0", "b" * 64)
    payload = reference.to_payload()
    assert list(payload.items()) == [
        ("version", 1),
        ("kind", kind),
        ("run_id", "run"),
        ("locator", "events.jsonl#bytes=0:0"),
        ("content_digest", "b" * 64),
    ]
    assert DurableReference.from_payload(payload) == reference


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"version": True, "kind": "unknown"}, "reference version is 1"),
        ({"kind": "unknown", "run_id": "bad id"}, "reference kind is not in the registry"),
        ({"run_id": "bad id", "locator": None}, "invalid journal record"),
        ({"locator": None}, "locator is not a string"),
        ({"locator": ""}, "locator length"),
        ({"locator": "x" * 513}, "locator length"),
        ({"locator": "é#bytes=0:1"}, "locator is not printable ASCII"),
        ({"locator": "events.jsonl#bytes=00:1"}, "locator does not follow the byte-range grammar"),
        (
            {"locator": "../events.jsonl#bytes=2:1", "content_digest": "short"},
            "locator escapes the run",
        ),
        ({"extra": "field", "version": 2}, "reference payload keys"),
    ],
)
def test_reference_retains_invalid_input_rules_and_precedence(changes, rule):
    payload = dict(
        version=1,
        kind="journal_prefix",
        run_id="run",
        locator="events.jsonl#bytes=0:1",
        content_digest="b" * 64,
    )
    with pytest.raises(SchemaError, match=f"^{re.escape(rule)}$"):
        DurableReference.from_payload({**payload, **changes})
