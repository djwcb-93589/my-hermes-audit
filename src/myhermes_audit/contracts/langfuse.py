"""SDK-neutral Langfuse dataset, experiment, and publication contracts."""

from __future__ import annotations

from enum import Enum

from pydantic import Field, JsonValue, StrictBool, StrictStr, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    Sha256Digest,
    UtcDatetime,
)


class LangfuseDatasetIdentity(ContractModel):
    dataset_name: NonEmptyText
    suite_id: Identifier
    suite_sha256: Sha256Digest


class LangfuseDatasetItemIdentity(ContractModel):
    dataset_name: NonEmptyText
    case_id: Identifier
    variant_id: Identifier | None = None
    case_sha256: Sha256Digest
    remote_item_id: StrictStr | None = None


class LangfuseDatasetItemPlan(ContractModel):
    identity: LangfuseDatasetItemIdentity
    input: JsonValue
    expected_output: JsonValue
    metadata: JsonObject = Field(default_factory=dict)


class LangfuseDatasetSyncPlan(ContractModel):
    dataset: LangfuseDatasetIdentity
    items: list[LangfuseDatasetItemPlan] = Field(default_factory=list)
    dry_run: StrictBool = False
    no_content: StrictBool = False

    @model_validator(mode="after")
    def validate_items(self) -> "LangfuseDatasetSyncPlan":
        item_keys = [
            (item.identity.case_id, item.identity.variant_id)
            for item in self.items
        ]
        remote_ids = [item.identity.remote_item_id for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("Dataset sync plan Case/Variant identities must be unique")
        if any(
            item.identity.dataset_name != self.dataset.dataset_name
            for item in self.items
        ):
            raise ValueError("Dataset Item names must match the Dataset")
        if None in remote_ids or len(remote_ids) != len(set(remote_ids)):
            raise ValueError("Dataset sync plan remote item IDs must be present and unique")
        return self


class LangfuseDatasetSyncResult(ContractModel):
    dataset: LangfuseDatasetIdentity
    items: list[LangfuseDatasetItemIdentity] = Field(default_factory=list)
    dry_run: StrictBool
    planned_upsert_count: NonNegativeInt
    added_count: NonNegativeInt | None = None
    updated_count: NonNegativeInt | None = None
    unchanged_count: NonNegativeInt | None = None
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "LangfuseDatasetSyncResult":
        if self.planned_upsert_count != len(self.items):
            raise ValueError("planned_upsert_count must match synchronized items")
        known = (self.added_count, self.updated_count, self.unchanged_count)
        if any(value is None for value in known) and not all(
            value is None for value in known
        ):
            raise ValueError("remote action counts must be all known or all unknown")
        if self.dry_run and any(value is not None for value in known):
            raise ValueError("dry-run remote action counts must remain unknown")
        if not self.dry_run and any(value is None for value in known):
            raise ValueError("non-dry-run remote action counts must be known")
        if all(value is not None for value in known) and sum(known) != len(self.items):
            raise ValueError("remote action counts must cover synchronized items")
        item_keys = [(item.case_id, item.variant_id) for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError(
                "synchronized Dataset Item Case/Variant IDs must be unique"
            )
        if any(item.dataset_name != self.dataset.dataset_name for item in self.items):
            raise ValueError("synchronized Dataset Item names must match the Dataset")
        return self


class ExperimentStrategy(str, Enum):
    RUNNER_REPLAY = "experiment_runner_replay"
    UNSUPPORTED = "unsupported_by_sdk"


class ExperimentPublicationStatus(str, Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PARTIALLY_PUBLISHED = "partially_published"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class DatasetSyncPublicationStatus(str, Enum):
    PUBLISHED = "published"
    FAILED = "failed"


class PublicationItemStatus(str, Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class PublicationManifestStatus(str, Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PARTIALLY_PUBLISHED = "partially_published"
    FAILED = "failed"


class LangfuseExperimentIdentity(ContractModel):
    experiment_name: NonEmptyText
    audit_run_id: Identifier
    dataset_name: NonEmptyText
    run_name: StrictStr | None = None
    remote_run_id: StrictStr | None = None
    url: StrictStr | None = None


class ReplayTrialPayload(ContractModel):
    """Safe, immutable input consumed by the official Experiment Runner task."""

    audit_run_id: Identifier
    trial_id: Identifier
    case_id: Identifier
    dataset_item_id: NonEmptyText
    final_output: JsonValue
    runtime_status: NonEmptyText
    safe_metric_summary: JsonObject = Field(default_factory=dict)
    local_trace_id: NonEmptyText
    artifact_summary: list[JsonObject] = Field(default_factory=list)


class LangfuseTrialPublishReceipt(ContractModel):
    trial_id: Identifier
    dataset_item_id: NonEmptyText
    experiment_item_key: NonEmptyText
    trace_id: NonEmptyText
    observation_id: NonEmptyText
    dataset_run_id: NonEmptyText
    experiment_id: NonEmptyText
    url: StrictStr | None = None


class ScorePublicationIdentity(ContractModel):
    score_id: Sha256Digest
    trace_id: NonEmptyText
    score_name: NonEmptyText
    evaluator_version: NonEmptyText
    trial_id: Identifier
    case_id: Identifier
    stable_timestamp: UtcDatetime
    value_hash: Sha256Digest


class LangfusePublishError(ContractModel):
    phase: Identifier
    error_type: Identifier
    message: NonEmptyText
    trial_id: Identifier | None = None
    retryable: StrictBool = False
    metadata: JsonObject = Field(default_factory=dict)


class TrialPublicationRecord(ContractModel):
    publication_key: Sha256Digest
    audit_run_id: Identifier
    trial_id: Identifier
    case_id: Identifier
    dataset_item_id: NonEmptyText
    local_trace_id: NonEmptyText
    content_fingerprint: Sha256Digest
    created_at: UtcDatetime
    updated_at: UtcDatetime
    confirmation_supported: StrictBool = True
    status: PublicationItemStatus = PublicationItemStatus.PENDING
    attempt_count: NonNegativeInt = 0
    remote_trace_id: StrictStr | None = None
    remote_observation_id: StrictStr | None = None
    dataset_run_id: StrictStr | None = None
    experiment_id: StrictStr | None = None
    experiment_item_key: StrictStr | None = None
    last_attempt_at: UtcDatetime | None = None
    confirmed_at: UtcDatetime | None = None
    error: LangfusePublishError | None = None

    @model_validator(mode="after")
    def validate_publication_state(self) -> "TrialPublicationRecord":
        remote_values = (
            self.remote_trace_id,
            self.remote_observation_id,
            self.dataset_run_id,
            self.experiment_id,
            self.experiment_item_key,
        )
        if self.updated_at < self.created_at:
            raise ValueError("Trial publication update cannot precede creation")
        if self.status is PublicationItemStatus.CONFIRMED:
            if any(value is None for value in remote_values) or self.confirmed_at is None:
                raise ValueError("confirmed Trial publication requires all remote identities")
            if self.error is not None:
                raise ValueError("confirmed Trial publication cannot contain an error")
        if self.status in {PublicationItemStatus.UNCERTAIN, PublicationItemStatus.FAILED}:
            if self.error is None:
                raise ValueError("uncertain/failed Trial publication requires an error")
        if self.attempt_count == 0 and self.last_attempt_at is not None:
            raise ValueError("Trial attempt timestamp requires a positive attempt count")
        return self


class ScorePublicationRecord(ContractModel):
    publication_key: Sha256Digest
    audit_run_id: Identifier
    dataset_item_id: NonEmptyText
    dataset_run_id: NonEmptyText
    experiment_id: NonEmptyText
    content_fingerprint: Sha256Digest
    created_at: UtcDatetime
    updated_at: UtcDatetime
    confirmation_supported: StrictBool
    identity: ScorePublicationIdentity
    status: PublicationItemStatus = PublicationItemStatus.PENDING
    attempt_count: NonNegativeInt = 0
    remote_id: StrictStr | None = None
    last_attempt_at: UtcDatetime | None = None
    confirmed_at: UtcDatetime | None = None
    error: LangfusePublishError | None = None

    @model_validator(mode="after")
    def validate_publication_state(self) -> "ScorePublicationRecord":
        if self.updated_at < self.created_at:
            raise ValueError("Score publication update cannot precede creation")
        if self.status is PublicationItemStatus.CONFIRMED:
            if not self.confirmation_supported:
                raise ValueError(
                    "confirmed Score publication requires confirmation capability"
                )
            if self.remote_id is None or self.confirmed_at is None:
                raise ValueError("confirmed Score publication requires remote identity")
            if self.error is not None:
                raise ValueError("confirmed Score publication cannot contain an error")
        if self.status in {PublicationItemStatus.UNCERTAIN, PublicationItemStatus.FAILED}:
            if self.error is None:
                raise ValueError("uncertain/failed Score publication requires an error")
        if self.attempt_count == 0 and self.last_attempt_at is not None:
            raise ValueError("Score attempt timestamp requires a positive attempt count")
        return self


class LangfusePublicationManifest(ContractModel):
    audit_run_id: Identifier
    experiment_name: NonEmptyText
    dataset_name: NonEmptyText
    created_at: UtcDatetime
    updated_at: UtcDatetime
    trial_publications: list[TrialPublicationRecord] = Field(default_factory=list)
    score_publications: list[ScorePublicationRecord] = Field(default_factory=list)
    remote_ids: JsonObject = Field(default_factory=dict)
    stable_timestamps: dict[NonEmptyText, UtcDatetime] = Field(default_factory=dict)
    score_submission_supported: StrictBool = False
    score_confirmation_supported: StrictBool = False
    status: PublicationManifestStatus = PublicationManifestStatus.PENDING
    last_error: LangfusePublishError | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> "LangfusePublicationManifest":
        if self.updated_at < self.created_at:
            raise ValueError("Manifest updated_at cannot precede created_at")
        if self.score_confirmation_supported and not self.score_submission_supported:
            raise ValueError("Manifest Score confirmation requires submission support")
        trial_ids = [item.trial_id for item in self.trial_publications]
        if len(trial_ids) != len(set(trial_ids)):
            raise ValueError("Manifest Trial identities must be unique")
        trial_keys = [item.publication_key for item in self.trial_publications]
        if len(trial_keys) != len(set(trial_keys)):
            raise ValueError("Manifest Trial publication keys must be unique")
        score_ids = [item.identity.score_id for item in self.score_publications]
        if len(score_ids) != len(set(score_ids)):
            raise ValueError("Manifest Score identities must be unique")
        score_keys = [item.publication_key for item in self.score_publications]
        if len(score_keys) != len(set(score_keys)):
            raise ValueError("Manifest Score publication keys must be unique")
        if any(
            item.audit_run_id != self.audit_run_id
            for item in (*self.trial_publications, *self.score_publications)
        ):
            raise ValueError("Manifest publications must match the Audit run")
        trial_by_id = {item.trial_id: item for item in self.trial_publications}
        for score in self.score_publications:
            trial = trial_by_id.get(score.identity.trial_id)
            if trial is None:
                raise ValueError("Manifest Score publication requires its Trial record")
            if (
                score.dataset_item_id != trial.dataset_item_id
                or score.dataset_run_id != trial.dataset_run_id
                or score.experiment_id != trial.experiment_id
            ):
                raise ValueError(
                    "Manifest Score publication must match its Trial remote identities"
                )
        if self.status is PublicationManifestStatus.PUBLISHED:
            if self.last_error:
                raise ValueError("published Manifest cannot contain a last error")
            if not self.trial_publications or any(
                item.status is not PublicationItemStatus.CONFIRMED
                for item in (*self.trial_publications, *self.score_publications)
            ):
                raise ValueError(
                    "published Manifest requires every publication to be confirmed"
                )
        return self


class PublicationManifestRef(ContractModel):
    path: NonEmptyText
    sha256: Sha256Digest
    status: PublicationManifestStatus


class LangfusePublicationCounts(ContractModel):
    published_trial_count: NonNegativeInt = 0
    associated_experiment_item_count: NonNegativeInt = 0
    published_score_count: NonNegativeInt = 0
    skipped_score_count: NonNegativeInt = 0
    uncertain_score_count: NonNegativeInt = 0
    failed_score_count: NonNegativeInt = 0


class LangfuseCapabilityReport(ContractModel):
    installed: StrictBool
    version: StrictStr | None = None
    compatible: StrictBool
    required_minimum_version: NonEmptyText
    capabilities: dict[NonEmptyText, StrictBool] = Field(default_factory=dict)
    missing_capabilities: list[NonEmptyText] = Field(default_factory=list)
    warnings: list[NonEmptyText] = Field(default_factory=list)
    experiment_strategy: ExperimentStrategy
    score_idempotency_strategy: NonEmptyText
    score_submission_supported: StrictBool
    score_confirmation_supported: StrictBool

    @model_validator(mode="after")
    def validate_capabilities(self) -> "LangfuseCapabilityReport":
        expected_missing = sorted(
            name for name, available in self.capabilities.items() if not available
        )
        if sorted(self.missing_capabilities) != expected_missing:
            raise ValueError("missing_capabilities must match capability flags")
        if self.compatible and (not self.installed or expected_missing):
            raise ValueError("compatible SDK must be installed with every capability")
        if not self.installed and self.version is not None:
            raise ValueError("uninstalled SDK cannot report a version")
        if self.score_confirmation_supported and not self.score_submission_supported:
            raise ValueError("Score confirmation requires Score submission support")
        if (
            self.capabilities.get("score_submission_supported", False)
            is not self.score_submission_supported
        ):
            raise ValueError(
                "Score submission field must match the capability flags"
            )
        if (
            self.capabilities.get("score_confirmation_supported", False)
            is not self.score_confirmation_supported
        ):
            raise ValueError(
                "Score confirmation field must match the capability flags"
            )
        return self


class LangfusePublishStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    ERROR = "error"


class LangfusePublishResult(ContractModel):
    status: LangfusePublishStatus
    dataset: LangfuseDatasetIdentity
    experiment: LangfuseExperimentIdentity
    dataset_sync_status: DatasetSyncPublicationStatus
    experiment_status: ExperimentPublicationStatus
    experiment_strategy: ExperimentStrategy
    published_trial_count: NonNegativeInt = 0
    associated_experiment_item_count: NonNegativeInt = 0
    published_score_count: NonNegativeInt = 0
    skipped_score_count: NonNegativeInt = 0
    uncertain_score_count: NonNegativeInt = 0
    failed_score_count: NonNegativeInt = 0
    publication_manifest: PublicationManifestRef | None = None
    errors: list[LangfusePublishError] = Field(default_factory=list)
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "LangfusePublishResult":
        if self.associated_experiment_item_count > self.published_trial_count:
            raise ValueError("Experiment Item count cannot exceed published Trial count")
        if self.status is LangfusePublishStatus.COMPLETED:
            if self.errors:
                raise ValueError("completed Langfuse publication cannot contain errors")
            if self.experiment_status is not ExperimentPublicationStatus.PUBLISHED:
                raise ValueError("completed publication requires a published Experiment")
            if self.uncertain_score_count or self.failed_score_count:
                raise ValueError("completed publication cannot contain unresolved Scores")
        if self.status is LangfusePublishStatus.ERROR and not self.errors:
            raise ValueError("error Langfuse publication requires errors")
        if self.status is LangfusePublishStatus.PARTIAL:
            if not self.errors or self.published_trial_count == 0:
                raise ValueError(
                    "partial Langfuse publication requires errors and a published Trial"
                )
        return self


__all__ = (
    "DatasetSyncPublicationStatus",
    "ExperimentPublicationStatus",
    "ExperimentStrategy",
    "LangfuseCapabilityReport",
    "LangfuseDatasetIdentity",
    "LangfuseDatasetItemIdentity",
    "LangfuseDatasetItemPlan",
    "LangfuseDatasetSyncPlan",
    "LangfuseDatasetSyncResult",
    "LangfuseExperimentIdentity",
    "LangfusePublicationManifest",
    "LangfusePublicationCounts",
    "LangfusePublishError",
    "LangfusePublishResult",
    "LangfusePublishStatus",
    "LangfuseTrialPublishReceipt",
    "PublicationItemStatus",
    "PublicationManifestRef",
    "PublicationManifestStatus",
    "ReplayTrialPayload",
    "ScorePublicationIdentity",
    "ScorePublicationRecord",
    "TrialPublicationRecord",
)
