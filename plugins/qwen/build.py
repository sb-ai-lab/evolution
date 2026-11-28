"""Assemble an assistant extension from the Lab and Core guides in this checkout."""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_URL = "https://github.com/sb-ai-lab/evolution"
LINK = re.compile(r"(?<=\]\()([^\s)]+)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
SERVER_NAME = "evolution-lab-plugin"
MODULE_ARGS = ("-E", "-P", "-m", "evolution_lab.cli")
PROBE = """\
import json
from importlib.metadata import version

from evolution_lab.projects.workspace import mcp_available

available = mcp_available()
asking = []
if available:
    from evolution_lab.mcp.server import serve_stdio
    from evolution_lab.mcp.tools import PUBLISHED

    asking = [tool.name for tool in PUBLISHED if tool.asks]
print(json.dumps({"version": version("evolution-lab"), "mcp": available, "asking": asking}))
"""


@dataclass(frozen=True)
class LabConnection:
    interpreter: str
    socket: str
    autonomy: str
    version: str
    asking: tuple[str, ...]

    @property
    def cli(self) -> tuple[str, ...]:
        return (self.interpreter, *MODULE_ARGS)

    @property
    def service_cli(self) -> tuple[str, ...]:
        return (*self.cli, "--socket", self.socket)

    @property
    def server_args(self) -> list[str]:
        return [*MODULE_ARGS, "mcp", "--socket", self.socket, "--autonomy", self.autonomy]

    def instructions(self) -> str:
        if self.autonomy == "ask":
            asking = ", ".join(f"`{name}`" for name in self.asking)
            approval = (
                f"Use MCP to ask before these actions: {asking}. Before their CLI equivalents "
                "or a scenario step marked `human_only`, ask in the conversation and execute "
                "after an explicit yes. Give expected calls and the spending limit before a paid "
                "CLI action. For a client without forms, its own MCP approval rules apply."
            )
        else:
            approval = (
                "Run the requested Lab actions, including steps marked `human_only`. "
                "The client's own approval rules still apply."
            )
        return (
            "## Lab connection for this package\n\n"
            f"Autonomy is `{self.autonomy}`. {approval}\n\n"
            f"Use `{SERVER_NAME}` in the checkout or client profile selected for this package. "
            "If `.lab-workspace.json` is present or more than one Lab registration is visible, "
            "stop and direct the operator to the workspace instructions or a separate profile. "
            "Check the socket in the server instructions when the client exposes it. "
            f"Check `health` for Lab version `{self.version}` before proceeding. "
            "For CLI fallback, run `--json health` using the service prefix below, with "
            "`--json` before `health`. If the binding differs, the version differs or the chosen "
            "service is unavailable, show the problem and ask the operator to correct the "
            "configuration or rebuild.\n\n"
            "Use the autonomy above in place of generic approval guidance in these packaged "
            "skills and commands, including references to a workspace's `AGENTS.md` or a "
            "command's own approval rules. Follow the operator's request and the client's "
            "approval settings.\n\n"
            "This binding overrides service discovery, `--home` and `--socket` examples "
            "elsewhere in these skills and commands. Always use the socket-bound prefix "
            "for service commands, even if a default Lab service is running. "
            "Read a saved campaign using `view OPERATION` with this prefix. "
            "If unsure whether a command uses the service, look it up before running it.\n\n"
            f"```sh\n{shlex.join(self.service_cli)}\n```\n\n"
            "Use the interpreter-only prefix for these offline commands: `guides`, `scenarios`, "
            "`new-project DIR`, `evaluate DIR`, `campaign DIR` and `trace DIR` with a directory "
            "argument, and `exec-policy digest FILE`. Use each command's documented options. "
            "For `panel status` and `panel stop`, ask for the panel's explicit `--home`.\n\n"
            f"```sh\n{shlex.join(self.cli)}\n```\n\n"
        )


def _connection(
    repo: Path, interpreter: Path | None, socket: Path | None, autonomy: str | None, *, host: str
) -> LabConnection | None:
    if interpreter is None and socket is None and autonomy is None:
        return None
    if interpreter is None or socket is None:
        raise ValueError("--lab-python and --lab-socket must be supplied together")
    if not interpreter.is_absolute() or not socket.is_absolute():
        raise ValueError("--lab-python and --lab-socket must be absolute paths")
    for path in (interpreter, socket):
        value = str(path)
        if "$" in value or any(ord(character) < 32 for character in value):
            raise ValueError("Lab paths must contain no dollar signs or control characters")
        if host != "agent-plugins" and any(marker in value for marker in ("{{args}}", "!{", "@{")):
            raise ValueError("Lab paths must contain no slash-command template markers")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("--lab-python must name an executable file")
    mode = autonomy if autonomy is not None else "ask"
    if mode not in {"ask", "yolo"}:
        raise ValueError("--autonomy must be ask or yolo")
    expected = tomllib.loads((repo / "lab/pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    try:
        result = subprocess.run(
            [interpreter, "-E", "-P", "-c", PROBE],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        info = json.loads(result.stdout)
        version, mcp, asking = info["version"], info["mcp"], info["asking"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        raise ValueError(
            "Could not inspect the selected Lab installation with --lab-python"
        ) from error
    if version != expected:
        raise ValueError(f"The selected Lab version must match the plugin source ({expected})")
    if mcp is not True:
        raise ValueError("Install the Lab MCP extra in the selected Lab environment")
    if not isinstance(asking, list) or not all(
        isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in asking
    ):
        raise ValueError("The selected Lab installation returned invalid approval metadata")
    return LabConnection(str(interpreter), str(socket), mode, version, tuple(asking))


def _guide_layout(repo: Path) -> dict[Path, Path]:
    lab = repo / "lab/src/evolution_lab/guides"
    skills = [*sorted(lab.glob("*/SKILL.md")), *sorted(lab.glob("scenarios/*/SKILL.md"))]
    skills += [repo / "docs" / name / "SKILL.md" for name in ("core-campaigns", "evolving-skills")]
    layout = {}
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        header = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.S)
        match = re.search(r"^name: ([a-z0-9][a-z0-9-]*)$", header[1], re.M) if header else None
        if not match:
            raise ValueError(f"Invalid skill name in {skill}")
        target = Path("skills") / match[1]
        if target in layout.values():
            raise ValueError(f"Duplicate skill name in {skill}")
        layout[skill.parent] = target
    for name in ("owner", "service"):
        layout[lab / name] = Path("references/lab") / name
    return layout


def _copy_guides(repo: Path, bundle: Path) -> None:
    layout = _guide_layout(repo)
    selection = json.loads(
        (repo / "scripts/public_snapshot/selection.json").read_text(encoding="utf-8")
    )
    public_paths = set(selection["paths"])
    projections = selection["projections"]
    pending = deque(
        path
        for folder in layout
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".json"}
    )
    copied: set[Path] = set()

    def destination(source: Path) -> Path:
        for folder, target in layout.items():
            if source.is_relative_to(folder):
                return bundle / target / source.relative_to(folder)
        if source.is_relative_to(repo) and source.relative_to(repo).as_posix() in public_paths:
            return bundle / "references/core" / source.relative_to(repo)
        raise ValueError(f"Guide link leaves the packaged documentation: {source}")

    while pending:
        source = pending.popleft()
        if source in copied:
            continue
        copied.add(source)
        target = destination(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        relative = source.relative_to(repo).as_posix()
        contents = (repo / projections.get(relative, relative)).read_bytes()
        if source.suffix != ".md":
            target.write_bytes(contents)
            continue

        def rebase(match: re.Match[str], source: Path = source, target: Path = target) -> str:
            link = urlsplit(match[1])
            if link.scheme or link.netloc or not link.path:
                return match[1]
            path = (source.parent / unquote(link.path)).resolve()
            if path.is_dir():
                if not path.is_relative_to(repo):
                    raise ValueError(f"Guide link leaves the packaged documentation: {path}")
                relative = path.relative_to(repo).as_posix()
                readme = path / "README.md"
                if readme.relative_to(repo).as_posix() not in public_paths and any(
                    name.startswith(f"{relative}/") for name in public_paths
                ):
                    url = f"{PUBLIC_URL}/tree/main/{quote(relative, safe='/')}"
                    return f"{url}#{link.fragment}" if link.fragment else url
                path = readme
            if not path.is_file():
                raise ValueError(f"Unresolved link {match[1]} in {source}")
            pending.append(path)
            href = Path(os.path.relpath(destination(path), target.parent)).as_posix()
            return link._replace(path=quote(href, safe="/")).geturl()

        text = contents.decode("utf-8").replace("@PUBLIC_URL@", PUBLIC_URL)
        lines = []
        fence = ""
        for line in text.splitlines(keepends=True):
            marker = FENCE.match(line)
            if marker and not fence:
                fence = marker[1]
            elif marker and marker[1].startswith(fence):
                fence = ""
            lines.append(line if fence or marker else LINK.sub(rebase, line))
        target.write_text("".join(lines), encoding="utf-8")


def _add_instructions(path: Path, instructions: str, text: str) -> None:
    opening = "---\n"
    if not text.startswith(opening):
        raise ValueError(f"Missing Markdown front matter in {path}")
    header, end, body = text.removeprefix(opening).partition("\n---\n")
    if not end:
        raise ValueError(f"Missing Markdown front matter in {path}")
    body = body.lstrip("\n")
    path.write_text(f"{opening}{header}{end}\n{instructions}{body}", encoding="utf-8")


def _configure_package(bundle: Path, manifest: str, host: str, lab: LabConnection) -> None:
    launch = {"command": lab.interpreter, "args": lab.server_args}
    config = {"mcpServers": {SERVER_NAME: launch}}
    if host == "agent-plugins":
        launcher = bundle / "bin/lab-mcp"
        launcher.parent.mkdir()
        launcher.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        launch.update(
            type="stdio", command="./bin/lab-mcp", args=[lab.interpreter, *lab.server_args]
        )
        config["$schema"] = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        config_path = bundle / "mcp.json"
    elif host == "gigacode":
        config_path = bundle / "mcp-settings.json"
    else:
        config_path = bundle / manifest
        config = {**json.loads(config_path.read_text(encoding="utf-8")), **config}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    instructions = lab.instructions()
    for skill in sorted((bundle / "skills").glob("evolution-lab-*/SKILL.md")):
        _add_instructions(skill, instructions, skill.read_text(encoding="utf-8"))
    if host == "agent-plugins":
        return
    commands = bundle / "commands/evolution"
    for name, args in (("status", ["status"]), ("report", ["report", "--markdown"])):
        path = commands / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        command = shlex.join([*lab.service_cli, *args, "--", "OPERATION"])
        paragraph = (
            "Run the bound command below. Pass the operation ID as one literal argument in "
            "place of `OPERATION`.\n\n"
            f"```sh\n{command}\n```"
        )
        text, count = re.subn(
            r"(?m)^Run `lab [^\n]+$", lambda _match, replacement=paragraph: replacement, text
        )
        if count != 1:
            raise ValueError(f"Expected one Lab invocation in {path}")
        _add_instructions(path, instructions, text)
    help_command = commands / "help.md"
    _add_instructions(help_command, instructions, help_command.read_text(encoding="utf-8"))


def build_extension(
    repo: Path,
    output: Path,
    *,
    host: str = "qwen",
    lab_python: Path | None = None,
    lab_socket: Path | None = None,
    autonomy: str | None = None,
) -> None:
    """Write a complete extension to a fresh directory."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists: {output}")
    lab = _connection(repo, lab_python, lab_socket, autonomy, host=host)
    output.parent.mkdir(parents=True, exist_ok=True)
    plugins = repo / "plugins"
    portable = host == "agent-plugins"
    manifest = "plugin.json" if portable else f"{host}-extension.json"
    with TemporaryDirectory(prefix=f".{host}-build-", dir=output.parent) as scratch:
        bundle = Path(scratch) / "extension"
        bundle.mkdir()
        if portable:
            shutil.copyfile(repo / "LICENSE", bundle / "LICENSE")
        else:
            shutil.copytree(plugins / "qwen/commands", bundle / "commands")
        shutil.copyfile(plugins / host / manifest, bundle / manifest)
        _copy_guides(repo, bundle)
        if lab is not None:
            _configure_package(bundle, manifest, host, lab)
        bundle.rename(output)


def main(*, host: str = "qwen") -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for the assembled extension")
    parser.add_argument(
        "--lab-python", type=Path, help="absolute interpreter path for the selected Lab"
    )
    parser.add_argument("--lab-socket", type=Path, help="absolute socket path for the selected Lab")
    parser.add_argument(
        "--autonomy", choices=("ask", "yolo"), help="configured Lab approval mode (default: ask)"
    )
    args = parser.parse_args()
    try:
        build_extension(
            ROOT,
            args.output.absolute(),
            host=host,
            lab_python=args.lab_python,
            lab_socket=args.lab_socket,
            autonomy=args.autonomy,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(args.output.absolute())


if __name__ == "__main__":
    main()
