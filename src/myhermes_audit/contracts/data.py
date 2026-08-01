"""Data-classification contracts shared by remote publication policies."""

from __future__ import annotations

from enum import Enum
from typing import Mapping


class DataClassification(str, Enum):
    SYNTHETIC = "synthetic"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


_CLASSIFICATION_RANK = {
    DataClassification.SYNTHETIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.SENSITIVE: 2,
}


def classification_from_metadata(
    metadata: Mapping[str, object],
    *,
    default: DataClassification = DataClassification.INTERNAL,
) -> DataClassification:
    value = metadata.get("data_classification", default.value)
    if not isinstance(value, str):
        raise ValueError("metadata.data_classification must be a string")
    try:
        return DataClassification(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in DataClassification)
        raise ValueError(
            f"metadata.data_classification must be one of: {allowed}"
        ) from exc


def is_classification_downgrade(
    parent: DataClassification,
    child: DataClassification,
) -> bool:
    return _CLASSIFICATION_RANK[child] < _CLASSIFICATION_RANK[parent]


__all__ = (
    "DataClassification",
    "classification_from_metadata",
    "is_classification_downgrade",
)
