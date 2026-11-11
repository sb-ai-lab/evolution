"""Private runtime assembly for adaptive campaign grouping.

The manifest parser owns syntax and :mod:`reflect_grouping` owns result
admission.  This module binds those two layers to a campaign session without
turning the unreleased family-4 inputs into a public execution surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.campaign_identity import AdaptationIdentityV1
from evolution.tree.execution.check_modes import scan_grouping_value, scan_grouping_wire
from evolution.tree.execution.command_binding import CommandBinding
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.json_pointer import MISSING, pointer_value
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.editor.subprocess_editor import _open_private_log, _terminate_shielded
from evolution.tree.roles.grouping.reflect_grouping import (
    DESCRIPTION_CHARS,
    FAMILY15_DESCRIPTION_CHARS,
    AdmittedGrouping,
    FieldFeature,
    GroupingRegistry,
    GroupingResult,
    admit_grouping_result,
    builtin_grouping,
)
from evolution.tree.roles.reflection_reader import ReaderIntegrityError, ReflectionReadSession
from evolution.tree.roles.reflection_role_helpers import install_helpers
from evolution.tree.roles.role_failures import GrouperRoleFailure

if TYPE_CHECKING:
    # Circular at module scope: the manifest loader imports this module.
    from evolution.tree.roles.config import AgentCliGrouperSection, ModelGroupingSection
    from evolution.tree.roles.editor.chat_editor import ChatCredentials
    from evolution.tree.roles.grouping.model_grouping_runtime import ModelGroupingOutcome
    from evolution.tree.roles.grouping.model_grouping_v2 import Adopted, CheckpointSink
    from evolution.tree.roles.role_call import AttemptRecorder
    from evolution.tree.roles.seals import FileSeal

from evolution.tree.roles.role_failures import GroupingExecutionError

__all__ = [
    "AdaptiveCampaignRuntimeConfig",
    "GroupingRuntime",
    "GroupingExecutionError",
    "SelectionRuntimeConfig",
]


def family_membership_for(
    families: Mapping[str, Any],
    reflection_metadata: Mapping[str, Mapping[str, Any]],
    corpus: Sequence[Any],
) -> Mapping[str, str | None]:
    """Each corpus case's family under `families`, `None` for a case without one.

    `families: none` is `{}`. A present value that is not a non-blank string
    raises `ValueError("invalid_family_value")`. Shared by the runtime config
    and by `evolve`, which records the membership in adaptation identity `/2`
    (ADR-0021) before the config exists.
    """

    if families.get("kind") == "none":
        return {}
    result = {}
    for case in corpus:
        value = pointer_value(reflection_metadata.get(case.case_id, {}), families["path"])
        if value is MISSING:
            value = None
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("invalid_family_value")
        result[case.case_id] = value
    return result


def _frozen_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy a JSON-compatible mapping into immutable canonical containers."""

    def mutable(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): mutable(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [mutable(child) for child in item]
        if isinstance(item, list):
            return [mutable(child) for child in item]
        return item

    try:
        copied = json.loads(
            json.dumps(mutable(value), ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid_runtime_metadata") from exc
    if not isinstance(copied, dict):  # defensive: callers receive a mapping annotation
        raise ValueError("invalid_runtime_metadata")

    def freeze(item: Any) -> Any:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(value) for key, value in item.items()})
        if isinstance(item, list):
            return tuple(freeze(value) for value in item)
        return item

    return freeze(copied)


@dataclass(frozen=True)
class SelectionRuntimeConfig:
    """The closed, bounded portion controls used by adaptive selection."""

    focus_cases: int = 16
    representatives_per_case: int = 2
    context_page_size: int = 32

    def __post_init__(self) -> None:
        if not 1 <= self.focus_cases <= 64:
            raise ValueError("invalid_focus_cases")
        if not 1 <= self.representatives_per_case <= 8:
            raise ValueError("invalid_representatives_per_case")
        if not 1 <= self.context_page_size <= 64:
            raise ValueError("invalid_context_page_size")


@dataclass(frozen=True)
class AdaptiveCampaignRuntimeConfig:
    """Private, already-parsed inputs required by the adaptive executor.

    ``identity`` is deliberately separate from the campaign measurement
    identity.  Its digest rejects a same-run diagnostic mismatch while keeping
    reflection metadata out of measurement comparability.  A command binding
    is process-local and never serialised into the journal; its identity proof
    is part of ``identity``.

    Exactly one grouping source is set: ``grouping_features`` (``fields``),
    ``custom_command`` (``command``) or ``model_grouping`` (``model``, family 9,
    ADR-0022) or ``agent_grouping`` (``agent_cli``, family 14). The model section
    is the parsed manifest one; its identity payload, fallback included, is
    already part of ``identity``.
    """

    identity: AdaptationIdentityV1
    grouping_features: tuple[FieldFeature, ...] = ()
    custom_command: CommandBinding | None = None
    custom_timeout_seconds: float = 30.0
    custom_config: Mapping[str, Any] = field(default_factory=dict)
    selection: SelectionRuntimeConfig = field(default_factory=SelectionRuntimeConfig)
    families: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({"kind": "none"}))
    reflection_metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    model_grouping: ModelGroupingSection | None = None
    #: `/15`'s `grouper.kind: agent_cli`: the parsed
    #: section; its identity, prompt and program seal included, is already part
    #: of `identity`. The following bindings are process-only; their values
    #: never enter the identity or journal.
    agent_grouping: AgentCliGrouperSection | None = None
    agent_program: FileSeal | None = field(default=None, kw_only=True, repr=False, compare=False)
    agent_prompt: FileSeal | None = field(default=None, kw_only=True, repr=False, compare=False)
    agent_child_environment: Mapping[str, str] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )
    #: Instance digests of the run-directory hygiene findings the operator has
    #: accepted (PDR-0010). The command grouping is the Reflector's, and the
    #: only writer reached through this config that admits an output root, so
    #: the set rides along here rather than through a second channel; it is
    #: deliberately outside `identity`, because an operator's decision about a
    #: directory is not part of what makes two groupings the same grouping.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    #: The grouping model's resolved key and proxy (`bindings.
    #: grouping_model_credentials`), required to run a `model` policy. Like
    #: `admitted_hygiene` it is outside `identity`: the identity binds the
    #: environment variable *names*, never their values.
    model_credentials: ChatCredentials | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )
    #: The `client_factory` handed to `GroupingRuntime.model`; `None` builds the
    #: production `openai` client (`model_grouping_runtime.new_grouping_client`).
    model_client_factory: Callable[[ModelGroupingSection, ChatCredentials], Any] | None = field(
        default=None, kw_only=True, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AdaptationIdentityV1):
            raise ValueError("invalid_adaptation_identity")
        if not math.isfinite(self.custom_timeout_seconds) or self.custom_timeout_seconds <= 0:
            raise ValueError("invalid_grouping_timeout")
        features = tuple(self.grouping_features)
        if self.model_grouping is None and self.model_credentials is not None:
            raise ValueError("model_credentials_without_model_grouping")
        if self.agent_grouping is not None:
            if self.model_grouping is not None or self.custom_command is not None:
                raise ValueError("agent_grouping_has_other_source")
            if features:
                raise ValueError("agent_grouping_has_features")
        elif self.model_grouping is not None:
            if self.custom_command is not None:
                raise ValueError("model_grouping_has_command")
            if features:
                raise ValueError("model_grouping_has_features")
        elif self.custom_command is None:
            if not 1 <= len(features) <= 16 or not all(
                isinstance(feature, FieldFeature) for feature in features
            ):
                raise ValueError("invalid_grouping_features")
            if len({feature.name for feature in features}) != len(features):
                raise ValueError("duplicate_grouping_feature")
        elif features:
            raise ValueError("command_grouping_has_features")
        if self.agent_child_environment is not None:
            object.__setattr__(
                self,
                "agent_child_environment",
                MappingProxyType(dict(self.agent_child_environment)),
            )
        object.__setattr__(self, "grouping_features", features)
        object.__setattr__(self, "custom_config", _frozen_json(self.custom_config))
        object.__setattr__(self, "families", _frozen_json(self.families))
        metadata = {
            case_id: _frozen_json(value) for case_id, value in self.reflection_metadata.items()
        }
        if not all(isinstance(case_id, str) and case_id for case_id in metadata):
            raise ValueError("invalid_reflection_metadata")
        object.__setattr__(self, "reflection_metadata", MappingProxyType(metadata))

    def family_membership(self, corpus: Sequence[Any]) -> Mapping[str, str | None]:
        return family_membership_for(self.families, self.reflection_metadata, corpus)

    @property
    def policy_digest(self) -> str:
        """The compact policy key used for once-per-baseline grouping reuse."""

        # Selection, family paging, feedback and documentation govern later
        # stages.  They must not rename an otherwise identical neighborhood.
        return hashlib.sha256(
            canonical_json(
                {
                    "version": "reflect_grouping/1",
                    "grouping": self.identity.to_payload()["grouping"],
                }
            )
        ).hexdigest()

    @property
    def command_grouping(self) -> bool:
        return self.custom_command is not None


def _role_exception_class(exc: Exception, routed: tuple[type[Exception], ...]) -> str | None:
    """The class name a family-14 `role_failed / exception` logs, else `None`.

    A `GrouperRoleFailure` is routed by its own reason: only `exception` is
    named, by its cause's class. Any other exception the executor's wrap lets
    through (`routed`) keeps its route and is not named here; everything else
    is the wrap's `exception`.
    """

    if isinstance(exc, GrouperRoleFailure):
        if exc.reason == "exception" and exc.__cause__ is not None:
            return type(exc.__cause__).__name__
        return None
    if isinstance(exc, routed):
        return None
    return type(exc).__name__


@dataclass(frozen=True)
class GroupingRuntime:
    """Execute the in-process portion of an adaptive grouping policy."""

    config: AdaptiveCampaignRuntimeConfig

    def builtin(
        self,
        registry: GroupingRegistry,
        *,
        forbidden_string_predicates: Sequence[tuple[str, Any]] = (),
        family15: bool = False,
    ) -> AdmittedGrouping:
        if self.config.command_grouping:
            raise ValueError("command_grouping_requires_process")
        return builtin_grouping(
            registry,
            self.config.grouping_features,
            policy_identity=self.config.policy_digest,
            forbidden_string_predicates=forbidden_string_predicates,
            describe=family15,
            # A family-15 `fields` description is bounded like a model
            # description (ADR-0057 §3.4); family 14 keeps 512.
            description_chars=FAMILY15_DESCRIPTION_CHARS if family15 else DESCRIPTION_CHARS,
        )

    async def agent(
        self,
        registry: GroupingRegistry,
        *,
        reader: ReflectionReadSession,
        control_root: Path,
        operation: str,
        log_root: Path,
        secrets: Sequence[str],
        parent_digest: str | None,
        verify_admission: Callable[[], None],
        cancelled: Callable[[], bool],
        forbidden_secret: Callable[[str], bool],
        oracle_needle: Callable[[str], bool],
        family15: bool = False,
    ) -> ModelGroupingOutcome:
        """Run the process-bound agent against the existing Grouper identity."""
        from evolution.tree.roles.grouping.agent_grouper import AgentCliGrouper

        section = self.config.agent_grouping
        program = self.config.agent_program
        prompt = self.config.agent_prompt
        child_env = self.config.agent_child_environment
        if section is None or program is None or prompt is None or child_env is None:
            raise ValueError("agent_grouping_bindings_required")
        identity = self.config.identity.to_payload()["grouping"]
        if (
            program.sha256 != identity["program_sha256"]
            or prompt.sha256 != identity["prompt_sha256"]
        ):
            raise GroupingExecutionError("command_changed")
        return await AgentCliGrouper(
            family15=family15,
            section=section,
            program=program,
            prompt=prompt,
            child_env=child_env,
            log_root=log_root,
            secrets=tuple(secrets),
            admitted_hygiene=self.config.admitted_hygiene,
        ).run(
            registry,
            reader=reader,
            control_root=control_root,
            operation=operation,
            policy_identity=self.config.policy_digest,
            parent_digest=parent_digest,
            verify_admission=verify_admission,
            cancelled=cancelled,
            forbidden_secret=forbidden_secret,
            oracle_needle=oracle_needle,
        )

    async def model(
        self,
        registry: GroupingRegistry,
        *,
        operation: str,
        credentials: ChatCredentials,
        log_root: Path,
        secrets: Sequence[str],
        cancelled: Callable[[], bool],
        forbidden_secret: Callable[[str], bool],
        oracle_needle: Callable[[str], bool],
        metric_primary: str,
        client_factory: Callable[[ModelGroupingSection, ChatCredentials], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        family14: bool = False,
        routed_faults: tuple[type[Exception], ...] = (),
        attempts: AttemptRecorder | None = None,
        reader: ReflectionReadSession | None = None,
        family15: bool = False,
        checkpoints: CheckpointSink | None = None,
        adopted: Adopted | None = None,
        provenance: Mapping[str, tuple[str, ...] | None] | None = None,
        record_budget: int | None = None,
        redact: Callable[[str], str] | None = None,
        normalize: Callable[[Any], Any] | None = None,
    ) -> ModelGroupingOutcome:
        """Run the grouping-model role once (family 9, ADR-0022).

        The prompt -- the declared `system_prompt` or the packaged default --
        must still have the digest bound into the adaptation identity, sealed
        the way `evolve` sealed it (`editor_seal.seal_prompt`); a different
        prompt is ``command_changed``, an integrity failure and never a
        fallback. The private log is ``<log_root>/grouping-<operation>.jsonl``,
        redacted of ``secrets``.

        ``family14`` runs the family-14 `chat` Grouper (ADR-0034): a provider
        exhaustion or the deadline raises `GrouperRoleFailure` instead of
        falling back (`run_model_grouping`). The default keeps families 9-13.

        In family 14 an exception that leaves the role call on the
        `role_failed / exception` route -- one the executor's wrap converts,
        being none of ``routed_faults`` (its `_GROUPING_ROUTED_FAULTS`), or a
        `GrouperRoleFailure("exception")` the runtime raised from a cause --
        is named in the private log as ``{"phase": "role_failed",
        "exception": <class name>}`` before the log closes: the class only,
        never the message. An exception raised before
        the log opens has no log to carry it. The record is diagnostic only
        and is written as the exception leaves: if a stop intervenes, the
        executor journals the grouping ``interrupted`` instead of
        ``role_failed``, and the log line stays.

        Family-14 callers must pass ``routed_faults`` (the executor's
        `_GROUPING_ROUTED_FAULTS`): with the empty default, a routed fault
        would be logged as a ``role_failed`` it is not.

        ``attempts`` is the caller's recorder of the operation's provider
        attempts, for the operator's line.

        ``reader`` is the operation's open grouping reader. The `model_grouping/2`
        path serves its read tools from it; the `/1` path ignores it.

        A bound identity recording `model_grouping/2` runs
        `run_model_grouping_v2` (b16 spec §3), which only a family-15 run
        (``family15``) may do; any other protocol runs `/15`'s map/reduce.
        In `model_grouping/2` ``timeout_seconds`` bounds each session and
        batch, not the operation (§6.2), and the call takes ``reader``,
        ``record_budget`` (the checkpoint bound, ``MAX_RECORD_BYTES -
        CHECKPOINT_ENVELOPE_BYTES``), and optionally ``checkpoints``,
        ``adopted``, ``provenance`` (stored `used` unions by evidence ID),
        ``redact`` (the run-secret redaction of component IDs) and
        ``normalize`` (the journal's form of a checkpointed decision).

        A family-15 run without a declared `system_prompt` seals the packaged
        `_v3` prompt (b17, ADR-0057); `/15` and every declared prompt keep
        their bytes.
        """

        # Lazy: the manifest loader imports this module, and both the runtime
        # and the packaged prompts sit above it.
        from evolution.prompts import TREE_GROUPING_MODEL_SYSTEM, TREE_GROUPING_MODEL_V3_SYSTEM
        from evolution.tree.roles.config import effective_grouping_v2
        from evolution.tree.roles.grouping.model_grouping_runtime import run_model_grouping
        from evolution.tree.roles.grouping.model_grouping_v2 import run_model_grouping_v2
        from evolution.tree.roles.seals import seal_prompt
        from evolution.tree.roles.workspace import open_attempt_log

        section = self.config.model_grouping
        if section is None:
            raise ValueError("grouping_is_not_model")
        grouping = self.config.identity.to_payload()["grouping"]
        expected = grouping.get("prompt_sha256")
        # A `/16` Grouper's identity records `model_grouping/2` and the packaged
        # prompt (b16 spec §11; b17 seals `_v3`, ADR-0057); the identity selects
        # the protocol, and only a family-15 run may record it.
        v2 = grouping.get("protocol") == "model_grouping/2"
        if v2 and not family15:
            raise ValueError("model_grouping_v2_outside_family15")
        if v2 and (reader is None or record_budget is None):
            raise ValueError("model_grouping_v2_bindings_required")
        packaged = TREE_GROUPING_MODEL_V3_SYSTEM if v2 else TREE_GROUPING_MODEL_SYSTEM
        try:
            seal = seal_prompt(section.system_prompt, packaged=packaged)
            if seal.sha256 != expected:
                raise GroupingExecutionError("command_changed")
            if section.system_prompt is None:
                prompt = packaged
            else:
                # Re-hash the bytes actually sent: the file may change between
                # the seal and this read.
                raw = section.system_prompt.read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise GroupingExecutionError("command_changed")
                prompt = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GroupingExecutionError("command_changed") from exc
        deadline = clock() + section.timeout_seconds
        with open_attempt_log(
            Path(log_root),
            f"grouping-{operation}",
            secrets=secrets,
            admitted=self.config.admitted_hygiene,
        ) as log:
            try:
                if v2:
                    assert reader is not None and record_budget is not None
                    return await run_model_grouping_v2(
                        registry,
                        section=section,
                        limits=effective_grouping_v2(section),
                        prompt=prompt,
                        metric_primary=metric_primary,
                        policy_identity=self.config.policy_digest,
                        credentials=credentials,
                        log=log,
                        session_seconds=section.timeout_seconds,
                        reader=reader,
                        provenance=provenance,
                        checkpoints=checkpoints,
                        adopted=adopted,
                        cancelled=cancelled,
                        forbidden_secret=forbidden_secret,
                        oracle_needle=oracle_needle,
                        record_budget=record_budget,
                        redact=redact,
                        normalize=normalize,
                        client_factory=client_factory,
                        clock=clock,
                        attempts=attempts,
                    )
                return await run_model_grouping(
                    registry,
                    section=section,
                    prompt=prompt,
                    metric_primary=metric_primary,
                    policy_identity=self.config.policy_digest,
                    credentials=credentials,
                    log=log,
                    deadline=deadline,
                    cancelled=cancelled,
                    forbidden_secret=forbidden_secret,
                    oracle_needle=oracle_needle,
                    client_factory=client_factory,
                    clock=clock,
                    family14=family14,
                    attempts=attempts,
                )
            except Exception as exc:
                unexpected = _role_exception_class(exc, routed_faults) if family14 else None
                if unexpected is not None:
                    # A failing write (an `OSError`, e.g. disk quota) is a
                    # routed attempt-log fault and replaces the role failure,
                    # as any other attempt-log write failure does (accepted).
                    log.record({"phase": "role_failed", "exception": unexpected})
                raise

    async def command(
        self,
        registry: GroupingRegistry,
        *,
        reader: ReflectionReadSession,
        control_root: os.PathLike[str] | str,
        log_root: Path | None = None,
        forbidden: Any = lambda _text: False,
        parent_digest: str | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        scorer_diagnostics: bool = False,
        family15: bool = False,
    ) -> AdmittedGrouping:
        """Run one sealed custom policy over operation-scoped helper launchers.

        ``scorer_diagnostics`` names the request schema the helper program
        sees: `reflect_grouping_request/2` when the bound baseline's inputs
        admit the `diagnostics` source (family 8, ADR-0021), `/1` otherwise --
        families 5-7 keep the request byte for byte.

        ``family15`` admits the result's descriptions up to
        `FAMILY15_DESCRIPTION_CHARS` instead of `DESCRIPTION_CHARS`
        (ADR-0057): a family-15 run has one description admission bound.
        """

        binding = self.config.custom_command
        if binding is None:
            raise ValueError("builtin_grouping_has_no_command")
        try:
            binding.verify()
        except Exception as exc:
            raise GroupingExecutionError("command_changed") from exc
        control = Path(control_root)
        process = None
        stderr = None
        tasks: list[asyncio.Task[Any]] = []
        try:
            try:
                helpers = install_helpers(control, reader)
            except OSError as exc:
                raise GroupingExecutionError("helper_unavailable") from exc
            descriptor = {
                "version": (
                    "reflect_grouping_request/2"
                    if scorer_diagnostics
                    else "reflect_grouping_request/1"
                ),
                "baseline_binding": registry.baseline_binding,
                "parent_digest": parent_digest,
                "policy_digest": self.config.policy_digest,
                "registry_digest": registry.digest,
                "record_count": len(registry.records),
                "config": json.loads(
                    json.dumps(
                        self.config.custom_config,
                        default=lambda value: (
                            dict(value) if isinstance(value, Mapping) else list(value)
                        ),
                    )
                ),
                "limits": {
                    "descriptor_bytes": 65536,
                    "result_bytes": 524288,
                    "page_characters": 16000,
                },
                "helpers": {name: list(argv) for name, argv in helpers.descriptors.items()},
            }
            encoded = canonical_json(descriptor)
            if len(encoded) > 64 * 1024:
                raise GroupingExecutionError("descriptor_too_large")
            request_file = control / "request.json"
            request_file.write_bytes(encoded)
            request_file.chmod(0o400)
            from evolution.tree.execution.output_admission import admit_output_root

            destination = (
                Path(log_root) if log_root is not None else control.parent / "reflector-logs"
            )
            admit_output_root(destination, admitted=self.config.admitted_hygiene)
            log_directory = ensure_private_directory(destination)
            stderr = _open_private_log(log_directory / f"{reader.operation_id}.stderr")
            process = await asyncio.create_subprocess_exec(
                *binding.spawn_argv(),
                str(request_file.resolve()),
                cwd=binding.cwd,
                env=dict(binding.child_env),
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                start_new_session=True,
            )
            assert process.stdout is not None

            async def bounded_read(stream: asyncio.StreamReader, limit: int) -> bytes:
                output = bytearray()
                while chunk := await stream.read(65536):
                    output.extend(chunk[: max(0, limit + 1 - len(output))])
                    if len(output) > limit:
                        raise GroupingExecutionError("result_too_large")
                return bytes(output)

            tasks = [
                asyncio.create_task(bounded_read(process.stdout, 512 * 1024)),
            ]

            async def exchange() -> bytes:
                stdout = await tasks[0]
                await process.wait()
                return stdout

            async def wait_for_cancellation() -> None:
                while not cancelled():
                    await asyncio.sleep(0.025)
                raise GroupingExecutionError("cancelled")

            exchange_task = asyncio.create_task(exchange())
            cancellation_task = asyncio.create_task(wait_for_cancellation())
            tasks.extend((exchange_task, cancellation_task))
            try:
                async with asyncio.timeout(self.config.custom_timeout_seconds):
                    done, _pending = await asyncio.wait(
                        (exchange_task, cancellation_task), return_when=asyncio.FIRST_COMPLETED
                    )
                    if cancellation_task in done:
                        cancellation_task.result()
                    if cancelled():
                        raise GroupingExecutionError("cancelled")
                    stdout = exchange_task.result()
            except TimeoutError as exc:
                if cancelled():
                    raise GroupingExecutionError("cancelled") from exc
                raise GroupingExecutionError("timeout") from exc
            helpers.verify()
            try:
                binding.verify()
            except Exception as exc:
                raise GroupingExecutionError("command_changed") from exc
            try:
                if request_file.is_symlink() or request_file.read_bytes() != encoded:
                    raise GroupingExecutionError("command_changed")
            except OSError as exc:
                raise GroupingExecutionError("command_changed") from exc
            if reader.integrity_fault is not None:
                raise ReaderIntegrityError(reader.integrity_fault)
            # Scan the complete wire result, including unknown keys and output
            # later rejected by the parser, before any result can be durable.
            try:
                output = stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GroupingExecutionError("invalid_result") from exc
            field_ids = tuple(row.evidence_id for row in registry.records)
            if scan_grouping_wire(forbidden, output, field_ids, decoded_enforcement=False):
                raise GroupingExecutionError("secret_detected")
            if process.returncode != 0:
                raise GroupingExecutionError("command_failed")
            try:
                raw = json.loads(output)
                if (
                    not isinstance(raw, dict)
                    or not {"version", "registry_digest", "assignments"} <= raw.keys()
                    or raw.keys()
                    - {"version", "registry_digest", "assignments", "descriptions", "variants"}
                ):
                    raise ValueError("invalid_result")

                if scan_grouping_value(
                    lambda value: isinstance(value, str) and forbidden(value), raw, field_ids
                ):
                    raise GroupingExecutionError("secret_detected")
                result = GroupingResult(
                    raw["registry_digest"],
                    tuple(raw["assignments"]),
                    raw.get("descriptions", {}),
                    raw.get("variants", {}),
                    raw["version"],
                )
            except GroupingExecutionError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise GroupingExecutionError("invalid_result") from exc
            return admit_grouping_result(
                registry,
                result,
                policy_identity=self.config.policy_digest,
                description_chars=FAMILY15_DESCRIPTION_CHARS if family15 else DESCRIPTION_CHARS,
            )
        except (OSError, WorkspaceError) as exc:
            raise GroupingExecutionError("command_failed") from exc
        except asyncio.CancelledError as cancellation:
            if process is not None:
                await _terminate_shielded(process, cancellation)
            raise
        finally:
            if process is not None:
                # Reap descendants too, including a parent that exits early
                # while a child retains a pipe or the supplied capability.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if stderr is not None:
                stderr.close()
            shutil.rmtree(control, ignore_errors=True)
