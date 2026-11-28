"""Installed S2 worker: public evolve plus conservative mutation settlement."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import threading

from evolution.tree import TargetCallLedger
from evolution.tree.target.http_adapter import HttpSessionAdapter

from .._manifest_schema import grouping_section
from .._suspension import suspension_hint
from . import core_raw
from .execution import validate_campaign_worker_config
from .settlement import CAMPAIGN_SETTLEMENT_SCHEMA, ledger_payload, settle_timeout
from .models import Failure
from .protocol import MAX_CONFIG_BYTES, read_frame
from .receipts import publish_json
from .secrets import _grouping_names
from ._worker_stderr import exception_record, exception_redactions, write_exception


_MIN_REDACTION_LENGTH = 8


def _grouping_forwarded_names(admitted, environ) -> tuple[str, ...]:
    workflow = admitted.workflow
    if workflow is None:
        return ()
    return tuple(name for name in sorted(_grouping_names(grouping_section(workflow))) if name in environ)


def _worker_redactions(admitted, environ, core_redactions) -> tuple[str, ...]:
    grouping = (
        environ[name]
        for name in _grouping_forwarded_names(admitted, environ)
        if len(environ[name]) >= _MIN_REDACTION_LENGTH
    )
    return tuple(dict.fromkeys((*core_redactions, *grouping)))


def _worker_short_forwarded_names(admitted, environ, core_names) -> tuple[str, ...]:
    grouping = (
        name
        for name in _grouping_forwarded_names(admitted, environ)
        if len(environ[name]) < _MIN_REDACTION_LENGTH
    )
    return tuple(sorted(set(core_names) | set(grouping)))


DIAGNOSTIC_SCHEMA = "lab-campaign-diagnostic/5"


def _publish_diagnostic(config, failure: Failure | None = None, refusal: dict | None = None,
                        core_terminal: dict | None = None, findings=(), *, exception=None) -> None:
    publish_json(
        config["diagnostic_path"],
        {
            "schema": DIAGNOSTIC_SCHEMA,
            "operation_id": config["operation_id"],
            "scope_id": config["scope_id"],
            "failure": None if failure is None else failure.to_payload(),
            "refusal": refusal,
            "core_terminal": core_terminal,
            "findings": list(findings),
            "exception": exception,
        },
    )


def refusal_payload(error, redactions) -> dict:
    """The first finding of a core refusal, as Owner shows it (ADR-0023); class as `_write_refusal` computes it."""
    from evolution.tree import refusal_class
    from evolution.tree import safe_record

    finding = error.findings[0]
    kind = "hard" if finding.instance_digest is None else refusal_class(finding.code)
    return {
        "code": safe_record(finding.code, redactions),
        "path": None if finding.path is None else safe_record(str(finding.path), redactions),
        "class": kind,
        "instance_digest": finding.instance_digest,
    }


def _write(stream, value) -> None:
    stream.write(value)
    stream.flush()


def refusal_findings(error, redactions) -> tuple[dict, ...]:
    from evolution.tree import effect_sentence, refusal_class
    from evolution.tree import safe_record

    rows = []
    for finding in error.findings:
        effects = finding.effects
        texts = []
        for effect in effects:
            try:
                texts.append(" ".join(effect_sentence(effect)))
            except KeyError:
                # Some effects (hygiene, rebase) have no explanatory sentence in Core.
                texts.append("")
        rows.append(safe_record({"code": finding.code, "path": finding.path,
            "class": "hard" if finding.instance_digest is None else refusal_class(finding.code),
            "instance_digest": finding.instance_digest, "effects": list(effects),
            "effect_texts": texts, "differs": [
                f"{d.field}  journaled {d.journaled}  observed {d.observed}"
                for d in finding.differences]}, redactions))
    return tuple(rows)


async def _evolve(config, environ, cancelled, admitted, redactions, short_names):
    import evolution.tree.evolve as evolve_module
    from evolution.tree import safe_record

    for name in short_names:
        try:
            _write(
                sys.stderr,
                "evo tree evolve: note: forwarded value " + name
                + " is shorter than 8 characters and is not redacted\n",
            )
        except OSError:
            pass

    def notes(text: str) -> None:
        prefix = "evo tree evolve: " if text.startswith("check shadowed: ") else "evo tree evolve: note: "
        try:
            _write(
                sys.stderr,
                safe_record(
                    prefix + text + "\n", redactions
                ),
            )
        except OSError:
            pass

    def say(text: str) -> None:
        try:
            _write(sys.stderr, safe_record("evo tree evolve: " + text + "\n", redactions))
        except OSError:
            pass

    if admitted.workflow is not None:
        for role, base_url, credential_declared in evolve_module.unencrypted_model_roles(admitted.workflow):
            text = (f"{role} sends its credential unencrypted to {base_url}" if credential_declared
                    else f"{role} talks to {base_url} without encryption")
            say("WARNING: " + text)
        for role, base_url in evolve_module.unauthenticated_roles(admitted.workflow):
            say(f"{role} calls {base_url} without credentials (no api_key_env declared)")
        for line in core_raw.check_mode_notes(admitted.workflow):
            notes(line)

    ledger = TargetCallLedger()
    normal_return = False

    def factory(manifest, run_id):
        if run_id != config["run_id"]:
            raise ValueError("campaign run identity mismatch")
        return HttpSessionAdapter(manifest.target, manifest.policy, run_id=run_id, ledger=ledger)

    try:
        result = await evolve_module.evolve(
            admitted,
            Path(config["run_directory"]),
            cancelled=cancelled,
            adapter_factory=factory,
            notes=notes,
            environ=environ,
            ledger=ledger,
            decision_directory=True,
        )
        normal_return = True
    finally:
        ledger.close()
        settled = await ledger.settle(settle_timeout(admitted.target))
        try:
            publish_json(config["settlement_path"], {
                "schema": CAMPAIGN_SETTLEMENT_SCHEMA,
                "scope_id": config["scope_id"],
                "owner_generation": config["owner_generation"],
                "operation_id": config["operation_id"],
                "run_id": config["run_id"],
                "normal_return": normal_return,
                "ledger": ledger_payload(settled),
            })
        except (OSError, ValueError):
            pass
    chosen = evolve_module.exit_code_for(result)
    if "diagnostic_path" in config:
        terminal = None if (result.terminal in {"finished", "suspended"}
                            and result.code != "projection_invalid") else {
            "owner": result.owner, "terminal": result.terminal, "code": result.code}
        try:
            _publish_diagnostic(config, core_terminal=safe_record(terminal, redactions))
        except (OSError, ValueError):
            pass
    document = {
        "terminal": result.terminal,
        "quiescent": result.quiescent,
        "finished": result.finished,
        "owner": result.owner,
        "code": result.code,
        "result": None if result.result is None else result.result.to_payload(),
        "status": result.status.to_payload(),
    }
    try:
        _write(
            sys.stdout,
            json.dumps(
                safe_record(document, redactions), default=str
            ) + "\n",
        )
    except OSError:
        pass
    # Keep the existing process-log repair hint beside the structured diagnostic.
    hint = suspension_hint(result.terminal, "en", result.code)
    if hint is not None:
        say("hint: " + hint)
    for line in result.provider_attempt_lines:
        say(line)
    return chosen


def _write_refusal(error, redactions) -> None:
    """Core's structured refusal fields, one block per finding (a /14 policy refusal adds core's `expected …` statement), without the authorize hint.

    Lab presents the matching authorization action through its own surfaces
    (PDR-0055). Every effect a decision may
    choose is listed, default first (a21), then each effect's sentences. The class is the
    instance's, as core's CLI computes it: a finding without an instance
    digest is hard whatever its code. Core's hard-reason sentence is chosen
    by a private helper and is not reproduced.
    """
    from evolution.tree import refusal_class
    from evolution.tree import effect_sentence
    from evolution.tree import safe_record

    lines = []
    for finding in error.findings:
        where = finding.code if finding.path is None else f"{finding.code} at {finding.path}"
        lines.append(f"refused: {where}")
        if getattr(finding, "expected", None):
            # A /14 policy refusal's fixed statement of the accepted domain; core's CLI prints it after `refused:`.
            lines.append(finding.expected)
        lines.extend(
            f"differs: {difference.field}  journaled {difference.journaled}  "
            f"observed {difference.observed}"
            for difference in finding.differences
        )
        kind = "hard" if finding.instance_digest is None else refusal_class(finding.code)
        lines.append(f"class: {kind}")
        if finding.instance_digest is not None:
            lines.append(f"instance_digest: {finding.instance_digest}")
        if kind == "authorizable":
            effects = tuple(getattr(finding, "effects", ()) or ((finding.effect,) if finding.effect else ()))
            if effects:
                lines.append("effects: " + ", ".join(
                    f"{effect} (default)" if index == 0 else effect for index, effect in enumerate(effects)))
            for effect in effects:
                try:
                    lines.extend(effect_sentence(effect))
                except KeyError:
                    pass
    for line in lines:
        _write(sys.stderr, safe_record(f"evo tree evolve: {line}\n", redactions))


def check_run_directory(run: Path, mode: str) -> None:
    """New: a fresh empty directory. Continue: core's campaign journal present (ADR-0015)."""
    if run.is_symlink() or not run.is_dir():
        raise ValueError("campaign run directory is unusable")
    if mode == "retry":
        # A refused first start has no journal. Core admits its existing inputs/decisions.
        return
    if mode == "new":
        if any(run.iterdir()):
            raise ValueError("campaign run directory is not fresh")
        return
    journal = run / "campaign.jsonl"
    if journal.is_symlink() or not journal.is_file():
        raise ValueError("campaign run directory holds no campaign")


def run_worker(config, environ, cancelled) -> int:
    """Run one admitted campaign; the entrypoint alone uses ``os._exit``."""
    import evolution.tree.evolve as evolve_module
    from evolution.tree import EvolveRefused

    redactions = ()
    log_redactions = None
    try:
        from .core_entry import publish_core_entry
        # A failed publication prevents admission, including after a worker crash.
        publish_core_entry(config)
        admitted = evolve_module.admit_manifest(Path(config["manifest_path"]))
        redactions = _worker_redactions(
            admitted, environ, evolve_module.redactions_for(admitted, environ)
        )
        log_redactions = exception_redactions(environ, redactions)
        short_names = _worker_short_forwarded_names(
            admitted, environ, evolve_module.short_forwarded_names(admitted, environ)
        )
        return asyncio.run(
            _evolve(config, environ, cancelled, admitted, redactions, short_names)
        )
    except EvolveRefused as error:
        try:
            _write_refusal(error, redactions)
        except OSError:
            pass
        if "diagnostic_path" in config:
            try:
                _publish_diagnostic(config, refusal=refusal_payload(error, redactions),
                                    findings=refusal_findings(error, redactions))
            except (OSError, ValueError):
                pass
        return 2
    except BaseException as error:
        write_exception(error, log_redactions)
        try:
            _publish_diagnostic(config, Failure.make("lab_error", "running"), exception=exception_record(error))
        except (OSError, ValueError):
            pass
        return 1


def main() -> None:
    chosen = 1
    config = None
    stopping = threading.Event()

    def request_stop(_number, _frame):
        stopping.set()

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, request_stop)
    try:
        raw_fd = os.environ.pop("LAB_CAMPAIGN_CONFIG_FD")
        config_fd = int(raw_fd)
        os.set_inheritable(config_fd, False)
        environ = dict(os.environ)
        try:
            config = validate_campaign_worker_config(
                read_frame(config_fd, max_bytes=MAX_CONFIG_BYTES)
            )
        finally:
            os.close(config_fd)
        run = Path(config["run_directory"])
        check_run_directory(run, config["mode"])
        chosen = run_worker(config, environ, stopping.is_set)
    except BaseException as error:
        write_exception(error, None)
        if config is not None:
            try:
                _publish_diagnostic(config, Failure.make("lab_error", "before_start"), exception=exception_record(error))
            except (OSError, ValueError):
                pass
        chosen = 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except BaseException:
            pass
    os._exit(chosen)


if __name__ == "__main__":
    main()
