"""P4 Memory/Compression ablation declarations and local result facts."""

from __future__ import annotations

import hashlib
import math
from enum import Enum

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    Sha256Digest,
)
from myhermes_audit.contracts.memory import RetrievalStrategy
from myhermes_audit.serialization import canonical_sha256


DISABLED_COMPRESSION_THRESHOLD = 2_147_483_647
_ALLOWED_COMPRESSION_OVERRIDE_KEYS = frozenset(
    {
        "threshold",
        "protect_first",
        "keep_recent_tool_results",
        "tail_token_budget",
    }
)


class MemoryMode(str, Enum):
    NO_MEMORY = "no_memory"
    SHORT_TERM_ONLY = "short_term_only"
    LONG_TERM_ONLY = "long_term_only"
    SHORT_AND_LONG_TERM = "short_and_long_term"


class CompressionMode(str, Enum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class FactMatchMode(str, Enum):
    EXACT = "exact"
    NORMALIZED_EXACT = "normalized_exact"
    CONTAINS = "contains"


class RequiredFactScope(str, Enum):
    SUBJECT_CONTEXT = "subject_context"
    LONG_TERM_MEMORY = "long_term_memory"
    FINAL_ANSWER = "final_answer"


class DistortionType(str, Enum):
    MISSING = "missing"
    CONTRADICTED = "contradicted"
    VALUE_CHANGED = "value_changed"
    ENTITY_CHANGED = "entity_changed"
    TEMPORAL_ORDER_CHANGED = "temporal_order_changed"
    UNSUPPORTED_ADDITION = "unsupported_addition"
    NOT_EVALUABLE = "not_evaluable"


class FactRetentionStatus(str, Enum):
    RETAINED = "retained"
    LOST = "lost"
    ABSENT = "absent"
    PRESENT_WHEN_FORBIDDEN = "present_when_forbidden"
    NOT_EVALUABLE = "not_evaluable"
    ERROR = "error"


class DiagnosticStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


class TokenCountSource(str, Enum):
    SUBJECT_REPORTED = "subject_reported"
    PROVIDER_REPORTED = "provider_reported"
    AUDIT_ESTIMATED = "audit_estimated"
    UNAVAILABLE = "unavailable"


class DurationSource(str, Enum):
    AUDIT_MEASURED = "audit_measured"
    SUBJECT_REPORTED = "subject_reported"
    UNAVAILABLE = "unavailable"


class SessionContextMode(str, Enum):
    ISOLATED_PER_TURN = "isolated_per_turn"
    SUBJECT_SESSION = "subject_session"


class CompressionControl(str, Enum):
    THRESHOLD_CONFIGURATION = "threshold_configuration"
    PUBLIC_TOGGLE = "public_toggle"
    UNAVAILABLE = "unavailable"


class CompressionEventStatus(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"
    NOT_EVALUABLE = "not_evaluable"


class ComparabilityStatus(str, Enum):
    COMPARABLE = "comparable"
    NOT_COMPARABLE = "not_comparable"


class DistortionCandidate(ContractModel):
    value: NonEmptyText
    distortion_type: DistortionType

    @model_validator(mode="after")
    def validate_candidate(self) -> "DistortionCandidate":
        if self.distortion_type in {
            DistortionType.MISSING,
            DistortionType.NOT_EVALUABLE,
        }:
            raise ValueError(
                "distortion candidates require an explicit observed distortion type"
            )
        return self


class RequiredFact(ContractModel):
    fact_id: Identifier
    canonical_value: NonEmptyText
    accepted_variants: list[NonEmptyText]
    match: FactMatchMode = FactMatchMode.NORMALIZED_EXACT
    scope: RequiredFactScope
    checkpoint_id: Identifier | None = None
    must_survive_compression: StrictBool = False
    must_survive_session_change: StrictBool = False
    must_be_absent: StrictBool = False
    distortion_candidates: list[DistortionCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fact(self) -> "RequiredFact":
        accepted = [self.canonical_value, *self.accepted_variants]
        if len(accepted) != len(set(accepted)):
            raise ValueError("canonical and accepted fact values must not repeat")
        candidates = [item.value for item in self.distortion_candidates]
        if len(candidates) != len(set(candidates)):
            raise ValueError("distortion candidate values must not repeat")
        if set(accepted) & set(candidates):
            raise ValueError(
                "accepted fact values and distortion candidates must be disjoint"
            )
        if self.must_be_absent and (
            self.must_survive_compression or self.must_survive_session_change
        ):
            raise ValueError("absent facts cannot declare survival requirements")
        if self.scope is RequiredFactScope.SUBJECT_CONTEXT:
            if self.checkpoint_id is None:
                raise ValueError("subject_context facts require checkpoint_id")
        elif self.checkpoint_id is not None:
            raise ValueError("checkpoint_id is only valid for subject_context facts")
        return self


class RequiredFactExpectation(ContractModel):
    expectation_id: Identifier
    required: StrictBool = True
    distortion_hard_gate: StrictBool = False
    applicable_variant_ids: list[Identifier] = Field(default_factory=list)
    facts: list[RequiredFact] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expectation(self) -> "RequiredFactExpectation":
        fact_ids = [item.fact_id for item in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_id must be unique within an expectation")
        if len(self.applicable_variant_ids) != len(
            set(self.applicable_variant_ids)
        ):
            raise ValueError("applicable_variant_ids must not repeat")
        return self


class LongConversationCheckpoint(ContractModel):
    checkpoint_id: Identifier
    after_turn: PositiveInt
    required_fact_ids: list[Identifier] = Field(default_factory=list)
    expected_answer: NonEmptyText | None = None
    answer_match: FactMatchMode = FactMatchMode.NORMALIZED_EXACT
    required: StrictBool = True

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "LongConversationCheckpoint":
        if len(self.required_fact_ids) != len(set(self.required_fact_ids)):
            raise ValueError("checkpoint required_fact_ids must not repeat")
        if not self.required_fact_ids and self.expected_answer is None:
            raise ValueError(
                "checkpoint requires required_fact_ids or expected_answer"
            )
        return self


def _compression_overrides(value: JsonObject) -> dict[str, object]:
    unknown_roots = set(value) - {"compression"}
    if unknown_roots:
        raise ValueError(
            "ablation config_overrides only supports public compression paths"
        )
    section = value.get("compression", {})
    if not isinstance(section, dict):
        raise ValueError("compression override must be a mapping")
    unknown = set(section) - _ALLOWED_COMPRESSION_OVERRIDE_KEYS
    if unknown:
        raise ValueError(
            "unsupported compression override paths: " + ", ".join(sorted(unknown))
        )
    for key, item in section.items():
        if type(item) is not int or item < 0:
            raise ValueError(f"compression.{key} must be a non-negative integer")
    threshold = section.get("threshold")
    if threshold is not None and threshold < 1:
        raise ValueError("compression.threshold must be positive")
    return section


class AblationVariant(ContractModel):
    variant_id: Identifier
    memory_mode: MemoryMode
    compression_mode: CompressionMode
    config_overrides: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_variant(self) -> "AblationVariant":
        compression = _compression_overrides(self.config_overrides)
        threshold = compression.get("threshold")
        if self.compression_mode is CompressionMode.ENABLED:
            if threshold is None:
                raise ValueError(
                    "compression_enabled variants must explicitly set "
                    "config_overrides.compression.threshold"
                )
            if threshold >= DISABLED_COMPRESSION_THRESHOLD:
                raise ValueError("enabled compression threshold is not actionable")
        elif threshold not in (None, DISABLED_COMPRESSION_THRESHOLD):
            raise ValueError(
                "compression_disabled uses the framework's fixed public threshold"
            )
        return self


def _normalized_variant_overrides(item: AblationVariant) -> JsonObject:
    compression = dict(_compression_overrides(item.config_overrides))
    compression["threshold"] = (
        compression["threshold"]
        if item.compression_mode is CompressionMode.ENABLED
        else DISABLED_COMPRESSION_THRESHOLD
    )
    return {"compression": compression}


class AblationPlan(ContractModel):
    variants: list[AblationVariant] = Field(min_length=1)
    reference_variant_id: Identifier
    maximum_turns: PositiveInt
    maximum_compression_events: NonNegativeInt
    checkpoints: list[LongConversationCheckpoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan(self) -> "AblationPlan":
        variant_ids = [item.variant_id for item in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("variant_id must be unique within an AblationPlan")
        if self.reference_variant_id not in set(variant_ids):
            raise ValueError("reference_variant_id must name a declared variant")
        combinations = [
            (
                item.memory_mode.value,
                item.compression_mode.value,
                canonical_sha256(_normalized_variant_overrides(item)),
            )
            for item in self.variants
        ]
        if len(combinations) != len(set(combinations)):
            raise ValueError("Ablation variants must not repeat a configuration")
        checkpoint_ids = [item.checkpoint_id for item in self.checkpoints]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("checkpoint_id must be unique within an AblationPlan")
        if any(item.after_turn > self.maximum_turns for item in self.checkpoints):
            raise ValueError("checkpoint after_turn cannot exceed maximum_turns")
        return self


class EffectiveSubjectConfiguration(ContractModel):
    memory_mode: MemoryMode
    compression_mode: CompressionMode
    session_context_mode: SessionContextMode
    include_memory: StrictBool
    include_user_profile: StrictBool
    memory_tool_enabled: StrictBool
    memory_strategy: RetrievalStrategy | None = None
    compression_control: CompressionControl
    compression_threshold: PositiveInt | None = None
    compression_observation_available: StrictBool = False
    maximum_turns: PositiveInt
    maximum_compression_events: NonNegativeInt
    public_config_overrides: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_configuration(self) -> "EffectiveSubjectConfiguration":
        long_term = self.memory_mode in {
            MemoryMode.LONG_TERM_ONLY,
            MemoryMode.SHORT_AND_LONG_TERM,
        }
        short_term = self.memory_mode in {
            MemoryMode.SHORT_TERM_ONLY,
            MemoryMode.SHORT_AND_LONG_TERM,
        }
        if self.include_memory != long_term or self.include_user_profile != long_term:
            raise ValueError("Memory prompt projection must match memory_mode")
        if self.memory_tool_enabled and not long_term:
            raise ValueError("memory tool requires a long-term Memory mode")
        if long_term != (self.memory_strategy is not None):
            raise ValueError("long-term Memory modes require exactly one strategy")
        expected_session_mode = (
            SessionContextMode.SUBJECT_SESSION
            if short_term
            else SessionContextMode.ISOLATED_PER_TURN
        )
        if self.session_context_mode is not expected_session_mode:
            raise ValueError("session context projection must match memory_mode")
        if self.compression_control is CompressionControl.UNAVAILABLE:
            if self.compression_threshold is not None:
                raise ValueError("unavailable compression cannot name a threshold")
        elif self.compression_threshold is None:
            raise ValueError("configured compression requires a threshold")
        _compression_overrides(self.public_config_overrides)
        return self


class TrialIdentity(ContractModel):
    suite_sha256: Sha256Digest
    case_id: Identifier
    variant_id: Identifier
    trial_ordinal: PositiveInt
    subject_commit: NonEmptyText
    subject_fingerprint_sha256: Sha256Digest
    configuration_sha256: Sha256Digest
    model_identifier: NonEmptyText
    identity_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_identity(self) -> "TrialIdentity":
        expected = canonical_sha256(
            {
                "suite_sha256": self.suite_sha256,
                "case_id": self.case_id,
                "variant_id": self.variant_id,
                "trial_ordinal": self.trial_ordinal,
                "subject_commit": self.subject_commit,
                "subject_fingerprint_sha256": self.subject_fingerprint_sha256,
                "configuration_sha256": self.configuration_sha256,
                "model_identifier": self.model_identifier,
            }
        )
        if self.identity_sha256 != expected:
            raise ValueError("identity_sha256 must match the stable Trial identity")
        return self


class FactProjection(ContractModel):
    sha256: Sha256Digest
    length: NonNegativeInt
    value: StrictStr | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "FactProjection":
        if self.value is not None:
            encoded = self.value.encode("utf-8")
            if self.length != len(self.value):
                raise ValueError("FactProjection length must match value")
            if self.sha256 != hashlib.sha256(encoded).hexdigest():
                raise ValueError("FactProjection sha256 must match value")
        return self


class FactContextObservation(ContractModel):
    fact_id: Identifier
    checkpoint_id: Identifier
    turn_index: PositiveInt
    session_id: Identifier | None = None
    matched: StrictBool | None = None
    matched_projection: FactProjection | None = None
    distortion_type: DistortionType | None = None
    distortion_projection: FactProjection | None = None
    compression_applied: StrictBool | None = None
    session_changed: StrictBool
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> "FactContextObservation":
        if self.matched is True and self.matched_projection is None:
            raise ValueError("matched fact observations require a projection")
        if self.matched is not True and self.matched_projection is not None:
            raise ValueError("unmatched fact observations cannot contain a projection")
        if (self.distortion_type is None) != (self.distortion_projection is None):
            raise ValueError(
                "fact distortion type and projection must be present together"
            )
        if self.matched is True and self.distortion_type is not None:
            raise ValueError("retained facts cannot also contain a distortion")
        if self.matched is None and self.error_type is None:
            raise ValueError("unevaluable fact observations require error_type")
        if self.matched is not None and self.error_type is not None:
            raise ValueError("evaluated fact observations cannot contain error_type")
        return self


class CompressionEvent(ContractModel):
    event_id: Identifier
    session_id: Identifier | None = None
    turn_index: PositiveInt | None = None
    trigger: NonEmptyText | None = None
    input_message_count: NonNegativeInt | None = None
    output_message_count: NonNegativeInt | None = None
    input_token_count: NonNegativeInt | None = None
    output_token_count: NonNegativeInt | None = None
    saved_token_count: NonNegativeInt | None = None
    duration_ms: NonNegativeInt | None = None
    status: CompressionEventStatus
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_event(self) -> "CompressionEvent":
        if (
            self.input_token_count is not None
            and self.output_token_count is not None
            and self.saved_token_count is not None
            and self.saved_token_count
            != max(0, self.input_token_count - self.output_token_count)
        ):
            raise ValueError("saved_token_count must match input-output tokens")
        if self.status is CompressionEventStatus.ERROR:
            if self.error_type is None:
                raise ValueError("compression event errors require error_type")
        elif self.error_type is not None:
            raise ValueError("non-error compression events cannot contain error_type")
        return self


class ContextDiagnostic(ContractModel):
    session_id: Identifier
    turn_index: PositiveInt
    message_count: NonNegativeInt | None = None
    estimated_or_reported_token_count: NonNegativeInt | None = None
    token_source: TokenCountSource
    compression_applied: StrictBool | None = None
    session_changed: StrictBool
    status: DiagnosticStatus = DiagnosticStatus.AVAILABLE
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_diagnostic(self) -> "ContextDiagnostic":
        if self.token_source is TokenCountSource.UNAVAILABLE:
            if self.estimated_or_reported_token_count is not None:
                raise ValueError("unavailable token source cannot report tokens")
        elif self.estimated_or_reported_token_count is None:
            raise ValueError("available token source requires a token count")
        if self.status is DiagnosticStatus.ERROR:
            if self.error_type is None:
                raise ValueError("context diagnostic errors require error_type")
        elif self.error_type is not None:
            raise ValueError("non-error context diagnostics cannot contain error_type")
        return self


class FactRetentionResult(ContractModel):
    expectation_id: Identifier
    fact_id: Identifier
    status: FactRetentionStatus
    scope: RequiredFactScope
    checkpoint_id: Identifier | None = None
    evidence_source: NonEmptyText
    expected_projection: FactProjection
    actual_projection: FactProjection | None = None
    hard_gate: StrictBool
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "FactRetentionResult":
        if self.status is FactRetentionStatus.ERROR:
            if self.error_type is None:
                raise ValueError("fact retention errors require error_type")
        elif self.error_type is not None:
            raise ValueError("non-error fact retention cannot contain error_type")
        return self


class RequiredFactLossResult(ContractModel):
    status: DiagnosticStatus
    required_fact_count: NonNegativeInt | None = None
    retained_required_fact_count: NonNegativeInt | None = None
    lost_required_fact_ids: list[Identifier] = Field(default_factory=list)
    required_fact_loss_count: NonNegativeInt | None = None
    required_fact_loss_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_loss(self) -> "RequiredFactLossResult":
        values = (
            self.required_fact_count,
            self.retained_required_fact_count,
            self.required_fact_loss_count,
            self.required_fact_loss_rate,
        )
        if self.status is DiagnosticStatus.NOT_APPLICABLE:
            if any(item is not None for item in values) or self.lost_required_fact_ids:
                raise ValueError("not-applicable fact loss cannot contain counts")
            return self
        if self.status is DiagnosticStatus.ERROR:
            if self.error_type is None:
                raise ValueError("fact loss errors require error_type")
            return self
        if self.error_type is not None:
            raise ValueError("non-error fact loss cannot contain error_type")
        if any(item is None for item in values):
            raise ValueError("available fact loss requires every count and rate")
        assert self.required_fact_count is not None
        assert self.retained_required_fact_count is not None
        assert self.required_fact_loss_count is not None
        assert self.required_fact_loss_rate is not None
        if self.required_fact_count == 0:
            raise ValueError("zero required facts must be not_applicable")
        if (
            self.retained_required_fact_count + self.required_fact_loss_count
            != self.required_fact_count
        ):
            raise ValueError("retained and lost facts must cover required facts")
        if self.required_fact_loss_count != len(self.lost_required_fact_ids):
            raise ValueError("lost fact IDs must match required_fact_loss_count")
        expected_rate = self.required_fact_loss_count / self.required_fact_count
        if not math.isclose(
            self.required_fact_loss_rate,
            expected_rate,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("required_fact_loss_rate must equal lost / required")
        return self


class DistortionResult(ContractModel):
    expectation_id: Identifier
    fact_id: Identifier
    distortion_type: DistortionType
    expected_projection: FactProjection
    actual_projection: FactProjection | None = None
    evidence_source: NonEmptyText
    hard_gate: StrictBool


class CheckpointResult(ContractModel):
    checkpoint_id: Identifier
    after_turn: PositiveInt
    required_fact_ids: list[Identifier] = Field(default_factory=list)
    fact_gate_passed: StrictBool | None = None
    answer_gate_passed: StrictBool | None = None
    context_diagnostic_available: StrictBool
    compression_applied: StrictBool | None = None


class TokenDiagnostics(ContractModel):
    status: DiagnosticStatus
    source: TokenCountSource
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    compression_input_tokens: NonNegativeInt | None = None
    compression_output_tokens: NonNegativeInt | None = None
    token_savings: StrictInt | None = None
    token_savings_rate: StrictFloat | None = None
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_tokens(self) -> "TokenDiagnostics":
        if (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if self.source is TokenCountSource.UNAVAILABLE and any(
            item is not None
            for item in (self.input_tokens, self.output_tokens, self.total_tokens)
        ):
            raise ValueError("unavailable token source cannot contain token counts")
        if self.status is DiagnosticStatus.ERROR:
            if self.error_type is None:
                raise ValueError("token diagnostic errors require error_type")
        elif self.error_type is not None:
            raise ValueError("non-error token diagnostics cannot contain error_type")
        if self.token_savings_rate is not None and not math.isfinite(
            self.token_savings_rate
        ):
            raise ValueError("token_savings_rate must be finite")
        return self


class DurationDiagnostics(ContractModel):
    status: DiagnosticStatus
    trial_duration_ms: NonNegativeInt | None = None
    trial_duration_source: DurationSource
    retrieval_duration_ms: NonNegativeInt | None = None
    retrieval_duration_source: DurationSource
    compression_duration_ms: NonNegativeInt | None = None
    compression_duration_source: DurationSource
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_duration(self) -> "DurationDiagnostics":
        if self.status is DiagnosticStatus.ERROR:
            if self.error_type is None:
                raise ValueError("duration diagnostic errors require error_type")
        elif self.error_type is not None:
            raise ValueError("non-error duration diagnostics cannot contain error_type")
        for name, value, source in (
            (
                "trial_duration",
                self.trial_duration_ms,
                self.trial_duration_source,
            ),
            (
                "retrieval_duration",
                self.retrieval_duration_ms,
                self.retrieval_duration_source,
            ),
            (
                "compression_duration",
                self.compression_duration_ms,
                self.compression_duration_source,
            ),
        ):
            if value is None and source is not DurationSource.UNAVAILABLE:
                raise ValueError(f"{name} without a value must be unavailable")
            if value is not None and source is DurationSource.UNAVAILABLE:
                raise ValueError(f"{name} with a value requires a source")
        return self


class AblationVariantResult(ContractModel):
    variant_id: Identifier
    memory_mode: MemoryMode
    compression_mode: CompressionMode
    trial_ids: list[Identifier] = Field(min_length=1)
    configuration_sha256: Sha256Digest
    subject_model: NonEmptyText | None = None
    task_success_rate: StrictFloat = Field(ge=0, le=1)
    retrieval_success_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    answer_quality_mean: StrictFloat | None = None
    required_fact_loss_rate: StrictFloat | None = Field(default=None, ge=0, le=1)
    distortion_count: NonNegativeInt
    total_tokens: NonNegativeInt | None = None
    duration_ms: NonNegativeInt
    token_source: TokenCountSource

    @model_validator(mode="after")
    def validate_variant_result(self) -> "AblationVariantResult":
        if len(self.trial_ids) != len(set(self.trial_ids)):
            raise ValueError("Ablation variant trial_ids must not repeat")
        return self


class AblationMetricDelta(ContractModel):
    variant_id: Identifier
    task_success_changed: StrictBool | None = None
    retrieval_success_changed: StrictBool | None = None
    answer_quality_delta: StrictFloat | None = None
    required_fact_loss_delta: StrictFloat | None = None
    distortion_count_delta: StrictInt | None = None
    token_delta: StrictInt | None = None
    duration_delta_ms: StrictInt | None = None


class AblationComparisonResult(ContractModel):
    case_id: Identifier
    reference_variant_id: Identifier
    variant_results: list[AblationVariantResult] = Field(min_length=1)
    comparability: ComparabilityStatus
    comparability_reasons: list[Identifier] = Field(default_factory=list)
    metric_deltas: list[AblationMetricDelta] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_comparison(self) -> "AblationComparisonResult":
        variant_ids = [item.variant_id for item in self.variant_results]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("comparison variant results must be unique")
        if self.reference_variant_id not in set(variant_ids):
            raise ValueError("comparison reference variant must exist")
        delta_ids = [item.variant_id for item in self.metric_deltas]
        if len(delta_ids) != len(set(delta_ids)) or set(delta_ids) != set(variant_ids):
            raise ValueError("metric_deltas must cover every variant exactly once")
        if self.comparability is ComparabilityStatus.COMPARABLE:
            if self.comparability_reasons:
                raise ValueError("comparable results cannot contain reasons")
        elif not self.comparability_reasons:
            raise ValueError("not-comparable results require reasons")
        return self


__all__ = (
    "AblationComparisonResult",
    "AblationMetricDelta",
    "AblationPlan",
    "AblationVariant",
    "AblationVariantResult",
    "CheckpointResult",
    "ComparabilityStatus",
    "CompressionControl",
    "CompressionEvent",
    "CompressionEventStatus",
    "CompressionMode",
    "ContextDiagnostic",
    "DiagnosticStatus",
    "DISABLED_COMPRESSION_THRESHOLD",
    "DistortionCandidate",
    "DistortionResult",
    "DistortionType",
    "DurationSource",
    "DurationDiagnostics",
    "EffectiveSubjectConfiguration",
    "FactContextObservation",
    "FactMatchMode",
    "FactProjection",
    "FactRetentionResult",
    "FactRetentionStatus",
    "LongConversationCheckpoint",
    "MemoryMode",
    "RequiredFact",
    "RequiredFactExpectation",
    "RequiredFactLossResult",
    "RequiredFactScope",
    "SessionContextMode",
    "TokenCountSource",
    "TokenDiagnostics",
    "TrialIdentity",
)
