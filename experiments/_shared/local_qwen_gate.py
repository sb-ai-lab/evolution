"""Back-compat shim for the old module name.

This module was renamed to ``local_endpoint_gate.py`` on 2026-05-12 since
the implementation never was Qwen-specific. Re-exports everything for
unrelated experiment scripts that still import the old path.

New callers should import from ``_shared.local_endpoint_gate`` directly.
"""

from __future__ import annotations

from _shared.local_endpoint_gate import (  # noqa: F401
    _DEFAULT_BASE,
    _DEFAULT_PERMITS,
    _GATES,
    _LOCK,
    _Gate,
    gate_for,
    is_local_endpoint,
    is_local_qwen,
    local_endpoint_gate,
    qwen_gate,
)

__all__ = [
    "gate_for",
    "is_local_endpoint",
    "is_local_qwen",
    "local_endpoint_gate",
    "qwen_gate",
]
