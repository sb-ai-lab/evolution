"""The `evo tree trace` export driver and command (Task 4 of #197).

Genuine CLI tests over module-scoped runs of families 12, 13 and 14, one
evaluation run and the observability fakes' special shapes (continued,
rebaselined, refreshed, retried, repaired, call-failing, crashed, linked),
plus edited copies for the mismatch/missing cases and the refusal/journal
errors. The summary line, the header labels and the privacy guarantees are
pinned end to end through `CliRunner`.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from evolution.tree import read_scorer_trace
from evolution.tree.cli import tree_app
from tests import run_observability_fakes as fakes
from tests.campaign_drivers import _only_c1
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import FakeAdapter
from tests.scorer_trace_packs import rewrite_production_trace
from tests.test_tree_train_test import evaluation_manifest as _evaluation_manifest

pytestmark = pytest.mark.slow

CHAMPION = "m-r00-champion"
RUNNER = CliRunner()
SECRET = "trace-export-credential-9f2c"
EVAL_SECRET = "eval-export-credential-51bd"


@pytest.fixture(scope="module")
async def repeated_campaign(tmp_path_factory):
    work = tmp_path_factory.mktemp("repeated_campaign")
    return work, await fakes.genuine_repeated_campaign_run(work)


@pytest.fixture(scope="module")
async def campaign(tmp_path_factory):
    work = tmp_path_factory.mktemp("campaign")
    return work, await fakes.genuine_campaign_run(work)


@pytest.fixture(scope="module")
async def family14(tmp_path_factory):
    work = tmp_path_factory.mktemp("family14")
    return work, await fakes.genuine_family14_run(work)


@pytest.fixture(scope="module")
async def refresh(tmp_path_factory):
    work = tmp_path_factory.mktemp("refresh")
    return work, await fakes.genuine_family14_refresh_run(work)


@pytest.fixture(scope="module")
async def evaluation(tmp_path_factory):
    work = tmp_path_factory.mktemp("evaluation")
    return work, await fakes.genuine_evaluation_run(work)


@pytest.fixture(scope="module")
async def continued(tmp_path_factory):
    return await fakes.genuine_continued_run(tmp_path_factory.mktemp("continued"))


@pytest.fixture(scope="module")
async def rebaselined(tmp_path_factory):
    return await fakes.genuine_rebaselined_run(tmp_path_factory.mktemp("rebaselined"))


@pytest.fixture(scope="module")
async def retried(tmp_path_factory):
    return await fakes.genuine_retried_evaluation_run(tmp_path_factory.mktemp("retried"))


@pytest.fixture(scope="module")
async def repaired(tmp_path_factory):
    return await fakes.genuine_repair_evaluation_run(tmp_path_factory.mktemp("repaired"))


@pytest.fixture(scope="module")
async def callfail(tmp_path_factory):
    return await fakes.genuine_call_failure_evaluation_run(tmp_path_factory.mktemp("callfail"))


@pytest.fixture(scope="module")
async def crashed(tmp_path_factory):
    return await fakes.genuine_crashed_resumed_run(tmp_path_factory.mktemp("crashed"))


@pytest.fixture(scope="module")
async def linked(tmp_path_factory):
    from evolution.tree.artifacts.artifacts import ArtifactStore

    work = tmp_path_factory.mktemp("linked")
    predecessor = await _two_case_run(work / "pred", used=_only_c1)
    champion = ArtifactStore.inspect(predecessor).champion
    seed = predecessor / "artifacts" / "champions" / champion.version_id
    successor = await _two_case_run(work / "succ", used=_only_c1, linked_from=(predecessor, seed))
    return work, predecessor, successor


SECRET_SCORER = r"""
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
view = json.dumps(payload["view"], sort_keys=True)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = ""
if root:
    skill_path = pathlib.Path(root) / "skills" / "SKILL.md"
    if skill_path.is_file():
        skill = skill_path.read_text(encoding="utf-8")
passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))
json.dump(
    {
        "metrics": {"score": 1.0 if passed else 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": passed}],
        "details": {"note": "the forwarded credential is " + os.environ["EVO_TRACE_SECRET"]},
    },
    sys.stdout,
)
"""

EVAL_SECRET_SCORER = r"""
import json
import os
import sys

payload = json.load(sys.stdin)
json.dump(
    {
        "metrics": {"score": 1.0},
        "critical_pass": True,
        "details": {"note": "the evaluation credential is " + os.environ["EVO_EVAL_SECRET"]},
    },
    sys.stdout,
)
"""


async def _genuine_secret_run(work: Path) -> Path:
    """One family-12 campaign whose scorer forwards `EVO_TRACE_SECRET` and
    echoes it into `details`, and whose scripted target messages name it too."""
    from tests.campaign_drivers import two_case_manifest as family12_manifest

    rows = [
        {
            "case_id": case_id,
            "group_id": "g1",
            "stratum": "s1",
            "public": {"text": "hello", "case_id": case_id},
            "oracle": {"critical": True, "pass": True},
            "reflection_metadata": {"uses": []},
            "private": {"script": [{"content": f"the agent message names {SECRET}"}]},
        }
        for case_id in ("c1", "c2")
    ]
    path = family12_manifest(work, rows=rows)
    document = json.loads(path.read_text())
    document["environment"] = {"kind": "scripted"}
    document["scorer"]["allowed_env"] = ["EVO_TRACE_SECRET"]
    document["campaign"].update({"rounds": 1})
    path.write_text(json.dumps(document))
    (work / "scorer" / "score.py").write_text(SECRET_SCORER)
    from evolution.tree.evolve import evolve

    os.environ["EVO_TRACE_SECRET"] = SECRET
    try:
        result = await evolve(path, work / "run", adapter_factory=lambda *a, **k: FakeAdapter())
    finally:
        del os.environ["EVO_TRACE_SECRET"]
    assert result.terminal == "finished"
    return work / "run"


@pytest.fixture(scope="module")
async def secret_run(tmp_path_factory):
    work = tmp_path_factory.mktemp("secret")
    run = await _genuine_secret_run(work)
    return work, run


@pytest.fixture(scope="module")
async def secret_eval(tmp_path_factory):
    from evolution.tree.evaluation import evaluate

    work = tmp_path_factory.mktemp("secret-eval")
    path = _evaluation_manifest(work)
    document = json.loads(path.read_text())
    document["scorer"]["allowed_env"] = ["EVO_EVAL_SECRET"]
    path.write_text(json.dumps(document))
    (work / "scorer" / "score.py").write_text(EVAL_SECRET_SCORER)
    os.environ["EVO_EVAL_SECRET"] = EVAL_SECRET
    try:
        result = await evaluate(path, work / "run", adapter_factory=lambda *args: FakeAdapter())
    finally:
        del os.environ["EVO_EVAL_SECRET"]
    assert result["state"] == "completed"
    return work, path, work / "run"


# -- helpers ---------------------------------------------------------------------


def _invoke(*args: object):
    return RUNNER.invoke(tree_app, ["trace", *[str(a) for a in args]])


def _export(run: Path, out: Path, *extra: object):
    result = _invoke("--run-dir", run, "--out", out, *extra)
    assert result.exit_code == 0, result.output
    return result


def _lines(out: Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


def _header(out: Path) -> dict:
    (head,) = [line for line in _lines(out) if line["record"] == "header"]
    return head


def _tries(out: Path) -> list[dict]:
    return [line for line in _lines(out) if line["record"] == "try"]


def _summary(result) -> str:
    (line,) = [line for line in result.stderr.splitlines() if line.startswith("evo tree trace:")]
    return line


def _journal_events(run: Path, measurement_id: str) -> list[dict]:
    path = fakes.measurement_journal(run, measurement_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _binding_order(run: Path) -> list[str]:
    from evolution.tree.run_reader import measurement_infos, open_run

    handle = open_run(run)
    return [info.id for info in measurement_infos(handle.snapshot)]


def _snapshot_tree(root: Path) -> list[tuple[str, int]]:
    return [
        (str(path.relative_to(root)), path.stat().st_mtime_ns) for path in sorted(root.rglob("*"))
    ]


# -- header and labels (decision 15) ----------------------------------------------


def test_tries_export_in_binding_order_and_journal_order(campaign, tmp_path):
    work, run = campaign
    out = tmp_path / "trace.jsonl"
    _export(run, out, "--manifest", work / "manifest.json")

    order = _binding_order(run)
    tries = _tries(out)
    exported = [line["measurement"]["id"] for line in tries]
    assert list(dict.fromkeys(exported)) == order
    for measurement_id in order:
        journal_order = [
            (record["payload"]["case_id"], record["payload"]["sample_id"])
            for record in _journal_events(run, measurement_id)
            if record["event"] in ("rollout_started", "sample_imported")
        ]
        exported_order = [
            (line["case_id"], line["sample_id"])
            for line in tries
            if line["measurement"]["id"] == measurement_id
        ]
        assert exported_order == journal_order


def test_measurement_filter_keeps_binding_order_and_heads(campaign, tmp_path):
    work, run = campaign
    order = _binding_order(run)
    chosen = [order[-1], order[0]]
    out = tmp_path / "trace.jsonl"
    _export(
        run,
        out,
        "--manifest",
        work / "manifest.json",
        "--measurement",
        chosen[0],
        "--measurement",
        chosen[1],
    )

    tries = _tries(out)
    assert {line["measurement"]["id"] for line in tries} == set(chosen)
    assert list(dict.fromkeys(line["measurement"]["id"] for line in tries)) == [
        name for name in order if name in set(chosen)
    ]
    assert list(_header(out)["journal_heads"]) == [
        "campaign.jsonl",
        *[f"measurements/{name}/events.jsonl" for name in order if name in set(chosen)],
    ]


def test_an_unknown_measurement_refuses(campaign, tmp_path):
    _work, run = campaign
    result = _invoke("--run-dir", run, "--out", tmp_path / "trace.jsonl", "--measurement", "m-nope")
    assert result.exit_code == 2
    assert "evo tree trace: refused: measurement_unknown" in result.stderr
    assert not (tmp_path / "trace.jsonl").exists()


# -- corpus authentication (decision 10) ------------------------------------------


def test_with_a_manifest_every_scored_try_is_authenticated(campaign, evaluation, tmp_path):
    work, run = campaign
    out = tmp_path / "campaign.jsonl"
    _export(run, out, "--manifest", work / "manifest.json")
    checks = {line["request"]["check"] for line in _tries(out)}
    assert checks == {"authenticated"}

    work, run = evaluation
    out = tmp_path / "evaluation.jsonl"
    _export(run, out, "--manifest", work / "evaluation.json")
    checks = {line["request"]["check"] for line in _tries(out)}
    assert checks == {"authenticated"}


def test_a_target_failure_before_scoring_carries_the_corpus_public_and_oracle(retried, tmp_path):
    manifest = _evaluation_manifest(tmp_path / "workspace")
    rows = {
        json.loads(line)["case_id"]: json.loads(line)
        for line in (manifest.parent / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
    }
    out = tmp_path / "trace.jsonl"
    result = _export(retried, out, "--manifest", manifest)

    lines = _tries(out)
    failed = [line for line in lines if line["scoring"]["status"] != "scored"]
    assert len(failed) == len(lines) - 1
    for line in failed:
        assert line["request"]["check"] == "corpus"
        assert line["request"]["public"] == rows[line["case_id"]]["public"]
        assert line["oracle"] == rows[line["case_id"]]["oracle"]
        assert line["rollout"]["class"] == "indeterminate"
        assert line["rollout"]["outcome"] == "target_failed"
        assert line["scoring"]["status"] == "not_started"
        assert line["scoring"]["trace"] == "not_applicable"
    (scored,) = [line for line in lines if line["scoring"]["status"] == "scored"]
    assert scored["request"]["check"] == "authenticated"
    n = len(lines)
    assert _summary(result) == (
        f"evo tree trace: {n} tries written · 1 scored · {n - 1} failed · "
        "0 unavailable · 0 unverified · 0 in flight"
    )


def test_without_a_manifest_a_failed_try_has_no_request(retried, tmp_path):
    out = tmp_path / "trace.jsonl"
    result = _export(retried, out)
    lines = _tries(out)
    failed = [line for line in lines if line["scoring"]["status"] != "scored"]
    assert failed and all(line["request"] is None for line in failed)
    n = len(lines)
    assert _summary(result) == (
        f"evo tree trace: {n} tries written · 1 scored · {n - 1} failed · "
        f"{n - 1} unavailable · 1 unverified · 0 in flight"
    )


# -- scoring outcomes, retries, repairs -------------------------------------------


def test_a_scorer_failure_after_a_target_reply_exports_a_failed_trace(repaired, tmp_path):
    out = tmp_path / "trace.jsonl"
    _export(repaired, out)
    lines = _tries(out)

    (failed,) = [line for line in lines if line["scoring"]["status"] == "failed"]
    assert failed["scoring"]["trace"] == "present"
    assert failed["rollout"]["class"] == "score"
    (repaired_line,) = [line for line in lines if line["repair"] is True]
    assert repaired_line["scoring"]["status"] == "scored"


def test_repeats_and_retries_export_in_ordinal_order(retried, tmp_path):
    out = tmp_path / "trace.jsonl"
    _export(retried, out)
    lines = _tries(out)
    assert [line["retry_ordinal"] for line in lines] == list(range(len(lines)))
    assert all(line["repeat"] == lines[0]["repeat"] for line in lines)


def test_a_retried_call_counts_every_attempt(callfail, tmp_path):
    out = tmp_path / "trace.jsonl"
    _export(callfail, out)
    (line,) = _tries(out)
    assert line["rollout"]["target_calls"] == 2
    assert line["rollout"]["error_code"] == "internal"
    assert line["scoring"]["status"] == "scored"


def test_a_resumed_run_exports_no_duplicate_tries(crashed, tmp_path):
    from evolution.tree.run_reader import measurement_infos, open_run

    handle = open_run(crashed)
    infos = measurement_infos(handle.snapshot)
    out = tmp_path / "trace.jsonl"
    _export(crashed, out)
    lines = _tries(out)
    for info in infos:
        journal = _journal_events(crashed, info.id)
        expected = sum(
            1 for record in journal if record["event"] in ("rollout_started", "sample_imported")
        )
        got = sum(1 for line in lines if line["measurement"]["id"] == info.id)
        assert got == expected, info.id
    seen = set()
    for line in lines:
        key = (
            line["measurement"]["id"],
            line["case_id"],
            line["sample_id"],
            line["retry_ordinal"],
            line["imported"] is not None,
        )
        assert key not in seen
        seen.add(key)


# -- carries (decision 9) ----------------------------------------------------------


def test_a_carry_whose_source_offers_nothing_keeps_provenance_only(linked, tmp_path):
    _work, _predecessor, successor = linked
    out = tmp_path / "trace.jsonl"
    result = _export(successor, out)
    lines = _tries(out)
    assert lines and all(line["imported"] is not None for line in lines)
    for line in lines:
        assert line["scoring"]["trace"] == "missing"
        assert line["request"] is None
        assert line["turns"] is None
        assert line["oracle"] is None
        assert line["scoring"]["status"] == "scored"
        assert line["scoring"]["metrics"] is not None
    n = len(lines)
    assert _summary(result) == (
        f"evo tree trace: {n} tries written · {n} scored · 0 failed · "
        f"{n} unavailable · 0 unverified · 0 in flight"
    )


def test_a_cross_run_carry_reports_provenance_only(linked, tmp_path):
    _work, _predecessor, successor = linked
    copied = tmp_path / "copied"
    shutil.copytree(successor, copied)
    journal = fakes.measurement_journal(copied, CHAMPION)
    rewritten = []
    for raw in journal.read_text(encoding="utf-8").splitlines():
        record = json.loads(raw)
        if record["event"] == "sample_imported":
            record["payload"]["source"]["run_id"] = "another-run"
        rewritten.append(json.dumps(record, sort_keys=True))
    journal.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    out = tmp_path / "trace.jsonl"
    _export(copied, out)
    lines = _tries(out)
    assert lines and all(line["imported"]["run_id"] == "another-run" for line in lines)
    assert all(line["scoring"]["trace"] == "missing" for line in lines)
    assert all(line["request"] is None for line in lines)


# -- epochs and refreshes -----------------------------------------------------------


def test_a_continue_rebind_reports_epoch_and_rebound_from(continued, tmp_path):
    out = tmp_path / "trace.jsonl"
    _export(continued, out)
    lines = _tries(out)
    rebound = [line for line in lines if line["measurement"]["id"] == "m-r01-a01@e1"]
    assert rebound
    assert all(line["measurement"]["epoch"] == 1 for line in rebound)
    assert all(line["measurement"]["rebound_from"] == "m-r01-a01" for line in rebound)
    carried = [line for line in rebound if line["imported"] is not None]
    assert carried and all(line["scoring"]["trace"] == "present" for line in carried)


def test_a_plain_id_measurement_bound_later_reports_epoch_one(rebaselined, tmp_path):
    out = tmp_path / "trace.jsonl"
    _export(rebaselined, out)
    lines = _tries(out)
    assert lines
    round_one = [line for line in lines if not line["measurement"]["id"].startswith("m-r02-")]
    round_two = [line for line in lines if line["measurement"]["id"].startswith("m-r02-")]
    assert round_one and all(line["measurement"]["epoch"] == 0 for line in round_one)
    assert round_two and all(line["measurement"]["epoch"] == 1 for line in round_two)
    champion = [line for line in lines if line["measurement"]["id"] == "m-r02-champion@e1"]
    assert champion and all(line["measurement"]["rebound_from"] is None for line in champion)
    plain = [line for line in lines if line["measurement"]["id"] == "m-r02-a01"]
    assert plain and all(line["measurement"]["rebound_from"] is None for line in plain)


def test_a_family_14_refresh_measurement_reports_role_refresh(refresh, tmp_path):
    _work, run = refresh
    out = tmp_path / "trace.jsonl"
    _export(run, out)
    lines = _tries(out)
    assert "refresh" in {line["measurement"]["role"] for line in lines}
    refreshed = [line for line in lines if line["measurement"]["role"] == "refresh"]
    assert all(line["measurement"]["id"].endswith("-refresh") for line in refreshed)


# -- edited and missing traces ------------------------------------------------------


def _first_scored_request(run: Path) -> str:
    for record in _journal_events(run, CHAMPION):
        if record["event"] == "scoring_started":
            return str(record["payload"]["score_request_id"])
    raise AssertionError("no scoring_started in the champion journal")


def _rewrite_champion_trace(run: Path, request_id: str, edit, *, section="request") -> None:
    """Rebuild only this copied pack with valid framing around changed content."""
    if edit is None:
        rewrite_production_trace(run, request_id, remove=True)
        return
    record = read_scorer_trace(run, request_id, measurement_id=CHAMPION)
    assert record is not None
    document = json.loads(getattr(record, section))
    edit(document)
    rewrite_production_trace(run, request_id, **{section: json.dumps(document).encode()})


def _edited_copy(run: Path, tmp_path: Path, edit) -> tuple[Path, str]:
    copied = tmp_path / "copied"
    shutil.copytree(run, copied)
    request_id = _first_scored_request(run)
    _rewrite_champion_trace(copied, request_id, edit)
    return copied, request_id


@pytest.mark.parametrize("field", ["public", "oracle"])
def test_an_edited_scorer_trace_refuses_its_content(campaign, tmp_path, field):
    if field == "public":

        def edit(document):
            document["input"]["public"]["text"] = "tampered"

    else:

        def edit(document):
            document["input"]["oracle"] = {"critical": False}

    _work, run = campaign
    copied, request_id = _edited_copy(run, tmp_path, edit)
    out = tmp_path / "trace.jsonl"
    _export(copied, out, "--manifest", _work / "manifest.json")
    lines = _tries(out)
    (line,) = [line for line in lines if line["scoring"]["score_request_id"] == request_id]
    assert line["request"] == {"source": "scorer_trace", "check": "mismatch", "public": None}
    assert line["turns"] is None
    assert line["oracle"] is None
    others = [other for other in lines if other["scoring"]["score_request_id"] != request_id]
    assert all(other["request"]["check"] == "authenticated" for other in others)


def test_an_edited_view_fails_its_own_hash_without_a_manifest(campaign, tmp_path):
    def edit(document):
        document["input"]["view"]["tampered"] = True

    _work, run = campaign
    copied, request_id = _edited_copy(run, tmp_path, edit)
    out = tmp_path / "trace.jsonl"
    _export(copied, out)
    (line,) = [line for line in _tries(out) if line["scoring"]["score_request_id"] == request_id]
    assert line["request"]["check"] == "mismatch"


def test_a_deleted_trace_falls_back_to_the_journal(campaign, tmp_path):
    _work, run = campaign
    copied = tmp_path / "copied"
    shutil.copytree(run, copied)
    request_id = _first_scored_request(run)
    _rewrite_champion_trace(copied, request_id, None)

    out = tmp_path / "trace.jsonl"
    _export(copied, out)
    (line,) = [line for line in _tries(out) if line["scoring"]["score_request_id"] == request_id]
    assert line["scoring"]["trace"] == "missing"
    assert line["request"] is None
    assert line["oracle"] is None
    assert line["turns"], "ruling-1 journal turns replace the scorer trace"
    assert all(turn["message"] is None for turn in line["turns"])
    assert line["scoring"]["metrics"] is not None, "the journal keeps its metrics"

    with_manifest = tmp_path / "with-manifest.jsonl"
    _export(copied, with_manifest, "--manifest", _work / "manifest.json")
    (line,) = [
        line for line in _tries(with_manifest) if line["scoring"]["score_request_id"] == request_id
    ]
    assert line["scoring"]["trace"] == "missing"
    assert line["request"]["check"] == "corpus"
    assert line["request"]["public"]["case_id"] == line["case_id"]
    assert line["oracle"] is not None


# -- live cuts ----------------------------------------------------------------------


def _early_primary(run: Path, target: Path, name: str, *, bound: bool = True):
    replay = fakes.JournalReplayer(run, target)
    lines = replay.lines(name)
    stop = next(
        i for i, line in enumerate(lines) if json.loads(line)["event"] == "measurement_bound"
    )
    prefix = lines[: stop + 1] if bound else lines[:1]
    replay.append_records(name, prefix)
    return replay, prefix


@pytest.mark.parametrize("missing", ["measurements", "child", "journal"])
@pytest.mark.parametrize("selected", [False, True])
def test_preflight_unborn_campaign_binding_has_no_tries(campaign, tmp_path, missing, selected):
    work, run = campaign
    replay, prefix = _early_primary(run, tmp_path / "early", "campaign.jsonl")
    if missing != "measurements":
        (replay.target / "measurements").mkdir()
    if missing == "journal":
        (replay.target / "measurements" / CHAMPION).mkdir()
    out = tmp_path / "trace.jsonl"
    extra = ["--measurement", CHAMPION] if selected else []
    _export(replay.target, out, *extra)
    assert _tries(out) == []
    head = _header(out)
    assert head["run_id"] == json.loads(prefix[0])["run_id"]
    assert head["journal_heads"] == {"campaign.jsonl": len(b"".join(prefix))}
    assert head["corpus"] == "absent"
    verified = tmp_path / "verified.jsonl"
    _export(replay.target, verified, "--manifest", work / "manifest.json")
    assert _header(verified)["corpus"] == "verified"


@pytest.mark.parametrize("bound", [False, True])
def test_preflight_early_evaluation_without_manifest_has_no_tries(evaluation, tmp_path, bound):
    _work, run = evaluation
    replay, prefix = _early_primary(run, tmp_path / "early", "evaluation.jsonl", bound=bound)
    out = tmp_path / "trace.jsonl"
    extra = ["--measurement", "evaluation"] if bound else []
    _export(replay.target, out, *extra)
    assert _tries(out) == []
    head = _header(out)
    assert head["run_id"] == json.loads(prefix[0])["run_id"]
    assert head["corpus"] == "absent"
    assert head["producing_version"] is None
    assert head["journal_heads"] == {"evaluation.jsonl": len(b"".join(prefix))}


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("stdout", [False, True])
def test_preflight_evaluation_manifest_requires_readable_child_identity(
    evaluation, tmp_path, bound, stdout
):
    work, run = evaluation
    replay, _prefix = _early_primary(run, tmp_path / "early", "evaluation.jsonl", bound=bound)
    out = tmp_path / "trace.jsonl"
    result = _invoke(
        "--run-dir",
        replay.target,
        "--out",
        "-" if stdout else out,
        "--manifest",
        work / "evaluation.json",
    )
    assert result.exit_code == 1
    assert "evo tree trace: journal unavailable:" in result.stderr
    assert result.stdout == ""
    assert not out.exists()


def test_preflight_evaluation_child_rejects_manifest_corpus_mismatch(evaluation, tmp_path):
    work, run = evaluation
    moved = tmp_path / "workspace"
    shutil.copytree(work, moved, ignore=shutil.ignore_patterns("run"))
    document = json.loads((moved / "evaluation.json").read_text())
    corpus = moved / document["corpus"]["path"]
    rows = [json.loads(line) for line in corpus.read_text().splitlines()]
    for row in rows:
        row["public"]["text"] = "tampered after evaluation"
    corpus.write_text("".join(json.dumps(row) + "\n" for row in rows))
    out = tmp_path / "trace.jsonl"
    result = _invoke("--run-dir", run, "--out", out, "--manifest", moved / "evaluation.json")
    assert result.exit_code == 2
    assert "evo tree trace: refused: corpus_digest_mismatch" in result.stderr
    assert not out.exists()


def test_preflight_campaign_header_keeps_admitted_raw_prefix(campaign, tmp_path, monkeypatch):
    from evolution.tree import run_trace

    _work, run = campaign
    replay, prefix = _early_primary(run, tmp_path / "early", "campaign.jsonl")
    # Valid JSON need not use Envelope.encode's whitespace. Provenance names raw bytes.
    prefix = tuple(b"  " + line for line in prefix)
    (replay.target / "campaign.jsonl").write_bytes(b"".join(prefix))
    shutil.copytree(run / "measurements" / CHAMPION, replay.target / "measurements" / CHAMPION)
    original = run_trace.open_run

    def append_after_admission(path):
        handle = original(path)
        replay.append_records(
            "campaign.jsonl", replay.lines("campaign.jsonl")[len(prefix) : len(prefix) + 1]
        )
        return handle

    monkeypatch.setattr(run_trace, "open_run", append_after_admission)
    out = tmp_path / "trace.jsonl"
    _export(replay.target, out)
    assert _tries(out)
    assert _header(out)["journal_heads"]["campaign.jsonl"] == len(b"".join(prefix))
    assert (replay.target / "campaign.jsonl").stat().st_size > len(b"".join(prefix))


@pytest.mark.parametrize("entry", ["measurements", "child", "journal"])
def test_preflight_dangling_symlink_is_not_an_unborn_child(campaign, tmp_path, entry):
    _work, run = campaign
    replay, _prefix = _early_primary(run, tmp_path / "early", "campaign.jsonl")
    paths = [replay.target / "measurements", replay.target / "measurements" / CHAMPION]
    if entry != "measurements":
        paths[0].mkdir()
    if entry == "journal":
        paths[1].mkdir()
    path = {"measurements": paths[0], "child": paths[1], "journal": paths[1] / "events.jsonl"}[
        entry
    ]
    path.symlink_to(tmp_path / "absent")
    out = tmp_path / "trace.jsonl"
    result = _invoke("--run-dir", replay.target, "--out", out)
    assert result.exit_code == 1
    assert "evo tree trace: journal unavailable:" in result.stderr
    assert not out.exists()


@pytest.mark.parametrize("unsafe", ["foreign", "permission", "not_directory"])
def test_preflight_unsafe_measurements_parent_is_not_unborn(
    campaign, tmp_path, monkeypatch, unsafe
):
    _work, run = campaign
    replay, _prefix = _early_primary(run, tmp_path / "early", "campaign.jsonl")
    parent = replay.target / "measurements"
    if unsafe == "not_directory":
        parent.touch()
    else:
        parent.mkdir()
    if unsafe == "foreign":
        inode = parent.stat().st_ino
        real_fstat = os.fstat

        def foreign(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:
                return SimpleNamespace(st_uid=info.st_uid + 1)
            return info

        monkeypatch.setattr(os, "fstat", foreign)
    elif unsafe == "permission":
        real_open = os.open

        def denied(path, flags, mode=0o777, *, dir_fd=None):
            if str(path) == "measurements":
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", denied)
    out = tmp_path / "trace.jsonl"
    result = _invoke("--run-dir", replay.target, "--out", out)
    assert result.exit_code == 1
    assert "evo tree trace: journal unavailable:" in result.stderr
    assert not out.exists()


@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
def test_preflight_completed_missing_child_remains_a_fault(campaign, evaluation, tmp_path, kind):
    _work, source = campaign if kind == "campaign" else evaluation
    run = tmp_path / "copied"
    shutil.copytree(source, run)
    measurement = CHAMPION if kind == "campaign" else "evaluation"
    shutil.rmtree(run / "measurements" / measurement)
    out = tmp_path / "trace.jsonl"
    result = _invoke("--run-dir", run, "--out", out)
    assert result.exit_code == 1
    assert "evo tree trace: journal unavailable:" in result.stderr
    assert not out.exists()


def _cut_after(run: Path, copied: Path, measurement_id: str, event: str) -> None:
    shutil.copytree(run, copied)
    journal = fakes.measurement_journal(copied, measurement_id)
    lines = journal.read_text(encoding="utf-8").splitlines()
    index = next(i for i, raw in enumerate(lines) if json.loads(raw)["event"] == event)
    journal.write_text("\n".join(lines[: index + 1]) + "\n", encoding="utf-8")


def test_a_try_without_a_rollout_terminal_is_in_flight(campaign, tmp_path):
    _work, run = campaign
    copied = tmp_path / "copied"
    _cut_after(run, copied, CHAMPION, "rollout_started")
    out = tmp_path / "trace.jsonl"
    result = _export(copied, out)
    assert all(line["measurement"]["id"] != CHAMPION for line in _tries(out))
    assert result.stderr.strip().endswith("· 1 in flight")


def test_a_scoring_started_without_a_terminal_is_unsettled(campaign, tmp_path):
    _work, run = campaign
    copied = tmp_path / "copied"
    _cut_after(run, copied, CHAMPION, "scoring_started")
    out = tmp_path / "trace.jsonl"
    result = _export(copied, out)
    (line,) = [
        line
        for line in _tries(out)
        if line["measurement"]["id"] == CHAMPION and line["scoring"]["status"] == "unsettled"
    ]
    assert line["scoring"]["metrics"] is None
    assert result.stderr.strip().endswith("· 0 in flight")


# -- refusals (exit 2) ---------------------------------------------------------------


def test_an_existing_out_refuses_and_leaves_it_unchanged(campaign, tmp_path):
    _work, run = campaign
    out = tmp_path / "trace.jsonl"
    out.write_text("precious", encoding="utf-8")
    result = _invoke("--run-dir", run, "--out", out)
    assert result.exit_code == 2
    assert "evo tree trace: refused: trace_out_exists" in result.stderr
    assert out.read_text(encoding="utf-8") == "precious"


def test_an_out_inside_the_run_directory_refuses(campaign, tmp_path):
    _work, run = campaign
    result = _invoke("--run-dir", run, "--out", run / "trace.jsonl")
    assert result.exit_code == 2
    assert "evo tree trace: refused: trace_out_inside_run_dir" in result.stderr
    assert not (run / "trace.jsonl").exists()


def test_a_symlinked_out_parent_inside_the_run_refuses(campaign, tmp_path):
    _work, run = campaign
    link = tmp_path / "link"
    link.symlink_to(run, target_is_directory=True)
    result = _invoke("--run-dir", run, "--out", link / "trace.jsonl")
    assert result.exit_code == 2
    assert "evo tree trace: refused: trace_out_inside_run_dir" in result.stderr


def test_a_directory_without_a_journal_refuses(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _invoke("--run-dir", empty, "--out", tmp_path / "trace.jsonl")
    assert result.exit_code == 2
    assert "evo tree trace: refused: not_a_run_directory" in result.stderr


def test_a_directory_with_both_journals_refuses(tmp_path):
    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    (ambiguous / "campaign.jsonl").touch()
    (ambiguous / "evaluation.jsonl").touch()
    result = _invoke("--run-dir", ambiguous, "--out", tmp_path / "trace.jsonl")
    assert result.exit_code == 2
    assert "evo tree trace: refused: run_dir_ambiguous" in result.stderr


def test_an_evaluation_manifest_for_a_campaign_run_refuses(campaign, evaluation, tmp_path):
    _work, run = campaign
    eval_work, _eval_run = evaluation
    result = _invoke(
        "--run-dir",
        run,
        "--out",
        tmp_path / "trace.jsonl",
        "--manifest",
        eval_work / "evaluation.json",
    )
    assert result.exit_code == 2
    assert "evo tree trace: refused: manifest_kind_mismatch" in result.stderr


def test_a_campaign_manifest_for_an_evaluation_run_refuses(campaign, evaluation, tmp_path):
    work, _run = campaign
    _eval_work, eval_run = evaluation
    result = _invoke(
        "--run-dir",
        eval_run,
        "--out",
        tmp_path / "trace.jsonl",
        "--manifest",
        work / "manifest.json",
    )
    assert result.exit_code == 2
    assert "evo tree trace: refused: manifest_kind_mismatch" in result.stderr


def test_a_corpus_edited_after_the_run_refuses(campaign, tmp_path):
    work, run = campaign
    moved = tmp_path / "workspace"
    shutil.copytree(work, moved, ignore=shutil.ignore_patterns("run"))
    corpus = moved / "corpus.jsonl"
    lines = corpus.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["public"]["text"] = "tampered after the run"
    corpus.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n", encoding="utf-8")
    result = _invoke(
        "--run-dir", run, "--out", tmp_path / "trace.jsonl", "--manifest", moved / "manifest.json"
    )
    assert result.exit_code == 2
    assert "evo tree trace: refused: corpus_digest_mismatch" in result.stderr


def test_max_field_bytes_below_one_is_a_usage_error(campaign, tmp_path):
    _work, run = campaign
    result = _invoke("--run-dir", run, "--out", tmp_path / "t.jsonl", "--max-field-bytes", "0")
    assert result.exit_code == 2


def test_a_race_on_out_creation_is_still_the_exists_refusal(campaign, tmp_path, monkeypatch):
    _work, run = campaign

    def raced(*_args, **_kwargs):
        raise FileExistsError(errno.EEXIST, "the name appeared between the check and the open")

    monkeypatch.setattr("evolution.tree.run_trace.open_private_file", raced)
    result = _invoke("--run-dir", run, "--out", tmp_path / "trace.jsonl")
    assert result.exit_code == 2
    assert "evo tree trace: refused: trace_out_exists" in result.stderr
    assert "Traceback" not in result.output
    assert str(tmp_path) not in result.stderr


def test_an_unwritable_out_parent_is_output_unavailable(campaign, tmp_path, monkeypatch):
    _work, run = campaign

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "the parent directory is not writable")

    monkeypatch.setattr("evolution.tree.run_trace.open_private_file", denied)
    result = _invoke("--run-dir", run, "--out", tmp_path / "trace.jsonl")
    assert result.exit_code == 1
    assert "evo tree trace: output unavailable: eacces" in result.stderr
    assert "Traceback" not in result.output
    assert str(tmp_path) not in result.stderr


# -- journal errors (exit 1) ---------------------------------------------------------


def test_a_corrupt_measurement_journal_exits_one_and_leaves_no_out(campaign, tmp_path):
    _work, run = campaign
    copied = tmp_path / "copied"
    shutil.copytree(run, copied)
    journal = fakes.measurement_journal(copied, CHAMPION)
    with journal.open("ab") as handle:
        handle.write(b'{"schema_version": 1, "not": "an envelope"}\n')
    out = tmp_path / "trace.jsonl"
    result = _invoke("--run-dir", copied, "--out", out)
    assert result.exit_code == 1
    assert "evo tree trace: journal unavailable: corrupt_suffix" in result.stderr
    assert not out.exists()


# -- privacy -------------------------------------------------------------------------


def test_the_out_file_is_owner_only(campaign, tmp_path):
    _work, run = campaign
    out = tmp_path / "trace.jsonl"
    _export(run, out)
    details = os.stat(out)
    assert stat.S_IMODE(details.st_mode) == 0o600
    assert details.st_uid == os.geteuid()


def test_dash_out_writes_the_trace_to_stdout_and_the_summary_to_stderr(campaign, tmp_path):
    _work, run = campaign
    result = _invoke("--run-dir", run, "--out", "-")
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[0]["record"] == "header"
    assert any(line["record"] == "try" for line in lines)
    assert _summary(result).startswith("evo tree trace: ")
    assert not (Path.cwd() / "-").exists()


def test_the_export_writes_nothing_under_the_run_directory(campaign, tmp_path):
    work, run = campaign
    before = _snapshot_tree(run)
    _export(run, tmp_path / "trace.jsonl", "--manifest", work / "manifest.json")
    assert _snapshot_tree(run) == before


def test_campaign_redaction_covers_the_scorer_output_and_the_agent_message(
    secret_run, monkeypatch, tmp_path
):
    work, run = secret_run
    monkeypatch.setenv("EVO_TRACE_SECRET", SECRET)
    out = tmp_path / "trace.jsonl"
    _export(run, out, "--manifest", work / "manifest.json")

    text = out.read_text(encoding="utf-8")
    assert SECRET not in text
    assert _header(out)["redaction"] == "campaign_secret_set"
    details = [
        line["scoring"]["output"]["details"]
        for line in _tries(out)
        if line["scoring"]["output"] is not None
    ]
    assert any("[REDACTED]" in json.dumps(note) for note in details)
    messages = [turn["message"] for line in _tries(out) if line["turns"] for turn in line["turns"]]
    assert messages and all(SECRET not in json.dumps(message) for message in messages)
    assert any("[REDACTED]" in json.dumps(message) for message in messages)


def test_evaluation_redaction_uses_the_evaluation_secret_set(secret_eval, monkeypatch, tmp_path):
    _work, manifest, run = secret_eval
    monkeypatch.setenv("EVO_EVAL_SECRET", EVAL_SECRET)
    out = tmp_path / "trace.jsonl"
    _export(run, out, "--manifest", manifest)
    text = out.read_text(encoding="utf-8")
    assert EVAL_SECRET not in text
    assert _header(out)["redaction"] == "evaluation_secret_set"
    assert "[REDACTED]" in text


def test_campaign_redacts_present_scorer_secret_when_chat_editor_key_is_unset(
    campaign, monkeypatch, tmp_path
):
    from tests.tree.manifest.test_manifest_v2 import CHAT, MODEL_GROUPING

    work, run = campaign
    workspace = tmp_path / "workspace"
    shutil.copytree(work, workspace, ignore=shutil.ignore_patterns("run"))
    copied_run = tmp_path / "run"
    shutil.copytree(run, copied_run)
    path = workspace / "manifest.json"
    document = json.loads(path.read_text())
    document["editor"] = {**CHAT, "retry": MODEL_GROUPING["retry"]}
    document["scorer"]["allowed_env"] = ["EVO_TRACE_SECRET"]
    from tests.tree_evolve_support import lift_document

    path.write_text(json.dumps(lift_document(document, "evolution-v2-manifest/15")))
    monkeypatch.setenv("EVO_TRACE_SECRET", SECRET)
    monkeypatch.delenv("EVO_EDITOR_API_KEY", raising=False)

    def edit(scoring):
        scoring["details"] = {"note": f"the forwarded credential is {SECRET}"}

    _rewrite_champion_trace(copied_run, _first_scored_request(copied_run), edit, section="stdout")

    out = tmp_path / "trace.jsonl"
    result = _export(copied_run, out, "--manifest", path)

    assert SECRET not in out.read_text()
    assert _header(out)["redaction"] == "campaign_secret_set"
    assert _summary(result).endswith(" · 1 declared secret variables unset")
    assert any(
        line["scoring"]["output"]["details"] == {"note": "the forwarded credential is [REDACTED]"}
        for line in _tries(out)
        if line["scoring"]["output"] is not None
    )


@pytest.mark.parametrize(
    "case,unset",
    [
        ("editor_empty_key", 1),
        ("editor_invalid_proxy", 0),
        ("reflector_missing_key", 1),
        ("agent_editor_missing_name", 1),
        ("unsafe_agent_editor_missing_name", 1),
        ("agent_reflector_missing_name", 1),
        ("grouper_missing_key", 1),
        ("grouper_invalid_proxy", 0),
        ("command_grouper_missing_name", 1),
        ("agent_grouper_missing_name", 1),
        ("unsafe_agent_grouper_missing_name", 1),
    ],
)
def test_campaign_partial_credentials_keep_other_available_trace_secrets(
    campaign, family14, tmp_path, case, unset
):
    from evolution.tree.run_trace import export_run
    from tests.tree.manifest.test_manifest_v2 import AGENT, CHAT, MODEL_GROUPING
    from tests.tree.manifest.test_manifest_v15 import AGENT_GROUPER, CHAT_GROUPER, COMMAND_GROUPER

    work, run = family14 if "grouper" in case else campaign
    workspace = tmp_path / "workspace"
    shutil.copytree(work, workspace, ignore=shutil.ignore_patterns("run"))
    copied_run = tmp_path / "run"
    shutil.copytree(run, copied_run)
    path = workspace / "manifest.json"
    document = json.loads(path.read_text())
    document["scorer"]["allowed_env"] = ["EVO_TRACE_SECRET"]
    document["editor"] = dict(AGENT)
    common = {
        key: value
        for key, value in document["reflector"].items()
        if key not in {"kind", "argv", "cwd", "dependency_paths", "allowed_env", "timeout_seconds"}
    }
    document["reflector"] = {**common, **AGENT}
    environment = {"EVO_TRACE_SECRET": SECRET, "OPENAI_API_KEY": "curated-agent-credential"}
    needles = [SECRET, "curated-agent-credential"]
    if case.startswith("editor_") or case == "reflector_missing_key":
        role = "reflector" if case.startswith("reflector") else "editor"
        document[role] = {
            **(common if role == "reflector" else {}),
            **CHAT,
            "api_key_env": "EVO_TRACE_CHAT_KEY",
            "proxy_env": "EVO_TRACE_CHAT_PROXY",
            "retry": MODEL_GROUPING["retry"],
        }
        if case == "editor_empty_key":
            environment["EVO_TRACE_CHAT_KEY"] = ""
        if case == "editor_invalid_proxy":
            environment["EVO_TRACE_CHAT_KEY"] = "present-chat-credential"
            needles.append("present-chat-credential")
        proxy = "invalid-proxy-credential" if "invalid" in case else "https://proxy.example:8443"
        environment["EVO_TRACE_CHAT_PROXY"] = proxy
        needles.append(proxy)
    elif case.startswith("model_grouping"):
        document["reflector"]["grouping"] = {
            **MODEL_GROUPING,
            "api_key_env": "EVO_TRACE_GROUP_KEY",
            "proxy_env": "EVO_TRACE_GROUP_PROXY",
        }
        if case != "model_grouping_missing_key":
            environment["EVO_TRACE_GROUP_KEY"] = "grouping-model-credential"
            needles.append("grouping-model-credential")
        if case != "model_grouping_missing_proxy":
            proxy = "invalid-group-proxy" if "invalid" in case else "https://group-proxy.example"
            environment["EVO_TRACE_GROUP_PROXY"] = proxy
            needles.append(proxy)
    elif case == "command_grouping_missing_name":
        document["reflector"]["grouping"] = {
            **document["scorer"],
            "kind": "command",
            "allowed_env": ["EVO_TRACE_GROUP_TOKEN", "EVO_TRACE_MISSING"],
            "config": {},
        }
        environment["EVO_TRACE_GROUP_TOKEN"] = "command-grouping-credential"
        needles.append("command-grouping-credential")
    elif case in {"grouper_missing_key", "grouper_invalid_proxy"}:
        document["grouper"] = {**CHAT_GROUPER, "proxy_env": "EVO_TRACE_GROUP_PROXY"}
        if "invalid" in case:
            environment["GROUPER_API_KEY"] = "grouper-chat-credential"
            needles.append("grouper-chat-credential")
        proxy = "invalid-grouper-proxy" if "invalid" in case else "https://grouper-proxy.example"
        environment["EVO_TRACE_GROUP_PROXY"] = proxy
        needles.append(proxy)
    elif case == "command_grouper_missing_name":
        document["grouper"] = {
            **COMMAND_GROUPER,
            "allowed_env": ["EVO_TRACE_GROUP_TOKEN", "EVO_TRACE_MISSING"],
        }
        environment["EVO_TRACE_GROUP_TOKEN"] = "command-grouper-credential"
        needles.append("command-grouper-credential")
    else:
        role = "grouper" if "grouper" in case else "reflector" if "reflector" in case else "editor"
        document[role] = {
            **(AGENT_GROUPER if role == "grouper" else common if role == "reflector" else AGENT),
            **AGENT,
            "allowed_env": ["EVO_TRACE_AGENT_TOKEN", "EVO_TRACE_MISSING", "PATH"],
            "permission": "unsafe" if case.startswith("unsafe") else "safe",
        }
        environment.update(
            {
                "EVO_TRACE_AGENT_TOKEN": "declared-agent-credential",
                "PATH": "runtime-path-preserved",
                "EVO_TRACE_UNRELATED": "unrelated-credential",
            }
        )
        needles.append("declared-agent-credential")
        if case.startswith("unsafe"):
            needles.append("unrelated-credential")
    from tests.tree_evolve_support import lift_document

    path.write_text(json.dumps(lift_document(document, "evolution-v2-manifest/15")))

    def edit(scoring):
        scoring["details"] = {"echo": needles, "runtime": "runtime-path-preserved"}
        if "agent_" in case and not case.startswith("unsafe"):
            scoring["details"]["unrelated"] = "unrelated-credential"

    _rewrite_champion_trace(copied_run, _first_scored_request(copied_run), edit, section="stdout")

    out = tmp_path / "trace.jsonl"
    summary = export_run(run_dir=copied_run, out=out, manifest=path, environ=environment)

    text = out.read_text()
    assert all(needle not in text for needle in needles)
    assert _header(out)["redaction"] == "campaign_secret_set"
    assert summary.unset_secrets == unset
    details = [
        line["scoring"]["output"]["details"]
        for line in _tries(out)
        if line["scoring"]["output"] is not None and line["scoring"]["output"]["details"]
    ]
    (echo,) = [detail for detail in details if "echo" in detail]
    assert echo["echo"] == ["[REDACTED]"] * len(needles)
    assert echo["runtime"] == "runtime-path-preserved"
    if "agent_" in case and not case.startswith("unsafe"):
        assert echo["unrelated"] == "unrelated-credential"


@pytest.mark.parametrize("supplied", [False, True])
def test_campaign_trace_reads_secrets_from_one_environment_snapshot(
    campaign, monkeypatch, tmp_path, supplied
):
    from evolution.tree import run_trace

    work, run = campaign
    workspace = tmp_path / "workspace"
    shutil.copytree(work, workspace, ignore=shutil.ignore_patterns("run"))
    copied_run = tmp_path / "run"
    shutil.copytree(run, copied_run)
    path = workspace / "manifest.json"
    document = json.loads(path.read_text())
    document["scorer"]["allowed_env"] = ["EVO_TRACE_SECRET"]
    path.write_text(json.dumps(document))
    environment = {"EVO_TRACE_SECRET": SECRET}
    monkeypatch.setenv("EVO_TRACE_SECRET", SECRET)
    original = run_trace.open_run

    def change_environment_after_snapshot(root):
        environment["EVO_TRACE_SECRET"] = "replacement-credential"
        monkeypatch.setenv("EVO_TRACE_SECRET", "replacement-credential")
        return original(root)

    monkeypatch.setattr(run_trace, "open_run", change_environment_after_snapshot)

    def edit(scoring):
        scoring["details"] = {"note": SECRET}

    _rewrite_champion_trace(copied_run, _first_scored_request(copied_run), edit, section="stdout")
    out = tmp_path / "trace.jsonl"

    summary = run_trace.export_run(
        run_dir=copied_run, out=out, manifest=path, environ=environment if supplied else None
    )

    assert SECRET not in out.read_text()
    assert any(
        line["scoring"]["output"]["details"] == {"note": "[REDACTED]"}
        for line in _tries(out)
        if line["scoring"]["output"] is not None
    )
    assert summary.unset_secrets == 0


def test_declared_but_unset_secret_names_are_counted(campaign, evaluation, tmp_path):
    work, run = campaign
    moved = tmp_path / "workspace"
    shutil.copytree(work, moved, ignore=shutil.ignore_patterns("run"))
    manifest = json.loads((moved / "manifest.json").read_text(encoding="utf-8"))
    manifest["scorer"]["allowed_env"] = ["EVO_TRACE_UNSET_CREDENTIAL"]
    (moved / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = _export(run, tmp_path / "trace.jsonl", "--manifest", moved / "manifest.json")
    assert _summary(result).endswith(" · 1 declared secret variables unset")

    _eval_work, eval_run = evaluation
    eval_workspace = tmp_path / "eval-workspace"
    shutil.copytree(_eval_work, eval_workspace, ignore=shutil.ignore_patterns("run"))
    document = json.loads((eval_workspace / "evaluation.json").read_text(encoding="utf-8"))
    document["scorer"]["allowed_env"] = ["EVO_EVAL_UNSET_CREDENTIAL"]
    (eval_workspace / "evaluation.json").write_text(json.dumps(document), encoding="utf-8")
    result = _export(
        eval_run, tmp_path / "eval-trace.jsonl", "--manifest", eval_workspace / "evaluation.json"
    )
    assert _summary(result).endswith(" · 1 declared secret variables unset")


# -- retired runs (reader only) ------------------------------------------------------
