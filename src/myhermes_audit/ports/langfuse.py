"""SDK-neutral Langfuse port and publication request objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from myhermes_audit.contracts import (
    AblationComparisonResult,
    AuditCase,
    DataClassification,
    LangfuseDatasetItemIdentity,
    LangfuseDatasetSyncPlan,
    LangfuseDatasetSyncResult,
    LangfuseExperimentIdentity,
    LangfusePublicationManifest,
    LangfusePublicationCounts,
    LangfuseTrialPublishReceipt,
    PublicationManifestRef,
    TrialResult,
)


@dataclass(frozen=True, slots=True)
class LangfuseExperimentRequest:
    identity: LangfuseExperimentIdentity
    suite_id: str
    suite_sha256: str
    subject_commit: str
    audit_commit: str
    audit_version: str


@dataclass(frozen=True, slots=True)
class LangfuseTrialRequest:
    experiment: LangfuseExperimentIdentity
    dataset_item: LangfuseDatasetItemIdentity
    suite_id: str
    suite_sha256: str
    subject_commit: str
    subject_dirty: bool
    audit_commit: str
    audit_version: str
    result_schema_version: str
    suite_comparison_sha256: str | None
    run_configuration_fingerprint: str
    case: AuditCase
    trial: TrialResult
    data_classification: DataClassification
    no_content: bool
    ablation_comparison: AblationComparisonResult | None = None


class LangfusePort(Protocol):
    def check_connection(self) -> None:
        """Validate credentials without creating remote data."""

    def sync_dataset(
        self,
        plan: LangfuseDatasetSyncPlan,
    ) -> LangfuseDatasetSyncResult:
        """Idempotently create/update the declared Dataset and current items."""

    def begin_experiment(
        self,
        request: LangfuseExperimentRequest,
    ) -> LangfuseExperimentIdentity:
        """Begin one serial AuditRun-to-Experiment lifecycle and local Manifest."""

    def publish_trial(
        self,
        request: LangfuseTrialRequest,
    ) -> LangfuseTrialPublishReceipt:
        """Replay local facts through the official Experiment Runner once."""

    def publish_scores(
        self,
        request: LangfuseTrialRequest,
        receipt: LangfuseTrialPublishReceipt,
    ) -> int:
        """Publish only supported top-level quality scores."""

    def finish_experiment(
        self,
        identity: LangfuseExperimentIdentity,
        receipts: Sequence[LangfuseTrialPublishReceipt],
    ) -> LangfuseExperimentIdentity:
        """Finalize the Experiment identity returned by the official runner."""

    def publication_manifest(self) -> LangfusePublicationManifest:
        """Return the current SDK-neutral publication state."""

    def publication_manifest_ref(self) -> PublicationManifestRef:
        """Return the local Manifest path, digest, and status."""

    def publication_counts(self) -> LangfusePublicationCounts:
        """Return confirmed/skipped/uncertain/failed publication counts."""

    def flush(self) -> None:
        """Deliver queued SDK events."""

    def shutdown(self) -> None:
        """Flush and stop SDK resources."""


__all__ = (
    "LangfuseExperimentRequest",
    "LangfusePort",
    "LangfuseTrialRequest",
)
