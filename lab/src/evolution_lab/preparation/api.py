"""Public synchronous facade for durable campaign and evaluation preparation."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from evolution.tree import ManifestError, load_manifest_v2
from evolution.tree.execution.corpus import CorpusError, load_eval_corpus

from evolution_lab._manifest_schema import (
    CAMPAIGN_MANIFEST_SCHEMAS,
    DIAGNOSIS_LIMIT,
    EVALUATION_MANIFEST_SCHEMA,
    campaign_cases,
    core_diagnosis,
    is_campaign_manifest,
)
from evolution_lab.owner import core_raw
from evolution_lab.owner.core_raw import CoreRawUnavailable
from evolution_lab.runner import _campaign_stop_grace

from .bundle import create_bundle as compose_bundle
from .environment import capture_environment
from .internal_manifest import load_internal_manifest
from .evaluation import (
    bind_campaign_evaluation,
    bind_evaluation_input,
    confirm_schedule,
    finite_json_object,
    load_evaluation_input,
)
from .models import (
    BundleSummary,
    CampaignSource,
    EnvironmentDescriptor,
    EnvironmentInput,
    EvaluationPlan,
    EvaluationSource,
    PathBinding,
    PreparationError,
    PreparedExecutionDescriptor,
    PreparedSummary,
    ProbeSource,
    RuntimePolicy,
    SourceRoot,
    TargetBinding,
)
from .storage import PreparationStorage
from .warnings import bundle_warnings


_CLEANUP_WAIT_SECONDS = 30.0


def describe_environment(environment_input: EnvironmentInput) -> EnvironmentDescriptor:
    """Verify one declared Python environment and expose only its descriptor."""
    if not isinstance(environment_input, EnvironmentInput):
        raise PreparationError("invalid_request", "bundle")
    execution = environment_input.executable.absolute() == Path(sys.executable).absolute()
    return capture_environment(environment_input, execution=execution).descriptor


def _prepare_error(error: PreparationError) -> PreparationError:
    return PreparationError(error.code, "prepare")


def _saved_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError, TypeError):
        raise PreparationError("artifacts_invalid", "prepare") from None
    if not isinstance(payload, dict):
        raise PreparationError("artifacts_invalid", "prepare")
    return payload


def _check_capacity(manifest, launch: Mapping[str, Any]) -> None:
    """Core's admission of the policy against the launch limits, before any spend (spec 2026-10-09 §4.1)."""
    try:
        limits = launch["limits"]
        declared, sessions = limits["max_instances"], limits["max_concurrent_sessions"]
        policy = manifest.workflow.campaign
        if core_raw.capacity_admitted(policy, max_instances=declared, max_concurrent_sessions=sessions):
            return
        try:
            needed = core_raw.instance_demand(policy, sessions)
        except ValueError:  # core refuses to count past 1024 instances
            needed = "more than 1024"
    except (KeyError, TypeError, CoreRawUnavailable):
        raise PreparationError("artifacts_invalid", "prepare") from None
    raise PreparationError(
        "artifacts_invalid", "prepare",
        f"target capacity: the campaign needs {needed} instances, the target declares {declared}"[:DIAGNOSIS_LIMIT])


def _target_binding(bundle, manifest, launch: Mapping[str, Any]) -> TargetBinding:
    try:
        endpoint = launch["endpoint"]
        identity = launch["identity"]
        limits = launch["limits"]
        policy_path = bundle.path / launch["policy"]
        policy_manifest = (
            load_internal_manifest(policy_path)
            if bundle.descriptor.source_kind in {"evaluation", "probe"}
            else load_manifest_v2(policy_path)
        )
        target = manifest.target
        if (
            endpoint["base_url"] != target.base_url
            or identity["seed_mode"] != target.seed_mode
            or (target.contract_digest is not None and identity["contract_digest"] != target.contract_digest)
            or (target.settings_hash is not None and identity["settings_hash"] != target.settings_hash)
            or policy_manifest.policy_digest != manifest.policy_digest
            or limits["max_input_bytes"] < target.min_input_bytes
            or limits["max_output_bytes"] > target.max_response_bytes
        ):
            raise PreparationError("artifacts_invalid", "prepare")
        return TargetBinding(
            launch_digest=bundle.summary().launch_digest,
            environment_digest=launch["environment_ref"],
            base_url=endpoint["base_url"],
            policy_digest=manifest.policy_digest,
            target_id=identity["target_id"],
            contract_digest=identity["contract_digest"],
            settings_hash=identity["settings_hash"],
            runtime_ref_digest=identity["runtime_ref_digest"],
            seed_mode=identity["seed_mode"],
        )
    except PreparationError:
        raise
    except (KeyError, TypeError, ManifestError, ValueError):
        raise PreparationError("artifacts_invalid", "prepare") from None


class PreparationStore:
    """Create and reopen immutable Bundle and PreparedExecution summaries."""

    def __init__(self, state_directory: Path | str):
        self._storage = PreparationStorage(state_directory)

    def create_bundle(
        self,
        *,
        source: CampaignSource | EvaluationSource | ProbeSource,
        environments: Sequence[EnvironmentInput],
        env: Mapping[str, str],
        secret_names: Sequence[str],
        roots: Sequence[SourceRoot],
        path_bindings: Sequence[PathBinding] = (),
        expected_execution_environment: str | None = None,
    ) -> BundleSummary:
        stored = compose_bundle(
            self._storage,
            source=source,
            environments=environments,
            env=env,
            secret_names=secret_names,
            roots=roots,
            path_bindings=path_bindings,
            expected_execution_environment=expected_execution_environment,
        )
        return stored.summary(bundle_warnings(stored))

    def prepare_campaign(
        self,
        bundle_digest: str,
        *,
        stop_grace_seconds: int | float | None = None,
        cleanup_wait_seconds: int | float | None = None,
    ) -> PreparedSummary:
        try:
            bundle = self._storage.get_bundle(bundle_digest)
        except PreparationError as error:
            raise _prepare_error(error) from None
        try:
            if bundle.descriptor.source_kind != "campaign":
                raise PreparationError("artifacts_invalid", "prepare")
            manifest = load_manifest_v2(bundle.path / bundle.descriptor.entrypoint)
            if not is_campaign_manifest(manifest):
                raise PreparationError("artifacts_invalid", "prepare")
            cases = campaign_cases(manifest)
        except PreparationError:
            raise
        except (ManifestError, CorpusError) as error:
            raise PreparationError("artifacts_invalid", "prepare", core_diagnosis(error)) from None
        except (CoreRawUnavailable, OSError, ValueError, TypeError):
            raise PreparationError("artifacts_invalid", "prepare") from None
        if any(case.public.get("conformance") is True for case in cases):
            raise PreparationError("reserved_public_input", "prepare")

        launch = _saved_json_object(bundle.path / bundle.descriptor.launch)
        target_binding = _target_binding(bundle, manifest, launch)
        _check_capacity(manifest, launch)
        runtime_policy = RuntimePolicy(
            _campaign_stop_grace(manifest)
            if stop_grace_seconds is None
            else stop_grace_seconds,
            _CLEANUP_WAIT_SECONDS
            if cleanup_wait_seconds is None
            else cleanup_wait_seconds,
        )
        descriptor = PreparedExecutionDescriptor(
            bundle_digest=bundle.digest,
            entrypoint=bundle.descriptor.entrypoint,
            target_binding=target_binding,
            execution_environment=bundle.descriptor.environments[0].digest,
            secret_names=bundle.descriptor.secret_names,
            runtime_policy=runtime_policy,
            evaluation=None,
        )
        return self._storage.publish_prepared(descriptor).summary()

    def prepare_evaluation(
        self,
        bundle_digest: str,
        *,
        selection: Mapping[str, Any],
        split: str | None = None,
        repeat_seeds: Sequence[int | None] | None = None,
        evaluation_set_id: str | None = None,
        max_turns: int | None = None,
        timeout_seconds: int | float | None = None,
        max_failed_turns: int | None = None,
        call_retries: Mapping[str, int] | str | None = None,
        sample_retries: int | None = None,
        min_coverage: float | None = None,
        max_consecutive_unmeasured: int | str | None = None,
        concurrency: int | None = None,
        scorer_concurrency: int | None = None,
        metric_plan: Mapping[str, Any] | None = None,
        runtime_policy: RuntimePolicy | Mapping[str, Any] | None = None,
    ) -> PreparedSummary:
        """Freeze an explicit fixed evaluation plan without executing it.

        ``split``: `"train"`/`"test"` of a campaign Bundle's corpus, `None` = train (an unlabelled corpus is all
        train); frozen into `case_ids`. An evaluation Bundle has no split: only `None`."""
        if split not in (None, "train", "test"):
            raise PreparationError("invalid_request", "prepare")
        try:
            bundle = self._storage.get_bundle(bundle_digest)
        except PreparationError as error:
            raise _prepare_error(error) from None

        try:
            launch = _saved_json_object(bundle.path / bundle.descriptor.launch)
            manifest_path = bundle.path / bundle.descriptor.conformance_entrypoint
            manifest = (
                load_internal_manifest(manifest_path)
                if bundle.descriptor.source_kind in {"evaluation", "probe"}
                else load_manifest_v2(manifest_path)
            )
            if manifest.schema not in {EVALUATION_MANIFEST_SCHEMA, *CAMPAIGN_MANIFEST_SCHEMAS}:
                raise PreparationError("artifacts_invalid", "prepare")
            if bundle.descriptor.source_kind == "evaluation":
                if split is not None:
                    raise PreparationError("invalid_request", "prepare")
                value = load_evaluation_input(bundle.path / bundle.descriptor.entrypoint, "prepare")
                cases = load_eval_corpus(
                    bundle.path / Path(bundle.descriptor.entrypoint).parent / value["corpus"]["path"]
                )
                environment, scorer, scorer_section = bind_evaluation_input(
                    value, document=bundle.path / bundle.descriptor.entrypoint,
                    manifest=manifest, env=bundle.descriptor.env,
                    secret_names=bundle.descriptor.secret_names, stage="prepare",
                )
                defaults = value["defaults"]
                repeat_seeds = defaults["repeat_seeds"] if repeat_seeds is None else repeat_seeds
                evaluation_set_id = value["evaluation_set_id"] if evaluation_set_id is None else evaluation_set_id
                max_turns = defaults["max_turns"] if max_turns is None else max_turns
                timeout_seconds = defaults["timeout_seconds"] if timeout_seconds is None else timeout_seconds
                metric_plan = value["metric_plan"] if metric_plan is None else metric_plan
                # JSON null in the document means "not given", like a missing key.
                if max_failed_turns is None:
                    max_failed_turns = defaults.get("max_failed_turns")
                if call_retries is None:
                    call_retries = defaults.get("call_retries")
                sample_retries = defaults.get("sample_retries") if sample_retries is None else sample_retries
                min_coverage = defaults.get("min_coverage") if min_coverage is None else min_coverage
                if max_consecutive_unmeasured is None:
                    max_consecutive_unmeasured = defaults.get("max_consecutive_unmeasured")
                concurrency = defaults.get("concurrency") if concurrency is None else concurrency
                scorer_concurrency = defaults.get("scorer_concurrency") if scorer_concurrency is None else scorer_concurrency
            elif bundle.descriptor.source_kind == "campaign":
                campaign = load_manifest_v2(bundle.path / bundle.descriptor.entrypoint)
                if not is_campaign_manifest(campaign):
                    raise PreparationError("artifacts_invalid", "prepare")
                manifest = campaign
                cases = campaign_cases(campaign, split or "train")
                if any(value is None for value in (
                    repeat_seeds, evaluation_set_id, max_turns, timeout_seconds, metric_plan,
                )):
                    raise PreparationError("invalid_request", "prepare")
                environment, scorer, scorer_section = bind_campaign_evaluation(
                    campaign,
                    bundle_root=bundle.path,
                    env=bundle.descriptor.env,
                    secret_names=bundle.descriptor.secret_names,
                    stage="prepare",
                )
            else:
                raise PreparationError("artifacts_invalid", "prepare")
        except PreparationError:
            raise
        except (ManifestError, CorpusError) as error:
            raise PreparationError("artifacts_invalid", "prepare", core_diagnosis(error)) from None
        except (CoreRawUnavailable, OSError, TypeError, ValueError):
            raise PreparationError("artifacts_invalid", "prepare") from None

        if any(case.public.get("conformance") is True for case in cases):
            raise PreparationError("reserved_public_input", "prepare")
        selected = _select_cases(cases, selection)
        try:
            assert repeat_seeds is not None
            assert evaluation_set_id is not None
            assert max_turns is not None
            assert timeout_seconds is not None
            assert metric_plan is not None
            if isinstance(call_retries, str) and call_retries != "off":
                raise PreparationError("invalid_request", "prepare")
            # Resolve new preparation defaults here, after document overrides.
            # Stored EvaluationPlan payloads retain their historical null/absence
            # semantics and must never acquire these defaults when read.
            if call_retries is None:
                call_retries = {"max_attempts": 3, "turn_max_attempts": 2}
            plan = EvaluationPlan(
                case_ids=tuple(case.case_id for case in selected),
                repeat_seeds=tuple(repeat_seeds),
                evaluation_set_id=evaluation_set_id,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
                metric_plan=finite_json_object(metric_plan, "prepare", request=True),
                max_failed_turns=max_turns if max_failed_turns is None else max_failed_turns,
                call_retries=None if call_retries == "off" else call_retries,
                concurrency=1 if concurrency is None else concurrency,
                scorer_concurrency=1 if scorer_concurrency is None else scorer_concurrency,
                sample_retries=3 if sample_retries is None else sample_retries,
                min_coverage=0.95 if min_coverage is None else min_coverage,
                max_consecutive_unmeasured=max_consecutive_unmeasured,
            )
        except (AssertionError, TypeError):
            raise PreparationError("invalid_request", "prepare") from None
        if plan.concurrency > launch["limits"]["max_concurrent_sessions"]:
            raise PreparationError(
                "invalid_request", "prepare", "concurrency exceeds target max_concurrent_sessions."
            )
        if any((seed is None) == (manifest.target.seed_mode == "exact") for seed in plan.repeat_seeds):
            raise PreparationError("invalid_request", "prepare")
        confirm_schedule(
            plan=plan, cases=selected, manifest=manifest,
            environment=environment, scorer=scorer,
        )
        target_binding = _target_binding(bundle, manifest, launch)
        if runtime_policy is None:
            scorer_timeout = scorer_section.timeout_seconds
            if scorer_timeout is None:
                raise PreparationError("artifacts_invalid", "prepare")
            policy = RuntimePolicy(
                3 * plan.timeout_seconds
                + 3 * (manifest.target.timeouts.connect_seconds + manifest.target.timeouts.read_seconds)
                + scorer_timeout + 10,
                _CLEANUP_WAIT_SECONDS,
            )
        elif isinstance(runtime_policy, RuntimePolicy):
            policy = runtime_policy
        else:
            try:
                policy = RuntimePolicy.from_payload(runtime_policy)
            except (PreparationError, TypeError):
                raise PreparationError("invalid_request", "prepare") from None
        allowed = set(scorer_section.allowed_env)
        descriptor = PreparedExecutionDescriptor(
            bundle_digest=bundle.digest,
            kind="evaluation",
            entrypoint=bundle.descriptor.entrypoint,
            target_binding=target_binding,
            execution_environment=bundle.descriptor.environments[0].digest,
            secret_names=tuple(name for name in bundle.descriptor.secret_names if name in allowed),
            runtime_policy=policy,
            evaluation=plan,
        )
        return self._storage.publish_prepared(descriptor).summary()

    def get_bundle(self, digest: str) -> BundleSummary:
        stored = self._storage.get_bundle(digest)
        return stored.summary(bundle_warnings(stored))

    def get_prepared(self, digest: str) -> PreparedSummary:
        return self._storage.get_prepared(digest).summary()


def _select_cases(cases, selection: Mapping[str, Any]):
    if not isinstance(selection, Mapping) or len(selection) != 1:
        raise PreparationError("invalid_request", "prepare")
    if set(selection) == {"all"}:
        if selection["all"] is not True:
            raise PreparationError("invalid_request", "prepare")
        return tuple(cases)
    if set(selection) == {"count"}:
        count = selection["count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= len(cases):
            raise PreparationError("invalid_request", "prepare")
        return tuple(cases[:count])
    if set(selection) == {"case_ids"}:
        case_ids = selection["case_ids"]
        if (
            not isinstance(case_ids, (list, tuple)) or not case_ids
            or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
            or len(set(case_ids)) != len(case_ids)
        ):
            raise PreparationError("invalid_request", "prepare")
        by_id = {case.case_id: case for case in cases}
        if any(case_id not in by_id for case_id in case_ids):
            raise PreparationError("invalid_request", "prepare")
        return tuple(by_id[case_id] for case_id in case_ids)
    raise PreparationError("invalid_request", "prepare")
