"""Import the ownership_mode fixture in modules that own process scopes."""
import sys

import pytest


@pytest.fixture(params=["subreaper", "group_tree_marker"])
def ownership_mode(request, monkeypatch):
    mode = request.param
    if sys.platform == "darwin":
        if mode == "subreaper":
            pytest.skip("Linux only: the subreaper ownership mode")
        monkeypatch.delenv("EVOLUTION_LAB_TEST_OWNERSHIP", raising=False)
    elif mode == "group_tree_marker":
        monkeypatch.setenv("EVOLUTION_LAB_TEST_OWNERSHIP", mode)
    else:
        monkeypatch.delenv("EVOLUTION_LAB_TEST_OWNERSHIP", raising=False)
    return mode
