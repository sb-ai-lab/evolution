"""The pinned core's /15 scaffold with Lab's observe and retry defaults (PDR-0015/0031).

Anything but the expected scaffold is refused: a changed scaffold must break loudly, not drift.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCAFFOLD_SCHEMA = "evolution-v2-manifest/15"

#: The 2.0.0b4 scaffold's keys (reference_target_cli.py:91-190); a key outside these is a changed scaffold.
_TOP_15 = ("schema", "seed_root", "target", "artifacts", "role_visibility", "campaign", "environment", "scorer",
           "editor", "grouper", "reflector", "corpus")
_CAMPAIGN_15 = ("rounds", "attempts_per_round", "parallel_attempts", "evaluation", "round_stop",
                "baseline_refresh_after_rounds", "indeterminate", "budget", "call_retries", "max_concurrency",
                "max_scorer_concurrency", "metric", "random_seed", "editor_timeout_seconds", "editor_context",
                "reflection_timeout_seconds", "gate", "attempt_feedback")
_GATE_15 = ("mode", "stability", "focus", "causal", "provenance", "fresh_parent", "global")
_SCORER_15 = ("argv", "cwd", "timeout_seconds", "dependency_paths", "allowed_env", "metric_contract")
_EDITOR_15 = ("kind", "argv", "cwd", "dependency_paths", "allowed_env")
_REFLECTOR_15 = ("kind", "argv", "cwd", "dependency_paths", "allowed_env", "environment_docs", "probe",
                 "max_consecutive_failures", "selection", "families")
_GROUPER_15 = ("kind", "features")
_ROLE_VISIBILITY_15 = {"version": "role_visibility/1", "readable_paths": []}


def _keys(where: str, value: Any, expected: tuple[str, ...]) -> dict:
    if not isinstance(value, dict) or tuple(value) != expected:
        got = list(value) if isinstance(value, dict) else type(value).__name__
        raise ValueError(f"core scaffold manifest {SCAFFOLD_SCHEMA} has unexpected {where} keys: {got}; "
                         f"expected {list(expected)}")
    return value


_FOCUS_15 = ("confidence", "margin")
_CAUSAL_15 = ("confidence", "tolerance")
LAB_SCHEMA_15 = "evolution-v2-manifest/15"


def _checked_15(document: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy of core's 2.0.0b4 /15 scaffold after checking its keys and order on every level Lab rewrites."""
    if document.get("schema") != SCAFFOLD_SCHEMA:
        raise ValueError(f"expected a core scaffold manifest {SCAFFOLD_SCHEMA}, got {document.get('schema')!r}")
    source = copy.deepcopy(dict(document))
    _keys("top-level", source, _TOP_15)
    campaign = _keys("campaign", source["campaign"], _CAMPAIGN_15)
    gate = _keys("campaign.gate", campaign["gate"], _GATE_15)
    _keys("campaign.gate.focus", gate["focus"], _FOCUS_15)
    _keys("campaign.gate.causal", gate["causal"], _CAUSAL_15)
    _keys("campaign.evaluation", campaign["evaluation"], ("repeats_per_question",))
    _keys("scorer", source["scorer"], _SCORER_15)
    _keys("editor", source["editor"], _EDITOR_15)
    _keys("reflector", source["reflector"], _REFLECTOR_15)
    grouper = _keys("grouper", source["grouper"], _GROUPER_15)
    if grouper["kind"] != "fields":
        raise ValueError(f"core scaffold manifest {SCAFFOLD_SCHEMA} has grouper.kind {grouper['kind']!r}; "
                         f"expected 'fields'")
    if source["role_visibility"] != _ROLE_VISIBILITY_15:
        raise ValueError(f"core scaffold manifest {SCAFFOLD_SCHEMA} has an unexpected role_visibility "
                         f"{source['role_visibility']!r}; expected {_ROLE_VISIBILITY_15!r}")
    return source


def as_manifest_15(document: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the observe smoke profile and retry table (spec dev66 §4a) to the checked core scaffold.
    ``stability``, ``provenance``, ``round_stop``,
    ``baseline_refresh_after_rounds``, ``grouper``, ``scorer.metric_contract`` and ``role_visibility`` stay as core
    wrote them. ``observe`` refuses ``focus.confidence``, ``causal.confidence`` and a ``global`` other than ``off``;
    ``margin`` is required in both modes."""
    out = _checked_15(document)
    return observe_profile(out)


def observe_profile(document: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the offline demo's observe gate to a /15 project, preserving its inputs."""
    if document.get("schema") != SCAFFOLD_SCHEMA:
        raise ValueError(f"expected {SCAFFOLD_SCHEMA}")
    out = copy.deepcopy(dict(document))
    campaign = out["campaign"]
    gate = campaign["gate"]
    campaign["evaluation"] = {"repeats_per_question": 1}
    campaign["gate"] = {"mode": "observe", "stability": gate["stability"], "focus": {"margin": 0},
                        "causal": {"tolerance": "measured_noise"}, "provenance": gate["provenance"],
                        "fresh_parent": True, "global": "off"}
    _retry_defaults(out)
    return out


def _retry_defaults(out: dict[str, Any]) -> None:
    campaign = out["campaign"]
    campaign["sample_retries"] = 3
    campaign["call_retries"] = {"max_attempts": 3, "turn_max_attempts": 2}
    campaign["budget"]["max_failed_turns"] = campaign["budget"]["max_turns"]
    for owner in campaign["indeterminate"].values():
        owner.update(max_count=20, max_rate="1")
    out["reflector"]["max_consecutive_failures"] = 5


def campaign_manifest(document: Mapping[str, Any], *, project: Path) -> dict[str, Any]:
    """Keep Core's statistical scaffold and use the project's evaluation inputs and retry profile."""
    out = _checked_15(document)
    evaluation = json.loads((project / "evaluation-input.json").read_text(encoding="utf-8"))
    for name in ("seed_root", "target", "corpus", "environment"):
        out[name] = copy.deepcopy(evaluation[name])
    out["artifacts"] = {key: copy.deepcopy(value) for key, value in evaluation["policy"].items()
                        if key != "version"}
    for name in ("argv", "cwd", "timeout_seconds", "dependency_paths", "allowed_env"):
        out["scorer"][name] = copy.deepcopy(evaluation["scorer"][name])
    for role in ("scorer", "editor", "reflector"):
        out[role]["argv"][0] = "lab:auto"
    _retry_defaults(out)
    return out

