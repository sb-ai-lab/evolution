"""``lab mcp``: the extra hint, socket resolution like the CLI, timeout and option handling."""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from evolution_lab.cli import main


def _run(argv, monkeypatch, tmp_path):
    monkeypatch.delenv("LAB_HOME", raising=False)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout=stdout, stderr=stderr, stdin=io.StringIO())
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def recorded(monkeypatch):
    import evolution_lab.mcp.server as server
    calls = []

    def fake(socket_path, *, timeout, **options):
        calls.append((socket_path, timeout, options))
        return 0
    monkeypatch.setattr(server, "serve_stdio", fake)
    return calls


def test_mcp_without_extra_exits_2_with_hint(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.delitem(sys.modules, "evolution_lab.mcp.server", raising=False)
    code, _, err = _run(["--socket", "/tmp/x.sock", "mcp"], monkeypatch, tmp_path)
    assert code == 2
    assert "evolution-lab[mcp]" in err


def test_mcp_other_import_error_propagates(monkeypatch, tmp_path):
    import evolution_lab.mcp.server as server
    monkeypatch.setattr(server, "serve_stdio", None)
    monkeypatch.delitem(sys.modules, "evolution_lab.mcp.server")
    # a module that fails on a non-mcp import must not get the extra hint
    import importlib.abc

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "evolution_lab.mcp.server":
                raise ImportError("boom", name="something_else")
    monkeypatch.setattr(sys, "meta_path", [Finder(), *sys.meta_path])
    with pytest.raises(ImportError):
        _run(["--socket", "/tmp/x.sock", "mcp"], monkeypatch, tmp_path)


def test_mcp_resolves_socket_like_the_cli(monkeypatch, tmp_path, recorded):
    from evolution_lab.projects.endpoint import resolve_socket
    assert _run(["--socket", "/tmp/x.sock", "mcp"], monkeypatch, tmp_path)[0] == 0
    assert _run(["mcp", "--socket", "/tmp/y.sock"], monkeypatch, tmp_path)[0] == 0
    home = tmp_path / "h"
    assert _run(["mcp", "--home", str(home)], monkeypatch, tmp_path)[0] == 0
    assert _run(["--home", str(home), "mcp"], monkeypatch, tmp_path)[0] == 0
    monkeypatch.setenv("LAB_HOME", str(home))
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main(["mcp"], stdout=stdout, stderr=stderr, stdin=io.StringIO()) == 0
    assert recorded[0][0] == "/tmp/x.sock"
    assert recorded[1][0] == "/tmp/y.sock"
    assert recorded[2][0] == str(resolve_socket(None, Path(home)))
    assert recorded[3][0] == recorded[2][0]
    assert recorded[4][0] == recorded[2][0]


def test_mcp_too_long_socket_is_a_usage_error(monkeypatch, tmp_path, recorded):
    code, _, err = _run(["mcp", "--home", "/tmp/" + "a" * 200], monkeypatch, tmp_path)
    assert code == 2
    assert recorded == []


def test_mcp_value_error_from_the_running_server_is_not_a_usage_error(monkeypatch, tmp_path):
    import evolution_lab.mcp.server as server

    def broken(socket_path, **options):
        raise ValueError("from the running server")
    monkeypatch.setattr(server, "serve_stdio", broken)
    code, _, err = _run(["--socket", "/tmp/x.sock", "mcp"], monkeypatch, tmp_path)
    assert code == 1
    assert not err.startswith("usage:")
    assert "client_error" in err


def test_mcp_default_timeout_is_60(monkeypatch, tmp_path, recorded):
    assert _run(["--socket", "/tmp/x.sock", "mcp"], monkeypatch, tmp_path)[0] == 0
    assert recorded[-1][1] == 60.0
    assert _run(["--socket", "/tmp/x.sock", "--timeout", "5", "mcp"], monkeypatch, tmp_path)[0] == 0
    assert recorded[-1][1] == 5
    assert _run(["mcp", "--timeout", "7"], monkeypatch, tmp_path)[0] == 0
    assert recorded[-1][1] == 7


def test_mcp_autonomy_and_approval_timeout_defaults(monkeypatch, tmp_path, recorded):
    assert _run(["mcp"], monkeypatch, tmp_path)[0] == 0
    assert recorded[-1][2] == {"autonomy": "ask", "approval_timeout": 600.0}


def test_mcp_passes_explicit_approval_options(monkeypatch, tmp_path, recorded):
    assert _run(["mcp", "--autonomy", "yolo", "--approval-timeout", "37.5"], monkeypatch, tmp_path)[0] == 0
    assert recorded[-1][2] == {"autonomy": "yolo", "approval_timeout": 37.5}


@pytest.mark.parametrize("options", [
    ["--autonomy", "ask-paid"], ["--approval-timeout", "0"], ["--approval-timeout", "inf"],
])
def test_mcp_rejects_invalid_approval_options(options, monkeypatch, tmp_path, recorded):
    assert _run(["mcp", *options], monkeypatch, tmp_path)[0] == 2
    assert recorded == []


@pytest.mark.parametrize("option", [["--request-id", "abc"], ["--generation", "g"], ["--no-request-id"]])
def test_mcp_rejects_key_options(option, monkeypatch, tmp_path, recorded):
    code, _, err = _run(["--socket", "/tmp/x.sock", *option, "mcp"], monkeypatch, tmp_path)
    assert code == 2
    assert recorded == []


def test_cli_help_lists_mcp(monkeypatch, tmp_path):
    code, out, err = _run(["--help"], monkeypatch, tmp_path)
    assert "mcp" in (out + err)
    assert "Service and maintenance" in out + err


def test_cli_module_does_not_import_mcp():
    subprocess.run([sys.executable, "-c",
                    "import evolution_lab.cli, sys; assert 'mcp' not in sys.modules; "
                    "assert 'evolution_lab.mcp.server' not in sys.modules"], check=True)


@pytest.mark.parametrize("variable, value", [("LAB_HOME", "relative-home"), ("XDG_DATA_HOME", "relative-data")])
def test_mcp_refuses_a_relative_socket_like_the_cli(variable, value, monkeypatch, tmp_path, recorded):
    """A relative home would pick the socket from the working directory (PDR-0061): usage error, exit 2."""
    monkeypatch.delenv("LAB_HOME", raising=False)
    monkeypatch.setenv(variable, value)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(["mcp"], stdout=stdout, stderr=stderr, stdin=io.StringIO())
    assert code == 2
    assert stderr.getvalue() == "usage: socket path must be absolute\n"
    assert stdout.getvalue() == ""
    assert recorded == []
