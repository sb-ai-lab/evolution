"""Core b18 /16 keys survive the Lab bundle boundary; Lab adds no check of its own."""
import json
from functools import partial

import pytest

from evolution_lab.preparation.models import PreparationError
from evolution_lab.preparation.storage import PreparationStorage
from scaffold_15 import scaffold_15
from scaffold_16 import scaffold_16
from test_preparation_bundle import fixture


def _edit(request, change):
    path = request["source"].manifest_path
    raw = json.loads(path.read_text())
    change(raw)
    path.write_text(json.dumps(raw))


def _saved(bundle, tmp_path, request):
    saved = bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    return json.loads((saved.path / saved.descriptor.entrypoint).read_text())


@pytest.mark.parametrize("value", [1, 3])
def test_min_claim_cases_passes_through_bundle(tmp_path, monkeypatch, value):
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_16)
    _edit(request, lambda raw: raw["reflector"]["selection"].update(min_claim_cases=value))
    assert _saved(bundle, tmp_path, request)["reflector"]["selection"]["min_claim_cases"] == value


def _fresh_point(raw):
    raw["campaign"]["gate"]["fresh_parent"] = True
    raw["campaign"]["evaluation"]["focus_repeats_per_question"] = 3


def test_focus_repeats_passes_through_bundle(tmp_path, monkeypatch):
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=partial(scaffold_16, mode="point"))
    _edit(request, _fresh_point)
    evaluation = _saved(bundle, tmp_path, request)["campaign"]["evaluation"]
    assert evaluation == {"repeats_per_question": 1, "focus_repeats_per_question": 3}


def test_focus_repeats_rule_is_core_s(tmp_path, monkeypatch):
    # Without a fresh Parent Core refuses the key before any run; Lab reports Core's refusal.
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=partial(scaffold_16, mode="point"))
    _edit(request, lambda raw: raw["campaign"]["evaluation"].update(focus_repeats_per_question=3))
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "manifest_invalid"


@pytest.mark.parametrize("change", [
    lambda raw: raw["reflector"]["selection"].update(min_claim_cases=2),
    lambda raw: raw["campaign"].setdefault("evaluation", {}).update(focus_repeats_per_question=3),
], ids=["min_claim_cases", "focus_repeats_per_question"])
def test_manifest_15_rejects_b18_keys(tmp_path, monkeypatch, change):
    bundle, _, request = fixture(tmp_path, monkeypatch, scaffold=scaffold_15)
    _edit(request, change)
    with pytest.raises(PreparationError) as caught:
        bundle.create_bundle(PreparationStorage(tmp_path / "state"), **request)
    assert caught.value.code == "manifest_invalid"
    assert "unknown_key" in caught.value.failure.message
