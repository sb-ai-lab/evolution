"""Fresh campaign paths, exact worker configuration, and settlement paths."""
from __future__ import annotations

from collections.abc import Sequence

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import asyncio
import uuid
from dataclasses import field, replace

from evolution_lab._outcomes import MAX_EVALUATION_RESULT_BYTES

from .models import Approval, BaselineImportChoice, CoreTerminal, OwnerError, Refusal, TargetRef
from .receipts import ReceiptError, open_directory_nofollow, read_json, validate_id

from evolution_lab.durability import sync_file

from evolution_lab.launch_env import with_launch_values


CAMPAIGN_CONFIG_SCHEMA = "lab-campaign-worker-config/2"
EVALUATION_CONFIG_SCHEMA = "lab-evaluation-worker-config/1"
EVALUATION_OUTCOME_SCHEMA = "lab-evaluation-outcome/3"
_CONFIG_KEYS = {
    "schema", "operation_id", "scope_id", "owner_generation", "bundle_digest",
    "prepared_digest", "manifest_path", "run_directory", "run_id",
    "settlement_path", "diagnostic_path", "mode",
}
_EVALUATION_CONFIG_KEYS = {
    "schema", "operation_id", "scope_id", "owner_generation", "bundle_digest",
    "prepared_digest", "state_directory", "run_directory", "run_id",
    "outcome_path", "inputs_path", "settlement_path", "diagnostic_path",
}
_EVALUATION_OUTCOME_KEYS = {
    "schema", "operation_id", "scope_id", "run_id", "measurement_returned",
    "coverage", "stopped_reason", "evaluation_budget", "unmeasured", "min_coverage",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CampaignPaths:
    scope_directory: Path
    operation_directory: Path
    run_directory: Path
    settlement_path: Path
    diagnostic_path: Path
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True, slots=True)
class EvaluationPaths:
    scope_directory: Path
    operation_directory: Path
    run_directory: Path
    measurement_directory: Path
    inputs_path: Path
    outcome_path: Path
    settlement_path: Path
    diagnostic_path: Path
    stdout_path: Path
    stderr_path: Path


def _open_child(directory_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )


def _fresh_directory(directory_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=directory_fd)
    except FileExistsError:
        raise OwnerError("path_conflict", "before_start") from None
    child = _open_child(directory_fd, name)
    sync_file(directory_fd)
    return child


def create_campaign_paths(
    state_root, operation_id: str, scope_id: str, *, continue_run: bool = False
) -> CampaignPaths:
    """Create exact fresh no-follow operation, run, and scope leaves.

    ``continue_run=True`` opens the operation/run pair core already wrote to
    for a previous start instead of creating it, and only preflights and
    creates the new scope. The run directory must exist and be a real
    directory (no symlink) or the admission fails as ``campaign_unreadable``:
    Lab cannot continue a campaign whose run directory it cannot see.
    """
    try:
        validate_id(operation_id)
        validate_id(scope_id)
        root = Path(state_root).absolute()
        root_fd = open_directory_nofollow(root)
        try:
            scopes_fd = _open_child(root_fd, "scopes")
            artifacts_fd = _open_child(root_fd, "artifacts")
            try:
                if continue_run:
                    try:
                        os.stat(scope_id, dir_fd=scopes_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise OwnerError("path_conflict", "before_start")
                    try:
                        operation_fd = _open_child(artifacts_fd, operation_id)
                        try:
                            run_fd = _open_child(operation_fd, operation_id)
                            os.close(run_fd)
                        finally:
                            os.close(operation_fd)
                    except (FileNotFoundError, NotADirectoryError):
                        raise OwnerError("campaign_unreadable", "before_start") from None
                    except OSError as error:
                        if error.errno == errno.ELOOP:
                            raise OwnerError("campaign_unreadable", "before_start") from None
                        raise
                else:
                    # Preflight both identities so an ordinary occupied path creates
                    # no new sibling. The Owner lock serializes sanctioned writers.
                    for descriptor, name in ((scopes_fd, scope_id), (artifacts_fd, operation_id)):
                        try:
                            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise OwnerError("path_conflict", "before_start")
                    operation_fd = _fresh_directory(artifacts_fd, operation_id)
                    try:
                        run_fd = _fresh_directory(operation_fd, operation_id)
                        os.close(run_fd)
                    finally:
                        os.close(operation_fd)
                scope_fd = _fresh_directory(scopes_fd, scope_id)
                os.close(scope_fd)
            finally:
                os.close(scopes_fd)
                os.close(artifacts_fd)
        finally:
            os.close(root_fd)
    except OwnerError:
        raise
    except (OSError, ReceiptError):
        raise OwnerError("io_error", "before_start") from None
    scope = root / "scopes" / scope_id
    operation = root / "artifacts" / operation_id
    run = operation / operation_id
    return CampaignPaths(
        scope, operation, run, scope / "settlement.json", scope / "diagnostic.json",
        scope / "stdout.log", scope / "stderr.log",
    )


def create_evaluation_paths(state_root, operation_id: str, scope_id: str) -> EvaluationPaths:
    """Create a fresh operation root while keeping Lab facts in its sibling scope."""
    common = create_campaign_paths(state_root, operation_id, scope_id)
    return EvaluationPaths(
        common.scope_directory,
        common.operation_directory,
        common.run_directory,
        common.run_directory / "measurement",
        common.scope_directory / "evaluation-inputs.json",
        common.scope_directory / "evaluation-outcome.json",
        common.settlement_path,
        common.diagnostic_path,
        common.stdout_path,
        common.stderr_path,
    )


def _absolute_path(value) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("invalid worker path")
    text = os.fspath(value)
    path = Path(text)
    if "\0" in text or not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid worker path")
    return path


def _digest(value) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("invalid digest")
    return value


def validate_campaign_worker_config(value):
    """Validate one closed config object without scalar coercion."""
    try:
        if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
            raise ValueError("invalid campaign worker configuration")
        if value["schema"] != CAMPAIGN_CONFIG_SCHEMA:
            raise ValueError("invalid campaign worker schema")
        if type(value["mode"]) is not str or value["mode"] not in ("new", "continue", "retry"):
            raise ValueError("invalid campaign worker mode")
        for name in ("operation_id", "scope_id", "owner_generation"):
            validate_id(value[name])
        for name in ("bundle_digest", "prepared_digest"):
            _digest(value[name])
        manifest = _absolute_path(value["manifest_path"])
        run = _absolute_path(value["run_directory"])
        settlement = _absolute_path(value["settlement_path"])
        diagnostic = _absolute_path(value["diagnostic_path"])
        if (
            run.name != value["operation_id"]
            or run.parent.name != value["operation_id"]
            or settlement.name != "settlement.json"
            or diagnostic.name != "diagnostic.json"
            or settlement.parent != diagnostic.parent
            or settlement.parent.name != value["scope_id"]
            or manifest == run
            or manifest.is_relative_to(run)
        ):
            raise ValueError("invalid campaign worker path binding")
        from evolution.tree.evolve import derive_run_id

        if value["run_id"] != derive_run_id(run):
            raise ValueError("campaign run identity mismatch")
        return dict(value)
    except (KeyError, ReceiptError, TypeError):
        raise ValueError("invalid campaign worker configuration") from None


def campaign_worker_config(*, operation_id, scope_id, owner_generation,
        bundle_digest, prepared_digest, manifest_path, run_directory,
        settlement_path, diagnostic_path, mode="new"):
    from evolution.tree.evolve import derive_run_id

    run = _absolute_path(run_directory)
    value = {
        "schema": CAMPAIGN_CONFIG_SCHEMA,
        "operation_id": operation_id,
        "scope_id": scope_id,
        "owner_generation": owner_generation,
        "bundle_digest": bundle_digest,
        "prepared_digest": prepared_digest,
        "manifest_path": str(_absolute_path(manifest_path)),
        "run_directory": str(run),
        "run_id": derive_run_id(run),
        "settlement_path": str(_absolute_path(settlement_path)),
        "diagnostic_path": str(_absolute_path(diagnostic_path)),
        "mode": mode,
    }
    return validate_campaign_worker_config(value)


def campaign_worker_command(executable) -> list[str]:
    path = _absolute_path(executable)
    return [str(path), "-I", "-m", "evolution_lab.owner._campaign_worker"]


def validate_evaluation_worker_config(value):
    """Validate the closed evaluation worker config and exact state-relative paths."""
    try:
        if not isinstance(value, dict) or set(value) != _EVALUATION_CONFIG_KEYS:
            raise ValueError("invalid evaluation worker configuration")
        if value["schema"] != EVALUATION_CONFIG_SCHEMA:
            raise ValueError("invalid evaluation worker schema")
        for name in ("operation_id", "scope_id", "owner_generation"):
            validate_id(value[name])
        for name in ("bundle_digest", "prepared_digest"):
            _digest(value[name])
        state = _absolute_path(value["state_directory"])
        run = _absolute_path(value["run_directory"])
        outcome = _absolute_path(value["outcome_path"])
        inputs = _absolute_path(value["inputs_path"])
        settlement = _absolute_path(value["settlement_path"])
        diagnostic = _absolute_path(value["diagnostic_path"])
        operation = state / "artifacts" / value["operation_id"]
        scope = state / "scopes" / value["scope_id"]
        if (
            run != operation / value["operation_id"]
            or outcome != scope / "evaluation-outcome.json"
            or inputs != scope / "evaluation-inputs.json"
            or settlement != scope / "settlement.json"
            or diagnostic != scope / "diagnostic.json"
        ):
            raise ValueError("invalid evaluation worker path binding")
        from evolution.tree.evolve import derive_run_id

        if value["run_id"] != derive_run_id(run):
            raise ValueError("evaluation run identity mismatch")
        return dict(value)
    except (KeyError, ReceiptError, TypeError):
        raise ValueError("invalid evaluation worker configuration") from None


def evaluation_worker_config(
    *, operation_id, scope_id, owner_generation, bundle_digest, prepared_digest,
    state_directory, run_directory, outcome_path, inputs_path, settlement_path,
    diagnostic_path,
):
    from evolution.tree.evolve import derive_run_id

    run = _absolute_path(run_directory)
    value = {
        "schema": EVALUATION_CONFIG_SCHEMA,
        "operation_id": operation_id,
        "scope_id": scope_id,
        "owner_generation": owner_generation,
        "bundle_digest": bundle_digest,
        "prepared_digest": prepared_digest,
        "state_directory": str(_absolute_path(state_directory)),
        "run_directory": str(run),
        "run_id": derive_run_id(run),
        "outcome_path": str(_absolute_path(outcome_path)),
        "inputs_path": str(_absolute_path(inputs_path)),
        "settlement_path": str(_absolute_path(settlement_path)),
        "diagnostic_path": str(_absolute_path(diagnostic_path)),
    }
    return validate_evaluation_worker_config(value)


def evaluation_worker_command(executable) -> list[str]:
    path = _absolute_path(executable)
    return [str(path), "-I", "-m", "evolution_lab.owner._evaluation_worker"]


def read_evaluation_outcome(
    path, *, operation_id, scope_id, run_id,
):
    """Read optional worker facts without treating them as measurement evidence."""
    try:
        validate_id(operation_id)
        validate_id(scope_id)
        value = read_json(path, max_bytes=MAX_EVALUATION_RESULT_BYTES)
        legacy = isinstance(value, dict) and value.get("schema") == "lab-evaluation-outcome/2"
        keys = _EVALUATION_OUTCOME_KEYS - {"unmeasured", "min_coverage"} if legacy else _EVALUATION_OUTCOME_KEYS
        if not isinstance(value, dict) or set(value) != keys:
            raise ReceiptError("invalid evaluation outcome shape")
        if (
            value["schema"] != ("lab-evaluation-outcome/2" if legacy else EVALUATION_OUTCOME_SCHEMA)
            or value["operation_id"] != operation_id
            or value["scope_id"] != scope_id
            or value["run_id"] != run_id
            or type(value["measurement_returned"]) is not bool
        ):
            raise ReceiptError("evaluation outcome identity mismatch")
        coverage = value["coverage"]
        if coverage is not None:
            if not isinstance(coverage, dict) or set(coverage) != {"expected", "scored"}:
                raise ReceiptError("invalid evaluation outcome coverage")
            expected, scored = coverage["expected"], coverage["scored"]
            if (
                type(expected) is not int
                or type(scored) is not int
                or expected < 0
                or not 0 <= scored <= expected
            ):
                raise ReceiptError("invalid evaluation outcome coverage")
        stopped = value["stopped_reason"]
        if stopped is not None and (not isinstance(stopped, str) or not stopped):
            raise ReceiptError("invalid evaluation stop reason")
        budget = value["evaluation_budget"]
        if not isinstance(budget, dict) or set(budget) != {"max_failed_turns", "call_retries"}:
            raise ReceiptError("invalid evaluation budget")
        threshold = budget["max_failed_turns"]
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ReceiptError("invalid evaluation budget")
        retries = budget["call_retries"]
        if retries is not None and not (isinstance(retries, dict)
                and set(retries) == {"max_attempts", "turn_max_attempts"}
                and all(type(v) is int and 1 <= v <= 10 for v in retries.values())):
            raise ReceiptError("invalid evaluation budget")
        from evolution_lab._outcomes import validate_coverage_threshold, validate_unmeasured
        if legacy:
            value = {**value, "unmeasured": None, "min_coverage": 1.0}
        validate_coverage_threshold(value["min_coverage"])
        validate_unmeasured(value["unmeasured"])
        return dict(value)
    except (KeyError, TypeError, ValueError):
        raise ReceiptError("invalid evaluation outcome") from None


@dataclass
class OperationRuntime:
    id: str
    scope_id: str
    prepared: object
    target: object
    secrets: object
    done: asyncio.Future
    helper: object = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    forced: bool = False
    paths: CampaignPaths | None = None
    mode: str = "new"
    # The core refusal finalization read for this start (ADR-0023). Kept here, not only in the
    # value being written, so a failed finalization write does not lose it (ADR-0004).
    refusal: Refusal | None = None
    core_terminal: CoreTerminal | None = None
    reflector_probe: object = None
    policy_use: str | None = None
    linked: dict | None = None
    baseline_import: BaselineImportChoice | None = None

    async def close_policy_use(self):
        if self.policy_use is not None:
            await self.target.gateway.close_use(self.policy_use)
            self.policy_use = None

    async def request_stop(self):
        self.stop.set()
        if self.reflector_probe is not None:
            await self.reflector_probe.request_stop()
        if self.helper is not None:
            from .protocol import ChannelClosed
            try:
                await self.helper.stop()
            except (ChannelClosed, OSError):
                self.helper.close_commands()

    def process_facts(self, previous):
        if self.helper is None:
            return previous
        events = self.helper.events
        self.forced |= any(event['kind'] == 'force_started' for event in events)
        launched = True if any(event['kind'] == 'child_started' for event in events) else previous.launched
        exited = next((event['data']['exit_code'] for event in reversed(events) if event['kind'] == 'child_exited'), previous.exit_code)
        return replace(previous, launched=launched, exit_code=exited,
            forced=True if self.forced else previous.forced)


def prepare_worker_inputs(prepared, secrets):
    from evolution_lab._manifest_schema import CAMPAIGN_MANIFEST_SCHEMAS
    from evolution_lab.client import _connect, _prepare
    binding = prepared.bundle
    receipt = binding.environment_receipt(binding.execution_environment.digest)
    return _prepare(_connect(binding.manifest_path, CAMPAIGN_MANIFEST_SCHEMAS), CAMPAIGN_MANIFEST_SCHEMAS,
        wheel_paths=[Path(item['path']) for item in receipt['archive_wheels']],
        extra_paths=[binding.launch_path, *[Path(path) for path in receipt['runtime_files']]],
        env=dict(secrets.environment))


def _fresh_core(run: Path):
    """The fresh core projection admission row 7 reads (owner/resume_rules.py)."""
    from .core_raw import FAMILIES, campaign_read
    try:
        return campaign_read(run, families=FAMILIES).status.to_payload()
    except Exception:
        raise OwnerError('campaign_unreadable', 'before_start') from None


def decision_effect(code: str, chosen: str | None, offered: Sequence[str]) -> str | None:
    """The effect Lab writes for an accepted finding.

    An effect the operator chose is written as chosen. Otherwise Lab writes
    ``"default"`` (the first effect the finding offers, the one Lab prints),
    except where Core's ``evo tree authorize`` writes no effect and that means
    the same: a code other than ``run_identity_changed`` whose finding offers a
    single effect. Core resolves a missing effect to ``close`` when offered,
    so a finding with several effects keeps ``"default"``.
    """
    if chosen is not None:
        return chosen
    if code != "run_identity_changed" and len(offered) == 1:
        return None
    return "default"


class ExecutionSupport:
    def _refuse_probe_target(self, target_ref):
        """Refuse before any action when ``target_ref`` names a probe target of this Owner generation.

        A target raised from a probe Bundle serves probe sessions only (spec 2026-10-02 §2.4). The refusal
        holds whether or not that target is still running: ``_targets`` keeps every target of the generation.
        """
        target = self._targets.get(target_ref.id) if isinstance(target_ref, TargetRef) else None
        if (target is not None and target_ref.owner_generation == self.owner_generation
                and target.binding.source_kind == 'probe'):
            raise OwnerError('bundle_probe_only', 'before_start')

    async def start(self, prepared_digest, *, target_ref, secrets, client_ref=None,
                    linked_from=None, baseline='inherit', seed='champion',
                    baseline_import: BaselineImportChoice | None = None,
                    approval: Approval | None = None, request_id: str | None = None):
        from .binding import open_prepared, declared_executor_identity
        from .secrets import bind_execution_secrets
        from .policy import execution_tokens
        from .tasks import failure_for
        from .models import ResourceRef
        self._assert_mutation()
        if (not isinstance(baseline, str) or baseline not in {'inherit', 'fresh'}
                or not isinstance(seed, str) or seed not in {'champion', 'project'}):
            raise OwnerError('invalid_argument', 'before_start')
        if baseline_import is not None:
            if linked_from is not None:
                raise ValueError("linked_from and baseline_import are exclusive")
            if not isinstance(baseline_import, BaselineImportChoice):
                raise OwnerError('invalid_argument', 'before_start')
            from .linked import admit_baseline_source
            baseline_import = await admit_baseline_source(self, baseline_import)
        if linked_from is not None:
            try:
                predecessor = await self._db('operation_status', linked_from)
            except OwnerError as error:
                if error.code in {'not_found', 'invalid_argument'}:
                    raise OwnerError('linked_predecessor_unknown', 'before_start') from None
                raise
            if predecessor.kind != 'campaign':
                raise OwnerError('linked_predecessor_unknown', 'before_start')
            if predecessor.results_state in {'deleted', 'deleting', 'delete_failed'}:
                raise OwnerError('linked_predecessor_deleted', 'before_start')
        self._refuse_probe_target(target_ref)
        target_status = await self.target(target_ref)
        target = self._targets.get(target_ref.id)
        if target_ref.owner_generation != self.owner_generation or target_status.state != 'ready' or target is None:
            raise OwnerError('target_not_running', 'before_start')
        prepared = await asyncio.to_thread(open_prepared, self._storage, prepared_digest)
        if (linked_from is not None or baseline_import is not None) and prepared.kind != 'campaign':
            raise OwnerError('invalid_argument', 'before_start')
        if prepared.bundle.bundle_digest != target_status.bundle_digest or target.secrets is None:
            raise OwnerError('binding_mismatch', 'before_start')
        bound = bind_execution_secrets(
            prepared.bundle, secrets, target_secrets=target.secrets, kind=prepared.kind,
            lab_filled=execution_tokens(prepared.bundle, target, kind=prepared.kind)
        )
        declared = await asyncio.to_thread(
            declared_executor_identity, prepared.bundle, kind=prepared.kind
        )
        operation_id, scope_id = uuid.uuid4().hex, uuid.uuid4().hex
        approval_kwargs = {} if approval is None else dict(approval=approval, request_id=request_id)
        async def admitted(accepted):
            runtime = None
            try:
                self._assert_mutation()
                reservation = await self._db('accept_operation', prepared_digest=prepared_digest,
                    target_ref=target_ref, executor=declared, kind=prepared.kind,
                    operation_id=operation_id, scope_id=scope_id, client_ref=client_ref, **approval_kwargs)
                runtime = OperationRuntime(operation_id, scope_id, prepared, target, bound, self._loop.create_future())
                if linked_from is not None:
                    runtime.linked = {'operation_id': linked_from, 'baseline': baseline, 'seed': seed}
                runtime.baseline_import = baseline_import
                self._operations[operation_id] = runtime
                auto = await self._auto_resume.started(reservation.status, target, secrets)
                if target.gateway is not None:
                    runtime.policy_use = f'operation:{runtime.id}:start:{reservation.status.starts[-1].start}'
                    await target.gateway.open_use(runtime.policy_use)
                accepted.set_result(replace(reservation.status, auto_resume=auto))
                await self._run_operation(runtime, declared)
            except BaseException as error:
                # Identity is assigned before acceptance, so even a failure while
                # returning a committed acceptance has an addressable finalizer.
                failure = failure_for(error, 'before_start')
                if runtime is not None:
                    await runtime.close_policy_use()
                try:
                    value = await self._db('settle_pre_dispatch_failure', operation_id, failure)
                    self._operation_cache[operation_id] = value
                except BaseException:
                    if runtime is not None:
                        await self._operation_failure(runtime, failure)
                if not accepted.done():
                    accepted.set_exception(OwnerError(failure.code, failure.stage))
            finally:
                if runtime is not None:
                    await runtime.close_policy_use()
                    runtime.secrets = None
                    if runtime.reflector_probe is not None:
                        await runtime.reflector_probe.finish_notes_without_worker()
                        runtime.reflector_probe.close_channels()
                    if runtime.helper is not None:
                        runtime.helper.close_commands()
                        runtime.helper.close_events()
                        runtime.helper.close_target_ready()
                    if not runtime.done.done():
                        runtime.done.set_result(None)
                    self._schedule_auto_resume(runtime)
        return await self._admit(admitted)

    async def resume(self, ref, *, target_ref, secrets, accept=(), reason=None, prepared=None,
                     approval: Approval | None = None, request_id: str | None = None,
                     observed_start: int | None = None):
        """Continue a suspended or refused campaign using explicitly named Core findings."""
        conditions = {} if approval is None else dict(approval=approval, request_id=request_id)
        if observed_start is not None:
            conditions['observed_start'] = observed_start
        return await self._resume(ref, target_ref=target_ref, secrets=secrets, initiated_by='human',
                                  cancellation=None, accept=accept, reason=reason, prepared_digest=prepared,
                                  **conditions)

    async def _resume_auto(self, ref, *, target_ref, secrets, cancellation=None):
        return await self._resume(ref, target_ref=target_ref, secrets=secrets, initiated_by='auto_resume', cancellation=cancellation)

    async def _resume(self, ref, *, target_ref, secrets, initiated_by, cancellation,
                      accept=(), reason=None, prepared_digest=None,
                      approval: Approval | None = None, request_id: str | None = None,
                      observed_start: int | None = None):
        from .binding import open_prepared, declared_executor_identity
        from .readers import operation_ref
        from .secrets import bind_execution_secrets
        from .policy import execution_tokens
        from .tasks import failure_for
        self._assert_mutation()
        ref = operation_ref(ref)
        from .models import Accepted
        from evolution.tree import Authorization
        if not isinstance(accept, (tuple, list)) or any(not isinstance(a, Accepted) for a in accept):
            raise OwnerError('invalid_argument', 'before_start')
        if accept and (not isinstance(reason, str) or not reason.strip()):
            raise OwnerError('authorization_reason_required', 'before_start')
        # A newer start may have replaced the refusal or made the campaign busy.
        if observed_start is not None:
            current = await self._db('resume_precheck', ref.id, expected_start=observed_start)
        decisions = []
        if accept:
            record = await self._db('operation_refusal', ref.id)
            for item in accept:
                match = next((f for f in (() if record is None else record.findings)
                              if f.code == item.code and f.instance_digest == item.digest
                              and f.refusal_class == 'authorizable'), None)
                if match is None:
                    raise OwnerError('authorization_unknown_finding', 'before_start')
                if item.effect is not None and item.effect not in match.effects:
                    raise OwnerError('authorization_effect_invalid', 'before_start')
                try:
                    decision = Authorization.new(item.code, item.digest, reason,
                                                 effect=decision_effect(item.code, item.effect, match.effects))
                except ValueError:
                    raise OwnerError('invalid_argument', 'before_start') from None
                default = None if item.effect is not None or not match.effects else match.effects[0]
                decisions.append((decision, default))
        self._refuse_probe_target(target_ref)
        if observed_start is None:
            current = await self._db('resume_precheck', ref.id)
        runtime_now = self._operations.get(ref.id)
        if runtime_now is not None and not runtime_now.done.done():
            raise OwnerError('resource_busy', 'resume')
        # A result read/export/delete in flight would see the start change under it.
        if self._result_io.held(ref.id):
            raise OwnerError('resource_busy', 'resume')
        refused = await self._db('resume_refusal', ref.id)
        run = self.state_directory / 'artifacts' / ref.id / ref.id
        core = None
        if not (refused and current.results_state == 'not_created'):
            core = await asyncio.to_thread(_fresh_core, run)
        # The whole admission verdict, rows 1–8, from the one rule the status shows
        # (ADR-0015 amendments 2026-09-24, 2026-09-29); accept_resume re-checks rows 1–6
        # and, when the target requirement holds, the used targets.
        from . import resume_rules
        verdict = resume_rules.resume_verdict(current, core, refusal=refused)
        resume_rules.raise_for(verdict)
        resume_rules.raise_for_target(verdict, target_ref)
        target_status = await self.target(target_ref)
        target = self._targets.get(target_ref.id)
        if target_ref.owner_generation != self.owner_generation or target_status.state != 'ready' or target is None:
            raise OwnerError('target_not_running', 'before_start')
        prepared = await asyncio.to_thread(open_prepared, self._storage, prepared_digest or current.prepared_digest)
        if prepared.kind != 'campaign':
            raise OwnerError('invalid_argument', 'before_start')
        if prepared.bundle.bundle_digest != target_status.bundle_digest or target.secrets is None:
            raise OwnerError('binding_mismatch', 'before_start')
        await self._db('check_operation_policy', ref.id, target.policy_digest)
        bound = bind_execution_secrets(
            prepared.bundle, secrets, target_secrets=target.secrets, kind=prepared.kind,
            lab_filled=execution_tokens(prepared.bundle, target, kind=prepared.kind))
        declared = await asyncio.to_thread(declared_executor_identity, prepared.bundle, kind=prepared.kind)
        scope_id = uuid.uuid4().hex
        admission_kwargs = {} if approval is None else dict(approval=approval, request_id=request_id)
        if observed_start is not None:
            admission_kwargs['expected_start'] = observed_start
        async def admitted(accepted):
            runtime = None
            try:
                self._assert_mutation()
                if cancellation is not None and cancellation.is_set():
                    raise OwnerError('dispatch_refused', 'before_start')
                if self._result_io.held(ref.id):
                    raise OwnerError('resource_busy', 'resume')
                reservation = await self._db('accept_resume', ref.id, target_ref=target_ref,
                    executor=declared, scope_id=scope_id, fresh_required=verdict.target is not None,
                    # The status always ends with the current start (== operations.start).
                    observed_start=current.starts[-1].start, initiated_by=initiated_by,
                    cancellation=cancellation, prepared_digest=prepared.prepared_digest, **admission_kwargs)
                self._operation_cache.pop(ref.id, None)
                runtime = OperationRuntime(ref.id, scope_id, prepared, target, bound,
                    self._loop.create_future(), mode='retry' if current.results_state == 'not_created' else 'continue')
                self._operations[ref.id] = runtime
                if decisions:
                    from evolution.tree import write_decision
                    import sys
                    for decision, default in decisions:
                        if default is not None:
                            print(f'authorization {decision.code}:{decision.digest}: default means {default}', file=sys.stderr)
                        await asyncio.to_thread(write_decision, run, decision)
                auto = await self._auto_resume.started(reservation.status, target, secrets,
                                                       automatic=initiated_by == 'auto_resume', previous_core=core)
                if target.gateway is not None:
                    runtime.policy_use = f'operation:{runtime.id}:start:{reservation.status.starts[-1].start}'
                    await target.gateway.open_use(runtime.policy_use)
                if cancellation is not None:
                    cancellation.admission_ready.set()
                accepted.set_result(replace(reservation.status, auto_resume=auto))
                await self._run_operation(runtime, declared)
            except BaseException as error:
                failure = failure_for(error, 'before_start')
                if cancellation is not None:
                    cancellation.admission_ready.set()
                if runtime is not None:
                    await runtime.close_policy_use()
                if runtime is None:
                    # Acceptance refused, or cancelled around it. A shielded
                    # accept_resume may still have committed before cancellation
                    # (Owner closing); the next generation treats that start as
                    # orphaned and it needs an explicit cleanup.
                    if not accepted.done():
                        accepted.set_exception(
                            error if isinstance(error, OwnerError)
                            else OwnerError(failure.code, failure.stage))
                    return
                try:
                    value = await self._db('settle_pre_dispatch_failure', ref.id, failure)
                    self._operation_cache[ref.id] = value
                except BaseException:
                    await self._operation_failure(runtime, failure)
                if not accepted.done():
                    accepted.set_exception(OwnerError(failure.code, failure.stage))
            finally:
                if runtime is not None:
                    await runtime.close_policy_use()
                    runtime.secrets = None
                    if runtime.reflector_probe is not None:
                        await runtime.reflector_probe.finish_notes_without_worker()
                        runtime.reflector_probe.close_channels()
                    if runtime.helper is not None:
                        runtime.helper.close_commands()
                        runtime.helper.close_events()
                        runtime.helper.close_target_ready()
                    if not runtime.done.done():
                        runtime.done.set_result(None)
                    self._schedule_auto_resume(runtime)
        return await self._admit(admitted)

    def _schedule_auto_resume(self, runtime):
        if self._stopping:
            return
        from datetime import datetime, timezone
        finished_at = datetime.now(timezone.utc)
        async def finish():
            from .models import ResourceRef
            try:
                value = await self.status(ResourceRef('operation', runtime.id))
                await self._auto_resume.finished(value, runtime, finished_at=finished_at)
            except Exception:
                self._auto_resume.projection_failed(runtime)
        self._hold(finish())

    async def _run_operation(self, runtime, declared):
        from .binding import revalidate_prepared
        from .protocol import HelperHandle
        from .target import matched_health, runtime_config
        from .models import ResourceRef
        from .tasks import failure_for
        probe_finish_started = False
        try:
            fresh = await asyncio.to_thread(
                revalidate_prepared,
                self._storage,
                runtime.prepared.prepared_digest,
                declared,
            )
            binding = fresh.bundle
            target = await self._db('target_status', runtime.target.ref.id)
            if target.state != 'ready' or runtime.target.stop.is_set():
                raise OwnerError('target_not_running', 'before_start')
            await matched_health(binding, runtime_config(self.state_directory, binding, runtime.target.ref,
                scratch=self.scratch_directory), target.boot_id)
            if fresh.kind == "campaign":
                if binding.manifest.workflow.reflector.probe is None:
                    await self._db('begin_reflector_probe', runtime.id, launched_by=None)
                await asyncio.to_thread(prepare_worker_inputs, fresh, runtime.secrets)
                if binding.manifest.workflow.reflector.probe is not None:
                    from .reflector_probe import ReflectorProbe
                    runtime.reflector_probe = ReflectorProbe(self, runtime, binding)
                    await runtime.reflector_probe.prepare()
            if runtime.stop.is_set() or self._stopping:
                raise OwnerError('dispatch_refused', 'before_start')
            if fresh.kind == "campaign":
                runtime.paths = await asyncio.to_thread(
                    create_campaign_paths, self.state_directory, runtime.id, runtime.scope_id,
                    continue_run=runtime.mode != "new",
                )
                from .linked import campaign_manifest
                manifest_path = await campaign_manifest(self, runtime, binding)
                config = campaign_worker_config(
                    operation_id=runtime.id, scope_id=runtime.scope_id,
                    owner_generation=self.owner_generation,
                    bundle_digest=binding.bundle_digest,
                    prepared_digest=fresh.prepared_digest,
                    manifest_path=manifest_path,
                    run_directory=runtime.paths.run_directory,
                    settlement_path=runtime.paths.settlement_path,
                    diagnostic_path=runtime.paths.diagnostic_path,
                    mode=runtime.mode,
                )
                command = campaign_worker_command(
                    binding.execution_environment.executable
                )
                role = "campaign"
            else:
                runtime.paths = await asyncio.to_thread(
                    create_evaluation_paths, self.state_directory, runtime.id,
                    runtime.scope_id,
                )
                config = evaluation_worker_config(
                    operation_id=runtime.id, scope_id=runtime.scope_id,
                    owner_generation=self.owner_generation,
                    bundle_digest=binding.bundle_digest,
                    prepared_digest=fresh.prepared_digest,
                    state_directory=self.state_directory,
                    run_directory=runtime.paths.run_directory,
                    outcome_path=runtime.paths.outcome_path,
                    inputs_path=runtime.paths.inputs_path,
                    settlement_path=runtime.paths.settlement_path,
                    diagnostic_path=runtime.paths.diagnostic_path,
                )
                command = evaluation_worker_command(
                    binding.execution_environment.executable
                )
                role = "evaluation"
            await self._db('mark_helper_possible', runtime.scope_id)
            runtime.helper = HelperHandle.launch(binding.execution_environment.executable,
                runtime.paths.scope_directory, runtime.scope_id, self.owner_generation, ownership=self.ownership)
            await runtime.helper.configure(argv=command,
                env=with_launch_values(runtime.secrets.environment), stop_grace_seconds=fresh.runtime_policy.stop_grace_seconds,
                role=role, child_config=config, cwd=str(runtime.paths.scope_directory))
            await runtime.helper.wait_event('ready')
            if runtime.stop.is_set() or self._stopping:
                await runtime.request_stop()
            else:
                await self._db('mark_dispatch_possible', runtime.id)
                await runtime.helper.go()
                await self._db('mark_operation_running', runtime.id)
            if runtime.reflector_probe is not None and runtime.reflector_probe.launch is not None:
                runtime.reflector_probe.watcher = asyncio.create_task(runtime.reflector_probe.watch())
            while runtime.helper.process.poll() is None:
                if self._closed:
                    return
                if runtime.stop.is_set() or self._stopping:
                    await runtime.request_stop()
                await asyncio.sleep(.02)
            if runtime.reflector_probe is not None:
                probe_finish_started = True
                await runtime.reflector_probe.finish()
            await runtime.close_policy_use()
            await self._finalize_operation(runtime)
        except BaseException as error:
            failure = failure_for(error, 'finalizing')
            if runtime.reflector_probe is not None and not probe_finish_started:
                probe_finish_started = True
                try:
                    await runtime.reflector_probe.finish()
                except BaseException:
                    await self._operation_failure(runtime, failure)
                    return
            if runtime.helper is None:
                raise
            await runtime.request_stop()
            await self._operation_failure(runtime, failure)

    async def _operation_failure(self, runtime, failure):
        from .models import CleanupFacts, Persistence, ResourceRef
        from .readers import project_operation
        await runtime.close_policy_use()
        terminal = None
        try:
            durable = await self._db('operation_status', runtime.id)
            # As in _finalize_operation, readers keep the state they had until the store write has an
            # outcome: a terminal state is only shown with the result of its persistence attempt.
            self._operation_cache.setdefault(runtime.id, durable)
            # A refusal finalization already read is part of the outcome; the durable row may lack it
            # because the write carrying it is the one that failed (ADR-0004).
            terminal = replace(durable, process=runtime.process_facts(durable.process),
                failure=failure, control_state='cleanup_required', target_cleanup=CleanupFacts('unconfirmed', None),
                refusal=durable.refusal if runtime.refusal is None else runtime.refusal,
                core_terminal=durable.core_terminal if runtime.core_terminal is None else runtime.core_terminal)
            terminal = await asyncio.to_thread(
                project_operation,
                self.state_directory,
                terminal,
                scope_directory=(
                    None if runtime.paths is None else runtime.paths.scope_directory
                ),
            )
            if terminal.result is not None:
                terminal = replace(terminal, result=replace(terminal.result, persistence=Persistence('saved')))
            await self._db('mark_operation_cleanup_required', runtime.id)
            await self._db('update_operation', runtime.id, process=terminal.process,
                target_cleanup=terminal.target_cleanup, result=terminal.result, failure=failure,
                refusal=terminal.refusal, core_terminal=terminal.core_terminal, results_state=terminal.results_state)
            self._operation_cache.pop(runtime.id, None)
        except BaseException:
            # The terminal value is published even without a result (a campaign that never produced one);
            # only a result carries the failed persistence.
            value = terminal or self._operation_cache.get(runtime.id)
            if value is not None and value.result is not None:
                from .models import Failure
                value = replace(value,
                    result=replace(value.result, persistence=Persistence('failed', Failure.make('io_error', 'finalizing'))))
            if value is not None:
                self._operation_cache[runtime.id] = value
