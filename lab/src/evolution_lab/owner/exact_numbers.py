"""Family-15 statistics as Core's exact rational plus a display value (ADR-0019 p.3).

``value`` is display only: rounded to 6 decimals for the reader, never fed back into any gate, average or
comparison. ``exact`` is Core's own ``rational-hex/1`` mapping, unchanged, so the exact number stays auditable.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from fractions import Fraction

from . import core_raw

_MAX_BINARY64 = Fraction(sys.float_info.max)


def statistical(value: Mapping | None) -> dict | None:
    """None -> None. Core's ``rational-hex/1`` mapping -> ``{"value": float | None, "exact": <the same mapping>}``.

    ``value`` is None when the exact number lies outside binary64 (``exact`` is then kept and shown as such).
    A malformed mapping raises ``ValueError`` (Core's ``SchemaError`` passes through ``core_raw``); callers
    treat that block as undescribed.
    """
    if value is None:
        return None
    q = core_raw.decode_rational(value)
    if abs(q) > _MAX_BINARY64:
        display = None
    else:
        display = float(round(q, 6))
        if display == 0.0:
            display = 0.0          # normalise -0.0
    return {"value": display, "exact": value}
