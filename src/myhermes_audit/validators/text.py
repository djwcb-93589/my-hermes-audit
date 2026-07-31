"""Deterministic final-output text validation with bounded regex syntax."""

from __future__ import annotations

import re

from myhermes_audit.contracts import MetricResult, MetricSource
from myhermes_audit.contracts.suite import TextExpectation, TextTarget
from myhermes_audit.errors import UnsupportedCaseError, ValidatorError
from myhermes_audit.validators.base import (
    ValidationContext,
    evidence,
    metric,
    require_text_output,
)


_MAX_OUTPUT_CHARS = 200_000
_MAX_REGEX_CHARS = 256
_BACKREFERENCE = re.compile(r"\\[1-9]")


class TextValidator:
    def validate(
        self,
        expectation: TextExpectation,
        context: ValidationContext,
        *,
        metric_name: str,
    ) -> MetricResult:
        if expectation.target is not TextTarget.FINAL_OUTPUT:
            raise UnsupportedCaseError(
                "P1 TextValidator supports only final_output",
                target=expectation.target.value,
            )
        output = require_text_output(context)
        if len(output) > _MAX_OUTPUT_CHARS:
            raise ValidatorError("final output exceeds the text validation limit")
        actual = output if expectation.case_sensitive else output.casefold()
        failures: list[str] = []

        if expectation.exact is not None:
            expected = (
                expectation.exact
                if expectation.case_sensitive
                else expectation.exact.casefold()
            )
            if actual != expected:
                failures.append("exact text mismatch")
        if expectation.contains is not None:
            required = (
                expectation.contains
                if expectation.case_sensitive
                else expectation.contains.casefold()
            )
            if required not in actual:
                failures.append("required text is missing")
        if expectation.not_contains is not None:
            forbidden = (
                expectation.not_contains
                if expectation.case_sensitive
                else expectation.not_contains.casefold()
            )
            if forbidden in actual:
                failures.append("forbidden text is present")
        if expectation.matches_regex is not None:
            pattern = _compile_safe_pattern(
                expectation.matches_regex,
                case_sensitive=expectation.case_sensitive,
            )
            if pattern.search(output) is None:
                failures.append("regex did not match")

        passed = not failures
        return metric(
            name=metric_name,
            source=MetricSource.DETERMINISTIC,
            passed=passed,
            reason="text constraints satisfied" if passed else "; ".join(failures),
            evidence_items=[
                evidence(
                    kind="text",
                    description=f"target=final_output; characters={len(output)}",
                    metadata={"characters": len(output)},
                )
            ],
        )


def _compile_safe_pattern(pattern: str, *, case_sensitive: bool) -> re.Pattern[str]:
    if len(pattern) > _MAX_REGEX_CHARS:
        raise ValidatorError("regex exceeds the pattern length limit")
    if "(?" in pattern or _BACKREFERENCE.search(pattern):
        raise ValidatorError("regex uses unsupported advanced constructs")
    if any(character in pattern for character in "()*+{}"):
        raise ValidatorError(
            "regex uses grouping or unbounded repetition outside the P1 safe subset"
        )
    try:
        return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise ValidatorError("regex is invalid") from exc


__all__ = ("TextValidator",)
