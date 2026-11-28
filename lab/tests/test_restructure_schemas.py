"""The five restructure schemas (v2 by fragments: map, goal, port spec, verify; scope/1) — self-contained, and what
they accept and refuse."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolution_lab import guides
from evolution_lab.guides._scenarios import _compile_schema
from restructure_samples import QUESTION, fresh

SCHEMAS = Path(guides.__file__).parent / "scenarios" / "restructure" / "schemas"
NAMES = {"scope": "scope-1.json", "map": "behavior-map-2.json", "goal": "goal-2.json", "port_spec": "port-spec-2.json",
         "verify": "verify-2.json"}


def validator(name: str):
    return _compile_schema(json.loads((SCHEMAS / NAMES[name]).read_text(encoding="utf-8")), name)


def errors(name: str, document) -> list[str]:
    return sorted(error.json_path for error in validator(name).iter_errors(document))


@pytest.mark.parametrize("name", list(NAMES))
def test_the_sample_passes(name):
    assert errors(name, fresh(name)) == []


@pytest.mark.parametrize("path", ["docs/*.md", "#notes", "!x", "..foo", "a/b..c", "dir/"])
def test_a_hide_path_is_literal_so_pattern_characters_are_allowed(path):
    document = fresh("scope"); document["hide"][0]["path"] = path
    assert errors("scope", document) == []


@pytest.mark.parametrize("path", ["", "/etc", "..", "../x", "a/../b", "a/.."])
def test_a_hide_path_outside_the_place_is_refused(path):
    document = fresh("scope"); document["hide"][0]["path"] = path
    assert errors("scope", document) == ["$.hide[0].path"]


@pytest.mark.parametrize("source, ok", [("app/x.py#fn", True), ("app/x.py", True), ("#fn", False),
                                        ("a/../b#y", False), ("/abs.py", False), ("app/x.py#", False)])
def test_a_source_reference_is_a_relative_path_with_an_optional_symbol(source, ok):
    document = fresh("map"); document["tools"][0]["sources"] = [source]
    assert errors("map", document) == ([] if ok else ["$.tools[0].sources[0]"])


@pytest.mark.parametrize("name, change, where", [
    ("scope", lambda d: d.pop("hide"), "$"),
    ("scope", lambda d: d.update(extra=1), "$"),
    ("map", lambda d: d["scenarios"][0].pop("id"), "$.scenarios[0]"),
    ("map", lambda d: d["tools"][0].update(id="has space"), "$.tools[0].id"),
    ("map", lambda d: d["scenarios"][0].update(sources=[]), "$.scenarios[0].sources"),
    ("map", lambda d: d["scenarios"][0].update(share={"value": 1.5, "basis": "x"}), "$.scenarios[0].share"),
    ("map", lambda d: d["scenarios"][0].update(share={"value": 0.5}), "$.scenarios[0].share"),
    ("map", lambda d: d["scenarios"][0]["outcomes"][0].update(kind="success"), "$.scenarios[0].outcomes[0].kind"),
    ("map", lambda d: d["assumptions"][0].update(n=0), "$.assumptions[0].n"),
    ("verify", lambda d: d["layers"][0].update(evidence=[]), "$.layers[0].evidence"),
    ("verify", lambda d: d["layers"][1].update(counts=None), "$.layers[1].counts"),
    ("verify", lambda d: d.update(layers=[l for l in d["layers"] if l["kind"] != "claims_vs_wiring"]), "$.layers"),
    ("verify", lambda d: d.update(port_commit="HEAD"), "$.port_commit"),
    ("verify", lambda d: d["failures"][0].update(blame="model"), "$.failures[0].blame"),
    ("verify", lambda d: d["layers"][0].update(status="ok"), "$.layers[0].status"),
])
def test_a_broken_document_is_refused_where_it_breaks(name, change, where):
    document = fresh(name); change(document)
    assert where in errors(name, document)


@pytest.mark.parametrize("name, change, where", [
    ("map", lambda d: d.update(questions=[dict(QUESTION, id=f"q{n}") for n in range(6)]), "$.questions"),
    ("map", lambda d: d["questions"][0].update(answer={"option": "yes"}), "$.questions[0].answer"),
    ("map", lambda d: d["architecture"].update(notes=[{"id": f"n{n}", "text": "x"} for n in range(4)]),
     "$.architecture.notes"),
    ("goal", lambda d: d["skills"]["links"][0].update(method="rewrite"), "$.skills.links[0].method"),
    ("goal", lambda d: d["skills"]["links"][0]["from"][0].update(kind="file"), "$.skills.links[0].from[0].kind"),
    ("goal", lambda d: d["modes"]["modes"][0].update(id="cloud"), "$.modes.modes[0].id"),
    ("port_spec", lambda d: d["stages"][0].update(cells=["kitchen"]), "$.stages[0].cells[0]"),
    ("port_spec", lambda d: d["notes"][0].update(section="notes"), "$.notes[0].section"),
    ("verify", lambda d: d.pop("accepting"), "$"),
])
def test_v2_refusals(name, change, where):
    document = fresh(name); change(document)
    assert where in errors(name, document)


@pytest.mark.parametrize("name", ["map", "goal", "port_spec"])
def test_a_written_empty_section_is_valid(name):
    document = fresh(name)
    for key in {"map": ("routing",), "goal": ("changes", "questions"), "port_spec": ("rules", "notes")}[name]:
        document[key] = {"rules": [], "risk": None} if key == "routing" else []
    assert errors(name, document) == []


def test_an_answer_carries_the_question_digest():
    document = fresh("map")
    document["questions"][0]["answer"] = {"option": "no", "for": "sha256:" + "0" * 64}
    assert errors("map", document) == []
    document["questions"][0]["answer"] = {"text": "only chat", "for": "sha256:" + "0" * 64}
    assert errors("map", document) == []


def test_goal_carries_the_template_card():
    schema = json.loads((SCHEMAS / "goal-2.json").read_text(encoding="utf-8"))
    card = schema["x-template"]
    assert card["name"] == "sylph"
    assert set(card["cells"]) == {"prompt", "skills", "tools", "texts", "handler", "deps", "modes", "changes",
                                  "questions"}
    assert {kind["id"] for kind in card["how"]["kinds"]} == {"evolve", "code"}


def test_a_deferred_or_not_run_layer_needs_no_evidence():
    document = fresh("verify")
    document["layers"][0].update(status="not_run", evidence=[], counts=None, run_at=None)
    assert errors("verify", document) == []


@pytest.mark.parametrize("name, change, where", [
    ("verify", lambda d: d.update(port_commit="a" * 40 + "\n"), "$.port_commit"),
    ("verify", lambda d: d["layers"][0].update(id="l\n"), "$.layers[0].id"),
    ("scope", lambda d: d["hide"][0].update(path="a\n"), "$.hide[0].path"),
    ("map", lambda d: d["tools"][0].update(sources=["app/x.py\n"]), "$.tools[0].sources[0]"),
])
def test_a_trailing_newline_does_not_pass_an_anchored_pattern(name, change, where):
    document = fresh(name); change(document)
    assert where in errors(name, document)


MAP_ITEMS = [("architecture", "numbers"), ("architecture", "chain"), ("routing", "rules"), (None, "outcomes"),
             (None, "assumptions")]


def _map_item(document, parent, key):
    return (document[parent] if parent else document)[key][0]


@pytest.mark.parametrize("parent, key", MAP_ITEMS)
def test_a_map_item_may_carry_sources_or_not(parent, key):
    document = fresh("map")
    assert errors("map", document) == []  # the sample shows the full form
    _map_item(document, parent, key).pop("sources")
    assert errors("map", document) == []
    _map_item(document, parent, key)["sources"] = ["app/x.py#fn", "app/y.py:10-40"]
    assert errors("map", document) == []


@pytest.mark.parametrize("bad", [[], "app/x.py", ["#fn"], ["/abs.py"], ["a/../b"], [1]])
@pytest.mark.parametrize("parent, key", MAP_ITEMS)
def test_a_map_item_with_malformed_sources_is_refused(parent, key, bad):
    document = fresh("map"); _map_item(document, parent, key)["sources"] = bad
    prefix = f"$.{parent}.{key}[0]" if parent else f"$.{key}[0]"
    assert any(e.startswith(prefix + ".sources") for e in errors("map", document))


def _skill_examples():
    """The one-line JSON examples of SKILL.md ("- `path[]`: `{...}`") with the document of the step they stand in."""
    import re
    skill = (SCHEMAS.parent / "SKILL.md").read_text(encoding="utf-8")
    document, found = None, []
    for line in skill.splitlines():
        if line.startswith("## Step `"):
            document = {"analysis": "map", "goal": "goal"}.get(line.split("`")[1])
        match = re.match(r"^- `([a-z_.]+)\[\]`: `(\{.*\})`$", line)
        if match and document:
            found.append((document, match.group(1), match.group(2)))
    return found


def test_skill_examples_are_found():
    assert {(d, p) for d, p, _ in _skill_examples()} >= {
        ("map", "architecture.numbers"), ("map", "scenarios"), ("map", "assumptions"), ("goal", "prompt"),
        ("goal", "skills.links"), ("goal", "tools.links"), ("goal", "texts")}


@pytest.mark.parametrize("document, path, text", _skill_examples())
def test_every_skill_example_passes_its_fragment_subschema(document, path, text):
    from jsonschema import Draft202012Validator
    root = json.loads((SCHEMAS / NAMES[document]).read_text(encoding="utf-8"))
    node = root
    for key in path.split("."):
        node = node["properties"][key]
    sub = {"$defs": root["$defs"], **node["items"]}
    assert [e.json_path for e in Draft202012Validator(sub).iter_errors(json.loads(text))] == []
