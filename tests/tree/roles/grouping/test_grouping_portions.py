"""The pure portion planner of the family-15 chat Grouper (spec §2, §5)."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from evolution.tree.execution.input_secrets import REDACTED, redact_run_secrets
from evolution.tree.roles.grouping import grouping_portions as gp
from evolution.tree.roles.grouping.model_grouping import InputTooLarge, build_case_profiles
from evolution.tree.roles.grouping.reflect_grouping import (
    GroupingRecord,
    _digest,
    build_registry,
)

RECORD_BUDGET = 64 * 1024 - gp.CHECKPOINT_ENVELOPE_BYTES


def rec(
    case,
    repeat,
    *,
    failing=True,
    available=True,
    terminal="completed",
    critical=False,
    criteria=("c1",),
    answer="the answer",
    feedback=None,
    evidence_id=None,
):
    observation = {
        "terminal": terminal,
        "critical": critical,
        "failed_criteria": list(criteria) if failing else [],
        "primary_score": 0.0 if failing else 1.0,
        "verdicts": [],
        "answer_text": [answer],
    }
    return GroupingRecord(
        evidence_id or f"{case}#{repeat}",
        case,
        repeat,
        failing,
        available,
        {
            "case": {},
            "public": {},
            "observation": observation,
            "feedback": {} if feedback is None else feedback,
        },
    )


def registry(rows):
    return build_registry("b" * 64, rows)


class Built:
    """Everything a caller builds before planning, as C6 will."""

    def __init__(
        self,
        reg,
        *,
        glimpse_chars=800,
        max_set_members=64,
        capacity=16,
        input_budget=60_000,
        record_budget=RECORD_BUDGET,
        representatives_per_case=1,
        provenance=None,
        redact=None,
        identity="i" * 64,
    ):
        self.registry = reg
        self.profiles = build_case_profiles(
            reg, metric_primary="score", representatives_per_case=representatives_per_case
        )
        self.table = gp.reference_table(reg)
        self.glimpses = {
            r.evidence_id: gp.glimpse(r, chars=glimpse_chars) for r in reg.records if r.available
        }
        self.headers = {}
        for profile in self.profiles:
            counts, unknown = gp.provenance_counts(profile, provenance or {})
            self.headers[profile.case_id] = gp.case_header(
                profile, provenance=counts, unknown=unknown, chars=glimpse_chars, redact=redact
            )
        by_id = {r.evidence_id: r for r in reg.records}
        self.representatives = {
            eid: gp.representative_payload(by_id[eid])
            for profile in self.profiles
            for eid in profile.representatives
        }
        self.contrast = {
            p.case_id: self.glimpses[p.contrast] for p in self.profiles if p.contrast is not None
        }
        self.units = gp.build_units(
            reg,
            self.table,
            self.glimpses,
            max_set_members=max_set_members,
            representatives=frozenset(self.representatives),
        )
        self.table = self.table.with_sets(self.units)
        self.input_budget = input_budget
        self.record_budget = record_budget
        self.plan = gp.plan_portions(
            self.units,
            capacity=capacity,
            input_budget=input_budget,
            record_budget=record_budget,
            render=self.render,
            registry_digest=reg.digest,
            identity_digest=identity,
        )

    def render(self, portion):
        return gp.render_portion(
            portion, self.table, self.headers, self.glimpses, self.representatives, self.contrast
        )


def members_of(plan):
    return [eid for p in plan.portions for u in p.units for eid in u.evidence_ids]


def expanded_checkpoint(portion, variant="\u0001" * 64):
    """The largest checkpoint body a portion's members can make."""
    group = {
        "members": [
            {"evidence_id": eid, "variant": variant}
            for unit in portion.units
            for eid in unit.evidence_ids
        ],
        "affected_cases": [unit.case_id for unit in portion.units for _ in unit.evidence_ids],
    }
    return json.dumps(group, ensure_ascii=False, separators=(",", ":")).encode()


def assert_within_limits(built):
    for portion in built.plan.portions:
        assert gp.portion_input_size(built.render(portion)) <= built.input_budget
        assert len(expanded_checkpoint(portion)) <= built.record_budget


def failing_ids(reg):
    return [r.evidence_id for r in reg.records if r.failing and r.available]


def test_plan_deterministic_and_concurrency_free():
    rows = [
        rec(f"case-{c}", n, criteria=(f"k{n % 3}",), answer=f"ans {n % 2}")
        for c in range(4)
        for n in range(1, 30)
    ] + [rec("case-0", 30, failing=False)]
    shuffled = list(reversed(rows))

    def plan_of(records):
        return Built(registry(records), capacity=3, input_budget=12_000).plan

    first = plan_of(rows)
    with ThreadPoolExecutor(max_workers=4) as pool:
        others = list(pool.map(plan_of, [rows, shuffled, rows, shuffled]))
    for other in others:
        assert other == first
        assert other.digest == first.digest
    assert sorted(members_of(first)) == sorted(failing_ids(registry(rows)))
    assert len(members_of(first)) == len(set(members_of(first)))
    # The digest binds the identity and the limits.
    reg = registry(rows)
    assert Built(reg, capacity=3, input_budget=12_000, identity="j" * 64).plan.digest != (
        first.digest
    )
    assert Built(reg, capacity=4, input_budget=12_000).plan.digest != first.digest


def test_set_needs_equal_signature_and_glimpse():
    reg = registry(
        [
            rec("a", 1),
            rec("a", 2),
            rec("a", 3, answer="a different answer"),
            rec("a", 4, critical=True),
            rec("a", 5),
        ]
    )
    built = Built(reg, representatives_per_case=1)
    # a#1 is the representative and stays alone; a#2 and a#5 share signature and glimpse.
    assert built.glimpses["a#3"] != built.glimpses["a#2"]
    assert built.glimpses["a#4"] == built.glimpses["a#2"]  # critical is not in the glimpse
    shapes = [(u.ref, u.evidence_ids) for u in built.units]
    assert shapes == [
        ("o1", ("a#1",)),
        ("s1", ("a#2", "a#5")),
        ("o3", ("a#3",)),
        ("o4", ("a#4",)),
    ]
    assert built.units[0].representative
    assert built.table.sets == ((1, 4),)
    assert built.table.to_log()["sets"] == {"s1": ["o2", "o5"]}


def test_20000_identical_observations_form_bounded_sets():
    reg = registry([rec("case", n) for n in range(1, 20_001)])
    started = time.monotonic()
    built = Built(reg, max_set_members=64, representatives_per_case=1)
    elapsed = time.monotonic() - started
    sets = [u for u in built.units if len(u.evidence_ids) > 1]
    assert sets and all(2 <= len(u.evidence_ids) <= 64 for u in sets)
    assert sorted(members_of(built.plan)) == sorted(failing_ids(reg))
    assert len(members_of(built.plan)) == 20_000
    assert_within_limits(built)
    assert elapsed < 10


def test_huge_max_set_members_still_cut_to_limits():
    cases = [("x" * 199) + str(c) for c in range(2)]
    reg = registry([rec(case, n) for case in cases for n in range(1, 10_001)])
    started = time.monotonic()
    built = Built(reg, max_set_members=20_000, input_budget=50_000, representatives_per_case=1)
    elapsed = time.monotonic() - started
    assert max(len(u.evidence_ids) for u in built.units) == 9_999
    assert len(built.plan.portions) > 2
    assert_within_limits(built)
    assert sorted(members_of(built.plan)) == sorted(failing_ids(reg))
    assert len(members_of(built.plan)) == len(set(members_of(built.plan)))
    # Cut pieces are named after the set they came from.
    refs = [u.ref for p in built.plan.portions for u in p.units]
    assert "s1.1" in refs and "s1.2" in refs
    assert elapsed < 10


def test_20000_distinct_criteria_keep_header_bounded():
    reg = registry([rec("case", n, criteria=(f"criterion-{n:05d}",)) for n in range(1, 20_001)])
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    header = gp.case_header(profile, provenance=None, unknown=0, chars=800)
    assert len(json.dumps(header, ensure_ascii=False, separators=(",", ":"))) <= 800
    listed = header["failed_criterion_counts"]
    *named, rest = listed
    assert named and all(set(entry) == {"name", "count"} for entry in named)
    assert set(rest) == {"more", "count"}
    assert len(named) + rest["more"] == 20_000
    assert sum(entry["count"] for entry in named) + rest["count"] == 20_000
    assert named[0]["name"] == "criterion-00001"  # equal counts: by name
    assert header["metric"] == {
        "name": "score",
        "count": 20_000,
        "min": 0.0,
        "median": 0.0,
        "max": 0.0,
    }
    assert "provenance" not in header


def test_histogram_orders_by_count_then_name():
    reg = registry([rec("a", 1, criteria=("z", "y")), rec("a", 2, criteria=("y",)), rec("a", 3)])
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    header = gp.case_header(profile, provenance=None, unknown=0, chars=800)
    assert header["failed_criterion_counts"] == [
        {"name": "y", "count": 2},
        {"name": "c1", "count": 1},
        {"name": "z", "count": 1},
    ]
    assert header["terminal_counts"] == [{"name": "completed", "count": 3}]


def test_case_spans_portions_with_header_each():
    reg = registry(
        [rec("big", n, answer=f"answer {n}") for n in range(1, 41)]
        + [rec("small", 1), rec("small", 2, failing=False)]
    )
    built = Built(reg, capacity=4)
    with_big = [p for p in built.plan.portions if "big" in p.case_ids]
    assert len(with_big) == 10
    headers = []
    for portion in with_big:
        body = json.loads(built.render(portion))
        (case,) = [c for c in body["cases"] if c["case"] == built.table.c_ref(0)]
        headers.append(case["header"])
    assert all(h == headers[0] for h in headers)
    assert headers[0]["failing"] == 40
    last = built.plan.portions[-1]
    assert last.case_ids == ("small",)
    body = json.loads(built.render(last))
    assert body["cases"][0]["contrast"] == built.glimpses["small#2"]


def test_split_halves_and_lone_set():
    reg = registry([rec("a", n, answer=f"x{n}") for n in range(1, 5)])
    built = Built(reg, capacity=16)
    (portion,) = built.plan.portions
    first, second = gp.split_portion(portion)
    assert (first.id, second.id) == ("p1.1", "p1.2")
    assert first.units + second.units == portion.units
    assert len(first.units) == 2 and len(second.units) == 2
    assert gp.split_portion(portion) == (first, second)

    reg = registry([rec("a", n) for n in range(1, 7)])
    built = Built(reg, representatives_per_case=1)
    lone = gp.MapPortion("p2", (built.units[1],), ("a",))
    assert built.units[1].ref == "s1" and len(built.units[1].evidence_ids) == 5
    left, right = gp.split_portion(lone)
    assert (left.id, right.id) == ("p2.1", "p2.2")
    assert (left.units[0].ref, left.units[0].evidence_ids) == ("s1.1", ("a#2", "a#3", "a#4"))
    assert (right.units[0].ref, right.units[0].evidence_ids) == ("s1.2", ("a#5", "a#6"))
    # A three-member set halves into a set and a single observation.
    left2, right2 = gp.split_portion(left)
    assert (left2.units[0].ref, right2.units[0].ref) == ("s1.1.1", "o4")
    assert right2.units[0].observations == (3,)
    single = gp.MapPortion("p9", (right2.units[0],), ("a",))
    with pytest.raises(ValueError, match="portion_unsplittable"):
        gp.split_portion(single)


def test_portion_references_cover_units_and_set_members():
    reg = registry([rec("a", n) for n in range(1, 5)])
    built = Built(reg, representatives_per_case=1)
    (portion,) = built.plan.portions
    assert portion.references() == {"o1": (0,), "s1": (1, 2, 3), "o2": (1,), "o3": (2,), "o4": (3,)}


def test_provenance_counts_and_unknown():
    reg = registry([rec("a", n) for n in range(1, 6)] + [rec("a", 6, failing=False)])
    used = {
        "a#1": ("tools/search.py", "prompt.md"),
        "a#2": ("prompt.md",),
        "a#3": (),
        "a#4": None,
        "a#6": ("ignored-passing.md",),
    }
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    counts, unknown = gp.provenance_counts(profile, used)
    assert counts == {"prompt.md": 2, "tools/search.py": 1}
    assert unknown == 2  # a#4 is null, a#5 is absent
    header = gp.case_header(profile, provenance=counts, unknown=unknown, chars=800)
    assert header["provenance"] == {
        "components": [{"name": "prompt.md", "count": 2}, {"name": "tools/search.py", "count": 1}],
        "unknown": 2,
    }


def test_provenance_is_bounded_like_histograms():
    reg = registry([rec("a", n) for n in range(1, 3)])
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    counts = {f"component/{n:05d}.py": 1 for n in range(20_000)}
    header = gp.case_header(profile, provenance=counts, unknown=0, chars=800)
    assert len(json.dumps(header, ensure_ascii=False, separators=(",", ":"))) <= 800
    *named, rest = header["provenance"]["components"]
    assert len(named) + rest["more"] == 20_000


def test_no_provenance_no_line():
    reg = registry([rec("a", n) for n in range(1, 4)])
    (profile,) = build_case_profiles(reg, metric_primary="score", representatives_per_case=1)
    assert gp.provenance_counts(profile, {}) == (None, 3)
    assert gp.provenance_counts(profile, {"a#1": None}) == (None, 3)
    built = Built(reg)
    assert "provenance" not in built.headers["a"]
    assert all("provenance" not in built.render(p) for p in built.plan.portions)


def test_provenance_id_equal_to_run_secret_is_redacted():
    secret = "sk-live-0123456789abcdef"
    redact = partial(redact_run_secrets, secrets=[secret])
    reg = registry([rec("a", n) for n in range(1, 4)])
    built = Built(
        reg,
        provenance={"a#1": (secret, "ok.py"), "a#2": (f"dir/{secret}",), "a#3": ("ok.py",)},
        redact=redact,
    )
    header = built.headers["a"]
    assert header["provenance"]["components"] == [
        {"name": "ok.py", "count": 2},
        {"name": REDACTED, "count": 1},
        {"name": "dir/" + REDACTED, "count": 1},
    ]
    for portion in built.plan.portions:
        assert secret not in built.render(portion)


def test_render_has_no_evidence_ids():
    rows = [
        rec(
            "CASEID-alpha",
            n,
            evidence_id=f"EVID-{n:04d}-ffff",
            criteria=("c1",) if n < 4 else ("c2",),
            answer="plain answer" if n % 2 else "other answer",
        )
        for n in range(1, 9)
    ] + [rec("CASEID-alpha", 9, failing=False, evidence_id="EVID-pass")]
    reg = registry(rows)
    built = Built(reg, representatives_per_case=2)
    texts = [built.render(p) for p in built.plan.portions]
    profile = built.profiles[0]
    signatures = {row.signature for row in profile.rows}
    for text in texts:
        assert "EVID-" not in text
        assert "CASEID-" not in text
        assert not any(sig in text for sig in signatures)
        assert '"evidence_id"' not in text
        assert '"signature"' not in text
    body = json.loads(texts[0])
    units = body["cases"][0]["units"]
    assert any("representative" in u for u in units)
    assert any("members" in u for u in units)
    assert _digest(texts[0]) == _digest(built.render(built.plan.portions[0]))


def test_input_too_large_on_one_glimpse():
    reg = registry([rec("a", 1, answer="z" * 5_000), rec("a", 2, answer="y" * 5_000)])
    with pytest.raises(InputTooLarge):
        Built(reg, glimpse_chars=4_000, input_budget=3_000, representatives_per_case=1)


def test_representative_falls_back_to_glimpse_when_too_large():
    reg = registry([rec("a", 1, answer="z" * 20_000), rec("a", 2, answer="q")])
    built = Built(reg, input_budget=4_000, representatives_per_case=1)
    (first,) = [u for p in built.plan.portions for u in p.units if u.evidence_ids == ("a#1",)]
    assert not first.representative
    assert_within_limits(built)


def test_glimpse_parts_and_cut_marker():
    record = rec(
        "a",
        1,
        criteria=("format", "accuracy"),
        feedback={"note": "missing total"},
        answer="HEAD" + "m" * 5_000 + "TAIL",
    )
    text = gp.glimpse(record, chars=300)
    assert len(text) <= 300
    full = gp.glimpse(record, chars=100_000)
    assert text.endswith(f"…[cut; full length {len(full)}]")
    assert text.index("format") < text.index("missing total") < text.index("HEAD")
    assert "TAIL" in text
    assert gp.glimpse(record, chars=300) == text
    short = rec("a", 2, answer="short")
    assert "[cut;" not in gp.glimpse(short, chars=800)
