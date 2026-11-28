"""Exact wheel pins must detect same-version rebuilds and changed installations."""
from importlib import metadata
from pathlib import Path
from zipfile import ZipFile

import pytest


def wheel_and_install(tmp_path, monkeypatch):
    wheel = tmp_path / "sample_agent-1.0-py3-none-any.whl"
    files = {"sample_agent/__init__.py": b"VALUE = 1\n",
             "sample_agent-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: sample-agent\nVersion: 1.0\n",
             "sample_agent-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
             "sample_agent-1.0.dist-info/RECORD": b""}
    site = tmp_path / "site"
    with ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
            path = site / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    dist = metadata.Distribution.at(site / "sample_agent-1.0.dist-info")
    monkeypatch.setattr(metadata, "distribution", lambda name: dist)
    return wheel, site


def test_exact_installed_wheel_verifies_and_reports_public_pin(tmp_path, monkeypatch):
    from evolution_lab.provenance import WheelPin
    wheel, site = wheel_and_install(tmp_path, monkeypatch)
    pin = WheelPin.capture(wheel)
    pin.verify()
    assert pin.name == "sample-agent" and pin.version == "1.0" and len(pin.sha256) == 64
    assert pin.public()["wheel"] == str(wheel)


def test_same_version_with_changed_installed_code_is_refused(tmp_path, monkeypatch):
    from evolution_lab.provenance import InputChanged, WheelPin
    wheel, site = wheel_and_install(tmp_path, monkeypatch)
    pin = WheelPin.capture(wheel)
    (site / "sample_agent/__init__.py").write_text("VALUE = 2\n")
    with pytest.raises(InputChanged): pin.verify()


def test_changed_wheel_bytes_are_refused(tmp_path, monkeypatch):
    from evolution_lab.provenance import InputChanged, WheelPin
    wheel, site = wheel_and_install(tmp_path, monkeypatch)
    pin = WheelPin.capture(wheel)
    with wheel.open("ab") as stream: stream.write(b"changed")
    with pytest.raises(InputChanged): pin.verify()


def test_file_pin_detects_mutation_without_reading_contents_into_metadata(tmp_path):
    from evolution_lab.provenance import FilePin, InputChanged
    path = tmp_path / "world.json"; path.write_text('"PRIVATE-CANARY"')
    pin = FilePin.capture(path)
    assert "PRIVATE-CANARY" not in str(pin.public())
    path.write_text('"changed"')
    with pytest.raises(InputChanged): pin.verify()


def test_isolated_pin_check_rejects_parent_only_installation(tmp_path, monkeypatch):
    import evolution_lab.provenance as provenance
    from evolution_lab.provenance import WheelPin, verify_isolated

    wheel, installed = wheel_and_install(tmp_path, monkeypatch)
    pin = WheelPin.capture(wheel)
    with pytest.raises(provenance.IsolatedEnvironmentError):
        verify_isolated((pin,))


def test_isolated_verification_accepts_an_explicit_copied_environment(monkeypatch):
    import subprocess
    import evolution_lab.provenance as provenance

    seen = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    env = {"PATH": "/isolated/bin", "TOKEN": "PRIVATE-CANARY"}
    provenance.verify_isolated((), env=env)
    env["TOKEN"] = "rotated"

    assert seen[0]["env"] == {"PATH": "/isolated/bin", "TOKEN": "PRIVATE-CANARY"}
