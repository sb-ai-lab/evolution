"""Pinned role-input secret checks, independent of journal redaction."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from evolution.tree.execution.check_modes import HitSink
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.input_secrets import redact_run_secrets
from evolution.tree.execution.interaction import ScoreResult


def input_secret_value(
    value: Any,
    secrets: Sequence[str],
    *,
    pin: EvidencePin | None = None,
    hits: HitSink | None = None,
    location: str | None = None,
) -> Any:
    """Apply only the pinned input check; journal redaction is independent."""
    mode = "enforce" if pin is None else pin.input_modes["input_run_secret_redaction"]
    if mode == "off":
        return value
    redacted = redact_run_secrets(value, secrets)
    if mode == "shadow":
        if redacted != value and hits is not None:
            if location is None:
                hits.hit("input_run_secret_redaction", "redacted")
            else:
                scanned = value
                if location.startswith("trace/") and isinstance(value, str):
                    try:
                        scanned = json.loads(value)
                    except ValueError:
                        pass

                def collect(item: Any, path: str) -> None:
                    if isinstance(item, Mapping):
                        for key, child in item.items():
                            collect(str(key), f"{path}/{key}/key")
                            collect(child, f"{path}/{key}")
                    elif isinstance(item, (list, tuple)):
                        for index, child in enumerate(item):
                            collect(child, f"{path}/{index}")
                    elif redact_run_secrets(item, secrets) != item:
                        hits.hit("input_run_secret_redaction", "redacted", location=path)

                collect(scanned, location)
        return value
    return redacted


def criteria_carry_run_secret(
    result: ScoreResult,
    secrets: Sequence[str],
    *,
    pin: EvidencePin | None = None,
    hits: HitSink | None = None,
    location: str | None = None,
) -> bool:
    """Whether a verdict criterion id holds a run secret.

    A criterion id is grouped on and published to the Editor, and cannot be
    redacted without breaking its grammar, so such an output is unusable for
    the roles, the same way an unreadable output is.
    """

    mode = "enforce" if pin is None else pin.input_modes["input_run_secret_redaction"]
    if mode == "off":
        return False
    matched = any(
        secret and secret in verdict["criterion"]
        for verdict in result.verdicts
        for secret in secrets
    )
    if matched and mode == "shadow" and hits is not None:
        if location is None:
            hits.hit("input_run_secret_redaction", "redacted")
        else:
            for verdict in result.verdicts:
                if any(secret and secret in verdict["criterion"] for secret in secrets):
                    hits.hit(
                        "input_run_secret_redaction",
                        "redacted",
                        location=f"{location}/{verdict['criterion']}",
                    )
    return matched and mode == "enforce"
