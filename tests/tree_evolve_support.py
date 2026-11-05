"""Shared workspace fixtures for tree campaign tests."""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15

SCORER_SCRIPT = r"""
import json
import os
import pathlib
import sys

payload = json.load(sys.stdin)
view = json.dumps(payload["view"], sort_keys=True)
root = os.environ.get("EVO_CANDIDATE_ROOT")
skill = ""
if root:
    skill_path = pathlib.Path(root) / "skills" / "SKILL.md"
    if skill_path.is_file():
        skill = skill_path.read_text(encoding="utf-8")
edited = "edited" in view or "edited" in skill
json.dump(
    {
        "metrics": {"score": 1.0 if edited else 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": bool(edited)}],
    },
    sys.stdout,
)
"""

REJECTING_SCORER_SCRIPT = r"""
import json
import sys

json.load(sys.stdin)
json.dump(
    {
        "metrics": {"score": 0.0},
        "critical_pass": True,
        "verdicts": [{"criterion": "answered", "passed": False}],
    },
    sys.stdout,
)
"""

EDITOR_SCRIPT = r"""
import json
import os
import pathlib
import sys

request = json.loads(sys.stdin.read())
skill = pathlib.Path(request["workspace"]) / "skills" / "SKILL.md"
skill.write_text(skill.read_text(encoding="utf-8").rstrip("\n") + " edited\n", encoding="utf-8")
print(json.dumps({"status": "edited", "note": os.environ.get("EVO_TREE_TOKEN", "no token")}))
"""

REFLECTOR_SCRIPT = r"""
print('{"status":"declined","groups":[],"note":""}')
"""

_DIAGNOSIS = {
    "status": "reflected",
    "note": "n",
    "groups": [
        {
            "root_cause": "r",
            "suggested_fix": "f",
            "evidence_ids": ["obs:c1:1"],
            "affected_cases": ["c1"],
            "target_paths": ["skills/SKILL.md"],
            "counterevidence": "c",
            "regression_risks": "r",
            "why_this_layer": "w",
        }
    ],
}
REFLECTING_REFLECTOR_SCRIPT = "print(" + repr(json.dumps(_DIAGNOSIS)) + ")\n"

CAMPAIGN_PAYLOAD: dict[str, Any] = {
    "rounds": 1,
    "attempts_per_round": 1,
    "parallel_attempts": 1,
    "alpha": {"budget": "0.05", "spend": {"3": "0.01"}},
    "indeterminate": {
        "target": {"max_count": 2, "max_rate": "0.10"},
        "environment": {"max_count": 2, "max_rate": "0.10"},
        "scorer": {"max_count": 2, "max_rate": "0.10"},
        "rollout": {"max_count": 4, "max_rate": "0.25"},
    },
    "budget": {"max_turns": 4, "timeout_seconds": 120},
    "max_concurrency": 2,
    "max_scorer_concurrency": 2,
    "metric": {"primary": "score"},
    "random_seed": 12345,
    "editor_timeout_seconds": 900,
    "editor_context": None,
    "reflection_timeout_seconds": 30,
    "gate": {"regression_margin": "0"},
    "attempt_feedback": "sequential",
}

WRITABLE = ("skills/**",)
POLICY = ArtifactPolicyV1((), (), WRITABLE)

AFFECTED_GATE = {
    "regression_margin": "0",
    "provenance": {"kind": "reported"},
    "fresh_parent": True,
    "global": False,
}


def write_workspace(
    root: Path,
    *,
    port: int = 8765,
    scorer_script: str = SCORER_SCRIPT,
    editor_script: str = EDITOR_SCRIPT,
    scorer_env: tuple[str, ...] = (),
    editor_env: tuple[str, ...] = (),
    campaign: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
    name: str = "manifest.json",
    editor: dict[str, Any] | None = None,
    reflector: dict[str, Any] | None = None,
    environment_docs: Mapping[str, str] | None = None,
    linked_from: str | Mapping[str, str] | None = None,
    seed_root: str | None = None,
    schema: str = SCHEMA_V15,
) -> Path:
    """A complete `/15` workspace: seed, three programs, corpus and manifest.

    A string `linked_from` becomes the object form with `baseline: fresh`.
    `seed_root` replaces the workspace seed, which is still written but unused.
    """

    if schema != SCHEMA_V15:
        raise ValueError("workspace fixtures support /15 only")
    root.mkdir(parents=True, exist_ok=True)
    seed = root / "seed" / "skills"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "SKILL.md").write_text("seed skill\n", encoding="utf-8")
    (root / "scorer").mkdir(exist_ok=True)
    (root / "scorer" / "score.py").write_text(scorer_script, encoding="utf-8")
    (root / "editor").mkdir(exist_ok=True)
    (root / "editor" / "edit.py").write_text(editor_script, encoding="utf-8")
    (root / "reflector").mkdir(exist_ok=True)
    (root / "reflector" / "reflect.py").write_text(REFLECTING_REFLECTOR_SCRIPT, encoding="utf-8")
    if environment_docs is not None:
        docs = root / "env-docs"
        docs.mkdir(exist_ok=True)
        for relative, text in environment_docs.items():
            (docs / relative).write_text(text, encoding="utf-8")
    corpus = (
        rows
        if rows is not None
        else [
            {
                "case_id": "c1",
                "group_id": "g1",
                "stratum": "s1",
                "public": {"text": "hello"},
                "oracle": {"critical": True},
            }
        ]
    )
    (root / "corpus.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in corpus), encoding="utf-8"
    )
    document: dict[str, Any] = {
        "schema": "evolution-v2-manifest/6"
        if isinstance(linked_from, Mapping)
        else "evolution-v2-manifest/5",
        "seed_root": "seed" if seed_root is None else seed_root,
        "target": {"base_url": f"http://127.0.0.1:{port}", "seed_mode": "exact"},
        "artifacts": {
            "excludes": [],
            "dependency_paths": [],
            "writable_paths": list(WRITABLE),
        },
        "campaign": copy.deepcopy(campaign if campaign is not None else CAMPAIGN_PAYLOAD),
        "environment": {"kind": "static", "input_text_key": "text"},
        "scorer": {
            "argv": [sys.executable, "score.py"],
            "cwd": "scorer",
            "timeout_seconds": 60,
            "dependency_paths": [],
            "allowed_env": list(scorer_env),
        },
        "editor": editor
        if editor is not None
        else {
            "kind": "subprocess",
            "argv": [sys.executable, "edit.py"],
            "cwd": "editor",
            "dependency_paths": [],
            "allowed_env": list(editor_env),
        },
        "reflector": {
            **(
                reflector
                if reflector is not None
                else {
                    "kind": "subprocess",
                    "argv": [sys.executable, "reflect.py"],
                    "cwd": "reflector",
                    "dependency_paths": [],
                    "allowed_env": [],
                }
            ),
            "environment_docs": None if environment_docs is None else "env-docs",
            "probe": None,
            "grouping": {
                "kind": "fields",
                "features": [
                    {
                        "name": "failed_criteria",
                        "source": "observation",
                        "path": "/failed_criteria",
                        "description": "Failed criteria.",
                    },
                    {
                        "name": "terminal",
                        "source": "observation",
                        "path": "/terminal",
                        "description": "Terminal observation status.",
                    },
                ],
            },
            "selection": {
                "focus_cases": 16,
                "representatives_per_case": 2,
                "context_page_size": 32,
            },
            "families": {"kind": "none"},
        },
        "corpus": {"path": "corpus.jsonl"},
    }
    if linked_from is not None:
        document["linked_from"] = (
            dict(linked_from) if isinstance(linked_from, Mapping) else linked_from
        )
    document = lift_document(document, schema)
    path = root / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def lift_document(document: dict[str, Any], schema: str) -> dict[str, Any]:
    """Lift raw workspace blueprint fields into the sole `/15` fixture schema."""

    from tests.tree.manifest.test_manifest_v2 import RETRY

    if schema != SCHEMA_V15:
        raise ValueError(f"unsupported workspace schema: {schema}")
    lifted = copy.deepcopy(document)
    campaign = lifted["campaign"]
    campaign["budget"] = {"max_failed_turns": 1, **campaign["budget"]}
    campaign["call_retries"] = {
        "max_attempts": 1,
        "turn_max_attempts": 1,
        **campaign.get("call_retries", {}),
    }
    campaign["gate"] = {**copy.deepcopy(AFFECTED_GATE), **campaign.get("gate", {})}
    lifted["reflector"].setdefault("max_consecutive_failures", 2)
    if isinstance(lifted.get("linked_from"), str):
        lifted["linked_from"] = {"run_dir": lifted["linked_from"], "baseline": "fresh"}
    for role in ("editor", "reflector"):
        if lifted[role].get("kind") == "chat":
            lifted[role].setdefault("retry", copy.deepcopy(RETRY))
            lifted[role].setdefault("context_compact_chars", 800_000)
            lifted[role].setdefault("context_message_max_chars", 64_000)
    from tests.campaign_fakes import PROBE_NEUTRAL_CONTRACT
    from tests.tree.manifest.test_manifest_v15 import v15_from

    return v15_from(
        lifted,
        round_stop=campaign.get("round_stop", "first_accepted"),
        refresh=campaign.get("baseline_refresh_after_rounds", 3),
        metric_contract=dict(PROBE_NEUTRAL_CONTRACT),
    )
