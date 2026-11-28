"""V-S1-2: local environment evidence never imports a declared application."""
from __future__ import annotations

import json
import hashlib
import sys
from importlib.util import find_spec
from importlib import metadata
from pathlib import Path
from zipfile import ZipFile

import pytest


_VERIFIER = Path(sys.executable)


def _installed_wheel(tmp_path: Path, name: str = "pytest", replacement: bytes | None = None) -> Path:
    """Make a wheel-shaped archive from an installed stdlib-probed distribution."""
    distribution = metadata.distribution(name)
    wheel = tmp_path / f"{name}-{distribution.version}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for entry in distribution.files or ():
            path = Path(distribution.locate_file(entry))
            member = str(entry).replace("\\", "/")
            if ".." in Path(member).parts or not path.is_file():
                continue
            content = replacement if replacement is not None and member == "pytest/__init__.py" else path.read_bytes()
            archive.writestr(member, content)
    return wheel


def _hooked_target(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build a non-runnable venv-shaped fixture with hostile site startup hooks."""
    root = tmp_path / "target"
    executable = root / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(_VERIFIER)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    site = root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    pth_marker = tmp_path / "pth-ran"
    customize_marker = tmp_path / "sitecustomize-ran"
    (site / "hostile.pth").write_text(
        f"import pathlib; pathlib.Path({str(pth_marker)!r}).write_text('ran')\n"
    )
    (site / "sitecustomize.py").write_text(
        f"import pathlib; pathlib.Path({str(customize_marker)!r}).write_text('ran')\n"
    )
    package = site / "synthetic_site_package"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('must not import')\n")
    info = site / "synthetic_site_package-1.0.dist-info"
    info.mkdir()
    metadata_bytes = b"Metadata-Version: 2.1\nName: synthetic-site-package\nVersion: 1.0\n"
    wheel_bytes = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    (info / "METADATA").write_bytes(metadata_bytes)
    (info / "WHEEL").write_bytes(wheel_bytes)
    (info / "RECORD").write_bytes(b"")
    wheel = tmp_path / "synthetic_site_package-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("synthetic_site_package/__init__.py", (package / "__init__.py").read_bytes())
        archive.writestr("synthetic_site_package-1.0.dist-info/METADATA", metadata_bytes)
        archive.writestr("synthetic_site_package-1.0.dist-info/WHEEL", wheel_bytes)
        archive.writestr("synthetic_site_package-1.0.dist-info/RECORD", b"")
    return executable, wheel, pth_marker, customize_marker


def test_capture_uses_isolated_stdlib_probe_and_keeps_source_paths_private(tmp_path):
    """Removing the probe would miss an installed-wheel drift and leak no source path."""
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput

    wheel = _installed_wheel(tmp_path)
    application = tmp_path / "synthetic_application.py"
    application.write_text("raise RuntimeError('the probe must not import this')\n")

    captured = capture_environment(EnvironmentInput(
        executable=_VERIFIER, wheel_paths=(wheel,), runtime_files=(application,)
    ), execution=False)

    payload = captured.descriptor.to_payload()
    receipt = json.loads(captured.source_receipt_json)
    assert payload["distributions"] == [{
        "name": "pytest", "version": metadata.version("pytest"),
        "wheel_sha256": captured.descriptor.distributions[0].wheel_sha256,
    }]
    assert str(wheel) not in json.dumps(payload)
    assert receipt["archive_wheels"][0]["path"] == str(wheel.absolute())
    assert payload["runtime_files"] == [{
        "path": str(application.absolute()),
        "sha256": hashlib.sha256(application.read_bytes()).hexdigest(),
    }]
    assert receipt["runtime_files"] == [str(application.absolute())]
    assert "synthetic_application" not in sys.modules


def test_capture_refuses_a_missing_wheel_as_unavailable(tmp_path):
    """Treating a missing external prerequisite as a valid descriptor is a bug."""
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    with pytest.raises(PreparationError) as error:
        capture_environment(
            EnvironmentInput(executable=_VERIFIER, wheel_paths=(tmp_path / "missing.whl",)),
            execution=False,
        )

    assert error.value.code == "environment_unavailable"
    assert error.value.stage == "bundle"


def test_capture_refuses_a_wheel_when_its_installed_bytes_differ(tmp_path):
    """Accepting same-name metadata without member bytes would hide environment drift."""
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    wheel = _installed_wheel(tmp_path, replacement=b"changed\n")

    with pytest.raises(PreparationError) as error:
        capture_environment(EnvironmentInput(executable=_VERIFIER, wheel_paths=(wheel,)), execution=False)

    assert error.value.code == "environment_invalid"
    assert error.value.stage == "bundle"


def _target_member(executable: Path) -> Path:
    return (
        executable.parent.parent / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages/synthetic_site_package/__init__.py"
    )


def test_modified_member_detail_names_distribution_and_member(tmp_path):
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    executable, wheel, _, _ = _hooked_target(tmp_path)
    _target_member(executable).write_bytes(b"# changed installed bytes\n")

    with pytest.raises(PreparationError) as error:
        capture_environment(EnvironmentInput(executable, (wheel,)), execution=False)

    assert error.value.code == "environment_invalid"
    assert error.value.stage == "bundle"
    assert str(error.value) == (
        "A declared environment does not match its identity. "
        "synthetic-site-package 1.0: installed file synthetic_site_package/__init__.py "
        "differs from its wheel"
    )


def test_missing_member_detail(tmp_path):
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    executable, wheel, _, _ = _hooked_target(tmp_path)
    _target_member(executable).unlink()

    with pytest.raises(PreparationError) as error:
        capture_environment(EnvironmentInput(executable, (wheel,)), execution=False)

    assert error.value.code == "environment_invalid"
    assert str(error.value).endswith(
        "synthetic-site-package 1.0: installed file synthetic_site_package/__init__.py is missing"
    )
    assert "differs" not in str(error.value)


def test_detail_has_no_host_path(tmp_path):
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    executable, wheel, _, _ = _hooked_target(tmp_path)
    member = _target_member(executable)
    site_packages = member.parent.parent
    member.write_bytes(b"# changed\n")

    with pytest.raises(PreparationError) as error:
        capture_environment(EnvironmentInput(executable, (wheel,)), execution=False)

    message = str(error.value)
    assert "synthetic_site_package/__init__.py" in message
    assert str(site_packages) not in message
    assert str(tmp_path) not in message
    assert str(executable) not in message
    assert str(wheel) not in message


@pytest.mark.parametrize("installed", [False, True], ids=["not-installed", "wrong-version"])
def test_distribution_identity_detail(tmp_path, installed):
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    executable, wheel, _, _ = _hooked_target(tmp_path)
    info = _target_member(executable).parent.parent / "synthetic_site_package-1.0.dist-info"
    if installed:
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: synthetic-site-package\nVersion: 2.0\n"
        )
        detail = "synthetic-site-package: installed version 2.0 differs from the wheel's 1.0"
    else:
        (info / "METADATA").unlink()
        detail = "synthetic-site-package: not installed in the declared environment"

    with pytest.raises(PreparationError) as error:
        capture_environment(EnvironmentInput(executable, (wheel,)), execution=False)

    assert error.value.code == "environment_invalid"
    assert str(error.value).endswith(detail)


def test_execution_interpreter_mismatch_detail(tmp_path):
    from evolution_lab.preparation.environment import _execution_pair
    from evolution_lab.preparation.models import PreparationError

    with pytest.raises(PreparationError) as error:
        _execution_pair(tmp_path / "other-python", {})

    assert error.value.code == "environment_invalid"
    assert str(error.value).endswith("the declared executable is not this service's interpreter")


def test_incomplete_execution_pair_detail():
    from evolution_lab.preparation.environment import _validate_execution_pair
    from evolution_lab.preparation.models import PreparationError

    with pytest.raises(PreparationError) as error:
        _validate_execution_pair({"archive_wheels": [{"name": "evolution-lab", "version": "1.0"}]})

    assert error.value.code == "environment_invalid"
    assert str(error.value).endswith("the execution pair evolution-lab and evo-skills is incomplete")


@pytest.mark.parametrize("detail", [None, 42, {}, "", "line\nbreak", "x" * 401])
def test_malformed_probe_detail_keeps_typed_failure(monkeypatch, detail):
    from types import SimpleNamespace

    import evolution_lab.preparation.environment as environment_module
    from evolution_lab.preparation.models import PreparationError

    monkeypatch.setattr(environment_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"status": "environment_invalid", "detail": detail}),
    ))

    with pytest.raises(PreparationError) as error:
        environment_module._run_probe(_VERIFIER, {})

    assert error.value.code == "environment_invalid"
    assert str(error.value) == "A declared environment does not match its identity."


def test_probe_detail_is_printable_and_bounded():
    from evolution_lab._manifest_schema import DIAGNOSIS_LIMIT
    from evolution_lab.preparation._environment_probe import _Invalid

    error = _Invalid("member\n" * DIAGNOSIS_LIMIT)

    assert len(error.detail) == DIAGNOSIS_LIMIT
    assert error.detail.isprintable()
    assert "?" in error.detail


def test_invalid_version_metadata_does_not_leak_a_host_path(tmp_path):
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    executable, wheel, _, _ = _hooked_target(tmp_path)
    info = _target_member(executable).parent.parent / "synthetic_site_package-1.0.dist-info"
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: synthetic-site-package\nVersion: {tmp_path}/private\n"
    )

    with pytest.raises(PreparationError) as error:
        capture_environment(EnvironmentInput(executable, (wheel,)), execution=False)

    assert error.value.code == "environment_invalid"
    assert str(tmp_path) not in str(error.value)
    assert "installed version unknown differs from the wheel's 1.0" in str(error.value)


def test_execution_capture_refuses_an_active_lab_source_outside_its_pinned_wheel(
    tmp_path, monkeypatch
):
    """Certifying a wheel while the active Lab origin is foreign would be false evidence."""
    from importlib.util import spec_from_file_location

    import evolution_lab.preparation.environment as environment_module
    from evolution_lab.preparation.models import EnvironmentInput, PreparationError

    lab_wheel = _installed_wheel(tmp_path, "evolution-lab")
    core_wheel = _installed_wheel(tmp_path, "evo-skills")
    foreign_package = tmp_path / "foreign/evolution_lab/__init__.py"
    foreign_package.parent.mkdir(parents=True)
    foreign_package.write_text("# foreign active Lab origin\n")
    foreign_spec = spec_from_file_location("evolution_lab", foreign_package)
    assert foreign_spec is not None
    real_find_spec = environment_module.find_spec

    def find_spec(name):
        if name == "evolution_lab":
            return foreign_spec
        return real_find_spec(name)

    monkeypatch.setattr(environment_module, "find_spec", find_spec)

    with pytest.raises(PreparationError) as error:
        environment_module.capture_environment(
            EnvironmentInput(executable=_VERIFIER, wheel_paths=(lab_wheel, core_wheel)),
            execution=True,
        )

    assert error.value.code == "environment_invalid"
    assert error.value.stage == "bundle"


def test_execution_pair_allows_extra_declared_wheels_after_required_pair():
    """Rejecting a checked declared dependency merely because it is extra is a bug."""
    from evolution_lab.preparation.environment import _validate_execution_pair

    _validate_execution_pair({"archive_wheels": [
        {"name": "evolution-lab", "version": metadata.version("evolution-lab")},
        {"name": "evo-skills", "version": metadata.version("evo-skills")},
        {"name": "pytest", "version": metadata.version("pytest")},
    ]})


def test_capture_skips_target_site_startup_hooks_and_uses_declared_venv_metadata(tmp_path):
    """Running target .pth or sitecustomize code would violate the verifier boundary."""
    from evolution_lab.preparation.environment import capture_environment
    from evolution_lab.preparation.models import EnvironmentInput

    executable, wheel, pth_marker, customize_marker = _hooked_target(tmp_path)

    captured = capture_environment(
        EnvironmentInput(executable=executable, wheel_paths=(wheel,)), execution=False
    )

    assert captured.descriptor.distributions[0].name == "synthetic-site-package"
    assert not pth_marker.exists()
    assert not customize_marker.exists()


def test_probe_deduplicates_site_paths_that_alias_by_filesystem_identity(tmp_path):
    """Treating lib and lib64->lib as two metadata roots rejects a normal venv."""
    from evolution_lab.preparation._environment_probe import _deduplicate_site_paths

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    alias = tmp_path / "venv" / "lib64"
    alias.symlink_to(tmp_path / "venv" / "lib", target_is_directory=True)
    alias_site = alias / "python3.12" / "site-packages"

    assert _deduplicate_site_paths((site, alias_site)) == (site,)


def test_execution_origins_must_match_probe_verified_member_locations(tmp_path):
    """Caller metadata must not substitute a same-version shadow copy for probe evidence."""
    from evolution_lab.preparation.environment import _validate_execution_origins
    from evolution_lab.preparation.models import PreparationError

    receipt = {"archive_wheels": []}
    for distribution, module in (("evolution-lab", "evolution_lab"), ("evo-skills", "evolution")):
        spec = find_spec(module)
        assert spec is not None and spec.origin is not None
        receipt["archive_wheels"].append({
            "name": distribution,
            "module_paths": {module: str(Path(spec.origin).absolute())},
        })

    _validate_execution_origins(receipt)
    shadow = tmp_path / "shadow.py"
    shadow.write_bytes(Path(receipt["archive_wheels"][0]["module_paths"]["evolution_lab"]).read_bytes())
    receipt["archive_wheels"][0]["module_paths"]["evolution_lab"] = str(shadow)
    with pytest.raises(PreparationError) as error:
        _validate_execution_origins(receipt)

    assert error.value.code == "environment_invalid"


def test_execution_origins_accept_an_ancestor_alias_of_probe_verified_members(tmp_path):
    """A lib64->lib spelling of the same verified module must not look like a shadow."""
    from evolution_lab.preparation.environment import _validate_execution_origins

    receipt = {"archive_wheels": []}
    for distribution, module in (("evolution-lab", "evolution_lab"), ("evo-skills", "evolution")):
        spec = find_spec(module)
        assert spec is not None and spec.origin is not None
        origin = Path(spec.origin).absolute()
        alias = tmp_path / module
        alias.symlink_to(origin.parent, target_is_directory=True)
        receipt["archive_wheels"].append({
            "name": distribution,
            "module_paths": {module: str(alias / origin.name)},
        })

    _validate_execution_origins(receipt)
