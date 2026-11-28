import stat

import pytest

from evolution_lab.projects.probes import ProbeStore


@pytest.mark.parametrize("durability,journal,synchronous", [
    (None, "wal", 1), ("strict", "delete", 2), ("relaxed", "wal", 1),
])
def test_probe_durability(tmp_path, durability, journal, synchronous, monkeypatch):
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    kwargs = {} if durability is None else {"durability": durability}
    store = ProbeStore.open(tmp_path, **kwargs)
    try:
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == journal
        assert store._db.execute("PRAGMA synchronous").fetchone()[0] == synchronous
    finally:
        store.close()


def test_probe_strict_restores_delete_after_relaxed(tmp_path):
    store = ProbeStore.open(tmp_path, durability="relaxed")
    _session(store, "a" * 32)
    store.close()
    store = ProbeStore.open(tmp_path, durability="strict")
    try:
        assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert store.session("a" * 32)["state"] == "open"
        assert not (tmp_path / "probes.sqlite3-wal").exists()
        assert not (tmp_path / "probes.sqlite3-shm").exists()
    finally:
        store.close()


def _session(store, id, reset_of=None):
    store.create_session({"id": id, "project": "p", "implementation": "i", "reset_of": reset_of,
                          "created_at": "2026-10-02T00:00:00Z", "record": {"ceiling": {"max_turns": 5}}})


def _done(store, sid, n, tokens=10, known=True, cost=None, cost_known=True):
    store.begin_turn(sid, n, "q", "t0")
    store.finish_turn(sid, n, {"turn_id": f"{sid}/{n}"}, tokens=tokens, tokens_known=known, cost_units=cost,
                      cost_known=cost_known)


def test_file_mode_and_version(tmp_path):
    store = ProbeStore.open(tmp_path)
    path = tmp_path / "probes.sqlite3"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 1
    store.close()


def test_chain_usage_sums_reset_chain_from_either_end(tmp_path):
    store = ProbeStore.open(tmp_path)
    _session(store, "a" * 32)
    _session(store, "b" * 32, reset_of="a" * 32)
    _session(store, "c" * 32)
    _done(store, "a" * 32, 1, tokens=10, cost=5)
    _done(store, "b" * 32, 1, tokens=20, cost=7)
    _done(store, "c" * 32, 1, tokens=999, cost=999)
    want = {"turns": 2, "tokens": 30, "tokens_incomplete": False, "cost_units": 12, "cost_unavailable": False}
    assert store.chain_usage("b" * 32) == want
    assert store.chain_usage("a" * 32) == want
    store.close()


def test_chain_usage_flags_unknown_tokens_and_cost(tmp_path):
    store = ProbeStore.open(tmp_path)
    _session(store, "a" * 32)
    _done(store, "a" * 32, 1, tokens=0, known=False, cost=None, cost_known=False)
    usage = store.chain_usage("a" * 32)
    assert usage["tokens_incomplete"] is True and usage["cost_unavailable"] is True
    store.close()


def test_pending_and_mark_interrupted(tmp_path):
    store = ProbeStore.open(tmp_path)
    sid = "a" * 32
    _session(store, sid)
    store.begin_turn(sid, 1, "question", "t0")
    assert store.pending_turns() == [(sid, 1)]
    store.mark_interrupted(sid, 1, "t1")
    assert store.pending_turns() == []
    [rec] = store.turns(sid)
    assert rec["failure"] == {"code": "interrupted"} and rec["agent_outcome"] == "infrastructure"
    assert rec["request"] == {"content": "question"} and rec["started_at"] == "t0" and rec["finished_at"] == "t1"
    usage = store.chain_usage(sid)
    assert usage["turns"] == 1 and usage["tokens_incomplete"] is True and usage["cost_unavailable"] is True
    store.close()


def test_reopen_reads_same_data_and_open_sessions(tmp_path):
    store = ProbeStore.open(tmp_path)
    _session(store, "a" * 32)
    _session(store, "b" * 32)
    _done(store, "a" * 32, 1)
    store.close_session("b" * 32, "client", "t9")
    store.close()
    again = ProbeStore.open(tmp_path)
    assert [s["id"] for s in again.open_sessions()] == ["a" * 32]
    closed = again.session("b" * 32)
    assert closed["state"] == "closed" and closed["closed_reason"] == "client" and closed["closed_at"] == "t9"
    assert again.session("a" * 32)["record"] == {"ceiling": {"max_turns": 5}}
    assert again.session("f" * 32) is None
    assert again.turns("a" * 32) == [{"turn_id": "a" * 32 + "/1"}]
    assert [s["id"] for s in again.sessions("p")] == ["a" * 32, "b" * 32]
    assert again.sessions("p", "other") == [] and again.sessions("q") == []
    again.close()


def test_three_member_chain_sums_from_any_member(tmp_path):
    store = ProbeStore.open(tmp_path)
    a, b, c = "a" * 32, "b" * 32, "c" * 32
    _session(store, a)
    _session(store, b, reset_of=a)
    _session(store, c, reset_of=b)
    _done(store, a, 1, tokens=1, cost=1)
    _done(store, b, 1, tokens=2, cost=2)
    _done(store, c, 1, tokens=4, cost=4)
    want = {"turns": 3, "tokens": 7, "tokens_incomplete": False, "cost_units": 7, "cost_unavailable": False}
    assert [store.chain_usage(x) for x in (a, b, c)] == [want] * 3
    store.close()


def test_pending_turn_counts_in_turns_with_unknown_spend(tmp_path):
    """PDR-0019: a pending turn's tokens and cost are unknown until finish_turn writes them."""
    store = ProbeStore.open(tmp_path)
    _session(store, "a" * 32)
    _done(store, "a" * 32, 1, tokens=10, cost=3)
    store.begin_turn("a" * 32, 2, "q", "t0")
    assert store.chain_usage("a" * 32) == {"turns": 2, "tokens": 10, "tokens_incomplete": True,
                                           "cost_units": 3, "cost_unavailable": True}
    store.close()


def test_repeated_close_and_finish_are_noops(tmp_path):
    store = ProbeStore.open(tmp_path)
    sid = "a" * 32
    _session(store, sid)
    store.close_session(sid, "client", "t1")
    store.close_session(sid, "reset", "t2")
    s = store.session(sid)
    assert s["closed_reason"] == "client" and s["closed_at"] == "t1"
    _done(store, sid, 1, tokens=10)
    store.finish_turn(sid, 1, {"turn_id": "other"}, tokens=99, tokens_known=False, cost_units=5, cost_known=False)
    assert store.turns(sid) == [{"turn_id": sid + "/1"}]
    assert store.chain_usage(sid) == {"turns": 1, "tokens": 10, "tokens_incomplete": False,
                                      "cost_units": 0, "cost_unavailable": False}
    store.close()


def test_finish_turn_writes_the_chain_ceiling_after_the_turn_in_one_transaction(tmp_path):
    store = ProbeStore.open(tmp_path)
    sid = "a" * 32
    _session(store, sid)
    _done(store, sid, 1, tokens=10)
    store.begin_turn(sid, 2, "q", "t1")
    seen = []

    def ceiling(used):
        seen.append(used)
        return {"turns": {"used": used["turns"], "max": 5}}
    stored = store.finish_turn(sid, 2, {"turn_id": sid + "/2"}, tokens=5, tokens_known=True, cost_units=None,
                               cost_known=True, ceiling=ceiling)
    assert seen == [{"turns": 2, "tokens": 15, "tokens_incomplete": False, "cost_units": 0,
                     "cost_unavailable": False}]  # the usage after this turn
    assert stored == {"turn_id": sid + "/2", "ceiling": {"turns": {"used": 2, "max": 5}}}
    assert store.turns(sid)[-1] == stored
    # A failure while building or writing the ceiling rolls the whole completion back: the turn stays pending.
    store.begin_turn(sid, 3, "q", "t2")

    def broken(used):
        raise RuntimeError("disk")
    with pytest.raises(RuntimeError):
        store.finish_turn(sid, 3, {"turn_id": sid + "/3"}, tokens=1, tokens_known=True, cost_units=None,
                          cost_known=True, ceiling=broken)
    assert store.pending_turns() == [(sid, 3)] and len(store.turns(sid)) == 2
    assert store.chain_usage(sid)["tokens_incomplete"] is True
    store.close()
