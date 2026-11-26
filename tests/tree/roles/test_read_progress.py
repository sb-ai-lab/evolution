"""Observable /16 receipt, coverage, and compaction budgets."""

import hashlib
import json

from evolution.tree.roles.read_progress import ReadProgress


def evidence(reads, offset, content, *, version="v1", total=15, reread=False):
    reads.begin_turn()
    return reads.deliver(
        "read_evidence",
        {"evidence_id": "E1", "offset": offset},
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
        omitted_offset=False,
    )


def test_coverage_survives_receipt_eviction_without_skipping_an_unseen_gap():
    reads = ReadProgress("finish_reflection", max_chars=1, message_chars=100)
    evidence(reads, 10, "abcde")
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
        return reads.deliver("read_file", {}, output, reread=False, omitted_offset=True)

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
    assert not reads.repeated_without_progress()
    read()
    assert reads.repeated_without_progress()
    reads.begin_turn()  # another tool intervened
    read()
    assert not reads.repeated_without_progress()
