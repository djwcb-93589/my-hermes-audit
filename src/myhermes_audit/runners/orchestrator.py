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
from myhermes_audit.ablation import (
    apply_token_savings,
    build_ablation_comparisons,
    build_trial_identity,
    comparison_basis_fingerprint,
    duration_diagnostics,
    effective_subject_configuration,
    stable_trial_id,
    token_diagnostics,
)
from myhermes_audit.contracts import (
    AblationVariant,
    AuditCase,
    AuditRunResult,
    AuditSuite,
    MetricStatus,
    TrialError,
    TrialResult,
    TrialStatus,
    TrialWarning,
    EffectiveSubjectConfiguration,
    TrialIdentity,
)
from myhermes_audit.contracts.common import CURRENT_SCHEMA_VERSION
from myhermes_audit.datasets.fixtures import materialize_fixtures
from myhermes_audit.errors import (
    AuditError,
    SandboxError,
    SubjectPreflightError,
    UnsupportedCaseError,
)
from myhermes_audit.fingerprint import (
    build_audit_fingerprint,
    read_subject_fingerprint,
)
from myhermes_audit.judges import JudgeEvaluation, JudgeService
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
from myhermes_audit.validators import ValidationContext, evaluate_case
from myhermes_audit.serialization import canonical_sha256


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
    ) -> None:
        self.runner = runner
        self.subject_repo = Path(subject_repo).expanduser().resolve(strict=False)
        self.sandbox_base = (
            None
            if sandbox_base is None
            else Path(sandbox_base).expanduser().resolve(strict=False)
        )
        self.judge_service = judge_service or JudgeService(None)

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
        for case in selected:
            for scenario in case.scenarios:
                if scenario.timeout_seconds > suite.defaults.timeout_seconds:
                    raise SubjectPreflightError(
                        "scenario timeout exceeds the Trial watchdog budget",
                        case_id=case.case_id,
                        scenario_id=scenario.scenario_id,
                        scenario_timeout_seconds=scenario.timeout_seconds,
                        trial_timeout_seconds=suite.defaults.timeout_seconds,
                    )
        subject_fingerprint = read_subject_fingerprint(self.subject_repo)

        audit_started = datetime.now(timezone.utc)
        audit_run_id = f"audit-{uuid.uuid4().hex}"
        audit_fingerprint = build_audit_fingerprint(
            suite,
            created_at=audit_started,
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
            variants: Sequence[AblationVariant | None] = (
                [None]
                if case.ablation is None
                else list(case.ablation.variants)
            )
            for variant in variants:
                configuration = (
                    None
                    if variant is None
                    else _p4_effective_subject_configuration(
                        self.runner,
                        case,
                        variant,
                    )
                )
                basis_fingerprint = (
                    None
                    if variant is None
                    else comparison_basis_fingerprint(case)
                )
                for trial_number in range(1, suite.defaults.trials + 1):
                    trial_identity = (
                        None
                        if variant is None or configuration is None
                        else build_trial_identity(
                            suite_sha256=audit_fingerprint.suite_sha256,
                            case=case,
                            variant=variant,
                            trial_ordinal=trial_number,
                            subject_fingerprint=subject_fingerprint,
                            configuration=configuration,
                        )
                    )
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
                        variant=variant,
                        configuration=configuration,
                        trial_identity=trial_identity,
                        basis_fingerprint=basis_fingerprint,
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

        trials = apply_token_savings(selected, trials)
        comparisons = build_ablation_comparisons(selected, trials)
        case_ids = [case.case_id for case in selected]
        audit_finished = datetime.now(timezone.utc)
        result = AuditRunResult(
            schema_version=CURRENT_SCHEMA_VERSION,
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
            ablation_comparisons=comparisons,
        )
        return OrchestrationOutcome(
            result=result,
            preserved_sandboxes=tuple(preserved),
        )

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
        variant: AblationVariant | None,
        configuration: EffectiveSubjectConfiguration | None,
        trial_identity: TrialIdentity | None,
        basis_fingerprint: str | None,
    ) -> tuple[TrialResult, Path | None]:
        trial_id = (
            (
                f"trial-{canonical_sha256(case.scenarios)[:16]}-{uuid.uuid4().hex}"
                if case.scenarios
                else f"trial-{uuid.uuid4().hex}"
            )
            if trial_identity is None
            else stable_trial_id(trial_identity)
        )
        trial_run_id = f"run-{uuid.uuid4().hex}"
        started_at = datetime.now(timezone.utc)
        trial_clock = time.perf_counter()
        sandbox = AuditSandbox(
            run_id=audit_run_id,
            case_id=case.case_id,
            trial_number=trial_number,
            variant_id=(None if variant is None else variant.variant_id),
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
            _, fixture_manifest_path = materialize_fixtures(
                case.fixture,
                sandbox,
                allow_background_review=bool(
                    case.fixture.background_review_plans
                ),
            )
            if variant is None:
                outcome = self.runner.run_trial(
                    case,
                    sandbox,
                    trial_id=trial_id,
                    timeout_seconds=timeout_seconds,
                )
            else:
                outcome = self.runner.run_trial(
                    case,
                    sandbox,
                    trial_id=trial_id,
                    timeout_seconds=timeout_seconds,
                    variant=variant,
                )
            context = ValidationContext(
                workspace=sandbox.workspace,
                hermes_home=sandbox.hermes_home,
                final_output=outcome.final_output,
                tool_calls=outcome.tool_calls,
                tool_trace_complete=outcome.tool_trace_complete,
                memory_query_results=outcome.memory_query_results,
                memory_snapshots=outcome.memory_snapshots,
                memory_state_changes=outcome.memory_state_changes,
                memory_errors=outcome.memory_errors,
                turns=outcome.turns,
                effective_subject_configuration=configuration,
                ablation_plan=(None if variant is None else case.ablation),
                context_diagnostics=outcome.context_diagnostics,
                fact_context_observations=outcome.fact_context_observations,
                variant_id=(None if variant is None else variant.variant_id),
                background_review_results=outcome.background_review_results,
                background_review_errors=outcome.background_review_errors,
                scenario_results=outcome.scenario_results,
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
                        memory_query_results=(
                            () if outcome is None else outcome.memory_query_results
                        ),
                        memory_snapshots=(
                            () if outcome is None else outcome.memory_snapshots
                        ),
                        memory_state_changes=(
                            () if outcome is None else outcome.memory_state_changes
                        ),
                        memory_errors=(
                            () if outcome is None else outcome.memory_errors
                        ),
                        turns=(() if outcome is None else outcome.turns),
                        effective_subject_configuration=configuration,
                        ablation_plan=(None if variant is None else case.ablation),
                        context_diagnostics=(
                            () if outcome is None else outcome.context_diagnostics
                        ),
                        fact_context_observations=(
                            ()
                            if outcome is None
                            else outcome.fact_context_observations
                        ),
                        variant_id=(
                            None if variant is None else variant.variant_id
                        ),
                        background_review_results=(
                            ()
                            if outcome is None
                            else outcome.background_review_results
                        ),
                        background_review_errors=(
                            ()
                            if outcome is None
                            else outcome.background_review_errors
                        ),
                        scenario_results=(
                            () if outcome is None else outcome.scenario_results
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
        local_required_gates_passed = (
            failure is None
            and outcome is not None
            and outcome.status is RunnerStatus.COMPLETED
            and validator_result is not None
            and validator_result.task_hard_gates_passed
        )
        toolchain_gate_passed = (
            None if validator_result is None else validator_result.toolchain_hard_gates_passed
        )
        process_gate_passed = (
            None if validator_result is None else validator_result.process_hard_gates_passed
        )
        review_gate_passed = (
            None
            if validator_result is None
            else validator_result.review_hard_gates_passed
        )
        task_passed = (
            local_required_gates_passed and review_gate_passed is not False
        )
        judge_gate_passed = (
            judge_evaluation is None
            or not judge_evaluation.required
            or (
                judge_evaluation.metric.status is MetricStatus.COMPLETED
                and judge_evaluation.metric.passed is True
            )
        )
        passed = task_passed and judge_gate_passed
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
        p4_token_diagnostics = (
            None
            if variant is None
            else token_diagnostics(
                None if outcome is None else outcome.runtime,
                () if outcome is None else outcome.compression_events,
                None if outcome is None else outcome.observations,
            )
        )
        p4_duration_diagnostics = (
            None
            if variant is None
            else duration_diagnostics(
                trial_duration_ms=duration_ms,
                retrieval_durations=(
                    ()
                    if outcome is None
                    else tuple(
                        item.duration_ms
                        for item in outcome.memory_query_results
                    )
                ),
                compression_events=(
                    () if outcome is None else outcome.compression_events
                ),
            )
        )
        return (
            TrialResult(
                trial_id=trial_id,
                run_id=trial_run_id,
                case_id=case.case_id,
                trial_number=trial_number,
                trial_identity=trial_identity,
                variant_id=(None if variant is None else variant.variant_id),
                memory_mode=(None if variant is None else variant.memory_mode),
                compression_mode=(
                    None if variant is None else variant.compression_mode
                ),
                configuration_fingerprint=(
                    None
                    if trial_identity is None
                    else trial_identity.configuration_sha256
                ),
                comparison_basis_fingerprint=basis_fingerprint,
                scenario_fingerprint=(
                    None
                    if not case.scenarios
                    else canonical_sha256(case.scenarios)
                ),
                effective_subject_configuration=configuration,
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
                memory_query_results=(
                    [] if outcome is None else list(outcome.memory_query_results)
                ),
                memory_snapshots=(
                    [] if outcome is None else list(outcome.memory_snapshots)
                ),
                memory_state_changes=(
                    [] if outcome is None else list(outcome.memory_state_changes)
                ),
                memory_errors=(
                    [] if outcome is None else list(outcome.memory_errors)
                ),
                compression_events=(
                    [] if outcome is None else list(outcome.compression_events)
                ),
                context_diagnostics=(
                    [] if outcome is None else list(outcome.context_diagnostics)
                ),
                fact_context_observations=(
                    []
                    if outcome is None
                    else list(outcome.fact_context_observations)
                ),
                checkpoint_results=(
                    []
                    if validator_result is None
                    else list(validator_result.checkpoint_results)
                ),
                fact_retention_results=(
                    []
                    if validator_result is None
                    else list(validator_result.fact_retention_results)
                ),
                required_fact_loss=(
                    None
                    if validator_result is None
                    else validator_result.required_fact_loss
                ),
                distortion_results=(
                    []
                    if validator_result is None
                    else list(validator_result.distortion_results)
                ),
                token_diagnostics=p4_token_diagnostics,
                duration_diagnostics=p4_duration_diagnostics,
                retrieval_gate_passed=(
                    None
                    if validator_result is None
                    else validator_result.retrieval_hard_gates_passed
                ),
                final_answer_gate_passed=(
                    None
                    if validator_result is None
                    else validator_result.final_answer_hard_gates_passed
                ),
                memory_state_gate_passed=(
                    None
                    if validator_result is None
                    else validator_result.memory_state_hard_gates_passed
                ),
                required_fact_gate_passed=(
                    None
                    if validator_result is None
                    else validator_result.required_fact_hard_gates_passed
                ),
                background_review_results=(
                    []
                    if outcome is None
                    else list(outcome.background_review_results)
                ),
                background_review_errors=(
                    []
                    if outcome is None
                    else list(outcome.background_review_errors)
                ),
                review_gate_passed=review_gate_passed,
                scenario_results=(
                    [] if outcome is None else list(outcome.scenario_results)
                ),
                process_errors=(
                    [] if outcome is None else list(outcome.process_errors)
                ),
                toolchain_gate_passed=toolchain_gate_passed,
                process_gate_passed=process_gate_passed,
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


def _p4_effective_subject_configuration(
    runner: TrialRunnerPort,
    case: AuditCase,
    variant: AblationVariant,
) -> EffectiveSubjectConfiguration:
    resolver = getattr(runner, "p4_effective_subject_configuration", None)
    if callable(resolver):
        value = resolver(case, variant)
        if isinstance(value, EffectiveSubjectConfiguration):
            return value
        raise TypeError("P4 configuration resolver returned an invalid contract")
    capability_report = getattr(runner, "capability_report", None)
    observation = (
        None
        if capability_report is None
        else capability_report.capability("compression_observation")
    )
    return effective_subject_configuration(
        case,
        variant,
        compression_observation_supported=(
            observation is not None and observation.available
        ),
    )


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


__all__ = ("AuditOrchestrator", "OrchestrationOutcome")
