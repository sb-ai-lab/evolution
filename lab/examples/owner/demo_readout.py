"""Check an example run and write what it did into readable files. `just demo-campaign` calls it.

    python demo_readout.py WORKSPACE

WORKSPACE holds what the campaign example made: `source/` (the agent project), `state/` (Lab's state) and
`summary.json` (the example's printed summary). This script checks the summary, reads the one
campaign in `state/` through the Python Owner, and writes into WORKSPACE:

- report.md         Lab's report of the run (`Owner.report_markdown`);
- conversations.md  every measured sample: the question, the agent's answer, the score and the scorer's verdicts;
- changes.diff      what the campaign changed in the agent (`Owner.diff`);
- export.zip        Lab's export of the champion (`Owner.published_export`).

Then it prints what happened, the samples and where to look. The example data is synthetic; for a
real agent these files hold private corpus rows and conversations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from evolution_lab.owner import Owner, ResourceRef
from evolution_lab._ownership import operator_line, select_mode

_CAMPAIGN = "lab-neutral-campaign-example/1"


def _checks(summary: dict) -> list[tuple[str, object, object]]:
    return [
        ("check", summary["check"], "succeeded"),
        ("campaign.outcome", summary["campaign"]["outcome"], "completed"),
        ("campaign.rounds", summary["campaign"]["rounds"], ["promoted", "clean"]),
        ("campaign.final_champion", summary["campaign"]["final_champion"], "v1"),
        ("diff.state", summary["diff"]["state"], "changed"),
        ("export.state", summary["export"]["state"], "succeeded"),
        ("final_target.state", summary["final_target"]["state"], "stopped"),
        ("blocker_count", summary["blocker_count"], 0),
    ]


_HAPPENED = {
    _CAMPAIGN: """\
What happened: Evolution Core improved an agent; Lab prepared, ran and measured it.
  1. The example built the ordinary project skeleton in source/ with an observe gate.
     Its synthetic agent answers correctly after the sample edit to seed/skills/SKILL.md.
  2. Lab froze the project into a Bundle in state/, started the agent as a target and
     checked that it speaks the turn protocol.
  3. Round 1: Core measured the seed (v0, score 0). The editor (source/editor/edit.py)
     rewrote the first line of SKILL.md, the candidate scored 1, and Core promoted it
     to champion v1.
  4. Round 2: nothing was left to improve, so the campaign ended (rounds_exhausted).
  5. Lab read the change v0 -> v1 (changes.diff) and exported the champion (export.zip).""",
}

_NOTE = {
    _CAMPAIGN: """\
The gate ran in observe mode: the promotion shows that the wiring works, not that the
change is better. Real campaigns enforce the gate (../docs/guide/campaign.md).""",
}


def _label(measurement: dict) -> str:
    parts = [measurement["role"]]
    if measurement.get("round") is not None:
        parts.append(f"round {measurement['round']}")
    if measurement.get("attempt_id"):
        parts.append(f"attempt {measurement['attempt_id']}")
    return f"{measurement['measurement_id']} ({', '.join(parts)})"


def _texts(sample: dict, pick) -> str:
    return "\n".join(text for turn in sample.get("turns") or () for text in pick(turn)) or "—"


def _question(sample: dict) -> str:
    return _texts(sample, lambda turn: [turn["message"]["content"]] if turn.get("message") else [])


def _answer(sample: dict) -> str:
    return _texts(sample, lambda turn: [event.get("content", "") for event in turn.get("events") or ()
                                        if event.get("type") == "assistant_message"])


def _score(sample: dict) -> str:
    metrics = (sample.get("scoring") or {}).get("metrics") or {}
    return ", ".join(f"{name} {value:g}" for name, value in metrics.items()) or "not scored"


async def _read(state: Path, out: Path) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    async with Owner.open(state) as owner:
        operations = list(await owner.list_operations())
        if len(operations) != 1:
            raise SystemExit(f"expected one operation in {state}, found {len(operations)}")
        operation = operations[0]
        ref = ResourceRef("operation", operation.id)

        report = await owner.report_markdown(ref)
        if report.state == "available":
            (out / "report.md").write_text(report.markdown, encoding="utf-8")

        lines = [f"# {operation.kind.capitalize()} conversations", "",
                 "Every sample Lab measured: the question Core sent, the agent's answer, "
                 "and the scorer's score and verdicts.", ""]
        for measurement in (await owner.measurements(ref))["measurements"]:
            measurement_id = measurement["measurement_id"]
            detail = (await owner.measurement(ref, measurement_id))["measurement"]
            mean = ", ".join(f"{name} {value['mean']:g}" for name, value in detail["metrics"].items())
            lines += [f"## {_label(detail)}", "", f"Mean: {mean or '—'}", ""]
            for row in detail.get("samples") or ():
                sample = await owner.sample(ref, measurement_id, row["case_id"], row["sample_id"], disclose=True)
                verdicts = ", ".join(f"{v['criterion']} {'passed' if v['passed'] else 'failed'}"
                                     for v in (sample.get("scoring") or {}).get("verdicts") or []) or "—"
                lines += [f"### case {row['case_id']}, sample {row['sample_id']}", "",
                          f"- question: `{_question(sample)}`",
                          f"- answer: `{_answer(sample)}`",
                          f"- {_score(sample)}; scorer verdicts: {verdicts}", ""]
                rows.append((_label(detail), row["case_id"], _question(sample), _answer(sample), _score(sample)))
        (out / "conversations.md").write_text("\n".join(lines), encoding="utf-8")

        if operation.kind == "campaign":
            diff = await owner.diff(ref)
            if diff.text:
                (out / "changes.diff").write_text(diff.text, encoding="utf-8")
            exported = await owner.published_export(ref)
            if exported is not None:
                shutil.copyfile(state / exported["path"], out / "export.zip")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path)
    workspace = parser.parse_args().workspace.resolve()
    summary = json.loads((workspace / "summary.json").read_text(encoding="utf-8"))
    kind = summary["schema"]
    if kind not in _HAPPENED:
        raise SystemExit(f"unknown example summary {kind!r}")

    print("Checks:")
    checks = _checks(summary)
    for name, got, want in checks:
        print(f"  {'ok  ' if got == want else 'FAIL'} {name} = {json.dumps(got)}")
    if any(got != want for _, got, want in checks):
        print(json.dumps(summary, indent=2, sort_keys=True))
        sys.exit("demo failed: see the summary above and ../docs/guide/install.md#troubleshooting")

    rows = asyncio.run(_read(workspace / "state", workspace))
    print()
    print(_HAPPENED[kind])
    print()
    print("Samples: case, question -> the agent's answer, score")
    shown = None
    for label, case, question, answer, score in rows:
        if label != shown:
            print(f"  {label}")
            shown = label
        print(f"    {case}  {question} -> {answer}   [{score}]")
    print()
    ownership = operator_line(select_mode())
    if ownership is not None:
        print(ownership)
        print()
    print(_NOTE[kind])
    print()
    files = [
        ("source/", "the agent project: agent/, launch.json, corpus.jsonl, scorer/, seed/"
                    + (", manifest.json, editor/, reflector/" if kind == _CAMPAIGN else "")),
        ("conversations.md", "every sample: question, answer, score, scorer verdicts"),
        ("report.md", "Lab's report of the run"),
    ]
    if kind == _CAMPAIGN:
        files += [("changes.diff", "what the campaign changed in SKILL.md"),
                  ("export.zip", "Lab's export of the champion, to take home")]
    files += [("summary.json", "the example's own summary"),
              ("state/", "Lab's internal state; read it through lab commands or the panel")]
    print(f"Workspace: {workspace}")
    for name, what in files:
        if (workspace / name.rstrip("/")).exists():
            print(f"  {name:<17} {what}")
    print()
    print("What it all means: ../docs/guide/demo.md")


if __name__ == "__main__":
    main()
