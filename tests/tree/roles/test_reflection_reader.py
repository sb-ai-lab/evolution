"""Contracts for the campaign-owned reflection reader transport."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from evolution.tree.roles.reflection_read_client import call, stream
from evolution.tree.roles.reflection_reader import (
    MAX_SUMMARY_BYTES,
    ByteLruCache,
    FrozenReflectionView,
    ReaderArgumentError,
    ReaderIntegrityError,
)

pytestmark = pytest.mark.slow


def test_native_and_real_client_dispatch_the_same_allowlisted_operation() -> None:
    view = FrozenReflectionView({"list_records": lambda arguments: {"items": [arguments["id"]]}})
    with view.open_operation("round-17", {"list_records"}) as reader:
        assert reader.dispatch("list_records", {"id": "case-a"}) == {"items": ["case-a"]}
        descriptor = reader.client_descriptor()
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "evolution.tree.roles.reflection_read_client",
                "--url",
                descriptor.url,
                "--capability",
                descriptor.capability,
                "list_records",
                json.dumps({"id": "case-a"}),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    assert json.loads(child.stdout) == {"items": ["case-a"]}


def test_bad_auth_and_unknown_or_oversized_arguments_are_recoverable() -> None:
    view = FrozenReflectionView({"read": lambda arguments: arguments})
    with view.open_operation("round-1", {"read"}) as reader:
        descriptor = reader.client_descriptor()
        with pytest.raises(ReaderArgumentError, match="unknown_operation"):
            reader.dispatch("write", {})
        with pytest.raises(ReaderArgumentError, match="arguments_too_large"):
            reader.dispatch("read", {"text": "x" * 65_537})
        with pytest.raises(ReaderArgumentError, match="bad_capability"):
            reader.dispatch("read", {}, capability="wrong")
        with pytest.raises(ReaderArgumentError, match="bad_capability"):
            call(descriptor.url, "wrong", "read", {})
        with pytest.raises(ReaderArgumentError, match="forbidden_routing_argument"):
            call(descriptor.url, descriptor.capability, "read", {"url": "http://not-a-route"})


def test_ignored_integrity_handler_error_stays_sticky() -> None:
    def broken(_: object) -> object:
        raise ReaderIntegrityError("source_changed")

    view = FrozenReflectionView({"read": broken, "list": lambda _: {"ok": True}})
    with view.open_operation("round-1", {"read", "list"}) as reader:
        with pytest.raises(ReaderIntegrityError, match="source_changed"):
            reader.dispatch("read", {})
        with pytest.raises(ReaderIntegrityError, match="source_changed"):
            reader.dispatch("list", {})
        assert reader.integrity_fault == "source_changed"


def test_integrity_error_during_stream_consumption_is_sticky() -> None:
    def broken_stream(_: object):
        yield {"part": 1}
        raise ReaderIntegrityError("trace_digest_mismatch")

    view = FrozenReflectionView({"stream": broken_stream, "list": lambda _: {"ok": True}})
    with view.open_operation("round-1", {"stream", "list"}) as reader:
        frames = reader.stream("stream", {})
        assert next(frames)["kind"] == "header"
        assert next(frames)["value"] == {"part": 1}
        with pytest.raises(ReaderIntegrityError, match="trace_digest_mismatch"):
            next(frames)
        with pytest.raises(ReaderIntegrityError, match="trace_digest_mismatch"):
            reader.dispatch("list", {})


def test_stream_frames_and_observed_summary_are_bounded() -> None:
    view = FrozenReflectionView(
        {
            "stream": lambda _: (
                {"number": number, "payload": "x" * 1000} for number in range(300)
            ),
            "list": lambda _: {"ok": True},
        }
    )
    with view.open_operation("round-1", {"stream", "list"}) as reader:
        frames = list(reader.stream("stream", {}))
        for _ in range(300):
            reader.dispatch("list", {})
        summary = reader.summary
    assert frames[0] == {"kind": "header", "operation": "stream"}
    assert frames[-1] == {"kind": "final", "operation": "stream"}
    assert len([frame for frame in frames if frame["kind"] == "data"]) == 300
    assert all(len(json.dumps(frame).encode()) <= 128 * 1024 for frame in frames)
    assert len(summary.entries) <= 256
    assert summary.omitted_reads > 0
    assert summary.byte_size <= 64 * 1024


def test_summary_payload_counts_its_envelope_against_the_byte_limit() -> None:
    view = FrozenReflectionView({"read": lambda _: {"ok": True}})
    with view.open_operation("round-1", {"read"}) as reader:
        for index in range(300):
            reader.dispatch("read", {"record_id": f"record-{index:03d}"})
        summary = reader.summary
    payload = summary.to_payload()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert set(payload) == {"entries", "omitted_reads", "omitted_errors"}
    assert len(encoded) <= MAX_SUMMARY_BYTES
    assert summary.byte_size == len(encoded)
    assert payload["omitted_reads"] > 0


def test_admission_integrity_fault_is_recorded_once_and_sticky() -> None:
    reader = FrozenReflectionView({"read": lambda _: {"ok": True}}).open_operation(
        "round-1", {"read"}
    )
    with reader:
        with pytest.raises(ReaderIntegrityError, match="snapshot_changed"):
            reader.verify_admission(
                lambda: (_ for _ in ()).throw(ReaderIntegrityError("snapshot_changed"))
            )
        assert reader.integrity_fault == "snapshot_changed"
        assert reader.summary.entries[-1]["operation"] == "admission"
        assert reader.summary.entries[-1]["error"] == "snapshot_changed"
        entries = reader.summary.entries
        with pytest.raises(ReaderIntegrityError, match="snapshot_changed"):
            reader.verify_admission(lambda: None)
        assert reader.summary.entries == entries


def test_close_invalidates_transport_and_lru_evicts_by_bytes() -> None:
    view = FrozenReflectionView({"read": lambda _: {"ok": True}})
    reader = view.open_operation("round-1", {"read"})
    descriptor = reader.client_descriptor()
    reader.close()
    with pytest.raises(ReaderArgumentError, match="operation_closed"):
        reader.dispatch("read", {})

    cache = ByteLruCache(4)
    cache.put("a", b"12")
    cache.put("b", b"34")
    cache.put("c", b"56")
    assert cache.get("a") is None
    assert cache.get("b") == b"34"
    assert descriptor.url.startswith("http://127.0.0.1:")


def test_close_cancels_an_active_stream_generator() -> None:
    closed = threading.Event()

    def endless(_: object):
        try:
            yield {"chunk": 1}
            while True:
                yield {"chunk": 2}
        finally:
            closed.set()

    view = FrozenReflectionView({"stream": endless})
    reader = view.open_operation("round-1", {"stream"})
    frames = reader.stream("stream", {})
    next(frames)
    next(frames)
    reader.close()
    assert closed.wait(1)


def test_close_blocks_late_frame_and_closes_generator_after_a_blocking_read() -> None:
    release = threading.Event()
    cleaned = threading.Event()
    consumed: list[object] = []

    def blocked(_: object):
        try:
            yield {"chunk": 1}
            release.wait()
            yield {"chunk": 2}
        finally:
            cleaned.set()

    reader = FrozenReflectionView({"stream": blocked}).open_operation("round-1", {"stream"})
    frames = reader.stream("stream", {})
    next(frames)
    next(frames)

    def consume() -> None:
        try:
            consumed.append(next(frames))
        except ReaderArgumentError as exc:
            consumed.append(exc.code)

    worker = threading.Thread(target=consume)
    worker.start()
    reader.close()
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert consumed == ["operation_closed"]
    assert cleaned.is_set()


def test_close_blocks_final_frame_when_pending_read_reaches_end() -> None:
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    consumed: list[object] = []

    def blocked(_: object):
        try:
            yield {"chunk": 1}
            entered.set()
            release.wait()
        finally:
            cleaned.set()

    reader = FrozenReflectionView({"stream": blocked}).open_operation("round-1", {"stream"})
    frames = reader.stream("stream", {})
    next(frames)
    next(frames)

    def consume() -> None:
        try:
            consumed.append(next(frames))
        except ReaderArgumentError as exc:
            consumed.append(exc.code)

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert entered.wait(1)
        reader.close()
    finally:
        release.set()
        worker.join(1)
        frames.close()
    assert not worker.is_alive()
    assert consumed == ["operation_closed"]
    assert cleaned.is_set()


def test_summary_records_validation_and_response_failures_with_stream_digest() -> None:
    view = FrozenReflectionView(
        {
            "read": lambda _: {"body": "x" * (128 * 1024)},
            "stream": lambda _: iter(({"chunk": 1}, {"chunk": 2})),
        }
    )
    with view.open_operation("round-1", {"read", "stream"}) as reader:
        with pytest.raises(ReaderArgumentError, match="forbidden_routing_argument"):
            reader.dispatch("read", {"path": "/never-exposed"})
        with pytest.raises(ReaderArgumentError, match="response_too_large"):
            reader.dispatch("read", {})
        list(reader.stream("stream", {}))
        entries = reader.summary.entries
    assert entries[0]["error"] == "forbidden_routing_argument"
    assert entries[1]["error"] == "response_too_large"
    assert entries[2]["response_digest"] != "unavailable"


def test_client_rejects_paths_and_reports_safe_activity_fields() -> None:
    view = FrozenReflectionView({"read": lambda _: {"ok": True}})
    with view.open_operation("round-1", {"read"}) as reader:
        descriptor = reader.client_descriptor()
        assert call(
            descriptor.url, descriptor.capability, "read", {"record_id": "r-1", "offset": 2}
        ) == {"ok": True}
        with pytest.raises(ReaderArgumentError, match="forbidden_routing_argument"):
            reader.dispatch("read", {"path": "/not-exposed"})
        assert reader.summary.entries[0]["record_id"] == "r-1"
        assert reader.summary.entries[0]["offset"] == "2"


def test_thin_client_stream_matches_native_frames() -> None:
    view = FrozenReflectionView({"stream": lambda _: iter(({"number": 1}, {"number": 2}))})
    with view.open_operation("round-1", {"stream"}) as reader:
        descriptor = reader.client_descriptor()
        remote = list(stream(descriptor.url, descriptor.capability, "stream", {}))
        native = list(reader.stream("stream", {}))
    assert remote == native


def test_thin_client_refuses_non_loopback_urls_and_incomplete_streams() -> None:
    with pytest.raises(ReaderArgumentError, match="invalid_reader_url"):
        call("https://example.invalid/read", "capability", "read", {})

    class IncompleteStream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(b'{"kind":"header","operation":"stream"}\n')

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), IncompleteStream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/read"
        with pytest.raises(ReaderArgumentError, match="incomplete_stream"):
            list(stream(url, "capability", "stream", {}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_thin_client_bounds_untrusted_http_error_codes() -> None:
    class BadError(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.dumps({"error": {"code": "x" * 1000}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BadError)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/read"
        with pytest.raises(ReaderArgumentError, match="transport_failed"):
            call(url, "capability", "read", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
