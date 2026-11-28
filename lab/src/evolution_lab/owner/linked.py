"""Owner start conditions over immutable Bundles and verified Champion copies."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil

from evolution.tree import manifest_dependencies

from evolution_lab.preparation.inventory_audit import dotted_to_pointer
from .models import BaselineImportChoice, OwnerError, ResourceRef
from .receipts import ReceiptError, publish_json, read_json


BASELINE_IMPORT_FILE = "baseline-import.json"
BASELINE_IMPORT_SCHEMA = "lab-operation-baseline-import/1"
DELETED_RESULTS = frozenset({"deleted", "deleting", "delete_failed"})


def manifest_copy(binding, *, seed: Path, out: Path, predecessor: Path | None = None,
                  baseline: str = "inherit", baseline_import: Path | None = None) -> Path:
    """Keep each declared dependency at its admitted Bundle location."""
    if predecessor is not None and baseline_import is not None:
        raise ValueError("linked_from and baseline_import are exclusive")
    raw = json.loads(binding.manifest_path.read_text())
    for dependency in manifest_dependencies(binding.manifest).dependencies:
        if (dependency.role == 'artifacts' or dependency.resolved is None
                or dependency.base == 'packaged'):
            continue
        # Core hashes the scorer argv spelling. Its working directory still
        # moves to the same absolute Bundle path, which preserves resolution.
        if baseline_import is not None and dependency.field.startswith('scorer.argv['):
            continue
        if dependency.kind == 'argv_operand' and not dependency.resolved.exists():
            continue
        parts = dotted_to_pointer(dependency.field).strip('/').split('/')
        node = raw
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        key = int(parts[-1]) if isinstance(node, list) else parts[-1]
        prior = node[key]
        value = str(dependency.resolved.absolute())
        # argv_operands exposes the value of a --name=value operand.
        if dependency.kind == 'argv_operand' and prior != dependency.declared:
            value = prior[:-len(dependency.declared)] + value
        node[key] = value
    raw['seed_root'] = str(seed)
    if predecessor is not None:
        raw['linked_from'] = {'run_dir': str(predecessor), 'baseline': baseline}
    if baseline_import is not None:
        raw['baseline_import'] = {'source': str(baseline_import)}
    publish_json(out, raw, max_bytes=1024 * 1024)
    return out


_ACTIVE_CONTROL_STATES = frozenset({"reserved", "running", "stopping"})


def require_evaluation_source(kind, control_state, results_state, source: Path) -> None:
    """Admit an operation as a baseline source from its facts, wherever they were read.

    One rule for the project quickstart (its own, an older pair's or a saved
    summary's facts) and for ``Owner.start``: a fixed evaluation that is not
    running, whose results are present and whose measurement journal exists.
    """
    if kind != "evaluation":
        raise OwnerError("baseline_source_unknown", "before_start")
    if results_state in DELETED_RESULTS:
        raise OwnerError("baseline_source_deleted", "before_start")
    if (results_state != "present" or not isinstance(control_state, str)
            or control_state in _ACTIVE_CONTROL_STATES or not (source / "events.jsonl").is_file()):
        raise OwnerError("baseline_source_unknown", "before_start")


async def admit_baseline_source(owner, choice: BaselineImportChoice) -> BaselineImportChoice:
    """Verify the choice's layout and the Owner's own operation facts."""
    try:
        canonical = await asyncio.to_thread(
            BaselineImportChoice.for_operation, choice.source_state, choice.source_operation_id,
        )
        if canonical.source_state == owner.state_directory:
            status = await owner._db("operation_status", choice.source_operation_id)
            await asyncio.to_thread(require_evaluation_source, status.kind, status.control_state,
                                    status.results_state, canonical.source)
        if (await asyncio.to_thread(choice.source.resolve) != canonical.source
                or not await asyncio.to_thread((canonical.source / "events.jsonl").is_file)):
            raise OwnerError("baseline_source_unknown", "before_start")
    except OwnerError as error:
        if error.code in {"not_found", "invalid_argument"}:
            raise OwnerError("baseline_source_unknown", "before_start") from None
        raise
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        raise OwnerError("baseline_source_unknown", "before_start") from None
    return canonical


def _saved_baseline_source(metadata_path: Path, operation_id: str) -> Path | None:
    try:
        row = read_json(metadata_path)
    except FileNotFoundError:
        return None
    except (OSError, ReceiptError):
        raise OwnerError("artifacts_invalid", "before_start") from None
    try:
        if (set(row) != {"schema", "operation_id", "source_operation_id", "source_state", "source"}
                or row["schema"] != BASELINE_IMPORT_SCHEMA or row["operation_id"] != operation_id):
            raise ValueError
        ResourceRef("operation", row["source_operation_id"])
        source, state = Path(row["source"]), Path(row["source_state"])
        if any(not path.is_absolute() or ".." in path.parts or "\0" in str(path) for path in (source, state)):
            raise ValueError
    except (TypeError, ValueError, OwnerError):
        raise OwnerError("artifacts_invalid", "before_start") from None
    # Reuse the stored canonical spelling. Core decides availability on the
    # next start, including when the source directory has since disappeared.
    return source


def _copy_champion(owner, status, inventory, destination):
    from .changes import snapshot_changes
    with snapshot_changes(owner.state_directory, owner._storage, status, inventory,
                          scratch_directory=owner.scratch_directory) as snapshot:
        shutil.copytree(snapshot.call_directory / 'selected', destination)


async def campaign_manifest(owner, runtime, binding) -> Path:
    """Store the start condition beside the run and recreate Core's manifest on retry."""
    paths = runtime.paths
    metadata_path = paths.operation_directory / 'linked.json'
    link = runtime.linked
    import_path = paths.operation_directory / BASELINE_IMPORT_FILE
    choice = runtime.baseline_import
    if choice is not None:
        await asyncio.to_thread(publish_json, import_path, {
            "schema": BASELINE_IMPORT_SCHEMA, "operation_id": runtime.id,
            "source_operation_id": choice.source_operation_id,
            "source_state": str(choice.source_state), "source": str(choice.source),
        })
        source = choice.source
    else:
        source = await asyncio.to_thread(_saved_baseline_source, import_path, paths.operation_directory.name)
    if source is not None:
        if link is not None or metadata_path.exists():
            raise OwnerError("artifacts_invalid", "before_start")
        return await asyncio.to_thread(manifest_copy, binding, seed=Path(binding.manifest.seed_root),
            baseline_import=source, out=paths.scope_directory / "campaign-manifest.json")
    if link is not None:
        await asyncio.to_thread(publish_json, metadata_path, {'schema': 'lab-operation-link/1', **link})
    else:
        try:
            link = read_json(metadata_path)
        except FileNotFoundError:
            return binding.manifest_path
    if (set(link) - {'schema'} != {'operation_id', 'baseline', 'seed'}
            or link.get('schema', 'lab-operation-link/1') != 'lab-operation-link/1'
            or link['baseline'] not in {'inherit', 'fresh'} or link['seed'] not in {'champion', 'project'}):
        raise OwnerError('artifacts_invalid', 'before_start')
    ref = ResourceRef('operation', link['operation_id'])
    predecessor = owner.state_directory / 'artifacts' / ref.id / ref.id
    seed = Path(binding.manifest.seed_root)
    if link['seed'] == 'champion':
        selected = paths.operation_directory / 'linked-seed'
        if not (paths.run_directory / 'campaign.jsonl').exists():
            async def copy():
                status = await owner._db('operation_status', ref.id)
                if status.results_state in {'deleted', 'deleting', 'delete_failed'}:
                    raise OwnerError('linked_predecessor_deleted', 'before_start')
                core = None if status.result is None else status.result.core
                # A refusal before Core's creation record did not bind this
                # copy. Retry against the predecessor's current Champion.
                if selected.exists():
                    await asyncio.to_thread(shutil.rmtree, selected)
                if status.results_state not in {'present', 'not_created'}:
                    raise OwnerError('artifacts_invalid', 'before_start')
                if core is None or core.get('final_champion') is None:
                    # Only an unfinished predecessor may defer to Core's
                    # lineage refusal. Missing finished evidence cannot select
                    # the project seed on the operator's behalf.
                    unborn = (status.results_state == 'not_created'
                              and not (predecessor / 'campaign.jsonl').exists())
                    if unborn or (core is not None and core.get('complete') is False):
                        return
                    raise OwnerError('artifacts_invalid', 'before_start')
                inventory = await owner._db('owned_inventory', ref.id)
                await owner._result_thread(_copy_champion, owner, status, inventory, selected)
            await owner._result_work(copy, owner._result_io.acquire_read(ref.id, 'read'))
        if selected.exists():
            seed = selected
    return await asyncio.to_thread(manifest_copy, binding, predecessor=predecessor,
        baseline=link['baseline'], seed=seed,
        out=paths.scope_directory / 'campaign-manifest.json')
