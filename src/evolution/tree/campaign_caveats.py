"""Caveats: under which condition a result was obtained (PDR-0012, D2a §4).

The lineage kinds are derived from the creation record's `identity.linked_from`
alone; `condition_changed_in_place` (I2 spec §4.4) from the campaign's own
applied in-place changes (`CampaignSnapshot.condition_changes`), and in a
successor's lineage from its predecessor's (§6.3), folded at creation;
`grouping_fallback` (family 9, model error grouping spec §10) from the
campaign's own durable grouping results, and never folded into a successor's
lineage. Family 12's `focus_selection_bias`, `completion_regression`,
`provenance_drift` and `components_changed_by_condition` (G2 spec §4.6, §4.7,
§4.9), family 13's method caveats (design §6.5, §10) and family 14's
`exact_contrast`, `noise_not_observed`, `champion_not_contemporaneous` and
`stored_pairs_excluded` (F0 R27, in their own registries) come from
`affected_report.py`, also local to their run. Nothing here is a journal record of its own, and nothing here changes
a statistic, a gate decision, an outcome or an exit code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evolution.tree.campaign_schema import CampaignSnapshot

FOCUS_BIAS_KIND = "focus_selection_bias"
COMPLETION_REGRESSION_KIND = "completion_regression"
PROVENANCE_DRIFT_KIND = "provenance_drift"
COMPONENTS_CHANGED_KIND = "components_changed_by_condition"
#: Family 13 (design §6.5, F8 plan choice 5): the method caveats a
#: `comparison_look_evaluated` carries, in §6.5's order. Spelled here, not
#: imported: `campaign_schema` imports this module through
#: `campaign_identity`. A test pins the set equal to
#: `campaign_schema.LOOK_CAVEATS`.
LOOK_CAVEAT_KINDS = (
    "intervals_empirical",
    "noise_allowance_empirical",
    "causal_parent_not_contemporaneous",
    "observe_gate_not_enforced",
    "uncalibrated_point_estimates",
    "observe_calibration_degenerate",
    "noise_allowance_unavailable",
)
#: Family 14's separate look vocabulary, pinned to `LOOK_CAVEATS14`.
#: These kinds do not widen legacy public report or lineage validators.
LOOK_CAVEAT_KINDS14 = (
    "intervals_empirical",
    "noise_allowance_empirical",
    "causal_parent_not_contemporaneous",
    "observe_gate_not_enforced",
    "uncalibrated_point_estimates",
    "noise_allowance_unavailable",
    "exact_contrast",
    "noise_not_observed",
)
#: Family 13 `observe` (PDR-0022): the two method kinds a
#: campaign that ran `observe` also reports once for the whole campaign, as
#: `{kind, round: null, attempt_id: null}`, whether or not any Attempt was
#: evaluated. They lead the affected items, in `LOOK_CAVEAT_KINDS` order, and
#: an Attempt keeps its own items of the same kinds.
OBSERVE_CAMPAIGN_KINDS = ("observe_gate_not_enforced", "uncalibrated_point_estimates")
#: Family 12 (G2 spec §4.6, §4.7, §4.9): report-only, local to the run, one
#: per Attempt (bias, component drift across an accepted change) or per
#: promoting round (the two completion checks), in §4.9's order. Family 13
#: adds one item per method caveat kind an Attempt's evaluated looks carried,
#: after the four (design §10); it never produces `completion_regression`
#: (D8), and its `provenance_drift` reads each finalist's cohort rows.
AFFECTED_CAVEAT_KINDS = (
    FOCUS_BIAS_KIND,
    COMPLETION_REGRESSION_KIND,
    PROVENANCE_DRIFT_KIND,
    COMPONENTS_CHANGED_KIND,
    *LOOK_CAVEAT_KINDS,
)
CAVEAT_KINDS = (
    "condition_changed",
    "predecessor_unfinished",
    "baseline_inherited",
    "condition_changed_in_place",
    "grouping_fallback",
    *AFFECTED_CAVEAT_KINDS,
)
#: Family 14 (F0 R27, PDR-0028 / ADR-0032 amendments 2026-10-09): an exact
#: look or global contrast says so (one item per Attempt), every `stable`
#: result says its Champion side was not measured with the finalists (a
#: campaign-level item, as the observe items), and a stored contrast that
#: removed pairs says how many (one item per finalist).
EXACT_CONTRAST_KIND = "exact_contrast"
CHAMPION_NOT_CONTEMPORANEOUS_KIND = "champion_not_contemporaneous"
STORED_PAIRS_EXCLUDED_KIND = "stored_pairs_excluded"
#: H12b plan choice 13: family 14's campaign-level items, in their leading order.
FINAL_CAMPAIGN_KINDS = (*OBSERVE_CAMPAIGN_KINDS, CHAMPION_NOT_CONTEMPORANEOUS_KIND)
#: H12b plan choice 12: family 14's own registries, separate tuples so that
#: family 13's sort keys and validators stay unchanged (F0 R27's purpose).
#: Family 14 never produces `completion_regression` (D8) and its looks never
#: carry `observe_calibration_degenerate` (R25).
AFFECTED_CAVEAT_KINDS14 = (
    FOCUS_BIAS_KIND,
    PROVENANCE_DRIFT_KIND,
    COMPONENTS_CHANGED_KIND,
    *LOOK_CAVEAT_KINDS14,
    CHAMPION_NOT_CONTEMPORANEOUS_KIND,
    STORED_PAIRS_EXCLUDED_KIND,
)
CAVEAT_KINDS14 = (
    "condition_changed",
    "predecessor_unfinished",
    "baseline_inherited",
    "condition_changed_in_place",
    "grouping_fallback",
    *AFFECTED_CAVEAT_KINDS14,
)
#: The kinds a document may carry more than once: one per applied change.
IN_PLACE_KIND = "condition_changed_in_place"
#: ... and one per round whose grouping fell back (family 9).
GROUPING_FALLBACK_KIND = "grouping_fallback"
#: The kinds a successor's `linked_from.lineage_caveats` may aggregate: a
#: grouping fallback is local to its run (spec §10).
LINEAGE_CAVEAT_KINDS = tuple(
    kind
    for kind in CAVEAT_KINDS
    if kind != GROUPING_FALLBACK_KIND and kind not in AFFECTED_CAVEAT_KINDS
)


@dataclass(frozen=True)
class LineageCaveat:
    kind: str
    runs: int
    fields: tuple[str, ...]
    nearest_run: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "runs": self.runs,
            "fields": list(self.fields),
            "nearest_run": self.nearest_run,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LineageCaveat:
        return cls(
            str(payload["kind"]),
            int(payload["runs"]),
            tuple(payload["fields"]),
            str(payload["nearest_run"]),
        )


def own_caveats(
    linked: Mapping[str, Any] | None, *, baseline_abandoned: bool = False
) -> list[dict[str, Any]]:
    if linked is None:
        return []
    caveats: list[dict[str, Any]] = []
    if linked.get("condition_changed"):
        caveats.append(
            {
                "kind": "condition_changed",
                "from_run": linked["run_id"],
                "fields": list(linked["condition_changed"]),
            }
        )
    if linked.get("predecessor_finished") is False:
        caveats.append(
            {
                "kind": "predecessor_unfinished",
                "from_run": linked["run_id"],
                "authorization_id": linked["authorization"]["id"],
            }
        )
    baseline = linked.get("baseline")
    if baseline and baseline.get("imported", 0) > 0 and not baseline_abandoned:
        caveats.append(
            {
                "kind": "baseline_inherited",
                "from_run": linked["run_id"],
                "source_measurement_id": baseline["source_measurement_id"],
                "imported": baseline["imported"],
                "scheduled": baseline["scheduled"],
                "fields": list(baseline["fields"]),
                "origin_runs": baseline["origin_runs"],
            }
        )
    return caveats


def in_place_caveats(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One `condition_changed_in_place` per applied in-place change (I2 spec §4.4).

    `changes` are `CampaignSnapshot.condition_changes`, in journal order; the
    caveat names the change, its effect, the comparable fields it moved and
    the round open at it (`None` between rounds).
    """

    return [
        {
            "kind": IN_PLACE_KIND,
            "seq": change["seq"],
            "effect": change["effect"],
            "fields": list(change["fields"]),
            "round": change["round"],
        }
        for change in changes
    ]


def grouping_caveat_entries(snapshot: CampaignSnapshot) -> list[tuple[int, dict[str, Any]]]:
    """`grouping_caveats` with each item's journal sequence, for merging by `seq`."""

    entries = []
    for number, state in snapshot.rounds.items():
        grouping = state.grouping
        if grouping is None or grouping.get("status") != "finished":
            continue
        fallback = grouping.get("fallback")
        if fallback is None:
            continue
        entries.append(
            (
                int(grouping["seq"]),
                {
                    "kind": GROUPING_FALLBACK_KIND,
                    "round": number,
                    "reason": fallback["reason"],
                    "fallback_kind": fallback["kind"],
                    "scope": fallback["scope"],
                },
            )
        )
    return sorted(entries, key=lambda entry: entry[0])


def grouping_caveats(snapshot: CampaignSnapshot) -> list[dict[str, Any]]:
    """One `grouping_fallback` per round whose grouping result fell back (spec §10).

    Derived from the durable `grouping_finished` (family 9), ordered by its
    journal sequence: `reason` is the closed code, `fallback_kind` `fields` or
    `singletons`, `scope` `uncovered` or `all`. It reports degraded search
    organization only; it is never a lineage caveat.
    """

    return [item for _, item in grouping_caveat_entries(snapshot)]


def aggregate_lineage(
    predecessor_linked: Mapping[str, Any] | None,
    predecessor_run_id: str,
    *,
    predecessor_baseline_abandoned: bool = False,
    predecessor_changes: Sequence[Mapping[str, Any]] = (),
    in_place: bool = True,
) -> tuple[LineageCaveat, ...]:
    """The bounded per-kind aggregate a successor journals (D2a §4, I2 §6.3).

    The predecessor's own caveats are folded into the aggregate it inherited:
    one item per kind, `runs` counting the links that carried the kind,
    `fields` their union and `nearest_run` the nearest run carrying it.
    `predecessor_changes` are the predecessor's `condition_changes`: all of
    them fold into one `condition_changed_in_place` item for that run, so a
    predecessor that changed its condition twice counts one run, the union of
    both changes' fields, and the size still never grows with the chain; which
    change moved which field stays in the predecessor's own report.
    `in_place=False` (a family-6 successor) drops the kind altogether, the
    predecessor's changes and any inherited item alike.
    """

    inherited = {
        item["kind"]: LineageCaveat.from_payload(item)
        for item in (predecessor_linked or {}).get("lineage_caveats", ())
    }
    own = {
        item["kind"]: item
        for item in own_caveats(
            predecessor_linked, baseline_abandoned=predecessor_baseline_abandoned
        )
    }
    if not in_place:
        inherited.pop(IN_PLACE_KIND, None)
    elif predecessor_changes:
        own[IN_PLACE_KIND] = {
            "kind": IN_PLACE_KIND,
            "from_run": predecessor_run_id,
            "fields": sorted({str(f) for change in predecessor_changes for f in change["fields"]}),
        }
    out: list[LineageCaveat] = []
    for kind in LINEAGE_CAVEAT_KINDS:
        before, mine = inherited.get(kind), own.get(kind)
        if before is None and mine is None:
            continue
        fields = set(before.fields if before else ()) | set(mine.get("fields", ()) if mine else ())
        out.append(
            LineageCaveat(
                kind=kind,
                runs=(before.runs if before else 0) + (1 if mine else 0),
                fields=tuple(sorted(fields)),
                nearest_run=predecessor_run_id if mine else before.nearest_run,  # type: ignore[union-attr]
            )
        )
    return tuple(out)
