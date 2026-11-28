"""Lab adapter: the module Lab loads (`launch.json` -> `agent`) to talk to your agent.

It has one function, `build(context)`, and one class with five methods.
Lab calls `build` once per target process; the factory it returns runs once
per instance and gets `view.root`, the version of the agent being measured.
Why each part is there: docs/guide/connect-agent.md, steps 2 and 3.

If your agent is a function `answer(question) -> str`, a first measurement
needs no change here: edit agent/my_agent.py and corpus.jsonl. The TODO(you)
marks below are for agents with their own settings, state or errors.
"""
from __future__ import annotations

from pathlib import Path

from evolution_lab.target import AgentTurn, TargetConfigurationError, TurnProvenance, TurnReason

# TODO(you), only if needed: this import is enough for `answer(question) -> str`.
from my_agent import MyAgent

_CONFORMANCE_REPLY = "conformance reply"
_PROMPT_PATH = "skills/SKILL.md"  # Relative to view.root, the measured version.


def build(context):
    # TODO(you), if your agent has settings: read declared settings from
    # context.env and credentials from context.secrets here, and pass them to
    # your agent.
    async def factory(view):
        prompt_file = Path(view.root) / _PROMPT_PATH
        try:
            prompt = prompt_file.read_text(encoding="utf-8")
        except OSError as error:
            # Broken configuration: every turn would fail the same way.
            raise TargetConfigurationError(f"cannot read {prompt_file.name} from the measured version") from error
        return MyTurnAgent(prompt)

    return factory


class MyTurnAgent:
    def __init__(self, prompt: str) -> None:
        self._agent = MyAgent(prompt)
        self._sessions: dict[str, dict[str, bool]] = {}  # state per session_id

    def describe(self):
        # TODO(you), optional: flat scalars that say how this instance was assembled; no secrets.
        return {"agent": "my-agent"}

    async def open(self, public, *, session_id, seed):
        # Lab's protocol self-test opens a session with {"conformance": True}
        # and must be answered without calling the agent (and any paid model).
        self._sessions[session_id] = {"conformance": public.get("conformance") is True}
        return {}

    async def turn(self, message, state, *, session_id, seed):
        session = self._sessions.get(session_id)
        if session is None:
            return AgentTurn("target_failed", "", reason=TurnReason("session", "unknown_session"))
        if session["conformance"]:
            return AgentTurn("ok", _CONFORMANCE_REPLY)
        try:
            # TODO(you): add a deadline around slow calls and return
            # target_failed when it fires (docs/guide/connect-agent.md, step 3).
            answer = self._agent.answer(message.content)
        except ValueError:
            # TODO(you): a defect of this agent version that would repeat on the
            # same input (malformed output, broken prompt) is candidate_failed.
            return AgentTurn("candidate_failed", "", reason=TurnReason("agent", "invalid_output"))
        except Exception:
            # TODO(you): timeouts, provider and network errors, anything you
            # cannot classify, are target_failed. Never put exception text in
            # the reason, only a short code.
            return AgentTurn("target_failed", "", reason=TurnReason("agent", "call_failed"))
        # TODO(you): report what your agent actually used on this turn, relative
        # to view.root. Do not report a prompt that your agent did not use.
        return AgentTurn("ok", answer, provenance=TurnProvenance(used=(_PROMPT_PATH,)))

    async def close_session(self, session_id):
        self._sessions.pop(session_id, None)

    async def aclose(self):
        self._sessions.clear()
