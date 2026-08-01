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
