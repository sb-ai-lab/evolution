"""Workflow-less input pins captured from Part A's /1 loader before C1 edits.

Oracle: e16e3b5fe2685f187e65451a136a6f4e222e748a, loaded in a separate
process with its own verified PYTHONPATH. The historical input was manifest.json;
only its required schema label was added to these three-field documents. Expected
results below are literals, not a dependency on Git or a historical checkout.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

import pytest

import evolution.tree as tree
from evolution.tree.execution.config import ManifestError
from evolution.tree.manifest.manifest_v2 import load_manifest_v2

DOCUMENT = {
    "seed_root": "./seed",
    "target": {"base_url": "http://127.0.0.1:8765", "seed_mode": "exact"},
    "artifacts": {
        "excludes": ["build/**"],
        "dependency_paths": ["uv.lock"],
        "writable_paths": ["skills/**"],
    },
}

# Operations are applied in order; a one-element operation deletes the field.
# @BASE makes absolute spellings portable without normalizing relative ones.
CASES = [
    ("defaults", []),
    ("localhost", [("target.base_url", "http://localhost:9000/")]),
    (
        "ipv6-unpaired",
        [("target.base_url", "http://[::1]:9000/"), ("target.seed_mode", "unpaired")],
    ),
    ("none", [("target.seed_mode", "none")]),
    ("absolute-seed", [("seed_root", "@BASE/seed")]),
    ("parent-relative-seed", [("seed_root", "../bundle/seed")]),
    (
        "optional-fields",
        [
            ("target.contract_digest", "contract-1"),
            ("target.settings_hash", "settings_1"),
            ("target.max_response_bytes", 4096),
            ("target.min_input_bytes", 2048),
            ("target.timeouts", {"connect_seconds": 2, "read_seconds": 3.5}),
        ],
    ),
    (
        "null-optionals",
        [
            ("target.contract_digest", None),
            ("target.settings_hash", None),
            ("target.timeouts", None),
        ],
    ),
    ("partial-timeouts", [("target.timeouts", {"read_seconds": 1})]),
    (
        "empty-policy",
        [("artifacts", {"excludes": [], "dependency_paths": [], "writable_paths": []})],
    ),
    (
        "nested-policy",
        [
            (
                "artifacts",
                {
                    "excludes": ["build/**", "cache"],
                    "dependency_paths": ["locks/uv.lock"],
                    "writable_paths": ["skills/**", "prompts/*.md"],
                },
            )
        ],
    ),
    ("missing-all", [("seed_root",), ("target",), ("artifacts",)]),
    ("missing-seed", [("seed_root",)]),
    ("missing-target", [("target",)]),
    ("missing-artifacts", [("artifacts",)]),
    ("missing-target-base", [("target.base_url",)]),
    ("missing-seed-mode", [("target.seed_mode",)]),
    ("missing-excludes", [("artifacts.excludes",)]),
    ("missing-dependencies", [("artifacts.dependency_paths",)]),
    ("missing-writable", [("artifacts.writable_paths",)]),
    ("unknown-root", [("extra", 1)]),
    ("unknown-target", [("target.extra", 1)]),
    ("unknown-artifacts", [("artifacts.extra", 1)]),
    ("unknown-timeouts", [("target.timeouts", {"extra": 1})]),
    ("target-type", [("target", [])]),
    ("artifacts-type", [("artifacts", None)]),
    ("base-url-type", [("target.base_url", [])]),
    ("seed-mode-list", [("target.seed_mode", ["exact"])]),
    ("seed-mode-dict", [("target.seed_mode", {"kind": "exact"})]),
    ("seed-mode-value", [("target.seed_mode", "sometimes")]),
    ("contract-empty", [("target.contract_digest", "")]),
    ("contract-invalid", [("target.contract_digest", "has space")]),
    ("settings-type", [("target.settings_hash", 7)]),
    ("settings-invalid", [("target.settings_hash", "bad/value")]),
    ("response-bool", [("target.max_response_bytes", True)]),
    ("response-zero", [("target.max_response_bytes", 0)]),
    ("input-float", [("target.min_input_bytes", 1.5)]),
    ("input-negative", [("target.min_input_bytes", -1)]),
    ("timeouts-type", [("target.timeouts", "bad")]),
    ("connect-bool", [("target.timeouts", {"connect_seconds": True})]),
    ("connect-zero", [("target.timeouts", {"connect_seconds": 0})]),
    ("read-negative", [("target.timeouts", {"read_seconds": -1})]),
    ("read-string", [("target.timeouts", {"read_seconds": "bad"})]),
    ("remote", [("target.base_url", "http://example.com:80")]),
    ("https", [("target.base_url", "https://localhost:80")]),
    ("no-port", [("target.base_url", "http://localhost")]),
    ("port-zero", [("target.base_url", "http://localhost:0")]),
    ("port-overflow", [("target.base_url", "http://localhost:65536")]),
    ("port-text", [("target.base_url", "http://localhost:bad")]),
    ("url-path", [("target.base_url", "http://localhost:80/path")]),
    ("url-query", [("target.base_url", "http://localhost:80/?q=1")]),
    ("url-fragment", [("target.base_url", "http://localhost:80/#frag")]),
    ("url-credentials", [("target.base_url", "http://user:pass@localhost:80")]),
    ("seed-type", [("seed_root", 3)]),
    ("seed-missing", [("seed_root", "missing")]),
    ("seed-file", [("seed_root", "file")]),
    ("seed-symlink", [("seed_root", "seed-link")]),
    ("excludes-type", [("artifacts.excludes", "build/**")]),
    ("dependency-item", [("artifacts.dependency_paths", [3])]),
    ("writable-type", [("artifacts.writable_paths", None)]),
    ("duplicate-pattern", [("artifacts.excludes", ["build/**", "build/**"])]),
    ("escape-pattern", [("artifacts.writable_paths", ["../escape"])]),
    ("absolute-pattern", [("artifacts.excludes", ["/absolute"])]),
    ("empty-pattern", [("artifacts.writable_paths", [""])]),
    ("dependency-glob", [("artifacts.dependency_paths", ["*.lock"])]),
    ("dependency-excluded", [("artifacts.excludes", ["uv.lock"])]),
    ("dependency-writable", [("artifacts.writable_paths", ["**"])]),
    ("writable-excluded", [("artifacts.writable_paths", ["build/**"])]),
    ("top-missing-before-target", [("seed_root",), ("target.base_url",)]),
    ("target-missing-before-artifacts", [("target.seed_mode",), ("artifacts.excludes",)]),
    ("nested-missing-before-root-unknown", [("extra", 1), ("artifacts.excludes",)]),
    ("root-unknown-before-target", [("extra", 1), ("target.extra", 1)]),
    ("target-unknown-before-artifacts", [("target.extra", 1), ("artifacts.extra", 1)]),
    (
        "timeouts-unknown-before-artifacts",
        [("target.timeouts", {"extra": 1}), ("artifacts.extra", 1)],
    ),
    ("structure-before-wrong-section-type", [("target", None), ("artifacts.excludes",)]),
    ("structure-before-field", [("target.seed_mode", []), ("artifacts.extra", 1)]),
    ("base-type-before-seed-mode", [("target.base_url", []), ("target.seed_mode", [])]),
    (
        "seed-mode-before-loopback",
        [("target.base_url", "http://remote:80"), ("target.seed_mode", [])],
    ),
    (
        "timeouts-before-loopback",
        [("target.base_url", "http://remote:80"), ("target.timeouts", {"read_seconds": 0})],
    ),
    ("target-before-artifacts", [("target.seed_mode", []), ("artifacts.excludes", 3)]),
    ("artifacts-before-seed", [("artifacts.excludes", 3), ("seed_root", "missing")]),
    ("policy-before-seed-type", [("artifacts.writable_paths", ["uv.lock"]), ("seed_root", 3)]),
]


def document_for(operations, base):
    document = copy.deepcopy(DOCUMENT)
    for operation in operations:
        *parents, key = operation[0].split(".")
        section = document
        for parent in parents:
            section = section[parent]
        if len(operation) == 1:
            del section[key]
        else:
            value = copy.deepcopy(operation[1])
            section[key] = value.replace("@BASE", str(base)) if isinstance(value, str) else value
    return document


def prepare_base(root):
    base = root / "bundle"
    (base / "seed").mkdir(parents=True)
    (base / "file").write_text("not a directory", encoding="utf-8")
    (base / "seed-link").symlink_to(base / "seed", target_is_directory=True)
    return base


def result_payload(manifest, base):
    return {
        "path": str(manifest.path).replace(str(base), "@BASE"),
        "schema": manifest.schema,
        "seed_root": str(manifest.seed_root).replace(str(base), "@BASE"),
        "workflow": manifest.workflow,
        "target": asdict(manifest.target),
        "policy": manifest.policy.to_payload(),
        "policy_digest": manifest.policy_digest,
        "declared": {
            key: value.replace(str(base), "@BASE") if isinstance(value, str) else value
            for key, value in manifest.declared.items()
        },
        "linked_from": manifest.linked_from,
        "linked_baseline": manifest.linked_baseline,
    }


# Literal pre-B results; unchanged fields share the captured default snapshot.
DEFAULT_RESULT = {
    "path": "@BASE/manifest.json",
    "schema": "evolution-v2-manifest/1",
    "seed_root": "@BASE/seed",
    "workflow": None,
    "target": {
        "base_url": "http://127.0.0.1:8765",
        "seed_mode": "exact",
        "contract_digest": None,
        "settings_hash": None,
        "max_response_bytes": 67108864,
        "min_input_bytes": 1048576,
        "timeouts": {"connect_seconds": 5.0, "read_seconds": 120.0},
    },
    "policy": {
        "version": 1,
        "excludes": ["build/**"],
        "dependency_paths": ["uv.lock"],
        "writable_paths": ["skills/**"],
    },
    "policy_digest": "384604dcc63fb6815da879606f2a66f7d310a285cafcb79e6e68feb711985937",
    "declared": {"seed_root": "./seed"},
    "linked_from": None,
    "linked_baseline": "fresh",
}
EXPECTED = {
    "defaults": DEFAULT_RESULT,
    "localhost": {
        **DEFAULT_RESULT,
        "target": {**DEFAULT_RESULT["target"], "base_url": "http://localhost:9000"},
    },
    "ipv6-unpaired": {
        **DEFAULT_RESULT,
        "target": {
            **DEFAULT_RESULT["target"],
            "base_url": "http://[::1]:9000",
            "seed_mode": "unpaired",
        },
    },
    "none": {**DEFAULT_RESULT, "target": {**DEFAULT_RESULT["target"], "seed_mode": "none"}},
    "absolute-seed": {**DEFAULT_RESULT, "declared": {"seed_root": "@BASE/seed"}},
    "parent-relative-seed": {**DEFAULT_RESULT, "declared": {"seed_root": "../bundle/seed"}},
    "optional-fields": {
        **DEFAULT_RESULT,
        "target": {
            "base_url": "http://127.0.0.1:8765",
            "seed_mode": "exact",
            "contract_digest": "contract-1",
            "settings_hash": "settings_1",
            "max_response_bytes": 4096,
            "min_input_bytes": 2048,
            "timeouts": {"connect_seconds": 2.0, "read_seconds": 3.5},
        },
    },
    "null-optionals": DEFAULT_RESULT,
    "partial-timeouts": {
        **DEFAULT_RESULT,
        "target": {
            **DEFAULT_RESULT["target"],
            "timeouts": {"connect_seconds": 5.0, "read_seconds": 1.0},
        },
    },
    "empty-policy": {
        **DEFAULT_RESULT,
        "policy": {"version": 1, "excludes": [], "dependency_paths": [], "writable_paths": []},
        "policy_digest": "fc46dea02f56186c177f03e15d94886bfb457974aa68b8e15ae7766a253427bc",
    },
    "nested-policy": {
        **DEFAULT_RESULT,
        "policy": {
            "version": 1,
            "excludes": ["build/**", "cache"],
            "dependency_paths": ["locks/uv.lock"],
            "writable_paths": ["skills/**", "prompts/*.md"],
        },
        "policy_digest": "cf6306fbf5bd5733abfc86bfb92aa5d6b25b6fcc675a1c73a3719a4e43989517",
    },
    "missing-all": {"error": ["missing_key", "artifacts", None]},
    "missing-seed": {"error": ["missing_key", "seed_root", None]},
    "missing-target": {"error": ["missing_key", "target", None]},
    "missing-artifacts": {"error": ["missing_key", "artifacts", None]},
    "missing-target-base": {"error": ["missing_key", "target.base_url", None]},
    "missing-seed-mode": {"error": ["missing_key", "target.seed_mode", None]},
    "missing-excludes": {"error": ["missing_key", "artifacts.excludes", None]},
    "missing-dependencies": {"error": ["missing_key", "artifacts.dependency_paths", None]},
    "missing-writable": {"error": ["missing_key", "artifacts.writable_paths", None]},
    "unknown-root": {"error": ["unknown_key", "extra", None]},
    "unknown-target": {"error": ["unknown_key", "target.extra", None]},
    "unknown-artifacts": {"error": ["unknown_key", "artifacts.extra", None]},
    "unknown-timeouts": {"error": ["unknown_key", "target.timeouts.extra", None]},
    "target-type": {"error": ["bad_type", "target", None]},
    "artifacts-type": {"error": ["bad_type", "artifacts", None]},
    "base-url-type": {"error": ["bad_type", "target.base_url", None]},
    "seed-mode-list": {"error": ["bad_type", "target.seed_mode", None]},
    "seed-mode-dict": {"error": ["bad_type", "target.seed_mode", None]},
    "seed-mode-value": {"error": ["bad_type", "target.seed_mode", None]},
    "contract-empty": {"error": ["bad_type", "target.contract_digest", None]},
    "contract-invalid": {"error": ["bad_type", "target.contract_digest", None]},
    "settings-type": {"error": ["bad_type", "target.settings_hash", None]},
    "settings-invalid": {"error": ["bad_type", "target.settings_hash", None]},
    "response-bool": {"error": ["bad_type", "target.max_response_bytes", None]},
    "response-zero": {"error": ["bad_type", "target.max_response_bytes", None]},
    "input-float": {"error": ["bad_type", "target.min_input_bytes", None]},
    "input-negative": {"error": ["bad_type", "target.min_input_bytes", None]},
    "timeouts-type": {"error": ["bad_type", "target.timeouts", None]},
    "connect-bool": {"error": ["bad_type", "target.timeouts.connect_seconds", None]},
    "connect-zero": {"error": ["bad_type", "target.timeouts.connect_seconds", None]},
    "read-negative": {"error": ["bad_type", "target.timeouts.read_seconds", None]},
    "read-string": {"error": ["bad_type", "target.timeouts.read_seconds", None]},
    "remote": {"error": ["not_loopback", "target.base_url", None]},
    "https": {"error": ["not_loopback", "target.base_url", None]},
    "no-port": {"error": ["not_loopback", "target.base_url", None]},
    "port-zero": {"error": ["not_loopback", "target.base_url", None]},
    "port-overflow": {"error": ["not_loopback", "target.base_url", None]},
    "port-text": {"error": ["not_loopback", "target.base_url", None]},
    "url-path": {"error": ["not_loopback", "target.base_url", None]},
    "url-query": {"error": ["not_loopback", "target.base_url", None]},
    "url-fragment": {"error": ["not_loopback", "target.base_url", None]},
    "url-credentials": {"error": ["not_loopback", "target.base_url", None]},
    "seed-type": {"error": ["bad_type", "seed_root", None]},
    "seed-missing": {"error": ["bad_path", "seed_root", None]},
    "seed-file": {"error": ["bad_path", "seed_root", None]},
    "seed-symlink": {"error": ["bad_path", "seed_root", None]},
    "excludes-type": {"error": ["bad_type", "artifacts.excludes", None]},
    "dependency-item": {"error": ["bad_type", "artifacts.dependency_paths[0]", None]},
    "writable-type": {"error": ["bad_type", "artifacts.writable_paths", None]},
    "duplicate-pattern": {"error": ["bad_type", "artifacts", None]},
    "escape-pattern": {"error": ["bad_type", "artifacts", None]},
    "absolute-pattern": {"error": ["bad_type", "artifacts", None]},
    "empty-pattern": {"error": ["bad_type", "artifacts", None]},
    "dependency-glob": {"error": ["bad_type", "artifacts", None]},
    "dependency-excluded": {"error": ["bad_type", "artifacts", None]},
    "dependency-writable": {"error": ["bad_type", "artifacts", None]},
    "writable-excluded": {"error": ["bad_type", "artifacts", None]},
    "top-missing-before-target": {"error": ["missing_key", "seed_root", None]},
    "target-missing-before-artifacts": {"error": ["missing_key", "target.seed_mode", None]},
    "nested-missing-before-root-unknown": {"error": ["missing_key", "artifacts.excludes", None]},
    "root-unknown-before-target": {"error": ["unknown_key", "extra", None]},
    "target-unknown-before-artifacts": {"error": ["unknown_key", "target.extra", None]},
    "timeouts-unknown-before-artifacts": {"error": ["unknown_key", "target.timeouts.extra", None]},
    "structure-before-wrong-section-type": {"error": ["missing_key", "artifacts.excludes", None]},
    "structure-before-field": {"error": ["unknown_key", "artifacts.extra", None]},
    "base-type-before-seed-mode": {"error": ["bad_type", "target.base_url", None]},
    "seed-mode-before-loopback": {"error": ["bad_type", "target.seed_mode", None]},
    "timeouts-before-loopback": {"error": ["bad_type", "target.timeouts.read_seconds", None]},
    "target-before-artifacts": {"error": ["bad_type", "target.seed_mode", None]},
    "artifacts-before-seed": {"error": ["bad_type", "artifacts.excludes", None]},
    "policy-before-seed-type": {"error": ["bad_type", "artifacts", None]},
}


@pytest.mark.parametrize(("name", "operations"), CASES, ids=[case[0] for case in CASES])
def test_builder_matches_pre_b_loader(tmp_path, name, operations):
    base = prepare_base(tmp_path)
    document = document_for(operations, base)
    before = copy.deepcopy(document)
    try:
        manifest = tree.conformance_manifest(document, base=base)
    except ManifestError as exc:
        actual = {"error": [exc.code, exc.detail, exc.expected]}
    else:
        actual = result_payload(manifest, base)
    assert actual == EXPECTED[name]
    assert document == before


@pytest.mark.parametrize("key", ["schema", "workflow", "campaign", "linked_from", "extra"])
def test_builder_rejects_every_other_top_level_key(tmp_path, key):
    base = prepare_base(tmp_path)
    document = document_for([], base)
    document[key] = "evolution-v2-manifest/1"
    with pytest.raises(ManifestError) as caught:
        tree.conformance_manifest(document, base=base)
    assert (caught.value.code, caught.value.detail, caught.value.expected) == (
        "unknown_key",
        key,
        None,
    )


def test_builder_accepts_mapping_and_never_touches_its_synthetic_path(tmp_path, monkeypatch):
    base = prepare_base(tmp_path)
    source = document_for([], base)
    synthetic = base / "manifest.json"
    assert not synthetic.exists()
    first = tree.conformance_manifest(MappingProxyType(source), base=base)
    assert not synthetic.exists()
    synthetic.mkdir()  # Even a non-file at that name is not inspected.
    link = tmp_path / "launch"
    link.symlink_to(base, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    second = tree.conformance_manifest(MappingProxyType(source), base=Path("launch"))
    assert first == second
    assert first.path == base.resolve() / "manifest.json"
    assert isinstance(first.declared, MappingProxyType)
    source["seed_root"] = "elsewhere"
    assert first.declared == {"seed_root": "./seed"}
    assert synthetic.is_dir()


def test_builder_never_reads_or_stats_the_synthetic_manifest(tmp_path, monkeypatch):
    base = prepare_base(tmp_path)
    synthetic = base / "manifest.json"
    synthetic.symlink_to(base / "file")

    def guarded(original):
        def call(path, *args, **kwargs):
            assert path != synthetic, "the builder must not inspect its synthetic path"
            return original(path, *args, **kwargs)

        return call

    for name in ("stat", "lstat", "open"):
        monkeypatch.setattr(Path, name, guarded(getattr(Path, name)))
    manifest = tree.conformance_manifest(DOCUMENT, base=base)
    assert manifest.path == synthetic
    assert manifest.workflow is None


def test_builder_does_not_admit_a_file_loaded_v1(tmp_path):
    import json

    base = prepare_base(tmp_path)
    manifest = tree.conformance_manifest(DOCUMENT, base=base)
    manifest.path.write_text(json.dumps({"schema": manifest.schema, **DOCUMENT}), encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_manifest_v2(manifest.path)
    assert (caught.value.code, caught.value.detail, caught.value.expected) == (
        "manifest_schema",
        "schema",
        "expected evolution-v2-manifest/15 or /16",
    )


def test_builder_does_not_bypass_campaign_admission(tmp_path):
    from evolution.tree.evolve import admit_manifest
    from evolution.tree.execution.output_admission import EvolveRefused

    manifest = tree.conformance_manifest(DOCUMENT, base=prepare_base(tmp_path))
    with pytest.raises(EvolveRefused) as caught:
        admit_manifest(manifest)
    assert (caught.value.code, caught.value.path, caught.value.expected) == (
        "manifest_schema",
        None,
        "expected evolution-v2-manifest/15 or /16",
    )


def test_builder_is_not_a_trace_campaign_manifest(tmp_path, monkeypatch):
    from evolution.tree.manifest import manifest_v2
    from evolution.tree.run_trace import TraceRefused, _bind_campaign_manifest

    manifest = tree.conformance_manifest(DOCUMENT, base=prepare_base(tmp_path))
    monkeypatch.setattr(manifest_v2, "load_manifest_v2", lambda _: manifest)
    with pytest.raises(TraceRefused) as caught:
        _bind_campaign_manifest(manifest.path, None, {})
    assert str(caught.value) == "manifest_schema: schema; expected evolution-v2-manifest/15 or /16"


def test_builder_inventory_keeps_workflow_boundary(tmp_path):
    base = prepare_base(tmp_path)
    manifest = tree.conformance_manifest(DOCUMENT, base=base)
    inventory = tree.manifest_dependencies(manifest)
    assert inventory.schema == "evolution-v2-manifest/1"
    assert inventory.manifest_path == base / "manifest.json"
    assert inventory.boundary == ("runtime_loads_unknown", "workflow_not_parsed")
    assert inventory.environment == ()
    assert [
        (dep.field, dep.declared, dep.base, dep.resolved) for dep in inventory.dependencies
    ] == [
        ("seed_root", "./seed", "manifest_dir", base / "seed"),
        ("artifacts.dependency_paths[0]", "uv.lock", "seed_root", base / "seed" / "uv.lock"),
    ]
