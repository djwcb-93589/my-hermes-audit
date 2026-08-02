"""P4 Variant configuration, stable identity, diagnostics, and replay comparison."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence

from myhermes_audit.contracts.ablation import (
    AblationComparisonResult,
    AblationMetricDelta,
    AblationVariant,
    AblationVariantResult,
    ComparabilityStatus,
    CompressionControl,
    CompressionEvent,
    CompressionMode,
    DiagnosticStatus,
    DISABLED_COMPRESSION_THRESHOLD,
    DistortionType,
    DurationDiagnostics,
    DurationSource,
    EffectiveSubjectConfiguration,
    MemoryMode,
    SessionContextMode,
    TokenCountSource,
    TokenDiagnostics,
    TrialIdentity,
    LongConversationCheckpoint,
    RequiredFactExpectation,
)
from myhermes_audit.contracts.result import (
    MetricSource,
    MetricStatus,
    TrialResult,
    TrialRuntimeSummary,
)
from myhermes_audit.contracts.fingerprint import SubjectFingerprint
from myhermes_audit.contracts.suite import AuditCase, ToolsetName
from myhermes_audit.errors import AblationComparisonError, AblationVariantError
from myhermes_audit.serialization import canonical_sha256


def _deep_merge(base: dict, overrides: Mapping) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def effective_subject_configuration(
    case: AuditCase,
    variant: AblationVariant,
    *,
    compression_observation_available: bool,
) -> EffectiveSubjectConfiguration:
    plan = case.ablation
    if plan is None:
        raise AblationVariantError(
            "P4 Variant requires an ablation plan",
            case_id=case.case_id,
            variant_id=variant.variant_id,
        )
    long_term = variant.memory_mode in {
        MemoryMode.LONG_TERM_ONLY,
        MemoryMode.SHORT_AND_LONG_TERM,
    }
    short_term = variant.memory_mode in {
        MemoryMode.SHORT_TERM_ONLY,
        MemoryMode.SHORT_AND_LONG_TERM,
    }
    if long_term and case.execution.memory_strategy is None:
        raise AblationVariantError(
            "long-term Memory Variant requires execution.memory_strategy",
            case_id=case.case_id,
            variant_id=variant.variant_id,
        )
    compression_section = dict(
        variant.config_overrides.get("compression", {})
    )
    threshold = (
        int(compression_section["threshold"])
        if variant.compression_mode is CompressionMode.ENABLED
        else DISABLED_COMPRESSION_THRESHOLD
    )
    compression_section["threshold"] = threshold
    public_overrides = {"compression": compression_section}
    return EffectiveSubjectConfiguration(
        memory_mode=variant.memory_mode,
        compression_mode=variant.compression_mode,
        session_context_mode=(
            SessionContextMode.SUBJECT_SESSION
            if short_term
            else SessionContextMode.ISOLATED_PER_TURN
        ),
        include_memory=long_term,
        include_user_profile=long_term,
        memory_tool_enabled=(
            long_term and ToolsetName.MEMORY in case.execution.enabled_toolsets
        ),
        memory_strategy=(case.execution.memory_strategy if long_term else None),
        compression_control=CompressionControl.THRESHOLD_CONFIGURATION,
        compression_threshold=threshold,
        compression_observation_available=compression_observation_available,
        maximum_turns=plan.maximum_turns,
        maximum_compression_events=plan.maximum_compression_events,
        public_config_overrides=public_overrides,
    )


def effective_config_overrides(
    case: AuditCase,
    configuration: EffectiveSubjectConfiguration,
) -> dict:
    return _deep_merge(
        dict(case.execution.config_overrides),
        configuration.public_config_overrides,
    )


def effective_toolsets(
    case: AuditCase,
    configuration: EffectiveSubjectConfiguration,
) -> list[ToolsetName]:
    return [
        item
        for item in case.execution.enabled_toolsets
        if item is not ToolsetName.MEMORY or configuration.memory_tool_enabled
    ]


def applicable_fact_expectations(
    case: AuditCase,
    variant_id: str,
) -> list[RequiredFactExpectation]:
    return [
        item
        for item in case.expected.required_facts
        if not item.applicable_variant_ids
        or variant_id in item.applicable_variant_ids
    ]


def applicable_checkpoints(
    case: AuditCase,
    variant_id: str,
) -> list[LongConversationCheckpoint]:
    if case.ablation is None:
        return []
    fact_ids = {
        fact.fact_id
        for expectation in applicable_fact_expectations(case, variant_id)
        for fact in expectation.facts
    }
    projected: list[LongConversationCheckpoint] = []
    for checkpoint in case.ablation.checkpoints:
        required_ids = [
            item for item in checkpoint.required_fact_ids if item in fact_ids
        ]
        if not required_ids and checkpoint.expected_answer is None:
            continue
        projected.append(
            checkpoint.model_copy(update={"required_fact_ids": required_ids})
        )
    return projected


def configuration_fingerprint(
    case: AuditCase,
    variant: AblationVariant,
    configuration: EffectiveSubjectConfiguration,
) -> str:
    return canonical_sha256(
        {
            "variant_id": variant.variant_id,
            "execution": case.execution,
            "effective_subject_configuration": configuration,
            "effective_config_overrides": effective_config_overrides(
                case,
                configuration,
            ),
            "effective_toolsets": [
                item.value for item in effective_toolsets(case, configuration)
            ],
        }
    )


def comparison_basis_fingerprint(case: AuditCase) -> str:
    return canonical_sha256(
        {
            "case_id": case.case_id,
            "mode": case.mode.value,
            "input": case.input,
            "execution": case.execution,
            "fixture": case.fixture,
            "expected": case.expected,
            "evaluators": case.evaluators,
        }
    )


def configured_model_identifier(
    case: AuditCase,
    configuration: EffectiveSubjectConfiguration,
) -> str:
    document = effective_config_overrides(case, configuration)
    model = document.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()[:256]
    return "subject-default"


def build_trial_identity(
    *,
    suite_sha256: str,
    case: AuditCase,
    variant: AblationVariant,
    trial_ordinal: int,
    subject_fingerprint: SubjectFingerprint,
    configuration: EffectiveSubjectConfiguration,
    model_identifier: str | None = None,
) -> TrialIdentity:
    config_sha256 = configuration_fingerprint(case, variant, configuration)
    resolved_model_identifier = (
        configured_model_identifier(case, configuration)
        if model_identifier is None
        else model_identifier
    )
    payload = {
        "suite_sha256": suite_sha256,
        "case_id": case.case_id,
        "variant_id": variant.variant_id,
        "trial_ordinal": trial_ordinal,
        "subject_commit": subject_fingerprint.git_commit,
        "subject_fingerprint_sha256": subject_identity_fingerprint(
            subject_fingerprint
        ),
        "configuration_sha256": config_sha256,
        "model_identifier": resolved_model_identifier,
    }
    return TrialIdentity(
        **payload,
        identity_sha256=canonical_sha256(payload),
    )


def subject_identity_fingerprint(subject: SubjectFingerprint) -> str:
    """Hash stable Subject execution facts without its machine-local path."""

    return canonical_sha256(
        {
            "git_commit": subject.git_commit,
            "tree_hash": subject.tree_hash,
            "dirty": subject.dirty,
            "python_requirement": subject.python_requirement,
        }
    )


def stable_trial_id(identity: TrialIdentity) -> str:
    return f"trial-{identity.identity_sha256}"


def token_diagnostics(
    runtime: TrialRuntimeSummary | None,
    compression_events: Sequence[CompressionEvent],
) -> TokenDiagnostics:
    if runtime is None or all(
        item is None
        for item in (
            runtime.prompt_tokens,
            runtime.completion_tokens,
            runtime.total_tokens,
        )
    ):
        return TokenDiagnostics(
            status=DiagnosticStatus.UNAVAILABLE,
            source=TokenCountSource.UNAVAILABLE,
        )
    complete = all(
        item is not None
        for item in (
            runtime.prompt_tokens,
            runtime.completion_tokens,
            runtime.total_tokens,
        )
    )
    compression_input = (
        sum(item.input_token_count for item in compression_events)
        if compression_events
        and all(item.input_token_count is not None for item in compression_events)
        else None
    )
    compression_output = (
        sum(item.output_token_count for item in compression_events)
        if compression_events
        and all(item.output_token_count is not None for item in compression_events)
        else None
    )
    return TokenDiagnostics(
        status=(DiagnosticStatus.AVAILABLE if complete else DiagnosticStatus.PARTIAL),
        source=TokenCountSource.PROVIDER_REPORTED,
        input_tokens=runtime.prompt_tokens,
        output_tokens=runtime.completion_tokens,
        total_tokens=runtime.total_tokens,
        compression_input_tokens=compression_input,
        compression_output_tokens=compression_output,
    )


def duration_diagnostics(
    *,
    trial_duration_ms: int,
    retrieval_durations: Sequence[int],
    compression_events: Sequence[CompressionEvent],
) -> DurationDiagnostics:
    retrieval_duration = (
        sum(retrieval_durations) if retrieval_durations else None
    )
    compression_duration = (
        sum(item.duration_ms for item in compression_events)
        if compression_events
        and all(item.duration_ms is not None for item in compression_events)
        else None
    )
    return DurationDiagnostics(
        status=(
            DiagnosticStatus.AVAILABLE
            if retrieval_duration is not None and compression_duration is not None
            else DiagnosticStatus.PARTIAL
        ),
        trial_duration_ms=trial_duration_ms,
        trial_duration_source=DurationSource.AUDIT_MEASURED,
        retrieval_duration_ms=retrieval_duration,
        retrieval_duration_source=(
            DurationSource.AUDIT_MEASURED
            if retrieval_duration is not None
            else DurationSource.UNAVAILABLE
        ),
        compression_duration_ms=compression_duration,
        compression_duration_source=(
            DurationSource.SUBJECT_REPORTED
            if compression_duration is not None
            else DurationSource.UNAVAILABLE
        ),
    )


def _reliable_token_source(source: TokenCountSource) -> bool:
    return source in {
        TokenCountSource.SUBJECT_REPORTED,
        TokenCountSource.PROVIDER_REPORTED,
    }


def apply_token_savings(
    cases: Sequence[AuditCase],
    trials: Sequence[TrialResult],
) -> list[TrialResult]:
    case_by_id = {case.case_id: case for case in cases}
    by_key = {
        (trial.case_id, trial.variant_id, trial.trial_number): trial
        for trial in trials
        if trial.variant_id is not None
    }
    updated: list[TrialResult] = []
    for trial in trials:
        case = case_by_id.get(trial.case_id)
        if case is None or case.ablation is None or trial.variant_id is None:
            updated.append(trial)
            continue
        reference = by_key.get(
            (
                trial.case_id,
                case.ablation.reference_variant_id,
                trial.trial_number,
            )
        )
        current_tokens = trial.token_diagnostics
        reference_tokens = None if reference is None else reference.token_diagnostics
        comparable = all(
            (
                reference is not None,
                current_tokens is not None,
                reference_tokens is not None,
                trial.runtime is not None,
                reference is not None and reference.runtime is not None,
                trial.runtime is not None
                and reference is not None
                and reference.runtime is not None
                and trial.runtime.subject_model is not None
                and trial.runtime.subject_model == reference.runtime.subject_model,
                trial.comparison_basis_fingerprint
                == (
                    None
                    if reference is None
                    else reference.comparison_basis_fingerprint
                ),
                trial.trial_identity is not None,
                reference is not None
                and reference.trial_identity is not None,
                trial.trial_identity is not None
                and reference is not None
                and reference.trial_identity is not None
                and trial.trial_identity.suite_sha256
                == reference.trial_identity.suite_sha256,
                trial.trial_identity is not None
                and reference is not None
                and reference.trial_identity is not None
                and trial.trial_identity.subject_commit
                == reference.trial_identity.subject_commit,
                trial.trial_identity is not None
                and reference is not None
                and reference.trial_identity is not None
                and trial.trial_identity.subject_fingerprint_sha256
                == reference.trial_identity.subject_fingerprint_sha256,
                current_tokens is not None
                and reference_tokens is not None
                and current_tokens.source is reference_tokens.source
                and _reliable_token_source(current_tokens.source),
                current_tokens is not None and current_tokens.total_tokens is not None,
                reference_tokens is not None
                and reference_tokens.total_tokens is not None,
            )
        )
        if not comparable:
            updated.append(trial)
            continue
        assert current_tokens is not None
        assert reference_tokens is not None
        assert current_tokens.total_tokens is not None
        assert reference_tokens.total_tokens is not None
        savings = reference_tokens.total_tokens - current_tokens.total_tokens
        savings_rate = (
            None
            if reference_tokens.total_tokens == 0
            else float(savings / reference_tokens.total_tokens)
        )
        revised = TokenDiagnostics(
            **current_tokens.model_dump(
                exclude={"schema_version", "token_savings", "token_savings_rate"}
            ),
            token_savings=savings,
            token_savings_rate=savings_rate,
        )
        updated.append(trial.model_copy(update={"token_diagnostics": revised}))
    return updated


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else float(sum(values) / len(values))


def _variant_result(
    variant: AblationVariant,
    trials: Sequence[TrialResult],
) -> AblationVariantResult:
    if not trials:
        raise AblationComparisonError(
            "Ablation Variant has no local Trial results",
            variant_id=variant.variant_id,
        )
    configuration_values = {
        item.configuration_fingerprint
        for item in trials
        if item.configuration_fingerprint is not None
    }
    if len(configuration_values) != 1:
        raise AblationComparisonError(
            "Ablation Variant has inconsistent configuration fingerprints",
            variant_id=variant.variant_id,
        )
    models = {
        item.runtime.subject_model
        for item in trials
        if item.runtime is not None and item.runtime.subject_model is not None
    }
    subject_model = next(iter(models)) if len(models) == 1 else None
    retrieval_values = [
        float(item.retrieval_gate_passed is True)
        for item in trials
        if item.retrieval_gate_passed is not None
    ]
    answer_values = [
        float(metric.value)
        for item in trials
        for metric in item.metrics
        if metric.metric_name == "answer_quality"
        and metric.source is MetricSource.JUDGE
        and metric.status is MetricStatus.COMPLETED
        and type(metric.value) in (int, float)
    ]
    fact_required = sum(
        item.required_fact_loss.required_fact_count or 0
        for item in trials
        if item.required_fact_loss is not None
        and item.required_fact_loss.status is DiagnosticStatus.AVAILABLE
    )
    fact_lost = sum(
        item.required_fact_loss.required_fact_loss_count or 0
        for item in trials
        if item.required_fact_loss is not None
        and item.required_fact_loss.status is DiagnosticStatus.AVAILABLE
    )
    token_records = [item.token_diagnostics for item in trials]
    token_sources = {
        item.source for item in token_records if item is not None
    }
    token_source = (
        next(iter(token_sources))
        if len(token_sources) == 1
        else TokenCountSource.UNAVAILABLE
    )
    total_tokens = (
        sum(item.total_tokens for item in token_records if item is not None)
        if len(token_records) == len(trials)
        and all(item is not None and item.total_tokens is not None for item in token_records)
        else None
    )
    return AblationVariantResult(
        variant_id=variant.variant_id,
        memory_mode=variant.memory_mode,
        compression_mode=variant.compression_mode,
        trial_ids=[item.trial_id for item in trials],
        configuration_sha256=next(iter(configuration_values)),
        subject_model=subject_model,
        task_success_rate=float(
            sum(item.task_passed is True for item in trials) / len(trials)
        ),
        retrieval_success_rate=_mean(retrieval_values),
        answer_quality_mean=_mean(answer_values),
        required_fact_loss_rate=(
            None if fact_required == 0 else float(fact_lost / fact_required)
        ),
        distortion_count=sum(
            result.distortion_type is not DistortionType.NOT_EVALUABLE
            for item in trials
            for result in item.distortion_results
        ),
        total_tokens=total_tokens,
        duration_ms=sum(item.duration_ms or 0 for item in trials),
        token_source=token_source,
    )


def build_ablation_comparisons(
    cases: Sequence[AuditCase],
    trials: Sequence[TrialResult],
) -> list[AblationComparisonResult]:
    comparisons: list[AblationComparisonResult] = []
    for case in cases:
        plan = case.ablation
        if plan is None:
            continue
        case_trials = [item for item in trials if item.case_id == case.case_id]
        variant_results = [
            _variant_result(
                variant,
                [
                    item
                    for item in case_trials
                    if item.variant_id == variant.variant_id
                ],
            )
            for variant in plan.variants
        ]
        by_id = {item.variant_id: item for item in variant_results}
        reference = by_id[plan.reference_variant_id]
        reasons: list[str] = []
        if any(item.subject_model is None for item in variant_results):
            reasons.append("subject_model_unavailable")
        elif len({item.subject_model for item in variant_results}) != 1:
            reasons.append("subject_model_mismatch")
        basis_values = {
            item.comparison_basis_fingerprint for item in case_trials
        }
        if len(basis_values) != 1 or None in basis_values:
            reasons.append("comparison_basis_mismatch")
        identity_values = [
            item.trial_identity for item in case_trials
        ]
        if any(item is None for item in identity_values):
            reasons.append("trial_identity_unavailable")
        else:
            if len({item.suite_sha256 for item in identity_values}) != 1:
                reasons.append("suite_fingerprint_mismatch")
            if len({item.subject_commit for item in identity_values}) != 1:
                reasons.append("subject_commit_mismatch")
            if len(
                {item.subject_fingerprint_sha256 for item in identity_values}
            ) != 1:
                reasons.append("subject_fingerprint_mismatch")
        if any(item.total_tokens is None for item in variant_results):
            reasons.append("token_data_unavailable")
        if len({item.token_source for item in variant_results}) != 1:
            reasons.append("token_source_mismatch")
        elif not _reliable_token_source(variant_results[0].token_source):
            reasons.append("token_source_not_reliable")
        reasons = list(dict.fromkeys(reasons))
        token_comparable = not reasons
        structural_reasons = {
            "subject_model_unavailable",
            "subject_model_mismatch",
            "comparison_basis_mismatch",
            "trial_identity_unavailable",
            "suite_fingerprint_mismatch",
            "subject_commit_mismatch",
            "subject_fingerprint_mismatch",
        }
        structurally_comparable = not (set(reasons) & structural_reasons)
        deltas: list[AblationMetricDelta] = []
        for item in variant_results:
            deltas.append(
                AblationMetricDelta(
                    variant_id=item.variant_id,
                    task_success_changed=(
                        item.task_success_rate != reference.task_success_rate
                    ),
                    retrieval_success_changed=(
                        None
                        if item.retrieval_success_rate is None
                        or reference.retrieval_success_rate is None
                        else item.retrieval_success_rate
                        != reference.retrieval_success_rate
                    ),
                    answer_quality_delta=(
                        None
                        if item.answer_quality_mean is None
                        or reference.answer_quality_mean is None
                        else float(
                            item.answer_quality_mean
                            - reference.answer_quality_mean
                        )
                    ),
                    required_fact_loss_delta=(
                        None
                        if item.required_fact_loss_rate is None
                        or reference.required_fact_loss_rate is None
                        else float(
                            item.required_fact_loss_rate
                            - reference.required_fact_loss_rate
                        )
                    ),
                    distortion_count_delta=(
                        item.distortion_count - reference.distortion_count
                    ),
                    token_delta=(
                        None
                        if not token_comparable
                        or item.total_tokens is None
                        or reference.total_tokens is None
                        else item.total_tokens - reference.total_tokens
                    ),
                    duration_delta_ms=(
                        None
                        if not structurally_comparable
                        else item.duration_ms - reference.duration_ms
                    ),
                )
            )
        comparisons.append(
            AblationComparisonResult(
                case_id=case.case_id,
                reference_variant_id=plan.reference_variant_id,
                variant_results=variant_results,
                comparability=(
                    ComparabilityStatus.COMPARABLE
                    if token_comparable
                    else ComparabilityStatus.NOT_COMPARABLE
                ),
                comparability_reasons=reasons,
                metric_deltas=deltas,
            )
        )
    return comparisons


__all__ = (
    "applicable_checkpoints",
    "applicable_fact_expectations",
    "apply_token_savings",
    "build_ablation_comparisons",
    "build_trial_identity",
    "comparison_basis_fingerprint",
    "configuration_fingerprint",
    "configured_model_identifier",
    "duration_diagnostics",
    "effective_config_overrides",
    "effective_subject_configuration",
    "effective_toolsets",
    "stable_trial_id",
    "subject_identity_fingerprint",
    "token_diagnostics",
)
