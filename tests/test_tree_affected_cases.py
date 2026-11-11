"""Conservative component selection, including screened declaration evidence."""

from types import SimpleNamespace

import pytest

from evolution.tree.affected_cases import (
    SampleProvenance,
    TreeChanges,
    known_maps,
    parent_mode,
    select_affected,
)

PUBLIC = {"public": {"sha256": "a" * 64, "sources": ["a.txt"]}}
BASELINE = {
    "c1": (SampleProvenance(("public",), "d" * 64, True),),
    "c2": (SampleProvenance((), "d" * 64, True),),
}


def _known(components=PUBLIC, withheld=0):
    payload = {"components_digest": "d" * 64, "components": components}
    if withheld:
        payload["components_withheld"] = withheld
    return known_maps(
        [SimpleNamespace(event="components_declared", payload=payload)],
        BASELINE,
        "run",
        lambda _: None,
    )


def _select(**overrides):
    inputs = dict(
        kind="reported",
        corpus_ids=("c1", "c2"),
        baseline=BASELINE,
        known=_known(),
        changes=TreeChanges(("a.txt",), False),
        labels={},
        candidate_map=PUBLIC,
        inspection_failed=False,
    )
    inputs.update(overrides)
    return select_affected(**inputs)


def test_parent_map_with_withheld_component_selects_every_case():
    known = _known(withheld=1)
    assert parent_mode(BASELINE, known) == ("component", "d" * 64, "components_withheld")
    selection = _select(known=known, candidate_map=None)
    assert selection.cases == ("c1", "c2")
    assert selection.fallback_reason == "components_withheld"
    assert known["d" * 64] == (PUBLIC, 1)


def test_candidate_map_with_withheld_component_selects_every_case():
    selection = _select(candidate_withheld=1)
    assert selection.cases == ("c1", "c2")
    assert selection.fallback_reason == "components_withheld"


def test_codex_r1_counterexample():
    candidate = {**PUBLIC, "secret-tool": {"sha256": "b" * 64, "sources": ["a.txt"]}}
    original = _select(candidate_map=candidate)
    assert original.fallback_reason == "component_added_or_removed"
    assert original.cases == ("c1", "c2")
    screened = _select(candidate_map=PUBLIC, candidate_withheld=1)
    assert screened.fallback_reason == "components_withheld"
    assert screened.cases == ("c1", "c2")
    assert screened.unknown_cases == ()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"inspection_failed": True}, "inspection_failed"),
        ({"candidate_map": None}, "components_unstable"),
        ({"candidate_map": {}}, "components_withheld"),
        ({"changes": TreeChanges(("uncovered.txt",), False)}, "components_withheld"),
    ],
)
def test_fallback_precedence(overrides, reason):
    selection = _select(candidate_withheld=1, **overrides)
    assert selection.fallback_reason == reason
    assert selection.cases == ("c1", "c2")


def test_parent_fallback_precedence():
    unstable = {**BASELINE, "c2": (SampleProvenance((), "e" * 64, True),)}
    assert parent_mode(unstable, _known(withheld=1))[2] == "components_unstable"
    assert parent_mode(BASELINE, {})[2] == "components_unavailable"


@pytest.mark.parametrize(
    ("overrides", "mode", "changed", "cases", "unknown", "reason"),
    [
        ({}, "component", ["a.txt"], [], [], None),
        (
            {"candidate_map": {"public": {"sha256": "b" * 64, "sources": ["a.txt"]}}},
            "component",
            ["a.txt", "public"],
            [0],
            [],
            None,
        ),
        (
            {"candidate_map": {}},
            "component",
            ["a.txt", "public"],
            [0, 1],
            [],
            "component_added_or_removed",
        ),
        ({"inspection_failed": True}, "component", ["a.txt"], [0, 1], [], "inspection_failed"),
        ({"candidate_map": None}, "component", ["a.txt"], [0, 1], [], "components_unstable"),
        ({"kind": "none"}, None, ["a.txt"], [0, 1], [], "none"),
        (
            {"kind": "metadata", "labels": {"c1": ["a.txt"], "c2": []}},
            None,
            ["a.txt"],
            [0],
            [],
            None,
        ),
    ],
)
def test_no_withheld_selection_unchanged(overrides, mode, changed, cases, unknown, reason):
    assert _select(**overrides).payload(
        1, "r01-a01", overrides.get("kind", "reported"), ("c1", "c2")
    ) == {
        "round": 1,
        "attempt": "r01-a01",
        "source": overrides.get("kind", "reported"),
        "mode": mode,
        "changed": changed,
        "cases": cases,
        "unknown_cases": unknown,
        "fallback_reason": reason,
    }
