"""Run the wrapper and verify pytest stops before collection or worker startup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux storage selection")
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/test-tmp"


def run_wrapper(
    env,
    code='import os,json; print(json.dumps([os.environ.get("TMPDIR"),os.environ.get("EVOLUTION_TEST_RAMDISK"),os.environ.get("PYTHONDONTWRITEBYTECODE")]))',
):
    env = dict(os.environ, **env)
    env.pop("EVOLUTION_TEST_ALLOW_DISK", None)
    return subprocess.run(
        [str(SCRIPT), sys.executable, "-c", code], env=env, capture_output=True, text=True
    )


def test_uses_tmpfs_tmpdir(tmp_path):
    filesystem = subprocess.check_output(["stat", "-f", "-c", "%T", str(tmp_path)], text=True)
    parent = tmp_path if filesystem.strip() == "tmpfs" else Path("/dev/shm")
    with TemporaryDirectory(dir=parent) as ram_dir:
        result = run_wrapper({"TMPDIR": ram_dir})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [ram_dir, ram_dir, "1"]


def test_falls_back_to_dev_shm():
    if subprocess.check_output(["stat", "-f", "-c", "%T", str(ROOT)], text=True).strip() == "tmpfs":
        pytest.skip("checkout itself is in RAM")
    result = run_wrapper({"TMPDIR": str(ROOT), "EVOLUTION_TEST_RAMDISK_MB": "1"})
    assert result.returncode == 0, result.stderr
    child_tmp, ram, bytecode = json.loads(result.stdout)
    assert child_tmp.startswith("/dev/shm/evo-test.")
    assert ram == child_tmp and bytecode == "1"
    assert not Path(child_tmp).exists()


def test_refuses_when_no_ram():
    free = os.statvfs("/dev/shm")
    mb = (free.f_bavail * free.f_frsize) // (1024 * 1024) + 1024
    result = run_wrapper({"TMPDIR": str(ROOT), "EVOLUTION_TEST_RAMDISK_MB": str(mb)})
    assert result.returncode == 2
    assert "/dev/shm" in result.stderr


def test_allow_disk():
    result = subprocess.run(
        [str(SCRIPT), sys.executable, "-c", 'import os; print(os.environ["TMPDIR"])'],
        env=dict(os.environ, TMPDIR=str(ROOT), EVOLUTION_TEST_ALLOW_DISK="1"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(ROOT)
    assert "EVOLUTION_TEST_ALLOW_DISK=1" in result.stderr


def test_command_exit_code(tmp_path):
    result = run_wrapper({"TMPDIR": str(tmp_path)}, "raise SystemExit(7)")
    assert result.returncode == 7


def test_macos_runs_use_distinct_mounts_and_detach_their_own_device(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    commands = {
        "uname": "echo Darwin",
        "uuidgen": 'echo "$MOCK_VOLUME"',
        "hdiutil": 'echo "hdiutil $*" >> "$MOCK_LOG"\n[ "$1" != attach ] || echo " /dev/$MOCK_VOLUME "',
        "diskutil": 'echo "diskutil $*" >> "$MOCK_LOG"',
        # Do not create real /Volumes directories on the Linux test host.
        "mkdir": 'echo "mkdir $*" >> "$MOCK_LOG"',
    }
    for name, script in commands.items():
        path = tools / name
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o755)
    for volume in ("first", "second"):
        result = run_wrapper(
            {
                "PATH": str(tools) + ":" + os.environ["PATH"],
                "MOCK_VOLUME": volume,
                "MOCK_LOG": str(log),
            }
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)[:2] == [
            f"/Volumes/evo-{volume}/tmp",
            f"/Volumes/evo-{volume}",
        ]
        lines = log.read_text().splitlines()
        assert f"diskutil erasevolume APFS evo-{volume} /dev/{volume}" in lines
        assert f"hdiutil detach -force /dev/{volume}" in lines


@pytest.mark.parametrize("workers", [False, True])
def test_guard_stops_disk_session(pytester, monkeypatch, workers):
    pytester.makeconftest((ROOT / "tests/conftest.py").read_text())
    pytester.makepyfile('def test_never_collected():\n    raise AssertionError("test ran")')
    monkeypatch.setenv("TMPDIR", str(ROOT))
    monkeypatch.delenv("EVOLUTION_TEST_RAMDISK", raising=False)
    monkeypatch.delenv("EVOLUTION_TEST_ALLOW_DISK", raising=False)
    args = ["-q", "-p", "no:cacheprovider", "--basetemp", str(pytester.path / "base")]
    if workers:
        args += ["-n", "2"]
    result = pytester.runpytest_subprocess(*args, timeout=20)
    output = result.stdout.str() + result.stderr.str()
    assert result.ret == 2
    assert "scripts/test-tmp" in output
    assert "EVOLUTION_TEST_ALLOW_DISK" in output
    assert "test ran" not in output
    assert "bringing up nodes" not in output
