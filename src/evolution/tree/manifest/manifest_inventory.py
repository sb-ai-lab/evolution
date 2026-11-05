"""A read-only inventory of what a parsed manifest declares (spec 2026-09-16).

`manifest_dependencies` walks one admitted `ManifestV2` — never the file, the
filesystem, `PATH`, the environment or a target — and lists every declared
file, tree, executable, working directory, prompt and documentation
dependency with its role, dotted document field, kind, source spelling
(`ManifestV2.declared`), resolution base and lexical resolved path, plus the
declared environment names and a closed set of completeness-boundary codes.

It is the same knowledge admission (`manifest_v2`) and binding
(`command_binding`, `bindings`) apply, gathered in one place for a consumer
that stores or relocates a manifest's project files: the argv walk is
`command_binding.argv_operands`, the only one; the executable rule mirrors
`command_binding._resolve_executable` (a name with a slash resolves against
`cwd`, a bare name is looked up on the bound search path at bind); the
environment rule mirrors `bindings.editor_environment_names` per field.
What a declared program loads once it runs is outside the inventory
(PDR-0005), and the inventory says so with `runtime_loads_unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from evolution.tree.execution.command_binding import argv_operands
from evolution.tree.execution.config import CommandSection
from evolution.tree.manifest.manifest_v2 import ManifestV2
from evolution.tree.roles.config import (
    AgentCliEditorSection,
    AgentCliGrouperSection,
    ChatEditorSection,
    CommandGroupingSection,
    ModelGroupingSection,
)

DependencyRole = Literal[
    "seed",
    "artifacts",
    "scorer",
    "editor",
    "reflector",
    "reflector.grouping",
    "grouper",
    "corpus",
    "linked",
]
DependencyKind = Literal[
    "tree", "file", "executable", "cwd", "prompt", "documentation", "argv_operand", "run_dir"
]
ResolutionBase = Literal["manifest_dir", "cwd", "seed_root", "absolute", "path_lookup", "packaged"]
EnvironmentRole = Literal["scorer", "editor", "reflector", "reflector.grouping", "grouper"]
Sensitivity = Literal["credential", "forwarded"]
BoundaryCode = Literal[
    "runtime_loads_unknown",
    "argv_operands_heuristic",
    "path_lookup_at_bind",
    "packaged_default",
    "workflow_not_parsed",
    "declared_spellings_unavailable",
]

__all__ = [
    "BoundaryCode",
    "Dependency",
    "DependencyInventory",
    "DependencyKind",
    "DependencyRole",
    "EnvironmentName",
    "EnvironmentRole",
    "ResolutionBase",
    "Sensitivity",
    "manifest_dependencies",
]


@dataclass(frozen=True)
class Dependency:
    """One declared path of the document: where it is declared, what it is, how it resolves."""

    role: DependencyRole
    field: str
    kind: DependencyKind
    declared: str | None
    base: ResolutionBase
    resolved: Path | None


@dataclass(frozen=True)
class EnvironmentName:
    """One environment variable name the document declares; never a value."""

    role: EnvironmentRole
    field: str
    name: str
    sensitivity: Sensitivity


@dataclass(frozen=True)
class DependencyInventory:
    schema: str
    manifest_path: Path
    dependencies: tuple[Dependency, ...]
    environment: tuple[EnvironmentName, ...]
    boundary: tuple[BoundaryCode, ...]


class _Walk:
    """Accumulates records and the facts the boundary codes are decided from."""

    def __init__(self, manifest: ManifestV2) -> None:
        self.manifest = manifest
        self.dependencies: list[Dependency] = []
        self.environment: list[EnvironmentName] = []
        self.spelling_missing = False

    def spelling(self, field: str) -> str | None:
        """The declared spelling, or `None` with the absence noted — a declared
        `null` is a present key and is not an absence.

        This dotted-key vocabulary is also written by `_declared_spellings` in
        `manifest_v2.py` and must stay identical to it; a mismatch degrades
        silently into `declared=None` plus `declared_spellings_unavailable`,
        which the inventory tests' boundary assertions are what catch."""
        if field in self.manifest.declared:
            return self.manifest.declared[field]
        self.spelling_missing = True
        return None

    def declared_path(
        self,
        role: DependencyRole,
        field: str,
        kind: DependencyKind,
        resolved: Path,
        base: ResolutionBase,
    ) -> None:
        """A path the loader resolved: `base` is the loader's rule, or
        `absolute` when the spelling itself is absolute."""
        spelling = self.spelling(field)
        if spelling is not None and Path(spelling).is_absolute():
            base = "absolute"
        self.dependencies.append(Dependency(role, field, kind, spelling, base, resolved))

    def command(self, role: DependencyRole, prefix: str, section: CommandSection) -> None:
        # The loader's `_argv` guarantees a non-empty argv for every admitted document;
        # a hand-built empty `CommandSection` is outside this function's contract.
        argv0 = section.argv[0]
        if Path(argv0).is_absolute():
            self.dependencies.append(
                Dependency(role, f"{prefix}.argv[0]", "executable", argv0, "absolute", Path(argv0))
            )
        elif "/" in argv0:
            self.dependencies.append(
                Dependency(
                    role, f"{prefix}.argv[0]", "executable", argv0, "cwd", section.cwd / argv0
                )
            )
        else:
            self.dependencies.append(
                Dependency(role, f"{prefix}.argv[0]", "executable", argv0, "path_lookup", None)
            )
        for index, operand in argv_operands(section.argv):
            candidate = Path(operand)
            base: ResolutionBase = "absolute" if candidate.is_absolute() else "cwd"
            resolved = candidate if candidate.is_absolute() else section.cwd / candidate
            self.dependencies.append(
                Dependency(role, f"{prefix}.argv[{index}]", "argv_operand", operand, base, resolved)
            )
        self.declared_path(role, f"{prefix}.cwd", "cwd", section.cwd, "manifest_dir")
        for index, dependency in enumerate(section.dependency_paths):
            self.declared_path(
                role, f"{prefix}.dependency_paths[{index}]", "file", dependency, "manifest_dir"
            )
        self._forwarded(role, prefix, section.allowed_env)

    def in_process(
        self,
        role: DependencyRole,
        prefix: str,
        section: ChatEditorSection | AgentCliEditorSection,
    ) -> None:
        env_role = cast(EnvironmentRole, role)
        if section.system_prompt is None:
            self.spelling(f"{prefix}.system_prompt")
            self.dependencies.append(
                Dependency(role, f"{prefix}.system_prompt", "prompt", None, "packaged", None)
            )
        else:
            self.declared_path(
                role, f"{prefix}.system_prompt", "prompt", section.system_prompt, "manifest_dir"
            )
        if isinstance(section, ChatEditorSection):
            if section.api_key_env is not None:
                self.environment.append(
                    EnvironmentName(
                        env_role, f"{prefix}.api_key_env", section.api_key_env, "credential"
                    )
                )
            if section.proxy_env is not None:
                self.environment.append(
                    EnvironmentName(
                        env_role, f"{prefix}.proxy_env", section.proxy_env, "credential"
                    )
                )
            return
        if section.agent_bin is None:
            self.spelling(f"{prefix}.agent_bin")
            self.dependencies.append(
                Dependency(role, f"{prefix}.agent_bin", "executable", None, "path_lookup", None)
            )
        else:
            self.declared_path(
                role, f"{prefix}.agent_bin", "executable", section.agent_bin, "manifest_dir"
            )
        self._forwarded(role, prefix, section.allowed_env)

    def model_grouping(
        self, role: DependencyRole, prefix: str, section: ModelGroupingSection
    ) -> None:
        """A grouping model's credential names and prompt, exactly as a `chat`
        role's own: `api_key_env` and `proxy_env` when declared, and the
        packaged default reported as `in_process` does (`/10`'s
        `reflector.grouping` and `/15`'s `chat` Grouper)."""

        env_role = cast(EnvironmentRole, role)
        if section.api_key_env is not None:
            self.environment.append(
                EnvironmentName(
                    env_role, f"{prefix}.api_key_env", section.api_key_env, "credential"
                )
            )
        if section.proxy_env is not None:
            self.environment.append(
                EnvironmentName(env_role, f"{prefix}.proxy_env", section.proxy_env, "credential")
            )
        if section.system_prompt is None:
            self.spelling(f"{prefix}.system_prompt")
            self.dependencies.append(
                Dependency(role, f"{prefix}.system_prompt", "prompt", None, "packaged", None)
            )
        else:
            self.declared_path(
                role, f"{prefix}.system_prompt", "prompt", section.system_prompt, "manifest_dir"
            )

    def _forwarded(self, role: DependencyRole, prefix: str, names: frozenset[str]) -> None:
        env_role = cast(EnvironmentRole, role)  # every command role is an environment role
        for name in sorted(names):
            self.environment.append(
                EnvironmentName(env_role, f"{prefix}.allowed_env", name, "forwarded")
            )

    def role(
        self,
        role: DependencyRole,
        prefix: str,
        section: CommandSection | ChatEditorSection | AgentCliEditorSection,
    ) -> None:
        if isinstance(section, CommandSection):
            self.command(role, prefix, section)
        else:
            self.in_process(role, prefix, section)


def manifest_dependencies(manifest: ManifestV2) -> DependencyInventory:
    """Every declared dependency and environment name of one parsed manifest.

    Pure and deterministic: it reads the parsed sections and
    `manifest.declared` and nothing else. See the module docstring for what
    each record means and `DependencyInventory.boundary` for what it cannot know.
    Native file-loaded campaigns always have a workflow; conformance_manifest
    deliberately does not, and keeps the workflow_not_parsed boundary.
    """

    walk = _Walk(manifest)
    walk.declared_path("seed", "seed_root", "tree", manifest.seed_root, "manifest_dir")
    for index, dep in enumerate(manifest.policy.dependency_paths):
        walk.dependencies.append(
            Dependency(
                "artifacts",
                f"artifacts.dependency_paths[{index}]",
                "file",
                dep,
                "seed_root",
                manifest.seed_root / dep,
            )
        )

    if manifest.linked_from is not None:
        # Lexical only, exactly as `seed_root`/`corpus.path` join against the
        # manifest's directory: no `.resolve()`, since `linked_from` need not
        # exist yet and this module never touches the filesystem.
        walk.declared_path(
            "linked",
            "linked_from",
            "run_dir",
            manifest.path.parent / manifest.linked_from,
            "manifest_dir",
        )

    workflow = manifest.workflow
    if workflow is not None:
        walk.command("scorer", "scorer", workflow.scorer)
        walk.role("editor", "editor", workflow.editor)
        reflector = workflow.reflector
        walk.role("reflector", "reflector", reflector.role)
        if reflector.environment_docs is None:
            walk.spelling("reflector.environment_docs")
        else:
            walk.declared_path(
                "reflector",
                "reflector.environment_docs",
                "documentation",
                reflector.environment_docs,
                "manifest_dir",
            )
        if isinstance(reflector.grouping, CommandGroupingSection):
            walk.command("reflector.grouping", "reflector.grouping", reflector.grouping.command)
        elif isinstance(reflector.grouping, ModelGroupingSection):
            walk.model_grouping("reflector.grouping", "reflector.grouping", reflector.grouping)
        grouper = workflow.grouper
        if isinstance(grouper, CommandGroupingSection):
            walk.command("grouper", "grouper", grouper.command)
        elif isinstance(grouper, ModelGroupingSection):
            walk.model_grouping("grouper", "grouper", grouper)
        elif isinstance(grouper, AgentCliGrouperSection):
            walk.in_process("grouper", "grouper", grouper.role)
        walk.declared_path("corpus", "corpus.path", "file", workflow.corpus_path, "manifest_dir")

    boundary: list[BoundaryCode] = ["runtime_loads_unknown"]
    kinds = {dep.kind for dep in walk.dependencies}
    bases = {dep.base for dep in walk.dependencies}
    if "argv_operand" in kinds:
        boundary.append("argv_operands_heuristic")
    if "path_lookup" in bases:
        boundary.append("path_lookup_at_bind")
    if "packaged" in bases:
        boundary.append("packaged_default")
    if workflow is None:
        boundary.append("workflow_not_parsed")
    if walk.spelling_missing:
        boundary.append("declared_spellings_unavailable")

    return DependencyInventory(
        schema=manifest.schema,
        manifest_path=manifest.path,
        dependencies=tuple(walk.dependencies),
        environment=tuple(sorted(walk.environment, key=lambda e: (e.role, e.field, e.name))),
        boundary=tuple(boundary),
    )
