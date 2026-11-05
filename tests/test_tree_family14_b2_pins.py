"""I1 (ADR-0037, spec §3.3): this release reads and re-derives every value a
2.0.0b2 writer produced for a family-14 prefix, byte for byte.

Fixture-integrity pins (`JOURNAL_VERBATIM`) read values verbatim from the
committed journal and only detect an edit of the fixture. Re-derivation pins
recompute with this release: the reducer replay of `effective_attempt_feedback`,
the audit, the identity round trip, round 2's history from the committed
bytes, and a live re-run of the scenario. A failure here is a STOP condition
of the b3 plan, never a pin edit.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evolution.prompts import TREE_AGENT_EDITOR_SYSTEM, TREE_EDITOR_SYSTEM
from evolution.tree.campaign import inspect_campaign
from evolution.tree.campaign_identity import CampaignIdentityV1
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor_contracts import BRIEF_VERSION
from tests.family14_b2_scenario import (
    JOURNAL_REDERIVED,
    JOURNAL_VERBATIM,
    LIVE_EXCLUDED,
    REQUIRED_REPRODUCIBLE,
    capture_role_inputs,
    collect_pins,
    i1_options,
    journal_records,
    rederive_round_two_history,
)
from tests.test_tree_campaign_family14_run import family14_run

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "family14_b2"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
#: The packaged Editor prompts at `5541b8989`: their digests enter the Editor
#: identity, so b3 must not change them (plan routine choice 7, ADR-0038).
PROMPT_SHA256 = {
    "TREE_EDITOR_SYSTEM": "6d54fce6724901d20a652a742c8645ccf33388c09d860b5a611490433d2e1bbc",
    "TREE_AGENT_EDITOR_SYSTEM": "07cb8e39e495f242df7de1adcc45cd7c5d040d26df9ce79fc85081193e6b5464",
}


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "run"
    shutil.copytree(FIXTURE / "run", target)
    for path in [target, *target.rglob("*")]:
        path.chmod(path.stat().st_mode | 0o200)
    return target


def test_the_brief_version_and_the_packaged_editor_prompts_are_unchanged() -> None:
    assert BRIEF_VERSION == EXPECTED["brief_version"] == "edit_brief/4"
    assert (
        hashlib.sha256(TREE_EDITOR_SYSTEM.encode()).hexdigest()
        == PROMPT_SHA256["TREE_EDITOR_SYSTEM"]
    )
    assert (
        hashlib.sha256(TREE_AGENT_EDITOR_SYSTEM.encode()).hexdigest()
        == PROMPT_SHA256["TREE_AGENT_EDITOR_SYSTEM"]
    )


def test_the_committed_journal_is_unedited_and_replays_to_the_b2_values() -> None:
    pins = collect_pins(FIXTURE / "run")
    for key in (*JOURNAL_VERBATIM, *JOURNAL_REDERIVED):
        assert pins[key] == EXPECTED[key], key


def test_round_two_history_re_derives_from_the_committed_bytes(tmp_path: Path) -> None:
    run = _copy(tmp_path)
    visibility = EXPECTED["role_visibility"]
    identity = journal_records(run)[0]["payload"]["identity"]
    assert hashlib.sha256(canonical_json(visibility)).hexdigest() == identity["role_visibility"]
    rederived = rederive_round_two_history(
        run, FIXTURE / "corpus.jsonl", visibility["readable_paths"]
    )
    assert rederived == EXPECTED["round_two_history"]
    assert rederived == {
        "editor_history": EXPECTED["editor_history_digests"][1],
        "reflector_history": EXPECTED["reflector_history_digests"][1],
    }


def test_the_committed_b2_run_audits_clean(tmp_path: Path) -> None:
    run = _copy(tmp_path)
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


def test_the_committed_identity_round_trips_byte_for_byte() -> None:
    identity = journal_records(FIXTURE / "run")[0]["payload"]["identity"]
    value = CampaignIdentityV1.from_payload(identity)
    assert value.to_payload() == identity
    assert value.digest == EXPECTED["identity_digest"]
    assert "verbatim_answers" not in identity


async def test_this_release_journals_the_pinned_role_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = i1_options(tmp_path, monkeypatch)
    captured = capture_role_inputs(monkeypatch)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished is not None
    live = {**collect_pins(run), **captured}
    assert set(REQUIRED_REPRODUCIBLE) <= set(EXPECTED["reproducible"])
    for key in EXPECTED["reproducible"]:
        if key not in LIVE_EXCLUDED:
            assert live[key] == EXPECTED[key], key
    assert live["identity_keys"] == EXPECTED["identity_keys"]
