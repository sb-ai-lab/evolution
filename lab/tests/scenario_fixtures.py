"""A test-only scenario of the restructure form (spec 2026-10-01 §5): `{implementation}` paths, a scenario-owned
schema, a human-only confirm and an optional step. Never shipped; written into a catalogue directory with real
digests, so no digest is a literal here."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from evolution_lab.guides import catalogue_digest
from evolution_lab.guides._scenarios import scenario_digest

BEHAVIOR_MAP = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "required": ["schema", "behaviors"], "additionalProperties": False,
    "properties": {
        "schema": {"const": "restructure-behavior-map/1"},
        "behaviors": {"type": "array", "items": {"$ref": "#/$defs/behavior"}},
    },
    "$defs": {"behavior": {"type": "object", "required": ["id", "source"], "additionalProperties": False,
                           "properties": {"id": {"type": "string", "minLength": 1},
                                          "source": {"type": "string"}}}},
}
RESTRUCTURE = {
    "schema": "lab-scenario/2", "id": "restructure", "version": 1, "title": "Restructure onto a template",
    "skill": "SKILL.md", "subject": "implementation", "requires": ["implementation.ported"],
    "produces": ["restructure.map_confirmed"], "repeatable": None,
    "schemas": {"behavior-map/1": "schemas/behavior-map-1.json"},
    "steps": [
        {"id": "map", "title": "Map the behaviour", "kind": "work", "human_only": False, "optional": False,
         "requires": [],
         "outputs": [{"artifact": {"path": "restructure/{implementation}/behavior-map.json", "check": "schema",
                                   "schema": "behavior-map/1"}}],
         "skill": "SKILL.md#map"},
        {"id": "map-confirm", "title": "Confirm the map", "kind": "confirm", "human_only": True, "optional": False,
         "requires": ["map"],
         "outputs": [{"attestation": {"claim": "restructure.map_confirmed",
                                      "path": "restructure/{implementation}/behavior-map.json"}}],
         "skill": "SKILL.md#map-confirm"},
        {"id": "notes", "title": "Write migration notes", "kind": "work", "human_only": False, "optional": True,
         "requires": [],
         "outputs": [{"artifact": {"path": "restructure/{implementation}/NOTES.md", "check": "exists"}}],
         "skill": "SKILL.md#notes"},
    ],
    "digest": "",
}
SKILL = ('---\nname: evolution-lab-scenario-restructure\ndescription: Test-only restructure scenario.\n---\n'
         '<a id="map"></a>\n<a id="map-confirm"></a>\n<a id="notes"></a>\n')


def write_catalogue(base: Path, scenarios: dict | None = None) -> Path:
    """Write ``{id: (descriptor, {relative path: document}, skill text)}`` as a scenario catalogue at ``base``,
    computing every digest (default: the restructure scenario alone)."""
    if scenarios is None:
        scenarios = {"restructure": (RESTRUCTURE, {"schemas/behavior-map-1.json": BEHAVIOR_MAP}, SKILL)}
    base.mkdir(parents=True, exist_ok=True)
    entries = []
    for scenario_id in sorted(scenarios):
        descriptor, files, skill = scenarios[scenario_id]
        directory = base / scenario_id
        directory.mkdir(parents=True, exist_ok=True)
        for relative, document in files.items():
            (directory / relative).parent.mkdir(parents=True, exist_ok=True)
            (directory / relative).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (directory / "SKILL.md").write_text(skill, encoding="utf-8")
        documents = {name: files[path] for name, path in descriptor.get("schemas", {}).items() if path in files}
        written = copy.deepcopy(descriptor)
        written["digest"] = scenario_digest(written, documents)
        (directory / "scenario.json").write_text(json.dumps(written, indent=2) + "\n", encoding="utf-8")
        entries.append({"id": scenario_id, "version": written["version"], "digest": written["digest"]})
    catalogue = {"schema": "lab-scenario-catalogue/1", "catalogue_version": 1, "scenarios": entries, "digest": ""}
    catalogue["digest"] = catalogue_digest(catalogue)
    (base / "catalogue.json").write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")
    return base
