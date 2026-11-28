"""Native parallel campaign through Lab and the A3 server; no provider calls."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from threading import Thread

from evolution.tree import load_manifest_v2
from evolution.tree.reference_target_cli import scaffold
from evolution.tree.execution.interaction import TurnResult
from evolution.tree.execution.usage import Usage
from evolution.tree.target.target_server import BackendReply, TargetServer, serve_target
from evolution_lab._manifest_schema import instance_demand
from evolution_lab._scaffold_manifest import as_manifest_15
from evolution_lab.provenance import WheelPin
from evolution_lab.recipe import prepare_inputs
from evolution_lab.report import read_campaign_report
from evolution_lab.runner import run_native


class Session:
    def __init__(self, instance_id, answer, seed):
        self.instance_id, self.answer, self.effective_seed = instance_id, answer, seed

    def turn(self, message):
        return BackendReply(TurnResult(
            events=({"type": "assistant_message", "content": self.answer},),
            usage=Usage(prompt=0, completion=0), latency_ms=0, outcome="ok"))

    def close(self):
        pass


class Instance:
    def __init__(self, tree):
        self.instance_id = tree.instance_id
        self.answer = (tree.root / "skills/SKILL.md").read_text().splitlines()[0]

    def open(self, public, seed):
        return Session(self.instance_id, self.answer, seed)

    def close(self):
        pass


class Backend:
    def load(self, tree):
        return Instance(tree)


async def main(args):
    for value in (args.max_concurrency, args.max_scorer_concurrency):
        if type(value) is not int or value < 1:
            raise ValueError("concurrency limits must be positive integers")
    pins = tuple(WheelPin.capture(Path(path)) for path in args.wheel)
    if not any(pin.name == "evo-skills" for pin in pins):
        raise ValueError("an exact verified core wheel is required")
    root = Path(args.output).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    sample = scaffold(root / "recipe")
    # Core scaffolds manifest /15; Lab writes /15 with core's observe smoke profile (gate not enforced: wiring only).
    raw = as_manifest_15(json.loads(sample.manifest.read_text()))
    raw["campaign"]["max_concurrency"] = args.max_concurrency
    raw["campaign"]["max_scorer_concurrency"] = args.max_scorer_concurrency
    for role in ("editor", "scorer"):
        raw[role]["argv"][0] = sys.executable
    sample.manifest.write_text(json.dumps(raw, indent=2) + "\n")
    manifest = load_manifest_v2(sample.manifest)
    # A3 serializes callbacks per instance. Advertising one session makes core
    # allocate distinct instances. Core owns admission, materialize and release.
    # Family 14 (/15): core's own admission sets the peak (finalists and the Champion side, each
    # Attempt's fresh Parent beside its Candidate, times max_concurrency); declare that peak.
    capacity = instance_demand(manifest, 1)
    target = TargetServer(Backend(), allowed_artifact_root=root, policy=manifest.policy,
        seed_mode="exact", target_id="offline-campaign", contract_digest="synthetic-1",
        settings_hash="no-models-1", runtime_ref_digest="synthetic-runtime-1",
        max_instances=capacity, max_concurrent_sessions=1)
    http = serve_target(target, port=0)
    thread = Thread(target=http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        raw["target"]["base_url"] = f"http://127.0.0.1:{http.server_port}"
        sample.manifest.write_text(json.dumps(raw, indent=2) + "\n")
        prepared = prepare_inputs(sample.manifest, wheel_paths=tuple(pin.path for pin in pins))
        result = await run_native(prepared, root / "run", dispatch_guard=lambda: None)
        if result.exit_code != 0 or result.stop_requested:
            raise RuntimeError(f"campaign did not finish normally; inspect {result.stderr}")
        read_campaign_report(root / "run", distributions=pins, comparison_scope="in-sample").write(root / "report")
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)
        target.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--wheel", action="append", required=True)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--max-scorer-concurrency", type=int, default=1)
    asyncio.run(main(parser.parse_args()))
