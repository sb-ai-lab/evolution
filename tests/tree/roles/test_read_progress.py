"""Observable /16 receipt, coverage, and compaction budgets."""

import hashlib
import json

import pytest

from evolution.tree.execution.check_modes import repeated_read_check
from evolution.tree.roles.read_progress import ReadProgress


def evidence(reads, offset, content, *, version="v1", total=15, reread=False, source="E1"):
    reads.begin_turn()
    return reads.deliver(
        "read_evidence",
        {"evidence_id": source, "offset": offset},
        # A clipped page must advance by its delivered content, even if the
        # source advertised another next_offset before clipping.
        {
            "offset": offset,
            "content": content,
            "content_digest": version,
            "total_characters": total,
            "next_offset": None,
        },
        reread=reread,
    )


def test_coverage_survives_receipt_eviction_without_skipping_an_unseen_gap():
    reads = ReadProgress("finish_reflection", max_chars=1, message_chars=100)
    assert evidence(reads, 10, "abcde")["next_offset"] == 0
    assert reads.offset("E1") == 0
    first = evidence(reads, 0, "12345")
    assert first["next_offset"] == reads.offset("E1") == 5
    evidence(reads, 5, "67890")
    assert reads.completed("E1") and reads.offset("E1") == 15
    # The one-character metadata budget evicted the first page, so text is delivered.
    assert evidence(reads, 0, "12345")["content"] == "12345"
    assert reads.summary(1) == ""
    assert evidence(reads, 0, "12345", version="v2")["content"] == "12345"
    assert reads.offset("E1") == 5


@pytest.mark.parametrize("exhausted", [False, True])
def test_alternating_physical_ends_repeat_until_a_page_adds_coverage(exhausted):
    reads = ReadProgress("finish_reflection", max_chars=1000, message_chars=100)
    if exhausted:
        for source in ("E1", "E2"):
            evidence(reads, 0, "first", source=source, total=5)

    for source, reread, repeated in (
        ("E1", False, False),
        ("E2", False, False),
        ("E1", True, True),
    ):
        receipt = evidence(reads, 5, "", total=5, source=source, reread=reread)
        assert receipt["source"] == source and receipt["source_version"] == "v1"
        assert receipt["status"] == ("eof" if exhausted else "gap")
        assert {"content", "offset", "end_offset"}.isdisjoint(receipt)
        if exhausted:
            assert "next_offset" not in receipt
        else:
            assert receipt["next_offset"] == 0
            assert not reads.completed(source)
        assert bool(reads.repair_message(repeated_read_check())) is repeated

    # The gap cursor and a newly delivered version both restart repeat detection.
    page = evidence(
        reads,
        reads.offset("E1") if not exhausted else 0,
        "first",
        total=5,
        version="v2" if exhausted else "v1",
    )
    assert page["content"] == "first"
    assert reads.completed("E1")
    evidence(reads, 5, "", source="E2", total=5)
    assert reads.repair_message(repeated_read_check()) is None


def test_quotations_repeat_even_when_receipts_are_evicted():
    reads = ReadProgress("finish_edit", max_chars=1, message_chars=100)
    check = repeated_read_check()
    evidence(reads, 0, "12345")
    assert reads.repair_message(check) is None
    quote = evidence(reads, 0, "12345", reread=True)
    assert quote["content"] == "12345"
    assert reads.repair_message(check) is None
    assert reads.summary(1) == ""
    reads.begin_turn()
    malformed = reads.deliver(
        "read_evidence",
        {"evidence_id": "E1", "offset": -1},
        {"error": "invalid offset"},
        reread=False,
    )
    assert "error" in malformed and reads.offset("E1") == 5
    assert reads.repair_message(check) is None
    # A different range contains only positions previously delivered.
    evidence(reads, 1, "2345")
    assert reads.repair_message(check) is None
    evidence(reads, 0, "12345", reread=True)
    assert "repeated_read_without_progress" in reads.repair_message(check)
    # An overlapping range with one new character clears the repeated action.
    evidence(reads, 1, "23456")
    assert reads.repair_message(check) is None
    assert reads.offset("E1") == 6
    evidence(reads, 0, "12345", reread=True)
    assert reads.repair_message(check) is None


def test_receipt_uses_sanitized_utf8_fragment_and_short_summary_keeps_the_page():
    reads = ReadProgress(
        "finish_edit", max_chars=10_000, message_chars=12_000, redactions=("passphrase",)
    )
    output = {
        "path": "example.md",
        "sha256": "v",
        "content": "жук <think>hidden</think> passphrase\n",
        "start_line": 1,
        "end_line": 1,
        "total_lines": 2,
    }

    def read():
        reads.begin_turn()
        return reads.deliver("read_file", {}, output, reread=False)

    assert read()["content"] == output["content"]
    assert json.loads(reads.summary(24)) == {"pages": [], "omitted": 1}
    receipt = read()
    assert receipt["source_version"] == "v" and receipt["next_line"] == 2
    assert receipt["status"] == "already_read"
    assert (
        receipt["fragment_sha256"]
        == hashlib.sha256("жук [REDACTED] [REDACTED]\n".encode()).hexdigest()
    )
    for text in (receipt["excerpt"], reads.summary(10_000)):
        assert "hidden" not in text and "passphrase" not in text
    check = repeated_read_check()
    assert reads.repair_message(check) is None
    read()
    assert reads.repair_message(check) is not None
    reads.begin_turn()  # another tool intervened
    read()
    assert reads.repair_message(check) is not None
