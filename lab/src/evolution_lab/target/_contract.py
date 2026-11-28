"""The neutral turn contract an agent implements to become a Lab target (ADR-0013)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, Union

from evolution.tree import ArtifactPolicyV1, Message, Usage
from evolution.tree.execution.wire_v2 import validate_turn_provenance, validate_turn_reason

SeedMode = Literal["exact", "unpaired", "none"]
#: A session state (spec §3.1): what `json.loads` can return. Floats must be
#: finite -- `_backend` checks every state with core's `canonical_body`, which
#: refuses NaN and the infinities as well as anything JSON cannot encode.
#: Spelled with `Union` and string forward references because the Python floor
#: is 3.11 (no `type` statement); `typing.get_type_hints` resolves it.
JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]
#: The three turn outcomes of core a16 (CORE-W19-01); `AgentTurn`'s
#: docstring says which one to report when.
OUTCOMES = frozenset({"ok", "candidate_failed", "target_failed"})

# Lab's neutral vocabulary for what the agent did on an `ok` turn (PDR-0019). A template declares it as
# `reason=TurnReason(AGENT_OUTCOME_CATEGORY, code)`; surviving `ok` turns carry it unchanged. Only the probe reads it.
AGENT_OUTCOME_CATEGORY = "agent_outcome"
AGENT_OUTCOMES = frozenset({"answered", "off_topic", "handoff", "gave_up"})


def component(rendered: bytes | str, sources: Sequence[str]) -> dict[str, Any]:
    """Describe rendered bytes and their candidate-relative source paths."""
    data = rendered.encode("utf-8") if isinstance(rendered, str) else rendered
    return {"sha256": sha256(data).hexdigest(), "sources": list(sources)}


class _Keep:
    """Sentinel: keep the session's previous state (a failed turn returns no state)."""

    _instance: _Keep | None = None

    def __new__(cls) -> _Keep:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "KEEP"


KEEP = _Keep()


class TargetConfigurationError(Exception):
    """The process configuration is broken and every sample would fail: a fault."""


@dataclass(frozen=True)
class AgentView:
    """Read-only projection of core's `LoadedTree`; `root` is the server's snapshot."""

    root: Path
    candidate_digest: str
    dependency_digest: str
    policy: ArtifactPolicyV1
    seed_mode: SeedMode


@dataclass(frozen=True)
class TurnReason:
    """Why a turn ended as it did, as two short codes (CORE-W19-02).

    Core carries it to the scorer and the editor on any outcome and concludes
    nothing from it. Both parts follow core's reason grammar
    ``[A-Za-z0-9._-]{1,64}``: a code, never exception text.
    """

    category: str
    code: str

    def __post_init__(self) -> None:
        try:
            validate_turn_reason({"category": self.category, "code": self.code})
        except ValueError as exc:
            raise ValueError("turn reason category and code must match [A-Za-z0-9._-]{1,64}") from exc

    def to_payload(self) -> dict[str, str]:
        return {"category": self.category, "code": self.code}


@dataclass(frozen=True)
class TurnProvenance:
    """Artifact paths or component identifiers used during a turn."""

    used: Sequence[str]

    def __post_init__(self) -> None:
        try:
            if isinstance(self.used, (str, bytes)):
                raise ValueError("used must be a sequence of identifiers")
            used = tuple(self.used)
            validate_turn_provenance({"used": list(used)})
        except (TypeError, ValueError) as exc:
            raise ValueError("turn provenance used must contain valid Core identifiers") from exc
        object.__setattr__(self, "used", used)

    def to_payload(self) -> dict[str, list[str]]:
        return {"used": list(self.used)}


@dataclass(frozen=True)
class AgentTurn:
    """One turn's answer. `outcome` is one of `OUTCOMES` (ADR-0001):

    - ``ok`` -- the agent answered;
    - ``candidate_failed`` -- the candidate is at fault: a *deterministic*
      defect the agent would reproduce on the same inputs (a broken reply
      format, an exhausted internal step limit). The sample counts against
      the candidate and is never retried;
    - ``target_failed`` -- the target could not answer: the sample is
      indeterminate and costs no score. An agent-internal timeout is
      ``target_failed``, never ``candidate_failed``: choosing a deadline does
      not make wall-clock time deterministic. When the agent cannot tell a
      deterministic defect from a timeout or a flaky dependency, it reports
      ``target_failed``.

    The backend defaults to reclassifying `ok` with `text.strip() == ""` as
    `target_failed`, replacing `reason` with `TurnReason("lab", "empty_answer")`
    before fitting the reply. Events, usage and state handling are preserved;
    agent failures are unchanged. `backend(..., empty_answer="ok")` opts out.

    `reason` and `provenance` are optional on any outcome. Provenance is the
    last field so earlier positional construction keeps its meaning.
    """

    outcome: str
    text: str
    events: tuple[Mapping[str, Any], ...] = ()
    state: JsonValue | None | _Keep = KEEP
    usage: Usage | None = None
    truncated: bool = False
    reason: TurnReason | None = None
    provenance: TurnProvenance | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown turn outcome: {self.outcome!r}")
        if not isinstance(self.text, str):
            raise ValueError("turn text must be a string")
        events = tuple(dict(event) for event in self.events)
        if any(not isinstance(event.get("type"), str) or not event["type"] for event in events):
            raise ValueError("every event needs a non-empty string type")
        object.__setattr__(self, "events", events)
        if self.usage is not None and not isinstance(self.usage, Usage):
            raise ValueError("usage must be a core Usage")
        if type(self.truncated) is not bool:
            raise ValueError("truncated must be a bool")
        if self.reason is not None and not isinstance(self.reason, TurnReason):
            raise ValueError("reason must be a TurnReason")
        if self.provenance is not None and not isinstance(self.provenance, TurnProvenance):
            raise ValueError("provenance must be a TurnProvenance")


class TurnAgent(Protocol):
    """An agent may also expose synchronous ``components() -> Mapping[str, Any]``.

    The optional method is discovered structurally, called once after describe
    on the instance loop, and must return Core's component map.
    """
    def describe(self) -> Mapping[str, Any]:
        """How this instance assembled itself from the loaded Candidate: a
        configuration hash, the source of its settings, its hermeticity mode,
        where it passed the seed -- whatever is worth carrying to core's
        journal, never anything that decides a candidate's score.

        Called once, synchronously, on the instance loop right after this
        agent is constructed (spec §3.2); its result is captured then and
        handed to core's `DescribingInstance.describe()` unchanged (Lab never
        calls this method again).

        Narrowed to core's shape (CORE-W19-03): a **flat** object of scalars
        (``str | int | float (finite) | bool | None``), keys matching the
        identifier grammar ``[A-Za-z0-9._-]{1,128}``, at most 32 keys and at
        most 4096 bytes of canonical JSON. `_backend` checks this with core's
        own `evolution.tree.execution.wire_v2.validate_instance_description` and fails
        the load with a `BackendError` on any violation -- nesting included,
        since there is no second level.

        Keeping launch secrets out of the returned mapping is this agent's
        obligation: `_backend`'s literal-secret scan is a backstop, not a
        substitute, and neither Lab nor core knows which values are secret
        except by the literal values the launcher declared.
        """
        ...

    async def open(self, public: Mapping[str, Any], *, session_id: str, seed: int | None) -> JsonValue | None: ...

    async def turn(self, message: Message, state: JsonValue | None, *, session_id: str, seed: int | None) -> AgentTurn: ...

    async def close_session(self, session_id: str) -> None: ...

    async def aclose(self) -> None: ...


AgentFactory = Callable[[AgentView], Awaitable[TurnAgent]]


@dataclass(frozen=True)
class LaunchContext:
    """What the Lab launcher hands `build`: the declared environment, split."""

    env: Mapping[str, str]
    secrets: Mapping[str, str]
    work_directory: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))
