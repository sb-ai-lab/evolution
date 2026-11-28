"""V3: durable normalized data without overwriting user output."""
import json
import pytest
from observations_support import flat, make_import, source_file


def test_structural_import_remains_readable_without_raw(tmp_path):
    from evolution_lab.observations import ImportGroup, ObservationSet, Source, import_traces
    raw = source_file(tmp_path/'source.json',[flat(question='private')])
    result = import_traces([ImportGroup(sources=[Source(raw)])],tmp_path/'out')
    raw.unlink()
    loaded = ObservationSet.open(result.path)
    assert len(loaded.spans) == 1
    assert loaded.spans[0]['attributes'] == {}
    assert loaded.executions == []


def test_existing_output_is_untouched(tmp_path):
    from evolution_lab.observations import ImportGroup, ObservationError, Source, import_traces
    raw=source_file(tmp_path/'source.json',[flat()]); out=tmp_path/'out';out.mkdir()
    sentinel=out/'mine';sentinel.write_text('keep')
    with pytest.raises(ObservationError) as error:
        import_traces([ImportGroup(sources=[Source(raw)])],out)
    assert error.value.code == 'output_failed'
    assert sentinel.read_text() == 'keep'
    assert not (out/'manifest.json').exists()


def test_failed_write_never_publishes_manifest(tmp_path, monkeypatch):
    from evolution_lab.observations import ObservationError
    from evolution_lab.observations import storage
    original = storage.write_bytes
    def failing(path, data):
        if path.name == 'executions.jsonl': raise OSError('disk failure')
        return original(path,data)
    monkeypatch.setattr(storage,'write_bytes',failing)
    with pytest.raises(ObservationError): make_import(tmp_path)
    assert not (tmp_path/'observations'/'manifest.json').exists()


def test_manifest_failure_does_not_leave_visible_commit_marker(tmp_path, monkeypatch):
    from evolution_lab.observations import ObservationError
    from evolution_lab.observations import storage
    original=storage.write_bytes
    def failing(path,data):
        if path.name.startswith('manifest'):
            path.write_bytes(b'{')
            raise OSError('disk failure')
        return original(path,data)
    monkeypatch.setattr(storage,'write_bytes',failing)
    with pytest.raises(ObservationError):make_import(tmp_path)
    assert not (tmp_path/'observations'/'manifest.json').exists()
