"""Run the complete neutral S3 evaluation path in one fresh producing-pair state."""
from __future__ import annotations

import argparse
import asyncio
import json
from importlib import metadata
from pathlib import Path
import sys

from evolution_lab.owner import Owner, ResourceRef, TargetRef
from evolution_lab.preparation import (
    EnvironmentInput,
    EvaluationSource,
    PreparationStore,
    SourceRoot,
    describe_environment,
)
from evolution_lab.quickstart import new_project, resolve_project


_ACTIVE = {"reserved", "running", "stopping"}


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
        await asyncio.sleep(0.1)
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

    # The project is the packaged skeleton (what `lab new-project` writes): a stub agent,
    # a 3-case corpus and a scorer. Its "lab:auto" fields are filled into a run copy;
    # `source` itself is never written to again.
    new_project(source)
    environment_input = EnvironmentInput(
        requested_python,
        (args.lab_wheel.absolute(), args.core_wheel.absolute()),
    )
    environment = describe_environment(environment_input)
    run = source / ".lab" / "run"
    resolve_project(
        source,
        run,
        environment=environment,
        port=args.port,
        lab_version=metadata.version("evolution-lab"),
        core_version=metadata.version("evo-skills"),
    )
    launch = json.loads((run / "launch.json").read_text(encoding="utf-8"))

    store = PreparationStore(state)
    bundle = store.create_bundle(
        source=EvaluationSource(run / "evaluation-input.json", run / "launch.json"),
        environments=(environment_input,),
        env=dict(launch["env"]),
        secret_names=(),
        roots=(SourceRoot("project", run),),
    )
    prepared = store.prepare_evaluation(bundle.digest, selection={"all": True})
    return state, bundle, prepared


async def _run(state: Path, bundle, prepared) -> dict[str, object]:
    async with Owner.open(state) as owner:
        connection = await owner.bind(bundle.digest)

        first_start = await owner.wait(await owner.start_target(connection, secrets={}))
        first_target = _target_ref(first_start)
        conformance = await owner.wait(
            await owner.conformance(connection, target_ref=first_target)
        )
        _successful_task(conformance, "conformance")
        if conformance.result is None or conformance.result.kind != "conformance":
            raise RuntimeError("conformance result is unavailable")
        conformance_result = conformance.result.to_payload()["payload"]
        core_report = conformance_result["core_report"]
        first_after = await owner.target(first_target)
        health_after_conformance = await owner.health()
        if (
            not isinstance(core_report, dict)
            or core_report.get("ok") is not True
            or conformance_result["cleanup"]["complete"] is not True
            or conformance_result["target_cleanup"]["complete"] is not True
            or first_after.state != "stopped"
            or health_after_conformance.blockers
        ):
            raise RuntimeError("conformance did not leave confirmed full cleanup")

        second_start = await owner.wait(await owner.start_target(connection, secrets={}))
        second_target = _target_ref(second_start)
        if second_target.id == first_target.id:
            raise RuntimeError("evaluation reused the conformance target")

        accepted = await owner.start(
            prepared.digest,
            target_ref=second_target,
            secrets={},
        )
        operation_ref = ResourceRef("operation", accepted.id)
        operation = await _finished(owner, operation_ref)
        if (
            operation.control_state != "finished"
            or operation.result is None
            or operation.result.kind != "evaluation"
            or operation.result.core is None
            or operation.result.coverage is None
            or operation.result.coverage.expected != 3
            or operation.result.coverage.scored != 3
        ):
            raise RuntimeError("fixed evaluation did not produce complete measurement")
        report = await owner.report(operation_ref)
        if report.state != "available":
            raise RuntimeError("public evaluation report is unavailable")

        second_stop = await owner.wait(await owner.stop_target(second_target))
        _successful_task(second_stop, "target_stop")
        second_after = await owner.target(second_target)
        final_health = await owner.health()
        if second_after.state != "stopped" or final_health.blockers:
            raise RuntimeError("final target cleanup is not confirmed")

        # Full core reports, corpus rows, trajectories, and logs stay in the
        # private state tree. Print only the neutral lifecycle evidence.
        return {
            "schema": "lab-neutral-evaluation-example/1",
            "prepared": {
                "kind": prepared.kind,
                "case_count": len(prepared.evaluation.case_ids),
                "repeat_count": len(prepared.evaluation.repeat_seeds),
            },
            "conformance": {
                "state": conformance.state,
                "core_ok": core_report["ok"],
                "process": conformance_result["process"],
                "cleanup": conformance_result["cleanup"],
                "target_cleanup": conformance_result["target_cleanup"],
                "persistence": conformance.persistence.to_payload(),
                "target_state": first_after.state,
            },
            "evaluation": {
                "control_state": operation.control_state,
                "process": operation.result.process.to_payload(),
                "coverage": operation.result.coverage.to_payload(),
                "stop_reason": operation.result.stopped_reason,
                "target_cleanup": operation.target_cleanup.to_payload(),
                "persistence": operation.result.persistence.to_payload(),
                "results_state": operation.results_state,
                "report_state": report.state,
            },
            "final_target": {
                "state": second_after.state,
                "cleanup": second_after.cleanup.to_payload(),
            },
            "blocker_count": len(final_health.blockers),
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
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    state, bundle, prepared = _prepare(args)
    result = asyncio.run(_run(state, bundle, prepared))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
