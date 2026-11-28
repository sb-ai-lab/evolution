"""Resolve ``"lab:auto"`` markers in a run copy of a project (ADR-0032 decisions 1-2).

The source project is never written. The resolver is pure: the port and the
environment are passed in and it does not read ``os.environ``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evolution_lab._fileset import excluded_name
from evolution_lab.preparation.models import EnvironmentDescriptor
from evolution_lab.preparation.storage import canonical_json_bytes
from evolution_lab.quickstart._errors import QuickstartError

MARKER = "lab:auto"
LAUNCH = "launch.json"
EVALUATION = "evaluation-input.json"

# Fields where a marker may stand (ADR-0032), as (file, dotted path).
_ALLOWED = {
    (LAUNCH, "environment_ref"), (LAUNCH, "endpoint.base_url"), (LAUNCH, "identity.contract_digest"),
    (LAUNCH, "identity.settings_hash"), (LAUNCH, "identity.runtime_ref_digest"),
    (EVALUATION, "target.base_url"), (EVALUATION, "target.contract_digest"),
    (EVALUATION, "target.settings_hash"), (EVALUATION, "scorer.argv.0"),
}
# The run copy leaves out what Lab's shared file-selection rule leaves out of every Bundle and inventory
# (``evolution_lab._fileset``, ADR-0017 p. 6: ``.git``, ``.venv``, caches, ``*.pyc``; ``venv`` without a dot is
# code and stays), and Lab's own ``.lab`` at the top of the project.
LAB_DIRECTORY = ".lab"
COMPUTED = "computed"
# One value shared by a launch field and an evaluation-input field.
_PAIRS = (
    ("endpoint.base_url", "target.base_url"),
    ("identity.contract_digest", "target.contract_digest"),
    ("identity.settings_hash", "target.settings_hash"),
)


@dataclass(frozen=True)
class Resolution:
    """What the resolver filled in and what the user had already written.

    ``substituted`` is every ``"lab:auto"`` field Lab replaced, with its value; ``sources`` says where each
    value came from: ``computed`` by Lab, or ``paired:<file>:<field>`` when Lab copied the explicit value of the
    paired field. ``pair``, ``python`` (the interpreter of the target and the scorer) and ``wheels`` (each with its
    sha256) say what the run was measured with; ``lab evaluate`` sets them."""

    substituted: dict[str, str]
    explicit: tuple[str, ...]
    lab_version: str
    core_version: str
    run_source: Path = field(compare=False, default=Path("."))
    pair: str | None = None
    python: str | None = None
    wheels: tuple[Mapping[str, str], ...] = ()
    sources: Mapping[str, str] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """The ``lab-evaluate-resolved/1`` document. Holds no secret value."""
        return {
            "schema": "lab-evaluate-resolved/1",
            "lab_version": self.lab_version,
            "core_version": self.core_version,
            "pair": self.pair,
            "python": self.python,
            "wheels": [dict(wheel) for wheel in self.wheels],
            "substituted": dict(sorted(self.substituted.items())),
            "sources": {name: self.sources.get(name, COMPUTED) for name in sorted(self.substituted)},
            "explicit": list(self.explicit),
        }


def _load(source: Path, name: str) -> Any:
    try:
        return json.loads((source / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QuickstartError("project_invalid", f"{name} is missing", file=name) from None
    except (OSError, ValueError) as error:
        raise QuickstartError("project_invalid", f"{name} is not valid JSON: {error}", file=name) from None


def _get(document: Any, path: str) -> tuple[bool, Any]:
    node = document
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def _set(document: Any, path: str, value: Any) -> None:
    *head, last = path.split(".")
    node = document
    for part in head:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _markers(node: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _markers(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _markers(value, f"{prefix}.{index}" if prefix else str(index))
    elif node == MARKER:
        found.append(prefix)
    return found


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def resolve_project(source: Path, run_source: Path, *, environment: EnvironmentDescriptor, port: int,
                    lab_version: str, core_version: str, pair: str | None = None, python: str | None = None,
                    wheels: Sequence[Mapping[str, str]] = ()) -> Resolution:
    """Resolve evaluation files; preserve the existing marker and resolution contracts."""
    return _resolve_project(source, run_source, environment=environment, port=port,
                            lab_version=lab_version, core_version=core_version, pair=pair,
                            python=python, wheels=wheels)


def _resolve_project(source: Path, run_source: Path, *, environment: EnvironmentDescriptor, port: int,
                    lab_version: str, core_version: str, pair: str | None = None, python: str | None = None,
                    wheels: Sequence[Mapping[str, str]] = (), launch_name: str = LAUNCH,
                    input_name: str = EVALUATION, role_interpreters: Sequence[str] = ("scorer",)) -> Resolution:
    """Write a resolved copy of ``source`` to ``run_source`` and describe the substitution.

    The copy leaves out what ``evolution_lab._fileset.excluded_name`` excludes, at any depth, and ``.lab`` at
    the top; ``launch.json`` and ``evaluation-input.json`` are written there as regular files, even when the
    project's own are symlinks. ``pair``, ``python``
    and ``wheels`` (``{"path", "sha256"}`` each) are only recorded in the resolution."""
    allowed = {(launch_name if name == LAUNCH else input_name, path) for name, path in _ALLOWED}
    allowed.update((input_name, f"{role}.argv.0") for role in role_interpreters)
    source, run_source = Path(source), Path(run_source)
    documents = {launch_name: _load(source, launch_name), input_name: _load(source, input_name)}
    for name, document in documents.items():
        if not isinstance(document, dict):
            raise QuickstartError("project_invalid", f"{name} must be a JSON object", file=name)
        for path in _markers(document):
            if (name, path) not in allowed:
                raise QuickstartError(
                    "marker_not_allowed", f'"{MARKER}" is not allowed in {name}:{path}', file=name, field=path)

    launch, evaluation = documents[launch_name], documents[input_name]
    written = json.loads(json.dumps(launch))  # launch.json as the user wrote it, markers in place
    env = launch.get("env", {})
    names = launch.get("secret_names", [])
    defaults = {
        (launch_name, "environment_ref"): environment.digest,
        (launch_name, "identity.runtime_ref_digest"): environment.digest,
        **{(input_name, f"{role}.argv.0"): environment.executable for role in role_interpreters},
        ("pair", "endpoint.base_url"): f"http://127.0.0.1:{port}",
        ("pair", "identity.contract_digest"): _sha256(written),
        ("pair", "identity.settings_hash"): _sha256({"env": dict(sorted(env.items())), "secret_names": sorted(names)}),
    }
    substituted: dict[str, str] = {}
    sources: dict[str, str] = {}
    explicit: list[str] = []

    def single(name: str, path: str) -> None:
        present, value = _get(documents[name], path)
        if not present:
            return
        if value == MARKER:
            value = defaults[(name, path)]
            _set(documents[name], path, value)
            substituted[f"{name}:{path}"] = value
            sources[f"{name}:{path}"] = COMPUTED
        else:
            explicit.append(f"{name}:{path}")

    single(launch_name, "environment_ref")
    single(launch_name, "identity.runtime_ref_digest")
    for role in role_interpreters:
        single(input_name, f"{role}.argv.0")

    for launch_path, evaluation_path in _PAIRS:
        l_here, l_value = _get(launch, launch_path)
        e_here, e_value = _get(evaluation, evaluation_path)
        l_name, e_name = f"{launch_name}:{launch_path}", f"{input_name}:{evaluation_path}"
        l_explicit = l_here and l_value != MARKER
        e_explicit = e_here and e_value != MARKER
        if l_explicit and e_explicit and str(l_value) != str(e_value):
            raise QuickstartError(
                "conflicting_values", f"{l_name} and {e_name} must be equal, got {l_value!r} and {e_value!r}",
                file=launch_name, field=launch_path)
        if l_explicit or e_explicit:
            value, origin = (l_value, l_name) if l_explicit else (e_value, e_name)
            explicit += [name for name, flag in ((l_name, l_explicit), (e_name, e_explicit)) if flag]
            # The other side of the pair, when it holds the marker, is replaced by the explicit value.
            for name, here, flag in ((l_name, l_here, l_explicit), (e_name, e_here, e_explicit)):
                if here and not flag:
                    substituted[name] = str(value)
                    sources[name] = f"paired:{origin}"
        else:
            value = defaults[("pair", launch_path)]
            for name, here in ((l_name, l_here), (e_name, e_here)):
                if here:
                    substituted[name] = value
                    sources[name] = COMPUTED
        if l_here:
            _set(launch, launch_path, value)
        if e_here:
            _set(evaluation, evaluation_path, value)

    def skipped(directory: str, names: list[str]) -> list[str]:
        top = Path(directory) == source
        return [name for name in names
                if (top and name == LAB_DIRECTORY)
                or excluded_name(name, directory=stat.S_ISDIR(os.lstat(os.path.join(directory, name)).st_mode))]

    shutil.copytree(source, run_source, symlinks=True, dirs_exist_ok=True, ignore=skipped)
    for name, document in documents.items():
        # The copy keeps a symlinked launch.json or evaluation-input.json as a link; writing through it would
        # change the original. Drop the copied entry and write a new regular file ("x": never follow a link).
        target = run_source / name
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        with target.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return Resolution(substituted, tuple(explicit), lab_version, core_version, run_source, pair, python,
                      tuple(dict(wheel) for wheel in wheels), sources)
