"""Pure family-15 deltas for CampaignLifecycle's atomic prepare/commit path.

No sibling bytes are read here. Local references prove admitted campaign order;
external publications and measurement prefixes are assertions authenticated by E.
Numerical ranking belongs to E, since its exact values are external bounded rows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from evolution.tree.artifacts.artifact_payloads import AuxiliarySourceReference
from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
from evolution.tree.campaign_policy import CampaignPolicyV12, decides_at_once
from evolution.tree.campaign_schema import (
    CHAMPION_SIDE,
    AttemptState,
    CampaignLifecycle,
    RoundState,
    _need,
)
from evolution.tree.execution import durable_reference as _durable_reference
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.family15_schema import (
    GROUPING_CHECKPOINTS,
    Family15AttemptState,
    Family15Grouping,
    Family15Record,
    Family15RoundState,
    Family15SnapshotState,
)
from evolution.tree.storage.envelope import Envelope


class Family15Lifecycle:
    """Adjunct indexes only; the owning lifecycle remains the sole state machine."""

    def __init__(self, owner: CampaignLifecycle):
        self.owner = owner
        self.records: dict[str, Family15Record] = {}
        self.current: dict[str, str] = {}
        #: Every grouping in journal order with its checkpoints (b16 §9): the
        #: adoption lookup reads it, never the journal.
        self.groupings: dict[str, Family15Grouping] = {}

    def snapshot(self) -> Family15SnapshotState:
        from evolution.tree.execution.check_modes import parse_checks

        modes = {}
        hits: dict[str, int] = {}
        for record in self.records.values():
            payload = record.payload
            if record.envelope.event in {"campaign_created", "identity_rebased"}:
                checks = payload.get("adaptation_payload", {}).get("checks")
                modes = {} if checks is None else dict(parse_checks(checks).non_enforce())
            entries = (
                [payload]
                if record.envelope.event == "check_shadowed"
                else payload.get("shadowed", ())
            )
            for entry in entries:
                hits[entry["check"]] = hits.get(entry["check"], 0) + entry["count"]
        return Family15SnapshotState(
            check_modes=modes,
            shadow_hits=hits,
            current_measurements=self.current,
            declarations=tuple(
                r for r in self.records.values() if r.envelope.event == "auxiliary_payload_declared"
            ),
            diagnostic_imports=tuple(
                r for r in self.records.values() if r.envelope.event == "diagnostic_import_declared"
            ),
            groupings=self.groupings,
        )

    @property
    def policy(self) -> CampaignPolicyV12:
        return cast(CampaignPolicyV12, self.owner.policy)

    def record(self, envelope: Envelope) -> Family15Record:
        raw = envelope.encode()
        start = self.owner._valid_bytes
        return Family15Record(
            envelope,
            AuxiliarySourceReference(
                1,
                "journal_record",
                envelope.to_payload()["run_id"],
                f"campaign.jsonl#bytes={start}:{start + len(raw)}",
                hashlib.sha256(raw).hexdigest(),
            ),
        )

    def remember(self, envelope: Envelope) -> None:
        record = self.record(envelope)
        self.records[record.reference.locator] = record
        event = envelope.event
        if event == "grouping_started":
            p = envelope.payload
            self.groupings[p["grouping_id"]] = Family15Grouping(
                p["grouping_id"], p["round"], p["baseline_binding"], p["policy_digest"]
            )
        elif event in ("grouping_finished", "grouping_failed"):
            p = envelope.payload
            grouping = self.groupings[p["grouping_id"]]
            self.groupings[p["grouping_id"]] = (
                replace(grouping, status="finished")
                if event == "grouping_finished"
                else replace(
                    grouping, status="failed", code=p["code"], error_class=p.get("error_class")
                )
            )

    def checkpoint(
        self, event: str, p: Mapping[str, Any], record: Family15Record
    ) -> Callable[[], None]:
        """A grouping checkpoint (b16 §9): it names the started grouping, once.

        A portion id, or a ``(level, index)``, is recorded at most once per
        grouping, and never beside a half or a parent of itself: a split
        portion is checkpointed by its halves only. Every portion of one
        grouping names one plan.
        """

        grouping_id = p["grouping_id"]
        grouping = self.groupings.get(grouping_id)
        _durable_reference._refuse(
            grouping is not None and grouping.status == "started",
            "a grouping checkpoint names the started grouping",
        )
        assert grouping is not None
        state = self.round(grouping.round).grouping
        _durable_reference._refuse(
            state is not None
            and state["status"] == "started"
            and state["grouping_id"] == grouping_id,
            "a grouping checkpoint names the started grouping",
        )

        def overlaps(a: str, b: str) -> bool:
            return a == b or a.startswith(b + ".") or b.startswith(a + ".")

        if event == "grouping_portion_finished":
            portion = p["portion_id"]
            _durable_reference._refuse(
                not any(overlaps(portion, other) for other in grouping.portions),
                "a grouping portion is checkpointed once",
            )
            _durable_reference._refuse(
                grouping.plan_digest in (None, p["plan_digest"]),
                "a grouping's portions and batches name one plan",
            )

            def apply_portion() -> None:
                current = self.groupings[grouping_id]
                self.groupings[grouping_id] = replace(
                    current,
                    plan_digest=p["plan_digest"],
                    portions={**current.portions, portion: record},
                )

            return apply_portion
        level, index = p["level"], p["index"]
        _durable_reference._refuse(
            not any(at == level and overlaps(index, other) for at, other in grouping.batches),
            "a grouping batch is checkpointed once",
        )
        # A grouping whose every portion was adopted checkpoints batches only.
        _durable_reference._refuse(
            grouping.plan_digest in (None, p["plan_digest"]),
            "a grouping's portions and batches name one plan",
        )

        def apply_batch() -> None:
            current = self.groupings[grouping_id]
            self.groupings[grouping_id] = replace(
                current,
                plan_digest=p["plan_digest"],
                batches={**current.batches, (level, index): record},
            )

        return apply_batch

    def resolve(self, value: Mapping[str, Any], *events: str) -> Family15Record:
        ref = AuxiliarySourceReference.from_payload(value)
        record = _need(self.records.get(ref.locator), "campaign reference is not admitted")
        _durable_reference._refuse(ref == record.reference, "campaign reference identity differs")
        _durable_reference._refuse(
            record.envelope.event in events, "campaign reference names another event"
        )
        return record

    def publication(self, value: Mapping[str, Any]) -> None:
        pub = AuxiliaryPublication.from_payload(value)
        declared = self.resolve(
            pub.declaration.declaration_ref.to_payload(), "auxiliary_payload_declared"
        )
        expected = pub.declaration.to_payload()
        expected.pop("declaration_ref")
        _durable_reference._refuse(
            declared.payload == expected, "publication differs from admitted declaration"
        )
        # S2 owns a separate run ID; E authenticates its local ledger terminal.

    def attempt(self, p: Mapping[str, Any]) -> AttemptState:
        a = self.owner._attempt(p["attempt_id"])
        if "round" in p:
            _durable_reference._refuse(a.round == p["round"], "record names another Attempt round")
        return a

    @staticmethod
    def adjunct(a: AttemptState) -> Family15AttemptState:
        return _need(a.family15, "family-15 Attempt adjunct is missing")

    def round(self, n: int) -> RoundState:
        return self.owner._round(n, "round does not exist")

    @staticmethod
    def round_adjunct(r: RoundState) -> Family15RoundState:
        return r.family15 or Family15RoundState()

    def put_attempt(self, a: AttemptState, **changes: Any) -> None:
        current = self.owner._attempt(a.attempt_id)
        self.owner._put_attempt(
            replace(current, family15=replace(self.adjunct(current), **changes))
        )

    def put_round(self, n: int, **changes: Any) -> None:
        r = self.round(n)
        self.owner._put_round(replace(r, family15=replace(self.round_adjunct(r), **changes)))

    def summary(self, value: Mapping[str, Any], *, settled: bool = False) -> Mapping[str, Any]:
        entry = _need(
            self.owner._measurements.get(value["measurement_id"]), "measurement is not bound"
        )
        _durable_reference._refuse(
            all(value[k] == entry[k] for k in ("candidate_digest", "identity_digest")),
            "measurement summary identity differs",
        )
        _durable_reference._refuse(
            not settled or value["prefix"] is not None, "settled measurement needs prefix"
        )
        return entry

    def finalists(self, r: RoundState) -> list[AttemptState]:
        return sorted(
            (a for a in r.attempts.values() if a.outcome == "finalist"), key=lambda a: a.index
        )

    def ready(self, r: RoundState) -> bool:
        if any(a.outcome is None for a in r.attempts.values()):
            return False
        finalists = self.finalists(r)
        if any(self.adjunct(a).global_result is None for a in finalists):
            return False
        return (self.policy.round_stop == "first_accepted" and bool(self.eligible(r))) or len(
            r.attempts
        ) == self.policy.attempts_per_round

    def eligible(self, r: RoundState) -> list[AttemptState]:
        return [
            a
            for a in self.finalists(r)
            if (result := self.adjunct(a).global_result) is not None and result.payload["accepted"]
        ]

    def preamble(self, event: str, p: Mapping[str, Any]) -> None:
        for r in self.owner._rounds.values():
            extra = self.round_adjunct(r)
            if r.closed is not None:
                continue
            if any(self.adjunct(a).path == "early_global" for a in r.attempts.values()):
                _durable_reference._refuse(
                    event
                    not in {
                        "grouping_started",
                        "focus_portion_selected",
                        "reflection_started",
                        "probe_checked",
                        "gate_scope_selected",
                    },
                    "early-global rejection dispatches no role work",
                )
            if extra.selection is not None:
                _durable_reference._refuse(
                    event in {"round_closed", "capacity_exhausted", "auxiliary_payload_declared"},
                    "selection owes its exact round closure",
                )
        if event in {
            "comparison_completed",
            "focus_decided",
            "gate_measurement_completed",
            "candidate_completion_completed",
            "final_measurement_completed",
            "global_comparison_completed",
            "selection_decided",
        }:
            _durable_reference._refuse(
                not self.owner.pending, "operational stages settle before comparison or selection"
            )

    def rule(
        self, event: str, p: dict[str, Any], op: str | None, seq: int, envelope: Envelope
    ) -> Callable[[], None] | None:
        if event in GROUPING_CHECKPOINTS:
            return self.checkpoint(event, p, self.record(envelope))
        if event == "check_shadowed":
            if p["check"] == "connection_preflight":
                _durable_reference._refuse(
                    self.owner._connection_shadow_settlement(event, p),
                    "a preflight shadow hit settles its matching owed failure",
                )

            def apply_shadow() -> None:
                if self.owner._connection_shadow_settlement(event, p):
                    self.owner._connection_owed = None

            return apply_shadow
        if event in {
            "attempt_opened",
            "comparison_preflight_failed",
            "comparison_bound",
            "comparison_completed",
            "focus_decided",
            "gate_measurement_completed",
            "candidate_gate_sealed",
            "finalists_bound",
            "champion_final_bound",
            "candidate_completion_bound",
            "candidate_completion_completed",
            "final_measurement_bound",
            "final_measurement_completed",
            "global_comparison_completed",
            "selection_decided",
            "gate_scope_selected",
        }:
            r = self.round(p["round"])
            _durable_reference._refuse(
                r.closed is None and r.selection is None,
                "family15 work requires open undecided round",
            )
        if "data" in p:
            self.publication(p["data"])
        if event == "diagnostic_import_declared":
            self.publication(p["bundle"])
            _durable_reference._refuse(
                p["measurement_id"] not in self.owner._measurements,
                "diagnostics precede measurement birth",
            )
            return lambda: None
        handlers = {
            "auxiliary_payload_declared": self.declaration,
            "attempt_opened": self.open_attempt,
            "comparison_preflight_failed": self.preflight,
            "comparison_bound": self.bind_comparison,
            "comparison_completed": self.complete_comparison,
            "focus_decided": self.decide_focus,
            "gate_measurement_completed": self.complete_gate_side,
            "candidate_gate_sealed": self.seal_gate,
            "finalists_bound": self.bind_finalists,
            "champion_final_bound": self.bind_champion,
            "candidate_completion_bound": self.bind_completion,
            "candidate_completion_completed": self.complete_completion,
            "final_measurement_bound": self.bind_final,
            "final_measurement_completed": self.complete_final,
            "global_comparison_completed": self.complete_global,
            "selection_decided": self.select,
        }
        if event == "attempt_closed" and "path" in p:
            return self.close_attempt(p, self.record(envelope))
        if event == "measurement_rebound":
            source = self.owner._measurements.get(p["from_measurement_id"])
            if source is not None and source.get("role") == "family15_completion":
                _durable_reference._refuse(
                    p["imported"] <= source["required"],
                    "completion rebound imports exceed required coverage",
                )
        if event in {
            "stage_opened",
            "stage_redispatched",
            "repairs_dispatched",
            "indeterminate_observed",
        }:
            entry = self.owner._measurements.get(p["measurement_id"])
            if entry and entry.get("role") in {"candidate", "parent"}:
                _durable_reference._refuse(
                    not entry["complete"], "completed gate side is immutable"
                )
        if event == "stage_opened":
            mid = p["measurement_id"]
            entry = self.owner._measurements.get(mid)
            if entry and entry.get("role") in {"candidate", "parent"}:
                a = self.owner._attempt(entry["attempt_id"])
                binding = _need(
                    self.adjunct(a).comparison_binding,
                    "Candidate stage needs family-15 comparison binding",
                )
                _durable_reference._refuse(
                    self.adjunct(a).preflight is None, "failed preflight dispatches nothing"
                )
                _durable_reference._refuse(
                    mid
                    in {
                        binding.payload["candidate"]["measurement_id"],
                        binding.payload["reference"]["measurement_id"],
                    },
                    "stage names superseded comparison",
                )
                if self.policy.focus_first:
                    self.check_gate_stage(a, entry, p)
                else:
                    _durable_reference._refuse("scope" not in p, "single stages have no scope")
            else:
                _durable_reference._refuse(
                    "scope" not in p, "only focus-first gate stages have scope"
                )
        if event in {
            "attempt_input_frozen",
            "editor_started",
            "attempt_sealed",
            "candidate_inspected",
            "affected_cases_selected",
            "measurement_bound",
        } and p.get("attempt_id") not in (None, CHAMPION_SIDE):
            a = self.attempt(p)
            _durable_reference._refuse(
                self.adjunct(a).path == "candidate", "early-global Attempt has no Candidate work"
            )
        handler = handlers.get(event)
        if handler is not None:
            return handler(p, self.record(envelope))
        return None

    def declaration(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        return lambda: None

    def open_attempt(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        o = self.owner
        r = self.round(p["round"])
        _durable_reference._refuse(
            r.baseline_complete and r.closed is None and r.selection is None,
            "Attempt needs complete undecided baseline",
        )
        _durable_reference._refuse(
            self.round_adjunct(r).finalists is None, "frozen finalist barrier forbids Attempts"
        )
        _durable_reference._refuse(
            p["attempt_id"] not in o._attempt_round, "Attempt id already used"
        )
        _durable_reference._refuse(
            p["attempt_index"] == len(r.attempts) + 1 <= o.policy.attempts_per_round,
            "Attempt order/budget",
        )
        holds = o._holds_attempts() and not decides_at_once(o.policy)
        _durable_reference._refuse(
            sum(
                a.outcome is None and not (holds and o._awaiting_decision(a))
                for a in r.attempts.values()
            )
            < o.policy.parallel_attempts,
            "parallel Attempt budget",
        )
        _durable_reference._refuse(
            (p["parent_version"], p["parent_digest"]) == o._champion,
            "Attempt starts from incumbent",
        )
        if p["path"] == "early_global":
            _durable_reference._refuse(
                r.gate_scope is None and not r.reflections and r.grouping is None,
                "early global precedes roles and F/C",
            )
            _durable_reference._refuse(
                o.policy.gate_mode != "unfiltered" and o.policy.global_mode != "off",
                "disabled global has no preflight",
            )
        else:
            _durable_reference._refuse(
                r.gate_scope is not None, "Candidate Attempt requires gate scope"
            )
        if o.policy.round_stop == "first_accepted":
            _durable_reference._refuse(
                all(self.adjunct(a).global_result is not None for a in self.finalists(r)),
                "finalist completes before next Attempt",
            )
            _durable_reference._refuse(not self.eligible(r), "accepted finalist stops Attempts")

        def apply() -> None:
            o._attempt_round[p["attempt_id"]] = p["round"]
            o._put_attempt(
                AttemptState(
                    p["attempt_id"],
                    p["round"],
                    p["attempt_index"],
                    p["parent_version"],
                    p["parent_digest"],
                    family15=Family15AttemptState(path=p["path"]),
                )
            )

        return apply

    def preflight(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        _durable_reference._refuse(
            a.outcome is None and extra.preflight is None and extra.comparison is None,
            "preflight already settled",
        )
        _durable_reference._refuse(
            p["epoch"] == len(self.owner._epochs) - 1 and p["mode"] == self.policy.gate_mode,
            "preflight policy/epoch",
        )
        _durable_reference._refuse(
            self.policy.gate_mode != "statistical"
            or (p["phase"] == "early_global" and self.policy.global_mode == "point"),
            "statistical has only the early global point preflight",
        )
        _durable_reference._refuse(
            (p["phase"] == "early_global") == (extra.path == "early_global"),
            "preflight path differs",
        )
        _durable_reference._refuse(
            a.measurement_id is None
            or not self.owner._measurements[str(a.measurement_id)]["stages"],
            "preflight precedes Candidate dispatch",
        )
        return lambda: self.put_attempt(a, preflight=record)

    def bind_comparison(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        _durable_reference._refuse(
            a.outcome is None
            and extra.path == "candidate"
            and extra.preflight is None
            and extra.comparison is None,
            "comparison requires open Candidate",
        )
        _durable_reference._refuse(
            p["epoch"] == len(self.owner._epochs) - 1, "comparison epoch differs"
        )
        _durable_reference._refuse(
            p["candidate"]["measurement_id"] == a.measurement_id,
            "comparison measures its Candidate",
        )
        self.comparison_side(p["candidate"], extra.gate_candidate)
        r = self.round(a.round)
        expected = (
            a.parent_measurement_id if self.policy.fresh_parent else r.baseline_measurement_id
        )
        _durable_reference._refuse(
            p["reference"]["measurement_id"] == expected, "comparison reference is not its Parent"
        )
        _durable_reference._refuse(
            p["reference_kind"] == ("fresh" if self.policy.fresh_parent else "stored"),
            "comparison reference policy",
        )
        if self.policy.fresh_parent:
            self.comparison_side(p["reference"], extra.gate_parent)
        else:
            self.summary(p["reference"], settled=True)
        if not self.policy.fresh_parent:
            _durable_reference._refuse(
                p["reference"]["prefix"] == r.baseline_reference,
                "stored comparison preserves exact round baseline prefix",
            )
        if extra.comparison_binding is not None:
            old = extra.comparison_binding.payload
            _durable_reference._refuse(
                any(
                    old[k]["measurement_id"] != p[k]["measurement_id"]
                    for k in ("candidate", "reference")
                ),
                "comparison is already bound",
            )
        decision = extra.focus_decision
        if decision is not None:
            _durable_reference._refuse(
                self.policy.focus_first and decision.payload["outcome"] == "passed",
                "only passing focus survives rebinding",
            )
            _durable_reference._refuse(
                p.get("focus")
                == dict(
                    seq=decision.envelope.to_payload()["sequence"],
                    digest=decision.reference.content_digest,
                    outcome="passed",
                ),
                "replacement binding must cite its own exact focus",
            )
        else:
            _durable_reference._refuse("focus" not in p, "initial binding cannot carry focus")
        return lambda: self.put_attempt(a, comparison_binding=record)

    def comparison_side(self, summary: Mapping[str, Any], complete: Family15Record | None) -> None:
        entry = self.summary(summary, settled=complete is not None)
        if complete is not None:
            _durable_reference._refuse(
                entry["complete"] and summary == complete.payload["source"],
                "comparison preserves exact completed gate side",
            )
        else:
            _durable_reference._refuse(
                not entry["complete"]
                and not entry["frozen"]
                and not entry["stages"]
                and summary["prefix"] is None,
                "comparison precedes unfinished side dispatch",
            )

    def scope_cases(self, a: AttemptState) -> tuple[frozenset[str], frozenset[str]]:
        from evolution.tree.campaign_schema import confidence_scopes

        r = self.round(a.round)
        corpus = self.owner.identity["corpus_cases"]
        selection = _need(a.selection, "scope needs affected-case selection")
        gate_scope = _need(r.gate_scope, "scope needs frozen exam cases")
        focus, causal = confidence_scopes(selection, corpus, gate_scope["cases"])
        return frozenset(corpus[i]["case_id"] for i in focus), frozenset(
            corpus[i]["case_id"] for i in causal
        )

    def check_gate_stage(
        self, a: AttemptState, entry: Mapping[str, Any], p: Mapping[str, Any]
    ) -> None:
        scope = "focus" if p["stage"] == 1 else "causal"
        _durable_reference._refuse(
            p["stage"] in (1, 2) and p.get("scope") == scope, "gate stage scope differs"
        )
        extra = self.adjunct(a)
        if scope == "focus":
            _durable_reference._refuse(
                extra.focus_decision is None
                or (
                    extra.comparison_binding is not None
                    and "focus" in extra.comparison_binding.payload
                    and entry.get("rebound_from") is not None
                    and p["sample_count"] == 0
                ),
                "a carried focus stage imports every row without redispatch",
            )
        else:
            decision = _need(extra.focus_decision, "causal dispatch needs a focus decision")
            binding = _need(extra.comparison_binding, "causal dispatch needs comparison binding")
            _durable_reference._refuse(
                decision.payload["outcome"] == "passed"
                and (
                    decision.payload["binding"] == binding.reference.to_payload()
                    or "focus" in binding.payload
                ),
                "causal dispatch needs the current passing focus decision",
            )
        if entry["role"] == "parent":
            candidate = self.owner._measurements[str(a.measurement_id)]
            _durable_reference._refuse(
                any(s.get("scope") == scope for s in candidate["stages"]),
                "Parent stage follows the Candidate scope",
            )

    def decide_focus(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        _durable_reference._refuse(
            self.policy.focus_first and a.outcome is None, "focus decision requires focus_first"
        )
        _durable_reference._refuse(
            extra.focus_decision is None and extra.comparison is None, "focus is already decided"
        )
        _durable_reference._refuse(
            p["reason"] not in {"candidate_incomplete", "reference_incomplete"},
            "incomplete owned focus cannot decide a comparison",
        )
        bound = self.resolve(p["binding"], "comparison_bound")
        _durable_reference._refuse(
            bound == extra.comparison_binding, "focus names current comparison binding"
        )
        _durable_reference._refuse(
            ("seed" in p) == (self.policy.gate_mode == "statistical"), "focus seed policy differs"
        )
        if "seed" in p:
            _durable_reference._refuse(
                p["seed"]["campaign_seed"] == self.policy.random_seed
                and p["seed"]["look"] == self.policy.repeats
                and p["seed"]["binding"] == bound.payload["data"]["declaration"]["content_digest"],
                "focus seed differs from binding or policy",
            )
        for mid in (
            a.measurement_id,
            a.parent_measurement_id if self.policy.fresh_parent else None,
        ):
            if mid is not None:
                entry = self.owner._measurements[mid]
                _durable_reference._refuse(
                    len(entry["stages"]) == 1 and entry["stages"][0].get("scope") == "focus",
                    "focus decision follows every owned focus stage",
                )
        _durable_reference._refuse(not self.owner._threshold_crossed(), "a threshold is crossed")
        self.owner._check_not_held(a)
        return lambda: self.put_attempt(a, focus_decision=record)

    def complete_gate_side(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        binding = self.resolve(p["binding"], "comparison_bound")
        _durable_reference._refuse(
            a.outcome is None and extra.comparison is None and binding == extra.comparison_binding,
            "gate completion needs current own undecided comparison binding",
        )
        if self.policy.focus_first:
            decision = _need(extra.focus_decision, "gate completion follows focus decision")
            expected_scopes = (
                ["focus", "causal"] if decision.payload["outcome"] == "passed" else ["focus"]
            )
            _durable_reference._refuse(
                p.get("scopes") == expected_scopes,
                "gate completion scopes differ from decision path",
            )
        else:
            _durable_reference._refuse("scopes" not in p, "single completion has no scopes")
        side = p["side"]
        field = "gate_candidate" if side == "candidate" else "gate_parent"
        _durable_reference._refuse(getattr(extra, field) is None, "gate side is already complete")
        _durable_reference._refuse(
            side == "candidate" or self.policy.fresh_parent,
            "stored Parent has no fresh gate completion",
        )
        mid = a.measurement_id if side == "candidate" else a.parent_measurement_id
        source = p["source"]
        expected = binding.payload["candidate" if side == "candidate" else "reference"]
        _durable_reference._refuse(
            source["measurement_id"] == mid
            and all(
                source[k] == expected[k]
                for k in ("measurement_id", "identity_digest", "candidate_digest")
            ),
            "gate completion changes current owned side",
        )
        entry = self.summary(source, settled=True)
        if (
            self.policy.focus_first
            and p["scopes"] == ["focus", "causal"]
            and self.scope_cases(a)[1]
        ):
            _durable_reference._refuse(
                any(stage.get("scope") == "causal" for stage in entry["stages"]),
                "full completion follows causal stage",
            )
        _durable_reference._refuse(
            not entry["complete"] and not entry["frozen"] and bool(entry["stages"]),
            "gate completion follows live owned stage",
        )
        _durable_reference._refuse(not self.owner._threshold_crossed(), "a threshold is crossed")
        _durable_reference._refuse(
            not self.owner._unreserved_threshold_repair(str(mid)), "a threshold repair is owed"
        )

        def apply() -> None:
            self.put_attempt(a, **{field: record})
            self.owner._measurements[str(mid)]["complete"] = True
            self.owner._completion_versions[str(mid)] = self.owner.producing_version

        return apply

    def complete_comparison(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        binding = self.resolve(p["binding"], "comparison_bound")
        _durable_reference._refuse(
            binding == extra.comparison_binding and extra.comparison is None and a.outcome is None,
            "comparison result names current unique binding",
        )
        if self.policy.focus_first:
            focus = _need(extra.focus_decision, "comparison follows durable focus decision")
            if focus.payload["outcome"] != "passed":
                _durable_reference._refuse(
                    all(p[k] == focus.payload[k] for k in ("outcome", "reason"))
                    and p["deciding_scope"] == "focus",
                    "comparison changes focus verdict",
                )
            for completed in (
                extra.gate_candidate,
                extra.gate_parent if self.policy.fresh_parent else None,
            ):
                if completed is not None and p["outcome"] == "passed":
                    _durable_reference._refuse(
                        completed.payload.get("scopes") == ["focus", "causal"],
                        "passing comparison requires full scope completion",
                    )
        _durable_reference._refuse(
            p["reason"] not in {"candidate_incomplete", "reference_incomplete"},
            "incomplete owned evidence cannot close a statistical comparison",
        )
        for name, completed in (
            ("candidate", extra.gate_candidate),
            ("reference", extra.gate_parent),
        ):
            if name == "candidate" or binding.payload["reference_kind"] == "fresh":
                side = _need(completed, "comparison waits for every owned gate side completion")
                _durable_reference._refuse(
                    p[name] == side.payload["source"],
                    "comparison changes completed gate side prefix",
                )
            self.summary(p[name], settled=True)
            _durable_reference._refuse(
                all(
                    p[name][k] == binding.payload[name][k]
                    for k in ("measurement_id", "identity_digest", "candidate_digest")
                ),
                "comparison result changes measurement",
            )
        if binding.payload["reference_kind"] == "stored":
            _durable_reference._refuse(
                p["reference"] == binding.payload["reference"], "stored reference prefix changed"
            )
        _durable_reference._refuse(
            bool(self.owner._measurements[str(a.measurement_id)]["stages"]),
            "comparison follows Candidate stage",
        )
        self.owner._check_not_held(a)

        return lambda: self.put_attempt(a, comparison=record)

    def close_attempt(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        _durable_reference._refuse(
            a.outcome is None and p["path"] == extra.path, "Attempt close repeats or changes path"
        )
        evidence = self.resolve(
            p["evidence"], "comparison_preflight_failed", "comparison_completed"
        )
        _durable_reference._refuse(
            evidence in (extra.preflight, extra.comparison), "Attempt close cites another result"
        )
        if evidence.envelope.event == "comparison_completed":
            expected = "finalist" if evidence.payload["outcome"] == "passed" else "rejected"
            _durable_reference._refuse(
                p["outcome"] == expected and p["reason"] == evidence.payload["reason"],
                "Attempt close contradicts result",
            )
        else:
            _durable_reference._refuse(
                p["outcome"] == "rejected" and p["reason"] == "structurally_unattainable",
                "preflight closure differs",
            )

        def apply() -> None:
            self.owner._put_attempt(
                replace(
                    a,
                    outcome=p["outcome"],
                    reason=p["reason"],
                    closed_seq=record.envelope.to_payload()["sequence"],
                    closed_record_digest=record.reference.content_digest,
                    closed_record_locator=record.reference.locator,
                )
            )

        return apply

    def seal_gate(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        result = _need(extra.comparison, "gate seal needs comparison")
        _durable_reference._refuse(
            a.outcome == "finalist" and extra.gate_seal is None, "only unique finalist gate seals"
        )
        _durable_reference._refuse(
            p["source"] == result.payload["candidate"],
            "gate seal must preserve completed Candidate prefix",
        )

        def apply() -> None:
            self.put_attempt(a, gate_seal=record)
            self.owner._measurements[p["source"]["measurement_id"]]["complete"] = True
            self.owner._completion_versions[p["source"]["measurement_id"]] = (
                self.owner.producing_version
            )

        return apply

    def bind_finalists(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        r = self.round(p["round"])
        _durable_reference._refuse(
            self.policy.round_stop == "all_attempts" and self.round_adjunct(r).finalists is None,
            "unique all-attempts barrier",
        )
        _durable_reference._refuse(
            len(r.attempts) == self.policy.attempts_per_round
            and all(a.outcome is not None for a in r.attempts.values()),
            "all Attempts close before finalist barrier",
        )
        finalists = self.finalists(r)
        _durable_reference._refuse(
            all(self.adjunct(a).gate_seal is not None for a in finalists),
            "finalists seal gates before barrier",
        )
        digest = hashlib.sha256(canonical_json([a.attempt_id for a in finalists])).hexdigest()
        _durable_reference._refuse(
            p["count"] == len(finalists) and p["attempts_digest"] == digest, "finalist set differs"
        )
        return lambda: self.put_round(r.round, finalists=record)

    def barrier(self, a: AttemptState) -> None:
        _durable_reference._refuse(
            a.outcome == "finalist" and self.adjunct(a).gate_seal is not None,
            "final work requires closed sealed finalist",
        )
        if self.policy.round_stop == "all_attempts":
            _need(
                self.round_adjunct(self.round(a.round)).finalists,
                "final work waits for finalist barrier",
            )

    def champion_binding(self, a: AttemptState) -> Family15Record | None:
        entries = self.round_adjunct(self.round(a.round)).champions
        return next((e for e in entries if e.payload["attempt_id"] in (None, a.attempt_id)), None)

    def bind_champion(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        r = self.round(p["round"])
        extra = self.round_adjunct(r)
        _durable_reference._refuse(
            self.policy.stability == "unstable", "stable final uses stored Champion"
        )
        if self.policy.round_stop == "all_attempts":
            _durable_reference._refuse(
                p["attempt_id"] is None and p["finalists"] is not None,
                "shared Champion belongs to finalist set",
            )
            _durable_reference._refuse(
                self.resolve(p["finalists"], "finalists_bound") == extra.finalists
                and not extra.champions,
                "shared Champion needs unique frozen set",
            )
            _durable_reference._refuse(
                bool(self.finalists(r)), "empty finalist set needs no Champion"
            )
        else:
            a = self.attempt(p)
            self.barrier(a)
            _durable_reference._refuse(
                p["finalists"] is None and self.champion_binding(a) is None,
                "per-finalist Champion is unique",
            )
        _durable_reference._refuse(
            p["candidate_digest"] == self.owner._champion[1], "fresh Champion names incumbent"
        )
        _durable_reference._refuse(
            p["measurement_id"] not in self.owner._measurements, "measurement already bound"
        )

        def apply() -> None:
            self.owner._register_measurement(
                p["measurement_id"],
                side=CHAMPION_SIDE,
                number=r.round,
                candidate_digest=p["candidate_digest"],
                identity_digest=p["identity_digest"],
                role="family15_champion",
                case_subset=None,
            )
            self.current[p["measurement_id"]] = p["measurement_id"]
            self.put_round(r.round, champions=(*extra.champions, record))

        return apply

    def final_ready(self, a: AttemptState) -> None:
        self.barrier(a)
        if self.policy.stability == "unstable":
            _need(self.champion_binding(a), "fresh Champion must bind before Candidate final work")

    def closure(self, a: AttemptState, value: Mapping[str, Any]) -> None:
        record = self.resolve(value, "attempt_closed")
        _durable_reference._refuse(
            record.envelope.to_payload()["sequence"] == a.closed_seq
            and record.payload["outcome"] == "finalist",
            "final work cites its own finalist closure",
        )

    def bind_completion(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        self.final_ready(a)
        _durable_reference._refuse(
            self.policy.verification == "reuse" and extra.completion_binding is None,
            "reuse completion is unique",
        )
        self.closure(a, p["finalist_closure"])
        _durable_reference._refuse(
            self.resolve(p["gate_seal"], "candidate_gate_sealed") == extra.gate_seal,
            "completion imports its own sealed gate",
        )
        _durable_reference._refuse(
            p["measurement_id"] not in self.owner._measurements, "completion ID already bound"
        )

        def apply() -> None:
            self.owner._register_measurement(
                p["measurement_id"],
                side=a.attempt_id,
                number=a.round,
                candidate_digest=str(a.candidate_digest),
                identity_digest=p["identity_digest"],
                role="family15_completion",
                case_subset=None,
                imported=p["imported"],
                required=p["required"],
                missing_count=p["missing_count"],
            )
            self.current[p["measurement_id"]] = p["measurement_id"]
            self.owner._import_versions[p["measurement_id"]] = self.owner.producing_version
            self.put_attempt(a, completion_binding=record)

        return apply

    def complete_completion(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        binding = self.resolve(p["binding"], "candidate_completion_bound")
        _durable_reference._refuse(
            binding == extra.completion_binding and extra.completion is None,
            "completion needs unique own binding",
        )
        mid = self.current[binding.payload["measurement_id"]]
        _durable_reference._refuse(
            p["source"]["measurement_id"] == mid and p["required"] == binding.payload["required"],
            "completion source/coverage differs",
        )
        entry = self.summary(p["source"], settled=True)
        _durable_reference._refuse(
            not entry["frozen"], "unfinished frozen completion cannot finish"
        )
        _durable_reference._refuse(
            bool(entry["stages"]) or entry["missing_count"] == 0,
            "missing completion work has no stage",
        )

        def apply() -> None:
            self.put_attempt(a, completion=record)
            self.owner._measurements[mid]["complete"] = True
            self.owner._completion_versions[mid] = self.owner.producing_version

        return apply

    def bind_final(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        self.final_ready(a)
        _durable_reference._refuse(
            extra.final_binding is None and p["verification"] == self.policy.verification,
            "final binding is unique and policy-bound",
        )
        self.closure(a, p["finalist_closure"])
        source = self.resolve(
            p["source_ref"],
            "candidate_completion_bound",
            "candidate_completion_completed",
            "candidate_gate_sealed",
        )
        candidate = p["candidate"]
        champion = p["champion"]
        _durable_reference._refuse(
            candidate["candidate_digest"] == a.candidate_digest, "final Candidate identity differs"
        )
        if p["verification"] == "reuse":
            _durable_reference._refuse(
                source in (extra.completion_binding, extra.completion),
                "final uses its own completion",
            )
            if source.envelope.event == "candidate_completion_completed":
                _durable_reference._refuse(
                    candidate == source.payload["source"],
                    "reuse final preserves full completion prefix",
                )
            else:
                _durable_reference._refuse(
                    candidate["measurement_id"] == self.current[source.payload["measurement_id"]]
                    and candidate["prefix"] is None,
                    "reuse final binds its declared unfinished completion",
                )
            self.summary(
                candidate, settled=source.envelope.event == "candidate_completion_completed"
            )
        else:
            _durable_reference._refuse(
                source == extra.gate_seal, "fresh final follows own gate seal"
            )
            _durable_reference._refuse(
                candidate["measurement_id"] not in self.owner._measurements
                and candidate["prefix"] is None,
                "fresh final needs new unmeasured Candidate",
            )
        _durable_reference._refuse(
            p["champion_side"] == ("stored" if self.policy.stability == "stable" else "fresh"),
            "final Champion policy differs",
        )
        if p["champion_side"] == "stored":
            r = self.round(a.round)
            _durable_reference._refuse(
                champion["measurement_id"] == r.baseline_measurement_id
                and champion["prefix"] == r.baseline_reference,
                "stable final keeps bound stored Champion prefix",
            )
        else:
            binding = _need(self.champion_binding(a), "missing fresh Champion binding")
            _durable_reference._refuse(
                champion["measurement_id"] == self.current[binding.payload["measurement_id"]],
                "final references its owned fresh Champion",
            )
        self.summary(champion, settled=p["champion_side"] == "stored")

        def apply() -> None:
            if p["verification"] == "fresh":
                self.owner._register_measurement(
                    candidate["measurement_id"],
                    side=a.attempt_id,
                    number=a.round,
                    candidate_digest=candidate["candidate_digest"],
                    identity_digest=candidate["identity_digest"],
                    role="family15_final",
                    case_subset=None,
                )
                self.current[candidate["measurement_id"]] = candidate["measurement_id"]
            self.put_attempt(a, final_binding=record)

        return apply

    def complete_final(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        r = self.round(p["round"])
        extra = self.round_adjunct(r)
        if p["side"] == "champion":
            binding = self.resolve(p["binding"], "champion_final_bound")
            _durable_reference._refuse(
                binding in extra.champions and binding.payload["attempt_id"] == p["attempt_id"],
                "Champion completion belongs to binding",
            )
            _durable_reference._refuse(
                not any(e.payload["binding"] == p["binding"] for e in extra.champion_completions),
                "Champion already complete",
            )
            mid = self.current[binding.payload["measurement_id"]]
        else:
            a = self.attempt(p)
            ax = self.adjunct(a)
            binding = self.resolve(p["binding"], "final_measurement_bound")
            _durable_reference._refuse(
                binding == ax.final_binding
                and ax.final_completion is None
                and binding.payload["verification"] == "fresh",
                "fresh Candidate completion needs own binding",
            )
            mid = self.current[binding.payload["candidate"]["measurement_id"]]
        _durable_reference._refuse(
            p["source"]["measurement_id"] == mid, "final completion names another measurement"
        )
        entry = self.summary(p["source"], settled=True)
        _durable_reference._refuse(
            not entry["frozen"] and bool(entry["stages"]), "final completion follows live stage"
        )

        def apply() -> None:
            self.owner._measurements[mid]["complete"] = True
            self.owner._completion_versions[mid] = self.owner.producing_version
            if p["side"] == "champion":
                self.put_round(r.round, champion_completions=(*extra.champion_completions, record))
            else:
                self.put_attempt(a, final_completion=record)

        return apply

    def complete_global(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        a = self.attempt(p)
        extra = self.adjunct(a)
        binding = self.resolve(p["binding"], "final_measurement_bound")
        _durable_reference._refuse(
            binding == extra.final_binding and extra.global_result is None,
            "global result needs unique own final binding",
        )
        completion = (
            extra.completion
            if binding.payload["verification"] == "reuse"
            else extra.final_completion
        )
        completion = _need(completion, "global comparison waits for full Candidate completion")
        _durable_reference._refuse(
            p["candidate"] == completion.payload["source"],
            "global comparison changes complete Candidate",
        )
        if binding.payload["champion_side"] == "stored":
            reference = binding.payload["champion"]
        else:
            champion = _need(self.champion_binding(a), "missing Champion binding")
            complete = next(
                (
                    c
                    for c in self.round_adjunct(self.round(a.round)).champion_completions
                    if c.payload["binding"] == champion.reference.to_payload()
                ),
                None,
            )
            reference = _need(complete, "global comparison waits for full Champion").payload[
                "source"
            ]
        _durable_reference._refuse(
            p["reference"] == reference, "global result changes Champion prefix"
        )
        return lambda: self.put_attempt(a, global_result=record)

    def incumbent(self, r: RoundState) -> dict[str, Any]:
        extra = self.round_adjunct(r)
        if self.policy.stability == "unstable" and extra.champion_completions:
            return extra.champion_completions[-1].payload["source"]
        mid = _need(r.baseline_measurement_id, "baseline missing")
        entry = self.owner._measurements[mid]
        return dict(
            measurement_id=mid,
            identity_digest=entry["identity_digest"],
            candidate_digest=entry["candidate_digest"],
            prefix=r.baseline_reference,
        )

    def select(self, p: dict[str, Any], record: Family15Record) -> Callable[[], None]:
        r = self.round(p["round"])
        extra = self.round_adjunct(r)
        _durable_reference._refuse(
            self.ready(r) and r.selection is None and extra.selection is None,
            "selection needs settled undecided round",
        )
        eligible = self.eligible(r)
        selected = None
        if eligible:
            _durable_reference._refuse(
                p["reason"] == "automatic", "automatic eligible selection reason"
            )
            selected = next(
                (
                    a
                    for a in eligible
                    if a.candidate_digest == p["selected_candidate"]
                    and (result := self.adjunct(a).global_result) is not None
                    and result.payload["candidate"] == p["selected_evidence"]
                ),
                None,
            )
            _need(selected, "automatic selection names no eligible Candidate")
            if self.policy.round_stop == "first_accepted":
                _durable_reference._refuse(
                    selected == eligible[0], "first accepted selection is exact"
                )
        else:
            _durable_reference._refuse(
                p["reason"] == "no_eligible_candidate", "retention reason differs"
            )
        source = self.incumbent(r)
        if selected is not None:
            result = _need(self.adjunct(selected).global_result, "selected Candidate has no result")
            source = result.payload["candidate"]
            _durable_reference._refuse(
                p["selected_candidate"] == selected.candidate_digest
                and p["selected_evidence"] == source,
                "selection changes exact Candidate/evidence",
            )
        else:
            _durable_reference._refuse(
                p["selected_candidate"] is None and p["selected_evidence"] is None,
                "retention selects no Candidate",
            )
        _durable_reference._refuse(
            p["next_champion"] == source, "selection changes next Champion evidence"
        )
        selection = dict(
            p,
            attempt_id=selected.attempt_id if selected else None,
            candidate_digest=selected.candidate_digest if selected else None,
            dependency_digest=selected.dependency_digest
            if selected
            else self.owner._champion_dependency,
            measurement_id=source["measurement_id"],
        )

        def apply() -> None:
            self.owner._put_round(
                replace(self.round(r.round), selection=selection, champion_complete=True)
            )
            self.put_round(r.round, selection=record)

        return apply

    @staticmethod
    def owns_final(entry: Mapping[str, Any]) -> bool:
        return entry.get("role") in {"family15_completion", "family15_final", "family15_champion"}

    def check_stage(self, entry: Mapping[str, Any], p: Mapping[str, Any]) -> None:
        _durable_reference._refuse(not entry["complete"], "completed final evidence is immutable")
        r = self.round(entry["round"])
        _durable_reference._refuse(
            r.closed is None and r.selection is None, "final dispatch after selection"
        )
        if self.policy.round_stop == "all_attempts":
            _need(self.round_adjunct(r).finalists, "final stage waits for finalist barrier")
        if entry["role"] == "family15_completion":
            a = self.owner._attempt(entry["attempt_id"])
            _need(
                self.adjunct(a).final_binding,
                "completion dispatch waits for final schedule binding",
            )
            _durable_reference._refuse(
                p["sample_count"] == entry["missing_count"],
                "completion dispatches exactly missing slots",
            )

    def append_target(self, mid: str) -> tuple[str, str] | None:
        entry = self.owner._measurements.get(mid)
        if entry and self.owns_final(entry) and not entry["complete"]:
            r = self.round(entry["round"])
            if r.closed is None and r.selection is None and mid in self.current.values():
                return ("family15", mid)
        return None

    def rebound(self, old: str, new: str) -> None:
        for original, current in self.current.items():
            if current == old:
                self.current[original] = new
        # Shared rebound preserves role; copy only bounded scheduling counters.
        source = self.owner._measurements[old]
        destination = self.owner._measurements[new]
        if "required" in source:
            destination["required"] = source["required"]
            destination["missing_count"] = source["required"] - destination["imported"]

    def final_members(self, r: RoundState) -> list[Mapping[str, Any]]:
        if r.closed is not None or r.selection is not None:
            return []
        return [
            self.owner._measurements[mid]
            for mid in self.current.values()
            if self.owner._measurements[mid]["round"] == r.round
        ]

    def check_final_barrier(self, r: RoundState) -> None:
        members = self.final_members(r)
        _durable_reference._refuse(bool(members), "final barrier requires owned measurements")
        _durable_reference._refuse(
            all(
                m["complete"]
                or m["stages"]
                or (m["role"] == "family15_completion" and m["missing_count"] == 0)
                for m in members
            ),
            "final barrier waits for all bound work stages",
        )

    def final_listing(self, p: Mapping[str, Any]) -> list[tuple[tuple[str, str, str], int]]:
        from evolution.tree.campaign_schema import OWNERS
        from evolution.tree.execution.journal_schema import ordinal_limit

        r = self.round(_need(self.owner._open_round(), "final gaps require open round"))
        self.check_final_barrier(r)
        evidence = p["evidence"]
        current = {m["measurement_id"]: m for m in self.final_members(r)}
        owners = {row["owner"] for row in evidence["rows"]}
        if not evidence["truncated"]:
            _durable_reference._refuse(
                p["owner"] == next(owner for owner in OWNERS if owner in owners),
                "final gap owner arbitration",
            )
        limit = ordinal_limit(self.policy.sample_retries or 0, self.owner._granting_suspensions())
        result = []
        for row in evidence["rows"]:
            measurement = _need(
                current.get(row["measurement_id"]), "final gap names no current final measurement"
            )
            _durable_reference._refuse(
                not measurement["complete"] and not measurement["frozen"],
                "completed/frozen final has no gaps",
            )
            pair = (str(row["measurement_id"]), str(row["case_id"]), str(row["sample_id"]))
            ordinal = int(row["retry_ordinal"])
            latest = self.owner._latest_try.get(pair)
            _durable_reference._refuse(
                latest is None or ordinal >= latest[0] and (ordinal != latest[0] or not latest[2]),
                "final gap ordinal already reserved",
            )
            _durable_reference._refuse(ordinal <= limit, "final gap exceeds retry ordinal limit")
            result.append((pair, ordinal))
        return result
