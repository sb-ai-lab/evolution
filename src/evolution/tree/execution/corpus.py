"""Corpus for tree execution."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from evolution.tree.execution.interaction import EvalCase


class CorpusError(ValueError):
    """A `corpus.jsonl` row cannot be read as an `EvalCase`."""

    def __init__(self, code: str, line: int) -> None:
        super().__init__(f"{code} at line {line}")
        self.code = code
        self.line = line


_REQUIRED_CASE_KEYS: tuple[str, ...] = ("case_id", "group_id", "stratum", "public")


_OPTIONAL_CASE_KEYS: tuple[str, ...] = ("tags", "private", "oracle", "reflection_metadata")


_ALLOWED_CASE_KEYS = frozenset(_REQUIRED_CASE_KEYS + _OPTIONAL_CASE_KEYS)


_MAX_REFLECTION_METADATA_KEYS = 64


_MAX_REFLECTION_METADATA_BYTES = 16 * 1024


_MAX_REFLECTION_METADATA_DEPTH = 8


@dataclass(frozen=True)
class ReflectionCorpus:
    """Measurement cases plus separately-held diagnostic metadata for family 4."""

    cases: tuple[EvalCase, ...]
    metadata: Mapping[str, Mapping[str, Any]]
    metadata_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(
                {case_id: _freeze_metadata(value) for case_id, value in self.metadata.items()}
            ),
        )


def load_eval_corpus(path: Path) -> tuple[EvalCase, ...]:
    """Read `corpus.jsonl` into `EvalCase`s, keeping the file's order.

    One JSON object per line; blank lines are ignored but still counted, so a
    `CorpusError`'s line number always names the offending line of the file. A
    file that cannot be read at all — absent, a directory, not UTF-8 — is
    `unreadable` at line 0, never an `OSError` carrying the resolved path.
    """

    return _load_corpus(path, allow_reflection_metadata=False).cases


def load_reflection_corpus(path: Path) -> ReflectionCorpus:
    """Load cases and their optional diagnostic metadata in one pass.

    Metadata is never copied into :class:`EvalCase`; callers that construct a
    measurement identity from ``cases`` therefore retain the existing corpus
    and evaluation-set identities exactly.
    """

    return _load_corpus(path, allow_reflection_metadata=True)


def _load_corpus(path: Path, *, allow_reflection_metadata: bool) -> ReflectionCorpus:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError("unreadable", 0) from exc
    except UnicodeDecodeError as exc:
        raise CorpusError("unreadable", 0) from exc
    cases: list[EvalCase] = []
    metadata: dict[str, Mapping[str, Any]] = {}
    seen: set[str] = set()
    for number, row_text in enumerate(text.splitlines(), start=1):
        if not row_text.strip():
            continue
        case, row_metadata = _reflection_case(
            row_text, number, allow_reflection_metadata=allow_reflection_metadata
        )
        if case.case_id in seen:
            raise CorpusError("duplicate_case_id", number)
        seen.add(case.case_id)
        cases.append(case)
        metadata[case.case_id] = row_metadata
    if not cases:
        raise CorpusError("empty_corpus", 0)
    digest = hashlib.sha256(
        _canonical_metadata_json({case_id: metadata[case_id] for case_id in sorted(metadata)})
    ).hexdigest()
    return ReflectionCorpus(tuple(cases), metadata, digest)


def _reflection_case(
    text: str, line: int, *, allow_reflection_metadata: bool
) -> tuple[EvalCase, Mapping[str, Any]]:
    try:
        row = json.loads(text)
    except ValueError as exc:
        raise CorpusError("not_json", line) from exc
    if not isinstance(row, dict):
        raise CorpusError("not_object", line)
    for key in _REQUIRED_CASE_KEYS:
        if key not in row:
            raise CorpusError("missing_key", line)
    allowed_keys = (
        _ALLOWED_CASE_KEYS
        if allow_reflection_metadata
        else frozenset(_REQUIRED_CASE_KEYS + ("tags", "private", "oracle"))
    )
    if not set(row) <= allowed_keys:
        raise CorpusError("unknown_key", line)
    for key in ("case_id", "group_id", "stratum"):
        if not isinstance(row[key], str):
            raise CorpusError("bad_type", line)
    tags = row.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise CorpusError("bad_type", line)
    # `public` is required and already known present; `private` and `oracle` default to `{}`.
    mappings: dict[str, dict[str, Any]] = {}
    for key in ("public", "private", "oracle"):
        value = row.get(key, {})
        if not isinstance(value, dict):
            raise CorpusError("bad_type", line)
        mappings[key] = value
    metadata = _reflection_metadata(row.get("reflection_metadata", {}), line)
    try:
        case = EvalCase(
            case_id=row["case_id"],
            group_id=row["group_id"],
            stratum=row["stratum"],
            tags=tuple(tags),
            public=mappings["public"],
            private=mappings["private"],
            oracle=mappings["oracle"],
        )
    except ValueError as exc:
        raise CorpusError("bad_type", line) from exc
    return case, metadata


def _reflection_metadata(value: Any, line: int) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError("bad_type", line)
    if len(value) > _MAX_REFLECTION_METADATA_KEYS:
        raise CorpusError("bad_value", line)
    try:
        encoded = _canonical_metadata_json(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CorpusError("bad_type", line) from exc
    if (
        len(encoded) > _MAX_REFLECTION_METADATA_BYTES
        or _metadata_depth(value) > _MAX_REFLECTION_METADATA_DEPTH
    ):
        raise CorpusError("bad_value", line)
    return _freeze_metadata(value)


def _canonical_metadata_json(value: Any) -> bytes:
    return json.dumps(
        _metadata_payload(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _metadata_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _metadata_payload(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_metadata_payload(item) for item in value]
    if isinstance(value, list):
        return [_metadata_payload(item) for item in value]
    return value


def _metadata_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_metadata_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_metadata_depth(item) for item in value), default=0)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite metadata")
    return 0


def _freeze_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_metadata(item) for item in value)
    return value
