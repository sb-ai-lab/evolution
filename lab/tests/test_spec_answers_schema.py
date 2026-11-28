"""Schema spec-answers/1 of the connect scenario (spec 2026-10-03 §2, PDR-0022)."""
from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from evolution_lab import guides
from evolution_lab.guides import answers

CATALOGUE = guides.requirements()
SCHEMA = answers.spec_answers_schema(CATALOGUE)
VALIDATOR = Draft202012Validator(SCHEMA)


def errors(document):
    return sorted((("/" + "/".join(map(str, e.absolute_path))) if e.absolute_path else "", e.message)
                  for e in VALIDATOR.iter_errors(document))


def complete(path="campaign"):
    return answers.complete_answers(CATALOGUE, implementation="agent", path=path)


def test_the_shipped_file_equals_the_generated_schema():
    shipped = answers.schema_file().read_text(encoding="utf-8")
    assert shipped == answers.render(SCHEMA), (
        "the requirements catalogue changed: run `python -m evolution_lab.guides.answers`, "
        "then bump connect `version` in scenario.json and recompute its digest, bump `catalogue_version` and "
        "recompute the scenario catalogue digest (docs/api/projects.md, 'Digest'), and update the digests in docs")


def test_generation_is_deterministic_and_self_contained():
    assert answers.render(answers.spec_answers_schema(CATALOGUE)) == answers.render(SCHEMA)
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert SCHEMA["properties"]["catalogue"]["properties"]["digest"] == {"const": CATALOGUE["digest"]}


@pytest.mark.parametrize("path", ["campaign", "evaluation"])
def test_a_complete_document_is_valid(path):
    assert errors(complete(path)) == []


def test_required_items_follow_obligation_and_path():
    for path in CATALOGUE["paths"]:
        assert answers.required_items(CATALOGUE, path) == [
            i["id"] for i in CATALOGUE["items"] if i["obligation"] == "required" and path in i["applies_to"]]


def test_required_item_counts_are_pinned():
    assert len(answers.required_items(CATALOGUE, "campaign")) == 24
    assert len(answers.required_items(CATALOGUE, "evaluation")) == 14


def test_required_items_are_scoped_by_path():
    document = complete("evaluation")
    document["items"].pop("campaign.scale", None)
    assert errors(document) == []
    document = complete("evaluation")
    del document["items"]["eval.corpus"]
    assert ("/items", "'eval.corpus' is a required property") in errors(document)


def test_a_missing_required_item_names_it():
    document = complete()
    del document["items"]["agent.config"]
    assert ("/items", "'agent.config' is a required property") in errors(document)


def test_an_unknown_item_id_is_rejected():
    document = complete()
    document["items"]["agent.confg"] = {"status": "unknown"}
    assert ("/items", "Additional properties are not allowed ('agent.confg' was unexpected)") in errors(document)


def test_an_open_required_item_is_not_closed():
    document = complete()
    document["items"]["agent.config"] = {"status": "needs_decision"}
    assert ("/items/agent.config/status", "'known' was expected") in errors(document)


def test_an_optional_item_may_stay_open():
    optional = next(i["id"] for i in CATALOGUE["items"] if i["obligation"] == "optional")
    document = complete()
    document["items"][optional] = {"status": "unknown"}
    assert errors(document) == []


@pytest.mark.parametrize("path", ["campaign", "evaluation"])
@pytest.mark.parametrize("status", ["unknown", "needs_decision", "known"])
def test_failure_tolerance_accepts_open_or_free_text_answers_on_both_paths(path, status):
    document = complete(path)
    document["items"]["run.failures"] = {"status": status}
    if status == "known":
        document["items"]["run.failures"].update(answer="retry — С повтором", source={"kind": "decision"})
    assert errors(document) == []


def test_v12_answers_are_rejected_without_being_rewritten():
    document = complete()
    document["catalogue"] = {"version": 12, "digest": "76b289d498e4e169a00fc8bc68c3338dffb5cee790144eb243662373d7338100"}
    document["items"].pop("run.failures", None)
    before = copy.deepcopy(document)
    assert [path for path, _ in errors(document)] == ["/catalogue/digest", "/catalogue/version"]
    assert document == before


@pytest.mark.parametrize("ref, old_digest, current_digest", [
    ("requirements", "76b289d498e4e169a00fc8bc68c3338dffb5cee790144eb243662373d7338100", CATALOGUE["digest"]),
    ("scenarios", "28d15994535b1973b8f44bf0f4ba415d7c6629245f1398a6e974646c0091c976", guides.scenarios().catalogue["digest"]),
])
def test_v12_catalogue_linked_confirmations_become_catalogue_changed(ref, old_digest, current_digest):
    from evolution_lab.projects.facts import compute_facts
    from evolution_lab.projects.models import Confirmation
    from test_projects_facts import ctx

    linked = {"kind": "catalogue", "ref": ref, "digest": old_digest}
    confirmation = Confirmation("c-1", "acq", None, "spec.confirmed", {"path": "SPEC.md", "digest": "sha256:1"},
                                (linked,), "cli", "operator", None, "2026-10-30T10:00:00Z", None)
    before = copy.deepcopy(confirmation)
    facts = compute_facts(ctx(confirmations=(confirmation,), confirmation_now={"c-1": {
        "attests": "sha256:1", "linked": [{**linked, "digest": current_digest}]}}))
    fact = next(f for f in facts if f.id == "spec.confirmed")
    assert not fact.holds and fact.stale_reason.codes == ("catalogue_changed",)
    assert confirmation == before


def test_a_stale_catalogue_names_the_current_digest():
    document = complete()
    document["catalogue"]["digest"] = "0" * 64
    assert ("/catalogue/digest", f"'{CATALOGUE['digest']}' was expected") in errors(document)


def test_known_needs_an_answer():
    document = complete()
    del document["items"]["agent.config"]["answer"]
    assert ("/items/agent.config", "'answer' is a required property") in errors(document)


def test_restructure_needs_reasons_and_as_is_may_have_none():
    document = complete()
    document["fit"].update(verdict="restructure", reasons=[])
    assert ("/fit/reasons", "[] should be non-empty") in errors(document)
    document["fit"].update(verdict="as_is")
    assert errors(document) == []


def test_fit_is_required_with_implementation_and_summary():
    document = complete()
    del document["fit"]
    assert ("", "'fit' is a required property") in errors(document)
    document = complete()
    del document["fit"]["implementation"], document["fit"]["summary"]
    found = errors(document)
    assert ("/fit", "'implementation' is a required property") in found
    assert ("/fit", "'summary' is a required property") in found


def test_client_is_open_on_both_levels_and_nothing_else_is():
    document = complete()
    document["client"] = {"current": "campaign.scale", "survey": {"anything": [1, {"x": None}]}}
    document["items"]["agent.config"]["client"] = {"form": "options", "options": [{"id": "a"}]}
    assert errors(document) == []
    document["current"] = "campaign.scale"
    assert ("", "Additional properties are not allowed ('current' was unexpected)") in errors(document)


def test_source_kind_comes_from_the_catalogue():
    kinds = SCHEMA["$defs"]["item"]["properties"]["source"]["properties"]["kind"]["enum"]
    assert kinds == [k for k in guides.ANSWERS if any(k in i["answer_from"] for i in CATALOGUE["items"])]


def test_a_new_catalogue_changes_the_schema():
    other = copy.deepcopy(CATALOGUE)
    other["catalogue_version"] += 1
    other["digest"] = "1" * 64
    assert answers.render(answers.spec_answers_schema(other)) != answers.render(SCHEMA)


def test_the_implementation_grammar_matches_lab_ids():
    document = complete()
    document["fit"]["implementation"] = "Agent_1"
    assert [p for p, _ in errors(document)] == ["/fit/implementation"]


def test_the_implementation_grammar_rejects_a_final_newline():
    document = complete()
    document["fit"]["implementation"] = "agent\n"
    assert [p for p, _ in errors(document)] == ["/fit/implementation"]
