"""Explicit, resumable measurement of one immutable tree on train or test."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from evolution.tree.artifacts.artifact_ledger import inspect_ledger
from evolution.tree.artifacts.artifact_policy import TreeScan, scan_tree, tree_inodes
from evolution.tree.artifacts.artifacts import ArtifactStore, CapacityEstimate
from evolution.tree.artifacts.attestation import PromotionCapableAdapter
from evolution.tree.bindings import build_environment, build_scorer
from evolution.tree.campaign import _remove_unborn_store, _strategy_for, inspect_campaign
from evolution.tree.campaign_identity import metric_plan_digest, schedule_for
from evolution.tree.campaign_measurement import Measurement, StagePlan
from evolution.tree.campaign_view import classify_store
from evolution.tree.evaluation_journal import (
    EvaluationState,
    inspect_evaluation_journal,
    open_evaluation_journal,
)
from evolution.tree.evaluation_manifest import EvaluationManifest, load_evaluation_manifest
from evolution.tree.execution.corpus_partition import load_partition, partition_notes
from evolution.tree.execution.interaction import RolloutInfrastructureError, Scorer
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.execution.journal_identity import (
    RunIdentityV1,
    canonical_json,
    corpus_digest,
    scorer_binding,
)
from evolution.tree.execution.output_admission import EvolveRefused, admit_output_root
from evolution.tree.execution.rollout_scoring import _close_scorer_traces
from evolution.tree.storage.append_log import AppendLog
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.inode_floor import (
    InodeNeed,
    _low_floor_note,
    evaluation_need,
    evaluation_resume_need,
    existing_ancestor,
    shares_tmp,
)
from evolution.tree.target.http_adapter import HttpSessionAdapter
from evolution.tree.target.target_calls import TargetCallLedger

MEASUREMENT_ID = "evaluation"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def evaluation_secrets(
    manifest: EvaluationManifest, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The secret values an evaluation run is armed with (O12, decision 14).

    One definition for the journal's birth record and every consumer that
    redacts what an evaluation prints: every declared `scorer.allowed_env`
    name the environment actually holds, value at least four characters (the
    evaluation floor; a shorter value is a flag, not a credential). `environ`
    defaults to a snapshot of the process environment taken here.
    """

    values = dict(os.environ) if environ is None else environ
    return tuple(
        values[name] for name in manifest.scorer.allowed_env if len(values.get(name, "")) >= 4
    )


def _source(manifest: EvaluationManifest) -> tuple[Path, dict[str, Any] | None]:
    if "root" in manifest.snapshot:
        return Path(manifest.snapshot["root"]), None
    root = Path(manifest.snapshot["run_dir"])
    campaign = inspect_campaign(root, verify=True).to_payload()
    if campaign.get("verification", {}).get("mismatches"):
        raise EvolveRefused("evaluation_source_unavailable")
    if not inspect_ledger(root).complete:
        raise EvolveRefused("evaluation_source_unavailable")
    lineage = ArtifactStore.inspect(root, verify=True)
    version = manifest.snapshot["version"]
    entry = next((row for row in lineage.champions if row.version_id == version), None)
    if entry is None or entry.corruption is not None:
        raise EvolveRefused("evaluation_source_unavailable")
    prefix = bytearray()
    copy_operation = None
    for record in inspect_ledger(root).records:
        prefix.extend(record.encode())
        if (
            record.event == "artifact_copy_started"
            and record.payload.get("artifact_id") == version
            and record.payload.get("kind") == "champion"
        ):
            copy_operation = record.operation_id
        if record.event == "artifact_copy_finished" and record.operation_id == copy_operation:
            break
    else:
        raise EvolveRefused("evaluation_source_unavailable")
    return root / entry.reference.locator, {
        "run_id": lineage.run_id,
        "artifact_prefix_digest": hashlib.sha256(prefix).hexdigest(),
        "artifact_prefix_bytes": len(prefix),
        "version": version,
        "candidate_digest": entry.candidate_digest,
        "dependency_digest": entry.dependency_digest,
    }


def _separate(paths: tuple[Path, ...]) -> None:
    resolved = [path.resolve() for path in paths]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise EvolveRefused("evaluation_path_overlap")


def _snapshot(
    root: Path,
    manifest: EvaluationManifest,
    source: Path,
    scan: TreeScan,
    samples: int,
    notes: Callable[[str], None] | None = None,
) -> ArtifactStore:
    location = root / "artifacts"
    birth = classify_store(location)
    if birth == "unborn":
        _remove_unborn_store(location)
    shared = shares_tmp(existing_ancestor(root))
    concurrency = manifest.policy.max_scorer_concurrency
    if birth == "born":
        lineage = ArtifactStore.inspect(location)
        # ADR-0040 D8: the resume floor is the creation floor minus what
        # already exists, checked before the store resumes.
        _note_resume_inodes(
            root,
            evaluation_resume_need(
                root,
                tree=tree_inodes(scan) if lineage.champion is None else None,
                unscored=_unscored(root, samples),
                scorer_concurrency=concurrency,
                shared_tmp=shared,
            ),
            notes,
        )
        return ArtifactStore.resume(
            location, manifest.artifacts, seed_root=source if lineage.champion is None else None
        )

    def need(tree: int) -> InodeNeed:
        return InodeNeed(
            evaluation_need(
                tree=tree, samples=samples, scorer_concurrency=concurrency, shared_tmp=shared
            )
        )

    def observe(estimate: CapacityEstimate) -> None:
        if estimate.free_inodes is not None and estimate.inode_need is not None:
            _note_inodes(estimate.free_inodes, estimate.inode_need.floor, notes)

    return ArtifactStore.create(
        location,
        source,
        manifest.artifacts,
        parallel_attempts=1,
        inodes=need,
        _estimate_observer=observe,
    )


def _note_resume_inodes(root: Path, floor: int, notes: Callable[[str], None] | None) -> None:
    """The evaluation resume estimate; unobservable or unlimited counts skip it."""

    try:
        stats = os.statvfs(existing_ancestor(root))
    except OSError:
        return
    if stats.f_files:
        _note_inodes(stats.f_favail, floor, notes)


def _note_inodes(free: int, floor: int, notes: Callable[[str], None] | None) -> None:
    message = _low_floor_note(free, floor)
    if notes is not None and message is not None:
        try:
            notes(message)
        except Exception:  # noqa: BLE001 -- only inode output is non-vetoing
            pass


def _unscored(root: Path, samples: int) -> int:
    """Samples without a durable score; all of them when the journal cannot be read."""

    if Measurement.classify(root, MEASUREMENT_ID) != "born":
        return samples
    try:
        records = inspect_journal(Measurement.directory_for(root, MEASUREMENT_ID)).records
    except Exception:  # noqa: BLE001 - the resume reports its own fault
        return samples
    scored = {
        (record.payload.get("case_id"), record.payload.get("sample_id"))
        for record in records
        if record.event == "scoring_finished"
    }
    return max(0, samples - len(scored))


def _initial_record(
    run_id: str, configuration: dict[str, Any], secrets: tuple[str, ...]
) -> Envelope:
    from datetime import UTC, datetime

    from evolution.tree.evaluation_journal import EVALUATION_FAMILY

    return Envelope.from_payload(
        {
            "schema_version": 1,
            "run_id": run_id,
            "sequence": 1,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "event": "evaluation_created",
            "operation_id": None,
            "payload": {"identity": configuration, "identity_digest": _digest(configuration)},
        },
        secrets=secrets,
        family=EVALUATION_FAMILY,
    )


def _bind_measurement(owner: AppendLog, run_id: str, identity: RunIdentityV1) -> None:
    records = owner.snapshot().records
    bound = next((row for row in records if row.event == "measurement_bound"), None)
    if bound is not None and bound.payload["identity_digest"] != identity.digest:
        raise EvolveRefused("run_identity_changed")
    if bound is None:
        owner.append(
            run_id,
            "measurement_bound",
            {"identity": identity.to_payload(), "identity_digest": identity.digest},
        )


def _pending_stage(
    measurement: Measurement, repeats: int
) -> tuple[StagePlan, frozenset[tuple[str, str]]]:
    rows = measurement.sample_rows(repeats)
    missing = {(row.case_id, row.sample_id) for row in rows if row.disposition != "score"}
    repairs = frozenset(
        (row.case_id, row.sample_id)
        for row in rows
        if row.disposition not in ("score", "not_started")
    )
    scheduled = tuple(
        row
        for row in measurement.identity.to_payload()["schedule"]
        if (row["case_id"], row["sample_id"]) in missing
    )
    return StagePlan(1, repeats, scheduled), repairs


def _record_attestations(owner: AppendLog, run_id: str, measurement: Measurement) -> None:
    for instance_id, attestation in measurement.attested.items():
        owner.append(
            run_id,
            "instance_attested",
            {
                "instance_id": instance_id,
                "semantic": asdict(attestation.semantic),
                "instance": asdict(attestation.instance),
                "attested": sorted(attestation.attested),
            },
        )


async def evaluate(
    manifest_path: Path,
    run_dir: Path,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    notes: Callable[[str], None] | None = None,
    adapter_factory: Callable[[EvaluationManifest, str, TargetCallLedger], PromotionCapableAdapter]
    | None = None,
    process_log: Callable[[Path, frozenset[str]], None] | None = None,
) -> dict[str, Any]:
    """Measure a frozen tree; optionally signal CLI log admission once (ADR-0036).

    The callback receives the run root and an empty hygiene set, and its
    errors are ignored. Without it no process log is created.
    """
    manifest = load_evaluation_manifest(Path(manifest_path))
    root = Path(run_dir).absolute()
    source, origin = _source(manifest)
    if "run_dir" in manifest.snapshot:
        _separate((root, Path(manifest.snapshot["run_dir"])))
    _separate((source, root, manifest.corpus, manifest.scorer.cwd))
    admit_output_root(root)
    partition = load_partition(manifest.corpus)
    corpus = partition.select(manifest.split).cases
    if notes is not None:
        for message in partition_notes(partition):
            notes(message)
    environment = build_environment(manifest.environment)
    scorer = build_scorer(manifest.scorer, seed_root=source, run_dir=root, accepts_diagnostics=True)
    scan = scan_tree(source, manifest.artifacts)
    if origin is not None and (
        scan.candidate_digest != origin["candidate_digest"]
        or scan.dependency_digest != origin["dependency_digest"]
    ):
        raise EvolveRefused("evaluation_source_policy_mismatch")
    configuration = {
        "profile": "evaluation/1",
        "declaration_digest": _digest(manifest.declaration),
        "partition": partition.descriptor,
        "split": manifest.split,
        "case_groups": [[case.case_id, case.group_id] for case in corpus],
        "snapshot": {
            "candidate_digest": scan.candidate_digest,
            "dependency_digest": scan.dependency_digest,
        },
        "origin": origin,
        "environment": asdict(environment.identity),
        "scorer": scorer_binding(scorer),
        "primary": manifest.policy.metric_primary,
        "repeats": manifest.policy.repeats,
    }
    secrets = evaluation_secrets(manifest)
    run_id = _evaluation_run_id(root, configuration)
    ledger = TargetCallLedger()
    ledger.bind_namespace(run_id)
    initial = _initial_record(run_id, configuration, secrets)

    owner = open_evaluation_journal(root, secrets, initial)
    store: ArtifactStore | None = None
    measurement: Measurement | None = None
    adapter: PromotionCapableAdapter | None = None
    try:
        records = owner.snapshot().records
        if records:
            if records[0].payload["identity"] != configuration:
                raise EvolveRefused("run_identity_changed")
            if records[-1].event == "evaluation_completed":
                return inspect_evaluation(root, verify=True)
        else:
            owner.append(
                run_id,
                "evaluation_created",
                {"identity": configuration, "identity_digest": _digest(configuration)},
            )
        # Binding follows artifact publication: a bound measurement proves the
        # store was born. A lost ledger is never permission to remove it
        # (ADR-0008, #270), even before admitting the invocation's process log.
        if (
            cast(EvaluationState, owner.state).bound
            and classify_store(root / "artifacts") != "born"
        ):
            raise EvolveRefused("artifact_ledger_missing")
        # The last refusal point (spec §3.1): the journal is leased, its
        # identity matched or was born, and it is not complete.
        if process_log is not None:
            try:
                process_log(root, frozenset())
            except Exception:  # noqa: BLE001 -- a log is never an evaluation failure
                pass
        artifact_log = root / "artifacts" / "artifacts.jsonl"
        if artifact_log.exists() and artifact_log.stat().st_size:
            if inspect_ledger(root / "artifacts").journal_integrity == "corrupt_suffix":
                raise EvolveRefused("evaluation_snapshot_invalid")
        store = _snapshot(
            root, manifest, source, scan, len(corpus) * manifest.policy.repeats, notes
        )
        candidate = store.champion()
        candidate.verify()
        if (
            candidate.digest != scan.candidate_digest
            or candidate.dependency_digest != scan.dependency_digest
        ):
            raise EvolveRefused("run_identity_changed")
        adapter = (
            adapter_factory(manifest, run_id, ledger)
            if adapter_factory
            else cast(
                PromotionCapableAdapter,
                HttpSessionAdapter(
                    manifest.target, manifest.artifacts, run_id=run_id, ledger=ledger
                ),
            )
        )
        preflight = await adapter.preflight(candidate.expected_attestation())
        policy = manifest.policy
        strategy = _strategy_for(preflight.capabilities.materialization)
        if (
            manifest.policy.max_concurrency
            > preflight.capabilities.max_instances * preflight.capabilities.max_concurrent_sessions
        ):
            raise EvolveRefused("capabilities_insufficient")
        identity = RunIdentityV1(
            {
                "profile": "rollout_evaluation/1",
                "candidate_digest": candidate.digest,
                "target": asdict(preflight.semantic),
                "environment": asdict(environment.identity),
                "scorer": scorer_binding(scorer),
                "evaluation_set_id": "corpus-" + corpus_digest(corpus)[:16],
                "corpus_digest": corpus_digest(corpus),
                "metric_plan_digest": metric_plan_digest(policy.metric_primary),
                "schedule": schedule_for(policy, corpus, preflight.semantic.seed_mode, "champion"),
                "budget": {
                    "max_turns": policy.budget.max_turns,
                    "timeout_seconds": policy.budget.timeout_seconds,
                },
                "max_concurrency": policy.max_concurrency,
                "max_scorer_concurrency": policy.max_scorer_concurrency,
                "materialization_strategy": strategy,
                "sample_retries": manifest.policy.sample_retries,
            }
        )
        _bind_measurement(owner, run_id, identity)
        measurement = Measurement.declare(
            run_root=root,
            campaign_run_id=run_id,
            measurement_id=MEASUREMENT_ID,
            identity=identity,
            corpus=corpus,
            secrets=secrets,
        )
        if Measurement.classify(root, MEASUREMENT_ID) == "born":
            await measurement.resume(adapter)
        else:
            Measurement.remove_unborn(root, MEASUREMENT_ID)
            measurement.create()
        stage, repairs = _pending_stage(measurement, policy.repeats)
        if stage.rows and not cancelled():
            await measurement.materialize(adapter, candidate, strategy, policy)
            _record_attestations(owner, run_id, measurement)
            await measurement.run_stage(
                stage,
                adapter=adapter,
                candidate=candidate,
                strategy=strategy,
                environment=environment,
                scorer=cast(Scorer, scorer),
                policy=policy,
                expected=preflight.semantic,
                cancelled=cancelled,
                repair_rows=repairs,
            )
        await measurement.release_all(adapter)
        if measurement.complete():
            _aggregates(measurement.sample_rows(policy.repeats), configuration)
            owner.append(
                run_id,
                "evaluation_completed",
                {"measurement_prefix": measurement.prefix_reference().content_digest},
            )
        else:
            owner.append(
                run_id,
                "evaluation_suspended",
                {"reason": "cancelled" if cancelled() else "incomplete"},
            )
        return inspect_evaluation(root, verify=True)
    except RolloutInfrastructureError:
        owner.append(run_id, "evaluation_suspended", {"reason": "infrastructure"})
        return inspect_evaluation(root, verify=True)
    finally:
        _close_scorer_traces(scorer)
        if measurement is not None:
            try:
                if adapter is not None:
                    await measurement.release_all(adapter)
            finally:
                measurement.close()
        if store is not None:
            store.close()
        ledger.close()
        await ledger.settle()
        owner.close()


def inspect_evaluation(run_dir: Path, *, verify: bool = True) -> dict[str, Any]:
    root = Path(run_dir)
    snapshot = inspect_evaluation_journal(root)
    if not snapshot.complete or not snapshot.records:
        raise EvolveRefused("evaluation_journal_invalid")
    configuration = snapshot.records[0].payload["identity"]
    bound = next(
        (record for record in snapshot.records if record.event == "measurement_bound"), None
    )
    completed = snapshot.records[-1].event == "evaluation_completed"
    result: dict[str, Any] = {
        "schema": "evolution-v2-evaluation-result/1",
        "run_id": snapshot.records[0].to_payload()["run_id"],
        "state": "completed" if completed else "suspended",
        "started_at": snapshot.records[0].to_payload()["timestamp"],
        "finished_at": snapshot.records[-1].to_payload()["timestamp"] if completed else None,
        "requested_cases": len(configuration["case_groups"]),
        "requested_groups": len({group for _, group in configuration["case_groups"]}),
        "split": configuration["split"],
        "identity_digest": snapshot.records[0].payload["identity_digest"],
        "snapshot": configuration["snapshot"],
        "origin": configuration["origin"],
        "partition_digest": _digest(configuration["partition"]),
        "measurement_identity": None if bound is None else bound.payload["identity"],
        "requested_samples": len(configuration["case_groups"]) * configuration["repeats"],
        "completed_samples": 0,
        "metrics": None,
        "weighting": "equal groups; equal cases within group; mean repeats within case",
    }
    if verify and (completed or (root / "artifacts" / "artifacts.jsonl").exists()):
        if not inspect_ledger(root / "artifacts").complete:
            raise EvolveRefused("evaluation_snapshot_invalid")
        lineage = ArtifactStore.inspect(root / "artifacts", verify=True)
        if (
            lineage.champion is None
            or lineage.champion.corruption is not None
            or lineage.champion.candidate_digest != configuration["snapshot"]["candidate_digest"]
            or lineage.champion.dependency_digest != configuration["snapshot"]["dependency_digest"]
        ):
            raise EvolveRefused("evaluation_snapshot_invalid")
    if bound is None or Measurement.classify(root, MEASUREMENT_ID) != "born":
        if completed:
            raise EvolveRefused("evaluation_measurement_missing")
        return result
    evidence = inspect_journal(Measurement.directory_for(root, MEASUREMENT_ID))
    if (
        not evidence.complete
        or evidence.records[0].payload["identity"] != bound.payload["identity"]
    ):
        raise EvolveRefused("evaluation_measurement_invalid")
    measurement = Measurement.open_frozen(root, MEASUREMENT_ID)
    rows = measurement.sample_rows(configuration["repeats"])
    result["completed_samples"] = sum(row.disposition == "score" for row in rows)
    from collections import Counter

    result["unscored_by_outcome"] = dict(
        Counter(row.outcome or row.disposition for row in rows if row.disposition != "score")
    )
    if completed:
        if (
            snapshot.records[-1].payload["measurement_prefix"] != evidence.prefix_digest
            or not measurement.complete()
        ):
            raise EvolveRefused("evaluation_measurement_incomplete")
        result["metrics"] = _aggregates(rows, configuration)
        result["metric_coverage"] = _metric_coverage(rows, configuration)
    return result


def _evaluation_run_id(root: Path, configuration: Mapping[str, Any]) -> str:
    """Read the owner and admit its bound store before any torn-tail repair."""
    if (root / "evaluation.jsonl").exists():
        snapshot = inspect_evaluation_journal(root)
        if snapshot.journal_integrity == "corrupt_suffix":
            raise EvolveRefused("evaluation_journal_invalid")
        if snapshot.records:
            state = EvaluationState()
            for record in snapshot.records:
                state.accept(record)
            # A mismatching identity is refused by the leased opener before
            # repair; a completed owner keeps its existing verification path.
            if (
                snapshot.records[0].payload["identity"] == configuration
                and state.bound
                and not state.finished
                and classify_store(root / "artifacts") != "born"
            ):
                raise EvolveRefused("artifact_ledger_missing")
            return str(snapshot.records[0].to_payload()["run_id"])
    return "eval-" + _digest(str(root))[:20]


def _aggregates(rows: Any, configuration: Mapping[str, Any]) -> dict[str, float]:
    """Require full primary coverage; weight optional metrics over their observed coverage."""
    by_case: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.metrics is None or configuration["primary"] not in row.metrics:
            raise EvolveRefused("evaluation_metric_coverage")
        for name, value in row.metrics.items():
            by_case[row.case_id][name].append(value)
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for case_id, group_id in configuration["case_groups"]:
        if len(by_case[case_id][configuration["primary"]]) != configuration["repeats"]:
            raise EvolveRefused("evaluation_metric_coverage")
        for name, values in by_case[case_id].items():
            groups[name][group_id].append(sum(values) / len(values))
    return {
        name: sum(sum(values) / len(values) for values in members.values()) / len(members)
        for name, members in groups.items()
    }


def _metric_coverage(rows: Any, configuration: Mapping[str, Any]) -> dict[str, Any]:
    case_groups = dict(configuration["case_groups"])
    observed: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for name in row.metrics or {}:
            observed[name].append(row.case_id)
    return {
        name: {
            "samples": len(cases),
            "cases": len(set(cases)),
            "groups": len({case_groups[case] for case in cases}),
        }
        for name, cases in observed.items()
    }
