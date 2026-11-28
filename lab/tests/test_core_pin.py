"""The exact core pin (spec 2026-10-29 §3): pyproject, the core_raw adapter and the installed core agree."""
from importlib.metadata import version
from pathlib import Path
import tomllib

from evolution_lab.owner import core_raw

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def test_pin_is_b22_everywhere():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.0.3.dev81"
    assert "evo-skills==2.0.0b22" in project["dependencies"]
    assert "evo-skills[tree]==2.0.0b22" in project["optional-dependencies"]["dev"]
    assert core_raw.SUPPORTED_CORE == "2.0.0b22"
    assert version("evo-skills") == core_raw.SUPPORTED_CORE
