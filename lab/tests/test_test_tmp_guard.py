"""Lab's independent guard refuses disk before collection and worker startup."""
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux mount admission")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('initial', [None, 'relaxed', 'strict'])
def test_process_durability_is_restored_between_tests(pytester, monkeypatch, initial):
    if initial is None:
        monkeypatch.delenv('EVOLUTION_DURABILITY', raising=False)
    else:
        monkeypatch.setenv('EVOLUTION_DURABILITY', initial)
    pytester.makeconftest((ROOT / 'tests/conftest.py').read_text())
    pytester.makepyfile(platforms=(ROOT / 'tests/platforms.py').read_text())
    pytester.makepyfile(f'''
import os
def test_1_service_sets_process_value():
    os.environ['EVOLUTION_DURABILITY'] = 'strict'
def test_2_next_test_keeps_launch_value():
    assert os.environ.get('EVOLUTION_DURABILITY') == {initial!r}
def test_3_process_value_is_removed():
    os.environ.pop('EVOLUTION_DURABILITY', None)
def test_4_next_test_keeps_launch_value():
    assert os.environ.get('EVOLUTION_DURABILITY') == {initial!r}
''')
    result = pytester.runpytest_subprocess('-q', '-p', 'no:cacheprovider', timeout=20)
    result.assert_outcomes(passed=4)


@pytest.mark.parametrize("workers", [False, True])
def test_guard_stops_disk_session(pytester, monkeypatch, workers):
    pytester.makeconftest((ROOT / "tests/conftest.py").read_text())
    # Simulate a disk mount even when the checkout and all test scratch are on tmpfs.
    pytester.makepyfile(platforms="def linux_mounts():\n    return ['none / ext4 rw 0 0']")
    pytester.makepyfile('def test_never_collected():\n    raise AssertionError("test ran")')
    monkeypatch.setenv("TMPDIR", str(pytester.path))
    monkeypatch.delenv("EVOLUTION_TEST_RAMDISK", raising=False)
    monkeypatch.delenv("EVOLUTION_TEST_ALLOW_DISK", raising=False)
    args = ["-q", "-p", "no:cacheprovider", "--basetemp", str(pytester.path / "base")]
    if workers:
        args += ["-n", "2"]
    result = pytester.runpytest_subprocess(*args, timeout=20)
    output = result.stdout.str() + result.stderr.str()
    assert result.ret == 2
    assert "scripts/test-tmp" in output
    assert "EVOLUTION_TEST_ALLOW_DISK" in output
    assert "test ran" not in output
    assert "bringing up nodes" not in output
