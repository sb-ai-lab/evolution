"""Scripted OpenAI-compatible endpoint for the content snapshot (plan 2026-10-13 decision 1).

Drives a34's real chat role loops on loopback without a paid provider. Wiring only, measures nothing.
"""
from __future__ import annotations

import http.server
import json
import threading

KEY = "fake-chat-key-0123456789abcdef"


def _reply(call_name: str | None, arguments: dict | None = None, content: str = "") -> dict:
    message = {"role": "assistant", "content": content}
    if call_name is not None:
        message["tool_calls"] = [{"id": f"call-{call_name}", "type": "function",
                                  "function": {"name": call_name, "arguments": json.dumps(arguments or {})}}]
    return {"id": "fake", "object": "chat.completion", "created": 0, "model": "fake-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if call_name else "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _tool_results(messages: list) -> list:
    opening = next((i for i, m in enumerate(messages) if m.get("role") == "user"), -1)
    out = []
    for message in messages[opening + 1:]:
        if message.get("role") == "tool":
            try:
                out.append(json.loads(message.get("content") or "null"))
            except ValueError:
                out.append(message.get("content"))
    return out


def _writable(result: dict) -> str:
    # a34 list_files has files and summaries, not writable flags. The demo admits skills/**.
    files = [path for path in result["files"] if path.startswith("skills/")]
    return next((path for path in files if path.endswith(".md")), files[0])


def _file_text(result: dict) -> str:
    return result["content"]


def _observation(result: dict) -> dict | None:
    # The adaptive a34 reader returns observation headers after filtering by a case_handle.
    entry = next((entry for entry in result["entries"]
                  if entry["relation"] == "observation" and entry["available"]), None)
    return None if entry is None else {"id": entry["evidence_id"]}


class FakeChat:
    def __init__(self):
        self.requests, self.editor_sessions = [], 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                payload = json.dumps(fake.answer(body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def answer(self, body: dict) -> dict:
        tools = [tool["function"]["name"] for tool in body.get("tools") or ()]
        if tools == ["list_files"]:
            return _reply("list_files", {})
        results = _tool_results(body["messages"])
        if "finish_reflection" in tools:
            return self._reflector(results)
        if not results:
            self.editor_sessions += 1
        return self._editor(results)

    def _editor(self, results: list) -> dict:
        if self.editor_sessions >= 2:
            return _reply(None, content="no tool call")
        if not results:
            return _reply("list_files", {}, content="Looking at the workspace.")
        path = _writable(results[0])
        if len(results) == 1:
            return _reply("record_plan", {"summary": "append a note", "changes": [
                {"path": path, "action": "update", "purpose": "wiring"}]})
        if len(results) == 2:
            return _reply("read_file", {"path": path})
        if len(results) == 3:
            return _reply("write_file", {"path": path, "content": _file_text(results[2]) +
                                        "\n<!-- fake chat editor -->\n"})
        return _reply("finish_edit", {"summary": "appended a note"})

    def _reflector(self, results: list) -> dict:
        if not results:
            return _reply("list_files", {})
        if len(results) == 1:
            return _reply("list_evidence", {})
        if len(results) == 2:
            return _reply("finish_reflection", {"groups": [{}]})
        case = next((entry for entry in results[1]["entries"] if entry["available"]), None)
        if case is None:
            return _reply("finish_reflection", {"groups": [], "declined": True})
        if len(results) == 3:
            return _reply("list_evidence", {"case_id": case["case_handle"]})
        evidence = _observation(results[3])
        if evidence is None:
            return _reply("finish_reflection", {"groups": [], "declined": True})
        if len(results) == 4:
            return _reply("read_evidence", {"evidence_id": evidence["id"]})
        return _reply("finish_reflection", {"groups": [{
            "root_cause": "wiring", "suggested_fix": "wiring", "counterevidence": "none",
            "regression_risks": "none", "why_this_layer": "wiring", "evidence_ids": [evidence["id"]],
            "affected_cases": [case["case_id"]], "target_paths": [_writable(results[0])]}]})
