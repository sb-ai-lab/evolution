"""Capture dev65 references from immutable source; no target/scorer is executed."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

PRODUCER = "b9cdcc2275f8a99ff16671a69cd2f40e18486114"
REFERENCE_EXECUTABLE = "/usr/bin/python3"


def restore_bundle(record: dict, state: Path) -> str:
    """Restore this synthetic immutable Bundle for future preparation comparisons."""
    digest = record["bundle_summary"]["digest"]
    destination = state / "bundles" / digest
    for item in record["bundle_files"]:
        path = destination / item["path"]
        if item["type"] == "directory":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["base64"]))
            path.chmod(0o644 | item["executable"])
    for item in reversed(record["bundle_files"]):
        if item["type"] == "directory":
            (destination / item["path"]).chmod(0o644 | item["executable"])
    return digest


def produce(root: Path, destination: Path) -> None:
    sys.path[:0] = [str(root / "src"), str(root / "tests")]
    from pytest import MonkeyPatch
    from test_preparation_evaluation import _campaign_bundle, _evaluation_bundle
    from test_service_cli import FakeClient, invoke
    from evolution_lab.cli.responses import validate
    from evolution_lab.preparation import PreparationError, PreparationStore
    from evolution_lab.preparation.storage import canonical_json_bytes
    from evolution_lab.projects.facts import FactContext, OperationView, TaskView, compute_facts
    from evolution_lab.projects.inventory import Entry, inventory_digest
    from evolution_lab.projects.models import Implementation, Pair, Project, WorkingCopy
    from evolution_lab.projects.revision import Revision

    destination.mkdir(parents=True, exist_ok=True)
    common = {
        "selection": {"all": True}, "repeat_seeds": [31], "evaluation_set_id": "neutral-fixed-set",
        "max_turns": 3, "timeout_seconds": 4, "metric_plan": {"primary": "score"},
    }
    tolerance = {
        "max_failed_turns": 2, "call_retries": {"max_attempts": 4, "turn_max_attempts": 2},
        "sample_retries": 2, "min_coverage": 0.9, "concurrency": 1, "scorer_concurrency": 2,
    }
    with MonkeyPatch.context() as patch:
        # Pin a real declared interpreter before composition, never the output/digest.
        # Environment capture is synthetic in these existing preparation fixtures.
        patch.setattr(sys, "executable", REFERENCE_EXECUTABLE)
        for kind, factory in (("evaluation", _evaluation_bundle), ("campaign", _campaign_bundle)):
            workspace = root / "workspace" / kind
            workspace.mkdir(parents=True)
            kwargs = {"schema": "lab-evaluation-input/2"} if kind == "evaluation" else {}
            bundle, state = factory(workspace, patch, **kwargs)
            store = PreparationStore(state)
            stored = store._storage.get_bundle(bundle.digest)
            files = []
            for path in sorted(stored.path.rglob("*")):
                item = {"path": path.relative_to(stored.path).as_posix(),
                        "type": "directory" if path.is_dir() else "file",
                        "executable": path.stat().st_mode & 0o111}
                if path.is_file():
                    item.update(base64=base64.b64encode(path.read_bytes()).decode("ascii"))
                files.append(item)
            record = {"producer_commit": PRODUCER, "bundle_summary": bundle.to_payload(),
                      "bundle_files": files, "requests": {}}
            for name, arguments in (("explicit", {**common, **tolerance}), ("omitted", common)):
                value = {"arguments": arguments}
                try:
                    prepared = store.prepare_evaluation(bundle.digest, **arguments)
                except PreparationError as error:
                    value["failure"] = error.failure.to_payload()
                else:
                    descriptor = store._storage.get_prepared(prepared.digest).descriptor.to_payload()
                    value.update(prepared_digest=prepared.digest, prepared=prepared.to_payload(),
                                 descriptor=descriptor,
                                 canonical_descriptor=canonical_json_bytes(descriptor).decode("utf-8"),
                                 canonical_plan=canonical_json_bytes(prepared.evaluation.to_payload()).decode("utf-8"))
                record["requests"][name] = value
            restored_state = root / "workspace" / f"{kind}-restored"
            restored = PreparationStore(restored_state)
            restore_bundle(record, restored_state)
            assert restored.get_bundle(bundle.digest).to_payload() == record["bundle_summary"]
            for name, reference in record["requests"].items():
                try:
                    replay = restored.prepare_evaluation(bundle.digest, **reference["arguments"])
                except PreparationError as error:
                    assert error.failure.to_payload() == reference["failure"]
                else:
                    replay_descriptor = restored._storage.get_prepared(replay.digest).descriptor.to_payload()
                    assert replay.to_payload() == reference["prepared"]
                    assert replay.digest == reference["prepared_digest"]
                    assert canonical_json_bytes(replay_descriptor).decode() == reference["canonical_descriptor"]
                    assert canonical_json_bytes(replay.evaluation.to_payload()).decode() == reference["canonical_plan"]
            code, output, error = invoke(
                ["--socket", "/tmp/reference.sock", "--json", "bundle", "show", bundle.digest],
                FakeClient([store.get_bundle(bundle.digest).to_payload()]),
            )
            assert code == 0, error
            validate(json.loads(output), "lab-bundle-summary/2")
            (destination / f"{kind}-bundle-show.json").write_text(output)
            (destination / f"{kind}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")

    # A pure, successful project-evidence fixture; no real project/pair state is created.
    pair = Pair("0.0.3.dev65", "2.0.0b15")
    entries = (Entry("agent/main.py", "file", 0, 1, "1" * 64),)
    inputs = inventory_digest(entries)
    revision = Revision(inputs, (), 1, None, ())
    binding = {"working_copy": "/reference/project", "materials_root": "lab", "place": "agent",
               "materials_revision": inputs, "inputs": {"digest": inputs, "sources": []}}
    implementation = Implementation("agent", "agent", "agent", {"kind": "connected"}, "bundle-request.json", None)
    project = Project("neutral", "Neutral", "ready", None, "t", None, 1,
                      WorkingCopy("/reference/project", False, False, None), "lab", (), (implementation,))
    tasks = tuple(TaskView(f"{action}-{order}", action, "succeeded", "target-1", pair,
                           binding, inputs, entries, inputs, entries, order)
                  for order, action in enumerate(("check", "conformance"), 1))
    result = {"coverage": {"expected": 2, "scored": 2}, "stop_reason": None,
              "persistence": {"state": "saved", "failure": None}}
    evaluation = OperationView("evaluation-1", "evaluation", pair, "finished", result, binding,
                               inputs, entries, inputs, entries, 3)
    context = FactContext(project=project, implementation=implementation, pair=pair, revision=revision,
                          package_present=True, package_errors=(), place_available=True,
                          tasks=tasks, operations=(evaluation,), campaigns=(), confirmations=(),
                          confirmation_now={}, acceptances=(), inventories={})
    facts = compute_facts(context)
    assert all(fact.holds for fact in facts if fact.id in {
        "check.passed", "conformance.passed", "baseline.measured", "connected"})
    payload = {"schema": "lab-project-facts/4", "project": context.project.id,
               "implementation": context.implementation.id, "pair": pair.to_payload(),
               "revision": revision.to_payload(), "facts": [fact.to_payload() for fact in facts]}
    validate(payload, "lab-project-facts/4")
    (destination / "project-facts.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--snapshot-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.snapshot_root:
        produce(args.snapshot_root, args.output.resolve())
        return
    repository = Path(__file__).resolve().parents[3]
    archive = subprocess.check_output([
        "git", "archive", PRODUCER, "src", "tests/test_preparation_evaluation.py",
        "tests/test_preparation_api.py",
        "tests/test_service_cli.py", "tests/scaffold_15.py",
    ], cwd=repository)
    with tempfile.TemporaryDirectory(prefix="lab-dev66-reference-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(root, filter="data")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        environment.pop("PYTHONPATH", None)
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--snapshot-root", str(root),
                        "--output", str(args.output.resolve())], env=environment, check=True)


if __name__ == "__main__":
    main()
