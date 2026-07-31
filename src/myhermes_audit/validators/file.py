"""Bounded, symlink-safe file expectation validation."""

from __future__ import annotations

from myhermes_audit.artifacts import sha256_file
from myhermes_audit.contracts import MetricResult, MetricSource
from myhermes_audit.contracts.suite import FileExpectation
from myhermes_audit.errors import ValidatorError
from myhermes_audit.validators.base import (
    ValidationContext,
    evidence,
    metric,
    resolve_validation_path,
)


_MAX_VALIDATED_FILE_BYTES = 10 * 1024 * 1024
_MAX_TEXT_FILE_BYTES = 1024 * 1024


class FileValidator:
    def validate(
        self,
        expectation: FileExpectation,
        context: ValidationContext,
        *,
        metric_name: str,
    ) -> MetricResult:
        target = resolve_validation_path(context, expectation.path)
        exists = target.exists()
        if expectation.exists is False:
            passed = not exists
            return metric(
                name=metric_name,
                source=MetricSource.DETERMINISTIC,
                passed=passed,
                reason=("file is absent" if passed else "file unexpectedly exists"),
                evidence_items=[
                    evidence(
                        kind="file",
                        description=f"path={expectation.path}; exists={exists}",
                        relative_path=expectation.path,
                    )
                ],
            )
        if not exists or not target.is_file():
            return metric(
                name=metric_name,
                source=MetricSource.DETERMINISTIC,
                passed=False,
                reason="expected regular file is missing",
                evidence_items=[
                    evidence(
                        kind="file",
                        description=f"path={expectation.path}; exists={exists}",
                        relative_path=expectation.path,
                    )
                ],
            )
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ValidatorError("cannot stat expected file") from exc
        if size > _MAX_VALIDATED_FILE_BYTES:
            raise ValidatorError("expected file exceeds the validation size limit")

        failures: list[str] = []
        if expectation.minimum_size_bytes is not None and size < expectation.minimum_size_bytes:
            failures.append("file is smaller than minimum_size_bytes")
        if expectation.maximum_size_bytes is not None and size > expectation.maximum_size_bytes:
            failures.append("file is larger than maximum_size_bytes")
        actual_sha = None
        if expectation.sha256 is not None:
            actual_sha = sha256_file(target)
            if actual_sha != expectation.sha256:
                failures.append("sha256 mismatch")

        text_checks = (
            expectation.exact_text is not None
            or bool(expectation.content_contains)
            or bool(expectation.content_not_contains)
        )
        if text_checks:
            if size > _MAX_TEXT_FILE_BYTES:
                raise ValidatorError("text validation file exceeds the text read limit")
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise ValidatorError("text expectation requires a UTF-8 file") from exc
            except OSError as exc:
                raise ValidatorError("cannot read expected file") from exc
            if expectation.exact_text is not None and content != expectation.exact_text:
                failures.append("exact_text mismatch")
            for required in expectation.content_contains:
                if required not in content:
                    failures.append("required text is missing")
            for forbidden in expectation.content_not_contains:
                if forbidden in content:
                    failures.append("forbidden text is present")

        passed = not failures
        metadata = {"size_bytes": size}
        if actual_sha is not None:
            metadata["sha256"] = actual_sha
        return metric(
            name=metric_name,
            source=MetricSource.DETERMINISTIC,
            passed=passed,
            reason="file constraints satisfied" if passed else "; ".join(failures),
            evidence_items=[
                evidence(
                    kind="file",
                    description=f"path={expectation.path}; size_bytes={size}",
                    relative_path=expectation.path,
                    metadata=metadata,
                )
            ],
        )


__all__ = ("FileValidator",)
