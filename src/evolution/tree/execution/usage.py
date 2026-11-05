"""Usage for tree execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    prompt: int | None = None
    completion: int | None = None
    cached: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        for name in ("prompt", "completion", "cached", "total"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"usage {name} must be a nonnegative integer or None")

    def to_payload(self) -> dict[str, int | None]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "cached": self.cached,
            "total": self.total,
        }
