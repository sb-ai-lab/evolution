"""Closed project failures (stage ``project``)."""
from __future__ import annotations

_MESSAGES = {
    "tree_not_found": "The project folder does not exist or is empty.",
    "tree_excluded": "The project folder is excluded.",
    "invalid_request": "The project request is invalid.",
    "invalid_id": "The identifier is invalid.",
    "id_taken": "The identifier is already taken.",
    "project_not_found": "The project was not found.",
    "implementation_not_found": "The implementation was not found.",
    "implementation_archived": "The implementation is archived.",
    "project_not_ready": "The project is not ready.",
    "archived": "The project or implementation is archived.",
    "active_operation": "An active operation uses this project or implementation.",
    "conflict": "The project record changed since it was read.",
    "ambiguous_implementation": "The project has several active implementations; name one.",
    "working_copy_unavailable": "The working copy is unavailable.",
    "package_unavailable": "The implementation package entry is unavailable.",
    "package_mismatch": "The package does not cover the implementation place.",
    "not_linked": "The object is not linked to this project and implementation.",
    "inputs_changed": "The project inputs changed during preparation.",
    "link_failed": "The project link could not be recorded; repeat the request.",
    "digest_mismatch": "The confirmed bytes differ from the current bytes, or the acceptance state differs from the expected one.",
    "reserved_claim": "The claim name is reserved for a computed fact.",
    "not_acceptable": "The facts cannot be accepted.",
    "clone_failed": "The repository clone failed.",
    "not_a_clone": "The project's working copy is not a Lab clone in its place; there is nothing to sync.",
    "sync_fetch_failed": "Fetching from the project's source failed.",
    "revision_not_found": "The source revision was not found.",
    "history_diverged": "The source no longer contains the project's base revision; its history was rewritten.",
    "branch_mismatch": "The working copy is not on the project's Lab branch.",
    "working_copy_dirty": "The working copy has changes to tracked files or an unfinished merge or rebase.",
    "merge_conflict": "Merging the source commit conflicts with the working copy; nothing was changed.",
    "merge_failed": "Merging the source commit failed.",
    "upstream_changed": "The source commit is not in the working copy; run the sync check again.",
    "sync_not_recorded": "The source commit was merged but the base revision was not recorded; repeat the sync with the same commit.",
    "git_unavailable": "git is not available to Lab.",
    "port_destination_exists": "The port destination exists and is not empty.",
    "port_into_working_copy": "The port destination is inside the project's working copy.",
    "port_into_lab_home": "The port destination is inside the Lab home directory.",
    "template_not_git": "The template is not a git repository.",
    "template_ref_not_found": "The template revision was not found.",
    "template_fetch_failed": "The template could not be fetched.",
    "port_failed": "The port repository could not be created.",
    "home_invalid": "The Lab home directory is invalid.",
    "home_busy": "Another Lab process writes this Lab home, or this handle cannot write it.",
    "registry_newer": "The project registry was written by an incompatible newer Lab.",
    "registry_invalid": "The project registry is unreadable.",
    "unsupported_schema": "The schema is not supported.",
    "target_undeclared": "The target is not declared; add lab-target-entry.json to the implementation place or connect a package.",
    "target_entry_invalid": "The target entry file is invalid.",
    "probe_session_closed": "The probe session is closed.",
    "probe_session_not_found": "The probe session does not exist.",
    "probe_ceiling_reached": "The probe session reached its ceiling.",
    "probe_ceiling_unverifiable": "The probe session ceiling cannot be verified.",
    "probe_limit_reached": "The probe limit is reached.",
    "target_not_ready": "The target is not ready.",
}
CODES = frozenset(_MESSAGES)


class ProjectError(ValueError):
    """A closed project failure; its payload never carries request data. ``detail``, appended to the message, is
    used by ``port_destination_exists`` (only Lab's own default destination ``LAB_HOME/ports/<project>/<port-id>``,
    spec 2026-10-01 §3.4; an ``--into`` path is the caller's, never echoed) and by ``target_entry_invalid`` (only
    a known field name of ``lab-target-entry/1`` or the fixed ``unknown key``, never input text). ``detail`` is
    also carried by ``working_copy_dirty`` (paths of the copy), ``merge_conflict`` (conflicting paths) and
    ``merge_failed`` (a fixed text about the copy's state): the paths belong to Lab's clone, not to request input."""

    stage = "project"

    def __init__(self, code: str, detail: str | None = None) -> None:
        if code not in _MESSAGES:
            raise ValueError("invalid project failure")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 4096):
            raise ValueError("invalid project failure detail")
        super().__init__(code)
        self.code = code
        self.detail = detail

    @property
    def message(self) -> str:
        return _MESSAGES[self.code] if self.detail is None else f"{_MESSAGES[self.code]} {self.detail}"

    @property
    def failure(self) -> "ProjectError":
        return self

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage, "message": self.message}
