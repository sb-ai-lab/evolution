"""Reflector inputs, results and diagnosis admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION
from evolution.tree.execution.check_modes import (
    ENFORCE_ALL,
    CheckModes,
    HitSink,
    redact_for_journal,
)
from evolution.tree.execution.evidence_settings import EvidencePin
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.roles.editor.editor_brief import _case_evidence_id, _observation_evidence_id
from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2
from evolution.tree.roles.editor_contracts import note_digest as _note_digest
from evolution.tree.roles.identities import ReflectorIdentity
from evolution.tree.roles.role_call import AttemptRecorder
from evolution.tree.roles.role_failures import ADAPTIVE_GROUP_KEY_ORDER as ADAPTIVE_GROUP_KEY_ORDER
from evolution.tree.storage.envelope import secret_match

if TYPE_CHECKING:  # pragma: no cover - the annotation only
    from evolution.tree.roles.environment_docs import EnvironmentDocs
    from evolution.tree.roles.reflection_reader import ReflectionReadSession
    from evolution.tree.target.probe_session import ProbeSession


from evolution.tree.roles.limits import adaptive_limits

REFLECT_REQUEST_VERSION = "reflect_request/2"


# list_history/read_history (ADR-0039); chat Reflector identities change.
REFLECT_TOOL_SCHEMA = "reflect_tools/2"


MIN_NEEDLE_LENGTH = 8


_OBSERVATION_LIMIT_KEYS = (
    "turns",
    "events_per_turn",
    "text_chars",
    "input_chars",
    "feedback_keys",
    "feedback_value_chars",
)


REFLECT_LIMITS_V1: Mapping[str, int] = MappingProxyType(
    {
        "cohorts": 16,
        "groups": 8,
        "sentence_chars": 512,
        "verdicts": 32,
        "verdict_note_chars": 256,
        "history_rounds": 6,
        "environment_docs_chars": 60_000,
        **{key: BRIEF_LIMITS_V2[key] for key in _OBSERVATION_LIMIT_KEYS},
    }
)


# -- the family-14 Reflector's bounded opening history -------------------
# ADR-0039 owns these bounds. Every size below is
# measured over serialized canonical JSON (`canonical_json(...).decode()`), or
# over a string's JSON literal, never over raw characters.

#: The history cap for every Reflector kind.
HISTORY_MAX_CHARS: Final = 96_000


#: Detail rounds are the newest `REFLECT_LIMITS_V1["history_rounds"]`.
SUMMARY_WINDOW_ROUNDS: Final = 12


AGGREGATE_PATHS: Final = 5


AGGREGATE_CHANGES: Final = 4


AGGREGATE_CHANGE_WITNESSES: Final = 2


AGGREGATE_REASONS: Final = 10


AGGREGATE_PROMOTIONS: Final = 4


AGGREGATE_GAINS: Final = 4


AGGREGATE_RECENT_ROUNDS: Final = 3


AGGREGATE_PROMOTION_PATHS: Final = 3


AGGREGATE_ROUND_OUTCOMES: Final = 12


REPEAT_MIN_ROUNDS: Final = 2


PATH_LITERAL_MAX_CHARS: Final = 258


CHANGE_EXCERPT_MAX_CHARS: Final = 120


CHANGE_FINGERPRINT_SCHEME: Final = "reflect_change_fingerprint/1"


FINGERPRINT_MAX_LINES: Final = 1_000


FINGERPRINT_MAX_BYTES: Final = 65_536


ATTEMPT_CHANGES_MAX_CHARS: Final = 22_000


AGGREGATE_MAX_CHARS: Final = 50_000


AGGREGATE_REDUCED_MAX_CHARS: Final = 22_000


AGGREGATE_FULL_LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "paths": AGGREGATE_PATHS,
        "changes": AGGREGATE_CHANGES,
        "change_witnesses": AGGREGATE_CHANGE_WITNESSES,
        "reasons": AGGREGATE_REASONS,
        "promotions": AGGREGATE_PROMOTIONS,
        "gains": AGGREGATE_GAINS,
        "recent_rounds": AGGREGATE_RECENT_ROUNDS,
        "promotion_paths": AGGREGATE_PROMOTION_PATHS,
        "round_outcomes": AGGREGATE_ROUND_OUTCOMES,
    }
)


AGGREGATE_REDUCED_LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "paths": 3,
        "changes": 2,
        "change_witnesses": 2,
        "reasons": 4,
        "promotions": 3,
        "gains": 2,
        "recent_rounds": 3,
        "promotion_paths": 1,
        "round_outcomes": 12,
    }
)


#: Per-entry bounds of the compact detail entries.
COMPACT_TEXT_CHARS: Final = 256


COMPACT_TARGET_PATHS: Final = 8


COMPACT_CHANGED_PATHS: Final = 8


GROUP_KEYS = frozenset(
    {"root_cause", "suggested_fix", "evidence_ids", "affected_cases", "target_paths"}
)


GROUP_KEYS_WITH_PROBES = GROUP_KEYS | {"probe_refs"}


@dataclass(frozen=True)
class ProbeAccess:
    url: str | None
    budget: int
    methods: tuple[str, ...]
    description: str | None
    string_body: str | None = None
    health_unavailable: bool = False
    on_unavailable: str | None = None
    # First-request sizing keeps the longest URL and both unavailable sentences.
    mediator_failed: bool = False


@dataclass(frozen=True)
class ReflectRequest:
    reflection_id: str
    round: int
    workspace: Path
    evidence: Any  # ReflectBrief (reflect_brief.py); Any here so this module has no import cycle
    # The rendering *and* the snapshot's files: the `subprocess` document and
    # the `chat` opening use `docs.text`, and the `agent_cli` control plane
    # writes each file under `.evolution/environment-docs/` beside
    # `RENDERED.txt` ("with the snapshot's files").
    environment_docs: EnvironmentDocs | None
    probe: ProbeAccess | None
    editable_paths: tuple[str, ...]
    deadline_seconds: float
    reader: ReflectionReadSession | None = field(default=None, repr=False, compare=False)
    probe_session: ProbeSession | None = field(default=None, repr=False, compare=False)
    #: ADR-0037 D10: the bound Editor's `max_changed_files`, stated in the family-14
    #: opening text only (never in the digested payload); `None` elsewhere.
    editor_max_changed_files: int | None = None
    #: The executor's recorder of this reflection's provider
    #: attempts, which a chat Reflector feeds; owned outside the role task, so
    #: a deadline or a stop that cancels it still leaves them. Never journaled.
    attempts: AttemptRecorder | None = field(default=None, repr=False, compare=False)
    #: `/16` chat only: the campaign-bound terminal admission callback.  It is
    #: deliberately request-local because only the campaign owns the frozen
    #: catalogs, corpus, oracle needles and run secrets for one reflection.
    result_admission: Callable[[ReflectResult], Any] | None = field(
        default=None, repr=False, compare=False
    )
    preview_admission: Callable[[ReflectResult], Any] | None = field(
        default=None, repr=False, compare=False
    )
    #: Family 15 only; request-local, never part of the wire payload or role identity.
    normalize_empty_keys: bool = False


@dataclass(frozen=True)
class RejectionDetail:
    category: str
    path: str | None = None

    def to_payload(self) -> dict[str, str]:
        return {"category": self.category, **({"path": self.path} if self.path is not None else {})}


@dataclass(frozen=True)
class ReflectResult:
    status: Literal["reflected", "declined", "failed"]
    groups: tuple[Mapping[str, Any], ...]
    note: str
    code: str | None = None
    rejection: RejectionDetail | None = None


class CandidateReflector(Protocol):
    """One reflection per invocation; never concurrent with itself; reads only its workspace;
    returns within `deadline_seconds`; honours cancellation; tolerates a repeated invocation
    for the same round across a resume."""

    @property
    def identity(self) -> ReflectorIdentity: ...
    async def reflect(self, request: ReflectRequest) -> ReflectResult: ...


@dataclass(frozen=True)
class Group:
    root_cause: str
    suggested_fix: str
    evidence_ids: tuple[str, ...]
    affected_cases: tuple[str, ...]
    target_paths: tuple[str, ...]
    probe_refs: tuple[int, ...] = ()
    counterevidence: str | None = None
    regression_risks: str | None = None
    why_this_layer: str | None = None
    document_ids: tuple[str, ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "suggested_fix": self.suggested_fix,
            "evidence_ids": list(self.evidence_ids),
            "affected_cases": list(self.affected_cases),
            "target_paths": list(self.target_paths),
            "probe_refs": list(self.probe_refs),
            **(
                {
                    "counterevidence": self.counterevidence,
                    "regression_risks": self.regression_risks,
                    "why_this_layer": self.why_this_layer,
                }
                if self.counterevidence is not None
                else {}
            ),
            **({"document_ids": list(self.document_ids)} if self.document_ids is not None else {}),
        }


class GroupError(ValueError):
    """A refused diagnosis: `code` is the journaled family, `detail` the stable text.

    `repair` says what a `/16` chat Reflector may do about it inside its own
    conversation (ADR-0048): `semantic` returns a correction and keeps the
    dialogue going, `protocol` is an existing bounds violation that takes the
    protocol-repair budget, and `none` -- an oracle string or a run secret --
    ends the role without any correction. An `oracle_leak` is never repairable.

    `correction` is the value-free, field-path form of the same refusal that
    the chat loop shows the model, such as `groups[0].affected_cases[0]:
    expected case_id`. It never quotes the refused value, an oracle string, a
    secret or a list of valid answers. `detail` keeps its historical text for
    every other reader (role logs, the agent-CLI self-check and the tests).
    """

    def __init__(
        self,
        code: Literal["invalid_result", "oracle_leak"],
        detail: str,
        *,
        repair: Literal["semantic", "protocol", "none"] | None = None,
        correction: str | None = None,
        needle: str | None = None,
        category: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.repair: Literal["semantic", "protocol", "none"] = (
            repair if repair is not None else "none" if code == "oracle_leak" else "semantic"
        )
        self.correction = correction
        self.needle = needle
        self.category = category
        self.path = path

    def rejection(self) -> RejectionDetail | None:
        if self.code == "oracle_leak":
            return None
        return RejectionDetail(self.category or "protocol", self.path)


def oracle_strings(
    oracle: Any, *, secret_paths: Sequence[str] | None = None
) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield selected strings in dictionary insertion and list index order (ADR-0049)."""

    from evolution.tree.execution.json_pointer import _pointer_tokens

    prefixes = None if secret_paths is None else tuple(_pointer_tokens(p) for p in secret_paths)

    def walk(value: Any, tokens: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], str]]:
        if isinstance(value, str):
            if prefixes is None or any(tokens[: len(prefix)] == prefix for prefix in prefixes):
                yield tokens, value
        elif isinstance(value, Mapping):
            for key, item in value.items():
                yield from walk(item, (*tokens, key))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                yield from walk(item, (*tokens, str(index)))

    yield from walk(oracle, ())


def oracle_needles(
    corpus: Sequence[EvalCase],
    declared: Collection[str] = (),
    *,
    secret_paths: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Every oracle string value of at least `MIN_NEEDLE_LENGTH` characters, longest first.

    ADR-0038 (spec D13a, the whole-needle rule): a value equal to a `declared`
    verbatim answer, or contained in one, is dropped -- a substring of a public
    string is public with it -- while a longer value that contains a declared
    answer stays. Byte-for-byte comparison, no normalization. With no
    declaration the set is unchanged.
    """

    found: dict[str, None] = {}

    for case in corpus:
        for _, value in oracle_strings(case.oracle, secret_paths=secret_paths):
            if len(value) >= MIN_NEEDLE_LENGTH:
                found[value] = None
    needles = tuple(sorted(found, key=lambda s: (-len(s), s)))
    if not declared:
        return needles
    return tuple(needle for needle in needles if not any(needle in answer for answer in declared))


def editor_visible_identifiers(corpus: Sequence[EvalCase]) -> frozenset[str]:
    """The corpus's structural strings the Editor already reads without the oracle.

    Every case's `case_id`, `group_id`, `stratum` and `tags` (every `case` record
    of `editor_brief.render_catalog` carries all four) and its `case:` evidence
    id. Observation ids (`obs:<case>:<repeat>`) are matched separately by
    `_is_observation_id`, because the repeat number is not a corpus fact. The
    case's `public` values are left out although the Editor reads them too: a
    multiple-choice option is public, which option is right is not. So is
    everything the Reflector alone receives -- scorer verdicts with their
    criterion ids, `diagnostics`, `reflection_metadata` -- since a Reflector
    that repeats those in its diagnosis would hand them to the Editor.
    """

    found: set[str] = set()
    for case in corpus:
        found.update((case.case_id, case.group_id, case.stratum, *case.tags))
        found.add(_case_evidence_id(case.case_id))
    return frozenset(found)


def _is_observation_id(value: str, case_ids: Collection[str]) -> bool:
    """Whether `value` is exactly the `obs:` evidence id of a corpus case and some repeat."""

    if not value.startswith("obs:"):
        return False
    tail = value.rpartition(":")[2]
    if not (tail.isascii() and tail.isdigit()) or str(int(tail)) != tail:
        return False
    repeat = int(tail)
    return any(_observation_evidence_id(case_id, repeat) == value for case_id in case_ids)


def reflection_needles(
    corpus: Sequence[EvalCase],
    *,
    family_version: int,
    published_criteria: Collection[str] = frozenset(),
    declared: Collection[str] = (),
    secret_paths: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """The needle set for reflection-side role text: grouping output, the Reflector's
    note and its diagnosis groups.

    Families 12 and later drop every oracle string that is *exactly equal* to an
    `editor_visible_identifiers` value or to a corpus case's observation id: the
    Editor reads those without the oracle, so a diagnosis that names them shows
    the Editor nothing new. Equality, not containment: an oracle string that
    merely contains an identifier ("the expected route of case-075 is X") stays a
    needle, and an identifier that merely contains a needle does not shield it.
    Families before 12 keep `oracle_needles` unchanged. `MIN_NEEDLE_LENGTH` and
    the substring scan (`find_needle`) are the same in every family. The
    Candidate seal and attempt feedback keep `oracle_needles`: the Candidate's
    reader is the target agent, which never sees case ids, groups, strata or tags.

    Family 14 (PDR-0017 and PDR-0028) also
    drops every oracle string exactly equal to one of `published_criteria`: the
    verdict criterion ids the scorer published on the frozen baseline the role
    input was built from (`FrozenReflectionInputs.published_criteria`). Again
    equality, not containment: an oracle string that contains an id stays a
    needle, and so does a value the scorer sent only in `diagnostics` or in a
    verdict note, since neither is a criterion. Families 12 and 13 ignore
    `published_criteria` (criterion ids stay needles there).

    Every family starts from `oracle_needles(corpus, declared)`, so `/15`'s
    declared verbatim answers (ADR-0038) leave this set before the exclusions
    above apply.
    """

    needles = oracle_needles(corpus, declared, secret_paths=secret_paths)
    if family_version < 12:
        return needles
    visible = editor_visible_identifiers(corpus)
    case_ids = {case.case_id for case in corpus}
    criteria = (
        frozenset(published_criteria)
        if family_version >= FINAL_CAMPAIGN_FAMILY_VERSION
        else frozenset()
    )
    return tuple(
        needle
        for needle in needles
        if needle not in visible
        and not _is_observation_id(needle, case_ids)
        and needle not in criteria
    )


def first_needle(text: str, needles: Sequence[str]) -> str | None:
    return next((needle for needle in needles if needle in text), None)


def find_needle(text: str, needles: Sequence[str]) -> bool:
    return first_needle(text, needles) is not None


class NeedleWitness:
    """Remember the last match in memory; never expose it through diagnostics."""

    def __init__(self, needles: Sequence[str]) -> None:
        self.needles = needles
        self.matched: str | None = None

    def __call__(self, text: str) -> bool:
        matched = first_needle(text, self.needles)
        if matched is None:
            return False
        self.matched = matched
        return True


def locate_needle(
    corpus: Sequence[EvalCase], needle: str, *, secret_paths: Sequence[str] | None
) -> tuple[str, tuple[str, ...]] | None:
    for case in corpus:
        for tokens, value in oracle_strings(case.oracle, secret_paths=secret_paths):
            if value == needle:
                return case.case_id, tokens
    return None


def encode_pointer(tokens: Sequence[str]) -> str:
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def _sentence(value: Any, name: str, limit: int | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroupError("invalid_result", f"{name} is not a non-blank string")
    if limit is not None and len(value) > limit:
        raise GroupError("invalid_result", f"{name} exceeds {limit} characters")
    if any(ord(char) < 32 and char != "\n" for char in value):
        raise GroupError("invalid_result", f"{name} carries a control character")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GroupError("invalid_result", f"{name} is not a list of strings")
    return tuple(value)


def _is_candidate_path(path: str) -> bool:
    """Whether `path` names a file *inside* the Candidate at all.

    This is the question the writable match cannot answer. `artifact_policy.matches`
    is the policy's own gitignore-like matcher, under which a pattern with no
    `/` matches a basename at any depth -- so with `editable_paths` of
    `["*.md"]` both `../outside.md` and `/tmp/outside.md` match, on their
    basename, while naming nothing the Candidate contains. The actor path
    grammar decides membership first (`editor_workspace.relative_candidate_path`,
    the same validator the editors' tools address files through: relative,
    non-empty, no `.` or `..` segment, no NUL), and only a path that passes it
    is put to the writable set (PDR-0007's "every target path is
    writable" presumes the path is a Candidate path).
    """

    from evolution.tree.roles.workspace import relative_candidate_path

    try:
        relative_candidate_path(path)
    except ValueError:
        return False
    return True


def _matches_any(path: str, globs: Sequence[str]) -> bool:
    """The artifact policy's own writable-path match, so `target_paths` and the seal agree.

    Imported lazily: `artifact_policy` is otherwise unrelated to this module's
    import graph, and a module-scope import would pull it in even for callers
    that only build requests or run the deadline wrapper.
    """

    from evolution.tree.artifacts.artifact_policy import matches

    return any(matches(pattern, PurePosixPath(path), False) for pattern in globs)


def validate_groups(
    groups: Sequence[Any],
    *,
    catalog_ids: Mapping[str, str | None],
    case_ids: Collection[str],
    editable_paths: Sequence[str],
    probe_seqs: Collection[int] | None,
    needles: Sequence[str],
    limits: Mapping[str, int] = REFLECT_LIMITS_V1,
    observation_ids: Mapping[str, str] | None = None,
    failing_evidence_ids: Collection[str] = (),
    document_ids: Collection[str] | None = None,
    group_text_chars: int | None = 512,
    schema_corrections: bool = False,
) -> tuple[Group, ...]:
    # `group_text_chars` applies to the adaptive form only: the limit of the
    # round the reflection ran in; the earlier form keeps `limits`.
    if observation_ids is not None:
        return _validate_adaptive_groups(
            groups,
            catalog_ids=catalog_ids,
            observation_ids=observation_ids,
            failing_evidence_ids=failing_evidence_ids,
            case_ids=case_ids,
            editable_paths=editable_paths,
            probe_seqs=probe_seqs,
            needles=needles,
            document_ids=document_ids,
            group_text_chars=group_text_chars,
            schema_corrections=schema_corrections,
        )
    if not isinstance(groups, (list, tuple)):
        raise GroupError("invalid_result", "groups is not a list")
    if len(groups) > limits["groups"]:
        raise GroupError("invalid_result", f"more than {limits['groups']} groups")
    keys = GROUP_KEYS if probe_seqs is None else GROUP_KEYS_WITH_PROBES
    parsed: list[Group] = []
    for item in groups:
        if not isinstance(item, Mapping) or set(item) != keys:
            raise GroupError("invalid_result", "group keys are not exactly the schema's")
        root_cause = _sentence(item["root_cause"], "root_cause", limits["sentence_chars"])
        suggested_fix = _sentence(item["suggested_fix"], "suggested_fix", limits["sentence_chars"])
        # A leak is reported as `oracle_leak` ahead of every other defect a group might
        # also carry (module docstring), so the needle scan runs as soon as the two
        # sentences are validated strings -- before any evidence/case/path check that
        # could otherwise report the same group as a mere `invalid_result`.
        for text in (root_cause, suggested_fix):
            if (needle := first_needle(text, needles)) is not None:
                raise GroupError(
                    "oracle_leak", "a sentence carries an oracle string", needle=needle
                )
        evidence_ids = _strings(item["evidence_ids"], "evidence_ids")
        if not evidence_ids or any(e not in catalog_ids for e in evidence_ids):
            raise GroupError("invalid_result", "evidence_ids is empty or cites an unknown record")
        cited_cases = {catalog_ids[e] for e in evidence_ids} - {None}
        affected = _strings(item["affected_cases"], "affected_cases")
        if any(c not in case_ids or c not in cited_cases for c in affected):
            raise GroupError(
                "invalid_result", "affected_cases names a case no cited record belongs to"
            )
        targets = _strings(item["target_paths"], "target_paths")
        if any(not _is_candidate_path(path) for path in targets):
            raise GroupError("invalid_result", "target_paths is not a relative Candidate path")
        if any(not _matches_any(path, editable_paths) for path in targets):
            raise GroupError("invalid_result", "target_paths leaves the writable set")
        refs: tuple[int, ...] = ()
        if probe_seqs is not None:
            raw = item["probe_refs"]
            if not isinstance(raw, list) or any(
                type(r) is not int or r not in probe_seqs for r in raw
            ):
                raise GroupError("invalid_result", "probe_refs names a probe that did not run")
            refs = tuple(raw)
        parsed.append(Group(root_cause, suggested_fix, evidence_ids, affected, targets, refs))
    return tuple(parsed)


def _screen_leaks(
    groups: Sequence[Any],
    note: str,
    *,
    needles: Sequence[str],
    runtime_secrets: Sequence[str],
    adaptive: bool,
    on_hit: Callable[[GroupError, str], None] | None = None,
) -> None:
    """Refuse an oracle string or a run secret in submitted, unparsed groups.

    The oracle scope is the one admission has always had -- the note and each
    group's sentences (`ADAPTIVE_GROUP_TEXT_KEYS`, or the two sentences of the
    earlier form) -- never identifiers or target paths. The secret scope is
    every string, key names included, that the model controlled. Items that
    are not objects are skipped here and refused later by the shape checks.
    """

    def reject(error: GroupError, location: str) -> None:
        if on_hit is None:
            raise error
        on_hit(error, location)

    if (needle := first_needle(note, needles)) is not None:
        reject(
            GroupError("oracle_leak", "the note carries an oracle string", needle=needle), "note"
        )
    keys = ADAPTIVE_GROUP_TEXT_KEYS if adaptive else ("root_cause", "suggested_fix")
    for index, item in enumerate(groups):
        if not isinstance(item, Mapping):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and (needle := first_needle(value, needles)) is not None:
                reject(
                    GroupError("oracle_leak", "a sentence carries an oracle string", needle=needle),
                    f"sentence/{index}/{key}",
                )
    if runtime_secrets:

        def scan(value: Any, location: str) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    scan(key, f"{location}/{key}/key")
                    scan(item, f"{location}/{key}")
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    scan(item, f"{location}/{index}")
            elif secret_match({"payload": value}, runtime_secrets) is not None:
                reject(
                    GroupError(
                        "invalid_result",
                        "the diagnosis carries a run secret",
                        repair="none",
                        category="run_secret",
                    ),
                    location,
                )

        scan(groups, "groups")
        scan(note, "note")


def admit_reflection_result(
    outcome: ReflectResult,
    *,
    catalog_ids: Mapping[str, str | None],
    case_ids: Collection[str],
    editable_paths: Sequence[str],
    probe_seqs: Collection[int] | None,
    needles: Sequence[str],
    runtime_secrets: Sequence[str],
    observation_ids: Mapping[str, str] | None = None,
    failing_evidence_ids: Collection[str] = (),
    document_ids: Collection[str] | None = None,
    group_text_chars: int | None = 512,
    leak_first: bool = False,
    normalize_empty_keys: bool = False,
    modes: CheckModes = ENFORCE_ALL,
    hits: HitSink | None = None,
    leak_field: Callable[[str], Mapping[str, str] | None] | None = None,
) -> tuple[Group, ...]:
    """The campaign's one admission rule for a normalized Reflector result.

    Non-enforcing output modes leave the schema, identifiers and bounds in
    force. Shadow scans retain only value-free metadata in the invocation's
    sink; off skips that scan. Family 15 also proves the bounds of the redacted
    journal form, since replacing a short secret can lengthen a sentence.

    The campaign calls it before journaling `reflection_finished`, and a `/16`
    chat Reflector calls the same campaign-bound closure on each attempted
    `finish_reflection` (ADR-0048), so the two judgments cannot differ on
    the same frozen inputs. `runtime_secrets` is the reflection's own set: the
    run's secrets plus, once a probe mediator started, its capability.

    With `normalize_empty_keys` (family 15), the raw run-secret screen runs
    first, including key names. Empty unknown keys are then dropped before
    either existing path sees bounds, merging or oracle text.

    Without normalization and `leak_first`, the order is the one every earlier
    release applied. It still decides `/15`: the note's oracle
    scan, then `validate_groups` (whose per-group sentence scan reports an
    `oracle_leak` ahead of that group's other defects), then the run-secret
    scan. With `leak_first` (`/16` chat only) both leak scans run over the
    submitted groups before the bounds and semantic checks, so no correction
    the role is shown can follow a leak anywhere in its result;
    a result with a leak and another defect is then reported as the leak.

    The run-secret scan, in either order: a model that echoes a forwarded
    credential back is this reflection's own bad result, not the campaign's
    storage failing. Without it the redactor inside `append` refuses the
    `reflection_finished` record and `CampaignInvariantViolated("invalid
    journal record")` escapes `evolve()` over an otherwise healthy run. The
    envelope rejects a secret in *any* string it carries, so the scan is
    `secret_match` over the record's own payload shape -- every string of every
    group, the evidence ids, the affected cases and the target paths included,
    each of them model-controlled -- and not the two sentences alone.
    Reusing the envelope's own walk is what makes this check agree with the
    redaction it stands in front of. The note is safe by construction (only
    its digest is journaled) but is scanned too: a sentence and a note are the
    same kind of model output.
    """

    adaptive = observation_ids is not None
    persistence_secrets = runtime_secrets
    raw_groups = outcome.groups
    for check, code in (
        ("reflection_oracle_leak", "oracle_leak"),
        ("reflection_run_secret", "run_secret"),
    ):
        mode = modes.mode(check)
        if mode == "shadow":

            def record_hit(
                error: GroupError, location: str, *, check: str = check, code: str = code
            ) -> None:
                if hits is not None:
                    hits.hit(
                        check,
                        code,
                        location=location,
                        leak=leak_field(error.needle)
                        if error.needle is not None and leak_field is not None
                        else None,
                    )

            _screen_leaks(
                raw_groups if isinstance(raw_groups, (list, tuple)) else [raw_groups],
                outcome.note,
                needles=needles if check == "reflection_oracle_leak" else (),
                runtime_secrets=runtime_secrets if check == "reflection_run_secret" else (),
                adaptive=adaptive,
                on_hit=record_hit,
            )
        if mode != "enforce":
            if check == "reflection_oracle_leak":
                needles = ()
            else:
                runtime_secrets = ()
    schema = adaptive_group_schema(
        probes=probe_seqs is not None, documents=document_ids is not None
    )
    if normalize_empty_keys:
        if (
            runtime_secrets
            and secret_match(
                {
                    "payload": {
                        "groups": list(raw_groups)
                        if isinstance(raw_groups, (list, tuple))
                        else raw_groups,
                        "note": outcome.note,
                    }
                },
                runtime_secrets,
            )
            is not None
        ):
            raise GroupError(
                "invalid_result",
                "the diagnosis carries a run secret",
                repair="none",
                category="run_secret",
            )
        if not isinstance(raw_groups, (list, tuple)):
            raise GroupError(
                "invalid_result", "groups is not a list", repair="protocol", category="protocol"
            )
        raw_groups = drop_empty_unknown_keys(raw_groups, schema)
    if leak_first:
        _screen_leaks(
            raw_groups,
            outcome.note,
            needles=needles,
            runtime_secrets=runtime_secrets,
            adaptive=adaptive,
        )
        if adaptive:
            try:
                check_adaptive_group_bounds(
                    raw_groups, group_text_chars, schema=schema if normalize_empty_keys else None
                )
            except GroupError as exc:
                raise GroupError(
                    exc.code,
                    exc.detail,
                    repair="protocol",
                    correction=exc.correction,
                    category=exc.category,
                    path=exc.path,
                ) from exc
    if (needle := first_needle(outcome.note, needles)) is not None:
        raise GroupError("oracle_leak", "the note carries an oracle string", needle=needle)
    groups = validate_groups(
        raw_groups,
        catalog_ids=catalog_ids,
        case_ids=case_ids,
        editable_paths=editable_paths,
        probe_seqs=probe_seqs,
        needles=needles,
        observation_ids=observation_ids,
        failing_evidence_ids=failing_evidence_ids,
        document_ids=document_ids,
        group_text_chars=group_text_chars,
        schema_corrections=normalize_empty_keys,
    )
    diagnosis = {"groups": [group.to_payload() for group in groups], "note": outcome.note}
    if runtime_secrets and secret_match({"payload": diagnosis}, runtime_secrets) is not None:
        raise GroupError(
            "invalid_result",
            "the diagnosis carries a run secret",
            repair="none",
            category="run_secret",
        )
    if normalize_empty_keys and adaptive and modes.mode("reflection_run_secret") != "enforce":
        try:
            check_adaptive_group_bounds(
                redact_for_journal(diagnosis["groups"], persistence_secrets), group_text_chars
            )
        except GroupError as exc:
            raise GroupError(
                exc.code, exc.detail, repair="protocol", category=exc.category
            ) from exc
    return groups


def result_note_digest(note: str) -> str:
    """A reflection's `note` digest -- `campaign_editor.note_digest`'s own canonical
    hashing, so a note digest means one thing whether an editor or a reflector wrote it."""

    return _note_digest(note)


def adaptive_group_schema(*, probes: bool, documents: bool) -> tuple[str, ...]:
    return tuple(
        name
        for name in ADAPTIVE_GROUP_KEY_ORDER
        if (name != "probe_refs" or probes) and (name != "document_ids" or documents)
    )


def is_empty(value: Any) -> bool:
    return value is None or (
        type(value) in (str, list, dict) and (value == "" or value == [] or value == {})
    )


def drop_empty_unknown_keys(groups: Any, allowed: Collection[str]) -> Any:
    if not isinstance(groups, (list, tuple)) or not all(
        isinstance(item, Mapping) for item in groups
    ):
        return groups
    return tuple(
        {key: value for key, value in item.items() if key in allowed or not is_empty(value)}
        for item in groups
    )


def _missing_key_correction(groups: Any, schema: Sequence[str] | None) -> str | None:
    # Missing required fields are already a bounds/shape path. Name the schema
    # key without changing its repair budget or the historical default text.
    if schema is None or not isinstance(groups, (list, tuple)):
        return None
    required = tuple(name for name in schema if name not in {"probe_refs", "document_ids"})
    for index, item in enumerate(groups):
        if isinstance(item, Mapping):
            for name in required:
                if name not in item:
                    return f"groups[{index}]: missing key {name}"
    return None


ADAPTIVE_GROUP_TEXT_KEYS = (
    "root_cause",
    "suggested_fix",
    "counterevidence",
    "regression_risks",
    "why_this_layer",
)


def _group_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise GroupError("invalid_result", "groups is not canonical JSON") from exc


_ADAPTIVE_LIST_KEYS = ("evidence_ids", "affected_cases", "target_paths", "document_ids")


def _over(value: int, limit: int | None) -> bool:
    return limit is not None and value > limit


def adaptive_group_violation(
    groups: Sequence[Mapping[str, Any]], limits: Mapping[str, int | None]
) -> str | None:
    """The first adaptive limit `groups` break, or `None`: one check for every layer.

    `groups` is the stored form, `Group.to_payload()` groups, which is what the
    `reflection_finished` record holds (R4): the canonical bytes include each
    group's `probe_refs`, and every list counts its entries as listed, repeated
    `evidence_ids` included. Limits only; the shape rules (non-blank text,
    control characters, key sets) stay with each layer.
    """

    if not isinstance(groups, (list, tuple)) or _over(len(groups), limits["groups"]):
        return "groups exceeds the protocol count limit"
    try:
        size = len(_group_bytes(groups))
    except GroupError as exc:
        return exc.detail
    if _over(size, limits["groups_bytes"]):
        return f"groups exceeds {limits['groups_bytes']} bytes"
    for item in groups:
        if not isinstance(item, Mapping):
            return "group is not an object"
        for name in ADAPTIVE_GROUP_TEXT_KEYS:
            value = item.get(name)
            if not isinstance(value, str):
                return f"{name} is not a non-blank string"
            if _over(len(value), limits["sentence_chars"]):
                return f"{name} exceeds {limits['sentence_chars']} characters"
        for name in _ADAPTIVE_LIST_KEYS:
            if name == "document_ids" and name not in item:
                continue
            values = item.get(name)
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                return f"{name} is not a list of strings"
            if _over(len(values), limits[name]):
                return f"{name} exceeds the protocol count limit"
    return None


def _as_stored(groups: Any) -> Any:
    """Submitted groups in the shape `Group.to_payload()` stores them: identical
    groups merged as `_validate_adaptive_groups` merges them (one per canonical
    form) and every group carrying `probe_refs`. The byte cap is the
    stored record's, so an identical repeat costs nothing (PDR-0039: `/15` keeps
    admitting it) while repeats inside one group still count (R4). A set that is
    not canonical JSON is returned unmerged, for the cap's own refusal."""

    if not isinstance(groups, (list, tuple)):
        return groups
    try:
        merged = list({_group_bytes(item): item for item in groups}.values())
    except GroupError:
        merged = list(groups)
    return [
        {**item, "probe_refs": item.get("probe_refs", [])} if isinstance(item, Mapping) else item
        for item in merged
    ]


def check_adaptive_group_bounds(
    groups: Any, group_text_chars: int | None = 512, *, schema: Sequence[str] | None = None
) -> None:
    """Backend-independent submitted-array limits; no partial repair or trimming.

    The group count bounds the submitted array, as before. Every other limit
    is `adaptive_group_violation`'s over the groups as they will be stored (merged,
    with `probe_refs`), so a response admitted here is never refused at the journal
    append. `group_text_chars` is the reflection's round limit, which every
    backend reads from its `AdaptiveReflectBrief` (`group_text_chars`).
    """
    limits = adaptive_limits(group_text_chars)
    if not isinstance(groups, (list, tuple)) or _over(len(groups), limits["groups"]):
        raise GroupError(
            "invalid_result",
            "groups exceeds the protocol count limit",
            category="protocol" if not isinstance(groups, (list, tuple)) else "bounds",
        )
    violation = adaptive_group_violation(_as_stored(groups), limits)
    if violation is not None:
        raise GroupError(
            "invalid_result",
            violation,
            correction=_missing_key_correction(groups, schema),
            category="protocol"
            if violation in {"groups is not canonical JSON", "group is not an object"}
            else "bounds",
        )
    try:
        for item in groups:
            for name in ADAPTIVE_GROUP_TEXT_KEYS:
                _sentence(item.get(name), name, None)
    except GroupError as exc:
        raise GroupError(exc.code, exc.detail, category="bounds") from exc


def _validate_adaptive_groups(
    groups: Sequence[Any],
    *,
    catalog_ids: Mapping[str, str | None],
    observation_ids: Mapping[str, str],
    failing_evidence_ids: Collection[str],
    case_ids: Collection[str],
    editable_paths: Sequence[str],
    probe_seqs: Collection[int] | None,
    needles: Sequence[str],
    document_ids: Collection[str] | None,
    group_text_chars: int | None,
    schema_corrections: bool = False,
) -> tuple[Group, ...]:
    schema = adaptive_group_schema(
        probes=probe_seqs is not None, documents=document_ids is not None
    )
    if schema_corrections:
        check_adaptive_group_bounds(groups, group_text_chars, schema=schema)
    else:
        check_adaptive_group_bounds(groups, group_text_chars)
    required = set(adaptive_group_schema(probes=False, documents=False))
    optional = set(schema) - required
    unique: dict[bytes, Group] = {}

    def refuse(detail: str, path: str, message: str, category: str) -> GroupError:
        # `detail` keeps its historical text; `correction` is the value-free
        # field path the `/16` chat loop shows the model.
        return GroupError(
            "invalid_result", detail, correction=f"{path}: {message}", category=category, path=path
        )

    def listed(item: Mapping[str, Any], name: str, at: str) -> tuple[str, ...]:
        try:
            return _strings(item[name], name)
        except GroupError as exc:
            raise refuse(
                exc.detail, f"{at}.{name}", "expected a list of strings", "not_a_string_list"
            ) from exc

    for index, item in enumerate(groups):
        at = f"groups[{index}]"
        if not required <= set(item) or set(item) - required - optional:
            correction = "keys do not match the finish_reflection group schema"
            missing = next((name for name in schema if name in required and name not in item), None)
            if schema_corrections:
                correction = (
                    f"missing key {missing}"
                    if missing is not None
                    else f"unknown key; allowed keys are {', '.join(schema)}"
                )
            raise refuse(
                "group keys do not match the adaptive schema",
                at,
                correction,
                "missing_key" if missing is not None else "unknown_key",
            )
        for name in ADAPTIVE_GROUP_TEXT_KEYS:
            if (needle := first_needle(item[name], needles)) is not None:
                raise GroupError(
                    "oracle_leak", "a sentence carries an oracle string", needle=needle
                )
        evidence = listed(item, "evidence_ids", at)
        unknown = "evidence_ids is empty or cites an unknown record"
        if not evidence:
            raise refuse(
                unknown,
                f"{at}.evidence_ids",
                "cite at least one evidence ID",
                "missing_evidence_id",
            )
        for position, value in enumerate(evidence):
            if value not in catalog_ids and value not in observation_ids:
                raise refuse(
                    unknown,
                    f"{at}.evidence_ids[{position}]",
                    "unknown evidence ID",
                    "unknown_evidence_id",
                )
        cited_cases = {observation_ids[value] for value in evidence if value in observation_ids}
        affected = listed(item, "affected_cases", at)
        for position, value in enumerate(affected):
            if value not in case_ids:
                category = "expected case_id"
            elif value not in cited_cases:
                category = "each affected case needs a citation of its own observation"
            else:
                continue
            raise refuse(
                "affected_cases requires a cited observation of every case",
                f"{at}.affected_cases[{position}]",
                category,
                "not_a_case_id" if value not in case_ids else "uncited_affected_case",
            )
        targets = listed(item, "target_paths", at)
        for position, path in enumerate(targets):
            if not _is_candidate_path(path) or not _matches_any(path, editable_paths):
                raise refuse(
                    "target_paths leaves the writable Candidate set",
                    f"{at}.target_paths[{position}]",
                    "expected a writable Candidate path",
                    "unwritable_target_path",
                )
        docs = listed(item, "document_ids", at) if "document_ids" in item else None
        for position, value in enumerate(docs or ()):
            if value not in (document_ids or ()):
                raise refuse(
                    "document_ids cites an unknown document",
                    f"{at}.document_ids[{position}]",
                    "unknown document ID",
                    "unknown_document_id",
                )
        raw_refs = item.get("probe_refs", [])
        probe_detail = "probe_refs names a probe that did not run"
        if not isinstance(raw_refs, list):
            raise refuse(
                probe_detail,
                f"{at}.probe_refs",
                "expected a list of probe sequence numbers",
                "not_a_probe_list",
            )
        for position, value in enumerate(raw_refs):
            if type(value) is not int or value not in (probe_seqs or ()):
                raise refuse(
                    probe_detail,
                    f"{at}.probe_refs[{position}]",
                    "names a probe that did not run",
                    "unknown_probe",
                )
        parsed = Group(
            item["root_cause"],
            item["suggested_fix"],
            evidence,
            affected,
            targets,
            tuple(raw_refs),
            item["counterevidence"],
            item["regression_risks"],
            item["why_this_layer"],
            docs,
        )
        unique.setdefault(_group_bytes(item), parsed)
    failing = set(failing_evidence_ids)

    def rank(pair: tuple[bytes, Group]) -> tuple[int, int, str]:
        content, parsed = pair
        cited = set(parsed.evidence_ids) & failing
        claims = set(parsed.affected_cases) & {
            observation_ids[value] for value in cited if value in observation_ids
        }
        return (-len(claims), -len(cited), hashlib.sha256(content).hexdigest())

    return tuple(parsed for _, parsed in sorted(unique.items(), key=rank))


REFLECT_TOOL_SCHEMA_V16 = "reflect_tools/3"


def reflect_tool_schema_for(pin: EvidencePin | None) -> str:
    return REFLECT_TOOL_SCHEMA if pin is None else REFLECT_TOOL_SCHEMA_V16
