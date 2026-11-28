"""A headless Chromium for the panel smoke (spec 2026-09-30 §4): found once, used with --dump-dom.

Order: LAB_PANEL_CHROMIUM (a path or a command line), then a browser in PATH or the playwright cache
that actually starts, then the local playwright Docker image. A cached binary may be present and still
not start (missing system libraries), so every host candidate is probed with --version.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

IMAGE = os.environ.get("LAB_PANEL_BROWSER_IMAGE", "mcr.microsoft.com/playwright:v1.61.1-noble")
NAMES = ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell")
CACHE_GLOB = "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
IN_IMAGE = ('exec "$(ls /ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/'
            'chrome-headless-shell | head -1)" "$@"')
PROBE_S, DUMP_S = 20, 120
BOOT = 'class="boot"'  # the static placeholder in index.html, gone once app.js renders
NOISE = ("dbus", "vaapi", "bluez", "Floss")  # container and headless start-up lines, present on every run
LOG_LINES = 40


def _starts(command: list[str]) -> bool:
    try:
        return subprocess.run(command, capture_output=True, timeout=PROBE_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _host_candidates() -> list[str]:
    found = [path for name in NAMES if (path := shutil.which(name))]
    cache = Path.home() / ".cache" / "ms-playwright"
    found += [str(p) for p in sorted(cache.glob(CACHE_GLOB)) if os.access(p, os.X_OK)]
    return found


def find_browser() -> list[str] | None:
    explicit = os.environ.get("LAB_PANEL_CHROMIUM")
    if explicit:
        return shlex.split(explicit)
    for candidate in _host_candidates():
        if _starts([candidate, "--version"]):
            return [candidate]
    docker = shutil.which("docker")
    if docker and _starts([docker, "image", "inspect", IMAGE]):
        return [docker, "run", "--rm", "--network=host", IMAGE, "bash", "-c", IN_IMAGE, "--"]
    return None


def _in_docker(prefix: list[str]) -> bool:
    return Path(prefix[0]).name == "docker"


def dump_dom(prefix: list[str], url: str, budget_ms: int = 5000, workdir: Path | None = None) -> str:
    """The DOM after `budget_ms` of virtual time. The container's browser is throwaway; a host browser
    gets the new headless mode and its own profile directory (under `workdir`, else the system temp
    directory), removed afterwards.

    When the app never booted (the static placeholder is still there), the DOM ends with an HTML comment
    holding the wall time and the browser's log, console messages included, so the failing assertion
    shows why (#475)."""
    flags = ["--no-sandbox", "--disable-gpu", "--enable-logging=stderr", f"--virtual-time-budget={budget_ms}", "--dump-dom"]
    started = time.monotonic()
    if _in_docker(prefix):
        done = _dump_in_docker(prefix, [*flags, url])
    else:
        with tempfile.TemporaryDirectory(prefix="lab-panel-browser-", dir=workdir) as profile:
            command = [*prefix, "--headless=new", f"--user-data-dir={profile}", *flags, url]
            done = subprocess.run(command, capture_output=True, text=True, timeout=DUMP_S, check=True)
    return _with_log(done.stdout, done.stderr, time.monotonic() - started)


def _with_log(dom: str, stderr: str, seconds: float) -> str:
    if BOOT not in dom:
        return dom
    lines = [line for line in stderr.splitlines() if line.strip() and not any(word in line for word in NOISE)]
    log = "\n".join(lines[-LOG_LINES:]).replace("--", "- -")
    return f"{dom}\n<!-- browser log ({seconds:.1f} s wall time, the app did not boot):\n{log}\n-->\n"


def _dump_in_docker(prefix: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
    """A timeout kills only the `docker run` client; the container would keep running (and, on the host
    network, keep the panel's live stream open). So the container is named and removed on any failure."""
    command = list(prefix)
    name = None
    if len(command) > 1 and command[1] == "run":
        name = f"lab-panel-smoke-{uuid.uuid4().hex}"
        command[2:2] = ["--name", name]
    try:
        return subprocess.run([*command, *args], capture_output=True, text=True, timeout=DUMP_S, check=True)
    except BaseException:
        if name:
            try:
                subprocess.run([command[0], "rm", "-f", name], capture_output=True, timeout=PROBE_S)
            except (OSError, subprocess.SubprocessError):
                pass  # the original failure is the one to report
        raise


class RequestLog:
    """An ASGI wrapper for a panel test server: every HTTP request a dumped page made, as the method and
    path (the query is left out: it carries the token), the status (or the exception, or "no response"
    when the dump came first) and its start after the first request. With the browser log from
    `dump_dom`, it tells a page that never asked for its modules from one whose module request failed
    (#475)."""

    def __init__(self, app) -> None:
        self.app = app
        self.entries: list[dict] = []
        self._first: float | None = None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        now = time.monotonic()
        if self._first is None:
            self._first = now
        entry = {"line": f"{scope['method']} {scope['path']}", "status": None, "at": now - self._first}
        self.entries.append(entry)

        async def tracked(message) -> None:
            if message["type"] == "http.response.start":
                entry["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except BaseException as error:
            entry["status"] = type(error).__name__
            raise

    def text(self) -> str:
        return "\n".join(f"{e['line']} {'no response' if e['status'] is None else e['status']} +{e['at'] * 1000:.0f} ms"
                         for e in self.entries)


def with_requests(dom: str, log: RequestLog) -> str:
    """The DOM, ending with the server's request log when the app did not boot."""
    if BOOT not in dom:
        return dom
    return f"{dom}\n<!-- server requests (status, start after the first request):\n{log.text().replace('--', '- -')}\n-->\n"
