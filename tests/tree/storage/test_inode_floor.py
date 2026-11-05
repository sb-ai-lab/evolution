"""ADR-0040 D8: the free-inode floor, its terms and advisory admission (PDR-0033)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import evolution.tree.campaign as campaign_module
from evolution.tree.artifacts.artifact_policy import scan_tree, tree_inodes
from evolution.tree.artifacts.artifacts import ArtifactStore, CapacityEstimate
from evolution.tree.campaign import _Campaign, _open_round
from evolution.tree.campaign_schema import AttemptState, RoundState, campaign_family_for
from evolution.tree.storage.inode_floor import (
    EVALUATION_FIXED_ENTRIES,
    EVALUATION_MEASUREMENT_ENTRIES,
    CampaignTerms,
    InodeNeed,
    campaign_need,
    count_tree,
    evaluation_need,
    evaluation_resume_need,
    measurements_per_round,
    other_entries_per_round,
    round_need,
    whole_run_note,
)
from tests.tree.artifacts.test_artifact_policy import seed
from tests.tree.artifacts.test_artifact_store import POLICY

FIXTURE_RUN = Path(__file__).resolve().parents[2] / "fixtures" / "family14_b2" / "run"


def _terms(**overrides: object) -> CampaignTerms:
    values: dict[str, object] = dict(
        attempts=2,
        parallel=1,
        rounds=2,
        cases=2,
        repeats=1,
        fresh_parent=True,
        refresh=False,
        stability="unstable",
        round_stop="first_accepted",
        probe_budget=None,
        growth=0,
        shared_tmp=False,
        scorer_concurrency=2,
    )
    values.update(overrides)
    return CampaignTerms(**values)  # type: ignore[arg-type]


def _statvfs(*, files: int, favail: int) -> os.statvfs_result:
    # (bsize, frsize, blocks, bfree, bavail, files, ffree, favail, flag, namemax)
    return os.statvfs_result((4096, 4096, 10**9, 10**9, 10**9, files, favail, favail, 0, 255))


@pytest.mark.parametrize(
    ("stability", "round_stop", "fresh", "refresh", "expected"),
    [
        ("stable", "first_accepted", False, False, 4),
        ("stable", "all_attempts", True, True, 7),
        ("unstable", "first_accepted", True, False, 8),
        ("unstable", "first_accepted", False, True, 7),
        ("unstable", "all_attempts", False, False, 5),
        ("unstable", "all_attempts", True, True, 8),
    ],
)
def test_measurements_per_round_follow_the_four_cells(
    stability: str, round_stop: str, fresh: bool, refresh: bool, expected: int
) -> None:
    terms = _terms(stability=stability, round_stop=round_stop, fresh_parent=fresh, refresh=refresh)
    assert measurements_per_round(terms) == expected


def test_other_entries_count_the_probe_only_when_declared() -> None:
    assert other_entries_per_round(_terms()) == 2 + 5 + 6 + 16 * 2
    assert other_entries_per_round(_terms(probe_budget=5)) == 2 + 5 + 2 + 5 + 6 + 16 * 2


def test_the_round_need_is_adr_0040s_formula() -> None:
    terms = _terms(growth=4, shared_tmp=True)
    tree = 3
    expected = (
        tree
        + (tree + 4) * (2 + 1 + 1 * (2 + 1))
        + 8 * (3 + 1)
        + (2 + 5 + 6 + 32)
        + (2 + 1) * (2 * 2)
    )
    assert round_need(terms, tree) == expected


def test_a_fresh_run_adds_its_start_terms_and_a_resume_does_not() -> None:
    terms = _terms()
    fresh = campaign_need(terms, 3, fresh=True, documents=5)
    resumed = campaign_need(terms, 3, fresh=False)
    assert fresh.floor - resumed.floor == 3 + 3 + 1 + 16 + 5
    assert resumed.whole_run == 2 * resumed.floor


def test_packed_measurement_cost_does_not_grow_with_samples(tmp_path: Path) -> None:
    from evolution.tree.execution.scorer_trace_writer import PackedTraceWriter
    from evolution.tree.storage.inode_floor import trace_inodes

    root = tmp_path / "scorer-traces"
    with PackedTraceWriter(root, "measurement") as writer:
        for index in range(12):
            writer.append_request(f"try-{index}", b"{}")
            with writer.streams() as (stdout, stderr):
                stdout.write(b"result")
                writer.append_output(
                    f"try-{index}", stdout, stderr, status="completed", returncode=0
                )
    assert [entry.name for entry in root.rglob("*")] == ["measurement.pack"]
    assert trace_inodes(_terms(cases=4, repeats=3)) == 1


def test_tree_inodes_count_files_directories_and_the_root(tmp_path: Path) -> None:
    source = seed(tmp_path)
    scan = scan_tree(source, POLICY)
    expected = sum(1 for _ in source.rglob("*") if not _.name.startswith(".")) + 1
    assert tree_inodes(scan) <= expected
    assert count_tree(source) >= tree_inodes(scan)
    assert count_tree(tmp_path / "absent") == 0


@pytest.mark.parametrize("free", [99, 100, 101])
def test_the_start_estimate_never_refuses_an_inode_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: int
) -> None:
    source = seed(tmp_path)
    monkeypatch.setattr(os, "statvfs", lambda path: _statvfs(files=10**6, favail=free))
    estimate = ArtifactStore.preflight(
        tmp_path / "run", source, POLICY, 2, inodes=lambda tree: InodeNeed(100)
    )
    assert estimate.free_inodes == free and estimate.inode_need == InodeNeed(100)
    with ArtifactStore.create(
        tmp_path / "run", source, POLICY, parallel_attempts=2, inodes=lambda tree: InodeNeed(100)
    ) as store:
        store.champion().verify()


def test_a_filesystem_without_an_inode_limit_is_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = seed(tmp_path)
    monkeypatch.setattr(os, "statvfs", lambda path: _statvfs(files=0, favail=0))
    estimate = ArtifactStore.preflight(
        tmp_path / "run", source, POLICY, 2, inodes=lambda tree: InodeNeed(100)
    )
    assert estimate.free_inodes is None and estimate.inode_need is None


def test_the_floor_is_computed_from_the_scanned_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = seed(tmp_path)
    seen: list[int] = []
    monkeypatch.setattr(os, "statvfs", lambda path: _statvfs(files=10**6, favail=10**6))

    def need(tree: int) -> InodeNeed:
        seen.append(tree)
        return InodeNeed(tree + 1, whole_run=10**7, per_round=10, start=0)

    estimate = ArtifactStore.preflight(tmp_path / "run", source, POLICY, 2, inodes=need)
    assert seen == [tree_inodes(scan_tree(source, POLICY))]
    assert estimate.free_inodes == 10**6 and estimate.inode_need is not None


def test_the_whole_run_note_names_free_inodes_the_estimate_and_the_rounds_covered() -> None:
    need = InodeNeed(floor=150, whole_run=550, per_round=100, start=50)
    assert whole_run_note(550, need) is None
    assert whole_run_note(349, need) == (
        "free inodes 349 are below this run's estimate of 550 for all its rounds; "
        "by the estimate they last 2 round(s)"
    )
    assert whole_run_note(10, InodeNeed(floor=5)) is None


def test_the_evaluation_floor_and_its_resume_floor_minus_what_exists(tmp_path: Path) -> None:
    creation = evaluation_need(tree=7, samples=6, scorer_concurrency=2, shared_tmp=False)
    assert creation == 7 + 3 + 1 + 13 + 1 + 2 * 2
    root = tmp_path / "eval"
    root.mkdir()
    nothing = evaluation_resume_need(
        root, tree=7, unscored=6, scorer_concurrency=2, shared_tmp=False
    )
    assert nothing == creation
    for entry in (*EVALUATION_FIXED_ENTRIES, *EVALUATION_MEASUREMENT_ENTRIES):
        path = root / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in (".jsonl", ".lock"):
            path.touch()
        else:
            path.mkdir(exist_ok=True)
    everything = evaluation_resume_need(
        root, tree=None, unscored=2, scorer_concurrency=2, shared_tmp=True
    )
    assert everything == 1 + 1 + 3 * 2


def test_a_resume_floor_uses_seed_champion_and_only_open_round_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large rejected Candidate from a closed round does not inflate F."""

    run = tmp_path / "run"
    shutil.copytree(FIXTURE_RUN, run)
    for path in [run, *run.rglob("*")]:
        path.chmod(path.stat().st_mode | 0o200)

    candidates = run / "artifacts" / "candidates"
    historical = candidates / "r01-a02"
    champion = run / "artifacts" / "champions" / "v0"
    open_candidate = candidates / "r02-a01"
    for index in range(20):
        (historical / f"large-rejection-{index}").write_text("x")
    for index in range(5):
        (open_candidate / f"open-growth-{index}").write_text("x")
    for index in range(3):
        (champion / f"champion-growth-{index}").write_text("x")

    def attempt(attempt_id: str, round_number: int, index: int, outcome: str | None):
        return AttemptState(
            attempt_id=attempt_id,
            round=round_number,
            index=index,
            parent_version="v0",
            parent_digest="a" * 64,
            outcome=outcome,
        )

    snapshot = SimpleNamespace(
        rounds={
            1: RoundState(
                round=1,
                baseline_measurement_id="m-r01",
                attempts={"r01-a02": attempt("r01-a02", 1, 2, "rejected")},
                closed={"outcome": "closed"},
            ),
            2: RoundState(
                round=2,
                baseline_measurement_id="m-r02",
                attempts={
                    "r02-a01": attempt("r02-a01", 2, 1, None),
                    "r02-a02": attempt("r02-a02", 2, 2, "rejected"),
                },
            ),
        }
    )
    assert _open_round(snapshot) == 2
    monkeypatch.setattr(campaign_module, "reduce", lambda *_args, **_kwargs: snapshot)
    holder = SimpleNamespace(
        run_root=run,
        family_version=14,
        envelope_family=campaign_family_for(14),
    )

    champion_inodes = count_tree(champion)
    open_inodes = count_tree(open_candidate)
    historical_inodes = count_tree(historical)
    held = max(champion_inodes, open_inodes)
    assert historical_inodes > held
    assert _Campaign._held_tree_inodes(holder) == held  # type: ignore[arg-type]

    source = seed(tmp_path / "seed-source")
    terms = _terms()
    seed_inodes = tree_inodes(scan_tree(source, POLICY))
    expected_f = max(seed_inodes, champion_inodes, open_inodes)
    expected = campaign_need(terms, expected_f, fresh=False)
    historical_floor = campaign_need(
        terms, max(seed_inodes, champion_inodes, open_inodes, historical_inodes), fresh=False
    )
    assert historical_floor.floor > expected.floor

    # At exactly the correct floor admission succeeds; the old parent-only
    # selection would require the larger closed-round Candidate and refuse it.
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda _path: _statvfs(files=10**6, favail=expected.floor),
    )
    estimate = ArtifactStore.preflight(
        tmp_path / "new-run",
        source,
        POLICY,
        2,
        inodes=lambda tree: campaign_need(terms, max(tree, held), fresh=False),
    )
    assert estimate.inode_need == expected
    assert estimate.free_inodes == expected.floor
    assert (
        _Campaign._held_tree_inodes(
            SimpleNamespace(
                run_root=tmp_path / "damaged",
                family_version=14,
                envelope_family=campaign_family_for(14),
            )
        )
        == 0
    )  # type: ignore[arg-type]


def test_the_whole_run_note_reaches_the_notes_sink_once() -> None:
    said: list[str] = []
    holder = SimpleNamespace(notes=said.append)
    need = InodeNeed(floor=150, whole_run=550, per_round=100, start=50)
    _Campaign.note_inodes(holder, CapacityEstimate(1, 1, 1, 349, need))  # type: ignore[arg-type]
    _Campaign.note_inodes(holder, CapacityEstimate(1, 1, 1, 600, need))  # type: ignore[arg-type]
    _Campaign.note_inodes(holder, CapacityEstimate(1, 1, 1))  # type: ignore[arg-type]
    _Campaign.note_inodes(holder, None)  # type: ignore[arg-type]
    assert said == [whole_run_note(349, need)]


@pytest.mark.parametrize(
    ("unscored", "present", "expected"), [(0, False, 5), (1, False, 6), (1, True, 5)]
)
def test_evaluation_only_charges_an_absent_pack_for_unscored_work(
    tmp_path: Path, unscored: int, present: bool, expected: int
) -> None:
    root = tmp_path / "eval"
    root.mkdir()
    source = seed(tmp_path)
    with ArtifactStore.create(root / "artifacts", source, POLICY, parallel_attempts=1):
        pass
    for entry in (
        "evaluation.jsonl",
        "evaluation.lock",
        "measurements/evaluation/events.jsonl",
        "measurements/evaluation/.owner.lock",
        "logs/.keep",
    ):
        path = root / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (root / "scorer-traces").mkdir()
    if present:
        (root / "scorer-traces/evaluation.pack").write_bytes(b"not scanned for capacity")
    assert (
        evaluation_resume_need(
            root, tree=None, unscored=unscored, scorer_concurrency=2, shared_tmp=False
        )
        == expected
    )
    for directory in ("candidates", ".workspaces", ".staging"):
        (root / "artifacts/artifacts" / directory).rmdir()
        expected += 1
        assert (
            evaluation_resume_need(
                root, tree=None, unscored=unscored, scorer_concurrency=2, shared_tmp=False
            )
            == expected
        )


def test_campaign_prefers_one_low_floor_note_and_does_not_claim_dispatch() -> None:
    said: list[str] = []
    holder = SimpleNamespace(notes=said.append)
    estimate = CapacityEstimate(1, 1, 1, 99, InodeNeed(100, 200, 50))
    _Campaign.note_inodes(holder, estimate)
    _Campaign.note_inodes(holder, estimate)
    assert len(said) == 1
    assert "99" in said[0] and "100" in said[0]
    assert "requested" in said[0] and "not block" in said[0]
    assert "external programs" in said[0]


@pytest.mark.parametrize("interrupt", [False, True])
def test_store_observer_is_before_first_write_and_ordinary_errors_are_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    from evolution.tree.artifacts import artifacts

    source = seed(tmp_path)
    root = tmp_path / "run"
    scans = []
    real_scan = artifacts._scan_seed

    def scan(*args):
        scans.append(True)
        return real_scan(*args)

    monkeypatch.setattr(artifacts, "_scan_seed", scan)
    seen = []

    def observer(estimate):
        assert not root.exists()
        seen.append(estimate)
        if interrupt:
            raise KeyboardInterrupt
        raise RuntimeError("broken optional output")

    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            ArtifactStore.create(
                root, source, POLICY, parallel_attempts=1, _estimate_observer=observer
            )
        assert not root.exists()
    else:
        with ArtifactStore.create(
            root, source, POLICY, parallel_attempts=1, _estimate_observer=observer
        ) as store:
            store.champion().verify()
    assert len(seen) == 1 and len(scans) == 1
