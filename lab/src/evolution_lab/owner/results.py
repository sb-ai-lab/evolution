"""File change-set, direct diff and Owner-held export of verified snapshots.

The ordered changes are the single source for direct diff and ZIP publication.
They contain only verified snapshot bytes, never mutable source paths.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
from hashlib import sha256

from .models import Approval, DiffResult, OwnerError, durable_campaign_outcome

from evolution_lab.durability import sync_file


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    kind: str
    before_sha256: str | None
    after_sha256: str | None
    before_executable: int | None
    after_executable: int | None
    payload: bytes | None
    before_data: bytes | None
    after_data: bytes | None

    def manifest_entry(self, index: int) -> dict:
        """Closed lab-change-set/1 row; payload name is independent of path."""
        return {'path': self.path, 'kind': self.kind,
            'before_sha256': self.before_sha256, 'after_sha256': self.after_sha256,
            'before_executable': self.before_executable,
            'after_executable': self.after_executable,
            'payload': f'payload/{index:08d}' if self.payload is not None else None}


def ordered_changes(snapshot) -> tuple[FileChange, ...]:
    """Encode exactly one ordered row per Task2 policy-checked changed file."""
    result = []
    for path in snapshot.changed_paths:
        before = snapshot.source_files.get(path)
        after = snapshot.selected_files.get(path)
        if before is None:
            kind = 'add'
        elif after is None:
            kind = 'delete'
        elif before.data != after.data:
            kind = 'modify'
        elif before.executable != after.executable:
            kind = 'mode'
        else:
            raise OwnerError('export_unavailable', 'diff')
        result.append(FileChange(path, kind,
            sha256(before.data).hexdigest() if before else None,
            sha256(after.data).hexdigest() if after else None,
            before.executable if before else None,
            after.executable if after else None,
            after.data if kind in {'add', 'modify'} else None,
            before.data if before else None,
            after.data if after else None))
    return tuple(result)


def _text(data: bytes | None) -> str | None:
    if data is None:
        return ''
    if b'\x00' in data:
        return None
    try:
        return data.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return None


def _lf_lines(value: str) -> list[str]:
    """Keep LF-terminated lines and the final fragment; preserve every other char."""
    if not value:
        return []
    pieces = value.split('\n')
    lines = [piece + '\n' for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def _unified(change: FileChange, before: str, after: str) -> str:
    left = '/dev/null' if change.before_data is None else f'a/{change.path}'
    right = '/dev/null' if change.after_data is None else f'b/{change.path}'
    lines = list(difflib.unified_diff(_lf_lines(before), _lf_lines(after),
        fromfile=left, tofile=right,
        lineterm='\n'))
    if not lines:
        # An empty file addition/deletion still has a change-set row.
        return f'--- {left}\n+++ {right}\n'
    output = []
    for line in lines:
        if line.startswith((' ', '+', '-')) and not line.endswith('\n'):
            output.append(line + '\n\\ No newline at end of file\n')
        else:
            output.append(line)
    return ''.join(output)


def diff_snapshot(snapshot, changes: tuple[FileChange, ...] | None = None) -> DiffResult:
    """Serialize UTF-8 text hunks and explicit binary/mode entries."""
    if changes is None:
        changes = ordered_changes(snapshot)
    if snapshot.no_change:
        if changes:
            raise OwnerError('export_unavailable', 'diff')
        return DiffResult(snapshot.operation_id, snapshot.source_digest,
            snapshot.selected_digest, 'no_change', '', (),
            source_ref=snapshot.source_ref, selected_ref=snapshot.selected_ref)
    if not changes:
        raise OwnerError('export_unavailable', 'diff')
    hunks = []
    nontext = []
    for change in changes:
        before = _text(change.before_data)
        after = _text(change.after_data)
        if before is None or after is None:
            nontext.append({'path': change.path, 'kind': 'binary'})
        if (change.before_executable is not None
                and change.after_executable is not None
                and change.before_executable != change.after_executable):
            nontext.append({'path': change.path, 'kind': 'mode'})
        if (before is not None and after is not None
                and (change.before_data is None or change.after_data is None
                     or change.before_data != change.after_data)):
            hunks.append(_unified(change, before, after))
    return DiffResult(snapshot.operation_id, snapshot.source_digest,
        snapshot.selected_digest, 'changed', ''.join(hunks), tuple(nontext),
        source_ref=snapshot.source_ref, selected_ref=snapshot.selected_ref)


def _plain(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def archive_snapshot(snapshot):
    """Return the closed result and deterministic ZIP bytes from verified bytes."""
    from io import BytesIO
    import json
    from zipfile import ZipFile, ZipInfo, ZIP_STORED
    from .models import ExportResult

    changes = ordered_changes(snapshot)
    if snapshot.no_change:
        if changes:
            raise OwnerError('export_unavailable', 'export')
        return ExportResult(snapshot.operation_id, 'no_change', None, None, None), None
    if not changes:
        raise OwnerError('export_unavailable', 'export')
    manifest = {
        'schema': 'lab-change-set/1', 'operation_id': snapshot.operation_id,
        'prepared_digest': snapshot.prepared_digest, 'bundle_digest': snapshot.bundle_digest,
        'distributions': _plain(snapshot.distributions),
        'source_digest': snapshot.source_digest, 'selected_digest': snapshot.selected_digest,
        'selected_version': snapshot.selected_version,
        'campaign_outcome': durable_campaign_outcome(_plain(snapshot.campaign_outcome)),
        'campaign_complete': snapshot.campaign_complete,
        'measurement_complete': snapshot.measurement_complete,
        'changes': [change.manifest_entry(index) for index, change in enumerate(changes)],
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_STORED) as archive:
        entries = [('manifest.json', manifest_bytes)]
        entries.extend((f'payload/{index:08d}', change.payload)
            for index, change in enumerate(changes) if change.payload is not None)
        for name, data in entries:
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    data = buffer.getvalue()
    digest = sha256(data).hexdigest()
    return ExportResult(snapshot.operation_id, 'ready', digest, digest, manifest), data


def _export_error(error, stage):
    from .tasks import failure_for
    return OwnerError(failure_for(error, stage).code, stage)


def _export_location(record, stage):
    """A registry row grants access only to its exact owned archive path."""
    from .models import ExportResult
    ExportResult(record['operation_id'], 'ready', record['export_id'], record['sha256'], {})
    expected = f"artifacts/{record['operation_id']}/exports/{record['export_id']}.zip"
    if record['path'] != expected:
        raise OwnerError('export_unavailable', stage)
    return expected


def _archive_bytes(directory, name):
    """Read one regular no-follow archive; its registry hash attests the bytes.

    Unlike included artifact files, a published archive may still share a link
    with its private temp name if the producing process died during publication.
    """
    import os
    import stat
    from .changes import _Unavailable
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise _Unavailable()
        data = stream.read()
        after = os.fstat(stream.fileno())
        if (before.st_size != len(data) or
                (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise _Unavailable()
        return data


def read_archive(state_directory, record, *, stage='read'):
    """Read without following links, then verify registry hash before parsing."""
    from io import BytesIO
    import errno
    import json
    from pathlib import Path
    from zipfile import BadZipFile, ZipFile
    from .changes import _directory, _Unavailable
    from .models import ExportResult

    try:
        path = Path(state_directory) / _export_location(record, stage)
        with _directory(path.parent) as directory:
            data = _archive_bytes(directory, path.name)
        if sha256(data).hexdigest() != record['sha256']:
            raise OwnerError('export_unavailable', stage)
        with ZipFile(BytesIO(data)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
        if (manifest.get('schema') != 'lab-change-set/1'
                or manifest.get('operation_id') != record['operation_id']):
            raise OwnerError('export_unavailable', stage)
        return ExportResult(record['operation_id'], 'ready', record['export_id'],
            record['sha256'], manifest), data
    except OSError as error:
        code = ('export_unavailable' if error.errno in
            {errno.ENOENT, errno.ELOOP, errno.ENOTDIR} else 'io_error')
        raise OwnerError(code, stage) from None
    except (_Unavailable, BadZipFile, ValueError, KeyError, TypeError, AttributeError):
        raise OwnerError('export_unavailable', stage) from None


def publish_archive(state_directory, result, data):
    """Fsync and atomically link to the hash name without overwriting any entry.

    The private temporary name is removed before registry publication. A crash
    destination is accepted only after complete byte and hash equality.
    """
    import os
    from pathlib import Path
    from uuid import uuid4
    from .changes import _directory, _relative_directory, _Unavailable

    if result.state != 'ready' or sha256(data).hexdigest() != result.sha256:
        raise OwnerError('export_unavailable', 'export')
    root = Path(state_directory) / 'artifacts' / result.operation_id
    try:
        with _directory(root) as root_fd:
            try:
                os.mkdir('exports', 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            sync_file(root_fd)
            with _relative_directory(root_fd, 'exports') as directory:
                name = f'{result.export_id}.zip'
                temporary = f'.{uuid4().hex}.tmp'
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        sync_file(stream.fileno())
                    try:
                        os.link(temporary, name, src_dir_fd=directory,
                            dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        existing = _archive_bytes(directory, name)
                        if existing != data or sha256(existing).hexdigest() != result.sha256:
                            raise OwnerError('export_unavailable', 'export')
                finally:
                    os.unlink(temporary, dir_fd=directory)
                sync_file(directory)
    except _Unavailable:
        raise OwnerError('export_unavailable', 'export') from None


def _delete_entry(parent, name, *, tree):
    """Remove one owned entry; never follow a link or chmod an outer parent."""
    import os
    import stat

    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(before.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    if not tree:
        raise OSError('owned file is a directory')
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        actual = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise OSError('owned directory changed')
        # Only a descriptor for a directory inside the recorded tree is changed.
        if stat.S_IMODE(actual.st_mode) & 0o700 != 0o700:
            os.fchmod(fd, stat.S_IMODE(actual.st_mode) | 0o700)
        for child in sorted(os.listdir(fd)):
            _delete_entry(fd, child, tree=True)
        sync_file(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino):
            raise OSError('owned directory changed')
        os.rmdir(name, dir_fd=parent)
    finally:
        os.close(fd)


def delete_inventory(state_directory, inventory):
    """Delete exact recorded entries, returning privately tracked incomplete roots."""
    import os
    from .changes import _directory, _relative, _relative_directory

    remaining = []
    with _directory(state_directory) as state_fd:
        for item in inventory:
            try:
                parts = _relative(item['path'])
                if item['kind'] not in {'tree', 'file'} or len(parts) < 2:
                    raise OSError('invalid owned inventory')
                try:
                    with _relative_directory(state_fd, '/'.join(parts[:-1])) as parent:
                        _delete_entry(parent, parts[-1], tree=item['kind'] == 'tree')
                        sync_file(parent)
                except FileNotFoundError:
                    # An absent parent or leaf is an already removed owned path.
                    pass
            except OSError:
                remaining.append(item)
    return tuple(remaining)


class ResultsSupport:
    """Owner-held export/delete and verified local reads."""

    def _export_ref(self, ref, stage):
        from .models import ResourceRef
        self._assert_loop()
        if not isinstance(ref, ResourceRef) or ref.kind != 'operation':
            raise OwnerError('invalid_argument', stage)
        if self._stopping or self._closed:
            raise OwnerError('service_stopping', stage)

    async def _export_status(self, ref, stage, *, producing=False):
        """Shared diff/export admission; the resolver decides artifact availability."""
        status = await self._db('operation_status', ref.id)
        if status.results_state == 'deleted':
            raise OwnerError('results_deleted', stage)
        if status.results_state in {'deleting', 'delete_failed'}:
            raise OwnerError('export_unavailable', stage)
        if producing:
            if status.kind != 'campaign':
                raise OwnerError('export_unavailable', stage)
            runtime = self._operations.get(ref.id)
            if (status.control_state in {'reserved', 'running', 'stopping'}
                    or (runtime is not None and not runtime.done.done())):
                raise OwnerError('resource_busy', stage)
            if status.control_state in {'orphaned', 'cleanup_required'}:
                raise OwnerError('cleanup_required', stage)
            # not_created/unavailable may predate orphan release. Only deletion
            # states above are authoritative; inspect the selected artifact fresh.
        if status.kind == 'campaign':
            await self._refuse_foreign_campaign(ref.id, stage)
        return status

    async def _refuse_foreign_campaign(self, operation_id, stage):
        """An archive/terminal task cache cannot bypass a confirmed family refusal.

        A temporarily unreadable journal does not invalidate a previously published
        archive. Pin mismatch is distinct from journal damage and remains refused.
        """
        from evolution.tree import JournalFault
        from evolution.tree.campaign import CampaignFamilyUnsupported
        from .core_raw import FAMILIES, campaign_read, CoreRawUnavailable
        try:
            await self._result_thread(campaign_read,
                self.state_directory / 'artifacts' / operation_id / operation_id, families=FAMILIES)
        except (CampaignFamilyUnsupported, CoreRawUnavailable):
            raise OwnerError('export_unavailable', stage) from None
        except (JournalFault, ValueError, OSError):
            pass  # temporary journal damage leaves a published archive available

    async def export(self, ref):
        import asyncio
        from .models import TaskResult

        self._export_ref(ref, 'export')
        lease = self._result_io.acquire_export(ref.id, 'export')
        accepted = self._loop.create_future()
        accepted.add_done_callback(lambda f: None if f.cancelled() else f.exception())

        async def work():
            entry = None
            try:
                status = await self._export_status(ref, 'export', producing=True)
                reservation = await self._db('accept_task', action='export', ref=ref)
                entry = self._register_task(reservation.task_ref, 'export', ref)
                accepted.set_result(entry.ref)
                await self._db('task_running', entry.ref.id)
                record = await self._db('published_export', ref.id)
                if record is not None:
                    result, _ = await self._result_thread(read_archive,
                        self.state_directory, record, stage='export')
                else:
                    inventory = await self._db('owned_inventory', ref.id)
                    def build():
                        from .changes import snapshot_changes
                        with snapshot_changes(self.state_directory, self._storage,
                                status, inventory, stage='export',
                                scratch_directory=self.scratch_directory) as snapshot:
                            result, data = archive_snapshot(snapshot)
                            if data is not None:
                                publish_archive(self.state_directory, result, data)
                            return result
                    result = await self._result_thread(build)
                    if result.state == 'ready':
                        await self._db('publish_export', ref.id, result.export_id)
                await self._finish_task(entry,
                    result=TaskResult('export', result.to_payload()))
            except BaseException as error:
                failure = _export_error(error, 'export').failure
                if entry is None:
                    if not accepted.done():
                        accepted.set_exception(OwnerError(failure.code, 'export'))
                else:
                    await self._finish_task(entry, failure=failure)

        self._hold_result(work, lease)
        return await asyncio.shield(accepted)

    async def read_export(self, ref, export_id):
        self._export_ref(ref, 'read')
        lease = self._result_io.acquire_read(ref.id, 'read')
        async def work():
            try:
                await self._export_status(ref, 'read')
                if (not isinstance(export_id, str) or len(export_id) != 64
                        or any(c not in '0123456789abcdef' for c in export_id)):
                    raise OwnerError('invalid_argument', 'read')
                record = await self._db('published_export', ref.id, export_id)
                if record is None:
                    raise OwnerError('not_found', 'read')
                _, data = await self._result_thread(read_archive,
                    self.state_directory, record, stage='read')
                return data
            except Exception as error:
                raise _export_error(error, 'read') from None
        return await self._result_work(work, lease)

    async def _project_export_task(self, status):
        """Called by task/wait inside their Owner-held shared read lease."""
        from dataclasses import replace
        from .models import TaskResult

        await self._refuse_foreign_campaign(status.ref.id, 'read')
        if status.result is not None and status.result.payload['state'] == 'no_change':
            return status
        record = await self._db('task_export', status.id)
        if record is None and status.result is not None:
            # A failed finalization commit can leave an in-memory result with a
            # separate persistence failure. It still requires a verified record.
            record = await self._db('published_export', status.ref.id,
                status.result.payload['export_id'])
        if record is None:
            return replace(status, result=None)
        result, _ = await self._result_thread(read_archive,
            self.state_directory, record, stage='read')
        return replace(status, result=TaskResult('export', result.to_payload()))


    async def _delete_summary(self, ref, status):
        """Use known facts first; a missing projection never prevents deletion."""
        prior = self._operation_cache.get(ref.id, status)
        if prior.result is not None:
            return prior.result
        scopes = await self._db('resource_scopes', ref)
        scope = self.state_directory / scopes[0]['root_relative'] if scopes else None
        def project():
            from contextlib import ExitStack
            from .changes import _directory, _Unavailable
            from .readers import project_operation
            try:
                with ExitStack() as stack:
                    stack.enter_context(_directory(
                        self.state_directory / 'artifacts' / ref.id / ref.id))
                    if status.kind == 'evaluation' and scope is not None:
                        stack.enter_context(_directory(scope))
                    return project_operation(self.state_directory, status,
                        scope_directory=scope).result
            except (OSError, _Unavailable):
                return status.result
        return await self._result_thread(project)

    def _invalidate_deleted_payloads(self, operation_id):
        from dataclasses import replace
        self._operation_cache.pop(operation_id, None)
        # Measurement ids repeat between operations; a deleted one's facts must not outlive it.
        self._measurements.drop(operation_id)
        self._projection_cache.pop(operation_id, None)
        self._baseline_provenance.drop(self.state_directory / "artifacts" / operation_id / operation_id)
        self._summary_cache.pop(operation_id, None)
        for task_id, status in tuple(self._task_cache.items()):
            if (status.action == 'export' and status.ref is not None
                    and status.ref.id == operation_id and status.result is not None
                    and status.result.payload['state'] == 'ready'):
                self._task_cache[task_id] = replace(status, result=None)

    async def _finish_deletion_task(self, entry, *, failure, remaining):
        from .models import DeletionResult, Failure, Persistence, TaskResult, TaskStatus
        from .tasks import failure_for
        try:
            terminal = await self._db('finish_deletion', entry.resource.id,
                entry.ref.id, failure=failure, remaining=remaining)
        except BaseException as error:
            # A failed final commit cannot produce a successful deletion receipt.
            persistence = failure_for(error, 'finalizing')
            failure = failure or Failure.make(persistence.code, 'delete')
            terminal = TaskStatus(entry.ref.id, self.owner_generation, 'delete',
                entry.resource, 'failed', TaskResult('delete', DeletionResult(
                    entry.resource.id, 'failed', failure)), failure,
                Persistence('failed', persistence))
        self._task_cache[entry.ref.id] = terminal
        if not entry.done.done():
            entry.done.set_result(terminal)

    async def delete_results(self, ref, *, approval: Approval | None = None,
                             request_id: str | None = None, observed_start: int | None = None):
        import asyncio
        from .models import Failure
        from .tasks import failure_for

        self._export_ref(ref, 'delete')
        lease = self._result_io.acquire_write(ref.id, 'delete')
        approval_kwargs = {} if approval is None else dict(
            approval=approval, request_id=request_id, observed_start=observed_start)
        accepted = self._loop.create_future()
        accepted.add_done_callback(lambda f: None if f.cancelled() else f.exception())

        async def work():
            entry = None
            remaining = ()
            failure = None
            try:
                status = await self._db('operation_status', ref.id)
                if observed_start is not None and status.starts[-1].start != observed_start:
                    raise OwnerError('state_changed', 'delete')
                runtime = self._operations.get(ref.id)
                if (status.control_state in {'reserved', 'running', 'stopping'}
                        or (runtime is not None and not runtime.done.done())):
                    raise OwnerError('resource_busy', 'delete')
                if status.control_state not in {'finished', 'released'}:
                    raise OwnerError('cleanup_required', 'delete')
                previous = await self._db('deletion_record', ref.id)
                completed = (self._completed_deletion(ref.id, previous['task_id'])
                    if previous is not None else None)
                inventory = (previous['inventory'] if previous is not None
                    else await self._db('owned_inventory', ref.id))
                remaining = inventory
                summary = (None if previous is not None
                    else await self._delete_summary(ref, status))
                # Cleanup may have released its scope while its Owner-held
                # finalizer still runs. A terminal result Future is insufficient.
                if any(task.action == 'cleanup' and task.resource == ref
                        and (task.runner is None or not task.runner.done())
                        for task in self._tasks.values()):
                    raise OwnerError('resource_busy', 'delete')
                reservation = await self._db('accept_deletion', ref.id,
                    summary_result=summary, completed_deletion=completed, **approval_kwargs)
                await self._auto_resume.cancel(ref.id, accepted_scope=status.starts[-1].scope_id)
                entry = self._register_task(reservation.task_ref, 'delete', ref)
                self._invalidate_deleted_payloads(ref.id)
                accepted.set_result(entry.ref)
                await self._db('task_running', entry.ref.id)
                remaining = (() if status.results_state == 'deleted'
                    else await self._result_thread(delete_inventory,
                        self.state_directory, inventory))
                if remaining:
                    failure = Failure.make('io_error', 'delete')
            except BaseException as error:
                failure = Failure.make(failure_for(error, 'delete').code, 'delete')
                if entry is None:
                    if not accepted.done():
                        accepted.set_exception(OwnerError(failure.code, 'delete'))
                    return
            await self._finish_deletion_task(entry, failure=failure, remaining=remaining)

        self._hold_result(work, lease)
        return await asyncio.shield(accepted)
