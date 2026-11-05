#!/usr/bin/env python3
"""Supervise the baselines train-count sweep with resumable per-cell jobs.

This launcher intentionally stays under ``experiments/baselines`` and uses
``afterbench_loop.py`` as the only cell runner. It creates train1..train5
subset manifests from the requested source subset, then runs framework x skill
cells with bounded process concurrency.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EVO_ROOT = HERE.parents[1]
DEFAULT_FRAMEWORKS = ("hermes", "memento", "memp", "evoskill", "evolution")


@dataclass(frozen=True)
class Job:
    schema: str
    framework: str
    skill: str
    subset: Path
    schema_root: Path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_csv(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def _generate_subsets(
    *,
    source_subset: Path,
    root: Path,
    schemas: list[int],
    n_test: int,
    anchors: list[str],
) -> dict[str, Any]:
    source = _read_json(source_subset)
    skills = source.get("skills") or {}
    missing = [a for a in anchors if a not in skills]
    if missing:
        raise SystemExit(f"anchors missing from subset: {', '.join(missing)}")

    manifest = {
        "source_subset": str(source_subset.resolve()),
        "created_at": _now(),
        "n_test": n_test,
        "schemas": {},
    }
    for n_train in schemas:
        schema = f"train{n_train}"
        out = {
            k: v for k, v in source.items() if k not in {"skills", "regen_command", "created_at"}
        }
        out["schema"] = f"{source.get('schema', 'after-subset')}-train{n_train}-test{n_test}"
        out["created_at"] = _now()
        out["source_subset"] = str(source_subset.resolve())
        out["slice"] = {"n_train": n_train, "n_test": n_test}
        out["skills"] = {}
        for anchor in anchors:
            block = skills[anchor]
            train = list(block.get("train") or [])
            test = list(block.get("test") or [])
            if len(train) < n_train:
                raise SystemExit(f"{anchor}: only {len(train)} train tasks, need {n_train}")
            if len(test) < n_test:
                raise SystemExit(f"{anchor}: only {len(test)} test tasks, need {n_test}")
            new_block = dict(block)
            new_block["train"] = train[:n_train]
            new_block["test"] = test[:n_test]
            new_block["slice"] = {"n_train": n_train, "n_test": n_test}
            out["skills"][anchor] = new_block
        schema_root = root / schema
        subset_path = schema_root / "subset.json"
        _write_json(subset_path, out)
        manifest["schemas"][schema] = {
            "n_train": n_train,
            "n_test": n_test,
            "subset": str(subset_path),
        }
    _write_json(root / "experiment_family.json", manifest)
    return manifest


def _cell_summary(job: Job) -> Path:
    return job.schema_root / "cells" / job.framework / job.skill / "cell_summary.json"


def _job_log(job: Job) -> Path:
    return job.schema_root / "full_logs" / f"{job.framework}__{job.skill}.log"


def _job_status_path(job: Job) -> Path:
    return job.schema_root / "job_status" / f"{job.framework}__{job.skill}.json"


def _status_record(job: Job, **extra: Any) -> dict[str, Any]:
    return {
        "schema": job.schema,
        "framework": job.framework,
        "skill": job.skill,
        "subset": str(job.subset),
        "schema_root": str(job.schema_root),
        **extra,
    }


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if text and not text.endswith("\n"):
            fh.write("\n")


def _launch_job(
    *,
    job: Job,
    py: str,
    rounds: int,
    n_trials: int,
    confirm_trials: int,
    timeout_s: int,
    attempt: int,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["AFTER_ARTIFACTS_ROOT"] = str(job.schema_root)
    env["EVO_PROJECT_ROOT"] = str(job.schema_root / "iso" / job.framework / job.skill)
    env["EVO_SKILLS_ROOT"] = str(Path(env["EVO_PROJECT_ROOT"]) / ".agents" / "skills")
    env["PYTHONUNBUFFERED"] = "1"
    Path(env["EVO_SKILLS_ROOT"]).mkdir(parents=True, exist_ok=True)

    log = _job_log(job)
    _append_log(
        log,
        (
            f"=== START schema={job.schema} framework={job.framework} "
            f"skill={job.skill} attempt={attempt} at={_now()} ===\n"
            f"schema_root={job.schema_root}\nsubset={job.subset}\n"
            f"EVO_PROJECT_ROOT={env['EVO_PROJECT_ROOT']}\n"
        ),
    )
    _write_json(
        _job_status_path(job),
        _status_record(job, status="running", attempt=attempt, started_at=_now(), pid=None),
    )
    cmd = [
        py,
        str(HERE / "afterbench_loop.py"),
        "--framework",
        job.framework,
        "--subset",
        str(job.subset),
        "--skill",
        job.skill,
        "--rounds",
        str(rounds),
        "--n-trials",
        str(n_trials),
        "--confirm-trials",
        str(confirm_trials),
        "--timeout-s",
        str(timeout_s),
        "--resume",
    ]
    fh = log.open("ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(EVO_ROOT),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._afterbench_log_fh = fh  # type: ignore[attr-defined]
    _write_json(
        _job_status_path(job),
        _status_record(
            job,
            status="running",
            attempt=attempt,
            started_at=_now(),
            pid=proc.pid,
            cmd=cmd,
        ),
    )
    return proc


def _close_proc(proc: subprocess.Popen[bytes]) -> None:
    fh = getattr(proc, "_afterbench_log_fh", None)
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass


def _collect_metrics(root: Path, out_name: str) -> None:
    cmd = [
        sys.executable,
        str(HERE / "collect_train_test_metrics.py"),
        "--root",
        str(root),
        "--out",
        str(root / out_name),
    ]
    log = root / "supervisor.log"
    with log.open("a", encoding="utf-8") as fh:
        subprocess.run(cmd, cwd=str(EVO_ROOT), stdout=fh, stderr=subprocess.STDOUT, check=False)


def _collate_schema(
    *,
    py: str,
    schema_root: Path,
    anchors: list[str],
    frameworks: list[str],
) -> None:
    cmd = [
        py,
        str(HERE / "collate_loop_gate.py"),
        "--artifacts-root",
        str(schema_root),
        "--anchors",
        ",".join(anchors),
        "--frameworks",
        ",".join(frameworks),
        "--no-noop-control",
        "--allow-missing",
        "--out",
        str(schema_root / "gate_report.json"),
    ]
    with (schema_root / "collate.log").open("a", encoding="utf-8") as fh:
        subprocess.run(cmd, cwd=str(EVO_ROOT), stdout=fh, stderr=subprocess.STDOUT, check=False)


def _terminate_running(running: dict[Job, subprocess.Popen[bytes]]) -> None:
    for proc in running.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.time() + 20
    for proc in running.values():
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _write_progress(
    *,
    root: Path,
    schema: str | None,
    total_jobs: int,
    completed: int,
    failed: list[dict[str, Any]],
    running: dict[Job, subprocess.Popen[bytes]],
    queued: int,
    started_at: float,
) -> None:
    payload = {
        "updated_at": _now(),
        "schema": schema,
        "total_jobs": total_jobs,
        "completed": completed,
        "failed": failed,
        "running": [
            {
                "schema": job.schema,
                "framework": job.framework,
                "skill": job.skill,
                "pid": proc.pid,
                "elapsed_s": round(time.time() - (getattr(proc, "_started_at", started_at)), 1),
            }
            for job, proc in sorted(
                running.items(), key=lambda item: (item[0].schema, item[0].framework, item[0].skill)
            )
        ],
        "queued": queued,
        "elapsed_s": round(time.time() - started_at, 1),
    }
    _write_json(root / "progress.json", payload)


def run_schema(
    *,
    schema: str,
    root: Path,
    anchors: list[str],
    frameworks: list[str],
    py: str,
    parallel: int,
    rounds: int,
    n_trials: int,
    confirm_trials: int,
    timeout_s: int,
    retries: int,
    started_at: float,
) -> list[dict[str, Any]]:
    schema_root = root / schema
    subset = schema_root / "subset.json"
    jobs = [Job(schema, fw, skill, subset, schema_root) for fw in frameworks for skill in anchors]
    (schema_root / "full_logs").mkdir(parents=True, exist_ok=True)
    _write_json(schema_root / "cell_jobs.json", [asdict(j) for j in jobs])

    attempts: dict[Job, int] = {job: 0 for job in jobs}
    pending = [job for job in jobs if not _cell_summary(job).is_file()]
    skipped = len(jobs) - len(pending)
    completed = skipped
    failed: list[dict[str, Any]] = []
    running: dict[Job, subprocess.Popen[bytes]] = {}
    last_progress = 0.0

    with (root / "supervisor.log").open("a", encoding="utf-8") as fh:
        fh.write(
            f"schema_start={schema} at={_now()} jobs={len(jobs)} "
            f"skipped={skipped} pending={len(pending)} parallel={parallel}\n"
        )

    interrupted = False

    def launch_more() -> None:
        while pending and len(running) < parallel:
            job = pending.pop(0)
            attempts[job] += 1
            proc = _launch_job(
                job=job,
                py=py,
                rounds=rounds,
                n_trials=n_trials,
                confirm_trials=confirm_trials,
                timeout_s=timeout_s,
                attempt=attempts[job],
            )
            proc._started_at = time.time()  # type: ignore[attr-defined]
            running[job] = proc

    try:
        launch_more()
        while running or pending:
            time.sleep(2)
            done: list[tuple[Job, subprocess.Popen[bytes], int]] = []
            for job, proc in list(running.items()):
                rc = proc.poll()
                if rc is not None:
                    done.append((job, proc, rc))
            for job, proc, rc in done:
                running.pop(job, None)
                _close_proc(proc)
                summary_exists = _cell_summary(job).is_file()
                log = _job_log(job)
                _append_log(
                    log,
                    (
                        f"=== DONE schema={job.schema} framework={job.framework} "
                        f"skill={job.skill} attempt={attempts[job]} rc={rc} "
                        f"summary_exists={int(summary_exists)} at={_now()} ==="
                    ),
                )
                if rc == 0 and summary_exists:
                    completed += 1
                    _write_json(
                        _job_status_path(job),
                        _status_record(
                            job,
                            status="complete",
                            attempt=attempts[job],
                            rc=rc,
                            finished_at=_now(),
                        ),
                    )
                elif attempts[job] <= retries:
                    _write_json(
                        _job_status_path(job),
                        _status_record(
                            job,
                            status="retry_queued",
                            attempt=attempts[job],
                            rc=rc,
                            summary_exists=summary_exists,
                            queued_at=_now(),
                        ),
                    )
                    pending.append(job)
                else:
                    completed += 1
                    fail = _status_record(
                        job,
                        status="failed",
                        attempt=attempts[job],
                        rc=rc,
                        summary_exists=summary_exists,
                        finished_at=_now(),
                        log=str(log),
                    )
                    failed.append(fail)
                    _write_json(_job_status_path(job), fail)
            launch_more()

            now = time.time()
            if now - last_progress >= 30:
                last_progress = now
                _write_progress(
                    root=root,
                    schema=schema,
                    total_jobs=len(jobs),
                    completed=completed,
                    failed=failed,
                    running=running,
                    queued=len(pending),
                    started_at=started_at,
                )
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        if interrupted:
            _terminate_running(running)
        _write_progress(
            root=root,
            schema=schema,
            total_jobs=len(jobs),
            completed=completed,
            failed=failed,
            running=running,
            queued=len(pending),
            started_at=started_at,
        )

    _collate_schema(py=py, schema_root=schema_root, anchors=anchors, frameworks=frameworks)
    _collect_metrics(root, "metrics_latest.json")
    with (root / "supervisor.log").open("a", encoding="utf-8") as fh:
        fh.write(
            f"schema_done={schema} at={_now()} complete={completed}/{len(jobs)} "
            f"failed={len(failed)}\n"
        )
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-subset", type=Path, default=HERE / "subset.json")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--schemas", default="1,2,3,4,5")
    ap.add_argument("--n-test", type=int, default=3)
    ap.add_argument("--anchors", default="")
    ap.add_argument("--frameworks", default=",".join(DEFAULT_FRAMEWORKS))
    ap.add_argument("--parallel", type=int, default=50)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--n-trials", type=int, default=5)
    ap.add_argument("--confirm-trials", type=int, default=5)
    ap.add_argument("--timeout-s", type=int, default=2400)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--python", default=os.environ.get("EVO_PYTHON", "python3"))
    args = ap.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_subset = args.source_subset.resolve()
    source = _read_json(source_subset)
    anchors = _parse_csv(args.anchors) or sorted((source.get("skills") or {}).keys())
    frameworks = _parse_csv(args.frameworks)
    schemas = [int(x) for x in _parse_csv(args.schemas)]
    started_at = time.time()

    _generate_subsets(
        source_subset=source_subset,
        root=root,
        schemas=schemas,
        n_test=args.n_test,
        anchors=anchors,
    )
    config = {
        "started_at": _now(),
        "root": str(root),
        "source_subset": str(source_subset),
        "schemas": [f"train{x}" for x in schemas],
        "n_test": args.n_test,
        "anchors": anchors,
        "frameworks": frameworks,
        "parallel": args.parallel,
        "rounds": args.rounds,
        "n_trials": args.n_trials,
        "confirm_trials": args.confirm_trials,
        "timeout_s": args.timeout_s,
        "retries": args.retries,
        "python": args.python,
        "models": {
            "solver": os.environ.get("AFTER_SOLVER_MODEL"),
            "evolver": os.environ.get("AFTER_EVOLVER_MODEL"),
            "solver_base": os.environ.get("AFTER_SOLVER_BASE_URL"),
            "evolver_base": os.environ.get("AFTER_EVOLVER_BASE_URL"),
            "embedding_base": "http://localhost:10225/v1",
        },
    }
    _write_json(root / "supervisor_config.json", config)
    with (root / "supervisor.log").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "start", **config}, sort_keys=True) + "\n")

    all_failed: list[dict[str, Any]] = []
    for n_train in schemas:
        schema_failed = run_schema(
            schema=f"train{n_train}",
            root=root,
            anchors=anchors,
            frameworks=frameworks,
            py=args.python,
            parallel=max(1, args.parallel),
            rounds=args.rounds,
            n_trials=args.n_trials,
            confirm_trials=args.confirm_trials,
            timeout_s=args.timeout_s,
            retries=max(0, args.retries),
            started_at=started_at,
        )
        all_failed.extend(schema_failed)
        _write_json(root / "failures_latest.json", all_failed)

    _collect_metrics(root, "metrics_final.json")
    _write_json(root / "failures_final.json", all_failed)
    with (root / "supervisor.log").open("a", encoding="utf-8") as fh:
        fh.write(
            f"run_done at={_now()} failed={len(all_failed)} "
            f"elapsed_s={round(time.time() - started_at, 1)}\n"
        )
    return 0 if not all_failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
