"""Safe exceptions at the native journal/component boundary (no runtime imports)."""

from typing import Literal

JournalCode = Literal[
    "storage_capacity_exhausted", "journal_limit_exceeded", "journal_unavailable", "journal_busy"
]


class JournalBoundaryError(Exception):
    """Must bypass broad component exception handlers."""

    def __init__(self, code: JournalCode = "journal_unavailable") -> None:
        if code not in (
            "storage_capacity_exhausted",
            "journal_limit_exceeded",
            "journal_unavailable",
            "journal_busy",
        ):
            raise ValueError("invalid journal diagnostic")
        self.code = code
        self.durable = False
        super().__init__(code)


class JournalFault(JournalBoundaryError):
    """An unusable writer; only reopening the durable prefix can authorize work."""


class JournalAdmissionRefused(JournalBoundaryError):
    """A healthy writer retains reserved terminal/cleanup capacity."""

    def __init__(self) -> None:
        super().__init__("journal_limit_exceeded")


class JournalRecoveryRequired(JournalBoundaryError):
    """Healthy owner must resolve its durable prefix before dispatching new work."""

    def __init__(self) -> None:
        super().__init__("journal_busy")
