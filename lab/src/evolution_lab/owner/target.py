"""Managed target launch, private ready binding, and scope lifecycle."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from evolution_lab._target_preflight import preflight_failure_code, run_target_preflight

from .binding import (open_bundle, revalidate_environment, declared_executor_identity,
    revalidate_executor_identity)
from .models import CleanupFacts, Failure, OwnerError, ResourceRef, TargetRef, TaskResult
from .protocol import ChannelClosed, FrameReader, HelperHandle, ProtocolError
from .receipts import decode_json, open_directory_nofollow, read_cleanup_receipt
from .secrets import bind_target_secrets, policy_token_bindings
from .target_gateway import TargetGateway, gateway_available, check_gateway_port
from .tasks import failure_for

from evolution_lab.durability import sync_file
from evolution_lab.launch_env import with_launch_values


def create_target_paths(root, target_id, scope_id, *, scratch=None):
    """Create only fresh private owned leaves, refusing symlinks at every step."""
    root_fd = open_directory_nofollow(root)
    try:
        for container, leaf in (('scopes', scope_id), ('targets', target_id), ('snapshots', target_id)):
            parent_fd = root_fd
            if scratch is not None and container != 'scopes':
                parent_fd = open_directory_nofollow(scratch)
            try:
                try:
                    os.mkdir(container, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                directory = os.open(container, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
                try:
                    os.mkdir(leaf, 0o700, dir_fd=directory)
                    sync_file(directory)
                finally:
                    os.close(directory)
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        try:
            os.mkdir('artifacts', 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        artifacts = os.open('artifacts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        os.close(artifacts)
        sync_file(root_fd)
    finally:
        os.close(root_fd)


def runtime_config(root, binding, target_ref, *, scratch=None):
    launch = binding.launch
    temporary = root if scratch is None else scratch
    config = dict(schema='lab-target-runtime/1', launch_id=target_ref.id,
        launch_digest=binding.launch_digest, bundle_digest=binding.bundle_digest,
        base_url=launch['endpoint']['base_url'], allowed_artifact_root=str(root / 'artifacts'),
        snapshot_root=str(temporary / 'snapshots' / target_ref.id),
        work_directory=str(temporary / 'targets' / target_ref.id),
        policy=binding.manifest.policy.to_payload(), identity=dict(launch['identity']), limits=dict(launch['limits']))
    if launch['schema'] in ('lab-target-launch/2', 'lab-target-launch/3'):
        import_root = launch['agent']['import_root']
        config['schema'] = 'lab-target-runtime/2'
        config['agent'] = dict(launch['agent'],
            import_root=None if import_root is None else str(binding.bundle_path / import_root))
        config['secret_names'] = list(launch['secret_names'])
        if launch['schema'] == 'lab-target-launch/3':
            config['turn'] = dict(launch['turn'])
    return config


def target_argv(binding) -> list[str]:
    if binding.launch['schema'] in ('lab-target-launch/2', 'lab-target-launch/3'):
        return [binding.target_environment.executable, '-I', '-m', 'evolution_lab.target.launch']
    command = binding.launch.get('command')
    return [command['executable'], *(
        arg['literal'] if 'literal' in arg else str(binding.bundle_path / arg['bundle_path'])
        for arg in command['args'])]


async def read_ready(handle, config):
    reader = FrameReader(16384)
    ready = None
    try:
        while True:
            messages, eof = reader.read_available(handle.target_ready_fd)
            if messages:
                if ready is not None or len(messages) != 1:
                    raise OwnerError('target_mismatch', 'target_start')
                ready = messages[0]
                if (set(ready) != {'schema', 'launch_id', 'launch_digest', 'base_url', 'boot_id'}
                    or ready['schema'] != 'lab-target-ready/1'
                    or any(ready[k] != config[k] for k in ('launch_id', 'launch_digest', 'base_url'))
                    or not isinstance(ready['boot_id'], str) or not ready['boot_id']):
                    raise OwnerError('target_mismatch', 'target_start')
            if eof:
                if ready is None:
                    raise OwnerError('target_unavailable', 'target_start')
                return ready
            await asyncio.sleep(.01)
    except ProtocolError:
        raise OwnerError('target_mismatch', 'target_start') from None
    finally:
        handle.close_target_ready()


async def matched_health(binding, config, boot_id):
    from evolution.tree.target.http_adapter import HttpTransport
    from evolution.tree.execution.wire_v2 import parse_health
    try:
        status, raw, media = await HttpTransport(binding.manifest.target).request('GET', '/evolution/v2/health')
        if status != 200 or media.split(';')[0].strip().lower() != 'application/json':
            raise OwnerError('target_unavailable', 'target_start')
        payload = decode_json(raw, max_bytes=binding.manifest.target.max_response_bytes)
        health = parse_health(payload)
        if (not health.ready or health.boot_id != boot_id
            or health.allowed_artifact_root != config['allowed_artifact_root']
            or health.policy_digest != binding.manifest.policy.digest
            or any(value is not None and getattr(health, key) != value for key, value in config['identity'].items())
            or any(getattr(health.capabilities, key) != value for key, value in config['limits'].items())):
            raise OwnerError('target_mismatch', 'target_start')
        return payload
    except OwnerError:
        raise
    except ValueError:
        raise OwnerError('target_mismatch', 'target_start') from None
    except Exception:
        raise OwnerError('target_unavailable', 'target_start') from None


async def evaluation_preflight(binding):
    """Run public health, capability, identity, and compatibility preflight only."""
    from evolution.tree.artifacts.artifact_policy import scan_tree
    from evolution.tree.target.http_adapter import HttpSessionAdapter
    from evolution_lab.client import CheckResult, Failure as CheckFailure

    try:
        if binding.source_kind not in {"evaluation", "probe"} or binding.manifest.workflow is not None:
            raise OwnerError("invalid_argument", "check")
        await run_target_preflight(
            binding.manifest, scan=scan_tree, adapter_type=HttpSessionAdapter
        )
    except OwnerError:
        raise
    except Exception as error:
        return CheckResult(
            "failed", CheckFailure(preflight_failure_code(error), "check")
        )
    return CheckResult("passed", None)


@dataclass
class TargetRuntime:
    ref: TargetRef
    scope_id: str
    binding: object
    secrets: object
    startup: object
    ended: asyncio.Future
    helper: HelperHandle | None = None
    gateway: TargetGateway | None = None
    execution_policy: object = None
    policy_digest: str | None = None
    token_bindings: dict = field(default_factory=dict)
    requested_secrets: object = field(default=None, repr=False)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    failure: Failure | None = None
    helper_possible: bool = False
    forced: bool = False
    probe_abort_deadline: float | None = None
    probe_abort_started: asyncio.Event = field(default_factory=asyncio.Event)
    stop_admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def request_stop(self):
        self.stop.set()
        if self.gateway is not None:
            await self.gateway.request_stop()
        if self.helper is not None:
            try:
                await self.helper.stop()
            except (ChannelClosed, OSError):
                self.helper.close_commands()


class TargetSupport:
    async def target(self, target_ref):
        self._assert_loop()
        if not isinstance(target_ref, TargetRef):
            raise OwnerError('invalid_argument', 'read')
        status = self._target_cache.get(target_ref.id)
        if status is None:
            status = await self._db('target_status', target_ref.id)
        if status.owner_generation != target_ref.owner_generation:
            raise OwnerError('not_found', 'read')
        return status

    async def start_target(self, connection, *, secrets, client_ref=None, execution_policy: str | None = None):
        self._assert_mutation()
        binding = self._connection_binding(connection)
        policy, token_bindings, upstream_keys = None, {}, {}
        if execution_policy is not None:
            from collections.abc import Mapping
            from evolution_lab.execution_policy import parse_policy
            try:
                record = await self.execution_policy(execution_policy)
            except OwnerError as error:
                if error.code == 'not_found':
                    raise OwnerError('policy_unconfirmed', 'target_start') from None
                raise
            policy = parse_policy(record.to_payload()['policy'])
            if policy.gateway is not None:
                gateway_available()
            token_bindings = policy_token_bindings(binding, policy)
            if not isinstance(secrets, Mapping):
                raise OwnerError('invalid_argument', 'target_start')
            if set(secrets) & token_bindings.keys():
                raise OwnerError('secret_conflict', 'target_start')
            upstream_names = {route.api_key_secret for route in policy.routes.values()} - {None}
            if not upstream_names <= secrets.keys():
                raise OwnerError('invalid_argument', 'target_start')
            upstream_keys = {name: secrets[name] for name in upstream_names}
            if any(not isinstance(value, str) for value in upstream_keys.values()):
                raise OwnerError('invalid_argument', 'target_start')
            target_values = {name: value for name, value in secrets.items() if name not in upstream_names}
            bound = bind_target_secrets(binding, target_values, lab_filled=frozenset(token_bindings))
        else:
            bound = bind_target_secrets(binding, secrets)
        requested_secrets = dict(secrets)
        async def admitted(accepted):
            self._assert_mutation()
            for runtime in self._targets.values():
                status = await self._db('target_status', runtime.ref.id)
                if runtime.probe_abort_deadline is not None and (
                        status.state != 'stopped' or not self._probe_io_clear(runtime.ref.id)):
                    raise OwnerError('cleanup_required', 'target_start')
                if status.state == 'ready':
                    # One live target per Owner (PDR-0004): another Bundle, launch or project holds the slot —
                    # target_slot_held (PDR-0025); the same Bundle with other secrets is a binding mismatch.
                    if status.bundle_digest != binding.bundle_digest or status.launch_digest != binding.launch_digest:
                        raise OwnerError('target_slot_held', 'target_start')
                    reused_ref = await self._db('target_client_ref', runtime.ref.id)
                    if client_ref is not None and reused_ref != client_ref:
                        raise OwnerError('target_slot_held', 'target_start')
                    if runtime.policy_digest != execution_policy:
                        raise OwnerError('binding_mismatch', 'target_start')
                    if runtime.secrets is None or (runtime.requested_secrets != requested_secrets if policy is not None else runtime.secrets.values != bound.values):
                        raise OwnerError('binding_mismatch', 'target_start')
                    reuse = await self._db('accept_task', action='target_start', ref=ResourceRef('target', runtime.ref.id))
                    entry = self._register_task(reuse.task_ref, 'target_start', ResourceRef('target', runtime.ref.id))
                    accepted.set_result(entry.ref)
                    await self._finish_task(entry, result=TaskResult('target_start', status.to_payload()))
                    return
            if policy is not None and policy.gateway is not None:
                check_gateway_port(policy)
            reservation = await self._db('accept_target_start', connection_id=connection.id,
                launch_digest=binding.launch_digest, bundle_digest=binding.bundle_digest,
                base_url=binding.launch['endpoint']['base_url'], client_ref=client_ref, execution_policy=execution_policy)
            entry = self._register_task(reservation.task_ref, 'target_start', ResourceRef('target', reservation.target_ref.id))
            runtime = TargetRuntime(reservation.target_ref, reservation.scope_id, binding, bound,
                entry, self._loop.create_future(), execution_policy=policy, policy_digest=execution_policy,
                token_bindings=token_bindings, requested_secrets=requested_secrets if policy is not None else None)
            if policy is not None and policy.gateway is not None:
                runtime.gateway = TargetGateway(self, runtime)
            self._targets[runtime.ref.id] = runtime
            accepted.set_result(entry.ref)
            await self._run_target(runtime, upstream_keys=upstream_keys)
        return await self._admit(admitted)

    async def _run_target(self, runtime, *, upstream_keys=None):
        entry, binding = runtime.startup, runtime.binding
        try:
            await self._db('task_running', entry.ref.id)
            fresh = await asyncio.to_thread(open_bundle, self._storage, binding.bundle_digest, stage='target_start')
            declared = await asyncio.to_thread(declared_executor_identity, fresh, stage='target_start')
            await asyncio.to_thread(revalidate_executor_identity, fresh, declared, stage='target_start')
            await asyncio.to_thread(revalidate_environment, fresh, fresh.target_environment.digest, stage='target_start')
            if entry.stop.is_set() or self._stopping:
                raise OwnerError('dispatch_refused', 'target_start')
            await asyncio.to_thread(create_target_paths, self.state_directory, runtime.ref.id,
                runtime.scope_id, scratch=self.scratch_directory)
            config = runtime_config(self.state_directory, fresh, runtime.ref, scratch=self.scratch_directory)
            if runtime.gateway is not None:
                async def start_gateway():
                    return await runtime.gateway.start(runtime.execution_policy, upstream_keys)
                gateway_start = asyncio.create_task(start_gateway())
                stopping = asyncio.create_task(entry.stop.wait())
                try:
                    done, _ = await asyncio.wait((gateway_start, stopping), return_when=asyncio.FIRST_COMPLETED)
                    if stopping in done or self._stopping:
                        raise OwnerError('dispatch_refused', 'target_start')
                    tokens = await gateway_start
                finally:
                    for pending in (gateway_start, stopping):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(gateway_start, stopping, return_exceptions=True)
                values = dict(runtime.secrets.values)
                for name in runtime.secrets.names:
                    if name in runtime.token_bindings:
                        values[name] = tokens[runtime.token_bindings[name]]
                environment = dict(runtime.secrets.environment)
                environment.update(values)
                runtime.secrets = replace(runtime.secrets, values=values, environment=environment)
                if not runtime.gateway.alive():
                    raise OwnerError('gateway_unavailable', 'target_start')
            await self._db('mark_helper_possible', runtime.scope_id)
            runtime.helper_possible = True
            if entry.stop.is_set() or self._stopping:
                # The marker is already durable: retain uncertainty if launch is skipped.
                raise OwnerError('dispatch_refused', 'target_start')
            runtime.helper = HelperHandle.launch(fresh.execution_environment.executable,
                self.state_directory / 'scopes' / runtime.scope_id, runtime.scope_id, self.owner_generation,
                ownership=self.ownership)
            args = target_argv(fresh)
            environment = dict(runtime.secrets.environment)
            if fresh.launch['schema'] in ('lab-target-launch/2', 'lab-target-launch/3'):
                environment = with_launch_values(environment)
            async def startup():
                await runtime.helper.configure(argv=args, env=environment,
                    stop_grace_seconds=fresh.launch['stop_grace_seconds'], role='target', child_config=config,
                    cwd=config['work_directory'])
                await runtime.helper.wait_event('ready')
                if entry.stop.is_set() or self._stopping:
                    raise OwnerError('dispatch_refused', 'target_start')
                await runtime.helper.go()
                ready = await read_ready(runtime.helper, config)
                return await matched_health(fresh, config, ready['boot_id'])
            startup_task = asyncio.create_task(startup())
            stop_wait = asyncio.create_task(entry.stop.wait())
            try:
                async with asyncio.timeout(fresh.launch['startup_timeout_seconds']):
                    done, _ = await asyncio.wait((startup_task, stop_wait), return_when=asyncio.FIRST_COMPLETED)
                    if stop_wait in done:
                        raise OwnerError('dispatch_refused', 'target_start')
                    health = await startup_task
                if self._stopping or entry.stop.is_set():
                    raise OwnerError('dispatch_refused', 'target_start')
                await self._db('mark_target_ready', entry.ref.id, boot_id=health['boot_id'], health=health)
                entry.done.set_result(await self._db('task_status', entry.ref.id))
            finally:
                for task in (startup_task, stop_wait):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(startup_task, stop_wait, return_exceptions=True)
        except BaseException as error:
            runtime.failure = (Failure.make('target_unavailable', 'target_start') if isinstance(error, TimeoutError)
                else failure_for(error, 'target_start'))
            await runtime.request_stop()
        finally:
            try:
                await self._settle_target(runtime)
            except BaseException as error:
                failure = runtime.failure or failure_for(error, 'finalizing')
                runtime.failure = failure
                try:
                    await self._db('mark_target_cleanup_required', runtime.ref.id)
                except BaseException:
                    pass
                if not entry.done.done():
                    await self._finish_task(entry, failure=failure)
            finally:
                runtime.secrets = None
                runtime.requested_secrets = None
                if runtime.gateway is not None:
                    runtime.gateway.close()
                if runtime.helper:
                    runtime.helper.close_commands()
                    runtime.helper.close_target_ready()
                    runtime.helper.close_events()
                if not runtime.ended.done():
                    runtime.ended.set_result(None)

    async def _settle_target(self, runtime):
        entry, helper = runtime.startup, runtime.helper
        if helper is None:
            if runtime.gateway is not None:
                await runtime.gateway.stop()
            if runtime.helper_possible:
                await self._db('mark_target_cleanup_required', runtime.ref.id)
            else:
                await self._db('settle_target_no_helper', runtime.ref.id, runtime.failure)
            await self._finish_task(entry, failure=runtime.failure)
            return
        failure_deadline = self._loop.time() + 30 if runtime.failure else None
        while helper.process.poll() is None:
            runtime.forced |= any(event['kind'] == 'force_started' for event in helper.events)
            if self._closed:
                return
            if runtime.gateway is not None and not runtime.gateway.alive() and not runtime.stop.is_set() and not entry.stop.is_set() and not self._stopping:
                runtime.failure = Failure.make('gateway_unavailable', 'running')
                await runtime.request_stop()
            if runtime.stop.is_set() or entry.stop.is_set() or self._stopping:
                await runtime.request_stop()
            if failure_deadline and self._loop.time() >= failure_deadline and not entry.done.done():
                await self._db('mark_target_cleanup_required', runtime.ref.id)
                await self._finish_task(entry, failure=runtime.failure)
            await asyncio.sleep(.02)
        runtime.forced |= any(event['kind'] == 'force_started' for event in helper.events)
        if runtime.gateway is not None:
            await runtime.gateway.stop()
        try:
            # Receipt reading must not queue behind cancelled Core HTTP workers in the default pool.
            receipt = await self._loop.run_in_executor(self._executor, read_cleanup_receipt,
                self.state_directory / 'scopes' / runtime.scope_id, runtime.scope_id, self.owner_generation)
        except (OSError, ValueError):
            await self._db('mark_target_cleanup_required', runtime.ref.id)
            if not entry.done.done():
                await self._finish_task(entry, failure=runtime.failure or Failure.make('cleanup_unconfirmed', 'finalizing'))
            return
        await self._db('confirm_scope_cleanup', runtime.scope_id, self.owner_generation,
            forced=runtime.forced or receipt['forced'])
        # No probe instance outlives a confirmed stop (spec 2026-10-02 §3.5). A target that ended
        # on its own still has in-memory sessions: settle them, then release every row.
        try:
            await self._close_target_probes(runtime)
        except Exception:
            pass
        if runtime.probe_abort_deadline is not None:
            # An already-ended target's watcher may have started before the abort's admitted stop.
            # Close admission before releasing the quarantine rows, even in that ordering.
            async with runtime.stop_state_lock:
                status = await self.target(runtime.ref)
                if status.state != 'stopped':
                    await self._db('set_target_state', runtime.ref.id, state='stopping')
            if not self._probe_io_clear(runtime.ref.id):
                # The process is gone, but a started pre-connect request could reach a replacement.
                # Keep the lifetime reservation and uncertain rows; neither reply nor watcher waits.
                await self._db('set_target_state', runtime.ref.id, state='cleanup_required',
                    cleanup=CleanupFacts('unconfirmed', None))
                self._hold(self._release_probe_target_after_io(runtime))
                return
        await self._release_stopped_target(runtime)

    async def _release_probe_target_after_io(self, runtime):
        await self._wait_probe_io(runtime.ref.id)
        if not self._closed:
            await self._release_stopped_target(runtime)

    async def _release_stopped_target(self, runtime):
        """Only the confirmed-stop watcher (possibly after its I/O drain) authorizes release."""
        await self._db('release_probes_of_target', runtime.ref.id)
        if runtime.probe_abort_deadline is None:
            await self._discard_released_snapshots()
        else:
            self._hold(self._discard_released_snapshots())
        if not await self._db('target_has_operation', runtime.ref.id):
            await self._db('release_evidenced_reservations', ResourceRef('target', runtime.ref.id),
                scope_ids=tuple(s['id'] for s in await self._db('resource_scopes', ResourceRef('target', runtime.ref.id))), disposition='confirmed_cleanup')
        async with runtime.stop_state_lock:
            await self._db('set_target_state', runtime.ref.id, state='stopped', cleanup=CleanupFacts('confirmed', True), failure=runtime.failure)
        if not runtime.startup.done.done():
            await self._finish_task(runtime.startup, failure=runtime.failure or Failure.make('target_unavailable', 'target_start'))

    async def _close_target_probes(self, runtime):
        """An abort also wakes a watcher that was already waiting for normal probe closure."""
        if runtime.probe_abort_deadline is None:
            closing = self._hold(self._close_probes_for(runtime.ref.id))
            aborted = asyncio.create_task(runtime.probe_abort_started.wait())
            try:
                done, _ = await asyncio.wait((closing, aborted), return_when=asyncio.FIRST_COMPLETED)
                if closing in done:
                    await closing
                    return
            finally:
                aborted.cancel()
                await asyncio.gather(aborted, return_exceptions=True)
        await self._close_probes_for(runtime.ref.id, deadline=runtime.probe_abort_deadline)

    async def stop_target(self, target_ref):
        self._assert_mutation()
        if not isinstance(target_ref, TargetRef):
            raise OwnerError('invalid_argument', 'read')
        status = await self._db('target_status_for_stop', target_ref.id)
        if target_ref.owner_generation != self.owner_generation:
            raise OwnerError('target_not_running', 'stop')
        runtime = self._targets.get(target_ref.id)
        retry_probe_abort = (runtime is not None and runtime.probe_abort_deadline is not None
            and not runtime.ended.done() and status.state == 'cleanup_required')
        if runtime is None or (status.state != 'ready' and not retry_probe_abort):
            raise OwnerError('target_not_running', 'stop')
        # The stop takes target_use itself, which open probe rows would refuse: close them first
        # (spec 2026-10-02 §3.4); an uncertain row does not refuse the stop.
        await asyncio.shield(self._hold(self._close_probes_for(
            target_ref.id, deadline=runtime.probe_abort_deadline)))
        return await self._admit_target_stop(target_ref, runtime)

    async def _admit_target_stop(self, target_ref, runtime, *, require_probe_free=False):
        """The same admitted stop for an explicit stop and a probe abort, single flight during admission."""
        async def admitted(accepted):
            self._assert_mutation()
            reservation = await self._db('accept_task', action='target_stop', ref=ResourceRef('target', target_ref.id), reserve_target_use=True,
                                         require_probe_free=require_probe_free)
            entry = self._register_task(reservation.task_ref, 'target_stop', ResourceRef('target', target_ref.id))
            accepted.set_result(entry.ref)
            try:
                await self._db('task_running', entry.ref.id)
                value = await self._stop_target_runtime(
                    target_ref, runtime, wait_seconds=30
                )
                if value.cleanup.complete is not True:
                    raise OwnerError('cleanup_unconfirmed', 'stop')
                await self._finish_task(entry, result=TaskResult('target_stop', value.to_payload()))
            except BaseException as error:
                try:
                    await runtime.request_stop()
                    if runtime.probe_abort_deadline is not None:
                        # Keep the live helper's scope/reservation: another explicit stop can retry it.
                        # Serialize with the watcher so a late timeout cannot overwrite confirmed cleanup.
                        async with runtime.stop_state_lock:
                            status = await self.target(target_ref)
                            if status.state != 'stopped':
                                await self._db('set_target_state', target_ref.id, state='cleanup_required',
                                    cleanup=CleanupFacts('unconfirmed', None))
                finally:
                    await self._finish_task(entry, failure=failure_for(error, 'stop'))
        async with runtime.stop_admission_lock:
            for entry in self._tasks.values():
                if (entry.action == 'target_stop' and entry.resource == ResourceRef('target', target_ref.id)
                        and not entry.done.done()):
                    return entry.ref
            return await self._admit(admitted)

    async def _stop_target_runtime(self, target_ref, runtime, *, wait_seconds):
        """Stop through the one target watcher without a second public admission."""
        async with runtime.stop_state_lock:
            status = await self.target(target_ref)
            if status.state == 'stopped':
                return status  # the watcher may have confirmed cleanup before this admitted stop started
            await self._db('set_target_state', target_ref.id, state='stopping')
        await runtime.request_stop()
        async with asyncio.timeout(wait_seconds):
            await asyncio.shield(runtime.ended)
        return await self.target(target_ref)
