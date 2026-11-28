import json
from pathlib import Path
import pytest
from evolution_lab.projects.entry import ENTRY_FILE, read_target_entry, probe_launch
from evolution_lab.projects.errors import ProjectError

def _write(place: Path, **over):
    doc = {"schema": "lab-target-entry/1",
           "agent": {"module": "demo_agent", "attribute": "build", "import_root": "agent"},
           "program_paths": ["agent"], "environment": {"kind": "service"},
           "env": {"A": "1"}, "secret_names": ["KEY"]}
    doc.update(over)
    (place / "agent").mkdir(parents=True, exist_ok=True)
    (place / ENTRY_FILE).write_text(json.dumps(doc), encoding="utf-8")
    return doc

def test_absent_file_is_none(tmp_path):
    assert read_target_entry(tmp_path) is None

def test_defaults_and_digest(tmp_path):
    _write(tmp_path)
    entry = read_target_entry(tmp_path)
    assert entry.environment is None and entry.limits["max_instances"] == 2
    assert entry.startup_timeout_seconds == 30.0 and len(entry.digest) == 64

@pytest.mark.parametrize("over,field", [
    ({"extra": 1}, "unknown key"),
    ({"program_paths": ["../x"]}, "program_paths"),
    ({"program_paths": ["/abs"]}, "program_paths"),
    ({"agent": {"module": "m", "attribute": "build"}}, "agent"),
    ({"secret_names": ["A"]}, "secret_names"),          # overlaps env
    ({"limits": {"max_instances": 0}}, "limits"),
])
def test_form_errors_name_the_field(tmp_path, over, field):
    _write(tmp_path, **over)
    with pytest.raises(ProjectError) as caught:
        read_target_entry(tmp_path)
    assert caught.value.code == "target_entry_invalid" and field in caught.value.detail

def test_target_environment_inside_place_is_refused(tmp_path):
    _write(tmp_path, environment={"executable": str(tmp_path / ".venv/bin/python"),
                                  "wheel_paths": [], "runtime_files": []})
    with pytest.raises(ProjectError) as caught:
        read_target_entry(tmp_path)
    assert caught.value.code == "target_entry_invalid" and "environment" in caught.value.detail

def test_relative_environment_path_is_refused(tmp_path):
    _write(tmp_path, environment={"executable": "venv/bin/python", "wheel_paths": [], "runtime_files": []})
    with pytest.raises(ProjectError):
        read_target_entry(tmp_path)

def test_wrong_schema_is_unsupported(tmp_path):
    _write(tmp_path, schema="lab-target-entry/2")
    with pytest.raises(ProjectError) as caught:
        read_target_entry(tmp_path)
    assert caught.value.code == "unsupported_schema"

def test_probe_launch_is_a_launch_2_without_environment_ref(tmp_path):
    _write(tmp_path)
    launch = probe_launch(read_target_entry(tmp_path), target_id="lab-probe-p-i", base_url="http://127.0.0.1:5555")
    assert launch["schema"] == "lab-target-launch/2" and "environment_ref" not in launch
    assert launch["identity"]["seed_mode"] == "none" and launch["identity"]["runtime_ref_digest"] is None
    assert launch["policy"] == ENTRY_FILE and launch["conformance"] is None
    assert launch["agent"] == {"module": "demo_agent", "attribute": "build", "import_root": "agent"}


def test_non_object_file_is_unsupported_schema(tmp_path):
    (tmp_path / ENTRY_FILE).write_text("[]", encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        read_target_entry(tmp_path)
    assert caught.value.code == "unsupported_schema"


def test_invalid_json_is_entry_invalid(tmp_path):
    (tmp_path / ENTRY_FILE).write_text("{", encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        read_target_entry(tmp_path)
    assert caught.value.code == "target_entry_invalid"
