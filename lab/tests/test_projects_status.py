"""Part 2 (spec §3.10): scenario status is one pure function over facts and link snapshots."""
from __future__ import annotations

from evolution_lab.guides import scenarios
from evolution_lab.guides._scenarios import read_catalogue
from evolution_lab.projects.models import Fact, Pair, StaleReason
from evolution_lab.projects.status import ArtifactCheck, CycleView, StatusInputs, scenario_status, status_payload
from scenario_fixtures import write_catalogue

PAIR, OLD = Pair("0.0.3.dev28", "0.3.0a17"), Pair("0.0.3.dev27", "0.3.0a17")
CATALOGUE = scenarios()
CONNECT, EVOLVE = CATALOGUE.descriptor("connect"), CATALOGUE.descriptor("evolve")


def fact(name, value=True, stale=None, carried=(), path=None, *, cid="c-1", created_at="2026-09-30T10:00:00Z",
         author="tester", channel="cli", subject=("implementation", "agent")):
    source = ({"kind": "confirmation", "id": cid, "attests": {"path": path, "digest": "d"}, "channel": channel,
               "author": author, "created_at": created_at}
              if path is not None else {"kind": "task", "id": "x"})
    return Fact(name, None if subject is None else {subject[0]: subject[1]}, value, source,
                None if stale is None else StaleReason(tuple(stale)), tuple(carried))


D = "sha256:" + "d" * 64
ANSWERS_CHECK = ("connect", "spec.answers.json", "spec-answers/1")


def inputs(*facts, artifacts=(), manifest_ok=False, prepared=(), cycles=(), moments=None, implementation="agent",
           checks=None):
    held = dict(artifacts) if isinstance(artifacts, dict) else {path: D for path in artifacts}
    return StatusInputs(PAIR, tuple(facts), held, manifest_ok, tuple(prepared), tuple(cycles),
                        **({} if moments is None else {"moments": moments}), implementation=implementation,
                        checks={} if checks is None else checks)


def steps(rows):
    return {row["id"]: row for row in rows}


DONE = {"outcome": "completed", "reason": "rounds_exhausted", "champion": "v1"}


def cycle(op, order, *, state="finished", resume=None, pair=PAIR, here=True, export=None, outcome="terminal", **extra):
    """A cycle; a settled one without a resume verdict is terminal by core's projection unless told otherwise."""
    if outcome == "terminal":
        outcome = DONE if resume is None and state not in ("reserved", "running", "stopping") else None
    return CycleView(op, pair, here, state, resume, "b" * 64, "p" * 64, export, order, outcome, **extra)


CONNECTED = (fact("implementation.available"), fact("package.valid", {"scope": "form"}), fact("check.passed"),
             fact("conformance.passed"), fact("baseline.measured"), fact("connected"))


def test_fresh_implementation_is_available_with_the_first_steps_ready():
    row = scenario_status(CONNECT, inputs(fact("implementation.available")))
    assert (row["state"], row["missing"], row["repeatable"]) == ("available", [], False)
    got = steps(row["steps"])
    assert [s["id"] for s in row["steps"]] == ["spec", "spec-confirm", "package", "check", "conformance", "baseline"]
    assert got["spec"] == {"id": "spec", "title": "Write the spec", "state": "ready", "optional": False,
                           "human_only": False, "missing": [], "blocked_reason": None, "invalid": [],
                           "stale_reason": None, "carried_over": [], "sources": [], "done_at": None}
    assert (got["spec-confirm"]["state"], got["spec-confirm"]["missing"]) == ("blocked", ["spec"])
    assert got["package"]["state"] == "ready"
    assert (got["check"]["state"], got["check"]["missing"]) == ("blocked", ["package"])
    assert (got["baseline"]["state"], got["baseline"]["missing"]) == ("blocked", ["check", "conformance"])


def test_missing_requirement_makes_the_scenario_unavailable():
    row = scenario_status(CONNECT, inputs(fact("implementation.available", False)))
    assert (row["state"], row["missing"]) == ("unavailable", ["implementation.available"])
    assert len(row["steps"]) == 6


def test_connected_implementation_has_produced_connect():
    row = scenario_status(CONNECT, inputs(*CONNECTED, fact("spec.confirmed", path="SPEC.md"), artifacts={"SPEC.md"},
                                          checks={ANSWERS_CHECK: ArtifactCheck("valid", digest=D)}))
    assert row["state"] == "produced"
    got = steps(row["steps"])
    assert {s["state"] for s in row["steps"]} == {"done"}
    assert got["package"]["sources"] == [{"fact": "package.valid", "confirmations": []}]
    assert got["spec"]["sources"] == [{"artifact": "SPEC.md", "digest": D}, {"artifact": "spec.answers.json", "digest": D}]
    assert got["spec-confirm"]["sources"] == [{"fact": "spec.confirmed", "confirmations": [
        {"id": "c-1", "author": "tester", "channel": "cli", "created_at": "2026-09-30T10:00:00Z"}]}]


def test_attestation_must_cover_the_step_path():
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), fact("spec.confirmed", path="OTHER.md"),
                                          artifacts={"SPEC.md"}, checks={ANSWERS_CHECK: ArtifactCheck("valid", digest=D)}))
    assert steps(row["steps"])["spec-confirm"]["state"] == "ready"


def test_stale_output_makes_the_step_stale_with_its_reason():
    facts = [f for f in CONNECTED if f.id not in ("check.passed", "connected")]
    row = scenario_status(CONNECT, inputs(*facts, fact("check.passed", stale=["inputs_changed"]), fact("connected", False)))
    assert row["state"] == "available"
    check = steps(row["steps"])["check"]
    assert check["state"] == "stale" and check["missing"] == [] and check["sources"] == []
    assert check["stale_reason"]["codes"] == ["inputs_changed"]


def test_a_held_output_is_done_whatever_its_preconditions():
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), fact("baseline.measured")))
    got = steps(row["steps"])
    assert got["check"]["state"] == "blocked" and got["baseline"]["state"] == "done"


def test_a_failed_operation_fact_is_not_an_output():
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), fact("package.valid", {"scope": "form"}),
                                          fact("check.passed", False)))
    assert steps(row["steps"])["check"]["state"] == "ready"


def test_carried_over_evidence_is_done_and_labelled():
    link = {"acceptance": "a-1", "reason": "rename", "across_pairs": False}
    facts = [f for f in CONNECTED if f.id != "check.passed"]
    row = scenario_status(CONNECT, inputs(*facts, fact("check.passed", carried=[link])))
    check = steps(row["steps"])["check"]
    assert check["state"] == "done" and check["carried_over"] == [link]


def test_evolve_needs_connected():
    row = scenario_status(EVOLVE, inputs(fact("implementation.available")))
    assert (row["state"], row["missing"], row["repeatable"]) == ("unavailable", ["connected"], True)
    assert "steps" not in row and row["cycles"] == []


def test_next_cycle_follows_manifest_and_prepared():
    row = scenario_status(EVOLVE, inputs(*CONNECTED))
    got = steps(row["next_cycle"]["steps"])
    assert row["state"] == "available"
    assert [(s, got[s]["state"], got[s]["missing"]) for s in ("plan", "prepare", "run", "result")] == [
        ("plan", "ready", []), ("prepare", "blocked", ["plan"]), ("run", "blocked", ["prepare"]),
        ("result", "blocked", ["run"])]
    row = scenario_status(EVOLVE, inputs(*CONNECTED, manifest_ok=True, prepared=("1" * 64, "2" * 64)))
    got = steps(row["next_cycle"]["steps"])
    assert got["plan"]["state"] == "done" and got["plan"]["sources"] == [{"artifact": "@manifest"}]
    assert got["prepare"]["state"] == "done" and got["prepare"]["sources"] == [{"prepared": "2" * 64}]
    assert (got["run"]["state"], got["result"]["state"]) == ("ready", "blocked")


def test_active_operation_is_the_current_cycle():
    running = cycle("op-2", 2, state="running")
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1), running)))
    first, second = row["cycles"]
    assert (first["operation_id"], first["current"], second["current"]) == ("op-2", True, False)
    got = steps(first["steps"])
    assert got["plan"]["sources"] == [{"bundle": "b" * 64}] and got["prepare"]["sources"] == [{"prepared": "p" * 64}]
    assert (got["run"]["state"], got["result"]["state"], got["result"]["missing"]) == ("ready", "blocked", ["run"])


def test_latest_resumable_is_current_when_nothing_runs():
    ready, cleanup = {"state": "ready", "reason": "r"}, {"state": "cleanup_first", "reason": "c"}
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(
        cycle("op-1", 1, resume=ready), cycle("op-2", 2, state="released", resume=cleanup), cycle("op-3", 3))))
    assert [(c["operation_id"], c["current"]) for c in row["cycles"]] == [("op-2", True), ("op-3", False), ("op-1", False)]
    assert row["cycles"][0]["resume"] == cleanup and row["cycles"][0]["control_state"] == "released"


def test_no_current_cycle_without_active_or_resumable():
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1, resume={"state": "refused", "reason": "x"}),)))
    assert [c["current"] for c in row["cycles"]] == [False]


def test_a_cycle_from_another_state_root_is_never_current():
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1, state="running", here=False),)))
    assert row["cycles"][0]["current"] is False and row["cycles"][0]["detail"] == "available"


def test_previous_pair_cycles_are_unavailable_snapshots():
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1, pair=OLD, here=False, export="e" * 64),)))
    only = row["cycles"][0]
    assert (only["detail"], only["current"], only["pair"]) == ("unavailable", False, OLD.to_payload())
    got = steps(only["steps"])
    assert got["run"]["state"] == "done" and got["result"]["sources"] == [{"export": "e" * 64, "results_state": "present"}]


def test_result_is_ready_after_a_terminal_run_without_export():
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1),)))
    got = steps(row["cycles"][0]["steps"])
    assert (got["run"]["state"], got["run"]["sources"]) == ("done", [{"operation": "op-1"}])
    assert got["result"]["state"] == "ready"


def test_status_payload_shape_and_scenario_filter():
    value = status_payload(project_id="acq", implementation_id="agent", catalogue=CATALOGUE, inputs=inputs(*CONNECTED))
    assert list(value) == ["schema", "project", "implementation", "pair", "catalogue", "facts", "scenarios"]
    assert value["schema"] == "lab-project-status/6" and value["pair"] == PAIR.to_payload()
    assert value["catalogue"] == {"version": 12, "digest": CATALOGUE.catalogue["digest"]}
    assert value["facts"] == [f.to_payload() for f in CONNECTED]
    assert [s["id"] for s in value["scenarios"]] == ["connect", "create-skill", "evolve", "restructure"]
    only = status_payload(project_id="acq", implementation_id="agent", catalogue=CATALOGUE,
                          inputs=inputs(*CONNECTED), scenario="evolve")
    assert [s["id"] for s in only["scenarios"]] == ["evolve"]
    assert list(only["scenarios"][0]) == ["id", "version", "repeatable", "state", "missing", "next_cycle", "cycles"]
    assert list(only["scenarios"][0]["next_cycle"]["steps"][0]) == [
        "id", "title", "state", "optional", "human_only", "missing", "blocked_reason", "invalid", "stale_reason",
        "carried_over", "sources", "done_at"]
    assert list(value["scenarios"][0]) == ["id", "version", "repeatable", "state", "missing", "steps"]


def test_an_older_operation_resumed_later_becomes_current():
    """F1: the order is the Owner order of the latest start, not the link order — A's link is
    older, but A was resumed after B started, so A is current and listed first."""
    resumable = {"state": "ready"}
    a = cycle("A", 7, resume=resumable)
    b = cycle("B", 5, resume=resumable)
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(a, b)))
    assert [(c["operation_id"], c["current"]) for c in row["cycles"]] == [("A", True), ("B", False)]


def test_step_has_title_and_done_at():
    row = scenario_status(CONNECT, inputs(fact("check.passed"), moments={("fact", "check.passed"): "2026-09-29T10:00:00Z"}))
    check = steps(row["steps"])["check"]
    assert check["title"] == "Check the connection"  # from the connect scenario.json
    assert check["done_at"] == "2026-09-29T10:00:00Z"


def test_done_at_is_null_without_a_source_moment():
    row = scenario_status(CONNECT, inputs(fact("check.passed"), moments={}))
    assert steps(row["steps"])["check"]["done_at"] is None


def test_done_at_of_a_not_done_step_is_null():
    row = scenario_status(CONNECT, inputs(moments={("fact", "check.passed"): "2026-09-29T10:00:00Z"}))
    assert steps(row["steps"])["check"]["done_at"] is None


def test_cycle_outcome_and_run_done_at():
    outcome = {"outcome": "completed", "reason": "rounds_exhausted", "champion": "v1"}
    only = cycle("op-1", 1, outcome=outcome, last_event_at="2026-09-29T12:00:00Z")
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(only,)))["cycles"][0]
    assert row["outcome"]["reason"] == "rounds_exhausted"
    assert steps(row["steps"])["run"]["done_at"] == "2026-09-29T12:00:00Z"


def test_every_step_has_a_null_blocked_reason_unless_results_are_deleted():
    row = scenario_status(CONNECT, inputs(fact("implementation.available")))
    assert {step["blocked_reason"] for step in row["steps"]} == {None}
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=[cycle("op-1", 1)]))
    for steps_ in [row["next_cycle"]["steps"]] + [c["steps"] for c in row["cycles"]]:
        assert {step["blocked_reason"] for step in steps_} == {None}


def test_result_exported_then_deleted_stays_done_with_a_deleted_export():
    gone = cycle("op-1", 1, export="e" * 64, export_deleted=True, results_state="deleted")
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=[gone]))
    result = steps(row["cycles"][0]["steps"])["result"]
    assert (result["state"], result["blocked_reason"]) == ("done", None)
    assert result["sources"] == [{"export": "e" * 64, "results_state": "deleted"}]


def test_result_deleted_without_export_is_blocked_with_a_reason():
    gone = cycle("op-1", 1, results_state="deleted")
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=[gone]))
    got = steps(row["cycles"][0]["steps"])
    assert got["run"]["state"] == "done"
    assert (got["result"]["state"], got["result"]["missing"], got["result"]["blocked_reason"]) == (
        "blocked", [], {"code": "results_deleted"})
    assert got["result"]["sources"] == []


def test_deletion_in_progress_keeps_the_export_present():
    """Decision Г: deleting/delete_failed rows are not marked; no blocked_reason either."""
    for state in ("deleting", "delete_failed"):
        busy = cycle("op-1", 1, export="e" * 64, results_state=state)
        result = steps(scenario_status(EVOLVE, inputs(*CONNECTED, cycles=[busy]))["cycles"][0]["steps"])["result"]
        assert result["sources"] == [{"export": "e" * 64, "results_state": "present"}]
        bare = cycle("op-1", 1, results_state=state)
        result = steps(scenario_status(EVOLVE, inputs(*CONNECTED, cycles=[bare]))["cycles"][0]["steps"])["result"]
        assert (result["state"], result["blocked_reason"]) == ("ready", None)


def test_a_fact_source_lists_every_held_confirmation_in_time_order_and_done_at_is_the_last():
    project = fact("spec.confirmed", path="SPEC.md", cid="c-p", created_at="2026-09-30T10:00:05Z",
                   author="lead", channel="auto-evolve", subject=None)
    mine = fact("spec.confirmed", path="SPEC.md", cid="c-i", created_at="2026-09-30T10:00:01Z")
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), mine, project, artifacts={"SPEC.md"}))
    step = steps(row["steps"])["spec-confirm"]
    assert step["sources"] == [{"fact": "spec.confirmed", "confirmations": [
        {"id": "c-i", "author": "tester", "channel": "cli", "created_at": "2026-09-30T10:00:01Z"},
        {"id": "c-p", "author": "lead", "channel": "auto-evolve", "created_at": "2026-09-30T10:00:05Z"}]}]
    assert step["done_at"] == "2026-09-30T10:00:05Z"


def test_same_second_confirmations_order_by_id():
    a = fact("spec.confirmed", path="SPEC.md", cid="c-b", created_at="2026-09-30T10:00:00Z")
    b = fact("spec.confirmed", path="SPEC.md", cid="c-a", created_at="2026-09-30T10:00:00Z", subject=None)
    step = steps(scenario_status(CONNECT, inputs(fact("implementation.available"), a, b,
                                                 artifacts={"SPEC.md"}))["steps"])["spec-confirm"]
    assert [c["id"] for c in step["sources"][0]["confirmations"]] == ["c-a", "c-b"]


def test_a_confirmation_of_another_path_or_a_stale_one_is_not_listed():
    held = fact("spec.confirmed", path="SPEC.md", cid="c-1", created_at="2026-09-30T10:00:01Z")
    other = fact("spec.confirmed", path="OTHER.md", cid="c-2", created_at="2026-09-30T10:00:09Z", subject=None)
    stale = fact("spec.confirmed", path="SPEC.md", cid="c-3", created_at="2026-09-30T10:00:07Z",
                 stale=["digest_changed"], subject=None)
    step = steps(scenario_status(CONNECT, inputs(fact("implementation.available"), held, other, stale,
                                                 artifacts={"SPEC.md"}))["steps"])["spec-confirm"]
    assert [c["id"] for c in step["sources"][0]["confirmations"]] == ["c-1"]
    assert step["done_at"] == "2026-09-30T10:00:01Z"


def test_a_computed_fact_source_has_no_confirmations():
    row = scenario_status(CONNECT, inputs(*CONNECTED))
    assert steps(row["steps"])["check"]["sources"] == [{"fact": "check.passed", "confirmations": []}]


def test_steps_carry_optional_and_human_only():
    got = steps(scenario_status(EVOLVE, inputs(*CONNECTED))["next_cycle"]["steps"])
    assert [(s, got[s]["optional"], got[s]["human_only"]) for s in ("plan", "prepare", "run", "result")] == [
        ("plan", False, False), ("prepare", False, False), ("run", False, True), ("result", True, False)]
    assert all(step["invalid"] == [] for step in got.values())


def test_a_terminal_cycle_is_complete_without_its_optional_export():
    only = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1, position=1),)))["cycles"][0]
    assert (steps(only["steps"])["result"]["state"], only["complete"], only["order"]) == ("ready", True, 1)


def test_a_suspended_cycle_is_not_complete_and_run_says_why():
    suspended = cycle("op-1", 1, resume={"state": "ready", "reason": None, "target": None})
    only = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(suspended,)))["cycles"][0]
    got = steps(only["steps"])
    assert (got["run"]["state"], got["run"]["blocked_reason"], got["run"]["missing"]) == (
        "blocked", {"code": "campaign_suspended"}, [])
    assert got["result"]["missing"] == ["run"] and only["complete"] is False


def test_a_settled_cycle_without_a_core_projection_is_unknown():
    only = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(cycle("op-1", 1, outcome=None),)))["cycles"][0]
    run = steps(only["steps"])["run"]
    assert (run["state"], run["blocked_reason"], only["complete"]) == (
        "blocked", {"code": "campaign_state_unknown"}, False)


def test_an_active_cycle_runs_without_a_blocked_reason():
    running = cycle("op-1", 1, state="running")
    run = steps(scenario_status(EVOLVE, inputs(*CONNECTED, cycles=(running,)))["cycles"][0]["steps"])["run"]
    assert (run["state"], run["blocked_reason"]) == ("ready", None)


def test_cycle_order_is_the_position_whatever_the_listing_order():
    cycles = (cycle("op-1", 7, position=1, resume={"state": "ready"}), cycle("op-2", 3, position=2))
    row = scenario_status(EVOLVE, inputs(*CONNECTED, cycles=cycles))
    assert [(c["operation_id"], c["order"]) for c in row["cycles"]] == [("op-1", 1), ("op-2", 2)]
    assert list(row["cycles"][0]) == ["operation_id", "order", "pair", "current", "detail", "control_state",
                                      "resume", "outcome", "complete", "steps"]


MAP = "restructure/port-a/behavior-map.json"
MAP_CHECK = ("restructure", MAP, "behavior-map/1")  # (scenario, substituted path, schema)


def _restructure(tmp_path):
    return read_catalogue(write_catalogue(tmp_path / "c")).descriptor("restructure")


def _ported(*more, **extra):
    return inputs(fact("implementation.ported"), *more, implementation="port-a", **extra)


def test_a_valid_schema_artifact_is_done_at_the_substituted_path(tmp_path):
    row = scenario_status(_restructure(tmp_path), _ported(checks={MAP_CHECK: ArtifactCheck("valid", digest=D)}))
    got = steps(row["steps"])
    assert (row["state"], got["map"]["state"], got["map"]["sources"]) == (
        "available", "done", [{"artifact": MAP, "digest": D}])
    assert (got["map-confirm"]["state"], got["map-confirm"]["human_only"]) == ("ready", True)


def test_an_invalid_artifact_blocks_with_its_json_pointers(tmp_path):
    invalid = tuple({"path": f"/behaviors/{i}", "message": "m"} for i in range(5))
    checks = {MAP_CHECK: ArtifactCheck("invalid", invalid, digest=D)}
    step = steps(scenario_status(_restructure(tmp_path), _ported(checks=checks))["steps"])["map"]
    assert (step["state"], step["blocked_reason"], step["missing"]) == ("blocked", {"code": "artifact_invalid"}, [])
    assert step["invalid"] == [{"artifact": MAP, "digest": D, **item} for item in invalid] and step["sources"] == []


def test_a_failing_schema_blocks_with_its_name_and_the_status_still_answers(tmp_path):
    checks = {MAP_CHECK: ArtifactCheck("schema_unusable", schema="behavior-map/1")}
    got = steps(scenario_status(_restructure(tmp_path), _ported(checks=checks))["steps"])
    assert (got["map"]["state"], got["map"]["blocked_reason"], got["map"]["invalid"]) == (
        "blocked", {"code": "schema_unusable", "schema": "behavior-map/1"}, [])
    assert got["notes"]["state"] == "ready"


def test_an_unusable_schema_first_blocks_without_the_later_outputs_invalid_pointers(tmp_path):
    from test_service_cli_projects import _status_with
    from evolution_lab.cli.responses import validate
    descriptor = _restructure(tmp_path)
    descriptor["steps"][0]["outputs"].append(
        {"artifact": {"path": "restructure/{implementation}/second.json", "check": "schema", "schema": "behavior-map/1"}})
    checks = {MAP_CHECK: ArtifactCheck("schema_unusable", schema="behavior-map/1"),
              ("restructure", "restructure/port-a/second.json", "behavior-map/1"): ArtifactCheck("invalid", ({"path": "/x", "message": "m"},))}
    step = steps(scenario_status(descriptor, _ported(checks=checks))["steps"])["map"]
    assert (step["state"], step["blocked_reason"], step["invalid"]) == (
        "blocked", {"code": "schema_unusable", "schema": "behavior-map/1"}, [])
    validate(_status_with(step), "lab-project-status/6")


def test_a_missing_schema_artifact_is_ready(tmp_path):
    got = steps(scenario_status(_restructure(tmp_path), _ported())["steps"])
    assert (got["map"]["state"], got["map"]["blocked_reason"]) == ("ready", None)


def test_another_implementation_does_not_see_the_ports_artifacts(tmp_path):
    other = inputs(fact("implementation.ported"), implementation="port-b",
                   checks={MAP_CHECK: ArtifactCheck("valid")}, artifacts={"restructure/port-a/NOTES.md"})
    got = steps(scenario_status(_restructure(tmp_path), other)["steps"])
    assert (got["map"]["state"], got["notes"]["state"]) == ("ready", "ready")


def test_an_attestation_is_matched_on_the_substituted_path(tmp_path):
    confirmed = fact("restructure.map_confirmed", path=MAP)
    row = scenario_status(_restructure(tmp_path), _ported(confirmed, checks={MAP_CHECK: ArtifactCheck("valid")}))
    assert steps(row["steps"])["map-confirm"]["state"] == "done" and row["state"] == "produced"


def test_restructure_is_unavailable_on_the_source(tmp_path):
    row = scenario_status(_restructure(tmp_path), inputs(fact("implementation.ported", False), implementation="agent"))
    assert (row["state"], row["missing"]) == ("unavailable", ["implementation.ported"])


SECOND = "restructure/port-a/second.json"


def _two_files(tmp_path, *, same_file=False):
    descriptor = _restructure(tmp_path)
    second = "restructure/{implementation}/" + ("behavior-map.json" if same_file else "second.json")
    descriptor["steps"][0]["outputs"].append(
        {"artifact": {"path": second, "check": "schema", "schema": "notes/1" if same_file else "behavior-map/1"}})
    return descriptor


def test_invalid_records_name_their_file_and_the_cap_is_per_file(tmp_path):
    first = tuple({"path": f"/behaviors/{i}", "message": "m"} for i in range(5))
    checks = {MAP_CHECK: ArtifactCheck("invalid", first, digest="sha256:" + "1" * 64),
              ("restructure", SECOND, "behavior-map/1"): ArtifactCheck(
                  "invalid", ({"path": "", "message": "the file is not UTF-8 JSON"},), digest="sha256:" + "2" * 64)}
    step = steps(scenario_status(_two_files(tmp_path), _ported(checks=checks))["steps"])["map"]
    assert [(r["artifact"], r["digest"][-1]) for r in step["invalid"]] == [(MAP, "1")] * 5 + [(SECOND, "2")]
    assert list(step["invalid"][0]) == ["artifact", "digest", "path", "message"]


def test_one_file_under_two_schemas_keeps_one_cap_and_no_duplicates(tmp_path):
    shared = ({"path": "/a", "message": "m"}, {"path": "/b", "message": "m"}, {"path": "/c", "message": "m"})
    other = ({"path": "/a", "message": "m"}, {"path": "/d", "message": "m"}, {"path": "/e", "message": "m"})
    checks = {MAP_CHECK: ArtifactCheck("invalid", shared, digest=D),
              ("restructure", MAP, "notes/1"): ArtifactCheck("invalid", other, digest=D)}
    step = steps(scenario_status(_two_files(tmp_path, same_file=True), _ported(checks=checks))["steps"])["map"]
    assert [r["path"] for r in step["invalid"]] == ["/a", "/b", "/c", "/d", "/e"]
    assert {r["digest"] for r in step["invalid"]} == {D}


def test_an_exists_source_carries_its_digest_and_a_null_one_when_unread():
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), artifacts={"SPEC.md": None}))
    assert steps(row["steps"])["spec"]["sources"] == [{"artifact": "SPEC.md", "digest": None}]
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), artifacts=["SPEC.md"]))
    assert steps(row["steps"])["spec"]["sources"] == [{"artifact": "SPEC.md", "digest": D}]


def test_the_status_schema_is_6():
    assert status_payload(project_id="p", implementation_id="agent", catalogue=CATALOGUE,
                          inputs=inputs(fact("implementation.available")), scenario=None)["schema"] == \
        "lab-project-status/6"


def test_files_come_in_the_order_of_their_first_output_even_when_it_is_valid(tmp_path):
    # Outputs: A under S1 (valid), B (invalid), A under S2 (invalid). Spec §2.2: A is the first file of the step.
    descriptor = _two_files(tmp_path)
    descriptor["steps"][0]["outputs"].append(
        {"artifact": {"path": "restructure/{implementation}/behavior-map.json", "check": "schema", "schema": "notes/1"}})
    checks = {MAP_CHECK: ArtifactCheck("valid", digest=D),
              ("restructure", SECOND, "behavior-map/1"): ArtifactCheck(
                  "invalid", ({"path": "/b", "message": "m"},), digest=D),
              ("restructure", MAP, "notes/1"): ArtifactCheck("invalid", ({"path": "/a", "message": "m"},), digest=D)}
    step = steps(scenario_status(descriptor, _ported(checks=checks))["steps"])["map"]
    assert [r["artifact"] for r in step["invalid"]] == [MAP, SECOND]


RESTRUCTURE = CATALOGUE.descriptor("restructure")
SCOPE = "restructure/port-a/scope.json"
SCOPE_CHECK = ("restructure", SCOPE, "scope/1")
RS_MAP = "restructure/port-a/behavior-map.json"
RS_GOAL = "restructure/port-a/goal.json"
RS_SPEC = "restructure/port-a/port-spec.json"
VALID = {("restructure", RS_MAP, "behavior-map/2"): ArtifactCheck("valid"),
         ("restructure", RS_GOAL, "goal/2"): ArtifactCheck("valid"),
         ("restructure", RS_SPEC, "port-spec/2"): ArtifactCheck("valid")}


def _port(*more, **extra):
    return inputs(fact("implementation.ported"), *more, implementation="port-a", **extra)


def test_restructure_on_the_source_is_unavailable():
    row = scenario_status(RESTRUCTURE, inputs(fact("implementation.ported", False), fact("implementation.available")))
    assert (row["state"], row["missing"]) == ("unavailable", ["implementation.ported"])


def test_restructure_on_a_fresh_port_starts_with_the_analysis():
    got = steps(scenario_status(RESTRUCTURE, _port(fact("implementation.available")))["steps"])
    assert got["analysis"]["state"] == "ready"
    assert (got["goal"]["state"], got["goal"]["missing"]) == ("blocked", ["analysis-confirm"])
    assert all(got[s]["human_only"] for s in ("analysis-confirm", "brief-confirm", "accept"))


def test_an_invalid_scope_blocks_the_brief_step():
    invalid = ({"path": "/hide/0/path", "message": "bad"},)
    confirmed = fact("restructure.map_confirmed", path=RS_MAP)
    got = steps(scenario_status(RESTRUCTURE, _port(fact("implementation.available"), confirmed,
                                                   artifacts={RS_MAP, RS_GOAL, RS_SPEC, SCOPE},
                                                   checks={**VALID, SCOPE_CHECK: ArtifactCheck("invalid", invalid)}))["steps"])
    assert (got["goal"]["state"], got["brief"]["state"], got["brief"]["blocked_reason"]) == (
        "done", "blocked", {"code": "artifact_invalid"})


def test_build_waits_for_the_port_place():
    brief = fact("restructure.brief_confirmed", path=SCOPE)
    got = steps(scenario_status(RESTRUCTURE, _port(brief, fact("implementation.available", False)))["steps"])
    assert got["build"]["state"] == "blocked" and "implementation.available" in got["build"]["missing"]


def test_an_accepted_port_produces_the_scenario():
    accepted = fact("restructure.port_accepted", path="restructure/port-a/verify.json")
    row = scenario_status(RESTRUCTURE, _port(fact("implementation.available"), accepted,
                                             checks={("restructure", "restructure/port-a/verify.json", "verify/1"):
                                                     ArtifactCheck("valid")}))
    assert row["state"] == "produced" and steps(row["steps"])["accept"]["state"] == "done"


def test_connect_2_spec_has_two_outputs():
    assert CONNECT["version"] == 8 and CONNECT["schemas"] == {"spec-answers/1": "schemas/spec-answers-1.json"}
    spec = next(s for s in CONNECT["steps"] if s["id"] == "spec")
    assert spec["outputs"] == [
        {"artifact": {"path": "SPEC.md", "check": "exists"}},
        {"artifact": {"path": "spec.answers.json", "check": "schema", "schema": "spec-answers/1"}}]


def test_spec_without_answers_is_ready_and_an_old_confirmation_still_holds():
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), fact("spec.confirmed", path="SPEC.md"),
                                          artifacts={"SPEC.md"}))
    got = steps(row["steps"])
    assert got["spec"]["state"] == "ready" and got["spec-confirm"]["state"] == "done"


def test_valid_answers_make_spec_done_with_both_sources():
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), artifacts={"SPEC.md"},
                                          checks={ANSWERS_CHECK: ArtifactCheck("valid", digest=D)}))
    got = steps(row["steps"])["spec"]
    assert got["state"] == "done"
    assert got["sources"] == [{"artifact": "SPEC.md", "digest": D}, {"artifact": "spec.answers.json", "digest": D}]


def test_stale_catalogue_blocks_spec_with_the_place():
    records = ({"path": "/catalogue/digest", "message": "'abc' was expected"},)
    row = scenario_status(CONNECT, inputs(fact("implementation.available"), artifacts={"SPEC.md"},
                                          checks={ANSWERS_CHECK: ArtifactCheck("invalid", records, digest=D)}))
    got = steps(row["steps"])["spec"]
    assert (got["state"], got["blocked_reason"]) == ("blocked", {"code": "artifact_invalid"})
    assert got["invalid"] == [{"artifact": "spec.answers.json", "digest": D, "path": "/catalogue/digest",
                               "message": "'abc' was expected"}]
