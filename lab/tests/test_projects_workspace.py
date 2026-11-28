import json
import os
import re
import shlex
import stat
from pathlib import Path

import pytest

from evolution_lab import guides
from evolution_lab.cli.errors import UsageError
from evolution_lab.cli.parser import build_parser
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.workspace import SERVER_NAME, Launcher, agents_text, build_workspace

PAIR = Pair("0.0.3.dev28", "0.3.0a17")


@pytest.fixture
def project(tmp_path):
    tmp_path.chmod(0o700)
    wc = tmp_path / "wc"
    (wc / "lab").mkdir(parents=True)
    (wc / "agent").mkdir()
    home = tmp_path / "home"
    with Projects.open(home) as projects:
        yield home, projects.create(id="acq", name="Acq", path=str(wc), materials_root="lab",
                                    implementations=[{"id": "agent", "place": "agent"}]), wc


def _skill_names():
    names = [g.name for g in guides.list_guides()]
    base = guides.root() / "scenarios"
    names += [guides._frontmatter((p).read_text())["name"] for p in sorted(base.glob("*/SKILL.md"))]
    return sorted(names)


def test_layout(project):
    home, item, wc = project
    path = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t")
    assert path == home / "workspaces" / "acq"
    assert (path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert os.readlink(path / "work") == str(wc)
    assert os.readlink(path / "materials") == str(wc / "lab")
    for tree in (".claude/skills", ".agents/skills"):
        assert sorted(p.name for p in (path / tree).iterdir()) == _skill_names()
        assert (path / tree / "evolution-lab-scenario-connect" / "scenario.json").is_file()
    meta = json.loads((path / ".lab-workspace.json").read_text())
    assert meta["schema"] == "lab-workspace/1" and meta["project"] == "acq"
    assert meta["pair"] == {"lab": "0.0.3.dev28", "core": "0.3.0a17"}
    assert meta["socket"] == str(home / "service.sock") and meta["generated_at"] == "t"
    assert meta["catalogues"]["scenarios"] == guides.scenarios().list_payload()["catalogue"]["digest"]
    assert meta["catalogues"]["requirements"] == guides.requirements()["digest"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_workspace_markdown_links_resolve(project):
    home, item, _ = project
    workspace = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t")
    for assistant in (workspace.resolve() / ".claude", workspace.resolve() / ".agents"):
        targets = set()
        for document in assistant.rglob("*.md"):
            for link in re.findall(r"\]\((\.\.?/[^)\s]+)\)", document.read_text(encoding="utf-8")):
                relative_path, _, fragment = link.partition("#")
                target = (document.parent / relative_path).resolve()
                assert target.is_relative_to(assistant) and target.is_file(), (document, link)
                targets.add((target.relative_to(assistant).as_posix(), fragment))
        assert ("skills/evolution-lab-pitfalls/SKILL.md", "gateway-accounting") in targets
        assert ("skills/evolution-lab-scenario-connect/SKILL.md", "check") in targets
        assert any(path.endswith("/service/README.md") and fragment == "bundle-measurements"
                   for path, fragment in targets)


PY = Path("/opt/lab venv/bin/python")
LAUNCHERS = (Launcher(PY),)
SOCKETS = (Path("/h/service.sock"), Path('/tmp/so cket "q"\\x/service.sock'))


def _text(item, launcher, socket_path=Path("/h/service.sock"), with_mcp=True, **options):
    return agents_text(item, socket_path=socket_path, launcher=launcher, with_mcp=with_mcp, **options)


def _commands(text, prefix):
    """Every backticked span that starts with the CLI prefix: each command the file proposes."""
    return [span for span in re.findall(r"`([^`]+)`", text) if span.startswith(prefix + " ")]


@pytest.mark.parametrize("launcher", LAUNCHERS)
@pytest.mark.parametrize("socket_path", SOCKETS)
@pytest.mark.parametrize("with_mcp", (True, False))
def test_every_command_is_absolute_and_names_the_socket(project, launcher, socket_path, with_mcp):
    _, item, _ = project
    text = _text(item, launcher, socket_path, with_mcp)
    prefix = launcher.cli(socket_path)
    assert prefix == f"{shlex.quote(str(PY))} -E -P -m evolution_lab.cli --socket {shlex.quote(str(socket_path))}"
    commands = _commands(text, prefix)
    assert len(commands) >= 6
    assert {shlex.split(c)[len(shlex.split(prefix))] for c in commands} >= {"project", "resume"}
    parser = build_parser()
    module = [str(PY), "-E", "-P", "-m", "evolution_lab.cli"]
    for command in commands:
        words = shlex.split(command)
        assert words[:len(module)] == module
        rest = words[len(module):]
        assert rest[0] == "--socket" and rest[1] == str(socket_path)
        if rest[-1].endswith("-*"):  # a family written as `revoke-*`: parse a real member
            continue
        try:
            parser.parse_args(rest)
        except UsageError as error:  # a bare `project accept` lacks its arguments; an unknown command is another error
            assert "are required" in str(error), (command, str(error))
    for bare in ("`lab project status", "`lab project accept", "`lab project show", "`lab resume", "`lab --socket"):
        assert bare not in text
    assert f"`{prefix} project accept`" in text and f"`{prefix} resume`" in text
    assert f"then run " in text and f"`{prefix} project status acq`" in text.split("first load the skill", 1)[1].splitlines()[0]


def test_tools_section_with_the_extra(project):
    _, item, _ = project
    text = _text(item, LAUNCHERS[0])
    for phrase in (SERVER_NAME, "/h/service.sock", "project_status", "operation_status",
                   "operation_report_markdown", "guide_read", "`project` = `acq`", "Lab never approves or trusts the server",
                   "Codex loads it only after the human trusts this directory", "restarts the assistant session",
                   "reconnects"):
        assert phrase in text
    assert "not configured" not in text
    assert text.index("Call the `project_status` tool") < text.index(LAUNCHERS[0].cli(Path("/h/service.sock")))


def test_no_tools_section_without_the_extra(project):
    _, item, _ = project
    text = _text(item, LAUNCHERS[0], with_mcp=False)
    assert f"`{SERVER_NAME}`" not in text and "project_status" not in text
    assert "Lab tools are not configured" in text and "`just setup`" in text and "`[service,mcp]`" in text


def test_named_tools_are_published(project):
    pytest.importorskip("mcp")
    from evolution_lab.mcp.tools import PUBLISHED
    published = {tool.name: tool for tool in PUBLISHED}
    _, item, _ = project
    text = _text(item, LAUNCHERS[0])
    named = {name for name in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text) if name in published or name.startswith(
        ("project_", "operation_", "guide_"))}
    assert {"project_status", "operation_status", "operation_report_markdown", "guide_read"} <= named
    assert named <= set(published)
    for tool, argument in (("project_status", "project"), ("operation_status", "operation"), ("guide_read", "name")):
        assert argument in published[tool].input_schema["properties"]
        assert f"`{tool}` (argument `{argument}`)" in text or tool == "operation_report_markdown"


@pytest.mark.parametrize("with_mcp", (True, False))
@pytest.mark.parametrize("autonomy", ("ask", "yolo"))
def test_workspace_rules_follow_selected_autonomy(project, with_mcp, autonomy):
    _, item, _ = project
    launcher = LAUNCHERS[0]
    text = _text(item, launcher, with_mcp=with_mcp, autonomy=autonomy)
    cli = launcher.cli(Path("/h/service.sock"))
    for command in ("project accept", "project revoke-*", "project confirm", "project start", "resume"):
        assert f"`{cli} {command}`" in text
    assert "only the human runs them" not in text and "never by the assistant" not in text
    assert f"Autonomy is `{autonomy}`" in text
    if autonomy == "ask":
        assert "ask the human in the conversation" in text and "run the command after a yes" in text
        if with_mcp:
            assert "use the MCP tool" in text
    else:
        assert "run the requested Lab actions" in text
        assert "ask the human in the conversation" not in text
    assert "Delivering changes to the team repository (push, merge request) is the human's action." in text


def test_reopen_replaces_and_never_touches_working_copy(project):
    home, item, wc = project
    before = sorted((p.relative_to(wc).as_posix(), p.lstat().st_mtime_ns) for p in wc.rglob("*"))
    path = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t1")
    (path / "notes.md").write_text("manual")
    build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t2")
    assert not (path / "notes.md").exists()
    assert json.loads((path / ".lab-workspace.json").read_text())["generated_at"] == "t2"
    assert sorted(p.name for p in (home / "workspaces").iterdir()) == ["acq"]
    assert sorted((p.relative_to(wc).as_posix(), p.lstat().st_mtime_ns) for p in wc.rglob("*")) == before


def test_failed_reopen_restores_the_previous_workspace(project, monkeypatch):
    home, item, wc = project
    path = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t1")
    real_rename = Path.rename

    def failing_rename(self, target):
        if self.name.startswith(f".staging-{item.id}-"):
            raise OSError("simulated staging -> final rename failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError):
        build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t2")
    assert path.is_dir()
    assert json.loads((path / ".lab-workspace.json").read_text())["generated_at"] == "t1"
    assert sorted(p.name for p in (home / "workspaces").iterdir()) == ["acq"]
    assert not list((home / "workspaces").glob(".staging-*"))


def test_failed_reopen_with_a_failed_restore_still_raises_the_original_error(project, monkeypatch):
    home, item, wc = project
    build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t1")
    real_rename = Path.rename

    def failing_rename(self, target):
        if self.name.startswith(f".staging-{item.id}-"):
            raise OSError("simulated staging -> final rename failure")
        if self.name.startswith(f".old-{item.id}-"):
            raise OSError("simulated old -> final restore failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError, match="staging -> final"):
        build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t2")
    assert not list((home / "workspaces").glob(".staging-*"))
    leftover = list((home / "workspaces").glob(".old-*"))
    assert len(leftover) == 1  # the restore failed, so the previous workspace stays under .old-*
    assert not (home / "workspaces" / "acq").exists()


@pytest.fixture
def project_open(tmp_path):
    tmp_path.chmod(0o700)
    wc = tmp_path / "wc"
    (wc / "lab").mkdir(parents=True)
    (wc / "agent").mkdir()
    home = tmp_path / "home"
    with Projects.open(home) as projects:
        item = projects.create(id="acq", name="Acq", path=str(wc), materials_root="lab",
                               implementations=[{"id": "agent", "place": "agent"}])
        yield home, projects, item, wc


@pytest.mark.parametrize("autonomy", ("ask", "yolo"))
async def test_session_open_workspace_payload(project_open, autonomy):
    from evolution_lab.projects.session import ProjectSession
    home, projects, item, wc = project_open
    session = ProjectSession(projects, preparation=None, owner=None, pair=PAIR, state_root=home.parent / "state",
                             socket_path=home / "service.sock")
    payload = await session.open_workspace("acq", autonomy=autonomy)
    assert payload == {"schema": "lab-project-workspace/1", "path": str(home / "workspaces" / "acq")}

    path = Path(payload["path"])
    assert f"Autonomy is `{autonomy}`" in (path / "AGENTS.md").read_text()
    meta = json.loads((path / ".lab-workspace.json").read_text())
    assert meta["schema"] == "lab-workspace/1"
    assert set(meta) == {"schema", "project", "pair", "socket", "generated_at", "catalogues"}


async def test_open_archived_refused(project_open):
    from evolution_lab.projects.session import ProjectSession
    home, projects, item, wc = project_open
    projects.update("acq", {"op": "archive", "reason": None}, active=lambda *_: None)
    session = ProjectSession(projects, preparation=None, owner=None, pair=PAIR, state_root=home.parent / "state",
                             socket_path=home / "service.sock")
    with pytest.raises(ProjectError) as error:
        await session.open_workspace("acq")
    assert error.value.code == "archived"
    assert not (home / "workspaces" / "acq").exists()


def test_agents_rules_w53_w54(project):
    home, item, _wc = project
    path = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t")
    text = (path / "AGENTS.md").read_text(encoding="utf-8").lower()
    for phrase in (" project accept`", " project revoke-", " project confirm`",
                   "Autonomy is `ask`".lower(), "outside the working copy",
                   "a step is done only when project status",
                   "load the skill `evolution-lab-scenario-<id>`"):
        assert phrase in text


def test_an_implementation_outside_the_working_copy_is_linked(tmp_path):
    tmp_path.chmod(0o700)
    wc = tmp_path / "wc"
    (wc / "lab").mkdir(parents=True)
    (wc / "agent").mkdir()
    port = tmp_path / "home" / "ports" / "acq" / "agent-port"
    inside = wc / "inner"
    home = tmp_path / "home"
    with Projects.open(home) as projects:
        projects.create(id="acq", name="Acq", path=str(wc), materials_root="lab",
                        implementations=[{"id": "agent", "place": "agent"},
                                         {"id": "agent-port", "place": str(port),
                                          "origin": {"kind": "ported_from", "implementation": "agent"}},
                                         {"id": "inner", "place": str(inside)},
                                         {"id": "old", "place": str(tmp_path / "old")}])
        item = projects.update("acq", {"op": "archive_implementation", "implementation": "old", "reason": None})
    port.mkdir(parents=True)
    path = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t")
    assert sorted(p.name for p in (path / "impl").iterdir()) == ["agent-port"]
    assert os.readlink(path / "impl" / "agent-port") == str(port)
    text = (path / "AGENTS.md").read_text(encoding="utf-8")
    assert f"- `agent-port` — place `{port}`, package entry `None`; linked as `impl/agent-port`" in text
    assert "- `agent` — place `agent`, package entry `None`" in text


def test_no_impl_directory_without_an_outside_implementation(project):
    home, item, _wc = project
    path = build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t")
    assert not (path / "impl").exists()


def test_agents_md_names_human_only_steps(project):
    home, item, _wc = project
    text = agents_text(item, socket_path=home / "service.sock", launcher=LAUNCHERS[0], with_mcp=True)
    assert "- `connect` — skill `evolution-lab-scenario-connect`; human-only steps: `baseline`" in text
    assert "- `evolve` — skill `evolution-lab-scenario-evolve`; human-only steps: `run`" in text
    assert ("- `restructure` — skill `evolution-lab-scenario-restructure`; human-only steps: `analysis-confirm`, "
            "`brief-confirm`, `accept`") in text
