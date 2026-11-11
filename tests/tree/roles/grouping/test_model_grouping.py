import dataclasses
import json

import pytest

from evolution.tree.roles.grouping import model_grouping
from evolution.tree.roles.grouping.model_grouping import (
    FallbackOutcome,
    InputTooLarge,
    apply_merges,
    build_bundle,
    build_case_profiles,
    chunk_bundles,
    covered,
    parse_map_response,
    parse_reduce_response,
    project,
    resolve_overlaps,
)
from evolution.tree.roles.grouping.reflect_grouping import (
    DEFAULT_FIELD_FEATURES,
    FieldFeature,
    GroupingRecord,
    _digest,
    build_registry,
    observation_signature,
    typed_json_key,
)


def rec(
    case,
    repeat,
    *,
    failing,
    available=True,
    terminal="completed",
    criteria=("c1",),
    score=0.0,
    answer="a",
):
    observation = {
        "terminal": terminal,
        "critical": not failing,
        "failed_criteria": list(criteria) if failing else [],
        "primary_score": score,
        "verdicts": [],
        "answer_text": [answer],
    }
    return GroupingRecord(
        f"{case}#{repeat}",
        case,
        repeat,
        failing,
        available,
        {"case": {}, "public": {}, "observation": observation, "feedback": {}},
    )


def registry(*rows):
    return build_registry("b" * 64, rows)


def test_profiles_count_every_repeat_and_skip_all_passing_cases():
    reg = registry(
        rec("a", 1, failing=True),
        rec("a", 2, failing=False),
        rec("a", 3, failing=True, available=False),
        rec("b", 1, failing=False),
    )
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=3)
    assert profile.case_id == "a"
    assert (profile.total, profile.passing, profile.failing, profile.unavailable) == (3, 1, 1, 1)
    assert profile.contrast == "a#2"


def test_representatives_prefer_frequent_signatures_then_fill():
    reg = registry(
        rec("a", 1, failing=True, criteria=("x",)),
        rec("a", 2, failing=True, criteria=("y",)),
        rec("a", 3, failing=True, criteria=("y",)),
        rec("a", 4, failing=True, criteria=("y",)),
    )
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=3)
    assert profile.representatives == ("a#2", "a#1", "a#3")
    assert profile.omitted_failing == 1


def test_metric_values_do_not_move_representatives():
    base = [
        rec("a", n, failing=True, criteria=("x",) if n % 2 else ("y",), score=0.1)
        for n in range(1, 7)
    ]
    moved = [
        rec("a", n, failing=True, criteria=("x",) if n % 2 else ("y",), score=0.9 - n / 10)
        for n in range(1, 7)
    ]
    one = build_case_profiles(registry(*base), metric_primary="score", representatives_per_case=2)
    two = build_case_profiles(registry(*moved), metric_primary="score", representatives_per_case=2)
    assert [p.representatives for p in one] == [p.representatives for p in two]


def test_bundle_keeps_every_failing_id_under_compaction():
    rows = [rec("a", n, failing=True, criteria=(f"c{n}",), answer="z" * 20000) for n in range(1, 9)]
    reg = registry(*rows)
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=8)
    bundle = build_bundle(profile, reg, max_chars=12000)
    assert len(bundle.text) <= 12000
    assert bundle.failing_ids == tuple(f"a#{n}" for n in range(1, 9))
    body = json.loads(bundle.text)
    assert [row["evidence_id"] for row in body["rows"]] == [f"a#{n}" for n in range(1, 9)]
    assert "[truncated]" in bundle.text or body["omitted_failing"] > 0


def test_bundle_too_large_even_compact():
    rows = [rec("a", n, failing=True) for n in range(1, 64)]
    reg = registry(*rows)
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    with pytest.raises(InputTooLarge):
        build_bundle(profile, reg, max_chars=500)


def test_bundle_drops_contrast_before_representatives():
    # One small failing row
    # plus a same-case passing contrast with a huge answer. At a size where
    # the representative and a (possibly shrunk) contrast cannot both fit,
    # the contrast is dropped first -- failing evidence outranks it.
    reg = registry(rec("a", 1, failing=True), rec("a", 2, failing=False, answer="z" * 20000))
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    bundle = build_bundle(profile, reg, max_chars=1000)
    assert len(bundle.text) <= 1000
    body = json.loads(bundle.text)
    assert body["contrast"] is None
    assert body["contrast_omitted"] is True
    assert [rep["evidence_id"] for rep in body["representatives"]] == ["a#1"]
    assert bundle.failing_ids == ("a#1",)


def test_bundle_keeps_compact_rows_when_representative_and_contrast_both_drop():
    # A size too small for the representative even after the contrast is
    # gone: only the compact rows survive, and every failing ID stays
    # covered.
    reg = registry(rec("a", 1, failing=True), rec("a", 2, failing=False, answer="z" * 20000))
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    bundle = build_bundle(profile, reg, max_chars=800)
    assert len(bundle.text) <= 800
    body = json.loads(bundle.text)
    assert body["representatives"] == []
    assert body["contrast"] is None
    assert body["contrast_omitted"] is True
    assert bundle.failing_ids == ("a#1",)
    assert [row["evidence_id"] for row in body["rows"]] == ["a#1", "a#2"]


def test_bundle_too_large_only_when_compact_rows_alone_exceed():
    # Below the compact-rows-only floor, InputTooLarge is correct even
    # though a representative and a passing contrast exist to drop --
    # they have already been dropped and it still does not fit.
    reg = registry(rec("a", 1, failing=True), rec("a", 2, failing=False, answer="z" * 20000))
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    with pytest.raises(InputTooLarge):
        build_bundle(profile, reg, max_chars=700)


def test_chunks_are_ordered_and_never_split_a_bundle():
    from evolution.tree.roles.grouping.model_grouping import CaseBundle

    bundles = [CaseBundle(c, "x" * 400, (f"{c}#1",)) for c in "abcde"]
    chunks = chunk_bundles(bundles, max_chars=1000)
    assert [[b.case_id for b in chunk] for chunk in chunks] == [["a", "b"], ["c", "d"], ["e"]]


# --- parse, admit, merge and project ---------------------------------


def group(local, members, *, title="t", cases=None, **extra):
    body = {
        "id": local,
        "title": title,
        "failure_mode": "fm",
        "expected_behaviour": "eb",
        "question": "q?",
        "members": [{"evidence_id": m, "variant": v} for m, v in members],
        "affected_cases": sorted(cases or {m.split("#")[0] for m, _ in members}),
        "citations": ["verdict:c1"],
        "priority": "p",
    }
    body.update(extra)
    return body


REG = registry(
    rec("a", 1, failing=True, criteria=("x",)),
    rec("a", 2, failing=True, criteria=("y",)),
    rec("a", 3, failing=False),
    rec("b", 1, failing=True, criteria=("x",)),
)
CASE_OF = {r.evidence_id: r.case_id for r in REG.records}
FAILING = ["a#1", "a#2", "b#1"]
FEATURES = [FieldFeature("c", "observation", "/failed_criteria", "criteria")]


def parse(groups, *, provenance_available=False):
    return parse_map_response(
        json.dumps({"groups": groups}),
        chunk_failing=FAILING,
        case_of=CASE_OF,
        provenance_available=provenance_available,
    )


def test_one_case_in_two_groups_and_two_cases_in_one():
    parsed = parse(
        [group("g1", [("a#1", "rule"), ("b#1", "rule")]), group("g2", [("a#2", "product")])]
    )
    assert parsed.defects == ()
    grouped, fallback = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
    )
    assert fallback is None
    by_id = {n.member_ids: n for n in grouped.neighborhoods}
    assert set(by_id) == {("a#1", "b#1"), ("a#2",)}
    assert grouped.result.assignments[2] is None  # a#3 passing
    assert dict(grouped.variants) == {0: "rule", 1: "product", 3: "rule"}


@pytest.mark.parametrize(
    "bad",
    [
        group("g", [("a#3", "v")]),  # passing member
        group("g", [("zz#1", "v")]),  # unknown
        group("g", [("a#1", "v"), ("a#1", "v")]),  # repeated in group
        group("g", [("a#1", "v")], cases=["a", "b"]),  # affected_cases mismatch
        group("g", [("a#1", "v")], root_cause="x"),  # forbidden key
        group("g", [("a#1", "v")], provenance="src/x.py"),  # invented provenance
        group("g", [("a#1", "")]),  # empty variant
        group("g", [("a#1", "v")], title="x" * 129),  # title too long
        group("g", [("a#1", "v")], citations=["c"] * 17),  # too many citations
        group("g", [("a#1", "v")], metrics={"score": True}),  # boolean metric
        group("g", []),  # no members
    ],
)
def test_invalid_groups_are_dropped_with_a_defect(bad):
    parsed = parse([bad])
    assert parsed.groups == () and parsed.defects


def test_defects_are_machine_codes_that_echo_only_short_identifiers():
    secretish = "leak " + "s" * 200
    parsed = parse([group("g", [(secretish, "v")]), group("h", [("a#1", "v")], fix="do x")])
    assert "unknown_member" in parsed.defects
    assert "forbidden_key:fix" in parsed.defects
    assert all(secretish not in defect and "do x" not in defect for defect in parsed.defects)
    long_id = "z" * 100
    parsed = parse([group("g", [(long_id, "v")])])
    assert f"unknown_member:{'z' * 64}" in parsed.defects


def test_incomplete_coverage_and_overlap_are_reported():
    parsed = parse([group("g1", [("a#1", "v")]), group("g2", [("a#1", "v"), ("b#1", "v")])])
    assert len(parsed.groups) == 2
    assert "overlap:a#1" in parsed.defects
    assert "uncovered:a#2" in parsed.defects


def test_provenance_is_admitted_only_when_available():
    body = [group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")], provenance="src/x.py")]
    assert parse(body, provenance_available=True).defects == ()
    nulled = [group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")], provenance=None)]
    assert parse(nulled).defects == ()


def test_response_is_the_first_object_after_a_fence():
    body = json.dumps({"groups": [group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")])]})
    text = "```json\n" + body + "\n```"
    parsed = parse_map_response(
        text, chunk_failing=FAILING, case_of=CASE_OF, provenance_available=False
    )
    assert parsed.defects == () and len(parsed.groups) == 1
    prose = "Here you go: " + body + " trailing {not json"
    parsed = parse_map_response(
        prose, chunk_failing=FAILING, case_of=CASE_OF, provenance_available=False
    )
    assert parsed.defects == ()


@pytest.mark.parametrize(
    ("text", "defect"),
    [("not json at all", "invalid_json"), ("[1, 2]", "not_an_object"), ('{"x": 1}', "no_groups")],
)
def test_unparseable_responses_name_one_defect(text, defect):
    parsed = parse_map_response(
        text, chunk_failing=FAILING, case_of=CASE_OF, provenance_available=False
    )
    assert parsed.groups == ()
    assert parsed.defects[0] == defect


def test_overlap_keeps_the_first_ranked_group():
    parsed = parse([group("g1", [("a#1", "v")]), group("g2", [("a#1", "v"), ("b#1", "v")])])
    kept = resolve_overlaps(parsed.groups)
    assert [g.members for g in kept] == [(("a#1", "v"), ("b#1", "v"))]


def test_overlap_trims_the_lower_ranked_group_and_its_cases():
    parsed = parse(
        [
            group("g1", [("a#1", "v"), ("a#2", "v")]),
            group("g2", [("a#1", "v"), ("b#1", "v")]),
        ]
    )
    kept = {g.local_id: g for g in resolve_overlaps(parsed.groups)}
    assert kept["g2"].members == (("a#1", "v"), ("b#1", "v"))
    assert kept["g1"].members == (("a#2", "v"),)
    assert kept["g1"].affected_cases == ("a",)
    assert covered(kept.values()) == frozenset(FAILING)


def test_partial_output_uses_fields_fallback_only_for_uncovered():
    parsed = parse([group("g1", [("a#1", "v"), ("b#1", "v")])])
    grouped, fallback = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=FEATURES,
        fallback=("uncovered", "invalid_output"),
        forbidden_string_predicates=(),
    )
    assert (fallback.kind, fallback.scope, fallback.reason) == (
        "fields",
        "uncovered",
        "invalid_output",
    )
    assert {n.member_ids for n in grouped.neighborhoods} == {("a#1", "b#1"), ("a#2",)}


def test_uncovered_fields_fallback_never_joins_a_model_group():
    # a#2 shares nothing with a#1's model group; b#1 shares a#1's criteria key
    # but a#1 is already covered, so b#1 forms its own fields group.
    parsed = parse([group("g1", [("a#1", "v")])])
    grouped, _ = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=FEATURES,
        fallback=("uncovered", "invalid_output"),
        forbidden_string_predicates=(),
    )
    assert {n.member_ids for n in grouped.neighborhoods} == {("a#1",), ("a#2",), ("b#1",)}


def test_uncovered_singletons_fallback():
    parsed = parse([group("g1", [("a#1", "v"), ("a#2", "w")])])
    grouped, fallback = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=None,
        fallback=("uncovered", "invalid_output"),
        forbidden_string_predicates=(),
    )
    assert fallback == FallbackOutcome("singletons", "uncovered", "invalid_output")
    assert {n.member_ids for n in grouped.neighborhoods} == {("a#1", "a#2"), ("b#1",)}


def test_absent_fallback_makes_exact_singletons():
    grouped, fallback = project(
        REG,
        (),
        policy_identity="p",
        fallback_features=None,
        fallback=("all", "provider_unavailable"),
        forbidden_string_predicates=(),
    )
    assert (fallback.kind, fallback.scope) == ("singletons", "all")
    assert sorted(n.member_ids for n in grouped.neighborhoods) == [("a#1",), ("a#2",), ("b#1",)]


def test_all_scope_fields_ignores_model_groups():
    parsed = parse([group("g1", [("a#1", "v"), ("a#2", "v")])])
    grouped, _ = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=FEATURES,
        fallback=("all", "provider_unavailable"),
        forbidden_string_predicates=(),
    )
    assert {n.member_ids for n in grouped.neighborhoods} == {("a#1", "b#1"), ("a#2",)}
    assert dict(grouped.variants) == {}


def test_project_without_fallback_requires_full_coverage():
    parsed = parse([group("g1", [("a#1", "v")])])
    with pytest.raises(ValueError, match="uncovered"):
        project(
            REG,
            parsed.groups,
            policy_identity="p",
            fallback_features=None,
            fallback=None,
            forbidden_string_predicates=(),
        )


def test_description_composition_and_empty_header_drop():
    parsed = parse(
        [
            group("g1", [("a#1", "v"), ("b#1", "v")], title="Wrong rule"),
            group("g2", [("a#2", "v")]),
        ]
    )
    g1, g2 = parsed.groups
    blank = dataclasses.replace(g2, title="", failure_mode="", expected_behaviour="", question="")
    grouped, _ = project(
        REG,
        (g1, blank),
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
    )
    by_id = {n.member_ids: n for n in grouped.neighborhoods}
    described = by_id[("a#1", "b#1")].description
    assert described.label == "Wrong rule"
    assert described.description == "Observed: fm\nExpected: eb\nQuestion: q?"
    assert by_id[("a#2",)].description is None


def test_description_is_cut_to_the_admission_limit():
    long = "x" * 512
    parsed = parse([group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")], failure_mode=long)])
    grouped, _ = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
    )
    assert len(grouped.neighborhoods[0].description.description) == 512


def test_secret_predicates_pass_through_to_admission():
    from evolution.tree.roles.grouping.reflect_grouping import GroupingAdmissionError

    parsed = parse([group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")], title="has SECRET")])
    with pytest.raises(GroupingAdmissionError, match="secret_detected"):
        project(
            REG,
            parsed.groups,
            policy_identity="p",
            fallback_features=None,
            fallback=None,
            forbidden_string_predicates=(("secret_detected", lambda s: "SECRET" in s),),
        )


def test_order_is_independent_of_response_order():
    g1, g2 = group("g1", [("a#1", "v"), ("b#1", "v")]), group("g2", [("a#2", "v")])
    kwargs = dict(
        policy_identity="p", fallback_features=None, fallback=None, forbidden_string_predicates=()
    )
    one, _ = project(REG, parse([g1, g2]).groups, **kwargs)
    two, _ = project(REG, parse([g2, g1]).groups, **kwargs)
    assert one.result == two.result


def test_merges_consume_whole_map_groups():
    parsed = parse(
        [group("m1", [("a#1", "v")]), group("m2", [("b#1", "v")]), group("m3", [("a#2", "v")])]
    )
    header = {
        "title": "t",
        "failure_mode": "fm",
        "expected_behaviour": "eb",
        "question": "q?",
        "priority": "p",
    }
    merges = parse_reduce_response(
        json.dumps({"merges": [{"groups": ["m1", "m2"], **header}]}), map_ids=["m1", "m2", "m3"]
    )
    applied = apply_merges(parsed.groups, merges)
    assert applied.skipped == 0
    merged = applied.groups
    assert sorted(len(g.members) for g in merged) == [1, 2]
    (joined,) = [g for g in merged if len(g.members) == 2]
    assert joined.affected_cases == ("a", "b")
    assert (
        parse_reduce_response(
            json.dumps({"merges": [{"groups": ["m1", "m1"]}]}), map_ids=["m1", "m2"]
        )
        is None
    )


@pytest.mark.parametrize(
    "merges",
    [
        [{"groups": ["m1"]}],  # fewer than two
        [{"groups": ["m1", "zz"]}],  # unknown map id
        [{"groups": ["m1", "m2"]}, {"groups": ["m2", "m3"]}],  # map id in two merges
        [{"groups": ["m1", "m2"], "fix": "x"}],  # forbidden key
        [{"groups": ["m1", "m2"], "title": "only a title"}],  # partial header
    ],
)
def test_invalid_reduce_responses(merges):
    text = json.dumps({"merges": merges})
    assert parse_reduce_response(text, map_ids=["m1", "m2", "m3"]) is None


def test_reduce_without_header_keeps_the_first_ranked_map_header():
    parsed = parse(
        [
            group("m1", [("a#1", "v")], title="one"),
            group("m2", [("a#2", "v"), ("b#1", "v")], title="two"),
        ]
    )
    merges = parse_reduce_response(
        json.dumps({"merges": [{"groups": ["m1", "m2"]}]}), map_ids=["m1", "m2"]
    )
    assert merges == ((("m1", "m2"), None),)
    (merged,) = apply_merges(parsed.groups, merges).groups
    assert merged.title == "two"
    assert covered([merged]) == frozenset(FAILING)
    assert parse_reduce_response(json.dumps({"merges": []}), map_ids=["m1"]) == ()


def test_identical_membership_resolves_independently_of_response_order():
    one = group("g1", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")], title="alpha")
    two = group("g2", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")], title="beta")
    first = resolve_overlaps(parse([one, two]).groups)
    second = resolve_overlaps(parse([two, one]).groups)
    assert [g.title for g in first] == [g.title for g in second]
    assert len(first) == 1


def test_duplicate_local_ids_drop_every_sharing_group_in_any_order():
    one = group("g", [("a#1", "v")], title="alpha")
    two = group("g", [("a#2", "v")], title="beta")
    keep = group("k", [("b#1", "v")])
    first, second = parse([one, two, keep]), parse([two, keep, one])
    assert [g.local_id for g in first.groups] == ["k"] == [g.local_id for g in second.groups]
    assert "duplicate_id:g" in first.defects
    assert sorted(first.defects) == sorted(second.defects)


def test_uncovered_scope_with_full_coverage_reports_no_fallback():
    parsed = parse([group("g", [("a#1", "v"), ("a#2", "v"), ("b#1", "v")])])
    _, fallback = project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=FEATURES,
        fallback=("uncovered", "invalid_output"),
        forbidden_string_predicates=(),
    )
    assert fallback is None


def test_merge_over_the_variant_cap_is_skipped_and_counted():
    many = [(f"a#{n}", f"v{n}") for n in range(1, 10)]
    more = [(f"b#{n}", f"w{n}") for n in range(1, 10)]
    reg = registry(
        *(rec("a", n, failing=True) for n in range(1, 10)),
        *(rec("b", n, failing=True) for n in range(1, 10)),
    )
    case_of = {r.evidence_id: r.case_id for r in reg.records}
    ids = [r.evidence_id for r in reg.records]
    parsed = parse_map_response(
        json.dumps({"groups": [group("m1", many), group("m2", more)]}),
        chunk_failing=ids,
        case_of=case_of,
        provenance_available=False,
    )
    assert parsed.defects == ()
    merges = parse_reduce_response(
        json.dumps({"merges": [{"groups": ["m1", "m2"]}]}), map_ids=["m1", "m2"]
    )
    applied = apply_merges(parsed.groups, merges)
    assert applied.skipped == 1
    assert [g.local_id for g in applied.groups] == ["m1", "m2"]


# A failing observation whose trace is missing: failing, not available.
UNAVAILABLE_REG = registry(
    rec("a", 1, failing=True, criteria=("x",)),
    rec("a", 2, failing=True, available=False, criteria=("x",)),
    rec("b", 1, failing=True, criteria=("x",)),
)


def test_unavailable_failure_is_not_coverage_eligible():
    case_of = {r.evidence_id: r.case_id for r in UNAVAILABLE_REG.records}
    parsed = parse_map_response(
        json.dumps({"groups": [group("g", [("a#1", "v"), ("b#1", "v")])]}),
        chunk_failing=["a#1", "b#1"],
        case_of=case_of,
        provenance_available=False,
    )
    grouped, fallback = project(
        UNAVAILABLE_REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
    )
    assert fallback is None
    assert grouped.result.assignments[1] is None
    assert {n.member_ids for n in grouped.neighborhoods} == {("a#1", "b#1")}


@pytest.mark.parametrize("features", [None, FEATURES])
@pytest.mark.parametrize("scope", ["all", "uncovered"])
def test_fallbacks_never_group_an_unavailable_failure(features, scope):
    grouped, fallback = project(
        UNAVAILABLE_REG,
        (),
        policy_identity="p",
        fallback_features=features,
        fallback=(scope, "provider_unavailable"),
        forbidden_string_predicates=(),
    )
    assert fallback is not None
    assert grouped.result.assignments[1] is None
    members = {eid for n in grouped.neighborhoods for eid in n.member_ids}
    assert members == {"a#1", "b#1"}


def test_model_group_claiming_an_unavailable_failure_is_refused():
    parsed = parse_map_response(
        json.dumps({"groups": [group("g", [("a#2", "v")])]}),
        chunk_failing=["a#1", "b#1"],
        case_of={r.evidence_id: r.case_id for r in UNAVAILABLE_REG.records},
        provenance_available=False,
    )
    assert parsed.groups == () and "ineligible_member:a#2" in parsed.defects


# --- Final review Important 1: fitting the sparse-map caps -------------------


def test_oversized_text_is_trimmed_from_the_lowest_ranked_group_up():
    from evolution.tree.roles.grouping.model_grouping import ModelGroup, _admission_order

    cyr = "Агент путает правило расчёта и продукт клиента; " * 12
    rows, groups = [], []
    for case in range(160):
        size = 16 - case % 4  # distinct sizes so admission rank is not only a digest
        members = tuple(
            (f"c{case:03d}#{r}", f"{r:02d} вариант {cyr}"[:64]) for r in range(1, size + 1)
        )
        rows += [rec(f"c{case:03d}", r, failing=True) for r in range(1, size + 1)]
        groups.append(
            ModelGroup(
                local_id=f"g{case}",
                title=f"{case} {cyr}"[:128],
                failure_mode=cyr[:512],
                expected_behaviour=cyr[:512],
                question=cyr[:512],
                members=members,
                affected_cases=(f"c{case:03d}",),
                citations=(),
                priority="p",
                metrics=(),
                member_cases=(f"c{case:03d}",) * size,
            )
        )
    reg = registry(*rows)
    trims = []
    grouped, outcome = project(
        reg,
        groups,
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
        on_trim=trims.append,
    )
    assert outcome is None
    assert len(grouped.neighborhoods) == 160
    (counts,) = trims
    assert counts["descriptions_truncated"] == 160
    assert 0 < counts["descriptions_dropped"] < 160
    assert counts["variants_shortened"] == 160
    assert 0 < counts["variants_dropped"] < 160
    by_members = {frozenset(n.member_ids): n for n in grouped.neighborhoods}
    ranked = [by_members[frozenset(e for e, _ in g.members)] for g in _admission_order(groups)]
    described = [n.description is not None for n in ranked]
    labelled = [all(i in grouped.variants for i in n.member_indices) for n in ranked]
    kept = 160 - counts["descriptions_dropped"]
    assert described == [True] * kept + [False] * (160 - kept)
    kept = 160 - counts["variants_dropped"]
    assert labelled == [True] * kept + [False] * (160 - kept)


def test_small_grouping_is_not_trimmed():
    parsed = parse([group("g", [("a#1", "v"), ("a#2", "w"), ("b#1", "v")])])
    trims = []
    project(
        REG,
        parsed.groups,
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
        on_trim=trims.append,
    )
    assert trims == []


def test_signature_helper_shared():
    from evolution.tree.roles.grouping.reflect_grouping import _variant_signature

    row = rec("a", 1, failing=True, criteria=("c1", "c2"))
    observation = row.sources["observation"]
    parts = observation_signature(observation)
    assert model_grouping._observation_row(row).signature == _digest(list(parts))
    assert _variant_signature(row, "v")[:3] == tuple(typed_json_key(v) for v in parts)


def test_default_field_features_include_critical():
    assert tuple(f.path for f in DEFAULT_FIELD_FEATURES) == (
        "/failed_criteria",
        "/terminal",
        "/critical",
    )


def test_declared_fields_fallback_unchanged():
    reg = registry(
        rec("a", 1, failing=True),
        rec("b", 1, failing=True),
        rec("c", 1, failing=True, terminal="timeout"),
    )
    declared = (
        FieldFeature("terminal", "observation", "/terminal", "t"),
        FieldFeature("failed_criteria", "observation", "/failed_criteria", "f"),
    )
    grouped, _ = project(
        reg,
        (),
        policy_identity="p",
        fallback_features=declared,
        fallback=("all", "invalid_output"),
        forbidden_string_predicates=(),
    )
    assert {n.member_ids for n in grouped.neighborhoods} == {("a#1", "b#1"), ("c#1",)}


def test_v15_bundle_signature_bytes_unchanged():
    reg = registry(rec("a", 1, failing=True, criteria=("c1", "c2")))
    # Literal computed with the pre-change inline `_digest([terminal, critical, failed_criteria])`.
    assert (
        model_grouping._observation_row(reg.records[0]).signature
        == "f34c71c6d525424d2a70e0cdc1d98ff41e2a45f675b00dd8071e397c3eb01f50"
    )


def test_describe_off_keeps_result_bytes():
    from evolution.tree.roles.grouping.reflect_grouping import builtin_grouping

    builtin = builtin_grouping(REG, FEATURES, policy_identity="p", describe=False)
    projected, _ = project(
        REG,
        parse([group("g1", [("a#1", "v"), ("b#1", "v")])]).groups,
        policy_identity="p",
        fallback_features=FEATURES,
        fallback=("uncovered", "invalid_output"),
        forbidden_string_predicates=(),
        describe_fields=False,
    )
    for value, expected in [
        (builtin, "fd1a6769c6a07d864aef8fc7ca75d2a4d0f4e9b33f1fe53d14d8a49d594bd18f"),
        (projected, "0455fc5877756f3a792356ae903c8e644faae76c5f864dcf8a48337c0480a503"),
    ]:
        result = value.result
        assert (
            _digest(
                dict(
                    assignments=result.assignments,
                    descriptions=result.descriptions,
                    variants=result.variants,
                    registry_digest=result.registry_digest,
                    version=result.version,
                )
            )
            == expected
        )


def test_variant_short_labels_unchanged():
    # Literal old shortening for a colliding pair; the model permits at most 16 variants.
    assert model_grouping._short_labels(["a" * 64, "a" * 63 + "b"]) == {
        "a" * 64: "a" * 9 + "~1",
        "a" * 63 + "b": "a" * 9 + "~2",
    }


def test_fields_fallback_respects_reserved_model_descriptions():
    from evolution.tree.roles.grouping.reflect_grouping import MAX_SPARSE_MAP_BYTES, _canonical_json

    # The sparse map is almost full of model descriptions before the fallback is added.
    rows = [rec(f"m{i:04}", 1, failing=True) for i in range(100)]
    rows += [rec(f"z{i:04}", 1, failing=True, criteria=(str(i) + "x" * 400,)) for i in range(200)]
    reg = registry(*rows)
    models = [
        dataclasses.replace(
            parse([group("g", [("a#1", "v")])]).groups[0],
            local_id=f"g{i}",
            title="t" * 128,
            failure_mode="f" * 300,
            expected_behaviour="e" * 100,
            question="q?",
            members=((f"m{i:04}#1", "v"),),
            affected_cases=(f"m{i:04}",),
            member_cases=(f"m{i:04}",),
        )
        for i in range(100)
    ]
    common = dict(
        policy_identity="p",
        fallback_features=FEATURES,
        fallback=("uncovered", "invalid_output"),
        forbidden_string_predicates=(),
    )
    before, _ = project(reg, models, **common)
    after, _ = project(reg, models, **common, describe_fields=True)
    for number, text in before.result.descriptions.items():
        assert after.result.descriptions[number] == text
    fields = [entry for entry in after.neighborhoods if entry.member_ids[0].startswith("z")]
    assert any(entry.description is not None for entry in fields)
    assert any(entry.description is None for entry in fields)
    assert len(_canonical_json(after.result.descriptions)) <= MAX_SPARSE_MAP_BYTES


def test_fields_fallback_screen_and_singletons_stay_null():
    reg = registry(rec("a", 1, failing=True, criteria=("needle",)), rec("b", 1, failing=True))
    for features in (FEATURES, None):
        grouped, _ = project(
            reg,
            (),
            policy_identity="p",
            fallback_features=features,
            fallback=("all", "invalid_output"),
            forbidden_string_predicates=(),
            describe_fields=True,
            fields_screens=(lambda text: "needle" in text,),
        )
        assert (
            next(
                entry for entry in grouped.neighborhoods if entry.member_ids == ("a#1",)
            ).description
            is None
        )
        if features is not None:
            assert (
                next(
                    entry for entry in grouped.neighborhoods if entry.member_ids == ("b#1",)
                ).description
                is not None
            )


# -- b17 wave 2 (ADR-0057): family 15 projects the description whole --------


def _near_bound_group():
    """#388 example 1's shape: three fields each near their 512-character
    admitted bounds, so the whole description is well over 512 characters."""

    return parse(
        [
            group(
                "g",
                [("a#1", "v"), ("a#2", "v"), ("b#1", "v")],
                title="T" * 128,
                failure_mode="F" * 512,
                expected_behaviour="E" * 512,
                question="Q" * 512,
            )
        ]
    ).groups


def test_family15_description_projects_every_field_and_title_whole():
    grouped, _ = project(
        REG,
        _near_bound_group(),
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
        family15=True,
    )
    described = grouped.neighborhoods[0].description
    assert described.label == "T" * 128
    assert described.description == (
        f"Observed: {'F' * 512}\nExpected: {'E' * 512}\nQuestion: {'Q' * 512}"
    )


def test_family14_description_of_the_same_group_is_byte_identical_to_today():
    grouped, _ = project(
        REG,
        _near_bound_group(),
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
    )
    described = grouped.neighborhoods[0].description
    # Today's projection: the whole template, silently cut to 512 characters,
    # which drops the question first.
    whole = f"Observed: {'F' * 512}\nExpected: {'E' * 512}\nQuestion: {'Q' * 512}"
    assert described.description == whole[:512]
    assert not described.description.endswith("Question: " + "Q" * 512)
    assert described.label == "T" * 128


def test_trimmed_form_family14_clips_the_question_and_family15_keeps_it_whole():
    from evolution.tree.roles.grouping.model_grouping import _description

    group_ = _near_bound_group()[0]
    family14 = _description(group_, trimmed=True)
    assert family14 == {
        "label": "T" * 63 + "…",
        "description": (f"Observed: {'F' * 95}…\nExpected: {'E' * 95}…\nQuestion: {'Q' * 95}…"),
    }
    family15 = _description(group_, trimmed=True, family15=True)
    assert family15 == {
        "label": "T" * 63 + "…",
        "description": (
            f"Observed: {'F' * 90}…[cut]\nExpected: {'E' * 90}…[cut]\nQuestion: {'Q' * 512}"
        ),
    }
    # The family-15 trimmed form keeps the whole question and so exceeds the
    # 512-character cut it must never be subjected to.
    assert len(family15["description"]) > 512


def _cyrillic_bound_registry(groups: int):
    return registry(*[rec(f"c{i}", 1, failing=True, criteria=("x",)) for i in range(groups)])


def test_family15_map_over_64kib_trims_with_marks_and_keeps_every_question():
    """A descriptions map over its 64 KiB bound, in Cyrillic text: every
    trimmed entry keeps its question whole and marks the shortened fields
    (`…[cut]`); the counts stay in `grouping_trimmed`."""

    groups = 30
    reg = _cyrillic_bound_registry(groups)
    model_groups = tuple(
        parse_map_response(
            json.dumps(
                {
                    "groups": [
                        group(
                            f"g{i}",
                            [(f"c{i}#1", "v")],
                            title="т" * 128,
                            failure_mode="ж" * 512,
                            expected_behaviour="ж" * 512,
                            question="ж" * 512,
                        )
                    ]
                }
            ),
            chunk_failing=[f"c{i}#1"],
            case_of={f"c{i}#1": f"c{i}"},
            provenance_available=False,
        ).groups
        for i in range(groups)
    )
    trims = []
    grouped, _ = project(
        reg,
        [g for parsed in model_groups for g in parsed],
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
        on_trim=trims.append,
        family15=True,
    )
    assert len(trims) == 1
    counts = trims[0]
    assert counts["descriptions_truncated"] > 0 and counts["descriptions_dropped"] == 0
    assert len(grouped.result.descriptions) == groups
    whole = f"Observed: {'ж' * 512}\nExpected: {'ж' * 512}\nQuestion: {'ж' * 512}"
    for entry in grouped.result.descriptions.values():
        description = entry["description"]
        # The question is whole in every entry, trimmed or not.
        assert description.endswith("Question: " + "ж" * 512)
        if description == whole:
            assert "…[cut]" not in description and entry["label"] == "т" * 128
        else:
            assert description.count("…[cut]") == 2
            assert entry["label"] == "т" * 63 + "…"
            assert f"Observed: {'ж' * 90}…[cut]" in description


def test_family14_same_groups_fit_the_map_without_trimming():
    """The family-14 projection of the same groups is today's: every
    description is the 512-character cut, the map fits, nothing is trimmed."""

    groups = 30
    reg = _cyrillic_bound_registry(groups)
    model_groups = [
        parse_map_response(
            json.dumps(
                {
                    "groups": [
                        group(
                            f"g{i}",
                            [(f"c{i}#1", "v")],
                            title="т" * 128,
                            failure_mode="ж" * 512,
                            expected_behaviour="ж" * 512,
                            question="ж" * 512,
                        )
                    ]
                }
            ),
            chunk_failing=[f"c{i}#1"],
            case_of={f"c{i}#1": f"c{i}"},
            provenance_available=False,
        ).groups[0]
        for i in range(groups)
    ]
    trims = []
    grouped, _ = project(
        reg,
        model_groups,
        policy_identity="p",
        fallback_features=None,
        fallback=None,
        forbidden_string_predicates=(),
        on_trim=trims.append,
    )
    assert trims == []
    assert len(grouped.result.descriptions) == groups
    whole = f"Observed: {'ж' * 512}\nExpected: {'ж' * 512}\nQuestion: {'ж' * 512}"
    for entry in grouped.result.descriptions.values():
        assert entry["description"] == whole[:512]
        assert entry["label"] == "т" * 128
