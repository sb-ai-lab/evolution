"""W21 §7.3: `lab guides` works offline and mirrors the Python API."""
from __future__ import annotations

import io
import json

import pytest

from evolution_lab import guides
from evolution_lab.cli import main


def _run(argv, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    def no_client(**_):
        raise AssertionError("guides must not create a service client")
    code = main(argv, stdout=out, stderr=err, client_factory=no_client, **kwargs)
    return code, out.getvalue(), err.getvalue()


def test_requirements_json_equals_python_api():
    code, out, err = _run(["guides", "requirements", "--json"])
    assert (code, err) == (0, "")
    assert json.loads(out) == guides.requirements()


def test_list_json_and_text():
    code, out, _ = _run(["guides", "list", "--json"])
    assert code == 0
    assert json.loads(out) == [{"name": g.name, "description": g.description, "path": g.path}
                               for g in guides.list_guides()]
    code, out, _ = _run(["guides", "list"])
    assert code == 0 and len(out.splitlines()) == len(guides.list_guides())


def test_path_prints_root():
    code, out, _ = _run(["guides", "path"])
    assert (code, out) == (0, f"{guides.root()}\n")


@pytest.mark.parametrize("prefix", [["--socket", "/tmp/x.sock"], ["--timeout", "5"]])
def test_service_options_are_usage_errors(prefix):
    code, _, err = _run(prefix + ["guides", "path"])
    assert code == 2 and err.startswith("usage:")


def test_key_flags_are_usage_errors():
    code, _, err = _run(["--no-request-id", "guides", "path"])
    assert code == 2 and err.startswith("usage:")


def test_invalid_catalogue_exits_1(monkeypatch):
    def broken():
        raise ValueError("requirements catalogue: digest does not match the catalogue")
    monkeypatch.setattr(guides, "requirements", broken)
    code, out, err = _run(["guides", "requirements", "--json"])
    assert (code, out) == (1, "")
    assert json.loads(err)["error"]["code"] == "guides_invalid"


def test_list_oserror_exits_1(monkeypatch):
    def broken():
        raise OSError("unpacked installation missing")
    monkeypatch.setattr(guides, "list_guides", broken)
    code, out, err = _run(["guides", "list", "--json"])
    assert (code, out) == (1, "")
    assert json.loads(err)["error"]["code"] == "guides_invalid"


def test_scenarios_list_json_equals_python_api():
    code, out, err = _run(["scenarios", "list", "--json"])
    assert (code, err) == (0, "")
    assert json.loads(out) == guides.scenarios().list_payload()


def test_scenarios_list_text():
    code, out, _ = _run(["scenarios", "list"])
    assert code == 0
    first = out.splitlines()[0].split("\t")
    assert first[:3] == ["connect", "8", "Connect an agent"] and first[3].endswith("/connect/SKILL.md")


def test_scenarios_show_json_and_text():
    code, out, _ = _run(["scenarios", "show", "evolve", "--json"])
    assert code == 0 and json.loads(out) == guides.scenarios().entry_payload("evolve")
    code, out, _ = _run(["scenarios", "show", "evolve"])
    assert code == 0
    assert "step run operation human_only requires=prepare" in out.splitlines()


def test_scenarios_path_prints_the_catalogue_directory():
    code, out, _ = _run(["scenarios", "path"])
    assert code == 0 and out.strip() == str(guides.root() / "scenarios")


def test_unknown_scenario_exits_1():
    code, out, err = _run(["scenarios", "show", "nope"])
    assert code == 1 and out == ""
    assert json.loads(err)["error"]["code"] == "not_found"


def test_invalid_scenario_catalogue_exits_1(monkeypatch):
    def broken():
        raise ValueError("scenario catalogue: broken")
    monkeypatch.setattr(guides, "scenarios", broken)
    code, _, err = _run(["scenarios", "list"])
    assert code == 1 and json.loads(err)["error"]["code"] == "guides_invalid"


@pytest.mark.parametrize("prefix", [["--socket", "/tmp/s.sock"], ["--timeout", "3"]])
def test_scenarios_refuse_service_options(prefix):
    code, _, err = _run([*prefix, "scenarios", "list"])
    assert code == 2 and "usage" in err


def test_scenarios_schema_prints_the_file_as_is(tmp_path, monkeypatch):
    from scenario_fixtures import write_catalogue
    from evolution_lab.guides._scenarios import read_catalogue
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))
    monkeypatch.setattr(guides, "scenarios", lambda: catalogue)
    code, out, err = _run(["scenarios", "schema", "restructure", "behavior-map/1"])
    assert (code, err) == (0, "")
    assert out == (tmp_path / "c" / "restructure" / "schemas" / "behavior-map-1.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", [["scenarios", "schema", "restructure", "other/1"],
                                  ["scenarios", "schema", "nope", "behavior-map/1"]])
def test_scenarios_schema_not_found_exits_1(tmp_path, monkeypatch, argv):
    from scenario_fixtures import write_catalogue
    from evolution_lab.guides._scenarios import read_catalogue
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))
    monkeypatch.setattr(guides, "scenarios", lambda: catalogue)
    code, out, err = _run(argv)
    assert code == 1 and out == "" and json.loads(err)["error"]["code"] == "not_found"


def test_scenarios_show_marks_optional_steps():
    code, out, _ = _run(["scenarios", "show", "evolve"])
    assert code == 0 and "step result operation optional requires=run" in out.splitlines()
