"""The offline R3 smoke owns its three-field input, not a campaign file schema."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import evolution.tree as tree
from evolution.tree.conformance import CHECK_NAMES


def test_smoke_uses_the_public_conformance_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    builder = tree.conformance_manifest

    def capture(document, *, base):
        calls.append((document, base))
        manifest = builder(document, base=base)
        assert manifest.workflow is None
        assert manifest.path == base.resolve() / "manifest.json"
        return manifest

    monkeypatch.setattr(tree, "conformance_manifest", capture)
    script = Path(__file__).resolve().parents[1] / "scripts" / "r3_conformance_smoke.py"
    spec = importlib.util.spec_from_file_location("r3_conformance_smoke", script)
    assert spec and spec.loader
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    payload = smoke.run(tmp_path)

    assert len(calls) == 1
    document, base = calls[0]
    assert base == tmp_path / "allowed"
    assert set(document) == {"seed_root", "target", "artifacts"}
    assert json.loads((base / "manifest.json").read_text(encoding="utf-8")) == document
    assert payload["ok"] is True
    assert payload["leaked"] == []
    assert (
        tuple((check["name"], check["expected"], check["suite"]) for check in payload["checks"])
        == CHECK_NAMES
    )
    assert all(check["ok"] is True for check in payload["checks"])
    assert smoke.CANARY.decode("ascii") not in json.dumps(payload)
    assert sorted(entry.name for entry in base.iterdir()) == ["manifest.json", "seed"]
