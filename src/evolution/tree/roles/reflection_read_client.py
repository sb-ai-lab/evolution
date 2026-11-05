"""Thin callable and command-line client for ``reflection_reader`` sessions.

Role launchers can import :func:`call` or :func:`stream`; the module entrypoint
is intentionally only a helper, not a registered application service.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from evolution.tree.roles.reflection_reader import (
    MAX_RESPONSE_BYTES,
    MAX_STREAM_FRAME_BYTES,
    ReaderArgumentError,
    ReaderIntegrityError,
)

__all__ = ["call", "main", "stream"]


def call(url: str, capability: str, operation: str, arguments: Mapping[str, Any]) -> Any:
    """Call one allowlisted operation. URL and capability come from the campaign."""

    response = _request(url, capability, operation, arguments, streaming=False)
    try:
        raw = _read_bounded(response, MAX_RESPONSE_BYTES, "response_too_large")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReaderArgumentError("invalid_response") from exc
    finally:
        response.close()


def stream(
    url: str, capability: str, operation: str, arguments: Mapping[str, Any]
) -> Iterator[dict[str, Any]]:
    """Consume bounded NDJSON frames incrementally, retaining no aggregate body."""

    response = _request(url, capability, operation, arguments, streaming=True)
    finished = False
    try:
        while line := response.readline(MAX_STREAM_FRAME_BYTES + 2):
            if len(line) > MAX_STREAM_FRAME_BYTES + 1 or not line.endswith(b"\n"):
                raise ReaderArgumentError("stream_frame_too_large")
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ReaderArgumentError("invalid_stream_frame") from exc
            if not isinstance(payload, dict):
                raise ReaderArgumentError("invalid_stream_frame")
            if payload.get("kind") == "error":
                error = payload.get("error")
                code = error.get("code") if isinstance(error, dict) else "transport_failed"
                safe_code = code if type(code) is str else "transport_failed"
                if payload.get("integrity") is True:
                    raise ReaderIntegrityError(safe_code)
                raise ReaderArgumentError(safe_code)
            if payload.get("kind") == "final":
                finished = True
            yield payload
        if not finished:
            raise ReaderArgumentError("incomplete_stream")
    finally:
        response.close()


def _request(
    url: str, capability: str, operation: str, arguments: Mapping[str, Any], *, streaming: bool
):
    _validate_url(url)
    try:
        body = json.dumps(
            {"operation": operation, "arguments": arguments, "stream": streaming},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReaderArgumentError("invalid_arguments") from exc
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {capability}", "Content-Type": "application/json"},
    )
    try:
        # The mediator is loopback-only.  A dedicated opener prevents ambient
        # proxy settings or redirects from carrying the capability elsewhere.
        return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30)
    except HTTPError as exc:
        try:
            payload = json.loads(
                _read_bounded(exc, MAX_RESPONSE_BYTES, "transport_failed").decode("utf-8")
            )
            code = payload["error"]["code"]
            if type(code) is not str or not 0 < len(code) <= 128:
                code = "transport_failed"
        except (KeyError, TypeError, UnicodeDecodeError, ValueError, ReaderArgumentError):
            code = "transport_failed"
        finally:
            exc.close()
        if exc.code == 409:
            raise ReaderIntegrityError(code) from None
        raise ReaderArgumentError(code) from None
    except (OSError, URLError):
        raise ReaderArgumentError("transport_failed") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_: object):
        return None


def _validate_url(url: str) -> None:
    """Accept precisely the operation mediator URL, before any connection."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ReaderArgumentError("invalid_reader_url") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.path != "/read"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReaderArgumentError("invalid_reader_url")


def _read_bounded(response: Any, limit: int, error: str) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > limit:
                raise ReaderArgumentError(error)
        except ValueError as exc:
            raise ReaderArgumentError("invalid_response") from exc
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ReaderArgumentError(error)
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    """Run a generated-helper-compatible one-operation client."""

    parser = argparse.ArgumentParser(prog="reflection-read-client")
    parser.add_argument("--url", required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("operation")
    parser.add_argument("arguments", nargs="?", default="{}")
    parsed = parser.parse_args(argv)
    try:
        arguments = json.loads(parsed.arguments)
        if not isinstance(arguments, dict):
            raise ReaderArgumentError("invalid_arguments")
        if parsed.stream:
            for frame in stream(parsed.url, parsed.capability, parsed.operation, arguments):
                print(json.dumps(frame, ensure_ascii=False, separators=(",", ":")), flush=True)
        else:
            print(
                json.dumps(
                    call(parsed.url, parsed.capability, parsed.operation, arguments),
                    ensure_ascii=False,
                )
            )
    except ReaderArgumentError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except ReaderIntegrityError as exc:
        print(exc.code, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
