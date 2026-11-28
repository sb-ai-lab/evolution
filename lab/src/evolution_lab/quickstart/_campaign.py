"""A campaign from one project, using preparation and Owner's existing execution path."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path

from tempfile import TemporaryDirectory
from typing import Any

from evolution_lab._paths import canonical_root
from evolution_lab.quickstart._errors import QuickstartError
from evolution_lab.quickstart._evaluate import (
    _abort,
    _allocate_run,
    _check_arguments,
    _clean_leftovers,
    _conformance,
    _drop_marker,
    _finished,
    _free_port,
    _grace,
    _load,
    _names,
    _secret_values,
    _start_target,
    _Stop,
    _stop_all,
    _stop_target,
    _wheel_pins,
    installed_versions,
    pair_label,
)
from evolution_lab.quickstart._resolve import Resolution, _resolve_project

MANIFEST = "manifest.json"
LAUNCH = "campaign-launch.json"
SUMMARY_SCHEMA = "lab-campaign-summary/1"


class CampaignResolution(Resolution):
    def payload(self) -> dict[str, Any]:
        return {**super().payload(), "schema": "lab-campaign-resolved/1"}


@dataclass(frozen=True)
class CampaignResult:
    exit_code: int
    run_dir: Path
    summary: dict[str, Any]
    reason: str | None
    resolution: CampaignResolution | None = None


def summary_schema() -> dict[str, Any]:
    return json.loads((resources.files("evolution_lab.quickstart") /
                       "lab-campaign-summary-1.schema.json").read_text(encoding="utf-8"))


def _campaign_verdict(summary: Mapping[str, Any]) -> tuple[int, str | None]:
    row = summary.get("campaign")
    target = summary.get("final_target")
    if summary["blocker_count"] or not target or target["state"] != "stopped" or target["complete"] is not True:
        return 3, "cleanup not confirmed"
    if row is None:
        return 3, "the campaign did not start"
    if row.get("refusal") is not None or row.get("failure") == "core_refused":
        return 5, "Core refused the campaign"
    if row.get("failure") or row["persistence_state"] != "saved" or row["cleanup_complete"] is not True:
        return 3, "campaign execution or persistence failed"
    if row.get("suspended"):
        return 4, row.get("suspension_reason") or "the campaign is suspended"
    terminal_exit = {"completed": 0, "stopped": 3, "budget_exhausted": 4}
    if row["outcome"] in terminal_exit and row["process_exit"] == terminal_exit[row["outcome"]]:
        return (0, None) if row["promoted"] else (1, "the campaign finished without a promotion")
    return 3, "Core did not report a finished or suspended campaign"


def _campaign_facts(status) -> dict[str, Any]:
    from evolution_lab._suspension import last_suspension
    from evolution_lab.owner.resume_rules import core_suspended

    result = status.result
    core = result.to_payload()["core"] if result is not None else None
    core = core if isinstance(core, dict) else {}
    final = (core.get("final_champion") or {}).get("version")
    outcome = core.get("campaign_outcome") or {}
    suspension = last_suspension(core) or {}
    return {"operation_id": status.id, "control_state": status.control_state,
            "outcome": outcome.get("outcome"), "reason": outcome.get("reason"),
            "suspended": core_suspended(core), "suspension_reason": suspension.get("reason"),
            "final_champion": final,
            "promoted": any(row.get("outcome") == "promoted" for row in core.get("rounds", ())),
            "refusal": None if status.refusal is None else status.refusal.to_payload(),
            "failure": None if status.failure is None else status.failure.code,
            "process_exit": None if result is None else result.process.exit_code,
            "cleanup_complete": status.target_cleanup.complete,
            "persistence_state": None if result is None else result.persistence.state,
            "report_state": None, "diff_state": None}


async def campaign(project: Path, *, wheels: Sequence[Path], python: Path | None = None,
                   environ: Mapping[str, str] | None = None, continue_run=False, accept=(),
                   reason=None, prepared=None, reprepare=False, linked_from=None, baseline=None,
                   baseline_from=None, seed='champion', on_baseline_warning=None) -> CampaignResult:
    from evolution_lab.quickstart._readout import redact
    where: dict[str, Any] = {"state": None, "run_dir": None, "secrets": {}}
    try:
        if continue_run and linked_from is not None:
            raise QuickstartError('invalid_argument', '--continue and --from cannot be combined')
        if baseline_from is not None:
            if not isinstance(baseline_from, str) or not baseline_from.strip():
                raise QuickstartError("invalid_argument", "--baseline-from requires a run or evaluation operation id")
            if linked_from is not None or baseline is not None:
                raise QuickstartError("invalid_argument", "--baseline-from is exclusive with --from and --baseline")
        if continue_run and (baseline is not None or baseline_from is not None):
            raise QuickstartError("invalid_argument", "--continue uses the saved baseline choice")
        if baseline not in (None, "fresh", "inherit"):
            raise QuickstartError("invalid_argument", "--baseline must be fresh or inherit")
        if baseline == "inherit" and linked_from is None:
            raise QuickstartError("invalid_argument", "--baseline inherit requires --from. Use --baseline-from RUN for an evaluation.")
        if (accept or reason is not None or prepared is not None or reprepare) and not continue_run:
            raise QuickstartError('invalid_argument', 'acceptance and prepared inputs require --continue')
        if reprepare and prepared is not None:
            raise QuickstartError('invalid_argument', '--reprepare and --prepared cannot be combined')
        if accept and (not isinstance(reason, str) or not reason.strip()):
            raise QuickstartError('authorization_reason_required', 'accepting a Core finding requires --reason')
        action = dict(continue_run=continue_run, accept=accept, reason=reason, prepared=prepared, reprepare=reprepare,
                      linked_from=linked_from, baseline=baseline, baseline_from=baseline_from,
                      seed=seed, on_baseline_warning=on_baseline_warning)
        return await _campaign(project, wheels=wheels, python=python, environ=environ, where=where, action=action)
    except QuickstartError:
        raise
    except asyncio.CancelledError:
        if where["state"] is not None:
            print(f"lab campaign: interrupted; the state is in {where['state']}", file=sys.stderr, flush=True)
        raise
    except Exception as error:
        raise QuickstartError(
            "unexpected_error", "lab campaign stopped on an unexpected error: "
            + redact(f"{type(error).__name__}: {error}", where["secrets"]), exit_code=3,
            state=None if where["state"] is None else str(where["state"]),
            run_dir=None if where["run_dir"] is None else str(where["run_dir"])) from error


async def _campaign(project, *, wheels, python, environ, where, action):
    from evolution_lab.preparation import EnvironmentInput, PreparationError, describe_environment

    try:
        project = canonical_root(project)
    except ValueError:
        raise QuickstartError('project_invalid', 'invalid project directory') from None
    if project.is_symlink():
        raise QuickstartError('project_invalid', f'{project} is a symlinked project directory')
    if not project.is_dir():
        raise QuickstartError("project_invalid", f"{project} is not a project directory")
    wheels = tuple(Path(path).absolute() for path in wheels)
    if not wheels:
        raise QuickstartError("wheels_missing", "pass the Lab and Core wheels with --wheel (just campaign does)")
    _check_arguments(wheels, None if python is None else Path(python).absolute())
    if action['continue_run'] and not action['reprepare']:
        try:
            await asyncio.to_thread(describe_environment, EnvironmentInput(Path(sys.executable).absolute(), wheels))
        except PreparationError as error:
            raise QuickstartError(error.code, f'the Python environment check failed: {error}', exit_code=3) from None
        return await _continue_project(project, wheels=wheels, environ=environ, where=where, action=action, python=python)
    launch_name = LAUNCH if (project / LAUNCH).is_file() else "launch.json"
    launch = _load(project, launch_name)
    environ = os.environ if environ is None else environ
    target_names = _names(launch.get("secret_names"), launch_name, "secret_names")
    target_secrets = _secret_values(environ, target_names, "target", launch_name)
    env = launch.get("env")
    if not isinstance(env, dict):
        raise QuickstartError("project_invalid", f"{launch_name}:env must be an object", file=launch_name, field="env")
    execution = Path(sys.executable).absolute()
    python = execution if python is None else Path(python).absolute()
    inputs = (EnvironmentInput(execution, wheels),)
    if python != execution:
        inputs += (EnvironmentInput(python, wheels),)
    try:
        environments = [await asyncio.to_thread(describe_environment, item) for item in inputs]
    except PreparationError as error:
        raise QuickstartError(error.code, f"the Python environment check failed: {error}", exit_code=3) from None
    versions = installed_versions()
    pair = pair_label(*versions)
    pins = await asyncio.to_thread(_wheel_pins, wheels)
    # Resolve before state admission: Core's public inventory sees concrete inputs,
    # including chat/Grouper credentials. Scratch disappears even on missing secrets.
    with TemporaryDirectory(prefix="lab-campaign-") as scratch:
        staged = Path(scratch) / "source"
        resolved = await asyncio.to_thread(
            _resolve_project, project, staged, environment=environments[-1], port=_free_port(),
            lab_version=versions[0], core_version=versions[1], pair=pair, python=str(python), wheels=pins,
            launch_name=launch_name, input_name=MANIFEST, role_interpreters=("scorer", "editor", "reflector"))
        from evolution.tree import ManifestError, load_manifest_v2, manifest_dependencies
        try:
            manifest = load_manifest_v2(staged / MANIFEST)
            inventory = manifest_dependencies(manifest)
        except ManifestError as error:
            raise QuickstartError("project_invalid", f"{MANIFEST}: {error}", file=MANIFEST) from None
        role_secrets = {}
        for item in inventory.environment:
            if item.name not in env:
                role_secrets.update(_secret_values(environ, [item.name], item.role, MANIFEST))
        secrets = {**target_secrets, **role_secrets}
        where["secrets"] = secrets
        return await _execute_project(project, staged, resolved, launch_name, env, wheels, python, inputs,
                                      versions, pair, target_secrets, role_secrets, secrets, where, action=action,
                                      manifest_schema=manifest.schema)


async def _execute_project(project, staged, resolved, launch_name, env, wheels, python, inputs,
                           versions, pair, target_secrets, role_secrets, secrets, where, action, manifest_schema):
    from evolution_lab.owner import Owner, OwnerError
    from evolution_lab.quickstart._baseline_source import select_baseline
    from evolution_lab.preparation import (
        CampaignSource,
        PreparationError,
        PreparationStore,
        SourceRoot,
    )
    from evolution_lab.quickstart._readout import write_campaign_files

    lab_dir = project / ".lab"
    state, runs = lab_dir / f"state-{pair}", lab_dir / "runs"
    where["state"] = state
    lab_dir.mkdir(mode=0o700, exist_ok=True)
    async with Owner.open(state) as owner:
        cleaned = await _clean_leftovers(owner, state, runs)
        warnings = []
        if action['continue_run']:
            previous = _previous_campaign(runs, state)
            action = {**action, 'operation_id': previous['campaign']['operation_id']}
            baseline = previous.get("baseline")
        else:
            choice, baseline, warnings = await select_baseline(
                owner, runs, schema=manifest_schema, baseline=action["baseline"],
                baseline_from=action["baseline_from"], linked_from=action["linked_from"],
            )
            action = {**action, "baseline_import": choice}
            warn = action["on_baseline_warning"]
            if warn is not None:
                for warning in warnings:
                    warn(warning)
        number, run_dir = _allocate_run(runs)
        where["run_dir"] = run_dir
        source = run_dir / "source"
        try:
            await asyncio.to_thread(shutil.copytree, staged, source, symlinks=True)
            # The private preflight copy is no longer needed during execution.
            await asyncio.to_thread(shutil.rmtree, staged)
            resolution = CampaignResolution(**vars(replace(resolved, run_source=source)))
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        launch = _load(source, launch_name)
        summary = {"schema": SUMMARY_SCHEMA, "exit_code": 3, "reason": None, "run": number,
                   "pair": pair, "lab_version": versions[0], "core_version": versions[1], "state": str(state),
                   "environment": {"python": str(python), "target_environment": _load(source, launch_name)["environment_ref"]},
                   "resolved": resolution.payload(), "cleaned_leftovers": cleaned, "prepared": None,
                   "conformance": None, "conformance_declared": launch.get("conformance") is not None,
                   "campaign": None, "final_target": None, "blocker_count": 0, "error": None,
                   "next_command": shlex.join(["lab", "campaign", str(project),
                                               *(arg for wheel in wheels for arg in ("--wheel", str(wheel))),
                                               "--python", str(python)])}
        if baseline is not None:
            summary["baseline"] = baseline
        log = [f"run {number:03d}: state {state}", *warnings]
        operation, targets = [], []
        try:
            store = await asyncio.to_thread(PreparationStore, state)
            bundle = await asyncio.to_thread(
                store.create_bundle, source=CampaignSource(source / MANIFEST, source / launch_name),
                environments=inputs, env=dict(env), secret_names=sorted(secrets), roots=(SourceRoot("project", source),))
            prepared = await asyncio.to_thread(store.prepare_campaign, bundle.digest)
            summary["prepared"] = {"kind": "campaign", "digest": prepared.digest, "bundle": bundle.digest}
            await _run_campaign(owner, bundle, prepared, launch, target_secrets, role_secrets,
                                summary, targets, operation, log, action=action)
            summary['next_command'] = _next_command(
                project, wheels, python, summary['campaign'], baseline_from=action["baseline_from"],
                continuing=action["continue_run"],
            )
            exit_code, reason = _campaign_verdict(summary)
        except PreparationError as error:
            summary["error"] = {"code": error.code, "message": str(error)}
            exit_code, reason = 2, f"preparation refused: {error}"
        except asyncio.CancelledError:
            await asyncio.shield(_abort(owner, operation, targets, _grace(launch)))
            raise
        except (_Stop, OwnerError) as error:
            await asyncio.shield(_stop_all(owner, operation, targets, _grace(launch), (OwnerError, TimeoutError)))
            if isinstance(error, OwnerError) and error.code in {"baseline_source_unknown", "baseline_source_deleted"}:
                summary["error"] = {"code": error.code, "message": str(error)}
                exit_code = 2
            else:
                exit_code = 3
            reason = str(error)
        except Exception:
            await asyncio.shield(_stop_all(owner, operation, targets, _grace(launch), (Exception,)))
            raise
        summary["blocker_count"] = len((await owner.health()).blockers)
        if summary["blocker_count"]:
            exit_code, reason = 3, "cleanup not confirmed"
        summary.update(exit_code=exit_code, reason=reason)
        await write_campaign_files(owner, operation[-1] if operation else None, run_dir,
                                   summary, log=log, secrets=secrets)
        if not summary["blocker_count"]:
            _drop_marker(run_dir)
        from evolution_lab.quickstart._readout import redact_document
        safe = redact_document(summary, summary_schema(), secrets)
        return CampaignResult(exit_code, run_dir, safe, safe["reason"], resolution)


async def _run_campaign(owner, bundle, prepared, launch, target_secrets, role_secrets,
                        summary, targets, operation, log, *, action):
    from evolution_lab.owner import ResourceRef
    connection = await owner.bind(bundle.digest)
    if launch.get("conformance") is not None:
        log.append("conformance: starting the Target Service")
        target = await _start_target(owner, connection, target_secrets, targets)
        try:
            await _conformance(owner, connection, target, summary)
        finally:
            row = summary.get("conformance")
            if row is not None:
                summary["conformance"] = {
                    key: row[key] for key in ("state", "failure", "core_ok", "failed_checks", "target_state")}
                summary["conformance"].update(
                    cleanup_complete=(row.get("cleanup") or {}).get("complete"),
                    target_cleanup_complete=(row.get("target_cleanup") or {}).get("complete"),
                    persistence_state=(row.get("persistence") or {}).get("state"))
    log.append("campaign: starting a fresh Target Service")
    target = await _start_target(owner, connection, target_secrets, targets)
    try:
        if action['continue_run']:
            accepted = await owner.resume(ResourceRef('operation', action['operation_id']), target_ref=target,
                secrets=dict(role_secrets), accept=action['accept'], reason=action['reason'], prepared=prepared.digest)
        else:
            conditions = ({} if action['linked_from'] is None else dict(linked_from=action['linked_from'],
                           baseline=action['baseline'] or 'inherit', seed=action['seed']))
            if action.get("baseline_import") is not None:
                conditions["baseline_import"] = action["baseline_import"]
            accepted = await owner.start(prepared.digest, target_ref=target, secrets=dict(role_secrets), **conditions)
        ref = ResourceRef("operation", accepted.id)
        operation.append(ref)
        status = await _finished(owner, ref)
        summary["campaign"] = _campaign_facts(status)
        log.append(f"campaign: {summary['campaign']['outcome'] or status.control_state}")
        if status.control_state == "cleanup_required":
            await owner.wait(await owner.cleanup(ref))
            summary['campaign'] = _campaign_facts(await owner.status(ref))
    finally:
        await _stop_target(owner, target)
    after = await owner.target(target)
    summary["final_target"] = {"state": after.state, "complete": after.cleanup.complete}
    summary["blocker_count"] = len((await owner.health()).blockers)


def _next_command(project, wheels, python, row, *, baseline_from=None, continuing=False):
    args = ['lab', 'campaign', str(project), *(arg for w in wheels for arg in ('--wheel', str(w)))]
    if python is not None:
        args += ['--python', str(python)]
    code = ((row or {}).get("refusal") or {}).get("code")
    if code == "baseline_source_unavailable":
        args += ["--baseline", "fresh"]
    elif code == "baseline_import_inputs_changed":
        if continuing:
            args += ["--continue"]
        elif baseline_from is not None:
            args += ["--baseline-from", baseline_from]
    elif row and (row['suspended'] or row['refusal'] is not None):
        args += ['--continue']
    elif row and row['outcome'] is not None:
        args += ['--from', row['operation_id']]
    return shlex.join(args)


def _previous_campaign(runs, state):
    summaries = [p for p in runs.glob('[0-9]*/summary.json') if p.parent.name.isdigit()]
    for path in sorted(summaries, key=lambda p: int(p.parent.name), reverse=True):
        document = json.loads(path.read_text())
        if (document.get('schema') == SUMMARY_SCHEMA and document.get('campaign')
                and document.get('state') == str(state) and state.is_dir()):
            return document
    raise QuickstartError('campaign_not_found', 'this project has no campaign to continue')


async def _continue_project(project, *, wheels, environ, where, action, python=None):
    """Resume with saved inputs; editing a project does not silently replace them."""
    from evolution.tree import manifest_dependencies
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.binding import open_prepared
    from evolution_lab.preparation.storage import PreparationStorage
    from evolution_lab.quickstart._readout import write_campaign_files, redact_document
    versions = installed_versions()
    pair = pair_label(*versions)
    lab_dir = project / '.lab'
    state, runs = lab_dir / f'state-{pair}', lab_dir / 'runs'
    previous = _previous_campaign(runs, state)
    operation_id = previous['campaign']['operation_id']
    action = {**action, 'operation_id': operation_id}
    environ = os.environ if environ is None else environ
    where['state'] = state
    async with Owner.open(state) as owner:
        cleaned = await _clean_leftovers(owner, state, runs)
        current = await owner.status(ResourceRef('operation', operation_id))
        digest = action['prepared'] or current.prepared_digest
        storage = PreparationStorage(state)
        binding = await asyncio.to_thread(open_prepared, storage, digest)
        target_python = Path(binding.bundle.target_environment.executable)
        if python is not None and Path(python).absolute() != target_python:
            raise QuickstartError('argument_invalid', '--python differs from the saved target environment; select --prepared instead')
        prepared = await asyncio.to_thread(storage.get_prepared, digest)
        bundle = await asyncio.to_thread(storage.get_bundle, binding.bundle.bundle_digest)
        launch = dict(binding.bundle.launch)
        target_secrets = _secret_values(environ, launch['secret_names'], 'target', LAUNCH)
        role_names = {item.name for item in manifest_dependencies(binding.bundle.manifest).environment
                      if item.name not in binding.bundle.base_environment}
        role_secrets = _secret_values(environ, sorted(role_names), 'roles', MANIFEST)
        secrets = {**target_secrets, **role_secrets}
        where['secrets'] = secrets
        number, run_dir = _allocate_run(runs)
        where['run_dir'] = run_dir
        receipt = binding.bundle.environment_receipt(binding.bundle.execution_environment.digest)
        actual_wheels = [Path(item['path']) for item in receipt['archive_wheels']]
        resolution = CampaignResolution({}, (), *versions, pair=pair, run_source=binding.bundle.bundle_path,
            python=str(target_python),
            wheels=tuple(await asyncio.to_thread(_wheel_pins, actual_wheels)))
        summary = {**previous, 'run': number, 'cleaned_leftovers': cleaned, 'campaign': None,
            'environment': {'python': str(target_python),
                            'target_environment': binding.bundle.target_environment.digest},
            'resolved': resolution.payload(),
            'prepared': {'kind': 'campaign', 'digest': digest, 'bundle': bundle.digest},
            'conformance': None, 'final_target': None, 'blocker_count': 0, 'error': None,
            'next_command': _next_command(project, wheels, target_python, None)}
        operation, targets, log = [], [], [f'continue {operation_id}: prepared {digest}']
        try:
            await _run_campaign(owner, bundle, prepared, launch, target_secrets, role_secrets,
                                summary, targets, operation, log, action=action)
            code, reason = _campaign_verdict(summary)
            summary['next_command'] = _next_command(project, wheels, target_python, summary['campaign'], continuing=True)
        except asyncio.CancelledError:
            await asyncio.shield(_abort(owner, operation, targets, _grace(launch)))
            raise
        except (OwnerError, _Stop) as error:
            await asyncio.shield(_stop_all(owner, operation, targets, _grace(launch), (OwnerError, TimeoutError)))
            summary['error'] = {'code': getattr(error, 'code', 'campaign_failed'), 'message': str(error)}
            code, reason = 3, str(error)
        except BaseException:
            await asyncio.shield(_stop_all(owner, operation, targets, _grace(launch), (Exception,)))
            raise
        summary.update(exit_code=code, reason=reason, blocker_count=len((await owner.health()).blockers))
        await write_campaign_files(owner, operation[-1] if operation else None, run_dir, summary, log=log, secrets=secrets)
        if not summary['blocker_count']:
            _drop_marker(run_dir)
        safe = redact_document(summary, summary_schema(), secrets)
        return CampaignResult(code, run_dir, safe, safe['reason'], resolution)


async def trace_project(project: Path, *, out: Path, secrets: Mapping[str, str], disclose_private: bool,
                        operation=None, measurements=(), max_field_bytes=65536, environ=None):
    """Read saved campaign inputs, resolving declared secrets as continuation does."""
    from evolution.tree import manifest_dependencies
    from evolution_lab.owner import Owner, OwnerError, ResourceRef
    from evolution_lab.owner.binding import open_prepared
    if disclose_private is not True:
        raise QuickstartError('private_disclosure_required', 'pass --disclose-private to export case data', exit_code=2)
    project = Path(project).absolute()
    state = project / '.lab' / f'state-{pair_label(*installed_versions())}'
    if not state.is_dir():
        raise QuickstartError('campaign_not_found', 'this project has no campaign state')
    if operation is None:
        operation = _previous_campaign(project / '.lab/runs', state)['campaign']['operation_id']
    try:
        async with Owner.open(state) as owner:
            ref = ResourceRef('operation', operation)
            current = await owner.status(ref)
            if current.kind != 'campaign':
                raise OwnerError('trace_not_a_campaign', 'export')
            binding = await asyncio.to_thread(open_prepared, owner._storage, current.prepared_digest)
            bundle = binding.bundle
            # Resolve the saved launch/role names, never today's editable project.
            # Explicit file/FD values override the caller environment.
            available = {**bundle.base_environment,
                         **(os.environ if environ is None else environ), **secrets}
            target_values = _secret_values(available, bundle.launch['secret_names'], 'target', LAUNCH)
            role_names = {item.name for item in manifest_dependencies(bundle.manifest).environment}
            role_values = _secret_values(available, sorted(role_names), 'roles', MANIFEST)
            values = {**target_values, **role_values}
            return await owner.trace(ref, out=out, secrets=values,
                disclose_private=disclose_private, measurements=measurements, max_field_bytes=max_field_bytes)
    except OwnerError as error:
        raise QuickstartError(error.code, str(error), exit_code=3, state=str(state)) from None
