"""Focused contract tests for the self-contained ``reflect_grouping`` core."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping

import pytest

from evolution.tree.execution.json_pointer import MISSING, pointer_value
from evolution.tree.roles.grouping.reflect_grouping import (
    MAX_REGISTRY_RECORDS,
    FieldFeature,
    GroupingAdmissionError,
    GroupingRecord,
    GroupingRegistry,
    GroupingResult,
    admit_grouping_result,
    build_registry,
    builtin_grouping,
    cohorts_from_grouping,
    select_portion,
    typed_json_key,
)

_NO_DIAGNOSTICS = object()


def record(
    evidence_id: str,
    case_id: str,
    repeat: int,
    *,
    failing: bool = False,
    available: bool = True,
    case: object = None,
    public: object = None,
    observation: object = None,
    feedback: object = None,
    diagnostics: object = _NO_DIAGNOSTICS,
) -> GroupingRecord:
    sources: dict[str, object] = {
        "case": {} if case is None else case,
        "public": {} if public is None else public,
        "observation": {} if observation is None else observation,
        "feedback": {} if feedback is None else feedback,
    }
    if diagnostics is not _NO_DIAGNOSTICS:
        # Family 8 only (ADR-0021): a fifth source, admitted only when a
        # caller explicitly asks for it, so every other fixture in this
        # module keeps the original four-key `sources` mapping.
        sources["diagnostics"] = diagnostics
    return GroupingRecord(
        evidence_id=evidence_id,
        case_id=case_id,
        repeat=repeat,
        failing=failing,
        available=available,
        sources=sources,
    )


def registry_with_sources(diagnostics_values: list[object]) -> GroupingRegistry:
    """A registry of failing records, each carrying one `diagnostics` value.

    Mirrors the plan's `registry_with_sources` fixture, adapted to this
    module's own `record`/`build_registry` helpers.
    """

    return build_registry(
        "baseline-1",
        [
            record(f"e{index}", f"c{index}", 1, failing=True, diagnostics=diagnostics)
            for index, diagnostics in enumerate(diagnostics_values, start=1)
        ],
    )


def test_registry_is_canonical_binds_baseline_and_rejects_duplicate_ids() -> None:
    registry = build_registry(
        "baseline-1",
        [
            record("b-2", "b", 2),
            record("a-2", "a", 2),
            record("a-1", "a", 1),
        ],
    )
    assert [(row.case_id, row.repeat) for row in registry.records] == [
        ("a", 1),
        ("a", 2),
        ("b", 2),
    ]
    assert registry.baseline_binding == "baseline-1"
    assert registry.digest == build_registry("baseline-1", list(reversed(registry.records))).digest
    assert registry.digest != build_registry("baseline-2", registry.records).digest
    assert (
        registry.digest
        != build_registry(
            "baseline-1",
            [
                record("a-1", "a", 1, failing=True),
                record("a-2", "a", 2),
                record("b-2", "b", 2),
            ],
        ).digest
    )
    with pytest.raises(ValueError, match="duplicate evidence_id"):
        build_registry("baseline-1", [record("same", "a", 1), record("same", "b", 1)])


def test_registry_has_protocol_capacity_limit() -> None:
    rows = [record(f"e{i}", f"c{i}", 1) for i in range(MAX_REGISTRY_RECORDS + 1)]
    with pytest.raises(ValueError, match="reflection_capacity"):
        build_registry("baseline", rows)


def test_registry_does_not_materialize_lazy_sources_before_feature_selection() -> None:
    class LazySources(Mapping[str, object]):
        def __init__(self) -> None:
            self.reads: list[str] = []
            self.values = {
                "case": {"large": ["not read"]},
                "public": {"large": ["not read"]},
                "observation": {"terminal": "failed"},
                "feedback": {"large": ["not read"]},
            }

        def __getitem__(self, key: str) -> object:
            self.reads.append(key)
            return self.values[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self.values)

        def __len__(self) -> int:
            return len(self.values)

    sources = LazySources()
    registry = build_registry(
        "base", [GroupingRecord("f", "case", 1, failing=True, available=True, sources=sources)]
    )
    assert sources.reads == []
    builtin_grouping(
        registry,
        [FieldFeature("terminal", "observation", "/terminal", "only this projection")],
        policy_identity="fields-v1",
    )
    assert sources.reads == ["observation"]


def test_builtin_features_preserve_json_types_and_missing_vs_null() -> None:
    registry = build_registry(
        "base",
        [
            record("missing", "a", 1, failing=True, observation={}),
            record("null", "b", 1, failing=True, observation={"value": None}),
            record("bool", "c", 1, failing=True, observation={"value": True}),
            record("number", "d", 1, failing=True, observation={"value": 1}),
            record(
                "nested-one", "e", 1, failing=True, observation={"value": {"b": [1, 2], "a": 3}}
            ),
            record(
                "nested-two", "f", 1, failing=True, observation={"value": {"a": 3, "b": [1, 2]}}
            ),
            record("passing", "g", 1, observation={"value": 1}),
        ],
    )
    admitted = builtin_grouping(
        registry,
        [FieldFeature("value", "observation", "/value", "typed grouping feature")],
        policy_identity="fields-v1",
    )
    members = {frozenset(group.member_ids) for group in admitted.neighborhoods}
    assert frozenset({"nested-one", "nested-two"}) in members
    assert all(group.member_ids != ("passing",) for group in admitted.neighborhoods)
    assert len(admitted.neighborhoods) == 5


def test_builtin_grouping_reads_the_diagnostics_source() -> None:
    """Family 8 only (ADR-0021): `FieldFeature.source == "diagnostics"` reads
    `row.sources["diagnostics"]`, and a `None` value (the scorer published
    none) yields the same missing marker as any other absent path, not an
    error."""

    feature = FieldFeature("route", "diagnostics", "/route", "Expected route.")
    registry = registry_with_sources(
        [{"route": "handoff"}, {"route": "handoff"}, {"route": "answer"}, None]
    )
    grouping = builtin_grouping(registry, (feature,), policy_identity="policy")
    labels = sorted(len(n.member_indices) for n in grouping.neighborhoods)
    assert labels == [1, 1, 2]  # handoff x2, answer, missing


def test_typed_keys_refuse_arbitrarily_recursive_projection_values() -> None:
    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(ValueError, match="invalid_json_value"):
        typed_json_key(recursive)


def test_default_builtin_features_are_lazy_and_a_maximum_registry_result_fits() -> None:
    registry = build_registry(
        "base",
        [
            record(
                f"e{index}",
                f"c{index:05d}",
                1,
                failing=True,
                observation={"terminal": "failed", "failed_criteria": ["x"]},
            )
            for index in range(MAX_REGISTRY_RECORDS)
        ],
    )
    grouped = builtin_grouping(registry, policy_identity="default-fields")
    assert len(grouped.result.assignments) == MAX_REGISTRY_RECORDS
    assert len(grouped.neighborhoods) == 1


def test_admission_rechecks_description_size_after_canonical_group_renumbering() -> None:
    registry = build_registry(
        "base",
        [
            record(f"e{index}", f"c{index:05d}", 1, failing=True)
            for index in range(MAX_REGISTRY_RECORDS)
        ],
    )
    descriptions = {index: {"label": "", "description": "x" * 20} for index in range(1169)}
    assert (
        len(json.dumps(descriptions, sort_keys=True, separators=(",", ":")).encode()) <= 64 * 1024
    )
    with pytest.raises(GroupingAdmissionError, match="description_size"):
        admit_grouping_result(
            registry,
            GroupingResult(registry.digest, tuple(range(MAX_REGISTRY_RECORDS)), descriptions),
            policy_identity="policy",
        )


@pytest.mark.parametrize("pointer", ["/١", "/²", "/" + "9" * 5000])
def test_pointer_array_indices_are_ascii_and_oversized_indices_are_missing(pointer: str) -> None:
    assert pointer_value(["zero", "one"], pointer) is MISSING


def test_group_and_cohort_ids_sort_member_evidence_ids_not_registry_order() -> None:
    registry = build_registry(
        "base",
        [record("z", "case", 1, failing=True), record("a", "case", 2, failing=True)],
    )
    grouped = admit_grouping_result(
        registry, GroupingResult(registry.digest, (4, 4)), policy_identity="policy"
    )
    (neighborhood,) = grouped.neighborhoods
    expected_group_id = hashlib.sha256(
        json.dumps(
            {"policy_identity": "policy", "baseline_binding": "base", "member_ids": ["a", "z"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert neighborhood.member_ids == ("z", "a")
    assert neighborhood.group_id == expected_group_id

    (cohort,) = cohorts_from_grouping(grouped)
    expected_cohort_id = hashlib.sha256(
        json.dumps(
            {"group_id": expected_group_id, "case_id": "case", "member_ids": ["a", "z"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert cohort.member_ids == ("z", "a")
    assert cohort.cohort_id == expected_cohort_id


def test_result_admission_compacts_labels_and_derives_canonical_ids() -> None:
    registry = build_registry(
        "base",
        [
            record("pass", "a", 1),
            record("f1", "a", 2, failing=True),
            record("f2", "b", 1, failing=True, available=False),
            record("f3", "c", 1, failing=True),
        ],
    )
    raw = GroupingResult(
        registry_digest=registry.digest,
        assignments=(None, 19, 19, None),
        descriptions={19: {"label": "display only", "description": "a description"}},
        variants={1: "one", 2: "two"},
    )
    admitted = admit_grouping_result(registry, raw, policy_identity="policy")
    assert len(admitted.neighborhoods) == 2
    joined = next(
        group for group in admitted.neighborhoods if set(group.member_ids) == {"f1", "f2"}
    )
    singleton = next(group for group in admitted.neighborhoods if group.member_ids == ("f3",))
    assert joined.group_number != 19
    assert singleton.group_number != 19
    assert joined.description.label == "display only"
    assert (
        joined.group_id
        != admit_grouping_result(
            registry,
            GroupingResult(
                registry.digest, (None, 1, 1, None), {1: {"label": "renamed", "description": "x"}}
            ),
            policy_identity="other-policy",
        )
        .neighborhoods[0]
        .group_id
    )


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (lambda d: GroupingResult(d, (None,), {}), "assignment_count"),
        (lambda d: GroupingResult("wrong", (None, None, None, None), {}), "registry_digest"),
        # F2: a non-text digest is a shape fault, refused before the evidence check.
        (lambda d: GroupingResult(1, (None, None, None, None), {}), "registry_digest_type"),
        (lambda d: GroupingResult(d, (0, None, None, None), {}), "nonfailing_membership"),
        (
            lambda d: GroupingResult(
                d, (None, 1, None, None), {2: {"label": "x", "description": "y"}}
            ),
            "unused_description",
        ),
        (lambda d: GroupingResult(d, (None, 1, None, None), {}, {0: "x"}), "variant_index"),
    ],
)
def test_result_admission_refuses_malformed_or_nonfailing_membership(result, code) -> None:
    registry = build_registry(
        "base",
        [
            record("p", "a", 1),
            record("f", "a", 2, failing=True),
            record("q", "b", 1),
            record("r", "c", 1),
        ],
    )
    with pytest.raises(GroupingAdmissionError, match=rf"^{code}$"):
        admit_grouping_result(registry, result(registry.digest), policy_identity="policy")


def test_admission_applies_injected_leak_predicates_to_all_output_strings() -> None:
    registry = build_registry("base", [record("f", "a", 1, failing=True)])
    raw = GroupingResult(
        registry.digest,
        (2,),
        {2: {"label": "safe", "description": "contains SECRET but must not echo it"}},
        {0: "safe"},
    )
    with pytest.raises(GroupingAdmissionError, match="grouping_secret_leak"):
        admit_grouping_result(
            registry,
            raw,
            policy_identity="policy",
            forbidden_string_predicates=(
                ("grouping_secret_leak", lambda value: "SECRET" in value),
            ),
        )


def test_admission_bounds_distinct_variant_labels_per_neighborhood() -> None:
    registry = build_registry(
        "base", [record(f"f{index}", "a", index + 1, failing=True) for index in range(17)]
    )
    with pytest.raises(GroupingAdmissionError, match="variant_count"):
        admit_grouping_result(
            registry,
            GroupingResult(
                registry.digest, (1,) * 17, {}, {index: f"v{index}" for index in range(17)}
            ),
            policy_identity="policy",
        )


def test_cohorts_follow_grouping_and_portion_is_size_first_with_cycle_reset() -> None:
    registry = build_registry(
        "base",
        [
            record("a-pass", "a", 1),
            record("a-f1", "a", 2, failing=True, observation={"terminal": "failed"}),
            record("a-f2", "a", 3, failing=True, observation={"terminal": "failed"}),
            record("b-f", "b", 1, failing=True, observation={"terminal": "failed"}),
            record("c-f", "c", 1, failing=True, observation={"terminal": "failed"}),
        ],
    )
    grouped = admit_grouping_result(
        registry,
        GroupingResult(registry.digest, (None, 7, 7, 7, 9), {}, {1: "x", 2: "y", 3: "x", 4: "z"}),
        policy_identity="policy",
    )
    cohorts = cohorts_from_grouping(grouped)
    selection = select_portion(
        cohorts, registry=registry, visited_in_cycle=frozenset(), focus_cases=1
    )
    assert selection.cycle_reset is False
    assert [cohort.case_id for cohort in selection.cohorts] == ["a"]
    portion = selection.portions[0]
    assert [row.evidence_id for row in portion.exemplars] == ["a-f1", "a-f2"]
    assert portion.passing_contrast is not None and portion.passing_contrast.evidence_id == "a-pass"

    visited = frozenset(cohort.cohort_id for cohort in cohorts)
    reset = select_portion(cohorts, registry=registry, visited_in_cycle=visited, focus_cases=4)
    assert reset.cycle_reset is True
    assert {cohort.case_id for cohort in reset.cohorts} == {"a", "b"}


def _equal_size_cohorts(binding: str):
    """Two one-case neighborhoods of the same size, over one baseline binding."""

    registry = build_registry(
        binding,
        [
            record("a-f", "a", 1, failing=True, observation={"terminal": "failed"}),
            record("b-f", "b", 1, failing=True, observation={"terminal": "failed"}),
        ],
    )
    grouped = admit_grouping_result(
        registry, GroupingResult(registry.digest, (1, 2)), policy_identity="policy"
    )
    return registry, cohorts_from_grouping(grouped)


def test_equal_size_cohorts_break_ties_by_content_not_by_the_run_binding() -> None:
    """PDR-0009: two otherwise identical campaigns must focus the same case."""

    focused = set()
    keys = set()
    for binding in ("base-a", "base-b", "base-c", "base-d"):
        registry, cohorts = _equal_size_cohorts(binding)
        assert len({cohort.group_id for cohort in cohorts}) == 2
        # The run-specific identities do move between the two campaigns...
        keys.add(tuple(sorted(cohort.cohort_id for cohort in cohorts)))
        selection = select_portion(
            cohorts, registry=registry, visited_in_cycle=frozenset(), focus_cases=1
        )
        focused.add(tuple(cohort.case_id for cohort in selection.cohorts))
    assert len(keys) == 4
    # ...while the focus does not.
    assert len(focused) == 1


def test_variant_exemplars_cap_and_record_unrepresented_variants_without_terciles() -> None:
    registry = build_registry(
        "base",
        [
            record(
                "f3",
                "a",
                3,
                failing=True,
                observation={"terminal": "bad", "critical": False, "failed_criteria": ["x"]},
            ),
            record(
                "f1",
                "a",
                1,
                failing=True,
                observation={"terminal": "bad", "critical": False, "failed_criteria": ["x"]},
            ),
            record(
                "f2",
                "a",
                2,
                failing=True,
                observation={"terminal": "bad", "critical": True, "failed_criteria": ["x"]},
            ),
        ],
    )
    grouped = admit_grouping_result(
        registry,
        GroupingResult(registry.digest, (4, 4, 4), {}, {0: "same", 1: "other", 2: "third"}),
        policy_identity="policy",
    )
    (cohort,) = cohorts_from_grouping(grouped)
    (portion,) = select_portion(
        (cohort,), registry=registry, focus_cases=1, representatives_per_case=2
    ).portions
    assert len(portion.exemplars) == 2
    assert {row.evidence_id for row in portion.exemplars} == {"f1", "f3"}
    assert portion.omitted_variant_count == 1
    assert portion.unrepresented_variant_ids == ("f2",)


def _unavailable_failure_registry():
    def row(repeat, available):
        return GroupingRecord(
            f"a#{repeat}",
            "a",
            repeat,
            True,
            available,
            {"case": {}, "public": {}, "observation": {}, "feedback": {}},
        )

    return build_registry("b" * 64, [row(1, True), row(2, False)])


def test_unavailable_failure_is_a_singleton_by_default():
    reg = _unavailable_failure_registry()
    admitted = admit_grouping_result(
        reg, GroupingResult(reg.digest, (0, None)), policy_identity="p"
    )
    assert sorted(n.member_ids for n in admitted.neighborhoods) == [("a#1",), ("a#2",)]
    admitted = admit_grouping_result(reg, GroupingResult(reg.digest, (0, 0)), policy_identity="p")
    assert [n.member_ids for n in admitted.neighborhoods] == [("a#1", "a#2")]


def test_unavailable_unassigned_keeps_an_unavailable_failure_out_of_every_group():
    reg = _unavailable_failure_registry()
    admitted = admit_grouping_result(
        reg,
        GroupingResult(reg.digest, (0, None)),
        policy_identity="p",
        unavailable_unassigned=True,
    )
    assert [n.member_ids for n in admitted.neighborhoods] == [("a#1",)]
    assert admitted.result.assignments == (0, None)
    for result, code in (
        (GroupingResult(reg.digest, (0, 0)), "unavailable_membership"),
        (GroupingResult(reg.digest, (0, None), variants={1: "v"}), "variant_index"),
    ):
        with pytest.raises(GroupingAdmissionError, match=code):
            admit_grouping_result(reg, result, policy_identity="p", unavailable_unassigned=True)


# b17: descriptions are optional bounded summaries, never grouping failures.
def b17_fields_registry(values):
    return build_registry(
        "base",
        [
            record(f"e{i}", f"c{i:04}", 1, failing=True, observation=value)
            for i, value in enumerate(values)
        ],
    )


def test_fields_group_description_template():
    reg = build_registry(
        "base",
        [
            record("a1", "a", 1, failing=True, observation={"error": "timeout", "tool": "search"}),
            record("a2", "a", 2, failing=True, observation={"error": "timeout", "tool": "search"}),
            record("b1", "b", 1, failing=True, observation={"error": "timeout", "tool": "search"}),
        ],
    )
    features = [
        FieldFeature("error_code", "observation", "/error", ""),
        FieldFeature("tool", "observation", "/tool", ""),
    ]
    result = builtin_grouping(reg, features, policy_identity="p", describe=True)
    (entry,) = result.neighborhoods
    assert entry.description.label == 'error_code="timeout", tool="search"'
    assert entry.description.description == (
        '3 failing observations in 2 cases share error_code="timeout", tool="search". '
        "What do they have in common, and why do they fail?"
    )


def test_missing_feature_value_and_one_member_group_have_description():
    result = builtin_grouping(
        b17_fields_registry([{}, {"x": None}]),
        [FieldFeature("x", "observation", "/x", "")],
        policy_identity="p",
        describe=True,
    )
    entries = {
        entry.description.label: entry.description.description for entry in result.neighborhoods
    }
    assert set(entries) == {"x=<absent>", "x=null"}
    assert entries["x=<absent>"].startswith("1 failing observation in 1 case share x=<absent>.")


def test_fields_label_shortened_distinct_and_description_cut_keeps_counts_question():
    feature = FieldFeature("n" * 128, "observation", "/x", "")
    result = builtin_grouping(
        b17_fields_registry([{"x": "a" * 2000}, {"x": "b" * 2000}]),
        [feature],
        policy_identity="p",
        describe=True,
    )
    labels = [entry.description.label for entry in result.neighborhoods]
    assert len(set(labels)) == 2 and all(len(label) <= 128 for label in labels)
    for entry in result.neighborhoods:
        text = entry.description.description
        assert len(text) <= 512 and "…[cut]" in text
        assert text.startswith("1 failing observation in 1 case share ")
        assert text.endswith("What do they have in common, and why do they fail?")


def test_many_fields_groups_fill_map_then_null():
    from evolution.tree.roles.grouping.reflect_grouping import MAX_SPARSE_MAP_BYTES, _canonical_json

    reg = b17_fields_registry([{"x": str(i) + "ж" * 400} for i in range(3000)])
    result = builtin_grouping(
        reg, [FieldFeature("x", "observation", "/x", "")], policy_identity="p", describe=True
    )
    numbers = sorted(result.result.descriptions)
    assert 0 < len(numbers) < 3000
    # Admission renumbers by group id; formation order is registry order.
    described_indices = sorted(
        entry.member_indices[0] for entry in result.neighborhoods if entry.description is not None
    )
    assert described_indices == list(range(len(numbers)))
    assert len(_canonical_json(result.result.descriptions)) <= MAX_SPARSE_MAP_BYTES
    assert len(result.neighborhoods) == 3000
    assert sum(entry.description is not None for entry in result.neighborhoods) == len(numbers)


@pytest.mark.parametrize(
    "code,needle", [("secret_detected", "secret-value"), ("oracle_leak", "oracle-value")]
)
def test_forbidden_fields_value_drops_that_group_only(code, needle):
    result = builtin_grouping(
        b17_fields_registry([{"x": "safe1"}, {"x": needle}, {"x": "safe2"}]),
        [FieldFeature("x", "observation", "/x", "")],
        policy_identity="p",
        describe=True,
        forbidden_string_predicates=((code, lambda text: needle in text),),
    )
    by_member = {entry.member_ids: entry.description for entry in result.neighborhoods}
    assert by_member[("e1",)] is None
    assert by_member[("e0",)] is not None and by_member[("e2",)] is not None


def test_shadow_mode_keeps_fields_text_and_counts_each_field_once():
    from evolution.tree.execution.check_modes import CheckModes, HitSink, check_predicate

    hits = HitSink()
    screen = check_predicate(
        "grouping_run_secret",
        "secret_detected",
        lambda text: "needle" in text,
        modes=CheckModes({"grouping_run_secret": "shadow"}),
        hits=hits,
    )
    result = builtin_grouping(
        b17_fields_registry([{"x": "needle"}]),
        [FieldFeature("x", "observation", "/x", "")],
        policy_identity="p",
        describe=True,
        forbidden_string_predicates=(("secret_detected", screen),),
    )
    assert result.neighborhoods[0].description is not None
    # Admission scans the label and description, not a third pre-screen hit.
    assert sum(entry["count"] for entry in hits.entries()) == 2


@pytest.mark.parametrize("nested", [False, True])
def test_fields_descriptions_serialize_lazy_and_nested_json_mappings(nested):
    from types import MappingProxyType

    value = MappingProxyType({"a": 1, "z": "ж"})
    observation = {"items": [value]} if nested else value
    reg = b17_fields_registry([observation])
    feature = FieldFeature("root", "observation", "", "")
    described = builtin_grouping(reg, [feature], policy_identity="p", describe=True)
    plain = builtin_grouping(reg, [feature], policy_identity="p")
    assert described.neighborhoods[0].group_id == plain.neighborhoods[0].group_id
    assert described.neighborhoods[0].description.label == (
        'root={"items":[{"a":1,"z":"ж"}]}' if nested else 'root={"a":1,"z":"ж"}'
    )


def test_fields_texts_check_every_member_even_when_the_first_is_visible():
    from dataclasses import replace

    rows = [
        replace(
            record("e0", "a", 1, failing=True, observation={"reads": ["visible", "hidden"]}),
            role_observation_value=lambda path: ["visible", "hidden"],
        ),
        replace(
            record("e1", "b", 1, failing=True, observation={"reads": ["visible", "hidden"]}),
            role_observation_value=lambda path: ["visible"],
        ),
    ]
    reg = build_registry("base", rows)
    result = builtin_grouping(
        reg,
        (FieldFeature("reads", "observation", "/reads", ""),),
        policy_identity="p",
        describe=True,
    )
    (group,) = result.neighborhoods
    assert group.member_ids == ("e0", "e1") and group.description is None
