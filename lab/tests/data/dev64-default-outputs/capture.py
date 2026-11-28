"""Capture from installed released dev64, importing only exact release test helpers.

release_tests must come from git archive v0.0.3.dev64 tests. Scratch must be empty
on tmpfs. The live Task0 algorithm comes from dev65 Task0 commit fc4b351b, with
its tests-root binding passed explicitly. Every imported helper and runtime
comes from released dev64. No fixture run or producing environment is modified.
Outputs refuse overwrite.
"""
import argparse
from importlib.metadata import version
from io import StringIO
import json
from pathlib import Path
import asyncio
import sys


# Task0 algorithm from fc4b351b; only tests-root binding is an explicit argument.
def capture_task0(scratch, destination, tests):
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


def capture(release_tests, scratch, destination):
    import evolution_lab
    assert version("evolution-lab") == "0.0.3.dev64"
    assert version("evo-skills") == "2.0.0b15"
    assert "site-packages" in evolution_lab.__file__
    assert scratch == Path("/tmp") or Path("/tmp") in scratch.parents
    sys.path.insert(0, str(release_tests))
    capture_task0(scratch, destination / "task0", release_tests)
    from dev63_default_outputs import read_outputs
    from evolution_lab.cli.output import write_value
    for name, fixture in {"task0": "b14/agent-finished", "focus-first": "b15/focus-first"}.items():
        outputs = read_outputs(release_tests / "fixtures/panel" / fixture / "run")
        for stem in ("campaign", "owner-report"):
            stream = StringIO()
            write_value(stream, outputs[stem + ".json"], machine=False)
            outputs[stem + ".txt"] = stream.getvalue()
        dest = destination / name
        dest.mkdir(parents=True, exist_ok=True)
        for filename, value in outputs.items():
            data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            path = dest / filename
            if path.exists():
                assert path.read_bytes() == data.encode(), path
            else:
                with path.open("x") as handle:
                    handle.write(data)
    print("Released dev64 Task0 + focus-first reader/CLI/panel outputs captured.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_tests", type=Path)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    capture(args.release_tests.resolve(), args.scratch.resolve(), args.destination.resolve())
