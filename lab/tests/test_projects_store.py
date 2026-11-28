import sqlite3

import pytest

from evolution_lab.projects import store as store_module
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Implementation, Project, WorkingCopy
from evolution_lab.projects.store import RegistryStore


def _project(project_id="acquiring", **overrides):
    values = dict(
        id=project_id, name="Acquiring", state="ready", failure=None,
        created_at="2026-09-27T12:00:00Z", archived=None, record_version=1,
        working_copy=WorkingCopy("/w", False, True, None), materials_root="lab", exclude=(),
        implementations=(Implementation("acquiring", "acquiring", ".", {"kind": "connected"}, "bundle-request.json", None),),
    )
    values.update(overrides)
    return Project(**values)


def test_round_trip_and_listing(tmp_path):
    with RegistryStore.open(tmp_path / "projects.sqlite3") as registry:
        registry.put_project(_project())
        registry.put_project(_project("old", archived={"at": "t", "reason": None}))
        assert registry.project("acquiring") == _project()
        assert [p.id for p in registry.projects(archived=False)] == ["acquiring"]
        assert [p.id for p in registry.projects(archived=True)] == ["acquiring", "old"]
        assert registry.known_id("old") and not registry.known_id("new")


def test_payload_is_lab_project_1():
    payload = _project().to_payload()
    assert payload["schema"] == "lab-project/1"
    assert Project.from_payload(payload) == _project()
    with pytest.raises(ValueError):
        Project.from_payload({**payload, "extra": 1})


def test_version_is_major_minor(tmp_path):
    with RegistryStore.open(tmp_path / "r.sqlite3") as registry:
        assert registry.version == 1000 * store_module.REGISTRY_MAJOR + store_module.REGISTRY_MINOR


def test_newer_major_is_refused_without_writes(tmp_path):
    path = tmp_path / "r.sqlite3"
    RegistryStore.open(path).close()
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version = 2000")
    with pytest.raises(ProjectError) as caught:
        RegistryStore.open(path)
    assert caught.value.code == "registry_newer"


def test_newer_minor_is_readable(tmp_path):
    path = tmp_path / "r.sqlite3"
    with RegistryStore.open(path) as registry:
        registry.put_project(_project())
    with sqlite3.connect(path) as database:
        database.execute("ALTER TABLE projects ADD COLUMN future TEXT")
        database.execute("PRAGMA user_version = 1006")
    with RegistryStore.open(path) as registry:
        assert registry.project("acquiring") == _project()
        registry.put_project(_project(name="Renamed"))
        assert registry.project("acquiring").name == "Renamed"


def test_older_minor_is_migrated_after_backup(tmp_path, monkeypatch):
    path = tmp_path / "r.sqlite3"
    RegistryStore.open(path).close()
    current = store_module.REGISTRY_MINOR
    monkeypatch.setattr(store_module, "REGISTRY_MINOR", current + 1)
    monkeypatch.setattr(store_module, "MIGRATIONS",
                        {**store_module.MIGRATIONS, current + 1: "ALTER TABLE projects ADD COLUMN extra TEXT"})
    with RegistryStore.open(path) as registry:
        assert registry.version == 1000 + current + 1
    assert (tmp_path / f"r.sqlite3.bak-{1000 + current}").is_file()


def _registry_1000(path):
    """A registry as part 1 wrote it: minor 0, no link_exports, link_resume_targets, ports or input_environments table."""
    RegistryStore.open(path).close()
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE link_exports")
        database.execute("DROP TABLE link_resume_targets")
        database.execute("DROP TABLE ports")
        database.execute("DROP TABLE input_environments")
        database.execute("DROP TABLE approvals")
        database.execute("PRAGMA user_version = 1000")


def _registry_1001(path):
    """A registry as dev28 wrote it: minor 1, no link_resume_targets, ports or input_environments table."""
    RegistryStore.open(path).close()
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE link_resume_targets")
        database.execute("DROP TABLE ports")
        database.execute("DROP TABLE input_environments")
        database.execute("DROP TABLE approvals")
        database.execute("PRAGMA user_version = 1001")


def _registry_1002(path):
    """A registry as dev29-dev34 wrote it: minor 2, no ports or input_environments table."""
    RegistryStore.open(path).close()
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE ports")
        database.execute("DROP TABLE input_environments")
        database.execute("DROP TABLE approvals")
        database.execute("PRAGMA user_version = 1002")


def test_minor_0_registry_gains_link_exports(tmp_path):
    path = tmp_path / "r.sqlite3"
    _registry_1000(path)
    with RegistryStore.open(path) as registry:
        assert registry.version == 1005
        registry.set_link_export("k", "e" * 64, 1)
    assert (tmp_path / "r.sqlite3.bak-1000").is_file()


def test_link_exports_round_trip_by_project(tmp_path):
    from evolution_lab.projects.models import Link, Pair
    with RegistryStore.open(tmp_path / "r.sqlite3") as registry:
        link = Link("operation", "acquiring", "acquiring", Pair("l", "c"), "/s", "t", {"inputs": {}}, "b" * 64,
                    "p" * 64, "op-1", "campaign", None)
        registry.insert_link(link)
        assert registry.link_exports("acquiring") == {}
        registry.set_link_export(link.dedupe_key(), "e" * 64, 1)
        registry.set_link_export(link.dedupe_key(), "f" * 64, 2)
        assert registry.link_exports("acquiring") == {link.dedupe_key(): ("f" * 64, 2)}
        assert registry.link_exports("other") == {}
        registry.set_link_export(link.dedupe_key(), None, None)
        assert registry.link_exports("acquiring") == {}


def test_link_exports_of_an_unmigrated_read_only_registry_are_empty(tmp_path):
    path = tmp_path / "r.sqlite3"
    _registry_1000(path)
    with RegistryStore.open(path, read_only=True) as registry:
        assert registry.version == 1000
        assert registry.link_exports("acquiring") == {}


_TARGET = {"requirement": "fresh_instance", "reason": "target_unstable_fresh_required",
           "excluded": [{"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g1"}]}


def test_registry_minor_one_migrates_and_reads_without_resume_targets(tmp_path):
    assert store_module.REGISTRY_MINOR == 5
    path = tmp_path / "r.sqlite3"
    _registry_1001(path)
    with RegistryStore.open(path, read_only=True) as registry:
        assert registry.version == 1001
        assert registry.link_resume_targets("acquiring") == {}  # no table, no error
    with RegistryStore.open(path) as registry:
        assert registry.version == 1005
        registry.set_link_resume_target("k", _TARGET)
    assert (tmp_path / "r.sqlite3.bak-1001").is_file()


def test_link_resume_targets_round_trip_by_project(tmp_path):
    from evolution_lab.projects.models import Link, Pair
    with RegistryStore.open(tmp_path / "r.sqlite3") as registry:
        link = Link("operation", "acquiring", "acquiring", Pair("l", "c"), "/s", "t", {"inputs": {}}, "b" * 64,
                    "p" * 64, "op-1", "campaign", None)
        registry.insert_link(link)
        assert registry.link_resume_targets("acquiring") == {}
        registry.set_link_resume_target(link.dedupe_key(), _TARGET)
        assert registry.link_resume_targets("acquiring") == {link.dedupe_key(): _TARGET}
        second = {**_TARGET, "excluded": _TARGET["excluded"] + [
            {"schema": "lab-target-ref/1", "id": "t2", "owner_generation": "g1"}]}
        registry.set_link_resume_target(link.dedupe_key(), second)
        assert registry.link_resume_targets("acquiring") == {link.dedupe_key(): second}
        assert registry.link_resume_targets("other") == {}
        registry.set_link_resume_target(link.dedupe_key(), None)
        assert registry.link_resume_targets("acquiring") == {}
        registry.set_link_resume_target(link.dedupe_key(), None)  # forgetting twice is fine


def test_read_only_open_of_missing_registry_is_empty(tmp_path):
    with RegistryStore.open(tmp_path / "none.sqlite3", read_only=True) as registry:
        assert registry.projects(archived=True) == ()
    assert not (tmp_path / "none.sqlite3").exists()


# --- Final-review fix wave: I8 ---

FROZEN = ("record_json shapes are frozen within a registry major (ADR-0017 p. 3, amendment 2026-09-27): "
          "add columns or tables instead, or bump the registry major")


def test_record_json_key_sets_are_frozen_within_the_major(tmp_path):
    import json

    from evolution_lab.projects.models import Acceptance, Confirmation, Link, Pair
    path = tmp_path / "r.sqlite3"
    source = {"repo": "r", "base_revision": "b", "branch": "lab/acquiring"}
    project = _project(working_copy=WorkingCopy("/w", True, True, source),
                       archived={"at": "t", "reason": None})
    link = Link("operation", "acquiring", "acquiring", Pair("l", "c"), "/s", "t", {"inputs": {}}, "b" * 64,
                "p" * 64, "op-1", "evaluation", {"control_state": "running"})
    confirmation = Confirmation("c-1", "acquiring", None, "spec.confirmed", {"path": "SPEC.md", "digest": "d"},
                                ({"kind": "revision", "ref": "r", "digest": "d"},), "cli", "t", None, "t", None)
    acceptance = Acceptance("a-1", "acquiring", "acquiring", ({"id": "spec.confirmed"},), False, "r", "cli", "t",
                            "t", None)
    with RegistryStore.open(path) as registry:
        registry.put_project(project)
        registry.insert_link(link)
        registry.put_confirmation(confirmation)
        registry.put_acceptance(acceptance)
    with sqlite3.connect(path) as database:
        stored = {table: json.loads(database.execute(f"SELECT record_json FROM {table}").fetchone()[0])
                  for table in ("projects", "links", "confirmations", "acceptances")}
    assert set(stored["projects"]) == {
        "schema", "id", "name", "state", "failure", "created_at", "archived", "record_version",
        "working_copy", "materials_root", "exclude", "implementations"}, FROZEN
    assert set(stored["projects"]["working_copy"]) == {"path", "created_by_lab", "git", "source"}, FROZEN
    assert set(stored["projects"]["working_copy"]["source"]) == {"repo", "base_revision", "branch"}, FROZEN
    assert set(stored["projects"]["archived"]) == {"at", "reason"}, FROZEN
    assert set(stored["projects"]["implementations"][0]) == {
        "id", "name", "place", "origin", "package_entry", "archived"}, FROZEN
    assert set(stored["links"]) == {
        "schema", "kind", "project", "implementation", "pair", "state_root", "created_at", "binding",
        "bundle_digest", "prepared_digest", "object_id", "operation_kind", "last_seen"}, FROZEN
    assert set(stored["links"]["pair"]) == {"lab", "core"}, FROZEN
    assert set(stored["confirmations"]) == {
        "schema", "id", "project", "implementation", "claim", "attests", "linked", "channel", "author",
        "note", "created_at", "revoked"}, FROZEN
    assert set(stored["acceptances"]) == {
        "schema", "id", "project", "implementation", "facts", "across_pairs", "reason", "channel", "author",
        "created_at", "revoked", "dry_run"}, FROZEN
    assert (stored["projects"]["schema"], stored["links"]["schema"], stored["confirmations"]["schema"],
            stored["acceptances"]["schema"]) == ("lab-project/1", "lab-project-link/1",
                                                  "lab-project-confirmation/1", "lab-project-acceptance/1"), FROZEN


def test_transaction_rolls_back_last_seen_and_resume_target_together(tmp_path):
    from evolution_lab.projects.models import Link, Pair
    pair = Pair("l", "c")
    with RegistryStore.open(tmp_path / "r.sqlite3") as registry:
        old_seen = {"at": "old", "control_state": "running"}
        new_seen = {"at": "new", "control_state": "stopping"}
        link = Link("operation", "acquiring", "acquiring", pair, "/s", "t", {"inputs": {}}, "b" * 64,
                    "p" * 64, "op-1", "campaign", old_seen)
        registry.insert_link(link)
        registry.set_link_resume_target(link.dedupe_key(), _TARGET)
        second = {**_TARGET, "excluded": []}
        with pytest.raises(RuntimeError):
            with registry.transaction():
                registry.set_last_seen("operation", "op-1", pair, "/s", new_seen)
                registry.set_link_resume_target(link.dedupe_key(), second)
                raise RuntimeError("crash between the halves")
        assert registry.link_for("operation", "op-1", pair, "/s").last_seen == old_seen
        assert registry.link_resume_targets("acquiring") == {link.dedupe_key(): _TARGET}
        with registry.transaction():
            registry.set_last_seen("operation", "op-1", pair, "/s", new_seen)
            registry.set_link_resume_target(link.dedupe_key(), second)
        assert registry.link_for("operation", "op-1", pair, "/s").last_seen == new_seen
        assert registry.link_resume_targets("acquiring") == {link.dedupe_key(): second}


_PORT = {"template_source": "https://example.com/sylph.git", "template_ref": "HEAD",
         "template_commit": "c" * 40, "created_at": "2026-10-01T12:00:00Z"}


def test_ports_round_trip_by_project(tmp_path):
    with RegistryStore.open(tmp_path / "r.sqlite3") as registry:
        assert registry.ports("acquiring") == {}
        registry.insert_port("acquiring", "acquiring-port", **_PORT)
        registry.insert_port("other", "x", **_PORT)
        assert registry.ports("acquiring") == {"acquiring-port": {
            "template": "https://example.com/sylph.git", "ref": "HEAD", "commit": "c" * 40,
            "created_at": "2026-10-01T12:00:00Z"}}
        with pytest.raises(sqlite3.IntegrityError):
            registry.insert_port("acquiring", "acquiring-port", **_PORT)


def test_registry_minor_two_migrates_to_ports_after_backup(tmp_path):
    path = tmp_path / "r.sqlite3"
    _registry_1002(path)
    with RegistryStore.open(path, read_only=True) as registry:
        assert registry.version == 1002 and registry.ports("acquiring") == {}  # no table, no error
    with RegistryStore.open(path) as registry:
        assert registry.version == 1005
        registry.insert_port("acquiring", "p", **_PORT)
    assert (tmp_path / "r.sqlite3.bak-1002").is_file()


def test_ports_table_columns_are_the_spec_ones(tmp_path):
    path = tmp_path / "r.sqlite3"
    RegistryStore.open(path).close()
    with sqlite3.connect(path) as database:
        columns = [row[1] for row in database.execute("PRAGMA table_info(ports)")]
    assert columns == ["project_id", "implementation_id", "template_source", "template_ref", "template_commit",
                       "created_at"]


def test_project_wire_adds_port_and_keeps_the_record_form():
    from evolution_lab.projects.models import project_from_wire, project_wire
    project = _project()
    ports = {"acquiring": {"template": "/t", "ref": "HEAD", "commit": "c" * 40, "created_at": "t"}}
    wire = project_wire(project, ports)
    assert wire["schema"] == "lab-project/2" and project.to_payload()["schema"] == "lab-project/1"
    assert wire["implementations"][0]["port"] == ports["acquiring"]
    assert list(wire["implementations"][0]) == ["id", "name", "place", "origin", "package_entry", "archived", "port"]
    assert project_from_wire(wire) == (project, ports)
    assert project_wire(project, {})["implementations"][0]["port"] is None
    for bad in ({**wire, "schema": "lab-project/1"},
                {**wire, "implementations": [{k: v for k, v in wire["implementations"][0].items() if k != "port"}]},
                {**wire, "implementations": [{**wire["implementations"][0], "port": {"template": "/t"}}]}):
        with pytest.raises(ValueError):
            project_from_wire(bad)
