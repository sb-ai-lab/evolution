"""Distribution dependency admission from the actual built wheel."""
from email.parser import BytesParser
import os
from zipfile import ZipFile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def test_wheel_declares_psutil_for_darwin_only():
    with ZipFile(os.environ['EVOLUTION_LAB_WHEEL']) as wheel:
        metadata_path, = [name for name in wheel.namelist() if name.endswith('.dist-info/METADATA')]
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
    requirements = [Requirement(value) for value in metadata.get_all('Requires-Dist', [])]
    psutil = [value for value in requirements if value.name == 'psutil']
    assert len(psutil) == 2
    assert all(value.specifier == SpecifierSet('>=7,<8') and value.marker is not None for value in psutil)
    runtime = [value for value in psutil if str(value.marker) == 'sys_platform == "darwin"']
    assert len(runtime) == 1
    assert runtime[0].marker.evaluate({'sys_platform': 'darwin', 'extra': ''})
    assert not runtime[0].marker.evaluate({'sys_platform': 'linux', 'extra': ''})
    dev = [value for value in psutil if str(value.marker) == 'extra == "dev"']
    assert len(dev) == 1
    assert all(dev[0].marker.evaluate({'sys_platform': platform, 'extra': 'dev'})
               for platform in ('linux', 'darwin'))
