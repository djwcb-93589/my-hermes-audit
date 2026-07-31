"""MyHermes worker protocol and isolated adapter implementation."""

from myhermes_audit.integrations.myhermes.contracts import (
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
    WorkerArtifactPaths,
    WorkerStatus,
)

__all__ = (
    "MyHermesWorkerRequest",
    "MyHermesWorkerResult",
    "ObservationBundle",
    "WorkerArtifactPaths",
    "WorkerStatus",
)
