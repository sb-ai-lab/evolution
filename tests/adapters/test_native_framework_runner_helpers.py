from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT / "src", REPO_ROOT / "experiments"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from _shared import native_framework_runners as nfr  # noqa: E402
from evolution.adapters.memento_adapter import write_llm_config  # noqa: E402


def test_evoskill_final_answer_falls_back_to_trace_result_json() -> None:
    code = "import pathlib\npathlib.Path('output/x.txt').write_text('ok')"
    trace = SimpleNamespace(
        output=None,
        result=json.dumps({"final_answer": code, "reasoning": "done"}),
    )

    final_answer, source, error = nfr._coerce_evoskill_final_answer(trace)

    assert final_answer == code
    assert source == "trace.result"
    assert error == ""


def test_evoskill_final_answer_falls_back_to_fenced_trace_result_json() -> None:
    code = "print('ok')"
    trace = SimpleNamespace(
        output=None,
        result="```json\n" + json.dumps({"final_answer": code}) + "\n```",
    )

    final_answer, source, error = nfr._coerce_evoskill_final_answer(trace)

    assert final_answer == code
    assert source == "trace.result"
    assert error == ""


def test_evoskill_final_answer_recovers_field_from_malformed_json_object() -> None:
    code = "import pathlib\\npathlib.Path('output/x.txt').write_text('ok')"
    malformed = '{"final_answer":"' + code + '","reasoning":"line one\nline two"}'
    trace = SimpleNamespace(output=None, result=malformed)

    final_answer, source, error = nfr._coerce_evoskill_final_answer(trace)

    assert final_answer == "import pathlib\npathlib.Path('output/x.txt').write_text('ok')"
    assert source == "trace.result.final_answer"
    assert error == ""


def test_memento_skill_mentions_extracts_executed_skills() -> None:
    text = "Calling execute_skill with name data_pipeline\nSelected skill 'pipelines'"

    assert nfr._extract_memento_skill_mentions(text) == ["data_pipeline", "pipelines"]


def test_memento_default_budget_matches_baseline_params(tmp_path: Path, monkeypatch) -> None:
    for name in (
        "EVO_MEMENTO_LLM_TIMEOUT_S",
        "EVO_MEMENTO_MAX_REACT_PER_STEP",
        "EVO_MEMENTO_MAX_REPLANS",
    ):
        monkeypatch.delenv(name, raising=False)

    write_llm_config(
        solver_model="openai/gpt-oss-20b",
        solver_base="https://openrouter.ai/api/v1",
        solver_key="key",
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_base="http://localhost:10225/v1",
        embedding_key="EMPTY",
        config_dir=tmp_path / "memento_s",
    )

    cfg = json.loads((tmp_path / "memento_s" / "config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["profiles"]["pilot"]["timeout"] == 240
    assert cfg["agent"]["max_react_per_step"] == 12
    assert cfg["agent"]["max_replans"] == 4
