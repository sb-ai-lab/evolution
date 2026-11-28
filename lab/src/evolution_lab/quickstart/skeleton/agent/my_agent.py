"""Your agent. This stub has no model and calls nothing outside this file.

TODO(you): in a real project this file imports your own agent instead of
containing it, for example `from my_package import Agent`, and `MyAgent`
wraps it so that `answer(question)` returns the final answer text.
"""
from __future__ import annotations

# The stub knows two of the three code words of corpus.jsonl; "gamma" is
# missing on purpose, so the first measurement shows both a 1 and a 0.
_KNOWN = {"alpha": "A1", "beta": "B2"}


class MyAgent:
    def __init__(self, prompt: str) -> None:
        # The prompt is seed/skills/SKILL.md as Lab hands it over. The stub uses
        # it for one thing: whether to answer with the code word alone.
        self._terse = "code word only" in prompt.lower()

    def answer(self, question: str) -> str:
        words = question.lower().replace("?", " ").split()
        code = next((_KNOWN[word] for word in words if word in _KNOWN), "unknown")
        return code if self._terse else f"The code word is {code}."
