"""Explicit public core conformance with one helper and mandatory target stop."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from evolution_lab.preparation.models import PreparationError, RuntimePolicy

from .binding import (
    declared_executor_identity,
    open_bundle,
    revalidate_environment,
    revalidate_executor_identity,
)
from .execution import OperationRuntime, _fresh_directory, _open_child
from .models import CleanupFacts, Failure, OwnerError, ProcessFacts, ResourceRef, TaskResult
from .protocol import HelperHandle
from .receipts import (
    ReceiptError,
    open_directory_nofollow,
    read_cleanup_receipt,
    read_json,
    validate_id,
)
from .tasks import failure_for

from evolution_lab.launch_env import with_launch_values


CONFORMANCE_CONFIG_SCHEMA = "lab-conformance-worker-config/1"
MAX_CONFORMANCE_REPORT_BYTES = 1024 * 1024
_DECLARATION = {"kind": "core-session-public/1"}
_CONFIG_KEYS = {
    "schema",
    "task_id",
    "scope_id",
    "owner_generation",
    "manifest_path",
    "allowed_artifact_root",
    "workdir",
    "report_path",
}


@dataclass(frozen=True, slots=True)
class ConformancePaths:
    scope_directory: Path
    workdir: Path
    report_path: Path
    stdout_path: Path
    stderr_path: Path


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("invalid conformance path")
    text = os.fspath(value)
    path = Path(text)
    if "\0" in text or not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid conformance path")
    return path


def create_conformance_paths(state_root: Path, task_id: str, scope_id: str) -> ConformancePaths:
    """Create only the private helper scope; core creates its transient workdir."""
    try:
        validate_id(task_id)
        validate_id(scope_id)
        root = Path(state_root).absolute()
        root_fd = open_directory_nofollow(root)
        try:
            scopes_fd = _open_child(root_fd, "scopes")
            artifacts_fd = _open_child(root_fd, "artifacts")
            try:
                workdir_name = f".conformance-{task_id}"
                for descriptor, name in (
                    (scopes_fd, scope_id),
                    (artifacts_fd, workdir_name),
                ):
                    try:
                        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise OwnerError("path_conflict", "before_start")
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
    return ConformancePaths(
        scope,
        root / "artifacts" / f".conformance-{task_id}",
        scope / "conformance-report.json",
        scope / "stdout.log",
        scope / "stderr.log",
    )


def conformance_worker_config(
    *, task_id: str, scope_id: str, owner_generation: str,
    manifest_path: Path, allowed_artifact_root: Path,
    workdir: Path, report_path: Path,
) -> dict[str, Any]:
    return validate_conformance_worker_config({
        "schema": CONFORMANCE_CONFIG_SCHEMA,
        "task_id": task_id,
        "scope_id": scope_id,
        "owner_generation": owner_generation,
        "manifest_path": str(manifest_path),
        "allowed_artifact_root": str(allowed_artifact_root),
        "workdir": str(workdir),
        "report_path": str(report_path),
    })


def validate_conformance_worker_config(value: Any) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
            raise ValueError("invalid conformance worker configuration")
        if value["schema"] != CONFORMANCE_CONFIG_SCHEMA:
            raise ValueError("invalid conformance worker schema")
        for name in ("task_id", "scope_id", "owner_generation"):
            validate_id(value[name])
        manifest = _absolute_path(value["manifest_path"])
        allowed = _absolute_path(value["allowed_artifact_root"])
        workdir = _absolute_path(value["workdir"])
        report = _absolute_path(value["report_path"])
        if workdir.parent != allowed or report.is_relative_to(allowed):
            raise ValueError("invalid conformance path boundary")
        if not manifest.is_file() or not allowed.is_dir() or not report.parent.is_dir():
            raise ValueError("missing conformance input")
        if workdir.exists() or report.exists():
            raise ValueError("occupied conformance output")
        return dict(value)
    except (OSError, ReceiptError, TypeError):
        raise ValueError("invalid conformance worker configuration") from None


def conformance_worker_command(executable: str) -> list[str]:
    if not isinstance(executable, str) or not os.path.isabs(executable):
        raise OwnerError("environment_invalid", "before_start")
    return [executable, "-I", "-m", "evolution_lab.owner._conformance_worker"]


def _report_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version", "ok", "checks", "leaked", "health", "capabilities", "duration_ms"
    }:
        raise ValueError("invalid conformance report")
    if value["version"] != "conformance/v2" or type(value["ok"]) is not bool:
        raise ValueError("invalid conformance report")
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError("invalid conformance report")
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"name", "ok", "expected", "got", "suite"}:
            raise ValueError("invalid conformance report")
        if (
            not all(isinstance(check[name], str) and check[name] for name in ("name", "expected", "got"))
            or type(check["ok"]) is not bool
            or check["suite"] not in {"G", "W"}
        ):
            raise ValueError("invalid conformance report")
    if not isinstance(value["leaked"], list) or any(not isinstance(item, str) for item in value["leaked"]):
        raise ValueError("invalid conformance report")
    for name in ("health", "capabilities"):
        if value[name] is not None and not isinstance(value[name], dict):
            raise ValueError("invalid conformance report")
    if isinstance(value["duration_ms"], bool) or not isinstance(value["duration_ms"], int) or value["duration_ms"] < 0:
        raise ValueError("invalid conformance report")
    return value


def read_conformance_report(path: Path) -> dict[str, Any]:
    return _report_payload(read_json(path, max_bytes=MAX_CONFORMANCE_REPORT_BYTES))


def project_conformance_task(status, *, scope_directory: Path | None):
    """Overlay a private report on DB-safe conformance task facts."""
    if status.action != "conformance" or status.result is None:
        return status
    prior = status.result.payload
    report = prior.get("core_report") if isinstance(prior, Mapping) else None
    if report is None and scope_directory is not None:
        try:
            report = read_conformance_report(
                Path(scope_directory) / "conformance-report.json"
            )
        except (OSError, ValueError):
            pass
    payload = dict(prior)
    payload["core_report"] = report
    return replace(status, result=TaskResult("conformance", payload))


class ConformanceSupport:
    async def conformance(self, connection, *, target_ref, runtime_policy=None):
        self._assert_mutation()
        binding = self._connection_binding(connection)
        if binding.source_kind == "probe":
            # A probe Bundle declares no conformance; its target serves probe sessions only (spec 2026-10-02 §2.4).
            raise OwnerError("bundle_probe_only", "before_start")
        target_status = await self.target(target_ref)
        target = self._targets.get(target_ref.id)
        if (
            target_ref.owner_generation != self.owner_generation
            or target_status.state != "ready"
            or target is None
        ):
            raise OwnerError("target_not_running", "conformance")
        if target_status.bundle_digest != binding.bundle_digest:
            raise OwnerError("binding_mismatch", "conformance")
        if dict(binding.launch.get("conformance") or {}) != _DECLARATION:
            raise OwnerError("conformance_unavailable", "conformance")
        if runtime_policy is None:
            policy = RuntimePolicy(binding.launch["stop_grace_seconds"], 30)
        elif isinstance(runtime_policy, RuntimePolicy):
            policy = runtime_policy
        else:
            try:
                policy = RuntimePolicy.from_payload(runtime_policy, "prepare")
            except (PreparationError, TypeError):
                raise OwnerError("invalid_argument", "conformance") from None
        from .secrets import bind_conformance_secrets
        bound = bind_conformance_secrets(binding)
        declared = await asyncio.to_thread(
            declared_executor_identity, binding, kind="conformance"
        )

        async def admitted(accepted):
            runtime = None
            try:
                self._assert_mutation()
                reservation = await self._db(
                    "accept_task", action="conformance",
                    ref=ResourceRef("target", target_ref.id),
                    reserve_target_use=True, reserve_execution=True,
                )
                entry = self._register_task(
                    reservation.task_ref, "conformance", ResourceRef("target", target_ref.id)
                )
                runtime = OperationRuntime(
                    entry.ref.id, reservation.scope_id, binding, target, bound,
                    self._loop.create_future(),
                )
                runtime.entry = entry
                runtime.runtime_policy = policy
                runtime.dispatch_possible = False
                runtime.helper_possible = False
                self._conformance[entry.ref.id] = runtime
                accepted.set_result(entry.ref)
                await self._run_conformance(runtime, declared)
            except BaseException as error:
                if runtime is not None:
                    await self._finalize_conformance(runtime, failure_for(error, "conformance"))
                elif not accepted.done():
                    accepted.set_exception(OwnerError(failure_for(error, "before_start").code, "before_start"))
            finally:
                if runtime is not None:
                    runtime.secrets = None
                    if runtime.helper is not None:
                        runtime.helper.close_commands()
                        runtime.helper.close_events()
                        runtime.helper.close_target_ready()
                    if not runtime.done.done():
                        runtime.done.set_result(None)

        return await self._admit(admitted)

    async def _run_conformance(self, runtime, declared):
        entry = runtime.entry
        failure = None
        try:
            await self._db("task_running", entry.ref.id)
            fresh = await asyncio.to_thread(
                open_bundle, self._storage, runtime.prepared.bundle_digest,
                stage="before_start",
            )
            if fresh.bundle_digest != runtime.prepared.bundle_digest:
                raise OwnerError("digest_mismatch", "before_start")
            if dict(fresh.launch.get("conformance") or {}) != _DECLARATION:
                raise OwnerError("conformance_unavailable", "conformance")
            await asyncio.to_thread(
                revalidate_environment, fresh, fresh.execution_environment.digest,
                stage="before_start",
            )
            await asyncio.to_thread(
                revalidate_executor_identity, fresh, declared, stage="before_start"
            )
            target = await self._db("target_status", runtime.target.ref.id)
            if target.state != "ready" or runtime.target.stop.is_set():
                raise OwnerError("target_not_running", "before_start")
            if entry.stop.is_set() or self._stopping:
                raise OwnerError("dispatch_refused", "conformance")
            runtime.paths = await asyncio.to_thread(
                create_conformance_paths, self.state_directory, runtime.id, runtime.scope_id
            )
            await self._db("mark_helper_possible", runtime.scope_id)
            runtime.helper_possible = True
            runtime.helper = HelperHandle.launch(
                fresh.execution_environment.executable,
                runtime.paths.scope_directory,
                runtime.scope_id,
                self.owner_generation,
                ownership=self.ownership,
            )
            config = conformance_worker_config(
                task_id=runtime.id,
                scope_id=runtime.scope_id,
                owner_generation=self.owner_generation,
                manifest_path=fresh.manifest_path,
                allowed_artifact_root=self.state_directory / "artifacts",
                workdir=runtime.paths.workdir,
                report_path=runtime.paths.report_path,
            )
            await runtime.helper.configure(
                argv=conformance_worker_command(fresh.execution_environment.executable),
                env=with_launch_values(runtime.secrets.environment),
                stop_grace_seconds=runtime.runtime_policy.stop_grace_seconds,
                role="conformance", child_config=config,
                cwd=str(runtime.paths.scope_directory),
            )
            await runtime.helper.wait_event("ready")
            if entry.stop.is_set() or self._stopping:
                await runtime.request_stop()
            else:
                await self._db("mark_task_dispatch_possible", entry.ref.id)
                runtime.dispatch_possible = True
                if entry.stop.is_set() or self._stopping:
                    await runtime.request_stop()
                else:
                    await runtime.helper.go()
            cleanup_deadline = None
            while runtime.helper.process.poll() is None:
                if self._closed:
                    return
                if entry.stop.is_set() or self._stopping:
                    await runtime.request_stop()
                    if cleanup_deadline is None:
                        cleanup_deadline = self._loop.time() + runtime.runtime_policy.cleanup_wait_seconds
                if cleanup_deadline is not None and self._loop.time() >= cleanup_deadline:
                    failure = Failure.make("cleanup_unconfirmed", "finalizing")
                    break
                await asyncio.sleep(.02)
        except BaseException as error:
            failure = failure_for(error, "conformance")
            if runtime.helper is not None:
                await runtime.request_stop()
                deadline = self._loop.time() + runtime.runtime_policy.cleanup_wait_seconds
                while runtime.helper.process.poll() is None and self._loop.time() < deadline:
                    await asyncio.sleep(.02)
        await self._finalize_conformance(runtime, failure)

    async def _finalize_conformance(self, runtime, failure):
        entry = runtime.entry
        if entry.done.done():
            return
        process = runtime.process_facts(ProcessFacts())
        helper_cleanup = CleanupFacts("unconfirmed", None)
        persistence_failure = None
        report = None
        if runtime.paths is not None:
            try:
                report = await asyncio.to_thread(
                    read_conformance_report, runtime.paths.report_path
                )
            except (OSError, ValueError):
                pass
        if runtime.helper is None and not runtime.helper_possible:
            process = ProcessFacts(False, None, True, False)
            helper_cleanup = CleanupFacts("not_needed", True)
        elif runtime.helper is not None:
            try:
                receipt = await asyncio.to_thread(
                    read_cleanup_receipt, runtime.paths.scope_directory,
                    runtime.scope_id, self.owner_generation,
                )
                process = ProcessFacts(
                    receipt["child_launched"], receipt["child_exit_code"], True,
                    process.forced is True or receipt["forced"],
                )
                try:
                    await self._db(
                        "confirm_scope_cleanup", runtime.scope_id,
                        self.owner_generation, forced=process.forced is True,
                    )
                    helper_cleanup = CleanupFacts("confirmed", True)
                except BaseException as error:
                    persistence_failure = failure_for(error, "finalizing")
            except (OSError, ValueError):
                pass

        target_cleanup = CleanupFacts("not_needed", True)
        if runtime.dispatch_possible:
            try:
                target = await self._stop_target_runtime(
                    runtime.target.ref, runtime.target,
                    wait_seconds=runtime.runtime_policy.cleanup_wait_seconds,
                )
                target_cleanup = target.cleanup
            except BaseException:
                try:
                    target = await self._db(
                        "mark_target_cleanup_required", runtime.target.ref.id
                    )
                    target_cleanup = target.cleanup
                except BaseException:
                    target_cleanup = CleanupFacts("unconfirmed", None)

        complete = helper_cleanup.complete is True and target_cleanup.complete is True
        payload = {
            "core_report": report,
            "process": process.to_payload(),
            "cleanup": helper_cleanup.to_payload(),
            "target_cleanup": target_cleanup.to_payload(),
        }
        result = TaskResult("conformance", payload)
        persisted = TaskResult("conformance", {**payload, "core_report": None})
        if complete:
            if runtime.helper_possible:
                try:
                    await self._db(
                        "release_evidenced_reservations", ResourceRef("task", runtime.id),
                        scope_ids=(runtime.scope_id,), disposition="normal",
                    )
                except BaseException as error:
                    persistence_failure = persistence_failure or failure_for(error, "finalizing")
                    complete = False
        if complete:
            if report is None:
                failure = failure or Failure.make("conformance_unavailable", "conformance")
            elif failure is not None and process.cleanup_complete is True:
                # A complete public report is the conformance outcome; worker exit
                # and an earlier process exception do not overwrite it.
                failure = None
            state = "failed" if failure is not None else "succeeded"
        else:
            try:
                await self._db("mark_task_cleanup_required", runtime.id)
            except BaseException as error:
                persistence_failure = persistence_failure or failure_for(error, "finalizing")
            failure = Failure.make("cleanup_unconfirmed", "finalizing")
            state = "unknown"
        await self._finish_task(
            entry, result=result, persisted_result=persisted,
            failure=failure, state=state, persistence_failure=persistence_failure,
        )


__all__ = [
    "ConformanceSupport", "conformance_worker_command", "conformance_worker_config",
    "create_conformance_paths", "project_conformance_task",
    "read_conformance_report", "validate_conformance_worker_config",
]
