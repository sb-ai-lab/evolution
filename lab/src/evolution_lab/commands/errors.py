"""Command failures shared by every transport (ADR-0070): a service envelope or a local one."""
from __future__ import annotations

from typing import Any

#: Local codes with the CLI's ``output.local_error`` envelope (stage ``transport``) and its exact messages.
_TRANSPORT = {
    "request_timeout": "The service request timed out.",
    "request_interrupted": "The service request was interrupted.",
    "acceptance_unknown": "The mutation acceptance is unknown.",
}
#: Codes new to MCP (stage ``client``, spec section 3) and their messages; ``internal_error`` reports an
#: unexpected server exception as a tool result, because protocol errors are reserved for malformed requests.
_CLIENT = {
    "service_unavailable": "The Lab service is not reachable.",
    "invalid_service_response": "The service response is invalid.",
    "usage_error": "The request arguments are invalid.",
    "internal_error": "The MCP server failed to run the command.",
}


class CommandFailure(Exception):
    """A command failed; ``envelope`` is the JSON error object, ``hint`` an optional next step."""

    def __init__(self, envelope: dict[str, Any], hint: str | None = None) -> None:
        super().__init__(envelope.get("error", {}).get("message", "command failed"))
        self.envelope = envelope
        self.hint = hint


class ServiceFailure(CommandFailure):
    """The service answered with its error envelope; passed through whole."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        super().__init__(envelope)


class LocalFailure(CommandFailure):
    """A failure produced locally; the stage follows the code unless given."""

    def __init__(self, code: str, message: str | None = None, hint: str | None = None,
                 stage: str | None = None) -> None:
        if message is None:
            message = _TRANSPORT.get(code) or _CLIENT[code]
        if stage is None:
            stage = "transport" if code in _TRANSPORT else "client"
        super().__init__({"error": {"code": code, "stage": stage, "message": message}}, hint)
        self.code = code
