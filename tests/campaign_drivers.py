"""Shared two-case campaign drivers and journal helpers."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.campaign import CampaignResult, inspect_campaign
from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import evolve
from evolution.tree.manifest.manifest_v2 import load_manifest_v2
from evolution.tree.reference_target_cli import bind_reference_target, scaffold
from evolution.tree.target.reference_target_http import serve
from tests.campaign_fakes import (
    Crash,
    FakeAdapter,
)
from tests.tree.manifest.test_manifest_v2 import (
    v11_document,
)
from tests.tree_evolve_support import AFFECTED_GATE, REFLECTOR_SCRIPT, write_workspace


def two_case_manifest(tmp_path, *, provenance=None, rows=None):
    """A native `/15` workspace with the shared affected-path settings."""

    from tests.tree.manifest.test_manifest_v2 import v11_document

    path = write_workspace(tmp_path, rows=rows)
    # These tests state their own Reflector; the default here declines.
    (tmp_path / "reflector" / "reflect.py").write_text(REFLECTOR_SCRIPT)
    document = json.loads(path.read_text())
    document["campaign"] = v11_document()["campaign"]
    document["campaign"]["gate"] = {
        **AFFECTED_GATE,
        **({"provenance": provenance} if provenance else {}),
    }
    from tests.campaign_fakes import family14_document

    path.write_text(json.dumps(family14_document(document)))
    return path


def _row(case_id, uses):
    return {
        "case_id": case_id,
        "group_id": "g1",
        "stratum": "s1",
        "public": {"text": "hello"},
        "oracle": {"critical": True},
        "reflection_metadata": {"uses": uses},
    }


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _measurement_events(run, measurement_id):
    return run / "measurements" / measurement_id / "events.jsonl"


TWO_CASE_SCORER = r"""
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
view = json.dumps(payload["view"], sort_keys=True)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = ""
if root:
    skill_path = pathlib.Path(root) / "skills" / "SKILL.md"
    if skill_path.is_file():
        skill = skill_path.read_text(encoding="utf-8")
passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))
json.dump(
    {
        "metrics": {"score": 1.0 if passed else 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": passed}],
    },
    sys.stdout,
)
"""


def _events(run, name):
    return [r for r in _records(run / "campaign.jsonl") if r["event"] == name]


async def two_case_run(
    tmp_path,
    *,
    components=None,
    used=None,
    provenance=None,
    adapter_class=FakeAdapter,
    as_family_13=None,
    adapter=None,
    campaign=None,
    labels=None,
    authorizations=(),
    cancelled=lambda: False,
    terminal="finished",
    scorer=TWO_CASE_SCORER,
    fault=None,
    linked_from=None,
    extra_cases=(),
    reflector=None,
    script=None,
    keep=None,
):
    """One finished run over `c1` (fails on the baseline: the exam) and `c2`.

    `labels` maps a case to its `reflection_metadata.uses` (default `[]`);
    `fault`, when given, is the terminal's expected `(owner, code)`;
    `linked_from`, when given, is `(predecessor run, seed root)`: the run
    links to it with `baseline: inherit` and seeds from that root;
    `extra_cases` maps further case ids to their oracles, after `c2`;
    `reflector`, when given, replaces the Reflector script's text;
    `script`, when given, runs the corpus under the `scripted` environment,
    every case's `private["script"]` one message of that content;
    `keep`, when given, is a list the `CampaignResult` is appended to;
    `as_family_13`, when given, is `family13_document`'s keyword arguments:
    these are gate/repeat overrides for this native `/15` fixture;
    `terminal=None` skips the terminal assertion.
    """

    # `public.case_id` is how a `FakeInstance` session learns its case.
    rows = [
        {
            **_row(case_id, (labels or {}).get(case_id, [])),
            "public": {"text": "hello", "case_id": case_id},
        }
        for case_id in ("c1", "c2", *dict(extra_cases))
    ]
    rows[1]["oracle"] = {"critical": True, "pass": True}
    for row in rows[2:]:
        row["oracle"] = dict(extra_cases)[row["case_id"]]
    if script is not None:
        for row in rows:
            row["private"] = {"script": [{"content": script}]}
    path = two_case_manifest(tmp_path, provenance=provenance, rows=rows)
    (tmp_path / "scorer" / "score.py").write_text(scorer)
    # A Reflector that claims the failing case, so every round opens Attempts.
    group = {
        "root_cause": "r",
        "suggested_fix": "f",
        "evidence_ids": ["obs:c1:1"],
        "affected_cases": ["c1"],
        "target_paths": ["skills/SKILL.md"],
        "counterevidence": "c",
        "regression_risks": "r",
        "why_this_layer": "w",
    }
    diagnosis = {"status": "reflected", "note": "n", "groups": [group]}
    (tmp_path / "reflector" / "reflect.py").write_text(
        reflector if reflector is not None else "print(" + repr(json.dumps(diagnosis)) + ")\n"
    )
    document = json.loads(path.read_text())
    # One round: each scenario opens exactly the Attempts it counts.
    document["campaign"].update({"rounds": 1, **(campaign or {})})
    if script is not None:
        document["environment"] = {"kind": "scripted"}
    if linked_from is not None:
        predecessor, seed_root = linked_from
        document["linked_from"] = {"run_dir": str(predecessor), "baseline": "inherit"}
        document["seed_root"] = str(seed_root)
    from tests.campaign_fakes import family14_admitted, family14_document

    document = family14_document(document, **(as_family_13 or {}))
    path.write_text(json.dumps(document))
    run = tmp_path / "run"

    def factory(*_args, **_kwargs):
        if adapter is not None:
            return adapter
        return adapter_class(components=components, used=used)

    with pytest.MonkeyPatch.context() as patch:
        family14_admitted(patch)
        result = await evolve(
            path, run, adapter_factory=factory, authorizations=authorizations, cancelled=cancelled
        )
    if keep is not None:
        keep.append(result)
    if terminal is not None:
        assert result.terminal == terminal, (result.terminal, result.owner, result.code)
    if fault is not None:
        assert (result.owner, result.code) == fault
    return run


OBSERVE_GATE = {
    "mode": "observe",
    "causal": {"tolerance": "measured_noise"},
    "provenance": {"kind": "reported"},
    "fresh_parent": True,
    "global": "off",
}


STATISTICAL_GATE = {
    "mode": "statistical",
    "focus": {"confidence": 0.9},
    "causal": {"confidence": 0.9, "tolerance": "measured_noise"},
    "provenance": {"kind": "reported"},
    "fresh_parent": True,
    "global": "point",
}


LENIENT = {
    owner: {"max_count": 100, "max_rate": "1"}
    for owner in ("target", "environment", "scorer", "rollout")
}


EDITED = {"used": lambda case_id, turn: ["skills/SKILL.md"]}


OBSERVE = {"gate": {"mode": "observe"}, "repeats": 1, **EDITED}


MANIFEST_SEED = v11_document()["campaign"]["random_seed"]


class _RepeatTagged(FakeAdapter):
    """Appends `#r<repeat>` to every reply, so `VARYING_SCORER` can score by
    repeat: the scorer sees only `{case_id, public, oracle, view}`, and the
    reply text is in `view.history`. Deterministic, so a resume re-scores alike."""

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        send = instance.send

        async def tagged(session, message, request_id):
            result = await send(session, message, request_id)
            tag = f" #r{session.requested_seed - MANIFEST_SEED}"
            first = {**result.events[0], "text": f"{result.events[0]['text']}{tag}"}
            return dataclasses.replace(result, events=(first, *result.events[1:]))

        instance.send = tagged
        return instance


VARYING_SCORER = r"""
import json
import os
import pathlib
import re
import sys

payload = json.load(sys.stdin)
view = json.dumps(payload["view"], sort_keys=True)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = ""
if root:
    path = pathlib.Path(root) / "skills" / "SKILL.md"
    if path.is_file():
        skill = path.read_text(encoding="utf-8")
edited = "edited" in view or "edited" in skill
match = re.search(r"#r(\d+)", view)
repeat = int(match.group(1)) if match else 1
table = {
    ("c1", True): [0.6, 0.9, 0.8, 1.0],
    ("c1", False): [0.1, 0.2, 0.2, 0.2],
    ("c2", True): [0.80, 0.90, 0.85, 0.90],
    ("c2", False): [0.82, 0.88, 0.85, 0.89],
}
score = table[(payload["case_id"], edited)][(repeat - 1) % 4]
json.dump(
    {"metrics": {"score": score}, "critical_pass": True,
     "verdicts": [{"criterion": "answered", "passed": score >= 0.5}]},
    sys.stdout,
)
"""


VARYING = {"adapter_class": _RepeatTagged, "scorer": VARYING_SCORER, **EDITED}


STATISTICAL = {"gate": {"causal": {"confidence": 0.9, "tolerance": 0.05}}, "repeats": 4, **VARYING}


def _arm_on_finalists_bound(monkeypatch, adapter):
    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook

    def hook(self, name):
        if name == "after_finalists_bound":
            adapter.armed = True
        return original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)


class _CohortFaults(FakeAdapter):
    """Plays `faults[(kind, case_id, repeat)]` on cohort instances only.

    `armed` turns on when `finalists_bound` is written (`_arm_on_finalists_bound`),
    so every instance it touches is a member's. `kind` is `candidate.kind`:
    `champion` for the Champion member, `candidate` for a finalist. Clearing
    `faults` repairs the target for a resume.
    """

    def __init__(self, *, faults, **kwargs):
        super().__init__(**kwargs)
        self.faults = dict(faults)
        self.armed = False

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        kind = candidate.kind

        def lookup(session):
            if not self.armed:
                return "completed"
            repeat = session.requested_seed - MANIFEST_SEED
            return self.faults.get((kind, session.case_id, repeat), "completed")

        instance._lookup = lookup
        return instance


def _run_kwargs(config):
    config = dict(config)
    return {"gate": config.pop("gate"), "repeats": config.pop("repeats")}, config


WORSE_SCORER = VARYING_SCORER.replace(
    '("c1", True): [0.6, 0.9, 0.8, 1.0]', '("c1", True): [0.0, 0.05, 0.1, 0.0]'
)


C2_FAILS_SCORER = r"""
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = ""
if root:
    skill_path = pathlib.Path(root) / "skills" / "SKILL.md"
    if skill_path.is_file():
        skill = skill_path.read_text(encoding="utf-8")
# `c2` fails its own criterion, so its failure is another cluster than `c1`'s.
criterion = "answered" if payload["case_id"] == "c1" else "formatted"
passed = payload["case_id"] == "c1" and "edited" in skill
json.dump(
    {
        "metrics": {"score": 1.0 if passed else 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": criterion, "passed": passed}],
    },
    sys.stdout,
)
"""


def _continue(refused):
    from evolution.tree.execution.authorizations import Authorization

    assert "continue" in refused.effects
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "the target moved",
        now="2026-10-01T10:00:00Z",
        effect="continue",
    )


def _only_c1(case_id, turn):
    """`c1` used the edited skill, `c2` only a prompt the editor never touches."""

    return ["skills/SKILL.md"] if case_id == "c1" else ["prompts/system.md"]


def _mismatches(run):
    from evolution.tree.campaign import inspect_campaign

    verification = inspect_campaign(run, verify=True).to_payload()["verification"]
    assert verification is not None
    return verification["mismatches"]


class _PowerCut(BaseException):
    """A process death between two records: no `except Exception` holds it.

    A joint stage holds every `Exception` of one side so that the other
    side's open stage still completes (codex round 2), so only a cut it cannot
    hold leaves the Candidate's stage open with nothing dispatched.
    """


async def _crash_once_and_resume(tmp_path, crashed):
    """Run the `_only_c1` Attempt until a cut raises, check `crashed`, resume.

    One Target object serves both invocations, so an instance the cut left
    live is one the resumed run must release. The resume finishes and the
    audit is clean.
    """

    from tests.campaign_fakes import Crash

    adapter = FakeAdapter(used=_only_c1)
    with pytest.raises((Crash, _PowerCut)):
        await two_case_run(tmp_path, adapter=adapter)
    crashed(tmp_path / "run")
    run = await two_case_run(tmp_path, adapter=adapter)
    assert adapter.live == {}
    assert _mismatches(run) == []
    return run


def _cut_hook(monkeypatch, seam, *, call=1):
    """Raise `Crash` from the `call`-th firing of the campaign hook `seam`, once."""

    from evolution.tree.campaign import _Campaign
    from tests.campaign_fakes import Crash

    original, seen = _Campaign.hook, []

    def hook(self, name):
        if name == seam:
            seen.append(name)
            if len(seen) == call:
                raise Crash("power cut")
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)
    return seen


def _every_case(case_id, turn):
    """Every case used the edited skill: every case is affected."""

    return ["skills/SKILL.md"]


def _rebaseline(refused):
    from evolution.tree.execution.authorizations import Authorization

    assert refused.effects == ("rebaseline", "close")
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "the gate moved",
        now="2026-10-02T10:00:00Z",
        effect="rebaseline",
    )


def _reflector_classifying(state, *portions):
    """`_reflector_claiming` with one group over several cases: the `n`-th
    reflection classifies every case of `portions[n]` (the last thereafter)."""

    def diagnosis(case_ids):
        group = {
            "root_cause": "r",
            "suggested_fix": "f",
            "evidence_ids": [f"obs:{case_id}:1" for case_id in case_ids],
            "affected_cases": list(case_ids),
            "target_paths": ["skills/SKILL.md"],
            "counterevidence": "c",
            "regression_risks": "r",
            "why_this_layer": "w",
        }
        return json.dumps({"status": "reflected", "note": "n", "groups": [group]})

    return (
        "import pathlib\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "seen = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(seen + 1))\n"
        f"replies = {[diagnosis(case_ids) for case_ids in portions]!r}\n"
        "print(replies[min(seen, len(replies) - 1)])\n"
    )


class _Resettable(FakeAdapter):
    """One fake Target whose settings the operator changes between invocations.

    One object across the change, so an instance the first condition left
    live is the one the resumed run must release.
    """

    settings = "settings-v1"

    def _semantic_for(self, dependency_digest):
        from evolution.tree.execution.journal_identity import SemanticIdentity

        semantic = super()._semantic_for(dependency_digest)
        return SemanticIdentity(
            semantic.contract_digest,
            semantic.dependency_digest,
            self.settings,
            semantic.seed_mode,
            semantic.protocol_version,
            semantic.runtime_ref_digest,
        )


def _attempt_of(run):
    return _events(run, "affected_cases_selected")[0]["payload"]["attempt"]


def _findings(run, event):
    return [m for m in verify_evidence(run)["mismatches"] if m["event"] == event]


async def _live_prefix(path, *, gate, repeats, **kwargs):
    from tests.campaign_drivers import _cut_hook, _records
    from tests.campaign_drivers import two_case_run as _two_case_run
    from tests.campaign_fakes import Crash

    with pytest.MonkeyPatch.context() as patch:
        _cut_hook(patch, "before_finalists_bound")
        with pytest.raises(Crash):
            await _two_case_run(
                path, as_family_13={"gate": gate, "repeats": repeats}, terminal=None, **kwargs
            )
    return _records(path / "run" / "campaign.jsonl")


def native14_created(**settings):
    """Copy a creation born in the native family-14 prefix, never an old writer."""
    from evolution.tree.campaign_policy import CampaignPolicyV11
    from evolution.tree.execution.journal_identity import canonical_json
    from tests.campaign_fakes import with_final_controls

    # Change only declared policy controls, retaining native connection bindings.
    payload = copy.deepcopy(_native14_creation())
    built = CampaignPolicyV11.from_payload(
        with_final_controls(payload["identity"]["policy"], **settings)
    )
    payload["identity"].update(policy=built.to_payload(), policy_digest=built.digest)
    payload["identity_digest"] = hashlib.sha256(canonical_json(payload["identity"])).hexdigest()
    return payload


@lru_cache(maxsize=1)
def _native14_creation():
    """One real prefix per worker, copied before each caller mutates it.

    A separate thread also permits use by synchronous schema helpers inside async tests.
    Scratch lives in the default temporary directory (tmpfs), never in the checkout.
    """
    from concurrent.futures import ThreadPoolExecutor
    from tempfile import TemporaryDirectory

    def build():
        with TemporaryDirectory() as root:
            records = asyncio.run(
                _live_prefix(Path(root), gate={"mode": "statistical"}, repeats=3, **EDITED)
            )
            assert records[0]["payload"]["family_version"] == 14
            return records[0]["payload"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(build).result()


def _open_round(life):
    (number,) = [n for n, s in life.snapshot.rounds.items() if s.closed is None]
    return number, life.snapshot.rounds[number]


@pytest.fixture(scope="module")
def observe_barrier(tmp_path_factory):
    """Observe, R = 1, one Attempt (F = [c1], C = [c2]) closed `accepted`."""

    from tests.campaign_drivers import EDITED

    return asyncio.run(
        _live_prefix(
            tmp_path_factory.mktemp("cohort-observe"),
            gate={"mode": "observe"},
            repeats=1,
            **EDITED,
        )
    )


SERIAL = {"max_concurrency": 1}


def _three_case_journal(root):
    """A real native family-14 run over `c1`-`c3` whose first Attempt is sealed.

    The rejecting scorer fails every case and the reflector claims `c2`, so
    round 1 opens, edits and seals an Attempt. Returns the journal's records.
    """

    import asyncio

    from tests.tree_evolve_support import REJECTING_SCORER_SCRIPT

    rows = [{**_row(case_id, []), "reflection_metadata": {}} for case_id in ("c1", "c2", "c3")]
    path = two_case_manifest(root, rows=rows)
    (root / "scorer" / "score.py").write_text(REJECTING_SCORER_SCRIPT)
    group = {
        "root_cause": "r",
        "suggested_fix": "f",
        "evidence_ids": ["obs:c2:1"],
        "affected_cases": ["c2"],
        "target_paths": ["skills/SKILL.md"],
        "counterevidence": "c",
        "regression_risks": "r",
        "why_this_layer": "w",
    }
    diagnosis = {"status": "reflected", "note": "n", "groups": [group]}
    (root / "reflector" / "reflect.py").write_text("print(" + repr(json.dumps(diagnosis)) + ")\n")
    result = asyncio.run(evolve(path, root / "run", adapter_factory=lambda *a, **k: FakeAdapter()))
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    return _records(root / "run" / "campaign.jsonl")


def _cancel_when(run, member, needle, count=1):
    """The caller's token: it flips once `member`'s journal holds `count` `needle`s."""

    journal = _measurement_events(run, member)

    def cancelled():
        return journal.is_file() and journal.read_bytes().count(needle) >= count

    return cancelled


def _change(refused, effect):
    """The operator's decision on a `repeats` change: `rebaseline` or `close`."""

    from evolution.tree.execution.authorizations import Authorization

    assert refused.effects == ("rebaseline", "close")
    return Authorization.new(
        "run_identity_changed",
        str(refused.instance_digest),
        "the repeats moved",
        now="2026-10-05T10:00:00Z",
        effect=effect,
    )


def v13_document(base=v11_document) -> dict:
    """Unparsed `/13`-shaped blueprint, lifted to native `/15` before loading."""

    document = copy.deepcopy(base())
    document["schema"] = "evolution-v2-manifest/13"
    document["campaign"]["gate"] = copy.deepcopy(AFFECTED_GATE)
    return document


TWO_ATTEMPTS = {"attempts_per_round": 2, "max_concurrency": 2}


def _statistical(mode):
    return {**STATISTICAL, "gate": {**STATISTICAL["gate"], "global": mode}}


def _cohort_scorer(flag, *, flip=False, critical=False):
    """`VARYING_SCORER`, changed once `flag` exists (after `finalists_bound`).

    `flip`: the edited tree scores as the unedited one, so the cohort sees a
    finalist exactly equal to the Champion, repeat by repeat. `critical`: the
    edited tree fails its critical check on `c2`. The gate, which runs before
    the flag exists, is unchanged.
    """

    from tests.campaign_drivers import VARYING_SCORER

    text = VARYING_SCORER
    if flip:
        text = text.replace(
            'edited = "edited" in view or "edited" in skill',
            'edited = ("edited" in view or "edited" in skill) '
            f"and not pathlib.Path({str(flag)!r}).exists()",
        )
    if critical:
        text = text.replace(
            '"critical_pass": True',
            '"critical_pass": not (edited and payload["case_id"] == "c2" '
            f"and pathlib.Path({str(flag)!r}).exists())",
        )
    assert text != VARYING_SCORER
    return text


def _flag_on_finalists_bound(monkeypatch, flag):
    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook

    def hook(self, name):
        if name == "after_finalists_bound":
            flag.touch()
        return original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)


class _CohortTagged(_CohortFaults):
    """Appends ` #cohort` to every reply once armed, so a scorer can score the
    cohort's members apart from every earlier measurement."""

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        send = instance.send

        async def tagged(session, message, request_id):
            result = await send(session, message, request_id)
            if not self.armed:
                return result
            first = {**result.events[0], "text": f"{result.events[0]['text']} #cohort"}
            return dataclasses.replace(result, events=(first, *result.events[1:]))

        instance.send = tagged
        return instance


def _blind_to_out_of_range(monkeypatch):
    """A defective executor: no primary range, so it never sees the out-of-range
    row as a fault and calibrates (or decides) over the faulted stage."""

    from evolution.tree.campaign_statistics import NO_PRIMARY_RANGE

    monkeypatch.setattr(
        campaign_module._Campaign, "range_of", lambda self, measurement_id: NO_PRIMARY_RANGE
    )


class _ResettableTagged(_Resettable, _RepeatTagged):
    """`_RepeatTagged` replies from a Target whose settings the operator changes."""


def _cut_after_cohort_stage(monkeypatch, stage):
    """Raise `Crash` once `cohort_stage(stage)` has returned (its barrier ran)."""

    from evolution.tree.campaign import _Campaign

    original = _Campaign.cohort_stage

    async def cohort_stage(self, action):
        await original(self, action)
        if action.stage == stage:
            raise Crash("cut after the cohort stage")

    monkeypatch.setattr(_Campaign, "cohort_stage", cohort_stage)


MAP_FILE = {"skills/SKILL.md": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}


DECLINING_EDITOR = (
    'import json; print(json.dumps({"status": "declined", "note": "nothing to do"}))\n'
)


async def _selection_run(tmp_path, monkeypatch, config=OBSERVE, *, cut=None, call=1, **kwargs):
    """A `/14` run of `config`. With `cut`, it raises `Crash` at that hook's
    `call`-th firing and returns `(run, None)`; without, it runs to its
    terminal and returns `(run, result)`. A second call over the same
    `tmp_path` resumes the run."""

    if cut is not None:
        _cut_hook(monkeypatch, cut, call=call)
    as_family_13, rest = _run_kwargs(config)
    if "adapter" in kwargs:
        rest.pop("used", None)  # the adapter carries its own `used`
    keep: list = []
    run_kwargs = {**rest, **kwargs}
    if cut is not None:
        with pytest.raises(Crash):
            await two_case_run(tmp_path, as_family_13=as_family_13, terminal=None, **run_kwargs)
        return tmp_path / "run", None
    await two_case_run(tmp_path, as_family_13=as_family_13, terminal=None, keep=keep, **run_kwargs)
    return tmp_path / "run", keep[0]


def _completed(life, mid, **overrides):
    payload = dict(
        round=1,
        measurement_id=mid,
        prefix=f"measurements/{mid}/events.jsonl#bytes=0:100",
        complete=True,
        point=0.5,
        missing=0,
        critical=False,
        evidence=[life.reference(1)],
    )
    payload.update(overrides)
    return payload


def _redispatched(life, measurement_id, end):
    locator = f"measurements/{measurement_id}/events.jsonl#bytes=0:{end}"
    reference = dict(
        version=1,
        kind="journal_prefix",
        run_id=life.run_id,
        locator=locator,
        content_digest="0" * 64,
    )
    return dict(measurement_id=measurement_id, stage=1, count=1, evidence=[reference])


class _CutOnSend(_CohortFaults):
    """`_CohortFaults` whose `cut` key `(kind, case_id)` cuts the power once.

    The power goes from the session's first `send`, after the try's
    `rollout_started` is durable. `_PowerCut` is a `BaseException`, so neither
    the rollout (which classifies a `RuntimeError` as a target failure) nor
    `run_joint_stage` holds it. `cut` is `None` until the test arms it.
    """

    cut: tuple[str, str] | None = None

    def instance_for(self, candidate, instance_id, boot_id):
        from tests.campaign_drivers import _PowerCut

        instance = super().instance_for(candidate, instance_id, boot_id)
        kind, send = candidate.kind, instance.send

        async def cutting(session, message, request_id):
            if self.cut == (kind, session.case_id):
                self.cut = None
                raise _PowerCut("power cut")
            return await send(session, message, request_id)

        instance.send = cutting
        return instance


def _starts(run, member, case_id):
    """`(retry_ordinal, repair)` of each try of `member`'s `case_id`/`01`, in order."""

    return [
        (int(r["payload"].get("retry_ordinal", 0)), r["payload"].get("repair") is True)
        for r in _records(_measurement_events(run, member))
        if r["event"] == "rollout_started"
        and (r["payload"]["case_id"], r["payload"]["sample_id"]) == (case_id, "01")
    ]


FACT_KEYS = (
    "round",
    "attempt_id",
    "measurement_id",
    "from_measurement_id",
    "look",
    "stage",
    "outcome",
    "reason",
    "effect",
    "supersedes",
    "comparison",
    "binding",
    "source",
    "epoch",
    "imported",
    "released",
    "complete",
)


ROW_EVENTS = ("sample_imported", "rollout_finished", "scoring_finished")


def _facts(run):
    """What a resumed run shares with an uncut run of its scenario: each
    campaign record's event and naming keys in journal order, the artifact
    ledger's events, and each measurement journal's rows (as a multiset: a
    stage's rows finish concurrently)."""

    campaign = [
        (r["event"], {k: r["payload"][k] for k in FACT_KEYS if k in r["payload"]})
        for r in _records(run / "campaign.jsonl")
    ]
    ledger = [r["event"] for r in _records(run / "artifacts.jsonl")]
    rows = {
        journal.name: sorted(
            (r["event"], r["payload"].get("case_id"), r["payload"].get("sample_id"))
            for r in _records(journal / "events.jsonl")
            if r["event"] in ROW_EVENTS
        )
        for journal in sorted((run / "measurements").iterdir())
    }
    return campaign, ledger, rows


COUNTING_SCORER = r"""
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = ""
if root:
    skill_path = pathlib.Path(root) / "skills" / "SKILL.md"
    if skill_path.is_file():
        skill = skill_path.read_text(encoding="utf-8")
oracle = payload["oracle"]
passed = bool(oracle.get("pass")) or skill.count("edited") >= oracle.get("need", 1)
json.dump(
    {
        "metrics": {"score": 1.0 if passed else 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": passed}],
    },
    sys.stdout,
)
"""


DECLINING_REFLECTOR = (
    'import json; print(json.dumps({"status": "declined", "note": "n", "groups": []}))\n'
)


def _cut_before_round_closed(monkeypatch):
    """Raise `Crash` once on the `round_closed` append: `promotion_committed` is durable."""

    from evolution.tree.storage.append_log import AppendLog

    original, fired = AppendLog.append, []

    def append(self, run_id, event, *args, **kwargs):
        if event == "round_closed" and not fired:
            fired.append(event)
            raise Crash("power cut")
        return original(self, run_id, event, *args, **kwargs)

    monkeypatch.setattr(AppendLog, "append", append)


def _cut_store_hook(monkeypatch, seam):
    """Raise `Crash` once from S2's `seam` (forwarded through `_Campaign.store_hooks`)."""

    from evolution.tree.campaign import _Campaign

    fired = []

    def crash():
        if not fired:
            fired.append(seam)
            raise Crash("power cut")

    monkeypatch.setattr(_Campaign, "store_hooks", property(lambda self: {seam: crash}))


@pytest.fixture(scope="module")
def sealed_prefix(tmp_path_factory):
    """A real family-12 journal over c1-c3, cut at its first `attempt_sealed`,
    with the exam narrowed to c2 (corpus index 1), as the family-12 tests have
    it. `LifecycleFixture` moves its creation to family 13."""

    from tests.campaign_drivers import _three_case_journal

    records = _three_case_journal(tmp_path_factory.mktemp("family13-sealed"))
    sealed = next(i for i, r in enumerate(records) if r["event"] == "attempt_sealed")
    prefix = json.loads(json.dumps(records[: sealed + 1]))
    for record in prefix:
        if record["event"] == "gate_scope_selected":
            record["payload"].update(cases=["c2"], selected_cases=["c2"], claimed_cases=["c2"])
    return prefix


class _UnseededRepeatTagged(FakeAdapter):
    """`_RepeatTagged` behind a target that honors no seed (`seed_mode: none`).
    No session carries a seed, so the tag counts each candidate's sessions of
    a case in the order they first send: the scores still vary per repeat, but
    no row proves which repeat it is. Not resumable, and no test resumes it."""

    def __init__(self, **kwargs):
        super().__init__(seed_mode="none", **kwargs)
        self.counts: dict[tuple[str, str], int] = {}

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        send = instance.send
        tags: dict[object, int] = {}

        async def tagged(session, message, request_id):
            if session not in tags:
                key = (candidate.digest, session.case_id)
                self.counts[key] = self.counts.get(key, 0) + 1
                tags[session] = self.counts[key]
            result = await send(session, message, request_id)
            first = {**result.events[0], "text": f"{result.events[0]['text']} #r{tags[session]}"}
            return dataclasses.replace(result, events=(first, *result.events[1:]))

        instance.send = tagged
        return instance


def _champion_copy(run):
    """The predecessor's final champion copy, the successor's seed root."""

    from evolution.tree.artifacts.artifacts import ArtifactStore

    champion = ArtifactStore.inspect(run).champion
    assert champion is not None
    return run / "artifacts" / "champions" / champion.version_id


def _arguments(config, adapter):
    """`_two_case_run`'s keywords for `config` over one shared `adapter`."""

    as_family_13, rest = _run_kwargs(config)
    rest.pop("used", None)  # the adapter carries its own `used`
    rest.pop("adapter_class", None)
    return {"as_family_13": as_family_13, "adapter": adapter, **rest}


async def _uncut(tmp_path, monkeypatch, config):
    """`config` run to its terminal with no cut and no change: the control."""

    adapter = _ResettableTagged(used=EDITED["used"])
    keep: list = []
    await two_case_run(tmp_path, terminal=None, keep=keep, **_arguments(config, adapter))
    assert keep[0].terminal == "finished", (keep[0].terminal, keep[0].owner, keep[0].code)
    return tmp_path / "run"


async def _successor(tmp_path, monkeypatch, predecessor, config, *, adapter=None, keep=None):
    """A successor of `predecessor`: `/14` running `config`, or `/13` (family
    12) when `config` is `None`. Returns its run directory."""

    if adapter is None:
        adapter = _ResettableTagged(used=EDITED["used"])
    linked = (predecessor, _champion_copy(predecessor))
    if config is None:
        arguments = {"adapter": adapter}
    else:
        arguments = _arguments(config, adapter)
    return await two_case_run(
        tmp_path / "succ", terminal=None, keep=keep, linked_from=linked, **arguments
    )


class _Drifts(FakeAdapter):
    """From `on` onwards, every instance of `kind` renders another map.

    `kind` is `"candidate"` (the Candidate measurement) or `"champion"`
    (the fresh Parent); a repaired Target sets `on = False`. The first
    `after` instances of `kind` once `on` is set still render their own map.
    """

    kind = "candidate"
    on = False
    after = 0

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if self.on and candidate.kind == self.kind:
            if self.after:
                self.after -= 1
                return instance
            instance.components = {
                name: {**entry, "sha256": "d" * 64}
                for name, entry in (instance.components or {}).items()
            }
        return instance


def _map_of_the_tree(candidate_digest):
    """One component whose rendered hash is the tree's: every edit changes it."""

    return {"skills/SKILL.md": {"sha256": candidate_digest, "sources": ["skills/SKILL.md"]}}


def _drift_after(monkeypatch, adapter, seam="after_candidate_inspected"):
    """Switch `adapter.on` once the campaign fires `seam` (the baseline is done)."""

    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook

    def hook(self, name):
        if name == seam:
            adapter.on = True
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)


def _drift_on_finalists_bound(monkeypatch, adapter):
    _drift_after(monkeypatch, adapter, seam="after_finalists_bound")


def _assert_component_selection(run):
    (selected,) = _events(run, "affected_cases_selected")
    assert (selected["payload"]["mode"], selected["payload"]["fallback_reason"]) == (
        "component",
        None,
    )
    return selected["payload"]["attempt"]


class _ResettableTaggedDrifts(_Resettable, _Drifts, _RepeatTagged):
    """`_Drifts` with `VARYING_SCORER`'s repeat tag, whose settings change."""


def _dispatch_order(monkeypatch):
    """Record each joint stage's rows in the order `run_interleaved` starts them."""

    original = campaign_module.run_interleaved
    seen: list[list[tuple[str, str, int]]] = []

    async def run_interleaved(dispatches, order):
        keyed = sorted(
            (
                order(index, row),
                dispatch.measurement.measurement_id,
                row["case_id"],
                int(row["repeat"]),
            )
            for index, dispatch in enumerate(dispatches)
            for row in dispatch.rows
        )
        seen.append([entry[1:] for entry in keyed])
        return await original(dispatches, order)

    monkeypatch.setattr(campaign_module, "run_interleaved", run_interleaved)
    return seen


STRICT_TARGET = {"indeterminate": {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}}}


class _ParentExhaustsOnceOn(FakeAdapter):
    """Once `on` is set, every champion instance plays `target_failed` for
    every try of `c1`. Instances live across stages, so the switch is read at
    each try, not when the instance is made."""

    on = False

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion":
            instance._lookup = lambda session: (
                "target_failed" if self.on and session.case_id == "c1" else "completed"
            )
        return instance


def _drift_locator(measurement_id="m-r01-a01", end=512, start=0):
    return f"measurements/{measurement_id}/events.jsonl#bytes={start}:{end}"


def _components_changed(seq, operation_id, kind="stage_record", **extra):
    evidence = dict(evidence_kind=kind, seq=seq, operation_id=operation_id)
    evidence.update({"prefix": _drift_locator(), **extra})
    return dict(reason="target_components_changed", owner="target", evidence=evidence)


_BOUND: dict = {}


def bound(attempt):
    assert _BOUND["attempt_id"] == attempt
    return dict(_BOUND)


def _stage(life, measurement_id, stage=1, count=1):
    """A `stage_opened` at the policy's first declared look."""

    look = life.state.policy.looks[0]
    return dict(measurement_id=measurement_id, stage=stage, look=look, sample_count=count)


def parent_bound(life, attempt):
    state = life.attempt(attempt)
    return dict(
        attempt_id=attempt,
        round=state.round,
        measurement_id=f"m-{attempt}-parent",
        candidate_digest=state.parent_digest,
        identity_digest="b" * 64,
    )


def selection(attempt, **fields):
    return {
        "round": 1,
        "attempt": attempt,
        "source": "reported",
        "mode": "path",
        "changed": ["skills/SKILL.md"],
        "unknown_cases": [],
        "fallback_reason": None,
        **fields,
    }


def v14_document(base=v11_document, *, gate=None, repeats=3) -> dict:
    """Unparsed `/14`-shaped blueprint, lifted to native `/15` before loading."""

    document = copy.deepcopy(base())
    document["schema"] = "evolution-v2-manifest/14"
    campaign = document["campaign"]
    del campaign["alpha"]
    campaign["evaluation"] = {"repeats_per_question": repeats}
    campaign["gate"] = copy.deepcopy(gate or STATISTICAL_GATE)
    return document


class _Workspace:
    """A scaffolded workspace and its reference target, served on a loopback port.

    The manifest is exactly the native `/15` scaffold document.
    """

    def __init__(self, root: Path) -> None:
        self.manifest = scaffold(root / "demo").manifest
        self.target = bind_reference_target(load_manifest_v2(self.manifest))
        self.server = serve(self.target, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.target.close()


def audit_mismatches(run_dir: Path) -> list[Any]:
    verification = inspect_campaign(run_dir, verify=True).to_payload()["verification"]
    return list(verification["mismatches"])


def result_payload(result: CampaignResult) -> dict[str, Any]:
    assert result.result is not None
    return result.result.to_payload()


class _ParentLies(FakeAdapter):
    """Every champion instance past the first `honest` attests a foreign Candidate.

    By default only the baseline's is honest.
    """

    champions = 0
    honest = 1

    def instance_for(self, candidate, instance_id, boot_id):
        from evolution.tree.execution.interaction import InstanceProvenance, TargetIdentity

        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion":
            self.champions += 1
            if self.champions > self.honest:
                identity = instance._identity
                instance._identity = TargetIdentity(
                    identity.semantic, InstanceProvenance(boot_id, "f" * 64), identity.attested
                )
        return instance
