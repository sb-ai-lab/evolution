"""Operator-root ancestors are resolved; the root itself remains a capability boundary."""
import pytest


def test_canonical_root_resolves_ancestors_only(tmp_path):
    from evolution_lab._paths import canonical_root
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir()
    link.symlink_to(real, target_is_directory=True)
    assert canonical_root(link / 'state') == real.resolve() / 'state'
    assert canonical_root(link / 'missing' / 'tail') == real.resolve() / 'missing/tail'
    assert canonical_root(link) == tmp_path.resolve() / 'link'
    assert canonical_root(real / 'state') == real.resolve() / 'state'


@pytest.mark.parametrize('path', ['/', '/tmp/invalid\0root'])
def test_canonical_root_refuses_root_and_nul(path):
    from evolution_lab._paths import canonical_root
    with pytest.raises(ValueError):
        canonical_root(path)


def test_parent_relative_root_is_accepted(tmp_path, monkeypatch):
    from evolution_lab._paths import canonical_root
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    assert canonical_root('../state') == tmp_path.resolve() / 'state'


@pytest.mark.parametrize('tail,leaf', [('link/../state', 'state'), ('link/..', '')])
def test_parent_traversal_follows_symlink_before_parent(tmp_path, monkeypatch, tail, leaf):
    from evolution_lab._paths import canonical_root
    real = tmp_path / 'real'
    (real / 'target').mkdir(parents=True)
    (tmp_path / 'link').symlink_to(real / 'target', target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert canonical_root(tail) == real.resolve() / leaf
