"""Validated train/test partitions, kept outside role-facing case payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.tree.execution.corpus import CorpusError, ReflectionCorpus, _reflection_case
from evolution.tree.execution.journal_identity import canonical_json, corpus_digest


@dataclass(frozen=True)
class CorpusPartition:
    train: ReflectionCorpus
    test: ReflectionCorpus
    descriptor: dict[str, Any]

    def select(self, split: str) -> ReflectionCorpus:
        if split not in ("train", "test"):
            raise CorpusError("invalid_split", 0)
        selected = self.train if split == "train" else self.test
        if not selected.cases:
            raise CorpusError("empty_split", 0)
        return selected


def load_partition(path: Path) -> CorpusPartition:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CorpusError("unreadable", 0) from exc
    cases: dict[str, list[Any]] = {"train": [], "test": []}
    metadata: dict[str, dict[str, Any]] = {"train": {}, "test": {}}
    assignments: list[list[str]] = []
    groups: dict[str, str] = {}
    seen: set[str] = set()
    labeled: bool | None = None
    for number, text in enumerate(lines, 1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except ValueError as exc:
            raise CorpusError("not_json", number) from exc
        if not isinstance(row, dict):
            raise CorpusError("not_object", number)
        explicit = "split" in row
        if labeled is not None and labeled != explicit:
            raise CorpusError("partial_split_labels", number)
        labeled = explicit
        split = row.pop("split", "train")
        if not isinstance(split, str) or split not in cases:
            raise CorpusError("invalid_split", number)
        case, details = _reflection_case(json.dumps(row), number, allow_reflection_metadata=True)
        if case.case_id in seen:
            raise CorpusError("duplicate_case_id", number)
        if groups.setdefault(case.group_id, split) != split:
            raise CorpusError("group_crosses_splits", number)
        seen.add(case.case_id)
        cases[split].append(case)
        metadata[split][case.case_id] = details
        assignments.append([case.case_id, split])
    if not seen:
        raise CorpusError("empty_corpus", 0)
    selected = {
        split: ReflectionCorpus(
            tuple(cases[split]),
            metadata[split],
            hashlib.sha256(canonical_json(metadata[split])).hexdigest(),
        )
        for split in cases
    }
    descriptor = {
        "profile": "corpus_partition/1",
        "mode": "explicit" if labeled else "implicit_train",
        "assignments": assignments,
        "corpus_digest": corpus_digest(tuple(cases["train"] + cases["test"])),
        "metadata_digest": hashlib.sha256(canonical_json(metadata)).hexdigest(),
        "train_digest": _split_digest(selected["train"]),
        "test_digest": _split_digest(selected["test"]),
    }
    return CorpusPartition(selected["train"], selected["test"], descriptor)


def validate_partition_descriptor(value: Any) -> None:
    from evolution.tree.execution.journal_identity import IdentityError

    keys = {
        "profile",
        "mode",
        "assignments",
        "corpus_digest",
        "metadata_digest",
        "train_digest",
        "test_digest",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise IdentityError("invalid_partition")
    if value["profile"] != "corpus_partition/1" or value["mode"] not in (
        "explicit",
        "implicit_train",
    ):
        raise IdentityError("invalid_partition")
    for name in keys - {"profile", "mode", "assignments"}:
        digest = value[name]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise IdentityError("invalid_partition")
    assignments = value["assignments"]
    if not isinstance(assignments, list) or not assignments:
        raise IdentityError("invalid_partition")
    seen = set()
    for row in assignments:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not isinstance(row[0], str)
            or not row[0]
            or row[1] not in ("train", "test")
            or row[0] in seen
        ):
            raise IdentityError("invalid_partition")
        if value["mode"] == "implicit_train" and row[1] != "train":
            raise IdentityError("invalid_partition")
        seen.add(row[0])


def _split_digest(corpus: ReflectionCorpus) -> str:
    if not corpus.cases:
        return hashlib.sha256(canonical_json([])).hexdigest()
    return corpus_digest(corpus.cases)


def partition_notes(partition: CorpusPartition) -> tuple[str, ...]:
    """Describe partition coverage for the operator, outside improvement-role inputs."""
    notes = [f"Corpus partition_mode: {partition.descriptor['mode']}."]
    strata = {case.stratum for case in (*partition.train.cases, *partition.test.cases)}
    for split, corpus in (("train", partition.train), ("test", partition.test)):
        notes.append(
            f"Corpus split {split}: {len(corpus.cases)} cases, "
            f"{len({case.group_id for case in corpus.cases})} groups."
        )
        for stratum in sorted(strata):
            cases = [case for case in corpus.cases if case.stratum == stratum]
            label = json.dumps(stratum, ensure_ascii=True)
            notes.append(
                f"Corpus split {split}, stratum {label}: {len(cases)} cases, "
                f"{len({case.group_id for case in cases})} groups."
            )
            if not cases:
                notes.append(f"Corpus split coverage: stratum {label} is absent from {split}.")
    return tuple(notes)
