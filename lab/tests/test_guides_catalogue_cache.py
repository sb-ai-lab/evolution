"""The shipped catalogues are validated once without sharing caller mutations."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evolution_lab import guides
from evolution_lab.guides import _scenarios


def _root(tmp_path: Path) -> Path:
    shipped = Path(guides.__file__).parent
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "requirements").symlink_to(shipped / "requirements", target_is_directory=True)
    (tmp_path / "scenarios").symlink_to(shipped / "scenarios", target_is_directory=True)
    return tmp_path


def test_each_catalogue_loads_and_validates_once_per_root(tmp_path, monkeypatch):
    base = _root(tmp_path / "first")
    monkeypatch.setattr(guides, "root", lambda: base)
    reads = validations = 0
    original_read = _scenarios.read_catalogue
    original_validate = guides.validate_catalogue

    def read(path):
        nonlocal reads
        reads += 1
        return original_read(path)

    def validate(document):
        nonlocal validations
        validations += 1
        return original_validate(document)

    monkeypatch.setattr(_scenarios, "read_catalogue", read)
    monkeypatch.setattr(guides, "validate_catalogue", validate)
    assert guides.scenarios().descriptor("connect")["id"] == "connect"
    assert guides.scenarios().descriptor("connect")["id"] == "connect"
    assert guides.requirements()["schema"] == "lab-requirements/1"
    assert guides.requirements()["schema"] == "lab-requirements/1"
    assert (reads, validations) == (1, 1)

    other = _root(tmp_path / "second")
    monkeypatch.setattr(guides, "root", lambda: other)
    assert guides.scenarios().base == other / "scenarios"
    assert guides.requirements()["schema"] == "lab-requirements/1"
    assert (reads, validations) == (2, 2)

    alias = tmp_path / "alias"
    alias.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(guides, "root", lambda: alias)
    assert guides.scenarios().base == alias / "scenarios"
    assert guides.requirements()["schema"] == "lab-requirements/1"
    assert (reads, validations) == (2, 2)


def test_simultaneous_first_calls_share_one_load(tmp_path, monkeypatch):
    base = _root(tmp_path / "concurrent")
    monkeypatch.setattr(guides, "root", lambda: base)
    original_read = _scenarios.read_catalogue
    original_validate = guides.validate_catalogue
    reads = validations = 0
    count_lock = threading.Lock()

    def read(path):
        nonlocal reads
        with count_lock:
            reads += 1
        time.sleep(0.05)
        return original_read(path)

    def validate(document):
        nonlocal validations
        with count_lock:
            validations += 1
        time.sleep(0.05)
        return original_validate(document)

    monkeypatch.setattr(_scenarios, "read_catalogue", read)
    monkeypatch.setattr(guides, "validate_catalogue", validate)
    gate = threading.Barrier(6)

    def use_both(_):
        gate.wait()
        assert guides.scenarios().descriptor("connect")["id"] == "connect"
        assert guides.requirements()["schema"] == "lab-requirements/1"

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(use_both, range(6)))
    assert (reads, validations) == (1, 1)


def test_returned_nested_values_cannot_poison_next_call(tmp_path, monkeypatch):
    base = _root(tmp_path / "mutable")
    monkeypatch.setattr(guides, "root", lambda: base)
    requirements = guides.requirements()
    original_feed = requirements["items"][0]["feeds"][0]
    requirements["items"][0]["feeds"][0] = "poison"
    requirements["paths"].append("poison")
    assert guides.requirements()["items"][0]["feeds"][0] == original_feed
    assert guides.requirements()["paths"] == ["campaign", "evaluation"]
    json.dumps(guides.requirements())

    catalogue = guides.scenarios()
    original_output = catalogue.descriptor("connect")["steps"][0]["outputs"][0].copy()
    catalogue.descriptor("connect")["steps"][0]["outputs"][0]["artifact"] = {"path": "poison"}
    catalogue.catalogue["scenarios"][0]["id"] = "poison"
    schema = catalogue.validators["connect"]["spec-answers/1"].schema
    original_type = schema["type"]
    schema["type"] = "poison"
    next_call = guides.scenarios()
    assert next_call.descriptor("connect")["steps"][0]["outputs"][0] == original_output
    assert next_call.catalogue["scenarios"][0]["id"] == "connect"
    assert next_call.validators["connect"]["spec-answers/1"].schema["type"] == original_type
    json.dumps(next_call.list_payload())
    assert next_call.skill_path("connect") == base / "scenarios" / "connect" / "SKILL.md"


def test_failed_load_is_retried(tmp_path, monkeypatch):
    base = _root(tmp_path / "retry")
    monkeypatch.setattr(guides, "root", lambda: base)
    original_read = _scenarios.read_catalogue
    original_validate = guides.validate_catalogue
    reads = validations = 0

    def read(path):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise ValueError("transient scenario failure")
        return original_read(path)

    def validate(document):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise ValueError("transient requirements failure")
        return original_validate(document)

    monkeypatch.setattr(_scenarios, "read_catalogue", read)
    monkeypatch.setattr(guides, "validate_catalogue", validate)
    with pytest.raises(ValueError, match="transient scenario failure"):
        guides.scenarios()
    with pytest.raises(ValueError, match="transient requirements failure"):
        guides.requirements()
    assert guides.scenarios().descriptor("connect")["id"] == "connect"
    assert guides.requirements()["schema"] == "lab-requirements/1"
    assert (reads, validations) == (2, 2)
