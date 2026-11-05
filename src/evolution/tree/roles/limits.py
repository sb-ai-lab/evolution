"""Adaptive role limits shared by briefs, admission and journal validation."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

ADAPTIVE_GROUP_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "groups": 8,
        "sentence_chars": 512,
        "evidence_ids": 64,
        "affected_cases": 32,
        "target_paths": 16,
        "document_ids": 16,
        "groups_bytes": 65_536,
    }
)


def adaptive_limits(group_text_chars: int | None) -> Mapping[str, int | None]:
    """`ADAPTIVE_GROUP_LIMITS` with `sentence_chars` set to the text limit given
    (`None`: no per-field limit), the one source every adaptive layer reads.

    The map is looked up at call time, not bound at import, so the tool schema,
    the backend bounds checks and the journal validator cannot hold different copies.
    """

    return MappingProxyType({**ADAPTIVE_GROUP_LIMITS, "sentence_chars": group_text_chars})
