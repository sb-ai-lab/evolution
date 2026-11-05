"""The small public CLI for service-first tree evolution.

`evolve` runs or continues a `/15` or opt-in `/16` campaign; `conformance`
checks a `/1` through `/16` target declaration against its wire contract.
`authorize` records acceptance of detected input changes. Evaluation, trace
and watch use their separate existing readers and boundaries.

`evolve` is a thin shell over `evolution.tree.evolve.evolve` (PDR-0003): four
options, a signal flag, one JSON document on stdout, and an exit code that
reports the campaign's outcome (PDR-0005). Every decision it could otherwise
take -- how many rounds, which model, which seed -- belongs to the manifest.

`authorize` is a policy command, not a semantic override (PDR-0003 amended
2026-09-17): it writes one decision file into the run directory and starts
nothing, reads no manifest, and can accept only the codes `AUTHORIZABLE_CODES`
names. It cannot change what a campaign measures, which is what keeps the
manifest the one carrier of campaign semantics; it says that a difference the
lifecycle detected is one this operator meant, and the next `evolve` journals
that decision as the reason it continued.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, cast

import typer

from evolution.cli.app import app, console
from evolution.tree import evolve as evolve_module
from evolution.tree import process_log
from evolution.tree.conformance import run_conformance
from evolution.tree.execution.authorizations import (
    ADAPTATION_PROFILE_FIELD,
    EFFECT_LEADS,
    EFFECT_SENTENCES,
    GUARD_CLASSES,
    HARD_REASONS,
    SCALE_CHANGE_SENTENCE,
    Authorization,
    AuthorizationError,
    DecisionEffect,
    comparable_changed,
    hint_command,
    hint_command_accept,
    read_decisions,
    refusal_class,
    write_decision,
)
from evolution.tree.execution.config import ManifestError
from evolution.tree.execution.output_admission import EvolveRefused, output_roots
from evolution.tree.manifest.manifest_v2 import load_manifest_v2
from evolution.tree.roles.role_call import is_attempt_line
from evolution.tree.storage.redaction import safe_record

tree_app = typer.Typer(help="Evolve a local service through immutable Candidate snapshots.")
app.add_typer(tree_app, name="tree")

#: The signals an operator uses to stop a campaign. Both set the same flag: C1
#: reads it at its terminal barrier and finishes the campaign `stopped`, so a
#: cancelled run still writes its `campaign_finished` record and still prints
#: its document. Killing the process instead leaves a resumable journal.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@tree_app.command("evaluate")
def evaluate_command(
    manifest: Path = typer.Option(..., "--manifest"),
    run_dir: Path = typer.Option(..., "--run-dir"),
    progress: bool = typer.Option(False, "--progress", help="Print run progress on stderr."),
) -> None:
    """Measure a frozen tree on train or test without running improvement roles."""
    with process_log.capture("evaluate", prefix="evo tree evaluate") as log:
        _evaluate(manifest, run_dir, progress, log)


def _evaluate(manifest: Path, run_dir: Path, progress: bool, log: process_log.ProcessLog) -> None:
    from evolution.tree.artifacts.artifact_errors import ArtifactCapacityError
    from evolution.tree.bindings import BindingError
    from evolution.tree.campaign import CampaignFamilyUnsupported
    from evolution.tree.evaluation import evaluate, evaluation_secrets
    from evolution.tree.evaluation_manifest import load_evaluation_manifest
    from evolution.tree.execution.candidate_integrity import ArtifactError
    from evolution.tree.execution.corpus import CorpusError
    from evolution.tree.execution.journal_identity import IdentityError
    from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault
    from evolution.tree.storage.journal_errors import JournalBoundaryError

    controller = None
    redactions: tuple[str, ...] = ()

    def note(message: str) -> None:
        typer.echo(safe_record(message, redactions), err=True)

    try:
        redactions = evaluation_secrets(load_evaluation_manifest(manifest), dict(os.environ))
        log.set_redactions(redactions)
        if progress:
            from evolution.tree.run_watch import ProgressController

            def say(text: str) -> None:
                typer.echo(safe_record(f"evo tree evaluate: {text}", redactions), err=True)

            controller = ProgressController(run_dir, say=say)
        with _stop_flag() as stopped:
            result = asyncio.run(
                evaluate(
                    manifest,
                    run_dir,
                    cancelled=stopped,
                    notes=note,
                    process_log=log.materialize,
                )
            )
        if controller is not None:
            controller.finish()
    except CampaignFamilyUnsupported as exc:
        if controller is not None:
            controller.discard()
        location = f" at {exc.path}; expected {exc.expected}" if exc.expected else ""
        typer.echo(f"evo tree evaluate: refused: {exc}{location}", err=True)
        raise typer.Exit(2) from None
    except (
        EvolveRefused,
        ManifestError,
        BindingError,
        CorpusError,
        IdentityError,
        ArtifactError,
    ) as exc:
        if controller is not None:
            controller.discard()
        if isinstance(exc, ArtifactCapacityError) and exc.detail is not None:
            # ADR-0040 D8: free and required inodes, path-free.
            typer.echo(safe_record(f"evo tree evaluate: {exc.detail}", redactions), err=True)
        typer.echo(
            f"evo tree evaluate: refused: {getattr(exc, 'code', 'invalid_identity')}", err=True
        )
        raise typer.Exit(2) from None
    except JournalBoundaryError as exc:
        if controller is not None:
            controller.discard()
        if isinstance(exc, ScorerTraceStorageFault):
            # ADR-0040 D7: the trace entry that failed, by its run-relative path.
            typer.echo(safe_record(f"evo tree evaluate: {exc.detail}", redactions), err=True)
        typer.echo("evo tree evaluate: journal unavailable", err=True)
        raise typer.Exit(1) from None
    finally:
        if controller is not None:
            controller.discard()
    typer.echo(json.dumps(result, ensure_ascii=True, sort_keys=True))
    raise typer.Exit(0 if result["state"] == "completed" else 1)


@contextmanager
def _stop_flag() -> Iterator[Callable[[], bool]]:
    """Turn `SIGINT`/`SIGTERM` into a flag, and put the old handlers back.

    The handler must not raise: a `KeyboardInterrupt` out of the default `SIGINT`
    handler would tear the event loop down mid-write, which is precisely what
    C1's cooperative cancellation exists to avoid.
    """

    stopping = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stopping.set()

    previous = [(number, signal.signal(number, request_stop)) for number in _STOP_SIGNALS]
    try:
        yield stopping.is_set
    finally:
        for number, handler in previous:
            signal.signal(number, handler)


def _print_refusal(exc: EvolveRefused, run_dir: Path, redactions: tuple[str, ...]) -> None:
    """The refusal line, the diff, the class and the way on (spec §4).

    Every line goes through the run's own redactor: a difference names a field
    and a digest, but the refusal line itself can carry a location built from
    corpus content, and a forwarded `allowed_env` value hiding in any token of
    any of these lines must not reach stderr. The loader's own refusal has no
    document yet and so nothing to redact.

    The class is the instance's, not the code's. `refusal_class` answers for the
    code -- `run_identity_changed` is authorizable -- while whether *this*
    difference can be accepted is what `instance_digest` says: core computes one
    only for a transition a decision could carry. A refusal with no digest has
    no command to print, whatever class its code is in, so the printer consults
    both and prints `hard` when there is nothing to quote.

    An authorizable refusal whose effect has a sentence (`close`, `terminal`,
    `link`) prints it between the class and the command (comparable-links spec
    §5.1). A hard `run_identity_changed` reaches `_hard_reason_key`, which picks
    among a finished campaign, a moved `linked_from` and an already owed terminal.

    A collected refusal (comparable-links spec §6) prints one such block per
    finding. When every finding is authorizable, one `--accept` command
    accepts them all; when the first is hard, no command is printed and its
    sentence comes last. A single-finding refusal prints what it always did.

    I2 spec §4.1: a finding that offers several effects (a family-7
    comparable `run_identity_changed`) prints no effect sentence; a scale
    change -- no `continue` among them -- says why instead, and the way on is
    one lead line and one command per effect, the default first and without
    `--effect`, every other one with `--effect <name>`. In a collected refusal
    each of those commands is the one combined `--accept` line. A finding with
    one effect, and every family 3-6 refusal, prints what it printed before.
    """

    def say(text: str) -> None:
        typer.echo(safe_record(f"evo tree evolve: {text}", redactions), err=True)

    def command(line: str) -> None:
        typer.echo(safe_record(f"  {line}", redactions), err=True)

    # Comparable-links spec §6: one block per finding, in the order admission
    # collected them. A single-finding refusal is this loop run once, and
    # prints exactly what it printed before findings were collected.
    authorizable: list[tuple[str, str]] = []
    for finding in exc.findings:
        say(
            f"refused: {finding.code if finding.path is None else f'{finding.code} at {finding.path}'}"
        )
        if finding.expected:
            say(finding.expected)
        for difference in finding.differences:
            say(
                f"differs: {difference.field}  journaled {difference.journaled}  "
                f"observed {difference.observed}"
            )
        kind = "hard" if finding.instance_digest is None else refusal_class(finding.code)
        say(f"class: {kind}")
        if kind == "authorizable" and finding.instance_digest is not None:
            # PDR-0012 / comparable-links spec §5.1: the consequence of accepting
            # is printed before the command, so the operator reads what the
            # decision will do -- close the campaign, or write its owed terminal
            # -- before pasting it.
            if len(finding.effects) > 1:
                if "continue" not in finding.effects:
                    say(SCALE_CHANGE_SENTENCE)
            else:
                for line in EFFECT_SENTENCES.get(finding.effect or "", ()):
                    say(line)
            authorizable.append((finding.code, finding.instance_digest))
    # The run directory as `--run-dir` carried it: pathlib normalizes the
    # string (a trailing separator, a `./` prefix) but resolves nothing, so
    # the line stays a command the operator can paste back.
    choices = next((f.effects for f in exc.findings if len(f.effects) > 1), ())
    if choices and authorizable and len(authorizable) == len(exc.findings):
        # I2 spec §4.1: one command per admissible effect, the default first.
        # Task 3's `--effect` attaches to the `run_identity_changed` item only,
        # so a collected refusal keeps its one combined `--accept` line.
        for index, name in enumerate(choices):
            say(EFFECT_LEADS[name])
            command(hint_command_accept(str(run_dir), authorizable, None if index == 0 else name))
        return
    if len(authorizable) == len(exc.findings) == 1:
        # A close or an owed terminal ends this campaign; its effect sentence
        # already names the linked run that continues, so the lead line does
        # not promise that this campaign continues.
        ending = exc.findings[0].effect in ("close", "terminal")
        say(
            "to accept this difference, run:"
            if ending
            else "to accept this difference and continue, run:"
        )
        command(hint_command(str(run_dir), *authorizable[0]))
        return
    if authorizable and len(authorizable) == len(exc.findings):
        # As for one finding: when any accepted finding closes the campaign or
        # writes its owed terminal, the lead line does not promise it continues.
        ending = any(finding.effect in ("close", "terminal") for finding in exc.findings)
        say(
            "to accept these findings, run:"
            if ending
            else "to accept these findings and continue, run:"
        )
        command(hint_command_accept(str(run_dir), authorizable))
        return
    # A hard finding -- always the first (§6) -- ends admission: accepting the
    # collected ones would not admit the run, so no command is printed, and
    # the hard finding's own sentence closes the report.
    sentence = HARD_REASONS.get(_hard_reason_key(exc))
    if sentence is not None:
        say(sentence)


def _hard_reason_key(exc: EvolveRefused) -> str:
    """Which sentence a hard refusal prints: its code, or the finished variant.

    A `run_identity_changed` that reports differences and no comparable one
    among them is hard for the other reason §3 gives: the campaign already
    wrote `campaign_finished`, so there is nothing left to run and no
    consequence record the lifecycle would accept. Telling that operator "a
    comparable field changed" names a cause that did not happen.

    A campaign that crossed its threshold and never wrote its terminal is no
    longer here (PDR-0010, amended 2026-09-17): it still owes one record, its
    non-comparable difference is authorizable, and the operator is given the
    `evo tree authorize` line above instead of a sentence.

    A refusal with *no* differences keeps the code's own sentence. The one
    instance that reaches here -- the artifact store's stamp disagreeing with
    the identity C1 derived -- is neither case, and its advice (a linked run, or
    another run directory) is right under either sentence.

    `run_dir_unsafe_entry` reaches here digest-less for three unrelated
    reasons, and only one of them is the finished campaign: a guard-class
    finding (`GUARD_CLASSES` -- `symlinked`, `foreign owner`, `not
    owner-only`) that `_refuse_hygiene_hard` stripped its digest from because
    the campaign is finished and no decision could be applied; a regular file
    where an output root must be (`detail == "not a directory"`), which is
    hard because there is no finding to admit at all, campaign finished or
    not; and an `OSError` from `lstat`, whose `detail` is neither and which
    prints only the code's own (absent) sentence rather than guess at either.
    """

    if exc.code == "run_identity_changed" and exc.differences:
        # ADR-0021: a moved adaptation profile is a family change, hard
        # whether or not the campaign finished; it leads its differences.
        if exc.differences[0].field == ADAPTATION_PROFILE_FIELD:
            return "run_identity_changed_adaptation_profile"
        # The resume presence rule for `linked_from` (continuable terminals
        # §6.3): hard, and neither of the other two causes -- the field is not
        # comparable, and the campaign need not be finished.
        if [difference.field for difference in exc.differences] == ["identity.linked_from"]:
            return "run_identity_changed_linked_from"
        if [difference.field for difference in exc.differences] == [
            "identity.linked_from.baseline"
        ]:
            # Baseline inheritance design §4.1: the same non-comparable,
            # campaign-need-not-be-finished shape as the presence rule above.
            return "run_identity_changed_linked_baseline"
        if not comparable_changed(exc.differences):
            return "run_identity_changed_finished"
    if exc.code == "run_dir_unsafe_entry" and exc.instance_digest is None:
        if exc.detail in GUARD_CLASSES:
            return "run_dir_unsafe_entry_finished"
        if exc.detail == "not a directory":
            return "run_dir_unsafe_entry_file"
        # Neither a guard class nor "not a directory": an `OSError` from
        # `lstat`. `exc.code` alone has no entry in `HARD_REASONS`, so this
        # prints no sentence rather than guess at one.
    return exc.code


def v15_help_epilog() -> str:
    """The `/15` settings for `evolve --help`: path, accepted values, default.

    The `\\b` line keeps click from rewrapping the one-line-per-setting block.
    """

    from evolution.tree.manifest.campaign_policy_help import PARAMETER_HELP_V15

    lines = ["Manifest /15 settings:", ""]
    lines.append("\b")
    lines.extend(f"{e.path}: {e.accepted} {e.default}" for e in PARAMETER_HELP_V15)
    lines.append("")
    lines.append("\b")
    lines.append("Full help: docs/manifest-v2.md, section 'Stable family keys (manifest /15)'.")
    lines.extend(
        [
            "",
            "Manifest /16 settings:",
            "",
            "\b",
            "campaign.evaluation.repeats_per_question: integer >= 1; statistical and measured_noise require >= 2; recommended 1 for point.",
            "campaign.gate.mode: point, confidence, unfiltered or statistical; recommended point. Statistical requires focus/causal confidence, no statistics block.",
            "campaign.gate.execution_noise: negligible, bounded allowance/source or observed; explicit, no calibration.",
            "campaign.gate.verification: reuse or fresh; recommended reuse.",
            "campaign.gate.causal.tolerance: finite number >= 0, or measured_noise in statistical, point or confidence at R >= 2; recommended 0.",
            "campaign.gate.stages: optional single or focus_first; absent means single; recommended focus_first.",
            "campaign.gate.bound_fallback: optional point, only in confidence mode; absent means no fallback.",
            "Other /16 fields are required, without defaults. The scaffold stays /15.",
            "Full help: docs/manifest-v2.md, section 'Independent controls (manifest /16)'.",
        ]
    )
    return "\n".join(lines)


@tree_app.command("evolve", epilog=v15_help_epilog())
def evolve(
    # No Click `exists=True` on either path: a `UsageError` prints the absolute
    # path the operator typed, and spec §9 keeps filesystem paths out of every
    # refusal. A path that does not exist is `evolve`'s refusal to make.
    manifest: Path = typer.Option(..., "--manifest"),
    run_dir: Path = typer.Option(..., "--run-dir"),
    quiet: bool = typer.Option(False, "--quiet", help="Print only the terminal and the outcome."),
    json_indent: int = typer.Option(2, "--json-indent", min=0, help="Indent of the JSON document."),
    progress: bool = typer.Option(False, "--progress", help="Print run progress on stderr."),
) -> None:
    """Run, or continue, a campaign declared by manifest /15 or opt-in /16.

    Stdout is one JSON document -- `terminal`, `quiescent`, `finished`,
    `suspended`, `owner`, `code`, `result`, `status`, or just `terminal`,
    `finished` and `code` under `--quiet`. The exit code reports the campaign's
    outcome: 0 completed (promoted or not), 3 stopped, 4 budget exhausted, 1 an
    infrastructure or storage failure -- a report, never a dead end: the run
    directory is resumable by this same command. A campaign that met an
    infrastructure cause is `suspended`: it prints
    `suspended: <reason> — <evidence>` and the `evolve` line that continues it,
    and exits 1. A finished campaign instead prints one `continue:` line on
    stderr naming the `linked_from` and `seed_root` a successor declares. A
    linked run whose comparable condition differs from its predecessor's prints,
    before it starts, `note: condition changed across the link` and one
    `differs:` line per changed field; the run proceeds and reports the
    difference as a `condition_changed` caveat (PDR-0012). A refusal to start
    leaves stdout empty and exits 2, having printed on stderr, per finding, its
    `refused:` line, one `differs:` line per reported difference, its class and,
    for a close or an owed terminal, what accepting does; then either the `evo tree authorize` command that accepts
    every finding (`--accept CODE:DIGEST` per finding when there are several) or
    the sentence that says why nothing can (PDR-0010).
    """

    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        _evolve(manifest, run_dir, quiet, json_indent, progress, log)


def _evolve(
    manifest: Path,
    run_dir: Path,
    quiet: bool,
    json_indent: int,
    progress: bool,
    log: process_log.ProcessLog,
) -> None:
    # The manifest is admitted once and the environment snapshotted once, here,
    # and the same document and mapping feed the redaction set, the short-value
    # notes and the run itself: a manifest rewritten between two reads, or a
    # variable changed between two snapshots, cannot leave the printed line
    # redacted against one secret set and the campaign armed with another (W2
    # fix pack). A loader refusal has no document and nothing to redact.
    try:
        admitted = evolve_module.admit_manifest(manifest)
    except EvolveRefused as exc:
        _print_refusal(exc, run_dir, ())
        raise typer.Exit(2) from exc
    environ = dict(os.environ)
    redactions = evolve_module.redactions_for(admitted, environ)
    log.set_redactions(redactions)
    # The secret floor is a length rule, so it cannot tell a short credential
    # from a flag. It keeps the flag -- the alternative ends the campaign after
    # the run root exists -- and names what it skipped, here, before the run,
    # while the operator can still act on it. The name, never the value: the
    # value is the one string this line exists to keep out of the terminal.
    for name in evolve_module.short_forwarded_names(admitted, environ):
        typer.echo(
            safe_record(
                f"evo tree evolve: note: forwarded value {name} is shorter than "
                "8 characters and is not redacted",
                redactions,
            ),
            err=True,
        )
    assert admitted.workflow is not None  # `admit_manifest` refuses one without
    # #295: a chat role on plain `http` to a host that is not loopback is
    # admitted, and its cost is named: one warning per role, before any role
    # runs, on every invocation (a resume included), kept by the process log.
    # A warning, never a question -- nothing reads stdin, so a worker, a pipe
    # and a terminal behave alike (PDR-0010). Only the label is styled, and
    # Click strips the style when stderr is not a terminal. Click does not read
    # `NO_COLOR`, so a non-empty value keeps the label plain here
    # (https://no-color.org).
    label = (
        "WARNING:" if environ.get("NO_COLOR") else typer.style("WARNING:", fg="yellow", bold=True)
    )
    for role, base_url, credential in evolve_module.unencrypted_model_roles(admitted.workflow):
        what = (
            f"sends its credential unencrypted to {base_url}"
            if credential
            else f"talks to {base_url} without encryption"
        )
        typer.echo(safe_record(f"evo tree evolve: {label} {role} {what}", redactions), err=True)
    # #290 §4.1: a chat role without `api_key_env` calls its endpoint without
    # credentials. A note, not a refusal, printed before any role runs, on every
    # invocation (a resume included); the process log keeps it like every line.
    for role, base_url in evolve_module.unauthenticated_roles(admitted.workflow):
        typer.echo(
            safe_record(
                f"evo tree evolve: {role} calls {base_url} without credentials "
                "(no api_key_env declared)",
                redactions,
            ),
            err=True,
        )

    # Every line this command writes to stderr after admission goes through
    # here: the command's own prefix, the run's redactor, and no filesystem
    # path the caller did not already type (spec §9). `note` and the suspension
    # lines below are its two callers.
    def say(text: str) -> None:
        typer.echo(safe_record(f"evo tree evolve: {text}", redactions), err=True)

    # `evolve`'s own sink for a remark about an invocation it accepts -- today,
    # a `PYTHONPATH` entry the binding dropped. It is prefixed here and nowhere
    # else, so every note reads like the refusal line above it.
    def note(text: str) -> None:
        # Comparable links D2a §3: a changed link's `differs:` lines read
        # exactly like a refusal's, so they take no `note:` prefix. The run's
        # redactor is defence in depth: a difference carries digests only.
        # #290 §4.4: a role invocation's provider-attempt line takes none either.
        plain = text.startswith(("differs: ", "check shadowed: ")) or is_attempt_line(text)
        say(text if plain else f"note: {text}")

    from evolution.tree.execution.check_modes import CONSEQUENCES

    if admitted.workflow.checks is not None:
        for name, mode in admitted.workflow.checks.non_enforce():
            note(f"check {name} is {mode}: {CONSEQUENCES[name]}")

    controller = None
    try:
        if progress:
            from evolution.tree.run_watch import ProgressController

            controller = ProgressController(run_dir, say=say)
        with _stop_flag() as cancelled:
            result = asyncio.run(
                evolve_module.evolve(
                    admitted,
                    run_dir,
                    cancelled=cancelled,
                    notes=note,
                    environ=environ,
                    # The decision directory is the `file` source and nothing
                    # else is: `evolve` stamps every decision it reads from the
                    # directory `file` and every value of `authorizations`
                    # `caller`, so the journaled provenance is core's own. The
                    # CLI no longer reads the directory: it
                    # would read it through the `--run-dir` name, and `evolve`
                    # resolves that name to the root it then inspects and
                    # mutates -- a link re-pointed between the two reads would
                    # apply campaign A's decisions to campaign B (PDR-0010).
                    # A malformed directory still arrives as
                    # `authorizations_invalid at <name>`, through `EvolveRefused`
                    # like every other refusal, and prints `class: hard`.
                    decision_directory=True,
                    # L7 / ADR-0036: retired schemas keep their output roots
                    # and get no process log even under the test admission helper.
                    process_log=log.materialize
                    if process_log.PROCESS_LOGS_DIR in output_roots(admitted.schema)
                    else None,
                )
            )
        if controller is not None:
            controller.finish()
    except EvolveRefused as exc:
        if controller is not None:
            controller.discard()
        # `evolve` converts every pre-admission refusal -- the loader's, the
        # bindings', the corpus's, C1's own and the adapter's preflight -- into
        # this one type, and `str()` is already `<code>` or `<code> at <path>`
        # with no filesystem path in it (spec §6, §9). Anything else propagates:
        # a fault after the first durable write is not a refusal, and its
        # traceback is the honest report. The line is printed through the run's
        # own redaction, same as the result document below: `path` can carry a
        # location inside the campaign's creation record (spec §6), built from
        # corpus content, and a forwarded `allowed_env` value hiding there must
        # not reach stderr whatever token of the line it happens to match.
        _print_refusal(exc, run_dir, redactions)
        raise typer.Exit(2) from exc
    finally:
        if controller is not None:
            controller.discard()
    document: dict[str, Any] = (
        {"terminal": result.terminal, "finished": result.finished, "code": result.code}
        if quiet
        else {
            "terminal": result.terminal,
            "quiescent": result.quiescent,
            "finished": result.finished,
            # I1 spec §5.2: `finished`'s counterpart. The `campaign_suspended`
            # payload this run wrote -- `reason`, `owner` and the evidence its
            # kind carries -- or `null` for every other terminal.
            "suspended": result.suspended,
            "owner": result.owner,
            "code": result.code,
            "result": None if result.result is None else result.result.to_payload(),
            "status": result.status.to_payload(),
        }
    )
    # Plain `json.dumps`, not `console.print_json`: this document is read by a
    # pipe, and rich would wrap it to the console width and colour it.
    typer.echo(json.dumps(safe_record(document, redactions), indent=json_indent, default=str))
    independent = result.status.to_payload().get("independent")
    if independent is not None:
        from evolution.tree.family15_report import report_lines

        for report_line in report_lines(independent):
            say(report_line)
    champion_path = _champion_path(result)
    # #290 §4.4: what the provider answered a role invocation that ended in a
    # provider failure, right after the failure report -- the suspension line
    # or, for another terminal, its report -- so the role log need not be
    # opened. Its recovered counterparts went out as notes during the run.
    attempt_lines = result.provider_attempt_lines
    if result.fault_detail is not None:
        # ADR-0040 D7/D8: the storage fault's own line -- the trace entry that
        # failed, or free and required inodes -- kept by the process log.
        say(result.fault_detail)
    if result.code == "projection_invalid":
        say(
            f"evo tree evolve: projection_invalid: the recorded terminal is {result.terminal}; "
            "its report could not be projected; report this Core version"
        )
    if result.terminal == "suspended" and result.suspended is not None:
        # I1 spec §5.5: a suspension names its cause and its evidence on one
        # line and then the command that continues it -- the same `evolve`, no
        # authorization and no new run directory. On stderr in every mode,
        # `--quiet` included, like the finished campaign's line below: the
        # operator's next step does not belong in the machine-read document.
        suspended = result.suspended
        # Family 14 (C spec §4.3): a connection suspension's sentence reads the
        # status document's closed `connection` object, never the probe's data.
        detail = _suspension_detail(
            suspended.get("evidence") or {},
            reason=suspended.get("reason"),
            connection=result.status.to_payload().get("connection"),
        )
        say(f"suspended: {suspended.get('reason')} — {detail}")
        for attempt_line in attempt_lines:
            say(attempt_line)
        say(
            "continue: evo tree evolve --manifest "
            f"{shlex.quote(str(manifest))} --run-dir {shlex.quote(str(run_dir))}"
        )
    elif result.finished is not None and champion_path is not None:
        # Continuable terminals §8: every finished campaign names the two keys
        # a successor declares. On stderr, so stdout stays one JSON document,
        # and in every mode, `--quiet` included. The run directory is printed
        # canonical and absolute, unlike the authorize hint's `--run-dir` as
        # typed: both keys resolve against the *successor manifest's* directory,
        # so a relative `--run-dir` pasted there would name another directory
        # (Ruling R15). An absolute path means the same thing in any manifest.
        absolute = os.path.realpath(run_dir)
        # Comparable-links spec §8: a campaign closed under a changed condition
        # says where that condition goes -- the successor's manifest, since
        # this journal keeps the identity every measurement in it was taken
        # under.
        condition = (
            "; declare the new condition in the successor's manifest"
            if result.finished.get("reason") == "condition_changed"
            else ""
        )
        typer.echo(
            safe_record(
                f"evo tree evolve: continue: set linked_from to {absolute} and seed_root to "
                f"{absolute}/{champion_path} (the target must allow trees under it){condition}",
                redactions,
            ),
            err=True,
        )
    if not (result.terminal == "suspended" and result.suspended is not None):
        for attempt_line in attempt_lines:
            say(attempt_line)
    # Role resilience spec §6.3: a family-10 run's status carries `resilience`
    # (absent in every earlier family); one line, whatever the terminal, so
    # the operator can see whether the skip and the continuation turn pay for
    # themselves without reading the JSON document above.
    line = _resilience_line(result.status.to_payload().get("resilience"))
    if line is not None:
        say(line)
    raise typer.Exit(evolve_module.exit_code_for(result))


def _resilience_line(resilience: Mapping[str, Any] | None) -> str | None:
    """The one status line spec §6.3 asks for, or `None` when absent."""

    if resilience is None:
        return None
    return (
        f"Skipped rounds: {resilience['rounds_skipped']} · "
        f"Continuation turns: {resilience['continuation_turns']}, "
        f"led to an edit: {resilience['attempts_saved']}"
    )


#: Role resilience spec §4.2, §5.3 (PDR-0019, ADR-0023): the one-line cause
#: of each family-10 suspension reason, printed in place of the generic
#: `stage_record` detail below -- naming which role failed and how, since
#: "decided by the record at sequence N" alone does not say what to expect
#: on resume. Every earlier family's suspension text is unchanged.
_RESILIENCE_SUSPENSION_CAUSES: Mapping[str, str] = {
    "reflector_provider_failed": "the Reflector's provider failed after retries",
    "reflector_failing": (
        "the Reflector failed for an unexplained reason or twice skipped in a row"
    ),
    "editor_provider_failed": "the Editor's provider failed after retries",
    "editor_failing": "the Editor failed for an unexplained reason",
}

#: Family 14 (ADR-0034, F0 R46): the Grouper's own suspensions, printed the
#: same way; their `stage_record` names the Grouper's `grouping_failed`.
_GROUPER_SUSPENSION_CAUSES: Mapping[str, str] = {
    "grouper_provider_failed": "the Grouper's provider failed after retries",
    "grouper_failing": (
        "the Grouper failed for an unexplained reason or fell back for every failure twice in a row"
    ),
}

#: G2b3b (G2 spec §4.6): family 12's component-drift suspension, printed the
#: same way. Its `stage_record` names the refused measurement's binding,
#: which says nothing about the map the Target rendered.
_AFFECTED_SUSPENSION_CAUSES: Mapping[str, str] = {
    "target_components_changed": (
        "the Target rendered a component map other than the one this condition "
        "already rendered for the tree"
    ),
}

#: #97 spec §4.1: the scorer's primary metric is outside [0, 1].
_SCORER_SUSPENSION_CAUSES: Mapping[str, str] = {
    "scorer_metric_out_of_range": (
        "the scorer returned a primary metric outside [0, 1]; fix the scorer, "
        "then rerun the same command (a changed scorer is a comparable change: "
        "accept it with evo tree authorize)"
    ),
}

#: Family 13 (design §8, D6): the final cohort's own suspension. Kept out of
#: `_SUSPENSION_CAUSES`, whose sentence replaces the evidence: this one is
#: followed by the first unfinished pair (`_suspension_detail`).
_CONFIDENCE_SUSPENSION_CAUSES: Mapping[str, str] = {
    "cohort_incomplete": (
        "the final cohort has pairs left without a score: an infrastructure-owned one "
        "on a finalist or any on the Champion; repair the cause and run the same "
        "command to resume"
    ),
    "final_incomplete": (
        "the final measurement has pairs left without a score: an infrastructure-owned one "
        "on a finalist or any on the Champion; repair the cause and run the same "
        "command to resume"
    ),
}

#: Family 14 (C spec §4.3, ADR-0029; H12b plan choice 16): the connection
#: preflight's three suspensions. Each sentence is filled from the status
#: document's `connection.detail` -- the stable code and the two diagnostic
#: paths of an incompatibility, the closed `cause_code` otherwise -- and never
#: from the probe's observed data. The second element names the detail keys.
_CONNECTION_SUSPENSION_CAUSES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "connection_incompatible": (
        "the connection probe found the Target or Scorer incompatible: {code} at "
        "{manifest_path} (observed {observed_path}); fix the Target or the Scorer, then "
        "run the same command (a changed Scorer or metric_contract is a comparable "
        "change: accept it with evo tree authorize)",
        ("code", "manifest_path", "observed_path"),
    ),
    "connection_preflight_unavailable": (
        "the connection probe could not reach a conclusion ({cause_code}); repair the "
        "cause and run the same command",
        ("cause_code",),
    ),
    "connection_preflight_inconclusive": (
        "the connection probe's rollout ended without a scoreable terminal ({cause_code}); "
        "repair the cause and run the same command",
        ("cause_code",),
    ),
}

_SUSPENSION_CAUSES: Mapping[str, str] = {
    **_RESILIENCE_SUSPENSION_CAUSES,
    **_AFFECTED_SUSPENSION_CAUSES,
    **_SCORER_SUSPENSION_CAUSES,
    **_GROUPER_SUSPENSION_CAUSES,
}


def _suspension_detail(
    evidence: Mapping[str, Any], *, reason: Any = None, connection: Any = None
) -> str:
    """The half-sentence a suspension's evidence reads as (I1 spec §5.2, §5.5).

    One clause per `evidence_kind`, saying what the operator has to act on: a
    threshold names its window's counts and the bound that was crossed, a
    fault-row suspension how many rows the stage holds and what the first one
    was coded, a stage record which journal record decided it. An evidence
    kind this does not know still prints its own name rather than nothing.

    A family-10 `reason` in `_RESILIENCE_SUSPENSION_CAUSES`, or family 12's
    in `_AFFECTED_SUSPENSION_CAUSES`, prints its own cause instead of the
    generic `stage_record` clause, which names no role and no map. Family 13's
    `cohort_incomplete` prints its cause and then its `cohort_rows`: how many
    pairs are unfinished and the first one with its owner.

    A family-14 connection suspension reads `connection`, the status
    document's `connection` object (C spec §6.3): its sentence when the object
    names that same outcome and carries the detail the sentence needs, else the
    generic `stage_record` clause. No other reason reads it.
    """

    sentence = _connection_sentence(reason, connection)
    if sentence is not None:
        return sentence

    if isinstance(reason, str) and reason in _CONFIDENCE_SUSPENSION_CAUSES:
        cause = _CONFIDENCE_SUSPENSION_CAUSES[reason]
        rows = evidence.get("rows")
        first = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(first, Mapping):
            return cause
        count = evidence.get("count")
        member_kind = "final" if reason == "final_incomplete" else "cohort"
        return (
            f"{cause}; {count} unfinished {member_kind} pair{'' if count == 1 else 's'}, first "
            f"{first.get('measurement_id')} {first.get('case_id')}/{first.get('sample_id')} "
            f"({first.get('owner')})"
        )
    if reason == "champion_incomplete":
        # #194 spec §3.5 (families 13 and 14): the stopped Champion measurement.
        rows = evidence.get("rows")
        first = rows[0] if isinstance(rows, list) and rows else None
        count = evidence.get("count")
        head = (
            "the campaign was stopped before the Champion measurement "
            f"{evidence.get('measurement_id')} finished"
        )
        if not isinstance(first, Mapping):
            return head
        return (
            f"{head}; {count} unfinished pair{'' if count == 1 else 's'}, first "
            f"{first.get('case_id')}/{first.get('sample_id')}"
        )
    if isinstance(reason, str) and reason in _SUSPENSION_CAUSES:
        return _SUSPENSION_CAUSES[reason]
    kind = str(evidence.get("evidence_kind"))
    if kind == "threshold":
        crossing = evidence.get("crossing")
        crossing = crossing if isinstance(crossing, Mapping) else {}
        bound = (
            f"{_percent(crossing.get('max_rate'))} %"
            if crossing.get("kind") == "rate"
            else f"{crossing.get('max_count')} tries"
        )
        return (
            f"{crossing.get('count')} of {crossing.get('denominator')} tries "
            f"in this window, threshold {bound}"
        )
    if kind == "fault_rows":
        count = evidence.get("count")
        rows = evidence.get("rows")
        first = rows[0] if isinstance(rows, list) and rows else {}
        code = first.get("code") if isinstance(first, Mapping) else None
        counted = f"{count} fault row{'' if count == 1 else 's'} in this stage"
        return counted if code is None else f"{counted}, the first coded {code}"
    if kind == "stage_record":
        return f"decided by the record at sequence {evidence.get('seq')}"
    return f"evidence {kind}"


#: An incompatibility whose repair is not the Target's or the Scorer's code
#: path: its own sentence, keyed by detail code and filled the same way (#287).
#: A description that holds a forwarded value is repaired in `describe()`,
#: not by un-forwarding the name, which would change the Scorer's identity.
_CONNECTION_DETAIL_SENTENCES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "connection_description_secret": (
        "the target's instance description holds the value of {manifest_path}; remove it "
        "from describe() (for example report a hash) and run the same command again",
        ("manifest_path",),
    ),
}


def _connection_sentence(reason: Any, connection: Any) -> str | None:
    """`_CONNECTION_SUSPENSION_CAUSES[reason]` filled from `connection.detail`, or
    `None` when `reason` is not the connection's or the object cannot fill it."""

    if not isinstance(reason, str) or reason not in _CONNECTION_SUSPENSION_CAUSES:
        return None
    if not isinstance(connection, Mapping) or connection.get("outcome") != reason:
        return None
    detail = connection.get("detail")
    if not isinstance(detail, Mapping):
        return None
    template, keys = _CONNECTION_SUSPENSION_CAUSES[reason]
    code = detail.get("code")
    if (
        reason == "connection_incompatible"
        and isinstance(code, str)
        and code in _CONNECTION_DETAIL_SENTENCES
    ):
        template, keys = _CONNECTION_DETAIL_SENTENCES[code]
    values = {key: detail.get(key) for key in keys}
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return template.format(**values)


def _percent(rate: Any) -> str:
    """A journaled rate as a percentage, without its trailing zeros."""

    try:
        value = (Decimal(str(rate)) * 100).normalize()
    except (ArithmeticError, ValueError):
        return str(rate)
    return f"{value:f}"


def _champion_path(result: Any) -> str | None:
    """The finished campaign's `status.campaign_outcome.champion_path`, if it has one."""

    status = result.status.to_payload() if result.status is not None else None
    outcome = status.get("campaign_outcome") if isinstance(status, dict) else None
    path = outcome.get("champion_path") if isinstance(outcome, dict) else None
    return path if isinstance(path, str) else None


@tree_app.command("conformance")
def conformance(
    manifest: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False),
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Append this command's output to an owner-only file."),
    ] = None,
) -> None:
    """Check that the operator-started service proves its declared identity.

    `--manifest` is the strict `evolution-v2-manifest/1` document; the command
    prints one `conformance/v2` report to stdout and exits 0 when every check
    passed and the probe left nothing on the target, 1 when a check failed or
    the report names a leaked instance, and 2 when the manifest cannot be loaded.
    `--log-file` refuses an unusable or unsafe path with `log_file_unavailable`
    or `log_file_unsafe` (exit 2).
    """

    if log_file is None:
        _conformance(manifest)
        return
    try:
        fd = process_log.open_log_file(log_file)
    except process_log.LogFileRefused as exc:
        typer.echo(f"conformance: log file {exc.code}", err=True)
        raise typer.Exit(2) from exc
    with process_log.capture("conformance", prefix="conformance", log_fd=fd):
        _conformance(manifest)


def _conformance(manifest: Path) -> None:
    try:
        loaded = load_manifest_v2(manifest)
    except ManifestError as exc:
        expected = f": {exc.expected}" if exc.expected else ""
        typer.echo(f"conformance: manifest {exc.code}: {exc.detail}{expected}", err=True)
        raise typer.Exit(2) from exc
    report = asyncio.run(run_conformance(loaded))
    console.print_json(json.dumps(report.to_payload()))
    if report.leaked:
        # stdout stays one JSON document, so the operator's copy goes to stderr.
        # These ids are the run's own (`<run_id>-<uuid>`), so §9 allows printing
        # them; nothing else in the report may be named in prose.
        typer.echo(f"conformance: leaked instances: {' '.join(report.leaked)}", err=True)
    raise typer.Exit(0 if report.ok else 1)


@tree_app.command("authorize")
def authorize(
    run_dir: Path = typer.Argument(...),
    code: str | None = typer.Argument(None),
    digest: str | None = typer.Option(None, "--digest"),
    reason: str | None = typer.Option(None, "--reason"),
    list_decisions: bool = typer.Option(False, "--list"),
    accept: list[str] = typer.Option([], "--accept"),
    effect: str | None = typer.Option(None, "--effect"),
) -> None:
    """Accept one refusal `evo tree evolve` printed, or list a run directory's decisions (PDR-0010).

    Writes `<run_dir>/authorizations/<id>.json` and nothing else; starts nothing;
    needs no manifest, opens no socket and never touches the campaign journal.
    The id it prints on stdout is the one the next `evolve` will journal as the
    reason it continued. `--list` prints one JSON object per decision -- `id`,
    `code`, `digest`, `reason`, `issued_at` and `status`, `consumed` once a run
    journaled it and `pending` otherwise -- in issue order; it reports only, so
    `--digest` and `--reason` mean nothing beside it and a code argument with it
    is refused. `--accept CODE:DIGEST`, repeatable, is the form a refusal with
    several findings prints (comparable-links spec §6): each writes its own
    decision with the shared `--reason`, exactly as one call per finding would,
    one id per stdout line; it stands alone, without a code, `--digest` or
    `--list`. `--effect {continue,rebaseline,close}` (I2 spec §4.2) names the
    operator's answer to every `run_identity_changed` item this invocation
    accepts -- alone on the positional form, or any number of them among
    `--accept` items that also carry other codes (review round 1 ruling R6):
    those other items are written without an effect, as they always were.
    `--effect` is refused, `authorization_invalid at --effect`, when the
    invocation accepts no `run_identity_changed` item at all, when it names a
    value it does not know, or when it accompanies `--list`, which accepts
    nothing. A `run_identity_changed` item accepted without
    `--effect` is written `effect: "default"`, which `evolve` resolves to the
    first effect the instance admits. Exit 2 with
    `refused: authorization_invalid at <key>` for a hard code or a malformed
    decision, and `refused: authorizations_invalid at <name>` for a directory
    that cannot be read.
    """

    # ADR-0046: this command otherwise bypasses the reader family layer.
    # Check before scanning decisions/journal records or writing a decision.
    try:
        evolve_module._refuse_unadmitted_family_at(run_dir)
    except EvolveRefused as exc:
        expected = f"; expected {exc.expected}" if exc.expected else ""
        typer.echo(f"evo tree authorize: refused: {exc.code} at {exc.path}{expected}", err=True)
        raise typer.Exit(2) from None

    if accept:
        _authorize_accepted(run_dir, accept, code, digest, reason, list_decisions, effect)
        return
    if list_decisions:
        if effect is not None:
            # `--list` reports; it accepts nothing, so there is no
            # `run_identity_changed` item for `--effect` to attach to. Refuse
            # it exactly as any other invalid `--effect` use.
            typer.echo(
                "evo tree authorize: refused: authorization_invalid at --effect",
                err=True,
            )
            raise typer.Exit(2)
        if code is not None:
            # `--list` reports; it does not accept. Refusing the pair is the
            # only reading that cannot be mistaken for either one.
            typer.echo(
                "evo tree authorize: refused: authorization_invalid at --list",
                err=True,
            )
            raise typer.Exit(2)
        # The journal is what says a decision was acted on, not the file: a
        # decision the operator wrote twice has one id, and the record that
        # quotes it is the campaign's own.
        consumed = evolve_module.journaled_authorization_ids(run_dir)
        try:
            decisions = read_decisions(run_dir)
        except AuthorizationError as exc:
            typer.echo(f"evo tree authorize: refused: {exc}", err=True)
            raise typer.Exit(2) from exc
        for decision in sorted(decisions, key=lambda item: (item.issued_at, item.id)):
            typer.echo(
                json.dumps(
                    {
                        "id": decision.id,
                        "code": decision.code,
                        "digest": decision.digest,
                        "reason": decision.reason,
                        "issued_at": decision.issued_at,
                        "status": "consumed" if decision.id in consumed else "pending",
                    },
                    sort_keys=True,
                )
            )
        return
    # Every refusal below precedes the one write, so a refused invocation leaves
    # no directory behind. The missing-argument cases are refusals rather than
    # Click usage errors for the same reason `evolve` declines `exists=True`:
    # a `UsageError` prints the absolute run directory (spec §9).
    try:
        if code is None:
            raise AuthorizationError("authorization_invalid", "code")
        if digest is None:
            raise AuthorizationError("authorization_invalid", "digest")
        if reason is None:
            raise AuthorizationError("authorization_invalid", "reason")
        decision = Authorization.new(code, digest, reason, effect=_item_effect(code, effect))
        write_decision(run_dir, decision)
    except AuthorizationError as exc:
        typer.echo(f"evo tree authorize: refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(decision.id)


#: I2 spec §4.2: the operator's own values for `--effect`; `"default"` is
#: never typed -- it is what a `run_identity_changed` item gets when the
#: operator names no effect.
_CLI_EFFECTS = ("continue", "rebaseline", "close")


def _item_effect(code: str, effect: str | None) -> DecisionEffect | None:
    """I2 §4.2: `--effect` beside a lone `run_identity_changed` item, `None` for any other code.

    The positional form (`CODE --digest ... --reason ...`) writes exactly one
    item, so "does this invocation have a `run_identity_changed` item" (ruling
    R6, see `_batch_effects`) reduces to "is this the one item's code":
    `--effect` is refused beside any other code, and beside a value it does
    not name (`authorization_invalid at --effect` either way). A
    `run_identity_changed` item accepted without `--effect` is written
    `effect: "default"` -- not left absent -- so `evolve` can tell an
    operator's answer made under this version (resolve `"default"` against
    the instance's admissible effects) from a decision written before I2
    (absent, meaning `close`, unchanged).

    Review round 1, ruling R4: this is safe for every `run_identity_changed`
    instance, comparable or not, because `Authorization.journal_payload()`
    never carries `effect` -- the one consequence shape with no `effect` slot
    of its own, `identity_rebased` (a non-comparable "rebase"), reads that
    payload and so never sees it. Only `decision_payload()` (the
    `authorization_applied` shapes that already carry an `effect` of their
    own -- close, terminal, fresh_baseline) carries the operator's `effect`
    forward.
    """

    if effect is not None and effect not in _CLI_EFFECTS:
        raise AuthorizationError("authorization_invalid", "--effect")
    if code != "run_identity_changed":
        if effect is not None:
            raise AuthorizationError("authorization_invalid", "--effect")
        return None
    return cast(DecisionEffect, effect or "default")


def _batch_effects(codes: Sequence[str], effect: str | None) -> list[DecisionEffect | None]:
    """I2 §4.2, review round 1 ruling R6: one `--effect` beside several `--accept` items.

    `--effect` attaches to every `run_identity_changed` item in `codes` and to
    no other item -- a hygiene (or other) item beside it is written without an
    effect, exactly as it always was, rather than refusing the whole line.
    It is refused, `authorization_invalid at --effect`, only when `codes` has
    no `run_identity_changed` item at all (nothing for it to attach to), or
    when it names a value it does not know. This is what Task 4's refusal
    printer needs: one combined `--accept ... --accept ... [--effect X]`
    command for a mixed refusal (hygiene findings plus one identity finding)
    must be acceptable in one call.
    """

    if effect is not None and effect not in _CLI_EFFECTS:
        raise AuthorizationError("authorization_invalid", "--effect")
    if effect is not None and "run_identity_changed" not in codes:
        raise AuthorizationError("authorization_invalid", "--effect")
    return [
        cast(DecisionEffect, effect or "default") if code == "run_identity_changed" else None
        for code in codes
    ]


def _authorize_accepted(
    run_dir: Path,
    accept: list[str],
    code: str | None,
    digest: str | None,
    reason: str | None,
    list_decisions: bool,
    effect: str | None,
) -> None:
    """`--accept CODE:DIGEST ...`: one decision per pair, with the shared reason (§6).

    Every decision is built -- and so validated -- before the first is written,
    so a hard code or a malformed digest anywhere on the line refuses the whole
    invocation and leaves no directory behind. A pair is `CODE:DIGEST` with
    exactly one `:`; the form does not combine with a positional code,
    `--digest` or `--list`, since a line that mixed them could be read two
    ways. One `--effect` applies to every `run_identity_changed` item on the
    line and no other item (I2 spec §4.2, ruling R6); it is refused only when
    no item on the line is `run_identity_changed`.
    """

    try:
        if code is not None or digest is not None or list_decisions:
            raise AuthorizationError("authorization_invalid", "--accept")
        if reason is None:
            raise AuthorizationError("authorization_invalid", "reason")
        pairs: list[tuple[str, str]] = []
        for item in accept:
            if item.count(":") != 1:
                raise AuthorizationError("authorization_invalid", "--accept")
            accepted_code, accepted_digest = item.split(":")
            pairs.append((accepted_code, accepted_digest))
        effects = _batch_effects([code for code, _ in pairs], effect)
        decisions = [
            Authorization.new(one, two, reason, effect=item_effect)
            for (one, two), item_effect in zip(pairs, effects, strict=True)
        ]
        for decision in decisions:
            write_decision(run_dir, decision)
    except AuthorizationError as exc:
        typer.echo(f"evo tree authorize: refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    for decision in decisions:
        typer.echo(decision.id)


@tree_app.command("trace")
def trace_command(
    # No Click `exists=True` on any path, as for `evolve`: a `UsageError`
    # would print the absolute path the operator typed, and spec §9 keeps
    # filesystem paths out of every refusal. `--out -` writes the trace to
    # stdout and the summary line to stderr.
    run_dir: Path = typer.Option(..., "--run-dir"),
    out: Path = typer.Option(..., "--out"),
    manifest: Path | None = typer.Option(None, "--manifest"),
    measurement: list[str] = typer.Option([], "--measurement"),
    max_field_bytes: int = typer.Option(65536, "--max-field-bytes", min=1),
) -> None:
    """Export one `evolution-v2-trace/1` JSON record per try of a campaign or evaluation run.

    Stdout is used only for `--out -`; otherwise the trace lands in the
    `--out` file, created owner-only (`0600`), and stderr carries the summary
    line (`<n> tries written · ... · <k> in flight`). With `--manifest` the
    corpus is re-bound and verified against the run's creation identity (exit
    2 `corpus_digest_mismatch` when it was edited after the run) and every
    scored try's request is checked against its scorer trace
    (`authenticated`/`mismatch`); without it, requests are `unauthenticated`
    or `absent`. A try whose rollout has no terminal record yet is omitted and
    counted as in flight. A refusal leaves nothing behind (exit 2,
    `evo tree trace: refused: <code>`); a journal that cannot be read exits 1
    (`evo tree trace: journal unavailable: <code>`) and any partial `--out`
    file is removed; a failure to create `--out` exits 1 with
    `evo tree trace: output unavailable: <errno name>` and no filesystem path.
    Nothing is ever written under the run directory.
    """
    from evolution.tree.run_reader import JournalUnreadable, RunRefused
    from evolution.tree.run_trace import (
        TraceOutputUnavailable,
        TraceRefused,
        export_run,
        summary_line,
    )
    from evolution.tree.storage.journal_errors import JournalBoundaryError

    try:
        summary = export_run(
            run_dir=run_dir,
            out=out,
            manifest=manifest,
            measurements=measurement,
            max_field_bytes=max_field_bytes,
        )
    except RunRefused as exc:
        typer.echo(f"evo tree trace: refused: {exc.code}", err=True)
        raise typer.Exit(2) from exc
    except TraceRefused as exc:
        typer.echo(f"evo tree trace: refused: {exc.code}", err=True)
        raise typer.Exit(2) from exc
    except (JournalBoundaryError, JournalUnreadable) as exc:
        typer.echo(f"evo tree trace: journal unavailable: {exc.code}", err=True)
        raise typer.Exit(1) from exc
    except TraceOutputUnavailable as exc:
        typer.echo(f"evo tree trace: output unavailable: {exc.code}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(summary_line(summary), err=True)
    raise typer.Exit(0)


@tree_app.command("watch")
def watch_command(
    run_dir: Path = typer.Option(..., "--run-dir"),
    once: bool = typer.Option(False, "--once"),
) -> None:
    """Print a run snapshot, then follow new records read-only until an ending."""
    from evolution.tree.campaign import CampaignFamilyUnsupported
    from evolution.tree.run_reader import JournalRewritten, JournalUnreadable, RunRefused
    from evolution.tree.run_watch import follow
    from evolution.tree.storage.journal_errors import JournalBoundaryError

    try:
        code = follow(run_dir, emit=typer.echo, once=once)
    except RunRefused as exc:
        typer.echo(f"evo tree watch: refused: {exc.code}", err=True)
        raise typer.Exit(2) from exc
    except CampaignFamilyUnsupported as exc:
        location = f" at {exc.path}; expected {exc.expected}" if exc.expected else ""
        typer.echo(f"evo tree watch: refused: {exc}{location}", err=True)
        raise typer.Exit(2) from exc
    except (JournalBoundaryError, JournalUnreadable, JournalRewritten) as exc:
        typer.echo(f"evo tree watch: journal unavailable: {exc.code}", err=True)
        raise typer.Exit(1) from exc
    raise typer.Exit(code)
