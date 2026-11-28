"""Copy the project skeleton shipped in the wheel into a user directory."""
from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from evolution_lab._paths import canonical_root
from evolution_lab.quickstart._errors import QuickstartError

# Files of the skeleton, as the user sees them. The packaged copy of
# ``.gitignore`` is called ``gitignore`` so that packaging keeps it.
SKELETON_FILES = (
    ".gitignore",
    "README.md",
    "agent/adapter.py",
    "agent/my_agent.py",
    "campaign-launch.json",
    "corpus.jsonl",
    "editor/edit.py",
    "evaluation-input.json",
    "launch.json",
    "manifest.json",
    "reflector/reflect.py",
    "scorer/score.py",
    "seed/skills/SKILL.md",
)
_RENAMED = {"gitignore": ".gitignore"}


def _walk(node: Traversable, prefix: str = ""):
    for child in sorted(node.iterdir(), key=lambda item: item.name):
        if child.name == "__pycache__":
            continue
        if child.is_dir():
            yield from _walk(child, f"{prefix}{child.name}/")
        else:
            yield f"{prefix}{_RENAMED.get(child.name, child.name)}", child


def new_project(directory: Path) -> Path:
    """Write the project skeleton into ``directory`` and return its absolute path.

    ``directory`` must not exist or must be empty; nothing is overwritten.
    """
    try:
        target = canonical_root(directory)
    except ValueError:
        raise QuickstartError('invalid_directory', 'invalid project directory') from None
    if target.is_symlink():
        raise QuickstartError('invalid_directory', f'{target} is a symlinked project directory')
    if target.exists():
        if not target.is_dir():
            raise QuickstartError("invalid_directory", f"{target} exists and is not a directory")
        if any(target.iterdir()):
            raise QuickstartError("directory_not_empty", f"{target} is not empty")
    else:
        for parent in target.parents:
            if parent.exists():
                if not parent.is_dir():
                    raise QuickstartError("invalid_directory", f"{parent} is not a directory")
                break
    files = list(_walk(resources.files("evolution_lab.quickstart") / "skeleton"))
    if sorted(name for name, _ in files) != sorted(SKELETON_FILES):
        raise QuickstartError("skeleton_damaged", "the packaged project skeleton is incomplete", exit_code=3)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name, source in files:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    except OSError as error:
        raise QuickstartError("invalid_directory", f"cannot write {target}: {error.strerror or error}") from error
    return target
