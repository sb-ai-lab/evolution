"""Create and reopen durable preparation summaries without running a campaign."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evolution_lab.preparation import (
    CampaignSource,
    EnvironmentInput,
    PreparationStore,
    SourceRoot,
    describe_environment,
)


def _pairs(parser: argparse.ArgumentParser, values: list[str], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item or name in result:
            parser.error(f"{option} requires unique NAME=VALUE entries")
        result[name] = item
    return result


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--launch-template", type=Path, required=True)
    parser.add_argument("--launch-output", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--root", action="append", default=[], metavar="ALIAS=PATH", required=True)
    parser.add_argument("--execution-wheel", action="append", type=Path, default=[], required=True)
    parser.add_argument("--execution-runtime-file", action="append", type=Path, default=[])
    parser.add_argument("--target-python", type=Path)
    parser.add_argument("--target-wheel", action="append", type=Path, default=[])
    parser.add_argument("--target-runtime-file", action="append", type=Path, default=[])
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--secret-name", action="append", default=[])
    args = parser.parse_args()

    root_values = _pairs(parser, args.root, "--root")
    nonsecret_env = _pairs(parser, args.env, "--env")
    if len(set(args.secret_name)) != len(args.secret_name) or any(not name for name in args.secret_name):
        parser.error("--secret-name requires unique, nonempty names")
    if set(nonsecret_env) & set(args.secret_name):
        parser.error("a name cannot be both --env and --secret-name")
    if args.target_python is None and (args.target_wheel or args.target_runtime_file):
        parser.error("target wheels/runtime files require --target-python")
    if args.target_python is not None and not args.target_wheel:
        parser.error("--target-python requires at least one --target-wheel")

    roots = tuple(SourceRoot(alias, Path(path).absolute()) for alias, path in root_values.items())
    root_paths = tuple(root.source_path for root in roots)
    template_path = args.launch_template.absolute()
    launch_path = args.launch_output.absolute()
    if any(_inside(template_path, root) for root in root_paths):
        parser.error("--launch-template must be outside every --root")
    if sum(_inside(launch_path, root) for root in root_paths) != 1:
        parser.error("--launch-output must be inside exactly one --root")

    execution_input = EnvironmentInput(
        Path(sys.executable),
        tuple(path.absolute() for path in args.execution_wheel),
        tuple(path.absolute() for path in args.execution_runtime_file),
    )
    execution = describe_environment(execution_input)
    environment_inputs = [execution_input]
    target = execution
    if args.target_python is not None:
        target_input = EnvironmentInput(
            args.target_python.absolute(),
            tuple(path.absolute() for path in args.target_wheel),
            tuple(path.absolute() for path in args.target_runtime_file),
        )
        target = describe_environment(target_input)
        environment_inputs.append(target_input)

    launch = json.loads(template_path.read_text(encoding="utf-8"))
    if not isinstance(launch, dict) or not isinstance(launch.get("command"), dict):
        parser.error("--launch-template must contain a JSON object with command")
    launch["environment_ref"] = target.digest
    launch["command"]["executable"] = target.executable
    launch["env"] = nonsecret_env
    launch["secret_names"] = sorted(args.secret_name)
    try:
        with launch_path.open("x", encoding="utf-8") as output:
            json.dump(launch, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
    except FileExistsError:
        parser.error("--launch-output already exists; choose a new explicit path")

    store = PreparationStore(args.state_directory.absolute())
    bundle = store.create_bundle(
        source=CampaignSource(args.manifest.absolute(), launch_path),
        environments=tuple(environment_inputs),
        env=nonsecret_env,
        secret_names=tuple(args.secret_name),
        roots=roots,
    )
    prepared = store.prepare_campaign(bundle.digest)

    reopened = PreparationStore(args.state_directory.absolute())
    payload = {
        "described_environments": [
            {"digest": descriptor.digest, "descriptor": descriptor.to_payload()}
            for descriptor in ([execution] if target is execution else [execution, target])
        ],
        "bundle": bundle.to_payload(),
        "prepared": prepared.to_payload(),
        "reopened_bundle": reopened.get_bundle(bundle.digest).to_payload(),
        "reopened_prepared": reopened.get_prepared(prepared.digest).to_payload(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
