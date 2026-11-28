"""Small synchronous UDS client used only by the command line interface."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import httpx

from evolution_lab.service.codecs import decode_json_object

from .responses import error as is_error_envelope
from .responses import operation_event
from .responses import validate


class ClientError(RuntimeError):
    pass


def _event_data(event: str, data: str) -> dict[str, Any]:
    """Decode one SSE frame: ``status`` frames are closed ``lab-operation-event/3``;
    ``error`` frames stay opaque error bodies; any other event name is rejected."""
    try:
        value = decode_json_object(data.encode(), max_bytes=None)
        if event == "status":
            return operation_event(value)
        if event == "error":
            return value
        raise ValueError("unknown event")
    except Exception:
        raise ClientError("invalid service response") from None


class RequestTimeout(ClientError):
    pass


class RequestInterrupted(ClientError):
    pass


class AcceptanceUnknown(ClientError):
    def __init__(self, cause: str):
        super().__init__(cause)
        self.cause = cause


class ApiError(ClientError):
    def __init__(self, payload: dict[str, Any]):
        super().__init__("service error")
        self.payload = payload


class ServiceClient:
    """One-request-at-a-time httpx UDS client; mutation calls never retry."""

    def __init__(self, *, socket_path: Path | str, timeout: float | None = None, transport=None):
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError("socket path must be absolute")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        self._transport = httpx.HTTPTransport(uds=str(path), retries=0) if transport is None else transport
        self._timeout = httpx.Timeout(connect=5.0, read=timeout, write=timeout, pool=timeout)
        self._client = httpx.Client(
            transport=self._transport, base_url="http://lab", timeout=self._timeout,
            trust_env=False, follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _ambiguous(error: BaseException) -> AcceptanceUnknown:
        if isinstance(error, KeyboardInterrupt):
            return AcceptanceUnknown("interrupted")
        if isinstance(error, httpx.TimeoutException):
            return AcceptanceUnknown("timeout")
        return AcceptanceUnknown("transport_error")

    def request_json(self, method: str, path: str, *, payload: dict[str, Any] | None = None,
                     mutation: bool = False, expected_schema: str | None = None,
                     timeout: float | None = None,
                     mutation_key: tuple[str, str] | None = None) -> dict[str, Any]:
        if mutation_key is not None and not mutation:
            raise ValueError("a request key applies only to mutations")
        headers = {} if payload is None else {"content-type": "application/json"}
        if mutation_key is not None:
            headers["lab-request-id"], headers["lab-owner-generation"] = mutation_key
        content = None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        try:
            request = {"headers": headers, "content": content}
            if timeout is not None:
                request["timeout"] = timeout
            response = self._client.request(method, path, **request)
            value = decode_json_object(response.content, max_bytes=None)
            if is_error_envelope(value):
                raise ApiError(value)
            if not response.is_success:
                raise ValueError("invalid service response")
            validated = validate(value, expected_schema)
            if mutation and expected_schema == "lab-task-ref/1":
                self._accepted_task = validated
                self._accepted_task_ref = value
        except KeyboardInterrupt:
            if mutation:
                raise self._ambiguous(KeyboardInterrupt()) from None
            raise RequestInterrupted() from None
        except httpx.TimeoutException as error:
            if mutation:
                raise self._ambiguous(error) from None
            raise RequestTimeout() from None
        except httpx.HTTPError as error:
            if mutation:
                raise self._ambiguous(error) from None
            raise ClientError("transport error") from None
        except ApiError:
            raise
        except Exception:
            if mutation:
                raise AcceptanceUnknown("transport_error") from None
            raise ClientError("invalid service response") from None
        return value

    def get_bytes(self, path: str, content_type: str) -> bytes:
        """A non-JSON body of the expected media type (``application/zip`` for export-content, ``text/markdown`` for
        report.md — ADR-0022 p. 3); another type is an error, a non-2xx answer is the JSON error envelope."""
        try:
            response = self._client.get(path)
            if not response.is_success:
                try:
                    value = decode_json_object(response.content, max_bytes=None)
                except Exception:
                    value = None
                if is_error_envelope(value):
                    raise ApiError(value)
                raise ClientError("invalid bytes response")
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != content_type:
                raise ClientError("invalid bytes response")
            return response.content
        except KeyboardInterrupt:
            raise RequestInterrupted() from None
        except httpx.TimeoutException:
            raise RequestTimeout() from None
        except httpx.HTTPError:
            raise ClientError("transport error") from None

    def stream_events(self, path: str) -> Iterator[dict[str, Any]]:
        try:
            with self._client.stream("GET", path, headers={"accept": "text/event-stream"}) as response:
                if not response.is_success:
                    try:
                        value = decode_json_object(response.read(), max_bytes=None)
                        if not is_error_envelope(value):
                            raise ValueError("invalid service response")
                        raise ApiError(value)
                    except ApiError:
                        raise
                    except Exception:
                        raise ClientError("invalid service response") from None
                event = None
                data = None
                for line in response.iter_lines():
                    if not line:
                        if event is not None and data is not None:
                            yield {"event": event, "data": _event_data(event, data)}
                        event = data = None
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
        except KeyboardInterrupt:
            raise RequestInterrupted() from None
        except httpx.TimeoutException:
            raise RequestTimeout() from None
        except httpx.HTTPError:
            raise ClientError("transport error") from None
