"""The core manifest schemas this Lab release accepts; one place to re-pin (PDR-0015 as amended 2026-10-15)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Campaign manifests the Owner paths admit (Bundle, preparation, binding, fixed evaluation, project form, corpus
#: placement): /15 and /16 (family 15). Core's ``load_manifest_v2`` validates /16; Lab adds no check of its own.
CAMPAIGN_MANIFEST_SCHEMAS = frozenset({"evolution-v2-manifest/15", "evolution-v2-manifest/16"})
#: The subset the H1 client, the legacy recipe and the runner still require: they stay /15-only.
LEGACY_CAMPAIGN_MANIFEST_SCHEMAS = frozenset({"evolution-v2-manifest/15"})
#: The schema Lab writes itself (spec 2026-10-13 §4): /15 with the observe smoke profile.
CAMPAIGN_MANIFEST_SCHEMA = "evolution-v2-manifest/15"
EVALUATION_MANIFEST_SCHEMA = "evolution-v2-manifest/1"
#: Longest core diagnosis a Lab refusal message carries (ServiceError allows 512 for the whole detail).
DIAGNOSIS_LIMIT = 400

def is_campaign_schema(value: Any) -> bool:
    """A raw ``schema`` value of a campaign manifest; a non-string (list, dict) is never one (not hashable)."""
    return isinstance(value, str) and value in CAMPAIGN_MANIFEST_SCHEMAS


def admits_campaign_schema(value: Any, schemas: frozenset[str]) -> bool:
    """A raw ``schema`` value that is one of ``schemas``; a non-string (list, dict) never is (not hashable)."""
    return isinstance(value, str) and value in schemas


def admits_campaign_manifest(manifest: Any, schemas: frozenset[str]) -> bool:
    """A parsed workflow manifest whose schema is in ``schemas``: the single admission rule for H1 and Owner callers."""
    return manifest.schema in schemas and manifest.workflow is not None


def is_legacy_campaign_schema(value: Any) -> bool:
    """A raw ``schema`` value of a /15 campaign manifest, the only one H1, the legacy recipe and the runner take."""
    return admits_campaign_schema(value, LEGACY_CAMPAIGN_MANIFEST_SCHEMAS)


def is_campaign_manifest(manifest: Any) -> bool:
    return manifest.schema in CAMPAIGN_MANIFEST_SCHEMAS and manifest.workflow is not None


def is_legacy_campaign_manifest(manifest: Any) -> bool:
    return admits_campaign_manifest(manifest, LEGACY_CAMPAIGN_MANIFEST_SCHEMAS)


def is_evaluation_manifest(manifest: Any) -> bool:
    return manifest.schema == EVALUATION_MANIFEST_SCHEMA and manifest.workflow is None


#: PDR-0013: a manifest that continues another run is refused on every Lab path.
LINKED_RUN_UNSUPPORTED = "linked_run_unsupported"


def is_linked_run(manifest: Any) -> bool:
    """A campaign manifest declaring ``linked_from``: continuation is not a Lab capability."""
    return manifest.linked_from is not None


BASELINE_IMPORT_UNSUPPORTED = "baseline_import_unsupported"
BASELINE_IMPORT_UNSUPPORTED_MESSAGE = (
    "Lab writes baseline_import into the manifest copy. Use lab campaign --baseline-from."
)


def has_baseline_import(manifest: Any) -> bool:
    return manifest.baseline_import is not None


#: Grouper kinds Lab supports (spec 2026-10-13 §5.1); core also has ``agent_cli``, which Lab refuses.
LAB_GROUPER_KINDS = ("fields", "command", "chat")
GROUPER_KIND_UNSUPPORTED = "grouper_kind_unsupported"


def unsupported_grouper_kind(raw: Mapping[str, Any]) -> bool:
    """A /15 document whose top-level ``grouper.kind`` Lab does not support (raw JSON, before any side effect)."""
    grouper = raw.get("grouper")
    return isinstance(grouper, Mapping) and grouper.get("kind") not in LAB_GROUPER_KINDS


#: The message of every ``grouper_kind_unsupported`` refusal (Bundle, project form, legacy recipe, H1 connect).
GROUPER_KIND_UNSUPPORTED_MESSAGE = "Lab supports grouper kinds fields, command, chat (grouper.kind)."


def has_unsupported_grouper(manifest: Any) -> bool:
    """The parsed counterpart of ``unsupported_grouper_kind`` for paths that hold only core's ``ManifestV2`` (legacy
    recipe, H1 connect): a /15 top-level ``grouper`` outside Lab's kinds."""
    from evolution.tree.roles.config import (
        CommandGroupingSection, FieldsGroupingSection, ModelGroupingSection)

    grouper = getattr(manifest.workflow, "grouper", None)
    return grouper is not None and not isinstance(
        grouper, (FieldsGroupingSection, CommandGroupingSection, ModelGroupingSection))


#: Acceptance dev48 F-11: core refuses a corpus inside a role's workspace only when ``evolve`` starts; Lab refuses it
#: on every preparation path, before any side effect, with core's own code.
CORPUS_INSIDE_ROLE_WORKSPACE = "corpus_inside_role_workspace"
CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE = (
    "The corpus must lie outside seed_root and every role's working directory and program (corpus.path).")


def corpus_inside_role_workspace(manifest: Any) -> bool:
    """Check declared corpus placement through the exact core helper.

    Missing corpora are left to the existing corpus validation stage. Only
    the placement refusal becomes True; other core and filesystem errors
    propagate for each preparation entry point to map safely.
    """
    from evolution.tree.evolve import EvolveRefused, check_corpus_placement

    if manifest.schema not in CAMPAIGN_MANIFEST_SCHEMAS or manifest.workflow is None:
        return False
    if not manifest.workflow.corpus_path.exists():
        return False
    try:
        check_corpus_placement(manifest)
    except EvolveRefused as error:
        if error.code == CORPUS_INSIDE_ROLE_WORKSPACE:
            return True
        raise
    return False


def _printable(text: str) -> str:
    """Core puts the document's own key names into a diagnosis; control characters never reach a message."""
    return "".join(c if c.isprintable() else "?" for c in text)[:DIAGNOSIS_LIMIT]


def core_diagnosis(error: BaseException | None) -> str | None:
    """Core's own words for a refused manifest or corpus, carried in the message of Lab's unchanged code (spec
    2026-10-09 §3, §3.1): ``ManifestError`` is ``<code> at <key path>`` plus ``: <expected …>`` when core states the
    accepted domain; ``CorpusError`` is ``<code> at line <n>``. Core guarantees neither carries a document value.
    Looks at the error and its ``__cause__``; ``None`` for anything else."""
    from evolution.tree import ManifestError
    from evolution.tree.execution.corpus import CorpusError

    for candidate in (error, getattr(error, "__cause__", None)):
        if isinstance(candidate, ManifestError):
            expected = getattr(candidate, "expected", None)
            text = f"{candidate.code} at {candidate.detail}" + (f": {expected}" if expected else "")
            return _printable(text)
        if isinstance(candidate, CorpusError):
            return _printable(f"{candidate.code} at line {candidate.line}")
    return None


def grouping_section(workflow):
    """The /15 campaign's required top-level ``grouper`` (spec 2026-10-13 §5.1)."""
    return workflow.grouper


def campaign_cases(manifest: Any, split: str | None = None) -> tuple:
    """The /15 campaign's corpus cases, read as core reads them (spec 2026-10-09 §3.1).
    Lab filters nothing itself: a campaign gets the whole corpus (core runs it on train); ``split`` is the fixed
    evaluation's choice."""
    from evolution_lab.owner import core_raw

    return core_raw.corpus_cases(manifest.workflow.corpus_path, split=split)


def instance_demand(manifest: Any, max_concurrent_sessions: int = 1) -> int:
    """Peak target instances the campaign manifest needs (core's preflight formula, spec 2026-10-09 §4.1); Lab's
    default ``max_instances`` where it picks launch limits itself."""
    from evolution_lab.owner import core_raw

    return core_raw.instance_demand(manifest.workflow.campaign, max_concurrent_sessions)
