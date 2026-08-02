"""Provider-neutral contracts for Memory evaluation facts."""

from __future__ import annotations

import math
from enum import Enum

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

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


class RetrievalStrategy(str, Enum):
    SUBJECT_NATIVE = "subject_native"
    DISABLED = "disabled"
    DENSE = "dense"
    BM25 = "bm25"
    HYBRID = "hybrid"


class MemoryQueryPhase(str, Enum):
    BEFORE_CONVERSATION = "before_conversation"
    AFTER_CONVERSATION = "after_conversation"


class MemorySnapshotPhase(str, Enum):
    BEFORE_CONVERSATION = "before_conversation"
    AFTER_CONVERSATION = "after_conversation"


class MemoryStateChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


class MemoryErrorType(str, Enum):
    CAPABILITY = "memory_capability_error"
    STRATEGY_UNSUPPORTED = "memory_strategy_unsupported"
    KIND_UNSUPPORTED = "memory_kind_unsupported"
    SCOPE_UNSUPPORTED = "memory_scope_unsupported"
    SEED = "memory_seed_error"
    SNAPSHOT = "memory_snapshot_error"
    QUERY = "memory_query_error"
    CLEAR = "memory_clear_error"
    MAPPING = "memory_mapping_error"
    STATE_VALIDATION = "memory_state_validation_error"
    PROTOCOL = "memory_protocol_error"


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

    @field_validator("score")
    @classmethod
    def validate_finite_score(cls, value: int | float | None) -> int | float | None:
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("retrieval score must be finite")
        return value


class MemoryQueryResult(ContractModel):
    query_id: Identifier
    phase: MemoryQueryPhase
    query: MemoryQuery
    strategy: RetrievalStrategy
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
        if self.strategy is RetrievalStrategy.DISABLED and self.items:
            raise ValueError("disabled Memory queries must return no items")
        if self.provider == "prompt_context_injection":
            if self.metadata.get("query_used") is not False:
                raise ValueError("prompt context injection must record query_used=false")
            if self.metadata.get("score_semantics") != "none":
                raise ValueError("prompt context injection must record no score semantics")
            if any(item.score is not None for item in self.items):
                raise ValueError("prompt context injection cannot report retrieval scores")
        return self


class MemoryStateSnapshot(ContractModel):
    snapshot_id: Identifier
    phase: MemorySnapshotPhase | None = None
    strategy: RetrievalStrategy | None = None
    provider: NonEmptyText | None = None
    captured_at: UtcDatetime
    items: list[MemoryItem] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "MemoryStateSnapshot":
        ids = [item.memory_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("memory_id must be unique within a snapshot")
        return self


class MemoryStateChange(ContractModel):
    change_id: Identifier
    change_type: MemoryStateChangeType
    memory_id: Identifier
    kind: MemoryKind
    before: MemoryItem | None = None
    after: MemoryItem | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_change_shape(self) -> "MemoryStateChange":
        if self.change_type is MemoryStateChangeType.ADDED:
            if self.before is not None or self.after is None:
                raise ValueError("added Memory changes require only an after item")
        elif self.change_type is MemoryStateChangeType.REMOVED:
            if self.before is None or self.after is not None:
                raise ValueError("removed Memory changes require only a before item")
        else:
            if self.before is None or self.after is None:
                raise ValueError("modified/unchanged changes require before and after")
        for item in (self.before, self.after):
            if item is None:
                continue
            if item.memory_id != self.memory_id or item.kind is not self.kind:
                raise ValueError("Memory change identity must match its item projections")
        if (
            self.change_type is MemoryStateChangeType.UNCHANGED
            and self.before != self.after
        ):
            raise ValueError("unchanged Memory changes require equal items")
        if (
            self.change_type is MemoryStateChangeType.MODIFIED
            and self.before == self.after
        ):
            raise ValueError("modified Memory changes require different items")
        return self


class MemoryOperationError(ContractModel):
    error_type: MemoryErrorType
    operation: Identifier
    message: NonEmptyText
    query_id: Identifier | None = None
    phase: MemoryQueryPhase | None = None
    retryable: StrictBool = False
    details: JsonObject = Field(default_factory=dict)
