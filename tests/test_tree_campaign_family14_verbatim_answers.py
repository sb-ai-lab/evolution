"""Declared verbatim answers (#219, ADR-0038) in real family-14 campaigns.

A `/15` `verbatim_answers` declaration names canned replies the agent is
meant to say word for word. Every needle set drops them, so a Reflector or
an Editor may write one; an undeclared oracle value stays a leak, and so does
a longer gold value that merely contains a declared answer. Adding, changing
or removing the declaration on a resume is a non-comparable identity change:
a `rebase` that keeps the open round, after which the new needle set is in
force.
"""

from __future__ import annotations

import json

import pytest

from evolution.tree.campaign import inspect_campaign
from evolution.tree.execution.authorizations import comparable_field
from tests.campaign_drivers import TWO_CASE_SCORER, _cut_hook
from tests.campaign_fakes import Crash
from tests.test_tree_campaign_family14_role_feedback import DIAGNOSIS
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_visibility_change import _decision

pytestmark = pytest.mark.slow

CANNED = "This request is not implemented yet."
ALT = "This request cannot be handled right now."
GOLD = "GOLD-FACT-4242: refund of 45 EUR"
#: `critical` because `TWO_CASE_SCORER` always returns `critical_pass`, which a
#: noncritical case refuses (`validate_score_result`).
ORACLE = {"critical": True, "answer": CANNED, "alt": ALT, "fact": GOLD}

#: `TWO_CASE_SCORER`, plus the gold fact in `diagnostics` for the case that has one.
SCORER = TWO_CASE_SCORER.replace(
    '"verdicts": [{"criterion": "answered", "passed": passed}],',
    '"verdicts": [{"criterion": "answered", "passed": passed}],\n'
    '        **({"diagnostics": {"reference": payload["oracle"]["fact"]}}'
    ' if "fact" in payload["oracle"] else {}),',
)
assert SCORER != TWO_CASE_SCORER

#: One round of two sequential Attempts over `c1`, `c2` and `c3` (which holds
#: the canned answer, an alternative and an undeclared gold fact).
RUN = dict(
    campaign={"rounds": 1, "attempts_per_round": 2, "attempt_feedback": "sequential"},
    extra_cases={"c3": ORACLE},
)


def declare(monkeypatch, strings):
    """Add (or not) `verbatim_answers` to every `/15` document the driver writes."""

    from tests import test_tree_campaign_family14_run as driver

    real = driver.family14_document

    def document(*args, **kwargs):
        built = real(*args, **kwargs)
        if strings is not None:
            built["verbatim_answers"] = {"version": "verbatim_answers/1", "strings": list(strings)}
        return built

    monkeypatch.setattr(driver, "family14_document", document)


def quoting_reflector(text: str) -> str:
    """A Reflector whose one diagnosis group quotes `text` in its root cause."""

    group = {**DIAGNOSIS["groups"][0], "root_cause": f"c1 should get the reply {text!r}"}
    return (
        "print(" + repr(json.dumps({"status": "reflected", "note": "n", "groups": [group]})) + ")\n"
    )


def canned_editor(monkeypatch, tmp_path, text=CANNED):
    """Every Editor call appends a line quoting `text` (by default the canned
    answer) to the skill, besides `edited`."""

    from tests import campaign_drivers as fixtures
    from tests.tree_evolve_support import EDITOR_SCRIPT

    script = EDITOR_SCRIPT.replace('" edited\\n"', f'" edited\\nanswer exactly {text!r}\\n"')
    assert script != EDITOR_SCRIPT
    workspace = fixtures.write_workspace
    monkeypatch.setattr(
        fixtures,
        "write_workspace",
        lambda *a, **k: workspace(*a, **{**k, "editor_script": script}),
    )


def mismatches(run):
    return inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]


async def test_a_declared_canned_answer_is_ordinary_text_for_every_role(tmp_path, monkeypatch):
    declare(monkeypatch, [CANNED])
    canned_editor(monkeypatch, tmp_path)
    run, _ = await family14_run(tmp_path, monkeypatch, reflector=quoting_reflector(CANNED), **RUN)
    assert not payloads(run, "reflection_failed")
    assert len(payloads(run, "attempt_opened")) == 2
    assert not [p for p in payloads(run, "attempt_closed") if p["reason"] == "seal_refused"]
    assert [p["attempt_id"] for p in payloads(run, "attempt_sealed")] == ["r01-a01", "r01-a02"]
    frozen = {p["attempt_id"]: p for p in payloads(run, "attempt_input_frozen")}
    (row,) = frozen["r01-a02"]["feedback"]["attempts"]
    assert row["changes_status"] == "available" and CANNED in row["diff_excerpt"]
    assert mismatches(run) == []


async def test_without_the_declaration_the_quote_is_still_a_leak(tmp_path, monkeypatch):
    declare(monkeypatch, None)
    run, _ = await family14_run(tmp_path, monkeypatch, reflector=quoting_reflector(CANNED), **RUN)
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "oracle_leak"
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["unplanned"]
    assert not payloads(run, "attempt_opened")


async def test_without_the_declaration_the_canned_line_is_refused_at_the_seal(
    tmp_path, monkeypatch
):
    declare(monkeypatch, None)
    canned_editor(monkeypatch, tmp_path)
    run, _ = await family14_run(tmp_path, monkeypatch, **RUN)
    assert {(p["reason"], p["code"]) for p in payloads(run, "attempt_closed")} == {
        ("seal_refused", "oracle_leak")
    }
    # The refused needle text is never journaled: no record (the second
    # Attempt's feedback included) holds the undeclared string that matched.
    assert CANNED not in (run / "campaign.jsonl").read_text(encoding="utf-8")


async def test_an_undeclared_gold_fact_from_the_scorer_diagnostics_is_still_a_leak(
    tmp_path, monkeypatch
):
    declare(monkeypatch, [CANNED])
    # `scorer` is a `_two_case_run` keyword: `family14_run` passes it through.
    run, _ = await family14_run(
        tmp_path, monkeypatch, reflector=quoting_reflector(GOLD), scorer=SCORER, **RUN
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "oracle_leak"


async def test_a_longer_gold_value_that_contains_the_canned_answer_stays_protected(
    tmp_path, monkeypatch
):
    declare(monkeypatch, [CANNED])
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        reflector=quoting_reflector(f"{CANNED} 45"),
        **{**RUN, "extra_cases": {"c3": {**ORACLE, "gold": f"{CANNED} 45"}}},
    )
    (failed,) = payloads(run, "reflection_failed")
    assert failed["error_class"] == "oracle_leak"


async def test_a_candidate_writing_the_longer_gold_value_is_refused_at_the_seal(
    tmp_path, monkeypatch
):
    """Review Focus 4, Candidate side: the canned part alone seals (the first
    test above); the whole gold value that contains it does not."""

    declare(monkeypatch, [CANNED])
    canned_editor(monkeypatch, tmp_path, text=f"{CANNED} 45")
    run, _ = await family14_run(
        tmp_path, monkeypatch, **{**RUN, "extra_cases": {"c3": {**ORACLE, "gold": f"{CANNED} 45"}}}
    )
    assert {(p["reason"], p["code"]) for p in payloads(run, "attempt_closed")} == {
        ("seal_refused", "oracle_leak")
    }
    assert not payloads(run, "attempt_sealed")


@pytest.mark.parametrize(
    "before,after",
    [(None, [CANNED]), ([ALT], [CANNED]), ([CANNED], None)],
    ids=["add", "change", "remove"],
)
async def test_a_changed_declaration_is_a_rebase_that_keeps_the_open_round(
    tmp_path, monkeypatch, before, after
):
    from evolution.tree.evolve import EvolveRefused

    # Every Editor call of this run writes the canned line, so the seal after
    # the rebase shows which needle set is in force (Review Focus 5): under
    # `before` alone the line seals only in `remove`, under `after` only in
    # `add` and `change`. The cut comes before the first Editor call.
    canned_editor(monkeypatch, tmp_path)
    with monkeypatch.context() as patch:
        declare(patch, before)
        _cut_hook(patch, "after_attempt_input_frozen")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **RUN)
    run = tmp_path / "run"
    (frozen,) = payloads(run, "attempt_input_frozen")
    journal = run / "campaign.jsonl"
    cut = journal.read_bytes()
    with monkeypatch.context() as patch:
        declare(patch, after)
        with pytest.raises(EvolveRefused) as caught:
            await family14_run(tmp_path, patch, **RUN)
        refused = caught.value
        # ADR-0015: a refusal writes nothing.
        assert journal.read_bytes() == cut
        assert (refused.code, refused.effects) == ("run_identity_changed", ("rebase",))
        assert [d.field for d in refused.differences] == ["identity.verbatim_answers"]
        accepted = _decision(refused, None, reason="accept the declared answers")
        run, snapshot = await family14_run(tmp_path, patch, authorizations=[accepted], **RUN)
    assert snapshot.finished is not None
    # ADR-0015: the accepted rebase is journaled before anything else.
    assert journal.read_bytes().startswith(cut)
    appended = [line for line in journal.read_bytes()[len(cut) :].splitlines() if line]
    assert json.loads(appended[0])["event"] == "identity_rebased"
    (rebased,) = payloads(run, "identity_rebased")
    assert rebased["changed_fields"] == ["identity.verbatim_answers"]
    assert ("verbatim_answers" in rebased["identity"]) is (after is not None)
    # The open round resumed: `run_editor` recomputed the brief and `D` and
    # matched the frozen common input, or it would have raised `frozen editor
    # common input changed`. (`editor_started.common_input_digest` is copied
    # from the frozen record, so it is not evidence and is not compared.)
    assert [p["attempt_id"] for p in payloads(run, "editor_started")][0] == frozen["attempt_id"]
    # Needle checks after the rebase use the new set (ADR-0038).
    closes = {p["attempt_id"]: (p["reason"], p["code"]) for p in payloads(run, "attempt_closed")}
    sealed = {p["attempt_id"] for p in payloads(run, "attempt_sealed")}
    if after is None:
        assert closes[frozen["attempt_id"]] == ("seal_refused", "oracle_leak")
    else:
        assert frozen["attempt_id"] in sealed
    assert mismatches(run) == []


@pytest.mark.parametrize(
    "before,after", [([CANNED], None), (None, [CANNED])], ids=["predecessor", "successor"]
)
async def test_a_14_to_14_link_admits_a_declaration_on_one_side_only(
    tmp_path, monkeypatch, before, after
):
    """A link compares only comparable fields (ADR-0038): a declaration that only
    the predecessor or only the successor makes is admitted, is no condition
    change, and leaves both runs clean."""

    from tests.test_tree_campaign_family14_link_run import (
        assert_clean,
        edit_document,
        link_of,
        predecessor14,
        successor14,
    )

    def declaring(strings):
        def edit(document):
            if strings is not None:
                document["verbatim_answers"] = {
                    "version": "verbatim_answers/1",
                    "strings": list(strings),
                }

        return edit

    cases = {"c3": ORACLE}
    with monkeypatch.context() as patch:
        edit_document(patch, declaring(before))
        predecessor = await predecessor14(tmp_path, patch, extra_cases=cases)
    with monkeypatch.context() as patch:
        edit_document(patch, declaring(after))
        run, snapshot = await successor14(tmp_path, patch, predecessor, extra_cases=cases)
    for directory, strings in ((predecessor, before), (run, after)):
        (created,) = payloads(directory, "campaign_created")
        assert ("verbatim_answers" in created["identity"]) is (strings is not None)
    link = link_of(run)
    assert link["predecessor_family"] == 14
    assert "condition_changed" not in link
    assert mismatches(predecessor) == []
    assert_clean(run, snapshot)


def test_the_declaration_is_not_a_comparable_field():
    """Neither a link nor a resume compares the declaration (ADR-0038): a
    difference in it is never a condition change."""

    from evolution.tree.campaign import _COMPARABLE
    from evolution.tree.execution.authorizations import (
        COMPARABLE_FIELDS,
        RefusalDifference,
        comparable_changed,
    )

    assert "verbatim_answers" not in COMPARABLE_FIELDS and "verbatim_answers" not in _COMPARABLE
    assert comparable_field("identity.verbatim_answers", family_version=14) is False
    difference = RefusalDifference("identity.verbatim_answers", None, "a" * 64)
    assert comparable_changed([difference], family_version=14) is False
