"""Tiny ``ok``/``fail`` factories for preflight check lists.

Every experiment's preflight reimplemented these inline. The factories
return plain dicts for backward compatibility with callers that already
treat results as ``{"name", "passed", "detail"}``; new callers can use
``CheckResult`` from ``preflight_common`` instead.
"""

from __future__ import annotations

from collections.abc import Iterable


def ok(name: str, detail: str = "") -> dict:
    return {"name": name, "passed": True, "detail": detail}


def fail(name: str, detail: str) -> dict:
    return {"name": name, "passed": False, "detail": detail[:600]}


def all_passed(checks: Iterable[dict]) -> bool:
    return all(c.get("passed") for c in checks)


def summarize(checks: Iterable[dict]) -> tuple[int, int, list[str]]:
    """Return ``(passed_count, total_count, failed_names)``."""
    items = list(checks)
    failed = [c["name"] for c in items if not c.get("passed")]
    return (len(items) - len(failed), len(items), failed)


__all__ = ["all_passed", "fail", "ok", "summarize"]
