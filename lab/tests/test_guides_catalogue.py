"""W21 §7.1: the requirements catalogue is closed, digest-bound and resolvable."""
from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path

import pytest

from evolution_lab import guides

DIGEST = "7249ffee24aea86e92a4261e23385cdbe6a8c9a8891a8cd4c236981d49489413"
GUIDES = Path(guides.__file__).parent
ANCHOR = re.compile(r'<a id="([a-z0-9-]+)"></a>')
MENTION = re.compile(r"\{req:([a-z]+\.[a-z_]+)\}")


def _raw() -> dict:
    return json.loads((GUIDES / "requirements" / "requirements.json").read_text(encoding="utf-8"))


def _resign(document: dict) -> dict:
    document["digest"] = guides.catalogue_digest(document)
    return document


def test_catalogue_version_13_is_the_spec_catalogue():
    catalogue = guides.requirements()
    assert catalogue["schema"] == "lab-requirements/1"
    assert catalogue["catalogue_version"] == 13 and catalogue["digest"] == DIGEST
    assert catalogue["paths"] == ["campaign", "evaluation"]
    ids = [item["id"] for item in catalogue["items"]]
    assert ids == sorted(ids) and len(ids) == 34
    campaign = [item for item in catalogue["items"] if "campaign" in item["applies_to"]]
    evaluation = [item for item in catalogue["items"] if "evaluation" in item["applies_to"]]
    assert (len(campaign), sum(item["obligation"] == "required" for item in campaign)) == (34, 24)
    assert (len(evaluation), sum(item["obligation"] == "required" for item in evaluation)) == (20, 14)


@pytest.mark.parametrize("item_id, feeds", [
    ("campaign.stability_and_stop", ["manifest/15:/campaign/gate/stability", "manifest/15:/campaign/gate/focus/margin",
                                     "manifest/15:/campaign/round_stop",
                                     "manifest/15:/campaign/baseline_refresh_after_rounds"]),
    ("eval.metric_contract", ["manifest/15:/scorer/metric_contract"]),
    ("campaign.grouper", ["manifest/15:/grouper"]),
    ("campaign.role_visibility", ["manifest/15:/role_visibility"]),
])
def test_new_items_of_v9(item_id, feeds):
    item = next(i for i in guides.requirements()["items"] if i["id"] == item_id)
    assert item["feeds"] == feeds and item["obligation"] == "required" and item["applies_to"] == ["campaign"]
    assert item["answer_from"] == ["decision"]


def test_independent_acceptance_item_feeds_manifest_16():
    item = next(i for i in guides.requirements()["items"] if i["id"] == "campaign.independent_acceptance")
    assert item["feeds"] == ["manifest/16:/campaign/gate/mode", "manifest/16:/campaign/gate/execution_noise",
                             "manifest/16:/campaign/gate/verification", "manifest/16:/campaign/gate/fresh_parent",
                             "manifest/16:/campaign/evaluation/repeats_per_question"]
    assert item["obligation"] == "optional"     # /16 is opt-in; counts become 33 items, 24 required
    assert item["applies_to"] == ["campaign"] and item["answer_from"] == ["decision"]
    assert item["guide"] == "campaign-package/SKILL.md#manifest-16" and "manifest/16" in guides.DOCUMENTS


def test_no_feed_names_manifest_14():
    assert guides.DOCUMENTS >= {"manifest/15"} and "manifest/14" not in guides.DOCUMENTS
    assert not [f for i in guides.requirements()["items"] for f in i["feeds"] if f.startswith("manifest/14:")]


def test_gate_item_feeds_the_14_gate_and_repeats():
    gate = _item("campaign.gate")
    assert (gate["title"], gate["obligation"], gate["applies_to"], gate["answer_from"]) == (
        "Как судить кандидата", "required", ["campaign"], ["decision"])
    assert gate["feeds"] == ["manifest/15:/campaign/gate", "manifest/15:/campaign/evaluation/repeats_per_question"]
    assert all(word in gate["why"] for word in ("observe", "statistical", "R ≥ 2", "tolerance"))
    assert gate["guide"] == "campaign-package/SKILL.md#confidence-gate"
    feeds = [f for item in guides.requirements()["items"] for f in item["feeds"]]
    assert not any(f.startswith("manifest/11:") for f in feeds)
    assert guides.DOCUMENTS >= {"manifest/15"} and "manifest/11" not in guides.DOCUMENTS


def test_probe_items_are_optional_and_point_at_their_sections():
    entry, outcome = _item("target.entry_file"), _item("agent.outcome_vocabulary")
    assert entry["obligation"] == outcome["obligation"] == "optional"
    assert entry["guide"] == "target-connector/SKILL.md#target-entry"
    assert outcome["guide"] == "target-connector/SKILL.md#agent-outcome"


def test_failure_tolerance_is_a_shared_optional_human_decision():
    item = _item("run.failures")
    assert (item["group"], item["title"], item["obligation"]) == ("run", "Сбои при исполнении", "optional")
    assert item["applies_to"] == ["campaign", "evaluation"] and item["answer_from"] == ["decision"]
    assert item["feeds"] == [
        "manifest/15:/campaign/sample_retries", "manifest/15:/campaign/indeterminate",
        "manifest/15:/campaign/call_retries", "manifest/15:/campaign/budget/max_failed_turns",
        "manifest/15:/reflector/max_consecutive_failures",
        "evaluation-input/3:/defaults/sample_retries", "evaluation-input/3:/defaults/min_coverage",
        "evaluation-input/3:/defaults/max_consecutive_unmeasured", "evaluation-input/3:/defaults/call_retries",
        "evaluation-input/3:/defaults/max_failed_turns",
    ]
    assert all(variant in item["why"] for variant in ("retry", "no_retry", "fail_fast"))
    assert "умолчанию — `retry`" in item["why"]
    assert item["guide"] == "campaign-package/SKILL.md#run-failures"
    assert "evaluation-input/3" in guides.DOCUMENTS
    assert guides.validate_catalogue(_raw()) == guides.requirements()


def test_scale_defers_failure_tolerance_to_run_failures():
    item = _item("campaign.scale")
    assert item["feeds"] == ["manifest/15:/campaign/rounds", "manifest/15:/campaign/attempts_per_round",
                             "manifest/15:/campaign/parallel_attempts", "manifest/15:/campaign/budget"]
    assert "run.failures" in item["why"]
    assert all(key not in item["why"] for key in ("sample_retries", "call_retries"))


def test_role_resilience_keys_are_fed_by_the_models_item():
    item = next(row for row in guides.requirements()["items"] if row["id"] == "campaign.models")
    assert {"manifest/15:/editor", "manifest/15:/reflector"} <= set(item["feeds"])
    assert "max_consecutive_failures" in item["why"] and "retry" in item["why"]


def _item(item_id: str) -> dict:
    return next(row for row in guides.requirements()["items"] if row["id"] == item_id)


def test_world_items_feed_what_reaches_the_agent():
    assert _item("world.data")["feeds"] == ["seam:backends", "case:/public", "case:/private/script"]
    assert _item("world.reference_time")["feeds"] == ["seam:settings", "launch/2:/env", "case:/public"]
    for item_id in ("world.data", "world.reference_time"):
        assert "case:/private" not in _item(item_id)["feeds"]


def test_agent_items_follow_adr_0016_and_the_session_guide():
    config = _item("agent.config")
    assert config["feeds"] == ["seam:settings", "launch/2:/env"]
    assert "os.environ" in config["why"] and "context" in config["why"]
    session = _item("agent.session_state")
    assert session["feeds"] == ["seam:session", "launch/2:/limits"]
    assert "close_session" in session["why"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),
    lambda d: d.pop("paths"),
    lambda d: d.update(paths=["evaluation", "campaign"]),
    lambda d: d["items"][0].update(extra=1),
    lambda d: d["items"][0].update(obligation="maybe"),
    lambda d: d["items"][0].update(title=""),
    lambda d: d["items"][0].update(title="x" * 81),
    lambda d: d["items"][0].update(why="a\nb"),
    lambda d: d["items"][0].update(why=" padded"),
    lambda d: d["items"][0].update(applies_to=["campaign", "campaign"]),
    lambda d: d["items"][0].update(applies_to=["evaluation", "campaign"]),
    lambda d: d["items"][0].update(answer_from=[]),
    lambda d: d["items"][0].update(feeds=["manifest:workflow.corpus_path"]),
    lambda d: d["items"][0].update(feeds=["seam:world"]),
    lambda d: d["items"][0].update(feeds=["case:/foo~2bar"]),
    lambda d: d["items"][0].update(feeds=["case:/foo~"]),
    lambda d: d["items"][0].update(group="world"),
    lambda d: d["items"][0].update(id="Agent.x"),
    lambda d: d["items"].reverse(),
    lambda d: d.update(catalogue_version=True),
])
def test_closed_schema_rejects(mutate):
    document = _raw()
    mutate(document)
    with pytest.raises(ValueError):
        guides.validate_catalogue(_resign(document))


def test_digest_mismatch_rejects():
    document = _raw()
    document["items"][0]["title"] = "Другой заголовок"
    with pytest.raises(ValueError):
        guides.validate_catalogue(document)


def _anchors(relative: str) -> set[str]:
    return set(ANCHOR.findall((GUIDES / relative).read_text(encoding="utf-8")))


def test_every_guide_and_rule_points_to_an_existing_anchor():
    for item in guides.requirements()["items"]:
        targets = [item["guide"]] + [f[len("rule:"):] for f in item["feeds"] if f.startswith("rule:")]
        for target in targets:
            relative, anchor = target.split("#", 1)
            assert (GUIDES / relative).is_file(), target
            assert anchor in _anchors(relative), target


def _synthetic_documents(tmp_path: Path) -> dict[str, dict]:
    from scaffold_15 import scaffold_15
    from evolution.tree.execution.interaction import EvalCase
    from evolution_lab.preparation.evaluation import _BUDGET_KEYS, _DEFAULT_KEYS, _EVALUATION_KEYS, _INPUT_KEYS
    scaffold_15(tmp_path / "s")
    manifest = json.loads((tmp_path / "s" / "manifest.json").read_text())
    launch_keys = {"schema", "command", "environment_ref", "program_paths", "env", "secret_names",
                   "endpoint", "identity", "policy", "limits", "startup_timeout_seconds",
                   "stop_grace_seconds", "conformance"}  # preparation/relocation.py Relocator.launch
    launch_keys_2 = (launch_keys - {"command"}) | {"agent"}  # preparation/relocation.py _LAUNCH_KEYS["lab-target-launch/2"]
    bundle_keys = {"schema", "source", "environments", "env", "secret_names", "roots",
                   "path_bindings"}  # service/codecs.py bundle request /2
    manifest_16 = copy.deepcopy(manifest)
    manifest_16["schema"] = "evolution-v2-manifest/16"
    manifest_16["campaign"]["gate"].update(execution_noise={"kind": "negligible"}, verification="reuse")
    return {
        "manifest/15": manifest,
        "manifest/16": manifest_16,
        "evaluation-input/1": {key: {} for key in _INPUT_KEYS},
        "evaluation-input/3": {**{key: {} for key in _INPUT_KEYS}, "defaults": {
            key: {} for key in _DEFAULT_KEYS | _BUDGET_KEYS | _EVALUATION_KEYS | {"max_consecutive_unmeasured"}}},
        "launch/1": {key: {} for key in launch_keys},
        "launch/2": {**{key: {} for key in launch_keys_2 - {"agent"}},
                     "agent": {"module": {}, "attribute": {}, "import_root": {}}},
        "bundle-request/2": {key: {} for key in bundle_keys},
        # `private.script` is read by core's scripted environment (tree/rollout.py:743).
        "case": {**{field.name: {} for field in dataclasses.fields(EvalCase)}, "private": {"script": {}}},
    }


def _resolves(document: dict, pointer: str) -> bool:
    node = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            return False
        node = node[token]
    return True


def test_every_document_pointer_resolves(tmp_path):
    documents = _synthetic_documents(tmp_path)
    for item in guides.requirements()["items"]:
        for feed in item["feeds"]:
            kind, _, rest = feed.partition(":")
            if kind in documents:
                assert _resolves(documents[kind], rest), (item["id"], feed)


def test_every_mentioned_id_exists():
    known = {item["id"] for item in guides.requirements()["items"]}
    for path in GUIDES.rglob("*.md"):
        for mentioned in MENTION.findall(path.read_text(encoding="utf-8")):
            assert mentioned in known, (path.name, mentioned)


def test_guides_do_not_import_core_and_base_import_skips_guides():
    import os, subprocess, sys
    code = ("import sys, evolution_lab; assert 'evolution_lab.guides' not in sys.modules;"
            "import evolution_lab.guides as g; g.requirements();"
            "assert not any(m == 'evolution' or m.startswith('evolution.') for m in sys.modules)")
    env = {**os.environ, "PYTHONPATH": str(Path(guides.__file__).parents[2]) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
