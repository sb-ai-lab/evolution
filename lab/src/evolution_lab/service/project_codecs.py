"""Closed request codecs of the project routes (spec §4.2).

Shapes are closed like every other service request: exact key sets, explicit schemas,
``implementation`` always present. ``null`` leaves the choice to the session: the only
active implementation, or project-level applicability for a confirmation (R16).
Project-level validation (ids, paths, mutations, claims) stays in ``evolution_lab.projects``.
"""
from __future__ import annotations

from typing import Any, Mapping

from evolution_lab.owner.models import Approval, TargetRef

from .codecs import (
    PrepareRequest,
    _approval,
    _closed,
    _digest,
    _string,
    decode_prepare_request,
    decode_secret_map,
    decode_target_ref,
)
from .errors import ServiceError

#: Project prepare request version -> the prepare request version its fields are (user decision 2026-10-09).
PROJECT_PREPARE_SCHEMAS = {"lab-service-project-prepare-request/1": "lab-service-prepare-request/2",
                           "lab-service-project-prepare-request/2": "lab-service-prepare-request/3",
                           "lab-service-project-prepare-request/3": "lab-service-prepare-request/5"}


def _invalid() -> None:
    raise ServiceError("invalid_request")


def _optional_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def decode_project_create(payload: Any) -> dict:
    """``lab-service-project-create/1`` into ``Projects.create`` keywords."""
    row = _closed(payload, {"schema", "id", "name", "materials_root", "implementations", "source", "exclude"},
                  schema="lab-service-project-create/1")
    source = row["source"]
    if isinstance(source, Mapping) and source.get("kind") == "path":
        _closed(source, {"kind", "path"})
        where = {"path": _string(source["path"]), "clone": None}
    elif isinstance(source, Mapping) and source.get("kind") == "clone":
        _closed(source, {"kind", "repo", "rev"})
        where = {"path": None, "clone": {"repo": _string(source["repo"]), "rev": _optional_string(source["rev"])}}
    else:
        _invalid()
    if not isinstance(row["implementations"], list) or not isinstance(row["exclude"], list):
        _invalid()
    # Implementation specs are validated by Projects.create (closed keys, ids, places).
    return {"id": _optional_string(row["id"]), "name": _string(row["name"]),
            "materials_root": _string(row["materials_root"]), "implementations": row["implementations"],
            "exclude": [_string(item) for item in row["exclude"]], **where}


def decode_project_update(payload: Any) -> tuple[dict, int | None]:
    """``lab-service-project-update/1``: one mutation plus an optional record version."""
    row = _closed(payload, {"schema", "mutation", "expected_record_version"}, schema="lab-service-project-update/1")
    expected = row["expected_record_version"]
    if not isinstance(row["mutation"], Mapping) or (
            expected is not None and (isinstance(expected, bool) or not isinstance(expected, int))):
        _invalid()
    return dict(row["mutation"]), expected


def decode_project_workspace(payload: Any) -> str:
    row = _closed(payload, {"schema", "autonomy"}, schema="lab-service-project-workspace-request/1")
    autonomy = row["autonomy"]
    if autonomy not in ("ask", "yolo"):
        _invalid()
    return autonomy


def decode_project_bundle(payload: Any) -> str | None:
    row = _closed(payload, {"schema", "implementation"}, schema="lab-service-project-bundle-request/1")
    return _optional_string(row["implementation"])


def decode_project_prepare(payload: Any) -> tuple[str | None, PrepareRequest]:
    """Decode project /1, /2, /3 as prepare /2, /3, /5 plus implementation."""
    if not isinstance(payload, Mapping) or "implementation" not in payload:
        _invalid()
    if not isinstance(payload.get("schema"), str) or payload["schema"] not in PROJECT_PREPARE_SCHEMAS:
        raise ServiceError("unsupported_schema")
    inner = {name: value for name, value in payload.items() if name != "implementation"}
    inner["schema"] = PROJECT_PREPARE_SCHEMAS[payload["schema"]]
    return _optional_string(payload["implementation"]), decode_prepare_request(inner)


def decode_project_target(payload: Any) -> tuple[str | None, str, dict]:
    row = _closed(payload, {"schema", "implementation", "bundle_digest", "secrets"},
                  schema="lab-service-project-target-request/1")
    return _optional_string(row["implementation"]), _digest(row["bundle_digest"]), decode_secret_map(row["secrets"])


def _closed_approval_version(payload: Any, keys: set[str], schema_prefix: str) -> Mapping[str, Any]:
    approved = f"{schema_prefix}/2"
    if isinstance(payload, Mapping) and payload.get("schema") == approved:
        return _closed(payload, keys | {"approval"}, schema=approved)
    return _closed(payload, keys, schema=f"{schema_prefix}/1")


def decode_project_start(payload: Any) -> tuple[str | None, str, TargetRef, dict, Approval | None]:
    """The fields of ``lab-service-start-request/1`` plus ``implementation`` and approval in /2."""
    row = _closed_approval_version(payload, {"schema", "implementation", "prepared_digest", "target_ref", "secrets"},
                                   "lab-service-project-start-request")
    return (_optional_string(row["implementation"]), _digest(row["prepared_digest"]),
            decode_target_ref(row["target_ref"]), decode_secret_map(row["secrets"]), _approval(row))


def decode_confirm(payload: Any) -> dict:
    """``implementation: null`` is project-level (R16)."""
    keys = {"schema", "implementation", "claim", "attests", "linked", "channel", "author", "note"}
    row = _closed_approval_version(payload, keys, "lab-service-confirm-request")
    if not isinstance(row["attests"], Mapping) or not isinstance(row["linked"], list):
        _invalid()
    if not all(isinstance(item, Mapping) for item in row["linked"]):
        _invalid()
    return {"implementation_id": _optional_string(row["implementation"]), "claim": _string(row["claim"]),
            "attests": dict(row["attests"]), "linked": [dict(item) for item in row["linked"]],
            "channel": _string(row["channel"]), "author": _string(row["author"]), "note": _optional_string(row["note"]),
            "approval": _approval(row)}


ACCEPT_REQUEST_SCHEMAS = (
    "lab-service-accept-request/1", "lab-service-accept-request/2", "lab-service-accept-request/3",
)


def decode_accept(payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        _invalid()
    schema = payload.get("schema")
    keys = {"schema", "implementation", "only", "across_pairs", "reason", "channel", "author", "dry_run"}
    if schema in ACCEPT_REQUEST_SCHEMAS[1:]:
        keys.add("expected")
    if schema == ACCEPT_REQUEST_SCHEMAS[2]:
        keys.add("approval")
    row = _closed(payload, keys)
    if schema not in ACCEPT_REQUEST_SCHEMAS:
        raise ServiceError("unsupported_schema")
    expected = row.get("expected")
    if expected is not None:
        if not isinstance(expected, list) or not expected:
            _invalid()
        for entry in expected:
            _closed(entry, {"id", "from", "to"})
    if not isinstance(row["across_pairs"], bool) or not isinstance(row["dry_run"], bool):
        _invalid()
    if schema == ACCEPT_REQUEST_SCHEMAS[2] and not row["dry_run"] and expected is None:
        _invalid()
    only = row["only"]
    if only is not None and (not isinstance(only, list) or not all(isinstance(item, str) for item in only)):
        _invalid()
    return {"implementation_id": _optional_string(row["implementation"]), "only": only,
            "across_pairs": row["across_pairs"], "reason": _string(row["reason"]),
            "channel": _string(row["channel"]), "author": _string(row["author"]), "dry_run": row["dry_run"],
            "expected": expected, "approval": _approval(row)}


def decode_project_demo(payload: Any) -> str | None:
    """``lab-service-project-demo-request/1``: ``{schema, id: <id|null>}``."""
    row = _closed(payload, {"schema", "id"}, schema="lab-service-project-demo-request/1")
    return _optional_string(row["id"])


def decode_revoke(payload: Any) -> tuple[str, Approval | None]:
    row = _closed_approval_version(payload, {"schema", "reason"}, "lab-service-revoke-request")
    return _string(row["reason"]), _approval(row)


def decode_project_port(payload: Any) -> dict:
    """``lab-service-project-port-request/1`` into ``ProjectSession.port`` keywords (spec 2026-10-01 §3).
    Path and template rules stay in ``evolution_lab.projects.port``."""
    row = _closed(payload, {"schema", "from", "template", "ref", "id", "into"},
                  schema="lab-service-project-port-request/1")
    return {"source": _string(row["from"]), "template": _string(row["template"]), "ref": _optional_string(row["ref"]),
            "port_id": _optional_string(row["id"]), "into": _optional_string(row["into"])}


def decode_project_sync_check(payload: Any) -> dict:
    """``lab-service-project-sync-check-request/1`` into ``ProjectSession.sync_check`` keywords (PDR-0024)."""
    row = _closed(payload, {"schema", "rev"}, schema="lab-service-project-sync-check-request/1")
    return {"rev": _optional_string(row["rev"])}


def decode_project_sync(payload: Any) -> dict:
    """The exact SHA form is checked by ``ProjectSession.sync_apply``."""
    row = _closed_approval_version(payload, {"schema", "upstream"}, "lab-service-project-sync-request")
    return {"upstream": _string(row["upstream"]), "approval": _approval(row)}


PROBE_TURN_LIMIT = 1 << 20
# JSON may spell every character of the text as a 6-byte \uXXXX escape, so the wire limit of the turn route
# is wider than the general body limit (which would refuse a 1 MiB text with its envelope).
PROBE_TURN_BODY_LIMIT = 6 * PROBE_TURN_LIMIT + 4096
DEFAULT_PROBE_TIMEOUT = 120


def decode_project_probe(payload: Any) -> dict:
    """``lab-service-project-probe-request/1`` into ``ProjectSession.probe_start`` keywords.

    All keys are present; ``budget``, ``seed`` and ``turn_timeout_seconds`` may be ``null`` (the timeout then
    defaults to 120 s). The ceiling and budget contents are checked by the session (spec §4.3, §4.4)."""
    row = _closed(payload, {"schema", "implementation", "ceiling", "budget", "turn_timeout_seconds", "seed"},
                  schema="lab-service-project-probe-request/1")
    timeout, seed = row["turn_timeout_seconds"], row["seed"]
    if not isinstance(row["ceiling"], Mapping) or not (row["budget"] is None or isinstance(row["budget"], Mapping)):
        _invalid()
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
        _invalid()
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        _invalid()
    return {"implementation": _string(row["implementation"]), "ceiling": dict(row["ceiling"]),
            "budget": None if row["budget"] is None else dict(row["budget"]),
            "turn_timeout_seconds": DEFAULT_PROBE_TIMEOUT if timeout is None else timeout, "seed": seed}


def decode_probe_turn(payload: Any) -> str:
    """``lab-service-probe-turn-request/1``: a non-empty text of at most 1 MiB UTF-8."""
    row = _closed(payload, {"schema", "content"}, schema="lab-service-probe-turn-request/1")
    content = _string(row["content"])
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        _invalid()
    if size > PROBE_TURN_LIMIT:
        _invalid()
    return content
