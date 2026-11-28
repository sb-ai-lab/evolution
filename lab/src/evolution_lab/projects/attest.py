"""Confirmations and acceptance of changes (spec §3.7-§3.8)."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .errors import ProjectError
from .facts import ACCEPTABLE_CODES, COMPUTED, OPERATION_FACTS
from .inventory import read_regular_file
from .models import Acceptance, Confirmation, Fact
from .registry import utc_now

CLAIM = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\Z")
_LINK_KINDS = {"file", "place", "revision", "catalogue", "tree"}


def _file_digest(path: Path) -> str:
    """A missing, unreadable or non-regular file (FIFO, device: never opened for a blocking read) has no digest."""
    try:
        return "sha256:" + hashlib.sha256(read_regular_file(path)).hexdigest()
    except OSError:  # NotRegularFile included
        raise ProjectError("invalid_request") from None


def _safe_relative(value: object) -> str:
    """A materials-root-relative path with no ``..`` segments and no absolute form.

    Shared by ``attests.path`` and any ``linked`` item of kind ``file`` (fix round 1, I3):
    without this, a ``file`` link's ``ref`` was hashed unvalidated, so ``../x`` or an absolute
    path like ``/etc/passwd`` would be read straight off disk.
    """
    if not isinstance(value, str) or not value or ".." in Path(value).parts or Path(value).is_absolute():
        raise ProjectError("invalid_request")
    return value


def _catalogue(ref: str) -> str:
    if ref == "requirements":
        from evolution_lab.guides import requirements
        return requirements()["digest"]
    if ref == "scenarios":
        from evolution_lab.guides import scenarios
        return scenarios().catalogue["digest"]
    raise ProjectError("invalid_request")


def current_digests(projects, project, implementation, revision, *, attests_path: str, linked: Iterable[Mapping]) -> dict:
    root = projects.root_path(project)
    result = {"attests": _file_digest(root / attests_path), "linked": []}
    for item in linked:
        kind, ref = item.get("kind"), item.get("ref")
        if kind not in _LINK_KINDS or not isinstance(ref, str):
            raise ProjectError("invalid_request")
        if kind == "file":
            digest = _file_digest(root / _safe_relative(ref))
        elif kind == "place":
            if ref == "materials_root":
                digest = revision.place_digest("materials_root")
            elif ref.startswith("implementation:"):
                digest = revision.place_digest("implementation", ref.split(":", 1)[1])
            else:
                raise ProjectError("invalid_request")
            if digest is None:
                raise ProjectError("implementation_not_found")
        elif kind == "tree":
            from .tree import excluded, parse_ref, tree_digest, tree_entries
            implementation_id, relpath = parse_ref(ref)
            if excluded(project, implementation_id, relpath) is not None:
                digest = None
            else:
                entries = tree_entries(project, revision, implementation_id, relpath)
                digest = None if entries is None else tree_digest(entries)
        elif kind == "revision":
            digest = revision.digest
        else:
            digest = _catalogue(ref)
        result["linked"].append({"kind": kind, "ref": ref, "digest": digest})
    return result


def make_confirmation(projects, project, implementation, *, claim, attests: Mapping, linked: Sequence[Mapping], channel, author,
                      note, revision) -> Confirmation:
    """Validate and measure a confirmation before its caller opens a write transaction."""
    # Reserved names (spec §3.7) are checked first: some computed facts ("connected",
    # "campaigns") are single words that also fail CLAIM's dotted-namespace shape, and a
    # client confirming one of those wants "this name is reserved", not "malformed claim".
    if isinstance(claim, str) and claim in COMPUTED:
        raise ProjectError("reserved_claim")
    if not isinstance(claim, str) or not CLAIM.match(claim):
        raise ProjectError("invalid_request")
    for value in (channel, author):
        if not isinstance(value, str) or not value:
            raise ProjectError("invalid_request")
    path = _safe_relative(attests.get("path") if isinstance(attests, Mapping) else None)
    now = current_digests(projects, project, implementation, revision, attests_path=path, linked=linked)
    if attests.get("digest") not in (None, now["attests"]):
        raise ProjectError("digest_mismatch")
    for asked, actual in zip(linked, now["linked"]):
        if asked.get("kind") == "tree":
            from .tree import excluded, parse_ref
            implementation_id, relpath = parse_ref(asked.get("ref"))
            reason = excluded(project, implementation_id, relpath)
            if reason is not None:
                raise ProjectError("tree_excluded", reason)
            if actual["digest"] is None:
                raise ProjectError("tree_not_found")
        if asked.get("digest") not in (None, actual["digest"]):
            raise ProjectError("digest_mismatch")
    confirmation = Confirmation("c-" + secrets.token_hex(8), project.id,
                                None if implementation is None else implementation.id, claim,
                                {"path": path, "digest": now["attests"]}, tuple(now["linked"]), channel, author,
                                note, utc_now(), None)
    return confirmation


def confirm(projects, project, implementation, *, claim, attests: Mapping, linked: Sequence[Mapping], channel, author,
            note, revision) -> Confirmation:
    confirmation = make_confirmation(projects, project, implementation, claim=claim, attests=attests, linked=linked,
                                     channel=channel, author=author, note=note, revision=revision)
    projects.store.put_confirmation(confirmation)
    return confirmation


def _digest_missing(fact: Fact) -> bool:
    """A confirmation whose current state lacks a digest (the attested file is gone, a linked
    place is unknown, ...) has nothing to accept: that state is not "the bytes now" (final
    review I2)."""
    if (fact.source or {}).get("kind") != "confirmation":
        return False
    state = fact.state or {}
    return state.get("attests") is None or any(digest is None for digest in state.get("linked") or ())


def plan_acceptance(facts: Sequence[Fact], *, only: Sequence[str] | None, across_pairs: bool) -> list[Fact]:
    stale = [f for f in facts if f.stale_reason is not None and f.value]
    eligible = [f for f in stale
                if (f.id in OPERATION_FACTS or (f.source or {}).get("kind") == "confirmation")
                and not _digest_missing(f)
                and set(f.stale_reason.codes) <= ACCEPTABLE_CODES
                and (across_pairs or "other_pair" not in f.stale_reason.codes)]
    if only is not None:
        wanted = set(only)
        if not wanted <= {f.id for f in eligible}:
            raise ProjectError("not_acceptable")
        eligible = [f for f in eligible if f.id in wanted]
    if not eligible:
        raise ProjectError("not_acceptable")
    return eligible


def _from(fact: Fact) -> dict:
    source = fact.source or {}
    return {"kind": source["kind"], "id": source["id"]}


def acceptance_entries(chosen: Sequence[Fact]) -> tuple[dict, ...]:
    return tuple({"id": f.id, "from": _from(f), "to": dict(f.state or {}), "codes": list(f.stale_reason.codes),
                  "changes": None if f.stale_reason.changes is None else f.stale_reason.changes.to_payload()}
                 for f in chosen)


def expected_matches(entries: Sequence[dict], expected: Sequence[dict]) -> bool:
    """Compare exact sets, including duplicate and unmatchable JSON values safely."""
    def states(rows):
        # Canonicalize the id too: the shallow request decoder permits JSON arrays/objects
        # there, which must mismatch rather than fail while building a hash set.
        return {tuple(json.dumps(row[key], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                      for key in ("id", "from", "to")) for row in rows}
    try:
        return states(entries) == states(expected)
    except (KeyError, TypeError, ValueError):
        return False


def accept(projects, project, implementation, *, facts, only, across_pairs, reason, channel, author, dry_run,
           inventories: Mapping, expected: Sequence[dict] | None = None) -> Acceptance:
    if not isinstance(reason, str) or not reason:
        raise ProjectError("invalid_request")
    try:
        chosen = plan_acceptance(facts, only=only, across_pairs=across_pairs)
    except ProjectError as error:
        if not dry_run and expected is not None and error.code == "not_acceptable":
            raise ProjectError("digest_mismatch") from None
        raise
    entries = acceptance_entries(chosen)
    if not dry_run and expected is not None and not expected_matches(entries, expected):
        raise ProjectError("digest_mismatch")
    acceptance = Acceptance(None if dry_run else "a-" + secrets.token_hex(8), project.id,
                            None if implementation is None else implementation.id, entries, bool(across_pairs),
                            reason, channel, author, utc_now(), None)
    if not dry_run:
        for digest, stored in inventories.items():
            projects.store.put_inventory(digest, stored)
        projects.store.put_acceptance(acceptance)
    return acceptance
