"""Opt-in, time-bounded CLI reach checks; output stays in pytest's tmp_path (tmpfs).

Select exactly one vendor (``codex`` or ``hermes``), for example::

    EVO_AGENT_GROUPER_LIVE_VENDOR=codex pytest -m live tests/tree/roles/grouping/test_agent_grouper_live.py

The 150-second deadline bounds time only. There is no hard token/spend ceiling
or guaranteed monetary ceiling; max_turns checks admission after CLI completion.
A pass requires all three installed launchers to reach the actual mediator and
an admitted answer. A fallback or an agent's claim of success is insufficient.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

import pytest

from evolution.eval.child_env import _RUNTIME_ENV_KEYS
from evolution.llm.agent_backend import AgentBackend
from evolution.tree.bindings import bound_child_environment
from evolution.tree.roles.config import AgentCliEditorSection, AgentCliGrouperSection
from evolution.tree.roles.grouping.agent_grouper import GROUPING_METHODS, AgentCliGrouper
from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
from evolution.tree.roles.seals import seal_program, seal_prompt
from evolution.tree.roles.workspace import Budgets
from tests.campaign_fakes import PRIMARY
from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

_PROBE_REQUIREMENT = """
For this reach check, invoke each declared helper argv with one JSON argument:
list_grouping_inputs {"offset":0}, read_grouping_input
{"index":0,"section":"identity","offset":0}, and stream_grouping_inputs
{"start":0,"stop":1,"sections":["feedback"]}. The stream is JSON Lines.
Use the returned observations to produce the required GROUPS.json. If a helper
cannot run, report that limit; do not invent observations or bypass safe mode.
"""


@contextmanager
def _live_inputs(root: Path):
    # Construct the synthetic scorer result before freezing its reader. This
    # corpus has one case and no split labels, so every observation is train.
    trace = _Trace(root, turns=1)
    try:
        stdout = trace.directory / "stdout"
        score = json.loads(stdout.read_text())
        score["verdicts"] = [{"criterion": "checks_x", "passed": False, "note": "missing"}]
        stdout.write_text(json.dumps(score), encoding="utf-8")
        inputs = FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=(_reflect_row("s1", 1),),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=(trace.case,),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
        assert any(record.failing and record.available for record in inputs.registry.records)
        yield inputs
    finally:
        trace.close()


@pytest.mark.parametrize("vendor,model", [("codex", "codex/gpt-5.5"), ("hermes", "hermes")])
async def test_safe_agent_grouping_live(vendor: str, model: str, tmp_path: Path) -> None:
    selected = os.environ.get("EVO_AGENT_GROUPER_LIVE_VENDOR")
    if selected not in {"codex", "hermes"}:
        pytest.skip(
            "Set EVO_AGENT_GROUPER_LIVE_VENDOR=codex or hermes to opt in to one vendor; "
            "150s bounds time only, with no hard token/spend ceiling"
        )
    if vendor != selected:
        pytest.skip("only the explicitly selected live vendor may run")
    executable = shutil.which(vendor)
    if executable is None:
        pytest.skip(f"{vendor} binary unavailable")
    # Transcripts and generated source stay in pytest's tmp_path on tmpfs,
    # never in the checkout. Never persist host settings.
    root = tmp_path / vendor
    root.mkdir()
    projection = root / "projection"
    projection.mkdir()
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=15, check=False
    )
    role = AgentCliEditorSection(
        model=model,
        permission="safe",
        agent_bin=Path(executable),
        reasoning_effort="low",
        max_turns=12 if vendor == "codex" else None,
        budgets=Budgets(None, 1 << 20, 8192),
    )
    environment = bound_child_environment(role, os.environ)
    secrets = tuple(value for name, value in environment.items() if name not in _RUNTIME_ENV_KEYS)

    def redact(value: str) -> str:
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value

    processes: list[dict[str, object]] = []

    class CapturingBackend(AgentBackend):
        async def _run(self, invocation, timeout):
            # Observe the real invocation without replacing the process,
            # sandbox, helper transport or vendor tools.
            result = await super()._run(invocation, timeout)
            timed_out, returncode, stdout, stderr, elapsed_ms = result
            processes.append(
                {
                    "timed_out": timed_out,
                    "returncode": returncode,
                    "elapsed_ms": elapsed_ms,
                    "mode": invocation.diagnostics_seed,
                }
            )
            (root / f"process-{len(processes)}.json").write_text(
                json.dumps(
                    {"stdout": redact(stdout), "stderr": redact(stderr)}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            return result

    prompt = files("evolution.prompts").joinpath("tree_agent_grouper.md").read_text()
    agent = AgentCliGrouper(
        section=AgentCliGrouperSection(role=role, timeout_seconds=150),
        program=seal_program(Path(executable)),
        prompt=seal_prompt(None, packaged=prompt + _PROBE_REQUIREMENT),
        child_env=environment,
        log_root=root / "logs",
        secrets=secrets,
        backend_factory=CapturingBackend,
    )
    started = time.monotonic()
    failure = None
    outcome = None
    with _live_inputs(root) as inputs:
        with inputs.reader_view().open_operation("live-grouping", set(GROUPING_METHODS)) as reader:
            try:
                outcome = await agent.run(
                    inputs.registry,
                    reader=reader,
                    control_root=projection,
                    operation="live-grouping",
                    policy_identity="synthetic-live-probe",
                    parent_digest=None,
                    verify_admission=inputs.verify_admission,
                    cancelled=lambda: False,
                    forbidden_secret=lambda text: any(
                        secret in text for secret in secrets if secret
                    ),
                    oracle_needle=lambda _text: False,
                )
            except Exception as exc:
                # Do not put provider error strings or account details into
                # pytest output. Inspect the ignored artifacts for diagnosis.
                failure = {"type": type(exc).__name__, "reason": getattr(exc, "reason", None)}
            activity = reader.summary.to_payload()
        successful = {
            entry["operation"]
            for entry in activity["entries"]
            if "response_digest" in entry and "error" not in entry
        }
        summary = {
            "vendor": vendor,
            "version": version.stdout.splitlines()[0] if version.stdout else "unavailable",
            "version_returncode": version.returncode,
            "model": model,
            "permission": "safe",
            "deadline_seconds": 150,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "processes": processes,
            "failure": failure,
            "reads": activity,
            "calls": dict(outcome.calls) if outcome is not None else None,
            "fallback": outcome.fallback.reason if outcome and outcome.fallback else None,
        }
        (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        if failure is not None:
            pytest.fail(
                f"{vendor} safe probe failed; inspect {root / 'summary.json'}", pytrace=False
            )
        assert GROUPING_METHODS <= successful, f"missing successful helpers; inspect {root}"
        assert outcome is not None and outcome.fallback is None, f"fallback; inspect {root}"
        assert (projection / ".evolution/GROUPS.json").is_file()
        assert all(
            (assignment is not None) == (record.failing and record.available)
            for record, assignment in zip(
                inputs.registry.records, outcome.grouped.result.assignments, strict=True
            )
        )
