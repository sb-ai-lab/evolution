"""Pure S2 secret-name partitioning and in-memory environment binding."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from evolution.tree.bindings import (
    bound_child_environment,
    editor_environment_names,
    reflector_environment_names,
)
from evolution.tree.roles.config import AgentCliEditorSection, CommandGroupingSection, ModelGroupingSection

from .._manifest_schema import grouping_section
from .binding import BundleBinding
from .models import OwnerError
from .protocol import _RESERVED_ENV


def reserved_service_names() -> frozenset[str]:
    """Names owned by the helper protocol, never by callers or saved env."""
    return frozenset(_RESERVED_ENV)


@dataclass(frozen=True, slots=True)
class SecretPartition:
    declared: tuple[str, ...]
    target: tuple[str, ...]
    execution: tuple[str, ...]
    unused: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundSecrets:
    """Short-lived private values and the exact subprocess environment copy."""

    kind: str
    bundle_digest: str
    names: tuple[str, ...]
    values: Mapping[str, str]
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.kind not in {"target", "execution", "conformance"}:
            raise OwnerError("invalid_argument", "before_start")
        object.__setattr__(self, "names", tuple(self.names))
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


def _grouping_names(grouping) -> tuple[str, ...]:
    """Environment names the reflector's grouping declares: a command's ``allowed_env``,
    a model's key and optional proxy (core ``grouping_model_credentials`` reads the same two)."""
    if isinstance(grouping, CommandGroupingSection):
        return tuple(grouping.command.allowed_env)
    if isinstance(grouping, ModelGroupingSection):
        return tuple(name for name in (grouping.api_key_env, grouping.proxy_env) if name is not None)
    return ()


@dataclass(frozen=True, slots=True)
class DeclaredNames:
    execution: frozenset[str]
    target_secrets: frozenset[str]
    target_static: frozenset[str]


def declared_environment_names(*, workflow=None, evaluation_input=None, launch=None,
                               kind: str = 'campaign') -> DeclaredNames:
    """Names explicitly declared by admitted inputs, excluding implicit forwarding."""
    execution = set()
    if kind == 'evaluation':
        execution.update(evaluation_input['scorer']['allowed_env'] if evaluation_input is not None
                         else workflow.scorer.allowed_env)
    elif kind == 'campaign':
        if workflow is None:
            raise ValueError('campaign workflow required')
        execution.update(workflow.scorer.allowed_env)
        execution.update(editor_environment_names(workflow.editor))
        execution.update(reflector_environment_names(workflow.reflector))
        execution.update(_grouping_names(grouping_section(workflow)))
    elif kind != 'probe':
        raise ValueError('invalid declaration kind')
    return DeclaredNames(
        frozenset(execution),
        frozenset(launch['secret_names']) if launch is not None else frozenset(),
        frozenset(launch['env']) if launch is not None else frozenset(),
    )


def _agent_forwarded_names(section, available: set[str]) -> set[str]:
    if not isinstance(section, AgentCliEditorSection):
        return set()
    placeholders = {name: "x" for name in available}
    return set(bound_child_environment(section, placeholders))


def secret_partition(binding: BundleBinding, *, kind: str | None = None) -> SecretPartition:
    """Derive S/T/E/U solely from saved inputs and public core bindings."""
    if not isinstance(binding, BundleBinding):
        raise OwnerError("invalid_argument", "before_start")
    try:
        kind = binding.source_kind if kind is None else kind
        if kind not in {"campaign", "evaluation", "probe"}:
            raise ValueError("invalid execution kind")
        if (kind == "probe") != (binding.source_kind == "probe"):
            raise ValueError("a probe Bundle has only the probe partition")
        declared = set(binding.secret_names)
        workflow = binding.manifest.workflow if binding.source_kind == 'campaign' else None
        names = declared_environment_names(
            workflow=workflow, launch=binding.launch, kind=kind,
            evaluation_input=binding.evaluation_input if binding.source_kind == 'evaluation' else None,
        )
        target = set(names.target_secrets)
        execution = set(names.execution)
        if kind == 'campaign':
            available = set(binding.base_environment) | declared
            execution.update(_agent_forwarded_names(workflow.editor, available))
            execution.update(_agent_forwarded_names(workflow.reflector.role, available))
        execution &= declared
        unused = declared - (target | execution)
        if not target <= declared or reserved_service_names() & (declared | target | execution):
            raise ValueError("invalid saved secret partition")
        return SecretPartition(
            tuple(sorted(declared)),
            tuple(sorted(target)),
            tuple(sorted(execution)),
            tuple(sorted(unused)),
        )
    except OwnerError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise OwnerError("artifacts_invalid", "before_start") from None


def _values(values: Mapping[str, str], expected: tuple[str, ...], stage: str) -> dict[str, str]:
    if (
        not isinstance(values, Mapping)
        or set(values) != set(expected)
        or any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in values.items())
        or reserved_service_names() & set(values)
    ):
        raise OwnerError("invalid_argument", stage)
    return {name: values[name] for name in expected}


def bind_target_secrets(
    binding: BundleBinding,
    secrets: Mapping[str, str],
    *,
    lab_filled: frozenset[str] = frozenset(),
) -> BoundSecrets:
    """Validate exactly T and build launch.env + T, with no ambient env."""
    partition = secret_partition(binding)
    if isinstance(secrets, Mapping) and set(secrets) & lab_filled:
        raise OwnerError("secret_conflict", "target_start")
    expected = tuple(name for name in partition.target if name not in lab_filled)
    values = _values(secrets, expected, "target_start")
    environment = dict(binding.launch["env"])
    if set(environment) & set(values):
        raise OwnerError("artifacts_invalid", "target_start")
    environment.update(values)
    return BoundSecrets("target", binding.bundle_digest, partition.target, values, environment)


def bind_execution_secrets(
    binding: BundleBinding,
    secrets: Mapping[str, str],
    *,
    target_secrets: BoundSecrets,
    kind: str | None = None,
    lab_filled: Mapping[str, str] | None = None,
) -> BoundSecrets:
    """Validate exactly E, compare E∩T, and build Bundle.env + E."""
    partition = secret_partition(binding, kind=kind)
    supplied = {} if lab_filled is None else dict(lab_filled)
    if not set(supplied) <= set(partition.execution):
        raise OwnerError("binding_mismatch", "before_start")
    if isinstance(secrets, Mapping) and set(secrets) & set(supplied):
        raise OwnerError("secret_conflict", "before_start")
    values = _values(secrets, tuple(name for name in partition.execution if name not in supplied), "before_start")
    values.update(supplied)
    if (
        not isinstance(target_secrets, BoundSecrets)
        or target_secrets.kind != "target"
        or target_secrets.bundle_digest != binding.bundle_digest
        or target_secrets.names != partition.target
    ):
        raise OwnerError("binding_mismatch", "before_start")
    for name in set(partition.execution) & set(partition.target):
        if values[name] != target_secrets.values[name]:
            raise OwnerError("binding_mismatch", "before_start")
    environment = dict(binding.base_environment)
    if set(environment) & set(values):
        raise OwnerError("artifacts_invalid", "before_start")
    environment.update(values)
    return BoundSecrets(
        "execution", binding.bundle_digest, partition.execution, values, environment
    )


def bind_conformance_secrets(binding: BundleBinding) -> BoundSecrets:
    """Build the deliberately empty role environment for conformance."""
    if not isinstance(binding, BundleBinding):
        raise OwnerError("invalid_argument", "conformance")
    return BoundSecrets("conformance", binding.bundle_digest, (), {}, {})


def policy_token_bindings(binding, policy) -> dict[str, str]:
    """Resolve consumer token names once, shared with execution-role binding.

    This checks name ownership only. Core model/URL/provider preflight belongs to
    start/resume; it is deliberately not performed by target launch.
    """
    from urllib.parse import urlsplit
    from evolution_lab.execution_policy import lab_filled_names

    assignments = {}
    consumers = {}

    def add(name, route, consumer):
        if name in assignments and assignments[name] != route:
            raise OwnerError('token_name_conflict', 'target_start',
                             token_conflict=(name, tuple(consumers[name] | {consumer})))
        assignments[name] = route
        consumers.setdefault(name, set()).add(consumer)

    if policy.target_token_name is not None:
        if policy.target_token_name not in binding.launch['secret_names']:
            raise OwnerError('binding_mismatch', 'target_start')
        add(policy.target_token_name, 'target', 'target')
    workflow = binding.manifest.workflow
    scorer_names = (binding.evaluation_input['scorer']['allowed_env'] if binding.source_kind == 'evaluation'
                    else workflow.scorer.allowed_env if workflow is not None else ())
    for name, route in policy.scorer_token_names.items():
        if name not in scorer_names:
            raise OwnerError('binding_mismatch', 'target_start')
        add(name, route, 'scorer')
    if workflow is not None:
        for consumer, section in (('editor', workflow.editor), ('reflector', workflow.reflector.role),
                                  ('grouping', grouping_section(workflow))):
            name = getattr(section, 'api_key_env', None)
            if name is None:
                continue
            endpoint = urlsplit(getattr(section, 'base_url', '') or '')
            path = endpoint.path.rstrip('/').split('/')
            group = path[-2] if len(path) > 1 and path[-1] == 'v1' else consumer
            route = group if group in policy.routes else None
            if group in policy.role_groups:
                route = next((member for member in policy.role_groups[group]
                              if policy.routes[member].client_model == getattr(section, 'model', None)), None)
            if route is not None:
                add(name, route, consumer)
    # An arbitrary scorer command has no inspectable route. Its policy mapping
    # must explicitly authorize receiving another consumer's generated token.
    for name in set(scorer_names) & assignments.keys():
        if name not in policy.scorer_token_names:
            raise OwnerError('token_name_conflict', 'target_start',
                             token_conflict=(name, tuple(consumers[name] | {'scorer'})))
    # Ordinary target names cannot silently become a different consumer's token.
    for name in set(binding.launch['secret_names']) & assignments.keys():
        if name != policy.target_token_name:
            raise OwnerError('token_name_conflict', 'target_start',
                             token_conflict=(name, tuple(consumers[name] | {'target'})))
    upstream = {route.api_key_secret for route in policy.routes.values()} - {None}
    if upstream & (assignments.keys() | set(lab_filled_names(policy)) | set(binding.launch['secret_names'])):
        raise OwnerError('secret_conflict', 'target_start')
    if (set(assignments) & (reserved_service_names() | set(binding.launch['env']) | set(binding.base_environment))):
        raise OwnerError('secret_conflict', 'target_start')
    return assignments
