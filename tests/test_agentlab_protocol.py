"""Exercise the AgentLab stage envelope and the public comparison contract."""

import hashlib
import io
import json
from zipfile import ZipFile

import pytest

from evolution import agentlab_protocol
from evolution.tree.roles.editor.chat_editor import ToolError, _Session
from evolution.tree.roles.reflector_contracts import ReflectResult
from tests.family15_fakes import v16_document
from tests.model_grouping_fakes import PortionGrouper
from tests.tree.manifest.test_manifest_v2 import RETRY, v11_chat_document, write_v5_document


def invoke(monkeypatch, capsys, request):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    agentlab_protocol.main()
    return json.loads(capsys.readouterr().out)


def envelope(operation):
    return {"stage_id": "test-stage", "protocol": "agentlab-evolution/1", "operation": operation}


def snapshot_id(files):
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    return hashlib.sha256(
        json.dumps(digests, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def test_capabilities_bind_version_and_stage(monkeypatch, capsys):
    request = {"stage_id": "probe", "operation": "capabilities", "protocol": "agentlab-evolution/1"}
    response = invoke(monkeypatch, capsys, request)
    assert response["stage_id"] == "probe"
    assert response["result"]["operations"] == ["group", "reflect", "prepare_candidates", "compare"]
    assert response["result"]["comparison_contract"] == "pinned-corpus/1"
    assert response["result"]["editor_evidence_contract"] == "replay-records/1"
    assert (
        invoke(monkeypatch, capsys, request | {"protocol": "unknown"})["error"]
        == "unsupported_protocol"
    )


def test_package_identity_tracks_runtime_resources_and_ignores_bytecode(tmp_path):
    package = tmp_path / "evolution"
    package.mkdir()
    prompt = package / "prompt.md"
    prompt.write_text("first")
    first = agentlab_protocol._package_contents(package)
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"generated")
    assert agentlab_protocol._package_contents(package) == first
    prompt.write_text("second")
    assert agentlab_protocol._package_contents(package) != first


def test_capabilities_verify_a_requested_wheel(tmp_path, monkeypatch, capsys):
    package = tmp_path / "evolution"
    package.mkdir()
    module = package / "agentlab_protocol.py"
    module.write_text("installed code")
    monkeypatch.setattr(agentlab_protocol, "__file__", str(module))
    archive = tmp_path / "release.whl"
    with ZipFile(archive, "w") as wheel:
        wheel.write(module, "evolution/agentlab_protocol.py")
    request = envelope("capabilities")
    installed = invoke(monkeypatch, capsys, request)["result"]
    assert len(installed["runtime_sha256"]) == 64
    pinned = request | {
        "wheel": str(archive),
        "wheel_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    assert invoke(monkeypatch, capsys, pinned)["result"] == installed
    module.write_text("different code")
    assert invoke(monkeypatch, capsys, pinned)["error"] == "wheel_changed"


def gate_request(tmp_path, score=1.0, violation=False):
    manifest = write_v5_document(tmp_path, v16_document())
    case = {"id": "case", "family": "family", "focus": True}
    corpus = json.dumps(case).encode()
    (tmp_path / "corpus.jsonl").write_bytes(corpus)
    snapshots = {"base": "a" * 64, "candidate": "b" * 64}
    schedule = [
        dict(side=side, case_id="case", repeat=1, slot=i) for i, side in enumerate(snapshots)
    ]
    rows = [
        slot
        | {
            "snapshot": snapshots[slot["side"]],
            "measurement_id": str(i) * 64,
            "critical": violation if i else False,
            "score": score if i else 0.0,
        }
        for i, slot in enumerate(schedule)
    ]
    return envelope("compare") | {
        "root": str(tmp_path),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(corpus).hexdigest(),
        "cases": [case],
        "focus": ["case"],
        "snapshots": snapshots,
        "schedule": schedule,
        "rows": rows,
    }


def test_group_preserves_public_dialogue_ids(monkeypatch, capsys):
    provider = PortionGrouper(budget=5)
    monkeypatch.setattr("evolution.agentlab_roles.new_grouping_client", provider.factory())
    dialogues = [
        {
            "id": name,
            "question": "A question",
            "judgment": "Incomplete",
            "answer": "Partial",
            "actions": None,
        }
        for name in ("sample-a", "sample-b")
    ]
    reply = invoke(
        monkeypatch,
        capsys,
        envelope("group")
        | {
            "dialogues": dialogues,
            "model": "test-model",
            "endpoint": "https://example.test/v1",
            "api_key": None,
        },
    )
    assert reply["status"] == "ok", reply
    assert [group["members"] for group in reply["result"]["groups"]] == [["sample-a", "sample-b"]]


@pytest.mark.parametrize(
    ("score", "violation", "outcome"),
    [
        (1.0, False, "passed"),
        (1.0, True, "comparison_not_passed"),
        (None, None, "comparison_unavailable"),
    ],
)
def test_gate_respects_missing_and_critical_measurements(
    tmp_path, monkeypatch, capsys, score, violation, outcome
):
    request = gate_request(tmp_path, score, violation)
    response = invoke(monkeypatch, capsys, request)
    assert response["status"] == "ok", response
    assert response["result"]["outcome"] == outcome
    changed = request | {"corpus_sha256": "0" * 64}
    assert invoke(monkeypatch, capsys, changed)["error"] == "invalid_measurements"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [("schedule", "repeat", True), ("rows", "repeat", 1.0), ("rows", "slot", False)],
)
def test_gate_requires_integer_repeat_and_slot(tmp_path, monkeypatch, capsys, section, key, value):
    request = gate_request(tmp_path)
    request[section][0][key] = value
    assert invoke(monkeypatch, capsys, request)["error"] == "invalid_measurements"


def prepare_request(tmp_path):
    editor = v11_chat_document(editor_retry=RETRY)["editor"]
    editor.update(context_compact_chars=128000, context_message_max_chars=12000)
    document = v16_document(editor=editor)
    document["artifacts"]["writable_paths"] = ["texts/prompt.md"]
    document["artifacts"]["dependency_paths"] = []
    document["role_visibility"]["readable_paths"] = []
    manifest = write_v5_document(tmp_path, document)
    files = {"prompt.md": "Задача\r\n".encode(), "private.md": b"private text\n"}
    texts = tmp_path / "seed" / "texts"
    texts.mkdir()
    for path, content in files.items():
        (texts / path).write_bytes(content)
    # The standard artifact exclusions also apply to the Editor's workspace.
    (tmp_path / "seed" / ".env").write_text("excluded text")
    return envelope("prepare_candidates") | {
        "root": str(tmp_path),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "parent": snapshot_id(files),
        "paths": ["prompt.md"],
        "hypotheses": [
            {
                "id": "hypothesis",
                "root_cause": "missing instruction",
                "suggested_fix": "add instruction",
                "evidence_ids": ["example"],
                "affected_cases": ["case"],
                "target_paths": ["prompt.md"],
            }
        ],
        "reflection_id": "reflection",
        "api_key": None,
    }


def test_prepare_checks_parent_before_editor(tmp_path, monkeypatch, capsys):
    request = prepare_request(tmp_path)
    (tmp_path / "seed" / "texts" / "prompt.md").write_text("changed after pinning")

    async def unexpected_edit(*args):
        pytest.fail("Editor called for a changed parent")

    monkeypatch.setattr(agentlab_protocol.ChatEditor, "edit", unexpected_edit)
    assert invoke(monkeypatch, capsys, request)["error"] == "parent_changed"


def test_prepare_editor_only_sees_visible_files(tmp_path, monkeypatch, capsys):
    request = prepare_request(tmp_path)

    async def session(self, edit, projection, log, attempts):
        files = {
            path.relative_to(projection.root).as_posix()
            for path in projection.root.rglob("*")
            if path.is_file()
        }
        assert files == {"texts/prompt.md"}
        (projection.root / "texts" / "prompt.md").write_bytes(b"edited\n")
        return "edited", "done", None, False

    monkeypatch.setattr(agentlab_protocol.ChatEditor, "_session", session)
    result = invoke(monkeypatch, capsys, request)
    assert result["status"] == "ok", result
    (candidate,) = result["result"]["candidates"]
    files = {"prompt.md": b"edited\n", "private.md": b"private text\n"}
    assert candidate["content_digest"] == snapshot_id(files)
    assert "private text" not in candidate["patch"]


def replay_evidence(request):
    request["hypotheses"][0]["evidence_ids"].append("second-unit")
    return {
        "references": {"example": "a" * 64, "second-unit": "a" * 64},
        "records": {
            "a" * 64: {
                "case_id": "bench-case",
                "turns": [{"customer": "Первый вопрос", "assistant": "Измеренный ответ"}],
            }
        },
    }


def test_prepare_exposes_selected_replay_under_both_cited_ids(tmp_path, monkeypatch, capsys):
    request = prepare_request(tmp_path)
    request["editor_evidence"] = replay_evidence(request)
    measurement = "a" * 64
    expected = {
        "case_id": "bench-case",
        "turns": [{"customer": "Первый вопрос", "assistant": "Измеренный ответ"}],
    }
    reached = []

    async def session(self, edit, projection, log, attempts):
        loop = _Session(self, edit, projection, log, client=None)
        catalog, _ = loop._run_tool("list_evidence", {})
        assert [record["id"] for record in catalog["records"]] == [
            "summary",
            "diagnosis",
            measurement,
        ]
        diagnosis, _ = loop._run_tool("read_evidence", {"evidence_id": "diagnosis"})
        assert "mass 1;" in diagnosis["content"]
        assert "evidence example, second-unit" in diagnosis["content"]
        for evidence_id in ("example", "second-unit", measurement):
            result, _ = loop._run_tool("read_evidence", {"evidence_id": evidence_id})
            assert json.loads(result["content"]) == expected
        with pytest.raises(ToolError, match="unknown evidence ID"):
            loop._run_tool("read_evidence", {"evidence_id": "unselected-unit"})
        reached.append(True)
        return "declined", "checked", None, False

    monkeypatch.setattr(agentlab_protocol.ChatEditor, "_session", session)
    response = invoke(monkeypatch, capsys, request)
    assert reached
    assert response["result"] == {
        "candidates": [],
        "reason": "editor_declined",
        "editor_evidence_contract": "replay-records/1",
    }


@pytest.mark.parametrize(
    ("section", "extra"),
    [
        ("references", {"example": []}),
        ("references", {"example": "b" * 64}),
        ("references", {"unselected": "a" * 64}),
        ("records", {"b" * 64: {"case_id": "private", "turns": []}}),
        ("records", {"a" * 64: {"case_id": "bench-case", "turns": []}}),
        (
            "records",
            {"a" * 64: {"case_id": "bench-case", "turns": [{"customer": 1, "assistant": "reply"}]}},
        ),
        (
            "records",
            {
                "a" * 64: {
                    "case_id": "bench-case",
                    "turns": [{"customer": "ask", "assistant": "reply", "oracle": "private"}],
                }
            },
        ),
    ],
)
def test_prepare_rejects_invalid_replay_evidence(tmp_path, monkeypatch, capsys, section, extra):
    request = prepare_request(tmp_path)
    evidence = replay_evidence(request)
    evidence[section].update(extra)
    request["editor_evidence"] = evidence

    async def unexpected_edit(*args):
        pytest.fail("Editor called with malformed replay evidence")

    monkeypatch.setattr(agentlab_protocol.ChatEditor, "edit", unexpected_edit)
    assert invoke(monkeypatch, capsys, request)["error"] == "invalid_editor_evidence"


@pytest.mark.parametrize("alias", ["summary", "attempt_feedback", "case:other", "bad/id", "a" * 64])
def test_prepare_rejects_reserved_or_colliding_replay_ids(tmp_path, monkeypatch, capsys, alias):
    request = prepare_request(tmp_path)
    evidence = replay_evidence(request)
    request["hypotheses"][0]["evidence_ids"][0] = alias
    evidence["references"][alias] = evidence["references"].pop("example")
    request["editor_evidence"] = evidence

    async def unexpected_edit(*args):
        pytest.fail("Editor called with an invalid evidence ID")

    monkeypatch.setattr(agentlab_protocol.ChatEditor, "edit", unexpected_edit)
    assert invoke(monkeypatch, capsys, request)["error"] == "invalid_editor_evidence"


@pytest.mark.parametrize("extra_path", [False, True])
def test_reflect_checks_snapshot_and_readable_paths(tmp_path, monkeypatch, capsys, extra_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    content = "Задача\r\n".encode()
    (parent / "prompt.md").write_bytes(content)
    names = {"prompt.md": content}
    if extra_path:
        (parent / "private.md").write_bytes(b"private text\n")
        names["private.md"] = b"private text\n"

    async def reflect(self, request):
        if extra_path:
            pytest.fail("Reflector called with an unregistered path in the snapshot")
        assert (request.workspace / "prompt.md").read_bytes() == content
        return ReflectResult("declined", (), "insufficient evidence")

    monkeypatch.setattr("evolution.agentlab_roles.ChatReflector.reflect", reflect)
    request = envelope("reflect") | {
        "root": str(tmp_path),
        "parent": snapshot_id(names),
        "paths": ["prompt.md"],
        "model": "test-model",
        "endpoint": "https://example.test/v1",
        "api_key": None,
        "cases": [{"unit_id": "example", "dialogue_id": "case"}],
        "problem": {"title": "a problem"},
        "requirements": [],
        "target_catalog": [{"path": "prompt.md"}],
    }
    response = invoke(monkeypatch, capsys, request)
    if extra_path:
        assert response["error"] == "invalid_snapshot"
    else:
        assert response["result"]["status"] == "insufficient-evidence"
