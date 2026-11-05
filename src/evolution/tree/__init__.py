"""Service-first evolution of immutable Candidate skill trees.

Campaign manifests `/15` and `/16` drive families 14 and 15. This surface
provides campaign execution, recovery and inspection, journal and artifact
services, and `conformance_manifest` for workflow-less service input.
`manifest_dependencies`, `DependencyInventory`, `Dependency` and
`EnvironmentName` expose a read-only inventory of declared dependencies.
`evaluate` and `inspect_evaluation` measure and inspect a frozen tree on an
explicit train or test split, outside campaign promotion and role inputs.

Target integration includes the target-server protocols, HTTP adapter,
reference target and rollout messages. `CampaignCandidateEditor` and
`CampaignCandidateReflector`, with their request, result and identity types,
are the role protocols; implementations and binders stay by module path.
Operator authorization uses `Authorization`, `RefusalDifference`,
`refusal_class`, `read_decisions` and `write_decision`; `IdentityChanged`
reports a rebuilt identity that differs from the journaled identity.

The four promised scorer-trace names are `read_scorer_trace`,
`ScorerTraceReader`, `ScorerTraceRecord` and `ScorerTraceUnreadable`.
They read one try's request, stdout and stderr from either trace layout.
"""

from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCapacityError,
    ArtifactCorruptionError,
    ArtifactStoreStopped,
    ArtifactUnsafeError,
)
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifact_projections import (
    ArtifactReference,
    CandidateEntry,
    ChampionEntry,
    LineageProjection,
    project_lineage,
)
from evolution.tree.artifacts.artifacts import (
    ArtifactRef,
    ArtifactStore,
    CapacityEstimate,
    ExpectedAttestation,
    RecoveryReport,
    SelectionDecision,
)
from evolution.tree.artifacts.attestation import (
    PromotionCapableAdapter,
    TargetPreflight,
    verify_attestation,
)
from evolution.tree.campaign import (
    ArtifactLedgerMissing,
    CampaignExists,
    CampaignMissing,
    CampaignResult,
    IdentityChanged,
    LineageChampionUnverified,
    LineageFamilyUnsupported,
    LineageModeUnsupported,
    LineageNotContinuable,
    LineageNotFinished,
    UndeclaredSibling,
    inspect_campaign,
    resume_campaign,
    run_campaign,
)
from evolution.tree.campaign_identity import CampaignIdentityV1, LinkedFrom
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    PolicyError,
)
from evolution.tree.campaign_projections import (
    CampaignProjection,
    CampaignUnreadable,
    project_campaign_result,
    project_campaign_status,
    read_run_id,
)
from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION, CampaignSnapshot
from evolution.tree.campaign_view import CampaignView, next_actions
from evolution.tree.conformance import (
    Check,
    ConformanceReport,
    Scenario,
    adapter_conformance,
    run_conformance,
    wire_conformance,
)
from evolution.tree.evaluation import evaluate, inspect_evaluation
from evolution.tree.execution.authorizations import (
    Authorization,
    RefusalDifference,
    read_decisions,
    refusal_class,
    write_decision,
)
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.config import ManifestError
from evolution.tree.execution.interaction import (
    AttestationError,
    CallRetries,
    EvalCase,
    Message,
    Turn,
    TurnResult,
)
from evolution.tree.execution.journal import Journal, Snapshot, inspect_journal
from evolution.tree.execution.journal_identity import RunIdentityV1, ScorerRecoveryMode
from evolution.tree.execution.journal_projections import (
    project_events,
    project_result,
    project_status,
)
from evolution.tree.execution.journal_recovery import recover_journal, recovery_plan
from evolution.tree.execution.journal_rollout import JournalRolloutAdapter, durable_rollout
from evolution.tree.execution.journal_scoring import durable_score
from evolution.tree.execution.rollout_scoring import parse_verdicts
from evolution.tree.execution.scorer_traces import (
    ScorerTraceReader,
    ScorerTraceRecord,
    ScorerTraceUnreadable,
    read_scorer_trace,
)
from evolution.tree.execution.usage import Usage
from evolution.tree.execution.wire_v2 import (
    PREFIX,
    STRATEGY,
    is_artifact_path,
    new_instance_id,
    validate_instance_description,
)
from evolution.tree.manifest.manifest_inventory import (
    Dependency,
    DependencyInventory,
    EnvironmentName,
    manifest_dependencies,
)
from evolution.tree.manifest.manifest_v2 import (
    SCHEMA_V15,
    ManifestV2,
    conformance_manifest,
    load_manifest_v2,
)
from evolution.tree.roles.editor_contracts import CandidateEditor as CampaignCandidateEditor
from evolution.tree.roles.editor_contracts import EditBrief, EditRequest, EditResult
from evolution.tree.roles.identities import EditorIdentity, ReflectorIdentity
from evolution.tree.roles.reflector_contracts import (
    CandidateReflector as CampaignCandidateReflector,
)
from evolution.tree.roles.reflector_contracts import ReflectRequest, ReflectResult
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalBoundaryError,
    JournalFault,
    JournalRecoveryRequired,
)
from evolution.tree.target.config import TargetBinding
from evolution.tree.target.connection_contract import ConnectionContract
from evolution.tree.target.http_adapter import HttpInstance, HttpSessionAdapter, HttpTransport
from evolution.tree.target.reference_target import ReferenceTarget
from evolution.tree.target.reference_target import serve as serve_reference_target
from evolution.tree.target.target_calls import (
    TargetCallLedger,
    TargetCallRecord,
    TargetCallSettlement,
)
from evolution.tree.target.target_server import (
    BackendError,
    BackendInstance,
    BackendReply,
    BackendSession,
    LoadedTree,
    TargetBackend,
    TargetServer,
    serve_target,
)

__all__ = [
    "EvalCase",
    "Turn",
    "parse_verdicts",
    "is_artifact_path",
    "PREFIX",
    "STRATEGY",
    "validate_instance_description",
    "Journal",
    "Snapshot",
    "RunIdentityV1",
    "ScorerRecoveryMode",
    "JournalRolloutAdapter",
    "inspect_journal",
    "read_scorer_trace",
    "ScorerTraceReader",
    "ScorerTraceRecord",
    "ScorerTraceUnreadable",
    "project_events",
    "project_result",
    "project_status",
    "recover_journal",
    "recovery_plan",
    "durable_rollout",
    "durable_score",
    "JournalAdmissionRefused",
    "JournalBoundaryError",
    "JournalFault",
    "JournalRecoveryRequired",
    "ArtifactStore",
    "ArtifactPolicyV1",
    "scan_tree",
    "ArtifactRef",
    "ArtifactReference",
    "CandidateEntry",
    "ChampionEntry",
    "CapacityEstimate",
    "ExpectedAttestation",
    "LineageProjection",
    "RecoveryReport",
    "SelectionDecision",
    "project_lineage",
    "ArtifactError",
    "ArtifactUnsafeError",
    "ArtifactCapacityError",
    "ArtifactCorruptionError",
    "ArtifactAdmissionRefused",
    "ArtifactStoreStopped",
    "AttestationError",
    "PromotionCapableAdapter",
    "TargetPreflight",
    "verify_attestation",
    "HttpInstance",
    "HttpSessionAdapter",
    "HttpTransport",
    "TargetCallLedger",
    "TargetCallSettlement",
    "TargetCallRecord",
    "Authorization",
    "RefusalDifference",
    "read_decisions",
    "refusal_class",
    "write_decision",
    "ManifestError",
    "SCHEMA_V15",
    "ManifestV2",
    "TargetBinding",
    "load_manifest_v2",
    "conformance_manifest",
    "manifest_dependencies",
    "DependencyInventory",
    "Dependency",
    "EnvironmentName",
    "new_instance_id",
    "Check",
    "ConformanceReport",
    "Scenario",
    "adapter_conformance",
    "wire_conformance",
    "run_conformance",
    "ReferenceTarget",
    "serve_reference_target",
    "TargetServer",
    "TargetBackend",
    "BackendInstance",
    "BackendSession",
    "BackendReply",
    "LoadedTree",
    "BackendError",
    "serve_target",
    "CallRetries",
    "Message",
    "TurnResult",
    "Usage",
    "ArtifactLedgerMissing",
    "CampaignCandidateEditor",
    "CampaignCandidateReflector",
    "CampaignExists",
    "CampaignIdentityV1",
    "CampaignMissing",
    "CampaignPolicyV11",
    "FINAL_CAMPAIGN_FAMILY_VERSION",
    "ConnectionContract",
    "CampaignProjection",
    "CampaignResult",
    "CampaignSnapshot",
    "CampaignUnreadable",
    "CampaignView",
    "EditBrief",
    "EditRequest",
    "EditResult",
    "EditorIdentity",
    "IdentityChanged",
    "LineageChampionUnverified",
    "LineageFamilyUnsupported",
    "LineageModeUnsupported",
    "LineageNotContinuable",
    "LineageNotFinished",
    "LinkedFrom",
    "PolicyError",
    "ReflectRequest",
    "ReflectResult",
    "ReflectorIdentity",
    "UndeclaredSibling",
    "inspect_campaign",
    "next_actions",
    "project_campaign_result",
    "project_campaign_status",
    "read_run_id",
    "resume_campaign",
    "run_campaign",
    "evaluate",
    "inspect_evaluation",
]
