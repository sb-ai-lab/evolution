"""Role failures close only what they own, on both current campaign families (#318).

A Reflector content failure closes its round `unplanned`; an Editor content
failure closes only its Attempt. Every `round_closed` a real run writes must
validate against the journal schema, which requires `attempts_opened` for
families 14 and 15 alike. Each run here crosses the real `evolve` admission on
`/15` (family 14) or `/16` (family 15); only offline inputs and executors are
replaced.
"""

from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
from evolution.tree.storage.envelope import Envelope
from tests.campaign_drivers import TWO_CASE_SCORER, _records, audit_mismatches
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import family14_document
from tests.family15_fakes import v16_document

pytestmark = pytest.mark.slow


FAMILIES = [14, 15]

GROUP = {
    "root_cause": "r",
    "suggested_fix": "f",
    "evidence_ids": ["obs:c1:1"],
    "affected_cases": ["c1"],
    "target_paths": ["skills/SKILL.md"],
    "counterevidence": "c",
    "regression_risks": "r",
    "why_this_layer": "w",
}

DECLINING_REFLECTOR = 'print(\'{"status":"declined","note":"nothing","groups":[]}\')\n'


class PowerCut(BaseException):
    pass


def reflector(*, invalid_calls: int) -> str:
    """A subprocess Reflector whose first `invalid_calls` answers cite no real evidence.

    `obs:nope:9` names no observation of the frozen input, so the settled
    result fails evidence validation: `reflection_failed(invalid_result)`, the
    reported run's terminal. Later calls cite the failing case. The call count
    lives beside the script, so a resume does not reset it.
    """

    bad = {
        "status": "reflected",
        "note": "n",
        "groups": [{**GROUP, "evidence_ids": ["obs:nope:9"]}],
    }
    good = {"status": "reflected", "note": "n", "groups": [GROUP]}
    return (
        "import pathlib\n"
        "calls = pathlib.Path(__file__).with_name('calls')\n"
        "n = int(calls.read_text()) if calls.exists() else 0\n"
        "calls.write_text(str(n + 1))\n"
        f"bad, good = {json.dumps(bad)!r}, {json.dumps(good)!r}\n"
        f"print(bad if n < {invalid_calls} else good)\n"
    )


def editor(*, violating_calls: int) -> str:
    """A subprocess Editor whose first `violating_calls` edits write outside its paths.

    `admit` refuses the projection: `editor_failed(reported, actor_violation)`,
    a `content` failure, which closes only that Attempt (a `provider` or
    `unknown` one would suspend instead). Later calls make the fixture's
    ordinary passing edit. The call count lives beside the script.
    """

    return (
        "import json, pathlib, sys\n"
        "calls = pathlib.Path(__file__).with_name('calls')\n"
        "n = int(calls.read_text()) if calls.exists() else 0\n"
        "calls.write_text(str(n + 1))\n"
        "request = json.loads(sys.stdin.read())\n"
        "workspace = pathlib.Path(request['workspace'])\n"
        f"if n < {violating_calls}:\n"
        "    (workspace / 'outside.txt').write_text('not permitted\\n')\n"
        "else:\n"
        "    skill = workspace / 'skills' / 'SKILL.md'\n"
        "    skill.write_text(skill.read_text().rstrip('\\n') + ' edited\\n')\n"
        "print(json.dumps({'status': 'edited', 'note': 'n'}))\n"
    )


async def campaign_run(
    family,
    root,
    monkeypatch,
    *,
    cut=None,
    campaign=None,
    editor_script=None,
    reflector_section=None,
    editor_section=None,
    **options,
):
    """Real `evolve` on `/15` (family 14, observe gate) or `/16` (family 15).

    `cut` raises `PowerCut` right after the named durable hook; reusing
    `root` resumes with the same command and run directory. Other keywords go
    to the shared two-case driver (`c1` fails on the baseline, `c2` passes).
    """
    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import evolve
    from tests import campaign_drivers as fixtures

    results = []
    original_hook = _Campaign.hook

    async def configured(path, *args, **kwargs):
        document = json.loads(path.read_text())
        if family == 15:
            document = family14_document(document, repeats=1)
            document["schema"] = "evolution-v2-manifest/16"
            document["campaign"]["gate"] = copy.deepcopy(v16_document()["campaign"]["gate"])
        else:
            document = family14_document(document, gate={"mode": "observe"}, repeats=1)
        if editor_section is not None:
            document["editor"] = copy.deepcopy(editor_section)
        if reflector_section is not None:
            common = {
                key: value
                for key, value in document["reflector"].items()
                if key
                in {
                    "environment_docs",
                    "families",
                    "max_consecutive_failures",
                    "probe",
                    "selection",
                }
            }
            document["reflector"] = {**common, **reflector_section}
        document["campaign"]["baseline_refresh_after_rounds"] = 0
        document["campaign"].update(campaign or {})
        path.write_text(json.dumps(document))
        if editor_script is not None:
            (path.parent / "editor" / "edit.py").write_text(editor_script)
        result = await evolve(path, *args, **kwargs)
        results.append(result)
        return result

    def hook(self, name):
        original_hook(self, name)
        if name == cut:
            raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", configured)
        patch.setattr(_Campaign, "hook", hook)
        await _two_case_run(root, terminal=None, campaign={"attempts_per_round": 1}, **options)
    return root / "run", results[-1]


# -- #318 part 2 (PDR-0040, ADR-0048): the `/16` chat Reflector repairs in role --

#: A diagnosis long enough to be an oracle needle (`MIN_NEEDLE_LENGTH`).
CHAT_GROUP = {**GROUP, "root_cause": "the skill omits the requested route"}
#: `c2` passes on the baseline and `obs:c1:1` is `c1`'s: a cross-case claim.
CROSS_CASE = {**CHAT_GROUP, "affected_cases": ["c2"]}


async def chat_campaign(family, root, monkeypatch, script, *, version=None, server=None, **options):
    """`campaign_run` with a `chat` Reflector served by `fake_chat_server`.

    Returns the run, the result and the Reflector requests the server received.
    `version`, when given, is the producing version the campaign stamps, as
    `test_tree_campaign_family14_visibility_change._version` sets it (default:
    the installed one); `server` reuses a started `(base_url, server)` pair.
    """

    import evolution
    from evolution.tree import campaign as campaign_module
    from evolution.tree.execution import journal_identity
    from tests import fake_chat_server
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector

    base_url, served = server or fake_chat_server.start(script)
    monkeypatch.setenv("E1_KEY", "test-key")
    installed = evolution.__version__
    monkeypatch.setattr(campaign_module, "resolve_package_version", lambda: version or installed)
    monkeypatch.setattr(journal_identity.metadata, "version", lambda _name: installed)
    options.setdefault("campaign", {"rounds": 1})
    try:
        run, result = await campaign_run(
            family, root, monkeypatch, reflector_section=chat_reflector(base_url), **options
        )
    finally:
        if server is None:
            served.shutdown()
            served.server_close()
    return run, result, served.script.requests


def tool(name, arguments=None):
    from tests import fake_chat_server

    return fake_chat_server.tool_call(name, arguments)


def private_log(run):
    path = run / "reflector-logs" / "r01-x01.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def events(run):
    return [r["event"] for r in records(run)]


async def test_a_v16_chat_reflector_repairs_before_the_campaign_journals_its_result(
    tmp_path, monkeypatch
):
    """The rejected finish is private: campaign history receives only the valid terminal,
    and no Editor, Target or Scorer work happens before it."""

    run, result, requests = await chat_campaign(
        15,
        tmp_path / "repaired",
        monkeypatch,
        [
            tool("list_files"),
            tool("finish_reflection", {"groups": [CROSS_CASE]}),
            tool("finish_reflection", {"groups": [CHAT_GROUP]}),
        ],
    )

    assert result.terminal == "finished", (result.owner, result.code, result.finished)
    assert len(requests) == 3
    (finished,) = payloads(run, "reflection_finished")
    assert finished["groups"][0]["affected_cases"] == ["c1"]
    assert not payloads(run, "reflection_failed")
    # Nothing is journaled between the start and the one accepted terminal.
    names = events(run)
    start = names.index("reflection_started")
    assert names[start + 1] == "reflection_finished"
    (refusal,) = [
        line for line in private_log(run) if line["phase"] == "finish_reflection_rejected"
    ]
    assert refusal["correction"] == (
        "groups[0].affected_cases[0]: each affected case needs a citation of its own observation"
    )
    assert not [line for line in private_log(run) if line["phase"] == "protocol_repair_requested"]
    # The rejection adds no campaign-journal event: the repaired run's event
    # sequence is the one of a run whose first finish was already valid.
    straight, result, _ = await chat_campaign(
        15,
        tmp_path / "straight",
        monkeypatch,
        [tool("list_files"), tool("finish_reflection", {"groups": [CHAT_GROUP]})],
    )
    assert result.terminal == "finished"
    assert events(straight) == names
    assert_round_closures_match_the_schema(run, 15)
    assert audit_mismatches(run) == []


async def test_a_v16_chat_reflector_secret_leak_closes_without_an_attempt(tmp_path, monkeypatch):
    """A leaked run secret ends the role with no correction turn, and the campaign
    records b11's content failure: `invalid_result`, not a `reported` code."""

    run, result, requests = await chat_campaign(
        15,
        tmp_path,
        monkeypatch,
        [
            tool("list_files"),
            tool("finish_reflection", {"groups": [{**CROSS_CASE, "root_cause": "test-key"}]}),
            tool("finish_reflection", {"groups": [CHAT_GROUP]}),
        ],
    )

    assert result.terminal == "finished"
    assert len(requests) == 2
    (failure,) = payloads(run, "reflection_failed")
    assert (failure["error_class"], failure["reason"]) == ("invalid_result", "invalid_result")
    assert not payloads(run, "attempt_opened")
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == ("unplanned", 0)
    assert "test-key" not in (run / "reflector-logs" / "r01-x01.jsonl").read_text()
    assert "test-key" not in (run / "campaign.jsonl").read_text()


async def test_a_v15_chat_reflector_keeps_its_admission_after_the_role_ends(tmp_path, monkeypatch):
    """`/15` is unchanged: its first finish is the result, refused only at final admission."""

    run, result, requests = await chat_campaign(
        14,
        tmp_path,
        monkeypatch,
        [
            tool("list_files"),
            tool("finish_reflection", {"groups": [CROSS_CASE]}),
            tool("finish_reflection", {"groups": [CHAT_GROUP]}),
        ],
    )

    assert result.terminal == "finished"
    assert len(requests) == 2
    assert "result_repair" not in json.dumps(requests[-1]["tools"])
    finish = next(t for t in requests[-1]["tools"] if t["function"]["name"] == "finish_reflection")
    assert finish["function"]["description"] == "finish reflection"
    assert_unplanned_first_round(run)
    assert not [line for line in private_log(run) if line["phase"] == "finish_reflection_rejected"]


#: The release before the repair contract, as the interrupted run's producing
#: version. The resume runs under the installed version, not a fixed one: the
#: artifact store stamps the installed version, and a resumed run whose version
#: equals the interrupted one would hide the `producing_version` difference.
PREVIOUS = "2.0.0b11"


async def test_an_unfinished_v16_chat_run_resumes_after_one_rebase(tmp_path, monkeypatch):
    """Design §7: a `/16` chat run started under the previous Reflector identity
    and producing version (`PREVIOUS`, with the in-role check and
    the V4 prompt switched off), interrupted during reflection, is refused once with
    `run_identity_changed`; one accepted `rebase` resumes it under the new
    instruction and `finish_reflection` description, with no second authorization."""

    from evolution.tree import evolve as evolve_module
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests import fake_chat_server

    script = [
        tool("list_files"),
        tool("finish_reflection", {"groups": [CROSS_CASE]}),
        tool("finish_reflection", {"groups": [CHAT_GROUP]}),
    ]
    served = fake_chat_server.start(script)
    try:
        real = evolve_module.build_reflector
        with monkeypatch.context() as patch:
            # The previous release: no in-role check, the V3 prompt, its version.
            patch.setattr(
                evolve_module,
                "build_reflector",
                lambda *a, **kw: real(*a, **{**kw, "result_repair": False}),
            )
            with pytest.raises(PowerCut):
                await chat_campaign(
                    15,
                    tmp_path,
                    monkeypatch,
                    script,
                    version=PREVIOUS,
                    server=served,
                    cut="after_reflection_started",
                )
        assert events(tmp_path / "run")[-1] == "reflection_started"
        assert served[1].script.requests == []
        with pytest.raises(EvolveRefused) as caught:
            await chat_campaign(15, tmp_path, monkeypatch, script, server=served)
        refused = caught.value
        assert refused.code == "run_identity_changed"
        assert refused.effects == ("rebase",)
        fields = sorted(difference.field for difference in refused.differences)
        assert fields == ["identity.reflector.digest", "producing_version"]
        accepted = Authorization.new(
            "run_identity_changed",
            str(refused.instance_digest),
            "accept the 318 repair contract and the version",
            now="2026-10-23T12:00:00Z",
        )
        run, result, requests = await chat_campaign(
            15,
            tmp_path,
            monkeypatch,
            script,
            server=served,
            authorizations=[accepted],
        )
    finally:
        served[1].shutdown()
        served[1].server_close()

    assert result.terminal == "finished", (result.owner, result.code, result.finished)
    assert events(run).count("identity_rebased") == 1
    assert len(requests) == 3
    (finished,) = payloads(run, "reflection_finished")
    assert finished["groups"][0]["affected_cases"] == ["c1"]
    assert audit_mismatches(run) == []


async def test_replay_does_not_recompute_admission_from_current_oracles(tmp_path, monkeypatch):
    """Semantic and oracle checks are admission-time: replay, audit and verified
    inspection keep their structural checks even when today's oracle set would
    refuse a journaled group (design §3)."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.roles import reflector_contracts

    run, result, _ = await chat_campaign(
        15,
        tmp_path,
        monkeypatch,
        [tool("list_files"), tool("finish_reflection", {"groups": [CHAT_GROUP]})],
    )
    assert result.terminal == "finished"
    journaled = payloads(run, "reflection_finished")[0]["groups"][0]["root_cause"]

    def changed_oracles(*_args, **_kwargs):
        return (journaled,)

    def no_admission(*_args, **_kwargs):
        raise AssertionError("replay must not re-run result admission")

    for module in (campaign_module, reflector_contracts):
        for name in ("oracle_needles", "reflection_needles"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, changed_oracles)
    monkeypatch.setattr(reflector_contracts, "admit_reflection_result", no_admission)
    monkeypatch.setattr(campaign_module, "admit_reflection_result", no_admission)
    monkeypatch.setattr(reflector_contracts, "validate_groups", no_admission)

    life = CampaignLifecycle(selector=None, family_version=15)
    for row in records(run):
        life.accept(Envelope.from_payload(row, family=campaign_family_for(15)))
    assert life.snapshot.finished["outcome"] == "completed"
    assert verify_evidence(run)["mismatches"] == []
    assert audit_mismatches(run) == []
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


def records(run):
    return _records(run / "campaign.jsonl")


def payloads(run, event):
    return [r["payload"] for r in records(run) if r["event"] == event]


def cited_events(run, references):
    """The journal events a list of `journal_record` references names, by their bytes."""
    raw = (run / "campaign.jsonl").read_bytes()
    events = []
    for reference in references:
        assert reference["kind"] == "journal_record"
        name, span = reference["locator"].split("#bytes=")
        assert name == "campaign.jsonl"
        start, end = (int(x) for x in span.split(":"))
        events.append(json.loads(raw[start:end])["event"])
    return events


def calls(root, role):
    path = root / role / "calls"
    return int(path.read_text()) if path.exists() else 0


def assert_round_closures_match_the_schema(run, family):
    """Every `round_closed` passes the family's own envelope check and counts its Attempts."""
    opened = Counter(p["round"] for p in payloads(run, "attempt_opened"))
    closures = [r for r in records(run) if r["event"] == "round_closed"]
    assert closures
    for row in closures:
        Envelope.from_payload(row, family=campaign_family_for(family))
        assert row["payload"]["attempts_opened"] == opened[row["payload"]["round"]]


def assert_verified(run, family, result):
    """Finished, replayable alone, and accepted by the verified inspection path."""
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    rows = records(run)
    assert rows[0]["payload"]["family_version"] == family
    life = CampaignLifecycle(selector=None, family_version=family)
    for row in rows:
        life.accept(Envelope.from_payload(row, family=campaign_family_for(family)))
    assert life.snapshot.finished["outcome"] == "completed"
    assert_round_closures_match_the_schema(run, family)
    assert verify_evidence(run)["mismatches"] == []
    assert audit_mismatches(run) == []


def assert_unplanned_first_round(run):
    """Round 1 closed on the invalid reflection alone; the Champion stayed."""
    (failed,) = [p for p in payloads(run, "reflection_failed") if p["round"] == 1]
    assert (failed["error_class"], failed["reason"]) == ("invalid_result", "invalid_result")
    first = next(p for p in payloads(run, "round_closed") if p["round"] == 1)
    assert (first["outcome"], first["reason"], first["attempts_opened"]) == (
        "unplanned",
        "reflection_failed",
        0,
    )
    # It cites the portion it focused and the reflection terminal it read.
    assert cited_events(run, first["evidence"]) == ["focus_portion_selected", "reflection_failed"]
    # No Attempt, no Candidate measurement and no Promotion in round 1.
    assert not [p for p in payloads(run, "attempt_opened") if p["round"] == 1]
    assert not [
        p
        for p in payloads(run, "measurement_bound")
        if p["round"] == 1 and p["attempt_id"] != "champion"
    ]
    baseline = next(p for p in payloads(run, "baseline_bound") if p["round"] == 1)
    assert first["champion_version"] == baseline["champion_version"]
    assert first["champion_digest"] == baseline["candidate_digest"]
    return first


# -- (a) a Reflector content failure closes its round and the campaign goes on --


@pytest.mark.parametrize("family", FAMILIES)
async def test_an_invalid_reflection_closes_its_round_unplanned_and_the_campaign_continues(
    tmp_path, monkeypatch, family
):
    run, result = await campaign_run(
        family, tmp_path, monkeypatch, campaign={"rounds": 2}, reflector=reflector(invalid_calls=1)
    )
    assert_unplanned_first_round(run)
    first, second = payloads(run, "round_closed")
    # The next round opens, reflects validly and promotes its Candidate.
    assert (second["round"], second["outcome"], second["attempts_opened"]) == (2, "promoted", 1)
    assert [p["round"] for p in payloads(run, "attempt_opened")] == [2]
    (finished,) = payloads(run, "campaign_finished")
    assert (finished["outcome"], finished["reason"]) == ("completed", "rounds_exhausted")
    assert calls(tmp_path, "reflector") == 2
    assert_verified(run, family, result)


# -- (b) resume from the reported run's durable state, and just after its close --


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("cut", ["after_reflection_failed", "after_round_closed"])
async def test_a_resume_after_the_invalid_reflection_closes_the_round_and_finishes(
    tmp_path, monkeypatch, family, cut
):
    options = dict(campaign={"rounds": 2}, reflector=reflector(invalid_calls=1))
    with pytest.raises(PowerCut):
        await campaign_run(family, tmp_path, monkeypatch, cut=cut, **options)
    run = tmp_path / "run"
    before = (run / "campaign.jsonl").read_bytes()
    # The reported run's journal ended at the durable `reflection_failed`, the
    # round still open; the other cut leaves its valid `round_closed` last.
    last = records(run)[-1]
    if cut == "after_reflection_failed":
        assert last["event"] == "reflection_failed"
        assert not payloads(run, "round_closed")
    else:
        assert (last["event"], last["payload"]["outcome"]) == ("round_closed", "unplanned")

    run, result = await campaign_run(family, tmp_path, monkeypatch, **options)
    assert (run / "campaign.jsonl").read_bytes().startswith(before)
    assert_unplanned_first_round(run)
    # The settled reflection is read, never re-run: one call per round.
    assert len(payloads(run, "reflection_failed")) == 1
    assert calls(tmp_path, "reflector") == 2
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["unplanned", "promoted"]
    assert_verified(run, family, result)


# -- (c) an Editor content failure closes only its Attempt --


def assert_editor_failure_closed_only_its_attempt(run):
    (failed,) = payloads(run, "editor_failed")
    assert (failed["attempt_id"], failed["error_class"], failed["reason"]) == (
        "r01-a01",
        "reported",
        "actor_violation",
    )
    closed = {p["attempt_id"]: p for p in payloads(run, "attempt_closed")}
    assert (closed["r01-a01"]["outcome"], closed["r01-a01"]["reason"]) == (
        "abandoned",
        "editor_failed",
    )
    # No seal, no Target and no Scorer for the failed Attempt's Candidate.
    assert "r01-a01" not in {p["attempt_id"] for p in payloads(run, "attempt_sealed")}
    assert "r01-a01" not in {p["attempt_id"] for p in payloads(run, "measurement_bound")}
    assert not list((run / "measurements").glob("m-r01-a01*"))
    # The round's next Attempt opens, edits and is measured.
    assert [p["attempt_id"] for p in payloads(run, "attempt_opened")] == ["r01-a01", "r01-a02"]
    assert closed["r01-a02"]["outcome"] == "finalist"
    (closure,) = payloads(run, "round_closed")
    assert (closure["outcome"], closure["attempts_opened"]) == ("promoted", 2)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("cut", [None, "after_attempt_closed"])
async def test_an_editor_failure_closes_only_its_attempt_and_the_next_one_opens(
    tmp_path, monkeypatch, family, cut
):
    options = dict(
        campaign={"rounds": 1, "attempts_per_round": 2},
        reflector=reflector(invalid_calls=0),
        editor_script=editor(violating_calls=1),
    )
    if cut is not None:
        # `after_attempt_closed` first fires on the failed Attempt's close.
        with pytest.raises(PowerCut):
            await campaign_run(family, tmp_path, monkeypatch, cut=cut, **options)
        last = records(tmp_path / "run")[-1]
        assert (last["event"], last["payload"]["reason"]) == ("attempt_closed", "editor_failed")
    run, result = await campaign_run(family, tmp_path, monkeypatch, **options)
    assert_editor_failure_closed_only_its_attempt(run)
    assert calls(tmp_path, "editor") == 2
    assert_verified(run, family, result)


# -- (d) every reachable `round_closed` outcome matches the schema --

#: Each scenario: its run options and the outcome per family. Not reachable on
#: family 15 with these fakes: `clean`/`stalled` -- a baseline with no failure
#: opens an Attempt there, which the comparison preflight rejects as
#: `structurally_unattainable`, so the round closes `retained` (the `perfect`
#: scenario). The family-14 closes `unscored` (no finalist, `selection_decided`
#: `no_finalists`) and `retained` (no eligible candidate) are each family's own.
SCENARIOS = {
    "invalid_reflection": (
        dict(reflector=reflector(invalid_calls=1)),
        {14: "unplanned", 15: "unplanned"},
    ),
    "declined_reflection": (
        dict(reflector=DECLINING_REFLECTOR),
        {14: "unplanned", 15: "unplanned"},
    ),
    "promoted": (dict(reflector=reflector(invalid_calls=0)), {14: "promoted", 15: "promoted"}),
    "every_editor_fails": (
        dict(reflector=reflector(invalid_calls=0), editor_script=editor(violating_calls=2)),
        {14: "unscored", 15: "retained"},
    ),
    "perfect": (
        dict(scorer=TWO_CASE_SCORER.replace('passed = "edited"', 'passed = True or "edited"')),
        {14: "clean", 15: "retained"},
    ),
}


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_every_reachable_round_closed_outcome_matches_the_schema(
    tmp_path, monkeypatch, family, scenario
):
    options, outcomes = SCENARIOS[scenario]
    run, result = await campaign_run(
        family,
        tmp_path,
        monkeypatch,
        campaign={"rounds": 1, "attempts_per_round": 2},
        **options,
    )
    assert [p["outcome"] for p in payloads(run, "round_closed")] == [outcomes[family]]
    assert_verified(run, family, result)


@pytest.mark.parametrize("family", FAMILIES)
async def test_a_subprocess_reflector_null_extra_key(tmp_path, monkeypatch, family):
    raw = {"status": "reflected", "note": "n", "groups": [{**GROUP, "suggested_fix_family": None}]}
    run, result = await campaign_run(
        family, tmp_path, monkeypatch, reflector="print(" + repr(json.dumps(raw)) + ")\n"
    )
    assert result.terminal == "finished"
    if family == 15:
        finished = [
            r["payload"]
            for r in _records(run / "campaign.jsonl")
            if r["event"] == "reflection_finished"
        ]
        assert finished and "suggested_fix_family" not in finished[0]["groups"][0]
    else:
        failed = [
            r["payload"]
            for r in _records(run / "campaign.jsonl")
            if r["event"] == "reflection_failed"
        ]
        assert failed[0]["error_class"] == "invalid_result"


@pytest.mark.parametrize(("family", "reason"), [(14, "unclassified"), (15, "program_failed")])
async def test_a_subprocess_reflector_program_failure_names_its_reason_by_family(
    tmp_path, monkeypatch, family, reason
):
    run, _ = await campaign_run(
        family,
        tmp_path,
        monkeypatch,
        campaign={"rounds": 1},
        reflector="print('not a diagnosis')\n",
    )
    (failed,) = payloads(run, "reflection_failed")
    assert (failed["error_class"], failed["reason"]) == ("reported", reason)


async def test_subprocess_empty_secret_key_is_refused_before_normalization(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family15_delivery import public_run

    secret = "forwarded-b12-role-secret"
    monkeypatch.setenv("B12_TOKEN", secret)
    raw = {"status": "reflected", "note": "n", "groups": [{**GROUP, secret: None}]}

    def configure(document):
        document["editor"]["allowed_env"] = ["B12_TOKEN"]

    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        reflector="print(" + repr(json.dumps(raw)) + ")\n",
    )
    failed = [
        r["payload"] for r in _records(run / "campaign.jsonl") if r["event"] == "reflection_failed"
    ]
    assert failed[0]["error_class"] == "invalid_result"
    assert not [r for r in _records(run / "campaign.jsonl") if r["event"] == "reflection_finished"]
    assert secret not in (run / "campaign.jsonl").read_text()


@pytest.mark.parametrize("family", FAMILIES)
async def test_final_admission_writes_the_category_and_path_only_in_family15(
    tmp_path, monkeypatch, family
):
    raw = {
        "status": "reflected",
        "note": "n",
        "groups": [{**GROUP, "affected_cases": ["VALUE-CASE"]}],
    }
    run, _ = await campaign_run(
        family, tmp_path, monkeypatch, reflector="print(" + repr(json.dumps(raw)) + ")\n"
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "invalid_result"
    if family == 15:
        assert failed["detail"] == {
            "category": "not_a_case_id",
            "path": "groups[0].affected_cases[0]",
        }
    else:
        assert "detail" not in failed
    assert "VALUE-CASE" not in (run / "campaign.jsonl").read_text()

    assert audit_mismatches(run) == []


@pytest.mark.parametrize("semantic", [True, False])
async def test_turns_exhausted_detail_is_only_the_last_semantic_rejection(
    tmp_path, monkeypatch, semantic
):
    from tests import test_tree_campaign_family14_reflector_history as helper

    original = helper.chat_reflector
    monkeypatch.setattr(helper, "chat_reflector", lambda url: {**original(url), "max_turns": 3})
    script = [tool("list_files")]
    if semantic:
        script.append(tool("finish_reflection", {"groups": [CROSS_CASE]}))
    script.append(tool("list_files"))
    run, result, _ = await chat_campaign(15, tmp_path, monkeypatch, script)
    assert result.terminal == "finished"
    (failed,) = payloads(run, "reflection_failed")
    assert failed["reason"] == "turns_exhausted"
    if semantic:
        assert failed["detail"] == {
            "category": "uncited_affected_case",
            "path": "groups[0].affected_cases[0]",
        }
    else:
        assert "detail" not in failed

    assert audit_mismatches(run) == []


async def test_an_unfinished_v16_chat_editor_run_resumes_after_one_rebase(tmp_path, monkeypatch):
    import evolution
    from evolution.tree import campaign as campaign_module
    from evolution.tree import evolve as evolve_module
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution import journal_identity
    from evolution.tree.execution.authorizations import Authorization
    from tests import fake_chat_server
    from tests.test_tree_campaign_family14_role_feedback import (
        _chat_plan,
        _chat_write,
        chat_budget_section,
    )
    from tests.test_tree_campaign_family15_delivery import PowerCut as Cut
    from tests.test_tree_campaign_family15_delivery import public_run

    script = [
        tool("list_files"),
        _chat_plan(["skills/SKILL.md"]),
        _chat_write("skills/SKILL.md", "edited\n"),
        tool("finish_edit", {"summary": "done"}),
    ]
    base_url, server = fake_chat_server.start(script)
    monkeypatch.setenv("E1_KEY", "test-key")
    installed = evolution.__version__

    def configure(document):
        document["editor"] = chat_budget_section(base_url)

    async def run(version, **options):
        with monkeypatch.context() as patch:
            patch.setattr(campaign_module, "resolve_package_version", lambda: version)
            patch.setattr(journal_identity.metadata, "version", lambda name: installed)
            return await public_run(tmp_path, patch, configure=configure, **options)

    try:
        real = evolve_module.build_editor
        with monkeypatch.context() as patch:
            patch.setattr(
                evolve_module,
                "build_editor",
                lambda *a, **kw: real(*a, **{**kw, "plan_correction": False}),
            )
            with pytest.raises(Cut):
                await run(PREVIOUS, cut="after_editor_started")
        with pytest.raises(EvolveRefused) as caught:
            await run(installed)
        refused = caught.value
        assert (refused.code, refused.effects) == ("run_identity_changed", ("rebase",))
        assert sorted(d.field for d in refused.differences) == [
            "identity.editor.digest",
            "producing_version",
        ]
        accepted = Authorization.new(
            "run_identity_changed",
            refused.instance_digest,
            "accept editor plan correction",
            now="2026-10-25T12:00:00Z",
        )
        directory, result = await run(installed, authorizations=[accepted])
    finally:
        server.shutdown()
        server.server_close()
    assert result.terminal == "finished" and events(directory).count("identity_rebased") == 1
    assert audit_mismatches(directory) == []
