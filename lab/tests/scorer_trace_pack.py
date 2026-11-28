"""Test-only b6 pack fixtures; these do not exercise the unshipped PR-B writer.

Exact evo_skills-2.0.0b6-py3-none-any.whl SHA256:
acdbc710902ea448fe1f22308250ba35768df5ee1cf4669c5b6751cb46514ec0.
Rechecked 2026-10-17 on that exact wheel; scorer_traces.py SHA256:
6830d3c68c52ef836a4f32c7ca44df14243ca00341a29fe36e95bfb1d1cc7a65.
Private header/layout provenance: evolution/tree/scorer_traces.py PACK_FORMAT,
_KEYS, _SECTION_NAMES, encode_pack_record and _name_ids in that wheel. Records
have format=scorer_trace_pack/1: pack(pack_id, segment=0), request(trace id,
request section), output(trace id, status, nullable returncode, stdout/stderr).
The core encoder supplies canonical headers, hsha, section lengths/digests and
framing; this helper neither parses nor reimplements that format. Addressing:
campaign <run>/scorer-traces/<measurement_id>.pack; fixed evaluation uses
<run>/measurement as reader root and its journal run_id as measurement_id.
At the next core pin, re-read the exact wheel and recheck these private fields,
layout, addressing and encoder acceptance before using or updating this helper.
"""
from evolution.tree.execution.scorer_traces import PACK_FORMAT, encode_pack_record


def scorer_pack(root, mid, rid, *, request=None, stdout=None, returncode=None, status="completed"):
    directory = root / "scorer-traces"
    directory.mkdir(parents=True, exist_ok=True)
    data = encode_pack_record({"format": PACK_FORMAT, "kind": "pack", "pack_id": mid, "segment": 0})
    if request is not None:
        data += encode_pack_record({"format": PACK_FORMAT, "kind": "request", "score_request_id": rid},
                                   [("request", request)])
    if stdout is not None:
        data += encode_pack_record({"format": PACK_FORMAT, "kind": "output", "score_request_id": rid,
                                    "status": status, "returncode": returncode},
                                   [("stdout", stdout), ("stderr", b"private stderr")])
    path = directory / (mid + ".pack")
    path.write_bytes(data)
    return path
