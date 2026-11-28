"""Run a complete neutral offline campaign in one fresh producing-pair state.

The ordinary project skeleton supplies the corpus, scorer, adapter and sample
Editor and Reflector. The demo adds an observe gate and a synthetic agent that
answers its three code-word questions correctly after the Editor marks the
skill as improved. This shows the wiring, not evidence of quality. No model, network or credential is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evolution_lab.durability import check as check_durability
from evolution_lab.owner import Owner, ResourceRef, TargetRef
from evolution_lab.preparation import (
    CampaignSource,
    EnvironmentInput,
    PreparationStore,
    SourceRoot,
    describe_environment,
)
from evolution_lab.projects.demo import campaign_project

_ACTIVE = {"reserved", "running", "stopping"}


def _write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _successful_task(status, action: str) -> None:
    if status.state != "succeeded":
        raise RuntimeError(f"{action} did not succeed: {status.state}")


def _target_ref(status) -> TargetRef:
    _successful_task(status, "target_start")
    if status.ref is None or status.ref.kind != "target":
        raise RuntimeError("target_start did not return a target resource")
    return TargetRef(status.ref.id, status.owner_generation)


async def _finished(owner: Owner, ref: ResourceRef):
    status = await owner.status(ref)
    while status.control_state in _ACTIVE:
        await asyncio.sleep(0.2)
        status = await owner.status(ref)
    return status


def _prepare(args):
    state = args.state_directory.absolute()
    source = args.source_directory.absolute()
    requested_python = args.python.absolute()
    if state.exists() or source.exists():
        raise RuntimeError("state and source directories must both be fresh")
    if state == source or state in source.parents or source in state.parents:
        raise RuntimeError("state and source directories must be separate trees")
    if requested_python.resolve() != Path(sys.executable).resolve():
        raise RuntimeError("run this example with the same interpreter passed as --python")

    campaign_project(source)
    manifest_path = source / "manifest.json"
    environment_input = EnvironmentInput(
        requested_python,
        (args.lab_wheel.absolute(), args.core_wheel.absolute()),
    )
    environment = describe_environment(environment_input)

    # The shared skeleton with the offline observe profile: wiring only.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target"]["base_url"] = f"http://127.0.0.1:{args.port}"
    manifest["target"]["max_response_bytes"] = 8192
    manifest["target"]["contract_digest"] = "a" * 64
    manifest["target"]["settings_hash"] = "b" * 64
    for role in ("scorer", "editor", "reflector"):
        # Roles run with the declared interpreter, never a bare `python3`.
        manifest[role]["argv"][0] = environment.executable
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    launch = source / "campaign-launch.json"
    _write_json(
        launch,
        {
            "schema": "lab-target-launch/2",
            "agent": {
                "module": "adapter",
                "attribute": "build",
                "import_root": "agent",
            },
            "environment_ref": environment.digest,
            "program_paths": ["agent"],
            "env": {},
            "secret_names": [],
            "endpoint": {"base_url": manifest["target"]["base_url"]},
            "identity": {
                "target_id": "neutral-campaign-target",
                "contract_digest": "a" * 64,
                "settings_hash": "b" * 64,
                "runtime_ref_digest": "c" * 64,
                "seed_mode": manifest["target"]["seed_mode"],
            },
            "policy": manifest_path.name,
            "limits": {
                "max_instances": 2,
                "max_concurrent_sessions": 2,
                "max_input_bytes": 1 << 20,
                "max_output_bytes": 8192,
            },
            "startup_timeout_seconds": 10,
            "stop_grace_seconds": 10,
            "conformance": None,
        },
    )

    store = PreparationStore(state)
    bundle = store.create_bundle(
        source=CampaignSource(manifest_path, launch),
        environments=(environment_input,),
        env={},
        secret_names=(),
        roots=(SourceRoot("neutral", source),),
    )
    prepared = store.prepare_campaign(bundle.digest)
    return state, bundle, prepared


async def _run(state: Path, bundle, prepared) -> dict[str, object]:
    async with Owner.open(state) as owner:
        connection = await owner.bind(bundle.digest)

        started = await owner.wait(await owner.start_target(connection, secrets={}))
        target = _target_ref(started)
        check = await owner.wait(await owner.check(connection, target_ref=target))
        _successful_task(check, "check")

        accepted = await owner.start(prepared.digest, target_ref=target, secrets={})
        operation_ref = ResourceRef("operation", accepted.id)
        operation = await _finished(owner, operation_ref)
        if operation.control_state != "finished" or operation.result is None:
            raise RuntimeError(f"campaign did not finish: {operation.control_state}")
        core = operation.result.to_payload()["core"]
        if not isinstance(core, dict):
            raise RuntimeError("campaign result is unavailable")
        report = await owner.report(operation_ref)

        # The core decides promotion; Lab only reads the champion's changes.
        diff = await owner.diff(operation_ref)
        export = await owner.wait(await owner.export(operation_ref))
        _successful_task(export, "export")

        stopped = await owner.wait(await owner.stop_target(target))
        _successful_task(stopped, "target_stop")
        after = await owner.target(target)
        health = await owner.health()
        if after.state != "stopped" or health.blockers:
            raise RuntimeError("final target cleanup is not confirmed")

        # Full core reports, journals and trajectories stay in the private
        # state tree. Print only neutral lifecycle facts and the outcome.
        return {
            "schema": "lab-neutral-campaign-example/1",
            "check": check.state,
            "campaign": {
                "control_state": operation.control_state,
                "outcome": core["campaign_outcome"]["outcome"],
                "reason": core["campaign_outcome"]["reason"],
                "rounds": [round_["outcome"] for round_ in core["rounds"]],
                "final_champion": core["final_champion"]["version"],
                "process": operation.result.process.to_payload(),
                "target_cleanup": operation.target_cleanup.to_payload(),
                "persistence": operation.result.persistence.to_payload(),
                "report_state": report.state,
            },
            "diff": {
                "state": diff.state,
                "changed_text": bool(diff.text),
            },
            "export": {
                "state": export.state,
                "persistence": export.persistence.to_payload(),
            },
            "final_target": {
                "state": after.state,
                "cleanup": after.cleanup.to_payload(),
            },
            "blocker_count": len(health.blockers),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--lab-wheel", type=Path, required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True,
                        help="free port on 127.0.0.1 for the stand-in agent the core calls; not the panel")
    args = parser.parse_args()
    check_durability()
    state, bundle, prepared = _prepare(args)
    result = asyncio.run(_run(state, bundle, prepared))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
