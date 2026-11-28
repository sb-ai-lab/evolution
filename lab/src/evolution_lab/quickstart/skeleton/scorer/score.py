"""Scorer: 1.0 when the agent's answer is the expected text, else 0.0.

The whole answer is compared with `oracle.expected` after `strip()` and
case-folding, so expected "50" does not match "150" or "50 cm".

Reads one JSON object on stdin (`case_id`, `public`, `oracle`, `view`) and
writes `{"metrics": ..., "critical_pass": ..., "verdicts": [...]}` on stdout.
The agent's answers are the `assistant_message` events of the turns in
`view["history"]["turns"]`.

TODO(you): replace the comparison with whatever "correct" means for your
agent. To accept a longer answer, use a substring check
(`expected.strip().casefold() in answer.casefold()`); for numbers, parse both
sides (`float(answer) == float(expected)`) so "50.0" matches "50". Keep the
`answered` verdict: it tells "no answer" from "wrong answer".
"""
from __future__ import annotations

import json
import sys


def _answer(view: dict) -> str:
    parts = []
    for turn in view.get("history", {}).get("turns", []):
        for event in turn.get("events", []):
            if event.get("type") == "assistant_message" and isinstance(event.get("content"), str):
                parts.append(event["content"])
    return "\n".join(parts)


def main() -> None:
    payload = json.load(sys.stdin)
    answer = _answer(payload["view"])
    expected = str(payload["oracle"]["expected"])
    answered = bool(answer.strip())
    matched = answered and answer.strip().casefold() == expected.strip().casefold()
    sys.stdout.write(
        json.dumps(
            {
                "metrics": {"score": 1.0 if matched else 0.0},
                "critical_pass": True,
                "verdicts": [
                    {"criterion": "answered", "passed": answered},
                    {"criterion": "matched", "passed": matched},
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
