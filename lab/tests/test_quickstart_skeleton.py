"""The project skeleton shipped in the wheel and `lab new-project`."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution_lab.cli.parser import main
from evolution_lab.quickstart import SKELETON_FILES, QuickstartError, new_project

ROOT = Path(__file__).resolve().parents[1]
SKELETON = ROOT / "src" / "evolution_lab" / "quickstart" / "skeleton"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(directory: Path) -> set[str]:
    return {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}


def test_skeleton_ships_in_wheel(tmp_path):
    wheel_path = os.environ.get("EVOLUTION_LAB_WHEEL")
    if wheel_path:
        wheel = Path(wheel_path)
    else:
        out = tmp_path / "wheel"
        subprocess.run(
            ["uv", "build", "--offline", "--wheel", "--out-dir", str(out), str(ROOT)],
            check=True,
            capture_output=True,
        )
        wheel = next(out.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    prefix = "evolution_lab/quickstart/skeleton/"
    shipped = {n[len(prefix):] for n in names if n.startswith(prefix) and not n.endswith("/")}
    assert shipped == {"gitignore" if f == ".gitignore" else f for f in SKELETON_FILES}


def test_new_project_copies_skeleton(tmp_path):
    target = tmp_path / "nested" / "my-agent-lab"
    result = new_project(target)
    assert result == target.absolute()
    assert _files(target) == set(SKELETON_FILES)
    assert (target / ".gitignore").read_text() == ".lab/\n"
    new_project(tmp_path / "again")  # a second project does not clash
    empty = tmp_path / "empty"
    empty.mkdir()
    assert new_project(empty) == empty.absolute()


def test_new_project_refuses_non_empty(tmp_path):
    (tmp_path / "stray.txt").write_text("mine")
    with pytest.raises(QuickstartError) as error:
        new_project(tmp_path)
    assert (error.value.code, error.value.field, error.value.exit_code) == ("directory_not_empty", None, 2)
    assert _files(tmp_path) == {"stray.txt"}
    (tmp_path / "file").write_text("x")
    with pytest.raises(QuickstartError) as error:
        new_project(tmp_path / "file")
    assert error.value.code == "invalid_directory"


def test_new_project_cli(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert main(["new-project", str(tmp_path / "p")], stdout=out, stderr=err) == 0
    steps = [line.strip() for line in out.getvalue().splitlines() if re.match(r"  \d\. ", line)]
    assert [step[:2] for step in steps] == ["1.", "2.", "3."]
    directory = str((tmp_path / "p").absolute())
    assert f"lab evaluate {directory} --wheel LAB_WHEEL --wheel CORE_WHEEL" in steps[2]
    assert f"just evaluate {directory}" in out.getvalue()
    out, err = io.StringIO(), io.StringIO()
    assert main(["new-project", str(tmp_path / "p")], stdout=out, stderr=err) == 2
    assert json.loads(err.getvalue())["error"]["code"] == "directory_not_empty"
    assert _files(tmp_path / "p") == set(SKELETON_FILES)


class _Message:
    def __init__(self, content):
        self.content = content


def test_new_project_unexpected_error_exits_3(tmp_path):
    """A directory Lab cannot read is not a mistake it names in advance: exit 3, never the service client's 1."""
    target = tmp_path / "locked"
    target.mkdir()
    target.chmod(0)
    try:
        out, err = io.StringIO(), io.StringIO()
        code = main(["new-project", str(target)], stdout=out, stderr=err)
    finally:
        target.chmod(0o755)
    assert code == 3, err.getvalue()
    error = json.loads(err.getvalue())["error"]
    assert (error["code"], error["stage"]) == ("unexpected_error", "new-project")
    assert "PermissionError" in error["message"]


def test_skeleton_adapter_offline_turn(tmp_path, monkeypatch):
    project = new_project(tmp_path / "p")
    monkeypatch.syspath_prepend(str(project / "agent"))
    for name in ("my_agent", "adapter"):
        sys.modules.pop(name, None)
    adapter = _load(project / "agent" / "adapter.py", "skeleton_adapter")
    import my_agent

    view = SimpleNamespace(root=project / "seed")
    agent = asyncio.run(adapter.build(SimpleNamespace(env={}, secrets={}))(view))

    async def drive():
        await agent.open({}, session_id="s1", seed=None)
        ok = await agent.turn(_Message("What is the code word for alpha?"), {}, session_id="s1", seed=None)
        await agent.open({"conformance": True}, session_id="s2", seed=None)
        calls = []
        monkeypatch.setattr(my_agent.MyAgent, "answer", lambda self, q: calls.append(q) or "x")
        conf = await agent.turn(_Message("conformance"), {}, session_id="s2", seed=None)
        await agent.close_session("s1")
        await agent.close_session("s2")
        await agent.aclose()
        return ok, conf, calls

    ok, conf, calls = asyncio.run(drive())
    assert ok.outcome == "ok" and ok.text == "A1"
    assert ok.provenance is not None
    assert ok.provenance.used == ("skills/SKILL.md",)
    assert "code word only" in (view.root / ok.provenance.used[0]).read_text().lower()
    assert conf.outcome == "ok" and calls == []
    assert conf.provenance is None  # The protocol self-test does not call the agent.
    assert agent.describe() == {"agent": "my-agent"}


def test_skeleton_reports_the_prompt_from_the_candidate_tree(tmp_path, monkeypatch):
    project = new_project(tmp_path / "p")
    candidate = tmp_path / "candidate"
    (candidate / "skills").mkdir(parents=True)
    (candidate / "skills/SKILL.md").write_text("Be friendly.", encoding="utf-8")
    monkeypatch.syspath_prepend(str(project / "agent"))
    monkeypatch.delitem(sys.modules, "my_agent", raising=False)
    adapter = _load(project / "agent/adapter.py", "skeleton_candidate_adapter")

    async def drive():
        agent = await adapter.build(SimpleNamespace(env={}, secrets={}))(SimpleNamespace(root=candidate))
        await agent.open({}, session_id="s", seed=None)
        return await agent.turn(_Message("What is the code word for alpha?"), {}, session_id="s", seed=None)

    turn = asyncio.run(drive())
    assert turn.text == "The code word is A1."
    assert turn.provenance is not None and turn.provenance.used == ("skills/SKILL.md",)
    assert (candidate / turn.provenance.used[0]).read_text() == "Be friendly."


def test_skeleton_scorer_scores(tmp_path):
    project = new_project(tmp_path / "p")
    cases = [json.loads(line) for line in (project / "corpus.jsonl").read_text().splitlines()]
    assert len(cases) == 3
    known = {"alpha": "A1", "beta": "B2"}
    scores, verdicts = [], set()
    for case in cases:
        word = next((v for k, v in known.items() if k in case["public"]["text"]), "unknown")
        view = {"history": {"turns": [{"message": {"content": case["public"]["text"]}, "events": [
            {"type": "assistant_message", "content": word}]}]}, "terminal": "done"}
        done = subprocess.run([sys.executable, str(project / "scorer/score.py")], capture_output=True, text=True,
                              check=True, input=json.dumps({"case_id": case["case_id"], "public": case["public"],
                                                            "oracle": case["oracle"], "view": view}))
        result = json.loads(done.stdout)
        scores.append(result["metrics"]["score"])
        assert result["critical_pass"] is True
        verdicts |= {v["criterion"] for v in result["verdicts"]}
    assert scores == [1, 1, 0] and verdicts == {"answered", "matched"}
    empty = subprocess.run([sys.executable, str(project / "scorer/score.py")], capture_output=True, text=True,
                           check=True, input=json.dumps({"case_id": "q", "public": {}, "oracle": {"expected": "A1"},
                                                         "view": {"history": {"turns": []}}}))
    assert {v["criterion"]: v["passed"] for v in json.loads(empty.stdout)["verdicts"]} == {
        "answered": False, "matched": False}


def _score(project, answer, expected):
    view = {"history": {"turns": [{"message": {"content": "q"}, "events": [
        {"type": "assistant_message", "content": answer}]}]}, "terminal": "done"}
    done = subprocess.run([sys.executable, str(project / "scorer/score.py")], capture_output=True, text=True,
                          check=True, input=json.dumps({"case_id": "q", "public": {}, "oracle": {"expected": expected},
                                                        "view": view}))
    return json.loads(done.stdout)["metrics"]["score"]


def test_skeleton_scorer_is_exact_match(tmp_path):
    project = new_project(tmp_path / "p")
    assert _score(project, "50", "50") == 1
    assert _score(project, "  a1\n", "A1") == 1          # strip() and case-folding
    assert _score(project, "150", "50") == 0               # not a substring match
    assert _score(project, "0.50", "50") == 0
    assert _score(project, "The code word is A1.", "A1") == 0
    assert "substring" in (project / "scorer/score.py").read_text()   # the docstring says how to switch


def test_skeleton_prompt_steers_the_stub(tmp_path):
    project = new_project(tmp_path / "p")
    monkey_path = str(project / "agent")
    sys.path.insert(0, monkey_path)
    try:
        sys.modules.pop("my_agent", None)
        import my_agent
        prompt = (project / "seed/skills/SKILL.md").read_text()
        assert my_agent.MyAgent(prompt).answer("What is the code word for alpha?") == "A1"
        chatty = my_agent.MyAgent("Be friendly.").answer("What is the code word for alpha?")
        assert chatty != "A1" and "A1" in chatty
    finally:
        sys.path.remove(monkey_path)
        sys.modules.pop("my_agent", None)


def test_skeleton_json_marks_only_adr_fields():
    launch = json.loads((SKELETON / "launch.json").read_text())
    evaluation = json.loads((SKELETON / "evaluation-input.json").read_text())
    assert launch["schema"] == "lab-target-launch/3" and evaluation["schema"] == "lab-evaluation-input/3"
    marked = {
        "launch.environment_ref": launch["environment_ref"],
        "launch.endpoint.base_url": launch["endpoint"]["base_url"],
        "launch.identity.contract_digest": launch["identity"]["contract_digest"],
        "launch.identity.settings_hash": launch["identity"]["settings_hash"],
        "launch.identity.runtime_ref_digest": launch["identity"]["runtime_ref_digest"],
        "eval.target.base_url": evaluation["target"]["base_url"],
        "eval.target.contract_digest": evaluation["target"]["contract_digest"],
        "eval.target.settings_hash": evaluation["target"]["settings_hash"],
        "eval.scorer.argv[0]": evaluation["scorer"]["argv"][0],
    }
    assert set(marked.values()) == {"lab:auto"}
    text = (SKELETON / "launch.json").read_text() + (SKELETON / "evaluation-input.json").read_text()
    assert text.count("lab:auto") == len(marked)
    assert evaluation["target"]["seed_mode"] == launch["identity"]["seed_mode"] == "none"
    assert launch["conformance"] == {"kind": "core-session-public/1"} and launch["secret_names"] == []


def test_skeleton_neutral():
    bad = re.compile(r"^\s*(from|import)\s+(langchain|langgraph|llama_index|openai|anthropic|autogen|crewai|haystack)\b",
                     re.MULTILINE | re.IGNORECASE)
    # Inspect exactly what the wheel ships; interpreter caches are not project files.
    for name in SKELETON_FILES:
        path = SKELETON / ("gitignore" if name == ".gitignore" else name)
        text = path.read_text(encoding="utf-8")
        assert "template" not in text.lower(), path
        assert not bad.search(text), path


def test_skeleton_states_every_tolerance_key():
    defaults = json.loads((SKELETON / "evaluation-input.json").read_text())["defaults"]
    assert defaults["sample_retries"] == 3
    assert defaults["min_coverage"] == 0.95
    assert defaults["max_consecutive_unmeasured"] == 3
    assert defaults["max_failed_turns"] == defaults["max_turns"]
    assert defaults["call_retries"] == {"max_attempts": 3, "turn_max_attempts": 2}
    manifest = json.loads((SKELETON / "manifest.json").read_text())
    campaign = manifest["campaign"]
    assert campaign["sample_retries"] == 3
    assert campaign["call_retries"] == {"max_attempts": 3, "turn_max_attempts": 2}
    assert campaign["budget"]["max_failed_turns"] == campaign["budget"]["max_turns"]
    assert set(campaign["indeterminate"]) == {"target", "environment", "scorer", "rollout"}
    assert all(value == {"max_count": 20, "max_rate": "1"}
               for value in campaign["indeterminate"].values())
    assert manifest["reflector"]["max_consecutive_failures"] == 5


def test_skeleton_readme_explains_each_tolerance_key():
    text = (SKELETON / "README.md").read_text()
    for key in ("sample_retries", "min_coverage", "max_consecutive_unmeasured", "max_failed_turns",
                "call_retries", "max_attempts", "turn_max_attempts", "indeterminate",
                "max_count", "max_rate", "max_consecutive_failures", "retry", "no_retry", "fail_fast"):
        assert key in text, key


def test_skeleton_contains_both_launch_paths(tmp_path):
    project = new_project(tmp_path / "project")
    assert {"manifest.json", "campaign-launch.json", "editor/edit.py", "reflector/reflect.py"} <= _files(project)
    evaluation_launch = json.loads((project / "launch.json").read_text())
    campaign_launch = json.loads((project / "campaign-launch.json").read_text())
    manifest = json.loads((project / "manifest.json").read_text())
    assert evaluation_launch["policy"] == "evaluation-input.json"
    assert campaign_launch["policy"] == "manifest.json"
    assert campaign_launch["identity"]["seed_mode"] == manifest["target"]["seed_mode"]
    assert campaign_launch["endpoint"]["base_url"] == manifest["target"]["base_url"]


def test_new_project_refuses_a_symlinked_root(tmp_path):
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir()
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(QuickstartError) as failure:
        new_project(link)
    assert failure.value.code == 'invalid_directory'
    assert not list(real.iterdir())
