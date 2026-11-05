"""Construct role, scorer and environment bindings from manifest sections."""

from __future__ import annotations

import importlib.util
import ipaddress
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from evolution.eval.child_env import _RUNTIME_ENV_KEYS, minimal_child_env
from evolution.llm.agent_backend import CURATED_ENV_NAMES, agent_tool_for
from evolution.prompts import (
    TREE_AGENT_EDITOR_SYSTEM,
    TREE_AGENT_REFLECTOR_SYSTEM,
    TREE_AGENT_REFLECTOR_SYSTEM_V3,
    TREE_EDITOR_SYSTEM,
    TREE_EDITOR_SYSTEM_V2,
    TREE_REFLECTOR_SYSTEM,
    TREE_REFLECTOR_SYSTEM_V3,
    TREE_REFLECTOR_SYSTEM_V4,
    TREE_REFLECTOR_SYSTEM_V5,
)
from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.execution.config import CommandSection, EnvironmentSection
from evolution.tree.execution.interaction import (
    Environment,
    ScriptedEnvironment,
    StaticInputEnvironment,
)
from evolution.tree.execution.output_admission import (
    EvolveRefused,
)
from evolution.tree.execution.rollout_scoring import SubprocessScorer
from evolution.tree.roles.config import (
    AgentCliEditorSection,
    AgentCliGrouperSection,
    ChatEditorSection,
    CommandGroupingSection,
    EditorSection,
    GrouperSection,
    ModelGroupingSection,
    ReflectorSection,
)
from evolution.tree.roles.editor.agent_editor import AgentCliEditor
from evolution.tree.roles.editor.chat_editor import ChatCredentials, ChatEditor
from evolution.tree.roles.editor.subprocess_editor import SubprocessEditor
from evolution.tree.roles.editor_contracts import CandidateEditor
from evolution.tree.roles.reflector.agent_reflector import AgentCliReflector
from evolution.tree.roles.reflector.chat_reflector import ChatReflector
from evolution.tree.roles.reflector.subprocess_reflector import SubprocessReflector
from evolution.tree.roles.reflector_contracts import CandidateReflector
from evolution.tree.roles.seals import FileSeal, seal_program, seal_prompt

__all__ = [
    "BindingError",
    "bound_child_environment",
    "build_editor",
    "build_environment",
    "build_grouper_command",
    "build_grouping_command",
    "build_reflector",
    "build_scorer",
    "editor_environment_names",
    "editor_environment_paths",
    "editor_environment_values",
    "editor_program_paths",
    "grouper_agent_seal",
    "grouper_environment_names",
    "grouper_environment_paths",
    "grouper_environment_values",
    "grouper_model_credentials",
    "grouper_program_paths",
    "grouping_model_credentials",
    "reflector_environment_names",
    "reflector_environment_paths",
    "reflector_environment_values",
    "reflector_program_paths",
]


class BindingError(ValueError):
    """A manifest section cannot be bound to a kernel object.

    The public refusal is `code` and the dotted manifest `path`; `detail` holds
    the underlying message, which may name a resolved filesystem path, and is
    deliberately absent from `str()` and from the exception's args.
    """

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(f"{code} at {path}")
        self.code = code
        self.path = path
        self.detail = detail


# The `ValueError` messages `CommandBinding` and `SubprocessScorer` raise, in
# full or as the stable prefix of a message that ends in an OS error, paired
# with the manifest field each one indicts. An explicit table, not a heuristic:
# a message this file does not know is reported against the section itself
# rather than against a guessed field.
_FIELD_FOR_MESSAGE: tuple[tuple[str, str], ...] = (
    ("argv must contain nonblank strings", "argv"),
    ("executable not found", "argv"),
    ("module command is not supported", "argv"),
    ("cwd is invalid", "cwd"),
    ("cwd must be a directory", "cwd"),
    ("dependency is invalid", "dependency_paths"),
    ("dependencies must be files", "dependency_paths"),
    ("dependency_paths must be unique", "dependency_paths"),
    ("allowed environment names must be nonblank", "allowed_env"),
    ("scorer timeout_seconds must be positive and finite", "timeout_seconds"),
)


def _field_for(message: str, section: str) -> str:
    """The dotted manifest path a bound command's `ValueError` indicts."""

    for prefix, field in _FIELD_FOR_MESSAGE:
        if message.startswith(prefix):
            return f"{section}.{field}"
    return section


def _bind_command(
    section: CommandSection,
    *,
    seed_root: Path,
    run_dir: Path,
    name: str,
    environ: Mapping[str, str] | None = None,
) -> CommandBinding:
    """Bind one declared command, keeping `program_inside_seed` its own code.

    `excluded_roots` is the seed tree and the run directory: both hold code the
    campaign writes -- the seed is what evolves, and the run directory holds the
    Attempt workspaces -- so a `PYTHONPATH` entry under either is dropped from
    the child environment before it is sealed. The binding announces the drop on
    `dropped_pythonpath`; it is a best-effort guard under the amended PDR-0005,
    not a claim about what the program loads.

    `environ` is the binding's `base_env`: the one mapping it reads, so the
    child is spawned with the values the run's secret set was computed from.
    `None` reads the process environment, as a direct caller always has.
    """

    try:
        return CommandBinding(
            argv=tuple(section.argv),
            cwd=Path(section.cwd),
            dependency_paths=tuple(section.dependency_paths),
            allowed_env=frozenset(section.allowed_env),
            seed_root=Path(seed_root),
            excluded_roots=(Path(seed_root), Path(run_dir)),
            base_env=environ,
        )
    except ValueError as exc:
        message = str(exc)
        if message == "program_inside_seed":
            raise BindingError("program_inside_seed", name) from exc
        raise BindingError(f"{name}_invalid", _field_for(message, name), detail=message) from exc


def build_grouping_command(
    section: CommandSection,
    *,
    seed_root: Path,
    run_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> CommandBinding:
    """Bind the optional adaptive grouping command with normal command seals."""

    return _bind_command(
        section, seed_root=seed_root, run_dir=run_dir, name="reflector.grouping", environ=environ
    )


def build_grouper_command(
    section: CommandSection,
    *,
    seed_root: Path,
    run_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> CommandBinding:
    """Bind a `/15` `command` Grouper with normal command seals; its refusals
    read `grouper_invalid at grouper.*` (A§3.3)."""

    try:
        return _bind_command(
            section, seed_root=seed_root, run_dir=run_dir, name="grouper", environ=environ
        )
    except BindingError as exc:
        if exc.code == "program_inside_seed":
            raise
        # The new Grouper contract makes details value-free too. Keep the
        # underlying command routes' historical details for the older roles.
        raise BindingError(exc.code, exc.path, detail="command could not be bound") from None
    except OSError:
        # A program or dependency can disappear during the seal, after its
        # path was resolved. There is no known field owner at this boundary.
        raise BindingError(
            "grouper_invalid", "grouper", detail="command could not be bound"
        ) from None


def build_environment(section: EnvironmentSection) -> Environment:
    """The kernel Environment the `environment` section declares."""

    if section.kind == "scripted":
        return ScriptedEnvironment()
    if section.kind == "static":
        try:
            return StaticInputEnvironment(section.input_text_key)
        except ValueError as exc:
            raise BindingError(
                "environment_invalid", "environment.input_text_key", detail=str(exc)
            ) from exc
    raise BindingError("environment_invalid", "environment.kind")


def build_scorer(
    section: CommandSection,
    *,
    seed_root: Path,
    run_dir: Path,
    environ: Mapping[str, str] | None = None,
    admitted_hygiene: frozenset[str] = frozenset(),
    accepts_diagnostics: bool = False,
) -> SubprocessScorer:
    """The `SubprocessScorer` the `scorer` section declares, bound and seed-checked.

    `trace_root` is `<run_dir>/scorer-traces`; the scorer canonicalizes it here
    and creates it on its first run, so this call writes nothing. `environ` is
    the environment the command is bound from, as for `build_editor`; `None`
    reads the process environment. `admitted_hygiene` is the set of hygiene
    instance digests the operator accepted (PDR-0010), which the scorer carries
    into its point-of-use re-admission of `trace_root`; empty binds a scorer
    that refuses exactly what it refused before decisions existed.
    `accepts_diagnostics` admits the scorer's `diagnostics` key (a `/9`
    manifest only, ADR-0021); `False` keeps it an unknown key.
    """

    if section.timeout_seconds is None:
        raise BindingError("scorer_invalid", "scorer.timeout_seconds")
    binding = _bind_command(
        section, seed_root=seed_root, run_dir=run_dir, name="scorer", environ=environ
    )
    try:
        return SubprocessScorer(
            argv=tuple(section.argv),
            cwd=Path(section.cwd),
            timeout_seconds=section.timeout_seconds,
            trace_root=Path(run_dir) / "scorer-traces",
            dependency_paths=tuple(section.dependency_paths),
            allowed_env=frozenset(section.allowed_env),
            binding=binding,
            admitted_hygiene=admitted_hygiene,
            accepts_diagnostics=accepts_diagnostics,
        )
    except ValueError as exc:
        message = str(exc)
        raise BindingError("scorer_invalid", _field_for(message, "scorer"), detail=message) from exc


def editor_environment_names(section: EditorSection) -> frozenset[str]:
    """The environment variable names the editor section *declares*.

    `api_key_env` and `proxy_env` for `chat`, `allowed_env` for the other two.
    Names, never values: this is what a pre-run diagnostic may print beside the
    scorer's `allowed_env` (design §9), and it is a pure function of the
    document, so it needs no environment at all.

    The curated names an `agent_cli` child additionally receives are *not*
    here. They are the binding's doing, not the operator's declaration, and
    naming them in a diagnostic about the manifest would misreport the
    document; their values still join the run's secret set through
    :func:`editor_environment_values`.
    """

    if isinstance(section, ChatEditorSection):
        names = {section.api_key_env} if section.api_key_env is not None else set()
        if section.proxy_env is not None:
            names.add(section.proxy_env)
        return frozenset(names)
    return section.allowed_env


def editor_environment_values(
    section: EditorSection, environ: Mapping[str, str], *, role: str = "editor"
) -> tuple[str, ...]:
    """The values the run's secret set must carry for this editor (design §9).

    * `chat`: the API key when `api_key_env` is declared (an undeclared one is
      an unauthenticated role and contributes nothing, #290) and, when
      `proxy_env` is declared, the proxy URL.
    * `agent_cli`: every value of the **bound child environment** whose name is
      outside `child_env._RUNTIME_ENV_KEYS` -- the curated credentials the
      backend forwards to the CLI (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, ...)
      as well as the operator's `allowed_env`. One mapping is the authority for
      what the CLI receives and for what the transcript and the journal
      redact, so there is no second list to drift. In `unsafe` mode that
      mapping is the whole operator environment and the same derivation
      applies to it: the cost of the operator's declaration.
    * `subprocess`: the forwarded `allowed_env` values, W1's own rule
      unchanged, so a `/2` document with a `subprocess` editor arms exactly the
      secret set it armed before this dispatch existed (rule R1).

    A declared name that is absent or has an empty value is refused here, before
    any write: an in-process editor cannot call its endpoint without its key,
    and this is the in-process analogue of W1's secret floor, stricter on
    purpose (design §3). A `subprocess` editor keeps W1's laxer rule -- a
    declared name the environment does not have simply contributes nothing --
    because a program may legitimately run without a forwarded name.

    A `chat` editor's proxy value is *validated* here too, not only where it is
    bound: this function is the environment step, and the refusal order puts
    every refusal that needs no filesystem ahead of the path check. Validating
    it at `build_editor` alone would report a malformed proxy URL only after
    `check_path_separation` and `run_dir_state` had already had their say.

    The caller applies the secret floor (`evolve._MIN_SECRET_LENGTH`) to these
    values exactly as it does to a scorer's: a value too short to be a
    credential is not made a redaction needle, whichever kind produced it.

    `role` names whose section this is in every refusal this function raises:
    `"editor"` (the default, byte-identical to every existing call site) or
    `"reflector"`, so `EvolveRefused`/`BindingError` read `{role}_env_missing
    at {role}.api_key_env` / `{role}_invalid at {role}.proxy_env` rather than
    always reporting a reflector's declared key under the editor's own
    vocabulary. `reflector_environment_values` is this function called with
    `role="reflector"`.
    """

    if isinstance(section, CommandSection):
        return tuple(environ[name] for name in sorted(section.allowed_env) if name in environ)
    if isinstance(section, ChatEditorSection):
        env_missing = f"{role}_env_missing"
        # An undeclared key (#290) is an unauthenticated role: no refusal and no
        # value, so it adds nothing to the run's secret set (design C6).
        values = (
            []
            if section.api_key_env is None
            else [
                _required_env(environ, section.api_key_env, f"{role}.api_key_env", code=env_missing)
            ]
        )
        if section.proxy_env is not None:
            proxy = _required_env(environ, section.proxy_env, f"{role}.proxy_env", code=env_missing)
            values.append(
                _validated_proxy_url(proxy, code=f"{role}_invalid", field_path=f"{role}.proxy_env")
            )
        return tuple(values)
    for name in sorted(section.allowed_env):
        _required_env(environ, name, f"{role}.allowed_env", code=f"{role}_env_missing")
    bound = bound_child_environment(section, environ)
    return tuple(value for name, value in sorted(bound.items()) if name not in _RUNTIME_ENV_KEYS)


def editor_environment_paths(
    section: EditorSection, environ: Mapping[str, str], *, role: str = "editor"
) -> tuple[str, ...]:
    """The manifest path of each value :func:`editor_environment_values` returns.

    Index for index (#287), from the same `environ`: `<role>.allowed_env.<NAME>`
    for a forwarded name, `<role>.api_key_env` and `<role>.proxy_env` for a
    `chat` role's two single-name keys (each only when declared, as its value
    is), and the bare `role` for a value of an
    `agent_cli` child's bound environment that the manifest does not name (a
    curated credential, or any value in `unsafe` mode): that name is the
    binding's, not the document's, so no manifest path spells it. Names and
    paths only, never a value; it refuses nothing, because the value function
    has already read the same mapping.
    """

    if isinstance(section, CommandSection):
        return tuple(
            f"{role}.allowed_env.{name}" for name in sorted(section.allowed_env) if name in environ
        )
    if isinstance(section, ChatEditorSection):
        paths = [] if section.api_key_env is None else [f"{role}.api_key_env"]
        if section.proxy_env is not None:
            paths.append(f"{role}.proxy_env")
        return tuple(paths)
    bound = bound_child_environment(section, environ)
    return tuple(
        f"{role}.allowed_env.{name}" if name in section.allowed_env else role
        for name in sorted(bound)
        if name not in _RUNTIME_ENV_KEYS
    )


def bound_child_environment(
    section: AgentCliEditorSection, environ: Mapping[str, str]
) -> dict[str, str]:
    """The environment an `agent_cli` child receives, computed once at bind (§3).

    `safe`: the runtime essentials plus the backend's curated names for this
    CLI plus the operator's `allowed_env`. `unsafe`: the whole operator
    environment, by the operator's declaration in a trusted document -- which
    also lets the CLI read files no environment rule covers (`docs/manifest-v2.md`).
    A copy either way: the binding never hands a child a live view of the
    operator's mapping.
    """

    if section.permission == "unsafe":
        return dict(environ)
    allow = CURATED_ENV_NAMES(agent_tool_for(section.model)) | section.allowed_env
    return minimal_child_env(environ, allow=allow)


def editor_program_paths(
    section: EditorSection, environ: Mapping[str, str], *, resolve: bool = True
) -> tuple[Path, ...]:
    """The editor's own paths `check_path_separation` must hold apart (design §9).

    A `subprocess` editor contributes its working directory, as it always has.
    An in-process editor contributes the files the binding seals: the declared
    `system_prompt` (a packaged default is not a path and contributes nothing)
    and, for `agent_cli`, the resolved CLI executable -- declared, or resolved
    from the bound `PATH` here, which is where `editor_program_missing` is
    raised so that it precedes both the path check and any write.

    `resolve=False` (#248, `evolve.check_corpus_placement`): declared paths
    only. An `agent_cli` section without `agent_bin` contributes its declared
    prompt and no program: the CLI is not looked up on `PATH`, so
    `editor_program_missing` is never raised.
    """

    if isinstance(section, CommandSection):
        return (section.cwd,)
    if isinstance(section, ChatEditorSection):
        return () if section.system_prompt is None else (section.system_prompt,)
    if not resolve:
        declared = () if section.agent_bin is None else (Path(section.agent_bin),)
        return declared if section.system_prompt is None else (section.system_prompt, *declared)
    program = _agent_program(section, bound_child_environment(section, environ))
    return (program,) if section.system_prompt is None else (section.system_prompt, program)


def build_editor(
    section: EditorSection,
    *,
    seed_root: Path,
    run_dir: Path,
    environ: Mapping[str, str],
    secrets: Sequence[str] = (),
    family15: bool = False,
    admitted_hygiene: frozenset[str] = frozenset(),
    role_resilience: bool = False,
    plan_correction: bool = False,
) -> CandidateEditor:
    """The Candidate editor the `editor` section declares, bound and seed-checked.

    `log_root` is `<run_dir>/editor-logs` for every kind; the editor
    canonicalizes it here and creates it on its first `edit`, so this call
    writes nothing. `environ` is the caller's environment, passed explicitly so
    the read is testable and happens once, before the first write; `secrets` is
    the run's secret set, which the caller has already computed from this same
    section (the editor redacts its own transcript with it, and C1's journal
    refuses an envelope that carries one of its members).

    `subprocess` binds exactly as it did at the E1-editor review base -- the
    same `CommandBinding`, the same `EditorIdentity` -- which is half of rule
    R1's promise that adding a kind changes no existing document. The section's
    `timeout_seconds` is `None` for an editor (the deadline is C1's, ADR-0001)
    and is bound into neither the command nor the identity.

    `admitted_hygiene` is the operator's accepted hygiene findings (PDR-0010).
    Every kind carries it for the same reason: each re-admits `log_root` at the
    point of use, the `subprocess` kind itself and the two in-process kinds
    through `open_attempt_log`.

    `role_resilience` (PDR-0019, ADR-0023) is true only for a `/11` manifest's
    `chat` and `agent_cli` kinds: the caller (`evolve`) derives it from
    `manifest.schema`, not from any key on this section, so a family-10
    branch is always selected by campaign family. It has no effect on the
    `subprocess` kind.
    """

    if isinstance(section, CommandSection):
        binding = _bind_command(
            section, seed_root=seed_root, run_dir=run_dir, name="editor", environ=environ
        )
        return SubprocessEditor(
            binding=binding,
            log_root=Path(run_dir) / "editor-logs",
            admitted_hygiene=admitted_hygiene,
        )
    log_root = Path(run_dir) / "editor-logs"
    if isinstance(section, ChatEditorSection):
        api_key = (
            None
            if section.api_key_env is None
            else _required_env(environ, section.api_key_env, "editor.api_key_env")
        )
        proxy_url: str | None = None
        if section.proxy_env is not None:
            # Re-validated rather than trusted: `evolve` has already run this
            # through `editor_environment_values`, but a direct caller of
            # `build_editor` has not, and the same refusal must reach both.
            proxy_url = _validated_proxy_url(
                _required_env(environ, section.proxy_env, "editor.proxy_env")
            )
        return ChatEditor(
            family15=family15,
            section=section,
            credentials=ChatCredentials(api_key=api_key, proxy_url=proxy_url),
            prompt=_sealed_prompt(
                section.system_prompt, TREE_EDITOR_SYSTEM_V2 if family15 else TREE_EDITOR_SYSTEM
            ),
            log_root=log_root,
            secrets=tuple(secrets),
            admitted_hygiene=admitted_hygiene,
            role_resilience=role_resilience,
            plan_correction=plan_correction,
        )
    for name in sorted(section.allowed_env):
        _required_env(environ, name, "editor.allowed_env")
    child_env = bound_child_environment(section, environ)
    program = _agent_program(section, child_env)
    try:
        sealed = seal_program(program)
    except OSError as exc:
        raise BindingError("editor_invalid", "editor.agent_bin", detail=str(exc)) from exc
    return AgentCliEditor(
        family15=family15,
        section=section,
        program=sealed,
        prompt=_sealed_prompt(section.system_prompt, TREE_AGENT_EDITOR_SYSTEM),
        child_env=child_env,
        log_root=log_root,
        secrets=tuple(secrets),
        admitted_hygiene=admitted_hygiene,
        role_resilience=role_resilience,
    )


def reflector_environment_names(section: ReflectorSection) -> frozenset[str]:
    """The environment variable names the reflector section *declares* (spec §3.3).

    `editor_environment_names`'s own answer over `section.role`: the two
    sections share the same three shapes, so naming their declared environment
    is one function's job either way.
    """

    return editor_environment_names(section.role)


def reflector_environment_values(
    section: ReflectorSection, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """The values the run's secret set must carry for this reflector (spec §3.3).

    `editor_environment_values`'s own answer over `section.role`, for the same
    reason `reflector_environment_names` delegates -- but called with
    `role="reflector"`, so a refusal here reads `reflector_env_missing at
    reflector.api_key_env` / `reflector_invalid at reflector.proxy_env`, never
    the editor's own vocabulary for a key the manifest declared under
    `reflector`. A `model` grouping's key and proxy follow
    (`grouping_model_credentials`): the grouping model is another role reading
    operator credentials, and one redaction set covers every journal.
    """

    values = editor_environment_values(section.role, environ, role="reflector")
    credentials = grouping_model_credentials(section, environ)
    if credentials is not None:
        if credentials.api_key is not None:
            values += (credentials.api_key,)
        if credentials.proxy_url is not None:
            values += (credentials.proxy_url,)
    return values


def reflector_environment_paths(
    section: ReflectorSection, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """The manifest path of each value :func:`reflector_environment_values`
    returns, index for index (#287): `editor_environment_paths` under
    `reflector`, then a `model` grouping's `reflector.grouping.api_key_env` and
    `reflector.grouping.proxy_env`."""

    paths = editor_environment_paths(section.role, environ, role="reflector")
    if isinstance(section.grouping, ModelGroupingSection):
        if section.grouping.api_key_env is not None:
            paths += ("reflector.grouping.api_key_env",)
        if section.grouping.proxy_env is not None:
            paths += ("reflector.grouping.proxy_env",)
    return paths


def _provider_sdks_installed() -> bool:
    """Whether the `tree` extra's `openai` and `httpx` are importable.

    `find_spec` locates without importing, so a core install is detected at
    bind; a finder that raises is read as absent, never as a traceback.
    """

    for name in ("openai", "httpx"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def _model_grouping_credentials(
    grouping: ModelGroupingSection, environ: Mapping[str, str], *, prefix: str, role: str
) -> ChatCredentials:
    """A grouping model's key and proxy under `prefix`, refused as `role`'s.

    Read exactly as a `chat` Reflector's are: a missing value is
    `{role}_env_missing at {prefix}.api_key_env` (or `.proxy_env`), a malformed
    proxy `{role}_invalid`; an undeclared `api_key_env` is `api_key=None`, an
    unauthenticated grouping model (#290). A core install without the `tree` extra cannot
    build the grouping client: that is `{role}_invalid at {prefix}.backend`,
    refused here -- the first bind step, before any write -- rather than an
    `ImportError` after `grouping_started` is journaled, which every resume
    would repeat.
    """

    if not _provider_sdks_installed():
        raise BindingError(
            f"{role}_invalid",
            f"{prefix}.backend",
            detail="install evo-skills[tree] to use a model grouping",
        )
    api_key = (
        None
        if grouping.api_key_env is None
        else _required_env(
            environ, grouping.api_key_env, f"{prefix}.api_key_env", code=f"{role}_env_missing"
        )
    )
    proxy_url: str | None = None
    if grouping.proxy_env is not None:
        proxy_url = _validated_proxy_url(
            _required_env(
                environ, grouping.proxy_env, f"{prefix}.proxy_env", code=f"{role}_env_missing"
            ),
            code=f"{role}_invalid",
            field_path=f"{prefix}.proxy_env",
        )
    return ChatCredentials(api_key=api_key, proxy_url=proxy_url)


def grouping_model_credentials(
    section: ReflectorSection, environ: Mapping[str, str]
) -> ChatCredentials | None:
    """The `/10`-`/14` grouping model's key and proxy, when `reflector.grouping`
    is `model` (ADR-0022); `None` for every other grouping kind. Refusals are
    the Reflector's (`reflector_env_missing`, `reflector_invalid`) at
    `reflector.grouping.*`, byte for byte as before the Grouper existed."""

    grouping = section.grouping
    if not isinstance(grouping, ModelGroupingSection):
        return None
    return _model_grouping_credentials(
        grouping, environ, prefix="reflector.grouping", role="reflector"
    )


def grouper_model_credentials(
    section: GrouperSection, environ: Mapping[str, str]
) -> ChatCredentials | None:
    """A `/15` `chat` Grouper's key and proxy (A§3.3, R19): the grouping
    model's rules under `grouper.*` and the `grouper_*` codes. `None` for the
    other three kinds."""

    if not isinstance(section, ModelGroupingSection):
        return None
    return _model_grouping_credentials(section, environ, prefix="grouper", role="grouper")


def reflector_program_paths(
    section: ReflectorSection, environ: Mapping[str, str], *, resolve: bool = True
) -> tuple[Path, ...]:
    """The reflector's own paths `check_path_separation` must hold apart (spec §9).

    `editor_program_paths`'s own answer over `section.role`, plus the declared
    `environment_docs` directory when one is set: a directory outside the seed
    that the Reflector reads, so it is held apart from the seed and the run
    directory exactly as the role's own program and prompt are.
    """

    paths = editor_program_paths(section.role, environ, resolve=resolve)
    return paths if section.environment_docs is None else (*paths, section.environment_docs)


def grouper_environment_names(section: GrouperSection) -> frozenset[str]:
    """The environment names the `/15` Grouper declares: a `chat` Grouper's key
    and proxy, a `command` or `agent_cli` Grouper's `allowed_env`."""

    if isinstance(section, ModelGroupingSection):
        names = {section.api_key_env} if section.api_key_env is not None else set()
        if section.proxy_env is not None:
            names.add(section.proxy_env)
        return frozenset(names)
    if isinstance(section, CommandGroupingSection):
        return section.command.allowed_env
    if isinstance(section, AgentCliGrouperSection):
        return section.role.allowed_env
    return frozenset()


def grouper_environment_values(
    section: GrouperSection, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """The values the run's secret set must carry for the `/15` Grouper (R19).

    * `chat`: the key and the proxy URL, each when declared
      (`grouper_model_credentials`, which refuses an absent value).
    * `command`: the forwarded `allowed_env` values the environment has, W1's
      laxer rule for a program. Older command grouping contributes the same
      values through `evolve._run_secrets` (issue #185).
    * `agent_cli`: the bound child environment outside the runtime names, as
      for the agent Editor, refused as `grouper_env_missing at
      grouper.allowed_env` when a declared name is absent.
    * `fields`: nothing.
    """

    if isinstance(section, ModelGroupingSection):
        credentials = grouper_model_credentials(section, environ)
        assert credentials is not None
        return tuple(
            value for value in (credentials.api_key, credentials.proxy_url) if value is not None
        )
    if isinstance(section, CommandGroupingSection):
        return tuple(
            environ[name] for name in sorted(section.command.allowed_env) if name in environ
        )
    if isinstance(section, AgentCliGrouperSection):
        return editor_environment_values(section.role, environ, role="grouper")
    return ()


def grouper_environment_paths(
    section: GrouperSection, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """The manifest path of each value :func:`grouper_environment_values`
    returns, index for index (#287): `grouper.api_key_env` and
    `grouper.proxy_env`, `grouper.allowed_env.<NAME>` for a `command` Grouper,
    `editor_environment_paths` under `grouper` for an `agent_cli` one."""

    if isinstance(section, ModelGroupingSection):
        declared = (
            ("grouper.api_key_env", section.api_key_env),
            ("grouper.proxy_env", section.proxy_env),
        )
        return tuple(path for path, name in declared if name is not None)
    if isinstance(section, CommandGroupingSection):
        return tuple(
            f"grouper.allowed_env.{name}"
            for name in sorted(section.command.allowed_env)
            if name in environ
        )
    if isinstance(section, AgentCliGrouperSection):
        return editor_environment_paths(section.role, environ, role="grouper")
    return ()


def _grouper_agent_program(section: AgentCliGrouperSection, environ: Mapping[str, str]) -> Path:
    return _agent_program(
        section.role,
        bound_child_environment(section.role, environ),
        code="grouper_program_missing",
        field_path="grouper.agent_bin",
    )


def grouper_program_paths(
    section: GrouperSection, environ: Mapping[str, str], *, resolve: bool = True
) -> tuple[Path, ...]:
    """The `/15` Grouper's own paths `check_path_separation` must hold apart:
    a `command` Grouper's working directory, a declared prompt, and an
    `agent_cli` Grouper's resolved CLI (`grouper_program_missing` when none
    resolves). `resolve=False` contributes declared paths only, without a
    CLI lookup on `PATH`."""

    if isinstance(section, CommandGroupingSection):
        return (section.command.cwd,)
    if isinstance(section, ModelGroupingSection):
        return () if section.system_prompt is None else (section.system_prompt,)
    if isinstance(section, AgentCliGrouperSection):
        if not resolve:
            prompt = section.role.system_prompt
            declared = () if section.role.agent_bin is None else (Path(section.role.agent_bin),)
            return declared if prompt is None else (prompt, *declared)
        program = _grouper_agent_program(section, environ)
        prompt = section.role.system_prompt
        return (program,) if prompt is None else (prompt, program)
    return ()


def grouper_agent_seal(section: AgentCliGrouperSection, environ: Mapping[str, str]) -> FileSeal:
    """The `agent_cli` Grouper's program seal (A§3.4): the resolved CLI's bytes."""

    program = _grouper_agent_program(section, environ)
    try:
        return seal_program(program)
    except OSError as exc:
        raise BindingError(
            "grouper_invalid", "grouper.agent_bin", detail="program could not be sealed"
        ) from exc


def build_reflector(
    section: ReflectorSection,
    *,
    seed_root: Path,
    run_dir: Path,
    environ: Mapping[str, str],
    secrets: Sequence[str] = (),
    family15: bool = False,
    admitted_hygiene: frozenset[str] = frozenset(),
    model_grouping_family: bool = False,
    role_resilience: bool = False,
    bounds_repair: bool = False,
    result_repair: bool = False,
) -> CandidateReflector:
    """The Candidate reflector the `reflector` section declares, bound and seed-checked.

    A faithful mirror of `build_editor` (spec §3.3): `log_root` is
    `<run_dir>/reflector-logs` for every kind, created by the reflector on its
    first `reflect` rather than here, so this call writes nothing. The three
    role shapes dispatch exactly as `build_editor`'s do, over `section.role`
    rather than an `editor` section; the refusal codes are the editor's own
    three, renamed at the `path`/`code` level: `reflector_invalid`,
    `reflector_env_missing`, `reflector_program_missing`. `admitted_hygiene`
    reaches `reflector-logs` by the editor's own three routes; the Reflector's
    *grouping* command is bound elsewhere, and takes the same set through
    `AdaptiveCampaignRuntimeConfig`.

    `model_grouping_family` selects the `_V3` packaged default for current
    campaigns (ADR-0022), framing a selected group's `description` as an
    investigation question, not a causal verdict. Direct callers retain the
    base packaged default when the flag is False. An operator-declared
    `section.role.system_prompt` is always sealed unchanged (`seal_prompt`).

    `result_repair` (`/16`, #318, ADR-0048) builds a `chat` Reflector that
    checks each `finish_reflection` in its own conversation and selects the
    `_V4` packaged prompt that states that contract; the other kinds ignore it.
    """

    role = section.role
    chat_prompt = (
        TREE_REFLECTOR_SYSTEM_V5
        if family15 and result_repair
        else TREE_REFLECTOR_SYSTEM_V4
        if result_repair
        else TREE_REFLECTOR_SYSTEM_V3
        if model_grouping_family
        else TREE_REFLECTOR_SYSTEM
    )
    agent_prompt = (
        TREE_AGENT_REFLECTOR_SYSTEM_V3 if model_grouping_family else TREE_AGENT_REFLECTOR_SYSTEM
    )
    log_root = Path(run_dir) / "reflector-logs"
    if isinstance(role, CommandSection):
        binding = _bind_command(
            role, seed_root=seed_root, run_dir=run_dir, name="reflector", environ=environ
        )
        return SubprocessReflector(
            binding=binding, log_root=log_root, admitted_hygiene=admitted_hygiene
        )
    if isinstance(role, ChatEditorSection):
        api_key = (
            None
            if role.api_key_env is None
            else _required_env(
                environ, role.api_key_env, "reflector.api_key_env", code="reflector_env_missing"
            )
        )
        proxy_url: str | None = None
        if role.proxy_env is not None:
            proxy_url = _validated_proxy_url(
                _required_env(
                    environ,
                    role.proxy_env,
                    "reflector.proxy_env",
                    code="reflector_env_missing",
                ),
                code="reflector_invalid",
                field_path="reflector.proxy_env",
            )
        return ChatReflector(
            family15=family15,
            section=role,
            credentials=ChatCredentials(api_key=api_key, proxy_url=proxy_url),
            prompt=_sealed_prompt(
                role.system_prompt,
                chat_prompt,
                code="reflector_invalid",
                field_path="reflector.system_prompt",
            ),
            log_root=log_root,
            secrets=tuple(secrets),
            admitted_hygiene=admitted_hygiene,
            role_resilience=role_resilience,
            bounds_repair=bounds_repair,
            result_repair=result_repair,
        )
    for name in sorted(role.allowed_env):
        _required_env(environ, name, "reflector.allowed_env", code="reflector_env_missing")
    child_env = bound_child_environment(role, environ)
    program = _agent_program(
        role, child_env, code="reflector_program_missing", field_path="reflector.agent_bin"
    )
    try:
        sealed = seal_program(program)
    except OSError as exc:
        raise BindingError("reflector_invalid", "reflector.agent_bin", detail=str(exc)) from exc
    return AgentCliReflector(
        family15=family15,
        section=role,
        program=sealed,
        prompt=_sealed_prompt(
            role.system_prompt,
            agent_prompt,
            code="reflector_invalid",
            field_path="reflector.system_prompt",
        ),
        child_env=child_env,
        log_root=log_root,
        secrets=tuple(secrets),
        admitted_hygiene=admitted_hygiene,
    )


def _required_env(
    environ: Mapping[str, str], name: str, path: str, *, code: str = "editor_env_missing"
) -> str:
    """One declared environment value, or `code` at its dotted `path`.

    `code` is parameterized, not duplicated, so `build_reflector` reports the
    same missing-value refusal under its own vocabulary (`reflector_env_missing`)
    rather than the editor's.
    """

    value = environ.get(name, "")
    if not value:
        raise EvolveRefused(code, path)
    return value


def _sealed_prompt(
    path: Path | None,
    packaged: str,
    *,
    code: str = "editor_invalid",
    field_path: str = "editor.system_prompt",
) -> FileSeal:
    """Seal the declared prompt file, or the packaged default when none is set.

    The loader already proved a declared file exists, is regular and lies
    outside the seed; a file that vanished between load and bind would arrive
    here as an `OSError` naming a resolved path, which is exactly what a
    refusal must not carry (spec §9).
    """

    try:
        return seal_prompt(path, packaged=packaged)
    except OSError as exc:
        detail = "prompt could not be sealed" if code == "grouper_invalid" else str(exc)
        raise BindingError(code, field_path, detail=detail) from exc


def _agent_program(
    section: AgentCliEditorSection,
    child_env: Mapping[str, str],
    *,
    code: str = "editor_program_missing",
    field_path: str = "editor.agent_bin",
) -> Path:
    """The CLI executable this binding seals: declared, or resolved once at bind.

    `shutil.which` searches the *bound* `PATH` -- the one the child will
    actually have -- and never the parent's: resolving a program the child
    cannot reach would seal an executable that is not the one it runs. A CLI
    that resolves nowhere is `code` at `field_path`, before any write (design §3).
    """

    if section.agent_bin is not None:
        return Path(section.agent_bin)
    found = shutil.which(agent_tool_for(section.model), path=child_env.get("PATH", ""))
    if found is None:
        raise EvolveRefused(code, field_path)
    return Path(found)


def _validated_proxy_url(
    value: str, *, code: str = "editor_invalid", field_path: str = "editor.proxy_env"
) -> str:
    """The proxy URL rules the retired `file_editor._validated_http_url` applied.

    HTTP or HTTPS, a hostname free of whitespace and control characters, no
    query and no fragment, and HTTPS for anything that is not a loopback host.
    Userinfo is admitted, as it was for the retired proxy environment variable
    and as an authenticating proxy needs (design §3: "validated at bind as the
    old proxy environment variable was"); the whole value is a secret of the
    run, so the credential inside it is redacted with the rest of it.

    The refusal is `code at field_path` and carries neither the URL nor its
    host: the value is an environment value, which no refusal, note or journal
    may print (spec §9).
    """

    if any(character in value for character in "\r\n\t"):
        raise BindingError(code, field_path, detail="control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # noqa: B018 - raises `ValueError` for an unparsable port
    except ValueError as exc:
        raise BindingError(code, field_path, detail="unparsable") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise BindingError(code, field_path, detail="scheme")
    if not hostname:
        raise BindingError(code, field_path, detail="no hostname")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in hostname
    ):
        raise BindingError(code, field_path, detail="hostname characters")
    if parsed.query or parsed.fragment:
        raise BindingError(code, field_path, detail="query or fragment")
    if scheme == "http" and not _is_loopback_host(hostname):
        raise BindingError(code, field_path, detail="plaintext to a remote host")
    return value


def _is_loopback_host(hostname: str) -> bool:
    """Whether a proxy host is the loopback interface, which may be plaintext."""

    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
