"""Registry reads retain snapshots without rewriting unchanged rows (ADR-0029)."""

from copy import deepcopy
import json

import pytest

from dev67_reference import reference_project
from evolution_lab.projects.inventory import observe_file
from evolution_lab.projects.store import RegistryStore


def _writes(store, monkeypatch):
    writes = []
    execute = store._execute

    def record(sql, params=()):
        if sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append((sql, params))
        return execute(sql, params)

    monkeypatch.setattr(store, "_execute", record)
    return writes


async def test_second_reference_read_and_schema_observation_write_nothing(tmp_path, monkeypatch):
    async with reference_project(tmp_path / "reference", monkeypatch) as stand:
        # The scenario status reads and validates behavior-map.json with read=True.
        path = "/v1/projects/acq/status?implementation=port-a&scenario=restructure"
        assert (await stand.client.get(path)).status_code == 200
        writes = _writes(stand.projects.store, monkeypatch)
        await stand.session.sync("acq")
        assert writes == []
        response = await stand.client.get(path)
        assert response.status_code == 200
        assert writes == []


async def test_owner_change_refreshes_only_its_operation_link(tmp_path, monkeypatch):
    async with reference_project(tmp_path / "reference", monkeypatch) as stand:
        await stand.session.sync("acq")
        store = stand.projects.store
        before = {link.dedupe_key(): link.last_seen for link in store.links("acq")}
        operation_id = stand.operation_ids["campaign-running"]
        link = store.link_for("operation", operation_id, stand.session.pair, stand.session.state_root)
        assert link.last_seen["control_state"] == "running"

        # This is a real Owner state transition, read back through Owner.status by sync.
        changed = await stand.owner._db("request_operation_stop", operation_id)
        assert changed.control_state == "stopping"
        monkeypatch.setattr("evolution_lab.projects.session.utc_now", lambda: "2099-01-01T00:00:00Z")
        writes = _writes(store, monkeypatch)
        await stand.session.sync("acq")
        assert len(writes) == 1
        assert writes[0][0].startswith("UPDATE links")
        assert writes[0][1][-1] == link.dedupe_key()

        after = {item.dedupe_key(): item.last_seen for item in store.links("acq")}
        assert after[link.dedupe_key()]["control_state"] == "stopping"
        assert after[link.dedupe_key()]["at"] != before[link.dedupe_key()]["at"]
        assert {key: value for key, value in after.items() if key != link.dedupe_key()} == {
            key: value for key, value in before.items() if key != link.dedupe_key()}


async def test_last_seen_ignores_only_top_level_at_and_keeps_transaction(tmp_path, monkeypatch):
    async with reference_project(tmp_path / "reference", monkeypatch) as stand:
        await stand.session.sync("acq")
        store = stand.projects.store
        operation_id = stand.operation_ids["campaign-finished"]
        link = store.link_for("operation", operation_id, stand.session.pair, stand.session.state_root)
        original = deepcopy(link.last_seen)
        changed_at = {**original, "at": "2099-01-01T00:00:00Z"}
        writes = _writes(store, monkeypatch)
        store.set_last_seen("operation", operation_id, stand.session.pair, stand.session.state_root, changed_at)
        assert writes == []
        assert store.link_for("operation", operation_id, stand.session.pair, stand.session.state_root).last_seen == original

        nested_time = deepcopy(original)
        nested_time["resume"] = {**(nested_time["resume"] or {}), "at": "nested-change"}
        store.set_last_seen("operation", operation_id, stand.session.pair, stand.session.state_root, nested_time)
        assert len(writes) == 1
        store.set_last_seen("operation", operation_id, stand.session.pair, stand.session.state_root, original)
        writes.clear()

        # A changed non-time field must write, even when the incoming at is unchanged.
        changed_state = {**original, "control_state": "stopping"}
        store.set_last_seen("operation", operation_id, stand.session.pair, stand.session.state_root, changed_state)
        assert len(writes) == 1
        assert store.link_for("operation", operation_id, stand.session.pair, stand.session.state_root).last_seen == changed_state

        writes.clear()
        monkeypatch.setattr("evolution_lab.projects.session.utc_now", lambda: "2099-01-01T00:00:00Z")
        await stand.session.sync("acq")
        assert len(writes) == 1
        assert writes[0][0].startswith("UPDATE links")
        assert writes[0][1][-1] == link.dedupe_key()
        refreshed = store.link_for("operation", operation_id, stand.session.pair, stand.session.state_root).last_seen
        assert {key: value for key, value in refreshed.items() if key != "at"} == {
            key: value for key, value in original.items() if key != "at"}
        assert refreshed["at"] != changed_state["at"]

        # Make both rows stale, then fail after both writes. Their joint transaction rolls back.
        dedupe = link.dedupe_key()
        store.set_last_seen("operation", operation_id, stand.session.pair, stand.session.state_root, changed_state)
        store.set_link_resume_target(dedupe, {"probe": "stale"})
        writes.clear()
        resume_targets = store.link_resume_targets("acq")
        assert resume_targets[dedupe] == {"probe": "stale"}
        set_target = store.set_link_resume_target

        def fail_after_target_write(key, target):
            set_target(key, target)
            raise RuntimeError("after paired writes")

        monkeypatch.setattr(store, "set_link_resume_target", fail_after_target_write)
        with pytest.raises(RuntimeError, match="after paired writes"):
            await stand.session.sync("acq")
        assert store.link_for("operation", operation_id, stand.session.pair, stand.session.state_root).last_seen == changed_state
        assert store.link_resume_targets("acq")[dedupe] == {"probe": "stale"}


def test_export_resume_and_hash_cache_write_only_when_values_change(tmp_path, monkeypatch):
    with RegistryStore.open(tmp_path / "registry.sqlite3") as store:
        writes = _writes(store, monkeypatch)
        store.set_link_export("link", "export-a", 1)
        store.set_link_resume_target("link", {"state": "ready"})
        artifact = tmp_path / "behavior-map.json"
        artifact.write_text('{"schema":"restructure-behavior-map/1"}')
        assert observe_file(artifact, read=True, cache=store).state == "read"
        assert len(writes) == 3
        writes.clear()

        store.set_link_export("link", "export-a", 1)
        store.set_link_resume_target("link", {"state": "ready"})
        assert observe_file(artifact, read=True, cache=store).state == "read"
        assert writes == []

        store.set_link_export("link", "export-a", 2)
        store.set_link_resume_target("link", {"state": "different"})
        [(key_json, digest)] = store._execute("SELECT key_json, sha256 FROM hash_cache WHERE path=?", (str(artifact),))
        key = tuple(json.loads(key_json))
        store.store(str(artifact), (*key[:3], key[3] + 1), digest)
        assert len(writes) == 3
        writes.clear()
        store.store(str(artifact), (*key[:3], key[3] + 1), "different")
        assert len(writes) == 1
        writes.clear()
        # A pre-existing noncanonical JSON spelling is still the same stat key.
        store._execute("UPDATE hash_cache SET key_json=? WHERE path=?",
                       (json.dumps([*key[:3], key[3] + 1]), str(artifact)))
        writes.clear()
        store.store(str(artifact), (*key[:3], key[3] + 1), "different")
        assert writes == []
        store.set_link_export("link", None, None)
        store.set_link_resume_target("link", None)
        assert len(writes) == 2
        writes.clear()
        store.set_link_export("link", None, None)
        store.set_link_resume_target("link", None)
        assert writes == []
