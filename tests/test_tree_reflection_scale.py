"""Practical streaming acceptance for the private reflection reader."""

from __future__ import annotations

import json
import subprocess
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest

from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.roles.editor.editor_evidence import TRACE_ROOT, TRACE_SUBDIR
from evolution.tree.roles.grouping.reflect_grouping import (
    builtin_grouping,
    cohorts_from_grouping,
    select_portion,
)
from evolution.tree.roles.reflection_reader import MAX_STREAM_FRAME_BYTES
from evolution.tree.roles.reflection_role_helpers import install_helpers
from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace
from tests.tree.roles.reflector.test_reflection_inputs import _inputs

pytestmark = pytest.mark.slow


def _stream(
    helper: tuple[str, ...],
    arguments: dict[str, object],
    validate: Callable[[dict[str, Any]], None],
    *,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> tuple[int, int]:
    """Consume bounded NDJSON lines, retaining no frame beyond its callback."""

    maximum = frames = 0
    process = popen(
        [*helper, json.dumps(arguments, separators=(",", ":"))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdout is not None
        while line := process.stdout.readline(MAX_STREAM_FRAME_BYTES + 1):
            raw = line.encode()
            assert len(raw) <= MAX_STREAM_FRAME_BYTES
            maximum = max(maximum, len(raw))
            validate(json.loads(line))
            frames += 1
        assert process.wait(timeout=20) == 0, process.stderr.read(8192) if process.stderr else ""
        return frames, maximum
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_generated_helper_streams_32768_lightweight_records_with_bounded_parent_buffers(
    tmp_path,
) -> None:
    trace = _Trace(tmp_path, turns=1)
    rows = tuple(
        SampleRow(
            case_id="c",
            sample_id=f"unscored-{repeat}",
            repeat=repeat,
            requested_seed=None,
            effective_seed=None,
            boot_id="boot",
            dependency_digest="dep",
            outcome=None,
            disposition="unscored",
            metrics=None,
            critical_pass=None,
        )
        for repeat in range(1, 32_769)
    )
    try:
        source = _inputs(trace, rows=rows)
        with source.reader_view().open_operation(
            "scale-light", {"stream_grouping_inputs"}
        ) as reader:
            helpers = install_helpers(tmp_path / "helpers", reader)
            outer_header = inner_header = inner_final = outer_final = False
            expected = 0

            def validate(frame: dict[str, Any]) -> None:
                nonlocal outer_header, inner_header, inner_final, outer_final, expected
                if frame["kind"] == "header":
                    assert not outer_header
                    outer_header = True
                elif frame["kind"] == "final":
                    assert outer_header and inner_final
                    outer_final = True
                else:
                    value = frame["value"]
                    if value["kind"] == "header":
                        assert not inner_header
                        inner_header = True
                    elif value["kind"] == "final":
                        inner_final = True
                    else:
                        assert inner_header and not inner_final
                        assert value["index"] == expected and value["offset"] == 0
                        assert len(value["section_digest"]) == 64
                        expected += 1

            starts = 0
            original_popen = subprocess.Popen

            def tracked(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
                nonlocal starts
                starts += 1
                return original_popen(*args, **kwargs)

            tracemalloc.start()
            before, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            frames, maximum = _stream(
                helpers.descriptors["stream_grouping_inputs"],
                {"sections": ["identity"]},
                validate,
                popen=tracked,
            )
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert starts == 1
            assert (
                outer_header and inner_header and inner_final and outer_final and expected == 32_768
            )
            assert peak - before <= source.reader_view().body_cache.capacity + 16 * 1024 * 1024
            assert (
                reader.summary.entries
                and reader.summary.entries[0]["operation"] == "stream_grouping_inputs"
            )
            print(
                f"scale-light starts={starts} frames={frames} cache={source.reader_view().body_cache.size} peak={peak - before} frame={maximum}"
            )
    finally:
        trace.close()


def test_authenticated_near_megabyte_observations_stay_lazy_until_one_stream(
    tmp_path, monkeypatch
) -> None:
    trace = _Trace(
        tmp_path,
        turns=1,
        samples=64,
        events=({"type": "assistant_message", "content": "x" * 980_000},),
    )
    try:
        failed = {
            "metrics": {"accuracy": 1.0},
            "verdicts": [{"criterion": "scale", "passed": False}],
        }
        for request_id in trace.request_ids:
            (trace.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / "stdout").write_text(
                json.dumps(failed)
            )
        raw_sizes = [
            (trace.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / "request.json")
            .stat()
            .st_size
            for request_id in trace.request_ids
        ]
        assert all(900_000 <= size <= 1_000_000 for size in raw_sizes)
        source = _inputs(trace, rows=tuple(_reflect_row(f"s{i}", i) for i in range(1, 65)))
        projections = 0
        original = source._observation_projection

        def counted(repeat):
            nonlocal projections
            projections += 1
            return original(repeat)

        monkeypatch.setattr(source, "_observation_projection", counted)
        grouped = builtin_grouping(source.registry, policy_identity="scale")
        selected = select_portion(
            cohorts_from_grouping(grouped), registry=source.registry, variants=grouped.variants
        )
        assert grouped.neighborhoods and selected.portions and projections == 0
        with source.reader_view().open_operation("scale-raw", {"stream_grouping_inputs"}) as reader:
            helpers = install_helpers(tmp_path / "raw-helpers", reader)
            starts = observations = 0
            outer_header = inner_header = inner_final = outer_final = False

            def validate(frame: dict[str, Any]) -> None:
                nonlocal observations, outer_header, inner_header, inner_final, outer_final
                if frame["kind"] == "header":
                    outer_header = True
                elif frame["kind"] == "final":
                    outer_final = True
                else:
                    value = frame["value"]
                    if value["kind"] == "header":
                        inner_header = True
                    elif value["kind"] == "final":
                        inner_final = True
                    else:
                        assert value["index"] == observations and value["offset"] == 0
                        assert json.loads(value["content"])["status"] == "present"
                        observations += 1

            original_popen = subprocess.Popen

            def tracked(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
                nonlocal starts
                starts += 1
                return original_popen(*args, **kwargs)

            tracemalloc.start()
            before, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            frames, maximum = _stream(
                helpers.descriptors["stream_grouping_inputs"],
                {"sections": ["observation"]},
                validate,
                popen=tracked,
            )
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert (
                starts == 1
                and observations == 64
                and outer_header
                and inner_header
                and inner_final
                and outer_final
            )
            assert peak - before <= source.reader_view().body_cache.capacity + 16 * 1024 * 1024
            assert projections >= 64
            print(
                f"scale-raw starts={starts} raw={min(raw_sizes)}..{max(raw_sizes)} frames={frames} cache={source.reader_view().body_cache.size} peak={peak - before} frame={maximum}"
            )
    finally:
        trace.close()
