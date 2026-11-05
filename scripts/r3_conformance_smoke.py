"""Offline R3 native-target-surface conformance smoke; runs with an installed
wheel or this source tree.

Starts the `/evolution/v2` reference target fixture on an ephemeral port,
writes its own three-field service input pointing at it, builds the in-memory
input with `conformance_manifest`, runs both conformance suites through
`run_conformance`, and asserts: the report is
`ok`; the target has released every instance it created; a canary byte
placed in the seed's excluded `.env` never reaches the report; and nothing
is left under the target's allowed artifact root except the seed tree and
the manifest document themselves.

The reference target is the packaged one (`evolution.tree.ReferenceTarget`, R3-ref).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import stat
import sys
import tempfile
import threading
from pathlib import Path

from evolution.tree import (
    ArtifactPolicyV1,
    ReferenceTarget,
    conformance_manifest,
    run_conformance,
)
from evolution.tree import (
    serve_reference_target as serve,
)

CANARY = b"CANARY-R3-SMOKE-8f2a71"
HEALTH_PATH = "/evolution/v2/health"


def write(path: Path, data: bytes, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def seed_tree(root: Path) -> Path:
    """A seed with a canary in `.env`: excluded by the policy, so it must
    never reach a published copy, the report, or the target's own reply."""
    write(root / "skills" / "SKILL.md", b"skill\n")
    write(root / "scripts" / "run.sh", b"#!/bin/sh\necho ok\n", executable=True)
    write(root / "uv.lock", b"lock\n")
    write(root / ".env", CANARY)
    return root


def health_instances(port: int) -> list[str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", HEALTH_PATH)
        body = json.loads(conn.getresponse().read().decode("utf-8"))
    finally:
        conn.close()
    return list(body["instances"])


def run(tmp: Path) -> dict:
    allowed = tmp / "allowed"
    allowed.mkdir()
    seed_root = seed_tree(allowed / "seed")
    policy = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))

    target = ReferenceTarget(allowed_artifact_root=allowed, policy=policy)
    server = serve(target, host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    port = server.server_address[1]

    try:
        manifest_payload = {
            "seed_root": str(seed_root),
            "target": {
                "base_url": f"http://127.0.0.1:{port}",
                "seed_mode": "exact",
            },
            "artifacts": {
                "excludes": [],
                "dependency_paths": ["uv.lock"],
                "writable_paths": ["skills/**"],
            },
        }
        manifest_path = allowed / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

        manifest = conformance_manifest(manifest_payload, base=allowed)
        report = asyncio.run(run_conformance(manifest))
        payload = report.to_payload()
        rendered = json.dumps(payload)

        check(report.ok, f"conformance report was not ok: {rendered}")
        check(health_instances(port) == [], "an instance was left on the target after the run")
        check(CANARY.decode("ascii") not in rendered, "canary leaked into the conformance report")
        remaining = sorted(entry.name for entry in allowed.iterdir())
        check(
            remaining == ["manifest.json", "seed"],
            f"unexpected entries left under the allowed root: {remaining}",
        )
    finally:
        # The documented close order: the HTTP server's `shutdown()`, its
        # `server_close()`, then the target's own `close()`, which releases
        # every instance and removes the snapshots it owns.
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        target.close()

    return payload


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="evo-r3-conformance-smoke-") as directory:
            payload = run(Path(directory))
    except Exception as exc:  # noqa: BLE001 - report and fail loudly, this is a smoke script
        print(f"R3 CONFORMANCE SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
