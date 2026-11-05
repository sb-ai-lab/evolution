"""The watch renderer and snapshot (Task 5 of #197).

`run_watch.Renderer` turns each new journal record into zero or more
prefixed lines (spec §4.2, decision 17): family-aware wording for families
13/14 through `campaign_vocabulary_for`, the shared dedicated wording for
families 5-12, and the generic `round <n> · <event> <id?>` line everywhere
else. Measurement progress is rate-limited to one line per measurement per
10 s by the injected clock, plus one forced line on completion. After 60 s
without a record an idle line appears, then one every 300 s.
`run_watch.Snapshot.from_state` reduces the same facts into the `--once`
view and the one-line `--progress` form (spec §4.3). Decision 22: no line
anywhere carries task data — every corpus canary and every free-text
payload string of the replayed journals is asserted absent from all output.

The tests replay genuine journals record by record through a fake clock.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.run_reader import RunKind
from evolution.tree.run_watch import Renderer, Snapshot
from evolution.tree.storage.append_log import inspect_log
from evolution.tree.storage.envelope import Envelope
from tests import run_observability_fakes as fakes

pytestmark = pytest.mark.slow

CAMPAIGN = "campaign.jsonl"
EVALUATE_RESUME = "run the same evaluate command after repair"


@pytest.mark.parametrize("family", [14, 15])
@pytest.mark.parametrize("status", ["ready", "unavailable"])
def test_watch_probe_checked_status(family, status):
    renderer = Renderer(RunKind.CAMPAIGN, family, lambda: 0.0)
    assert (
        renderer._generic_line("probe_checked", {"round": 3, "status": status})
        == f"round 3 · probe {status}"
    )


def test_watch_shadow_counts():
    renderer = Renderer(RunKind.CAMPAIGN, 15, lambda: 0.0)
    record = Envelope.from_payload(
        {
            "schema_version": 1,
            "run_id": "run",
            "sequence": 1,
            "timestamp": "2026-10-27T00:00:00Z",
            "event": "check_shadowed",
            "operation_id": None,
            "payload": {
                "check": "connection_preflight",
                "code": "connection_preflight_inconclusive",
                "count": 2,
                "round": None,
                "operation_id": None,
            },
        },
        family=campaign_family_for(15),
    )
    assert "connection_preflight · connection_preflight_inconclusive · count 2" in "\n".join(
        renderer.feed(record)
    )


class _Clock:
    """A scriptable monotonic clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _utc_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Record timestamps are UTC; with TZ=UTC the local-time prefix is the
    record's own wall clock, so expected lines can pin it literally."""

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(scope="module")
async def family14_run(tmp_path_factory):
    return await fakes.genuine_family14_run(tmp_path_factory.mktemp("family14"))


@pytest.fixture(scope="module")
async def campaign_run(tmp_path_factory):
    return await fakes.genuine_campaign_run(tmp_path_factory.mktemp("campaign"))


@pytest.fixture(scope="module")
async def repeated_campaign_run(tmp_path_factory):
    return await fakes.genuine_repeated_campaign_run(tmp_path_factory.mktemp("repeated_campaign"))


@pytest.fixture(scope="module")
async def evaluation_run(tmp_path_factory):
    return await fakes.genuine_evaluation_run(tmp_path_factory.mktemp("evaluation"))


@pytest.fixture(scope="module")
async def retried_evaluation_run(tmp_path_factory):
    return await fakes.genuine_retried_evaluation_run(tmp_path_factory.mktemp("retried"))


def _campaign_records(run: Path, family: int) -> list[Envelope]:
    log = inspect_log(
        run,
        file_name=CAMPAIGN,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=lambda: CampaignLifecycle(selector=None, family_version=family),
        envelope_family=campaign_family_for(family),
    )
    return list(log.records)


def _evaluation_records(run: Path) -> list[Envelope]:
    from evolution.tree.evaluation_journal import inspect_evaluation_journal

    return list(inspect_evaluation_journal(run).records)


def _measurement_records(run: Path, measurement_id: str) -> list[Envelope]:
    from evolution.tree.execution.journal import inspect_journal

    return list(inspect_journal(run / "measurements" / measurement_id).records)


def _prefixed(record: Envelope, text: str) -> str:
    """The line `text` carries when prefixed with the record's local time."""

    return f"{record.to_payload()['timestamp'][11:19]}  {text}"


def _completing(records: list[Envelope]) -> Envelope:
    """The record whose count change completes a measurement: its last
    `scoring_finished`. The S1 journal goes on after it (`target_release_*`),
    so the journal's last record may carry a later second (#236)."""

    return next(record for record in reversed(records) if record.event == "scoring_finished")


def _with_tail_in_the_next_second(records: list[Envelope]) -> list[Envelope]:
    """`records` with every record after the last `scoring_finished` stamped one
    second after it: the journal tail #236 met under load, made deterministic."""

    index = max(i for i, record in enumerate(records) if record.event == "scoring_finished")
    stamp = datetime.fromisoformat(records[index].to_payload()["timestamp"][:-1] + "+00:00")
    later = (stamp + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    shifted = list(records[: index + 1])
    for record in records[index + 1 :]:
        body = record.to_payload()
        body["timestamp"] = later
        shifted.append(Envelope.from_payload(body, family=S1_FAMILY))
    return shifted


def _feed_all(
    renderer: Renderer, records: list[Envelope], measurement_id: str | None = None
) -> list[str]:
    lines: list[str] = []
    for record in records:
        lines.extend(renderer.feed(record, measurement_id=measurement_id))
    return lines


def _assert_feed(
    renderer: Renderer, records: list[Envelope], expected: Mapping[int, tuple[str, ...]]
) -> None:
    seen = []
    for record in records:
        sequence = record.to_payload()["sequence"]
        seen.append(sequence)
        want = expected[sequence]
        assert renderer.feed(record) == [_prefixed(record, text) for text in want], (
            f"sequence {sequence} ({record.event})"
        )
    assert seen == sorted(expected), "the table must pin every record of the journal"


# -- the spec §4.2 wording on a family-14 run -----------------------------------


def test_family_14_records_render_the_spec_example_wording(family14_run):
    renderer = Renderer(RunKind.CAMPAIGN, 14, _Clock())
    _assert_feed(
        renderer,
        _campaign_records(family14_run, 14),
        {
            1: ("campaign created · family 14 · 2 cases × 1 repeats",),
            2: ("round 1 · baseline m-r00-champion bound",),
            3: (),
            4: ("connection_preflight_started",),
            5: ("connection_preflight_finished",),
            6: ("round 1 · stage_opened m-r00-champion",),
            7: ("m-r00-champion · instance attested",),
            8: (),
            9: ("round 1 · grouping (fields) started",),
            10: ("round 1 · grouping finished · 1 groups · no fallback",),
            11: ("round 1 · focus_portion_selected",),
            12: ("round 1 · reflection r01-x01 started",),
            13: ("round 1 · reflection r01-x01 finished · reflected",),
            14: ("round 1 · gate_scope_selected",),
            15: ("r01-a01 · attempt opened",),
            16: ("round 1 · attempt_input_frozen r01-a01",),
            17: ("r01-a01 · editor started",),
            18: ("r01-a01 · editor finished · edited",),
            19: ("r01-a01 · sealed",),
            20: ("round 1 · affected_cases_selected r01-a01",),
            21: ("m-r01-a01 · candidate measurement bound",),
            22: ("m-r01-a01-parent · parent measurement bound",),
            23: ("round 1 · comparison_bound r01-a01",),
            24: ("round 1 · noise_calibration_bound r01-a01",),
            25: ("round 1 · stage_opened m-r01-a01",),
            26: ("round 1 · stage_opened m-r01-a01-parent",),
            27: ("m-r01-a01 · instance attested",),
            28: ("m-r01-a01-parent · instance attested",),
            29: ("round 1 · noise_calibration_completed r01-a01",),
            30: ("r01-a01 · final look · observe_finalist → finalist",),
            31: ("r01-a01 · attempt closed · finalist (observe_finalist)",),
            32: ("round 1 · finalists bound · 2 members",),
            33: ("m-r01-final-01-champion · final measurement bound · champion",),
            34: ("m-r01-final-01-a01 · final measurement bound · finalist",),
            35: ("round 1 · stage_opened m-r01-final-01-champion",),
            36: ("m-r01-final-01-champion · instance attested",),
            37: ("round 1 · stage_opened m-r01-final-01-a01",),
            38: ("m-r01-final-01-a01 · instance attested",),
            39: (),
            40: (),
            41: ("r01-a01 · global comparison · global_off → accepted",),
            42: ("r01-a01 · selected · finalist_selected",),
            43: ("round 1 closed · promoted",),
            44: ("campaign finished · completed · rounds_exhausted",),
        },
    )


def test_family_14_suppresses_the_redundant_champion_bind(family14_run):
    renderer = Renderer(RunKind.CAMPAIGN, 14, _Clock())
    lines = _feed_all(renderer, _campaign_records(family14_run, 14))
    assert not any("champion measurement bound" in line for line in lines)


# -- the same records on a family-13 run: cohort / accepted wording -------------


# -- family-12: shared dedicated wording, generic lines elsewhere ----------------


# -- retired families: shared wording plus the generic line ---------------------


# -- evaluation runs ------------------------------------------------------------


def test_evaluation_records_render_created_bound_attested_completed(evaluation_run):
    renderer = Renderer(RunKind.EVALUATION, None, _Clock())
    _assert_feed(
        renderer,
        _evaluation_records(evaluation_run),
        {
            1: ("evaluation created · evaluation/1 · test · 1 cases × 3 repeats",),
            2: ("evaluation · measurement bound",),
            3: ("evaluation · instance attested",),
            4: ("evaluation completed",),
        },
    )


def test_retried_evaluation_renders_suspensions_and_forced_final_progress(
    retried_evaluation_run,
):
    records = _measurement_records(retried_evaluation_run, "evaluation")
    evaluation = _evaluation_records(retried_evaluation_run)
    clock = _Clock()
    renderer = Renderer(RunKind.EVALUATION, None, clock)
    lines = _feed_all(renderer, records, measurement_id="evaluation")
    lines.extend(renderer.feed(evaluation[-1]))
    completion = evaluation[-1]
    assert lines == [
        _prefixed(
            records[16],
            "evaluation · 0/1 pairs scored (0 retried, 1 indeterminate, 1 target_failed)",
        ),
        _prefixed(
            _completing(records), "evaluation · 1/1 pairs scored (5 retried, 0 indeterminate)"
        ),
        _prefixed(completion, "evaluation completed"),
    ]
    renderer = Renderer(RunKind.EVALUATION, None, _Clock())
    suspended = [
        line
        for line in _feed_all(renderer, evaluation)
        if line.split("  ", 1)[1].startswith("suspended:")
    ]
    assert suspended == [
        _prefixed(record, f"suspended: incomplete → {EVALUATE_RESUME}")
        for record in evaluation
        if record.event == "evaluation_suspended"
    ]


# -- measurement progress: rate limit, completion flush, counts ------------------


def test_progress_lines_are_rate_limited_per_measurement(repeated_campaign_run):
    champion = _measurement_records(repeated_campaign_run, "m-r00-champion")
    candidate = _measurement_records(repeated_campaign_run, "m-r01-a01")
    clock = _Clock()
    renderer = Renderer(RunKind.CAMPAIGN, 12, clock)
    champion_lines: list[str] = []
    for record in champion:
        champion_lines.extend(renderer.feed(record, measurement_id="m-r00-champion"))
    clock.advance(1)
    candidate_lines: list[str] = []
    for record in candidate:
        candidate_lines.extend(renderer.feed(record, measurement_id="m-r01-a01"))
    # Each measurement gets an initial line and a forced completion; the
    # intermediate scores inside its own 10 s window are suppressed.
    first_score = next(record for record in champion if record.event == "scoring_finished")
    assert champion_lines == [
        _prefixed(first_score, "m-r00-champion · 1/6 pairs scored (0 retried, 0 indeterminate)"),
        _prefixed(
            _completing(champion),
            "m-r00-champion · 6/6 pairs scored (0 retried, 0 indeterminate)",
        ),
    ]
    candidate_score = next(record for record in candidate if record.event == "scoring_finished")
    assert candidate_lines == [
        _prefixed(candidate_score, "m-r01-a01 · 1/6 pairs scored (0 retried, 0 indeterminate)"),
        _prefixed(
            _completing(candidate), "m-r01-a01 · 6/6 pairs scored (0 retried, 0 indeterminate)"
        ),
    ]
    # Ten seconds after the first line, the next change prints again.
    clock = _Clock()
    renderer = Renderer(RunKind.CAMPAIGN, 12, clock)
    last_score = max(
        index for index, record in enumerate(champion) if record.event == "scoring_finished"
    )
    for record in champion[:last_score]:
        renderer.feed(record, measurement_id="m-r00-champion")
    clock.advance(10)
    lines = renderer.feed(champion[last_score], measurement_id="m-r00-champion")
    assert lines == [
        _prefixed(
            champion[last_score],
            "m-r00-champion · 6/6 pairs scored (0 retried, 0 indeterminate)",
        )
    ]


@pytest.mark.parametrize(
    ("fixture", "measurement_id", "kind", "family", "text"),
    [
        (
            "family14_run",
            "m-r00-champion",
            RunKind.CAMPAIGN,
            14,
            # The native observe fixture measures two cases once (PDR-0028).
            "m-r00-champion · 2/2 pairs scored (0 retried, 0 indeterminate)",
        ),
        (
            "retried_evaluation_run",
            "evaluation",
            RunKind.EVALUATION,
            None,
            "evaluation · 1/1 pairs scored (5 retried, 0 indeterminate)",
        ),
    ],
)
def test_completion_line_carries_the_completing_record_time_not_the_journal_tail(
    request, fixture, measurement_id, kind, family, text
):
    """#236: the S1 journal goes on after its last score (`target_release_*`).
    When that tail falls in the next second, the completion line still carries
    the time of the record that completed the measurement."""

    run = request.getfixturevalue(fixture)
    records = _with_tail_in_the_next_second(_measurement_records(run, measurement_id))
    completing = _completing(records)
    assert records[-1].event == "target_release_finished"
    lines = _feed_all(Renderer(kind, family, _Clock()), records, measurement_id)
    assert lines[-1] == _prefixed(completing, text)
    # The old expectation, the journal's last record, now names another second.
    assert lines[-1] != _prefixed(records[-1], text)


def test_progress_counts_retried_and_indeterminate_outcomes(retried_evaluation_run):
    records = _measurement_records(retried_evaluation_run, "evaluation")

    completion = _evaluation_records(retried_evaluation_run)[-1]

    def drive(advance: bool) -> list[str]:
        clock = _Clock()
        renderer = Renderer(RunKind.EVALUATION, None, clock)
        lines = []
        emitted = 0
        for record in records:
            if advance and emitted:
                clock.advance(11)
            before = len(lines)
            lines.extend(renderer.feed(record, measurement_id="evaluation"))
            emitted += len(lines) - before
        before = len(lines)
        lines.extend(renderer.feed(completion))
        assert len(lines) - before == 1
        return lines

    frozen = [line.split("  ", 1)[1] for line in drive(advance=False)]
    assert frozen == [
        "evaluation · 0/1 pairs scored (0 retried, 1 indeterminate, 1 target_failed)",
        "evaluation · 1/1 pairs scored (5 retried, 0 indeterminate)",
        "evaluation completed",
    ]
    unfrozen = [line.split("  ", 1)[1] for line in drive(advance=True)]
    assert unfrozen[-1] == "evaluation completed"
    assert "evaluation · 0/1 pairs scored (4 retried, 1 indeterminate, 1 target_failed)" in unfrozen
    assert "evaluation · 0/1 pairs scored (5 retried, 0 indeterminate)" in unfrozen
    assert not any("2 target_failed" in line or "5 target_failed" in line for line in unfrozen)


# -- idle lines ------------------------------------------------------------------


def test_idle_lines_after_sixty_seconds_then_every_five_minutes(family14_run):
    clock = _Clock()
    renderer = Renderer(RunKind.CAMPAIGN, 14, clock)
    created = _campaign_records(family14_run, 14)[0]
    assert renderer.feed(created)
    assert renderer.idle() == []
    clock.advance(59)
    assert renderer.idle() == []
    clock.advance(1)
    stamp = created.to_payload()["timestamp"][11:19]
    assert renderer.idle() == [f"no new records for 1m (last: campaign_created at {stamp})"]
    clock.advance(299)
    assert renderer.idle() == []
    clock.advance(1)
    assert renderer.idle() == [f"no new records for 6m (last: campaign_created at {stamp})"]
    clock.advance(900)
    assert renderer.idle() == [
        f"no new records for 11m (last: campaign_created at {stamp})",
        f"no new records for 16m (last: campaign_created at {stamp})",
        f"no new records for 21m (last: campaign_created at {stamp})",
    ]
    # A new record resets the idle schedule; none appear while records arrive.
    assert renderer.feed(_campaign_records(family14_run, 14)[1])
    clock.advance(59)
    assert renderer.idle() == []


def test_idle_lines_need_a_record_first(family14_run):
    renderer = Renderer(RunKind.CAMPAIGN, 14, _Clock(10_000.0))
    assert renderer.idle() == []


# -- the snapshot (spec §4.3) -----------------------------------------------------


def _cut(family14_run: Path, target: Path, campaign_upto: int) -> Path:
    replayer = fakes.JournalReplayer(family14_run, target)
    replayer.append_records(CAMPAIGN, replayer.lines(CAMPAIGN)[:campaign_upto])
    journal = "measurements/{0}/events.jsonl"
    # The champion's journal is complete; the candidate's is cut mid-flight:
    # run_created, materialize, and the first try's start, no terminals yet.
    replayer.append_records(
        journal.format("m-r00-champion"), replayer.lines(journal.format("m-r00-champion"))
    )
    replayer.append_records(
        journal.format("m-r01-a01"), replayer.lines(journal.format("m-r01-a01"))[:4]
    )
    replayer.append_records(
        journal.format("m-r01-a01-parent"), replayer.lines(journal.format("m-r01-a01-parent"))
    )
    return target


def test_snapshot_of_a_finished_run(family14_run):
    snapshot = Snapshot.from_state(family14_run)
    created = _campaign_records(family14_run, 14)[0]
    assert snapshot.lines() == [
        _prefixed(created, "campaign created · family 14 · 2 cases × 1 repeats"),
        "round 1",
        "open attempts: none",
        "last decision: r01-a01 · selected · finalist_selected",
        "suspensions: 0",
        "finished: completed (rounds_exhausted)",
    ]
    assert snapshot.snapshot_line() == (
        "round 1 · last: r01-a01 · selected · finalist_selected · finished completed (rounds_exhausted)"
    )


def test_snapshot_of_a_live_cut_mid_measurement(family14_run, tmp_path):
    cut = _cut(family14_run, tmp_path / "cut", 26)
    snapshot = Snapshot.from_state(cut)
    created = _campaign_records(family14_run, 14)[0]
    assert snapshot.lines() == [
        _prefixed(created, "campaign created · family 14 · 2 cases × 1 repeats"),
        "round 1 · measurement",
        "open attempts: r01-a01 (measurement)",
        "m-r01-a01 · candidate · 0/2 pairs scored (0 retried, 0 indeterminate)",
        "suspensions: 0",
        "running",
    ]
    assert snapshot.snapshot_line() == (
        "round 1 · measurement · open r01-a01 (measurement) · "
        "m-r01-a01 · candidate · 0/2 pairs scored (0 retried, 0 indeterminate) · running"
    )


def test_snapshot_of_a_live_cut_mid_editing(family14_run, tmp_path):
    cut = _cut(family14_run, tmp_path / "cut", 15)
    snapshot = Snapshot.from_state(cut)
    created = _campaign_records(family14_run, 14)[0]
    assert snapshot.lines() == [
        _prefixed(created, "campaign created · family 14 · 2 cases × 1 repeats"),
        "round 1 · editing",
        "open attempts: r01-a01 (editor)",
        "suspensions: 0",
        "running",
    ]
    assert snapshot.snapshot_line() == "round 1 · editing · open r01-a01 (editor) · running"


def test_a_new_empty_run_has_no_snapshot_line(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / CAMPAIGN).write_text("")
    snapshot = Snapshot.from_state(run)
    assert snapshot.snapshot_line() is None
    assert snapshot.lines() == []


def test_snapshot_of_a_finished_evaluation(evaluation_run):
    snapshot = Snapshot.from_state(evaluation_run)
    created = _evaluation_records(evaluation_run)[0]
    assert snapshot.lines() == [
        _prefixed(created, "evaluation created · evaluation/1 · test · 1 cases × 3 repeats"),
        "suspensions: 0",
        "finished: completed",
    ]
    assert snapshot.snapshot_line() == "finished completed"


def test_snapshot_of_a_suspended_evaluation_cut(retried_evaluation_run, tmp_path):
    replayer = fakes.JournalReplayer(retried_evaluation_run, tmp_path / "cut")
    replayer.append_records("evaluation.jsonl", replayer.lines("evaluation.jsonl")[:4])
    replayer.append_records(
        "measurements/evaluation/events.jsonl",
        replayer.lines("measurements/evaluation/events.jsonl")[:17],
    )
    snapshot = Snapshot.from_state(tmp_path / "cut")
    created = _evaluation_records(retried_evaluation_run)[0]
    assert snapshot.lines() == [
        _prefixed(created, "evaluation created · evaluation/1 · test · 1 cases × 1 repeats"),
        "evaluation · 0/1 pairs scored (0 retried, 1 indeterminate, 1 target_failed)",
        "suspensions: 1 · last: incomplete",
        "suspended: incomplete · waiting for a resume",
    ]
    assert snapshot.snapshot_line() == (
        "evaluation · 0/1 pairs scored (0 retried, 1 indeterminate, 1 target_failed) · suspended 1× · suspended: incomplete · waiting for a resume"
    )


# -- decision 22: no task data in any line ----------------------------------------


def _render_every_line(run: Path) -> list[str]:
    from evolution.tree.run_reader import open_run

    handle = open_run(run)
    clock = _Clock()
    renderer = Renderer(handle.kind, handle.family, clock)
    if handle.kind is RunKind.EVALUATION:
        lines = _feed_all(renderer, _evaluation_records(run))
        measurement_ids = ["evaluation"]
    else:
        family = handle.family or 0
        lines = _feed_all(renderer, _campaign_records(run, family))
        measurement_ids = sorted(
            path.name for path in (run / "measurements").iterdir() if path.is_dir()
        )
    for measurement_id in measurement_ids:
        journal = run / "measurements" / measurement_id / "events.jsonl"
        if journal.exists():
            for record in _measurement_records(run, measurement_id):
                lines.extend(renderer.feed(record, measurement_id=measurement_id))
    clock.advance(10_000.0)
    lines.extend(renderer.idle())
    snapshot = Snapshot.from_state(run)
    rendered = snapshot.lines()
    if snapshot.snapshot_line() is not None:
        rendered.append(snapshot.snapshot_line())
    return [*lines, *rendered]


def _assert_no_task_data(lines: list[str], needles: set[str]) -> None:
    # One-character role notes in these genuine fixtures also occur inside
    # ordinary words. Match short strings as tokens to distinguish disclosure
    # from a coincidental letter; longer canaries remain substring checks.
    leaked = [
        (needle, line)
        for needle in needles
        for line in lines
        if (
            needle.lower() in line.lower()
            if len(needle) >= 5
            else re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", line, re.IGNORECASE)
        )
    ]
    assert leaked == []


def test_no_task_data_appears_in_any_line(
    family14_run, campaign_run, repeated_campaign_run, evaluation_run, retried_evaluation_run
):
    for run in (
        family14_run,
        campaign_run,
        repeated_campaign_run,
        evaluation_run,
        retried_evaluation_run,
    ):
        lines = _render_every_line(run)
        assert lines
        needles = fakes.task_data_needles(run)
        assert needles
        _assert_no_task_data(lines, needles)


@pytest.fixture(scope="module")
async def repaired_evaluation_run(tmp_path_factory):
    return await fakes.genuine_repair_evaluation_run(tmp_path_factory.mktemp("repair"))


@pytest.mark.parametrize(
    "terminal,outcome",
    [("scoring_failed", "scorer_failed"), ("scoring_interrupted", "interrupted")],
)
def test_effective_scoring_failure_in_live_progress_and_snapshot(
    repaired_evaluation_run, tmp_path, terminal, outcome
):
    records = _measurement_records(repaired_evaluation_run, "evaluation")
    failure_index = next(i for i, r in enumerate(records) if r.event == "scoring_failed")
    failed = records[failure_index].to_payload()
    failed["event"] = terminal
    if terminal == "scoring_interrupted":
        del failed["payload"]["failure"]
    records = records[:failure_index] + [Envelope.from_payload(failed, family=S1_FAMILY)]
    renderer = Renderer(RunKind.EVALUATION, None, _Clock())
    lines = _feed_all(renderer, records, "evaluation")
    want = f"evaluation · 0/1 pairs scored (0 retried, 1 indeterminate, 1 {outcome})"
    assert lines == [_prefixed(records[-1], want)]
    replayer = fakes.JournalReplayer(repaired_evaluation_run, tmp_path / "cut")
    evaluation = replayer.lines("evaluation.jsonl")
    replayer.append_records("evaluation.jsonl", evaluation[:4])
    replayer.append_records(
        "measurements/evaluation/events.jsonl",
        [json.dumps(r.to_payload()).encode() + b"\n" for r in records],
    )
    snapshot = Snapshot.from_state(replayer.target)
    assert snapshot.incomplete == (want,)
    assert snapshot.lines()[-1] == "suspended: incomplete · waiting for a resume"


def test_rate_limited_progress_flushes_after_window_without_new_records(repeated_campaign_run):
    records = _measurement_records(repeated_campaign_run, "m-r01-a01")
    second_score = [i for i, r in enumerate(records) if r.event == "scoring_finished"][1]
    clock = _Clock()
    renderer = Renderer(RunKind.CAMPAIGN, 12, clock)
    _feed_all(renderer, records[: second_score + 1], "m-r01-a01")
    clock.advance(9)
    assert renderer.idle() == []
    clock.advance(1)
    assert renderer.tick() == [
        _prefixed(
            records[second_score], "m-r01-a01 · 2/6 pairs scored (0 retried, 0 indeterminate)"
        )
    ]
    assert renderer.tick() == []


def _campaign_cut(run: Path, target: Path, event: str, *, last: bool = False) -> Path:
    replayer = fakes.JournalReplayer(run, target)
    lines = replayer.lines(CAMPAIGN)
    positions = [i for i, line in enumerate(lines) if json.loads(line)["event"] == event]
    position = positions[-1] if last else positions[0]
    replayer.append_records(CAMPAIGN, lines[: position + 1])
    return target


@pytest.mark.parametrize("fixture,family,role", [("family14_run", 14, "final")])
def test_snapshot_projects_current_final_members_and_completion(
    request, tmp_path, fixture, family, role
):
    run = request.getfixturevalue(fixture)
    cut = _campaign_cut(run, tmp_path / "bound", f"{role}_measurement_bound", last=True)
    snapshot = Snapshot.from_state(cut)
    assert snapshot.stage == role
    assert len(snapshot.incomplete) == 2
    assert all(f" · {role} · not started" in line for line in snapshot.incomplete)
    # Member completion is owned by cohort_completed, not registry.complete.
    cut = _campaign_cut(run, tmp_path / "complete", f"{role}_measurement_completed", last=True)
    snapshot = Snapshot.from_state(cut)
    assert snapshot.stage == "selection"
    assert snapshot.incomplete == ()


def test_snapshot_closed_round_takes_priority_over_selection(family14_run, tmp_path):
    cut = _campaign_cut(family14_run, tmp_path / "closed", "round_closed")
    snapshot = Snapshot.from_state(cut)
    assert snapshot.stage == "closed"
    assert snapshot.incomplete == ()


def test_snapshot_does_not_read_closed_attempt_measurements(family14_run, tmp_path):
    cut = _campaign_cut(family14_run, tmp_path / "closed", "attempt_closed")
    snapshot = Snapshot.from_state(cut)
    assert snapshot.open_attempts == ()
    assert snapshot.incomplete == ()


def test_snapshot_uses_one_campaign_prefix_even_if_writer_advances(
    family14_run, tmp_path, monkeypatch
):
    from evolution.tree import run_watch
    from evolution.tree.run_reader import open_run

    cut = _campaign_cut(family14_run, tmp_path / "cut", "final_measurement_completed", last=True)
    expected = "r01-a01 · final look · observe_finalist → finalist"
    replayer = fakes.JournalReplayer(family14_run, cut)
    remaining = replayer.lines(CAMPAIGN)[len((cut / CAMPAIGN).read_bytes().splitlines()) :]

    def open_then_advance(path):
        handle = open_run(path)
        replayer.append_records(CAMPAIGN, remaining)
        return handle

    monkeypatch.setattr(run_watch, "open_run", open_then_advance)
    snapshot = Snapshot.from_state(cut)
    assert snapshot.last_decision == expected
    assert snapshot.stage == "selection"
    assert snapshot.finished is None


def test_snapshot_old_suspension_does_not_hide_evaluation_resume(retried_evaluation_run, tmp_path):
    replayer = fakes.JournalReplayer(retried_evaluation_run, tmp_path / "cut")
    lines = replayer.lines("evaluation.jsonl")
    suspended = next(
        i for i, line in enumerate(lines) if json.loads(line)["event"] == "evaluation_suspended"
    )
    replayer.append_records("evaluation.jsonl", lines[: suspended + 2])
    snapshot = Snapshot.from_state(replayer.target)
    assert snapshot.suspension_count == 1
    assert snapshot.lines()[-1] == "running"


def test_fixture_needles_cover_all_task_data_sources(family14_run, tmp_path):
    # A controlled fixture checks the collector itself, including data that
    # does not occur in the journal, so omission cannot silently weaken O8.
    work = tmp_path / "fixture"
    run = work / "run"
    run.mkdir(parents=True)
    (work / "corpus.jsonl").write_text(
        json.dumps({"public": {"text": "CORPUS-PUBLIC"}, "oracle": "CORPUS-ORACLE"}) + "\n"
    )
    traces = run / "scorer-traces" / "trace"
    traces.mkdir(parents=True)
    (traces / "request.json").write_text(
        json.dumps(
            {
                "input": {
                    "view": {
                        "history": {
                            "turns": [
                                {
                                    "message": {"content": "REQUEST-MESSAGE"},
                                    "events": [{"text": "AGENT-OUTPUT"}],
                                }
                            ]
                        }
                    }
                }
            }
        )
    )
    (traces / "stdout").write_text(
        json.dumps({"verdicts": [{"note": "SCORER-NOTE"}], "feedback": "SCORER-FEEDBACK"})
    )
    (run / CAMPAIGN).write_text(
        json.dumps(
            {
                "payload": {
                    "note": "REFLECTOR-NOTE",
                    "groups": [{"description": "GROUP-DESCRIPTION"}],
                }
            }
        )
        + "\n"
    )
    assert fakes.task_data_needles(run) == {
        "CORPUS-PUBLIC",
        "CORPUS-ORACLE",
        "REQUEST-MESSAGE",
        "AGENT-OUTPUT",
        "SCORER-NOTE",
        "SCORER-FEEDBACK",
        "REFLECTOR-NOTE",
        "GROUP-DESCRIPTION",
    }
    assert "hello" in fakes.task_data_needles(family14_run)


@pytest.mark.parametrize("family", [14])
def test_unknown_event_uses_only_ids_and_counts(family14_run, family):
    from evolution.tree.storage.envelope import EnvelopeFamily

    body = _campaign_records(family14_run, 14)[0].to_payload()
    body.update(
        event="connection_preflight_started",
        payload={
            "round": 1,
            "attempt_id": "r01-a01",
            "description": "FUTURE-PRIVATE-DESCRIPTION",
            "message": "FUTURE-REQUEST-MESSAGE",
            "oracle": "FUTURE-ORACLE",
        },
    )
    # The renderer handles an already admitted event unknown to its version;
    # admission of future schemas belongs to the journal reader.
    future_family = EnvelopeFamily(
        lambda event, payload: None, frozenset({body["event"]}), lambda event: False
    )
    record = Envelope.from_payload(body, family=future_family)
    renderer = Renderer(RunKind.CAMPAIGN, family, _Clock())
    lines = renderer.feed(record)
    assert lines == [_prefixed(record, "round 1 · connection_preflight_started r01-a01")]
    _assert_no_task_data(
        lines, {"FUTURE-PRIVATE-DESCRIPTION", "FUTURE-REQUEST-MESSAGE", "FUTURE-ORACLE"}
    )


def test_watch_prints_the_leak_case_but_never_the_pointer():
    from tests.test_tree_campaign_family15_schema import leak_records

    renderer = Renderer(RunKind.CAMPAIGN, 15, _Clock())
    lines = [
        renderer._feed_campaign(
            event, {**payload, "leak": {"case_id": "c3", "oracle_pointer": "/reference"}}, ""
        )[0]
        for event, payload in leak_records()
    ]
    assert lines[0].endswith("failed · oracle_leak · case c3")
    assert lines[1].endswith(" · case c3")
    assert lines[2].endswith("attempt closed · abandoned (seal_refused) · case c3")
    assert all("/reference" not in line for line in lines)


def test_watch_prints_the_reflection_category_and_path():
    from tests.test_tree_campaign_schema import _projection_changed_failure

    renderer = Renderer(RunKind.CAMPAIGN, 15, _Clock())
    payload = _projection_changed_failure(error_class="invalid_result", reason="invalid_result")
    line = renderer._feed_campaign(
        "reflection_failed",
        {**payload, "detail": {"category": "not_a_case_id", "path": "groups[0].affected_cases[0]"}},
        "",
    )[0]
    assert line.endswith("failed · invalid_result · not_a_case_id at groups[0].affected_cases[0]")
    for detail in ({"category": "PRIVATE VALUE"}, {"category": "bounds", "path": "PRIVATE VALUE"}):
        assert (
            "PRIVATE VALUE"
            not in renderer._feed_campaign("reflection_failed", {**payload, "detail": detail}, "")[
                0
            ]
        )


def _write_editor_log(root, entries):
    directory = root / "editor-logs"
    directory.mkdir(exist_ok=True)
    path = directory / "a1.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def _editor_failure_line(root, family=15):
    from evolution.tree.run_watch import last_protocol_category

    renderer = Renderer(
        RunKind.CAMPAIGN,
        family,
        _Clock(),
        editor_log_category=lambda attempt: last_protocol_category(root, attempt),
    )
    return renderer._feed_campaign(
        "editor_failed", {"attempt_id": "a1", "error_class": "protocol"}, ""
    )[0]


def test_watch_prints_the_editor_category_and_log_path(tmp_path):
    _write_editor_log(
        tmp_path,
        [
            {
                "phase": "plan_correction_requested",
                "category": "plan_path_not_writable",
                "error": "path is not writable: secret/FILE CONTENT",
            }
        ],
    )
    assert _editor_failure_line(tmp_path).endswith(
        "editor failed · protocol · last protocol error: plan_path_not_writable (editor-logs/a1.jsonl)"
    )
    assert "FILE CONTENT" not in _editor_failure_line(tmp_path)


def test_watch_prints_only_a_closed_category_from_the_editor_log(tmp_path):
    path = _write_editor_log(
        tmp_path,
        [
            {"phase": "protocol_repair_requested", "category": "plan_mismatch"},
            {"phase": "plan_correction_requested", "category": "QUOTED FILE CONTENT", "error": "x"},
        ],
    )
    assert _editor_failure_line(tmp_path).endswith(
        "editor failed · protocol · log editor-logs/a1.jsonl"
    )
    path.unlink()
    assert _editor_failure_line(tmp_path).endswith("log editor-logs/a1.jsonl")


def test_family_14_editor_failed_line_is_unchanged(tmp_path):
    renderer = Renderer(
        RunKind.CAMPAIGN,
        14,
        _Clock(),
        editor_log_category=lambda attempt: pytest.fail("family 14 must not read the log"),
    )
    assert (
        renderer._feed_campaign(
            "editor_failed", {"attempt_id": "a1", "error_class": "protocol"}, ""
        )[0]
        == "a1 · editor failed · protocol"
    )


def test_editor_log_category_skips_bad_lines_and_refuses_symlinks(tmp_path):
    from evolution.tree.run_watch import last_protocol_category

    path = _write_editor_log(
        tmp_path, [{"phase": "protocol_repair_requested", "category": "invalid_arguments"}]
    )
    path.write_bytes(b"\xff\nnot json\n" + path.read_bytes() + b'{"phase":[] }\n')
    assert last_protocol_category(tmp_path, "a1") == "invalid_arguments"
    actual = path.with_name("actual.jsonl")
    path.rename(actual)
    path.symlink_to(actual)
    assert last_protocol_category(tmp_path, "a1") is None
    path.unlink()
    (tmp_path / "editor-logs").rename(tmp_path / "actual-logs")
    (tmp_path / "editor-logs").symlink_to(tmp_path / "actual-logs", target_is_directory=True)
    assert last_protocol_category(tmp_path, "a1") is None


async def test_watch_session_injects_the_editor_category_reader(tmp_path, monkeypatch):
    from evolution.tree.run_watch import WatchSession
    from tests.test_tree_campaign_family15_delivery import public_run

    directory, _ = await public_run(tmp_path, monkeypatch)
    _write_editor_log(
        directory,
        [
            {
                "phase": "plan_correction_requested",
                "category": "plan_mismatch",
                "error": "PRIVATE CONTENT",
            }
        ],
    )
    session = WatchSession(directory)
    try:
        line = session.renderer._feed_campaign(
            "editor_failed", {"attempt_id": "a1", "error_class": "protocol"}, ""
        )[0]
        assert line.endswith("last protocol error: plan_mismatch (editor-logs/a1.jsonl)")
        assert "PRIVATE CONTENT" not in line
    finally:
        session.close()


def test_watch_prints_one_line_per_shadow_entry_without_task_data():
    from evolution.tree.campaign_schema import campaign_family_for

    renderer = Renderer(RunKind.CAMPAIGN, 15, _Clock())
    payload = {
        "check": "reflection_oracle_leak",
        "code": "oracle_leak",
        "count": 2,
        "leak": {"case_id": "c1", "oracle_pointer": "/PRIVATE"},
        "round": 3,
        "operation_id": "probe1",
    }
    record = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=2,
            timestamp="2026-10-26T00:00:00Z",
            event="check_shadowed",
            operation_id=None,
            payload=payload,
        ),
        family=campaign_family_for(15),
    )
    assert renderer.feed(record) == [
        "00:00:00  check shadowed: reflection_oracle_leak · oracle_leak · count 2 · round 3 · case c1"
    ]
