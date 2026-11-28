"""Explicit schema path relocation; arbitrary strings are never substituted."""
from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .models import EnvironmentDescriptor, PathBinding, PreparationError, SourceRoot, logical_path


def unsupported() -> None:
    raise PreparationError("unsupported_input", "bundle")


_LAUNCH_COMMON = {"schema", "environment_ref", "program_paths", "env", "secret_names", "endpoint", "identity",
                  "policy", "limits", "startup_timeout_seconds", "stop_grace_seconds", "conformance"}
_LAUNCH_KEYS = {"lab-target-launch/1": _LAUNCH_COMMON | {"command"},
                "lab-target-launch/2": _LAUNCH_COMMON | {"agent"},
                "lab-target-launch/3": _LAUNCH_COMMON | {"agent", "reflector_probe", "turn"}}
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_ATTRIBUTE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def local_path(value: Any, base: Path) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        unsupported()
    path = Path(value)
    if ".." in path.parts:
        unsupported()
    return (base / path).absolute()


_PYTHON_SWITCHES = frozenset({"-B", "-E", "-I", "-O", "-OO", "-P", "-s", "-S", "-u", "-v", "-vv", "-q", "-b", "-bb"})


def _python_entrypoint(argv: list[str]) -> tuple[int | None, int]:
    """Return the script index, if any, and the first application argument.

    Only this prefix recognizes Python options. A genuine -c body is opaque;
    -m names an importable identifier, never a source file operand.
    """
    index = 1
    while index < len(argv) and argv[index] in _PYTHON_SWITCHES:
        index += 1
    if index == len(argv):
        unsupported()
    value = argv[index]
    if value in {"-c", "-m"}:
        if index + 1 == len(argv):
            unsupported()
        if value == "-m" and not all(part.isidentifier() for part in argv[index + 1].split(".")):
            unsupported()
        return None, index + 2
    if value == "--":
        index += 1
        if index == len(argv) or argv[index] == "-":
            unsupported()
    elif value.startswith("-"):
        unsupported()
    return index, index + 1


def operand_is_path(relocator: "Relocator", document: Path, pointer: str, value: str, cwd: Path) -> bool:
    """Lab's accepted rule for which application operands are paths (shared by relocation and audit)."""
    explicit = (document, pointer) in relocator.bindings
    candidate = local_path(value, cwd)
    return explicit or candidate.exists() or Path(value).is_absolute() or "/" in value


def _check_application_option(value: str, cwd: Path) -> None:
    """Refuse unsupported attached path operands; retain simple option names."""
    option, separator, attached = value.partition("=")
    if any(character in option for character in ("/", "\\", "\x00")):
        unsupported()
    if not separator and not value.startswith("--") and len(value) > 2:
        attached = value[2:]
    if attached:
        candidate = local_path(attached, cwd)
        if candidate.exists() or Path(attached).is_absolute() or "/" in attached:
            unsupported()


class Relocator:
    """Map caller-declared roots and only pointers in the native/launch tables."""

    def __init__(self, roots: Iterable[SourceRoot], bindings: Iterable[PathBinding]):
        self.roots: dict[str, Path] = {}
        for root in roots:
            if not isinstance(root, SourceRoot):
                unsupported()
            logical_path(root.alias, "bundle")
            if root.alias in self.roots:
                unsupported()
            path = local_path(str(root.source_path), Path.cwd())
            if any(path == other or path in other.parents or other in path.parents for other in self.roots.values()):
                unsupported()
            self.roots[root.alias] = path
        if not self.roots:
            unsupported()
        self.bindings: dict[tuple[Path, str], PathBinding] = {}
        self.used: set[tuple[Path, str]] = set()
        self.relocated: set[tuple[Path, str]] = set()
        for binding in bindings:
            if not isinstance(binding, PathBinding) or binding.root_alias not in self.roots:
                unsupported()
            key = (local_path(str(binding.document), Path.cwd()), binding.pointer)
            if key in self.bindings:
                unsupported()
            self.bindings[key] = binding

    def logical(self, path: Path) -> str:
        matches = [(alias, root) for alias, root in self.roots.items() if path == root or root in path.parents]
        if len(matches) != 1:
            unsupported()
        alias, root = matches[0]
        relative = path.relative_to(root)
        return (Path("inputs") / alias / relative).as_posix()

    def reference(self, document: Path, pointer: str, value: Any, base: Path, *, relative_to: str | None) -> str:
        source = local_path(value, base)
        binding = self.bindings.get((document, pointer))
        if binding is not None:
            bound = self.roots[binding.root_alias] / binding.relative_path
            # Bindings identify the existing operand; they cannot change semantics.
            if source != bound:
                unsupported()
            self.used.add((document, pointer))
        mapped = self.logical(source)
        if not source.exists():
            unsupported()
        self.relocated.add((document, pointer))
        return mapped if relative_to is None else os.path.relpath(mapped, relative_to).replace(os.sep, "/")

    def _command(
        self,
        document: Path,
        section: dict[str, Any],
        pointer_base: str,
        *,
        base: Path,
        destination_base: str,
        pinned: set[str],
    ) -> None:
        if "argv" not in section:
            if section.get("kind") == "agent_cli" and section.get("agent_bin") not in pinned:
                unsupported()
            return
        argv = section["argv"]
        if not isinstance(argv, list) or not argv or argv[0] not in pinned:
            unsupported()
        cwd = local_path(section["cwd"], base)
        section["cwd"] = self.reference(
            document, f"{pointer_base}/cwd", section["cwd"], base,
            relative_to=destination_base,
        )
        destination_cwd = self.logical(cwd)
        dependencies = section.get("dependency_paths")
        if not isinstance(dependencies, list):
            unsupported()
        for index, value in enumerate(dependencies):
            dependencies[index] = self.reference(
                document, f"{pointer_base}/dependency_paths/{index}", value, base,
                relative_to=destination_base,
            )
        script, arguments_start = _python_entrypoint(argv)
        if script is not None:
            argv[script] = self.reference(
                document, f"{pointer_base}/argv/{script}", argv[script], cwd,
                relative_to=destination_cwd,
            )
        for index in range(arguments_start, len(argv)):
            value = argv[index]
            if not isinstance(value, str):
                unsupported()
            if value.startswith("-"):
                _check_application_option(value, cwd)
                continue
            pointer = f"{pointer_base}/argv/{index}"
            if operand_is_path(self, document, pointer, value, cwd):
                argv[index] = self.reference(
                    document, pointer, value, cwd, relative_to=destination_cwd,
                )

    def manifest(self, document: Path, raw: dict[str, Any], environments: tuple[EnvironmentDescriptor, ...]) -> dict[str, Any]:
        result = copy.deepcopy(raw)
        base = document.parent
        destination_base = str(Path(self.logical(document)).parent)
        pinned = {environment.executable for environment in environments}

        def field(container, key, pointer):
            container[key] = self.reference(document, pointer, container[key], base, relative_to=destination_base)

        field(result, "seed_root", "/seed_root")
        field(result["corpus"], "path", "/corpus/path")
        for role in ("scorer", "editor", "reflector"):
            section = result[role]
            if section.get("system_prompt") is not None:
                field(section, "system_prompt", f"/{role}/system_prompt")
            if role == "reflector" and section["environment_docs"] is not None:
                field(section, "environment_docs", "/reflector/environment_docs")
            self._command(
                document, section, f"/{role}", base=base,
                destination_base=destination_base, pinned=pinned,
            )
        if "grouper" in result:  # /15: top-level grouper (spec 2026-10-13 §5.1); chat is core's /15 name for model
            grouping, pointer = result["grouper"], "/grouper"
        else:
            grouping, pointer = result["reflector"]["grouping"], "/reflector/grouping"
        if grouping.get("kind") == "command":
            self._command(
                document, grouping, pointer, base=base,
                destination_base=destination_base, pinned=pinned,
            )
        elif grouping.get("kind") in ("model", "chat") and grouping.get("system_prompt") is not None:
            field(grouping, "system_prompt", f"{pointer}/system_prompt")
        return result

    def evaluation(
        self,
        document: Path,
        raw: dict[str, Any],
        environments: tuple[EnvironmentDescriptor, ...],
    ) -> dict[str, Any]:
        """Relocate only the declared file fields in a Lab evaluation input."""
        result = copy.deepcopy(raw)
        base = document.parent
        destination_base = str(Path(self.logical(document)).parent)
        pinned = {environment.executable for environment in environments}
        result["seed_root"] = self.reference(
            document, "/seed_root", result["seed_root"], base,
            relative_to=destination_base,
        )
        result["corpus"]["path"] = self.reference(
            document, "/corpus/path", result["corpus"]["path"], base,
            relative_to=destination_base,
        )
        self._command(
            document, result["scorer"], "/scorer", base=base,
            destination_base=destination_base, pinned=pinned,
        )
        return result

    def launch(self, document: Path, raw: dict[str, Any], environments: tuple[EnvironmentDescriptor, ...], env: dict[str, str], secret_names: tuple[str, ...], *, policy_entry: str | None = None) -> dict[str, Any]:
        schema = raw.get("schema")
        if not isinstance(schema, str) or schema not in _LAUNCH_KEYS:
            raise PreparationError("unsupported_schema", "bundle")
        if set(raw) != _LAUNCH_KEYS[schema]:
            raise PreparationError("invalid_request", "bundle")
        result = copy.deepcopy(raw)
        endpoint, identity, limits = result["endpoint"], result["identity"], result["limits"]
        if not isinstance(endpoint, dict) or set(endpoint) != {"base_url"} or not isinstance(endpoint["base_url"], str) or not endpoint["base_url"]:
            unsupported()
        if not isinstance(identity, dict) or set(identity) != {"target_id", "contract_digest", "settings_hash", "runtime_ref_digest", "seed_mode"}:
            unsupported()
        if any(not isinstance(identity[key], str) or not identity[key] for key in ("target_id", "seed_mode")):
            unsupported()
        for key in ("contract_digest", "settings_hash", "runtime_ref_digest"):
            value = identity[key]
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value)):
                unsupported()
        if not isinstance(limits, dict) or set(limits) != {"max_instances", "max_concurrent_sessions", "max_input_bytes", "max_output_bytes"}:
            unsupported()
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in limits.values()):
            unsupported()
        for key in ("startup_timeout_seconds", "stop_grace_seconds"):
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                unsupported()
        if result["conformance"] is not None and result["conformance"] not in (
            {"kind": "core-a5-session-public/1"},
            {"kind": "core-session-public/1"},
        ):
            unsupported()
        matches = [environment for environment in environments if environment.digest == result["environment_ref"]]
        if schema == "lab-target-launch/1":
            command = result["command"]
            if not isinstance(command, dict) or set(command) != {"executable", "args"} or not isinstance(command["args"], list):
                unsupported()
            if len(matches) != 1 or command["executable"] != matches[0].executable:
                raise PreparationError("environment_invalid", "bundle")
        else:
            if len(matches) != 1:
                raise PreparationError("environment_invalid", "bundle")
        if schema == "lab-target-launch/3":
            turn = result["turn"]
            if (not isinstance(turn, dict) or set(turn) != {"empty_answer"}
                    or turn["empty_answer"] not in ("target_failed", "ok")):
                unsupported()
            probe = result["reflector_probe"]
            if probe is not None:
                if (not isinstance(probe, dict) or set(probe) != {
                        "environment_ref", "executable", "args", "startup_timeout_seconds",
                        "stop_grace_seconds", "max_restarts"} or not isinstance(probe["args"], list)):
                    unsupported()
                probe_matches = [environment for environment in environments if environment.digest == probe["environment_ref"]]
                if len(probe_matches) != 1 or probe["executable"] != probe_matches[0].executable:
                    raise PreparationError("environment_invalid", "bundle")
                for key in ("startup_timeout_seconds", "stop_grace_seconds"):
                    value = probe[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        unsupported()
                restarts = probe["max_restarts"]
                if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
                    unsupported()
                for index, value in enumerate(probe["args"]):
                    if not isinstance(value, dict) or len(value) != 1:
                        unsupported()
                    if set(value) == {"bundle_path"}:
                        value["bundle_path"] = self.reference(document, f"/reflector_probe/args/{index}/bundle_path",
                                                              value["bundle_path"], document.parent, relative_to=None)
                    elif set(value) == {"probe_port"}:
                        if value["probe_port"] is not None:
                            unsupported()
                    elif set(value) != {"literal"} or not isinstance(value["literal"], str):
                        unsupported()
        if not isinstance(result["env"], dict) or any(key not in env or env[key] != value for key, value in result["env"].items()):
            raise PreparationError("invalid_request", "bundle")
        names = result["secret_names"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names) or not set(names) <= set(secret_names):
            raise PreparationError("invalid_request", "bundle")
        result["secret_names"] = sorted(names)
        if not isinstance(result["program_paths"], list):
            unsupported()
        for index, value in enumerate(result["program_paths"]):
            result["program_paths"][index] = self.reference(document, f"/program_paths/{index}", value, document.parent, relative_to=None)
        if policy_entry is None:
            result["policy"] = self.reference(document, "/policy", result["policy"], document.parent, relative_to=None)
        else:
            logical_path(policy_entry, "bundle")
            local_path(result["policy"], document.parent)
            result["policy"] = policy_entry
        if schema == "lab-target-launch/1":
            for index, value in enumerate(command["args"]):
                if not isinstance(value, dict) or len(value) != 1:
                    unsupported()
                if set(value) == {"bundle_path"}:
                    value["bundle_path"] = self.reference(document, f"/command/args/{index}/bundle_path", value["bundle_path"], document.parent, relative_to=None)
                elif set(value) != {"literal"} or not isinstance(value["literal"], str):
                    unsupported()
        else:
            agent = result["agent"]
            if (not isinstance(agent, dict) or set(agent) != {"module", "attribute", "import_root"}
                    or not isinstance(agent["module"], str) or not _MODULE.fullmatch(agent["module"])
                    or not isinstance(agent["attribute"], str) or not _ATTRIBUTE.fullmatch(agent["attribute"])):
                unsupported()
            if agent["import_root"] is not None:
                if not isinstance(agent["import_root"], str):
                    unsupported()
                agent["import_root"] = self.reference(document, "/agent/import_root", agent["import_root"],
                                                       document.parent, relative_to=None)
        return result

    def finish(self) -> None:
        if self.used != set(self.bindings):
            unsupported()
