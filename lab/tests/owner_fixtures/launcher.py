"""Neutral external launcher used only by the authorized S2 target fixtures."""
import os

# Close the inheritance window before importing code that might create children.
config_fd = int(os.environ.pop('LAB_TARGET_CONFIG_FD'))
ready_fd = int(os.environ.pop('LAB_TARGET_READY_FD'))
os.set_inheritable(config_fd, False)
os.set_inheritable(ready_fd, False)

import signal
import threading
from pathlib import Path
from urllib.parse import urlsplit

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.target.reference_target import ReferenceBackend
from evolution.tree.target.target_server import TargetServer, serve_target
from evolution.tree.execution.wire_v2 import canonical_body
from evolution_lab.owner.protocol import read_frame, encode_frame

try:
    config = read_frame(config_fd)
finally:
    os.close(config_fd)
stopped = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopped.set())
signal.signal(signal.SIGINT, lambda *_: stopped.set())
mode = os.environ.get('TARGET_MODE', 'normal')
if mode == 'stall':
    stopped.wait()
    os.close(ready_fd)
    raise SystemExit(0)
server = TargetServer(
    ReferenceBackend(),
    allowed_artifact_root=Path(config['allowed_artifact_root']),
    snapshot_root=Path(config['snapshot_root']),
    policy=ArtifactPolicyV1.from_payload(config['policy']),
    **config['identity'], **config['limits'],
)
if mode.startswith("fail_after:"):
    real_dispatch = server.dispatch
    turn_lock, turns = threading.Lock(), 0

    def dispatch(method, path, raw_body=b""):
        global turns
        if method == "POST" and path.endswith("/turns"):
            with turn_lock:
                turns += 1
                turn = turns
            _, successful_turns, flag = mode.split(":", 2)
            fail = turn > int(successful_turns) and Path(flag).exists()
            if fail:
                return 500, canonical_body(
                    {"error": {"code": "internal", "message": "internal", "retryable": True}})
        return real_dispatch(method, path, raw_body)

    server.dispatch = dispatch
http = None
try:
    endpoint = urlsplit(config['base_url'])
    http = serve_target(server, endpoint.hostname, endpoint.port)
    thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    ready = dict(schema='lab-target-ready/1', launch_id=config['launch_id'],
        launch_digest=config['launch_digest'], base_url=config['base_url'], boot_id=server.boot_id)
    if mode == 'wrong_boot':
        ready['boot_id'] = 'another-boot'
    if mode == 'extra_ready':
        ready['unexpected'] = True
    os.write(ready_fd, encode_frame(ready))
    os.close(ready_fd)
    ready_fd = -1
    stopped.wait()
finally:
    if ready_fd >= 0:
        os.close(ready_fd)
    if http is not None:
        http.shutdown()
        http.server_close()
        thread.join()
    server.close()
