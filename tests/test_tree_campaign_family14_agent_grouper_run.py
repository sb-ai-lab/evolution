"""H9 genuine agent CLI campaigns, through the pre-public H12 report seam."""

from __future__ import annotations

import json

import pytest

from tests.agent_grouping_fakes import sealed_grouping_cli
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_grouper_run import _grouper_digest, _spy_refusals
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


async def test_real_agent_groups_completes_and_reuses_durable_grouping(tmp_path, monkeypatch):
    refusals = _spy_refusals(monkeypatch)
    section = sealed_grouping_cli(tmp_path)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    assert refusals == []
    (started,) = payloads(run, "grouping_started")
    assert started["grouper_kind"] == "agent_cli"
    assert started["policy_digest"] == _grouper_digest(run)
    (finished,) = payloads(run, "grouping_finished")
    assert finished["calls"] == {"agent_calls": 1, "repairs": 0, "turns": 1}
    assert finished["fallback"] is None
    assert any(a is not None for a in finished["result"]["assignments"])
    assert snapshot.finished["outcome"] == "completed"
    assert len(payloads(run, "round_closed")) == 1
    assert not payloads(run, "grouping_failed")
    assert audit(run)["mismatches"] == []
    invocations = (tmp_path / "grouper-invocations.jsonl").read_text().splitlines()
    assert len(invocations) == 1
    assert set(json.loads(invocations[0])["request"]["helpers"]) == {
        "list_grouping_inputs",
        "read_grouping_input",
        "stream_grouping_inputs",
    }
    assert list((run / ".grouping").iterdir()) == []
    await family14_run(tmp_path, monkeypatch, family14_settings={"grouper": section})
    assert (tmp_path / "grouper-invocations.jsonl").read_text().splitlines() == invocations
    assert len(payloads(run, "grouping_finished")) == 1


async def test_split_corpus_private_and_test_values_stay_out_of_agent_repair_plane(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.corpus_partition import load_partition
    from tests import campaign_drivers as fixtures
    from tests.agent_grouping_fakes import GROUP_ALL_AGENT_BODY
    from tests.campaign_drivers import _records

    train_private = "train-private-task7-384021"
    test_value = "held-out-task7-429133"
    workspace = fixtures.write_workspace

    def split_workspace(*args, **kwargs):
        rows = [dict(row) for row in kwargs["rows"]]
        rows[0]["split"] = "train"
        rows[0]["private"] = {"secret": train_private}
        rows[1]["split"] = "test"
        rows[1]["group_id"] = "held-out-group"
        rows[1]["oracle"] = {**rows[1]["oracle"], "secret": test_value, "check": test_value}
        rows[1]["public"] = {**rows[1]["public"], "text": test_value}
        rows[1]["private"] = {"secret": test_value}
        kwargs["rows"] = rows
        return workspace(*args, **kwargs)

    monkeypatch.setattr(fixtures, "write_workspace", split_workspace)
    published = []
    real_needles = _Campaign._reflection_needles_for

    def capture_published_train_criteria(self, inputs):
        published.append((inputs.baseline_binding, inputs.published_criteria))
        return real_needles(self, inputs)

    monkeypatch.setattr(_Campaign, "_reflection_needles_for", capture_published_train_criteria)
    plane = tmp_path / "agent-plane.jsonl"
    sections = ("identity", "metadata", "public", "observation", "feedback", "diagnostics")
    collect = (
        "repair = (work / '.evolution/VALIDATION.json').exists()\n"
        "page = helper('list_grouping_inputs', {'offset': 0})\n"
        f"sections = {sections!r}\n"
        "read = [helper('read_grouping_input', {'index': entry['index'], 'section': section}) "
        "for entry in page['entries'] for section in sections]\n"
        "stream = subprocess.check_output([*request['helpers']['stream_grouping_inputs'], "
        "json.dumps({'start': 0, 'stop': page['total'], 'sections': list(sections)})]).decode()\n"
        f"with Path({str(plane)!r}).open('a') as output:\n"
        "    output.write(json.dumps({'request': request, "
        "'opening': (work / '.evolution/OPENING.md').read_text(), "
        "'validation': (work / '.evolution/VALIDATION.json').read_text() if repair else None, "
        "'page': page, 'read': read, 'stream': stream}) + '\\n')\n"
        "if repair:\n"
        + "\n".join("    " + line for line in GROUP_ALL_AGENT_BODY.splitlines())
        + "\nelse:\n    (work / '.evolution/GROUPS.json').write_text('{broken')\n"
    )
    section = sealed_grouping_cli(tmp_path, body=collect)

    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )

    corpus = (tmp_path / "corpus.jsonl").read_text()
    assert train_private in corpus and test_value in corpus
    assert [json.loads(line)["split"] for line in corpus.splitlines()] == ["train", "test"]
    assert snapshot.finished["outcome"] == "completed"
    (grouped,) = payloads(run, "grouping_finished")
    assert grouped["fallback"] is None
    assert grouped["calls"] == {"agent_calls": 2, "repairs": 1, "turns": 2}
    (started,) = payloads(run, "grouping_started")
    assert published and set(published) == {(started["baseline_binding"], frozenset({"answered"}))}
    (baseline,) = payloads(run, "baseline_bound")
    score_events = _records(run / "measurements" / baseline["measurement_id"] / "events.jsonl")
    assert {
        row["payload"]["case_id"] for row in score_events if row["event"] == "scoring_started"
    } == {"c1"}
    projections = [json.loads(line) for line in plane.read_text().splitlines()]
    assert len(projections) == 2
    assert projections[0]["validation"] is None
    assert projections[1]["validation"] is not None
    for projection in projections:
        assert projection["page"]["total"] == 1
        assert len(projection["read"]) == len(sections)
        assert "obs:c1:1" in projection["stream"]
        assert train_private not in json.dumps(projection)
        assert test_value not in json.dumps(projection)
    assert (
        verify_evidence(run, load_partition(tmp_path / "corpus.jsonl").train.cases)["mismatches"]
        == []
    )


@pytest.mark.parametrize("old_directory", [False, True])
async def test_durable_grouping_survives_an_unfinished_resume(tmp_path, monkeypatch, old_directory):
    from evolution.tree import ScorerTraceReader
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash
    from tests.scorer_trace_packs import write_directory_trace

    refusals = _spy_refusals(monkeypatch)
    section = sealed_grouping_cli(tmp_path)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_finished")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, family14_settings={"grouper": section})
    old_bytes = {}
    old_ids = set()
    if old_directory:
        # Model the prior directory writer only in this ephemeral campaign.
        root = tmp_path / "run"
        traces = []
        with ScorerTraceReader(root) as reader:
            for journal in (root / "measurements").glob("*/events.jsonl"):
                for event in map(json.loads, journal.read_text().splitlines()):
                    if event["event"] == "scoring_started":
                        record = reader.read(
                            event["payload"]["score_request_id"], measurement_id=event["run_id"]
                        )
                        assert record is not None and record.layout == "pack"
                        traces.append(record)
        for record in traces:
            old_ids.add(record.score_request_id)
            directory = write_directory_trace(
                root / "scorer-traces",
                record.score_request_id,
                request=record.request,
                stdout=record.stdout,
                stderr=record.stderr,
            )
            old_bytes.update(
                {str(p.relative_to(root)): p.read_bytes() for p in directory.iterdir()}
            )
        for location in {record.location for record in traces}:
            (root / location).unlink()
    trace = (tmp_path / "grouper-invocations.jsonl").read_text()
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    if old_directory:
        assert old_bytes
        assert all((run / path).read_bytes() == body for path, body in old_bytes.items())
        assert {p.name for p in (run / "scorer-traces/scorer_traces").iterdir()} == old_ids
        assert list((run / "scorer-traces").glob("*.pack"))
    assert refusals == []
    assert snapshot.finished["outcome"] == "completed"
    assert (tmp_path / "grouper-invocations.jsonl").read_text() == trace
    assert len(payloads(run, "grouping_finished")) == 1
    assert list((run / ".grouping").iterdir()) == []
    assert audit(run)["mismatches"] == []


async def test_no_failing_baseline_never_starts_agent(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER

    refusals = _spy_refusals(monkeypatch)
    section = sealed_grouping_cli(tmp_path, body="raise AssertionError('must not run')")
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        family14_settings={"grouper": section},
        scorer=TWO_CASE_SCORER.replace(
            'passed = "edited" in view', 'passed = True or "edited" in view'
        ),
    )
    assert refusals == []
    assert snapshot.finished["outcome"] == "completed"
    assert not payloads(run, "grouping_started")
    assert not (tmp_path / "grouper-invocations.jsonl").exists()
    assert not (run / ".grouping").exists()
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("changed", ["prompt", "program", "config"])
async def test_resume_refuses_changed_agent_identity(tmp_path, monkeypatch, changed):
    from evolution.tree.execution.output_admission import EvolveRefused
    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash

    prompt = tmp_path / "grouper-prompt.md"
    prompt.write_text("Group observed failures.\n")
    section = sealed_grouping_cli(tmp_path, system_prompt=str(prompt))
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_finished")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, family14_settings={"grouper": section})
    journal = (tmp_path / "run/campaign.jsonl").read_bytes()
    if changed == "prompt":
        prompt.write_text("Group failures by their observations.\n")
    elif changed == "program":
        program = tmp_path / "grouper-cli"
        program.write_text(program.read_text() + "\n# upgraded\n")
    else:
        section = {**section, "max_turns": 5}
    with pytest.raises(EvolveRefused) as caught:
        await family14_run(tmp_path, monkeypatch, family14_settings={"grouper": section})
    assert caught.value.code == "run_identity_changed"
    assert caught.value.effects == ("rebase",)
    assert (tmp_path / "run/campaign.jsonl").read_bytes() == journal
    assert len((tmp_path / "grouper-invocations.jsonl").read_text().splitlines()) == 1


async def test_forwarded_environment_snapshot_and_transcript_redaction(tmp_path, monkeypatch):
    import hashlib

    from evolution.tree import evolve as evolve_module
    from tests.agent_grouping_fakes import GROUP_ALL_AGENT_BODY

    token = "task4-forwarded-secret-384172"
    rotated = "task4-rotated-secret-489321"
    monkeypatch.setenv("AGENT_TOKEN", token)
    original = evolve_module._run_secrets

    def rotate_after_snapshot(workflow, environ):
        secrets = original(workflow, environ)
        monkeypatch.setenv("AGENT_TOKEN", rotated)
        return secrets

    monkeypatch.setattr(evolve_module, "_run_secrets", rotate_after_snapshot)
    section = sealed_grouping_cli(
        tmp_path,
        allowed_env=["AGENT_TOKEN"],
        body=GROUP_ALL_AGENT_BODY + "\nprint(os.environ['AGENT_TOKEN'], file=sys.stderr)\n",
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    assert snapshot.finished["outcome"] == "completed"
    invocation = json.loads((tmp_path / "grouper-invocations.jsonl").read_text())
    assert invocation["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    log = next((run / "reflector-logs").glob("grouping-*.jsonl")).read_text()
    assert token not in log and rotated not in log
    assert "[REDACTED]" in log
    journal = (run / "campaign.jsonl").read_text()
    assert token not in journal and rotated not in journal
    assert audit(run)["mismatches"] == []


async def test_rotated_environment_resumes_with_same_identity(tmp_path, monkeypatch):
    import hashlib

    from tests.agent_grouping_fakes import GROUP_ALL_AGENT_BODY

    first, second = "task4-old-token-11111", "task4-new-token-22222"
    section = sealed_grouping_cli(
        tmp_path,
        allowed_env=["AGENT_TOKEN"],
        body=f"if os.environ['AGENT_TOKEN'] == {first!r}:\n    sys.exit(9)\n"
        + GROUP_ALL_AGENT_BODY,
    )
    monkeypatch.setenv("AGENT_TOKEN", first)
    run, suspended = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    assert suspended.finished is None
    assert payloads(run, "grouping_failed")[0]["reason"] == "agent_error"
    assert list((run / ".grouping").iterdir()) == []
    monkeypatch.setenv("AGENT_TOKEN", second)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    assert snapshot.finished["outcome"] == "completed"
    invocations = [
        json.loads(line)
        for line in (tmp_path / "grouper-invocations.jsonl").read_text().splitlines()
    ]
    assert [call["token_sha256"] for call in invocations] == [
        hashlib.sha256(value.encode()).hexdigest() for value in (first, second)
    ]
    assert invocations[0]["root"] != invocations[1]["root"]
    assert not payloads(run, "identity_rebased")
    assert list((run / ".grouping").iterdir()) == []
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("ending", ["success", "failure", "cancelled", "reader_setup"])
async def test_agent_directory_and_reader_always_discarded(tmp_path, monkeypatch, ending):
    import asyncio

    from evolution.tree.roles.reflection_reader import FrozenReflectionView, ReaderArgumentError

    refusals = _spy_refusals(monkeypatch)
    readers = []
    original = FrozenReflectionView.open_operation

    def opened(self, operation_id, methods):
        if operation_id.startswith("group-") and ending == "reader_setup":
            raise ValueError("injected setup failure")
        reader = original(self, operation_id, methods)
        if operation_id.startswith("group-"):
            readers.append(reader)
        return reader

    monkeypatch.setattr(FrozenReflectionView, "open_operation", opened)
    body = {"failure": "sys.exit(7)", "cancelled": "import time\ntime.sleep(30)"}.get(ending)
    section = sealed_grouping_cli(tmp_path, body=body)
    run, snapshot = await asyncio.wait_for(
        family14_run(
            tmp_path,
            monkeypatch,
            family14_settings={"grouper": section},
            cancelled=lambda: (
                ending == "cancelled" and (tmp_path / "grouper-invocations.jsonl").exists()
            ),
        ),
        20,
    )
    assert refusals == []
    assert list((run / ".grouping").iterdir()) == []
    assert len(readers) == (0 if ending == "reader_setup" else 1)
    assert all(reader._closed and reader._server is None for reader in readers)
    for reader in readers:
        assert reader.methods == {
            "list_grouping_inputs",
            "read_grouping_input",
            "stream_grouping_inputs",
        }
        with pytest.raises(ReaderArgumentError, match="operation_closed"):
            reader.dispatch("list_grouping_inputs", {"offset": 0})
    if ending == "success":
        assert snapshot.finished["outcome"] == "completed"
    else:
        (failed,) = payloads(run, "grouping_failed")
        assert (
            failed["code"]
            == {
                "failure": "role_failed",
                "cancelled": "interrupted",
                "reader_setup": "malformed_result",
            }[ending]
        )
    assert audit(run)["mismatches"] == []


def test_discard_directory_handles_deep_readonly_trees_and_external_links(tmp_path):
    from evolution.tree.roles.workspace import discard_directory

    root = tmp_path / "operation"
    root.mkdir()
    external = tmp_path / "keep"
    external.mkdir()
    (external / "sentinel").write_text("untouched")
    (root / "external").symlink_to(external, target_is_directory=True)
    branch = root
    directories = [root]
    for _ in range(1100):
        branch = branch / "d"
        branch.mkdir()
        directories.append(branch)
    (branch / "result").write_text("discard")
    for directory in reversed(directories):
        directory.chmod(0o500)
    discard_directory(root)
    assert not root.exists()
    assert (external / "sentinel").read_text() == "untouched"


@pytest.mark.parametrize("binding", ["agent_program", "agent_prompt"])
async def test_runtime_refuses_a_binding_that_does_not_match_identity(
    tmp_path, monkeypatch, binding
):
    from dataclasses import replace

    from evolution.tree.roles.grouping.grouping_runtime import GroupingRuntime

    refusals = _spy_refusals(monkeypatch)
    original = GroupingRuntime.agent

    async def replaced(self, *args, **kwargs):
        seal = getattr(self.config, binding)
        config = replace(self.config, **{binding: replace(seal, sha256="0" * 64)})
        return await original(GroupingRuntime(config), *args, **kwargs)

    monkeypatch.setattr(GroupingRuntime, "agent", replaced)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": sealed_grouping_cli(tmp_path)}
    )
    assert refusals == []
    assert snapshot.finished is None
    assert payloads(run, "grouping_failed")[0]["code"] == "changed_seal"
    assert not (tmp_path / "grouper-invocations.jsonl").exists()
    assert list((run / ".grouping").iterdir()) == []
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("reason", ["agent_error", "timeout", "exception"])
async def test_agent_unknown_failure_suspends_without_closing_round(tmp_path, monkeypatch, reason):
    from evolution.llm.agent_backend import AgentBackend
    from tests.test_tree_campaign_family14_grouper_run import _assert_suspended_on_grouping

    # RuntimeError is a backend defect, not the CLI's nonzero-exit contract.
    # Keep the real call/log/reader lifecycle around this narrow seam.
    if reason == "exception":

        async def broken_backend(self, *args, **kwargs):
            raise RuntimeError("unexpected backend failure")

        monkeypatch.setattr(AgentBackend, "complete_file_edit", broken_backend)
    body = "import time\ntime.sleep(30)" if reason == "timeout" else "sys.exit(7)"
    section = sealed_grouping_cli(tmp_path, body=body, timeout_seconds=10)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    _assert_suspended_on_grouping(
        run,
        snapshot,
        failed={"code": "role_failed", "reason": reason, "error_class": "unknown"},
        suspension=("grouper_failing", "grouper"),
    )
    assert list((run / ".grouping").iterdir()) == []
    if reason != "exception":
        assert len((tmp_path / "grouper-invocations.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("answer", ["missing", "invalid_output"])
async def test_agent_unusable_answer_gets_one_repair_then_content_caveat(
    tmp_path, monkeypatch, answer
):
    from tests.agent_grouping_fakes import scripted_grouping_cli
    from tests.test_tree_campaign_family14_agent_grouper_chain import assert_caveats

    section = scripted_grouping_cli(tmp_path, answer)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    assert snapshot.finished["outcome"] == "completed"
    assert not payloads(run, "grouping_failed")
    assert_caveats(run, snapshot, [(1, "invalid_output", "all")])
    calls = [
        json.loads(line)
        for line in (tmp_path / "grouper-invocations.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 2 and calls[0]["root"] == calls[1]["root"]
    assert [call["repair"] for call in calls] == [False, True]
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "fault,code",
    [
        ("malformed", "malformed_result"),
        ("secret", "secret_detected"),
        ("seal", "changed_seal"),
        ("reader", "reader_integrity"),
    ],
)
async def test_agent_existing_fault_route_wins_over_cli_failure(tmp_path, monkeypatch, fault, code):
    from evolution.llm.agent_backend import AgentBackend
    from evolution.tree.roles.reflection_reader import ReaderIntegrityError, ReflectionReadSession
    from tests.test_tree_campaign_family14_grouper_run import _assert_suspended_on_grouping

    secret = "h9-task5-private-secret-485732"
    monkeypatch.setenv("AGENT_TOKEN", secret)
    body = "sys.exit(7)"
    if fault == "secret":
        body = (
            "(work / '.evolution/GROUPS.json').write_text(os.environ['AGENT_TOKEN'])\nsys.exit(7)"
        )
    if fault == "seal":
        body = "(work / '.evolution/request.json').chmod(0o600)\n(work / '.evolution/request.json').write_text('{}')\nsys.exit(7)"
    if fault == "malformed":

        async def malformed_backend(self, *args, **kwargs):
            raise ValueError("malformed backend value")

        monkeypatch.setattr(AgentBackend, "complete_file_edit", malformed_backend)
    if fault == "reader":
        original = ReflectionReadSession.verify_admission

        def failed_reader(self, check):
            # Inject an actual sticky reader fault after the real CLI exits.
            # Its source bytes stay intact so the offline audit can verify them.
            if (tmp_path / "grouper-invocations.jsonl").exists():

                def faulted_check():
                    raise ReaderIntegrityError("source_changed")

                return original(self, faulted_check)
            return original(self, check)

        monkeypatch.setattr(ReflectionReadSession, "verify_admission", failed_reader)
    section = sealed_grouping_cli(tmp_path, body=body, allowed_env=["AGENT_TOKEN"])
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, family14_settings={"grouper": section}
    )
    _assert_suspended_on_grouping(
        run, snapshot, failed={"code": code}, suspension=("grouping_failed", "campaign")
    )
    assert secret not in (run / "campaign.jsonl").read_text()
    assert list((run / ".grouping").iterdir()) == []


@pytest.mark.parametrize(
    "when",
    ["hanging", "last_error", "last_timeout", "repair_hanging", "repair_error", "repair_timeout"],
)
async def test_agent_cancellation_wins_and_expires_reader(tmp_path, monkeypatch, when):
    import asyncio

    from evolution.llm.agent_backend import AgentBackend, AgentBackendError
    from evolution.tree.roles.reflection_reader import FrozenReflectionView, ReaderArgumentError
    from tests.agent_grouping_fakes import scripted_grouping_cli
    from tests.campaign_drivers import _records

    readers = []
    original_open = FrozenReflectionView.open_operation

    def capture_reader(self, operation_id, methods):
        reader = original_open(self, operation_id, methods)
        if operation_id.startswith("group-"):
            readers.append(reader)
        return reader

    monkeypatch.setattr(FrozenReflectionView, "open_operation", capture_reader)
    stop = False
    original_call = AgentBackend.complete_file_edit
    calls = 0

    async def stop_at_completion(self, *args, **kwargs):
        nonlocal calls, stop
        calls += 1
        target = 2 if when.startswith("repair") else 1
        if calls == target:
            # Atomic seam: stop becomes true immediately before the last
            # failing backend completion, with no await for the poller to win.
            stop = True
            if when.endswith("timeout"):
                raise AgentBackendError("deadline", timed_out=True)
            raise AgentBackendError("CLI failed")
        return await original_call(self, *args, **kwargs)

    if not when.endswith("hanging"):
        monkeypatch.setattr(AgentBackend, "complete_file_edit", stop_at_completion)
    mode = (
        "repair_hang"
        if when == "repair_hanging"
        else "hang"
        if when == "hanging"
        else "invalid_output"
    )
    section = scripted_grouping_cli(tmp_path, mode)

    def cancelled():
        if not when.endswith("hanging"):
            return stop
        trace = tmp_path / "grouper-invocations.jsonl"
        return trace.exists() and len(trace.read_text().splitlines()) >= (
            2 if when.startswith("repair") else 1
        )

    run, snapshot = await asyncio.wait_for(
        family14_run(
            tmp_path, monkeypatch, family14_settings={"grouper": section}, cancelled=cancelled
        ),
        20,
    )
    records = _records(run / "campaign.jsonl")
    assert [r["event"] for r in records[-3:]] == [
        "grouping_started",
        "grouping_failed",
        "campaign_finished",
    ]
    started, failed, finished = records[-3:]
    assert failed["operation_id"] == started["operation_id"]
    assert failed["payload"] == {
        "round": 1,
        "grouping_id": started["payload"]["grouping_id"],
        "code": "interrupted",
        "reads": failed["payload"]["reads"],
    }
    assert (finished["payload"]["outcome"], finished["payload"]["reason"]) == (
        "stopped",
        "cancelled",
    )
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert not payloads(run, "campaign_suspended")
    assert not payloads(run, "round_closed")
    if when.endswith("hanging"):
        import os

        trace = [
            json.loads(line)
            for line in (tmp_path / "grouper-invocations.jsonl").read_text().splitlines()
        ]
        assert [call["repair"] for call in trace] == (
            [False, True] if when.startswith("repair") else [False]
        )
        for call in trace:
            with pytest.raises(ProcessLookupError):
                os.kill(call["pid"], 0)
    else:
        assert calls == (2 if when.startswith("repair") else 1)
    assert len(readers) == 1
    reader = readers[0]
    assert reader._closed and reader._server is None
    with pytest.raises(ReaderArgumentError, match="operation_closed"):
        reader.dispatch("list_grouping_inputs", {"offset": 0})
    assert list((run / ".grouping").iterdir()) == []
    assert audit(run)["mismatches"] == []


async def test_agent_replay_leaves_genuinely_unavailable_failure_unassigned(tmp_path, monkeypatch):
    from evolution.tree import campaign as campaign_module
    from evolution.tree.execution.rollout_scoring import SubprocessScorer
    from tests.campaign_drivers import OBSERVE, TWO_CASE_SCORER, _cut_hook
    from tests.campaign_fakes import CONNECTION_PROBING, Crash

    # Leave H10's connection probe intact. Run the measurement scorer once
    # without its requested trace id: the score is durable, but the observation
    # cannot authenticate a grouping reader view.
    original_score = SubprocessScorer.score_traced
    untraced = []

    async def score_without_one_trace(self, case, view, candidate, *, trace_id, pack_id):
        if not CONNECTION_PROBING.get() and case.case_id == "c1" and not untraced:
            untraced.append(trace_id)
            return await self.score(case, view, candidate)
        return await original_score(self, case, view, candidate, trace_id=trace_id, pack_id=pack_id)

    monkeypatch.setattr(SubprocessScorer, "score_traced", score_without_one_trace)
    admitted = []
    original_admit = campaign_module.admit_grouping_result

    def capture_admission(*args, **kwargs):
        result = original_admit(*args, **kwargs)
        admitted.append(result)
        return result

    monkeypatch.setattr(campaign_module, "admit_grouping_result", capture_admission)
    section = sealed_grouping_cli(tmp_path)
    config = {
        **OBSERVE,
        "repeats": 2,
        "scorer": TWO_CASE_SCORER.replace('"critical_pass": True', '"critical_pass": passed'),
    }
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_grouping_finished")
        with pytest.raises(Crash):
            await family14_run(
                tmp_path,
                monkeypatch,
                config,
                family14_settings={"grouper": section},
            )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, family14_settings={"grouper": section}
    )
    assert snapshot.finished["outcome"] == "completed"
    assert len(admitted) >= 1
    grouped = admitted[-1]
    (unavailable,) = [
        index
        for index, record in enumerate(grouped.registry.records)
        if record.failing and not record.available
    ]
    assert grouped.result.assignments[unavailable] is None
    assert any(item is not None for item in grouped.result.assignments)
    assert all(unavailable not in item.member_indices for item in grouped.neighborhoods)
    (finished,) = payloads(run, "grouping_finished")
    assert finished["result"]["assignments"] == list(grouped.result.assignments)
    assert len(payloads(run, "grouping_started")) == 1
    assert len((tmp_path / "grouper-invocations.jsonl").read_text().splitlines()) == 1
    assert len(untraced) == 1
    assert payloads(run, "connection_preflight_finished")
    from evolution.tree import read_scorer_trace

    assert read_scorer_trace(run, untraced[0]) is None
    assert audit(run)["mismatches"] == []
