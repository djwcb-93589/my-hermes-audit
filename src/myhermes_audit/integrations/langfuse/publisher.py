"""Post-process a persisted local Audit result through the Langfuse adapter."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from myhermes_audit.contracts import (
    AuditCase,
    AuditRunResult,
    AuditSuite,
    DataClassification,
    DatasetSyncPublicationStatus,
    ExperimentPublicationStatus,
    ExperimentStrategy,
    LangfuseDatasetIdentity,
    LangfuseDatasetItemIdentity,
    LangfuseDatasetSyncResult,
    LangfuseExperimentIdentity,
    LangfusePublicationCounts,
    LangfusePublicationManifest,
    LangfusePublishError,
    LangfusePublishResult,
    LangfusePublishStatus,
    LangfuseTrialPublishReceipt,
    PublicationItemStatus,
    PublicationManifestRef,
    PublicationManifestStatus,
)
from myhermes_audit.contracts.data import classification_from_metadata
from myhermes_audit.errors import AuditError, PublicationStateError
from myhermes_audit.integrations.langfuse.client import LangfuseV4Adapter
from myhermes_audit.integrations.langfuse.dataset_sync import (
    build_dataset_sync_plan,
)
from myhermes_audit.integrations.langfuse.manifest import (
    PublicationManifestStore,
    publication_manifest_path,
)
from myhermes_audit.ports.langfuse import (
    LangfuseExperimentRequest,
    LangfuseTrialRequest,
)
from myhermes_audit.serialization import canonical_sha256


_T = TypeVar("_T")


class _PublicationAbort(Exception):
    def __init__(self, phase: str, error: Exception) -> None:
        super().__init__(phase)
        self.phase = phase
        self.error = error


def publish_audit_result(
    *,
    suite: AuditSuite,
    cases: Sequence[AuditCase],
    result: AuditRunResult,
    report_path: Path,
    dataset_name: str,
    experiment_name: str,
    no_content: bool,
) -> AuditRunResult:
    """Publish only existing local facts and always return a reportable result."""

    report = Path(report_path).expanduser().resolve(strict=False)
    dataset_identity = LangfuseDatasetIdentity(
        dataset_name=dataset_name,
        suite_id=suite.suite_id,
        suite_sha256=canonical_sha256(suite),
    )
    dataset_name = dataset_identity.dataset_name
    experiment_identity = LangfuseExperimentIdentity(
        experiment_name=experiment_name,
        audit_run_id=result.run_id,
        dataset_name=dataset_name,
    )
    experiment_name = experiment_identity.experiment_name
    errors: list[LangfusePublishError] = []
    warnings: list[str] = []
    receipts: list[LangfuseTrialPublishReceipt] = []
    adapter: LangfuseV4Adapter | None = None
    dataset_result: LangfuseDatasetSyncResult | None = None
    dataset_status = DatasetSyncPublicationStatus.FAILED
    manifest_store = PublicationManifestStore(
        publication_manifest_path(report, result.run_id)
    )
    manifest: LangfusePublicationManifest | None = None

    try:
        if not report.is_file():
            raise PublicationStateError(
                "Langfuse post-processing requires a persisted local JSON report"
            )
        manifest = manifest_store.load_or_create(
            audit_run_id=result.run_id,
            experiment_name=experiment_name,
            dataset_name=dataset_name,
        )
        _validate_local_publication_input(suite, cases, result)
        plan = build_dataset_sync_plan(
            suite,
            dataset_name=dataset_name,
            dry_run=False,
            no_content=no_content,
        )
        adapter = _stage(
            "initialization",
            lambda: LangfuseV4Adapter.from_environment(report_path=report),
        )
        manifest = manifest_store.load_or_create(
            audit_run_id=result.run_id,
            experiment_name=experiment_name,
            dataset_name=dataset_name,
            score_submission_supported=(
                adapter.capability_report.score_submission_supported
            ),
            score_confirmation_supported=(
                adapter.capability_report.score_confirmation_supported
            ),
        )
        warnings.extend(adapter.capability_report.warnings)
        _stage("connection", adapter.check_connection)
        dataset_result = _stage("dataset_sync", lambda: adapter.sync_dataset(plan))
        dataset_status = DatasetSyncPublicationStatus.PUBLISHED
        dataset_items = _stage(
            "dataset_preflight",
            lambda: _preflight_dataset(suite, cases, result, dataset_result),
        )
        experiment_identity = _stage(
            "experiment_initialization",
            lambda: adapter.begin_experiment(
                LangfuseExperimentRequest(
                    identity=experiment_identity,
                    suite_id=suite.suite_id,
                    suite_sha256=result.audit_fingerprint.suite_sha256,
                    subject_commit=result.subject_fingerprint.git_commit,
                    audit_commit=(
                        result.audit_fingerprint.audit_commit or "unavailable"
                    ),
                    audit_version=result.audit_fingerprint.audit_version,
                )
            ),
        )
        case_by_id = {case.case_id: case for case in cases}
        for trial in result.trials:
            case = case_by_id[trial.case_id]
            request = LangfuseTrialRequest(
                experiment=experiment_identity,
                dataset_item=dataset_items[case.case_id],
                suite_id=suite.suite_id,
                suite_sha256=result.audit_fingerprint.suite_sha256,
                subject_commit=result.subject_fingerprint.git_commit,
                subject_dirty=result.subject_fingerprint.dirty,
                audit_commit=result.audit_fingerprint.audit_commit or "unavailable",
                audit_version=result.audit_fingerprint.audit_version,
                case=case,
                trial=trial,
                data_classification=_case_classification(suite, case),
                no_content=no_content,
            )
            try:
                receipt = adapter.publish_trial(request)
                receipts.append(receipt)
            except Exception as exc:
                errors.append(_publication_error("trial", exc, trial.trial_id))
                continue
            try:
                adapter.publish_scores(request, receipt)
            except Exception as exc:
                phase = (
                    "score_confirmation"
                    if isinstance(exc, AuditError)
                    and "confirmation_supported" in exc.details
                    else "scores"
                )
                errors.append(_publication_error(phase, exc, trial.trial_id))
        try:
            experiment_identity = adapter.finish_experiment(
                experiment_identity,
                receipts,
            )
        except Exception as exc:
            errors.append(_publication_error("experiment", exc))
    except _PublicationAbort as abort:
        errors.append(_publication_error(abort.phase, abort.error))
    except Exception as exc:
        errors.append(_publication_error("preparation", exc))
    finally:
        if adapter is not None:
            try:
                adapter.flush()
            except Exception as exc:
                errors.append(_publication_error("flush", exc))
            try:
                adapter.shutdown()
            except Exception as exc:
                errors.append(_publication_error("shutdown", exc))

    manifest, manifest_ref = _finalize_manifest(
        manifest_store,
        manifest,
        errors,
    )
    counts = _publication_counts(adapter, manifest)
    if dataset_result is not None:
        warnings.extend(dataset_result.warnings)
        dataset_identity = dataset_result.dataset
    experiment_status = (
        ExperimentPublicationStatus.FAILED
        if manifest is None
        else _experiment_status(manifest.status)
    )
    has_confirmed = bool(
        counts.published_trial_count or counts.published_score_count
    )
    remote_status = (
        LangfusePublishStatus.COMPLETED
        if not errors
        and manifest is not None
        and manifest.status is PublicationManifestStatus.PUBLISHED
        and not counts.uncertain_score_count
        and not counts.failed_score_count
        else (
            LangfusePublishStatus.PARTIAL
            if has_confirmed
            else LangfusePublishStatus.ERROR
        )
    )
    publication = LangfusePublishResult(
        status=remote_status,
        dataset=dataset_identity,
        experiment=experiment_identity,
        dataset_sync_status=dataset_status,
        experiment_status=experiment_status,
        experiment_strategy=ExperimentStrategy.RUNNER_REPLAY,
        published_trial_count=counts.published_trial_count,
        associated_experiment_item_count=(
            counts.associated_experiment_item_count
        ),
        published_score_count=counts.published_score_count,
        skipped_score_count=counts.skipped_score_count,
        uncertain_score_count=counts.uncertain_score_count,
        failed_score_count=counts.failed_score_count,
        publication_manifest=manifest_ref,
        errors=errors,
        warnings=_deduplicate(warnings),
    )
    payload = result.model_dump(mode="python")
    payload.update(
        {
            "experiment_identity": experiment_identity,
            "remote_publication_status": remote_status,
            "langfuse_publish_result": publication,
            "integration_errors": errors,
        }
    )
    return AuditRunResult.model_validate(payload)


def _stage(phase: str, operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except Exception as exc:
        raise _PublicationAbort(phase, exc) from exc


def _validate_local_publication_input(
    suite: AuditSuite,
    cases: Sequence[AuditCase],
    result: AuditRunResult,
) -> None:
    selected = list(cases)
    if not selected or result.suite_id != suite.suite_id:
        raise PublicationStateError(
            "local Audit result does not match the requested Suite"
        )
    if result.audit_fingerprint.suite_sha256 != canonical_sha256(suite):
        raise PublicationStateError(
            "local Audit result Suite fingerprint changed before publication"
        )
    selected_ids = [case.case_id for case in selected]
    if len(selected_ids) != len(set(selected_ids)):
        raise PublicationStateError("publication Cases must be unique")
    result_case_ids = [item.case_id for item in result.cases]
    if result_case_ids != selected_ids:
        raise PublicationStateError(
            "local Audit result Case order differs from the publication request"
        )
    trial_case_ids = [trial.case_id for trial in result.trials]
    if suite.defaults.trials != 1 or trial_case_ids != selected_ids:
        raise PublicationStateError(
            "Dataset-backed Experiment publication requires one completed local "
            "Trial identity per selected Case"
        )
    if result.langfuse_publish_result is not None or result.integration_errors:
        raise PublicationStateError(
            "Langfuse post-processing requires the original local AuditRunResult"
        )


def _preflight_dataset(
    suite: AuditSuite,
    cases: Sequence[AuditCase],
    result: AuditRunResult,
    dataset: LangfuseDatasetSyncResult,
) -> dict[str, LangfuseDatasetItemIdentity]:
    if dataset.dry_run:
        raise PublicationStateError(
            "a dry-run Dataset plan cannot publish an Audit result"
        )
    if (
        dataset.dataset.suite_id != suite.suite_id
        or dataset.dataset.suite_sha256 != result.audit_fingerprint.suite_sha256
    ):
        raise PublicationStateError(
            "synchronized Langfuse Dataset does not match the local Audit result"
        )
    items = {item.case_id: item for item in dataset.items}
    if len(items) != len(dataset.items):
        raise PublicationStateError(
            "synchronized Langfuse Dataset contains duplicate Case identities"
        )
    for case in cases:
        item = items.get(case.case_id)
        if (
            item is None
            or item.dataset_name != dataset.dataset.dataset_name
            or item.case_sha256 != canonical_sha256(case)
            or not item.remote_item_id
        ):
            raise PublicationStateError(
                "Langfuse Dataset Item is missing or stale during post-processing",
                case_id=case.case_id,
            )
    return items


def _case_classification(
    suite: AuditSuite,
    case: AuditCase,
) -> DataClassification:
    suite_classification = classification_from_metadata(suite.defaults.metadata)
    if "data_classification" not in case.metadata:
        return suite_classification
    return classification_from_metadata(
        case.metadata,
        default=suite_classification,
    )


def _finalize_manifest(
    store: PublicationManifestStore,
    manifest: LangfusePublicationManifest | None,
    errors: list[LangfusePublishError],
) -> tuple[LangfusePublicationManifest | None, PublicationManifestRef | None]:
    try:
        current = store.read() if manifest is not None else None
        if current is None:
            return None, None
        unresolved = any(
            item.status is not PublicationItemStatus.CONFIRMED
            for item in (*current.trial_publications, *current.score_publications)
        )
        if unresolved and not errors:
            errors.append(
                LangfusePublishError(
                    phase="manifest",
                    error_type="unresolved_publication_state",
                    message="publication Manifest contains unresolved remote items",
                    retryable=True,
                )
            )
        has_confirmed = any(
            item.status is PublicationItemStatus.CONFIRMED
            for item in (*current.trial_publications, *current.score_publications)
        )
        status = (
            PublicationManifestStatus.PUBLISHED
            if not errors and not unresolved
            else (
                PublicationManifestStatus.PARTIALLY_PUBLISHED
                if has_confirmed
                else PublicationManifestStatus.FAILED
            )
        )
        payload = current.model_dump(mode="python")
        payload.update(
            {
                "status": status,
                "last_error": errors[-1] if errors else None,
            }
        )
        current = store.write(
            LangfusePublicationManifest.model_validate(payload)
        )
        return current, store.reference(current)
    except Exception as exc:
        errors.append(_publication_error("manifest", exc))
        return manifest, None


def _publication_counts(
    adapter: LangfuseV4Adapter | None,
    manifest: LangfusePublicationManifest | None,
) -> LangfusePublicationCounts:
    if adapter is not None:
        try:
            return adapter.publication_counts()
        except Exception:
            pass
    if manifest is None:
        return LangfusePublicationCounts()
    confirmed_trials = sum(
        item.status is PublicationItemStatus.CONFIRMED
        for item in manifest.trial_publications
    )
    return LangfusePublicationCounts(
        published_trial_count=confirmed_trials,
        associated_experiment_item_count=confirmed_trials,
        published_score_count=sum(
            item.status is PublicationItemStatus.CONFIRMED
            for item in manifest.score_publications
        ),
        uncertain_score_count=sum(
            item.status is PublicationItemStatus.UNCERTAIN
            for item in manifest.score_publications
        ),
        failed_score_count=sum(
            item.status is PublicationItemStatus.FAILED
            for item in manifest.score_publications
        ),
    )


def _experiment_status(
    status: PublicationManifestStatus,
) -> ExperimentPublicationStatus:
    return {
        PublicationManifestStatus.PENDING: ExperimentPublicationStatus.PENDING,
        PublicationManifestStatus.PUBLISHING: ExperimentPublicationStatus.PUBLISHING,
        PublicationManifestStatus.PUBLISHED: ExperimentPublicationStatus.PUBLISHED,
        PublicationManifestStatus.PARTIALLY_PUBLISHED: (
            ExperimentPublicationStatus.PARTIALLY_PUBLISHED
        ),
        PublicationManifestStatus.FAILED: ExperimentPublicationStatus.FAILED,
    }[status]


def _publication_error(
    phase: str,
    error: Exception,
    trial_id: str | None = None,
) -> LangfusePublishError:
    if isinstance(error, AuditError):
        error_type = error.code
        message = error.message
        retryable = error.details.get("retryable") is True
    else:
        error_type = "unexpected_langfuse_error"
        message = f"unexpected Langfuse post-processing error: {type(error).__name__}"
        retryable = False
    return LangfusePublishError(
        phase=phase,
        error_type=error_type,
        message=message,
        trial_id=trial_id,
        retryable=retryable,
        metadata={"exception_type": type(error).__name__},
    )


def _deduplicate(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = ("publish_audit_result",)
