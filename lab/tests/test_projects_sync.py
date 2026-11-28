import os, subprocess
from pathlib import Path
import pytest
from evolution_lab.projects import sync
from evolution_lab.projects.clone import clone_repository
from evolution_lab.projects.errors import ProjectError

AUTHOR = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    for name in AUTHOR:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = T\n\temail = t@t\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return config


def git(cwd, *argv) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, path, text, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def pair(tmp_path):
    """A source repo ``team`` (branch main, one commit) and Lab's clone of it on ``lab/acq``."""
    team = tmp_path / "team"
    team.mkdir()
    git(team, "init", "-q", "-b", "main")
    base = commit(team, "agent/a.py", "A = 1\n", "init")
    clone = tmp_path / "clones" / "acq"
    clone.parent.mkdir()
    source = clone_repository(str(team), None, clone, "lab/acq")
    assert source["base_revision"] == base
    return team, clone, base


def test_inspect_up_to_date(pair):
    team, clone, base = pair
    sync.fetch(clone)
    value = sync.inspect(clone, project_id="acq", base_revision=base, rev=None)
    assert value["schema"] == "lab-project-sync-check/1" and value["mode"] == "up_to_date"
    assert value["upstream"] == {"sha": base, "ref": "origin/main"} and value["commits"] == [] and value["dirty"] == []


def test_inspect_fast_forward_lists_commits_and_files(pair):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "Новый скилл")
    git(team, "mv", "agent/a.py", "agent/b.py"); git(team, "commit", "-q", "-m", "rename")
    head = git(team, "rev-parse", "HEAD")
    sync.fetch(clone)
    value = sync.inspect(clone, project_id="acq", base_revision=base, rev=None)
    assert value["mode"] == "fast_forward" and value["upstream"]["sha"] == head
    assert [c["subject"] for c in value["commits"]] == ["rename", "Новый скилл"]  # newest first
    assert {"path": "skills/new.md", "status": "A"} in value["files"]
    assert {"path": "agent/b.py", "status": "R", "old_path": "agent/a.py"} in value["files"]
    assert value["commits_truncated"] is False and value["files_truncated"] is False


def test_inspect_merge_when_lab_branch_has_own_commits(pair):
    team, clone, base = pair
    commit(team, "skills/new.md", "x\n", "upstream")
    commit(clone, "lab/package.json", "{}\n", "assistant")
    sync.fetch(clone)
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev=None)["mode"] == "merge"


def test_inspect_rev_branch_tag_commit_and_missing(pair):
    team, clone, base = pair
    git(team, "switch", "-q", "-c", "develop"); dev = commit(team, "d.txt", "d\n", "dev")
    git(team, "tag", "v1", dev); git(team, "switch", "-q", "main")
    sync.fetch(clone)
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev="develop")["upstream"] == {"sha": dev, "ref": "origin/develop"}
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev="v1")["upstream"] == {"sha": dev, "ref": "tag:v1"}
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev=dev)["upstream"] == {"sha": dev, "ref": None}
    with pytest.raises(ProjectError) as error:
        sync.inspect(clone, project_id="acq", base_revision=base, rev="nope")
    assert error.value.code == "revision_not_found"


def test_inspect_rev_not_reachable_from_the_source_is_revision_not_found(pair):
    team, clone, base = pair
    sync.fetch(clone)
    local = commit(clone, "lab/x.txt", "x\n", "assistant")
    for rev in ("lab/acq", "refs/heads/lab/acq", local):
        with pytest.raises(ProjectError) as error:
            sync.inspect(clone, project_id="acq", base_revision=base, rev=rev)
        assert error.value.code == "revision_not_found" and "not a commit of the source" in error.value.message


def test_apply_of_a_clone_only_commit_is_upstream_changed(pair):
    team, clone, base = pair
    sync.fetch(clone)
    local = commit(clone, "lab/x.txt", "x\n", "assistant")
    head = git(clone, "rev-parse", "HEAD")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, local)
    assert error.value.code == "upstream_changed" and git(clone, "rev-parse", "HEAD") == head


def test_inspect_without_origin_head_needs_rev(pair):
    team, clone, base = pair
    git(clone, "remote", "set-head", "origin", "--delete")
    git(clone, "config", "remote.origin.followRemoteHEAD", "never")  # git >= 2.48 would re-create origin/HEAD on fetch
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        sync.inspect(clone, project_id="acq", base_revision=base, rev=None)
    assert error.value.code == "revision_not_found"


def test_inspect_history_diverged(pair):
    team, clone, base = pair
    git(team, "commit", "-q", "--amend", "-m", "rewritten")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        sync.inspect(clone, project_id="acq", base_revision=base, rev=None)
    assert error.value.code == "history_diverged"


def test_inspect_reports_dirty_tracked_files(pair):
    team, clone, base = pair
    (clone / "agent" / "a.py").write_text("A = 2\n")
    sync.fetch(clone)
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev=None)["dirty"] == [{"path": "agent/a.py"}]


def test_inspect_reports_merge_in_progress(pair):
    team, clone, base = pair
    commit(team, "agent/a.py", "A = 'team'\n", "team")
    commit(clone, "agent/a.py", "A = 'lab'\n", "lab")
    sync.fetch(clone)
    subprocess.run(["git", "merge", "--no-edit", "origin/main"], cwd=clone, capture_output=True)  # leaves MERGE_HEAD
    dirty = sync.inspect(clone, project_id="acq", base_revision=base, rev=None)["dirty"]
    assert {"path": None, "state": "merge_in_progress"} in dirty


def test_inspect_truncates_commits(pair, monkeypatch):
    team, clone, base = pair
    monkeypatch.setattr(sync, "COMMITS_LIMIT", 2)
    for n in range(3):
        commit(team, f"f{n}.txt", "x\n", f"c{n}")
    sync.fetch(clone)
    value = sync.inspect(clone, project_id="acq", base_revision=base, rev=None)
    assert len(value["commits"]) == 2 and value["commits_truncated"] is True


def test_fetch_failure_is_sync_fetch_failed_without_credentials(pair, caplog):
    team, clone, base = pair
    git(clone, "remote", "set-url", "origin", "https://user:secret@127.0.0.1:9/none.git")
    with pytest.raises(ProjectError) as error:
        sync.fetch(clone)
    assert error.value.code == "sync_fetch_failed" and "secret" not in error.value.message
    assert "secret" not in caplog.text


def apply(clone, base, upstream):
    return sync.integrate(clone, branch="lab/acq", base_revision=base, upstream=upstream)


def test_integrate_fast_forward(pair):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    sync.fetch(clone)
    assert apply(clone, base, new) == {"mode": "fast_forward", "merged": new, "head": new}
    assert (clone / "skills" / "new.md").read_text() == "x\n"


def test_integrate_merge_keeps_assistant_commits_with_lab_identity(pair):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    mine = commit(clone, "lab/package.json", "{}\n", "assistant")
    sync.fetch(clone)
    result = apply(clone, base, new)
    assert result["mode"] == "merge" and result["merged"] == new
    assert git(clone, "rev-parse", "HEAD^1") == mine and git(clone, "rev-parse", "HEAD^2") == new
    assert git(clone, "log", "-1", "--format=%an <%ae>") == "Evolution Lab <lab@evolution-lab.invalid>"
    assert (clone / "skills" / "new.md").exists() and (clone / "lab" / "package.json").exists()


def test_integrate_up_to_date_does_nothing(pair):
    team, clone, base = pair
    sync.fetch(clone)
    head = git(clone, "rev-parse", "HEAD")
    assert apply(clone, base, base) == {"mode": "up_to_date", "merged": base, "head": head}


def test_integrate_conflict_is_refused_and_copy_unchanged(pair):
    team, clone, base = pair
    new = commit(team, "agent/a.py", "A = 'team'\n", "team edit")
    mine = commit(clone, "agent/a.py", "A = 'lab'\n", "lab edit")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_conflict" and "agent/a.py" in error.value.message
    assert git(clone, "rev-parse", "HEAD") == mine and git(clone, "status", "--porcelain") == ""
    assert (clone / "agent" / "a.py").read_text() == "A = 'lab'\n"


def test_integrate_refuses_dirty_branch_mismatch_and_unknown_upstream(pair):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    sync.fetch(clone)
    (clone / "agent" / "a.py").write_text("A = 2\n")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "working_copy_dirty" and "agent/a.py" in error.value.message
    git(clone, "checkout", "-q", "--", "agent/a.py")
    git(clone, "switch", "-q", "-c", "other")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "branch_mismatch"
    git(clone, "switch", "-q", "lab/acq")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, "f" * 40)
    assert error.value.code == "upstream_changed"


def test_untracked_aside_is_fine_but_overwrite_of_untracked_or_ignored_is_a_conflict(pair):
    team, clone, base = pair
    (clone / "notes.txt").write_text("mine\n")
    (clone / ".git" / "info" / "exclude").write_text("corpus.json\n")
    (clone / "corpus.json").write_text("local data\n")
    new = commit(team, "corpus.json", "team data\n", "track corpus")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_conflict" and "corpus.json" in error.value.message
    assert (clone / "corpus.json").read_text() == "local data\n" and (clone / "notes.txt").exists()


def test_user_git_config_does_not_change_the_outcome(pair, isolated_git, monkeypatch):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    commit(clone, "lab/package.json", "{}\n", "assistant")
    sync.fetch(clone)
    # The hostile configuration is installed only now: the fixture commits above must not be signed or hooked.
    hooks = clone.parent / "hooks"; hooks.mkdir()
    (hooks / "pre-merge-commit").write_text("#!/bin/sh\nexit 1\n"); (hooks / "pre-merge-commit").chmod(0o755)
    isolated_git.write_text(isolated_git.read_text() + f"[merge]\n\tff = only\n[commit]\n\tgpgSign = true\n"
                            f"[core]\n\thooksPath = {hooks}\n")
    git(clone, "config", "branch.lab/acq.mergeOptions", "-s ours")
    isolated_git.write_text(isolated_git.read_text() + "[merge]\n\tverifySignatures = true\n[rerere]\n\tenabled = true\n")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone"); monkeypatch.setenv("GIT_AUTHOR_EMAIL", "s@s")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Someone"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "s@s")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
    assert apply(clone, base, new)["mode"] == "merge"
    assert (clone / "skills" / "new.md").read_text() == "x\n"  # -s ours would have dropped it
    assert git(clone, "log", "-1", "--format=%an|%cn|%ad", "--date=short") == "Evolution Lab|Evolution Lab|" + \
        git(clone, "log", "-1", "--format=%cd", "--date=short")


def test_history_diverged_on_apply(pair):
    team, clone, base = pair
    git(team, "commit", "-q", "--amend", "-m", "rewritten")
    rewritten = git(team, "rev-parse", "HEAD")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, rewritten)
    assert error.value.code == "history_diverged"


def test_ignored_file_is_a_conflict_also_in_a_true_merge(pair):
    team, clone, base = pair
    (clone / ".git" / "info" / "exclude").write_text("corpus.json\n")
    (clone / "corpus.json").write_text("local data\n")
    commit(clone, "lab/package.json", "{}\n", "assistant")
    new = commit(team, "corpus.json", "team data\n", "track corpus")
    sync.fetch(clone)
    head = git(clone, "rev-parse", "HEAD")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_conflict" and "corpus.json" in error.value.message
    assert (clone / "corpus.json").read_text() == "local data\n" and git(clone, "rev-parse", "HEAD") == head


def test_ignored_parent_collisions_are_conflicts(pair):
    team, clone, base = pair
    (clone / ".git" / "info" / "exclude").write_text("data\ncache/\n")
    (clone / "data").write_text("ignored file\n")           # upstream needs data/ as a directory
    (clone / "cache").mkdir(); (clone / "cache" / "x").write_text("x\n")  # upstream adds a file named cache
    commit(clone, "lab/package.json", "{}\n", "assistant")
    new = commit(team, "data/set.csv", "1\n", "dir over ignored file")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_conflict" and "data/set.csv" in error.value.message
    newer = commit(team, "cache", "file\n", "file over ignored dir")
    git(team, "rm", "-q", "-r", "data"); git(team, "commit", "-q", "-m", "drop data")
    newer = git(team, "rev-parse", "HEAD")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, newer)
    assert error.value.code == "merge_conflict" and "cache" in error.value.message
    assert (clone / "cache" / "x").exists() and (clone / "data").read_text() == "ignored file\n"


def test_dirty_with_staged_rename_lists_new_path_only(pair):
    team, clone, base = pair
    git(clone, "mv", "agent/a.py", "agent/b.py")
    sync.fetch(clone)
    dirty = sync.inspect(clone, project_id="acq", base_revision=base, rev=None)["dirty"]
    assert dirty == [{"path": "agent/b.py"}]


def test_long_path_lists_stay_within_the_detail_budget(pair):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    sync.fetch(clone)
    names = [f"d{n}/" + "p" * 150 + ".txt" for n in range(50)]
    for name in names:
        commit(clone, name, "x\n", f"add {name[:3]}")
    for name in names:
        (clone / name).write_text("changed\n")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "working_copy_dirty" and len(error.value.detail) <= 4096
    assert "more)" in error.value.detail and names[0] in error.value.detail


def test_fetch_follows_a_moved_tag(pair):
    team, clone, base = pair
    git(team, "tag", "v1")
    sync.fetch(clone)
    moved = commit(team, "n.txt", "n\n", "next")
    git(team, "tag", "-f", "v1", moved)
    sync.fetch(clone)
    assert git(clone, "rev-parse", "v1^{commit}") == moved


def test_ignored_file_with_a_leading_space_is_a_conflict(pair):
    team, clone, base = pair
    (clone / ".git" / "info" / "exclude").write_text("\\ corpus.json\n")
    (clone / " corpus.json").write_text("local data\n")
    commit(clone, "lab/package.json", "{}\n", "assistant")  # a true merge: ort ignores --no-overwrite-ignore
    (team / " 0.txt").write_text("0\n")  # sorts first, so the ignored path is not the first of the diff output
    new = commit(team, " corpus.json", "team data\n", "track corpus")
    sync.fetch(clone)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_conflict"
    assert (clone / " corpus.json").read_text() == "local data\n"


def _checked_then_source_branch_deleted(team, clone, base):
    """C lives only on a temporary source branch when it is checked; the branch is then deleted and pruned."""
    git(team, "switch", "-q", "-c", "tmp")
    c = commit(team, "skills/c.md", "c\n", "on tmp")
    git(team, "switch", "-q", "main")
    sync.fetch(clone)
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev="tmp")["upstream"]["sha"] == c
    git(team, "branch", "-q", "-D", "tmp")
    sync.fetch(clone)
    assert sync.inspect(clone, project_id="acq", base_revision=base, rev=None)["upstream"]["sha"] == base
    return c


def test_checked_commit_stays_appliable_after_its_source_branch_is_deleted(pair):
    team, clone, base = pair
    c = _checked_then_source_branch_deleted(team, clone, base)
    assert apply(clone, base, c)["merged"] == c
    assert (clone / "skills" / "c.md").read_text() == "c\n"


def test_retry_of_an_applied_commit_after_its_source_branch_is_deleted_is_up_to_date(pair):
    team, clone, base = pair
    c = _checked_then_source_branch_deleted(team, clone, base)
    apply(clone, base, c)
    assert apply(clone, base, c)["mode"] == "up_to_date"  # the sync_not_recorded retry


def test_assistant_commit_is_still_upstream_changed_after_a_checked_one(pair):
    team, clone, base = pair
    c = _checked_then_source_branch_deleted(team, clone, base)
    local = commit(clone, "lab/x.txt", "x\n", "assistant")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, local)
    assert error.value.code == "upstream_changed"


def _conflicting(pair):
    team, clone, base = pair
    new = commit(team, "agent/a.py", "A = 'team'\n", "team edit")
    mine = commit(clone, "agent/a.py", "A = 'lab'\n", "lab edit")
    sync.fetch(clone)
    return clone, base, new, mine


def _assert_untouched(clone, mine):
    assert git(clone, "rev-parse", "HEAD") == mine and git(clone, "status", "--porcelain") == ""
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert (clone / "agent" / "a.py").read_text() == "A = 'lab'\n"


def test_inherited_global_merge_driver_cannot_swallow_a_conflict(pair, isolated_git):
    """Codex r1 #1: ``merge.default`` + a driver in the user's config would 'merge' keeping local content."""
    clone, base, new, mine = _conflicting(pair)
    isolated_git.write_text(isolated_git.read_text() + "[merge]\n\tdefault = keep-local\n[merge \"keep-local\"]\n\tdriver = true\n")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_conflict" and "agent/a.py" in error.value.message
    _assert_untouched(clone, mine)


@pytest.mark.parametrize("key", ["merge.keep-local.driver", "merge.default"])
def test_merge_driver_in_the_working_copy_config_is_refused_before_the_merge(pair, key):
    clone, base, new, mine = _conflicting(pair)
    git(clone, "config", key, "true" if key.endswith("driver") else "keep-local")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "custom merge driver configured in the working copy" in error.value.message
    _assert_untouched(clone, mine)


def test_merge_calls_run_without_user_and_system_git_config(pair, monkeypatch):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    commit(clone, "lab/package.json", "{}\n", "assistant")
    sync.fetch(clone)
    seen = []
    real = subprocess.run

    def spy(argv, **kwargs):
        if "merge" in argv and "-c" in argv or argv[-2:] == ["merge", "--abort"]:
            seen.append(kwargs["env"])
        return real(argv, **kwargs)
    monkeypatch.setattr(sync.subprocess, "run", spy)
    apply(clone, base, new)
    assert seen and all(e["GIT_CONFIG_GLOBAL"] == "/dev/null" and e["GIT_CONFIG_NOSYSTEM"] == "1" for e in seen)


def _fail_call(monkeypatch, matches, *, after_real=True):
    """Make the matching ``_run`` call raise ProjectError (a timeout looks like this); after_real runs git first."""
    real = sync._run

    def run(argv, clone, code, **kwargs):
        if matches(argv):
            if after_real:
                real(argv, clone, code, **kwargs)
            raise ProjectError(code)
        return real(argv, clone, code, **kwargs)
    monkeypatch.setattr(sync, "_run", run)


def _is_merge(argv):
    return "merge" in argv and "--no-overwrite-ignore" in argv


def test_timeout_during_the_merge_restores_the_copy(pair, monkeypatch):
    clone, base, new, mine = _conflicting(pair)
    _fail_call(monkeypatch, _is_merge)  # the merge ran, left MERGE_HEAD and conflict markers, then 'timed out'
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "unfinished" not in error.value.message
    _assert_untouched(clone, mine)


def test_failure_collecting_conflicts_restores_the_copy(pair, monkeypatch):
    clone, base, new, mine = _conflicting(pair)
    _fail_call(monkeypatch, lambda argv: argv[:2] == ["diff", "--name-only"] and "--diff-filter=U" in argv, after_real=False)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "unfinished" not in error.value.message
    _assert_untouched(clone, mine)


def test_failed_abort_leaves_the_fixed_unfinished_state_detail(pair, monkeypatch):
    clone, base, new, mine = _conflicting(pair)
    _fail_call(monkeypatch, lambda argv: argv[:2] == ["merge", "--abort"], after_real=False)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "a person must repair it" in error.value.message


def test_failing_restore_check_is_the_unfinished_state_too(pair, monkeypatch):
    clone, base, new, mine = _conflicting(pair)
    calls = []
    real = sync._dirty

    def dirty(path, **kwargs):
        calls.append(1)
        if len(calls) > 1:  # the first call is the pre-merge check
            raise ProjectError("working_copy_dirty")
        return real(path, **kwargs)
    monkeypatch.setattr(sync, "_dirty", dirty)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "a person must repair it" in error.value.message


def test_merge_driver_pulled_in_through_an_include_is_refused_before_the_merge(pair):
    """Codex r2 #1: merge follows [include]; the guard must read the effective config, not only .git/config."""
    clone, base, new, mine = _conflicting(pair)
    (clone / ".git" / "merge-policy.inc").write_text("[merge]\n\tdefault = keep-local\n[merge \"keep-local\"]\n\tdriver = true\n")
    cfg = clone / ".git" / "config"
    cfg.write_text(cfg.read_text() + "[include]\n\tpath = merge-policy.inc\n")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "custom merge driver configured in the working copy" in error.value.message
    _assert_untouched(clone, mine)
    assert sync._is_ancestor(clone, base, new)


def test_failure_reading_head_after_a_successful_merge_is_sync_not_recorded(pair, monkeypatch):
    """Codex r2 #2: the merge succeeded; a failure while finalizing must not look like a failed merge."""
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    sync.fetch(clone)
    real = sync._run
    state = {"merged": False}

    def run(argv, clone_, code, **kwargs):
        if state["merged"] and argv == ["rev-parse", "HEAD"]:
            raise ProjectError(code)
        result = real(argv, clone_, code, **kwargs)
        if _is_merge(argv) and result.returncode == 0:
            state["merged"] = True
        return result
    monkeypatch.setattr(sync, "_run", run)
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "sync_not_recorded"
    assert git(clone, "rev-parse", "HEAD") == new


ROT = "[filter \"rot\"]\n\tclean = tr a-z n-za-m\n\tsmudge = tr a-z n-za-m\n\trequired = true\n"


@pytest.mark.parametrize("mode", ["fast_forward", "merge"])
def test_user_filter_drivers_survive_the_isolated_merge(tmp_path, isolated_git, mode):
    """Git LFS shape: a global ``filter.<name>`` must still smudge what the isolated merge checks out."""
    isolated_git.write_text(isolated_git.read_text() + ROT)
    team = tmp_path / "team"
    team.mkdir()
    git(team, "init", "-q", "-b", "main")
    (team / ".gitattributes").write_text("*.dat filter=rot\n")
    (team / "a.dat").write_text("hello\n")
    git(team, "add", "-A")
    git(team, "commit", "-q", "-m", "init")
    base = git(team, "rev-parse", "HEAD")
    clone = tmp_path / "clones" / "acq"
    clone.parent.mkdir()
    clone_repository(str(team), None, clone, "lab/acq")
    assert (clone / "a.dat").read_text() == "hello\n"
    if mode == "merge":
        commit(clone, "lab/package.json", "{}\n", "assistant")
    (team / "a.dat").write_text("changed\n")
    new = commit(team, "b.dat", "world\n", "upstream")
    sync.fetch(clone)
    assert apply(clone, base, new)["mode"] == mode
    assert (clone / "a.dat").read_text() == "changed\n" and (clone / "b.dat").read_text() == "world\n"
    assert sync._dirty(clone) == []


def test_filter_keys_are_the_only_user_config_passed_to_the_merge(pair, isolated_git):
    isolated_git.write_text(isolated_git.read_text() + ROT + "[merge]\n\tdefault = keep-local\n")
    env = sync._merge_env(pair[1])
    pairs = {env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs == {"filter.rot.clean": "tr a-z n-za-m", "filter.rot.smudge": "tr a-z n-za-m", "filter.rot.required": "true"}
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


@pytest.mark.parametrize("line", ["* merge=union", "*.py merge", "*.py -merge", "*.py !merge", "*.py text merge=ours"])
def test_merge_attribute_in_info_attributes_is_refused_before_the_merge(pair, line):
    clone, base, new, mine = _conflicting(pair)
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text(f"# note\n{line}\n")
    with pytest.raises(ProjectError) as error:
        apply(clone, base, new)
    assert error.value.code == "merge_failed" and "merge attribute configured in the working copy" in error.value.message
    _assert_untouched(clone, mine)


def test_info_attributes_without_merge_is_fine(pair):
    team, clone, base = pair
    new = commit(team, "skills/new.md", "x\n", "upstream")
    sync.fetch(clone)
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text("*.md text mergeable=1\n# merge=union\n")
    assert apply(clone, base, new)["mode"] == "fast_forward"


def test_failing_update_ref_in_inspect_is_a_check_path_code(pair, monkeypatch):
    team, clone, base = pair
    commit(team, "n.txt", "n\n", "next")
    sync.fetch(clone)
    _fail_call(monkeypatch, lambda argv: argv[0] == "update-ref", after_real=False)
    # the monkeypatch re-raises with the call's own code
    with pytest.raises(ProjectError) as error:
        sync.inspect(clone, project_id="acq", base_revision=base, rev=None)
    assert error.value.code == "sync_fetch_failed"
