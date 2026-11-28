"""The command layer core: path quoting parity with the CLI, the registry and response checking."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from evolution_lab.cli import parser
from evolution_lab.commands import _core
from evolution_lab.commands._core import Command, Effect, Request, check_response, command, segment_path, with_query
from tests.test_service_cli import HEALTH


@pytest.mark.parametrize("segments", [
    ("v1", "operations", "op 1", "measurements", "m@e2"),
    ("a%b", "c/d"),
    ("x", "y@z", "w w"),
    ("é", "100%"),
    ("", "a"),
    ("v1",),
])
def test_segment_path_matches_cli_quoting(segments):
    assert segment_path(*segments) == parser._path(*segments)


def test_with_query_drops_none_and_keeps_order():
    assert with_query("/p", b="x y", a=None, c="1/2") == parser._query("/p", b="x y", c="1/2")
    assert with_query("/p", a=None) == "/p"


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(_core, "REGISTRY", {})
    return _core.REGISTRY


def test_registry_rejects_duplicate_names(clean_registry):
    @command("x", effect=Effect.READ, schema=None)
    def first():
        return Request("GET", "/x", None)

    assert clean_registry["x"].build is first and clean_registry["x"].run is None
    with pytest.raises(ValueError, match="duplicate"):
        command("x", effect=Effect.READ, schema=None)(first)


def test_offline_command_registers_run(clean_registry):
    @command("o", effect=Effect.READ, schema=None, service=False)
    def offline():
        return 1

    assert clean_registry["o"].run is offline and clean_registry["o"].build is None
    assert not clean_registry["o"].needs_service


def test_command_needs_exactly_one_of_build_or_run():
    with pytest.raises(ValueError):
        Command("n", Effect.READ, True, None, None, None)
    with pytest.raises(ValueError):
        Command("b", Effect.READ, True, None, lambda: None, lambda: None)


def test_check_response_returns_value_unchanged():
    request = Request("GET", "/v1/health", "lab-service-health/2")
    assert check_response(request, HEALTH) is HEALTH


def test_check_response_without_schema_is_unvalidated():
    value = {"anything": 1}
    assert check_response(Request("GET", "/x", None), value) is value


def test_check_response_raises_on_wrong_schema():
    request = Request("GET", "/v1/health", "lab-service-health/2")
    with pytest.raises(ValueError, match="invalid public response"):
        check_response(request, {"schema": "lab-service-health/2"})


def test_importing_commands_does_not_load_the_cli_parser():
    program = ("import evolution_lab.commands, sys\n"
               "from evolution_lab.commands._core import Request, check_response\n"
               "import json\n"
               "check_response(Request('GET', '/v1/health', 'lab-service-health/2'), json.loads(sys.argv[1]))\n"
               "assert 'evolution_lab.cli.parser' not in sys.modules\n"
               "assert 'argparse' not in sys.modules\n")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-c", program, json.dumps(HEALTH)], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
