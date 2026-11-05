"""The redactor for records that leave the process: journals, artifacts, stdout.

`safe_record` strips hidden model reasoning and credential values from a value
before it is persisted or printed. The append log, Candidate editors and
`evo tree evolve` all pass their payloads through it.
"""

from __future__ import annotations

import json
import re
from typing import Any

_HIDDEN_REASONING = re.compile(
    r"<(think|analysis|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)", re.IGNORECASE | re.DOTALL
)
# A key-name rule for credential-shaped and hidden-reasoning keys. `token` is
# matched in the singular only: a provider's `usage` counts are `*_tokens`
# (`prompt_tokens`, `completion_tokens`, `reasoning_tokens`, ...) and are
# counters, not secrets; `access_token`, `api_token`, `token`
# still match.
_SECRET_KEY = re.compile(
    r"authorization|token(?!s)|api.?key|credential|password|secret|reasoning(?!_tokens)"
    r"|thought|oracle|private.?context",
    re.I,
)


def safe_record(value: Any, redactions: tuple[str, ...] = ()) -> Any:
    """Remove hidden reasoning and credential values from persisted records."""

    return _safe(value, redactions)


def _safe(value: Any, redactions: tuple[str, ...], key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _safe(item, redactions, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_safe(item, redactions) for item in value]
    if isinstance(value, tuple):
        return [_safe(item, redactions) for item in value]
    if not isinstance(value, str):
        return value
    parsed = _json_value(value)
    if parsed is not None:
        sanitized = _safe(parsed, redactions)
        if (
            sanitized == parsed
            and not _HIDDEN_REASONING.search(value)
            and not any(secret in value for secret in redactions)
        ):
            return value
        return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    result = _HIDDEN_REASONING.sub("[REDACTED]", value)
    for secret in redactions:
        result = result.replace(secret, "[REDACTED]")
    return result


def _json_value(value: str) -> Any | None:
    if not value.lstrip().startswith(("{", "[")):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
