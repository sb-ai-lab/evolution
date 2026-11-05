"""Stored final contrasts proved from genuine sibling journals, independently of replay."""

from __future__ import annotations

import pytest

from tests.campaign_drivers import OBSERVE, _statistical
from tests.test_tree_campaign_family14_audit import audit, cut_run, forge
from tests.test_tree_campaign_family14_gate_audit import journal_consistent, row_finding
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow
STABLE = {"stability": "stable"}


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
async def test_stored_final_journals_audit_clean(tmp_path, monkeypatch, mode, fresh):
    config = OBSERVE if mode == "observe" else _statistical(mode)
    config = {**config, "gate": {**config["gate"], "fresh_parent": fresh}}
    run, _ = await family14_run(
        tmp_path, monkeypatch, config, family14_settings=STABLE, campaign={"rounds": 2}
    )
    journal_consistent(run)
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "field,value", [("point", 0.123), ("lower", 0.0123), ("pairing", "unpaired"), ("exact", True)]
)
async def test_stored_numerical_forgery_passes_replay_but_fails_rows(
    tmp_path, monkeypatch, field, value
):
    run = await cut_run(
        tmp_path, monkeypatch, config=_statistical("bound"), family14_settings=STABLE
    )
    assert payloads(run, "global_comparison_completed")[0][field] != value

    def edit(event, payload):
        if event == "global_comparison_completed":
            payload[field] = value
            if field == "exact":
                payload["lower"] = payload["point"]

    forge(run, edit)
    row_finding(run, "global_comparison_completed", field)


@pytest.mark.parametrize(
    "corruption", ["one_side", "missing_pairs", "extra_pairs", "extra_cases", "false_exact"]
)
async def test_stored_exclusions_and_exactness_need_rows(tmp_path, monkeypatch, corruption):
    from tests.campaign_drivers import LENIENT
    from tests.test_tree_campaign_family14_gate_audit import (
        CONSTANT,
        _ExcludeLastPair,
        scorer_tables,
    )

    # c1/04 is the sole varying pair: its exclusion must remove the fresh value too.
    scorer = scorer_tables([0, 0, 0, 0.25], [0.5, 0.5, 0.5, 1], [1] * 4, [1] * 4)
    config = {**CONSTANT, "repeats": 4, "adapter_class": _ExcludeLastPair, "scorer": scorer}
    run = await cut_run(
        tmp_path,
        monkeypatch,
        config=config,
        family14_settings=STABLE,
        campaign={"indeterminate": LENIENT},
    )
    comparison = payloads(run, "global_comparison_completed")[0]
    assert comparison["removed_pairs"] == [["c1", 4]]
    assert (comparison["point"], comparison["lower"], comparison["exact"]) == (0.25, 0.25, True)
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if event != "global_comparison_completed":
            return
        if corruption == "one_side":
            payload.update(point=0.3125, lower=0.3125)
        elif corruption == "missing_pairs":
            payload["removed_pairs"] = []
        elif corruption == "extra_pairs":
            payload["removed_pairs"] = [["c1", 3], ["c1", 4]]
        elif corruption == "extra_cases":
            payload["removed_cases"] = ["c2"]
        else:
            payload["exact"] = False

    forge(run, edit)
    row_finding(
        run,
        "global_comparison_completed",
        {
            "one_side": "lower",
            "missing_pairs": "removed_pairs",
            "extra_pairs": "removed_pairs",
            "extra_cases": "removed_cases",
            "false_exact": "exact",
        }[corruption],
    )


@pytest.mark.parametrize(
    "corruption", ["missing_case", "summary_count", "summary_digest", "wrong_weight"]
)
async def test_removed_case_and_summary_forgeries_need_rows(tmp_path, monkeypatch, corruption):
    from tests.campaign_drivers import LENIENT, _flag_on_finalists_bound
    from tests.test_tree_campaign_family14_stored import _ranking_adapter, _ranking_scorer

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    run = await cut_run(
        tmp_path,
        monkeypatch,
        config={**OBSERVE, "scorer": _ranking_scorer(flag, "reduced")},
        adapter=_ranking_adapter(exclude_c2=True),
        extra_cases={"c3": {"critical": True, "pass": True}},
        family14_settings=STABLE,
        campaign={"indeterminate": LENIENT},
    )
    comparison = payloads(run, "global_comparison_completed")[0]
    assert comparison["removed_cases"] == ["c2"]
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if event != "global_comparison_completed":
            return
        if corruption == "missing_case":
            payload["removed_cases"] = []
        elif corruption == "summary_count":
            payload["removed_pairs"] = {"count": 65, "digest": "f" * 64}
        elif corruption == "summary_digest":
            payload["removed_cases"] = {"count": 1, "digest": "f" * 64}
        else:
            payload["point"] = 0.123

    forge(run, edit)
    row_finding(
        run,
        "global_comparison_completed",
        {
            "missing_case": "removed_cases",
            "summary_count": "removed_pairs",
            "summary_digest": "removed_cases",
            "wrong_weight": "point",
        }[corruption],
    )


@pytest.mark.parametrize("corruption", ["measurement", "digest", "later_prefix"])
async def test_stored_binding_corruption_is_separate_lifecycle_failure(
    tmp_path, monkeypatch, corruption
):
    from tests.test_tree_campaign_family14_audit import finding

    run = await cut_run(tmp_path, monkeypatch, family14_settings=STABLE)

    def edit(event, payload):
        if event != "final_measurement_bound":
            return
        if corruption == "measurement":
            payload["stored_champion"]["measurement_id"] = "m-r01-a01"
            payload["evidence"][0]["locator"] = "measurements/m-r01-a01/events.jsonl#bytes=0:100"
        elif corruption == "digest":
            payload["stored_champion"]["prefix_digest"] = "f" * 64
            payload["evidence"][0]["content_digest"] = "f" * 64
        else:
            ref = payload["evidence"][0]
            name, extent = ref["locator"].split("#bytes=")
            ref["locator"] = name + "#bytes=0:" + str(int(extent.split(":")[1]) + 1)

    forge(run, edit)
    finding(run, None, "round's complete baseline", code="suffix_refused")


@pytest.mark.parametrize("change", ["missing", "corrupt", "unreadable"])
@pytest.mark.parametrize("recompute", [True, False])
async def test_stored_reference_is_checked_even_without_numerical_recompute(
    tmp_path, monkeypatch, change, recompute
):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.corpus import load_reflection_corpus
    from tests.campaign_drivers import _measurement_events

    run = await cut_run(tmp_path, monkeypatch, family14_settings=STABLE)
    journal_consistent(run)
    original = (run / "campaign.jsonl").read_bytes()
    ledger = (run / "artifacts.jsonl").read_bytes()
    bound = payloads(run, "final_measurement_bound")[0]
    path = _measurement_events(run, bound["stored_champion"]["measurement_id"])
    corpus = load_reflection_corpus(run.parent / "corpus.jsonl").cases if recompute else []
    if change == "missing":
        path.unlink()
    elif change == "corrupt":
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"score":0.0', b'"score":0.1', 1))
        assert raw != path.read_bytes()
    else:
        read = type(path).read_bytes

        def unreadable(self):
            if self == path:
                raise PermissionError("stored prefix unreadable")
            return read(self)

        monkeypatch.setattr(type(path), "read_bytes", unreadable)
    if change == "unreadable":
        with pytest.raises(PermissionError, match="stored prefix unreadable"):
            verify_evidence(run, corpus)
    else:
        result = verify_evidence(run, corpus)
        assert result["corpus"] == ("proven" if recompute else "mismatch")
        for event in ("final_measurement_bound", "global_comparison_completed"):
            assert any(
                row["event"] == event
                and row["code"]
                == ("evidence_missing" if change == "missing" else "evidence_mismatch")
                for row in result["mismatches"]
            ), result
    assert (run / "campaign.jsonl").read_bytes() == original
    assert (run / "artifacts.jsonl").read_bytes() == ledger
    assert not (run / "measurements" / "m-r01-final-01-champion").exists()


@pytest.mark.parametrize("substitute", [False, True])
async def test_later_valid_baseline_rows_leave_settled_comparison_unchanged(
    tmp_path, monkeypatch, substitute
):
    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook
    saved = []

    def append_after_comparison(self, name):
        if name == "after_global_comparison_completed":
            state = self.snapshot().rounds[1]
            mid = state.baseline_measurement_id
            saved.append(dict(state.baseline_reference))
            self.measurements[mid]._require_journal().append(
                "journal_dispatch_stopped", {"code": "journal_limit_exceeded"}
            )
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", append_after_comparison)
    run, _ = await family14_run(
        tmp_path, monkeypatch, _statistical("bound"), family14_settings=STABLE
    )
    assert saved
    comparison = payloads(run, "global_comparison_completed")[0]
    assert comparison["evidence"][1] == saved[0]
    path, extent = saved[0]["locator"].split("#bytes=")
    raw = (run / path).read_bytes()
    assert len(raw) > int(extent.split(":")[1])
    journal_consistent(run)
    assert audit(run)["mismatches"] == []
    if substitute:
        import hashlib

        from tests.test_tree_campaign_family14_audit import finding

        def edit(event, payload):
            if event == "final_measurement_bound":
                payload["evidence"][0].update(
                    locator=f"{path}#bytes=0:{len(raw)}",
                    content_digest=hashlib.sha256(raw).hexdigest(),
                )
                payload["stored_champion"]["prefix_digest"] = hashlib.sha256(raw).hexdigest()

        forge(run, edit)
        finding(run, None, "round's complete baseline", code="suffix_refused")


class _PointOnlyCase:
    """Factory: keep c2/01 alone on the stored side, with fresh full coverage."""

    def __new__(cls, **kwargs):
        from tests.campaign_drivers import MANIFEST_SEED, _RepeatTagged

        class Adapter(_RepeatTagged):
            champions = 0

            def instance_for(self, candidate, instance_id, boot_id):
                instance = super().instance_for(candidate, instance_id, boot_id)
                if candidate.kind == "champion":
                    self.champions += 1
                    if self.champions == 1:
                        instance._lookup = lambda session: (
                            "target_failed"
                            if session.case_id == "c2"
                            and session.requested_seed - MANIFEST_SEED > 1
                            else "completed"
                        )
                return instance

        return Adapter(**kwargs)


@pytest.mark.parametrize(
    "field,value", [("point", 0.5), ("lower", 0.375), ("removed_cases", ["c2"])]
)
async def test_point_only_case_is_not_removed_or_used_in_bound(tmp_path, monkeypatch, field, value):
    from tests.campaign_drivers import LENIENT
    from tests.test_tree_campaign_family14_gate_audit import CONSTANT, scorer_tables

    config = {
        **CONSTANT,
        "adapter_class": _PointOnlyCase,
        "used": lambda case_id, turn: ["skills/SKILL.md"] if case_id == "c1" else [],
        "scorer": scorer_tables([0] * 4, [0.5] * 4, [0.5] * 4, [0.75] * 4),
    }
    # Fresh Parent keeps the Candidate causal gate measurable although stored c2 has one row.
    config["gate"] = {"fresh_parent": True}
    run = await cut_run(
        tmp_path,
        monkeypatch,
        config=config,
        family14_settings=STABLE,
        campaign={"indeterminate": LENIENT},
    )
    comparison = payloads(run, "global_comparison_completed")[0]
    assert (comparison["point"], comparison["lower"], comparison["removed_cases"]) == (
        0.375,
        0.5,
        [],
    )
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if event == "global_comparison_completed":
            payload[field] = value

    forge(run, edit)
    row_finding(run, "global_comparison_completed", field)


@pytest.mark.parametrize("corruption", [None, "count", "digest"])
async def test_genuine_large_removal_summary_is_fully_audited(tmp_path, monkeypatch, corruption):
    from tests.campaign_drivers import LENIENT
    from tests.test_tree_campaign_family14_gate_audit import scorer_tables
    from tests.test_tree_campaign_family14_stored import _ranking_adapter

    config = {
        **OBSERVE,
        "repeats": 66,
        "scorer": scorer_tables([0] * 4, [0.5] * 4, [1] * 4, [1] * 4),
    }
    adapter = _ranking_adapter(exclude_c2=True)
    adapter.used = lambda case_id, turn: ["skills/SKILL.md"] if case_id == "c1" else []
    run = await cut_run(
        tmp_path,
        monkeypatch,
        config=config,
        family14_settings=STABLE,
        adapter=adapter,
        campaign={
            "indeterminate": {
                owner: {**limit, "max_count": 1000} for owner, limit in LENIENT.items()
            }
        },
    )
    comparison = payloads(run, "global_comparison_completed")[0]
    assert comparison["removed_pairs"]["count"] == 66
    assert audit(run)["mismatches"] == []
    if corruption is None:
        return

    def edit(event, payload):
        if event == "global_comparison_completed":
            payload["removed_pairs"][corruption] = 67 if corruption == "count" else "f" * 64

    forge(run, edit)
    row_finding(run, "global_comparison_completed", "removed_pairs")


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("cut", ["after_baseline_bound", "after_cohort_measurement_bound"])
async def test_stored_and_selected_baseline_identity_survives_continue(
    tmp_path, monkeypatch, retained, cut
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import (
        EDITED,
        _cohort_scorer,
        _continue,
        _cut_hook,
        _flag_on_finalists_bound,
        _Resettable,
    )
    from tests.campaign_fakes import Crash

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    adapter = _Resettable(used=EDITED["used"])
    config = {
        **_statistical("bound"),
        "scorer": _cohort_scorer(flag, flip=True) if retained else _statistical("bound")["scorer"],
    }
    options = dict(adapter=adapter, family14_settings=STABLE, campaign={"rounds": 2})
    with monkeypatch.context() as patch:
        _cut_hook(patch, cut)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, config, **options)
    run, _ = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[_continue(refusal.value)], **options
    )
    selection = payloads(run, "selection_decided")[0]
    baseline = payloads(run, "baseline_complete")[0]["measurement_id"]
    final = payloads(run, "final_measurement_completed")[0]["measurement_id"]
    assert selection["measurement_id"] == (baseline if retained else final)
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == selection["measurement_id"]
    assert ("@e1" in baseline) == (cut == "after_baseline_bound")
    assert ("@e1" in final) == (cut == "after_cohort_measurement_bound")
    journal_consistent(run)
    assert audit(run)["mismatches"] == []

    from tests.test_tree_campaign_family14_audit import finding

    def edit(event, payload):
        if event == "baseline_bound" and payload["round"] == 2:
            payload["measurement_id"] = final if retained else baseline

    forge(run, edit)
    finding(run, None, "baseline", code="suffix_refused")


async def test_stored_measurement_identity_is_bound_without_numerical_recompute(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign_projections import verify_evidence

    run = await cut_run(tmp_path, monkeypatch, family14_settings=STABLE)

    def edit(event, payload):
        if event == "measurement_bound" and payload["attempt_id"] == "champion":
            payload["identity_digest"] = "f" * 64

    forge(run, edit)
    journal_consistent(run)
    result = verify_evidence(run, [])
    assert result["corpus"] == "mismatch"
    assert any(
        row["event"] == "final_measurement_bound"
        and row["detail"] == "the bound identity is not the derived one"
        for row in result["mismatches"]
    ), result


async def test_point_only_unseeded_case_cannot_claim_paired_comparison(tmp_path, monkeypatch):
    from tests.campaign_drivers import LENIENT, _UnseededRepeatTagged
    from tests.test_tree_campaign_family14_gate_audit import CONSTANT, scorer_tables

    class Adapter(_UnseededRepeatTagged):
        champions = 0

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                self.champions += 1
                if self.champions == 1:
                    instance._lookup = lambda session: (
                        "target_failed"
                        if session.case_id == "c2"
                        and self.counts.get((candidate.digest, "c2"), 0) > 1
                        else "completed"
                    )
            return instance

    config = {
        **CONSTANT,
        "adapter_class": Adapter,
        "used": lambda case_id, turn: ["skills/SKILL.md"] if case_id == "c1" else [],
        "scorer": scorer_tables([0] * 4, [0.5] * 4, [0.5] * 4, [0.75] * 4),
    }
    run = await cut_run(
        tmp_path,
        monkeypatch,
        config=config,
        family14_settings=STABLE,
        campaign={"indeterminate": LENIENT},
    )
    comparison = payloads(run, "global_comparison_completed")[0]
    assert (comparison["pairing"], comparison["point"], comparison["lower"]) == (
        "unpaired",
        0.375,
        0.5,
    )
    assert audit(run)["mismatches"] == []

    def edit(event, payload):
        if event == "global_comparison_completed":
            payload["pairing"] = "exact"

    forge(run, edit)
    row_finding(run, "global_comparison_completed", "pairing")


async def test_imported_point_only_provenance_corruption_is_refused_by_s1(tmp_path, monkeypatch):
    import json

    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import (
        LENIENT,
        MANIFEST_SEED,
        _continue,
        _measurement_events,
        _records,
        _RepeatTagged,
        _Resettable,
    )
    from tests.test_tree_campaign_family14_gate_audit import CONSTANT, scorer_tables

    class Adapter(_Resettable, _RepeatTagged):
        changed = False

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                instance._lookup = lambda session: (
                    "target_failed"
                    if (session.case_id == "c1" and not self.changed)
                    or (session.case_id == "c2" and session.requested_seed - MANIFEST_SEED > 1)
                    else "completed"
                )
            return instance

    adapter = Adapter(used=lambda case_id, turn: ["skills/SKILL.md"] if case_id == "c1" else [])
    config = {**CONSTANT, "scorer": scorer_tables([0] * 4, [0.5] * 4, [0.5] * 4, [0.75] * 4)}
    options = dict(
        adapter=adapter,
        family14_settings=STABLE,
        campaign={"indeterminate": {**LENIENT, "target": {"max_count": 8, "max_rate": "1"}}},
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, config, **options)
    assert snapshot.suspensions and not snapshot.finished
    source = _measurement_events(run, "m-r00-champion")
    original = source.read_bytes()
    adapter.changed = True
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, config, **options)
    run, _ = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[_continue(refusal.value)], **options
    )
    assert source.read_bytes() == original
    baseline = payloads(run, "baseline_complete")[0]["measurement_id"]
    imported = [
        row["payload"]
        for row in _records(_measurement_events(run, baseline))
        if row["event"] == "sample_imported"
    ]
    assert [(row["case_id"], row["sample_id"]) for row in imported] == [("c2", "01")]
    comparison = payloads(run, "global_comparison_completed")[0]
    assert (
        comparison["pairing"],
        comparison["point"],
        comparison["lower"],
        comparison["exact"],
    ) == ("exact", 0.375, 0.5, False)
    assert audit(run)["mismatches"] == []

    # Only the carried point-only row loses its dependency proof. The genuine
    # control above has real carry; this is a sibling-evidence forgery after it.
    import hashlib

    baseline_path = _measurement_events(run, baseline)
    records = _records(baseline_path)
    old = baseline_path.read_bytes()
    for row in records:
        if row["event"] == "sample_imported":
            row["payload"]["dependency_digest"] = "f" * 64
    raw = b"".join(
        (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode() for row in records
    )
    assert len(raw) == len(old) and raw != old
    baseline_path.write_bytes(raw)

    def rehash(value):
        if isinstance(value, list):
            for item in value:
                rehash(item)
        elif isinstance(value, dict):
            locator = value.get("locator", "")
            if locator.startswith(f"measurements/{baseline}/events.jsonl#bytes="):
                start, end = map(int, locator.split("#bytes=")[1].split(":"))
                value["content_digest"] = hashlib.sha256(raw[start:end]).hexdigest()
            for item in value.values():
                rehash(item)

    def edit(event, payload):
        rehash(payload)
        if event == "final_measurement_bound":
            payload["stored_champion"]["prefix_digest"] = payload["evidence"][0]["content_digest"]

    forge(run, edit)
    journal_consistent(run)
    result = audit(run)
    assert any(row["code"] == "baseline_import_mismatch" for row in result["mismatches"]), result
    # Exact-seed S1 imports require both dependency and effective-seed equality.
    # This corruption is refused before numerical pairing; do not misreport it
    # as a reachable full-point pairing finding (the pure producer covers that).
    assert any(
        row["event"] == "global_comparison_completed"
        and row["code"] == "precondition_failed"
        and "not a journal head" in row["detail"]
        for row in result["mismatches"]
    ), result
    assert not any("pairing is" in str(row["detail"]) for row in result["mismatches"])
