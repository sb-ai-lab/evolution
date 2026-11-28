"""Audit cache-filling trace opens against that trace's own nofollow key."""
import os
import shutil
from pathlib import Path

import pytest

from evolution_lab.owner import core_raw, projection_signature
from test_owner_projection_open_audit import opened_paths, outside_inputs
from test_unanswered_count import count_reader


def packed_measurement(stand, *, segmented):
    from scorer_trace_pack import scorer_pack, PACK_FORMAT, encode_pack_record
    root = stand.root
    first = stand.outputs['first-0'].parent
    request, stdout = (first / 'request.json').read_bytes(), (first / 'stdout').read_bytes()
    shutil.rmtree(first)
    pack = scorer_pack(root, 'first', 'first-0', request=request, stdout=stdout)
    if segmented:
        second = stand.outputs['first-1'].parent
        request, stdout = (second / 'request.json').read_bytes(), (second / 'stdout').read_bytes()
        shutil.rmtree(second)
        pack.with_name('first.s1.pack').write_bytes(
            encode_pack_record({'format': PACK_FORMAT, 'kind': 'pack', 'pack_id': 'first', 'segment': 1})
            + encode_pack_record({'format': PACK_FORMAT, 'kind': 'request', 'score_request_id': 'first-1'},
                                 [('request', request)])
            + encode_pack_record({'format': PACK_FORMAT, 'kind': 'output', 'score_request_id': 'first-1',
                                  'status': 'completed', 'returncode': 0}, [('stdout', stdout), ('stderr', b'')]))


async def trace_audit(tmp_path, monkeypatch, *, layout, omit_stdout=False):
    stand = count_reader(tmp_path, monkeypatch)
    if layout != 'directory':
        packed_measurement(stand, segmented=layout == 'segmented')
    named_inputs, trace_reads = {}, []
    current = None
    original_input = projection_signature.input_signature
    original_signature = core_raw.scorer_trace_signature
    original_trace = core_raw.scorer_trace

    def signature_input(path):
        if omit_stdout and Path(path).name == 'stdout':
            return ('negative-control-omission',)
        if current is not None:
            named_inputs.setdefault(current, set()).add(Path(path).absolute())
        return original_input(path)

    def signature(root, request, *, measurement_id):
        nonlocal current
        current = (str(root), request, measurement_id)
        try:
            return original_signature(root, request, measurement_id=measurement_id)
        finally:
            current = None

    def trace(root, request, case, *, measurement_id, **kwargs):
        key = (str(root), request, measurement_id)
        with opened_paths(monkeypatch) as opened:
            # Preserve the real dir_fd capability while observing os.open.
            with monkeypatch.context() as capabilities:
                capabilities.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {os.open})
                value = original_trace(root, request, case, measurement_id=measurement_id, **kwargs)
        trace_reads.append((key, opened))
        return value

    monkeypatch.setattr(projection_signature, 'input_signature', signature_input)
    monkeypatch.setattr(core_raw, 'scorer_trace_signature', signature)
    monkeypatch.setattr(core_raw, 'scorer_trace', trace)
    first = await stand.read()  # actual ReaderSupport._unanswered_counts cache fill
    assert first['first']['unanswered'] == first['second']['unanswered'] == 2
    assert len(trace_reads) == 4
    # Audit each load against its own actual signature probes, never run_signature.
    for key, opened in trace_reads:
        assert any(not flags & os.O_DIRECTORY for _, _, flags in opened)
        unsigned = outside_inputs(opened, named_inputs[key])
        assert not unsigned, f'unsigned trace inputs: {unsigned}'
    assert await stand.read() == first
    assert len(trace_reads) == 4  # warm cache performs metadata probes only


@pytest.mark.parametrize('layout', ['directory', 'pack', 'segmented'])
async def test_trace_cache_filling_opens_are_covered_by_its_own_key(tmp_path, monkeypatch, layout):
    await trace_audit(tmp_path, monkeypatch, layout=layout)


async def test_trace_cache_open_audit_detects_an_omitted_dependency(tmp_path, monkeypatch):
    with pytest.raises(AssertionError, match='unsigned trace inputs.*stdout'):
        await trace_audit(tmp_path, monkeypatch, layout='directory', omit_stdout=True)
