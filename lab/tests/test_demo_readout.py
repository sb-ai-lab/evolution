"""Example readout caveat is human-only and precedes its closing note."""
import importlib.util
import json
from pathlib import Path
import sys

from evolution_lab import _ownership


def test_demo_readout_prints_ownership_line(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[1] / 'examples/owner/demo_readout.py'
    spec = importlib.util.spec_from_file_location('lab_demo_readout_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = {'schema': module._CAMPAIGN, 'check': 'succeeded',
        'campaign': {'outcome': 'completed', 'rounds': ['promoted', 'clean'], 'final_champion': 'v1'},
        'diff': {'state': 'changed'}, 'export': {'state': 'succeeded'},
        'final_target': {'state': 'stopped'}, 'blocker_count': 0}
    summary = tmp_path / 'summary.json'
    summary.write_text(json.dumps(value))
    before = summary.read_bytes()
    async def rows(*args):
        return [('measurement', 'case', 'question', 'answer', 1)]
    monkeypatch.setattr(module, '_read', rows)
    monkeypatch.setattr(sys, 'argv', [str(path), str(tmp_path)])
    monkeypatch.setattr(_ownership.sys, 'platform', 'linux')
    monkeypatch.delenv(_ownership.SEAM_NAME, raising=False)
    module.main()
    baseline = capsys.readouterr().out
    assert _ownership.OPERATOR_LINE not in baseline
    monkeypatch.setenv(_ownership.SEAM_NAME, _ownership.GROUP_TREE_MARKER)
    module.main()
    output = capsys.readouterr().out
    assert output.count(_ownership.OPERATOR_LINE) == 1
    assert output.index(_ownership.OPERATOR_LINE) < output.index(module._NOTE[module._CAMPAIGN].strip())
    assert summary.read_bytes() == before
