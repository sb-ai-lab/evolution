"""A scripted OpenAI-compatible endpoint on loopback, for the chat editor's campaigns.

`tests/tree/roles/editor/test_chat_editor.py` drives `ChatEditor` through an injected
`client_factory`, which proves the tool loop but never the binding: a campaign
started from a `/2` manifest builds its own `openai.AsyncOpenAI` inside
`bindings.build_editor`, and nothing about that path is exercised unless a real
socket answers it. This module is that socket -- a `ThreadingHTTPServer` on a
daemon thread that answers `POST .../chat/completions` from a fixed script and
starts the script over when it runs out, so a campaign that makes more attempts
than the script has turns still terminates.

The responses are the OpenAI chat-completion JSON the SDK parses into the
objects `chat_editor._tool_calls` reads: one `choices[0].message.tool_calls`
entry per turn. `tool_call` builds one, `completion` a plain text answer;
`start` serves them.

A script item may also be `{"status": <int>}` -- served as that HTTP status with
a JSON error body `{"error": {"message": "scripted failure", "code": <status>}}`,
whose `message`, `code` and `type` the item may set (`"code": "server_busy"`,
say: a provider code the role logs record, #290) -- or `{"delay": <seconds>, "body": <item>}`, served after the
delay, or a callable taking the request JSON and returning an item (the grouping
model's answer names run-derived evidence IDs only its request carries).
`server.script.requests` holds every request body received, in order, and
`server.script.headers` the same requests' headers: one tuple of
`(lower-cased name, value)` pairs per request, repeats kept, so a test can say
which credential headers left the client (#290).

`start(..., strict=mode)` makes the server a strict provider
(`tests/strict_history.py`, b18): a request whose history holds an assistant
tool call with arguments that are not one JSON object is refused (`http_400`
or `error_body`) before the script is consulted, so the refusal consumes no
script item. `server.script.refusals` holds the broken call IDs of every
refused request, in order. Without `strict` the server behaves as before.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.strict_history import Mode

__all__ = ["completion", "refuse_parallel", "router_refusal", "start", "tool_call"]

MODEL = "e1-editor-model"
"""The `model` the scripted completions report; the manifest declares the same."""


def router_refusal(code: object, message: str, provider: str | None = None) -> dict:
    """An HTTP-200 router error without completion identity or choices."""
    return {"error": {"code": code, "message": message, "metadata": {"provider_name": provider}}}


def refuse_parallel(normal: Any, refusal: dict) -> Callable[[dict], Any]:
    """Refuse the parallel-call parameter, then serve the ordinary script."""

    def respond(request: dict) -> Any:
        if "parallel_tool_calls" in request:
            return refusal
        return normal(request) if callable(normal) else normal

    return respond


def tool_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """One chat completion whose single choice is one native function call."""

    return {
        "id": "chatcmpl-e1",
        "object": "chat.completion",
        "created": 1_757_000_000,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call-{name}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments or {}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def completion(content: str) -> dict[str, Any]:
    """One chat completion whose single choice is the plain text `content`."""

    return {
        "id": "chatcmpl-e1",
        "object": "chat.completion",
        "created": 1_757_000_000,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _Script:
    """The scripted responses, handed out in order and looped, under one lock."""

    def __init__(self, responses: Sequence[Any], strict: Mode | None = None) -> None:
        if not responses:
            raise ValueError("the script needs at least one response")
        self._responses = list(responses)
        self._lock = threading.Lock()
        self._index = 0
        self._refuse: Callable[[Any], Any] | None = None
        self._broken: Callable[[Mapping[str, Any]], list[str]] | None = None
        if strict is not None:
            # Imported here, not at module level: `scripts/e1_evolve_smoke.py`
            # loads this file by path, where `tests` is not importable.
            from tests.strict_history import broken_history_calls, strict_history

            self._refuse = strict_history(None, mode=strict)
            self._broken = broken_history_calls
        self.served = 0
        self.requests: list[Any] = []
        self.headers: list[tuple[tuple[str, str], ...]] = []
        self.refusals: list[list[str]] = []

    def next(self, request: Any = None, headers: tuple[tuple[str, str], ...] = ()) -> Any:
        with self._lock:
            if (
                self._refuse is not None
                and self._broken is not None
                and isinstance(request, Mapping)
            ):
                broken = self._broken(request)
                if broken:
                    self.refusals.append(broken)
                    self.requests.append(request)
                    self.headers.append(headers)
                    return self._refuse(request)
            response = self._responses[self._index % len(self._responses)]
            self._index += 1
            self.served += 1
            self.requests.append(request)
            self.headers.append(headers)
            return response


_ERROR_KEYS = frozenset({"status", "message", "code", "type"})


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - `BaseHTTPRequestHandler`'s name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self.path.endswith("/chat/completions"):
            self._send(404, {"error": {"message": "no such endpoint"}})
            return
        try:
            request = json.loads(raw) if raw else None
        except ValueError:
            request = None
        headers = tuple((name.lower(), value) for name, value in self.headers.items())
        item = self.server.script.next(request, headers)  # type: ignore[attr-defined]
        if callable(item):
            item = item(request)
        if isinstance(item, dict) and "status" in item and set(item) <= _ERROR_KEYS:
            status = int(item["status"])
            error = {"message": "scripted failure", "code": status}
            error.update({key: item[key] for key in ("message", "code", "type") if key in item})
            self._send(status, {"error": error})
            return
        if isinstance(item, dict) and set(item) == {"delay", "body"}:
            time.sleep(float(item["delay"]))
            item = item["body"]
        self._send(200, item)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the handler's stderr line per request."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script: _Script) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script


def start(responses: Sequence[Any], *, strict: Mode | None = None) -> tuple[str, _Server]:
    """Serve `responses` in order, looping, and return `(base_url, server)`.

    `strict`, when given, refuses a request whose history holds a broken tool
    call in that mode (see the module docstring).

    The caller shuts the server down (`shutdown()` then `server_close()`); the
    serving thread is a daemon, so a test that forgets cannot hang the suite.
    """

    server = _Server(_Script(responses, strict))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    server.thread = thread  # type: ignore[attr-defined]
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/v1", server
