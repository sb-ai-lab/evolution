"""The README and the user guide name the latest released version pair.

The pair comes from the first row of each table in docs/RELEASES.md, so adding a release row
without updating the install instructions fails here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "docs" / "RELEASES.md"
GUIDE = Path(__file__).resolve().parents[2] / "docs" / "guide"
DOCS = (ROOT / "README.md", *sorted(GUIDE.glob("*.md")))
CORE_PROJECT = tomllib.loads((ROOT.parent / "pyproject.toml").read_text())["project"]
PUBLIC = CORE_PROJECT["urls"]["Repository"] == "https://github.com/sb-ai-lab/evolution"


def _first_row(heading: str) -> list[str]:
    section = RELEASES.read_text(encoding="utf-8").split(f"## {heading}\n", 1)[1]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    return [cell.strip().strip("`") for cell in rows[2].strip("|").split("|")]


def _released_pair() -> tuple[str, str, str | None]:
    lab, core = _first_row("Released pairs")[:2]
    if PUBLIC:
        return lab, core, None
    pair, _lab_sha, core_sha = _first_row("Wheel checksums")
    assert pair == f"{lab.rsplit('.', 1)[1]} / b{core.rsplit('b', 1)[1]}", pair
    return lab, core, core_sha


def test_release_tables_agree_on_the_latest_pair():
    lab, core, core_sha = _released_pair()
    assert re.fullmatch(r"0\.0\.3\.dev\d+", lab)
    assert re.fullmatch(r"2\.0\.0b\d+", core)
    if PUBLIC:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        assert lab == project["version"]
        assert f"evo-skills=={core}" in project["dependencies"]
    else:
        assert re.fullmatch(r"[0-9a-f]{64}", core_sha)


def test_docs_name_the_wheels_of_the_latest_released_pair():
    lab, core, core_sha = _released_pair()
    expected = {
        r"evolution_lab-([0-9][^-\s]*)-py3-none-any\.whl": lab,
        r"evo_skills-([0-9][^-\s]*)-py3-none-any\.whl": core,
        r"evolution-wheels/([0-9][^/\s]*)/": core,
        r"evolution-lab ([^\s]+)  \+  evo-skills": lab,
        r"evolution-lab [^\s]+  \+  evo-skills ([^\s]+) \(exact\)": core,
    }
    if core_sha is not None:
        expected[r"\b([0-9a-f]{64})  wheels/evo_skills-"] = core_sha
    stale = [
        f"{path.relative_to(ROOT.parent)}: {match.group(0)}"
        for path in DOCS
        for pattern, want in expected.items()
        for match in re.finditer(pattern, path.read_text(encoding="utf-8"))
        if match.group(1) != want
    ]
    assert not stale, "docs name another pair than docs/RELEASES.md:\n" + "\n".join(stale)


def test_readme_names_the_latest_release_tag():
    lab, _core, _sha = _released_pair()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    if PUBLIC:
        assert "](docs/RELEASES.md)" in text
        assert "](../docs/guide/install.md)" in text
    else:
        assert f"Current release: tag `lab-v{lab}`" in text
    assert f"evolution-lab {lab}  +  evo-skills" in text


def test_owner_examples_name_the_source_pair():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = next(item.split("==", 1)[1] for item in project["dependencies"] if item.startswith("evo-skills=="))
    text = (ROOT / "examples/owner/README.md").read_text(encoding="utf-8")
    assert f"evolution-lab=={project['version']}" in text
    assert f"evo-skills=={core}" in text
    assert f"evolution_lab-{project['version']}-py3-none-any.whl" in text
    assert f"evo_skills-{core}-py3-none-any.whl" in text
