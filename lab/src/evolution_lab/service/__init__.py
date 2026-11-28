"""Closed transport codecs for the optional local service.

The package initializer deliberately has no FastAPI or uvicorn imports so the
existing Python Owner remains usable without the optional service dependency.
"""

from .codecs import (
    BundleRequest,
    PrepareRequest,
    decode_bind_request,
    decode_bundle_request,
    decode_digest,
    decode_empty_request,
    decode_http_json_object,
    decode_identifier,
    decode_json_object,
    decode_prepare_request,
    decode_resource_ref,
    decode_runtime_policy,
    decode_secret_map,
    decode_target_ref,
    decode_target_query,
    decode_task_ref,
    decode_task_query,
    serialize_public,
)
from .errors import ServiceError, error_response, translate_exception

__all__ = [
    "BundleRequest", "PrepareRequest", "ServiceError",
    "decode_bind_request", "decode_bundle_request", "decode_digest", "decode_empty_request", "decode_http_json_object", "decode_identifier", "decode_json_object",
    "decode_prepare_request", "decode_resource_ref",
    "decode_runtime_policy", "decode_secret_map", "decode_target_query", "decode_target_ref",
    "decode_task_query", "decode_task_ref", "error_response",
    "serialize_public", "translate_exception",
]
