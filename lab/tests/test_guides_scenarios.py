"""Part 2 (spec §3.9): the scenario catalogue is read like the requirements catalogue."""
from __future__ import annotations

import copy
import json
import re
import shlex
import shutil
from pathlib import Path

import pytest

from evolution_lab import guides
from evolution_lab.guides import _scenarios
from evolution_lab.guides._scenarios import COMPUTED_FACTS, read_catalogue, scenario_digest, validate_descriptor

BASE = Path(guides.__file__).parent / "scenarios"

FENCE = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.S)
GUIDE_REF = re.compile(r"`evolution-lab-(?!scenario-)([a-z-]+)(?:#([a-z0-9-]+))?`")
ANCHOR = re.compile(r'<a id="([a-z0-9-]+)"></a>')


def _skill(name: str) -> str:
    return (BASE / name / "SKILL.md").read_text(encoding="utf-8")


def step_sections(descriptor: dict, text: str) -> dict[str, str]:
    """Split a scenario skill only on the anchors of the descriptor's steps; inner anchors stay in their step."""
    ids = [step["id"] for step in descriptor["steps"]]
    parts = re.split(r'<a id="(%s)"></a>' % "|".join(re.escape(i) for i in ids), text)
    return dict(zip(parts[1::2], parts[2::2]))


def lab_commands() -> list[tuple[str, str]]:
    found = []
    for descriptor in guides.scenarios().descriptors:
        for block in FENCE.findall(_skill(descriptor["id"])):
            found += [(descriptor["id"], line.strip()) for line in block.splitlines() if line.strip().startswith("lab ")]
    return found


def test_every_lab_command_in_scenario_skills_parses():
    from evolution_lab.cli.parser import build_parser
    commands = lab_commands()
    assert len(commands) >= 20
    for scenario, line in commands:
        build_parser().parse_args(shlex.split(line)[1:])  # UsageError on any unknown flag or missing argument


def test_every_guide_reference_resolves():
    names = {info.path.split("/")[0] for info in guides.list_guides()}
    for descriptor in guides.scenarios().descriptors:
        references = GUIDE_REF.findall(_skill(descriptor["id"]))
        assert references, descriptor["id"]
        for name, anchor in references:
            assert name in names, (descriptor["id"], name)
            if anchor:
                text = (guides.root() / name / "SKILL.md").read_text(encoding="utf-8")
                assert anchor in ANCHOR.findall(text), (descriptor["id"], name, anchor)


def test_every_step_section_says_goal_and_done_when():
    for descriptor in guides.scenarios().descriptors:
        sections = step_sections(descriptor, _skill(descriptor["id"]))
        assert list(sections) == [s["id"] for s in descriptor["steps"]]
        for step in descriptor["steps"]:
            section = sections[step["id"]]
            assert "Goal:" in section and "Done when:" in section, (descriptor["id"], step["id"])


def test_human_only_steps_describe_confirmation_at_ask():
    for descriptor in guides.scenarios().descriptors:
        sections = step_sections(descriptor, _skill(descriptor["id"]))
        for step in descriptor["steps"]:
            section = sections[step["id"]]
            assert ("Needs the human's confirmation at `ask`" in section) == step["human_only"], (
                descriptor["id"], step["id"])


def _descriptor(name: str) -> dict:
    return json.loads((BASE / name / "scenario.json").read_text(encoding="utf-8"))


def _connect_schemas() -> dict:
    return {name: json.loads((BASE / "connect" / path).read_text(encoding="utf-8"))
            for name, path in _descriptor("connect")["schemas"].items()}


def _redigest(document: dict) -> dict:
    document["digest"] = guides.catalogue_digest(document)
    return document


def _redigest_descriptor(document: dict) -> dict:
    """A descriptor digest covers its declared schema documents (spec 2026-10-01 §2.3)."""
    document["digest"] = scenario_digest(document, _connect_schemas())
    return document


def test_catalogue_lists_four_scenarios():
    catalogue = guides.scenarios()
    assert catalogue.catalogue["schema"] == "lab-scenario-catalogue/1"
    assert catalogue.catalogue["catalogue_version"] == 12
    assert [d["id"] for d in catalogue.descriptors] == ["connect", "create-skill", "evolve", "restructure"]
    assert [(e["id"], e["version"], e["digest"]) for e in catalogue.catalogue["scenarios"]] == [
        (d["id"], d["version"], d["digest"]) for d in catalogue.descriptors]
    assert catalogue.catalogue["digest"] == guides.catalogue_digest(catalogue.catalogue)


def test_connect_and_evolve_match_the_spec():
    catalogue = guides.scenarios()
    connect, evolve = catalogue.descriptor("connect"), catalogue.descriptor("evolve")
    assert (connect["requires"], connect["produces"], connect["repeatable"]) == (
        ["implementation.available"], ["connected"], None)
    assert [(s["id"], s["kind"], s["human_only"], s["requires"]) for s in connect["steps"]] == [
        ("spec", "work", False, []), ("spec-confirm", "confirm", False, ["spec"]),
        ("package", "work", False, []), ("check", "operation", False, ["package"]),
        ("conformance", "operation", False, ["package"]),
        ("baseline", "operation", True, ["check", "conformance"])]
    assert (evolve["requires"], evolve["produces"], evolve["repeatable"]) == (
        ["connected"], ["campaigns"], {"cycle": "operation.campaign"})
    assert [(s["id"], s["kind"], s["human_only"], s["optional"], s["requires"], s["outputs"]) for s in evolve["steps"]] == [
        ("plan", "decide", False, False, [], [{"artifact": {"path": "@manifest", "check": "manifest"}}]),
        ("prepare", "operation", False, False, ["plan"], [{"operation": {"kind": "prepare.campaign", "outcome": "prepared"}}]),
        ("run", "operation", True, False, ["prepare"], [{"operation": {"kind": "campaign", "outcome": "terminal"}}]),
        ("result", "operation", False, True, ["run"], [{"operation": {"kind": "export", "outcome": "done"}}])]
    assert (connect["version"], evolve["version"]) == (8, 2)
    assert all(d["schema"] == "lab-scenario/2" for d in catalogue.descriptors)
    assert connect["schemas"] == {"spec-answers/1": "schemas/spec-answers-1.json"} and evolve["schemas"] == {}
    assert [s["optional"] for s in connect["steps"]] == [False] * 6
    assert {d["subject"] for d in catalogue.descriptors} == {"implementation"}


def test_payloads_carry_absolute_skill_paths():
    catalogue = guides.scenarios()
    listing = catalogue.list_payload()
    assert listing["schema"] == "lab-scenario-list/1" and listing["catalogue"] == catalogue.catalogue
    assert [item["skill_path"] for item in listing["scenarios"]] == [
        str(BASE / "connect" / "SKILL.md"), str(BASE / "create-skill" / "SKILL.md"), str(BASE / "evolve" / "SKILL.md"),
        str(BASE / "restructure" / "SKILL.md")]
    assert all(Path(item["skill_path"]).is_absolute() for item in listing["scenarios"])
    entry = catalogue.entry_payload("evolve")
    assert entry == {"schema": "lab-scenario-entry/1", "descriptor": catalogue.descriptor("evolve"),
                     "skill_path": str(BASE / "evolve" / "SKILL.md")}
    with pytest.raises(KeyError):
        catalogue.entry_payload("nope")


def test_computed_facts_match_the_projects_vocabulary():
    from evolution_lab.projects.facts import COMPUTED
    assert COMPUTED_FACTS == frozenset(COMPUTED)


def test_skill_names_follow_the_naming_rule():
    for info in guides.list_guides():
        assert info.name == "evolution-lab-" + info.path.split("/")[0], info
    for descriptor in guides.scenarios().descriptors:
        text = (BASE / descriptor["id"] / "SKILL.md").read_text(encoding="utf-8")
        assert guides._frontmatter(text)["name"] == "evolution-lab-scenario-" + descriptor["id"]
        assert guides._frontmatter(text)["description"]


def test_list_guides_excludes_scenario_skills():
    found = guides.list_guides()
    assert found
    assert not any(info.path.startswith("scenarios/") for info in found)


def test_every_step_skill_anchor_exists():
    anchor = re.compile(r'<a id="([a-z0-9-]+)"></a>')
    for descriptor in guides.scenarios().descriptors:
        text = (BASE / descriptor["id"] / "SKILL.md").read_text(encoding="utf-8")
        found = set(anchor.findall(text))
        for step in descriptor["steps"]:
            name, _, target = step["skill"].partition("#")
            assert name == "SKILL.md" and target in found, (descriptor["id"], step["id"])


MUTATIONS = [
    lambda d: d.update(extra=1),
    lambda d: d.update(schema="lab-scenario/1"),
    lambda d: d.update(version=0),
    lambda d: d.update(id="Connect"),
    lambda d: d.update(subject="team"),
    lambda d: d.update(repeatable={"cycle": "operation.evaluation"}),
    lambda d: d.update(requires=["unknown.fact"]),
    lambda d: d["steps"][0].update(kind="run"),
    lambda d: d["steps"][0].update(human_only=True),  # only operation or confirm steps may be human-only
    lambda d: d["steps"][1].update(requires=["baseline"]),  # a later step, not a previous one
    lambda d: d["steps"][1].update(requires=["spec", "spec"]),
    lambda d: d["steps"][0].update(outputs=[]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "../SPEC.md", "check": "exists"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "@package", "check": "exists"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "SPEC.md", "check": "exists"}, "operation": {}}]),
    lambda d: d["steps"][1].update(outputs=[{"attestation": {"claim": "connected", "path": "SPEC.md"}}]),
    lambda d: d["steps"][1].update(outputs=[{"attestation": {"claim": "spec", "path": "SPEC.md"}}]),
    lambda d: d["steps"][3].update(outputs=[{"operation": {"kind": "check", "outcome": "measured"}}]),
    lambda d: d["steps"][0].update(outputs=[{"operation": {"kind": "check", "outcome": "passed"}}]),  # work step
    lambda d: d["steps"][3].update(id="spec"),
    lambda d: d["steps"][3].update(id="check.passed"),
    lambda d: d["steps"][0].update(skill="README.md#spec"),
    lambda d: d["steps"][0].update(optional="yes"),
    lambda d: d["steps"][2].update(optional=True),  # check and conformance require package
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "docs/{project}.md", "check": "exists"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "docs/}.md", "check": "exists"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "@{implementation}", "check": "exists"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "SPEC.md", "check": "schema"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "SPEC.md", "check": "schema", "schema": "map/1"}}]),
    lambda d: d["steps"][0].update(outputs=[{"artifact": {"path": "SPEC.md", "check": "exists", "schema": "map/1"}}]),
    lambda d: d.update(schemas=[]),
    lambda d: d.update(schemas={"Map/1": "schemas/Map-1.json"}),
    lambda d: d.update(schemas={"map/1": "elsewhere/map.json"}),
    lambda d: d.update(extra_optional=True),
]


@pytest.mark.parametrize("mutate", MUTATIONS)
def test_closed_descriptor_schema_rejects(mutate):
    document = copy.deepcopy(_descriptor("connect"))
    mutate(document)
    # the schema documents are supplied, so a rejection is the mutation's own, never "has no document"
    with pytest.raises(ValueError) as caught:
        validate_descriptor(_redigest_descriptor(document), directory=None, schema_documents=_connect_schemas())
    message = str(caught.value)
    assert "has no document" not in message
    assert "digest does not match" not in message  # nor the digest-mismatch rejection: the mutation's own rule fires


def test_unmutated_connect_validates_with_its_schema_documents():
    document = _descriptor("connect")
    assert validate_descriptor(document, directory=None, schema_documents=_connect_schemas()) == document


def test_descriptor_digest_mismatch_rejects():
    document = _descriptor("evolve")  # connect carries a schema document, which `directory=None` cannot supply
    document["title"] = "Changed"
    with pytest.raises(ValueError, match="digest"):
        validate_descriptor(document, directory=None)


def test_descriptor_skill_file_must_exist(tmp_path):
    with pytest.raises(ValueError, match="skill"):
        validate_descriptor(_descriptor("evolve"), directory=tmp_path)


def _copy(tmp_path: Path) -> Path:
    base = tmp_path / "scenarios"
    shutil.copytree(BASE, base)
    return base


def test_catalogue_must_match_the_directories_on_disk(tmp_path):
    base = _copy(tmp_path)
    shutil.copytree(base / "evolve", base / "extra")
    with pytest.raises(ValueError, match="directories"):
        read_catalogue(base)


def test_catalogue_entry_must_match_its_descriptor(tmp_path):
    base = _copy(tmp_path)
    catalogue = json.loads((base / "catalogue.json").read_text())
    catalogue["scenarios"][0]["version"] += 1
    (base / "catalogue.json").write_text(json.dumps(_redigest(catalogue)))
    with pytest.raises(ValueError):
        read_catalogue(base)


def test_catalogue_digest_mismatch_rejects(tmp_path):
    base = _copy(tmp_path)
    catalogue = json.loads((base / "catalogue.json").read_text())
    catalogue["catalogue_version"] += 1
    (base / "catalogue.json").write_text(json.dumps(catalogue))
    with pytest.raises(ValueError, match="digest"):
        read_catalogue(base)


def test_unreadable_catalogue_is_a_value_error(tmp_path):
    base = _copy(tmp_path)
    (base / "catalogue.json").write_text("{")
    with pytest.raises(ValueError, match="unreadable"):
        read_catalogue(base)


def test_scenarios_do_not_import_core_or_projects():
    import os, subprocess, sys
    code = ("import sys, evolution_lab.guides as g; g.scenarios();"
            "assert not any(m == 'evolution' or m.startswith('evolution.') for m in sys.modules);"
            "assert 'evolution_lab.projects' not in sys.modules")
    env = {**os.environ, "PYTHONPATH": str(Path(guides.__file__).parents[2]) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_evolve_prepare_shows_the_full_campaign_request():
    from evolution_lab.service.codecs import PREPARE_REQUEST_SCHEMA, decode_prepare_request
    block = _skill("evolve").split('<a id="prepare"></a>', 1)[1].split('<a id="run"></a>', 1)[0]
    for key in ('"kind": "campaign"', '"bundle_digest"', '"stop_grace_seconds"', '"cleanup_wait_seconds"'):
        assert key in block
    raw = re.search(r"```json\n(.*?)```", block, re.S).group(1)
    example = json.loads(raw.replace("<digest from lab project bundle>", "a" * 64))
    decode_prepare_request({"schema": PREPARE_REQUEST_SCHEMA, **example})


from scenario_fixtures import BEHAVIOR_MAP, RESTRUCTURE, SKILL, write_catalogue


def _with(descriptor=None, schema=None):
    descriptor = copy.deepcopy(RESTRUCTURE if descriptor is None else descriptor)
    return {"restructure": (descriptor, {"schemas/behavior-map-1.json": BEHAVIOR_MAP if schema is None else schema},
                            SKILL)}


def test_the_restructure_shaped_scenario_loads(tmp_path):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))
    descriptor = catalogue.descriptor("restructure")
    assert descriptor["schemas"] == {"behavior-map/1": "schemas/behavior-map-1.json"}
    assert catalogue.schema_path("restructure", "behavior-map/1") == tmp_path / "c" / "restructure" / "schemas" / "behavior-map-1.json"
    assert catalogue.schema_path("restructure", "other/1") is None and catalogue.schema_path("nope", "behavior-map/1") is None


def test_placeholder_needs_an_implementation_subject(tmp_path):
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["subject"] = "project"
    descriptor["requires"] = []
    with pytest.raises(ValueError, match="implementation_placeholder_without_subject"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(descriptor)))


@pytest.mark.parametrize("kind", ["decide", "work"])
def test_human_only_is_refused_on_decide_and_work(tmp_path, kind):
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["steps"][0]["kind"], descriptor["steps"][0]["human_only"] = kind, True
    with pytest.raises(ValueError, match="human_only"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(descriptor)))


def test_human_only_is_allowed_on_confirm(tmp_path):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))
    assert catalogue.descriptor("restructure")["steps"][1]["human_only"] is True


def test_a_required_step_cannot_be_optional(tmp_path):
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["steps"][0]["optional"] = True  # map-confirm requires map
    with pytest.raises(ValueError, match="optional"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(descriptor)))


def test_the_only_source_of_a_produced_fact_cannot_be_optional(tmp_path):
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["steps"][1]["optional"] = True  # the only attestation of restructure.map_confirmed
    with pytest.raises(ValueError, match="optional"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(descriptor)))


def test_an_undeclared_schema_is_refused(tmp_path):
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["steps"][0]["outputs"][0]["artifact"]["schema"] = "behavior-map/2"
    with pytest.raises(ValueError, match="undeclared schema"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(descriptor)))


def test_a_missing_schema_file_is_refused(tmp_path):
    base = write_catalogue(tmp_path / "c")
    (base / "restructure" / "schemas" / "behavior-map-1.json").unlink()
    with pytest.raises(ValueError, match="schema file"):
        read_catalogue(base)


@pytest.mark.parametrize("schema, message", [
    ({"type": 3}, "not a valid 2020-12 schema"),
    ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}, "draft 2020-12"),
    ({"$ref": "other.json"}, "external \\$ref"),
    ({"properties": {"a": {"$ref": "https://example.com/s.json#/x"}}}, "external \\$ref"),
    ({"$ref": "#/$defs/missing"}, "unresolvable \\$ref"),
    ({"properties": {"a": {"$ref": "#nope"}}}, "unresolvable \\$ref"),
    ({"properties": {"a": {"$dynamicRef": "#x"}}}, "\\$dynamicRef"),
    ({"$defs": {"x": {"$dynamicAnchor": "x"}}}, "\\$dynamicAnchor"),
    ({"properties": {"a": {"$id": "https://example.com/a", "type": "string"}}}, "\\$id below the root"),
    ({"pattern": "("}, "not a valid 2020-12 schema"),
])
def test_a_schema_that_is_not_self_contained_is_a_catalogue_error(tmp_path, schema, message):
    with pytest.raises(ValueError, match=message):
        read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))


@pytest.mark.parametrize("schema", [
    # codex r2: the outer $ref resolves, but its target is const data holding an external reference
    {"$ref": "#/$defs/payload/const", "$defs": {"payload": {"const": {"$ref": "https://example.invalid/external"}}}},
    {"$ref": "#/$defs/p/enum/0", "$defs": {"p": {"enum": [{"type": "string"}]}}},
    {"$ref": "#/$defs/p/default", "$defs": {"p": {"default": {"type": "string"}}}},
    {"$ref": "#/$defs/p/examples/0", "$defs": {"p": {"examples": [{"type": "string"}]}}},
    {"properties": {"a": {"$ref": "#/dependentRequired"}}, "dependentRequired": {"a": ["b"]}},
    {"$ref": "#/properties", "properties": {"a": {"type": "string"}}},  # the name level, not a subschema
    {"$ref": "#/$defs", "$defs": {"a": {"type": "string"}}},
    {"$ref": "#/allOf", "allOf": [{"type": "string"}]},  # an array of subschemas is not one
    {"$ref": "#/$defs/p/const/0", "$defs": {"p": {"const": [{"$ref": "#"}]}}},
])
def test_a_ref_into_data_is_a_catalogue_error(tmp_path, schema):
    with pytest.raises(ValueError, match="\\$ref into data"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))


def test_refs_to_subschemas_load(tmp_path):
    schema = {"type": "object",
              "properties": {"a": {"$ref": "#/$defs/a~1b"}, "b": {"$ref": "#/allOf/0"},
                             "c": {"$ref": "#/$defs/list/items"}, "d": {"$ref": "#/$defs/never"},
                             "e": {"$ref": "#/$defs/%24pct"}},
              "allOf": [{"type": "object"}],
              "$defs": {"a/b": {"type": "integer"}, "list": {"items": {"type": "string"}}, "never": False,
                        "$pct": {"type": "boolean"}}}
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    assert catalogue.check_artifact("restructure", "behavior-map/1", {"a": 1, "c": "x", "e": True}) == []
    assert [e["path"] for e in catalogue.check_artifact("restructure", "behavior-map/1", {"a": "x", "d": 1})] == [
        "/a", "/d"]


def test_a_repeatable_scenario_cannot_check_an_artifact_by_schema(tmp_path):
    """User decision 2026-10-01 (codex r2): a started cycle reads its artifacts from the cycle's Bundle, which does
    not validate them; v1 therefore refuses schema-checked outputs in repeatable scenarios."""
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor["repeatable"] = {"cycle": "operation.campaign"}
    with pytest.raises(ValueError, match="scenario restructure.*repeatable.*check schema"):
        read_catalogue(write_catalogue(tmp_path / "c", _with(descriptor=descriptor)))
    descriptor["steps"][0]["outputs"] = [{"artifact": {"path": "restructure/{implementation}/behavior-map.json",
                                                        "check": "exists"}}]
    assert read_catalogue(write_catalogue(tmp_path / "d", _with(descriptor=descriptor))).descriptor("restructure")


def test_names_and_data_that_look_like_references_are_not_references(tmp_path):
    schema = {"type": "object",
              "properties": {"$ref": {"type": "string"}, "$id": {"type": "string"},
                             "kind": {"const": {"$ref": "elsewhere.json"}}},
              "$defs": {"$id": {"type": "integer"}}}
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    assert catalogue.check_artifact("restructure", "behavior-map/1", {"$ref": "x"}) == []


def test_a_root_id_and_anchors_resolve(tmp_path):
    schema = {"$id": "https://example.com/map.json", "$ref": "#/$defs/top",
              "$defs": {"top": {"type": "object", "properties": {"a": {"$ref": "#item"}}},
                        "item": {"$anchor": "item", "type": "integer"}}}
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    assert catalogue.check_artifact("restructure", "behavior-map/1", {"a": 1}) == []
    assert catalogue.check_artifact("restructure", "behavior-map/1", {"a": "x"}) == [
        {"path": "/a", "message": "'x' is not of type 'integer'"}]


def test_check_artifact_reports_at_most_five_json_pointers_in_order(tmp_path):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))
    document = {"schema": "restructure-behavior-map/1",
                "behaviors": [{"id": "", "source": 1, "x": 0}, {"id": 2}, {"source": 3}, {"id": "ok", "source": "s"}]}
    errors = catalogue.check_artifact("restructure", "behavior-map/1", document)
    assert len(errors) == 5
    assert [e["path"] for e in errors] == sorted(e["path"] for e in errors)
    assert all(set(e) == {"path", "message"} and e["path"].startswith("/behaviors/") for e in errors)
    assert catalogue.check_artifact("restructure", "behavior-map/1",
                                    {"schema": "restructure-behavior-map/1", "behaviors": []}) == []


def test_a_pointer_escapes_tilde_and_slash(tmp_path):
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    assert catalogue.check_artifact("restructure", "behavior-map/1", {"a/b~c": "x"})[0]["path"] == "/a~1b~0c"


def test_a_failing_validator_is_schema_unusable(tmp_path):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c"))

    class Broken:
        def iter_errors(self, document):
            raise RuntimeError("validator bug")
    catalogue.validators["restructure"]["behavior-map/1"] = Broken()
    with pytest.raises(_scenarios.SchemaUnusable):
        catalogue.check_artifact("restructure", "behavior-map/1", {})


def test_a_schema_change_changes_the_scenario_and_catalogue_digests(tmp_path):
    first = read_catalogue(write_catalogue(tmp_path / "a"))
    changed = copy.deepcopy(BEHAVIOR_MAP)
    changed["properties"]["note"] = {"type": "string"}
    second = read_catalogue(write_catalogue(tmp_path / "b", _with(schema=changed)))
    assert first.descriptor("restructure")["digest"] != second.descriptor("restructure")["digest"]
    assert first.catalogue["digest"] != second.catalogue["digest"]


def test_an_edited_schema_file_breaks_the_descriptor_digest(tmp_path):
    base = write_catalogue(tmp_path / "c")
    path = base / "restructure" / "schemas" / "behavior-map-1.json"
    path.write_text(json.dumps({**BEHAVIOR_MAP, "title": "edited"}), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        read_catalogue(base)


def test_substitute_and_inside_root():
    assert _scenarios.substitute("restructure/{implementation}/map.json", "port-a") == "restructure/port-a/map.json"
    assert _scenarios.substitute("SPEC.md", None) == "SPEC.md"
    with pytest.raises(ValueError):
        _scenarios.substitute("a/{implementation}", None)
    assert _scenarios.inside_root("a/b.json") and not _scenarios.inside_root("../a")
    assert not _scenarios.inside_root("/a") and not _scenarios.inside_root("@manifest") and not _scenarios.inside_root("")


def test_optional_steps_say_so():
    for descriptor in guides.scenarios().descriptors:
        sections = step_sections(descriptor, _skill(descriptor["id"]))
        for step in descriptor["steps"]:
            assert ("**Optional.**" in sections[step["id"]]) == step["optional"], (descriptor["id"], step["id"])


def _nested(depth: int):
    document: list = []
    for _ in range(depth):
        document = [document]
    return document


def test_a_too_deep_document_against_a_recursive_schema_is_an_instance_error(tmp_path):
    schema = {"type": "array", "items": {"$ref": "#"}}
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    assert catalogue.check_artifact("restructure", "behavior-map/1", _nested(50)) == []
    assert catalogue.check_artifact("restructure", "behavior-map/1", _nested(300)) == [
        {"path": "", "message": "nested too deeply"}]


def test_a_self_looping_schema_is_still_schema_unusable(tmp_path):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema={"$ref": "#"})))
    with pytest.raises(_scenarios.SchemaUnusable):
        catalogue.check_artifact("restructure", "behavior-map/1", {})


def test_array_errors_are_ordered_by_numeric_index(tmp_path):
    schema = {"type": "array", "items": {"type": "integer"}}
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    errors = catalogue.check_artifact("restructure", "behavior-map/1", ["x"] * 12)
    assert [e["path"] for e in errors] == ["/0", "/1", "/2", "/3", "/4"]


@pytest.mark.parametrize("schema", [{"dependentRequired": {"$ref": ["a"]}}, {"dependentRequired": {"$id": ["a"]}}])
def test_dependent_required_names_are_data(tmp_path, schema):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _with(schema=schema)))
    assert catalogue.check_artifact("restructure", "behavior-map/1", {"$ref": 1, "a": 2}) == []


def _operations_scenario(produces, steps):
    """A restructure-shaped catalogue whose steps are operation steps (codex r1 of the branch,
    optional-computed-fact-source): ``steps`` is ``[(id, kind, outcome, optional)]``."""
    descriptor = copy.deepcopy(RESTRUCTURE)
    descriptor.update(produces=produces, schemas={})
    descriptor["steps"] = [
        {"id": step_id, "title": step_id, "kind": "operation", "human_only": False, "optional": optional,
         "requires": [], "outputs": [{"operation": {"kind": kind, "outcome": outcome}}], "skill": "SKILL.md"}
        for step_id, kind, outcome, optional in steps]
    return {"restructure": (descriptor, {}, SKILL)}


@pytest.mark.parametrize("produces, steps", [
    (["baseline.measured"], [("baseline", "evaluation", "measured", True)]),
    (["campaigns"], [("run", "campaign", "terminal", True)]),
    # `connected` needs all three operation facts: an optional sole source of any of them is refused.
    (["connected"], [("check", "check", "passed", False), ("conformance", "conformance", "passed", False),
                     ("baseline", "evaluation", "measured", True)]),
])
def test_an_optional_step_cannot_be_the_only_source_of_a_produced_computed_fact(tmp_path, produces, steps):
    with pytest.raises(ValueError, match=f"produces {produces[0]} comes only from optional steps"):
        read_catalogue(write_catalogue(tmp_path / "c", _operations_scenario(produces, steps)))


def test_an_optional_step_may_source_a_computed_fact_a_required_step_also_sources(tmp_path):
    catalogue = read_catalogue(write_catalogue(tmp_path / "c", _operations_scenario(
        ["baseline.measured"], [("baseline", "evaluation", "measured", False),
                                ("again", "evaluation", "measured", True)])))
    assert [s["optional"] for s in catalogue.descriptor("restructure")["steps"]] == [False, True]


def test_computed_fact_sources_match_the_status_mapping():
    """guides stays free of the projects import, so a test pins its copy of the status mapping."""
    from evolution_lab.projects import facts, status
    sources = _scenarios.COMPUTED_SOURCES
    for pair, fact in status._FACT_OF.items():
        assert sources[fact] == (frozenset({("operation", pair)}),)
    assert sources["connected"] == tuple(sources[name][0] for name in facts.OPERATION_FACTS)
    assert sources["package.valid"] == (frozenset({("artifact", "@package")}),)
    assert set(sources) <= COMPUTED_FACTS


P = "restructure/{implementation}/"


def test_restructure_matches_the_spec():
    restructure = guides.scenarios().descriptor("restructure")
    assert (restructure["version"], restructure["subject"], restructure["requires"], restructure["produces"],
            restructure["repeatable"]) == (2, "implementation", ["implementation.ported"],
                                           ["restructure.port_accepted"], None)
    assert restructure["schemas"] == {"behavior-map/2": "schemas/behavior-map-2.json", "goal/2": "schemas/goal-2.json",
                                      "port-spec/2": "schemas/port-spec-2.json", "scope/1": "schemas/scope-1.json",
                                      "verify/2": "schemas/verify-2.json"}
    assert [(s["id"], s["kind"], s["human_only"], s["optional"], s["requires"]) for s in restructure["steps"]] == [
        ("analysis", "decide", False, False, []),
        ("analysis-confirm", "confirm", True, False, ["analysis"]),
        ("goal", "decide", False, False, ["analysis-confirm"]),
        ("brief", "decide", False, False, ["goal"]),
        ("brief-confirm", "confirm", True, False, ["brief"]),
        ("build", "work", False, False, ["brief-confirm", "implementation.available"]),
        ("verify", "work", False, False, ["build"]), ("accept", "confirm", True, False, ["verify"])]
    assert [s["outputs"] for s in restructure["steps"]] == [
        [{"artifact": {"path": P + "behavior-map.json", "check": "schema", "schema": "behavior-map/2"}}],
        [{"attestation": {"claim": "restructure.map_confirmed", "path": P + "behavior-map.json"}}],
        [{"artifact": {"path": P + "goal.json", "check": "schema", "schema": "goal/2"}}],
        [{"artifact": {"path": P + "port-spec.json", "check": "schema", "schema": "port-spec/2"}},
         {"artifact": {"path": P + "scope.json", "check": "schema", "schema": "scope/1"}}],
        [{"attestation": {"claim": "restructure.brief_confirmed", "path": P + "scope.json"}}],
        [{"artifact": {"path": P + "BUILD_REPORT.md", "check": "exists"}}],
        [{"artifact": {"path": P + "verify.json", "check": "schema", "schema": "verify/2"}}],
        [{"attestation": {"claim": "restructure.port_accepted", "path": P + "verify.json"}}]]
    assert [s["skill"] for s in restructure["steps"]] == [f"SKILL.md#{s['id']}" for s in restructure["steps"]]


@pytest.mark.parametrize("schema, sample", [("scope/1", "scope"), ("behavior-map/2", "map"), ("verify/2", "verify"),
                                            ("goal/2", "goal"), ("port-spec/2", "port_spec")])
def test_restructure_samples_pass_the_catalogue_check(schema, sample):
    from restructure_samples import fresh
    assert guides.scenarios().check_artifact("restructure", schema, fresh(sample)) == []


def test_restructure_skill_names_its_documents_and_links_every_input():
    text = _skill("restructure")
    for name in ("behavior-map.json", "goal.json", "port-spec.json", "scope.json", "BUILD_REPORT.md", "verify.json"):
        assert f"restructure/<port>/{name}" in text, name
    for anchor in ("analysis", "analysis-confirm", "goal", "brief", "brief-confirm", "build", "verify", "accept"):
        assert f'<a id="{anchor}"></a>' in text, anchor
    analysis_confirm = text.split('<a id="analysis-confirm"></a>', 1)[1].split('<a id="goal"></a>', 1)[0]
    assert "--link place:implementation:<source>" in analysis_confirm
    brief_confirm = text.split('<a id="brief-confirm"></a>', 1)[1].split('<a id="build"></a>', 1)[0]
    assert "--path restructure/<port>/scope.json" in brief_confirm
    for link in ("file:restructure/<port>/behavior-map.json", "file:restructure/<port>/goal.json",
                 "file:restructure/<port>/port-spec.json", "place:implementation:<source>"):
        assert f"--link {link}" in brief_confirm, link
    accept = text.split('<a id="accept"></a>', 1)[1]
    for link in ("file:restructure/<port>/goal.json", "file:restructure/<port>/scope.json",
                 "place:implementation:<source>", "file:restructure/<port>/behavior-map.json",
                 "file:restructure/<port>/port-spec.json", "file:restructure/<port>/BUILD_REPORT.md",
                 "place:implementation:<port>"):
        assert f"--link {link}" in accept, link
    assert "rsync -a --safe-links --exclude=.git" in text and "--exclude-from" not in text
    for gone in ("GOAL.md", "PORT_SPEC.md", "scope-confirm", "behavior-confirm", "port-spec-confirm"):
        assert gone not in text, gone


def test_connect_skill_carries_the_answers_procedure_and_the_port_choice():
    text = (BASE / "connect" / "SKILL.md").read_text(encoding="utf-8")
    assert '<a id="connect-or-port"></a>' in text
    for needle in ("spec.answers.json", "lab scenarios schema connect spec-answers/1", "fit.implementation",
                   "as_is", "hard", "restructure", "lab project port", "evolution-lab-scenario-restructure",
                   "--link file:spec.answers.json", "agent.entrypoint", "agent.tools", "world.systems",
                   "agent.session_state", "agent.config", "Connecting as it is — realistic.",
                   "Connecting as it is — hard:", "I recommend porting onto a template:"):
        assert needle in text, needle
    spec = text[text.index('<a id="spec"></a>'):text.index('<a id="connect-or-port"></a>')]
    assert "Done when:" in spec
