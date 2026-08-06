"""P4 Variant configuration, stable identity, diagnostics, and replay comparison."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

from myhermes_audit.contracts.ablation import (
    AblationComparisonResult,
    AblationMetricDelta,
    AblationVariant,
    AblationVariantResult,
    ComparabilityAssessment,
    ComparabilityReason,
    ComparabilityStatus,
    CompressionControl,
    CompressionEvent,
    CompressionMode,
    DiagnosticStatus,
    EffectiveCompressionSemantics,
    DistortionType,
    DurationDiagnostics,
    DurationSource,
    EffectiveSubjectConfiguration,
    MemoryMode,
    ModelIdentifierSource,
    SessionContextMode,
    TokenCountSource,
    TokenCountScope,
    TokenDiagnostics,
    THRESHOLD_DISABLED_COMPRESSION_THRESHOLD,
    TrialIdentity,
    LongConversationCheckpoint,
    RequiredFactExpectation,
)
from myhermes_audit.contracts.result import (
    TrialResult,
    TrialRuntimeSummary,
    TrialObservationSummary,
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
    compression_observation_supported: bool,
    compression_threshold_control: bool = True,
    emergency_overflow_compression_disable_supported: bool = False,
    model_identifier: str = "subject-default",
    model_identifier_source: ModelIdentifierSource = (
        ModelIdentifierSource.SUBJECT_DEFAULT
    ),
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
        if variant.compression_mode is CompressionMode.THRESHOLD_ENABLED
        else THRESHOLD_DISABLED_COMPRESSION_THRESHOLD
    )
    compression_section["threshold"] = threshold
    public_overrides = {"compression": compression_section}
    return EffectiveSubjectConfiguration(
        memory_mode=variant.memory_mode,
        requested_compression_mode=variant.compression_mode,
        effective_compression_semantics=(
            EffectiveCompressionSemantics.THRESHOLD_TRIGGER_ENABLED
            if variant.compression_mode is CompressionMode.THRESHOLD_ENABLED
            else EffectiveCompressionSemantics.THRESHOLD_TRIGGER_DISABLED
        ),
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
        compression_threshold_control=compression_threshold_control,
        emergency_overflow_compression_disable_supported=(
            emergency_overflow_compression_disable_supported
        ),
        emergency_compression_possible=(
            not plan.require_emergency_compression_disable
        ),
        compression_events_observable=compression_observation_supported,
        maximum_turns=plan.maximum_turns,
        maximum_compression_events=plan.maximum_compression_events,
        minimum_compression_events=plan.minimum_compression_events,
        require_emergency_compression_disable=(
            plan.require_emergency_compression_disable
        ),
        model_identifier=model_identifier,
        model_identifier_source=model_identifier_source,
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


_SENSITIVE_IDENTITY_KEY = re.compile(
    r"(?:api[_-]?key|password|secret|credential|access[_-]?token|token|authorization|headers?|cookies?)$",
    re.IGNORECASE,
)
_URL_IDENTITY_KEY = re.compile(r"(?:url|endpoint)$", re.IGNORECASE)
_PATH_IDENTITY_KEY = re.compile(
    r"(?:path|directory|dir|home|workspace)$",
    re.IGNORECASE,
)
_RUNTIME_IDENTITY_KEY = re.compile(
    r"(?:run|trial|sandbox|session)[_-]?id$",
    re.IGNORECASE,
)
_USER_IDENTITY_KEY = re.compile(
    r"(?:user|identity|account|owner)",
    re.IGNORECASE,
)
_CONTENT_IDENTITY_KEY = re.compile(
    r"(?:prompt|output|reasoning|review|content|message|instruction|system|template)",
    re.IGNORECASE,
)


def _safe_identity_projection(value: object, *, key: str | None = None) -> object:
    """Project configuration facts without secrets or machine-local paths."""

    if isinstance(value, Enum):
        return value.value
    if key is not None and _SENSITIVE_IDENTITY_KEY.search(key):
        if isinstance(value, str):
            reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
            if reference is not None:
                return {"environment_reference": reference.group(1)}
        return "<redacted>"
    if key is not None and _PATH_IDENTITY_KEY.search(key):
        return "<path>"
    if key is not None and _RUNTIME_IDENTITY_KEY.search(key):
        return "<runtime-id>"
    if key is not None and _USER_IDENTITY_KEY.search(key):
        return "<user-fact>"
    if key is not None and _CONTENT_IDENTITY_KEY.search(key):
        if isinstance(value, str):
            return {
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "length": len(value),
            }
        return {
            "sha256": canonical_sha256(_safe_identity_projection(value)),
            "kind": "content",
        }
    if value is None or type(value) in (str, int, bool):
        if isinstance(value, str):
            if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/)", value):
                return "<path>"
            parts = urlsplit(value)
            if parts.scheme and parts.netloc and (
                (key is not None and _URL_IDENTITY_KEY.search(key))
                or parts.username is not None
                or parts.password is not None
            ):
                hostname = parts.hostname or ""
                port = ""
                try:
                    if parts.port is not None:
                        port = f":{parts.port}"
                except ValueError:
                    return "<url>"
                return urlunsplit((parts.scheme, hostname + port, parts.path, "", ""))
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("configuration identity numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_identity_projection(child, key=str(child_key))
            for child_key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_identity_projection(child) for child in value]
    if hasattr(value, "model_dump"):
        return _safe_identity_projection(value.model_dump(mode="python"))
    raise ValueError(
        "configuration identity contains an unsupported value type"
    )


def configuration_fingerprint(
    case: AuditCase,
    variant: AblationVariant | None,
    configuration: EffectiveSubjectConfiguration | None = None,
    *,
    prepared_subject_configuration: Mapping[str, object] | None = None,
    model_identifier: str | None = None,
) -> str:
    if variant is not None:
        if configuration is None:
            raise AblationVariantError(
                "Variant configuration is required for its fingerprint",
                case_id=case.case_id,
                variant_id=variant.variant_id,
            )
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
    if prepared_subject_configuration is None or not model_identifier:
        raise ValueError(
            "base configuration fingerprint requires prepared Subject config and model"
        )
    return canonical_sha256(
        {
            "variant_id": None,
            "ablation_state": "base",
            "execution": _safe_identity_projection(case.execution),
            "prepared_subject_configuration": _safe_identity_projection(
                prepared_subject_configuration
            ),
            "effective_toolsets": [item.value for item in case.execution.enabled_toolsets],
            "memory_strategy": (
                None
                if case.execution.memory_strategy is None
                else case.execution.memory_strategy.value
            ),
            "model_identifier": model_identifier,
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


def build_trial_identity(
    *,
    suite_sha256: str,
    case: AuditCase,
    variant: AblationVariant | None,
    trial_ordinal: int,
    subject_fingerprint: SubjectFingerprint,
    configuration: EffectiveSubjectConfiguration | None = None,
    prepared_subject_configuration: Mapping[str, object] | None = None,
    model_identifier: str | None = None,
) -> TrialIdentity:
    config_sha256 = configuration_fingerprint(
        case,
        variant,
        configuration,
        prepared_subject_configuration=prepared_subject_configuration,
        model_identifier=model_identifier,
    )
    effective_model_identifier = (
        configuration.model_identifier
        if configuration is not None
        else model_identifier
    )
    if not effective_model_identifier:
        raise ValueError("Trial identity requires an effective model identifier")
    payload = {
        "suite_sha256": suite_sha256,
        "case_id": case.case_id,
        "variant_id": None if variant is None else variant.variant_id,
        "trial_ordinal": trial_ordinal,
        "subject_commit": subject_fingerprint.git_commit,
        "subject_fingerprint_sha256": subject_identity_fingerprint(
            subject_fingerprint
        ),
        "configuration_sha256": config_sha256,
        "model_identifier": effective_model_identifier,
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
    observations: TrialObservationSummary | None = None,
) -> TokenDiagnostics:
    model_call_count = (
        len(observations.model_calls)
        if observations is not None and not observations.truncated
        else None
    )
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
            scope=TokenCountScope.SUBJECT_TRIAL_MODEL_CALLS,
            model_call_count=model_call_count,
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
        scope=TokenCountScope.SUBJECT_TRIAL_MODEL_CALLS,
        model_call_count=model_call_count,
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


def _comparable_token_source(source: TokenCountSource) -> bool:
    return source is not TokenCountSource.UNAVAILABLE


def _unique_reasons(
    reasons: Sequence[ComparabilityReason],
) -> list[ComparabilityReason]:
    return list(dict.fromkeys(reasons))


def _trial_structural_reasons(
    trial: TrialResult,
    reference: TrialResult,
) -> list[ComparabilityReason]:
    reasons: list[ComparabilityReason] = []
    if (
        trial.case_id != reference.case_id
        or trial.comparison_basis_fingerprint
        != reference.comparison_basis_fingerprint
        or trial.comparison_basis_fingerprint is None
    ):
        reasons.append(ComparabilityReason.COMPARISON_BASIS_MISMATCH)
    identity = trial.trial_identity
    reference_identity = reference.trial_identity
    if identity is None or reference_identity is None:
        reasons.append(ComparabilityReason.TRIAL_IDENTITY_UNAVAILABLE)
        return _unique_reasons(reasons)
    if identity.suite_sha256 != reference_identity.suite_sha256:
        reasons.append(ComparabilityReason.SUITE_FINGERPRINT_MISMATCH)
    if identity.subject_commit != reference_identity.subject_commit:
        reasons.append(ComparabilityReason.SUBJECT_COMMIT_MISMATCH)
    if (
        identity.subject_fingerprint_sha256
        != reference_identity.subject_fingerprint_sha256
    ):
        reasons.append(ComparabilityReason.SUBJECT_FINGERPRINT_MISMATCH)
    if identity.model_identifier != reference_identity.model_identifier:
        reasons.append(ComparabilityReason.MODEL_IDENTIFIER_MISMATCH)
    return _unique_reasons(reasons)


def _trial_token_reasons(
    trial: TrialResult,
    reference: TrialResult,
) -> list[ComparabilityReason]:
    if _trial_structural_reasons(trial, reference):
        return [ComparabilityReason.STRUCTURAL_INCOMPARABILITY]
    current = trial.token_diagnostics
    baseline = reference.token_diagnostics
    reasons: list[ComparabilityReason] = []
    if (
        current is None
        or baseline is None
        or current.total_tokens is None
        or baseline.total_tokens is None
    ):
        reasons.append(ComparabilityReason.TOKEN_DATA_UNAVAILABLE)
    if current is not None and baseline is not None:
        if current.source is not baseline.source:
            reasons.append(ComparabilityReason.TOKEN_SOURCE_MISMATCH)
        elif not _comparable_token_source(current.source):
            reasons.append(ComparabilityReason.TOKEN_SOURCE_UNSUPPORTED)
        if current.scope is not baseline.scope:
            reasons.append(ComparabilityReason.TOKEN_SCOPE_MISMATCH)
        if current.model_call_count is None or baseline.model_call_count is None:
            reasons.append(
                ComparabilityReason.TOKEN_MODEL_CALL_COUNT_UNAVAILABLE
            )
        elif current.model_call_count != baseline.model_call_count:
            reasons.append(ComparabilityReason.TOKEN_MODEL_CALL_COUNT_MISMATCH)
    return _unique_reasons(reasons)


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
        if reference is None or _trial_token_reasons(trial, reference):
            updated.append(trial)
            continue
        current_tokens = trial.token_diagnostics
        reference_tokens = reference.token_diagnostics
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
    model_identifiers = {
        item.trial_identity.model_identifier
        for item in trials
        if item.trial_identity is not None
    }
    if len(model_identifiers) != 1:
        raise AblationComparisonError(
            "Ablation Variant has inconsistent effective model identifiers",
            variant_id=variant.variant_id,
        )
    retrieval_values = [
        float(item.retrieval_gate_passed is True)
        for item in trials
        if item.retrieval_gate_passed is not None
    ]
    judge_results = [item.judge_result for item in trials if item.judge_result is not None]
    answer_values = [float(item.overall_score) for item in judge_results]
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
    token_sources = {item.source for item in token_records if item is not None}
    token_scopes = {item.scope for item in token_records if item is not None}
    token_source = (
        next(iter(token_sources))
        if len(token_sources) == 1
        else TokenCountSource.UNAVAILABLE
    )
    token_scope = next(iter(token_scopes)) if len(token_scopes) == 1 else None
    total_tokens = (
        sum(item.total_tokens for item in token_records if item is not None)
        if len(token_records) == len(trials)
        and all(item is not None and item.total_tokens is not None for item in token_records)
        else None
    )
    model_call_count = (
        sum(item.model_call_count for item in token_records if item is not None)
        if len(token_records) == len(trials)
        and all(
            item is not None and item.model_call_count is not None
            for item in token_records
        )
        else None
    )
    duration_ms = (
        sum(item.duration_ms for item in trials if item.duration_ms is not None)
        if all(item.duration_ms is not None for item in trials)
        else None
    )
    return AblationVariantResult(
        variant_id=variant.variant_id,
        memory_mode=variant.memory_mode,
        requested_compression_mode=variant.compression_mode,
        trial_ids=[item.trial_id for item in trials],
        configuration_sha256=next(iter(configuration_values)),
        model_identifier=next(iter(model_identifiers)),
        task_success_rate=float(
            sum(item.task_passed is True for item in trials) / len(trials)
        ),
        retrieval_success_rate=_mean(retrieval_values),
        answer_quality_mean=_mean(answer_values),
        judge_completed_trial_count=len(judge_results),
        judge_prompt_versions=list(
            dict.fromkeys(item.prompt_version for item in judge_results)
        ),
        judge_model_identifiers=list(
            dict.fromkeys(item.judge_model for item in judge_results)
        ),
        required_fact_loss_rate=(
            None if fact_required == 0 else float(fact_lost / fact_required)
        ),
        distortion_count=sum(
            result.distortion_type is not DistortionType.NOT_EVALUABLE
            for item in trials
            for result in item.distortion_results
        ),
        total_tokens=total_tokens,
        duration_ms=duration_ms,
        token_source=token_source,
        token_scope=token_scope,
        model_call_count=model_call_count,
    )


def _assessment(
    reasons: Sequence[ComparabilityReason],
) -> ComparabilityAssessment:
    normalized = _unique_reasons(reasons)
    return ComparabilityAssessment(
        status=(
            ComparabilityStatus.COMPARABLE
            if not normalized
            else ComparabilityStatus.NOT_COMPARABLE
        ),
        reasons=normalized,
    )


def _structural_reasons(
    trials: Sequence[TrialResult],
) -> list[ComparabilityReason]:
    reasons: list[ComparabilityReason] = []
    basis_values = {item.comparison_basis_fingerprint for item in trials}
    if len(basis_values) != 1 or None in basis_values:
        reasons.append(ComparabilityReason.COMPARISON_BASIS_MISMATCH)
    identities = [item.trial_identity for item in trials]
    if any(item is None for item in identities):
        reasons.append(ComparabilityReason.TRIAL_IDENTITY_UNAVAILABLE)
        return _unique_reasons(reasons)
    concrete = [item for item in identities if item is not None]
    if len({item.suite_sha256 for item in concrete}) != 1:
        reasons.append(ComparabilityReason.SUITE_FINGERPRINT_MISMATCH)
    if len({item.subject_commit for item in concrete}) != 1:
        reasons.append(ComparabilityReason.SUBJECT_COMMIT_MISMATCH)
    if len({item.subject_fingerprint_sha256 for item in concrete}) != 1:
        reasons.append(ComparabilityReason.SUBJECT_FINGERPRINT_MISMATCH)
    if len({item.model_identifier for item in concrete}) != 1:
        reasons.append(ComparabilityReason.MODEL_IDENTIFIER_MISMATCH)
    return _unique_reasons(reasons)


def _token_reasons(
    trials: Sequence[TrialResult],
    *,
    structural_comparable: bool,
) -> list[ComparabilityReason]:
    if not structural_comparable:
        return [ComparabilityReason.STRUCTURAL_INCOMPARABILITY]
    diagnostics = [item.token_diagnostics for item in trials]
    reasons: list[ComparabilityReason] = []
    if any(item is None or item.total_tokens is None for item in diagnostics):
        return [ComparabilityReason.TOKEN_DATA_UNAVAILABLE]
    concrete = [item for item in diagnostics if item is not None]
    sources = {item.source for item in concrete}
    if len(sources) != 1:
        reasons.append(ComparabilityReason.TOKEN_SOURCE_MISMATCH)
    elif not sources or not _comparable_token_source(next(iter(sources))):
        reasons.append(ComparabilityReason.TOKEN_SOURCE_UNSUPPORTED)
    if len({item.scope for item in concrete}) != 1:
        reasons.append(ComparabilityReason.TOKEN_SCOPE_MISMATCH)
    call_counts = [item.model_call_count for item in concrete]
    if len(concrete) != len(trials) or any(item is None for item in call_counts):
        reasons.append(ComparabilityReason.TOKEN_MODEL_CALL_COUNT_UNAVAILABLE)
    elif len(set(call_counts)) != 1:
        reasons.append(ComparabilityReason.TOKEN_MODEL_CALL_COUNT_MISMATCH)
    return _unique_reasons(reasons)


def _answer_quality_reasons(
    variants: Sequence[AblationVariantResult],
    *,
    structural_comparable: bool,
) -> list[ComparabilityReason]:
    if not structural_comparable:
        return [ComparabilityReason.STRUCTURAL_INCOMPARABILITY]
    reasons: list[ComparabilityReason] = []
    if any(
        item.judge_completed_trial_count != len(item.trial_ids)
        for item in variants
    ):
        return [ComparabilityReason.JUDGE_RESULT_UNAVAILABLE]
    prompt_versions = {
        version for item in variants for version in item.judge_prompt_versions
    }
    if len(prompt_versions) != 1:
        reasons.append(ComparabilityReason.JUDGE_PROMPT_VERSION_MISMATCH)
    model_identifiers = {
        model for item in variants for model in item.judge_model_identifiers
    }
    if len(model_identifiers) != 1:
        reasons.append(ComparabilityReason.JUDGE_MODEL_IDENTIFIER_MISMATCH)
    return _unique_reasons(reasons)


def _duration_reasons(
    variants: Sequence[AblationVariantResult],
    *,
    structural_comparable: bool,
) -> list[ComparabilityReason]:
    if not structural_comparable:
        return [ComparabilityReason.STRUCTURAL_INCOMPARABILITY]
    if any(item.duration_ms is None for item in variants):
        return [ComparabilityReason.DURATION_DATA_UNAVAILABLE]
    return []


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
        structural_reasons = _structural_reasons(case_trials)
        structural_comparable = not structural_reasons
        token_reasons = _token_reasons(
            case_trials,
            structural_comparable=structural_comparable,
        )
        answer_reasons = _answer_quality_reasons(
            variant_results,
            structural_comparable=structural_comparable,
        )
        duration_reasons = _duration_reasons(
            variant_results,
            structural_comparable=structural_comparable,
        )
        token_comparable = not token_reasons
        answer_comparable = not answer_reasons
        duration_comparable = not duration_reasons
        deltas: list[AblationMetricDelta] = []
        for item in variant_results:
            if not structural_comparable:
                deltas.append(AblationMetricDelta(variant_id=item.variant_id))
                continue
            token_delta = (
                item.total_tokens - reference.total_tokens
                if token_comparable
                and item.total_tokens is not None
                and reference.total_tokens is not None
                else None
            )
            token_savings = None if token_delta is None else -token_delta
            token_savings_rate = (
                None
                if token_savings is None
                or reference.total_tokens in (None, 0)
                else float(token_savings / reference.total_tokens)
            )
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
                        if not answer_comparable
                        or item.answer_quality_mean is None
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
                    token_delta=token_delta,
                    token_savings=token_savings,
                    token_savings_rate=token_savings_rate,
                    duration_delta_ms=(
                        item.duration_ms - reference.duration_ms
                        if duration_comparable
                        and item.duration_ms is not None
                        and reference.duration_ms is not None
                        else None
                    ),
                )
            )
        comparisons.append(
            AblationComparisonResult(
                case_id=case.case_id,
                reference_variant_id=plan.reference_variant_id,
                variant_results=variant_results,
                structural_comparability=_assessment(structural_reasons),
                token_comparability=_assessment(token_reasons),
                answer_quality_comparability=_assessment(answer_reasons),
                duration_comparability=_assessment(duration_reasons),
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
    "duration_diagnostics",
    "effective_config_overrides",
    "effective_subject_configuration",
    "effective_toolsets",
    "stable_trial_id",
    "subject_identity_fingerprint",
    "token_diagnostics",
)
