"""Durable Python Owner state and closed /1 value objects.

The package initializer is intentionally stdlib-only.  Runtime adapters and core
campaign imports live behind later Owner implementation seams.
"""
from .models import (
    Accepted,
    BaselineImportChoice,
    CleanupFacts,
    CoreTerminal,
    ClientTask,
    Connection,
    Coverage,
    DiffResult,
    ExportResult,
    DeletionResult,
    ExecutorIdentity,
    Failure,
    RefusalFinding,
    RefusalRecord,
    WorkerFailureRecord,
    Health,
    OperationResult,
    OperationStatus,
    OwnerError,
    Persistence,
    PolicyRecord,
    ProcessFacts,
    ReleaseReceipt,
    ReportResult,
    ReportMarkdownResult,
    SuspensionEvidenceResult,
    ResourceRef,
    ResumeVerdict,
    ShutdownResult,
    StartFacts,
    StopReceipt,
    TargetRef,
    TargetRequirement,
    TargetStatus,
    TaskRef,
    TaskResult,
    TaskStatus,
)

__all__ = [
    "Accepted",
    "BaselineImportChoice",
    "RefusalFinding", "RefusalRecord", "WorkerFailureRecord",
    "Owner",
    "CleanupFacts", "CoreTerminal", "ClientTask", "Connection", "Coverage", "DiffResult", "ExportResult", "DeletionResult", "ExecutorIdentity", "Failure", "Health",
    "OperationResult", "OperationStatus", "OwnerError", "Persistence", "PolicyRecord", "ProcessFacts",
    "ReleaseReceipt", "ReportResult", "ReportMarkdownResult", "SuspensionEvidenceResult", "ResourceRef", "ResumeVerdict", "ShutdownResult", "StartFacts", "StopReceipt",
    "TargetRef", "TargetRequirement", "TargetStatus", "TaskRef", "TaskResult", "TaskStatus",
]


def __getattr__(name):
    if name == "Owner":
        from .api import Owner
        return Owner
    raise AttributeError(name)
