"""Contract tests for the lazy family-4 reflection input catalog."""

from __future__ import annotations

import json

import pytest

from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.roles.editor.editor_evidence import TRACE_ROOT, TRACE_SUBDIR
from evolution.tree.roles.environment_docs import _v2_from_files
from evolution.tree.roles.grouping.reflect_grouping import (
    FieldFeature,
    builtin_grouping,
    cohorts_from_grouping,
    select_portion,
)
from evolution.tree.roles.reflection_reader import (
    ByteLruCache,
    ReaderArgumentError,
    ReaderIntegrityError,
)
from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
from tests.campaign_fakes import PRIMARY
from tests.tree.execution.test_journal_identity import cases
from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace


def _inputs(
    trace: _Trace,
    *,
    rows=None,
    metadata=None,
    families=None,
    body_cache=None,
    scorer_diagnostics: bool = False,
    secrets: tuple[str, ...] = (),
    family15: bool = False,
    documents=None,
) -> FrozenReflectionInputs:
    return FrozenReflectionInputs(
        baseline_binding=trace.baseline.digest,
        rows=(_reflect_row("s1", 1),) if rows is None else rows,
        records=trace.journal.snapshot().records,
        run_root=trace.run_root,
        corpus=cases(),
        baseline=trace.baseline,
        environment_kind="static",
        metric_primary=PRIMARY,
        reflection_metadata=metadata or {"c": {"topic": "routing"}},
        families=families or {"c": "family-a"},
        body_cache=body_cache,
        scorer_diagnostics=scorer_diagnostics,
        secrets=secrets,
        family15=family15,
        documents=documents,
    )


def build_inputs(
    tmp_path, *, diagnostics: dict | None = None, scorer_diagnostics: bool = False
) -> FrozenReflectionInputs:
    """One `_Trace`'s inputs, its scored sample carrying `diagnostics` or not.

    Adapts the plan's `build_inputs(tmp_path, diagnostics=, scorer_diagnostics=)`
    to this module's actual fixtures (`_Trace`/`_inputs`, not `DeterministicScorer`
    -- this file never used that fixture).
    """

    trace = _Trace(tmp_path, turns=1, diagnostics=diagnostics)
    return _inputs(trace, scorer_diagnostics=scorer_diagnostics)


def first_id(inputs: FrozenReflectionInputs) -> str:
    return inputs.list_grouping_inputs({"offset": 0})["entries"][0]["evidence_id"]


def test_grouping_registry_is_lazy_and_exposes_only_safe_sections(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace)
        assert source.registry.records[0].failing is False
        handlers = source.handlers()
        page = handlers["list_grouping_inputs"]({"offset": 0})
        assert page["total"] == 1
        row = page["entries"][0]
        assert row["available"] is True and row["failing"] is False
        assert "case_id" not in row

        content = handlers["read_grouping_input"](
            {"index": 0, "section": "observation", "offset": 0}
        )
        body = json.loads(content["content"])
        assert body["terminal"] == "completed"
        assert body["failed_criteria"] == []
        assert "DETAILS-CANARY" not in content["content"]
        assert "oracle" not in content["content"]
    finally:
        trace.close()


def test_catalog_pages_cases_and_evidence_without_eager_repeat_bodies(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1, samples=2)
    try:
        source = _inputs(trace, rows=(_reflect_row("s1", 1), _reflect_row("s2", 2)))
        handlers = source.handlers()
        cases_page = handlers["list_evidence"]({"offset": 0})
        assert cases_page["total"] == 1
        handle = cases_page["entries"][0]["case_handle"]
        evidence_page = handlers["list_evidence"]({"case_id": handle, "offset": 0})
        assert evidence_page["total"] == 2
        evidence_id = evidence_page["entries"][0]["evidence_id"]
        first = handlers["read_evidence"]({"evidence_id": evidence_id, "offset": 0})
        assert first["total_characters"] > 0
        assert first["content_digest"]
        assert "source_path" not in json.dumps(first)
    finally:
        trace.close()


def test_default_grouping_and_representatives_use_authenticated_observation_headers(
    tmp_path, monkeypatch
) -> None:
    trace = _Trace(tmp_path, turns=1, samples=2)
    try:
        failed = {
            "metrics": {"accuracy": 1.0},
            "verdicts": [{"criterion": "checks_x", "passed": False}],
            "public_feedback": {"format": "ok"},
        }
        for request_id in trace.request_ids:
            path = trace.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / "stdout"
            path.write_text(json.dumps(failed), encoding="utf-8")
        source = _inputs(trace, rows=(_reflect_row("s1", 1), _reflect_row("s2", 2)))
        projections = 0
        original = source._observation_projection

        def counted(repeat):
            nonlocal projections
            projections += 1
            return original(repeat)

        monkeypatch.setattr(source, "_observation_projection", counted)
        grouped = builtin_grouping(source.registry, policy_identity="policy")
        selected = select_portion(
            cohorts_from_grouping(grouped), registry=source.registry, variants=grouped.variants
        )
        assert grouped.neighborhoods and selected.portions
        assert projections == 0

        answer_grouping = builtin_grouping(
            source.registry,
            (FieldFeature("answer", "observation", "/answer_text", "answers"),),
            policy_identity="policy-answer",
        )
        assert answer_grouping.neighborhoods
        assert projections == 2
        body = json.loads(
            source.read_grouping_input({"index": 0, "section": "observation"})["content"]
        )
        assert body["answer_text"]

        for name, path, body_field in (
            ("tool", "/tool_names", "tool_names"),
            ("root", "", "answer_text"),
        ):
            explicit = _inputs(trace, rows=(_reflect_row("s1", 1), _reflect_row("s2", 2)))
            calls = [0]

            def counted_explicit(original, counter):
                def counted(repeat):
                    counter[0] += 1
                    return original(repeat)

                return counted

            monkeypatch.setattr(
                explicit,
                "_observation_projection",
                counted_explicit(explicit._observation_projection, calls),
            )
            assert builtin_grouping(
                explicit.registry,
                (FieldFeature(name, "observation", path, name),),
                policy_identity=f"policy-{name}",
            ).neighborhoods
            assert calls == [2]
            observation = explicit.registry.records[0].sources["observation"]
            assert observation[body_field]
    finally:
        trace.close()


def test_known_unavailable_is_data_but_mutated_bound_trace_is_integrity_failure(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace)
        handlers = source.handlers()
        evidence_id = handlers["list_evidence"](
            {"case_id": handlers["list_evidence"]({})["entries"][0]["case_handle"]}
        )["entries"][0]["evidence_id"]
        with source.reader_view().open_operation("round-1", {"read_evidence"}) as reader:
            reader.dispatch("read_evidence", {"evidence_id": evidence_id})
            (trace.directory / "stdout").write_text('{"changed":true}', encoding="utf-8")
            with pytest.raises(ReaderIntegrityError, match="source_changed"):
                reader.dispatch("read_evidence", {"evidence_id": evidence_id})
            with pytest.raises(ReaderIntegrityError, match="source_changed"):
                reader.dispatch("read_evidence", {"evidence_id": evidence_id})
    finally:
        trace.close()


def test_preexisting_bad_trace_is_explicit_unavailable_data(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        trace.write_request(trace.input, trace_id="wrong")
        source = _inputs(trace)
        handlers = source.handlers()
        handle = handlers["list_evidence"]({})["entries"][0]["case_handle"]
        evidence_id = handlers["list_evidence"]({"case_id": handle})["entries"][0]["evidence_id"]
        read = handlers["read_evidence"]({"evidence_id": evidence_id})
        assert json.loads(read["content"])["observation"]["status"] == "unavailable"
    finally:
        trace.close()


def test_stream_frames_are_incremental_and_family_pages_use_opaque_handles(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace)
        handlers = source.handlers()
        frames = list(
            handlers["stream_grouping_inputs"](
                {"start": 0, "stop": 1, "sections": ["identity", "metadata"]}
            )
        )
        assert frames[0]["kind"] == "header" and frames[-1]["kind"] == "final"
        assert [frame["section"] for frame in frames[1:-1]] == ["identity", "metadata"]
        family = handlers["list_evidence"]({"family_id": "family-a", "offset": 0})
        assert family["total"] == 1
        assert family["entries"][0]["relation"] == "family_member"
    finally:
        trace.close()


def test_feedback_is_a_pointer_ready_mapping_and_docs_use_v2_pages(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace)
        docs = _v2_from_files((("guide.md", b"read this"),))
        documented = FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=(_reflect_row("s1", 1),),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
            documents=docs,
        )
        feedback = source.handlers()["read_grouping_input"]({"index": 0, "section": "feedback"})
        assert json.loads(feedback["content"])["format"] == "ok"
        handlers = documented.handlers()
        catalog = handlers["list_documents"]({})
        assert catalog["entries"][0]["path"] == "guide.md"
        page = handlers["read_document"]({"document_id": catalog["entries"][0]["document_id"]})
        assert page["content"] == "read this"
    finally:
        trace.close()


def test_started_baseline_registry_has_the_protocol_capacity_limit(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        rows = tuple(
            SampleRow(
                case_id="c",
                sample_id=f"unscored-{repeat}",
                repeat=repeat,
                requested_seed=None,
                effective_seed=None,
                boot_id="boot",
                dependency_digest="dep",
                outcome=None,
                disposition="unscored",
                metrics=None,
                critical_pass=None,
            )
            for repeat in range(1, 32_770)
        )
        with pytest.raises(ValueError, match="reflection_capacity"):
            _inputs(trace, rows=rows)
    finally:
        trace.close()


def test_unavailable_header_stays_unavailable_when_a_missing_source_returns(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        stdout = trace.directory / "stdout"
        saved = stdout.read_text(encoding="utf-8")
        stdout.unlink()
        source = _inputs(trace)
        handle = source.list_evidence({})["entries"][0]["case_handle"]
        evidence_id = source.list_evidence({"case_id": handle})["entries"][0]["evidence_id"]
        body = json.loads(source.read_evidence({"evidence_id": evidence_id})["content"])
        assert body["observation"]["status"] == "unavailable"
        stdout.write_text(saved, encoding="utf-8")
        with pytest.raises(ReaderIntegrityError, match="source_changed"):
            source.verify_admission()
    finally:
        trace.close()


def test_admission_rechecks_a_source_mutated_after_its_last_read(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace)
        handle = source.list_evidence({})["entries"][0]["case_handle"]
        evidence_id = source.list_evidence({"case_id": handle})["entries"][0]["evidence_id"]
        source.read_evidence({"evidence_id": evidence_id})
        (trace.directory / "stdout").write_text('{"changed": true}', encoding="utf-8")
        with pytest.raises(ReaderIntegrityError, match="source_changed"):
            source.verify_admission()
    finally:
        trace.close()


def test_feedback_markers_are_separate_from_pointer_values_in_pages_and_streams(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1, feedback={"route": "x" * 5_000})
    try:
        source = _inputs(trace)
        response = source.read_grouping_input({"index": 0, "section": "feedback"})
        assert json.loads(response["content"])["route"].startswith("x")
        assert response["feedback_omitted"] == 0
        assert response["feedback_truncated"] == ["route"]
        frame = next(
            frame
            for frame in source.stream_grouping_inputs(
                {"start": 0, "stop": 1, "sections": ["feedback"]}
            )
            if frame["kind"] == "data"
        )
        assert frame["feedback_truncated"] == ["route"]
        handle = source.list_evidence({})["entries"][0]["case_handle"]
        evidence_id = source.list_evidence({"case_id": handle})["entries"][0]["evidence_id"]
        body = json.loads(source.read_evidence({"evidence_id": evidence_id})["content"])
        assert body["feedback_truncated"] == ["route"]
    finally:
        trace.close()


@pytest.mark.parametrize("capacity", [0, 1])
def test_feedback_markers_do_not_require_cache_retention(tmp_path, capacity) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace, body_cache=ByteLruCache(capacity))
        page = source.read_grouping_input({"index": 0, "section": "feedback"})
        assert json.loads(page["content"]) == {"format": "ok"}
        assert page["feedback_omitted"] == 0
        assert page["feedback_truncated"] == []
    finally:
        trace.close()


def test_registry_uses_the_existing_bounded_observation_id_for_long_case_ids(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        case_id = "case-" + "🙂" * 200
        row = SampleRow(
            case_id=case_id,
            sample_id="unscored",
            repeat=1,
            requested_seed=None,
            effective_seed=None,
            boot_id="boot",
            dependency_digest="dep",
            outcome=None,
            disposition="unscored",
            metrics=None,
            critical_pass=None,
        )
        source = FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=(row,),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=(EvalCase(case_id, "g", "s", (), {}, {}, {}),),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
        (record,) = source.registry.records
        assert record.evidence_id.startswith("obs:") and len(record.evidence_id) <= 128
        entry = source.list_evidence({})["entries"][0]
        assert entry["case_id_omitted"] is True
        assert entry["case_id"] == entry["case_handle"]
        assert case_id not in entry["case_handle"]
        observations = source.list_evidence({"case_id": entry["case_handle"]})
        assert observations["entries"][0]["evidence_id"] == record.evidence_id
        identity = source.read_grouping_input({"index": 0, "section": "identity"})
        assert json.loads(identity["content"])["case_id"] == case_id
    finally:
        trace.close()


def test_the_observation_projection_withholds_the_editor_only_turn_reason(tmp_path) -> None:
    """PDR-0008: `read_evidence` is the adaptive Reflector's evidence registry,
    so the W19 turn `reason` -- editor-facing evidence -- stops here too."""

    from evolution.tree.roles.editor_contracts import Observation, ObservationInput, ObservationTurn
    from evolution.tree.roles.reflector.reflect_evidence import RepeatEvidence

    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace)
        observation = Observation(
            "present",
            turns=(
                ObservationTurn(
                    0,
                    "candidate_failed",
                    ObservationInput("present", "q", False),
                    ({"type": "assistant_message", "content": "x"},),
                    0,
                    False,
                    {"category": "format", "code": "REASON-CANARY"},
                ),
            ),
            feedback={"format": "ok"},
            feedback_status="present",
        )
        repeat = RepeatEvidence(
            "c", 1, "s1", "completed", None, 0.0, "present", (), {}, 0, (), observation, 1, ()
        )

        projected = source._observation_projection(repeat)

        assert "REASON-CANARY" not in json.dumps(projected)
        assert projected["behavior"]["turns"][0]["outcome"] == "candidate_failed"
        assert "reason" not in projected["behavior"]["turns"][0]
    finally:
        trace.close()


def test_diagnostics_section_is_served_only_to_family_eight(tmp_path) -> None:
    inputs_v7 = build_inputs(
        tmp_path / "a", diagnostics={"route": "handoff"}, scorer_diagnostics=False
    )
    with pytest.raises(ReaderArgumentError, match="unknown_section"):
        inputs_v7.read_grouping_input({"index": 0, "section": "diagnostics"})
    inputs_v8 = build_inputs(
        tmp_path / "b", diagnostics={"route": "handoff"}, scorer_diagnostics=True
    )
    page = inputs_v8.read_grouping_input({"index": 0, "section": "diagnostics"})
    assert json.loads(page["content"]) == {"route": "handoff"}


def test_read_evidence_carries_diagnostics_whole_and_null_when_absent(tmp_path) -> None:
    with_value = build_inputs(
        tmp_path / "a", diagnostics={"route": "handoff"}, scorer_diagnostics=True
    )
    body = json.loads(with_value.read_evidence({"evidence_id": first_id(with_value)})["content"])
    assert body["diagnostics"] == {"route": "handoff"}
    without = build_inputs(tmp_path / "b", diagnostics=None, scorer_diagnostics=True)
    body = json.loads(without.read_evidence({"evidence_id": first_id(without)})["content"])
    assert body["diagnostics"] is None


def test_family_seven_read_evidence_is_byte_identical(tmp_path) -> None:
    inputs = build_inputs(tmp_path, diagnostics=None, scorer_diagnostics=False)
    body = json.loads(inputs.read_evidence({"evidence_id": first_id(inputs)})["content"])
    assert set(body) == {
        "identity",
        "metadata",
        "public",
        "observation",
        "feedback",
        "feedback_omitted",
        "feedback_truncated",
    }


def test_a_family_seven_trace_with_diagnostics_is_unavailable(tmp_path) -> None:
    inputs = build_inputs(tmp_path, diagnostics={"route": "x"}, scorer_diagnostics=False)
    assert inputs.registry.records[0].available is False


# -- published criterion ids ------------


def _scored(trace: _Trace, request_id: str, stdout: dict) -> None:
    path = trace.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / "stdout"
    path.write_text(json.dumps(stdout), encoding="utf-8")


def test_published_criteria_hold_every_verdict_of_authenticated_rows_only(tmp_path) -> None:
    """Passed and failed verdict criteria of authenticated rows; never a verdict
    note, a `diagnostics` value or a criterion of an unauthenticated row."""

    trace = _Trace(tmp_path, turns=1, samples=3)
    try:
        # s1: unauthenticated (its request names another trace id).
        _scored(
            trace,
            trace.request_ids[0],
            {
                "metrics": {"accuracy": 0.0},
                "verdicts": [{"criterion": "hidden-check", "passed": False}],
            },
        )
        trace.write_request(trace.input, trace_id="wrong")
        # s2: a failed verdict with a note, a passed verdict, and diagnostics.
        _scored(
            trace,
            trace.request_ids[1],
            {
                "metrics": {"accuracy": 0.0},
                "verdicts": [
                    {"criterion": "exact-answer-check", "passed": False, "note": "note-text-value"},
                    {"criterion": "passed-check", "passed": True},
                ],
                "diagnostics": {"criterion": "diagnostic-check", "hint": "diagnostic-only"},
            },
        )
        # s3: only a passed verdict.
        _scored(
            trace,
            trace.request_ids[2],
            {
                "metrics": {"accuracy": 1.0},
                "verdicts": [{"criterion": "later-check", "passed": True}],
            },
        )
        source = _inputs(
            trace,
            rows=(_reflect_row("s1", 1), _reflect_row("s2", 2), _reflect_row("s3", 3)),
            scorer_diagnostics=True,
        )
        assert [record.available for record in source.registry.records] == [False, True, True]
        assert source.published_criteria == frozenset(
            {"exact-answer-check", "passed-check", "later-check"}
        )
        assert isinstance(source.published_criteria, frozenset)
    finally:
        trace.close()


def test_published_criteria_are_empty_without_verdicts(tmp_path) -> None:
    inputs = build_inputs(
        tmp_path, diagnostics={"criterion": "x-check-value"}, scorer_diagnostics=True
    )
    assert inputs.published_criteria == frozenset()


# -- a scorer's run secret never reaches the Reflector or the Grouper -------

_SECRET = "scorer-token-7f3a9c2e51"
_CLEAN = {
    "metrics": {"accuracy": 0.0},
    "verdicts": [{"criterion": "checks_x", "passed": False, "note": "missed x"}],
    "public_feedback": {"judge": "the answer missed x"},
    "diagnostics": {"route": "handoff"},
}


@pytest.mark.parametrize(
    "stdout",
    [
        {**_CLEAN, "public_feedback": {"judge": f"auth failed for {_SECRET}"}},
        {**_CLEAN, "diagnostics": {"route": "handoff", _SECRET: _SECRET}},
        {
            **_CLEAN,
            "verdicts": [{"criterion": "checks_x", "passed": False, "note": f"key {_SECRET}"}],
        },
    ],
    ids=["public_feedback", "diagnostics", "verdict_note"],
)
def test_a_run_secret_in_scorer_output_is_redacted_for_the_roles(tmp_path, stdout) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        _scored(trace, trace.request_ids[0], stdout)
        inputs = _inputs(trace, scorer_diagnostics=True, secrets=(_SECRET,))
        assert inputs.registry.records[0].available is True
        assert inputs.registry.records[0].failing is True
        evidence = inputs.read_evidence({"evidence_id": first_id(inputs)})["content"]
        served = [
            evidence,
            *(
                inputs.read_grouping_input({"index": 0, "section": section})["content"]
                for section in ("observation", "feedback", "diagnostics")
            ),
        ]
        for content in served:
            assert _SECRET not in content
        assert "[REDACTED]" in "".join(served)
    finally:
        trace.close()


def test_a_numeric_run_secret_in_scorer_output_is_redacted_for_the_roles(tmp_path) -> None:
    numeric = "12345678"
    trace = _Trace(tmp_path, turns=1)
    try:
        _scored(
            trace,
            trace.request_ids[0],
            {
                **_CLEAN,
                "public_feedback": {"pin": 12345678},
                "diagnostics": {"route": "handoff", "pins": [12345678, 1]},
            },
        )
        inputs = _inputs(trace, scorer_diagnostics=True, secrets=(numeric,))
        assert inputs.registry.records[0].available is True
        feedback = inputs.read_grouping_input({"index": 0, "section": "feedback"})
        diagnostics = inputs.read_grouping_input({"index": 0, "section": "diagnostics"})
        assert json.loads(feedback["content"]) == {"pin": "[REDACTED]"}
        assert json.loads(diagnostics["content"]) == {"route": "handoff", "pins": ["[REDACTED]", 1]}
    finally:
        trace.close()


def test_a_run_secret_equal_to_a_verdict_key_keeps_the_verdict_schema(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        _scored(trace, trace.request_ids[0], _CLEAN)
        # "criterion" is nine characters, so it can be a forwarded value.
        inputs = _inputs(trace, scorer_diagnostics=True, secrets=("criterion",))
        assert inputs.registry.records[0].available is True
        page = inputs.read_grouping_input({"index": 0, "section": "feedback"})
        assert json.loads(page["content"]) == {"judge": "the answer missed x"}
    finally:
        trace.close()


def test_a_run_secret_in_a_criterion_id_makes_the_output_unavailable(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        _scored(
            trace,
            trace.request_ids[0],
            {**_CLEAN, "verdicts": [{"criterion": f"c-{_SECRET}", "passed": False}]},
        )
        inputs = _inputs(trace, scorer_diagnostics=True, secrets=(_SECRET,))
        assert inputs.registry.records[0].available is False
        assert inputs.published_criteria == frozenset()
        content = inputs.read_evidence({"evidence_id": first_id(inputs)})["content"]
        assert _SECRET not in content
    finally:
        trace.close()


def test_a_scorer_output_without_a_run_secret_stays_available(tmp_path) -> None:
    trace = _Trace(tmp_path, turns=1)
    try:
        _scored(trace, trace.request_ids[0], _CLEAN)
        inputs = _inputs(trace, scorer_diagnostics=True, secrets=(_SECRET,))
        assert inputs.registry.records[0].available is True
        page = inputs.read_grouping_input({"index": 0, "section": "feedback"})
        assert json.loads(page["content"]) == {"judge": "the answer missed x"}
    finally:
        trace.close()


@pytest.mark.parametrize("family15", [False, True])
def test_list_evidence_case_id_filter_is_family_gated(tmp_path, family15):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace, family15=family15)
        entry = source.list_evidence({})["entries"][0]
        by_handle = source.list_evidence({"case_id": entry["case_handle"]})
        if family15:
            assert source.list_evidence({"case_id": entry["case_id"]}) == by_handle
        else:
            with pytest.raises(ReaderArgumentError, match="^unknown_case$"):
                source.list_evidence({"case_id": entry["case_id"]})
    finally:
        trace.close()


@pytest.mark.parametrize("value", ["missing", "x" * 300, 7, ["missing"]])
def test_unknown_case_detail_names_value_and_expected_forms(tmp_path, value):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace, family15=True)
        with pytest.raises(ReaderArgumentError) as caught:
            source.list_evidence({"case_id": value})
        error = caught.value
        assert str(error) == error.code == "unknown_case"
        expected = json.dumps(value, ensure_ascii=False)[:128]
        assert (
            error.detail
            == f"{expected} is neither a case_handle nor a case_id; use a case_handle or case_id from a list_evidence catalog entry"
        )
    finally:
        trace.close()


@pytest.mark.parametrize(
    "method,kind",
    [
        ("list_evidence", "catalog"),
        ("list_evidence", "family"),
        ("list_evidence", "case"),
        ("read_evidence", "body"),
        ("list_documents", "documents"),
        ("read_document", "document"),
    ],
)
@pytest.mark.parametrize("offset", [-1, "bad", 10**9])
def test_invalid_offset_detail_names_range(tmp_path, method, kind, offset):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(
            trace, family15=True, documents=_v2_from_files((("guide.md", b"read this"),))
        )
        catalog = source.list_evidence({})
        entry = catalog["entries"][0]
        args = {}
        if kind == "family":
            args["family_id"] = next(iter(source._family_case_ids))
        elif kind == "case":
            args["case_id"] = entry["case_handle"]
        elif kind == "body":
            args["evidence_id"] = first_id(source)
        elif kind == "document":
            args["document_id"] = source.list_documents({})["entries"][0]["document_id"]
        page = getattr(source, method)(args)
        total = page.get("total", page.get("total_characters"))
        with pytest.raises(ReaderArgumentError) as caught:
            getattr(source, method)({**args, "offset": offset})
        assert str(caught.value) == caught.value.code == "invalid_offset"
        assert caught.value.detail == f"offset must be between 0 and {total}"
    finally:
        trace.close()


def test_reader_codes_and_precedence_unchanged(tmp_path):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = _inputs(trace, family15=True)
        with pytest.raises(ReaderArgumentError) as caught:
            source.list_evidence({"family_id": "unknown", "offset": -1})
        assert str(caught.value) == "invalid_offset"
        assert caught.value.detail == "offset must be a non-negative integer"
    finally:
        trace.close()


def test_handle_wins_over_equal_case_id(tmp_path):
    import hashlib

    trace = _Trace(tmp_path, turns=1)
    try:
        first = "first"
        handle = (
            "case_"
            + hashlib.sha256((trace.baseline.digest + "\0" + first).encode()).hexdigest()[:24]
        )
        rows = [
            SampleRow(
                case_id=case,
                sample_id=f"unscored-{i}",
                repeat=1,
                requested_seed=None,
                effective_seed=None,
                boot_id="boot",
                dependency_digest="dep",
                outcome=None,
                disposition="unscored",
                metrics=None,
                critical_pass=None,
            )
            for i, case in enumerate((first, handle))
        ]
        source = FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=rows,
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=[EvalCase(case, "g", "s", (), {}, {}, {}) for case in (first, handle)],
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
            family15=True,
        )
        assert source.list_evidence({"case_id": handle}) == source.list_evidence({"case_id": first})
        second_handle = source.case_identity_summary(handle)["case_handle"]
        assert source.list_evidence({"case_id": handle}) != source.list_evidence(
            {"case_id": second_handle}
        )
    finally:
        trace.close()
