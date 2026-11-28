"""Shared content forms: core_raw reads, disclosure, masking, then bounded output.

An unreadable part carries its own state (ADR-0019); this module never imports core.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import content, core_raw


def _finish(body, field_max, omitted=0, *, tail_paths=()):
    clipped, truncation = content.clip_fields(
        content.mask(body), field_max, tail_paths=tail_paths,
        preserve_paths=tuple("/" + name for name in (
            "schema", "operation_id", "measurement_id", "case_id", "sample_id", "attempt_id", "reflection_id",
            "source", "disclosure", "masking")))
    clipped["truncation"] = truncation
    clipped["truncated"] = bool(truncation or omitted)
    return clipped


def _scorer_output(stdout):
    try:
        value = json.loads(stdout) if stdout is not None else None
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    verdicts = value.get("verdicts")
    if verdicts is not None and (not isinstance(verdicts, list) or any(
            not isinstance(verdict, dict) or not isinstance(verdict.get("criterion"), str)
            or not isinstance(verdict.get("passed"), bool)
            or (verdict.get("note") is not None and not isinstance(verdict["note"], str))
            for verdict in verdicts)):
        return None
    return value


def _trace_view(trace):
    """Normalize only raw trace shapes consumed here; keep independent input/oracle."""
    if trace is None:
        return {}, [], False
    view = dict(trace["view"])
    broken = view.get("terminal") is not None and not isinstance(view["terminal"], str)
    if broken:
        view["terminal"] = None
    history = view.get("history")
    if history is None:
        return view, [], broken
    if not isinstance(history, dict):
        return view, [], True
    turns = history.get("turns")
    if turns is None:
        return view, [], broken
    if not isinstance(turns, list):
        return view, [], True
    for turn in turns:
        if not isinstance(turn, dict):
            return view, [], True
        if turn.get("message") is not None and not isinstance(turn["message"], dict):
            return view, [], True
        events = turn.get("events")
        if events is not None and (not isinstance(events, list) or any(not isinstance(event, dict) for event in events)):
            return view, [], True
    return view, turns, broken


def _text_shape(value):
    # content._text joins the text fields of OpenAI content parts. Other JSON
    # values are serializable as-is; malformed part text must not reach join.
    return not isinstance(value, list) or all(
        isinstance(part, dict) and isinstance(part.get("text", ""), str) for part in value)


def _role_records_readable(records):
    """Check raw operands before projection, without swallowing code errors."""
    for record in records:
        phase = record.get("phase")
        if phase is not None and not isinstance(phase, str):
            return False
        if phase == "model_request":
            after = record.get("messages_after")
            if after is not None and (type(after) is not int or after < 0):
                return False
            messages = record.get("messages")
            if messages is not None and (not isinstance(messages, list) or any(
                    not isinstance(message, dict) or not _text_shape(message.get("content")) for message in messages)):
                return False
        elif phase == "model_response":
            calls = record.get("calls")
            if calls is not None and (not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls)):
                return False
        if phase in {"model_response", "tool_call", "tool_error"}:
            # The projection uses turn as a dictionary key to pair calls/results.
            if record.get("turn") is not None and type(record["turn"]) is not int:
                return False
        text_key = {"model_response": "content", "tool_call": "result", "tool_error": "error",
                    "finish_reflection_rejected": "error", "refused": "error", "failed": "note",
                    "request": "opening", "response": "content", "exception": "error"}.get(phase)
        if text_key is not None and not _text_shape(record.get(text_key)):
            return False
        if phase == "response":
            diagnostics = record.get("diagnostics")
            if diagnostics is not None and (not isinstance(diagnostics, dict) or any(
                    diagnostics.get(key) is not None and not isinstance(diagnostics[key], str)
                    for key in ("last_message", "stderr_tail"))):
                return False
        elif phase == "published":
            changed = record.get("changed")
            if changed is not None and (not isinstance(changed, list) or any(not isinstance(path, str) for path in changed)):
                return False
        elif phase == "reflected":
            if not isinstance(record.get("status"), str) or type(record.get("groups")) is not int:
                return False
    return True


def sample_view(operation_id, measurement_id, row, root: Path, journal: Path, disclose: bool) -> dict:
    case_id, sample_id = row["case_id"], row["sample_id"]
    trace, trace_state, scoring, stdout = None, {"state": "unavailable", "reason": "not_scored"}, None, None
    returncode = None
    try:
        scoring = core_raw.scoring_of(journal, case_id, sample_id)
    except core_raw.CoreRawUnavailable as error:
        trace_state = {"state": "unavailable", "reason": error.code}
    if scoring is not None:
        if scoring["state"] in ("finished", "failed"):
            try:
                found = core_raw.scorer_trace(root, scoring["score_request_id"], case_id,
                                              measurement_id=measurement_id)
                trace, stdout, returncode = found["input"], found["stdout"], found["returncode"]
                trace_state = ({"state": "available"} if found["output_reason"] is None else
                               {"state": "unavailable", "reason": found["output_reason"]})
            except core_raw.CoreRawUnavailable as error:
                trace_state = {"state": "unavailable", "reason": "trace_missing" if error.code == "missing" else error.code}
        else:
            trace_state = {"state": "unavailable", "reason": scoring["state"]}
    admitted = scoring is not None and scoring["state"] == "finished"
    out = _scorer_output(stdout) if admitted else None
    view, raw_turns, trace_broken = _trace_view(trace)
    if trace_broken or (admitted and stdout is not None and out is None):
        # The trace artifact is incomplete, while its readable dialogue and the
        # journal's admitted metrics remain independently inspectable.
        trace_state = {"state": "unavailable", "reason": "unreadable"}
    scoring_part = {
        "state": "admitted" if admitted else "failed" if scoring and scoring["state"] == "failed" else "not_scored",
        "metrics": scoring["metrics"] if admitted else None,
        "critical_pass": scoring["critical_pass"] if admitted else None,
        "verdicts": [{"criterion": v.get("criterion"), "passed": v.get("passed")}
                     for v in (out or {}).get("verdicts") or ()] if admitted else None,
        "public_feedback": (out or {}).get("public_feedback") if admitted else None,
    }
    private_part = {
        "oracle": (trace or {}).get("oracle"), "diagnostics": (out or {}).get("diagnostics"),
        "details": (out or {}).get("details"),
        "verdict_notes": [{"criterion": v.get("criterion"), "note": v.get("note")}
                          for v in (out or {}).get("verdicts") or () if v.get("note") is not None],
    }
    if scoring is not None and scoring["state"] == "failed":
        private_part["scorer_output"] = {"stdout": stdout, "exit_status": returncode}

    def build(field_max, list_max):
        turns, omitted = content.clip_list(raw_turns, list_max)
        events_omitted = 0
        bounded_turns = []
        for turn in turns:
            bounded = dict(turn)
            if isinstance(turn.get("events"), list):
                bounded["events"], cut = content.clip_list(turn["events"], list_max)
                events_omitted += cut
            bounded_turns.append(bounded)
        body = {
            "schema": "lab-sample/1", "operation_id": operation_id, "measurement_id": measurement_id,
            "case_id": case_id, "sample_id": sample_id, "source": "core_raw",
            "disclosure": "private" if disclose else "public", "masking": "keys",
            "execution_state": row.get("execution_state"), "trace": trace_state,
            "try": None if scoring is None or scoring["score_request_id"] is None else {
                "score_request_id": scoring["score_request_id"], "retry_ordinal": scoring["retry_ordinal"],
                "tries_total": scoring["tries_total"]},
            "input": None if trace is None else {"public": trace.get("public")},
            "turns": bounded_turns, "turns_omitted": omitted, "terminal": view.get("terminal"),
            "scoring": scoring_part, "private": content.private(private_part, disclose),
        }
        return _finish(body, field_max, omitted + events_omitted)

    return content.fit(build)


def _role_content(role, log_id, kind, run, facts, disclose):
    part = {"source": "core_raw", "kind": kind, "state": "unavailable", "reason": None,
            "events": [], "events_omitted": 0, "tool_calls": "not_recorded", "stderr_tail": None}
    records, skipped = [], 0
    if isinstance(facts, core_raw.CoreRawUnavailable):
        part["reason"] = facts.code
        return part, records, skipped
    completed = log_id in (facts["closed"] if role == "editor" else facts["reflections"])
    if not completed:
        part["reason"] = "incomplete"
        return part, records, skipped
    log_error = None
    if kind != "subprocess":
        try:
            records, broken = core_raw.role_log(run, role, log_id)
        except core_raw.CoreRawUnavailable as error:
            log_error = error
        else:
            if not _role_records_readable(records):
                part["reason"] = "unreadable"
                return part, [], skipped
            if kind is None:
                phases = {r.get("phase") for r in records}
                kind = "chat" if "model_request" in phases else "agent_cli" if "request" in phases else None
                part["kind"] = kind
            if kind in ("chat", "agent_cli"):
                projected = content.project_role(kind, records)
                events = projected["events"]
                if role == "reflector" and kind == "chat":
                    terminal = facts["reflections"][log_id]
                    if terminal["event"] == "reflection_finished":
                        events.append({"type": "reflected", "status": terminal.get("status"), "groups": len(terminal["groups"])})
                part.update(state="available", reason="unreadable" if broken else None,
                            events=content.disclose_events(role, events, disclose), tool_calls=projected["tool_calls"])
                return part, records, projected["skipped"]
    if kind is None and log_error is None:
        part["reason"] = "unreadable"
        return part, records, skipped
    if kind in (None, "subprocess") and (log_error is None or log_error.code == "missing"):
        try:
            stderr = core_raw.role_stderr(run, role, log_id)
        except core_raw.CoreRawUnavailable as error:
            if error.code != "missing":
                part["reason"] = error.code
                return part, records, skipped
            if kind is None:
                part["reason"] = "log_missing"
                return part, records, skipped
            stderr = None
        part.update(kind="subprocess", state="available", reason="not_recorded",
                    stderr_tail=content.private(stderr, disclose) if role == "reflector" and stderr is not None else stderr)
        return part, records, skipped
    part["reason"] = "log_missing" if log_error is not None and log_error.code == "missing" else (
        log_error.code if log_error is not None else "unreadable")
    return part, records, skipped


def _confidence(block):
    if block is None:
        return None
    out = {name: block.get(name) for name in ("method", "tolerance", "retained", "parent_source", "repeats",
                                            "focus_cases", "causal_cases", "calibration")}
    out["levels"] = block.get("confidence")
    out["looks"] = []
    for look in block.get("evaluated") or ():
        row = {name: look.get(name) for name in ("look", "final", "decision", "reason", "would_pass_point", "caveats")}
        for name in ("focus", "causal", "overall"):
            value = look.get(name)
            row[name] = None if value is None else {key: value.get(key) for key in ("point", "lower", "upper", "level", "unavailable")}
        out["looks"].append(row)
    return out


def attempt_view(operation_id, attempt_id, round_no, core_confidence, view_failure, kind, run,
                 journal_facts, disclose):
    editor, records, _ = _role_content("editor", attempt_id, kind, run, journal_facts, disclose)
    changed = journal_facts.get("changed", {}).get(attempt_id) if isinstance(journal_facts, dict) else None
    if changed is None:
        changed = next((event.get("changed") for event in reversed(editor["events"])
                        if event["type"] == "published"), None)
    editor.update(changed=None if isinstance(changed, int) else changed,
                  changed_count=changed if isinstance(changed, int) else None, failure=None)
    if view_failure is not None:
        failure = {"error_class": view_failure.get("error_class"),
                   "reason": view_failure.get("role_reason") or view_failure.get("code")}
        # The attempt may close after a recovered Editor, e.g. at measurement or seal.
        # Only the authoritative Editor failure may borrow text from its terminal session.
        text = None
        if view_failure.get("stage") == "editor" and view_failure.get("role") == "editor":
            if editor["kind"] == "subprocess":
                text = (editor["stderr_tail"], "stderr") if editor["stderr_tail"] is not None else None
            else:
                text = content.failure_text(editor["kind"], records)
        if text is not None:
            failure.update(text=text[0], text_source=text[1])
        editor["failure"] = failure

    def build(field_max, list_max):
        part = dict(editor)
        part["events"], omitted = content.clip_list(editor["events"], list_max)
        part["events_omitted"] = omitted
        body = {"schema": "lab-attempt/1", "operation_id": operation_id, "attempt_id": attempt_id,
                "round": round_no, "disclosure": "private" if disclose else "public", "masking": "keys",
                "confidence": _confidence(core_confidence), "editor": part}
        return _finish(body, field_max, omitted,
                       tail_paths=("/editor/stderr_tail", "/editor/failure/text") if part["kind"] == "subprocess" else ())

    return content.fit(build)


def reflection_view(operation_id, reflection_id, round_no, kind, run, journal_facts, disclose):
    part, _, skipped = _role_content("reflector", reflection_id, kind, run, journal_facts, disclose)
    if part["state"] == "available":
        terminal = journal_facts["reflections"][reflection_id]
        detail = terminal.get("detail") if terminal["event"] == "reflection_failed" else None
        if detail is not None:
            # core_raw admitted this closed category/path through Core's journal grammar.
            # It is still reflector text: the existing disclosure rule applies to every kind.
            text = detail["category"] + (": " + detail["path"] if "path" in detail else "")
            event = {"type": "failed", "error_class": terminal["error_class"], "text": text}
            part["events"].extend(content.disclose_events("reflector", [event], disclose))

    def build(field_max, list_max):
        body = dict(part)
        body["events"], omitted = content.clip_list(part["events"], list_max)
        body.update(schema="lab-reflection/1", operation_id=operation_id, reflection_id=reflection_id,
                    round=round_no, disclosure="private" if disclose else "public", masking="keys",
                    skipped=skipped, events_omitted=omitted)
        return _finish(body, field_max, omitted, tail_paths=("/stderr_tail",))

    return content.fit(build)
