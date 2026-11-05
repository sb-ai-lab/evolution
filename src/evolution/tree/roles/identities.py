"""Declared role binding identity values."""

from dataclasses import dataclass

from evolution.tree.execution.journal_identity import _identifier


@dataclass(frozen=True)
class EditorIdentity:
    """The `{kind, digest}` an in-process or subprocess `CandidateEditor` attests."""

    kind: str
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.kind)
        _identifier(self.digest)


@dataclass(frozen=True)
class ReflectorIdentity:
    """The `{kind, digest}` an in-process or subprocess `CandidateReflector` attests."""

    kind: str
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.kind)
        _identifier(self.digest)
