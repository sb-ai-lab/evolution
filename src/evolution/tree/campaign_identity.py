"""C1 campaign identity, measurement derivation and capability admission.

`CampaignIdentityV1` (profile `campaign`) freezes everything a campaign run
needs to derive every measurement it will ever open: the campaign policy, the
adapter's preflight-declared `SemanticIdentity` (ADR-0007), the Environment
and scorer bindings, the corpus binding, the S2 store identity, the
materialization strategy, the admitted target capabilities, the editor
identity and, for a baseline-recovery continuation, the predecessor's
inherited ledger (`LinkedFrom`, spec §7). It follows the same value-object
style as S1's `RunIdentityV1`: a closed field set, canonical bytes, a stable
digest, and `to_payload`/`from_payload` round trips, with no state outside
those canonical bytes. `IdentityError` (S1's own class) carries every
refusal here too, so a campaign identity failure and a measurement identity
failure share one diagnostic vocabulary.

`measurement_identity` derives a fresh S1 `RunIdentityV1` for a Candidate
measurement from the campaign identity and an explicit `corpus` argument
(spec §4: target, environment, scorer, evaluation_set_id, corpus_digest,
metric_plan_digest, budget, concurrency and materialization strategy are all
copied from the campaign identity; the schedule is the full corpus x repeats
schedule built by `schedule_for` from the supplied corpus). It verifies
`corpus_digest(corpus) == identity.corpus_digest` first and raises
`IdentityError("run_identity_changed")` on a mismatch, the same check S1's
`RunIdentityV1.validate_corpus` makes — the identity itself never retains the
corpus it was built with — and then delegates the actual derivation to
`derived_identity`, which needs no corpus at all: it reads the `(case_id,
group_id, stratum)` schedule input straight from the identity's own
`corpus_cases` (spec §3, ADR-0008), which is what lets an audit over the run
root re-derive a measurement's identity from the identity alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from dataclasses import field as _context_field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.attestation import TargetPreflight
from evolution.tree.campaign_caveats import LINEAGE_CAVEAT_KINDS, LineageCaveat
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    PolicyError,
    policy_class_for_payload,
)
from evolution.tree.execution import config as _config
from evolution.tree.execution.authorizations import (
    Authorization,
    AuthorizationError,
    comparable_field,
    lineage_instance_digest,
)
from evolution.tree.execution.interaction import (
    AdapterCapabilities,
    EnvironmentIdentity,
    EvalCase,
    SemanticIdentity,
)
from evolution.tree.execution.journal_identity import (
    IdentityError,
    RunIdentityV1,
    canonical_json,
    checked_case_repeats,
    checked_case_subset,
    corpus_digest,
    effective_scorer_identity,
    scorer_binding,
)
from evolution.tree.gate_scope import DEFAULT_MIN_CLAIM_CASES
from evolution.tree.storage.envelope import is_identifier

if TYPE_CHECKING:
    from evolution.tree.target.config import ProbeSection

from evolution.tree.roles.config import GROUPING_V2_MINIMUMS
from evolution.tree.roles.identities import EditorIdentity, ReflectorIdentity

Side = Literal["champion", "candidate"]

_FIELDS = (
    "profile policy policy_digest target environment scorer evaluation_set_id "
    "corpus_digest corpus_cases metric_plan_digest artifact_store materialization_strategy "
    "capabilities editor reflector linked_from"
)
#: Family 14 (family-14 implementation design R17, M8): the digests of the
#: canonical `metric_contract/1` and `role_visibility/1` objects. Both are
#: present in a family-14 identity and absent from every older one, so an
#: identity of families up to 13 keeps its bytes. The family rule is the
#: journal's (creation and every rebase); the value object admits
#: either shape. ADR-0038's optional `verbatim_answers` digest is not one of
#: them: it rides beside them only when `/15` declares it, and is neither
#: required nor comparable.
CONNECTION_FIELDS = ("metric_contract", "role_visibility")
#: The first campaign family whose identity carries `CONNECTION_FIELDS` (R17).
#: `campaign_schema.FINAL_CAMPAIGN_FAMILY_VERSION` is the same number; it is not
#: imported here because `campaign_schema` imports this module.
CONNECTION_FAMILY_VERSION: Final = 14
_CASE_KEYS = "case_id group_id stratum"
# Derived from the R1 dataclass, not copied from S1's own `_TARGET` string, so the
# two field lists cannot drift apart.
_TARGET = " ".join(f.name for f in fields(SemanticIdentity))
_ENV_SCORER_KEYS = "kind digest"
_CAPABILITIES_KEYS = "max_instances max_concurrent_sessions"
_LINKED_FIELDS = (
    "run_id prefix_digest final_champion_digest final_champion_dependency_digest "
    "rounds_closed alpha_rounds indeterminate_counts denominator"
)
#: Family-6-only `LinkedFrom` keys, present only when set.
_LINKED_OPTIONAL = (
    "first_round",
    "predecessor_family",
    "condition_changed",
    "lineage_caveats",
    "predecessor_finished",
    "authorization",
    "baseline",
)
_BASELINE_FIELDS = (
    "mode source_measurement_id source_prefix_digest pairs_digest "
    "imported scheduled fields origin_runs"
)
_FIELD_RE = re.compile(r"[a-z_]+(\.[a-z_]+){1,2}")


#: A baseline declaration's `fields` also name the top-level identity fields an
#: in-place change moved (`condition_changes[].fields`, I2 spec §4.4, §6.3): a
#: row carried across a `continue` records them, and a successor inheriting
#: from a predecessor measurement older than its latest epoch names them too.
#: A bare name is admitted only when it is one of those comparable top-level
#: fields of families up to 13; anything else keeps the dotted grammar.
#: `family_version` is the successor's family when the caller knows it (H12a):
#: from family 14 the bare comparable name `metric_contract` is admitted too.
#: Without it the legacy family-13 grammar applies.
def _baseline_field(name: str, *, family_version: int | None = None) -> bool:
    family = 13 if family_version is None else family_version
    return (
        "." not in name and comparable_field(f"identity.{name}", family_version=family)
    ) or _FIELD_RE.fullmatch(name) is not None


def _link_context(identity_payload: Any) -> int | None:
    """The successor family a stored identity implies for its `linked_from`.

    `CONNECTION_FAMILY_VERSION` exactly when the identity carries the
    connection fields (family 14), else `None` (legacy grammar).
    """

    if isinstance(identity_payload, Mapping) and any(
        name in identity_payload for name in CONNECTION_FIELDS
    ):
        return CONNECTION_FAMILY_VERSION
    return None


_INDETERMINATE_OWNERS = frozenset({"target", "environment", "scorer", "rollout"})
_DECIMAL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?\Z")
_SHA256_RE = re.compile("[0-9a-f]{64}")
_MATERIALIZATION = ("adapter_apply_manifest", "filesystem_overlay", "artifact_root_injection")
_PROBE_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})


class CapabilitiesInsufficient(ValueError):
    """The adapter's admitted capabilities cannot serve the policy's peak demand."""


# The next four validators mirror S1's own private rules
# (`journal_identity._closed/_identifier/_positive/_sha`) rather than importing
# them: an underscore-prefixed name is not a public API, and C1 does not couple
# itself to S1's unstable internals for a handful of one-line checks.


def _closed(value: Any, fields_str: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields_str.split()):
        raise IdentityError("invalid_identity_fields")
    return dict(value)


def _identifier(value: Any) -> None:
    if type(value) is not str or not is_identifier(value):
        raise IdentityError("invalid_identity_identifier")


def _positive(value: Any) -> None:
    if type(value) is not int or value < 1:
        raise IdentityError("invalid_identity_integer")


def _sha(value: Any) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise IdentityError("invalid_identity_digest")


def _text(value: Any) -> None:
    if type(value) is not str or not value.strip():
        raise IdentityError("invalid_identity_text")


def _nonneg_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise IdentityError("invalid_identity_integer")
    return value


def _decimal_string(value: Any) -> str:
    if not isinstance(value, str) or _DECIMAL_RE.match(value) is None:
        raise IdentityError("invalid_identity_decimal")
    return value


def _probe_identity(value: Any) -> dict[str, Any]:
    data = _closed(
        value,
        "base_url health_path methods budget request_timeout_seconds description_sha256"
        + (" string_body" if isinstance(value, Mapping) and "string_body" in value else "")
        + (" on_unavailable" if isinstance(value, Mapping) and "on_unavailable" in value else ""),
    )
    if "string_body" in data and data["string_body"] not in ("send", "parse"):
        raise IdentityError("invalid_identity_fields")
    if "on_unavailable" in data and data["on_unavailable"] not in ("suspend", "continue"):
        raise IdentityError("invalid_identity_fields")
    if type(data["base_url"]) is not str or not data["base_url"]:
        raise IdentityError("invalid_identity_text")
    if type(data["health_path"]) is not str or not data["health_path"]:
        raise IdentityError("invalid_identity_text")
    methods = data["methods"]
    if (
        not isinstance(methods, list)
        or not methods
        or any(type(method) is not str or method not in _PROBE_METHODS for method in methods)
        or len(set(methods)) != len(methods)
    ):
        raise IdentityError("invalid_identity_fields")
    _positive(data["budget"])
    timeout = data["request_timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise IdentityError("invalid_identity_decimal")
    data["request_timeout_seconds"] = float(timeout)
    if data["description_sha256"] is not None:
        _sha(data["description_sha256"])
    return data


def probe_identity_payload(probe: ProbeSection | None) -> dict[str, Any] | None:
    """Serialize declared probe semantics without runtime access details."""

    if probe is None:
        return None
    description = probe.description
    return {
        **({"on_unavailable": probe.on_unavailable} if probe.on_unavailable is not None else {}),
        **({"string_body": probe.string_body} if probe.string_body is not None else {}),
        "base_url": probe.base_url,
        "health_path": probe.health_path,
        "methods": list(probe.methods),
        "budget": probe.budget,
        "request_timeout_seconds": probe.request_timeout_seconds,
        "description_sha256": (
            None if description is None else hashlib.sha256(description.encode("utf-8")).hexdigest()
        ),
    }


def probe_continues(identity: Mapping[str, Any], family_version: int) -> bool:
    """Only an explicit family-15 binding relaxes probe suspension rules."""
    if family_version != 15:
        return False
    probe = identity["reflector"]["probe"]
    return probe is not None and probe.get("on_unavailable") == "continue"


def _corpus_cases(value: Any) -> list[dict[str, str]]:
    """Normalize `corpus_cases` to `{case_id, group_id, stratum}` in corpus order.

    Duplicate `case_id`s raise `invalid_corpus`, the same code S1's own
    `corpus_digest` uses for the same fault, so the two corpus-shape checks share
    one diagnostic vocabulary.
    """

    if not isinstance(value, list) or not value:
        raise IdentityError("invalid_corpus")
    seen: set[str] = set()
    cases: list[dict[str, str]] = []
    for entry in value:
        row = _closed(entry, _CASE_KEYS)
        _identifier(row["case_id"])
        _text(row["group_id"])
        _text(row["stratum"])
        if row["case_id"] in seen:
            raise IdentityError("invalid_corpus")
        seen.add(row["case_id"])
        cases.append(dict(case_id=row["case_id"], group_id=row["group_id"], stratum=row["stratum"]))
    return cases


@dataclass(frozen=True)
class LinkedFrom:
    """The predecessor's inherited ledger for a baseline-recovery continuation (spec §7).

    `final_champion_dependency_digest` is not in the spec §4 table's short list, but
    the §7 comparability rule requires the caller's seed to scan to *both* the
    predecessor's final champion candidate digest and its dependency digest, so both
    travel with the identity.

    `first_round` is the family-6 successor's first round (continuable-terminals
    spec §5.2): one more than the highest round any predecessor record names,
    which can exceed `rounds_closed + 1` when a terminal interrupted a round. It
    is optional and emitted only when set, so a family-5 identity keeps its
    payload and its digest.

    `predecessor_family` is the campaign family of the predecessor journal the
    link was derived from (continuable-terminals spec §8 reports it as
    `linked_from.family`). Like `first_round` it is family-6 only, optional and
    emitted only when set.

    The D2a keys (spec §4, §9) are all optional and emitted only when
    set/non-empty, so a family-6 link written by `0.3.0a13` keeps its exact
    payload and digest: `condition_changed` is the sorted, unique, dotted
    fields a comparability check found different (`differs:` lines);
    `lineage_caveats` is the bounded per-kind aggregate the lineage carries
    forward (`campaign_caveats.aggregate_lineage`); `predecessor_finished`
    is `False` only when the predecessor is an unfinished family-5 campaign
    linked under an accepted decision, never `True`; `authorization` is that
    decision's `decision_payload()` form, present iff `predecessor_finished
    is False`.

    `baseline` (spec §5.1) is the creation-time declaration of a baseline
    inheritance: present only when the manifest declared `baseline: inherit`
    (a `fresh` link, from `/5` or a `/6` document, never sets it), with the
    closed key set `mode` (always `"inherit"`), `source_measurement_id`,
    `source_prefix_digest`, `pairs_digest`, `imported`, `scheduled`, `fields`
    and `origin_runs`. Ruling R1: an empty admission set still journals it,
    with `imported == 0`, `origin_runs == 0` and `fields == []`.
    """

    run_id: str
    prefix_digest: str
    final_champion_digest: str
    final_champion_dependency_digest: str
    rounds_closed: int
    alpha_rounds: Mapping[int, str]
    indeterminate_counts: Mapping[str, int]
    denominator: int
    first_round: int | None = None
    predecessor_family: int | None = None
    condition_changed: tuple[str, ...] = ()
    lineage_caveats: tuple[LineageCaveat, ...] = ()
    predecessor_finished: bool | None = None
    authorization: Mapping[str, str] | None = None
    baseline: Mapping[str, Any] | None = None
    #: H12a: the successor's campaign family, when the caller knows it. Context
    #: only -- never in `to_payload`, never compared -- so every payload and
    #: digest is unchanged. `None` keeps the legacy context-free grammar.
    successor_family: int | None = _context_field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _identifier(self.run_id)
        for value in (
            self.prefix_digest,
            self.final_champion_digest,
            self.final_champion_dependency_digest,
        ):
            _sha(value)
        _nonneg_int(self.rounds_closed)
        _nonneg_int(self.denominator)
        if self.first_round is not None:
            _positive(self.first_round)
            if self.first_round <= self.rounds_closed:
                raise IdentityError("invalid_identity_integer")
        if self.predecessor_family is not None:
            _positive(self.predecessor_family)
        fields_ = tuple(self.condition_changed)
        if list(fields_) != sorted(set(fields_)) or any(
            _FIELD_RE.fullmatch(name) is None for name in fields_
        ):
            raise IdentityError("invalid_identity_fields")
        object.__setattr__(self, "condition_changed", fields_)
        caveats = tuple(self.lineage_caveats)
        kinds = {c.kind for c in caveats}
        if [c.kind for c in caveats] != [k for k in LINEAGE_CAVEAT_KINDS if k in kinds]:
            raise IdentityError("invalid_identity_fields")
        for caveat in caveats:
            _positive(caveat.runs)
            _identifier(caveat.nearest_run)
            if list(caveat.fields) != sorted(set(caveat.fields)):
                raise IdentityError("invalid_identity_fields")
        object.__setattr__(self, "lineage_caveats", caveats)
        if self.predecessor_finished is True:
            raise IdentityError("invalid_identity_fields")  # absent, never true
        if (self.predecessor_finished is False) != (self.authorization is not None):
            raise IdentityError("invalid_identity_fields")
        if self.authorization is not None:
            try:
                decision = Authorization.from_decision_payload(self.authorization)
            except AuthorizationError:
                raise IdentityError("invalid_identity_fields") from None
            if decision.code != "lineage_not_finished":
                raise IdentityError("invalid_identity_fields")
            # The decision names this predecessor at this prefix (§5.4), and
            # nothing else: one accepted for another run, or for this run
            # before it grew, never rides inside a creation record.
            if decision.digest != lineage_instance_digest(self.run_id, self.prefix_digest):
                raise IdentityError("invalid_identity_fields")
            object.__setattr__(self, "authorization", MappingProxyType(dict(self.authorization)))
        alpha_rounds: dict[int, str] = {}
        for look, share in dict(self.alpha_rounds).items():
            if type(look) is not int or look < 1:
                raise IdentityError("invalid_identity_integer")
            alpha_rounds[look] = _decimal_string(share)
        counts = dict(self.indeterminate_counts)
        if set(counts) != _INDETERMINATE_OWNERS:
            raise IdentityError("invalid_identity_fields")
        for count in counts.values():
            _nonneg_int(count)
        if self.baseline is not None:
            baseline = _closed(self.baseline, _BASELINE_FIELDS)
            if baseline["mode"] != "inherit":
                raise IdentityError("invalid_identity_fields")
            _identifier(baseline["source_measurement_id"])
            _sha(baseline["source_prefix_digest"])
            _sha(baseline["pairs_digest"])
            imported, scheduled = baseline["imported"], baseline["scheduled"]
            if type(imported) is not int or type(scheduled) is not int:
                raise IdentityError("invalid_identity_integer")
            if scheduled < 1 or not (0 <= imported <= scheduled):
                raise IdentityError("invalid_identity_integer")
            baseline_fields = tuple(baseline["fields"])
            if list(baseline_fields) != sorted(set(baseline_fields)) or any(
                not _baseline_field(name, family_version=self.successor_family)
                for name in baseline_fields
            ):
                raise IdentityError("invalid_identity_fields")
            origin_runs = baseline["origin_runs"]
            if type(origin_runs) is not int or origin_runs < 0:
                raise IdentityError("invalid_identity_integer")
            if (origin_runs == 0) != (imported == 0):
                raise IdentityError("invalid_identity_fields")
            object.__setattr__(
                self,
                "baseline",
                MappingProxyType(
                    dict(
                        mode="inherit",
                        source_measurement_id=baseline["source_measurement_id"],
                        source_prefix_digest=baseline["source_prefix_digest"],
                        pairs_digest=baseline["pairs_digest"],
                        imported=imported,
                        scheduled=scheduled,
                        fields=baseline_fields,
                        origin_runs=origin_runs,
                    )
                ),
            )
        # PDR-0003: `linked_from` is journaled inside `campaign_created.identity`,
        # so its two mappings are frozen views over private copies for the same
        # reason the policy's are -- nothing a caller still holds can move the
        # inherited totals behind the digest.
        object.__setattr__(
            self,
            "alpha_rounds",
            MappingProxyType({look: alpha_rounds[look] for look in sorted(alpha_rounds)}),
        )
        object.__setattr__(
            self,
            "indeterminate_counts",
            MappingProxyType({owner: counts[owner] for owner in sorted(counts)}),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = dict(
            run_id=self.run_id,
            prefix_digest=self.prefix_digest,
            final_champion_digest=self.final_champion_digest,
            final_champion_dependency_digest=self.final_champion_dependency_digest,
            rounds_closed=self.rounds_closed,
            alpha_rounds={str(look): share for look, share in self.alpha_rounds.items()},
            indeterminate_counts=dict(self.indeterminate_counts),
            denominator=self.denominator,
        )
        if self.first_round is not None:
            payload["first_round"] = self.first_round
        if self.predecessor_family is not None:
            payload["predecessor_family"] = self.predecessor_family
        if self.condition_changed:
            payload["condition_changed"] = list(self.condition_changed)
        if self.lineage_caveats:
            payload["lineage_caveats"] = [caveat.to_payload() for caveat in self.lineage_caveats]
        if self.predecessor_finished is False:
            payload["predecessor_finished"] = False
        if self.authorization is not None:
            payload["authorization"] = dict(self.authorization)
        if self.baseline is not None:
            baseline = dict(self.baseline)
            baseline["fields"] = list(baseline["fields"])
            payload["baseline"] = baseline
        return payload

    @classmethod
    def from_payload(cls, payload: Any, *, successor_family: int | None = None) -> LinkedFrom:
        optional = [
            name for name in _LINKED_OPTIONAL if isinstance(payload, Mapping) and name in payload
        ]
        data = _closed(payload, " ".join((_LINKED_FIELDS, *optional)))
        if any(data[name] is None for name in optional):
            # Absent, never null: a null would round-trip to a different payload.
            raise IdentityError("invalid_identity_integer")
        alpha_raw = data["alpha_rounds"]
        if not isinstance(alpha_raw, Mapping):
            raise IdentityError("invalid_identity_fields")
        alpha_rounds: dict[int, str] = {}
        for key, value in alpha_raw.items():
            if not isinstance(key, str) or not key.isdecimal():
                raise IdentityError("invalid_identity_integer")
            alpha_rounds[int(key)] = value
        counts = data["indeterminate_counts"]
        if not isinstance(counts, Mapping):
            raise IdentityError("invalid_identity_fields")
        lineage_caveats: tuple[LineageCaveat, ...] = ()
        if "lineage_caveats" in data:
            raw_caveats = data["lineage_caveats"]
            if not isinstance(raw_caveats, list):
                raise IdentityError("invalid_identity_fields")
            try:
                lineage_caveats = tuple(LineageCaveat.from_payload(item) for item in raw_caveats)
            except (KeyError, TypeError, ValueError) as exc:
                raise IdentityError("invalid_identity_fields") from exc
        return cls(
            run_id=data["run_id"],
            prefix_digest=data["prefix_digest"],
            final_champion_digest=data["final_champion_digest"],
            final_champion_dependency_digest=data["final_champion_dependency_digest"],
            rounds_closed=data["rounds_closed"],
            alpha_rounds=alpha_rounds,
            indeterminate_counts=dict(counts),
            denominator=data["denominator"],
            first_round=data.get("first_round"),
            predecessor_family=data.get("predecessor_family"),
            condition_changed=tuple(data.get("condition_changed", ())),
            lineage_caveats=lineage_caveats,
            predecessor_finished=data.get("predecessor_finished"),
            authorization=data.get("authorization"),
            baseline=data.get("baseline"),
            successor_family=successor_family,
        )


def metric_plan_digest(primary: str) -> str:
    return hashlib.sha256(canonical_json({"schema_version": 1, "primary": primary})).hexdigest()


def evaluation_set_id(corpus: Sequence[EvalCase]) -> str:
    return "corpus-" + corpus_digest(corpus)[:16]


@dataclass(frozen=True, init=False)
class CampaignIdentityV1:
    """Canonical bytes prevent mutation through nested objects or caller aliases.

    A value object with a digest carries no state outside those canonical bytes:
    the identity never retains the corpus it was built with, so every schedule
    derivation (`measurement_identity`) takes the corpus explicitly and verifies it
    against `corpus_digest` before using it.

    The optional `verbatim_answers` digest (ADR-0038) is admitted only beside
    both `CONNECTION_FIELDS`, so no identity older than family 14 can carry it.
    """

    _canonical: bytes

    def __init__(self, payload: Mapping[str, Any]) -> None:
        present = [
            name for name in CONNECTION_FIELDS if isinstance(payload, Mapping) and name in payload
        ]
        if present and len(present) != len(CONNECTION_FIELDS):
            raise IdentityError("invalid_identity_fields")
        # ADR-0038: the optional, non-comparable digest of `/15`'s
        # `verbatim_answers/1`, admitted only beside the connection fields
        # (family 14); absent, the identity keeps its b1/b2 bytes.
        verbatim = (
            " verbatim_answers"
            if present and isinstance(payload, Mapping) and "verbatim_answers" in payload
            else ""
        )
        secret_paths = (
            " secret_oracle_paths"
            if present and isinstance(payload, Mapping) and "secret_oracle_paths" in payload
            else ""
        )
        checks = " checks" if present and "checks" in payload else ""
        preflight_retries = (
            " preflight_sample_retries" if present and "preflight_sample_retries" in payload else ""
        )
        data = _closed(
            payload,
            _FIELDS
            + "".join(f" {name}" for name in present)
            + verbatim
            + secret_paths
            + checks
            + preflight_retries,
        )
        for name in present:
            _sha(data[name])
        if verbatim:
            _sha(data["verbatim_answers"])
        if secret_paths:
            _sha(data["secret_oracle_paths"])
        if checks:
            _sha(data["checks"])
        if preflight_retries:
            count = data["preflight_sample_retries"]
            if type(count) is not int or count < 0:
                raise IdentityError("invalid_identity_integer")
        if data["profile"] != "campaign":
            raise IdentityError("invalid_identity_profile")
        try:
            raw_policy = data["policy"]
            policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
        except PolicyError as exc:
            raise IdentityError("invalid_identity_policy") from exc
        if policy.digest != data["policy_digest"]:
            raise IdentityError("invalid_identity_digest")
        target = _closed(data["target"], _TARGET)
        for value in target.values():
            _identifier(value)
        if target["seed_mode"] not in ("exact", "unpaired", "none"):
            raise IdentityError("invalid_seed_mode")
        env = _closed(data["environment"], _ENV_SCORER_KEYS)
        for value in env.values():
            _identifier(value)
        # `effective_scorer_identity` applies S1's own scorer-shape validation
        # (closed keys, identifier-shaped kind/digest, a legal recovery mode) and
        # raises `IdentityError` on a bad shape; its returned `ScorerIdentity` is not
        # what this payload stores, so it is discarded.
        effective_scorer_identity(data["scorer"])
        _identifier(data["evaluation_set_id"])
        _sha(data["corpus_digest"])
        data["corpus_cases"] = _corpus_cases(data["corpus_cases"])
        _sha(data["metric_plan_digest"])
        try:
            ArtifactStoreIdentityV1.from_payload(data["artifact_store"])
        except (ValueError, TypeError) as exc:
            # `ArtifactPolicyV1.from_payload` does not isinstance-guard its own
            # `payload` before calling `set(payload)`, so a non-iterable nested
            # `policy` value (an int, `None`, ...) raises `TypeError` rather than
            # `ValueError`; both must land as the same safe `IdentityError` here.
            raise IdentityError("invalid_identity_artifact_store") from exc
        if data["materialization_strategy"] not in _MATERIALIZATION:
            raise IdentityError("invalid_materialization_strategy")
        capabilities = _closed(data["capabilities"], _CAPABILITIES_KEYS)
        for value in capabilities.values():
            _positive(value)
        editor = _closed(data["editor"], _ENV_SCORER_KEYS)
        for value in editor.values():
            _identifier(value)
        reflector = _closed(data["reflector"], "kind digest environment_docs_digest probe")
        _identifier(reflector["kind"])
        _identifier(reflector["digest"])
        if reflector["environment_docs_digest"] is not None:
            _sha(reflector["environment_docs_digest"])
        if reflector["probe"] is not None:
            reflector["probe"] = _probe_identity(reflector["probe"])
        data["reflector"] = reflector
        if data["linked_from"] is not None:
            data["linked_from"] = LinkedFrom.from_payload(
                data["linked_from"],
                successor_family=CONNECTION_FAMILY_VERSION if present else None,
            ).to_payload()
        object.__setattr__(self, "_canonical", canonical_json(data))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CampaignIdentityV1:
        return cls(payload)

    def to_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self._canonical)
        return result

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def corpus_digest(self) -> str:
        return str(self.to_payload()["corpus_digest"])

    @property
    def corpus_cases(self) -> tuple[Mapping[str, str], ...]:
        return tuple(MappingProxyType(dict(case)) for case in self.to_payload()["corpus_cases"])


_ADAPTATION_FIELDS = (
    "profile grouping selection families attempt_feedback reflection_metadata_digest "
    "environment_docs_digest reader_protocol"
)
_ADAPTATION_FIELDS_V2 = _ADAPTATION_FIELDS + " family_membership"
_ADAPTATION_PROFILES = (
    "campaign_adaptation/1",
    "campaign_adaptation/2",
    "campaign_adaptation/3",
    "campaign_adaptation/4",
    "campaign_adaptation/5",
    "campaign_adaptation/6",
    "campaign_adaptation/7",
)
#: Family 14 (family-14 implementation design R18): `/6`'s fields, whose
#: `grouping` slot is the Grouper identity of the `/15` `grouper` section
#: (A§3.4). Only `/7` admits the `chat` and `agent_cli` kinds, and `/7`
#: refuses `model`, which `/15` names `chat`.
FINAL_ADAPTATION_PROFILE = "campaign_adaptation/7"
#: G2 spec §4.1: family 12's adaptation profile is `/5` plus, under
#: `gate.provenance.kind: metadata`, the sorted per-case labels; family 14's
#: `/7` keeps both. All three bind the corpus partition.
PARTITION_PROFILES = ("campaign_adaptation/5", "campaign_adaptation/6", FINAL_ADAPTATION_PROFILE)
#: The profiles that record `family_membership` (ADR-0021): `/3` (family 9,
#: ADR-0022) is `/2` plus an admitted `model` grouping payload; `/4` (family
#: 10, ADR-0023) is `/3` plus the required `reflection` payload.
_MEMBERSHIP_PROFILES = (
    "campaign_adaptation/2",
    "campaign_adaptation/3",
    "campaign_adaptation/4",
    "campaign_adaptation/5",
    "campaign_adaptation/6",
    "campaign_adaptation/7",
)
#: Bounds on `/4`'s `reflection.max_consecutive_failures` (PDR-0019, ADR-0023):
#: the same `1..20` the manifest parser bounds `reflector.max_consecutive_failures`
#: with.
_REFLECTION_MAX_CONSECUTIVE_FAILURES = (1, 20)
#: #282, R2: the Reflector group text limit of a `/7` `reflection` without
#: `group_text_chars`. Every such journal ran at 512: a `/15` run that left the
#: key out, and a `/16` run created before the key existed.
_JOURNALED_ABSENT_GROUP_TEXT_CHARS = 512
#: The closed `{"kind": "model", ...}` grouping payload `/3` admits (LH2 model
#: error grouping design §7, §12): what binds the grouping model, its prompt and
#: its declared fallback into the adaptation identity. No credential value.
#: `api_key_env` is the one optional member (#290, ADR-0034 2026-10-21): a
#: Grouper without a declared key leaves it out; when present it is a name.
_MODEL_GROUPING_FIELDS = (
    "kind protocol backend model base_url api_key_env proxy_env verify_ssl_certs "
    "reasoning_effort provider_only timeout_seconds concurrency max_input_chars "
    "max_output_tokens representatives_per_case retry prompt_sha256 fallback"
)
_MODEL_GROUPING_OPTIONAL = frozenset({"api_key_env"})
#: b16 (spec §11, ADR-0055): the `/16` chat Grouper's portion keys with their
#: minimums, optional `/7` `chat` fields admitted only beside
#: `model_grouping/2`; `campaign_schema` keeps them out of family 14.
GROUPING_V2_KEYS = GROUPING_V2_MINIMUMS
MODEL_GROUPING_V2 = "model_grouping/2"
#: `/7`'s closed `{"kind": "agent_cli", ...}` Grouper payload (A§3.4): the
#: agent keys that change what the Grouper does, the operation deadline, the
#: prompt's and the program's content digests and the fallback. Environment
#: *names* only, sorted.
_AGENT_GROUPER_FIELDS = (
    "kind protocol model permission allowed_env reasoning_effort max_turns "
    "max_payload_bytes timeout_seconds prompt_sha256 program_sha256 fallback"
)
_AGENT_GROUPER_PROTOCOL = "agent_grouping_request/1"
#: `manifest_v2._parse_model_grouping`'s own integer bounds, which are the
#: authority: an identity cannot admit what no manifest can declare.
_MODEL_GROUPING_RANGES = {
    "timeout_seconds": (10, 7200),
    "concurrency": (1, 16),
    "max_input_chars": (8000, 2_000_000),
    "max_output_tokens": (256, 65_536),
    "representatives_per_case": (1, 8),
}


def _validate_provenance_labels(value: Any) -> None:
    """`[[case_id, [glob, ...]], ...]`, by case id, each glob list sorted and unique (G2 §4.1)."""

    from evolution.tree.artifacts.artifact_policy import validate_pattern

    if not isinstance(value, list):
        raise IdentityError("invalid_identity_fields")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], list):
            raise IdentityError("invalid_identity_fields")
        case_id, globs = item
        _identifier(case_id)
        if any(type(glob) is not str for glob in globs) or globs != sorted(set(globs)):
            raise IdentityError("invalid_identity_fields")
        for glob in globs:
            try:
                validate_pattern(glob)
            except ValueError:
                raise IdentityError("invalid_identity_fields") from None
        ids.append(case_id)
    if ids != sorted(set(ids)):
        raise IdentityError("invalid_identity_fields")


@dataclass(frozen=True, init=False)
class AdaptationIdentityV1:
    """Private family-4 diagnostic/resume identity.

    It is intentionally separate from :class:`CampaignIdentityV1`: diagnostic
    metadata must refuse a same-run resume without becoming a measurement
    corpus input or changing ``evaluation_set_id``.
    """

    _canonical: bytes

    def __init__(self, payload: Mapping[str, Any]) -> None:
        profile = payload.get("profile") if isinstance(payload, Mapping) else None
        if profile not in _ADAPTATION_PROFILES:
            raise IdentityError("invalid_identity_profile")
        # `/2` records `family_membership` exactly when families come from
        # metadata; with `families: none` the field is absent (spec §5).
        families = payload.get("families")
        membership = (
            profile in _MEMBERSHIP_PROFILES
            and isinstance(families, Mapping)
            and families.get("kind") == "metadata"
        )
        base_fields = _ADAPTATION_FIELDS_V2 if membership else _ADAPTATION_FIELDS
        fields = (
            base_fields + " reflection"
            if profile in ("campaign_adaptation/4", *PARTITION_PROFILES)
            else base_fields
        )
        if profile in PARTITION_PROFILES:
            fields += " corpus_partition"
        # G2 spec §4.1: `/6` records the labels exactly under
        # `gate.provenance.kind: metadata`; the creation record checks which.
        optional_labels = (
            profile in ("campaign_adaptation/6", FINAL_ADAPTATION_PROFILE)
            and "provenance_labels" in payload
        )
        if optional_labels:
            fields += " provenance_labels"
        if profile == FINAL_ADAPTATION_PROFILE:
            fields += "".join(f" {key}" for key in ("checks", "evidence") if key in payload)
        data = _closed(payload, fields)
        if "evidence" in data:
            from evolution.tree.execution.evidence_settings import EvidenceSettings, SettingsError

            try:
                EvidenceSettings.from_payload(data["evidence"])
            except SettingsError as exc:
                raise IdentityError("invalid_identity_fields") from exc
        if "checks" in data:
            from evolution.tree.execution.check_modes import parse_checks
            from evolution.tree.execution.evidence_settings import SettingsError

            try:
                parse_checks(data["checks"])
            except SettingsError as exc:
                raise IdentityError("invalid_identity_fields") from exc
        if profile in PARTITION_PROFILES:
            from evolution.tree.execution.corpus_partition import validate_partition_descriptor

            validate_partition_descriptor(data["corpus_partition"])
        if optional_labels:
            _validate_provenance_labels(data["provenance_labels"])
        if data["attempt_feedback"] not in ("none", "sequential"):
            raise IdentityError("invalid_identity_fields")
        if data["reader_protocol"] != "reflection_reader/1":
            raise IdentityError("invalid_identity_fields")
        _sha(data["reflection_metadata_digest"])
        if data["environment_docs_digest"] is not None:
            _sha(data["environment_docs_digest"])
        self._validate_grouping(data["grouping"], profile)
        self._validate_selection(data["selection"])
        self._validate_families(data["families"])
        if membership:
            self._validate_membership(data["family_membership"])
        if profile in ("campaign_adaptation/4", *PARTITION_PROFILES):
            self._validate_reflection(data["reflection"], profile)
        try:
            canonical = canonical_json(data)
        except (TypeError, ValueError, RecursionError) as exc:
            raise IdentityError("invalid_identity_fields") from exc
        object.__setattr__(self, "_canonical", canonical)

    @staticmethod
    def _validate_grouping(value: Any, profile: str) -> None:
        if not isinstance(value, Mapping) or not isinstance(value.get("kind"), str):
            raise IdentityError("invalid_identity_fields")
        kind = value["kind"]
        # New limits are optional /7 fields. Strip only after validation so
        # nested fallbacks and older profiles keep their closed shapes.
        if profile == FINAL_ADAPTATION_PROFILE:
            value = dict(value)
            if kind == "chat":
                order = value.pop("provider_order", None)
                if order is not None and not (
                    isinstance(order, list)
                    and order
                    and all(isinstance(item, str) and item.strip() for item in order)
                    and len(set(order)) == len(order)
                    and value.get("provider_only") is None
                ):
                    raise IdentityError("invalid_identity_fields")
                if "allow_fallbacks" in value:
                    if type(value.pop("allow_fallbacks")) is not bool or order is None:
                        raise IdentityError("invalid_identity_fields")
                if "require_parameters" in value:
                    if type(value.pop("require_parameters")) is not bool or (
                        order is None and value.get("provider_only") is None
                    ):
                        raise IdentityError("invalid_identity_fields")
            if "max_consecutive_full_fallbacks" in value:
                _positive(value.pop("max_consecutive_full_fallbacks"))
            if kind == "chat" and "max_repairs" in value:
                repairs = value.pop("max_repairs")
                if type(repairs) is not int or repairs < 0:
                    raise IdentityError("invalid_identity_integer")
            if kind == "chat":
                for name, minimum in GROUPING_V2_KEYS.items():
                    if name not in value:
                        continue
                    if value.get("protocol") != MODEL_GROUPING_V2:
                        raise IdentityError("invalid_identity_fields")
                    count = value.pop(name)
                    if type(count) is not int or count < minimum:
                        raise IdentityError("invalid_identity_integer")
        if kind == "fields":
            AdaptationIdentityV1._validate_fields_grouping(value)
            return
        if kind == "command":
            data = _closed(value, "kind command_digest config_digest")
            _sha(data["command_digest"])
            _sha(data["config_digest"])
            return
        # ADR-0022/ADR-0023: only `/3` to `/6` admit a model grouping; `/1` and
        # `/2` keep admitting exactly `fields` and `command`. R18: `/7` admits
        # `chat` (the model payload under its `/15` name) and `agent_cli`, and
        # refuses `model`.
        final = profile == FINAL_ADAPTATION_PROFILE
        if kind == "model" and profile in (
            "campaign_adaptation/3",
            "campaign_adaptation/4",
            "campaign_adaptation/5",
            "campaign_adaptation/6",
        ):
            AdaptationIdentityV1._validate_model_grouping(value)
            return
        if kind == "chat" and final:
            AdaptationIdentityV1._validate_model_grouping(
                value, protocols=("model_grouping/1", MODEL_GROUPING_V2)
            )
            return
        if kind == "agent_cli" and final:
            AdaptationIdentityV1._validate_agent_grouper(value)
            return
        raise IdentityError("invalid_identity_fields")

    @staticmethod
    def _validate_agent_grouper(value: Mapping[str, Any]) -> None:
        """`/7`'s closed `agent_cli` Grouper payload, bounded as the manifest bounds it."""

        from evolution.tree.manifest.manifest_v2 import REASONING_EFFORTS

        data = _closed(value, _AGENT_GROUPER_FIELDS)
        if data["protocol"] != _AGENT_GROUPER_PROTOCOL:
            raise IdentityError("invalid_identity_fields")
        if not (isinstance(data["model"], str) and data["model"].strip()):
            raise IdentityError("invalid_identity_fields")
        if data["permission"] not in ("safe", "unsafe"):
            raise IdentityError("invalid_identity_fields")
        names = data["allowed_env"]
        if not (
            isinstance(names, list)
            and all(isinstance(name, str) and name.strip() for name in names)
            and names == sorted(set(names))
        ):
            raise IdentityError("invalid_identity_fields")
        effort = data["reasoning_effort"]
        if effort is not None and (not isinstance(effort, str) or effort not in REASONING_EFFORTS):
            raise IdentityError("invalid_identity_fields")
        if data["max_turns"] is not None:
            _positive(data["max_turns"])
        _positive(data["max_payload_bytes"])
        timeout = data["timeout_seconds"]
        if not (type(timeout) is int and 10 <= timeout <= 7200):
            raise IdentityError("invalid_identity_integer")
        _sha(data["prompt_sha256"])
        _sha(data["program_sha256"])
        fallback = data["fallback"]
        if fallback is not None:
            if not isinstance(fallback, Mapping) or fallback.get("kind") != "fields":
                raise IdentityError("invalid_identity_fields")
            AdaptationIdentityV1._validate_fields_grouping(fallback)

    @staticmethod
    def _validate_fields_grouping(value: Mapping[str, Any]) -> None:
        if set(value) != {"kind", "features"} or not isinstance(value["features"], list):
            raise IdentityError("invalid_identity_fields")

    @staticmethod
    def _validate_model_grouping(
        value: Mapping[str, Any], *, protocols: tuple[str, ...] = ("model_grouping/1",)
    ) -> None:
        """`/3`'s closed model grouping payload, bounded as the manifest parser bounds it.

        `protocols` is what the slot admits: `/7`'s `chat` also admits
        `model_grouping/2` (b16), which only a family-15 run may record."""

        # Circular at module scope: the manifest loader imports this module.
        from evolution.tree.manifest.manifest_v2 import REASONING_EFFORTS
        from evolution.tree.roles.role_call import RoleCallRetry

        if not isinstance(value, Mapping):
            raise IdentityError("invalid_identity_fields")
        omitted = set(_MODEL_GROUPING_FIELDS.split()) - set(value)
        if not omitted <= _MODEL_GROUPING_OPTIONAL:
            raise IdentityError("invalid_identity_fields")
        data = _closed({**value, **{name: None for name in omitted}}, _MODEL_GROUPING_FIELDS)
        if data["protocol"] not in protocols or data["backend"] != "openai_chat":
            raise IdentityError("invalid_identity_fields")
        model = data["model"]
        if not (isinstance(model, str) and model.strip() and len(model) <= 256):
            raise IdentityError("invalid_identity_fields")
        if not (isinstance(data["base_url"], str) and data["base_url"].strip()):
            raise IdentityError("invalid_identity_fields")
        for name in ("api_key_env", "proxy_env"):
            env = data[name]
            if env is None and (name == "proxy_env" or name in omitted):
                continue
            if not (isinstance(env, str) and env.strip() and "=" not in env):
                raise IdentityError("invalid_identity_fields")
        if type(data["verify_ssl_certs"]) is not bool:
            raise IdentityError("invalid_identity_fields")
        effort = data["reasoning_effort"]
        if effort is not None and effort not in REASONING_EFFORTS:
            raise IdentityError("invalid_identity_fields")
        # The parser caps no length here (it follows the chat roles), so
        # neither does the identity: `None` or non-empty strings, at least one.
        only = data["provider_only"]
        if only is not None and not (
            isinstance(only, list)
            and only
            and all(isinstance(item, str) and item.strip() for item in only)
        ):
            raise IdentityError("invalid_identity_fields")
        for name, (low, high) in _MODEL_GROUPING_RANGES.items():
            number = data[name]
            if not (type(number) is int and low <= number <= high):
                raise IdentityError("invalid_identity_integer")
        try:
            RoleCallRetry.from_payload(data["retry"])
        except ValueError as exc:
            raise IdentityError("invalid_identity_fields") from exc
        _sha(data["prompt_sha256"])
        fallback = data["fallback"]
        if fallback is not None:
            if not isinstance(fallback, Mapping) or fallback.get("kind") != "fields":
                raise IdentityError("invalid_identity_fields")
            AdaptationIdentityV1._validate_fields_grouping(fallback)

    @staticmethod
    def _validate_selection(value: Any) -> None:
        fields = "focus_cases representatives_per_case context_page_size"
        if isinstance(value, Mapping) and "min_claim_cases" in value:
            fields += " min_claim_cases"
        data = _closed(value, fields)
        if "min_claim_cases" in data:
            _positive(data["min_claim_cases"])
        if not (type(data["focus_cases"]) is int and 1 <= data["focus_cases"] <= 64):
            raise IdentityError("invalid_identity_integer")
        if not (
            type(data["representatives_per_case"]) is int
            and 1 <= data["representatives_per_case"] <= 8
        ):
            raise IdentityError("invalid_identity_integer")
        if not (type(data["context_page_size"]) is int and 1 <= data["context_page_size"] <= 64):
            raise IdentityError("invalid_identity_integer")

    @staticmethod
    def _validate_families(value: Any) -> None:
        if not isinstance(value, Mapping) or value.get("kind") not in {"none", "metadata"}:
            raise IdentityError("invalid_identity_fields")
        if value["kind"] == "none" and set(value) == {"kind"}:
            return
        if (
            value["kind"] == "metadata"
            and set(value) == {"kind", "path"}
            and isinstance(value["path"], str)
        ):
            return
        raise IdentityError("invalid_identity_fields")

    @staticmethod
    def _validate_membership(value: Any) -> None:
        """`/2` with `families: metadata` only: sorted, unique `[case_id, family]` pairs."""

        if not isinstance(value, list):
            raise IdentityError("invalid_identity_fields")
        previous: str | None = None
        for pair in value:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(item, str) and item.strip() for item in pair)
            ):
                raise IdentityError("invalid_identity_fields")
            if previous is not None and pair[0] <= previous:
                raise IdentityError("invalid_identity_fields")
            previous = pair[0]

    @staticmethod
    def _validate_reflection(value: Any, profile: str) -> None:
        """`/4`'s closed `reflection` payload: the one field family 10 adds.

        `/7` (#282) also admits an optional `group_text_chars`, an integer
        `>= 1` or `null`; the `/15` and `/16` documents share `/7`, so the
        payload does not say which one emitted it (R2)."""

        optional = (
            profile == FINAL_ADAPTATION_PROFILE
            and isinstance(value, Mapping)
            and "group_text_chars" in value
        )
        fields = (
            "max_consecutive_failures group_text_chars" if optional else "max_consecutive_failures"
        )
        if (
            profile == FINAL_ADAPTATION_PROFILE
            and isinstance(value, Mapping)
            and "max_consecutive_skips" in value
        ):
            fields += " max_consecutive_skips"
            _positive(value["max_consecutive_skips"])
        data = _closed(value, fields)
        low, high = _REFLECTION_MAX_CONSECUTIVE_FAILURES
        n = data["max_consecutive_failures"]
        if not (type(n) is int and low <= n <= high):
            raise IdentityError("invalid_identity_integer")
        if optional and data["group_text_chars"] is not None:
            _positive(data["group_text_chars"])

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AdaptationIdentityV1:
        return cls(payload)

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def profile(self) -> str:
        return str(self.to_payload()["profile"])

    @property
    def family_membership(self) -> Mapping[str, str]:
        """Case id -> family; empty when the field is absent (`/1`, families `none`)."""

        pairs = self.to_payload().get("family_membership", [])
        return MappingProxyType({case_id: family for case_id, family in pairs})

    @property
    def min_claim_cases(self) -> int:
        return int(self.to_payload()["selection"].get("min_claim_cases", DEFAULT_MIN_CLAIM_CASES))

    @property
    def provenance_labels(self) -> Mapping[str, tuple[str, ...]]:
        """Case id -> globs (G2 spec §4.1); empty when the field is absent."""

        pairs = self.to_payload().get("provenance_labels", [])
        return MappingProxyType({case_id: tuple(globs) for case_id, globs in pairs})


def reflection_group_text_chars(reflection: Mapping[str, Any]) -> int | None:
    """The Reflector group text limit a journaled `reflection` payload binds
    (#282, R2): its `group_text_chars` (`None` for no per-field limit), else
    512, the limit every run that journaled no key ran at."""

    if "group_text_chars" not in reflection:
        return _JOURNALED_ABSENT_GROUP_TEXT_CHARS
    value = reflection["group_text_chars"]
    return None if value is None else int(value)


def adaptation_identity_slot(identity: AdaptationIdentityV1) -> dict[str, str]:
    """The compact versioned slot a campaign identity binds."""

    return {"version": identity.profile, "digest": identity.digest}


def build_adaptation_identity(
    *,
    grouping: Mapping[str, Any],
    selection: Mapping[str, Any],
    families: Mapping[str, Any],
    attempt_feedback: Literal["none", "sequential"],
    reflection_metadata_digest: str,
    environment_docs_digest: str | None,
    reader_protocol: str = "reflection_reader/1",
    family_membership: Mapping[str, str | None] | None = None,
    profile: str | None = None,
    reflection: Mapping[str, int | None] | None = None,
    corpus_partition: Mapping[str, Any] | None = None,
    provenance_labels: list[list[Any]] | None = None,
) -> AdaptationIdentityV1:
    """Freeze family-4 diagnostic inputs without changing measurement identity.

    A command grouping supplies only its sealed ``command_digest`` and
    ``config_digest`` here; command bytes and opaque configuration are never
    retained in a journal identity payload. ``family_membership=None`` builds
    profile ``/1`` byte for byte; a mapping (family 8, ADR-0021) builds ``/2``,
    which under ``families: metadata`` records each case's family (cases
    without one omitted) and under ``families: none`` has no such field.
    ``profile="campaign_adaptation/3"`` (family 9, ADR-0022) builds ``/3``:
    ``/2``'s fields, with a ``model`` grouping payload admitted too; it needs
    ``family_membership``. ``profile="campaign_adaptation/4"`` (family 10,
    ADR-0023) builds ``/4``: ``/3``'s fields plus the required ``reflection``
    (``{"max_consecutive_failures": int}``, ``1..20``).
    ``profile="campaign_adaptation/7"`` (family 14, R18) builds ``/7`` in one
    step: ``/4``'s fields plus the required ``corpus_partition`` and the
    optional ``provenance_labels``, with the Grouper identity as ``grouping``;
    its ``reflection`` may also carry ``group_text_chars`` (#282).
    """

    final = profile == FINAL_ADAPTATION_PROFILE
    if profile is not None and (
        profile not in ("campaign_adaptation/3", "campaign_adaptation/4", FINAL_ADAPTATION_PROFILE)
        or family_membership is None
    ):
        raise IdentityError("invalid_identity_profile")
    if profile in ("campaign_adaptation/4", FINAL_ADAPTATION_PROFILE):
        if not isinstance(reflection, Mapping):
            raise IdentityError("invalid_identity_fields")
    elif reflection is not None:
        raise IdentityError("invalid_identity_fields")
    # R18, plan choice 11: `/7` is built in one step, so it takes the
    # partition (required) and the labels (optional) here; no other profile
    # takes either (`evolve` patches `/5` and `/6` in after this call).
    if final and corpus_partition is None:
        raise IdentityError("invalid_identity_fields")
    if not final and (corpus_partition is not None or provenance_labels is not None):
        raise IdentityError("invalid_identity_fields")

    payload: dict[str, Any] = {
        "profile": "campaign_adaptation/1",
        "grouping": dict(grouping),
        "selection": dict(selection),
        "families": dict(families),
        "attempt_feedback": attempt_feedback,
        "reflection_metadata_digest": reflection_metadata_digest,
        "environment_docs_digest": environment_docs_digest,
        "reader_protocol": reader_protocol,
    }
    if family_membership is not None:
        payload["profile"] = profile or "campaign_adaptation/2"
    if family_membership is not None and families.get("kind") == "metadata":
        payload["family_membership"] = sorted(
            [case_id, family] for case_id, family in family_membership.items() if family is not None
        )
    if profile in ("campaign_adaptation/4", FINAL_ADAPTATION_PROFILE):
        payload["reflection"] = dict(reflection)  # type: ignore[arg-type]
    if final:
        payload["corpus_partition"] = dict(corpus_partition)  # type: ignore[arg-type]
        if provenance_labels is not None:
            payload["provenance_labels"] = provenance_labels
    return AdaptationIdentityV1(payload)


def build_identity(
    policy: CampaignPolicyV11,
    preflight: TargetPreflight,
    environment: EnvironmentIdentity,
    scorer: Any,
    editor: EditorIdentity,
    corpus: Sequence[EvalCase],
    artifact_store: ArtifactStoreIdentityV1,
    strategy: str,
    linked_from: LinkedFrom | None,
    *,
    reflector: ReflectorIdentity,
    environment_docs_digest: str | None,
    probe: Mapping[str, Any] | None,
    connection: Mapping[str, str | int] | None = None,
) -> CampaignIdentityV1:
    """Build the campaign identity from every declared input (spec §4 table).

    `connection` is family 14's `ConnectionContract.identity_fields()`; the
    two digests are written exactly when it is given (R17). A third,
    `verbatim_answers` (ADR-0038), is copied exactly when `connection` carries it.
    """

    semantic = preflight.semantic
    payload: dict[str, Any] = dict(
        profile="campaign",
        policy=policy.to_payload(),
        policy_digest=policy.digest,
        target=dict(
            contract_digest=semantic.contract_digest,
            dependency_digest=semantic.dependency_digest,
            settings_hash=semantic.settings_hash,
            seed_mode=semantic.seed_mode,
            protocol_version=semantic.protocol_version,
            runtime_ref_digest=semantic.runtime_ref_digest,
        ),
        environment=dict(kind=environment.kind, digest=environment.digest),
        scorer=scorer_binding(scorer),
        evaluation_set_id=evaluation_set_id(corpus),
        corpus_digest=corpus_digest(corpus),
        corpus_cases=[
            dict(case_id=case.case_id, group_id=case.group_id, stratum=case.stratum)
            for case in corpus
        ],
        metric_plan_digest=metric_plan_digest(policy.metric_primary),
        artifact_store=artifact_store.to_payload(),
        materialization_strategy=strategy,
        capabilities=dict(
            max_instances=preflight.capabilities.max_instances,
            max_concurrent_sessions=preflight.capabilities.max_concurrent_sessions,
        ),
        editor=dict(kind=editor.kind, digest=editor.digest),
        reflector=dict(
            kind=reflector.kind,
            digest=reflector.digest,
            environment_docs_digest=environment_docs_digest,
            probe=None if probe is None else dict(probe),
        ),
        linked_from=linked_from.to_payload() if linked_from is not None else None,
    )
    if connection is not None:
        payload["metric_contract"] = connection["metric_contract"]
        payload["role_visibility"] = connection["role_visibility"]
        if "verbatim_answers" in connection:
            payload["verbatim_answers"] = connection["verbatim_answers"]
        if "secret_oracle_paths" in connection:
            payload["secret_oracle_paths"] = connection["secret_oracle_paths"]
        if "checks" in connection:
            payload["checks"] = connection["checks"]
        if "preflight_sample_retries" in connection:
            payload["preflight_sample_retries"] = connection["preflight_sample_retries"]
    return CampaignIdentityV1(payload)


def schedule_for_ids(
    policy: CampaignPolicyV11 | _config.EvaluationPolicy,
    case_ids: Sequence[str],
    seed_mode: str,
    side: Side,
    *,
    case_repeats: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """The spec §4 schedule rows: every case id x repeats `1..policy.repeats`.

    Family-13 design §5: `case_repeats` (case id -> count) gives a listed case
    repeats `1..count` instead. Each row follows the rule for its repeat, so a
    `(case_id, repeat)` row is byte-identical to the uniform schedule's.
    """

    if side not in ("champion", "candidate"):
        raise ValueError("side must be 'champion' or 'candidate'")
    counts = case_repeats or {}
    rows: list[dict[str, Any]] = []
    for case_id in case_ids:
        for repeat in range(1, counts.get(case_id, policy.repeats) + 1):
            requested_seed: int | None
            if seed_mode == "exact":
                sample_id = f"{repeat:02d}"
                requested_seed = policy.random_seed + repeat
            else:
                sample_id = f"{side}-{repeat:02d}"
                requested_seed = None
            rows.append(
                dict(
                    case_id=case_id,
                    sample_id=sample_id,
                    repeat=repeat,
                    requested_seed=requested_seed,
                )
            )
    return rows


def schedule_for(
    policy: CampaignPolicyV11 | _config.EvaluationPolicy,
    corpus: Sequence[EvalCase],
    seed_mode: str,
    side: Side,
) -> list[dict[str, Any]]:
    """The spec §4 schedule rows: every corpus case x repeats `1..policy.repeats`."""

    return schedule_for_ids(policy, [case.case_id for case in corpus], seed_mode, side)


def derived_identity(
    identity: CampaignIdentityV1,
    candidate_digest: str,
    side: Side,
    *,
    case_subset: Sequence[int] | None = None,
    case_repeats: Sequence[Sequence[int]] | None = None,
) -> RunIdentityV1:
    """Derive one measurement's `RunIdentityV1` from the campaign identity alone (spec §3).

    ADR-0008's rule in its literal form: the schedule comes from the identity's
    own `corpus_cases`, every other field from the identity, and no corpus is
    needed — which is what lets an audit over the run root re-derive it (spec §5)
    without holding the corpus itself.

    G2 spec §3.1: `case_subset` selects sorted `corpus_cases` indices. The
    identity keeps the campaign's `corpus_digest` and records the selector, so
    the audit still needs no corpus; the schedule is the full schedule's rows
    of the selected cases. Without it the identity is byte for byte today's.

    Family-13 design §5: `case_repeats` is `[corpus index, count]` pairs over
    the selected cases. It is recorded as a selector, and each listed case is
    scheduled repeats `1..count`. Without it nothing changes.
    """

    data = identity.to_payload()
    raw_policy = data["policy"]
    policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
    corpus_cases = data["corpus_cases"]
    selector: dict[str, Any] = {}
    if case_subset is None:
        indices: Sequence[int] = range(len(corpus_cases))
    else:
        indices = checked_case_subset(list(case_subset), len(corpus_cases))
        selector["case_subset"] = list(indices)
    case_ids = [str(corpus_cases[index]["case_id"]) for index in indices]
    counts: dict[str, int] | None = None
    if case_repeats is not None:
        pairs = checked_case_repeats([list(pair) for pair in case_repeats], set(indices))
        counts = {str(corpus_cases[index]["case_id"]): count for index, count in pairs}
        selector["case_repeats"] = pairs
    schedule = schedule_for_ids(
        policy, case_ids, data["target"]["seed_mode"], side, case_repeats=counts
    )
    # A V5 policy's `sample_retries` changes how the measurement is taken, so it
    # is part of the measurement identity; an earlier policy adds no key and so
    # keeps the identity digest it always had.
    retries: dict[str, int] = {}
    if policy.sample_retries is not None:
        retries["sample_retries"] = policy.sample_retries
    # G2 spec §4.2: only a family-12 (`/13`) policy's measurements store
    # provenance; the marker is derived here, so the audit needs no corpus.
    marker = {"row_provenance": 1} if isinstance(policy, CampaignPolicyV11) else {}
    if isinstance(policy, CampaignPolicyV11):
        # Family-14 design R59: a family-14 measurement's starts name their
        # connection binding; families up to 13 keep their bytes.
        marker["connection_binding"] = 1
    return RunIdentityV1.from_payload(
        dict(
            **retries,
            **selector,
            **marker,
            profile="rollout_measurement",
            candidate_digest=candidate_digest,
            target=data["target"],
            environment=data["environment"],
            scorer=data["scorer"],
            evaluation_set_id=data["evaluation_set_id"],
            corpus_digest=data["corpus_digest"],
            metric_plan_digest=data["metric_plan_digest"],
            schedule=schedule,
            budget=dict(
                max_turns=policy.budget.max_turns, timeout_seconds=policy.budget.timeout_seconds
            ),
            max_concurrency=policy.max_concurrency,
            max_scorer_concurrency=policy.max_scorer_concurrency,
            materialization_strategy=data["materialization_strategy"],
        )
    )


def fresh_final_identity(
    identity: RunIdentityV1, measurement_id: str, repeats: int
) -> RunIdentityV1:
    """Family 15's separate final schedule; epoch carries keep this namespace.

    Exact seeds occupy the next R slots after gate seeds. Unpaired providers
    still receive no seed. Sample IDs bind the fresh measurement's own namespace.
    """
    data = identity.to_payload()
    from evolution.tree.campaign_epochs import base_measurement_id

    namespace = base_measurement_id(measurement_id)
    for row in data["schedule"]:
        row["sample_id"] = f"{namespace}-{row['sample_id']}"
        if row["requested_seed"] is not None:
            row["requested_seed"] += repeats
    return RunIdentityV1.from_payload(data)


def measurement_identity(
    identity: CampaignIdentityV1,
    corpus: Sequence[EvalCase],
    candidate_digest: str,
    side: Side,
    *,
    case_subset: Sequence[int] | None = None,
    case_repeats: Sequence[Sequence[int]] | None = None,
) -> RunIdentityV1:
    """Verify `corpus` against the identity, then delegate to `derived_identity` (spec §4).

    `corpus` is taken explicitly and checked against `identity.corpus_digest` — the
    same check S1's `RunIdentityV1.validate_corpus` makes — because the identity
    itself carries no state outside its canonical bytes; once verified, the actual
    schedule derivation is `derived_identity`'s alone, from the identity's own
    `corpus_cases`. `case_subset` and `case_repeats` as in `derived_identity`.
    """

    if corpus_digest(corpus) != identity.corpus_digest:
        raise IdentityError("run_identity_changed")
    return derived_identity(
        identity, candidate_digest, side, case_subset=case_subset, case_repeats=case_repeats
    )


def observe_case_repeats(
    policy: CampaignPolicyV11, causal: Sequence[int]
) -> list[list[int]] | None:
    """Design §5: the observe R=1 fresh Parent's second observation on each C case.

    Present exactly under the four conditions: `observe`, R = 1, `measured_noise`
    and `fresh_parent`, with a non-empty C (the whole measured C, not only the
    retained cases, so the identity stays derivable from the selection alone).
    """

    if (
        policy.gate_mode != "observe"
        or policy.repeats != 1
        or policy.causal_tolerance != "measured_noise"
        or not policy.fresh_parent
        or not causal
    ):
        return None
    return [[index, 2] for index in sorted(causal)]


def focus_case_repeats(policy: CampaignPolicyV11, focus: Sequence[int]) -> list[list[int]] | None:
    """b18: the `/16` Candidate's and fresh Parent's focus cases at R_focus.

    `evaluation.focus_repeats_per_question` schedules each focus case repeats
    `1..R_focus`; causal cases keep `repeats_per_question`. Absent, or with an
    empty focus, the measurement keeps today's identity.
    """

    count = getattr(policy, "focus_repeats_per_question", None)
    if count is None or not focus:
        return None
    return [[index, count] for index in sorted(focus)]


def case_subset_for(identity: CampaignIdentityV1, case_ids: Iterable[str]) -> tuple[int, ...]:
    """G2 spec §3.5: the sorted `corpus_cases` indices of `case_ids`, each a corpus case named once."""

    if isinstance(case_ids, str):
        raise IdentityError("invalid_case_subset")
    names = list(case_ids)
    position = {str(case["case_id"]): index for index, case in enumerate(identity.corpus_cases)}
    if not names or len(set(names)) != len(names) or any(name not in position for name in names):
        raise IdentityError("invalid_case_subset")
    return tuple(sorted(position[name] for name in names))


def measurement_id_for(attempt_id: str | None) -> str:
    return "m-r00-champion" if attempt_id is None else f"m-{attempt_id}"


def parent_measurement_id_for(attempt_id: str) -> str:
    """G2 spec §4.6: the fresh Parent measurement of a family-12 Attempt."""

    return f"m-{attempt_id}-parent"


def final_measurement_id_for(round_number: int, ordinal: int, index: int | None) -> str:
    """A family-14 binding's Champion or finalist ID, including its ordinal."""

    for name, value in (("round_number", round_number), ("ordinal", ordinal), ("index", index)):
        if name == "index" and value is None:
            continue
        if type(value) is not int or value <= 0:
            raise IdentityError(f"invalid_final_measurement_{name}")
    member = "champion" if index is None else f"a{index:02d}"
    return f"m-r{round_number:02d}-final-{ordinal:02d}-{member}"


def refresh_measurement_id_for(round_number: int) -> str:
    """A family-14 baseline refresh's Champion measurement ID, `m-r<NN>-refresh`.

    It carries no `@e<k>` even in a later epoch (ADR-0020).
    """

    if type(round_number) is not int or round_number <= 0:
        raise IdentityError("invalid_refresh_measurement_round")
    return f"m-r{round_number:02d}-refresh"


def instances_per_measurement(
    policy: CampaignPolicyV11 | _config.EvaluationPolicy, capabilities: AdapterCapabilities
) -> int:
    return math.ceil(policy.max_concurrency / capabilities.max_concurrent_sessions)


def admit_capabilities(policy: CampaignPolicyV11, capabilities: AdapterCapabilities) -> None:
    """Refuse a policy whose peak instance demand exceeds the adapter's admission (spec §6)."""

    per_measurement = instances_per_measurement(policy, capabilities)
    # G2b3a: a family-12 joint stage holds the Candidate's and the fresh
    # Parent's instances at once.
    per_attempt = 2 if getattr(policy, "fresh_parent", None) else 1
    concurrent = max(1, policy.parallel_attempts) * per_attempt
    if isinstance(policy, CampaignPolicyV11):
        # Family-14 design R16: a final measurement holds one finalist under
        # `first_accepted` and up to K under `all_attempts`, plus a fresh
        # Champion only under `unstable`. #160 (`stable`, `first_accepted`,
        # `fresh_parent: false`, one Attempt at a time) needs one instance.
        finalists = 1 if policy.round_stop == "first_accepted" else policy.attempts_per_round
        champion = 1 if policy.stability == "unstable" else 0
        concurrent = max(concurrent, finalists + champion)
    if concurrent * per_measurement > capabilities.max_instances:
        raise CapabilitiesInsufficient("capabilities_insufficient")
