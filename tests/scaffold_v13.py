"""The `/13` scaffold sample, frozen for tests whose subject is `/13` or `/14` (H12c choice 7).

`evo-target init` wrote this `evolution-v2-manifest/13` document up to
`0.3.0a35`; from `2.0.0b1` it writes `/15` (PDR-0004 amendment, R72). The
dictionary is copied verbatim from `reference_target_cli.SAMPLE_MANIFEST` as
it stood before that change -- not derived from the `/15` sample, because the
product never had a converter from `/15` back to `/13`.
"""

from __future__ import annotations

from typing import Any

SAMPLE_MANIFEST_V13: dict[str, Any] = {
    "schema": "evolution-v2-manifest/13",
    "seed_root": "seed",
    "target": {"base_url": "http://127.0.0.1:8765", "seed_mode": "exact"},
    "artifacts": {
        "excludes": ["build/**"],
        "dependency_paths": ["uv.lock"],
        "writable_paths": ["skills/**"],
    },
    "campaign": {
        "rounds": 2,
        "attempts_per_round": 1,
        "parallel_attempts": 1,
        "alpha": {"budget": "0.05", "spend": {"3": "0.01"}},
        "indeterminate": {
            owner: {"max_count": 2, "max_rate": "0.5"}
            for owner in ("target", "environment", "scorer", "rollout")
        },
        "budget": {"max_turns": 2, "timeout_seconds": 30, "max_failed_turns": 1},
        "call_retries": {"max_attempts": 4, "turn_max_attempts": 1},
        "max_concurrency": 2,
        "max_scorer_concurrency": 2,
        "metric": {"primary": "score"},
        "random_seed": 7,
        "editor_timeout_seconds": 60,
        "editor_context": None,
        "reflection_timeout_seconds": 60,
        "gate": {
            "regression_margin": "0",
            "provenance": {"kind": "reported"},
            "fresh_parent": True,
            "global": False,
        },
        "attempt_feedback": "sequential",
    },
    "environment": {"kind": "static", "input_text_key": "text"},
    "scorer": {
        "argv": ["python3", "score.py"],
        "cwd": "scorer",
        "timeout_seconds": 30,
        "dependency_paths": [],
        "allowed_env": [],
    },
    "editor": {
        "kind": "subprocess",
        "argv": ["python3", "edit.py"],
        "cwd": "editor",
        "dependency_paths": [],
        "allowed_env": [],
    },
    "reflector": {
        "kind": "subprocess",
        "argv": ["python3", "reflect.py"],
        "cwd": "reflector",
        "dependency_paths": [],
        "allowed_env": [],
        "environment_docs": None,
        "probe": None,
        "max_consecutive_failures": 2,
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
        "selection": {"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        "families": {"kind": "none"},
    },
    "corpus": {"path": "corpus.jsonl"},
}
