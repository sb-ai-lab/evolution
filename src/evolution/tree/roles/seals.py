"""Seals over a Candidate editor's declared configuration, and its identity.

Digests two kinds of bytes -- a system prompt (packaged or file-declared) and,
for an `agent_cli` editor, the resolved CLI executable -- plus the declared
`ChatEditorSection` / `AgentCliEditorSection` fields, into one
`EditorIdentity(kind, digest)` from declared inputs and sealed bytes. What enters the digest:

* every scalar field the section declares (model, base_url, permission,
  reasoning_effort, the three budgets,...);
* environment variables by **name** (`api_key_env`, `proxy_env`,
  `allowed_env`), never by value;
* the prompt's bytes, as a SHA-256 digest, never the path that named it;
* for `agent_cli`, the CLI executable's bytes, as a SHA-256 digest under
  `program_sha256`, never the path that resolved to it;
* the chat tool schema version (`CHAT_TOOL_SCHEMA`) and the brief version
  (`BRIEF_VERSION`), so a tool-surface or brief-shape change is a new
  identity.

What never enters the digest: any environment variable's *value* (the key,
proxy or forwarded credentials), the prompt or CLI *path*, what a trusted CLI
loads at run time (its own configuration files, interpreters or libraries),
the observed provider or model of a response, and the run directory. This is
PDR-0003's boundary (the identity records declared program bytes and
declared environment names, never a value) applied through PDR-0005 (a
binding seals what the manifest names or resolves from it; what a trusted
program does once running is the operator's trust, not a provable property).
A CLI upgrade between runs therefore changes the identity and a resume is
refused with `run_identity_changed`, exactly as replacing the interpreter
named by a `subprocess` editor's `argv[0]` is; rotating a forwarded token or
moving the same CLI binary to a new path does not.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolution.tree.execution.command_binding import file_digest
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.config import AgentCliEditorSection, ChatEditorSection
from evolution.tree.roles.editor.editor_brief import BRIEF_VERSION
from evolution.tree.roles.editor_contracts import BRIEF_VERSION_V16
from evolution.tree.roles.identities import EditorIdentity, ReflectorIdentity
from evolution.tree.roles.reflector_contracts import (
    REFLECT_REQUEST_VERSION,
    REFLECT_TOOL_SCHEMA,
    REFLECT_TOOL_SCHEMA_V16,
)

CHAT_TOOL_SCHEMA = "chat_tools/1"
CHAT_TOOL_SCHEMA_V16 = "chat_tools/2"
"""The version of the chat editor's `TOOLS` list; part of the chat digest."""


class SealChanged(RuntimeError):
    """A sealed prompt's or program's bytes no longer match what was bound."""


@dataclass(frozen=True)
class FileSeal:
    """A digest over one file's bytes at bind time, or over a packaged text.

    `path` is `None` for a packaged prompt (`packaged_text` then holds the
    text the digest was taken over); the packaged text cannot change within a
    process, so re-verifying it is a constant comparison, not a re-read.
    """

    path: Path | None
    sha256: str
    packaged_text: str | None = None

    def verify(self) -> None:
        """Recompute the digest over the current bytes; raise on a mismatch.

        A vanished or unreadable sealed file is the largest difference there
        is, never a stray `OSError` for a caller to unpack (mirrors
        `CommandBinding.verify`).
        """

        if self.path is None:
            current = hashlib.sha256((self.packaged_text or "").encode()).hexdigest()
        else:
            try:
                current = file_digest(self.path)
            except OSError as exc:
                raise SealChanged(f"sealed file unavailable: {self.path.name}") from exc
        if current != self.sha256:
            raise SealChanged(str(self.path) if self.path is not None else "packaged prompt")

    def text(self) -> str:
        """The sealed text: the packaged text, or the file's current content."""

        if self.path is None:
            assert self.packaged_text is not None
            return self.packaged_text
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SealChanged(f"sealed file unavailable: {self.path.name}") from exc


def seal_prompt(path: Path | None, *, packaged: str) -> FileSeal:
    """Seal a declared prompt file, or the packaged default when none is set."""

    if path is None:
        return FileSeal(
            path=None, sha256=hashlib.sha256(packaged.encode()).hexdigest(), packaged_text=packaged
        )
    return FileSeal(path=path, sha256=file_digest(path))


def seal_program(path: Path) -> FileSeal:
    """Seal a resolved CLI executable's bytes."""

    return FileSeal(path=path, sha256=file_digest(path))


def section_payload(section: ChatEditorSection | AgentCliEditorSection) -> dict[str, Any]:
    """The declared section, digest-ready: paths dropped, names kept, sets sorted.

    `system_prompt` and `agent_bin` are paths, replaced elsewhere by their
    bytes' digest (`system_prompt_sha256`, `program_sha256`), so they are
    dropped here. `api_key_env`, `proxy_env` and `allowed_env` stay as
    environment variable *names*. `budgets` is a nested dataclass and
    `dataclasses.asdict` already renders it as a nested object.

    `retry` (`/11`, PDR-0019, ADR-0023) is `None` for every editor without a
    declared retry policy -- dropped entirely, so a pre-`/11` chat editor's
    digest is unaffected -- and otherwise replaced with `RoleCallRetry.to_payload()`'s
    canonical `role_call_retry/1` shape, not `dataclasses.asdict`'s flat
    rendering of the dataclass's own fields.

    `api_key_env` (ADR-0023) is the same: a chat section
    without a declared key is left out of the payload, never written as
    `null`, so a section that declares its key keeps its identity digest.
    """

    payload = dataclasses.asdict(section)
    payload.pop("system_prompt", None)
    payload.pop("agent_bin", None)
    for name in (
        "max_protocol_repairs",
        "max_consecutive_tool_failures",
        "provider_order",
        "allow_fallbacks",
        "require_parameters",
        "nearest_paths",
    ):
        if payload.get(name) is None:
            payload.pop(name, None)
    if "api_key_env" in payload and payload["api_key_env"] is None:
        del payload["api_key_env"]
    if payload.get("retry") is None:
        payload.pop("retry", None)
    else:
        assert isinstance(section, ChatEditorSection) and section.retry is not None
        payload["retry"] = section.retry.to_payload()
    for key, value in list(payload.items()):
        if isinstance(value, frozenset):
            payload[key] = sorted(value)
        elif isinstance(value, tuple):
            payload[key] = list(value)
    return payload


def chat_identity(
    section: ChatEditorSection,
    prompt: FileSeal,
    *,
    continuation_sha256: str | None = None,
    family15: bool = False,
    plan_correction: bool = False,
) -> EditorIdentity:
    """The declared-configuration identity of a `chat` editor.

    `continuation_sha256` (`/11`, PDR-0019, ADR-0023) is the SHA-256 of
    `tree_editor_continuation.md`'s bytes, present only for a family-10 run
    (`role_resilience=True`); it is left out of the hashed payload entirely
    when `None`, so every earlier family's digest is unaffected by the
    continuation text existing at all.
    """

    payload: dict[str, Any] = {
        "kind": "chat",
        "section": section_payload(section),
        "system_prompt_sha256": prompt.sha256,
        "tool_schema": CHAT_TOOL_SCHEMA_V16 if family15 else CHAT_TOOL_SCHEMA,
        "brief_version": BRIEF_VERSION_V16 if family15 else BRIEF_VERSION,
    }
    if continuation_sha256 is not None:
        payload["continuation_sha256"] = continuation_sha256
    if plan_correction:
        payload["record_plan"] = "plan_correction/1"
    if family15:
        payload["role_contracts"] = "role_contracts/1"
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return EditorIdentity(kind="chat", digest=digest)


def agent_identity(
    section: AgentCliEditorSection,
    prompt: FileSeal,
    program: FileSeal,
    *,
    continuation_sha256: str | None = None,
    family15: bool = False,
) -> EditorIdentity:
    """The declared-configuration identity of an `agent_cli` editor.

    `continuation_sha256`: see `chat_identity`.
    """

    payload: dict[str, Any] = {
        "kind": "agent_cli",
        **({"tool_schema": CHAT_TOOL_SCHEMA_V16} if family15 else {}),
        "section": section_payload(section),
        "system_prompt_sha256": prompt.sha256,
        "program_sha256": program.sha256,
        "brief_version": BRIEF_VERSION_V16 if family15 else BRIEF_VERSION,
    }
    if continuation_sha256 is not None:
        payload["continuation_sha256"] = continuation_sha256
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return EditorIdentity(kind="agent_cli", digest=digest)


def chat_reflector_identity(
    section: ChatEditorSection,
    prompt: FileSeal,
    *,
    result_repair: bool = False,
    family15: bool = False,
) -> ReflectorIdentity:
    """The declared-configuration identity of a `chat` reflector.

    Same shape as `chat_identity`, but hashed under the reflector's own
    versions (`REFLECT_TOOL_SCHEMA`, `REFLECT_REQUEST_VERSION`) rather than the
    editor's, so a chat editor and a chat reflector configured identically
    still attest different identities.

    `result_repair` (`/16`, ADR-0048) adds one key for the in-role
    `finish_reflection` check and its tool description, so the identity
    changes even under an operator-declared prompt; without it every byte of
    the hashed object, and so every `/15` identity, is unchanged.
    """

    digest = hashlib.sha256(
        canonical_json(
            {
                "kind": "chat",
                "section": section_payload(section),
                "system_prompt_sha256": prompt.sha256,
                "tool_schema": REFLECT_TOOL_SCHEMA_V16 if family15 else REFLECT_TOOL_SCHEMA,
                "request_version": REFLECT_REQUEST_VERSION,
                **({"reflect_brief": "reflect_brief/6"} if family15 else {}),
                **({"role_contracts": "role_contracts/1"} if family15 else {}),
                **({"finish_reflection": "result_repair/1"} if result_repair else {}),
            }
        )
    ).hexdigest()
    return ReflectorIdentity(kind="chat", digest=digest)


def agent_reflector_identity(
    section: AgentCliEditorSection, prompt: FileSeal, program: FileSeal, *, family15: bool = False
) -> ReflectorIdentity:
    """The declared-configuration identity of an `agent_cli` reflector."""

    digest = hashlib.sha256(
        canonical_json(
            {
                "kind": "agent_cli",
                "section": section_payload(section),
                "system_prompt_sha256": prompt.sha256,
                "program_sha256": program.sha256,
                "request_version": REFLECT_REQUEST_VERSION,
                **({"reflect_brief": "reflect_brief/6"} if family15 else {}),
            }
        )
    ).hexdigest()
    return ReflectorIdentity(kind="agent_cli", digest=digest)


def chat_tool_schema_for(pin: EvidencePin | None) -> str:
    return CHAT_TOOL_SCHEMA if pin is None else CHAT_TOOL_SCHEMA_V16
