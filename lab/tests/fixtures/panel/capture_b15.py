"""Genuine offline b15 campaigns, using Core's HTTP reference target.

Run with the installed producing wheels. Temporary campaign work lives in tmpfs;
only selected neutral evidence is exported. No journal is manufactured or edited.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from contextlib import ExitStack
import datetime
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from scaffold_16 import scaffold_16
from dev62_default_outputs import read_outputs

INTERRUPTIONS = {
    "focus-open": {
        "hook": "_Campaign._append_stage_opened (after original; scope=focus)",
        "reason": "Preserve the open focus stage before dispatch or focus_decided",
    },
    "focus-decided-open": {
        "hook": "_Campaign.hook(after_focus_decided)",
        "reason": "Preserve rejected focus_decided before comparison_completed and attempt_closed",
    },
    "causal-open": {
        "hook": "_Campaign._append_stage_opened (after original; scope=causal)",
        "reason": "Preserve the open causal stage after passing focus_decided, before causal dispatch",
    },
}
SCENARIOS = ("insufficient-evidence", "focus-first", *INTERRUPTIONS)


class GatePowerCut(BaseException):
    """Core's crash-test technique: escape orchestration without closing the attempt."""


SCORER = '''import json, os, sys
from pathlib import Path
p = json.load(sys.stdin)
edited = "edited" in (Path(os.environ["EVO_CANDIDATE_ROOT"]) / "skills/SKILL.md").read_text()
score = 1.0 if edited or p["case_id"] == "c2" else 0.0
print(json.dumps({"metrics": {"score": score}, "critical_pass": True,
                  "verdicts": [{"criterion": "answered", "passed": score == 1.0}]}))
'''
EDITOR = '''import json, sys
from pathlib import Path
p = json.load(sys.stdin)
skill = Path(p["workspace"]) / "skills/SKILL.md"
skill.write_text("edited neutral skill\\n" + skill.read_text())
print(json.dumps({"status": "edited", "note": "neutral fixture edit"}))
'''


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


async def capture(work: Path, name: str, *, core_only=False):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.evolve import evolve
    from evolution.tree.family15_report import report_lines
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2
    from evolution.tree.reference_target_cli import bind_reference_target
    from evolution.tree.target.reference_target import _ReferenceSession
    from evolution.tree.target.reference_target_http import serve

    assert name in SCENARIOS
    sample = scaffold_16(work / "input", mode="point", repeats=1)
    inputs = sample.manifest.parent
    document = json.loads(sample.manifest.read_text())
    scorer, editor = SCORER, EDITOR
    if name in {"focus-first", "focus-open", "focus-decided-open"}:
        scorer = SCORER.replace('score = 1.0 if edited or p["case_id"] == "c2" else 0.0',
            'score = (1.0 if "r01-a02" in skill else 0.25 if edited else 0.0) if p["case_id"] == "c1" else 1.0').replace(
            'edited = "edited" in (Path(os.environ["EVO_CANDIDATE_ROOT"]) / "skills/SKILL.md").read_text()',
            'skill = (Path(os.environ["EVO_CANDIDATE_ROOT"]) / "skills/SKILL.md").read_text()\nedited = "edited" in skill')
        editor = EDITOR.replace('"edited neutral skill\\n"', '"edited " + p["attempt_id"] + "\\n"')
    for role, program, member in (("scorer", scorer, "score.py"), ("editor", editor, "edit.py")):
        (inputs / role / member).write_text(program)
        document[role]["argv"] = [sys.executable, member]
    document["scorer"]["metric_contract"] = {
        "version": "metric_contract/1", "kind": "pointwise", "target_fields": []}
    group = {"root_cause": "neutral", "suggested_fix": "neutral", "evidence_ids": ["obs:c1:1"],
             "affected_cases": ["c1"], "target_paths": ["skills/SKILL.md"],
             "counterevidence": "neutral", "regression_risks": "neutral", "why_this_layer": "neutral"}
    diagnosis = {"status": "reflected", "note": "neutral", "groups": [group]}
    (inputs / "reflector/reflect.py").write_text("print(" + repr(json.dumps(diagnosis)) + ")\n")
    document["reflector"]["argv"] = [sys.executable, "reflect.py"]
    rows = [{"case_id": case, "group_id": "neutral", "stratum": "neutral",
             "public": {"text": case}, "oracle": {"critical": True},
             "reflection_metadata": {"uses": []}} for case in ("c1", "c2")]
    (inputs / "corpus.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    campaign = document["campaign"]
    campaign.update(rounds=1, attempts_per_round=1, max_concurrency=1,
                    baseline_refresh_after_rounds=0, random_seed=7, attempt_feedback="sequential")
    campaign["gate"]["fresh_parent"] = True
    if name == "focus-first":
        campaign["attempts_per_round"] = 2
    if name == "focus-first" or name in INTERRUPTIONS:
        campaign["gate"].update(stages="focus_first", focus={"margin": 0.5})
    campaign["indeterminate"] = {owner: {"max_count": 100, "max_rate": "1"}
                                  for owner in ("target", "environment", "scorer", "rollout")}
    dump(sample.manifest, document)
    target = bind_reference_target(load_manifest_v2(sample.manifest), allowed_root=work)
    server = serve(target, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    document["target"]["base_url"] = f"http://127.0.0.1:{server.server_address[1]}"
    dump(sample.manifest, document)
    armed, failures = threading.Event(), []
    original_hook, original_turn = _Campaign.hook, _ReferenceSession.turn
    original_stage_opened = _Campaign._append_stage_opened

    def interrupt_hook(self, hook):
        original_hook(self, hook)
        if hook == "after_focus_decided":
            raise GatePowerCut()

    def interrupt_stage_opened(self, mid, plan):
        # Exact v2.0.0b15 Core crash-test technique: write via Core, then cut.
        original_stage_opened(self, mid, plan)
        if plan.scope == ("focus" if name == "focus-open" else "causal"):
            raise GatePowerCut()

    def arm(self, hook):
        if hook == "after_comparison_bound":
            armed.set()
        return original_hook(self, hook)

    def exhausted_parent(self, message):
        reply = original_turn(self, message)
        if armed.is_set() and "edited" not in self._skill_line and message.content == "c1":
            failures.append(message.content)
            return replace(reply, result=replace(reply.result, outcome="target_failed"))
        return reply

    run = work / "run"
    result, interrupted = None, False
    try:
        with ExitStack() as stack:
            if name == "insufficient-evidence":
                stack.enter_context(patch.object(_Campaign, "hook", arm))
                stack.enter_context(patch.object(_ReferenceSession, "turn", exhausted_parent))
            elif name == "focus-decided-open":
                stack.enter_context(patch.object(_Campaign, "hook", interrupt_hook))
            elif name in {"focus-open", "causal-open"}:
                stack.enter_context(patch.object(_Campaign, "_append_stage_opened", interrupt_stage_opened))
            try:
                result = await evolve(sample.manifest, run)
            except GatePowerCut:
                interrupted = True
        assert interrupted == (name in INTERRUPTIONS)
        if not interrupted:
            assert result.terminal == "finished" and result.result is not None, result
        projection = inspect_campaign(run, verify=True).to_payload()
        assert projection["verification"]["mismatches"] == [], projection["verification"]
        attempts = projection["independent"]["attempts"]
        if name == "insufficient-evidence":
            assert [(a["outcome"], a["reason"]) for a in attempts] == [
                ("insufficient_evidence", "incomplete_sample")], attempts
            assert len(failures) == 2, failures  # original try plus configured retry
        elif name == "focus-first":
            assert len(attempts) == 2, attempts
            assert attempts[0]["gate_comparison"]["outcome"] == "comparison_not_passed"
            assert attempts[1]["gate_comparison"]["outcome"] == "passed"
            assert attempts[0]["gate_comparison"]["scopes"][1]["status"] == "not_measured"
            assert attempts[1]["gate_comparison"]["scopes"][1]["status"] == "available"
            assert all(set(a["slots"]) == {"candidate", "parent"} for a in attempts)
        else:
            records = [json.loads(line) for line in (run / "campaign.jsonl").read_text().splitlines()]
            assert len(attempts) == 1 and attempts[0]["outcome"] is None, attempts
            assert not any(row["event"] == "attempt_closed" for row in records)
            decisions = [row["payload"]["outcome"] for row in records if row["event"] == "focus_decided"]
            expected = {"focus-open": [], "focus-decided-open": ["comparison_not_passed"],
                        "causal-open": ["passed"]}
            assert decisions == expected[name], decisions
            if name == "focus-decided-open":
                assert records[-1]["event"] == "focus_decided"
            else:
                assert records[-1]["event"] == "stage_opened"
                assert records[-1]["payload"]["scope"] == ("focus" if name == "focus-open" else "causal")
        dump(work / "core-projection.json", projection)
        dump(work / "result.json", {"terminal": "interrupted" if interrupted else result.terminal,
                                  "owner": None if interrupted else result.owner,
                                  "code": None if interrupted else result.code,
                                  "injected_failed_turns": len(failures),
                                  **({"interruption": INTERRUPTIONS[name]} if interrupted else {})})
        (work / "core-report.txt").write_text("\n".join(report_lines(projection["independent"])) + "\n")
        for member, value in ({} if core_only else read_outputs(run)).items():
            if member.endswith(".md"):
                (work / member).write_text(value)
            else:
                dump(work / member, value)
        return run
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        target.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-wheel", type=Path, required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--retained", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--core-only", action="store_true", help="Capture Core evidence before Lab admits the new form")
    parser.add_argument("--destination", type=Path, default=Path(__file__).parent / "b15")
    args = parser.parse_args()
    import evolution_lab
    import evolution.tree
    assert "site-packages" in evolution_lab.__file__ and "site-packages" in evolution.tree.__file__
    assert version("evolution-lab") in {"0.0.3.dev63", "0.0.3.dev64"}
    assert version("evo-skills") in {"2.0.0b14", "2.0.0b15"}
    args.retained.mkdir(parents=True, exist_ok=False)
    wheels = {name: {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
              for name, path in (("lab", args.lab_wheel), ("core", args.core_wheel))}
    helpers = [Path(__file__), REPO / "tests/scaffold_16.py", REPO / "tests/dev62_default_outputs.py"]
    generators = [{"path": str(path.relative_to(REPO)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in helpers]
    for path in helpers:
        shutil.copyfile(path, args.retained / path.name)
    # Retain the named tmpfs work path for fault diagnosis; never redirect scratch onto disk.
    work_root = Path(tempfile.mkdtemp(prefix="lab-b15-capture-"))
    dump(args.retained / "work.json", {"tmpfs_work": str(work_root)})
    scenarios = args.scenario or SCENARIOS
    for name in scenarios:
        work = work_root / name
        run = asyncio.run(capture(work, name, core_only=args.core_only))
        exported = args.destination / name
        exported.mkdir(parents=True, exist_ok=False)
        for pattern in ("campaign.jsonl", "artifacts.jsonl", "measurements/*/events.jsonl",
                        "scorer-traces/*.pack", "reflector-logs/*.jsonl",
                        "connection/*/events.jsonl", "artifacts/auxiliary/*/payload.json",
                        "artifacts/candidates/**/*", "artifacts/champions/**/*"):
            for source in run.glob(pattern):
                if not source.is_file() or source.is_symlink():
                    continue
                target = exported / "run" / source.relative_to(run)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        # The exported evidence must independently retain every audited attempt.
        from evolution.tree.campaign import inspect_campaign
        exported_projection = inspect_campaign(exported / "run", verify=True).to_payload()
        assert exported_projection == json.loads((work / "core-projection.json").read_text())
        assert exported_projection["verification"]["mismatches"] == []
        members = ("core-projection.json", "core-report.txt", "result.json")
        if not args.core_only:
            members += ("campaign.json", "owner-report.json", "report.md")
        for member in members:
            shutil.copyfile(work / member, exported / member)
        files = {str(path.relative_to(exported)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(exported.rglob("*")) if path.is_file()}
        dump(exported / "meta.json", {"schema": "lab-offline-core-capture/1", "scenario": name,
            "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "core_version": version("evo-skills"), "lab_version": version("evolution-lab"),
            "core_commit": args.core_commit, "preliminary": version("evo-skills") != "2.0.0b15",
            "producing_wheels": wheels, "generators": generators,
            "core_functions": ["evolution.tree.evolve.evolve", "evolution.tree.campaign.inspect_campaign"],
            "fault_injections": (["after_comparison_bound arms ReferenceSession.turn target_failed on parent c1"]
                                 if name == "insufficient-evidence" else
                                 [INTERRUPTIONS[name]["hook"] + " raises GatePowerCut(BaseException)"]
                                 if name in INTERRUPTIONS else []),
            **({"interruption": INTERRUPTIONS[name]} if name in INTERRUPTIONS else {}),
            "executor": "Core ReferenceTarget over loopback HTTP; neutral subprocess roles",
            "files_sha256": files})
        print(f"captured {name}: {exported}", flush=True)
    dump(args.retained / "receipt.json", {"completed": True, "scenarios": scenarios,
                                           "producing_wheels": wheels, "generators": generators})


if __name__ == "__main__":
    main()
