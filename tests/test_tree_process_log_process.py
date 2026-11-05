"""Real process exits and signal ownership; pytest's faulthandler is not involved."""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from tests.test_tree_family14_public_commands import _evaluation_manifest
from tests.test_tree_family14_public_commands import scaffold15 as scaffold15
from tests.test_tree_process_log_commands import copied
from tests.test_tree_surface_contract import (
    EVOLVE_ARGV,
    SLEEPING_EDITOR_SCRIPT,
    SUBPROCESS_ENV,
    _workspace15,
)
from tests.test_tree_surface_contract import (
    served_target as served_target,
)

pytestmark = pytest.mark.slow


def _ignore_hup():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


@pytest.fixture
def driver(tmp_path):
    children = []

    def start(body, *, operator=False, ignored=False, preamble=""):
        script = tmp_path / "driver.py"
        script.write_text(
            "import faulthandler, json, os, resource, signal, sys, time\n"
            "from pathlib import Path\n" + textwrap.dedent(preamble) + "\n"
            "from evolution.tree.process_log import capture\n"
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
            f"run = Path({str(tmp_path / 'run')!r})\n"
            "run.mkdir(mode=0o700)\n" + textwrap.dedent(body)
        )
        environ = dict(os.environ)
        environ.pop("PYTHONFAULTHANDLER", None)
        if operator:
            environ["PYTHONFAULTHANDLER"] = "1"
        child = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environ,
            preexec_fn=_ignore_hup if ignored else None,
        )
        children.append(child)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def _ready(child):
    assert select.select([child.stdout], [], [], 15)[0], "child did not become ready"
    assert child.stdout.readline() == "ready\n"


def _log(tmp_path):
    files = list((tmp_path / "run" / "logs").glob("*.log"))
    assert len(files) == 1
    return files[0].read_text()


def test_an_operator_enabled_faulthandler_stays_enabled_after_capture(driver, tmp_path):
    child = driver(
        """
        assert faulthandler.is_enabled()
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
        print(json.dumps([faulthandler.is_enabled(), signal.getsignal(signal.SIGHUP) == signal.SIG_DFL]))
        """,
        operator=True,
    )
    out, err = child.communicate(timeout=15)
    assert child.returncode == 0 and out.strip() == "[true, true]" and err == ""
    assert _log(tmp_path).endswith("log exit 0\n")


def test_an_operator_enabled_faulthandler_gets_no_sighup_registration(driver, tmp_path):
    child = driver(
        """
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
            os.kill(os.getpid(), signal.SIGHUP)
        """,
        operator=True,
    )
    _, err = child.communicate(timeout=15)
    assert child.returncode == -signal.SIGHUP and err == ""
    text = _log(tmp_path)
    assert 'File "' not in text and "log exit " not in text


def test_a_segfault_after_materialize_keeps_partial_text_and_dump(driver, tmp_path):
    child = driver(
        """
        assert not faulthandler.is_enabled()
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
            sys.stderr.write('partial')
            faulthandler._sigsegv()
        """
    )
    _, err = child.communicate(timeout=15)
    assert child.returncode == -signal.SIGSEGV
    assert err in ("", "partial")  # The original stderr may still buffer it at death.
    text = _log(tmp_path)
    assert "stderr+ partial\nFatal Python error: Segmentation fault" in text
    assert 'File "' in text and "driver.py" in text
    assert "log exit " not in text


def test_default_sighup_dumps_then_takes_the_default_action(driver, tmp_path):
    child = driver(
        """
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
            print('ready', flush=True)
            time.sleep(60)
        """
    )
    _ready(child)
    child.send_signal(signal.SIGHUP)
    _, err = child.communicate(timeout=15)
    assert child.returncode == -signal.SIGHUP and err == ""
    text = _log(tmp_path)
    assert 'File "' in text and "driver.py" in text
    assert "log exit " not in text


def test_an_ignored_sighup_is_left_ignored(driver, tmp_path):
    child = driver(
        """
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
            print('ready', flush=True)
            sys.stdin.readline()
        """,
        ignored=True,
    )
    _ready(child)
    child.send_signal(signal.SIGHUP)
    _, err = child.communicate("finish\n", timeout=15)
    assert child.returncode == 0 and err == ""
    text = _log(tmp_path)
    assert text.endswith("log exit 0\n") and 'File "' not in text


def test_a_python_sighup_handler_is_left_alone(driver, tmp_path):
    child = driver(
        """
        signal.signal(signal.SIGHUP, lambda *args: print('handled', flush=True))
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
            os.kill(os.getpid(), signal.SIGHUP)
        """
    )
    out, err = child.communicate(timeout=15)
    assert child.returncode == 0 and out == "handled\n" and err == ""
    assert _log(tmp_path).endswith("log exit 0\n")


def test_capture_undoes_only_its_own_handlers_before_closing_the_file(driver, tmp_path):
    child = driver(
        """
        assert not faulthandler.is_enabled()
        with capture('evolve', prefix='evo tree evolve') as log:
            log.materialize(run)
            assert faulthandler.is_enabled()
        assert not faulthandler.is_enabled()
        assert faulthandler.unregister(signal.SIGHUP) is False
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL
        os.kill(os.getpid(), signal.SIGHUP)
        """
    )
    _, err = child.communicate(timeout=15)
    assert child.returncode == -signal.SIGHUP and err == ""
    text = _log(tmp_path)
    assert text.endswith("log exit 0\n") and 'File "' not in text


def test_before_materialize_a_segfault_dump_goes_to_original_stderr(driver, tmp_path):
    child = driver(
        """
        with capture('evolve', prefix='evo tree evolve'):
            faulthandler._sigsegv()
        """
    )
    _, err = child.communicate(timeout=15)
    assert child.returncode == -signal.SIGSEGV
    assert "Fatal Python error: Segmentation fault" in err and "driver.py" in err
    assert not (tmp_path / "run" / "logs").exists()


@pytest.mark.parametrize("error", ["exit", "usage"])
def test_import_with_vendored_typer_does_not_require_upstream_click(driver, error):
    pytest.importorskip("typer._click")
    expression = (
        "typer.Exit(3)" if error == "exit" else "_click.exceptions.UsageError('bad argument')"
    )
    code = 3 if error == "exit" else 2
    child = driver(
        f"""
        import typer
        from typer import _click
        failure = {expression}
        try:
            with capture('evolve', prefix='evo tree evolve') as log:
                log.materialize(run)
                raise failure
        except type(failure) as exc:
            assert exc.exit_code == {code}
        assert next((run / 'logs').glob('*.log')).read_text().endswith('log exit {code}\\n')
        print('imported')
        """,
        preamble="""
        import importlib.abc
        class NoClick(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'click' or fullname.startswith('click.'):
                    raise ModuleNotFoundError('upstream Click is absent')
        sys.meta_path.insert(0, NoClick())
        """,
    )
    out, err = child.communicate(timeout=15)
    assert child.returncode == 0, err
    assert out == "imported\n" and err == ""


def _child_setup(ignored=False, close_stderr=False):
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGHUP, signal.SIG_IGN if ignored else signal.SIG_DFL)
    if close_stderr:
        os.close(2)


@pytest.fixture
def campaign_process(tmp_path, served_target):
    children = []

    def start(*, sleeping=True, ignored=False, close_stderr=False, eio=False):
        sleep_script = "import os\n_cli_parent = os.getppid()\n" + SLEEPING_EDITOR_SCRIPT.replace(
            "time.sleep(120)",
            "for _ in range(2400):\n"
            "    if os.getppid() != _cli_parent:\n"
            "        pathlib.Path('orphan-exited').write_text('1')\n"
            "        break\n"
            "    time.sleep(0.05)",
        )
        options = {"editor_script": sleep_script} if sleeping else {}
        manifest = _workspace15(tmp_path / "ws", port=served_target, **options)
        root = manifest.parent / "run"
        argv = EVOLVE_ARGV
        if eio:
            snippet = (
                "import io, os, sys\n"
                "class Gone(io.TextIOBase):\n"
                "    def write(self, text):\n"
                "        raise OSError(5, 'terminal EIO')\n"
                "sys.stderr = Gone()\n"
                "from evolution.cli.main import app\n"
                "app()\n"
            )
            argv = [sys.executable, "-c", snippet, "tree", "evolve"]
        env = dict(SUBPROCESS_ENV)
        env.pop("PYTHONFAULTHANDLER", None)
        child = subprocess.Popen(
            [*argv, "--manifest", str(manifest), "--run-dir", str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            preexec_fn=lambda: _child_setup(ignored, close_stderr),
        )
        children.append(child)
        if sleeping:
            marker = manifest.parent / "editor" / "started"
            deadline = time.monotonic() + 60
            while not marker.exists() and time.monotonic() < deadline:
                assert child.poll() is None, child.communicate()
                time.sleep(0.02)
            assert marker.exists(), "editor never became ready"
        return child, root

    yield start
    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=15)
        if child.returncode == -signal.SIGHUP:
            marker = tmp_path / "ws" / "editor" / "orphan-exited"
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), "the test editor did not leave after its parent died"


def _campaign_log(root):
    (path,) = (root / "logs").glob("evolve-*.log")
    return path


@pytest.mark.parametrize("side", ["stdout", "stderr"])
def test_a_terminal_pipe_closed_mid_run_preserves_the_command_exit(campaign_process, side):
    child, root = campaign_process()
    getattr(child, side).close()
    setattr(child, side, None)
    child.send_signal(signal.SIGINT)
    out, err = child.communicate(timeout=90)
    assert child.returncode == 3, (out, err)
    path = _campaign_log(root)
    text = path.read_text()
    assert text.count(f"terminal {side} unavailable: EPIPE") == 1
    assert text.endswith("log exit 3\n")
    assert json.loads(copied(path, "stdout"))["terminal"] == "finished"
    assert "continue:" in copied(path, "stderr")
    if side == "stdout":
        assert "continue:" in err and "Exception ignored" not in err
    else:
        assert json.loads(out)["finished"]["reason"] == "cancelled"


@pytest.mark.parametrize("failure", ["absent", "EIO"])
def test_a_terminal_missing_at_launch_does_not_end_a_short_run(campaign_process, failure):
    child, root = campaign_process(
        sleeping=False, close_stderr=failure == "absent", eio=failure == "EIO"
    )
    out, err = child.communicate(timeout=90)
    assert child.returncode == 0, (out, err)
    assert json.loads(out)["terminal"] == "finished"
    text = _campaign_log(root).read_text()
    assert text.count(f"terminal stderr unavailable: {failure}") == 1
    assert "continue:" in text and text.endswith("log exit 0\n")
    assert "Exception ignored" not in err


def test_sighup_ends_a_running_campaign_with_a_dump_and_no_terminal(campaign_process):
    child, root = campaign_process()
    child.send_signal(signal.SIGHUP)
    out, err = child.communicate(timeout=15)
    assert child.returncode == -signal.SIGHUP, (out, err)
    text = _campaign_log(root).read_text()
    assert 'File "' in text and "log exit " not in text
    rows = [json.loads(line) for line in (root / "campaign.jsonl").read_text().splitlines()]
    assert not any(row["event"] == "campaign_finished" for row in rows)


def test_nohup_style_launch_survives_sighup_then_stops_cleanly(campaign_process):
    child, root = campaign_process(ignored=True)
    child.send_signal(signal.SIGHUP)
    time.sleep(0.05)
    assert child.poll() is None
    child.send_signal(signal.SIGINT)
    out, err = child.communicate(timeout=90)
    assert child.returncode == 3, (out, err)
    text = _campaign_log(root).read_text()
    assert text.endswith("log exit 3\n") and 'File "' not in text


def _closed_stdout_command(argv):
    env = dict(SUBPROCESS_ENV)
    env.pop("PYTHONFAULTHANDLER", None)
    child = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    child.stdout.close()
    child.stdout = None
    try:
        out, err = child.communicate(timeout=90)
        return child.returncode, out, err
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)


def test_evaluate_keeps_the_report_when_stdout_is_closed(scaffold15):
    from evolution.tree.evolve import evolve

    campaign = scaffold15.parent / "campaign"
    result = asyncio.run(evolve(scaffold15, campaign))
    version = result.result.to_payload()["final_champion"]["version"]
    manifest = _evaluation_manifest(scaffold15, campaign, version)
    root = scaffold15.parent / "evaluation"
    code, _, err = _closed_stdout_command(
        [*EVOLVE_ARGV[:-1], "evaluate", "--manifest", str(manifest), "--run-dir", str(root)]
    )
    assert code == 0 and "Exception ignored" not in err
    (path,) = (root / "logs").glob("evaluate-*.log")
    assert json.loads(copied(path, "stdout"))["state"] == "completed"
    assert path.read_text().count("terminal stdout unavailable: EPIPE") == 1
    assert path.read_text().endswith("log exit 0\n")


@pytest.mark.parametrize("logged", [False, True])
def test_conformance_dead_stdout_keeps_its_opt_in_boundary(scaffold15, logged):
    path = scaffold15.parent / "conformance.log"
    extra = ["--log-file", str(path)] if logged else []
    code, _, err = _closed_stdout_command(
        [*EVOLVE_ARGV[:-1], "conformance", "--manifest", str(scaffold15), *extra]
    )
    assert code == (0 if logged else 1), err  # No-log base measured at aaf46a0d.
    if logged:
        assert json.loads(copied(path, "stdout"))["ok"] is True
        assert path.read_text().count("terminal stdout unavailable: EPIPE") == 1
        assert path.read_text().endswith("log exit 0\n")
    else:
        assert not path.exists()


@pytest.mark.parametrize("close_before_ready", [False, True])
def test_serve_dead_stdout_still_stops_cleanly(tmp_path, close_before_ready):
    from tests.test_tree_reference_target_cli import free_port, scaffold_on_port, start_serve, stop

    manifest = scaffold_on_port(tmp_path, free_port())
    path = tmp_path / "serve.log"
    child = start_serve(manifest, "--log-file", str(path))
    try:
        if not close_before_ready:
            _ready_line = select.select([child.stdout], [], [], 15)
            assert _ready_line[0], "serve did not become ready"
            assert json.loads(child.stdout.readline())["status"] == "listening"
        child.stdout.close()
        child.stdout = None
        if close_before_ready:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert child.poll() is None
                if path.exists() and '"status": "listening"' in path.read_text():
                    break
                time.sleep(0.02)
            else:
                pytest.fail("serve never logged its ready line")
        code, out, err = stop(child)
        assert code == 0 and err == "", (out, err)
        text = path.read_text()
        assert text.endswith("log exit 0\n")
        # Serve writes stdout only once. Closing after its flushed ready line
        # causes no further failing forward; closing before it exercises EPIPE.
        assert text.count("terminal stdout unavailable: EPIPE") == int(close_before_ready)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=15)
