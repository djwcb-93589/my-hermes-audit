"""确定性 JSON 序列化与 SHA-256 工具。"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from myhermes_audit.errors import ContractValidationError


def _json_compatible(value: Any) -> Any:
    """将受支持的合同值转换为无平台差异的 JSON 值。"""

    if isinstance(value, BaseModel):
        return _json_compatible(
            value.model_dump(mode="json", exclude_none=False)
        )
    if isinstance(value, Enum):
        return _json_compatible(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ContractValidationError(
                "datetime must include a timezone",
                field_path="datetime",
            )
        normalized = value.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ContractValidationError(
                "JSON object keys must be strings",
                field_path="object key",
            )
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractValidationError(
            "non-finite floats are not valid canonical JSON",
            field_path="number",
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractValidationError(
        f"unsupported canonical JSON value: {type(value).__name__}",
        field_path="value",
    )


def canonical_json(value: Any) -> str:
    """生成 UTF-8 语义、键排序且无多余空白的 canonical JSON。"""

    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    """生成 canonical JSON 的 UTF-8 字节。"""

    return canonical_json(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """计算 canonical JSON 的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def pretty_json(value: Any) -> str:
    """生成键顺序稳定、适合人类阅读的 JSON。"""

    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
