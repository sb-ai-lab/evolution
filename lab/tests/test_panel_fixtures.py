import httpx
import pytest

from panel_fixtures import CURRENT, LEGACY, FIXTURES, SNAPSHOTS, canonical, current_form, load_snapshot, mock_upstream

LAB_SHA256 = "196d8c4fc02ee6dce5514159a1add04539a6fa96dc7a0a0251f731f8822e7cb7"
#: The accepted c58r1final4 dev58 candidate produces native snapshots; defaults retain LAB_SHA256.
FINAL_LAB_SHA256 = "8e0e6bf34bd2d7fd3d6549f6d804bd112e4b504f06df315f2e49f9f4f2780370"
CORE_SHA256 = "bce365df308b54cc73eb2ff724e3d5843263d666f5359da31b1197b419763d44"
CURRENT_CORE_SHA256 = "55eb763dcacda415641a3be3fd00c78e0922bd96b85dc933a9a8e3aec61b1a93"
B6_SHA256 = "acdbc710902ea448fe1f22308250ba35768df5ee1cf4669c5b6751cb46514ec0"


@pytest.mark.parametrize("schema", ["lab-campaign-view/15", "lab-measurement/3", "lab-measurement-list/3"])
def test_notice_adapter_preserves_closed_current_forms(schema):
    # Missing /3 fields are invalid evidence, never repaired by a historical adapter.
    key = "measurement" if schema == "lab-measurement/3" else "measurements"
    body = {"schema": schema, key: {"role": "baseline"} if key == "measurement" else [{"role": "baseline"}]}
    assert current_form(body) == body


@pytest.mark.parametrize("schema", ["lab-measurement/2", "lab-measurement-list/2"])
def test_notice_adapter_only_augments_old_measurement_forms(schema):
    from copy import deepcopy

    key = "measurement" if schema == "lab-measurement/2" else "measurements"
    body = {"schema": schema, key: {"role": "evaluation"} if key == "measurement" else [{"role": "evaluation"}]}
    original = deepcopy(body)
    adapted = current_form(body)
    rows = [adapted[key]] if key == "measurement" else adapted[key]
    assert adapted["schema"] == schema.replace("/2", "/3")
    assert rows == [{"role": "evaluation", "lab_notices": [], "lab_notices_state": "unavailable"}]
    assert body == original


def test_canonical_sorts_query_and_unquotes_epoch():
    assert canonical("/v1/operations/o/diff?to=attempt:r01-a01&from=champion:v0") == \
        "/v1/operations/o/diff?from=champion:v0&to=attempt:r01-a01"
    assert canonical("/v1/operations/o/measurements/m-r01%40e1") == "/v1/operations/o/measurements/m-r01@e1"
    assert canonical("/v1/projects") == "/v1/projects"


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_every_snapshot_has_the_shell_routes(name):
    routes = load_snapshot(name)["routes"]
    for path in ("/v1/health", "/v1/projects", "/v1/operations?view=summary"):
        assert routes[path]["status"] == 200


def _status(snapshot, key):
    data = load_snapshot(snapshot)
    return data["routes"][f"/v1/operations/{data['meta']['ids'][key]}"]["body"]


def test_running_snapshot_has_a_live_campaign_with_an_open_attempt():
    data = load_snapshot("running")
    op = data["meta"]["ids"]["campaign_running"]
    assert data["routes"][f"/v1/operations/{op}"]["body"]["control_state"] in {"reserved", "running", "stopping"}
    view = data["routes"][f"/v1/operations/{op}/campaign"]["body"]
    assert any(a["group"] == "in_progress" for r in view["rounds"] for a in r["attempts"])
    assert any("event: status" in frame for frame in data["events"][op])


def test_suspended_snapshots_differ_by_target_requirement():
    assert _status("suspended-target", "campaign_suspended_target")["resume"]["target"]["requirement"] == "fresh_instance"
    role = _status("suspended-role", "campaign_suspended_role")["resume"]
    assert role is not None and role["target"] is None


def test_final_snapshot_has_deleted_results():
    body = _status("final", "campaign_finished")
    assert body["results_state"] == "deleted"


async def test_mock_upstream_serves_routes_events_and_404():
    data = load_snapshot("running")
    op = data["meta"]["ids"]["campaign_running"]
    async with httpx.AsyncClient(transport=mock_upstream("running"), base_url="http://lab") as client:
        assert (await client.get("/v1/health")).status_code == 200
        events = await client.get(f"/v1/operations/{op}/events")
        assert events.headers["content-type"].startswith("text/event-stream") and "event: status" in events.text
        missing = await client.get("/v1/nowhere")
        assert missing.status_code == 404 and missing.json()["error"]["stage"] == "fixture"


def test_details_snapshot_has_a_mixed_round():
    snap = load_snapshot("details")
    op = snap["meta"]["ids"]["campaign_mixed"]
    view = snap["routes"][f"/v1/operations/{op}/campaign"]["body"]
    attempts = view["rounds"][0]["attempts"]
    assert len(attempts) == 2
    assert {a["group"] for a in attempts} - {"accepted"}, "the declined Editor is the non-accepted attempt"
    assert any(a["group"] == "accepted" for a in attempts)
    assert any(a["failure"] for a in attempts)


def test_initial_to_champion_diffs_are_captured():
    snap = load_snapshot("suspended-target")
    op = snap["meta"]["ids"]["campaign_finished"]
    view = snap["routes"][f"/v1/operations/{op}/campaign"]["body"]
    for champion in view["champions"][1:]:
        assert canonical(f"/v1/operations/{op}/diff?from=initial&to=champion:{champion['version']}") in snap["routes"]


def test_snapshots_carry_the_current_service_forms():
    routes = load_snapshot("final")["routes"]
    assert routes["/v1/projects/demo/status"]["body"]["schema"] == "lab-project-status/6"
    assert routes["/v1/projects/demo/facts"]["body"]["schema"] == "lab-project-facts/4"
    assert routes["/v1/projects/demo"]["body"]["schema"] == "lab-project/2"
    assert routes["/v1/projects"]["body"]["schema"] == "lab-project-list/2"
    campaign = next(v for k, v in routes.items() if k.endswith("/campaign") and v["status"] == 200)
    assert campaign["body"]["schema"] == "lab-campaign-view/15"
    measurements = next(v for k, v in routes.items() if k.endswith("/measurements") and v["status"] == 200)
    assert measurements["body"]["schema"] == "lab-measurement-list/3"
    assert all("repairs" in row for row in measurements["body"]["measurements"])


def test_final_snapshot_shows_the_threshold_repairs_in_lab_rows():
    """The #78 effect through Lab's own form: the resumed, completed campaign's champion row carries
    core's repair counters and no pair is excluded."""
    routes = load_snapshot("final")["routes"]
    ops = routes["/v1/operations"]["body"]["operations"]
    completed = [op["id"] for op in ops
                 if (((op.get("result") or {}).get("core") or {}).get("campaign_outcome") or {}).get("outcome") == "completed"]
    assert completed, "no completed campaign in the final snapshot"
    body = routes[f"/v1/operations/{completed[0]}/measurements"]["body"]
    assert body["schema"] == "lab-measurement-list/3"
    row = next(r for r in body["measurements"] if r["measurement_id"] == "m-r00-champion")
    assert row["role"] == "baseline"
    assert row["repairs"]["tries"] >= 1 and row["repairs"]["pairs_scored"] >= 1
    assert row["coverage"]["excluded_pairs"] == 0


def test_captured_campaign_views_keep_provenance_and_load_as_view_12():
    """Native /10 bytes keep dev58/b10 provenance; all old forms adapt only in memory."""
    seen = {14: 0, 15: 0}
    pairs = set()
    for name in CURRENT + LEGACY:
        snap = load_snapshot(name)
        pairs.add((name in CURRENT, snap["meta"]["lab_version"], snap["meta"]["core_version"],
                   snap["meta"]["producing_wheels"]["lab"]["sha256"],
                   snap["meta"]["producing_wheels"]["core"]["sha256"]))
        for path, answer in snap["routes"].items():
            if path.endswith("/campaign") and answer["status"] == 200:
                view = answer["body"]
                assert view["schema"] == "lab-campaign-view/15" and view["family"] == (15 if name == "family15" else 14)
                seen[view["family"]] += 1
                if view["family"] == 14:
                    assert view["gate_mode"] == "observe" and "independent" not in view
                assert "alpha_total" not in view
                assert all("cohort" not in r for r in view["rounds"])
    assert pairs == {(False, "0.0.3.dev55", "2.0.0b8", LAB_SHA256, CORE_SHA256),
                     (True, "0.0.3.dev58", "2.0.0b10", FINAL_LAB_SHA256, CURRENT_CORE_SHA256)}
    assert seen[14] >= 5 and seen[15] == 1


def test_suspension_fault_snapshot_is_historical_dev52_b6():
    """User decision 2026-10-19: the scorer-fault snapshot stays the dev52/b6 capture (historical input, read by
    the current adapter); it is not a current-pair capture and its campaign view is the old /7."""
    meta = load_snapshot("suspension-fault")["meta"]
    assert (meta["lab_version"], meta["core_version"], meta["producing_wheels"]["core"]["sha256"]) == \
        ("0.0.3.dev52", "2.0.0b6", B6_SHA256)


def test_family14_snapshot_has_a_promotion_and_a_resumed_connection_pause():
    """Real answers of two /15 observe demo campaigns (spec 2026-10-13 §6.7): one with a finalist accepted and
    promoted; one suspended by core's connection check (``connection_incompatible``), resumed and finished."""
    snap = load_snapshot("family14")
    ids = snap["meta"]["ids"]
    assert snap["meta"]["core_version"] == "2.0.0b10"
    final = snap["routes"][f"/v1/operations/{ids['campaign_final']}/campaign"]["body"]
    assert final["schema"] == "lab-campaign-view/15" and final["family"] == 14
    assert any(a["final_decision"] and a["final_decision"]["state"] == "accepted"
               for r in final["rounds"] for a in r["attempts"])
    for rnd in final["rounds"]:
        for block in rnd["final"] or []:
            for member in block["members"]:
                path = f"/v1/operations/{ids['campaign_final']}/measurements/{member['measurement_id']}"
                answer = snap["routes"][path]
                assert answer["status"] == 200
                assert answer["body"]["measurement"]["measurement_id"] == member["measurement_id"]
            if block["selection"] is not None:
                champion = next(c for c in final["champions"] if c["attempt_id"] == block["selection"]["attempt_id"])
                assert champion["measurement_id"] == block["selection"]["measurement_id"]
            assert all(c["lower"] is None and c["level"] is None and c["exact"] is None
                       for c in block["contrasts"])
    assert any(r["reflection"]["state"] == "absent" and r["reflection"]["reflection_id"] is None
               for r in final["rounds"])
    connection = snap["routes"][f"/v1/operations/{ids['campaign_connection']}/campaign"]["body"]
    assert connection["connection"]["outcome"] == "compatible" and connection["connection"]["attempts"] == 2
    assert [s["reason"] for s in connection["suspensions"]] == ["connection_incompatible"]


def test_every_crawled_campaign_includes_suspension_history():
    for name in SNAPSHOTS:
        routes = load_snapshot(name)["routes"]
        for path in routes:
            if path.endswith("/campaign"):
                assert path.removesuffix("/campaign") + "/suspensions" in routes


def test_scorer_fault_snapshot_has_real_fault_rows():
    snap = load_snapshot("suspension-fault")
    operation = snap["meta"]["ids"]["campaign_scorer_fault"]
    answer = snap["routes"][f"/v1/operations/{operation}/suspensions"]
    assert answer["status"] == 200
    assert answer["body"]["source"] == "core_raw"
    assert answer["body"]["state"] == "available"
    rows = [entry for entry in answer["body"]["suspensions"] if entry["evidence_kind"] == "fault_rows"]
    assert rows and all(entry["rows"] for entry in rows)


def test_family15_snapshot_is_a_real_16_confidence_campaign():
    """The /16 capture keeps native dev58/b10 provenance; copies use /12 and /10.
    Wiring only: no b13 capture or statistical outcome is asserted."""
    snap = load_snapshot("family15")
    op = snap["meta"]["ids"]["campaign_confidence"]
    routes = snap["routes"]
    view = routes[f"/v1/operations/{op}/campaign"]["body"]
    assert (view["schema"], view["family"]) == ("lab-campaign-view/15", 15)
    assert view["independent"]["evidence"] == "authenticated_publications"
    assert view["details_state"] == "present" and view["champions_state"] == "present"
    core = routes[f"/v1/operations/{op}"]["body"]["result"]["core"]
    assert core["family_version"] == 15 and core["suspended"] is False
    report = routes[f"/v1/operations/{op}/report"]["body"]
    assert (report["schema"], report["state"]) == ("lab-owner-report/15", "available")
    assert report["report"]["lab_independent"] == view["independent"]
    assert routes[f"/v1/operations/{op}/suspensions"]["body"]["state"] == "available"
    for path, answer in routes.items():
        if "/diff" in path:
            assert answer["status"] == 200, path


def test_raw_closed_forms_keep_native_schemas_and_recorded_layers():
    import copy
    import json
    raw = json.loads((FIXTURES / "family15" / "routes.json").read_text())
    view = next(r["body"] for r in raw.values() if r.get("body", {}).get("schema") == "lab-campaign-view/10")
    report = next(r["body"] for r in raw.values() if r.get("body", {}).get("schema") == "lab-owner-report/7")
    before = copy.deepcopy(raw)
    loaded = load_snapshot("family15")["routes"]
    for path, answer in raw.items():
        if isinstance(answer.get("body"), dict):
            assert loaded[path]["body"] == current_form(answer["body"])
    for body, producing, schema, block_key in [(view, "lab-campaign-view/10", "lab-campaign-view/15", "independent"),
                                               (report, "lab-owner-report/7", "lab-owner-report/15", None)]:
        assert body["schema"] == producing
        adapted = current_form(body)
        assert adapted is not body and adapted["schema"] == schema
        assert (adapted["reflector_probe"], adapted["reflector_probe_state"], adapted["reflector_probe_reason"]) == (None, "unavailable", "unreadable")
        checks = adapted if block_key else adapted["report"]
        prefix = "" if block_key else "lab_"
        assert (checks[prefix + "checks"], checks[prefix + "checks_state"],
                checks[prefix + "checks_reason"]) == (None, "present", None)
        block = adapted[block_key] if block_key else adapted["report"]["lab_independent"]
        bindings = []
        for attempt in block["attempts"]:
            for key in ("gate_binding", "global_binding"):
                binding = attempt[key]
                if binding is not None:
                    bindings.append(binding)
                    assert set(binding) == {"source", "scopes"} and binding["source"] == "core_raw"
                    assert binding["scopes"]
                    for scope in binding["scopes"]:
                        assert set(scope) == {"name", "rule"}
                        assert scope["name"] in {"focus", "causal", "global"}
                        assert scope["rule"] in {"point", "bound", "off", "bootstrap"}
            for layer in ("gate_comparison", "global_comparison"):
                assert layer in attempt
                if attempt[layer] is not None:
                    assert "reference_measurement_id" in attempt[layer]
        assert bindings, "native authenticated binding DTO must not be erased by the legacy adapter"
    assert raw == before
    assert current_form({"schema": "other/1", "value": 7}) == {"schema": "other/1", "value": 7}


@pytest.mark.parametrize("schema", ["lab-campaign-view/9", "lab-owner-report/6"])
def test_legacy_two_layer_forms_adapt_only_in_memory_without_inventing_binding(schema):
    """Synthetic historical forms keep coverage without modifying native captures."""
    from copy import deepcopy
    block = {"settings": {}, "attempts": [{
        "comparison": {"reference_measurement_id": "recorded", "scopes": [{"name": "focus", "rule": "bound"}]},
        "gate_comparison": {"reference_measurement_id": "recorded", "scopes": [{"name": "focus", "rule": "bound"}]},
        "global_comparison": None,
    }]}
    body = ({"schema": schema, "family": 15, "independent": block} if schema.startswith("lab-campaign-view/")
            else {"schema": schema, "report": {"lab_independent": block}})
    before = deepcopy(body)
    adapted = current_form(body)
    assert adapted is not body and body == before
    assert adapted["schema"] == {"lab-campaign-view/9": "lab-campaign-view/15",
                                 "lab-owner-report/6": "lab-owner-report/15"}[schema]
    assert current_form(adapted) is adapted
    actual = adapted["independent"] if "independent" in adapted else adapted["report"]["lab_independent"]
    attempt = actual["attempts"][0]
    assert attempt["gate_binding"] is attempt["global_binding"] is None
    assert attempt["global_comparison"] is None
    assert attempt["gate_comparison"]["reference_measurement_id"] == "recorded"
    assert actual["settings"]["bound_fallback"] is None
    assert attempt["gate_comparison"]["calibration"] is None
    assert attempt["gate_comparison"]["scopes"][0]["lower"] is None


def test_adapter_projects_gate_or_null_without_changing_other_fields():
    body = {"schema": "lab-campaign-view/8", "family": 15, "independent": {"attempts": [
        {"attempt_id": "gate", "comparison": {"deciding_layer": "focus", "reference": "fresh"}},
        {"attempt_id": "none", "comparison": None}]}}
    result = current_form(body)
    gate, missing = result["independent"]["attempts"]
    assert gate["comparison"] == gate["gate_comparison"] == {
        "deciding_layer": "focus", "reference": "fresh", "reference_measurement_id": None,
        "method": None, "unit": None, "calibration": None, "caveats": None, "bound_fallback": None}
    assert gate["global_comparison"] is None
    assert missing["comparison"] is missing["gate_comparison"] is missing["global_comparison"] is None
    # An already-current form is not repaired or augmented.
    assert current_form({**body, "schema": "lab-campaign-view/15"}) == {**body, "schema": "lab-campaign-view/15"}


def test_load_snapshot_adapts_sse_embedded_views_and_keeps_metadata(tmp_path, monkeypatch):
    import json
    import panel_fixtures
    root = tmp_path / "adapter"
    root.mkdir()
    old = {"schema": "lab-campaign-view/8", "family": 14, "rounds": []}
    frame = "event: status\ndata: " + json.dumps({"payload": {"view": old}}) + "\n\n"
    for part, body in [("routes", {"/view": {"status": 200, "body": old}}),
                       ("events", {"op": [frame, ": keepalive\n\n"]}),
                       ("meta", {"lab_version": "0.0.3.dev55", "core_version": "2.0.0b8"})]:
        (root / (part + ".json")).write_text(json.dumps(body))
    monkeypatch.setattr(panel_fixtures, "FIXTURES", tmp_path)
    panel_fixtures.load_snapshot.cache_clear()
    loaded = load_snapshot("adapter")
    assert loaded["routes"]["/view"]["body"]["schema"] == "lab-campaign-view/15"
    event = loaded["events"]["op"][0]
    assert json.loads(event.split("data: ", 1)[1])["payload"]["view"]["schema"] == "lab-campaign-view/15"
    assert loaded["events"]["op"][1] == ": keepalive\n\n"
    assert loaded["meta"] == {"lab_version": "0.0.3.dev55", "core_version": "2.0.0b8"}
    assert json.loads((root / "routes.json").read_text())["/view"]["body"]["schema"] == "lab-campaign-view/8"
    panel_fixtures.load_snapshot.cache_clear()
