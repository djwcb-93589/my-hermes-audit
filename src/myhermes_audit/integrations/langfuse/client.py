"""Delayed-import Langfuse v4 adapter for Dataset and Experiment publication."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
from typing import Any, Sequence
from urllib.parse import urlsplit

from myhermes_audit.contracts import (
    LangfuseDatasetSyncPlan,
    LangfuseDatasetSyncResult,
    LangfuseExperimentIdentity,
    LangfuseTrialPublishReceipt,
)
from myhermes_audit.errors import (
    AuditError,
    DatasetSyncError,
    ExperimentPublishError,
    LangfuseConfigError,
    LangfuseConnectionError,
    LangfuseDependencyError,
    ScorePublishError,
)
from myhermes_audit.integrations.langfuse.score_mapper import project_scores
from myhermes_audit.integrations.langfuse.trace_mapper import publish_trial_trace
from myhermes_audit.ports.langfuse import (
    LangfuseExperimentRequest,
    LangfuseTrialRequest,
)
from myhermes_audit.security import (
    redact_text,
    sanitize_external_error,
    sensitive_environment_values,
    truncate_text_head_tail,
)


LANGFUSE_ADAPTER_VERSION = "langfuse-v4-adapter-v1"
_SUPPORTED_LANGFUSE_MAJOR = 4


class LangfuseV4Adapter:
    """One parent-process Langfuse client with SDK-neutral return values."""

    def __init__(
        self,
        *,
        client: Any,
        propagate_attributes: Any,
        not_found_error: type[BaseException],
        sensitive_values: tuple[str, ...],
    ) -> None:
        self._client = client
        self._propagate_attributes = propagate_attributes
        self._not_found_error = not_found_error
        self._sensitive_values = sensitive_values
        self._active_experiment: LangfuseExperimentRequest | None = None
        self._shutdown = False

    @classmethod
    def from_environment(cls) -> "LangfuseV4Adapter":
        try:
            langfuse = importlib.import_module("langfuse")
            langfuse_api = importlib.import_module("langfuse.api")
            installed_version = importlib.metadata.version("langfuse")
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise LangfuseDependencyError(
                "Langfuse SDK is unavailable; install my-hermes-audit[langfuse]"
            ) from exc
        try:
            major = int(installed_version.split(".", maxsplit=1)[0])
        except (TypeError, ValueError) as exc:
            raise LangfuseDependencyError(
                "installed Langfuse SDK version cannot be identified"
            ) from exc
        if major != _SUPPORTED_LANGFUSE_MAJOR:
            raise LangfuseDependencyError(
                "Langfuse SDK major version 4 is required"
            )

        public_key = _required_environment("LANGFUSE_PUBLIC_KEY")
        secret_key = _required_environment("LANGFUSE_SECRET_KEY")
        if os.environ.get("LANGFUSE_TRACING_ENABLED", "true").lower() == "false":
            raise LangfuseConfigError(
                "LANGFUSE_TRACING_ENABLED=false is incompatible with --langfuse",
                field="LANGFUSE_TRACING_ENABLED",
            )
        if os.environ.get("OTEL_SDK_DISABLED", "false").lower() == "true":
            raise LangfuseConfigError(
                "OTEL_SDK_DISABLED=true is incompatible with --langfuse",
                field="OTEL_SDK_DISABLED",
            )
        base_url = (
            os.environ.get("LANGFUSE_BASE_URL")
            or os.environ.get("LANGFUSE_HOST")
            or None
        )
        if base_url is not None:
            base_url = base_url.strip()
            _validate_base_url(base_url)
        timeout = _timeout_from_environment()
        sensitive_values = tuple(
            sorted(
                {
                    *sensitive_environment_values(os.environ),
                    public_key,
                    secret_key,
                },
                key=len,
                reverse=True,
            )
        )
        arguments: dict[str, Any] = {
            "public_key": public_key,
            "secret_key": secret_key,
            "timeout": timeout,
            "sample_rate": 1.0,
        }
        if base_url is not None:
            arguments["base_url"] = base_url
        try:
            client = langfuse.Langfuse(**arguments)
            propagate_attributes = langfuse.propagate_attributes
            not_found_error = langfuse_api.NotFoundError
        except Exception as exc:
            raise LangfuseConfigError(
                "Langfuse client initialization failed",
                exception_type=type(exc).__name__,
            ) from exc
        return cls(
            client=client,
            propagate_attributes=propagate_attributes,
            not_found_error=not_found_error,
            sensitive_values=sensitive_values,
        )

    def check_connection(self) -> None:
        try:
            authenticated = self._client.auth_check()
        except Exception as exc:
            raise LangfuseConnectionError(
                "Langfuse connection check failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc
        if authenticated is not True:
            raise LangfuseConnectionError(
                "Langfuse connection check did not authenticate the client",
                retryable=False,
            )

    def sync_dataset(
        self,
        plan: LangfuseDatasetSyncPlan,
    ) -> LangfuseDatasetSyncResult:
        if plan.dry_run:
            raise DatasetSyncError(
                "dry-run Dataset planning must not instantiate the Langfuse adapter"
            )
        try:
            try:
                remote_dataset = self._client.get_dataset(plan.dataset.dataset_name)
            except self._not_found_error:
                self._client.create_dataset(
                    name=plan.dataset.dataset_name,
                    description=(
                        "Managed non-destructively by my-hermes-audit; historical "
                        "items are retained."
                    ),
                    metadata={
                        "audit_suite_id": plan.dataset.suite_id,
                        "audit_suite_sha256": plan.dataset.suite_sha256,
                        "managed_by": "my-hermes-audit",
                    },
                )
                existing_items: list[Any] = []
                remote_dataset_metadata: dict[str, Any] = {}
            else:
                existing_items = list(remote_dataset.items)
                candidate_metadata = getattr(remote_dataset, "metadata", None)
                remote_dataset_metadata = (
                    candidate_metadata
                    if isinstance(candidate_metadata, dict)
                    else {}
                )

            remote_suite_ids: set[str] = set()
            remote_suite_id = remote_dataset_metadata.get("audit_suite_id")
            if isinstance(remote_suite_id, str):
                remote_suite_ids.add(remote_suite_id)
            for existing_item in existing_items:
                metadata = getattr(existing_item, "metadata", None)
                if not isinstance(metadata, dict):
                    continue
                remote_suite_id = metadata.get("audit_suite_id")
                if isinstance(remote_suite_id, str):
                    remote_suite_ids.add(remote_suite_id)
            if remote_suite_ids - {plan.dataset.suite_id}:
                raise DatasetSyncError(
                    "Langfuse Dataset name is already associated with another Audit Suite"
                )

            by_id = {
                str(item.id): item
                for item in existing_items
                if isinstance(getattr(item, "id", None), str)
            }
            prior_case_ids = {
                metadata.get("audit_case_id")
                for item in existing_items
                if isinstance((metadata := getattr(item, "metadata", None)), dict)
                and isinstance(metadata.get("audit_case_id"), str)
            }
            added_count = 0
            updated_count = 0
            unchanged_count = 0
            for item in plan.items:
                remote_item_id = item.identity.remote_item_id
                if not remote_item_id:
                    raise DatasetSyncError(
                        "Dataset item plan is missing its stable remote identity",
                        case_id=item.identity.case_id,
                    )
                existing = by_id.get(remote_item_id)
                existing_metadata = (
                    getattr(existing, "metadata", None)
                    if existing is not None
                    else None
                )
                if (
                    isinstance(existing_metadata, dict)
                    and existing_metadata.get("audit_case_sha256")
                    == item.identity.case_sha256
                    and existing_metadata.get("audit_projection_sha256")
                    == item.metadata.get("audit_projection_sha256")
                ):
                    unchanged_count += 1
                    continue
                created = self._client.create_dataset_item(
                    dataset_name=plan.dataset.dataset_name,
                    id=remote_item_id,
                    input=item.input,
                    expected_output=item.expected_output,
                    metadata=item.metadata,
                )
                created_id = getattr(created, "id", None)
                if created_id != remote_item_id:
                    raise DatasetSyncError(
                        "Langfuse returned an unexpected Dataset Item identity",
                        case_id=item.identity.case_id,
                    )
                if existing is not None or item.identity.case_id in prior_case_ids:
                    updated_count += 1
                else:
                    added_count += 1
            return LangfuseDatasetSyncResult(
                dataset=plan.dataset,
                items=[item.identity for item in plan.items],
                dry_run=False,
                planned_upsert_count=len(plan.items),
                added_count=added_count,
                updated_count=updated_count,
                unchanged_count=unchanged_count,
                warnings=[
                    "historical Dataset Items are retained; P2 performs no destructive prune"
                ],
            )
        except AuditError:
            raise
        except Exception as exc:
            raise DatasetSyncError(
                "Langfuse Dataset synchronization failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc

    def begin_experiment(
        self,
        request: LangfuseExperimentRequest,
    ) -> LangfuseExperimentIdentity:
        if self._active_experiment is not None:
            raise ExperimentPublishError(
                "a Langfuse Experiment is already active for this adapter"
            )
        if request.identity.remote_run_id is not None or request.identity.url is not None:
            raise ExperimentPublishError(
                "a new Experiment identity cannot contain remote publication fields"
            )
        self._active_experiment = request
        return request.identity

    def publish_trial(
        self,
        request: LangfuseTrialRequest,
    ) -> LangfuseTrialPublishReceipt:
        self._validate_active_experiment(request.experiment)
        dataset_item_id = request.dataset_item.remote_item_id
        if not dataset_item_id:
            raise ExperimentPublishError(
                "Trial publication requires a synchronized Dataset Item identity",
                trial_id=request.trial.trial_id,
            )
        try:
            trace_id, observation_id = publish_trial_trace(
                self._client,
                self._propagate_attributes,
                request,
                sensitive_values=self._sensitive_values,
            )
            run_item = self._client.api.dataset_run_items.create(
                run_name=request.experiment.experiment_name,
                dataset_item_id=dataset_item_id,
                run_description="my-hermes-audit post-hoc P2 experiment publication",
                metadata={
                    "audit_run_id": request.experiment.audit_run_id,
                    "suite_id": request.suite_id,
                    "suite_sha256": request.suite_sha256,
                    "subject_commit": request.subject_commit,
                    "subject_dirty": request.subject_dirty,
                    "audit_commit": request.audit_commit,
                    "audit_version": request.audit_version,
                    "adapter_version": LANGFUSE_ADAPTER_VERSION,
                },
                observation_id=observation_id,
                trace_id=trace_id,
            )
            dataset_run_id = getattr(run_item, "dataset_run_id", None)
            if not isinstance(dataset_run_id, str) or not dataset_run_id:
                raise ExperimentPublishError(
                    "Langfuse did not return a Dataset Run identity",
                    trial_id=request.trial.trial_id,
                )
            return LangfuseTrialPublishReceipt(
                trial_id=request.trial.trial_id,
                dataset_item_id=dataset_item_id,
                trace_id=trace_id,
                observation_id=observation_id,
                dataset_run_id=dataset_run_id,
                url=None,
            )
        except AuditError:
            raise
        except Exception as exc:
            raise ExperimentPublishError(
                "Langfuse Trial publication failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                trial_id=request.trial.trial_id,
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc

    def publish_scores(
        self,
        request: LangfuseTrialRequest,
        receipt: LangfuseTrialPublishReceipt,
    ) -> int:
        self._validate_active_experiment(request.experiment)
        if receipt.trial_id != request.trial.trial_id:
            raise ScorePublishError(
                "Score publication receipt does not match the Trial",
                trial_id=request.trial.trial_id,
            )
        published_count = 0
        try:
            for score in project_scores(request.trial):
                score_id = self._client.create_observation_id(
                    seed="|".join(
                        (
                            "myhermes-audit-score-v1",
                            receipt.trace_id,
                            score.name,
                            score.evaluator_version,
                        )
                    )
                )
                metadata = {
                    key: value
                    for key, value in {
                        **score.metadata,
                        "source": score.source,
                        "evaluator_version": score.evaluator_version,
                        "trial_id": request.trial.trial_id,
                        "case_id": request.trial.case_id,
                        "adapter_version": LANGFUSE_ADAPTER_VERSION,
                    }.items()
                    if value is not None
                }
                comment = truncate_text_head_tail(
                    redact_text(score.comment, self._sensitive_values),
                    limit=500,
                )
                self._client.create_score(
                    name=score.name,
                    value=score.value,
                    dataset_run_id=receipt.dataset_run_id,
                    trace_id=receipt.trace_id,
                    score_id=score_id,
                    data_type="NUMERIC",
                    comment=comment,
                    metadata=metadata,
                )
                published_count += 1
        except AuditError:
            raise
        except Exception as exc:
            raise ScorePublishError(
                "Langfuse Score publication failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                trial_id=request.trial.trial_id,
                published_count=published_count,
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc
        return published_count

    def finish_experiment(
        self,
        identity: LangfuseExperimentIdentity,
        receipts: Sequence[LangfuseTrialPublishReceipt],
    ) -> LangfuseExperimentIdentity:
        self._validate_active_experiment(identity)
        remote_ids = {receipt.dataset_run_id for receipt in receipts}
        if len(remote_ids) > 1:
            raise ExperimentPublishError(
                "Langfuse returned inconsistent Dataset Run identities"
            )
        remote_run_id = next(iter(remote_ids), None)
        completed = identity.model_copy(update={"remote_run_id": remote_run_id})
        self._active_experiment = None
        return completed

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            raise ExperimentPublishError(
                "Langfuse flush failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self._client.shutdown()
        except Exception as exc:
            raise ExperimentPublishError(
                "Langfuse shutdown failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc

    def _validate_active_experiment(
        self,
        identity: LangfuseExperimentIdentity,
    ) -> None:
        active = self._active_experiment
        if active is None or active.identity != identity:
            raise ExperimentPublishError(
                "Langfuse operation is outside its active Experiment lifecycle"
            )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise LangfuseConfigError(
            f"missing required Langfuse environment variable {name}",
            field=name,
        )
    return value.strip()


def _timeout_from_environment() -> int:
    raw = os.environ.get("LANGFUSE_TIMEOUT", "5")
    try:
        value = int(raw)
    except ValueError as exc:
        raise LangfuseConfigError(
            "LANGFUSE_TIMEOUT must be a positive integer",
            field="LANGFUSE_TIMEOUT",
        ) from exc
    if value < 1 or value > 600:
        raise LangfuseConfigError(
            "LANGFUSE_TIMEOUT must be between 1 and 600 seconds",
            field="LANGFUSE_TIMEOUT",
        )
    return value


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LangfuseConfigError(
            "LANGFUSE_BASE_URL/HOST must be an HTTP(S) URL without credentials or query",
            field="LANGFUSE_BASE_URL",
        )


def _is_retryable(error: BaseException) -> bool:
    status = getattr(error, "status_code", None)
    if status in {408, 409, 429} or (type(status) is int and status >= 500):
        return True
    name = type(error).__name__.lower()
    return any(marker in name for marker in ("timeout", "connection", "ratelimit"))


__all__ = ("LANGFUSE_ADAPTER_VERSION", "LangfuseV4Adapter")
