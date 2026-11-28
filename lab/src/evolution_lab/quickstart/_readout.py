"""Run files and the readout of ``lab evaluate`` (spec §3.4).

``write_run_files`` reads the finished operation through Owner and writes what a
person opens after a run; ``render_readout`` is the text ``lab evaluate``
prints. The read side follows ``examples/owner/demo_readout.py``. No file Lab
writes holds a known secret value: every one is replaced by ``<secret:NAME>``.
What the agent or the scorer emits itself stays as the core and Owner recorded
it in the pair state (ADR-0032 p. 4, amended 2026-11-09).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from evolution_lab.quickstart._errors import QuickstartError
from evolution_lab.quickstart._resolve import Resolution

LATEST = "latest"
_NONE = "—"


def _texts(sample: Mapping[str, Any], pick) -> str:
    return "\n".join(text for turn in sample.get("turns") or () for text in pick(turn)) or _NONE


def _question(sample: Mapping[str, Any]) -> str:
    return _texts(sample, lambda turn: [turn["message"]["content"]] if turn.get("message") else [])


def _answer(sample: Mapping[str, Any]) -> str:
    return _texts(sample, lambda turn: [event.get("content", "") for event in turn.get("events") or ()
                                        if event.get("type") == "assistant_message"])


def _score(sample: Mapping[str, Any]) -> str:
    metrics = (sample.get("scoring") or {}).get("metrics") or {}
    return ", ".join(f"{name} {value:g}" for name, value in metrics.items()) or "not scored"


def _verdicts(sample: Mapping[str, Any]) -> str:
    items = (sample.get("scoring") or {}).get("verdicts") or []
    return ", ".join(f"{v['criterion']} {'passed' if v['passed'] else 'failed'}" for v in items) or _NONE


def _label(measurement: Mapping[str, Any]) -> str:
    parts = [measurement["role"]]
    if measurement.get("round") is not None:
        parts.append(f"round {measurement['round']}")
    if measurement.get("attempt_id"):
        parts.append(f"attempt {measurement['attempt_id']}")
    return f"{measurement['measurement_id']} ({', '.join(parts)})"


def _inline(text: str) -> str:
    return " ".join(text.split()) or _NONE


def fenced(text: str, info: str = "text") -> list[str]:
    """``text`` as a Markdown code block whose fence is longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + info, *text.split("\n"), fence]


def unscored_label(sample: Mapping[str, Any] | None) -> str:
    """Why a sample has no score, in the words the readout uses: ``scorer failed``, ``target failed``."""
    sample = sample or {}
    outcome = sample.get("outcome")
    if outcome == "scorer_failed" or sample.get("scoring_state") == "failed":
        return "scorer failed"
    if outcome == "target_failed":
        return "target failed"
    if isinstance(outcome, str) and outcome:
        return "not scored: " + outcome.replace("_", " ")
    return "not scored"


def _exit_status(sample: Mapping[str, Any]) -> Any:
    output = (sample.get("private") or {}).get("scorer_output")
    return output.get("exit_status") if isinstance(output, Mapping) else None


async def read_conversations(owner, ref, states: Mapping[tuple[str, str], Mapping[str, Any]] | None = None
                             ) -> tuple[str, list[dict[str, str]]]:
    """``conversations.md`` and one row per measured sample (label, case, sample, question, answer, score).

    ``states`` (``summary.samples`` by case and sample id) names why a sample has no score."""
    states = states or {}
    lines = ["# Evaluation conversations", "",
             "Every sample Lab measured: the question the core sent, the agent's answer, "
             "and the scorer's score and verdicts.", ""]
    rows: list[dict[str, str]] = []
    for measurement in (await owner.measurements(ref))["measurements"]:
        measurement_id = measurement["measurement_id"]
        detail = (await owner.measurement(ref, measurement_id))["measurement"]
        mean = ", ".join(f"{name} {value['mean']:g}" for name, value in (detail.get("metrics") or {}).items())
        label = _label(detail)
        lines += [f"## {label}", "", f"Mean: {mean or _NONE}", ""]
        for row in detail.get("samples") or ():
            case_id, sample_id = str(row.get("case_id")), str(row.get("sample_id"))
            lines += [f"### case {case_id}, sample {sample_id}", ""]
            try:
                sample = await owner.sample(ref, measurement_id, row["case_id"], row["sample_id"], disclose=True)
                question, answer, score = _question(sample), _answer(sample), _score(sample)
                verdicts = _verdicts(sample)
                if score == "not scored":
                    score = unscored_label(states.get((case_id, sample_id)) or
                                           {"scoring_state": (sample.get("scoring") or {}).get("state")})
                    status = _exit_status(sample)
                    detail = f"{score} (exit status {status})" if score == "scorer failed" and status else score
                else:
                    detail = score
            except Exception as error:  # one unreadable sample must not drop the others
                code = getattr(error, "code", None) or type(error).__name__
                lines += [f"- sample unreadable: {code}", ""]
                rows.append({"measurement": label, "case_id": case_id, "sample_id": sample_id,
                             "question": f"sample unreadable: {code}", "answer": _NONE, "score": "unreadable"})
                continue
            lines += ["- question:", "", *fenced(question), "",
                      "- answer:", "", *fenced(answer), "",
                      f"- {detail}; scorer verdicts: {verdicts}", ""]
            rows.append({"measurement": label, "case_id": case_id, "sample_id": sample_id,
                         "question": question, "answer": answer, "score": score})
    return "\n".join(lines), rows


def _secret_pairs(secrets: Mapping[str, str]) -> list[tuple[str, str]]:
    return sorted(((v, n) for n, v in secrets.items() if v), key=lambda p: -len(p[0]))


def _redact_text(text: str, pairs: Sequence[tuple[str, str]]) -> str:
    for secret, name in pairs:
        text = text.replace(secret, f"<secret:{name}>")
    return text


def redact(value: Any, secrets: Mapping[str, str]) -> Any:
    """A copy of ``value`` with every known secret value in its strings replaced by ``<secret:NAME>``.

    Only string values are rewritten, recursively; dict keys are structure and stay as they are."""
    pairs = _secret_pairs(secrets)
    if not pairs:
        return value

    def walk(item):
        if isinstance(item, str):
            return _redact_text(item, pairs)
        if isinstance(item, list):
            return [walk(x) for x in item]
        if isinstance(item, tuple):
            return tuple(walk(x) for x in item)
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        return item

    return walk(value)


def redact_document(document: Any, schema: Mapping[str, Any], secrets: Mapping[str, str],
                    node: Mapping[str, Any] | None = None) -> Any:
    """A sanitized copy of a document of ``schema`` (``summary.json``, ``resolved.json``).

    Only the string values the schema leaves free are rewritten. Dict keys, and values the schema owns (a
    ``const``, an ``enum`` or a ``pattern`` such as a sha256), are kept, so the copy still matches the schema."""
    pairs = _secret_pairs(secrets)
    if not pairs:
        return document

    def resolve(item: Any) -> Mapping[str, Any]:
        while isinstance(item, Mapping) and isinstance(item.get("$ref"), str) and item["$ref"].startswith("#/"):
            target: Any = schema
            for part in item["$ref"][2:].split("/"):
                target = target[part]
            item = {**target, **{key: value for key, value in item.items() if key != "$ref"}}
        return item if isinstance(item, Mapping) else {}

    def walk(item: Any, here: Any) -> Any:
        here = resolve(here)
        if isinstance(item, str):
            owned = "const" in here or "enum" in here or "pattern" in here
            return item if owned else _redact_text(item, pairs)
        if isinstance(item, dict):
            properties = here.get("properties") or {}
            extra = here.get("additionalProperties")
            return {key: walk(value, properties.get(key, extra if isinstance(extra, Mapping) else {}))
                    for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(value, here.get("items") or {}) for value in item]
        return item

    return walk(document, schema if node is None else node)


def redact_summary(summary: Mapping[str, Any], secrets: Mapping[str, str]) -> dict[str, Any]:
    """A sanitized copy of a ``lab-evaluate-summary/1`` document; the original is left as it is."""
    from evolution_lab.quickstart._evaluate import summary_schema

    return redact_document(dict(summary), summary_schema(), secrets)


def _set_latest(run_dir: Path) -> None:
    """``DIR/.lab/latest`` -> ``runs/<NNN>``: a relative symlink, or a text file where one cannot be made."""
    latest = run_dir.parent.parent / LATEST
    target = f"runs/{run_dir.name}"
    with contextlib.suppress(FileNotFoundError):
        latest.unlink()
    try:
        os.symlink(target, latest)
    except OSError:
        latest.write_text(target + "\n", encoding="utf-8")


def _validated(summary: Mapping[str, Any]) -> None:
    import jsonschema

    from evolution_lab.quickstart._evaluate import summary_schema

    try:
        jsonschema.Draft202012Validator(summary_schema()).validate(dict(summary))
    except jsonschema.ValidationError as error:
        raise QuickstartError("summary_invalid", f"summary.json does not match lab-evaluate-summary/1: "
                              f"{error.message}", exit_code=3) from None


async def write_run_files(owner, operation_ref, run_dir: Path, resolution: Resolution | None,
                          summary: dict[str, Any], *, log: Sequence[str] = (),
                          secrets: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Write ``report.md``, ``conversations.md``, ``resolved.json``, ``summary.json`` and ``evaluate.log`` into
    ``run_dir`` and point ``.lab/latest`` at it. Without an operation (conformance failed first) the report and
    the conversations are skipped. A failed read of the report or the conversations is noted in the log and
    never stops the base files. Known secret values are replaced by ``<secret:NAME>`` everywhere. Returns the
    sample rows the readout shows."""
    secrets = secrets or {}
    log = list(log)
    rows: list[dict[str, str]] = []
    if operation_ref is not None:
        states = {(str(item.get("case_id")), str(item.get("sample_id"))): item
                  for item in summary.get("samples") or ()}
        try:
            report = await owner.report_markdown(operation_ref)
            if report.state == "available":
                (run_dir / "report.md").write_text(redact(report.markdown, secrets), encoding="utf-8")
        except Exception as error:
            log.append(f"run files: report.md not written: {getattr(error, 'code', None) or type(error).__name__}")
        try:
            text, rows = await read_conversations(owner, operation_ref, states)
            (run_dir / "conversations.md").write_text(redact(text, secrets), encoding="utf-8")
            rows = redact(rows, secrets)
        except Exception as error:
            rows = []
            log.append("run files: conversations.md not written: "
                       f"{getattr(error, 'code', None) or type(error).__name__}")
    unreadable = sum(1 for row in rows if row["score"] == "unreadable")
    if unreadable:
        log.append(f"run files: {unreadable} sample(s) unreadable, see conversations.md")
    resolved = resolution.payload() if resolution is not None else summary.get("resolved")
    if resolved is not None and secrets:
        from evolution_lab.quickstart._evaluate import summary_schema
        schema = summary_schema()
        resolved = redact_document(resolved, schema, secrets, schema["properties"]["resolved"])
    (run_dir / "resolved.json").write_text(json.dumps(resolved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "evaluate.log").write_text("".join(f"{line}\n" for line in redact(log, secrets)), encoding="utf-8")
    _set_latest(run_dir)
    _validated(summary)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows


# --- the printed text ---------------------------------------------------------------------------------------

def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _steps(summary: Mapping[str, Any], run_dir: Path) -> list[str]:
    """The steps this run took, from the facts in ``summary`` only: a step that did not run is not named."""
    steps = [f"Lab copied the project into {run_dir / 'source'} and filled in the fields marked lab:auto."]
    prepared = summary.get("prepared")
    if prepared is None:
        steps.append("Lab tried to freeze the copy into a Bundle; preparation refused it.")
        return steps
    steps.append("Lab froze the copy into a Bundle, an immutable copy of the inputs, in the state directory.")
    conformance, evaluation = summary.get("conformance"), summary.get("evaluation")
    if conformance is not None:
        if conformance.get("core_ok") is True:
            outcome = "it passed"
        elif conformance.get("core_ok") is False:
            failed = ", ".join(conformance.get("failed_checks") or ())
            outcome = "it failed" + (f" ({failed})" if failed else "")
        else:
            outcome = f"it did not finish ({conformance.get('state')})"
        steps.append("Lab started the agent as a target and ran conformance, the core's check that it speaks "
                     f"the turn protocol: {outcome}.")
        if conformance.get("core_ok") is not True:
            return steps
    elif summary.get("conformance_declared") is False:
        steps.append("launch.json declares no conformance, so Lab skipped it.")
    elif evaluation is None:
        steps.append("The run stopped before the conformance check.")
        return steps
    if evaluation is None:
        steps.append("The run stopped before the evaluation.")
        return steps
    steps.append(f"Lab ran the evaluation on a fresh target: {_count(prepared['case_count'], 'case')} x "
                 f"{_count(prepared['repeat_count'], 'repeat')}, and sent the agent's answers to the scorer.")
    final = summary.get("final_target")
    if final is not None:
        if final.get("state") == "stopped" and not summary.get("blocker_count"):
            steps.append("Lab stopped the target and checked that nothing was left running.")
        else:
            steps.append(f"Lab stopped the target: it is {final.get('state')}, "
                         f"{_count(summary.get('blocker_count') or 0, 'blocker')} left.")
    return steps


# Plain words for the fields "lab:auto" may stand in (ADR-0032), in the order the readout names them.
_FILLED_WORDS = ("environment digest", "port", "scorer Python")
_IDENTITY_FIELDS = ("contract_digest", "settings_hash", "runtime_ref_digest")


def filled_line(substituted: Mapping[str, Any], sources: Mapping[str, str] | None = None) -> str:
    """``Lab filled: environment digest, port, scorer Python, 3 identity digests (...)``: what the
    substituted fields are, in plain words; a field of the same kind in both files is named once.

    A field Lab filled from the explicit value of its paired field (``sources`` ``paired:<field>``) is
    counted the same way, and the line says how many were copied so."""
    words: set[str] = set()
    identity: set[str] = set()
    other: list[str] = []
    for name in sorted(substituted):
        path = name.split(":", 1)[-1]
        last = path.rsplit(".", 1)[-1]
        if path == "environment_ref":
            words.add("environment digest")
        elif last == "base_url":
            words.add("port")
        elif path == "scorer.argv.0":
            words.add("scorer Python")
        elif last in _IDENTITY_FIELDS:
            identity.add(last)
        else:
            other.append(name)
    named = [word for word in _FILLED_WORDS if word in words]
    if identity:
        named.append(_count(len(identity), "identity digest"))
    paired = sum(1 for name in substituted if str((sources or {}).get(name, "")).startswith("paired:"))
    copied = "" if not paired else f"; {paired} copied from the paired field you set"
    return f"Lab filled: {', '.join(named + other)} (listed in resolved.json{copied}); nothing to do."


def _quality_text(quality: Mapping[str, Any] | None, *, scored_samples: bool = False) -> str:
    """``mean score 0.40 (2 of 5 scored 1)``: the quality next to the acceptance, never inferred from coverage."""
    if not quality or quality.get("mean") is None:
        return "mean score unavailable"
    text = f"mean {quality.get('metric') or 'score'} {quality['mean']:.2f}"
    if scored_samples:
        text += f" of the {_count(quality['scored'], 'scored sample')}"
    if quality.get("full_score_count") is not None:
        text += f" ({quality['full_score_count']} of {quality['scored']} scored 1)"
    return text


def _scorer_traces(summary: Mapping[str, Any]) -> list[Path]:
    """Where the core recorded the scorer's output for this run's evaluation (the state layout, read only)."""
    operation = (summary.get("evaluation") or {}).get("operation_id")
    state = summary.get("state")
    if not operation or not state:
        return []
    with contextlib.suppress(OSError):
        return sorted((Path(state) / "artifacts" / operation).glob("*/measurement/scorer-traces/*.pack"))
    return []


def scorer_name(run_dir: Path) -> str:
    """``the scorer (scorer/score.py)``: the script from the run copy's ``scorer.cwd`` and ``scorer.argv``,
    or ``the scorer`` when the command names no script."""
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, KeyError):
        scorer = json.loads((Path(run_dir) / "source" / "evaluation-input.json").read_text(encoding="utf-8"))["scorer"]
        argv, cwd = scorer.get("argv"), scorer.get("cwd")
        script = argv[1] if isinstance(argv, list) and len(argv) > 1 else None
        if isinstance(script, str) and script and not script.startswith("-"):
            path = Path(cwd) / script if isinstance(cwd, str) and cwd else Path(script)
            return f"the scorer ({path.as_posix()})"
    return "the scorer"


def _scorer_line(summary: Mapping[str, Any], run_dir: Path) -> str | None:
    samples = summary.get("samples") or []
    failed = sum(1 for sample in samples if unscored_label(sample) == "scorer failed")
    if not failed:
        return None
    line = (f"The scorer failed on {failed} of {_count(len(samples), 'sample')}: the agent answered, "
            f"but {scorer_name(run_dir)} did not return a score. conversations.md has its exit status")
    traces = _scorer_traces(summary)
    return line + (f"; its stderr is recorded in {traces[-1]}." if traces else ".")


def render_readout(summary: Mapping[str, Any], run_dir: Path, *,
                   rows: Sequence[Mapping[str, str]] | None = None, agent_stderr: Any = None,
                   ownership_line: str | None = None) -> str:
    """The text ``lab evaluate`` prints for the human form.

    ``agent_stderr`` (``EvaluateResult.agent_stderr``) adds the last lines the target process wrote, with
    the path of its full log: a traceback of an adapter that does not import is only there."""
    run_dir = Path(run_dir)
    out: list[str] = []
    measured = summary.get("evaluation") is not None
    out.append("What happened: Lab measured your agent as it is, without changing it." if measured
               else "What happened:")
    out += [f"  {number}. {step}" for number, step in enumerate(_steps(summary, run_dir), start=1)]
    out.append("")

    samples = summary.get("samples") or []
    states = {(str(sample.get("case_id")), str(sample.get("sample_id"))): sample for sample in samples}
    if not (run_dir / "conversations.md").exists() and measured:
        out.append("conversations.md was not written; see evaluate.log")
    if rows:
        out.append("Samples: case, question -> the agent's answer [score]")
        shown = None
        for row in rows:
            if row["measurement"] != shown:
                shown = row["measurement"]
                out.append(f"  {shown}")
            score = row["score"]
            if score == "not scored":
                score = unscored_label(states.get((row["case_id"], row["sample_id"])))
            out.append(f"    {row['case_id']}  {_inline(row['question'])} -> {_inline(row['answer'])}   "
                       f"[{score}]")
        out.append("")
    elif samples:
        out.append("Samples: case [score]")
        for sample in samples:
            score = unscored_label(sample) if sample["score"] is None else f"{sample['score']:g}"
            out.append(f"    {sample['case_id']}  [{score}]")
        out.append("")
    scorer = _scorer_line(summary, run_dir)
    if scorer is not None:
        out += [scorer, ""]

    resolved = summary.get("resolved") or {}
    if resolved.get("substituted"):
        out.append(filled_line(resolved["substituted"], resolved.get("sources")))
    leftovers = summary.get("cleaned_leftovers") or []
    if leftovers:
        runs = "run" if len(leftovers) == 1 else "runs"
        out.append(f"Cleaned up after {runs} " + ", ".join(f"{number:03d}" for number in leftovers))

    from evolution_lab._notice_text import EVALUATION_PROVENANCE_NOTICES

    notice = (summary.get("provenance") or {}).get("notice")
    if notice in EVALUATION_PROVENANCE_NOTICES:
        out.append(EVALUATION_PROVENANCE_NOTICES[notice])

    if summary.get("exit_code") == 0:
        evaluation = summary["evaluation"]["coverage"]
        out.append(f"run {summary['run']:03d}: measurement accepted: "
                   f"{evaluation['scored']}/{evaluation['expected']} samples scored; "
                   f"{_quality_text(summary.get('quality'))}")
    else:
        stopped = not measured and summary.get("exit_code") in (2, 3)
        out.append(f"{'Run stopped' if stopped else 'Measurement not accepted'}: {summary.get('reason')}")
        coverage = (summary.get("evaluation") or {}).get("coverage")
        if coverage is not None:
            out.append(f"  scored {coverage['scored']}/{coverage['expected']}"
                       f", stop_reason {(summary['evaluation'].get('stopped_reason')) or 'none'}")
        quality = summary.get("quality")
        if quality is not None and quality.get("mean") is not None:
            out.append("  " + _quality_text(quality, scored_samples=True))
        out.append(f"  exit code {summary.get('exit_code')}")
    out.append("")

    if agent_stderr is not None:
        out.append(f"The agent's process wrote (last lines of {agent_stderr.path}):")
        out.extend(f"    {line}" for line in agent_stderr.lines)
        out.append("")

    files = [("report.md", "Lab's report of the run"),
             ("conversations.md", "every sample: question, answer, score, scorer verdicts"),
             ("resolved.json", "the fields Lab filled in, the Python and the wheels"),
             ("summary.json", "this result as JSON (lab-evaluate-summary/1)"),
             ("evaluate.log", "the steps of the run"),
             ("source/", "the project copy this run measured")]
    out.append(f"Run files in {run_dir}:")
    present = [(name, what) for name, what in files if (run_dir / name.rstrip("/")).exists()]
    for name, what in present:
        out.append(f"  {name:<17} {what}")
    if (run_dir / "conversations.md").exists():
        first = run_dir / "conversations.md"
    elif agent_stderr is not None:
        first = agent_stderr.path
    else:
        first = run_dir / "evaluate.log"
    out.append("")
    if ownership_line is not None:
        out.extend([ownership_line, ''])
    out.append(f"Open first: {first}")
    return "\n".join(out) + "\n"


def _baseline_choice_line(baseline: Mapping[str, Any]) -> str:
    source = baseline["source"]
    if source == "evaluation":
        run = baseline["run"] or baseline["operation_id"]
        if baseline.get("abandoned"):
            return f"Baseline import was planned from evaluation run {run}."
        return f"Baseline from evaluation run {run}."
    if source == "linked":
        return f"Baseline inherited from campaign {baseline['operation_id']}."
    reason = {
        "requested": "by request",
        "manifest_15": "manifest /15 can diagnose imported verdict-only failures only after the first baseline refresh (#208)",
        "no_scorer_traces": "the latest evaluation has no readable scorer traces",
        "no_evaluation": "the project has no eligible evaluation",
    }.get(baseline["reason"])
    return "Baseline measured in full." if reason is None else f"Baseline measured in full ({reason})."


async def write_campaign_files(owner, operation_ref, run_dir: Path, summary: dict[str, Any], *,
                               log: Sequence[str] = (), secrets: Mapping[str, str] | None = None) -> None:
    """Write the campaign's own closed summary and the existing Owner report/diff, with secret redaction."""
    import jsonschema
    from evolution_lab.quickstart._campaign import summary_schema

    secrets = secrets or {}
    report, difference = "# Campaign\n\nThe campaign did not start.\n", ""
    if operation_ref is not None:
        row = summary["campaign"]
        record = await owner.refusal(operation_ref)
        if record is not None:
            (run_dir / 'refusal.json').write_text(json.dumps(record.to_payload(), indent=2) + '\n', encoding='utf-8')
        history = await owner.inputs(operation_ref)
        (run_dir / 'inputs.json').write_text(json.dumps(history, indent=2) + '\n', encoding='utf-8')
        status = (await owner.status(operation_ref)).to_payload()
        core = (status.get('result') or {}).get('core') or {}
        block = core.get("baseline_import")
        if block is not None and "baseline" in summary:
            from evolution_lab.owner.campaign_views import baseline_import_fields
            summary["baseline"].update(baseline_import_fields(block))
        notices = {'schema': 'lab-campaign-notices/1', 'operation_id': operation_ref.id,
                   'caveats': core.get('caveats', [])}
        (run_dir / 'core-notices.json').write_text(json.dumps(notices, indent=2) + '\n', encoding='utf-8')
        rendered = await owner.report_markdown(operation_ref)
        row["report_state"] = rendered.state
        report = rendered.markdown if rendered.state == "available" else "# Campaign\n\nReport unavailable.\n"
        try:
            diff = await owner.diff(operation_ref)
            row["diff_state"] = diff.state
            difference = diff.text
            if diff.nontext_changes:
                difference += "\n".join(f"# {item['kind']}: {item['path']}" for item in diff.nontext_changes) + "\n"
        except Exception as error:
            row["diff_state"] = "unavailable"
            difference = f"# Diff unavailable: {getattr(error, 'code', type(error).__name__)}\n"
    if "baseline" in summary and operation_ref is not None:
        title, _, body = report.partition("\n")
        report = title + "\n\n" + _baseline_choice_line(summary["baseline"]) + "\n" + body
    schema = summary_schema()
    safe = redact_document(summary, schema, secrets)
    try:
        jsonschema.Draft202012Validator(schema).validate(safe)
    except jsonschema.ValidationError as error:
        raise QuickstartError("summary_invalid", f"summary.json does not match lab-campaign-summary/1: {error.message}",
                              exit_code=3, state=summary["state"], run_dir=str(run_dir)) from None
    for name, text in (("report.md", report), ("changes.diff", difference),
                       ("campaign.log", "\n".join([*log, f"result: exit {safe['exit_code']}"]) + "\n")):
        (run_dir / name).write_text(redact(text, secrets), encoding="utf-8")
    (run_dir / "resolved.json").write_text(json.dumps(safe["resolved"], indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")
    _set_latest(run_dir)


def render_campaign_readout(summary: Mapping[str, Any], run_dir: Path, *,
                            ownership_line: str | None = None) -> str:
    row = summary.get("campaign") or {}
    outcome = "suspended" if row.get("suspended") else row.get("outcome") or "not started"
    reason = row.get("suspension_reason") or row.get("reason") or summary.get("reason") or "no reason"
    extra = []
    baseline = summary.get("baseline")
    if baseline is not None:
        extra.append(_baseline_choice_line(baseline))
        if baseline.get("used_saved_summary"):
            from evolution_lab.quickstart._baseline_source import SAVED_STATE_WARNING

            extra.append(SAVED_STATE_WARNING)
        if baseline.get("abandoned"):
            extra.append("Core abandoned the import. The campaign measures the full baseline.")
        if baseline["imported"] is not None:
            extra.append(f"Imported {baseline['imported']} pairs with {baseline['to_measure']} to measure.")
            counts = ", ".join(f"{name}={count}" for name, count in baseline["not_imported"].items())
            extra.append(f"Source rows outside the import — {counts}.")
        if baseline["fields"]:
            extra.append("Condition differences — " + ", ".join(baseline["fields"]) + ".")
        for name, label in (
            ("authorization_id", "Accepted compatibility decision"),
            ("held_out_authorization_id", "Accepted held-out decision"),
        ):
            if baseline[name] is not None:
                extra.append(f"{label} {baseline[name]}.")
        if baseline["source"] == "fresh" and baseline["reason"] != "requested":
            extra.append("To select an evaluation explicitly, use --baseline-from RUN.")
    refusal_code = (row.get("refusal") or {}).get("code")
    if refusal_code == "baseline_source_unavailable":
        import shlex

        command = shlex.split(summary["next_command"])
        if command[-2:] == ["--baseline", "fresh"]:
            alternate = shlex.join([*command[:-2], "--baseline-from", "RUN"])
            extra.append(f"To select a different evaluation, run {alternate}")
    elif refusal_code == "baseline_import_inputs_changed":
        extra.append("Rerun the command once the seed tree, scorer and metric plan are stable.")
    if (run_dir / 'core-notices.json').is_file():
        notices = json.loads((run_dir / 'core-notices.json').read_text())
        extra.extend('Core caveat: ' + item['kind'] for item in notices['caveats'])
    if (run_dir / 'inputs.json').is_file():
        history = json.loads((run_dir / 'inputs.json').read_text())
        if len({item['prepared_digest'] for item in history['starts']}) > 1:
            extra.extend(f"start {item['start']}: prepared {item['prepared_digest']} bundle {item['bundle_digest']}"
                         for item in history['starts'])
    if (run_dir / 'refusal.json').is_file():
        import shlex
        from evolution_lab.cli.output import finding_lines
        record = json.loads((run_dir / 'refusal.json').read_text())
        extra.extend(finding_lines(record, command=shlex.split(summary['next_command'])))
    if ownership_line is not None:
        extra.append(ownership_line)
    return "\n".join([
        f"Campaign: {outcome} ({reason})",
        f"Champion: {row.get('final_champion') or 'unavailable'}",
        f"Diff: {row.get('diff_state') or 'unavailable'} — {run_dir / 'changes.diff'}", "",
        "Open first:", f"  {run_dir / 'report.md'}", f"  {run_dir / 'summary.json'}",
        f"  {run_dir / 'campaign.log'}", "",
        *extra, "Next:", f"  {summary['next_command']}",
        f"exit code {summary['exit_code']}", "",
    ])
