"""Requirement answers of the ``connect`` scenario: the Lab schema ``spec-answers/1`` built from the requirements
catalogue, and a complete sample document (spec 2026-10-03 §2, PDR-0022, ADR-0020 amendment 2026-10-03).

The schema file ships ready in ``scenarios/connect/schemas/``; ``tests/test_spec_answers_schema.py`` keeps it equal
to the generation. After a catalogue change: rewrite it from a source tree (``python -m evolution_lab.guides.answers``), then bump the
``connect`` ``version`` and recompute its digest, bump ``catalogue_version`` and recompute the scenario catalogue
digest (docs/api/projects.md, "Digest"), and update the digests in the docs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from . import ANSWERS, requirements, root
from ._scenarios import SCHEMA_DIALECT

SCHEMA = "connect-spec-answers/1"
NAME = "spec-answers/1"
FILE = "spec.answers.json"
STATUSES = ("known", "unknown", "needs_decision")
VERDICTS = ("as_is", "hard", "restructure")
_IMPLEMENTATION = r"^[a-z][a-z0-9-]{1,39}(?![\s\S])"  # projects/ids.py:_ID; no `$`, it admits a final newline


def _text(limit: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": limit}


def required_items(catalogue: Mapping[str, Any], path: str) -> list[str]:
    return [item["id"] for item in catalogue["items"]
            if item["obligation"] == "required" and path in item["applies_to"]]


def spec_answers_schema(catalogue: Mapping[str, Any]) -> dict[str, Any]:
    ids = [item["id"] for item in catalogue["items"]]
    kinds = [kind for kind in ANSWERS if any(kind in item["answer_from"] for item in catalogue["items"])]
    closed = [
        {"if": {"properties": {"path": {"const": path}}, "required": ["path"]},
         "then": {"properties": {"items": {
             "required": required_items(catalogue, path),
             "properties": {i: {"properties": {"status": {"const": "known"}}} for i in required_items(catalogue, path)},
         }}}}
        for path in catalogue["paths"]
    ]
    return {
        "$schema": SCHEMA_DIALECT,
        "title": SCHEMA,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "catalogue", "path", "items", "fit"],
        "properties": {
            "schema": {"const": SCHEMA},
            "catalogue": {
                "type": "object", "additionalProperties": False, "required": ["version", "digest"],
                "properties": {"version": {"const": catalogue["catalogue_version"]},
                               "digest": {"const": catalogue["digest"]}},
            },
            "path": {"enum": list(catalogue["paths"])},
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {i: {"$ref": "#/$defs/item"} for i in ids}},
            "fit": {"$ref": "#/$defs/fit"},
            "client": {"type": "object"},
        },
        "allOf": closed,
        "$defs": {
            "item": {
                "type": "object", "additionalProperties": False, "required": ["status"],
                "properties": {
                    "status": {"enum": list(STATUSES)},
                    "answer": _text(4000),
                    "source": {"type": "object", "additionalProperties": False, "required": ["kind"],
                               "properties": {"kind": {"enum": kinds}, "ref": _text(500)}},
                    "client": {"type": "object"},
                },
                "if": {"properties": {"status": {"const": "known"}}},
                "then": {"required": ["answer"]},
            },
            "fit": {
                "type": "object", "additionalProperties": False,
                "required": ["implementation", "verdict", "summary", "reasons"],
                "properties": {
                    "implementation": {"type": "string", "pattern": _IMPLEMENTATION},
                    "verdict": {"enum": list(VERDICTS)},
                    "summary": _text(600),
                    "reasons": {"type": "array", "maxItems": 8, "items": {
                        "type": "object", "additionalProperties": False, "required": ["item", "text"],
                        "properties": {"item": {"enum": ids}, "text": _text(400)}}},
                },
                "if": {"properties": {"verdict": {"not": {"const": "as_is"}}}},
                "then": {"properties": {"reasons": {"minItems": 1}}},
            },
        },
    }


def render(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def schema_file() -> Path:
    return root() / "scenarios" / "connect" / "schemas" / "spec-answers-1.json"


def complete_answers(catalogue: Mapping[str, Any], *, implementation: str, path: str = "campaign") -> dict[str, Any]:
    """A document that passes the schema: every required item of ``path`` known (the demo, tests)."""
    titles = {item["id"]: item for item in catalogue["items"]}
    items = {i: {"status": "known", "answer": f"Demo answer: {titles[i]['title']}",
                 "source": {"kind": titles[i]["answer_from"][0]}}
             for i in required_items(catalogue, path)}
    return {"schema": SCHEMA, "catalogue": {"version": catalogue["catalogue_version"], "digest": catalogue["digest"]},
            "path": path, "items": items,
            "fit": {"implementation": implementation, "verdict": "as_is",
                    "summary": "One turn entry point; tools and settings are declared, so the agent connects as is.",
                    "reasons": []}}


if __name__ == "__main__":
    import sys

    if not (Path(__file__).resolve().parents[3] / "pyproject.toml").is_file():
        sys.exit("refusing to write: the schema file is not inside a source tree (no pyproject.toml above the package)")
    schema_file().write_text(render(spec_answers_schema(requirements())), encoding="utf-8")
