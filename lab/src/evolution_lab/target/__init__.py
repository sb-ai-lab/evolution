"""Neutral turn protocol: make any asynchronous agent a core target (ADR-0013)."""
from evolution_lab.target._backend import backend
from evolution_lab.target._contract import (
    AGENT_OUTCOME_CATEGORY,
    AGENT_OUTCOMES,
    KEEP,
    OUTCOMES,
    AgentFactory,
    AgentTurn,
    AgentView,
    JsonValue,
    LaunchContext,
    TargetConfigurationError,
    TurnAgent,
    TurnProvenance,
    TurnReason,
    component,
)

__all__ = [
    "AGENT_OUTCOME_CATEGORY", "AGENT_OUTCOMES", "KEEP", "OUTCOMES", "AgentFactory", "AgentTurn", "AgentView",
    "JsonValue",
    "LaunchContext", "TargetConfigurationError", "TurnAgent", "TurnProvenance", "TurnReason", "backend", "component",
]
