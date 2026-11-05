"""W1 end-to-end smoke: the manifest/15 sample with its packaged scorer,
editor and Reflector, and `evo tree evolve` run as a real process — twice, the
second run resuming the finished campaign, then a third refused for a changed
identity — followed by the campaign audit.

The three declared programs and the corpus are the shipped sample `evo-target
init` scaffolds beside the seed (`evolution.tree.reference_sample`). The
scaffold's manifest is `/15` (campaign family 14), the only schema `evo tree
evolve` admits from `2.0.0b2`; the smoke runs that document as written and
edits nothing but the port.

Imports are the standard library and `evolution.tree`'s public surface. Every
check prints one line; the last line is `ok` and the exit code is 0.

This run needs a `python3` on the operator's `PATH`, because the shipped
sample declares it as the interpreter of all three declared programs; the
smoke passes its own environment through to every subprocess it spawns.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from evolution.tree import inspect_campaign
from evolution.tree.reference_target_cli import SAMPLE_MANIFEST

TARGET_ARGV = [sys.executable, "-m", "evolution.tree.reference_target_cli"]
EVO_ARGV = [sys.executable, "-m", "evolution.cli.main"]

#: How long `evo-target serve` gets to print its ready line before it is killed.
READY_TIMEOUT_SECONDS = 20.0

#: The scaffolded seed's first line: the answer the seed Champion gives, and the
#: one the editor replaces with a line carrying `IMPROVED`.
SEED_LINE = "Reference skill: answer with this line."


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


def child_env() -> dict[str, str]:
    """This checkout's `src` on `PYTHONPATH` when running from a source tree.

    An installed wheel needs nothing: `evolution` is already importable. From a
    checkout the spawned interpreter is whichever `sys.executable` is, whose own
    editable install may point at a different tree, so the tree this script was
    loaded from wins.
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
    """The one edit the smoke makes to the scaffolded `/15` sample: a port nothing holds.

    Everything else — the six workflow sections, the corpus, the scorer, the
    editor and the Reflector — is the shipped sample, so the checks below are the proof of
    the operator's path (`docs/reference-target.md`), port aside.
    """

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["target"]["base_url"] = f"http://127.0.0.1:{port}"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def variant_with_rounds(manifest_path: Path, rounds: int) -> Path:
    """A second document that yields a different campaign identity (check 8)."""

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["campaign"]["rounds"] = rounds
    variant = manifest_path.with_name(f"manifest-rounds-{rounds}.json")
    variant.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return variant


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


def evolve(manifest: Path, run_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*EVO_ARGV, "tree", "evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def run(root: Path) -> None:
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

    port = free_port()
    set_port(manifest, port)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    check(
        payload["schema"] == SAMPLE_MANIFEST["schema"] == "evolution-v2-manifest/15",
        f"the scaffold is not the /15 document: {payload['schema']}",
    )
    check(
        payload["campaign"]["call_retries"] == SAMPLE_MANIFEST["campaign"]["call_retries"],
        f"the scaffold declares other call retries: {payload['campaign'].get('call_retries')}",
    )
    check(
        {
            key: payload[key]["kind"] if "kind" in payload[key] else None
            for key in ("environment", "editor", "reflector")
        }
        == {"environment": "static", "editor": "subprocess", "reflector": "subprocess"},
        "the scaffold does not declare the static environment and the two subprocess roles",
    )
    check(
        (payload["reflector"]["environment_docs"], payload["reflector"]["probe"]) == (None, None),
        f"the scaffold declares environment docs or a probe: {payload['reflector']}",
    )
    say(
        "2. the scaffolded manifest is the /15 sample with the scorer, editor, "
        "Reflector and corpus; port rewritten to a free one"
    )

    # 2. The target, on the port the manifest declares.
    process, ready = start_target(manifest, env)
    try:
        check(ready["status"] == "listening", f"the target is not listening: {ready}")
        check(
            ready["base_url"] == f"http://127.0.0.1:{port}",
            f"the target bound another address: {ready['base_url']}",
        )
        say(
            f"3. evo-target serve is listening on {ready['base_url']} (seed_mode {ready['seed_mode']})"
        )

        # 3. The campaign.
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
        say(
            "4. evo tree evolve: exit 0, terminal finished, outcome "
            f"{document['finished']['outcome']}"
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
        say(
            f"5. the Candidate was promoted: final Champion {champion['version']} from a "
            f"{champion['baseline_origin']}, measurement complete"
        )

        rounds = {entry["round"]: entry for entry in result["rounds"]}
        check(set(rounds) == {1, 2}, f"the campaign ran rounds {sorted(rounds)}, not [1, 2]")
        check(
            rounds[1]["outcome"] == "promoted",
            f"round 1 did not promote: {rounds[1]['outcome']!r}",
        )
        # Round 2 is the clean round (spec §4.4): the promoted Champion answers
        # with the improved line, the sample scorer's `improved` verdict passes
        # on every repeat, the deterministic grouping finds no failing cohort,
        # and the round closes without a reflection and without an Attempt.
        check(
            rounds[2]["outcome"] == "clean",
            f"round 2 did not close clean: {rounds[2]['outcome']!r}",
        )
        check(
            rounds[2]["attempts"] == [],
            f"the clean round opened an Attempt: {rounds[2]['attempts']}",
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
                for line in (run_dir / "campaign.jsonl").read_text(encoding="utf-8").splitlines()
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
            "6. round 1 promoted on a paired difference of 1.0; round 2 found no failing cohort "
            "and closed clean, without a reflection and without an Attempt"
        )

        # 3b. The Reflector's own round prelude, from the journal: the only
        # place it is observable. `reflector-logs/` holds the sample program's
        # stderr and says nothing about what it returned, and the brief the
        # editor was handed is written nowhere outside the run.
        records = [
            json.loads(line)
            for line in (run_dir / "campaign.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        events = [record["event"] for record in records]
        finished = {
            record["payload"]["round"]: record["payload"]
            for record in records
            if record["event"] == "reflection_finished"
        }
        check(
            sorted(finished) == [1],
            f"reflection_finished was journaled for rounds {sorted(finished)}, not [1]",
        )
        check(
            finished[1]["status"] == "reflected",
            f"round 1's reflection did not report `reflected`: {finished[1]}",
        )
        check(
            [group["target_paths"] for group in finished[1]["groups"]]
            == [["skills/SKILL.md"], ["skills/SKILL.md"]],
            f"round 1's diagnosis is not the sample's two groups: {finished[1]['groups']}",
        )
        check(
            events.index("reflection_started") < events.index("attempt_opened"),
            "the first Attempt opened before the first reflection started",
        )
        check(
            events.index("reflection_finished") < events.index("gate_scope_selected"),
            "the round's gate scope was selected before the reflection finished",
        )
        clean_focus = [
            record["payload"]
            for record in records
            if record["event"] == "focus_portion_selected" and record["payload"]["round"] == 2
        ]
        check(
            [(p["reason"], p["unavailable"]) for p in clean_focus] == [("none", 0)],
            f"the clean round's focus is not the mechanical `none`: {clean_focus}",
        )
        say(
            "7. the sample Reflector ran once, in round 1, before that round's Attempt and "
            "before its gate scope; round 2's focus_portion_selected(none, unavailable 0) closed the "
            "round with no reflection at all"
        )

        # 4. Resume by fact (PDR-0005): the same command, the same run directory.
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
        resumed = json.loads(second.stdout)
        check(
            (resumed["terminal"], resumed["finished"])
            == (document["terminal"], document["finished"]),
            f"the resume reported another terminal: {resumed['terminal']} {resumed['finished']}",
        )
        check(
            resumed["result"]["final_champion"] == champion,
            "the resume named another final Champion",
        )
        say(
            "8. the second run resumed the finished campaign: exit 0, run directory "
            "byte-identical, the same terminal and Champion"
        )

        # 5. A manifest that changes the identity is refused, and writes nothing.
        variant = variant_with_rounds(manifest, 3)
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
                line.startswith("evo tree evolve: differs: identity.policy_digest")
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
            "9. a manifest with three rounds is refused: exit 2, refused: "
            "run_identity_changed, run directory untouched"
        )

        # 6. The audit, over the public surface, with every E record re-verified.
        verification = inspect_campaign(run_dir, verify=True).to_payload()["verification"]
        check(verification is not None, "inspect_campaign(verify=True) reported no verification")
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
            f"{verification['preconditions_checked']} preconditions re-verified, no mismatches"
        )
    finally:
        code = stop_target(process)
    check(code == 0, f"evo-target serve exited {code} on SIGINT")
    say("11. evo-target serve stopped cleanly on SIGINT (exit 0)")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="evo-w1-evolve-smoke-") as directory:
            run(Path(directory))
    except Exception as exc:  # noqa: BLE001 - report and fail loudly, this is a smoke script
        print(f"W1 EVOLVE SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    say("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
