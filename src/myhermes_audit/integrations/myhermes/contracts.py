"""Strict versioned file protocol shared by the parent and MyHermes worker."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, model_validator

from myhermes_audit.contracts import ToolsetName, TurnResult
from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    SafeRelativePath,
)


WORKER_PROTOCOL_VERSION = "myhermes-audit-worker-v1"
WorkerProtocolVersion = Literal["myhermes-audit-worker-v1"]


class WorkerMode(str, Enum):
    SINGLE_TURN = "single_turn"
    SCRIPTED_MULTI_TURN = "scripted_multi_turn"


class WorkerStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerArtifactPaths(ContractModel):
    worker_request: Path
    worker_result: Path
    transcript: Path
    observations: Path
    validator_results: Path
    stdout_log: Path
    stderr_log: Path

    @model_validator(mode="after")
    def validate_artifact_paths(self) -> "WorkerArtifactPaths":
        paths = [getattr(self, name) for name in type(self).model_fields if name != "schema_version"]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("worker artifact paths must be absolute")
        parents = {path.resolve(strict=False).parent for path in paths}
        if len(parents) != 1:
            raise ValueError("worker artifacts must share one artifacts directory")
        if len({path.resolve(strict=False) for path in paths}) != len(paths):
            raise ValueError("worker artifact paths must be unique")
        return self


class MyHermesWorkerRequest(ContractModel):
    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    mode: WorkerMode
    turns: list[NonEmptyText] = Field(min_length=1)
    workspace: Path
    hermes_home: Path
    sqlite_path: Path
    enabled_toolsets: list[ToolsetName] = Field(default_factory=list)
    timeout_seconds: PositiveInt
    artifact_paths: WorkerArtifactPaths

    @model_validator(mode="after")
    def validate_request(self) -> "MyHermesWorkerRequest":
        runtime_paths = (self.workspace, self.hermes_home, self.sqlite_path)
        if any(not path.is_absolute() for path in runtime_paths):
            raise ValueError("worker runtime paths must be absolute")
        if self.workspace.resolve(strict=False) == self.hermes_home.resolve(strict=False):
            raise ValueError("workspace and hermes_home must be distinct")
        if len(self.enabled_toolsets) != len(set(self.enabled_toolsets)):
            raise ValueError("enabled_toolsets must not repeat")
        return self


class WorkerError(ContractModel):
    error_type: Identifier
    message: NonEmptyText


class WorkerWarning(ContractModel):
    warning_type: Identifier
    message: NonEmptyText


class RunObservationRecord(ContractModel):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    status: NonEmptyText
    stop_reason: NonEmptyText
    iterations: NonNegativeInt
    tool_call_count: NonNegativeInt
    has_final_reply: StrictBool
    duration_ms: NonNegativeInt | None = None


class ModelObservationRecord(ContractModel):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    finish_reason: StrictStr | None = None
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    duration_ms: NonNegativeInt
    tool_call_count: NonNegativeInt
    error_category: Identifier | None = None

    @model_validator(mode="after")
    def validate_tokens(self) -> "ModelObservationRecord":
        if (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("model total_tokens must equal prompt plus completion")
        return self


class ToolObservationRecord(ContractModel):
    run_id: Identifier
    parent_run_id: Identifier | None = None
    tool_call_id: Identifier
    tool_name: NonEmptyText
    status: NonEmptyText
    success: StrictBool
    error_type: Identifier | None = None
    duration_ms: NonNegativeInt


class ObservationBundle(ContractModel):
    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    runs: list[RunObservationRecord] = Field(default_factory=list)
    model_calls: list[ModelObservationRecord] = Field(default_factory=list)
    tool_calls: list[ToolObservationRecord] = Field(default_factory=list)
    truncated: StrictBool = False

    @model_validator(mode="after")
    def validate_observations(self) -> "ObservationBundle":
        run_ids = [item.run_id for item in self.runs]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run observations must have unique run_id values")
        tool_call_ids = [item.tool_call_id for item in self.tool_calls]
        if len(tool_call_ids) != len(set(tool_call_ids)):
            raise ValueError("tool observations must have unique tool_call_id values")
        return self


class WorkerTranscript(ContractModel):
    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    turns: list[TurnResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_turns(self) -> "WorkerTranscript":
        numbers = [turn.turn_number for turn in self.turns]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("transcript turns must be contiguous from 1")
        return self


class MyHermesWorkerResult(ContractModel):
    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    worker_status: WorkerStatus
    runtime_status: StrictStr | None = None
    final_output: StrictStr | None = None
    turns: list[TurnResult] = Field(default_factory=list)
    run_ids: list[Identifier] = Field(default_factory=list)
    error_type: Identifier | None = None
    fatal: StrictBool = False
    retryable: StrictBool = False
    iterations: NonNegativeInt = 0
    tool_batches: NonNegativeInt = 0
    tool_call_count: NonNegativeInt = 0
    tool_names: list[NonEmptyText] = Field(default_factory=list)
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    duration_ms: NonNegativeInt
    observations_artifact: SafeRelativePath
    transcript_artifact: SafeRelativePath
    warnings: list[WorkerWarning] = Field(default_factory=list)
    error: WorkerError | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "MyHermesWorkerResult":
        if self.runtime_status is None or not self.runtime_status.strip():
            raise ValueError("worker results require runtime_status")
        if self.worker_status is WorkerStatus.COMPLETED:
            if self.error is not None or self.error_type is not None:
                raise ValueError("completed worker results must not contain error")
            if self.fatal or self.retryable:
                raise ValueError("completed worker results cannot be fatal or retryable")
            if not self.turns:
                raise ValueError("completed worker results require at least one turn")
            if len(self.run_ids) != len(self.turns):
                raise ValueError("completed worker results require one run_id per turn")
            if self.final_output != self.turns[-1].final_output:
                raise ValueError("completed final_output must match the last turn")
        else:
            if self.error is None or self.error_type is None:
                raise ValueError("failed worker results require error and error_type")
            if self.error.error_type != self.error_type:
                raise ValueError("worker error_type fields must agree")
            if self.final_output is not None:
                raise ValueError("failed worker results cannot contain final_output")
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("tool_names must be unique in first-seen order")
        expected_run_ids = [turn.run_id for turn in self.turns if turn.run_id is not None]
        if self.run_ids != expected_run_ids:
            raise ValueError("run_ids must match the ordered non-null turn run_ids")
        if len(self.run_ids) != len(set(self.run_ids)):
            raise ValueError("run_ids must not repeat")
        turn_numbers = [turn.turn_number for turn in self.turns]
        if turn_numbers != list(range(1, len(turn_numbers) + 1)):
            raise ValueError("worker result turns must be contiguous from 1")
        warning_types = [warning.warning_type for warning in self.warnings]
        if len(warning_types) != len(set(warning_types)):
            raise ValueError("worker warning types must not repeat")
        if (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        return self


__all__ = (
    "MyHermesWorkerRequest",
    "MyHermesWorkerResult",
    "ModelObservationRecord",
    "ObservationBundle",
    "RunObservationRecord",
    "ToolObservationRecord",
    "WORKER_PROTOCOL_VERSION",
    "WorkerArtifactPaths",
    "WorkerError",
    "WorkerMode",
    "WorkerStatus",
    "WorkerTranscript",
    "WorkerWarning",
)
