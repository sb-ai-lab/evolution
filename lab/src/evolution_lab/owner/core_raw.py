"""The only place Lab reads core's raw run files and private modules for new data (ADR-0019).

Written for Lab ``0.0.3.dev81`` / core ``evo-skills==2.0.0b22``; on any other core it answers
``core_raw_unsupported`` instead of guessing. Callers mark such data ``source: "core_raw"``
or put an explicit state next to an empty value.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .models import ATTEMPT_ID, REFLECTION_ID

SUPPORTED_CORE = "2.0.0b22"


class CoreRawUnavailable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@functools.cache
def _installed_core() -> str | None:
    """The installed core version, read once per process (called on every status read);
    tests replace this function itself."""
    try:
        return version("evo-skills")
    except PackageNotFoundError:
        return None


def _require_pin() -> None:
    if _installed_core() != SUPPORTED_CORE:
        raise CoreRawUnavailable("core_raw_unsupported")


#: Campaign families Owner paths read on the exact core pin (manifest ``/15`` -> 14, ``/16`` -> 15).
FAMILIES = (14, 15)
#: The families H1 (``_client_readers``), the legacy runner and the H1 report read.
LEGACY_FAMILIES = (14,)


def _require_family(snapshot, families: tuple[int, ...]) -> None:
    from evolution.tree import CampaignUnreadable
    from evolution.tree.campaign import CampaignFamilyUnsupported

    # An unreadable creation can select b4's base reader; that is not evidence
    # of an old family. Only a validated identity can establish the family.
    if not snapshot.identity:
        raise CampaignUnreadable("campaign_unreadable")
    if snapshot.family_version not in families:
        raise CampaignFamilyUnsupported(snapshot.family_version, reader=max(families))


def _require_family14(snapshot) -> None:
    _require_family(snapshot, LEGACY_FAMILIES)


@dataclass(frozen=True, slots=True)
class CampaignRead:
    """One admitted Core snapshot and the projections made from it (spec 2026-10-19 D3)."""

    view: Any                  # Core's CampaignView: one snapshot for everything below
    family: int                # view.snapshot.family_version, 14 | 15
    status: Any                # Core CampaignProjection (status)
    result: Any | None         # Core CampaignProjection (result); None unless requested
    bindings: Mapping | None = None  # authenticated minimal family-15 binding rules of this snapshot
    bindings_reason: str | None = None
    records: tuple | None = None  # admitted prefix shared by details/report/baseline readers
    journal_integrity: str | None = None
    stages: Mapping | None = None  # authenticated focus-first facts of the same prefix
    stages_reason: str | None = None
    complete: bool = True  # Lab helper reads; never part of a Core/public payload


def campaign_read(run: Path, *, families: tuple[int, ...] = FAMILIES, verify: bool = False,
                  secrets: Sequence[str] = (), with_result: bool = False, with_bindings: bool = False) -> CampaignRead:
    """Status (and, on request, result) of one admitted snapshot; Core exceptions pass through.

    Core's own pattern (``inspect_campaign``, campaign.py:14162, b10). Family 14 makes exactly the calls of
    ``campaign_status``/``result_of_view``. Family 15 makes the one authenticated acquisition of the
    ``independent`` projection, ``project_status(..., run_root=run)``, and hands that same block to the
    result (``independent_report=``), so status and result share the public projection. Only view/report callers
    request minimal raw bindings (``with_bindings`` or ``with_result``). These and the round details use an admitted
    prefix of that same snapshot; status/list callers do not authenticate unused binding DTOs. When Core
    cannot authenticate (journal past the snapshot, missing publication) its own block says
    ``evidence: "unavailable"`` with ``attempts: []`` (family15_report.py:580-585): a state, not
    an error."""
    _require_pin()
    from evolution.tree import project_campaign_result
    from evolution.tree.campaign_projections import inspect_view, project_status, verify_evidence

    run = Path(run)
    view = inspect_view(run, verify=verify)
    _require_family(view.snapshot, families)
    family = view.snapshot.family_version
    verification = verify_evidence(run) if verify else None
    if family == 14:
        status = project_status(view, secrets=secrets, verification=verification)
        result = project_campaign_result(view.snapshot, tries=view.tries) if with_result else None
        records, integrity = None, None
        if with_bindings or with_result:
            try:
                records, integrity = _family15_prefix(run, view.snapshot)
            except Exception:
                integrity = "unavailable"
        return CampaignRead(view=view, family=family, status=status, result=result, stages={},
                            records=records, journal_integrity=integrity, complete=integrity != "unavailable")
    status = project_status(view, secrets=secrets, verification=verification, run_root=run)
    result = None
    if with_result:
        result = project_campaign_result(view.snapshot, tries=view.tries, affected_items=view.affected_items,
                                         independent_report=status.to_payload().get("independent"))
    bindings, reason, records, integrity = None, None, None, None
    stages, stages_reason = None, None
    if with_bindings or with_result:
        try:
            records, integrity = _family15_prefix(run, view.snapshot)
            # Core's readable unavailable/snapshot-only block with no attempts
            # needs no binding facts. Never replace its evidence or next action
            # with a second failure of the same missing publication.
            if status.to_payload()["independent"]["attempts"]:
                bindings = family15_bindings(run, view.snapshot, records=records)
            else:
                bindings = {}
        except Exception:
            bindings, reason = None, "unreadable"
            if records is None:
                integrity = "unavailable"
        try:
            if records is None:
                raise CoreRawUnavailable("unreadable")
            stages = family15_stage_facts(run, view.snapshot, records=records)
        except Exception:
            stages, stages_reason = None, "unreadable"
    return CampaignRead(view=view, family=family, status=status, result=result,
                        bindings=bindings, bindings_reason=reason, records=records, journal_integrity=integrity,
                        stages=stages, stages_reason=stages_reason,
                        complete=reason is None and stages_reason is None)


def inspect_family14_view(run: Path, *, verify: bool = False):
    """One core snapshot, admitted before any projection; core exceptions pass through.

    Family 14 only: H1 and the legacy runner read here, so a family-15 run is ``CampaignFamilyUnsupported``."""
    _require_pin()
    from evolution.tree.campaign_projections import inspect_view

    view = inspect_view(Path(run), verify=verify)
    _require_family14(view.snapshot)
    return view


def campaign_status(run: Path, *, verify: bool = False, secrets=()):
    """Core status of the admitted family-14 snapshot, with core's masking/verification (H1, runner)."""
    view = inspect_family14_view(run)
    return status_of_view(view, run=run, verify=verify, secrets=secrets)


def status_of_view(view, *, run: Path, verify: bool = False, secrets=()):
    _require_pin()
    _require_family14(view.snapshot)
    from evolution.tree.campaign_projections import project_status, verify_evidence

    return project_status(view, secrets=secrets,
                          verification=verify_evidence(Path(run)) if verify else None)


def result_of_view(view):
    _require_pin()
    _require_family14(view.snapshot)
    from evolution.tree import project_campaign_result

    return project_campaign_result(view.snapshot, tries=view.tries)


def _journal_family(run: Path) -> int:
    """The family the creation record selects, as Core's own readers choose it (``inspect_view``)."""
    from evolution.tree.campaign_projections import _reader_family_version

    return _reader_family_version(Path(run))


def campaign_log(run: Path, *, families: tuple[int, ...] = LEGACY_FAMILIES):
    """core's validated campaign journal (``campaign.jsonl``), as the report has read it since a8.

    The default is the H1 report's family 14; Owner-side readers pass ``FAMILIES``."""
    log, _ = _campaign_log_snapshot(run, families)
    return log


def _campaign_log_snapshot(run: Path, families):
    _require_pin()
    from evolution.tree.storage.append_log import inspect_log
    from evolution.tree.campaign import CAMPAIGN_FILE, MAX_JOURNAL_BYTES, MAX_RECORD_BYTES
    from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for, reduce

    family_version = _journal_family(run)
    log = inspect_log(Path(run), file_name=CAMPAIGN_FILE, max_record_bytes=MAX_RECORD_BYTES,
                      max_log_bytes=MAX_JOURNAL_BYTES,
                      state_factory=lambda: CampaignLifecycle(selector=None, family_version=family_version),
                      envelope_family=campaign_family_for(family_version))
    snapshot = reduce(log.records, selector=None, family_version=family_version)
    _require_family(snapshot, families)
    return log, snapshot


def campaign_suspensions(run: Path) -> list[dict]:
    """Read family-14/15 suspension history; unread journals never mean no pauses.

    Core validates family-15 suspensions with the family-14 registries (campaign_schema.py:3175-3240).

    Only Core inspection/reduction sits inside the generic exception boundary.
    Lab's structural projection runs afterwards so its programming errors escape.
    """
    _require_pin()
    from evolution.tree import CampaignUnreadable
    from evolution.tree.campaign import CAMPAIGN_FILE, CampaignFamilyUnsupported
    from evolution.tree.campaign_schema import reduce
    from .suspension_evidence import UnsupportedSuspensionShape, project_suspensions

    try:
        journal = Path(run) / CAMPAIGN_FILE
        try:
            mode = journal.stat().st_mode
        except FileNotFoundError:
            raise CoreRawUnavailable("campaign_missing") from None
        if not stat.S_ISREG(mode):
            raise CoreRawUnavailable("campaign_unreadable")
        log = campaign_log(Path(run), families=FAMILIES)
        if not log.records or log.journal_integrity == "corrupt_suffix":
            raise CoreRawUnavailable("campaign_unreadable")
        snapshot = reduce(log.records, selector=None, family_version=_journal_family(run))
        _require_family(snapshot, FAMILIES)
    except CoreRawUnavailable:
        raise
    except CampaignFamilyUnsupported:
        raise CoreRawUnavailable("campaign_family_unsupported") from None
    except CampaignUnreadable:
        raise CoreRawUnavailable("campaign_unreadable") from None
    except Exception:
        raise CoreRawUnavailable("campaign_unreadable") from None
    try:
        return project_suspensions(snapshot.suspensions)
    except UnsupportedSuspensionShape:
        raise CoreRawUnavailable("unsupported_schema") from None


def campaign_journal_times(run: Path) -> tuple[str, str] | None:
    """First and last record timestamps of the campaign journal; ``None`` before it exists."""
    _require_pin()
    from evolution.tree.campaign import CAMPAIGN_FILE

    run = Path(run)
    if not (run / CAMPAIGN_FILE).is_file():
        return None
    try:
        # The creation record alone, not `inspect_view` (which re-reads every measurement).
        log = campaign_log(run, families=FAMILIES)
    except CoreRawUnavailable:
        raise
    except Exception:
        raise CoreRawUnavailable("unreadable") from None
    if not log.records:
        return None
    # a17 and a21 ``Envelope`` exposes ``timestamp`` only through its payload.
    return log.records[0].to_payload()["timestamp"], log.records[-1].to_payload()["timestamp"]


def _family_in(family: int | None, families: tuple[int, ...]) -> None:
    if family is not None and family not in families:
        raise ValueError(f"Lab reads campaign families {families!r} here, not {family!r}")


def _family14(family: int | None) -> None:
    """Vocabularies that exist for family 14 only (Core b8 has no family-15 counterpart)."""
    _family_in(family, LEGACY_FAMILIES)


def attempt_outcome_table(family: int = 14) -> dict[str, frozenset[str]]:
    """Core's family-14 closing outcomes and feedback reasons of an Attempt."""
    _require_pin()
    _family14(family)
    from evolution.tree import campaign_schema as cs

    out: dict[str, frozenset[str]] = {}
    for table in (cs._FINAL_ATTEMPT_OUTCOMES, cs._FINAL_FEEDBACK_OUTCOMES):
        for outcome, reasons in table.items():
            out[outcome] = out.get(outcome, frozenset()) | frozenset(reasons)
    return out


def attempt_outcome_reasons(family: int | None = None) -> frozenset[str]:
    """Every reason core closes a family-14 Attempt with."""
    _family14(family)
    return frozenset(reason for reasons in attempt_outcome_table().values() for reason in reasons)


def confidence_codes(family: int | None = None) -> dict[str, frozenset[str]]:
    """Family-14 look decisions and final member roles (core's shared ``COHORT_ROLES``)."""
    _require_pin()
    _family14(family)
    from evolution.tree.campaign_schema import COHORT_ROLES
    from evolution.tree.final_statistics import ATTEMPT_DECISIONS14

    return {"decisions": frozenset(ATTEMPT_DECISIONS14), "cohort_roles": frozenset(COHORT_ROLES)}


def selection_reasons(family: int | None = None) -> frozenset[str]:
    """Why core chose or kept a round's family-14 Champion."""
    _require_pin()
    _family14(family)
    from evolution.tree.final_report import SELECTION_REASONS14

    return frozenset(SELECTION_REASONS14)


def suspension_reasons(family: int | None = None) -> frozenset[str]:
    """All suspension reasons, including role, connection, final and Champion pauses. Family 15 shares family
    14's registries (campaign_schema.py:3175-3240) and words (``campaign_vocabulary_for(15)``,
    campaign_vocabulary.py:82-83)."""
    _require_pin()
    _family_in(family, FAMILIES)
    from evolution.tree.campaign_schema import CHAMPION_SUSPENSIONS, SUSPENSION_REASONS
    from evolution.tree.campaign_vocabulary import campaign_vocabulary_for

    return frozenset(SUSPENSION_REASONS) | frozenset(CHAMPION_SUSPENSIONS) | {
        campaign_vocabulary_for(family or 14).incomplete_reason}


def caveat_kinds(family: int | None = None) -> frozenset[str]:
    """The complete caveat vocabulary core admits for family 14; a family-15 status carries the same
    family-14 caveats (Task 1 audit: campaign_projections.py:1486-1490, the ``>=`` family paths)."""
    _require_pin()
    _family_in(family, FAMILIES)
    from evolution.tree.campaign_caveats import CAVEAT_KINDS14

    return frozenset(CAVEAT_KINDS14)


def status_action_names(family: int | None = None) -> frozenset[str]:
    """Core's status action names, including its final-action renaming. Family 15 shares the action classes
    (``campaign_view.Action``) and renames as family 14 does (``_final`` is true for it)."""
    _require_pin()
    _family_in(family, FAMILIES)
    from evolution.tree import campaign_view
    from evolution.tree.campaign_projections import _FINAL_ACTION_KINDS

    return frozenset(_FINAL_ACTION_KINDS.get(cls.__name__, cls.__name__)
                     for cls in campaign_view.Action.__subclasses__())


# Family 15 (manifest ``/16``). Two separate closed vocabularies (spec 2026-10-19 D3): the attempt's outcome and
# reason, and the comparison's. Core has importable constants for the comparison and for the attempt_closed table;
# the rest are inline literals in Core, previously verified against v2.0.0b15 (30ae438c7ca8).
# See docs/codex/reports/2026-10-29-core-b15-audit.md. Comments below retain the b13 source-line origins.

#: Projection-only attempt outcomes, beyond the attempt_closed table (family15_report.py:1267-1278).
_ATTEMPT15_PROJECTION_OUTCOMES = frozenset({"declined", "failed", "promoted", "retained"})
#: Scope rules (family15_report.py:1083).
_SCOPE15_RULES = frozenset({"point", "bound", "off", "bootstrap"})
#: Comparison scope statuses (family15_report.py:1109), distinct from preflight (:996-1018).
_SCOPE15_STATUSES = frozenset({"skipped", "available", "unavailable", "not_measured"})
#: Preflight explanation scope statuses (family15_report.py:1007).
_PREFLIGHT15_STATUSES = frozenset({"skipped", "calculated", "uncalibrated"})
#: Deciding layers of a comparison (family15_report.py:1042).
_LAYERS15 = frozenset({"focus", "causal", "global"})
#: Reference kinds of a comparison, besides null (family15_report.py:1058; mapping :272-277).
_REFERENCES15 = frozenset({"stored_non_contemporaneous", "fresh"})
#: Evidence kinds of the ``independent`` block (family15_report.py:1159).
_EVIDENCE15 = frozenset({"snapshot_only", "unavailable", "authenticated_publications"})


def family15_attempt_vocabulary() -> dict[str, frozenset[str]]:
    """Family-15 attempt outcomes and reasons: the attempt_closed table (``feedback_outcomes(15)``,
    campaign_schema.py:1484-1497; a failed preflight closes ``rejected/structurally_unattainable``) plus the
    projection-only outcomes. Never the comparison's vocabulary."""
    _require_pin()
    from evolution.tree.campaign_schema import feedback_outcomes

    table = feedback_outcomes(15)
    return {"outcomes": frozenset(table) | _ATTEMPT15_PROJECTION_OUTCOMES,
            "reasons": frozenset(reason for reasons in table.values() for reason in reasons)}


def family15_comparison_vocabulary() -> dict[str, frozenset[str]]:
    """A family-15 comparison's outcomes, reasons and unavailable codes (``family15_schema``, :64-84)."""
    _require_pin()
    from evolution.tree.family15_schema import OUTCOMES, REASONS, UNAVAILABLE

    return {"outcomes": frozenset(OUTCOMES), "reasons": frozenset(REASONS), "unavailable": frozenset(UNAVAILABLE)}


def family15_scope_vocabulary() -> dict[str, frozenset[str]]:
    """Scope rules and statuses, deciding layers, reference kinds and evidence kinds of the ``independent``
    block, and the preflight scope statuses: Core literals, see the constants above. ``modes`` is the campaign's
    gate mode (``family15_schema.MODES``), labelled by the panel; it never chooses a scope's statement."""
    _require_pin()
    from evolution.tree.family15_schema import MODES

    return {"rules": _SCOPE15_RULES, "statuses": _SCOPE15_STATUSES, "layers": _LAYERS15,
            "references": _REFERENCES15, "evidence": _EVIDENCE15, "preflight_statuses": _PREFLIGHT15_STATUSES,
            "modes": frozenset(MODES)}


def decode_rational(value: Mapping) -> Fraction:
    """Core's exact ``rational-hex/1`` decoder (``family15_schema.decode_rational``); its ``SchemaError``
    passes through."""
    _require_pin()
    from evolution.tree.family15_schema import decode_rational as core_decode

    return core_decode(value)


def validate_family15_projection(block: Mapping) -> None:
    """The pin's public grammar, including conditional keys and closed values (:951-1330).

    Kept in this adapter even though its input is public: the validator lives in
    a private Core module. Lab's own output key guards are separate.
    """
    _require_pin()
    from evolution.tree.family15_report import validate_campaign15_projection
    try:
        validate_campaign15_projection(block)
    except Exception as error:
        raise ValueError("family-15 projection not described by this pin") from error


def check_mode_notes(workflow) -> list[str]:
    """Core's start/resume consequence text for each non-enforced check, in Core's order."""
    _require_pin()
    from evolution.tree.execution.check_modes import CONSEQUENCES

    if workflow.checks is None:
        return []
    return [f"check {name} is {mode}: {CONSEQUENCES[name]}"
            for name, mode in workflow.checks.non_enforce()]


def reflector_probe_policy(manifest) -> str:
    """Read Core's loaded setting; Lab never parses the /16 key table itself."""
    _require_pin()
    probe = manifest.workflow.reflector.probe
    return 'suspend' if probe is None else probe.on_unavailable or 'suspend'


def family15_probe_lines(block: Mapping) -> list[str]:
    """The pin's exact per-reflection wording from both reduced terminal carriers."""
    if 'probe_availability' not in block:
        return []
    validate_family15_projection(block)
    from evolution.tree.family15_report import report_lines

    return [line for line in report_lines(block) if re.match(r'^round [1-9][0-9]*: probe ', line)]


def family15_checks(block: Mapping) -> dict | None:
    """Copy the admitted projection's modes and hits independently of binding availability."""
    validate_family15_projection(block)
    if "check_modes" not in block and "shadow_hits" not in block:
        return None
    return {"modes": dict(block.get("check_modes", {})),
            "shadow_hits": dict(block.get("shadow_hits", {}))}


def _family15_prefix(run: Path, snapshot) -> tuple[tuple, str]:
    """Retain the Core-admitted record prefix which reduces to the acquired view.

    inspect_view does not expose its records. A later evidence read may already
    contain appended work: find the prefix with exactly the acquired snapshot,
    not a new snapshot to mix with that view. Envelope equality compares immutable
    canonical bytes; subsequent binding authentication verifies this retained
    prefix again, following family15_report.py:444-451.
    """
    from evolution.tree.campaign_projections import _evidence_of

    context, log = _evidence_of(Path(run))
    if log.journal_integrity == "corrupt_suffix":
        raise CoreRawUnavailable("unreadable")
    # Core retains one forward lifecycle replay. Increasing positions admit
    # each record once; decreasing positions would restart it for every suffix.
    for end in range(1, len(context.records) + 1):
        if context.snapshot_before(end) == snapshot:
            return tuple(context.records[:end]), log.journal_integrity
    raise CoreRawUnavailable("unreadable")


@dataclass(frozen=True)
class _BaselineEvidence:
    """Completion from the current admitted prefix, not a new campaign snapshot."""

    number: int
    measurement_id: str
    kind: str
    completion: Any
    records: tuple
    integrity: str
    end: int
    digest: str

    @property
    def key(self):
        # The immutable envelope also binds sequence, run/operation identity,
        # locator, reference kind/version and every completion payload field.
        return (self.number, self.measurement_id, self.kind, self.end, self.digest,
                self.completion.encode())


def _baseline_evidence(run, snapshot, read=None):
    """Admit completion once, reusing only a CampaignRead's authenticated records."""
    _require_pin()
    try:
        _require_family(snapshot, FAMILIES)
        kind = snapshot.identity['policy']['gate']['provenance']['kind']
        if kind in ('metadata', 'none'):
            return None
        if kind != 'reported':
            raise CoreRawUnavailable('unreadable')
        completed = [(int(number), state) for number, state in snapshot.rounds.items()
                     if state.baseline_complete and state.baseline_measurement_id is not None]
        if not completed:
            return None
        number, state = max(completed, key=lambda item: item[0])
        measurement_id = str(state.baseline_measurement_id)
        if read is not None:
            if read.view.snapshot != snapshot or read.family != snapshot.family_version:
                raise CoreRawUnavailable('unreadable')
            if read.journal_integrity == 'unavailable':
                raise CoreRawUnavailable('unreadable')
        if read is not None and read.records is not None:
            records, integrity = read.records, read.journal_integrity
        else:
            records, integrity = _family15_prefix(Path(run), snapshot)
        if integrity not in ('complete', 'incomplete_tail'):
            raise CoreRawUnavailable('unreadable')
        completion = next(record for record in reversed(records)
                          if record.event == 'baseline_complete'
                          and record.payload['round'] == number
                          and record.payload['measurement_id'] == measurement_id)
        reference, = completion.payload['evidence']
        expected = f'measurements/{measurement_id}/events.jsonl'
        match = re.fullmatch(re.escape(expected) + r'#bytes=0:(\d+)', reference['locator'])
        if match is None or reference['kind'] != 'journal_prefix':
            raise CoreRawUnavailable('unreadable')
        return _BaselineEvidence(number, measurement_id, kind, completion, tuple(records), integrity,
                                 int(match[1]), reference['content_digest'])
    except CoreRawUnavailable:
        raise
    except Exception:
        raise CoreRawUnavailable('unreadable') from None


class BaselineProvenanceCache:
    """One aggregate per run; campaign progress does not change its completion.

    Each request first admits its current completion. Only that identity and
    nofollow measurement metadata key the aggregate, never a mutable file's
    digest alone. The admitted campaign prefix is a request input, not cached.
    """

    def __init__(self):
        self._entries = {}
        self._lock = threading.Lock()

    @staticmethod
    def signature(run, evidence):
        from .projection_signature import input_signature
        measurement = evidence.measurement_id
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', measurement):
            return None
        path = Path(run) / 'measurements' / measurement / 'events.jsonl'
        value = input_signature(path)
        if value is None or value[-1][1] != stat.S_IFREG:
            return None
        return ((str(path), value),)

    def read(self, run, snapshot, *, read=None):
        _require_pin()
        run = Path(run).absolute()
        try:
            evidence = _baseline_evidence(run, snapshot, read)
        except CoreRawUnavailable:
            self.drop(run)
            raise
        if evidence is None:
            self.drop(run)
            return None
        signature = self.signature(run, evidence)
        complete = evidence.integrity == 'complete'
        with self._lock:
            cached = self._entries.get(run)
            if complete and signature is not None and cached is not None and cached[:2] == (evidence.key, signature):
                return dict(cached[2])
            self._entries.pop(run, None)
        value = baseline_provenance_state(run, snapshot, _evidence=evidence)
        if complete and signature is not None and signature == self.signature(run, evidence):
            with self._lock:
                self._entries[run] = (evidence.key, signature, dict(value))
        return value

    def drop(self, run):
        with self._lock:
            self._entries.pop(Path(run).absolute(), None)


def baseline_provenance_state(run: Path, snapshot, *, read=None,
                              _evidence=None) -> dict | None:
    """Core's latest completed baseline, within the supplied campaign snapshot.

    ``samples`` and ``known`` cover Core's entire schedule; ``admitted`` counts
    scored samples separately, including imported scores. Exceptions represent
    unavailable evidence and must not be cached by the caller.
    """
    _require_pin()
    from evolution.tree import recovery_plan
    from evolution.tree.affected_cases import baseline_provenance, cited_baseline_prefix
    from evolution.tree.execution.journal import MAX_JOURNAL_BYTES

    try:
        evidence = _evidence if _evidence is not None else _baseline_evidence(run, snapshot, read)
        if evidence is None:
            return None
        expected = f'measurements/{evidence.measurement_id}/events.jsonl'
        raw = read_inside(Path(run), *expected.split('/'), limit=MAX_JOURNAL_BYTES, prefix_bytes=evidence.end)
        if hashlib.sha256(raw).hexdigest() != evidence.digest:
            raise CoreRawUnavailable('unreadable')
        prefix = cited_baseline_prefix(evidence.records, evidence.number, evidence.measurement_id,
                                       lambda path: raw if path == expected else b'')
        rows = [row for values in baseline_provenance(prefix).values() for row in values]
        decisions = recovery_plan(prefix).executions
        admitted = sum(row.scoring_state == 'finished' and row.evaluation is not None
                       and row.evaluation.kind == 'score' for row in decisions)
        return {'round': evidence.number, 'measurement_id': evidence.measurement_id, 'samples': len(rows),
                'known': sum(row.used is not None for row in rows), 'admitted': admitted}
    except CoreRawUnavailable:
        raise
    except Exception:
        raise CoreRawUnavailable('unreadable') from None


def family15_bindings(run: Path, snapshot, *, records: Sequence | None = None) -> dict:
    """Authenticate binding publications of ``snapshot`` and its retained prefix; expose only rules.

    Core's report authenticates these same S2 publications (family15_report.py:
    444-457,474-489), but its public scopes contain result rules, not binding
    rules. Use its audit, never reconstruct a binding from a result or raw JSON.
    Appended records are allowed only after verifying the retained immutable
    prefix still matches and reduces to this snapshot. A changed prefix or missing/corrupt publication raises; an
    actually absent binding is null. No numerical replay or journal writes.
    """
    _require_pin()
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.family15_audit import Family15Audit

    attempts = [attempt for state in snapshot.rounds.values() for attempt in state.attempts.values()]
    if not attempts:
        return {}
    context, log = _evidence_of(Path(run))
    if log.journal_integrity == "corrupt_suffix":
        raise CoreRawUnavailable("unreadable")
    if records is None:
        records = tuple(context.records)
    else:
        records = tuple(records)
        if tuple(context.records[:len(records)]) != records:
            raise CoreRawUnavailable("unreadable")
    if context.snapshot_before(len(records)) != snapshot:
        raise CoreRawUnavailable("unreadable")
    audit = Family15Audit(context)

    def binding(record, *, final=False):
        if record is None:
            return None
        body = audit.publication(AuxiliaryPublication.from_payload(record.payload["data"]))
        if body is None:
            raise CoreRawUnavailable("unreadable")
        profile = body["comparison"] if final else body
        # The audit validates the whole exact b10 body, including unknown keys.
        # These explicit closed sets also keep a future rule from disappearing
        # into an apparently readable minimal DTO.
        scopes = []
        for scope in profile["scopes"]:
            if scope["name"] not in _LAYERS15 or scope["rule"] not in _SCOPE15_RULES:
                raise CoreRawUnavailable("unreadable")
            scopes.append({"name": scope["name"], "rule": scope["rule"]})
        return {"source": "core_raw", "scopes": scopes}

    return {attempt.attempt_id: {
        "gate_binding": binding(attempt.family15.comparison_binding) if attempt.family15 else None,
        "global_binding": binding(attempt.family15.final_binding, final=True) if attempt.family15 else None,
    } for attempt in attempts}


def family15_stage_facts(run: Path, snapshot, *, records: Sequence | None = None) -> dict[str, dict]:
    """Restate focus-first stage records of one authenticated, admitted prefix.

    Core b15 writes these in campaign.jsonl (run_watch.py and family15_lifecycle.py).
    comparison_bound associates both gate measurement sides with their Attempt;
    retaining earlier associations preserves openings across a later rebind.
    No scheduler actions, numeric replay, or journal writes are involved.
    """
    _require_pin()
    from evolution.tree.campaign_projections import _evidence_of

    try:
        context, log = _evidence_of(Path(run))
        if log.journal_integrity == "corrupt_suffix":
            raise CoreRawUnavailable("unreadable")
        if records is None:
            records = tuple(context.records)
        else:
            records = tuple(records)
            if tuple(context.records[:len(records)]) != records:
                raise CoreRawUnavailable("unreadable")
        if context.snapshot_before(len(records)) != snapshot:
            raise CoreRawUnavailable("unreadable")
        if snapshot.identity["policy"].get("gate", {}).get("stages") != "focus_first":
            return {}
        facts = {attempt.attempt_id: {"focus_opened": False, "causal_opened": False, "focus_decided": None}
                 for state in snapshot.rounds.values() for attempt in state.attempts.values()}
        measurements = {}
        for record in records:
            payload = record.payload
            if record.event == "comparison_bound":
                for side in ("candidate", "reference"):
                    measurements[payload[side]["measurement_id"]] = payload["attempt_id"]
            elif record.event == "stage_opened" and "scope" in payload:
                scope = payload["scope"]
                if scope not in {"focus", "causal"}:
                    raise CoreRawUnavailable("unreadable")
                facts[measurements[payload["measurement_id"]]][scope + "_opened"] = True
            elif record.event == "focus_decided":
                outcome = payload["outcome"]
                if outcome not in {"passed", "comparison_not_passed", "comparison_unavailable"}:
                    raise CoreRawUnavailable("unreadable")
                facts[payload["attempt_id"]]["focus_decided"] = outcome
        return facts
    except Exception:
        raise CoreRawUnavailable("unreadable") from None


def final_vocabulary() -> dict[str, str]:
    """The words of family 14 Lab classifies by (core ``campaign_vocabulary_for(14)``, spec 2026-10-13 §6.3)."""
    _require_pin()
    from evolution.tree.campaign_vocabulary import campaign_vocabulary_for

    vocabulary = campaign_vocabulary_for(14)
    return {"gate_pass_outcome": vocabulary.gate_pass_outcome, "contrast_key": vocabulary.contrast_key,
            "incomplete_reason": vocabulary.incomplete_reason,
            "focus_rejection_reason": vocabulary.final_focus_rejection_reason}


def role_suspension_reasons() -> frozenset[str]:
    """The suspension reasons an improvement role's failure decides (core ``RESILIENCE_SUSPENSIONS``)."""
    _require_pin()
    from evolution.tree.campaign_schema import RESILIENCE_SUSPENSIONS

    return frozenset(RESILIENCE_SUSPENSIONS)


def champion_suspension_reasons() -> frozenset[str]:
    """The Champion's suspension reasons (core ``CHAMPION_SUSPENSIONS``): ``champion_incomplete`` on 2.0.0b6."""
    _require_pin()
    from evolution.tree.campaign_schema import CHAMPION_SUSPENSIONS

    return frozenset(CHAMPION_SUSPENSIONS)


def reflection_terminal_statuses() -> frozenset[str]:
    """Every terminal status of a reduced reflection: core's ``REFLECTION_STATUSES`` (``reflection_finished``)
    and ``failed`` (a failed reflection); ``started`` is not terminal."""
    _require_pin()
    from evolution.tree.campaign_schema import REFLECTION_STATUSES

    return frozenset(REFLECTION_STATUSES) | {"failed"}


def corpus_cases(path: Path, *, split: str | None = None) -> tuple:
    """Cases of a `/14` corpus as core reads it (`corpus_partition.load_partition`, the reader of `evo tree evolve`
    and `evo tree evaluate`; spec 2026-10-09 §3.1): ``None`` — every case, train then test; ``"train"`` /
    ``"test"`` — core's ``select`` (an empty split is core's ``CorpusError("empty_split")``). The labelling rules
    (`partial_split_labels`, `group_crosses_splits`, `invalid_split`) are core's ``CorpusError`` and pass through."""
    _require_pin()
    from evolution.tree.execution.corpus_partition import load_partition

    partition = load_partition(Path(path))
    if split is None:
        return tuple(partition.train.cases) + tuple(partition.test.cases)
    return tuple(partition.select(split).cases)


def _reflection_entry(entry) -> dict | None:
    """A reduced reflection of core (`RoundState.reflections`) as plain data; `reason` is family 10 only."""
    if entry is None:
        return None
    return {"reflection_id": entry["reflection_id"], "status": entry["status"],
            "groups": [dict(group) for group in entry.get("groups") or ()],
            "error_class": entry.get("error_class"), "reason": entry.get("reason")}


def campaign_details(run: Path, *, read: CampaignRead | None = None) -> dict:
    """Per round: the baseline version, the counted and the last reflection; per attempt: the last
    Editor terminal and the seal refusal code (spec 2026-09-30 campaign-details §3.1).

    Raw core fields only, no Lab decisions. The journal goes through core's own reducer, as core's
    public projections do (`campaign_projections.py:1448`); an existing journal with no valid record or
    with a corrupt suffix is `unreadable` (core's writer treats it as a fault, `append_log.py:363`)."""
    _require_pin()
    from evolution.tree.campaign import CAMPAIGN_FILE
    from evolution.tree.campaign_schema import reduce

    run = Path(run)
    shared = read is not None and read.family == 15
    if not shared and not (run / CAMPAIGN_FILE).is_file():
        return {"journal": "missing", "rounds": {}}
    try:
        if shared:
            if read.records is None or read.journal_integrity == "corrupt_suffix":
                raise CoreRawUnavailable("unreadable")
            records, snapshot = read.records, read.view.snapshot
        else:
            log = campaign_log(run, families=FAMILIES)
            if not log.records or log.journal_integrity == "corrupt_suffix":
                raise CoreRawUnavailable("unreadable")
            records = log.records
            snapshot = reduce(records, selector=None, family_version=_journal_family(run))
        baselines: dict[int, str] = {}
        for record in records:
            envelope = record.to_payload()
            if envelope["event"] == "baseline_bound":
                baselines[int(envelope["payload"]["round"])] = envelope["payload"]["champion_version"]
        rounds = {}
        for number, state in snapshot.rounds.items():
            rounds[int(number)] = {
                "baseline_version": baselines.get(int(number)),
                "diagnosis": _reflection_entry(state.diagnosis(True)),
                "last_reflection": _reflection_entry(state.reflections[-1] if state.reflections else None),
                "attempts": {attempt_id: {"editor_error": attempt.editor_error,
                                          "editor_reason": attempt.editor_reason, "code": attempt.code}
                             for attempt_id, attempt in state.attempts.items()},
            }
    except CoreRawUnavailable:
        raise
    except Exception:
        raise CoreRawUnavailable("unreadable") from None
    details = {"journal": "present", "rounds": rounds}
    if shared:
        # Binding failure belongs only to independent, not these admitted
        # reflection/baseline facts. No second acquisition or authentication.
        details["bindings"], details["bindings_reason"] = read.bindings, read.bindings_reason
        details["stages"], details["stages_reason"] = read.stages, read.stages_reason
    return details


def instance_demand(policy, max_concurrent_sessions: int) -> int:
    """Peak target instances a campaign policy needs: the least ``max_instances`` core's own ``admit_capabilities``
    admits for family14 (spec 2026-10-13 §5.2), including finalists and Champion side; Lab keeps no formula of its own."""
    _require_pin()
    limit = 1
    while not capacity_admitted(policy, max_instances=limit, max_concurrent_sessions=max_concurrent_sessions):
        limit += 1
        if limit > 1024:
            raise ValueError("core admits no target capacity up to 1024 instances")
    return limit


def capacity_admitted(policy, *, max_instances: int, max_concurrent_sessions: int) -> bool:
    """Core's own ``admit_capabilities`` against the declared target limits: ``False`` is its refusal."""
    _require_pin()
    from evolution.tree.campaign_identity import AdapterCapabilities, CapabilitiesInsufficient, admit_capabilities

    try:
        admit_capabilities(policy, AdapterCapabilities(
            seed_mode="exact", max_concurrent_sessions=max_concurrent_sessions, instantiable=True,
            materialization=("artifact_root_injection",), max_instances=max_instances))
    except CapabilitiesInsufficient:
        return False
    return True


FILE_MAX = 32 * 1024 * 1024


def read_inside(run: Path, *parts: str, limit: int = FILE_MAX, prefix_bytes: int | None = None) -> bytes:
    """One bounded regular file under the run (spec 2026-10-13 §4.1).

    Every directory, including the run boundary, is opened without following
    symlinks. Descriptor-relative traversal stays bound if a pathname is replaced.
    ``prefix_bytes`` reads exactly a cited range, bounded by its journal limit;
    an uncited suffix is neither read nor subject to the section-size cap.
    """
    _require_pin()
    if prefix_bytes is not None and not 0 < prefix_bytes <= limit:
        raise CoreRawUnavailable("too_large")
    if not parts or any(part in ("", ".", "..") or "/" in part or "\0" in part for part in parts):
        raise CoreRawUnavailable("unreadable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directories = Path(run).absolute().parts
        if any(part == ".." or "\0" in part for part in directories):
            raise CoreRawUnavailable("unreadable")
        with ExitStack() as stack:
            def opened(name: str, flags: int, parent: int | None = None) -> int:
                try:
                    descriptor = os.open(name, flags, dir_fd=parent)
                except FileNotFoundError:
                    raise CoreRawUnavailable("missing") from None
                stack.callback(os.close, descriptor)
                return descriptor

            directory = opened(directories[0], directory_flags)
            for part in (*directories[1:], *parts[:-1]):
                directory = opened(part, directory_flags, directory)
            descriptor = opened(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise CoreRawUnavailable("unreadable")
            if prefix_bytes is None and info.st_size > limit:
                raise CoreRawUnavailable("too_large")
            if prefix_bytes is not None and info.st_size < prefix_bytes:
                raise CoreRawUnavailable("unreadable")
            bound = limit + 1 if prefix_bytes is None else prefix_bytes
            chunks, size = [], 0
            while size < bound and (chunk := os.read(descriptor, min(64 * 1024, bound - size))):
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise CoreRawUnavailable("too_large")
            if prefix_bytes is not None and size != prefix_bytes:
                raise CoreRawUnavailable("unreadable")
            data = b"".join(chunks)
    except OSError:
        raise CoreRawUnavailable("unreadable") from None
    return data


def read_jsonl(run: Path, *parts: str) -> tuple[list[dict], bool]:
    """A role log's records and whether its last line is cut (a finished record's log with a broken tail: the rest
    is given, spec §4.3). A broken line before the last one is ``unreadable``."""
    lines = read_inside(run, *parts).split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    records, broken = [], False
    for index, line in enumerate(lines):
        try:
            value = json.loads(line.decode("utf-8"))
        except ValueError:
            value = None
        if not isinstance(value, dict):
            if index == len(lines) - 1:
                broken = True
                break
            raise CoreRawUnavailable("unreadable")
        records.append(value)
    return records, broken


_SCORE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ROLE_DIRS = {"editor": "editor-logs", "reflector": "reflector-logs"}
_ROLE_IDS = {"editor": ATTEMPT_ID, "reflector": REFLECTION_ID}
TRACE_PARTS = ("scorer-traces", "scorer_traces")


def _journal_events(journal: Path):
    records, broken = read_jsonl(journal.parent, journal.name)
    # A role log may expose a valid prefix, but public journal facts must not
    # claim completion after silently dropping a broken last record (§4.3).
    if broken:
        raise CoreRawUnavailable("unreadable")
    for record in records:
        event, payload = record.get("event"), record.get("payload")
        if not isinstance(event, str) or not isinstance(payload, dict):
            raise CoreRawUnavailable("unreadable")
        yield event, payload


def scoring_index(journal: Path) -> dict:
    """Group a completely read journal once; validate only the requested sample.

    An invalid binding on an unrelated sample cannot spoil admitted evidence.
    Structural journal read errors still fail the complete index.
    """
    _require_pin()
    samples = {}
    for event, payload in _journal_events(Path(journal)):
        if event not in {'scoring_started', 'scoring_finished', 'scoring_failed', 'sample_imported'}:
            continue
        key = (payload.get('case_id'), payload.get('sample_id'))
        if not all(isinstance(part, str) for part in key):
            continue  # Such a key cannot match an admitted case/sample id.
        samples.setdefault(key, []).append((event, payload))
    return samples


def scoring_from_index(index, case_id, sample_id, *, require_critical=False):
    return _scoring_of(index.get((case_id, sample_id), ()), require_critical=require_critical)


def scoring_of(journal: Path, case_id: str, sample_id: str, *, require_critical: bool = False) -> dict:
    """The sample's last completed scoring try, from its public measurement journal (§3.1).

    The fixed-evaluation notice opts into confirmed critical_pass evidence;
    sample disclosure retains its existing normalization by default.
    """
    _require_pin()
    events = ((event, payload) for event, payload in _journal_events(Path(journal))
              if payload.get('case_id') == case_id and payload.get('sample_id') == sample_id)
    return _scoring_of(events, require_critical=require_critical)


def _scoring_of(events, *, require_critical=False):
    started, last, imported = [], None, False
    try:
        for event, payload in events:
            if event == "scoring_started":
                started.append(payload)
            elif event in ("scoring_finished", "scoring_failed"):
                last = (event, payload)
            elif event == "sample_imported":
                imported = True
        empty = {"score_request_id": None, "retry_ordinal": None, "tries_total": len(started),
                 "metrics": None, "critical_pass": None}
        if last is None:
            state = "imported" if imported and not started else "incomplete" if started else "not_scored"
            return {"state": state, **empty}
        event, payload = last
        if require_critical and event == "scoring_finished" and (
                "critical_pass" not in payload or (payload["critical_pass"] is not None
                                                  and type(payload["critical_pass"]) is not bool)):
            raise CoreRawUnavailable("unreadable")
        rid = payload["score_request_id"]
        # Bind the trace to its start, as required by §3.1, rather than an
        # unrelated scoring start of the same sample (or a later unfinished try).
        start = next(s for s in reversed(started) if s["score_request_id"] == rid)
        return {"state": "finished" if event == "scoring_finished" else "failed", "score_request_id": rid,
                "retry_ordinal": start.get("retry_ordinal"), "tries_total": len(started),
                "metrics": payload.get("metrics"), "critical_pass": payload.get("critical_pass")}
    except CoreRawUnavailable:
        raise
    except Exception:
        raise CoreRawUnavailable("unreadable") from None


def scorer_trace_signature(root: Path, score_request_id: str, *, measurement_id: str):
    """Exact b22 reader inputs: this measurement's segments and this trace's files.

    Include the first absent segment, since a newly created segment can turn a
    previously unambiguous directory/pack trace into an unreadable one. The
    adapter remains pinned; no trace payload or negative projection is cached.
    """
    _require_pin()
    from evolution.tree.execution.scorer_traces import PACK_ID
    from .projection_signature import input_signature
    if not isinstance(score_request_id, str) or _SCORE_REQUEST_ID.fullmatch(score_request_id) is None:
        raise CoreRawUnavailable('unreadable')
    signatures = []
    if isinstance(measurement_id, str) and PACK_ID.fullmatch(measurement_id):
        segment = 0
        while True:
            name = f'{measurement_id}.pack' if segment == 0 else f'{measurement_id}.s{segment}.pack'
            signature = input_signature(root / 'scorer-traces' / name)
            if signature is None:
                return None
            signatures.append((name, signature))
            if signature[-1] == ('', 'absent'):
                break
            segment += 1
    for name in ('request.json', 'stdout', 'stderr'):
        signature = input_signature(root / 'scorer-traces/scorer_traces' / score_request_id / name)
        if signature is None:
            return None
        signatures.append((name, signature))
    return tuple(signatures)


def _scorer_root_precheck(root: Path) -> None:
    """Keep missing/unsafe root mappings before the strict reader admits its root.

    These descriptors close here; the b6 reader pins its own root descriptor.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        parts = Path(root).absolute().parts
        if any(part == ".." or "\0" in part for part in parts):
            raise CoreRawUnavailable("unreadable")
        with ExitStack() as stack:
            directory = None
            for part in parts:
                try:
                    directory = os.open(part, flags, dir_fd=directory)
                except FileNotFoundError:
                    raise CoreRawUnavailable("missing") from None
                stack.callback(os.close, directory)
    except (OSError, ValueError):
        raise CoreRawUnavailable("unreadable") from None


def _scorer_bounds(record) -> None:
    """Check both requested sections before selecting payload or disclosing input."""
    for section in ("request", "stdout"):
        size = record.sizes.get(section)
        data = getattr(record, section)
        if (size is not None and size > FILE_MAX) or (data is not None and len(data) > FILE_MAX):
            raise CoreRawUnavailable("too_large")


def scorer_trace(root: Path, score_request_id: str, case_id: str, *, measurement_id: str,
                 require_output: bool = False) -> dict:
    """One public-reader trace, bound to an Owner-resolved measurement and sample.

    Unknown request sizes refuse the trace; unknown stdout sizes preserve only
    verified input. The internal output_reason is consumed by sample_view; no
    new public fields are introduced. The strict reader pins the run boundary
    and caps each selected payload section; the pack index has no aggregate
    memory cap, and the run owner can still modify file contents.
    """
    _require_pin()
    if (not isinstance(score_request_id, str) or _SCORE_REQUEST_ID.fullmatch(score_request_id) is None
            or not isinstance(measurement_id, str)):
        raise CoreRawUnavailable("unreadable")
    _scorer_root_precheck(root)
    from evolution.tree import ScorerTraceReader, ScorerTraceUnreadable

    try:
        with ScorerTraceReader(root, run_boundary="nofollow") as reader:
            metadata = reader.read(score_request_id, measurement_id=measurement_id, sections=())
            if metadata is None:
                raise CoreRawUnavailable("missing")
            _scorer_bounds(metadata)
            if "request" not in metadata.sizes:
                raise CoreRawUnavailable("missing" if metadata.layout == "pack" else "unreadable")
            if require_output and not metadata.sizes.get('stdout'):
                # Directory traces normalize a missing stream to empty bytes.
                # Neither form can contain the JSON required by a count.
                raise CoreRawUnavailable('missing')
            sections = ("request", "stdout") if "stdout" in metadata.sizes else ("request",)
            record = reader.read(score_request_id, measurement_id=measurement_id, sections=sections,
                                 max_section_bytes=FILE_MAX)
            if record is None:
                raise CoreRawUnavailable("missing")
            _scorer_bounds(record)
            if "request" not in record.sizes or record.request is None:
                raise CoreRawUnavailable("missing" if record.layout == "pack" else "unreadable")
            if len(record.request) != record.sizes["request"]:
                raise CoreRawUnavailable("unreadable")
            raw = record.request
            stdout = record.stdout if "stdout" in sections and "stdout" in record.sizes else None
            if stdout is not None and len(stdout) != record.sizes["stdout"]:
                raise CoreRawUnavailable("unreadable")
            output_reason = None if stdout is not None else (
                "trace_missing" if record.layout == "pack" else "unreadable")
            returncode = record.returncode
    except ScorerTraceUnreadable as error:
        raise CoreRawUnavailable("too_large" if error.code == "too_large" else "unreadable") from None
    try:
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"scorer", "trace_id", "input", "input_sha256"}:
            raise ValueError
        payload = request["input"]
        if (not isinstance(payload, dict) or set(payload) != {"case_id", "public", "oracle", "view"}
                or request["trace_id"] != score_request_id or payload["case_id"] != case_id
                or not isinstance(payload["view"], dict)):
            raise ValueError
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if hashlib.sha256(canonical).hexdigest() != request["input_sha256"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CoreRawUnavailable("unreadable") from None
    return {"input": payload, "stdout": None if stdout is None else stdout.decode("utf-8", errors="replace"),
            "output_reason": output_reason, "returncode": returncode}


def _event_of(record) -> tuple[str, dict]:
    envelope = record.to_payload()
    return envelope["event"], envelope["payload"]


def campaign_content(run: Path) -> dict:
    """Closed attempts, reflection terminals and selected changed paths from a34's journal (§4.3)."""
    _require_pin()
    from evolution.tree.storage.append_log import _scan
    from evolution.tree.campaign import CAMPAIGN_FILE, MAX_RECORD_BYTES
    from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for, reduce

    try:
        raw = read_inside(run, CAMPAIGN_FILE)
        family = _journal_family(run)
        log, _state = _scan(raw, MAX_RECORD_BYTES,
                            lambda: CampaignLifecycle(selector=None, family_version=family),
                            campaign_family_for(family))
        _require_family(reduce(log.records, selector=None, family_version=family), FAMILIES)
        # Neither a corrupt suffix nor an incomplete tail may silently
        # disappear when the journal authorizes disclosure of completed work.
        if not log.records or log.journal_integrity != "complete":
            raise CoreRawUnavailable("unreadable")
        closed, reflections, changed = set(), {}, {}
        for record in log.records:
            event, payload = _event_of(record)
            if event == "attempt_closed":
                closed.add(payload["attempt_id"])
            elif event in ("reflection_finished", "reflection_failed"):
                # Shadow hits have their own Core projection; role content
                # keeps the pre-existing terminal fields only.
                reflections[payload["reflection_id"]] = {
                    "event": event, **{key: value for key, value in payload.items() if key != "shadowed"}}
            elif event == "affected_cases_selected":
                # On a34 `attempt` is already the full rNN-aMM identifier;
                # `changed` is a count only for changes_unrecorded.
                changed[payload["attempt"]] = payload["changed"]
        return {"closed": closed, "reflections": reflections, "changed": changed}
    except CoreRawUnavailable:
        raise
    except Exception:
        raise CoreRawUnavailable("unreadable") from None


def _role_directory(role: str, log_id: str) -> str:
    if (role not in _ROLE_DIRS or not isinstance(log_id, str)
            or _ROLE_IDS[role].fullmatch(log_id) is None):
        raise CoreRawUnavailable("unreadable")
    return _ROLE_DIRS[role]


def role_log(run: Path, role: str, log_id: str) -> tuple[list[dict], bool]:
    """Role JSONL records and its broken-tail flag; projection belongs to the assembler."""
    _require_pin()
    return read_jsonl(run, _role_directory(role, log_id), f"{log_id}.jsonl")


def role_stderr(run: Path, role: str, log_id: str) -> str:
    """Full FILE_MAX-bounded stderr; the assembler masks it before taking a field-sized tail."""
    _require_pin()
    return read_inside(run, _role_directory(role, log_id), f"{log_id}.stderr").decode("utf-8", errors="replace")


def preflight_limits(workflow):
    """Resolve the effective /16 evidence and chat Grouper settings through Core."""
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE
    from evolution.tree.roles.config import ModelGroupingSection, effective_grouping_v2

    _require_pin()
    grouping = effective_grouping_v2(workflow.grouper) if isinstance(workflow.grouper, ModelGroupingSection) else None
    return (workflow.evidence or DEFAULT_EVIDENCE).to_payload(), grouping


def _preflight_shadow_repeats(records, snapshot, chat_roles):
    """Count shadow hits only when every invocation of the role settled its counters."""
    check = "repeated_read_without_progress"
    if snapshot.family15.check_modes.get(check) != "shadow":
        return {}
    role_events = {
        "reflection_started": "reflector", "reflection_finished": "reflector", "reflection_failed": "reflector",
        "editor_started": "editor", "editor_finished": "editor", "editor_failed": "editor",
    }
    totals = {}
    pending = set()
    for record in records:
        role = role_events.get(record.event)
        if role is None or role not in chat_roles:
            continue
        payload = record.payload
        session = (role, payload["reflection_id" if role == "reflector" else "attempt_id"])
        totals.setdefault(role, 0)
        if record.event.endswith("_started"):
            pending.add(session)
        else:
            pending.discard(session)
            if record.event.endswith("_failed") and "reason" not in payload:
                # Core omits shadow counters on lifecycle failures. A retry reports only its own hits.
                totals[role] = None
            elif totals[role] is not None:
                totals[role] += sum(hit["count"] for hit in payload.get("shadowed", ()) if hit["check"] == check)
    for role, _ in pending:
        totals[role] = None
    return totals


def preflight_campaign(run: Path, run_id: str, chat_roles, cases) -> dict:
    """Closed numeric and identity projection of one admitted family-15 journal."""
    from evolution.tree import JournalBoundaryError

    try:
        log, snapshot = _campaign_log_snapshot(run, (15,))
    except (OSError, ValueError, JournalBoundaryError):
        raise CoreRawUnavailable("unreadable") from None
    if log.journal_integrity != "complete" or not log.records or log.records[0].to_payload()["run_id"] != run_id:
        raise CoreRawUnavailable("unreadable")
    from evolution.tree.execution.journal_identity import corpus_digest

    roles = {role for role in ("editor", "reflector") if snapshot.identity[role]["kind"] == "chat"}
    if roles != chat_roles or snapshot.identity["corpus_digest"] != corpus_digest(cases):
        raise CoreRawUnavailable("binding_mismatch")
    group_rounds, sessions, diagnoses = {}, [], set()
    attempts = 0
    for record in log.records:
        payload = record.payload
        if record.event == "identity_rebased":
            raise CoreRawUnavailable("identity_changed")
        if record.event == "grouping_finished":
            group_rounds[payload["round"]] = len(payload["result"]["assignments"])
        elif record.event == "reflection_started":
            sessions.append(("reflector", payload["reflection_id"], payload["round"] in group_rounds))
        elif record.event == "attempt_opened":
            attempts += 1
            sessions.append(("editor", payload["attempt_id"], payload["round"] in group_rounds))
        elif record.event == "reflection_finished" and payload.get("groups"):
            diagnoses.add(payload["reflection_id"])
    return dict(groups=group_rounds, sessions=sessions, diagnoses=diagnoses, attempts=attempts,
                shadow_repeats=_preflight_shadow_repeats(log.records, snapshot, roles),
                head=log.valid_bytes, producing_version=snapshot.producing_version,
                measurements={key: value["identity_digest"] for key, value in snapshot.measurements.items()})


def preflight_export_counts(rows, run: Path, heads, measurements, workflow):
    """Apply the role's full-view projection only to the supplied export, retaining counts."""
    from evolution.tree import JournalBoundaryError, inspect_journal
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE
    from evolution.tree.evidence_view import cut_text, is_marker, render_turns, text_of

    _require_pin()
    evidence = workflow.evidence or DEFAULT_EVIDENCE
    checked = set()
    before_turns = before_events = after_turns = after_events = 0
    before_chars = after_chars = 0
    seen = 0

    def retained(source, rendered, unit):
        # Core's counted middle-cut markers identify the original retained items.
        position = 0
        for item in rendered:
            if is_marker(item):
                position += item["cut"][unit]
            else:
                yield source[position], item
                position += 1

    for row in rows:
        measurement = row["measurement"]["id"]
        if row.get("record") != "try" or measurement not in measurements:
            raise CoreRawUnavailable("unreadable")
        if measurement not in checked:
            file = f"measurements/{measurement}/events.jsonl"
            try:
                journal = inspect_journal(run / "measurements" / measurement)
            except JournalBoundaryError:
                raise CoreRawUnavailable("unreadable") from None
            if (not journal.records or journal.valid_bytes != heads.get(file)
                    or journal.records[0].payload["identity_digest"] != measurements[measurement]):
                raise CoreRawUnavailable("revision_mismatch")
            checked.add(measurement)
        turns = row.get("turns")
        if not isinstance(turns, list) or not all(isinstance(t, dict) and isinstance(t.get("events"), list)
                                                  and isinstance(t.get("message"), dict) for t in turns):
            continue
        seen += 1
        before_turns += len(turns)
        before_events += sum(len(t["events"]) for t in turns)
        before_chars += sum(len(text_of(value)) for t in turns for event in t["events"] for value in event.values())
        if evidence.view == "full":
            shown, _ = render_turns(turns, evidence, ())
            after_turns += sum(not is_marker(t) for t in shown)
            after_events += sum(not is_marker(event) for t in shown if not is_marker(t) for event in t["events"])
            for turn, rendered in retained(turns, shown, "turns"):
                for event, _ in retained(turn["events"], rendered["events"], "events"):
                    for value in event.values():
                        text = text_of(value)
                        _, removed = cut_text(text, evidence.text_chars)
                        after_chars += len(text) - removed
    return evidence, seen, (
        ("turns", "turns", before_turns, after_turns),
        ("events", "events_per_turn", before_events, after_events),
        ("characters", "text_chars", before_chars, after_chars),
    )


def preflight_baseline(directory: Path, run_id: str, cases) -> tuple:
    """Reopen a Lab fixed measurement; bind its train cases to the scorer corpus identity."""
    from evolution.tree.baseline_import import BaselineSourceUnavailable
    from evolution.tree.baseline_sources import read_standalone_source
    from evolution.tree.execution.journal_identity import corpus_digest

    _require_pin()
    try:
        source = read_standalone_source(directory)
    except BaselineSourceUnavailable:
        raise CoreRawUnavailable("baseline_unavailable") from None
    records = source.measurement.records
    if (source.kind != "measurement" or source.measurement.run_id != run_id
            or records[0].payload["identity"]["corpus_digest"] != corpus_digest(cases)):
        raise CoreRawUnavailable("baseline_mismatch")
    turns = [record for record in records if record.event == "target_send_finished"]
    usages = []
    for turn in turns:
        usage = turn.payload.get("usage")
        tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
        if type(tokens) is int and tokens >= 0:
            usages.append(tokens)
    return len(turns), usages, records[0].payload.get("producing_version")
