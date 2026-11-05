"""Synthetic framing fixtures and production pack builders for offline tests."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from evolution.tree.execution.scorer_trace_writer import PackedTraceWriter
from evolution.tree.execution.scorer_traces import (
    PACK_FORMAT,
    ScorerTraceRecord,
    encode_pack_record,
    read_scorer_trace,
)

STDOUT_A = b'{"critical_pass":true,"metrics":{"score":1.0}}\n'
STDOUT_B = b'{"critical_pass":false,"metrics":{"score":0.0}}\n'
STDERR_B = b"judge: slow reply\n"
ID_A = "3c0bb7c82c594a558c1cdb9bd89023f7"
ID_B = "56c8995c5d98475996aac1762db605c7"
#: The §12 worked example of the design, byte for byte (2476 bytes).
EXAMPLE_SHA256 = "024d82b772f31a296d59d35da3d35591e77d71cc2b4abecbdb2b6357bdab5658"


def request_bytes(request_id: str, case_id: str) -> bytes:
    """A request document serialized as `atomic_write_private_json` writes it."""

    payload: dict[str, Any] = {
        "case_id": case_id,
        "public": {"text": "hi"},
        "oracle": {"pass": True},
        "view": {"history": {"turns": []}, "terminal": "completed"},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    document = {
        "scorer": "89" * 32,
        "trace_id": request_id,
        "input": payload,
        "input_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


def pack_record(pack_id: str, segment: int = 0) -> bytes:
    return encode_pack_record(
        {"format": PACK_FORMAT, "kind": "pack", "pack_id": pack_id, "segment": segment}
    )


def request_record(request_id: str, body: bytes) -> bytes:
    return encode_pack_record(
        {"format": PACK_FORMAT, "kind": "request", "score_request_id": request_id},
        [("request", body)],
    )


def output_record(
    request_id: str,
    stdout: bytes,
    stderr: bytes = b"",
    *,
    status: str = "completed",
    returncode: int | None = 0,
) -> bytes:
    return encode_pack_record(
        {
            "format": PACK_FORMAT,
            "kind": "output",
            "score_request_id": request_id,
            "status": status,
            "returncode": returncode,
        },
        [("stdout", stdout), ("stderr", stderr)],
    )


def example_pack() -> bytes:
    """The design's §12 example: two tries of `m-r01-a02`."""

    return b"".join(
        (
            pack_record("m-r01-a02"),
            request_record(ID_A, request_bytes(ID_A, "c1")),
            request_record(ID_B, request_bytes(ID_B, "c2")),
            output_record(ID_A, STDOUT_A),
            output_record(ID_B, STDOUT_B, STDERR_B),
        )
    )


def write_private(path: Path, data: bytes) -> Path:
    """Write `data` owner-only, creating owner-only parents."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return path


def write_pack(trace_root: Path, name: str, records: Iterable[bytes]) -> Path:
    return write_private(trace_root / name, b"".join(records))


def write_production_pack(
    trace_root: Path, pack_id: str, records: Iterable[ScorerTraceRecord]
) -> None:
    """Reproduce ephemeral evidence through the real writer, preserving raw sections.

    Directory fixtures have no process status; their complete streams represent
    successful scoring. Never use this helper to rewrite retained run evidence.
    """
    with PackedTraceWriter(trace_root, pack_id) as writer:
        for record in records:
            assert record.request is not None
            writer.append_request(record.score_request_id, record.request)
            if record.stdout is not None or record.stderr is not None:
                with writer.streams() as (stdout, stderr):
                    stdout.write(record.stdout or b"")
                    stderr.write(record.stderr or b"")
                    writer.append_output(
                        record.score_request_id,
                        stdout,
                        stderr,
                        status=record.status or "completed",
                        returncode=record.returncode if record.status else 0,
                    )


def rewrite_production_trace(
    run: Path, request_id: str, *, remove: bool = False, **sections: bytes
) -> None:
    """Edit one try in an ephemeral copied measurement pack with valid framing."""
    target = read_scorer_trace(run, request_id)
    assert target is not None and target.layout == "pack"
    pack_id = Path(target.location).stem
    assert target.location == f"scorer-traces/{pack_id}.pack"
    journal = run / "measurements" / pack_id / "events.jsonl"
    records = []
    for event in map(json.loads, journal.read_text().splitlines()):
        if event["event"] != "scoring_started":
            continue
        record = read_scorer_trace(
            run, event["payload"]["score_request_id"], measurement_id=event["run_id"]
        )
        assert record is not None and record.location == target.location
        if record.score_request_id == request_id:
            if remove:
                continue
            record = replace(record, **sections)
        records.append(record)
    (run / target.location).unlink()
    write_production_pack(run / "scorer-traces", pack_id, records)


def write_directory_trace(
    trace_root: Path,
    request_id: str,
    *,
    request: bytes | None,
    stdout: bytes | None = None,
    stderr: bytes | None = None,
) -> Path:
    """One directory-layout try; a `None` file is not written."""

    directory = trace_root / "scorer_traces" / request_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in (("request.json", request), ("stdout", stdout), ("stderr", stderr)):
        if data is not None:
            write_private(directory / name, data)
    return directory
