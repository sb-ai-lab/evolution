"""Pure input-secret transformation shared by execution and role projections."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"


def redact_run_secrets(value: Any, secrets: Sequence[str]) -> Any:
    """`value` as plain JSON with every run secret replaced by `[REDACTED]`.

    The verdict notes, `public_feedback` and `diagnostics` a scorer prints
    reach the Reflector and the Grouper, and through them a model provider.
    A forwarded credential in them is replaced, in keys and values, as the
    CLI's output replaces it; the rest of the text stays for the diagnosis.
    A number whose JSON text holds a secret becomes `[REDACTED]` as a whole.
    """

    if isinstance(value, str):
        # Replace a containing credential before its shorter prefix; otherwise
        # the prefix replacement leaves the rest of the credential visible.
        for secret in sorted({s for s in secrets if s}, key=lambda s: (-len(s), s)):
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, Mapping):
        return {
            redact_run_secrets(str(key), secrets): redact_run_secrets(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_run_secrets(item, secrets) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A numeric credential is a secret too: compare its JSON text.
        text = json.dumps(value)
        if any(secret and secret in text for secret in secrets):
            return REDACTED
    return value
