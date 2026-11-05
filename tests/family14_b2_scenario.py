"""The I1 scenario of the 2.0.0b3 role-feedback plan (ADR-0037, spec §3.3, §8).

One family-14 run a `2.0.0b2` writer produces from offline fakes:

- round 1: `r01-a01` writes outside `skills/**`, so admission refuses it
  (`editor_failed {reason: actor_violation}`); `r01-a02` edits the skill, is a
  finalist and loses its global comparison (`critical_failure`); the round
  closes with no promotion;
- round 2: the Reflector and the Editor read round 1 as history; its
  finalists lose again and the campaign finishes.

`tests/fixtures/family14_b2/generate.py` runs it and commits the run
directory, its corpus and `expected.json`; `tests/test_tree_family14_b2_pins.py`
checks them.

Two kinds of pin (plan Task 1). `JOURNAL_VERBATIM` values are read verbatim
from the committed journal: they only detect an edit of the fixture.
`JOURNAL_REDERIVED` values, `rederive_round_two_history`, the audit, the
identity round trip and the live re-run recompute with the current code:
they are the I1 evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.journal_identity import canonical_json

#: Read verbatim from the committed journal: fixture integrity only.
JOURNAL_VERBATIM = (
    "editor_failed_reasons",
    "frozen_feedback_rows",
    "common_input_digests",
    "editor_brief_digests",
    "reflection_input_digests",
    "identity_keys",
    "identity_digest",
)
#: Recomputed from the committed journal by this release's reducer.
JOURNAL_REDERIVED = ("effective_feedback",)
#: Never compared live. The command bindings seal the interpreter, so the
#: identity digest differs per host; `effective_feedback` carries journal
#: sequence numbers, which a release that adds a record (#210) may shift, so
#: the live half compares its reasons (`effective_reasons`) instead.
LIVE_EXCLUDED = frozenset({"identity_digest", "effective_feedback"})
#: The role-input values I1 is about. If any of them is not reproducible
#: between two runs on one machine, the generator refuses (STOP condition).
REQUIRED_REPRODUCIBLE = (
    "editor_brief_digests",
    "common_input_digests",
    "base_brief_digests",
    "editor_history_digests",
    "reflector_history_digests",
    "frozen_feedback_rows",
    "effective_reasons",
)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def i1_editor_script(counter: Path) -> str:
    """Call 1 writes `notes.txt` outside `skills/**`; later calls edit the skill."""

    from tests.tree_evolve_support import EDITOR_SCRIPT

    return EDITOR_SCRIPT.replace(
        'skill = pathlib.Path(request["workspace"])',
        f"counter = pathlib.Path({str(counter)!r})\n"
        "calls = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(calls))\n"
        "if calls == 1:\n"
        '    (pathlib.Path(request["workspace"]) / "notes.txt").write_text(\n'
        '        "outside\\n", encoding="utf-8"\n'
        "    )\n"
        '    print(json.dumps({"status": "edited", "note": "outside"}))\n'
        "    sys.exit(0)\n"
        'skill = pathlib.Path(request["workspace"])',
        1,
    )


def _normalize_modes(root: Path) -> None:
    """Seed modes independent of the host umask, so artifact digests are portable."""

    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def i1_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install the scenario's seams once and return `family14_run`'s keywords."""

    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family14_first_accepted_feedback import feedback_options

    editor = i1_editor_script(tmp_path / "editor.calls")
    workspace = fixtures.write_workspace

    def write_workspace(*args: Any, **kwargs: Any) -> Path:
        path = workspace(*args, **{**kwargs, "editor_script": editor})
        _normalize_modes(Path(args[0]) / "seed")
        return path

    monkeypatch.setattr(fixtures, "write_workspace", write_workspace)
    options = feedback_options(
        tmp_path, monkeypatch, stability="unstable", reason="critical_failure", k=2
    )
    options["campaign"]["rounds"] = 2
    return options


def capture_role_inputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Record, as the running code builds them, each round's base Editor brief
    digest and history bytes and each reflection's history and payload bytes."""

    from evolution.tree import campaign as campaign_module
    from evolution.tree.roles.editor_contracts import brief_digest, history_payload
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload

    captured: dict[str, list[str]] = {
        "base_brief_digests": [],
        "editor_history_digests": [],
        "reflector_history_digests": [],
        "reflector_payload_digests": [],
    }
    real_build = campaign_module.build_adaptive_reflect_brief

    def build(**kwargs: Any) -> Any:
        brief = real_build(**kwargs)
        captured["reflector_history_digests"].append(
            _sha(history_payload(kwargs["history"], excerpts=True))
        )
        captured["reflector_payload_digests"].append(_sha(reflect_brief_payload(brief)))
        return brief

    monkeypatch.setattr(campaign_module, "build_adaptive_reflect_brief", build)
    real_brief_for = campaign_module._Campaign.brief_for

    def brief_for(self: Any, attempt: Any) -> Any:
        brief = real_brief_for(self, attempt)
        digest = brief_digest(brief)
        if digest not in captured["base_brief_digests"]:
            captured["base_brief_digests"].append(digest)
            captured["editor_history_digests"].append(_sha(history_payload(brief.history)))
        return brief

    monkeypatch.setattr(campaign_module._Campaign, "brief_for", brief_for)
    return captured


def journal_records(run: Path) -> list[dict[str, Any]]:
    text = (run / "campaign.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def collect_pins(run: Path) -> dict[str, Any]:
    """Every journal-level I1 value of one family-14 run directory."""

    from evolution.tree.campaign_schema import effective_attempt_feedback
    from tests.campaign_fakes import Family14Life

    records = journal_records(run)

    def payloads(event: str) -> list[dict[str, Any]]:
        return [record["payload"] for record in records if record["event"] == event]

    snapshot = Family14Life(records).snapshot
    effective: dict[str, list[Any]] = {}
    for state in snapshot.rounds.values():
        for attempt in state.attempts.values():
            if attempt.outcome is None:
                continue
            reason, comparison = effective_attempt_feedback(state, attempt, family_version=14)
            effective[attempt.attempt_id] = [
                reason,
                None if comparison is None else int(comparison["seq"]),
            ]
    identity = records[0]["payload"]["identity"]
    return {
        "editor_failed_reasons": {
            p["attempt_id"]: p.get("reason") for p in payloads("editor_failed")
        },
        "effective_feedback": effective,
        "effective_reasons": {key: value[0] for key, value in effective.items()},
        "frozen_feedback_rows": {
            p["attempt_id"]: [_sha(row) for row in p["feedback"]["attempts"]]
            for p in payloads("attempt_input_frozen")
        },
        "common_input_digests": {
            p["attempt_id"]: p["common_input_digest"] for p in payloads("attempt_input_frozen")
        },
        "editor_brief_digests": {
            p["attempt_id"]: p["brief_digest"] for p in payloads("editor_started")
        },
        "reflection_input_digests": {
            p["reflection_id"]: p["input_digest"] for p in payloads("reflection_started")
        },
        "identity_keys": sorted(identity),
        "identity_digest": _sha(identity),
    }


class _PublishedCopies:
    """`PublishedRefs` over a run directory's published copies, read-only."""

    def __init__(self, run: Path, artifact_policy: Any) -> None:
        self._run = run
        self._policy = artifact_policy

    def published_ref(self, kind: str, artifact_id: str) -> Any:
        from evolution.tree.artifacts.artifact_policy import scan_tree
        from evolution.tree.artifacts.artifacts import ArtifactRef

        root = (
            self._run
            / "artifacts"
            / {"champion": "champions", "candidate": "candidates"}[kind]
            / artifact_id
        )
        scan = scan_tree(root, self._policy)
        return ArtifactRef(
            kind,  # type: ignore[arg-type]
            artifact_id,
            root,
            scan.candidate_digest,
            scan.dependency_digest,
            self._policy,
        )


def rederive_round_two_history(
    run: Path, corpus_path: Path, readable_paths: Sequence[str]
) -> dict[str, str]:
    """Round 2's closed-round history, recomputed by the current `project_history`
    from committed bytes only: the journal prefix before round 2's first
    `reflection_started`, the measurement journals (`FrozenMeasurement`), the
    published copies (digests checked against the journal by
    `_verify_published_copies`) and the corpus file. Returns the SHA-256 of the
    Editor form (`excerpts=False`) and the Reflector form (`excerpts=True`)."""

    from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
    from evolution.tree.campaign_measurement import FrozenMeasurement
    from evolution.tree.campaign_policy import policy_class_for_payload
    from evolution.tree.execution.corpus_partition import load_partition
    from evolution.tree.execution.journal_identity import corpus_digest
    from evolution.tree.roles.editor.editor_evidence import project_history
    from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2, history_payload
    from evolution.tree.roles.reflector_contracts import REFLECT_LIMITS_V1
    from evolution.tree.roles.role_visibility import RoleVisibilityView
    from tests.campaign_fakes import Family14Life

    records = journal_records(run)
    cut = next(
        index
        for index, record in enumerate(records)
        if record["event"] == "reflection_started" and record["payload"]["round"] == 2
    )
    snapshot = Family14Life(records[:cut]).snapshot
    identity = records[0]["payload"]["identity"]
    policy = policy_class_for_payload(identity["policy"]).from_payload(identity["policy"])
    artifact_policy = ArtifactStoreIdentityV1.from_payload(identity["artifact_store"]).policy
    corpus = load_partition(corpus_path).select("train").cases
    assert corpus_digest(corpus) == identity["corpus_digest"], (
        "corpus.jsonl is not the bound corpus"
    )
    measurements = {
        path.name: FrozenMeasurement(run, path.name)
        for path in sorted((run / "measurements").iterdir())
        if path.is_dir()
    }
    visible = RoleVisibilityView(
        readable_paths=tuple(readable_paths), writable_paths=artifact_policy.writable_paths
    ).visible
    common = dict(
        measurements=measurements,
        store=_PublishedCopies(run, artifact_policy),
        artifact_policy=artifact_policy,
        corpus=corpus,
        policy=policy,
        visible=visible,
    )
    editor = project_history(snapshot, **common)
    reflector = project_history(
        snapshot, **common, limits={**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1}, excerpts=True
    )
    assert [r.round for r in editor.rounds] == [1], "round 2 must read round 1 only"
    assert all(
        a.changes.status != "unavailable" or a.candidate_digest is None
        for a in reflector.rounds[0].attempts
    )
    return {
        "editor_history": _sha(history_payload(editor)),
        "reflector_history": _sha(history_payload(reflector, excerpts=True)),
    }


def assert_i1_shape(run: Path) -> None:
    """The run has every prefix kind I1 needs; the generator refuses otherwise."""

    records = journal_records(run)

    def payloads(event: str) -> list[dict[str, Any]]:
        return [record["payload"] for record in records if record["event"] == event]

    failed = {p["attempt_id"]: p for p in payloads("editor_failed")}
    assert failed["r01-a01"]["reason"] == "actor_violation", failed
    closes = {p["attempt_id"]: (p["outcome"], p["reason"]) for p in payloads("attempt_closed")}
    assert closes["r01-a01"] == ("abandoned", "editor_failed"), closes
    assert closes["r01-a02"][0] == "finalist", closes
    comparisons = {p["attempt_id"]: p for p in payloads("global_comparison_completed")}
    assert comparisons["r01-a02"]["accepted"] is False, comparisons
    assert 1 in {p["round"] for p in payloads("round_closed")}
    assert 2 in {p["round"] for p in payloads("reflection_started")}
    frozen = {p["attempt_id"]: p for p in payloads("attempt_input_frozen")}
    assert [row["reason"] for row in frozen["r01-a02"]["feedback"]["attempts"]] == ["editor_failed"]
    assert "r02-a01" in frozen
