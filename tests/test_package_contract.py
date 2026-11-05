"""Public distribution contract for the production package."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

import evolution

ROOT = Path(__file__).resolve().parents[1]


def test_public_distribution_is_runtime_complete_and_experiment_free() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    project = config["project"]
    assert project["name"] == "evo-skills"
    assert project["requires-python"] == ">=3.11.3"
    assert project["dynamic"] == ["version"]
    assert any(requirement.startswith("pytest>=8") for requirement in project["dependencies"])
    assert set(project["optional-dependencies"]) == {"llm", "gigachat", "tree"}

    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/evolution"]

    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    included = tuple(sdist["only-include"])
    assert not any(path.startswith("/experiments") for path in included)
    assert not any(path.startswith("/tests") for path in included)

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (locked,) = (pkg for pkg in lock["package"] if pkg["name"] == "evo-skills")
    assert set(locked["metadata"]["provides-extras"]) == set(project["optional-dependencies"])


def test_version_has_one_source() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" not in config["project"]
    assert config["tool"]["hatch"]["version"]["path"] == "src/evolution/__init__.py"
    assert evolution.__version__ == "2.0.0b16"


def test_changelog_opens_with_the_current_version() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \[(.+?)\]", text, flags=re.M)
    # An unreleased change may open the file; the release PR renames `[Unreleased]`.
    if headings[:1] == ["Unreleased"]:
        headings = headings[1:]
    assert headings and headings[0] == evolution.__version__
    section = text.split(f"## [{evolution.__version__}]", 1)[1].split("\n## [", 1)[0]
    for phrase in (
        "`evo tree evolve`",
        "`evo tree conformance`",
        "`evolution-v2-manifest/5`",
        "`evo-target`",
        "`evo gui`",
        "`[gui]`",
        "`evo tree edit`",
        "`package_version_mismatch`",
    ):
        assert phrase in section, phrase


def test_distribution_declares_its_inline_types() -> None:
    assert (ROOT / "src" / "evolution" / "py.typed").is_file()


def test_citation_metadata_matches_the_release() -> None:
    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert citation["version"] == evolution.__version__
    assert citation["repository-code"] == config["project"]["urls"]["Repository"]
