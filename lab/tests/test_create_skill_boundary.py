"""Exercise the documented reviewer comparison with real Lab place inventories."""
import re
from pathlib import Path
import pytest

from evolution_lab.projects.models import Implementation, Project, WorkingCopy
from evolution_lab.projects.revision import materials_revision
from evolution_lab.projects.tree import tree_entries


def _snapshot(project):
    return {entry.path: entry.content_key() for entry in tree_entries(project, materials_revision(project), 'agent', '.') or ()}


def _outside(before, after, *, owned_root, skill='skills/new'):
    # Reference implementation of the skill's documented narrow reviewer rule.
    owned = re.escape(owned_root.rstrip('/'))
    managed = re.compile(rf'^{owned}/(?:place-before\.json|build\.json|check\.json|redo/build-[1-9][0-9]*/(?:build|check)\.json|redo/check-[1-9][0-9]*/check\.json)$')
    managed_files = {name for name in before.keys() | after.keys() if managed.fullmatch(name)
                     and all(side.get(name) is None or side[name][0] == 'file' for side in (before, after))}
    ancestors = {str(Path(name).parent) for name in managed_files}
    ancestors |= {str(parent) for name in managed_files for parent in Path(name).parents if str(parent) != '.'}
    changes = set()
    for name in before.keys() | after.keys():
        if name == skill or name.startswith(skill + '/') or name in managed_files:
            continue
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        # Ignore only a newly created directory required to hold managed files;
        # retain deletion and metadata changes of any pre-existing directory.
        if a is None and b is not None and b[0] == 'directory' and name in ancestors:
            continue
        changes.add(name)
    return changes


@pytest.mark.parametrize('layout', ['equal', 'nested', 'separate'])
def test_boundary_handles_scenario_artifacts_without_masking_other_changes(tmp_path, layout):
    work = tmp_path / 'work'
    place = work / 'agent' if layout != 'equal' else work
    materials = place if layout == 'equal' else (place / 'materials' if layout == 'nested' else work / 'materials')
    place.mkdir(parents=True)
    (place / 'skills').mkdir()
    owned = materials / 'create-skill' / 'agent'
    owned.mkdir(parents=True)
    (owned / 'brief.json').write_text('brief')
    (owned / 'unrelated.txt').write_text('unchanged')
    (place / 'agent.py').write_text('before')
    impl = Implementation('agent', 'Agent', str(place), {'kind': 'connected'}, None, None)
    # Relative place for equal/nested ensures the tree uses normal place prefixes.
    impl = Implementation('agent', 'Agent', str(place.relative_to(work)) if place != work else '.', {'kind': 'connected'}, None, None)
    project = Project('p', 'P', 'ready', None, 't', None, 1, WorkingCopy(str(work), False, False, None), str(materials.relative_to(work)) if materials != work else '.', (), (impl,))
    before = _snapshot(project)
    (owned / 'place-before.json').write_text('snapshot')
    (owned / 'build.json').write_text('build')
    (owned / 'redo' / 'build-1').mkdir(parents=True)
    (owned / 'redo' / 'build-1' / 'check.json').write_text('old check')
    skill = place / 'skills' / 'new'; skill.mkdir(); (skill / 'SKILL.md').write_text('skill')
    owned_rel = str(owned.relative_to(place)) if owned.is_relative_to(place) else '__outside_materials__'
    assert _outside(before, _snapshot(project), owned_root=owned_rel) == set()
    (place / 'agent.py').write_text('changed')
    assert 'agent.py' in _outside(before, _snapshot(project), owned_root=owned_rel)
    if layout == 'equal':
        (owned / 'unrelated.txt').write_text('changed')
        (owned / 'redo' / 'build-1' / 'other.txt').write_text('not managed')
        changes = _outside(before, _snapshot(project), owned_root=owned_rel)
        assert {'create-skill/agent/unrelated.txt', 'create-skill/agent/redo/build-1/other.txt'} <= changes
        (owned / 'brief.json').write_text('tampered')
        assert 'create-skill/agent/brief.json' in _outside(before, _snapshot(project), owned_root=owned_rel)
        (owned / 'build.json').unlink()
        (owned / 'build.json').symlink_to('unrelated.txt')
        assert 'create-skill/agent/build.json' in _outside(before, _snapshot(project), owned_root=owned_rel)
        (owned / 'redo').chmod(0o700)
        # redo was new: only newly added directory entries can be disregarded.
        # Existing ancestor metadata remains visible.
        (owned / '..').resolve().chmod(0o700)
        assert 'create-skill' in _outside(before, _snapshot(project), owned_root=owned_rel)


def test_skill_explains_narrow_boundary_and_snapshot_integrity():
    text = (Path(__file__).parents[1] / 'src/evolution_lab/guides/scenarios/create-skill/SKILL.md').read_text()
    assert 'scenario-owned comparison exceptions' in text
    assert 'pre-existing directory' in text
    assert 'outside the builder’s control' in text
    assert 'never exclude the whole scenario directory' in text


def test_boundary_excludes_exactly_a_single_file_skill(tmp_path):
    # PDR-0032 amendment 2026-11-13: a skill may be one file; only that file is ignored.
    work = tmp_path / 'work'
    (work / 'skills' / 'org').mkdir(parents=True)
    (work / 'skills' / 'org' / 'headcount.yaml').write_text('existing')
    impl = Implementation('agent', 'Agent', '.', {'kind': 'connected'}, None, None)
    project = Project('p', 'P', 'ready', None, 't', None, 1, WorkingCopy(str(work), False, False, None), 'materials', (), (impl,))
    (work / 'materials').mkdir()
    before = _snapshot(project)
    (work / 'skills' / 'org' / 'manager_load.yaml').write_text('new recipe')
    skill = 'skills/org/manager_load.yaml'
    assert _outside(before, _snapshot(project), owned_root='__outside__', skill=skill) == set()
    (work / 'skills' / 'org' / 'manager_load.yaml.bak').write_text('beside the skill')
    (work / 'skills' / 'org' / 'headcount.yaml').write_text('changed')
    assert _outside(before, _snapshot(project), owned_root='__outside__', skill=skill) == {
        'skills/org/manager_load.yaml.bak', 'skills/org/headcount.yaml'}


def test_skill_allows_a_single_file_skill():
    text = (Path(__file__).parents[1] / 'src/evolution_lab/guides/scenarios/create-skill/SKILL.md').read_text()
    assert 'A skill is a folder or a single file' in text
    assert 'or exactly its one file' in text
