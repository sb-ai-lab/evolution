"""Inside the Core repository the justfile builds Core from the repository's HEAD (Core ADR-0062).

Each test builds a throwaway git repository with a minimal Core project at its root and a copy
of Lab's justfile under ``lab/`` with a minimal Lab project that pins that Core version. The
wheels are real ``uv build`` wheels; ``install`` runs offline against them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

LAB_ROOT = Path(__file__).resolve().parents[1]
PIN = "2.0.0b22"
CORE_WHEEL = f"evo_skills-{PIN}-py3-none-any.whl"

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None or shutil.which("uv") is None or shutil.which("git") is None,
    reason="needs just, uv and git",
)

_HATCH = '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _set_core_version(repo: Path, version: str) -> None:
    (repo / "src/evolution/__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "evo-skills"\nversion = "{version}"\nrequires-python = ">=3.11"\n'
        '[project.optional-dependencies]\ntree = []\n' + _HATCH
        + '[tool.hatch.build.targets.wheel]\npackages = ["src/evolution"]\n',
        encoding="utf-8",
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "core"
    (root / "src/evolution").mkdir(parents=True)
    _set_core_version(root, PIN)
    lab = root / "lab"
    (lab / "src/evolution_lab").mkdir(parents=True)
    (lab / "src/evolution_lab/__init__.py").write_text("def main():\n    print('lab')\n", encoding="utf-8")
    (lab / "pyproject.toml").write_text(
        f'[project]\nname = "evolution-lab"\nversion = "0.0.3.dev77"\nrequires-python = ">=3.11"\n'
        f'dependencies = ["evo-skills=={PIN}"]\n'
        '[project.optional-dependencies]\nservice = []\n'
        '[project.scripts]\nlab = "evolution_lab:main"\n' + _HATCH
        + '[tool.hatch.build.targets.wheel]\npackages = ["src/evolution_lab"]\n',
        encoding="utf-8",
    )
    shutil.copy2(LAB_ROOT / "justfile", lab / "justfile")
    (root / "scripts").mkdir()
    shutil.copy2(LAB_ROOT.parent / "scripts/test-tmp", root / "scripts/test-tmp")
    (lab / ".gitignore").write_text(".lab/\n__pycache__/\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "A")
    return root


def _just(repo: Path, *args: str, check: bool = True,
          env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, UV_OFFLINE="1", **(env_extra or {}))
    env.pop("VIRTUAL_ENV", None)
    done = subprocess.run(["just", *args], cwd=repo / "lab", capture_output=True, text=True,
                          env=env, timeout=300)
    if check and done.returncode != 0:
        raise AssertionError(f"just {' '.join(args)} exited {done.returncode}:\n{done.stdout}{done.stderr}")
    return done


def _core_dirs(repo: Path) -> dict[str, list[str]]:
    wheels = repo / "lab/.lab/wheels"
    return {d.name: sorted(p.name for p in d.iterdir())
            for d in sorted(wheels.glob("core-*")) if d.is_dir()}


def _lab_only_commit(repo: Path, message: str) -> str:
    (repo / "lab/NOTES.md").write_text(message + "\n", encoding="utf-8")
    return _commit(repo, message)


def test_core_wheel_is_keyed_by_commit(repo: Path) -> None:
    a = _git(repo, "rev-parse", "HEAD")
    _just(repo, "core")
    (repo / "src/evolution/extra.py").write_text("X = 1\n", encoding="utf-8")
    b = _commit(repo, "B")
    _just(repo, "core")
    assert _core_dirs(repo) == {f"core-{a}": [CORE_WHEEL], f"core-{b}": [CORE_WHEEL]}
    assert _just(repo, "--evaluate", "core_wheel").stdout == str(
        repo / "lab/.lab/wheels" / f"core-{b}" / CORE_WHEEL)


def test_a_lab_only_commit_keeps_the_core_key(repo: Path) -> None:
    a = _git(repo, "rev-parse", "HEAD")
    _lab_only_commit(repo, "lab only")
    assert _just(repo, "--evaluate", "core_commit").stdout == a
    # A change outside lab/ that is not core code still moves the key: it can reach the wheel.
    (repo / "README.md").write_text("core readme\n", encoding="utf-8")
    c = _commit(repo, "core readme")
    _lab_only_commit(repo, "lab again")
    assert _just(repo, "--evaluate", "core_commit").stdout == c


def test_merged_lab_branch_keeps_the_core_key(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "side")
    _lab_only_commit(repo, "side lab")
    _git(repo, "checkout", "-q", "main")
    (repo / "src/evolution/extra.py").write_text("X = 1\n", encoding="utf-8")
    b = _commit(repo, "core on main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    assert _just(repo, "--evaluate", "core_commit").stdout == b


def test_version_mismatch_refuses(repo: Path) -> None:
    other_version = f"{PIN}.post1"
    _set_core_version(repo, other_version)
    _commit(repo, "bump")
    done = _just(repo, "core", check=False)
    assert done.returncode != 0
    assert other_version in done.stderr and f"evo-skills=={PIN} " in done.stderr
    assert _core_dirs(repo) == {}


def test_stale_environment_refuses(repo: Path) -> None:
    a = _git(repo, "rev-parse", "HEAD")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    _just(repo, f"python={python_version}", "setup")
    assert (repo / "lab/.lab/venv/core-commit").read_text(encoding="utf-8").strip() == a
    _just(repo, "_require-fresh-core")
    _lab_only_commit(repo, "lab only")
    _just(repo, "_require-fresh-core")
    (repo / "src/evolution/extra.py").write_text("X = 1\n", encoding="utf-8")
    b = _commit(repo, "B")
    done = _just(repo, "_require-fresh-core", check=False)
    assert done.returncode != 0
    assert a in done.stderr and b in done.stderr
    # Every recipe that runs Lab goes through the same check.
    assert _just(repo, "lab", "--help", check=False).returncode != 0


def test_dirty_core_paths_are_reported(repo: Path) -> None:
    a = _git(repo, "rev-parse", "HEAD")
    (repo / "src/evolution/__init__.py").write_text(f'__version__ = "{PIN}"\nEDITED = 1\n',
                                                     encoding="utf-8")
    done = _just(repo, "core")
    assert "src/evolution/__init__.py" in done.stdout + done.stderr
    wheel = repo / "lab/.lab/wheels" / f"core-{a}" / CORE_WHEEL
    with zipfile.ZipFile(wheel) as archive:
        assert b"EDITED" not in archive.read("evolution/__init__.py")
