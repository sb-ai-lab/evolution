"""Five neutral /16 fixtures, written only by the exact pinned engine, offline.

The target protocol and command roles are reduced from Lab's neutral
``fixtures/suspension_evidence/capture.py``; gate inputs follow v2.0.0b10's
``test_tree_case_comparison.py`` and family15 delivery tests. No service,
network, provider, journal editing or native panel capture is involved.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution_lab.owner import core_raw

from scaffold_16 import scaffold_16

SCENARIOS = ("statistical", "noise-stored", "noise-fresh", "fallback-point", "no-option")


class NeutralTarget:
    def __init__(self):
        from evolution.tree.execution.interaction import AdapterCapabilities
        self.capabilities = AdapterCapabilities("exact", 2, True,
                                                frozenset({"artifact_root_injection"}), 4)
        self.live = {}
        self.boots = 0

    def semantic(self, dependency_digest):
        from evolution.tree.execution.interaction import SemanticIdentity
        return SemanticIdentity("contract-v1", dependency_digest, "settings-v1", "exact", "2", "runtime-v1")

    async def preflight(self, expected):
        from evolution.tree.artifacts.attestation import TargetPreflight
        return TargetPreflight(self.semantic(expected.dependency_digest), self.capabilities, "epoch-1")

    async def materialize(self, candidate, strategy, instance_id):
        assert strategy == "artifact_root_injection" and instance_id not in self.live
        assert len(self.live) < 4
        self.boots += 1
        instance = NeutralInstance(self, candidate, instance_id, f"boot-{self.boots}")
        self.live[instance_id] = instance
        return instance

    async def release(self, instance_id):
        self.live.pop(instance_id, None)


class NeutralInstance:
    description = None
    components = None

    def __init__(self, target, candidate, instance_id, boot):
        self.target, self.candidate, self.boot = target, candidate, boot
        self.instance_id, self.boot_id = instance_id, boot

    async def identity(self):
        from evolution.tree.execution.interaction import InstanceProvenance, TargetIdentity
        from evolution.tree.execution.wire_v2 import REQUIRED_ATTESTS
        return TargetIdentity(self.target.semantic(self.candidate.dependency_digest),
                              InstanceProvenance(self.boot, self.candidate.digest), frozenset(REQUIRED_ATTESTS))

    async def open(self, case_public, seed, request_id):
        return SimpleNamespace(session_id=f"session-{request_id}", requested_seed=seed, effective_seed=seed)

    async def send(self, session, message, request_id):
        from evolution.tree.execution.interaction import TurnResult
        return TurnResult(events=({"type": "message", "text": message.content},), usage=None, latency_ms=1, outcome="ok",
                          provenance={"used": ["skills/SKILL.md"]})

    async def close(self, session):
        pass


SCORER = '''import json, os, sys
from pathlib import Path
p = json.load(sys.stdin)
skill = Path(os.environ["EVO_CANDIDATE_ROOT"]) / "skills/SKILL.md"
edited = "edited" in skill.read_text()
score = 1.0 if edited or p["case_id"] == "c2" else 0.0
print(json.dumps({"metrics": {"score": score}, "critical_pass": True,
                  "verdicts": [{"criterion": "answered", "passed": score == 1.0}]}))
'''
EDITOR = '''import json, sys
from pathlib import Path
p = json.load(sys.stdin)
skill = Path(p["workspace"]) / "skills/SKILL.md"
skill.write_text(skill.read_text() + " edited\\n")
print(json.dumps({"status": "edited", "note": "neutral fixture edit"}))
'''


async def generate(root: Path) -> dict[str, Path]:
    """Exactly one Core-authored campaign for each approved option set."""
    from importlib.metadata import version
    from evolution.tree.evolve import evolve

    assert version("evo-skills") == core_raw.SUPPORTED_CORE
    runs = {}
    for name in SCENARIOS:
        work = root / name
        sample = scaffold_16(work, repeats=2)
        document = json.loads(sample.manifest.read_text())
        for role, program, member in (("scorer", SCORER, "score.py"), ("editor", EDITOR, "edit.py")):
            (work / role / member).write_text(program)
            document[role]["argv"] = [sys.executable, member]
        document["scorer"]["metric_contract"] = {
            "version": "metric_contract/1", "kind": "pointwise", "target_fields": []}
        group = {"root_cause": "r", "suggested_fix": "f", "evidence_ids": ["obs:c1:1"],
                 "affected_cases": ["c1"], "target_paths": ["skills/SKILL.md"],
                 "counterevidence": "c", "regression_risks": "r", "why_this_layer": "w"}
        diagnosis = {"status": "reflected", "note": "neutral", "groups": [group]}
        (work / "reflector/reflect.py").write_text("print(" + repr(json.dumps(diagnosis)) + ")\n")
        document["reflector"]["argv"] = [sys.executable, "reflect.py"]
        rows = [{"case_id": case, "group_id": "g1", "stratum": "s1",
                 "public": {"text": "hello", "case_id": case}, "oracle": {"critical": True},
                 "reflection_metadata": {"uses": []}} for case in ("c1", "c2")]
        (work / "corpus.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        campaign = document["campaign"]
        campaign.update(rounds=1, attempts_per_round=1, max_concurrency=1, baseline_refresh_after_rounds=0,
                        random_seed=7)
        gate = campaign["gate"]
        if name == "statistical":
            gate.pop("statistics")
            gate.update(mode="statistical", focus={"margin": 0, "confidence": 0.1},
                        causal={"tolerance": 0, "confidence": 0.1})
        elif name in ("noise-stored", "noise-fresh"):
            gate["causal"] = {"tolerance": "measured_noise", "confidence": 0.9}
            if name == "noise-fresh":
                gate.update(fresh_parent=True, bound_fallback="point")
            else:
                gate.pop("statistics")
                gate.update(mode="point", focus={"margin": 0}, causal={"tolerance": "measured_noise"})
        elif name == "fallback-point":
            gate.update(bound_fallback="point", focus={"margin": 0, "confidence": 0.99})
        sample.manifest.write_text(json.dumps(document, indent=2) + "\n")
        run = work / "run"
        target = NeutralTarget()
        result = await evolve(sample.manifest, run, adapter_factory=lambda *_a, **_k: target)
        assert result.terminal == "finished", (name, result.terminal, result.owner, result.code)
        assert not target.live
        runs[name] = run
    return runs


@pytest.fixture(scope="session")
def offline_b10_campaigns(tmp_path_factory):
    return asyncio.run(generate(tmp_path_factory.mktemp("b10-options")))


def _unplanned_reflector(*, invalid_calls: int) -> str:
    """Reflector from Core v2.0.0b11's test_tree_campaign_family15_role_failures.py."""
    group = {"root_cause": "r", "suggested_fix": "f", "evidence_ids": ["obs:c1:1"],
             "affected_cases": ["c1"], "target_paths": ["skills/SKILL.md"],
             "counterevidence": "c", "regression_risks": "r", "why_this_layer": "w"}
    bad = {"status": "reflected", "note": "n", "groups": [{**group, "evidence_ids": ["obs:nope:9"]}]}
    good = {"status": "reflected", "note": "n", "groups": [group]}
    return (
        "import pathlib\n"
        "calls = pathlib.Path(__file__).with_name('calls')\n"
        "n = int(calls.read_text()) if calls.exists() else 0\n"
        "calls.write_text(str(n + 1))\n"
        f"bad, good = {json.dumps(bad)!r}, {json.dumps(good)!r}\n"
        f"print(bad if n < {invalid_calls} else good)\n"
    )


async def generate_unplanned(root: Path) -> dict[str, Path]:
    """Two Core-authored /16 runs whose first reflection cannot cite real evidence."""
    from importlib.metadata import version
    from evolution.tree.evolve import evolve

    assert version("evo-skills") == core_raw.SUPPORTED_CORE
    runs = {}
    for name, rounds in (("unplanned-then-continue", 2), ("only-unplanned", 1)):
        work = root / name
        sample = scaffold_16(work, repeats=2)
        document = json.loads(sample.manifest.read_text())
        for role, program, member in (("scorer", SCORER, "score.py"), ("editor", EDITOR, "edit.py")):
            (work / role / member).write_text(program)
            document[role]["argv"] = [sys.executable, member]
        document["scorer"]["metric_contract"] = {
            "version": "metric_contract/1", "kind": "pointwise", "target_fields": []}
        (work / "reflector/reflect.py").write_text(_unplanned_reflector(invalid_calls=1))
        document["reflector"]["argv"] = [sys.executable, "reflect.py"]
        rows = [{"case_id": case, "group_id": "g1", "stratum": "s1",
                 "public": {"text": "hello", "case_id": case}, "oracle": {"critical": True},
                 "reflection_metadata": {"uses": []}} for case in ("c1", "c2")]
        (work / "corpus.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        document["campaign"].update(rounds=rounds, attempts_per_round=1, max_concurrency=1,
                                    baseline_refresh_after_rounds=0, random_seed=7)
        sample.manifest.write_text(json.dumps(document, indent=2) + "\n")
        run = work / "run"
        target = NeutralTarget()
        result = await evolve(sample.manifest, run, adapter_factory=lambda *_a, **_k: target)
        assert result.terminal == "finished", (name, result.terminal, result.owner, result.code)
        assert not target.live
        runs[name] = run
    return runs


@pytest.fixture(scope="session")
def offline_unplanned_campaign(tmp_path_factory):
    return asyncio.run(generate_unplanned(tmp_path_factory.mktemp("unplanned")))
