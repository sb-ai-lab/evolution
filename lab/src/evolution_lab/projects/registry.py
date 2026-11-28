"""Project registry operations and the creation lifecycle (spec §3.2)."""
from __future__ import annotations

import os
import secrets
import shutil
import stat
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

from evolution_lab.durability import Durability

from .clone import _git, _option_like, _redact, clone_repository
from .errors import ProjectError
from .home import REGISTRY_NAME, HomeLock, prepare_home
from .ids import generate_id, validate_id
from .models import Implementation, Project, WorkingCopy
from .port import LEFTOVER_MARKER_SUFFIX, is_leftover_marker, is_marked_leftover
from .store import RegistryStore

_DISRUPT_PROJECT = {"archive", "set_root", "set_working_copy"}
_DISRUPT_IMPLEMENTATION = {"archive_implementation", "move_implementation"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative(value: object, *, allow_absolute: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ProjectError("invalid_request")
    path = PurePosixPath(value)
    if path.is_absolute():
        if not allow_absolute or ".." in path.parts:
            raise ProjectError("invalid_request")
        return value
    if ".." in path.parts:
        raise ProjectError("invalid_request")
    return path.as_posix()


def _origin(value: object, known: set[str]) -> dict:
    if value is None:
        return {"kind": "connected"}
    if isinstance(value, Mapping) and dict(value) in ({"kind": "connected"}, {"kind": "created"}):
        return dict(value)
    if (isinstance(value, Mapping) and set(value) == {"kind", "implementation"}
            and value["kind"] == "ported_from" and value["implementation"] in known):
        return dict(value)
    raise ProjectError("invalid_request")


def _git_present(path: Path) -> bool:
    """Whether ``path`` is itself a git checkout root (has its own ``.git``).

    Deliberately not ``git -C <path> rev-parse --is-inside-work-tree``: that walks up
    to any ancestor repository, so a working copy created under a tmp dir that itself
    lives inside another checkout (as pytest's ``--basetemp`` does here, per the host's
    /tmp-quota workaround) would always read as "git present" even with no ``.git`` of
    its own.
    """
    return (path / ".git").exists()


def _stored_source(source: Mapping) -> dict:
    """``working_copy.source`` as stored and returned: URL credentials redacted. The real URL
    serves only the clone call (codex fix wave, follow-up 3)."""
    return {**source, "repo": _redact(source["repo"])}


def _source_from_clone(destination: Path) -> dict:
    """Re-derive ``{"repo", "base_revision", "branch"}`` from a clone already on disk.

    Used by :meth:`Projects.reconcile_creating` when a process crashed after
    ``staging.rename(destination)`` but before the registry recorded the ready state
    (so ``source`` was never written). Raises ``ProjectError("clone_failed")`` (via
    :func:`clone._git`) if the clone is unreadable, which the caller turns into a
    ``failed``/``interrupted`` reconciliation instead of a false ``ready``.
    """
    return {
        "repo": _git("config", "--get", "remote.origin.url", cwd=destination),
        "base_revision": _git("rev-parse", "HEAD", cwd=destination),
        "branch": _git("branch", "--show-current", cwd=destination),
    }


class Projects:
    def __init__(self, home: Path, store: RegistryStore, lock: HomeLock | None, now, clone_runner) -> None:
        self.home, self.store, self._lock = home, store, lock
        self._now, self._clone = now, clone_runner

    @classmethod
    def open(cls, home: Path, *, read_only: bool = False, now: Callable[[], str] = utc_now,
             clone_runner=clone_repository, protected: Iterable[Path] = (),
             durability: Durability | None = None) -> "Projects":
        """``protected``: paths the opener owns in or near the home (the service's state root) — the port-leftover
        sweep never removes them, anything inside them, or anything that holds them."""
        home = prepare_home(Path(home)) if not read_only else Path(home)
        lock = None if read_only else HomeLock(home)
        try:
            store = RegistryStore.open(home / REGISTRY_NAME, read_only=read_only, durability=durability)
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        projects = cls(home, store, lock, now, clone_runner)
        if not read_only:
            projects.reconcile_creating()
            projects._sweep_port_leftovers(protected)
        return projects

    def close(self) -> None:
        self.store.close()
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> "Projects":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # reads
    def list(self, *, archived: bool = False) -> tuple[Project, ...]:
        return self.store.projects(archived=archived)

    def get(self, project_id: str) -> Project:
        project = self.store.project(validate_id(project_id))
        if project is None:
            raise ProjectError("project_not_found")
        return project

    def ready(self, project_id: str) -> Project:
        project = self.get(project_id)
        if project.archived is not None:
            raise ProjectError("archived")
        if project.state != "ready":
            raise ProjectError("project_not_ready")
        return project

    def implementation(self, project: Project, implementation_id: str | None) -> Implementation:
        if implementation_id is None:
            active = project.active_implementations()
            if len(active) != 1:
                raise ProjectError("ambiguous_implementation" if active else "implementation_not_found")
            return active[0]
        item = project.implementation(implementation_id)
        if item is None:
            raise ProjectError("implementation_not_found")
        if item.archived is not None:
            raise ProjectError("archived")
        return item

    def root_path(self, project: Project) -> Path:
        return Path(project.working_copy.path) / project.materials_root

    def place_path(self, project: Project, implementation: Implementation) -> Path:
        place = PurePosixPath(implementation.place)
        return Path(place) if place.is_absolute() else Path(project.working_copy.path) / place

    # creation
    def _implementations(self, specs: Sequence[Mapping], *, existing: frozenset[str] = frozenset()) -> tuple[Implementation, ...]:
        result: list[Implementation] = []
        for spec in specs:
            if not isinstance(spec, Mapping) or not set(spec) <= {"id", "name", "place", "origin", "package_entry"}:
                raise ProjectError("invalid_request")
            if "name" in spec and (not isinstance(spec["name"], str) or not spec["name"]):
                raise ProjectError("invalid_request")  # like rename_implementation (final review M7)
            known = set(existing) | {item.id for item in result}
            item_id = validate_id(spec.get("id") or generate_id(lambda name: name in known))
            if item_id in known:
                raise ProjectError("id_taken")
            package = spec.get("package_entry")
            result.append(Implementation(
                item_id, spec.get("name") or item_id, _relative(spec.get("place"), allow_absolute=True),
                _origin(spec.get("origin"), known), None if package is None else _relative(package), None,
            ))
        return tuple(result)

    def create(self, *, id: str | None, name: str, materials_root: str, implementations: Sequence[Mapping],
               path: str | None = None, clone: Mapping | None = None, exclude: Sequence[str] = ()) -> Project:
        if (path is None) == (clone is None) or not isinstance(name, str) or not name:
            raise ProjectError("invalid_request")
        root = _relative(materials_root)
        items = self._implementations(implementations)
        patterns = tuple(_relative(pattern) for pattern in exclude)
        if id is None:
            project_id = generate_id(self.store.known_id)
        else:
            project_id = validate_id(id)
            existing = self.store.project(project_id)
            if existing is not None and existing.state != "failed":
                raise ProjectError("id_taken")
        if path is not None:
            directory = Path(path)
            if not directory.is_absolute() or not directory.is_dir():
                raise ProjectError("working_copy_unavailable")
            project = Project(project_id, name, "ready", None, self._now(), None, 1,
                              WorkingCopy(str(directory), False, _git_present(directory), None),
                              root, patterns, items)
            self.store.put_project(project)
            return project
        if not isinstance(clone, Mapping) or not set(clone) <= {"repo", "rev"} or not isinstance(clone.get("repo"), str):
            raise ProjectError("invalid_request")
        if _option_like(clone.get("rev")):
            raise ProjectError("invalid_request")  # before the record and before git
        destination = self.home / "clones" / project_id
        pending = Project(project_id, name, "creating", None, self._now(), None, 1,
                          WorkingCopy(str(destination), True, True, None), root, patterns, items)
        self.store.put_project(pending)
        staging = self.home / "clones" / f".staging-{project_id}-{secrets.token_hex(4)}"
        try:
            if destination.exists():
                raise ProjectError("clone_failed")
            source = _stored_source(self._clone(clone["repo"], clone.get("rev"), staging, f"lab/{project_id}"))
            staging.rename(destination)
        except (ProjectError, OSError) as error:
            # A filesystem error from the clone or the rename (PermissionError, EIO, ...) is the
            # same failed transition as a git failure (ADR-0017 p. 9, codex r2), not "creating"
            # left for a restart to reconcile.
            code = error.code if isinstance(error, ProjectError) else "clone_failed"
            shutil.rmtree(staging, ignore_errors=True)
            self.store.put_project(replace(pending, state="failed", failure=code))
            if isinstance(error, ProjectError):
                raise
            raise ProjectError("clone_failed") from None
        ready = replace(pending, state="ready", working_copy=replace(pending.working_copy, source=source))
        self.store.put_project(ready)
        return ready

    def create_demo(self, *, id: str | None, environment_digest: str, executable: str,
                    port: int | None = None) -> Project:
        """The demo project (spec §3.12) with the clone lifecycle of ADR-0017 p. 9."""
        from . import demo
        if id is None:
            project_id = generate_id(self.store.known_id)
        else:
            project_id = validate_id(id)
            existing = self.store.project(project_id)
            if existing is not None and existing.state != "failed":
                raise ProjectError("id_taken")
        destination = self.home / "clones" / project_id
        pending = Project(project_id, f"Demo ({project_id})", "creating", None, self._now(), None, 1,
                          WorkingCopy(str(destination), True, False, None), demo.MATERIALS_ROOT, (),
                          self._implementations([demo.IMPLEMENTATION]))
        self.store.put_project(pending)
        staging = self.home / "clones" / f".staging-{project_id}-{secrets.token_hex(4)}"
        try:
            if destination.exists():
                raise ProjectError("working_copy_unavailable")
            demo.write_demo(staging, final=destination, project_id=project_id,
                            environment_digest=environment_digest, executable=executable,
                            port=demo.free_port() if port is None else port)
            staging.rename(destination)
        except Exception as error:
            # Any exception from generation gets the same failed transition (never a
            # "creating" record left for a restart to guess about), but what leaves this
            # method depends on what came in: a ProjectError keeps its own code and
            # traceback (bare raise, like the clone path); an OSError or a core ScaffoldError
            # (a ValueError) becomes the closed "working_copy_unavailable" failure a caller
            # can rely on; anything else is a programming bug (TypeError, AttributeError, ...)
            # and must propagate as itself, never be laundered into a project failure code
            # (fix round 1, Important finding).
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(error, ProjectError):
                self.store.put_project(replace(pending, state="failed", failure=error.code))
                raise
            if isinstance(error, (OSError, ValueError)):
                self.store.put_project(replace(pending, state="failed", failure="working_copy_unavailable"))
                raise ProjectError("working_copy_unavailable") from None
            self.store.put_project(replace(pending, state="failed", failure="working_copy_unavailable"))
            raise
        ready = replace(pending, state="ready")
        self.store.put_project(ready)
        return ready

    def reconcile_creating(self) -> None:
        clones = self.home / "clones"
        for project in self.store.projects(archived=True):
            if project.state != "creating":
                continue
            for staging in clones.glob(f".staging-{project.id}-*"):
                shutil.rmtree(staging, ignore_errors=True)
            destination = clones / project.id
            if not destination.is_dir():
                self.store.put_project(replace(project, state="failed", failure="interrupted"))
                continue
            if not project.working_copy.git:
                # A demo (no git): its directory lands by one rename of a complete staging
                # tree, never a clone `_source_from_clone` could re-derive a source from.
                self.store.put_project(replace(project, state="ready"))
                continue
            try:
                source = _stored_source(_source_from_clone(destination))
            except ProjectError:
                # The clone landed but is unreadable (crashed mid-write, corrupted, ...);
                # leave it on disk for inspection rather than pretend it is ready.
                self.store.put_project(replace(project, state="failed", failure="interrupted"))
                continue
            self.store.put_project(replace(project, state="ready",
                                          working_copy=replace(project.working_copy, source=source)))

    # mutations
    def update(self, project_id: str, mutation: Mapping, *, expected_record_version: int | None = None,
               active: Callable[[str, str | None], str | None] | None = None) -> Project:
        project = self.get(project_id)
        if expected_record_version is not None and expected_record_version != project.record_version:
            raise ProjectError("conflict")
        if not isinstance(mutation, Mapping) or not isinstance(mutation.get("op"), str):
            raise ProjectError("invalid_request")
        op = mutation["op"]
        if active is not None:
            target = mutation.get("implementation") if op in _DISRUPT_IMPLEMENTATION else None
            if (op in _DISRUPT_PROJECT or op in _DISRUPT_IMPLEMENTATION) and active(project.id, target):
                raise ProjectError("active_operation")
        updated = self._apply(project, op, mutation)
        updated = replace(updated, record_version=project.record_version + 1)
        self.store.put_project(updated)
        return updated

    def register_port(self, project_id: str, *, source_id: str, port_id: str, place: str, template_source: str,
                      template_ref: str, template_commit: str) -> Project:
        """The port's implementation (``origin=ported_from:<source>``, absolute ``place``) and its ``ports`` row in
        one registry transaction (ADR-0020 p. 5): both or neither. Nothing after the commit can fail."""
        with self.store.transaction():
            project = self.ready(project_id)
            spec = {"id": port_id, "place": place, "origin": {"kind": "ported_from", "implementation": source_id}}
            updated = self._apply(project, "add_implementation", {"op": "add_implementation", "implementation": spec})
            updated = replace(updated, record_version=project.record_version + 1)
            self.store.put_project(updated)
            self.store.insert_port(project_id, port_id, template_source=template_source, template_ref=template_ref,
                                   template_commit=template_commit, created_at=self._now())
        return updated

    def sync_clone(self, project_id: str) -> tuple[Project, Path]:
        """A ``ready`` project whose working copy is Lab's own clone in its place, and that place (PDR-0024, spec
        2026-10-08 §2.1 p. 1). A copy moved by ``set_working_copy`` keeps ``created_by_lab`` and ``source`` in the
        record, so the place itself is compared. ``create`` stores the path as ``str(home / "clones" / id)``, so a
        plain comparison is exact."""
        if self._lock is None:
            # ADR-0017, one writer per home: a read-only handle holds no home lock, so it never fetches nor merges.
            # No dedicated code exists; ``home_busy`` is the closed refusal of "this process may not write the home".
            raise ProjectError("home_busy")
        project = self.ready(project_id)
        wc, clone = project.working_copy, self.home / "clones" / project_id
        if not (wc.created_by_lab and wc.source is not None and Path(wc.path) == clone):
            raise ProjectError("not_a_clone")
        return project, clone

    def record_sync(self, project_id: str, base_revision: str, *, approval_id: str | None = None) -> Project:
        """``source.base_revision`` := the merged source commit, one registry transaction (ADR-0017 amendment
        2026-10-08). The rest of the record is unchanged."""
        with self.store.transaction():
            project, _ = self.sync_clone(project_id)
            source = {**project.working_copy.source, "base_revision": base_revision}
            updated = replace(project, working_copy=replace(project.working_copy, source=source),
                              record_version=project.record_version + 1)
            self.store.put_project(updated)
            if approval_id is not None:
                self.store.set_approval_outcome(approval_id, "applied")
        return updated

    def _sweep_port_leftovers(self, protected: Iterable[Path] = ()) -> None:
        """What an interrupted port left in the home (spec 2026-10-01 §3.4, codex r2): a fetch directory
        ``tmp/port-*`` or a staging ``ports/*/.staging-*`` is removed only when it carries Lab's port-leftover mark
        (``port.mark_leftover``) — never by its name alone — and is not, holds, or lies inside a registered place
        (any implementation or working copy, active or archived) or a ``protected`` path. Symlinks are never
        followed: a symlinked candidate, mark or parent is left alone. A mark whose directory is gone is removed.
        The home's single writer runs this at open, before any port can start; a staging next to an ``--into``
        destination outside the home is not tracked."""
        kept = self._kept_places(protected)
        for parent, pattern in self._leftover_parents():
            for marker in parent.glob(f"{pattern}{LEFTOVER_MARKER_SUFFIX}"):
                if not is_leftover_marker(marker):
                    continue
                directory = parent / marker.name[:-len(LEFTOVER_MARKER_SUFFIX)]
                real = Path(os.path.realpath(parent)) / directory.name
                if any(real == place or real.is_relative_to(place) or place.is_relative_to(real) for place in kept):
                    continue
                if is_marked_leftover(directory):
                    shutil.rmtree(directory, ignore_errors=True)
                if not os.path.lexists(directory):
                    marker.unlink(missing_ok=True)

    def _leftover_parents(self) -> list[tuple[Path, str]]:
        """``(directory, name pattern)`` where port leftovers may lie: real directories only, no symlink on the way."""
        def real_directory(path: Path) -> bool:
            try:
                return stat.S_ISDIR(os.lstat(path).st_mode)
            except OSError:
                return False
        parents = [(self.home / "tmp", "port-*")] if real_directory(self.home / "tmp") else []
        if real_directory(self.home / "ports"):
            parents += [(project, ".staging-*") for project in (self.home / "ports").iterdir() if real_directory(project)]
        return parents

    def _kept_places(self, protected: Iterable[Path]) -> list[Path]:
        """Every registered place and working copy (active or archived) and every protected path, both as written
        and resolved: whichever form a leftover path is compared in."""
        paths = [Path(path) for path in protected]
        for project in self.store.projects(archived=True):
            paths.append(Path(project.working_copy.path))
            paths.extend(self.place_path(project, item) for item in project.implementations)
        kept = []
        for path in paths:
            if path.is_absolute():
                kept += [Path(os.path.normpath(path)), Path(os.path.realpath(path))]
        return kept

    def _apply(self, project: Project, op: str, m: Mapping) -> Project:
        def keys(*names: str) -> None:
            if set(m) != {"op", *names}:
                raise ProjectError("invalid_request")

        def with_item(item_id: str, **changes) -> Project:
            item = project.implementation(item_id)
            if item is None:
                raise ProjectError("implementation_not_found")
            items = tuple(replace(i, **changes) if i.id == item_id else i for i in project.implementations)
            return replace(project, implementations=items)

        if op == "rename":
            keys("name")
            if not isinstance(m["name"], str) or not m["name"]:
                raise ProjectError("invalid_request")
            return replace(project, name=m["name"])
        if op == "set_working_copy":
            keys("path")
            directory = Path(m["path"]) if isinstance(m["path"], str) else None
            if directory is None or not directory.is_absolute() or not directory.is_dir():
                raise ProjectError("working_copy_unavailable")
            return replace(project, working_copy=replace(project.working_copy, path=str(directory), git=_git_present(directory)))
        if op == "set_root":
            keys("materials_root")
            return replace(project, materials_root=_relative(m["materials_root"]))
        if op == "set_exclude":
            keys("exclude")
            if not isinstance(m["exclude"], list):
                raise ProjectError("invalid_request")
            return replace(project, exclude=tuple(_relative(pattern) for pattern in m["exclude"]))
        if op == "add_implementation":
            keys("implementation")
            existing = frozenset(item.id for item in project.implementations)
            added = self._implementations([m["implementation"]], existing=existing)[0]
            return replace(project, implementations=project.implementations + (added,))
        if op == "rename_implementation":
            keys("implementation", "name")
            if not isinstance(m["name"], str) or not m["name"]:
                raise ProjectError("invalid_request")
            return with_item(m["implementation"], name=m["name"])
        if op == "move_implementation":
            keys("implementation", "place")
            return with_item(m["implementation"], place=_relative(m["place"], allow_absolute=True))
        if op == "set_package":
            keys("implementation", "package_entry")
            value = m["package_entry"]
            return with_item(m["implementation"], package_entry=None if value is None else _relative(value))
        if op == "archive_implementation":
            keys("implementation", "reason")
            if m["reason"] is not None and not isinstance(m["reason"], str):
                raise ProjectError("invalid_request")
            return with_item(m["implementation"], archived={"at": self._now(), "reason": m["reason"]})
        if op == "archive":
            keys("reason")
            if m["reason"] is not None and not isinstance(m["reason"], str):
                raise ProjectError("invalid_request")
            return replace(project, archived={"at": self._now(), "reason": m["reason"]})
        raise ProjectError("invalid_request")
