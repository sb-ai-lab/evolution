"""Core's scaffold as Lab's /15 with the observe smoke profile (spec 2026-10-13 §4)."""
from __future__ import annotations

import json
from pathlib import Path

from evolution.tree.reference_target_cli import scaffold

from evolution_lab._scaffold_manifest import as_manifest_15


def scaffold_15(path: Path):
    sample = scaffold(path)
    document = json.loads(sample.manifest.read_text(encoding="utf-8"))
    sample.manifest.write_text(json.dumps(as_manifest_15(document), indent=2) + "\n", encoding="utf-8")
    return sample


def apply_legacy_failure_profile(document: dict) -> None:
    """Opt in to pre-dev66 thresholds for controlled suspension/digest fixtures.

    Values come from fixtures/scaffold-b1-manifest-15.json. Keep every other
    field and its order intact; the production scaffold retains retry defaults.
    """
    campaign = document["campaign"]
    campaign.pop("sample_retries", None)
    campaign["call_retries"] = {"max_attempts": 4, "turn_max_attempts": 1}
    campaign["budget"]["max_failed_turns"] = 1
    for owner in campaign["indeterminate"].values():
        owner.update(max_count=2, max_rate="0.5")
    document["reflector"]["max_consecutive_failures"] = 2


def scaffold_15_with_legacy_failures(path: Path):
    """The /15 observe scaffold with explicit legacy failure thresholds."""
    sample = scaffold_15(path)
    document = json.loads(sample.manifest.read_text(encoding="utf-8"))
    apply_legacy_failure_profile(document)
    sample.manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return sample
