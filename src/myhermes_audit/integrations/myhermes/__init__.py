"""MyHermes worker protocol and isolated adapter implementation."""

from myhermes_audit.integrations.myhermes.contracts import (
    AblationArtifact,
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
    ProcessCleanupArtifact,
    ProcessScenarioArtifact,
    ToolchainScenarioArtifact,
    WorkerArtifactPaths,
    WorkerStatus,
)
from myhermes_audit.integrations.myhermes.capability_contracts import (
    SubjectCapabilityReport,
)

__all__ = (
    "AblationArtifact",
    "MyHermesWorkerRequest",
    "MyHermesWorkerResult",
    "ObservationBundle",
    "ProcessCleanupArtifact",
    "ProcessScenarioArtifact",
    "SubjectCapabilityReport",
    "ToolchainScenarioArtifact",
    "WorkerArtifactPaths",
    "WorkerStatus",
)
