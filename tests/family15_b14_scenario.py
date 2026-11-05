"""Deterministic Single fake, based on a frozen offline native /15 prefix."""

from tests.family15_fakes import Family15Life

BASE = "c405ce1e1e46ed44315e202d476dfc45e651f784"


def single_journal(seed):
    """Re-emit /16 creation, gate, reuse final, retained round and completed run."""
    life = Family15Life(seed)
    assert "stages" not in life.snapshot.identity["policy"]["gate"]
    life.finish_final(accepted=False)
    life.select()
    life.close_round()
    life.accept(
        "campaign_finished",
        outcome="completed",
        reason="rounds_exhausted",
        continuation=dict(kind="linked_run", reason="rounds_exhausted"),
        evidence=None,
    )
    assert life.snapshot.finished is not None
    return b"".join(envelope.encode() for envelope in life.envelopes)
