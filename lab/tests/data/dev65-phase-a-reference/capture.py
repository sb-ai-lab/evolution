"""Capture no-policy outputs with the immutable starting-master installation.

Run with the isolated baseline interpreter, not source imports. Scratch must be
an empty directory on /tmp; the caller owns its cleanup. Never overwrite captures.
"""
import argparse
import asyncio
from io import StringIO
import json
from pathlib import Path
import sys


def capture(scratch, destination):
    tests = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(tests))
    from dev62_default_outputs import read_outputs
    from test_owner_cleanup import finished
    from test_owner_target import make_bundle, ready_target
    from scaffold_16 import scaffold_16
    from evolution_lab.cli.output import write_value
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.preparation import PreparationStore

    state, bundle, _ = make_bundle(scratch, scaffold=scaffold_16)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            target_status = (await owner.target(target)).to_payload()
            operation = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef("operation", operation.id)
            status = await finished(owner, ref, seconds=120)
            assert status.control_state == "finished", status.to_payload()
            assert status.result is not None and status.result.core is not None
            outputs = {
                "target.json": target_status,
                "operation.json": status.to_payload(),
                "live-owner-report.json": (await owner.report(ref)).to_payload(),
            }
            await owner.wait(await owner.stop_target(target))
            return outputs

    outputs = asyncio.run(scenario())
    # Reuse a committed genuine Core capture so later readers can reproduce
    # campaign/report parity without creating or mutating any producing state.
    outputs.update(read_outputs(tests / "fixtures/panel/b14/agent-finished/run"))
    # Normalize only machine paths and live Owner identifiers. Core fixture
    # projections and report.md are retained exactly as the reader returns them.
    target = outputs["target.json"]
    operation = outputs["operation.json"]
    substitutions = {
        str(scratch): "<scratch>",
        str(Path(sys.executable).parent.parent): "<baseline-venv>",
        target["id"]: "target-reference",
        operation["id"]: "operation-reference",
        target["owner_generation"]: "owner-reference",
        target["base_url"]: "http://127.0.0.1:12345",
        target["boot_id"]: "boot-reference",
    }

    def normalized(value):
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if isinstance(value, str):
            for old, new in substitutions.items():
                if old:
                    value = value.replace(old, new)
        return value

    for name in ("target.json", "operation.json", "live-owner-report.json"):
        outputs[name] = normalized(outputs[name])
    for name in ("target", "operation", "campaign", "owner-report", "live-owner-report"):
        stream = StringIO()
        write_value(stream, outputs[name + ".json"], machine=False)
        outputs[name + ".txt"] = stream.getvalue()
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in outputs.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        with (destination / name).open("x") as handle:
            handle.write(text)
    print(f"Captured {len(outputs)} outputs; live target/campaign and immutable fixture reader.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    capture(args.scratch.resolve(), args.destination.resolve())
