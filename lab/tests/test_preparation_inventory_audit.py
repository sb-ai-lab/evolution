"""S6 Task5: audit campaign relocation against the core manifest inventory (spec S3)."""
from __future__ import annotations

import json
import sys

import pytest
from evolution.tree import load_manifest_v2
from scaffold_15 import scaffold_15, scaffold_15_with_legacy_failures

from evolution_lab.preparation.inventory_audit import dotted_to_pointer
from evolution_lab.preparation.models import PreparationError
from evolution_lab.preparation.relocation import Relocator
from evolution_lab.preparation.storage import PreparationStorage as Storage

from test_preparation_bundle import fixture


@pytest.mark.parametrize("field, pointer", [
    ("seed_root", "/seed_root"),
    ("corpus.path", "/corpus/path"),
    ("grouper.argv[2]", "/grouper/argv/2"),
    ("scorer.dependency_paths[0]", "/scorer/dependency_paths/0"),
    ("artifacts.dependency_paths[1]", "/artifacts/dependency_paths/1"),
])
def test_dotted_fields_map_to_json_pointers(field, pointer):
    assert dotted_to_pointer(field) == pointer


def _grouping_campaign_request(tmp_path, monkeypatch, *, scaffold=scaffold_15):
    """The neutral campaign Bundle fixture with a `command` grouping section
    (S6 §3.4 digest invariant fixture), built from the shared factory rather
    than a hand-copied manifest.
    """
    bundle, root, request = fixture(tmp_path, monkeypatch, scaffold=scaffold)
    grouping = root / "grouping"
    (grouping / "nested").mkdir(parents=True)
    (grouping / "config").mkdir()
    script = grouping / "nested/group.py"
    argument = grouping / "config/request.json"
    dependency = root / "shared/grouping.txt"
    dependency.parent.mkdir()
    script.write_text("raise RuntimeError('must not execute')\n")
    argument.write_text('{"kind":"neutral"}\n')
    dependency.write_text("grouping dependency\n")
    docs = root / "docs"
    (docs / "nested").mkdir(parents=True)
    (docs / "guide.md").write_text("guide\n")
    (docs / "nested/details.md").write_text("details\n")

    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["reflector"]["environment_docs"] = "docs"
    raw["grouper"] = {
        "kind": "command",
        "argv": [sys.executable, "nested/group.py", "config/request.json"],
        "cwd": "grouping",
        "dependency_paths": ["shared/grouping.txt"],
        "allowed_env": [],
        "timeout_seconds": 17,
        "config": {
            # Opaque and never relocated (spec §3.2); kept path-free so the
            # digest test's expected value has no run-directory-derived bytes.
            "nested": {"path": "config/request.json"},
        },
    }
    path.write_text(json.dumps(raw))
    return bundle, root, request


def test_supported_campaign_bundle_digest_is_unchanged(tmp_path, monkeypatch):
    # Pin the interpreter path so the digest is portable across venvs/checkouts:
    # it ends up verbatim in argv[0], EnvironmentDescriptor.executable and the
    # launch command's executable (Relocator never relocates the executable
    # spelling itself, only pin-checks it against `pinned`), so leaving the
    # real sys.executable in would bake this machine's venv path into the hash.
    monkeypatch.setattr(sys, "executable", "/lab-test/bin/python")
    # This is a retained-byte reference, independent of new scaffold defaults.
    bundle, root, request = _grouping_campaign_request(
        tmp_path, monkeypatch, scaffold=scaffold_15_with_legacy_failures)
    storage = Storage(tmp_path / "state")
    stored = bundle.create_bundle(storage, **request)
    # /15 observe scaffold and top-level Grouper; interpreter spelling is pinned above.
    # Exact Core b13 reference_sample/edit.py adds edit_brief/7 acceptance. The
    # retained b12/b13 bundles differ only in that file and its descriptor entry.
    assert stored.digest == "7a9500b0a894dddb2a06bc479aedb7c67925a72f0b2c89014f79ee72249f0fc3"


def test_audit_omission_does_not_change_bundle_bytes(tmp_path, monkeypatch):
    """`audit_manifest_relocation` only refuses or passes through; it must
    never itself change relocated bytes. Build the same supported campaign
    Bundle twice from the same source, once with the audit call live and once
    patched to a no-op in `bundle.py`, and assert both the published digest
    and the normalized manifest bytes are identical.
    """
    bundle, root, request = _grouping_campaign_request(tmp_path, monkeypatch)

    storage_with_audit = Storage(tmp_path / "state-with-audit")
    stored_with_audit = bundle.create_bundle(storage_with_audit, **request)

    monkeypatch.setattr(bundle, "audit_manifest_relocation", lambda *args, **kwargs: None)
    storage_without_audit = Storage(tmp_path / "state-without-audit")
    stored_without_audit = bundle.create_bundle(storage_without_audit, **request)

    assert stored_with_audit.digest == stored_without_audit.digest
    manifest_with_audit = (stored_with_audit.path / stored_with_audit.descriptor.entrypoint).read_bytes()
    manifest_without_audit = (stored_without_audit.path / stored_without_audit.descriptor.entrypoint).read_bytes()
    assert manifest_with_audit == manifest_without_audit


def test_relocator_skipping_grouping_cwd_is_refused(tmp_path, monkeypatch):
    bundle, root, request = _grouping_campaign_request(tmp_path, monkeypatch)
    original = Relocator._command

    def patched(self, document, section, pointer_base, *, base, destination_base, pinned):
        original(self, document, section, pointer_base, base=base, destination_base=destination_base, pinned=pinned)
        if pointer_base == "/grouper":
            self.relocated.discard((document, "/grouper/cwd"))

    monkeypatch.setattr(Relocator, "_command", patched)
    storage = Storage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"


def test_skipped_path_operand_is_refused(tmp_path, monkeypatch):
    bundle, root, request = _grouping_campaign_request(tmp_path, monkeypatch)
    original = Relocator._command

    def patched(self, document, section, pointer_base, *, base, destination_base, pinned):
        original(self, document, section, pointer_base, base=base, destination_base=destination_base, pinned=pinned)
        if pointer_base == "/grouper":
            self.relocated.discard((document, "/grouper/argv/1"))

    monkeypatch.setattr(Relocator, "_command", patched)
    storage = Storage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"


def test_opaque_operand_is_covered(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    grouping = root / "grouping"
    grouping.mkdir()
    (grouping / "group.py").write_text("pass\n")
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["grouper"] = {
        "kind": "command",
        "argv": [sys.executable, "group.py", "fast"],
        "cwd": "grouping",
        "dependency_paths": [],
        "allowed_env": [],
        "timeout_seconds": 17,
        "config": {},
    }
    path.write_text(json.dumps(raw))
    storage = Storage(tmp_path / "state")

    stored = bundle.create_bundle(storage, **request)

    manifest = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    assert manifest.workflow.grouper.command.argv[2:] == ("fast",)


def test_bare_executable_is_refused(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["scorer"]["argv"][0] = "python3"
    path.write_text(json.dumps(raw))
    storage = Storage(tmp_path / "state")

    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(storage, **request)

    assert caught.value.code == "unsupported_input"
    assert caught.value.stage == "bundle"


def test_packaged_prompt_and_seed_root_artifacts_are_covered(tmp_path, monkeypatch):
    bundle, root, request = fixture(tmp_path, monkeypatch)
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    raw["editor"] = {
        "kind": "chat",
        "model": "gpt-x",
        "context_compact_chars": 128_000, "context_message_max_chars": 12_000,
        "base_url": "http://127.0.0.1:9/v1",
        "api_key_env": "EDITOR_API_KEY",
        "system_prompt": None,
        # /15 requires the author's own retry policy on a chat role; Lab never supplies it (PDR-0015).
        "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                  "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8, "jitter": "full"}},
    }
    path.write_text(json.dumps(raw))
    storage = Storage(tmp_path / "state")

    stored = bundle.create_bundle(storage, **request)

    manifest = load_manifest_v2(stored.path / stored.descriptor.entrypoint)
    assert manifest.seed_root == stored.path / "inputs/project/seed"
    saved = json.loads((stored.path / stored.descriptor.entrypoint).read_bytes())
    assert saved["editor"]["system_prompt"] is None
