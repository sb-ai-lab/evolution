from __future__ import annotations

import os

import pytest

from evolution_lab import durability as state_io


@pytest.mark.parametrize("value", [None, ""])
def test_default_is_relaxed(monkeypatch, value):
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    if value is not None:
        monkeypatch.setenv("EVOLUTION_DURABILITY", value)
    assert state_io.effective() == "relaxed"


@pytest.mark.parametrize("value", ["Strict", " strict", "strict ", "off", "full"])
def test_unknown_value_refused(monkeypatch, value):
    monkeypatch.setenv("EVOLUTION_DURABILITY", value)
    with pytest.raises(state_io.DurabilityConfigError) as exc:
        state_io.check()
    assert str(exc.value) == f'EVOLUTION_DURABILITY must be "relaxed" or "strict" (got "{value}")'


def test_relaxed_makes_no_syscall(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOLUTION_DURABILITY", "relaxed")

    def forbidden(*args):
        pytest.fail("unexpected sync syscall")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", forbidden)
        patch.setattr(os, "open", forbidden)
        state_io.sync_file(-1)
        state_io.sync_directory(tmp_path)


def test_strict_calls_fsync(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOLUTION_DURABILITY", "strict")
    calls = []
    monkeypatch.setattr(os, "fsync", calls.append)
    state_io.sync_file(123)
    state_io.sync_directory(tmp_path)
    assert calls[0] == 123
    assert len(calls) == 2


