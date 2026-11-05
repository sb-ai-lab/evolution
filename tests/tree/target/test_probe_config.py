"""Probe configuration and public manifest activation contracts."""

from __future__ import annotations

import json

import pytest

from evolution.tree.manifest import manifest_v2
from evolution.tree.reference_target_cli import scaffold


def _parse(value: object, *, target: str = "http://127.0.0.1:8765"):
    return manifest_v2._parse_probe_config(
        value, "reflector.probe", target_base_url=target, schema=manifest_v2.SCHEMA_V15
    )


def test_private_probe_parser_applies_defaults_and_normalizes_loopback() -> None:
    probe = _parse({"base_url": "http://[::1]:8099/"})
    assert probe == manifest_v2.ProbeSection(
        base_url="http://[::1]:8099",
        health_path="/health",
        methods=("GET", "POST"),
        budget=32,
        request_timeout_seconds=60.0,
        description=None,
    )


def test_private_probe_parser_preserves_complete_declared_configuration() -> None:
    probe = _parse(
        {
            "base_url": "http://localhost:8099",
            "health_path": "/ready?deep=1",
            "methods": ["DELETE", "PUT", "POST", "GET"],
            "budget": 7,
            "request_timeout_seconds": 2.5,
            "description": "Disposable environment",
        }
    )
    assert probe == manifest_v2.ProbeSection(
        base_url="http://localhost:8099",
        health_path="/ready?deep=1",
        methods=("DELETE", "PUT", "POST", "GET"),
        budget=7,
        request_timeout_seconds=2.5,
        description="Disposable environment",
    )


@pytest.mark.parametrize(
    ("value", "code", "path"),
    [
        ({}, "missing_key", "reflector.probe.base_url"),
        ({"base_url": "http://127.0.0.1:8099", "extra": 1}, "unknown_key", "reflector.probe.extra"),
        ({"base_url": 3}, "bad_type", "reflector.probe.base_url"),
        ({"base_url": "http://127.0.0.1"}, "not_loopback", "reflector.probe.base_url"),
        (
            {"base_url": "http://127.0.0.1:8099", "methods": "GET"},
            "bad_type",
            "reflector.probe.methods",
        ),
        (
            {"base_url": "http://127.0.0.1:8099", "budget": True},
            "bad_type",
            "reflector.probe.budget",
        ),
        (
            {"base_url": "http://127.0.0.1:8099", "request_timeout_seconds": 0},
            "bad_value",
            "reflector.probe.request_timeout_seconds",
        ),
        (
            {"base_url": "http://127.0.0.1:8099", "description": 1},
            "bad_type",
            "reflector.probe.description",
        ),
        (
            {"base_url": "http://127.0.0.1:8099", "description": "\ud800"},
            "bad_value",
            "reflector.probe.description",
        ),
    ],
)
def test_private_probe_parser_refuses_wrong_shapes_and_values(value, code, path) -> None:
    with pytest.raises(manifest_v2.ManifestError) as info:
        _parse(value)
    assert (info.value.code, info.value.detail) == (code, path)


@pytest.mark.parametrize(
    ("probe_url", "target_url"),
    [
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("http://localhost:8765", "http://127.0.0.1:8765"),
        ("http://[::1]:8765", "http://localhost:8765"),
        ("http://127.0.0.1:080", "http://[::1]:80"),
    ],
)
def test_private_probe_parser_refuses_same_loopback_origin_aliases(
    probe_url: str, target_url: str
) -> None:
    with pytest.raises(manifest_v2.ManifestError) as info:
        _parse({"base_url": probe_url}, target=target_url)
    assert (info.value.code, info.value.detail) == (
        "bad_value",
        "reflector.probe.base_url",
    )


def test_private_probe_parser_accepts_a_distinct_explicit_port() -> None:
    probe = _parse(
        {"base_url": "http://localhost:8766"},
        target="http://[::1]:8765",
    )
    assert probe.base_url == "http://localhost:8766"


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "//authority/path",
        "/a\\b",
        "/a\x00b",
        "/a#fragment",
        "/a/./b",
        "/a/../b",
        "/a/%2e%2e/b",
        "/a/%3F/../b",
        "/a/%3F/%2e%2e/b",
        "/a/%2F/b",
        "/a/%5c/b",
        "/a/%252e%252e/b",
        "/a/%C2%80/b",
        "/a/%E2%80%AE/b",
        "/bad%escape",
        "/" + "x" * 2048,
    ],
)
def test_safe_probe_path_refuses_ambiguous_or_unsafe_destinations(path: str) -> None:
    with pytest.raises(manifest_v2.ManifestError) as info:
        manifest_v2._validate_probe_path(path, "probe.path")
    assert (info.value.code, info.value.detail) == ("bad_value", "probe.path")


def test_safe_probe_path_accepts_absolute_path_and_query() -> None:
    assert (
        manifest_v2._validate_probe_path("/objects/%E2%9C%93?q=a/b", "probe.path")
        == "/objects/%E2%9C%93?q=a/b"
    )


def test_safe_probe_path_ignores_traversal_like_query_data() -> None:
    value = "/objects?next=/../secret&encoded=%2e%2e"
    assert manifest_v2._validate_probe_path(value, "probe.path") == value


def test_safe_probe_path_reports_wrong_type() -> None:
    with pytest.raises(manifest_v2.ManifestError) as info:
        manifest_v2._validate_probe_path(["/health"], "reflector.probe.health_path")
    assert (info.value.code, info.value.detail) == (
        "bad_type",
        "reflector.probe.health_path",
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_public_v4_manifest_accepts_probe_configuration(tmp_path, enabled) -> None:
    scaffold(tmp_path / "demo")
    path = tmp_path / "demo" / "manifest.json"
    document = json.loads(path.read_text())
    assert document["schema"] == "evolution-v2-manifest/15"
    assert document["reflector"]["probe"] is None
    if enabled:
        document["reflector"]["probe"] = {"base_url": "http://127.0.0.1:8099/"}
        path.write_text(json.dumps(document))
    manifest = manifest_v2.load_manifest_v2(path)
    assert manifest.workflow is not None
    assert manifest.workflow.reflector is not None
    assert manifest.workflow.reflector.probe == (
        manifest_v2.ProbeSection(base_url="http://127.0.0.1:8099") if enabled else None
    )


def test_public_v4_manifest_refuses_target_origin(tmp_path) -> None:
    scaffold(tmp_path / "demo")
    path = tmp_path / "demo" / "manifest.json"
    document = json.loads(path.read_text())
    document["reflector"]["probe"] = {"base_url": document["target"]["base_url"]}
    path.write_text(json.dumps(document))
    with pytest.raises(manifest_v2.ManifestError) as info:
        manifest_v2.load_manifest_v2(path)
    assert (info.value.code, info.value.detail) == ("bad_value", "reflector.probe.base_url")


@pytest.mark.parametrize("mode", ["send", "parse"])
def test_string_body_is_a_v16_only_declared_identity_field(mode):
    from evolution.tree.campaign_identity import probe_identity_payload

    value = {"base_url": "http://localhost:8099", "string_body": mode}
    with pytest.raises(manifest_v2.ManifestError) as error:
        _parse(value)
    assert error.value.code == "unknown_key"
    parsed = manifest_v2._parse_probe_config(
        value,
        "reflector.probe",
        target_base_url="http://localhost:8765",
        schema=manifest_v2.SCHEMA_V16,
    )
    assert parsed.string_body == mode
    assert probe_identity_payload(parsed)["string_body"] == mode
    assert "string_body" not in probe_identity_payload(_parse({"base_url": value["base_url"]}))


@pytest.mark.parametrize("mode", [None, True, "decode", 1, []])
def test_invalid_string_body_is_refused(mode):
    with pytest.raises(manifest_v2.ManifestError):
        manifest_v2._parse_probe_config(
            {"base_url": "http://localhost:8099", "string_body": mode},
            "reflector.probe",
            target_base_url="http://localhost:8765",
            schema=manifest_v2.SCHEMA_V16,
        )


def test_probe_identity_roundtrips_string_body_only_in_family15():
    from evolution.tree.campaign_identity import _probe_identity, probe_identity_payload
    from evolution.tree.campaign_schema import SchemaError, _validate_identity_connection_fields

    probe = manifest_v2.ProbeSection("http://localhost:8099", string_body="parse")
    payload = probe_identity_payload(probe)
    assert _probe_identity(payload) == payload
    identity = {"metric_contract": "x", "role_visibility": "y", "reflector": {"probe": payload}}
    _validate_identity_connection_fields(identity, 15)
    with pytest.raises(SchemaError, match="probe.string_body"):
        _validate_identity_connection_fields(identity, 14)
