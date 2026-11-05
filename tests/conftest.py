from __future__ import annotations

import shutil
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
