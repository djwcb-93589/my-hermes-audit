"""MyHermes worker protocol and isolated adapter implementation."""

from myhermes_audit.integrations.myhermes.contracts import (
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
    WorkerArtifactPaths,
    WorkerStatus,
)
from myhermes_audit.integrations.myhermes.capability_contracts import (
    SubjectCapabilityReport,
)

__all__ = (
    "MyHermesWorkerRequest",
    "MyHermesWorkerResult",
    "ObservationBundle",
    "SubjectCapabilityReport",
    "WorkerArtifactPaths",
    "WorkerStatus",
)
