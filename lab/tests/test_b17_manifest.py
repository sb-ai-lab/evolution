"""Core b17 /16 keys survive the Lab bundle boundary."""
import json

import pytest

from evolution_lab.preparation.models import PreparationError
from evolution_lab.preparation.storage import PreparationStorage
from scaffold_15 import scaffold_15
from scaffold_16 import scaffold_16
from test_preparation_bundle import fixture


@pytest.mark.parametrize("key,value", [("reflector_context", "only the reflector"), ("context_max_bytes", 4096)])
def test_campaign_b17_keys_pass_through_bundle(tmp_path, monkeypatch, key, value):
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_16)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["campaign"][key] = value
    path.write_text(json.dumps(raw))
    saved = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    result = json.loads((saved.path / saved.descriptor.entrypoint).read_text())
    assert result["campaign"][key] == value


@pytest.mark.parametrize("key,value", [("require_parameters", False), ("nearest_paths", 3)])
def test_chat_editor_b17_keys_pass_through_bundle(tmp_path, monkeypatch, key, value):
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_16)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["editor"] = {"kind": "chat", "model": "neutral", "base_url": "http://127.0.0.1:9000/v1",
                     "api_key_env": "TOKEN", "provider_only": ["neutral"], key: value,
                     "context_compact_chars": 128000, "context_message_max_chars": 12000,
                     "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                               "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8, "jitter": "full"}}}
    path.write_text(json.dumps(raw))
    saved = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    result = json.loads((saved.path / saved.descriptor.entrypoint).read_text())
    assert result["editor"][key] == value


def test_manifest_15_rejects_b17_campaign_key(tmp_path, monkeypatch):
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["campaign"]["reflector_context"] = "not supported"
    path.write_text(json.dumps(raw))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "manifest_invalid"
    assert "unknown_key" in caught.value.failure.message
