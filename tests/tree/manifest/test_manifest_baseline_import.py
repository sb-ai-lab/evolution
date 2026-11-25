"""The standalone first-round baseline source is a closed manifest declaration."""

from dataclasses import replace

import pytest

from evolution.tree.evaluation_manifest import load_evaluation_manifest
from evolution.tree.execution.config import ManifestError
from evolution.tree.manifest.manifest_inventory import manifest_dependencies
from tests.family15_fakes import v16_document
from tests.test_tree_train_test import evaluation_manifest
from tests.tree.manifest.test_manifest_v15 import load, v15_document


@pytest.mark.parametrize("document", [v15_document, v16_document])
def test_baseline_import_grammar(tmp_path, document):
    doc = document()
    doc["baseline_import"] = {"source": "../evaluation"}
    manifest = load(tmp_path, doc)
    assert manifest.baseline_import == "../evaluation"
    assert manifest.declared["baseline_import.source"] == "../evaluation"
    inventory = manifest_dependencies(manifest)
    source = next(item for item in inventory.dependencies if item.field == "baseline_import.source")
    assert source.declared == "../evaluation"
    assert source.resolved == manifest.path.parent / "../evaluation"


@pytest.mark.parametrize("document", [v15_document, v16_document])
@pytest.mark.parametrize(
    ("value", "code", "path"),
    [
        ({}, "missing_key", "baseline_import.source"),
        ({"source": "x", "extra": 1}, "unknown_key", "baseline_import.extra"),
        ([], "bad_type", "baseline_import"),
        ({"source": 1}, "bad_type", "baseline_import.source"),
        ({"source": ""}, "bad_value", "baseline_import.source"),
    ],
)
def test_baseline_import_bad_grammar(tmp_path, document, value, code, path):
    doc = document()
    doc["baseline_import"] = value
    with pytest.raises(ManifestError) as caught:
        load(tmp_path, doc)
    assert (caught.value.code, caught.value.detail) == (code, path)


@pytest.mark.parametrize("document", [v15_document, v16_document])
def test_baseline_sources_are_exclusive(tmp_path, document):
    doc = document()
    doc.update(baseline_import={"source": "x"}, linked_from={"run_dir": "y", "baseline": "inherit"})
    with pytest.raises(ManifestError) as caught:
        load(tmp_path, doc)
    assert (caught.value.code, caught.value.detail) == ("bad_value", "baseline_import")
    assert (
        caught.value.expected
        == "a campaign takes one first-round baseline source; a linked run carries its predecessor's rows with linked_from.baseline: inherit"
    )


@pytest.mark.parametrize("document", [v15_document, v16_document])
def test_baseline_import_absent_keeps_manifest_bytes(tmp_path, document):
    doc = document()
    before = load(tmp_path, doc)
    doc["baseline_import"] = {"source": "x"}
    after = load(tmp_path, doc)
    assert replace(after, baseline_import=None, declared=before.declared) == before
    assert "baseline_import.source" not in before.declared


def test_evaluation_refuses_baseline_import(tmp_path):
    import json

    path = evaluation_manifest(tmp_path)
    doc = json.loads(path.read_text())
    doc["baseline_import"] = {"source": "x"}
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as caught:
        load_evaluation_manifest(path)
    assert (caught.value.code, caught.value.detail) == ("unknown_key", "baseline_import")
