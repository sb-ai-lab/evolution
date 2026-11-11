"""Distribution members must belong to the declared source and preserve its bytes."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_release_artifacts", ROOT / "scripts" / "check_release_artifacts.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "checkout"
    (source / "src/sample").mkdir(parents=True)
    (source / "src/sample/__init__.py").write_bytes(b"VALUE = 1\n")
    (source / "src/sample/py.typed").write_bytes(b"")
    (source / "README.md").write_bytes(b"# Sample package\n")
    (source / "LICENSE").write_bytes(b"Example license\n")
    (source / ".gitignore").write_bytes(b"build/\n")
    (source / "pyproject.toml").write_text("""
[project]
name = "sample-project"
license-files = ["LICENSE"]
[tool.hatch.build.targets.wheel]
packages = ["src/sample"]
exclude = ["src/sample/omitted"]
[tool.hatch.build.targets.sdist]
only-include = ["/src/sample", "/README.md", "/LICENSE", "/pyproject.toml"]
exclude = ["/src/sample/omitted"]
""")
    monkeypatch.setattr(checker, "ROOT", source, raising=False)
    return source


def archive(tmp_path: Path, kind: str, files: dict[str, bytes]) -> Path:
    result = tmp_path / ("package." + kind)
    if kind in {"whl", "zip"}:
        with zipfile.ZipFile(result, "w") as output:
            for name, data in files.items():
                output.writestr(name, data)
    else:
        with tarfile.open(result, "w:gz") as output:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
    return result


@pytest.mark.parametrize("kind", ["whl", "tar.gz", "zip"])
def test_accepts_declared_files_and_generated_metadata(
    tmp_path: Path, project: Path, kind: str
) -> None:
    if kind == "whl":
        files = {
            "sample/__init__.py": (project / "src/sample/__init__.py").read_bytes(),
            "sample/py.typed": b"",
            "sample_project-1.0.dist-info/METADATA": b"Name: sample-project\n",
            "sample_project-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
            "sample_project-1.0.dist-info/RECORD": b"",
            "sample_project-1.0.dist-info/entry_points.txt": b"",
            "sample_project-1.0.dist-info/licenses/LICENSE": (project / "LICENSE").read_bytes(),
        }
    else:
        files = {
            "sample_project-1.0/" + name: (project / name).read_bytes()
            for name in [
                "src/sample/__init__.py",
                "README.md",
                "LICENSE",
                "pyproject.toml",
                ".gitignore",
            ]
        }
        files["sample_project-1.0/PKG-INFO"] = b"Name: sample-project\n"
    assert checker.check_archive(archive(tmp_path, kind, files)) == []


@pytest.mark.parametrize("kind", ["whl", "tar.gz", "zip"])
def test_rejects_unknown_paths_and_changed_source_bytes(
    tmp_path: Path, project: Path, kind: str
) -> None:
    prefix = "" if kind == "whl" else "sample_project-1.0/"
    source = "sample/__init__.py" if kind == "whl" else "src/sample/__init__.py"
    files = {
        prefix + source: b"altered source\n",
        prefix + "not-shipped/state.txt": b"unrelated data",
    }
    assert checker.check_archive(archive(tmp_path, kind, files)) == sorted(files)


@pytest.mark.parametrize("kind", ["whl", "tar.gz", "zip"])
def test_respects_declared_exclusions(tmp_path: Path, project: Path, kind: str) -> None:
    path = project / "src/sample/omitted/record.txt"
    path.parent.mkdir()
    path.write_bytes(b"not distributed")
    name = (
        "sample/omitted/record.txt"
        if kind == "whl"
        else "sample_project-1.0/src/sample/omitted/record.txt"
    )
    files = {name: path.read_bytes()}
    assert checker.check_archive(archive(tmp_path, kind, files)) == [name]


@pytest.mark.parametrize("kind", ["whl", "tar.gz", "zip"])
def test_rejects_unsafe_paths(tmp_path: Path, project: Path, kind: str) -> None:
    files = {
        "../README.md": b"outside",
        "/README.md": b"absolute",
        "sample/../../README.md": b"escape",
    }
    assert checker.check_archive(archive(tmp_path, kind, files)) == sorted(files)


@pytest.mark.parametrize("kind", ["tar.gz", "zip"])
def test_rejects_links_even_with_a_valid_empty_source_path(
    tmp_path: Path, project: Path, kind: str
) -> None:
    import stat

    name = "sample_project-1.0/src/sample/py.typed"
    path = tmp_path / ("link." + kind)
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as output:
            member = zipfile.ZipInfo(name)
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(member, b"")
    else:
        with tarfile.open(path, "w:gz") as output:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = "elsewhere"
            output.addfile(member)
    assert checker.check_archive(path) == [name]


def test_license_copy_must_match_source(tmp_path: Path, project: Path) -> None:
    name = "sample_project-1.0.dist-info/licenses/LICENSE"
    assert checker.check_archive(archive(tmp_path, "whl", {name: b"altered"})) == [name]


def test_untracked_package_file_is_not_admitted(tmp_path: Path, project: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    (project / "src/sample/extra.py").write_bytes(b"untracked")
    name = "sample/extra.py"
    assert checker.check_archive(archive(tmp_path, "whl", {name: b"untracked"})) == [name]
