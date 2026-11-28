"""Installed native campaigns preserve nested public data and bounded evidence."""
import asyncio
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.slow


async def test_nested_public_observations_and_closed_history_reach_next_round(tmp_path, monkeypatch):
    wheel = os.environ.get("EVOLUTION_CORE_WHEEL")
    if not wheel:
        pytest.skip("set EVOLUTION_CORE_WHEEL to the installed producing wheel")
    example = runpy.run_path(str(Path(__file__).parents[1] / "examples/campaign/offline.py"))
    original_scaffold = example["scaffold"]
    public = {"history": [{"speaker": "user", "text": "PUBLIC-HISTORY-7421"}],
              "nested": {"items": [{"label": "PUBLIC-NESTED-7421"}]}}
    canaries = ("PRIVATE-7421", "ORACLE-7421", "SCRIPTED-7421")

    def scaffold(root):
        sample = original_scaffold(root)
        corpus = root / "corpus.jsonl"
        rows = [json.loads(line) for line in corpus.read_text().splitlines()]
        for row in rows:
            row["public"] = public
            row["private"] = {"secret": canaries[0],
                              "script": [{"content": canaries[2], "fields": {}}]}
            row["oracle"]["secret"] = canaries[1]
        corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
        raw = json.loads(sample.manifest.read_text())
        raw["environment"] = {"kind": "scripted"}
        sample.manifest.write_text(json.dumps(raw))
        # A5 skips the editor when no failing cohort remains. Give the sample
        # edit partial credit while retaining a failed criterion, so round two
        # really requests an edit and receives the closed first-round history.
        scorer = root / "scorer/score.py"
        scorer.write_text(scorer.read_text().replace(
            'score = 1.0 if "IMPROVED"', 'score = 0.5 if "IMPROVED"',
        ))
        editor = root / "editor/edit.py"
        # Observe the actual subprocess request, then run the packaged sample
        # editor unchanged against its supplied immutable-parent workspace.
        original = editor.read_text()
        editor.write_text(original.replace(
            'request = json.load(sys.stdin)',
            'request = json.load(sys.stdin)\n'
            '    number = request["brief"]["history"]["rounds_closed"] + 1\n'
            '    Path(__file__).with_name(f"brief-{number}.json").write_text(json.dumps(request["brief"]))',
        ))
        return sample

    monkeypatch.setitem(example["main"].__globals__, "scaffold", scaffold)
    root = tmp_path / "campaign"
    await asyncio.wait_for(example["main"](SimpleNamespace(
        output=str(root), wheel=[wheel], max_concurrency=2, max_scorer_concurrency=1)), timeout=40)
    briefs = [json.loads((root / f"recipe/editor/brief-{n}.json").read_text()) for n in (1, 2)]
    for brief in briefs:
        assert brief["version"] == "edit_brief/5"
        assert all(canary not in json.dumps(brief) for canary in canaries)
        assert brief["limits"]
        assert brief["cases"]
        for case in brief["cases"]:
            assert case["public"] == public
            assert case["repeats"]
            for repeat in case["repeats"]:
                observation = repeat["observation"]
                assert observation["status"] == "present"
                assert observation["turns"]
                for turn in observation["turns"]:
                    assert turn["input"]["status"] == "withheld"
                    assert turn["input"]["content"] is None
                    assert any(event["type"] == "assistant_message" and event["content"]
                               for event in turn["events"])
    assert briefs[0]["history"]["rounds_closed"] == 0
    history = briefs[1]["history"]
    assert history["rounds_closed"] == 1
    attempt = history["rounds"][0]["attempts"][0]
    assert attempt["editor"] == "edited"
    # Family 14 (/15): the Attempt that passed the observe gate is core's "finalist" (the final decides acceptance).
    assert attempt["outcome"] == "finalist"
    assert attempt["candidate_digest"]
    assert attempt["changes"]["paths"] == ["skills/SKILL.md"]
    assert attempt["result"]["status"] == "present"
    from evolution_lab.report import read_campaign_report
    report = read_campaign_report(root / "run")
    assert report.data["core"]["complete"]
    assert len(report.data["core"]["rounds"]) == 2
    assert all(m["coverage"]["state"] == "complete" for m in report.data["measurements"])
