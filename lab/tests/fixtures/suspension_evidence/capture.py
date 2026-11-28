"""Once-only neutral b6 writer evidence for final_rows, champion_rows and prefix.

Adapted from the three Core scenarios named in the sibling README/spec §6.
No Core test harness is imported. The installed engine owns every journal write;
only neutral target responses and named campaign interruption hooks are scripted.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from copy import deepcopy
import datetime
import hashlib
from importlib.metadata import distribution, version
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CORE_VERSION = "2.0.0b6"
CORE_SHA256 = "acdbc710902ea448fe1f22308250ba35768df5ee1cf4669c5b6751cb46514ec0"
SEED = 7
CHAMPION = "m-r00-champion"
PROBING = ContextVar("neutral_connection_probe", default=False)
SCENARIOS = ("final-fault", "champion-gap", "component-drift")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def payloads(run, event):
    path = run / "campaign.jsonl"
    return [row["payload"] for row in records(path) if row["event"] == event] if path.exists() else []


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class CutAfterStage(RuntimeError):
    """Approved simulated power cut after a real, durable seed-baseline stage."""


class BeforePublicReport(Exception):
    def __init__(self, snapshot, fault):
        self.snapshot, self.fault = snapshot, fault


class NeutralTarget:
    """Only the promotion-capable protocol and the three approved fault inputs.

    Reduced from Core campaign_fakes.FakeAdapter/FakeInstance, _CohortFaults,
    _GapTarget and _Drifts. No audit rig, synthetic journals or alternate scenarios.
    """
    def __init__(self, run, scenario):
        from evolution.tree.execution.interaction import AdapterCapabilities
        self.capabilities = AdapterCapabilities("exact", 2, True,
                                                frozenset({"artifact_root_injection"}), 4)
        self.run, self.scenario = run, scenario
        self.armed = False
        self.repaired = False
        self.boots = 0
        self.live = {}

    def semantic(self, dependency_digest):
        from evolution.tree.execution.interaction import SemanticIdentity
        return SemanticIdentity("contract-v1", dependency_digest, "settings-v1", "exact", "2", "runtime-v1")

    async def preflight(self, expected):
        from evolution.tree.artifacts.attestation import TargetPreflight
        return TargetPreflight(self.semantic(expected.dependency_digest), self.capabilities, "epoch-1")

    async def materialize(self, candidate, strategy, instance_id):
        require(strategy == "artifact_root_injection", "unexpected materialization strategy")
        require(instance_id not in self.live and len(self.live) < 4, "neutral target capacity exceeded")
        self.boots += 1
        instance = NeutralInstance(self, candidate, instance_id, f"boot-{self.boots}", PROBING.get())
        self.live[instance_id] = instance
        return instance

    async def release(self, instance_id):
        self.live.pop(instance_id, None)

    def fails(self, candidate, case_id, seed):
        if self.repaired or case_id != "c2" or seed != SEED + 1:
            return False
        if self.scenario == "final-fault":
            return self.armed and candidate.kind == "candidate"
        if self.scenario == "champion-gap" and candidate.kind == "champion":
            journal = self.run / "measurements" / CHAMPION / "events.jsonl"
            return journal.exists() and not any(
                row["measurement_id"] == CHAMPION for row in payloads(self.run, "baseline_complete"))
        return False


class NeutralInstance:
    def __init__(self, target, candidate, instance_id, boot_id, probing):
        self.target, self.candidate, self.probing = target, candidate, probing
        self.instance_id, self.boot_id = instance_id, boot_id
        self.description = None
        self.components = None
        if target.scenario == "component-drift" and not probing:
            rendered = "d" * 64 if target.armed and candidate.kind == "candidate" else candidate.digest
            self.components = {"skills/SKILL.md": {"sha256": rendered, "sources": ["skills/SKILL.md"]}}
        self.sessions = 0

    async def identity(self):
        from evolution.tree.execution.interaction import InstanceProvenance, TargetIdentity
        from evolution.tree.execution.wire_v2 import REQUIRED_ATTESTS
        return TargetIdentity(self.target.semantic(self.candidate.dependency_digest),
                              InstanceProvenance(self.boot_id, self.candidate.digest), frozenset(REQUIRED_ATTESTS))

    async def open(self, case_public, seed, request_id):
        self.sessions += 1
        require(self.sessions <= 2, "neutral session capacity exceeded")
        case_id = str(case_public.get("case_id", ""))
        return SimpleNamespace(session_id=f"session-{case_id}-{seed}", case_id=case_id,
                               requested_seed=seed, effective_seed=seed)

    async def send(self, session, message, request_id):
        from evolution.tree.execution.interaction import TurnResult
        from evolution.tree.execution.usage import Usage
        fail = not self.probing and self.target.fails(self.candidate, session.case_id, session.requested_seed)
        tag = (f" #r{session.requested_seed - SEED}"
               if self.target.scenario == "champion-gap" and not self.probing else "")
        return TurnResult(events=({"type": "message", "text": message.content + tag},),
                          usage=Usage(prompt=1, completion=1, cached=0, total=2), latency_ms=1,
                          outcome="target_failed" if fail else "ok",
                          provenance=None if self.probing else {"used": ["skills/SKILL.md"]})

    async def close(self, session):
        self.sessions -= 1


SCORER = r'''import json
import os
from pathlib import Path
import re
import sys
p = json.load(sys.stdin)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = Path(root) / "skills" / "SKILL.md" if root else None
view = json.dumps(p["view"], sort_keys=True)
edited = "edited" in view or bool(skill and skill.is_file() and "edited" in skill.read_text())
if Path("varying").exists():
    match = re.search(r"#r(\d+)", view)
    repeat = int(match.group(1)) if match else 1
    table = {("c1", True): [0.0, 0.05, 0.1, 0.0], ("c1", False): [0.1, 0.2, 0.2, 0.2],
             ("c2", True): [0.80, 0.90, 0.85, 0.90], ("c2", False): [0.82, 0.88, 0.85, 0.89]}
    score = table[(p["case_id"], edited)][(repeat - 1) % 4]
    passed = score >= 0.5
else:
    passed = edited or bool(p["oracle"].get("pass"))
    score = 1.0 if passed else 0.0
print(json.dumps({"metrics": {"score": score}, "critical_pass": True,
                  "verdicts": [{"criterion": "answered", "passed": passed}]}))
'''
EDITOR = r'''import json
from pathlib import Path
import sys
p = json.load(sys.stdin)
skill = Path(p["workspace"]) / "skills" / "SKILL.md"
skill.write_text(skill.read_text().rstrip("\n") + " edited\n")
print(json.dumps({"status": "edited", "note": "neutral fixture edit"}))
'''


def workspace(root, scenario):
    """Installed /15 sample, reduced to the approved two-case Core scenario."""
    from evolution.tree.reference_target_cli import SAMPLE_MANIFEST
    document = deepcopy(SAMPLE_MANIFEST)
    document["artifacts"] = {"excludes": [], "dependency_paths": [], "writable_paths": ["skills/**"]}
    (root / "seed" / "skills").mkdir(parents=True)
    (root / "seed" / "skills" / "SKILL.md").write_text("seed skill\n")
    for role, program, name in (("scorer", SCORER, "score.py"), ("editor", EDITOR, "edit.py")):
        (root / role).mkdir()
        (root / role / name).write_text(program)
        document[role]["argv"] = [sys.executable, name]
    document["scorer"]["metric_contract"] = {"version": "metric_contract/1", "kind": "pointwise", "target_fields": []}
    document["scorer"]["timeout_seconds"] = 60
    (root / "reflector").mkdir()
    group = {"root_cause": "r", "suggested_fix": "f", "evidence_ids": ["obs:c1:1"],
             "affected_cases": ["c1"], "target_paths": ["skills/SKILL.md"], "counterevidence": "c",
             "regression_risks": "r", "why_this_layer": "w"}
    diagnosis = {"status": "reflected", "note": "n", "groups": [group]}
    (root / "reflector" / "reflect.py").write_text("print(" + repr(json.dumps(diagnosis)) + ")\n")
    document["reflector"]["argv"] = [sys.executable, "reflect.py"]
    rows = [{"case_id": case, "group_id": "g1", "stratum": "s1",
             "public": {"text": "hello", "case_id": case},
             "oracle": {"critical": True, **({"pass": True} if case == "c2" else {})},
             "reflection_metadata": {"uses": []}} for case in ("c1", "c2")]
    (root / "corpus.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    statistical = scenario == "champion-gap"
    gate = {"mode": "statistical" if statistical else "observe", "stability": "unstable",
            "focus": {"margin": 0, **({"confidence": 0.9} if statistical else {})},
            "causal": {"confidence": 0.9, "tolerance": 0.05} if statistical else {"tolerance": "measured_noise"},
            "provenance": {"kind": "reported"}, "fresh_parent": True, "global": "off"}
    document["campaign"].update(
        rounds=1, evaluation={"repeats_per_question": 4 if statistical else 1},
        round_stop="all_attempts", baseline_refresh_after_rounds=0, gate=gate,
        random_seed=SEED, max_concurrency=1 if statistical else 2,
        sample_retries=0 if scenario == "final-fault" else 1,
        budget={"max_turns": 2, "timeout_seconds": 30, "max_failed_turns": 3},
        indeterminate={owner: {"max_count": 100, "max_rate": "1"}
                       for owner in ("target", "environment", "scorer", "rollout")})
    if statistical:
        (root / "scorer" / "varying").write_text("neutral negative candidate score table\n")
    path = root / "manifest.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


async def drive(manifest, target, *, cut=False, cancelled=lambda: False):
    """Drive the installed engine, returning before public report assembly as Core does."""
    from evolution.tree import campaign
    from evolution.tree.evolve import evolve
    original_probe, original_hook = campaign.run_probe, campaign._Campaign.hook
    original_stage = campaign._Campaign.run_stage

    async def probe(**kwargs):
        token = PROBING.set(True)
        try:
            return await original_probe(**kwargs)
        finally:
            PROBING.reset(token)

    def hook(engine, name):
        if name == "after_finalists_bound":
            target.armed = True
        return original_hook(engine, name)

    async def stage(engine, measurement_id, number):
        result = await original_stage(engine, measurement_id, number)
        if cut and measurement_id == CHAMPION:
            raise CutAfterStage("power cut after seed baseline stage, before callback")
        return result

    def result(engine):
        raise BeforePublicReport(engine.snapshot(), engine.fault)

    with patch.object(campaign, "run_probe", probe), patch.object(campaign._Campaign, "hook", hook), \
         patch.object(campaign._Campaign, "run_stage", stage), patch.object(campaign._Campaign, "result", result):
        try:
            await evolve(manifest, target.run, adapter_factory=lambda *_a, **_k: target, cancelled=cancelled)
        except BeforePublicReport as done:
            require(done.fault is None, f"unexpected engine fault: {done.fault}")
            return done.snapshot
    raise RuntimeError("engine did not reach the expected result seam")


def retain(run, output):
    """Byte copies only: campaign and measurement journals, no trees or traces."""
    kept = []
    for pattern in ("campaign.jsonl", "measurements/*/events.jsonl"):
        for source in sorted(run.glob(pattern)):
            relative = source.relative_to(run)
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            kept.append({"path": relative.as_posix(), "source": str(source), "sha256": digest(source),
                         "bytes": source.stat().st_size})
    return kept


async def scenario(root, name, receipt, save):
    root.mkdir()
    manifest = workspace(root, name)
    target = NeutralTarget(root / "run", name)
    receipt["manifest_sha256"] = digest(manifest)
    receipt["calls"] = []

    async def call(label, **options):
        receipt["calls"].append({"call": label, "completed": False})
        save()
        snapshot = await drive(manifest, target, **options)
        receipt["calls"][-1]["completed"] = True
        save()
        return snapshot

    if name == "champion-gap":
        journal = target.run / "measurements" / CHAMPION / "events.jsonl"
        def cancelled():
            return journal.exists() and b'"outcome":"target_failed"' in journal.read_bytes()
        try:
            await call("cut-seed-baseline", cut=True, cancelled=cancelled)
        except CutAfterStage:
            receipt["calls"][-1].update(completed=True, expected_cut=True)
            save()
        else:
            raise RuntimeError("seed-baseline cut did not fire")
        require(not payloads(target.run, "campaign_finished"), "cut unexpectedly wrote a terminal")
        await call("list-champion-gaps")
        listed = payloads(target.run, "campaign_suspended")
        require(len(listed) == 1 and listed[0]["evidence"]["evidence_kind"] == "champion_rows",
                "continuation did not list champion_rows")
        require(listed[0]["evidence"]["count"] == 3, "seed baseline did not list the approved three gaps")
        before = (target.run / "campaign.jsonl").read_bytes()
        receipt["listed_prefix"] = {"bytes": len(before), "sha256": hashlib.sha256(before).hexdigest()}
        target.repaired = True
        snapshot = await call("complete-champion-gaps")
        require(snapshot.finished and snapshot.finished["outcome"] == "completed", "champion continuation did not complete")
        require((target.run / "campaign.jsonl").read_bytes().startswith(before), "continuation changed writer prefix")
    else:
        await call(name)
        suspended = payloads(target.run, "campaign_suspended")
        require(len(suspended) == 1, "expected one real suspension")
        evidence = suspended[0]["evidence"]
        if name == "final-fault":
            require(evidence["evidence_kind"] == "final_rows" and evidence["count"] == 1,
                    "final fault did not produce the approved final_rows")
        else:
            require(suspended[0]["reason"] == "target_components_changed"
                    and evidence["evidence_kind"] == "stage_record" and bool(evidence.get("prefix")),
                    "component drift did not produce a stage_record prefix")
    receipt["actual_evidence"] = [{"reason": row["reason"], "kind": row["evidence"]["evidence_kind"],
                                    "prefix": "prefix" in row["evidence"]}
                                   for row in payloads(target.run, "campaign_suspended")]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel, output = args.core_wheel.resolve(), args.output.resolve()
    require(version("evo-skills") == CORE_VERSION and digest(wheel) == CORE_SHA256, "exact b6 producing pair required")
    require(not (output / "meta.json").exists(), "retained generation exists; do not overwrite or retry")
    run = REPO / ".superpowers" / "suspension-evidence" / uuid.uuid4().hex
    run.mkdir(parents=True, mode=0o700)
    producing = run / "producing-wheels"
    producing.mkdir()
    shutil.copyfile(wheel, producing / wheel.name)
    receipt = {"schema": "lab-suspension-fixture/1", "capture_id": run.name, "argv": sys.argv,
               "started_at": datetime.datetime.now(datetime.UTC).isoformat(), "completed": False,
               "core_version": version("evo-skills"), "producer": "installed evolution.tree.campaign writer",
               "core_wheel": {"source": str(wheel), "file": wheel.name, "sha256": digest(wheel)},
               "installed_direct_url": json.loads(distribution("evo-skills").read_text("direct_url.json") or "null"),
               "helper": {"path": str(HERE.relative_to(REPO) / "capture.py"), "sha256": digest(Path(__file__)),
                          "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()},
               "source_provenance": SOURCE_PROVENANCE, "scenarios": {}}
    output.mkdir(parents=True, exist_ok=True)
    def save():
        text = json.dumps(receipt, indent=2) + "\n"
        (run / "receipt.json").write_text(text)
        (output / "meta.json").write_text(text)
    save()
    try:
        for name in SCENARIOS:
            item = receipt["scenarios"][name] = {"completed": False}
            save()
            try:
                asyncio.run(scenario(run / name, name, item, save))
                item["completed"] = True
            finally:
                item["files"] = retain(run / name / "run", output / name)
                save()
        receipt["completed"] = True
    finally:
        save()
    print(f"suspension evidence: {run}")
    return 0


# Source checkout is read only; hashes below were collected during static adaptation.
SOURCE_PROVENANCE = {'repository_revision': '34c1699a34b13c3b399245673ae1d5a495cde36b', 'source_tree': 'user-owned dirty checkout; source-file hashes identify the inspected bytes', 'files': {'test_tree_campaign_family14_recovery.py': '6469230b9b62d2d98e5b10db4c01ec95929f5014e950d05327448e0b197c5ad4', 'test_tree_campaign_family14_champion_gap.py': 'fe97e2f02508e30863d72a5153da59b7a07e11ef75e5aa302ec8585f7d46d19e', 'test_tree_campaign_family14_run.py': '9a4b03af921ba71a016a13b65dd0f769056480d5c08e7a15c5ed271491cdf8cf', 'test_tree_campaign_family13_cohort_run.py': '521884cdb6391031d24a627bc348aa4958a01766edecf9f689234cb5f03ef157', 'test_tree_campaign_family13_gate.py': '5e78bfc27a933e8d1d8a69035626ec679a8a5a6ab0734ae04041eea6b61d2261', 'test_tree_campaign_family13_recovery_run.py': '6726e95bd4da4432bc75ebc075ba309c1ee6b06af0100f214d1ebe2ccb6c7cda', 'test_tree_campaign_family12.py': 'e0d952bae02f3ce51f3d363c8a75fd82d9db62fd73564291d874fd568d080462', 'campaign_fakes.py': '782089aa04e7ad4ed4879f64e0ebb50dda1be4a0e7696c38ba063b822b51e926', 'scaffold_v13.py': 'd68d854eae3a01c0db498bf3297833070509ee8d8d9e6772ff881334bcd66ac7'}}

if __name__ == "__main__":
    raise SystemExit(main())
