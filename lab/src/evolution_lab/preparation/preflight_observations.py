"""Provenance of offline observations from Lab starts, native logs and trace exports."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from evolution_lab._manifest_schema import campaign_cases
from evolution_lab.execution_policy import bound_chat_route, parse_policy, policy_digest
from evolution_lab.owner import core_raw
from evolution_lab.owner.routing import SnapshotUnavailable, preflight_route_observations, saved_snapshot

from .context_preflight import _stage, role_turns
from .models import PreparationError
from .storage import PreparationStorage


def _refuse(detail):
    raise PreparationError("artifacts_invalid", "read", detail)


def _operation(storage, operation, kind):
    if not isinstance(operation, str) or not re.fullmatch(r"[a-f0-9]{32}", operation):
        _refuse("Invalid operation reference.")
    path = storage.state_directory.absolute() / "owner.sqlite3"
    with saved_snapshot(path) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT run_id,kind,prepared_digest,start FROM operations WHERE id=?", (operation,)).fetchone()
        if row is None or row[0] != f"{kind[0]}-{operation}" or row[1] != kind:
            _refuse("The evidence does not identify a compatible operation.")
        inputs = db.execute("SELECT start,prepared_digest FROM operation_starts WHERE operation_id=? ORDER BY start",
                            (operation,)).fetchall()
        inputs.append((row[3], row[2]))  # Owner archives previous starts only on resume.
        starts = db.execute(
            "SELECT p.start,p.policy_digest,e.body_json,p.lab_version FROM operation_start_policies p "
            "LEFT JOIN execution_policies e ON e.digest=p.policy_digest WHERE p.operation_id=? ORDER BY p.start",
            (operation,)).fetchall()
    expected = list(range(1, row[3] + 1))
    if [s[0] for s in inputs] != expected or any(s[1] != row[2] for s in inputs) or [s[0] for s in starts] != expected:
        _refuse("The operation spans unbound or different prepared inputs.")
    policies = {s[1] for s in starts}
    if len(policies) != 1:
        _refuse("The operation spans different execution policies; role logs do not identify a start.")
    _, policy_id, body, _ = starts[-1]
    versions = {start[3] for start in starts}
    lab_version = versions.pop() if len(versions) == 1 else None
    policy = None
    if policy_id is not None:
        document = json.loads(body)
        if policy_digest(document) != policy_id:
            _refuse("The saved execution policy digest does not match.")
        policy = parse_policy(document)
    return row[2], policy_id, policy, lab_version, expected


def _bind_policy(result, policy_id, policy, operation, starts):
    if policy is None:
        return
    observed = preflight_route_observations(policy, operation, starts) if policy.gateway else {}
    workflow = result.manifest.workflow
    sections = {"reflector": workflow.reflector.role, "editor": workflow.editor, "grouper": workflow.grouper}
    for role, row in result.routes.items():
        row.update(policy_version=policy.to_payload()["schema"], policy_digest=policy_id)
        if policy.gateway is None:
            continue
        name = bound_chat_route(policy, sections[role].base_url, row["client_model"])
        if name is None:
            _refuse("The saved gateway route does not match the prepared role.")
        route = policy.routes[name]
        rows = observed.get(name, [])
        calls = sum(item["calls"] for item in rows) if rows and all(
            item["client_model"] == row["client_model"] and item["upstream_model"] == route.model
            and item["endpoint"] == route.endpoint for item in rows) else None
        row.update(upstream_model=route.model, endpoint=route.endpoint, route_name=name, source="bound_execution_policy",
                   observed_endpoint=route.endpoint if calls else None, observed_calls=calls)
        _stage(result, role, "calls", "operation_route_calls", calls, "gateway_ledger", after=calls,
               status="observed" if calls else "unavailable")


def _campaign(result, storage, operation, header):
    digest, policy_id, policy, lab_version, starts = _operation(storage, operation, "campaign")
    if digest != result.prepared_digest:
        _refuse("The operation was started with different prepared inputs.")
    _bind_policy(result, policy_id, policy, operation, starts)
    run = storage.state_directory / "artifacts" / operation / operation
    if any(path.is_symlink() for path in (run, run.parent, run.parent.parent)):
        _refuse("The operation artifact directory is invalid.")
    facts = core_raw.preflight_campaign(run, f"c-{operation}", {"editor", "reflector"}.intersection(result.routes),
                                         campaign_cases(result.manifest, split="train"))
    result.operation_id = operation
    result.producing_version = facts["producing_version"]
    if header and (header.get("journal_heads", {}).get("campaign.jsonl") != facts["head"]
                   or header.get("producing_version") != result.producing_version):
        _refuse("The trace does not match the saved campaign revision.")
    group_rounds, sessions, diagnoses = facts["groups"], facts["sessions"], facts["diagnoses"]
    attempts = facts["attempts"]
    for count in group_rounds.values():
        _stage(result, "grouper", "assignments", "grouping_finished.result.assignments",
               count, "campaign_journal", after=count, status="observed")
    result.grouping_saved = bool(group_rounds)
    _stage(result, "editor", "attempts", "attempt_opened", attempts, "campaign_journal", after=attempts,
           status="observed_prefix")
    for role, count in facts["shadow_repeats"].items():
        _stage(result, role, "matches", "repeated_read_shadow_matches", count, "campaign_journal",
               status="unavailable" if count is None else "observed")
    for role, session, grouped in sessions:
        if role not in result.routes:
            continue
        try:
            records, broken = core_raw.role_log(run, role, session)
        except core_raw.CoreRawUnavailable:
            _stage(result, role, "turns", "model_request", None, "role_log", status="unavailable")
            continue
        route = result.routes[role]
        turns = role_turns(records, role=role, client_model=route["client_model"], grouping_saved=grouped)
        for row in turns:
            row.update(session=session, policy_version=route["policy_version"], policy_digest=policy_id,
                       lab_version=lab_version, reconstruction="redacted_log")
        result.turns.extend(turns)
        if turns:
            repeats = sum(1 for rec in records if rec.get("phase") == "protocol_repair_requested"
                          and str(rec.get("error", "")).startswith("repeated_read_without_progress"))
            _stage(result, role, "repairs", "repeated_read_without_progress", repeats, "role_log",
                   after=repeats, status="observed_prefix" if broken else "observed_log")
            finished = any(rec.get("phase") == "tool_call" and rec.get("tool") == "finish_reflection"
                           and rec.get("result") == {"finished": True} and rec.get("turn") == turns[-1]["turn"]
                           for rec in records)
            if session in diagnoses and finished and not broken:
                _stage(result, role, "turns", "turns_to_accepted_diagnosis", len(turns), "role_log_and_journal",
                       after=len(turns), status="observed_log")
    return run, facts["measurements"]


def _trace_rows(result, rows, run, measurements, header):
    """A redacted try export supplies observed *export* sizes. It cannot replay a private role view."""
    evidence, seen, counts = core_raw.preflight_export_counts(
        rows, run, header["journal_heads"], measurements, result.manifest.workflow)
    for unit, key, before, after in counts:
        _stage(result, "recorded_episode", unit, f"evidence.{key}", getattr(evidence, key),
               "redacted_trace_scenario", before=before if seen else None,
               after=after if seen and evidence.view == "full" else None,
               status="scenario" if seen else "unavailable")


def _baseline(result, storage, directory):
    directory = directory.absolute()
    if directory.name == "measurement":
        directory = directory.parent
    operation = directory.name
    artifacts = directory.parent.parent
    if (directory.parent.name != operation or artifacts.name != "artifacts"
            or any(path.is_symlink() for path in (directory, directory.parent, artifacts))):
        _refuse("Select a saved Lab evaluation run or its measurement directory.")
    source_store = PreparationStorage(artifacts.parent, read_only=True)
    digest, _, _, _, _ = _operation(source_store, operation, "evaluation")
    prepared = source_store.get_prepared(digest).descriptor
    if prepared.evaluation is None:
        _refuse("The baseline is not a prepared evaluation.")
    if prepared.bundle_digest != result.bundle_digest:
        from .basis import measurement_basis
        actual = measurement_basis(source_store.get_bundle(prepared.bundle_digest))
        if actual is None or actual != measurement_basis(storage.get_bundle(result.bundle_digest)):
            _refuse("The baseline evaluation inputs differ from this Bundle.")
    training = {case.case_id: case for case in campaign_cases(result.manifest, split="train")}
    selected = [training.get(case_id) for case_id in prepared.evaluation.case_ids]
    if any(case is None for case in selected):
        _refuse("The baseline is not a training evaluation.")
    completed, usages, producing_version = core_raw.preflight_baseline(
        directory / "measurement", f"e-{operation}", selected)
    result.baseline_operation_id = operation
    result.baseline_version = producing_version
    _stage(result, "recorded_episode", "turns", "target_send_finished", completed,
           "baseline_measurement_journal", after=completed, status="observed")
    for tokens in usages:
        _stage(result, "target_input", "tokens", "target_send_finished.usage.input_tokens",
               tokens, "target_reported_usage", after=tokens, status="observed")


def read_observations(result, storage, operation_id, trace, baseline):
    try:
        if trace is not None:
            path = Path(trace)
            rows, broken = core_raw.read_jsonl(path.parent, path.name)
            if broken or not rows:
                _refuse("The trace export is incomplete.")
            header = rows[0]
            if (header.get("schema") != "evolution-v2-trace/1" or header.get("record") != "header"
                    or header.get("kind") != "campaign" or header.get("family") != 15):
                _refuse("A family-15 campaign trace export is required.")
            run_id = header.get("run_id")
            if not isinstance(run_id, str) or not run_id.startswith("c-"):
                _refuse("Invalid campaign trace reference.")
            if operation_id is not None and operation_id != run_id[2:]:
                _refuse("The trace refers to a different operation.")
            run, measurements = _campaign(result, storage, run_id[2:], header)
            _trace_rows(result, rows[1:], run, measurements, header)
        elif operation_id is not None:
            _campaign(result, storage, operation_id, None)
        if baseline is not None:
            _baseline(result, storage, Path(baseline))
    except PreparationError:
        raise
    except SnapshotUnavailable:
        _refuse("Supply a checkpointed Owner database for offline inspection.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, core_raw.CoreRawUnavailable):
        _refuse("The supplied local evidence is unavailable or incompatible.")
