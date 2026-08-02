"""Audit Suite、Case、Fixture、预期与 Evaluator 声明合同。"""

from __future__ import annotations

import math
from enum import Enum
from pathlib import Path

from pydantic import (
    Field,
    JsonValue,
    PrivateAttr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from myhermes_audit.contracts.background_review import (
    BackgroundReviewExpectation,
    ReviewRequest,
    SkillManagedBy,
    SkillSource,
)
from myhermes_audit.contracts.common import (
    ContractModel,
    FixtureTargetPath,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    SafeRelativePath,
    SchemaVersion,
    Sha256Digest,
)
from myhermes_audit.contracts.data import (
    DataClassification,
    classification_from_metadata,
    is_classification_downgrade,
)
from myhermes_audit.contracts.judge import JudgeExpectation
from myhermes_audit.contracts.memory import (
    MemoryFixture,
    MemoryKind,
    MemoryQuery,
    MemoryQueryPhase,
    RetrievalStrategy,
)


class CaseMode(str, Enum):
    SINGLE_TURN = "single_turn"
    SCRIPTED_MULTI_TURN = "scripted_multi_turn"
    SIMULATED_USER = "simulated_user"


class ConversationRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationTurn(ContractModel):
    role: ConversationRole
    message: NonEmptyText
    session_id: Identifier | None = None


class SimulatedUserGoal(ContractModel):
    goal: NonEmptyText
    persona: NonEmptyText | None = None
    constraints: list[NonEmptyText] = Field(default_factory=list)
    success_criteria: list[NonEmptyText] = Field(default_factory=list)
    max_turns: PositiveInt = 8


class CaseInput(ContractModel):
    message: NonEmptyText | None = None
    session_id: Identifier | None = None
    turns: list[ConversationTurn] = Field(default_factory=list)
    simulated_user: SimulatedUserGoal | None = None

    @model_validator(mode="after")
    def validate_one_input_shape(self) -> "CaseInput":
        populated = sum(
            (
                self.message is not None,
                bool(self.turns),
                self.simulated_user is not None,
            )
        )
        if populated != 1:
            raise ValueError(
                "exactly one of message, turns, or simulated_user must be provided"
            )
        if self.session_id is not None and self.message is None:
            raise ValueError("input.session_id is only valid with input.message")
        return self


class TrialConfig(ContractModel):
    trials: PositiveInt = 1
    timeout_seconds: PositiveInt = 120
    seed: StrictInt | None = None
    preserve_sandbox: StrictBool = False
    metadata: JsonObject = Field(
        default_factory=lambda: {
            "data_classification": DataClassification.INTERNAL.value,
        }
    )

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: JsonObject) -> JsonObject:
        normalized = dict(value)
        classification = classification_from_metadata(normalized)
        normalized["data_classification"] = classification.value
        return normalized


class RunnerKind(str, Enum):
    CONVERSATION = "conversation"


class ToolsetName(str, Enum):
    FILE = "file"
    TERMINAL = "terminal"
    MEMORY = "memory"


class ExecutionSpec(ContractModel):
    runner: RunnerKind = RunnerKind.CONVERSATION
    workdir: SafeRelativePath = "workspace"
    enabled_toolsets: list[ToolsetName] = Field(default_factory=list)
    memory_strategy: RetrievalStrategy | None = None
    config_overrides: JsonObject = Field(default_factory=dict)
    environment_overrides: dict[NonEmptyText, StrictStr] = Field(default_factory=dict)

    @field_validator("enabled_toolsets")
    @classmethod
    def validate_enabled_toolsets(
        cls,
        value: list[ToolsetName],
    ) -> list[ToolsetName]:
        if len(value) != len(set(value)):
            raise ValueError("enabled_toolsets must not repeat")
        return value

    @field_validator("environment_overrides")
    @classmethod
    def validate_environment_overrides(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        from myhermes_audit.environment import validate_suite_environment_overrides

        return validate_suite_environment_overrides(value)


class FixtureFile(ContractModel):
    path: FixtureTargetPath
    source: SafeRelativePath | None = None
    content: StrictStr | None = None

    _resolved_source: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_source_or_content(self) -> "FixtureFile":
        if (self.source is None) == (self.content is None):
            raise ValueError("exactly one of source or content must be provided")
        return self

    @property
    def resolved_source(self) -> Path | None:
        """返回 loader 校验后的源路径；内联 content 时为 None。"""

        return self._resolved_source

    def set_resolved_source(self, source: Path) -> None:
        """仅供 Dataset resolver 保存不参与序列化的已解析路径。"""

        self._resolved_source = Path(source)


class SkillFixture(ContractModel):
    skill_id: Identifier
    name: NonEmptyText
    content: NonEmptyText
    source: SkillSource = SkillSource.LOCAL
    managed_by: SkillManagedBy = SkillManagedBy.USER
    pinned: StrictBool = False
    metadata: JsonObject = Field(default_factory=dict)


class DatabaseFixtureReference(ContractModel):
    reference: Identifier
    source: SafeRelativePath | None = None
    sha256: Sha256Digest | None = None

    _resolved_source: Path | None = PrivateAttr(default=None)

    @property
    def resolved_source(self) -> Path | None:
        """返回 loader 校验后的数据库 Fixture 源路径。"""

        return self._resolved_source

    def set_resolved_source(self, source: Path) -> None:
        """保存不参与合同序列化的已解析路径。"""

        self._resolved_source = Path(source)


class FixtureSpec(ContractModel):
    files: list[FixtureFile] = Field(default_factory=list)
    memory: MemoryFixture | None = None
    skills: list[SkillFixture] = Field(default_factory=list)
    database: DatabaseFixtureReference | None = None
    review_requests: list[ReviewRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_fixture_ids(self) -> "FixtureSpec":
        skill_ids = [skill.skill_id for skill in self.skills]
        paths = [fixture.path for fixture in self.files]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skill_id must be unique within a FixtureSpec")
        if len(paths) != len(set(paths)):
            raise ValueError("fixture file path must be unique within a FixtureSpec")
        review_ids = [request.review_id for request in self.review_requests]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("review_id must be unique within a FixtureSpec")
        return self


class FileExpectation(ContractModel):
    path: FixtureTargetPath
    exists: StrictBool = True
    sha256: Sha256Digest | None = None
    exact_text: StrictStr | None = None
    content_contains: list[NonEmptyText] = Field(default_factory=list)
    content_not_contains: list[NonEmptyText] = Field(default_factory=list)
    minimum_size_bytes: NonNegativeInt | None = None
    maximum_size_bytes: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_file_expectation(self) -> "FileExpectation":
        if (
            self.minimum_size_bytes is not None
            and self.maximum_size_bytes is not None
            and self.minimum_size_bytes > self.maximum_size_bytes
        ):
            raise ValueError("minimum_size_bytes cannot exceed maximum_size_bytes")
        if not self.exists and any(
            (
                self.sha256 is not None,
                self.exact_text is not None,
                bool(self.content_contains),
                bool(self.content_not_contains),
                self.minimum_size_bytes is not None,
                self.maximum_size_bytes is not None,
            )
        ):
            raise ValueError("a non-existent file cannot have content constraints")
        return self


class TextTarget(str, Enum):
    FINAL_OUTPUT = "final_output"
    ARTIFACT = "artifact"
    FILE = "file"
    TOOL_OUTPUT = "tool_output"


class TextExpectation(ContractModel):
    target: TextTarget
    reference: NonEmptyText | None = None
    exact: StrictStr | None = None
    contains: NonEmptyText | None = None
    not_contains: NonEmptyText | None = None
    matches_regex: NonEmptyText | None = None
    case_sensitive: StrictBool = True

    @model_validator(mode="after")
    def validate_matcher(self) -> "TextExpectation":
        if all(
            value is None
            for value in (
                self.exact,
                self.contains,
                self.not_contains,
                self.matches_regex,
            )
        ):
            raise ValueError(
                "exact, contains, not_contains, or matches_regex must be provided"
            )
        if self.target is TextTarget.FINAL_OUTPUT and self.reference is not None:
            raise ValueError("final_output text expectations do not use reference")
        return self


class JsonMatchMode(str, Enum):
    EXACT = "exact"
    SUBSET = "subset"


class JsonRootType(str, Enum):
    OBJECT = "object"
    ARRAY = "array"
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"


class JsonValueExpectation(ContractModel):
    path: NonEmptyText
    expected: JsonValue


class JsonExpectation(ContractModel):
    target: FixtureTargetPath
    expected: JsonValue | None = None
    match: JsonMatchMode = JsonMatchMode.EXACT
    root_type: JsonRootType | None = None
    required_keys: list[NonEmptyText] = Field(default_factory=list)
    values: list[JsonValueExpectation] = Field(default_factory=list)
    forbidden_keys: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_json_expectation(self) -> "JsonExpectation":
        if len(self.required_keys) != len(set(self.required_keys)):
            raise ValueError("required_keys must not repeat")
        if len(self.forbidden_keys) != len(set(self.forbidden_keys)):
            raise ValueError("forbidden_keys must not repeat")
        paths = [item.path for item in self.values]
        if len(paths) != len(set(paths)):
            raise ValueError("JSON value paths must not repeat")
        return self


class ToolCallExpectation(ContractModel):
    tool_name: NonEmptyText
    arguments: JsonObject = Field(default_factory=dict)


class ToolTrajectoryExpectation(ContractModel):
    calls: list[ToolCallExpectation] = Field(default_factory=list)
    ordered: StrictBool = True
    allow_additional_calls: StrictBool = False
    required_tools: list[NonEmptyText] = Field(default_factory=list)
    forbidden_tools: list[NonEmptyText] = Field(default_factory=list)
    minimum_tool_calls: NonNegativeInt | None = None
    maximum_tool_calls: NonNegativeInt | None = None
    required_successful_tools: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_trajectory(self) -> "ToolTrajectoryExpectation":
        for field_name in (
            "required_tools",
            "forbidden_tools",
            "required_successful_tools",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not repeat")
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("a tool cannot be both required and forbidden")
        if set(self.required_successful_tools) & set(self.forbidden_tools):
            raise ValueError("a successful tool cannot also be forbidden")
        if (
            self.minimum_tool_calls is not None
            and self.maximum_tool_calls is not None
            and self.minimum_tool_calls > self.maximum_tool_calls
        ):
            raise ValueError("minimum_tool_calls cannot exceed maximum_tool_calls")
        return self


class MemoryExpectation(ContractModel):
    query_id: Identifier
    phase: MemoryQueryPhase = MemoryQueryPhase.BEFORE_CONVERSATION
    query: MemoryQuery
    required_memory_ids: list[Identifier] = Field(default_factory=list)
    forbidden_memory_ids: list[Identifier] = Field(default_factory=list)
    runtime_generated_memory_ids: list[Identifier] = Field(default_factory=list)
    required_kinds: list[MemoryKind] = Field(default_factory=list)
    minimum_matches: NonNegativeInt = 0
    minimum_recall_at_k: StrictFloat | None = Field(default=None, ge=0, le=1)
    minimum_mrr: StrictFloat | None = Field(default=None, ge=0, le=1)

    @field_validator(
        "required_memory_ids",
        "forbidden_memory_ids",
        "runtime_generated_memory_ids",
        "required_kinds",
    )
    @classmethod
    def validate_unique_memory_values(cls, value: list) -> list:
        if len(value) != len(set(value)):
            raise ValueError("Memory expectation lists must not repeat values")
        return value

    @model_validator(mode="after")
    def validate_memory_expectation(self) -> "MemoryExpectation":
        required = set(self.required_memory_ids)
        forbidden = set(self.forbidden_memory_ids)
        if required & forbidden:
            raise ValueError("required and forbidden memory IDs must be disjoint")
        if self.minimum_matches > len(self.required_memory_ids):
            raise ValueError("minimum_matches cannot exceed required_memory_ids")
        if not self.required_memory_ids and (
            self.minimum_recall_at_k is not None or self.minimum_mrr is not None
        ):
            raise ValueError("Recall/MRR thresholds require required_memory_ids")
        return self


class MemoryContentMatchMode(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    NORMALIZED_EXACT = "normalized_exact"


class MemoryContentExpectation(ContractModel):
    content: NonEmptyText
    match: MemoryContentMatchMode = MemoryContentMatchMode.EXACT
    kind: MemoryKind | None = None


class MemoryStateExpectation(ContractModel):
    state_id: Identifier
    required_present_memory_ids: list[Identifier] = Field(default_factory=list)
    required_absent_memory_ids: list[Identifier] = Field(default_factory=list)
    required_added_content: list[MemoryContentExpectation] = Field(default_factory=list)
    forbidden_added_content: list[MemoryContentExpectation] = Field(default_factory=list)
    required_removed_memory_ids: list[Identifier] = Field(default_factory=list)
    unchanged_memory_ids: list[Identifier] = Field(default_factory=list)
    runtime_generated_memory_ids: list[Identifier] = Field(default_factory=list)
    allow_other_changes: StrictBool = False

    @field_validator(
        "required_present_memory_ids",
        "required_absent_memory_ids",
        "required_removed_memory_ids",
        "unchanged_memory_ids",
        "runtime_generated_memory_ids",
    )
    @classmethod
    def validate_unique_state_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Memory state ID lists must not repeat")
        return value

    @model_validator(mode="after")
    def validate_state_expectation(self) -> "MemoryStateExpectation":
        present = set(self.required_present_memory_ids)
        absent = set(self.required_absent_memory_ids)
        removed = set(self.required_removed_memory_ids)
        unchanged = set(self.unchanged_memory_ids)
        if present & absent:
            raise ValueError("present and absent Memory IDs must be disjoint")
        if present & removed:
            raise ValueError("present and removed Memory IDs must be disjoint")
        if absent & unchanged:
            raise ValueError("absent and unchanged Memory IDs must be disjoint")
        if removed & unchanged:
            raise ValueError("removed and unchanged Memory IDs must be disjoint")
        content_values: dict[str, set[str]] = {}
        for field_name in ("required_added_content", "forbidden_added_content"):
            values = [item.stable_json() for item in getattr(self, field_name)]
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not repeat")
            content_values[field_name] = set(values)
        if (
            content_values["required_added_content"]
            & content_values["forbidden_added_content"]
        ):
            raise ValueError(
                "required and forbidden added Memory content must be disjoint"
            )
        return self


class ExpectedSpec(ContractModel):
    files: list[FileExpectation] = Field(default_factory=list)
    texts: list[TextExpectation] = Field(default_factory=list)
    json_values: list[JsonExpectation] = Field(default_factory=list)
    tool_trajectories: list[ToolTrajectoryExpectation] = Field(default_factory=list)
    memories: list[MemoryExpectation] = Field(default_factory=list)
    memory_states: list[MemoryStateExpectation] = Field(default_factory=list)
    background_reviews: list[BackgroundReviewExpectation] = Field(default_factory=list)
    judges: list[JudgeExpectation] = Field(default_factory=list)


class EvaluatorKind(str, Enum):
    DETERMINISTIC = "deterministic"
    TOOL_TRAJECTORY = "tool_trajectory"
    LLM_JUDGE = "llm_judge"
    RETRIEVAL = "retrieval"
    COMPRESSION = "compression"
    BACKGROUND_REVIEW = "background_review"


class EvaluatorSpec(ContractModel):
    evaluator_id: Identifier
    kind: EvaluatorKind
    required: StrictBool
    weight: StrictInt | StrictFloat | None = Field(default=None, ge=0)
    config: JsonObject = Field(default_factory=dict)

    @field_validator("weight")
    @classmethod
    def validate_finite_weight(
        cls,
        value: int | float | None,
    ) -> int | float | None:
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("weight must be finite")
        return value

    @property
    def is_hard_gate(self) -> bool:
        """required=True 表示硬门禁，否则表示软评分。"""

        return self.required


class AuditCase(ContractModel):
    case_id: Identifier
    name: NonEmptyText
    description: StrictStr = ""
    mode: CaseMode
    tags: list[NonEmptyText] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)
    input: CaseInput
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    fixture: FixtureSpec = Field(default_factory=FixtureSpec)
    expected: ExpectedSpec = Field(default_factory=ExpectedSpec)
    evaluators: list[EvaluatorSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case(self) -> "AuditCase":
        input_value = self.input
        if self.mode is CaseMode.SINGLE_TURN and input_value.message is None:
            raise ValueError("single_turn mode requires input.message")
        if self.mode is CaseMode.SCRIPTED_MULTI_TURN and not input_value.turns:
            raise ValueError("scripted_multi_turn mode requires input.turns")
        if self.mode is CaseMode.SIMULATED_USER and input_value.simulated_user is None:
            raise ValueError("simulated_user mode requires input.simulated_user")
        evaluator_ids = [item.evaluator_id for item in self.evaluators]
        if len(evaluator_ids) != len(set(evaluator_ids)):
            raise ValueError("evaluator_id must be unique within an AuditCase")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("case tags must not repeat")
        if "data_classification" in self.metadata:
            classification_from_metadata(self.metadata)
        query_ids = [item.query_id for item in self.expected.memories]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Memory query_id must be unique within an AuditCase")
        state_ids = [item.state_id for item in self.expected.memory_states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("Memory state_id must be unique within an AuditCase")
        fixture_ids = {
            item.memory_id
            for item in (
                [] if self.fixture.memory is None else self.fixture.memory.items
            )
        }
        runtime_ids = {
            memory_id
            for expectation in (
                *self.expected.memories,
                *self.expected.memory_states,
            )
            for memory_id in expectation.runtime_generated_memory_ids
        }
        known_ids = fixture_ids | runtime_ids
        if fixture_ids & runtime_ids:
            raise ValueError("runtime-generated Memory IDs cannot shadow fixture IDs")
        referenced_ids = {
            memory_id
            for expectation in self.expected.memories
            for memory_id in (
                *expectation.required_memory_ids,
                *expectation.forbidden_memory_ids,
            )
        } | {
            memory_id
            for expectation in self.expected.memory_states
            for memory_id in (
                *expectation.required_present_memory_ids,
                *expectation.required_absent_memory_ids,
                *expectation.required_removed_memory_ids,
                *expectation.unchanged_memory_ids,
            )
        }
        unknown_ids = sorted(referenced_ids - known_ids)
        if unknown_ids:
            raise ValueError(
                "Memory expectation IDs must reference fixture or explicitly "
                "runtime-generated IDs: " + ", ".join(unknown_ids)
            )
        return self


class AuditSuite(ContractModel):
    schema_version: SchemaVersion = Field(
        description="Required top-level Audit Suite schema version."
    )
    suite_id: Identifier
    name: NonEmptyText
    description: StrictStr = ""
    tags: list[NonEmptyText] = Field(default_factory=list)
    defaults: TrialConfig = Field(default_factory=TrialConfig)
    cases: list[AuditCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suite(self) -> "AuditSuite":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id must be unique within an AuditSuite")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("suite tags must not repeat")
        suite_classification = classification_from_metadata(self.defaults.metadata)
        for case in self.cases:
            if "data_classification" not in case.metadata:
                continue
            case_classification = classification_from_metadata(case.metadata)
            if is_classification_downgrade(
                suite_classification,
                case_classification,
            ):
                raise ValueError(
                    f"case {case.case_id} cannot downgrade the Suite data classification"
                )
        return self
