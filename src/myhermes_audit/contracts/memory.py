"""与具体存储及检索算法无关的 Memory 评测合同。"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, StrictFloat, StrictInt, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    UtcDatetime,
)


class MemoryKind(str, Enum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    USER_PROFILE = "user_profile"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    UNKNOWN = "unknown"


class MemoryItem(ContractModel):
    memory_id: Identifier
    kind: MemoryKind
    content: NonEmptyText
    source: NonEmptyText
    user_id: Identifier | None = None
    session_id: Identifier | None = None
    metadata: JsonObject = Field(default_factory=dict)


class MemoryFixture(ContractModel):
    items: list[MemoryItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "MemoryFixture":
        ids = [item.memory_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("memory_id must be unique within a MemoryFixture")
        return self


class MemoryQuery(ContractModel):
    query: NonEmptyText
    top_k: PositiveInt = 10
    user_id: Identifier | None = None
    session_id: Identifier | None = None
    filters: JsonObject = Field(default_factory=dict)


class RetrievedMemory(ContractModel):
    memory_id: Identifier
    kind: MemoryKind
    content: NonEmptyText
    rank: PositiveInt
    score: StrictInt | StrictFloat | None = None
    source: NonEmptyText
    metadata: JsonObject = Field(default_factory=dict)


class MemoryQueryResult(ContractModel):
    query: MemoryQuery
    provider: NonEmptyText
    items: list[RetrievedMemory] = Field(default_factory=list)
    duration_ms: NonNegativeInt
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ranks(self) -> "MemoryQueryResult":
        ranks = [item.rank for item in self.items]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(
                "retrieved ranks must be unique, sorted, and contiguous from 1"
            )
        memory_ids = [item.memory_id for item in self.items]
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("memory_id must be unique within a MemoryQueryResult")
        if len(self.items) > self.query.top_k:
            raise ValueError("retrieved item count cannot exceed query.top_k")
        return self


class MemoryStateSnapshot(ContractModel):
    snapshot_id: Identifier
    captured_at: UtcDatetime
    items: list[MemoryItem] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "MemoryStateSnapshot":
        ids = [item.memory_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("memory_id must be unique within a snapshot")
        return self
