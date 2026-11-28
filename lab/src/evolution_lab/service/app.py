"""Optional FastAPI construction for the local UDS service."""
from __future__ import annotations

import json

from .errors import ServiceError, error_response
from .routes import EventHandler, install_routes


class _NoStoreASGI:
    """Stamp every HTTP response without buffering a streaming response body."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = False

        async def stamped(message):
            nonlocal started
            if message["type"] == "http.response.start":
                headers = [item for item in message.get("headers", ()) if item[0].lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
                started = True
            await send(message)

        try:
            await self._app(scope, receive, stamped)
        except Exception as error:
            # Starlette's outer error middleware intentionally re-raises after
            # its safe handler sends a response. Do not turn that response into
            # a second framework failure. A pre-response framework error still
            # receives the same closed envelope.
            if started:
                return
            value = error_response(error)
            body = json.dumps(value["body"], separators=(",", ":"), allow_nan=False).encode("utf-8")
            await stamped({"type": "http.response.start", "status": value["status"],
                "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii"))]})
            await stamped({"type": "http.response.body", "body": body})


def create_app(runtime, *, event_handler: EventHandler | None = None):
    """Create a closed /v1 application; Owner lifetime remains in ServiceRuntime."""
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException

    app = FastAPI(debug=False, docs_url=None, redoc_url=None, openapi_url=None)
    app.router.redirect_slashes = False

    def safe_response(error, *, status: int | None = None):
        value = error_response(error)
        return JSONResponse(value["body"], status_code=value["status"] if status is None else status,
            headers={"Cache-Control": "no-store"})

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, error: ServiceError):
        return safe_response(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError):
        return safe_response(ServiceError("invalid_request"))

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, error: HTTPException):
        if error.status_code == 404:
            return safe_response(ServiceError("not_found"))
        if error.status_code == 405:
            return safe_response(ServiceError("invalid_request"), status=405)
        return safe_response(ServiceError("lab_error"), status=error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, error: Exception):
        return safe_response(error)

    if event_handler is None:
        from .events import event_response

        def event_handler(operation_id, request):
            return event_response(runtime, operation_id, request)

    install_routes(app, runtime, event_handler=event_handler)
    return _NoStoreASGI(app)
