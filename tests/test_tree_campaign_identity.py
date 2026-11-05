import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.tree import campaign_identity
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.attestation import TargetPreflight
from evolution.tree.campaign_caveats import LineageCaveat
from evolution.tree.campaign_identity import (
    AdaptationIdentityV1,
    CampaignIdentityV1,
    CapabilitiesInsufficient,
    LinkedFrom,
    admit_capabilities,
    build_adaptation_identity,
    build_identity,
    case_subset_for,
    derived_identity,
    evaluation_set_id,
    instances_per_measurement,
    measurement_id_for,
    measurement_identity,
    metric_plan_digest,
    observe_case_repeats,
    schedule_for,
    schedule_for_ids,
)
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.execution.authorizations import Authorization, lineage_instance_digest
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    EnvironmentIdentity,
    EvalCase,
    ScorerIdentity,
    SemanticIdentity,
)
from evolution.tree.execution.journal_identity import IdentityError, corpus_digest
from evolution.tree.roles.identities import EditorIdentity, ReflectorIdentity
from evolution.tree.target.config import ProbeSection
from tests.campaign_fakes import v11_payload, v11_policy


def policy_payload(**overrides):
    """Raw policy blueprint for context-free identity value tests."""
    base = v11_payload(
        repeats=10,
        round_stop="all_attempts",
        refresh=0,
        fresh_parent=False,
        **{
            "rounds": 2,
            "attempts_per_round": 3,
            "parallel_attempts": 2,
            "indeterminate": {
                o: {"max_count": 2, "max_rate": "0.10"}
                for o in ("target", "environment", "scorer", "rollout")
            },
            "budget": {"max_turns": 8, "timeout_seconds": 120, "max_failed_turns": 1},
            "max_concurrency": 4,
            "max_scorer_concurrency": 16,
            "metric": {"primary": "score"},
            "random_seed": 12345,
            "editor_timeout_seconds": 900,
            "editor_context": "ctx",
            "reflection_timeout_seconds": 60,
        },
    )
    base.update(overrides)
    return base


ENV = EnvironmentIdentity("static", "e" * 64)
SCORER = SimpleNamespace(identity=ScorerIdentity("judge", "s" * 64))
EDITOR = EditorIdentity("internal", "d" * 64)
REFLECTOR = ReflectorIdentity("internal", "9" * 64)
STORE_POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
STORE_IDENTITY = ArtifactStoreIdentityV1(STORE_POLICY, "1" * 64, "2" * 64)
INDETERMINATE_OWNERS = ("target", "environment", "scorer", "rollout")


def _semantic(seed_mode="exact"):
    return SemanticIdentity(
        contract_digest="contract",
        dependency_digest="dep",
        settings_hash="settings",
        seed_mode=seed_mode,
        protocol_version="1",
        runtime_ref_digest="runtime",
    )


@pytest.fixture
def policy():
    return CampaignPolicyV11.from_payload(policy_payload())


@pytest.fixture
def corpus():
    return [
        EvalCase("c1", "g1", "s1", (), {}, {}, {}),
        EvalCase("c2", "g1", "s1", (), {}, {}, {}),
    ]


@pytest.fixture
def preflight():
    capabilities = AdapterCapabilities("exact", 2, True, frozenset({"artifact_root_injection"}), 16)
    return TargetPreflight(_semantic("exact"), capabilities, "boot-1")


@pytest.fixture
def preflight_unpaired():
    capabilities = AdapterCapabilities(
        "unpaired", 2, True, frozenset({"artifact_root_injection"}), 16
    )
    return TargetPreflight(_semantic("unpaired"), capabilities, "boot-1")


@pytest.fixture
def preflight_none():
    capabilities = AdapterCapabilities("none", 2, True, frozenset({"artifact_root_injection"}), 16)
    return TargetPreflight(_semantic("none"), capabilities, "boot-1")


def _linked_from(**overrides):
    base = dict(
        run_id="predecessor",
        prefix_digest="a" * 64,
        final_champion_digest="b" * 64,
        final_champion_dependency_digest="c" * 64,
        rounds_closed=1,
        alpha_rounds={1: "0.010000000000"},
        indeterminate_counts=dict.fromkeys(INDETERMINATE_OWNERS, 0),
        denominator=10,
    )
    base.update(overrides)
    return LinkedFrom(**base)


def test_a_link_without_d2a_keys_keeps_its_payload_and_digest():
    base = _linked_from()
    assert "condition_changed" not in base.to_payload()
    assert LinkedFrom.from_payload(base.to_payload()) == base


DECLARATION = dict(
    mode="inherit",
    source_measurement_id="m-r00-champion",
    source_prefix_digest="a" * 64,
    pairs_digest="b" * 64,
    imported=118,
    scheduled=120,
    fields=["identity.target.runtime_ref_digest"],
    origin_runs=1,
)


def test_a_fresh_link_keeps_the_a14_payload_and_digest():
    assert "baseline" not in _linked_from().to_payload()


def test_an_inherit_declaration_round_trips():
    linked = _linked_from(baseline=DECLARATION)
    assert LinkedFrom.from_payload(linked.to_payload()) == linked
    assert linked.to_payload()["baseline"] == DECLARATION


@pytest.mark.parametrize(
    "bad",
    [
        {**DECLARATION, "mode": "fresh"},
        {**DECLARATION, "imported": 121},
        {**DECLARATION, "fields": ["b", "a"]},
        {**DECLARATION, "x": 1},
    ],
)
def test_an_invalid_declaration_is_refused(bad):
    with pytest.raises(IdentityError):
        _linked_from(baseline=bad)


def test_baseline_fields_admit_bare_names_only_for_comparable_top_level_fields():
    """Final fix wave: a bare name is a comparable top-level field an in-place
    change moved (`target`); any other bare token is refused, dotted paths stay."""

    bare = _linked_from(baseline={**DECLARATION, "fields": ["identity.policy_digest", "target"]})
    assert LinkedFrom.from_payload(bare.to_payload()) == bare
    for name in ("producing_version", "anything", "seed_root"):
        with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
            _linked_from(baseline={**DECLARATION, "fields": [name]})


def test_a_changed_link_round_trips_its_optional_keys():
    changed = replace(
        _linked_from(),
        condition_changed=("identity.scorer.digest", "seed_root.candidate_digest"),
        lineage_caveats=(
            LineageCaveat("condition_changed", 1, ("identity.target.runtime_ref_digest",), "run-0"),
        ),
    )
    assert LinkedFrom.from_payload(changed.to_payload()) == changed


@pytest.mark.parametrize("fields", [("b.x", "a.y"), ("a.y", "a.y"), ("Identity.X",)])
def test_condition_changed_must_be_sorted_unique_dotted(fields):
    with pytest.raises(IdentityError):
        replace(_linked_from(), condition_changed=fields)


def _lineage_decision(digest: str | None = None) -> Authorization:
    linked = _linked_from()
    return Authorization.new(
        "lineage_not_finished",
        digest or lineage_instance_digest(linked.run_id, linked.prefix_digest),
        "stuck a12",
        now="2026-09-18T10:00:00Z",
    )


def test_an_unfinished_link_needs_its_decision_and_only_then():
    decision = _lineage_decision()
    unfinished = replace(
        _linked_from(), predecessor_finished=False, authorization=decision.decision_payload()
    )
    assert LinkedFrom.from_payload(unfinished.to_payload()) == unfinished
    with pytest.raises(IdentityError):
        replace(_linked_from(), predecessor_finished=False)
    with pytest.raises(IdentityError):
        replace(_linked_from(), authorization=decision.decision_payload())
    with pytest.raises(IdentityError):
        replace(_linked_from(), predecessor_finished=True)


def test_an_unfinished_link_decision_must_name_this_predecessor_prefix():
    """Final review: the embedded decision is bound to `(run_id, prefix_digest)`.

    A decision for another predecessor, or for this one at another prefix,
    would otherwise ride inside a creation record it never authorized.
    """

    stranger = _lineage_decision("c" * 64)
    with pytest.raises(IdentityError, match="invalid_identity_fields"):
        replace(
            _linked_from(),
            predecessor_finished=False,
            authorization=stranger.decision_payload(),
        )
    decision = _lineage_decision()
    with pytest.raises(IdentityError, match="invalid_identity_fields"):
        replace(
            _linked_from(prefix_digest="d" * 64),
            predecessor_finished=False,
            authorization=decision.decision_payload(),
        )


def test_linked_from_mappings_are_frozen():
    """PDR-0003: `linked_from` is journaled inside the identity, so it freezes too.

    The inherited totals decide the successor's thresholds and its alpha ledger;
    a caller that could move them after `campaign_created` would leave the digest
    saying one thing and the campaign doing another.
    """

    counts = dict.fromkeys(INDETERMINATE_OWNERS, 0)
    linked = _linked_from(alpha_rounds={1: "0.010000000000"}, indeterminate_counts=counts)
    payload = linked.to_payload()

    for mapping, key, value in (
        (linked.alpha_rounds, 1, "0.900000000000"),
        (linked.indeterminate_counts, "target", 99),
    ):
        with pytest.raises(TypeError):
            mapping[key] = value  # type: ignore[index]

    # The caller's own dict is not the stored one either.
    counts["target"] = 99
    assert linked.indeterminate_counts["target"] == 0
    assert linked.to_payload() == payload


def test_measurement_identity_is_derived_and_schedule_follows_the_rollout_design(
    policy, corpus, preflight
):
    identity = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    run_identity = measurement_identity(identity, corpus, "a" * 64, "candidate")
    data = run_identity.to_payload()
    assert (
        data["candidate_digest"] == "a" * 64 and data["target"] == identity.to_payload()["target"]
    )
    rows = data["schedule"]
    assert rows[0] == dict(
        case_id=corpus[0].case_id, sample_id="01", repeat=1, requested_seed=policy.random_seed + 1
    )
    assert len(rows) == len(corpus) * policy.repeats


def test_unpaired_schedule_carries_the_side_and_no_seed(policy, corpus, preflight_unpaired):
    rows = schedule_for(policy, corpus, "unpaired", "champion")
    assert rows[0]["sample_id"] == "champion-01" and rows[0]["requested_seed"] is None


def test_none_seed_mode_schedule_through_measurement_identity(policy, corpus, preflight_none):
    rows = schedule_for(policy, corpus, "none", "candidate")
    assert rows[0]["sample_id"] == "candidate-01" and rows[0]["requested_seed"] is None

    identity = build_identity(
        policy,
        preflight_none,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    run_identity = measurement_identity(identity, corpus, "a" * 64, "champion")
    data = run_identity.to_payload()
    assert data["target"]["seed_mode"] == "none"
    assert data["schedule"][0] == dict(
        case_id=corpus[0].case_id, sample_id="champion-01", repeat=1, requested_seed=None
    )
    assert len(data["schedule"]) == len(corpus) * policy.repeats


def test_capability_admission_refuses_oversubscription(policy):
    caps = AdapterCapabilities(
        "exact", 1, True, frozenset({"artifact_root_injection"}), max_instances=2
    )
    with pytest.raises(CapabilitiesInsufficient):
        admit_capabilities(policy, caps)  # peak_demand = 2 × ceil(4/1) = 8 > 2


def test_capability_admission_accepts_sufficient_capacity(policy):
    caps = AdapterCapabilities(
        "exact", 1, True, frozenset({"artifact_root_injection"}), max_instances=12
    )
    assert instances_per_measurement(policy, caps) == 4
    # ADR-0032: all_attempts also reserves all three finalists: 3 x 4 == 12.
    admit_capabilities(policy, caps)


def test_identity_digest_changes_with_editor_and_linked_from(policy, corpus, preflight):
    base = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    other_editor = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EditorIdentity("other", "f" * 64),
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    assert other_editor.digest != base.digest

    linked = _linked_from()
    linked_identity = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        linked,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    assert linked_identity.digest != base.digest
    assert linked_identity.to_payload()["linked_from"] == linked.to_payload()


def test_identity_round_trips_through_payload(policy, corpus, preflight):
    identity = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        _linked_from(),
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    restored = CampaignIdentityV1.from_payload(identity.to_payload())
    assert restored.digest == identity.digest
    assert restored.to_payload() == identity.to_payload()


def test_from_payload_refuses_wrong_profile(policy, corpus, preflight):
    identity = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    data = identity.to_payload()
    data["profile"] = "other"
    with pytest.raises(IdentityError):
        CampaignIdentityV1.from_payload(data)


def test_measurement_identity_refuses_a_corpus_that_does_not_match_the_digest(
    policy, corpus, preflight
):
    identity = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )
    other_corpus = [EvalCase("other", "g1", "s1", (), {}, {}, {})]
    with pytest.raises(IdentityError):
        measurement_identity(identity, other_corpus, "a" * 64, "candidate")


def test_metric_plan_digest_and_evaluation_set_id_are_pure_functions(corpus):
    assert metric_plan_digest("score") == metric_plan_digest("score")
    assert metric_plan_digest("score") != metric_plan_digest("other")
    assert evaluation_set_id(corpus) == "corpus-" + corpus_digest(corpus)[:16]


def test_measurement_id_for_champion_and_attempt():
    assert measurement_id_for(None) == "m-r00-champion"
    assert measurement_id_for("a1") == "m-a1"


def _identity(policy, preflight, corpus, linked=None):
    return build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        linked,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )


def test_corpus_cases_are_the_three_fields_in_corpus_order(policy, preflight):
    corpus = [
        EvalCase(
            "z",
            "g2",
            "long",
            ("t",),
            {"text": "PUBLIC-CANARY"},
            {"s": "PRIVATE-CANARY"},
            {"o": "ORACLE-CANARY"},
        ),
        EvalCase("a", "g1", "short", (), {}, {}, {}),
    ]
    identity = _identity(policy, preflight, corpus)
    assert identity.corpus_cases == (
        {"case_id": "z", "group_id": "g2", "stratum": "long"},
        {"case_id": "a", "group_id": "g1", "stratum": "short"},
    )
    canonical = identity.to_payload()
    for canary in ("PUBLIC-CANARY", "PRIVATE-CANARY", "ORACLE-CANARY", '"t"'):
        assert canary not in json.dumps(canonical)
    assert CampaignIdentityV1.from_payload(canonical).corpus_cases == identity.corpus_cases


def test_unicode_case_id_roundtrips_in_campaign_identity(policy, preflight):
    corpus = [EvalCase("пример-1", "group", "short", (), {}, {}, {})]
    identity = _identity(policy, preflight, corpus)
    assert CampaignIdentityV1.from_payload(identity.to_payload()).corpus_cases[0]["case_id"] == (
        "пример-1"
    )


@pytest.mark.parametrize(
    ("cases", "code"),
    [
        ([], "invalid_corpus"),
        (
            [{"case_id": "a", "group_id": "g", "stratum": "s", "tags": []}],
            "invalid_identity_fields",
        ),
        ([{"case_id": "a", "group_id": "g"}], "invalid_identity_fields"),
        ([{"case_id": "a b", "group_id": "g", "stratum": "s"}], "invalid_identity_identifier"),
        ([{"case_id": "a", "group_id": " ", "stratum": "s"}], "invalid_identity_text"),
        ([{"case_id": "a", "group_id": "g", "stratum": 3}], "invalid_identity_text"),
        (
            [
                {"case_id": "a", "group_id": "g", "stratum": "s"},
                {"case_id": "a", "group_id": "g", "stratum": "s"},
            ],
            "invalid_corpus",
        ),
    ],
)
def test_corpus_cases_shape_is_refused(policy, preflight, corpus, cases, code):
    payload = _identity(policy, preflight, corpus).to_payload()
    payload["corpus_cases"] = cases
    with pytest.raises(IdentityError, match=f"^{code}$"):
        CampaignIdentityV1.from_payload(payload)


def test_group_change_changes_both_digests(policy, preflight):
    one = _identity(policy, preflight, [EvalCase("a", "g1", "s", (), {}, {}, {})])
    other = _identity(policy, preflight, [EvalCase("a", "g2", "s", (), {}, {}, {})])
    assert one.corpus_digest != other.corpus_digest
    assert one.digest != other.digest
    assert one.corpus_cases != other.corpus_cases


@pytest.mark.parametrize("side", ["champion", "candidate"])
def test_derived_identity_equals_measurement_identity(
    policy, preflight, preflight_none, corpus, side
):
    for flight in (preflight, preflight_none):
        identity = _identity(policy, flight, corpus)
        derived = derived_identity(identity, "f" * 64, side)
        assert derived.digest == measurement_identity(identity, corpus, "f" * 64, side).digest
    with pytest.raises(IdentityError, match="^run_identity_changed$"):
        measurement_identity(identity, corpus[:1], "f" * 64, side)


def test_schedule_for_ids_matches_schedule_for(policy, corpus):
    assert schedule_for_ids(
        policy, [c.case_id for c in corpus], "exact", "candidate"
    ) == schedule_for(policy, corpus, "exact", "candidate")


@pytest.fixture
def identity_payload(policy, preflight, corpus):
    return build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=ReflectorIdentity("subprocess", "a" * 64),
        environment_docs_digest=None,
        probe=None,
    ).to_payload()


def test_identity_reflector_slot_is_closed(identity_payload):
    payload = dict(identity_payload)
    payload["reflector"] = {
        "kind": "subprocess",
        "digest": "a" * 64,
        "environment_docs_digest": None,
        "probe": None,
    }
    CampaignIdentityV1(payload)
    payload["reflector"]["extra"] = 1
    with pytest.raises(IdentityError):
        CampaignIdentityV1(payload)
    del payload["reflector"]
    with pytest.raises(IdentityError):
        CampaignIdentityV1(payload)


def test_probe_identity_payload_is_canonical_and_round_trips(policy, corpus, preflight):
    probe = ProbeSection(
        base_url="http://127.0.0.1:8099",
        health_path="/ready",
        methods=("POST", "GET"),
        budget=7,
        request_timeout_seconds=2.5,
        description="Disposable environment",
    )
    payload = campaign_identity.probe_identity_payload(probe)
    assert payload == {
        "base_url": "http://127.0.0.1:8099",
        "health_path": "/ready",
        "methods": ["POST", "GET"],
        "budget": 7,
        "request_timeout_seconds": 2.5,
        "description_sha256": "29a3073ff7f938a1005c40bf0af5b0ff0fe19686cc1a7e19fda62697a9ed02fb",
    }
    identity = build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=payload,
    )
    restored = CampaignIdentityV1.from_payload(identity.to_payload())
    assert restored.to_payload() == identity.to_payload()
    assert restored.digest == identity.digest
    assert campaign_identity.probe_identity_payload(None) is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("base_url", "http://localhost:8099"),
        ("health_path", "/other"),
        ("methods", ["GET", "POST"]),
        ("budget", 8),
        ("request_timeout_seconds", 3.0),
        ("description_sha256", None),
    ],
)
def test_each_probe_field_changes_campaign_identity(
    policy, corpus, preflight, field, replacement
) -> None:
    probe = campaign_identity.probe_identity_payload(
        ProbeSection(
            base_url="http://127.0.0.1:8099",
            health_path="/ready",
            methods=("POST", "GET"),
            budget=7,
            request_timeout_seconds=2.5,
            description="Disposable environment",
        )
    )
    changed = dict(probe)
    changed[field] = replacement

    def with_probe(value):
        return build_identity(
            policy,
            preflight,
            ENV,
            SCORER,
            EDITOR,
            corpus,
            STORE_IDENTITY,
            "artifact_root_injection",
            None,
            reflector=REFLECTOR,
            environment_docs_digest=None,
            probe=value,
        )

    assert with_probe(changed).digest != with_probe(probe).digest


def test_probe_identity_does_not_change_measurement_seed_or_run_identity(
    policy, corpus, preflight
) -> None:
    def campaign(probe):
        return build_identity(
            policy,
            preflight,
            ENV,
            SCORER,
            EDITOR,
            corpus,
            STORE_IDENTITY,
            "artifact_root_injection",
            None,
            reflector=REFLECTOR,
            environment_docs_digest=None,
            probe=probe,
        )

    without_probe = campaign(None)
    with_probe = campaign(
        campaign_identity.probe_identity_payload(ProbeSection(base_url="http://127.0.0.1:8099"))
    )
    assert without_probe.digest != with_probe.digest
    assert (
        derived_identity(without_probe, "f" * 64, "candidate").to_payload()
        == derived_identity(with_probe, "f" * 64, "candidate").to_payload()
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("base_url", 1),
        ("health_path", None),
        ("methods", "GET"),
        ("methods", ["GET", 1]),
        ("budget", True),
        ("request_timeout_seconds", float("inf")),
        ("description_sha256", "short"),
    ],
)
def test_probe_identity_refuses_wrong_field_types(identity_payload, field, replacement) -> None:
    payload = identity_payload
    probe = campaign_identity.probe_identity_payload(ProbeSection(base_url="http://127.0.0.1:8099"))
    probe[field] = replacement
    payload["reflector"]["probe"] = probe
    with pytest.raises(IdentityError):
        CampaignIdentityV1.from_payload(payload)


def test_probe_identity_refuses_unknown_keys(identity_payload) -> None:
    probe = campaign_identity.probe_identity_payload(ProbeSection(base_url="http://127.0.0.1:8099"))
    probe["ephemeral_url"] = "http://127.0.0.1:1234/capability/probe"
    identity_payload["reflector"]["probe"] = probe
    with pytest.raises(IdentityError, match="^invalid_identity_fields$"):
        CampaignIdentityV1.from_payload(identity_payload)


# -- adaptation identity `/2` (scorer-diagnostics spec §5, ADR-0021) ---------

ADAPTATION_COMMON: dict[str, Any] = dict(
    grouping={"kind": "fields", "features": []},
    selection={"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
    families={"kind": "metadata", "path": "/family"},
    attempt_feedback="sequential",
    reflection_metadata_digest="0" * 64,
    environment_docs_digest=None,
)


def test_adaptation_v2_with_metadata_families_records_an_empty_membership() -> None:
    identity = build_adaptation_identity(**ADAPTATION_COMMON, family_membership={"c1": None})
    assert identity.to_payload()["family_membership"] == []


@pytest.mark.parametrize(
    "membership",
    [
        [["c2", "q"], ["c1", "q"]],
        [["c1", "q"], ["c1", "r"]],
        [["c1", ""]],
        [["c1"]],
        [["c1", 3]],
        "c1",
    ],
)
def test_adaptation_v2_refuses_unsorted_duplicate_or_blank_membership(membership: Any) -> None:
    payload = build_adaptation_identity(**ADAPTATION_COMMON, family_membership={}).to_payload()
    payload["family_membership"] = membership
    with pytest.raises(IdentityError):
        AdaptationIdentityV1.from_payload(payload)


def _campaign(policy, preflight, corpus):
    return build_identity(
        policy,
        preflight,
        ENV,
        SCORER,
        EDITOR,
        corpus,
        STORE_IDENTITY,
        "artifact_root_injection",
        None,
        reflector=REFLECTOR,
        environment_docs_digest=None,
        probe=None,
    )


@pytest.fixture
def three():
    return [EvalCase(case_id, "g1", "s1", (), {}, {}, {}) for case_id in ("c1", "c2", "c3")]


def test_case_repeats_extends_the_listed_cases_with_byte_identical_rows():
    policy = v11_policy(fresh_parent=True, mode="observe", repeats=1)
    plain = schedule_for_ids(policy, ["c1", "c2"], "exact", "champion")
    extended = schedule_for_ids(policy, ["c1", "c2"], "exact", "champion", case_repeats={"c2": 2})
    assert extended[:2] == plain
    assert extended[2] == {
        "case_id": "c2",
        "sample_id": "02",
        "repeat": 2,
        "requested_seed": policy.random_seed + 2,
    }


def test_observe_case_repeats_follows_the_four_conditions():
    observe = v11_policy(
        fresh_parent=True, mode="observe", repeats=1
    )  # measured_noise, fresh_parent by default
    assert observe_case_repeats(observe, [1, 3]) == [[1, 2], [3, 2]]
    assert observe_case_repeats(observe, []) is None
    assert (
        observe_case_repeats(v11_policy(fresh_parent=True, mode="observe", repeats=2), [1]) is None
    )
    assert (
        observe_case_repeats(
            v11_policy(fresh_parent=True, mode="observe", repeats=1, tolerance=0.0), [1]
        )
        is None
    )
    assert (
        observe_case_repeats(v11_policy(mode="observe", repeats=1, fresh_parent=False), [1]) is None
    )
    assert (
        observe_case_repeats(v11_policy(fresh_parent=True, mode="statistical", repeats=4), [1])
        is None
    )


def test_a_derivation_with_case_repeats_lists_them_and_extends_the_schedule(
    policy, three, preflight
):
    identity = _campaign(policy, preflight, three)
    count = policy.repeats + 1
    derived = derived_identity(
        identity, "a" * 64, "champion", case_subset=(0, 2), case_repeats=[[2, count]]
    )
    data = derived.to_payload()
    assert data["case_repeats"] == [[2, count]]
    assert [row["repeat"] for row in data["schedule"] if row["case_id"] == "c3"] == list(
        range(1, count + 1)
    )
    assert (
        measurement_identity(
            identity, three, "a" * 64, "champion", case_subset=(0, 2), case_repeats=[[2, count]]
        )
        == derived
    )


def test_a_subset_keeps_the_corpus_commitment_and_the_full_schedules_rows(policy, three, preflight):
    identity = _campaign(policy, preflight, three)
    full = derived_identity(identity, "a" * 64, "candidate").to_payload()
    subset = derived_identity(identity, "a" * 64, "candidate", case_subset=(0, 2))
    data = subset.to_payload()
    assert data["case_subset"] == [0, 2]
    assert data["corpus_digest"] == full["corpus_digest"] == identity.corpus_digest
    assert data["schedule"] == [row for row in full["schedule"] if row["case_id"] != "c2"]
    rest = {k: v for k, v in data.items() if k not in ("schedule", "case_subset")}
    assert rest == {k: v for k, v in full.items() if k != "schedule"}
    assert set(subset.validate_corpus(three)) == {"c1", "c3"}
    assert (
        measurement_identity(identity, three, "a" * 64, "candidate", case_subset=(0, 2)) == subset
    )


def test_an_unpaired_subset_keeps_the_sides_sample_ids(policy, three, preflight_unpaired):
    identity = _campaign(policy, preflight_unpaired, three)
    full = derived_identity(identity, "a" * 64, "candidate").to_payload()["schedule"]
    subset = derived_identity(identity, "a" * 64, "candidate", case_subset=(1,))
    assert subset.to_payload()["schedule"] == [row for row in full if row["case_id"] == "c2"]
    assert {row["sample_id"] for row in subset.to_payload()["schedule"]} == {
        f"candidate-{repeat:02d}" for repeat in range(1, policy.repeats + 1)
    }


def test_a_selector_of_every_case_is_still_a_subset_identity(policy, three, preflight):
    identity = _campaign(policy, preflight, three)
    full = derived_identity(identity, "a" * 64, "candidate")
    every = derived_identity(identity, "a" * 64, "candidate", case_subset=(0, 1, 2))
    assert every.to_payload()["schedule"] == full.to_payload()["schedule"]
    assert every.digest != full.digest


@pytest.mark.parametrize("subset", [(), (3,), (1, 0), (0, 0), (-1,)])
def test_a_selector_outside_the_corpus_or_malformed_is_refused(policy, three, preflight, subset):
    identity = _campaign(policy, preflight, three)
    with pytest.raises(IdentityError, match="invalid_case_subset"):
        derived_identity(identity, "a" * 64, "candidate", case_subset=subset)


def test_the_audit_derives_a_subset_identity_from_the_selector_alone(policy, three, preflight):
    identity = _campaign(policy, preflight, three)
    stored = measurement_identity(identity, three, "a" * 64, "candidate", case_subset=(1, 2))
    # No corpus: the campaign identity and the stored selector are enough (ADR-0008).
    again = derived_identity(identity, "a" * 64, "candidate", case_subset=stored.case_subset)
    assert again.digest == stored.digest


def test_a_new_epoch_rederivation_keeps_the_case_set(policy, three, preflight):
    """G2 spec §3.6: `continue` re-derives from the new identity and the source's selector."""

    before = _campaign(policy, preflight, three)
    source = derived_identity(before, "a" * 64, "candidate", case_subset=(0, 2))
    changed = TargetPreflight(
        replace(preflight.semantic, settings_hash="settings-after-the-change"),
        preflight.capabilities,
        "boot-2",
    )
    after = _campaign(policy, changed, three)
    moved = derived_identity(after, "a" * 64, "candidate", case_subset=source.case_subset)
    assert moved.case_subset == (0, 2)
    assert moved.to_payload()["schedule"] == source.to_payload()["schedule"]
    assert moved.to_payload()["target"]["settings_hash"] == "settings-after-the-change"
    assert moved.digest != source.digest


def test_case_subset_for_names_each_corpus_case_once(policy, three, preflight):
    identity = _campaign(policy, preflight, three)
    assert case_subset_for(identity, ["c3", "c1"]) == (0, 2)
    for bad in ([], ["c1", "c1"], ["c1", "zzz"], "c1"):
        with pytest.raises(IdentityError, match="invalid_case_subset"):
            case_subset_for(identity, bad)


def test_identity_without_secret_oracle_paths_keeps_its_bytes(tmp_path, preflight, corpus):
    from evolution.tree.target.connection_contract import (
        ConnectionContract,
        MetricContract,
        RoleVisibility,
        SecretOraclePaths,
    )
    from tests.family15_fakes import v16_document
    from tests.tree.manifest.test_manifest_v15 import load

    policy = load(tmp_path, v16_document()).workflow.campaign
    connection = ConnectionContract(MetricContract("pointwise", ()), RoleVisibility(()))
    payload = _identity(policy, preflight, corpus).to_payload()
    payload.update(connection.identity_fields())
    assert (
        CampaignIdentityV1(payload).digest
        == "ff88442ae1eef46faf4cd083445b8c0670c7281eab2101c2fd8b811b3d5963fd"
    )
    declared = replace(connection, secret_oracle_paths=SecretOraclePaths(("/reference",)))
    payload.update(declared.identity_fields())
    assert payload["secret_oracle_paths"] == declared.secret_oracle_paths.digest
    assert (
        CampaignIdentityV1(payload).digest
        != "ff88442ae1eef46faf4cd083445b8c0670c7281eab2101c2fd8b811b3d5963fd"
    )


def test_secret_oracle_paths_requires_both_connection_fields(identity_payload):
    for connection in ({}, {"metric_contract": "a" * 64}):
        with pytest.raises(ValueError):
            CampaignIdentityV1({**identity_payload, **connection, "secret_oracle_paths": "b" * 64})


def test_checks_bind_only_when_declared_and_keep_absent_identity_bytes(tmp_path, preflight, corpus):
    from evolution.tree.execution.check_modes import parse_checks
    from evolution.tree.target.connection_contract import (
        ConnectionContract,
        MetricContract,
        RoleVisibility,
    )
    from tests.family15_fakes import v16_document
    from tests.tree.manifest.test_manifest_v15 import load

    policy = load(tmp_path, v16_document()).workflow.campaign
    connection = ConnectionContract(MetricContract("pointwise", ()), RoleVisibility(()))
    payload = _identity(policy, preflight, corpus).to_payload() | connection.identity_fields()
    assert (
        CampaignIdentityV1(payload).digest
        == "ff88442ae1eef46faf4cd083445b8c0670c7281eab2101c2fd8b811b3d5963fd"
    )
    checks = parse_checks({"version": "checks/1", "modes": {"seal_oracle_leak": "off"}})
    declared = replace(connection, checks=checks)
    identity = CampaignIdentityV1(payload | declared.identity_fields())
    assert identity.to_payload()["checks"] == checks.digest
    assert identity.digest != CampaignIdentityV1(payload).digest


def test_family14_refuses_an_identity_with_preflight_sample_retries():
    import hashlib

    from evolution.tree.campaign_schema import validate_payload
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.storage.envelope import SchemaError
    from tests.campaign_drivers import native14_created

    value = native14_created()
    value["identity"]["preflight_sample_retries"] = 0
    value["identity_digest"] = hashlib.sha256(canonical_json(value["identity"])).hexdigest()
    with pytest.raises(SchemaError):
        validate_payload("campaign_created", value, final=True)


@pytest.mark.parametrize("value", [True, -1, "0", None])
def test_identity_refuses_invalid_preflight_retry_counts(identity_payload, value):
    with pytest.raises(IdentityError):
        CampaignIdentityV1(
            {
                **identity_payload,
                "metric_contract": "a" * 64,
                "role_visibility": "b" * 64,
                "preflight_sample_retries": value,
            }
        )
