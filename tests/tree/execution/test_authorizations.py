"""`evolution.tree.execution.authorizations` (PDR-0010, ADR-0015; spec §3–§5, §7)."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.tree import campaign as campaign_module
from evolution.tree.cli import tree_app
from evolution.tree.execution import authorizations as authorizations_module
from evolution.tree.execution.authorizations import (
    AUTHORIZABLE_CODES,
    AUTHORIZATIONS_DIR,
    COMPARABLE_FIELDS,
    EFFECT_SENTENCES,
    HARD_CODES,
    HARD_REASONS,
    MAX_DECISIONS,
    SCHEMA,
    TEMPORARY_PREFIX,
    Authorization,
    AuthorizationConflict,
    AuthorizationError,
    RefusalDifference,
    baseline_import_instance_digest,
    comparable_changed,
    hint_command,
    hint_command_accept,
    hygiene_instance_digest,
    identity_differences,
    identity_instance_digest,
    identity_record,
    lineage_instance_digest,
    match,
    read_decisions,
    refusal_class,
    validate_issued_at,
    write_decision,
)
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.output_admission import EvolveRefused, RefusalFinding

DIGEST = "a" * 64


def created_payload(**identity_overrides: object) -> dict:
    identity = {
        "profile": "campaign",
        "policy": {"rounds": 1, "alpha": {"spend": {"3": "0.05"}}},
        "policy_digest": "b" * 64,
        "target": {"contract_digest": "c" * 64, "settings_hash": "d" * 64, "seed_mode": "exact"},
        "environment": {"kind": "static", "digest": "e" * 64},
        "scorer": {"kind": "subprocess", "digest": "f" * 64},
        "evaluation_set_id": "corpus-0123456789abcdef",
        "corpus_digest": "1" * 64,
        "corpus_cases": [{"case_id": "c1", "group_id": "g1"}],
        "metric_plan_digest": "2" * 64,
        "artifact_store": {"policy_digest": "3" * 64, "seed_candidate_digest": "4" * 64},
        "materialization_strategy": "artifact_root_injection",
        "capabilities": {"max_instances": 4, "max_concurrent_sessions": 2},
        "editor": {"kind": "subprocess", "digest": "5" * 64},
        "reflector": {
            "kind": "subprocess",
            "digest": "6" * 64,
            "environment_docs_digest": None,
            "probe": None,
        },
        "linked_from": None,
    }
    identity.update(identity_overrides)
    return {
        "identity": identity,
        "identity_digest": hashlib.sha256(canonical_json(identity)).hexdigest(),
        "producing_version": "0.3.0a11",
        "adaptation": {"version": "campaign_adaptation/1", "digest": "7" * 64},
    }


def test_comparable_fields_are_the_campaigns_own_tuple_plus_the_store() -> None:
    """§3's list is C1's comparability contract, not a second opinion about it."""

    assert set(COMPARABLE_FIELDS) == set(campaign_module._COMPARABLE) | {"artifact_store"}


def test_refusal_classes_are_closed() -> None:
    assert refusal_class("run_identity_changed") == "authorizable"
    assert refusal_class("run_dir_unsafe_entry") == "authorizable"
    assert refusal_class("baseline_import_unavailable") == "authorizable"
    for code in (
        "pin_mismatch",
        "program_inside_seed",
        "run_dir_overlaps_program",
        "attestation_missing",
        "journal_busy",
        "campaign_family_unsupported",
        "record_matches_secret",
        "capabilities_insufficient",
        "package_version_unavailable",
        "stored_identity_digest_mismatch",
        "adaptive_runtime_required",
        "authorizations_invalid",
        "authorization_invalid",
        "baseline_source_unavailable",
        "never_heard_of_it",
    ):
        assert refusal_class(code) == "hard", code


def test_lineage_refusals_are_hard_and_explained() -> None:
    """Continuable-terminals spec §6.3: the remaining lineage refusals are hard and explained.

    `lineage_not_finished` moved to `AUTHORIZABLE_CODES` (D2a §5.4): a family-5
    predecessor may be linked under a decision, so it is covered by its own
    test rather than asserted hard here.
    """

    expected = {
        "lineage_family_unsupported": (
            "The predecessor's campaign family cannot be linked by this version."
        ),
        "lineage_champion_unverified": (
            "The predecessor's champion copy does not match its artifact ledger."
        ),
        # Family-13 D10 (maintainer, E2): an `observe` predecessor under a
        # `statistical` successor.
        "lineage_mode_unsupported": (
            "The predecessor ran in observe mode, so its Champion cannot seed a "
            "statistical campaign."
        ),
    }
    # Ruling R16: the comparable-change sentence names only a door that opens.
    # Comparable-links §5.1: an unfinished campaign's comparable change is a
    # close, so the hard one is the finished campaign's, and its door is a link.
    assert HARD_REASONS["run_identity_changed"] == (
        "the campaign is finished and there is nothing left to continue; "
        "start a linked run that declares the new condition"
    )
    for code, sentence in expected.items():
        assert code in HARD_CODES
        assert refusal_class(code) == "hard"
        assert HARD_REASONS[code] == sentence
    assert "lineage_not_continuable" not in HARD_CODES


def test_baseline_source_unavailable_is_hard_with_the_manifest_door() -> None:
    """Baseline inheritance §5.0: nothing is declared yet, so the door is the manifest."""

    assert "baseline_source_unavailable" in HARD_CODES
    assert refusal_class("baseline_source_unavailable") == "hard"
    assert HARD_REASONS["baseline_source_unavailable"] == (
        "the predecessor's baseline cannot be read; declare "
        '"baseline": "fresh" in linked_from to measure it in full'
    )


def test_verbatim_answer_unmatched_is_hard_with_the_manifest_door() -> None:
    """ADR-0038: a declared answer equal to no oracle value is the manifest's to correct."""

    assert "verbatim_answer_unmatched" in HARD_CODES
    assert refusal_class("verbatim_answer_unmatched") == "hard"
    assert HARD_REASONS["verbatim_answer_unmatched"] == (
        "a declared verbatim answer equals no oracle value of the train cases; "
        "correct it or remove it"
    )


def test_lineage_not_finished_is_authorizable_and_the_condition_code_is_gone() -> None:
    assert "lineage_not_finished" in AUTHORIZABLE_CODES
    assert "lineage_not_finished" not in HARD_CODES
    assert "lineage_condition_changed" not in AUTHORIZABLE_CODES | HARD_CODES
    assert "lineage_condition_changed" not in HARD_REASONS
    # The hard variant (an unfinished family-6 predecessor) keeps a sentence.
    assert "lineage_not_finished" in HARD_REASONS


def test_decision_payload_round_trips_and_binds_the_id() -> None:
    decision = Authorization.new(
        "run_identity_changed", "a" * 64, "scorer v2", now="2026-09-18T10:00:00Z"
    )
    payload = decision.decision_payload()
    assert set(payload) == {"id", "code", "digest", "reason", "issued_at", "source"}
    assert Authorization.from_decision_payload(payload) == decision
    forged = dict(payload, reason="other")
    with pytest.raises(AuthorizationError) as caught:
        Authorization.from_decision_payload(forged)
    assert caught.value.path == "id"


def test_lineage_instance_digest_binds_run_and_prefix() -> None:
    one = lineage_instance_digest("run-1", "b" * 64)
    assert one != lineage_instance_digest("run-1", "c" * 64)
    assert one != lineage_instance_digest("run-2", "b" * 64)


def test_effect_sentences_are_the_spec_text() -> None:
    assert EFFECT_SENTENCES["close"] == (
        "accepting closes this campaign as stopped(condition_changed);",
        "continue it with a linked run that declares the new condition",
    )
    assert EFFECT_SENTENCES["link"] == (
        "the predecessor is unfinished and will not be closed; if it is ever resumed,",
        "its rounds and this run's spend the same alpha",
    )
    assert EFFECT_SENTENCES["terminal"] == (
        "accepting writes the terminal this campaign already owes;",
        "continue it with a linked run that declares the new condition",
    )
    assert EFFECT_SENTENCES["fresh_baseline"] == (
        "accepting measures this run's baseline in full instead of carrying",
        "the predecessor's rows",
    )


def test_effect_sentences_gain_continue_and_rebaseline() -> None:
    """I2 §3.1, §4.1: the two in-place effects a family-7 instance may offer beside `close`."""

    assert EFFECT_SENTENCES["continue"] == (
        "accepting keeps this campaign and its measurements; unfinished measurements "
        "continue under the new condition",
    )
    assert EFFECT_SENTENCES["rebaseline"] == (
        "accepting closes the open round without promotion; the next round starts "
        "from a fresh baseline under the new condition",
    )


def test_i2_hard_codes_and_reasons() -> None:
    """I2 §3.4, §5.4, §4.2: the three new hard refusals, each naming its own door."""

    for code in (
        "policy_contradicts_prefix",
        "authorized_identity_changed",
        "authorization_conflict",
    ):
        assert code in HARD_CODES
        assert refusal_class(code) == "hard"
        assert code in HARD_REASONS
    assert HARD_REASONS["policy_contradicts_prefix"] == (
        "the new policy asks for less than this run has already spent; "
        "correct the value, or accept close"
    )
    assert HARD_REASONS["authorized_identity_changed"] == (
        "the manifest or the package version changed after the accepted decision "
        "(the differs: lines name which); restore the manifest and the package "
        "version that wrote the decision, finish the transition, then present "
        "the new change as its own decision"
    )
    assert HARD_REASONS["authorization_conflict"] == (
        "more than one decision names this refusal with different effects; remove all but one"
    )


def test_baseline_import_instance_digest_binds_code_run_and_pairs() -> None:
    """Baseline inheritance §5.3: the code, the run's run_id and the declared pairs_digest."""

    one = baseline_import_instance_digest("run-1", "b" * 64)
    assert (
        one
        == hashlib.sha256(
            canonical_json(
                {"code": "baseline_import_unavailable", "run_id": "run-1", "pairs_digest": "b" * 64}
            )
        ).hexdigest()
    )
    assert one != baseline_import_instance_digest("run-1", "c" * 64)
    assert one != baseline_import_instance_digest("run-2", "b" * 64)


def test_accept_hint_names_every_finding_once() -> None:
    line = hint_command_accept(
        "runs/r 1", [("run_dir_unsafe_entry", "a" * 64), ("run_identity_changed", "b" * 64)]
    )
    assert line == (
        "evo tree authorize 'runs/r 1' --accept run_dir_unsafe_entry:"
        + "a" * 64
        + " --accept run_identity_changed:"
        + "b" * 64
        + ' --reason "<why>"'
    )


def test_evolve_refused_carries_its_own_finding_and_a_collected_tuple() -> None:
    single = EvolveRefused("run_identity_changed", instance_digest="a" * 64, effect="close")
    assert len(single.findings) == 1 and single.findings[0].effect == "close"
    hygiene = RefusalFinding(
        "run_dir_unsafe_entry", "scorer-traces", "not owner-only", (), "b" * 64, "hygiene"
    )
    collected = EvolveRefused.from_findings([hygiene, single.findings[0]])
    assert (collected.code, collected.path, collected.instance_digest) == (
        "run_dir_unsafe_entry",
        "scorer-traces",
        "b" * 64,
    )
    assert str(collected) == "run_dir_unsafe_entry at scorer-traces"
    assert [f.code for f in collected.findings] == ["run_dir_unsafe_entry", "run_identity_changed"]


def test_identity_differences_name_dotted_fields_and_digests_only() -> None:
    before = identity_record(created_payload())
    after = identity_record(created_payload(editor={"kind": "subprocess", "digest": "9" * 64}))
    after["producing_version"] = "0.3.0a12"
    differences = identity_differences(before, after)
    fields = [d.field for d in differences]
    assert fields == ["identity.editor.digest", "producing_version"]
    assert differences[0] == RefusalDifference("identity.editor.digest", "5" * 64, "9" * 64)
    assert differences[1] == RefusalDifference("producing_version", "0.3.0a11", "0.3.0a12")
    assert not comparable_changed(differences)


def test_corpus_order_is_one_difference_of_two_list_digests() -> None:
    before = identity_record(created_payload())
    after = identity_record(created_payload(corpus_cases=[{"case_id": "c2", "group_id": "g1"}]))
    differences = identity_differences(before, after)
    assert [d.field for d in differences] == ["identity.corpus_cases"]
    assert (
        differences[0].journaled
        == hashlib.sha256(canonical_json(before["identity"]["corpus_cases"])).hexdigest()
    )
    assert "c1" not in json.dumps([d.__dict__ for d in differences])
    assert comparable_changed(differences)


def test_comparable_fields_include_the_artifact_store_and_policy() -> None:
    before = identity_record(created_payload())
    for field, value in (
        ("policy_digest", "0" * 64),
        ("scorer", {"kind": "subprocess", "digest": "0" * 64}),
        ("artifact_store", {"policy_digest": "0" * 64, "seed_candidate_digest": "4" * 64}),
    ):
        after = identity_record(created_payload(**{field: value}))
        assert comparable_changed(identity_differences(before, after)), field


def test_the_identity_instance_digest_binds_both_sides() -> None:
    a = identity_record(created_payload())
    b = identity_record(created_payload(editor={"kind": "subprocess", "digest": "9" * 64}))
    c = identity_record(created_payload(editor={"kind": "subprocess", "digest": "8" * 64}))
    assert identity_instance_digest(a, b) != identity_instance_digest(c, b)
    assert identity_instance_digest(a, b) == identity_instance_digest(a, b)
    assert len(identity_instance_digest(a, b)) == 64


def test_the_hygiene_instance_digest_binds_the_observed_fact() -> None:
    one = hygiene_instance_digest("scorer-traces", "not owner-only", "0o755")
    other = hygiene_instance_digest("scorer-traces", "not owner-only", "0o775")
    link = hygiene_instance_digest("scorer-traces", "symlinked", "/elsewhere")
    assert len({one, other, link}) == 3
    with pytest.raises(ValueError):
        hygiene_instance_digest("scorer-traces", "unknown class", "x")


def test_authorization_id_is_derived_from_the_four_stored_fields() -> None:
    auth = Authorization.new(
        "run_identity_changed", DIGEST, "fixed the prompt", now="2026-09-17T10:00:00Z"
    )
    same = Authorization(
        "run_identity_changed", DIGEST, "fixed the prompt", "2026-09-17T10:00:00Z", source="file"
    )
    assert auth.id == same.id
    # Spec §5's algorithm exactly: the four decided fields, and not `schema`.
    # An external writer computes the same id from the same decision file only
    # if core hashes what the spec says it hashes.
    assert (
        auth.id
        == hashlib.sha256(
            canonical_json(
                {
                    "code": "run_identity_changed",
                    "digest": DIGEST,
                    "reason": "fixed the prompt",
                    "issued_at": "2026-09-17T10:00:00Z",
                }
            )
        ).hexdigest()
    )
    assert auth.id != hashlib.sha256(canonical_json(auth.to_payload())).hexdigest()
    assert auth.to_payload() == {
        "schema": SCHEMA,
        "code": "run_identity_changed",
        "digest": DIGEST,
        "reason": "fixed the prompt",
        "issued_at": "2026-09-17T10:00:00Z",
    }
    assert Authorization.from_payload(auth.to_payload(), source="file") == same


def test_decision_id_without_effect_is_unchanged() -> None:
    """I2 §4.2: a decision that does not set `effect` hashes exactly as before I2."""

    a = Authorization("run_identity_changed", DIGEST, "switch provider", "2026-09-23T10:00:00Z")
    legacy = hashlib.sha256(
        canonical_json(
            {
                "code": a.code,
                "digest": a.digest,
                "reason": a.reason,
                "issued_at": a.issued_at,
            }
        )
    ).hexdigest()
    assert a.id == legacy


def test_decision_id_with_effect_includes_it() -> None:
    base = dict(
        code="run_identity_changed", digest=DIGEST, reason="r", issued_at="2026-09-23T10:00:00Z"
    )
    assert Authorization(**base, effect="continue").id != Authorization(**base).id
    assert (
        Authorization(**base, effect="continue").id != Authorization(**base, effect="rebaseline").id
    )


def test_effect_round_trips_through_the_file(tmp_path: Path) -> None:
    written = Authorization.new(
        "run_identity_changed", DIGEST, "r", now="2026-09-23T10:00:00Z", effect="rebaseline"
    )
    write_decision(tmp_path / "run", written)
    (read,) = read_decisions(tmp_path / "run")
    assert read.effect == "rebaseline"
    assert read.id == written.id


def test_effect_round_trips_through_decision_payload() -> None:
    decision = Authorization.new("run_identity_changed", DIGEST, "r", effect="close")
    payload = decision.decision_payload()
    assert payload["effect"] == "close"
    assert Authorization.from_decision_payload(payload) == decision
    # A decision that never set `effect` keeps `decision_payload` as before.
    legacy = Authorization.new("run_identity_changed", DIGEST, "r")
    assert "effect" not in legacy.decision_payload()


def test_journal_payload_never_carries_effect() -> None:
    """Review round 1, ruling R4: `journal_payload` drops `effect` unconditionally.

    `id` already binds `effect` when it is set, so nothing readers of this
    slot need is lost; `journal_payload` is what `identity_rebased` and the
    hygiene `authorization_applied` shape journal, and
    `campaign_schema._validate_authorization`'s closed four-key set
    (`id`, `reason`, `issued_at`, `source`) is unrelated to I2 and unchanged.
    """

    from evolution.tree.campaign_schema import _validate_authorization

    decision = Authorization.new(
        "run_identity_changed", DIGEST, "r", now="2026-09-23T10:00:00Z", effect="continue"
    )
    journaled = decision.journal_payload()
    assert set(journaled) == {"id", "reason", "issued_at", "source"}
    assert "effect" not in journaled
    _validate_authorization(journaled)  # does not raise SchemaError

    # A decision that never set `effect` journals the same four keys -- only
    # `id` differs, because `effect` still joins the *hashed* fields (§4.2).
    legacy = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-23T10:00:00Z")
    assert set(legacy.journal_payload()) == set(journaled)
    assert legacy.journal_payload()["id"] != journaled["id"]


def test_resolved_effect() -> None:
    base = dict(
        code="run_identity_changed", digest=DIGEST, reason="r", issued_at="2026-09-23T10:00:00Z"
    )
    assert Authorization(**base).resolved_effect(("continue", "rebaseline", "close")) == "close"
    assert (
        Authorization(**base, effect="default").resolved_effect(("rebaseline", "close"))
        == "rebaseline"
    )
    assert Authorization(**base, effect="close").resolved_effect(("continue", "close")) == "close"
    for effect in (None, "default", "close"):
        with pytest.raises(ValueError, match="offered effects"):
            Authorization(**base, effect=effect).resolved_effect(())


def test_from_payload_refuses_a_bad_effect() -> None:
    with pytest.raises(AuthorizationError) as failure:
        Authorization.from_payload(
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "r",
                "issued_at": "2026-09-17T10:00:00Z",
                "effect": "rebase",
            },
            source="file",
        )
    assert failure.value.code == "authorization_invalid" and failure.value.path == "effect"


def test_from_decision_payload_refuses_an_effect_that_does_not_match_the_id() -> None:
    decision = Authorization.new("run_identity_changed", DIGEST, "r", effect="continue")
    tampered = dict(decision.decision_payload(), effect="close")
    with pytest.raises(AuthorizationError) as failure:
        Authorization.from_decision_payload(tampered)
    assert failure.value.path == "id"


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        (
            {
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "r",
                "issued_at": "2026-09-17T10:00:00Z",
            },
            "schema",
        ),
        (
            {
                "schema": SCHEMA,
                "code": "pin_mismatch",
                "digest": DIGEST,
                "reason": "r",
                "issued_at": "2026-09-17T10:00:00Z",
            },
            "code",
        ),
        (
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": "short",
                "reason": "r",
                "issued_at": "2026-09-17T10:00:00Z",
            },
            "digest",
        ),
        (
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "",
                "issued_at": "2026-09-17T10:00:00Z",
            },
            "reason",
        ),
        (
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "r" * 1001,
                "issued_at": "2026-09-17T10:00:00Z",
            },
            "reason",
        ),
        (
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "r",
                "issued_at": "yesterday",
            },
            "issued_at",
        ),
        (
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "r",
                "issued_at": "2026-09-17T10:00:00Z",
                "extra": 1,
            },
            "extra",
        ),
    ],
)
def test_from_payload_refuses_bad_shapes_at_the_key(payload: dict, path: str) -> None:
    with pytest.raises(AuthorizationError) as failure:
        Authorization.from_payload(payload, source="file")
    assert failure.value.code == "authorization_invalid"
    assert failure.value.path == path


def test_match_is_by_code_and_digest() -> None:
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")
    assert match([auth], "run_identity_changed", DIGEST) is auth
    assert match([auth], "run_identity_changed", "b" * 64) is None
    assert match([auth], "run_dir_unsafe_entry", DIGEST) is None


def test_match_still_returns_the_first_of_several_agreeing_decisions() -> None:
    """D2a §6: unchanged -- several matches that agree admit as today, the first wins."""

    first = Authorization.new("run_identity_changed", DIGEST, "a", now="2026-09-17T10:00:00Z")
    second = Authorization.new("run_identity_changed", DIGEST, "b", now="2026-09-17T11:00:00Z")
    assert match([first, second], "run_identity_changed", DIGEST) is first
    # `None` and an explicit `"close"` both resolve to the same meaning, so two
    # decisions that agree that way are not a conflict either.
    explicit_close = Authorization.new(
        "run_identity_changed", DIGEST, "c", now="2026-09-17T12:00:00Z", effect="close"
    )
    assert match([first, explicit_close], "run_identity_changed", DIGEST) is first


def test_match_raises_authorization_conflict_on_disagreeing_effects() -> None:
    base = dict(
        code="run_identity_changed", digest=DIGEST, reason="r", issued_at="2026-09-23T10:00:00Z"
    )
    continuing = Authorization(**base, effect="continue")
    closing = Authorization(**base, effect="close")
    with pytest.raises(AuthorizationConflict) as failure:
        match([continuing, closing], "run_identity_changed", DIGEST)
    assert failure.value.ids == (continuing.id, closing.id)
    assert len(failure.value.ids) == 2
    assert str(failure.value) == "authorization_conflict"


def test_match_with_offered_compares_what_the_decisions_do() -> None:
    """Ruling R5 (I2 Task 7): with the instance's effects known, only resolved effects conflict."""

    base = dict(
        code="run_identity_changed", digest=DIGEST, reason="r", issued_at="2026-09-23T10:00:00Z"
    )
    default = Authorization(**base, effect="default")
    continuing = Authorization(**base, effect="continue")
    legacy = Authorization(**base)
    closing = Authorization(**base, effect="close")
    offered = ("continue", "rebaseline", "close")
    assert match([default, continuing], "run_identity_changed", DIGEST, offered=offered) is default
    assert match([legacy, closing], "run_identity_changed", DIGEST, offered=offered) is legacy
    with pytest.raises(AuthorizationConflict) as failure:
        match([default, legacy, closing], "run_identity_changed", DIGEST, offered=offered)
    assert failure.value.ids == (default.id, legacy.id, closing.id)
    # Without `offered` the comparison stays the fail-safe raw one.
    with pytest.raises(AuthorizationConflict):
        match([default, continuing], "run_identity_changed", DIGEST)


def test_a_legacy_decision_keeps_the_single_effect_of_its_instance() -> None:
    """A decision without `effect` means `close` where `close` is offered, and
    otherwise the one effect its instance had before I2 (a rebase, a terminal)."""

    base = dict(
        code="run_identity_changed", digest=DIGEST, reason="r", issued_at="2026-09-23T10:00:00Z"
    )
    assert Authorization(**base).resolved_effect(("rebase",)) == "rebase"
    assert Authorization(**base).resolved_effect(("terminal",)) == "terminal"
    assert Authorization(**base).resolved_effect(("continue", "close")) == "close"
    assert Authorization(**base, effect="default").resolved_effect(("rebase",)) == "rebase"


def test_rebound_source_corrupt_is_hard_with_its_door() -> None:
    assert "rebound_source_corrupt" in HARD_CODES
    assert refusal_class("rebound_source_corrupt") == "hard"
    assert HARD_REASONS["rebound_source_corrupt"] == (
        "a frozen measurement changed after the condition change; restore it from backup "
        "-- the run directory cannot continue until it matches its declaration"
    )


def test_write_then_read_decisions_round_trips_with_private_modes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    auth = Authorization.new(
        "run_identity_changed", DIGEST, "fixed the prompt", now="2026-09-17T10:00:00Z"
    )
    path = write_decision(run_dir, auth)
    assert path == run_dir / AUTHORIZATIONS_DIR / f"{auth.id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not any(name.startswith(".") for name in os.listdir(path.parent))
    (decisions,) = read_decisions(run_dir)
    assert decisions == Authorization(
        auth.code, auth.digest, auth.reason, auth.issued_at, source="file"
    )
    # The file name is a convenience: the same content under another name is the same decision.
    (path.parent / "renamed.json").write_bytes(path.read_bytes())
    assert len(read_decisions(run_dir)) == 1


def test_read_decisions_is_empty_without_the_directory(tmp_path: Path) -> None:
    assert read_decisions(tmp_path / "run") == ()


@pytest.mark.parametrize("kind", ["unknown_key", "not_json", "symlink", "subdirectory", "too_big"])
def test_read_decisions_refuses_a_malformed_entry_by_name(tmp_path: Path, kind: str) -> None:
    run_dir = tmp_path / "run"
    directory = run_dir / AUTHORIZATIONS_DIR
    directory.mkdir(parents=True, mode=0o700)
    good = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")
    write_decision(run_dir, good)
    if kind == "unknown_key":
        (directory / "bad.json").write_text(json.dumps({**good.to_payload(), "extra": 1}))
    elif kind == "not_json":
        (directory / "bad.json").write_text("{")
    elif kind == "symlink":
        (directory / "bad.json").symlink_to(directory / f"{good.id}.json")
    elif kind == "subdirectory":
        (directory / "bad.json").mkdir()
    else:
        (directory / "bad.json").write_text(
            json.dumps({**good.to_payload(), "reason": "x" * 70000})
        )
    with pytest.raises(AuthorizationError) as failure:
        read_decisions(run_dir)
    assert failure.value.code == "authorizations_invalid"
    assert failure.value.path == "bad.json"


def test_read_decisions_bounds_the_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    directory = run_dir / AUTHORIZATIONS_DIR
    directory.mkdir(parents=True, mode=0o700)
    for index in range(MAX_DECISIONS + 1):
        (directory / f"{index}.json").write_text("{}")
    with pytest.raises(AuthorizationError) as failure:
        read_decisions(run_dir)
    assert failure.value.code == "authorizations_invalid"
    assert failure.value.path == AUTHORIZATIONS_DIR


def test_write_decision_refuses_a_missing_parent_and_creates_nothing(tmp_path: Path) -> None:
    """§7 and `evolve`'s own rule: the parent is the operator's to create."""

    run_dir = tmp_path / "absent" / "run"
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")

    with pytest.raises(AuthorizationError) as failure:
        write_decision(run_dir, auth)

    assert failure.value.code == "authorization_invalid"
    assert failure.value.path == "run_dir"
    assert os.listdir(tmp_path) == []


def test_write_decision_creates_an_absent_run_dir_owner_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")

    write_decision(run_dir, auth)

    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / AUTHORIZATIONS_DIR).stat().st_mode) == 0o700


def test_write_decision_refuses_a_symlinked_authorizations_directory(tmp_path: Path) -> None:
    """PDR-0010: the decision is written into the run directory, or not at all.

    `os.mkdir` tolerating `FileExistsError` is how a second decision joins the
    first, but an `authorizations` entry that already exists as a symlink would
    otherwise carry the `chmod`, the staged file and the rename straight to
    whatever it points at (codex round 1).
    """

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (run_dir / AUTHORIZATIONS_DIR).symlink_to(elsewhere)
    before = stat.S_IMODE(elsewhere.stat().st_mode)
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")

    with pytest.raises(AuthorizationError) as failure:
        write_decision(run_dir, auth)

    assert failure.value.code == "authorization_invalid"
    assert failure.value.path == AUTHORIZATIONS_DIR
    # Nothing at the link target: no decision, no temporary file, no new mode.
    assert os.listdir(elsewhere) == []
    assert stat.S_IMODE(elsewhere.stat().st_mode) == before


def test_write_decision_refuses_an_authorizations_entry_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / AUTHORIZATIONS_DIR).write_text("not a directory\n", encoding="utf-8")
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")

    with pytest.raises(AuthorizationError) as failure:
        write_decision(run_dir, auth)

    assert (failure.value.code, failure.value.path) == ("authorization_invalid", AUTHORIZATIONS_DIR)
    assert (run_dir / AUTHORIZATIONS_DIR).read_text(encoding="utf-8") == "not a directory\n"


def test_write_decision_follows_a_symlinked_run_dir(tmp_path: Path) -> None:
    """A symlinked *run directory* is the operator's own arrangement (§3).

    `evolve` admits it under a `run_dir_unsafe_entry` decision, and that
    decision has to be writable into the directory it names, so `write_decision`
    follows the link and only requires `authorizations/` beneath it to be real.
    """

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    auth = Authorization.new("run_dir_unsafe_entry", DIGEST, "my box", now="2026-09-17T10:00:00Z")

    written = write_decision(link, auth)

    assert written.exists()
    assert (real / AUTHORIZATIONS_DIR / f"{auth.id}.json").exists()


def test_write_decision_is_anchored_to_the_directory_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDR-0010: the checked directory and the written-to directory are one.

    `authorizations/` is opened once, with `O_NOFOLLOW`, and the ownership
    check, the `0o700`, the staged file and the rename all go through that one
    descriptor. Here the directory is renamed away and a symlink to somewhere
    else is put in its place *after* the open -- the path-based writer would
    have carried the decision to the link's target, while the descriptor still
    names the directory that was checked (codex round 2).
    """

    run_dir = tmp_path / "run"
    directory = run_dir / AUTHORIZATIONS_DIR
    directory.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")
    real = authorizations_module.canonical_json
    swapped: list[bool] = []

    def swap(payload: object) -> bytes:
        if not swapped:
            swapped.append(True)
            directory.rename(tmp_path / "moved")
            directory.symlink_to(elsewhere)
        return real(payload)  # type: ignore[arg-type]

    monkeypatch.setattr(authorizations_module, "canonical_json", swap)

    write_decision(run_dir, auth)

    monkeypatch.undo()
    assert swapped == [True]
    assert os.listdir(elsewhere) == []
    assert os.listdir(tmp_path / "moved") == [f"{auth.id}.json"]


def test_read_decisions_skips_a_crashed_writers_temporary_file(tmp_path: Path) -> None:
    """A crash between `mkstemp` and the rename must not brick every later run."""

    run_dir = tmp_path / "run"
    auth = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")
    write_decision(run_dir, auth)
    leftover = run_dir / AUTHORIZATIONS_DIR / f"{TEMPORARY_PREFIX}abc123"
    leftover.write_text("{ half a decision", encoding="utf-8")

    (decision,) = read_decisions(run_dir)

    assert decision.id == auth.id


def test_hint_command_prints_the_operators_run_dir_verbatim() -> None:
    assert hint_command("./runs/r1", "run_identity_changed", DIGEST) == (
        f'evo tree authorize ./runs/r1 run_identity_changed --digest {DIGEST} --reason "<why>"'
    )


def test_hint_command_quotes_a_run_dir_the_shell_would_split() -> None:
    """The line is meant to be pasted, so a path with a space stays one word."""

    line = hint_command("runs/my run", "run_dir_unsafe_entry", DIGEST)
    assert line.startswith("evo tree authorize 'runs/my run' run_dir_unsafe_entry ")
    assert shlex.split(line)[:3] == ["evo", "tree", "authorize"]
    assert shlex.split(line)[3] == "runs/my run"


# -- the CLI: `evo tree authorize` (spec §4, §7) -------------------------------


def test_authorize_writes_one_decision_and_lists_it_pending(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "run_identity_changed",
            "--digest",
            DIGEST,
            "--reason",
            "prompt v2",
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    (decision,) = read_decisions(run_dir)
    assert decision.code == "run_identity_changed" and decision.reason == "prompt v2"
    # The one line on stdout is the decision's id: what `--list` will key on.
    assert invocation.stdout.strip() == decision.id
    listed = CliRunner().invoke(tree_app, ["authorize", str(run_dir), "--list"])
    assert listed.exit_code == 0, listed.output
    row = json.loads(listed.stdout.strip().splitlines()[0])
    assert row == {
        "id": decision.id,
        "code": "run_identity_changed",
        "digest": DIGEST,
        "reason": "prompt v2",
        "issued_at": decision.issued_at,
        "status": "pending",
    }


def test_authorize_positional_form_writes_default_and_explicit_effects(tmp_path: Path) -> None:
    """Review round 1, ruling R4: the positional form behaves like `--accept` (spec §4.2).

    `journal_payload()` never carries `effect` (it is dropped there
    unconditionally), so a `run_identity_changed` decision written this way is
    safe for every instance, comparable or not -- `--effect` is no longer
    scoped to `--accept` alone.
    """

    run_dir = tmp_path / "run"
    defaulted = CliRunner().invoke(
        tree_app,
        ["authorize", str(run_dir), "run_identity_changed", "--digest", DIGEST, "--reason", "r"],
    )
    assert defaulted.exit_code == 0, defaulted.output
    (decision,) = read_decisions(run_dir)
    assert decision.effect == "default"

    other_digest = "b" * 64
    explicit = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "run_identity_changed",
            "--digest",
            other_digest,
            "--reason",
            "r",
            "--effect",
            "continue",
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    assert {d.effect for d in read_decisions(run_dir)} == {"default", "continue"}


def test_authorize_positional_form_refuses_effect_beside_another_code(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "run_dir_unsafe_entry",
            "--digest",
            DIGEST,
            "--reason",
            "r",
            "--effect",
            "continue",
        ],
    )
    assert invocation.exit_code == 2
    assert (
        invocation.stderr.strip()
        == "evo tree authorize: refused: authorization_invalid at --effect"
    )
    assert not run_dir.exists()


def test_authorize_accept_writes_default_and_explicit_effects(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    written = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "--accept",
            f"run_identity_changed:{DIGEST}",
            "--reason",
            "switch",
        ],
    )
    assert written.exit_code == 0, written.output
    (decision,) = read_decisions(run_dir)
    assert decision.effect == "default"

    other_digest = "b" * 64
    explicit = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "--accept",
            f"run_identity_changed:{other_digest}",
            "--effect",
            "close",
            "--reason",
            "x",
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    assert {d.effect for d in read_decisions(run_dir)} == {"default", "close"}


def test_authorize_accept_refuses_a_bad_effect_value(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "--accept",
            f"run_identity_changed:{DIGEST}",
            "--effect",
            "rebase",
            "--reason",
            "x",
        ],
    )
    assert invocation.exit_code == 2
    assert (
        invocation.stderr.strip()
        == "evo tree authorize: refused: authorization_invalid at --effect"
    )
    assert not run_dir.exists()


def test_effect_beside_another_code_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "--accept",
            f"run_dir_unsafe_entry:{DIGEST}",
            "--effect",
            "continue",
            "--reason",
            "x",
        ],
    )
    assert invocation.exit_code == 2
    assert (
        invocation.stderr.strip()
        == "evo tree authorize: refused: authorization_invalid at --effect"
    )
    assert not run_dir.exists()


def test_effect_applies_to_every_run_identity_changed_item_on_the_line(tmp_path: Path) -> None:
    """§4.2: with `--accept` repeated, one `--effect` applies to every `run_identity_changed` item."""

    run_dir = tmp_path / "run"
    other_digest = "b" * 64
    invocation = CliRunner().invoke(
        tree_app,
        [
            "authorize",
            str(run_dir),
            "--accept",
            f"run_identity_changed:{DIGEST}",
            "--accept",
            f"run_identity_changed:{other_digest}",
            "--effect",
            "rebaseline",
            "--reason",
            "x",
        ],
    )
    assert invocation.exit_code == 0, invocation.output
    assert {d.effect for d in read_decisions(run_dir)} == {"rebaseline"}


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        (
            ["attestation_missing", "--digest", DIGEST, "--reason", "r"],
            "evo tree authorize: refused: authorization_invalid at code",
        ),
        (
            ["run_identity_changed", "--digest", "nope", "--reason", "r"],
            "evo tree authorize: refused: authorization_invalid at digest",
        ),
        (
            ["run_identity_changed", "--digest", DIGEST, "--reason", ""],
            "evo tree authorize: refused: authorization_invalid at reason",
        ),
        (
            ["run_identity_changed", "--reason", "r"],
            "evo tree authorize: refused: authorization_invalid at digest",
        ),
        (
            ["--reason", "r"],
            "evo tree authorize: refused: authorization_invalid at code",
        ),
        (
            ["run_identity_changed", "--list"],
            "evo tree authorize: refused: authorization_invalid at --list",
        ),
    ],
)
def test_authorize_refuses_a_hard_code_or_a_malformed_decision(
    tmp_path: Path, argv: list[str], line: str
) -> None:
    invocation = CliRunner().invoke(tree_app, ["authorize", str(tmp_path / "run"), *argv])
    assert invocation.exit_code == 2
    assert invocation.stderr.strip() == line
    assert not (tmp_path / "run").exists()


def test_authorize_lists_in_issue_order_and_refuses_a_malformed_directory(tmp_path: Path) -> None:
    """`--list` is an ordered report over the directory `read_decisions` guards."""

    run_dir = tmp_path / "run"
    later = Authorization.new("run_identity_changed", DIGEST, "later", now="2026-09-17T11:00:00Z")
    earlier = Authorization.new(
        "run_dir_unsafe_entry", DIGEST, "earlier", now="2026-09-17T10:00:00Z"
    )
    write_decision(run_dir, later)
    write_decision(run_dir, earlier)

    listed = CliRunner().invoke(tree_app, ["authorize", str(run_dir), "--list"])

    assert listed.exit_code == 0, listed.output
    rows = [json.loads(line) for line in listed.stdout.strip().splitlines()]
    assert [row["reason"] for row in rows] == ["earlier", "later"]
    assert [row["status"] for row in rows] == ["pending", "pending"]

    (run_dir / AUTHORIZATIONS_DIR / "bad.json").write_text("{", encoding="utf-8")
    broken = CliRunner().invoke(tree_app, ["authorize", str(run_dir), "--list"])
    assert broken.exit_code == 2
    assert broken.stderr.strip() == (
        "evo tree authorize: refused: authorizations_invalid at bad.json"
    )


@pytest.mark.parametrize("kind", ["symlink", "subdirectory"])
def test_read_decisions_validates_a_temporary_prefixed_entry(tmp_path: Path, kind: str) -> None:
    """ADR-0015: the prefix exempts a staged file, never an arbitrary name.

    `write_decision` can only ever leave a regular file this user owns behind.
    A symlink or a subdirectory wearing the same prefix is somebody else's
    entry, and the directory's validation contract covers every entry it can
    see (codex round 4).
    """

    run_dir = tmp_path / "run"
    good = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")
    write_decision(run_dir, good)
    directory = run_dir / AUTHORIZATIONS_DIR
    name = f"{TEMPORARY_PREFIX}anything"
    if kind == "symlink":
        (directory / name).symlink_to(directory / f"{good.id}.json")
    else:
        (directory / name).mkdir()

    with pytest.raises(AuthorizationError) as failure:
        read_decisions(run_dir)

    assert failure.value.code == "authorizations_invalid"
    assert failure.value.path == name


def test_read_decisions_skips_a_temporary_entry_that_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename that completes `write_decision` is the ordinary race here."""

    run_dir = tmp_path / "run"
    good = Authorization.new("run_identity_changed", DIGEST, "r", now="2026-09-17T10:00:00Z")
    write_decision(run_dir, good)
    directory = run_dir / AUTHORIZATIONS_DIR
    name = f"{TEMPORARY_PREFIX}abc123"
    (directory / name).write_text("{ half a decision", encoding="utf-8")

    real_lstat = os.lstat

    def vanishing(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == name:
            (directory / name).unlink()
            raise FileNotFoundError(name)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", vanishing)

    (decision,) = read_decisions(run_dir)

    assert decision.id == good.id


def test_from_payload_refuses_an_issued_at_that_is_not_an_instant() -> None:
    """The expression places the digits; the parse says they name a moment."""

    assert validate_issued_at("2026-99-99T99:99:99Z") is False
    with pytest.raises(AuthorizationError) as failure:
        Authorization.from_payload(
            {
                "schema": SCHEMA,
                "code": "run_identity_changed",
                "digest": DIGEST,
                "reason": "r",
                "issued_at": "2026-99-99T99:99:99Z",
            },
            source="file",
        )

    assert failure.value.code == "authorization_invalid"
    assert failure.value.path == "issued_at"


def test_identity_and_lineage_refusals_carry_no_unread_pending_findings() -> None:
    """Final review: `evolve` reports its own collected findings, never these copies."""

    from evolution.tree.campaign import IdentityChanged, LineageNotFinished

    assert not hasattr(IdentityChanged((), None, hard=True), "pending")
    assert not hasattr(LineageNotFinished(), "pending")


def test_artifact_ledger_missing_is_hard_with_its_door() -> None:
    assert "artifact_ledger_missing" in HARD_CODES
    assert refusal_class("artifact_ledger_missing") == "hard"
    assert HARD_REASONS["artifact_ledger_missing"] == (
        "the artifact ledger artifacts.jsonl is missing or empty while the campaign "
        "journal records measurements; restore it from a trusted copy of the run directory"
    )
