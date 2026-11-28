"""Private policy preflight over saved declarations and public Core role values."""
from __future__ import annotations

import json
from ..execution_policy import bound_chat_route

from evolution.tree.roles.config import ChatEditorSection, ModelGroupingSection

from .._manifest_schema import grouping_section
from .models import OwnerError
from .secrets import policy_token_bindings, secret_partition


def execution_tokens(binding, target, *, kind):
    """Refuse mismatched roles before admission; return only required execution tokens."""
    policy = target.execution_policy
    if policy is None or policy.gateway is None:
        return {}
    if kind == 'campaign':
        # Presence matters: an explicit false allow_fallbacks is still a pin.
        # Parsed Core defaults cannot establish whether the author declared it.
        document = json.loads(binding.manifest_path.read_text(encoding='utf-8'))
        workflow = binding.manifest.workflow
        for key, section in (('editor', workflow.editor), ('reflector', workflow.reflector.role),
                             ('grouper', grouping_section(workflow))):
            if not isinstance(section, (ChatEditorSection, ModelGroupingSection)):
                continue
            if bound_chat_route(policy, section.base_url, section.model) is None:
                raise OwnerError('policy_route_mismatch', 'before_start')
            if {'provider_only', 'provider_order', 'allow_fallbacks'} & document[key].keys():
                raise OwnerError('manifest_provider_pin', 'before_start')
            if section.api_key_env is None or section.api_key_env not in binding.secret_names:
                raise OwnerError('binding_mismatch', 'before_start')
    assignments = policy_token_bindings(binding, policy)
    required = set(secret_partition(binding, kind=kind).execution)
    if not set(policy.scorer_token_names) <= set(binding.secret_names):
        raise OwnerError('binding_mismatch', 'before_start')
    return {name: target.gateway.tokens[route] for name, route in assignments.items() if name in required}


def check_continuation(first_policy, current_policy):
    """The first start fixes policy presence and the complete gateway block."""
    if (first_policy is None) != (current_policy is None):
        raise OwnerError('policy_continuation_mismatch', 'before_start')
    if first_policy is not None and first_policy.get('gateway') != current_policy.get('gateway'):
        raise OwnerError('policy_gateway_changed', 'before_start')
