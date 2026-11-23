import asyncio
import json
import subprocess
import sys

import pytest

from evolution.agentlab_protocol import _Failure, dispatch
from evolution.tree.roles.editor.chat_editor import read_evidence_page
from evolution.tree.roles.editor.editor_brief import EvidenceRecord


def test_installed_protocol_without_wheel(tmp_path):
    def invoke(operation, **payload):
        request = {
            "protocol": "agentlab-evolution/1",
            "operation": operation,
            "stage_id": "probe",
            **payload,
        }
        process = subprocess.run(
            [sys.executable, "-I", "-m", "evolution.agentlab_protocol"],
            cwd=tmp_path,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        response = json.loads(process.stdout)
        assert response["status"] == "ok", response
        return response["result"]

    capabilities = invoke("capabilities")
    assert len(capabilities["runtime_sha256"]) == 64
    assert capabilities["editor_evidence_reader"] == "read_evidence/1"
    record = {"case_id": "sample", "turns": [{"customer": "Hello", "assistant": "Hello back"}]}
    result = invoke(
        "read_editor_evidence",
        evidence_id="sample-turn",
        hypotheses=[{"evidence_ids": ["sample-turn"]}],
        editor_evidence={"references": {"sample-turn": "a" * 64}, "records": {"a" * 64: record}},
    )
    assert result["record"] == record


def test_reader_returns_long_record_without_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    record = {"case_id": "case", "turns": [{"customer": "x" * 150000, "assistant": "ok"}]}
    measurement = "a" * 64
    result = asyncio.run(
        dispatch(
            {
                "operation": "read_editor_evidence",
                "hypotheses": [{"evidence_ids": ["alias"]}],
                "editor_evidence": {
                    "references": {"alias": measurement},
                    "records": {measurement: record},
                },
                "evidence_id": "alias",
            }
        )
    )
    assert result == {"record": record, "editor_evidence_contract": "replay-records/1"}


def test_unknown_id_is_rejected():
    with pytest.raises(_Failure, match="unknown_evidence_id"):
        asyncio.run(
            dispatch(
                {
                    "operation": "read_editor_evidence",
                    "hypotheses": [],
                    "editor_evidence": {"references": {}, "records": {}},
                    "evidence_id": "unknown",
                }
            )
        )


def test_shared_reader_paginates():
    content = "line\n" * 20000
    catalog = (EvidenceRecord("record", content, aliases=("alias",)),)
    first = read_evidence_page(catalog, "alias", 0)
    assert first["next_offset"] is not None
    second = read_evidence_page(catalog, "alias", first["next_offset"])
    assert first["content"] + second["content"] == content
