import importlib
import json
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = "evolution-lab-plugin"


def _probe_python(tmp_path, *, mcp=True, version=None):
    interpreter = tmp_path / "selected Lab=python"
    if version is None:
        version = tomllib.loads((ROOT / "lab/pyproject.toml").read_text())["project"]["version"]
    result = {
        "version": version,
        "mcp": mcp,
        "asking": ["operation_start", "project_accept_commit"],
    }
    interpreter.write_text(
        "#!/bin/sh\n"
        'test "$1" = -E && test "$2" = -P && test "$3" = -c || exit 2\n'
        f"printf '%s\\n' {shlex.quote(json.dumps(result))}\n"
    )
    interpreter.chmod(0o700)
    return interpreter


def test_build_qwen_plugin_from_lab_and_core_guides(tmp_path):
    build_extension = importlib.import_module("plugins.qwen.build").build_extension
    output = tmp_path / "evolution"
    build_extension(ROOT, output)

    manifest = json.loads((output / "qwen-extension.json").read_text())
    assert manifest["name"] == "evolution"
    assert "mcpServers" not in manifest
    commands = output / manifest["commands"] / "evolution"
    assert {path.stem for path in commands.glob("*.md")} >= {"help", "report", "status"}

    skills = output / manifest["skills"]
    first = skills / "evolution-lab-first-evaluation/SKILL.md"
    connect = skills / "evolution-lab-scenario-connect"
    assert "../evolution-lab-target-connector/SKILL.md#neutral-contract" in first.read_text()
    assert (connect / "schemas/spec-answers-1.json").is_file()
    assert "../evolution-lab-first-evaluation/SKILL.md" in (connect / "SKILL.md").read_text()
    assert (skills / "evolution-lab-campaign-operations/SKILL.md").is_file()
    campaign_package = skills / "evolution-lab-campaign-package/SKILL.md"
    assert (
        "https://github.com/sb-ai-lab/evolution/blob/main/docs/manifest-v2.md#focus-first-attempts"
        in campaign_package.read_text()
    )
    assert (skills / "evolving-skills/reference.md").is_file()
    assert (
        "../../references/core/docs/cli.md#in-place-effects-on-family-7"
        in (skills / "core-campaigns/SKILL.md").read_text()
    )
    assert (output / "references/core/docs/cli.md").is_file()
    assert (output / "references/lab/service/README.md").is_file()
    assert not (output / "references/core/docs/development").exists()

    with pytest.raises(FileExistsError, match="already exists"):
        build_extension(ROOT, output)
    assert (output / "qwen-extension.json").is_file()


def test_build_gigacode_plugin_reuses_qwen_content(tmp_path):
    build_extension = importlib.import_module("plugins.qwen.build").build_extension
    qwen = tmp_path / "qwen"
    gigacode = tmp_path / "gigacode"
    build_extension(ROOT, qwen)
    build_extension(ROOT, gigacode, host="gigacode")

    manifest = json.loads((gigacode / "gigacode-extension.json").read_text())
    assert manifest["name"] == "evolution"
    assert manifest["skills"] == "skills"
    assert manifest["commands"] == "commands"
    assert not (gigacode / "qwen-extension.json").exists()

    qwen_files = {path.relative_to(qwen) for path in qwen.rglob("*") if path.is_file()}
    qwen_files.remove(Path("qwen-extension.json"))
    for relative in qwen_files:
        assert (gigacode / relative).read_bytes() == (qwen / relative).read_bytes(), relative


def test_build_portable_agent_plugin(tmp_path):
    build_extension = importlib.import_module("plugins.qwen.build").build_extension
    output = tmp_path / "evolution"
    build_extension(ROOT, output, host="agent-plugins")

    manifest = json.loads((output / "plugin.json").read_text())
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert manifest["name"] == "evolution"
    assert "skills" not in manifest and "commands" not in manifest
    assert not (output / "mcp.json").exists()
    assert not (output / "commands").exists()
    assert (output / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()

    skills = output / "skills"
    first = skills / "evolution-lab-first-evaluation/SKILL.md"
    assert "../evolution-lab-target-connector/SKILL.md#neutral-contract" in first.read_text()
    assert (skills / "core-campaigns/SKILL.md").is_file()
    assert (output / "references/core/docs/cli.md").is_file()


@pytest.mark.parametrize(
    ("host", "manifest"),
    [("gigacode", "gigacode-extension.json"), ("agent-plugins", "plugin.json")],
)
def test_build_entry_point_with_an_installed_plugins_package(tmp_path, monkeypatch, host, manifest):
    installed = tmp_path / "site-packages" / "plugins"
    installed.mkdir(parents=True)
    (installed / "__init__.py").touch()
    monkeypatch.setenv("PYTHONPATH", str(installed.parent))

    output = tmp_path / "evolution"
    result = subprocess.run(
        [sys.executable, ROOT / "plugins" / host / "build.py", output],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((output / manifest).read_text())["name"] == "evolution"


@pytest.mark.parametrize(
    ("host", "configuration"),
    [
        ("qwen", "qwen-extension.json"),
        ("agent-plugins", "mcp.json"),
        ("gigacode", "mcp-settings.json"),
    ],
)
@pytest.mark.parametrize("autonomy", [None, "yolo"], ids=["default-ask", "explicit-yolo"])
def test_configured_plugin_binds_both_entry_points(tmp_path, host, configuration, autonomy):
    output = tmp_path / "plugin"
    interpreter = _probe_python(tmp_path)
    socket = tmp_path / "other service.sock"
    args = ["--lab-python", str(interpreter), "--lab-socket", str(socket)]
    if autonomy:
        args += ["--autonomy", autonomy]
    result = subprocess.run(
        [sys.executable, ROOT / "plugins" / host / "build.py", output, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads((output / configuration).read_text())
    assert set(config["mcpServers"]) == {SERVER}
    entry = config["mcpServers"][SERVER]
    mode = autonomy or "ask"
    expected = [
        "-E",
        "-P",
        "-m",
        "evolution_lab.cli",
        "mcp",
        "--socket",
        str(socket),
        "--autonomy",
        mode,
    ]
    if host == "agent-plugins":
        assert config["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        assert entry["type"] == "stdio"
        assert entry["command"].startswith("./")
        launcher = output / entry["command"]
        launched = subprocess.run(
            [launcher, "/bin/sh", "-c", 'printf "%s" "$1"', "probe", str(interpreter)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert launched.stdout == str(interpreter)
        assert entry["args"] == [str(interpreter), *expected]
    else:
        assert entry == {"command": str(interpreter), "args": expected}
        status = (output / "commands/evolution/status.md").read_text()
        report = (output / "commands/evolution/report.md").read_text()
        for command, text in [("status", status), ("report --markdown", report)]:
            assert (
                f"{shlex.quote(str(interpreter))} -E -P -m evolution_lab.cli --socket {shlex.quote(str(socket))} {command}"
                in text
            )
            assert "If `lab` is unavailable on PATH" not in text
        assert "{{args}}" in status and "{{args}}" in report
        assert "Read the Markdown from stdout and keep the interaction read-only." in report
        assert "Keep the interaction read-only." in status
    if host == "gigacode":
        assert "mcpServers" not in json.loads((output / "gigacode-extension.json").read_text())
    skill = (output / "skills/evolution-lab-first-evaluation/SKILL.md").read_text()
    assert skill.startswith("---\n")
    assert f"Autonomy is `{mode}`" in skill
    assert f"{shlex.quote(str(interpreter))} -E -P -m evolution_lab.cli" in skill
    assert SERVER in skill and str(socket) in skill and "health" in skill
    assert "Always use the socket-bound prefix" in skill
    assert "overrides service discovery, `--home` and `--socket` examples" in skill
    assert "Use the autonomy above in place of generic approval guidance" in skill
    assert "Use the interpreter-only prefix for these offline commands" in skill
    assert "`campaign DIR` and `trace DIR`" in skill
    assert "`view OPERATION`" in skill
    if mode == "ask":
        assert "operation_start" in skill and "project_accept_commit" in skill
        assert "explicit yes" in skill and "human_only" in skill
    else:
        assert "client's own approval" in skill


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("partial", "supplied together"),
        ("relative", "absolute paths"),
        ("invalid-mode", "invalid choice"),
        ("no-interpreter", "executable"),
        ("not-executable", "executable"),
        ("template-path", "slash-command template"),
        ("no-extra", "MCP extra"),
        ("wrong-version", "Lab version"),
    ],
)
def test_invalid_connection_leaves_no_bundle(tmp_path, case, message):
    output = tmp_path / "plugin"
    interpreter = _probe_python(
        tmp_path,
        mcp=case != "no-extra",
        version="0.0.invalid" if case == "wrong-version" else None,
    )
    socket = "relative.sock" if case == "relative" else str(tmp_path / "service.sock")
    if case == "template-path":
        socket = str(tmp_path / "service-{{args}}.sock")
    if case == "no-interpreter":
        interpreter.unlink()
    if case == "not-executable":
        interpreter.chmod(0o600)
    args = ["--lab-python", str(interpreter)]
    if case != "partial":
        args += ["--lab-socket", socket]
    if case == "invalid-mode":
        args += ["--autonomy", "automatic"]
    result = subprocess.run(
        [sys.executable, ROOT / "plugins/qwen/build.py", output, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()
