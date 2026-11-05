"""H7: the family-14 baseline refresh (PDR-0022 / F0 R42)."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.tree.campaign import visitation_chain_start
from evolution.tree.campaign_epochs import (
    RefreshDue,
    family14_next_baseline,
    fresh_baseline_id,
    refresh_count,
    refresh_due,
)
from evolution.tree.campaign_identity import (
    IdentityError,
    final_measurement_id_for,
    measurement_id_for,
    refresh_measurement_id_for,
)
from evolution.tree.campaign_schema import (
    CampaignSnapshot,
    SchemaError,
    campaign_family_for,
)

# --------------------------------------------------------------------------
# Pure rule tests (hand-built round states)
# --------------------------------------------------------------------------

CHAMPION0 = measurement_id_for(None)


@dataclass(frozen=True)
class _Round:
    baseline_measurement_id: str | None
    closed: dict[str, Any] | None = None
    selection: dict[str, Any] | None = None


def _closed(baseline: str, outcome: str = "stalled", *, selected: str | None = None) -> _Round:
    return _Round(
        baseline,
        {"outcome": outcome, "reason": "x"},
        None if selected is None else {"attempt_id": None, "measurement_id": selected},
    )


def _next(rounds, number, *, first_round=1, rebaseline_from_round=None, epoch=0):
    return family14_next_baseline(
        rounds,
        first_round=first_round,
        rebaseline_from_round=rebaseline_from_round,
        epoch=epoch,
        round_number=number,
    )


def _count(rounds, number, *, first_round=1, rebaseline_from_round=None, epoch=0):
    nxt = _next(
        rounds,
        number,
        first_round=first_round,
        rebaseline_from_round=rebaseline_from_round,
        epoch=epoch,
    )
    return refresh_count(rounds, first_round=first_round, round_number=number, next_baseline=nxt)


def _due(rounds, number, n, *, first_round=1, rebaseline_from_round=None, epoch=0):
    return refresh_due(
        rounds,
        first_round=first_round,
        rebaseline_from_round=rebaseline_from_round,
        epoch=epoch,
        round_number=number,
        after_rounds=n,
    )


def test_next_baseline_first_rebaseline_and_carry():
    rounds = {1: _closed(CHAMPION0)}
    assert _next(rounds, 1) == CHAMPION0
    assert _next(rounds, 2) == CHAMPION0
    assert _next(rounds, 2, rebaseline_from_round=2, epoch=1) == fresh_baseline_id(2, 1)
    assert _next({4: _closed(CHAMPION0)}, 4, first_round=4) == CHAMPION0


def test_next_baseline_follows_selection_of_promoted_or_retained():
    win = final_measurement_id_for(1, 1, 1)
    keep = final_measurement_id_for(2, 1, None)
    rounds = {
        1: _closed(CHAMPION0, "promoted", selected=win),
        2: _closed(win, "retained", selected=keep),
        3: _closed(keep, "stalled"),
    }
    assert _next(rounds, 2) == win
    assert _next(rounds, 3) == keep
    assert _next(rounds, 4) == keep


@pytest.mark.parametrize("outcome", ["stalled", "clean", "unplanned", "unscored", "unmeasurable"])
def test_count_counts_non_moving_closures(outcome):
    rounds = {1: _closed(CHAMPION0, outcome), 2: _closed(CHAMPION0, outcome)}
    assert _count(rounds, 3) == 2
    assert _count(rounds, 2) == 1


def test_count_stable_retained_counts_on_the_stored_id():
    keep = final_measurement_id_for(1, 1, None)
    rounds = {1: _closed(CHAMPION0, "retained", selected=keep)}
    # round 2 binds `keep`; round 1 ran on CHAMPION0, so it is not counted.
    assert _count(rounds, 2) == 0
    rounds[2] = _closed(keep, "retained", selected=keep)
    rounds[3] = _closed(keep, "retained", selected=keep)
    assert _count(rounds, 3) == 1
    assert _count(rounds, 4) == 2


def test_open_or_suspended_round_does_not_count():
    rounds = {1: _closed(CHAMPION0), 2: _Round(CHAMPION0)}
    assert _count(rounds, 3) == 1


def test_resets_on_promotion_unstable_retention_and_first_round():
    win = final_measurement_id_for(2, 1, 1)
    fresh = final_measurement_id_for(2, 1, None)
    rounds = {1: _closed(CHAMPION0), 2: _closed(CHAMPION0, "promoted", selected=win)}
    assert _count(rounds, 3) == 0
    rounds = {1: _closed(CHAMPION0), 2: _closed(CHAMPION0, "retained", selected=fresh)}
    assert _count(rounds, 3) == 0
    assert _count({}, 1) == 0
    assert _count({}, 4, first_round=4) == 0


def test_resets_on_refresh_and_counts_on_the_refresh_id():
    refresh = refresh_measurement_id_for(3)
    rounds = {
        1: _closed(CHAMPION0),
        2: _closed(CHAMPION0),
        3: _closed(refresh),
        4: _closed(refresh),
    }
    assert _count(rounds, 3) == 2  # the refresh round's own count is not yet its own
    assert _next(rounds, 5) == refresh
    assert _count(rounds, 5) == 2


def test_resets_at_the_rebaseline_round_and_for_a_linked_successor():
    rounds = {1: _closed(CHAMPION0), 2: _closed(CHAMPION0)}
    assert _count(rounds, 3, rebaseline_from_round=3, epoch=1) == 0
    # A linked successor never sees the predecessor's rounds.
    assert _count({}, 4, first_round=4) == 0
    assert _count({4: _closed(CHAMPION0)}, 5, first_round=4) == 1


def test_epoch_ids_normalise_and_continue_keeps_the_count():
    refresh = refresh_measurement_id_for(3)
    rounds = {
        1: _closed(CHAMPION0),
        2: _closed(CHAMPION0 + "@e2"),
        3: _closed(refresh),
        4: _closed(refresh + "@e1"),
    }
    assert _count(rounds, 3, epoch=2) == 2
    assert _count(rounds, 5, epoch=2) == 2
    assert _next(rounds, 5, epoch=1) == refresh + "@e1"


def test_refresh_due_is_off_for_none_zero_and_below_n():
    rounds = {1: _closed(CHAMPION0), 2: _closed(CHAMPION0)}
    assert _due(rounds, 3, None) is None
    assert _due(rounds, 3, 0) is None
    assert _due(rounds, 3, 3) is None


@pytest.mark.parametrize(("n", "number"), [(1, 2), (3, 4), (3, 5)])
def test_refresh_due_at_n_names_the_contiguous_range(n, number):
    rounds = {k: _closed(CHAMPION0) for k in range(1, number)}
    due = _due(rounds, number, n)
    assert due == RefreshDue(
        round=number, replaced=CHAMPION0, first=number - n, last=number - 1, after_rounds=n
    )
    # The counted rounds are exactly [first, last].
    base = _next(rounds, number)
    counted = [
        k
        for k in rounds
        if rounds[k].closed is not None and rounds[k].baseline_measurement_id == base
    ]
    assert due is not None
    assert counted[-n:] == list(range(due.first, due.last + 1))


def test_refresh_due_replaced_is_the_current_id_with_epoch():
    rounds = {1: _closed(CHAMPION0), 2: _closed(CHAMPION0 + "@e1")}
    due = _due(rounds, 3, 2, epoch=1)
    assert due is not None and due.replaced == CHAMPION0 + "@e1"


def test_refresh_due_is_not_repeated_right_after_a_refresh():
    refresh = refresh_measurement_id_for(3)
    rounds = {1: _closed(CHAMPION0), 2: _closed(CHAMPION0), 3: _closed(refresh)}
    assert _due(rounds, 4, 2) is None
    rounds[4] = _closed(refresh)
    due = _due(rounds, 5, 2)
    assert due is not None and (due.first, due.last, due.replaced) == (3, 4, refresh)


def test_refresh_measurement_id():
    assert refresh_measurement_id_for(4) == "m-r04-refresh"
    assert refresh_measurement_id_for(12) == "m-r12-refresh"
    for bad in (0, -1, True, False, None, "3"):
        with pytest.raises(IdentityError, match="invalid_refresh_measurement_round"):
            refresh_measurement_id_for(bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Visitation: a refresh is neither a Promotion nor a rebaseline
# --------------------------------------------------------------------------


def _visit_snapshot(closed):
    return SimpleNamespace(
        first_round=1,
        rebaseline_from_round=None,
        rounds={
            number: SimpleNamespace(closed={"outcome": outcome, "reason": "x"})
            for number, outcome in closed.items()
        },
    )


def test_visitation_chain_start_is_unchanged_across_a_refresh_round():
    closed = {1: "stalled", 2: "stalled", 3: "retained", 4: "stalled"}
    snapshot = _visit_snapshot(closed)
    assert visitation_chain_start(snapshot, 3) == 1  # round 3 starts on a refresh
    assert visitation_chain_start(snapshot, 5) == 1


# --------------------------------------------------------------------------
# Grammar: the two family-14 refresh records
# --------------------------------------------------------------------------

_SHA = "a" * 64
_RUN = "run-1"


def _ref(kind: str, locator: str, digest: str = _SHA) -> dict[str, Any]:
    return {
        "version": 1,
        "kind": kind,
        "run_id": _RUN,
        "locator": locator,
        "content_digest": digest,
    }


def _bound() -> dict[str, Any]:
    return {
        "round": 4,
        "measurement_id": refresh_measurement_id_for(4),
        "baseline_refresh_after_rounds": 2,
        "counted_rounds": {"first": 2, "last": 3},
        "replaced": {"measurement_id": "m-r00-champion", "prefix_digest": _SHA},
        "evidence": [
            _ref("journal_prefix", "measurements/m-r00-champion/events.jsonl#bytes=0:100"),
            _ref("journal_record", "campaign.jsonl#bytes=10:20"),
        ],
    }


def _completed(measurement_id: str | None = None) -> dict[str, Any]:
    measurement_id = measurement_id or refresh_measurement_id_for(4)
    return {
        "round": 4,
        "measurement_id": measurement_id,
        "evidence": [
            _ref("journal_prefix", f"measurements/{measurement_id}/events.jsonl#bytes=0:50")
        ],
    }


def _validate(family: int, event: str, payload: dict[str, Any]) -> None:
    campaign_family_for(family).validate_payload(event, payload)


def test_family14_admits_both_refresh_records():
    _validate(14, "baseline_refresh_bound", _bound())
    _validate(14, "baseline_refresh_completed", _completed())
    _validate(14, "baseline_refresh_completed", _completed("m-r04-refresh@e2"))


def _mutated(base, edit):
    payload = base()
    edit(payload)
    return payload


_BAD_BOUND = {
    "missing_key": (lambda p: p.pop("replaced"), "invalid journal record"),
    "extra_key": (lambda p: p.update(extra=1), "invalid journal record"),
    "round_zero": (lambda p: p.update(round=0), "round is not a positive integer"),
    "round_bool": (lambda p: p.update(round=True), "round is not a positive integer"),
    "after_zero": (
        lambda p: p.update(baseline_refresh_after_rounds=0),
        "baseline_refresh_after_rounds is not a positive integer",
    ),
    "after_negative": (
        lambda p: p.update(baseline_refresh_after_rounds=-1),
        "baseline_refresh_after_rounds is not a positive integer",
    ),
    "first_after_last": (
        lambda p: p.update(counted_rounds={"first": 4, "last": 3}),
        "counted rounds are inverted",
    ),
    "counted_extra": (lambda p: p["counted_rounds"].update(extra=1), "invalid journal record"),
    "counted_missing": (lambda p: p["counted_rounds"].pop("first"), "invalid journal record"),
    "counted_zero": (
        lambda p: p.update(counted_rounds={"first": 0, "last": 3}),
        "counted first round is not a positive integer",
    ),
    "bad_digest": (
        lambda p: p["replaced"].update(prefix_digest="xyz"),
        "value is not a sha-256 digest",
    ),
    "replaced_extra": (lambda p: p["replaced"].update(extra=1), "invalid journal record"),
    "wrong_measurement": (
        lambda p: p.update(measurement_id="m-r05-refresh"),
        "refresh measurement ID is not the round's canonical refresh ID",
    ),
    "epoch_measurement": (
        lambda p: p.update(measurement_id="m-r04-refresh@e1"),
        "refresh measurement ID is not the round's canonical refresh ID",
    ),
    "one_evidence": (
        lambda p: p["evidence"].pop(),
        "refresh evidence cites the replaced prefix and one closure",
    ),
    "three_evidence": (
        lambda p: p["evidence"].append(p["evidence"][0]),
        "refresh evidence cites the replaced prefix and one closure",
    ),
    "swapped_kinds": (
        lambda p: p["evidence"].reverse(),
        "refresh evidence matches the replaced prefix identity",
    ),
    "prefix_other_measurement": (
        lambda p: p["evidence"][0].update(
            locator="measurements/m-r09-champion/events.jsonl#bytes=0:100"
        ),
        "refresh evidence matches the replaced prefix identity",
    ),
    "prefix_digest_mismatch": (
        lambda p: p["evidence"][0].update(content_digest="b" * 64),
        "refresh evidence matches the replaced prefix identity",
    ),
    "bad_reference": (lambda p: p["evidence"][1].update(version=2), "reference version is 1"),
    "over_cap": (
        lambda p: p["replaced"].update(measurement_id="m" * 3000),
        "final record string exceeds the byte budget",
    ),
}


@pytest.mark.parametrize("name", sorted(_BAD_BOUND))
def test_refresh_bound_refuses_malformed_payloads(name):
    edit, rule = _BAD_BOUND[name]
    with pytest.raises(SchemaError, match=f"^{re.escape(rule)}$"):
        _validate(14, "baseline_refresh_bound", _mutated(_bound, edit))


_BAD_COMPLETED = {
    "missing_key": (lambda p: p.pop("evidence"), "invalid journal record"),
    "extra_key": (lambda p: p.update(extra=1), "invalid journal record"),
    "round_zero": (lambda p: p.update(round=0), "round is not a positive integer"),
    "wrong_base": (
        lambda p: p.update(measurement_id="m-r05-refresh"),
        "refresh measurement ID is not the round's refresh ID or its epoch rebind",
    ),
    "champion_id": (
        lambda p: p.update(measurement_id="m-r04-champion"),
        "refresh measurement ID is not the round's refresh ID or its epoch rebind",
    ),
    "noncanonical_epoch": (
        lambda p: p.update(measurement_id="m-r04-refresh@e0"),
        "refresh measurement ID is not the round's refresh ID or its epoch rebind",
    ),
    "empty_evidence": (lambda p: p.update(evidence=[]), "evidence must be a non-empty list"),
    "two_evidence": (
        lambda p: p["evidence"].append(p["evidence"][0]),
        "refresh completion cites one baseline prefix",
    ),
    "record_kind": (
        lambda p: p["evidence"][0].update(kind="journal_record"),
        "refresh completion cites the refresh measurement's prefix",
    ),
    "prefix_other_measurement": (
        lambda p: p["evidence"][0].update(
            locator="measurements/m-r09-champion/events.jsonl#bytes=0:100"
        ),
        "refresh completion cites the refresh measurement's prefix",
    ),
    "over_cap": (
        lambda p: p.update(measurement_id="m-r04-refresh@e" + "9" * 3000),
        "final record string exceeds the byte budget",
    ),
}


@pytest.mark.parametrize("name", sorted(_BAD_COMPLETED))
def test_refresh_completed_refuses_malformed_payloads(name):
    edit, rule = _BAD_COMPLETED[name]
    with pytest.raises(SchemaError, match=f"^{re.escape(rule)}$"):
        _validate(14, "baseline_refresh_completed", _mutated(_completed, edit))


def test_snapshot_refreshes_defaults_to_empty():
    field = CampaignSnapshot.__dataclass_fields__["refreshes"]
    assert field.default == ()


# --------------------------------------------------------------------------
# Lifecycle: admission, order and supersession (F0 R43)
#
# Every prefix below is genuine: a real family-14 engine run (`family14_run`)
# on a no-finalist configuration, cut at a round's close. The engine writes no
# refresh record before the H7 executor (Task 5), so every record appended
# after the prefix is a *synthetic lifecycle input* built from the snapshot
# and the prefix's real records -- never evidence of what the engine writes.
# --------------------------------------------------------------------------

_PREFIXES: dict[tuple[int, int], list[dict[str, Any]]] = {}


async def _genuine_prefix(tmp_path, monkeypatch, *, refresh: int, closes: int):
    """The campaign records of a real stalled run cut after its `closes`-th close.

    Cached per worker: the prefix is a pure function of the configuration.
    """
    from tests.campaign_drivers import STATISTICAL, WORSE_SCORER, _cut_hook, _records
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run

    key = (refresh, closes)
    if key not in _PREFIXES:
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_round_closed", call=closes)
            with pytest.raises(Crash):
                await family14_run(
                    tmp_path,
                    monkeypatch,
                    {**STATISTICAL, "scorer": WORSE_SCORER},
                    family14_settings={"refresh": refresh},
                    campaign={"rounds": closes + 2},
                )
        records = _records(tmp_path / "run" / "campaign.jsonl")
        assert records[-1]["event"] == "round_closed"
        closed = [r["payload"]["outcome"] for r in records if r["event"] == "round_closed"]
        assert closed == ["stalled"] * closes
        policy = records[0]["payload"]["identity"]["policy"]
        assert policy["baseline_refresh_after_rounds"] == refresh
        _PREFIXES[key] = records
    return copy.deepcopy(_PREFIXES[key])


def _life(records):
    from tests.campaign_fakes import Family14Life

    return Family14Life(records)


@pytest.fixture
async def due_life(tmp_path, monkeypatch):
    """N = 1, round 1 closed `stalled`: round 2's refresh is due."""
    return _life(await _genuine_prefix(tmp_path, monkeypatch, refresh=1, closes=1))


@pytest.fixture
async def two_records(tmp_path, monkeypatch):
    """N = 2, rounds 1 and 2 closed `stalled`: round 3's refresh is due."""
    return await _genuine_prefix(tmp_path, monkeypatch, refresh=2, closes=2)


@pytest.fixture
async def off_life(tmp_path, monkeypatch):
    """N = 0, round 1 closed `stalled`: the policy refreshes nothing."""
    return _life(await _genuine_prefix(tmp_path, monkeypatch, refresh=0, closes=1))


def _first_close(records):
    """The genuine prefix up to and including round 1's `round_closed`."""
    end = next(i for i, r in enumerate(records) if r["event"] == "round_closed")
    return records[: end + 1]


def _exact(rule: str) -> str:
    return f"^{re.escape(rule)}$"


def refresh_bound_payload(life, number: int, **overrides: Any) -> dict[str, Any]:
    """Synthetic lifecycle input: the `baseline_refresh_bound` of round `number`.

    Derived from the snapshot by plain arithmetic (not by `refresh_due`, which
    the lifecycle itself calls): N is the policy's, the counted rounds are the
    N rounds before `number`, the replaced measurement is the previous round's
    baseline with its real `baseline_reference`, and the closure is the
    previous round's genuine `round_closed`.
    """
    snapshot = life.snapshot
    after = life.state.policy.baseline_refresh_after_rounds or 1
    previous = snapshot.rounds[number - 1]
    payload: dict[str, Any] = dict(
        round=number,
        measurement_id=refresh_measurement_id_for(number),
        baseline_refresh_after_rounds=after,
        counted_rounds={"first": max(1, number - after), "last": number - 1},
        replaced={
            "measurement_id": previous.baseline_measurement_id,
            "prefix_digest": previous.baseline_prefix_digest,
        },
    )
    if "evidence" not in overrides:
        payload["evidence"] = [
            dict(previous.baseline_reference),
            life.reference(life.sequence_of("round_closed", round=number - 1)),
        ]
    payload.update(overrides)
    return payload


def _genuine(life, event: str, number: int = 1) -> dict[str, Any]:
    """A genuine record's payload of round `number` (the first one found)."""
    for envelope in life.envelopes:
        if envelope.event == event and envelope.payload.get("round") == number:
            return dict(envelope.payload)
    raise LookupError(event)


def _baseline_bound(life, number: int, measurement_id: str) -> dict[str, Any]:
    return {**_genuine(life, "baseline_bound"), "round": number, "measurement_id": measurement_id}


def _champion_bound(life, number: int, measurement_id: str) -> dict[str, Any]:
    bound = next(
        dict(e.payload)
        for e in life.envelopes
        if e.event == "measurement_bound" and e.payload["attempt_id"] == "champion"
    )
    return {**bound, "round": number, "measurement_id": measurement_id}


def _stages(life, measurement_id: str) -> None:
    """The champion baseline's genuine stage plan, replayed on `measurement_id`."""
    for envelope in list(life.envelopes):
        if envelope.event == "stage_opened" and envelope.payload["measurement_id"] == CHAMPION0:
            life.accept("stage_opened", **{**envelope.payload, "measurement_id": measurement_id})


def _prefix(measurement_id: str, end: int = 4096, digest: str = "e" * 64) -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "journal_prefix",
        "run_id": measurement_id,
        "locator": f"measurements/{measurement_id}/events.jsonl#bytes=0:{end}",
        "content_digest": digest,
    }


def _complete(life, number: int, measurement_id: str) -> dict[str, Any]:
    reference = _prefix(measurement_id)
    life.accept(
        "baseline_complete", round=number, measurement_id=measurement_id, evidence=[reference]
    )
    return reference


def _grouping(life, number: int, binding: str = "e" * 64) -> dict[str, Any]:
    started = _genuine(life, "grouping_started")
    return {
        **started,
        "round": number,
        "baseline_binding": binding,
        "grouping_id": f"r{number:02d}-g-refresh",
    }


def _measure_refresh(life, number: int = 2) -> str:
    """Bind the refresh and its baseline, then bind and stage the measurement."""
    mid = refresh_measurement_id_for(number)
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, number))
    life.accept("baseline_bound", **_baseline_bound(life, number, mid))
    life.accept("measurement_bound", **_champion_bound(life, number, mid))
    _stages(life, mid)
    return mid


@pytest.mark.slow
async def test_lifecycle_admits_the_refresh_order(due_life):
    life = due_life
    mid = _measure_refresh(life)
    entry = dict(life.snapshot.refreshes[0])
    assert entry["completed"] is None
    assert entry["measurement_id"] == mid
    assert life.snapshot.rounds[2].baseline_measurement_id == mid
    reference = _complete(life, 2, mid)
    completion = life.accept(
        "baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference]
    )
    life.accept("grouping_started", **_grouping(life, 2))
    (entry,) = life.snapshot.refreshes
    bound_seq = life.sequence_of("baseline_refresh_bound", round=2)
    assert dict(entry) == {
        "round": 2,
        "measurement_id": mid,
        "baseline_refresh_after_rounds": 1,
        "counted_rounds": {"first": 1, "last": 1},
        "replaced": {
            "measurement_id": "m-r00-champion",
            "prefix_digest": life.snapshot.rounds[1].baseline_prefix_digest,
        },
        "seq": bound_seq,
        "completed": {"measurement_id": mid, "seq": completion},
    }
    # The refresh replaces the stored reference: the round's baseline is its own.
    assert dict(life.snapshot.rounds[2].baseline_reference) == reference
    assert life.snapshot.measurements[mid]["attempt_id"] == "champion"


@pytest.mark.slow
async def test_snapshot_refreshes_are_immutable_copies(due_life):
    life = due_life
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 2))
    (entry,) = life.snapshot.refreshes
    with pytest.raises(TypeError):
        entry["round"] = 3  # type: ignore[index]
    with pytest.raises(TypeError):
        entry["replaced"]["measurement_id"] = "x"  # type: ignore[index]


@pytest.mark.slow
async def test_a_due_round_refuses_the_ordinary_baseline(due_life):
    life = due_life
    ordinary = _baseline_bound(life, 2, "m-r00-champion")
    with pytest.raises(
        SchemaError, match=_exact("a due refresh is bound before the round's baseline")
    ):
        life.accept("baseline_bound", **ordinary)
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 2))
    with pytest.raises(
        SchemaError, match=_exact("a refreshed round binds its refresh measurement")
    ):
        life.accept("baseline_bound", **ordinary)


@pytest.mark.slow
async def test_the_refresh_measurement_needs_its_refresh_entry(due_life):
    life = due_life
    with pytest.raises(
        SchemaError, match=_exact("the refresh measurement needs its baseline_refresh_bound")
    ):
        life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r02-refresh"))


@pytest.mark.slow
async def test_a_refresh_that_is_not_due_is_refused(two_records):
    life = _life(_first_close(two_records))  # N = 2, one counted round
    with pytest.raises(SchemaError, match=_exact("no refresh is due for the round")):
        life.accept(
            "baseline_refresh_bound",
            **refresh_bound_payload(life, 2, counted_rounds={"first": 1, "last": 1}),
        )
    with pytest.raises(
        SchemaError, match=_exact("the refresh measurement needs its baseline_refresh_bound")
    ):
        life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r02-refresh"))
    life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r00-champion"))
    assert life.snapshot.refreshes == ()


@pytest.mark.slow
async def test_n_rounds_on_one_measurement_make_the_refresh_due(two_records):
    life = _life(two_records)
    assert [r.baseline_measurement_id for r in life.snapshot.rounds.values()] == [
        "m-r00-champion",
        "m-r00-champion",
    ]
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 3))
    assert dict(life.snapshot.refreshes[0])["counted_rounds"] == {"first": 1, "last": 2}


@pytest.mark.slow
@pytest.mark.parametrize(
    "counted",
    [{"first": 2, "last": 2}, {"first": 1, "last": 1}, {"first": 1, "last": 3}],
    ids=["forged_span", "forged_last", "forged_beyond"],
)
async def test_a_forged_counted_range_is_refused(two_records, counted):
    life = _life(two_records)
    with pytest.raises(
        SchemaError, match=_exact("the refresh's counted rounds are not the derived count")
    ):
        life.accept(
            "baseline_refresh_bound", **refresh_bound_payload(life, 3, counted_rounds=counted)
        )


@pytest.mark.slow
async def test_policy_without_refresh_refuses_every_refresh_record(off_life):
    life = off_life
    payload = refresh_bound_payload(life, 2)
    with pytest.raises(SchemaError, match=_exact("the policy refreshes no baseline")):
        life.accept("baseline_refresh_bound", **payload)
    with pytest.raises(SchemaError, match=_exact("the policy refreshes no baseline")):
        life.accept(
            "baseline_refresh_completed",
            round=2,
            measurement_id="m-r02-refresh",
            evidence=[_prefix("m-r02-refresh")],
        )
    # The ordinary next baseline stays the rule, and only it.
    with pytest.raises(
        SchemaError, match=_exact("the refresh measurement needs its baseline_refresh_bound")
    ):
        life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r02-refresh"))
    life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r00-champion"))


@pytest.mark.slow
async def test_family14_round_binds_only_the_derived_next_baseline(off_life):
    """`family14_next_baseline`, now the lifecycle's: a stalled round's successor
    continues its baseline, not another champion-digest measurement."""
    life = off_life
    other = "m-r01-a01-parent"  # measures the Champion's tree too
    assert life.snapshot.measurements[other]["candidate_digest"] == life.snapshot.champion[1]
    with pytest.raises(
        SchemaError, match=_exact("a family-14 round's baseline is the derived next baseline")
    ):
        life.accept("baseline_bound", **_baseline_bound(life, 2, other))


def _swap_replaced(life, payload, measurement_id):
    """Forge `replaced` consistently with its evidence, so the grammar admits it."""
    digest = "f" * 64
    payload["replaced"] = {"measurement_id": measurement_id, "prefix_digest": digest}
    payload["evidence"][0] = _prefix(measurement_id, digest=digest)


_FORGED = {
    "after_rounds": (
        lambda life, p: p.update(baseline_refresh_after_rounds=2),
        "the refresh names the policy's baseline_refresh_after_rounds",
    ),
    "counted_last": (
        lambda life, p: p.update(counted_rounds={"first": 1, "last": 2}),
        "the refresh's counted rounds are not the derived count",
    ),
    "replaced_id": (
        lambda life, p: _swap_replaced(life, p, "m-r01-a01-parent"),
        "the refresh replaces the round's derived baseline",
    ),
    "replaced_digest": (
        lambda life, p: _swap_replaced(life, p, "m-r00-champion"),
        "the refresh names the replaced baseline's prefix digest",
    ),
    "evidence_prefix": (
        lambda life, p: p["evidence"][0].update(
            locator=p["evidence"][0]["locator"].rsplit(":", 1)[0] + ":1"
        ),
        "the refresh cites the replaced baseline's reference",
    ),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(_FORGED))
async def test_a_forged_refresh_binding_is_refused(due_life, name):
    life = due_life
    edit, rule = _FORGED[name]
    payload = refresh_bound_payload(life, 2)
    edit(life, payload)
    with pytest.raises(SchemaError, match=_exact(rule)):
        life.accept("baseline_refresh_bound", **payload)
    assert life.snapshot.refreshes == ()


@pytest.mark.slow
async def test_a_refresh_is_bound_once_per_round(due_life):
    life = due_life
    payload = refresh_bound_payload(life, 2)
    life.accept("baseline_refresh_bound", **payload)
    with pytest.raises(SchemaError, match=_exact("the round already has a refresh")):
        life.accept("baseline_refresh_bound", **payload)


@pytest.mark.slow
async def test_a_refresh_for_a_round_with_a_baseline_is_refused(due_life):
    life = due_life
    payload = refresh_bound_payload(life, 2)
    payload.update(round=1, measurement_id=refresh_measurement_id_for(1))
    with pytest.raises(SchemaError, match=_exact("the round already has a baseline")):
        life.accept("baseline_refresh_bound", **payload)


@pytest.mark.slow
async def test_a_refresh_outside_the_range_is_refused(due_life):
    life = due_life
    beyond = life.state.last_round + 1
    payload = refresh_bound_payload(life, 2)
    payload.update(
        round=beyond,
        measurement_id=refresh_measurement_id_for(beyond),
        counted_rounds={"first": beyond - 1, "last": beyond - 1},
    )
    with pytest.raises(SchemaError, match=_exact("round is outside the campaign's own range")):
        life.accept("baseline_refresh_bound", **payload)


@pytest.mark.slow
async def test_a_refresh_needs_the_previous_round(due_life):
    life = due_life
    payload = refresh_bound_payload(life, 2)
    payload.update(
        round=3,
        measurement_id=refresh_measurement_id_for(3),
        counted_rounds={"first": 2, "last": 2},
    )
    with pytest.raises(SchemaError, match=_exact("the previous round does not exist")):
        life.accept("baseline_refresh_bound", **payload)


@pytest.mark.slow
async def test_the_rebaseline_round_refuses_a_refresh(due_life):
    life = due_life
    payload = refresh_bound_payload(life, 2)
    life.continue_block(effect="rebaseline")
    assert life.snapshot.rebaseline_from_round == 2
    with pytest.raises(SchemaError, match=_exact("the rebaseline round binds a fresh baseline")):
        life.accept("baseline_refresh_bound", **payload)


@pytest.mark.slow
async def test_grouping_waits_for_the_refresh_completion(due_life):
    life = due_life
    mid = _measure_refresh(life)
    reference = _prefix(mid)
    with pytest.raises(SchemaError, match=_exact("the refresh's baseline is not complete")):
        life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    _complete(life, 2, mid)
    with pytest.raises(SchemaError, match=_exact("the round's refresh is not completed")):
        life.accept("grouping_started", **_grouping(life, 2))
    with pytest.raises(
        SchemaError, match=_exact("the refresh completion cites the round's baseline reference")
    ):
        life.accept(
            "baseline_refresh_completed",
            round=2,
            measurement_id=mid,
            evidence=[_prefix(mid, end=1)],
        )
    with pytest.raises(
        SchemaError, match=_exact("the refresh completion names the round's baseline")
    ):
        life.accept(
            "baseline_refresh_completed",
            round=2,
            measurement_id=f"{mid}@e1",
            evidence=[_prefix(f"{mid}@e1")],
        )
    life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    with pytest.raises(SchemaError, match=_exact("the refresh is already completed")):
        life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    life.accept("grouping_started", **_grouping(life, 2))


@pytest.mark.slow
async def test_a_completion_without_a_refresh_is_refused(due_life):
    life = due_life
    with pytest.raises(SchemaError, match=_exact("the round has no refresh")):
        life.accept(
            "baseline_refresh_completed",
            round=2,
            measurement_id="m-r02-refresh",
            evidence=[_prefix("m-r02-refresh")],
        )


@pytest.mark.slow
async def test_continue_between_rounds_keeps_the_count(due_life):
    """PDR-0016: `continue` rebinds no complete measurement and resets nothing."""
    life = due_life
    life.continue_block()
    assert life.snapshot.rebaseline_from_round is None
    with pytest.raises(
        SchemaError, match=_exact("a due refresh is bound before the round's baseline")
    ):
        life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r00-champion"))
    mid = _measure_refresh(life)
    # ADR-0020: a new binding in a later epoch keeps its plain id.
    assert mid == "m-r02-refresh"
    assert life.snapshot.measurements[mid]["epoch"] == 1


@pytest.mark.slow
async def test_a_refresh_rebound_by_continue_completes_under_its_epoch_id(due_life):
    life = due_life
    mid = _measure_refresh(life)
    ((source, rebound),) = life.continue_block([mid])
    assert (source, rebound) == (mid, f"{mid}@e1")
    assert life.snapshot.rounds[2].baseline_measurement_id == rebound
    _stages(life, rebound)
    reference = _complete(life, 2, rebound)
    with pytest.raises(
        SchemaError, match=_exact("the refresh completion names the round's baseline")
    ):
        life.accept(
            "baseline_refresh_completed", round=2, measurement_id=mid, evidence=[_prefix(mid)]
        )
    life.accept("baseline_refresh_completed", round=2, measurement_id=rebound, evidence=[reference])
    life.accept("grouping_started", **_grouping(life, 2))
    assert dict(life.snapshot.refreshes[0]["completed"])["measurement_id"] == rebound


@pytest.mark.slow
async def test_a_rebaseline_before_the_baseline_supersedes_the_refresh(due_life):
    """Plan decision 7: the round becomes the `rebaseline` round, binds the
    fresh id, and the refresh entry stays uncompleted."""
    life = due_life
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 2))
    life.continue_block(effect="rebaseline")
    assert life.snapshot.rebaseline_from_round == 2
    with pytest.raises(SchemaError, match=_exact("the fresh baseline keeps its epoch id")):
        life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r02-refresh"))
    fresh = fresh_baseline_id(2, 1)
    life.accept("baseline_bound", **_baseline_bound(life, 2, fresh))
    life.accept("measurement_bound", **_champion_bound(life, 2, fresh))
    _stages(life, fresh)
    reference = _complete(life, 2, fresh)
    with pytest.raises(
        SchemaError, match=_exact("the round's baseline is not its refresh measurement")
    ):
        life.accept(
            "baseline_refresh_completed",
            round=2,
            measurement_id="m-r02-refresh",
            evidence=[_prefix("m-r02-refresh")],
        )
    life.accept("grouping_started", **_grouping(life, 2))
    (entry,) = life.snapshot.refreshes
    assert entry["completed"] is None
    assert dict(life.snapshot.rounds[2].baseline_reference) == reference


def _close_condition_changed(life, number: int) -> None:
    """The `rebaseline` block's `round_closed(condition_changed)` of a round with no Attempt."""
    version, digest = life.snapshot.champion
    life.accept(
        "round_closed",
        round=number,
        outcome="condition_changed",
        reason=None,
        attempts_opened=0,
        champion_version=version,
        champion_digest=digest,
        evidence=None,
        coverage={
            "selected_cohort_visits": [],
            "claimed_cases": 0,
            "examined_cases": 0,
            "measured_cases": 0,
            "had_assessed_candidate": False,
            "cycle": 1,
            "closure_refs": [],
            "omitted_closure_refs": 0,
        },
    )


@pytest.mark.slow
async def test_a_refresh_round_closed_condition_changed_stays_uncompleted(due_life):
    life = due_life
    mid = _measure_refresh(life)
    life.continue_block(effect="rebaseline")
    assert life.snapshot.pending_change["phase"] == "close_round"
    _close_condition_changed(life, 2)
    (entry,) = life.snapshot.refreshes
    assert entry["completed"] is None
    with pytest.raises(SchemaError, match=_exact("the refresh's baseline is not complete")):
        life.accept(
            "baseline_refresh_completed", round=2, measurement_id=mid, evidence=[_prefix(mid)]
        )
    # The next round is the `rebaseline` round: a fresh baseline, no refresh owed.
    assert life.snapshot.rebaseline_from_round == 3
    fresh = fresh_baseline_id(3, 1)
    life.accept("baseline_bound", **_baseline_bound(life, 3, fresh))


@pytest.mark.slow
async def test_a_completed_refresh_baseline_closed_before_completion_stays_open(due_life):
    """A `rebaseline` between `baseline_complete` and `baseline_refresh_completed`
    closes the round; the refresh is then never completed."""
    life = due_life
    mid = _measure_refresh(life)
    reference = _complete(life, 2, mid)
    life.continue_block(effect="rebaseline")
    _close_condition_changed(life, 2)
    with pytest.raises(SchemaError, match=_exact("the round is already closed")):
        life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    assert life.snapshot.refreshes[0]["completed"] is None


@pytest.mark.slow
async def test_a_refresh_stage_threshold_suspends_with_the_refresh_open(due_life):
    """ADR-0019 / PDR-0015: the refresh is a champion-side baseline stage, so the
    existing `baseline_stage` barrier and window accounting admit the
    suspension with no refresh-specific rule."""
    life = due_life
    mid = _measure_refresh(life)
    owner = "target"
    limits = life.state.policy.indeterminate[owner]
    count = life.snapshot.counts[owner]
    rows = [(case, f"{sample:02d}") for sample in range(1, 4) for case in ("c1", "c2")]
    for case, sample in rows[: limits.max_count + 1 - count]:  # a count crossing exceeds max_count
        count += 1
        life.accept(
            "indeterminate_observed",
            owner=owner,
            reason="target_unstable",
            measurement_id=mid,
            case_id=case,
            sample_id=sample,
            retry_ordinal=0,
            retry_follows=False,
            count=count,
            evidence=[_prefix(mid, end=100)],
        )
    window_count, denominator = life.state._window_totals(owner)
    assert window_count == count == limits.max_count + 1
    life.accept(
        "campaign_suspended",
        reason="target_unstable",
        owner=owner,
        evidence={
            "evidence_kind": "threshold",
            "crossing": {
                "owner": owner,
                "kind": "count",
                "count": window_count,
                "denominator": denominator,
                "max_count": limits.max_count,
                "max_rate": limits.max_rate,
            },
            "window_start": life.snapshot.window_start,
        },
    )
    assert life.snapshot.suspended
    (entry,) = life.snapshot.refreshes
    assert entry["completed"] is None
    assert life.snapshot.rounds[2].baseline_complete is False


@pytest.mark.slow
async def test_a_refresh_excludes_its_exhausted_pairs(due_life):
    """ADR-0016: one `pairs_excluded` before the refresh's `baseline_complete`."""
    from evolution.tree.campaign_schema import pairs_digest

    life = due_life
    mid = _measure_refresh(life)
    last_stage = int(life.snapshot.measurements[mid]["stages"][-1]["stage"])
    pair = dict(case_id="c1", sample_id="01", owner="target", reason="target_unstable", tries=1)
    life.accept(
        "pairs_excluded",
        round=2,
        measurement_id=mid,
        stage=last_stage,
        count=1,
        by_owner={"target": 1},
        pairs_digest=pairs_digest([("c1", "01")]),
        pairs=[pair],
        truncated=False,
        evidence=[_prefix(mid, end=200)],
    )
    assert life.snapshot.exclusions[mid]["count"] == 1
    reference = _complete(life, 2, mid)
    life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    life.accept("grouping_started", **_grouping(life, 2))


@pytest.mark.slow
async def test_a_refresh_needs_the_previous_round_closed(two_records):
    """Genuine prefix cut inside round 2 (its baseline complete, not closed)."""
    end = max(i for i, r in enumerate(two_records) if r["event"] == "round_closed")
    life = _life(two_records[:end])
    assert life.snapshot.rounds[2].closed is None
    previous = life.snapshot.rounds[2]
    payload = refresh_bound_payload(
        life,
        3,
        evidence=[
            dict(previous.baseline_reference),
            life.reference(life.sequence_of("round_closed", round=1)),
        ],
    )
    with pytest.raises(SchemaError, match=_exact("the previous round is not closed")):
        life.accept("baseline_refresh_bound", **payload)


def _no_failing_portion(life, number: int, reference: dict[str, Any]) -> dict[str, Any]:
    """Synthetic: the `focus_portion_selected(reason="none")` the engine writes
    without any grouping when no case fails (`select_focus_portion`)."""
    return {
        **_genuine(life, "focus_portion_selected"),
        "round": number,
        "reason": "none",
        "failing": 0,
        "grouping_digest": None,
        "neighborhood_id": None,
        "cohort_ids": [],
        "case_ids": [],
        "baseline_prefix_digest": reference["content_digest"],
        "baseline_binding": reference["content_digest"],
        "cycle": 1,
        "evidence": [reference],
    }


@pytest.mark.slow
async def test_a_no_failing_focus_portion_waits_for_the_refresh_completion(due_life):
    """The no-failing (and grouping-reuse) path skips `grouping_started`, so the
    focus portion itself is gated."""
    life = due_life
    mid = _measure_refresh(life)
    reference = _complete(life, 2, mid)
    portion = _no_failing_portion(life, 2, reference)
    with pytest.raises(SchemaError, match=_exact("the round's refresh is not completed")):
        life.accept("focus_portion_selected", **portion)
    assert life.snapshot.refreshes[0]["completed"] is None
    life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    life.accept("focus_portion_selected", **portion)


@pytest.mark.slow
async def test_a_refresh_round_cannot_close_before_the_refresh_completion(due_life):
    life = due_life
    mid = _measure_refresh(life)
    reference = _complete(life, 2, mid)
    close = {**_genuine(life, "round_closed"), "round": 2, "attempts_opened": 0}
    with pytest.raises(SchemaError, match=_exact("the round's refresh is not completed")):
        life.accept("round_closed", **close)
    life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    # Past the gate, the existing adaptive rule takes over.
    with pytest.raises(SchemaError, match=_exact("an adaptive round has no focus portion")):
        life.accept("round_closed", **close)


@pytest.mark.slow
async def test_continue_between_refresh_and_baseline_keeps_the_plain_refresh_id(due_life):
    """Synthetic `continue` block on the genuine prefix, between
    `baseline_refresh_bound` and `baseline_bound`: nothing is open to rebind,
    the count is kept, and the round binds plain `m-r02-refresh` (ADR-0020)."""
    life = due_life
    life.accept("baseline_refresh_bound", **refresh_bound_payload(life, 2))
    assert life.continue_block() == []
    assert life.snapshot.rebaseline_from_round is None
    assert len(life.snapshot.epochs) == 2
    with pytest.raises(
        SchemaError, match=_exact("a refreshed round binds its refresh measurement")
    ):
        life.accept("baseline_bound", **_baseline_bound(life, 2, "m-r00-champion"))
    mid = refresh_measurement_id_for(2)
    life.accept("baseline_bound", **_baseline_bound(life, 2, mid))
    life.accept("measurement_bound", **_champion_bound(life, 2, mid))
    assert life.snapshot.measurements[mid]["epoch"] == 1
    _stages(life, mid)
    reference = _complete(life, 2, mid)
    life.accept("baseline_refresh_completed", round=2, measurement_id=mid, evidence=[reference])
    life.accept("grouping_started", **_grouping(life, 2))
    assert dict(life.snapshot.refreshes[0]["completed"])["measurement_id"] == mid
