"""Release artifacts must contain product code and no checkout/runtime state."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_release_artifacts", ROOT / "scripts" / "check_release_artifacts.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_wheel_accepts_product_files_and_rejects_runtime_state(tmp_path: Path) -> None:
    clean = tmp_path / "clean.whl"
    with zipfile.ZipFile(clean, "w") as archive:
        archive.writestr("evolution/__init__.py", "")
        archive.writestr("evolution/py.typed", "")
        archive.writestr("evolution/tree/cli.py", "")
        archive.writestr("evo_skills-0.3.0a1.dist-info/METADATA", "")
    assert checker.check_archive(clean) == []

    dirty = tmp_path / "dirty.whl"
    with zipfile.ZipFile(dirty, "w") as archive:
        archive.writestr("evolution/__init__.py", "")
        archive.writestr("evolution/core/experiments.py", "")
        archive.writestr("evolution/gui", "")
        archive.writestr("evolution/gui/app.py", "")
        archive.writestr("evolution/gui/views/skills.py", "")
        archive.writestr(".agents/skills/private/.traces/index.json", "")
    assert checker.check_archive(dirty) == [
        ".agents/skills/private/.traces/index.json",
        "evolution/core/experiments.py",
        "evolution/gui",
        "evolution/gui/app.py",
        "evolution/gui/views/skills.py",
    ]


def test_sdist_rejects_checkout_only_directories(tmp_path: Path) -> None:
    sdist = tmp_path / "evo_skills-0.3.0a1.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "evo_skills-0.3.0a1/src/evolution/__init__.py",
            "evo_skills-0.3.0a1/src/evolution/gui/app.py",
            "evo_skills-0.3.0a1/experiments/research.py",
            "evo_skills-0.3.0a1/tests/test_private.py",
        ):
            data = b""
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    assert checker.check_archive(sdist) == [
        "evo_skills-0.3.0a1/experiments/research.py",
        "evo_skills-0.3.0a1/src/evolution/gui/app.py",
        "evo_skills-0.3.0a1/tests/test_private.py",
    ]


def test_artifact_rejects_development_records_under_renamed_paths(tmp_path: Path) -> None:
    wheel = tmp_path / "records.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("docs/notes/state.md", "# Working state\n")
        archive.writestr("docs/getting-started.md", "---\nstatus: Accepted\n---\n# Decision\n")
        archive.writestr("AGENTS.md", "# Repository instructions\n")
        archive.writestr("docs/tutorial.md", "# Run a campaign\n")
    assert checker.check_archive(wheel) == [
        "AGENTS.md",
        "docs/getting-started.md",
        "docs/notes/state.md",
    ]
