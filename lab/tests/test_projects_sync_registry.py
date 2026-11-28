import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.models import Fact, Link, Pair, StaleReason
from evolution_lab.projects.session import ProjectSession, client_ref
from test_projects_session import FakeOwner, FakeStatus

IMPL = [{"id": "agent", "place": "agent"}]


@pytest.fixture
def registry_session(tmp_path):
    work = tmp_path / "work"
    (work / "agent").mkdir(parents=True)
    (work / "lab").mkdir()
    with Projects.open(tmp_path / "home") as projects:
        projects.create(id="pr", name="Project", materials_root="lab", implementations=IMPL, path=str(work))
        session = ProjectSession(projects, preparation=SimpleNamespace(_storage=None), owner=FakeOwner(),
                                 pair=Pair("0.0.3.dev68", "2.0.0b15"), state_root=tmp_path / "state")
        yield session


def _seed_link(session, kind, object_id, *, operation_kind=None):
    binding = {"inputs": {"digest": "sha256:" + "a" * 64, "sources": []}}
    link = Link(kind, "pr", "agent", session.pair, session.state_root, "2026-11-05T00:00:00Z", binding,
                "b" * 64, "p" * 64 if kind == "operation" else None, object_id, operation_kind, None)
    session.projects.store.insert_link(link)
    return link


def _trace(session):
    statements = []
    session.projects.store._db.set_trace_callback(statements.append)
    return statements


def _commits(statements):
    return sum(sql == "COMMIT" for sql in statements)


def _dml(statements):
    return [sql for sql in statements if sql.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_sync_three_targets_commit_together_and_unchanged_sync_writes_nothing(registry_session, existing):
    session = registry_session
    _seed_link(session, "bundle", "b" * 64)
    session.owner.targets = [(f"target-{i}", "b" * 64, client_ref("pr", "agent")) for i in range(3)]
    if existing:
        for i in range(3):
            _seed_link(session, "target", f"target-{i}")  # last_seen=None must refresh
    statements = _trace(session)
    await session.sync("pr")
    assert len(session.projects.store.links("pr", kind="target")) == 3
    assert _commits(statements) == 1
    assert len(_dml(statements)) == 3
    statements.clear()
    wal = session.projects.home / "projects.sqlite3-wal"
    before = wal.read_bytes()
    await session.sync("pr")
    assert _commits(statements) == 0
    assert _dml(statements) == []
    assert wal.read_bytes() == before


@pytest.mark.asyncio
async def test_record_exports_two_campaigns_commit_together(registry_session):
    session = registry_session
    links = [_seed_link(session, "operation", f"op-{i}", operation_kind="campaign") for i in range(2)]
    session.owner.operations = [(link.object_id, client_ref("pr", "agent")) for link in links]
    session.owner.statuses = {link.object_id: FakeStatus(link.object_id, kind="campaign") for link in links}
    session.owner.exports = {link.object_id: {"export_id": f"export-{i}"} for i, link in enumerate(links)}
    statements = _trace(session)
    await session._record_exports([link.object_id for link in links])
    assert session.projects.store.link_exports("pr") == {
        links[0].dedupe_key(): ("export-0", 1), links[1].dedupe_key(): ("export-1", 2)}
    assert _commits(statements) == 1
    assert len(_dml(statements)) == 2
    statements.clear()
    await session._record_exports([link.object_id for link in links])
    assert _commits(statements) == 0 and _dml(statements) == []


@pytest.mark.asyncio
async def test_unchanged_operation_snapshot_has_no_commit_or_dml(registry_session):
    session = registry_session
    _seed_link(session, "operation", "op-1", operation_kind="campaign")
    session.owner.operations = [("op-1", client_ref("pr", "agent"))]
    session.owner.statuses = {"op-1": FakeStatus("op-1", kind="campaign")}
    session.owner.exports = {"op-1": {"export_id": "export-1"}}
    await session.sync("pr")
    statements = _trace(session)
    await session.sync("pr")
    assert _dml(statements) == []
    assert _commits(statements) == 0


def test_status_three_new_artifact_hashes_commit_together(registry_session):
    session = registry_session
    project = session.projects.get("pr")
    root = session.projects.root_path(project)
    paths = ("one.md", "two.md", "three.md")
    for path in paths:
        (root / path).write_text(path)
    catalogue = SimpleNamespace(descriptors=({"id": "example", "steps": [{"outputs": [
        {"artifact": {"path": path, "check": "exists"}} for path in paths]}]},))
    statements = _trace(session)
    inputs = session._status_inputs(project, project.implementations[0], (), catalogue, {}, {})
    assert set(inputs.artifacts) == set(paths)
    assert _commits(statements) == 1
    assert len(_dml(statements)) == 3
    statements.clear()
    session._status_inputs(project, project.implementations[0], (), catalogue, {}, {})
    assert _commits(statements) == 0 and _dml(statements) == []


@pytest.mark.asyncio
async def test_facts_three_new_inventory_hashes_commit_together(registry_session):
    session = registry_session
    root = session.projects.root_path(session.projects.get("pr"))
    for name in ("one.md", "two.md", "three.md"):
        (root / name).write_text(name)
    statements = _trace(session)
    revision, _ = await session.facts("pr", "agent")
    assert revision.files == 3
    assert _commits(statements) == 1
    assert len(_dml(statements)) == 3
    statements.clear()
    await session.facts("pr", "agent")
    assert _commits(statements) == 0 and _dml(statements) == []


@pytest.mark.asyncio
async def test_bundle_groups_hash_measurement_before_atomic_metadata_publication(registry_session, monkeypatch):
    from evolution_lab.preparation import ProbeSource, SourceRoot
    from evolution_lab.service.codecs import BundleRequest
    session = registry_session
    project = session.projects.get("pr")
    work = Path(project.working_copy.path)
    for name in ("one.md", "two.md", "three.md"):
        (work / "lab" / name).write_text(name)
    request = BundleRequest(ProbeSource(work / "lab" / "one.md", {}), (), {}, (),
                            (SourceRoot("work", work),), ())
    monkeypatch.setattr(session, "_probe_request", lambda *args: request)

    def publish(**kwargs):
        assert not session.projects.store._db.in_transaction
        return SimpleNamespace(digest="b" * 64)
    session.preparation.create_bundle = publish
    statements = _trace(session)
    _, link = await session.bundle("pr", "agent")
    digest = link.binding["inputs"]["digest"]
    assert session.projects.store.inventory(digest) is not None
    assert session.projects.store.input_environment(digest) is not None
    assert _commits(statements) == 2  # one hash measurement; metadata + environment + link
    assert len(_dml(statements)) == 6


@pytest.mark.asyncio
async def test_confirm_groups_revision_hashes_with_confirmation(registry_session):
    session = registry_session
    root = session.projects.root_path(session.projects.get("pr"))
    for name in ("one.md", "two.md", "three.md"):
        (root / name).write_text(name)
    statements = _trace(session)
    confirmation = await session.confirm("pr", "agent", claim="spec.confirmed", attests={"path": "one.md"},
                                         linked=[], channel="cli", author="tester", note=None)
    assert session.projects.store.confirmations("pr") == (confirmation,)
    assert _commits(statements) == 1
    assert len(_dml(statements)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("checked,failure", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("source_kind", ["operation", "task"])
async def test_accept_groups_collected_hashes_and_publishes_inventories_atomically(
        registry_session, monkeypatch, checked, failure, source_kind):
    from evolution_lab.projects.inventory import collect, inventory_digest
    from evolution_lab.projects.attest import acceptance_entries
    session = registry_session
    root = session.projects.root_path(session.projects.get("pr"))
    facts, digests = [], []
    for index, name in enumerate(("check.passed", "conformance.passed")):
        path = f"lab/input-{index}.md"
        file = root / f"input-{index}.md"
        file.write_text(name)
        entries = collect(file, prefix=path)
        digest = inventory_digest(entries)
        digests.append(digest)
        link = _seed_link(session, "operation" if source_kind == "operation" else "target", f"op-{index}",
                          operation_kind="evaluation")
        session.projects.store.upsert_link(replace(link, binding={
            "inputs": {"digest": "sha256:" + "a" * 64, "sources": [{"path": path}]}}))
        if source_kind == "task":
            session.projects.store.set_last_seen("target", link.object_id, session.pair, session.state_root,
                                                 {"tasks": [{"task_id": f"op-{index}"}]})
        facts.append(Fact(name, {"implementation": "agent"}, True, {"kind": source_kind, "id": f"op-{index}"},
                          StaleReason(("inputs_changed",)), (), state={"inputs_digest": digest}))

    async def evidence(*args):
        return None, tuple(facts)
    monkeypatch.setattr(session, "facts", evidence)
    statements = _trace(session)
    kwargs = dict(only=None, across_pairs=False, reason="reviewed", channel="cli", author="tester", dry_run=False,
                  expected=list(acceptance_entries(facts)) if checked else None)
    if failure:
        def fail(acceptance):
            raise OSError("acceptance unavailable")
        monkeypatch.setattr(session.projects.store, "put_acceptance", fail)
        with pytest.raises(OSError, match="acceptance unavailable"):
            await session.accept("pr", "agent", **kwargs)
        assert session.projects.store.acceptances("pr") == ()
        assert all(session.projects.store.inventory(digest) is None for digest in digests)
        assert _commits(statements) == 1  # only the independently measured hash cache remains
        return
    acceptance = await session.accept("pr", "agent", **kwargs)
    assert session.projects.store.acceptances("pr") == (acceptance,)
    assert all(session.projects.store.inventory(digest) is not None for digest in digests)
    assert _commits(statements) == 2  # one collected hash batch, one inventories + acceptance
    assert len(_dml(statements)) == 5


@pytest.mark.asyncio
async def test_target_batch_failure_rolls_back_earlier_links(registry_session, monkeypatch):
    session = registry_session
    _seed_link(session, "bundle", "b" * 64)
    session.owner.targets = [(f"target-{i}", "b" * 64, client_ref("pr", "agent")) for i in range(3)]
    original = session.projects.store.insert_link

    def insert(link):
        if link.object_id == "target-1":
            raise OSError("registry unavailable")
        return original(link)
    monkeypatch.setattr(session.projects.store, "insert_link", insert)
    with pytest.raises(OSError, match="registry unavailable"):
        await session.sync("pr")
    assert session.projects.store.links("pr", kind="target") == ()


def test_status_hash_batch_failure_rolls_back_earlier_cache_rows(registry_session, monkeypatch):
    session = registry_session
    project = session.projects.get("pr")
    root = session.projects.root_path(project)
    for name in ("one.md", "two.md"):
        (root / name).write_text(name)
    catalogue = SimpleNamespace(descriptors=({"id": "example", "steps": [{"outputs": [
        {"artifact": {"path": name, "check": "exists"}} for name in ("one.md", "two.md")]}]},))
    original = session.projects.store.store

    def cache(path, key, digest):
        if Path(path).name == "two.md":
            raise OSError("cache unavailable")
        original(path, key, digest)
    monkeypatch.setattr(session.projects.store, "store", cache)
    with pytest.raises(OSError, match="cache unavailable"):
        session._status_inputs(project, project.implementations[0], (), catalogue, {}, {})
    assert session.projects.store._db.execute("SELECT path FROM hash_cache").fetchall() == []


@pytest.mark.asyncio
async def test_sync_owner_reads_are_outside_registry_transactions(registry_session, monkeypatch):
    session = registry_session
    _seed_link(session, "operation", "op-1", operation_kind="campaign")
    session.owner.operations = [("op-1", client_ref("pr", "agent"))]
    session.owner.statuses = {"op-1": FakeStatus("op-1", kind="campaign")}
    for name in ("client_operations", "status", "cycle_starts", "client_tasks", "client_targets"):
        original = getattr(session.owner, name)

        async def read(*args, _original=original):
            assert not session.projects.store._db.in_transaction
            return await _original(*args)
        monkeypatch.setattr(session.owner, name, read)
    await session.sync("pr")
    assert len(session.projects.store.links("pr", kind="operation")) == 1


@pytest.mark.asyncio
async def test_full_status_groups_new_hashes_and_unchanged_status_writes_nothing(registry_session):
    session = registry_session
    root = session.projects.root_path(session.projects.get("pr"))
    for name in ("one.md", "two.md", "three.md"):
        (root / name).write_text(name)
    statements = _trace(session)
    status = await session.status_payload("pr", "agent")
    assert status["schema"] == "lab-project-status/6"
    assert _commits(statements) == 1
    assert len(_dml(statements)) == 3
    statements.clear()
    wal = session.projects.home / "projects.sqlite3-wal"
    before = wal.read_bytes()
    assert await session.status_payload("pr", "agent") == status
    assert _commits(statements) == 0 and _dml(statements) == []
    assert wal.read_bytes() == before



def assert_registry_available(store):
    """A competing request must read the registry before this I/O step completes."""
    done = threading.Event()
    rows = []

    def read():
        rows.extend(store.projects(archived=False))
        done.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert done.wait(1), "file I/O held the registry lock and blocked another request"
    reader.join()
    assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["facts", "status", "bundle", "confirm", "accept"])
async def test_file_measurement_leaves_registry_available(registry_session, monkeypatch, action):
    from evolution_lab.preparation import ProbeSource, SourceRoot
    from evolution_lab.projects import inventory
    from evolution_lab.projects.inventory import collect, inventory_digest
    from evolution_lab.service.codecs import BundleRequest
    session = registry_session
    project = session.projects.get("pr")
    root = session.projects.root_path(project)
    file = root / "one.md"
    file.write_text("one")
    if action == "bundle":
        request = BundleRequest(ProbeSource(file, {}), (), {}, (), (SourceRoot("work", root),), ())
        monkeypatch.setattr(session, "_probe_request", lambda *args: request)
        session.preparation.create_bundle = lambda **kwargs: SimpleNamespace(digest="b" * 64)
    if action == "accept":
        entries = collect(file, prefix="lab/one.md")
        digest = inventory_digest(entries)
        link = _seed_link(session, "operation", "op", operation_kind="evaluation")
        session.projects.store.upsert_link(replace(link, binding={"inputs": {
            "digest": "sha256:" + "a" * 64, "sources": [{"path": "lab/one.md"}]}}))
        fact = Fact("check.passed", {"implementation": "agent"}, True, {"kind": "operation", "id": "op"},
                    StaleReason(("inputs_changed",)), (), state={"inputs_digest": digest})
        async def facts(*args):
            return None, (fact,)
        monkeypatch.setattr(session, "facts", facts)
    original = inventory.hashlib.sha256
    observations = []

    def measure(*args, **kwargs):
        assert_registry_available(session.projects.store)
        observations.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(inventory.hashlib, "sha256", measure)
    if action == "facts":
        await session.facts("pr", "agent")
    elif action == "status":
        catalogue = SimpleNamespace(descriptors=({"id": "example", "steps": [{"outputs": [
            {"artifact": {"path": "one.md", "check": "exists"}}]}]},))
        await session._run_io(lambda: session._status_inputs(project, project.implementations[0], (), catalogue, {}, {}))
    elif action == "bundle":
        await session.bundle("pr", "agent")
    elif action == "confirm":
        await session.confirm("pr", "agent", claim="spec.confirmed", attests={"path": "one.md"},
                              linked=[], channel="cli", author="tester", note=None)
    else:
        await session.accept("pr", "agent", only=None, across_pairs=False, reason="reviewed", channel="cli",
                             author="tester", dry_run=False)
    assert observations


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["validation", "publication"])
async def test_failed_confirmation_discards_revision_hashes(registry_session, monkeypatch, failure):
    session = registry_session
    root = session.projects.root_path(session.projects.get("pr"))
    (root / "one.md").write_text("one")
    attests = {"path": "one.md"}
    if failure == "validation":
        attests["digest"] = "sha256:" + "0" * 64
        error = ProjectError
    else:
        original = session.projects.store.put_confirmation
        def fail(confirmation):
            original(confirmation)
            raise OSError("confirmation unavailable")
        monkeypatch.setattr(session.projects.store, "put_confirmation", fail)
        error = OSError
    statements = _trace(session)
    with pytest.raises(error):
        await session.confirm("pr", "agent", claim="spec.confirmed", attests=attests, linked=[],
                              channel="cli", author="tester", note=None)
    assert session.projects.store.confirmations("pr") == ()
    assert session.projects.store._db.execute("SELECT path FROM hash_cache").fetchall() == []
    assert _commits(statements) == 0


def test_overlapping_hash_batches_never_reuse_digest_for_a_different_stat_key(registry_session):
    from evolution_lab.projects.inventory import BufferedHashCache, observe_file
    session = registry_session
    store = session.projects.store
    file = session.projects.root_path(session.projects.get("pr")) / "one.md"
    file.write_bytes(b"old")
    older = BufferedHashCache(store)
    assert observe_file(file, read=False, cache=older).digest == (
        "sha256:cba06b5736faf67e54b07b561eae94395e774c517a7d910a54369e1263ccfbd4")
    file.write_bytes(b"new contents")
    newer = BufferedHashCache(store)
    expected = "sha256:6a5f63424a0c878f3b23b7a85dd453523605762e73f536f968f874e57f565d7e"
    assert observe_file(file, read=False, cache=newer).digest == expected
    with store.transaction():
        newer.flush()
    with store.transaction():
        older.flush()  # the older measurement finishes last
    assert observe_file(file, read=False, cache=BufferedHashCache(store)).digest == expected
    # The same batch must also check the complete key after its file changes.
    assert observe_file(file, read=False, cache=older).digest == expected

def _team(tmp_path):
    team = tmp_path / "team"; (team / "agent").mkdir(parents=True); (team / "lab").mkdir()
    (team / "agent" / "a.py").write_text("A = 1\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "add", "."], ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(argv, cwd=team, check=True, env=env)
    return team


def test_sync_clone_admits_a_lab_clone_in_its_place(tmp_path):
    with Projects.open(tmp_path / "home") as projects:
        projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL, clone={"repo": str(_team(tmp_path))})
        project, clone = projects.sync_clone("acq")
        assert clone == projects.home / "clones" / "acq" and project.id == "acq"


def test_sync_clone_refuses_path_projects_and_moved_copies(tmp_path):
    team = _team(tmp_path)
    with Projects.open(tmp_path / "home") as projects:
        projects.create(id="own", name="Own", materials_root="lab", implementations=IMPL, path=str(team))
        with pytest.raises(ProjectError) as error:
            projects.sync_clone("own")
        assert error.value.code == "not_a_clone"
        projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL, clone={"repo": str(team)})
        projects.update("acq", {"op": "set_working_copy", "path": str(team)})
        with pytest.raises(ProjectError) as error:
            projects.sync_clone("acq")
        assert error.value.code == "not_a_clone"


def test_record_sync_moves_base_revision_only(tmp_path):
    with Projects.open(tmp_path / "home") as projects:
        before = projects.create(id="acq", name="Acq", materials_root="lab", implementations=IMPL,
                                 clone={"repo": str(_team(tmp_path))})
        after = projects.record_sync("acq", "a" * 40)
        assert after.working_copy.source == {**before.working_copy.source, "base_revision": "a" * 40}
        assert after.record_version == before.record_version + 1
        assert projects.get("acq").working_copy.source["base_revision"] == "a" * 40
