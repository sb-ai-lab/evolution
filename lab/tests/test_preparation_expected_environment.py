"""S7 W26: Bundle publication checks the service environment digest before staging."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
from test_owner_target import make_bundle
from test_preparation_evaluation import _evaluation_request


def _inputs(tmp_path):
    from evolution_lab.preparation import CampaignSource, EnvironmentInput, SourceRoot

    _, bundle, _ = make_bundle(tmp_path)
    source = tmp_path / "source"
    environment = EnvironmentInput(Path(sys.executable), (_installed_lab_wheel(), CORE_WHEEL), ())
    kwargs = dict(
        source=CampaignSource(source / "manifest.json", source / "launch.json"),
        environments=(environment,), env={"TARGET_MODE": "normal"},
        secret_names=["TARGET_TOKEN"], roots=(SourceRoot("project", source),),
    )
    return bundle, kwargs


def test_matching_expected_environment_publishes_the_same_bundle(tmp_path):
    from evolution_lab.preparation import PreparationStore

    bundle, kwargs = _inputs(tmp_path)
    again = PreparationStore(tmp_path / "other-state").create_bundle(
        **kwargs, expected_execution_environment=bundle.environment_digests[0])
    assert again.digest == bundle.digest


def test_mismatched_expected_environment_refuses_before_publication(tmp_path):
    from evolution_lab.preparation import PreparationError, PreparationStore

    _, kwargs = _inputs(tmp_path)
    state = tmp_path / "other-state"
    with pytest.raises(PreparationError) as caught:
        PreparationStore(state).create_bundle(**kwargs, expected_execution_environment="f" * 64)
    assert caught.value.code == "environment_invalid"
    assert list((state / "bundles").iterdir()) == []


@pytest.mark.parametrize("value", ["F" * 64, "f" * 63, 7])
def test_malformed_expected_environment_is_invalid_request(tmp_path, monkeypatch, value):
    from evolution_lab.preparation import PreparationError, PreparationStore
    from evolution_lab.preparation import bundle as bundle_module

    _, kwargs = _inputs(tmp_path)

    def forbidden(*_, **__):
        raise AssertionError("capture must not run")

    monkeypatch.setattr(bundle_module, "capture_environment", forbidden)
    with pytest.raises(PreparationError) as caught:
        PreparationStore(tmp_path / "other-state").create_bundle(**kwargs, expected_execution_environment=value)
    assert caught.value.code == "invalid_request"


def test_evaluation_mismatched_expected_environment_refuses_before_publication(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError, PreparationStore

    request = _evaluation_request(tmp_path, monkeypatch)
    state = tmp_path / "other-state"
    with pytest.raises(PreparationError) as caught:
        PreparationStore(state).create_bundle(**request, expected_execution_environment="f" * 64)
    assert caught.value.code == "environment_invalid"
    assert list((state / "bundles").iterdir()) == []
