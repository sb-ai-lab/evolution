"""The W1 orchestrator: admit an invocation, then hand it to C1 (spec §6).

`evolve` composes what Tasks 1-5 built into one run of `evo tree evolve`. Its
one responsibility is the ordered, write-free admission of a manifest and a run
directory, and the hand-off to the campaign controller:

0. The operator's decisions (PDR-0010) are separated first: the campaign
   journal's own `authorization_applied` records and the supplied
   `run_dir_unsafe_entry` decisions together are the admitted hygiene set that
   steps 1 and 3 judge every run-directory entry against (§5's two sources).
   Each entry those steps admit on a *supplied* decision is kept, for C1 to
   journal after its first durable record.
1. `load_manifest_v2`; anything outside `CAMPAIGN_SCHEMAS` (`/15` and `/16`, which
   produce families 14 and 15; `/5` to `/14` are refused from `2.0.0b2`) is
   `manifest_schema`, before the run directory is read. The run directory is
   then canonicalized: a `run_dir` that is *itself* a symlink is
   `run_dir_unsafe_entry` unless the operator has accepted that exact link, and
   every ancestor is resolved once, so steps 2-7 all work on one real path.
2. The editor's declared environment (its key, its proxy, its forwarded names)
   is read once and becomes the run's secret set; a name that is absent or
   empty is `editor_env_missing`, and an agent CLI that resolves nowhere is
   `editor_program_missing` (E1-editor design §3, §9). Every supplied
   decision's `reason` is then held against that set -- a decision is journaled
   verbatim, so one holding a secret is `record_matches_secret` here rather
   than a record C1 would refuse after the run root exists. Then path
   separation: the canonical `run_dir` is disjoint from `seed_root`, from the
   scorer's working directory and from the editor's own paths -- its working
   directory, or the prompt file and CLI executable an in-process editor seals.
3. Run-dir state: absent or empty is a new run, a `campaign.jsonl` is a resume,
   anything else is `run_dir_not_campaign`; the four output roots W1's own
   writers own (and, for family 14, the probe's `connection-logs` and the CLI's
   `logs`) are admitted by `admit_output_root`. Then the documentation
   snapshot, which is read and compared here and repaired only by C1 (below).
4. The bindings and the corpus (§5). Each binding drops the `PYTHONPATH`
   entries that lie under the seed tree or under the run directory before its
   child environment is sealed, and a drop is announced through `notes` -- one
   sentence naming the program, never the path (amendment A18, PDR-0005 as
   amended). A declared `verbatim_answers` list (ADR-0038) is then checked
   against the train partition, entry by entry: an entry holding a run secret
   is `record_matches_secret` first, then one equal to no oracle string value
   of the train cases is `verbatim_answer_unmatched` -- both before anything is
   written, on a new run and on a resume alike.
5. The campaign `run_id`: derived from the run directory's name -- the name the
   operator wrote, which canonicalization cannot change -- for a new run, read
   from the journal for a resume.
6. The target adapter, built on that `run_id`. No socket is opened here: the
   adapter connects on `preflight`, inside C1's admission.
7. `run_campaign` or `resume_campaign`, with the `secrets` of step 2, the
   supplied decisions, the hygiene applications of step 0 and the CLI's
   process-log callback C1 calls at its last refusal point -- including
   C1's own creation probe, which can still refuse before the run root exists
   (`corpus_too_large`, `record_matches_secret`, `campaign_family_unsupported`).

**W1 creates nothing under a run directory it does not already own.** Steps 1-6
are read-only on a new run, so a refused invocation leaves the run directory
exactly as it found it and the same command can be retried. The run root is
C1's to create (declare-then-create), and the four output roots are created by
the scorer, editor and Reflector bindings on their first use -- and, for
`environment-docs`, by the campaign right after its first durable record --
which is after C1's admission. No socket is opened before step 7 either.

**There is no exception.** `_environment_docs` on a *resume* once rewrote a
missing or damaged snapshot during admission, so an invocation refused a moment
later for an unrelated identity difference had nevertheless written into the run
directory. It now only reads: when the live documentation still has the
journaled digest and the stored copy is missing or no longer verifies, the pair
it returns carries `environment_docs_repair=True`, and C1 repairs its own
directory after its consequence records (ADR-0015 §6.2, codex round 1).

**One refusal type.** `evolve` raises exactly `EvolveRefused` for every refusal,
whoever detected it: its own five codes, the manifest loader's, the bindings'
and the corpus's, and C1's own pre-admission refusals -- a `preflight` code such
as `not_ready`, `run_identity_changed` from a resume whose manifest yields
another identity (PDR-0005), `run_root_not_empty`, `campaign_absent`,
`run_id_mismatch`. Each keeps the code its owner gave it. That is a deliberate
narrowing of spec §6, which converts the bindings' and C1's refusals but is
silent on the loader's: a caller that must distinguish "refused to start" from
"ran and failed" should not have to know five exception families to do it, and
the CLI (Task 7) prints one line, `refused: <code>[ at <path>]`, from one place.

**Authorizations.** `authorizations` and `file_authorizations` carry the
operator's decisions (PDR-0010): `authorizations.AUTHORIZABLE_CODES` is the
whole of what one may accept -- a run whose identity moved on a field that is
not comparable, and a run-directory entry whose hygiene fact the operator
vouches for. The keyword is the decision's `source`: a library caller's
decisions are journaled `caller`, and the decision directory -- read by
`evolve` itself under `decision_directory=True`, which is how the CLI supplies
it, or by a caller that passes `file_authorizations` -- is journaled `file`,
whatever `source` the incoming value carried. Where both sources name one
instance, the caller's decision is the one consumed. A decision for any other code is refused as
`authorization_invalid at code` -- one case of the full re-validation every
supplied decision passes -- before any other admission, and a decision no
record of the run quotes is reported through `notes` as `authorization not
needed` once the campaign returns (spec §5).
`_PRE_WRITE_CODES` is the two classes together, minus W1's own hygiene code,
and is the complete map of what a refusal out of C1 can be named.

C1's refusal types are mapped through that set, never by their message
alone: the same exception types also carry executor faults raised long
after the first durable write, and reporting one of those as a refusal would
both misname it and claim the run directory was untouched. Every refusal that
does escape C1 precedes its first durable write, so no campaign file exists
afterwards -- though the adapter did open a loopback connection for `preflight`.
A `RolloutInfrastructureError` raised after the first event is not a refusal
either: C1 classifies it into the campaign's own `infrastructure_failed`
outcome and `evolve` returns that result.

`EvolveRefused` and `admit_output_root` are defined in
`execution/output_admission.py` and re-exported here. That module owns refusal
values and output-directory admission. `exit_code_for` lives here, beside the
run it maps, so the outcome-to-exit map is unit-testable without a CLI runner.

PDR-0003: `evolve` takes a manifest path and a run directory and nothing else.
There is no override of anything the manifest declares -- not the rounds, not
the seed, not the model, not a secret. `cancelled` and `adapter_factory` are
mechanisms, not semantics: the first is the CLI's signal flag, the second is how
a test injects an in-memory adapter in place of the loopback one.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

from evolution.prompts import (
    TREE_AGENT_GROUPER_SYSTEM,
    TREE_GROUPING_MODEL_SYSTEM,
    TREE_GROUPING_MODEL_V3_SYSTEM,
)
from evolution.tree.artifacts.attestation import PromotionCapableAdapter
from evolution.tree.baseline_import import BaselineImport, BaselineSourceUnavailable
from evolution.tree.bindings import (
    BindingError,
    _sealed_prompt,
    bound_child_environment,
    build_editor,
    build_environment,
    build_grouper_command,
    build_reflector,
    build_scorer,
    editor_environment_names,
    editor_environment_paths,
    editor_environment_values,
    editor_program_paths,
    grouper_agent_seal,
    grouper_environment_names,
    grouper_environment_paths,
    grouper_environment_values,
    grouper_model_credentials,
    grouper_program_paths,
    reflector_environment_names,
    reflector_environment_paths,
    reflector_environment_values,
    reflector_program_paths,
)
from evolution.tree.campaign import (
    CAMPAIGN_FILE,
    REMOVED_FAMILY_GUIDANCE,
    ArtifactLedgerMissing,
    AuthorizedIdentityChanged,
    BaselineImportUnavailable,
    CampaignExists,
    CampaignFamilyUnsupported,
    CampaignInvariantViolated,
    CampaignMissing,
    CampaignResult,
    CorpusTooLarge,
    FindingsPending,
    HygieneNotAuthorizable,
    IdentityChanged,
    LineageChampionUnverified,
    LineageFamilyUnsupported,
    LineageModeUnsupported,
    LineageNotFinished,
    PolicyContradictsPrefix,
    ReboundSourceCorrupt,
    RecordMatchesSecret,
    UndeclaredSibling,
    finish_owed_terminal,
    owes_applied_terminal,
    raised_after_first_write,
    resume_campaign,
    run_campaign,
)
from evolution.tree.campaign_identity import (
    FINAL_ADAPTATION_PROFILE,
    AdaptationIdentityV1,
    CapabilitiesInsufficient,
    build_adaptation_identity,
)
from evolution.tree.campaign_policy import ReflectionCapacityError
from evolution.tree.campaign_projections import (
    CampaignUnreadable,
    read_family_version,
    read_run_id,
)
from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION
from evolution.tree.execution.authorizations import (
    AUTHORIZABLE_CODES,
    AUTHORIZATIONS_DIR,
    HARD_CODES,
    Authorization,
    AuthorizationConflict,
    AuthorizationError,
    RefusalDifference,
    hygiene_instance_digest,
    read_decisions,
)
from evolution.tree.execution.check_modes import ENFORCE_ALL, CheckModes, HitSink
from evolution.tree.execution.config import ManifestError
from evolution.tree.execution.corpus import CorpusError
from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE
from evolution.tree.execution.interaction import EvalCase, RolloutInfrastructureError, Scorer
from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES
from evolution.tree.execution.journal_identity import IdentityError, canonical_json
from evolution.tree.execution.output_admission import (
    OUTPUT_ROOTS,
    EvolveRefused,
    HygieneFinding,
    RefusalFinding,
    admit_output_root,
    collect_output_root_findings,
    output_roots,
)
from evolution.tree.execution.rollout_scoring import _close_scorer_traces
from evolution.tree.execution.wire_v2 import MAX_RUN_ID
from evolution.tree.manifest.manifest_v2 import (
    SCHEMA_V15,
    SCHEMA_V16,
    ManifestV2,
    WorkflowSections,
    effective_group_text_chars,
    load_manifest_v2,
)
from evolution.tree.roles.config import (
    GROUPING_V2_MINIMUMS,
    AgentCliGrouperSection,
    ChatEditorSection,
    CommandGroupingSection,
    FieldsGroupingSection,
    ModelGroupingSection,
)
from evolution.tree.roles.editor.subprocess_editor import SubprocessEditor
from evolution.tree.roles.environment_docs import (
    EnvironmentDocsError,
    _v2_check_document_snapshot,
    _v2_create_document_snapshot,
    _V2DocumentSnapshot,
)
from evolution.tree.roles.grouping.grouping_runtime import (
    AdaptiveCampaignRuntimeConfig,
    SelectionRuntimeConfig,
    family_membership_for,
)
from evolution.tree.roles.grouping.reflect_grouping import FieldFeature
from evolution.tree.roles.reflector_contracts import (
    MIN_NEEDLE_LENGTH,
    oracle_needles,
    oracle_strings,
)
from evolution.tree.storage.journal_errors import JournalFault
from evolution.tree.target.config import _LOOPBACK_HOSTS
from evolution.tree.target.connection_contract import SecretOraclePaths, VerbatimAnswers
from evolution.tree.target.connection_probe import secret_order
from evolution.tree.target.http_adapter import HttpSessionAdapter
from evolution.tree.target.target_calls import TargetCallLedger

__all__ = [
    "RUN_DIR_FINDING",
    "EvolveRefused",
    "admit_output_root",
    "canonical_run_dir_finding",
    "check_corpus_placement",
    "check_path_separation",
    "derive_run_id",
    "evolve",
    "exit_code_for",
    "journaled_authorization_ids",
    "journaled_hygiene_digests",
    "redactions_for",
    "run_dir_state",
    "short_forwarded_names",
    "unauthenticated_roles",
    "unencrypted_model_roles",
]

#: The name the run directory's own hygiene finding carries (PDR-0010).
#:
#: A constant, and not `run_dir.name`, for the reason the four output roots are
#: constants: a `HygieneFinding.name` is journaled as the `path` of an
#: `authorization_applied`, whose grammar is the journal's identifier grammar.
#: An operator may call a run directory `run with space`, and deriving the
#: finding's name from it made the refusal printable and the decision it
#: published unjournalable -- the retry wrote `campaign_created` and then failed
#: with `invalid journal record`. The observed fact stays in the
#: instance digest, which is what binds a decision to one link target, so
#: nothing about the entry's identity is lost by naming the entry after its
#: role: there is exactly one run directory per campaign.
RUN_DIR_FINDING = "run_dir"

#: The only manifest schemas that produce a campaign (PDR-0038, ADR-0046).
#: `_manifest` checks these before reading the run directory, including for
#: hand-built `ManifestV2` inputs; the documentation contract reads this tuple too.
CAMPAIGN_SCHEMAS: tuple[str, ...] = (SCHEMA_V15, SCHEMA_V16)

#: Spec D4: the refusal names the replacement.
_MANIFEST_SCHEMA_EXPECTED: Final = "expected evolution-v2-manifest/15 or /16"

#: Characters ADR-0007's identifier grammar admits in a `run_id`.
_UNSAFE_RUN_ID = re.compile(r"[^A-Za-z0-9._-]")

#: `"c-"` plus the sanitized name must stay inside the wire's `run_id` bound.
_RUN_ID_PREFIX = "c-"
_MAX_DERIVED = MAX_RUN_ID - len(_RUN_ID_PREFIX)

#: Every code the campaign can raise before its first durable write (spec §3,
#: §4): the class table itself, minus the one hygiene code that never leaves C1
#: -- `run_dir_unsafe_entry` is W1's own, raised by `admit_output_root` and
#: `_canonical_run_dir` before C1 is called at all.
#:
#: The set is closed on purpose: C1 also raises
#: `CampaignInvariantViolated(str(schema_error))` while appending to the campaign
#: journal -- long after admission, with a journal already on disk -- and such a
#: message is a lifecycle rule ("invalid journal record"), not a code, so it is
#: in neither class and propagates unchanged. Mapping every one of these
#: exceptions by its message would report an executor bug as a refusal
#: ("refused: invalid journal record", exit 2) and would claim a run directory
#: was left as it was found when it was not.
_PRE_WRITE_CODES = (AUTHORIZABLE_CODES | HARD_CODES) - {"run_dir_unsafe_entry"}

#: The shortest forwarded `allowed_env` value `_secrets` will treat as a secret.
#: Eight characters is below every credential format this project meets and above
#: every flag one (`1`, `true`, `C`, `en_US`), which is the whole of the rule.
_MIN_SECRET_LENGTH = 8

#: Spec §6: the exit code reports the campaign's outcome, not the journal's
#: finalization. Every terminal other than `finished` is a failure of storage.
_EXIT_FOR_OUTCOME: dict[str, int] = {
    "completed": 0,
    "stopped": 3,
    "budget_exhausted": 4,
    "infrastructure_failed": 1,
}


def derive_run_id(run_dir: Path) -> str:
    """The `run_id` a new run takes from its run directory's name (ADR-0007).

    Lower-cased, every character outside `[A-Za-z0-9._-]` replaced by `-`,
    truncated to 93 characters and prefixed `c-`, so the complete id is at most
    95 characters -- `wire_v2.new_instance_id`'s bound, which keeps every
    `<run_id>-<32 hex>` instance id inside the 128-character grammar.

    `evolve` calls this with the path the *operator* gave, not the canonical one
    it derives from it: §6 step 5 says "the run directory's name", and the name
    an operator can read off their own command line is the one that belongs in
    an id they will see again. The two agree in any case -- resolving symlinks
    changes ancestors, never the last component, and a run directory that is
    itself a symlink is refused before this point -- so the choice is a
    statement of intent rather than a difference in behaviour.
    """

    # `abspath`, not `realpath`: this must not touch the filesystem, because the
    # run directory need not exist yet. It collapses `..` lexically, so
    # `runs/x/..` yields `runs`, which is the same name the shell shows and is
    # what a lexical rule should say about a path nobody has resolved.
    name = Path(os.path.abspath(run_dir)).name
    return _RUN_ID_PREFIX + _UNSAFE_RUN_ID.sub("-", name.lower())[:_MAX_DERIVED]


def check_path_separation(run_dir: Path, seed_root: Path, program_paths: Sequence[Path]) -> None:
    """Refuse a run directory that overlaps the seed or a declared program.

    Disjoint means neither equal to nor inside the other, in both directions,
    over canonical (`realpath`) paths. Without it C1's first seed scan would
    take the campaign's own files as part of the seed and the store's identity
    check would then fail with `run_identity_changed` -- a confusing report of a
    layout mistake. The check reads the filesystem and writes nothing; a path
    that does not exist yet canonicalizes lexically, which is what a run
    directory C1 has not created yet needs.

    Two rules, because a `program_path` is one of two things:

    * a **directory** -- a declared program's working directory, as it always
      was -- is disjoint in both directions, the rule above;
    * a **file** the binding seals -- an in-process editor's `system_prompt` or
      an agent CLI's executable (E1-editor design §9) -- may not *lie under* the
      run directory, and that is the whole of it. The reverse containment is not
      a rule a file can break, and judging a file by the directory that holds it
      would refuse the natural layout: a prompt beside the manifest, with
      `--run-dir` a sibling under the same workspace root, has the manifest's
      own directory as the prompt's parent and that directory holds the run.

    A path that does not exist is judged as a directory, which is W1's behaviour
    for a declared `cwd` that no longer exists and keeps this check from
    indicting a whole parent tree for a missing directory.
    """

    run = Path(os.path.realpath(run_dir))
    if _overlaps(run, Path(os.path.realpath(seed_root))):
        raise EvolveRefused("run_dir_overlaps_seed")
    for program in program_paths:
        resolved = Path(os.path.realpath(program))
        overlaps = resolved.is_relative_to(run) if resolved.is_file() else _overlaps(run, resolved)
        if overlaps:
            raise EvolveRefused("run_dir_overlaps_program")


def _overlaps(one: Path, other: Path) -> bool:
    return one == other or one.is_relative_to(other) or other.is_relative_to(one)


def check_corpus_placement(
    manifest: ManifestV2 | Path,
    *,
    run_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refuse a corpus file that the campaign or a role could read (#234, #248).

    The rule `evolve` applies to a current campaign manifest before anything is
    written, for a library caller that prepares a campaign before the launch
    machine is known: the corpus is refused as `corpus_inside_role_workspace` when it lies inside
    `seed_root`, the run directory or a **declared** program path (the scorer's
    `cwd`, a role's working directory, prompt or declared `agent_bin`, a command
    grouping's `cwd`), or is the same file as a declared prompt or program
    file, through a symlink or a hard link too.

    Declared paths only (#248, maintainer ruling 2026-10-16, option A): an
    `agent_cli` role without `agent_bin` is not looked up on `PATH`, so this
    function never reports `editor_program_missing`. `evolve` additionally
    keeps the corpus apart from the CLI it resolves on the bound `PATH`; that
    comparison (`samefile`) is the only check `evolve` makes that this function
    does not.

    `manifest` is a path or an already loaded document; a path that does not
    load raises the loader's refusal, as `evolve` does. A document with no
    workflow gives `None`. `run_root`, when given, is canonicalized (`realpath`)
    as `evolve` canonicalizes its run directory and need not
    exist; `None` skips the run-directory check. `environ` is accepted for
    callers of `2.0.0b4` and is no longer read. It does not check the role
    environment's keys; `evolve` does that first. Reads the filesystem;
    writes nothing.
    """

    del environ  # #248: declared paths need no environment
    document = _loaded_manifest(manifest)
    workflow = document.workflow
    if workflow is None:
        return None
    try:
        program_paths = _program_paths(workflow, {}, resolve=False)
    except BindingError as exc:
        raise EvolveRefused(exc.code, exc.path, detail=exc.detail) from exc
    root = None if run_root is None else Path(os.path.realpath(run_root))
    _refuse_corpus_placement(document, root, program_paths)
    return None


def _program_paths(
    workflow: WorkflowSections, environ: Mapping[str, str], *, resolve: bool = True
) -> tuple[Path, ...]:
    """Every declared program path the run directory and the corpus are held
    apart from: the scorer's `cwd`, each role's declared paths and a command
    grouping's `cwd`. Raises the bindings' `BindingError` for a path that cannot
    be resolved, such as an agent CLI missing from the bound `PATH`.
    `resolve=False` skips CLI resolution and returns declared paths only."""

    return (
        workflow.scorer.cwd,
        *editor_program_paths(workflow.editor, environ, resolve=resolve),
        *reflector_program_paths(workflow.reflector, environ, resolve=resolve),
        *(
            grouper_program_paths(workflow.grouper, environ, resolve=resolve)
            if workflow.grouper is not None
            else ()
        ),
    )


def _refuse_corpus_placement(
    manifest: ManifestV2, root: Path | None, program_paths: Sequence[Path]
) -> None:
    """The corpus placement rule itself, shared by `evolve` and
    `check_corpus_placement`. `root` is the canonical run directory, or `None`
    when the helper skips that check."""

    workflow = manifest.workflow
    assert workflow is not None
    corpus_path = workflow.corpus_path.resolve()
    if any(
        (
            corpus_path.is_relative_to(directory.resolve())
            or (directory.is_file() and corpus_path.samefile(directory))
        )
        for directory in (
            manifest.seed_root,
            *(() if root is None else (root,)),
            *program_paths,
            *_grouping_prompts(workflow),
        )
    ):
        raise EvolveRefused("corpus_inside_role_workspace")


def run_dir_state(
    run_dir: Path, *, admitted: Collection[str] = (), roots: Sequence[str] = OUTPUT_ROOTS
) -> Literal["new", "resume"]:
    """Whether this run directory starts a campaign or continues one (spec §6 step 3).

    Resume is a fact of the directory, not a flag: `campaign.jsonl` is present
    or it is not. An empty or absent directory is a new run (an absent one needs
    an existing parent directory: `run_dir_parent_missing` otherwise); a directory holding
    anything else -- ignoring the output roots `roots`, which are the workflow's
    own writers' -- is `run_dir_not_campaign`, because a campaign must never be
    laid down on top of somebody else's files. `roots` is the manifest's
    `output_roots(schema)`: the four W1 and Reflector roots, and for a family-14
    run also the connection probe's `connection-logs` and the CLI's `logs`; an
    older family keeps the four, so both are foreign entries and never judged.
    The roots are admitted here, before
    preflight and before any write, and the walk to them proves no component of
    `run_dir` is a symlink. `evolve` calls this with a path it has already
    canonicalized, so there the walk can only fire for a component swapped after
    that; a direct caller gets the check on whatever it passed.

    `admitted` is the operator's accepted hygiene instance digests (PDR-0010):
    a root whose finding is among them is admitted rather than refused. The
    findings themselves are not returned -- this function answers one question
    -- so a caller that must journal what it accepted walks the same roots
    itself; `evolve` does.
    """

    root = Path(run_dir)
    # An existing path that is not a directory holds no campaign, and saying so
    # is a better diagnosis than the unsafe-entry refusal the walk to the output
    # roots would otherwise raise for it. A *symlinked* run directory is not
    # this case: it stays the admission's to refuse.
    try:
        entry = root.lstat()
    except OSError:
        entry = None
    if entry is not None and not stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
        raise EvolveRefused("run_dir_not_campaign", detail="not a directory")
    # An absent run directory is created by the journal as a leaf, never with
    # its ancestors: the workflow writes only inside its own run directory. A
    # missing or non-directory parent is therefore a refusal here, before any
    # write, rather than `journal_unavailable` after admission (W2 F1).
    if entry is None and not root.parent.is_dir():
        raise EvolveRefused("run_dir_parent_missing")
    for name in roots:
        admit_output_root(root / name, admitted=admitted)
    try:
        entries = set(os.listdir(root))
    except FileNotFoundError:
        return "new"
    except OSError as exc:  # not a directory, or unreadable
        raise EvolveRefused("run_dir_not_campaign", detail=str(exc)) from exc
    if CAMPAIGN_FILE in entries:
        return "resume"
    # The operator's own decision directory is not a foreign entry: `evo tree
    # authorize` writes it before the campaign exists, which is exactly the
    # shape a `run_dir_unsafe_entry` decision is written in, and calling the run
    # directory foreign afterwards would make that decision unusable. Nothing
    # here reads it; `read_decisions` is the guard on its own safety.
    if entries - set(roots) - {AUTHORIZATIONS_DIR}:
        raise EvolveRefused("run_dir_not_campaign")
    return "new"


def exit_code_for(result: CampaignResult) -> int:
    """The process exit code for a campaign that ran (spec §6).

    `0` for a completed campaign whether or not a Candidate was promoted, `3`
    for a cancellation, `4` for an exhausted budget, `1` for an infrastructure
    failure, either storage terminal, or `projection_invalid` even when the
    recorded terminal is finished. A refusal never reaches here: it is the CLI's
    exit `2`.

    A family-7 suspension (I1 spec §5.5) is `1` through the same branch every
    non-`finished` terminal takes: exit `1` keeps its meaning -- infrastructure,
    storage, or Core reporting -- and the same `evo tree evolve` resumes a
    suspended run after repair; a finished run is read with `evo tree watch`.
    """

    if result.code == "projection_invalid" or result.terminal != "finished":
        return 1
    finished = result.finished or {}
    return _EXIT_FOR_OUTCOME.get(str(finished.get("outcome")), 1)


def _admitted(
    decisions: Sequence[Authorization], source: Literal["caller", "file"]
) -> tuple[Authorization, ...]:
    """Re-validate one keyword's decisions and stamp them with *its* source (§5).

    The returned values are what the run carries: `from_payload` is the
    validation every decision passes, whether it came from a file or from a
    library caller, and the `source` it is given here is the keyword's, never
    the one the incoming value happened to hold.
    """

    admitted: list[Authorization] = []
    for decision in decisions:
        try:
            admitted.append(Authorization.from_payload(decision.to_payload(), source=source))
        except AuthorizationError as exc:
            raise EvolveRefused("authorization_invalid", exc.path) from exc
    return tuple(admitted)


async def evolve(
    manifest_path: Path | ManifestV2,
    run_dir: Path,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    adapter_factory: Callable[[ManifestV2, str], PromotionCapableAdapter] | None = None,
    notes: Callable[[str], None] | None = None,
    environ: Mapping[str, str] | None = None,
    ledger: TargetCallLedger | None = None,
    authorizations: Sequence[Authorization] = (),
    file_authorizations: Sequence[Authorization] = (),
    decision_directory: bool = False,
    process_log: Callable[[Path, frozenset[str]], None] | None = None,
) -> CampaignResult:
    """Run, or continue, the campaign one `/3` manifest declares (spec §6).

    Raises :class:`EvolveRefused` for every refusal, before any write under
    `run_dir` and, for steps 1-6, before any socket is opened.

    `process_log` is the CLI's process-log callback (PDR-0030, ADR-0036):
    called at most once at this invocation's last refusal point, with the
    canonical run root and admitted hygiene digests. Its errors are ignored.
    A caller that passes nothing gets no log.

    `authorizations` and `file_authorizations` are the operator's decisions
    (PDR-0010): each accepts one instance of one code in
    `authorizations.AUTHORIZABLE_CODES`, by the `instance_digest` the refusal
    published, and nothing else -- a decision for any other code is
    `authorization_invalid at code` before any other admission, and one whose
    `reason` holds a run secret is itself `record_matches_secret`. The two
    keywords are the two `source` values §5 defines, and which one a decision
    arrived through is what core journals: everything in `authorizations` is
    re-validated as `caller` and everything in `file_authorizations` as `file`,
    whatever `source` the value carried in. `Authorization` is an exported
    dataclass whose constructor takes `source`, so preserving it would make the
    audit record's one objective fact -- whether a decision file existed --
    caller-controlled. `file_authorizations` is what a caller
    that reads the directory itself passes; a library caller that has no
    decision directory uses `authorizations`. Matching order is the journal,
    then `authorizations`, then `file_authorizations`, and the first match in
    that order wins for hygiene as it already did for identity. A decision this
    run did not consume is no error: it is one `authorization not needed` note
    once the campaign returns.

    `decision_directory` makes `evolve` read `<run_dir>/authorizations/` itself,
    and is how the CLI passes the operator's decision directory. It is read
    *after* the run directory is canonicalized and through that canonical root,
    because a caller that reads it first reads it through the name it was
    given: a `run_dir` symlink re-pointed from campaign A to campaign B between
    the two calls would hand A's decisions to an invocation that then inspects
    and mutates B, journaling a matching digest in B with A's reason and file
    provenance (PDR-0010). Its decisions join
    `file_authorizations`, after them, and are journaled `file` like them. A
    malformed directory is `authorizations_invalid at <name>`, a hard refusal
    before any write. A hygiene instance the campaign journal already carries
    needs no decision on a later invocation;
    an identity change does not become standing authority, because the journal
    records the identity it moved to.

    `manifest_path` may be the admitted document itself (`admit_manifest`) and
    `environ` a snapshot of the process environment, so a caller that also
    derives the redaction set and the short-value notes -- the CLI -- reads the
    manifest and the environment exactly once and every derivation sees the
    same document and the same values (W2 fix pack, the two-read finding).

    `notes` is the caller's sink for a remark this invocation has to make about
    something it nevertheless accepts. It receives one complete, path-free
    sentence per remark, without the command prefix the CLI adds. The
    `PYTHONPATH` remarks are said during admission, before the campaign starts;
    the `authorization not needed` remarks of §5 are said after it returns,
    because only the journal says which decisions a run consumed. A refused
    invocation says neither: the refusal is the answer. Unlike
    `short_forwarded_names` and `redactions_for`, which the CLI reads from the
    manifest before `evolve` runs, these facts are only known once the bindings
    exist, and `evolve` is the one place that builds them.

    `ledger` is the caller's `TargetCallLedger` (ADR-0014). `evolve` binds it
    to the campaign's `run_id` — new or journaled — before the adapter exists,
    and the default adapter accounts every mutating target call through it. A
    custom `adapter_factory` receives no ledger: passing it to the transports
    the factory builds is the factory's job, and `bound_transports` shows
    whether it did. A refusal raised before the adapter exists — steps 1-6,
    including the manifest, the run directory and the bindings — leaves the
    ledger unbound. A refusal raised after that point — one of C1's own
    pre-admission refusals, a `preflight` failure, `reflection_capacity`,
    `invalid_family_value` — leaves the ledger bound to the campaign's
    `run_id`, and possibly holding calls the adapter already admitted.
    Nothing about the ledger is journaled.
    """

    # Spec §5, before any other admission: every supplied decision is admitted
    # through exactly the validation a decision read from the directory passes.
    # `Authorization` is an exported frozen dataclass, so a library caller can
    # construct one directly, and its constructor checks nothing: a malformed
    # digest would be carried the length of the run and match nothing, while an
    # empty reason or a free-text `issued_at` could match a real refusal and
    # fail much later, as a journal schema violation after the first durable
    # write. Round-tripping the value through `from_payload` is the one check --
    # a code no decision can make admissible is its `code` case -- and its
    # `path` is the key at fault, which is what the refusal prints. The `source`
    # each value comes back with is this boundary's, not
    # the caller's: the two keywords *are* the two sources, so a hand-built
    # `Authorization(..., source="file")` is journaled `caller` like every other
    # library decision, and only the decision directory -- read below when
    # `decision_directory` says so, or by a caller that hands its contents to
    # `file_authorizations` -- produces a `file`.
    caller_decisions = _admitted(authorizations, "caller")
    file_decisions = _admitted(file_authorizations, "file")

    manifest = _manifest(manifest_path)
    workflow = manifest.workflow
    assert workflow is not None  # `_manifest` refuses a document without one
    # The run directory's output roots: the four, and for family 14 the
    # connection probe's `connection-logs` and the CLI's `logs` (ADR-0036). Every walk
    # below -- the hard pass, the collection, `run_dir_state`, the journaling
    # pass -- judges exactly these.
    roots = output_roots(manifest.schema)

    # One reading of the run directory decides everything below: the link is
    # read once, the root every later step uses is derived from *that* target,
    # and the journal is read through that root rather than through the name
    # the caller gave. A link re-pointed from campaign A to campaign B after
    # this line changes nothing this invocation does; reading the journal
    # through `given` first would have applied A's standing authority while
    # inspecting and running B, admitting an identical unsafe entry in B that B
    # never authorized (PDR-0010). The finding is judged a few
    # lines below, once the journal it is judged against has been read, because
    # the run directory's own link is one of the instances the journal can
    # already carry.
    root, finding = _resolve_run_dir(Path(run_dir))

    # §7's decision directory, read here and nowhere earlier: through `root`,
    # the target the line above canonicalized, rather than through the name the
    # caller gave. It is the same reasoning as the journal's, one step further
    # (PDR-0010) -- a link re-pointed after that line changes
    # neither the journal this invocation reads nor the decisions it applies to
    # it -- and the reads inside `read_decisions` are anchored to the directory
    # descriptor it opened, so an entry swapped between the listing and the read
    # cannot redirect one either.
    if decision_directory:
        try:
            file_decisions = (*file_decisions, *_admitted(read_decisions(root), "file"))
        except AuthorizationError as exc:
            # `exc.path` is the entry's name, never its path (spec §9).
            raise EvolveRefused(exc.code, exc.path) from exc
    supplied = (*caller_decisions, *file_decisions)

    # The two sources of admitted hygiene (spec §5), read before the first
    # entry is judged. Source 1 is the campaign journal's own
    # `authorization_applied` records -- standing authority for the instances it
    # already carries, so a resume of a run whose `scorer-traces` the operator
    # accepted needs no decision at all. Source 2 is what this invocation was
    # given. A new run directory needs no guard of its own: the reader is best
    # effort over `OSError`, so an absent journal contributes the empty set.
    journaled_hygiene = journaled_hygiene_digests(root)
    # First match wins, over `supplied`'s own caller-then-file order: §5 fixes
    # the sources as "the journals ... the library argument, the decision
    # directory", and a plain comprehension would let the later file decision
    # replace the caller's for the same instance, journaling the directory's
    # reason and `source: file` (ADR-0015). The identity path
    # already resolves its match this way, through `match_authorization`.
    supplied_hygiene: dict[str, Authorization] = {}
    for authorization in supplied:
        if authorization.code == "run_dir_unsafe_entry":
            supplied_hygiene.setdefault(authorization.digest, authorization)
    # PDR-0010 (amended 2026-09-17): a finished campaign takes no authorization.
    # It has nothing left to run and no consequence record the lifecycle would
    # accept after `campaign_finished`, so a decision for one of its entries
    # cannot be applied -- and advertising the `evo tree authorize` line for it
    # sent the operator to write a decision that the retry then failed on, deep
    # inside C1. The journal's own `authorization_applied`
    # records keep their standing authority: an entry this campaign already
    # admitted stays admitted, whatever its terminal.
    finished = _journal_finished(root)
    if finished:
        supplied_hygiene = {}
    admitted_hygiene = journaled_hygiene | frozenset(supplied_hygiene)
    # Every finding this invocation admits on a *supplied* decision, for C1 to
    # journal after its first durable record. A finding the journal already
    # carries is left out: C1 deduplicates too, but the record belongs to the
    # invocation that made the decision, not to every later resume.
    applications: list[tuple[HygieneFinding, Authorization]] = []

    if finished:
        # Every entry this campaign has not already admitted is hard, and it is
        # judged here rather than at each guard below so that one statement --
        # "a finished campaign takes no authorization" -- covers the run
        # directory and the output roots alike. The walk is the same walk
        # `_judge_run_dir` and `run_dir_state` make a few lines down, against
        # the same reduced `admitted_hygiene`, so they find nothing left to
        # refuse; only the digest is dropped, which is what makes the CLI print
        # `class: hard` and no command (PDR-0010).
        _refuse_hygiene_hard(root, finding, admitted_hygiene, roots)

    # Comparable-links spec §6: on an unfinished or new campaign every
    # `run_dir_unsafe_entry` of the run directory is collected rather than
    # raised -- the run directory's own link here, the output roots below
    # -- so one refusal can report all of them beside the identity or link
    # finding C1 detects. A finished campaign was judged above, hard, and has
    # nothing left to collect.
    pending: list[HygieneFinding] = []
    if finding is not None and finding.instance_digest not in admitted_hygiene:
        pending.append(finding)
    elif finding is not None and finding.instance_digest not in journaled_hygiene:
        applications.append((finding, supplied_hygiene[finding.instance_digest]))

    # Comparable-links spec §5.3: an applied close or terminal decision is
    # standing authority and owes one record. It is written here -- after the
    # manifest's schema check and the run directory's path safety, before any
    # binding, target preflight, identity comparison, documentation check or
    # sibling -- because none of them can change what the journal already
    # decided. It runs only once the run directory's own link is admitted,
    # journaled or supplied: recovery journals nothing but the terminal, so a
    # decision supplied for that link is simply left unconsumed and noted. On
    # a prefix that owes nothing, an unaccepted link is a collected finding
    # like any other, and the path below refuses it before any write.
    #
    # The family probe precedes both the owed terminal and the collected
    # findings: from `2.0.0b2` this release neither resumes nor closes a run
    # of an unadmitted family (PDR-0024, PDR-0026, ADR-0030), so it must not
    # write that run's owed terminal, nor offer its unsafe entries for
    # authorization. A finished run was judged hard above and stays so.
    if (root / CAMPAIGN_FILE).exists():
        _refuse_unadmitted_family_at(root)
    if pending and owes_applied_terminal(root):
        # The link alone stands between this prefix and its owed terminal:
        # accepting it is all the recovery needs, so it is the whole refusal,
        # before any binding. Collecting on would also report an identity
        # change the journal has already decided (§5.3, §6).
        raise EvolveRefused.from_findings((_as_finding(pending[0]),))
    if not pending and (root / CAMPAIGN_FILE).exists():
        try:
            owed = finish_owed_terminal(
                root,
                process_log=None
                if process_log is None
                else lambda run_root: process_log(run_root, admitted_hygiene),
            )
        except JournalFault as exc:
            # A second writer holds the campaign lock over a prefix that owes
            # its terminal: a refusal before any write. Every other journal
            # fault is reported by the helper as a result, as resume reports it.
            if exc.code != "journal_busy":
                raise
            raise EvolveRefused(exc.code) from exc
        if owed is not None:
            _note_unconsumed(root, supplied, notes)
            return owed

    scorer = None
    try:
        # §6, the output roots: each judged independently, each unadmitted
        # guard finding collected. "Not a directory" and an unreadable entry
        # stay hard and raise here, ahead of the collected link finding.
        pending.extend(collect_output_root_findings(root, admitted_hygiene, roots=roots))
        # §6 reading 7: "if none of those entries is the campaign journal
        # itself" holds by construction -- no guarded entry is `campaign.jsonl`.
        assert all(entry.name != CAMPAIGN_FILE for entry in pending)
        # Admitted for detection only: every later walk -- `run_dir_state`,
        # the output-root pass, the four builders -- treats a collected finding
        # as admitted so none of them raises it a second time. The application
        # list is still built only from findings a *supplied* decision matched,
        # and C1 refuses the rest (`FindingsPending`) before ordinary work.
        # State-first cancellation recovery can append without consuming them;
        # the log callback must receive only actually admitted digests.
        provisional = admitted_hygiene | frozenset(entry.instance_digest for entry in pending)
        reached = (
            None
            if process_log is None
            else lambda run_root: process_log(run_root, admitted_hygiene)
        )
        collected = tuple(_as_finding(entry) for entry in pending)
        # One snapshot of the process environment, taken before the first read of
        # it, is the run's whole environment. Every later derivation -- the secret
        # set, the editor's declared program paths, the bound child environment --
        # takes this mapping and never `os.environ` again, so a value another
        # thread rewrites between two of those calls cannot make the editor spawn
        # with a credential or a `PATH` the redaction set and the path-separation
        # check never saw. A caller's own snapshot is taken as given, for the same
        # reason: one mapping for the whole invocation.
        environ = dict(os.environ) if environ is None else dict(environ)

        # The environment the editor names is read once, here, before the first
        # filesystem check and before any write: an absent key is a refusal, and the
        # values it yields are the run's secret set, which `build_editor` receives so
        # the editor's own transcript and C1's journal redact one set (design §9).
        # The editor's declared paths are read in the same breath, because
        # `check_path_separation` must hold them apart from the run directory.
        try:
            secrets = _run_secrets(workflow, environ)
            # #287: where the manifest declares each secret's name, from the
            # same snapshot, for the connection probe's refusal.
            secret_paths = _run_secret_paths(workflow, environ)
            secret_names = _run_secret_names(workflow, environ)
            program_paths = _program_paths(workflow, environ)
        except BindingError as exc:
            raise EvolveRefused(exc.code, exc.path, detail=exc.detail) from exc

        # A decision is journaled verbatim (§6), so its reason obeys the run's own
        # redaction rule, and it obeys it here rather than as a record the campaign
        # journal would refuse after the run root exists. The set is the one
        # `_run_secrets` just built, so the decision and the journal apply one rule.
        for authorization in supplied:
            if any(secret and secret in authorization.reason for secret in secrets):
                raise EvolveRefused("record_matches_secret", f"authorizations/{authorization.id}")

        check_path_separation(root, manifest.seed_root, program_paths)
        state = run_dir_state(root, admitted=provisional, roots=roots)
        # Continuable terminals §6.3, read-only and before any write. A new run
        # directory checks the predecessor path once, here, and hands the resolved
        # directory to C1, which derives the link from it; a run directory that
        # holds a campaign never re-derives the link -- its journaled `LinkedFrom`
        # is the authority -- and only the key's presence and baseline mode must
        # agree. A resume still resolves the path, and C1 reads it solely to
        # complete a declared baseline copy that is still owed (baseline
        # inheritance §5.2); an inadmissible path then refuses as
        # `baseline_import_unavailable`, never `bad_path` (ruling R6).
        linked_from = _admit_linked_from(manifest, root) if state == "new" else None
        if state == "resume":
            linked_from = _resume_linked_from(manifest, root)
        # `run_dir_state` admits the output roots; this second, identical pass keeps
        # the findings it accepted, which only the caller that supplied the decision
        # can journal. Both walks are read-only, so the repetition costs one
        # `lstat` chain per root and buys `run_dir_state` its unchanged one-value
        # contract.
        for name in roots:
            found = admit_output_root(root / name, admitted=provisional)
            if (
                found is not None
                and found.instance_digest not in journaled_hygiene
                and found.instance_digest in supplied_hygiene
            ):
                applications.append((found, supplied_hygiene[found.instance_digest]))
        docs, docs_repair = _environment_docs(workflow, root, state)

        try:
            environment = build_environment(workflow.environment)
            scorer = build_scorer(
                workflow.scorer,
                seed_root=manifest.seed_root,
                run_dir=root,
                environ=environ,
                admitted_hygiene=provisional,
                accepts_diagnostics=True,
            )
            editor = build_editor(
                workflow.editor,
                seed_root=manifest.seed_root,
                run_dir=root,
                environ=environ,
                secrets=secrets,
                admitted_hygiene=provisional,
                role_resilience=True,
                plan_correction=manifest.schema == SCHEMA_V16,
                family15=manifest.schema == SCHEMA_V16,
            )
            reflector = build_reflector(
                workflow.reflector,
                seed_root=manifest.seed_root,
                run_dir=root,
                environ=environ,
                secrets=secrets,
                admitted_hygiene=provisional,
                # Current campaigns select V3; declared prompt bytes still win.
                model_grouping_family=True,
                role_resilience=True,
                bounds_repair=True,
                # #318 (ADR-0048): `/16` chat checks `finish_reflection` in role.
                result_repair=manifest.schema == SCHEMA_V16,
                family15=manifest.schema == SCHEMA_V16,
            )
            from evolution.tree.execution.corpus_partition import load_partition, partition_notes

            # #234: the rule `check_corpus_placement` exposes to library callers.
            _refuse_corpus_placement(manifest, root, program_paths)
            partition = load_partition(workflow.corpus_path)
            reflection_corpus = partition.select("train")
            if notes is not None:
                notes(
                    f"Corpus split: train ({len(partition.train.cases)} cases); held-out test ({len(partition.test.cases)} cases); mode {partition.descriptor['mode']}."
                )
                for message in partition_notes(partition):
                    notes(message)
            # R18: build profile `/7` directly from the train partition and labels.
            final_adaptation: dict[str, Any] = {"corpus_partition": partition.descriptor}
            labels = _provenance_labels(workflow, reflection_corpus)
            if labels is not None:
                final_adaptation["provenance_labels"] = labels
            bind_hits = HitSink()
            modes = workflow.checks or ENFORCE_ALL
            if workflow.verbatim_answers is not None:
                # ADR-0038, ruling Q1: checked against the train partition, the
                # corpus every needle set of the campaign is built from.
                _refuse_verbatim_answers(
                    workflow.verbatim_answers,
                    reflection_corpus.cases,
                    secrets,
                    modes=modes,
                    hits=bind_hits,
                )
            if workflow.secret_oracle_paths is not None:
                _refuse_secret_oracle_paths(
                    workflow.secret_oracle_paths,
                    reflection_corpus.cases,
                    secrets,
                    modes=modes,
                    hits=bind_hits,
                )
            if notes is not None:
                for hit in bind_hits.entries():
                    notes(f"check shadowed: {hit['check']} · bind · count {hit['count']}")
            adaptive = _adaptive_config(
                workflow,
                corpus=reflection_corpus.cases,
                reflection_metadata=reflection_corpus.metadata,
                reflection_metadata_digest=reflection_corpus.metadata_digest,
                environment_docs_digest=None if docs is None else docs.digest,
                seed_root=manifest.seed_root,
                run_dir=root,
                environ=environ,
                admitted_hygiene=provisional,
                final_adaptation=final_adaptation,
                schema=manifest.schema,
            )
        except BindingError as exc:
            raise EvolveRefused(exc.code, exc.path, detail=exc.detail) from exc
        except CorpusError as exc:
            raise EvolveRefused(exc.code, _corpus_path(exc.line)) from exc

        if notes is not None:
            # The `PYTHONPATH` guard is silent in the environment it hands the child
            # and must not be silent to the operator: a program that expected to
            # import from the seed will now fail to, and the reason is here rather
            # than in a traceback. The program, never the path (spec §9). Only a
            # declared subprocess has such an environment; an in-process editor
            # spawns nothing the guard applies to.
            commands = [("scorer", scorer.command)]
            if isinstance(editor, SubprocessEditor):
                commands.append(("editor", editor.binding))
            for program, binding in commands:
                if binding.dropped_pythonpath:
                    notes(
                        f"{program} PYTHONPATH entry under the seed tree or the run directory dropped"
                    )

        # `run_dir`, not `root`: the id carries the name the operator wrote. See
        # `derive_run_id` -- canonicalization cannot change a basename, so this is a
        # statement about which path is authoritative, not a behavioural fork.
        run_id = derive_run_id(Path(run_dir)) if state == "new" else _journaled_run_id(root)
        if ledger is not None:
            ledger.bind_namespace(run_id)
        factory = (
            adapter_factory
            if adapter_factory is not None
            else functools.partial(_http_session_adapter, ledger=ledger)
        )
        adapter = factory(manifest, run_id)

        # The link is derived at creation only: a resume is handed the path solely
        # so C1 can complete a declared baseline copy still owed (baseline
        # inheritance §5.2); it never re-derives the link from it.
        def condition_changed(differences: tuple[RefusalDifference, ...]) -> None:
            # D2a §3: a link across a changed comparable field proceeds; the
            # operator reads the same `differs:` lines a refusal would print --
            # fields and digests, never values (PDR-0012).
            if notes is None:
                return
            notes("condition changed across the link")
            for difference in differences:
                notes(
                    f"differs: {difference.field}  journaled {difference.journaled}  "
                    f"observed {difference.observed}"
                )

        def baseline_inherited(imported: BaselineImport, other_tree: bool) -> None:
            # Baseline inheritance §4.2, §4.3: counts only, never a value.
            if notes is None:
                return
            if imported.imported == 0:
                notes("nothing to inherit from the predecessor's baseline")
                return
            notes(
                f"baseline inherited: {imported.imported} pairs carried, "
                f"{imported.scheduled - imported.imported} to measure"
            )
            if other_tree:
                notes("carried rows were measured on another seed tree")

        entry = (
            functools.partial(
                run_campaign,
                linked_from=linked_from,
                on_condition_changed=condition_changed,
                linked_baseline=manifest.linked_baseline,
                on_baseline_inherited=baseline_inherited,
            )
            if state == "new"
            else functools.partial(resume_campaign, linked_from=linked_from)
        )
        try:
            result = await entry(
                workflow.campaign,
                run_root=root,
                seed_root=manifest.seed_root,
                artifact_policy=manifest.policy,
                adapter=adapter,
                environment=environment,
                # A typing artefact, not a doubt about the scorer: R1's `Scorer`
                # protocol declares `identity` as a settable member, so a frozen
                # `SubprocessScorer` is not a structural subtype of it, exactly as
                # `conformance` casts around the same protocol shape for the
                # adapter. C1 reads `identity` and calls `score`, both of which the
                # bound scorer has.
                scorer=cast(Scorer, scorer),
                editor=editor,
                reflector=reflector,
                # Family 4 exposes documents only through the reader.  The legacy
                # reflector receives no rendered body alongside its adaptive brief.
                environment_docs=docs,
                environment_docs_repair=docs_repair,
                corpus=reflection_corpus.cases,
                run_id=run_id,
                cancelled=cancelled,
                secrets=secrets,
                secret_paths=secret_paths,
                secret_names=secret_names,
                adaptive=adaptive,
                connection=(
                    None
                    if workflow.connection is None
                    else replace(
                        workflow.connection,
                        preflight_sample_retries=manifest.target.preflight_sample_retries,
                    )
                ),
                probe=workflow.reflector.probe,
                authorizations=supplied,
                hygiene_applications=tuple(applications),
                # §6: the collected findings no decision accepted; C1 reports
                # them beside its own identity or link finding, or refuses them
                # alone (`FindingsPending`), before its first write.
                pending_findings=tuple(pending),
                process_log=reached,
                notes=notes,
                **({"bind_hits": bind_hits.entries()} if bind_hits.entries() else {}),
            )
        except IdentityChanged as exc:
            # The one refusal that is data (spec §4): the dotted diff and, unless
            # the change is hard, the digest a decision must quote. Branched on the
            # type rather than the code, because a bare
            # `IdentityError("run_identity_changed")` -- a sibling C1 refuses with
            # no comparison to publish -- is still raised and is still a refusal,
            # just one with nothing to print beyond its name.
            #
            # The effect is the refusal's own (comparable-links spec §5.1): a
            # rebase, a close, or the terminal the campaign already owes. A hard
            # refusal has none. `effects` is every effect a decision may pick,
            # default first (I2 spec §4.3); several only on family 7.
            #
            # §6: an authorizable identity finding follows the collected hygiene
            # in one refusal; a hard one leads, and the outer handler appends
            # the collected findings after it.
            changed = EvolveRefused(
                "run_identity_changed",
                differences=exc.differences,
                instance_digest=exc.instance_digest,
                effect=None if exc.hard else exc.effect,
                effects=() if exc.hard else exc.effects,
            )
            if collected and not exc.hard:
                raise EvolveRefused.from_findings((*collected, *changed.findings)) from exc
            raise changed from exc
        except (
            AuthorizedIdentityChanged,
            ReboundSourceCorrupt,
            PolicyContradictsPrefix,
            AuthorizationConflict,
            AuthorizationError,
        ) as exc:
            # The I2 refusal types, mapped like `_PRE_WRITE_CODES`: a refusal
            # only before this invocation's first durable write. The same types
            # raised after it -- a frozen source that no longer reads back
            # while the block's records are durable -- are faults and propagate
            # unchanged, like every post-write exception (module docstring).
            if raised_after_first_write(exc):
                raise
            raise _condition_change_refusal(exc) from exc
        except BaselineImportUnavailable as exc:
            # Baseline inheritance §5.3: the declared copy is owed and its source
            # cannot yield it. Authorizable: the accepted decision measures the
            # baseline in full. §6: after the collected hygiene, like the
            # identity finding.
            lost = EvolveRefused(
                str(exc), instance_digest=exc.instance_digest, effect="fresh_baseline"
            )
            raise EvolveRefused.from_findings((*collected, *lost.findings)) from exc
        except FindingsPending as exc:
            # §6: C1 found nothing to report beside the collected hygiene --
            # the identity is admitted or the link needs no decision -- and
            # refused before its first write. The collected findings are the
            # whole refusal.
            raise EvolveRefused.from_findings(collected) from exc
        except HygieneNotAuthorizable as exc:
            # C1's own copy of "a finished campaign takes no authorization"
            # (PDR-0010, amended 2026-09-17). `evolve` refuses the same case during
            # admission and a library caller that assembled the applications itself
            # does not pass through that check, so C1 refuses it once more, before
            # its first append. No digest: there is nothing a decision could accept.
            raise EvolveRefused(exc.code, exc.name, detail=exc.guard) from exc
        except (
            CampaignExists,
            CampaignMissing,
            CampaignFamilyUnsupported,
            CorpusTooLarge,
            RecordMatchesSecret,
            CampaignInvariantViolated,
            IdentityError,
            UndeclaredSibling,
            ArtifactLedgerMissing,
            CapabilitiesInsufficient,
            LineageNotFinished,
            LineageFamilyUnsupported,
            LineageModeUnsupported,
            LineageChampionUnverified,
            BaselineSourceUnavailable,
        ) as exc:
            # C1's own pre-admission refusals, and only those: anything else these
            # types carry is an executor fault after a durable write, which is not a
            # refusal and must not be reported as one.
            if str(exc) not in _PRE_WRITE_CODES:
                raise
            # The public location rides in `path` -- the slot the contract prints --
            # never in `detail` (spec §6): the journaled family, or where in the
            # creation record the redaction would refuse.
            path: str | None = None
            expected: str | None = None
            differences: tuple[RefusalDifference, ...] = ()
            if isinstance(exc, CampaignFamilyUnsupported):
                path = exc.path
                expected = exc.expected
            elif isinstance(exc, RecordMatchesSecret):
                path = exc.location
            elif isinstance(
                exc,
                (
                    LineageNotFinished,
                    LineageFamilyUnsupported,
                    LineageModeUnsupported,
                    LineageChampionUnverified,
                    BaselineSourceUnavailable,
                ),
            ):
                # §6.3: every lineage refusal is about the manifest's `linked_from`.
                # A changed comparable field is no longer one of them (D2a §3): it
                # is admitted and noted through `condition_changed` above.
                path = "linked_from"
                if isinstance(exc, LineageFamilyUnsupported):
                    expected = _family_refusal_guidance(exc.journaled)
                if isinstance(exc, LineageNotFinished) and exc.instance_digest is not None:
                    # Comparable-links spec §5.4: an unfinished family-5
                    # predecessor is authorizable; the CLI prints the `link`
                    # sentence and the command that quotes this instance.
                    # §6: after the collected hygiene of the new run directory.
                    link = EvolveRefused(
                        str(exc), path, instance_digest=exc.instance_digest, effect="link"
                    )
                    raise EvolveRefused.from_findings((*collected, *link.findings)) from exc
            raise EvolveRefused(str(exc), path, expected=expected, differences=differences) from exc
        except RolloutInfrastructureError as exc:
            # Only a failure before C1's first event escapes: preflight. Anything
            # after it is journaled and returned as `infrastructure_failed`.
            raise EvolveRefused(exc.code) from exc
        except ReflectionCapacityError as exc:
            # The count is public scheduling information, while the raw exception
            # is an internal policy boundary.  Preserve the count in the refusal
            # line so an operator can reduce corpus rows or repeats and retry
            # before C1 has opened the target or created its journal.
            detail = str(exc).removeprefix("reflection_capacity:")
            raise EvolveRefused("reflection_capacity", detail) from exc
        except ValueError as exc:
            if str(exc) == "invalid_family_value":
                raise EvolveRefused("invalid_family_value", "reflector.families.path") from exc
            # C1 refuses a family-6 campaign without an adaptive runtime, and one
            # whose policy is not `/5`, with a bare `ValueError` before its first
            # write. Both are the operator's `adaptive_runtime_required` -- the
            # class table's own name for "this manifest cannot drive this journal".
            # So is a resume that crosses the `/13` boundary: a family-12 journal
            # given a non-`/13` manifest, or a family-11 one given a `/13`
            # manifest (G2 spec §4) -- the family comes from the journal and the
            # policy from the manifest, and the pair is refused before any write.
            # The `/14` boundary is the same (F8): a family-12 journal given a
            # `/14` manifest, or a family-13 journal given a non-`/14` one.
            # The `/15` boundary is the same (family-14 design R14): a V11
            # policy below family 14, a family-14 run without its connection
            # contract or an older one with one.
            if str(exc) in (
                "adaptive_runtime_required",
                "adaptive_runtime_requires_policy_v4",
                "adaptive_runtime_requires_policy_v5",
                "adaptive_runtime_policy_v9_family_mismatch",
                "adaptive_runtime_policy_v10_family_mismatch",
                "adaptive_runtime_policy_v11_family_mismatch",
                "connection_contract_required",
                "connection_contract_unexpected",
            ):
                # One public code for both, with the raised name as the private
                # detail: which of the two conditions failed is a diagnosis for the
                # operator's log, not part of the refusal contract.
                raise EvolveRefused("adaptive_runtime_required", detail=str(exc)) from exc
            raise
    except EvolveRefused as exc:
        # §6: a hard finding ends admission at once; the authorizable findings
        # collected before it are reported after it, each with its class, and
        # no command is printed since accepting them would not admit the run.
        # A refusal that already carries the collected findings -- the
        # identity or link finding, or `FindingsPending` -- leads with an
        # authorizable one and passes through unchanged.
        if pending and not exc.findings[0].instance_digest:
            raise EvolveRefused.from_findings(
                (*exc.findings, *(_as_finding(entry) for entry in pending))
            ) from exc
        raise

    finally:
        _close_scorer_traces(scorer)

    _note_unconsumed(root, supplied, notes)
    return result


def _condition_change_refusal(exc: Exception) -> EvolveRefused:
    """The typed hard refusal of an I2 refusal type raised before any write."""

    if isinstance(exc, AuthorizedIdentityChanged):
        # I2 spec §5.4: hard, with both identity triples as the differences;
        # the detail names the keys that moved (a package upgrade moves only
        # `producing_version`).
        return EvolveRefused(str(exc), differences=exc.differences, detail=",".join(exc.changed))
    if isinstance(exc, ReboundSourceCorrupt):
        # I2 spec §5.3: hard, naming the frozen measurement.
        return EvolveRefused(str(exc), exc.measurement_id)
    if isinstance(exc, PolicyContradictsPrefix):
        # I2 spec §3.4: hard, naming the key, the journaled spend and the new
        # value (as the difference's `journaled` and `observed`).
        return EvolveRefused(
            str(exc), exc.key, differences=(RefusalDifference(exc.key, exc.spent, exc.value),)
        )
    if isinstance(exc, AuthorizationConflict):
        # I2 spec §4.2: hard, naming every conflicting decision id.
        return EvolveRefused(str(exc), ",".join(exc.ids))
    assert isinstance(exc, AuthorizationError)
    # I2 spec §3.3 (ruling R1): a decision whose effect the instance does not
    # admit, `authorization_invalid at effect`.
    return EvolveRefused(exc.code, exc.path)


def _note_unconsumed(
    root: Path, supplied: Sequence[Authorization], notes: Callable[[str], None] | None
) -> None:
    """Spec §5: one `authorization not needed` note per decision the run left unmatched.

    A decision that matched nothing is not an error and not a journal record --
    it is one note per unmatched decision, so a stale file in the directory is
    visible and costs nothing. It is said only after the campaign returned: an
    invocation that was refused has already printed the refusal, which is the
    answer, and a decision it never reached is not stale. The journal is the
    authority on what was consumed, exactly as `evo tree authorize --list`
    reports it -- the decision the invocation matched is the one a consequence
    record quotes, and a hygiene instance the journal already carried needed no
    decision at all, so it is reported here as the surplus it was.
    """

    if notes is None:
        return
    consumed = journaled_authorization_ids(root)
    for authorization in supplied:
        if authorization.id not in consumed:
            notes(f"authorization not needed: {authorization.code} {authorization.digest}")


def _admit_linked_from(manifest: ManifestV2, root: Path) -> Path | None:
    """A new run's predecessor directory, checked and resolved, or `None` (§6.3).

    Resolved against the manifest's own directory, as every relative path in
    the document is. `bad_path at linked_from.run_dir` -- hard,
    before any write, and never naming the path (spec §9) -- when the named
    entry is itself a symlink (judged on the path as written: a symlinked
    *ancestor* is canonicalized, as for the run directory), when it does not
    resolve to a directory, or when it equals, lies inside, or contains the
    canonical run directory -- and when the path cannot be inspected at all
    (an `OSError`, such as an unreadable ancestor). Whether the directory
    holds a finished, linkable, comparable campaign is C1's to refuse
    (`derive_linked_from`), from the path returned here.
    """

    if manifest.linked_from is None:
        return None
    path_field = "linked_from.run_dir"
    given = manifest.path.parent / manifest.linked_from
    try:
        if given.is_symlink():
            raise EvolveRefused("bad_path", path_field)
        resolved = Path(os.path.realpath(given))
        if not resolved.is_dir() or _overlaps(Path(os.path.realpath(root)), resolved):
            raise EvolveRefused("bad_path", path_field)
    except OSError as exc:
        # An unreadable ancestor (`PermissionError`) and every other failure to
        # inspect the path are the same refusal, never a traceback.
        raise EvolveRefused("bad_path", path_field) from exc
    return resolved


def _resume_linked_from(manifest: ManifestV2, root: Path) -> Path | None:
    """The predecessor directory a resume hands C1 for an owed copy, or `None`.

    Baseline inheritance §5.2-§5.3: a resume reads the predecessor only while a
    declared copy is still owed, and after the birth the run is
    self-contained. A path that fails `_admit_linked_from`'s checks is
    therefore not refused here -- that would refuse the resume of a run that
    no longer needs its predecessor -- but passed as `None`, which C1 reports
    as the authorizable `baseline_import_unavailable` only when the copy is
    owed.
    """

    try:
        return _admit_linked_from(manifest, root)
    except EvolveRefused:
        return None


def canonical_run_dir_finding(run_dir: Path) -> HygieneFinding | None:
    """The hygiene finding a symlinked run directory is, or `None` (spec §3).

    Split out of :func:`_canonical_run_dir` so `evolve` can *match* the finding
    against the operator's decisions before deciding whether it is a refusal:
    the fact is the link target, and the digest is the one an authorization for
    this particular link must quote (PDR-0010).
    """

    if run_dir.is_symlink():
        target = os.readlink(run_dir)
        return HygieneFinding(
            RUN_DIR_FINDING,
            "symlinked",
            target,
            hygiene_instance_digest(RUN_DIR_FINDING, "symlinked", target),
        )
    return None


def _canonical_run_dir(
    run_dir: Path, admitted: Collection[str] = ()
) -> tuple[Path, HygieneFinding | None]:
    """The one real path every later step uses for this run (spec §6, amendment A7).

    A run directory that is *itself* a symlink is refused: C1's journal, the
    scorer's traces and the editor's logs would all be laid down somewhere other
    than the directory the operator named, and the link could be re-pointed
    between admission and the first write. A *symlinked ancestor* is refused by
    nothing, because it is ordinary: `/tmp` is a symlink to `/private/tmp` on
    macOS, and a symlinked `$HOME` is common on Linux. Before this, the ancestor
    walk in `admit_output_root` -- which runs for `scorer-traces` and
    `editor-logs` alike -- refused every such invocation with
    `run_dir_unsafe_entry at scorer-traces`, naming an entry that did not exist
    and blaming the operator's `--run-dir` for a symlink somewhere above it.

    Resolving once, here, is what makes the rest coherent: `run_dir_state`,
    `check_path_separation`, the two bindings and C1 then agree on the path, and
    `admit_output_root`'s walk keeps its point: under a canonical root it can
    only fire for a component swapped for a symlink after this call, which is
    exactly the race the point-of-use re-check exists for.

    Nothing here writes, and `realpath` on a path that does not exist yet
    resolves the ancestors that do and appends the rest lexically -- which is
    what a run directory C1 has not created yet needs.

    The symlink is a refusal only while its digest is outside `admitted`; when
    the operator has accepted it the finding comes back beside the resolved
    path, for the caller to journal -- and the path it comes back beside is
    derived from *that one reading of the link*, never from a second
    `realpath(given)`. PDR-0010 binds an authorization to one instance: the
    instance digest quotes the target this call read, so the campaign has to
    write under that same target. Resolving the link a second time would let a
    link re-pointed from A to B in between be accepted as A and written as B
    (codex round 1). `os.path.join` returns an absolute target unchanged, so
    both kinds of link are anchored the same way.

    `evolve` itself calls the two halves below in the other order -- it resolves
    first and judges after reading the journal, which lives under the resolved
    root -- so this function is the composed form for every other caller and for
    the tests that pin the anchoring.
    """

    root, finding = _resolve_run_dir(Path(run_dir))
    _judge_run_dir(finding, admitted)
    return root, finding


def _resolve_run_dir(given: Path) -> tuple[Path, HygieneFinding | None]:
    """The root, and the link finding, from one reading of the run directory.

    Nothing is judged here: the caller decides whether the finding is a refusal,
    and it needs this root first because the standing authority it judges
    against is the journal *under* the root (codex round 2).
    """

    finding = canonical_run_dir_finding(given)
    if finding is not None:
        parent = os.path.dirname(os.path.abspath(given))
        return Path(os.path.realpath(os.path.join(parent, finding.fact))), finding
    return Path(os.path.realpath(given)), finding


def _refuse_hygiene_hard(
    root: Path,
    finding: HygieneFinding | None,
    admitted: Collection[str],
    roots: Sequence[str] = OUTPUT_ROOTS,
) -> None:
    """Every unadmitted run-directory entry of a finished campaign, as a hard refusal.

    The same two walks `evolve` makes below, with one difference: the refusal
    they raise is re-raised without its instance digest. PDR-0010 computes a
    digest only for a transition a decision could carry, and a finished campaign
    can carry none -- the lifecycle refuses every record after
    `campaign_finished` -- so publishing one would invite the operator to write
    a decision that the next invocation could not apply.

    `admitted` is the journal's own set at this point, never the invocation's:
    a decision supplied for a finished campaign is not admitted, it is refused
    with everything else.
    """

    try:
        _judge_run_dir(finding, admitted)
        for name in roots:
            admit_output_root(root / name, admitted=admitted)
    except EvolveRefused as exc:
        raise EvolveRefused(exc.code, exc.path, detail=exc.detail) from exc


def _journal_finished(run_dir: Path) -> bool:
    """Has this campaign written its terminal? (PDR-0010, amended 2026-09-17)

    Best effort over `_journal_records`, like `journaled_hygiene_digests` beside
    it: an absent, unreadable or over-long journal answers "not finished", which
    is what a new run directory answers too. The cost of that silence is bounded
    by C1: `adopt` reduces the same journal properly a moment later and refuses
    a finished campaign's pending decisions there, before any write. What this
    reader buys is the *presentation* -- the class and the sentence the operator
    is shown -- for which reducing the whole log during admission would be a
    second authority on a journal C1 has not opened yet.

    `campaign_finished` is the last record a lifecycle admits, so finding the
    event anywhere in the file is finding the terminal.
    """

    return any(raw.get("event") == "campaign_finished" for _, raw in _journal_records(run_dir))


def _as_finding(finding: HygieneFinding) -> RefusalFinding:
    """A collected hygiene finding as the refusal reports it (§6): name, guard, digest."""

    return RefusalFinding(
        "run_dir_unsafe_entry", finding.name, finding.guard, (), finding.instance_digest, "hygiene"
    )


def _judge_run_dir(finding: HygieneFinding | None, admitted: Collection[str]) -> None:
    """The symlinked run directory is a refusal only while its digest is unadmitted."""

    if finding is not None and finding.instance_digest not in admitted:
        raise EvolveRefused(
            "run_dir_unsafe_entry",
            finding.name,
            detail=finding.guard,
            instance_digest=finding.instance_digest,
            effect="hygiene",
        )


def admit_manifest(manifest_path: Path | ManifestV2) -> ManifestV2:
    """Step 1: a `/3` document, or a refusal carrying the loader's own code.

    An already-loaded document passes through the same `/3` check and is
    returned as it is, so the CLI can admit once and hand the same document to
    `redactions_for`, `short_forwarded_names` and `evolve`.
    """

    return _manifest(manifest_path)


def _loaded_manifest(manifest_path: Path | ManifestV2) -> ManifestV2:
    if isinstance(manifest_path, ManifestV2):
        return manifest_path
    try:
        return load_manifest_v2(Path(manifest_path))
    except ManifestError as exc:
        raise EvolveRefused(exc.code, exc.detail, expected=exc.expected) from exc


def _manifest(manifest_path: Path | ManifestV2) -> ManifestV2:
    manifest = _loaded_manifest(manifest_path)
    if manifest.schema not in CAMPAIGN_SCHEMAS or manifest.workflow is None:
        # The loader already checks the schema; keep the same refusal for
        # hand-built objects and workflow-less inputs (PDR-0038, ADR-0046).
        raise EvolveRefused("manifest_schema", expected=_MANIFEST_SCHEMA_EXPECTED)
    return manifest


def _adaptive_config(
    workflow: WorkflowSections,
    *,
    corpus: Sequence[Any],
    reflection_metadata: Mapping[str, Mapping[str, object]],
    reflection_metadata_digest: str,
    environment_docs_digest: str | None,
    seed_root: Path,
    run_dir: Path,
    environ: Mapping[str, str],
    admitted_hygiene: frozenset[str] = frozenset(),
    final_adaptation: Mapping[str, Any],
    schema: str,
) -> AdaptiveCampaignRuntimeConfig:
    """Assemble the sealed adaptive runtime inputs for families 14 and 15.

    `admitted_hygiene` is the operator's accepted hygiene findings (PDR-0010).
    It rides on the config because the config is what `GroupingRuntime` is
    constructed from, and the command grouping is the one writer reached that
    way which re-admits `reflector-logs` at the point of use.

    The top-level `grouper` builds profile `/7` in one step with the partition
    and labels `evolve` passed in; its grouping slot is the Grouper identity.
    `schema` is the manifest's, which sets the absent Reflector group text limit.
    An invalid family-membership value is refused before the target is opened.
    """

    grouping = workflow.grouper
    selection = workflow.reflector.selection
    families = workflow.reflector.families
    assert grouping is not None and selection is not None and families is not None
    model_grouping: ModelGroupingSection | None = None
    agent_grouping: AgentCliGrouperSection | None = None
    agent_program = None
    agent_prompt = None
    agent_child_environment = None
    command = None
    timeout = 30.0
    config: Mapping[str, object] = {}
    features: tuple[FieldFeature, ...] = ()
    if isinstance(grouping, FieldsGroupingSection):
        grouping_identity = _fields_grouping_identity(grouping)
        features = grouping.features
    elif isinstance(grouping, ModelGroupingSection):
        grouping_identity = _chat_grouper_identity(grouping, family15=schema == SCHEMA_V16)
        model_grouping = grouping
    elif isinstance(grouping, AgentCliGrouperSection):
        grouping_identity = _agent_cli_grouper_identity(grouping, environ)
        agent_grouping = grouping
        agent_program = grouper_agent_seal(grouping, environ)
        agent_prompt = _sealed_prompt(
            grouping.role.system_prompt,
            TREE_AGENT_GROUPER_SYSTEM,
            code="grouper_invalid",
            field_path="grouper.system_prompt",
        )
        agent_child_environment = bound_child_environment(grouping.role, environ)
    else:
        assert isinstance(grouping, CommandGroupingSection)
        command = build_grouper_command(
            grouping.command, seed_root=seed_root, run_dir=run_dir, environ=environ
        )
        assert grouping.command.timeout_seconds is not None
        timeout = grouping.command.timeout_seconds
        config = grouping.config
        grouping_identity = {
            "kind": "command",
            "command_digest": command.identity_digest(timeout_seconds=timeout),
            "config_digest": hashlib.sha256(canonical_json(config)).hexdigest(),
        }
    if grouping.max_consecutive_full_fallbacks is not None:
        grouping_identity["max_consecutive_full_fallbacks"] = (
            grouping.max_consecutive_full_fallbacks
        )
    selection_identity = {
        "focus_cases": selection.focus_cases,
        "representatives_per_case": selection.representatives_per_case,
        "context_page_size": selection.context_page_size,
    }
    runtime_selection = SelectionRuntimeConfig(**selection_identity)
    if selection.min_claim_cases is not None:
        selection_identity["min_claim_cases"] = selection.min_claim_cases
    family_identity: dict[str, object] = {"kind": families.kind}
    if families.path is not None:
        family_identity["path"] = families.path
    feedback = workflow.campaign.attempt_feedback
    assert feedback is not None
    try:
        membership = family_membership_for(family_identity, reflection_metadata, corpus)
    except ValueError as exc:
        if str(exc) != "invalid_family_value":
            raise
        raise EvolveRefused("invalid_family_value", "reflector.families.path") from exc
    assert workflow.reflector.max_consecutive_failures is not None
    reflection: dict[str, int | None] = {
        "max_consecutive_failures": workflow.reflector.max_consecutive_failures
    }
    # #282, ADR-0047: `/16` always binds its effective Reflector group text
    # limit, since its absent value (4,096) is not the 512 a journal without the
    # key means; `/15` binds it only when declared, so its bytes stay as they were.
    if schema == SCHEMA_V16 or workflow.reflector.group_text_chars_declared:
        reflection["group_text_chars"] = effective_group_text_chars(workflow.reflector, schema)
    if workflow.reflector.max_consecutive_skips is not None:
        reflection["max_consecutive_skips"] = workflow.reflector.max_consecutive_skips
    identity = build_adaptation_identity(
        grouping=grouping_identity,
        selection=selection_identity,
        families=family_identity,
        attempt_feedback=feedback,
        reflection_metadata_digest=reflection_metadata_digest,
        environment_docs_digest=environment_docs_digest,
        family_membership=membership,
        profile=FINAL_ADAPTATION_PROFILE,
        reflection=reflection,
        corpus_partition=final_adaptation["corpus_partition"],
        provenance_labels=final_adaptation.get("provenance_labels"),
    )
    if schema == SCHEMA_V16:
        payload = identity.to_payload()
        payload["evidence"] = (workflow.evidence or DEFAULT_EVIDENCE).to_payload()
        if workflow.checks is not None:
            payload["checks"] = workflow.checks.to_payload()
        identity = AdaptationIdentityV1(payload)
    return AdaptiveCampaignRuntimeConfig(
        identity=identity,
        grouping_features=features,
        custom_command=command,
        custom_timeout_seconds=timeout,
        custom_config=config,
        selection=runtime_selection,
        families=family_identity,
        reflection_metadata=reflection_metadata,
        model_grouping=model_grouping,
        agent_grouping=agent_grouping,
        agent_program=agent_program,
        agent_prompt=agent_prompt,
        agent_child_environment=agent_child_environment,
        admitted_hygiene=admitted_hygiene,
        # Read from the same snapshot `_run_secrets` redacts with, so the key
        # the grouping model is sent is a member of the run's secret set.
        model_credentials=grouper_model_credentials(grouping, environ),
    )


def _fields_grouping_identity(section: FieldsGroupingSection) -> dict[str, Any]:
    """A `fields` grouping's identity payload: its ordered features, verbatim."""

    return {
        "kind": "fields",
        "features": [
            {
                "name": feature.name,
                "source": feature.source,
                "path": feature.path,
                "description": feature.description,
            }
            for feature in section.features
        ],
    }


def _grouping_prompts(workflow: WorkflowSections) -> tuple[Path, ...]:
    """The declared `chat` or `agent_cli` Grouper prompt files, which the corpus
    must not lie inside."""

    grouping = workflow.grouper
    if isinstance(grouping, ModelGroupingSection) and grouping.system_prompt is not None:
        return (grouping.system_prompt,)
    if isinstance(grouping, AgentCliGrouperSection) and grouping.role.system_prompt is not None:
        return (grouping.role.system_prompt,)
    return ()


def _refuse_verbatim_answers(
    declared: VerbatimAnswers,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    *,
    modes: CheckModes = ENFORCE_ALL,
    hits: HitSink | None = None,
) -> None:
    """ADR-0038 bind checks, before anything is written: an entry holding a run
    secret is `record_matches_secret`; an entry equal to no oracle string value
    the campaign scans (`oracle_needles` of the train cases) is
    `verbatim_answer_unmatched`. Both name the entry's path, never its value."""

    mode = modes.mode("verbatim_answer_unmatched")
    values = frozenset(oracle_needles(corpus)) if mode != "off" else frozenset()
    for index, entry in enumerate(declared.strings):
        path = f"verbatim_answers.strings[{index}]"
        if any(secret and secret in entry for secret in secrets):
            raise EvolveRefused("record_matches_secret", path)
        if mode != "off" and entry not in values:
            if mode == "enforce":
                raise EvolveRefused("verbatim_answer_unmatched", path)
            if hits is not None:
                hits.hit("verbatim_answer_unmatched", "verbatim_answer_unmatched")


def _refuse_secret_oracle_paths(
    declared: SecretOraclePaths,
    corpus: Sequence[EvalCase],
    secrets: Sequence[str],
    *,
    modes: CheckModes = ENFORCE_ALL,
    hits: HitSink | None = None,
) -> None:
    """Screen all declared pointers for secrets before checking train selections."""

    for index, pointer in enumerate(declared.pointers):
        if any(secret and secret in pointer for secret in secrets):
            raise EvolveRefused("record_matches_secret", f"secret_oracle_paths.pointers[{index}]")
    mode = modes.mode("secret_oracle_path_unmatched")
    if mode == "off":
        return
    for index, pointer in enumerate(declared.pointers):
        if not any(
            len(value) >= MIN_NEEDLE_LENGTH
            for case in corpus
            for _, value in oracle_strings(case.oracle, secret_paths=(pointer,))
        ):
            if mode == "enforce":
                raise EvolveRefused(
                    "secret_oracle_path_unmatched", f"secret_oracle_paths.pointers[{index}]"
                )
            if hits is not None:
                hits.hit("secret_oracle_path_unmatched", "secret_oracle_path_unmatched")


def _provenance_labels(
    workflow: WorkflowSections, reflection_corpus: Any
) -> list[list[Any]] | None:
    """Every train case's label under `gate.provenance.kind: metadata`, else
    `None` (G2 spec §4.1). A malformed label is `bad_value at
    campaign.gate.provenance.path`, before any write."""

    from evolution.tree.affected_cases import LabelError, provenance_labels_for

    gate = workflow.campaign.gate_provenance
    assert gate is not None
    if gate["kind"] != "metadata":
        return None
    try:
        return provenance_labels_for(
            gate,
            reflection_corpus.metadata,
            [case.case_id for case in reflection_corpus.cases],
        )
    except LabelError as exc:
        raise EvolveRefused(
            "bad_value", "campaign.gate.provenance.path", detail=exc.case_id
        ) from exc


def _model_grouping_identity(
    section: ModelGroupingSection,
    *,
    code: str = "reflector_invalid",
    field_path: str = "reflector.grouping.system_prompt",
    family15: bool = False,
) -> dict[str, Any]:
    """A `model` grouping's identity payload (adaptation identity `/3`, ADR-0022).

    Binds the backend, model, endpoint, credential and proxy environment
    *names*, budgets, the `role_call_retry/1` policy, the prompt's digest and
    the declared fallback policy (model error grouping spec §12). The prompt is
    the declared `system_prompt` file's bytes, or the packaged default sealed
    the way a chat role's is (`_sealed_prompt`): a declared file that vanished
    or became unreadable after load is `reflector_invalid` at
    `reflector.grouping.system_prompt`, never a traceback naming its path.

    An undeclared `api_key_env` (#290, ADR-0034 2026-10-21) is left out, not
    written as `null`: a Grouper that declares its key keeps its identity.

    `family15` (a `/16` chat Grouper, b16 spec §11, ADR-0055) records
    `model_grouping/2`, seals the packaged prompt -- `_v3` from 2.0.0b17
    (ADR-0057), `_v2` before it -- and adds each declared portion key. Without
    it the payload keeps its `/15` bytes.
    """

    payload: dict[str, Any] = {
        "kind": "model",
        "protocol": "model_grouping/2" if family15 else "model_grouping/1",
        "backend": section.backend,
        "model": section.model,
        "base_url": section.base_url,
        "api_key_env": section.api_key_env,
        "proxy_env": section.proxy_env,
        "verify_ssl_certs": section.verify_ssl_certs,
        "reasoning_effort": section.reasoning_effort,
        "provider_only": None if section.provider_only is None else list(section.provider_only),
        "timeout_seconds": section.timeout_seconds,
        "concurrency": section.concurrency,
        "max_input_chars": section.max_input_chars,
        "max_output_tokens": section.max_output_tokens,
        "representatives_per_case": section.representatives_per_case,
        "retry": section.retry.to_payload(),
        "prompt_sha256": _sealed_prompt(
            section.system_prompt,
            TREE_GROUPING_MODEL_V3_SYSTEM if family15 else TREE_GROUPING_MODEL_SYSTEM,
            code=code,
            field_path=field_path,
        ).sha256,
        "fallback": (
            None if section.fallback is None else _fields_grouping_identity(section.fallback)
        ),
    }
    if section.api_key_env is None:
        del payload["api_key_env"]
    if section.max_repairs is not None:
        payload["max_repairs"] = section.max_repairs
    if section.provider_order is not None:
        payload["provider_order"] = list(section.provider_order)
    if section.allow_fallbacks is not None:
        payload["allow_fallbacks"] = section.allow_fallbacks
    if section.require_parameters is not None:
        payload["require_parameters"] = section.require_parameters
    for key in GROUPING_V2_MINIMUMS:
        if getattr(section, key) is not None:
            payload[key] = getattr(section, key)
    return payload


def _chat_grouper_identity(
    section: ModelGroupingSection, *, family15: bool = False
) -> dict[str, Any]:
    """A `/15` `chat` Grouper's identity (A§3.4, R18): the grouping model's
    payload under the kind `chat`, its prompt refused as the Grouper's.
    `family15` is a `/16` Grouper's `model_grouping/2` payload."""

    payload = _model_grouping_identity(
        section, code="grouper_invalid", field_path="grouper.system_prompt", family15=family15
    )
    payload["kind"] = "chat"
    return payload


def _agent_cli_grouper_identity(
    section: AgentCliGrouperSection, environ: Mapping[str, str]
) -> dict[str, Any]:
    """A `/15` `agent_cli` Grouper's identity (A§3.4, R18): its keys, the
    operation deadline, the prompt's content digest (the declared file, or the
    packaged `TREE_AGENT_GROUPER_SYSTEM`), the program seal and the fallback.
    Environment names only, sorted; no value."""

    role = section.role
    prompt = _sealed_prompt(
        role.system_prompt,
        TREE_AGENT_GROUPER_SYSTEM,
        code="grouper_invalid",
        field_path="grouper.system_prompt",
    )
    return {
        "kind": "agent_cli",
        "protocol": "agent_grouping_request/1",
        "model": role.model,
        "permission": role.permission,
        "allowed_env": sorted(role.allowed_env),
        "reasoning_effort": role.reasoning_effort,
        "max_turns": role.max_turns,
        "max_payload_bytes": role.budgets.max_payload_bytes,
        "timeout_seconds": section.timeout_seconds,
        "prompt_sha256": prompt.sha256,
        "program_sha256": grouper_agent_seal(section, environ).sha256,
        "fallback": (
            None if section.fallback is None else _fields_grouping_identity(section.fallback)
        ),
    }


def _environment_docs(
    workflow: WorkflowSections, run_dir: Path, state: Literal["new", "resume"]
) -> tuple[_V2DocumentSnapshot | None, bool]:
    """Bind the byte-addressed adaptive-family documentation snapshot, and read nothing else.

    New campaigns validate the live source without writing it; C1 publishes the
    sealed snapshot after the creation record.  A resumed campaign proves the
    live directory still has the digest in the *latest* journaled identity and
    then *reads* the stored snapshot.

    Nothing here writes (ADR-0015, codex round 1). A stored copy that is missing
    or no longer verifies while the live source still has the journaled digest
    comes back as the second element of the pair -- `environment_docs_repair` --
    and the campaign repairs it after its own consequence records, so an
    invocation refused for an unrelated identity difference leaves the run
    directory exactly as it found it.

    A live source that no longer has that digest is no longer refused here
    (PDR-0010). The **live** snapshot comes back instead, so `build_identity`
    yields the new `environment_docs_digest`, `adopt` reports it by name as
    `identity.reflector.environment_docs_digest`, and the operator can accept
    it like any other authorizable difference -- at which point the campaign
    re-takes the run directory's own copy from this very snapshot, which is why
    that path needs no repair flag of its own. Refusing here would have meant a
    refusal with no diff and no digest to quote, and a branch in C1 that could
    never fire. Every other way the source or the stored copy can fail stays
    `environment_docs_invalid`: those are not differences an operator can
    accept, they are directories to repair.
    """

    declared = workflow.reflector.environment_docs
    if declared is None:
        return None, False
    try:
        if state == "new":
            return _v2_create_document_snapshot(declared), False
        expected = _journaled_docs_digest(run_dir)
        if expected is None:
            raise EnvironmentDocsError("snapshot_invalid", "missing identity digest")
        try:
            return _v2_check_document_snapshot(declared, run_dir, expected_digest=expected)
        except EnvironmentDocsError as exc:
            if exc.code != "identity_changed":
                raise
            # `_v2_check_document_snapshot` renders the live source before it
            # compares, so this second rendering is the one it already built
            # and can only fail on a source rewritten in between -- in which
            # case the refusal below is the right one anyway. The re-take the
            # accepted rebase performs replaces the stored copy whatever state
            # it was in, so this path never asks for a repair.
            return _v2_create_document_snapshot(declared), False
    except EnvironmentDocsError as exc:
        raise EvolveRefused(
            "environment_docs_invalid", "reflector.environment_docs", detail=exc.detail
        ) from exc


def _journaled_docs_digest(run_dir: Path) -> str | None:
    """The `environment_docs_digest` of the campaign's *latest* identity.

    The creation record, or the last `identity_rebased` record if the operator
    has accepted a move since (PDR-0010): comparing a live source against the
    digest the run *started* with would refuse every resume after an authorized
    documentation change, and would re-report a difference the journal already
    settled. This stays silent about every way the journal can fail to answer;
    C1 remains the authority that classifies a malformed journal a moment later.
    """

    payload: object = None
    for index, raw in _journal_records(run_dir):
        if (index == 0 and payload is None) or raw.get("event") == "identity_rebased":
            payload = raw.get("payload")
    identity = payload.get("identity") if isinstance(payload, Mapping) else None
    reflector = identity.get("reflector") if isinstance(identity, Mapping) else None
    if not isinstance(reflector, Mapping):
        return None
    digest = reflector.get("environment_docs_digest")
    return digest if isinstance(digest, str) else None


def journaled_hygiene_digests(run_dir: Path) -> frozenset[str]:
    """Every `authorization_applied` instance digest in `campaign.jsonl` (§5 source 1).

    The journal is standing authority for the hygiene instances it already
    carries: an entry the operator accepted once is accepted by every later
    invocation of *that* campaign, and only for that exact instance -- the
    digest binds the entry's name, its guard class and the observed fact, so a
    permission bit accepted last week does not cover a symlink that appeared
    today.

    Best effort, like `_journaled_docs_digest`: see `_journal_records` for what
    the two readers stay silent about and why. The cost of that silence is a
    decision the operator has to supply again, never an entry admitted that
    should not have been.
    """

    digests: set[str] = set()
    for _, raw in _journal_records(run_dir):
        if raw.get("event") != "authorization_applied":
            continue
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            continue
        # Comparable-links spec §5.2: a `close`/`terminal` record's instance
        # digest is an identity instance, not a run-directory entry's; only a
        # hygiene record -- which carries no `effect` key -- admits an entry.
        if payload.get("effect", "hygiene") != "hygiene":
            continue
        if isinstance(payload.get("instance_digest"), str):
            digests.add(payload["instance_digest"])
    return frozenset(digests)


def journaled_authorization_ids(run_dir: Path) -> frozenset[str]:
    """Every decision id a consequence record of this campaign carries (§6).

    The two records a decision produces -- `identity_rebased` for an accepted
    identity move and `authorization_applied` for an accepted run-directory
    entry -- both quote the decision in their `authorization` slot. Their ids
    are what `evo tree authorize --list` calls `consumed`: the journal, not the
    decision file, is what says a run acted on it.

    Best effort over `_journal_records`, like the two readers above; here the
    cost of silence is a decision reported `pending` that a run has in fact
    consumed, which is a presentation fault and never an admission.
    """

    ids: set[str] = set()
    for line, raw in _journal_records(run_dir):
        if line == 0 and raw.get("event") == "campaign_created":
            # Comparable-links spec §5.4: a link from an unfinished family-5
            # predecessor carries its full decision in the creation record's
            # `identity.linked_from.authorization` -- the one decision a run
            # consumes without a consequence record of its own.
            payload = raw.get("payload")
            identity = payload.get("identity") if isinstance(payload, Mapping) else None
            linked = identity.get("linked_from") if isinstance(identity, Mapping) else None
            decision = linked.get("authorization") if isinstance(linked, Mapping) else None
            if isinstance(decision, Mapping) and isinstance(decision.get("id"), str):
                ids.add(decision["id"])
            continue
        if raw.get("event") not in ("identity_rebased", "authorization_applied"):
            continue
        payload = raw.get("payload")
        authorization = payload.get("authorization") if isinstance(payload, Mapping) else None
        if isinstance(authorization, Mapping) and isinstance(authorization.get("id"), str):
            ids.add(authorization["id"])
    return frozenset(ids)


def _journal_records(run_dir: Path) -> Iterator[tuple[int, Mapping[str, Any]]]:
    """Every legible record of `campaign.jsonl`, with its line number, bounded.

    The one reader behind `_journaled_docs_digest` and
    `journaled_hygiene_digests`. Both run during admission, on a journal C1 has
    not opened yet and whose contents nothing has validated, so both must stay
    inside the bounds C1's own log enforces rather than trusting the file:

    * a file past `MAX_JOURNAL_BYTES` yields nothing at all. C1 is about to
      refuse it as a persistence outcome, and one `st_size` is cheaper than
      reading a quarter of a gigabyte to reach the same silence.
    * every read is bounded by `MAX_RECORD_BYTES + 1`, and a line that hits the
      bound is discarded to the next newline in bounded steps rather than
      materialized. Iterating the handle line by line would have allocated the
      whole of an oversized line before the length check could reject it, so a
      torn journal with one long line -- which is precisely the shape a crash
      leaves -- would cost the admission that line twice over.

    The line number is the *file's*, counting lines this reader skipped, so
    `index == 0` still means "the creation record" whatever precedes it.

    Best effort throughout: an unreadable file, a line past the bound and a line
    that is not a JSON object all contribute nothing rather than refusing. C1
    opens the same journal a moment later and is the authority on whether it is
    well formed; nothing here may pre-empt that with a worse diagnosis.
    """

    path = run_dir / CAMPAIGN_FILE
    try:
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            return
        with path.open("rb") as handle:
            index = 0
            while True:
                line = handle.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    return
                number, index = index, index + 1
                if len(line) == MAX_RECORD_BYTES + 1 and not line.endswith(b"\n"):
                    # The bound was hit mid-record. A *legal* record of exactly
                    # `MAX_RECORD_BYTES` content bytes also comes back one byte
                    # over the bound -- its terminator -- so the terminator, not
                    # the length alone, is what tells a whole record from a
                    # truncated one. Step to the next newline without ever
                    # holding more than the bound in memory.
                    while True:
                        chunk = handle.readline(MAX_RECORD_BYTES + 1)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if isinstance(raw, Mapping):
                    yield number, raw
    except OSError:
        return


def _family_refusal_guidance(journaled: int | None) -> str | None:
    """ADR-0046's one migration sentence for a removed or unversioned family."""

    if journaled is None or journaled < FINAL_CAMPAIGN_FAMILY_VERSION:
        return REMOVED_FAMILY_GUIDANCE
    return None


def _refuse_unadmitted_family_at(run_dir: Path) -> None:
    """Refuse a legible journaled family this release does not resume, before the adapter exists.

    The admitted set is `campaign._ADMITTED_CAMPAIGN_FAMILIES` (family 14 from
    `2.0.0b2`); a family above it, an older one or the unversioned previous
    family is refused here rather than after the adapter factory has opened a
    target session. Readers are not consulted: an illegible journal is left to
    `read_run_id` and C1, which classify it.
    """

    from evolution.tree import campaign as campaign_module  # one source for the set

    try:
        journaled = read_family_version(run_dir)
    except (ValueError, OSError, JournalFault):
        return
    if journaled in campaign_module._ADMITTED_CAMPAIGN_FAMILIES:
        return
    exc = CampaignFamilyUnsupported(journaled)
    raise EvolveRefused(str(exc), exc.path, expected=exc.expected) from exc


def _journaled_run_id(run_dir: Path) -> str:
    """Step 5 on a resume: the id `campaign_created` recorded, never a derived one.

    A journal that exists but cannot be read is not a refusal. `run_dir_state`
    chose resume because `campaign.jsonl` is there, and a creation record torn by
    a crash between the file's `open` and the completed append is a realistic
    window; so is a file this process cannot read at all -- its permissions
    removed, a journal past the reader's bound -- which `read_run_id` reports as
    the same `CampaignUnreadable`. Reporting `campaign_absent` for either would
    tell the operator the campaign is gone and would step around C1's own
    recovery of the journal it is holding. So the adapter is built on the derived id instead — no id is
    journaled to build it on, and `adopt` will never compare it, because C1's
    scan faults before that — and `resume_campaign` classifies the run root as
    the persistence outcome it is (`persistence_failed`, exit 1).
    """

    # Probed once before the owed terminal already; again here because another
    # process may have written the journal between that probe and the run
    # directory's classification.
    _refuse_unadmitted_family_at(run_dir)
    try:
        return read_run_id(run_dir)
    except CampaignUnreadable:
        return derive_run_id(run_dir)
    except CampaignFamilyUnsupported as exc:
        # Defensive since `2.0.0b2`: `_refuse_unadmitted_family_at` above
        # already refuses every legible unadmitted family.
        # A legible integer outside every supported reader family is an
        # invocation refusal, not a malformed journal. This happens before the
        # adapter is constructed and before C1 can open or repair the log.
        if str(exc) not in _PRE_WRITE_CODES:
            raise
        raise EvolveRefused(str(exc), exc.path, expected=exc.expected) from exc
    except CampaignMissing as exc:
        # Only a `campaign.jsonl` that vanished between `run_dir_state` and here.
        if str(exc) not in _PRE_WRITE_CODES:
            raise
        raise EvolveRefused(str(exc)) from exc


def redactions_for(
    manifest_path: Path | ManifestV2, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The secret values a caller must keep out of anything it prints (spec §9).

    The CLI prints a `CampaignResult` through the S1 redactor and needs the same
    secret set C1 journals with. It is derived here rather than returned by
    `evolve`, because `CampaignResult` is C1's type and has no room for it, and
    reading the manifest is this module's job in the first place.

    Call it *before* `evolve`, on the document `evolve` is about to read: the
    redaction set is then fixed by the manifest as admitted, not by whatever is
    on disk once a campaign that may have run for hours returns. A manifest this
    cannot load has no `allowed_env` to read and `evolve` is about to refuse it;
    a refusal prints no document, so the empty tuple never redacts anything.

    It is the same `_secrets` the campaign is armed with, so the printed
    document and the journal apply one rule, the short-value floor included.

    `environ` defaults to a snapshot of the process environment taken here. A
    caller that already holds one -- `evolve`, which snapshots once for the
    whole run -- passes it, so one mapping decides what is spawned and what is
    redacted.
    """

    try:
        manifest = _manifest(manifest_path)
        workflow = manifest.workflow
        assert workflow is not None  # `_manifest` refuses a document without one
        return _run_secrets(workflow, environ)
    except (EvolveRefused, BindingError):
        # An editor whose declared environment is incomplete is `evolve`'s
        # refusal in waiting: it is about to refuse, and a refusal prints no
        # document, so there is nothing to redact.
        return ()


def short_forwarded_names(
    manifest_path: Path | ManifestV2, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The `allowed_env` names whose present value the secret floor skips.

    The floor (amendment A6) is a length rule, and length does not distinguish a
    flag from a credential: a six-digit PIN and `DEBUG=1` are both under it, and
    the first is a secret this run will not redact. Keeping the floor is the
    lesser evil -- without it, forwarding `DEBUG=1` makes the journal refuse its
    own first record after the run root has been created -- but the skip must
    not be silent, so the caller can say which names it applies to. Names only:
    the value is exactly what must not be printed, which is the whole point.

    Read `before` `evolve`, like `redactions_for`, and on the same document; a
    manifest that cannot be loaded has no `allowed_env` and is about to be
    refused, so the empty tuple names nothing. `environ` is the same optional
    snapshot `redactions_for` takes.
    """

    values = dict(os.environ) if environ is None else environ
    try:
        manifest = _manifest(manifest_path)
        workflow = manifest.workflow
        assert workflow is not None  # `_manifest` refuses a document without one
        names = (
            workflow.scorer.allowed_env
            | editor_environment_names(workflow.editor)
            | reflector_environment_names(workflow.reflector)
        )
        if workflow.grouper is not None:
            names = names | grouper_environment_names(workflow.grouper)
    except (EvolveRefused, BindingError):
        return ()
    return tuple(
        name for name in sorted(names) if name in values and len(values[name]) < _MIN_SECRET_LENGTH
    )


def unauthenticated_roles(workflow: WorkflowSections) -> tuple[tuple[str, str], ...]:
    """The `chat` roles that declare no `api_key_env`, with their `base_url` (#290).

    `(role, base_url)` in role order Editor, Reflector, Grouper: what the CLI's
    pre-run note names (design §4.1), one line per role on every invocation,
    a resume included. A pure function of the document: names and declared
    URLs only, never an environment value.
    """

    sections: tuple[tuple[str, object], ...] = (
        ("editor", workflow.editor),
        ("reflector", workflow.reflector.role),
        ("grouper", workflow.grouper),
    )
    return tuple(
        (role, section.base_url)
        for role, section in sections
        if isinstance(section, ChatEditorSection | ModelGroupingSection)
        and section.api_key_env is None
    )


def unencrypted_model_roles(
    workflow: WorkflowSections,
) -> tuple[tuple[str, str, bool], ...]:
    """The `chat` roles on plain `http` to a host that is not loopback (#295).

    `(role, base_url, credential declared)` in role order Editor, Reflector,
    Grouper: what the CLI's pre-run `WARNING:` lines name, one per role on
    every invocation, a resume included. `credential` is whether the role
    declares `api_key_env`, so the warning says whether a key or only the
    traffic travels unencrypted. Loopback is `manifest_v2._LOOPBACK_HOSTS`,
    the set the loader has always used for it. A pure function of the
    document: names and declared URLs only, never an environment value.
    """

    sections: tuple[tuple[str, object], ...] = (
        ("editor", workflow.editor),
        ("reflector", workflow.reflector.role),
        ("grouper", workflow.grouper),
    )
    roles: list[tuple[str, str, bool]] = []
    for role, section in sections:
        if not isinstance(section, ChatEditorSection | ModelGroupingSection):
            continue
        parts = urlsplit(section.base_url)
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
            roles.append((role, section.base_url, section.api_key_env is not None))
    return tuple(roles)


def _corpus_path(line: int) -> str:
    """The manifest path a corpus refusal indicts, with the offending line."""

    return "corpus" if line == 0 else f"corpus[{line}]"


def _run_secrets(
    workflow: WorkflowSections, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The one secret set this run is armed with: every role's, one floor.

    The scorer contributes its forwarded `allowed_env` values, as it always
    has. The editor contributes what its kind gives
    (`bindings.editor_environment_values`): a `subprocess` editor's forwarded
    values unchanged, a `chat` editor's key and proxy, an `agent_cli` editor's
    whole bound child environment outside the runtime names -- the curated
    credential the backend forwards to the CLI included, declared or not,
    because the CLI can echo a value it was given whether or not the manifest
    named it (E1-editor design §9). The Reflector contributes the same three
    shapes through `reflector_environment_values`: it is another role reading
    operator credentials, and one redaction set covers every journal. The
    Grouper contributes its own (`bindings.grouper_environment_values`):
    a `chat` Grouper's key and proxy, a `command` Grouper's forwarded values and
    an `agent_cli` Grouper's bound child environment (R19).

    `_MIN_SECRET_LENGTH` applies to every member whatever produced it: a value
    too short to be a credential is a flag, and a flag as a redaction needle
    would refuse the campaign's own first journal record. Duplicates are folded
    -- one name may be forwarded to both programs -- so the tuple is a set of
    needles in a stable order, not a census of who declared what.

    Raises the editor's own `EvolveRefused`/`BindingError` when a declared
    environment name is missing: the read happens once, and this is where it
    happens first.

    `environ` is the mapping to read. `evolve` passes the single snapshot it
    took before any other derivation, so the secret set, the sealed program and
    the bound child environment are one set of bytes rather than three
    independent reads of a mutable `os.environ`; `None` snapshots here instead,
    for the callers that hold no run.
    """

    snapshot = dict(os.environ) if environ is None else environ
    values = (
        _secrets(workflow.scorer.allowed_env, environ=snapshot)
        + tuple(
            value
            for value in editor_environment_values(workflow.editor, snapshot)
            if len(value) >= _MIN_SECRET_LENGTH
        )
        + tuple(
            value
            for value in reflector_environment_values(workflow.reflector, snapshot)
            if len(value) >= _MIN_SECRET_LENGTH
        )
        + tuple(
            value
            for value in (
                grouper_environment_values(workflow.grouper, snapshot)
                if workflow.grouper is not None
                else ()
            )
            if len(value) >= _MIN_SECRET_LENGTH
        )
    )
    return tuple(dict.fromkeys(values))


def _run_secret_provenance(
    workflow: WorkflowSections, environ: Mapping[str, str] | None = None
) -> tuple[tuple[str, str | None], ...]:
    """The manifest path and declared name of each `_run_secrets` member (#287).

    Index for index with `_run_secrets`. Each role's values come from the same
    function `_run_secrets` reads, paired with its twin in `bindings`, and the
    short-value floor is applied to the pairs. When several pairs hold one
    value, its provenance is the first pair by `connection_probe.secret_order`
    -- Scorer, Editor, Reflector, Grouper, then the declared name sorted within
    the role, a bare-role path after the named ones (ADR-0029 amendment
    2026-10-21) -- chosen before the duplicates are folded, so the fold cannot
    discard the name the record requires. The value's position stays its
    first occurrence, which is `_run_secrets`' own order; that tuple is
    unchanged.
    """

    snapshot = dict(os.environ) if environ is None else environ
    names = sorted(name for name in workflow.scorer.allowed_env if name in snapshot)
    pairs = [(snapshot[name], f"scorer.allowed_env.{name}") for name in names]
    pairs += zip(
        editor_environment_values(workflow.editor, snapshot),
        editor_environment_paths(workflow.editor, snapshot),
        strict=True,
    )
    pairs += zip(
        reflector_environment_values(workflow.reflector, snapshot),
        reflector_environment_paths(workflow.reflector, snapshot),
        strict=True,
    )
    if workflow.grouper is not None:
        pairs += zip(
            grouper_environment_values(workflow.grouper, snapshot),
            grouper_environment_paths(workflow.grouper, snapshot),
            strict=True,
        )
    kept = [(value, path) for value, path in pairs if len(value) >= _MIN_SECRET_LENGTH]
    paths = [path for _value, path in kept]
    declared = [_declared_name(workflow, path) for path in paths]
    chosen: dict[str, int] = {}
    for index, (value, _path) in enumerate(kept):
        best = chosen.get(value)
        # Reassigning an existing key keeps its position: the first occurrence.
        if best is None or secret_order(index, paths, declared) < secret_order(
            best, paths, declared
        ):
            chosen[value] = index
    return tuple((paths[index], declared[index]) for index in chosen.values())


def _declared_name(workflow: WorkflowSections, path: str) -> str | None:
    """The environment name the manifest declares at secret path `path` (#287).

    The `<NAME>` of `<role>.allowed_env.<NAME>`, the name a `chat` role's
    `<role>.api_key_env`/`<role>.proxy_env` (or `reflector.grouping.*`)
    declares, which its path does not spell, and `None` for a bare-role path
    (an undeclared value of an `agent_cli` role's bound environment).
    """

    _head, separator, tail = path.partition(".allowed_env.")
    if separator:
        return tail
    owner, dot, key = path.rpartition(".")
    if not dot or key not in ("api_key_env", "proxy_env"):
        return None
    sections: dict[str, Any] = {
        "editor": workflow.editor,
        "reflector": workflow.reflector.role,
        "reflector.grouping": workflow.reflector.grouping,
        "grouper": workflow.grouper,
    }
    declared = getattr(sections.get(owner), key, None)
    return declared if isinstance(declared, str) else None


def _run_secret_paths(
    workflow: WorkflowSections, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The manifest path of each member of `_run_secrets`, index for index (#287).

    The connection probe names a run secret it finds in an instance
    description by where the manifest declares its name, never by the value:
    `scorer.allowed_env.<NAME>`, `<role>.allowed_env.<NAME>`,
    `<role>.api_key_env`, `<role>.proxy_env`, or the bare role name for a
    value of an `agent_cli` role's bound environment that no manifest key
    names (ruling R10). A value several names hold is named by the first in
    role order, names sorted within a role (`_run_secret_provenance`).

    `environ` must be the snapshot `_run_secrets` read: one mapping, so the
    set and its names cannot disagree.
    """

    return tuple(path for path, _name in _run_secret_provenance(workflow, environ))


def _run_secret_names(
    workflow: WorkflowSections, environ: Mapping[str, str] | None = None
) -> tuple[str | None, ...]:
    """The declared environment name of each member of `_run_secrets` (#287).

    Index for index with `_run_secret_paths`, from the same provenance
    (`_declared_name`): a `chat` role's path does not spell its name. The
    probe and the campaign order matching secrets by it, names sorted within a
    role (ADR-0029 amendment 2026-10-21). Names only, never a value.
    """

    return tuple(name for _path, name in _run_secret_provenance(workflow, environ))


def _secrets(
    *allowed_env: frozenset[str], environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The values C1's redactor must keep out of every journal payload (spec §9).

    Every name either program may see, that the process environment actually
    has: a token an operator forwards to a scorer or an editor is a secret of
    the run, and the editor's own note is journaled.

    Every value, that is, of at least `_MIN_SECRET_LENGTH` characters. Both
    consumers of this tuple treat a member as a needle to be found in arbitrary
    text, and a needle of one or two characters is found everywhere. C1's
    journal is a *refuser*: `storage.envelope` rejects an envelope whose strings
    contain a secret and scans `run_id`, `timestamp` and `event` as well as the
    payload, so `allowed_env: ["DEBUG"]` with `DEBUG=1` -- or `CI=true`,
    `NO_COLOR=1`, `LC_ALL=C` -- would make the campaign's very first append
    invalid and turn a legitimate run into `CampaignInvariantViolated` after the
    run root was touched. The CLI's redactor is a *replacer*: the same `"1"`
    rewrites `"score 1.0"` in the printed document, and an empty value redacts
    between every pair of characters. A value that short cannot be a
    credential; it is a flag, and forwarding a flag must not poison a campaign.

    The rule is a length rule and cannot tell a six-digit PIN from `DEBUG=1`, so
    the skip is announced rather than assumed: `short_forwarded_names` lists the
    names it applies to and the CLI prints one note per name before the run.

    `environ` is the mapping to read, defaulting to a snapshot taken here.
    """

    snapshot = dict(os.environ) if environ is None else environ
    names = frozenset().union(*allowed_env) if allowed_env else frozenset()
    values = (snapshot[name] for name in sorted(names) if name in snapshot)
    return tuple(value for value in values if len(value) >= _MIN_SECRET_LENGTH)


def _http_session_adapter(
    manifest: ManifestV2, run_id: str, ledger: TargetCallLedger | None = None
) -> PromotionCapableAdapter:
    """The default adapter: one loopback `/evolution/v2` target, no socket yet."""

    adapter = HttpSessionAdapter(manifest.target, manifest.policy, run_id=run_id, ledger=ledger)
    # The same typing artefact `conformance` casts around: R1's `TargetInstance`
    # and `TargetSession` protocols declare settable members, so the frozen
    # `HttpInstance` this adapter returns is not a structural subtype of
    # `TargetInstance` and the `materialize` return types do not line up.
    return cast(PromotionCapableAdapter, adapter)
