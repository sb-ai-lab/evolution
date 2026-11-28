import json
import os
from pathlib import Path

import pytest

from evolution_lab.panel import runtime as rt


_GOOD = {"schema": "lab-panel-runtime/1", "pid": 7, "port": 80, "url": "u", "started_at": "s", "home": "h"}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _record(home, pid=None):
    return rt.runtime_record(pid=pid or os.getpid(), port=4321, host="p0123456789abcdef.localhost", token="t" * 43,
                             started_at="2026-09-29T10:00:00.000001+00:00", home=home)


def test_record_is_closed_and_url_carries_the_token(tmp_path):
    home = _home(tmp_path)
    record = _record(home)
    assert set(record) == {"schema", "pid", "port", "url", "started_at", "home"}
    assert record["schema"] == "lab-panel-runtime/1"
    assert record["url"] == "http://p0123456789abcdef.localhost:4321/?token=" + "t" * 43
    assert record["home"] == str(home)


def test_write_is_atomic_private_and_readable(tmp_path):
    home = _home(tmp_path)
    rt.write_runtime(home, _record(home))
    path = home / "panel.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert rt.read_runtime(home) == _record(home)
    assert [p.name for p in home.iterdir()] == ["panel.json"]  # no temp file left


@pytest.mark.parametrize("content", ["", "{", json.dumps({"schema": "lab-panel-runtime/2"}),
                                     json.dumps({"schema": "lab-panel-runtime/1", "pid": 1}),
                                     *[json.dumps({**_GOOD, key: bad}) for key, bad in [
                                         ("pid", "1"), ("pid", True), ("pid", 0), ("pid", -5), ("pid", 1.5),
                                         ("port", 0), ("port", 65536), ("port", False), ("port", "80"),
                                         ("url", 1), ("started_at", None), ("home", ["x"])]]])
def test_unreadable_or_foreign_shape_reads_as_none(tmp_path, content):
    home = _home(tmp_path)
    (home / "panel.json").write_text(content)
    assert rt.read_runtime(home) is None


def test_lock_is_exclusive_across_open_descriptions(tmp_path):
    home = _home(tmp_path)
    fd = rt.acquire_lock(home)
    try:
        assert rt.lock_is_held(home) is True
        rt.write_runtime(home, _record(home))
        with pytest.raises(rt.PanelError) as error:
            rt.acquire_lock(home)
        assert error.value.code == "panel_already_running"
        assert error.value.fields["url"] == _record(home)["url"]
    finally:
        rt.release_lock(fd)
    assert rt.lock_is_held(home) is False


def test_remove_only_own_record(tmp_path):
    home = _home(tmp_path)
    rt.write_runtime(home, _record(home, pid=os.getpid()))
    rt.remove_runtime(home, os.getpid() + 1)
    assert (home / "panel.json").exists()
    rt.remove_runtime(home, os.getpid())
    assert not (home / "panel.json").exists()


def test_status_running_while_the_lock_is_held(tmp_path):
    home = _home(tmp_path)
    fd = rt.acquire_lock(home)
    try:
        rt.write_runtime(home, _record(home))
        status = rt.runtime_status(home)
        assert status == {"schema": "lab-panel-status/1", "state": "running", "panel": _record(home)}
    finally:
        rt.release_lock(fd)


def test_stale_runtime_file_is_reported_and_removed(tmp_path):
    home = _home(tmp_path)
    rt.write_runtime(home, _record(home))  # a panel killed with -9: file stays, lock is free
    assert rt.runtime_status(home) == {"schema": "lab-panel-status/1", "state": "not_running", "panel": None}
    assert not (home / "panel.json").exists()


def test_error_payload_is_the_closed_envelope():
    error = rt.PanelError("panel_already_running", url="http://127.0.0.1:1/?token=x")
    assert error.payload() == {"error": {"code": "panel_already_running", "stage": "panel",
                                         "message": rt.MESSAGES["panel_already_running"]},
                               "url": "http://127.0.0.1:1/?token=x"}


def test_status_never_removes_the_file_while_the_lock_is_held(tmp_path):
    home = _home(tmp_path)
    fd = rt.acquire_lock(home)
    try:
        rt.write_runtime(home, _record(home))
        for _ in range(3):
            rt.runtime_status(home)
        assert (home / "panel.json").exists()
    finally:
        rt.release_lock(fd)


def test_stale_file_is_removed_before_the_probe_lock_is_released(tmp_path, monkeypatch):
    home = _home(tmp_path)
    rt.write_runtime(home, _record(home))
    seen = []
    real = rt.release_lock
    monkeypatch.setattr(rt, "release_lock", lambda fd: (seen.append((home / "panel.json").exists()), real(fd)))
    assert rt.runtime_status(home)["state"] == "not_running"
    assert seen == [False]  # already unlinked at release time, i.e. removed under the lock


def test_lock_refuses_a_symlink(tmp_path):
    home = _home(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_text("")
    (home / rt.LOCK_NAME).symlink_to(target)
    with pytest.raises(rt.PanelError) as error:
        rt.acquire_lock(home)
    assert error.value.code == "home_invalid"


def test_launch_host_is_random_per_launch():
    from evolution_lab.panel.server import launch_host
    first, second = launch_host(), launch_host()
    assert first != second
    for host in (first, second):
        assert host.startswith("p") and host.endswith(".localhost") and len(host) == 1 + 16 + len(".localhost")


def test_panel_home_creates_only_the_home(tmp_path):
    home = tmp_path / "a" / "home"
    assert rt.prepare_panel_home(home) == home
    assert home.stat().st_mode & 0o777 == 0o700 and list(home.iterdir()) == []
    os.close(rt.open_log(home))
    assert sorted(p.name for p in home.iterdir()) == ["logs"]
    assert (home / "logs").stat().st_mode & 0o777 == 0o700
    assert (home / "logs" / "panel.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("relative", [True, False])
def test_panel_home_refuses_relative_dotted_or_symlinked_paths(tmp_path, relative):
    if relative:
        bad = [Path("home"), tmp_path / "x" / ".." / "home"]
    else:
        real = _home(tmp_path)
        (tmp_path / "link").symlink_to(real)
        bad = [tmp_path / "link"]
    for path in bad:
        with pytest.raises(rt.PanelError) as error:
            rt.prepare_panel_home(path)
        assert error.value.code == "home_invalid"


def test_logs_symlink_is_refused_and_its_target_untouched(tmp_path):
    home = _home(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    (home / "logs").symlink_to(target)
    with pytest.raises(rt.PanelError) as error:
        rt.open_log(home)
    assert error.value.code == "home_invalid"
    assert target.stat().st_mode & 0o777 == 0o755 and list(target.iterdir()) == []


def test_log_file_symlink_is_refused(tmp_path):
    home = _home(tmp_path)
    (home / "logs").mkdir(mode=0o700)
    target = tmp_path / "elsewhere.log"
    target.write_text("")
    (home / "logs" / "panel.log").symlink_to(target)
    with pytest.raises(rt.PanelError) as error:
        rt.open_log(home)
    assert error.value.code == "home_invalid" and target.read_text() == ""


def test_panel_json_symlink_reads_as_none(tmp_path):
    home = _home(tmp_path)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(_GOOD))
    (home / rt.RUNTIME_NAME).symlink_to(other)
    assert rt.read_runtime(home) is None


def test_existing_panel_home_is_read_only_and_refuses_links(tmp_path):
    assert rt.existing_panel_home(tmp_path / "absent") is None
    assert not (tmp_path / "absent").exists()
    home = _home(tmp_path)
    assert rt.existing_panel_home(home) == home
    link = tmp_path / "link"
    link.symlink_to(home)
    assert rt.existing_panel_home(link / 'sub') is None
    assert not (home / 'sub').exists()
    for bad in (link, Path("relative"), tmp_path / ".." / "x"):
        with pytest.raises(rt.PanelError) as error:
            rt.existing_panel_home(bad)
        assert error.value.code == "home_invalid"


def test_lock_symlink_is_home_invalid_not_a_held_lock(tmp_path):
    home = _home(tmp_path)
    (home / rt.LOCK_NAME).symlink_to(tmp_path / "elsewhere")
    for call in (rt.runtime_status, rt.lock_is_held):
        with pytest.raises(rt.PanelError) as error:
            call(home)
        assert error.value.code == "home_invalid"


def test_panel_home_under_symlinked_ancestor_is_accepted(tmp_path):
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    expected = real.resolve() / 'home'
    assert rt.existing_panel_home(link / 'home') is None
    assert not expected.exists()
    assert rt.prepare_panel_home(link / 'home') == expected
    assert rt.existing_panel_home(link / 'home') == expected
    with pytest.raises(rt.PanelError):
        rt.existing_panel_home(link)
