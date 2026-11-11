"""Which corpus cases an edit can affect (G2 spec §4.3, §4.4, ADR-0024)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from evolution.tree.artifacts.artifact_policy import (
    ArtifactPolicyV1,
    matches,
    scan_tree,
    validate_pattern,
)
from evolution.tree.execution.journal import EVENTS_FILE, MAX_RECORD_BYTES
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
from evolution.tree.execution.json_pointer import MISSING, pointer_value
from evolution.tree.execution.wire_v2 import is_artifact_path
from evolution.tree.storage.append_log import _scan
from evolution.tree.storage.envelope import Envelope


class LabelError(ValueError):
    """One case's `metadata` label is neither absent, `null` nor an array of globs."""

    def __init__(self, case_id: str) -> None:
        super().__init__("bad_value")
        self.case_id = case_id


def provenance_labels_for(
    provenance: Mapping[str, str],
    metadata: Mapping[str, Mapping[str, Any]],
    case_ids: Sequence[str],
) -> list[list[Any]]:
    """The sorted `[case_id, sorted globs]` pairs of every labelled case (spec §4.1).

    A missing pointer or JSON `null` is an unknown label: the case is left out,
    and the selection treats it as affected. `[]` is a label: the case depends
    on no path. An array arrives as a tuple from the loaded corpus, which
    freezes every row's arrays (`bindings._freeze_metadata`), and as a list
    from a plain mapping; both are the same JSON array.
    """

    labels = []
    for case_id in case_ids:
        value = pointer_value(metadata.get(case_id, {}), provenance["path"])
        if value is MISSING or value is None:
            continue
        if not isinstance(value, (list, tuple)) or any(type(glob) is not str for glob in value):
            raise LabelError(case_id)
        if len(set(value)) != len(value):
            raise LabelError(case_id)
        for glob in value:
            try:
                validate_pattern(glob)
            except ValueError:
                raise LabelError(case_id) from None
        labels.append([case_id, sorted(value)])
    return sorted(labels, key=lambda item: item[0])


#: G2 spec §4.4: what one `affected_cases_selected.changed` may hold.
CHANGED_MAX = 4096
CHANGED_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class SampleProvenance:
    """One baseline sample's stored provenance (G2 spec §4.2).

    `used` is the sorted union of `used` over the sample's turns, or `None`
    when it is unknown (a turn reported none, the union overflowed, the try
    failed or never ran). `carries_digest` is whether the row is of a kind that
    can name a component map (`rollout_finished`, `sample_imported`), so only
    such rows decide the mode. `source` names an imported row's origin
    `(run_id, measurement_id)`; `None` for an executed row.
    """

    used: tuple[str, ...] | None
    components_digest: str | None
    carries_digest: bool
    source: tuple[str, str] | None = None


@dataclass(frozen=True)
class TreeChanges:
    """Changed file paths between Parent and Candidate, added and removed included."""

    changed: tuple[str, ...]
    added_or_removed: bool


@dataclass(frozen=True)
class AffectedSelection:
    """What `affected_cases_selected` records, with case ids instead of indices."""

    mode: str | None
    changed: tuple[str, ...] | int
    cases: tuple[str, ...]
    unknown_cases: tuple[str, ...]
    fallback_reason: str | None

    def payload(
        self, round_number: int, attempt_id: str, source: str, corpus_ids: Sequence[str]
    ) -> dict[str, Any]:
        index = {case_id: position for position, case_id in enumerate(corpus_ids)}
        return {
            "round": round_number,
            "attempt": attempt_id,
            "source": source,
            "mode": self.mode,
            "changed": self.changed if isinstance(self.changed, int) else list(self.changed),
            "cases": sorted(index[case_id] for case_id in self.cases),
            "unknown_cases": sorted(index[case_id] for case_id in self.unknown_cases),
            "fallback_reason": self.fallback_reason,
        }


def layer_one(selection: AffectedSelection, exam: Sequence[str]) -> tuple[str, ...]:
    """Exam ∩ affected, in corpus order (design §3)."""

    focus = set(exam)
    return tuple(case_id for case_id in selection.cases if case_id in focus)


def parent_mode(
    baseline: Mapping[str, Sequence[SampleProvenance]],
    known: Mapping[str, tuple[Mapping[str, Any], int]],
) -> tuple[str, str | None, str | None]:
    """The mode the baseline rows indicate: `(mode, parent digest, fallback)` (§4.4)."""

    digests = {
        sample.components_digest
        for samples in baseline.values()
        for sample in samples
        if sample.carries_digest
    }
    if not digests or digests == {None}:
        return "path", None, None
    if len(digests) != 1:
        return "component", None, "components_unstable"
    (digest,) = digests
    assert digest is not None
    if digest not in known:
        return "component", digest, "components_unavailable"
    if known[digest][1] > 0:
        return "component", digest, "components_withheld"
    return "component", digest, None


def _all_cases(
    mode: str | None, changed: tuple[str, ...], corpus_ids: Sequence[str], reason: str
) -> AffectedSelection:
    return AffectedSelection(mode, changed, tuple(corpus_ids), (), reason)


def _bounded(selection: AffectedSelection, corpus_ids: Sequence[str]) -> AffectedSelection:
    changed = selection.changed
    assert not isinstance(changed, int)
    if len(changed) <= CHANGED_MAX and len(canonical_json(list(changed))) <= CHANGED_MAX_BYTES:
        return selection
    return AffectedSelection(
        selection.mode, len(changed), tuple(corpus_ids), (), "changes_unrecorded"
    )


def _affected(
    corpus_ids: Sequence[str],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    hit: Callable[[str], bool | None],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Cases whose baseline used a changed part; `hit` is `None` for an unrecognised entry."""

    cases: list[str] = []
    unknown: list[str] = []
    for case_id in corpus_ids:
        samples = baseline.get(case_id, ())
        is_unknown = not samples
        is_hit = False
        for sample in samples:
            if sample.used is None:
                is_unknown = True
                continue
            verdicts = [hit(entry) for entry in sample.used]
            if any(verdict is None for verdict in verdicts):
                is_unknown = True
            elif any(verdicts):
                is_hit = True
        if is_unknown:
            unknown.append(case_id)
        if is_unknown or is_hit:
            cases.append(case_id)
    return tuple(cases), tuple(unknown)


def select_affected(
    *,
    kind: str,
    corpus_ids: Sequence[str],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    known: Mapping[str, tuple[Mapping[str, Any], int]],
    changes: TreeChanges,
    labels: Mapping[str, Sequence[str]],
    candidate_map: Mapping[str, Any] | None,
    inspection_failed: bool,
    candidate_withheld: int = 0,
) -> AffectedSelection:
    """The affected cases of one Attempt (G2 spec §4.3, §4.4); see the plan's precedence."""

    if kind == "none":
        return _bounded(_all_cases(None, changes.changed, corpus_ids, "none"), corpus_ids)
    if kind == "metadata":
        return _bounded(_by_labels(corpus_ids, changes, labels), corpus_ids)
    mode, digest, fallback = parent_mode(baseline, known)
    if mode == "path":
        return _bounded(_by_paths(corpus_ids, baseline, changes), corpus_ids)
    if fallback is not None:
        return _bounded(_all_cases(mode, changes.changed, corpus_ids, fallback), corpus_ids)
    assert digest is not None
    return _bounded(
        _by_components(
            corpus_ids,
            baseline,
            changes,
            known[digest][0],
            candidate_map,
            inspection_failed,
            candidate_withheld,
        ),
        corpus_ids,
    )


def _by_paths(
    corpus_ids: Sequence[str],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    changes: TreeChanges,
) -> AffectedSelection:
    if changes.added_or_removed:
        return _all_cases("path", changes.changed, corpus_ids, "file_added_or_removed")
    changed = set(changes.changed)

    def hit(entry: str) -> bool | None:
        return entry in changed if is_artifact_path(entry) else None

    cases, unknown = _affected(corpus_ids, baseline, hit)
    return AffectedSelection("path", changes.changed, cases, unknown, None)


def _by_labels(
    corpus_ids: Sequence[str], changes: TreeChanges, labels: Mapping[str, Sequence[str]]
) -> AffectedSelection:
    def matched(globs: Sequence[str], path: str) -> bool:
        return any(matches(glob, PurePosixPath(path), False) for glob in globs)

    for path in changes.changed:
        if not any(matched(globs, path) for globs in labels.values()):
            return _all_cases(None, changes.changed, corpus_ids, "unlabelled_path")
    cases = []
    unknown = []
    for case_id in corpus_ids:
        globs = labels.get(case_id)
        if globs is None:
            unknown.append(case_id)
            cases.append(case_id)
        elif any(matched(globs, path) for path in changes.changed):
            cases.append(case_id)
    return AffectedSelection(None, changes.changed, tuple(cases), tuple(unknown), None)


def _by_components(
    corpus_ids: Sequence[str],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    changes: TreeChanges,
    parent_map: Mapping[str, Any],
    candidate_map: Mapping[str, Any] | None,
    inspection_failed: bool,
    candidate_withheld: int,
) -> AffectedSelection:
    """Component mode (§4.3, §4.4): a declared identifier wins over the path reading.

    The changed parts are the component identifiers whose presence or `sha256`
    differs between the two maps, plus the changed files. They are computed
    before any fallback is chosen, so `changes_unrecorded` bounds their real
    count (ADR-0024). Without a Candidate map (`inspection_failed`,
    `components_unstable`) only the changed files can be computed.
    """

    if inspection_failed:
        return _all_cases("component", changes.changed, corpus_ids, "inspection_failed")
    if candidate_map is None:
        return _all_cases("component", changes.changed, corpus_ids, "components_unstable")
    changed_components = {
        name
        for name in parent_map.keys() | candidate_map.keys()
        if name not in parent_map
        or name not in candidate_map
        or parent_map[name]["sha256"] != candidate_map[name]["sha256"]
    }
    changed_files = set(changes.changed)
    parts = tuple(sorted(changed_components | changed_files))
    if candidate_withheld > 0:
        return _all_cases("component", parts, corpus_ids, "components_withheld")
    if set(parent_map) != set(candidate_map):
        return _all_cases("component", parts, corpus_ids, "component_added_or_removed")
    sources = {
        path
        for mapping in (parent_map, candidate_map)
        for item in mapping.values()
        for path in item["sources"]
    }
    direct = {
        entry
        for samples in baseline.values()
        for sample in samples
        if sample.used is not None
        for entry in sample.used
        if entry not in parent_map and is_artifact_path(entry)
    }
    if any(path not in sources and path not in direct for path in changes.changed):
        return _all_cases("component", parts, corpus_ids, "unaccounted_path")

    def hit(entry: str) -> bool | None:
        if entry in parent_map:
            return entry in changed_components
        if is_artifact_path(entry):
            return entry in changed_files
        return None

    cases, unknown = _affected(corpus_ids, baseline, hit)
    return AffectedSelection("component", parts, cases, unknown, None)


def _replay(records: Sequence[Any]) -> Lifecycle:
    state = Lifecycle()
    for record in records:
        state.accept(record)
    return state


def _union(payload: Mapping[str, Any]) -> tuple[str, ...] | None:
    used = payload.get("provenance")
    return None if used is None else tuple(used)


def baseline_provenance(records: Sequence[Any]) -> dict[str, tuple[SampleProvenance, ...]]:
    """Each scheduled sample's stored provenance, latest try, by case (§4.2, §4.4)."""

    if not records:
        return {}
    state = _replay(records)
    rows: dict[str, list[SampleProvenance]] = {}
    for entry in records[0].payload["identity"]["schedule"]:
        key = (str(entry["case_id"]), str(entry["sample_id"]))
        imported = state.imported.get(key)
        if imported is not None:
            payload = imported.payload
            source = payload["source"]
            sample = SampleProvenance(
                _union(payload),
                payload.get("components_digest"),
                True,
                (str(source["run_id"]), str(source["measurement_id"])),
            )
        else:
            execution = state.executions.get(key)
            if execution is None or execution.event != "rollout_finished":
                sample = SampleProvenance(None, None, False)
            else:
                payload = execution.payload
                sample = SampleProvenance(_union(payload), payload.get("components_digest"), True)
        rows.setdefault(key[0], []).append(sample)
    return {case_id: tuple(samples) for case_id, samples in rows.items()}


def _declared(records: Sequence[Any]) -> dict[str, tuple[Mapping[str, Any], int]]:
    return {
        str(record.payload["components_digest"]): (
            record.payload["components"],
            record.payload.get("components_withheld", 0),
        )
        for record in records
        if record.event == "components_declared"
    }


def known_maps(
    baseline_records: Sequence[Any],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    run_id: str,
    source_records: Callable[[str], Sequence[Any] | None],
) -> dict[str, tuple[Mapping[str, Any], int]]:
    """The `(components, withheld)` declarations the baseline's rows can name (§4.4).

    The baseline's own declarations, and for imported rows those of the source
    measurement when it is a measurement of this run. A map that only another
    run journaled is not known here: `components_unavailable`.
    """

    known: dict[str, tuple[Mapping[str, Any], int]] = {}
    for _measurement_id, records in _declaring_sources(
        "", baseline_records, baseline, run_id, source_records
    ):
        known.update(_declared(records))
    return known


def _declaring_sources(
    baseline_id: str,
    baseline_records: Sequence[Any],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    run_id: str,
    source_records: Callable[[str], Sequence[Any] | None],
) -> Iterator[tuple[str, Sequence[Any]]]:
    """The journals `known_maps` and `declaring_measurements` both read.

    The baseline's own, then each same-run import source's, in sorted order.
    """

    yield baseline_id, baseline_records
    sources = sorted(
        {
            sample.source[1]
            for samples in baseline.values()
            for sample in samples
            if sample.source is not None and sample.source[0] == run_id
        }
    )
    for measurement_id in sources:
        records = source_records(measurement_id)
        if records is not None:
            yield measurement_id, records


def declaring_measurements(
    baseline_id: str,
    baseline_records: Sequence[Any],
    baseline: Mapping[str, Sequence[SampleProvenance]],
    run_id: str,
    source_records: Callable[[str], Sequence[Any] | None],
) -> dict[str, tuple[str, ...]]:
    """Each map digest `known_maps` knows, with the measurements declaring it.

    The baseline first, then the same-run sources in sorted order (G2b3b): the
    epochs of these measurements fix a Parent map's reference epoch.
    """

    declaring: dict[str, list[str]] = {}
    for measurement_id, records in _declaring_sources(
        baseline_id, baseline_records, baseline, run_id, source_records
    ):
        for digest in _declared(records):
            if measurement_id not in declaring.setdefault(digest, []):
                declaring[digest].append(measurement_id)
    return {digest: tuple(ids) for digest, ids in declaring.items()}


#: `measurements/<id>/events.jsonl#bytes=<start>:<end>`, as the campaign writes it.
_PREFIX_LOCATOR = re.compile(r"(?P<file>[^#]+)#bytes=(?P<start>[0-9]+):(?P<end>[0-9]+)\Z")


class BaselineUnavailable(ValueError):
    """The round's `baseline_complete` does not cite a readable baseline prefix."""


def cited_baseline_prefix(
    records: Sequence[Envelope],
    round_number: int,
    measurement_id: str,
    read_file: Callable[[str], bytes],
) -> tuple[Envelope, ...]:
    """The round's baseline records as its `baseline_complete` cited them (§4.4).

    The one bound the campaign and the audit both read a selection's baseline
    through (final review Minor 2): `records` is the campaign journal up to the
    selection, `read_file` returns a run-relative file's bytes. A selection is
    written after its round's baseline completed, so the cited prefix holds
    every row it reads, and nothing appended to the baseline journal later
    can move it. Raises `BaselineUnavailable` naming what is wrong.
    """

    for earlier in reversed(records):
        if (
            earlier.event == "baseline_complete"
            and int(earlier.payload["round"]) == round_number
            and str(earlier.payload["measurement_id"]) == measurement_id
        ):
            break
    else:
        raise BaselineUnavailable("the selection's round baseline never completed")
    evidence = earlier.payload["evidence"]
    if len(evidence) != 1:
        raise BaselineUnavailable("the record cites more than the evidence it relies on")
    reference = evidence[0]
    # Imported here: `campaign_measurement` imports the schema, which imports
    # this module.
    from evolution.tree.campaign_measurement import MEASUREMENTS_DIR

    expected = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    match = _PREFIX_LOCATOR.fullmatch(str(reference.get("locator")))
    if reference.get("kind") != "journal_prefix" or match is None or match["file"] != expected:
        raise BaselineUnavailable("the baseline evidence is not the baseline's own prefix")
    raw = read_file(expected)
    snapshot, _state = _scan(
        raw[int(match["start"]) : int(match["end"])], MAX_RECORD_BYTES, Lifecycle, S1_FAMILY
    )
    if snapshot.journal_integrity != "complete" or not snapshot.records:
        raise BaselineUnavailable("the baseline evidence is not a valid journal prefix")
    return snapshot.records


def baseline_inputs(
    baseline_records: Sequence[Any],
    run_id: str,
    source_records: Callable[[str], Sequence[Any] | None],
) -> tuple[dict[str, tuple[SampleProvenance, ...]], dict[str, tuple[Mapping[str, Any], int]]]:
    """The baseline half of a selection's inputs: `(baseline, known)` (§4.4).

    The one reading the campaign and the audit both call, each with its own
    record access: `baseline_records` is the round's baseline journal and
    `source_records` returns a same-run measurement's records, or `None` when
    that measurement is not a born measurement of this run.
    """

    baseline = baseline_provenance(baseline_records)
    return baseline, known_maps(baseline_records, baseline, run_id, source_records)


def tree_changes(parent_root: Path, child_root: Path, policy: ArtifactPolicyV1) -> TreeChanges:
    """Changed file paths between two published trees (§4.4), file entries only.

    A file on one side and a directory on the other is in exactly one file set,
    so it counts as an added or removed file.
    """

    before = {e.path: e for e in scan_tree(parent_root, policy).artifact if e.kind == "file"}
    after = {e.path: e for e in scan_tree(child_root, policy).artifact if e.kind == "file"}
    changed = set(before.keys() ^ after.keys())
    for path in before.keys() & after.keys():
        if before[path].executable != after[path].executable or (
            (parent_root / path).read_bytes() != (child_root / path).read_bytes()
        ):
            changed.add(path)
    return TreeChanges(tuple(sorted(changed)), before.keys() != after.keys())
