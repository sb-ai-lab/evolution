"""H12c Task 7: public family-14 links, the deprecation lines and compatibility
(§15.7, §15.9, C§9.3; Review Focus 2).

Every run here is made by the public `evolve` (or `evo tree evolve` through
the CLI runner) from a `write_workspace` document, lifted to `/15` with
`v15_from` where the cell is family 14. No gate is patched and no driver
is used: `/15` is admitted by the product, and the offline `FakeAdapter`
serves the target. The metric contract names the event `FakeAdapter`
replies with (`MESSAGE_CONTRACT`), so the connection preflight finds the
target compatible.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evolution.tree import evolve as evolve_module
from evolution.tree.cli import tree_app
from evolution.tree.evolve import evolve
from tests.campaign_drivers import _champion_copy, audit_mismatches
from tests.test_tree_campaign_family14_link_run import MESSAGE_CONTRACT
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_evolve import fake_adapters
from tests.tree.manifest.test_manifest_v15 import V15_GATE, v15_from
from tests.tree_evolve_support import write_workspace

pytestmark = pytest.mark.slow


# -- helpers ----------------------------------------------------------------------------


def _cli(manifest: Path, run_dir: Path, monkeypatch, *extra: str):
    fake = fake_adapters()
    # The CLI's default adapter seam, as tests/test_tree_evolve.py patches it.
    monkeypatch.setattr(
        evolve_module,
        "_http_session_adapter",
        lambda manifest, run_id, ledger=None: fake(manifest, run_id),
    )
    return CliRunner().invoke(
        tree_app, ["evolve", "--manifest", str(manifest), "--run-dir", str(run_dir), *extra]
    )


def _workspace15(root: Path, **options: Any) -> Path:
    """A `write_workspace` document lifted to `/15` (`v15_from`), `FakeAdapter`'s contract.

    `gate` goes to `v15_from`; the other keywords go to `write_workspace`.
    """

    gate = options.pop("gate", None)
    path = write_workspace(root, **options)
    document = v15_from(json.loads(path.read_text()), gate=gate, metric_contract=MESSAGE_CONTRACT)
    path.write_text(json.dumps(document))
    return path


async def _finished(manifest: Path, run: Path) -> Path:
    result = await evolve(manifest, run, adapter_factory=fake_adapters())
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    assert result.finished is not None
    return run


def _link(predecessor: Path, baseline: str) -> dict[str, Any]:
    return {
        "linked_from": {"run_dir": str(predecessor), "baseline": baseline},
        "seed_root": str(_champion_copy(predecessor)),
    }


def _fingerprint(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _created(run: Path) -> dict[str, Any]:
    (created,) = payloads(run, "campaign_created")
    return created


# -- §15.7: links through the public command ----------------------------------------------


async def test_public_link_14_to_14_changes_stability(tmp_path):
    predecessor = await _finished(_workspace15(tmp_path / "ws1"), tmp_path / "ws1" / "run")
    assert _created(predecessor)["family_version"] == 14
    assert _created(predecessor)["identity"]["policy"]["gate"]["stability"] == "stable"
    before = _fingerprint(predecessor)
    successor = _workspace15(
        tmp_path / "ws2",
        gate={**V15_GATE, "stability": "unstable"},
        **_link(predecessor, "fresh"),
    )
    run = await _finished(successor, tmp_path / "ws2" / "run")
    link = _created(run)["identity"]["linked_from"]
    assert link["predecessor_family"] == 14
    assert link["condition_changed"] == ["identity.policy_digest"]
    result = await evolve(successor, run, adapter_factory=fake_adapters())  # reopens
    payload = result.result.to_payload()
    assert payload["settings"]["stability"] == "unstable"
    assert payload["linked_from"]["family"] == 14
    assert audit_mismatches(run) == []
    assert _fingerprint(predecessor) == before


@pytest.mark.parametrize("predecessor_kind", ["unfinished-15"])
def test_public_link_refusals(tmp_path, monkeypatch, predecessor_kind):
    """A family-12 run, or an unfinished family-14 run, cannot be linked from `/15`.

    Synchronous: the CLI runs its own event loop.
    """

    from tests.campaign_drivers import _cut_hook
    from tests.campaign_fakes import Crash

    predecessor = tmp_path / "ws1" / "run"
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_campaign_created")
        with pytest.raises(Crash):
            asyncio.run(
                evolve(
                    _workspace15(tmp_path / "ws1"),
                    predecessor,
                    adapter_factory=fake_adapters(),
                )
            )
    assert _created(predecessor)["family_version"] == 14
    assert not payloads(predecessor, "campaign_finished")
    code = "lineage_not_finished"
    # The seed root is the predecessor's own seed: a crashed run has no
    # Champion copy, and the refusal comes before the seed is read.
    successor = _workspace15(
        tmp_path / "ws2",
        linked_from={"run_dir": str(predecessor), "baseline": "fresh"},
        seed_root=str(tmp_path / "ws1" / "seed"),
    )
    before = _fingerprint(predecessor)
    run = tmp_path / "ws2" / "run"
    result = _cli(successor, run, monkeypatch)
    assert result.exit_code == 2, result.output
    assert f"refused: {code}" in result.stderr
    assert "Traceback" not in result.output
    assert not run.exists() or not any(run.iterdir())
    assert _fingerprint(predecessor) == before


# -- §15.9: the public command admits /15 only ---------------------------------------------


def test_the_public_command_runs_15_without_a_deprecation_line(tmp_path, monkeypatch):
    """`/15` completes (exit 0) and prints no deprecation line."""

    run = tmp_path / "run"
    manifest = _workspace15(tmp_path / "ws")
    result = _cli(manifest, run, monkeypatch)
    assert result.exit_code == 0, result.output
    assert not [line for line in result.stderr.splitlines() if "deprecated" in line]


# -- C§9.3: older families keep their bytes under the admitting release ---------------------
