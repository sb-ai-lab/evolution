"""V2-preview rehearsal: the wheel, from outside the checkout, end to end.

A stdlib-only check of wheel execution
outside the checkout, write containment of the whole operator path over a
finite boundary, and rollback to a control environment the preview never
touched — then the finished campaign reopened by a recreated environment of
the same version, and continued by a linked run that seeds from its champion
copy.

    python scripts/v2_preview_rehearsal.py ROOT [--wheel PATH] [--python EXE]

`ROOT` must lie outside this checkout and be absent or empty; everything the
rehearsal makes lives under it (`dist/`, `control/`, `home/`, `tmp/`, `cwd/`,
`preview/`, `demo/`, `report.json`). Without `--wheel` both release artifacts
are built from the checkout with `python -m build`; with it the given wheel
is copied under `ROOT/dist` and the sdist is recorded as not exercised.

Two rules set this apart from `scripts/w1_evolve_smoke.py`: it imports nothing
from `evolution`, and it runs `evo` and `evo-target` as the console scripts of
the environment it created, with no `PYTHONPATH` and no `-m`, so a checkout on
the path cannot mask a packaging defect. One line per check, `ok` last and
exit 0; `failed: <check>` and exit 1; a refused root exits 2 before writing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
VERSION = "2.0.0b18"
#: The caveat kinds, in order, of the scaffold's campaign (check 8) and of its
#: `baseline: inherit` successor (check 15). `tests/test_tree_family14_smoke.py`
#: runs the same campaigns in process and checks them against these constants.
CAMPAIGN_CAVEAT_KINDS: tuple[str, ...] = (
    "champion_not_contemporaneous",
    "focus_selection_bias",
    "intervals_empirical",
    "exact_contrast",
)
INHERIT_CAVEAT_KINDS: tuple[str, ...] = ("baseline_inherited", "champion_not_contemporaneous")
RELEASE_BASE_REGISTRY = {
    "init",
    "doctor",
    "quickstart",
    "task",
    "skill",
    "run",
    "trace",
    "evolve",
    "split",
    "tree",
}
TREE_REGISTRY = {"evolve", "conformance", "authorize", "evaluate", "trace", "watch"}
EXTRAS = {"llm", "gigachat", "tree"}

BUILD_TIMEOUT = 600.0
INSTALL_TIMEOUT = 600.0
STEP_TIMEOUT = 300.0
READY_TIMEOUT = 20.0
GRACE = 10.0
SEED_LINE = "Reference skill: answer with this line."


class Failure(Exception):
    """A check that did not hold; the message is the `failed:` line."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def say(message: str) -> None:
    print(message, flush=True)


# --- pure helpers (tested directly) -------------------------------------


def process_log_summary(run_dir: Path) -> dict:
    """The fresh campaign's diagnostic log, without importing the installed product."""
    directory = run_dir / "logs"
    paths = sorted(directory.glob("evolve-*.log"))
    summary = {
        "count": len(paths),
        "directory_mode": stat.S_IMODE(directory.lstat().st_mode) if directory.exists() else None,
        "file_mode": None,
        "exit_0": False,
        "finished_stdout": False,
    }
    if len(paths) == 1:
        path = paths[0]
        lines = path.read_text(encoding="utf-8").splitlines()
        summary["file_mode"] = stat.S_IMODE(path.lstat().st_mode)
        summary["exit_0"] = bool(lines and lines[-1].endswith(" log exit 0"))
        summary["finished_stdout"] = any(
            " stdout " in line and '"terminal": "finished"' in line for line in lines
        )
    return summary


def fingerprint(root: Path, *, exclude: tuple[str, ...] = ()) -> str:
    """SHA-256 over every entry under `root`: path, kind, mode and content.

    Directories, regular files, symbolic links and anything else each count
    as an entry, so an empty directory, a FIFO or a mode change changes the
    digest; timestamps and ownership are not read. `__pycache__` trees are
    ignored (an interpreter may write them on import), as is every top-level
    name in `exclude`. Symbolic links are not followed.
    """

    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        relative_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name != "__pycache__" and not (relative_dir == Path() and name in exclude)
        )
        names = sorted(
            name for name in filenames if not (relative_dir == Path() and name in exclude)
        )
        for name in (*dirnames, *names):
            path = Path(dirpath) / name
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                kind, payload = "link", os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                kind, payload = "dir", ""
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                kind, payload = "file", f"{info.st_size}:{digest.hexdigest()}"
            else:
                kind, payload = "other", ""
            lines.append(f"{(relative_dir / name).as_posix()}\0{kind}\0{mode:o}\0{payload}")
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def refuse_root(root: Path, checkout: Path) -> str | None:
    """`root_inside_checkout`, `root_not_a_directory`, `root_not_empty`, or None when usable."""

    resolved = root.resolve()
    checkout_resolved = checkout.resolve()
    if resolved == checkout_resolved or resolved.is_relative_to(checkout_resolved):
        return "root_inside_checkout"
    if resolved.exists() and not resolved.is_dir():
        return "root_not_a_directory"
    if resolved.exists() and any(resolved.iterdir()):
        return "root_not_empty"
    return None


def canonical_arguments(
    root: Path, wheel: Path | None, python: str
) -> tuple[Path, Path | None, str]:
    """Absolute ROOT, `--wheel` and `--python` before any child runs from `ROOT/cwd`.

    `rehearse` starts every child with `cwd=ROOT/cwd`, so a relative ROOT, a
    relative `--wheel`, or a `--python` naming a relative path would resolve
    against the wrong directory once inside a child if passed through as
    given. A bare command name for `--python` (no path separator) is left
    untouched so it still resolves against `PATH`, exactly as `subprocess`
    would resolve it.
    """

    canonical_root = root.resolve()
    canonical_wheel = wheel.resolve() if wheel is not None else None
    if os.sep in python or "/" in python:
        canonical_python = str(Path(python).resolve())
    else:
        canonical_python = python
    return canonical_root, canonical_wheel, canonical_python


def successor_manifest(document: dict, *, run_dir: Path, champion_path: str, baseline: str) -> dict:
    """The manifest a linked check writes beside the scaffolded one.

    `/6` reshaped `linked_from` from a bare string into `{run_dir, baseline}`
    (baseline inheritance spec §4.1), and a `/6` document refuses the string
    form. Checks 14 and 15 differ only in `baseline`, so both build their
    document here and `tests/test_v2_preview_rehearsal.py` runs the result
    through the real loader -- the one guard the two lagged checks needed.
    The caller's document is not modified.
    """

    successor = json.loads(json.dumps(document))
    successor["linked_from"] = {"run_dir": str(run_dir), "baseline": baseline}
    successor["seed_root"] = str(run_dir / champion_path)
    return successor


def retired_schema_manifest(document: dict) -> dict:
    """Relabel the native scaffold for check 10's early schema refusal only.

    The body need not match the retired schema: admission rejects the label
    before parsing workflow sections. The caller's document is not modified.
    """
    moved = json.loads(json.dumps(document))
    moved["schema"] = "evolution-v2-manifest/14"
    return moved


def parse_commands(help_text: str) -> set[str]:
    """The names under click's `Commands:` heading (two-space indent, one word)."""

    commands: set[str] = set()
    in_commands = False
    for line in help_text.splitlines():
        if line.strip() == "Commands:":
            in_commands = True
            continue
        if in_commands and line.startswith("  ") and not line.startswith("   "):
            commands.add(line.split()[0])
    return commands


def stop_group(process: subprocess.Popen[str], *, grace: float = GRACE) -> int:
    """SIGINT the child's whole process group, SIGKILL it after `grace`; wait for the leader.

    Every child the rehearsal starts is a session leader (`start_new_session`),
    so its pid is its process group: the scorer and editor an `evolve`
    spawns, or the backend a build spawns, die with it. A descendant that left
    the group on purpose is not reached.
    """

    if process.poll() is None:
        for sig, wait in ((signal.SIGINT, grace), (signal.SIGKILL, grace)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    return process.returncode if process.returncode is not None else -1


def boundary_env(root: Path) -> dict[str, str]:
    """The operator's environment with every implicit write location under `root`."""

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    home = root / "home"
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["TMPDIR"] = str(root / "tmp")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def set_port(manifest_path: Path, port: int) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["target"]["base_url"] = f"http://127.0.0.1:{port}"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# --- process steps ---------------------------------------------------------


def run_step(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    what: str,
) -> subprocess.CompletedProcess[str]:
    """One bounded child in its own session; on timeout its whole group is stopped."""

    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(process)
        raise Failure(f"{what} did not finish within {timeout:.0f}s") from None
    except BaseException:
        stop_group(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def read_ready_line(process: subprocess.Popen[str], timeout: float) -> str:
    """The child's first stdout line under a watchdog; stop its group on any failure.

    A blocking `readline` sits between `Popen` and any caller `try`/`finally`
    that could reach the child, so a `KeyboardInterrupt` landing there must
    not leave the child running in its own session: any exception escaping
    the wait stops the process group before propagating. An expired
    watchdog leaves `readline` returning an empty string rather than
    raising, so that case is turned into `Failure` here too, after the same
    group stop.
    """

    assert process.stdout is not None
    watchdog = threading.Timer(timeout, lambda: stop_group(process, grace=2.0))
    watchdog.start()
    try:
        try:
            line = process.stdout.readline()
        finally:
            watchdog.cancel()
    except BaseException:
        stop_group(process, grace=2.0)
        raise
    if not line:
        stop_group(process, grace=2.0)
        raise Failure("evo-target serve printed no ready line")
    return line


def start_target(
    evo_target: Path, manifest: Path, env: dict[str, str], cwd: Path
) -> tuple[subprocess.Popen[str], dict]:
    process = subprocess.Popen(
        [str(evo_target), "serve", str(manifest)],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        line = read_ready_line(process, READY_TIMEOUT)
        try:
            ready = json.loads(line)
        except json.JSONDecodeError:
            raise Failure(f"evo-target serve printed a non-JSON ready line: {line!r}") from None
        return process, ready
    except BaseException:
        stop_group(process, grace=2.0)
        raise


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path) as archive:
        return archive.getnames()


def inspect_run(python: Path, run_dir: Path, env: dict[str, str], cwd: Path) -> dict:
    code = (
        "import json, sys\nfrom pathlib import Path\n"
        "from evolution.tree import inspect_campaign\n"
        "print(json.dumps(inspect_campaign(Path(sys.argv[1]), verify=True).to_payload()))\n"
    )
    completed = run_step(
        [str(python), "-c", code, str(run_dir)],
        env=env,
        cwd=cwd,
        timeout=STEP_TIMEOUT,
        what="inspect_campaign",
    )
    check(completed.returncode == 0, f"inspect_campaign failed: {completed.stderr}")
    return json.loads(completed.stdout)


def make_venv(python: str, target: Path, env: dict[str, str], cwd: Path, what: str) -> None:
    completed = run_step(
        [python, "-m", "venv", str(target)], env=env, cwd=cwd, timeout=INSTALL_TIMEOUT, what=what
    )
    check(completed.returncode == 0, f"{what}: venv creation failed: {completed.stderr}")


def install_preview(preview: Path, wheel: Path, env: dict[str, str], cwd: Path) -> None:
    pip = [str(preview / "bin" / "python"), "-m", "pip", "install", "--quiet", f"{wheel}[tree]"]
    completed = run_step(pip, env=env, cwd=cwd, timeout=INSTALL_TIMEOUT, what="pip install")
    check(completed.returncode == 0, f"pip install failed: {completed.stderr}")
    completed = run_step(
        [str(preview / "bin" / "python"), "-m", "pip", "check"],
        env=env,
        cwd=cwd,
        timeout=STEP_TIMEOUT,
        what="pip check",
    )
    check(completed.returncode == 0, f"pip check failed: {completed.stdout}{completed.stderr}")


# --- the rehearsal --------------------------------------------------------


def fingerprint_root(root: Path) -> str:
    """ROOT minus `demo/runs/` and `report.json` (§5 check 7).

    A per-directory part for each of the six known subdirectories and `demo`
    (below), plus one more part naming ROOT's own top-level entries — name,
    kind and mode from `os.lstat`, excluding `report.json` — so a stray entry
    created directly under ROOT, or a mode/type change on one of the known
    subdirectories itself (the recursive parts only see their *contents*),
    is not invisible to the containment and rollback checks.
    """

    parts = [
        fingerprint(root / name)
        for name in ("control", "home", "tmp", "cwd", "preview", "dist")
        if (root / name).exists()
    ]
    demo = root / "demo"
    parts.append(fingerprint(demo, exclude=("runs",)))
    top_level: list[str] = []
    for entry in root.iterdir():
        if entry.name == "report.json":
            continue
        info = entry.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            kind = "link"
        elif stat.S_ISDIR(info.st_mode):
            kind = "dir"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        top_level.append(f"{entry.name}\0{kind}\0{mode:o}")
    top_level.sort()
    parts.append(hashlib.sha256("\n".join(top_level).encode("utf-8")).hexdigest())
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def rehearse(root: Path, wheel_arg: Path | None, python: str, report: dict) -> None:
    """The fifteen checks; `report` is filled as they pass so a failure still leaves it."""

    report["fingerprint_exclusions"] = ["__pycache__", "demo/runs", "report.json"]
    started = time.monotonic()

    def mark(name: str) -> None:
        report["timings"][name] = round(time.monotonic() - started, 3)

    root.mkdir(parents=True, exist_ok=True)
    dist = root / "dist"
    dist.mkdir()
    operator_env = dict(os.environ)
    operator_env.pop("PYTHONPATH", None)

    # 1. The artifacts.
    if wheel_arg is None:
        completed = run_step(
            [sys.executable, "-m", "build", "--outdir", str(dist), str(CHECKOUT)],
            env=operator_env,
            cwd=CHECKOUT,
            timeout=BUILD_TIMEOUT,
            what="python -m build",
        )
        check(completed.returncode == 0, f"python -m build failed: {completed.stderr[-2000:]}")
        sdist_exercised = True
    else:
        shutil.copy2(wheel_arg, dist / wheel_arg.name)
        sdist_exercised = False
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    check(
        len(wheels) == 1 and wheels[0].name.startswith(f"evo_skills-{VERSION}-"),
        f"wheels under dist: {[w.name for w in wheels]}",
    )
    check(
        (not sdist_exercised)
        or (len(sdists) == 1 and sdists[0].name == f"evo_skills-{VERSION}.tar.gz"),
        f"sdists under dist: {[s.name for s in sdists]}",
    )
    wheel = wheels[0]
    artifacts = [wheel, *sdists]
    completed = run_step(
        [
            sys.executable,
            str(CHECKOUT / "scripts" / "check_release_artifacts.py"),
            *map(str, artifacts),
        ],
        env=operator_env,
        cwd=CHECKOUT,
        timeout=STEP_TIMEOUT,
        what="check_release_artifacts",
    )
    check(completed.returncode == 0, f"check_release_artifacts refused: {completed.stderr}")
    for artifact in artifacts:
        members = archive_members(artifact)
        gui = [
            m
            for m in members
            if m == "evolution/gui" or m.endswith("/evolution/gui") or "/evolution/gui/" in f"/{m}"
        ]
        check(not gui, f"{artifact.name} carries {gui[:3]}")
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    extras = {
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("Provides-Extra:")
    }
    check(extras == EXTRAS, f"Provides-Extra is {sorted(extras)}")
    report["artifacts"] = {a.name: sha256_file(a) for a in artifacts}
    report["sdist_exercised"] = sdist_exercised
    say(
        f"1. artifacts: {wheel.name}"
        + (f" and {sdists[0].name}" if sdist_exercised else " (sdist not exercised: --wheel given)")
        + "; check_release_artifacts accepts, no evolution/gui member, extras "
        + ", ".join(sorted(extras))
    )
    mark("artifacts")

    # 2. The control environment and the boundary.
    control, home, tmp, cwd = root / "control", root / "home", root / "tmp", root / "cwd"
    for directory in (home, tmp, cwd):
        directory.mkdir()
    make_venv(python, control, operator_env, cwd, "control venv")
    env = boundary_env(root)
    report["boundary"] = {
        k: env[k] for k in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TMPDIR")
    }
    fp_control = fingerprint(control)
    fp_boundary_before = fingerprint(home), fingerprint(tmp), fingerprint(cwd)
    report["fingerprints"]["control"] = fp_control
    report["fingerprints"]["home"] = fp_boundary_before[0]
    report["fingerprints"]["tmp"] = fp_boundary_before[1]
    report["fingerprints"]["cwd"] = fp_boundary_before[2]
    say(
        "2. control environment created with nothing installed and fingerprinted; "
        "HOME, XDG_*, TMPDIR and cwd redirected under ROOT"
    )
    mark("control")

    # 3. The preview environment.
    preview = root / "preview"
    make_venv(python, preview, env, cwd, "preview venv")
    install_preview(preview, wheel, env, cwd)
    evo, evo_target, ppython = (
        preview / "bin" / "evo",
        preview / "bin" / "evo-target",
        preview / "bin" / "python",
    )
    completed = run_step(
        [str(evo), "--version"], env=env, cwd=cwd, timeout=STEP_TIMEOUT, what="evo --version"
    )
    check(
        completed.returncode == 0 and VERSION in completed.stdout,
        f"evo --version: {completed.stdout}{completed.stderr}",
    )
    completed = run_step(
        [str(ppython), "-c", "import evolution; print(evolution.__file__)"],
        env=env,
        cwd=cwd,
        timeout=STEP_TIMEOUT,
        what="import evolution",
    )
    location = Path(completed.stdout.strip())
    check(
        completed.returncode == 0 and location.is_relative_to(preview),
        f"evolution imported from {location}",
    )
    check(
        not location.is_relative_to(CHECKOUT), f"evolution imported from the checkout: {location}"
    )
    report["script_interpreter"] = sys.version
    report["venv_python"] = python
    say(
        "3. preview environment installed from the wheel with [tree]; "
        f"evo --version {VERSION}; evolution imported from ROOT/preview"
    )
    mark("preview")

    # 4. The registry.
    completed = run_step(
        [str(evo), "--help"], env=env, cwd=cwd, timeout=STEP_TIMEOUT, what="evo --help"
    )
    top = parse_commands(completed.stdout)
    check(top == RELEASE_BASE_REGISTRY, f"evo --help lists {sorted(top)}")
    completed = run_step(
        [str(evo), "tree", "--help"], env=env, cwd=cwd, timeout=STEP_TIMEOUT, what="evo tree --help"
    )
    tree = parse_commands(completed.stdout)
    check(tree == TREE_REGISTRY, f"evo tree --help lists {sorted(tree)}")
    for removed in (["gui"], ["tree", "edit"]):
        completed = run_step(
            [str(evo), *removed],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what=f"evo {' '.join(removed)}",
        )
        output = completed.stdout + completed.stderr
        check(
            completed.returncode != 0 and "No such command" in output,
            f"evo {' '.join(removed)}: exit {completed.returncode}: {output}",
        )
        check(
            not any(word in output for word in ("Streamlit", "dashboard", "paused", "attempt")),
            f"evo {' '.join(removed)} names the removed purpose: {output}",
        )
    say(
        "4. registry: evo --help lists the release-base registry without gui; "
        "evo tree lists evolve, conformance, authorize, evaluate, trace and watch; evo gui and "
        "evo tree edit get the generic unknown-command error"
    )
    mark("registry")

    # 5. The scaffold and the target.
    demo = root / "demo"
    completed = run_step(
        [str(evo_target), "init", str(demo)],
        env=env,
        cwd=cwd,
        timeout=STEP_TIMEOUT,
        what="evo-target init",
    )
    check(completed.returncode == 0, f"evo-target init failed: {completed.stderr}")
    manifest = demo / "manifest.json"
    seven = {
        "manifest.json",
        "seed/skills/SKILL.md",
        "seed/uv.lock",
        "scorer/score.py",
        "editor/edit.py",
        "reflector/reflect.py",
        "corpus.jsonl",
    }
    found = {p.relative_to(demo).as_posix() for p in demo.rglob("*") if p.is_file()}
    check(found == seven, f"scaffold is not the seven files: {sorted(found ^ seven)}")
    check(
        (demo / "seed" / "skills" / "SKILL.md").read_text(encoding="utf-8").splitlines()[0]
        == SEED_LINE,
        "seed line differs",
    )
    port = free_port()
    set_port(manifest, port)
    (demo / "runs").mkdir()
    run_dir = demo / "runs" / "run-1"
    target, ready = start_target(evo_target, manifest, env, cwd)
    serve_exits: list[int] = []
    try:
        check(
            ready.get("status") == "listening"
            and ready.get("base_url") == f"http://127.0.0.1:{port}",
            f"ready line: {ready}",
        )
        say(
            f"5. evo-target init scaffolded demo/ (seven files); port rewritten; "
            f"evo-target serve listening on {ready['base_url']}"
        )
        mark("scaffold")

        # 6. Conformance.
        completed = run_step(
            [str(evo), "tree", "conformance", "--manifest", str(manifest)],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what="evo tree conformance",
        )
        check(
            completed.returncode == 0,
            f"conformance exited {completed.returncode}: {completed.stderr}",
        )
        conformance = json.loads(completed.stdout)
        check(conformance.get("leaked") == [], f"conformance leaked: {conformance.get('leaked')}")
        say("6. evo tree conformance: exit 0, leaked []")
        mark("conformance")

        # 7. The containment fingerprint.
        fp_before = fingerprint_root(root)
        report["fingerprints"]["boundary_before_campaign"] = fp_before
        say("7. containment fingerprint taken over ROOT minus demo/runs/ and report.json")

        # 8. The campaign.
        completed = run_step(
            [str(evo), "tree", "evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what="evo tree evolve",
        )
        check(
            completed.returncode == 0, f"evolve exited {completed.returncode}: {completed.stderr}"
        )
        document = json.loads(completed.stdout)
        check(
            document["terminal"] == "finished" and document["finished"]["outcome"] == "completed",
            f"campaign: {document.get('terminal')}/{document.get('finished')}",
        )
        champion = document["result"]["final_champion"]
        check(
            champion is not None
            and champion["baseline_origin"] == "promoted_attempt"
            and champion["version"] == "v1"
            and champion["measurement_complete"] is True,
            f"final champion: {champion}",
        )
        check(
            tuple(caveat["kind"] for caveat in document["result"]["caveats"])
            == CAMPAIGN_CAVEAT_KINDS,
            f"caveats: {document['result']['caveats']}",
        )
        projection = inspect_run(ppython, run_dir, env, cwd)
        check(
            projection["complete"] is True and projection["consistency"] == "durable",
            f"projection: {projection.get('complete')}/{projection.get('consistency')}",
        )
        verification = projection["verification"]
        check(
            verification is not None and verification["mismatches"] == [], f"audit: {verification}"
        )
        process_log = process_log_summary(run_dir)
        check(
            process_log
            == {
                "count": 1,
                "directory_mode": 0o700,
                "file_mode": 0o600,
                "exit_0": True,
                "finished_stdout": True,
            },
            f"process log: {process_log}",
        )
        report["process_log"] = process_log
        report["campaign"] = {
            "terminal": document["terminal"],
            "outcome": document["finished"]["outcome"],
            "champion_digest": projection["final_champion"]["digest"],
        }
        say(
            "8. evo tree evolve: exit 0, finished/completed; Champion v1 from a promoted_attempt; "
            f"inspect_campaign complete and durable, audit {verification['checked']} references, no mismatches; "
            "one private process log, stdout finished, exit 0"
        )
        mark("campaign")

        # 9. Containment.
        fp_after = fingerprint_root(root)
        check(
            fp_after == fp_before, "the campaign wrote outside demo/runs/run-1 within the boundary"
        )
        say(
            "9. containment: no entry created, removed, re-typed, re-permissioned or rewritten "
            "under ROOT outside demo/runs/run-1 (timestamps and ownership unobserved)"
        )
        mark("containment")

        # 10. A `/14` label is refused. The relabelled native scaffold sits beside
        # `manifest.json` (same relative paths, same port); `manifest_schema` comes
        # before the run directory is read, so nothing is created.
        refused_manifest = demo / "manifest-14.json"
        refused = retired_schema_manifest(json.loads(manifest.read_text(encoding="utf-8")))
        refused_manifest.write_text(json.dumps(refused, indent=2) + "\n", encoding="utf-8")
        refused_dir = demo / "runs" / "refused-14"
        completed = run_step(
            [
                str(evo),
                "tree",
                "evolve",
                "--manifest",
                str(refused_manifest),
                "--run-dir",
                str(refused_dir),
            ],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what="evo tree evolve (/14)",
        )
        check(
            completed.returncode == 2
            and completed.stdout == ""
            and "refused: manifest_schema" in completed.stderr
            and not refused_dir.exists(),
            f"/14 refusal: exit {completed.returncode}, stdout {completed.stdout!r}, "
            f"stderr {completed.stderr!r}, run dir exists {refused_dir.exists()}",
        )
        say("10. evo tree evolve refuses a /14 document: exit 2, manifest_schema, nothing written")
        mark("refused 14")
    finally:
        serve_exits.append(stop_group(target))
        report["serve_exits"] = serve_exits
    check(serve_exits[-1] == 0, f"evo-target serve exited {serve_exits[-1]} on SIGINT")
    say("11. evo-target serve stopped on SIGINT (exit 0)")

    # 12. Rollback.
    fp_run = fingerprint(run_dir)
    report["fingerprints"]["run_dir"] = fp_run
    shutil.rmtree(preview)
    check(fingerprint(control) == fp_control, "the control environment changed")
    check(
        (fingerprint(home), fingerprint(tmp), fingerprint(cwd)) == fp_boundary_before,
        "home, tmp or cwd changed since check 2",
    )
    check(fingerprint(run_dir) == fp_run, "the run directory changed across the preview's deletion")
    check(not (control / "bin" / "evo").exists(), "control/bin/evo exists")
    completed = run_step(
        [str(control / "bin" / "python"), "-c", "import evolution"],
        env=env,
        cwd=cwd,
        timeout=STEP_TIMEOUT,
        what="control import",
    )
    check(completed.returncode != 0, "the control environment imports evolution")
    say(
        "12. rollback: preview environment deleted; control, home/tmp/cwd and the run directory "
        "unchanged; control has no evo and cannot import evolution"
    )
    mark("rollback")

    # 13. Same-version reopening.
    make_venv(python, preview, env, cwd, "preview venv (recreated)")
    install_preview(preview, wheel, env, cwd)
    target, ready = start_target(evo_target, manifest, env, cwd)
    try:
        completed = run_step(
            [str(evo), "tree", "evolve", "--manifest", str(manifest), "--run-dir", str(run_dir)],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what="evo tree evolve (reopen)",
        )
        check(
            completed.returncode == 0, f"reopen exited {completed.returncode}: {completed.stderr}"
        )
        again = json.loads(completed.stdout)
        check(
            again["terminal"] == "finished" and again["finished"]["outcome"] == "completed",
            f"reopen: {again.get('terminal')}",
        )
        reopened = inspect_run(ppython, run_dir, env, cwd)
        check(
            reopened["final_champion"]["digest"] == report["campaign"]["champion_digest"],
            "the reopened campaign names another Champion",
        )
        check(fingerprint(run_dir) == fp_run, "the reopening changed the run directory")
    finally:
        serve_exits.append(stop_group(target))
        report["serve_exits"] = serve_exits
    check(serve_exits[-1] == 0, f"evo-target serve exited {serve_exits[-1]} on SIGINT (reopen)")
    say(
        "13. same-version reopening: preview recreated from the same wheel; evo tree evolve on the "
        "finished campaign exit 0, same Champion, run directory byte-identical; serve stopped (exit 0)"
    )
    mark("reopen")

    # 14. Linked continuation (continuable terminals §5.4, §10). The successor
    # manifest sits next to the scaffolded one, so its relative keys resolve to
    # the same scorer, editor, Reflector and corpus, and the target is served
    # from the scaffolded manifest: its default allowed root is `demo/`, which
    # already holds both run directories and so the predecessor's champion copy.
    champion_path = document["status"]["campaign_outcome"]["champion_path"]
    check(
        isinstance(champion_path, str) and champion_path.startswith("artifacts/champions/"),
        f"champion_path: {champion_path!r}",
    )
    linked_manifest = demo / "manifest-linked.json"
    successor = successor_manifest(
        json.loads(manifest.read_text(encoding="utf-8")),
        run_dir=run_dir,
        champion_path=champion_path,
        baseline="fresh",
    )
    linked_manifest.write_text(json.dumps(successor, indent=2) + "\n", encoding="utf-8")
    linked_dir = demo / "runs" / "run-2"
    target, ready = start_target(evo_target, manifest, env, cwd)
    try:
        completed = run_step(
            [
                str(evo),
                "tree",
                "evolve",
                "--manifest",
                str(linked_manifest),
                "--run-dir",
                str(linked_dir),
            ],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what="evo tree evolve (linked)",
        )
        check(
            completed.returncode == 0,
            f"linked run exited {completed.returncode}: {completed.stderr}",
        )
        linked = json.loads(completed.stdout)
        check(linked["terminal"] == "finished", f"linked run: {linked.get('terminal')}")
        linked_from = linked["result"]["linked_from"]
        check(
            linked_from is not None and linked_from["first_round"] == 3,
            f"linked_from: {linked_from}",
        )
        linked_projection = inspect_run(ppython, linked_dir, env, cwd)
        linked_verification = linked_projection["verification"]
        check(
            linked_verification is not None and linked_verification["mismatches"] == [],
            f"linked audit: {linked_verification}",
        )
        check(fingerprint(run_dir) == fp_run, "the linked run changed the predecessor's directory")
    finally:
        serve_exits.append(stop_group(target))
        report["serve_exits"] = serve_exits
    check(serve_exits[-1] == 0, f"evo-target serve exited {serve_exits[-1]} on SIGINT (linked)")
    report["linked"] = {
        "terminal": linked["terminal"],
        "outcome": linked["finished"]["outcome"],
        "first_round": linked_from["first_round"],
    }
    say(
        "14. linked continuation: a successor manifest with linked_from the finished run and "
        f"seed_root its {champion_path}; evo tree evolve exit 0, finished, first round 3, "
        f"audit {linked_verification['checked']} references, no mismatches; predecessor "
        "byte-identical; serve stopped (exit 0)"
    )
    mark("linked continuation")

    # 15. Baseline inheritance (D2b §4, §10). A second successor of the same
    # predecessor declares `baseline: inherit`. Its `seed_root` is the same
    # champion copy, and `link_differences` compares a successor's seed root
    # against the predecessor's *final champion*, not against the scaffolded
    # seed: seeding from the champion is therefore the case where no
    # comparable field differs at all. So no `condition_changed` arises, every
    # pair is admitted under every seed mode, and the predecessor's whole
    # champion measurement lands in the first baseline, which dispatches
    # nothing.
    inherit_manifest = demo / "manifest-inherit.json"
    inheritor = successor_manifest(
        json.loads(manifest.read_text(encoding="utf-8")),
        run_dir=run_dir,
        champion_path=champion_path,
        baseline="inherit",
    )
    inherit_manifest.write_text(json.dumps(inheritor, indent=2) + "\n", encoding="utf-8")
    inherit_dir = demo / "runs" / "run-3"
    target, ready = start_target(evo_target, manifest, env, cwd)
    try:
        completed = run_step(
            [
                str(evo),
                "tree",
                "evolve",
                "--manifest",
                str(inherit_manifest),
                "--run-dir",
                str(inherit_dir),
            ],
            env=env,
            cwd=cwd,
            timeout=STEP_TIMEOUT,
            what="evo tree evolve (inherit)",
        )
        check(
            completed.returncode == 0,
            f"inheriting run exited {completed.returncode}: {completed.stderr}",
        )
        inheriting = json.loads(completed.stdout)
        check(inheriting["terminal"] == "finished", f"inheriting run: {inheriting.get('terminal')}")
        carried = next(
            (
                caveat
                for caveat in inheriting["result"]["caveats"]
                if caveat["kind"] == "baseline_inherited"
            ),
            None,
        )
        check(carried is not None, f"caveats: {inheriting['result']['caveats']}")
        check(
            carried["imported"] == carried["scheduled"] and carried["imported"] > 0,
            f"baseline_inherited: {carried}",
        )
        check(carried["fields"] == [], f"condition fields: {carried['fields']}")
        check(carried["origin_runs"] == 1, f"origin_runs: {carried['origin_runs']}")
        check(
            tuple(caveat["kind"] for caveat in inheriting["result"]["caveats"])
            == INHERIT_CAVEAT_KINDS,
            f"caveats: {inheriting['result']['caveats']}",
        )
        inherit_projection = inspect_run(ppython, inherit_dir, env, cwd)
        inherit_verification = inherit_projection["verification"]
        check(
            inherit_verification is not None and inherit_verification["mismatches"] == [],
            f"inheriting audit: {inherit_verification}",
        )
        check(
            fingerprint(run_dir) == fp_run,
            "the inheriting run changed the predecessor's directory",
        )
    finally:
        serve_exits.append(stop_group(target))
        report["serve_exits"] = serve_exits
    check(serve_exits[-1] == 0, f"evo-target serve exited {serve_exits[-1]} on SIGINT (inherit)")
    report["inherited"] = {
        "terminal": inheriting["terminal"],
        "outcome": inheriting["finished"]["outcome"],
        "imported": carried["imported"],
        "scheduled": carried["scheduled"],
        "fields": carried["fields"],
    }
    say(
        "15. baseline inheritance: a second successor with baseline inherit; evo tree evolve "
        f"exit 0, finished, the caveats {', '.join(INHERIT_CAVEAT_KINDS)}; baseline_inherited "
        f"carries {carried['from_run']}'s "
        f"{carried['source_measurement_id']}, {carried['imported']} of {carried['scheduled']} "
        "pairs carried and none dispatched, no condition field changed, audit "
        f"{inherit_verification['checked']} references, no mismatches; predecessor "
        "byte-identical; serve stopped (exit 0)"
    )
    mark("baseline inheritance")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--wheel", type=Path, default=None)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    reason = refuse_root(args.root, CHECKOUT)
    if reason is not None:
        print(f"refused: {reason}", file=sys.stderr)
        return 2

    root, wheel, python = canonical_arguments(args.root, args.wheel, args.python)
    if wheel is not None and not wheel.is_file():
        print("refused: wheel_missing", file=sys.stderr)
        return 2

    report: dict = {"version": VERSION, "fingerprints": {}, "timings": {}, "outcome": "failed"}
    try:
        rehearse(root, wheel, python, report)
        report["outcome"] = "ok"
    except Failure as failure:
        report["failure"] = str(failure)
        print(f"failed: {failure}", flush=True)
        return 1
    except Exception as exc:
        report["failure"] = f"unexpected: {exc!r}"
        print(f"failed: unexpected: {exc!r}", flush=True)
        return 1
    finally:
        if root.is_dir():
            (root / "report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    say("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
