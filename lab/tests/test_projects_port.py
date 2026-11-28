"""dev35 W73 (spec 2026-10-01 §3): `lab project port` — checks, the template snapshot, the port repository, one
registry transaction and its cleanup. The template is a throwaway git repository; nothing leaves tmp_path."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from evolution_lab.projects import clone as clone_module
from evolution_lab.projects import port as port_module
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair, project_from_wire
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.session import ProjectSession

PAIR = Pair("0.0.3.dev35", "0.3.0a21")
AUTHOR = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(*argv, cwd):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **AUTHOR}).stdout.strip()


def _template(tmp_path) -> Path:
    """Two commits, a tag, a tracked file its own .gitignore matches, an untracked file and a remote."""
    work = tmp_path / "template"
    (work / "skills").mkdir(parents=True)
    (work / "skills" / "SKILL.md").write_text("v1\n")
    (work / ".gitignore").write_text("*.local\n")
    (work / "keep.local").write_text("tracked although ignored\n")
    _git("init", "-q", "-b", "main", cwd=work)
    _git("add", "-A", "-f", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    _git("tag", "v1", cwd=work)
    (work / "skills" / "SKILL.md").write_text("v2\n")
    (work / "untracked.txt").write_text("not in the template\n")
    _git("commit", "-q", "-am", "two", cwd=work)
    _git("remote", "add", "origin", "https://example.com/team/sylph.git", cwd=work)
    return work


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Project `acq` with implementation `agent`, its session (no Owner: port never calls it), a template."""
    tmp_path.chmod(0o700)
    for name in AUTHOR:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Pat Porter\n\temail = pat@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    wc = tmp_path / "wc"
    (wc / "lab").mkdir(parents=True)
    (wc / "agent").mkdir()
    projects = Projects.open(tmp_path / "home")
    projects.create(id="acq", name="Acq", path=str(wc), materials_root="lab",
                    implementations=[{"id": "agent", "place": "agent"}])
    session = ProjectSession(projects, preparation=None, owner=None, pair=PAIR, state_root=tmp_path / "state")
    yield session, projects, _template(tmp_path), wc
    projects.close()


def _item(value, implementation_id="agent-port"):
    return next(item for item in value["implementations"] if item["id"] == implementation_id)


def _ports_dir(projects):
    directory = projects.home / "ports" / "acq"
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


async def test_port_from_a_local_template(env):
    session, projects, template, wc = env
    before = sorted(path.relative_to(wc).as_posix() for path in wc.rglob("*"))
    value = await session.port("acq", source="agent", template=str(template))
    place = projects.home / "ports" / "acq" / "agent-port"
    head = _git("rev-parse", "HEAD", cwd=template)
    item = _item(value)
    assert value["schema"] == "lab-project/2"
    assert item == {"id": "agent-port", "name": "agent-port", "place": str(place),
                    "origin": {"kind": "ported_from", "implementation": "agent"}, "package_entry": None,
                    "archived": None, "port": {"template": str(template), "ref": "HEAD", "commit": head,
                                               "created_at": item["port"]["created_at"]}}
    assert (place / "skills" / "SKILL.md").read_text() == "v2\n" and (place / "keep.local").is_file()
    assert not (place / "untracked.txt").exists()
    assert _git("log", "--format=%s", cwd=place) == f"Lab: port of agent from {template}@{head}"
    assert _git("log", "--format=%an <%ae>", cwd=place) == "Pat Porter <pat@example.com>"
    assert _git("tag", cwd=place) == "lab/port/created"
    assert _git("remote", cwd=place) == "" and _git("rev-list", "--count", "HEAD", cwd=place) == "1"
    assert _git("status", "--porcelain", cwd=place) == ""
    assert sorted(path.relative_to(wc).as_posix() for path in wc.rglob("*")) == before  # never the working copy
    assert projects.store.ports("acq")["agent-port"]["commit"] == head
    assert _ports_dir(projects) == ["agent-port"]  # no staging left


async def test_a_port_row_reaches_get_project_as_lab_project_2(env):
    """Review of Task 5: a real `ports` row, written by a port, flows through `get_project` into the wire form."""
    session, projects, template, wc = env
    await session.port("acq", source="agent", template=str(template))
    got = await session.get_project("acq")
    row = projects.store.ports("acq")["agent-port"]
    assert got["schema"] == "lab-project/2" and _item(got, "agent")["port"] is None
    assert _item(got)["port"] == {"template": str(template), "ref": "HEAD",
                                  "commit": _git("rev-parse", "HEAD", cwd=template), "created_at": row["created_at"]}
    record, ports = project_from_wire(got)
    assert ports == {"agent-port": _item(got)["port"]}
    assert record.implementation("agent-port").origin == {"kind": "ported_from", "implementation": "agent"}


@pytest.mark.parametrize("value, url", [
    ("https://example.com/team/sylph.git", True), ("file:///srv/sylph", True), ("git@example.com:team/sylph", True),
    ("/srv/sylph", False), ("/srv/a:b", False), ("./x:y", False),
])
def test_one_url_check_is_shared_with_clone(value, url):
    assert clone_module.is_url(value) is url
    assert port_module.is_url is clone_module.is_url


async def test_a_ref_a_given_id_and_the_default_id_sequence(env):
    session, projects, template, wc = env
    tagged = _item(await session.port("acq", source="agent", template=str(template), ref="v1", port_id="tagged"),
                   "tagged")
    assert tagged["port"]["ref"] == "v1"
    assert tagged["port"]["commit"] == _git("rev-parse", "v1^{commit}", cwd=template)
    assert (Path(tagged["place"]) / "skills" / "SKILL.md").read_text() == "v1\n"
    await session.port("acq", source="agent", template=str(template))
    second = await session.port("acq", source="agent", template=str(template))
    assert [item["id"] for item in second["implementations"]] == ["agent", "tagged", "agent-port", "agent-port-2"]


@pytest.mark.parametrize("kwargs, code", [
    ({"source": "nope"}, "implementation_not_found"),
    ({"source": "agent", "port_id": "agent"}, "id_taken"),
    ({"source": "agent", "port_id": "Bad"}, "invalid_id"),
    ({"source": "agent", "ref": "no-such-ref"}, "template_ref_not_found"),
    ({"source": "agent", "ref": "-x"}, "invalid_request"),
])
async def test_refusals_leave_nothing(env, kwargs, code):
    session, projects, template, wc = env
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", template=str(template), **kwargs)
    assert caught.value.code == code
    assert [item.id for item in projects.get("acq").implementations] == ["agent"]
    assert _ports_dir(projects) == [] and projects.store.ports("acq") == {}


async def test_an_archived_source_is_refused(env):
    session, projects, template, wc = env
    projects.update("acq", {"op": "archive_implementation", "implementation": "agent", "reason": None})
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template))
    assert caught.value.code == "implementation_archived"


async def test_an_archived_implementations_id_stays_taken(env):
    session, projects, template, wc = env
    await session.port("acq", source="agent", template=str(template), port_id="p1")
    projects.update("acq", {"op": "archive_implementation", "implementation": "p1", "reason": None})
    assert (projects.home / "ports" / "acq" / "p1" / ".git").is_dir()  # archiving never deletes the port (§3.6)
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template), port_id="p1")
    assert caught.value.code == "id_taken"


async def test_into_the_working_copy_is_refused(env):
    session, projects, template, wc = env
    for into in (wc / "lab" / "port", wc):
        with pytest.raises(ProjectError) as caught:
            await session.port("acq", source="agent", template=str(template), into=str(into))
        assert caught.value.code == "port_into_working_copy"
    assert not (wc / "lab" / "port").exists()


async def test_into_the_lab_home_is_refused(env):
    """LAB_HOME is Lab's own (its open sweeps `tmp/port-*` and `ports/*/.staging-*`): no `--into` lands there."""
    session, projects, template, wc = env
    home = projects.home
    for into in (home / "tmp" / "x", home / "ports" / "acq" / ".staging-x", home / "ports" / "acq" / "mine", home):
        with pytest.raises(ProjectError) as caught:
            await session.port("acq", source="agent", template=str(template), into=str(into))
        assert caught.value.code == "port_into_lab_home" and caught.value.detail is None
    assert _ports_dir(projects) == [] and not (home / "tmp" / "x").exists()


async def test_into_an_empty_directory_and_a_relative_into(env, tmp_path):
    session, projects, template, wc = env
    target = tmp_path / "elsewhere" / "port"
    target.mkdir(parents=True)
    value = await session.port("acq", source="agent", template=str(template), into=str(target))
    assert _item(value)["place"] == str(target) and (target / ".git").is_dir()
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template), into="relative/port", port_id="p2")
    assert caught.value.code == "invalid_request"


async def test_an_existing_into_destination_is_refused_without_echoing_the_path(env, tmp_path):
    """The service never echoes request data: the caller knows the --into it passed."""
    session, projects, template, wc = env
    target = tmp_path / "taken"
    target.mkdir()
    (target / "file").write_text("x")
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template), into=str(target))
    assert caught.value.code == "port_destination_exists" and caught.value.detail is None
    assert caught.value.to_payload()["message"] == "The port destination exists and is not empty."
    assert (target / "file").read_text() == "x"


async def test_a_leftover_default_destination_names_its_path_and_is_never_reused(env):
    session, projects, template, wc = env
    leftover = projects.home / "ports" / "acq" / "agent-port"
    (leftover / ".git").mkdir(parents=True)  # as a crash between rename and commit leaves it
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template))
    assert caught.value.code == "port_destination_exists" and caught.value.detail == str(leftover)
    assert caught.value.to_payload()["message"] == f"The port destination exists and is not empty. {leftover}"


async def test_git_unavailable(env, monkeypatch):
    session, projects, template, wc = env
    monkeypatch.setattr(port_module.shutil, "which", lambda name: None)
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template))
    assert caught.value.code == "git_unavailable"


async def test_a_plain_or_missing_directory_is_not_git(env, tmp_path):
    session, projects, template, wc = env
    plain = tmp_path / "plain"
    plain.mkdir()
    for source in (plain, tmp_path / "missing"):
        with pytest.raises(ProjectError) as caught:
            await session.port("acq", source="agent", template=str(source))
        assert caught.value.code == "template_not_git"


async def test_template_inside_another_repository_is_not_git(env):
    """Review Focus: `git rev-parse` walks up to the enclosing repository; a subdirectory is not a template."""
    session, projects, template, wc = env
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template / "skills"))
    assert caught.value.code == "template_not_git"


async def test_a_directory_inside_a_bare_repository_is_not_git(env, tmp_path):
    """`--is-bare-repository` is true in `refs/`, `hooks/`, `objects/` too: only the bare repository's top counts."""
    session, projects, template, wc = env
    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(template), str(bare), cwd=tmp_path)
    for inside in (bare / "refs", bare / "objects", bare / "hooks"):
        with pytest.raises(ProjectError) as caught:
            await session.port("acq", source="agent", template=str(inside))
        assert caught.value.code == "template_not_git"
    assert _ports_dir(projects) == [] and projects.store.ports("acq") == {}


async def test_a_bare_repository_is_a_template(env, tmp_path):
    session, projects, template, wc = env
    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(template), str(bare), cwd=tmp_path)
    value = await session.port("acq", source="agent", template=str(bare))
    assert _item(value)["port"]["commit"] == _git("rev-parse", "HEAD", cwd=template)


async def test_a_file_url_is_fetched_shallow_into_lab_tmp_and_removed(env, monkeypatch):
    session, projects, template, wc = env
    calls = []
    real = port_module._run

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real(argv, **kwargs)
    monkeypatch.setattr(port_module, "_run", spy)
    url = f"file://{template}"
    item = _item(await session.port("acq", source="agent", template=url, ref="main"))
    assert item["port"]["template"] == url and item["port"]["commit"] == _git("rev-parse", "main", cwd=template)
    assert (Path(item["place"]) / "skills" / "SKILL.md").read_text() == "v2\n"
    assert ["-c", "credential.helper=", "fetch", "-q", "--depth", "1", url, "main"] in calls
    # No credential helper on any call that carries the URL: a token is never approved into a store.
    assert all(argv[:2] == ["-c", "credential.helper="] for argv in calls if url in argv)
    assert any(argv[2] == "ls-remote" for argv in calls if url in argv)
    assert not (projects.home / "tmp").exists() or not any((projects.home / "tmp").iterdir())


@pytest.mark.parametrize("make, ref, code", [
    (lambda tmp, template: f"file://{tmp / 'plain-url'}", None, "template_not_git"),
    (lambda tmp, template: f"file://{template}", "no-such-ref", "template_ref_not_found"),
    (lambda tmp, template: "http://127.0.0.1:9/nothing.git", None, "template_fetch_failed"),
])
async def test_url_refusals(env, tmp_path, make, ref, code):
    session, projects, template, wc = env
    (tmp_path / "plain-url").mkdir()
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=make(tmp_path, template), ref=ref)
    assert caught.value.code == code
    assert _ports_dir(projects) == []


async def test_url_credentials_are_never_stored_or_logged(env, tmp_path, caplog):
    session, projects, template, wc = env
    caplog.set_level(logging.DEBUG, logger="evolution_lab.projects")
    value = await session.port("acq", source="agent", template=f"file://user:s3cret@localhost{template}")
    item = _item(value)
    shown = f"file://***@localhost{template}"
    assert item["port"]["template"] == shown and projects.store.ports("acq")["agent-port"]["template"] == shown
    assert "s3cret" not in json.dumps(value)
    place = Path(item["place"])
    assert "s3cret" not in _git("log", "--format=%B", cwd=place)
    assert all("s3cret" not in path.read_text(errors="ignore") for path in (place / ".git").rglob("*") if path.is_file())
    with pytest.raises(ProjectError):
        await session.port("acq", source="agent", template=f"file://user:s3cret@localhost{tmp_path}/missing",
                           port_id="p2")
    assert caplog.records and "s3cret" not in caplog.text


async def test_lab_identity_only_without_a_user_identity(env, tmp_path, monkeypatch):
    session, projects, template, wc = env
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    place = Path(_item(await session.port("acq", source="agent", template=str(template)))["place"])
    assert _git("log", "--format=%an <%ae>", cwd=place) == "Evolution Lab <lab@evolution-lab.invalid>"
    assert not (tmp_path / "empty-gitconfig").exists()  # the global config is never written


async def test_a_global_tag_signing_setting_does_not_sign_or_prompt(env, tmp_path, monkeypatch):
    session, projects, template, wc = env
    config = tmp_path / "signing-gitconfig"
    config.write_text("[user]\n\tname = Pat Porter\n\temail = pat@example.com\n[tag]\n\tgpgSign = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_EDITOR", "false")
    place = Path(_item(await session.port("acq", source="agent", template=str(template)))["place"])
    assert _git("cat-file", "-t", "lab/port/created", cwd=place) == "commit"  # lightweight, not an annotated tag


async def test_a_failed_transaction_removes_the_port_and_writes_nothing(env, monkeypatch):
    session, projects, template, wc = env

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(projects.store, "insert_port", broken)
    with pytest.raises(sqlite3.OperationalError):
        await session.port("acq", source="agent", template=str(template))
    assert [item.id for item in projects.get("acq").implementations] == ["agent"]
    assert projects.get("acq").record_version == 1 and projects.store.ports("acq") == {}
    assert _ports_dir(projects) == []


async def test_a_failure_after_the_commit_keeps_the_port(env, monkeypatch):
    session, projects, template, wc = env

    def broken(project):
        raise RuntimeError("response failed")
    monkeypatch.setattr(session, "project_payload", broken)
    with pytest.raises(RuntimeError):
        await session.port("acq", source="agent", template=str(template))
    place = projects.home / "ports" / "acq" / "agent-port"
    assert (place / ".git").is_dir() and "agent-port" in projects.store.ports("acq")
    assert projects.get("acq").implementation("agent-port").place == str(place)


async def test_a_source_archived_during_the_fetch_is_refused_at_publication(env, monkeypatch):
    session, projects, template, wc = env
    real = port_module.stage

    def stage_then_archive(home, plan):
        staged = real(home, plan)
        projects.update("acq", {"op": "archive_implementation", "implementation": "agent", "reason": "moved"})
        return staged
    monkeypatch.setattr(port_module, "stage", stage_then_archive)
    with pytest.raises(ProjectError) as caught:
        await session.port("acq", source="agent", template=str(template))
    assert caught.value.code == "implementation_archived"
    assert _ports_dir(projects) == [] and projects.store.ports("acq") == {}


async def test_only_the_publication_runs_under_the_mutation_lock(env, monkeypatch):
    """...and the slow stage runs on its own thread, never on the session's `run_io` (in the service a one-worker
    executor every project and preparation call shares)."""
    session, projects, template, wc = env
    seen, in_run_io = {}, []
    real_stage, real_publish, real_run_io = port_module.stage, port_module.publish, session._run_io

    async def run_io(function):
        in_run_io.append(True)
        try:
            return await real_run_io(function)
        finally:
            in_run_io.pop()
    monkeypatch.setattr(session, "_run_io", run_io)

    def stage(home, plan):
        seen["stage"] = session._mutation_lock.locked()
        seen["stage_on_run_io"] = bool(in_run_io)
        return real_stage(home, plan)

    def publish(projects_, plan, staged):
        seen["publish"] = session._mutation_lock.locked()
        return real_publish(projects_, plan, staged)
    monkeypatch.setattr(port_module, "stage", stage)
    monkeypatch.setattr(port_module, "publish", publish)
    await session.port("acq", source="agent", template=str(template))
    assert seen == {"stage": False, "stage_on_run_io": False, "publish": True}


def _home(tmp_path) -> Path:
    tmp_path.chmod(0o700)
    home = tmp_path / "home"
    Projects.open(home).close()
    return home


def _leftover(path: Path, *, marked: bool = True) -> Path:
    """A directory with content, as an interrupted port (marked) or someone else (unmarked) leaves it."""
    (path / "inner").mkdir(parents=True)
    (path / "inner" / "file").write_text("x\n")
    if marked:
        port_module.mark_leftover(path)
    return path


def _markers(root: Path) -> list[str]:
    return sorted(str(path) for path in root.rglob(f"*{port_module.LEFTOVER_MARKER_SUFFIX}"))


def test_open_sweeps_only_marked_port_leftovers(tmp_path):
    """Codex r2 (PDR-0018): a directory is Lab's port leftover only by Lab's marker, never by its name."""
    home = _home(tmp_path)
    fetch = _leftover(home / "tmp" / "port-abcd")
    staging = _leftover(home / "ports" / "acq" / ".staging-agent-port-abcd")
    unmarked_fetch = _leftover(home / "tmp" / "port-old", marked=False)  # e.g. an Owner state root
    unmarked_staging = _leftover(home / "ports" / "acq" / ".staging-mine", marked=False)
    other_tmp = _leftover(home / "tmp" / "other")  # marked, but not a port name: not Lab's port leftover
    kept = home / "ports" / "acq" / "agent-port"
    kept.mkdir()
    orphan = home / "tmp" / f"port-gone{port_module.LEFTOVER_MARKER_SUFFIX}"
    orphan.write_text("")
    Projects.open(home).close()
    assert not fetch.exists() and not staging.exists() and not orphan.exists()
    for survivor in (unmarked_fetch, unmarked_staging):
        assert (survivor / "inner" / "file").read_text() == "x\n"
    assert kept.is_dir() and other_tmp.is_dir()
    assert _markers(home) == [str(home / "tmp" / f"other{port_module.LEFTOVER_MARKER_SUFFIX}")]


@pytest.mark.parametrize("marked", [False, True])
def test_a_registered_place_in_the_home_survives_reopen(tmp_path, marked):
    """A hand-registered port at ``H/tmp/port-saved`` (valid through the implementation API) is never swept, even
    marked; so is a marked directory that contains or lies inside a registered place."""
    home = _home(tmp_path)
    wc = tmp_path / "wc"
    (wc / "agent").mkdir(parents=True)
    (wc / "lab").mkdir()
    saved, outer = home / "tmp" / "port-saved", home / "tmp" / "port-outer"
    holder = home / "ports" / "other"
    inner = holder / ".staging-x-1234"
    with Projects.open(home) as projects:
        projects.create(id="acq", name="Acq", path=str(wc), materials_root="lab",
                        implementations=[{"id": "agent", "place": "agent"}])
        for item_id, place in (("saved", saved), ("deep", outer / "inner"), ("holder", holder)):
            projects.update("acq", {"op": "add_implementation", "implementation": {
                "id": item_id, "place": str(place), "origin": {"kind": "ported_from", "implementation": "agent"}}})
        projects.update("acq", {"op": "archive_implementation", "implementation": "deep", "reason": None})
        projects.update("acq", {"op": "archive", "reason": None})
    for directory in (saved, outer, inner):
        _leftover(directory, marked=marked)
    Projects.open(home).close()
    for directory in (saved, outer, inner):
        assert (directory / "inner" / "file").read_text() == "x\n"


def test_a_protected_root_survives_even_marked(tmp_path):
    """The service passes its state root: a marked directory that is, holds or lies inside it is never swept."""
    home = _home(tmp_path)
    state = _leftover(home / "tmp" / "port-state")
    Projects.open(home, protected=(state / "inner",)).close()
    assert (state / "inner" / "file").read_text() == "x\n"
    Projects.open(home, protected=(state,)).close()
    assert (state / "inner" / "file").read_text() == "x\n"


def test_the_sweep_never_follows_symlinks(tmp_path):
    home = _home(tmp_path)
    outside = _leftover(tmp_path / "outside" / ".staging-y-1234")
    target = _leftover(tmp_path / "target")
    (home / "tmp").mkdir(exist_ok=True)
    os.symlink(target, home / "tmp" / "port-link")
    port_module.mark_leftover(home / "tmp" / "port-link")
    (home / "ports").mkdir(exist_ok=True)
    os.symlink(outside.parent, home / "ports" / "acq")
    elsewhere = _leftover(home / "tmp" / "port-real", marked=False)
    os.symlink(tmp_path / "outside" / f".staging-y-1234{port_module.LEFTOVER_MARKER_SUFFIX}",
               home / "tmp" / f"port-real{port_module.LEFTOVER_MARKER_SUFFIX}")  # a symlinked marker is no marker
    Projects.open(home).close()
    assert (target / "inner" / "file").is_file() and (outside / "inner" / "file").is_file()
    assert (home / "tmp" / "port-link").is_symlink() and (elsewhere / "inner" / "file").is_file()


async def test_a_staging_is_marked_and_a_published_port_is_not(env, tmp_path):
    session, projects, template, wc = env
    plan = port_module.plan_port(projects, "acq", source_id="agent", template=str(template), ref=None, port_id=None,
                                 into=None)
    staged = port_module.stage(projects.home, plan)
    assert port_module.is_marked_leftover(staged.directory)
    assert not list(staged.directory.rglob(f"*{port_module.LEFTOVER_MARKER_SUFFIX}"))  # never inside the tree
    port_module.publish(projects, plan, staged)
    assert not port_module.is_marked_leftover(plan.destination) and _markers(projects.home) == []
    into = tmp_path / "elsewhere" / "port"
    await session.port("acq", source="agent", template=f"file://{template}", into=str(into), port_id="p2")
    assert _markers(projects.home) == [] and _markers(into.parent) == []
    assert _git("status", "--porcelain", cwd=into) == ""


async def test_a_failed_port_leaves_no_marker(env, tmp_path):
    session, projects, template, wc = env
    with pytest.raises(ProjectError):
        await session.port("acq", source="agent", template=f"file://{template}", ref="no-such-ref")
    with pytest.raises(ProjectError):
        await session.port("acq", source="agent", template=str(template), ref="no-such-ref")
    assert _markers(projects.home) == [] and _ports_dir(projects) == []


async def test_a_staging_that_could_not_be_removed_keeps_its_mark(env, monkeypatch):
    """The mark goes only with its directory: what a failed removal leaves, the next open still sweeps."""
    session, projects, template, wc = env
    with monkeypatch.context() as patched, pytest.raises(ProjectError):
        patched.setattr(port_module.shutil, "rmtree", lambda *args, **kwargs: None)
        await session.port("acq", source="agent", template=f"file://{template}", ref="no-such-ref")
    left = [path for path in (projects.home / "ports" / "acq").iterdir() if path.is_dir()]
    left += [path for path in (projects.home / "tmp").iterdir() if path.is_dir()]
    assert len(left) == 2 and all(port_module.is_marked_leftover(path) for path in left)
    projects.close()
    Projects.open(projects.home).close()
    assert not any(path.exists() for path in left) and _markers(projects.home) == []


def test_the_plan_repr_never_shows_the_raw_template(env):
    session, projects, template, wc = env
    url = f"https://user:s3cret@example.com/team/sylph.git"
    plan = port_module.plan_port(projects, "acq", source_id="agent", template=url, ref=None, port_id=None, into=None)
    assert plan.template == url and "s3cret" not in repr(plan)
    assert "https://***@example.com/team/sylph.git" in repr(plan)


def _tree(root: Path) -> dict:
    """Every path under ``root`` with its bytes (directories as None): any write into a repository shows."""
    return {path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None)
            for path in sorted(root.rglob("*"))}


# Repository selectors a service could inherit; each one alone redirects git away from its cwd.
SELECTORS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


@pytest.mark.parametrize("url", [False, True])
async def test_inherited_git_repository_selectors_never_reach_the_working_copy(env, monkeypatch, url):
    """codex r1 of the branch (PDR-0018): GIT_DIR/GIT_WORK_TREE/... in the service environment would send the
    port's init/fetch/add/commit/tag into another repository, e.g. the project's working copy."""
    session, projects, template, wc = env
    (wc / "agent" / "a.py").write_text("A = 1\n")
    _git("init", "-q", "-b", "main", cwd=wc)
    _git("add", "-A", ".", cwd=wc)
    # No detached auto-maintenance from this commit: its lock file would come and go under the comparison.
    _git("-c", "maintenance.auto=false", "-c", "gc.auto=0", "commit", "-q", "-m", "team", cwd=wc)
    before = _tree(wc)
    for name, value in zip(SELECTORS, (wc / ".git", wc, wc / ".git" / "index", wc / ".git" / "objects",
                                       wc / ".git")):
        monkeypatch.setenv(name, str(value))
    value = await session.port("acq", source="agent", template=f"file://{template}" if url else str(template))
    for name in SELECTORS:
        monkeypatch.delenv(name)
    assert _tree(wc) == before  # no object, ref, tag, index or file of the working copy changed
    place = Path(_item(value)["place"])
    assert (place / "skills" / "SKILL.md").read_text() == "v2\n"
    assert _git("tag", cwd=place) == "lab/port/created"
    assert _git("rev-list", "--count", "HEAD", cwd=place) == "1"


def test_the_shared_git_environment_drops_repository_selectors(monkeypatch):
    for name in (*SELECTORS, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                 "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_PREFIX", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(name, "/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/kept/gitconfig")
    got = clone_module._environment()
    assert not {name for name in got if name in SELECTORS or name in clone_module.REPOSITORY_SELECTORS}
    assert got["GIT_CONFIG_GLOBAL"] == "/kept/gitconfig" and got["GIT_TERMINAL_PROMPT"] == "0"


def test_the_template_is_always_extracted_with_the_tar_filter(tmp_path, monkeypatch):
    """dev38 (spec 2026-10-02 §4): Python >= 3.11.4 is the floor, so the "tar" filter is never optional — no silent
    unfiltered extraction on an older interpreter."""
    import tarfile
    work = _template(tmp_path)
    seen = []
    real = tarfile.TarFile.extractall

    def spy(self, path=".", members=None, *, numeric_owner=False, filter=None):
        seen.append(filter)
        return real(self, path, members, numeric_owner=numeric_owner, filter=filter)
    monkeypatch.setattr(tarfile.TarFile, "extractall", spy)
    monkeypatch.delattr(tarfile, "tar_filter")  # the old code path skipped the filter when this was missing
    staging = tmp_path / "staging"
    staging.mkdir()
    port_module._extract(work, "HEAD", staging, dict(os.environ))
    assert seen == ["tar"] and (staging / "skills" / "SKILL.md").is_file()
