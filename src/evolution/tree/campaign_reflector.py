"""Campaign reflector orchestration and cooperative deadline enforcement.

Role values and diagnosis admission live in roles.reflector_contracts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from evolution.tree.campaign_editor import EDITOR_GRACE_SECONDS
from evolution.tree.roles.role_failures import ADAPTIVE_GROUP_KEY_ORDER as ADAPTIVE_GROUP_KEY_ORDER

if TYPE_CHECKING:  # pragma: no cover - the annotation only
    pass

# `EDITOR_GRACE_SECONDS` is imported (not redefined) so the two wrappers share
# one grace by construction; `_POLL_INTERVAL_SECONDS` is this module's own
# copy of the editor wrapper's constant, read fresh on every call so a test
# that monkeypatches it here is honoured immediately.
from evolution.tree.roles.reflector_contracts import (
    CandidateReflector,
    ReflectRequest,
    ReflectResult,
    RejectionDetail,
)

_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class ReflectorOutcome:
    """The executor's mapping of one `run_reflector` call, `run_editor`'s twin (spec §9 / §7).

    One field has no editor counterpart: `groups` carries the claimed diagnosis
    of a *finished* reflection through to the caller, which is the only party
    that can prove it (`validate_groups` needs the catalog, the corpus and the
    writable set). Every other terminal carries none.
    """

    terminal: Literal["finished", "failed"]
    status: str | None
    error_class: str | None
    note: str
    alive: bool
    groups: tuple[Mapping[str, Any], ...] = ()
    code: str | None = None
    rejection: RejectionDetail | None = None


def _outcome_for(
    task: asyncio.Task[ReflectResult], *, cancelled_reason: str | None
) -> ReflectorOutcome:
    if task.cancelled():
        return ReflectorOutcome("failed", None, cancelled_reason or "cancelled", "", False)
    exc = task.exception()
    if exc is not None:
        return ReflectorOutcome("failed", None, f"exception:{type(exc).__name__}", str(exc), False)
    result = task.result()
    if cancelled_reason is not None:
        return ReflectorOutcome("failed", None, cancelled_reason, result.note or "", False)
    if result.status == "failed":
        return ReflectorOutcome(
            "failed",
            None,
            "reported",
            result.note,
            False,
            code=result.code,
            rejection=result.rejection,
        )
    return ReflectorOutcome("finished", result.status, None, result.note, False, result.groups)


async def run_reflector(
    reflector: CandidateReflector,
    request: ReflectRequest,
    *,
    cancelled: Callable[[], bool],
    deadline_at: float | None = None,
) -> ReflectorOutcome:
    """Run `reflector.reflect(request)`, enforcing the deadline and cooperative cancellation.

    `run_editor`'s independently readable twin: on a deadline or a `cancelled()`
    signal the task is cancelled and awaited for at most `EDITOR_GRACE_SECONDS`
    (read from the module at call time, so a caller that monkeypatches the
    module attribute is honoured immediately). A task still alive after the
    grace is reported `alive=True`; ending the campaign over it is the
    executor's decision, not this function's.
    """

    task: asyncio.Task[ReflectResult] = asyncio.ensure_future(reflector.reflect(request))
    if deadline_at is None:
        deadline_at = time.monotonic() + request.deadline_seconds
    reason: str | None = None
    while not task.done():
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            reason = "timeout"
            break
        if cancelled():
            reason = "cancelled"
            break
        await asyncio.wait({task}, timeout=min(_POLL_INTERVAL_SECONDS, remaining))
    if reason is None:
        return _outcome_for(task, cancelled_reason=None)
    task.cancel()
    _done, pending = await asyncio.wait({task}, timeout=EDITOR_GRACE_SECONDS)
    if task in pending:
        return ReflectorOutcome("failed", None, "unresponsive", "", True)
    return _outcome_for(task, cancelled_reason=reason)
