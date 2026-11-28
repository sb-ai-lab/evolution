"""Lab target launcher: `python -I -m evolution_lab.target.launch` (ADR-0013).

The two inherited descriptors are taken before any import that could fork.
"""
import os

_config_fd = int(os.environ.pop("LAB_TARGET_CONFIG_FD"))
_ready_fd = int(os.environ.pop("LAB_TARGET_READY_FD"))
os.set_inheritable(_config_fd, False)
os.set_inheritable(_ready_fd, False)

import importlib  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from collections.abc import Callable, Mapping  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

from evolution_lab.owner.protocol import encode_frame, read_frame  # noqa: E402
from evolution_lab.target._contract import AgentFactory, LaunchContext  # noqa: E402

RUNTIME_SCHEMA = "lab-target-runtime/2"
READY_SCHEMA = "lab-target-ready/1"


def load_config(fd: int) -> dict[str, Any]:
    try:
        config = read_frame(fd)
    finally:
        os.close(fd)
    if not isinstance(config, dict) or config.get("schema") != RUNTIME_SCHEMA:
        raise ValueError("launcher expects lab-target-runtime/2")
    empty_answer_from(config)
    return config


def empty_answer_from(config: Mapping[str, Any]) -> str:
    turn = config.get("turn", {"empty_answer": "target_failed"})
    if (not isinstance(turn, Mapping) or set(turn) != {"empty_answer"}
            or not isinstance(turn["empty_answer"], str) or turn["empty_answer"] not in ("target_failed", "ok")):
        raise ValueError("runtime turn must contain only empty_answer: target_failed or ok")
    return turn["empty_answer"]


def context_from(config: Mapping[str, Any], environ: Mapping[str, str]) -> LaunchContext:
    names = set(config["secret_names"])
    return LaunchContext(env={k: v for k, v in environ.items() if k not in names},
                         secrets={k: v for k, v in environ.items() if k in names},
                         work_directory=Path(config["work_directory"]))


def import_build(agent: Mapping[str, Any]) -> Callable[[LaunchContext], AgentFactory]:
    if agent["import_root"] is not None:
        sys.path.insert(0, agent["import_root"])
    return getattr(importlib.import_module(agent["module"]), agent["attribute"])


def run(config: Mapping[str, Any], stop: threading.Event, ready_fd: int) -> int:
    empty_answer = empty_answer_from(config)
    context = context_from(config, os.environ)
    factory = import_build(config["agent"])(context)
    if stop.is_set():
        # A stop that arrived while the agent was building must not bind a
        # server or write a ready frame: nothing has started yet to stop.
        return 0

    from evolution.tree import ArtifactPolicyV1, TargetServer, serve_target
    from evolution_lab.target import backend

    server = TargetServer(backend(factory, secrets=tuple(context.secrets.values()), empty_answer=empty_answer),
                          allowed_artifact_root=Path(config["allowed_artifact_root"]),
                          snapshot_root=Path(config["snapshot_root"]),
                          policy=ArtifactPolicyV1.from_payload(config["policy"]),
                          **config["identity"], **config["limits"])
    http = None
    thread = None
    try:
        address = urlsplit(config["base_url"])
        http = serve_target(server, address.hostname, address.port)
        thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        frame = encode_frame({"schema": READY_SCHEMA, "launch_id": config["launch_id"],
                              "launch_digest": config["launch_digest"], "base_url": config["base_url"],
                              "boot_id": server.boot_id})
        written = os.write(ready_fd, frame)
        if written != len(frame):
            raise RuntimeError("incomplete target ready frame")
        os.close(ready_fd)
        ready_fd = -1
        stop.wait()
    finally:
        if ready_fd >= 0:
            os.close(ready_fd)
        if http is not None:
            http.shutdown()
            http.server_close()
        if thread is not None:
            thread.join()
        server.close()
    return 0


def main() -> int:
    config = load_config(_config_fd)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    return run(config, stop, _ready_fd)


if __name__ == "__main__":
    sys.exit(main())
