"""Durable journal references and their closed payload validation contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from evolution.tree.storage.envelope import SchemaError, _identifier, _keys

EVIDENCE_KINDS = frozenset({"journal_record", "journal_prefix", "ledger_record"})

_SHA256 = re.compile("[a-f0-9]{64}")

_LOCATOR = re.compile(r"(?P<file>[^#]+)#bytes=(?P<start>0|[1-9][0-9]*):(?P<end>0|[1-9][0-9]*)\Z")


def _refuse(ok: object, rule: str) -> None:
    if not ok:
        raise SchemaError(rule)


def _sha(value: object, rule: str = "value is not a sha-256 digest") -> None:
    _refuse(isinstance(value, str) and _SHA256.fullmatch(value) is not None, rule)


@dataclass(frozen=True)
class DurableReference:
    """The S1-shaped reference an E record carries as its proof (spec §5)."""

    version: int
    kind: str
    run_id: str
    locator: str
    content_digest: str

    def __post_init__(self) -> None:
        _refuse(type(self.version) is int and self.version == 1, "reference version is 1")
        _refuse(self.kind in EVIDENCE_KINDS, "reference kind is not in the registry")
        _identifier(self.run_id)
        _validate_locator(self.locator)
        _sha(self.content_digest, "reference content_digest is not a sha-256 digest")

    def to_payload(self) -> dict[str, Any]:
        return dict(
            version=self.version,
            kind=self.kind,
            run_id=self.run_id,
            locator=self.locator,
            content_digest=self.content_digest,
        )

    @classmethod
    def from_payload(cls, obj: Any) -> DurableReference:
        try:
            value = _keys(obj, {"version", "kind", "run_id", "locator", "content_digest"})
        except ValueError as exc:
            raise SchemaError("reference payload keys") from exc
        return cls(
            version=value["version"],
            kind=value["kind"],
            run_id=value["run_id"],
            locator=value["locator"],
            content_digest=value["content_digest"],
        )


def _validate_locator(value: object) -> None:
    """`<file>#bytes=<start>:<end>`, relative, printable and never a path out."""

    _refuse(isinstance(value, str), "locator is not a string")
    assert isinstance(value, str)
    _refuse(0 < len(value) <= 512, "locator length")
    _refuse(all(0x20 <= ord(char) <= 0x7E for char in value), "locator is not printable ASCII")
    match = _LOCATOR.fullmatch(value)
    _refuse(match is not None, "locator does not follow the byte-range grammar")
    assert match is not None
    name = match["file"]
    _refuse(not name.startswith("/") and ".." not in name.split("/"), "locator escapes the run")
    _refuse(int(match["start"]) <= int(match["end"]), "locator byte range is inverted")
