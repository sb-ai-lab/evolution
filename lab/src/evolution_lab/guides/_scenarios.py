"""Scenario catalogue (spec §3.9, ADR-0017 p. 8; dev35: spec 2026-10-01 §2, ADR-0020): closed descriptors
``lab-scenario/2`` with digests, read like the requirements catalogue (ADR-0012). A scenario may own JSON Schema
2020-12 documents for its artifacts; each must be self-contained. No core import, no projects import."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from . import catalogue_digest

CATALOGUE_SCHEMA = "lab-scenario-catalogue/1"
SCENARIO_SCHEMA = "lab-scenario/2"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
PLACEHOLDER = "{implementation}"
MAX_INVALID = 5
# Deeper artifacts are an instance error before validation, so a recursive schema's validator never runs out of
# stack on a legitimate document and a RecursionError below this depth is the schema's own (e.g. a "$ref": "#" loop).
MAX_ARTIFACT_DEPTH = 200
# The computed facts of lab-project-facts/4 (spec §3.6; dev35 §2.5); a test pins equality with
# evolution_lab.projects.facts.COMPUTED so guides stays free of the projects import.
COMPUTED_FACTS = frozenset({
    "implementation.available", "implementation.ported", "materials.revision", "package.present", "package.valid",
    "check.passed", "conformance.passed", "baseline.measured", "connected", "campaigns"})
# The step outputs that establish a computed fact, as the status reads them (status._FACT_OF, the `@package` check,
# facts.OPERATION_FACTS for `connected`; a test pins the copy): one group per fact the computed one needs, so
# `connected` has three. An optional step may not be the only source in any group of a fact in `produces` (§2.4).
COMPUTED_SOURCES: dict[str, tuple[frozenset, ...]] = {
    "package.valid": (frozenset({("artifact", "@package")}),),
    "check.passed": (frozenset({("operation", ("check", "passed"))}),),
    "conformance.passed": (frozenset({("operation", ("conformance", "passed"))}),),
    "baseline.measured": (frozenset({("operation", ("evaluation", "measured"))}),),
    "campaigns": (frozenset({("operation", ("campaign", "terminal"))}),),
}
COMPUTED_SOURCES["connected"] = tuple(
    COMPUTED_SOURCES[name][0] for name in ("check.passed", "conformance.passed", "baseline.measured"))
KINDS = ("decide", "work", "operation", "confirm")
OPERATION_OUTCOMES = frozenset({
    ("check", "passed"), ("conformance", "passed"), ("evaluation", "measured"),
    ("prepare.campaign", "prepared"), ("campaign", "terminal"), ("export", "done")})
# Which output kinds a step kind may declare.
_OUTPUTS_OF = {"decide": {"artifact"}, "work": {"artifact"}, "operation": {"operation"}, "confirm": {"attestation"}}
_HUMAN_ONLY_KINDS = frozenset({"operation", "confirm"})
_TOP = frozenset({"schema", "catalogue_version", "scenarios", "digest"})
_ENTRY = frozenset({"id", "version", "digest"})
_DESCRIPTOR = frozenset({"schema", "id", "version", "title", "skill", "subject", "requires", "produces",
                         "repeatable", "steps", "digest"})
_DESCRIPTOR_OPTIONAL = frozenset({"schemas"})
_STEP = frozenset({"id", "title", "kind", "human_only", "requires", "outputs", "skill"})
_STEP_OPTIONAL = frozenset({"optional"})
_ID = re.compile(r"[a-z][a-z0-9-]*\Z")
_CLAIM = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\Z")
_SKILL = re.compile(r"SKILL\.md(#[a-z0-9-]+)?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_NAME = re.compile(r"([a-z][a-z0-9-]*)/([1-9][0-9]*)\Z")
# Self-containment (ADR-0020 p. 2): dynamic and recursive references could resolve outside the document.
_FORBIDDEN_KEYWORDS = ("$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor")
# Values that are data, not subschemas: a "$ref" inside them is not a reference (dependentRequired: name arrays).
_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples", "dependentRequired"})
# Keywords whose object keys are names (of properties, definitions); only their values are subschemas.
_NAMED_KEYWORDS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})


class SchemaUnusable(Exception):
    """A scenario schema failed while checking an artifact: the schema's fault, not the artifact's."""


def _fail(message: str) -> None:
    raise ValueError(f"scenario catalogue: {message}")


def _positive(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{where} must be a positive integer")


def _text(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 80 or "\n" in value or value != value.strip():
        _fail(f"{where} must be a non-empty single-line trimmed string up to 80 characters")


def _names(value: Any, where: str) -> list[str]:
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)
            or len(set(value)) != len(value)):
        _fail(f"{where} must be a list of unique names")
    return value


def _keys(value: Any, required: frozenset, optional: frozenset, where: str) -> None:
    if not isinstance(value, dict) or not required <= set(value) <= required | optional:
        extra = f" (optional: {', '.join(sorted(optional))})" if optional else ""
        _fail(f"{where} keys must be exactly " + ", ".join(sorted(required)) + extra)


def substitute(path: str, implementation_id: str | None) -> str:
    """``{implementation}`` replaced by the id the status is computed for (spec 2026-10-01 §2.2)."""
    if PLACEHOLDER not in path:
        return path
    if implementation_id is None:
        raise ValueError("scenario catalogue: {implementation} needs an implementation")
    return path.replace(PLACEHOLDER, implementation_id)


def inside_root(path: Any) -> bool:
    """The static path rule: a relative path without ``..`` that is not an ``@`` name (stays in the materials root)."""
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "@"))
            and ".." not in Path(path).parts)


def _relative(value: Any, where: str) -> None:
    if not inside_root(value):
        _fail(f"{where} must be a relative path without ..")


def _template_path(value: Any, subject: str, where: str) -> None:
    """A path that may carry ``{implementation}``: only with subject implementation, no other braces, and the
    substituted result must stay inside the materials root (checked with a stand-in id here, with the real id
    by the session)."""
    if not isinstance(value, str):
        _fail(f"{where} must be a string")
    if PLACEHOLDER in value and subject != "implementation":
        _fail(f"{where}: implementation_placeholder_without_subject")
    rest = value.replace(PLACEHOLDER, "")
    if "{" in rest or "}" in rest:
        _fail(f"{where} has an unknown placeholder")
    _relative(value.replace(PLACEHOLDER, "x"), where)


def _output(value: Any, step_kind: str, subject: str, schemas: Mapping[str, Any], where: str) -> str | None:
    """Checks one output; returns the attested claim, if any."""
    if not isinstance(value, dict) or len(value) != 1:
        _fail(f"{where} must have exactly one key")
    (kind, body), = value.items()
    if kind not in _OUTPUTS_OF[step_kind]:
        _fail(f"{where}: a {step_kind} step cannot declare a {kind} output")
    if kind == "artifact":
        if not isinstance(body, dict) or set(body) not in ({"path", "check"}, {"path", "check", "schema"}):
            _fail(f"{where}.artifact keys must be path, check (and schema for check schema)")
        path, check = body["path"], body["check"]
        if path == "@package":
            fits = check == "package" and "schema" not in body
        elif path == "@manifest":
            fits = check == "manifest" and "schema" not in body
        else:
            _template_path(path, subject, f"{where}.artifact.path")
            fits = (check == "exists" and "schema" not in body) or (check == "schema" and "schema" in body)
        if not fits:
            _fail(f"{where}.artifact check does not fit the path")
        if check == "schema" and body["schema"] not in schemas:
            _fail(f"{where}.artifact names an undeclared schema: {body['schema']!r}")
        return None
    if kind == "operation":
        if (not isinstance(body, dict) or set(body) != {"kind", "outcome"}
                or (body["kind"], body["outcome"]) not in OPERATION_OUTCOMES):
            _fail(f"{where}.operation must be a known kind/outcome pair")
        return None
    if not isinstance(body, dict) or set(body) != {"claim", "path"}:
        _fail(f"{where}.attestation keys must be claim, path")
    if not isinstance(body["claim"], str) or not _CLAIM.match(body["claim"]) or body["claim"] in COMPUTED_FACTS:
        _fail(f"{where}.attestation.claim must be a dotted claim that is not a computed fact")
    _template_path(body["path"], subject, f"{where}.attestation.path")
    return body["claim"]


def _compile_schema(schema: Any, where: str) -> Draft202012Validator:
    """A valid, self-contained 2020-12 schema (ADR-0020 p. 2), compiled against an empty resource registry:
    every ``$ref`` is internal and resolves here, at load time, so nothing can be retrieved later."""
    if not isinstance(schema, dict):
        _fail(f"{where} must be a JSON object")
    if schema.get("$schema", SCHEMA_DIALECT) != SCHEMA_DIALECT:
        _fail(f"{where} must be JSON Schema draft 2020-12")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        _fail(f"{where} is not a valid 2020-12 schema: {error.message}")
    refs: list[str] = []
    # Every object the walk treats as a schema (by identity). A $ref may only land on one of them (or on a boolean
    # schema): a target inside data (const, enum, default, examples, dependentRequired), at the name level of
    # properties/$defs/..., or on an array is not a schema location, and the walk never checked what lies there
    # (codex r2, ADR-0020). Since every target must be a walked location, the closure of reachable schemas is
    # exactly what the walk checked; a cycle among references needs no further walking.
    schema_nodes: set[int] = set()

    def walk(node: Any, root: bool) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, False)
            return
        if not isinstance(node, dict):
            return
        schema_nodes.add(id(node))
        for key, value in node.items():
            if key in _FORBIDDEN_KEYWORDS:
                _fail(f"{where} uses {key}; a scenario schema is self-contained")
            if key == "$id" and not root:
                _fail(f"{where} has a $id below the root")
            if key == "$ref":
                if not isinstance(value, str) or not value.startswith("#"):
                    _fail(f"{where} has an external $ref: {value!r}")
                refs.append(value)
            if key in _DATA_KEYWORDS:
                continue
            if key in _NAMED_KEYWORDS and isinstance(value, dict):
                for subschema in value.values():
                    walk(subschema, False)
            else:
                walk(value, False)

    walk(schema, True)
    base = schema.get("$id", "")
    registry = Registry().with_resource(base, DRAFT202012.create_resource(schema))
    resolver = registry.resolver(base_uri=base)
    for ref in refs:
        try:
            target = resolver.lookup(ref).contents
        except Unresolvable:
            _fail(f"{where} has an unresolvable $ref: {ref}")
        if not isinstance(target, bool) and id(target) not in schema_nodes:
            _fail(f"{where} has a $ref into data, not to a subschema: {ref}")
    return Draft202012Validator(schema, registry=registry)


def _schemas(document: dict, directory: Path | None, given: Mapping[str, Any] | None,
             where: str) -> dict[str, tuple[Any, Draft202012Validator]]:
    declared = document.get("schemas", {})
    if not isinstance(declared, dict):
        _fail(f"{where}.schemas must be an object")
    result = {}
    for name, path in declared.items():
        match = _SCHEMA_NAME.match(name) if isinstance(name, str) else None
        if match is None:
            _fail(f"{where}.schemas keys must be <name>/<n>: {name!r}")
        expected = f"schemas/{match.group(1)}-{match.group(2)}.json"
        if path != expected:
            _fail(f"{where}.schemas[{name}] must be {expected}")
        if directory is not None:
            file = directory / path
            if not file.is_file() or not file.resolve().is_relative_to(directory.resolve()):
                _fail(f"{where}: schema file {path} is missing")
            schema = _read(file)
        elif given is not None and name in given:
            schema = given[name]
        else:
            _fail(f"{where}: schema {name} has no document")
        result[name] = (schema, _compile_schema(schema, f"{where} schema {name}"))
    return result


def scenario_digest(document: Mapping[str, Any], schema_documents: Mapping[str, Any]) -> str:
    """The descriptor's digest: its canonical JSON without ``digest``, plus the canonical JSON of every declared
    schema document by name (spec 2026-10-01 §2.3) — a schema change is a scenario change."""
    body = {key: value for key, value in document.items() if key != "digest"}
    if schema_documents:
        body["schema_documents"] = {name: schema_documents[name] for name in sorted(schema_documents)}
    return catalogue_digest(body)


def _establishes(step: dict, group: frozenset) -> bool:
    """Whether one of the (already validated) outputs of ``step`` is in ``group`` of COMPUTED_SOURCES."""
    for output in step["outputs"]:
        (kind, body), = output.items()
        key = (kind, (body["kind"], body["outcome"])) if kind == "operation" else (kind, body.get("path"))
        if key in group:
            return True
    return False


def _validate(document: Any, directory: Path | None,
              given: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Draft202012Validator]]:
    _keys(document, _DESCRIPTOR, _DESCRIPTOR_OPTIONAL, "descriptor")
    if document["schema"] != SCENARIO_SCHEMA:
        _fail("unsupported descriptor schema")
    if not isinstance(document["id"], str) or not _ID.match(document["id"]):
        _fail("descriptor id is malformed")
    where = f"scenario {document['id']}"
    _positive(document["version"], f"{where}.version")
    _text(document["title"], f"{where}.title")
    if not isinstance(document["skill"], str) or not _SKILL.match(document["skill"]):
        _fail(f"{where}.skill must be SKILL.md with an optional anchor")
    if document["subject"] not in ("implementation", "project"):
        _fail(f"{where}.subject must be implementation or project")
    if document["repeatable"] not in (None, {"cycle": "operation.campaign"}):
        _fail(f"{where}.repeatable must be null or the campaign cycle")
    schemas = _schemas(document, directory, given, where)
    steps = document["steps"]
    if not isinstance(steps, list) or not steps:
        _fail(f"{where}.steps must be a non-empty list")
    if document["repeatable"] is not None and any(
            isinstance(step, dict) and isinstance(step.get("outputs"), list)
            and any(isinstance(output, dict) and isinstance(output.get("artifact"), dict)
                    and output["artifact"].get("check") == "schema" for output in step["outputs"])
            for step in steps):
        # A started cycle takes its documents from the cycle's Bundle, which does not validate them (user decision
        # 2026-10-01, codex r2): v1 keeps schema checks out of repeatable scenarios.
        _fail(f"{where}: a repeatable scenario cannot declare an artifact with check schema")
    claims: dict[str, list[dict]] = {}
    for index, step in enumerate(steps):
        at = f"{where}.steps[{index}]"
        _keys(step, _STEP, _STEP_OPTIONAL, at)
        if step["kind"] not in KINDS:
            _fail(f"{at}.kind is unknown")
        if not isinstance(step.get("optional", False), bool):
            _fail(f"{at}.optional must be a boolean")
        outputs = step["outputs"]
        if not isinstance(outputs, list) or not outputs:
            _fail(f"{at}.outputs must be a non-empty list")
        for position, output in enumerate(outputs):
            claim = _output(output, step["kind"], document["subject"], schemas, f"{at}.outputs[{position}]")
            if claim is not None:
                claims.setdefault(claim, []).append(step)
    facts = COMPUTED_FACTS | set(claims)
    seen: list[str] = []
    optional: set[str] = set()
    for index, step in enumerate(steps):
        at = f"{where}.steps[{index}]"
        if not isinstance(step["id"], str) or not _ID.match(step["id"]) or step["id"] in seen:
            _fail(f"{at}.id must be a unique lowercase id")
        if step["id"] in facts:
            _fail(f"{at}.id must not be a fact name")
        _text(step["title"], f"{at}.title")
        if not isinstance(step["human_only"], bool) or (step["human_only"] and step["kind"] not in _HUMAN_ONLY_KINDS):
            _fail(f"{at}.human_only is allowed only on operation and confirm steps")
        for name in _names(step["requires"], f"{at}.requires"):
            if name not in facts and name not in seen:
                _fail(f"{at}.requires names neither a fact nor a previous step: {name}")
            if name in optional:
                _fail(f"{at}.requires names an optional step: {name}")
        if not isinstance(step["skill"], str) or not _SKILL.match(step["skill"]):
            _fail(f"{at}.skill must be SKILL.md with an optional anchor")
        seen.append(step["id"])
        if step.get("optional", False):
            optional.add(step["id"])
    for name in ("requires", "produces"):
        for fact in _names(document[name], f"{where}.{name}"):
            if fact not in facts:
                _fail(f"{where}.{name} names an unknown fact: {fact}")
    for fact in document["produces"]:
        groups = ([[step for step in steps if _establishes(step, group)] for group in COMPUTED_SOURCES[fact]]
                  if fact in COMPUTED_SOURCES else [claims.get(fact, [])])
        if any(sources and all(step.get("optional", False) for step in sources) for sources in groups):
            _fail(f"{where}.produces {fact} comes only from optional steps")
    if not isinstance(document["digest"], str) or not _DIGEST.match(document["digest"]):
        _fail(f"{where}.digest must be 64 lowercase hex characters")
    if scenario_digest(document, {name: pair[0] for name, pair in schemas.items()}) != document["digest"]:
        _fail(f"{where}: digest does not match the descriptor")
    if directory is not None and not (directory / document["skill"].split("#", 1)[0]).is_file():
        _fail(f"{where}: skill file is missing")
    return document, {name: pair[1] for name, pair in schemas.items()}


def validate_descriptor(document: Any, *, directory: Path | None,
                        schema_documents: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate one ``lab-scenario/2`` descriptor; schema documents come from ``directory`` or, without one,
    from ``schema_documents``."""
    return _validate(document, directory, schema_documents)[0]


def _pointer(parts) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _pointer_key(parts) -> tuple:
    """Document order of an error location: array indices compare as numbers (``/2`` before ``/10``)."""
    return tuple((0, part, "") if isinstance(part, int) else (1, 0, str(part)) for part in parts)


def _deeper_than(document: Any, bound: int) -> bool:
    """Whether arrays and objects nest more than ``bound`` levels; iterative, so any parsed document is safe."""
    pending = [(document, 0)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, (list, dict)):
            if depth >= bound:
                return True
            pending.extend((child, depth + 1) for child in (node.values() if isinstance(node, dict) else node))
    return False


@dataclass(frozen=True)
class ScenarioCatalogue:
    catalogue: dict[str, Any]
    descriptors: tuple[dict[str, Any], ...]
    base: Path
    validators: Mapping[str, dict[str, Any]] = field(default_factory=dict, compare=False, repr=False)

    def descriptor(self, scenario_id: str) -> dict[str, Any] | None:
        return next((d for d in self.descriptors if d["id"] == scenario_id), None)

    def skill_path(self, scenario_id: str) -> Path:
        descriptor = self.descriptor(scenario_id)
        if descriptor is None:
            raise KeyError(scenario_id)
        return self.base / scenario_id / descriptor["skill"].split("#", 1)[0]

    def schema_path(self, scenario_id: str, name: str) -> Path | None:
        """The schema file as shipped (``lab scenarios schema``, the schema route); ``None`` when not declared."""
        descriptor = self.descriptor(scenario_id)
        declared = {} if descriptor is None else descriptor.get("schemas", {})
        return None if name not in declared else self.base / scenario_id / declared[name]

    def check_artifact(self, scenario_id: str, name: str, document: Any) -> list[dict[str, str]]:
        """Instance errors of ``document`` against the scenario's schema, at most ``MAX_INVALID``, ordered by JSON
        Pointer (array indices numerically). A document nested deeper than ``MAX_ARTIFACT_DEPTH`` is one instance
        error at the root. Any other failure of the check itself is the schema's: ``SchemaUnusable``."""
        validator = self.validators[scenario_id][name]
        if _deeper_than(document, MAX_ARTIFACT_DEPTH):
            return [{"path": "", "message": "nested too deeply"}]
        try:
            found = [(tuple(error.absolute_path), error.message) for error in validator.iter_errors(document)]
        except Exception as error:
            raise SchemaUnusable(name) from error
        found.sort(key=lambda item: (_pointer_key(item[0]), item[1]))
        return [{"path": _pointer(parts), "message": message[:300]} for parts, message in found[:MAX_INVALID]]

    def entry_payload(self, scenario_id: str) -> dict[str, Any]:
        path = self.skill_path(scenario_id)
        return {"schema": "lab-scenario-entry/1", "descriptor": self.descriptor(scenario_id), "skill_path": str(path)}

    def list_payload(self) -> dict[str, Any]:
        return {"schema": "lab-scenario-list/1", "catalogue": self.catalogue,
                "scenarios": [{"descriptor": d, "skill_path": str(self.skill_path(d["id"]))} for d in self.descriptors]}


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"scenario catalogue: unreadable {path.name}") from error


def read_catalogue(base: Path) -> ScenarioCatalogue:
    document = _read(base / "catalogue.json")
    if not isinstance(document, dict) or set(document) != _TOP:
        _fail("catalogue keys must be exactly " + ", ".join(sorted(_TOP)))
    if document["schema"] != CATALOGUE_SCHEMA:
        _fail("unsupported catalogue schema")
    _positive(document["catalogue_version"], "catalogue_version")
    if not isinstance(document["digest"], str) or not _DIGEST.match(document["digest"]):
        _fail("digest must be 64 lowercase hex characters")
    entries = document["scenarios"]
    if not isinstance(entries, list) or not entries:
        _fail("scenarios must be a non-empty list")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != _ENTRY:
            _fail(f"scenarios[{index}] keys must be exactly digest, id, version")
    ids = [entry["id"] for entry in entries]
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        _fail("scenarios must be unique and ordered by id")
    on_disk = sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    if on_disk != ids:
        _fail("scenario directories do not match the catalogue")
    descriptors, validators = [], {}
    for entry in entries:
        directory = base / entry["id"]
        descriptor, compiled = _validate(_read(directory / "scenario.json"), directory, None)
        if (descriptor["id"], descriptor["version"], descriptor["digest"]) != (entry["id"], entry["version"], entry["digest"]):
            _fail(f"catalogue entry {entry['id']} does not match its descriptor")
        descriptors.append(descriptor)
        validators[descriptor["id"]] = compiled
    if catalogue_digest(document) != document["digest"]:
        _fail("digest does not match the catalogue")
    return ScenarioCatalogue(document, tuple(descriptors), base, validators)
