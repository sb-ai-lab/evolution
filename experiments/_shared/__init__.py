"""Shared helpers for the sb-bench pilot family.

Modules:

* ``preflight_common`` — env loading, smoke chat, endpoint healthcheck,
  HOME isolation gate. ``CheckResult`` / ``SmokeResult`` dataclasses.
* ``preflight_helpers`` — ``ok`` / ``fail`` / ``all_passed`` / ``summarize``
  on the plain-dict check shape used by older preflights.
* ``local_qwen_gate`` — concurrency gate for local sglang Qwen at
  ``localhost:30004``.
* ``training_subset`` — deterministic ``[(task_id, source_model_id, seed)]``
  selection for §5.2 trace-budget rows.
* ``workspace_materializer`` — copy a task tree into a clean workspace
  with the sb-bench layout adjustments.
* ``llm_telemetry`` — unified ``TelemetryRecord`` schema +
  ``JsonlRecorder`` + ``wrap_openai_client`` /
  ``patch_openai_class`` for cross-framework token+latency capture.
* ``pytest_runner`` — ``run_pytest_against_workspace`` for the sb-bench
  task layout.
* ``bootstrap`` — ``bootstrap_ci`` (rough 95%, returns None when n<3).
* ``subprocess_helpers`` — labelled ``run`` wrapper around
  ``subprocess.run``.
"""

from __future__ import annotations

from _shared.bootstrap import bootstrap_ci
from _shared.llm_telemetry import (
    JsonlRecorder,
    TelemetryRecord,
    patch_openai_class,
    read_telemetry_jsonl,
    wrap_openai_client,
)
from _shared.preflight_helpers import all_passed, fail, ok, summarize
from _shared.pytest_runner import run_pytest_against_workspace
from _shared.subprocess_helpers import run

__all__ = [
    "JsonlRecorder",
    "TelemetryRecord",
    "all_passed",
    "bootstrap_ci",
    "fail",
    "ok",
    "patch_openai_class",
    "read_telemetry_jsonl",
    "run",
    "run_pytest_against_workspace",
    "summarize",
    "wrap_openai_client",
]
