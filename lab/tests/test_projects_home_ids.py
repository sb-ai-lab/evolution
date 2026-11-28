import os
import random
import stat

import pytest

from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.home import HomeLock, default_home, prepare_home
from evolution_lab.projects.ids import generate_id, validate_id


@pytest.mark.parametrize("value", ["acquiring", "ab", "quiet-river-42", "a" * 40])
def test_valid_ids(value):
    assert validate_id(value) == value


@pytest.mark.parametrize("value", ["", "a", "Acq", "1abc", "a_b", "a" * 41, None, "a/b", "-ab"])
def test_invalid_ids(value):
    with pytest.raises(ProjectError) as caught:
        validate_id(value)
    assert caught.value.code == "invalid_id"


def test_generated_ids_are_valid_and_avoid_taken():
    taken = {"amber-anchor-00"}
    value = generate_id(lambda name: name in taken, rng=random.Random(1))
    assert validate_id(value) == value and value not in taken
    parts = value.split("-")
    assert len(parts) == 3 and len(parts[2]) == 2 and parts[2].isdigit()


def test_generation_gives_up_when_everything_is_taken():
    with pytest.raises(ProjectError) as caught:
        generate_id(lambda name: True, rng=random.Random(1))
    assert caught.value.code == "id_taken"


def test_default_home_order(tmp_path):
    assert default_home({"LAB_HOME": "/x/lab"}) == tmp_path.__class__("/x/lab")
    assert default_home({"XDG_DATA_HOME": "/x/data"}).as_posix() == "/x/data/evolution-lab"
    assert default_home({}).as_posix().endswith("/.local/share/evolution-lab")


def test_prepare_home_creates_private_layout(tmp_path):
    home = prepare_home(tmp_path / "a" / "home")
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert (home / "clones").is_dir() and (home / "workspaces").is_dir()


def test_prepare_home_refuses_public_or_relative(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    os.chmod(public, 0o755)
    for bad in (public, tmp_path.relative_to(tmp_path.anchor)):
        with pytest.raises(ProjectError) as caught:
            prepare_home(bad)
        assert caught.value.code == "home_invalid"


def test_second_writer_is_busy(tmp_path):
    home = prepare_home(tmp_path / "home")
    first = HomeLock(home)
    with pytest.raises(ProjectError) as caught:
        HomeLock(home)
    assert caught.value.code == "home_busy"
    first.release()
    HomeLock(home).release()


def test_home_under_symlinked_ancestor_is_accepted(tmp_path):
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    assert prepare_home(link / 'home') == real.resolve() / 'home'
    assert (real / 'home/clones').is_dir()


def test_symlinked_home_root_is_refused(tmp_path):
    real, link = tmp_path / 'real', tmp_path / 'link'
    real.mkdir(mode=0o700)
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ProjectError):
        prepare_home(link)
    assert not (real / 'clones').exists()
