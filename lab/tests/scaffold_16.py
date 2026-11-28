"""Core's /15 scaffold as a neutral /16 document, built the way Core's own tests build one.

Mirrors ``tests/family15_fakes.py::v16_document`` of the pinned Core (``campaign.sample_retries``, the indeterminate
``max_rate`` strings and a complete ``campaign.gate``); confidence-mode values follow Core's
``tests/test_tree_campaign_family15_delivery.py``. No ``campaign.promotion`` key: b8 refuses it.
``as_manifest_16`` is the conversion alone; ``tests/fixtures/panel/capture.py --only family15`` applies it to the
demo's working-copy manifest.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def as_manifest_16(document: dict[str, Any], *, mode: str = "confidence", repeats: int = 1) -> dict[str, Any]:
    """``document`` (a /15 manifest) converted in place to /16 and returned."""
    document["schema"] = "evolution-v2-manifest/16"
    campaign = document["campaign"]
    campaign["evaluation"] = {"repeats_per_question": repeats}
    campaign["sample_retries"] = 1
    for threshold in campaign["indeterminate"].values():
        threshold["max_rate"] = "0.500000000000"
    gate = {
        "mode": mode, "stability": "stable", "provenance": {"kind": "reported"}, "fresh_parent": False,
        "global": "point", "focus": {"margin": 0}, "causal": {"tolerance": 0},
        "execution_noise": {"kind": "negligible"}, "verification": "reuse",
    }
    if mode == "confidence":
        gate["focus"] = {"margin": -0.9, "confidence": 0.1}
        gate["causal"] = {"tolerance": 1, "confidence": 0.1}
        gate["statistics"] = {"method": "bounded-weighted-mean/1", "unit": "case"}
    campaign["gate"] = gate
    return document


def scaffold_16(path: Path, *, mode: str = "confidence", repeats: int = 1):
    from evolution.tree.reference_target_cli import scaffold

    sample = scaffold(path)
    document = as_manifest_16(json.loads(sample.manifest.read_text(encoding="utf-8")), mode=mode, repeats=repeats)
    sample.manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return sample
