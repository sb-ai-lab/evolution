"""Tests for the shared async retry helper."""

from __future__ import annotations

import asyncio

import pytest

from evolution.llm.retry import retry_async


class _Transient(Exception):
    pass


class _Fatal(Exception):
    pass


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, _Transient)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Patch asyncio.sleep so retry tests run instantly."""

    async def _fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


async def test_returns_first_success():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    result = await retry_async(factory, classify=_is_transient, max_attempts=3)
    assert result == "ok"
    assert calls["n"] == 1


async def test_retries_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Transient(f"fail {calls['n']}")
        return "ok"

    result = await retry_async(factory, classify=_is_transient, max_attempts=5)
    assert result == "ok"
    assert calls["n"] == 3


async def test_fatal_does_not_retry():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _Fatal("nope")

    with pytest.raises(_Fatal):
        await retry_async(factory, classify=_is_transient, max_attempts=5)
    assert calls["n"] == 1


async def test_exhausted_retries_raises_last_error():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _Transient(f"flap {calls['n']}")

    with pytest.raises(_Transient, match="flap 3"):
        await retry_async(factory, classify=_is_transient, max_attempts=3)
    assert calls["n"] == 3
