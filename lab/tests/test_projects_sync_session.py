"""ProjectSession.sync_check / sync_apply (spec 2026-10-08-lab-project-sync §2.2)."""
from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest

from evolution_lab.projects import sync
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.revision import materials_revision
from evolution_lab.projects.session import ProjectSession

PAIR = Pair("0.0.3.dev42", "0.3.0a28")


def git(cwd, *argv):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = T\n\temail = t@t\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    team = tmp_path / "team"
    (team / "agent").mkdir(parents=True)
    (team / "lab").mkdir()
    (team / "agent" / "a.py").write_text("A = 1\n")
    git(team, "init", "-q", "-b", "main")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "init")
    projects = Projects.open(tmp_path / "home")
    projects.create(id="acq", name="Acq", materials_root="lab", implementations=[{"id": "agent", "place": "agent"}],
                    clone={"repo": str(team)})
    session = ProjectSession(projects, preparation=None, owner=None, pair=PAIR, state_root=tmp_path / "state")
    yield session, projects, team
    projects.close()


def test_check_then_apply_moves_base_revision_and_stales_the_place(env):
    session, projects, team = env
    digest = materials_revision(projects.get("acq")).place_digest("implementation", "agent")
    (team / "agent" / "b.py").write_text("B = 1\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "b")
    check = asyncio.run(session.sync_check("acq"))
    assert check["mode"] == "fast_forward"
    result = asyncio.run(session.sync_apply("acq", upstream=check["upstream"]["sha"]))
    assert result["schema"] == "lab-project-sync-result/1" and result["mode"] == "fast_forward"
    assert result["project"]["working_copy"]["source"]["base_revision"] == check["upstream"]["sha"]
    assert materials_revision(projects.get("acq")).place_digest("implementation", "agent") != digest


def test_invalid_rev_and_upstream_are_invalid_request(env):
    session, *_ = env
    for call in (session.sync_check("acq", rev="-x"), session.sync_apply("acq", upstream="HEAD")):
        with pytest.raises(ProjectError) as error:
            asyncio.run(call)
        assert error.value.code == "invalid_request"


def test_registry_failure_after_merge_is_sync_not_recorded_and_retry_records(env, monkeypatch):
    session, projects, team = env
    (team / "x.txt").write_text("x\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "x")
    sha = asyncio.run(session.sync_check("acq"))["upstream"]["sha"]
    real = projects.record_sync
    monkeypatch.setattr(projects, "record_sync", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(ProjectError) as error:
        asyncio.run(session.sync_apply("acq", upstream=sha))
    assert error.value.code == "sync_not_recorded"
    monkeypatch.setattr(projects, "record_sync", real)
    result = asyncio.run(session.sync_apply("acq", upstream=sha))
    assert result["mode"] == "up_to_date" and result["project"]["working_copy"]["source"]["base_revision"] == sha


def test_project_error_from_the_registry_after_merge_is_also_sync_not_recorded(env, monkeypatch):
    session, projects, team = env
    (team / "x.txt").write_text("x\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "x")
    sha = asyncio.run(session.sync_check("acq"))["upstream"]["sha"]
    monkeypatch.setattr(projects, "record_sync", lambda *a, **k: (_ for _ in ()).throw(ProjectError("not_a_clone")))
    with pytest.raises(ProjectError) as error:
        asyncio.run(session.sync_apply("acq", upstream=sha))
    assert error.value.code == "sync_not_recorded"


def test_cancelled_caller_keeps_the_locks_until_git_ends(env, monkeypatch):
    session, projects, team = env
    (team / "x.txt").write_text("x\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "x")
    sha = asyncio.run(session.sync_check("acq"))["upstream"]["sha"]
    gate, entered = threading.Event(), threading.Event()
    real = sync.integrate

    def slow(*args, **kwargs):
        entered.set()
        gate.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(sync, "integrate", slow)

    async def scenario():
        task = asyncio.create_task(session.sync_apply("acq", upstream=sha))
        try:
            for _ in range(500):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0.05)
            assert session._mutation_lock.locked()  # still held: git has not finished
        finally:
            gate.set()  # never leave the worker thread blocked
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not session._mutation_lock.locked()
    asyncio.run(scenario())
    assert projects.get("acq").working_copy.source["base_revision"] == sha  # the work ran to its end


def test_read_only_registry_handle_refuses_sync_before_any_git(env, tmp_path, monkeypatch):
    """ADR-0017, one writer per home: a handle without the home lock never fetches nor merges (codex r1 #2)."""
    session, projects, team = env
    clone = projects.home / "clones" / "acq"
    (team / "x.txt").write_text("x\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "x")
    head = git(clone, "rev-parse", "HEAD")
    refs = git(clone, "for-each-ref")
    calls = []
    real = sync._run
    monkeypatch.setattr(sync, "_run", lambda *a, **k: calls.append(a) or real(*a, **k))
    with Projects.open(projects.home, read_only=True) as reader:
        reader_session = ProjectSession(reader, preparation=None, owner=None, pair=PAIR, state_root=tmp_path / "state2")
        for call in (reader_session.sync_check("acq"), reader_session.sync_apply("acq", upstream="a" * 40)):
            with pytest.raises(ProjectError) as error:
                asyncio.run(call)
            assert error.value.code == "home_busy"
    assert not calls and git(clone, "rev-parse", "HEAD") == head and git(clone, "for-each-ref") == refs


def test_sync_not_recorded_retry_records_after_the_source_branch_is_deleted(env, monkeypatch):
    session, projects, team = env
    git(team, "switch", "-q", "-c", "tmp")
    (team / "x.txt").write_text("x\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "x")
    sha = git(team, "rev-parse", "HEAD")
    git(team, "switch", "-q", "main")
    assert asyncio.run(session.sync_check("acq", rev="tmp"))["upstream"]["sha"] == sha
    real = projects.record_sync
    monkeypatch.setattr(projects, "record_sync", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(ProjectError) as error:
        asyncio.run(session.sync_apply("acq", upstream=sha))
    assert error.value.code == "sync_not_recorded"
    git(team, "branch", "-q", "-D", "tmp")
    asyncio.run(session.sync_check("acq"))  # fetch --prune: the branch is gone from the clone
    monkeypatch.setattr(projects, "record_sync", real)
    result = asyncio.run(session.sync_apply("acq", upstream=sha))
    assert result["mode"] == "up_to_date" and projects.get("acq").working_copy.source["base_revision"] == sha


def test_failure_finalizing_a_successful_merge_is_sync_not_recorded_and_retry_records(env, monkeypatch):
    session, projects, team = env
    (team / "x.txt").write_text("x\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "x")
    sha = asyncio.run(session.sync_check("acq"))["upstream"]["sha"]
    real = sync._run
    state = {"merged": False, "fail": True}

    def run(argv, clone, code, **kwargs):
        if state["fail"] and state["merged"] and argv == ["rev-parse", "HEAD"]:
            raise ProjectError(code)
        result = real(argv, clone, code, **kwargs)
        if "merge" in argv and "--no-overwrite-ignore" in argv and result.returncode == 0:
            state["merged"] = True
        return result
    monkeypatch.setattr(sync, "_run", run)
    with pytest.raises(ProjectError) as error:
        asyncio.run(session.sync_apply("acq", upstream=sha))
    assert error.value.code == "sync_not_recorded"
    state["fail"] = False
    result = asyncio.run(session.sync_apply("acq", upstream=sha))
    assert result["mode"] == "up_to_date" and result["project"]["working_copy"]["source"]["base_revision"] == sha
