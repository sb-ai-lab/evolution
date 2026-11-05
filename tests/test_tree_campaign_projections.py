"""Task 7: the two campaign projections and the read-only inspection view (spec §12)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign import CampaignMissing, inspect_campaign, run_campaign
from evolution.tree.campaign_identity import measurement_id_for
from evolution.tree.campaign_projections import (
    CAMPAIGN_FILE,
    CampaignProjection,
    CampaignUnreadable,
    _verify_reference,
    inspect_view,
    project_result,
    project_status,
    read_family_version,
    read_run_id,
    verify_evidence,
)
from evolution.tree.campaign_schema import (
    MAX_EXCLUDED_PAIRS,
    OWNERS,
    campaign_family_for,
    pairs_digest,
)
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from evolution.tree.storage.envelope import Envelope, SchemaError
from tests.campaign_fakes import (
    corpus,
    policy,
)
from tests.test_tree_campaign import (
    ARTIFACT_POLICY,
    Rig,
    view_of,
)

pytestmark = pytest.mark.slow


async def finished_rig(tmp_path: Path, **overrides: Any) -> Rig:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1), **overrides)
    result = await rig.run()
    assert result.terminal == "finished"
    return rig


# -- shapes ---------------------------------------------------------------


async def test_the_status_projection_lists_siblings_and_the_resume_actions(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))

    def after_editor_finished() -> None:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await rig.run(_hooks={"after_editor_finished": after_editor_finished})

    payload = project_status(inspect_view(rig.run_root)).to_payload()

    assert payload["kind"] == "campaign_status"
    assert payload["claim_scope"] == "campaign"
    assert payload["siblings"]["artifact_store"] == "born"
    assert payload["siblings"]["m-r00-champion"] == "born"
    assert payload["measurements"][0]["classification"] == "born"
    assert payload["measurements"][0]["status"]["kind"] == "status"
    assert [action["kind"] for action in payload["actions"]] == ["SealAttempt"]
    assert payload["lineage"]["store_state"] == "active"
    assert payload["verification"] is None
    assert payload["complete"] is False


async def test_a_stalled_campaign_names_the_seed_as_its_final_champion(
    tmp_path: Path,
) -> None:
    rig = Rig(
        tmp_path,
        campaign_policy=policy(rounds=1, attempts_per_round=1),
    )
    from tests.campaign_fakes import EditorScript, ScriptedEditor

    rig.editor = ScriptedEditor({"r01-a01": EditorScript("declined")})
    await rig.run()

    view = inspect_view(rig.run_root)
    payload = project_result(view.snapshot, tries=view.tries).to_payload()

    assert payload["final_champion"]["baseline_origin"] == "seed"
    assert payload["final_champion"]["version"] == "v0"
    assert payload["rounds"][0]["attempts"][0]["reason"] == "editor_declined"
    assert payload["rounds"][0]["attempts"][0]["artifact_locator"] is None


def test_a_projection_refuses_an_unknown_key_and_a_secret() -> None:
    with pytest.raises(SchemaError):
        CampaignProjection({"kind": "campaign_result"})
    view = view_of_empty()
    projection = project_status(view)
    from evolution.tree.campaign_projections import ProjectionInvalid

    with pytest.raises(SchemaError, match="sensitive public projection") as refused:
        CampaignProjection(projection.to_payload(), secrets=("campaign",))
    assert not isinstance(refused.value, ProjectionInvalid)


def view_of_empty() -> Any:
    from evolution.tree.campaign_schema import CampaignSnapshot

    snapshot = CampaignSnapshot(
        family_version=14,
        identity={},
        run_id="c-empty",
        alpha_plan={},
        rounds={},
        champion=("", ""),
        champion_dependency_digest="c" * 64,
        measurements={},
        accounted=frozenset(),
        counts=dict.fromkeys(OWNERS, 0),
        denominator=0,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=1,
    )
    return view_of(snapshot)


# -- inspection -----------------------------------------------------------


async def test_inspect_campaign_verifies_every_evidence_reference(tmp_path: Path) -> None:
    rig = await finished_rig(tmp_path)

    projection = inspect_campaign(rig.run_root, verify=True)
    payload = projection.to_payload()

    verification = payload["verification"]
    # Two passes, two counters: `checked` is references, and the semantic pass
    # is counted on its own, so neither number changes meaning as the other
    # grows. Every E record with evidence is re-checked, and a record carries at
    # least one reference, so there are never more preconditions than references.
    assert verification["checked"] > 0
    assert 0 < verification["preconditions_checked"] <= verification["checked"]
    assert verification["mismatches"] == []
    assert payload["complete"] is True


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


def _run_family(run_root: Path):
    family_version = read_family_version(run_root)
    assert family_version is not None
    return family_version, campaign_family_for(family_version)


def _forge(run_root: Path, event: str, payload: dict[str, Any], *, instead_of: str) -> None:
    """Rewrite `campaign.jsonl` as another writer might have produced it.

    The campaign prefix is truncated at its first `instead_of` record and one
    forged `event` is appended in its place. The record still satisfies the
    campaign family and every lifecycle rule — that is the point: only the
    E-precondition it claims is false, and nothing but an audit over the
    sibling bytes can say so.
    """

    path = run_root / "campaign.jsonl"
    kept: list[bytes] = []
    for line in _lines(path):
        if json.loads(line)["event"] == instead_of:
            break
        kept.append(line)
    assert kept
    last = json.loads(kept[-1])
    _family_version, family = _run_family(run_root)
    record = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id=last["run_id"],
            sequence=len(kept) + 1,
            timestamp=last["timestamp"],
            event=event,
            operation_id="forged",
            payload=payload,
        ),
        family=family,
    )
    path.write_bytes(b"".join(kept) + record.encode())


def _rewrite(
    run_root: Path, event: str, payload: dict[str, Any], *, match: dict[str, Any] | None = None
) -> None:
    """Replace one `event` record's payload; the rest of the journal stands.

    The shape of a forgery that changes one recorded fact and nothing else —
    every later record, and every reference into a sibling log, is untouched.
    `match` picks the record by payload fields when the event occurs more than
    once; the first match wins.
    """

    path = run_root / "campaign.jsonl"
    _family_version, family = _run_family(run_root)
    out: list[bytes] = []
    done = False
    for line in _lines(path):
        record = json.loads(line)
        fits = record["event"] == event and all(
            record["payload"].get(key) == value for key, value in (match or {}).items()
        )
        if not done and fits:
            record["payload"] = payload
            line = Envelope.from_payload(record, family=family).encode()
            done = True
        out.append(line)
    assert done
    path.write_bytes(b"".join(out))


def _record_reference(
    run_root: Path, measurement_id: str, event: str, **match: Any
) -> dict[str, Any]:
    """A `journal_record` reference over one measurement record, by event and payload."""

    path = run_root / "measurements" / measurement_id / "events.jsonl"
    offset = 0
    for line in _lines(path):
        record = json.loads(line)
        fits = record["event"] == event and all(
            record["payload"].get(key) == value for key, value in match.items()
        )
        if fits:
            return dict(
                version=1,
                kind="journal_record",
                run_id=measurement_id,
                locator=(
                    f"measurements/{measurement_id}/events.jsonl"
                    f"#bytes={offset}:{offset + len(line)}"
                ),
                content_digest=hashlib.sha256(line).hexdigest(),
            )
        offset += len(line)
    raise AssertionError(f"no {event} record matching {match}")


def _prefix_reference(run_root: Path, measurement_id: str, records: int) -> dict[str, Any]:
    """A `journal_prefix` over the first `records` records of a measurement journal.

    Intact — the digest is the digest of exactly those bytes — and, for a count
    short of the whole journal, incomplete.
    """

    path = run_root / "measurements" / measurement_id / "events.jsonl"
    raw = b"".join(_lines(path)[:records])
    return dict(
        version=1,
        kind="journal_prefix",
        run_id=measurement_id,
        locator=f"measurements/{measurement_id}/events.jsonl#bytes=0:{len(raw)}",
        content_digest=hashlib.sha256(raw).hexdigest(),
    )


async def test_a_semantically_forged_baseline_complete_is_reported(tmp_path: Path) -> None:
    """ADR-0008: an audit re-verifies the E-precondition, not only the digest.

    A `baseline_complete` that references an *intact but incomplete* measurement
    prefix hashes perfectly; only re-evaluating the precondition over what those
    bytes say — every scheduled sample scored — detects it.
    """

    rig = await finished_rig(tmp_path)
    measurement = "m-r00-champion"
    reference = _prefix_reference(rig.run_root, measurement, 2)
    assert _verify_reference(rig.run_root, reference, {}) is None

    _forge(
        rig.run_root,
        "baseline_complete",
        dict(round=1, measurement_id=measurement, evidence=[reference]),
        instead_of="baseline_complete",
    )

    verification = verify_evidence(rig.run_root)

    assert [row["code"] for row in verification["mismatches"]] == ["precondition_failed"]
    assert verification["mismatches"][0]["event"] == "baseline_complete"
    assert verification["mismatches"][0]["detail"]


async def test_verify_evidence_is_read_only(tmp_path: Path) -> None:
    rig = await finished_rig(tmp_path)
    before = (rig.run_root / "campaign.jsonl").read_bytes()

    verify_evidence(rig.run_root)

    assert (rig.run_root / "campaign.jsonl").read_bytes() == before


# -- look_evaluated recomputation (Task 6) ---------------------------------


# Other records citing the rewritten journal fail integrity; that is expected and not asserted here.


async def test_inspect_campaign_passes_the_corpus_through(tmp_path: Path) -> None:
    rig = await finished_rig(tmp_path)
    projection = inspect_campaign(rig.run_root, verify=True, corpus=rig.cases)
    assert projection.to_payload()["verification"]["corpus"] == "proven"


async def test_the_run_result_carries_both_projections(tmp_path: Path) -> None:
    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    result = await rig.run()

    assert result.result is not None
    assert result.result.to_payload()["kind"] == "campaign_result"
    assert result.status.to_payload()["kind"] == "campaign_status"
    assert json.loads(result.status.encode())["run_id"] == rig.records()[0]["run_id"]


def test_run_campaign_and_the_artifact_policy_are_importable() -> None:
    assert ARTIFACT_POLICY.writable_paths == ("skill.py",)
    assert run_campaign is not None


@pytest.mark.parametrize(
    "locator",
    ["../secrets.txt#bytes=0:3", "/etc/passwd#bytes=0:3", "measurements/../../x#bytes=0:3"],
)
def test_a_locator_that_escapes_the_run_is_a_mismatch(tmp_path: Path, locator: str) -> None:
    """`_validate_locator` bars these at append time; verification re-applies the rule.

    `verify_evidence` reads any run root, including one another writer produced,
    so the safety half of the locator grammar is re-checked before a locator
    becomes a path -- and reported as a mismatch, never raised and never read.
    """

    outside = tmp_path / "secrets.txt"
    outside.write_bytes(b"abc")
    run_root = tmp_path / "run"
    run_root.mkdir()

    code = _verify_reference(run_root, {"locator": locator, "content_digest": "0" * 64}, {})

    assert code == "malformed_locator"


# -- read_run_id ----------------------------------------------------------


async def test_read_run_id_returns_the_journaled_id(tmp_path: Path) -> None:
    """The orchestrator builds its adapter from this before `resume_campaign`."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run(run_id="c-read-me")

    assert read_run_id(rig.run_root) == "c-read-me"


def test_read_run_id_refuses_an_absent_journal(tmp_path: Path) -> None:
    with pytest.raises(CampaignMissing, match="campaign_absent"):
        read_run_id(tmp_path / "nope")


@pytest.mark.parametrize("raw", [b'{"not": "an envelope"}\n', b"", b'{"event": "campaign_'])
def test_read_run_id_signals_an_unreadable_journal_apart_from_an_absent_one(
    tmp_path: Path, raw: bytes
) -> None:
    """A journal that exists but holds no readable creation is C1's to classify.

    Garbage bytes, an empty file and a creation record torn by a crash are one
    fact: a campaign was started in this run root and its id is not legible. That
    is not `campaign_absent` — the orchestrator must hand such a run root to
    `resume_campaign`, which reports its persistence outcome.
    """

    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / CAMPAIGN_FILE).write_bytes(raw)

    with pytest.raises(CampaignUnreadable, match="campaign_unreadable"):
        read_run_id(run_root)


async def test_read_run_id_refuses_a_journal_that_lost_its_creation(
    tmp_path: Path,
) -> None:
    """Dropping `campaign_created` leaves records the reader must not read an id from."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run()
    path = rig.run_root / CAMPAIGN_FILE
    lines = path.read_bytes().splitlines(keepends=True)
    assert json.loads(lines[0])["event"] == "campaign_created"
    path.write_bytes(b"".join(lines[1:]))

    with pytest.raises(CampaignUnreadable, match="campaign_unreadable"):
        read_run_id(rig.run_root)


async def test_read_run_id_reads_past_a_truncated_tail(tmp_path: Path) -> None:
    """Only the first envelope matters: a torn last record does not hide the id."""

    rig = Rig(tmp_path, campaign_policy=policy(rounds=1, attempts_per_round=1))
    await rig.run(run_id="c-torn")
    path = rig.run_root / CAMPAIGN_FILE
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 20])

    assert read_run_id(rig.run_root) == "c-torn"


# -- read_family_version ---------------------------------------------------


def _rewrite_creation(run_root: Path, edit: Any) -> None:
    """Rewrite the first line of `campaign.jsonl` as raw JSON — no family validation."""

    path = run_root / CAMPAIGN_FILE
    lines = _lines(path)
    record = json.loads(lines[0])
    edit(record)
    lines[0] = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    path.write_bytes(b"".join(lines))


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r["payload"].clear(),
        lambda r: r["payload"].__setitem__("extra", 1) or r["payload"].pop("family_version"),
        lambda r: (r["payload"].pop("family_version"), r["payload"].pop("identity")),
        lambda r: r["payload"].__setitem__("family_version", "x\nforged"),
        lambda r: r["payload"].__setitem__("family_version", True),
        lambda r: r["payload"].__setitem__("family_version", [2]),
        # The envelope's own common shape is
        # enforced by the same `Envelope.from_bytes` every other reader uses,
        # so a malformed envelope field is illegible, not a family.
        lambda r: r.__setitem__("schema_version", True),
        lambda r: r.pop("run_id"),
    ],
)
async def test_read_family_version_refuses_other_shapes(tmp_path: Path, edit: Any) -> None:
    rig = await finished_rig(tmp_path)
    _rewrite_creation(rig.run_root, edit)
    with pytest.raises(CampaignUnreadable) as failure:
        read_family_version(rig.run_root)
    assert "forged" not in str(failure.value)


async def test_read_family_version_refuses_a_duplicated_key(tmp_path: Path) -> None:
    """Finding 2 (codex branch round 1): a repeated key is illegible, not a family.

    `_rewrite_creation` edits a parsed dict, which cannot represent a
    duplicate key, so this forges the raw line's text directly.
    """

    rig = await finished_rig(tmp_path)
    path = rig.run_root / CAMPAIGN_FILE
    lines = _lines(path)
    original = json.loads(lines[0])
    assert "forged" not in json.dumps(original)
    text = json.dumps(original, separators=(",", ":"), ensure_ascii=False)
    # Insert a second, later "run_id" key so the line still parses as one JSON
    # object but carries the key twice -- `Envelope.from_bytes`'s
    # object_pairs_hook refuses that, unlike a bare `json.loads`.
    forged = text.replace('"run_id":', '"run_id":"forged","run_id":', 1)
    lines[0] = forged.encode() + b"\n"
    path.write_bytes(b"".join(lines))

    with pytest.raises(CampaignUnreadable) as failure:
        read_family_version(rig.run_root)

    assert "forged" not in str(failure.value)


def test_read_family_version_absent_and_illegible(tmp_path: Path) -> None:
    with pytest.raises(CampaignMissing, match="^campaign_absent$"):
        read_family_version(tmp_path)
    (tmp_path / "campaign.jsonl").write_bytes(b"")
    with pytest.raises(CampaignUnreadable):
        read_family_version(tmp_path)
    (tmp_path / "campaign.jsonl").write_bytes(b"\x00garbage\n")
    with pytest.raises(CampaignUnreadable):
        read_family_version(tmp_path)


REBASE = {
    "previous_digest": "a" * 64,
    "identity_digest": "b" * 64,
    "producing_version": "0.3.0a12",
    "changed_fields": ["identity.editor.digest", "producing_version"],
    "reason": "fixed the prompt",
}


def _view_with_one_rebase() -> Any:
    from evolution.tree.campaign_schema import CampaignSnapshot
    from tests.campaign_drivers import native14_created

    created = native14_created()
    snapshot = CampaignSnapshot(
        family_version=14,
        grouper_kind=created["adaptation_payload"]["grouping"]["kind"],
        identity=created["identity"],
        run_id="c-rebased",
        alpha_plan={},
        rounds={},
        champion=("", ""),
        champion_dependency_digest="c" * 64,
        measurements={},
        accounted=frozenset(),
        counts=dict.fromkeys(OWNERS, 0),
        denominator=0,
        alpha_rounds={},
        threshold=None,
        finished=None,
        capacity=None,
        first_round=1,
        total_rounds=1,
        identity_digest="b" * 64,
        producing_version="0.3.0a12",
        rebases=(dict(REBASE),),
        admitted_versions=frozenset({"0.3.0a11", "0.3.0a12"}),
    )
    return view_of(snapshot)


def test_both_projections_carry_the_latest_identity_digest_and_its_rebases() -> None:
    view = _view_with_one_rebase()

    status = project_status(view).to_payload()
    result = project_result(view.snapshot, tries=view.tries).to_payload()

    for payload in (status, result):
        assert payload["identity_digest"] == "b" * 64
        assert payload["rebases"] == [REBASE]
        assert set(payload["rebases"][0]) == {
            "previous_digest",
            "identity_digest",
            "producing_version",
            "changed_fields",
            "reason",
        }
    # An identity that has never been rebased publishes no digest and no rebases.
    empty = project_status(view_of_empty()).to_payload()
    assert empty["identity_digest"] is None and empty["rebases"] == []


def test_a_projection_refuses_a_rebase_entry_with_an_extra_key() -> None:
    payload = project_status(_view_with_one_rebase()).to_payload()
    payload["rebases"][0]["surprise"] = 1
    with pytest.raises(SchemaError):
        CampaignProjection(payload)
    payload["rebases"][0].pop("surprise")
    payload["identity_digest"] = "not-a-digest"
    with pytest.raises(SchemaError):
        CampaignProjection(payload)


# -- family 6: tries, excluded pairs, linked runs (continuable terminals) --


def _v6_rig(tmp_path: Path, name: str, *, schema: str = SCHEMA_V15, **values: Any) -> Rig:
    from tests.campaign_fakes import v5_policy
    from tests.test_tree_campaign import LENIENT

    settings: dict[str, Any] = dict(
        rounds=1, attempts_per_round=1, sample_retries=1, indeterminate=LENIENT
    )
    cases = values.pop("cases", None)
    settings.update(values)
    return Rig(
        tmp_path,
        campaign_policy=v5_policy(**settings),
        run_name=name,
        **({} if cases is None else {"cases": cases}),
        schema=schema,
    )


async def exhausted_baseline_rig(tmp_path: Path, name: str = "exhausted-baseline") -> Rig:
    """A baseline sample exhausts both of its tries; the Attempt evaluates without it."""

    from tests.test_tree_campaign import seed_key

    rig = _v6_rig(tmp_path, name)
    rig.adapter.dispositions[seed_key(rig)] = "target_failed"
    result = await rig.run()
    assert result.terminal == "finished"
    assert "pairs_excluded" in rig.events() and "look_evaluated" in rig.events()
    return rig


async def recovered_baseline_rig(
    tmp_path: Path, name: str = "recovered", *, schema: str = SCHEMA_V15
) -> Rig:
    """Try `0` of a baseline sample fails, try `1` scores; two retries are allowed."""

    from tests.test_tree_campaign import seed_key

    rig = _v6_rig(tmp_path, name, sample_retries=2, schema=schema)
    rig.adapter.dispositions[seed_key(rig)] = ["target_failed"]
    result = await rig.run()
    assert result.terminal == "finished" and "pairs_excluded" not in rig.events()
    return rig


async def exhausted_attempt_rig(
    tmp_path: Path, name: str = "exhausted-attempt", *, schema: str = SCHEMA_V15
) -> Rig:
    from tests.test_tree_campaign import edited_key

    rig = _v6_rig(tmp_path, name, schema=schema)
    rig.adapter.dispositions[edited_key(tmp_path)] = "time_budget"
    await rig.run()
    assert "look_not_started" in rig.events()
    return rig


async def barred_look_with_exclusion_rig(tmp_path: Path, name: str = "barred") -> Rig:
    """The baseline excludes one pair and the Attempt's look is barred by another."""

    from tests.test_tree_campaign import edited_key, seed_key

    rig = _v6_rig(tmp_path, name)
    rig.adapter.dispositions[seed_key(rig)] = "target_failed"
    rig.adapter.dispositions[edited_key(tmp_path, "case-02", "01")] = "time_budget"
    await rig.run()
    assert "pairs_excluded" in rig.events() and "look_not_started" in rig.events()
    return rig


async def no_evaluable_pairs_rig(tmp_path: Path, name: str = "no-pairs") -> Rig:
    from tests.test_tree_campaign import TWO_LOOKS, seed_key

    rig = _v6_rig(
        tmp_path,
        name,
        sample_retries=0,
        alpha=TWO_LOOKS,
        cases=corpus(1),
    )
    for repeat in (1, 2, 3):
        rig.adapter.dispositions[seed_key(rig, "case-01", f"{repeat:02d}")] = "target_failed"
    await rig.run()
    closed = [r["payload"]["reason"] for r in rig.records() if r["event"] == "attempt_closed"]
    assert closed == ["no_evaluable_pairs"]
    return rig


def _focused_on_case_one(rig: Rig) -> None:
    """Scope the gate to `case-01`, as `test_focus_fully_excluded_closes_no_evaluable_focus`."""

    from tests.campaign_fakes import ReflectorScript, ScriptedReflector
    from tests.test_tree_campaign import GROUP, _PassingCaseScorer

    rig.scorer = _PassingCaseScorer(
        __import__("evolution.tree.artifacts.artifact_policy", fromlist=["scan_tree"])
        .scan_tree(rig.seed_root, ARTIFACT_POLICY)
        .candidate_digest
    )
    rig.reflector = ScriptedReflector(
        {1: ReflectorScript("reflect", groups=[GROUP])}, cite_first_exemplar=True
    )


async def no_evaluable_focus_rig(tmp_path: Path, name: str = "no-focus") -> Rig:
    from tests.test_tree_campaign import TWO_LOOKS, seed_key

    rig = _v6_rig(
        tmp_path,
        name,
        sample_retries=0,
        alpha=TWO_LOOKS,
        cases=corpus(2),
    )
    _focused_on_case_one(rig)
    for repeat in (1, 2, 3):
        rig.adapter.dispositions[seed_key(rig, "case-01", f"{repeat:02d}")] = "target_failed"
    await rig.run()
    closed = [r["payload"]["reason"] for r in rig.records() if r["event"] == "attempt_closed"]
    assert closed == ["no_evaluable_focus"]
    return rig


async def rest_layer_exclusion_rig(tmp_path: Path, name: str = "rest-layer") -> Rig:
    """The frozen focus is one case, the rest layer the other, and a baseline pair is excluded.

    Which of the two cases the single-case claim scopes the gate to is not pinned
    here (the focus tie-break between equal cohorts is a known open question);
    either way the rest layer is non-empty after the exclusion.
    """

    from tests.test_tree_campaign import seed_key

    rig = _v6_rig(tmp_path, name, sample_retries=0, cases=corpus(2))
    _focused_on_case_one(rig)
    rig.adapter.dispositions[seed_key(rig, "case-02", "02")] = "target_failed"
    await rig.run()
    looks = [r["payload"] for r in rig.records() if r["event"] == "look_evaluated"]
    assert "pairs_excluded" in rig.events()
    assert len(_payloads(rig, "gate_scope_selected")[0]["cases"]) == 1
    assert looks and looks[0]["layers"]["rest"] is not None
    return rig


async def threshold_after_recovery_rig(tmp_path: Path, name: str = "threshold") -> Rig:
    """Two baseline samples each fail once and recover; the second try crosses `target`."""

    from tests.test_tree_campaign import seed_key

    thresholds = {
        owner: {"max_count": 100, "max_rate": "1"}
        for owner in ("target", "environment", "scorer", "rollout")
    }
    thresholds["target"] = {"max_count": 1, "max_rate": "1"}
    rig = _v6_rig(tmp_path, name, indeterminate=thresholds)
    rig.adapter.dispositions[seed_key(rig, "case-01", "01")] = ["target_failed"]
    rig.adapter.dispositions[seed_key(rig, "case-01", "02")] = ["target_failed"]
    result = await rig.run()
    assert "threshold_reached" in rig.events() and "pairs_excluded" not in rig.events()
    assert result.finished is not None and result.finished["reason"] == "target_unstable"
    return rig


async def linked_successor_rig(tmp_path: Path, name: str = "second") -> Rig:
    from tests.test_tree_campaign import finished_rig as finished_v6_rig
    from tests.test_tree_campaign import successor_of

    first = await finished_v6_rig(tmp_path, "first")
    second = successor_of(first, tmp_path, name)
    assert (await second.run(linked_from=first.run_root)).terminal == "finished"
    return second


def _payloads(rig: Rig, event: str) -> list[dict[str, Any]]:
    return [record["payload"] for record in rig.records() if record["event"] == event]


def _codes(report: dict[str, Any], event: str) -> list[tuple[str, str | None]]:
    return [(row["code"], row["detail"]) for row in report["mismatches"] if row["event"] == event]


async def test_audit_refuses_an_indeterminate_try_the_journal_never_ran(tmp_path: Path) -> None:
    """Item 4: ordinal `k` is admitted by the lifecycle without try `k`; the audit is not."""

    rig = await recovered_baseline_rig(tmp_path)
    observed = _payloads(rig, "indeterminate_observed")
    assert [row["retry_ordinal"] for row in observed] == [0]
    # Ordinal 2 is the policy's last try, which reserves no retry of its own.
    _rewrite(
        rig.run_root,
        "indeterminate_observed",
        dict(observed[0], retry_ordinal=2, retry_follows=False),
    )

    codes = _codes(verify_evidence(rig.run_root), "indeterminate_observed")

    assert [code for code, _detail in codes] == ["precondition_failed"]
    assert "try 2" in str(codes[0][1])


async def test_audit_refuses_an_indeterminate_try_that_scored(tmp_path: Path) -> None:
    rig = await recovered_baseline_rig(tmp_path)
    observed = _payloads(rig, "indeterminate_observed")[0]
    _rewrite(rig.run_root, "indeterminate_observed", dict(observed, retry_ordinal=1))

    codes = _codes(verify_evidence(rig.run_root), "indeterminate_observed")

    assert [code for code, _detail in codes] == ["precondition_failed"]


async def test_audit_refuses_a_denied_retry_a_later_try_contradicts(tmp_path: Path) -> None:
    """ADR-0008 as amended: only a run that really started no further try denies one."""

    rig = await exhausted_attempt_rig(tmp_path)
    first, second = _payloads(rig, "indeterminate_observed")
    assert (first["retry_follows"], second["retry_follows"]) == (True, False)
    _rewrite(
        rig.run_root,
        "indeterminate_observed",
        dict(first, retry_follows=False),
        match={"retry_ordinal": 0},
    )

    codes = _codes(verify_evidence(rig.run_root), "indeterminate_observed")

    assert [code for code, _detail in codes] == ["precondition_failed"]
    assert "try 1 ran after the record denied a retry" in str(codes[0][1])


async def test_audit_refuses_an_indeterminate_try_citing_another_try(tmp_path: Path) -> None:
    rig = await exhausted_attempt_rig(tmp_path)
    first, second = _payloads(rig, "indeterminate_observed")
    assert (first["retry_ordinal"], second["retry_ordinal"]) == (0, 1)
    _rewrite(
        rig.run_root,
        "indeterminate_observed",
        dict(first, evidence=second["evidence"]),
        match={"retry_ordinal": 0},
    )

    codes = _codes(verify_evidence(rig.run_root), "indeterminate_observed")

    assert [code for code, _detail in codes] == ["precondition_failed"]
    assert "try 0" in str(codes[0][1])


# -- family 6: what the two projections report ----------------------------


async def test_the_run_result_reports_tries_like_the_read_back_projection(tmp_path: Path) -> None:
    from tests.test_tree_campaign import seed_key

    rig = _v6_rig(tmp_path, "live")
    rig.adapter.dispositions[seed_key(rig)] = "target_failed"
    result = await rig.run()

    assert result.result is not None
    assert result.result.to_payload() == rig.result_payload()
    live_status = result.status.to_payload()
    read_back = project_status(inspect_view(rig.run_root)).to_payload()
    # The live S1 statuses are taken before the executor's last sibling writes, so
    # only the family-6 facts are compared.
    assert [(m["tries"], m["excluded_pairs"]) for m in live_status["measurements"]] == [
        (m["tries"], m["excluded_pairs"]) for m in read_back["measurements"]
    ]


async def test_three_generations_report_cumulative_lineage_counts(tmp_path: Path) -> None:
    """Ruling R11 (Task 6 review): C's lineage is A's own tries plus B's own tries."""

    from tests.test_tree_campaign import seed_key, successor_of

    first = _v6_rig(tmp_path, "a")
    first.adapter.dispositions[seed_key(first)] = ["target_failed"]
    assert (await first.run()).terminal == "finished"
    second = successor_of(
        first, tmp_path, "b", indeterminate=first.policy.to_payload()["indeterminate"]
    )
    second.adapter.dispositions[seed_key(second, "case-02", "01")] = ["time_budget"]
    assert (await second.run(linked_from=first.run_root)).terminal == "finished"
    third = successor_of(
        second, tmp_path, "c", indeterminate=first.policy.to_payload()["indeterminate"]
    )
    assert (await third.run(linked_from=second.run_root)).terminal == "finished"

    a, b, c = (rig.result_payload()["indeterminate"] for rig in (first, second, third))

    assert a["lineage"] is None
    assert a["counts"]["target"] == 1 and b["counts"]["rollout"] == 1
    assert b["lineage"] == {"counts": a["counts"], "denominator": a["denominator"]}
    assert c["lineage"] == {
        "counts": {owner: a["counts"][owner] + b["counts"][owner] for owner in OWNERS},
        "denominator": a["denominator"] + b["denominator"],
    }
    assert c["counts"] == dict.fromkeys(OWNERS, 0)
    assert third.result_payload()["linked_from"]["first_round"] == 3


async def test_an_unborn_measurement_reports_zero_tries(tmp_path: Path) -> None:
    from tests.campaign_fakes import Crash

    rig = _v6_rig(tmp_path, "unborn")

    def after_measurement_bound() -> None:
        raise Crash("power cut")

    with pytest.raises(Crash):
        await rig.run(_hooks={"after_measurement_bound": after_measurement_bound})

    view = inspect_view(rig.run_root)
    payload = project_status(view).to_payload()

    unborn = [entry for entry in payload["measurements"] if entry["classification"] != "born"]
    assert unborn and all(entry["tries"] == 0 for entry in unborn)
    assert all(type(entry["tries"]) is int for entry in payload["measurements"])


def _journal_fault() -> Exception:
    from evolution.tree.storage.journal_errors import JournalFault

    return JournalFault("journal_limit_exceeded")


@pytest.mark.parametrize(
    ("patched", "failure", "code"),
    [
        ("read_measurement_tries", _journal_fault, "journal_limit_exceeded"),
        ("read_measurement_tries", lambda: OSError("gone"), "journal_unavailable"),
        # The prefix reduction itself fails: `read_measurement_tries` converts
        # it into the journal fault it is, and nothing else's
        # `ValueError` is widened into one.
        ("measurement_tries", lambda: ValueError("not a journal"), "journal_unavailable"),
    ],
    ids=["journal_fault", "os_error", "value_error"],
)
async def test_unreadable_try_facts_after_the_terminal_are_a_persistence_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched: str, failure: Any, code: str
) -> None:
    """Facts that cannot be read after `campaign_finished` is durable
    end the run as `persistence_failed` over the measurement journal, with the
    read-only status, never a traceback and never a null `tries`."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree import campaign_projections as projections_module

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise failure()

    module = campaign_module if patched == "read_measurement_tries" else projections_module
    monkeypatch.setattr(module, patched, unreadable)
    rig = _v6_rig(tmp_path, "unreadable")

    result = await rig.run()

    assert "campaign_finished" in rig.events()
    assert (result.terminal, result.owner) == ("persistence_failed", "measurement_journal")
    assert result.code == code
    assert result.result is None and result.finished is None
    status = result.status.to_payload()
    assert status["kind"] == "campaign_status"
    assert all(type(entry["tries"]) is int for entry in status["measurements"])


# -- D2a spec §4: caveats and lineage caveats ------------------------------


async def test_the_validator_refuses_an_unknown_caveat_kind(tmp_path: Path) -> None:
    rig = await finished_rig(tmp_path)
    payload = dict(rig.result_payload(), caveats=[{"kind": "other", "from_run": "r"}])

    with pytest.raises(ValueError):
        CampaignProjection(payload)


async def test_the_validator_refuses_a_baseline_inherited_caveat_missing_scheduled(
    tmp_path: Path,
) -> None:
    rig = await finished_rig(tmp_path)
    caveat = {
        "kind": "baseline_inherited",
        "from_run": "r",
        "source_measurement_id": "m-r00-champion",
        "imported": 5,
        "fields": [],
        "origin_runs": 1,
    }
    payload = dict(rig.result_payload(), caveats=[caveat])

    with pytest.raises(ValueError):
        CampaignProjection(payload)


#: More than the 256 pairs a `pairs_excluded` record lists (spec §10): 90
#: cases at three repeats each, every one of them out of tries.
LARGE_CASES = 90
LARGE_PAIRS = LARGE_CASES * 3


async def large_exclusion_rig(tmp_path: Path, name: str = "large-exclusion") -> Rig:
    """Every baseline pair of a `LARGE_CASES`-case corpus is exhausted."""

    from tests.test_tree_campaign import seed_key

    lenient = {
        owner: {"max_count": LARGE_PAIRS * 2, "max_rate": "1"}
        for owner in ("target", "environment", "scorer", "rollout")
    }
    rig = _v6_rig(
        tmp_path, name, sample_retries=0, cases=corpus(LARGE_CASES), indeterminate=lenient
    )
    for index in range(1, LARGE_CASES + 1):
        for repeat in (1, 2, 3):
            key = seed_key(rig, f"case-{index:02d}", f"{repeat:02d}")
            rig.adapter.dispositions[key] = "target_failed"
    await rig.run()
    return rig


async def test_a_truncated_exclusion_summary_is_recomputed_from_the_prefix(
    tmp_path: Path,
) -> None:
    """Spec §10: over 256 exhausted pairs, the record stays bounded and provable."""

    rig = await large_exclusion_rig(tmp_path)
    payload = _payloads(rig, "pairs_excluded")[0]

    assert (payload["count"], payload["truncated"]) == (LARGE_PAIRS, True)
    assert len(payload["pairs"]) == MAX_EXCLUDED_PAIRS
    assert payload["by_owner"] == {"target": LARGE_PAIRS}
    expected = pairs_digest(
        [
            (f"case-{index:02d}", f"{repeat:02d}")
            for index in range(1, LARGE_CASES + 1)
            for repeat in (1, 2, 3)
        ]
    )
    assert payload["pairs_digest"] == expected
    # The whole journal still verifies: the audit recomputes the same bounded
    # summary from the measurement prefix, listed pairs included.
    assert verify_evidence(rig.run_root, corpus=rig.cases)["mismatches"] == []

    # `truncated` cannot be flipped on its own: the record's own shape ties it
    # to `count` and the listed pairs, so the journal is never even rewritten.
    with pytest.raises(SchemaError):
        _rewrite(rig.run_root, "pairs_excluded", dict(payload, truncated=False))

    # A self-consistent forgery -- "these 256 pairs are all of them" -- passes
    # the shape and is caught by the audit, which recomputes the set from the
    # measurement prefix instead of trusting the summary.
    forged = dict(
        payload,
        count=MAX_EXCLUDED_PAIRS,
        by_owner={"target": MAX_EXCLUDED_PAIRS},
        truncated=False,
    )
    _rewrite(rig.run_root, "pairs_excluded", forged)

    codes = _codes(verify_evidence(rig.run_root), "pairs_excluded")

    assert [code for code, _detail in codes] == ["pairs_excluded_mismatch"]
    assert "count" in str(codes[0][1])


# -- family 6: an applied close (comparable-links spec §5.2-§5.3) -----------


# -- task 9: the audit proves the copy; statistics are unchanged ----------


def test_the_audit_refuses_a_carried_row_without_the_runs_primary_metric() -> None:
    """§3.3 target-side admission: every carried row holds the run's own
    `metric.primary`. A copy whose rows digest to the declaration but lack
    it was never admissible, so the digest match alone does not pass it."""

    from types import SimpleNamespace

    from evolution.tree.baseline_import import import_pairs_digest
    from evolution.tree.campaign_projections import _check_baseline_import, _PreconditionFailed

    def carried(metrics: dict[str, float]) -> dict[str, Any]:
        return dict(case_id="case-01", sample_id="01", metrics=metrics)

    def context(rows: list[dict[str, Any]]) -> Any:
        declared = dict(imported=len(rows), pairs_digest=import_pairs_digest(rows))
        identity = SimpleNamespace(to_payload=lambda: {"linked_from": {"baseline": declared}})
        return SimpleNamespace(
            identity=lambda: identity,
            policy=lambda: SimpleNamespace(metric_primary="score"),
            policy_for=lambda measurement_id: SimpleNamespace(metric_primary="score"),
            records=(),
        )

    def envelopes(rows: list[dict[str, Any]]) -> list[Any]:
        return [SimpleNamespace(event="sample_imported", payload=row) for row in rows]

    good = [carried({"score": 1.0})]
    _check_baseline_import(context(good), measurement_id_for(None), envelopes(good))

    bad = [carried({"other": 1.0})]
    with pytest.raises(_PreconditionFailed) as caught:
        _check_baseline_import(context(bad), measurement_id_for(None), envelopes(bad))
    assert caught.value.code == "baseline_import_mismatch"


# -- family 7: suspensions, the threshold window and call-retry evidence ----
#
# I1 Task 10 (spec §3.4, §5.2, §5.3): a suspendable projection adds the
# suspension list, whether the run stands suspended, the window the next
# threshold counts over, and per measurement what the call retries cost.


def _v7_rig(tmp_path: Path, name: str, *, schema: str = SCHEMA_V15, **values: Any) -> Rig:
    """A native `/15` suspension rig with explicit retry and threshold settings."""

    from tests.campaign_fakes import v8_policy
    from tests.test_tree_campaign_suspension import LENIENT

    settings: dict[str, Any] = dict(
        rounds=1, attempts_per_round=1, sample_retries=1, indeterminate=LENIENT
    )
    cases = values.pop("cases", None)
    settings.update(values)
    return Rig(
        tmp_path,
        campaign_policy=v8_policy(**settings),
        run_name=name,
        schema=schema,
        **({} if cases is None else {"cases": cases}),
    )


async def suspended_threshold_rig(
    tmp_path: Path, name: str = "threshold-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """A `target` threshold crosses on the baseline stage and suspends there."""

    from tests.test_tree_campaign import seed_key
    from tests.test_tree_campaign_suspension import STRICT

    rig = _v7_rig(tmp_path, name, sample_retries=0, indeterminate=STRICT, schema=schema)
    rig.adapter.dispositions[seed_key(rig)] = "target_failed"
    result = await rig.run()
    assert result.terminal == "suspended"
    return rig


async def resumed_threshold_rig(
    tmp_path: Path, name: str = "resumed-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """The same crossing, then a healthy resume that completes the campaign."""

    rig = await suspended_threshold_rig(tmp_path, name, schema=schema)
    rig.adapter.dispositions.clear()
    result = await rig.resume()
    assert result.terminal == "finished"
    return rig


async def unrepaired_threshold_rig(
    tmp_path: Path, name: str = "unrepaired-threshold-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """#78: the same crossing, then a resume whose repair fails and suspends again.

    The second window holds the reservation's slot and the repair's own
    indeterminate try, which follows a try that denied a retry.
    """

    rig = await suspended_threshold_rig(tmp_path, name, schema=schema)
    result = await rig.resume()
    assert result.terminal == "suspended"
    assert [r["event"] for r in rig.records()].count("repairs_dispatched") == 1
    return rig


async def repaired_fault_rig(
    tmp_path: Path, name: str = "repaired-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """A contract fault suspends the baseline stage; the resume repairs the row."""

    from tests.test_tree_campaign import seed_key

    rig = _v7_rig(tmp_path, name, schema=schema)
    rig.adapter.dispositions[seed_key(rig)] = ["invalid_request"]
    assert (await rig.run()).terminal == "suspended"
    assert (await rig.resume()).terminal != "suspended"
    return rig


async def unrepaired_fault_rig(
    tmp_path: Path, name: str = "unrepaired-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """Three suspensions on the same row, two repair tries, nothing completed."""

    from tests.test_tree_campaign import seed_key

    rig = _v7_rig(tmp_path, name, schema=schema)
    rig.adapter.dispositions[seed_key(rig)] = "invalid_request"
    await rig.run()
    for _ in range(2):
        assert (await rig.resume()).terminal == "suspended"
    return rig


async def stage_record_rig(
    tmp_path: Path, name: str = "stage-record-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """An unresponsive editor suspends on the record that decided it."""

    from tests.campaign_fakes import EditorScript, ScriptedEditor

    rig = _v7_rig(
        tmp_path,
        name,
        sample_retries=0,
        editor_timeout_seconds=0.05,
        attempts_per_round=1,
        schema=schema,
    )
    rig.editor = ScriptedEditor({"r01-a01": EditorScript("swallow_cancel", seconds=3600.0)})
    with pytest.MonkeyPatch.context() as patch:
        from evolution.tree import campaign_editor

        patch.setattr(campaign_editor, "EDITOR_GRACE_SECONDS", 0.05)
        assert (await rig.run()).terminal == "suspended"
    return rig


async def probe_setup_rig(
    tmp_path: Path, name: str = "probe-setup-7", *, schema: str = SCHEMA_V15
) -> Rig:
    """The probe mediator never starts, so the suspension names the round's
    *ready* check whose setup is still pending (spec §5.2's second cause); the
    resume starts the mediator and the campaign completes."""

    from evolution.tree import campaign as campaign_module
    from tests.probe_fakes import ProbeHTTPServer
    from tests.tree.target.test_probe_session import config as probe_config

    started = campaign_module.ProbeMediator.start
    broken = {"on": True}

    async def start(self: Any) -> Any:
        if broken["on"]:
            raise OSError("listener could not start")
        return await started(self)

    rig = _v7_rig(tmp_path, name, sample_retries=0, schema=schema)
    with pytest.MonkeyPatch.context() as patch, ProbeHTTPServer() as server:
        patch.setattr(campaign_module.ProbeMediator, "start", start)
        probe = probe_config(server.base_url)
        assert (await rig.run(probe=probe)).terminal == "suspended"
        broken["on"] = False
        assert (await rig.resume(probe=probe)).terminal == "finished"
    statuses = [
        record["payload"]["status"]
        for record in rig.records()
        if record["event"] == "probe_checked"
    ]
    assert statuses == ["ready", "ready"]
    return rig


async def test_a_suspended_status_reports_the_suspension_the_window_and_the_totals(
    tmp_path: Path,
) -> None:
    rig = await suspended_threshold_rig(tmp_path)

    payload = rig.projection()

    assert payload["suspended"] is True
    (entry,) = payload["suspensions"]
    record = next(r for r in rig.records() if r["event"] == "campaign_suspended")
    assert entry == dict(
        seq=record["sequence"],
        reason="target_unstable",
        owner="target",
        evidence_kind="threshold",
    )
    indeterminate = payload["indeterminate"]
    # The window started at the suspension, so it is empty; the totals are not.
    assert indeterminate["counts"]["target"] >= 1
    assert indeterminate["window"] == dict(
        counts=dict.fromkeys(OWNERS, 0),
        denominator=0,
        start=record["sequence"],
    )
    # The result projection carries the same two keys.
    result = rig.result_payload()
    assert result["suspended"] is True and result["suspensions"] == payload["suspensions"]


async def test_a_resumed_completion_keeps_the_suspension_and_reports_not_suspended(
    tmp_path: Path,
) -> None:
    rig = await resumed_threshold_rig(tmp_path)

    payload = rig.projection()

    assert payload["suspended"] is False
    assert [entry["reason"] for entry in payload["suspensions"]] == ["target_unstable"]
    window = payload["indeterminate"]["window"]
    assert window["start"] == payload["suspensions"][0]["seq"]
    # The window counts only what the resume ran; the totals keep the crossing.
    assert window["denominator"] >= 1
    assert payload["indeterminate"]["denominator"] > window["denominator"]


async def test_the_result_reports_a_resumes_repair_tries(tmp_path: Path) -> None:
    """#78 (spec §3.6): the effect of a threshold resume is visible."""

    baseline_id = measurement_id_for(None)

    def baseline(payload: dict[str, Any]) -> dict[str, Any]:
        return next(e for e in payload["measurements"] if e["measurement_id"] == baseline_id)

    repaired = await resumed_threshold_rig(tmp_path, "repair-tries-healthy")
    dispatched = [r for r in repaired.records() if r["event"] == "repairs_dispatched"]
    assert len(dispatched) == 1
    owed = dispatched[0]["payload"]["count"]
    assert owed >= 1
    for payload in (repaired.projection(), repaired.result_payload()):
        entry = baseline(payload)
        assert entry["repair_tries"] == owed
        assert entry["repairs_scored"] == owed
        other = next(e for e in payload["measurements"] if e["measurement_id"] != baseline_id)
        assert (other["repair_tries"], other["repairs_scored"]) == (0, 0)

    broken = await unrepaired_threshold_rig(tmp_path, "repair-tries-broken")
    entry = baseline(broken.result_payload())
    assert entry["repair_tries"] >= 1
    assert entry["repairs_scored"] == 0


async def test_the_adaptive_state_payload_carries_a_suspended_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suspension (family 7 onward) marks the reflection it resolved; the compact
    adaptation state serialises reflections verbatim and keeps the flag.
    Families 6 and below never set it."""

    from evolution.tree import campaign_reflector
    from evolution.tree.campaign_projections import adaptive_state_payload
    from tests.campaign_fakes import ReflectorScript, ScriptedReflector

    monkeypatch.setattr(campaign_reflector, "EDITOR_GRACE_SECONDS", 0.05)
    rig = _v7_rig(tmp_path, "reflector-7", sample_retries=0, reflection_timeout_seconds=0.05)
    rig.reflector = ScriptedReflector({1: ReflectorScript("swallow_cancel", seconds=3600.0)})
    assert (await rig.run()).terminal == "suspended"

    state = adaptive_state_payload(inspect_view(rig.run_root).snapshot)

    (reflection,) = state["rounds"][0]["reflections"]
    assert reflection["error_class"] == "unresponsive" and reflection["suspended"] is True


# -- I2 Task 10: byte-identity with 0.3.0a17 for change-free runs -------------


# -- family 12: the per-Attempt `affected` block (G2b5 Task 1) -----------------


def _family12_documents(run: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    view = inspect_view(run)
    status = inspect_campaign(run, verify=True).to_payload()
    result = project_result(view.snapshot, tries=view.tries).to_payload()
    return status, result


@pytest.mark.slow
async def test_the_validator_refuses_a_malformed_affected_block(tmp_path: Path) -> None:
    import copy

    from tests.campaign_drivers import two_case_run as _two_case_run

    run = await _two_case_run(tmp_path)
    status, _ = _family12_documents(run)
    CampaignProjection(copy.deepcopy(status))
    for edit in (
        lambda block: block.update(layer_one=-1),
        lambda block: block.update(extra=1),
        lambda block: block.update(alpha_statement="nope"),
    ):
        forged = copy.deepcopy(status)
        block = next(
            a["affected"]
            for r in forged["rounds"]
            for a in r["attempts"]
            if a["affected"] is not None
        )
        edit(block)
        with pytest.raises(SchemaError):
            CampaignProjection(forged)


# -- family 12: the caveat vocabulary (G2b5 Task 2) -----------------------------


# -- H12b Task 6: the family-14 mark and suspension validation by family ----------
#
# Hand-edited documents: the family-13 golden status document (captured before
# any H12b change) and the same document moved to the family-14 shape (plan
# choice 2: `connection`, `roles` and `settings` at top level, `final` for
# `cohort`). Genuine family-14 documents are in
# `test_tree_campaign_family14_reports.py`.

_GROUPER_REASONS = ("grouper_provider_failed", "grouper_failing")
_CONNECTION_REASONS = (
    "connection_incompatible",
    "connection_preflight_unavailable",
    "connection_preflight_inconclusive",
)


def _family13_status() -> dict[str, Any]:
    """The family-13 golden, its two normalized top-level placeholders made concrete."""

    path = Path(__file__).parent / "goldens" / "reports"
    document = json.loads((path / "family13-cohort-incomplete-suspended.status.json").read_bytes())
    document.update(run_id="run-golden", identity_digest="0" * 64)
    return document


def _family14_status() -> dict[str, Any]:
    """The family-13 golden moved to the family-14 shape, before any final binding."""

    document = _family13_status()
    document["connection"] = {
        "state": "not_started",
        "binding_digest": None,
        "operation_id": None,
        "attempts": 0,
        "outcome": None,
        "detail": None,
    }
    document["roles"] = {
        "grouper": {"kind": "fields"},
        "reflector": {"kind": "command"},
        "editor": {"kind": "command"},
    }
    document["settings"] = {
        "stability": "unstable",
        "round_stop": "all_attempts",
        "baseline_refresh": "off",
    }
    for entry in document["rounds"]:
        del entry["cohort"]
        entry["final"] = []
        for attempt in entry["attempts"]:
            attempt["confidence"] = None
    # Only the campaign-level observe pair: the Attempt's method items named
    # family-13 kinds its (now null) block carried.
    document["caveats"] = [item for item in document["caveats"] if item["round"] is None]
    (suspension,) = document["suspensions"]
    suspension.update(reason="final_incomplete", evidence_kind="final_rows")
    # The planner's `RunCohortStage`, by its family-14 name (PDR-0028, F0 M4).
    (action,) = document["actions"]
    action["kind"] = "RunFinalStage"
    return document


def _suspended(document: dict[str, Any], reason: str, owner: str, kind: str) -> dict[str, Any]:
    edited = json.loads(json.dumps(document))
    edited["suspensions"][0].update(reason=reason, owner=owner, evidence_kind=kind)
    return edited


def test_the_hand_edited_documents_validate() -> None:
    CampaignProjection(_family13_status())
    CampaignProjection(_family14_status())


def test_the_connection_mark_requires_alpha_null() -> None:
    document = _family14_status()
    document["alpha"] = {"per_round": {}, "per_attempt": {}}
    with pytest.raises(SchemaError):
        CampaignProjection(document)


def test_the_round_block_follows_the_family_mark() -> None:
    family14 = _family14_status()
    family14["rounds"][0]["cohort"] = None
    with pytest.raises(SchemaError):
        CampaignProjection(family14)
    family14 = _family14_status()
    del family14["rounds"][0]["final"]
    family14["rounds"][0]["cohort"] = None
    with pytest.raises(SchemaError):
        CampaignProjection(family14)
    family14 = _family14_status()
    family14["rounds"][0]["final"] = None  # a list, never null (M5)
    with pytest.raises(SchemaError):
        CampaignProjection(family14)
    family13 = _family13_status()
    family13["rounds"][0]["final"] = []
    with pytest.raises(SchemaError):
        CampaignProjection(family13)


def test_roles_and_settings_need_the_connection_mark() -> None:
    family14 = _family14_status()
    for name in ("roles", "settings"):
        family13 = _family13_status()
        family13[name] = family14[name]
        with pytest.raises(SchemaError):
            CampaignProjection(family13)
    for name in ("connection", "roles", "settings"):
        edited = _family14_status()
        del edited[name]
        with pytest.raises(SchemaError):
            CampaignProjection(edited)


def test_a_family_14_attempt_block_is_never_family_13s() -> None:
    family13 = _family13_status()
    block = family13["rounds"][0]["attempts"][0]["confidence"]
    family14 = _family14_status()
    family14["rounds"][0]["attempts"][0]["confidence"] = block
    with pytest.raises(SchemaError):
        CampaignProjection(family14)


def test_a_family_14_document_admits_its_suspension_reasons() -> None:
    family14 = _family14_status()
    for reason in (*_GROUPER_REASONS, *_CONNECTION_REASONS):
        owner = "grouper" if reason in _GROUPER_REASONS else "connection"
        CampaignProjection(_suspended(family14, reason, owner, "stage_record"))
    for owner in OWNERS:
        CampaignProjection(_suspended(family14, "final_incomplete", owner, "final_rows"))
        CampaignProjection(_suspended(family14, "champion_incomplete", owner, "champion_rows"))
    for reason, owner, kind in (
        ("cohort_incomplete", "target", "cohort_rows"),
        ("final_incomplete", "target", "cohort_rows"),
        ("final_incomplete", "nobody", "final_rows"),
        ("grouper_failing", "reflector", "stage_record"),
        ("connection_incompatible", "target", "stage_record"),
    ):
        with pytest.raises(SchemaError):
            CampaignProjection(_suspended(family14, reason, owner, kind))


def test_a_family_14_status_names_its_actions_final() -> None:
    """PDR-0028's "final everywhere" (F0 M4): the planner keeps its class names,
    a family-14 status names them by the family-14 word, and family 13 keeps its own."""

    from dataclasses import fields

    from evolution.tree.campaign_projections import _action_payload
    from evolution.tree.campaign_view import (
        Action,
        BindCohortMember,
        CompleteCohortMember,
        DecideRound,
        RunCohortStage,
    )

    for action, final_kind in (
        (BindCohortMember(round=1, measurement_id="m-r01-final-01-c1"), "BindFinalMember"),
        (RunCohortStage(round=1, stage=1), "RunFinalStage"),
        (CompleteCohortMember(round=1, measurement_id="m-r01-final-01-c1"), "CompleteFinalMember"),
    ):
        family13 = _action_payload(action)
        assert family13["kind"] == type(action).__name__  # family 13 unchanged
        assert _action_payload(action, final=True) == {**family13, "kind": final_kind}
    # A kind without "Cohort" reads the same in both families.
    decide = DecideRound(round=1)
    assert _action_payload(decide, final=True) == _action_payload(decide)

    # Every planner class is covered: these three are the only ones that say
    # "Cohort", and no action field is named for the cohort.
    def planner_classes(base: type) -> list[type]:
        found = []
        for cls in base.__subclasses__():
            found += [cls, *planner_classes(cls)]
        return found

    classes = planner_classes(Action)
    cohort = sorted(cls.__name__ for cls in classes if "Cohort" in cls.__name__)
    assert cohort == ["BindCohortMember", "CompleteCohortMember", "RunCohortStage"]
    for cls in classes:
        assert not [field.name for field in fields(cls) if "cohort" in field.name.lower()]


def test_a_family_14_document_refuses_a_cohort_action_kind() -> None:
    family14 = _family14_status()
    assert [action["kind"] for action in family14["actions"]] == ["RunFinalStage"]
    family14["actions"][0]["kind"] = "RunCohortStage"
    with pytest.raises(SchemaError):
        CampaignProjection(family14)


def test_a_stable_family_14_document_carries_champion_not_contemporaneous() -> None:
    """PDR-0028: every `stable` result shows the caveat."""

    document = _family14_status()
    document["settings"]["stability"] = "stable"
    with pytest.raises(SchemaError):
        CampaignProjection(document)
    item = {"kind": "champion_not_contemporaneous", "round": None, "attempt_id": None}
    document["caveats"].append(item)
    CampaignProjection(document)
    # Admitted, not required, where the document does not show `stable`.
    document["settings"]["stability"] = "unstable"
    CampaignProjection(document)


async def invalid_projection_run(tmp_path, monkeypatch, terminal="finished", *, result_only=False):
    """Inject validator failure only after the real terminal is durable."""
    from evolution.tree import campaign, family15_report
    from evolution.tree.evolve import evolve
    from tests.campaign_drivers import TWO_CASE_SCORER, _records, _Resettable
    from tests.family15_fakes import independent_run
    from tests.test_tree_campaign_family15_condition_change import (
        _held_change_authorizations,
        _interrupted,
    )
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    adapter = _Resettable()

    def invalid(*args, **kwargs):
        raise SchemaError("invalid public projection")

    def inject(patch):
        if result_only:
            from evolution.tree.campaign_projections import ProjectionInvalid

            def invalid_result(*args, **kwargs):
                raise ProjectionInvalid("invalid public projection")

            patch.setattr(campaign, "project_result", invalid_result)
        else:
            patch.setattr(family15_report, "validate_campaign15_projection", invalid)

    if terminal == "owed":
        run = await _interrupted(tmp_path, monkeypatch, adapter, "after_baseline_complete")
        auth = await _held_change_authorizations(
            tmp_path / "manifest.json", run, adapter, effect="close"
        )
        hook = campaign._Campaign.hook

        def cut(self, name):
            hook(self, name)
            if name == "after_close_applied":
                raise GatePowerCut()

        with monkeypatch.context() as patch:
            patch.setattr(campaign._Campaign, "hook", cut)
            with pytest.raises(GatePowerCut):
                await evolve(
                    tmp_path / "manifest.json",
                    run,
                    adapter_factory=lambda *a: adapter,
                    authorizations=auth,
                )
        assert _records(run / "campaign.jsonl")[-1]["event"] == "authorization_applied"
        with monkeypatch.context() as patch:
            inject(patch)
            result = campaign.finish_owed_terminal(run)
        return run, result, adapter

    original = campaign._Campaign.result

    def result(self):
        records = _records(self.run_root / "campaign.jsonl")
        assert records[-1]["event"] == (
            "campaign_suspended" if terminal == "suspended" else "campaign_finished"
        )
        with monkeypatch.context() as patch:
            inject(patch)
            return original(self)

    options = {}
    if terminal == "suspended":
        options["scorer"] = TWO_CASE_SCORER.replace(
            "passed = ", 'if "edited" in view or "edited" in skill:\n    sys.exit(1)\npassed = '
        )
    with monkeypatch.context() as patch:
        patch.setattr(campaign._Campaign, "result", result)
        run, finished = await independent_run(tmp_path, patch, adapter=adapter, **options)
    return run, finished, adapter


@pytest.mark.parametrize("terminal", ["finished", "suspended", "owed"])
async def test_invalid_projection_after_terminal_is_projection_invalid(
    tmp_path, monkeypatch, terminal
):
    from tests.campaign_drivers import _records

    run, result, _ = await invalid_projection_run(tmp_path, monkeypatch, terminal)
    assert (result.code, result.owner, result.result) == (
        "projection_invalid",
        "campaign_projection",
        None,
    )
    assert result.terminal == ("suspended" if terminal == "suspended" else "finished")
    status = result.status.to_payload()
    assert status["run_id"] == _records(run / "campaign.jsonl")[0]["run_id"]
    assert status["measurements"] == [] and status["lineage"] is None
    assert "independent" not in status


async def test_inspected_status_survives_invalid_projection(tmp_path, monkeypatch):
    from evolution.tree import family15_report
    from evolution.tree.campaign import _inspected_status
    from tests.family15_fakes import independent_run

    run, result = await independent_run(tmp_path, monkeypatch)

    def invalid(*args, **kwargs):
        raise SchemaError("invalid public projection")

    monkeypatch.setattr(family15_report, "validate_campaign15_projection", invalid)
    status = _inspected_status(run, result.status.to_payload()["run_id"], ()).to_payload()
    assert status["run_id"] == result.status.to_payload()["run_id"]
    assert status["measurements"] == [] and status["lineage"] is None


async def test_rerun_after_fix_prints_full_result(tmp_path, monkeypatch):
    from evolution.tree.evolve import evolve, exit_code_for

    run, broken, adapter = await invalid_projection_run(tmp_path, monkeypatch)
    assert broken.code == "projection_invalid"
    before = (run / "campaign.jsonl").read_bytes()
    fixed = await evolve(tmp_path / "manifest.json", run, adapter_factory=lambda *a: adapter)
    assert fixed.code is None and fixed.result is not None
    assert fixed.status.to_payload()["independent"]["next_action"] == "finished"
    assert exit_code_for(fixed) == 0
    assert (run / "campaign.jsonl").read_bytes() == before


@pytest.mark.parametrize("terminal", ["finished", "owed"])
async def test_invalid_result_after_valid_status_is_projection_invalid(
    tmp_path, monkeypatch, terminal
):
    run, result, _ = await invalid_projection_run(tmp_path, monkeypatch, terminal, result_only=True)
    assert result.terminal == "finished"
    assert (result.code, result.owner, result.result) == (
        "projection_invalid",
        "campaign_projection",
        None,
    )
    assert result.status.to_payload()["run_id"]
    assert result.status.to_payload()["measurements"] == []


def test_minimal_status_projection_failure_does_not_recurse(tmp_path, monkeypatch):
    from evolution.tree import campaign_projections
    from evolution.tree.campaign import _inspected_status
    from evolution.tree.campaign_projections import ProjectionInvalid

    def invalid(payload):
        raise SchemaError("invalid public projection")

    monkeypatch.setattr(campaign_projections, "_validate", invalid)
    with pytest.raises(ProjectionInvalid):
        _inspected_status(tmp_path, "run", (), minimal=True)


async def test_projection_invalid_before_a_durable_terminal_propagates(tmp_path, monkeypatch):
    from evolution.tree import campaign
    from evolution.tree.campaign_projections import ProjectionInvalid
    from tests.family15_fakes import independent_run
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    failure = ProjectionInvalid("pre-terminal projection failure")
    original = campaign._Campaign.hook

    def cut(self, name):
        original(self, name)
        if name == "after_baseline_complete":
            assert self.snapshot().finished is None and self.suspended is None

            def invalid(*args, **kwargs):
                raise failure

            with monkeypatch.context() as patch:
                patch.setattr(campaign, "project_status", invalid)
                with pytest.raises(ProjectionInvalid) as refused:
                    self.result()
                assert refused.value is failure
            raise GatePowerCut()

    monkeypatch.setattr(campaign._Campaign, "hook", cut)
    with pytest.raises(GatePowerCut):
        await independent_run(tmp_path, monkeypatch)
