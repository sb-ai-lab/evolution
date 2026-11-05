"""The opt-in family preserves /15 and historical reader contracts."""

import inspect
import json

import pytest
from typer.testing import CliRunner

from evolution.tree import ScorerTraceReader, read_scorer_trace
from evolution.tree.campaign import inspect_campaign
from evolution.tree.cli import tree_app
from evolution.tree.evolve import EvolveRefused, evolve, exit_code_for
from tests.test_tree_campaign_family15_delivery import public_run
from tests.test_tree_evolve import fake_adapters
from tests.test_tree_family14_public_links import _link, _workspace15

pytestmark = pytest.mark.slow


def test_help_explains_opt_in_controls_and_keeps_scaffold_family():
    result = CliRunner().invoke(tree_app, ["evolve", "--help"])
    assert result.exit_code == 0
    assert "Manifest /15 settings" in result.output
    assert "Manifest /16 settings" in result.output
    for control in (
        "campaign.evaluation.repeats_per_question",
        "campaign.gate.verification",
        "execution_noise",
    ):
        assert control in result.output
    assert "promotion" not in result.output
    v16_help = result.output.split("Manifest /16 settings:", 1)[1]
    for control in ("statistical", "measured_noise", "bound_fallback"):
        assert control in v16_help


async def test_public_v15_keeps_identity_exit_and_forbids_backward_link(tmp_path, monkeypatch):
    path = _workspace15(tmp_path / "old")
    result = await evolve(path, tmp_path / "old/run", adapter_factory=fake_adapters())
    assert result.terminal == "finished" and exit_code_for(result) == 0
    status = inspect_campaign(tmp_path / "old/run", verify=True).to_payload()
    assert "independent" not in status
    assert status["verification"]["mismatches"] == []
    created = json.loads((tmp_path / "old/run/campaign.jsonl").read_text().splitlines()[0])
    assert created["payload"]["family_version"] == 14
    assert "promotion" not in created["payload"]["identity"]["policy"]
    source, _ = await public_run(tmp_path / "new", monkeypatch)
    path = _workspace15(tmp_path / "backward")
    document = json.loads(path.read_text())
    document.update(_link(source, "fresh"))
    path.write_text(json.dumps(document))
    with pytest.raises(EvolveRefused, match="lineage_family_unsupported"):
        await evolve(path, tmp_path / "backward/run", adapter_factory=fake_adapters())
    assert not (tmp_path / "backward/run").exists()


async def test_public_v16_links_finished_v15_without_changing_source(tmp_path, monkeypatch):
    from tests.campaign_drivers import _champion_copy, _records
    from tests.test_tree_family14_public_links import _fingerprint

    path = _workspace15(tmp_path / "source")
    source = tmp_path / "source/run"
    result = await evolve(path, source, adapter_factory=fake_adapters())
    assert result.terminal == "finished"
    before = _fingerprint(source)
    run, _ = await public_run(
        tmp_path / "successor", monkeypatch, linked_from=(source, _champion_copy(source))
    )
    created = _records(run / "campaign.jsonl")[0]["payload"]
    assert created["family_version"] == 15
    assert created["identity"]["linked_from"]["predecessor_family"] == 14
    assert _fingerprint(source) == before
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


def test_existing_trace_reader_calls_keep_defaults_and_absence(tmp_path):
    assert inspect.signature(read_scorer_trace).parameters["max_section_bytes"].default is None
    assert inspect.signature(read_scorer_trace).parameters["run_boundary"].default == "resolved"
    assert inspect.signature(ScorerTraceReader).parameters["run_boundary"].default == "resolved"
    assert inspect.signature(ScorerTraceReader.read).parameters["max_section_bytes"].default is None
    assert read_scorer_trace(tmp_path, "absent") is None
    with ScorerTraceReader(tmp_path) as reader:
        assert reader.read("absent") is None
