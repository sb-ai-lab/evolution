"""An implementation's package entry: read, cover, inventory, form check (spec §3.3, §3.6)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from evolution_lab._fileset import excluded_path
from evolution_lab.preparation import EnvironmentInput, PreparationError, ProbeSource
from evolution_lab.service.codecs import BundleRequest, decode_bundle_request, decode_json_object
from evolution_lab.service.errors import ServiceError

from .errors import ProjectError
from .inventory import Entry, collect, inventory_digest, observe_file

REQUEST_SCHEMA = "lab-service-bundle-request/2"


def read_package_entry(path: Path) -> BundleRequest:
    try:
        value = decode_json_object(Path(path).read_bytes(), max_bytes=None)
        if "schema" in value:
            raise ProjectError("package_unavailable")
        return decode_bundle_request({"schema": REQUEST_SCHEMA, **value}, base=Path(path).parent)
    except (OSError, ServiceError, ValueError):
        raise ProjectError("package_unavailable") from None


def _related(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def covers_place(request: BundleRequest, place: Path) -> bool:
    return any(_related(Path(root.source_path), Path(place)) for root in request.roots)


def _documents(request: BundleRequest) -> tuple[Path, ...]:
    source = request.source
    if isinstance(source, ProbeSource):
        return (Path(source.entry_path),)
    first = getattr(source, "manifest_path", None) or getattr(source, "input_path")
    return (Path(first), Path(source.launch_path))


def source_paths(request: BundleRequest) -> tuple[Path, ...]:
    return tuple(Path(root.source_path) for root in request.roots) + _documents(request)


def _label(path: Path, working_copy: Path) -> str:
    return path.relative_to(working_copy).as_posix() if path.is_relative_to(working_copy) else f"@{path}"


def environment_declarations(environments) -> list[dict]:
    """Position identifies the execution/target role; lists retain their declared order."""
    return [{"executable": str(env.executable), "wheel_paths": list(map(str, env.wheel_paths)),
             "runtime_files": list(map(str, env.runtime_files))}
            if isinstance(env, EnvironmentInput) else {"kind": "service"} for env in environments]


def environment_sources(declarations) -> list[dict]:
    # These files are external inputs even when physically inside the working copy. Do not
    # coalesce their source roles with roots/documents: a later declaration can drop one role.
    return [{"path": f"@{path}"} for env in declarations
            for path in (*env.get("wheel_paths", ()), *env.get("runtime_files", ()))]


def input_sources(request: BundleRequest, working_copy: Path) -> list[dict]:
    return ([{"alias": root.alias, "path": _label(Path(root.source_path), working_copy)} for root in request.roots]
            + [{"path": _label(document, working_copy)} for document in _documents(request)])


def inventory_sources(sources, *, working_copy: Path, cache=None) -> tuple[Entry, ...]:
    union: dict[str, Entry] = {}
    for source in sources:
        label = source["path"]
        path = Path(label[1:]) if label.startswith("@/") else working_copy / label
        for entry in collect(path, prefix="" if label == "." else label, cache=cache):
            union[entry.path] = entry
    return tuple(sorted(union.values(), key=lambda entry: entry.path))


def environment_inventory(sources, declarations, *, working_copy: Path, cache=None) -> tuple[Entry, ...]:
    union = {entry.path: entry for entry in inventory_sources(sources, working_copy=working_copy, cache=cache)}
    for source in environment_sources(declarations):
        label = source["path"]
        path = Path(label[1:])
        # Declared files are explicit inputs, including .venv children and the bytes behind
        # symlinks. The ordinary root walk's exclusions/symlink entries do not apply here.
        observed = observe_file(path, read=False, cache=cache)
        try:
            info = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if observed.state == "unreadable":
            raise OSError(f"cannot read environment input: {path}")
        if observed.state == "read":
            target = os.readlink(path) if path.is_symlink() else None
            entry = Entry(label, "symlink" if target is not None else "file", info.st_mode & 0o111,
                          info.st_size, observed.digest.removeprefix("sha256:"), target)
        else:
            entry = Entry(label, "directory" if stat.S_ISDIR(info.st_mode) else "other", 0, None, None)
        union[label] = entry
    return tuple(sorted(union.values(), key=lambda entry: entry.path))


def environment_inputs_digest(entries, declarations, sources) -> str:
    # Domain-separated from legacy byte-only digests. Base-source roles are part of the key
    # so identical file inventories cannot alias different re-inventory instructions.
    return inventory_digest(entries, extra={"environment_declarations": declarations, "sources": sources})


def inputs_inventory(request: BundleRequest, *, working_copy: Path, cache=None) -> tuple[str, tuple[Entry, ...], list[dict]]:
    """Inventory Bundle sources and declared environment files, never interpreter bytes."""
    base_sources = input_sources(request, working_copy)
    declarations = environment_declarations(request.environments)
    sources = base_sources + environment_sources(declarations)
    entries = environment_inventory(base_sources, declarations, working_copy=working_copy, cache=cache)
    return environment_inputs_digest(entries, declarations, base_sources), entries, sources


def validate_form(request: BundleRequest, *, place: Path) -> list[dict[str, str]]:
    """Form checks without environment capture; the full check runs at Bundle creation."""
    errors: list[dict[str, str]] = []
    if not covers_place(request, place):
        errors.append({"code": "package_mismatch", "detail": "no Bundle root covers the implementation place"})
    from evolution_lab.preparation.relocation import Relocator
    try:
        relocator = Relocator(request.roots, request.path_bindings)
    except PreparationError as error:
        errors.append({"code": error.code, "detail": "roots or path bindings"})
        return errors
    documents = _documents(request)
    for document in documents:
        try:
            logical = relocator.logical(document)
        except PreparationError as error:
            errors.append({"code": error.code, "detail": f"{document.name} is outside the Bundle roots"})
            continue
        if excluded_path(logical):
            errors.append({"code": "unsupported_input", "detail": f"{document.name} lies inside an excluded path"})
        if not document.is_file():
            errors.append({"code": "unsupported_input", "detail": f"{document.name} is missing"})
    if errors:
        return errors
    if isinstance(request.source, ProbeSource):
        return errors  # the entry file exists inside a root; its form is read_target_entry's business
    try:
        parsed = [decode_json_object(document.read_bytes(), max_bytes=None) for document in documents]
    except (OSError, ServiceError):
        return [{"code": "unsupported_input", "detail": "a document is not one JSON object"}]
    if getattr(request.source, "manifest_path", None) is not None:
        from evolution.tree import load_manifest_v2
        from evolution_lab._manifest_schema import (
            CORPUS_INSIDE_ROLE_WORKSPACE, CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE, corpus_inside_role_workspace,
            is_campaign_schema, is_linked_run, unsupported_grouper_kind)
        if not is_campaign_schema(parsed[0].get("schema")):
            return [{"code": "unsupported_schema", "detail": "manifest schema is not the pinned core's"}]
        try:
            manifest = load_manifest_v2(documents[0])
        except Exception:  # core raises its own validation errors
            return [{"code": "manifest_invalid", "detail": "load_manifest_v2 refused the manifest"}]
        if getattr(manifest, "workflow", None) is None:
            errors.append({"code": "manifest_invalid", "detail": "manifest has no workflow"})
        if is_linked_run(manifest):
            errors.append({"code": "linked_run_unsupported", "detail": "linked runs are refused"})
        if unsupported_grouper_kind(parsed[0]):
            errors.append({"code": "grouper_kind_unsupported",
                           "detail": f"grouper.kind {parsed[0]['grouper'].get('kind')}: Lab supports fields, command, chat"})
        try:
            misplaced_corpus = corpus_inside_role_workspace(manifest)
        except ValueError:
            errors.append({"code": "manifest_invalid", "detail": "corpus placement could not be checked"})
        except OSError:
            errors.append({"code": "unsupported_input", "detail": "corpus placement could not be checked"})
        else:
            if misplaced_corpus:
                errors.append({"code": CORPUS_INSIDE_ROLE_WORKSPACE, "detail": CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE})
    else:
        from evolution_lab.preparation.evaluation import load_evaluation_input
        try:
            load_evaluation_input(documents[0], "bundle")
        except PreparationError as error:
            errors.append({"code": error.code, "detail": "evaluation input"})
    if parsed[1].get("schema") not in ("lab-target-launch/1", "lab-target-launch/2", "lab-target-launch/3"):
        errors.append({"code": "unsupported_schema", "detail": "launch file schema"})
    for binding in request.path_bindings:
        if excluded_path(binding.relative_path):
            errors.append({"code": "unsupported_input", "detail": "a path binding points inside an excluded path"})
    return errors
