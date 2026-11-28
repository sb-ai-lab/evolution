"""`"lab:auto"` resolution in a run copy (ADR-0032 decisions 1-2)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from evolution_lab.preparation.evaluation import load_evaluation_input
from evolution_lab.preparation.models import (
    DistributionPin, EnvironmentDescriptor, PlatformIdentity, RuntimeFile,
)
from evolution_lab.preparation.relocation import _LAUNCH_KEYS
from evolution_lab.preparation.storage import canonical_json_bytes
from evolution_lab.quickstart import QuickstartError, Resolution, new_project, resolve_project


def _environment() -> EnvironmentDescriptor:
    return EnvironmentDescriptor(
        executable="/venv/bin/python", executable_sha256="1" * 64, python_version="3.12.1",
        distributions=(DistributionPin("evolution-lab", "0.0.3.dev73", "2" * 64),),
        runtime_files=(RuntimeFile("/venv/rt.txt", "4" * 64),),
        platform=PlatformIdentity("Linux", "x86_64"), proof_profile="wheel-members/1",
        limitations=(),
    )


def _resolve(source: Path, run: Path, **over):
    kwargs = dict(environment=_environment(), port=45123, lab_version="0.0.3.dev73", core_version="b17")
    kwargs.update(over)
    return resolve_project(source, run, **kwargs)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _edit(path: Path, fn) -> None:
    data = _json(path)
    fn(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _is_hex(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")


@pytest.fixture()
def project(tmp_path):
    return new_project(tmp_path / "p")


def test_resolves_every_evaluation_marker(project, tmp_path):
    run = tmp_path / "run"
    resolution = _resolve(project, run)
    assert isinstance(resolution, Resolution)
    for name in ("launch.json", "evaluation-input.json"):
        assert "lab:auto" not in (run / name).read_text(encoding="utf-8")
    for name in ("campaign-launch.json", "manifest.json"):
        assert (run / name).read_bytes() == (project / name).read_bytes()
    launch = _json(run / "launch.json")
    env = _environment()
    assert launch["environment_ref"] == env.digest
    assert launch["endpoint"]["base_url"] == "http://127.0.0.1:45123"
    assert launch["identity"]["runtime_ref_digest"] == env.digest
    # no standalone launch validator exists: apply the closed-key and identity checks preparation applies
    assert set(launch) == _LAUNCH_KEYS[launch["schema"]]
    assert all(_is_hex(launch["identity"][k]) for k in ("contract_digest", "settings_hash", "runtime_ref_digest"))
    assert launch["endpoint"]["base_url"].startswith("http://")
    evaluation = load_evaluation_input(run / "evaluation-input.json", "bundle")
    assert evaluation["target"]["base_url"] == launch["endpoint"]["base_url"]
    assert evaluation["target"]["contract_digest"] == launch["identity"]["contract_digest"]
    assert evaluation["target"]["settings_hash"] == launch["identity"]["settings_hash"]
    assert evaluation["scorer"]["argv"][0] == env.executable
    assert resolution.explicit == ()
    assert resolution.substituted["launch.json:environment_ref"] == env.digest
    assert set(resolution.substituted) >= {
        "launch.json:endpoint.base_url", "launch.json:identity.contract_digest",
        "launch.json:identity.settings_hash", "launch.json:identity.runtime_ref_digest",
        "evaluation-input.json:scorer.argv.0",
    }


def test_digests_follow_adr_rule(project, tmp_path):
    run = tmp_path / "run"
    _resolve(project, run)
    written = _json(project / "launch.json")
    launch = _json(run / "launch.json")
    assert launch["identity"]["contract_digest"] == hashlib.sha256(canonical_json_bytes(written)).hexdigest()
    settings = {"env": dict(sorted(written["env"].items())), "secret_names": sorted(written["secret_names"])}
    assert launch["identity"]["settings_hash"] == hashlib.sha256(canonical_json_bytes(settings)).hexdigest()


def test_contract_digest_ignores_code(project, tmp_path):
    first = _resolve(project, tmp_path / "r1")
    (project / "agent" / "my_agent.py").write_text("# changed\n", encoding="utf-8")
    second = _resolve(project, tmp_path / "r2", port=45999)
    key = "launch.json:identity.contract_digest"
    assert first.substituted[key] == second.substituted[key]
    _edit(project / "launch.json", lambda d: d["limits"].update(max_output_bytes=4096))
    third = _resolve(project, tmp_path / "r3")
    assert third.substituted[key] != first.substituted[key]


def test_explicit_value_wins_and_pairs(project, tmp_path):
    _edit(project / "launch.json", lambda d: d["endpoint"].update(base_url="http://127.0.0.1:7001"))
    run = tmp_path / "run"
    resolution = _resolve(project, run)
    assert _json(run / "launch.json")["endpoint"]["base_url"] == "http://127.0.0.1:7001"
    assert _json(run / "evaluation-input.json")["target"]["base_url"] == "http://127.0.0.1:7001"
    assert "launch.json:endpoint.base_url" in resolution.explicit
    assert "launch.json:endpoint.base_url" not in resolution.substituted


def test_explicit_in_evaluation_input_pairs_to_launch(project, tmp_path):
    h = "a" * 64
    _edit(project / "evaluation-input.json", lambda d: d["target"].update(settings_hash=h))
    run = tmp_path / "run"
    resolution = _resolve(project, run)
    assert _json(run / "launch.json")["identity"]["settings_hash"] == h
    assert "evaluation-input.json:target.settings_hash" in resolution.explicit


def test_conflicting_explicit_values(project, tmp_path):
    _edit(project / "launch.json", lambda d: d["identity"].update(settings_hash="a" * 64))
    _edit(project / "evaluation-input.json", lambda d: d["target"].update(settings_hash="b" * 64))
    with pytest.raises(QuickstartError) as info:
        _resolve(project, tmp_path / "run")
    assert info.value.code == "conflicting_values"
    assert info.value.exit_code == 2
    message = str(info.value)
    assert "launch.json:identity.settings_hash" in message
    assert "evaluation-input.json:target.settings_hash" in message
    assert info.value.field


def test_marker_outside_allowed_fields(project, tmp_path):
    _edit(project / "launch.json", lambda d: d["limits"].update(max_instances="lab:auto"))
    with pytest.raises(QuickstartError) as info:
        _resolve(project, tmp_path / "run")
    assert info.value.code == "marker_not_allowed"
    assert info.value.file == "launch.json"
    assert info.value.field == "limits.max_instances"
    assert info.value.exit_code == 2


def test_invalid_json_and_missing_file(project, tmp_path):
    (project / "launch.json").write_text("{", encoding="utf-8")
    with pytest.raises(QuickstartError) as info:
        _resolve(project, tmp_path / "r1")
    assert (info.value.code, info.value.file, info.value.exit_code) == ("project_invalid", "launch.json", 2)
    (project / "launch.json").unlink()
    with pytest.raises(QuickstartError) as info:
        _resolve(project, tmp_path / "r2")
    assert (info.value.code, info.value.file) == ("project_invalid", "launch.json")


def test_resolved_payload_has_no_secret(project, tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_SENTINEL_SECRET", "s3cr3t-sentinel-value")
    resolution = _resolve(project, tmp_path / "run")
    payload = resolution.payload()
    assert "s3cr3t-sentinel-value" not in json.dumps(payload)
    assert payload["schema"] == "lab-evaluate-resolved/1"
    assert payload["lab_version"] == "0.0.3.dev73" and payload["core_version"] == "b17"
    for blob in (tmp_path / "run").rglob("*"):
        if blob.is_file():
            assert b"s3cr3t-sentinel-value" not in blob.read_bytes()


def test_source_untouched_and_run_copy_excludes_lab_dir(project, tmp_path):
    (project / ".lab").mkdir()
    (project / ".lab" / "state.txt").write_text("x", encoding="utf-8")
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    run = tmp_path / "run"
    _resolve(project, run)
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
    assert not (run / ".lab").exists()
    assert (run / "agent" / "my_agent.py").read_bytes() == before[project / "agent" / "my_agent.py"]


def test_run_copy_skips_vcs_virtualenvs_and_caches(project, tmp_path):
    """The run copy uses Lab's shared file-selection rule (ADR-0017 p. 6) plus the top-level ``.lab``:
    ``.venv`` and caches are left out, ``venv`` without a dot is code and is copied."""
    for name in (".git/HEAD", ".venv/bin/python", "agent/venv/helper.py", "agent/__pycache__/my_agent.pyc",
                 "scorer/.pytest_cache/v", "agent/.mypy_cache/m.json", "agent/.ruff_cache/r", "agent/sub/.git",
                 "agent/stray.pyc", "agent/stray.pyo"):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text("x", encoding="utf-8")
    run = tmp_path / "run"
    _resolve(project, run)
    copied = {path.relative_to(run).as_posix() for path in run.rglob("*")}
    for name in (".git", ".venv", "agent/__pycache__", "scorer/.pytest_cache", "agent/.mypy_cache",
                 "agent/.ruff_cache", "agent/sub/.git", "agent/stray.pyc", "agent/stray.pyo"):
        assert name not in copied, name
    assert (run / "agent" / "venv" / "helper.py").read_text(encoding="utf-8") == "x"
    assert "agent/sub" in copied and "agent/my_agent.py" in copied


def test_paired_substitution_is_recorded_with_its_source(project, tmp_path):
    """An explicit launch.json endpoint.base_url fills the marker in evaluation-input.json target.base_url;
    resolved.json lists that replaced field, its value and where the value came from."""
    _edit(project / "launch.json", lambda d: d["endpoint"].update(base_url="http://127.0.0.1:7001"))
    payload = _resolve(project, tmp_path / "run").payload()
    name = "evaluation-input.json:target.base_url"
    assert payload["substituted"][name] == "http://127.0.0.1:7001"
    assert payload["sources"][name] == "paired:launch.json:endpoint.base_url"
    assert "launch.json:endpoint.base_url" in payload["explicit"]
    assert "launch.json:endpoint.base_url" not in payload["substituted"]
    assert set(payload["sources"]) == set(payload["substituted"])
    assert payload["sources"]["launch.json:environment_ref"] == "computed"
    assert payload["sources"]["launch.json:identity.settings_hash"] == "computed"


@pytest.mark.parametrize("absolute", [True, False])
def test_symlinked_project_files_are_never_written_through(project, tmp_path, absolute):
    """A symlinked launch.json or evaluation-input.json still resolves, and the run copy gets regular files:
    the files the links point at keep their bytes."""
    config = project / "config"
    config.mkdir()
    originals = {}
    for name in ("launch.json", "evaluation-input.json"):
        (project / name).rename(config / name)
        originals[name] = (config / name).read_bytes()
        (project / name).symlink_to(config / name if absolute else Path("config") / name)
    run = tmp_path / "run"
    resolution = _resolve(project, run)
    for name, data in originals.items():
        assert (config / name).read_bytes() == data, name
        assert (project / name).is_symlink(), name
        assert not (run / name).is_symlink() and (run / name).is_file(), name
        assert "lab:auto" not in (run / name).read_text(encoding="utf-8"), name
        assert (run / "config" / name).read_bytes() == data, name
    assert resolution.substituted["launch.json:endpoint.base_url"] == "http://127.0.0.1:45123"


def test_resolution_records_pair_python_and_wheels(project, tmp_path):
    wheels = [{"path": "/w/evolution_lab.whl", "sha256": "a" * 64}]
    payload = _resolve(project, tmp_path / "run", pair="dev73-b17", python="/venv/bin/python",
                       wheels=wheels).payload()
    assert (payload["pair"], payload["python"], payload["wheels"]) == ("dev73-b17", "/venv/bin/python", wheels)
    bare = _resolve(project, tmp_path / "run2").payload()
    assert (bare["pair"], bare["python"], bare["wheels"]) == (None, None, [])
