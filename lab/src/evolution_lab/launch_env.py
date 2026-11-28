"""Host process settings added only at launch, outside prepared identities."""
from __future__ import annotations

import os
from collections.abc import Mapping

from .durability import DURABILITY_ENV, effective


def with_launch_values(env: Mapping[str, str]) -> dict[str, str]:
    values = dict(env)
    values[DURABILITY_ENV] = effective()
    if "TMPDIR" not in values and "TMPDIR" in os.environ:
        values["TMPDIR"] = os.environ["TMPDIR"]
    return values
