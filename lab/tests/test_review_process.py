import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import pytest
import psutil
from ownership_modes import ownership_mode  # noqa: F401

async def test_forced_stop_reaps_detached_grandchild_and_records_result(tmp_path, ownership_mode):
    from evolution_lab.runner import _stop
    import evolution_lab._supervisor as supervisor
    pidfile = tmp_path / 'pid'
    script = tmp_path / 'core.py'
    script.write_text('import subprocess,sys,time,signal\n'
                      'signal.signal(signal.SIGTERM, lambda *a: None)\n'
                      'subprocess.Popen([sys.executable,"-c",'
                      f'"import os,time;open({str(pidfile)!r},chr(119)).write(str(os.getpid()));time.sleep(60)"],start_new_session=True)\n'
                      'time.sleep(60)\n')
    status = tmp_path / 'supervisor.json'
    proc = await asyncio.create_subprocess_exec(sys.executable, '-I', supervisor.__file__, '--ownership', ownership_mode, str(status),
                                               sys.executable, '-I', str(script), start_new_session=True)
    try:
        for _ in range(500):
            if pidfile.exists() and pidfile.read_text(): break
            await asyncio.sleep(.01)
        assert pidfile.exists()
        child = int(pidfile.read_text())
        assert await _stop(proc, .05)
        if ownership_mode == 'subreaper':
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
        else:
            try:
                assert psutil.Process(child).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                pass
        record = json.loads(status.read_text())
        assert record['forced_stop'] and record['cleanup_complete']
    finally:
        if proc.returncode is None:
            os.kill(proc.pid, signal.SIGUSR1)
            await proc.wait()


def test_default_grace_respects_manifest_bounds(tmp_path):
    from test_runner import prepared
    from evolution_lab.runner import _grace
    p = prepared(tmp_path)
    w = p.manifest.workflow
    assert _grace(p) >= w.campaign.editor_timeout_seconds + w.scorer.timeout_seconds + w.campaign.budget.timeout_seconds
