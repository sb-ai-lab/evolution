"""Characterisation lock: the declared build backend must ship every prompt Markdown file.

The framework loads its prompts from ``src/evolution/prompts/**/*.md`` at runtime, so a
repackaging that stops shipping those files produces a ``pip install``ed copy that imports
fine and then fails the moment it needs a prompt. Hatchling already ships non-Python files
that live under the configured package directory, which is why ``pyproject.toml`` carries no
``[tool.hatch.build.targets.wheel] artifacts`` entry; this test locks that behaviour in place
across the repackaging.

The wheel is built through whatever ``[build-system] build-backend`` ``pyproject.toml``
declares, invoked via the PEP 517 ``build_wheel`` hook. Swapping the declared backend
therefore swaps what this lock exercises: a backend that does not carry package data fails
here instead of passing on a hard-coded ``hatchling`` invocation that no longer matches the
project.

The expected set is *derived from the source tree*, so adding or removing a prompt needs no
maintenance here -- there is no hash to refresh after an intentional change. A failure means
the wheel and the source tree disagree, which is one of:

  * the build configuration stopped including package data -- fix the build, not this test;
  * the declared backend changed to one that needs explicit package-data configuration --
    configure it, not this test;
  * a prompt moved out of ``src/evolution/prompts`` -- update ``SOURCE_PROMPTS_RELPATH`` and
    ``WHEEL_PROMPT_PREFIX`` below in the same commit as the move;
  * a prompt is now excluded from the wheel deliberately -- narrow the derivation below in
    the same commit, so the exclusion is visible in review.

Known limitations, stated so nobody reads more assurance into a green run than it carries:

  * the backend runs in the ambient interpreter, without PEP 517 build isolation, so the
    version pins in ``build-system.requires`` are not verified -- only that the declared
    backend, as installed here, ships the prompts;
  * the declared backend (``hatchling``) is not listed in any ``[project.optional-dependencies]``
    extra, so an environment installed from ``[dev]`` alone does not have it. This lock then
    *fails* with a message naming the missing requirement rather than skipping, because a
    silent skip on a packaging lock reports success for a check that never ran;
  * the expected inventory comes from ``git ls-files``, so files that git does not track are
    not expected in the wheel (matching the backend, which honours ``.gitignore``). Without
    git the derivation falls back to globbing the working tree, which is looser.

To reproduce the build by hand::

    pip wheel --no-deps --no-build-isolation -w /tmp/evolution-wheel .
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
SOURCE_PROMPTS_RELPATH = "src/evolution/prompts"
SOURCE_PROMPTS_DIR = REPO_ROOT / SOURCE_PROMPTS_RELPATH
WHEEL_PROMPT_PREFIX = "evolution/prompts/"
WHEEL_REFERENCE_SAMPLE_PREFIX = "evolution/tree/reference_sample/"
REFERENCE_SAMPLE_FILES = ("score.py", "edit.py", "corpus.jsonl")
BUILD_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 30
BACKEND_IMPORT_EXIT_CODE = 3
DEFAULT_BUILD_BACKEND = "setuptools.build_meta:__legacy__"

_BUILD_WHEEL_SCRIPT = f"""\
import importlib
import os
import sys

backend_name, backend_path, dist_dir, name_path = sys.argv[1:5]
for entry in reversed([part for part in backend_path.split(os.pathsep) if part]):
    sys.path.insert(0, entry)
module_name, _, attribute = backend_name.partition(":")
try:
    backend = importlib.import_module(module_name)
except ImportError as exc:
    sys.stderr.write(str(exc))
    raise SystemExit({BACKEND_IMPORT_EXIT_CODE})
for part in filter(None, attribute.split(".")):
    backend = getattr(backend, part)
wheel_name = backend.build_wheel(dist_dir)
with open(name_path, "w", encoding="utf-8") as handle:
    handle.write(wheel_name)
"""

pytestmark = pytest.mark.slow


def _build_system_table() -> dict:
    """The ``[build-system]`` table this project declares."""
    parsed = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return parsed.get("build-system", {})


def _tracked_prompt_files() -> list[str] | None:
    """Repo-relative prompt paths git tracks, or ``None`` when git cannot answer."""
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--", SOURCE_PROMPTS_RELPATH],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    return [name for name in listing.stdout.split("\0") if name.endswith(".md")]


def _globbed_prompt_entries() -> set[str]:
    """Fallback inventory for checkouts where git is unavailable."""
    return {
        WHEEL_PROMPT_PREFIX + path.relative_to(SOURCE_PROMPTS_DIR).as_posix()
        for path in SOURCE_PROMPTS_DIR.rglob("*.md")
    }


def _expected_prompt_entries() -> set[str]:
    """Wheel entry names the source tree says the wheel must contain.

    Tracked-and-present is the criterion: an untracked scratch ``prompts/NOTES.md`` is not
    something the backend ships, and a tracked file already deleted from the working tree is
    not something it can ship.
    """
    tracked = _tracked_prompt_files()
    if tracked is None:
        return _globbed_prompt_entries()
    prefix = SOURCE_PROMPTS_RELPATH + "/"
    return {
        WHEEL_PROMPT_PREFIX + name[len(prefix) :]
        for name in tracked
        if name.startswith(prefix) and (REPO_ROOT / name).is_file()
    }


@pytest.fixture(scope="module")
def wheel_entries(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """Names inside a wheel built by the declared backend (built once per module)."""
    build_system = _build_system_table()
    backend = build_system.get("build-backend", DEFAULT_BUILD_BACKEND)
    backend_path = os.pathsep.join(
        str(REPO_ROOT / entry) for entry in build_system.get("backend-path", [])
    )
    dist_dir = tmp_path_factory.mktemp("wheel")
    name_path = tmp_path_factory.mktemp("wheel-name") / "built.txt"
    build = subprocess.run(
        [
            sys.executable,
            "-c",
            _BUILD_WHEEL_SCRIPT,
            backend,
            backend_path,
            str(dist_dir),
            str(name_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if build.returncode == BACKEND_IMPORT_EXIT_CODE:
        requires = ", ".join(build_system.get("requires", [])) or "<none declared>"
        pytest.fail(
            f"declared PEP 517 backend {backend!r} is not importable "
            f"({build.stderr.strip()}); install build-system.requires ({requires}) -- "
            "this lock cannot check packaging without the backend, and reporting a pass "
            "for a build that never ran would be worse than this failure"
        )
    assert build.returncode == 0, f"wheel build failed:\n{build.stdout}\n{build.stderr}"
    assert name_path.is_file(), (
        f"backend {backend!r} exited 0 without reporting a wheel name:\n"
        f"{build.stdout}\n{build.stderr}"
    )
    wheel_name = name_path.read_text(encoding="utf-8").strip()
    wheel_path = dist_dir / wheel_name
    assert wheel_path.is_file(), (
        f"backend {backend!r} reported wheel {wheel_name!r}, which is not in {dist_dir}: "
        f"{sorted(path.name for path in dist_dir.iterdir())}"
    )
    with zipfile.ZipFile(wheel_path) as archive:
        return archive.namelist()


def test_wheel_ships_exactly_the_source_prompt_files(wheel_entries: list[str]) -> None:
    """An installed copy must carry the same prompt files the source tree carries."""
    shipped = {
        name
        for name in wheel_entries
        if name.startswith(WHEEL_PROMPT_PREFIX) and name.endswith(".md")
    }
    expected = _expected_prompt_entries()
    untracked = sorted(shipped - expected)
    assert shipped == expected, (
        "built wheel and git-tracked src/evolution/prompts/**/*.md drifted apart; "
        f"missing from wheel (the backend stopped shipping them): {sorted(expected - shipped)}; "
        f"in the wheel but not tracked by git: {untracked}"
        + (
            " -- a new prompt is only shipped to other checkouts once it is `git add`ed"
            if untracked
            else ""
        )
    )


def test_wheel_prompt_count_is_non_zero(wheel_entries: list[str]) -> None:
    """Guard against the set assertion passing vacuously on an empty prompt tree."""
    shipped = [
        name
        for name in wheel_entries
        if name.startswith(WHEEL_PROMPT_PREFIX) and name.endswith(".md")
    ]
    assert shipped, f"built wheel contains no {WHEEL_PROMPT_PREFIX}**/*.md entries"


def test_expected_prompt_inventory_is_non_empty() -> None:
    """Guard against a derivation that silently stops finding any prompt to expect."""
    assert _expected_prompt_entries(), (
        f"no prompt Markdown derived from {SOURCE_PROMPTS_RELPATH}; the derivation, not the "
        "wheel, is broken -- with an empty expectation this lock would pass on any wheel"
    )


def test_wheel_ships_the_reference_sample_files(wheel_entries: list[str]) -> None:
    """`evo-target init`'s scaffold copies these three files from the wheel (spec §4);
    an installed copy that stopped shipping them would scaffold from nothing."""
    expected = {WHEEL_REFERENCE_SAMPLE_PREFIX + name for name in REFERENCE_SAMPLE_FILES}
    shipped = set(wheel_entries)
    missing = sorted(expected - shipped)
    assert not missing, (
        f"built wheel is missing {missing} under {WHEEL_REFERENCE_SAMPLE_PREFIX}; "
        "evo-target init scaffolds a campaign from exactly these packaged files"
    )
