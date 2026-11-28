"""A free demo project (spec §3.12): the core reference target with a synthetic agent.

No git, no model, no paid call. The agent answers with the first line of the measured
tree's ``skills/SKILL.md``, so the reference editor's change shows up in what the
reference scorer sees. Each answered turn also carries one step event, a ``usage``
(characters counted as tokens) and an ``agent_outcome`` reason (``handoff`` when the
message mentions an operator, else ``answered``), so a probe session on the free demo
shows what a real agent may report (spec 2026-10-02 §6). While ``<working copy>.fault``
exists every turn fails as the target's fault (``target_failed``), which suspends a
campaign deterministically — the cheap suspension a client can use to test ``resume``
(auto-evolve request, item 7): under /15 core's connection check stops at it
(``connection_preflight_unavailable``).
A flag containing ``empty`` makes the agent emit an ok turn with no text. By default,
the Lab turn layer classifies it as ``target_failed`` with reason ``lab/empty_answer``.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

from evolution_lab import guides
from evolution_lab.guides import answers

FAULT_ENV = "LAB_DEMO_FAULT_FLAG"
MATERIALS_ROOT = "lab"
IMPLEMENTATION = {"id": "demo", "place": "agent", "package_entry": "package.json"}

_AGENT = '''"""Synthetic demo agent (Evolution Lab demo project): no model, no network."""
from pathlib import Path

from evolution.tree import Usage
from evolution_lab.target import AGENT_OUTCOME_CATEGORY, AgentTurn, TurnReason


class DemoAgent:
    def __init__(self, root, flag):
        skill = Path(root) / "skills" / "SKILL.md"
        self.line = skill.read_text(encoding="utf-8").splitlines()[0]
        self.flag = flag

    def describe(self):
        return {"agent": "lab-demo"}

    async def open(self, public, *, session_id, seed):
        return {"turns": 0}

    async def turn(self, message, state, *, session_id, seed):
        if self.flag and Path(self.flag).exists():
            if Path(self.flag).read_text(encoding="utf-8").strip() == "empty":
                # The Lab turn layer converts this empty ok answer to target_failed.
                return AgentTurn("ok", "", events=({"type": "tool_call", "name": "read_skill"},),
                                 state={"turns": state["turns"] + 1})
            return AgentTurn("target_failed", "")
        content = message.content
        answer = f"{self.line} | {content}"
        outcome = "handoff" if "оператор" in content.lower() else "answered"
        return AgentTurn("ok", answer, events=({"type": "tool_call", "name": "read_skill"},),
                         state={"turns": state["turns"] + 1},
                         usage=Usage(prompt=len(content), completion=len(answer), cached=0,
                                     total=len(content) + len(answer)),
                         reason=TurnReason(AGENT_OUTCOME_CATEGORY, outcome))

    async def close_session(self, session_id):
        return None

    async def aclose(self):
        return None


def build(context):
    flag = context.env.get("''' + FAULT_ENV + '''", "")

    async def factory(view):
        return DemoAgent(view.root, flag)
    return factory
'''

_SPEC = """# Demo agent — task specification

The demo agent answers every message with the first line of its skill file.
The project scorer compares each answer with the expected improved skill line and question.

- Changeable: `skills/**` of the seed (the campaign manifest's writable artifacts).
- Fixed: the corpus, the scorer, the agent code.

Confirm this spec with `lab project confirm` (step `spec-confirm` of the `connect` scenario).
"""


def fault_flag(working_copy: Path) -> Path:
    working_copy = Path(working_copy)
    return working_copy.with_name(working_copy.name + ".fault")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def campaign_project(directory: Path) -> Path:
    """The ordinary skeleton, with an observe gate and a deterministic free demo agent.

    The scorer, corpus, adapter and sample roles stay identical to new-project.
    Only this demo knows all three codes and requires the sample editor's marker.
    """
    from evolution_lab._scaffold_manifest import observe_profile
    from evolution_lab.quickstart import new_project

    project = new_project(directory)
    _write(project / "manifest.json", observe_profile(json.loads((project / "manifest.json").read_text())))
    (project / "seed/skills/SKILL.md").write_text("Reference skill: answer with this line.\n", encoding="utf-8")
    (project / "agent/my_agent.py").write_text('''"""Synthetic campaign demo: no model or network."""
_KNOWN = {"alpha": "A1", "beta": "B2", "gamma": "C3"}


class MyAgent:
    def __init__(self, prompt):
        self.improved = prompt.splitlines()[0].startswith("IMPROVED:")

    def answer(self, question):
        words = question.lower().replace("?", " ").split()
        code = next((_KNOWN[word] for word in words if word in _KNOWN), "unknown")
        return code if self.improved else "not improved"
''', encoding="utf-8")
    return project


def write_demo(staging: Path, *, final: Path, project_id: str, environment_digest: str, executable: str,
               port: int) -> None:
    """Write the demo working copy into the fresh ``staging`` directory; absolute paths (the
    fault flag) point at ``final``, where the caller renames ``staging``."""
    from evolution.tree import load_manifest_v2
    from evolution_lab._manifest_schema import instance_demand

    destination = Path(staging)
    destination.mkdir(mode=0o700)
    materials = destination / MATERIALS_ROOT
    campaign_project(materials)
    manifest_path = materials / "manifest.json"
    base_url = f"http://127.0.0.1:{port}"
    # Registry demo keeps its historical first-line agent and fault controls.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus = materials / "corpus.jsonl"
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    for row in rows:
        row["oracle"]["expected"] = "IMPROVED: reference skill | " + row["public"]["text"]
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    # Preserve the registry demo's explicit seeded evaluation request.
    manifest["target"].update(base_url=base_url, contract_digest="a" * 64, settings_hash="b" * 64,
                              seed_mode="exact")
    for role in ("scorer", "editor", "reflector"):
        manifest[role]["argv"][0] = executable  # roles run with the service interpreter
    _write(manifest_path, manifest)
    (materials / "SPEC.md").write_text(_SPEC, encoding="utf-8")
    _write(materials / answers.FILE,
           answers.complete_answers(guides.requirements(), implementation=IMPLEMENTATION["id"]))

    agent = destination / "agent"
    agent.mkdir()
    (agent / "demo_agent.py").write_text(_AGENT, encoding="utf-8")

    env = {FAULT_ENV: str(fault_flag(Path(final)))}
    _write(destination / "launch.json", {
        "schema": "lab-target-launch/3",
        "reflector_probe": None,
        "turn": {"empty_answer": "target_failed"},
        "environment_ref": environment_digest,
        "agent": {"module": "demo_agent", "attribute": "build", "import_root": "agent"},
        "program_paths": ["agent"],
        "env": env, "secret_names": [],
        "endpoint": {"base_url": base_url},
        "identity": {"target_id": f"lab-demo-{project_id}", "contract_digest": "a" * 64,
                     "settings_hash": "b" * 64, "runtime_ref_digest": "c" * 64,
                     "seed_mode": manifest["target"]["seed_mode"]},
        "policy": f"{MATERIALS_ROOT}/manifest.json",
        "limits": {"max_instances": instance_demand(load_manifest_v2(manifest_path), 1), "max_concurrent_sessions": 1,
                   "max_input_bytes": 1 << 20, "max_output_bytes": 4096},
        "startup_timeout_seconds": 10, "stop_grace_seconds": 5,
        "conformance": {"kind": "core-session-public/1"},
    })
    _write(materials / IMPLEMENTATION["package_entry"], {
        "source": {"kind": "campaign", "manifest_path": "manifest.json", "launch_path": "../launch.json"},
        "environments": [{"kind": "service"}],
        "env": env, "secret_names": [],
        "roots": [{"alias": "project", "source_path": ".."}], "path_bindings": [],
    })
    _write(materials / "evaluation-request.json", {
        "kind": "evaluation", "bundle_digest": None, "selection": {"case_ids": ["q1"]},
        "repeat_seeds": [17], "evaluation_set_id": "demo-baseline", "max_turns": 2,
        "timeout_seconds": 30, "metric_plan": {"primary": "score"}, "runtime_policy": None,
        "max_failed_turns": 1, "call_retries": "off",
    })
