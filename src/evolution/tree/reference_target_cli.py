"""`evo-target`: the packaged reference `/evolution/v2` target from a shell.

`init DIR` scaffolds a demonstration directory (a `/15` manifest, the seed
tree, a two-case corpus and a sample scorer and editor); `serve MANIFEST`
starts the reference target bound to that manifest. The target is, by
construction, the one the manifest describes: its policy, loopback address
and seed mode come from the manifest, its capacities and semantic identity
are the constants below, and `bind_reference_target` refuses a manifest the
runtime's preflight would refuse (spec §4).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import signal
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from evolution.lineage.state_io import (
    atomic_write_private_bytes,
    atomic_write_private_text,
    ensure_private_directory,
)
from evolution.tree import process_log
from evolution.tree.execution.config import ManifestError
from evolution.tree.manifest.manifest_v2 import ManifestV2, load_manifest_v2
from evolution.tree.target.reference_target import ReferenceTarget
from evolution.tree.target.reference_target import serve as _serve_target

REFERENCE_CONTRACT_DIGEST = "reference-contract-1"
REFERENCE_SETTINGS_HASH = "reference-settings-1"
MAX_INPUT_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 1 << 20

BIND_CODES = frozenset(
    {"root_not_directory", "seed_outside_root", "pin_mismatch", "limits_incompatible"}
)


class BindError(ValueError):
    """A manifest the reference target cannot be bound to; `code` is closed (§4)."""

    def __init__(self, code: str) -> None:
        assert code in BIND_CODES
        super().__init__(code)
        self.code = code


def bind_reference_target(
    manifest: ManifestV2, allowed_root: Path | None = None
) -> ReferenceTarget:
    """Build the target the manifest describes, or refuse with a preflight code."""

    root = Path(
        os.path.realpath(allowed_root if allowed_root is not None else manifest.path.parent)
    )
    if not root.is_dir():
        raise BindError("root_not_directory")
    seed = Path(os.path.realpath(manifest.seed_root))
    if seed != root and root not in seed.parents:
        raise BindError("seed_outside_root")
    binding = manifest.target
    if binding.contract_digest is not None and binding.contract_digest != REFERENCE_CONTRACT_DIGEST:
        raise BindError("pin_mismatch")
    if binding.settings_hash is not None and binding.settings_hash != REFERENCE_SETTINGS_HASH:
        raise BindError("pin_mismatch")
    if binding.min_input_bytes > MAX_INPUT_BYTES or binding.max_response_bytes < MAX_OUTPUT_BYTES:
        raise BindError("limits_incompatible")
    return ReferenceTarget(
        allowed_artifact_root=root,
        policy=manifest.policy,
        seed_mode=binding.seed_mode,
        max_input_bytes=MAX_INPUT_BYTES,
        max_output_bytes=MAX_OUTPUT_BYTES,
        contract_digest=REFERENCE_CONTRACT_DIGEST,
        settings_hash=REFERENCE_SETTINGS_HASH,
    )


SAMPLE_PORT = 8765
SAMPLE_MANIFEST: dict[str, Any] = {
    "schema": "evolution-v2-manifest/15",
    "seed_root": "seed",
    "target": {"base_url": f"http://127.0.0.1:{SAMPLE_PORT}", "seed_mode": "exact"},
    "artifacts": {
        "excludes": ["build/**"],
        "dependency_paths": ["uv.lock"],
        "writable_paths": ["skills/**"],
    },
    "role_visibility": {"version": "role_visibility/1", "readable_paths": []},
    "campaign": {
        "rounds": 2,
        "attempts_per_round": 1,
        "parallel_attempts": 1,
        "evaluation": {"repeats_per_question": 3},
        "round_stop": "first_accepted",
        "baseline_refresh_after_rounds": 3,
        "indeterminate": {
            owner: {"max_count": 2, "max_rate": "0.5"}
            for owner in ("target", "environment", "scorer", "rollout")
        },
        "budget": {"max_turns": 2, "timeout_seconds": 30, "max_failed_turns": 1},
        "call_retries": {"max_attempts": 4, "turn_max_attempts": 1},
        "max_concurrency": 2,
        "max_scorer_concurrency": 2,
        "metric": {"primary": "score"},
        "random_seed": 7,
        "editor_timeout_seconds": 60,
        "editor_context": None,
        "reflection_timeout_seconds": 60,
        "gate": {
            "mode": "statistical",
            "stability": "stable",
            "focus": {"confidence": 0.9, "margin": 0},
            "causal": {"confidence": 0.9, "tolerance": "measured_noise"},
            "provenance": {"kind": "reported"},
            "fresh_parent": False,
            "global": "point",
        },
        "attempt_feedback": "sequential",
    },
    "environment": {"kind": "static", "input_text_key": "text"},
    "scorer": {
        "argv": ["python3", "score.py"],
        "cwd": "scorer",
        "timeout_seconds": 30,
        "dependency_paths": [],
        "allowed_env": [],
        # The reference target answers every turn with one non-empty
        # `assistant_message` (ADR-0029): the contract the scorer reads.
        "metric_contract": {
            "version": "metric_contract/1",
            "kind": "pointwise",
            "target_fields": [
                {"event": "assistant_message", "pointer": "/content", "value": "nonempty_string"}
            ],
        },
    },
    "editor": {
        "kind": "subprocess",
        "argv": ["python3", "edit.py"],
        "cwd": "editor",
        "dependency_paths": [],
        "allowed_env": [],
    },
    "grouper": {
        "kind": "fields",
        "features": [
            {
                "name": "failed_criteria",
                "source": "observation",
                "path": "/failed_criteria",
                "description": "Failed criteria.",
            },
            {
                "name": "terminal",
                "source": "observation",
                "path": "/terminal",
                "description": "Terminal observation status.",
            },
        ],
    },
    "reflector": {
        "kind": "subprocess",
        "argv": ["python3", "reflect.py"],
        "cwd": "reflector",
        "dependency_paths": [],
        "allowed_env": [],
        "environment_docs": None,
        "probe": None,
        "max_consecutive_failures": 2,
        "selection": {"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        "families": {"kind": "none"},
    },
    "corpus": {"path": "corpus.jsonl"},
}
SAMPLE_SKILL_MD = (
    "Reference skill: answer with this line.\n"
    "\n"
    "This tree is the seed of an Evolution v2 demonstration. The reference target\n"
    "answers every turn with the first line of this file followed by the message.\n"
)
SAMPLE_UV_LOCK = "# placeholder dependency file for the reference target sample\n"

SAMPLE_PROGRAM_PATHS: dict[str, tuple[str, ...]] = {
    "score.py": ("scorer", "score.py"),
    "edit.py": ("editor", "edit.py"),
    "reflect.py": ("reflector", "reflect.py"),
    "corpus.jsonl": ("corpus.jsonl",),
}
SAMPLE_PROGRAM_NAMES = tuple(SAMPLE_PROGRAM_PATHS)

SCAFFOLD_CODES = frozenset({"not_a_directory", "dir_not_empty", "scaffold_failed"})


def render_sample_manifest() -> str:
    return json.dumps(SAMPLE_MANIFEST, indent=2) + "\n"


def sample_program_bytes(name: str) -> bytes:
    """The bytes of one packaged sample file, exactly as `init` copies them.

    Bytes, not text: `Path.read_text` translates newlines, so a program with
    `\r\n` line endings would be normalized by the scaffold (W2 finding F3).
    """
    if name not in SAMPLE_PROGRAM_PATHS:
        raise ValueError(f"unknown sample file: {name}")
    return (resources.files("evolution.tree.reference_sample") / name).read_bytes()


def sample_program_text(name: str) -> str:
    """The text of one packaged sample file: a plain decode of its bytes."""
    return sample_program_bytes(name).decode("utf-8")


class ScaffoldError(ValueError):
    def __init__(self, code: str) -> None:
        assert code in SCAFFOLD_CODES
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ScaffoldResult:
    dir: Path
    manifest: Path
    seed_root: Path


def _nonempty_directory(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def scaffold(target_dir: Path) -> ScaffoldResult:
    """Create the demonstration directory atomically (spec §3.1).

    Files are written into a staging sibling and renamed onto `target_dir`
    in one `rename(2)`, which succeeds onto a missing or empty directory and
    fails onto anything else; any failure removes the staging directory and
    leaves `target_dir` as it was.

    Every filesystem inspection, write and cleanup — the pre-checks included —
    runs inside one boundary. The pre-checks still run before any write and
    still yield `not_a_directory`/`dir_not_empty`, but they no longer run
    outside the `try`: an unlistable `target_dir` raises `PermissionError` out
    of `_nonempty_directory`'s `iterdir()`, and that must be classified like
    any other `OSError` here, not left to escape as a bare traceback. Cleanup
    in `finally` is swallowed outright (`contextlib.suppress(OSError)`), so a
    failure removing the staging directory can never replace the typed result
    — or an unclassified `BaseException`'s traceback — already decided above.
    """

    final = Path(os.path.abspath(target_dir))
    parent = final.parent
    staging = parent / f".{final.name}.evo-target-{os.getpid()}"
    try:
        if final.is_symlink():
            # A rename can never target a symlink: `os.rename` onto one
            # replaces the link itself, not the directory it points at, which
            # is never the scaffold's intent.
            raise ScaffoldError("not_a_directory")
        if final.exists() and not final.is_dir():
            raise ScaffoldError("not_a_directory")
        if _nonempty_directory(final):
            raise ScaffoldError("dir_not_empty")
        parent.mkdir(parents=True, exist_ok=True)
        # Clear any pre-existing staging path (symlink, file, or stale directory)
        if staging.is_symlink() or staging.is_file():
            os.unlink(staging)
        elif staging.is_dir():
            shutil.rmtree(staging)
        ensure_private_directory(staging)
        atomic_write_private_text(staging / "manifest.json", render_sample_manifest())
        ensure_private_directory(staging / "seed")
        ensure_private_directory(staging / "seed" / "skills")
        atomic_write_private_text(staging / "seed" / "skills" / "SKILL.md", SAMPLE_SKILL_MD)
        atomic_write_private_text(staging / "seed" / "uv.lock", SAMPLE_UV_LOCK)
        ensure_private_directory(staging / "scorer")
        ensure_private_directory(staging / "editor")
        ensure_private_directory(staging / "reflector")
        for name in SAMPLE_PROGRAM_NAMES:
            atomic_write_private_bytes(
                staging.joinpath(*SAMPLE_PROGRAM_PATHS[name]), sample_program_bytes(name)
            )
        os.rename(staging, final)
    except ScaffoldError:
        raise
    except (OSError, ValueError) as exc:
        # Classify rename errors by errno; every other `OSError` (including a
        # `PermissionError` from an unlistable `target_dir`) falls through to
        # `scaffold_failed`.
        if isinstance(exc, OSError):
            if exc.errno in (errno.ENOTDIR, errno.EEXIST):
                if final.exists() and not final.is_dir():
                    raise ScaffoldError("not_a_directory") from exc
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                if _nonempty_directory(final):
                    raise ScaffoldError("dir_not_empty") from exc
        raise ScaffoldError("scaffold_failed") from exc
    finally:
        # `os.rename` consumes `staging` on success; anything else — a
        # `ScaffoldError` classified above, or a `BaseException` (e.g.
        # `KeyboardInterrupt`) propagating straight through — leaves it
        # behind, so remove whatever is still there on every other exit.
        with contextlib.suppress(OSError):
            if staging.is_symlink() or staging.is_file():
                os.unlink(staging)
            elif staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
    resolved = final.resolve()
    return ScaffoldResult(resolved, resolved / "manifest.json", resolved / "seed")


app = typer.Typer(
    help="Run the packaged reference /evolution/v2 target.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Run the packaged reference /evolution/v2 target."""


def _print_json(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _refuse(command: str, code: str) -> typer.Exit:
    sys.stderr.write(f"evo-target: {command} refused: {code}\n")
    sys.stderr.flush()
    return typer.Exit(2)


@app.command("init")
def init(
    directory: Path = typer.Argument(..., metavar="DIR", help="Demonstration directory path"),
) -> None:
    """Create a demonstration directory: manifest.json (/15), a seed tree, a corpus and sample programs."""

    try:
        result = scaffold(directory)
    except ScaffoldError as exc:
        raise _refuse("init", exc.code) from exc
    except OSError as exc:
        # Belt and suspenders: `scaffold` classifies every `OSError` itself, so
        # this should be unreachable, but `init` must never let one escape as
        # a bare traceback if that classification is ever incomplete.
        raise _refuse("init", "scaffold_failed") from exc
    _print_json(
        {
            "status": "created",
            "dir": str(result.dir),
            "manifest": str(result.manifest),
            "seed_root": str(result.seed_root),
        }
    )


class _Serving:
    """The serving lifecycle: start, wait, one termination path (spec §3.2).

    Handlers install in `__init__`, before the socket ever binds, so a
    SIGINT/SIGTERM that lands anywhere from that point on — including while
    the bind is still in flight — only sets `stop` and never raises
    `KeyboardInterrupt` into the command. `attach` receives the already-bound
    server and starts the polling thread; if a signal arrived before
    `attach`, `stop` is already set and the normal `wait`/`finish` path drains
    it with no special case.
    """

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.started = threading.Event()
        self.stop = threading.Event()
        self.failure: BaseException | None = None
        self.thread: threading.Thread | None = None
        self._previous: dict[int, Any] = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous[sig] = signal.signal(sig, lambda signum, frame: self.stop.set())

    def _run(self) -> None:
        assert self.server is not None
        try:
            self.started.set()
            self.server.serve_forever(poll_interval=0.1)
        except BaseException as exc:  # noqa: BLE001 — reported by `finish`
            self.failure = exc
        finally:
            self.stop.set()

    def restore(self) -> None:
        """Restore the previous handlers; idempotent — a second call is a no-op."""
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous = {}

    def attach(self, server: ThreadingHTTPServer) -> None:
        self.server = server
        self.thread = threading.Thread(target=self._run, name="evo-target-serve", daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            self.restore()
            raise

    def ready(self) -> bool:
        """Whether the ready line should be printed.

        `_run` sets `started` immediately before calling `serve_forever`, so
        a loop that fails synchronously — or a signal that already landed
        before `attach` — sets `stop` a moment later, in the same thread,
        which can be after this method's caller has already woken up.
        Waiting on `started` alone races that: a waiter can wake the instant
        `started` is set, before the child thread has even reached
        `serve_forever`, let alone failed. So after `started` or `stop`
        fires, this joins the thread with a short timeout: a synchronous
        failure gets that window to also set `stop` before the decision is
        read; a loop that is genuinely running is unaffected, since the join
        simply times out while the thread stays alive inside
        `serve_forever`.
        """
        while not (self.started.is_set() or self.stop.is_set()):
            self.started.wait(0.05)
        if self.thread is not None:
            self.thread.join(timeout=0.05)
        return self.started.is_set() and not self.stop.is_set()

    def wait(self) -> None:
        self.stop.wait()

    def finish(self) -> BaseException | None:
        # `shutdown` waits for `serve_forever`'s own exit event, which a loop
        # that died before entering its `try` never sets; only stop a loop
        # that is still running.
        if self.thread is not None:
            if self.failure is None:
                assert self.server is not None
                self.server.shutdown()
            self.thread.join(timeout=10)
        if self.server is not None:
            self.server.server_close()
        self.restore()
        return self.failure


def _bind_address(base_url: str) -> tuple[str, int]:
    parts = urlsplit(base_url)
    if parts.hostname is None or parts.port is None:
        # The loader guarantees both; this only removes the `-O` hazard of an
        # `assert` that Python strips under `-O`/`-OO`.
        raise ValueError("manifest base_url lacks host or port")
    return parts.hostname, parts.port


@app.command("serve")
def serve(
    manifest_path: Path = typer.Argument(..., metavar="MANIFEST"),
    allowed_root: Path | None = typer.Option(
        None,
        "--allowed-root",
        metavar="DIR",
        help="Root under which the target loads trees; default: the manifest's directory.",
    ),
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Append this command's output to an owner-only file."),
    ] = None,
) -> None:
    """Start the reference target bound to MANIFEST until SIGINT or SIGTERM.

    `--log-file` refuses an unusable or unsafe path with `log_file_unavailable`
    or `log_file_unsafe` (exit 2).
    """
    if log_file is None:
        _serve(manifest_path, allowed_root)
        return
    try:
        fd = process_log.open_log_file(log_file)
    except process_log.LogFileRefused as exc:
        raise _refuse("serve", exc.code) from exc
    with process_log.capture("serve", prefix="evo-target", log_fd=fd):
        _serve(manifest_path, allowed_root)


def _serve(manifest_path: Path, allowed_root: Path | None) -> None:
    try:
        manifest = load_manifest_v2(manifest_path)
    except ManifestError as exc:
        if exc.code == "manifest_schema":
            raise _refuse("serve", f"{exc.code}; {exc.expected}") from exc
        raise _refuse("serve", exc.code) from exc
    # Before the target exists, so no path between constructing it and the
    # `try` whose `finally` closes it can raise and leave it unclosed.
    host, port = _bind_address(manifest.target.base_url)
    # `_Serving`'s constructor installs the signal handlers, and it runs before
    # the target is bound: `signal.signal` raises `ValueError` off the main
    # thread, and that failure must land where there is no target yet to leave
    # unclosed. A refused bind restores the handlers itself, since it exits
    # before the `finally` below can.
    serving = _Serving()
    try:
        target = bind_reference_target(manifest, allowed_root)
    except BindError as exc:
        serving.restore()
        raise _refuse("serve", exc.code) from exc
    try:
        try:
            server = _serve_target(target, host=host, port=port)
        except OSError as exc:
            sys.stderr.write(f"evo-target: cannot bind {host}:{port}: {exc.strerror or exc}\n")
            sys.stderr.flush()
            raise typer.Exit(1) from exc
        try:
            serving.attach(server)
        except RuntimeError as exc:  # thread could not start
            server.server_close()
            sys.stderr.write(f"evo-target: serving stopped: {type(exc).__name__}\n")
            sys.stderr.flush()
            raise typer.Exit(1) from exc
        if serving.ready():
            _print_json(
                {
                    "status": "listening",
                    "base_url": manifest.target.base_url,
                    "allowed_artifact_root": str(target.allowed_artifact_root),
                    "seed_root": str(Path(os.path.realpath(manifest.seed_root))),
                    "seed_mode": target.seed_mode,
                    "policy_digest": manifest.policy_digest,
                    "boot_id": target.boot_id,
                }
            )
        serving.wait()
        failure = serving.finish()
        if failure is not None:
            sys.stderr.write(f"evo-target: serving stopped: {type(failure).__name__}\n")
            sys.stderr.flush()
            raise typer.Exit(1)
    finally:
        # One boundary (ADR-0009): close the target exactly once, after the
        # server has stopped and the signal handlers are restored — including
        # a bind-failure path above that never attached a server at all,
        # since `target` is bound before this `try` and `close()` is
        # idempotent.
        serving.restore()
        target.close()


if __name__ == "__main__":  # pragma: no cover — `python -m` entry
    app()
