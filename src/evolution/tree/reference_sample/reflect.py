"""Sample Reflector: one diagnosis group per portion cohort that has an exemplar.

Reads the `reflect_request/2` document on stdin and prints
`{"status": ..., "groups": [...], "note": "sample reflector"}`. For every
cohort of the request's `evidence.portion` that carries an exemplar it claims
one group: the seed's first line does not carry what the scorer looks for,
citing that exemplar, claiming the cohort's case and naming the seed's one
skill file. It uses the supplied `list_evidence` and `read_evidence` helpers
to confirm the selected observations before it makes those claims. An empty
portion — no failing cohort to account for — is `declined` with no groups.
A sample, not a recommended Reflector: its hypothesis is the one the sample
campaign is built around.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence

REQUEST_VERSION = "reflect_request/2"
ROOT_CAUSE = "the skill's first line does not carry the improvement the scorer looks for"
SUGGESTED_FIX = "rewrite the first line of skills/SKILL.md"
TARGET_PATH = "skills/SKILL.md"


def _reader_call(
    readers: object, operation: str, arguments: dict[str, object]
) -> Mapping[str, object] | None:
    """Run one sealed helper command and accept only its one JSON object result."""

    if not isinstance(readers, Mapping):
        return None
    argv = readers.get(operation)
    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, (str, bytes))
        or not all(isinstance(part, str) for part in argv)
    ):
        return None
    try:
        completed = subprocess.run(
            [*argv, json.dumps(arguments, separators=(",", ":"))],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        result = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not isinstance(result, Mapping) or "error" in result:
        return None
    return result


def _selected_observations(request: Mapping[str, object]) -> set[str] | None:
    """Confirm each selected exemplar through the operation-local helpers."""

    evidence = request.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    portion = evidence.get("portion")
    if not isinstance(portion, list):
        return None
    selected = {
        exemplar
        for cohort in portion
        if isinstance(cohort, Mapping)
        for exemplars in (cohort.get("exemplars"),)
        if isinstance(exemplars, list)
        for exemplar in exemplars[:1]
        if isinstance(exemplar, str)
    }
    if not selected:
        return set()
    readers = request.get("readers")
    catalog = _reader_call(readers, "list_evidence", {})
    if catalog is None or not isinstance(catalog.get("entries"), list):
        return None
    for evidence_id in selected:
        if (
            _reader_call(readers, "read_evidence", {"evidence_id": evidence_id, "offset": 0})
            is None
        ):
            return None
    return selected


def main() -> None:
    request = json.load(sys.stdin)
    version = request.get("version") if isinstance(request, dict) else None
    if version != REQUEST_VERSION:
        sys.stderr.write(f"reflect.py: expected {REQUEST_VERSION}, got {version!r}\n")
        raise SystemExit(2)
    selected = _selected_observations(request)
    if selected is None:
        print(json.dumps({"status": "failed", "groups": [], "note": "reader unavailable"}))
        return
    evidence = request.get("evidence")
    assert isinstance(evidence, Mapping)
    portion = evidence.get("portion")
    assert isinstance(portion, list)
    groups = [
        {
            "root_cause": ROOT_CAUSE,
            "suggested_fix": SUGGESTED_FIX,
            "evidence_ids": [cohort["exemplars"][0]],
            "affected_cases": [cohort["case_id"]],
            "target_paths": [TARGET_PATH],
            "counterevidence": "No passing observation was selected for this cohort.",
            "regression_risks": "Keep the existing response format while changing the first line.",
            "why_this_layer": "The observed failure is caused by the editable skill text.",
        }
        for cohort in portion
        if isinstance(cohort, Mapping)
        and isinstance(cohort.get("exemplars"), list)
        and cohort["exemplars"]
        and isinstance(cohort["exemplars"][0], str)
        and cohort["exemplars"][0] in selected
        and isinstance(cohort.get("case_id"), str)
    ]
    status = "reflected" if groups else "declined"
    print(json.dumps({"status": status, "groups": groups, "note": "sample reflector"}))


if __name__ == "__main__":
    main()
