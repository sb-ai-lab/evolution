import pytest

from evolution_lab.projects.facts import FactContext, OperationView, TaskView, baseline_failure, compute_facts
from evolution_lab.projects.inventory import Entry
from evolution_lab.projects.models import Implementation, Pair, Project, WorkingCopy
from evolution_lab.projects.revision import Revision

PAIR = Pair("0.0.3.dev28", "0.3.0a17")
OLD = Pair("0.0.3.dev27", "0.3.0a17")
E1 = (Entry("agent/a.py", "file", 0, 1, "11"),)
E2 = (Entry("agent/a.py", "file", 0, 1, "22"),)
BINDING = {"working_copy": "/w", "materials_root": "lab", "place": "agent", "materials_revision": "r",
           "inputs": {"digest": "D1", "sources": []}}
IMPL = Implementation("agent", "agent", "agent", {"kind": "connected"}, "bundle-request.json", None)
PROJECT = Project("acq", "Acq", "ready", None, "t", None, 1, WorkingCopy("/w", False, False, None), "lab", (), (IMPL,))
REV = Revision("R", (), 1, None, ())
MEASURED = {"coverage": {"expected": 2, "scored": 2}, "stop_reason": None, "persistence": {"state": "saved", "failure": None}}


def task(action="check", state="succeeded", pair=PAIR, current="D1", entries=E1, order=1):
    return TaskView(f"{action}-{order}", action, state, "t1", pair, BINDING, "D1", E1, current, entries, order, "bundle-a", "basis-a")


def evaluation(result=MEASURED, control="finished", pair=PAIR, current="D1", entries=E1, order=3):
    return OperationView(f"op{order}", "evaluation", pair, control, result, BINDING, "D1", E1, current, entries, order, "bundle-a", "basis-a")


def ctx(**overrides):
    values = dict(project=PROJECT, implementation=IMPL, pair=PAIR, revision=REV, package_present=True,
                  package_errors=(), place_available=True, tasks=(task(), task("conformance", order=2)),
                  operations=(evaluation(),), campaigns=(), confirmations=(), confirmation_now={},
                  acceptances=(), inventories={}, current_bundle_digest="bundle-a", current_measurement_basis="basis-a")
    values.update(overrides)
    return FactContext(**values)


def by_id(facts):
    return {fact.id: fact for fact in facts}


def test_fresh_evidence_makes_connected():
    facts = by_id(compute_facts(ctx()))
    for name in ("check.passed", "conformance.passed", "baseline.measured", "connected"):
        assert facts[name].holds, name
    assert facts["package.valid"].value == {"scope": "form"}


def test_inputs_change_stales_operation_facts_with_changes():
    facts = by_id(compute_facts(ctx(tasks=(task(current="D2", entries=E2), task("conformance", order=2)))))
    stale = facts["check.passed"]
    assert not stale.holds and stale.stale_reason.codes == ("inputs_changed",)
    assert stale.stale_reason.changes.modified == ("agent/a.py",)
    assert not facts["connected"].holds


def test_other_pair_and_inputs_are_both_reported():
    facts = by_id(compute_facts(ctx(tasks=(task(pair=OLD, current="D2", entries=E2),))))
    assert facts["check.passed"].stale_reason.codes == ("other_pair", "inputs_changed")


def test_place_change_is_reported():
    from dataclasses import replace
    facts = by_id(compute_facts(ctx(project=replace(PROJECT, materials_root="lab2"))))
    assert "place_changed" in facts["check.passed"].stale_reason.codes


@pytest.mark.parametrize("result,control,reason", [
    (MEASURED, "running", "not_finished"),
    ({**MEASURED, "coverage": {"expected": 2, "scored": 1}}, "finished", "coverage"),
    ({**MEASURED, "coverage": None}, "finished", "coverage"),
    ({**MEASURED, "coverage": {"expected": 0, "scored": 0}}, "finished", "coverage"),
    ({**MEASURED, "stop_reason": "cancelled"}, "finished", "stop_reason"),
    ({**MEASURED, "persistence": {"state": "failed", "failure": {}}}, "finished", "persistence"),
    (MEASURED, "released", None),
])
def test_baseline_predicate(result, control, reason):
    assert baseline_failure(evaluation(result=result, control=control)) == reason


def test_latest_terminal_evaluation_counts():
    running = evaluation(control="running", order=4)
    facts = by_id(compute_facts(ctx(operations=(evaluation(), running))))
    assert facts["baseline.measured"].holds


def test_failed_check_is_false_not_stale():
    facts = by_id(compute_facts(ctx(tasks=(task(state="failed"),))))
    assert facts["check.passed"].value is False and facts["check.passed"].stale_reason is None


def test_connected_needs_same_inputs_for_all_three():
    other = OperationView("op9", "evaluation", PAIR, "finished", MEASURED, {**BINDING, "inputs": {"digest": "D3", "sources": []}},
                          "D3", E1, "D3", E1, 9, "bundle-b", "basis-b")
    facts = by_id(compute_facts(ctx(operations=(other,), current_bundle_digest="bundle-b", current_measurement_basis="basis-b")))
    assert facts["baseline.measured"].holds and not facts["connected"].holds


def test_no_package_entry():
    facts = by_id(compute_facts(ctx(package_present=False, package_errors=None)))
    assert facts["package.present"].value is False and facts["package.valid"].value is False


def test_package_present_source_explains_absence():
    facts = by_id(compute_facts(ctx(package_present=False, package_errors=None,
                                    package_entry_path="bundle-request.json", package_reason="missing")))
    assert facts["package.present"].source == {"kind": "package_entry", "entry": "bundle-request.json",
                                               "reason": "missing"}


def test_package_present_source_when_present():
    facts = by_id(compute_facts(ctx(package_entry_path="bundle-request.json", package_reason=None)))
    assert facts["package.present"].source == {"kind": "package_entry", "entry": "bundle-request.json", "reason": None}


def test_newer_orphaned_evaluation_shadows_an_older_measured_one():
    orphaned = evaluation(result=None, control="orphaned", order=4)
    facts = by_id(compute_facts(ctx(operations=(evaluation(), orphaned))))
    assert facts["baseline.measured"].value is False
    assert facts["baseline.measured"].source["failed"] == "not_finished"


def test_newer_running_evaluation_still_does_not_shadow_a_measured_one():
    running = evaluation(control="running", order=4)
    facts = by_id(compute_facts(ctx(operations=(evaluation(), running))))
    assert facts["baseline.measured"].holds


def test_place_unavailable_blocks_implementation_available():
    facts = by_id(compute_facts(ctx(place_available=False)))
    assert facts["implementation.available"].value is False


# --- Final-review fix wave: I1 (a revoked link breaks the chain), I2 (no holding on missing digests) ---

from evolution_lab.projects.models import Acceptance, Confirmation  # noqa: E402

E_S1 = (Entry("agent/a.py", "file", 0, 1, "22"),)
E_S2 = (Entry("agent/a.py", "file", 0, 1, "22"), Entry("agent/b.py", "file", 0, 1, "33"))
PLACE = ["/w", "lab", "agent"]


def _link(acceptance_id, to_digest, created, revoked_at=None):
    entry = {"id": "check.passed", "from": {"kind": "task", "id": "check-1"},
             "to": {"inputs_digest": to_digest, "place": PLACE, "pair": PAIR.to_payload()},
             "codes": ["inputs_changed"], "changes": None}
    return Acceptance(acceptance_id, "acq", "agent", (entry,), False, "r", "cli", "t", created,
                      None if revoked_at is None else {"at": revoked_at, "reason": "x"})


def _chain_ctx(*acceptances):
    # The check ran on D1 (E1); S1 = D2 (a.py modified), S2 = D3 (b.py added) is the current state.
    return ctx(tasks=(task(current="D3", entries=E_S2),), acceptances=acceptances,
               inventories={"D2": E_S1, "D3": E_S2})


def test_unbroken_two_link_chain_carries_the_fact():
    facts = by_id(compute_facts(_chain_ctx(_link("a1", "D2", "2026-09-27T10:00:01Z"),
                                           _link("a2", "D3", "2026-09-27T10:00:02Z"))))
    check = facts["check.passed"]
    assert check.holds and [item["acceptance"] for item in check.carried_over] == ["a1", "a2"]


def test_revoking_the_first_link_breaks_the_chain_back_to_the_source():
    facts = by_id(compute_facts(_chain_ctx(_link("a1", "D2", "2026-09-27T10:00:01Z", revoked_at="2026-09-27T10:00:03Z"),
                                           _link("a2", "D3", "2026-09-27T10:00:02Z"))))
    check = facts["check.passed"]
    assert not check.holds and check.carried_over == ()
    assert check.stale_reason.codes == ("inputs_changed",)
    # relative to the original source (E1): a.py modified *and* b.py added
    assert check.stale_reason.changes.modified == ("agent/a.py",)
    assert check.stale_reason.changes.added == ("agent/b.py",)


def test_revoking_only_the_second_link_stales_relative_to_the_first_to():
    facts = by_id(compute_facts(_chain_ctx(_link("a1", "D2", "2026-09-27T10:00:01Z"),
                                           _link("a2", "D3", "2026-09-27T10:00:02Z", revoked_at="2026-09-27T10:00:03Z"))))
    check = facts["check.passed"]
    assert not check.holds and check.stale_reason.codes == ("inputs_changed",)
    assert check.stale_reason.changes.modified == () and check.stale_reason.changes.added == ("agent/b.py",)


def test_a_revocation_in_the_same_second_as_a_later_link_breaks_it_conservatively():
    facts = by_id(compute_facts(_chain_ctx(_link("a1", "D2", "2026-09-27T10:00:01Z", revoked_at="2026-09-27T10:00:02Z"),
                                           _link("a2", "D3", "2026-09-27T10:00:02Z"))))
    assert not facts["check.passed"].holds


def test_an_acceptance_made_after_a_revocation_starts_a_new_chain():
    # Re-accepting after a revoke must still work: a3 was made after a1's revocation, on top of
    # the (then empty) chain, so it carries the fact on its own.
    facts = by_id(compute_facts(_chain_ctx(_link("a1", "D2", "2026-09-27T10:00:01Z", revoked_at="2026-09-27T10:00:02Z"),
                                           _link("a3", "D3", "2026-09-27T10:00:03Z"))))
    check = facts["check.passed"]
    assert check.holds and [item["acceptance"] for item in check.carried_over] == ["a3"]


def test_a_confirmation_never_holds_while_its_current_digest_is_missing():
    confirmation = Confirmation("c-1", "acq", None, "spec.confirmed", {"path": "SPEC.md", "digest": "sha256:1"},
                                (), "cli", "t", None, "2026-09-27T10:00:00Z", None)
    # an acceptance written before the I2 fix could carry a state with a missing digest
    poisoned = Acceptance("a1", "acq", None, ({"id": "spec.confirmed", "from": {"kind": "confirmation", "id": "c-1"},
                                               "to": {"attests": None, "linked": []}, "codes": ["digest_changed"],
                                               "changes": None},),
                          False, "r", "cli", "t", "2026-09-27T10:00:01Z", None)
    facts = by_id(compute_facts(ctx(confirmations=(confirmation,), acceptances=(poisoned,),
                                    confirmation_now={"c-1": {"attests": None, "linked": []}})))
    fact = facts["spec.confirmed"]
    assert not fact.holds and fact.stale_reason.codes == ("digest_changed",)


def test_a_confirmed_fact_source_names_author_channel_and_time():
    confirmation = Confirmation("c-1", "acq", None, "spec.confirmed", {"path": "SPEC.md", "digest": "sha256:1"},
                                (), "auto-evolve", "lead", None, "2026-09-30T10:00:00Z", None)
    facts = by_id(compute_facts(ctx(confirmations=(confirmation,),
                                    confirmation_now={"c-1": {"attests": "sha256:1", "linked": []}})))
    assert facts["spec.confirmed"].source == {
        "kind": "confirmation", "id": "c-1", "attests": {"path": "SPEC.md", "digest": "sha256:1"},
        "channel": "auto-evolve", "author": "lead", "created_at": "2026-09-30T10:00:00Z"}


def _ported_ctx(origin, *, archived_source=False, source_present=True):
    source = Implementation("agent", "agent", "agent", {"kind": "connected"}, "bundle-request.json",
                            {"at": "t", "reason": None} if archived_source else None)
    port = Implementation("agent-port", "agent-port", "/home/lab/ports/acq/agent-port", origin, None, None)
    items = (source, port) if source_present else (port,)
    project = Project("acq", "Acq", "ready", None, "t", None, 1, WorkingCopy("/w", False, False, None), "lab", (),
                      items)
    return ctx(project=project, implementation=port)


def test_a_connected_implementation_is_not_ported():
    fact = by_id(compute_facts(ctx()))["implementation.ported"]
    assert (fact.value, fact.holds) == (False, False)
    assert fact.source == {"kind": "origin", "implementation": None, "reason": "not_ported"}
    assert fact.subject == {"implementation": "agent"} and fact.stale_reason is None


@pytest.mark.parametrize("archived_source", [False, True])
def test_a_port_of_a_registered_source_is_ported(archived_source):
    origin = {"kind": "ported_from", "implementation": "agent"}
    fact = by_id(compute_facts(_ported_ctx(origin, archived_source=archived_source)))["implementation.ported"]
    assert fact.holds is True
    assert fact.source == {"kind": "origin", "implementation": "agent", "reason": None}


def test_a_port_whose_source_is_gone_is_not_ported():
    origin = {"kind": "ported_from", "implementation": "agent"}
    fact = by_id(compute_facts(_ported_ctx(origin, source_present=False)))["implementation.ported"]
    assert fact.holds is False
    assert fact.source == {"kind": "origin", "implementation": "agent", "reason": "source_missing"}


def test_ported_follows_available_in_the_fact_order():
    ids = [fact.id for fact in compute_facts(ctx())]
    assert ids[:2] == ["implementation.available", "implementation.ported"]


def _partial_baseline(scored=19, **changes):
    return {"coverage": {"expected": 20, "scored": scored}, "min_coverage": 0.95,
            "stopped_reason": None, "unmeasured": [{"case_id": "c", "sample_id": "s",
            "outcome": "scorer_failed", "tries": 1}],
            "persistence": {"state": "saved", "failure": None}, **changes}


def test_baseline_accepted_at_95_percent():
    facts = by_id(compute_facts(ctx(operations=(evaluation(result=_partial_baseline()),))))
    assert facts["baseline.measured"].holds and facts["connected"].holds


def test_baseline_refused_below_threshold():
    assert baseline_failure(evaluation(result=_partial_baseline(18))) == "coverage"


def test_pre_dev60_result_needs_full_coverage():
    legacy = {**MEASURED, "coverage": {"expected": 20, "scored": 19}}
    assert baseline_failure(evaluation(result=legacy)) == "coverage"
    assert baseline_failure(evaluation(result=MEASURED)) is None


def test_stopped_run_never_accepted():
    assert baseline_failure(evaluation(result=_partial_baseline(stopped_reason="budget_exhausted"))) == "stop_reason"
    assert baseline_failure(evaluation(result=_partial_baseline(persistence={"state": "failed"}))) == "persistence"


# Environment binding regressions: the request is deliberately outside consumed roots.
from test_projects_session import session_env  # noqa: E402,F401


@pytest.fixture
def environment_facts(session_env, tmp_path):
    import json
    from pathlib import Path
    session, projects, owner = session_env
    lab = Path(projects.get("acq").working_copy.path) / "lab"
    entry = lab / "bundle-request.json"
    payload = lab / "payload"
    payload.mkdir()
    for path in tuple(lab.iterdir()):
        if path not in (entry, payload):
            path.rename(payload / path.name)
    request = json.loads(entry.read_text())
    request["source"]["manifest_path"] = "payload/manifest.json"
    request["source"]["launch_path"] = "payload/launch.json"
    request["roots"][1]["source_path"] = "payload"
    wheel = tmp_path / "agent-1.whl"
    wheel.write_bytes(b"wheel one")
    runtime = tmp_path / "settings.dat"
    runtime.write_bytes(b"runtime one")
    request["environments"][0].update(wheel_paths=[str(wheel)], runtime_files=[str(runtime)])
    entry.write_text(json.dumps(request))
    return session, projects, owner, entry, request, wheel, runtime


async def _environment_check(env):
    from evolution_lab.owner.models import ClientTask
    from evolution_lab.projects.session import client_ref
    session, _, owner, *_ = env
    summary, link = await session.bundle("acq")
    await session.start_target("acq", None, bundle_digest=summary.digest, secrets={})
    owner.tasks.append(ClientTask("k1", "check", "succeeded", "t1", summary.digest, client_ref("acq", "agent")))
    assert (await _environment_fact(session)).holds
    return link


async def _environment_fact(session):
    return by_id((await session.facts("acq"))[1])["check.passed"]


@pytest.mark.parametrize("service", [False, True])
async def test_rebuilt_wheel_makes_facts_stale(environment_facts, service):
    import json
    from types import SimpleNamespace
    from evolution_lab.projects.package import read_package_entry
    env = environment_facts
    await _environment_check(env)
    if service:
        # Current configuration resolves the marker to the same declared execution role.
        resolved = read_package_entry(env[3]).environments[0]
        env[0]._service_environment = SimpleNamespace(input=resolved, digest="unused-for-facts")
        env[4]["environments"] = [{"kind": "service"}]
        env[3].write_text(json.dumps(env[4]))
        assert (await _environment_fact(env[0])).holds
    env[5].write_bytes(b"wheel two")
    fact = await _environment_fact(env[0])
    assert fact.stale_reason.codes == ("inputs_changed",)
    assert fact.stale_reason.changes.modified == (f"@{env[5]}",)


async def test_runtime_file_change_makes_facts_stale(environment_facts):
    env = environment_facts
    await _environment_check(env)
    env[6].write_bytes(b"runtime two")
    fact = await _environment_fact(env[0])
    assert fact.stale_reason.codes == ("inputs_changed",)
    assert fact.stale_reason.changes.modified == (f"@{env[6]}",)


async def test_new_wheel_path_makes_facts_stale(environment_facts):
    import json
    env = environment_facts
    await _environment_check(env)
    new = env[5].with_name("agent-2.whl")
    new.write_bytes(env[5].read_bytes())
    env[4]["environments"][0]["wheel_paths"] = [str(new)]
    env[3].write_text(json.dumps(env[4]))
    fact = await _environment_fact(env[0])
    assert not fact.holds and fact.stale_reason.codes == ("inputs_changed",)
    assert env[5].read_bytes() == b"wheel one"
    # The existing human acceptance must bind the newly selected wheel's bytes, too.
    session = env[0]
    state = fact.state
    accepted = await session.accept("acq", None, only=["check.passed"], across_pairs=False,
                                    reason="reviewed", channel="cli", author="human", dry_run=False)
    carried = await _environment_fact(session)
    assert carried.holds and carried.carried_over[0]["acceptance"] == accepted.id
    new.write_bytes(b"wheel three")
    again = await _environment_fact(session)
    assert not again.holds and again.state["inputs_digest"] != state["inputs_digest"]
    assert again.stale_reason.changes.modified == (f"@{new}",)


async def test_interpreter_path_change_makes_facts_stale(environment_facts):
    import json
    env = environment_facts
    await _environment_check(env)
    env[4]["environments"][0]["executable"] += "-other"
    env[3].write_text(json.dumps(env[4]))
    fact = await _environment_fact(env[0])
    assert not fact.holds and fact.stale_reason.codes == ("inputs_changed",)
    assert fact.stale_reason.changes.empty  # no fictitious file source for declaration metadata


async def test_pre_dev60_binding_unaffected(environment_facts):
    import json
    from dataclasses import replace
    from evolution_lab.projects.inventory import inventory_digest
    env = environment_facts
    link = await _environment_check(env)
    session, projects = env[:2]
    # Historical records have the original byte-only digest and no companion metadata.
    entries = tuple(e for e in projects.store.inventory(link.binding["inputs"]["digest"])
                    if e.path not in (f"@{env[5]}", f"@{env[6]}"))
    digest = inventory_digest(entries)
    binding = {**link.binding, "inputs": {"digest": digest, "sources": [s for s in link.binding["inputs"]["sources"]
               if s["path"] not in (f"@{env[5]}", f"@{env[6]}")]}}
    projects.store.put_inventory(digest, entries)
    for recorded in projects.store.links("acq"):
        projects.store.upsert_link(replace(recorded, binding=binding))
    before = [json.loads(row[0])["binding"] for row in projects.store._execute("SELECT record_json FROM links ORDER BY seq")]
    assert set(binding) == {"working_copy", "materials_root", "place", "materials_revision", "inputs"}
    assert set(binding["inputs"]) == {"digest", "sources"}
    # Exercise an actual pre-dev60 minor3 registry: read-only does not migrate; the writer
    # backs it up and adds metadata storage without changing any historical binding.
    from evolution_lab.projects.store import RegistryStore
    import sqlite3
    database = projects.home / "projects.sqlite3"
    projects.store._execute("DROP TABLE input_environments")
    projects.store._execute("DROP TABLE approvals")
    projects.store._execute("PRAGMA user_version=1003")
    projects.store.close()
    with RegistryStore.open(database, read_only=True) as reader:
        assert reader.version == 1003 and reader.input_environment(digest) is None
        assert reader.inventory(digest) == entries
    projects.store = RegistryStore.open(database)
    assert projects.store.version == 1005
    assert projects.store.input_environment(digest) is None
    with sqlite3.connect(database.with_name(database.name + ".bak-1003")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1003
        assert [json.loads(row[0])["binding"] for row in backup.execute("SELECT record_json FROM links ORDER BY seq")] == before
    env[5].write_bytes(b"new wheel")
    env[6].write_bytes(b"new runtime")
    env[4]["environments"][0]["executable"] += "-other"
    env[3].write_text(json.dumps(env[4]))
    assert (await _environment_fact(session)).holds
    assert [json.loads(row[0])["binding"] for row in projects.store._execute("SELECT record_json FROM links ORDER BY seq")] == before
