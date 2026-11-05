"""Adaptive diagnosis admission and shared reader backend contracts."""

from __future__ import annotations

import pytest

from evolution.tree.roles.reflector_contracts import GroupError, validate_groups

pytestmark = pytest.mark.slow


def group(**changes):
    return {
        "root_cause": "The route omits a guard.",
        "suggested_fix": "Add the guard.",
        "counterevidence": "The passing case used the same route.",
        "regression_risks": "The guard may block valid input.",
        "why_this_layer": "The routing instructions own the decision.",
        "evidence_ids": ["obs:a:1"],
        "affected_cases": ["a"],
        "target_paths": ["SKILL.md"],
        **changes,
    }


def admit(groups, **changes):
    return validate_groups(
        groups,
        catalog_ids={"obs:a:1": "a", "obs:b:1": "b", "case:a": "a"},
        observation_ids={"obs:a:1": "a", "obs:b:1": "b"},
        failing_evidence_ids={"obs:a:1"},
        case_ids={"a", "b"},
        editable_paths=("*.md",),
        probe_seqs=None,
        needles=(),
        **changes,
    )


def test_adaptive_admission_keeps_passing_claims_ranks_and_deduplicates():
    passing = group(evidence_ids=["obs:b:1"], affected_cases=["b"])
    result = admit([passing, group(), group()])
    assert len(result) == 2
    assert result[0].affected_cases == ("a",)
    assert result[1].affected_cases == ("b",)
    assert result[0].to_payload()["counterevidence"] == group()["counterevidence"]


def test_case_metadata_cannot_back_an_adaptive_behavior_claim():
    with pytest.raises(GroupError, match="observation"):
        admit([group(evidence_ids=["case:a"])])


def test_adaptive_text_and_document_contract_rejects_entire_response():
    with pytest.raises(GroupError):
        admit([group(), group(counterevidence="x" * 513)])
    with pytest.raises(GroupError):
        admit([group(document_ids=["doc1"])])
    assert admit([group(document_ids=["doc1"])], document_ids={"doc1"})[0].to_payload()[
        "document_ids"
    ] == ["doc1"]


def test_adaptive_protocol_count_limits_are_not_silent_trimming():
    with pytest.raises(GroupError):
        admit([group(target_paths=[f"{index}.md" for index in range(17)])])


@pytest.mark.parametrize(
    ("field", "value", "count"),
    [("affected_cases", "a", 33), ("target_paths", "SKILL.md", 17), ("document_ids", "doc1", 17)],
)
def test_adaptive_array_limits_count_repeated_entries(field, value, count):
    with pytest.raises(GroupError, match="protocol count limit"):
        admit([group(**{field: [value] * count})], document_ids={"doc1"})


def test_duplicate_evidence_ids_are_counted_as_stored():
    """R4 (#282): the stored group keeps every listed id, so the limit counts them all.

    Counting distinct ids admitted 65 copies of one id, which the journal then
    refused at the append; the refusal now happens at admission.
    """
    from evolution.tree.roles.limits import adaptive_limits
    from evolution.tree.roles.reflector.chat_reflector import reflect_tools
    from evolution.tree.roles.reflector_contracts import adaptive_group_violation

    assert admit([group(evidence_ids=["obs:a:1"] * 64)])
    with pytest.raises(GroupError, match="evidence_ids exceeds the protocol count limit"):
        admit([group(evidence_ids=["obs:a:1"] * 65)])
    stored = {**group(evidence_ids=["obs:a:1"] * 65), "probe_refs": []}
    assert adaptive_group_violation([stored], adaptive_limits(512)) == (
        "evidence_ids exceeds the protocol count limit"
    )
    terminal = next(
        tool["function"]
        for tool in reflect_tools(probe=False, adaptive=True)
        if tool["function"]["name"] == "finish_reflection"
    )
    evidence_schema = terminal["parameters"]["properties"]["groups"]["items"]["properties"][
        "evidence_ids"
    ]
    assert "maxItems" not in evidence_schema


def adaptive_fixture(tmp_path, *, docs=False, pin=None, **brief_kw):
    from evolution.tree.roles.editor_contracts import BriefHistory
    from evolution.tree.roles.environment_docs import _v2_from_files
    from evolution.tree.roles.grouping.reflect_grouping import (
        GroupingResult,
        admit_grouping_result,
        cohorts_from_grouping,
        select_portion,
    )
    from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
    from tests.campaign_fakes import PRIMARY
    from tests.tree.execution.test_journal_identity import cases
    from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

    trace = _Trace(tmp_path, turns=1)
    inputs = FrozenReflectionInputs(
        pin=pin,
        scorer_diagnostics=pin is not None,
        model_grouping_family=pin is not None,
        baseline_binding=trace.baseline.digest,
        rows=(_reflect_row("s1", 1),),
        records=trace.journal.snapshot().records,
        run_root=trace.run_root,
        corpus=cases(),
        baseline=trace.baseline,
        environment_kind="static",
        metric_primary=PRIMARY,
        documents=_v2_from_files((("guide.md", b"DOC-BODY-CANARY"),)) if docs else None,
    )
    grouping = admit_grouping_result(
        inputs.registry, GroupingResult(inputs.registry.digest, (None,)), policy_identity="policy"
    )
    selection = select_portion(cohorts_from_grouping(grouping), registry=inputs.registry)
    from evolution.tree.roles.reflector import reflect_brief

    assert hasattr(reflect_brief, "build_adaptive_reflect_brief")
    if pin is not None:
        from tests.tree.roles.reflector.test_reflect_history_brief import _history

        brief_kw.setdefault("compact_history", _history())
    brief = reflect_brief.build_adaptive_reflect_brief(
        champion_digest=trace.baseline.digest,
        operator_context=None,
        history=BriefHistory(0, (), 0, ()),
        editable_paths=("*.md",),
        grouping=grouping,
        selection=selection,
        inputs=inputs,
        **brief_kw,
    )
    return trace, inputs, brief


def test_adaptive_brief_is_compact_body_free_and_binds_runtime_independently(tmp_path):
    import json

    from evolution.tree.roles.reflector.reflect_brief import (
        reflect_brief_digest,
        reflect_brief_payload,
        render_reflect_opening,
    )

    trace, inputs, brief = adaptive_fixture(tmp_path, docs=True)
    try:
        payload = reflect_brief_payload(brief)
        assert payload["version"] == "reflect_brief/2"
        assert payload["evidence_catalog"]["total"] == 1
        assert payload["documents"]["entries"][0]["path"] == "guide.md"
        assert "DOC-BODY-CANARY" not in json.dumps(payload)
        assert str(trace.run_root) not in render_reflect_opening(brief, (), ("*.md",))
        assert brief.observation_ids == {inputs.registry.records[0].evidence_id: "c"}
        assert len(reflect_brief_digest(brief)) == 64
    finally:
        trace.close()


@pytest.mark.parametrize("docs", [False, True])
def test_generated_helpers_share_live_reader_and_omit_absent_docs(tmp_path, docs):
    import json
    import subprocess

    from evolution.tree.roles import reflection_role_helpers

    trace, inputs, brief = adaptive_fixture(tmp_path, docs=docs)
    try:
        methods = {"list_evidence", "read_evidence"} | (
            {"list_documents", "read_document"} if docs else set()
        )
        with inputs.reader_view().open_operation("reflect-1", methods) as reader:
            control = reflection_role_helpers.install_helpers(tmp_path / "control", reader)
            assert set(control.descriptors) == methods
            result = subprocess.run(
                [*control.descriptors["list_evidence"], "{}"], capture_output=True, check=True
            )
            assert json.loads(result.stdout) == reader.dispatch("list_evidence", {})
            assert "cap_" not in json.dumps(dict(control.descriptors))
            if docs:
                catalog = reader.dispatch("list_documents", {})
                args = {"document_id": catalog["entries"][0]["document_id"]}
                result = subprocess.run(
                    [*control.descriptors["read_document"], json.dumps(args)],
                    capture_output=True,
                    check=True,
                )
                assert json.loads(result.stdout) == reader.dispatch("read_document", args)
            control.verify()
            assert not (tmp_path / "control" / "evidence").exists()
    finally:
        trace.close()


@pytest.mark.parametrize("docs", [False, True])
async def test_chat_adaptive_reader_uses_same_pages_and_docs_schema(tmp_path, docs):
    from evolution.tree.roles.reflector_contracts import ReflectRequest
    from tests.tree.roles.reflector.test_chat_reflector import _reflector, _response

    trace, inputs, brief = adaptive_fixture(tmp_path, docs=docs)
    try:
        methods = {"list_evidence", "read_evidence"} | (
            {"list_documents", "read_document"} if docs else set()
        )
        with inputs.reader_view().open_operation("chat-1", methods) as reader:
            request = ReflectRequest(
                "reflect-1",
                1,
                tmp_path,
                brief,
                inputs._documents,
                None,
                ("*.md",),
                30,
                reader=reader,
            )
            eid = next(iter(brief.observation_ids))
            responses = [
                _response("list_files"),
                _response("list_evidence"),
                _response("read_evidence", {"evidence_id": eid}),
            ]
            if docs:
                did = next(iter(brief.document_ids))
                responses.append(_response("read_document", {"document_id": did}))
            responses.append(
                _response(
                    "finish_reflection",
                    {"groups": [group(evidence_ids=[eid], affected_cases=["c"])]},
                )
            )
            reflector, client = _reflector(tmp_path, responses, max_turns=12)
            result = await reflector.reflect(request)
            assert result.status == "reflected", result.note
            tools = {
                tool["function"]["name"]: tool["function"] for tool in client.calls[-1]["tools"]
            }
            assert ("read_document" in tools) == docs
            required = tools["finish_reflection"]["parameters"]["properties"]["groups"]["items"][
                "required"
            ]
            assert "counterevidence" in required
            assert len(reader.summary.entries) >= 2
            from tests.tree.roles.reflector.test_chat_reflector import _log_lines

            usage = next(
                row
                for row in _log_lines(reflector, "reflect-1")
                if row.get("phase") == "evidence_usage"
            )
            assert usage["cited_ids"] == [eid]
            assert usage["read_ids"] == [eid]
            assert usage["selected_ids"] == []
    finally:
        trace.close()


def test_generated_grouping_helper_streams_one_session(tmp_path):
    import json
    import subprocess

    from evolution.tree.roles.reflection_role_helpers import install_helpers

    trace, inputs, brief = adaptive_fixture(tmp_path)
    try:
        with inputs.reader_view().open_operation(
            "grouping-1", {"stream_grouping_inputs", "list_grouping_inputs", "read_grouping_input"}
        ) as reader:
            helpers = install_helpers(tmp_path / "helpers", reader)
            assert "stream_grouping_inputs" in helpers.descriptors
            process = subprocess.run(
                [*helpers.descriptors["stream_grouping_inputs"], '{"sections":["identity"]}'],
                capture_output=True,
                check=True,
            )
            frames = [json.loads(line) for line in process.stdout.splitlines()]
            assert frames[0]["kind"] == "header"
            assert frames[-1]["kind"] == "final"
            assert any(frame.get("value", {}).get("section") == "identity" for frame in frames)
    finally:
        trace.close()


@pytest.mark.parametrize("backend", ["subprocess", "agent"])
async def test_external_adaptive_roles_read_docs_without_copying_bodies(tmp_path, backend):
    import json
    import subprocess

    from evolution.tree.roles.reflector_contracts import ReflectRequest

    trace, inputs, brief = adaptive_fixture(tmp_path, docs=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        with inputs.reader_view().open_operation(
            "external-1", {"list_evidence", "read_evidence", "list_documents", "read_document"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1",
                1,
                workspace,
                brief,
                inputs._documents,
                None,
                ("*.md",),
                30,
                reader=reader,
            )
            if backend == "subprocess":
                from tests.tree.roles.reflector.test_subprocess_reflector import _bind_reflector

                reflector = _bind_reflector(
                    tmp_path,
                    """import json, subprocess, sys
request = json.load(sys.stdin)
assert request["version"] == "reflect_request/2"
assert "environment_docs" not in request
assert "DOC-BODY-CANARY" not in json.dumps(request)
docid = request["evidence"]["documents"]["entries"][0]["document_id"]
result = subprocess.check_output(request["readers"]["read_document"] + [json.dumps({"document_id": docid})])
assert json.loads(result)["content"] == "DOC-BODY-CANARY"
print(json.dumps({"status":"declined","groups":[],"note":"read doc"}))""",
                )
            else:
                from tests.tree.roles.reflector.test_agent_reflector import _reflector

                def run(root):
                    control = root / ".evolution"
                    payload = json.loads((control / "REFLECT_REQUEST.json").read_text())
                    assert "DOC-BODY-CANARY" not in json.dumps(payload)
                    docid = payload["evidence"]["documents"]["entries"][0]["document_id"]
                    result = subprocess.check_output(
                        payload["readers"]["read_document"] + [json.dumps({"document_id": docid})]
                    )
                    assert json.loads(result)["content"] == "DOC-BODY-CANARY"
                    (control / "REFLECTION.json").write_text(
                        json.dumps({"status": "declined", "groups": [], "note": "read doc"})
                    )

                reflector, _ = _reflector(tmp_path, [run])
            result = await reflector.reflect(request)
            assert result.status == "declined", result.note
            assert not (workspace / ".evolution" / "evidence").exists()
            assert not (workspace / ".evolution" / "environment-docs").exists()
            assert len(reader.summary.entries) == 1
    finally:
        trace.close()


def test_the_byte_cap_is_measured_on_the_stored_form():
    """R4 (#282): `to_payload()` adds `"probe_refs": []` to every group, and the
    journal measures that form; admission measures the same bytes."""
    import json

    from evolution.tree.roles.limits import adaptive_limits
    from evolution.tree.roles.reflector_contracts import adaptive_group_violation

    def size(value):
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )

    groups = [group(target_paths=[f"{index}/" + "x" * 7000 + ".md"]) for index in range(8)]
    pad = 65_536 - 8 - size(groups)
    groups[0]["target_paths"] = [groups[0]["target_paths"][0][:-3] + "x" * pad + ".md"]
    stored = [{**item, "probe_refs": []} for item in groups]
    assert size(groups) == 65_536 - 8 and size(stored) > 65_536
    with pytest.raises(GroupError, match="groups exceeds 65536 bytes"):
        admit(groups)
    assert adaptive_group_violation(stored, adaptive_limits(512)) == "groups exceeds 65536 bytes"
    # Within the cap once stored, the same set is admitted.
    groups[0]["target_paths"] = [groups[0]["target_paths"][0][:-3][: -16 * 8] + ".md"]
    assert len(admit(groups)) == 8


def test_identical_groups_are_measured_once_they_merge():
    """PDR-0039 (#282): the byte cap is the stored record's, and identical groups
    merge before they are stored -- eight identical groups whose submitted bytes
    are within the cap (but whose eight stored copies would not be) are admitted
    as one group, as `/15` admitted them before the round limit existed."""
    import json

    def size(value):
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )

    item = group(target_paths=["x.md"])
    pad = (65_481 - 9) // 8 - size(item)
    item = group(target_paths=["x" * (pad + 1) + ".md"])
    groups = [dict(item) for _ in range(8)]
    assert size(groups) == 65_481
    assert size([{**entry, "probe_refs": []} for entry in groups]) > 65_536
    result = admit(groups)
    assert len(result) == 1
    assert size([result[0].to_payload()]) == size([{**item, "probe_refs": []}]) < 65_536


def test_adaptive_whole_array_byte_limit_and_new_text_leaks():
    with pytest.raises(GroupError, match="65536"):
        admit([group(target_paths=[str(index) + "x" * 5000 + ".md" for index in range(16)])])
    with pytest.raises(GroupError, match="oracle_leak"):
        validate_groups(
            [group(why_this_layer="SECRET-ORACLE is the answer")],
            catalog_ids={"obs:a:1": "a"},
            observation_ids={"obs:a:1": "a"},
            failing_evidence_ids=(),
            case_ids={"a"},
            editable_paths=("*.md",),
            probe_seqs=None,
            needles=("SECRET-ORACLE",),
        )


@pytest.mark.parametrize("backend", ["subprocess", "agent"])
async def test_external_roles_reject_helper_tampering_even_when_role_declines(tmp_path, backend):
    from evolution.tree.roles.reflector_contracts import ReflectRequest

    trace, inputs, brief = adaptive_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        with inputs.reader_view().open_operation(
            "tamper-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1", 1, workspace, brief, None, None, ("*.md",), 30, reader=reader
            )
            if backend == "subprocess":
                from tests.tree.roles.reflector.test_subprocess_reflector import _bind_reflector

                reflector = _bind_reflector(
                    tmp_path,
                    """import json, pathlib, sys
request=json.load(sys.stdin)
helper=pathlib.Path(request["readers"]["list_evidence"][-1])
helper.chmod(0o600)
helper.write_text("# corrupted")
print(json.dumps({"status":"declined","groups":[],"note":"done"}))""",
                )
            else:
                from tests.tree.roles.reflector.test_agent_reflector import _reflector

                def run(root):
                    helper = root / ".evolution" / "readers" / "list_evidence.py"
                    helper.chmod(0o600)
                    helper.write_text("# corrupted")
                    (root / ".evolution" / "REFLECTION.json").write_text(
                        '{"status":"declined","groups":[],"note":"done"}'
                    )

                reflector, _ = _reflector(tmp_path, [run])
            assert (await reflector.reflect(request)).status == "failed"
    finally:
        trace.close()


def mixed_repeat_fixture(tmp_path, monkeypatch, *, samples=5, unavailable_failing=False):
    import json
    from dataclasses import replace

    from evolution.tree.execution.interaction import EvalCase
    from evolution.tree.roles.editor.editor_evidence import TRACE_ROOT, TRACE_SUBDIR
    from evolution.tree.roles.editor_contracts import BriefHistory
    from evolution.tree.roles.grouping.reflect_grouping import (
        GroupingResult,
        admit_grouping_result,
        cohorts_from_grouping,
        select_portion,
    )
    from evolution.tree.roles.reflector.reflect_brief import build_adaptive_reflect_brief
    from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
    from tests.campaign_fakes import PRIMARY
    from tests.tree.execution.test_journal_identity import cases
    from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

    trace = _Trace(tmp_path, turns=1, samples=samples)
    rows = [_reflect_row(f"s{i}", i) for i in range(1, samples + 1)]
    for i, request_id in enumerate(trace.request_ids, 1):
        path = trace.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / "stdout"
        if samples == 5 and i >= 4:
            path.unlink()
            if unavailable_failing:
                rows[i - 1] = replace(rows[i - 1], critical_pass=False)
        elif i != 3 or samples != 5:
            path.write_text(
                json.dumps(
                    {
                        "metrics": {"accuracy": 1.0},
                        "verdicts": [{"criterion": f"criterion-{i}", "passed": False}],
                    }
                )
            )
    # The focus case is deliberately beyond the initial global catalog page.
    others = [EvalCase(f"a{i:02d}", "g", "s", (), {}, {}, {}) for i in range(40)]
    rows += [replace(_reflect_row("s1", 1), case_id=case.case_id) for case in others]
    inputs = FrozenReflectionInputs(
        baseline_binding=trace.baseline.digest,
        rows=rows,
        records=trace.journal.snapshot().records,
        run_root=trace.run_root,
        corpus=(*others, *cases()),
        baseline=trace.baseline,
        environment_kind="static",
        metric_primary=PRIMARY,
        families={"c": "family"},
    )

    def no_body(*args):
        raise AssertionError("summary eagerly projected an observation body")

    monkeypatch.setattr(inputs, "_observation_projection", no_body)
    assignments = tuple(0 if row.failing else None for row in inputs.registry.records)
    grouped = admit_grouping_result(
        inputs.registry,
        GroupingResult(
            inputs.registry.digest,
            assignments,
            variants={i: "declared" for i, slot in enumerate(assignments) if slot is not None},
        ),
        policy_identity="policy",
    )
    selection = select_portion(
        cohorts_from_grouping(grouped),
        registry=inputs.registry,
        representatives_per_case=1,
    )
    brief = build_adaptive_reflect_brief(
        champion_digest=trace.baseline.digest,
        operator_context=None,
        history=BriefHistory(0, (), 0, ()),
        editable_paths=("*.md",),
        grouping=grouped,
        selection=selection,
        inputs=inputs,
    )
    return trace, inputs, brief


@pytest.mark.parametrize("unavailable_failing", [False, True])
def test_focus_summary_counts_and_direct_handle_cover_all_five_repeats(
    tmp_path, monkeypatch, unavailable_failing
):
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload

    trace, inputs, brief = mixed_repeat_fixture(
        tmp_path, monkeypatch, unavailable_failing=unavailable_failing
    )
    try:
        payload = reflect_brief_payload(brief)
        [portion] = payload["portion"]
        assert portion["case_id"] == "c" and portion["case_id_omitted"] is False
        assert portion["case_handle"] not in {
            entry["case_handle"] for entry in payload["evidence_catalog"]["entries"]
        }
        assert portion["repeat_summary"] == {
            "case_total": 5,
            "case_available": 3,
            "case_passed": 1,
            "case_unknown": 2,
            "cohort_failing": 4 if unavailable_failing else 2,
        }
        summary = portion["variant_summary"]
        assert summary["distinct_outcomes"] == 1
        assert summary["distinct_declared_variants"] == 1
        assert summary["distinct_criterion_sets"] == 2
        assert summary["unknown_criterion_repeats"] == (2 if unavailable_failing else 0)
        assert summary["distinct_signatures"] == (3 if unavailable_failing else 2)
        assert sum(row["repeats"] for row in summary["entries"]) == portion["failure_count"]
        assert portion["omitted_exemplar_count"] == portion["failure_count"] - 1
        assert len(portion["unrepresented_variant_ids"]) == portion["omitted_variant_count"]
        page = inputs.list_evidence({"case_id": portion["case_handle"]})
        assert page["total"] == 5
        assert [entry["repeat"] for entry in page["entries"]] == [1, 2, 3, 4, 5]
        assert [entry["available"] for entry in page["entries"]] == [True] * 3 + [False] * 2
        family = inputs.list_evidence({"family_id": "family"})
        assert family["entries"][0]["case_id"] == "c"
        assert family["entries"][0]["passed"] == 1
    finally:
        trace.close()


def test_cohort_summary_bounds_variant_ids_and_preserves_exact_omissions(tmp_path, monkeypatch):
    import json

    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload

    trace, inputs, brief = mixed_repeat_fixture(tmp_path, monkeypatch, samples=20)
    try:
        [portion] = reflect_brief_payload(brief)["portion"]
        assert portion["repeat_summary"]["case_total"] == 20
        assert portion["variant_summary"]["distinct_signatures"] == 20
        assert len(portion["variant_summary"]["entries"]) == 8
        assert portion["variant_summary"]["omitted_entries"] == 12
        assert portion["omitted_exemplar_count"] == 19
        assert portion["omitted_variant_count"] == 19
        assert len(portion["unrepresented_variant_ids"]) == 8
        assert portion["omitted_unrepresented_variant_ids"] == 11
        assert len(json.dumps(portion).encode()) < 4096
        assert "criterion-" not in json.dumps(portion)
        assert inputs.list_evidence({"case_id": portion["case_handle"]})["total"] == 20
    finally:
        trace.close()


@pytest.mark.parametrize("backend", ["chat", "agent_cli", "subprocess"])
async def test_mixed_repeat_opening_and_direct_case_page_match_across_backends(
    tmp_path, monkeypatch, backend
):
    import json
    import subprocess

    from evolution.tree.roles.reflector.reflect_brief import (
        reflect_brief_payload,
        render_reflect_opening,
    )
    from evolution.tree.roles.reflector_contracts import ReflectRequest

    trace, inputs, brief = mixed_repeat_fixture(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        payload = reflect_brief_payload(brief)
        [portion] = payload["portion"]
        arguments = {"case_id": portion["case_handle"]}
        expected_page = inputs.list_evidence(arguments)
        with inputs.reader_view().open_operation(
            "mixed-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1",
                1,
                workspace,
                brief,
                None,
                None,
                ("*.md",),
                30,
                reader=reader,
            )
            if backend == "chat":
                from tests.tree.roles.reflector.test_chat_reflector import _reflector, _response

                reflector, client = _reflector(
                    tmp_path,
                    [
                        _response("list_files"),
                        _response("list_evidence", arguments),
                        _response("finish_reflection", {"groups": [], "declined": True}),
                    ],
                )
            elif backend == "agent_cli":
                from tests.tree.roles.reflector.test_agent_reflector import _reflector

                def run(root):
                    control = root / ".evolution"
                    document = json.loads((control / "REFLECT_REQUEST.json").read_text())
                    assert document["evidence"] == payload
                    actual = subprocess.check_output(
                        document["readers"]["list_evidence"] + [json.dumps(arguments)]
                    )
                    assert json.loads(actual) == expected_page
                    (control / "REFLECTION.json").write_text(
                        json.dumps({"status": "declined", "groups": [], "note": "done"})
                    )

                reflector, _ = _reflector(tmp_path, [run])
            else:
                from tests.tree.roles.reflector.test_subprocess_reflector import _bind_reflector

                reflector = _bind_reflector(
                    tmp_path,
                    f"""import json, subprocess, sys
request = json.load(sys.stdin)
assert request['evidence'] == {payload!r}
actual = subprocess.check_output(request['readers']['list_evidence'] + [json.dumps({arguments!r})])
assert json.loads(actual) == {expected_page!r}
print(json.dumps({{'status':'declined', 'groups':[], 'note':'done'}}))
""",
                )
            result = await reflector.reflect(request)
            assert result.status == "declined", result.note
            assert [row["operation"] for row in reader.summary.entries] == ["list_evidence"]
            if backend == "chat":
                messages = client.calls[-1]["messages"]
                assert render_reflect_opening(brief, (), ("*.md",)) in [
                    message.get("content") for message in messages
                ]
                assert any(
                    message.get("role") == "tool"
                    and json.loads(message["content"]) == expected_page
                    for message in messages
                    if message.get("role") == "tool"
                )
    finally:
        trace.close()


@pytest.mark.parametrize("bounds_repair", [True, False])
async def test_chat_adaptive_bounds_violation_is_repaired_and_logged(tmp_path, bounds_repair):
    """#180: a `finish_reflection` over a group bound asks for a repair (families 12+).

    The rejected arguments reach the private reflector log in every family, so
    an operator can see which bound failed; families before 12 keep failing
    the reflection `invalid_result` at once.
    """

    from evolution.tree.roles.reflector_contracts import ReflectRequest
    from tests.tree.roles.reflector.test_chat_reflector import _log_lines, _reflector, _response

    trace, inputs, brief = adaptive_fixture(tmp_path)
    try:
        with inputs.reader_view().open_operation(
            "chat-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1",
                1,
                tmp_path,
                brief,
                inputs._documents,
                None,
                ("*.md",),
                30,
                reader=reader,
            )
            eid = next(iter(brief.observation_ids))
            too_long = group(evidence_ids=[eid], affected_cases=["c"], root_cause="x" * 513)
            valid = group(evidence_ids=[eid], affected_cases=["c"])
            reflector, client = _reflector(
                tmp_path,
                [
                    _response("list_files"),
                    _response("finish_reflection", {"groups": [too_long]}),
                    _response("finish_reflection", {"groups": [valid]}),
                ],
                max_turns=12,
                bounds_repair=bounds_repair,
            )
            result = await reflector.reflect(request)
            lines = _log_lines(reflector, "reflect-1")
            rejected = [row for row in lines if row.get("phase") == "finish_reflection_rejected"]
            assert len(rejected) == 1
            assert rejected[0]["error"] == "root_cause exceeds 512 characters"
            assert rejected[0]["arguments"]["groups"][0]["root_cause"] == "x" * 513
            if bounds_repair:
                assert result.status == "reflected", result.note
                repair = next(
                    row for row in lines if row.get("phase") == "protocol_repair_requested"
                )
                assert "root_cause exceeds 512 characters" in repair["error"]
                assert len(client.calls) == 3
            else:
                assert (result.status, result.code) == ("failed", "invalid_result")
                assert len(client.calls) == 2
    finally:
        trace.close()


# -- #282: every layer holds the round's group text limit ------------------------------

#: The limits an adaptive brief published before the round limit existed
#: (`{**REFLECT_LIMITS_V1, **ADAPTIVE_GROUP_LIMITS}` at `8f7ed0407`).
V15_BRIEF_LIMITS = {
    "cohorts": 16,
    "groups": 8,
    "sentence_chars": 512,
    "verdicts": 32,
    "verdict_note_chars": 256,
    "history_rounds": 6,
    "environment_docs_chars": 60000,
    "turns": 8,
    "events_per_turn": 32,
    "text_chars": 4096,
    "input_chars": 2048,
    "feedback_keys": 16,
    "feedback_value_chars": 64,
    "evidence_ids": 64,
    "affected_cases": 32,
    "target_paths": 16,
    "document_ids": 16,
    "groups_bytes": 65536,
}


def test_admission_holds_the_round_limit_at_its_boundary():
    from evolution.tree.roles.reflector_contracts import check_adaptive_group_bounds

    assert admit([group(root_cause="x" * 4096)], group_text_chars=4096)
    with pytest.raises(GroupError, match="why_this_layer exceeds 4096 characters"):
        admit([group(why_this_layer="x" * 4097)], group_text_chars=4096)
    assert admit([group(suggested_fix="x" * 20_000)], group_text_chars=None)
    with pytest.raises(GroupError, match="root_cause exceeds 512 characters"):
        admit([group(root_cause="x" * 513)])
    check_adaptive_group_bounds([group(counterevidence="x" * 4096)], 4096)
    with pytest.raises(GroupError, match="counterevidence exceeds 4096 characters"):
        check_adaptive_group_bounds([group(counterevidence="x" * 4097)], 4096)
    with pytest.raises(GroupError, match="exceeds 512 characters"):
        check_adaptive_group_bounds([group(counterevidence="x" * 513)])


def test_an_absent_v15_limit_keeps_the_brief_bytes(tmp_path):
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload

    trace, _, default = adaptive_fixture(tmp_path / "default")
    other, _, explicit = adaptive_fixture(tmp_path / "explicit", group_text_chars=512)
    try:
        assert dict(default.payload["limits"]) == V15_BRIEF_LIMITS
        assert list(default.payload["limits"]) == list(V15_BRIEF_LIMITS)
        assert canonical_json(reflect_brief_payload(explicit)) == canonical_json(
            reflect_brief_payload(default)
        )
        assert default.group_text_chars == explicit.group_text_chars == 512
    finally:
        trace.close()
        other.close()


@pytest.mark.parametrize("limit", [4096, None])
def test_the_brief_publishes_the_round_limit(tmp_path, limit):
    from evolution.tree.roles.reflector.reflect_brief import (
        render_reflect_catalog,
        render_reflect_opening,
    )

    trace, _, brief = adaptive_fixture(tmp_path, group_text_chars=limit)
    try:
        limits = dict(brief.payload["limits"])
        expected = {
            key: value for key, value in V15_BRIEF_LIMITS.items() if key != "sentence_chars"
        }
        if limit is not None:
            expected["sentence_chars"] = limit
        # `None` omits the key: the legacy 512 of `REFLECT_LIMITS_V1` never shows through.
        assert limits == expected
        assert brief.group_text_chars == limit
        opening = render_reflect_opening(brief, render_reflect_catalog(brief), ("*.md",))
        assert ('"sentence_chars"' in opening) is (limit is not None)
        assert '"sentence_chars": 512' not in opening and '"sentence_chars":512' not in opening
    finally:
        trace.close()


def _long_group(brief, length):
    eid = next(iter(brief.observation_ids))
    return group(
        evidence_ids=[eid], affected_cases=[brief.observation_ids[eid]], root_cause="x" * length
    )


#: (round limit, `root_cause` length, admitted): the 4,096/4,097 boundary, no
#: per-field limit, and a 900-character field under the absent `/15` key.
LIMIT_CASES = [(4096, 4096, True), (4096, 4097, False), (None, 9000, True), (512, 900, False)]


@pytest.mark.parametrize(("limit", "length", "admitted"), LIMIT_CASES)
async def test_the_chat_backend_holds_the_round_limit(tmp_path, limit, length, admitted):
    from evolution.tree.roles.reflector_contracts import ReflectRequest
    from tests.tree.roles.reflector.test_chat_reflector import _log_lines, _reflector, _response

    trace, inputs, brief = adaptive_fixture(tmp_path, group_text_chars=limit)
    try:
        with inputs.reader_view().open_operation(
            "chat-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1", 1, tmp_path, brief, None, None, ("*.md",), 30, reader=reader
            )
            reflector, client = _reflector(
                tmp_path,
                [
                    _response("list_files"),
                    _response("finish_reflection", {"groups": [_long_group(brief, length)]}),
                    _response("finish_reflection", {"groups": [_long_group(brief, 8)]}),
                ],
                max_turns=12,
                bounds_repair=True,
            )
            result = await reflector.reflect(request)
            assert result.status == "reflected", result.note
            offered = _group_items(client.calls[-1]["tools"])["properties"]["root_cause"]
            assert offered.get("maxLength") == limit
            lines = _log_lines(reflector, "reflect-1")
            rejected = [row for row in lines if row.get("phase") == "finish_reflection_rejected"]
            if admitted:
                assert rejected == [] and len(client.calls) == 2
                assert len(result.groups[0]["root_cause"]) == length
            else:
                assert [row["error"] for row in rejected] == [
                    f"root_cause exceeds {limit} characters"
                ]
                assert len(client.calls) == 3 and len(result.groups[0]["root_cause"]) == 8
    finally:
        trace.close()


def _group_items(tools):
    terminal = next(t["function"] for t in tools if t["function"]["name"] == "finish_reflection")
    return terminal["parameters"]["properties"]["groups"]["items"]


@pytest.mark.parametrize(("limit", "length", "admitted"), LIMIT_CASES)
@pytest.mark.parametrize("backend", ["subprocess", "agent"])
async def test_external_backends_hold_the_round_limit(tmp_path, backend, limit, length, admitted):
    import json

    from evolution.tree.roles.reflector_contracts import ReflectRequest

    trace, inputs, brief = adaptive_fixture(tmp_path, group_text_chars=limit)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result_text = json.dumps(
        {"status": "reflected", "groups": [_long_group(brief, length)], "note": "n"}
    )
    try:
        with inputs.reader_view().open_operation(
            "external-1", {"list_evidence", "read_evidence"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1", 1, workspace, brief, None, None, ("*.md",), 30, reader=reader
            )
            if backend == "subprocess":
                from tests.tree.roles.reflector.test_subprocess_reflector import _bind_reflector

                reflector = _bind_reflector(tmp_path, f"print({result_text!r})")
            else:
                from tests.tree.roles.reflector.test_agent_reflector import _reflector

                def run(root):
                    (root / ".evolution" / "REFLECTION.json").write_text(result_text)

                reflector, _ = _reflector(tmp_path, [run])
            result = await reflector.reflect(request)
            if admitted:
                assert result.status == "reflected", result.note
                assert len(result.groups[0]["root_cause"]) == length
            else:
                assert (result.status, result.code) == ("failed", "invalid_result")
    finally:
        trace.close()


@pytest.mark.parametrize("backend", ["chat", "subprocess", "agent_cli"])
async def test_pinned_reflectors_read_raw_baseline_through_their_actual_reader(tmp_path, backend):
    import json
    import subprocess

    from evolution.tree.roles.reflector_contracts import ReflectRequest
    from tests.test_tree_evidence_projection import pin

    trace, inputs, brief = adaptive_fixture(tmp_path, pin=pin())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        with inputs.reader_view().open_operation(
            "raw-1", {"list_evidence", "read_evidence", "read_trace"}
        ) as reader:
            request = ReflectRequest(
                "reflect-1", 1, workspace, brief, None, None, ("*.md",), 30, reader=reader
            )
            arguments = {"evidence_id": "obs:c:1"}
            if backend == "chat":
                from tests.tree.roles.reflector.test_chat_reflector import _reflector, _response

                reflector, client = _reflector(
                    tmp_path,
                    [
                        _response("list_files"),
                        _response("read_trace", {**arguments, "offset": -1}),
                        _response("read_trace", arguments),
                        _response("finish_reflection", {"groups": [], "declined": True}),
                    ],
                )
            elif backend == "subprocess":
                from tests.tree.roles.reflector.test_subprocess_reflector import _bind_reflector

                reflector = _bind_reflector(
                    tmp_path,
                    """import json, subprocess, sys
request=json.load(sys.stdin)
assert request['evidence']['version'] == 'reflect_brief/6'
page=json.loads(subprocess.check_output(request['readers']['read_trace']+[json.dumps({'evidence_id':'obs:c:1'})]))
assert json.loads(page['content'])['history']['turns']
print(json.dumps({'status':'declined','groups':[],'note':'read trace'}))
""",
                )
            else:
                from tests.tree.roles.reflector.test_agent_reflector import _reflector

                def run(root):
                    control = root / ".evolution"
                    document = json.loads((control / "REFLECT_REQUEST.json").read_text())
                    page = json.loads(
                        subprocess.check_output(
                            document["readers"]["read_trace"] + [json.dumps(arguments)]
                        )
                    )
                    assert json.loads(page["content"])["history"]["turns"]
                    (control / "REFLECTION.json").write_text(
                        json.dumps({"status": "declined", "groups": [], "note": "read trace"})
                    )

                reflector, _ = _reflector(tmp_path, [run])
            result = await reflector.reflect(request)
            assert result.status == "declined", result.note
            assert any(entry["operation"] == "read_trace" for entry in reader.summary.entries)
            if backend == "chat":
                messages = client.calls[-1]["messages"]
                assert any("bad_offset" in str(message.get("content")) for message in messages)
                assert any(
                    "history" in str(message.get("content"))
                    for message in messages
                    if message.get("role") == "tool"
                )
    finally:
        trace.close()
