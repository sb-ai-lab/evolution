"""The examples Lab ships write manifest /15 through as_manifest_15 (spec 2026-10-13 §4)."""
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.mark.parametrize("relative", ["owner/campaign.py", "client/offline.py", "campaign/offline.py"])
def test_example_writes_15(relative):
    text = (EXAMPLES / relative).read_text(encoding="utf-8")
    assert "as_manifest_15(" in text or "campaign_project(" in text
    assert "evolution-v2-manifest/11" not in text and '"alpha"' not in text
