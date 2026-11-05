"""Family 14's role visibility: which artifact paths the Reflector and Editor see.

ADR-0029 owns this boundary. The
*effective set* is `role_visibility.readable_paths ∪ artifacts.writable_paths`,
matched with the artifact grammar (`artifact_policy.matches`), the one matcher
`admit` and the seal also use, so every path admission accepts as writable is
visible by construction. Artifact exclusions still win, because an excluded
path never reaches the store copy a projection is captured from.

A file the artifact includes but that lies outside the effective set is
*hidden*: it stays in the Target's tree and in artifact identity, and never in
a role projection. Directories are not matched; they appear in a projection
only as ancestors of visible files (`workspace.visible_capture`).

The same set filters the repository evidence the controller generates for
either role (ADR-0029): `visible_paths` for the changed-path lists of
Attempt feedback and history, and `visible_observation` for the `skill_read`
events of a role-rendered observation. Filtering is silent by contract: the caller drops hidden members before it caps, diffs or excerpts,
so every `truncated` flag still means "the visible list hit its cap".

This module holds no I/O. Families up to 13 have no `ConnectionContract` and
therefore no view (`RoleVisibilityView.for_campaign` returns `None`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from evolution.tree.artifacts.artifact_policy import matches

if TYPE_CHECKING:
    from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
    from evolution.tree.target.connection_contract import ConnectionContract


@dataclass(frozen=True)
class RoleVisibilityView:
    """The effective set of one family-14 campaign, as a path predicate."""

    readable_paths: tuple[str, ...]
    writable_paths: tuple[str, ...]

    def visible(self, path: str) -> bool:
        """Whether a relative POSIX file path is in `readable_paths ∪ writable_paths`."""

        relative = PurePosixPath(path)
        return any(
            matches(rule, relative, False) for rule in (*self.readable_paths, *self.writable_paths)
        )

    @classmethod
    def for_campaign(
        cls, connection: ConnectionContract | None, artifact_policy: ArtifactPolicyV1
    ) -> RoleVisibilityView | None:
        """The view a campaign's roles get, or `None` for a family without a connection."""

        if connection is None:
            return None
        return cls(
            readable_paths=tuple(connection.role_visibility.readable_paths),
            writable_paths=tuple(artifact_policy.writable_paths),
        )


def visible_paths(
    paths: Sequence[str], visible: Callable[[str], bool], files: Iterable[str]
) -> tuple[str, ...]:
    """The members of a changed-path list a role may see, in their input order.

    `files` is every regular-file path of *both* trees the list compares. A
    member is kept when it is a visible file, or when a visible file lies
    beneath it in either tree: a directory that only leads to visible files may
    appear, an empty or hidden-only one may not. So a hidden file that is
    created, rewritten, re-moded, deleted or moved never appears, nor does a
    directory that holds nothing visible; a file moved from a hidden path into
    the visible set appears at its new path only.
    """

    shown = {path for path in files if visible(path)}
    ancestors = {
        parent.as_posix()
        for path in shown
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    return tuple(path for path in paths if path in shown or path in ancestors)


def visible_observation(event: Mapping[str, Any], visible: Callable[[str], bool]) -> bool:
    """Whether one projected observation event may be rendered to a role.

    Only a `skill_read` event carries a repository path the controller renders
    as one (`editor_evidence.shown_event`); it is shown only when that path is
    visible. Every other event is Target log text -- an `assistant_message`
    content, a tool's identifier name and status -- which PDR-0023 leaves
    uncensored even when it spells a hidden path.
    """

    if event.get("type") != "skill_read":
        return True
    path = event.get("path")
    return isinstance(path, str) and visible(path)
