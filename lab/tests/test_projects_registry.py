import os
import subprocess

import pytest

from evolution_lab.projects.clone import clone_repository
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Project, WorkingCopy
from evolution_lab.projects.registry import Projects, utc_now

IMPL = [{"id": "agent", "place": "agent", "package_entry": "bundle-request.json"}]


@pytest.mark.parametrize("durability,synchronous", [(None, 1), ("strict", 2), ("relaxed", 1)])
def test_registry_durability(tmp_path, durability, synchronous, monkeypatch):
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    kwargs = {} if durability is None else {"durability": durability}
    with Projects.open(tmp_path / "home", **kwargs) as projects:
        assert projects.store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert projects.store._db.execute("PRAGMA synchronous").fetchone()[0] == synchronous


def test_registry_read_only_after_relaxed_writer_closes(tmp_path):
    with Projects.open(tmp_path / "home", durability="relaxed") as projects:
        projects.create(id="pr", name="Project", materials_root=".", implementations=[], path=str(tmp_path))
    with Projects.open(tmp_path / "home", read_only=True) as projects:
        assert projects.get("pr").name == "Project"


def test_empty_registry_transaction_does_not_commit_or_change_wal(tmp_path):
    with Projects.open(tmp_path / "home") as projects:
        projects.create(id="pr", name="Project", materials_root=".", implementations=[], path=str(tmp_path))
        statements = []
        projects.store._db.set_trace_callback(statements.append)
        wal = projects.home / "projects.sqlite3-wal"
        before = wal.read_bytes()
        with projects.store.transaction():
            assert projects.store.project("pr").name == "Project"
        assert "COMMIT" not in statements
        assert wal.read_bytes() == before


def _home(tmp_path):
    home = tmp_path / "home"
    return home


def _bare_repo(tmp_path):
    work = tmp_path / "team"
    (work / "agent").mkdir(parents=True)
    (work / "lab").mkdir()
    (work / "agent" / "a.py").write_text("A = 1\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(argv, cwd=work, check=True, env=env)
    return work


def test_create_from_path_is_ready(tmp_path):
    (tmp_path / "wc" / "lab").mkdir(parents=True)
    with Projects.open(_home(tmp_path)) as projects:
        project = projects.create(id="acquiring", name="Acquiring", materials_root="lab",
                                  implementations=IMPL, path=str(tmp_path / "wc"))
        assert project.state == "ready" and project.working_copy.git is False
        assert project.implementations[0].origin == {"kind": "connected"}
        assert projects.get("acquiring") == project


def test_generated_id_and_taken_id(tmp_path):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        generated = projects.create(id=None, name="x", materials_root=".", implementations=[], path=str(tmp_path / "wc"))
        assert len(generated.id.split("-")) == 3
        with pytest.raises(ProjectError) as caught:
            projects.create(id=generated.id, name="y", materials_root=".", implementations=[], path=str(tmp_path / "wc"))
        assert caught.value.code == "id_taken"


def test_clone_goes_through_staging_to_ready(tmp_path):
    team = _bare_repo(tmp_path)
    with Projects.open(_home(tmp_path)) as projects:
        project = projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                                  clone={"repo": str(team), "rev": None})
        clone = projects.home / "clones" / "acq"
        assert project.state == "ready" and project.working_copy.path == str(clone)
        assert project.working_copy.source["branch"] == "lab/acq"
        assert (clone / "agent" / "a.py").is_file()
        assert not list((projects.home / "clones").glob(".staging-*"))
        head = subprocess.run(["git", "-C", str(clone), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        assert head == "lab/acq"


def test_failed_clone_is_failed_and_retryable_with_same_id(tmp_path):
    team = _bare_repo(tmp_path)
    with Projects.open(_home(tmp_path)) as projects:
        with pytest.raises(ProjectError) as caught:
            projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                            clone={"repo": str(tmp_path / "missing"), "rev": None})
        assert caught.value.code == "clone_failed"
        assert projects.get("acq").state == "failed"
        assert not list((projects.home / "clones").glob(".staging-*"))
        project = projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                                  clone={"repo": str(team), "rev": None})
        assert project.state == "ready"


def test_interrupted_creation_is_reconciled_on_open(tmp_path):
    team = _bare_repo(tmp_path)
    home = _home(tmp_path)
    def crash(repo, rev, destination, branch):
        destination.mkdir()
        raise KeyboardInterrupt
    projects = Projects.open(home, clone_runner=crash)
    with pytest.raises(KeyboardInterrupt):
        projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL, clone={"repo": str(team), "rev": None})
    projects.close()
    with Projects.open(home) as reopened:
        assert reopened.get("acq").state == "failed" and reopened.get("acq").failure == "interrupted"
        assert not list((home / "clones").glob(".staging-*"))


def test_mutations_and_record_version(tmp_path):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        projects.create(id="pp", name="P", materials_root=".", implementations=IMPL, path=str(tmp_path / "wc"))
        renamed = projects.update("pp", {"op": "rename", "name": "Q"})
        assert renamed.name == "Q" and renamed.record_version == 2
        with pytest.raises(ProjectError) as caught:
            projects.update("pp", {"op": "rename", "name": "R"}, expected_record_version=1)
        assert caught.value.code == "conflict"
        added = projects.update("pp", {"op": "add_implementation", "implementation": {"id": "port", "place": "port", "origin": {"kind": "ported_from", "implementation": "agent"}}})
        assert [item.id for item in added.active_implementations()] == ["agent", "port"]
        archived = projects.update("pp", {"op": "archive_implementation", "implementation": "port", "reason": None})
        assert [item.id for item in archived.active_implementations()] == ["agent"]
        with pytest.raises(ProjectError) as caught:
            projects.update("pp", {"op": "add_implementation", "implementation": {"id": "port", "place": "x"}})
        assert caught.value.code == "id_taken"


def test_active_operation_refuses_disruptive_mutations(tmp_path):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        projects.create(id="pp", name="P", materials_root=".", implementations=IMPL, path=str(tmp_path / "wc"))
        busy = lambda project, implementation: "op-1"
        for mutation in ({"op": "archive", "reason": None}, {"op": "set_root", "materials_root": "lab"},
                         {"op": "move_implementation", "implementation": "agent", "place": "x"},
                         {"op": "archive_implementation", "implementation": "agent", "reason": None}):
            with pytest.raises(ProjectError) as caught:
                projects.update("pp", mutation, active=busy)
            assert caught.value.code == "active_operation"
        assert projects.update("pp", {"op": "rename", "name": "fine"}, active=busy).name == "fine"


def test_archived_project_leaves_default_list(tmp_path):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        projects.create(id="pp", name="P", materials_root=".", implementations=[], path=str(tmp_path / "wc"))
        projects.update("pp", {"op": "archive", "reason": "done"})
        assert projects.list() == () and [p.id for p in projects.list(archived=True)] == ["pp"]
        with pytest.raises(ProjectError) as caught:
            projects.ready("pp")
        assert caught.value.code == "archived"


def test_default_implementation_and_ambiguity(tmp_path):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        project = projects.create(id="pp", name="P", materials_root=".", implementations=IMPL + [{"id": "bb", "place": "bb"}], path=str(tmp_path / "wc"))
        assert projects.implementation(project, "bb").id == "bb"
        with pytest.raises(ProjectError) as caught:
            projects.implementation(project, None)
        assert caught.value.code == "ambiguous_implementation"


@pytest.mark.parametrize("root", ["../x", "/abs", "a/../b", ""])
def test_bad_materials_root(tmp_path, root):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        with pytest.raises(ProjectError) as caught:
            projects.create(id="pp", name="P", materials_root=root, implementations=[], path=str(tmp_path / "wc"))
        assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("name", [None, ""])
def test_rename_implementation_rejects_empty_name(tmp_path, name):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        projects.create(id="pp", name="P", materials_root=".", implementations=IMPL, path=str(tmp_path / "wc"))
        with pytest.raises(ProjectError) as caught:
            projects.update("pp", {"op": "rename_implementation", "implementation": "agent", "name": name})
        assert caught.value.code == "invalid_request"


def test_archive_and_archive_implementation_reject_non_string_reason(tmp_path):
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        projects.create(id="pp", name="P", materials_root=".", implementations=IMPL, path=str(tmp_path / "wc"))
        with pytest.raises(ProjectError) as caught:
            projects.update("pp", {"op": "archive", "reason": {"x": 1}})
        assert caught.value.code == "invalid_request"
        with pytest.raises(ProjectError) as caught:
            projects.update("pp", {"op": "archive_implementation", "implementation": "agent", "reason": {"x": 1}})
        assert caught.value.code == "invalid_request"


def test_reconcile_after_rename_but_before_ready_derives_source(tmp_path):
    team = _bare_repo(tmp_path)
    home = _home(tmp_path)
    project_id = "acq"
    branch = f"lab/{project_id}"
    with Projects.open(home) as projects:
        destination = projects.home / "clones" / project_id
        staging = projects.home / "clones" / f".staging-{project_id}-test"
        real_source = clone_repository(str(team), None, staging, branch)
        staging.rename(destination)
        # Simulate a crash after the rename but before the final ready write: the
        # registry still holds the "creating" record, with no source recorded yet.
        pending = Project(project_id, "Acq", "creating", None, utc_now(), None, 1,
                          WorkingCopy(str(destination), True, True, None), "lab", (), tuple())
        projects.store.put_project(pending)
    with Projects.open(home) as reopened:
        project = reopened.get(project_id)
        assert project.state == "ready" and project.failure is None
        assert project.working_copy.source["branch"] == branch
        assert project.working_copy.source["repo"]
        assert project.working_copy.source["base_revision"] == real_source["base_revision"]
        assert not list((home / "clones").glob(".staging-*"))


# --- Final-review fix wave ---

def _git_env():
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t"}


def test_clone_with_a_non_default_branch_rev(tmp_path):
    """I6: ``--rev develop`` names a branch that exists only as ``origin/develop`` in the clone."""
    team = _bare_repo(tmp_path)
    for argv in (["git", "switch", "-q", "-c", "develop"], ["git", "commit", "-q", "--allow-empty", "-m", "dev"],
                 ["git", "switch", "-q", "main"]):
        subprocess.run(argv, cwd=team, check=True, env=_git_env())
    develop = subprocess.run(["git", "rev-parse", "develop"], cwd=team, capture_output=True, text=True,
                             check=True).stdout.strip()
    main = subprocess.run(["git", "rev-parse", "main"], cwd=team, capture_output=True, text=True,
                          check=True).stdout.strip()
    assert develop != main
    with Projects.open(_home(tmp_path)) as projects:
        project = projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                                  clone={"repo": str(team), "rev": "develop"})
        clone = projects.home / "clones" / "acq"
        assert project.state == "ready" and project.working_copy.source["base_revision"] == develop
        head = subprocess.run(["git", "-C", str(clone), "branch", "--show-current"], capture_output=True,
                              text=True).stdout.strip()
        assert head == "lab/acq"
        # a commit id still resolves locally, as before
        pinned = projects.create(id="pin", name="Pin", materials_root="lab", implementations=IMPL,
                                 clone={"repo": str(team), "rev": main})
        assert pinned.working_copy.source["base_revision"] == main


def test_clone_passes_the_repo_after_double_dash_and_never_prompts_ssh(tmp_path, monkeypatch):
    """M5: the repo argument cannot be read as an option, and ssh never asks for a password."""
    from evolution_lab.projects import clone as clone_module
    calls = []

    def run(argv, **kwargs):
        if argv[1] == "config":  # the core.sshCommand probe: none configured
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout="abc\n", stderr="")
    monkeypatch.setattr(clone_module.subprocess, "run", run)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    clone_repository("--upload-pack=touch /tmp/x", None, tmp_path / "dest", "lab/pp")
    argv, env = calls[0]
    assert argv[:4] == ["git", "clone", "--quiet", "--"] and argv[4] == "--upload-pack=touch /tmp/x"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes" and env["GIT_TERMINAL_PROMPT"] == "0"
    calls.clear()
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    clone_repository("git@example.invalid:team/repo.git", None, tmp_path / "dest", "lab/pp")
    assert all(env["GIT_SSH_COMMAND"] == "ssh -i key" for _, env in calls)


@pytest.mark.parametrize("name", ["", 5, None, ["x"]])
def test_implementation_name_when_given_is_a_non_empty_string(tmp_path, name):
    """M7: an implementation's ``name`` is a non-empty string when the spec carries one."""
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as projects:
        with pytest.raises(ProjectError) as caught:
            projects.create(id="pp", name="P", materials_root=".", path=str(tmp_path / "wc"),
                            implementations=[{"id": "agent", "place": "agent", "name": name}])
        assert caught.value.code == "invalid_request"
        projects.create(id="pp", name="P", materials_root=".", implementations=IMPL, path=str(tmp_path / "wc"))
        with pytest.raises(ProjectError) as caught:
            projects.update("pp", {"op": "add_implementation", "implementation": {"id": "bb", "place": "bb", "name": name}})
        assert caught.value.code == "invalid_request"
        assert projects.get("pp").implementations[0].name == "agent"  # no name: the id


def test_a_reader_opens_and_lists_while_a_writer_holds_the_home(tmp_path):
    """M11(c): the home lock is for writers only."""
    (tmp_path / "wc").mkdir()
    with Projects.open(_home(tmp_path)) as writer:
        writer.create(id="pp", name="P", materials_root=".", implementations=IMPL, path=str(tmp_path / "wc"))
        with pytest.raises(ProjectError) as caught:
            Projects.open(_home(tmp_path))
        assert caught.value.code == "home_busy"
        with Projects.open(_home(tmp_path), read_only=True) as reader:
            assert [project.id for project in reader.list()] == ["pp"]
            assert reader.get("pp").name == "P"


# --- Codex branch review round 1: clone wrapper git semantics -------------------------------

def _spy_clone_envs(monkeypatch):
    from evolution_lab.projects import clone as clone_module
    real_run, envs = subprocess.run, []

    def run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            envs.append(kwargs["env"])
        return real_run(argv, **kwargs)
    monkeypatch.setattr(clone_module.subprocess, "run", run)
    return envs


def test_clone_keeps_a_configured_core_ssh_command(tmp_path, monkeypatch):
    """R-b: ``git config core.sshCommand`` (global/system) is the user's transport, so BatchMode
    is injected only when neither the environment nor git config names an ssh command; a local
    repo config around the service's directory does not count (the clone never reads it)."""
    team = _bare_repo(tmp_path)
    envs = _spy_clone_envs(monkeypatch)
    for name in ("GIT_SSH_COMMAND", "GIT_SSH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    configured = tmp_path / "configured.gitconfig"
    configured.write_text("[core]\n\tsshCommand = ssh -i /keys/team\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(configured))
    clone_repository(str(team), None, tmp_path / "with-config", "lab/a")
    assert "GIT_SSH_COMMAND" not in envs[-1]

    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    subprocess.run(["git", "-C", str(team), "config", "core.sshCommand", "ssh -i /keys/local"], check=True)
    monkeypatch.chdir(team)
    clone_repository(str(team), None, tmp_path / "without-config", "lab/b")
    assert envs[-1]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def _rev(cwd, rev):
    return subprocess.run(["git", "rev-parse", rev], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_clone_rev_prefers_the_remote_branch_then_a_tag_and_never_tracks(tmp_path):
    """R-c: ``--rev R`` probes ``refs/remotes/origin/R``, then ``refs/tags/R``, then the bare
    commit-ish, so a hex-named branch is not read as a commit prefix; ``lab/<id>`` has no upstream."""
    team = _bare_repo(tmp_path)
    main = _rev(team, "main")
    hexname = main[:7]  # a branch named like a prefix of main's commit id
    for argv in (["git", "switch", "-q", "-c", hexname], ["git", "commit", "-q", "--allow-empty", "-m", "hex"],
                 ["git", "switch", "-q", "-c", "develop"], ["git", "commit", "-q", "--allow-empty", "-m", "dev"],
                 ["git", "switch", "-q", "main"], ["git", "tag", "v1", f"refs/heads/{hexname}"]):
        subprocess.run(argv, cwd=team, check=True, env=_git_env())
    branch_head, develop = _rev(team, f"refs/heads/{hexname}"), _rev(team, "develop")
    assert len({main, branch_head, develop}) == 3
    with Projects.open(_home(tmp_path)) as projects:
        hexed = projects.create(id="hexed", name="H", materials_root="lab", implementations=IMPL,
                                clone={"repo": str(team), "rev": hexname})
        assert hexed.working_copy.source["base_revision"] == branch_head
        tagged = projects.create(id="tagged", name="T", materials_root="lab", implementations=IMPL,
                                 clone={"repo": str(team), "rev": "v1"})
        assert tagged.working_copy.source["base_revision"] == branch_head
        pinned = projects.create(id="pinned", name="P", materials_root="lab", implementations=IMPL,
                                 clone={"repo": str(team), "rev": main})
        assert pinned.working_copy.source["base_revision"] == main
        dev = projects.create(id="dev", name="D", materials_root="lab", implementations=IMPL,
                              clone={"repo": str(team), "rev": "develop"})
        assert dev.working_copy.source["base_revision"] == develop
        for project_id in ("hexed", "tagged", "pinned", "dev"):
            clone = projects.home / "clones" / project_id
            upstream = subprocess.run(["git", "-C", str(clone), "rev-parse", "--abbrev-ref",
                                       f"lab/{project_id}@{{upstream}}"], capture_output=True, text=True)
            assert upstream.returncode != 0, (project_id, upstream.stdout)


def _project_warnings(caplog):
    import logging
    return [record for record in caplog.records
            if record.name == "evolution_lab.projects" and record.levelno == logging.WARNING]


def test_a_failing_clone_logs_the_git_stderr_tail(tmp_path, caplog):
    """§3.2 diagnostics: the error envelope stays closed (``clone_failed``), git's stderr tail
    goes to the service log as a WARNING."""
    import logging
    caplog.set_level(logging.WARNING, logger="evolution_lab.projects")
    with pytest.raises(ProjectError) as caught:
        clone_repository(str(tmp_path / "missing"), None, tmp_path / "dest", "lab/x")
    assert caught.value.code == "clone_failed"
    records = _project_warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith("git clone exited 128") and str(tmp_path / "missing") in message  # git's own words


def test_the_logged_stderr_tail_is_20_lines_with_credentials_redacted(tmp_path, monkeypatch, caplog):
    """The WARNING carries the last 20 stderr lines; URL credentials never reach the log, whether
    git echoes them as ``scheme://user:secret@host`` or bare ``user:secret@host``."""
    import logging
    from evolution_lab.projects import clone as clone_module
    caplog.set_level(logging.WARNING, logger="evolution_lab.projects")
    stderr = "\n".join([f"early {i:02d} https://lab:s3cret@old.example.invalid/x" for i in range(10)]
                       + [f"line {i:02d}" for i in range(17)]
                       + ["fatal: unable to access 'https://lab:s3cret@git.example.invalid/team.git/': 403",
                          "fatal: unable to look up lab:s3cret@git.example.invalid (port 9418)",
                          "warning: ssh://deploy@git.example.invalid/team.git refused"]) + "\n"

    def run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr=stderr)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
    monkeypatch.setattr(clone_module.subprocess, "run", run)
    with pytest.raises(ProjectError) as caught:
        clone_repository("https://lab:s3cret@git.example.invalid/team.git", None, tmp_path / "dest", "lab/x")
    assert caught.value.code == "clone_failed"
    records = _project_warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "line 00" in message and "line 16" in message and "early" not in message
    assert "'https://***@git.example.invalid/team.git/': 403" in message
    assert "unable to look up ***@git.example.invalid (port 9418)" in message
    assert "ssh://***@git.example.invalid/team.git refused" in message
    assert "s3cret" not in caplog.text and "s3cret" not in message


# --- Codex fix wave follow-up: a rev never reaches git as an option -------------------------

def _spy_git_calls(monkeypatch):
    from evolution_lab.projects import clone as clone_module
    real_run, calls = subprocess.run, []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)
    monkeypatch.setattr(clone_module.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("rev", ["-", "--orphan", "--upload-pack=touch x", "-b"])
def test_a_rev_starting_with_a_dash_is_refused_before_any_git_call(tmp_path, monkeypatch, rev):
    """Follow-up 2: no revision starts with ``-``; such a ``--rev`` is ``invalid_request`` before
    git runs and before a ``creating`` record is written (the request is malformed)."""
    team = _bare_repo(tmp_path)
    calls = _spy_git_calls(monkeypatch)
    with Projects.open(_home(tmp_path)) as projects:
        with pytest.raises(ProjectError) as caught:
            projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                            clone={"repo": str(team), "rev": rev})
        assert caught.value.code == "invalid_request"
        assert projects.list(archived=True) == ()
    with pytest.raises(ProjectError) as caught:
        clone_repository(str(team), rev, tmp_path / "direct", "lab/x")
    assert caught.value.code == "invalid_request"
    assert calls == [] and not (tmp_path / "direct").exists()


def test_checkout_takes_the_start_point_without_end_of_options(tmp_path, monkeypatch):
    """Git 2.34 and 2.43 (Ubuntu 22.04, 24.04) read ``--end-of-options`` in checkout as the start point
    and fail every clone; the start point is a probed ``refs/`` name, never option-like."""
    team = _bare_repo(tmp_path)
    calls = _spy_git_calls(monkeypatch)
    clone_repository(str(team), "main", tmp_path / "dest", "lab/x")
    checkout = next(argv for argv in calls if argv[1] == "checkout")
    assert "--end-of-options" not in checkout
    assert checkout[-1] == "refs/remotes/origin/main"


# --- Codex fix wave follow-up: clone credentials are never stored or returned -------------

_SECRET_URL = "https://lab:s3cr3t-tok@git.example.invalid/team/repo.git"


def _registry_dump(home):
    import sqlite3
    from evolution_lab.projects.home import REGISTRY_NAME
    with sqlite3.connect(home / REGISTRY_NAME) as database:
        return "\n".join(database.iterdump())


def test_a_clone_url_with_credentials_is_stored_and_returned_redacted(tmp_path):
    """Follow-up 3: the real URL serves only the clone call; ``working_copy.source.repo`` in the
    registry and in ``lab-project/1`` is the redacted form."""
    import json
    team = _bare_repo(tmp_path)
    seen = []

    def runner(repo, rev, destination, branch):  # clones the local team repo, reports the URL as given
        seen.append(repo)
        return {**clone_repository(str(team), rev, destination, branch), "repo": repo}
    home = _home(tmp_path)
    with Projects.open(home, clone_runner=runner) as projects:
        project = projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                                  clone={"repo": _SECRET_URL, "rev": None})
        assert seen == [_SECRET_URL]  # the clone itself gets the real URL
        assert project.working_copy.source["repo"] == "https://***@git.example.invalid/team/repo.git"
        assert "s3cr3t" not in json.dumps(project.to_payload())
        assert "s3cr3t" not in json.dumps(projects.get("acq").to_payload())
    assert "s3cr3t" not in _registry_dump(home)


def test_reconciling_a_clone_with_credentials_stores_the_redacted_url(tmp_path):
    """Follow-up 3, the crash path: ``remote.origin.url`` read back from the clone is redacted too."""
    team = _bare_repo(tmp_path)
    home = _home(tmp_path)
    with Projects.open(home) as projects:
        destination = projects.home / "clones" / "acq"
        clone_repository(str(team), None, destination, "lab/acq")
        subprocess.run(["git", "-C", str(destination), "remote", "set-url", "origin", _SECRET_URL], check=True)
        projects.store.put_project(Project("acq", "Acq", "creating", None, utc_now(), None, 1,
                                           WorkingCopy(str(destination), True, True, None), "lab", (), ()))
    with Projects.open(home) as reopened:
        project = reopened.get("acq")
        assert project.state == "ready"
        assert project.working_copy.source["repo"] == "https://***@git.example.invalid/team/repo.git"
    assert "s3cr3t" not in _registry_dump(home)


# --- Codex branch review round 2 ---

def _failing_rename(monkeypatch):
    from pathlib import Path
    real = Path.rename

    def rename(self, target):
        if self.name.startswith(".staging-"):
            raise PermissionError(13, "Permission denied")
        return real(self, target)
    monkeypatch.setattr(Path, "rename", rename)


def _failing_clone(repo, rev, destination, branch):
    destination.mkdir()
    (destination / "partial").write_text("x")
    raise OSError(5, "Input/output error")


@pytest.mark.parametrize("failure", ["rename", "clone"])
def test_a_filesystem_error_during_clone_publication_is_failed_clone_failed(tmp_path, monkeypatch, failure):
    """ADR-0017 p. 9 (codex r2): an OSError from the clone or the staging rename takes the
    same failed transition as a git failure — staging removed, the record ``failed``
    (``clone_failed``) in this attempt, not ``creating`` until a restart reconciles it."""
    team = _bare_repo(tmp_path)
    runner = {}
    if failure == "rename":
        _failing_rename(monkeypatch)
    else:
        runner["clone_runner"] = _failing_clone
    with Projects.open(_home(tmp_path), **runner) as projects:
        with pytest.raises(ProjectError) as caught:
            projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                            clone={"repo": str(team), "rev": None})
        assert caught.value.code == "clone_failed"
        project = projects.get("acq")
        assert project.state == "failed" and project.failure == "clone_failed"
        assert not list((projects.home / "clones").glob(".staging-*"))
        assert not (projects.home / "clones" / "acq").exists()


def test_clone_ignores_inherited_git_repository_selectors(tmp_path, monkeypatch):
    """codex r1 of the branch (PDR-0018): `--clone` with GIT_DIR/GIT_WORK_TREE in the service environment
    clones into its destination and leaves the repository those variables name untouched."""
    team = _bare_repo(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=sentinel, check=True)
    before = {p.relative_to(sentinel).as_posix(): (p.read_bytes() if p.is_file() else None)
              for p in sorted(sentinel.rglob("*"))}
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(sentinel))
    source = clone_repository(str(team), None, tmp_path / "dest", "lab/acq")
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    after = {p.relative_to(sentinel).as_posix(): (p.read_bytes() if p.is_file() else None)
             for p in sorted(sentinel.rglob("*"))}
    assert after == before
    assert (tmp_path / "dest" / "agent" / "a.py").read_text() == "A = 1\n"
    assert source["branch"] == "lab/acq" and len(source["base_revision"]) == 40
