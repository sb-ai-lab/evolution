"""Obtain approval through either revision of the MCP form exchange."""
from __future__ import annotations

from hashlib import sha256
import json
from time import monotonic

import anyio
from mcp import types
from mcp.server.request_state import compact_json

from evolution_lab.commands.errors import LocalFailure
from evolution_lab.mcp.facts import Facts, render

_MESSAGES = {
    "approval_declined": "The human declined the approval form.",
    "approval_cancelled": "The client cancelled the approval form.",
    "approval_failed": "The approval form did not return a valid answer.",
    "approval_timeout": "The approval form timed out.",
}
_FORM_ID = "lab-approval"


def _failure(code: str) -> LocalFailure:
    return LocalFailure(code, _MESSAGES[code], stage="approval")


def via_form(session, autonomy: str) -> bool:
    if autonomy != "ask":
        return False
    capabilities = session.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    # A legacy bare elicitation capability advertised form support.
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _confirmed(facts: Facts, answer: types.ElicitResult, tool: str) -> dict:
    if answer.action == "decline":
        raise _failure("approval_declined")
    if answer.action == "cancel":
        raise _failure("approval_cancelled")
    fields = answer.content or {}
    if fields.get("confirm") is False:
        raise _failure("approval_declined")
    if fields.get("confirm") is not True:
        raise _failure("approval_failed")
    result = {"approval": {"via": "form", "autonomy": "ask", "tool": tool}}
    if facts.needs_author:
        author = fields.get("author")
        if not isinstance(author, str) or not author.strip():
            raise _failure("approval_failed")
        result["author"] = author
    return result


async def obtain(session, facts: Facts, *, autonomy: str, tool: str, timeout: float) -> dict:
    """Obtain an in-call form answer, or record the appropriate path without a form."""
    if not via_form(session, autonomy):
        return {"approval": {"via": "none" if autonomy == "yolo" else "client_rules",
                             "autonomy": autonomy, "tool": tool}}
    message, schema = render(facts)
    try:
        with anyio.fail_after(timeout):
            answer = await session.elicit_form(message, schema)
    except TimeoutError:
        raise _failure("approval_timeout") from None
    except Exception:
        raise _failure("approval_failed") from None
    return _confirmed(facts, answer, tool)


def shown_form(request_state: str | None, timeout: float) -> str | None:
    """Read the issuance time and form fingerprint from SDK-verified state."""
    if request_state is None:
        return None
    issued, fingerprint = json.loads(request_state)
    if monotonic() - issued >= timeout:
        raise _failure("approval_timeout")
    return fingerprint


def form_round(facts: Facts, params: types.CallToolRequestParams, shown: str | None) -> dict | types.InputRequiredResult:
    """Compare the fresh form with the one approved, or issue a new question."""
    message, schema = render(facts)
    fingerprint = sha256(compact_json([message, schema], sort_keys=True).encode()).hexdigest()
    if shown != fingerprint:
        return types.InputRequiredResult(
            input_requests={_FORM_ID: types.ElicitRequest(params=types.ElicitRequestFormParams(
                message=message, requested_schema=schema))},
            # RequestStateBoundary protects this payload and binds it to the tool and arguments.
            request_state=compact_json([monotonic(), fingerprint]),
        )
    answer = (params.input_responses or {}).get(_FORM_ID)
    if not isinstance(answer, types.ElicitResult):
        raise _failure("approval_failed")
    return _confirmed(facts, answer, params.name)
