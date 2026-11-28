"""Content-addressed, atomic storage for preparation descriptors and bundle bytes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .models import (
    BundleDescriptor,
    PreparedExecutionDescriptor,
    PreparationError,
    StoredBundle,
    StoredPrepared,
)

from evolution_lab.durability import sync_directory, sync_file


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the local /1 canonical JSON format without normalizing Unicode."""
    if hasattr(value, "to_payload"):
        value = value.to_payload()
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise PreparationError("invalid_request", "bundle") from None


def descriptor_digest(descriptor: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(descriptor)).hexdigest()


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_descriptor(data: bytes) -> Any:
    try:
        return json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, ValueError, TypeError):
        raise PreparationError("artifacts_invalid", "read") from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class PreparationStorage:
    """Internal persistence boundary used by bundle composition and the S1 facade."""

    def __init__(self, state_directory: Path | str, *, read_only: bool = False):
        self.state_directory = Path(state_directory)
        self._stage_guard = threading.Lock()
        self._bundle_stages: dict[Path, tuple[int, int]] = {}
        if read_only:
            for path in (self.state_directory, self.state_directory / "bundles", self.state_directory / "prepared"):
                if path.is_symlink() or not path.is_dir():
                    raise PreparationError("not_found", "read")
            return
        try:
            if self.state_directory.is_symlink():
                raise OSError("state directory is a symlink")
            self.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.state_directory, 0o700)
            for relative in ("bundles", "prepared", "receipts/bundles"):
                directory = self.state_directory / relative
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if directory.is_symlink():
                    raise OSError("storage directory is a symlink")
                os.chmod(directory, 0o700)
            lock = self.state_directory / ".publish.lock"
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(descriptor)
            os.chmod(lock, 0o600)
        except OSError:
            raise PreparationError("io_error", "bundle") from None

    def bundle_path(self, digest: str) -> Path:
        self._validate_digest(digest, "read")
        return self.state_directory / "bundles" / digest

    def prepared_path(self, digest: str) -> Path:
        self._validate_digest(digest, "read")
        return self.state_directory / "prepared" / digest

    def create_bundle_staging(self) -> Path:
        path = self.state_directory / "bundles" / f".{uuid.uuid4().hex}.staging"
        try:
            path.mkdir(mode=0o700)
            info = path.stat(follow_symlinks=False)
            with self._stage_guard:
                self._bundle_stages[self._stage_key(path)] = (info.st_dev, info.st_ino)
            return path
        except OSError:
            raise PreparationError("io_error", "bundle") from None

    def publish_bundle(
        self,
        staging: Path | str,
        descriptor: BundleDescriptor,
        *,
        source_receipt: Mapping[str, Any] | None = None,
    ) -> StoredBundle:
        stage = self._claim_bundle_stage(Path(staging))
        digest = descriptor.digest
        destination = self.state_directory / "bundles" / digest
        try:
            self._verify_inventory(stage, descriptor, descriptor_present=False)
            with self._publication_lock():
                reused = self._destination_exists(destination, "bundle")
                if not reused:
                    self._write_descriptor(stage / "descriptor.json", descriptor, stage="bundle")
                    self._sync_and_freeze_bundle(stage, descriptor)
                    self._publish_directory(stage, destination)
            if reused:
                self._discard_stage(stage)
            stored = self._read_bundle_at(destination, digest, stage="bundle" if reused else "read")
            if source_receipt is not None:
                self._publish_receipt(digest, source_receipt)
            return stored
        except PreparationError:
            self._discard_stage(stage)
            raise
        except OSError:
            self._discard_stage(stage)
            raise PreparationError("io_error", "bundle") from None

    def abort_bundle_staging(self, staging: Path | str) -> None:
        """Discard an owned, unpublished staging directory after composition fails."""
        stage = self._claim_bundle_stage(Path(staging))
        self._discard_stage(stage)

    def publish_prepared(self, descriptor: PreparedExecutionDescriptor) -> StoredPrepared:
        self._validate_prepared_link(descriptor, stage="prepare")
        digest = descriptor.digest
        stage = self.state_directory / "prepared" / f".{uuid.uuid4().hex}.staging"
        destination = self.state_directory / "prepared" / digest
        try:
            with self._publication_lock():
                if self._destination_exists(destination, "prepare"):
                    return self._read_prepared_at(destination, digest, stage="prepare")
            stage.mkdir(mode=0o700)
            self._write_descriptor(stage / "descriptor.json", descriptor, stage="prepare")
            self._fsync_file(stage / "descriptor.json")
            os.chmod(stage / "descriptor.json", 0o400)
            self._fsync_directory(stage)
            os.chmod(stage, 0o500)
            with self._publication_lock():
                reused = self._destination_exists(destination, "prepare")
                if not reused:
                    self._publish_directory(stage, destination)
            stored = self._read_prepared_at(destination, digest, stage="prepare" if reused else "read")
            if reused:
                self._discard_stage(stage)
            return stored
        except PreparationError:
            self._discard_stage(stage)
            raise
        except OSError:
            self._discard_stage(stage)
            raise PreparationError("io_error", "prepare") from None

    def get_bundle(self, digest: str) -> StoredBundle:
        self._validate_digest(digest, "read")
        return self._read_bundle_at(self.state_directory / "bundles" / digest, digest, stage="read")

    def get_prepared(self, digest: str) -> StoredPrepared:
        self._validate_digest(digest, "read")
        return self._read_prepared_at(self.state_directory / "prepared" / digest, digest, stage="read")

    @staticmethod
    def _validate_digest(digest: Any, stage: str) -> None:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise PreparationError("invalid_request", stage)

    @staticmethod
    def _stage_key(stage: Path) -> Path:
        return Path(os.path.abspath(stage))

    def _claim_bundle_stage(self, stage: Path) -> Path:
        key = self._stage_key(stage)
        with self._stage_guard:
            identity = self._bundle_stages.pop(key, None)
        if identity is None:
            raise PreparationError("invalid_request", "bundle")
        try:
            info = key.stat(follow_symlinks=False)
        except OSError:
            raise PreparationError("invalid_request", "bundle") from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != identity
            or key.parent != self._stage_key(self.state_directory / "bundles")
        ):
            raise PreparationError("invalid_request", "bundle")
        return key

    @staticmethod
    def _write_descriptor(path: Path, descriptor: Any, *, stage: str) -> None:
        data = canonical_json_bytes(descriptor)
        try:
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(data)
                stream.flush()
                sync_file(stream.fileno())
        except FileExistsError:
            raise PreparationError("artifacts_invalid", stage) from None

    @contextmanager
    def _publication_lock(self) -> Iterator[None]:
        lock_path = self.state_directory / ".publish.lock"
        with lock_path.open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    @staticmethod
    def _destination_exists(destination: Path, operation_stage: str) -> bool:
        """Check publication destinations while the caller holds .publish.lock."""
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise PreparationError("artifacts_invalid", operation_stage)
            return True
        return False

    def _publish_directory(self, stage: Path, destination: Path) -> None:
        """Publish a checked, absent destination while holding .publish.lock."""
        os.rename(stage, destination)
        self._fsync_directory(destination.parent)

    def _read_bundle_at(self, path: Path, digest: str, *, stage: str) -> StoredBundle:
        if not path.exists():
            raise PreparationError("not_found", stage)
        if path.is_symlink() or not path.is_dir():
            raise PreparationError("artifacts_invalid", stage)
        payload, descriptor_bytes = self._read_payload(path / "descriptor.json", stage)
        descriptor = BundleDescriptor.from_payload(payload, stage=stage)
        if canonical_json_bytes(descriptor) != descriptor_bytes:
            raise PreparationError("artifacts_invalid", stage)
        if descriptor.digest != digest:
            raise PreparationError("digest_mismatch", stage)
        self._verify_inventory(path, descriptor, descriptor_present=True, stage=stage)
        return StoredBundle(digest=digest, path=path, descriptor=descriptor)

    def _read_prepared_at(self, path: Path, digest: str, *, stage: str) -> StoredPrepared:
        if not path.exists():
            raise PreparationError("not_found", stage)
        if path.is_symlink() or not path.is_dir():
            raise PreparationError("artifacts_invalid", stage)
        payload, descriptor_bytes = self._read_payload(path / "descriptor.json", stage)
        descriptor = PreparedExecutionDescriptor.from_payload(payload, stage=stage)
        if canonical_json_bytes(descriptor) != descriptor_bytes:
            raise PreparationError("artifacts_invalid", stage)
        if descriptor.digest != digest:
            raise PreparationError("digest_mismatch", stage)
        try:
            names = {entry.name for entry in os.scandir(path)}
        except OSError:
            raise PreparationError("io_error", stage) from None
        if names != {"descriptor.json"} or (path / "descriptor.json").is_symlink():
            raise PreparationError("artifacts_invalid", stage)
        self._validate_prepared_link(descriptor, stage=stage)
        return StoredPrepared(digest=digest, path=path, descriptor=descriptor)

    @staticmethod
    def _read_payload(path: Path, stage: str) -> tuple[Any, bytes]:
        try:
            if path.is_symlink() or not path.is_file():
                raise PreparationError("artifacts_invalid", stage)
            data = path.read_bytes()
        except PreparationError:
            raise
        except FileNotFoundError:
            raise PreparationError("artifacts_invalid", stage) from None
        except OSError:
            raise PreparationError("io_error", stage) from None
        try:
            return _decode_descriptor(data), data
        except PreparationError as error:
            raise PreparationError(error.code, stage) from None

    def _validate_prepared_link(self, descriptor: PreparedExecutionDescriptor, *, stage: str) -> None:
        try:
            bundle = self._read_bundle_at(
                self.state_directory / "bundles" / descriptor.bundle_digest,
                descriptor.bundle_digest,
                stage=stage,
            )
        except PreparationError as error:
            raise PreparationError(error.code, stage) from None
        environment_digests = {environment.digest for environment in bundle.descriptor.environments}
        if (
            descriptor.entrypoint != bundle.descriptor.entrypoint
            or descriptor.execution_environment not in environment_digests
            or descriptor.target_binding.environment_digest not in environment_digests
            or descriptor.target_binding.launch_digest != bundle.summary().launch_digest
            or (
                descriptor.secret_names != bundle.descriptor.secret_names
                if descriptor.kind == "campaign"
                else not set(descriptor.secret_names) <= set(bundle.descriptor.secret_names)
            )
        ):
            raise PreparationError("digest_mismatch", stage)

    def _verify_inventory(
        self,
        root: Path,
        descriptor: BundleDescriptor,
        *,
        descriptor_present: bool,
        stage: str = "bundle",
    ) -> None:
        expected = {entry.path: entry for entry in descriptor.entries}
        actual: dict[str, os.stat_result] = {}
        try:
            stack = [root]
            while stack:
                directory = stack.pop()
                for item in os.scandir(directory):
                    path = Path(item.path)
                    relative = path.relative_to(root).as_posix()
                    if relative == "descriptor.json" and descriptor_present:
                        if item.is_symlink() or not item.is_file(follow_symlinks=False):
                            raise PreparationError("artifacts_invalid", stage)
                        continue
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        raise PreparationError("artifacts_invalid", stage)
                    actual[relative] = info
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(path)
        except PreparationError:
            raise
        except OSError:
            raise PreparationError("io_error", stage) from None
        if set(actual) != set(expected):
            raise PreparationError("artifacts_invalid", stage)
        for path, entry in expected.items():
            info = actual[path]
            kind = "file" if stat.S_ISREG(info.st_mode) else "directory"
            if kind != entry.type or stat.S_IMODE(info.st_mode) & 0o111 != entry.executable:
                raise PreparationError("artifacts_invalid", stage)
            if entry.type == "file":
                try:
                    if info.st_size != entry.size or _sha256_file(root / path) != entry.sha256:
                        raise PreparationError("artifacts_invalid", stage)
                except PreparationError:
                    raise
                except OSError:
                    raise PreparationError("io_error", stage) from None

    def _sync_and_freeze_bundle(self, root: Path, descriptor: BundleDescriptor) -> None:
        for entry in descriptor.entries:
            path = root / entry.path
            if entry.type == "file":
                self._fsync_file(path)
                os.chmod(path, 0o400 | entry.executable)
        os.chmod(root / "descriptor.json", 0o400)
        directory_entries = [entry for entry in descriptor.entries if entry.type == "directory"]
        for entry in sorted(directory_entries, key=lambda value: value.path.count("/"), reverse=True):
            directory = root / entry.path
            self._fsync_directory(directory)
            os.chmod(directory, 0o400 | entry.executable)
        self._fsync_directory(root)
        os.chmod(root, 0o500)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as stream:
            sync_file(stream.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        sync_directory(path)

    def _publish_receipt(self, digest: str, receipt: Mapping[str, Any]) -> None:
        data = canonical_json_bytes(dict(receipt))
        destination = self.state_directory / "receipts" / "bundles" / f"{digest}.json"
        pending = destination.with_name(f".{digest}.{uuid.uuid4().hex}.pending")
        try:
            if destination.exists() or destination.is_symlink():
                return
            with pending.open("xb") as stream:
                os.chmod(pending, 0o600)
                stream.write(data)
                stream.flush()
                sync_file(stream.fileno())
            try:
                os.link(pending, destination)
                self._fsync_directory(destination.parent)
            except FileExistsError:
                pass
            finally:
                pending.unlink(missing_ok=True)
        except OSError:
            pending.unlink(missing_ok=True)
            raise PreparationError("io_error", "bundle") from None

    @staticmethod
    def _discard_stage(stage: Path) -> None:
        try:
            info = stage.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                stage.unlink()
                return
            os.chmod(stage, 0o700)
            for root, directories, _ in os.walk(stage, topdown=True, followlinks=False):
                root_path = Path(root)
                os.chmod(root_path, 0o700)
                real_directories = []
                for name in directories:
                    child = root_path / name
                    child_info = child.lstat()
                    if stat.S_ISLNK(child_info.st_mode):
                        child.unlink()
                    elif stat.S_ISDIR(child_info.st_mode):
                        os.chmod(child, 0o700)
                        real_directories.append(name)
                directories[:] = real_directories
            shutil.rmtree(stage)
        except FileNotFoundError:
            pass
        except OSError:
            pass
