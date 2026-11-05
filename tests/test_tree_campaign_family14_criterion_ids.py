"""H8 Task 9: criterion ids the scorer published on the baseline (F0 R51, A§6, A§10.7).

PDR-0017 (2026-10-09 amendment), PDR-0028 and plan decision 12. In family 14 an
oracle string exactly equal to a verdict `criterion` the scorer published on
the frozen baseline is not a needle for reflection-side text: the Grouper's
groups and the Reflector's note and diagnosis. Families 12 and 13 keep #172,
where the id stays a needle. An oracle string that merely contains the id, a
value the scorer sent only in `diagnostics`, the text of a verdict `note` and
the Candidate seal keep their checks in every family.

Every run here is genuine: a real family-14 `family14_run`, or the family-13
contrast through `_two_case_run(as_family_13=...)` (a real `/14` run). The
scorer publishes the verdict criterion `exact-answer-check` on every
observation (passed on `c2`, failed on `c1`'s baseline), with `c1`'s verdict
note and `diagnostics` taken from its oracle; the corpus's `c1` oracle holds
the id exactly, a string containing it, the `diagnostics`-only value and the
note text. Each Grouper kind is tested on its own leak route: `chat` blanks a
needled description (`_scrub`), `command` refuses `secret_detected`, and the
Reflector fails `oracle_leak`.

`fields` (honest label, plan decision 16): a built-in `fields` grouping's
result carries assignments only -- no description and no variant label
(`reflect_grouping.builtin_grouping`) -- so the `oracle_leak` predicate the
engine hands it has no string to scan, and no corpus can make a family-13
`fields` grouping fail `malformed_result` through it. Its cell therefore
shows both families grouping alike on a `failed_criteria` feature, and
checks the predicate the engine actually passed (captured from the real
call): it flags the id in family 13 and not in family 14.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from evolution.tree.roles.grouping.grouping_runtime import GroupingRuntime
from tests.agent_grouping_fakes import GROUP_ALL_AGENT_BODY, sealed_grouping_cli
from tests.campaign_drivers import TWO_CASE_SCORER, _records
from tests.model_grouping_fakes import (
    ScriptedClient,
    chat_grouper_section,
    group_everything,
    use_chat_grouper,
)
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_grouper_run import _spy_refusals
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

#: The scorer's verdict criterion; the oracle holds exactly this string too.
CRITERION = "exact-answer-check"
#: An oracle string that contains the id: still a needle in family 14.
CONTAINING = f"{CRITERION} expects refund_flow"
#: A value the scorer sends only in `diagnostics`: still a needle.
DIAGNOSTIC_ONLY = "diagnostic-only-hint"
#: The text of `c1`'s verdict note: still a needle.
NOTE = "the reference route is refund_flow"
SECOND_CRITERION = "second-baseline-check"
ORACLE = {
    "critical": True,
    "check": CRITERION,
    "explained": CONTAINING,
    "hint": DIAGNOSTIC_ONLY,
    "note": NOTE,
}

#: `TWO_CASE_SCORER` publishing the verdict `CRITERION` (note and diagnostics
#: from the case's oracle, when it has them) instead of `answered`.
SCORER = TWO_CASE_SCORER.replace(
    """json.dump(
    {
        "metrics": {"score": 1.0 if passed else 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": passed}],
    },
    sys.stdout,
)""",
    """verdict = {"criterion": "exact-answer-check", "passed": passed}
if payload["oracle"].get("note"):
    verdict["note"] = payload["oracle"]["note"]
result = {"metrics": {"score": 1.0 if passed else 0.0}, "critical_pass": True, "verdicts": [verdict]}
if payload["oracle"].get("hint"):
    result["diagnostics"] = {"hint": payload["oracle"]["hint"]}
json.dump(result, sys.stdout)""",
)

#: A Reflector naming `{text}` in its note or diagnosis (one group on `c1`).
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

#: A sealed `command` grouping program that echoes the first failing
#: observation's first failed criterion as its group label.
ECHO_CRITERION_PROGRAM = (
    "import json, subprocess, sys\n"
    "request = json.load(open(sys.argv[-1]))\n"
    "helpers = request['helpers']\n"
    "entries = json.loads(subprocess.check_output(helpers['list_grouping_inputs']))['entries']\n"
    "first = next(entry for entry in entries if entry['failing'])\n"
    "page = json.loads(subprocess.check_output(helpers['read_grouping_input'] + "
    "[json.dumps({'index': first['index'], 'section': 'observation'})]))\n"
    "label = json.loads(page['content'])['failed_criteria'][0]\n"
    "print(json.dumps({'version': 'reflect_grouping_result/1', "
    "'registry_digest': request['registry_digest'], "
    "'assignments': [0 if entry['failing'] else None for entry in entries], "
    "'descriptions': {'0': {'label': label, 'description': 'fails ' + label}}}))\n"
)

FIELDS_GROUPER = {
    "kind": "fields",
    "features": [
        {
            "name": "criteria",
            "source": "observation",
            "path": "/failed_criteria",
            "description": "the failed verdict criteria",
        }
    ],
}


def _arrange(
    monkeypatch, *, editor_script: str | None = None, extra_oracle: dict[str, Any] | None = None
) -> None:
    """`c1`'s oracle is `ORACLE`; `editor_script`, when given, is the Editor."""

    from tests import campaign_drivers as fixtures

    workspace = fixtures.write_workspace

    def write_workspace(*args, **kwargs):
        rows = [dict(row) for row in kwargs["rows"]]
        assert rows[0]["case_id"] == "c1"
        rows[0]["oracle"] = {**ORACLE, **(extra_oracle or {})}
        kwargs["rows"] = rows
        if editor_script is not None:
            kwargs["editor_script"] = editor_script
        return workspace(*args, **kwargs)

    monkeypatch.setattr(fixtures, "write_workspace", write_workspace)


def _reflector(*, note: str = "n", root_cause: str = "r") -> str:
    diagnosis = {
        "status": "reflected",
        "note": note,
        "groups": [{**GROUP, "root_cause": root_cause}],
    }
    return "print(" + repr(json.dumps(diagnosis)) + ")\n"


def _command_grouper(tmp_path) -> dict[str, Any]:
    (tmp_path / "grouping").mkdir()
    (tmp_path / "grouping" / "group.py").write_text(ECHO_CRITERION_PROGRAM)
    return {
        "kind": "command",
        "argv": [sys.executable, "group.py"],
        "cwd": "grouping",
        "dependency_paths": [],
        "allowed_env": [],
        "timeout_seconds": 60,
        "config": {},
    }


async def _run(
    tmp_path,
    monkeypatch,
    family: int,
    *,
    grouper: dict[str, Any] | None = None,
    **kwargs: Any,
):
    """One genuine run of family `family` with `SCORER`; returns `(run, finished)`.

    `grouper` is the `/15` `grouper` section (family 14) or, as `kind: model`
    for `chat`, the `/14` `reflector.grouping` (family 13); `None` keeps the
    fixture's own grouping. `finished` is the run's finished outcome, or
    `None` for a suspended run.
    """

    if family == 14:
        settings = None if grouper is None else {"grouper": grouper}
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, family14_settings=settings, scorer=SCORER, **kwargs
        )
        created = _records(run / "campaign.jsonl")[0]["payload"]
        assert created["family_version"] == 14
        # Every genuine family-14 run here audits clean.
        assert audit(run)["mismatches"] == []
        return run, None if snapshot.finished is None else snapshot.finished["outcome"]


def _events(run) -> list[str]:
    return [record["event"] for record in _records(run / "campaign.jsonl")]


def _baseline_verdicts(run, round_number: int = 1) -> dict[str, list[tuple[str, bool]]]:
    """`case_id -> [(criterion, passed)]` from one baseline's score requests."""

    bound = payloads(run, "baseline_bound")[round_number - 1]
    events = _records(run / "measurements" / bound["measurement_id"] / "events.jsonl")
    published: dict[str, list[tuple[str, bool]]] = {}
    for record in events:
        if record["event"] != "scoring_started":
            continue
        payload = record["payload"]
        from evolution.tree import read_scorer_trace

        trace = read_scorer_trace(run, payload["score_request_id"], measurement_id=record["run_id"])
        assert trace is not None and trace.stdout is not None
        published.setdefault(payload["case_id"], []).extend(
            (verdict["criterion"], verdict["passed"])
            for verdict in json.loads(trace.stdout)["verdicts"]
        )
    return published


def _suspended_on_grouping(run, code: str) -> None:
    records = _records(run / "campaign.jsonl")
    started = next(r for r in records if r["event"] == "grouping_started")
    tail = records[records.index(started) :]
    assert [r["event"] for r in tail] == [
        "grouping_started",
        "grouping_failed",
        "campaign_suspended",
    ]
    assert tail[1]["payload"]["code"] == code
    assert (tail[2]["payload"]["reason"], tail[2]["payload"]["owner"]) == (
        "grouping_failed",
        "campaign",
    )


# -- the scorer really publishes the id on the baseline -------------------------


async def test_the_scorer_publishes_the_id_on_the_baseline_and_the_oracle_holds_it(
    tmp_path, monkeypatch
):
    _arrange(monkeypatch)
    run, finished = await _run(tmp_path, monkeypatch, 14)
    assert finished == "completed"
    assert _baseline_verdicts(run) == {"c1": [(CRITERION, False)], "c2": [(CRITERION, True)]}
    corpus = [json.loads(line) for line in (tmp_path / "corpus.jsonl").read_text().splitlines()]
    assert corpus[0]["oracle"]["check"] == CRITERION


# -- `chat`: `_scrub` blanks a needled description --------------------------------


def _descriptions(run) -> dict[str, Any]:
    (finished,) = payloads(run, "grouping_finished")
    assert finished["fallback"] is None
    return finished["result"]["descriptions"]


def _agent_body(title: str, variant: str) -> str:
    """A sealed CLI answer over the real helper records, with chosen role text."""

    return (
        GROUP_ALL_AGENT_BODY.replace('"shared failures"', repr(title))
        .replace('"variant": "failure"', f'"variant": {variant!r}')
        .replace('"observed failures"', repr(title))
    )


async def test_agent_group_retains_a_published_oracle_criterion_in_text_and_variant(
    tmp_path, monkeypatch
):
    _arrange(monkeypatch)
    section = sealed_grouping_cli(tmp_path, body=_agent_body(CRITERION, CRITERION))

    run, finished = await _run(tmp_path, monkeypatch, 14, grouper=section)

    assert finished == "completed"
    (grouping,) = payloads(run, "grouping_finished")
    assert grouping["fallback"] is None
    (description,) = grouping["result"]["descriptions"].values()
    assert description["label"] == CRITERION
    assert CRITERION in description["description"]
    assert grouping["result"]["variants"] == {"0": CRITERION}
    assert grouping["calls"] == {"agent_calls": 1, "repairs": 0, "turns": 1}


@pytest.mark.parametrize("text", [CONTAINING, DIAGNOSTIC_ONLY, NOTE])
async def test_agent_scrubs_other_oracle_text_from_headers_and_variants(
    tmp_path, monkeypatch, text
):
    _arrange(monkeypatch)
    section = sealed_grouping_cli(tmp_path, body=_agent_body(text, text))

    run, finished = await _run(tmp_path, monkeypatch, 14, grouper=section)

    assert finished == "completed"
    (grouping,) = payloads(run, "grouping_finished")
    assert grouping["fallback"] is None
    assert grouping["result"]["descriptions"] == {}
    assert text not in json.dumps(grouping["result"]["variants"])
    assert "redacted-1" in json.dumps(grouping["result"]["variants"])


async def test_agent_refuses_a_run_secret_even_with_a_published_criterion(tmp_path, monkeypatch):
    secret = "task7-agent-secret-842913"
    monkeypatch.setenv("AGENT_TOKEN", secret)
    _arrange(monkeypatch)
    section = sealed_grouping_cli(
        tmp_path,
        body=_agent_body(f"{CRITERION} {secret}", CRITERION),
        allowed_env=["AGENT_TOKEN"],
    )

    run, finished = await _run(tmp_path, monkeypatch, 14, grouper=section)

    assert finished is None
    _suspended_on_grouping(run, "secret_detected")
    assert not payloads(run, "grouping_finished")
    assert secret not in (run / "campaign.jsonl").read_text()


async def test_refreshed_baseline_replaces_the_agent_criterion_exception(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from tests.test_tree_campaign_family14_grouper_run import NO_FINALISTS

    _arrange(monkeypatch, extra_oracle={"next_check": SECOND_CRITERION})
    marker = tmp_path / "round-one-closed"
    hook = _Campaign.hook

    def change_criterion_after_round(self, name):
        if name == "after_round_closed" and not marker.exists():
            marker.write_text("closed")
        return hook(self, name)

    monkeypatch.setattr(_Campaign, "hook", change_criterion_after_round)
    scorer = SCORER.replace(
        'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
        'passed = bool(payload["oracle"].get("pass"))',
    ).replace(
        'verdict = {"criterion": "exact-answer-check", "passed": passed}',
        f'verdict = {{"criterion": ({SECOND_CRITERION!r} if pathlib.Path({str(marker)!r}).exists() else {CRITERION!r}), "passed": passed}}',
    )
    assert scorer != SCORER
    section = sealed_grouping_cli(tmp_path, body=_agent_body(SECOND_CRITERION, CRITERION))

    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**NO_FINALISTS, "scorer": scorer},
        family14_settings={"grouper": section, "refresh": 1},
        campaign={"rounds": 2},
    )

    assert snapshot.finished["outcome"] == "completed"
    assert marker.exists()
    assert _baseline_verdicts(run) == {
        "c1": [(CRITERION, False)] * 4,
        "c2": [(CRITERION, True)] * 4,
    }
    assert _baseline_verdicts(run, 2) == {
        "c1": [(SECOND_CRITERION, False)] * 4,
        "c2": [(SECOND_CRITERION, True)] * 4,
    }
    first, second = payloads(run, "grouping_finished")
    assert first["result"]["descriptions"] == {}
    assert set(first["result"]["variants"].values()) == {CRITERION}
    assert len(first["result"]["variants"]) == 4
    (description,) = second["result"]["descriptions"].values()
    assert description["label"] == SECOND_CRITERION
    assert SECOND_CRITERION in description["description"]
    assert set(second["result"]["variants"].values()) == {"redacted-1"}
    assert len(second["result"]["variants"]) == 4
    assert first["fallback"] is second["fallback"] is None


@pytest.mark.parametrize("family", [14])
async def test_a_chat_group_title_naming_the_id(tmp_path, monkeypatch, family):
    """Family 13 blanks the description (`_scrub`); family 14 keeps it."""

    _arrange(monkeypatch)
    refusals = _spy_refusals(monkeypatch)
    client = use_chat_grouper(monkeypatch, ScriptedClient([group_everything(title=CRITERION)]))

    run, finished = await _run(tmp_path, monkeypatch, family, grouper=chat_grouper_section())

    assert refusals == [] and client.script == []
    assert finished == "completed"
    descriptions = _descriptions(run)
    if family == 13:
        assert descriptions == {}
        assert CRITERION.encode() not in (run / "campaign.jsonl").read_bytes()
    else:
        (description,) = descriptions.values()
        assert CRITERION in json.dumps(description)
    assert "grouping_failed" not in _events(run) and "campaign_suspended" not in _events(run)


@pytest.mark.parametrize("text", [CONTAINING, DIAGNOSTIC_ONLY, NOTE])
async def test_a_chat_group_title_with_other_oracle_text_is_still_blanked_in_family_14(
    tmp_path, monkeypatch, text
):
    _arrange(monkeypatch)
    use_chat_grouper(monkeypatch, ScriptedClient([group_everything(title=text)]))

    run, finished = await _run(tmp_path, monkeypatch, 14, grouper=chat_grouper_section())

    assert finished == "completed"
    assert _descriptions(run) == {}
    assert text.encode() not in (run / "campaign.jsonl").read_bytes()


# -- `command`: `secret_detected` -------------------------------------------------


@pytest.mark.parametrize("family", [14])
async def test_a_command_group_label_echoing_the_id(tmp_path, monkeypatch, family):
    """Family 13 refuses `secret_detected` and suspends; family 14 groups."""

    _arrange(monkeypatch)
    refusals = _spy_refusals(monkeypatch)

    run, finished = await _run(tmp_path, monkeypatch, family, grouper=_command_grouper(tmp_path))

    assert refusals == []
    if family == 13:
        assert finished is None
        _suspended_on_grouping(run, "secret_detected")
        assert "grouping_finished" not in _events(run)
    else:
        assert finished == "completed"
        assert _descriptions(run) == {
            "0": {"label": CRITERION, "description": f"fails {CRITERION}"}
        }
        assert "grouping_failed" not in _events(run)


# -- `fields`: the predicate the engine passes (see the module docstring) -------------


@pytest.mark.parametrize("family", [14])
async def test_a_fields_grouping_on_failed_criteria(tmp_path, monkeypatch, family):
    _arrange(monkeypatch)
    refusals = _spy_refusals(monkeypatch)
    predicates: list[Any] = []
    builtin = GroupingRuntime.builtin

    def capture(self, registry, *, forbidden_string_predicates=(), family15=False):
        assert family15 is False
        predicates.extend(forbidden_string_predicates)
        return builtin(
            self,
            registry,
            forbidden_string_predicates=forbidden_string_predicates,
            family15=family15,
        )

    monkeypatch.setattr(GroupingRuntime, "builtin", capture)

    run, finished = await _run(tmp_path, monkeypatch, family, grouper=FIELDS_GROUPER)

    assert refusals == [] and finished == "completed"
    (finished_grouping,) = payloads(run, "grouping_finished")
    assert finished_grouping["result"]["descriptions"] == {}
    assert finished_grouping["result"]["variants"] == {}
    assert "grouping_failed" not in _events(run)
    ((name, predicate),) = predicates
    assert name == "oracle_leak"
    assert predicate(CRITERION) is (family == 13)
    assert predicate(f"the group fails {CRITERION}") is (family == 13)
    for text in (CONTAINING, DIAGNOSTIC_ONLY, NOTE):
        assert predicate(text) is True


# -- the Reflector: `oracle_leak` ---------------------------------------------------


def _reflection_terminal(run) -> dict[str, Any]:
    (terminal,) = [
        r
        for r in _records(run / "campaign.jsonl")
        if r["event"] in {"reflection_finished", "reflection_failed"}
    ]
    return terminal


@pytest.mark.parametrize("family", [14])
@pytest.mark.parametrize("where", ["note", "root_cause"])
async def test_a_reflector_naming_the_id(tmp_path, monkeypatch, family, where):
    """Family 13 fails `oracle_leak`; family 14 finishes `reflected`."""

    _arrange(monkeypatch)
    text = f"c1 fails the {CRITERION} criterion"
    run, finished = await _run(tmp_path, monkeypatch, family, reflector=_reflector(**{where: text}))

    terminal = _reflection_terminal(run)
    if family == 13:
        assert terminal["event"] == "reflection_failed"
        assert (terminal["payload"]["error_class"], terminal["payload"]["reason"]) == (
            "oracle_leak",
            "oracle_leak",
        )
        assert payloads(run, "round_closed")[0]["outcome"] == "unplanned"
    else:
        assert terminal["event"] == "reflection_finished"
        assert terminal["payload"]["status"] == "reflected"
        if where == "root_cause":
            (group,) = terminal["payload"]["groups"]
            assert group["root_cause"] == text
        assert payloads(run, "attempt_sealed")
    assert finished == "completed"


@pytest.mark.parametrize("family", [14])
@pytest.mark.parametrize("text", [CONTAINING, DIAGNOSTIC_ONLY, NOTE])
async def test_a_reflector_naming_other_oracle_text_still_leaks(
    tmp_path, monkeypatch, family, text
):
    """An oracle string containing the id, a `diagnostics`-only value and a
    verdict note named in a diagnosis are leaks in both families."""

    _arrange(monkeypatch)
    run, finished = await _run(
        tmp_path, monkeypatch, family, reflector=_reflector(root_cause=f"because {text}")
    )

    terminal = _reflection_terminal(run)
    assert terminal["event"] == "reflection_failed"
    assert (terminal["payload"]["error_class"], terminal["payload"]["reason"]) == (
        "oracle_leak",
        "oracle_leak",
    )
    assert text.encode() not in (run / "campaign.jsonl").read_bytes()
    assert finished == "completed"


# -- the Candidate seal keeps `self.needles` -------------------------------------------


@pytest.mark.parametrize("family", [14])
async def test_the_candidate_seal_still_refuses_the_id(tmp_path, monkeypatch, family):
    from tests.tree_evolve_support import EDITOR_SCRIPT

    editor = EDITOR_SCRIPT.replace('" edited\\n"', f'" edited {CRITERION}\\n"')
    assert editor != EDITOR_SCRIPT
    _arrange(monkeypatch, editor_script=editor)
    run, finished = await _run(tmp_path, monkeypatch, family, reflector=_reflector(root_cause="r"))

    assert payloads(run, "reflection_finished")
    (editor,) = payloads(run, "editor_finished")
    assert editor["status"] == "edited"
    (closed,) = payloads(run, "attempt_closed")
    assert (closed["outcome"], closed["reason"], closed["code"]) == (
        "abandoned",
        "seal_refused",
        "oracle_leak",
    )
    assert not payloads(run, "attempt_sealed")
    assert finished == "completed"
