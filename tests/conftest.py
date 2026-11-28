from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_skills(tmp_path: Path) -> Path:
    """A temporary directory pre-populated with fixture skills."""
    skills_dir = tmp_path / ".agents" / "skills"
    if FIXTURES_DIR.exists():
        shutil.copytree(FIXTURES_DIR, skills_dir)
    else:
        skills_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def strict_durability(monkeypatch):
    monkeypatch.setenv("EVOLUTION_DURABILITY", "strict")


# Set before collecting test modules; the wrapper also covers interpreter startup.

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def ram_backed(path: Path) -> bool:
    path = path.resolve()
    marker = os.environ.get("EVOLUTION_TEST_RAMDISK")
    if marker and path.is_relative_to(Path(marker).resolve()):
        return True
    if sys.platform != "linux":
        return False
    selected = None
    try:
        mounts = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return False
    for line in mounts:
        fields = line.split()
        mount = fields[1]
        for escaped, value in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
            mount = mount.replace(escaped, value)
        root = Path(mount)
        if path.is_relative_to(root) and (
            selected is None or len(root.parts) > len(selected[0].parts)
        ):
            selected = (root, fields[2])
    return selected is not None and selected[1] == "tmpfs"


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    config = session.config
    if hasattr(config, "workerinput"):
        return
    if os.environ.get("EVOLUTION_TEST_ALLOW_DISK") == "1":
        print("pytest: EVOLUTION_TEST_ALLOW_DISK=1, temporary files may go to disk")
        return
    for path in (config._tmp_path_factory.getbasetemp().parent, Path(tempfile.gettempdir())):
        if not ram_backed(path):
            pytest.exit(
                f"Temporary directory {path} is not RAM-backed; run scripts/test-tmp or set EVOLUTION_TEST_ALLOW_DISK=1",
                returncode=2,
            )
