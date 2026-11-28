from pathlib import Path
import sys

import pytest

from evolution_lab.projects.endpoint import (
    SOCKET_NAME, default_socket, resolve_socket, socket_fits)


def test_default_socket_is_in_the_home():
    assert default_socket(Path("/h")) == Path("/h/service.sock") and SOCKET_NAME == "service.sock"


def test_explicit_socket_wins_over_home_and_environment():
    assert resolve_socket("/s/x.sock", "/h", {"LAB_HOME": "/e"}) == Path("/s/x.sock")


def test_home_flag_wins_over_environment():
    assert resolve_socket(None, "/h", {"LAB_HOME": "/e"}) == Path("/h/service.sock")


def test_lab_home_then_xdg_then_user_default():
    assert resolve_socket(None, None, {"LAB_HOME": "/e"}) == Path("/e/service.sock")
    assert resolve_socket(None, None, {"XDG_DATA_HOME": "/x"}) == Path("/x/evolution-lab/service.sock")


def test_socket_fits_is_the_sun_path_limit():
    assert socket_fits(Path("/" + "a" * 106), platform='linux')
    assert not socket_fits(Path("/" + "a" * 107), platform='linux')


def test_too_long_is_refused_with_a_hint_never_replaced():
    home = "/" + "h" * 100
    with pytest.raises(ValueError) as error:
        resolve_socket(None, home, {})
    from evolution_lab.projects.endpoint import too_long_hint
    assert str(error.value) == too_long_hint() and "--socket" in too_long_hint()


@pytest.mark.parametrize('platform,limit', [('linux', 108), ('darwin', 104)])
def test_socket_limit_per_platform(monkeypatch, platform, limit):
    from evolution_lab.projects import endpoint
    assert endpoint.sun_path_max(platform) == limit
    assert socket_fits(Path('/' + 'a' * (limit - 2)), platform=platform)
    assert not socket_fits(Path('/' + 'a' * (limit - 1)), platform=platform)
    assert f'{limit - 1} bytes' in endpoint.too_long_hint(platform)
    # Encoding remains byte-based, even when the character count fits.
    assert not socket_fits(Path('/' + 'é' * (limit // 2)), platform=platform)
    monkeypatch.setattr(endpoint.sys, 'platform', platform)
    fitting_home = '/' + 'h' * (limit - len('/service.sock') - 2)
    assert endpoint.resolve_socket(None, fitting_home, {}) == Path(fitting_home) / 'service.sock'
    with pytest.raises(ValueError, match=f'{limit - 1} bytes'):
        endpoint.resolve_socket(None, fitting_home + 'h', {})


@pytest.mark.parametrize('platform,limit', [('linux', 107), ('darwin', 103)])
def test_service_socket_hint_uses_platform_limit(monkeypatch, platform, limit):
    import runpy
    from evolution_lab.service import errors
    monkeypatch.setattr(sys, 'platform', platform)
    message = runpy.run_path(errors.__file__)['_MESSAGES']['socket_path_too_long']
    assert message == (f'The service socket path is longer than the Unix socket limit ({limit} bytes); '
                       'pass --socket with a shorter path.')
