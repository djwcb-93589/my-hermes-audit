"""安全 YAML Suite 加载与静态合同校验。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from myhermes_audit.contracts import AuditSuite
from myhermes_audit.datasets.resolver import resolve_suite_sources
from myhermes_audit.errors import DatasetLoadError


_MAX_SUITE_BYTES = 5 * 1024 * 1024


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate keys in every mapping."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            "expected a mapping node",
            node.start_mark,
        )

    # Flatten merge keys first so aliases remain supported while collisions in
    # the resulting mapping are still rejected.
    loader.flatten_mapping(node)
    seen: dict[Any, Any] = {}
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        try:
            first_mark = seen.get(key)
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if first_mark is not None:
            first_location = (
                f"line {first_mark.line + 1}, column {first_mark.column + 1}"
            )
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}; first declared at {first_location}",
                key_node.start_mark,
            )
        seen[key] = key_node.start_mark
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _field_path(location: tuple[Any, ...]) -> str:
    if not location:
        return "<root>"
    result = ""
    for part in location:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += ("." if result else "") + str(part)
    return result


def _case_id_for_location(
    raw_data: Mapping[str, Any],
    location: tuple[Any, ...],
) -> str | None:
    if len(location) < 2 or location[0] != "cases":
        return None
    index = location[1]
    cases = raw_data.get("cases")
    if not isinstance(index, int) or not isinstance(cases, list):
        return "<unknown>"
    if index < 0 or index >= len(cases) or not isinstance(cases[index], Mapping):
        return "<unknown>"
    case_id = cases[index].get("case_id")
    return case_id if isinstance(case_id, str) and case_id else "<unknown>"


def _reject_duplicate_ids(raw_data: Mapping[str, Any], yaml_path: Path) -> None:
    cases = raw_data.get("cases")
    if not isinstance(cases, list):
        return
    seen_cases: dict[str, int] = {}
    for case_index, raw_case in enumerate(cases):
        if not isinstance(raw_case, Mapping):
            continue
        case_id = raw_case.get("case_id")
        if isinstance(case_id, str) and case_id:
            if case_id in seen_cases:
                raise DatasetLoadError(
                    yaml_path,
                    case_id=case_id,
                    field_path=f"cases[{case_index}].case_id",
                    reason=(
                        "duplicate case_id; first declared at "
                        f"cases[{seen_cases[case_id]}].case_id"
                    ),
                )
            seen_cases[case_id] = case_index

        evaluators = raw_case.get("evaluators")
        if not isinstance(evaluators, list):
            continue
        seen_evaluators: dict[str, int] = {}
        for evaluator_index, evaluator in enumerate(evaluators):
            if not isinstance(evaluator, Mapping):
                continue
            evaluator_id = evaluator.get("evaluator_id")
            if not isinstance(evaluator_id, str) or not evaluator_id:
                continue
            if evaluator_id in seen_evaluators:
                raise DatasetLoadError(
                    yaml_path,
                    case_id=(
                        case_id
                        if isinstance(case_id, str) and case_id
                        else "<unknown>"
                    ),
                    field_path=(
                        f"cases[{case_index}].evaluators[{evaluator_index}]"
                        ".evaluator_id"
                    ),
                    reason=(
                        "duplicate evaluator_id; first declared at "
                        f"evaluators[{seen_evaluators[evaluator_id]}].evaluator_id"
                    ),
                )
            seen_evaluators[evaluator_id] = evaluator_index


def _read_yaml_file(yaml_path: Path) -> str:
    try:
        stat = yaml_path.stat()
    except OSError as exc:
        raise DatasetLoadError(
            yaml_path,
            case_id=None,
            field_path="<file>",
            reason=f"cannot stat YAML file: {exc}",
        ) from exc
    if not yaml_path.is_file():
        raise DatasetLoadError(
            yaml_path,
            case_id=None,
            field_path="<file>",
            reason="YAML path is not a regular file",
        )
    if stat.st_size > _MAX_SUITE_BYTES:
        raise DatasetLoadError(
            yaml_path,
            case_id=None,
            field_path="<file>",
            reason=f"YAML file exceeds {_MAX_SUITE_BYTES} bytes",
        )
    try:
        return yaml_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DatasetLoadError(
            yaml_path,
            case_id=None,
            field_path="<file>",
            reason=f"cannot read UTF-8 YAML: {exc}",
        ) from exc


def load_suite(path: Path) -> AuditSuite:
    """使用 ``yaml.safe_load`` 加载并严格校验一个 AuditSuite。"""

    yaml_path = Path(path).expanduser().resolve(strict=False)
    text = _read_yaml_file(yaml_path)
    try:
        raw_data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f"line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else "unknown location"
        )
        problem = getattr(exc, "problem", None) or str(exc)
        raise DatasetLoadError(
            yaml_path,
            case_id=None,
            field_path="<yaml>",
            reason=f"YAML parse error at {location}: {problem}",
        ) from exc
    if not isinstance(raw_data, Mapping):
        raise DatasetLoadError(
            yaml_path,
            case_id=None,
            field_path="<root>",
            reason="YAML root must be a mapping",
        )

    _reject_duplicate_ids(raw_data, yaml_path)
    try:
        suite = AuditSuite.model_validate(raw_data)
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_context=False)[0]
        location = tuple(error.get("loc", ()))
        raise DatasetLoadError(
            yaml_path,
            case_id=_case_id_for_location(raw_data, location),
            field_path=_field_path(location),
            reason=str(error.get("msg", "contract validation failed")),
        ) from exc
    return resolve_suite_sources(suite, yaml_path)
