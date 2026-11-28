"""Complete installed-wheel A3 example. Synthetic backend; no model calls.

Run with an isolated consumer interpreter and explicit wheel paths. The output
root must not exist. Replace the domain backend/cases/scorer to integrate an
agent; keep the shared Lab operation and core server.
"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from threading import Thread

from evolution.tree import load_manifest_v2
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.conformance import run_conformance
from evolution.tree.target.config import TargetBinding
from evolution.tree.reference_target_cli import scaffold
from evolution.tree.execution.interaction import EvalCase, RolloutBudget, ScriptedEnvironment, TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.rollout_scoring import SubprocessScorer
from evolution.tree.target.target_server import BackendReply, TargetServer, serve_target
from evolution_lab.provenance import WheelPin
from evolution_lab.report import read_evaluation_report
from evolution_lab.standalone import evaluate_target


class Session:
    effective_seed = None

    def __init__(self, answer):
        self.answer = answer

    def turn(self, message):
        return BackendReply(TurnResult(
            events=({"type": "assistant_message", "content": self.answer},),
            usage=Usage(prompt=0, completion=0), latency_ms=0, outcome="ok"))

    def close(self):
        pass


class Instance:
    def __init__(self, tree):
        self.answer = (tree.root / "instructions.txt").read_text()

    def open(self, public, seed):
        return Session(self.answer)

    def close(self):
        pass


class Backend:
    def load(self, tree):
        return Instance(tree)


async def main(args):
    pins = tuple(WheelPin.capture(Path(p)) for p in args.wheel)
    root = Path(args.output).resolve()
    root.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir()
    (source / "instructions.txt").write_text("synthetic answer")
    policy = ArtifactPolicyV1((), (), ("instructions.txt",))
    target = TargetServer(Backend(), allowed_artifact_root=root, policy=policy,
                          seed_mode="none", target_id="offline-example", contract_digest="example-1",
                          settings_hash="no-models-1", runtime_ref_digest="synthetic-runtime-1")
    http = serve_target(target, port=0)
    thread = Thread(target=http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        binding = TargetBinding(f"http://127.0.0.1:{http.server_port}", "none", "example-1", "no-models-1")
        # Scaffold is used only to obtain a core conformance manifest in this
        # synthetic example. Standalone evaluation does not read its editor.
        sample = scaffold(root / "conformance-config")
        manifest = replace(load_manifest_v2(sample.manifest), seed_root=source, policy=policy, target=binding)
        conformance = await run_conformance(manifest)
        (root / "conformance.json").write_text(json.dumps(conformance.to_payload(), indent=2) + "\n")
        if not conformance.ok:
            raise RuntimeError("conformance failed; see conformance.json")
        scorer = SubprocessScorer(
            argv=(sys.executable, "-I", str(Path(__file__).with_name("score.py"))),
            cwd=Path(__file__).resolve().parent, timeout_seconds=10, trace_root=root / "traces")
        result = await evaluate_target(
            source_root=source, policy=policy, target=binding,
            run_directory=root / "evaluation", run_id="offline-example", evaluation_set_id="synthetic",
            cases=(EvalCase("case-1", "group", "synthetic", (), {},
                            {"script": [{"content": "question", "fields": {}}]},
                            {"answer": "synthetic answer"}),),
            environment=ScriptedEnvironment(), scorer=scorer, repeat_seeds=(None, None),
            budget=RolloutBudget(max_turns=1, timeout_seconds=10), metric_plan={"primary": "quality"},
            wheels=pins, dispatch_guard=lambda: None,  # Only this model-free fixture.
        )
        assert result["coverage"] == {"expected": 2, "scored": 2}, result
        assert all(s["metrics"] == {"quality": 1.0} for s in result["core"]["samples"])
        read_evaluation_report(root / "evaluation/measurement", distributions=pins).write(root / "report")
        print(json.dumps({"conformance": True, "coverage": result["coverage"], "output": str(root)}))
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)
        target.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--wheel", action="append", required=True)
    asyncio.run(main(parser.parse_args()))
