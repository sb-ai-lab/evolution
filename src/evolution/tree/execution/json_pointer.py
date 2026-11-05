"""Shared RFC-6901 pointer grammar and lookup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

MISSING: Final = object()


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise ValueError("invalid_json_pointer")
    tokens: list[str] = []
    for token in pointer[1:].split("/"):
        decoded: list[str] = []
        index = 0
        while index < len(token):
            char = token[index]
            if char != "~":
                decoded.append(char)
                index += 1
                continue
            if index + 1 == len(token) or token[index + 1] not in "01":
                raise ValueError("invalid_json_pointer")
            decoded.append("~" if token[index + 1] == "0" else "/")
            index += 2
        tokens.append("".join(decoded))
    return tuple(tokens)


def pointer_value(value: Any, pointer: str) -> Any:
    """Read a RFC-6901 pointer, returning :data:`MISSING` for absent paths."""

    current = value
    for token in _pointer_tokens(pointer):
        if isinstance(current, Mapping):
            if token not in current:
                return MISSING
            current = current[token]
        elif isinstance(current, (list, tuple)):
            if (
                not token.isascii()
                or not token.isdigit()
                or (token.startswith("0") and token != "0")
                or not current
            ):
                return MISSING
            highest_index = str(len(current) - 1)
            if len(token) > len(highest_index) or (
                len(token) == len(highest_index) and token > highest_index
            ):
                return MISSING
            index = int(token)
            current = current[index]
        else:
            return MISSING
    return current
