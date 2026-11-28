import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution_lab.preparation import EnvironmentInput, PreparationError
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Pair
from evolution_lab.projects.registry import Projects
from evolution_lab.projects.session import ProjectSession
from evolution_lab.service.codecs import SERVICE_ENVIRONMENT

PAIR = Pair("0.0.3.dev37", "0.3.0a28")
ENTRY = {"schema": "lab-target-entry/1", "agent": {"module": "a", "attribute": "agent", "import_root": "."},
         "program_paths": ["a.py"], "environment": {"kind": "service"}, "env": {"MODE": "x"},
         "secret_names": ["TOKEN"]}


class FakePreparation:
    def __init__(self):
        self.calls = []

    def create_bundle(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(digest="d" * 64)

    def prepare_evaluation(self, digest, **params):
        raise PreparationError("artifacts_invalid", "prepare")


@pytest.fixture
def probe_env(tmp_path):
    work = tmp_path / "work"
    for name in ("port-a", "port-b", "port-c"):
        (work / name).mkdir(parents=True)
        (work / name / "a.py").write_text("A = 1\n")
    (work / "lab").mkdir()
    (work / "port-a" / "lab-target-entry.json").write_text(json.dumps(ENTRY))
    (work / "port-c" / "lab-target-entry.json").write_text(json.dumps(ENTRY))
    (work / "port-c" / "bundle-request.json").write_text("{}")
    projects = Projects.open(tmp_path / "home")
    projects.create(id="pr", name="P", materials_root="lab", path=str(work), implementations=[
        {"id": "port-a", "name": "A", "place": "port-a"},
        {"id": "port-b", "name": "B", "place": "port-b"},
        {"id": "port-c", "name": "C", "place": "port-c", "package_entry": "port-c/bundle-request.json"}])
    prep = FakePreparation()
    service = SimpleNamespace(input=EnvironmentInput(Path(sys.executable), ()), digest="e" * 64)
    session = ProjectSession(projects, preparation=prep, owner=None, pair=PAIR, state_root=tmp_path / "state",
                             service_environment=service)
    yield session, projects, prep, service, work
    projects.close()


async def test_no_package_with_entry_file_makes_a_probe_bundle(probe_env):
    session, projects, prep, service, work = probe_env
    summary, link = await session.bundle("pr", "port-a")
    call = prep.calls[-1]
    assert link.kind == "bundle" and link.object_id == summary.digest
    assert call["source"].__class__.__name__ == "ProbeSource"
    assert call["source"].entry_path == work / "port-a" / "lab-target-entry.json"
    launch = call["source"].launch
    assert launch["endpoint"]["base_url"].startswith("http://127.0.0.1:")
    assert launch["identity"]["target_id"] == "lab-probe-pr-port-a"
    assert [(r.alias, r.source_path) for r in call["roots"]] == [("implementation", work / "port-a")]
    assert call["env"] == {"MODE": "x"} and list(call["secret_names"]) == ["TOKEN"]


async def test_package_wins_over_entry_file(probe_env):
    session, projects, prep, service, work = probe_env
    with pytest.raises(ProjectError) as caught:  # the package path ran (its file is not a package)
        await session.bundle("pr", "port-c")
    assert caught.value.code == "package_unavailable" and prep.calls == []


async def test_neither_package_nor_entry_is_target_undeclared(probe_env):
    session, projects, prep, service, work = probe_env
    with pytest.raises(ProjectError) as caught:
        await session.bundle("pr", "port-b")
    assert caught.value.code == "target_undeclared" and prep.calls == []


async def test_entry_environment_service_marker_expands_like_a_package(probe_env):
    session, projects, prep, service, work = probe_env
    await session.bundle("pr", "port-a")
    call = prep.calls[-1]
    assert tuple(call["environments"]) == (service.input,)
    assert call["expected_execution_environment"] == service.digest


async def test_entry_with_a_target_environment_appends_it(probe_env):
    session, projects, prep, service, work = probe_env
    entry = dict(ENTRY, environment={"executable": "/opt/py/bin/python", "wheel_paths": ["/opt/w/a.whl"],
                                     "runtime_files": []})
    (work / "port-a" / "lab-target-entry.json").write_text(json.dumps(entry))
    await session.bundle("pr", "port-a")
    call = prep.calls[-1]
    assert call["environments"][0] == service.input
    assert call["environments"][-1] == EnvironmentInput(Path("/opt/py/bin/python"), (Path("/opt/w/a.whl"),), ())


async def test_prepare_on_a_probe_bundle_is_refused_by_preparation(probe_env):
    session, projects, prep, service, work = probe_env
    summary, _ = await session.bundle("pr", "port-a")
    with pytest.raises(PreparationError) as caught:
        await session.prepare("pr", "port-a", bundle_digest=summary.digest, kind="evaluation", params={})
    assert caught.value.code == "artifacts_invalid"


async def test_repeated_probe_bundle_keeps_one_link(probe_env):
    session, projects, prep, service, work = probe_env
    await session.bundle("pr", "port-a")
    await session.bundle("pr", "port-a")
    assert len(projects.store.links("pr", implementation="port-a", kind="bundle")) == 1


def test_package_helpers_accept_a_probe_source(probe_env):
    from evolution_lab.preparation import ProbeSource, SourceRoot
    from evolution_lab.service.codecs import BundleRequest
    from evolution_lab.projects.package import inputs_inventory, source_paths, validate_form
    session, projects, prep, service, work = probe_env
    place = work / "port-a"
    request = BundleRequest(ProbeSource(place / "lab-target-entry.json", {}), (SERVICE_ENVIRONMENT,), {}, (),
                            (SourceRoot("implementation", place),), ())
    assert place / "lab-target-entry.json" in source_paths(request)
    assert inputs_inventory(request, working_copy=work)[1]
    assert validate_form(request, place=place) == []
    (place / "lab-target-entry.json").unlink()
    assert validate_form(request, place=place)[0]["code"] == "unsupported_input"
