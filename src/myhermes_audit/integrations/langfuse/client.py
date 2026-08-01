"""Delayed-import adapter for current public Langfuse v4 capabilities."""

from __future__ import annotations

import importlib
import os
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from myhermes_audit.contracts import (
    LangfuseCapabilityReport,
    LangfuseDatasetSyncPlan,
    LangfuseDatasetSyncResult,
    LangfuseExperimentIdentity,
    LangfusePublicationCounts,
    LangfusePublicationManifest,
    LangfusePublishError,
    LangfuseTrialPublishReceipt,
    PublicationItemStatus,
    PublicationManifestRef,
    PublicationManifestStatus,
    ReplayTrialPayload,
    ScorePublicationIdentity,
    ScorePublicationRecord,
    TrialPublicationRecord,
)
from myhermes_audit.errors import (
    AuditError,
    DatasetSyncError,
    ExperimentAssociationError,
    ExperimentInitializationError,
    ExperimentPublishError,
    ExperimentReplayError,
    LangfuseConfigError,
    LangfuseConnectionError,
    PublicationManifestError,
    PublicationStateError,
    ScoreIdempotencyError,
    ScoreIdentityError,
    ScorePublicationConflictError,
)
from myhermes_audit.integrations.langfuse.capability import (
    require_langfuse_capabilities,
)
from myhermes_audit.integrations.langfuse.manifest import (
    PublicationManifestStore,
    publication_manifest_path,
)
from myhermes_audit.integrations.langfuse.redaction import project_remote_content
from myhermes_audit.integrations.langfuse.score_mapper import (
    ScoreProjection,
    project_scores,
)
from myhermes_audit.integrations.langfuse.trace_mapper import (
    publish_replay_observations,
)
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
from myhermes_audit.serialization import canonical_sha256


LANGFUSE_ADAPTER_VERSION = "langfuse-v4-runner-replay-v3"


class LangfuseV4Adapter:
    """One parent-process client whose public results are immediately mapped locally."""

    def __init__(
        self,
        *,
        client: Any,
        propagate_attributes: Any,
        not_found_error: type[BaseException],
        sensitive_values: tuple[str, ...],
        capability_report: LangfuseCapabilityReport,
        report_path: Path | None,
    ) -> None:
        self._client = client
        self._propagate_attributes = propagate_attributes
        self._not_found_error = not_found_error
        self._sensitive_values = sensitive_values
        self._capability_report = capability_report
        self._report_path = report_path
        self._active_experiment: LangfuseExperimentRequest | None = None
        self._dataset_items: dict[str, Any] = {}
        self._manifest_store: PublicationManifestStore | None = None
        self._manifest: LangfusePublicationManifest | None = None
        self._remote_run_id: str | None = None
        self._remote_run_url: str | None = None
        self._skipped_score_count = 0
        self._conflicted_score_ids: set[str] = set()
        self._shutdown = False
        self._finished = False

    @classmethod
    def from_environment(
        cls,
        *,
        report_path: Path | None = None,
    ) -> "LangfuseV4Adapter":
        capability_report = require_langfuse_capabilities()
        langfuse = importlib.import_module("langfuse")

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
            not_found_error = langfuse.api.NotFoundError
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
            capability_report=capability_report,
            report_path=(
                None
                if report_path is None
                else Path(report_path).expanduser().resolve(strict=False)
            ),
        )

    @property
    def capability_report(self) -> LangfuseCapabilityReport:
        return self._capability_report

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
                    "historical Dataset Items are retained; P2.1 performs no destructive prune"
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
        if self._active_experiment is not None or self._finished:
            raise ExperimentInitializationError(
                "this adapter cannot begin another Langfuse Experiment"
            )
        if self._report_path is None:
            raise PublicationManifestError(
                "Experiment publication requires a local report path"
            )
        if request.identity.remote_run_id is not None or request.identity.url is not None:
            raise ExperimentInitializationError(
                "a new Experiment identity cannot contain remote publication fields"
            )
        run_name = _experiment_run_name(
            request.identity.experiment_name,
            request.identity.audit_run_id,
        )
        identity = request.identity.model_copy(update={"run_name": run_name})
        active_request = replace(request, identity=identity)
        try:
            dataset = self._client.get_dataset(identity.dataset_name)
            items = {
                str(item.id): item
                for item in list(dataset.items)
                if isinstance(getattr(item, "id", None), str)
            }
            if not items:
                raise ExperimentInitializationError(
                    "Langfuse Dataset contains no publishable items"
                )
        except AuditError:
            raise
        except Exception as exc:
            raise ExperimentInitializationError(
                "Langfuse Experiment preflight failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            ) from exc

        manifest_path = publication_manifest_path(
            self._report_path,
            identity.audit_run_id,
        )
        store = PublicationManifestStore(manifest_path)
        manifest = store.load_or_create(
            audit_run_id=identity.audit_run_id,
            experiment_name=identity.experiment_name,
            dataset_name=identity.dataset_name,
            score_submission_supported=(
                self._capability_report.score_submission_supported
            ),
            score_confirmation_supported=(
                self._capability_report.score_confirmation_supported
            ),
        )
        prior_run_id = manifest.remote_ids.get("dataset_run_id")
        if prior_run_id is not None and not isinstance(prior_run_id, str):
            raise PublicationManifestError(
                "publication Manifest contains an invalid Dataset Run identity"
            )
        prior_run_url = manifest.remote_ids.get("dataset_run_url")
        if prior_run_url is not None and not isinstance(prior_run_url, str):
            raise PublicationManifestError(
                "publication Manifest contains an invalid Dataset Run URL"
            )
        if isinstance(prior_run_url, str):
            parsed_prior_url = urlsplit(prior_run_url)
            if (
                parsed_prior_url.scheme not in {"http", "https"}
                or not parsed_prior_url.netloc
                or parsed_prior_url.username is not None
                or parsed_prior_url.password is not None
                or parsed_prior_url.query
                or parsed_prior_url.fragment
            ):
                raise PublicationManifestError(
                    "publication Manifest contains an unsafe Dataset Run URL"
                )
        self._dataset_items = items
        self._manifest_store = store
        self._manifest = manifest
        self._remote_run_id = prior_run_id
        self._remote_run_url = prior_run_url
        self._active_experiment = active_request
        return identity

    def publish_trial(
        self,
        request: LangfuseTrialRequest,
    ) -> LangfuseTrialPublishReceipt:
        self._validate_active_experiment(request.experiment)
        dataset_item_id = request.dataset_item.remote_item_id
        if not dataset_item_id:
            error = ExperimentAssociationError(
                "Trial publication requires a synchronized Dataset Item identity",
                trial_id=request.trial.trial_id,
            )
            self._record_trial_setup_error(error, request.trial.trial_id)
            raise error
        local_trace_id = _local_trace_id(request)
        publication_key = _trial_publication_key(request, dataset_item_id)
        content_fingerprint = _trial_content_fingerprint(request, dataset_item_id)
        prior = self._trial_record(request.trial.trial_id)
        if prior is not None:
            try:
                _validate_trial_record(
                    prior,
                    request,
                    dataset_item_id=dataset_item_id,
                    local_trace_id=local_trace_id,
                    publication_key=publication_key,
                    content_fingerprint=content_fingerprint,
                )
            except AuditError as error:
                self._record_trial_setup_error(
                    error,
                    request.trial.trial_id,
                    record=prior,
                )
                raise
            if prior.status is PublicationItemStatus.CONFIRMED:
                return _receipt_from_record(prior, self._remote_run_url)
        else:
            created_at = datetime.now(timezone.utc)
            prior = TrialPublicationRecord(
                publication_key=publication_key,
                audit_run_id=request.experiment.audit_run_id,
                trial_id=request.trial.trial_id,
                case_id=request.trial.case_id,
                dataset_item_id=dataset_item_id,
                local_trace_id=local_trace_id,
                content_fingerprint=content_fingerprint,
                created_at=created_at,
                updated_at=created_at,
                confirmation_supported=True,
            )
            self._write_manifest(
                _replace_trial_record(self._require_manifest(), prior)
            )

        dataset_item = self._dataset_items.get(dataset_item_id)
        if dataset_item is None:
            error = ExperimentAssociationError(
                "synchronized Dataset Item is absent from Experiment preflight",
                trial_id=request.trial.trial_id,
                dataset_item_id=dataset_item_id,
            )
            self._record_trial_setup_error(
                error,
                request.trial.trial_id,
                record=prior,
            )
            raise error
        try:
            payload = _replay_payload(
                request,
                dataset_item_id=dataset_item_id,
                sensitive_values=self._sensitive_values,
            )
        except Exception as exc:
            mapped = (
                exc
                if isinstance(exc, AuditError)
                else ExperimentReplayError(
                    "Trial result cannot be projected for Experiment replay",
                    trial_id=request.trial.trial_id,
                    exception_type=type(exc).__name__,
                )
            )
            self._record_trial_setup_error(
                mapped,
                request.trial.trial_id,
                record=prior,
            )
            raise mapped

        attempt_at = datetime.now(timezone.utc)
        publishing = prior.model_copy(
            update={
                "status": PublicationItemStatus.PUBLISHING,
                "attempt_count": prior.attempt_count + 1,
                "last_attempt_at": attempt_at,
                "updated_at": attempt_at,
                "confirmed_at": None,
                "error": None,
            }
        )
        manifest = _replace_trial_record(self._require_manifest(), publishing)
        self._write_manifest(
            _update_manifest(
                manifest,
                status=PublicationManifestStatus.PUBLISHING,
            )
        )

        remote_attempted = False
        runner_returned = False
        try:
            captured: dict[str, str] = {}

            def replay_task(*, item: Any, **_: Any) -> dict[str, Any]:
                item_id = getattr(item, "id", None)
                if item_id != dataset_item_id:
                    raise ExperimentReplayError(
                        "Experiment Runner supplied an unexpected Dataset Item",
                        trial_id=request.trial.trial_id,
                    )
                trace_id = self._client.get_current_trace_id()
                observation_id = self._client.get_current_observation_id()
                if not isinstance(trace_id, str) or not trace_id:
                    raise ExperimentReplayError(
                        "Experiment Runner did not expose a current Trace identity",
                        trial_id=request.trial.trial_id,
                    )
                if not isinstance(observation_id, str) or not observation_id:
                    raise ExperimentReplayError(
                        "Experiment Runner did not expose a current Observation identity",
                        trial_id=request.trial.trial_id,
                    )
                captured["trace_id"] = trace_id
                captured["observation_id"] = observation_id
                publish_replay_observations(
                    self._client,
                    self._propagate_attributes,
                    request,
                    sensitive_values=self._sensitive_values,
                )
                return payload.model_dump(mode="json", exclude={"schema_version"})

            active = self._require_active_experiment()
            remote_attempted = True
            result = self._client.run_experiment(
                name=active.identity.experiment_name,
                run_name=active.identity.run_name,
                description="my-hermes-audit post-hoc local-result replay",
                data=[dataset_item],
                task=replay_task,
                evaluators=[],
                run_evaluators=[],
                max_concurrency=1,
                metadata={
                    "audit_run_id": active.identity.audit_run_id,
                    "suite_id": request.suite_id,
                    "suite_sha256": request.suite_sha256,
                    "subject_commit": request.subject_commit,
                    "subject_dirty": str(request.subject_dirty).lower(),
                    "audit_commit": request.audit_commit,
                    "audit_version": request.audit_version,
                    "adapter_version": LANGFUSE_ADAPTER_VERSION,
                    "replay_only": "true",
                },
            )
            runner_returned = True
            receipt = self._map_experiment_result(
                result,
                request=request,
                dataset_item_id=dataset_item_id,
                captured=captured,
            )
            confirmed_at = datetime.now(timezone.utc)
            confirmed = publishing.model_copy(
                update={
                    "status": PublicationItemStatus.CONFIRMED,
                    "remote_trace_id": receipt.trace_id,
                    "remote_observation_id": receipt.observation_id,
                    "dataset_run_id": receipt.dataset_run_id,
                    "experiment_id": receipt.experiment_id,
                    "experiment_item_key": receipt.experiment_item_key,
                    "updated_at": confirmed_at,
                    "confirmed_at": confirmed_at,
                    "error": None,
                }
            )
            manifest = _replace_trial_record(self._require_manifest(), confirmed)
            remote_ids = dict(manifest.remote_ids)
            remote_ids.update(
                {
                    "dataset_run_id": receipt.dataset_run_id,
                    "experiment_id": receipt.experiment_id,
                    f"trial:{receipt.trial_id}:trace_id": receipt.trace_id,
                    f"trial:{receipt.trial_id}:observation_id": receipt.observation_id,
                }
            )
            if receipt.url is not None:
                remote_ids["dataset_run_url"] = receipt.url
            stable_timestamps = dict(manifest.stable_timestamps)
            stable_timestamps[f"trial:{receipt.trial_id}"] = (
                request.trial.finished_at or manifest.created_at
            )
            self._write_manifest(
                _update_manifest(
                    manifest,
                    remote_ids=remote_ids,
                    stable_timestamps=stable_timestamps,
                    status=PublicationManifestStatus.PUBLISHING,
                    last_error=_last_unresolved_error(manifest),
                )
            )
            return receipt
        except Exception as exc:
            mapped = self._map_trial_exception(exc, request.trial.trial_id)
            error = _publish_error(
                phase="trial",
                error=mapped,
                trial_id=request.trial.trial_id,
            )
            failed_record = publishing.model_copy(
                update={
                    "status": (
                        PublicationItemStatus.UNCERTAIN
                        if (
                            runner_returned
                            or bool(captured)
                            or (remote_attempted and _is_retryable(exc))
                        )
                        else PublicationItemStatus.FAILED
                    ),
                    "updated_at": datetime.now(timezone.utc),
                    "error": error,
                }
            )
            manifest = _replace_trial_record(self._require_manifest(), failed_record)
            self._write_manifest(
                _update_manifest(
                    manifest,
                    status=_error_manifest_status(manifest),
                    last_error=error,
                )
            )
            raise mapped

    def publish_scores(
        self,
        request: LangfuseTrialRequest,
        receipt: LangfuseTrialPublishReceipt,
    ) -> int:
        self._validate_active_experiment(request.experiment)
        if receipt.trial_id != request.trial.trial_id:
            raise ScoreIdentityError(
                "Score publication receipt does not match the Trial",
                trial_id=request.trial.trial_id,
            )
        trial_record = self._trial_record(request.trial.trial_id)
        if (
            trial_record is None
            or trial_record.status is not PublicationItemStatus.CONFIRMED
            or trial_record.remote_trace_id != receipt.trace_id
        ):
            raise PublicationStateError(
                "Score publication requires a confirmed Trial association",
                trial_id=request.trial.trial_id,
            )
        projections = project_scores(request.trial)
        prepared: list[tuple[ScoreProjection, ScorePublicationRecord]] = []
        for score in projections:
            record = self._prepare_score_record(request, receipt, score)
            prepared.append((score, record))
        published_count = 0
        first_error: Exception | None = None
        for score, record in prepared:
            if record.status is PublicationItemStatus.CONFIRMED:
                self._skipped_score_count += 1
                continue
            try:
                self._publish_one_score(request, receipt, score, record)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            else:
                published_count += 1
        if first_error is not None:
            if isinstance(first_error, AuditError):
                first_error.details.setdefault("published_count", published_count)
                first_error.details.setdefault(
                    "skipped_count", self._skipped_score_count
                )
            raise first_error
        return published_count

    def finish_experiment(
        self,
        identity: LangfuseExperimentIdentity,
        receipts: Sequence[LangfuseTrialPublishReceipt],
    ) -> LangfuseExperimentIdentity:
        self._validate_active_experiment(identity)
        manifest = self._require_manifest()
        confirmed_trials = [
            item
            for item in manifest.trial_publications
            if item.status is PublicationItemStatus.CONFIRMED
        ]
        remote_ids = {
            item.dataset_run_id
            for item in confirmed_trials
            if item.dataset_run_id is not None
        }
        receipt_ids = {item.dataset_run_id for item in receipts}
        if len(remote_ids) > 1 or receipt_ids != remote_ids:
            error = ExperimentAssociationError(
                "Langfuse returned inconsistent Experiment identities"
            )
            self._finalize_manifest_error(error)
            self._active_experiment = None
            self._finished = True
            raise error
        remote_run_id = next(iter(remote_ids), None)
        if remote_run_id is None:
            error = ExperimentAssociationError(
                "Langfuse Experiment has no confirmed associated items"
            )
            self._finalize_manifest_error(error)
            self._active_experiment = None
            self._finished = True
            raise error
        all_confirmed = (
            len(confirmed_trials) == len(manifest.trial_publications)
            and all(
                item.status is PublicationItemStatus.CONFIRMED
                for item in manifest.score_publications
            )
            and manifest.last_error is None
        )
        final_status = (
            PublicationManifestStatus.PUBLISHED
            if all_confirmed
            else PublicationManifestStatus.PARTIALLY_PUBLISHED
        )
        self._write_manifest(
            _update_manifest(
                manifest,
                status=final_status,
                last_error=None if all_confirmed else manifest.last_error,
            )
        )
        completed = identity.model_copy(
            update={
                "remote_run_id": remote_run_id,
                "url": self._remote_run_url,
            }
        )
        self._active_experiment = None
        self._finished = True
        return completed

    def publication_manifest(self) -> LangfusePublicationManifest:
        return self._require_manifest()

    def publication_manifest_ref(self) -> PublicationManifestRef:
        store = self._require_manifest_store()
        return store.reference(self._require_manifest())

    def publication_counts(self) -> LangfusePublicationCounts:
        manifest = self._require_manifest()
        published_trials = sum(
            item.status is PublicationItemStatus.CONFIRMED
            for item in manifest.trial_publications
        )
        recorded_score_ids = {
            item.identity.score_id for item in manifest.score_publications
        }
        unrecorded_conflicts = sum(
            item not in recorded_score_ids for item in self._conflicted_score_ids
        )
        return LangfusePublicationCounts(
            published_trial_count=published_trials,
            associated_experiment_item_count=published_trials,
            published_score_count=sum(
                item.status is PublicationItemStatus.CONFIRMED
                for item in manifest.score_publications
            ),
            skipped_score_count=self._skipped_score_count,
            uncertain_score_count=sum(
                item.status is PublicationItemStatus.UNCERTAIN
                for item in manifest.score_publications
            ),
            failed_score_count=sum(
                item.status is PublicationItemStatus.FAILED
                for item in manifest.score_publications
            )
            + unrecorded_conflicts,
        )

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            error = ExperimentPublishError(
                "Langfuse flush failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            )
            self._record_lifecycle_error("flush", error)
            raise error from exc

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self._client.shutdown()
        except Exception as exc:
            error = ExperimentPublishError(
                "Langfuse shutdown failed: "
                + sanitize_external_error(exc, self._sensitive_values),
                retryable=_is_retryable(exc),
                exception_type=type(exc).__name__,
            )
            self._record_lifecycle_error("shutdown", error)
            raise error from exc

    def _map_experiment_result(
        self,
        result: Any,
        *,
        request: LangfuseTrialRequest,
        dataset_item_id: str,
        captured: dict[str, str],
    ) -> LangfuseTrialPublishReceipt:
        active = self._require_active_experiment()
        result_name = getattr(result, "name", None)
        result_run_name = getattr(result, "run_name", None)
        if (
            result_name != active.identity.experiment_name
            or result_run_name != active.identity.run_name
        ):
            raise ExperimentAssociationError(
                "Experiment Runner returned an unexpected name or run name",
                trial_id=request.trial.trial_id,
            )
        item_results = getattr(result, "item_results", None)
        if not isinstance(item_results, list) or len(item_results) != 1:
            raise ExperimentAssociationError(
                "Experiment Runner did not return exactly one replay result",
                trial_id=request.trial.trial_id,
            )
        item_result = item_results[0]
        result_item = getattr(item_result, "item", None)
        if getattr(result_item, "id", None) != dataset_item_id:
            raise ExperimentAssociationError(
                "Experiment Runner result does not map to the requested Dataset Item",
                trial_id=request.trial.trial_id,
            )
        trace_id = getattr(item_result, "trace_id", None)
        dataset_run_id = getattr(item_result, "dataset_run_id", None)
        top_level_run_id = getattr(result, "dataset_run_id", None)
        experiment_id = getattr(result, "experiment_id", None)
        if not isinstance(trace_id, str) or not trace_id:
            raise ExperimentAssociationError(
                "Experiment Runner returned no Trace identity",
                trial_id=request.trial.trial_id,
            )
        if (
            not isinstance(dataset_run_id, str)
            or not dataset_run_id
            or dataset_run_id != top_level_run_id
            or experiment_id != dataset_run_id
        ):
            raise ExperimentAssociationError(
                "Experiment Runner returned no consistent Experiment identity",
                trial_id=request.trial.trial_id,
            )
        if captured.get("trace_id") != trace_id:
            raise ExperimentReplayError(
                "replay task Trace identity differs from Experiment result",
                trial_id=request.trial.trial_id,
            )
        observation_id = captured.get("observation_id")
        if not observation_id:
            raise ExperimentReplayError(
                "replay task Observation identity was not captured",
                trial_id=request.trial.trial_id,
            )
        if self._remote_run_id is not None and self._remote_run_id != dataset_run_id:
            raise ExperimentAssociationError(
                "Experiment Runner split one Audit run across remote runs",
                trial_id=request.trial.trial_id,
            )
        url = getattr(result, "dataset_run_url", None)
        if url is not None and not isinstance(url, str):
            raise ExperimentAssociationError(
                "Experiment Runner returned an invalid Dataset Run URL",
                trial_id=request.trial.trial_id,
            )
        if url is not None:
            parsed_url = urlsplit(url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ExperimentAssociationError(
                    "Experiment Runner returned an unsafe Dataset Run URL",
                    trial_id=request.trial.trial_id,
                )
        if self._remote_run_url is not None and url not in {None, self._remote_run_url}:
            raise ExperimentAssociationError(
                "Experiment Runner returned inconsistent Dataset Run URLs",
                trial_id=request.trial.trial_id,
            )
        self._remote_run_id = dataset_run_id
        if url is not None:
            self._remote_run_url = url
        return LangfuseTrialPublishReceipt(
            trial_id=request.trial.trial_id,
            dataset_item_id=dataset_item_id,
            experiment_item_key=dataset_item_id,
            trace_id=trace_id,
            observation_id=observation_id,
            dataset_run_id=dataset_run_id,
            experiment_id=experiment_id,
            url=url,
        )

    def _prepare_score_record(
        self,
        request: LangfuseTrialRequest,
        receipt: LangfuseTrialPublishReceipt,
        score: ScoreProjection,
    ) -> ScorePublicationRecord:
        manifest = self._require_manifest()
        score_id = _score_id(
            trace_id=receipt.trace_id,
            score_name=score.name,
            evaluator_version=score.evaluator_version,
            trial_id=request.trial.trial_id,
        )
        timestamp_key = f"score:{score_id}"
        stable_timestamp = manifest.stable_timestamps.get(timestamp_key)
        if stable_timestamp is None:
            stable_timestamp = request.trial.finished_at or datetime.now(timezone.utc)
        value_hash = canonical_sha256(
            {
                "name": score.name,
                "value": score.value,
                "source": score.source,
                "evaluator_version": score.evaluator_version,
                "comment": _safe_score_comment(
                    score.comment,
                    sensitive_values=self._sensitive_values,
                ),
                "metadata": score.metadata,
            }
        )
        identity = ScorePublicationIdentity(
            score_id=score_id,
            trace_id=receipt.trace_id,
            score_name=score.name,
            evaluator_version=score.evaluator_version,
            trial_id=request.trial.trial_id,
            case_id=request.trial.case_id,
            stable_timestamp=stable_timestamp,
            value_hash=value_hash,
        )
        publication_key = _score_publication_key(
            request,
            receipt,
            score_id=score_id,
        )
        existing = self._score_record(score_id)
        if existing is not None:
            if (
                existing.publication_key != publication_key
                or existing.audit_run_id != request.experiment.audit_run_id
                or existing.dataset_item_id != receipt.dataset_item_id
                or existing.dataset_run_id != receipt.dataset_run_id
                or existing.experiment_id != receipt.experiment_id
                or _identity_key(existing.identity) != _identity_key(identity)
            ):
                error = ScoreIdentityError(
                    "stable Score ID maps to different identity fields",
                    score_id=score_id,
                    trial_id=request.trial.trial_id,
                )
                self._record_score_conflict(error, request.trial.trial_id)
                raise error
            if existing.identity.value_hash != value_hash:
                error = ScorePublicationConflictError(
                    "Score identity already exists with a different value hash; "
                    "increment evaluator_version before publishing",
                    score_id=score_id,
                    trial_id=request.trial.trial_id,
                    existing_value_hash=existing.identity.value_hash,
                    incoming_value_hash=value_hash,
                )
                self._record_score_conflict(error, request.trial.trial_id)
                raise error
            if existing.content_fingerprint != value_hash:
                error = ScorePublicationConflictError(
                    "Score publication fingerprint conflicts with its stable identity",
                    score_id=score_id,
                    trial_id=request.trial.trial_id,
                )
                self._record_score_conflict(error, request.trial.trial_id)
                raise error
            confirmation_supported = (
                self._capability_report.score_confirmation_supported
            )
            if existing.confirmation_supported != confirmation_supported:
                existing = existing.model_copy(
                    update={
                        "confirmation_supported": confirmation_supported,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                self._write_manifest(
                    _replace_score_record(self._require_manifest(), existing)
                )
            return existing

        stable_timestamps = dict(manifest.stable_timestamps)
        stable_timestamps[timestamp_key] = stable_timestamp
        created_at = datetime.now(timezone.utc)
        record = ScorePublicationRecord(
            publication_key=publication_key,
            audit_run_id=request.experiment.audit_run_id,
            dataset_item_id=receipt.dataset_item_id,
            dataset_run_id=receipt.dataset_run_id,
            experiment_id=receipt.experiment_id,
            content_fingerprint=value_hash,
            created_at=created_at,
            updated_at=created_at,
            confirmation_supported=(
                self._capability_report.score_confirmation_supported
            ),
            identity=identity,
        )
        manifest = _replace_score_record(manifest, record)
        self._write_manifest(
            _update_manifest(
                manifest,
                stable_timestamps=stable_timestamps,
            )
        )
        return record

    def _publish_one_score(
        self,
        request: LangfuseTrialRequest,
        receipt: LangfuseTrialPublishReceipt,
        score: ScoreProjection,
        record: ScorePublicationRecord,
    ) -> None:
        attempt_at = datetime.now(timezone.utc)
        publishing = record.model_copy(
            update={
                "status": PublicationItemStatus.PUBLISHING,
                "attempt_count": record.attempt_count + 1,
                "last_attempt_at": attempt_at,
                "updated_at": attempt_at,
                "confirmed_at": None,
                "error": None,
            }
        )
        manifest = _replace_score_record(self._require_manifest(), publishing)
        self._write_manifest(
            _update_manifest(
                manifest,
                status=PublicationManifestStatus.PUBLISHING,
            )
        )
        submission_returned = False
        recorded_confirmation_error: AuditError | None = None
        try:
            metadata = {
                key: value
                for key, value in {
                    **score.metadata,
                    "source": score.source,
                    "evaluator_version": score.evaluator_version,
                    "trial_id": request.trial.trial_id,
                    "case_id": request.trial.case_id,
                    "adapter_version": LANGFUSE_ADAPTER_VERSION,
                    "value_hash": record.identity.value_hash,
                }.items()
                if value is not None
            }
            comment = _safe_score_comment(
                score.comment,
                sensitive_values=self._sensitive_values,
            )
            self._client.create_score(
                name=score.name,
                value=score.value,
                dataset_run_id=receipt.dataset_run_id,
                trace_id=receipt.trace_id,
                score_id=record.identity.score_id,
                data_type="NUMERIC",
                comment=comment,
                metadata=metadata,
                timestamp=record.identity.stable_timestamp,
            )
            submission_returned = True
            self._client.flush()
            if self._capability_report.score_confirmation_supported:
                recorded_confirmation_error = ScoreIdempotencyError(
                    "Langfuse Score confirmation is advertised but no supported "
                    "high-level verifier is configured",
                    score_id=record.identity.score_id,
                    trial_id=request.trial.trial_id,
                    retryable=False,
                    confirmation_supported=True,
                )
            else:
                recorded_confirmation_error = ScoreIdempotencyError(
                    "Langfuse Score was submitted but the supported high-level SDK "
                    "cannot reliably confirm remote persistence",
                    score_id=record.identity.score_id,
                    trial_id=request.trial.trial_id,
                    retryable=True,
                    confirmation_supported=False,
                )
            error = _publish_error(
                phase="score_confirmation",
                error=recorded_confirmation_error,
                trial_id=request.trial.trial_id,
            )
            uncertain = publishing.model_copy(
                update={
                    "status": PublicationItemStatus.UNCERTAIN,
                    "remote_id": None,
                    "updated_at": datetime.now(timezone.utc),
                    "confirmed_at": None,
                    "error": error,
                }
            )
            manifest = _replace_score_record(self._require_manifest(), uncertain)
            self._write_manifest(
                _update_manifest(
                    manifest,
                    status=_error_manifest_status(manifest),
                    last_error=error,
                )
            )
            raise recorded_confirmation_error
        except Exception as exc:
            if exc is recorded_confirmation_error:
                raise
            mapped = (
                exc
                if isinstance(exc, AuditError)
                else ScoreIdempotencyError(
                    "Langfuse Score delivery could not be confirmed: "
                    + sanitize_external_error(exc, self._sensitive_values),
                    score_id=record.identity.score_id,
                    trial_id=request.trial.trial_id,
                    retryable=_is_retryable(exc),
                    exception_type=type(exc).__name__,
                )
            )
            error = _publish_error(
                phase="scores",
                error=mapped,
                trial_id=request.trial.trial_id,
            )
            failed = publishing.model_copy(
                update={
                    "status": (
                        PublicationItemStatus.UNCERTAIN
                        if submission_returned or _is_retryable(exc)
                        else PublicationItemStatus.FAILED
                    ),
                    "updated_at": datetime.now(timezone.utc),
                    "error": error,
                }
            )
            manifest = _replace_score_record(self._require_manifest(), failed)
            self._write_manifest(
                _update_manifest(
                    manifest,
                    status=_error_manifest_status(manifest),
                    last_error=error,
                )
            )
            raise mapped

    def _map_trial_exception(
        self,
        error: Exception,
        trial_id: str,
    ) -> AuditError:
        if isinstance(error, AuditError):
            return error
        return ExperimentAssociationError(
            "Langfuse Experiment replay/association failed: "
            + sanitize_external_error(error, self._sensitive_values),
            trial_id=trial_id,
            retryable=_is_retryable(error),
            exception_type=type(error).__name__,
        )

    def _record_score_conflict(self, error: AuditError, trial_id: str) -> None:
        score_id = error.details.get("score_id")
        if isinstance(score_id, str) and score_id:
            self._conflicted_score_ids.add(score_id)
        else:
            self._conflicted_score_ids.add(f"trial:{trial_id}:{error.code}")
        manifest = self._require_manifest()
        publish_error = _publish_error(
            phase="score_identity",
            error=error,
            trial_id=trial_id,
        )
        if isinstance(score_id, str) and score_id:
            record = self._score_record(score_id)
            if record is not None:
                failed = record.model_copy(
                    update={
                        "status": PublicationItemStatus.FAILED,
                        "updated_at": datetime.now(timezone.utc),
                        "confirmed_at": None,
                        "error": publish_error,
                    }
                )
                manifest = _replace_score_record(manifest, failed)
        self._write_manifest(
            _update_manifest(
                manifest,
                status=_error_manifest_status(manifest),
                last_error=publish_error,
            )
        )

    def _record_trial_setup_error(
        self,
        error: AuditError,
        trial_id: str,
        *,
        record: TrialPublicationRecord | None = None,
    ) -> None:
        publish_error = _publish_error(
            phase="trial",
            error=error,
            trial_id=trial_id,
        )
        manifest = self._require_manifest()
        if record is not None:
            failed_at = datetime.now(timezone.utc)
            failed = record.model_copy(
                update={
                    "status": PublicationItemStatus.FAILED,
                    "updated_at": failed_at,
                    "confirmed_at": None,
                    "error": publish_error,
                }
            )
            manifest = _replace_trial_record(manifest, failed)
        self._write_manifest(
            _update_manifest(
                manifest,
                status=_error_manifest_status(manifest),
                last_error=publish_error,
            )
        )

    def _finalize_manifest_error(self, error: AuditError) -> None:
        manifest = self._require_manifest()
        publish_error = _publish_error(phase="experiment", error=error)
        self._write_manifest(
            _update_manifest(
                manifest,
                status=_error_manifest_status(manifest),
                last_error=publish_error,
            )
        )

    def _record_lifecycle_error(self, phase: str, error: AuditError) -> None:
        if self._manifest is None or self._manifest_store is None:
            return
        publish_error = _publish_error(phase=phase, error=error)
        self._write_manifest(
            _update_manifest(
                self._manifest,
                status=_error_manifest_status(self._manifest),
                last_error=publish_error,
            )
        )

    def _validate_active_experiment(
        self,
        identity: LangfuseExperimentIdentity,
    ) -> None:
        active = self._active_experiment
        if active is None or active.identity != identity or self._finished:
            raise PublicationStateError(
                "Langfuse operation is outside its active Experiment lifecycle"
            )

    def _require_active_experiment(self) -> LangfuseExperimentRequest:
        active = self._active_experiment
        if active is None:
            raise PublicationStateError("there is no active Langfuse Experiment")
        return active

    def _require_manifest(self) -> LangfusePublicationManifest:
        if self._manifest is None:
            raise PublicationManifestError("publication Manifest is not initialized")
        return self._manifest

    def _require_manifest_store(self) -> PublicationManifestStore:
        if self._manifest_store is None:
            raise PublicationManifestError("publication Manifest store is not initialized")
        return self._manifest_store

    def _write_manifest(
        self,
        manifest: LangfusePublicationManifest,
    ) -> LangfusePublicationManifest:
        updated = self._require_manifest_store().write(manifest)
        self._manifest = updated
        return updated

    def _trial_record(self, trial_id: str) -> TrialPublicationRecord | None:
        manifest = self._require_manifest()
        return next(
            (item for item in manifest.trial_publications if item.trial_id == trial_id),
            None,
        )

    def _score_record(self, score_id: str) -> ScorePublicationRecord | None:
        manifest = self._require_manifest()
        return next(
            (
                item
                for item in manifest.score_publications
                if item.identity.score_id == score_id
            ),
            None,
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


def _experiment_run_name(experiment_name: str, audit_run_id: str) -> str:
    return f"{experiment_name}::{audit_run_id}"


def _replay_payload(
    request: LangfuseTrialRequest,
    *,
    dataset_item_id: str,
    sensitive_values: tuple[str, ...],
) -> ReplayTrialPayload:
    trial = request.trial
    metric_summary = {
        metric.metric_name: {
            "status": metric.status.value,
            "value": metric.value,
            "passed": metric.passed,
            "evaluator_version": metric.evaluator_version,
        }
        for metric in trial.metrics
    }
    artifact_summary = [
        {
            "artifact_id": item.artifact_id,
            "kind": item.kind,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in trial.artifacts
    ]
    return ReplayTrialPayload(
        audit_run_id=request.experiment.audit_run_id,
        trial_id=trial.trial_id,
        case_id=trial.case_id,
        dataset_item_id=dataset_item_id,
        final_output=project_remote_content(
            trial.final_output,
            classification=request.data_classification,
            no_content=request.no_content,
            sensitive_values=sensitive_values,
        ),
        runtime_status=trial.status.value,
        safe_metric_summary=project_remote_content(
            metric_summary,
            classification=request.data_classification,
            no_content=request.no_content,
            sensitive_values=sensitive_values,
        ),
        local_trace_id=_local_trace_id(request),
        artifact_summary=artifact_summary,
    )


def _local_trace_id(request: LangfuseTrialRequest) -> str:
    return sha256(
        (
            f"{request.experiment.audit_run_id}\x1f{request.trial.trial_id}"
        ).encode("utf-8")
    ).hexdigest()[:32]


def _trial_publication_key(
    request: LangfuseTrialRequest,
    dataset_item_id: str,
) -> str:
    return canonical_sha256(
        {
            "audit_run_id": request.experiment.audit_run_id,
            "trial_id": request.trial.trial_id,
            "dataset_item_id": dataset_item_id,
            "experiment_name": request.experiment.experiment_name,
        }
    )


def _trial_content_fingerprint(
    request: LangfuseTrialRequest,
    dataset_item_id: str,
) -> str:
    return canonical_sha256(
        {
            "trial": request.trial.model_dump(mode="json"),
            "dataset_item_id": dataset_item_id,
            "data_classification": request.data_classification.value,
            "no_content": request.no_content,
            "adapter_version": LANGFUSE_ADAPTER_VERSION,
        }
    )


def _score_id(
    *,
    trace_id: str,
    score_name: str,
    evaluator_version: str,
    trial_id: str,
) -> str:
    stable_input = "\x1f".join(
        (trace_id, score_name, evaluator_version, trial_id)
    )
    return sha256(stable_input.encode("utf-8")).hexdigest()


def _score_publication_key(
    request: LangfuseTrialRequest,
    receipt: LangfuseTrialPublishReceipt,
    *,
    score_id: str,
) -> str:
    return canonical_sha256(
        {
            "audit_run_id": request.experiment.audit_run_id,
            "trial_id": request.trial.trial_id,
            "dataset_item_id": receipt.dataset_item_id,
            "dataset_run_id": receipt.dataset_run_id,
            "experiment_id": receipt.experiment_id,
            "score_id": score_id,
        }
    )


def _safe_score_comment(
    comment: str,
    *,
    sensitive_values: tuple[str, ...],
) -> str:
    return truncate_text_head_tail(
        redact_text(comment, sensitive_values),
        limit=500,
    )


def _identity_key(identity: ScorePublicationIdentity) -> tuple[str, ...]:
    return (
        identity.score_id,
        identity.trace_id,
        identity.score_name,
        identity.evaluator_version,
        identity.trial_id,
        identity.case_id,
        identity.stable_timestamp.isoformat(),
    )


def _validate_trial_record(
    record: TrialPublicationRecord,
    request: LangfuseTrialRequest,
    *,
    dataset_item_id: str,
    local_trace_id: str,
    publication_key: str,
    content_fingerprint: str,
) -> None:
    if (
        record.publication_key != publication_key
        or record.audit_run_id != request.experiment.audit_run_id
        or record.case_id != request.trial.case_id
        or record.dataset_item_id != dataset_item_id
        or record.local_trace_id != local_trace_id
        or record.content_fingerprint != content_fingerprint
    ):
        raise PublicationStateError(
            "Trial publication state conflicts with the replay payload",
            trial_id=request.trial.trial_id,
        )


def _receipt_from_record(
    record: TrialPublicationRecord,
    url: str | None,
) -> LangfuseTrialPublishReceipt:
    if any(
        value is None
        for value in (
            record.remote_trace_id,
            record.remote_observation_id,
            record.dataset_run_id,
            record.experiment_id,
            record.experiment_item_key,
        )
    ):
        raise PublicationStateError(
            "confirmed Trial record is missing remote identities",
            trial_id=record.trial_id,
        )
    return LangfuseTrialPublishReceipt(
        trial_id=record.trial_id,
        dataset_item_id=record.dataset_item_id,
        experiment_item_key=record.experiment_item_key,
        trace_id=record.remote_trace_id,
        observation_id=record.remote_observation_id,
        dataset_run_id=record.dataset_run_id,
        experiment_id=record.experiment_id,
        url=url,
    )


def _replace_trial_record(
    manifest: LangfusePublicationManifest,
    record: TrialPublicationRecord,
) -> LangfusePublicationManifest:
    records = [
        record if item.trial_id == record.trial_id else item
        for item in manifest.trial_publications
    ]
    if not any(item.trial_id == record.trial_id for item in manifest.trial_publications):
        records.append(record)
    records.sort(key=lambda item: item.trial_id)
    return _update_manifest(manifest, trial_publications=records)


def _replace_score_record(
    manifest: LangfusePublicationManifest,
    record: ScorePublicationRecord,
) -> LangfusePublicationManifest:
    records = [
        record if item.identity.score_id == record.identity.score_id else item
        for item in manifest.score_publications
    ]
    if not any(
        item.identity.score_id == record.identity.score_id
        for item in manifest.score_publications
    ):
        records.append(record)
    records.sort(key=lambda item: item.identity.score_id)
    return _update_manifest(manifest, score_publications=records)


def _update_manifest(
    manifest: LangfusePublicationManifest,
    **updates: Any,
) -> LangfusePublicationManifest:
    payload = manifest.model_dump(mode="python")
    payload.update(updates)
    return LangfusePublicationManifest.model_validate(payload)


def _error_manifest_status(
    manifest: LangfusePublicationManifest,
) -> PublicationManifestStatus:
    has_confirmed = any(
        item.status is PublicationItemStatus.CONFIRMED
        for item in (*manifest.trial_publications, *manifest.score_publications)
    )
    return (
        PublicationManifestStatus.PARTIALLY_PUBLISHED
        if has_confirmed
        else PublicationManifestStatus.FAILED
    )


def _last_unresolved_error(
    manifest: LangfusePublicationManifest,
) -> LangfusePublishError | None:
    unresolved = [
        item
        for item in (*manifest.trial_publications, *manifest.score_publications)
        if item.status in {
            PublicationItemStatus.UNCERTAIN,
            PublicationItemStatus.FAILED,
        }
        and item.error is not None
    ]
    if unresolved:
        latest = max(
            unresolved,
            key=lambda item: item.last_attempt_at or manifest.created_at,
        )
        return latest.error
    if (
        manifest.last_error is not None
        and manifest.last_error.phase
        in {"score_identity", "experiment", "flush", "shutdown"}
    ):
        return manifest.last_error
    if manifest.last_error is not None and manifest.last_error.phase == "trial":
        failed_trial_id = manifest.last_error.trial_id
        if failed_trial_id is None or not any(
            item.trial_id == failed_trial_id
            and item.status is PublicationItemStatus.CONFIRMED
            for item in manifest.trial_publications
        ):
            return manifest.last_error
    return None


def _publish_error(
    *,
    phase: str,
    error: AuditError,
    trial_id: str | None = None,
) -> LangfusePublishError:
    metadata = {
        key: value
        for key, value in error.details.items()
        if key in {"score_id", "exception_type", "published_count"}
        and isinstance(value, (str, int, float, bool))
    }
    return LangfusePublishError(
        phase=phase,
        error_type=error.code,
        message=error.message,
        trial_id=trial_id,
        retryable=error.details.get("retryable") is True,
        metadata=metadata,
    )


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, AuditError) and error.details.get("retryable") is True:
        return True
    status = getattr(error, "status_code", None)
    if status in {408, 409, 429} or (type(status) is int and status >= 500):
        return True
    name = type(error).__name__.lower()
    return any(marker in name for marker in ("timeout", "connection", "ratelimit"))


__all__ = ("LANGFUSE_ADAPTER_VERSION", "LangfuseV4Adapter")
