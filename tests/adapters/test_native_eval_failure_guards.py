from __future__ import annotations

from _shared.native_framework_runners import _all_llm_calls_failed


def test_all_llm_calls_failed_requires_at_least_one_call() -> None:
    assert _all_llm_calls_failed([]) is False


def test_all_llm_calls_failed_detects_no_successes() -> None:
    assert _all_llm_calls_failed([{"ok": False}, {"ok": False}]) is True


def test_all_llm_calls_failed_allows_any_success() -> None:
    assert _all_llm_calls_failed([{"ok": False}, {"ok": True}]) is False
    assert _all_llm_calls_failed([{"ok": False}, {}]) is False
