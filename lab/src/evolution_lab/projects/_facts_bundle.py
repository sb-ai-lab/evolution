"""Read-only proof that a linked Bundle is the Bundle of current materials."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from evolution_lab.preparation.basis import measurement_basis
from evolution_lab.preparation.models import CampaignSource, EvaluationSource, PreparationError
from evolution_lab.preparation.relocation import Relocator
from evolution_lab.service.codecs import decode_json_object
from evolution_lab.service.environment import expand_service_environment
from evolution_lab.service.errors import ServiceError

from .package import inputs_inventory, read_package_entry
from .errors import ProjectError


def linked_basis(link, *, pair, state_root, storage, cache):
    """Only the original Bundle in this pair's state can supply a basis."""
    if link.pair != pair or link.state_root != state_root or link.bundle_digest is None:
        return None
    if link.bundle_digest not in cache:
        try:
            stored = storage.get_bundle(link.bundle_digest)
            cache[link.bundle_digest] = (stored, measurement_basis(stored))
        except (PreparationError, OSError):
            cache[link.bundle_digest] = (None, None)
    return cache[link.bundle_digest][1]


def current_bundle(project, implementation, *, entry, errors, links, pair, state_root,
                   storage, registry, service_environment, cache):
    """Reuse a verified link; never compose, capture an environment or store a basis.

    The inputs digest includes source roles and environment declarations, but not
    env/secrets/path bindings. The existing receipt and descriptor prove those
    non-file request fields as well. Missing legacy proof fails closed.
    """
    if entry is None or errors != ():
        return None, None
    try:
        request = read_package_entry(entry)
        environments, expected = expand_service_environment(request.environments, service_environment)
        request = replace(request, environments=environments)
        digest, _, _ = inputs_inventory(request, working_copy=Path(project.working_copy.path), cache=registry)
        relocator = Relocator(request.roots, request.path_bindings)
        if isinstance(request.source, CampaignSource):
            kind, field = 'campaign', 'manifest_path'
        elif isinstance(request.source, EvaluationSource):
            kind, field = 'evaluation', 'input_path'
        else:
            return None, None  # probe packages do not provide fixed evaluations
        source_path = getattr(request.source, field)
        expected_receipt = {
            'schema': 'lab-bundle-source-receipt/1', field: str(source_path.absolute()),
            'launch_path': str(request.source.launch_path.absolute()),
            'roots': [{'alias': alias, 'source_path': str(path)} for alias, path in relocator.roots.items()],
            'path_bindings': [{'document': str(binding.document.absolute()), 'pointer': binding.pointer,
                              'root_alias': binding.root_alias, 'relative_path': binding.relative_path}
                             for binding in request.path_bindings],
        }
        for link in reversed(links):
            binding = link.binding
            if (link.kind != 'bundle' or binding['inputs']['digest'] != digest
                    or binding.get('working_copy') != project.working_copy.path
                    or binding.get('materials_root') != project.materials_root
                    or binding.get('place') != implementation.place):
                continue
            basis = linked_basis(link, pair=pair, state_root=state_root, storage=storage, cache=cache)
            if basis is None:
                continue
            stored, _ = cache[link.bundle_digest]
            descriptor = stored.descriptor
            if (descriptor.source_kind != kind or dict(descriptor.env) != dict(request.env)
                    or tuple(descriptor.secret_names) != tuple(sorted(request.secret_names))
                    or descriptor.entrypoint != relocator.logical(source_path)
                    or descriptor.launch != relocator.logical(request.source.launch_path)
                    or (expected is not None and descriptor.environments[0].digest != expected)):
                continue
            try:
                receipt = decode_json_object((storage.state_directory / 'receipts' / 'bundles'
                                             / f'{link.bundle_digest}.json').read_bytes(), max_bytes=None)
            except (OSError, ServiceError):
                continue
            if all(receipt.get(key) == value for key, value in expected_receipt.items()):
                return link.bundle_digest, basis
    except (ProjectError, PreparationError, ServiceError, OSError, ValueError):
        pass
    return None, None
