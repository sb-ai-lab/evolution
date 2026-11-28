"""Byte pins for declared files and installed pure-Python distributions."""
import hashlib
import re
import subprocess
from dataclasses import dataclass
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


class InputChanged(ValueError):
    """A declared input or installed producing distribution no longer matches."""


class IsolatedEnvironmentError(RuntimeError):
    """The pinned producing environment cannot be verified in isolation."""


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class FilePin:
    path: Path
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> "FilePin":
        path = Path(path).absolute()
        if path.is_symlink() or not path.is_file():
            raise ValueError("declared input must be a regular non-symlink file")
        return cls(path, file_sha256(path))

    def verify(self) -> None:
        try:
            if self.path.is_symlink() or file_sha256(self.path) != self.sha256:
                raise InputChanged("declared file changed")
        except OSError as exc:
            raise InputChanged("declared file unavailable") from exc

    def public(self) -> dict[str, str]:
        return {"path": str(self.path), "sha256": self.sha256}


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _installed_matches(wheel: Path) -> tuple[str, str]:
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate wheel members")
        for name in names:
            parts = PurePosixPath(name)
            if parts.is_absolute() or ".." in parts.parts or "\\" in name:
                raise ValueError("invalid wheel member")
            if any(part.endswith(".data") for part in parts.parts):
                raise ValueError("relocated wheel data is not supported by this pin checker")
        metadata_files = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError("wheel must describe one distribution")
        info = BytesParser().parsebytes(archive.read(metadata_files[0]))
        name, version = info.get("Name"), info.get("Version")
        if not name or not version:
            raise ValueError("wheel metadata is incomplete")
        dist = metadata.distribution(name)
        if _name(dist.metadata["Name"] or "") != _name(name) or dist.version != version:
            raise InputChanged("installed distribution version differs from wheel")
        for member in names:
            if member.endswith("/") or member.endswith(".dist-info/RECORD"):
                continue  # Installers rewrite RECORD to include generated files.
            installed = Path(dist.locate_file(member))
            try:
                if installed.is_symlink() or installed.read_bytes() != archive.read(member):
                    raise InputChanged("installed distribution bytes differ from wheel")
            except OSError as exc:
                raise InputChanged("installed distribution file unavailable") from exc
        return name, version


@dataclass(frozen=True)
class WheelPin:
    path: Path
    sha256: str
    name: str
    version: str

    @classmethod
    def capture(cls, path: Path) -> "WheelPin":
        file = FilePin.capture(path)
        name, version = _installed_matches(file.path)
        file.verify()
        return cls(file.path, file.sha256, name, version)

    def verify(self) -> None:
        FilePin(self.path, self.sha256).verify()
        if _installed_matches(self.path) != (self.name, self.version):
            raise InputChanged("producing distribution changed")
        FilePin(self.path, self.sha256).verify()

    def public(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "wheel": str(self.path), "sha256": self.sha256}


def verify_isolated(pins, *, env=None) -> None:
    """Verify wheel bytes under exactly the interpreter's isolated launch path."""
    import json
    import sys
    child_env = None if env is None else dict(env)
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve())],
            input=json.dumps([pin.public() for pin in pins]), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolatedEnvironmentError("isolated environment verification failed") from exc
    if result.returncode != 0:
        raise IsolatedEnvironmentError("isolated environment verification failed")


if __name__ == "__main__":
    import json
    import sys
    import importlib.util
    for row in json.load(sys.stdin):
        WheelPin(Path(row["wheel"]), row["sha256"], row["name"], row["version"]).verify()
        dist = metadata.distribution(row["name"])
        with ZipFile(row["wheel"]) as archive:
            for member in archive.namelist():
                parts = PurePosixPath(member).parts
                if len(parts) == 2 and parts[1] == "__init__.py":
                    spec = importlib.util.find_spec(parts[0])
                    if spec is None or spec.origin is None or Path(spec.origin).resolve() != Path(dist.locate_file(member)).resolve():
                        raise InputChanged("isolated import origin differs from pinned distribution")
