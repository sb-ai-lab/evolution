"""Closed CLI output helpers which never echo request input."""
from __future__ import annotations

import json
from typing import Any, TextIO

from evolution_lab._independent_text import (
    attempt_progress_text, preflight_parts, quantity, reference_text, repeat_variance_text,
    round_progress_text, scope_statement, stages_text, with_checks,
)
from evolution_lab._notice_text import CLEAN_ROUNDS_EN, FOCUS_SIGNAL_EN, FOCUS_UNAVAILABLE_RU, all_rounds_clean
from evolution_lab._suspension import last_suspension, suspension_hint


def write_json(stream: TextIO, value: Any) -> None:
    stream.write(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    stream.flush()


def local_error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    return {"error": {"code": code, "stage": "transport", "message": message}, **fields}


_RESUME = "lab resume {id} --target <ready target of the same bundle> --generation <owner generation>"
_STOP = "stop target {target} if it is still running (lab agent stop {target} --generation {generation} --wait)"
_FRESH = ("on a fresh target: " + _STOP + ", start a new target of the same bundle (lab agent start <connection>), "
          "then lab resume {id} --target <new target> --generation <owner generation>")
_READY = (_STOP + ", then lab resume {id} --target {ready} --generation {ready_generation} "
          "(a ready target of the same bundle the campaign has not used)")


def continuation_line(value: dict[str, Any], ready_target: dict[str, Any] | None = None) -> str | None:
    """How a suspended Owner campaign continues, from the status's ``resume`` (PDR-0015)."""
    resume = value.get("resume")
    if not isinstance(resume, dict):
        return None
    identifier = value.get("id", "")
    line = _RESUME.format(id=identifier)
    target = resume.get("target")
    if isinstance(target, dict) and target.get("excluded"):
        # One running target at a time (spec 2026-09-29): the operation's current target is the one
        # to stop. ``excluded`` keeps first occurrences (starts A, B, A -> [A, B]), so its last
        # element is only a fallback when the status has no ``target_ref``.
        current = value.get("target_ref")
        last = current if isinstance(current, dict) else target["excluded"][-1]
        if ready_target is not None:
            line = _READY.format(id=identifier, target=last.get("id"), generation=last.get("owner_generation"),
                                 ready=ready_target["id"], ready_generation=ready_target["owner_generation"])
        else:
            line = _FRESH.format(id=identifier, target=last.get("id"), generation=last.get("owner_generation"))
    state, reason = resume.get("state"), resume.get("reason")
    if state == "ready":
        return f"continuation: {line}"
    if state == "cleanup_first":
        return f"continuation: lab cleanup operation {identifier}, then {line}"
    if state == "refused" and reason == "cleanup_unconfirmed":
        return ("continuation: not available after manual release; "
                "the run directory is kept until results are deleted")
    return None


def _process_summary(process: dict[str, Any]) -> str:
    exit_code = process.get("exit_code")
    if exit_code is not None:
        summary = f"exit:{exit_code}"
    else:
        summary = "launched" if process.get("launched") else "not_launched"
    return summary + (",forced" if process.get("forced") else "")


def _project_summary_extra(value: dict[str, Any]) -> str | None:
    """A second summary line for the project schemas: counts and identifiers that
    ``write_value``'s generic (schema, id, state) line does not surface on its own."""
    schema = value.get("schema")
    if schema == "lab-project-tree/1":
        return f"digest={_token(value.get('digest'))} bundle_covers={_token(value.get('bundle_covers'))}"
    if schema == "lab-project/2":
        implementations = value.get("implementations", [])
        connected = sum(1 for item in implementations if isinstance(item, dict)
                        and isinstance(item.get("origin"), dict) and item["origin"].get("kind") == "connected")
        ports = sum(1 for item in implementations if isinstance(item, dict) and isinstance(item.get("port"), dict))
        return f"implementations={len(implementations)} connected={connected} ports={ports}"
    if schema == "lab-project-list/2":
        return f"projects={len(value.get('projects', []))}"
    if schema == "lab-project-facts/5":
        return f"project={value.get('project')} implementation={value.get('implementation')} facts={len(value.get('facts', []))}"
    if schema == "lab-project-operation-list/1":
        return f"links={len(value.get('links', []))}"
    if schema == "lab-project-confirmation-list/1":
        return f"confirmations={len(value.get('confirmations', []))}"
    if schema in {"lab-project-bundle-result/1", "lab-project-prepared-result/1",
                  "lab-project-target-result/1", "lab-project-start-result/5"}:
        link = value.get("link")
        return f"link={link['kind']}:{link['object_id']}" if isinstance(link, dict) else "link=none"
    if schema == "lab-project-operation-list/2":
        return f"project={_token(value.get('project'))} operations={len(value.get('operations') or [])}"
    if schema == "lab-project-sync-check/1":
        dirty = ", dirty" if value.get("dirty") else ""
        return (f"{_token(value.get('mode'))}, {len(value.get('commits') or [])} commits, "
                f"{len(value.get('files') or [])} files{dirty}")
    if schema == "lab-project-sync-result/1":
        return f"{_token(value.get('mode'))} {str(value.get('merged'))[:12]} → head {str(value.get('head'))[:12]}"
    if schema == "lab-project-status/6":
        catalogue = value.get("catalogue") if isinstance(value.get("catalogue"), dict) else {}
        return (f"project={_token(value.get('project'))} implementation={_token(value.get('implementation'))} "
                f"catalogue={_token(catalogue.get('version'))} facts={len(value.get('facts') or [])}")
    return None


def _token(value: Any) -> str:
    """A response value on one output line: non-printable characters (a newline or an escape
    sequence in a file name) never reach the terminal as such."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))


def _acceptance_fact_line(item: dict[str, Any]) -> str:
    source = item.get("from") if isinstance(item.get("from"), dict) else {}
    codes = item.get("codes") if isinstance(item.get("codes"), list) else []
    parts = [f"fact {_token(item.get('id'))}", f"from={_token(source.get('kind'))}:{_token(source.get('id'))}",
             f"codes={_token(','.join(str(code) for code in codes))}"]
    changes = item.get("changes")
    if not isinstance(changes, dict):
        return " ".join(parts + ["changes=none"])
    counts = changes.get("counts") if isinstance(changes.get("counts"), dict) else {}
    parts += [f"{name}={_token(counts.get(name, 0))}" for name in ("renamed", "modified", "added", "removed")]
    paths = [f"{pair[0]}->{pair[1]}" for pair in changes.get("renamed") or () if isinstance(pair, list) and len(pair) == 2]
    paths += [str(path) for path in changes.get("modified") or ()]
    if paths:
        parts.append("paths=" + _token(",".join(paths[:3])))
    return " ".join(parts)


def _fact_line(item: dict[str, Any]) -> str:
    parts = [f"fact {_token(item.get('id'))}", "holds=yes" if item.get("holds") is True else "holds=no"]
    stale = item.get("stale_reason")
    if isinstance(stale, dict) and stale.get("codes"):
        parts.append("stale=" + _token(",".join(str(code) for code in stale["codes"])))
    if item.get("carried_over"):
        parts.append("carried")
    source = item.get("source")
    if isinstance(source, dict) and source.get("reason"):
        parts.append("reason=" + _token(source["reason"]))  # E8: why package.present does not hold (spec dev46 §3)
    return " ".join(parts)


def _step_line(step: dict[str, Any], indent: str, label: str = "step") -> str:
    parts = [f"{indent}{label} {_token(step.get('id'))} {_token(step.get('state'))}"]
    if step.get("optional") is True:
        parts.append("optional")
    if step.get("human_only") is True:
        parts.append("human_only")
    if step.get("missing"):
        parts.append("missing=" + _token(",".join(str(name) for name in step["missing"])))
    blocked = step.get("blocked_reason")
    if isinstance(blocked, dict) and blocked.get("code"):
        detail = f":{blocked['schema']}" if blocked.get("schema") else ""
        parts.append("blocked=" + _token(f"{blocked['code']}{detail}"))
    invalid = [item for item in step.get("invalid") or () if isinstance(item, dict)]
    if invalid:
        more = f"(+{len(invalid) - 1})" if len(invalid) > 1 else ""
        first = invalid[0]
        where = f"{first.get('artifact') or ''}{first.get('path') or ''}" or "/"
        parts.append("invalid=" + _token(where + more))
    stale = step.get("stale_reason")
    if isinstance(stale, dict) and stale.get("codes"):
        parts.append("stale=" + _token(",".join(str(code) for code in stale["codes"])))
    if step.get("carried_over"):
        parts.append("carried")
    for source in step.get("sources") or ():
        if not isinstance(source, dict):
            continue
        if source.get("results_state") == "deleted":
            parts.append("export=deleted")
        confirmations = [c for c in source.get("confirmations") or () if isinstance(c, dict)]
        if confirmations:
            parts.append("confirmed=" + _token(",".join(
                f"{c.get('author')}@{c.get('channel')}:{c.get('created_at')}" for c in confirmations)))
    if step.get("done_at"):
        parts.append("done_at=" + _token(step["done_at"]))
    return " ".join(parts)


def _project_summary_lines(value: dict[str, Any]) -> list[str]:
    """One line per project operation: label operation_state phase-or-dash outcome-or-dash."""
    lines = []
    for row in value.get("operations") or ():
        operation = row.get("operation") if isinstance(row.get("operation"), dict) else {}
        progress = operation.get("progress") if isinstance(operation.get("progress"), dict) else {}
        outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
        lines.append(" ".join(_token(part) for part in (
            row.get("label"), row.get("operation_state"), progress.get("phase") or "-", outcome.get("outcome") or "-")))
    return lines


def _status_lines(value: dict[str, Any]) -> list[str]:
    lines = []
    for scenario in value.get("scenarios") or ():
        if not isinstance(scenario, dict):
            continue
        head = f"scenario {_token(scenario.get('id'))} state={_token(scenario.get('state'))}"
        if scenario.get("missing"):
            head += " missing=" + _token(",".join(str(name) for name in scenario["missing"]))
        lines.append(head)
        lines += [_step_line(step, "  ") for step in scenario.get("steps") or ()]
        lines += [_step_line(step, "  ", "next") for step in (scenario.get("next_cycle") or {}).get("steps") or ()]
        for cycle in scenario.get("cycles") or ():
            resume = cycle.get("resume") if isinstance(cycle.get("resume"), dict) else {}
            lines.append(" ".join(part for part in (
                f"  cycle {_token(cycle.get('operation_id'))} order={_token(cycle.get('order'))}",
                "current" if cycle.get("current") else "",
                "complete" if cycle.get("complete") is True else "",
                "unavailable" if cycle.get("detail") == "unavailable" else "",
                f"control={_token(cycle.get('control_state'))}",
                f"resume={_token(resume.get('state'))}" if resume else "",
                "target=fresh" if isinstance(resume.get("target"), dict) else "") if part))
            lines += [_step_line(step, "    ") for step in cycle.get("steps") or ()]
    return lines


def _implementation_line(item: dict[str, Any]) -> str:
    origin = item.get("origin") if isinstance(item.get("origin"), dict) else {}
    kind = origin.get("kind")
    parts = [f"  impl {_token(item.get('id'))}", f"place={_token(item.get('place'))}",
             "origin=" + _token(f"{kind}:{origin.get('implementation')}" if kind == "ported_from" else kind)]
    port = item.get("port")
    if isinstance(port, dict):
        parts.append("port=" + _token(f"{port.get('template')}@{str(port.get('commit'))[:12]}"))
    if item.get("archived"):
        parts.append("archived")
    return " ".join(parts)


def _sync_check_lines(value: dict[str, Any]) -> list[str]:
    lines = [f"  commit {_token(str(c.get('sha'))[:12])} {_token(c.get('subject'))}"
             for c in value.get("commits") or () if isinstance(c, dict)]
    if value.get("commits_truncated"):
        lines.append("  commits (truncated)")
    for f in value.get("files") or ():
        if isinstance(f, dict):
            path = f"{_token(f.get('old_path'))} -> {_token(f.get('path'))}" if f.get("status") == "R" else _token(f.get("path"))
            lines.append(f"  {_token(f.get('status'))} {path}")
    if value.get("files_truncated"):
        lines.append("  files (truncated)")
    dirty = value.get("dirty") or []
    for d in dirty:
        if isinstance(d, dict):
            lines.append("  dirty " + _token(d.get("path") if d.get("path") is not None else d.get("state")))
    upstream = value.get("upstream") if isinstance(value.get("upstream"), dict) else {}
    if upstream.get("sha") != value.get("base_revision") and not dirty:
        lines.append(f"next: lab project sync {_token(value.get('project'))} --apply {_token(upstream.get('sha'))}")
    return lines


def _project_detail_lines(value: dict[str, Any]) -> list[str]:
    """One line per fact: what an acceptance (or its dry-run preview) carries, and the facts
    of ``lab project facts`` (final review I3, M3)."""
    schema = value.get("schema")
    if schema == "lab-project-tree/1":
        return [f"{_token(item['type'])} {_token(item['size'])} {_token((item['sha256'] or '')[:12])} {_token(item['path'])}"
                for item in value.get('files', [])]
    if schema == "lab-project/2":
        return [_implementation_line(item) for item in value.get("implementations") or () if isinstance(item, dict)]
    if schema == "lab-project-sync-check/1":
        return _sync_check_lines(value)
    if schema == "lab-project-status/6":
        return _status_lines(value)
    if schema == "lab-project-operation-list/2":
        return _project_summary_lines(value)
    facts = value.get("facts")
    if not isinstance(facts, list):
        return []
    if schema == "lab-project-acceptance/1":
        return [_acceptance_fact_line(item) for item in facts if isinstance(item, dict)]
    if schema == "lab-project-facts/5":
        return [_fact_line(item) for item in facts if isinstance(item, dict)]
    return []


def _progress_line(value: dict[str, Any]) -> str | None:
    progress = value.get("progress")
    if not isinstance(progress, dict):
        state = value.get("progress_state")
        return f"progress: {state}" if state in {"not_started", "unavailable"} else None
    if progress.get("kind") == "evaluation":
        cov = progress.get("coverage") or {}
        return f"progress: scored {cov.get('scored', '-')}/{cov.get('expected', '-')}"
    rounds, attempts = progress["rounds"], progress["attempts"]["current_round"]
    cov = progress.get("coverage") or {}
    planned = "-" if rounds["planned"] is None else rounds["planned"]
    return (f"progress: phase={progress['phase']} round={rounds['current'] or '-'}/{planned} "
            f"attempts={attempts['closed']}+{attempts['running']}/{attempts['planned'] or '-'} "
            f"scored={cov.get('scored', '-')}/{cov.get('expected', '-')}")


def _measurement_line(row: dict[str, Any]) -> str:
    coverage = row.get("coverage") if isinstance(row.get("coverage"), dict) else {}
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    baseline = row.get("baseline") if isinstance(row.get("baseline"), dict) else {}
    delta = baseline.get("delta") if isinstance(baseline.get("delta"), dict) else {}
    cases = baseline.get("cases") if isinstance(baseline.get("cases"), dict) else None
    metric = next(iter(delta), None) or next(iter(metrics), None)
    mean = (metrics.get(metric) or {}).get("mean") if metric is not None else None
    shift = (delta.get(metric) or {}).get("mean") if metric is not None else None
    parts = [_token(row.get("measurement_id")), _token(row.get("role")),
             f"scored={_token(coverage.get('scored', '-'))}/{_token(coverage.get('expected', '-'))}",
             f"{_token(metric or 'metric')}={'-' if mean is None else f'{mean:.4g}'}",
             f"Δ={'-' if shift is None else f'{shift:+.4g}'}"]
    if cases is not None:
        parts.append(f"better/same/worse={cases.get('better')}/{cases.get('same')}/{cases.get('worse')}")
    if baseline and baseline.get("state") != "present":
        parts.append(f"baseline={_token(baseline.get('state'))}")
    repairs = row.get("repairs")
    if isinstance(repairs, dict):
        parts.append(f"repairs={_token(repairs.get('tries'))}/{_token(repairs.get('pairs_scored'))}")
    return " ".join(parts)


def _focus_lines(row):
    if row.get("lab_notices_state") == "unavailable":
        return [FOCUS_UNAVAILABLE_RU]
    return ["notice: " + notice_line(notice) for notice in row.get("lab_notices") or ()]


def _measurement_lines(value: dict[str, Any]) -> list[str]:
    """One line per measurement, and the target-call tokens of a report (PDR-0017)."""
    schema = value.get("schema")
    if schema == "lab-measurement-list/4":
        return [line for row in value.get("measurements") or () if isinstance(row, dict)
                for line in [_measurement_line(row), *_focus_lines(row)]]
    if schema == "lab-measurement/4" and isinstance(value.get("measurement"), dict):
        return [_measurement_line(value["measurement"]), *_focus_lines(value["measurement"])]
    if schema == "lab-owner-report/15" and isinstance(value.get("usage"), list):
        rows = [row["usage"] for row in value["usage"] if isinstance(row, dict) and isinstance(row.get("usage"), dict)]
        if not rows:
            return ["tokens (target calls only): unavailable"]
        total = {key: sum(row.get(key, 0) for row in rows) for key in ("input_tokens", "output_tokens", "unknown_calls")}
        missing = len(value["usage"]) - len(rows)
        return [f"tokens (target calls only): in={total['input_tokens']} out={total['output_tokens']} "
                f"unknown_calls={total['unknown_calls']}" + (f" unavailable_measurements={missing}" if missing else "")]
    return []


def _content_lines(value: dict[str, Any]) -> list[str]:
    schema = value.get("schema")
    if schema not in {"lab-sample/1", "lab-attempt/1", "lab-reflection/1"}:
        return []
    lines = [f"disclosure={value.get('disclosure')} truncated={value.get('truncated')}"]
    if schema == "lab-sample/1":
        trace, scoring, turns = value.get("trace", {}), value.get("scoring", {}), value.get("turns", [])
        lines += [f"trace={trace.get('state')} reason={trace.get('reason')}",
                  f"scoring={scoring.get('state')}"]
        if scoring.get("state") == "admitted":
            critical_pass = scoring["critical_pass"]
            lines.append("critical_pass: " + ("—" if critical_pass is None else str(critical_pass).lower()))
        lines.append(f"turns={len(turns) if isinstance(turns, list) else 'unavailable'} omitted={value.get('turns_omitted')}")
    else:
        role = value.get("editor", {}) if schema == "lab-attempt/1" else value
        events = role.get("events", [])
        lines += [f"role={role.get('kind')} state={role.get('state')} reason={role.get('reason')}",
                  f"events={len(events) if isinstance(events, list) else 'unavailable'} omitted={role.get('events_omitted')}"]
    return lines


def _number(value: Any) -> str:
    return "-" if not isinstance(value, (int, float)) or isinstance(value, bool) else f"{value:.4g}"


def _interval_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "-"
    return f"{_number(value.get('point'))} [{_number(value.get('lower'))}, {_number(value.get('upper'))}]"


def gate_line(family: Any, gate_mode: Any, caveats: Any) -> str | None:
    """The gate of a family-14 campaign; observe is never shown as evidence (spec 2026-10-09 §6.1, §6.2,
    2026-10-13 §7.3). Without a gate mode (its caveats were unreadable) it is an unknown gate, said so, never left
    unmarked."""
    if family != 14:
        return None
    if gate_mode == "statistical":
        return "gate: statistical"
    if gate_mode != "observe":
        return "gate: unknown (unreadable) — not evidence of quality"
    kinds = [c.get("kind") for c in caveats or () if isinstance(c, dict) and c.get("round") is None]
    return "gate: observe — not evidence of quality" + (f" ({', '.join(_token(k) for k in kinds)})" if kinds else "")


def settings_line(settings: Any) -> str | None:
    if not isinstance(settings, dict):
        return None
    refresh = settings.get("baseline_refresh")
    if isinstance(refresh, dict):
        refresh_text = (f"every {_token(refresh.get('every_rounds'))}, "
                        f"performed {len(refresh.get('performed') or ())}")
    else:
        refresh_text = _token(refresh)
    return (f"settings: stability={_token(settings.get('stability'))} "
            f"round_stop={_token(settings.get('round_stop'))} refresh={refresh_text}")


def connection_line(connection: Any) -> str | None:
    """Core's connection check (spec 2026-10-13 §7.3): codes and paths only, never values."""
    if not isinstance(connection, dict):
        return None
    line = (f"connection: {_token(connection.get('state'))} {_token(connection.get('outcome') or '-')} "
            f"attempts={_token(connection.get('attempts'))}")
    detail = connection.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("cause_code")
        line += f" detail={_token(code or '-')}"
        if detail.get("manifest_path"):
            line += f" at {_token(detail['manifest_path'])}"
    return line


def outcome_line(outcome: Any) -> str:
    """Core's campaign outcome as one text line: the word Lab's notices stand next to (PDR-0025); `-` while core has
    no terminal outcome."""
    if not isinstance(outcome, dict):
        return "outcome: -"
    return f"outcome: {_token(outcome.get('outcome'))}/{_token(outcome.get('reason') or '-')}"


def _report_lines(value: dict[str, Any]) -> list[str]:
    """Core's outcome then Lab's notices of a campaign report (PDR-0025), and the gate of a family-14 one (spec
    2026-10-09 §6.2): observe is not evidence of quality. A family-15 report has no family-14 gate: its
    ``lab_independent`` block follows instead (``independent_lines``), unreadable said so with
    ``lab_independent_reason``."""
    if value.get("schema") != "lab-owner-report/15":
        return []
    report = value.get("report")
    from evolution_lab._reflector_probe import probe_lines
    probe = [] if isinstance(report, dict) and report.get("operation") == "evaluation" else probe_lines(value)
    notices = probe + ([notice_line(n) for n in (report or {}).get("lab_notices") or ()] if isinstance(report, dict) else [])
    core = report.get("core") if isinstance(report, dict) else None
    if not isinstance(core, dict):
        return notices
    if report.get("operation") == "campaign" and report.get("lab_notices_state") == "unavailable":
        notices.append("Baseline provenance notices are unavailable.")
    if report.get("operation") == "evaluation":
        if report.get("lab_notices_state") == "unavailable":
            notices.append(FOCUS_UNAVAILABLE_RU)
        result = report.get("lab_result") or {}
        unmeasured = result.get("unmeasured")
        count = len(unmeasured) if isinstance(unmeasured, list) else "unknown"
        return [f"{row['coverage']['scored']} of {row['coverage']['expected']} scored "
                f"{_token(row['coverage']['state'])} unmeasured={count} "
                f"stopped_reason={_token(result.get('stopped_reason'))} "
                f"min_coverage={result.get('min_coverage', 1.0)}"
                for row in report.get("measurements") or ()] + notices
    if report.get("operation") == "campaign":
        notices = [outcome_line(core.get("campaign_outcome"))] + notices
    if report.get("operation") != "campaign":
        return notices
    if core.get("family_version") == 15:
        independent = report.get("lab_independent")
        reason = report.get("lab_independent_reason") or "unreadable"
        lines = (independent_lines(independent) if isinstance(independent, dict)
                 else [f"independent: unavailable ({_token(reason)})"])
        return notices + with_checks(lines, report.get("lab_checks"),
                                     report.get("lab_checks_state", "present"), _token)
    from evolution_lab.owner.campaign_views import gate_mode_of
    try:
        mode = gate_mode_of(core)
    except Exception:  # unreadable caveats: the gate is unknown (ADR-0019 p.3)
        mode = None
    line = gate_line(14, mode, core.get("caveats"))
    return notices + ([line] if line else [])


def notice_line(notice: dict) -> str:
    """One text line of a Lab notice (PDR-0025)."""
    if notice.get("code") == "baseline_provenance_unknown":
        from evolution_lab._notice_text import BASELINE_PROVENANCE_EN
        return BASELINE_PROVENANCE_EN + f" (round={notice['round']}, measurement_id={notice['measurement_id']}, samples={notice['samples']})"
    if notice.get("code") == "scorer_without_focus_signal":
        return FOCUS_SIGNAL_EN + f" admitted={notice['admitted']} not_completed={notice['not_completed']}"
    counts = lambda d: ", ".join(f"{_token(k)} {v}" for k, v in (d or {}).items())  # noqa: E731
    line = f"no candidate was measured: {counts(notice.get('by_reason')) or '-'}"  # zero rounds: no dangling colon
    if notice.get("reflection_errors"):
        line += f"; reflection errors: {counts(notice['reflection_errors'])}"
    if all_rounds_clean(notice):
        line += f"; {CLEAN_ROUNDS_EN}"
    return line


def campaign_keyed_lines(value: dict[str, Any]) -> list[tuple[str, str]]:
    """Core's outcome, the gate of a family-14 campaign, Lab's notices (PDR-0025), one line per round (baseline, hypotheses or
    reflection state with its error class, final) and one per attempt (outcome group, core's reason, then where it
    stopped, then the gate's last look). A family-15 view has no gate mode, final or look; its ``independent`` block
    follows the rounds. Keys identify each line independently of its text; repeated keys receive numbered suffixes
    after the complete campaign list is assembled."""
    if value.get("schema") != "lab-campaign-view/15":
        return []
    pairs = [("outcome", outcome_line(value.get("outcome")))]  # always, before the notices: they stand next to it (PDR-0025)
    from evolution_lab._reflector_probe import probe_lines
    pairs.extend(("reflector_probe", line) for line in probe_lines(value))
    gate = gate_line(value.get("family"), value.get("gate_mode"), value.get("caveats"))
    if gate:
        pairs.append(("gate", gate))
    for key, extra in (("settings", settings_line(value.get("settings"))),
                       ("connection", connection_line(value.get("connection")))):
        if extra:
            pairs.append((key, extra))
    if value.get("details_state") == "unavailable":
        pairs.append(("details", f"details unavailable: {_token(value.get('details_reason'))}"))
    if value.get("lab_notices_state") == "unavailable":
        pairs.append(("notices", "Baseline provenance notices are unavailable."))
    pairs.extend((f"notice:{_token(n.get('code'))}", notice_line(n)) for n in value.get("lab_notices") or ())
    progress = value.get("progress") or {}
    suspensions = value.get("suspensions") or ()
    if progress.get("phase") == "suspended" and suspensions:
        reason = suspensions[-1].get("reason")
        pairs.append(("suspended", f"suspended: {_token(reason)}"))
        hint = suspension_hint(reason, "en", ((value.get("connection") or {}).get("detail") or {}).get("code"))
        if hint:
            pairs.append(("suspended-hint", f"hint: {hint}"))
    for item in value.get("rounds") or ():
        reflection = item.get("reflection")
        if reflection is None:
            about = "reflection=-"
        elif reflection.get("state") == "present":
            about = f"hyp={len(reflection.get('hypotheses') or ())}"
        else:
            state = _token(reflection.get("state"))
            code = reflection.get("error_class")
            about = (f"reflection={state}({_token(code or '-')}: {_token(reflection.get('reason') or '-')})"
                     if code or reflection.get("reason") else f"reflection={state}")
        line = (f"round {_token(item.get('round'))} {_token(item.get('outcome'))} "
                f"baseline={_token(item.get('baseline_version') or '-')} {about}")
        final = item.get("final")
        if isinstance(final, list) and final:
            selection = final[-1].get("selection")
            listed = sum(entry.get("listed") or 0 for entry in final)
            if isinstance(selection, dict) and selection.get("attempt_id") is not None:
                line += f" final=selected:{_token(selection['attempt_id'])}/{_token(selection.get('reason'))}"
            elif listed:
                line += f" final=incomplete listed={_token(listed)}"
            else:
                line += f" final=none/{_token((selection or {}).get('reason') or '-')}"
        pairs.append((f"round:{_token(item.get('round'))}", line))
        for attempt in item.get("attempts") or ():
            line = (f"  {_token(attempt.get('attempt_id'))} {_token(attempt.get('group'))} "
                    f"{_token(attempt.get('reason') or '-')}")
            failure = attempt.get("failure")
            if failure is not None:
                detail = failure.get("role_reason") or failure.get("error_class") or failure.get("code") or "-"
                line += f" {_token(failure.get('stage'))}:{_token(detail)}"
            latest = (attempt.get("confidence") or {}).get("latest")
            if isinstance(latest, dict):
                focus = latest.get("focus") or {}
                line += (f" gate={_token(latest.get('decision'))}/{_token(latest.get('reason'))}"
                         f" focus={_interval_text(latest.get('focus'))}")
                if attempt.get("final_decision") is not None or (attempt.get("confidence") or {}).get("margin") is not None:
                    exact = focus.get("exact") if isinstance(focus, dict) else None
                    line += f" exact={'-' if exact is None else str(exact).lower()}"
                    line += f" margin={_number((attempt.get('confidence') or {}).get('margin'))}"
            decision = attempt.get("final_decision")
            if isinstance(decision, dict):
                line += f" decision={_token(decision.get('state'))}/{_token(decision.get('reason') or '-')}"
            pairs.append((f"attempt:{_token(item.get('round'))}:{_token(attempt.get('attempt_id'))}", line))
    if value.get("family") == 15:
        independent = _independent_pairs(value.get("independent"))
        if not independent and (value.get("checks") is not None or value.get("checks_state") == "unavailable"):
            independent = [("independent", "independent: unavailable (unreadable)")]
        lines = with_checks([line for _, line in independent], value.get("checks"),
                            value.get("checks_state", "present"), _token)
        # Preserve the established watch keys while giving check lines stable identities.
        keys = iter(key for key, _ in independent)
        for line in lines:
            key = ("checks" if line.startswith("checks:") else
                   "shadow:" + line.split(" · count ", 1)[0][len("shadow hits: "):]
                   if line.startswith("shadow hits:") else next(keys))
            pairs.append((key, line))
    return _numbered(pairs)


def _numbered(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep repeated line keys distinct, in their original order."""
    seen: dict[str, int] = {}
    numbered = []
    for key, text in pairs:
        seen[key] = seen.get(key, 0) + 1
        numbered.append((key if seen[key] == 1 else f"{key}#{seen[key]}", text))
    return numbered


def _campaign_lines(value: dict[str, Any]) -> list[str]:
    """The text of ``lab campaign`` (see ``campaign_keyed_lines``)."""
    return [text for _, text in campaign_keyed_lines(value)]


def _quantity(q: Any) -> str:
    return quantity(q, "en")


def _preflight_text(explanation: dict[str, Any]) -> str:
    """Core's preflight ceilings with Core's own ``passes`` (``_independent_text.preflight_parts``)."""
    parts = preflight_parts(explanation, "en", _token)
    head = f"  preflight {_token(explanation.get('status'))}"
    return head + (": " + "; ".join(parts) if parts else "")


def _independent_pairs(block: Any, *, operator_report: bool = False) -> list[tuple[str, str]]:
    """Family-15 settings and preflight, then each attempt's head and explicit comparison layers.

    Each present gate/global layer carries its own outcome, reason, reference and scopes; the compatibility
    ``comparison`` adds no duplicate. An attempt with neither layer shows its preflight ceilings."""
    if not isinstance(block, dict):
        return []
    settings = block.get("settings") or {}
    noise = settings.get("execution_noise") or {}
    pairs = [("independent", f"independent: mode={_token(settings.get('mode'))} R={_token(settings.get('repeats'))} "
             f"verification={_token(settings.get('verification'))} noise={_token(noise.get('kind'))} "
             f"allowance={_quantity(noise.get('allowance'))} ({_token(noise.get('source_kind'))}) "
             f"stability={_token(settings.get('stability'))} "
             f"fresh_parent={'yes' if settings.get('fresh_parent') is True else 'no'} "
              f"global={_token(settings.get('global_rule'))}")]
    if settings.get("bound_fallback") is not None:
        pairs[0] = (pairs[0][0], pairs[0][1] + f" bound_fallback={_token(settings['bound_fallback'])}")
    if block.get("evidence") != "authenticated_publications":
        pairs.append(("comparisons", f"comparisons unavailable ({_token(block.get('evidence'))})"))
    if operator_report:
        suspension = block.get("suspension")
        if suspension is not None:
            pairs.append(("suspension", f"campaign suspended · {_token(suspension['reason'])} · {_token(suspension['owner'])}"))
        for row in block.get("round_progress", ()):
            pairs.extend((f"round-progress:{row['round']}", line) for line in round_progress_text(row, "en", _token))
    for item in (block.get("attainability") or {}).get("preflight") or ():
        explanation = item.get("explanation") or {}
        pairs.append((f"preflight:{_token(item.get('round'))}",
                      f"round {_token(item.get('round'))} preflight {_token(explanation.get('status'))} "
                      f"({_token(explanation.get('basis'))})"))
    for attempt in block.get("attempts") or ():
        key = f"ind:{_token(attempt.get('round'))}:{_token(attempt.get('attempt_id'))}"
        head = (f"{_token(attempt.get('attempt_id'))} {_token(attempt.get('group'))} "
                f"{_token(attempt.get('outcome') or '-')}/{_token(attempt.get('reason') or '-')}")
        comparisons = [(layer, attempt.get(f"{layer}_comparison")) for layer in ("gate", "global")]
        present = [(layer, comparison) for layer, comparison in comparisons if isinstance(comparison, dict)]
        pairs.append((key, head if present else f"{head} · no comparison"))
        if operator_report and "progress" in attempt:
            pairs.extend((f"{key}:progress", f"  {line}")
                         for line in attempt_progress_text(attempt["progress"], "en", _token))
        if "stages" in attempt:
            pairs.append((f"{key}:stages", f"  {stages_text(attempt['stages'])}"))
        if not present and isinstance(attempt.get("preflight"), dict):
            pairs.append((f"{key}:preflight", _preflight_text(attempt["preflight"])))
        for layer in ("gate", "global"):
            binding = attempt.get(f"{layer}_binding")
            if binding is not None:
                rules = ", ".join(f"{_token(s['name'])}={_token(s['rule'])}" for s in binding["scopes"])
                pairs.append((f"{key}:{layer}:binding", f"  {layer} binding (core_raw): {rules}"))
        for layer, comparison in present:
            layer_key = f"{key}:{layer}"
            reference_id = comparison.get("reference_measurement_id")
            short_id = _token(reference_id[:12]) if reference_id else "-"
            pairs.append((layer_key,
                          f"  {layer} {_token(comparison.get('outcome'))}/{_token(comparison.get('reason'))} "
                           f"({_token(comparison.get('deciding_layer'))}) · {reference_text(comparison)} · ref {short_id}"))
            if comparison.get("calibration") is not None:
                calibration = comparison["calibration"]
                pairs.append((f"{layer_key}:calibration", f"  calibration {_token(calibration['source'])} "
                              f"{_token(calibration['method'])} level={_quantity(calibration['level'])} "
                              f"allowance={_quantity(calibration['allowance'])}"))
            if comparison.get("caveats"):
                pairs.append((f"{layer_key}:caveats", "  caveats: " + ", ".join(map(_token, comparison["caveats"]))))
            if operator_report:
                variance = repeat_variance_text(comparison.get("noise") or {}, "en", _token)
                if variance is not None:
                    pairs.append((f"{layer_key}:repeat-variance", f"  {variance}"))
            for scope in comparison.get("scopes") or ():
                text = scope_statement(scope, "en", _token)
                if attempt.get(f"{layer}_binding") is not None:
                    text = f"result rule={_token(scope['rule'])} · {text}"
                if scope.get("name") == comparison.get("deciding_layer"):
                    text += f" → {_token(comparison.get('outcome'))}"
                pairs.append((f"{layer_key}:scope:{_token(scope.get('name'))}", f"  {_token(scope.get('name'))}: {text}"))
        if attempt.get("eligible_final_score") is not None:
            pairs.append((f"{key}:eligible", f"  eligible final score {_quantity(attempt['eligible_final_score'])}"))
    return pairs


def independent_keyed_lines(block: Any) -> list[tuple[str, str]]:
    """The independent block's lines with unique keys, also used outside a campaign view."""
    return _numbered(_independent_pairs(block))


def independent_lines(block: Any) -> list[str]:
    """The text of the family-15 block in the operator report."""
    return [text for _, text in _independent_pairs(block, operator_report=True)]


def _summary_lines(value: dict[str, Any]) -> list[str]:
    """One line per operation: id kind control_state phase-or-progress-state outcome."""
    if value.get("schema") != "lab-operation-summary-list/1":
        return []
    lines = []
    for row in value.get("operations") or ():
        progress = row.get("progress")
        phase = progress.get("phase") if isinstance(progress, dict) else None
        outcome = row.get("outcome")
        lines.append(" ".join(_token(part) for part in (
            row.get("id"), row.get("kind"), row.get("control_state"), phase or "-",
            (outcome.get("outcome") if isinstance(outcome, dict) else None) or "-")))
    return lines


def _probe_ceiling_line(ceiling: dict[str, Any]) -> str:
    parts = [f"turns {ceiling['turns']['used']}/{ceiling['turns']['max']}"]
    if ceiling.get("tokens") is not None:
        parts.append(f"tokens {ceiling['tokens']['used']}/{ceiling['tokens']['max']}")
    cost = ceiling.get("cost")
    if cost is not None:
        parts.append(f"cost {cost['used']}/{cost['max']} {cost['currency']}")
    return "ceiling: " + ", ".join(parts)


def _probe_turn_lines(turn: dict[str, Any]) -> list[str]:
    """One probe turn (spec §5.2): outcome line, answer, steps, spend, ceiling left."""
    latency = turn.get("latency_ms")
    usage, cost = turn.get("usage"), turn.get("cost")
    tokens = "unknown" if usage is None or usage.get("total") is None else str(usage["total"])
    spent = "\u2014" if cost is None else f"{cost['amount']} {cost['currency']}" + (
        " (upper bound)" if cost.get("upper_bound") else "")
    steps = turn.get("steps") or []
    kinds = ", ".join(str(step["type"]) for step in steps)
    answer = turn.get("answer")
    lines = [f"turn {turn['turn_id']}  {turn['agent_outcome']}  {turn['outcome']}  "
             f"{'-' if latency is None else latency} ms",
             f"answer: {'-' if answer is None else answer}",
             f"steps: {len(steps)}" + (f" ({kinds})" if steps else ""),
             f"tokens: {tokens}  cost: {spent}"]
    if turn.get("failure") is not None:
        lines.append(f"failure: {turn['failure']['code']}")
    lines.append(_probe_ceiling_line(turn["ceiling"]))
    return lines


def _probe_session_line(row: dict[str, Any]) -> str:
    parts = [f"session {row['id']}", row["state"] + (f"/{row['closed_reason']}" if row.get("closed_reason") else ""),
             f"project={row['project']}", f"implementation={row['implementation']}"]
    if row.get("reset_of"):
        parts.append(f"reset_of={row['reset_of']}")
    return " ".join(parts)


def _probe_lines(value: dict[str, Any]) -> list[str] | None:
    schema = value.get("schema")
    if schema == "lab-probe-turn/1":
        return _probe_turn_lines(value)
    if schema == "lab-probe-session/1":
        lines = [_probe_session_line(value), _probe_ceiling_line(value["ceiling"])]
        for turn in value.get("turns") or ():
            lines.append(f"request: {turn['request']['content']}")
            lines.extend(_probe_turn_lines(turn))
        return lines
    if schema == "lab-probe-session-list/1":
        return [_probe_session_line(row) + f"  {_probe_ceiling_line(row['ceiling'])}" for row in value["sessions"]] \
            or ["no probe sessions"]
    return None


def _suspension_evidence_lines(value: dict[str, Any]) -> list[str]:
    if value.get("schema") != "lab-suspension-evidence/1":
        return []
    if value["state"] == "unavailable":
        return [f"suspensions unavailable: {_token(value['reason'])}"]
    if not value["suspensions"]:
        return ["no suspensions"]
    lines = []
    for entry in value["suspensions"]:
        lines.append("suspension " + " ".join(f"{key}={_token(entry[key])}" for key in
                     ("seq", "reason", "owner", "evidence_kind")))
        # Render every admitted metadata field, including nested threshold crossing,
        # and preserve JSON null/true/false instead of guessing absent values.
        lines.append("evidence " + " ".join(
            f"{key}={_token(json.dumps(item, ensure_ascii=False, separators=(',', ':'), allow_nan=False))}"
            for key, item in entry["evidence"].items()))
        for row in entry["rows"] or ():
            lines.append("row " + " ".join(
                f"{key}={_token('null' if item is None else item)}" for key, item in row.items()))
    return lines


def refusal_lines(value: dict[str, Any]) -> list[str]:
    """The latest start's refusal, with the same printable code and path as status output."""
    refusal = value.get("refusal")
    if not isinstance(refusal, dict):
        return []
    from evolution_lab._manifest_schema import _printable
    where = "" if refusal.get("path") is None else f" at {_printable(str(refusal['path']))}"
    return [f"refused: {_printable(str(refusal.get('code')))}{where} ({refusal.get('class')})"]


def finding_lines(record, *, command):
    """Show Core's effects and executable Lab command syntax for each finding."""
    import shlex
    from evolution_lab._manifest_schema import _printable
    lines = []
    for finding in record['findings']:
        where = '' if finding['path'] is None else ' at ' + finding['path']
        lines.append(f"finding: {finding['code']}{where} ({finding['class']})")
        if finding['instance_digest'] is not None:
            lines.append('instance_digest: ' + finding['instance_digest'])
        lines.extend('differs: ' + text for text in finding['differs'])
        effects = finding['effects']
        if effects:
            lines.append('effects: ' + ', '.join(e + (' (default)' if i == 0 else '') for i, e in enumerate(effects)))
        lines.extend(text for text in finding['effect_texts'] if text)
        if finding['class'] == 'authorizable' and finding['instance_digest'] is not None:
            for effect in effects:
                args = [*command, '--accept', finding['code'] + ':' + finding['instance_digest']]
                if finding['code'] == 'run_identity_changed' and effect in {'continue', 'rebaseline', 'close'}:
                    args += ['--effect', effect]
                lines.append('accept: ' + shlex.join([*args, '--reason', 'REASON']))
    return [_token(line) if line.startswith("accept: ") else _printable(line) for line in lines]


def core_terminal_lines(value: dict[str, Any]) -> list[str]:
    """The latest start's Core failure and its existing English repair hint."""
    terminal = value.get("core_terminal")
    if not isinstance(terminal, dict):
        return []
    line = (f"core terminal: {_token(terminal.get('terminal'))} "
            f"(owner={_token(terminal.get('owner'))}, code={_token(terminal.get('code'))})")
    hint = terminal.get("hint")
    return [line] + (["hint: " + _token(hint["en"])] if isinstance(hint, dict) and hint.get("en") else [])


def suspension_lines(value: dict[str, Any], ready_target: dict[str, Any] | None = None) -> list[str]:
    """Status's last Core suspension, its hint and how to continue (PDR-0015)."""
    result = value.get("result")
    suspension = last_suspension(result.get("core")) if isinstance(result, dict) else None
    if suspension is None:
        return []
    lines = [f"suspended: {suspension['reason']} (owner={suspension['owner']}, "
             f"evidence={suspension['evidence_kind']})"]
    hint = suspension_hint(suspension["reason"], "en",
                           ((result["core"].get("connection") or {}).get("detail") or {}).get("code"))
    if hint is not None:
        lines.append(f"hint: {hint}")
    line = continuation_line(value, ready_target)
    if line is not None:
        lines.append(line)
    return lines


def warning_lines(value: dict[str, Any]) -> list[str]:
    """One warning line per response warning, preserving detail order and nulls as ``-``."""
    lines = []
    for warning in value.get("warnings", []) if isinstance(value.get("warnings"), list) else ():
        if not isinstance(warning, dict):
            continue
        detail = warning.get("detail")
        fields = " ".join(
            f"{key}={item if item is not None else '-'}" for key, item in detail.items()
        ) if isinstance(detail, dict) else ""
        lines.append(" ".join(part for part in (f"warning: {warning.get('code')}", fields) if part))
    return lines


def write_value(stream: TextIO, value: dict[str, Any], *, machine: bool, ready_target: dict[str, Any] | None = None) -> None:
    if machine:
        write_json(stream, value)
        return
    if value.get('schema') == 'lab-trace-summary/1':
        stream.write('Trace: ' + ', '.join(f"{value[key]} {key.replace('_', ' ')}" for key in
            ('tries', 'scored', 'failed', 'unavailable', 'unverified', 'in_flight', 'unset_secrets')) + '\n')
        stream.flush()
        return
    if value.get("schema") == "lab-approval-list/1":
        for record in value["approvals"]:
            resource = record["resource"]
            start = "" if record["start"] is None else f" start={record['start']}"
            stream.write(f"{record['created_at']} {record['tool']} via={record['via']} "
                         f"autonomy={record['autonomy']} {resource['kind']}:{resource['id']}"
                         f"{start} {record['outcome']}\n")
        stream.flush()
        return
    if value.get("schema") == "lab-bundle-measurements/1":
        stream.write(f"bundle={_token(value['bundle_digest'])} "
                     f"measurement_basis={_token(value['measurement_basis'])}\n")
        for item in value['items']:
            outcome = item['outcome'] or {}
            coverage = outcome.get('coverage')
            measured = (f"{coverage['scored']}/{coverage['expected']}"
                        if coverage is not None else 'unknown')
            fields = " ".join(f"{key}={_token(item[key])}" for key in (
                'operation_id', 'prepared_digest', 'bundle_digest', 'control_state', 'results_state'))
            stream.write(fields + f" same_bundle={str(item['same_bundle']).lower()} "
                         f"complete={json.dumps(outcome.get('complete'))} coverage={measured}\n")
            stream.write("plan " + " ".join(
                f"{key}={_token(json.dumps(content, separators=(',', ':'), ensure_ascii=False))}"
                for key, content in item['plan'].items()) + "\n")
        stream.flush()
        return
    if value.get("schema") == "lab-operation-routing/1":
        for run in value['runs']:
            stream.write("run " + " ".join(f"{key}={_token(run[key])}" for key in (
                'start', 'policy_digest', 'author', 'channel', 'confirmed_at', 'lab_version')) + "\n")
        stream.write(f"routing shared={_token(value['shared'])} outside={_token(value['outside'])}\n")
        if not value["available"]:
            stream.write(f"routing unavailable reason={_token(value['reason'])}\n")
        else:
            for role, rows in value["roles"].items():
                for row in rows:
                    stream.write(" ".join([_token(role)] + [
                        f"{key}={_token(row[key])}" for key in ("endpoint", "upstream_model", "client_model",
                                                              "requested", "reported_provider", "calls", "mismatched")]) + "\n")
        stream.flush()
        return
    probe = _probe_lines(value)
    if probe is not None:
        stream.write("\n".join(probe) + "\n")
        stream.flush()
        return
    schema = value.get("schema", "response")
    identifier = value.get("id", value.get("digest", value.get("operation_id", "")))
    state = value.get("control_state", value.get("state", ""))
    process = value.get("process")
    cleanup = value.get("target_cleanup", value.get("cleanup"))
    summary = " ".join(part for part in (
        str(schema), str(identifier), str(state),
        f"process={_process_summary(process)}" if isinstance(process, dict) else "",
        f"cleanup={cleanup.get('state')}" if isinstance(cleanup, dict) else "",
        f"available={value.get('state')}" if "availability" in value else "",
    ) if part)
    stream.write(summary + "\n")
    result = value.get("result")
    if value.get("kind") == "evaluation" and isinstance(result, dict):
        coverage = result.get("coverage")
        if isinstance(coverage, dict):
            stream.write(f"{coverage['scored']} of {coverage['expected']} scored ")
        unmeasured = result.get("unmeasured")
        count = len(unmeasured) if isinstance(unmeasured, list) else "unknown"
        stopped = result.get("stopped_reason", result.get("stop_reason"))
        from types import SimpleNamespace
        from evolution_lab.projects.facts import baseline_failure
        accepted = baseline_failure(SimpleNamespace(kind="evaluation", result=result,
                                    control_state=value.get("control_state"))) is None
        if accepted:
            stream.write("accepted ")
        stream.write(f"unmeasured={count} stopped_reason={_token(stopped)} "
                     f"min_coverage={result.get('min_coverage', 1.0)}\n")
    if "progress_state" in value:
        line = _progress_line(value)
        if line is not None:
            stream.write(line + "\n")
    auto_resume = value.get('auto_resume')
    if isinstance(auto_resume, dict):
        line = f"auto-resume: {_token(auto_resume.get('state'))} used={_token(auto_resume.get('used'))}/{_token(auto_resume.get('max') if auto_resume.get('max') is not None else '-')}"
        if auto_resume.get('next_at') is not None:
            line += f" next={_token(auto_resume['next_at'])}"
        blocked = auto_resume.get('blocked')
        if isinstance(blocked, dict):
            line += f" blocked={_token(blocked.get('code'))}"
        detail = auto_resume.get('detail')
        if isinstance(detail, dict) and isinstance(detail.get('busy'), dict):
            line += f" busy={_token(detail['busy'].get('code'))}"
        stream.write(line + '\n')
    extra = _project_summary_extra(value)
    if extra is not None:
        stream.write(extra + "\n")
    for line in _project_detail_lines(value) + _suspension_evidence_lines(value) + _content_lines(value) + _measurement_lines(value) + _campaign_lines(value) + _report_lines(value) + _summary_lines(value):
        stream.write(line + "\n")
    for line in refusal_lines(value) + core_terminal_lines(value) + suspension_lines(value, ready_target) + warning_lines(value):
        stream.write(line + "\n")
    stream.flush()
