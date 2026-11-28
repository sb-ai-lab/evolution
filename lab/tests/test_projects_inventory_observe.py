"""dev38 W80 (spec 2026-10-02 §2.1): one look at an artifact path — establish a regular file, then read it."""
from __future__ import annotations

import hashlib
import os
import socket

import pytest

from evolution_lab.projects.inventory import Observation, observe_file


class Cache(dict):
    def __init__(self):
        super().__init__()
        self.stores = 0

    def lookup(self, path, key):
        return self.get((path, key))

    def store(self, path, key, sha256):
        self.stores += 1
        self[(path, key)] = sha256


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_missing_and_directories_are_missing(tmp_path):
    assert observe_file(tmp_path / "none", read=True) == Observation("missing")
    assert observe_file(tmp_path / "none" / "deeper", read=True) == Observation("missing")
    (tmp_path / "file").write_text("x")
    assert observe_file(tmp_path / "file" / "child", read=True) == Observation("missing")  # NotADirectoryError
    assert observe_file(tmp_path, read=False) == Observation("missing")


def test_a_read_gives_the_bytes_and_their_digest(tmp_path):
    target = tmp_path / "a.json"
    target.write_bytes(b'{"a": 1}')
    seen = observe_file(target, read=True)
    assert (seen.state, seen.digest, seen.data) == ("read", _sha(b'{"a": 1}'), b'{"a": 1}')
    assert observe_file(target, read=False) == Observation("read", _sha(b'{"a": 1}'))


def test_the_digest_equals_the_confirmation_digest(tmp_path):
    from evolution_lab.projects.attest import _file_digest
    target = tmp_path / "SPEC.md"
    target.write_text("spec\n")
    assert observe_file(target, read=False).digest == _file_digest(target)


def test_observe_never_opens_a_socket_or_fifo(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX)
    try:
        try:
            server.bind(str(path))
        except OSError:
            pytest.skip("socket path too long")
        assert observe_file(fifo, read=True) == Observation("not_regular")
        assert observe_file(path, read=True) == Observation("not_regular")
        assert observe_file(path, read=False) == Observation("not_regular")
    finally:
        server.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
def test_a_regular_file_that_cannot_be_read_is_unreadable(tmp_path):
    target = tmp_path / "secret.md"
    target.write_text("x")
    target.chmod(0)
    try:
        assert observe_file(target, read=True) == Observation("unreadable")
        assert observe_file(target, read=False, cache=Cache()) == Observation("unreadable")
    finally:
        target.chmod(0o600)


def test_exists_only_uses_the_cache_and_a_schema_read_always_reads(tmp_path, monkeypatch):
    target = tmp_path / "SPEC.md"
    target.write_text("spec\n")
    cache = Cache()
    first = observe_file(target, read=False, cache=cache)
    assert cache.stores == 1
    import evolution_lab.projects.inventory as inventory
    real = inventory.os.fdopen
    opened = []
    monkeypatch.setattr(inventory.os, "fdopen", lambda *a, **k: opened.append(1) or real(*a, **k))
    assert observe_file(target, read=False, cache=cache) == first and opened == []  # a cache hit reads nothing
    assert observe_file(target, read=True, cache=cache).data == b"spec\n" and opened == [1]


def test_exists_hashes_in_bounded_blocks_and_a_schema_read_keeps_the_bytes(tmp_path, monkeypatch):
    """codex r1: read=False must never hold the whole artifact (an excluded multi-GiB file would exhaust memory)."""
    data = os.urandom(3 << 20)
    target = tmp_path / "big.bin"
    target.write_bytes(data)
    import evolution_lab.projects.inventory as inventory
    real = inventory.os.fdopen
    sizes = []

    class Spy:
        def __init__(self, handle):
            self._handle = handle

        def read(self, *args):
            sizes.append(args[0] if args else -1)
            return self._handle.read(*args)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    monkeypatch.setattr(inventory.os, "fdopen", lambda *a, **k: Spy(real(*a, **k)))
    cache = Cache()
    seen = observe_file(target, read=False, cache=cache)
    assert seen == Observation("read", _sha(data), None)
    assert sizes and all(size > 0 and size <= 1 << 20 for size in sizes)
    assert cache.stores == 1
    sizes.clear()
    assert observe_file(target, read=True).data == data


def test_declared_environment_file_bytes_include_venv_and_symlink_targets(tmp_path):
    from evolution_lab.preparation import EnvironmentInput, ProbeSource, SourceRoot
    from evolution_lab.projects.package import inputs_inventory
    from evolution_lab.service.codecs import BundleRequest
    work = tmp_path / "work"
    work.mkdir()
    entry = work / "entry.json"
    entry.write_text("{}")
    runtime = work / ".venv" / "runtime.dat"
    runtime.parent.mkdir()
    runtime.write_bytes(b"one")
    wheel = tmp_path / "agent.whl"
    wheel.write_bytes(b"first")
    alias = tmp_path / "selected.whl"
    alias.symlink_to(wheel)
    request = BundleRequest(ProbeSource(entry, {}),
        (EnvironmentInput(tmp_path / "python", (alias,), (runtime,)),), {}, (), (SourceRoot("work", work),), ())
    original, entries, _ = inputs_inventory(request, working_copy=work)
    assert f"@{runtime}" in {e.path for e in entries}
    linked = next(e for e in entries if e.path == f"@{alias}")
    assert linked.type == "symlink" and linked.target == str(wheel)
    assert linked.sha256 == hashlib.sha256(b"first").hexdigest()
    runtime.write_bytes(b"two")
    after_runtime = inputs_inventory(request, working_copy=work)[0]
    assert after_runtime != original
    wheel.write_bytes(b"second")
    assert inputs_inventory(request, working_copy=work)[0] != after_runtime
