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
)


class LangfuseDatasetIdentity(ContractModel):
    dataset_name: NonEmptyText
    suite_id: Identifier
    suite_sha256: Sha256Digest


class LangfuseDatasetItemIdentity(ContractModel):
    dataset_name: NonEmptyText
    case_id: Identifier
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
        case_ids = [item.identity.case_id for item in self.items]
        remote_ids = [item.identity.remote_item_id for item in self.items]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Dataset sync plan Case identities must be unique")
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
        case_ids = [item.case_id for item in self.items]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("synchronized Dataset Item Case IDs must be unique")
        if any(item.dataset_name != self.dataset.dataset_name for item in self.items):
            raise ValueError("synchronized Dataset Item names must match the Dataset")
        return self


class LangfuseExperimentIdentity(ContractModel):
    experiment_name: NonEmptyText
    audit_run_id: Identifier
    dataset_name: NonEmptyText
    remote_run_id: StrictStr | None = None
    url: StrictStr | None = None


class LangfuseTrialPublishReceipt(ContractModel):
    trial_id: Identifier
    dataset_item_id: NonEmptyText
    trace_id: NonEmptyText
    observation_id: NonEmptyText
    dataset_run_id: NonEmptyText
    url: StrictStr | None = None


class LangfusePublishStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    ERROR = "error"


class LangfusePublishError(ContractModel):
    phase: Identifier
    error_type: Identifier
    message: NonEmptyText
    trial_id: Identifier | None = None
    retryable: StrictBool = False
    metadata: JsonObject = Field(default_factory=dict)


class LangfusePublishResult(ContractModel):
    status: LangfusePublishStatus
    dataset: LangfuseDatasetIdentity
    experiment: LangfuseExperimentIdentity
    published_trial_count: NonNegativeInt = 0
    published_score_count: NonNegativeInt = 0
    errors: list[LangfusePublishError] = Field(default_factory=list)
    warnings: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "LangfusePublishResult":
        if self.status is LangfusePublishStatus.COMPLETED and self.errors:
            raise ValueError("completed Langfuse publication cannot contain errors")
        if self.status is LangfusePublishStatus.ERROR and not self.errors:
            raise ValueError("error Langfuse publication requires errors")
        if self.status is LangfusePublishStatus.PARTIAL:
            if not self.errors or self.published_trial_count == 0:
                raise ValueError(
                    "partial Langfuse publication requires errors and a published Trial"
                )
        return self


__all__ = (
    "LangfuseDatasetIdentity",
    "LangfuseDatasetItemIdentity",
    "LangfuseDatasetItemPlan",
    "LangfuseDatasetSyncPlan",
    "LangfuseDatasetSyncResult",
    "LangfuseExperimentIdentity",
    "LangfusePublishError",
    "LangfusePublishResult",
    "LangfusePublishStatus",
    "LangfuseTrialPublishReceipt",
)
