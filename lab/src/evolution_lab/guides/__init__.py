"""Lab guides and the requirements catalogue shipped in the installed wheel."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

__all__ = ["GuideInfo", "catalogue_digest", "list_guides", "requirements", "root", "scenarios", "validate_catalogue"]

SCHEMA = "lab-requirements/1"
PATHS = ("campaign", "evaluation")
ANSWERS = ("code", "user", "decision", "run")
SEAMS = frozenset({"settings", "session", "trace", "backends"})
DOCUMENTS = frozenset({"manifest/15", "manifest/16", "evaluation-input/1", "evaluation-input/3", "launch/1", "launch/2", "bundle-request/2", "case"})
_TOP = frozenset({"schema", "catalogue_version", "digest", "paths", "items"})
_ITEM = frozenset({"id", "group", "applies_to", "obligation", "title", "why", "feeds", "answer_from", "guide"})
_ID = re.compile(r"[a-z]+\.[a-z_]+")
_GUIDE = re.compile(r"[a-z0-9-]+/SKILL\.md#[a-z0-9-]+")
_POINTER = re.compile(r"(/([^/~]|~[01])*)+")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_catalogue_load_lock = threading.Lock()


@dataclass(frozen=True)
class GuideInfo:
    name: str
    description: str
    path: str


def root() -> Path:
    files = resources.files(__name__)
    if not isinstance(files, Path):
        raise RuntimeError("Lab guides require an unpacked installation")
    return files


def catalogue_digest(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "digest"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _fail(message: str) -> None:
    raise ValueError(f"requirements catalogue: {message}")


def _text(value: Any, limit: int, where: str) -> None:
    if not isinstance(value, str) or not value or len(value) > limit or "\n" in value or value != value.strip():
        _fail(f"{where} must be a non-empty single-line trimmed string up to {limit} characters")


def _ordered(value: Any, allowed: tuple[str, ...], where: str) -> None:
    if not isinstance(value, list) or not value or any(v not in allowed for v in value):
        _fail(f"{where} must be a non-empty list of {allowed}")
    if value != [v for v in allowed if v in value]:
        _fail(f"{where} must list unique values in declared order")


def _feed(value: Any, where: str) -> None:
    if not isinstance(value, str):
        _fail(f"{where} must be a string")
    kind, sep, rest = value.partition(":")
    ok = bool(sep) and (
        (kind in DOCUMENTS and _POINTER.fullmatch(rest) is not None)
        or (kind == "seam" and rest in SEAMS)
        or (kind == "rule" and _GUIDE.fullmatch(rest) is not None)
        or (kind == "external" and rest == "operator")
    )
    if not ok:
        _fail(f"{where} has an unknown form: {value!r}")


def validate_catalogue(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != _TOP:
        _fail("top-level keys must be exactly " + ", ".join(sorted(_TOP)))
    if document["schema"] != SCHEMA:
        _fail("unsupported schema")
    version = document["catalogue_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        _fail("catalogue_version must be a positive integer")
    if not isinstance(document["digest"], str) or _DIGEST.fullmatch(document["digest"]) is None:
        _fail("digest must be 64 lowercase hex characters")
    if document["paths"] != list(PATHS):
        _fail("paths must be exactly campaign, evaluation")
    items = document["items"]
    if not isinstance(items, list) or not items:
        _fail("items must be a non-empty list")
    ids: list[str] = []
    for index, item in enumerate(items):
        where = f"items[{index}]"
        if not isinstance(item, dict) or set(item) != _ITEM:
            _fail(f"{where} keys must be exactly " + ", ".join(sorted(_ITEM)))
        if not isinstance(item["id"], str) or _ID.fullmatch(item["id"]) is None:
            _fail(f"{where}.id is malformed")
        if item["group"] != item["id"].split(".", 1)[0]:
            _fail(f"{where}.group must equal the id prefix")
        _ordered(item["applies_to"], PATHS, f"{where}.applies_to")
        if item["obligation"] not in ("required", "optional"):
            _fail(f"{where}.obligation must be required or optional")
        _text(item["title"], 80, f"{where}.title")
        _text(item["why"], 300, f"{where}.why")
        feeds = item["feeds"]
        if not isinstance(feeds, list) or not feeds or len(set(map(str, feeds))) != len(feeds):
            _fail(f"{where}.feeds must be a non-empty list without repeats")
        for position, feed in enumerate(feeds):
            _feed(feed, f"{where}.feeds[{position}]")
        _ordered(item["answer_from"], ANSWERS, f"{where}.answer_from")
        if not isinstance(item["guide"], str) or _GUIDE.fullmatch(item["guide"]) is None:
            _fail(f"{where}.guide is malformed")
        ids.append(item["id"])
    if ids != sorted(ids) or len(set(ids)) != len(ids):
        _fail("items must be unique and ordered by id")
    if catalogue_digest(document) != document["digest"]:
        _fail("digest does not match the catalogue")
    return document


@lru_cache(maxsize=None)
def _requirements_at(base: Path) -> dict[str, Any]:
    path = base / "requirements" / "requirements.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("requirements catalogue: unreadable") from error
    return validate_catalogue(document)


def requirements() -> dict[str, Any]:
    with _catalogue_load_lock:
        document = _requirements_at(root().resolve())
    return copy.deepcopy(document)


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:lines.index("---", 1)]:
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def list_guides() -> tuple[GuideInfo, ...]:
    base = root()
    found = []
    for path in sorted(base.glob("*/SKILL.md")):
        fields = _frontmatter(path.read_text(encoding="utf-8"))
        found.append(GuideInfo(fields.get("name", ""), fields.get("description", ""),
                               path.relative_to(base).as_posix()))
    return tuple(found)


@lru_cache(maxsize=None)
def _scenarios_at(base: Path):
    from ._scenarios import read_catalogue
    return read_catalogue(base / "scenarios")


def scenarios():
    """The scenario catalogue (spec §3.9): validated as a whole, never partially."""
    base = root()
    with _catalogue_load_lock:
        catalogue = _scenarios_at(base.resolve())
    return replace(copy.deepcopy(catalogue), base=base / "scenarios")
