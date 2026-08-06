"""Audit 公共异常层级。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AuditError(Exception):
    """所有可预期 Audit 错误的基类。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "audit_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """返回适合 CLI 或未来适配层使用的结构化错误。"""

        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class ContractValidationError(AuditError):
    """领域合同未满足。"""

    def __init__(self, message: str, *, field_path: str = "<root>") -> None:
        super().__init__(
            message,
            code="contract_validation_error",
            details={"field_path": field_path},
        )
        self.field_path = field_path


class DatasetLoadError(AuditError):
    """Suite YAML 无法读取、解析或静态校验。"""

    def __init__(
        self,
        yaml_path: Path,
        *,
        case_id: str | None,
        field_path: str,
        reason: str,
    ) -> None:
        resolved_path = Path(yaml_path).resolve(strict=False)
        display_case = case_id or "<suite>"
        message = (
            f"{resolved_path}: case={display_case}: "
            f"field={field_path}: {reason}"
        )
        super().__init__(
            message,
            code="dataset_load_error",
            details={
                "yaml_file": str(resolved_path),
                "case_id": display_case,
                "field_path": field_path,
                "reason": reason,
            },
        )
        self.yaml_path = resolved_path
        self.case_id = display_case
        self.field_path = field_path
        self.reason = reason


class UnsafePathError(AuditError):
    """路径不是受控根目录中的安全相对路径。"""

    def __init__(self, path: str | Path, *, reason: str) -> None:
        super().__init__(
            f"unsafe path {str(path)!r}: {reason}",
            code="unsafe_path",
            details={"path": str(path), "reason": reason},
        )
        self.path = str(path)
        self.reason = reason


class SandboxError(AuditError):
    """Sandbox 创建、使用或清理失败。"""

    def __init__(self, message: str, *, operation: str) -> None:
        super().__init__(
            message,
            code="sandbox_error",
            details={"operation": operation},
        )
        self.operation = operation


class FingerprintError(AuditError):
    """无法以只读方式生成指纹。"""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        repository: Path | None = None,
        returncode: int | None = None,
    ) -> None:
        details: dict[str, Any] = {"operation": operation}
        if repository is not None:
            details["repository"] = str(repository.resolve(strict=False))
        if returncode is not None:
            details["returncode"] = returncode
        super().__init__(message, code="fingerprint_error", details=details)
        self.operation = operation
        self.repository = repository
        self.returncode = returncode


class RunnerError(AuditError):
    def __init__(self, message: str, *, code: str = "runner_error", **details: Any) -> None:
        super().__init__(message, code=code, details=details)


class SubjectPreflightError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="subject_preflight_error", **details)


class SubjectCapabilityError(SubjectPreflightError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "subject_capability_error"


class UnsupportedCaseError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="unsupported_case", **details)


class WorkerProtocolError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="worker_protocol_error", **details)


class WorkerProcessError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="worker_process_error", **details)


class WorkerTimeoutError(WorkerProcessError):
    def __init__(self, message: str = "MyHermes worker timed out", **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "worker_timeout"


class MemoryEvaluationError(RunnerError):
    """A safe, stable Memory pipeline failure without Memory content."""

    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message, code=code, **details)


class MemoryCapabilityError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_capability_error", **details)


class MemoryStrategyUnsupportedError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_strategy_unsupported", **details)


class MemoryKindUnsupportedError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_kind_unsupported", **details)


class MemoryScopeUnsupportedError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_scope_unsupported", **details)


class MemorySeedError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_seed_error", **details)


class MemorySnapshotError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_snapshot_error", **details)


class MemoryQueryError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_query_error", **details)


class MemoryClearError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_clear_error", **details)


class MemoryMappingError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_mapping_error", **details)


class MemoryStateValidationError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_state_validation_error", **details)


class MemoryProtocolError(MemoryEvaluationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="memory_protocol_error", **details)


class BackgroundReviewError(RunnerError):
    """Safe Review failure without evidence, prompts, or claim tokens."""

    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message, code=code, **details)


class BackgroundReviewCapabilityError(BackgroundReviewError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="background_review_capability_error", **details)


class BackgroundReviewProtocolError(BackgroundReviewError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="background_review_protocol_error", **details)


class AblationError(RunnerError):
    """Safe ablation failure without conversation or fact content."""

    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message, code=code, **details)


class AblationContractError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="ablation_contract_error", **details)


class AblationVariantError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="ablation_variant_error", **details)


class AblationCapabilityError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="ablation_capability_error", **details)


class ShortTermContextError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="short_term_context_error", **details)


class CompressionCapabilityError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="compression_capability_error", **details)


class CompressionConfigurationError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="compression_configuration_error", **details)


class CompressionObservationError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="compression_observation_error", **details)


class CompressionLimitError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="compression_limit_error", **details)


class RequiredFactValidationError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="required_fact_validation_error", **details)


class FactRetentionError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="fact_retention_error", **details)


class DistortionValidationError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="distortion_validation_error", **details)


class AblationComparisonError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="ablation_comparison_error", **details)


class TokenDiagnosticsError(AblationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="token_diagnostics_error", **details)


class FixtureMaterializationError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="fixture_materialization_error", **details)


class ConfigBuildError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="config_build_error", **details)


class ValidatorError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="validator_error", **details)


class ReportError(RunnerError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="report_error", **details)


class IntegrationError(AuditError):
    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message, code=code, details=details)


class LangfuseDependencyError(IntegrationError):
    def __init__(
        self,
        message: str = "Langfuse dependency is unavailable",
        **details: Any,
    ) -> None:
        super().__init__(message, code="langfuse_dependency_error", **details)


class UnsupportedLangfuseVersionError(LangfuseDependencyError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "unsupported_langfuse_version"


class LangfuseCapabilityError(LangfuseDependencyError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "langfuse_capability_error"


class LangfuseConfigError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="langfuse_config_error", **details)


class LangfuseConnectionError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="langfuse_connection_error", **details)


class DatasetSyncError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="dataset_sync_error", **details)


class ExperimentPublishError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="experiment_publish_error", **details)


class ExperimentInitializationError(ExperimentPublishError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "experiment_initialization_error"


class ExperimentAssociationError(ExperimentPublishError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "experiment_association_error"


class ExperimentReplayError(ExperimentPublishError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "experiment_replay_error"


class ScorePublishError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="score_publish_error", **details)


class ScoreIdentityError(ScorePublishError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "score_identity_error"


class ScorePublicationConflictError(ScoreIdentityError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "score_publication_conflict"


class ScoreIdempotencyError(ScorePublishError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "score_idempotency_error"


class _ClassifiedScoreError(ScorePublishError):
    error_code = "score_publish_error"
    default_retryable = False

    def __init__(self, message: str, **details: Any) -> None:
        details.setdefault("retryable", self.default_retryable)
        super().__init__(message, **details)
        self.code = self.error_code


class ScoreTargetError(_ClassifiedScoreError):
    error_code = "score_target_error"


class ScoreValidationError(_ClassifiedScoreError):
    error_code = "score_validation_error"


class ScoreAuthenticationError(_ClassifiedScoreError):
    error_code = "score_authentication_error"


class ScorePermissionError(_ClassifiedScoreError):
    error_code = "score_permission_error"


class ScoreRateLimitError(_ClassifiedScoreError):
    error_code = "score_rate_limit_error"
    default_retryable = True


class ScoreTransportError(_ClassifiedScoreError):
    error_code = "score_transport_error"
    default_retryable = True


class ScoreTimeoutError(_ClassifiedScoreError):
    error_code = "score_timeout_error"
    default_retryable = True


class ScoreConfirmationTimeoutError(_ClassifiedScoreError):
    error_code = "score_confirmation_timeout"
    default_retryable = True


class ScoreIdentityConflictError(_ClassifiedScoreError):
    error_code = "score_identity_conflict"


class ScoreCapabilityError(_ClassifiedScoreError):
    error_code = "score_capability_error"


class PublicationManifestError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="publication_manifest_error", **details)


class PublicationStateError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="publication_state_error", **details)


class DeprecatedLangfuseApiError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="deprecated_langfuse_api_error", **details)


class JudgeDependencyError(IntegrationError):
    def __init__(self, message: str = "Judge dependency is unavailable") -> None:
        super().__init__(message, code="judge_dependency_error")


class JudgeConfigError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="judge_config_error", **details)


class JudgeInvocationError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="judge_invocation_error", **details)


class JudgeTimeoutError(JudgeInvocationError):
    def __init__(self, message: str = "Judge request timed out", **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "judge_timeout_error"


class JudgeParseError(JudgeInvocationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "judge_parse_error"


class JudgeProtocolError(JudgeInvocationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = "judge_protocol_error"


class ContentRedactionError(IntegrationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, code="content_redaction_error", **details)
