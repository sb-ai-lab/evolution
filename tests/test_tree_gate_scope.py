"""The round's exam scope, derived once for the executor and the audit (ADR-0021)."""

from __future__ import annotations

from evolution.tree.gate_scope import derive_gate_scope

CORPUS = ["a", "b", "c", "d", "e", "f"]
FAMILIES = {"a": "q1", "b": "q1", "c": "q1", "d": "q2", "e": "q3"}  # f has no family


def scope(groups, *, failed=False, fallback=("d", "f"), families=FAMILIES, evaluable=None):
    return derive_gate_scope(
        reflection_failed=failed,
        groups=groups,
        fallback=list(fallback),
        family_membership=families,
        corpus_order=CORPUS,
        evaluable_cases=evaluable,
    )


def test_claim_expands_to_whole_families_after_the_selection():
    s = scope([{"affected_cases": ["a", "d"]}])
    assert (s.source, s.claimed_cases, s.selected_cases) == ("claim", ("a", "d"), ("a", "d"))
    assert s.cases == ("a", "d", "b", "c")


def test_single_case_counts_the_claim_before_expansion():
    s = scope([{"affected_cases": ["a"]}])
    assert (s.source, s.claimed_cases, s.selected_cases) == ("single-case", ("a",), ("d", "f"))
    assert s.cases == ("d", "f")  # q2 has only d; f has no family


def test_empty_claim_and_failed_reflection_fall_back_and_expand():
    assert scope([]).source == "empty-claim"
    s = scope([], failed=True, fallback=("a",))
    assert (s.source, s.claimed_cases, s.selected_cases, s.cases) == (
        "reflection-failed",
        (),
        ("a",),
        ("a", "b", "c"),
    )


def test_no_membership_means_no_expansion():
    s = scope([{"affected_cases": ["a", "d"]}], families=None)
    assert s.cases == s.selected_cases == ("a", "d")


def test_unevaluable_companions_are_named():
    s = scope([{"affected_cases": ["a", "d"]}], evaluable=frozenset({"a", "d", "c"}))
    assert s.unevaluable_cases == ("b",)


def test_without_membership_every_source_keeps_the_family_seven_scope():
    """Families 5-7 (and `families: none`): `cases` is exactly the pre-ADR-0021 scope."""

    for families in (None, {}):
        claim = scope(
            [{"affected_cases": ["d", "a"]}, {"affected_cases": ["a"]}], families=families
        )
        assert (claim.source, claim.cases) == ("claim", ("a", "d"))
        single = scope([{"affected_cases": ["a"]}], families=families)
        assert (single.source, single.cases) == ("single-case", ("d", "f"))
        empty = scope([], families=families)
        assert (empty.source, empty.cases) == ("empty-claim", ("d", "f"))
        failed = scope([], failed=True, families=families)
        assert (failed.source, failed.cases, failed.unevaluable_cases) == (
            "reflection-failed",
            ("d", "f"),
            (),
        )
