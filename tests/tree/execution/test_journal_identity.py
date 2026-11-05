from copy import deepcopy
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from evolution import __version__
from evolution.tree.execution.interaction import EvalCase, ScorerIdentity
from evolution.tree.execution.journal_identity import (
    IdentityError,
    RunIdentityV1,
    canonical_json,
    case_payload,
    checked_case_subset,
    corpus_digest,
    resolve_package_version,
    scorer_binding,
    scoring_input_digest,
)


def cases():
    return [EvalCase("c", "g", "s", ("é",), {"x": 1}, {"secret": "α"}, {"critical": False})]


def payload():
    return dict(
        profile="rollout_measurement",
        candidate_digest="candidate",
        target=dict(
            contract_digest="contract",
            dependency_digest="dep",
            settings_hash="settings",
            seed_mode="exact",
            protocol_version="1",
            runtime_ref_digest="runtime",
        ),
        environment=dict(kind="env", digest="envdigest"),
        scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
        evaluation_set_id="label",
        corpus_digest=corpus_digest(cases()),
        metric_plan_digest="a" * 64,
        schedule=[dict(case_id="c", sample_id="s1", repeat=1, requested_seed=42)],
        budget=dict(max_turns=2, timeout_seconds=1.5),
        max_concurrency=1,
        max_scorer_concurrency=1,
        materialization_strategy="filesystem_overlay",
    )


def test_canonical_private_corpus_vectors():
    assert canonical_json({"é": [1, None]}) == '{"é":[1,null]}'.encode()
    assert case_payload(cases()[0])["private"] == {"secret": "α"}
    import hashlib

    assert (
        corpus_digest(cases())
        == hashlib.sha256(canonical_json([case_payload(cases()[0])])).hexdigest()
    )
    with pytest.raises(IdentityError):
        corpus_digest(cases() * 2)
    for invalid in ({1: "x"}, {"x": float("nan")}, {"x": object()}):
        with pytest.raises(IdentityError):
            canonical_json(invalid)


def test_immutable_roundtrip_and_comparison_identity():
    source = payload()
    identity = RunIdentityV1.from_payload(source)
    source["target"]["contract_digest"] = "changed"
    assert identity.to_payload() == payload()
    copy = identity.to_payload()
    copy["schedule"][0]["repeat"] = 99
    assert identity.to_payload() == payload()
    changed = payload()
    changed.update(evaluation_set_id="other", max_concurrency=3, candidate_digest="other")
    other = RunIdentityV1.from_payload(changed)
    assert other.digest != identity.digest
    assert other.condition_key() == identity.condition_key()
    assert other.measurement_key() != identity.measurement_key()
    changed["scorer"]["recovery"] = "deterministic"
    assert RunIdentityV1.from_payload(changed).condition_key() != identity.condition_key()


def test_unicode_case_id_roundtrips_in_measurement_identity():
    corpus = [EvalCase("пример-1", "g", "s", (), {}, {}, {})]
    data = payload()
    data["corpus_digest"] = corpus_digest(corpus)
    data["schedule"][0]["case_id"] = "пример-1"
    identity = RunIdentityV1.from_payload(data)
    assert identity.validate_corpus(corpus)["пример-1"] == corpus[0]


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.update(extra=1),
        lambda p: p.update(max_concurrency=True),
        lambda p: p["budget"].update(timeout_seconds=float("inf")),
        lambda p: p["schedule"][0].update(repeat=0),
        lambda p: p["schedule"][0].update(repeat=True),
        lambda p: p["schedule"].append(deepcopy(p["schedule"][0])),
        lambda p: p["schedule"][0].update(requested_seed=None),
        lambda p: p["scorer"].update(recovery="maybe"),
        lambda p: p.update(corpus_digest="notsha"),
    ],
)
def test_strict_identity_rejects_invalid(edit):
    data = payload()
    edit(data)
    with pytest.raises(IdentityError):
        RunIdentityV1.from_payload(data)


def test_uniform_schedule_and_corpus_binding():
    data = payload()
    data["schedule"].append(dict(case_id="other", sample_id="x", repeat=1, requested_seed=43))
    with pytest.raises(IdentityError):
        RunIdentityV1.from_payload(data)
    data["schedule"][-1]["requested_seed"] = 42
    identity = RunIdentityV1.from_payload(data)
    with pytest.raises(IdentityError):
        identity.validate_corpus(cases())
    data = payload()
    data["schedule"][0]["repeat"] = 2
    with pytest.raises(IdentityError):
        RunIdentityV1.from_payload(data)


def test_resume_checks_all_fields_and_package():
    original = RunIdentityV1.from_payload(payload())
    kwargs = dict(
        stored_digest=original.digest,
        stored_run_id="r",
        requested_run_id="r",
        producing_version=__version__,
        cases=cases(),
        version_resolver=lambda _: __version__,
    )
    original.validate_resume(original, **kwargs)
    for field, value in [
        ("max_concurrency", 2),
        ("evaluation_set_id", "new"),
        ("materialization_strategy", "artifact_root_injection"),
    ]:
        changed = payload()
        changed[field] = value
        with pytest.raises(IdentityError):
            original.validate_resume(RunIdentityV1.from_payload(changed), **kwargs)
    for key, value in [
        ("stored_digest", "0" * 64),
        ("requested_run_id", "different"),
        ("producing_version", "wrong"),
    ]:
        with pytest.raises(IdentityError):
            original.validate_resume(original, **(kwargs | {key: value}))

    def missing(_):
        raise PackageNotFoundError("evo-skills")

    with pytest.raises(IdentityError):
        resolve_package_version(missing)
    with pytest.raises(IdentityError):
        resolve_package_version(lambda _: "wrong")


def test_scorer_capability_binding():
    component = SimpleNamespace(identity=ScorerIdentity("judge", "d"))
    assert scorer_binding(component)["recovery"] == "non_replayable"
    component.recovery_mode = "deterministic"
    assert scorer_binding(component)["recovery"] == "deterministic"
    component.recovery_mode = "invalid"
    with pytest.raises(IdentityError):
        scorer_binding(component)


def test_scoring_input_digest_paths_and_private_changes():
    candidate = SimpleNamespace(digest="candidate", artifact_paths=("b", "a"), verify=lambda: None)
    view = SimpleNamespace(to_payload=lambda: {"history": {"turns": []}})
    scorer = payload()["scorer"]
    first = scoring_input_digest(cases()[0], view, candidate, scorer)
    candidate.artifact_paths = ("a", "b")
    assert scoring_input_digest(cases()[0], view, candidate, scorer) == first
    for path in ("/absolute", "a//b", "a/../b", "./a", "a\\b"):
        candidate.artifact_paths = (path,)
        with pytest.raises(IdentityError):
            scoring_input_digest(cases()[0], view, candidate, scorer)


identity_payload = payload


def test_condition_key_matches_existing_helper():
    from evolution.tree.execution.interaction import (
        EnvironmentIdentity,
        InstanceProvenance,
        SemanticIdentity,
        TargetIdentity,
        condition_key,
    )
    from evolution.tree.execution.journal_identity import effective_scorer_identity

    data = payload()
    target = TargetIdentity(
        SemanticIdentity(**data["target"]), InstanceProvenance("boot", "candidate"), frozenset()
    )
    expected = condition_key(
        target,
        EnvironmentIdentity(**data["environment"]),
        effective_scorer_identity(data["scorer"]),
        data["corpus_digest"],
        1,
        (42,),
        data["metric_plan_digest"],
    )
    assert RunIdentityV1(data).condition_key(target) == expected


def test_live_inputs_match_frozen_case_sample_and_capability():
    from evolution.tree.execution.interaction import (
        EnvironmentIdentity,
        RolloutBudget,
        RolloutSample,
        SemanticIdentity,
    )

    identity = RunIdentityV1(payload())
    component = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"))
    kwargs = dict(
        case=cases()[0],
        sample=RolloutSample("s1", "candidate", 42),
        target=SemanticIdentity(**payload()["target"]),
        environment=EnvironmentIdentity(**payload()["environment"]),
        scorer=component,
        budget=RolloutBudget(2, 1.5),
        corpus=cases(),
    )
    identity.validate_call(**kwargs)
    component.recovery_mode = "deterministic"
    with pytest.raises(IdentityError):
        identity.validate_call(**kwargs)
    component.recovery_mode = "non_replayable"
    kwargs["case"] = EvalCase(
        "c", "g", "s", ("é",), {"x": 1}, {"secret": "changed"}, {"critical": False}
    )
    with pytest.raises(IdentityError):
        identity.validate_call(**kwargs)


@pytest.mark.parametrize(
    "field",
    [
        "profile",
        "candidate_digest",
        "target",
        "environment",
        "scorer",
        "evaluation_set_id",
        "corpus_digest",
        "metric_plan_digest",
        "schedule",
        "budget",
        "max_concurrency",
        "max_scorer_concurrency",
        "materialization_strategy",
    ],
)
@pytest.mark.parametrize("hostile", [[], {}, True, None])
def test_hostile_field_types_fail_with_safe_identity_error(field, hostile):
    data = payload()
    data[field] = hostile
    with pytest.raises(IdentityError):
        RunIdentityV1(data)


@pytest.mark.parametrize("field", ["kind", "digest", "recovery"])
@pytest.mark.parametrize("hostile", [[], {}, True, None])
def test_hostile_scorer_types(field, hostile):
    data = payload()
    data["scorer"][field] = hostile
    with pytest.raises(IdentityError):
        RunIdentityV1(data)


@pytest.mark.parametrize(
    "path,value",
    [
        (("candidate_digest",), "other"),
        *[
            (("target", field), "other")
            for field in (
                "contract_digest",
                "dependency_digest",
                "settings_hash",
                "protocol_version",
                "runtime_ref_digest",
            )
        ],
        (("environment", "kind"), "other"),
        (("environment", "digest"), "other"),
        (("scorer", "kind"), "other"),
        (("scorer", "digest"), "other"),
        (("scorer", "recovery"), "deterministic"),
        (("evaluation_set_id",), "other"),
        (("metric_plan_digest",), "b" * 64),
        (("max_concurrency",), 2),
        (("max_scorer_concurrency",), 2),
        (("materialization_strategy",), "artifact_root_injection"),
        (("budget", "max_turns"), 3),
        (("budget", "timeout_seconds"), 2.5),
        (("schedule", 0, "sample_id"), "other"),
        (("schedule", 0, "requested_seed"), 43),
    ],
)
def test_resume_rejects_each_changed_identity_owner(path, value):
    original = RunIdentityV1(payload())
    changed = payload()
    node = changed
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(IdentityError, match="run_identity_changed"):
        original.validate_resume(
            RunIdentityV1(changed),
            stored_digest=original.digest,
            stored_run_id="r",
            requested_run_id="r",
            producing_version=__version__,
            cases=cases(),
            version_resolver=lambda _: __version__,
        )


@pytest.mark.parametrize("change", ["corpus", "repeats", "seed_mode"])
def test_resume_rejects_consistent_compound_identity_change(change):
    original = RunIdentityV1(payload())
    changed = payload()
    corpus = cases()
    if change == "corpus":
        corpus = [EvalCase("c", "g", "s", ("é",), {"x": 2}, {"secret": "α"}, {"critical": False})]
        changed["corpus_digest"] = corpus_digest(corpus)
    elif change == "repeats":
        changed["schedule"].append(dict(case_id="c", sample_id="s2", repeat=2, requested_seed=43))
    else:
        changed["target"]["seed_mode"] = "none"
        changed["schedule"][0]["requested_seed"] = None
    with pytest.raises(IdentityError, match="run_identity_changed"):
        original.validate_resume(
            RunIdentityV1(changed),
            stored_digest=original.digest,
            stored_run_id="r",
            requested_run_id="r",
            producing_version=__version__,
            cases=corpus,
            version_resolver=lambda _: __version__,
        )


@pytest.mark.parametrize(
    "section,field",
    [
        *[
            ("target", field)
            for field in (
                "contract_digest",
                "dependency_digest",
                "settings_hash",
                "protocol_version",
                "runtime_ref_digest",
            )
        ],
        ("environment", "kind"),
        ("environment", "digest"),
        ("scorer", "kind"),
        ("scorer", "digest"),
    ],
)
@pytest.mark.parametrize("invalid", ["contains space", "x" * 257, "/absolute", "a/b"])
def test_component_labels_match_event_identifier_boundary(section, field, invalid):
    data = payload()
    data[section][field] = invalid
    with pytest.raises(IdentityError):
        RunIdentityV1(data)


def test_validate_resume_admits_a_journaled_version_in_admitted_versions():
    original = RunIdentityV1.from_payload(payload())
    kwargs = dict(
        stored_digest=original.digest,
        stored_run_id="r",
        requested_run_id="r",
        producing_version="0.3.0a0",
        cases=cases(),
        version_resolver=lambda _: __version__,
    )
    with pytest.raises(IdentityError, match="package_version_mismatch"):
        original.validate_resume(original, **kwargs)
    original.validate_resume(original, **kwargs, admitted_versions={"0.3.0a0"})
    with pytest.raises(IdentityError, match="package_version_mismatch"):
        original.validate_resume(original, **kwargs, admitted_versions={"0.3.0a1"})


def test_sample_retries_absent_keeps_the_digest():
    assert "sample_retries" not in RunIdentityV1(payload()).to_payload()
    assert RunIdentityV1(payload()).sample_retries is None


def test_sample_retries_present_is_in_the_digest():
    base = RunIdentityV1(payload())
    retried = RunIdentityV1({**payload(), "sample_retries": 1})
    assert retried.sample_retries == 1 and retried.digest != base.digest
    assert RunIdentityV1({**payload(), "sample_retries": 0}).digest != base.digest
    assert RunIdentityV1(retried.to_payload()).digest == retried.digest


@pytest.mark.parametrize("value", [True, -1, 9, "1", None, 1.0])
def test_sample_retries_is_a_bounded_integer(value):
    with pytest.raises(IdentityError):
        RunIdentityV1({**payload(), "sample_retries": value})


#: `RunIdentityV1.from_payload(payload()).digest` at 4f23dda0, before G2a.
FULL_IDENTITY_DIGEST = "036613ba3b4fbb5a2776ac0b9faff0ca38793dcfa5ec227a48fa6568907b0e2b"


def test_an_identity_without_case_subset_keeps_its_bytes():
    identity = RunIdentityV1.from_payload(payload())
    assert "case_subset" not in identity.to_payload()
    assert identity.case_subset is None
    assert identity.digest == FULL_IDENTITY_DIGEST


def test_row_provenance_is_optional_and_strict():
    plain = RunIdentityV1.from_payload(payload())
    assert plain.row_provenance is False and plain.digest == FULL_IDENTITY_DIGEST
    marked = RunIdentityV1.from_payload({**payload(), "row_provenance": 1})
    assert marked.row_provenance is True and marked.digest != plain.digest
    for bad in (True, 0, 2, "1", None):
        with pytest.raises(IdentityError) as caught:
            RunIdentityV1.from_payload({**payload(), "row_provenance": bad})
        assert str(caught.value) == "invalid_identity_fields"


def three_cases():
    return [EvalCase(case_id, "g", "s", (), {}, {}, {}) for case_id in ("a", "b", "c")]


def subset_payload(indices, case_ids):
    data = payload()
    data["corpus_digest"] = corpus_digest(three_cases())
    data["schedule"] = [
        dict(case_id=case_id, sample_id="s1", repeat=1, requested_seed=42) for case_id in case_ids
    ]
    data["case_subset"] = indices
    return data


def test_case_subset_enters_the_digest_and_selects_its_cases():
    identity = RunIdentityV1.from_payload(subset_payload([0, 2], ["a", "c"]))
    assert identity.case_subset == (0, 2)
    assert identity.to_payload()["case_subset"] == [0, 2]
    assert set(identity.validate_corpus(three_cases())) == {"a", "c"}
    assert RunIdentityV1(identity.to_payload()).digest == identity.digest
    without = subset_payload([0, 2], ["a", "c"])
    del without["case_subset"]
    bare = RunIdentityV1.from_payload(without)
    assert bare.digest != identity.digest
    # Without the selector the same schedule no longer matches the corpus.
    with pytest.raises(IdentityError, match="run_identity_changed"):
        bare.validate_corpus(three_cases())


@pytest.mark.parametrize(
    "subset", [[], [2, 0], [0, 0], [-1], [True], ["0"], [0.0], "0", None, {0: 0}]
)
def test_case_subset_is_sorted_distinct_non_negative_indices(subset):
    with pytest.raises(IdentityError, match="invalid_case_subset"):
        RunIdentityV1.from_payload(subset_payload(subset, ["a"]))


def test_case_subset_names_as_many_cases_as_the_schedule():
    with pytest.raises(IdentityError, match="invalid_case_subset"):
        RunIdentityV1.from_payload(subset_payload([0, 1], ["a"]))


def test_a_selector_outside_the_corpus_is_refused():
    identity = RunIdentityV1.from_payload(subset_payload([0, 3], ["a", "d"]))
    with pytest.raises(IdentityError, match="invalid_case_subset"):
        identity.validate_corpus(three_cases())
    with pytest.raises(IdentityError, match="invalid_case_subset"):
        checked_case_subset([0, 3], 3)
    assert checked_case_subset([0, 2], 3) == (0, 2)


def test_a_selector_naming_other_cases_than_the_schedule_is_refused():
    identity = RunIdentityV1.from_payload(subset_payload([0, 1], ["a", "c"]))
    with pytest.raises(IdentityError, match="run_identity_changed"):
        identity.validate_corpus(three_cases())


def test_a_subset_identity_resumes_only_under_the_same_selector():
    stored = RunIdentityV1.from_payload(subset_payload([0, 2], ["a", "c"]))
    kwargs = dict(
        stored_digest=stored.digest,
        stored_run_id="r",
        requested_run_id="r",
        producing_version=__version__,
        cases=three_cases(),
        version_resolver=lambda _: __version__,
    )
    stored.validate_resume(stored, **kwargs)
    other = RunIdentityV1.from_payload(subset_payload([0, 1], ["a", "b"]))
    with pytest.raises(IdentityError, match="run_identity_changed"):
        stored.validate_resume(other, **kwargs)


def _repeats_payload(case_repeats, counts):
    """`subset_payload([0, 1], ["a", "b"])` with `counts[case]` repeats per case
    (1 by default) and, unless `None`, the `case_repeats` selector."""

    data = subset_payload([0, 1], ["a", "b"])
    data["schedule"] = [
        dict(case_id=case_id, sample_id=f"s{repeat}", repeat=repeat, requested_seed=41 + repeat)
        for case_id in ("a", "b")
        for repeat in range(1, counts.get(case_id, 1) + 1)
    ]
    if case_repeats is not None:
        data["case_repeats"] = case_repeats
    return data


def test_case_repeats_is_an_optional_selector_beside_case_subset():
    identity = RunIdentityV1.from_payload(_repeats_payload([[1, 2]], {"b": 2}))
    assert identity.to_payload()["case_repeats"] == [[1, 2]]
    assert set(identity.validate_corpus(three_cases())) == {"a", "b"}
    assert RunIdentityV1(identity.to_payload()).digest == identity.digest


def test_a_nonuniform_schedule_without_case_repeats_is_still_refused():
    with pytest.raises(IdentityError, match="nonuniform_schedule"):
        RunIdentityV1.from_payload(_repeats_payload(None, {"b": 2}))


@pytest.mark.parametrize(
    "value, counts",
    [
        ([], {"b": 2}),  # empty
        ([[1, 1]], {"b": 2}),  # count not above R
        ([[1, 2], [1, 3]], {"b": 2}),  # index named twice
        ([[1, 2], [0, 2]], {"a": 2, "b": 2}),  # unsorted
        ([[0, 2], [1, 2]], {"a": 2, "b": 2}),  # every case listed: no base R
        ([[2, 2]], {"b": 2}),  # index outside case_subset
        ([[1, 3]], {"b": 2}),  # the schedule does not follow the count
        ("x", {"b": 2}),
        ([[1, "2"]], {"b": 2}),
    ],
)
def test_a_malformed_case_repeats_is_refused(value, counts):
    with pytest.raises(IdentityError, match="invalid_case_repeats"):
        RunIdentityV1.from_payload(_repeats_payload(value, counts))


def test_an_identity_without_case_repeats_keeps_its_bytes():
    payload = subset_payload([0, 2], ["a", "c"])
    identity = RunIdentityV1.from_payload(payload)
    assert "case_repeats" not in identity.to_payload()
    assert identity.digest == RunIdentityV1.from_payload(subset_payload([0, 2], ["a", "c"])).digest


def test_a_case_repeats_identity_keys_its_condition_on_the_base_repeats():
    """A listed first case does not lengthen the condition's seed tuple past R."""

    extended = RunIdentityV1.from_payload(_repeats_payload([[0, 2]], {"a": 2}))
    uniform = RunIdentityV1.from_payload(_repeats_payload(None, {}))
    assert extended.condition_key() == uniform.condition_key()


def test_validate_corpus_derives_each_cases_count_from_the_corpus():
    """A schedule out of corpus order extends another case than the index names."""

    data = _repeats_payload([[0, 2]], {"b": 2})
    data["schedule"] = [row for row in data["schedule"] if row["case_id"] == "b"] + [
        row for row in data["schedule"] if row["case_id"] == "a"
    ]
    identity = RunIdentityV1.from_payload(data)  # well formed over its rows alone
    with pytest.raises(IdentityError, match="run_identity_changed"):
        identity.validate_corpus(three_cases())


# -- the probe journal's profile `connection_preflight/1` --------


def probe_payload(*, seed_mode="exact", seed=7, rows=None, subset=(0,)):
    """Choice 5's probe identity: the full corpus, one selected case, one row."""

    data = payload()
    data["profile"] = "connection_preflight/1"
    data["target"]["seed_mode"] = seed_mode
    data["corpus_digest"] = corpus_digest(three_cases())
    data["schedule"] = rows or [dict(case_id="a", sample_id="probe", repeat=1, requested_seed=seed)]
    if subset is not None:
        data["case_subset"] = list(subset)
    return data


def test_the_probe_profile_admits_one_row_on_one_selected_case():
    identity = RunIdentityV1.from_payload(probe_payload())
    assert identity.to_payload()["profile"] == "connection_preflight/1"
    assert identity.case_subset == (0,)
    assert set(identity.validate_corpus(three_cases())) == {"a"}
    assert RunIdentityV1(identity.to_payload()).digest == identity.digest
    for mode in ("unpaired", "none"):
        assert RunIdentityV1.from_payload(probe_payload(seed_mode=mode, seed=None))


def test_the_probe_profile_refuses_a_second_row():
    two = [
        dict(case_id="a", sample_id="probe", repeat=1, requested_seed=7),
        dict(case_id="b", sample_id="probe-2", repeat=1, requested_seed=7),
    ]
    with pytest.raises(IdentityError, match="invalid_schedule"):
        RunIdentityV1.from_payload(probe_payload(rows=two, subset=(0, 1)))
    repeated = [
        dict(case_id="a", sample_id="probe", repeat=1, requested_seed=7),
        dict(case_id="a", sample_id="probe-2", repeat=2, requested_seed=8),
    ]
    with pytest.raises(IdentityError, match="invalid_schedule"):
        RunIdentityV1.from_payload(probe_payload(rows=repeated))


def test_the_probe_profile_names_its_case_by_selector():
    with pytest.raises(IdentityError, match="invalid_case_subset"):
        RunIdentityV1.from_payload(probe_payload(subset=None))


@pytest.mark.parametrize("mode", ["unpaired", "none"])
def test_the_probe_profile_refuses_a_seed_outside_exact(mode):
    with pytest.raises(IdentityError, match="invalid_seed"):
        RunIdentityV1.from_payload(probe_payload(seed_mode=mode, seed=7))
    with pytest.raises(IdentityError, match="invalid_seed"):
        RunIdentityV1.from_payload(probe_payload(seed_mode="exact", seed=None))


def test_an_unknown_profile_is_still_refused():
    with pytest.raises(IdentityError, match="invalid_identity_profile"):
        RunIdentityV1.from_payload({**payload(), "profile": "connection_preflight/2"})


# -- the `connection_binding` identity marker --------


def test_connection_binding_is_optional_and_strict():
    plain = RunIdentityV1.from_payload(payload())
    assert plain.connection_binding is False and plain.digest == FULL_IDENTITY_DIGEST
    marked = RunIdentityV1.from_payload({**payload(), "connection_binding": 1})
    assert marked.connection_binding is True and marked.digest != plain.digest
    assert marked.to_payload()["connection_binding"] == 1
    assert RunIdentityV1(marked.to_payload()).digest == marked.digest
    both = RunIdentityV1.from_payload({**payload(), "connection_binding": 1, "row_provenance": 1})
    assert both.connection_binding and both.row_provenance
    for bad in (True, 0, 2, "1", None, 1.0):
        with pytest.raises(IdentityError) as caught:
            RunIdentityV1.from_payload({**payload(), "connection_binding": bad})
        assert str(caught.value) == "invalid_identity_fields"


@pytest.mark.parametrize("profile", ["rollout_evaluation/1", "connection_preflight/1"])
def test_only_a_measurement_identity_carries_the_marker(profile):
    data = probe_payload() if profile == "connection_preflight/1" else payload()
    data["profile"] = profile
    assert RunIdentityV1.from_payload(data)
    with pytest.raises(IdentityError, match="invalid_identity_fields"):
        RunIdentityV1.from_payload({**data, "connection_binding": 1})
