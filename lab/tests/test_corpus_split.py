"""Spec 2026-10-09 §3.1: a /15 corpus may label rows `split: train|test`; core checks the labels, the campaign runs
on train (core's choice), and Lab's fixed evaluation measures the split its request names."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from scaffold_15 import scaffold_15

from evolution_lab.preparation import PreparationError, PreparationStore
from test_preparation_api import _request

BUDGET = dict(repeat_seeds=[1], evaluation_set_id="split-check", max_turns=2, timeout_seconds=30,
              metric_plan={"primary": "score"}, max_failed_turns=1, call_retries="off")


def _label(corpus, labels):
    """labels: per row (group_id, split or None), in file order."""
    rows = [json.loads(line) for line in corpus.read_text().splitlines() if line]
    for row, (group, split) in zip(rows, labels):
        row["group_id"] = group
        if split is not None:
            row["split"] = split
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_labelled_15_corpus_prepares_and_test_evaluation_freezes_only_test(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch, scaffold=scaffold_15)
    _label(tmp_path / "source" / "corpus.jsonl", [("g1", "train"), ("g2", "test")])
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)
    assert store.prepare_campaign(bundle.digest).kind == "campaign"
    test = store.prepare_evaluation(bundle.digest, selection={"all": True}, split="test", **BUDGET)
    train = store.prepare_evaluation(bundle.digest, selection={"all": True}, split="train", **BUDGET)
    default = store.prepare_evaluation(bundle.digest, selection={"all": True}, **BUDGET)
    assert test.evaluation.case_ids == ("q2",)
    assert train.evaluation.case_ids == default.evaluation.case_ids == ("q1",)
    assert test.digest != train.digest and train.digest == default.digest


def test_partial_labels_are_refused_with_core_code(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch, scaffold=scaffold_15)
    _label(tmp_path / "source" / "corpus.jsonl", [("g1", "train"), ("g2", None)])
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)
    with pytest.raises(PreparationError) as caught:
        store.prepare_campaign(bundle.digest)
    assert (caught.value.code, caught.value.stage) == ("artifacts_invalid", "prepare")
    assert caught.value.failure.message == "The saved preparation artifacts are invalid. partial_split_labels at line 2"


def test_group_crossing_splits_is_refused_with_core_code(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch, scaffold=scaffold_15)
    _label(tmp_path / "source" / "corpus.jsonl", [("g", "train"), ("g", "test")])
    store = PreparationStore(tmp_path / "state")
    with pytest.raises(PreparationError) as caught:
        store.prepare_campaign(store.create_bundle(**request).digest)
    assert caught.value.failure.message.endswith("group_crosses_splits at line 2")


def test_unlabelled_15_has_an_empty_test_split(tmp_path, monkeypatch):
    request = _request(tmp_path, monkeypatch, scaffold=scaffold_15)
    store = PreparationStore(tmp_path / "state")
    bundle = store.create_bundle(**request)
    assert store.prepare_evaluation(bundle.digest, selection={"all": True}, **BUDGET).evaluation.case_ids == ("q1", "q2")
    with pytest.raises(PreparationError) as caught:
        store.prepare_evaluation(bundle.digest, selection={"all": True}, split="test", **BUDGET)
    assert caught.value.failure.message.endswith("empty_split at line 0")
    with pytest.raises(PreparationError) as caught:
        store.prepare_evaluation(bundle.digest, selection={"all": True}, split="holdout", **BUDGET)
    assert caught.value.code == "invalid_request"


def test_evaluation_worker_reads_a_labelled_15_corpus(tmp_path, monkeypatch):
    from evolution.tree import load_manifest_v2
    from evolution_lab.owner import _evaluation_worker as worker
    sample = scaffold_15(tmp_path / "s")
    _label(tmp_path / "s" / "corpus.jsonl", [("g1", "train"), ("g2", "test")])
    prepared = SimpleNamespace(kind="evaluation", evaluation=SimpleNamespace(case_ids=("q2",)),
                               bundle=SimpleNamespace(bundle_digest="b" * 64, manifest=load_manifest_v2(sample.manifest),
                                                      source_kind="campaign", bundle_path=tmp_path / "s"))
    monkeypatch.setattr(worker, "open_prepared", lambda storage, digest: prepared)
    monkeypatch.setattr(worker, "bind_campaign_evaluation", lambda *args, **kwargs: ("environment", "scorer", None))
    config = {"state_directory": str(tmp_path / "state"), "prepared_digest": "p" * 64, "bundle_digest": "b" * 64,
              "run_directory": str(tmp_path / "run")}
    _, _, selected, _, _ = worker._bindings(config, {})
    assert [case.case_id for case in selected] == ["q2"]
