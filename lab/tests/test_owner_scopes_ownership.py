"""Live helper coverage of the group/tree/marker scope and its declared gap."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil
import pytest

from ownership_modes import ownership_mode  # noqa: F401
from platforms import linux_only
from test_owner_scopes import finish, scope
from evolution_lab.owner.protocol import HelperHandle, ProtocolError, make_config
from evolution_lab.owner.receipts import read_cleanup_receipt


async def _file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(.01)
    assert path.exists(), f"child did not publish {path.name}"
    return path.read_text()


def _live(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _cleanup(processes):
    for process in processes:
        try:
            if _live(process):
                process.kill()
        except psutil.NoSuchProcess:
            pass


def _instrumented_python(tmp_path, patch):
    executable = tmp_path / "helper-python"
    executable.write_text(f"#!{sys.executable}\nimport sys, time\nfrom pathlib import Path\n"
        "sys.argv = sys.argv[3:]\nfrom evolution_lab.owner._helper import ScopeHelper, main\n"
        + patch + "\nraise SystemExit(main())\n")
    executable.chmod(0o700)
    return str(executable)


async def test_probe_matrix_group_tree_marker(tmp_path):
    directory, sid, generation = scope(tmp_path)
    sleeper = "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    program = f'''import json,os,subprocess,sys,time
children=[]
for new_session,env in [(False,None),(True,None),(False,{{}}),(True,{{}})]:
 children.append(subprocess.Popen([sys.executable,'-I','-c',{sleeper!r}],env=env,start_new_session=new_session))
children.append(subprocess.Popen(['/bin/sleep','60']))
if os.fork()==0:
 os.setsid()
 if os.fork()!=0: os._exit(0)
 open('daemon.tmp','w').write(str(os.getpid()))
 os.replace('daemon.tmp','daemon.pid')
 while True: time.sleep(.05)
os.waitpid(-1,0)
while not os.path.exists('daemon.pid'): time.sleep(.01)
json.dump([os.getpid(),*[p.pid for p in children],int(open('daemon.pid').read())],open('matrix.tmp','w'))
os.replace('matrix.tmp','matrix.json')
while True: time.sleep(.05)
'''
    handle = HelperHandle.launch(sys.executable, directory, sid, generation, ownership="group_tree_marker")
    processes = []
    try:
        await handle.configure(argv=[sys.executable, '-I', '-c', program], env={},
                               cwd=str(directory), stop_grace_seconds=.2)
        await handle.wait_event('ready', 5)
        await handle.go()
        processes = [psutil.Process(pid) for pid in json.loads(await _file(directory / 'matrix.json'))]
        await handle.stop()
        assert await handle.wait(5) == 0
        assert all(not _live(p) for p in processes)
        receipt = read_cleanup_receipt(directory, sid, generation)
        assert receipt['process_tree_empty'] and receipt['forced']
    finally:
        _cleanup(processes)
        await finish(handle)


async def test_group_member_forked_after_last_poll_is_killed(tmp_path):
    directory, sid, generation = scope(tmp_path)
    member = '''import os,time
while not os.path.exists('fork'): time.sleep(.01)
if os.fork(): os._exit(0)
open('grandchild.pid','w').write(str(os.getpid()))
while True: time.sleep(.05)
'''
    program = f'''import os,subprocess,sys,time
p=subprocess.Popen([sys.executable,'-I','-c',{member!r}],env={{}})
open('leader.pid','w').write(str(os.getpid()))
open('member.pid','w').write(str(p.pid))
while not os.path.exists('exit'): time.sleep(.01)
'''
    handle = HelperHandle.launch(sys.executable, directory, sid, generation, ownership="group_tree_marker")
    processes, paused = [], False
    try:
        await handle.configure(argv=[sys.executable,'-I','-c',program], env={}, cwd=str(directory), stop_grace_seconds=.2)
        await handle.wait_event('ready',5)
        await handle.go()
        leader = psutil.Process(int(await _file(directory/'leader.pid')))
        processes.append(leader)
        await _file(directory/'member.pid')
        handle.process.send_signal(signal.SIGSTOP)
        paused = True
        (directory/'fork').touch()
        grandchild = psutil.Process(int(await _file(directory/'grandchild.pid')))
        processes.append(grandchild)
        (directory/'exit').touch()
        deadline = time.monotonic()+3
        while leader.status()!=psutil.STATUS_ZOMBIE and time.monotonic()<deadline:
            await asyncio.sleep(.01)
        assert leader.status()==psutil.STATUS_ZOMBIE
        assert grandchild.ppid()!=int((directory/'member.pid').read_text())
        assert 'EVOLUTION_LAB_SCOPE' not in grandchild.environ()
        await handle.force()
        handle.process.send_signal(signal.SIGCONT)
        paused = False
        assert await handle.wait(5)==0
        assert not _live(grandchild)
        assert read_cleanup_receipt(directory,sid,generation)['forced']
    finally:
        if paused:
            handle.process.send_signal(signal.SIGCONT)
        _cleanup(processes)
        await finish(handle)


async def test_residual_gap_survivor_is_documented(tmp_path):
    directory,sid,generation=scope(tmp_path)
    escaped = '''import os,time
if os.fork(): os._exit(0)
if os.fork(): os._exit(0)
open('escaped.pid','w').write(str(os.getpid()))
while True: time.sleep(.05)
'''
    program = f'''import subprocess,sys,time
p=subprocess.Popen([sys.executable,'-I','-c',{escaped!r}],env={{}},start_new_session=True)
p.wait()
while True: time.sleep(.05)
'''
    patch = f'''from evolution_lab._ownership import GroupTreeMarker
original_poll=GroupTreeMarker.poll
def delayed_poll(self):
 while not Path({str(directory/'escaped.pid')!r}).exists(): time.sleep(.01)
 return original_poll(self)
GroupTreeMarker.poll=delayed_poll
'''
    executable=_instrumented_python(tmp_path,patch)
    handle=HelperHandle.launch(executable,directory,sid,generation,ownership='group_tree_marker')
    survivor=None
    try:
        await handle.configure(argv=[sys.executable,'-I','-c',program],env={},cwd=str(directory),stop_grace_seconds=.2)
        await handle.wait_event('ready',5)
        await handle.go()
        survivor=psutil.Process(int(await _file(directory/'escaped.pid')))
        assert 'EVOLUTION_LAB_SCOPE' not in survivor.environ()
        await handle.stop()
        assert await handle.wait(5)==0
        assert _live(survivor)
        assert read_cleanup_receipt(directory,sid,generation)['process_tree_empty']
    finally:
        if survivor is not None:
            _cleanup([survivor])
        await finish(handle)


async def test_child_exited_comes_at_final_reap_in_macos_mode(tmp_path):
    directory,sid,generation=scope(tmp_path)
    paused,resume=tmp_path/'paused',tmp_path/'resume'
    patch=f'''original_event=ScopeHelper.event
def event(self,kind,data=None):
 if kind=='child_exited': Path({str(paused)!r}).touch()
 original_event(self,kind,data)
 if kind=='child_exited':
  while not Path({str(resume)!r}).exists(): time.sleep(.01)
ScopeHelper.event=event
'''
    child="import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    program=f'''import os,subprocess,sys,time
p=subprocess.Popen([sys.executable,'-I','-c',{child!r}],start_new_session=True)
open('descendant.pid','w').write(str(p.pid))
while True: time.sleep(.05)
'''
    handle=HelperHandle.launch(_instrumented_python(tmp_path,patch),directory,sid,generation,ownership='group_tree_marker')
    processes=[]
    try:
        await handle.configure(argv=[sys.executable,'-I','-c',program],env={},cwd=str(directory),stop_grace_seconds=10)
        await handle.wait_event('ready',5)
        await handle.go()
        processes=[psutil.Process(int(await _file(directory/'descendant.pid')))]
        await handle.stop()
        await handle.wait_event('child_exited',5)
        assert paused.exists()
        assert all(not _live(p) for p in processes)
        assert not (directory/'cleanup.receipt').exists()
        resume.touch()
        assert await handle.wait(5)==0
        kinds=[e['kind'] for e in handle.events]
        assert kinds[-2:]==['child_exited','tree_empty']
    finally:
        resume.touch()
        _cleanup(processes)
        await finish(handle)


async def test_stop_after_leader_exit_is_too_late_in_macos_mode(tmp_path):
    directory,sid,generation=scope(tmp_path)
    handle=HelperHandle.launch(sys.executable,directory,sid,generation,ownership='group_tree_marker')
    program="import os,time; open('leader.pid','w').write(str(os.getpid())); time.sleep(.2)"
    try:
        await handle.configure(argv=[sys.executable,'-I','-c',program],env={},cwd=str(directory),stop_grace_seconds=.2)
        await handle.wait_event('ready',5)
        await handle.go()
        leader=psutil.Process(int(await _file(directory/'leader.pid')))
        handle.process.send_signal(signal.SIGSTOP)
        deadline=time.monotonic()+3
        while leader.status()!=psutil.STATUS_ZOMBIE and time.monotonic()<deadline:
            await asyncio.sleep(.01)
        assert leader.status()==psutil.STATUS_ZOMBIE
        await handle.stop()
        handle.process.send_signal(signal.SIGCONT)
        assert await handle.wait(5)==0
        effects=[e['data']['value'] for e in handle.events if e['kind']=='stop_effect']
        assert effects==['too_late']
    finally:
        if handle.process.poll() is None:
            handle.process.send_signal(signal.SIGCONT)
        await finish(handle)


async def test_marker_token_never_persisted(tmp_path,ownership_mode):
    directory,sid,generation=scope(tmp_path)
    token_file=tmp_path/'token'
    program=f"import os; open({str(token_file)!r},'w').write(os.environ.get('EVOLUTION_LAB_SCOPE',''))"
    handle=HelperHandle.launch(sys.executable,directory,sid,generation)
    try:
        await handle.configure(argv=[sys.executable,'-I','-c',program],env={},stop_grace_seconds=.2)
        await handle.wait_event('ready',5)
        await handle.go()
        assert await handle.wait(5)==0
        token=token_file.read_bytes()
        if ownership_mode=='subreaper':
            assert token==b''
        else:
            assert len(token)==32
            for path in directory.iterdir():
                if path.is_file(): assert token not in path.read_bytes()
            assert token.decode() not in json.dumps(handle.events)
    finally:
        await finish(handle)


def test_config_environment_names_marker_is_refused():
    with pytest.raises(ProtocolError,match='invalid child environment'):
        make_config('a'*32,'b'*32,argv=[sys.executable,'-c','pass'],
                    env={'EVOLUTION_LAB_SCOPE':'x'},stop_grace_seconds=.2)


@pytest.mark.parametrize('mode_args',[[],['unknown']])
def test_helper_without_mode_argument_exits_before_ready(mode_args):
    result=subprocess.run([sys.executable,'-I','-m','evolution_lab.owner._helper',
        '-1','-1','-1','-1','a'*32,'b'*32,*mode_args],capture_output=True,timeout=5)
    assert result.returncode==2
    assert result.stdout==b''


async def test_macos_mode_without_psutil_fails_before_ready(tmp_path):
    directory,sid,generation=scope(tmp_path)
    patch='''import importlib.abc
class MissingPsutil(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='psutil': raise ImportError('psutil is unavailable')
sys.meta_path.insert(0,MissingPsutil())
'''
    handle=HelperHandle.launch(_instrumented_python(tmp_path,patch),directory,sid,generation,ownership='group_tree_marker')
    try:
        try:
            await handle.configure(argv=[sys.executable,'-c','pass'],env={},stop_grace_seconds=.2)
        except (OSError,ConnectionError):
            pass
        assert await handle.wait(5) not in (None,0)
        assert not any(e['kind']=='ready' for e in handle.events)
    finally:
        await finish(handle)


@linux_only('the test seam is admitted on Linux only')
async def test_owner_refuses_invalid_ownership_seam(tmp_path,monkeypatch):
    from evolution_lab.owner import Owner, OwnerError
    monkeypatch.setenv('EVOLUTION_LAB_TEST_OWNERSHIP','invalid')
    with pytest.raises(OwnerError) as caught:
        async with Owner.open(tmp_path/'state'):
            pass
    assert (caught.value.code,caught.value.stage)==('environment_invalid','connect')


def _late_marker_patch(pidfile):
    # Publish a real, separately grouped process only after two clean sweeps.
    return f"""import subprocess
from evolution_lab._ownership import GroupTreeMarker
original_reap = GroupTreeMarker.reap_leader
def reap(self):
 path = Path({str(pidfile)!r})
 if not path.exists():
  process = subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],
      env={{'EVOLUTION_LAB_SCOPE': self._token}}, start_new_session=True)
  path.write_text(str(process.pid))
 return original_reap(self)
GroupTreeMarker.reap_leader = reap
"""


async def test_final_reap_newcomer_is_killed_before_receipt(tmp_path):
    directory, sid, generation = scope(tmp_path)
    pidfile = tmp_path / 'newcomer.pid'
    executable = _instrumented_python(tmp_path, _late_marker_patch(pidfile))
    handle = HelperHandle.launch(executable, directory, sid, generation, ownership='group_tree_marker')
    try:
        await handle.configure(argv=[sys.executable, '-I', '-c', 'raise SystemExit(0)'],
                               env={}, stop_grace_seconds=.1)
        await handle.wait_event('ready', 5)
        await handle.go()
        assert await handle.wait(5) == 0
        try:
            newcomer = psutil.Process(int(pidfile.read_text()))
        except psutil.NoSuchProcess:
            pass
        else:
            assert not _live(newcomer)
        receipt = read_cleanup_receipt(directory, sid, generation)
        assert receipt['forced'] and receipt['process_tree_empty']
        assert receipt['child_exit_code'] == 0
        kinds = [event['kind'] for event in handle.events]
        assert kinds.count('child_exited') == 1
        assert kinds[-2:] == ['child_exited', 'tree_empty']
    finally:
        if pidfile.exists():
            try:
                _cleanup([psutil.Process(int(pidfile.read_text()))])
            except psutil.NoSuchProcess:
                pass
        await finish(handle)
