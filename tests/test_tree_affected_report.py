from types import SimpleNamespace

import pytest

from evolution.tree.affected_report import (
    affected_caveats,
)


def _snapshot(exam):
    cases = [{"case_id": name} for name in ("c1", "c2", "c3")]
    rounds = {1: SimpleNamespace(gate_scope={"cases": exam})}
    return SimpleNamespace(identity={"corpus_cases": cases}, rounds=rounds), cases


def _bias(number, attempt_id):
    return {"kind": "focus_selection_bias", "round": number, "attempt_id": attempt_id}


# -- G2b5 Task 3: `completion_regression` and `provenance_drift` -----------------


# -- Alpha and final look from the completion's own facts -------------------------


# -- G2b3b Task 5: `components_changed_by_condition` ------------------------------

INSPECTED = "a" * 64
BASELINE_MAP = "b" * 64
OTHER = "c" * 64
COMPONENT = {"mode": "component", "fallback_reason": None}


def _drift_snapshot(selection=COMPONENT):
    attempt = SimpleNamespace(attempt_id="r01-a01", index=1, round=1, selection=selection)
    measurements = {
        "m-r00-champion": {"attempt_id": "champion", "role": "baseline"},
        "m-r01-a01": {"attempt_id": "r01-a01", "role": "candidate"},
        "m-r01-a01@e1": {"attempt_id": "r01-a01", "role": "candidate"},
        "m-r01-a01-parent": {"attempt_id": "r01-a01", "role": "parent"},
    }
    rounds = {1: SimpleNamespace(attempts={"r01-a01": attempt}, cohort=None)}
    return SimpleNamespace(rounds=rounds, measurements=measurements)


def _references():
    from evolution.tree.component_drift import DriftReference

    return {
        ("r01-a01", "candidate"): DriftReference("candidate", "t1", INSPECTED, 0),
        ("r01-a01", "parent"): DriftReference("parent", "t0", BASELINE_MAP, 0),
    }


def _attested(measurement_id, digest=None):
    payload = {"measurement_id": measurement_id, "instance_id": f"i-{measurement_id}"}
    if digest is not None:
        payload["components_digest"] = digest
    return payload


def _drift_item(sides):
    return {
        "kind": "components_changed_by_condition",
        "round": 1,
        "attempt_id": "r01-a01",
        "sides": sides,
    }


# -- F7 Task 7: the sides of family-13 cohort members (plan choice 10) -----------

CHAMPION_MAP = "d" * 64


def _cohort_snapshot(selection=COMPONENT):
    """`_drift_snapshot` with round 1's cohort: the Champion and the Attempt's finalist."""

    snapshot = _drift_snapshot(selection)
    snapshot.measurements.update(
        {
            "m-r01-cohort-champion": {
                "attempt_id": "champion",
                "round": 1,
                "role": "cohort",
                "cohort_role": "champion",
            },
            "m-r01-cohort-a01": {
                "attempt_id": "r01-a01",
                "round": 1,
                "role": "cohort",
                "cohort_role": "finalist",
            },
            # A member a `continue` re-bound counts for its side.
            "m-r01-cohort-a01@e1": {
                "attempt_id": "r01-a01",
                "round": 1,
                "role": "cohort",
                "cohort_role": "finalist",
            },
        }
    )
    return snapshot


def _champions():
    from evolution.tree.component_drift import DriftReference

    return {1: DriftReference("champion", "t0", CHAMPION_MAP, 0)}


def _champion_item():
    return {
        "kind": "components_changed_by_condition",
        "round": 1,
        "attempt_id": None,
        "sides": ["cohort_champion"],
    }


# -- F8 Task 5: family 13's method caveats, focus bias and cohort drift ----------


def _identity(mode):
    return {"policy": {"gate": {"mode": mode}}}


def _looked(*looks, attempt_id="r01-a01", index=1, number=1, mode="observe"):
    """One Attempt whose evaluated looks carry `looks` (each a caveat list),
    bound under a `mode` comparison in a campaign whose only identity is `mode`."""

    attempt = SimpleNamespace(
        attempt_id=attempt_id,
        index=index,
        measurement_id=f"m-{attempt_id}",
        comparison={"mode": mode},
        looks={look: {"caveats": list(kinds)} for look, kinds in enumerate(looks, start=1)},
    )
    return SimpleNamespace(
        identity=_identity(mode),
        epochs=(_identity(mode),),
        rounds={number: SimpleNamespace(attempts={attempt_id: attempt}, cohort=None)},
    )


#: The two campaign-level observe items (PDR-0022).
OBSERVE_ITEMS = [
    {"kind": "observe_gate_not_enforced", "round": None, "attempt_id": None},
    {"kind": "uncalibrated_point_estimates", "round": None, "attempt_id": None},
]


def _method(kind, attempt_id="r01-a01", number=1):
    return {"kind": kind, "round": number, "attempt_id": attempt_id}


# -- Campaign-level observe items (PDR-0022) ----------------------------------


def _campaign(*identities, rounds=None):
    """A family-13 snapshot whose creation and comparable epochs are
    `identities[:-1]` (or the one identity) and whose policy in force is the last."""

    epochs = identities[:-1] or identities
    return SimpleNamespace(identity=identities[-1], epochs=epochs, rounds=rounds or {})


def _refused(caveats, **kwargs):
    from evolution.tree.campaign_projections import _validate_caveats

    with pytest.raises(Exception):
        _validate_caveats(caveats, **kwargs)


# -- H12b Task 5: family 14's caveat items (plan choices 12-14) -------------------

#: Choice 13: family 14's campaign-level items, in their leading order.
CHAMPION_ITEM = {"kind": "champion_not_contemporaneous", "round": None, "attempt_id": None}


def _identity14(mode="statistical", stability="unstable"):
    return {"policy": {"gate": {"mode": mode, "stability": stability}}}


def _contrast(attempt_id, *, side="fresh", exact=None, removed=(), stored="m-r00-champion"):
    return {
        "attempt_id": attempt_id,
        "champion_measurement_id": stored if side == "stored" else "m-r01-final-1-champion",
        "finalist_measurement_id": f"m-{attempt_id}-final",
        "champion_side": side,
        "exact": exact,
        "removed_pairs": removed,
    }


def _looked14(*looks, comparisons=None, mode="statistical", stability="unstable", finals=()):
    """`_looked` in family 14: the round also carries its global comparisons
    (Attempt id -> contrast) and its final bindings."""

    snapshot = _looked(*looks, mode=mode)
    snapshot.identity = _identity14(mode, stability)
    snapshot.epochs = (snapshot.identity,)
    state = snapshot.rounds[1]
    state.global_comparisons = dict(comparisons or {})
    state.finals = tuple(finals)
    return snapshot


def test_family14_method_caveats_read_the_family14_look_kinds():
    from evolution.tree.affected_report import method_caveats

    snapshot = _looked14(
        ["noise_not_observed", "intervals_empirical"], ["exact_contrast", "intervals_empirical"]
    )
    assert method_caveats(snapshot, final=True) == [
        _method("intervals_empirical"),
        _method("exact_contrast"),
        _method("noise_not_observed"),
    ]
    # Family 13's call keeps its vocabulary: the family-14 kinds are not read.
    assert method_caveats(snapshot) == [_method("intervals_empirical")]


@pytest.mark.parametrize(
    ("looks", "exact", "expected"),
    [
        ([["exact_contrast"]], None, 1),  # a look only
        ([["intervals_empirical"]], True, 1),  # the global contrast only
        ([["exact_contrast"]], True, 1),  # both: one item per Attempt
        ([["intervals_empirical"]], False, 0),
        ([["intervals_empirical"]], None, 0),
    ],
)
def test_one_exact_contrast_item_per_attempt(looks, exact, expected):
    from evolution.tree.affected_report import method_caveats

    snapshot = _looked14(*looks, comparisons={"r01-a01": _contrast("r01-a01", exact=exact)})
    kinds = [item["kind"] for item in method_caveats(snapshot, final=True)]
    assert kinds.count("exact_contrast") == expected
    assert kinds == sorted(set(kinds), key=kinds.index)


def test_a_global_exact_contrast_names_its_attempt_in_kind_order():
    from evolution.tree.affected_report import method_caveats

    snapshot = _looked14(
        ["intervals_empirical", "noise_not_observed"],
        comparisons={"r01-a01": _contrast("r01-a01", exact=True)},
    )
    assert method_caveats(snapshot, final=True) == [
        _method("intervals_empirical"),
        _method("exact_contrast"),
        _method("noise_not_observed"),
    ]


@pytest.mark.parametrize(
    ("identities", "finals", "expected"),
    [
        ([_identity14()], (), []),
        ([_identity14(stability="stable")], (), [CHAMPION_ITEM]),
        ([_identity14("observe")], (), OBSERVE_ITEMS),
        ([_identity14("observe", "stable")], (), [*OBSERVE_ITEMS, CHAMPION_ITEM]),
        # Created stable, rebaselined to unstable: any own identity counts.
        ([_identity14(stability="stable"), _identity14()], (), [CHAMPION_ITEM]),
        # A stable final binding under unstable identities.
        ([_identity14()], ({"mode": "statistical", "stability": "stable"},), [CHAMPION_ITEM]),
    ],
)
def test_final_campaign_caveats(identities, finals, expected):
    from evolution.tree.affected_report import final_campaign_caveats

    state = SimpleNamespace(attempts={}, cohort=None, finals=finals)
    snapshot = _campaign(*identities, rounds={1: state})
    assert final_campaign_caveats(snapshot) == expected


def _stored(attempt_id="r01-a01", number=1, count=2):
    return {
        "kind": "stored_pairs_excluded",
        "round": number,
        "attempt_id": attempt_id,
        "measurement_id": "m-r00-champion",
        "count": count,
    }


def test_stored_pairs_excluded_per_stored_contrast_with_removed_pairs():
    from evolution.tree.affected_report import stored_caveats

    pairs = [["c1", 0], ["c2", 1]]
    comparisons = {
        "r01-a01": _contrast("r01-a01", side="stored", removed=pairs),
    }
    snapshot = _looked14(["intervals_empirical"], comparisons=comparisons, stability="stable")
    assert stored_caveats(snapshot) == [_stored()]
    # A removal summary (H5) is counted, not listed.
    comparisons["r01-a01"]["removed_pairs"] = {"count": 65, "digest": "a" * 64}
    assert stored_caveats(snapshot) == [_stored(count=65)]
    # A stored contrast with no removed pair, and a fresh contrast, report none.
    comparisons["r01-a01"]["removed_pairs"] = []
    assert stored_caveats(snapshot) == []
    comparisons["r01-a01"] = _contrast("r01-a01", removed=[])
    assert stored_caveats(snapshot) == []


def test_family14_affected_caveats_lead_and_order():
    """Choice 13: the campaign items lead (observe pair, then the stored
    Champion's); an Attempt's items follow `AFFECTED_CAVEAT_KINDS14`."""

    snapshot = _looked14(
        ["observe_gate_not_enforced", "noise_not_observed"],
        comparisons={
            "r01-a01": _contrast("r01-a01", side="stored", exact=True, removed=[["c1", 0]])
        },
        mode="observe",
        stability="stable",
    )
    policies = {1: SimpleNamespace(fresh_parent=False)}
    drift = {
        "kind": "provenance_drift",
        "round": 1,
        "attempt_id": "r01-a01",
        "measurement_id": "m",
        "cases": ["q1"],
    }
    assert affected_caveats(snapshot, policies, {1: [drift]}, final=True) == [
        *OBSERVE_ITEMS,
        CHAMPION_ITEM,
        _bias(1, "r01-a01"),
        drift,
        _method("observe_gate_not_enforced"),
        _method("exact_contrast"),
        _method("noise_not_observed"),
        _stored(count=1),
    ]
    # `stable` alone: the stored Champion's item leads.
    statistical = _looked14(["intervals_empirical"], stability="stable")
    assert affected_caveats(
        statistical, {1: SimpleNamespace(fresh_parent=True)}, {}, final=True
    ) == [
        CHAMPION_ITEM,
        _method("intervals_empirical"),
    ]


def _final_snapshot():
    """`_cohort_snapshot` in family 14: the members' registry role is `final`."""

    snapshot = _cohort_snapshot()
    snapshot.family_version = 14
    for entry in snapshot.measurements.values():
        if entry["role"] == "cohort":
            entry["role"] = "final"
    return snapshot


def test_family14_drift_sides_say_final():
    """M4 (plan choice 14): `final_finalist` and `final_champion` where
    family 13 says `cohort_finalist` and `cohort_champion`."""

    from evolution.tree.affected_report import drift_caveats
    from evolution.tree.component_drift import DriftReference

    attested = {
        "m-r01-a01": [_attested("m-r01-a01", OTHER)],
        "m-r01-cohort-a01": [_attested("m-r01-cohort-a01", OTHER)],
        "m-r01-cohort-champion": [_attested("m-r01-cohort-champion", OTHER)],
    }
    references = {
        **_references(),
        ("r01-a01", "final_finalist"): DriftReference("candidate", "t1", INSPECTED, 0),
    }
    assert drift_caveats(_final_snapshot(), attested, references, _champions(), final=True) == [
        _drift_item(["candidate", "final_finalist"]),
        {**_champion_item(), "sides": ["final_champion"]},
    ]


def test_read_cohort_drift_items_reads_every_final_of_a_first_accepted_round(tmp_path, monkeypatch):
    """Choice 14: under `first_accepted` each finalist has its own final; the
    first finalist's drift is reported too, not only the latest final's."""

    from dataclasses import replace

    from evolution.tree import affected_report
    from tests.test_tree_final_report import _attempt_id, _first_accepted, _member_id

    state = _first_accepted("unstable")
    state = replace(
        state,
        cohort=state.finals[-1],
        attempts={
            _attempt_id(n): SimpleNamespace(attempt_id=_attempt_id(n), index=n) for n in (1, 2)
        },
    )
    snapshot = SimpleNamespace(family_version=14, rounds={1: state})
    read = []
    monkeypatch.setattr(affected_report, "round_policy", lambda snapshot, number: "policy")
    monkeypatch.setattr(affected_report, "_drift_checked", lambda policy, attempt: True)
    monkeypatch.setattr(
        affected_report,
        "_measurement_records",
        lambda root, member: (read.append(member) or [member], None),
    )
    monkeypatch.setattr(affected_report, "baseline_provenance", lambda records: records)
    monkeypatch.setattr(
        affected_report, "completion_row_facts", lambda snapshot, attempt_id, rows: (None, rows)
    )
    monkeypatch.setattr(
        affected_report,
        "provenance_drift_item",
        lambda snapshot, policy, attempt, member, executed: {
            "kind": "provenance_drift",
            "attempt_id": attempt.attempt_id,
            "measurement_id": member,
        },
    )
    members = [_member_id(1, 1), _member_id(2, 2)]
    siblings = {member: "born" for member in members}
    found = affected_report.read_cohort_drift_items(tmp_path, snapshot, siblings)
    assert read == members
    assert [(item["attempt_id"], item["measurement_id"]) for item in found[1]] == [
        (_attempt_id(1), members[0]),
        (_attempt_id(2), members[1]),
    ]


# -- H12b Task 5: the family-14 caveat validators (plan choices 12, 13, R45) -------


def _refused14(caveats, **kwargs):
    """A strict refusal: the validator's own error, never a `TypeError`."""

    from evolution.tree.campaign_projections import _validate_caveats
    from evolution.tree.campaign_schema import SchemaError

    with pytest.raises(SchemaError, match="^invalid public projection$"):
        _validate_caveats(caveats, **kwargs)


ORDER = {"r01-a01": (1, 0)}
FINAL_ITEMS = {
    "exact_contrast": [_method("exact_contrast")],
    "noise_not_observed": [_method("noise_not_observed")],
    "champion_not_contemporaneous": [CHAMPION_ITEM],
    "stored_pairs_excluded": [_stored()],
    "final_finalist": [{**_drift_item(["candidate", "final_finalist"])}],
    "final_champion": [{**_champion_item(), "sides": ["final_champion"]}],
}


@pytest.mark.parametrize("name", list(FINAL_ITEMS))
def test_each_family14_kind_validates_only_in_a_family14_document(name):
    from evolution.tree.campaign_projections import _validate_caveats

    caveats = FINAL_ITEMS[name]
    _validate_caveats(caveats, attempt_order=ORDER, confidence=True, final=True)
    _refused14(caveats, attempt_order=ORDER, confidence=True)
    _refused14(caveats, attempt_order=ORDER)


@pytest.mark.parametrize(
    "caveats",
    [
        [_method("observe_calibration_degenerate")],
        [
            {
                "kind": "completion_regression",
                "round": 1,
                "attempt_id": "r01-a01",
                "measurement_id": "m",
                "cases": 1,
                "lower": 0.0,
                "upper": 0.1,
                "alpha": "0.05",
                "margin": "0",
            }
        ],
        [_drift_item(["candidate", "cohort_finalist"])],
        [_champion_item()],
    ],
)
def test_family13_only_items_are_refused_in_a_family14_document(caveats):

    _refused14(caveats, attempt_order=ORDER, confidence=True, final=True)


@pytest.mark.parametrize(
    "caveats",
    [
        # The stored Champion's item follows the observe pair, never precedes it.
        [CHAMPION_ITEM, *OBSERVE_ITEMS],
        [OBSERVE_ITEMS[0], CHAMPION_ITEM, OBSERVE_ITEMS[1]],
        [CHAMPION_ITEM, CHAMPION_ITEM],
        [{**CHAMPION_ITEM, "attempt_id": "r01-a01"}],
        [{**CHAMPION_ITEM, "round": 1}],
        # The observe pair stays both or neither.
        [OBSERVE_ITEMS[0], CHAMPION_ITEM],
        # Exactly `{kind, round, attempt_id, measurement_id, count}`, a positive count.
        [_stored(count=0)],
        [{**_stored(), "count": True}],
        [{key: value for key, value in _stored().items() if key != "measurement_id"}],
        [{**_stored(), "measurement_id": None}],
        [{**_stored(), "attempt_id": None}],
        [{**_stored(), "round": None}],
        [{**_stored(), "extra": 1}],
        # Choice 12's order inside an Attempt: method kinds, then stored pairs.
        [_stored(), _method("exact_contrast")],
        [_method("noise_not_observed"), _method("exact_contrast")],
        [{**_drift_item(["final_finalist", "candidate"])}],
    ],
)
def test_the_family14_validator_refuses_a_bad_item(caveats):
    _refused14(caveats, attempt_order=ORDER, confidence=True, final=True)


def test_the_family14_validator_admits_choice_13s_order():
    from evolution.tree.campaign_projections import _validate_caveats

    caveats = [
        *OBSERVE_ITEMS,
        CHAMPION_ITEM,
        _bias(1, "r01-a01"),
        _method("observe_gate_not_enforced"),
        _method("exact_contrast"),
        _method("noise_not_observed"),
        _stored(),
        {**_champion_item(), "sides": ["final_champion"]},
    ]
    for observe in (False, True):
        _validate_caveats(
            caveats, attempt_order=ORDER, confidence=True, final=True, observe=observe
        )
    # `stable` alone: the stored Champion's item leads without the observe pair,
    # which an Attempt's observe item or an observe round still requires.
    stable = [item for item in caveats if item not in OBSERVE_ITEMS]
    stable.remove(_method("observe_gate_not_enforced"))
    _validate_caveats(stable, attempt_order=ORDER, confidence=True, final=True)
    _refused14(stable, attempt_order=ORDER, confidence=True, final=True, observe=True)
    _refused14(
        [CHAMPION_ITEM, _bias(1, "r01-a01"), _method("observe_gate_not_enforced")],
        attempt_order=ORDER,
        confidence=True,
        final=True,
    )


def _fallback(reason):
    return {
        "kind": "grouping_fallback",
        "round": 1,
        "reason": reason,
        "fallback_kind": "fields",
        "scope": "all",
    }


def test_grouping_fallback_reasons_follow_the_family():
    """R45 (validator half): family 14's reasons in a family-14 document,
    family 9's below it."""

    from evolution.tree.campaign_projections import _validate_caveats
    from evolution.tree.campaign_schema import (
        GROUPING_FALLBACK_REASONS,
        GROUPING_FALLBACK_REASONS14,
    )

    for reason in sorted(GROUPING_FALLBACK_REASONS14):
        _validate_caveats([_fallback(reason)], model_grouping=True, confidence=True, final=True)
    _refused14([_fallback("turns_exhausted")], model_grouping=True, confidence=True)
    family9_only = sorted(GROUPING_FALLBACK_REASONS - GROUPING_FALLBACK_REASONS14)
    assert family9_only
    for reason in family9_only:
        _refused14([_fallback(reason)], model_grouping=True, confidence=True, final=True)


@pytest.mark.slow
@pytest.mark.parametrize("family", [15, 16])
@pytest.mark.parametrize("side", ["parent", "candidate"])
async def test_audit_rederives_withheld_selection(tmp_path, monkeypatch, family, side):
    from evolution.tree.campaign_projections import _check_affected_cases_selected
    from tests.test_tree_b18_secret_components import _withheld_selection_run

    context, selected = await _withheld_selection_run(tmp_path, monkeypatch, family, side)
    _check_affected_cases_selected(context, selected)
    from evolution.tree.campaign_projections import verify_evidence

    assert verify_evidence(context.run_root)["mismatches"] == []


@pytest.mark.slow
@pytest.mark.parametrize("family", [15, 16])
@pytest.mark.parametrize("side", ["parent", "candidate"])
async def test_audit_refuses_subset_with_withheld_map(tmp_path, monkeypatch, family, side):
    from dataclasses import replace

    from evolution.tree.campaign_projections import (
        _check_affected_cases_selected,
        _PreconditionFailed,
    )
    from evolution.tree.campaign_schema import campaign_family_for
    from evolution.tree.storage.envelope import Envelope
    from tests.test_tree_b18_secret_components import _withheld_selection_run

    context, selected = await _withheld_selection_run(tmp_path, monkeypatch, family, side)
    forged = selected.to_payload()
    forged["payload"].update(cases=[0], fallback_reason=None)
    wrong = Envelope.from_payload(forged, family=campaign_family_for(family - 1))
    changed = replace(
        context, records=tuple(wrong if row is selected else row for row in context.records)
    )
    with pytest.raises(_PreconditionFailed, match="selection differs from its re-derivation"):
        _check_affected_cases_selected(changed, wrong)

    if side == "parent":
        forged["payload"].update(cases=[0, 1], fallback_reason="inspection_failed")
        wrong = Envelope.from_payload(forged, family=campaign_family_for(family - 1))
        changed = replace(
            context, records=tuple(wrong if row is selected else row for row in context.records)
        )
        with pytest.raises(_PreconditionFailed, match="inspection that was never due"):
            _check_affected_cases_selected(changed, wrong)
