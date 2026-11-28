"""Compare report.md with captures made before dev72's markdown escaping.

dev72 (spec §7) renders report.md values with markdown escapes instead of HTML
entities, writes mapping and list values as ``key=value`` fields instead of
Python reprs, and adds the "без ответа: " count line. Older captures stay the
reference for everything else: both sides are reduced to plain text, so any
other difference still fails. Wave2 explicitly renames the manual operation
release command; legacy captures are mapped to its new primary spelling.
b22 adds optional operator progress fields and lines, checked separately.
"""
import ast
import html
import re

_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*\[\]<>|&_])")


def _plain(text: str) -> str:
    return _MARKDOWN_ESCAPE.sub(r"\1", text)


def current(text: str) -> str:
    """A dev72 report.md without the count line, escapes removed."""
    return _plain("\n".join(line for line in without_operator_progress(text).split("\n")
                            if not line.startswith("без ответа: ")))


def without_operator_progress(value):
    """Compare the earlier output while keeping its frozen capture unchanged."""
    if isinstance(value, str):
        prefixes = ("Рефлексия · ", "- Редактор ", "campaign suspended · ", "Reflection · ", "  Editor ")
        return "\n".join(line for line in value.split("\n")
                         if not line.startswith(prefixes)
                         and not re.match(r"(?:Раунд \d+ · открыто попыток |round \d+ · \d+ Attempts opened )", line))
    if "report" in value:
        report = value["report"]
        blocks = [report.get("lab_independent"), report["core"].get("independent")]
    else:
        blocks = [value.get("independent")]
    for block in blocks:
        if block is not None:
            for field in ("round_progress", "suspension"):
                block.pop(field, None)
            for attempt in block["attempts"]:
                attempt.pop("progress", None)
    return value


def with_b15_operator_progress(captured, *measurement_states):
    """Add the expected stage facts for the single-round b15 capture fixtures."""
    round_, = captured["rounds"]
    block = captured["independent"]
    block["round_progress"] = [{
        "round": round_["round"], "outcome": round_["outcome"], "reason": round_["reason"],
        "attempts_opened": len(measurement_states), "candidates_sealed": len(measurement_states),
        "attempts_measured": measurement_states.count("scored"),
        "reflections": [{"status": "reflected", "category": None, "reason": None, "suspended": False}],
    }]
    for attempt, state in zip(block["attempts"], measurement_states, strict=True):
        attempt["progress"] = {"editor": "edited", "editor_failures": [], "candidate_sealed": True,
                               "measurement": state, "parent_measurement": state}
    return captured


def _fields(value) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={_fields(item)}" for key, item in value.items()) or "—"
    if isinstance(value, list):
        return "; ".join(_fields(item) for item in value) or "—"
    return "—" if value is None else str(value)


_REPR = re.compile(r"[{\[]'.*?[}\]](?=$|[ .,;)])")


def _repr_fields(match: re.Match) -> str:
    try:
        value = ast.literal_eval(match.group(0))
    except (ValueError, SyntaxError):
        return match.group(0)
    return _fields(value)


def legacy(text: str) -> str:
    """A pre-dev72 capture: HTML entities and table escapes removed, Python reprs as fields."""
    text = text.replace("`lab release`", "`lab release-operation`")
    return _plain("\n".join(_REPR.sub(_repr_fields, line) for line in html.unescape(text).split("\n")))
