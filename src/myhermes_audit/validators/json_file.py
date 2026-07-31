"""Strict, expression-free JSON file validation."""

from __future__ import annotations

import json
import math
import re

from myhermes_audit.contracts import MetricResult, MetricSource
from myhermes_audit.contracts.suite import (
    JsonExpectation,
    JsonMatchMode,
    JsonRootType,
)
from myhermes_audit.errors import ValidatorError
from myhermes_audit.validators.base import (
    ValidationContext,
    evidence,
    metric,
    resolve_validation_path,
)


_MAX_JSON_BYTES = 2 * 1024 * 1024
_DOTTED_PATH = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*|\[[0-9]+\])*$"
)
_PATH_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)|\[([0-9]+)\]")
_MISSING = object()


class JsonFileValidator:
    def validate(
        self,
        expectation: JsonExpectation,
        context: ValidationContext,
        *,
        metric_name: str,
    ) -> MetricResult:
        target = resolve_validation_path(context, expectation.target)
        if not target.exists() or not target.is_file():
            return metric(
                name=metric_name,
                source=MetricSource.DETERMINISTIC,
                passed=False,
                reason="JSON file is missing",
                evidence_items=[
                    evidence(
                        kind="json_file",
                        description=f"path={expectation.target}; exists=False",
                        relative_path=expectation.target,
                    )
                ],
            )
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ValidatorError("cannot stat JSON file") from exc
        if size > _MAX_JSON_BYTES:
            raise ValidatorError("JSON file exceeds the read limit")
        try:
            document = json.loads(
                target.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        except UnicodeError as exc:
            raise ValidatorError("JSON file must be UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise ValidatorError("JSON file is not parseable") from exc
        except (RecursionError, OverflowError) as exc:
            raise ValidatorError("JSON file exceeds structural limits") from exc
        except ValueError as exc:
            raise ValidatorError("JSON file violates strict JSON constraints") from exc
        except OSError as exc:
            raise ValidatorError("cannot read JSON file") from exc
        _validate_json_tree(document)

        failures: list[str] = []
        if expectation.root_type is not None:
            actual_type = _json_type(document)
            if actual_type is not expectation.root_type:
                failures.append(
                    f"root type is {actual_type.value}, expected {expectation.root_type.value}"
                )
        if expectation.required_keys:
            if not isinstance(document, dict):
                failures.append("required_keys needs an object root")
            else:
                for key in expectation.required_keys:
                    if key not in document:
                        failures.append(f"required key is missing: {key}")
        for path in expectation.forbidden_keys:
            if _lookup(document, path) is not _MISSING:
                failures.append(f"forbidden key is present: {path}")
        for item in expectation.values:
            actual = _lookup(document, item.path)
            if actual is _MISSING:
                failures.append(f"JSON path is missing: {item.path}")
            elif not _strict_json_equal(actual, item.expected):
                failures.append(f"JSON value mismatch: {item.path}")
        if expectation.expected is not None:
            if expectation.match is JsonMatchMode.EXACT:
                matches = _strict_json_equal(document, expectation.expected)
            else:
                matches = _json_subset(document, expectation.expected)
            if not matches:
                failures.append(f"root {expectation.match.value} comparison failed")

        passed = not failures
        return metric(
            name=metric_name,
            source=MetricSource.DETERMINISTIC,
            passed=passed,
            reason="JSON constraints satisfied" if passed else "; ".join(failures),
            evidence_items=[
                evidence(
                    kind="json_file",
                    description=f"path={expectation.target}; size_bytes={size}",
                    relative_path=expectation.target,
                    metadata={
                        "root_type": _json_type(document).value,
                        "size_bytes": size,
                    },
                )
            ],
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _json_type(value: object) -> JsonRootType:
    if value is None:
        return JsonRootType.NULL
    if type(value) is bool:
        return JsonRootType.BOOLEAN
    if type(value) in (int, float):
        return JsonRootType.NUMBER
    if type(value) is str:
        return JsonRootType.STRING
    if type(value) is list:
        return JsonRootType.ARRAY
    if type(value) is dict:
        return JsonRootType.OBJECT
    raise ValidatorError("JSON contains an unsupported value type")


def _validate_json_tree(value: object, *, depth: int = 0) -> None:
    if depth > 100:
        raise ValidatorError("JSON nesting exceeds the validation limit")
    if type(value) is float and not math.isfinite(value):
        raise ValidatorError("JSON numbers must be finite")
    if type(value) is dict:
        for item in value.values():
            _validate_json_tree(item, depth=depth + 1)
    elif type(value) is list:
        for item in value:
            _validate_json_tree(item, depth=depth + 1)
    else:
        _json_type(value)


def _strict_json_equal(actual: object, expected: object) -> bool:
    if _json_type(actual) is not _json_type(expected):
        return False
    if type(actual) is dict and type(expected) is dict:
        return actual.keys() == expected.keys() and all(
            _strict_json_equal(actual[key], expected[key]) for key in actual
        )
    if type(actual) is list and type(expected) is list:
        return len(actual) == len(expected) and all(
            _strict_json_equal(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _json_subset(actual: object, expected: object) -> bool:
    if _json_type(actual) is not _json_type(expected):
        return False
    if type(expected) is dict and type(actual) is dict:
        return all(
            key in actual and _json_subset(actual[key], value)
            for key, value in expected.items()
        )
    return _strict_json_equal(actual, expected)


def _lookup(document: object, path: str) -> object:
    if not _DOTTED_PATH.fullmatch(path):
        raise ValidatorError("JSON path uses unsupported syntax")
    current = document
    position = 0
    while position < len(path):
        match = _PATH_TOKEN.match(path, position)
        if match is None:
            if path[position] == ".":
                position += 1
                continue
            raise ValidatorError("JSON path uses unsupported syntax")
        key, index_text = match.groups()
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                return _MISSING
            current = current[key]
        else:
            index = int(index_text)
            if not isinstance(current, list) or index >= len(current):
                return _MISSING
            current = current[index]
        position = match.end()
    return current


__all__ = ("JsonFileValidator",)
