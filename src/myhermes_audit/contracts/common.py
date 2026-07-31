"""所有 Audit 合同共享的严格类型与校验器。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)


CURRENT_SCHEMA_VERSION = "1.0"
SchemaVersion = Literal["1.0"]
Identifier = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
NonEmptyText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Sha256Digest = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
GitObjectId = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
JsonObject = dict[NonEmptyText, JsonValue]
Number = StrictInt | StrictFloat

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def ensure_utc_datetime(value: datetime) -> datetime:
    """拒绝无时区时间，并将有效值规范化为 UTC。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc_datetime)]


def validate_relative_path(
    value: str,
    *,
    allowed_roots: frozenset[str] | None = None,
) -> str:
    """校验跨平台安全相对路径，不访问文件系统。"""

    if not isinstance(value, str):
        raise ValueError("path must be a string")
    if not value or value != value.strip():
        raise ValueError("path must be non-empty and have no surrounding whitespace")
    if "\x00" in value:
        raise ValueError("path must not contain NUL")
    if "\\" in value:
        raise ValueError("path must use forward slashes")
    if value.startswith(("/", "//")):
        raise ValueError("absolute paths are not allowed")
    if _WINDOWS_DRIVE_RE.match(value):
        raise ValueError("Windows drive paths are not allowed")
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute() or windows_path.drive:
        raise ValueError("absolute or drive-relative paths are not allowed")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("empty, '.' and '..' path segments are not allowed")
    if any(":" in part for part in raw_parts):
        raise ValueError("':' is not allowed in path segments")
    if any(any(ord(character) < 32 for character in part) for part in raw_parts):
        raise ValueError("control characters are not allowed in paths")
    normalized = PurePosixPath(*raw_parts).as_posix()
    if allowed_roots is not None and raw_parts[0] not in allowed_roots:
        roots = ", ".join(sorted(allowed_roots))
        raise ValueError(f"path must start with one of: {roots}")
    return normalized


def _validate_safe_relative(value: str) -> str:
    return validate_relative_path(value)


def _validate_fixture_target(value: str) -> str:
    normalized = validate_relative_path(
        value,
        allowed_roots=frozenset({"workspace", "hermes_home"}),
    )
    if "/" not in normalized:
        raise ValueError("fixture target must name an entry below its allowed root")
    return normalized


SafeRelativePath = Annotated[StrictStr, AfterValidator(_validate_safe_relative)]
FixtureTargetPath = Annotated[StrictStr, AfterValidator(_validate_fixture_target)]


class ContractModel(BaseModel):
    """严格、可版本化且可稳定序列化的合同基类。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )

    schema_version: SchemaVersion = Field(
        default=CURRENT_SCHEMA_VERSION,
        description="Audit contract schema version.",
    )

    @model_validator(mode="after")
    def reject_non_finite_numbers(self) -> "ContractModel":
        """确保任意窄作用域 JSON 扩展也能稳定序列化。"""

        def visit(value: Any) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite numbers are not allowed")
            if isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)

        for field_name in type(self).model_fields:
            visit(getattr(self, field_name))
        return self

    def stable_dump(self) -> dict[str, Any]:
        """返回包含显式空值的 JSON 兼容合同数据。"""

        return self.model_dump(mode="json", exclude_none=False)

    def stable_json(self) -> str:
        """返回 canonical JSON。"""

        from myhermes_audit.serialization import canonical_json

        return canonical_json(self)
