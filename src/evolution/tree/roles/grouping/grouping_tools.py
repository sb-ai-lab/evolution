"""Read tools of the family-15 chat Grouper's map session (spec §6.1, ADR-0055).

The tools serve the grouping reader views the `agent_cli` Grouper uses
(`list_grouping_inputs`, `read_grouping_input`, and `read_trace` when the
campaign keeps traces), with the same visibility rules. They add only what a
portion session needs:

* the model names cases and observations by reference (`c<n>`, a repeat
  number), never by registry index or evidence ID; the toolbox maps them;
* a case or observation outside the portion is a `ToolError`;
* every result stays within `read_chars` characters of content, whatever page
  size the reader uses. Offsets are in the section's (or trace's) own
  coordinates, and `total_characters` lets the model page.

`finish_portion` is declared here and executed by the session's terminal
callback, never by `GroupingToolbox.run`.
"""

from __future__ import annotations

import json
import weakref
from collections.abc import Callable, Mapping
from typing import Any, Final

from evolution.tree.roles.editor.chat_editor import (  # noqa: PLC2701 - one tool loop, not two
    ToolError,
    _expect,
    _offset,
    _tool,
)
from evolution.tree.roles.grouping.grouping_portions import MapPortion, RefTable
from evolution.tree.roles.reflection_reader import (
    ReaderArgumentError,
    ReaderIntegrityError,
    ReflectionReadSession,
)

__all__ = ["RESULT_ENVELOPE_CHARS", "GroupingToolbox", "grouping_tools"]

#: Characters a tool result adds to a request beyond its `read_chars` of
#: content: the result's other fields (references, offsets, digest, totals)
#: and the tool message around it, as `chat_loop.chat_request_size` counts
#: them. `read_chars` bounds the content only; the caller reserves this too
#: (the session reserve is ``read_chars + RESULT_ENVELOPE_CHARS + ...``).
#: Content that JSON must escape (quotes, backslashes, control characters)
#: grows beyond its character count and is not covered: the session's
#: elision keeps such a request within `max_input_chars` instead.
RESULT_ENVELOPE_CHARS: Final = 512

_ENTRY_KEYS: Final = frozenset(
    {"index", "evidence_id", "case_index", "repeat", "available", "failing"}
)


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "description": description}


_CASE = _text("Case reference of this portion, such as c3.")
_REPEAT = _integer("Repeat number of the observation within the case.")
_OFFSET = _integer("Character (or entry) offset to continue from; default 0.")

_GROUP: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("Short local label, unique within the answer."),
        "title": _text("What the group is, at most 128 characters."),
        "failure_mode": _text("What failed and, as far as the evidence shows, why."),
        "expected_behaviour": _text("What the case expected instead."),
        "question": _text("What the Reflector should investigate."),
        "members": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": _text("An o<n> or s<n> reference of this portion."),
                    "variant": _text("Failure variant label within its case."),
                },
                "required": ["ref", "variant"],
                "additionalProperties": False,
            },
        },
        "citations": {"type": "array", "items": {"type": "string"}},
        "priority": _text("Why the group matters; possibly empty."),
        "metrics": {
            "type": ["object", "null"],
            "additionalProperties": {"type": "number"},
            "description": "Observed metric values by name.",
        },
        "provenance": _text("Reported component IDs of the members, when reported."),
    },
    "required": [
        "id",
        "title",
        "failure_mode",
        "expected_behaviour",
        "question",
        "members",
        "citations",
        "priority",
    ],
    "additionalProperties": False,
}


def grouping_tools(*, traces: bool) -> list[dict[str, Any]]:
    """The portion session's tool schemas; `list_case` is first, for the preflight."""

    tools = [
        _tool("list_case", {"case": _CASE, "offset": _OFFSET}, ["case"]),
        _tool(
            "read_observation",
            {
                "case": _CASE,
                "repeat": _REPEAT,
                "section": _text("Section name, such as observation, feedback or public."),
                "offset": _OFFSET,
            },
            ["case", "repeat", "section"],
        ),
    ]
    if traces:
        tools.append(
            _tool(
                "read_trace",
                {"case": _CASE, "repeat": _REPEAT, "offset": _OFFSET},
                ["case", "repeat"],
            )
        )
    tools.append(
        _tool(
            "finish_portion",
            {"groups": {"type": "array", "items": _GROUP}},
            ["groups"],
        )
    )
    return tools


#: One scan of the reader's entries per operation, shared by its portion sessions.
_ENTRIES: weakref.WeakKeyDictionary[ReflectionReadSession, list[dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


def _entries(reader: ReflectionReadSession) -> list[dict[str, Any]]:
    cached = _ENTRIES.get(reader)
    if cached is not None:
        return cached
    entries: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = reader.dispatch("list_grouping_inputs", {"offset": offset})
        entries.extend(page["entries"])
        offset = page["next_offset"]
    _ENTRIES[reader] = entries
    return entries


def _clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: max(chars, 0)]


class GroupingToolbox:
    """`list_case`, `read_observation` and `read_trace` for one portion session.

    ``glimpses`` maps evidence IDs to the glimpses the planner built;
    `list_case` shows them. ``read_trace`` is served only when the reader's
    operation was opened with that method.
    """

    def __init__(
        self,
        portion: MapPortion,
        table: RefTable,
        reader: ReflectionReadSession,
        *,
        read_chars: int,
        glimpses: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(read_chars, bool) or not isinstance(read_chars, int) or read_chars < 1:
            raise ValueError("invalid_read_chars")
        self._portion = portion
        self._table = table
        self._reader = reader
        self._read_chars = read_chars
        self._glimpses = glimpses or {}
        self._refs = portion.references()
        self._cases = {table.case_ref(case_id): case_id for case_id in portion.case_ids}
        self._o_ref = {eid: table.o_ref(i) for i, eid in enumerate(table.observations)}
        self._by_case: dict[str, dict[int, dict[str, Any]]] | None = None

    @property
    def traces(self) -> bool:
        return "read_trace" in self._reader.methods

    async def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One read tool call. `finish_portion` is the caller's terminal, never run here."""

        if name == "finish_portion":
            raise AssertionError(name)
        handler: Callable[[dict[str, Any]], dict[str, Any]]
        if name == "list_case":
            handler = self._list_case
        elif name == "read_observation":
            handler = self._read_observation
        elif name == "read_trace" and self.traces:
            handler = self._read_trace
        else:
            raise ToolError("unknown tool")
        try:
            return handler(arguments)
        except ReaderArgumentError as exc:
            raise ToolError(exc.code) from exc

    # --- Mapping --------------------------------------------------------------

    def _repeats(self, case: object) -> tuple[str, dict[int, dict[str, Any]]]:
        if not isinstance(case, str) or case not in self._cases:
            raise ToolError("unknown case reference")
        if self._by_case is None:
            # The portion's cases are known by the evidence IDs of their failing
            # observations; the reader numbers cases its own way.
            wanted = {eid for unit in self._portion.units for eid in unit.evidence_ids}
            entries = _entries(self._reader)
            positions: dict[int, str] = {}
            case_of = {
                eid: unit.case_id for unit in self._portion.units for eid in unit.evidence_ids
            }
            for e in entries:
                if e["evidence_id"] in wanted:
                    positions[e["case_index"]] = case_of[e["evidence_id"]]
            by_case: dict[str, dict[int, dict[str, Any]]] = {}
            for e in entries:
                case_id = positions.get(e["case_index"])
                if case_id is not None:
                    by_case.setdefault(case_id, {})[e["repeat"]] = e
            self._by_case = by_case
        return self._cases[case], self._by_case.get(self._cases[case], {})

    def _entry(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        _, repeats = self._repeats(arguments["case"])
        repeat = arguments["repeat"]
        if type(repeat) is not int or repeat not in repeats:
            raise ToolError("unknown repeat")
        return repeats[repeat], arguments

    # --- Paging ---------------------------------------------------------------

    def _repage(self, fetch: Callable[[int], Mapping[str, Any]], offset: int) -> dict[str, Any]:
        """The reader's pages from ``offset``, joined and cut to `read_chars`."""

        page = dict(fetch(offset))
        content = str(page["content"])
        while len(content) < self._read_chars and page["next_offset"] is not None:
            following = fetch(page["next_offset"])
            if not following["content"] or following["content_digest"] != page["content_digest"]:
                raise ReaderIntegrityError("source_changed")
            content += str(following["content"])
            page["next_offset"] = following["next_offset"]
        content = content[: self._read_chars]
        end = offset + len(content)
        page["content"] = content
        page["next_offset"] = end if end < page["total_characters"] else None
        return page

    # --- Tools ----------------------------------------------------------------

    def _list_case(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _expect(arguments, {"case"}, {"offset"})
        offset = _offset(arguments)
        case_id, repeats = self._repeats(arguments["case"])
        ordered = [repeats[number] for number in sorted(repeats)]
        if offset > len(ordered):
            raise ToolError("offset is past the last repeat")
        shown: list[dict[str, Any]] = []
        used = 0
        for entry in ordered[offset:]:
            item = self._item(entry)
            size = len(json.dumps(item, ensure_ascii=False))
            if shown and used + size > self._read_chars:
                break
            if not shown and size > self._read_chars and "glimpse" in item:
                excess = size - self._read_chars
                item["glimpse"] = _clip(item["glimpse"], len(item["glimpse"]) - excess)
            shown.append(item)
            used += size
        end = offset + len(shown)
        return {
            "case": arguments["case"],
            "repeats": shown,
            "total_repeats": len(ordered),
            "offset": offset,
            "next_offset": end if end < len(ordered) else None,
        }

    def _item(self, entry: dict[str, Any]) -> dict[str, Any]:
        failing = bool(entry["failing"]) and bool(entry["available"])
        state = "unavailable" if not entry["available"] else "failing" if failing else "passing"
        ref = self._o_ref.get(entry["evidence_id"]) if failing else None
        item: dict[str, Any] = {
            "repeat": entry["repeat"],
            "ref": ref,
            "state": state,
            "in_portion": ref is not None and ref in self._refs,
        }
        glimpse = self._glimpses.get(entry["evidence_id"])
        if glimpse is not None:
            item["glimpse"] = glimpse
        return item

    def _read_observation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _expect(arguments, {"case", "repeat", "section"}, {"offset"})
        offset = _offset(arguments)
        entry, _ = self._entry(arguments)
        section = arguments["section"]
        if not isinstance(section, str):
            raise ToolError("section must be a string")
        index = entry["index"]
        page = self._repage(
            lambda at: self._reader.dispatch(
                "read_grouping_input", {"index": index, "section": section, "offset": at}
            ),
            offset,
        )
        return {
            "case": arguments["case"],
            "repeat": arguments["repeat"],
            "section": section,
            **{key: value for key, value in page.items() if key != "evidence_id"},
        }

    def _read_trace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _expect(arguments, {"case", "repeat"}, {"offset"})
        offset = _offset(arguments)
        entry, _ = self._entry(arguments)
        evidence_id = entry["evidence_id"]
        page = self._repage(
            lambda at: self._reader.dispatch(
                "read_trace", {"evidence_id": evidence_id, "offset": at}
            ),
            offset,
        )
        return {
            "case": arguments["case"],
            "repeat": arguments["repeat"],
            **{key: value for key, value in page.items() if key != "evidence_id"},
        }
