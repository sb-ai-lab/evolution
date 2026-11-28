"""Task7 offline b13 contract capture; preserves every historical panel fixture.

Run with the installed pair's Python and --lab-wheel/--core-wheel. All generated
state and producing wheels remain in --retained; only selected evidence is exported.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

import capture as base
from fake_chat import FakeChat, KEY


class ShadowChat(FakeChat):
    """A neutral diagnosis deliberately contains the declared fake run secret.

    The real b13 reflection_run_secret check must detect and admit it in shadow;
    Core's independent journal redaction must still remove the value.
    """
    def _reflector(self, results):
        reply = super()._reflector(results)
        message = reply["choices"][0]["message"]
        for call in message.get("tool_calls", []):
            if call["function"]["name"] == "finish_reflection":
                arguments = json.loads(call["function"]["arguments"])
                for group in arguments.get("groups", []):
                    if "root_cause" in group:
                        group["root_cause"] = "neutral wiring " + KEY
                call["function"]["arguments"] = json.dumps(arguments)
        return reply


def copy_worker_logs(run, name, ids):
    """Export only the campaign/evaluation stderr matched by its settlement id."""
    for operation in ids.values():
        for settlement in (run / "state-main" / "scopes").glob("*/settlement.json"):
            if json.loads(settlement.read_text()).get("operation_id") == operation:
                target = base.HERE / name / "worker-logs" / operation / "stderr.log"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(settlement.with_name("stderr.log"), target)


def family16(capture, home, *, checks=False):
    sys.path.insert(0, str(base.REPO / "tests"))
    from scaffold_16 import as_manifest_16

    name = "checks" if checks else "family15"
    fake = ShadowChat() if checks else None
    secrets = capture.run / "chat-secrets.json"
    if checks:
        secrets.write_text(json.dumps({"FAKE_CHAT_KEY": KEY}))
        secrets.chmod(0o600)
    try:
        with capture.service("main", home) as cli:
            reads, get = base._getter(home / "service.sock")
            with reads:
                project = cli("project", "demo", "demo-" + name)[-1]
                wc = Path(project["working_copy"]["path"])
                path = wc / "lab" / "manifest.json"
                manifest = as_manifest_16(json.loads(path.read_text()), mode="confidence", repeats=1)
                if checks:
                    manifest["checks"] = {"version": "checks/1", "modes": {
                        "reflection_run_secret": "shadow", "seal_oracle_leak": "off"}}
                path.write_text(json.dumps(manifest, indent=2) + "\n")
                if checks:
                    base._chat_roles(wc / "lab", fake.port)
                    base._declare_secret(wc / "lab", "FAKE_CHAT_KEY")
                digest, prepared = base._connect(cli, capture.run, project["id"])
                target, _ = base._project_target(cli, digest, project["id"])
                extra = ("--secrets-file", str(secrets)) if checks else ()
                done = base._wait_operation(cli, cli("project", "start", project["id"],
                    "--prepared", prepared, "--target", target, *extra)[-1]["operation"], timeout=900)
                base._check(done["control_state"] == "finished" and not
                    ((done.get("result") or {}).get("core") or {}).get("suspended"), name + " finished", done)
                ids = {"campaign_checks" if checks else "campaign_confidence": done["id"]}
                status, view = get(f"/v1/operations/{done['id']}/campaign")
                base._check(status == 200 and view["schema"] == "lab-campaign-view/11"
                    and view["family"] == 15, name + " current view", view)
                capture.snapshot(home / "service.sock", name, ids, home,
                    paths=base._content_paths(get, ids) if checks else None)
                base._copy_runs(capture.run / "state-main", ids, base.HERE / name / "run")
    finally:
        if fake is not None:
            (capture.run / "fake-chat.json").write_text(json.dumps(fake.requests, indent=1))
            fake.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-wheel", type=Path, required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--retained", type=Path, required=True)
    parser.add_argument("--only", choices=("family14", "content", "family15", "checks"), required=True)
    args = parser.parse_args()
    base.HERE = Path(__file__).resolve().parent / "b13"
    base.HERE.mkdir(exist_ok=True)
    destination = base.HERE / args.only
    if destination.exists():
        raise RuntimeError(f"preserve prior capture: {destination}")
    run = args.retained / (args.only + "-" + uuid.uuid4().hex)
    run.mkdir(parents=True, mode=0o700)
    capture = base.Capture(Path(sys.executable).with_name("lab"),
        (args.lab_wheel.resolve(), args.core_wheel.resolve()), run)
    capture.helper["task7_generator"] = {"path": str(Path(__file__).relative_to(base.REPO)),
        "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    shutil.copyfile(__file__, run / "capture_b13.py")
    short = Path(tempfile.mkdtemp(prefix="b13-", dir=os.environ.get("TMPDIR")))
    started = time.monotonic()
    try:
        if args.only == "family14":
            base._family14(capture, short / "home")
            capture.kept.clear()  # historical family14 projection files remain untouched
        elif args.only == "content":
            base._content(capture, short / "home", run)
        else:
            family16(capture, short / "home", checks=args.only == "checks")
        for name, snapshot in capture.snapshots.items():
            copy_worker_logs(run, name, snapshot["meta"]["ids"])
        capture.write()
        capture.receipt["completed"] = True
        capture.receipt["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    finally:
        capture.write_receipt()
        capture.commands.close()
        shutil.rmtree(short, ignore_errors=True)
    print(f"capture {args.only} completed in {time.monotonic() - started:.1f}s; {run}", flush=True)


if __name__ == "__main__":
    main()
