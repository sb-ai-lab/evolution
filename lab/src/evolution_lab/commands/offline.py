"""Offline reads (guides, requirements, scenarios) as commands: they never open the service socket (ADR-0070).

Values come from ``evolution_lab.guides``, never from ``evolution_lab.cli``.
"""
from __future__ import annotations

from pathlib import PurePath
from typing import Any

from evolution_lab.commands._core import Effect, command


@command("guides_list", effect=Effect.READ, schema=None, service=False)
def guides_list() -> list[dict[str, str]]:
    from evolution_lab import guides
    return [{"name": g.name, "description": g.description, "path": g.path} for g in guides.list_guides()]


def _contained(base, directory: str, file: str):
    """The file under ``base/directory``, resolved; it must sit under both the guide directory and ``base``."""
    guide_directory = (base / directory).resolve()
    target = (guide_directory / file).resolve()
    if not (target.is_relative_to(guide_directory) and target.is_relative_to(base)) or not target.is_file():
        raise ValueError("unknown guide file")
    return target


@command("guide_read", effect=Effect.READ, schema=None, service=False)
def guide_read(name: str, file: str = "SKILL.md") -> dict[str, str]:
    from evolution_lab import guides
    if not isinstance(name, str) or not isinstance(file, str) or PurePath(file).is_absolute():
        raise ValueError("unknown guide file")
    base = guides.root().resolve()
    # A guide is addressed by its listed frontmatter name or its directory (as in ``lab guides list`` paths).
    listed = guides.list_guides()
    directories = {g.name: g.path.partition("/")[0] for g in listed}
    directories.update({g.path.partition("/")[0]: g.path.partition("/")[0] for g in listed})
    if name.startswith("scenarios/") and name[len("scenarios/"):] in {d["id"] for d in guides.scenarios().descriptors}:
        directory = name
    elif name in directories:
        directory = directories[name]
    else:
        raise ValueError("unknown guide file")
    target = _contained(base, directory, file)
    return {"name": name, "file": file, "text": target.read_text(encoding="utf-8")}


@command("requirements", effect=Effect.READ, schema=None, service=False)
def requirements() -> dict[str, Any]:
    from evolution_lab import guides
    return guides.requirements()


@command("scenarios_list", effect=Effect.READ, schema=None, service=False)
def scenarios_list() -> dict[str, Any]:
    from evolution_lab import guides
    return guides.scenarios().list_payload()


@command("scenario_show", effect=Effect.READ, schema=None, service=False)
def scenario_show(scenario: str) -> dict[str, Any]:
    from evolution_lab import guides
    catalogue = guides.scenarios()
    if catalogue.descriptor(scenario) is None:
        raise LookupError("The scenario is not in the catalogue.")
    return catalogue.entry_payload(scenario)


@command("scenario_schema", effect=Effect.READ, schema=None, service=False)
def scenario_schema(scenario: str, schema: str) -> str:
    from evolution_lab import guides
    path = guides.scenarios().schema_path(scenario, schema)
    if path is None:
        raise LookupError("The scenario or its schema is not in the catalogue.")
    return path.read_text(encoding="utf-8")


@command("context_preflight", effect=Effect.READ, schema=None, service=False)
def context_preflight(state, prepared: str, *, operation=None, trace=None, baseline=None, scenario=None):
    """Measure a local prepared campaign with optional compatible evidence."""
    from evolution_lab.preflight import inspect_context
    return inspect_context(state, prepared, operation=operation, trace=trace, baseline=baseline,
                           scenario=scenario)
