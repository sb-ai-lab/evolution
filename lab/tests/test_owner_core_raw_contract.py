"""ADR-0019 p. 2: core_raw over a real run of the pinned core (snapshot content).

Every form produced by the demo and fake chat roles is checked against selected real files.
agent_cli is not covered here (plan decision 1). Wiring only, measures nothing.
"""
import json
from pathlib import Path

import pytest

from evolution_lab.owner import content, core_raw
from fixtures.panel.fake_chat import KEY
from panel_fixtures import load_snapshot

SNAP = load_snapshot("content")
RUNS = Path(__file__).parent / "fixtures" / "panel" / "content" / "run"
IDS = SNAP["meta"]["ids"]
# Historical producer identities, not import or monkeypatch targets on this pin.
# The import-path guard exempts only this top-level literal assignment.
B13_EDITOR6_CORE_FUNCTIONS = [
    "evolution.tree.campaign_editor.build_brief",
    "evolution.tree.chat_editor.ChatEditor.edit",
]
ROUTES = {key: value["body"] for key, value in SNAP["routes"].items() if value["status"] == 200}


def _answers(schema):
    return [body for body in ROUTES.values() if body.get("schema") == schema]


def _normalized(value):
    # Capture's documented replacement, while the copied core files retain their original bytes.
    return json.loads(json.dumps(value, ensure_ascii=False).replace(str(Path.home()), "/home/user"))


def _current_captured_role_events(events):
    # Historical and Task7 captures predate the approved R1 content reduction.
    # Adapt a copy using their independently checked page shape, preserving the
    # producing events and their content as historical evidence.
    original = json.dumps(events, ensure_ascii=False)
    current = []
    for event in events:
        if event["type"] == "tool_result" and isinstance(event.get("text"), str):
            try:
                page = json.loads(event["text"])
            except ValueError:
                page = None
            if isinstance(page, dict) and "content" in page and "total_characters" in page:
                assert event["name"] == "read_evidence"
                assert set(page) == {"content", "content_digest", "offset", "total_characters", "next_offset"}
                assert isinstance(page["content"], str) and page["content"]
                metadata = {"offset": page["offset"], "total_characters": page["total_characters"],
                            "next_offset": page["next_offset"], "content_digest": page["content_digest"]}
                event = {**event, "text": json.dumps(metadata, ensure_ascii=False)}
        current.append(event)
    assert json.dumps(events, ensure_ascii=False) == original
    return current


def _sample_files(body):
    run = RUNS / body["operation_id"]
    if body["operation_id"] == IDS["evaluation"]:
        root = run / "measurement"
        return root, root / "events.jsonl"
    operation = ROUTES[f"/v1/operations/{body['operation_id']}"]
    entry = next(row for row in operation["result"]["core"]["measurements"]
                 if row["measurement_id"] == body["measurement_id"])
    return run, run / entry["locator"]


def test_snapshot_retains_dev58_b10_provenance():
    assert SNAP["meta"]["core_version"] == "2.0.0b10"
    assert SNAP["meta"]["lab_version"] == "0.0.3.dev58"
    assert set(IDS) == {"evaluation", "campaign_subprocess", "campaign_chat"}


def test_no_secret_value_anywhere():
    files = [path for path in (RUNS.parent).rglob("*") if path.is_file()]
    assert files and not [path for path in files if KEY.encode() in path.read_bytes()]


def test_every_finished_sample_has_a_checked_trace():
    samples = _answers("lab-sample/1")
    finished = [body for body in samples if body["scoring"]["state"] in ("admitted", "failed")]
    assert finished and {body["operation_id"] for body in finished} == set(IDS.values())
    for body in finished:
        root, journal = _sample_files(body)
        scoring = core_raw.scoring_of(journal, body["case_id"], body["sample_id"])
        trace = core_raw.scorer_trace(root, scoring["score_request_id"], body["case_id"],
                                      measurement_id=body["measurement_id"])
        # b6 directory traces carry null; b8 packs record Core's returncode 0, which core_raw returns as recorded.
        assert trace["output_reason"] is None and trace["returncode"] in (None, 0)
        assert body["trace"] == {"state": "available"}, body["case_id"]
        assert body["try"] == {key: scoring[key] for key in ("score_request_id", "retry_ordinal", "tries_total")}
        assert body["input"] == _normalized(content.mask({"public": trace["input"]["public"]}))
        assert body["turns"] == _normalized(content.mask(trace["input"]["view"]["history"]["turns"]))
        assert body["turns"] and body["terminal"] == trace["input"]["view"]["terminal"]
        assert body["scoring"]["state"] == ("admitted" if scoring["state"] == "finished" else "failed")
        if scoring["state"] == "finished":
            assert body["scoring"]["metrics"] == scoring["metrics"]
            assert body["scoring"]["critical_pass"] == scoring["critical_pass"]
            output = json.loads(trace["stdout"])
            assert body["scoring"]["verdicts"] == [{"criterion": row["criterion"], "passed": row["passed"]}
                                                   for row in output.get("verdicts") or []]
            assert body["scoring"]["public_feedback"] == _normalized(content.mask(output.get("public_feedback")))
        if body["disclosure"] == "private":
            assert body["private"]["oracle"] == _normalized(content.mask(trace["input"]["oracle"]))


def test_private_never_leaks_without_the_flag():
    public_samples = [body for body in _answers("lab-sample/1") if body["disclosure"] == "public"]
    public_reflections = [body for body in _answers("lab-reflection/1") if body["disclosure"] == "public"]
    assert public_samples and public_reflections
    for body in public_samples:
        assert body["private"] == {"withheld": "private"}
    hidden = []
    for body in public_reflections:
        for event in body["events"]:
            if event["type"] in ("input", "model_message"):
                assert event == {"type": event["type"], "withheld": "private"}
                hidden.append(event)
    assert hidden


def test_chat_editor_projection_on_real_logs():
    run = RUNS / IDS["campaign_chat"]
    records, broken = core_raw.role_log(run, "editor", "r01-a01")
    projected = content.project_role("chat", records)
    types = [event["type"] for event in projected["events"]]
    assert not broken and types[0] == "input" and "tool_call" in types and "tool_result" in types
    assert not projected["events"][0]["text"].startswith("Call list_files now")
    assert not any("usage" in event for event in projected["events"])
    attempts = [body for body in _answers("lab-attempt/1") if body["operation_id"] == IDS["campaign_chat"]]
    assert attempts
    facts = core_raw.campaign_content(run)
    for body in attempts:
        assert body["attempt_id"] in facts["closed"]
        records, broken = core_raw.role_log(run, "editor", body["attempt_id"])
        assert not broken
        assert body["editor"]["events"] == _normalized(content.mask(content.project_role("chat", records)["events"]))


def test_chat_editor_failure_has_text():
    attempts = [body for body in _answers("lab-attempt/1") if body["operation_id"] == IDS["campaign_chat"]]
    failed = [body for body in attempts if body["editor"]["failure"]]
    assert failed
    for body in failed:
        records, broken = core_raw.role_log(RUNS / IDS["campaign_chat"], "editor", body["attempt_id"])
        text, source = content.failure_text("chat", records)
        assert not broken and text and source == "editor_log"
        assert body["editor"]["failure"]["text"] == _normalized(content.mask(text))
        assert body["editor"]["failure"]["text_source"] == source


def test_chat_reflector_rejection_and_reflected():
    reflections = [body for body in _answers("lab-reflection/1")
                   if body["operation_id"] == IDS["campaign_chat"] and body["disclosure"] == "private"]
    assert reflections
    types = []
    run = RUNS / IDS["campaign_chat"]
    facts = core_raw.campaign_content(run)
    for body in reflections:
        assert body["reflection_id"] in facts["reflections"]
        records, broken = core_raw.role_log(run, "reflector", body["reflection_id"])
        projected = content.project_role("chat", records)
        terminal = facts["reflections"][body["reflection_id"]]
        assert terminal["event"] == "reflection_finished"
        expected = projected["events"] + [{"type": "reflected", "status": terminal["status"],
                                           "groups": len(terminal["groups"])}]
        assert not broken
        assert _current_captured_role_events(body["events"]) == _normalized(content.mask(expected))
        types.extend(event["type"] for event in body["events"])
    assert "rejected" in types and "reflected" in types


def test_subprocess_roles():
    operation = IDS["campaign_subprocess"]
    attempts = [body for body in _answers("lab-attempt/1") if body["operation_id"] == operation]
    reflections = [body for body in _answers("lab-reflection/1") if body["operation_id"] == operation]
    assert attempts and reflections
    facts = core_raw.campaign_content(RUNS / operation)
    for body in attempts:
        assert body["attempt_id"] in facts["closed"]
        assert body["editor"]["kind"] == "subprocess" and body["editor"]["events"] == []
        stderr = core_raw.role_stderr(RUNS / operation, "editor", body["attempt_id"])
        assert body["editor"]["stderr_tail"] == _normalized(content.mask(stderr))
    for body in reflections:
        assert body["reflection_id"] in facts["reflections"]
        assert body["kind"] == "subprocess" and body["events"] == []
        if body["disclosure"] == "private":
            stderr = core_raw.role_stderr(RUNS / operation, "reflector", body["reflection_id"])
            assert body["stderr_tail"] == _normalized(content.mask(stderr))


def test_attempt_confidence_is_the_whole_evaluated_list():
    attempts = _answers("lab-attempt/1")
    assert attempts
    for body in attempts:
        operation = ROUTES[f"/v1/operations/{body['operation_id']}"]
        row = next(attempt for rnd in operation["result"]["core"]["rounds"] for attempt in rnd["attempts"]
                   if attempt["attempt_id"] == body["attempt_id"])
        block = row.get("confidence")
        if block is None:
            assert body["confidence"] is None
            continue
        evaluated = block["evaluated"]
        assert len(body["confidence"]["looks"]) == len(evaluated)
        for actual, expected in zip(body["confidence"]["looks"], evaluated):
            projected = {key: expected.get(key) for key in actual}
            # /15 core estimates also carry exact; lab-attempt/1 keeps its closed public estimate fields.
            for estimate in ("focus", "causal", "overall"):
                if actual[estimate] is not None:
                    projected[estimate] = {key: expected[estimate].get(key) for key in actual[estimate]}
            assert actual == projected


def test_evaluation_samples():
    samples = [body for body in _answers("lab-sample/1") if body["operation_id"] == IDS["evaluation"]]
    assert samples
    for body in samples:
        root, journal = _sample_files(body)
        scoring = core_raw.scoring_of(journal, body["case_id"], body["sample_id"])
        trace = core_raw.scorer_trace(root, scoring["score_request_id"], body["case_id"],
                                      measurement_id=body["measurement_id"])
        assert body["trace"] == {"state": "available"}
        assert body["input"]["public"] == _normalized(content.mask(trace["input"]["public"]))


def test_answers_validate():
    from evolution_lab.cli import responses
    checked = set()
    for body in ROUTES.values():
        if body.get("schema") in ("lab-sample/1", "lab-attempt/1", "lab-reflection/1"):
            responses.validate(body, body["schema"])
            checked.add(body["schema"])
    assert checked == {"lab-sample/1", "lab-attempt/1", "lab-reflection/1"}


FAMILY14 = load_snapshot("family14")
FAMILY14_RUNS = Path(__file__).parent / "fixtures" / "panel" / "family14" / "run"


def test_family_14_runs_read_through_the_adapter():
    """ADR-0019 p. 2 on family 14 (spec 2026-10-13 §6.7): both real /15 runs read without an exception, with the
    rounds of the captured view."""
    assert FAMILY14["meta"]["core_version"] == "2.0.0b10"
    assert FAMILY14["meta"]["lab_version"] == "0.0.3.dev58"
    ids = FAMILY14["meta"]["ids"]
    assert set(ids) == {"campaign_final", "campaign_connection"}
    for operation in ids.values():
        run = FAMILY14_RUNS / operation
        payload = core_raw.campaign_status(run).to_payload()
        assert payload["suspended"] is False
        details = core_raw.campaign_details(run)
        assert details["journal"] == "present", operation
        view = FAMILY14["routes"][f"/v1/operations/{operation}/campaign"]["body"]
        assert sorted(details["rounds"]) == [r["round"] for r in view["rounds"]], operation
        for rnd in view["rounds"]:
            assert set(details["rounds"][rnd["round"]]["attempts"]) == {a["attempt_id"] for a in rnd["attempts"]}
            assert details["rounds"][rnd["round"]]["baseline_version"] == rnd["baseline_version"]
            raw_round = next(r for r in payload["rounds"] if r["round"] == rnd["round"])
            assert [f["selection"] for f in rnd["final"] or []] == [f["selection"] for f in raw_round["final"] or []]
            assert (rnd["reflection"]["state"] == "absent") == (details["rounds"][rnd["round"]]["diagnosis"] is None)
        first, last = core_raw.campaign_journal_times(run)
        assert first <= last, operation


FAMILY15 = load_snapshot("family15")
FAMILY15_RUNS = Path(__file__).parent / "fixtures" / "panel" / "family15" / "run"


def test_family_15_runs_read_through_the_adapter():
    """Spec 2026-10-19 D3/D9 on the real /16 confidence R=1 capture: the copied run reads through
    ``campaign_read(run, families=FAMILIES)`` as family 15 with the captured view's attempts, and every journal
    reader and every packed scorer trace of it reads through ``core_raw``. The copy holds journals and traces only
    (``_copy_runs``), not Core's authenticated publications, so its ``independent`` evidence is Core's
    ``unavailable`` state; the served view (authenticated) is checked in ``test_panel_fixtures``."""
    assert FAMILY15["meta"]["core_version"] == "2.0.0b10"
    assert FAMILY15["meta"]["lab_version"] == "0.0.3.dev58"
    ids = FAMILY15["meta"]["ids"]
    assert set(ids) == {"campaign_confidence"}
    for operation in ids.values():
        run = FAMILY15_RUNS / operation
        view = FAMILY15["routes"][f"/v1/operations/{operation}/campaign"]["body"]
        read = core_raw.campaign_read(run, families=core_raw.FAMILIES, with_result=True)
        assert read.family == 15
        status, result = read.status.to_payload(), read.result.to_payload()
        assert status["family_version"] == result["family_version"] == 15
        assert status["independent"]["evidence"] in ("snapshot_only", "unavailable", "authenticated_publications")
        captured = {a["attempt_id"] for r in view["rounds"] for a in r["attempts"]}
        assert captured and captured == {a["attempt_id"] for a in view["independent"]["attempts"]}
        assert {a["attempt_id"] for r in status["rounds"] for a in r["attempts"]} == captured
        assert {c["kind"] for c in status["caveats"]} <= core_raw.caveat_kinds(15)
        details = core_raw.campaign_details(run)
        assert details["journal"] == "present"
        assert sorted(details["rounds"]) == [r["round"] for r in view["rounds"]]
        for rnd in view["rounds"]:
            assert set(details["rounds"][rnd["round"]]["attempts"]) == {a["attempt_id"] for a in rnd["attempts"]}
        assert set(core_raw.campaign_content(run)["closed"]) == captured
        assert core_raw.campaign_suspensions(run) == FAMILY15["routes"][
            f"/v1/operations/{operation}/suspensions"]["body"]["suspensions"]
        first, last = core_raw.campaign_journal_times(run)
        assert first <= last
        locators = {m["measurement_id"]: m["locator"] for m in
                    FAMILY15["routes"][f"/v1/operations/{operation}"]["body"]["result"]["core"]["measurements"]}
        traced = set()
        for measurement_id, locator in locators.items():
            for line in (run / locator).read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("event") not in ("scoring_finished", "sample_imported"):
                    continue
                case_id, sample_id = event["payload"]["case_id"], event["payload"]["sample_id"]
                scoring = core_raw.scoring_of(run / locator, case_id, sample_id)
                if scoring["state"] == "imported":
                    continue
                trace = core_raw.scorer_trace(run, scoring["score_request_id"], case_id, measurement_id=measurement_id)
                assert trace["output_reason"] is None and trace["input"]["case_id"] == case_id
                traced.add(measurement_id)
        assert traced and all((run / "scorer-traces" / f"{m}.pack").is_file() for m in traced)


# Fresh installed dev61/b13 evidence is adjacent to, and never adapts or replaces,
# the historical captures above. These served bodies retain their producing /11 major.
B13 = Path(__file__).parent / "fixtures" / "panel" / "b13"
B13_NAMES = ("content", "family14", "family15", "checks")
B13_CORE_SHA = "25a2f21619461c743267967424c422cbf0a2990b1db4ac12212af8cde390139e"
B13_LAB_SHA = "19dfc752dd16c31a4980f32f8b7f00d7854a91ded75f173c0beb7b7dfea472c2"


def _b13(name):
    directory = B13 / name
    meta = json.loads((directory / "meta.json").read_text())
    routes = {path: row["body"] for path, row in json.loads((directory / "routes.json").read_text()).items()
              if row["status"] == 200}
    return directory, meta, routes


@pytest.mark.parametrize("name", B13_NAMES)
def test_b13_producing_pair_and_secret_boundary(name):
    directory, meta, routes = _b13(name)
    assert meta["core_version"] == "2.0.0b13" and meta["lab_version"] == "0.0.3.dev61"
    assert meta["producing_wheels"]["core"]["sha256"] == B13_CORE_SHA
    assert meta["producing_wheels"]["lab"]["sha256"] == B13_LAB_SHA
    assert meta["helper"]["task7_generator"]["path"].endswith("capture_b13.py")
    assert all(KEY.encode() not in path.read_bytes() for path in directory.rglob("*") if path.is_file())
    assert {body["schema"] for body in routes.values() if body.get("schema", "").startswith("lab-campaign-view/")} == {
        "lab-campaign-view/11"}


@pytest.mark.parametrize("name", B13_NAMES)
def test_b13_real_campaigns_and_packed_traces_read_through_adapter(name):
    directory, meta, routes = _b13(name)
    seen = set()
    for operation in meta["ids"].values():
        if operation == meta["ids"].get("evaluation"):
            continue
        run = directory / "run" / operation
        view = routes[f"/v1/operations/{operation}/campaign"]
        read = core_raw.campaign_read(run, families=core_raw.FAMILIES, with_result=True)
        status, result = read.status.to_payload(), read.result.to_payload()
        assert read.family == view["family"] == (14 if name in ("content", "family14") else 15)
        if read.family == 15:
            assert status["family_version"] == result["family_version"] == read.family
        else:
            assert "family_version" not in status and "family_version" not in result
        assert not status["suspended"]
        assert {c["kind"] for c in status["caveats"]} <= core_raw.caveat_kinds(read.family)
        details = core_raw.campaign_details(run)
        assert details["journal"] == "present"
        assert sorted(details["rounds"]) == [row["round"] for row in view["rounds"]]
        attempts = {a["attempt_id"] for row in view["rounds"] for a in row["attempts"]}
        assert attempts == set(core_raw.campaign_content(run)["closed"])
        assert {a["attempt_id"] for row in status["rounds"] for a in row["attempts"]} == attempts
        assert core_raw.campaign_suspensions(run) == routes[f"/v1/operations/{operation}/suspensions"]["suspensions"]
        first, last = core_raw.campaign_journal_times(run)
        assert first <= last
        for measurement in routes[f"/v1/operations/{operation}"]["result"]["core"]["measurements"]:
            journal = run / measurement["locator"]
            for event in map(json.loads, journal.read_text().splitlines()):
                if event["event"] != "scoring_finished":
                    continue
                case_id, sample_id = event["payload"]["case_id"], event["payload"]["sample_id"]
                scoring = core_raw.scoring_of(journal, case_id, sample_id)
                trace = core_raw.scorer_trace(run, scoring["score_request_id"], case_id,
                    measurement_id=measurement["measurement_id"])
                assert trace["output_reason"] is None and trace["input"]["case_id"] == case_id
                seen.add(operation)
                assert (run / "scorer-traces" / f"{measurement['measurement_id']}.pack").is_file()
    assert seen == set(meta["ids"].values()) - {meta["ids"].get("evaluation")}


@pytest.mark.parametrize("name", ("content", "checks"))
def test_b13_served_sample_content_matches_real_raw_trace(name):
    directory, meta, routes = _b13(name)
    samples = [body for body in routes.values() if body.get("schema") == "lab-sample/1"
               and body["scoring"]["state"] in ("admitted", "failed")]
    assert samples
    for body in samples:
        run = directory / "run" / body["operation_id"]
        if body["operation_id"] == meta["ids"].get("evaluation"):
            run = run / "measurement"
            journal = run / "events.jsonl"
        else:
            measurement = next(row for row in routes[f"/v1/operations/{body['operation_id']}"]["result"]["core"][
                "measurements"] if row["measurement_id"] == body["measurement_id"])
            journal = run / measurement["locator"]
        scoring = core_raw.scoring_of(journal, body["case_id"], body["sample_id"])
        trace = core_raw.scorer_trace(run, scoring["score_request_id"], body["case_id"],
                                    measurement_id=body["measurement_id"])
        assert body["trace"] == {"state": "available"}
        assert body["input"] == _normalized(content.mask({"public": trace["input"]["public"]}))
        assert body["turns"] == _normalized(content.mask(trace["input"]["view"]["history"]["turns"]))
        assert body["terminal"] == trace["input"]["view"]["terminal"]
        assert body["try"] == {key: scoring[key] for key in ("score_request_id", "retry_ordinal", "tries_total")}
        if body["disclosure"] == "private":
            assert body["private"]["oracle"] == _normalized(content.mask(trace["input"]["oracle"]))
        else:
            assert body["private"] == {"withheld": "private"}


def _real_openings(records):
    """Independently replay the complete Core request deltas, not selected text excerpts."""
    conversation, openings, started = [], [], False
    for record in records:
        if record.get("phase") != "model_request":
            continue
        after = record["messages_after"]
        if after == 0:
            conversation, started = [], False
        assert after <= len(conversation)
        conversation = conversation[:after] + record["messages"]
        if record["tool_names"] == ["list_files"] or started:
            continue
        users = [m["content"] for m in conversation if m["role"] == "user"
                 and not m["content"].startswith("Call list_files now")]
        assert users and isinstance(users[0], str)
        openings.append(users[0])
        started = True
    return openings


@pytest.mark.parametrize("name", ("content", "checks", "editor6"))
def test_b13_complete_editor_and_reflector_openings_from_real_role_logs(name):
    directory = B13 / name
    seen = set()
    for run in (directory / "run").iterdir():
        for role in ("editor", "reflector"):
            for log in sorted((run / f"{role}-logs").glob("*.jsonl")):
                records, broken = core_raw.role_log(run, role, log.stem)
                assert not broken and records
                expected = _real_openings(records)
                assert expected
                projected = content.project_role("chat", records)
                assert [e["text"] for e in projected["events"] if e["type"] == "input"] == expected
                assert projected["events"][0] == {"type": "input", "text": expected[0]}
                assert all(len(text) > 500 for text in expected)
                if name == "checks" and role == "reflector":
                    assert all('"version":"reflect_brief/6"' in text for text in expected)
                if name == "checks" and role == "editor":
                    # Editor prose does not serialize brief.version. The installed
                    # campaign attaches feedback to common /6 (therefore /7), and
                    # these actual logs expose its feedback opening and trace tool.
                    assert all('Read "attempt_feedback"' in text for text in expected)
                    assert all("read_trace" in row["tool_names"] for row in records
                        if row.get("phase") == "model_request" and row["tool_names"] != ["list_files"])
                seen.add(role)
    assert seen == ({"editor"} if name == "editor6" else {"editor", "reflector"})
    if name == "editor6":
        meta = json.loads((directory / "meta.json").read_text())
        assert meta["invocation"] == "direct_role_not_campaign"
        assert meta["core_version"] == "2.0.0b13"
        assert meta["producing_wheels"]["core"]["sha256"] == B13_CORE_SHA
        assert meta["core_functions"] == B13_EDITOR6_CORE_FUNCTIONS
        assert json.loads((directory / "brief.json").read_text())["version"] == meta["brief_version"] == "edit_brief/6"


@pytest.mark.parametrize("name", ("content", "checks"))
def test_b13_served_chat_roles_match_real_logs(name):
    directory, meta, routes = _b13(name)
    operation = meta["ids"]["campaign_chat" if name == "content" else "campaign_checks"]
    run = directory / "run" / operation
    facts = core_raw.campaign_content(run)
    attempts = [body for body in routes.values() if body.get("schema") == "lab-attempt/1"
                and body["operation_id"] == operation]
    reflections = [body for body in routes.values() if body.get("schema") == "lab-reflection/1"
                   and body["operation_id"] == operation and body["disclosure"] == "private"]
    assert attempts and reflections
    for body in attempts:
        records, broken = core_raw.role_log(run, "editor", body["attempt_id"])
        assert not broken
        assert body["editor"]["events"] == _normalized(content.mask(content.project_role("chat", records)["events"]))
    for body in reflections:
        records, broken = core_raw.role_log(run, "reflector", body["reflection_id"])
        terminal = facts["reflections"][body["reflection_id"]]
        assert not broken and terminal["event"] == "reflection_finished"
        expected = content.project_role("chat", records)["events"] + [
            {"type": "reflected", "status": terminal["status"], "groups": len(terminal["groups"])}]
        assert _current_captured_role_events(body["events"]) == _normalized(content.mask(expected))


def test_b13_real_shadow_hits_terminal_batches_role_records_and_worker_notes():
    from types import SimpleNamespace
    from evolution.tree.execution.check_modes import parse_checks
    directory, meta, routes = _b13("checks")
    operation = meta["ids"]["campaign_checks"]
    run = directory / "run" / operation
    view = routes[f"/v1/operations/{operation}/campaign"]
    expected_modes = {"reflection_run_secret": "shadow", "seal_oracle_leak": "off"}
    read = core_raw.campaign_read(run, families=core_raw.FAMILIES, with_result=True)
    status, result = read.status.to_payload(), read.result.to_payload()
    assert status["check_modes"] == result["check_modes"] == expected_modes
    assert status["shadow_hits"] == result["shadow_hits"] == {"reflection_run_secret": 2}
    assert view["checks_state"] == "present" and view["checks_reason"] is None
    assert view["checks"] == {"modes": expected_modes, "shadow_hits": status["shadow_hits"]}
    records = list(map(json.loads, (run / "campaign.jsonl").read_text().splitlines()))
    batches = [r for r in records if r["payload"].get("shadowed")]
    assert len(batches) == 2 and {r["event"] for r in batches} == {"reflection_finished"}
    role_hits = []
    for terminal in batches:
        reflection = terminal["payload"]["reflection_id"]
        role_records, broken = core_raw.role_log(run, "reflector", reflection)
        assert not broken
        hits = [r for r in role_records if r.get("event") == "check_shadowed"]
        assert [{k: v for k, v in hit.items() if k != "event"} for hit in hits] == terminal["payload"]["shadowed"]
        projected = content.project_role("chat", role_records)
        assert projected["skipped"] >= len(hits)
        assert "shadowed" not in core_raw.campaign_content(run)["reflections"][reflection]
        role_hits.extend(hits)
    assert sum(hit["count"] for hit in role_hits) == status["shadow_hits"]["reflection_run_secret"]
    workflow = SimpleNamespace(checks=parse_checks({"version": "checks/1", "modes": expected_modes}))
    stderr = (directory / "worker-logs" / operation / "stderr.log").read_text()
    assert [line for line in stderr.splitlines() if line.startswith("evo tree evolve: note: check ")] == [
        "evo tree evolve: note: " + line for line in core_raw.check_mode_notes(workflow)]


@pytest.mark.parametrize("name", ("content", "family14", "family15"))
def test_b13_undeclared_checks_have_no_worker_mode_note(name):
    directory, meta, routes = _b13(name)
    for operation in meta["ids"].values():
        stderr = (directory / "worker-logs" / operation / "stderr.log").read_text()
        assert "evo tree evolve: note: check " not in stderr
        view = routes.get(f"/v1/operations/{operation}/campaign")
        if view is not None and view["family"] == 15:
            assert view["checks"] is None and view["checks_state"] == "present"


B14 = Path(__file__).parent / 'fixtures/panel/b14'
B14_SCENARIOS = ('agent-finished', 'agent-failed', 'subprocess-finished',
                 'subprocess-failed', 'agent-unreachable')


@pytest.mark.parametrize('name', B14_SCENARIOS)
def test_b14_real_probe_forms_both_terminals_and_exact_core_report_wording(name):
    import hashlib
    from evolution_lab.report import read_campaign_report
    from evolution_lab.owner.independent_view import independent_view

    directory = B14 / name
    if name == 'subprocess-failed' and not directory.is_dir():
        directory = B14 / 'subprocess-failed-neutral'
    meta = json.loads((directory / 'meta.json').read_text())
    assert meta['core_version'] == '2.0.0b14' and meta['lab_version'] == '0.0.3.dev62'
    assert meta['producing_wheels']['core']['sha256'] == (
        '7a506e3460fa3b1a697e6e23798032cb2f7768c4adbb822e8cf42829e902e243')
    assert meta['producing_wheels']['lab']['sha256'] == (
        'bb7162194c3bf5167fefd7a83fc3a19220e5c14cd6e4b7f5fad3ef085dd4c4a5')
    for member, digest in meta['files_sha256'].items():
        assert hashlib.sha256((directory / member).read_bytes()).hexdigest() == digest, member
    assert meta['generators'] and all(len(row['sha256']) == 64 for row in meta['generators'])
    run = directory / 'run'
    records = list(map(json.loads, (run / 'campaign.jsonl').read_text().splitlines()))
    assert records[0]['payload']['family_version'] == 15
    assert records[0]['payload']['producing_version'] == '2.0.0b14'
    probes = [row['payload'] for row in records if row['event'] == 'probe_completed']
    agent = name.startswith('agent-')
    terminal = 'reflection_failed' if name.endswith('-failed') else 'reflection_finished'
    payload, = [row['payload'] for row in records if row['event'] == terminal]
    expected = {'health': 'unavailable', 'mediator': 'started' if name == 'agent-unreachable' else 'failed',
                'unreachable': 2 if agent else 0, 'transport_failures': 0}
    assert payload['probe_availability'] == expected
    assert [row['seq'] for row in probes] == ([1, 2] if agent else [])
    assert all(row['error'] == 'unreachable' and row['status'] is None for row in probes)
    # Actual b14 schema/reducer admission through the sole Lab raw adapter.
    facts = core_raw.campaign_content(run)
    assert facts['reflections'][payload['reflection_id']]['event'] == terminal
    assert facts['reflections'][payload['reflection_id']]['probe_availability'] == expected
    read = core_raw.campaign_read(run, with_result=True)
    assert read.family == 15
    for projection in (read.status, read.result):
        block = projection.to_payload()['independent']
        assert block['probe_availability'] == [{'round': 1, **expected}]
        assert independent_view(block)['evidence'] in ('snapshot_only', 'unavailable', 'authenticated_publications')
    captured = json.loads((directory / 'core-projection.json').read_text())
    assert captured['verification']['mismatches'] == []
    expected_line = ('round 1: probe unavailable at health check; 2 calls unreachable' if name == 'agent-unreachable'
        else 'round 1: probe mediator failed to start; ' + ('2 calls unreachable' if agent else 'no probe calls'))
    assert expected_line in (directory / 'core-report.txt').read_text().splitlines()
    assert core_raw.family15_probe_lines(read.status.to_payload()['independent']) == [expected_line]
    assert expected_line in (directory / 'lab-report.md').read_text()
    assert expected_line in read_campaign_report(run, families=core_raw.FAMILIES).markdown()
    # Fresh b14 packs are checked too; old captures are preserved and still covered above.
    traced = 0
    for journal in (run / 'measurements').glob('*/events.jsonl'):
        for row in map(json.loads, journal.read_text().splitlines()):
            if row['event'] != 'scoring_finished':
                continue
            p = row['payload']
            scoring = core_raw.scoring_of(journal, p['case_id'], p['sample_id'])
            trace = core_raw.scorer_trace(run, scoring['score_request_id'], p['case_id'],
                measurement_id=journal.parent.name)
            assert trace['input']['case_id'] == p['case_id'] and trace['output_reason'] is None
            traced += 1
    assert traced == 2


@pytest.mark.parametrize('name, expected', [
    ('focus-first', {
        'r01-a01': {'focus_opened': True, 'causal_opened': False, 'focus_decided': 'comparison_not_passed'},
        'r01-a02': {'focus_opened': True, 'causal_opened': True, 'focus_decided': 'passed'},
    }),
    ('focus-open', {'r01-a01': {'focus_opened': True, 'causal_opened': False, 'focus_decided': None}}),
    ('focus-decided-open', {
        'r01-a01': {'focus_opened': True, 'causal_opened': False, 'focus_decided': 'comparison_not_passed'},
    }),
    ('causal-open', {'r01-a01': {'focus_opened': True, 'causal_opened': True, 'focus_decided': 'passed'}}),
])
def test_family15_stage_facts_real_captures(name, expected):
    run = Path(__file__).parent / 'fixtures/panel/b15' / name / 'run'
    read = core_raw.campaign_read(run, with_bindings=True)
    assert read.stages == expected and read.stages_reason is None
    assert core_raw.family15_stage_facts(run, read.view.snapshot, records=read.records) == expected
    assert core_raw.family15_stage_facts(run, read.view.snapshot) == expected
    details = core_raw.campaign_details(run, read=read)
    assert details['stages'] == expected and details['stages_reason'] is None


@pytest.mark.parametrize('run', [
    Path(__file__).parent / 'fixtures/panel/b14/agent-unreachable/run',
    FAMILY15_RUNS / next(iter(FAMILY15['meta']['ids'].values())),
    FAMILY14_RUNS / FAMILY14['meta']['ids']['campaign_final'],
])
def test_stage_facts_absent_without_focus_first(run):
    read = core_raw.campaign_read(run, with_bindings=True)
    assert read.stages == {} and read.stages_reason is None
    if read.family == 15:
        assert core_raw.family15_stage_facts(run, read.view.snapshot, records=read.records) == {}
