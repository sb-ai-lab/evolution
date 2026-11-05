"""Small standard-library client for an operation-scoped probe mediator."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections.abc import Sequence
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if __package__:
    from .probe_wire import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
else:
    # The sealed agent helper copies both modules and imports them without the
    # evolution package, retaining a standard-library-only execution boundary.
    from probe_wire import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES  # type: ignore[no-redef]

__all__ = ["MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES", "ProbeClientError", "call", "main"]

_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,256}")


class ProbeClientError(RuntimeError):
    """A fixed client-side failure that never invents probe accounting."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_: object):
        return None


def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ProbeClientError("invalid_probe_url") from exc
    segments = parsed.path.split("/")
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or len(segments) != 3
        or segments[0] != ""
        or _CAPABILITY.fullmatch(segments[1]) is None
        or segments[2] != "probe"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProbeClientError("invalid_probe_url")


def _encode_request(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ProbeClientError("invalid_request") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ProbeClientError("invalid_request")
    return encoded


def _read_response(response: Any) -> dict[str, object]:
    length = response.headers.get("Content-Length")
    if length is None or not length.isascii() or not length.isdecimal():
        raise ProbeClientError("invalid_response")
    if len(length) > len(str(MAX_RESPONSE_BYTES)):
        raise ProbeClientError("invalid_response")
    try:
        size = int(length)
    except ValueError as exc:
        raise ProbeClientError("invalid_response") from exc
    if size > MAX_RESPONSE_BYTES:
        raise ProbeClientError("invalid_response")
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES or len(raw) != size:
        raise ProbeClientError("invalid_response")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise ProbeClientError("invalid_response") from exc
    if not isinstance(result, dict):
        raise ProbeClientError("invalid_response")
    if set(result) == {"error", "detail"}:
        if type(result["error"]) is str and type(result["detail"]) is str:
            return cast(dict[str, object], result)
        raise ProbeClientError("invalid_response")
    if set(result) == {"seq", "status", "body", "truncated"}:
        if (
            type(result["seq"]) is int
            and result["seq"] > 0
            and type(result["status"]) is int
            and 100 <= result["status"] <= 599
            and type(result["body"]) is str
            and type(result["truncated"]) is bool
        ):
            return cast(dict[str, object], result)
    raise ProbeClientError("invalid_response")


def call(
    url: str,
    value: object,
    *,
    timeout_seconds: float = 60.0,
    deadline_at: float | None = None,
) -> dict[str, object]:
    """Send once; an operation's monotonic deadline overrides the local timeout.

    Client failures cannot establish whether a request dispatched or completed.
    The mediator owns the operation deadline and authoritative accounting.
    """

    _validate_url(url)
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ProbeClientError("invalid_timeout")
    if deadline_at is not None:
        if type(deadline_at) not in (int, float) or not math.isfinite(deadline_at):
            raise ProbeClientError("invalid_timeout")
        timeout_seconds = deadline_at - time.monotonic()
        if timeout_seconds <= 0:
            raise ProbeClientError("unavailable")
    body = _encode_request(value)
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    response: Any | None = None
    try:
        response = build_opener(ProxyHandler({}), _NoRedirect()).open(
            request, timeout=float(timeout_seconds)
        )
        return _read_response(response)
    except HTTPError as exc:
        response = exc
        try:
            return _read_response(exc)
        except ProbeClientError:
            raise ProbeClientError("unavailable") from None
    except (OSError, URLError):
        raise ProbeClientError("unavailable") from None
    finally:
        if response is not None:
            response.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run one generated-helper-compatible probe request."""

    parser = argparse.ArgumentParser(prog="probe-client")
    parser.add_argument("--url", required=True)
    parser.add_argument("request")
    parsed = parser.parse_args(argv)
    try:
        value = json.loads(parsed.request)
        result = call(parsed.url, value)
    except (RecursionError, UnicodeDecodeError, ValueError):
        error = ProbeClientError("invalid_request")
    except ProbeClientError as exc:
        error = exc
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0
    print(error.code, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
