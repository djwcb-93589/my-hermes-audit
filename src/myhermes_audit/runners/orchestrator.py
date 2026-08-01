"""Failure-isolated serial orchestration for P1 Audit Suites."""

from __future__ import annotations

import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from myhermes_audit.artifacts import artifact_ref, atomic_write_json
from myhermes_audit.contracts import (
    AuditCase,
    AuditRunResult,
    AuditSuite,
    DataClassification,
    LangfuseDatasetItemIdentity,
    LangfuseDatasetSyncResult,
    LangfuseExperimentIdentity,
    LangfusePublishError,
    LangfusePublishResult,
    LangfusePublishStatus,
    LangfuseTrialPublishReceipt,
    MetricStatus,
    TrialError,
    TrialResult,
    TrialStatus,
    TrialWarning,
)
from myhermes_audit.contracts.data import classification_from_metadata
from myhermes_audit.datasets.fixtures import materialize_fixtures
from myhermes_audit.errors import AuditError, SandboxError, UnsupportedCaseError
from myhermes_audit.fingerprint import (
    build_audit_fingerprint,
    read_subject_fingerprint,
)
from myhermes_audit.judges import JudgeEvaluation, JudgeService
from myhermes_audit.ports.langfuse import (
    LangfuseExperimentRequest,
    LangfusePort,
    LangfuseTrialRequest,
)
from myhermes_audit.reports.aggregate import (
    aggregate_audit,
    aggregate_cases,
    aggregate_judges,
)
from myhermes_audit.runners.base import (
    RunnerStatus,
    TrialRunnerOutcome,
    TrialRunnerPort,
)
from myhermes_audit.sandbox import AuditSandbox
from myhermes_audit.serialization import canonical_sha256
from myhermes_audit.validators import ValidationContext, evaluate_case


@dataclass(frozen=True, slots=True)
class OrchestrationOutcome:
    result: AuditRunResult
    preserved_sandboxes: tuple[Path, ...] = ()


class AuditOrchestrator:
    def __init__(
        self,
        *,
        runner: TrialRunnerPort,
        subject_repo: Path,
        sandbox_base: Path | None = None,
        judge_service: JudgeService | None = None,
        langfuse: LangfusePort | None = None,
        langfuse_dataset: LangfuseDatasetSyncResult | None = None,
        experiment_name: str | None = None,
        langfuse_no_content: bool = False,
    ) -> None:
        self.runner = runner
        self.subject_repo = Path(subject_repo).expanduser().resolve(strict=False)
        self.sandbox_base = (
            None
            if sandbox_base is None
            else Path(sandbox_base).expanduser().resolve(strict=False)
        )
        self.judge_service = judge_service or JudgeService(None)
        self.langfuse = langfuse
        self.langfuse_dataset = langfuse_dataset
        self.experiment_name = experiment_name
        self.langfuse_no_content = langfuse_no_content
        if langfuse is None and any(
            value is not None for value in (langfuse_dataset, experiment_name)
        ):
            raise ValueError("Langfuse options require a Langfuse port")
        if langfuse is not None and (
            langfuse_dataset is None or experiment_name is None
        ):
            raise ValueError(
                "Langfuse publication requires synchronized Dataset and Experiment names"
            )

    def run(
        self,
        suite: AuditSuite,
        *,
        cases: Sequence[AuditCase] | None = None,
        preserve_on_failure: bool = False,
    ) -> OrchestrationOutcome:
        selected = list(suite.cases if cases is None else cases)
        if not selected:
            raise ValueError("at least one case must be selected")
        suite_cases = {case.case_id: case for case in suite.cases}
        selected_ids = [case.case_id for case in selected]
        if len(selected_ids) != len(set(selected_ids)) or any(
            case.case_id not in suite_cases or case != suite_cases[case.case_id]
            for case in selected
        ):
            raise UnsupportedCaseError(
                "selected cases must be unique members of the loaded Suite"
            )
        if suite.defaults.seed is not None:
            raise UnsupportedCaseError("P1 does not provide deterministic model seeding")

        # All suite, subject, config, fixture, and evaluator preflight completes
        # before the first Sandbox or worker process is created.
        self.judge_service.preflight(selected)
        self.runner.preflight(selected)
        subject_fingerprint = read_subject_fingerprint(self.subject_repo)

        audit_started = datetime.now(timezone.utc)
        audit_run_id = f"audit-{uuid.uuid4().hex}"
        audit_fingerprint = build_audit_fingerprint(
            suite,
            created_at=audit_started,
        )
        dataset_items: dict[str, LangfuseDatasetItemIdentity] = {}
        experiment_identity: LangfuseExperimentIdentity | None = None
        receipts: list[LangfuseTrialPublishReceipt] = []
        publication_errors: list[LangfusePublishError] = []
        published_trial_count = 0
        published_score_count = 0
        if self.langfuse is not None:
            dataset_items = self._preflight_langfuse(suite, selected)
            dataset = self.langfuse_dataset
            if dataset is None or self.experiment_name is None:
                raise RuntimeError("Langfuse constructor invariants were not preserved")
            experiment_identity = self.langfuse.begin_experiment(
                LangfuseExperimentRequest(
                    identity=LangfuseExperimentIdentity(
                        experiment_name=self.experiment_name,
                        audit_run_id=audit_run_id,
                        dataset_name=dataset.dataset.dataset_name,
                    ),
                    suite_id=suite.suite_id,
                    suite_sha256=audit_fingerprint.suite_sha256,
                    subject_commit=subject_fingerprint.git_commit,
                    audit_commit=audit_fingerprint.audit_commit or "unavailable",
                    audit_version=audit_fingerprint.audit_version,
                )
            )
        created_base = self.sandbox_base is None
        if created_base:
            sandbox_base = Path(tempfile.mkdtemp(prefix="myhermes-audit-run-"))
        else:
            sandbox_base = self.sandbox_base
            if sandbox_base is None:
                raise RuntimeError("sandbox base resolution failed")

        trials: list[TrialResult] = []
        preserved: list[Path] = []
        for case in selected:
            for trial_number in range(1, suite.defaults.trials + 1):
                trial, preserved_path = self._run_one(
                    case,
                    trial_number=trial_number,
                    timeout_seconds=suite.defaults.timeout_seconds,
                    audit_run_id=audit_run_id,
                    sandbox_base=sandbox_base,
                    preserve=(
                        suite.defaults.preserve_sandbox
                        or preserve_on_failure
                    ),
                    preserve_all=suite.defaults.preserve_sandbox,
                )
                trials.append(trial)
                if preserved_path is not None:
                    preserved.append(preserved_path)

        if created_base and not preserved:
            try:
                sandbox_base.rmdir()
            except OSError as exc:
                raise SandboxError(
                    "cannot remove the owned Audit run root",
                    operation="cleanup_run_root",
                ) from exc

        # Remote publication is post-hoc: all local Trial facts exist before the
        # first Trace or Score write, so network failures cannot interleave with
        # or prevent the remaining local executions.
        if self.langfuse is not None and experiment_identity is not None:
            for trial in trials:
                case = suite_cases[trial.case_id]
                publication_request = LangfuseTrialRequest(
                    experiment=experiment_identity,
                    dataset_item=dataset_items[case.case_id],
                    suite_id=suite.suite_id,
                    suite_sha256=audit_fingerprint.suite_sha256,
                    subject_commit=subject_fingerprint.git_commit,
                    subject_dirty=subject_fingerprint.dirty,
                    audit_commit=audit_fingerprint.audit_commit or "unavailable",
                    audit_version=audit_fingerprint.audit_version,
                    case=case,
                    trial=trial,
                    data_classification=_case_classification(suite, case),
                    no_content=self.langfuse_no_content,
                )
                try:
                    receipt = self.langfuse.publish_trial(publication_request)
                    receipts.append(receipt)
                    published_trial_count += 1
                except Exception as exc:
                    publication_errors.append(
                        _publication_error(
                            "trial",
                            exc,
                            trial_id=trial.trial_id,
                        )
                    )
                else:
                    try:
                        published_score_count += self.langfuse.publish_scores(
                            publication_request,
                            receipt,
                        )
                    except Exception as exc:
                        if isinstance(exc, AuditError):
                            partial_count = exc.details.get("published_count")
                            if type(partial_count) is int and partial_count >= 0:
                                published_score_count += partial_count
                        publication_errors.append(
                            _publication_error(
                                "scores",
                                exc,
                                trial_id=trial.trial_id,
                            )
                        )

        langfuse_publish_result: LangfusePublishResult | None = None
        if self.langfuse is not None and experiment_identity is not None:
            try:
                experiment_identity = self.langfuse.finish_experiment(
                    experiment_identity,
                    receipts,
                )
            except Exception as exc:
                publication_errors.append(_publication_error("experiment", exc))
            try:
                self.langfuse.flush()
            except Exception as exc:
                publication_errors.append(_publication_error("flush", exc))
            try:
                self.langfuse.shutdown()
            except Exception as exc:
                publication_errors.append(_publication_error("shutdown", exc))
            publication_status = (
                LangfusePublishStatus.COMPLETED
                if not publication_errors
                else (
                    LangfusePublishStatus.PARTIAL
                    if published_trial_count > 0
                    else LangfusePublishStatus.ERROR
                )
            )
            dataset = self.langfuse_dataset
            if dataset is None:
                raise RuntimeError("Langfuse Dataset result was lost")
            langfuse_publish_result = LangfusePublishResult(
                status=publication_status,
                dataset=dataset.dataset,
                experiment=experiment_identity,
                published_trial_count=published_trial_count,
                published_score_count=published_score_count,
                errors=publication_errors,
                warnings=list(dataset.warnings),
            )

        case_ids = [case.case_id for case in selected]
        audit_finished = datetime.now(timezone.utc)
        result = AuditRunResult(
            run_id=audit_run_id,
            suite_id=suite.suite_id,
            subject_fingerprint=subject_fingerprint,
            audit_fingerprint=audit_fingerprint,
            started_at=audit_started,
            finished_at=audit_finished,
            trials=trials,
            cases=aggregate_cases(case_ids, trials),
            summary=aggregate_audit(case_ids, trials),
            judge_summary=aggregate_judges(trials),
            experiment_identity=experiment_identity,
            langfuse_publish_result=langfuse_publish_result,
            integration_errors=publication_errors,
        )
        return OrchestrationOutcome(
            result=result,
            preserved_sandboxes=tuple(preserved),
        )

    def _preflight_langfuse(
        self,
        suite: AuditSuite,
        selected: Sequence[AuditCase],
    ) -> dict[str, LangfuseDatasetItemIdentity]:
        dataset = self.langfuse_dataset
        experiment_name = self.experiment_name
        if dataset is None or experiment_name is None:
            raise RuntimeError("Langfuse constructor invariants were not preserved")
        if dataset.dry_run:
            raise UnsupportedCaseError(
                "a dry-run Dataset plan cannot be used for Trial publication"
            )
        current_suite_hash = canonical_sha256(suite)
        if (
            dataset.dataset.suite_id != suite.suite_id
            or dataset.dataset.suite_sha256 != current_suite_hash
        ):
            raise UnsupportedCaseError(
                "synchronized Langfuse Dataset does not match the loaded Suite"
            )
        if (
            not experiment_name.strip()
            or len(experiment_name) > 200
            or any(ord(character) < 32 for character in experiment_name)
        ):
            raise UnsupportedCaseError(
                "experiment-name must be a safe non-empty name up to 200 characters"
            )
        items = {item.case_id: item for item in dataset.items}
        if len(items) != len(dataset.items):
            raise UnsupportedCaseError(
                "synchronized Langfuse Dataset contains duplicate Case identities"
            )
        for case in selected:
            item = items.get(case.case_id)
            if (
                item is None
                or item.dataset_name != dataset.dataset.dataset_name
                or item.case_sha256 != canonical_sha256(case)
                or not item.remote_item_id
            ):
                raise UnsupportedCaseError(
                    "Langfuse Dataset Item is missing or stale before Trial execution",
                    case_id=case.case_id,
                )
        return items

    def _run_one(
        self,
        case: AuditCase,
        *,
        trial_number: int,
        timeout_seconds: int,
        audit_run_id: str,
        sandbox_base: Path,
        preserve: bool,
        preserve_all: bool,
    ) -> tuple[TrialResult, Path | None]:
        trial_id = f"trial-{uuid.uuid4().hex}"
        trial_run_id = f"run-{uuid.uuid4().hex}"
        started_at = datetime.now(timezone.utc)
        trial_clock = time.perf_counter()
        sandbox = AuditSandbox(
            run_id=audit_run_id,
            case_id=case.case_id,
            trial_number=trial_number,
            base_dir=sandbox_base,
            preserve=False,
        )
        outcome: TrialRunnerOutcome | None = None
        fixture_manifest_path: Path | None = None
        validator_path: Path | None = None
        validator_result = None
        judge_evaluation: JudgeEvaluation | None = None
        warnings: list[TrialWarning] = []
        failure: Exception | None = None
        preserved_path: Path | None = None
        artifacts = []
        sandbox_created = False

        try:
            sandbox.create()
            sandbox_created = True
            _, fixture_manifest_path = materialize_fixtures(case.fixture, sandbox)
            outcome = self.runner.run_trial(
                case,
                sandbox,
                trial_id=trial_id,
                timeout_seconds=timeout_seconds,
            )
            context = ValidationContext(
                workspace=sandbox.workspace,
                hermes_home=sandbox.hermes_home,
                final_output=outcome.final_output,
                tool_calls=outcome.tool_calls,
                tool_trace_complete=outcome.tool_trace_complete,
            )
            validator_result = evaluate_case(
                case,
                context,
                trial_id=trial_id,
            )
            validator_path = sandbox.artifacts_dir / "validator-results.json"
            atomic_write_json(validator_path, validator_result)
        except Exception as exc:
            failure = exc
            if sandbox_created:
                try:
                    context = ValidationContext(
                        workspace=sandbox.workspace,
                        hermes_home=sandbox.hermes_home,
                        final_output=(None if outcome is None else outcome.final_output),
                        tool_calls=(None if outcome is None else outcome.tool_calls),
                        tool_trace_complete=(
                            False if outcome is None else outcome.tool_trace_complete
                        ),
                    )
                    validator_result = evaluate_case(
                        case,
                        context,
                        trial_id=trial_id,
                    )
                    validator_path = sandbox.artifacts_dir / "validator-results.json"
                    atomic_write_json(validator_path, validator_result)
                except Exception as validator_exc:
                    warnings.append(
                        TrialWarning(
                            warning_type="validator_artifact_error",
                            message=(
                                "validator artifact could not be produced: "
                                f"{type(validator_exc).__name__}"
                            ),
                        )
                    )

        provisional_status = _trial_status(outcome, failure)
        judge_evaluation = self.judge_service.evaluate(
            case,
            trial_status=provisional_status,
            final_output=(None if outcome is None else outcome.final_output),
            deterministic_metrics=(
                [] if validator_result is None else validator_result.metrics
            ),
            tool_calls=(None if outcome is None else outcome.tool_calls),
            turns=[] if outcome is None else outcome.turns,
        )

        if sandbox_created:
            artifact_paths = (
                {} if outcome is None else dict(outcome.artifact_paths)
            )
            if fixture_manifest_path is not None:
                artifact_paths["fixture_manifest"] = fixture_manifest_path
            if validator_path is not None:
                artifact_paths["validator_results"] = validator_path
            for artifact_id, path in sorted(artifact_paths.items()):
                try:
                    artifacts.append(
                        artifact_ref(
                            path,
                            trial_root=sandbox.root,
                            artifact_id=artifact_id.replace("_", "-"),
                            kind=artifact_id,
                        )
                    )
                except Exception as exc:
                    failure = failure or exc

        status = _trial_status(outcome, failure)
        task_passed = (
            failure is None
            and outcome is not None
            and outcome.status is RunnerStatus.COMPLETED
            and validator_result is not None
            and validator_result.deterministic_hard_gates_passed
        )
        local_required_gates_passed = (
            failure is None
            and outcome is not None
            and outcome.status is RunnerStatus.COMPLETED
            and validator_result is not None
            and validator_result.hard_gates_passed
        )
        judge_gate_passed = (
            judge_evaluation is None
            or not judge_evaluation.required
            or (
                judge_evaluation.metric.status is MetricStatus.COMPLETED
                and judge_evaluation.metric.passed is True
            )
        )
        passed = local_required_gates_passed and judge_gate_passed
        should_preserve = preserve_all or (preserve and not passed)
        if sandbox_created:
            if should_preserve:
                sandbox.preserve = True
                preserved_path = sandbox.root
                warnings.append(
                    TrialWarning(
                        warning_type="sandbox_preserved",
                        message="Trial Sandbox was preserved by policy",
                    )
                )
            else:
                try:
                    sandbox.cleanup()
                except SandboxError as exc:
                    failure = exc
                    status = TrialStatus.ENVIRONMENT_ERROR
                    task_passed = False
                    passed = False
                    warnings.append(
                        TrialWarning(
                            warning_type="sandbox_cleanup_error",
                            message="Trial Sandbox cleanup failed",
                        )
                    )
                    try:
                        preserved_path = sandbox.root
                    except SandboxError:
                        preserved_path = None

        finished_at = datetime.now(timezone.utc)
        duration_ms = (
            outcome.duration_ms
            if outcome is not None
            else max(0, round((time.perf_counter() - trial_clock) * 1000))
        )
        error = _trial_error(outcome, failure, status)
        if outcome is not None:
            warnings = [*outcome.warnings, *warnings]
        metrics = [] if validator_result is None else list(validator_result.metrics)
        if judge_evaluation is not None:
            metrics.append(judge_evaluation.metric)
        return (
            TrialResult(
                trial_id=trial_id,
                run_id=trial_run_id,
                case_id=case.case_id,
                trial_number=trial_number,
                status=status,
                task_passed=task_passed,
                passed=passed,
                final_output=(
                    outcome.final_output
                    if outcome is not None and status is TrialStatus.COMPLETED
                    else None
                ),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                turns=[] if outcome is None else list(outcome.turns),
                runtime=(None if outcome is None else outcome.runtime),
                observations=(None if outcome is None else outcome.observations),
                metrics=metrics,
                judge_result=(
                    None if judge_evaluation is None else judge_evaluation.result
                ),
                artifacts=artifacts,
                warnings=warnings,
                error=error,
            ),
            preserved_path,
        )


def _trial_status(
    outcome: TrialRunnerOutcome | None,
    failure: Exception | None,
) -> TrialStatus:
    if failure is not None or outcome is None:
        return TrialStatus.ENVIRONMENT_ERROR
    return {
        RunnerStatus.COMPLETED: TrialStatus.COMPLETED,
        RunnerStatus.FAILED: TrialStatus.FAILED,
        RunnerStatus.TIMEOUT: TrialStatus.TIMEOUT,
        RunnerStatus.ENVIRONMENT_ERROR: TrialStatus.ENVIRONMENT_ERROR,
    }[outcome.status]


def _trial_error(
    outcome: TrialRunnerOutcome | None,
    failure: Exception | None,
    status: TrialStatus,
) -> TrialError | None:
    if status is TrialStatus.COMPLETED:
        return None
    if status is TrialStatus.TIMEOUT:
        return TrialError(
            error_type="timeout",
            message="MyHermes worker exceeded the Trial timeout",
            retryable=True,
        )
    if failure is not None:
        if isinstance(failure, AuditError):
            error_type = failure.code.replace("_error", "")
            message = f"Trial failed during {failure.code}"
        else:
            error_type = "orchestrator_error"
            message = f"Trial orchestration failed: {type(failure).__name__}"
        return TrialError(error_type=error_type, message=message)
    error_type = outcome.error_type if outcome is not None else None
    message = outcome.error_message if outcome is not None else None
    return TrialError(
        error_type=error_type or status.value,
        message=message or f"MyHermes worker ended with {status.value}",
        retryable=False if outcome is None else outcome.retryable,
    )


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


def _publication_error(
    phase: str,
    error: Exception,
    *,
    trial_id: str | None = None,
) -> LangfusePublishError:
    if isinstance(error, AuditError):
        error_type = error.code
        message = error.message
        retryable = error.details.get("retryable") is True
    else:
        error_type = "unexpected_langfuse_error"
        message = f"unexpected Langfuse adapter error: {type(error).__name__}"
        retryable = False
    return LangfusePublishError(
        phase=phase,
        error_type=error_type,
        message=message,
        trial_id=trial_id,
        retryable=retryable,
        metadata={"exception_type": type(error).__name__},
    )


__all__ = ("AuditOrchestrator", "OrchestrationOutcome")
