"""Operator authorizations: the class table, the value, the diff, the directory (PDR-0010, ADR-0015).

Core detects a difference and reports it as data; the caller decides; core
records the decision. This module holds the pure half: which refusal codes an
operator may accept, what one accepted instance is (a digest over both sides
of the transition), what the difference looks like when printed (dotted fields
and digests, never values), and how the CLI's decision directory is read and
written. It imports nothing that opens a socket or a journal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from evolution.tree.execution.journal_identity import canonical_json

SCHEMA = "evolution-v2-authorization/1"
AUTHORIZATIONS_DIR = "authorizations"
MAX_DECISIONS = 1000
MAX_DECISION_BYTES = 64 * 1024
MAX_REASON_CHARS = 1000
#: `write_decision`'s own temporary name. A crash between the staged file and the
#: rename leaves one behind, and §7 makes the file name a convenience rather
#: than an input, so the reader skips it instead of refusing the directory.
TEMPORARY_PREFIX = ".decision-"

RefusalClass = Literal["authorizable", "hard"]

#: Spec §3. A code moves between classes only by amending the spec and ADR-0015.
#: `lineage_not_finished` joined here in D2a (§5.4): a family-5 predecessor
#: that has not finished may be linked under an accepted decision; the same
#: code stays hard for an unfinished family-6 predecessor, where resume is
#: the only door (`HARD_REASONS` keeps that case's sentence).
#: `baseline_import_unavailable` joined in D2b (§5.3): a declared baseline copy
#: whose source is lost may be measured fresh under an accepted decision.
AUTHORIZABLE_CODES = frozenset(
    {
        "run_identity_changed",
        "run_dir_unsafe_entry",
        "lineage_not_finished",
        "baseline_import_unavailable",
    }
)

#: PDR-0012 / spec §5.1, §5.4: what an accepted decision will do, printed after
#: `class: authorizable` so the operator reads the consequence before the
#: command. Keyed by :class:`Effect`; `rebase` and `hygiene` need no sentence
#: of their own -- a rebase is today's familiar continuation and a hygiene
#: admission is a fact about a directory entry, not a campaign consequence.
#: `continue` and `rebaseline` joined in I2 (spec §3.1, §4.1, §4.3): the two
#: in-place effects a family-7 `run_identity_changed` finding may offer beside
#: `close`, printed the same way as the effects above.
Effect = Literal[
    "rebase", "hygiene", "close", "terminal", "link", "fresh_baseline", "continue", "rebaseline"
]

EFFECT_SENTENCES: Mapping[str, tuple[str, ...]] = {
    "close": (
        "accepting closes this campaign as stopped(condition_changed);",
        "continue it with a linked run that declares the new condition",
    ),
    "terminal": (
        "accepting writes the terminal this campaign already owes;",
        "continue it with a linked run that declares the new condition",
    ),
    "link": (
        "the predecessor is unfinished and will not be closed; if it is ever resumed,",
        "its rounds and this run's spend the same alpha",
    ),
    "fresh_baseline": (
        "accepting measures this run's baseline in full instead of carrying",
        "the predecessor's rows",
    ),
    "continue": (
        "accepting keeps this campaign and its measurements; unfinished measurements "
        "continue under the new condition",
    ),
    "rebaseline": (
        "accepting closes the open round without promotion; the next round starts "
        "from a fresh baseline under the new condition",
    ),
}

#: I2 spec §4.1: the lead line before each command of a finding that offers
#: several effects. The default's command carries no `--effect`, whichever
#: effect it is; every other one names its effect.
EFFECT_LEADS: Mapping[str, str] = {
    "continue": "to continue with the existing measurements, run:",
    "rebaseline": "to start the next round from a fresh baseline under the new condition, run:",
    "close": "to close the campaign and continue with a linked run, run:",
}

#: I2 spec §4.1: why a scale change offers no `continue`.
SCALE_CHANGE_SENTENCE = (
    "the scorer, metric, corpus or policy changed; "
    "existing scores cannot be compared on the new scale inside a round"
)

HARD_CODES = frozenset(
    {
        "corpus_partition_changed",
        "bad_schema",
        "manifest_schema",
        "missing_key",
        "unknown_key",
        "bad_type",
        "bad_value",
        "bad_path",
        "not_loopback",
        "program_inside_seed",
        "scorer_invalid",
        "editor_invalid",
        "reflector_invalid",
        "environment_invalid",
        "run_dir_overlaps_program",
        "campaign_family_unsupported",
        "run_root_not_empty",
        "run_dir_not_campaign",
        "run_dir_parent_missing",
        "run_dir_overlaps_seed",
        "run_id_mismatch",
        "package_version_unavailable",
        "stored_identity_digest_mismatch",
        "adaptive_runtime_required",
        "capabilities_insufficient",
        "corpus_too_large",
        "record_matches_secret",
        #: ADR-0038: a declared verbatim answer equal to no oracle value of the
        #: train corpus; the manifest is the only door.
        "verbatim_answer_unmatched",
        "secret_oracle_path_unmatched",
        "not_json",
        "not_object",
        "duplicate_case_id",
        "empty_corpus",
        "unreadable",
        "reflection_capacity",
        "environment_docs_invalid",
        "editor_env_missing",
        "reflector_env_missing",
        "editor_program_missing",
        "reflector_program_missing",
        #: A§3.3: the `/15` Grouper's binding refusals, the Reflector's three
        #: under the `grouper` prefix.
        "grouper_invalid",
        "grouper_env_missing",
        "grouper_program_missing",
        "not_ready",
        "pin_mismatch",
        "attestation_missing",
        "strategy_unsupported",
        "limits_incompatible",
        "policy_mismatch",
        "journal_busy",
        "undeclared_sibling",
        "artifact_ledger_missing",
        "lineage_family_unsupported",
        #: Family-13 D10 (maintainer, E2): an `observe` predecessor under a
        #: `statistical` successor; no decision turns observe evidence into
        #: statistical evidence.
        "lineage_mode_unsupported",
        "lineage_champion_unverified",
        "baseline_source_unavailable",
        "authorizations_invalid",
        "authorization_invalid",
        "invalid_family_value",
        "campaign_absent",
        #: I2 §3.4, §5.4, §4.2: an impossibility (a policy that contradicts the
        #: durable prefix), a recovery-time drift (the manifest changed again
        #: after the accepted decision), and a decision conflict (two decisions
        #: naming the same instance with different effects) -- none of these
        #: has a door that admits the run as it stands.
        "policy_contradicts_prefix",
        "authorized_identity_changed",
        "authorization_conflict",
        #: I2 §5.3 (Task 7): a frozen measurement -- this run's own source of
        #: carried rows, never written again -- no longer yields what its
        #: re-binding declared. Corruption, not a lost source: no decision
        #: admits it.
        "rebound_source_corrupt",
    }
)
#: The identity fields a rebase may never change (§3): `_COMPARABLE` plus the store.
COMPARABLE_FIELDS = (
    "policy_digest",
    "target",
    "environment",
    "scorer",
    "evaluation_set_id",
    "corpus_digest",
    "corpus_cases",
    "metric_plan_digest",
    "metric_contract",
    "materialization_strategy",
    "artifact_store",
)
GUARD_CLASSES = ("symlinked", "foreign owner", "not owner-only")

#: The sentence the CLI prints after `class: hard` (§3, §4).
HARD_REASONS: Mapping[str, str] = {
    #: Comparable-links spec §5.1: a comparable change on an unfinished
    #: family-6 campaign is authorizable (it closes the campaign), so the only
    #: hard comparable case the producer can meet is a finished campaign; the
    #: door is a linked run that declares the new condition (§3).
    "run_identity_changed": (
        "the campaign is finished and there is nothing left to continue; "
        "start a linked run that declares the new condition"
    ),
    #: Not a code: a finished campaign whose difference names no comparable
    #: field (the CLI picks it by `comparable_changed`). Both this sentence and
    #: `run_identity_changed`'s describe a finished campaign -- since
    #: comparable-links §5.1 that is the only hard case either can reach --
    #: and they differ only in the door: a comparable change needs a linked run
    #: that declares the new condition, a non-comparable one may also start a
    #: new run directory. A campaign that crossed its threshold without writing
    #: its terminal is not this case: it owes one record, and PDR-0010 (amended
    #: 2026-09-17) makes its difference authorizable so that record is written.
    "run_identity_changed_finished": (
        "the campaign is finished and there is nothing left to continue; "
        "start a linked run or a new run directory"
    ),
    #: Not a code either: continuable terminals §6.3's resume presence rule.
    #: `identity.linked_from` is not comparable and the campaign need not be
    #: finished, so neither sentence above names this cause.
    "run_identity_changed_linked_from": (
        "A successor needs a new run directory; a campaign keeps the link it was created "
        "with, so linked_from cannot be added to or removed from a campaign that exists."
    ),
    #: Not a code either: baseline inheritance design §4.1's resume rule
    #: beside the presence one above. `identity.linked_from.baseline` is not
    #: comparable and the campaign need not be finished, for the same reason.
    "run_identity_changed_linked_baseline": (
        "A successor needs a new run directory; the manifest must declare the baseline "
        "mode the campaign was created with, so it cannot change on a campaign that exists."
    ),
    #: Not a code either: ADR-0021's adaptation profile move (`/1` <-> `/2`,
    #: ADR-0022's `/3` and ADR-0023's `/4`), a family change whatever the
    #: campaign's state, so no decision applies and the door is a linked run
    #: under the manifest schema it needs.
    "run_identity_changed_adaptation_profile": (
        "Manifests /8, /9, /10 and /11 each write a different campaign family, so a campaign "
        "cannot change between them; start a linked run (linked_from) under the new manifest."
    ),
    #: Not a code either: `run_dir_unsafe_entry` is authorizable, and the one
    #: instance of it that is not is the same case as the line above -- the
    #: campaign is finished, so no `authorization_applied` could follow its
    #: terminal. The CLI reaches this key when the refusal carries no instance
    #: digest, which core computes only for a decision a run could apply
    #: The entry itself is not the operator's mistake, so the
    #: sentence does not ask them to change it.
    "run_dir_unsafe_entry_finished": (
        "the campaign is finished and there is nothing left to continue; "
        "start a linked run or a new run directory"
    ),
    #: Not a code either: `admit_output_root` raises this same digest-less
    #: `run_dir_unsafe_entry` when `lstat` finds a regular file where an
    #: output root must be. That case is hard for a reason unrelated to a
    #: finished campaign -- there is no finding to admit, only a file to
    #: remove -- and the CLI tells the two apart by `detail`: a guard class
    #: (`GUARD_CLASSES`) is the finished-campaign case above, and this one
    #: is `detail == "not a directory"`.
    "run_dir_unsafe_entry_file": (
        "a file stands where an output directory must be; remove it, it is not accepted"
    ),
    "campaign_family_unsupported": "the journal is of another record family and there is no converter",
    "run_root_not_empty": "choose an empty run directory",
    "run_dir_not_campaign": "choose an empty run directory or the campaign's own",
    "program_inside_seed": "declare the program outside the seed tree",
    "run_dir_overlaps_program": "keep the run directory apart from every program's cwd and files",
    "scorer_invalid": "bind the program by a script path, not a `-m` module",
    "editor_invalid": "bind the program by a script path, not a `-m` module",
    "reflector_invalid": "bind the program by a script path, not a `-m` module",
    "pin_mismatch": "the manifest pins this target; set the pin to null to follow a drifting target",
    "attestation_missing": "the target does not attest what it loaded; nothing it scores is evidence",
    "journal_busy": "another writer holds this campaign",
    "record_matches_secret": "a forwarded environment value appears in the record",
    "capabilities_insufficient": "the target can no longer serve the policy's demand",
    "package_version_unavailable": (
        "the installed package has no version metadata; install the wheel or the editable "
        "checkout properly"
    ),
    "stored_identity_digest_mismatch": (
        "a measurement journal does not match the identity the campaign derives for it"
    ),
    "undeclared_sibling": (
        "a measurement directory the campaign journal never declared; remove it or choose "
        "another run directory"
    ),
    "artifact_ledger_missing": (
        "the artifact ledger artifacts.jsonl is missing or empty while the campaign "
        "journal records measurements; restore it from a trusted copy of the run directory"
    ),
    "adaptive_runtime_required": (
        "the campaign policy must be a /5 policy with an adaptive Reflector"
    ),
    #: Continuable-terminals spec §6.3, amended by D2a §5.4: `lineage_not_finished`
    #: moved to `AUTHORIZABLE_CODES` for a family-5 predecessor, but stays hard
    #: for an unfinished family-6 one, where resume is the only door and no
    #: instance digest is computed -- this sentence is what that hard instance
    #: prints. `lineage_family_unsupported` and `lineage_champion_unverified`
    #: stay hard outright.
    "lineage_not_finished": (
        "The predecessor has not finished; resume it and let it finish, then link."
    ),
    "lineage_family_unsupported": (
        "The predecessor's campaign family cannot be linked by this version."
    ),
    "lineage_mode_unsupported": (
        "The predecessor ran in observe mode, so its Champion cannot seed a statistical campaign."
    ),
    "lineage_champion_unverified": (
        "The predecessor's champion copy does not match its artifact ledger."
    ),
    #: Baseline inheritance §5.0: nothing is declared yet when the source
    #: cannot be read at creation, so the door is the manifest itself.
    "baseline_source_unavailable": (
        "the predecessor's baseline cannot be read; declare "
        '"baseline": "fresh" in linked_from to measure it in full'
    ),
    #: ADR-0038: the bind check names the entry's path, never its value.
    "verbatim_answer_unmatched": (
        "a declared verbatim answer equals no oracle value of the train cases; "
        "correct it or remove it"
    ),
    "secret_oracle_path_unmatched": (
        "a declared oracle pointer selects no protected-length string in the train cases; "
        "correct it or remove it"
    ),
    #: I2 §3.4: an impossibility, not a claim-protecting rule -- a round or
    #: alpha budget (or any other campaign-wide budget) below what the
    #: durable prefix has already spent cannot apply without rewriting
    #: history.
    "policy_contradicts_prefix": (
        "the new policy asks for less than this run has already spent; "
        "correct the value, or accept close"
    ),
    #: I2 §5.4: the transition recomputes the identity from the manifest on
    #: hand at recovery; a manifest edited again after the operator's decision
    #: -- or a package upgraded between the crash and the resume, which moves
    #: only `producing_version` -- no longer names the identity that decision
    #: accepted.
    "authorized_identity_changed": (
        "the manifest or the package version changed after the accepted decision "
        "(the differs: lines name which); restore the manifest and the package "
        "version that wrote the decision, finish the transition, then present "
        "the new change as its own decision"
    ),
    #: I2 §4.2: one instance takes one answer; two decisions naming the same
    #: `(code, digest)` with different effects are not a case `evolve` can
    #: pick between.
    "authorization_conflict": (
        "more than one decision names this refusal with different effects; remove all but one"
    ),
    #: I2 §5.3: the source of a `continue` carry is in this run directory and
    #: nothing writes it again, so a difference is damage to the directory.
    "rebound_source_corrupt": (
        "a frozen measurement changed after the condition change; restore it from backup "
        "-- the run directory cannot continue until it matches its declaration"
    ),
}


class AuthorizationError(ValueError):
    """`authorizations_invalid at <name>` (the directory) or `authorization_invalid at <key>`."""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(f"{code} at {path}")
        self.code = code
        self.path = path


class AuthorizationConflict(ValueError):
    """I2 §4.2: `authorization_conflict` -- two decisions name one instance with different effects.

    `ids` is every conflicting decision's id, in the order `match` found them,
    so the caller can print all of them (the door is to remove all but one).
    """

    def __init__(self, ids: tuple[str, ...]) -> None:
        super().__init__("authorization_conflict")
        self.ids = ids


def refusal_class(code: str) -> RefusalClass:
    return "authorizable" if code in AUTHORIZABLE_CODES else "hard"


_SHA = re.compile(r"[a-f0-9]{64}\Z")
#: The one `issued_at` grammar: a second-resolution UTC instant. Exported so the
#: journal's own `authorization` slot is checked by this exact expression rather
#: than by a length that any twenty characters would satisfy.
ISSUED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_STORED = ("schema", "code", "digest", "reason", "issued_at")
#: I2 §4.2, §4.3: the only key a decision file may carry beyond `_STORED`, and
#: only these four values -- `None` (the field absent) means `"close"`,
#: exactly as a decision written before I2 does.
_EFFECT_VALUES = frozenset({"default", "continue", "rebaseline", "close"})
DecisionEffect = Literal["default", "continue", "rebaseline", "close"]


def validate_issued_at(value: object) -> bool:
    """Is this an `issued_at` value -- the grammar *and* the instant?

    The expression above places the digits; it does not say they name a day or
    a time. `2026-99-99T99:99:99Z` matched it and was journaled verbatim as the
    moment an operator decided something, so the value is also
    parsed. `strptime` with this exact format accepts nothing the expression
    does not, which is why both run: the expression is the contract other
    modules import and compare against, and the parse is the calendar.
    """

    if type(value) is not str or ISSUED_AT.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def comparable_field(name: str, *, family_version: int | None = None) -> bool:
    """Does this dotted `changed_fields` entry name a comparable identity field?

    The one place the `identity.<owner>[.<key>]` grammar is decoded: both the
    record's declared list and the lifecycle's own computed diff ask here.
    Readers with a recorded family should supply it: the metric contract first
    becomes comparable in family 14. With no context, retain the current
    registry's behavior for standalone callers comparing newly built identities.
    """

    parts = name.split(".")
    return (
        len(parts) >= 2
        and parts[0] == "identity"
        and parts[1] in COMPARABLE_FIELDS
        and (parts[1] != "metric_contract" or family_version is None or family_version >= 14)
    )


@dataclass(frozen=True)
class Authorization:
    code: str
    digest: str
    reason: str
    issued_at: str
    source: Literal["caller", "file"] = "caller"
    #: I2 §4.2, §4.3: the operator's answer to a `run_identity_changed`
    #: instance -- `None` means `"close"`, exactly as a decision written
    #: before I2 does, so `id` below is unchanged for every decision that
    #: does not set this.
    effect: DecisionEffect | None = None

    @classmethod
    def new(
        cls,
        code: str,
        digest: str,
        reason: str,
        *,
        now: str | None = None,
        effect: DecisionEffect | None = None,
    ) -> Authorization:
        issued = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "schema": SCHEMA,
            "code": code,
            "digest": digest,
            "reason": reason,
            "issued_at": issued,
        }
        if effect is not None:
            payload["effect"] = effect
        return cls.from_payload(payload, source="caller")

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, source: Literal["caller", "file"]
    ) -> Authorization:
        if not isinstance(payload, Mapping):
            raise AuthorizationError("authorization_invalid", "$")
        # Not a stored key -- it is where the decision came from -- but it is
        # journaled beside the rest, so a library caller re-validating a
        # hand-built value here must not smuggle a source the record schema
        # would refuse much later.
        if source not in ("caller", "file"):
            raise AuthorizationError("authorization_invalid", "source")
        for key in payload:
            if key not in _STORED and key != "effect":
                raise AuthorizationError("authorization_invalid", str(key))
        for key in _STORED:
            if key not in payload or type(payload[key]) is not str:
                raise AuthorizationError("authorization_invalid", key)
        if payload["schema"] != SCHEMA:
            raise AuthorizationError("authorization_invalid", "schema")
        if payload["code"] not in AUTHORIZABLE_CODES:
            raise AuthorizationError("authorization_invalid", "code")
        if _SHA.fullmatch(payload["digest"]) is None:
            raise AuthorizationError("authorization_invalid", "digest")
        if not 1 <= len(payload["reason"]) <= MAX_REASON_CHARS:
            raise AuthorizationError("authorization_invalid", "reason")
        if not validate_issued_at(payload["issued_at"]):
            raise AuthorizationError("authorization_invalid", "issued_at")
        effect: DecisionEffect | None = None
        if "effect" in payload:
            candidate = payload["effect"]
            if type(candidate) is not str or candidate not in _EFFECT_VALUES:
                raise AuthorizationError("authorization_invalid", "effect")
            effect = candidate  # type: ignore[assignment]
        return cls(
            payload["code"],
            payload["digest"],
            payload["reason"],
            payload["issued_at"],
            source,
            effect,
        )

    def to_payload(self) -> dict[str, str]:
        payload = {
            "schema": SCHEMA,
            "code": self.code,
            "digest": self.digest,
            "reason": self.reason,
            "issued_at": self.issued_at,
        }
        if self.effect is not None:
            payload["effect"] = self.effect
        return payload

    @property
    def id(self) -> str:
        """Spec §5, amended I2 §4.2: sha256 over the canonical bytes of the decided fields.

        The four are `code`, `digest`, `reason` and `issued_at` -- what the
        operator decided -- and not `schema`, which is the file format's own
        key. Hashing the stored payload instead would make every id core
        produces differ from the documented algorithm, so a second
        implementation reading the same decision file would compute another id
        for it, and the ids in the journals, in `--list` and in a consequence
        record would correlate with nothing outside this package. `effect`
        joins the hashed fields only when present (I2
        §4.2), so the id of every decision written before I2 -- including
        those Lab writes -- is unchanged.
        """

        fields: dict[str, str] = {
            "code": self.code,
            "digest": self.digest,
            "reason": self.reason,
            "issued_at": self.issued_at,
        }
        if self.effect is not None:
            fields["effect"] = self.effect
        return hashlib.sha256(canonical_json(fields)).hexdigest()

    def resolved_effect(self, offered: Sequence[str]) -> str:
        """I2 §3.3, §4.2: what this decision does, once the instance's admissible effects are known.

        `None` (a decision written before I2, or one that named no effect)
        means `"close"`. `"default"` means the effect the operator named
        none of -- the first `offered`, the same one `RefusalFinding.effect`
        names and the refusal prints first. Any other value is the operator's
        own answer, unchanged.

        An instance always offers at least one effect; an empty `offered` is a
        caller's error and raises `ValueError` rather than resolving to
        `close` (or failing on `offered[0]`).
        """

        if not offered:
            raise ValueError("resolved_effect needs the instance's offered effects; none given")
        if self.effect is None:
            # A decision written before I2 keeps the one meaning it had: on a
            # comparable instance (every family-7 one offers `close`) that is
            # `close`; on an instance whose single effect is another -- a
            # non-comparable `rebase`, a family-6 owed `terminal` -- it is
            # that effect (I2 Task 7).
            return "close" if "close" in offered else offered[0]
        if self.effect == "default":
            return offered[0]
        return self.effect

    def journal_payload(self) -> dict[str, str]:
        """The `authorization` slot of a consequence record (§6).

        Never carries `effect`: this slot is also
        `identity_rebased`'s (a non-comparable "rebase" instance), whose
        `authorization` shape (`campaign_schema._validate_authorization`) is a
        closed four-key set unrelated to I2 and stays that way. `effect` is
        already bound into `id` above when present, so nothing this slot's
        readers need is lost; a reader that wants the operator's own `effect`
        value reads `decision_payload()` instead (the `authorization_applied`
        shapes that carry it: close, terminal, fresh_baseline, a linked-run
        decision).
        """

        return {
            "id": self.id,
            "reason": self.reason,
            "issued_at": self.issued_at,
            "source": self.source,
        }

    def decision_payload(self) -> dict[str, str]:
        """The full decision (§5.4, §7): what a record needs so no audit reads the file."""
        payload = {
            "id": self.id,
            "code": self.code,
            "digest": self.digest,
            "reason": self.reason,
            "issued_at": self.issued_at,
            "source": self.source,
        }
        if self.effect is not None:
            payload["effect"] = self.effect
        return payload

    @classmethod
    def from_decision_payload(cls, payload: Mapping[str, Any]) -> Authorization:
        """The inverse of `decision_payload`: refuses a payload whose `id` does not recompute.

        The `id` is not one of `Authorization.from_payload`'s stored fields --
        it is derived, not decided -- so it is checked here, against the id the
        four (or five, with `effect`) decided fields recompute, rather than
        trusted from the payload.
        """

        required = {"id", "code", "digest", "reason", "issued_at", "source"}
        if (
            not isinstance(payload, Mapping)
            or not required <= set(payload)
            or set(payload) - required - {"effect"}
        ):
            raise AuthorizationError("authorization_invalid", "$")
        from_payload: dict[str, Any] = {
            "schema": SCHEMA,
            **{k: payload[k] for k in ("code", "digest", "reason", "issued_at")},
        }
        if "effect" in payload:
            from_payload["effect"] = payload["effect"]
        decision = cls.from_payload(from_payload, source=payload["source"])
        if decision.id != payload["id"]:
            raise AuthorizationError("authorization_invalid", "id")
        return decision


@dataclass(frozen=True)
class RefusalDifference:
    field: str
    journaled: str | None
    observed: str | None


#: The difference a moved adaptation profile reports (ADR-0021): `/1` is
#: family 7 and `/2` family 8, so the move is a family change -- always hard,
#: journaled and observed carrying the two profile names.
ADAPTATION_PROFILE_FIELD = "adaptation.version"


def identity_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The comparable projection of a `campaign_created` or `identity_rebased` payload."""
    adaptation = payload.get("adaptation")
    return {
        "identity": dict(payload["identity"]),
        "identity_digest": str(payload["identity_digest"]),
        "producing_version": str(payload["producing_version"]),
        "adaptation_digest": None if adaptation is None else str(adaptation["digest"]),
    }


def _leaf(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return hashlib.sha256(canonical_json(value)).hexdigest()


def identity_differences(
    journaled: Mapping[str, Any], rebuilt: Mapping[str, Any]
) -> tuple[RefusalDifference, ...]:
    """Every field whose value differs, as dotted paths with digests or identifiers (§4)."""
    out: list[RefusalDifference] = []
    before, after = journaled["identity"], rebuilt["identity"]
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if isinstance(old, Mapping) and isinstance(new, Mapping) and name != "corpus_cases":
            for key in sorted(set(old) | set(new)):
                if old.get(key) != new.get(key):
                    out.append(
                        RefusalDifference(
                            f"identity.{name}.{key}", _leaf(old.get(key)), _leaf(new.get(key))
                        )
                    )
        else:
            out.append(RefusalDifference(f"identity.{name}", _leaf(old), _leaf(new)))
    for name in ("producing_version", "adaptation_digest"):
        if journaled.get(name) != rebuilt.get(name):
            label = "adaptation" if name == "adaptation_digest" else name
            out.append(
                RefusalDifference(label, _leaf(journaled.get(name)), _leaf(rebuilt.get(name)))
            )
    return tuple(out)


def comparable_changed(
    differences: Iterable[RefusalDifference], *, family_version: int | None = None
) -> bool:
    return any(
        comparable_field(difference.field, family_version=family_version)
        for difference in differences
    )


def _triple(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identity_digest": record["identity_digest"],
        "producing_version": record["producing_version"],
        "adaptation_digest": record.get("adaptation_digest"),
    }


def identity_instance_digest(journaled: Mapping[str, Any], rebuilt: Mapping[str, Any]) -> str:
    """Bound to both sides: an authorization for A→B never matches C→B (§4)."""
    return hashlib.sha256(
        canonical_json(
            {
                "code": "run_identity_changed",
                "journaled": _triple(journaled),
                "observed": _triple(rebuilt),
            }
        )
    ).hexdigest()


def hygiene_instance_digest(name: str, guard: str, fact: str) -> str:
    if guard not in GUARD_CLASSES:
        raise ValueError("unknown guard class")
    return hashlib.sha256(
        canonical_json({"code": "run_dir_unsafe_entry", "name": name, "guard": guard, "fact": fact})
    ).hexdigest()


def lineage_instance_digest(run_id: str, prefix_digest: str) -> str:
    """§5.4: a predecessor that grew since the refusal is a different instance.

    Bound to the code, the *predecessor's* `run_id` and its prefix digest, as
    §5.4 states. The decision itself lives in the successor's own run
    directory, so it is never offered to another successor's invocation.
    """

    return hashlib.sha256(
        canonical_json(
            {"code": "lineage_not_finished", "run_id": run_id, "prefix_digest": prefix_digest}
        )
    ).hexdigest()


def baseline_import_instance_digest(run_id: str, pairs_digest: str) -> str:
    """Baseline inheritance §5.3: bound to the code, *this* run's `run_id` and
    the declared `pairs_digest`, so a decision never releases another run's
    copy or a different declaration.
    """

    return hashlib.sha256(
        canonical_json(
            {"code": "baseline_import_unavailable", "run_id": run_id, "pairs_digest": pairs_digest}
        )
    ).hexdigest()


def match(
    authorizations: Iterable[Authorization],
    code: str,
    digest: str,
    *,
    offered: Sequence[str] | None = None,
) -> Authorization | None:
    """The one decision naming this instance, or `None` (§4).

    Every decision matching `(code, digest)` is collected -- across whatever
    order the caller's `authorizations` already combines journals,
    `authorizations`, `file_authorizations` and the decision directory (D2a
    §6) -- and their `effect` values are compared, `None` normalized to
    `"close"` for the comparison only (I2 §4.2: a decision without `effect`
    keeps meaning `"close"`). Two matches that disagree raise
    :class:`AuthorizationConflict`, naming every conflicting id, before this
    function returns anything; matches that agree -- including every case
    before I2, where there is at most one meaning to agree on -- return the
    first one, exactly as before.

    `offered` (ruling R5, I2 Task 7) is the instance's admissible effects,
    when the caller knows them: decisions are then compared by what they do
    (`resolved_effect`), so `"default"` beside the effect it resolves to is
    one answer, not two.
    """

    matches = [
        authorization
        for authorization in authorizations
        if authorization.code == code and authorization.digest == digest
    ]
    if not matches:
        return None
    # Ruling R5: a caller that knows the instance's admissible effects passes
    # them as `offered`, and decisions conflict only when what they *do*
    # differs (`resolved_effect`). Without `offered` the comparison stays the
    # fail-safe one, as written, so `"default"` beside an explicit effect is
    # flagged even when both would resolve alike.
    resolved = (
        {a.resolved_effect(offered) for a in matches}
        if offered is not None
        else {"close" if a.effect is None else a.effect for a in matches}
    )
    if len(resolved) > 1:
        raise AuthorizationConflict(tuple(a.id for a in matches))
    return matches[0]


def read_decisions(run_dir: Path) -> tuple[Authorization, ...]:
    """Every decision in `<run_dir>/authorizations/`, deduplicated by id; `()` without it (§7).

    The thin half: it opens the directory and hands the descriptor to
    :func:`read_decisions_fd`, which is where every check and every read
    happens. `evo tree authorize --list` and a library caller that reads its
    own directory keep this shape; `evolve` opens the directory itself, through
    the run root it canonicalized (PDR-0010).
    """

    directory = Path(run_dir) / AUTHORIZATIONS_DIR
    if not os.path.lexists(directory):
        return ()
    # `O_NOFOLLOW` is the symlink and the not-a-directory check in one: a
    # symlink fails with `ELOOP` and a regular file with `ENOTDIR`, both before
    # anything is read, and the descriptor that comes back names the directory
    # those checks were made against rather than whatever the name resolves to
    # at the next call (PDR-0010).
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise AuthorizationError("authorizations_invalid", AUTHORIZATIONS_DIR) from exc
    try:
        return read_decisions_fd(directory_fd)
    finally:
        os.close(directory_fd)


def read_decisions_fd(directory_fd: int) -> tuple[Authorization, ...]:
    """Every decision under an already-opened `authorizations/`, deduplicated by id (§7).

    One descriptor is the whole directory: it is listed with `os.listdir(fd)`
    and every entry is opened relative to it with `O_NOFOLLOW`, so an entry
    swapped for a symlink -- or the directory itself swapped for another --
    between the listing and the read cannot redirect what is read. Each file is
    then judged by `fstat` on the descriptor that will be read, not by an
    `lstat` on a name that may already mean something else.
    """

    if os.fstat(directory_fd).st_uid != os.geteuid():
        raise AuthorizationError("authorizations_invalid", AUTHORIZATIONS_DIR)
    names = sorted(os.listdir(directory_fd))
    if len(names) > MAX_DECISIONS:
        raise AuthorizationError("authorizations_invalid", AUTHORIZATIONS_DIR)
    decisions: dict[str, Authorization] = {}
    for name in names:
        if name.startswith(TEMPORARY_PREFIX) and _staged_file(directory_fd, name):
            # `write_decision`'s own half-written file, left by a crash between
            # the staged file and its rename. It was never a decision -- no run
            # can have consumed it and no operator wrote it under that name --
            # so it is skipped rather than refused: one crashed `authorize`
            # must not make every later `evolve` refuse the directory until an
            # operator finds a dotfile.
            #
            # The name alone does not earn that exemption (ADR-0015): a
            # symlink, a subdirectory or a foreign-owned entry
            # under this prefix is nothing `write_decision` can have left, and
            # ADR-0015 refuses every malformed or non-file entry of the
            # directory. Only what the writer could actually have staged -- a
            # regular file this user owns -- is skipped; everything else falls
            # through to the validation below and refuses like any other entry.
            continue
        # `O_NONBLOCK` so a fifo an operator left in the directory is refused by
        # the `S_ISREG` check below instead of blocking the open forever; it
        # changes nothing about a regular file.
        try:
            entry_fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
        except OSError as exc:
            raise AuthorizationError("authorizations_invalid", name) from exc
        try:
            entry = os.fstat(entry_fd)
            if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.geteuid():
                raise AuthorizationError("authorizations_invalid", name)
            if entry.st_size > MAX_DECISION_BYTES:
                raise AuthorizationError("authorizations_invalid", name)
            try:
                payload = json.loads(_read_fd(entry_fd, entry.st_size))
                decision = Authorization.from_payload(payload, source="file")
            except (ValueError, AuthorizationError, OSError) as exc:
                raise AuthorizationError("authorizations_invalid", name) from exc
        finally:
            os.close(entry_fd)
        decisions.setdefault(decision.id, decision)
    return tuple(decisions.values())


def _staged_file(directory_fd: int, name: str) -> bool:
    """Could `write_decision` have left this entry behind? (ADR-0015 §7)

    `lstat` through the directory descriptor, so the answer is about the entry
    itself and never about what a symlink under the prefix points at. An entry
    that has already gone counts: renaming the staged file onto its final name
    is the *successful* end of `write_decision`, so a concurrent `authorize`
    makes a listed temporary name vanish routinely, and refusing the whole
    directory for it would be the failure mode this skip exists to prevent.
    Every other `OSError` is not a staged file and falls through to the
    validation below.
    """

    try:
        entry = os.lstat(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(entry.st_mode) and entry.st_uid == os.geteuid()


def _read_fd(fd: int, size: int) -> bytes:
    """The whole file, from the descriptor `fstat` judged, bounded by its size."""

    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_decision(run_dir: Path, authorization: Authorization) -> Path:
    """One file per decision, whole or absent: a temporary name and one rename (§7)."""
    run = Path(run_dir)
    # `evolve` creates no ancestor of a run directory -- an absent parent is its
    # own `run_dir_parent_missing` -- and a decision written before the first
    # run must not create a tree that run would then refuse. The parent is the
    # operator's; the leaf and its `authorizations/` are this command's.
    if not run.parent.is_dir():
        raise AuthorizationError("authorization_invalid", "run_dir")
    directory = run / AUTHORIZATIONS_DIR
    for path in (run, directory):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    # The `mkdir`s above tolerate an entry that already exists, which is how a
    # second decision joins the first -- but "exists" is not "is the directory
    # this command owns", and a check by path answers about whatever the name
    # resolves to at the instant of the check. Everything below goes through
    # *one* descriptor instead: the run directory is opened by path (a symlinked
    # run directory is the operator's own arrangement, which `evolve` admits
    # under an authorization, so this one is followed), `authorizations/` is
    # opened relative to it with `O_NOFOLLOW` -- a symlink there fails the open
    # rather than redirecting the decision -- and the ownership check, the
    # `0o700`, the staged file and the rename are all made against that
    # descriptor. An `authorizations/` swapped for a symlink after the open
    # cannot move any of them: the descriptor still names the directory that
    # was checked (PDR-0010).
    try:
        run_fd = os.open(run, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise AuthorizationError("authorization_invalid", "run_dir") from exc
    try:
        try:
            directory_fd = os.open(
                AUTHORIZATIONS_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=run_fd,
            )
        except OSError as exc:
            raise AuthorizationError("authorization_invalid", AUTHORIZATIONS_DIR) from exc
        try:
            if os.fstat(directory_fd).st_uid != os.geteuid():
                raise AuthorizationError("authorization_invalid", AUTHORIZATIONS_DIR)
            os.chmod(directory_fd, 0o700)
            name = f"{authorization.id}.json"
            # `mkstemp` takes a directory *path*; the name is generated here so
            # the staged file is created relative to the descriptor instead.
            temporary = f"{TEMPORARY_PREFIX}{secrets.token_hex(8)}"
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(canonical_json(authorization.to_payload()) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
                raise
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(run_fd)
    return directory / name


def hint_command(run_dir: str, code: str, digest: str) -> str:
    """The `evo tree authorize` line a refused `evolve` prints (§4).

    The run directory is quoted for the shell: it is the operator's own
    argument echoed back, and the one token of this line that can hold a space
    or a metacharacter -- the code and the digest are identifiers core produced.
    `shlex.quote` leaves an ordinary path exactly as it was typed, so the
    common line is unchanged and only a path that needed quoting gains it.
    """

    return f'evo tree authorize {shlex.quote(run_dir)} {code} --digest {digest} --reason "<why>"'


def hint_command_accept(
    run_dir: str, accepted: Sequence[tuple[str, str]], effect: str | None = None
) -> str:
    """§6: the one command a multi-finding refusal prints, one `--accept` per finding.

    I2 §4.1: `effect` adds `--effect <name>`, which the CLI attaches to the
    `run_identity_changed` item among them; `None` is the default effect.
    """

    flags = " ".join(f"--accept {code}:{digest}" for code, digest in accepted)
    if effect is not None:
        flags = f"{flags} --effect {effect}"
    return f'evo tree authorize {shlex.quote(run_dir)} {flags} --reason "<why>"'
