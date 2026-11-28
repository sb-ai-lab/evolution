"""Transport-neutral Lab command layer shared by the CLI and the MCP server (ADR-0070)."""
from evolution_lab.commands._core import REGISTRY, Command, Effect, Request

# Command modules register themselves on import; they are added here as they land.
from evolution_lab.commands import mutations, offline, operations, project_mutations, projects, resources  # noqa: F401

__all__ = ["REGISTRY", "Command", "Effect", "Request"]
