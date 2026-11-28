import json
import tomllib
from pathlib import Path

import pytest

from evolution_lab.projects.models import Pair
from evolution_lab.projects.registry import Projects
from evolution_lab.projects import workspace as workspace_module
from evolution_lab.projects.workspace import Launcher, build_workspace, client_files, mcp_available

PAIR = Pair("0.0.3.dev28", "0.3.0a17")
L = Launcher(interpreter=Path("/opt/lab venv/bin/python"))
SOCK = Path('/tmp/a "b"\\c/service.sock')
PATHS = (".mcp.json", ".codex/config.toml", ".qwen/settings.json", ".gigacode/settings.json")


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


def _build(project, now="t", **kw):
    home, item, _ = project
    return build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now=now,
                           launcher=L, **kw)


def test_server_entry_is_the_service_interpreter_with_explicit_socket():
    assert L.server(SOCK) == ("/opt/lab venv/bin/python",
                              ["-E", "-P", "-m", "evolution_lab.cli", "mcp", "--socket", str(SOCK), "--autonomy", "ask"])


def test_cli_prefix_is_shell_quoted():
    assert L.cli(Path("/a b/s.sock")) == "'/opt/lab venv/bin/python' -E -P -m evolution_lab.cli --socket '/a b/s.sock'"


def test_client_files_round_trip():
    files = client_files(*L.server(SOCK))
    assert set(files) == set(PATHS)
    entry = {"command": "/opt/lab venv/bin/python", "args": L.server(SOCK)[1]}
    assert json.loads(files[".mcp.json"]) == {"mcpServers": {"evolution-lab": entry}}
    assert tomllib.loads(files[".codex/config.toml"]) == {"mcp_servers": {"evolution-lab": entry}}
    for name in (".qwen/settings.json", ".gigacode/settings.json"):
        assert json.loads(files[name]) == {"mcpServers": {"evolution-lab": entry}}
    assert all(text.endswith("\n") for text in files.values())


def test_toml_escapes_control_characters():
    files = client_files("/p\t\x01", ["a\nb", "\\", '"'])
    entry = tomllib.loads(files[".codex/config.toml"])["mcp_servers"]["evolution-lab"]
    assert entry == {"command": "/p\t\x01", "args": ["a\nb", "\\", '"']}


@pytest.mark.parametrize("options, level", [({}, "ask"), ({"autonomy": "yolo"}, "yolo")])
def test_build_writes_client_files_with_the_extra(project, options, level):
    home = project[0]
    workspace = _build(project, with_mcp=True, **options)
    entry = {"command": str(L.interpreter),
             "args": ["-E", "-P", "-m", "evolution_lab.cli", "mcp", "--socket", str(home / "service.sock"),
                      "--autonomy", level]}
    for name in PATHS:
        contents = (workspace / name).read_text(encoding="utf-8")
        if name.endswith(".toml"):
            assert tomllib.loads(contents) == {"mcp_servers": {"evolution-lab": entry}}
        else:
            assert json.loads(contents) == {"mcpServers": {"evolution-lab": entry}}


def test_build_writes_no_client_files_without_the_extra(project):
    workspace = _build(project, with_mcp=False)
    for name in (*PATHS, ".codex", ".qwen", ".gigacode"):
        assert not (workspace / name).exists()


def test_rebuild_is_byte_identical(project):
    workspace = _build(project, now="one", with_mcp=True)
    first = {name: (workspace / name).read_bytes() for name in PATHS}
    workspace = _build(project, now="two", with_mcp=True)
    assert {name: (workspace / name).read_bytes() for name in PATHS} == first


def test_reopen_without_the_extra_drops_old_client_files(project):
    workspace = _build(project, with_mcp=True)
    assert (workspace / ".mcp.json").exists()
    workspace = _build(project, with_mcp=False)
    assert not any((workspace / name).exists() for name in PATHS)


def test_no_trust_or_context_settings(project):
    for text in client_files(*L.server(SOCK)).values():
        for word in ("trust", "enableAllProjectMcpServers", "fileName"):
            assert word not in text


def test_current_launcher_keeps_the_symlinked_interpreter(tmp_path, monkeypatch):
    real = tmp_path / "real" / "python3"
    real.parent.mkdir()
    real.write_text("")
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    link = bin_dir / "python"
    link.symlink_to(real)
    monkeypatch.setattr("sys.executable", str(link))
    assert Launcher.current() == Launcher(link)
    (bin_dir / "lab").write_text(f"#!{link}\nprint('x')\n")
    assert Launcher.current() == Launcher(link)
    assert Launcher.current().cli(Path("/s.sock")).startswith(f"{link} -E -P -m evolution_lab.cli --socket")


@pytest.mark.parametrize("version, expected", [
    ("1.9.0", False), ("2.2.9", False), ("2.3.0", True), ("2.10.1", True), ("2.3.0rc1", False), ("2.4.0.dev1", False), ("2.3.0.post1", False),
    ("2.3.0+local", False), ("2.3", True),
    ("3.0.0", False), ("garbage", False), ("", False)])
def test_mcp_available_requires_the_extra_version_range(monkeypatch, version, expected):
    monkeypatch.setattr(workspace_module.metadata, "version", lambda name: version)
    assert mcp_available() is expected


def test_mcp_available_is_false_without_the_distribution(monkeypatch):
    def missing(name):
        raise workspace_module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(workspace_module.metadata, "version", missing)
    assert mcp_available() is False


def _build_default(project, available, monkeypatch):
    monkeypatch.setattr(workspace_module, "mcp_available", lambda: available)
    home, item, _ = project
    return build_workspace(home, item, pair=PAIR, socket_path=home / "service.sock", now="t")


def test_default_build_without_the_extra_writes_the_notice(project, monkeypatch):
    workspace = _build_default(project, False, monkeypatch)
    assert not any((workspace / name).exists() for name in PATHS)
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Lab tools are not configured" in text and "## Lab tools\n" not in text


def test_default_build_with_the_extra_writes_files_and_section(project, monkeypatch):
    workspace = _build_default(project, True, monkeypatch)
    assert all((workspace / name).is_file() for name in PATHS)
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Lab tools\n" in text and "not configured" not in text


@pytest.mark.parametrize("with_mcp", [True, False])
def test_agents_md_has_no_bare_runnable_lab_command(project, with_mcp):
    import re
    text = (_build(project, with_mcp=with_mcp) / "AGENTS.md").read_text(encoding="utf-8")
    body = text.split("\n", 1)[1]
    assert not re.search(r"`lab (project|serve|resume)", body)


CLAUDE_NOTE = "Claude Code is not configured because the path contains `$`"


def test_dollar_in_server_command_skips_only_the_claude_file(project):
    home, item, _ = project
    socket_path = home / "${X}" / "service.sock"
    workspace = build_workspace(home, item, pair=PAIR, socket_path=socket_path, now="t", launcher=L, with_mcp=True)
    assert not (workspace / ".mcp.json").exists()
    for name in PATHS[1:]:
        assert (workspace / name).is_file()
    assert CLAUDE_NOTE in (workspace / "AGENTS.md").read_text(encoding="utf-8")


def test_dollar_in_interpreter_skips_the_claude_file(project):
    home, item, _ = project
    launcher = Launcher(interpreter=Path("/opt/$HOME/python"))
    workspace = build_workspace(home, item, pair=PAIR, socket_path=home / "s.sock", now="t",
                                launcher=launcher, with_mcp=True)
    assert not (workspace / ".mcp.json").exists()


def test_plain_path_writes_the_claude_file_and_no_note(project):
    workspace = _build(project, with_mcp=True)
    assert (workspace / ".mcp.json").is_file()
    assert CLAUDE_NOTE not in (workspace / "AGENTS.md").read_text(encoding="utf-8")
