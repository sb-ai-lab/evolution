"""The trace record builder (Task 3 of #197): request authentication (decision 7),
scorer-output labels (decision 8), the try record (spec §3.2/§3.4, ruling 1),
redaction and the field cap (decisions 11 and 12), and the `evaluation_secrets`
extraction (decision 14).

The records and trace documents come from one genuine family-13 run; tests that
edit a trace copy the files into `tmp_path` first. Carries (decision 9) and the
export driver are Task 4; the journal record shape is pinned by Task 2.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from evolution import __version__
from evolution.tree import read_scorer_trace
from evolution.tree.evaluation import evaluation_secrets
from evolution.tree.evaluation_manifest import load_evaluation_manifest
from evolution.tree.execution.corpus_partition import load_partition
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.manifest.manifest_v2 import load_manifest_v2
from evolution.tree.roles.config import AgentCliEditorSection, ChatEditorSection
from evolution.tree.run_reader import (
    ImportedProvenance,
    MeasurementInfo,
    TryRollout,
    TryScoring,
    TryState,
    measurement_infos,
    open_run,
    tries,
)
from evolution.tree.run_trace import (
    TraceDirReader,
    build_try,
    cap_fields,
    check_request,
    header,
    redact_try,
)
from evolution.tree.storage.envelope import Envelope
from tests import run_observability_fakes as fakes
from tests.scorer_trace_packs import rewrite_production_trace, write_directory_trace
from tests.test_tree_train_test import evaluation_manifest

pytestmark = pytest.mark.slow

CHAMPION = "m-r00-champion"


@pytest.mark.parametrize(
    ("schema", "loader"),
    [("evolution-v2-manifest/15", "partition"), ("evolution-v2-manifest/16", "reflection")],
)
def test_native_trace_corpus_loader_dispatch_is_unchanged(tmp_path, monkeypatch, schema, loader):
    """C1 removes retired entries only; /16's existing dispatch is not constant with /15."""
    from types import SimpleNamespace

    from evolution.tree import run_trace
    from evolution.tree.execution import corpus, corpus_partition
    from evolution.tree.execution.interaction import EvalCase
    from evolution.tree.execution.journal_identity import corpus_digest
    from evolution.tree.manifest import manifest_v2

    workflow = SimpleNamespace(corpus_path=tmp_path / "corpus.jsonl")
    admitted = SimpleNamespace(schema=schema, workflow=workflow)
    case = EvalCase("case", "group", "stratum", (), {}, {}, {})
    bound = SimpleNamespace(cases=(case,))
    seen = []

    def partition(path):
        assert path == workflow.corpus_path
        seen.append("partition")

        def select(split):
            assert split == "train"
            return bound

        return SimpleNamespace(select=select)

    def reflection(path):
        assert path == workflow.corpus_path
        seen.append("reflection")
        return bound

    monkeypatch.setattr(manifest_v2, "load_manifest_v2", lambda _: admitted)
    monkeypatch.setattr(corpus_partition, "load_partition", partition)
    monkeypatch.setattr(corpus, "load_reflection_corpus", reflection)
    monkeypatch.setattr(run_trace, "_campaign_trace_secrets", lambda *_: ((), 0))
    campaign = SimpleNamespace(identity={"corpus_digest": corpus_digest(bound.cases)})
    assert run_trace._bind_campaign_manifest(tmp_path / "manifest.json", campaign, {}) == (
        {"case": case},
        (),
        0,
        "campaign_secret_set",
    )
    assert seen == [loader]


@pytest.fixture(scope="module")
async def campaign(tmp_path_factory):
    work = tmp_path_factory.mktemp("campaign")
    run = await fakes.genuine_campaign_run(work)
    return work, run


@pytest.fixture(scope="module")
def campaign_corpus(campaign):
    work, _run = campaign
    cases = load_partition(work / "corpus.jsonl").select("train").cases
    return {case.case_id: case for case in cases}


@pytest.fixture(scope="module")
async def evaluation(tmp_path_factory):
    work = tmp_path_factory.mktemp("evaluation")
    run = await fakes.genuine_evaluation_run(work)
    return work, run


def _records_of(run: Path, measurement_id: str = CHAMPION) -> tuple[Envelope, ...]:
    return inspect_journal(fakes.measurement_journal(run, measurement_id).parent).records


def _states(records: tuple[Envelope, ...]) -> list[TryState]:
    return tries(records, records[0].payload["identity"])


def _state_for(records: tuple[Envelope, ...], case_id: str) -> TryState:
    (state,) = [state for state in _states(records) if state.case_id == case_id]
    return state


def _info_for(run: Path, measurement_id: str) -> MeasurementInfo:
    infos = {info.id: info for info in measurement_infos(open_run(run).snapshot)}
    return infos[measurement_id]


def _request(run: Path, request_id: str) -> dict:
    trace = read_scorer_trace(run, request_id)
    assert trace is not None
    return trace.request_document()


def _reader(run: Path, records: tuple[Envelope, ...]) -> TraceDirReader:
    return TraceDirReader(run, records)


def _build(
    run: Path,
    records: tuple[Envelope, ...],
    info: MeasurementInfo,
    state: TryState,
    corpus,
    carried=None,
) -> dict:
    reader = _reader(run, records)
    try:
        return build_try(info, state, trace_dir_reader=reader, corpus=corpus, carried=carried)
    finally:
        reader.close()


def _rebound(document: dict) -> dict:
    """Recompute `input_sha256` after editing `input`, so only the journal
    binding (`scoring_input_digest`) can catch the edit (decision 7)."""

    payload = document["input"]
    document["input_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return document


def _write_trace_request(run: Path, score_request_id: str, document: dict) -> None:
    rewrite_production_trace(run, score_request_id, request=json.dumps(document).encode())


def _scored_inputs(campaign, campaign_corpus):
    """(document, started, case, scorer, candidate_digest) for the c1 try."""

    _work, run = campaign
    records = _records_of(run)
    state = _state_for(records, "c1")
    started = state.scoring.started
    document = _request(run, started.payload["score_request_id"])
    return (
        document,
        started,
        campaign_corpus["c1"],
        started.payload["scorer"],
        records[0].payload["identity"]["candidate_digest"],
    )


def _edited_trace_run(campaign, tmp_path, case_id: str = "c1") -> tuple[Path, str]:
    """An ephemeral copy of traces and unchanged measurement journals."""

    _work, run = campaign
    copy = tmp_path / f"run-{_next_copy()}"
    shutil.copytree(run / "scorer-traces", copy / "scorer-traces")
    shutil.copytree(run / "measurements", copy / "measurements")
    request_id = _state_for(_records_of(run), case_id).scoring.started.payload["score_request_id"]
    return copy, request_id


_COPY_COUNTER = itertools.count()


def _next_copy() -> int:
    return next(_COPY_COUNTER)


# -- check_request (decision 7) -------------------------------------------------


def test_check_request_authenticates_an_untouched_trace(campaign, campaign_corpus):
    document, started, case, scorer, candidate_digest = _scored_inputs(campaign, campaign_corpus)
    assert check_request(document, started, case, scorer, candidate_digest) == "authenticated"


@pytest.mark.parametrize("field", ["public", "oracle", "view"])
def test_check_request_mismatches_an_edited_bound_field_with_a_rebound_hash(
    campaign, campaign_corpus, tmp_path, field
):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    document = _request(run, request_id)
    if field == "public":
        document["input"]["public"]["text"] = "tampered"
    elif field == "oracle":
        document["input"]["oracle"]["critical"] = False
    else:
        document["input"]["view"]["terminal"] = "rollout_failed"
    _write_trace_request(copy, request_id, _rebound(document))

    records = _records_of(run)
    state = _state_for(records, "c1")
    check = check_request(
        document,
        state.scoring.started,
        campaign_corpus["c1"],
        state.scoring.started.payload["scorer"],
        records[0].payload["identity"]["candidate_digest"],
    )
    assert check == "mismatch"


def test_check_request_mismatches_an_edited_input_sha256(campaign, campaign_corpus, tmp_path):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    document = _request(run, request_id)
    document["input_sha256"] = "f" * 64
    _write_trace_request(copy, request_id, document)

    records = _records_of(run)
    state = _state_for(records, "c1")
    assert (
        check_request(
            document,
            state.scoring.started,
            campaign_corpus["c1"],
            state.scoring.started.payload["scorer"],
            records[0].payload["identity"]["candidate_digest"],
        )
        == "mismatch"
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda document: document.update(trace_id="forged-trace"),
        lambda document: document.update(scorer="e" * 64),
        lambda document: document.update(extra=True),
    ],
    ids=["trace_id", "scorer", "extra_keys"],
)
def test_check_request_mismatches_a_wrong_identity_or_shape(
    campaign, campaign_corpus, tmp_path, edit
):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    document = _request(run, request_id)
    edit(document)
    _write_trace_request(copy, request_id, document)

    records = _records_of(run)
    state = _state_for(records, "c1")
    assert (
        check_request(
            document,
            state.scoring.started,
            campaign_corpus["c1"],
            state.scoring.started.payload["scorer"],
            records[0].payload["identity"]["candidate_digest"],
        )
        == "mismatch"
    )


def test_check_request_without_corpus_reports_unauthenticated_or_mismatch(
    campaign, campaign_corpus, tmp_path
):
    document, started, _case, scorer, candidate_digest = _scored_inputs(campaign, campaign_corpus)
    assert check_request(document, started, None, scorer, candidate_digest) == "unauthenticated"

    broken = json.loads(json.dumps(document))
    broken["input_sha256"] = "0" * 64
    assert check_request(broken, started, None, scorer, candidate_digest) == "mismatch"


# -- the try record (spec §3.2/§3.4) --------------------------------------------


def test_build_try_labels_every_part_of_a_scored_try(campaign, campaign_corpus):
    _work, run = campaign
    records = _records_of(run)
    state = _state_for(records, "c1")
    info = _info_for(run, CHAMPION)
    record = _build(run, records, info, state, campaign_corpus)

    assert record["record"] == "try"
    measurement = record["measurement"]
    assert measurement == {
        "id": CHAMPION,
        "role": "baseline",
        "round": info.round,
        "attempt_id": "champion",
        "candidate_digest": info.candidate_digest,
        "epoch": 0,
        "rebound_from": None,
    }
    assert "cohort_role" not in measurement
    assert record["case_id"] == "c1"
    assert record["sample_id"] == state.sample_id
    assert record["repeat"] == state.repeat
    assert record["retry_ordinal"] == 0
    assert record["repair"] is False
    assert record["imported"] is None

    request_id = state.scoring.started.payload["score_request_id"]
    document = _request(run, request_id)
    assert record["request"] == {
        "source": "scorer_trace",
        "check": "authenticated",
        "public": {"text": "hello", "case_id": "c1"},
    }
    turns = document["input"]["view"]["history"]["turns"]
    assert record["turns"] == turns
    assert turns and all(
        set(turn) == {"index", "message", "events", "usage", "latency_ms", "outcome", "reason"}
        for turn in turns
    )
    assert record["oracle"] == {"critical": True}

    sends = [
        record.payload
        for record in records
        if record.event == "target_send_started"
        and (record.payload["case_id"], record.payload["sample_id"]) == ("c1", state.sample_id)
    ]
    assert record["rollout"] == {
        "outcome": "completed",
        "class": "score",
        "owner": None,
        "error_code": None,
        "started": state.rollout.started,
        "finished": state.rollout.finished,
        "target_calls": len(sends),
    }

    output = record["scoring"]["output"]
    assert record["scoring"]["status"] == "scored"
    assert record["scoring"]["score_request_id"] == request_id
    assert record["scoring"]["trace"] == "present"
    assert record["scoring"]["metrics"] == {"score": 0.0}
    assert record["scoring"]["critical_pass"] is True
    assert output["check"] == "unbound"
    assert output["metrics"] == {"score": 0.0}
    assert output["verdicts"] == [{"criterion": "answered", "passed": False}]
    assert output["public_feedback"] is None
    assert output["details"] is None
    assert output["diagnostics"] is None


def test_a_mismatch_record_keeps_the_check_and_suppresses_the_content(
    campaign, campaign_corpus, tmp_path
):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    document = _request(run, request_id)
    document["input"]["oracle"]["critical"] = False
    _write_trace_request(copy, request_id, _rebound(document))

    records = _records_of(run)
    record = _build(
        copy, records, _info_for(run, CHAMPION), _state_for(records, "c1"), campaign_corpus
    )
    assert record["request"] == {"source": "scorer_trace", "check": "mismatch", "public": None}
    assert record["turns"] is None
    assert record["oracle"] is None
    # The scorer output is independent of the request binding and still labels.
    assert record["scoring"]["trace"] == "present"
    assert record["scoring"]["output"]["check"] == "unbound"


def test_build_try_without_a_trace_uses_the_corpus_and_journal_turns(
    campaign, campaign_corpus, tmp_path
):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    rewrite_production_trace(copy, request_id, remove=True)

    records = _records_of(run)
    state = _state_for(records, "c1")
    record = _build(copy, records, _info_for(run, CHAMPION), state, campaign_corpus)

    assert record["request"] == {
        "source": "corpus",
        "check": "corpus",
        "public": {"text": "hello", "case_id": "c1"},
    }
    assert record["oracle"] == {"critical": True}
    assert record["scoring"]["trace"] == "missing"
    assert record["scoring"]["status"] == "scored"
    assert record["scoring"]["metrics"] == {"score": 0.0}

    expected = [
        {
            "index": payload["turn"],
            "message": None,
            "events": None,
            "usage": payload.get("usage"),
            "latency_ms": payload.get("target_latency_ms"),
            "outcome": None,
            "reason": None,
        }
        for record_ in records
        if record_.event == "target_send_finished"
        for payload in [record_.payload]
        if (payload["case_id"], payload["sample_id"], payload.get("retry_ordinal", 0))
        == ("c1", state.sample_id, state.retry_ordinal)
    ]
    assert expected, "the genuine try must have finished turns to label"
    assert record["turns"] == expected


def test_build_try_without_corpus_or_trace_leaves_content_null_but_turns_journalled(
    campaign, campaign_corpus, tmp_path
):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    rewrite_production_trace(copy, request_id, remove=True)

    records = _records_of(run)
    state = _state_for(records, "c1")
    record = _build(copy, records, _info_for(run, CHAMPION), state, None)
    assert record["request"] is None
    assert record["oracle"] is None
    assert record["turns"], "journal turns need no manifest (ruling 1)"
    assert record["scoring"]["trace"] == "missing"


def test_build_turns_are_empty_only_when_no_turn_finished(campaign, campaign_corpus, tmp_path):
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    rewrite_production_trace(copy, request_id, remove=True)
    records = _records_of(run)
    finished = tuple(record for record in records if record.event != "target_send_finished")
    state = _state_for(finished, "c1")
    record = _build(copy, finished, _info_for(run, CHAMPION), state, campaign_corpus)
    assert record["turns"] == []


# -- scorer output (decision 8) ---------------------------------------------------


def _edited_stdout_record(campaign, campaign_corpus, tmp_path, stdout) -> dict:
    _work, run = campaign
    copy, request_id = _edited_trace_run(campaign, tmp_path)
    if stdout is None:
        # Keep the historical missing-file case; the other edited cases use
        # production packs. A pack output always owns both stream sections.
        trace = read_scorer_trace(copy, request_id)
        assert trace is not None
        rewrite_production_trace(copy, request_id, remove=True)
        write_directory_trace(
            copy / "scorer-traces",
            request_id,
            request=trace.request,
            stdout=None,
            stderr=trace.stderr,
        )
    else:
        rewrite_production_trace(copy, request_id, stdout=stdout.encode())
    records = _records_of(run)
    return _build(
        copy, records, _info_for(run, CHAMPION), _state_for(records, "c1"), campaign_corpus
    )


def test_scorer_output_is_unbound_for_edited_verdicts_or_feedback(
    campaign, campaign_corpus, tmp_path
):
    base = {"metrics": {"score": 0.0}, "critical_pass": True}
    verdicts = {**base, "verdicts": [{"criterion": "answered", "passed": True}]}
    record = _edited_stdout_record(campaign, campaign_corpus, tmp_path, json.dumps(verdicts))
    assert record["scoring"]["trace"] == "present"
    assert record["request"]["check"] == "authenticated"
    assert record["scoring"]["output"]["check"] == "unbound"
    assert record["scoring"]["output"]["verdicts"] == [{"criterion": "answered", "passed": True}]

    feedback = {**base, "public_feedback": {"note": "operator-facing"}}
    record = _edited_stdout_record(campaign, campaign_corpus, tmp_path, json.dumps(feedback))
    assert record["scoring"]["output"]["check"] == "unbound"
    assert record["scoring"]["output"]["public_feedback"] == {"note": "operator-facing"}


def test_scorer_output_labels_changed_metrics(campaign, campaign_corpus, tmp_path):
    record = _edited_stdout_record(
        campaign, campaign_corpus, tmp_path, json.dumps({"metrics": {"score": 0.5}})
    )
    assert record["scoring"]["output"]["check"] == "metrics_differ"
    assert record["scoring"]["output"]["metrics"] == {"score": 0.5}


@pytest.mark.parametrize("stdout", ["not json at all", "", '{"metrics": 1} trailing'])
def test_scorer_output_is_null_for_unparseable_stdout(campaign, campaign_corpus, tmp_path, stdout):
    record = _edited_stdout_record(campaign, campaign_corpus, tmp_path, stdout)
    assert record["scoring"]["trace"] == "present"
    assert record["scoring"]["output"] is None


def test_scorer_output_is_null_for_a_missing_stdout(campaign, campaign_corpus, tmp_path):
    record = _edited_stdout_record(campaign, campaign_corpus, tmp_path, None)
    assert record["scoring"]["trace"] == "present"
    assert record["scoring"]["output"] is None


# -- scoring status ---------------------------------------------------------------


def test_scoring_statuses_cover_the_terminal_vocabulary(campaign, campaign_corpus):
    _work, run = campaign
    records = _records_of(run)
    info = _info_for(run, CHAMPION)

    # not_started: the journal holds no scoring record for the try.
    unscored = tuple(record for record in records if not record.event.startswith("scoring_"))
    record = _build(run, unscored, info, _state_for(unscored, "c1"), campaign_corpus)
    assert record["scoring"]["status"] == "not_started"
    assert record["scoring"]["score_request_id"] is None
    assert record["scoring"]["trace"] == "not_applicable"
    assert record["scoring"]["metrics"] is None
    assert record["scoring"]["critical_pass"] is None
    assert record["scoring"]["output"] is None

    # unsettled: a scoring_started with no terminal, cut from the genuine journal.
    index = next(
        i
        for i, record in enumerate(records)
        if record.event == "scoring_started" and record.payload["case_id"] == "c1"
    )
    cut = records[: index + 1]
    record = _build(run, cut, info, _state_for(cut, "c1"), campaign_corpus)
    assert record["scoring"]["status"] == "unsettled"
    assert record["scoring"]["metrics"] is None
    assert record["scoring"]["trace"] == "present"
    assert record["scoring"]["output"]["check"] == "metrics_differ"

    # failed / interrupted: the terminal renamed, nothing else edited.
    def _renamed(terminal: str) -> tuple[Envelope, ...]:
        edited = []
        for record in records:
            if record.event == "scoring_finished":
                payload = record.payload
                replaced = {
                    "case_id": payload["case_id"],
                    "sample_id": payload["sample_id"],
                    "candidate_digest": payload["candidate_digest"],
                    "score_request_id": payload["score_request_id"],
                }
                if "retry_ordinal" in payload:
                    replaced["retry_ordinal"] = payload["retry_ordinal"]
                if terminal == "scoring_failed":
                    replaced["failure"] = "scorer_error"
                raw = record.to_payload()
                raw["event"] = terminal
                raw["payload"] = replaced
                edited.append(Envelope.from_payload(raw, family=S1_FAMILY))
            else:
                edited.append(record)
        return tuple(edited)

    for terminal, status in (("scoring_failed", "failed"), ("scoring_interrupted", "interrupted")):
        edited = _renamed(terminal)
        record = _build(run, edited, info, _state_for(edited, "c1"), campaign_corpus)
        assert record["scoring"]["status"] == status
        assert record["scoring"]["metrics"] is None
        assert record["scoring"]["trace"] == "present"


# -- carries (decision 9, implemented here; genuinely tested in Task 4) -----------


def _imported_state(campaign, campaign_corpus) -> tuple[tuple[Envelope, ...], TryState, TryState]:
    _work, run = campaign
    records = _records_of(run)
    source = _state_for(records, "c1")
    state = TryState(
        case_id="c1",
        sample_id=source.sample_id,
        repeat=source.repeat,
        retry_ordinal=None,
        repair=False,
        imported=ImportedProvenance(
            run_id="run elsewhere",
            measurement_id=CHAMPION,
            retry_ordinal=0,
            candidate_digest=records[0].payload["identity"]["candidate_digest"],
            metrics={"score": 1.0},
            critical_pass=True,
        ),
        start=None,
        terminal=None,
        rollout=TryRollout(outcome="completed", kind="score", owner=None),
        scoring=TryScoring(),
    )
    return records, source, state


def test_an_in_run_carry_takes_its_content_from_the_source_trace(campaign, campaign_corpus):
    _work, run = campaign
    records, source, state = _imported_state(campaign, campaign_corpus)
    carried = {(state.case_id, state.sample_id): (source.scoring.started, source.scoring.terminal)}
    record = _build(run, records, _info_for(run, CHAMPION), state, campaign_corpus, carried)

    assert record["imported"] == {
        "run_id": "run elsewhere",
        "measurement_id": CHAMPION,
        "retry_ordinal": 0,
        "candidate_digest": state.imported.candidate_digest,
    }
    assert record["rollout"] == {
        "outcome": "completed",
        "class": "score",
        "owner": None,
        "error_code": None,
        "started": None,
        "finished": None,
        "target_calls": None,
    }
    assert record["request"]["check"] == "authenticated"
    assert record["oracle"] == {"critical": True}
    assert record["turns"], "content comes from the source trace"
    assert record["scoring"]["status"] == "scored"
    assert (
        record["scoring"]["score_request_id"] == source.scoring.started.payload["score_request_id"]
    )
    assert record["scoring"]["trace"] == "present"
    assert record["scoring"]["metrics"] == {"score": 0.0}
    assert record["scoring"]["output"]["check"] == "unbound"


def test_a_carry_without_a_source_pair_gets_provenance_and_its_row_metrics(
    campaign, campaign_corpus
):
    """Both no-pair shapes (an in-run carry whose source offers nothing, and a
    cross-run carry): content stays null and `scoring.trace` "missing"
    (decision 9), but identity, metrics and provenance come from the row —
    the `sample_imported` payload is the journal's scoring evidence."""
    _work, run = campaign
    records, _source, state = _imported_state(campaign, campaign_corpus)
    record = _build(run, records, _info_for(run, CHAMPION), state, campaign_corpus, carried={})

    assert record["request"] is None
    assert record["turns"] is None
    assert record["oracle"] is None
    assert record["scoring"]["status"] == "scored"
    assert record["scoring"]["trace"] == "missing"
    assert record["scoring"]["metrics"] == {"score": 1.0}
    assert record["scoring"]["critical_pass"] is True
    assert record["scoring"]["output"] is None


# -- genuine carries on the family-12 completion (spec review Critical 1) ---------


@pytest.fixture(scope="module")
async def repeated_campaign(tmp_path_factory):
    work = tmp_path_factory.mktemp("repeated_campaign")
    run = await fakes.genuine_repeated_campaign_run(work)
    return work, run


# -- redaction and the field cap (decisions 11 and 12) ----------------------------

SECRET = "topsecret-credential"


def _secret_record() -> dict:
    return {
        "record": "try",
        "turns": [
            {
                "index": 0,
                "message": {"content": f"the agent answered with {SECRET} embedded"},
                "events": [],
            }
        ],
        "oracle": {"note": f"an oracle string naming {SECRET}"},
        "token": "abc",
        "nested": {f"a {SECRET} key": "clean"},
    }


def test_campaign_trace_secrets_keep_the_campaign_floor_membership_and_deduplication(campaign):
    from evolution.tree.run_trace import _campaign_trace_secrets

    work, _run = campaign
    workflow = load_manifest_v2(work / "manifest.json").workflow
    assert workflow is not None
    workflow = replace(
        workflow,
        scorer=replace(
            workflow.scorer,
            allowed_env=frozenset({"SCORER_FLOOR", "SCORER_SHORT", "DUPLICATE", "MISSING"}),
        ),
        editor=AgentCliEditorSection(
            model="codex", allowed_env=frozenset({"DUPLICATE", "PATH", "MISSING"})
        ),
        reflector=replace(
            workflow.reflector,
            role=ChatEditorSection(
                model="example",
                base_url="https://example.test/v1",
                api_key_env="REFLECTOR_KEY",
                proxy_env="REFLECTOR_PROXY",
            ),
        ),
    )
    secrets, unset = _campaign_trace_secrets(
        workflow,
        {
            "SCORER_FLOOR": "12345678",
            "SCORER_SHORT": "1234567",
            "DUPLICATE": "shared-credential",
            "OPENAI_API_KEY": "curated-credential",
            "PATH": "runtime-path-preserved",
            "UNRELATED": "unrelated-value-preserved",
            "REFLECTOR_KEY": "reflector-credential",
            "REFLECTOR_PROXY": "https://proxy.example.test",
        },
    )
    assert secrets == (
        "shared-credential",
        "12345678",
        "curated-credential",
        "reflector-credential",
        "https://proxy.example.test",
    )
    assert unset == 1


def test_campaign_trace_secrets_do_not_count_an_undeclared_key_as_unset(campaign):
    """#290: a chat role without `api_key_env` declares no secret variable, so
    the trace summary's unset count ignores it; its declared proxy still counts."""

    from evolution.tree.run_trace import _campaign_trace_secrets

    work, _run = campaign
    workflow = load_manifest_v2(work / "manifest.json").workflow
    assert workflow is not None
    workflow = replace(
        workflow,
        scorer=replace(workflow.scorer, allowed_env=frozenset()),
        editor=ChatEditorSection(
            model="example", base_url="https://example.test/v1", api_key_env=None
        ),
        reflector=replace(
            workflow.reflector,
            role=ChatEditorSection(
                model="example",
                base_url="https://example.test/v1",
                api_key_env=None,
                proxy_env="REFLECTOR_PROXY",
            ),
        ),
    )
    assert _campaign_trace_secrets(workflow, {"OPENAI_API_KEY": "curated-credential"}) == ((), 1)
    assert _campaign_trace_secrets(workflow, {"REFLECTOR_PROXY": "https://proxy.example.test"}) == (
        ("https://proxy.example.test",),
        0,
    )


def test_redaction_replaces_secret_occurrences_in_values_and_keys():
    record = redact_try(_secret_record(), (SECRET,))
    assert record["turns"][0]["message"]["content"] == "the agent answered with [REDACTED] embedded"
    assert record["oracle"] == {"note": "an oracle string naming [REDACTED]"}
    assert record["nested"] == {"a [REDACTED] key": "clean"}
    # A key *named* `token` is task data, not a credential shape: left alone.
    assert record["token"] == "abc"


def test_the_cap_truncates_long_strings_after_redaction():
    long_value = "prefix-" + SECRET + "-suffix-that-is-long"
    record = {"blob": long_value, "short": "ok"}
    record = redact_try(record, (SECRET,))
    redacted = "prefix-[REDACTED]-suffix-that-is-long"
    record = cap_fields(record, 16)
    assert record["blob"] == {
        "truncated": True,
        "bytes": len(redacted.encode()),
        "sha256": hashlib.sha256(redacted.encode()).hexdigest(),
        "head": redacted.encode()[:16].decode(),
    }
    assert record["short"] == "ok"


def test_the_cap_cuts_head_at_a_utf8_boundary():
    record = cap_fields({"text": "é" * 10}, 7)
    assert record["text"]["bytes"] == 20
    assert record["text"]["head"] == "é" * 3
    record["text"]["head"].encode()


def test_redaction_and_cap_do_not_touch_the_header():
    line = header(
        run_id="r1",
        kind="campaign",
        family=13,
        producing_version="0.3.0a34",
        journal_heads={"campaign.jsonl": 12},
        corpus="verified",
        redaction="campaign_secret_set",
        max_field_bytes=65536,
    )
    untouched = json.loads(json.dumps(line))
    assert redact_try(line, (SECRET,)) == untouched
    assert cap_fields(line, 65536) == untouched


# -- the header (decision 15) -----------------------------------------------------


def test_header_carries_the_schema_and_provenance_labels():
    line = header(
        run_id="r1",
        kind="evaluation",
        family=None,
        producing_version=None,
        journal_heads={"evaluation.jsonl": 4, "measurements/evaluation/events.jsonl": 2},
        corpus="absent",
        redaction="none",
        max_field_bytes=1024,
    )
    assert line == {
        "record": "header",
        "schema": "evolution-v2-trace/1",
        "run_id": "r1",
        "kind": "evaluation",
        "family": None,
        "producing_version": None,
        "generated_by": __version__,
        "journal_heads": {"evaluation.jsonl": 4, "measurements/evaluation/events.jsonl": 2},
        "corpus": "absent",
        "redaction": "none",
        "max_field_bytes": 1024,
    }


# -- evaluation_secrets (decision 14) ---------------------------------------------


def test_evaluation_secrets_matches_the_old_inline_expression(tmp_path):
    manifest = load_evaluation_manifest(evaluation_manifest(tmp_path))
    names = ("SHORT_NAME", "OK_NAME", "LONG_NAME", "UNSET_NAME")
    manifest = replace(manifest, scorer=replace(manifest.scorer, allowed_env=frozenset(names)))
    environ = {"SHORT_NAME": "abc", "OK_NAME": "abcd", "LONG_NAME": "x" * 12}

    expected = tuple(
        environ[name] for name in manifest.scorer.allowed_env if len(environ.get(name, "")) >= 4
    )
    assert evaluation_secrets(manifest, environ) == expected
    assert sorted(evaluation_secrets(manifest, environ)) == ["abcd", "x" * 12]


def test_evaluation_secrets_reads_the_process_environment_by_default(tmp_path, monkeypatch):
    manifest = load_evaluation_manifest(evaluation_manifest(tmp_path))
    manifest = replace(
        manifest, scorer=replace(manifest.scorer, allowed_env=frozenset({"OK_NAME"}))
    )
    monkeypatch.setenv("OK_NAME", "env-credential")
    assert evaluation_secrets(manifest) == ("env-credential",)
    assert evaluation_secrets(manifest, {}) == ()


# -- an evaluation run builds the same record shape --------------------------------


def test_an_evaluation_try_builds_with_the_split_corpus(evaluation):
    work, run = evaluation
    records = _records_of(run, "evaluation")
    (info,) = measurement_infos(open_run(run).snapshot)
    manifest = load_evaluation_manifest(work / "evaluation.json")
    corpus = {
        case.case_id: case for case in load_partition(manifest.corpus).select(manifest.split).cases
    }

    record = _build(run, records, info, _states(records)[0], corpus)
    assert record["measurement"] == {
        "id": "evaluation",
        "role": "evaluation",
        "round": None,
        "attempt_id": None,
        "candidate_digest": info.candidate_digest,
        "epoch": 0,
        "rebound_from": None,
    }
    assert record["request"]["check"] == "authenticated"
    assert record["scoring"]["status"] == "scored"
    assert record["scoring"]["trace"] == "present"
