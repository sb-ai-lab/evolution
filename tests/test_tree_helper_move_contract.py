"""Copied role clients have transient seals and declared identity stays stable."""

import hashlib
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution.tree.campaign_identity import probe_identity_payload
from evolution.tree.roles.config import AgentCliEditorSection
from evolution.tree.roles.reflection_reader import ReaderIntegrityError
from evolution.tree.roles.reflection_role_helpers import install_helpers, install_probe_helper
from evolution.tree.roles.reflector_contracts import ProbeAccess
from evolution.tree.roles.seals import agent_reflector_identity, seal_program, seal_prompt
from evolution.tree.target.config import ProbeSection


def test_all_copied_clients_match_the_installed_package(tmp_path: Path) -> None:
    reader = SimpleNamespace(
        methods=frozenset(),
        client_descriptor=lambda: SimpleNamespace(url="http://127.0.0.1:1", capability="ephemeral"),
    )
    readers = tmp_path / "readers"
    install_helpers(readers, reader)
    probe = tmp_path / "probe"
    install_probe_helper(probe, ProbeAccess(None, 1, ("GET",), None), deadline_at=1)
    sources = files("evolution.tree")
    copies = {
        "reflection_read_client.py": readers / "evolution/tree/roles/reflection_read_client.py",
        "reflection_reader.py": readers / "evolution/tree/roles/reflection_reader.py",
        "probe_client.py": probe / "probe_client.py",
        "probe_wire.py": probe / "probe_wire.py",
    }
    assert {name: path.read_bytes() for name, path in copies.items()} == {
        name: sources.joinpath(
            "target" if name.startswith("probe_") else "roles", name
        ).read_bytes()
        for name in copies
    }


def test_helper_tampering_is_transient_and_does_not_rebind_identity(tmp_path: Path) -> None:
    program_path = tmp_path / "agent"
    program_path.write_text("agent program")
    program = seal_program(program_path)
    prompt = seal_prompt(None, packaged="declared prompt")
    section = AgentCliEditorSection(model="codex")
    probe = ProbeSection("http://127.0.0.1:8099", "/health", ("GET",), 1, 1.0, None)
    before = (agent_reflector_identity(section, prompt, program), probe_identity_payload(probe))
    helpers = install_probe_helper(
        tmp_path / "control", ProbeAccess(None, 1, ("GET",), None), deadline_at=1
    )
    helpers.verify()
    copied = tmp_path / "control/probe_client.py"
    copied.chmod(0o600)
    copied.write_bytes(copied.read_bytes() + b"\n# changed transient helper\n")
    assert hashlib.sha256(copied.read_bytes()).hexdigest() != dict(helpers.seals)[copied]
    with pytest.raises(ReaderIntegrityError, match="helper_changed"):
        helpers.verify()
    assert (
        agent_reflector_identity(section, prompt, program),
        probe_identity_payload(probe),
    ) == before
