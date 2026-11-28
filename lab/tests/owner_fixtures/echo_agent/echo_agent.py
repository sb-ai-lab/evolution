"""Synthetic neutral agent for the Lab launcher tests: echoes the message."""
import asyncio
from itertools import count
from pathlib import Path
import threading

from evolution_lab.target import AgentTurn


class EchoAgent:
    def __init__(self, view, context, turns, turn_lock):
        self.mode = context.env.get("TARGET_MODE", "normal")
        self.turns, self.turn_lock = turns, turn_lock

    def describe(self):
        return {"agent": "echo", "mode": self.mode}

    async def open(self, public, *, session_id, seed):
        return {"turns": 0}

    async def turn(self, message, state, *, session_id, seed):
        with self.turn_lock:
            turn = next(self.turns)
        if self.mode.startswith("fail_after:"):
            _, successful_turns, flag = self.mode.split(":", 2)
            if turn > int(successful_turns) and Path(flag).exists():
                return AgentTurn("target_failed", "")
        if self.mode == "target_failed" or (
            self.mode.startswith("fail_while:")
            and Path(self.mode.split(":", 1)[1]).exists()
        ):
            return AgentTurn("target_failed", "")
        if self.mode.startswith("sleep:"):
            await asyncio.sleep(float(self.mode.split(":", 1)[1]))
        if self.mode.startswith("gate:"):
            gate = Path(self.mode.split(":", 1)[1])
            gate.with_suffix('.entered').touch()
            while not gate.exists():
                await asyncio.sleep(.01)
        return AgentTurn("ok", f"echo:{message.content}", state={"turns": state["turns"] + 1})

    async def close_session(self, session_id):
        if self.mode.startswith("close_sleep:"):
            await asyncio.sleep(float(self.mode.split(":", 1)[1]))
        return None

    async def aclose(self):
        return None


def build(context):
    # A materialized instance gets its own agent; the preflight allowance belongs
    # to the whole target process, shared with the later baseline instances.
    turns, turn_lock = count(1), threading.Lock()
    async def factory(view):
        return EchoAgent(view, context, turns, turn_lock)
    return factory
