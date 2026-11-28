"""The assistant's project directory ``<home>/workspaces/<project>/`` (spec §3.11, D12).

Rebuilt whole for the current pair on every ``open``: generated files, packaged
skills and references with workspace-relative links, and symlinks to the working
copy and the materials root. Lab never writes into the working copy or the team repository.

Generated files: ``AGENTS.md``, ``CLAUDE.md``, ``.lab-workspace.json`` and, when the ``mcp``
extra is installed, the client registrations of the ``lab mcp`` server: ``.mcp.json``,
``.codex/config.toml``, ``.qwen/settings.json`` and ``.gigacode/settings.json``.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from evolution_lab import guides

from .models import Pair, Project

WORKSPACE_SCHEMA = "lab-workspace/1"
_ASSISTANT_DIRS = (".claude", ".agents")
_RELATIVE_GUIDE_LINK = re.compile(r"(?<=\]\()\.\.?/[^)#\s]+")
SERVER_NAME = "evolution-lab"
_MODULE_ARGS = ["-E", "-P", "-m", "evolution_lab.cli"]


@dataclass(frozen=True)
class Launcher:
    """How the running service's own Lab is started again: its interpreter, always with ``-E -P -m``."""

    interpreter: Path

    @classmethod
    def current(cls) -> "Launcher":
        return cls(Path(sys.executable).absolute())

    def server(self, socket_path: Path, autonomy: str = "ask") -> tuple[str, list[str]]:
        return str(self.interpreter), [*_MODULE_ARGS, "mcp", "--socket", str(socket_path), "--autonomy", autonomy]

    def cli(self, socket_path: Path) -> str:
        return shlex.join([str(self.interpreter), *_MODULE_ARGS, "--socket", str(socket_path)])


_FINAL_RELEASE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)*")


def mcp_available() -> bool:
    """Whether ``lab mcp`` can start here: a final release of ``mcp`` inside the extra's range ``>=2.3,<3``."""
    try:
        version = metadata.version("mcp")
    except metadata.PackageNotFoundError:
        return False
    match = _FINAL_RELEASE.fullmatch(version or "")
    return bool(match) and (2, 3) <= (int(match[1]), int(match[2])) < (3, 0)


def _toml_string(value: str) -> str:
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def client_files(command: str, args: list[str]) -> dict[str, str]:
    """Relative path -> exact text of the four client files registering the ``lab mcp`` server."""
    entry = {"command": command, "args": list(args)}
    as_json = json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, sort_keys=True) + "\n"
    toml = (f"[mcp_servers.{SERVER_NAME}]\ncommand = {_toml_string(command)}\n"
            f"args = [{', '.join(_toml_string(a) for a in args)}]\n")
    return {".mcp.json": as_json, ".codex/config.toml": toml,
            ".qwen/settings.json": as_json, ".gigacode/settings.json": as_json}


def claude_code_safe(command: str, args: list[str]) -> bool:
    """Claude Code expands ``${VAR}`` in ``.mcp.json`` command and args, so a ``$`` there cannot be written as is."""
    return not any("$" in part for part in (command, *args))


def _skill_dirs() -> list[tuple[str, Path]]:
    base = guides.root()
    found = [(info.name, base / Path(info.path).parent) for info in guides.list_guides()]
    for path in sorted((base / "scenarios").glob("*/SKILL.md")):
        found.append((guides._frontmatter(path.read_text(encoding="utf-8"))["name"], path.parent))
    return found


def _rebase_guide_links(document: Path, original: Path, locations: dict[Path, Path]) -> None:
    def rebase(match: re.Match[str]) -> str:
        target = Path(os.path.normpath(original.parent / match[0]))
        for source, destination in locations.items():
            if target.is_relative_to(source):
                return os.path.relpath(destination / target.relative_to(source), document.parent)
        return match[0]

    text = document.read_text(encoding="utf-8")
    rewritten = _RELATIVE_GUIDE_LINK.sub(rebase, text)
    if rewritten != text:
        document.write_text(rewritten, encoding="utf-8")


def _copy_guides(workspace: Path) -> None:
    paths = {source: Path("skills") / name for name, source in _skill_dirs()}
    # Keep supporting documentation outside the skill discovery directories.
    for name in ("owner", "service"):
        paths[guides.root() / name] = Path("guides") / name

    for assistant in _ASSISTANT_DIRS:
        locations = {source: workspace / assistant / path for source, path in paths.items()}
        for source, destination in locations.items():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for document in destination.rglob("*.md"):
                _rebase_guide_links(document, source / document.relative_to(destination), locations)


def _outside(project: Project) -> list:
    """Active implementations whose place is an absolute path outside the working copy (spec 2026-10-01 §3.5)."""
    working_copy = Path(os.path.normpath(project.working_copy.path))
    found = []
    for item in project.active_implementations():
        place = Path(os.path.normpath(item.place))
        if place.is_absolute() and place != working_copy and working_copy not in place.parents:
            found.append(item)
    return found



def _tools_section(socket_path: Path, reopen: str, claude_code: bool) -> list[str]:
    return [
        "## Lab tools",
        "",
        f"The MCP server `{SERVER_NAME}` is configured in this directory and uses the Lab service at "
        f"`{socket_path}`. Lab never approves or trusts the server. "
        "Codex loads it only after the human trusts this directory.",
        *([] if claude_code else [
            "",
            "Claude Code is not configured because the path contains `$` (Claude Code expands it); use the CLI "
            "fallback below.",
        ]),
        "",
        "- For reads, prefer the tools: `project_status` (argument `project`) for the scenario state, "
        "`operation_status` (argument `operation`), `operation_report_markdown`, `guide_read` (argument `name`).",
        "- If the tools are absent, the server fails to start, or a call fails with `service_unavailable`, use the "
        f"CLI commands in this file. Run `{reopen}` again if "
        "Lab was upgraded or moved.",
        "- After `project open` regenerates this directory, or after the service on this socket is restarted "
        f"or upgraded, the running server keeps what it read at startup. The human restarts the assistant "
        f"session here, or reconnects the `{SERVER_NAME}` server where the client offers it.",
        "",
    ]


def _notice_section(reopen: str) -> list[str]:
    return [
        "## Lab tools are not configured",
        "",
        "The `mcp` extra is not installed where the Lab service runs, so this directory has no Lab tools. Use "
        "the CLI commands in this file. To get the tools, the human installs the extra: in a Lab checkout run "
        "`just setup` again; for a wheel install reinstall the Lab wheel with `[service,mcp]` in the environment "
        "where the Lab service runs; then run "
        f"`{reopen}` again.",
        "",
    ]


def agents_text(project: Project, *, socket_path: Path, launcher: Launcher, with_mcp: bool,
                claude_code: bool = True, autonomy: str = "ask") -> str:
    lab = launcher.cli(socket_path)
    reopen = f"{lab} project open {project.id} --autonomy {autonomy}"
    status = f"{lab} project status {project.id}"
    catalogue = guides.scenarios()
    lines = [
        f"<!-- Generated by `lab project open {project.id}`. Manual edits are lost on the next open. -->",
        f"# Project {project.id} — {project.name}",
        "",
        "This directory is generated by Evolution Lab for an assistant working on this project.",
        f"- `work/` → the working copy `{project.working_copy.path}`; run git there.",
        f"- `materials/` → the materials root `{project.materials_root}` (evaluation, spec).",
        "",
        "## Implementations",
        "",
    ]
    linked = {item.id for item in _outside(project)}
    for item in project.implementations:
        if item.archived is None:
            line = f"- `{item.id}` — place `{item.place}`, package entry `{item.package_entry}`"
            if item.id in linked:
                line += f"; linked as `impl/{item.id}`"
            lines.append(line)
    lines += [
        "",
        "## Start here",
        "",
        *([f"Call the `project_status` tool with `project` = `{project.id}`. Without the tool, run:", ""]
          if with_mcp else []),
        f"`{status}`",
        "",
        "It lists every scenario step with its state and the missing facts.",
        "",
        *(_tools_section(socket_path, reopen, claude_code) if with_mcp else _notice_section(reopen)),
        "## Scenarios",
        "",
    ]
    for descriptor in catalogue.descriptors:
        line = f"- `{descriptor['id']}` — skill `evolution-lab-scenario-{descriptor['id']}`"
        human = [step["id"] for step in descriptor["steps"] if step.get("human_only", False)]
        if human:
            line += "; human-only steps: " + ", ".join(f"`{name}`" for name in human)
        lines.append(line)
    lines += [
        "",
        "## Rules",
        "",
        "- For any scenario step, first load the skill `evolution-lab-scenario-<id>`, then run "
        + (f"project status (the `project_status` tool, or `{status}`)." if with_mcp else f"`{status}`."),
        "- Act on the human's request for any Lab action.",
        (("- Autonomy is `ask`. Before a costly, irreversible or on-behalf action, or a step marked `human_only`, "
          + ("use the MCP tool so Lab can request approval through the client. For the CLI form, "
             if with_mcp else "use the CLI with the human's confirmation. To confirm, ")
          + "ask the human in the conversation and run the command after a yes. Give the expected calls and "
            "spending limit before a paid CLI action.")
         if autonomy == "ask" else
         "- Autonomy is `yolo`. Use the available MCP tools or CLI commands to run the requested Lab actions, "
         "including steps marked `human_only`. The client's own approvals still apply."),
        f"- Asking actions include `{lab} project accept` (commit), `{lab} project revoke-*`, "
        f"`{lab} project confirm`, `{lab} project start` and `{lab} resume`, "
        "applying a project sync, confirming a policy, deleting results, releasing and cleaning up resources.",
        "- Keep request files outside the working copy — in this project directory, next to AGENTS.md.",
        "- A step is done only when project status "
        + (f"(the `project_status` tool or `{status}`) " if with_mcp else f"(`{status}`) ")
        + "says so, never by a command's exit code.",
        "- Delivering changes to the team repository (push, merge request) is the human's action.",
        f"- Guides are skills named `evolution-lab-<topic>`; `{lab} project show {project.id}` shows the record.",
        "",
    ]
    return "\n".join(lines)


def build_workspace(home: Path, project: Project, *, pair: Pair, socket_path: Path, now: str,
                    launcher: Launcher | None = None, with_mcp: bool | None = None,
                    autonomy: str = "ask") -> Path:
    workspaces = Path(home) / "workspaces"
    for leftover in [*workspaces.glob(f".staging-{project.id}-*"), *workspaces.glob(f".old-{project.id}-*")]:
        shutil.rmtree(leftover, ignore_errors=True)
    final = workspaces / project.id
    staging = workspaces / f".staging-{project.id}-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o700)
    old = None
    try:
        launcher = launcher or Launcher.current()
        with_mcp = mcp_available() if with_mcp is None else with_mcp
        server = launcher.server(socket_path, autonomy)
        claude_code = claude_code_safe(*server)
        (staging / "AGENTS.md").write_text(
            agents_text(project, socket_path=socket_path, launcher=launcher, with_mcp=with_mcp,
                        claude_code=claude_code, autonomy=autonomy), encoding="utf-8")
        (staging / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        if with_mcp:
            for name, text in client_files(*server).items():
                if name == ".mcp.json" and not claude_code:
                    continue
                target = staging / name
                target.parent.mkdir(exist_ok=True)
                target.write_text(text, encoding="utf-8")
        _copy_guides(staging)
        working_copy = Path(project.working_copy.path)
        os.symlink(working_copy, staging / "work")
        os.symlink(working_copy / project.materials_root, staging / "materials")
        outside = _outside(project)
        if outside:
            (staging / "impl").mkdir(mode=0o700)
            for item in outside:
                os.symlink(Path(item.place), staging / "impl" / item.id)
        meta = {"schema": WORKSPACE_SCHEMA, "project": project.id, "pair": pair.to_payload(),
                "socket": str(socket_path), "generated_at": now,
                "catalogues": {"scenarios": guides.scenarios().list_payload()["catalogue"]["digest"],
                               "requirements": guides.requirements()["digest"]}}
        (staging / ".lab-workspace.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        if final.exists():
            old = workspaces / f".old-{project.id}-{secrets.token_hex(4)}"
            final.rename(old)
        staging.rename(final)
    except BaseException:
        # ``final.rename(old)`` may have already moved the previous workspace out of the way
        # before ``staging.rename(final)`` itself fails: restore it rather than leave the
        # project without a workspace at ``final`` and its predecessor stranded under
        # ``.old-*`` (fix round 1, finding 1). The restore itself is best-effort (fix round 2,
        # finding 1): if it also fails, ``old`` just stays under ``.old-*`` for inspection —
        # the error that surfaces is still the original failure, and ``staging`` is always
        # cleaned up either way.
        if old is not None and not final.exists():
            try:
                old.rename(final)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    return final
