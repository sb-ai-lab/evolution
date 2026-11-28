"""Task 3: the demo project — generator, registry lifecycle, session (spec §3.12)."""
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from evolution_lab import guides
from evolution_lab.guides import answers
from evolution_lab.projects.demo import FAULT_ENV, IMPLEMENTATION, MATERIALS_ROOT, fault_flag, write_demo
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.package import read_package_entry, validate_form
from evolution_lab.projects.registry import Projects

DIGEST = "e" * 64


def _demo(tmp_path) -> Path:
    destination = tmp_path / "clones" / "acq"
    destination.parent.mkdir(parents=True)
    write_demo(destination, final=destination, project_id="acq", environment_digest=DIGEST,
               executable=sys.executable, port=18765)
    return destination


def test_fault_flag_points_at_the_final_working_copy(tmp_path):
    staging, final = tmp_path / ".staging-acq-1", tmp_path / "acq"
    write_demo(staging, final=final, project_id="acq", environment_digest=DIGEST,
               executable=sys.executable, port=18765)
    assert json.loads((staging / "launch.json").read_text())["env"] == {FAULT_ENV: str(tmp_path / "acq.fault")}


def test_layout_and_launch(tmp_path):
    wc = _demo(tmp_path)
    assert (wc / "agent" / "demo_agent.py").is_file() and (wc / "lab" / "SPEC.md").is_file()
    assert not (wc / ".git").exists()
    launch = json.loads((wc / "launch.json").read_text())
    assert launch["schema"] == "lab-target-launch/3" and launch["environment_ref"] == DIGEST
    assert launch["reflector_probe"] is None
    assert launch["turn"] == {"empty_answer": "target_failed"}
    assert launch["agent"] == {"module": "demo_agent", "attribute": "build", "import_root": "agent"}
    assert launch["policy"] == "lab/manifest.json" and launch["program_paths"] == ["agent"]
    assert launch["env"] == {FAULT_ENV: str(fault_flag(wc))}
    assert launch["conformance"] == {"kind": "core-session-public/1"}
    assert launch["endpoint"]["base_url"] == "http://127.0.0.1:18765"


def test_demo_writes_valid_spec_answers(tmp_path):
    wc = _demo(tmp_path)
    document = json.loads((wc / "lab" / "spec.answers.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(answers.spec_answers_schema(guides.requirements())).iter_errors(document)) == []
    assert document["path"] == "campaign" and document["fit"]["implementation"] == "demo"
    assert document["catalogue"] == {"version": 13, "digest": guides.requirements()["digest"]}
    assert document["fit"]["verdict"] == "as_is"
    assert document["fit"]["reasons"] == []
    for item_id, item in document["items"].items():
        assert item["status"] == "known", f"item {item_id} should be known"


def test_manifest_roles_run_with_the_service_interpreter(tmp_path):
    manifest = json.loads((_demo(tmp_path) / "lab" / "manifest.json").read_text())
    assert manifest["target"]["base_url"] == "http://127.0.0.1:18765"
    for role in ("scorer", "editor", "reflector"):
        assert manifest[role]["argv"][0] == sys.executable


def test_package_entry_is_valid_and_covers_the_agent(tmp_path):
    wc = _demo(tmp_path)
    entry = json.loads((wc / "lab" / "package.json").read_text())
    assert entry["environments"] == [{"kind": "service"}]
    assert entry["roots"] == [{"alias": "project", "source_path": ".."}]
    assert entry["source"] == {"kind": "campaign", "manifest_path": "manifest.json", "launch_path": "../launch.json"}
    assert entry["env"] == {FAULT_ENV: str(fault_flag(wc))}
    # the same check `package.valid` runs (form only, no environment capture)
    validate_form(read_package_entry(wc / MATERIALS_ROOT / IMPLEMENTATION["package_entry"]), place=wc / "agent")


def test_fault_flag_is_a_sibling_outside_the_working_copy(tmp_path):
    wc = _demo(tmp_path)
    assert fault_flag(wc) == wc.parent / "acq.fault" and not fault_flag(wc).is_relative_to(wc)


def test_evaluation_request_names_its_budget(tmp_path):
    body = json.loads((_demo(tmp_path) / "lab" / "evaluation-request.json").read_text())
    assert body["kind"] == "evaluation" and body["bundle_digest"] is None
    assert body["max_failed_turns"] == 1 and body["call_retries"] == "off"


def test_demo_campaign_manifest_inherits_retry_table(tmp_path):
    manifest = json.loads((_demo(tmp_path) / "lab" / "manifest.json").read_text())
    campaign = manifest["campaign"]
    assert campaign["sample_retries"] == 3
    assert campaign["call_retries"] == {"max_attempts": 3, "turn_max_attempts": 2}
    assert campaign["budget"]["max_failed_turns"] == campaign["budget"]["max_turns"]
    assert all(value == {"max_count": 20, "max_rate": "1"} for value in campaign["indeterminate"].values())
    assert manifest["reflector"]["max_consecutive_failures"] == 5


def _load_demo_agent(tmp_path):
    """The generated demo agent, built over a view whose skill's first line is ``IMPROVED: …``, and its fault flag."""
    import asyncio, importlib.util, types
    wc = _demo(tmp_path)
    spec = importlib.util.spec_from_file_location("demo_agent", wc / "agent" / "demo_agent.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root = tmp_path / "view"; (root / "skills").mkdir(parents=True)
    (root / "skills" / "SKILL.md").write_text("IMPROVED: reference skill\n")
    flag = fault_flag(wc)
    context = types.SimpleNamespace(env={FAULT_ENV: str(flag)}, secrets={})
    return asyncio.run(module.build(context)(types.SimpleNamespace(root=root))), flag


def test_agent_answers_first_skill_line_and_fails_while_flag_exists(tmp_path):
    import asyncio, types
    agent, flag = _load_demo_agent(tmp_path)
    message = types.SimpleNamespace(content="q1")
    state = asyncio.run(agent.open({}, session_id="s", seed=0))
    ok = asyncio.run(agent.turn(message, state, session_id="s", seed=0))
    assert (ok.outcome, ok.text) == ("ok", "IMPROVED: reference skill | q1")
    assert (ok.reason.category, ok.reason.code) == ("agent_outcome", "answered")
    flag.write_text("")
    failed = asyncio.run(agent.turn(message, state, session_id="s", seed=0))
    assert (failed.outcome, failed.text, failed.events, failed.usage, failed.reason) == (
        "target_failed", "", (), None, None)


def test_demo_agent_empty_flag_answers_ok_with_empty_text(tmp_path):
    """The agent emits empty ok; the default Lab turn layer classifies it as target_failed.
    Direct agent calls here deliberately precede that layer; other flag contents fail directly."""
    import asyncio, types
    agent, flag = _load_demo_agent(tmp_path)
    message = types.SimpleNamespace(content="q1")
    state = asyncio.run(agent.open({}, session_id="s", seed=0))
    flag.write_text("empty\n")
    turn = asyncio.run(agent.turn(message, state, session_id="s", seed=0))
    assert (turn.outcome, turn.text) == ("ok", "")
    flag.write_text("")
    turn = asyncio.run(agent.turn(message, state, session_id="s", seed=0))
    assert turn.outcome == "target_failed"


def test_demo_agent_declares_agent_outcome_step_and_usage(tmp_path):
    import asyncio
    from evolution.tree import Message
    agent, _ = _load_demo_agent(tmp_path)
    turn = asyncio.run(agent.turn(Message("позовите Оператора"), {"turns": 0}, session_id="s", seed=None))
    assert turn.outcome == "ok" and turn.state == {"turns": 1}
    assert turn.reason.category == "agent_outcome" and turn.reason.code == "handoff"
    assert turn.events == ({"type": "tool_call", "name": "read_skill"},)
    content, answer = "позовите Оператора", "IMPROVED: reference skill | позовите Оператора"
    assert turn.text == answer
    assert (turn.usage.prompt, turn.usage.completion, turn.usage.cached, turn.usage.total) == (
        len(content), len(answer), 0, len(content) + len(answer))


# --- registry lifecycle -----------------------------------------------------------------

def _home(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path / "home"


def test_create_demo_registers_a_ready_project(tmp_path):
    with Projects.open(_home(tmp_path)) as projects:
        project = projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
    assert project.state == "ready" and project.materials_root == "lab"
    assert project.working_copy.to_payload() == {"path": str(tmp_path / "home" / "clones" / "acq"),
                                                 "created_by_lab": True, "git": False, "source": None}
    (impl,) = project.implementations
    assert (impl.id, impl.place, impl.package_entry, impl.origin) == ("demo", "agent", "package.json",
                                                                      {"kind": "connected"})
    assert not list((tmp_path / "home" / "clones").glob(".staging-*"))


def test_create_demo_generates_an_id(tmp_path):
    with Projects.open(_home(tmp_path)) as projects:
        project = projects.create_demo(id=None, environment_digest=DIGEST, executable=sys.executable, port=18765)
    assert (tmp_path / "home" / "clones" / project.id / "launch.json").is_file()


def test_demo_id_taken_writes_nothing(tmp_path):
    with Projects.open(_home(tmp_path)) as projects:
        projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
        before = sorted(p.name for p in (tmp_path / "home" / "clones").iterdir())
        with pytest.raises(ProjectError) as error:
            projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
    assert error.value.code == "id_taken"
    assert sorted(p.name for p in (tmp_path / "home" / "clones").iterdir()) == before


def test_demo_generation_failure_marks_failed_and_cleans_staging(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("evolution_lab.projects.demo.write_demo", broken)
    with Projects.open(_home(tmp_path)) as projects:
        with pytest.raises(ProjectError) as error:
            projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
        assert error.value.code == "working_copy_unavailable"
        assert projects.get("acq").state == "failed"
    assert not list((tmp_path / "home" / "clones").glob(".staging-*"))


def test_demo_generation_bug_propagates_but_still_marks_failed(tmp_path, monkeypatch):
    """A programming bug (`TypeError`, ...) from `write_demo` must not be laundered into a
    `ProjectError("working_copy_unavailable")`: the record still fails and staging is still
    cleaned up, but the real exception propagates (fix round 1, Important finding)."""
    def broken(*args, **kwargs):
        raise TypeError("write_demo() got an unexpected keyword argument")
    monkeypatch.setattr("evolution_lab.projects.demo.write_demo", broken)
    with Projects.open(_home(tmp_path)) as projects:
        with pytest.raises(TypeError):
            projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
        assert projects.get("acq").state == "failed"
    assert not list((tmp_path / "home" / "clones").glob(".staging-*"))


def test_reconcile_keeps_fault_flag_and_readies_demo(tmp_path):
    home = _home(tmp_path)
    with Projects.open(home) as projects:
        project = projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
        # simulate a crash after the rename, before the ready record
        projects.store.put_project(replace(project, state="creating"))
    fault_flag(Path(project.working_copy.path)).write_text("")
    with Projects.open(home) as projects:
        assert projects.get("acq").state == "ready"
    assert fault_flag(Path(project.working_copy.path)).exists()


def test_reconcile_interrupted_demo_without_directory_fails(tmp_path):
    home = _home(tmp_path)
    with Projects.open(home) as projects:
        project = projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
        projects.store.put_project(replace(project, state="creating"))
    import shutil
    shutil.rmtree(project.working_copy.path)
    with Projects.open(home) as projects:
        assert (projects.get("acq").state, projects.get("acq").failure) == ("failed", "interrupted")


def test_reconcile_stray_fault_file_does_not_disturb_demo(tmp_path):
    """A stray ``clones/<id>.fault`` file (left by a killed target run) must not confuse
    reconciliation of an interrupted ``creating`` record — it is not a clone or a directory."""
    home = _home(tmp_path)
    with Projects.open(home) as projects:
        project = projects.create_demo(id="acq", environment_digest=DIGEST, executable=sys.executable, port=18765)
        projects.store.put_project(replace(project, state="creating"))
    (Path(project.working_copy.path).parent / "acq.fault").write_text("")
    with Projects.open(home) as projects:
        assert projects.get("acq").state == "ready"


def test_two_demos_get_distinct_names_with_the_id(tmp_path):
    with Projects.open(_home(tmp_path)) as projects:
        first = projects.create_demo(id="one", environment_digest=DIGEST, executable=sys.executable, port=18765)
        second = projects.create_demo(id="two", environment_digest=DIGEST, executable=sys.executable, port=18766)
    assert first.name != second.name and "one" in first.name and "two" in second.name


def test_demo_manifest_is_15_with_the_observe_smoke_profile(tmp_path):
    from evolution.tree import load_manifest_v2
    from evolution_lab._manifest_schema import instance_demand
    wc = _demo(tmp_path)
    path = wc / "lab" / "manifest.json"
    manifest = json.loads(path.read_text())
    assert manifest["schema"] == "evolution-v2-manifest/15"
    assert manifest["campaign"]["gate"]["mode"] == "observe" and manifest["grouper"]["kind"] == "fields"
    assert manifest["campaign"]["evaluation"] == {"repeats_per_question": 1} and "alpha" not in manifest["campaign"]
    assert load_manifest_v2(path).workflow.campaign.gate_mode == "observe"
    launch = json.loads((wc / "launch.json").read_text())
    assert launch["limits"]["max_instances"] == instance_demand(load_manifest_v2(path), 1)


def test_campaign_demo_builds_on_the_shared_project_skeleton(tmp_path):
    from evolution_lab.projects.demo import campaign_project
    from evolution_lab.quickstart import new_project
    project = campaign_project(tmp_path / "campaign")
    ordinary = new_project(tmp_path / "ordinary")
    for relative in ("scorer/score.py", "agent/adapter.py", "editor/edit.py", "reflector/reflect.py", "corpus.jsonl"):
        assert (project / relative).read_bytes() == (ordinary / relative).read_bytes(), relative
    manifest = json.loads((project / "manifest.json").read_text())
    evaluation = json.loads((project / "evaluation-input.json").read_text())
    assert manifest["campaign"]["gate"]["mode"] == "observe"
    for name in ("seed_root", "corpus", "scorer"):
        actual = dict(manifest[name]) if isinstance(manifest[name], dict) else manifest[name]
        if name == "scorer":
            actual.pop("metric_contract")
        expected = evaluation[name]
        if name == "scorer":
            expected = {key: value for key, value in expected.items() if key != "kind"}
        assert actual == expected


def test_registry_demo_prepares_its_shipped_evaluation_request(tmp_path):
    from evolution_lab.preparation import (
        CampaignSource, EnvironmentInput, PreparationStore, SourceRoot, describe_environment,
    )
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel

    environment = EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL))
    descriptor = describe_environment(environment)
    wc = tmp_path / "demo"
    write_demo(wc, final=wc, project_id="demo", environment_digest=descriptor.digest,
               executable=descriptor.executable, port=18765)
    launch = json.loads((wc / "launch.json").read_text())
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(source=CampaignSource(wc / "lab/manifest.json", wc / "launch.json"),
                                 environments=(environment,), env=launch["env"], secret_names=(),
                                 roots=(SourceRoot("project", wc),))
    request = json.loads((wc / "lab/evaluation-request.json").read_text())
    request.pop("kind")
    request.pop("bundle_digest")
    prepared = store.prepare_evaluation(bundle.digest, **request)
    assert prepared.kind == "evaluation"
