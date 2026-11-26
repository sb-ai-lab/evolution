"""Fault injection at the file-copy boundary of artifact staging."""

import os
import shutil

import pytest


def fail_file_copy(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def fail(*args, **kwargs):
        raise OSError(code, "injected file-copy failure")

    monkeypatch.setattr(shutil, "copy2", fail)
    if hasattr(os, "copy_file_range"):
        monkeypatch.setattr(os, "copy_file_range", fail)
