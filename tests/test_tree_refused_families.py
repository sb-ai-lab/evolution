"""Release `2.0.0b2` refuses campaign families 6-13 in the writers (PDR-0024, PDR-0026, ADR-0030).

`run_campaign` and `resume_campaign` create and resume family 14 only; every
other family is refused with `campaign_family_unsupported` before anything is
written. These tests carry the real admission gates, so they see the real gates instead
of the session-wide test helper that reopens them (spec 2026-10-13 §6).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.tree import evolve as evolve_module
from evolution.tree.campaign_projections import read_family_version
from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION
from evolution.tree.cli import tree_app
from evolution.tree.evolve import admit_manifest
from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
from evolution.tree.reference_target_cli import render_sample_manifest, scaffold
from tests.test_tree_evolve import fake_adapters
from tests.test_tree_family14_public_links import _workspace15

pytestmark = pytest.mark.slow


#: The frozen run directories of `tests/fixtures/retired_families/` (ADR-0030).


async def test_family_14_still_runs_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.family14_public import family14_public_run

    run, result = await family14_public_run(tmp_path, monkeypatch)
    assert result.terminal == "finished"
    assert read_family_version(run) == FINAL_CAMPAIGN_FAMILY_VERSION
    journal = (run / "campaign.jsonl").read_bytes()
    run, reopened = await family14_public_run(tmp_path, monkeypatch)
    assert reopened.terminal == "finished"
    assert (run / "campaign.jsonl").read_bytes() == journal


async def test_an_illegible_journal_with_a_15_policy_keeps_its_persistence_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No readable family in the journal: a /15 policy expects family 14, which
    # the gate admits, so the existing C1 classification answers as before.
    from tests.family14_public import family14_public_run

    run, result = await family14_public_run(tmp_path, monkeypatch)
    assert result.terminal == "finished"
    (run / "campaign.jsonl").write_bytes(b"\x00garbage\n")
    run, again = await family14_public_run(tmp_path, monkeypatch)
    assert again.terminal == "persistence_failed"
    assert again.code == "journal_unavailable"
    assert (run / "campaign.jsonl").read_bytes() == b"\x00garbage\n"


# -- the schema gate and the evolve-side family probe (Task 4) ------------------

#: Spec D4 and plan choice 5: what `manifest_schema` tells the operator to use.
#: Plan choice 5: the probe's guidance for a family-13 run and for families 3-12.


def test_the_schema_gate_admits_15_and_16_with_15_scaffold(tmp_path: Path) -> None:
    assert evolve_module.CAMPAIGN_SCHEMAS == (SCHEMA_V15, "evolution-v2-manifest/16")
    # The scaffold's own document, as `evo-target init` writes it (spec §6).
    result = scaffold(tmp_path / "demo")
    assert result.manifest.read_text(encoding="utf-8") == render_sample_manifest()
    assert admit_manifest(result.manifest).schema == SCHEMA_V15


# -- Task 5: the deprecation machinery is gone ------------------------------------------


@pytest.mark.parametrize("extra", [(), ("--progress",)], ids=["plain", "progress"])
def test_a_15_run_prints_no_deprecation_line_with_and_without_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...]
) -> None:
    manifest = _workspace15(tmp_path / "ws")
    fake = fake_adapters()
    monkeypatch.setattr(
        evolve_module,
        "_http_session_adapter",
        lambda manifest, run_id, ledger=None: fake(manifest, run_id),
    )
    invocation = CliRunner().invoke(
        tree_app,
        ["evolve", "--manifest", str(manifest), "--run-dir", str(tmp_path / "run"), *extra],
    )
    assert invocation.exit_code == 0, invocation.output
    assert not [line for line in invocation.stderr.splitlines() if "deprecated" in line]


# -- Task 7: every reader, and the 13 to 14 link, under the real gates ------------------
#
# PDR-0024's review trigger is "a reader of a retired family regresses": a failure
# here is a reader regression to report, never an expectation to edit.
