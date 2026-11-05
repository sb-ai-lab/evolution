"""The refusal printer (spec §4; I2 spec §4.1).

Two halves. The goldens pin, byte for byte, what every family 3-6 refusal
printed before I2 -- a single-effect finding prints exactly that whether or
not it carries `effects`. The I2 half pins spec §4.1: a family-7
`run_identity_changed` finding with several admissible effects prints one lead
line and one command per effect, the default first and without `--effect`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.tree import cli as cli_module
from evolution.tree.execution.authorizations import RefusalDifference
from evolution.tree.execution.output_admission import EvolveRefused, RefusalFinding

A, B, C = "a" * 64, "b" * 64, "c" * 64
SCORER = (RefusalDifference("identity.scorer.digest", "a" * 8, "b" * 8),)
TARGET = (RefusalDifference("identity.target.settings_hash", "a" * 8, "b" * 8),)
VERSION = (RefusalDifference("producing_version", "0.3.0a16", "0.3.0a17"),)
WHY = '--reason "<why>"'
P = "evo tree evolve: "


def _print(refused: EvolveRefused, capsys: pytest.CaptureFixture[str]) -> list[str]:
    cli_module._print_refusal(refused, Path("run dir"), ())
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err.splitlines()


def _hygiene(effects: bool) -> RefusalFinding:
    return RefusalFinding(
        "run_dir_unsafe_entry",
        "scorer-traces",
        "symlinked",
        (),
        C,
        "hygiene",
        *((("hygiene",),) if effects else ()),
    )


def _identity(
    differences: tuple[RefusalDifference, ...],
    digest: str | None,
    effect: str | None,
    effects: tuple[str, ...] = (),
) -> RefusalFinding:
    return RefusalFinding(
        "run_identity_changed",
        None,
        "",
        differences,
        digest,
        effect,  # type: ignore[arg-type]
        effects,
    )


def _single(
    code: str,
    path: str | None,
    detail: str,
    effect: str,
    digest: str | None,
    differences: tuple[RefusalDifference, ...],
    effects: bool,
) -> EvolveRefused:
    extra = {"effects": (effect,)} if effects and digest is not None else {}
    return EvolveRefused(
        code,
        path,
        detail,
        differences=differences,
        instance_digest=digest,
        effect=effect if digest is not None else None,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def golden_refusal(name: str, effects: bool) -> EvolveRefused:
    """Each family 3-6 refusal; `effects` adds the single-effect tuple I2 now fills."""

    one = (lambda e: (e,)) if effects else (lambda e: ())
    return {
        "rebase": lambda: _single("run_identity_changed", None, "", "rebase", A, VERSION, effects),
        "close": lambda: _single("run_identity_changed", None, "", "close", A, SCORER, effects),
        "terminal": lambda: _single(
            "run_identity_changed", None, "", "terminal", A, SCORER, effects
        ),
        "hard": lambda: _single("run_identity_changed", None, "", "close", None, SCORER, effects),
        "hygiene": lambda: _single(
            "run_dir_unsafe_entry", "scorer-traces", "symlinked", "hygiene", C, (), effects
        ),
        "fresh_baseline": lambda: _single(
            "baseline_import_unavailable", None, "", "fresh_baseline", B, (), effects
        ),
        "link": lambda: _single("lineage_not_finished", "linked_from", "", "link", B, (), effects),
        "hygiene+close": lambda: EvolveRefused.from_findings(
            (_hygiene(effects), _identity(SCORER, A, "close", one("close")))
        ),
        "hygiene+rebase": lambda: EvolveRefused.from_findings(
            (_hygiene(effects), _identity(VERSION, A, "rebase", one("rebase")))
        ),
        "hard+hygiene": lambda: EvolveRefused.from_findings(
            (_identity(SCORER, None, None), _hygiene(effects))
        ),
    }[name]()


GOLDEN: dict[str, tuple[str, ...]] = {
    "rebase": (
        f"{P}refused: run_identity_changed",
        f"{P}differs: producing_version  journaled 0.3.0a16  observed 0.3.0a17",
        f"{P}class: authorizable",
        f"{P}to accept this difference and continue, run:",
        f"  evo tree authorize 'run dir' run_identity_changed --digest {A} {WHY}",
    ),
    "close": (
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.scorer.digest  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: authorizable",
        f"{P}accepting closes this campaign as stopped(condition_changed);",
        f"{P}continue it with a linked run that declares the new condition",
        f"{P}to accept this difference, run:",
        f"  evo tree authorize 'run dir' run_identity_changed --digest {A} {WHY}",
    ),
    "terminal": (
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.scorer.digest  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: authorizable",
        f"{P}accepting writes the terminal this campaign already owes;",
        f"{P}continue it with a linked run that declares the new condition",
        f"{P}to accept this difference, run:",
        f"  evo tree authorize 'run dir' run_identity_changed --digest {A} {WHY}",
    ),
    "hard": (
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.scorer.digest  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: hard",
        f"{P}the campaign is finished and there is nothing left to continue; "
        "start a linked run that declares the new condition",
    ),
    "hygiene": (
        f"{P}refused: run_dir_unsafe_entry at scorer-traces",
        f"{P}class: authorizable",
        f"{P}to accept this difference and continue, run:",
        f"  evo tree authorize 'run dir' run_dir_unsafe_entry --digest {C} {WHY}",
    ),
    "fresh_baseline": (
        f"{P}refused: baseline_import_unavailable",
        f"{P}class: authorizable",
        f"{P}accepting measures this run's baseline in full instead of carrying",
        f"{P}the predecessor's rows",
        f"{P}to accept this difference and continue, run:",
        f"  evo tree authorize 'run dir' baseline_import_unavailable --digest {B} {WHY}",
    ),
    "link": (
        f"{P}refused: lineage_not_finished at linked_from",
        f"{P}class: authorizable",
        f"{P}the predecessor is unfinished and will not be closed; if it is ever resumed,",
        f"{P}its rounds and this run's spend the same alpha",
        f"{P}to accept this difference and continue, run:",
        f"  evo tree authorize 'run dir' lineage_not_finished --digest {B} {WHY}",
    ),
    "hygiene+close": (
        f"{P}refused: run_dir_unsafe_entry at scorer-traces",
        f"{P}class: authorizable",
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.scorer.digest  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: authorizable",
        f"{P}accepting closes this campaign as stopped(condition_changed);",
        f"{P}continue it with a linked run that declares the new condition",
        f"{P}to accept these findings, run:",
        f"  evo tree authorize 'run dir' --accept run_dir_unsafe_entry:{C} "
        f"--accept run_identity_changed:{A} {WHY}",
    ),
    "hygiene+rebase": (
        f"{P}refused: run_dir_unsafe_entry at scorer-traces",
        f"{P}class: authorizable",
        f"{P}refused: run_identity_changed",
        f"{P}differs: producing_version  journaled 0.3.0a16  observed 0.3.0a17",
        f"{P}class: authorizable",
        f"{P}to accept these findings and continue, run:",
        f"  evo tree authorize 'run dir' --accept run_dir_unsafe_entry:{C} "
        f"--accept run_identity_changed:{A} {WHY}",
    ),
    "hard+hygiene": (
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.scorer.digest  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: hard",
        f"{P}refused: run_dir_unsafe_entry at scorer-traces",
        f"{P}class: authorizable",
        f"{P}the campaign is finished and there is nothing left to continue; "
        "start a linked run that declares the new condition",
    ),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_a_single_effect_finding_prints_the_same_with_its_effects_filled(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    assert tuple(_print(golden_refusal(name, effects=True), capsys)) == GOLDEN[name]


# -- I2 spec §4.1: one command per admissible effect ---------------------------

CONTINUE_LEAD = f"{P}to continue with the existing measurements, run:"
REBASELINE_LEAD = f"{P}to start the next round from a fresh baseline under the new condition, run:"
CLOSE_LEAD = f"{P}to close the campaign and continue with a linked run, run:"
SCALE = (
    f"{P}the scorer, metric, corpus or policy changed; "
    "existing scores cannot be compared on the new scale inside a round"
)
ACCEPT = f"  evo tree authorize 'run dir' --accept run_identity_changed:{A}"


def _family7(differences: tuple[RefusalDifference, ...], effects: tuple[str, ...]) -> EvolveRefused:
    return EvolveRefused(
        "run_identity_changed",
        differences=differences,
        instance_digest=A,
        effect=effects[0],  # type: ignore[arg-type]
        effects=effects,
    )


def test_an_agent_side_change_prints_a_command_per_effect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = _print(_family7(TARGET, ("continue", "rebaseline", "close")), capsys)
    assert lines == [
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.target.settings_hash  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: authorizable",
        CONTINUE_LEAD,
        f"{ACCEPT} {WHY}",
        REBASELINE_LEAD,
        f"{ACCEPT} --effect rebaseline {WHY}",
        CLOSE_LEAD,
        f"{ACCEPT} --effect close {WHY}",
    ]


def test_an_agent_side_change_with_no_round_left_offers_continue_and_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = _print(_family7(TARGET, ("continue", "close")), capsys)
    assert lines[3:] == [
        CONTINUE_LEAD,
        f"{ACCEPT} {WHY}",
        CLOSE_LEAD,
        f"{ACCEPT} --effect close {WHY}",
    ]


def test_a_scale_change_says_why_and_offers_rebaseline_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = _print(_family7(SCORER, ("rebaseline", "close")), capsys)
    assert lines == [
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.scorer.digest  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: authorizable",
        SCALE,
        REBASELINE_LEAD,
        f"{ACCEPT} {WHY}",
        CLOSE_LEAD,
        f"{ACCEPT} --effect close {WHY}",
    ]


def test_a_collected_refusal_prints_one_combined_command_per_effect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = EvolveRefused.from_findings(
        (
            _hygiene(effects=False),
            _identity(TARGET, A, "continue", ("continue", "rebaseline", "close")),
        )
    )
    # Ruling R7: a finding with an effect offers at least that one.
    assert refused.effects == ("hygiene",)
    accept = (
        f"  evo tree authorize 'run dir' --accept run_dir_unsafe_entry:{C} "
        f"--accept run_identity_changed:{A}"
    )
    assert _print(refused, capsys) == [
        f"{P}refused: run_dir_unsafe_entry at scorer-traces",
        f"{P}class: authorizable",
        f"{P}refused: run_identity_changed",
        f"{P}differs: identity.target.settings_hash  journaled aaaaaaaa  observed bbbbbbbb",
        f"{P}class: authorizable",
        CONTINUE_LEAD,
        f"{accept} {WHY}",
        REBASELINE_LEAD,
        f"{accept} --effect rebaseline {WHY}",
        CLOSE_LEAD,
        f"{accept} --effect close {WHY}",
    ]


def test_a_collected_scale_change_says_why_in_its_own_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = EvolveRefused.from_findings(
        (_hygiene(effects=False), _identity(SCORER, A, "rebaseline", ("rebaseline", "close")))
    )
    lines = _print(refused, capsys)
    assert lines[4:7] == [f"{P}class: authorizable", SCALE, REBASELINE_LEAD]
    assert lines[-1].endswith(f"--effect close {WHY}")


def test_a_hard_first_finding_still_prints_no_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = EvolveRefused.from_findings(
        (_identity(SCORER, None, None), _identity(TARGET, A, "continue", ("continue", "close")))
    )
    lines = _print(refused, capsys)
    assert not any(line.startswith("  evo tree authorize") for line in lines)


# -- the refusal carries the effects -------------------------------------------


def test_a_refusal_and_its_findings_carry_the_effects() -> None:
    refused = _family7(TARGET, ("continue", "rebaseline", "close"))
    assert refused.effect == "continue"
    assert refused.effects == ("continue", "rebaseline", "close")
    assert refused.findings[0].effects == ("continue", "rebaseline", "close")
    collected = EvolveRefused.from_findings((refused.findings[0], _hygiene(effects=False)))
    assert collected.effects == ("continue", "rebaseline", "close")


def test_a_refusal_with_no_effects_given_has_none() -> None:
    refused = EvolveRefused("run_identity_changed", differences=SCORER)
    assert refused.effects == ()
    assert refused.findings[0].effects == ()


def test_every_finding_with_an_effect_offers_at_least_that_one() -> None:
    """Ruling R7: `effects` defaults to `(effect,)`; the printer is unchanged."""

    assert _hygiene(effects=False).effects == ("hygiene",)
    assert _identity(SCORER, A, "close").effects == ("close",)
    assert _identity(SCORER, None, None).effects == ()
    refused = EvolveRefused("run_identity_changed", instance_digest=A, effect="rebase")
    assert refused.effects == ("rebase",)
    assert refused.findings[0].effects == ("rebase",)


def _champion_evidence(count: int, measurement_id: str = "m-r01-champion") -> dict:
    return {
        "evidence_kind": "champion_rows",
        "measurement_id": measurement_id,
        "count": count,
        "rows_digest": "0" * 64,
        "truncated": False,
        "rows": [
            {
                "measurement_id": measurement_id,
                "case_id": "c2",
                "sample_id": "01",
                "retry_ordinal": 0,
                "owner": "target",
            }
        ],
    }


def test_the_cli_names_the_champion_incomplete_cause() -> None:
    """#194 spec §3.5: the stopped Champion measurement and its first pair."""

    from evolution.tree.cli import _suspension_detail

    assert _suspension_detail(_champion_evidence(3), reason="champion_incomplete") == (
        "the campaign was stopped before the Champion measurement m-r01-champion "
        "finished; 3 unfinished pairs, first c2/01"
    )
    assert _suspension_detail(_champion_evidence(1), reason="champion_incomplete") == (
        "the campaign was stopped before the Champion measurement m-r01-champion "
        "finished; 1 unfinished pair, first c2/01"
    )
    # No winner-completion wording: that measurement exists in family 12 only.
    assert "completion" not in _suspension_detail(
        _champion_evidence(2, "m-r00-champion"), reason="champion_incomplete"
    )


# -- family 14: the connection preflight's suspensions (C spec §4.3, H12b choice 16) --


#: A stage-record evidence of a connection suspension; `seq` names the failed terminal.
CONNECTION_EVIDENCE = {"evidence_kind": "stage_record", "seq": 12, "operation_id": "cp-0001"}
INCOMPATIBLE_DETAIL = {
    "code": "connection_event_missing",
    "metric": "score",
    "manifest_path": "scorer.metric_contract.target_fields[0]",
    "observed_path": "events[event=assistant_message,last]/content",
}


def _connection(outcome: str | None, detail: object) -> dict:
    """A status document's `connection` object (C spec §6.3) after a failed probe."""

    return {
        "state": "suspended",
        "binding_digest": "0" * 64,
        "operation_id": "cp-0001",
        "attempts": 1,
        "outcome": outcome,
        "detail": detail,
    }


CONNECTION_SENTENCES = {
    "connection_incompatible": (
        INCOMPATIBLE_DETAIL,
        "the connection probe found the Target or Scorer incompatible: connection_event_missing "
        "at scorer.metric_contract.target_fields[0] (observed "
        "events[event=assistant_message,last]/content); fix the Target or the Scorer, then run "
        "the same command (a changed Scorer or metric_contract is a comparable change: accept "
        "it with evo tree authorize)",
    ),
    "connection_preflight_unavailable": (
        {"cause_code": "process_failed"},
        "the connection probe could not reach a conclusion (process_failed); repair the cause "
        "and run the same command",
    ),
    "connection_preflight_inconclusive": (
        {"cause_code": "turn_budget"},
        "the connection probe's rollout ended without a scoreable terminal (turn_budget); "
        "repair the cause and run the same command",
    ),
}


@pytest.mark.parametrize("reason", sorted(CONNECTION_SENTENCES))
def test_the_cli_names_each_connection_suspension_from_the_connection_object(reason) -> None:
    from evolution.tree.cli import _suspension_detail

    detail, sentence = CONNECTION_SENTENCES[reason]
    connection = _connection(reason, detail)
    assert _suspension_detail(CONNECTION_EVIDENCE, reason=reason, connection=connection) == (
        sentence
    )


def _malformed(reason: str) -> list[object]:
    """`connection` values the sentence cannot be read from."""

    detail, _ = CONNECTION_SENTENCES[reason]
    other = next(name for name in CONNECTION_SENTENCES if name != reason)
    first = next(iter(detail))
    return [
        None,
        "suspended",
        {},
        _connection(reason, None),
        _connection(reason, []),
        _connection(reason, {}),
        _connection(reason, {key: value for key, value in detail.items() if key != first}),
        _connection(reason, {**detail, first: 5}),
        _connection(reason, {**detail, first: None}),
        # The object names another outcome than the suspension's reason.
        _connection(other, detail),
        _connection(None, detail),
    ]


@pytest.mark.parametrize("reason", sorted(CONNECTION_SENTENCES))
def test_a_missing_or_malformed_connection_falls_back_to_the_stage_record(reason) -> None:
    from evolution.tree.cli import _suspension_detail

    generic = "decided by the record at sequence 12"
    assert _suspension_detail(CONNECTION_EVIDENCE, reason=reason) == generic
    for connection in _malformed(reason):
        assert (
            _suspension_detail(CONNECTION_EVIDENCE, reason=reason, connection=connection) == generic
        ), connection


@pytest.mark.parametrize(
    ("reason", "evidence"),
    [
        ("final_incomplete", {"evidence_kind": "final_rows", "count": 0, "rows": []}),
        ("cohort_incomplete", {"evidence_kind": "cohort_rows", "count": 0, "rows": []}),
        ("champion_incomplete", _champion_evidence(2)),
        ("grouper_provider_failed", {"evidence_kind": "stage_record", "seq": 9}),
        ("grouper_failing", {"evidence_kind": "stage_record", "seq": 9}),
        ("target_unstable", {"evidence_kind": "stage_record", "seq": 9}),
    ],
)
def test_every_other_sentence_ignores_the_connection(reason, evidence) -> None:
    """A suspension the connection does not own prints what it printed before."""

    from evolution.tree.cli import _suspension_detail

    connection = _connection("connection_incompatible", INCOMPATIBLE_DETAIL)
    assert _suspension_detail(evidence, reason=reason, connection=connection) == (
        _suspension_detail(evidence, reason=reason)
    )
