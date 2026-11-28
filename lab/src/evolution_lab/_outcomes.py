"""Private Lab diagnostics beside core journals; no campaign semantics."""
import json
import os
from pathlib import Path


# Exact b11 admits the complete schedule inside a 1 MiB run_created record
# (journal.MAX_RECORD_BYTES). Each sample contributes at most one unmeasured
# row, retaining its two ids and adding a bounded outcome/try count. Four times
# that record bound covers row annotations, JSON whitespace and Lab envelopes.
MAX_EVALUATION_RESULT_BYTES = 4 * 1024 * 1024


def write_result(path: Path, payload: dict, *, max_bytes: int | None = None) -> None:
    encoded = None
    if max_bytes is not None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError("invalid evaluation result size")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        if encoded is None:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.write("\n")
        else:
            stream.write(encoded)


from contextlib import contextmanager
import asyncio


@contextmanager
def evaluation_outcome(directory: Path, *, min_coverage: float = 1.0):
    result = {"version": "lab-evaluation-result/2", "stopped_reason": None,
              "coverage": None, "unmeasured": None, "min_coverage": min_coverage}
    try:
        yield result
    except BaseException as exc:
        result["stopped_reason"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "exception"
        raise
    finally:
        write_result(Path(directory) / "evaluation-result.json", result,
                     max_bytes=MAX_EVALUATION_RESULT_BYTES)


def validate_coverage_threshold(value):
    # Bounds first: huge integers must not overflow a float conversion.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise ValueError("invalid min_coverage")
    return float(value)


def validate_unmeasured(value):
    if value is None:
        return
    if not isinstance(value, (list, tuple)):
        raise ValueError("invalid unmeasured samples")
    for row in value:
        if not isinstance(row, dict) or set(row) != {"case_id", "sample_id", "outcome", "tries"}:
            raise ValueError("invalid unmeasured sample")
        if any(not isinstance(row[key], str) or not row[key] for key in ("case_id", "sample_id", "outcome")):
            raise ValueError("invalid unmeasured sample")
        if type(row["tries"]) is not int or row["tries"] < 1:
            raise ValueError("invalid unmeasured sample tries")
