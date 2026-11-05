"""Explicit family-15 documents and narrowly scoped internal writer admission."""

from __future__ import annotations

import copy
from typing import Any

from tests.tree.manifest.test_manifest_v15 import v15_document


def v16_document(**overrides: Any) -> dict[str, Any]:
    """Build a complete /16 recommendation; overrides replace document sections."""
    document = v15_document(repeats=1, refresh=0)
    document["schema"] = "evolution-v2-manifest/16"
    document["campaign"]["sample_retries"] = 1
    for threshold in document["campaign"]["indeterminate"].values():
        threshold["max_rate"] = "0.500000000000"
    document["campaign"]["gate"] = {
        "mode": "point",
        "stability": "stable",
        "focus": {"margin": 0},
        "causal": {"tolerance": 0},
        "execution_noise": {"kind": "negligible"},
        "verification": "reuse",
        "provenance": {"kind": "reported"},
        "fresh_parent": False,
        "global": "point",
    }
    document.update(copy.deepcopy(overrides))
    return document


def family15_admitted(patch):
    """Mark the /16 probe; the product already admits both current families."""
    from tests.campaign_fakes import family14_admitted

    family14_admitted(patch)


def family15_created(policy):
    """Real creation identity, retaining the existing train/connection bindings."""
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json
    from tests.campaign_drivers import native14_created

    result = native14_created(round_stop="all_attempts")
    result["family_version"] = 15
    result["diagnostic_import_format"] = "baseline-diagnostic-bundle/1"
    result["identity"].update(policy=policy.to_payload(), policy_digest=policy.digest)
    result["identity_digest"] = hashlib.sha256(canonical_json(result["identity"])).hexdigest()
    return result


def reference15(kind="journal_record", measurement_id=None):
    """Shape-only canonical reference; no claim that these bytes exist."""
    locator = (
        f"measurements/{measurement_id}/events.jsonl#bytes=0:100"
        if measurement_id
        else "artifacts.jsonl#bytes=0:100"
        if kind == "ledger_record"
        else "campaign.jsonl#bytes=0:100"
    )
    return dict(version=1, kind=kind, run_id="run", locator=locator, content_digest="a" * 64)


def publication15(purpose="comparison"):
    from evolution.tree.artifacts.artifact_payloads import (
        AuxiliaryDeclaration,
        AuxiliaryFile,
        AuxiliarySourceReference,
    )
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication

    names = ("payload.json",) if purpose == "comparison" else ("index.json", "payload", "gaps.json")
    declaration = AuxiliaryDeclaration(
        15,
        purpose,
        "comparison-data/1" if purpose == "comparison" else "baseline-diagnostic-bundle/1",
        "a" * 64,
        tuple(AuxiliaryFile(name, 100, "a" * 64) for name in names),
        AuxiliarySourceReference.from_payload(reference15()),
    )
    return AuxiliaryPublication(
        declaration, AuxiliarySourceReference.from_payload(reference15("ledger_record"))
    ).to_payload()


def measurement15(name="candidate", *, settled=False):
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.tree.execution.test_journal_identity import payload

    identity = payload()
    identity["candidate_digest"] = ("b" if name == "candidate" else "c") * 64
    return dict(
        measurement_id=name,
        identity=identity,
        identity_digest=RunIdentityV1.from_payload(identity).digest,
        prefix=reference15("journal_prefix", name) if settled else None,
    )


class Family15Disk:
    """Real sibling journals plus an admitted family15 prefix and S2 publications.

    No lifecycle fields are patched. The historical fixture supplies actual
    offline execution/scoring bytes; only its campaign grammar is transcribed.
    """

    def __init__(self, root, records, **settings):
        self.root = root
        self.donor_attestations = [
            r["payload"] for r in records if r["event"] == "instance_attested"
        ]
        self.life = Family15Life(records, disk_evidence=True, **settings)
        self.write()

    def write(self):
        (self.root / "campaign.jsonl").write_bytes(
            b"".join(e.encode() for e in self.life.envelopes)
        )

    def rows(self, mid):
        from evolution.tree.campaign_statistics import latest_rows
        from evolution.tree.execution.journal import inspect_journal
        from evolution.tree.execution.journal_schema import Lifecycle

        journal = inspect_journal(self.root / "measurements" / mid)
        state = Lifecycle()
        for record in journal.records:
            state.accept(record)
        return latest_rows(journal.records, state)

    def attest_donor(self, mid):
        known = {
            e.payload["instance_id"] for e in self.life.envelopes if e.event == "instance_attested"
        }
        for payload in self.donor_attestations:
            if payload["measurement_id"] == mid and payload["instance_id"] not in known:
                self.life.accept("instance_attested", **payload)
        self.write()

    def measurement(self, mid, *, settled=False):
        import hashlib
        import json

        raw = (self.root / "measurements" / mid / "events.jsonl").read_bytes()
        created = json.loads(raw.splitlines()[0])
        return dict(
            measurement_id=mid,
            identity=created["payload"]["identity"],
            identity_digest=created["payload"]["identity_digest"],
            prefix=dict(
                version=1,
                kind="journal_prefix",
                run_id=created["run_id"],
                locator=f"measurements/{mid}/events.jsonl#bytes=0:{len(raw)}",
                content_digest=hashlib.sha256(raw).hexdigest(),
            )
            if settled
            else None,
        )

    def publish(self, body, *, diagnostic_files=None):
        import json
        import tempfile
        from pathlib import Path

        from evolution.tree.artifacts.artifact_payloads import (
            AuxiliaryDeclaration,
            describe_auxiliary_files,
        )
        from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
        from evolution.tree.artifacts.artifacts import ArtifactStore
        from evolution.tree.execution.journal_identity import canonical_json

        ledger = json.loads((self.root / "artifacts.jsonl").read_bytes().splitlines()[0])
        policy = ArtifactPolicyV1.from_payload(ledger["payload"]["identity"]["policy"])
        with tempfile.TemporaryDirectory() as directory:
            prepared = Path(directory)
            purpose = "comparison" if diagnostic_files is None else "baseline_diagnostics"
            contents = (
                {"payload.json": canonical_json(body)}
                if diagnostic_files is None
                else diagnostic_files
            )
            for name, raw in contents.items():
                (prepared / name).write_bytes(raw)
            files = describe_auxiliary_files(prepared, purpose=purpose)
            declaration = dict(
                family_version=15,
                purpose=purpose,
                format="comparison-data/1"
                if diagnostic_files is None
                else "baseline-diagnostic-bundle/1",
                content_digest=next(
                    f.digest
                    for f in files
                    if f.name == ("payload.json" if diagnostic_files is None else "index.json")
                ),
                files=[f.to_payload() for f in files],
            )
            seq = self.life.accept("auxiliary_payload_declared", **declaration)
            self.write()
            with ArtifactStore.resume(self.root, policy) as store:
                return store.publish_auxiliary(
                    AuxiliaryDeclaration.from_payload(
                        {**declaration, "declaration_ref": self.life.reference(seq)}
                    ),
                    prepared,
                ).to_payload()

    def bind_gate(self):
        from tests.test_tree_campaign_family15_schema import source_summary

        life = self.life
        attempt = life.attempt(life.sealed)
        parent = (
            attempt.parent_measurement_id
            if life.state.policy.fresh_parent
            else life.snapshot.rounds[1].baseline_measurement_id
        )
        candidate = self.measurement(attempt.measurement_id)
        reference = self.measurement(parent, settled=not life.state.policy.fresh_parent)
        from evolution.tree.campaign_statistics import latest_rows
        from evolution.tree.case_comparison import build_profile
        from evolution.tree.execution.journal import inspect_journal
        from evolution.tree.execution.journal_schema import Lifecycle

        journal = inspect_journal(self.root / "measurements" / parent)
        state = Lifecycle()
        for entry in journal.records:
            state.accept(entry)
        body = build_profile(
            policy=life.state.policy,
            corpus=life.snapshot.identity["corpus_cases"],
            scopes={"focus": ["c1"], "causal": ["c2"]},
            candidate=candidate,
            reference=reference,
            reference_rows=latest_rows(journal.records, state),
            epoch=0,
            reference_kind="fresh" if life.state.policy.fresh_parent else "stored",
        ).to_payload()
        data = self.publish(body)
        seq = life.accept(
            "comparison_bound",
            round=1,
            attempt_id=life.sealed,
            epoch=0,
            data=data,
            candidate=source_summary(candidate),
            reference=source_summary(reference),
            reference_kind=body["reference_kind"],
        )
        self.write()
        return seq, body

    def settle_gate(self, bound, *, side="candidate"):
        from tests.test_tree_campaign_family15_schema import source_summary

        life = self.life
        p = life.envelopes[bound - 1].payload
        mid = p["candidate" if side == "candidate" else "reference"]["measurement_id"]
        m = self.measurement(mid, settled=True)
        life.accept(
            "stage_opened",
            measurement_id=mid,
            stage=1,
            look=1,
            sample_count=len(m["identity"]["schedule"]),
        )
        self.attest_donor(mid)
        seq = life.accept(
            "gate_measurement_completed",
            round=1,
            attempt_id=life.sealed,
            binding=life.reference(bound),
            side=side,
            source=source_summary(m),
            required=len(m["identity"]["schedule"]),
            admissible=len(m["identity"]["schedule"]),
        )
        self.write()
        return seq

    def finish_gate(self):
        import hashlib

        from evolution.tree.execution.journal_identity import canonical_json
        from tests.test_tree_campaign_family15_schema import source_summary

        bound, binding = self.bind_gate()
        self.settle_gate(bound)
        life = self.life
        if life.state.policy.fresh_parent:
            self.settle_gate(bound, side="parent")
        candidate = self.measurement(binding["candidate"]["measurement_id"], settled=True)
        reference = self.measurement(binding["reference"]["measurement_id"], settled=True)
        from evolution.tree.campaign_statistics import latest_rows
        from evolution.tree.case_comparison import ComparisonProfile, evaluate_comparison
        from evolution.tree.execution.journal import inspect_journal
        from evolution.tree.execution.journal_schema import Lifecycle

        def rows(mid):
            journal = inspect_journal(self.root / "measurements" / mid)
            state = Lifecycle()
            for entry in journal.records:
                state.accept(entry)
            return latest_rows(journal.records, state)

        profile = ComparisonProfile.from_payload(
            binding,
            primary=life.state.policy.metric_primary,
            groups={c["case_id"]: c["group_id"] for c in life.snapshot.identity["corpus_cases"]},
        )
        body = evaluate_comparison(
            profile,
            rows(candidate["measurement_id"]),
            rows(reference["measurement_id"]),
            candidate_source=source_summary(candidate),
            reference_source=source_summary(reference),
        ).to_payload()
        data = self.publish(body)
        result = life.accept(
            "comparison_completed",
            round=1,
            attempt_id=life.sealed,
            binding=life.reference(bound),
            data=data,
            **{
                k: body[k]
                for k in ("outcome", "reason", "deciding_scope", "candidate", "reference")
            },
        )
        life.close_gate(result)
        digest = hashlib.sha256(canonical_json(candidate["identity"]["schedule"])).hexdigest()
        data = self.publish(
            dict(
                format="comparison-data/1",
                kind="gate_seal",
                measurement=candidate,
                slots_digest=digest,
            )
        )
        seal = life.accept(
            "candidate_gate_sealed",
            round=1,
            attempt_id=life.sealed,
            data=data,
            source=source_summary(candidate),
            slots_digest=digest,
        )
        self.write()
        return seal

    def bind_completion(self):
        from evolution.tree.baseline_import import admit_rows, read_frozen_source
        from evolution.tree.execution.journal_identity import RunIdentityV1

        life = self.life
        self.finish_gate()
        life.bind_finalists()
        candidate = self.measurement(life.attempt(life.sealed).measurement_id, settled=True)
        target = RunIdentityV1.from_payload(candidate["identity"])
        source = read_frozen_source(
            self.root,
            life.run_id,
            candidate["measurement_id"],
            expected_identity_digest=target.digest,
        )
        imported = admit_rows(
            source,
            target,
            primary=life.state.policy.metric_primary,
            condition_changed=(),
            primary_range=True,
        )
        destination = dict(candidate, measurement_id="completion", prefix=None)
        body = dict(
            format="comparison-data/1",
            kind="completion",
            destination=destination,
            source=candidate,
            imports=imported.declaration(),
            rows=list(imported.rows),
            missing_slots=[],
            finalist_closure=life.reference(life.sequence_of("attempt_closed")),
            gate_seal=life.reference(life.sequence_of("candidate_gate_sealed")),
        )
        data = self.publish(body)
        bound = life.accept(
            "candidate_completion_bound",
            round=1,
            attempt_id=life.sealed,
            data=data,
            finalist_closure=body["finalist_closure"],
            gate_seal=body["gate_seal"],
            measurement_id="completion",
            identity_digest=target.digest,
            pairs_digest=imported.pairs_digest,
            missing_count=0,
            required=2,
            imported=2,
        )
        self.write()
        return bound, body

    def finish_completion(self, bound, body):
        from evolution import __version__
        from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, Journal
        from tests.test_tree_campaign_family15_schema import source_summary

        destination = body["destination"]
        created = dict(
            identity=destination["identity"],
            identity_digest=destination["identity_digest"],
            producing_version=__version__,
        )
        raw = Journal._birth_bytes(
            "completion",
            created,
            body["rows"],
            secrets=(),
            max_record_bytes=MAX_RECORD_BYTES,
            max_journal_bytes=MAX_JOURNAL_BYTES,
        )
        path = self.root / "measurements" / "completion"
        path.mkdir()
        (path / "events.jsonl").write_bytes(raw)
        seq = self.life.accept(
            "candidate_completion_completed",
            round=1,
            attempt_id=self.life.sealed,
            binding=self.life.reference(bound),
            source=source_summary(self.measurement("completion", settled=True)),
            required=2,
            admissible=2,
        )
        self.write()
        return seq

    def finish_final(self, *, before_completions=False, prebirth=False):
        from tests.test_tree_campaign_family15_schema import source_summary

        life = self.life
        fresh = life.state.policy.verification == "fresh"
        champion_bound = None
        if fresh:
            self.finish_gate()
            life.bind_finalists()
            if prebirth:
                from evolution.tree.campaign_identity import (
                    CampaignIdentityV1,
                    derived_identity,
                    fresh_final_identity,
                )

                def planned(mid, digest, side):
                    assert not (self.root / "measurements" / mid).exists()
                    identity = derived_identity(
                        CampaignIdentityV1.from_payload(life.snapshot.identity), digest, side
                    )
                    identity = fresh_final_identity(identity, mid, life.state.policy.repeats)
                    return dict(
                        measurement_id=mid,
                        identity=identity.to_payload(),
                        identity_digest=identity.digest,
                        prefix=None,
                    )

                candidate = planned(
                    "m-r01-final-01-a01", life.attempt(life.sealed).candidate_digest, "candidate"
                )
                champion = planned("m-r01-final-01-champion", life.snapshot.champion[1], "champion")
            else:
                candidate = self.measurement("m-r01-final-01-a01")
                champion = self.measurement("m-r01-final-01-champion")
            champion_bound = life.accept(
                "champion_final_bound",
                round=1,
                attempt_id=None,
                finalists=life.reference(life.sequence_of("finalists_bound")),
                measurement_id=champion["measurement_id"],
                identity_digest=champion["identity_digest"],
                candidate_digest=champion["identity"]["candidate_digest"],
            )
        else:
            bound, completion = self.bind_completion()
            self.finish_completion(bound, completion)
            candidate = self.measurement("completion", settled=True)
            champion = self.measurement(
                life.snapshot.rounds[1].baseline_measurement_id, settled=True
            )
        from evolution.tree.case_comparison import build_profile

        comparison = build_profile(
            policy=life.state.policy,
            corpus=life.snapshot.identity["corpus_cases"],
            scopes={"global": ["c1", "c2"]},
            candidate=candidate,
            reference=champion,
            reference_rows=() if fresh else self.rows(champion["measurement_id"]),
            epoch=0,
            reference_kind="fresh" if fresh else "stored",
        ).to_payload()
        body = dict(
            format="comparison-data/1",
            kind="final",
            verification="fresh" if fresh else "reuse",
            candidate_source="fresh" if fresh else "completion",
            champion_side="fresh" if fresh else "stored",
            candidate=candidate,
            champion=champion,
            comparison=comparison,
            finalist_closure=life.reference(life.sequence_of("attempt_closed")),
            source_ref=life.reference(
                life.sequence_of(
                    "candidate_gate_sealed" if fresh else "candidate_completion_completed"
                )
            ),
            candidate_work=candidate["identity"]["schedule"] if fresh else [],
            schedule_order=[
                dict(measurement_id=m["measurement_id"], slot=slot)
                for slot_pair in zip(
                    candidate["identity"]["schedule"], champion["identity"]["schedule"], strict=True
                )
                for m, slot in zip((candidate, champion), slot_pair, strict=True)
            ]
            if fresh
            else [],
        )
        data = self.publish(body)
        final = life.accept(
            "final_measurement_bound",
            round=1,
            attempt_id=life.sealed,
            data=data,
            **{
                k: body[k]
                for k in (
                    "verification",
                    "candidate_source",
                    "champion_side",
                    "finalist_closure",
                    "source_ref",
                )
            },
            candidate=source_summary(candidate),
            champion=source_summary(champion),
        )
        if before_completions:
            self.write()
            return final, champion_bound, body
        if fresh:
            for side, m, own_binding, attempt in (
                ("candidate", candidate, final, life.sealed),
                ("champion", champion, champion_bound, None),
            ):
                life.accept(
                    "stage_opened",
                    measurement_id=m["measurement_id"],
                    stage=1,
                    look=1,
                    sample_count=2,
                )
                self.attest_donor(m["measurement_id"])
                life.accept(
                    "final_measurement_completed",
                    round=1,
                    attempt_id=attempt,
                    binding=life.reference(own_binding),
                    side=side,
                    source=source_summary(self.measurement(m["measurement_id"], settled=True)),
                    required=2,
                    admissible=2,
                )
            candidate = self.measurement(candidate["measurement_id"], settled=True)
            champion = self.measurement(champion["measurement_id"], settled=True)
        return self.finish_global(final, comparison, candidate, champion)

    def finish_global(self, final, comparison, candidate, champion):
        from evolution.tree.case_comparison import ComparisonProfile, evaluate_comparison
        from tests.test_tree_campaign_family15_schema import q, source_summary

        life = self.life
        profile = ComparisonProfile.from_payload(
            comparison,
            primary=life.state.policy.metric_primary,
            groups={c["case_id"]: c["group_id"] for c in life.snapshot.identity["corpus_cases"]},
        )
        result = evaluate_comparison(
            profile,
            self.rows(candidate["measurement_id"]),
            self.rows(champion["measurement_id"]),
            candidate_source=source_summary(candidate),
            reference_source=source_summary(champion),
            schedules={
                "candidate": candidate["identity"]["schedule"],
                "reference": champion["identity"]["schedule"],
            },
        ).to_payload()
        data = self.publish(result)
        global_result = life.accept(
            "global_comparison_completed",
            round=1,
            attempt_id=life.sealed,
            binding=life.reference(final),
            data=data,
            accepted=True,
            **{
                k: result[k]
                for k in ("candidate", "reference", "outcome", "reason", "deciding_scope")
            },
        )
        ranking = dict(
            format="comparison-data/1",
            kind="ranking",
            round_stop=life.state.policy.round_stop,
            entries=[
                dict(
                    attempt_id=life.sealed,
                    attempt_index=1,
                    candidate_digest=candidate["identity"]["candidate_digest"],
                    result=life.reference(global_result),
                    evidence=source_summary(candidate),
                    accepted=True,
                    point=q(1),
                )
            ],
            proposed_attempt=life.sealed,
        )
        data = self.publish(ranking)
        selected = life.accept(
            "selection_decided",
            round=1,
            data=data,
            reason="automatic",
            selected_candidate=candidate["identity"]["candidate_digest"],
            selected_evidence=source_summary(candidate),
            next_champion=source_summary(candidate),
        )
        self.write()
        return selected

    def fresh_carry(self, side, *, hops=1, born=True):
        from evolution import __version__
        from evolution.tree.baseline_import import admit_rows, read_frozen_source
        from evolution.tree.campaign_identity import (
            CampaignIdentityV1,
            derived_identity,
            fresh_final_identity,
        )
        from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, Journal
        from tests.test_tree_campaign_family15_schema import source_summary

        final, champion_bound, body = self.finish_final(before_completions=True)
        life = self.life
        own = body["candidate" if side == "candidate" else "champion"]["measurement_id"]
        sibling = body["champion" if side == "candidate" else "candidate"]["measurement_id"]
        sibling_side = "champion" if side == "candidate" else "candidate"
        for mid in (own, sibling):
            life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
            self.attest_donor(mid)
        sibling_complete = life.accept(
            "final_measurement_completed",
            round=1,
            side=sibling_side,
            attempt_id=life.sealed if sibling_side == "candidate" else None,
            binding=life.reference(final if sibling_side == "candidate" else champion_bound),
            source=source_summary(self.measurement(sibling, settled=True)),
            required=2,
            admissible=2,
        )
        source_mid = own
        for _ in range(hops):
            entry = life.snapshot.measurements[source_mid]
            changed = copy.deepcopy(dict(life.snapshot.identity))
            changed["target"]["settings_hash"] = f"settings-{len(life.snapshot.epochs) + 1}"
            target = derived_identity(
                CampaignIdentityV1.from_payload(changed),
                entry["candidate_digest"],
                "candidate" if side == "candidate" else "champion",
            )
            target = fresh_final_identity(target, source_mid, life.state.policy.repeats)
            source = read_frozen_source(
                self.root,
                life.run_id,
                source_mid,
                expected_identity_digest=entry["identity_digest"],
            )
            imported = admit_rows(
                source,
                target,
                primary=life.state.policy.metric_primary,
                condition_changed=["target"],
                primary_range=True,
            )
            moved = life.continue_block(
                [source_mid],
                identity_digest=target.digest,
                imported=imported.imported,
                pairs_digest=imported.pairs_digest,
            )
            destination = moved[0][1]
            if born:
                raw = Journal._birth_bytes(
                    destination,
                    dict(
                        identity=target.to_payload(),
                        identity_digest=target.digest,
                        producing_version=__version__,
                    ),
                    imported.rows,
                    secrets=(),
                    max_record_bytes=MAX_RECORD_BYTES,
                    max_journal_bytes=MAX_JOURNAL_BYTES,
                )
                path = self.root / "measurements" / destination
                path.mkdir()
                (path / "events.jsonl").write_bytes(raw)
            source_mid = destination
        self.write()
        return dict(
            root=own,
            destination=destination,
            sibling=sibling_complete,
            binding=final if side == "candidate" else champion_bound,
            body=body,
            rows=imported.rows,
            source=source.measurement_id,
        )

    def fresh_connection(self):
        """Run the real offline probe when fresh execution crosses an epoch."""
        import asyncio

        from evolution.tree.campaign_identity import (
            CampaignIdentityV1,
            derived_identity,
        )
        from evolution.tree.execution.interaction import RolloutBudget
        from evolution.tree.execution.journal_rollout import ConnectionAdmission
        from evolution.tree.target.connection_probe import (
            binding_digest,
            epoch_binding,
            probe_identity,
            probe_seed,
            run_probe,
        )

        life, execution = self.life, self.execution
        epoch = len(life.snapshot.epochs) - 1
        if execution["connection_epoch"] == epoch:
            return
        execution["adapter"].settings = life.snapshot.identity["target"]["settings_hash"]
        preflight = asyncio.run(
            execution["adapter"].preflight(execution["champion"].expected_attestation())
        )
        execution["expected"] = preflight.semantic
        fields = epoch_binding(epoch, life.snapshot.identity)
        digest = binding_digest(**fields)
        seed = probe_seed(digest, life.snapshot.champion[1])
        operation = f"probe-fresh-epoch-{epoch}"
        invocation = f"fresh-epoch-{epoch}"
        life.accept(
            "connection_preflight_started",
            op=operation,
            operation_id=operation,
            invocation_id=invocation,
            binding_digest=digest,
            **{key: value for key, value in fields.items() if key != "corpus_digest"},
            candidate_digest=life.snapshot.champion[1],
            dependency_digest=life.snapshot.champion_dependency_digest,
            probe_seed=seed,
        )
        self.write()
        identity = probe_identity(
            derived_identity(
                CampaignIdentityV1.from_payload(life.snapshot.identity),
                life.snapshot.champion[1],
                "champion",
            ),
            execution["corpus"],
            seed,
        )
        budget = life.state.policy.budget
        result = asyncio.run(
            run_probe(
                run_root=self.root,
                operation_id=operation,
                campaign_run_id=life.run_id,
                identity=identity,
                corpus=execution["corpus"],
                secrets=(),
                **{
                    key: execution[key]
                    for key in (
                        "adapter",
                        "strategy",
                        "environment",
                        "scorer",
                        "expected",
                        "call_attempts",
                    )
                },
                candidate=execution["champion"],
                contract=execution["metric_contract"],
                primary_metric=life.state.policy.metric_primary,
                budget=RolloutBudget(
                    budget.max_turns,
                    budget.timeout_seconds,
                    max_failed_turns=budget.max_failed_turns,
                    call_retries=budget.call_retries,
                    cancelled=lambda: False,
                ),
            )
        )
        event, payload = result.terminal()
        assert event == "connection_preflight_finished"
        life.accept(event, op=operation, **payload)
        execution["connection"] = ConnectionAdmission(digest, operation, invocation)
        execution["connection_epoch"] = epoch
        self.write()

    def execute_fresh(self, mid, side, *, create_only=False):
        """Execute actual offline S1 work under the admitted measurement identity."""
        import asyncio

        from evolution.tree.campaign_measurement import Measurement, _partition
        from evolution.tree.campaign_projections import _derive_bound_identity, _evidence_of

        self.fresh_connection()
        self.write()
        context, _ = _evidence_of(self.root)
        identity = _derive_bound_identity(context, mid)
        execution = self.execution
        measurement = Measurement.declare(
            run_root=self.root,
            campaign_run_id=self.life.run_id,
            measurement_id=mid,
            identity=identity,
            corpus=execution["corpus"],
        )
        measurement.create()
        try:
            plan = _partition(self.life.state.policy.looks, identity.to_payload()["schedule"])[0]
            self.life.accept(
                "stage_opened", measurement_id=mid, stage=1, look=1, sample_count=len(plan.rows)
            )
            self.write()
            if not create_only:
                outcome = asyncio.run(
                    measurement.run_stage(
                        plan,
                        **{
                            key: execution[key]
                            for key in (
                                "adapter",
                                "environment",
                                "scorer",
                                "strategy",
                                "expected",
                                "connection",
                            )
                        },
                        candidate=execution[side],
                        policy=self.life.state.policy,
                        cancelled=lambda: False,
                    )
                )
                assert outcome.terminals == len(plan.rows)
                prefix = self.measurement(mid, settled=True)["prefix"]
                for instance_id, identity in outcome.attested.items():
                    payload = dict(
                        measurement_id=mid,
                        instance_id=instance_id,
                        loaded_candidate_digest=identity.instance.loaded_candidate_digest,
                        dependency_digest=identity.semantic.dependency_digest,
                        boot_id=identity.instance.boot_id,
                        identity=identity.to_payload(),
                        evidence=[prefix],
                    )
                    if outcome.attested_components_digest.get(instance_id) is not None:
                        payload["components_digest"] = outcome.attested_components_digest[
                            instance_id
                        ]
                    if outcome.attested_description.get(instance_id) is not None:
                        payload["description"] = dict(outcome.attested_description[instance_id])
                    self.life.accept("instance_attested", **payload)
                self.write()

        finally:
            measurement.close()

    def prebirth_carry(self, side, *, hops=1, born_source=False, source_layout="absent"):
        """Declare before birth, carry zero, then execute the new epoch locally."""
        from evolution.tree.baseline_import import import_pairs_digest
        from evolution.tree.campaign_identity import (
            CampaignIdentityV1,
            derived_identity,
            fresh_final_identity,
        )
        from tests.test_tree_campaign_family15_schema import source_summary

        final, champion_bound, body = self.finish_final(before_completions=True, prebirth=True)
        life = self.life
        own = body["candidate" if side == "candidate" else "champion"]["measurement_id"]
        sibling_side = "champion" if side == "candidate" else "candidate"
        sibling = body[sibling_side]["measurement_id"]
        self.execute_fresh(sibling, sibling_side)
        sibling_complete = life.accept(
            "final_measurement_completed",
            round=1,
            side=sibling_side,
            attempt_id=life.sealed if sibling_side == "candidate" else None,
            binding=life.reference(final if sibling_side == "candidate" else champion_bound),
            source=source_summary(self.measurement(sibling, settled=True)),
            required=2,
            admissible=2,
        )
        if born_source:
            self.execute_fresh(own, side, create_only=True)
        source = own
        for hop in range(hops):
            if source_layout == "empty" and not (born_source and hop == 0):
                directory = self.root / "measurements" / source
                directory.mkdir()
                (directory / "events.jsonl").write_bytes(b"")
            changed = copy.deepcopy(dict(life.snapshot.identity))
            changed["target"]["settings_hash"] = f"settings-{len(life.snapshot.epochs) + 1}"
            identity = derived_identity(
                CampaignIdentityV1.from_payload(changed),
                life.snapshot.measurements[source]["candidate_digest"],
                side,
            )
            identity = fresh_final_identity(identity, source, life.state.policy.repeats)
            source = life.continue_block(
                [source],
                identity_digest=identity.digest,
                imported=0,
                pairs_digest=import_pairs_digest(()),
                released=2 if born_source and hop == 0 else 0,
            )[0][1]
        self.execute_fresh(source, side)
        completion = life.accept(
            "final_measurement_completed",
            round=1,
            side=side,
            attempt_id=life.sealed if side == "candidate" else None,
            binding=life.reference(final if side == "candidate" else champion_bound),
            source=source_summary(self.measurement(source, settled=True)),
            required=2,
            admissible=2,
        )
        self.write()
        candidate = source if side == "candidate" else sibling
        champion = source if side == "champion" else sibling
        self.finish_global(
            final,
            body["comparison"],
            self.measurement(candidate, settled=True),
            self.measurement(champion, settled=True),
        )
        return dict(source=own, destination=source, completion=completion, sibling=sibling_complete)


class Family15Life:
    """An explicit /16 prefix admitted record by record with real local references.

    Auxiliary publication summaries model L's asserted E boundary only; these
    fixtures do not claim that artifact or measurement bytes exist on disk.
    """

    def __init__(self, records, *, early=False, disk_evidence=False, **settings):
        import hashlib

        from evolution.tree.campaign_policy import CampaignPolicyV12
        from evolution.tree.execution.journal_identity import canonical_json
        from tests.campaign_fakes import LifecycleFixture

        rows = copy.deepcopy(records)
        assert rows[0]["payload"]["family_version"] == 14
        created = copy.deepcopy(rows[0]["payload"])
        policy = created["identity"]["policy"]
        document = v16_document()
        policy["gate"] = document["campaign"]["gate"]
        policy["baseline_refresh_after_rounds"] = 0
        policy["round_stop"] = settings.pop("round_stop", "all_attempts")
        policy["gate"]["stability"] = settings.pop("stability", "stable")
        policy["gate"]["verification"] = settings.pop("verification", "reuse")
        policy["gate"]["fresh_parent"] = settings.pop("fresh_parent", False)
        for key, value in settings.items():
            policy[key] = value
        parsed = CampaignPolicyV12.from_payload(policy)
        created["identity"].update(policy=parsed.to_payload(), policy_digest=parsed.digest)
        created.update(family_version=15, diagnostic_import_format="baseline-diagnostic-bundle/1")
        created["identity_digest"] = hashlib.sha256(canonical_json(created["identity"])).hexdigest()
        rows[0]["payload"] = created
        kept = []
        for row in rows:
            if row["event"] == ("grouping_started" if early else "comparison_bound"):
                break
            if row["event"] == "attempt_opened":
                row["payload"]["path"] = "candidate"
                row["operation_id"] = None
            if row["event"] == "grouping_started":
                row["payload"]["grouper_kind"] = created["adaptation_payload"]["grouping"]["kind"]
            if row["event"] != "parent_measurement_bound" or parsed.fresh_parent:
                kept.append(row)
        for sequence, row in enumerate(kept, 1):
            row["sequence"] = sequence
        if disk_evidence:
            # Re-address existing local references after the creation row changes
            # size. Every target is an actual prior envelope, never invented L state.
            from evolution.tree.family15_schema import FAMILY15_CONTRACT
            from evolution.tree.storage.envelope import Envelope

            originals = {
                hashlib.sha256(canonical_json(row) + b"\n").hexdigest(): row for row in records
            }
            replacements = {}
            offset = 0
            for row in kept:

                def rewrite(value):
                    if isinstance(value, dict):
                        if (
                            value.get("kind") == "journal_record"
                            and value.get("content_digest") in replacements
                        ):
                            return replacements[value["content_digest"]]
                        return {k: rewrite(v) for k, v in value.items()}
                    if isinstance(value, list):
                        return [rewrite(v) for v in value]
                    return value

                row["payload"] = rewrite(row["payload"])
                encoded = Envelope.from_payload(row, family=FAMILY15_CONTRACT).encode()
                for digest, original in originals.items():
                    if original["event"] == row["event"] and original["payload"] == row["payload"]:
                        replacements[digest] = dict(
                            version=1,
                            kind="journal_record",
                            run_id=row["run_id"],
                            locator=f"campaign.jsonl#bytes={offset}:{offset + len(encoded)}",
                            content_digest=hashlib.sha256(encoded).hexdigest(),
                        )
                offset += len(encoded)
        LifecycleFixture._initialize(self, kept, 15)

    from tests.campaign_fakes import LifecycleFixture as _Base

    _initialize = _Base._initialize
    snapshot = _Base.snapshot
    sealed = _Base.sealed
    attempt = _Base.attempt
    _envelope = _Base._envelope
    accept = _Base.accept
    reference = _Base.reference
    sequence_of = _Base.sequence_of

    def data(self):
        from evolution.tree.artifacts.artifact_payloads import (
            AuxiliaryDeclaration,
            AuxiliarySourceReference,
        )
        from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication

        p = publication15()
        declaration = p["declaration"]
        import hashlib

        digest = hashlib.sha256(str(len(self.envelopes)).encode()).hexdigest()
        declaration["content_digest"] = digest
        declaration["files"][0]["digest"] = digest
        seq = self.accept(
            "auxiliary_payload_declared",
            **{k: v for k, v in declaration.items() if k != "declaration_ref"},
        )
        declaration["declaration_ref"] = self.reference(seq)
        p["publication_ref"]["run_id"] = self.run_id
        return AuxiliaryPublication(
            AuxiliaryDeclaration.from_payload(declaration),
            AuxiliarySourceReference.from_payload(p["publication_ref"]),
        ).to_payload()

    @classmethod
    def focus_first(cls, records, **settings):
        gate = copy.deepcopy(settings.pop("gate", v16_document()["campaign"]["gate"]))
        gate.update(stages="focus_first", fresh_parent=settings.get("fresh_parent", False))
        return cls(records, gate=gate, **settings)

    def decide_focus(self, outcome="passed"):
        a = self.attempt(self.sealed)
        return self.accept(
            "focus_decided",
            round=a.round,
            attempt_id=a.attempt_id,
            binding=a.family15.comparison_binding.reference.to_payload(),
            outcome=outcome,
            reason="passed" if outcome == "passed" else "threshold_not_passed",
            data=self.data(),
        )

    def early_attempt(self, index=1):
        return self.accept(
            "attempt_opened",
            round=1,
            attempt_index=index,
            attempt_id=f"r1-a{index}",
            parent_version=self.snapshot.champion[0],
            parent_digest=self.snapshot.champion[1],
            path="early_global",
        )

    def preflight_payload(self):
        return dict(
            round=1,
            attempt_id="r1-a1",
            epoch=0,
            phase="early_global",
            mode="point",
            data=self.data(),
            reason="structurally_unattainable",
        )

    def source(self, mid, settled=True):
        entry = self.snapshot.measurements[mid]
        prefix = reference15("journal_prefix", mid) if settled else None
        if prefix:
            prefix["run_id"] = mid
            r = self.snapshot.rounds[entry["round"]]
            if r.baseline_measurement_id == mid and r.baseline_reference is not None:
                prefix = dict(r.baseline_reference)
        return dict(
            measurement_id=mid,
            identity_digest=entry["identity_digest"],
            candidate_digest=entry["candidate_digest"],
            prefix=prefix,
        )

    def bind_gate(self):
        a = self.attempt(self.sealed)
        baseline = self.snapshot.rounds[1].baseline_measurement_id
        fresh = self.state.policy.fresh_parent
        return self.accept(
            "comparison_bound",
            round=1,
            attempt_id=a.attempt_id,
            epoch=len(self.snapshot.epochs) - 1,
            data=self.data(),
            candidate=(
                a.family15.gate_candidate.payload["source"]
                if a.family15.gate_candidate is not None
                else self.source(a.measurement_id, False)
            ),
            reference=self.source(a.parent_measurement_id, False)
            if fresh
            else self.source(baseline),
            reference_kind="fresh" if fresh else "stored",
        )

    def gate_side_payload(self, bound, side="candidate"):
        binding = self.envelopes[bound - 1].payload
        mid = binding["candidate" if side == "candidate" else "reference"]["measurement_id"]
        return dict(
            round=1,
            attempt_id=self.sealed,
            binding=self.reference(bound),
            side=side,
            source=self.source(mid),
            required=2,
            admissible=2,
        )

    def settle_gate_sides(self, bound):
        a = self.attempt(self.sealed)
        for side, complete in (
            ("candidate", a.family15.gate_candidate),
            ("parent", a.family15.gate_parent),
        ):
            if complete is None and (side == "candidate" or self.state.policy.fresh_parent):
                self.accept("gate_measurement_completed", **self.gate_side_payload(bound, side))

    def gate_result_payload(self, bound):
        p = self.envelopes[bound - 1].payload
        return dict(
            round=1,
            attempt_id=self.sealed,
            binding=self.reference(bound),
            data=self.data(),
            outcome="passed",
            reason="passed",
            deciding_scope="causal",
            candidate=self.source(p["candidate"]["measurement_id"]),
            reference=self.source(p["reference"]["measurement_id"]),
        )

    def close_gate(self, result):
        return self.accept(
            "attempt_closed",
            attempt_id=self.sealed,
            outcome="finalist",
            reason="passed",
            path="candidate",
            evidence=self.reference(result),
        )

    def finish_gate(self):
        bound = self.bind_gate()
        a = self.attempt(self.sealed)
        self.accept(
            "stage_opened", measurement_id=a.measurement_id, stage=1, look=1, sample_count=2
        )
        if self.state.policy.fresh_parent:
            self.accept(
                "stage_opened",
                measurement_id=a.parent_measurement_id,
                stage=1,
                look=1,
                sample_count=2,
            )
        self.settle_gate_sides(bound)
        result = self.accept("comparison_completed", **self.gate_result_payload(bound))
        self.close_gate(result)
        self.accept(
            "candidate_gate_sealed",
            round=1,
            attempt_id=self.sealed,
            data=self.data(),
            source=self.source(a.measurement_id),
            slots_digest="d" * 64,
        )

    def bind_finalists(self):
        import hashlib

        from evolution.tree.execution.journal_identity import canonical_json

        return self.accept(
            "finalists_bound",
            round=1,
            count=1,
            attempts_digest=hashlib.sha256(canonical_json([self.sealed])).hexdigest(),
        )

    def bind_champion(self):
        return self.accept(
            "champion_final_bound",
            round=1,
            attempt_id=None if self.state.policy.round_stop == "all_attempts" else self.sealed,
            finalists=self.reference(self.sequence_of("finalists_bound"))
            if self.state.policy.round_stop == "all_attempts"
            else None,
            measurement_id="fresh-champion",
            identity_digest="c" * 64,
            candidate_digest=self.snapshot.champion[1],
        )

    def bind_completion(self):
        return self.accept(
            "candidate_completion_bound",
            round=1,
            attempt_id=self.sealed,
            data=self.data(),
            finalist_closure=self.reference(self.sequence_of("attempt_closed")),
            gate_seal=self.reference(self.sequence_of("candidate_gate_sealed")),
            measurement_id="completion",
            identity_digest="e" * 64,
            pairs_digest="f" * 64,
            missing_count=0,
            required=2,
            imported=2,
        )

    def finish_final(self, accepted=True):
        self.finish_gate()
        if self.state.policy.round_stop == "all_attempts":
            self.bind_finalists()
        champion_binding = None
        if self.state.policy.stability == "unstable":
            champion_binding = self.bind_champion()
        if self.state.policy.verification == "reuse":
            binding = self.bind_completion()
            source_ref = self.accept(
                "candidate_completion_completed",
                round=1,
                attempt_id=self.sealed,
                binding=self.reference(binding),
                source=self.source("completion"),
                required=2,
                admissible=2,
            )
            candidate = self.source("completion")
        else:
            source_ref = self.sequence_of("candidate_gate_sealed")
            candidate = dict(
                measurement_id="fresh-candidate",
                identity_digest="e" * 64,
                candidate_digest=self.attempt(self.sealed).candidate_digest,
                prefix=None,
            )
        champion = (
            self.source("fresh-champion", False)
            if champion_binding
            else self.source(self.snapshot.rounds[1].baseline_measurement_id)
        )
        final = self.accept(
            "final_measurement_bound",
            round=1,
            attempt_id=self.sealed,
            data=self.data(),
            verification=self.state.policy.verification,
            candidate_source="completion" if self.state.policy.verification == "reuse" else "fresh",
            champion_side="fresh" if champion_binding else "stored",
            finalist_closure=self.reference(self.sequence_of("attempt_closed")),
            source_ref=self.reference(source_ref),
            candidate=candidate,
            champion=champion,
        )
        if self.state.policy.verification == "fresh":
            self.accept(
                "stage_opened", measurement_id="fresh-candidate", stage=1, look=1, sample_count=2
            )
            self.accept(
                "final_measurement_completed",
                round=1,
                attempt_id=self.sealed,
                binding=self.reference(final),
                side="candidate",
                source=self.source("fresh-candidate"),
                required=2,
                admissible=2,
            )
            candidate = self.source("fresh-candidate")
        if champion_binding:
            self.accept(
                "stage_opened", measurement_id="fresh-champion", stage=1, look=1, sample_count=2
            )
            self.accept(
                "final_measurement_completed",
                round=1,
                attempt_id=None if self.state.policy.round_stop == "all_attempts" else self.sealed,
                binding=self.reference(champion_binding),
                side="champion",
                source=self.source("fresh-champion"),
                required=2,
                admissible=2,
            )
            champion = self.source("fresh-champion")
        self.accept(
            "global_comparison_completed",
            round=1,
            attempt_id=self.sealed,
            binding=self.reference(final),
            data=self.data(),
            outcome="passed" if accepted else "comparison_not_passed",
            reason="passed" if accepted else "threshold_not_passed",
            deciding_scope="global",
            accepted=accepted,
            candidate=candidate,
            reference=champion,
        )

    def select(self):
        result = self.attempt(self.sealed).family15.global_result.payload
        accepted = result["accepted"]
        return self.accept(
            "selection_decided",
            round=1,
            data=self.data(),
            reason="automatic" if accepted else "no_eligible_candidate",
            selected_candidate=result["candidate"]["candidate_digest"] if accepted else None,
            selected_evidence=result["candidate"] if accepted else None,
            next_champion=result["candidate"] if accepted else result["reference"],
        )

    continue_block = _Base.continue_block
    rebound_payload = _Base.rebound_payload

    def close_round(self, *, closure_ref=None, outcome=None):
        r = self.snapshot.rounds[1]
        selected = r.selection
        promote = selected is not None and selected["attempt_id"] is not None
        result = self.attempt(self.sealed).family15.comparison
        coverage = dict(
            selected_cohort_visits=r.focus_portion["cohort_ids"],
            cycle=r.focus_portion["cycle"],
            claimed_cases=1,
            examined_cases=len(r.gate_scope["cases"]),
            measured_cases=2,
            had_assessed_candidate=True,
            closure_refs=[closure_ref or result.reference.to_payload()],
            omitted_closure_refs=0,
        )
        evidence = [reference15("ledger_record")] if promote else None
        if evidence:
            evidence[0]["run_id"] = self.run_id
        return self.accept(
            "round_closed",
            round=1,
            outcome=outcome or ("promoted" if promote else "retained"),
            reason=None,
            champion_version="v1" if promote else self.snapshot.champion[0],
            champion_digest=selected["candidate_digest"] if promote else self.snapshot.champion[1],
            attempts_opened=len(r.attempts),
            coverage=coverage,
            evidence=evidence,
        )

    def completion_payload(self):
        return dict(
            round=1,
            attempt_id=self.sealed,
            data=self.data(),
            finalist_closure=self.reference(self.sequence_of("attempt_closed")),
            gate_seal=self.reference(self.sequence_of("candidate_gate_sealed")),
            measurement_id="completion",
            identity_digest="e" * 64,
            pairs_digest="f" * 64,
            missing_count=0,
            required=2,
            imported=2,
        )

    def bind_reuse_final(self, binding):
        return self.accept(
            "final_measurement_bound",
            round=1,
            attempt_id=self.sealed,
            data=self.data(),
            verification="reuse",
            candidate_source="completion",
            champion_side="fresh" if self.state.policy.stability == "unstable" else "stored",
            finalist_closure=self.reference(self.sequence_of("attempt_closed")),
            source_ref=self.reference(binding),
            candidate=self.source("completion", False),
            champion=self.source("fresh-champion", False)
            if self.state.policy.stability == "unstable"
            else self.source(self.snapshot.rounds[1].baseline_measurement_id),
        )


async def independent_run(tmp_path, monkeypatch, *, campaign_options=None, **options):
    """Run the two-case /16 campaign through real `evolve`; return its run and result."""
    import json

    from evolution.tree.evolve import evolve
    from tests import campaign_drivers as fixtures
    from tests.campaign_drivers import two_case_run as _two_case_run
    from tests.campaign_fakes import family14_document

    family15_admitted(monkeypatch)
    results = []

    async def configured(path, *args, **kwargs):
        document = family14_document(json.loads(path.read_text()), repeats=1)
        document["schema"] = "evolution-v2-manifest/16"
        document["campaign"]["gate"] = v16_document()["campaign"]["gate"]
        path.write_text(json.dumps(document))
        result = await evolve(path, *args, **kwargs)
        results.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", configured)
        await _two_case_run(
            tmp_path,
            campaign={"attempts_per_round": 2, **(campaign_options or {})},
            terminal=None,
            **options,
        )
    return tmp_path / "run", results[-1]
