"""Capture real panel fixtures from one installed Lab/Core pair.

The default capture records empty/running/suspended-target/suspended-role/final/details;
--only family14, --only content and --only suspension-fault record the remaining family-14 snapshots (/15 inputs);
--only family15 records one /16 confidence-mode campaign (R=1, automatic promotion). All components are neutral
local fixtures. Answers are stored as served,
with only the documented machine-path replacement. Each invocation retains its state,
commands, service logs and producing wheels under .superpowers/panel-capture/<uuid>/.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode
import uuid

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCHEMA = "lab-panel-fixture/1"
PROJECT = "demo"
ACTIVE = {"reserved", "running", "stopping"}
TERMINAL_TASK = {"succeeded", "failed", "unknown"}
HOME_ALIAS = "/tmp/lab-fixture-home"
USER_HOME_ALIAS = "/home/user"
EMPTY_ROUTES = ("/v1/health", "/v1/projects", "/v1/operations", "/v1/operations?view=summary",
                "/v1/scenarios", "/v1/targets")


# ---- the fixture key and the crawl (canonical is a copy of tests/panel_fixtures.py) ----

def canonical(path_and_query: str) -> str:
    path, _, query = path_and_query.partition("?")
    path = unquote(path)
    if not query:
        return path
    return path + "?" + urlencode(sorted(parse_qsl(query, keep_blank_values=True)), safe=":@")


def crawl(get) -> dict[str, dict]:
    """get(path) -> (status, body). Every GET the panel may issue in waves 2a/2b over this home."""
    out: dict[str, dict] = {}

    def take(path):
        key = canonical(path)
        if key not in out:
            status, body = get(path)
            out[key] = {"status": status, "body": body}
        entry = out[key]
        return entry["body"] if entry["status"] == 200 else None

    take("/v1/health")
    for path in ("/v1/scenarios", "/v1/targets", "/v1/operations", "/v1/operations?view=summary"):
        take(path)
    # lab-scenario-list/1 rows are {descriptor, skill_path}; the id is in the descriptor.
    for scenario in (take("/v1/scenarios") or {}).get("scenarios", []):
        take(f"/v1/scenarios/{scenario['descriptor']['id']}")
    # GET /v1/targets/T requires ?owner_generation=G (service-s5 route table).
    for target in (take("/v1/targets") or {}).get("targets", []):
        take(f"/v1/targets/{target['id']}?owner_generation={target['owner_generation']}")
    for project in (take("/v1/projects") or {}).get("projects", []):
        base = f"/v1/projects/{project['id']}"
        for path in (base, f"{base}/status", f"{base}/facts", f"{base}/confirmations",
                     f"{base}/operations", f"{base}/operations?view=summary"):
            take(path)
    for row in (take("/v1/operations") or {}).get("operations", []):
        base = f"/v1/operations/{row['id']}"
        take(base)
        take(f"{base}/report")
        # lab-measurement-list/3 summaries carry measurement_id (owner-s4 "Measurements").
        if row.get("kind") != "campaign":
            for m in (take(f"{base}/measurements") or {}).get("measurements", []):
                take(f"{base}/measurements/{m['measurement_id']}")
            take(f"{base}/diff")
            continue
        view = take(f"{base}/campaign") or {}
        take(f"{base}/suspensions")
        for m in (take(f"{base}/measurements") or {}).get("measurements", []):
            take(f"{base}/measurements/{m['measurement_id']}")
        take(f"{base}/diff")
        versions = [c["version"] for c in view.get("champions") or []]
        for left, right in zip(versions, versions[1:]):
            take(f"{base}/diff?from=champion:{left}&to=champion:{right}")
        for version in versions[1:]:
            take(f"{base}/diff?from=initial&to=champion:{version}")
        for rnd in view.get("rounds") or []:
            for attempt in rnd.get("attempts") or []:
                if rnd.get("baseline_version") and attempt.get("has_tree"):
                    take(f"{base}/diff?from=champion:{rnd['baseline_version']}&to=attempt:{attempt['attempt_id']}")
    return out


def normalize(value: Any, replacements: list[tuple[str, str]]) -> Any:
    """The only change to captured content: string replacement of machine paths, whole tree."""
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [normalize(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item, replacements) for key, item in value.items()}
    return value


# ---- the service and the CLI (copies of tests/test_service_installed_projects.py) ----

def _json_lines(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def _client(console: Path, home: Path, command_log):
    def cli(*arguments: str, check: bool = True, timeout: float = 30, raw: bool = False):
        result = subprocess.run(
            [str(console), "--home", str(home), "--json", "--timeout", str(timeout), *arguments],
            text=True, capture_output=True, check=False,
            env={**os.environ, "PYTHONPATH": ""}, timeout=timeout + 30,
        )
        command_log.write(json.dumps({"argv": list(arguments), "returncode": result.returncode,
                                      "stdout": result.stdout, "stderr": result.stderr}) + "\n")
        command_log.flush()
        if check and result.returncode != 0:
            raise RuntimeError(f"lab {' '.join(arguments)} failed: {result.stderr}")
        return result if raw else _json_lines(result)
    return cli


@contextmanager
def _service(console: Path, *, state: Path, home: Path, wheels, log: Path, env_home: Path, command_log):
    """``lab serve --state S --home H`` (socket <home>/service.sock); yields a CLI bound to it."""
    argv = [str(console), "serve", "--state", str(state), "--home", str(home), "--shutdown-wait-seconds", "30"]
    for wheel in wheels:
        argv += ["--execution-wheel", str(wheel)]
    stderr = log.open("x", encoding="utf-8")
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr,
                               text=True, env={**os.environ, "PYTHONPATH": "", "LAB_HOME": str(env_home)})
    cli = _client(console, home, command_log)
    try:
        deadline = time.monotonic() + 30
        while not cli("health", check=False):
            if process.poll() is not None:
                raise RuntimeError(f"service exited before readiness: {log.read_text()}")
            if time.monotonic() > deadline:
                raise RuntimeError("service did not become healthy")
            time.sleep(0.1)
        yield cli
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        stderr.close()
    if process.returncode != 0:
        print(f"capture: service {home} exited {process.returncode}, see {log}", file=sys.stderr)


def _wait_operation(cli, operation, timeout=120):
    deadline = time.monotonic() + timeout
    while operation["control_state"] in ACTIVE:
        if time.monotonic() > deadline:
            raise RuntimeError(f"operation {operation['id']} did not settle")
        time.sleep(0.2)
        operation = cli("status", operation["id"])[-1]
    return operation


def _wait_task(cli, task: dict, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = cli("task", "status", task["id"], "--generation", task["owner_generation"])[-1]
        if status["state"] in TERMINAL_TASK:
            return status
        if time.monotonic() > deadline:
            raise RuntimeError(f"task {task['id']} did not finish")
        time.sleep(0.2)


def _project_target(cli, digest: str, project: str = PROJECT) -> tuple[str, str]:
    started = cli("project", "target", project, "--bundle", digest)[-1]
    status = _wait_task(cli, started["task"])
    if status["state"] != "succeeded":
        raise RuntimeError(f"target failed: {status}")
    return status["ref"]["id"], status["owner_generation"]


def _check(condition: bool, what: str, value: Any) -> None:
    if not condition:
        raise RuntimeError(f"capture: {what}: {json.dumps(value)[:2000]}")


# ---- UDS reads: plain GETs and the raw SSE frames ----

def _getter(sock: Path):
    client = httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), base_url="http://lab", timeout=60)

    def get(path: str) -> tuple[int, Any]:
        response = client.get(path)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text
    return client, get


def _record_events(sock: Path, operation_id: str, frames: list[str], limit: float = 60) -> None:
    """Raw SSE frames (``event:``/``data:`` lines plus the closing blank line) until the stream
    ends or ``limit`` seconds pass."""
    deadline = time.monotonic() + limit
    timeout = httpx.Timeout(10, read=limit + 5)
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), base_url="http://lab", timeout=timeout) as client:
        with client.stream("GET", f"/v1/operations/{operation_id}/events") as response:
            lines: list[str] = []
            for line in response.iter_lines():
                if line == "":
                    if lines:
                        frames.append("\n".join(lines) + "\n\n")
                        lines = []
                else:
                    lines.append(line)
                if time.monotonic() > deadline:
                    break
            if lines:
                frames.append("\n".join(lines) + "\n\n")


# ---- the capture ----

class Capture:
    def __init__(self, console: Path, wheels: tuple[Path, Path], run: Path):
        self.console, self.wheels, self.run = console, wheels, run
        self.snapshots: dict[str, dict] = {}
        self.events: dict[str, list[str]] = {}  # operation id -> frames (campaign 1 only)
        self.kept: dict[str, dict] = {}  # name -> core projection (family 14 only)
        self.commands = (run / "commands.jsonl").open("x", encoding="utf-8")
        producing = run / "producing-wheels"
        producing.mkdir()
        self.provenance = {}
        for label, wheel in zip(("lab", "core"), wheels):
            shutil.copyfile(wheel, producing / wheel.name)
            self.provenance[label] = {"source": str(wheel), "file": wheel.name,
                                      "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
        self.helper = {"source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
            "path": str(Path(__file__).relative_to(REPO)),
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.receipt = {"helper": self.helper, "capture_id": run.name, "argv": sys.argv, "wheels": self.provenance,
                        "started_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                        "completed": False}
        self.write_receipt()

    def write_receipt(self) -> None:
        self.receipt["snapshots"] = {name: snap["meta"] for name, snap in self.snapshots.items()}
        (self.run / "receipt.json").write_text(json.dumps(self.receipt, indent=1) + "\n", encoding="utf-8")

    def service(self, tag: str, home: Path):
        return _service(self.console, state=self.run / f"state-{tag}", home=home, wheels=self.wheels,
                        log=self.run / f"service-{tag}.jsonl", env_home=self.run / f"env-home-{tag}",
                        command_log=self.commands)

    def snapshot(self, sock: Path, name: str, ids: dict[str, str], home: Path, paths=None) -> None:
        client, get = _getter(sock)
        with client:
            if paths is None:
                routes = crawl(get)
            else:
                routes = {canonical(p): dict(zip(("status", "body"), get(p))) for p in paths}
        health = routes["/v1/health"]["body"]
        versions = {d["name"]: d["version"] for d in (health.get("service_environment") or {}).get("distributions", [])}
        meta = {"schema": SCHEMA, "snapshot": name,
                "captured_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                "lab_version": versions.get("evolution-lab"), "core_version": versions.get("evo-skills"),
                "ids": dict(ids), "capture_id": self.run.name,
                "producing_wheels": self.provenance, "helper": self.helper}
        self.snapshots[name] = {"routes": routes, "meta": meta, "home": str(home)}
        print(f"capture: snapshot {name}: {len(routes)} routes, ids {ids}", file=sys.stderr)

    def write(self) -> None:
        user_home = os.environ.get("HOME", str(Path.home()))
        for name, snap in self.snapshots.items():
            replacements = [(snap["home"], HOME_ALIAS), (user_home, USER_HOME_ALIAS)]
            ops = {v for v in snap["meta"]["ids"].values()}
            events = {op: frames for op, frames in self.events.items() if op in ops}
            target = HERE / name
            target.mkdir(exist_ok=True)
            for part, value in (("routes", snap["routes"]), ("events", events), ("meta", snap["meta"])):
                text = json.dumps(normalize(value, replacements), ensure_ascii=False, indent=1) + "\n"
                (target / f"{part}.json").write_text(text, encoding="utf-8")
        for name, kept in self.kept.items():
            replacements = [(kept["home"], HOME_ALIAS), (user_home, USER_HOME_ALIAS)]
            # The format of the Task 0 projections in that directory (dev48 branch 2).
            text = json.dumps(normalize(kept["value"], replacements), ensure_ascii=False, indent=1, sort_keys=True)
            (REPO / "tests" / "fixtures" / "family14" / f"{name}.json").write_text(text + "\n", encoding="utf-8")

    def keep(self, name: str, value: dict, home: Path) -> None:
        """A core projection to keep beside the family-14 fixtures (``tests/fixtures/family14/<name>.json``),
        written by ``write`` with ``measurements: []`` (the view does not read them) and the same replacements."""
        self.kept[name] = {"value": {**value, "measurements": []}, "home": str(home)}


def _wrap_editor(materials: Path, flag_base: Path) -> dict:
    """Point ``editor.argv`` at ``editor/editor_hold.py`` (a copy of this directory's wrapper) with its
    values in ``editor/editor_hold.json``; like the role-suspension test, before the Bundle is built."""
    manifest_path = materials / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original = list(manifest["editor"]["argv"])
    _check(original[1:] == ["edit.py"], "unexpected demo editor argv", original)
    editor = materials / manifest["editor"]["cwd"]
    shutil.copyfile(HERE / "editor_hold.py", editor / "editor_hold.py")
    (editor / "editor_hold.json").write_text(json.dumps({"flag_base": str(flag_base), "original_argv": original}),
                                             encoding="utf-8")
    manifest["editor"]["argv"] = [original[0], "editor_hold.py"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"original_argv": original, "argv": manifest["editor"]["argv"]}


def _set_attempts(materials: Path, n: int) -> None:
    """``campaign.attempts_per_round = n`` in the materials' manifest, before the Bundle (as ``_wrap_editor``)."""
    manifest_path = materials / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["campaign"]["attempts_per_round"] = n
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _family14(capture: Capture, home: Path) -> None:
    """Real /15 observe demo campaigns (spec 2026-10-13 §6.7): one with a finalist and a promotion; one suspended by
    core's connection check (the demo's fault flag says ``empty``, outside the Bundle), then resumed once the flag
    is gone. No production code changes; wiring only."""
    with capture.service("main", home) as cli:
        sock = home / "service.sock"
        reads, get = _getter(sock)
        with reads:
            ids = {}
            for name in ("demo-final", "demo-connection"):
                for ready in get("/v1/targets")[1].get("targets", []):
                    if ready.get("state") == "ready":
                        cli("target", "stop", ready["id"], "--generation", ready["owner_generation"], "--wait")
                project = cli("project", "demo", name)[-1]
                wc = Path(project["working_copy"]["path"])
                schema = json.loads((wc / "lab" / "manifest.json").read_text(encoding="utf-8"))["schema"]
                _check(schema == "evolution-v2-manifest/15", "family14 needs the /15 demo", schema)
                digest, campaign = _connect(cli, capture.run, project["id"])
                target, generation = _project_target(cli, digest, project["id"])
                flag = wc.with_name(wc.name + ".fault")
                if name == "demo-connection":
                    flag.write_text("empty\n")
                done = _wait_operation(cli, cli("project", "start", project["id"], "--prepared", campaign,
                                                "--target", target)[-1]["operation"], timeout=600)
                if name == "demo-connection":
                    core = (done.get("result") or {}).get("core") or {}
                    last = (core.get("suspensions") or [{}])[-1]
                    _check(core.get("suspended") is True and last.get("reason") == "connection_incompatible",
                           "campaign 2 suspended on connection_incompatible", done)
                    capture.keep("connection-suspended-status", core, home)
                    flag.unlink()
                    cli("resume", done["id"], "--target", target, "--generation", generation)
                    done = _wait_operation(cli, cli("status", done["id"])[-1], timeout=600)
                _check(done["control_state"] == "finished"
                       and ((done.get("result") or {}).get("core") or {}).get("suspended") is False,
                       f"campaign {name} finished", done)
                ids["campaign_final" if name == "demo-final" else "campaign_connection"] = done["id"]
            status, view = get(f"/v1/operations/{ids['campaign_final']}/campaign")
            _check(status == 200 and view.get("family") == 14
                   and any(r.get("outcome") == "promoted" for r in view.get("rounds") or ()),
                   "campaign 1 has a promotion", view)
            capture.snapshot(sock, "family14", ids, home)
            _copy_runs(capture.run / "state-main", ids, HERE / "family14" / "run")


#: Core's three ``independent.evidence`` kinds (family15_report.py; ``owner/independent_view.EVIDENCE``).
EVIDENCE15 = ("snapshot_only", "unavailable", "authenticated_publications")


def _family15(capture: Capture, home: Path) -> None:
    """One neutral demo project whose working-copy manifest becomes /16 by ``tests/scaffold_16.py``'s conversion:
    ``gate.mode: confidence``, ``repeats: 1``, automatic promotion (no ``promotion`` key). One campaign to completion.
    Wiring only: no statistical outcome is asserted."""
    sys.path.insert(0, str(REPO / "tests"))
    from scaffold_16 import as_manifest_16

    with capture.service("main", home) as cli:
        sock = home / "service.sock"
        reads, get = _getter(sock)
        with reads:
            project = cli("project", "demo", "demo-confidence")[-1]
            wc = Path(project["working_copy"]["path"])
            manifest_path = wc / "lab" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _check(manifest["schema"] == "evolution-v2-manifest/15", "family15 converts the /15 demo", manifest["schema"])
            manifest = as_manifest_16(manifest, mode="confidence", repeats=1)
            _check("promotion" not in manifest["campaign"], "automatic promotion", manifest["campaign"])
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            digest, campaign = _connect(cli, capture.run, project["id"])
            target, _ = _project_target(cli, digest, project["id"])
            done = _wait_operation(cli, cli("project", "start", project["id"], "--prepared", campaign,
                                            "--target", target)[-1]["operation"], timeout=900)
            _check(done["control_state"] == "finished"
                   and ((done.get("result") or {}).get("core") or {}).get("suspended") is False,
                   "campaign demo-confidence finished", done)
            ids = {"campaign_confidence": done["id"]}
            status, view = get(f"/v1/operations/{done['id']}/campaign")
            _check(status == 200 and view.get("schema") == "lab-campaign-view/10" and view.get("family") == 15
                   and (view.get("independent") or {}).get("evidence") in EVIDENCE15,
                    "campaign demo-confidence is a family-15 /10 view", view)
            capture.snapshot(sock, "family15", ids, home)
            _copy_runs(capture.run / "state-main", ids, HERE / "family15" / "run")


def _retain_suspensions(capture: Capture, snapshot: str, ids: dict[str, str]) -> None:
    """Retain exact campaign writer bytes, independently of normalized HTTP answers."""
    retained = {}
    for scenario, operation in ids.items():
        source = capture.run / "state-main" / "artifacts" / operation / operation / "campaign.jsonl"
        target = HERE / snapshot / "run" / operation / "campaign.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
        retained[scenario] = {"operation_id": operation, "source": str(source),
                              "path": str(target.relative_to(HERE / snapshot)),
                              "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    capture.snapshots[snapshot]["meta"]["retained_journals"] = retained
    capture.write()
    for kind, kept in retained.items():
        answer = capture.snapshots[snapshot]["routes"][f"/v1/operations/{kept['operation_id']}/suspensions"]
        matches = [entry for entry in answer["body"].get("suspensions") or []
                   if entry["evidence_kind"] == kind] if answer["status"] == 200 else []
        _check(bool(matches) and (kind != "stage_record" or any(
               "prefix" not in entry["evidence"] for entry in matches)), f"actual {kind} evidence", answer)


def _suspension_fault(capture: Capture, home: Path) -> None:
    """Spec §6: arm a missing-primary scorer on the last corpus case after admission."""
    with capture.service("main", home) as cli:
        project = cli("project", "demo", "demo-scorer-fault")[-1]
        wc = Path(project["working_copy"]["path"])
        materials = wc / "lab"
        manifest_path = materials / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        rows = [json.loads(line) for line in (materials / manifest["corpus"]["path"]).read_text().splitlines()
                if line.strip()]
        _check(len(rows) > 1 and rows[-1]["case_id"] != rows[0]["case_id"], "distinct last corpus case", rows)
        flag = wc.with_name(wc.name + ".scorer-fault")
        scorer = materials / manifest["scorer"]["cwd"]
        config = {"argv": manifest["scorer"]["argv"], "flag": str(flag),
                  "case_id": rows[-1]["case_id"], "primary": manifest["campaign"]["metric"]["primary"]}
        (scorer / "fault.json").write_text(json.dumps(config))
        (scorer / "fault.py").write_text(SCORER_FAULT_WRAPPER)
        manifest["scorer"]["argv"] = [manifest["scorer"]["argv"][0], "fault.py"]
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        digest, campaign = _connect(cli, capture.run, project["id"])
        flag.write_text("armed\n")
        target, _ = _project_target(cli, digest, project["id"])
        done = _wait_operation(cli, cli("project", "start", project["id"], "--prepared", campaign,
                                       "--target", target)[-1]["operation"], timeout=180)
        sock = home / "service.sock"
        capture.snapshot(sock, "suspension-fault", {"campaign_scorer_fault": done["id"]}, home)
        _retain_suspensions(capture, "suspension-fault", {"fault_rows": done["id"]})


SCORER_FAULT_WRAPPER = r'''import json
from pathlib import Path
import subprocess
import sys
config = json.loads(Path("fault.json").read_text())
request = sys.stdin.buffer.read()
result = subprocess.run(config["argv"], input=request, capture_output=True)
sys.stderr.buffer.write(result.stderr)
if result.returncode:
    sys.stdout.buffer.write(result.stdout)
    raise SystemExit(result.returncode)
output = json.loads(result.stdout)
if Path(config["flag"]).exists() and json.loads(request)["case_id"] == config["case_id"]:
    output["metrics"]["fixture_auxiliary"] = output["metrics"].pop(config["primary"])
print(json.dumps(output))
'''


def _observe_mixed(get, operation: str) -> None:
    """What core made of the mixed round, to stderr: the groups and failures to record in the README."""
    status, view = get(f"/v1/operations/{operation}/campaign")
    for rnd in (view.get("rounds") or [])[:1] if status == 200 else []:
        for attempt in rnd.get("attempts") or []:
            print(f"capture: mixed r{rnd.get('round')} {attempt.get('attempt_id')}: group={attempt.get('group')} "
                  f"failure={attempt.get('failure')}", file=sys.stderr)


def _connect(cli, run: Path, project: str) -> tuple[str, str]:
    """confirm, Bundle, connect, check, conformance (as the installed demo e2e); returns the
    Bundle digest and the prepared campaign digest."""
    cli("project", "confirm", project, "--implementation", "demo", "--claim", "spec.confirmed",
        "--path", "SPEC.md", "--author", "tester", "--link", "catalogue:requirements")
    digest = cli("project", "bundle", project)[-1]["bundle"]["digest"]
    connection = cli("connect", digest)[-1]["id"]
    first, first_gen = _project_target(cli, digest, project)
    checked = cli("check", connection, "--target", first, "--generation", first_gen, "--wait", timeout=90)[-1]
    _check(checked["state"] == "succeeded", "check", checked)
    conformed = cli("conformance", connection, "--target", first, "--generation", first_gen,
                    "--wait", timeout=180)[-1]
    _check(conformed["state"] == "succeeded", "conformance", conformed)
    request = run / f"campaign-{project}.json"
    request.write_text(json.dumps({"kind": "campaign", "bundle_digest": digest,
                                   "stop_grace_seconds": None, "cleanup_wait_seconds": None}))
    campaign = cli("project", "prepare", project, "--request", str(request))[-1]
    return digest, campaign["prepared"]["digest"]


def _fault_target(wc: Path, flag: Path) -> None:
    """Use the existing installed-resume fixture's fail_after:1 agent, before bundling.

    The flag is absent during connection/conformance; on the fresh campaign target it
    permits the one core connection preflight turn, then fails baseline. No demo agent
    behavior or core state is changed.
    """
    shutil.copyfile(REPO / "tests" / "owner_fixtures" / "echo_agent" / "echo_agent.py",
                    wc / "agent" / "echo_agent.py")
    path = wc / "launch.json"
    launch = json.loads(path.read_text(encoding="utf-8"))
    launch["agent"] = {"module": "echo_agent", "attribute": "build", "import_root": "agent"}
    launch["env"] = {"TARGET_MODE": f"fail_after:1:{flag}"}
    launch["limits"]["max_instances"] = 4
    path.write_text(json.dumps(launch, indent=2) + "\n", encoding="utf-8")
    path = wc / "lab" / "package.json"
    package = json.loads(path.read_text(encoding="utf-8"))
    package["env"] = dict(launch["env"])
    path.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def _role_suspension(cli, project: str, wc: Path, digest: str, campaign_digest: str) -> dict:
    (wc.with_name(wc.name + ".editor-fault")).write_text("")
    target, _ = _project_target(cli, digest, project)
    started = cli("project", "start", project, "--prepared", campaign_digest, "--target", target,
                  check=False, raw=True)
    if started.returncode != 0:
        return {"refused": started.stdout + started.stderr}
    return _wait_operation(cli, _json_lines(started)[-1]["operation"], timeout=180)


def _is_role_suspension(operation: dict) -> bool:
    core = ((operation.get("result") or {}).get("core")) or {}
    resume = operation.get("resume")
    return (operation.get("control_state") == "finished" and core.get("suspended") is True
            and resume is not None and resume.get("target") is None
            and (core.get("suspensions") or [{}])[-1].get("reason") == "editor_failing")


def _evaluation(cli, run: Path, project: str, wc: Path, digest: str) -> str:
    """The demo evaluation shared with the original capture; behavior is unchanged."""
    request = json.loads((wc / "lab" / "evaluation-request.json").read_text())
    request["bundle_digest"] = digest
    path = run / "evaluation.json"
    path.write_text(json.dumps(request))
    prepared = cli("project", "prepare", project, "--request", str(path))[-1]
    baseline_target, _ = _project_target(cli, digest, project)
    evaluation = _wait_operation(cli, cli("project", "start", project, "--prepared",
                                          prepared["prepared"]["digest"], "--target", baseline_target)[-1]["operation"])
    _check(evaluation["control_state"] == "finished", "evaluation", evaluation)
    return evaluation["id"]


def _chat_roles(materials: Path, port: int) -> None:
    path = materials / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    retry = {"version": "role_call_retry/1", "max_attempts": 1, "total_budget_seconds": 60,
             "backoff": {"initial_seconds": 0, "multiplier": 1, "max_seconds": 0, "jitter": "full"}}
    chat = {"kind": "chat", "model": "fake-model", "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key_env": "FAKE_CHAT_KEY", "max_turns": 16, "retry": retry,
            "context_compact_chars": 128_000, "context_message_max_chars": 12_000}
    manifest["editor"] = dict(chat)
    keep = {key: manifest["reflector"][key] for key in
            ("selection", "families", "max_consecutive_failures") if key in manifest["reflector"]}
    manifest["reflector"] = {**chat, **keep, "environment_docs": None, "probe": None}
    manifest["campaign"]["attempts_per_round"] = 2
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    from evolution.tree import load_manifest_v2
    from evolution_lab._manifest_schema import instance_demand

    launch_path = materials.parent / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    launch["limits"]["max_instances"] = instance_demand(
        load_manifest_v2(path), launch["limits"]["max_concurrent_sessions"])
    launch_path.write_text(json.dumps(launch, indent=2) + "\n", encoding="utf-8")


def _declare_secret(materials: Path, name: str) -> None:
    # Demo connection's package entry, not the target launch: roles receive bundle secrets.
    path = materials / "package.json"
    package = json.loads(path.read_text(encoding="utf-8"))
    package["secret_names"].append(name)
    path.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def _content_paths(get, ids) -> list[str]:
    paths = list(EMPTY_ROUTES)

    def take(path):
        paths.extend((path, path + "?disclose=private"))
        status, body = get(path)
        _check(status == 200, path, body)
        return body

    for name, operation in ids.items():
        base = f"/v1/operations/{quote(operation, safe='')}"
        take(base)
        view = take(base + "/campaign") if name != "evaluation" else None
        if view is not None:
            paths.append(base + "/suspensions")
        measurements = take(base + "/measurements")
        for measurement in measurements["measurements"]:
            path = base + "/measurements/" + quote(measurement["measurement_id"], safe="")
            detail = take(path)["measurement"]
            samples = detail.get("samples") or []
            selected = samples[:1]
            other = next((sample for sample in samples if sample["scoring_state"] != "finished"), None)
            if other is not None and other not in selected:
                selected.append(other)
            for sample in selected:
                take(path + "/cases/" + quote(sample["case_id"], safe="") + "/samples/" +
                     quote(sample["sample_id"], safe=""))
        if view is not None:
            for rnd in view["rounds"]:
                for attempt in rnd["attempts"]:
                    take(base + "/attempts/" + quote(attempt["attempt_id"], safe=""))
                reflection = rnd.get("reflection") or {}
                if reflection.get("reflection_id"):
                    take(base + "/reflections/" + quote(reflection["reflection_id"], safe=""))
    return list(dict.fromkeys(paths))


def _copy_runs(state: Path, ids: dict, target: Path) -> None:
    """Select only core journals, traces and role evidence; never copy trees or probe artifacts."""
    patterns = ("campaign.jsonl", "evaluation.jsonl", "measurements/*/events.jsonl", "scorer-traces/**/*",
                "editor-logs/*", "reflector-logs/*", "measurement/events.jsonl",
                "measurement/scorer-traces/**/*")
    # Replace only this generated snapshot's selected evidence. Original captures stay in capture.run.
    if target.exists():
        shutil.rmtree(target)
    for operation in ids.values():
        source = state / "artifacts" / operation / operation
        for pattern in patterns:
            for path in source.glob(pattern):
                relative = path.relative_to(source)
                if (not path.is_file() or "probes" in relative.parts or
                        any(parent.is_symlink() for parent in (source, *path.parents[:len(relative.parts)], path))):
                    continue
                destination = target / operation / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                destination.chmod(0o644)


def _content(capture: Capture, home: Path, run: Path) -> None:
    from fake_chat import KEY, FakeChat
    fake = FakeChat()
    secrets = run / "chat-secrets.json"
    secrets.write_text(json.dumps({"FAKE_CHAT_KEY": KEY}))
    secrets.chmod(0o600)
    try:
        with capture.service("main", home) as cli:
            sock = home / "service.sock"
            reads, get = _getter(sock)
            with reads:
                ids = {}
                for name, chat in (("content-sub", False), ("content-chat", True)):
                    for ready in get("/v1/targets")[1].get("targets", []):
                        if ready.get("state") == "ready":
                            cli("target", "stop", ready["id"], "--generation", ready["owner_generation"], "--wait")
                    project = cli("project", "demo", name)[-1]
                    wc = Path(project["working_copy"]["path"])
                    schema = json.loads((wc / "lab" / "manifest.json").read_text(encoding="utf-8"))["schema"]
                    _check(schema == "evolution-v2-manifest/15", "content needs the /15 demo", schema)
                    if chat:
                        _chat_roles(wc / "lab", fake.port)
                        _declare_secret(wc / "lab", "FAKE_CHAT_KEY")
                    digest, campaign = _connect(cli, run, project["id"])
                    if not chat:
                        ids["evaluation"] = _evaluation(cli, run, project["id"], wc, digest)
                    target, _ = _project_target(cli, digest, project["id"])
                    extra = ("--secrets-file", str(secrets)) if chat else ()
                    done = _wait_operation(cli, cli("project", "start", project["id"], "--prepared", campaign,
                                                    "--target", target, *extra)[-1]["operation"], timeout=900)
                    _check(done["control_state"] == "finished", f"campaign {name}", done)
                    ids["campaign_chat" if chat else "campaign_subprocess"] = done["id"]
                capture.snapshot(sock, "content", ids, home, paths=_content_paths(get, ids))
                _copy_runs(capture.run / "state-main", ids, HERE / "content" / "run")
    finally:
        (capture.run / "fake-chat.json").write_text(json.dumps(fake.requests, ensure_ascii=False, indent=1))
        fake.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lab-wheel", type=Path, required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--only", choices=("content", "family14", "family15", "suspension-fault"))
    args = parser.parse_args()
    console = Path(sys.executable).with_name("lab")
    _check(console.is_file(), "the installed environment must provide the lab console", str(console))
    run = REPO / ".superpowers" / "panel-capture" / uuid.uuid4().hex
    run.mkdir(parents=True, mode=0o700)
    started_at = time.monotonic()
    capture = Capture(console, (args.lab_wheel.resolve(), args.core_wheel.resolve()), run)
    short = Path(tempfile.mkdtemp(prefix="pfx-", dir=os.environ.get("TMPDIR")))  # short: the socket lives in the home
    short.chmod(0o700)
    home, empty_home = short / "home", short / "empty"
    deviation = None
    try:
        if args.only == "content":
            _content(capture, home, run)
        elif args.only == "family14":
            _family14(capture, home)
        elif args.only == "family15":
            _family15(capture, home)
        elif args.only == "suspension-fault":
            _suspension_fault(capture, home)
        else:
            with capture.service("main", home) as cli:
                sock = home / "service.sock"
                _check(sock.is_socket() and len(str(sock).encode()) < 108, "socket", str(sock))
                reads, get = _getter(sock)

                # 2. Demo, the Editor wrapper before the Bundle, connect.
                project = cli("project", "demo", PROJECT)[-1]
                wc = Path(project["working_copy"]["path"])
                schema = json.loads((wc / "lab" / "manifest.json").read_text(encoding="utf-8"))["schema"]
                _check(schema == "evolution-v2-manifest/15", "default capture needs the /15 demo", schema)
                hold = wc.with_name(wc.name + ".hold")
                _wrap_editor(wc / "lab", wc)
                digest, campaign_digest = _connect(cli, run, PROJECT)

                # 3. The evaluation (the demo e2e's baseline).
                evaluation_id = _evaluation(cli, run, PROJECT, wc, digest)
                ids = {"evaluation": evaluation_id}

                # 4. Campaign 1 held in the Editor: the running snapshot, then its end and an export.
                hold.write_text("")
                target, _ = _project_target(cli, digest)
                cycle = cli("project", "start", PROJECT, "--prepared", campaign_digest, "--target", target)[-1]["operation"]
                frames: list[str] = []
                capture.events[cycle["id"]] = frames
                reader = threading.Thread(target=_record_events, args=(sock, cycle["id"], frames), daemon=True)
                reader.start()
                deadline = time.monotonic() + 120
                while True:
                    status, view = get(f"/v1/operations/{cycle['id']}/campaign")
                    if status == 200 and any(a.get("group") == "in_progress"
                                             for r in view.get("rounds") or [] for a in r.get("attempts") or []):
                        break
                    _check(time.monotonic() < deadline, "no in_progress attempt within 120 s", view)
                    time.sleep(0.2)
                held_at = time.monotonic()
                capture.snapshot(sock, "running", {**ids, "campaign_running": cycle["id"]}, home)
                print(f"capture: running snapshot took {time.monotonic() - held_at:.1f} s under the hold",
                      file=sys.stderr)
                hold.unlink()
                cycle = _wait_operation(cli, cli("status", cycle["id"])[-1], timeout=180)
                _check(cycle["control_state"] == "finished" and cycle["result"]["core"]["suspended"] is False,
                       "campaign 1", cycle)
                cli("export", cycle["id"], "--wait", timeout=90)
                reader.join(timeout=60)
                ids["campaign_finished"] = cycle["id"]

                # 5. Campaign 2 on a failing target: suspended, a fresh target required; then resumed.
                for ready in get("/v1/targets")[1].get("targets", []):
                    if ready.get("state") == "ready":
                        cli("target", "stop", ready["id"], "--generation", ready["owner_generation"], "--wait")
                target_project = cli("project", "demo", "demo-target")[-1]
                target_wc = Path(target_project["working_copy"]["path"])
                target_fault = target_wc.with_name(target_wc.name + ".baseline-fault")
                _fault_target(target_wc, target_fault)
                target_digest, target_campaign = _connect(cli, run, target_project["id"])
                ids["project_target"] = target_project["id"]
                target_fault.write_text("")
                faulty, faulty_gen = _project_target(cli, target_digest, target_project["id"])
                suspended = _wait_operation(cli, cli("project", "start", target_project["id"], "--prepared", target_campaign,
                                                     "--target", faulty)[-1]["operation"])
                _check(suspended["result"]["core"]["suspensions"][-1]["reason"] == "target_unstable",
                       "campaign 2 baseline target_unstable", suspended)
                _check(((suspended.get("resume") or {}).get("target") or {}).get("requirement") == "fresh_instance",
                       "campaign 2 resume", suspended)
                capture.snapshot(sock, "suspended-target", {**ids, "campaign_suspended_target": suspended["id"]}, home)
                stopped = cli("target", "stop", faulty, "--generation", faulty_gen, "--wait")[-1]
                _check(stopped["state"] == "succeeded", "stop faulty target", stopped)
                target_fault.unlink()
                fresh, fresh_gen = _project_target(cli, target_digest, target_project["id"])
                cli("resume", suspended["id"], "--target", fresh, "--generation", fresh_gen)
                resumed = _wait_operation(cli, cli("status", suspended["id"])[-1], timeout=180)
                _check(resumed["control_state"] == "finished", "campaign 2 resume", resumed)
                ids["campaign_resumed"] = resumed["id"]

                # 6. Campaign 3 with a failing Editor: a role suspension (no target requirement).
                for ready in get("/v1/targets")[1].get("targets", []):
                    if ready.get("state") == "ready":
                        cli("target", "stop", ready["id"], "--generation", ready["owner_generation"], "--wait")
                role = _role_suspension(cli, PROJECT, wc, digest, campaign_digest)
                if not _is_role_suspension(role):
                    deviation = {"project": PROJECT, "answer": role}
                    other = cli("project", "demo", "demo-role")[-1]
                    other_wc = Path(other["working_copy"]["path"])
                    _wrap_editor(other_wc / "lab", other_wc)
                    other_digest, other_campaign = _connect(cli, run, other["id"])
                    role = _role_suspension(cli, other["id"], other_wc, other_digest, other_campaign)
                    _check(_is_role_suspension(role), "role suspension on demo-role", role)
                    ids["project_role"] = other["id"]
                ids["campaign_suspended_role"] = role["id"]
                capture.snapshot(sock, "suspended-role", ids, home)

                # 7. Campaign 1's results deleted (it was exported): the final snapshot.
                cli("delete-results", ids["campaign_finished"], "--wait", timeout=90)
                capture.snapshot(sock, "final", ids, home)

                # 7b. A second demo project with three attempts per round: an Editor that declines, a candidate
                # that does not improve, an improvement (the details snapshot, plan 2026-09-30 panel-2a-experiments).
                # The service runs one target at a time (a ready target of another Bundle refuses the next).
                for ready in get("/v1/targets")[1].get("targets", []):
                    if ready.get("state") == "ready":
                        stopped = cli("target", "stop", ready["id"], "--generation", ready["owner_generation"],
                                      "--wait")[-1]
                        _check(stopped["state"] == "succeeded", "stop target before demo-mix", stopped)
                mixed = cli("project", "demo", "demo-mix")[-1]
                mixed_wc = Path(mixed["working_copy"]["path"])
                _wrap_editor(mixed_wc / "lab", mixed_wc)
                _set_attempts(mixed_wc / "lab", 3)
                mixed_digest, mixed_campaign = _connect(cli, run, mixed["id"])
                mixed_wc.with_name(mixed_wc.name + ".editor-script").write_text(json.dumps(["refuse", "plain", "pass"]))
                mixed_target, _ = _project_target(cli, mixed_digest, mixed["id"])
                cycle4 = _wait_operation(cli, cli("project", "start", mixed["id"], "--prepared", mixed_campaign,
                                                  "--target", mixed_target)[-1]["operation"], timeout=240)
                _check(cycle4["control_state"] == "finished", "campaign demo-mix", cycle4)
                ids_details = {**ids, "project_mixed": mixed["id"], "campaign_mixed": cycle4["id"]}
                capture.snapshot(sock, "details", ids_details, home)
                _retain_suspensions(capture, "details", {
                    "threshold": ids["campaign_resumed"],
                    "stage_record": ids["campaign_suspended_role"]})
                _observe_mixed(get, cycle4["id"])
                reads.close()

            # 8. A second service on a new empty home.
            with capture.service("empty", empty_home):
                capture.snapshot(empty_home / "service.sock", "empty", {}, empty_home, paths=EMPTY_ROUTES)

        capture.write()
        capture.receipt["completed"] = True
    finally:
        capture.write_receipt()
        capture.commands.close()
        if deviation is not None:
            (run / "role-deviation.json").write_text(json.dumps(deviation, indent=1))
        shutil.rmtree(short, ignore_errors=True)
    print(f"capture: done in {time.monotonic() - started_at:.0f} s; run files in {run}"
          + (f"; role suspension moved to demo-role, see {run / 'role-deviation.json'}" if deviation else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
