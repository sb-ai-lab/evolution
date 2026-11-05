"""b16 spec §11.1: a round's grouping stays bound across a Grouper `rebase`.

Every run is a genuine engine invocation of the shared two-case fixture, in
family 14 (`/15`) and family 15 (`/16`). The scorer never lets a Candidate
pass, so no round promotes, the baseline binding stays, and round 2 reuses
round 1's grouping (the reuse path: no `grouping_started` of its own). A run
is cut by raising `_PowerCut` from a campaign hook, after the record the hook
follows is durable, and is resumed with the `fields` Grouper's first feature
description changed: that changes the adaptation identity and so the policy
digest every group and cohort id is a digest over. The first resume is
refused `run_identity_changed` with the one effect `rebase`; the second
carries that authorization.

A round that has a finished grouping of its own, or a selected portion, keeps
the grouping that produced it and that grouping's policy digest; only an
unbound round looks for a grouping under the new digest.
"""

from __future__ import annotations

import copy
import json
import shutil
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import TWO_CASE_SCORER, _records, audit_mismatches, two_case_run
from tests.test_tree_campaign_family14_visibility_change import _decision

pytestmark = pytest.mark.slow

FAMILIES = pytest.mark.parametrize("family", [14, 15], ids=["family14", "family15"])

#: c1 fails on every tree, so nothing promotes and round 2 reuses round 1's grouping.
NEVER_PASSES = TWO_CASE_SCORER.replace(
    'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
    'passed = bool(payload["oracle"].get("pass"))',
)

#: The `fields` Grouper's first feature description after the change.
CHANGED_DESCRIPTION = "The criteria that failed."


class _PowerCut(BaseException):
    pass


async def _run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: int,
    *,
    cut: tuple[str, int] | None = None,
    changed: bool = False,
    authorizations: tuple[Any, ...] = (),
    rounds: int = 2,
):
    """One engine invocation; reusing `root` resumes the run under `root / "run"`.

    `cut=(hook, n)` raises `_PowerCut` after the `n`-th call of `hook`;
    `changed` declares the changed Grouper feature description.
    """

    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import evolve
    from tests import campaign_drivers as fixtures
    from tests.family15_fakes import v16_document

    results: list[Any] = []
    original_hook = _Campaign.hook
    calls: Counter[str] = Counter()
    digests: list[str] = []

    async def configured(path, *args, **kwargs):
        document = json.loads(path.read_text())
        if family == 15:
            document["schema"] = "evolution-v2-manifest/16"
            document["campaign"]["gate"] = copy.deepcopy(v16_document()["campaign"]["gate"])
        if changed:
            document["grouper"]["features"][0]["description"] = CHANGED_DESCRIPTION
        path.write_text(json.dumps(document))
        result = await evolve(path, *args, **kwargs)
        results.append(result)
        return result

    def hook(self, name):
        original_hook(self, name)
        digests.append(self.adaptive.policy_digest)
        calls[name] += 1
        if cut is not None and (name, calls[name]) == cut:
            raise _PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", configured)
        patch.setattr(_Campaign, "hook", hook)
        await two_case_run(
            root,
            terminal=None,
            scorer=NEVER_PASSES,
            authorizations=authorizations,
            campaign={"attempts_per_round": 1, "rounds": rounds},
        )
    _CURRENT_DIGEST[root] = digests[-1]
    return root / "run", results[-1]


#: Each root's grouping policy digest in force in its latest invocation (the
#: journal records only the adaptation identity's digest, not its payload).
_CURRENT_DIGEST: dict[Path, str] = {}


async def _cut_then_rebase(
    root: Path, monkeypatch: pytest.MonkeyPatch, family: int, cut: tuple[str, int], **options: Any
):
    """Cut the run at `cut`, then resume it once with the Grouper changed under `rebase`."""

    with pytest.raises(_PowerCut):
        await _run(root, monkeypatch, family, cut=cut, **options)
    before = _journal(root / "run")
    with pytest.raises(EvolveRefused) as caught:
        await _run(root, monkeypatch, family, changed=True, **options)
    refusal = caught.value
    assert refusal.code == "run_identity_changed" and refusal.effects == ("rebase",)
    assert [difference.field for difference in refusal.differences] == ["adaptation"]
    decision = _decision(refusal, None, "accept the changed Grouper")
    run, result = await _run(
        root, monkeypatch, family, changed=True, authorizations=(decision,), **options
    )
    return run, result, before


def _journal(run: Path) -> list[dict[str, Any]]:
    return _records(run / "campaign.jsonl")


def _of(records: list[dict[str, Any]], event: str, **match: Any) -> list[dict[str, Any]]:
    return [
        record["payload"]
        for record in records
        if record["event"] == event
        and all(record["payload"].get(key) == value for key, value in match.items())
    ]


def _old_and_new_digests(run: Path) -> tuple[str, str]:
    """The policy digest before the rebase (round 1's grouping) and the rebased one."""

    records = _journal(run)
    (first, *_) = _of(records, "grouping_started", round=1)
    old = first["policy_digest"]
    (rebased,) = _of(records, "identity_rebased")
    assert rebased["changed_fields"] == ["adaptation"]
    return old, _CURRENT_DIGEST[run.parent]


def _assert_finished(run: Path, result: Any) -> None:
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert audit_mismatches(run) == []


def _spy_admissions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, tuple, tuple]]:
    """Record each re-admission: `(round, policy identity, group ids, cohort ids)`."""

    seen: list[tuple[int, str, tuple, tuple]] = []
    real = campaign_module._Campaign._admitted_grouping

    def spy(self, round_number):
        grouped = real(self, round_number)
        if grouped is not None:
            cohorts = self.focus_cohorts(round_number, grouped)
            seen.append(
                (
                    round_number,
                    grouped.policy_identity,
                    tuple(sorted(entry.group_id for entry in grouped.neighborhoods)),
                    tuple(sorted(cohort.cohort_id for cohort in cohorts)),
                )
            )
        return grouped

    monkeypatch.setattr(campaign_module._Campaign, "_admitted_grouping", spy)
    return seen


# -- a round cut after its own grouping finished ----------------------------------


@FAMILIES
async def test_round_cut_after_own_grouping_finished_selects_portion(tmp_path, monkeypatch, family):
    """The resume writes the round's own grouping's policy digest into
    `focus_portion_selected`, and the lifecycle accepts it."""

    run, result, before = await _cut_then_rebase(
        tmp_path, monkeypatch, family, ("after_grouping_finished", 1)
    )
    _assert_finished(run, result)
    old, new = _old_and_new_digests(run)
    assert old != new
    records = _journal(run)
    assert len(_of(records, "grouping_started", round=1)) == 1
    (grouping,) = _of(records, "grouping_finished", round=1)
    (portion,) = _of(records, "focus_portion_selected", round=1)
    assert portion["policy_digest"] == old
    assert portion["grouping_digest"] == grouping["result_digest"]
    assert portion["reason"] == "selected"


# -- a reuse-path round cut after its portion was selected --------------------------


@FAMILIES
async def test_reuse_round_cut_after_portion_selected_reflects(tmp_path, monkeypatch, family):
    """Reflection and the Attempt input find the grouping the portion names."""

    run, result, before = await _cut_then_rebase(
        tmp_path, monkeypatch, family, ("after_focus_portion_selected", 2)
    )
    _assert_finished(run, result)
    old, _new = _old_and_new_digests(run)
    records = _journal(run)
    assert _of(records, "grouping_started", round=2) == []
    (portion,) = _of(records, "focus_portion_selected", round=2)
    assert [portion] == _of(before, "focus_portion_selected", round=2)
    assert portion["policy_digest"] == old
    assert _of(records, "reflection_finished", round=2)
    assert _of(records, "attempt_input_frozen", round=2)
    assert _of(records, "editor_finished", attempt_id="r02-a01")


# -- bound rounds keep their ids across later steps ---------------------------------

#: kind: (the round, the hook call that cuts it).
KINDS = {"own": 1, "reuse": 2}


async def _assert_ids_kept(tmp_path, monkeypatch, family, kind, hook):
    number = KINDS[kind]
    seen = _spy_admissions(monkeypatch)
    run, result, before = await _cut_then_rebase(tmp_path, monkeypatch, family, (hook, number))
    _assert_finished(run, result)
    old, new = _old_and_new_digests(run)
    records = _journal(run)
    rebased_at = next(
        index for index, record in enumerate(records) if record["event"] == "identity_rebased"
    )
    admissions = [entry for entry in seen if entry[0] == number]
    assert admissions, "the round re-admitted its grouping"
    (portion,) = _of(records, "focus_portion_selected", round=number)
    assert {entry[1] for entry in admissions} == {old}
    assert {entry[2] for entry in admissions} == {admissions[0][2]}
    assert {entry[3] for entry in admissions} == {admissions[0][3]}
    assert set(portion["cohort_ids"]) <= set(admissions[0][3])
    assert portion["neighborhood_id"] in admissions[0][2]
    # Every record of the round after the rebase was resolved from the bound grouping.
    assert all(
        record["payload"].get("round") != number or record["event"] != "grouping_started"
        for record in records[rebased_at:]
    )
    return run, records, before


@FAMILIES
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_bound_round_keeps_ids_after_reflection_started(tmp_path, monkeypatch, family, kind):
    number = KINDS[kind]
    run, records, before = await _assert_ids_kept(
        tmp_path, monkeypatch, family, kind, "after_reflection_started"
    )
    (first,) = _of(before, "reflection_started", round=number)
    retried = _of(records, "reflection_started", round=number)
    assert len(retried) == 2
    # The interrupted reflection is retried on the same brief.
    assert retried[1]["input_digest"] == first["input_digest"]
    assert _of(records, "reflection_finished", round=number)


@FAMILIES
@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_bound_round_keeps_ids_after_attempt_input_frozen(
    tmp_path, monkeypatch, family, kind
):
    number = KINDS[kind]
    run, records, before = await _assert_ids_kept(
        tmp_path, monkeypatch, family, kind, "after_attempt_input_frozen"
    )
    attempt_id = f"r{number:02d}-a01"
    (frozen,) = _of(before, "attempt_input_frozen", attempt_id=attempt_id)
    assert _of(records, "attempt_input_frozen", attempt_id=attempt_id) == [frozen]
    (started,) = _of(records, "editor_started", attempt_id=attempt_id)
    assert started["common_input_digest"] == frozen["common_input_digest"]
    assert started["input_digest"] == frozen["input_digest"]


# -- unbound rounds use the current identity ----------------------------------------


@FAMILIES
async def test_unbound_reuse_round_groups_again(tmp_path, monkeypatch, family):
    """Round 2, cut before it selected a portion, finds no grouping under the
    new digest and groups its baseline again."""

    run, result, _ = await _cut_then_rebase(
        tmp_path, monkeypatch, family, ("after_round_closed", 1)
    )
    _assert_finished(run, result)
    old, new = _old_and_new_digests(run)
    records = _journal(run)
    (started,) = _of(records, "grouping_started", round=2)
    assert started["policy_digest"] == new
    (round1,) = _of(records, "grouping_started", round=1)
    assert started["baseline_binding"] == round1["baseline_binding"]
    (portion,) = _of(records, "focus_portion_selected", round=2)
    assert portion["policy_digest"] == new
    # Visitation counts new-digest portions only: round 1's does not count.
    assert portion["cycle"] == 1


@FAMILIES
async def test_next_round_groups_under_new_digest_and_visits_new_cycle(
    tmp_path, monkeypatch, family
):
    """Round 1, cut after its portion was selected, keeps its grouping through
    the rebase; round 2 groups the same baseline again under the new digest,
    and its visitation starts a new cycle; round 3 reuses round 2's grouping."""

    run, result, _ = await _cut_then_rebase(
        tmp_path, monkeypatch, family, ("after_focus_portion_selected", 1), rounds=3
    )
    _assert_finished(run, result)
    old, new = _old_and_new_digests(run)
    records = _journal(run)
    assert [p["policy_digest"] for p in _of(records, "grouping_started")] == [old, new]
    portions = _of(records, "focus_portion_selected")
    assert [p["policy_digest"] for p in portions] == [old, new, new]
    assert [p["cycle"] for p in portions] == [1, 1, 2]
    assert _of(records, "grouping_started", round=3) == []


# -- an unchanged family-14 resume keeps the b15 writer's bytes ---------------------


async def test_family14_resume_without_grouper_change_keeps_bytes(tmp_path, monkeypatch):
    """Review Focus 5: a family-14 run cut after its grouping finished and
    resumed without a Grouper change writes the same `focus_portion_selected`
    bytes as the b15 writer, which resolved the grouping and its digest from
    the current identity alone. The b15 path is reproduced by resuming a copy
    of the cut run with `bound_grouping` resolving nothing."""

    cut = tmp_path / "cut"
    cut.mkdir()
    with pytest.raises(_PowerCut):
        await _run(cut, monkeypatch, 14, cut=("after_grouping_finished", 1))
    b15 = tmp_path / "b15"
    shutil.copytree(cut, b15, symlinks=True)

    resolve_nothing: Callable[..., None] = lambda *_args, **_kwargs: None  # noqa: E731
    with monkeypatch.context() as patch:
        patch.setattr(campaign_module, "bound_grouping", resolve_nothing)
        b15_run, b15_result = await _run(b15, patch, 14)
    run, result = await _run(cut, monkeypatch, 14)
    _assert_finished(run, result)
    assert b15_result.terminal == "finished"

    def portions(root: Path) -> list[bytes]:
        return [
            canonical_json(record["payload"])
            for record in _records(root / "campaign.jsonl")
            if record["event"] == "focus_portion_selected"
        ]

    ours, theirs = portions(run), portions(b15_run)
    assert len(ours) == 2
    assert ours == theirs


# -- the lifecycle checks a portion against the round's own grouping ----------------


async def test_lifecycle_checks_an_own_grouping_rounds_portion_against_it(tmp_path, monkeypatch):
    """A round with its own finished grouping selects from that grouping: a
    portion naming another policy digest is refused, and the recorded one is
    accepted."""

    from evolution.tree.campaign_schema import SchemaError
    from tests.campaign_fakes import Family14Life

    run, result = await _run(tmp_path, monkeypatch, 14, rounds=1)
    _assert_finished(run, result)
    records = _journal(run)
    index = next(
        at for at, record in enumerate(records) if record["event"] == "focus_portion_selected"
    )
    prefix, portion = records[:index], dict(records[index]["payload"])
    with pytest.raises(SchemaError, match="portion names another grouping"):
        Family14Life(prefix).accept(
            "focus_portion_selected", **{**portion, "policy_digest": "0" * 64}
        )
    life = Family14Life(prefix)
    life.accept("focus_portion_selected", **portion)
    assert life.snapshot.rounds[1].focus_portion is not None
