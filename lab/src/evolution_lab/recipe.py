"""Seal native recipe output; no second campaign configuration or overrides.

Domain recipes produce a native manifest and corpus. This common preparation
layer consumes that output in place, preserving core's path resolution and
identity rules. It performs no provider calls and does not copy private inputs.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from evolution.tree import ArtifactUnsafeError, ManifestV2, load_manifest_v2
from evolution.tree.artifacts.artifact_policy import TreeScan, scan_tree
from evolution.tree.bindings import (
    editor_environment_values,
    editor_program_paths,
    reflector_environment_values,
    reflector_program_paths,
)
from evolution.tree.execution.command_binding import CommandBinding, CommandChanged
from evolution.tree.roles.config import CommandGroupingSection, ModelGroupingSection
from evolution.tree.execution.config import CommandSection
from evolution.tree.execution.interaction import EvalCase

from evolution_lab._manifest_schema import (
    LEGACY_CAMPAIGN_MANIFEST_SCHEMAS,
    CORPUS_INSIDE_ROLE_WORKSPACE,
    CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE,
    GROUPER_KIND_UNSUPPORTED,
    GROUPER_KIND_UNSUPPORTED_MESSAGE,
    BASELINE_IMPORT_UNSUPPORTED,
    BASELINE_IMPORT_UNSUPPORTED_MESSAGE,
    LINKED_RUN_UNSUPPORTED,
    campaign_cases,
    corpus_inside_role_workspace,
    grouping_section,
    has_unsupported_grouper,
    admits_campaign_manifest,
    admits_campaign_schema,
    has_baseline_import,
    is_linked_run,
)
from evolution_lab._document_inputs import (
    DocumentInputs,
    DocumentInputsError,
    capture_document_inputs,
)
from evolution_lab.owner.core_raw import CoreRawUnavailable
from evolution_lab.provenance import FilePin, InputChanged, WheelPin


class PreparationArtifactsInvalid(ValueError):
    """A declared corpus, seed, program, docs tree or extra input is unsuitable."""


class PreparationEnvironmentInvalid(ValueError):
    """The explicit environment or producing distribution set is unsuitable."""


class LinkedRunUnsupported(ValueError):
    """The manifest continues another run (``linked_from``); Lab does not continue campaigns."""

    code = LINKED_RUN_UNSUPPORTED


class BaselineImportUnsupported(ValueError):
    """Lab inserts the source into the manifest it passes to Core."""

    code = BASELINE_IMPORT_UNSUPPORTED


class GrouperKindUnsupported(ValueError):
    """A /15 manifest whose ``grouper.kind`` Lab does not support (``agent_cli``; PDR-0015)."""

    code = GROUPER_KIND_UNSUPPORTED


class CorpusInsideRoleWorkspace(ValueError):
    """The corpus lies inside ``seed_root`` or a role's workspace; core refuses it at ``evolve`` (acceptance F-11)."""

    code = CORPUS_INSIDE_ROLE_WORKSPACE


@dataclass(frozen=True)
class PreparedInputs:
    manifest: ManifestV2
    cases: tuple[EvalCase, ...] = field(repr=False)
    files: tuple[FilePin, ...]
    wheels: tuple[WheelPin, ...]
    seed: TreeScan = field(repr=False)
    commands: tuple[CommandBinding, ...] = field(repr=False)
    editor_environment: tuple[str, ...] = field(repr=False)
    reflector_environment: tuple[str, ...] = field(repr=False)
    program_paths: tuple[Path, ...] = field(repr=False)
    document_inputs: DocumentInputs | None = field(repr=False)
    environment_docs_digest: str | None
    _environment: Mapping[str, str] | None = field(repr=False, compare=False)

    def _verification_environment(
        self, env: Mapping[str, str] | None
    ) -> Mapping[str, str]:
        if self._environment is None:
            current = os.environ
            if env is not None:
                try:
                    matches = dict(env) == dict(current)
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    raise InputChanged("prepared environment changed")
            return current
        if env is not None:
            try:
                matches = dict(env) == dict(self._environment)
            except (TypeError, ValueError):
                matches = False
            if not matches:
                raise InputChanged("prepared environment changed")
        return self._environment

    def verify(self, *, env: Mapping[str, str] | None = None) -> None:
        environ = self._verification_environment(env)
        for pin in self.files:
            pin.verify()
        for wheel in self.wheels:
            wheel.verify()
        try:
            workflow = self.manifest.workflow
            assert workflow is not None
            if editor_environment_values(workflow.editor, environ) != self.editor_environment:
                raise InputChanged("editor environment changed")
            if (
                reflector_environment_values(workflow.reflector, environ)
                != self.reflector_environment
            ):
                raise InputChanged("reflector environment changed")
            if scan_tree(self.manifest.seed_root, self.manifest.policy) != self.seed:
                raise InputChanged("seed tree changed")
            for command in self.commands:
                command.verify()
            docs_path = workflow.reflector.environment_docs
            documents = (
                None
                if docs_path is None
                else capture_document_inputs(docs_path)
            )
            if documents != self.document_inputs:
                raise InputChanged("environment docs changed")
        except (
            OSError, ValueError, ArtifactUnsafeError, CommandChanged, DocumentInputsError
        ) as exc:
            raise InputChanged("prepared inputs changed") from exc

    def public(self) -> dict[str, Any]:
        """Provenance metadata only: no corpus text, argv or environment values."""
        return {
            "manifest": str(self.manifest.path),
            "files": [pin.public() for pin in self.files],
            "distributions": [pin.public() for pin in self.wheels],
            "seed": {
                "candidate_digest": self.seed.candidate_digest,
                "dependency_digest": self.seed.dependency_digest,
            },
            "case_count": len(self.cases),
            "environment_docs_digest": self.environment_docs_digest,
        }


def _bind_command(
    section: CommandSection,
    manifest: ManifestV2,
    environment: Mapping[str, str] | None,
) -> CommandBinding:
    return CommandBinding(
        argv=section.argv,
        cwd=section.cwd,
        dependency_paths=section.dependency_paths,
        allowed_env=section.allowed_env,
        seed_root=manifest.seed_root,
        excluded_roots=(),
        base_env=environment,
    )


def prepare_inputs(
    manifest_path: Path,
    *,
    wheel_paths: tuple[Path, ...] = (),
    extra_paths: tuple[Path, ...] = (),
    env: Mapping[str, str] | None = None,
    schemas: frozenset[str] = LEGACY_CAMPAIGN_MANIFEST_SCHEMAS,
) -> PreparedInputs:
    """Validate and seal an existing runnable recipe without relocation.

    The default `schemas` admits the legacy `/15` set only; the Owner callers widen it through `schemas=`.

    `extra_paths` explicitly names non-secret world/model/settings dependencies.
    Wheels remain optional for offline inspection; the H1 facade requires the
    producing Lab and core distributions for a runnable preparation.
    """
    source = FilePin.capture(manifest_path)
    try:
        raw = json.loads(source.path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raw = None  # Keep core's diagnosis for malformed documents.
    if isinstance(raw, dict) and isinstance(raw.get("schema"), str) and not admits_campaign_schema(raw["schema"], schemas):
        raise ValueError("Lab requires a native campaign manifest: " + " or ".join(sorted(schemas)))
    manifest = load_manifest_v2(source.path)
    if not admits_campaign_manifest(manifest, schemas):
        raise ValueError("Lab requires a native campaign manifest: " + " or ".join(sorted(schemas)))
    if is_linked_run(manifest):
        raise LinkedRunUnsupported(f"Lab does not continue a campaign from a linked run ({LINKED_RUN_UNSUPPORTED})")
    if has_baseline_import(manifest):
        raise BaselineImportUnsupported(BASELINE_IMPORT_UNSUPPORTED_MESSAGE)
    if has_unsupported_grouper(manifest):
        raise GrouperKindUnsupported(GROUPER_KIND_UNSUPPORTED_MESSAGE)
    try:
        misplaced_corpus = corpus_inside_role_workspace(manifest)
    except (OSError, ValueError) as exc:
        raise PreparationArtifactsInvalid() from exc
    if misplaced_corpus:
        raise CorpusInsideRoleWorkspace(CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE)
    workflow = manifest.workflow
    try:
        environment = None if env is None else MappingProxyType(dict(env))
        if environment is not None and any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError("environment must map strings to strings")
    except (TypeError, ValueError) as exc:
        raise PreparationEnvironmentInvalid() from exc
    environ = os.environ if environment is None else environment

    try:
        corpus = FilePin.capture(workflow.corpus_path)
        cases = campaign_cases(manifest)
    except (OSError, ValueError, CoreRawUnavailable) as exc:
        raise PreparationArtifactsInvalid() from exc
    try:
        editor_environment = editor_environment_values(workflow.editor, environ)
        reflector_environment = reflector_environment_values(workflow.reflector, environ)
        editor_paths = editor_program_paths(workflow.editor, environ)
        reflector_paths = reflector_program_paths(workflow.reflector, environ)
    except (OSError, ValueError) as exc:
        raise PreparationEnvironmentInvalid() from exc

    grouping = grouping_section(workflow)
    grouping_command = (
        grouping.command if isinstance(grouping, CommandGroupingSection) else None
    )
    commands = []
    for section in (
        workflow.scorer,
        workflow.editor,
        workflow.reflector.role,
        grouping_command,
    ):
        if not isinstance(section, CommandSection):
            continue
        try:
            commands.append(_bind_command(section, manifest, environment))
        except (OSError, ValueError) as exc:
            raise PreparationArtifactsInvalid() from exc

    program_paths = tuple(dict.fromkeys((
        workflow.scorer.cwd,
        *editor_paths,
        *reflector_paths,
        *(() if grouping_command is None else (grouping_command.cwd,)),
    )))
    pin_paths = [*extra_paths]
    if isinstance(grouping, ModelGroupingSection) and grouping.system_prompt is not None:
        # The model grouping's own prompt file: like a role's `system_prompt`, its drift must be refused.
        pin_paths.append(grouping.system_prompt)
    for command in commands:
        pin_paths.extend((command.executable, *command.proof_paths))
    for path in (*editor_paths, *reflector_paths):
        if not path.is_dir():
            pin_paths.append(path)
    pins = {source.path: source, corpus.path: corpus}
    for path in pin_paths:
        try:
            pin = FilePin.capture(path)
        except (OSError, ValueError) as exc:
            try:
                for command in commands:
                    command.verify()
            except CommandChanged as drift:
                raise InputChanged("bound command changed while being pinned") from drift
            raise PreparationArtifactsInvalid() from exc
        pins[pin.path] = pin

    try:
        documents = (
            None
            if workflow.reflector.environment_docs is None
            else capture_document_inputs(workflow.reflector.environment_docs)
        )
    except DocumentInputsError as exc:
        raise PreparationArtifactsInvalid() from exc
    if documents is not None:
        for document in documents.files:
            try:
                pin = FilePin.capture(document.path)
            except (OSError, ValueError) as exc:
                raise PreparationArtifactsInvalid() from exc
            pins[pin.path] = pin
    try:
        wheels = tuple(WheelPin.capture(path) for path in wheel_paths)
    except (OSError, ValueError) as exc:
        raise PreparationEnvironmentInvalid() from exc
    try:
        seed = scan_tree(manifest.seed_root, manifest.policy)
    except (OSError, ValueError, ArtifactUnsafeError) as exc:
        raise PreparationArtifactsInvalid() from exc

    prepared = PreparedInputs(
        manifest=manifest,
        cases=cases,
        files=tuple(pins.values()),
        wheels=wheels,
        seed=seed,
        commands=tuple(commands),
        editor_environment=editor_environment,
        reflector_environment=reflector_environment,
        program_paths=program_paths,
        document_inputs=documents,
        environment_docs_digest=None if documents is None else documents.digest,
        _environment=environment,
    )
    prepared.verify()
    return prepared
