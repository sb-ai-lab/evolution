"""Offline preflight reads the selected immutable inputs and public counts only."""
import json

import pytest

from scaffold_16 import scaffold_16
from test_preparation_bundle import fixture
from evolution_lab.preparation.api import PreparationStore
from evolution_lab.preparation.models import PreparationError


def prepared_chat(tmp_path, monkeypatch, *, reserve=7000, chat_reflector=False):
    _, _, request = fixture(tmp_path, monkeypatch, scaffold_16)
    path = request["source"].manifest_path
    doc = json.loads(path.read_bytes())
    doc["editor"] = {
        "kind": "chat", "model": "client-1", "base_url": "http://localhost:9000/edit/v1",
        "api_key_env": "TOKEN", "context_compact_chars": 100000,
        "context_message_max_chars": 6000, "max_output_tokens": reserve,
        "retry": {"version": "role_call_retry/1", "max_attempts": 2, "total_budget_seconds": 60,
                  "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 8, "jitter": "full"}},
    }
    if chat_reflector:
        settings = {key: doc["reflector"][key] for key in ("environment_docs", "probe", "max_consecutive_failures",
                                                             "selection", "families")}
        doc["reflector"] = {**doc["editor"], **settings}
    doc["target"]["min_input_bytes"] = 4096
    doc["evidence"] = {"version": "evidence/1", "events_per_turn": 3, "text_chars": 32}
    path.write_text(json.dumps(doc))
    state = tmp_path / "state"
    store = PreparationStore(state)
    bundle = store.create_bundle(**request)
    prepared = store.prepare_campaign(bundle.digest)
    return state, prepared.digest, bundle.digest


def test_existing_store_only_and_authenticated_bundle(tmp_path, monkeypatch):
    from evolution_lab.preparation.context_preflight import resolve_preflight_inputs

    absent = tmp_path / "absent"
    with pytest.raises(PreparationError):
        resolve_preflight_inputs(absent, "0" * 64)
    assert not absent.exists()

    state, digest, bundle = prepared_chat(tmp_path, monkeypatch)
    (state / ".publish.lock").unlink()
    result = resolve_preflight_inputs(state, digest)
    assert not (state / ".publish.lock").exists()
    assert result.bundle_digest == bundle
    assert result.routes["editor"]["client_model"] == "client-1"
    assert result.routes["editor"]["upstream_model"] is None
    assert result.routes["editor"]["reserve_tokens"] == 7000
    settings = {row["setting"]: row for row in result.stages}
    assert settings["evidence.events_per_turn"]["value"] == 3
    assert settings["evidence.events_per_turn"]["source"] == "bundle_manifest"
    assert settings["evidence.turns"]["source"] == "core_default"
    assert result.turns == []
    assert not result.grouping_saved

    manifest = result.manifest.path
    manifest.chmod(0o600)
    manifest.write_text("{}")
    with pytest.raises(PreparationError):
        resolve_preflight_inputs(state, digest)


def test_role_log_pairs_style_model_and_provider_usage_ignoring_private_fields():
    from evolution_lab.preparation.context_preflight import role_turns

    rows = role_turns([
        {"phase": "model_request", "turn": 2, "request_style": "legacy", "messages": ["PRIVATE"]},
        {"phase": "model_response", "turn": 2, "request_style": "legacy",
         "requested_model": "c", "returned_model": "r", "returned_provider": "p",
         "usage": {"prompt_tokens": 116678}, "content": "PRIVATE"},
        {"phase": "model_response", "turn": 3, "request_style": "modern",
         "usage": {"prompt_tokens": True}},
    ], role="reflector", client_model="c", grouping_saved=True)
    assert len(rows) == 1
    assert (rows[0]["input_tokens"], rows[0]["request_style"], rows[0]["upstream_model"],
            rows[0]["provider"], rows[0]["grouping_saved"]) == (116678, "legacy", "r", "p", True)
    assert "PRIVATE" not in json.dumps(rows)


def saved_chat_trace(tmp_path, monkeypatch):
    import shutil
    import sqlite3
    from pathlib import Path
    from evolution_lab.execution_policy import policy_digest
    from evolution_lab.preparation.context_preflight import resolve_preflight_inputs

    state, prepared, _ = prepared_chat(tmp_path, monkeypatch, reserve=16384, chat_reflector=True)
    operation = "8e93a4b6fa04495b8a90179c331dd0f5"
    run = state / "artifacts" / operation / operation
    source = Path(__file__).parent / "fixtures/panel/b13/checks/run" / operation
    run.mkdir(parents=True)
    shutil.copy2(source / "campaign.jsonl", run / "campaign.jsonl")
    shutil.copytree(source / "measurements", run / "measurements")
    (run / "editor-logs").mkdir()
    log = run / "editor-logs" / "r01-a01.jsonl"
    log.write_text("\n".join(map(json.dumps, [
        {"phase": "model_request", "turn": 1, "request_style": "modern", "messages": ["PRIVATE"]},
        {"phase": "model_response", "turn": 1, "request_style": "modern", "requested_model": "client-1",
         "returned_model": "up-1", "returned_provider": "provider", "usage": {"prompt_tokens": 116678}},
    ])) + "\n")
    policy = {"schema": "lab-execution-policy/1", "gateway": {
        "host": "localhost", "port": 9000, "ledger_path": str(tmp_path / "ledger"), "limit_units": 999,
        "ledger_identity": "test", "currency": "USD"},
        "routes": {"edit": {"client_model": "client-1", "endpoint": "https://up.invalid/v1",
                            "model": "up-1", "api_key_secret": None,
                            "price": {"input_per_million": 1, "output_per_million": 2, "context_tokens": 8},
                            "max_tokens": 16384, "timeout_seconds": 5}}}
    with sqlite3.connect(state / "owner.sqlite3") as db:
        db.executescript("""
            CREATE TABLE operations (id,run_id,kind,prepared_digest,start);
            CREATE TABLE operation_starts (operation_id,start,prepared_digest);
            CREATE TABLE operation_start_policies (operation_id,start,policy_digest,lab_version);
            CREATE TABLE execution_policies (digest,body_json);
        """)
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (operation, f"c-{operation}", "campaign", prepared, 1))
        db.execute("INSERT INTO operation_start_policies VALUES(?,?,?,?)",
                   (operation, 1, policy_digest(policy), "0.0.3.dev79"))
        db.execute("INSERT INTO execution_policies VALUES(?,?)", (policy_digest(policy), json.dumps(policy)))
    # The producing gateway ledger assigns this route to one start. Other clients' rows cannot attest it.
    from evolution_lab.budget import BudgetLedger
    ledger = BudgetLedger(tmp_path / "ledger", identity="test", limit_units=999)
    route_request = ledger.reserve(1, role="edit", model="up-1", request_hash="x",
                                   routing=("https://up.invalid/v1", "client-1", "up-1",
                                            json.dumps([f"operation:{operation}:start:1"])))
    ledger.record_routing(route_request, None, {"provider": "provider", "model": "up-1"})
    ledger.close_anchor()
    # Export fields can be redacted. Only counts under the supplied envelope enter the report.
    trace = tmp_path / "export.jsonl"
    measurement = "m-r00-champion"
    journal = run / "measurements" / measurement / "events.jsonl"
    header = {"record": "header", "schema": "evolution-v2-trace/1", "run_id": f"c-{operation}",
              "kind": "campaign", "family": 15, "producing_version": "2.0.0b13", "corpus": "verified",
              "journal_heads": {"campaign.jsonl": (run / "campaign.jsonl").stat().st_size,
                                f"measurements/{measurement}/events.jsonl": journal.stat().st_size}}
    row = {"record": "try", "measurement": {"id": measurement},
           "request": {"source": "scorer_trace", "check": "authenticated"}, "oracle": "PRIVATE",
           "turns": [{"index": 1, "message": {}, "events": [{"type": "observation", "content": "a" * 40}] * 6}]}
    trace.write_text("\n".join(map(json.dumps, [header, row])) + "\n")
    return state, prepared, operation, trace


def test_bound_operation_trace_reports_usage_cuts_and_policy(tmp_path, monkeypatch):
    import sqlite3
    from evolution_lab.preparation.context_preflight import resolve_preflight_inputs

    state, prepared, operation, trace = saved_chat_trace(tmp_path, monkeypatch)
    result = resolve_preflight_inputs(state, prepared, trace=trace)
    assert result.grouping_saved
    assert len(result.routes["editor"]["policy_digest"]) == 64
    assert result.routes["editor"]["policy_version"] == "lab-execution-policy/1"
    assert result.routes["editor"]["upstream_model"] == "up-1"
    assert result.routes["editor"]["observed_calls"] == 1
    assert result.routes["editor"]["observed_endpoint"] == "https://up.invalid/v1"
    assert result.turns[0]["input_tokens"] == 116678
    assert result.turns[0]["grouping_saved"]
    cuts = next(row for row in result.stages if row["unit"] == "events" and row["before"] is not None)
    assert (cuts["before"], cuts["after"], cuts["lost"]) == (6, 3, 3)
    chars = next(row for row in result.stages if row["unit"] == "characters" and row["before"] is not None)
    # Six 11-character event types and six 40-character fields; three events retained with 32 field characters.
    assert (chars["before"], chars["after"], chars["lost"]) == (306, 129, 177)
    assert "PRIVATE" not in json.dumps([result.stages, result.routes, result.turns])
    with sqlite3.connect(state / "owner.sqlite3") as db:
        db.execute("UPDATE operations SET prepared_digest=?", ("0"*64,))
    with pytest.raises(PreparationError):
        resolve_preflight_inputs(state, prepared, trace=trace)


def test_bound_fixed_training_measurement_reports_target_usage(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    from evolution_lab.preparation.context_preflight import resolve_preflight_inputs

    state, prepared, bundle = prepared_chat(tmp_path, monkeypatch)
    evaluation = PreparationStore(state).prepare_evaluation(
        bundle, selection={"case_ids": ["q1"]}, repeat_seeds=[17], evaluation_set_id="fixed",
        max_turns=3, timeout_seconds=10, metric_plan={"primary": "score"})
    operation = "68207c8e2014428d8fa735d1220f0dae"
    directory = state / "artifacts" / operation / operation / "measurement"
    directory.mkdir(parents=True)
    fixture_path = Path(__file__).parent / "fixtures/panel/content/run" / operation / "measurement/events.jsonl"
    directory.joinpath("events.jsonl").write_text(fixture_path.read_text().replace(f"c-{operation}", f"e-{operation}"))
    with sqlite3.connect(state / "owner.sqlite3") as db:
        db.executescript("""
            CREATE TABLE operations (id,run_id,kind,prepared_digest,start);
            CREATE TABLE operation_starts (operation_id,start,prepared_digest);
            CREATE TABLE operation_start_policies (operation_id,start,policy_digest,lab_version);
            CREATE TABLE execution_policies (digest,body_json);
        """)
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?)",
                   (operation, f"e-{operation}", "evaluation", evaluation.digest, 1))
        db.execute("INSERT INTO operation_start_policies VALUES(?,?,?,?)", (operation, 1, None, "0.0.3.dev79"))
    data = resolve_preflight_inputs(state, prepared, baseline=directory)
    assert data.baseline_operation_id == operation
    assert data.baseline_version
    # A standalone evaluation produced in an older Owner state still has its own immutable binding.
    import shutil
    archived = tmp_path / "archived-state"
    shutil.copytree(state, archived)
    cross = resolve_preflight_inputs(state, prepared, baseline=archived / directory.relative_to(state))
    assert cross.baseline_operation_id == operation
    assert [row["after"] for row in data.stages if row["stage"] == "target_input"] == [12]


def test_active_wal_preflight_keeps_source_sidecars_byte_identical(tmp_path, monkeypatch):
    from contextlib import closing
    import sqlite3
    from evolution_lab.preparation.context_preflight import resolve_preflight_inputs

    state, prepared, operation, _ = saved_chat_trace(tmp_path, monkeypatch)
    owner = state / "owner.sqlite3"
    with closing(sqlite3.connect(owner)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE operations SET start=2")
        writer.commit()
        before = {p.name: p.read_bytes() for p in state.glob("owner.sqlite3*")}
        with pytest.raises(PreparationError):
            resolve_preflight_inputs(state, prepared, operation_id=operation)
        assert {p.name: p.read_bytes() for p in state.glob("owner.sqlite3*")} == before


def test_operation_rejects_other_role_identity_and_unbound_measurement(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from evolution_lab.preparation.context_preflight import resolve_preflight_inputs

    state, digest, operation, trace = saved_chat_trace(tmp_path, monkeypatch)
    run = state / "artifacts" / operation / operation
    rows = list(map(json.loads, trace.read_text().splitlines()))
    source = rows[1]["measurement"]["id"]
    shutil.copytree(run / "measurements" / source, run / "measurements" / "other")
    rows[0]["journal_heads"]["measurements/other/events.jsonl"] = rows[0]["journal_heads"][f"measurements/{source}/events.jsonl"]
    rows[1]["measurement"]["id"] = "other"
    trace.write_text("\n".join(map(json.dumps, rows)) + "\n")
    with pytest.raises(PreparationError):
        resolve_preflight_inputs(state, digest, trace=trace)

    # Another admitted family-15 journal has a subprocess Editor. A copied role log
    # in its directory cannot bind a chat role request to the selected Bundle.
    other_journal = next((Path(__file__).parent / "fixtures/panel/family15/run").glob("*/campaign.jsonl"))
    (run / "campaign.jsonl").write_text(other_journal.read_text().replace(other_journal.parent.name, operation))
    with pytest.raises(PreparationError):
        resolve_preflight_inputs(state, digest, operation_id=operation)


def test_redacted_role_log_delta_counts_include_prefix_and_drop_after_compaction():
    from evolution_lab.preparation.context_preflight import role_turns

    first = {"role": "user", "content": "é"}
    second = {"role": "assistant", "content": "PRIVATE"}
    records = [
        {"phase": "model_request", "turn": turn, "request_style": "modern", "tool_names": ["read_file"],
         "message_count": count, "messages_after": after, "messages": messages}
        for turn, count, after, messages in [(0, 1, 0, [first]), (1, 2, 1, [second]), (2, 1, 0, [second]),
                                             (3, 4, 3, [second])]]
    rows = role_turns(records, role="editor", client_model="test", grouping_saved=False)
    assert [r["redacted_messages_bytes"] for r in rows] == [32, 73, 42, None]
    assert [r["tool_count"] for r in rows] == [1] * 4
    assert "PRIVATE" not in json.dumps(rows)


def saved_shadow_records(tmp_path, monkeypatch):
    import hashlib

    state, prepared, operation, _ = saved_chat_trace(tmp_path, monkeypatch)
    run = state / "artifacts" / operation / operation
    journal = run / "campaign.jsonl"
    records = list(map(json.loads, journal.read_text().splitlines()))
    check = "repeated_read_without_progress"
    created = records[0]["payload"]
    config = created["adaptation_payload"]["checks"]
    config["modes"][check] = "shadow"

    def digest(document):
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    created["identity"]["checks"] = digest(config)
    created["identity_digest"] = digest(created["identity"])
    created["adaptation"]["digest"] = digest(created["adaptation_payload"])
    return state, prepared, operation, journal, records


@pytest.mark.parametrize(("editor_finished", "editor_hits", "expected_editor"), [
    (True, 2, 2), (True, 0, 0), (False, 2, None),
])
def test_shadow_matches_and_repairs_are_separate_in_saved_report(tmp_path, monkeypatch, editor_finished,
                                                                  editor_hits, expected_editor):
    from evolution_lab.preflight import inspect_context
    from evolution_lab.context_report import render_table

    state, prepared, operation, journal, records = saved_shadow_records(tmp_path, monkeypatch)
    run = journal.parent
    check = "repeated_read_without_progress"
    prefix = []
    for record in records:
        if record["event"] == "editor_finished" and not editor_finished:
            break
        prefix.append(record)
        if record["event"] in ("reflection_finished", "editor_finished"):
            hits = 1 if record["event"] == "reflection_finished" else editor_hits
            if hits:
                record["payload"].setdefault("shadowed", []).append({"check": check, "code": "protocol", "count": hits})
        if record["event"] == "editor_finished":
            break
    journal.write_text("".join(json.dumps(row) + "\n" for row in prefix))
    reflector_log = run / "reflector-logs" / "r01-x01.jsonl"
    reflector_log.parent.mkdir()
    reflector_log.write_text((run / "editor-logs" / "r01-a01.jsonl").read_text())
    report = inspect_context(state, prepared, operation=operation)
    matches = {}
    repairs = {}
    for row in report.rows:
        if row.reason == "repeated_read_shadow_matches" and row.basis == "unavailable":
            matches[row.stage] = None
        for metric in row.metrics:
            if metric.name == "repeated_read_shadow_matches":
                matches[row.stage] = metric.measurement.lower
            elif metric.name == check:
                repairs[row.stage] = metric.measurement.lower
    assert matches == {"reflector": 1, "editor": expected_editor}
    assert repairs == {"reflector": 0, "editor": 0}
    assert "repeated_read_shadow_matches" in render_table(report)


@pytest.mark.parametrize(("role", "event_prefix"), [("reflector", "reflection"), ("editor", "editor")])
@pytest.mark.parametrize("error_class", ["interrupted", "cancelled", "unresponsive"])
def test_stopped_role_has_unknown_shadow_total_even_after_resume(
    tmp_path, monkeypatch, role, event_prefix, error_class,
):
    from copy import deepcopy
    from evolution_lab.preflight import inspect_context

    state, prepared, operation, journal, records = saved_shadow_records(tmp_path, monkeypatch)
    start = next(row for row in records if row["event"] == event_prefix + "_started")
    finish = next(row for row in records if row["event"] == event_prefix + "_finished")
    prefix = records[:finish["sequence"] - 1]
    failure = deepcopy(finish)
    failure["event"] = event_prefix + "_failed"
    fields = ("operation_id", "round", "reflection_id", "reads") if role == "reflector" else ("operation_id", "attempt_id")
    failure["payload"] = {key: finish["payload"][key] for key in fields}
    failure["payload"].update(error_class=error_class, note_digest=None)
    prefix.append(failure)

    def assert_total_unavailable():
        journal.write_text("".join(json.dumps(record) + "\n" for record in prefix))
        rows = inspect_context(state, prepared, operation=operation).rows
        shadow = [row for row in rows if row.stage == role and (
            row.reason == "repeated_read_shadow_matches"
            or any(metric.name == "repeated_read_shadow_matches" for metric in row.metrics)
        )]
        assert [row.basis for row in shadow] == ["unavailable"]
        if role == "editor":
            # The settled Reflector in the same journal still has a known zero.
            assert [metric.measurement.lower for row in rows if row.stage == "reflector"
                    for metric in row.metrics if metric.name == "repeated_read_shadow_matches"] == [0]

    assert_total_unavailable()
    if error_class == "interrupted":
        for original in (start, finish):
            resumed = deepcopy(original)
            resumed["sequence"] = len(prefix) + 1
            resumed["operation_id"] = resumed["payload"]["operation_id"] = "abcd" * 8
            if role == "reflector":
                resumed["payload"]["reflection_id"] = "r01-x02"
            if original is finish:
                resumed["payload"]["shadowed"] = [{"check": "repeated_read_without_progress", "code": "protocol",
                                                    "count": 2}]
            prefix.append(resumed)
        assert_total_unavailable()
