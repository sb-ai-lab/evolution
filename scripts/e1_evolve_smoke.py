"""E1 end-to-end smoke: the packaged reference target, a `/15` manifest whose
Candidate editor is the in-process `chat` kind, and `evo tree evolve` run as a
real process — twice, the second run resuming the finished campaign, then a
third refused for a changed identity — followed by the campaign audit.

This is `scripts/w1_evolve_smoke.py` with one substitution. W1 declared a
`subprocess` editor and proved the bound-invocation path; E1 declares

    {"kind": "chat", "model": ..., "base_url": ..., "api_key_env": ...}

and proves the binding `evolution.tree.bindings.build_editor` builds for it: a
real `openai.AsyncOpenAI` client, a real tool loop over the private projection,
real admission and publication against the operator's `writable_paths`, and a
real per-attempt transcript under `<run_dir>/editor-logs`. Only the provider is
scripted. `tests/fake_chat_server.py` is an OpenAI-compatible
`ThreadingHTTPServer` on loopback that answers `POST /v1/chat/completions` from
a fixed, looping script of native function calls; the four calls below are the
ones `tests/test_tree_evolve.py` drives `ChatEditor` with — `list_files`, a
`record_plan` naming `skills/SKILL.md`, a `write_file` putting `IMPROVED` in
that file's first line, and `finish_edit`.

The rest is the shipped scaffold: `evo-target init` writes the seven-file
scaffold (`evolution.tree.reference_sample`) beside the seed — the scorer, the
subprocess Reflector, the corpus, and an `editor/edit.py` this manifest never
names. Its manifest is `/15` (campaign family 14), the only schema `evo tree
evolve` admits from `2.0.0b2`; the smoke edits the port and the `editor`
section only; the scorer still rewards a trajectory whose answers
carry `IMPROVED`, and the reference target still answers every turn with the
first line of `skills/SKILL.md`. The seed Champion therefore scores 0.0,
round 1's Candidate scores 1.0 and is promoted, and round 2 finds no failing cohort at all and
closes `clean` without a reflection and without an Attempt.

The one check W1 has no counterpart for is the secret. `E1_SMOKE_KEY` carries a
value long enough to be redacted rather than passed through as a short string,
and check 7 reads every byte the run directory holds to prove the value reached
no file — not the journal, not the transcript, not a projection remnant.

Imports are the standard library and `evolution.tree`'s public surface, plus
`tests/fake_chat_server.py` loaded by file path: `scripts/` must not import
`tests` as a package, and the module is a fixture of this checkout rather
than of the wheel, so a tree without it is reported as such. Every check
prints one line; the last line is `ok` and the exit code is 0.

This run needs a `python3` on the operator's `PATH`, because the shipped
sample declares it as the interpreter of the scorer and the (unused) sample
editor program; the smoke passes its own environment through to every
subprocess it spawns.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from evolution.tree import inspect_campaign

TARGET_ARGV = [sys.executable, "-m", "evolution.tree.reference_target_cli"]
EVO_ARGV = [sys.executable, "-m", "evolution.cli.main"]

#: How long `evo-target serve` gets to print its ready line before it is killed.
READY_TIMEOUT_SECONDS = 20.0

#: The scaffolded seed's first line: the answer the seed Champion gives, and the
#: one the editor replaces with a line carrying `IMPROVED`.
SEED_LINE = "Reference skill: answer with this line."

#: The improved first line, written by the scripted `write_file` call.
IMPROVED_LINE = "IMPROVED: reference skill"

#: The environment variable the manifest names, and the value it holds. The
#: value is longer than the redaction floor, so a leak would be a verbatim leak.
KEY_ENV = "E1_SMOKE_KEY"
KEY_VALUE = "smoke-secret-value-1234"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def say(message: str) -> None:
    print(message, flush=True)


def free_port() -> int:
    """A port nothing holds: the manifest must name it before `serve` binds it."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def load_fake_chat_server() -> ModuleType:
    """`tests/fake_chat_server.py`, by file path rather than as `tests.*`.

    `scripts/` is not allowed to import the test package, and the fixture is
    part of this checkout rather than of the installed wheel, so a tree without
    it gets a named failure instead of an `ImportError` from `tests`.
    """

    path = Path(__file__).resolve().parents[1] / "tests" / "fake_chat_server.py"
    check(path.is_file(), f"this smoke needs the checkout's {path.name}; it is not at {path}")
    spec = importlib.util.spec_from_file_location("e1_smoke_fake_chat_server", path)
    check(spec is not None and spec.loader is not None, f"cannot load {path} by path")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def chat_script(fake: ModuleType) -> list[dict[str, Any]]:
    """The four scripted turns that drive one `ChatEditor` attempt to `edited`.

    `tests/test_tree_evolve.py`'s `CHAT_SCRIPT`, with the written content
    changed to the reference seed's improved first line: `record_plan` must
    cover the final diff, and the reference target answers with exactly this
    line, which is what the scorer rewards. The server loops the script, so
    every attempt of every round replays the same four turns.
    """

    return [
        fake.tool_call("list_files"),
        fake.tool_call(
            "record_plan",
            {
                "summary": "make the skill say what it does",
                "changes": [
                    {
                        "path": "skills/SKILL.md",
                        "action": "update",
                        "purpose": "state the behaviour",
                    }
                ],
            },
        ),
        fake.tool_call("write_file", {"path": "skills/SKILL.md", "content": f"{IMPROVED_LINE}\n"}),
        fake.tool_call("finish_edit", {"summary": "rewrote the skill"}),
    ]


def child_env() -> dict[str, str]:
    """This checkout's `src` on `PYTHONPATH` when running from a source tree.

    An installed wheel needs nothing: `evolution` is already importable. From a
    checkout the spawned interpreter is whichever `sys.executable` is, whose own
    editable install may point at a different tree, so the tree this script was
    loaded from wins. `E1_SMOKE_KEY` is in `os.environ` by the time this runs,
    so every `evo tree evolve` child inherits the key the manifest names.
    """

    source = Path(__file__).resolve().parents[1] / "src"
    if not (source / "evolution" / "tree").is_dir():
        return dict(os.environ)
    existing = os.environ.get("PYTHONPATH")
    joined = str(source) if not existing else f"{source}{os.pathsep}{existing}"
    return {**os.environ, "PYTHONPATH": joined}


def scaffold(root: Path, env: dict[str, str]) -> Path:
    """`evo-target init`, for real: manifest.json plus the seed tree."""

    demo = root / "demo"
    completed = subprocess.run(
        [*TARGET_ARGV, "init", str(demo)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    check(
        completed.returncode == 0,
        f"evo-target init exited {completed.returncode}: {completed.stderr}",
    )
    created = json.loads(completed.stdout)
    check(created["status"] == "created", f"evo-target init did not create: {created}")
    return Path(created["manifest"])


def set_port(manifest_path: Path, port: int) -> None:
    """The first of the two edits this smoke makes to the scaffolded `/15` sample: a free port.

    The campaign policy, the environment, the scorer and the corpus stay the
    shipped sample; the `editor` section is replaced below by
    `set_chat_editor`.
    """

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["target"]["base_url"] = f"http://127.0.0.1:{port}"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def set_chat_editor(manifest_path: Path, *, chat_base_url: str, model: str) -> None:
    """Replace the scaffold's subprocess editor with the chat editor under test.

    The second and last edit the smoke makes: with it, the checks prove that
    `kind: chat` binds and runs on the shipped scaffold, not the walkthrough's
    `subprocess` path (that is the W1 smoke's proof).
    """

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["editor"] = {
        "kind": "chat",
        "model": model,
        "base_url": chat_base_url,
        "api_key_env": KEY_ENV,
        "max_turns": 8,
        # `/15` chat roles declare their context compaction (PDR-0028): the
        # recommended values, 64000 being at most half of 800000.
        "context_compact_chars": 800000,
        "context_message_max_chars": 64000,
        # `retry` is required on `editor.kind: chat` (role-resilience,
        # PDR-0019); this smoke does not exercise retries, so the values are
        # arbitrary but must be present and valid.
        "retry": {
            "version": "role_call_retry/1",
            "max_attempts": 3,
            "total_budget_seconds": 90,
            "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 15, "jitter": "full"},
        },
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def start_target(manifest: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], dict]:
    """Start `evo-target serve` and read its one ready line.

    The read is blocking, so a `serve` that binds but never prints would hang
    here with the child already spawned and no `finally` yet to reap it — and
    under pytest the outer timeout would kill this process and orphan it. A
    watchdog kills the child instead: `readline` then returns the empty string
    and the ordinary "printed no ready line" path reports it.
    """

    process = subprocess.Popen(
        [*TARGET_ARGV, "serve", str(manifest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    watchdog = threading.Timer(READY_TIMEOUT_SECONDS, process.kill)
    watchdog.start()
    try:
        line = process.stdout.readline()
    finally:
        watchdog.cancel()
    if not line:
        process.kill()
        _, stderr = process.communicate(timeout=20)
        raise AssertionError(f"evo-target serve printed no ready line: {stderr}")
    return process, json.loads(line)


def stop_target(process: subprocess.Popen[str]) -> int:
    """SIGINT, then kill if it will not go; the return code is the caller's to check."""

    if process.poll() is not None:
        return int(process.returncode)
    process.send_signal(signal.SIGINT)
    try:
        process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=20)
    return int(process.returncode)


def tree_listing(root: Path) -> list[tuple[str, int]]:
    """Every file under `root` by relative path and size.

    The evidence that an invocation wrote nothing: a refusal must leave the run
    directory as it found it, and so must a resume of an already finished
    campaign. Sizes rather than digests — every writer here is append-only, so a
    byte that changed changed a length.
    """

    return sorted(
        (str(path.relative_to(root)), path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file()
    )


def secret_bearing_files(root: Path, secret: str) -> list[str]:
    """Every file under `root` whose bytes contain `secret`, by relative path."""

    needle = secret.encode()
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes():
            found.append(str(path.relative_to(root)))
    return found


def evolve(manifest: Path, run_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*EVO_ARGV, "tree", "evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def run(root: Path) -> None:
    # The key the manifest names, in this process's environment and therefore in
    # every `evo tree evolve` child's: the binding reads it once, by name.
    os.environ[KEY_ENV] = KEY_VALUE
    env = child_env()

    # 1. The scaffolded demonstration tree: the shipped seven-file scaffold, its
    # `/15` manifest is the document this smoke runs.
    manifest = scaffold(root, env)
    demo = manifest.parent
    seed_skill = demo / "seed" / "skills" / "SKILL.md"
    check(
        seed_skill.read_text(encoding="utf-8").splitlines()[0] == SEED_LINE,
        "the scaffolded seed does not carry the reference skill line",
    )
    check((demo / "seed" / "uv.lock").is_file(), "the scaffolded seed has no uv.lock")
    seven_files = {
        "manifest.json",
        "seed/skills/SKILL.md",
        "seed/uv.lock",
        "scorer/score.py",
        "editor/edit.py",
        "reflector/reflect.py",
        "corpus.jsonl",
    }
    found = {path.relative_to(demo).as_posix() for path in demo.rglob("*") if path.is_file()}
    check(
        found == seven_files,
        f"the scaffold is not exactly the seven files: missing {sorted(seven_files - found)}, "
        f"extra {sorted(found - seven_files)}",
    )
    say(
        f"1. evo-target init scaffolded {demo.name}/ with the reference seed, uv.lock, "
        "and the seven-file scaffold"
    )

    # 2. The scripted provider. It binds its own loopback port, so it starts
    # before the manifest that must name it can be written.
    fake = load_fake_chat_server()
    chat_base_url, chat_server = fake.start(chat_script(fake))
    try:
        port = free_port()
        set_port(manifest, port)
        # The scaffold's rounds (2) is left as-is; the scaffold's `editor/edit.py`
        # is never named by the manifest once `set_chat_editor` replaces the
        # `editor` section below, and stays on disk unused, unnamed, unsealed.
        set_chat_editor(manifest, chat_base_url=chat_base_url, model=fake.MODEL)
        say(
            "2. the scaffolded /15 manifest: port rewritten, editor replaced by the chat "
            "editor; sample scorer and corpus kept"
        )
        say(
            f"3. the scripted OpenAI-compatible endpoint is listening on {chat_base_url} "
            f"(model {fake.MODEL}, {len(chat_script(fake))} turns, looping)"
        )

        # 3. The target, on the port the manifest declares.
        process, ready = start_target(manifest, env)
        try:
            check(ready["status"] == "listening", f"the target is not listening: {ready}")
            check(
                ready["base_url"] == f"http://127.0.0.1:{port}",
                f"the target bound another address: {ready['base_url']}",
            )
            say(
                f"4. evo-target serve is listening on {ready['base_url']} "
                f"(seed_mode {ready['seed_mode']})"
            )

            # 4. The campaign.
            run_dir = demo / "runs" / "r1"
            run_dir.parent.mkdir(parents=True)
            first = evolve(manifest, run_dir, env)
            check(
                first.returncode == 0,
                f"evo tree evolve exited {first.returncode}\nstdout: {first.stdout}\n"
                f"stderr: {first.stderr}",
            )
            document = json.loads(first.stdout)
            check(
                document["terminal"] == "finished",
                f"the campaign terminal is {document['terminal']!r}",
            )
            check(
                document["finished"]["outcome"] == "completed",
                f"the campaign outcome is {document['finished']}",
            )
            check(
                chat_server.script.served >= len(chat_script(fake)),
                f"the chat editor made {chat_server.script.served} calls to the endpoint",
            )
            say(
                "5. evo tree evolve: exit 0, terminal finished, outcome "
                f"{document['finished']['outcome']}, "
                f"{chat_server.script.served} scripted completions served"
            )

            result = document["result"]
            check(result is not None, "the completed campaign carries no result projection")
            champion = result["final_champion"]
            check(champion is not None, "the completed campaign named no final Champion")
            check(
                champion["baseline_origin"] == "promoted_attempt",
                f"the final Champion is not a promoted Attempt: {champion}",
            )
            check(
                champion["version"] == "v1",
                f"the final Champion is version {champion['version']!r}, not v1",
            )
            check(
                champion["measurement_complete"] is True,
                f"the final Champion's measurement is incomplete: {champion}",
            )
            rounds = {entry["round"]: entry for entry in result["rounds"]}
            check(1 in rounds, f"the campaign ran rounds {sorted(rounds)}")
            check(
                rounds[1]["outcome"] == "promoted",
                f"round 1 did not promote: {rounds[1]['outcome']!r}",
            )
            # The gate's own number, not just its verdict. The ranking's `score` is the
            # Candidate's own absolute score, so it cannot show the margin: the winner's
            # `global_comparison_completed` record carries the paired Candidate - Champion
            # difference (`point`). The Candidate beat the Champion 1.0 to 0.0 on every
            # sample, so anything under 1.0 would mean the fixtures stopped separating the
            # two trees.
            winner = result["selection"][0]["ranking"][0]
            check(winner["accepted"] is True, f"round 1's winner is not accepted: {winner}")
            paired = [
                record["payload"]
                for record in (
                    json.loads(line)
                    for line in (run_dir / "campaign.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip()
                )
                if record["event"] == "global_comparison_completed"
                and record["payload"]["attempt_id"] == winner["attempt_id"]
            ]
            check(
                len(paired) == 1 and paired[0]["point"] == 1.0,
                f"round 1's promotion rests on {paired}, not a paired difference of the full 1.0",
            )
            say(
                "6. the chat editor's Candidate was promoted: final Champion "
                f"{champion['version']} from a {champion['baseline_origin']}, measurement "
                "complete, round 1 promoted on a paired difference of 1.0"
            )

            # 5. The secret. The key is the run's, in both directions: the editor
            # redacts its own transcript with it and C1's journal refuses an
            # envelope that carries it. Nothing under the run root may hold it.
            leaked = secret_bearing_files(run_dir, KEY_VALUE)
            check(leaked == [], f"the api key value reached {leaked}")
            logs = sorted((run_dir / "editor-logs").glob("*.jsonl"))
            check(bool(logs), "the chat editor wrote no transcript under editor-logs/")
            modes = {stat.S_IMODE(path.stat().st_mode) for path in logs}
            check(
                modes == {0o600}, f"a transcript is not owner-only: {sorted(oct(m) for m in modes)}"
            )
            say(
                f"7. no file under the run directory carries the api key value; "
                f"{len(logs)} editor transcript(s) under editor-logs/, mode 0600"
            )

            # 6. Resume by fact (PDR-0005): the same command, the same run directory.
            #
            # The outcome alone proves nothing here. The campaign is seeded
            # (`random_seed: 7`) and the fixtures are deterministic, so a second
            # invocation that re-ran the whole campaign instead of adopting the
            # finished one would report the same terminal and the same Champion.
            # The run directory is what separates the two: a resume of a finished
            # campaign appends no record, opens no measurement and stages no
            # workspace, so every file under the run root keeps its size.
            before_resume = tree_listing(run_dir)
            # Guard against a vacuous comparison: two empty listings are equal too.
            check(len(before_resume) > 1, f"the finished campaign left {before_resume} behind")
            served_before_resume = chat_server.script.served
            second = evolve(manifest, run_dir, env)
            check(
                second.returncode == 0,
                f"the resume exited {second.returncode}\nstderr: {second.stderr}",
            )
            check(
                tree_listing(run_dir) == before_resume,
                "the second run wrote to the run directory: it re-ran the campaign, "
                "it did not resume the finished one",
            )
            check(
                chat_server.script.served == served_before_resume,
                "the resume called the chat endpoint again",
            )
            resumed = json.loads(second.stdout)
            check(
                (resumed["terminal"], resumed["finished"])
                == (document["terminal"], document["finished"]),
                f"the resume reported another terminal: {resumed['terminal']} "
                f"{resumed['finished']}",
            )
            check(
                resumed["result"]["final_champion"] == champion,
                "the resume named another final Champion",
            )
            say(
                "8. the second run resumed the finished campaign: exit 0, run directory "
                "byte-identical, no further completion served, the same Champion"
            )

            # 7. A manifest that changes the editor's declared model changes the
            # `EditorIdentity` digest, and so the campaign identity: refused,
            # before any write.
            variant = demo / "manifest-other-model.json"
            variant.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
            set_chat_editor(variant, chat_base_url=chat_base_url, model=f"{fake.MODEL}-v2")
            before = tree_listing(run_dir)
            third = evolve(variant, run_dir, env)
            check(third.returncode == 2, f"the identity-changed run exited {third.returncode}")
            check(third.stdout == "", f"a refusal printed a document: {third.stdout!r}")
            lines = third.stderr.splitlines()
            # The manifest prints its corpus-split `note:` lines at admission, before the refusal.
            notes = 0
            while notes < len(lines) and lines[notes].startswith("evo tree evolve: note: "):
                notes += 1
            check(
                lines[notes : notes + 1] == ["evo tree evolve: refused: run_identity_changed"],
                f"the refusal line is {third.stderr.strip()!r}",
            )
            # PDR-0010: the refusal names what differs and its class. This campaign
            # is finished, so the difference is hard whatever it is: there is no
            # `authorize` command to print.
            check(
                any(
                    line.startswith("evo tree evolve: differs: identity.editor.digest")
                    for line in lines
                ),
                f"the refusal did not name the field that differs: {third.stderr.strip()!r}",
            )
            check(
                "evo tree evolve: class: hard" in lines,
                f"the refusal did not print a hard class: {third.stderr.strip()!r}",
            )
            check(
                not any("evo tree authorize" in line for line in lines),
                "a hard refusal printed an authorize command",
            )
            check(
                tree_listing(run_dir) == before,
                "the refusal changed the run directory it claimed to leave untouched",
            )
            say(
                "9. a manifest naming another editor model is refused: exit 2, refused: "
                "run_identity_changed, run directory untouched"
            )

            # 8. The audit, over the public surface, with every E record re-verified.
            verification = inspect_campaign(run_dir, verify=True).to_payload()["verification"]
            check(
                verification is not None, "inspect_campaign(verify=True) reported no verification"
            )
            check(
                verification["mismatches"] == [],
                f"the audit found mismatches: {verification['mismatches']}",
            )
            check(
                verification["checked"] > 0 and verification["preconditions_checked"] > 0,
                f"the audit verified nothing: {verification}",
            )
            say(
                f"10. audit: {verification['checked']} references and "
                f"{verification['preconditions_checked']} preconditions re-verified, "
                "no mismatches"
            )
        finally:
            code = stop_target(process)
    finally:
        chat_server.shutdown()
        chat_server.server_close()
    check(code == 0, f"evo-target serve exited {code} on SIGINT")
    say("11. both servers stopped: the scripted endpoint closed, evo-target serve exit 0 on SIGINT")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="evo-e1-evolve-smoke-") as directory:
            run(Path(directory))
    except Exception as exc:  # noqa: BLE001 - report and fail loudly, this is a smoke script
        print(f"E1 EVOLVE SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    say("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
