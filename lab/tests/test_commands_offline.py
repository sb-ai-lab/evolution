"""Offline reads as commands: same values as the shipped guides and ``lab guides/scenarios`` output."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution_lab import guides
from evolution_lab.commands import REGISTRY
from tests.test_service_cli import invoke


OFFLINE = frozenset({"context_preflight", "guides_list", "guide_read", "requirements", "scenarios_list", "scenario_show", "scenario_schema"})


def run(command, /, **kwargs):
    return REGISTRY[command].run(**kwargs)


def test_offline_commands_are_runners_without_a_service():
    for name in sorted(OFFLINE):
        command = REGISTRY[name]
        assert not command.needs_service and command.build is None and command.run is not None


def test_guides_list_equals_cli_json():
    code, out, _ = invoke(["guides", "list", "--json"], None)
    assert code == 0
    assert run("guides_list") == json.loads(out)


def test_requirements_and_scenarios_list_equal_cli_json():
    _, out, _ = invoke(["guides", "requirements"], None)
    assert run("requirements") == json.loads(out)
    _, out, _ = invoke(["scenarios", "list", "--json"], None)
    assert run("scenarios_list") == json.loads(out)


@pytest.mark.parametrize(("name", "file"), [
    ("../pitfalls", "SKILL.md"), ("pitfalls", "../../__init__.py"), ("pitfalls", "/etc/passwd"),
    ("pitfalls", "nothing.md"), ("nope", "SKILL.md"), ("scenarios/nope", "SKILL.md"), ("scenarios", "catalogue.json"),
])
def test_guide_read_refuses_escape(name, file):
    with pytest.raises(ValueError, match="unknown guide file"):
        run("guide_read", name=name, file=file)


def test_guide_read_refuses_an_absolute_file_inside_the_guide():
    absolute = str(guides.root() / "pitfalls" / "SKILL.md")
    with pytest.raises(ValueError, match="unknown guide file"):
        run("guide_read", name="pitfalls", file=absolute)


def test_guide_containment_refuses_a_guide_directory_that_leaves_the_root(tmp_path):
    from evolution_lab.commands.offline import _contained
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "real").mkdir(parents=True)
    outside.mkdir()
    (root / "real" / "SKILL.md").write_text("ok", encoding="utf-8")
    (outside / "SKILL.md").write_text("secret", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert _contained(root.resolve(), "real", "SKILL.md").read_text(encoding="utf-8") == "ok"
    with pytest.raises(ValueError, match="unknown guide file"):
        _contained(root.resolve(), "linked", "SKILL.md")


def test_guide_read_returns_shipped_text():
    expected = (guides.root() / "pitfalls" / "SKILL.md").read_text(encoding="utf-8")
    assert run("guide_read", name="pitfalls") == {"name": "pitfalls", "file": "SKILL.md", "text": expected}


def test_guide_read_accepts_every_listed_name():
    listed = run("guides_list")
    assert listed
    for entry in listed:
        text = (guides.root() / entry["path"]).read_text(encoding="utf-8")
        assert run("guide_read", name=entry["name"]) == {"name": entry["name"], "file": "SKILL.md", "text": text}


def test_guide_read_reads_a_scenario_skill_and_a_nested_file():
    scenario = guides.scenarios().descriptors[0]["id"]
    text = (guides.root() / "scenarios" / scenario / "SKILL.md").read_text(encoding="utf-8")
    assert run("guide_read", name=f"scenarios/{scenario}")["text"] == text
    assert run("guide_read", name=f"scenarios/{scenario}", file="scenario.json")["file"] == "scenario.json"


def test_scenario_show_equals_cli_json_and_unknown_raises():
    scenario = guides.scenarios().descriptors[0]["id"]
    _, out, _ = invoke(["scenarios", "show", scenario, "--json"], None)
    assert run("scenario_show", scenario=scenario) == json.loads(out)
    with pytest.raises(LookupError):
        run("scenario_show", scenario="nope")


def test_scenario_schema_returns_shipped_file():
    path = Path(guides.root()) / "scenarios" / "connect" / "schemas" / "spec-answers-1.json"
    assert run("scenario_schema", scenario="connect", schema="spec-answers/1") == path.read_text(encoding="utf-8")
    with pytest.raises(LookupError):
        run("scenario_schema", scenario="connect", schema="nope/1")
    with pytest.raises(LookupError):
        run("scenario_schema", scenario="nope", schema="spec-answers/1")


def test_context_preflight_missing_state_does_not_create_it_or_call_service(tmp_path):
    state = tmp_path / "absent"
    code, out, err = invoke(["preflight", str(state / "prepared" / ("a" * 64))], None)
    assert code == 1 and not out
    assert json.loads(err)["error"]["stage"] == "preflight"
    assert not state.exists()


def test_preflight_prepared_cli_matches_offline_command(tmp_path, monkeypatch):
    from test_context_preflight_inputs import prepared_chat

    state, digest, bundle = prepared_chat(tmp_path, monkeypatch)
    selection = state / "prepared" / digest
    lock = state / ".publish.lock"
    lock.unlink()
    code, out, err = invoke(["preflight", str(selection), "--json"], None)
    assert code == 0, err
    report = json.loads(out)
    assert report == run("context_preflight", state=state, prepared=digest).as_dict()
    assert report["bundle_digest"] == bundle
    first = next(row for row in report["rows"] if row["role"] == "editor" and row["scenario"] == "first")
    assert first["basis"] == "hypothetical" and first["budget"]["status"] == "unknown"
    assert any(metric["name"] == "serialized_bytes" and metric["measurement"]["lower"] > 0
               for metric in first["metrics"])
    code, table, err = invoke(["preflight", digest, "--state", str(state)], None)
    assert code == 0, err
    assert bundle in table and "serialized_bytes" in table and "unknown" in table
    assert not lock.exists()


def test_preflight_reports_observed_overflow_and_hypothetical_reserve_with_same_input(tmp_path, monkeypatch):
    from test_context_preflight_inputs import saved_chat_trace

    state, prepared, operation, trace = saved_chat_trace(tmp_path, monkeypatch)
    scenario = tmp_path / "scenario.json"
    model = {"model": "up-1", "upstream": "provider", "styles": ["modern"], "window_tokens": 131072,
             "reserve_tokens": 14384, "source": "operator route specification"}
    scenario.write_text(json.dumps({"schema": "lab-context-scenario/1", "roles": {"editor": model}}))
    argv = ["preflight", str(state / "prepared" / prepared), "--operation", operation, "--trace", str(trace),
            "--scenario", str(scenario)]
    code, output, error = invoke([*argv, "--json"], None)
    assert code == 0, error
    assert "PRIVATE" not in output
    assert invoke([*argv, "--json"], None)[1] == output
    rows = json.loads(output)["rows"]
    observed, alternative = [r for r in rows if r["role"] == "editor" and r["session"] is not None and r["budget"]]
    assert observed["budget"]["input_tokens"]["provenance"] == "provider_usage"
    assert observed["budget"]["status"] == "over_window"
    assert observed["budget"]["headroom"]["lower"] == -1990
    assert alternative["basis"] == "hypothetical"
    assert alternative["budget"]["headroom"]["lower"] == 10
    code, table, error = invoke(argv, None)
    assert code == 0, error
    assert "-1990 tokens" in table and "10 tokens" in table
