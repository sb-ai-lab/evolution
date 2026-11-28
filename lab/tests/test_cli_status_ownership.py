"""Ownership caveat is a human status addition; machine bytes stay identical."""
from evolution_lab import _ownership
from test_service_cli import FakeClient, invoke, _status


def test_status_prints_ownership_line_in_macos_mode(monkeypatch):
    monkeypatch.setattr(_ownership.sys, 'platform', 'linux')
    monkeypatch.delenv(_ownership.SEAM_NAME, raising=False)
    status = _status(None)
    baseline = invoke(['status', 'operation'], FakeClient([status]))
    machine = invoke(['--json', 'status', 'operation'], FakeClient([status]))
    assert baseline[0] == 0 and _ownership.OPERATOR_LINE not in baseline[1]
    monkeypatch.setenv(_ownership.SEAM_NAME, _ownership.GROUP_TREE_MARKER)
    result, output, error = invoke(['status', 'operation'], FakeClient([status]))
    assert result == 0, error
    assert output.count(_ownership.OPERATOR_LINE) == 1
    assert output.index(_ownership.OPERATOR_LINE) > output.index('lab-operation-status/8')
    assert output.replace(_ownership.OPERATOR_LINE + '\n', '', 1) == baseline[1]
    assert invoke(['--json', 'status', 'operation'], FakeClient([status])) == machine
