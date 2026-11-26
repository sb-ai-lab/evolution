"""Pages delivered during one direct chat role invocation on manifest /16."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from evolution.tree.execution.check_modes import RepeatedReadCheck
from evolution.tree.storage.redaction import safe_record

READ_TOOLS = frozenset({"read_evidence", "read_file"})
EVIDENCE_GUIDANCE = (
    "Omit offset to continue from the first unread character of the contiguous prefix. "
    "Set offset and reread=true for an exact quotation from an earlier page."
)
_FILE_GUIDANCE = (
    "Request a line range. Set reread=true to see the text of an already read range again."
)


def read_tool_contracts(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Offer the /16 read contract without modifying shared /15 definitions."""
    result = []
    for tool in tools:
        fn = tool["function"]
        if fn["name"] in READ_TOOLS:
            guidance = EVIDENCE_GUIDANCE if fn["name"] == "read_evidence" else _FILE_GUIDANCE
            params = fn["parameters"]
            properties = {**params["properties"], "reread": {"type": "boolean"}}
            tool = {
                **tool,
                "function": {
                    **fn,
                    "description": f"{fn['description']}. {guidance}",
                    "parameters": {**params, "properties": properties},
                },
            }
        result.append(tool)
    return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _Coverage:
    version: str
    total: int
    # Sorted, disjoint half-open intervals, each backed by an executed read.
    # There are at most as many intervals as the operator allows role turns.
    ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def prefix(self) -> int:
        return self.ranges[0][1] if self.ranges and self.ranges[0][0] == 0 else 0

    def add(self, start: int, end: int) -> None:
        if start == end:
            return
        merged: list[tuple[int, int]] = []
        for lower, upper in sorted([*self.ranges, (start, end)]):
            if merged and lower <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
            else:
                merged.append((lower, upper))
        self.ranges = merged


class ReadProgress:
    def __init__(
        self,
        finish_tool: str,
        *,
        max_chars: int,
        message_chars: int,
        redactions: tuple[str, ...] = (),
    ) -> None:
        self.finish_tool = finish_tool
        self._max_chars = max_chars
        # An excerpt gets one percent of the configured per-message budget.
        self._excerpt_chars = message_chars // 100
        self._redactions = redactions
        self._stored_chars = 0
        # The cursor survives page eviction. Sources are from the fixed catalog.
        self._cursors: dict[str, _Coverage] = {}
        self._pages: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._omitted = 0
        self._previous_short: tuple | None = None
        self._current_short: tuple | None = None

    def begin_turn(self) -> None:
        self._previous_short, self._current_short = self._current_short, None

    def repeated_without_progress(self) -> bool:
        return self._current_short is not None and self._current_short == self._previous_short

    def repair_message(self, receipt: dict[str, Any], check: RepeatedReadCheck) -> str | None:
        if receipt.get("status") in ("already_read", "eof") and check(
            self.repeated_without_progress
        ):
            return f"repeated_read_without_progress: {receipt['guidance']}"
        return None

    def offset(self, evidence_id: str) -> int:
        coverage = self._cursors.get(evidence_id)
        return coverage.prefix if coverage is not None else 0

    def completed(self, evidence_id: str) -> bool:
        coverage = self._cursors.get(evidence_id)
        return coverage is not None and coverage.prefix == coverage.total

    def deliver(
        self,
        name: str,
        arguments: dict[str, Any],
        output: dict[str, Any],
        *,
        reread: bool,
        omitted_offset: bool,
    ) -> dict[str, Any]:
        """Only successful dispatched pages reach the receipt index; no page body is retained."""
        if (
            name not in READ_TOOLS
            or not isinstance(output.get("content"), str)
            or "error" in output
        ):
            return output
        content = output["content"]
        if name == "read_evidence":
            source = arguments["evidence_id"]
            version = output["content_digest"]
            start = output.get("offset", arguments["offset"])
            end = start + len(content)
            total = (
                output["total_characters"]
                if "total_characters" in output
                else output["total_chars"]
            )
            coverage = self._cursors.get(source)
            if coverage is None or coverage.version != version:
                coverage = self._cursors[source] = _Coverage(version, total)
            coverage.add(start, end)
            cursor = coverage.prefix
            bounds = {"offset": start, "end_offset": end}
            following = {"next_offset": cursor if cursor < total else None}
            output = {**output, **bounds, "next_offset": end if end < total else None}
            if start == end == total:
                self._current_short = (name, source, version, start, end, omitted_offset, "eof")
                if reread:
                    self._current_short = None
                return {
                    **output,
                    "source": source,
                    "source_version": version,
                    "status": "eof",
                    "guidance": f"Choose another source, request a quotation, or call {self.finish_tool}.",
                }
        else:
            source = output["path"]
            version = output["sha256"]
            start, end = output["start_line"], output["end_line"]
            bounds = {"start_line": start, "end_line": end}
            following = {"next_line": end + 1 if end < output["total_lines"] else None}

        key = (name, source, version, start, end)
        visible = safe_record(content, self._redactions)
        digest = hashlib.sha256(visible.encode("utf-8")).hexdigest()
        known = self._pages.pop(key, None)
        page = {
            "tool": name,
            "source": source,
            "source_version": version,
            **bounds,
            "fragment_sha256": digest,
            "excerpt": visible[: self._excerpt_chars],
            **following,
        }
        if known is not None:
            self._stored_chars -= len(_json(known))
        self._stored_chars += len(_json(page))
        self._pages[key] = page
        while self._stored_chars > self._max_chars:
            _, expired = self._pages.popitem(last=False)
            self._stored_chars -= len(_json(expired))
            self._omitted += 1
        if not reread and known is not None and digest == known["fragment_sha256"]:
            self._current_short = (*key, omitted_offset, "already_read")
            return {
                "status": "already_read",
                **safe_record(page, self._redactions),
                "guidance": f"Continue, specify the range with reread=true, or call {self.finish_tool}.",
            }
        return {**output, "source_version": version, "fragment_sha256": digest}

    def summary(self, max_chars: int) -> str:
        """Fit the newest receipts, dropping excerpts before dropping any receipt."""
        if not self._pages and not self._omitted:
            return ""

        rows = []
        for page in self._pages.values():
            row = dict(page)
            if row["tool"] == "read_evidence":
                coverage = self._cursors[row["source"]]
                if coverage.version == row["source_version"]:
                    row["next_offset"] = (
                        None if coverage.prefix == coverage.total else coverage.prefix
                    )
            rows.append(safe_record(row, self._redactions))
        text = _json({"pages": rows, "omitted": self._omitted})
        if len(text) <= max_chars:
            return text
        # The display budget is separate from retention. Show recent receipts
        # without excerpts before omitting even the oldest metadata.
        for row in rows:
            del row["excerpt"]
        kept: list[dict[str, Any]] = []
        omitted = self._omitted + len(rows)
        chars = len(_json({"pages": [], "omitted": omitted}))
        if chars > max_chars:
            return ""
        for row in reversed(rows):
            extra = len(_json(row)) + bool(kept)
            if chars + extra > max_chars:
                break
            kept.append(row)
            chars += extra
            omitted -= 1
        return _json({"pages": kept[::-1], "omitted": omitted})
