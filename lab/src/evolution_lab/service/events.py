"""Per-consumer sampled SSE through public Owner status reads.

progress и время — те же формы, что в статусе ``lab-operation-status/7``.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from starlette.responses import JSONResponse, Response

from evolution_lab.owner import OperationStatus, ResourceRef
from evolution_lab.owner.models import _thaw_json as _thaw

from .errors import ServiceError, error_response


POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
SEND_SECONDS = 30.0
_TERMINAL = frozenset({"finished", "orphaned", "cleanup_required", "released"})
_REMOVED = frozenset({"deleting", "delete_failed", "deleted"})


class _ObservationClosed(Exception):
    """A send failed; distinct from an error raised by a public status read."""


def _summary(status: OperationStatus) -> dict:
    """Project only small public facts, without thawing/copying the core payload."""
    result = status.result
    available = status.results_state not in _REMOVED
    core = result.core if result is not None and available else None
    return {
        "kind": status.kind, "control_state": status.control_state,
        "process": status.process.to_payload(), "target_cleanup": status.target_cleanup.to_payload(),
        "results_state": status.results_state,
        "failure": None if status.failure is None else status.failure.to_payload(),
        "result_available": core is not None,
        "persistence": None if result is None else result.persistence.to_payload(),
        "progress": _thaw(status.progress), "progress_state": status.progress_state,
        "times": _thaw(status.times), "times_state": status.times_state,
        "times_source": status.times_source, "times_reason": status.times_reason,
        "start": status.starts[-1].start if status.starts else 1,
    }


def _frame(event: str, data: dict, identifier: str | None = None) -> bytes:
    prefix = f"event: {event}\n"
    if identifier is not None:
        prefix += f"id: {identifier}\n"
    return (prefix + "data: " + json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n\n").encode("utf-8")


class _EventResponse(Response):
    """Own actual ASGI sends: generator yields cannot bound network backpressure."""

    def __init__(self, runtime, operation_id: str):
        super().__init__(media_type="text/event-stream", headers={"Cache-Control": "no-store"})
        self.raw_headers = [(key, value) for key, value in self.raw_headers if key != b"content-length"]
        self._runtime = runtime
        self._operation_id = operation_id

    async def __call__(self, scope, receive, send):
        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass

        if self._runtime.stopping.is_set():
            # This request has not opened an observation. Preserve the normal
            # admission error even if shutdown began after the handler returned.
            value = error_response(ServiceError("service_stopping"))
            try:
                async with asyncio.timeout(SEND_SECONDS):
                    await JSONResponse(value["body"], status_code=value["status"],
                        headers={"Cache-Control": "no-store"})(scope, receive, send)
            except (OSError, TimeoutError):
                pass
            return
        observers = [asyncio.create_task(disconnected()), asyncio.create_task(self._runtime.stopping.wait())]
        stream = asyncio.create_task(self._stream(scope, receive, send))
        tasks = [stream, *observers]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if stream in done:
                await stream
        finally:
            # Cancel only observation wrappers. ServiceRuntime shields and tracks
            # any already-admitted Owner call through its actual completion.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _read(self):
        status = await self._runtime.call_owner("status", ResourceRef("operation", self._operation_id))
        # The task result contains no OperationStatus/core, including while sending.
        return status.owner_generation, _summary(status), asyncio.get_running_loop().time()

    async def _stream(self, scope, receive, send):
        loop = asyncio.get_running_loop()
        heartbeat_at = loop.time() + HEARTBEAT_SECONDS

        async def timed_send(message):
            nonlocal heartbeat_at
            try:
                async with asyncio.timeout(SEND_SECONDS):
                    await send(message)
            except (OSError, TimeoutError):
                raise _ObservationClosed from None
            heartbeat_at = loop.time() + HEARTBEAT_SECONDS

        async def body(frame: bytes, *, more=True):
            await timed_send({"type": "http.response.body", "body": frame, "more_body": more})

        async def waiting(awaitable):
            # One awaited read or delay, never a background snapshot producer.
            task = asyncio.create_task(awaitable)
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=max(0.0, heartbeat_at - loop.time()))
                    if done:
                        return task.result()
                    await body(b": heartbeat\n\n")
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            try:
                generation, summary, read_at = await self._read()
            except Exception as error:
                value = error_response(error)
                await JSONResponse(value["body"], status_code=value["status"])(scope, receive, timed_send)
                return

            await timed_send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            stream_id = uuid.uuid4().hex
            sequence = 1

            async def publish():
                await body(_frame("status", {
                    "schema": "lab-operation-event/3", "operation_id": self._operation_id,
                    "owner_generation": generation, "stream_id": stream_id, "sequence": sequence,
                    "kind": "status", "payload": summary,
                }, f"{stream_id}:{sequence}"))

            await publish()
            while summary["control_state"] not in _TERMINAL:
                await waiting(asyncio.sleep(max(0.0, read_at + POLL_SECONDS - loop.time())))
                try:
                    generation, current, read_at = await waiting(self._read())
                except _ObservationClosed:
                    raise
                except Exception as error:
                    await body(_frame("error", error_response(error)["body"]))
                    break
                if current != summary:
                    summary = current
                    sequence += 1
                    await publish()
                del current
            await body(b"", more=False)
        except _ObservationClosed:
            # A disconnected/slow consumer ends observation, never owned work.
            return


def event_response(runtime, operation_id: str, request) -> Response:
    """Return a fresh observation, refusing unsupported replay before any read."""
    if "last-event-id" in request.headers:
        raise ServiceError("history_unavailable")
    return _EventResponse(runtime, operation_id)
