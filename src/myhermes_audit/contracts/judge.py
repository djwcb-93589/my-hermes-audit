"""Strict LLM-as-a-Judge request, configuration, and result contracts."""

from __future__ import annotations

import math
from typing import Annotated

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    SafeRelativePath,
)


JUDGE_PROMPT_VERSION = "answer-quality-v1"
JUDGE_RESULT_SCHEMA_VERSION = "judge-result-v1"
ShortJudgeText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
]


class JudgeCriterion(ContractModel):
    name: Identifier
    description: NonEmptyText
    weight: StrictInt | StrictFloat = Field(default=1.0, gt=0)

    @field_validator("weight")
    @classmethod
    def validate_finite_weight(cls, value: int | float) -> int | float:
        if not math.isfinite(float(value)):
            raise ValueError("weight must be finite")
        return value


def default_judge_criteria() -> list[JudgeCriterion]:
    return [
        JudgeCriterion(
            name="correctness",
            description="The response agrees with the trusted runtime evidence.",
            weight=0.4,
        ),
        JudgeCriterion(
            name="completeness",
            description="The response includes the outcome needed by the user.",
            weight=0.3,
        ),
        JudgeCriterion(
            name="instruction_following",
            description="The response follows the requested constraints and format.",
            weight=0.3,
        ),
    ]


class JudgeExpectation(ContractModel):
    rubric: NonEmptyText
    criteria: list[JudgeCriterion] = Field(
        default_factory=default_judge_criteria,
        max_length=5,
    )
    minimum_score: StrictInt | StrictFloat | None = Field(
        default=0.7,
        ge=0,
        le=1,
    )
    maximum_score: StrictInt | StrictFloat | None = Field(
        default=1.0,
        ge=0,
        le=1,
    )

    @field_validator("criteria")
    @classmethod
    def apply_default_criteria(
        cls,
        value: list[JudgeCriterion],
    ) -> list[JudgeCriterion]:
        return value or default_judge_criteria()

    @model_validator(mode="after")
    def validate_score_range(self) -> "JudgeExpectation":
        names = [criterion.name for criterion in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("judge criterion names must be unique")
        for value in (self.minimum_score, self.maximum_score):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("judge score bounds must be finite")
        if (
            self.minimum_score is not None
            and self.maximum_score is not None
            and self.minimum_score > self.maximum_score
        ):
            raise ValueError("minimum_score cannot exceed maximum_score")
        return self


class JudgeRequest(ContractModel):
    judge_id: Identifier
    task_input: StrictStr
    case_mode: Identifier
    final_output: StrictStr
    rubric: NonEmptyText
    criteria: list[JudgeCriterion] = Field(min_length=1, max_length=5)
    deterministic_summary: StrictStr
    tool_summary: StrictStr
    conversation_summary: StrictStr | None = None
    minimum_score: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=1)
    maximum_score: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=1)
    prompt_version: NonEmptyText = JUDGE_PROMPT_VERSION
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request(self) -> "JudgeRequest":
        names = [criterion.name for criterion in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("judge request criterion names must be unique")
        if (
            self.minimum_score is not None
            and self.maximum_score is not None
            and self.minimum_score > self.maximum_score
        ):
            raise ValueError("minimum_score cannot exceed maximum_score")
        return self


class JudgeCriterionResult(ContractModel):
    name: Identifier
    score: StrictInt | StrictFloat = Field(ge=0, le=1)
    passed: StrictBool | None = None
    reason: ShortJudgeText
    evidence: list[ShortJudgeText] = Field(default_factory=list, max_length=5)


class JudgeResult(ContractModel):
    result_schema_version: NonEmptyText = JUDGE_RESULT_SCHEMA_VERSION
    judge_id: Identifier
    judge_model: NonEmptyText
    judge_provider: Identifier
    prompt_version: NonEmptyText
    overall_score: StrictFloat = Field(ge=0, le=1)
    passed: StrictBool
    criteria: list[JudgeCriterionResult] = Field(min_length=1, max_length=5)
    summary: ShortJudgeText
    duration_ms: NonNegativeInt
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    retry_count: NonNegativeInt = 0
    raw_response_artifact: SafeRelativePath | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_result(self) -> "JudgeResult":
        names = [criterion.name for criterion in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("judge result criterion names must be unique")
        if not math.isfinite(self.overall_score):
            raise ValueError("overall_score must be finite")
        if (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("judge total_tokens must equal prompt plus completion")
        return self


class JudgeRunSummary(ContractModel):
    declared_count: NonNegativeInt = 0
    completed_count: NonNegativeInt = 0
    skipped_count: NonNegativeInt = 0
    error_count: NonNegativeInt = 0
    not_applicable_count: NonNegativeInt = 0
    mean_answer_quality: StrictFloat | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts(self) -> "JudgeRunSummary":
        classified = (
            self.completed_count
            + self.skipped_count
            + self.error_count
            + self.not_applicable_count
        )
        if classified != self.declared_count:
            raise ValueError("judge status counts must equal declared_count")
        if self.completed_count == 0 and self.mean_answer_quality is not None:
            raise ValueError("mean_answer_quality requires a completed Judge result")
        if self.completed_count > 0 and self.mean_answer_quality is None:
            raise ValueError("completed Judge results require mean_answer_quality")
        return self


__all__ = (
    "JUDGE_PROMPT_VERSION",
    "JUDGE_RESULT_SCHEMA_VERSION",
    "JudgeCriterion",
    "JudgeCriterionResult",
    "JudgeExpectation",
    "JudgeRequest",
    "JudgeResult",
    "JudgeRunSummary",
    "default_judge_criteria",
)
