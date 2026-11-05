"""Typed S2 boundary errors; codes are closed and safe to persist."""

from __future__ import annotations

from evolution.tree.execution.candidate_integrity import ArtifactError


class ArtifactUnsafeError(ArtifactError):
    """A tree or destination violates the safe-tree or containment contract."""

    CODES = frozenset(
        {
            "symlink",
            "special_file",
            "hard_link",
            "dependency_missing",
            "escape",
            "destination_exists",
            "foreign_owner",
            "artifact_unsafe",
            "source_changed",
        }
    )


class ArtifactCapacityError(ArtifactError):
    CODES = frozenset(
        {
            "storage_capacity_insufficient",
            "storage_capacity_exhausted",
            "storage_inodes_insufficient",
        }
    )

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        super().__init__(code)
        #: A path-free line for stderr and the process log (ADR-0040 D8), or `None`.
        self.detail = detail


class ArtifactCorruptionError(ArtifactError):
    CODES = frozenset(
        {
            "artifact_digest_mismatch",
            "artifact_missing",
            "artifact_unsafe",
            "seed_mismatch",
            "policy_mismatch",
        }
    )


class ArtifactAdmissionRefused(ArtifactError):
    """The store refuses new work: stopped, recovery required or wrong state."""


class ArtifactStoreStopped(ArtifactAdmissionRefused):
    pass
