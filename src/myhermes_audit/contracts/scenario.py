"""Strict P6.1 end-to-end scenario contracts.

The scenario models deliberately describe intent and safe observations rather
than an executable command DSL.  Commands and input bodies are never copied
into execution results; only bounded, content-free projections are retained.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    FixtureTargetPath,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    SafeRelativePath,
    Sha256Digest,
    UtcDatetime,
)


ScenarioTimeout = Annotated[PositiveInt, Field(le=3600)]
ScenarioStepTimeout = Annotated[PositiveInt, Field(le=600)]


class E2EScenarioKind(str, Enum):
    TOOLCHAIN = "toolchain"
    PROCESS_BACKGROUND = "process_background"


class ScenarioStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_EVALUABLE = "not_evaluable"
    ERROR = "error"


class ScenarioProcessStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    KILLED = "killed"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


_ACTIVE_PROCESS_STATUSES = frozenset(
    {
        ScenarioProcessStatus.STARTING,
        ScenarioProcessStatus.RUNNING,
        ScenarioProcessStatus.WAITING_FOR_INPUT,
    }
)
_TERMINAL_PROCESS_STATUSES = frozenset(
    {
        ScenarioProcessStatus.COMPLETED,
        ScenarioProcessStatus.FAILED,
        ScenarioProcessStatus.INTERRUPTED,
        ScenarioProcessStatus.KILLED,
        ScenarioProcessStatus.TIMED_OUT,
    }
)


class ProcessAction(str, Enum):
    START = "start"
    READ_INCREMENTAL = "read_incremental"
    SEND_INPUT = "send_input"
    WAIT = "wait"
    INTERRUPT = "interrupt"
    KILL = "kill"
    ASSERT_STATUS = "assert_status"
    CLEANUP_SESSION = "cleanup_session"


class ScenarioCheckpoint(ContractModel):
    checkpoint_id: Identifier
    required: StrictBool = True


class ScenarioToolCall(ContractModel):
    tool_name: NonEmptyText
    arguments: JsonObject = Field(default_factory=dict)


class ScenarioTraceRequirement(ContractModel):
    tool_name: NonEmptyText
    minimum_calls: NonNegativeInt = 1
    minimum_successful_calls: NonNegativeInt = 0
    required: StrictBool = True

    @model_validator(mode="after")
    def validate_counts(self) -> "ScenarioTraceRequirement":
        if self.minimum_successful_calls > self.minimum_calls:
            raise ValueError(
                "minimum_successful_calls cannot exceed minimum_calls"
            )
        return self


class ScenarioPlanBase(ContractModel):
    scenario_id: Identifier
    timeout_seconds: ScenarioTimeout = 120
    required: StrictBool = True
    checkpoints: list[ScenarioCheckpoint] = Field(default_factory=list)
    required_toolsets: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_checkpoints(self) -> "ScenarioPlanBase":
        checkpoint_ids = [item.checkpoint_id for item in self.checkpoints]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("scenario checkpoint IDs must be unique")
        if len(self.required_toolsets) != len(set(self.required_toolsets)):
            raise ValueError("scenario required toolsets must be unique")
        allowed_toolsets = {"file", "terminal", "memory", "skill_read"}
        unknown = sorted(set(self.required_toolsets) - allowed_toolsets)
        if unknown:
            raise ValueError(
                "scenario required_toolsets contains unknown values: "
                + ", ".join(unknown)
            )
        return self


class ToolchainScenarioPlan(ScenarioPlanBase):
    kind: Literal[E2EScenarioKind.TOOLCHAIN] = E2EScenarioKind.TOOLCHAIN
    required_tool_calls: list[ScenarioToolCall] = Field(default_factory=list)
    forbidden_tool_calls: list[ScenarioToolCall] = Field(default_factory=list)
    input_artifacts: list[FixtureTargetPath] = Field(default_factory=list)
    output_artifacts: list[FixtureTargetPath] = Field(default_factory=list)
    trace_requirements: list[ScenarioTraceRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_toolchain(self) -> "ToolchainScenarioPlan":
        if len(self.input_artifacts) != len(set(self.input_artifacts)):
            raise ValueError("toolchain input_artifacts must be unique")
        if len(self.output_artifacts) != len(set(self.output_artifacts)):
            raise ValueError("toolchain output_artifacts must be unique")
        required_names = {item.tool_name for item in self.required_tool_calls}
        forbidden_names = {item.tool_name for item in self.forbidden_tool_calls}
        if required_names & forbidden_names:
            raise ValueError("a tool cannot be both required and forbidden")
        trace_names = [item.tool_name for item in self.trace_requirements]
        if len(trace_names) != len(set(trace_names)):
            raise ValueError("scenario trace requirement tool names must be unique")
        return self


class ProcessStepBase(ContractModel):
    step_id: Identifier
    required: StrictBool = True
    timeout_seconds: ScenarioStepTimeout = 30


class ProcessStartStep(ProcessStepBase):
    action: Literal[ProcessAction.START] = ProcessAction.START
    command: NonEmptyText
    expected_initial_status: ScenarioProcessStatus = ScenarioProcessStatus.RUNNING

    @model_validator(mode="after")
    def validate_initial_status(self) -> "ProcessStartStep":
        if self.expected_initial_status not in _ACTIVE_PROCESS_STATUSES:
            raise ValueError("Process start must expect an active status")
        return self


class ProcessReadIncrementalStep(ProcessStepBase):
    action: Literal[ProcessAction.READ_INCREMENTAL] = ProcessAction.READ_INCREMENTAL
    minimum_new_output_length: NonNegativeInt = 0
    required_markers: list[NonEmptyText] = Field(default_factory=list)
    forbidden_markers: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_markers(self) -> "ProcessReadIncrementalStep":
        if set(self.required_markers) & set(self.forbidden_markers):
            raise ValueError("required and forbidden markers must be disjoint")
        return self


class ProcessSendInputStep(ProcessStepBase):
    action: Literal[ProcessAction.SEND_INPUT] = ProcessAction.SEND_INPUT
    input_source: SafeRelativePath
    submit: StrictBool = True


class ProcessWaitStep(ProcessStepBase):
    action: Literal[ProcessAction.WAIT] = ProcessAction.WAIT
    expected_status: ScenarioProcessStatus
    maximum_wait_seconds: Annotated[PositiveInt, Field(le=600)] = 30


class ProcessInterruptStep(ProcessStepBase):
    action: Literal[ProcessAction.INTERRUPT] = ProcessAction.INTERRUPT
    expected_terminal_status: ScenarioProcessStatus

    @model_validator(mode="after")
    def validate_terminal_status(self) -> "ProcessInterruptStep":
        if self.expected_terminal_status not in _TERMINAL_PROCESS_STATUSES:
            raise ValueError("Process interrupt must expect a terminal status")
        return self


class ProcessKillStep(ProcessStepBase):
    action: Literal[ProcessAction.KILL] = ProcessAction.KILL
    expected_terminal_status: ScenarioProcessStatus

    @model_validator(mode="after")
    def validate_terminal_status(self) -> "ProcessKillStep":
        if self.expected_terminal_status not in _TERMINAL_PROCESS_STATUSES:
            raise ValueError("Process kill must expect a terminal status")
        return self


class ProcessAssertStatusStep(ProcessStepBase):
    action: Literal[ProcessAction.ASSERT_STATUS] = ProcessAction.ASSERT_STATUS
    expected_status: ScenarioProcessStatus


class ProcessCleanupSessionStep(ProcessStepBase):
    action: Literal[ProcessAction.CLEANUP_SESSION] = ProcessAction.CLEANUP_SESSION
    expect_no_live_processes: StrictBool = True


ProcessStep = Annotated[
    ProcessStartStep
    | ProcessReadIncrementalStep
    | ProcessSendInputStep
    | ProcessWaitStep
    | ProcessInterruptStep
    | ProcessKillStep
    | ProcessAssertStatusStep
    | ProcessCleanupSessionStep,
    Field(discriminator="action"),
]


class ProcessScenarioPlan(ScenarioPlanBase):
    kind: Literal[E2EScenarioKind.PROCESS_BACKGROUND] = (
        E2EScenarioKind.PROCESS_BACKGROUND
    )
    steps: list[ProcessStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> "ProcessScenarioPlan":
        step_ids = [item.step_id for item in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Process scenario step IDs must be unique")
        if not any(item.required for item in self.steps):
            raise ValueError("Process scenario requires at least one required step")
        return self


ScenarioPlan = Annotated[
    ToolchainScenarioPlan | ProcessScenarioPlan,
    Field(discriminator="kind"),
]


class ScenarioError(ContractModel):
    error_type: Identifier
    message: NonEmptyText
    step_id: Identifier | None = None
    retryable: StrictBool = False


class ScenarioCheckpointResult(ContractModel):
    checkpoint_id: Identifier
    required: StrictBool
    passed: StrictBool | None = None
    observed_status: ScenarioStatus | None = None
    error: ScenarioError | None = None


class ScenarioArtifactObservation(ContractModel):
    relative_path: FixtureTargetPath
    exists: StrictBool
    sha256: Sha256Digest | None = None
    size_bytes: NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_artifact(self) -> "ScenarioArtifactObservation":
        if not self.exists and (self.sha256 is not None or self.size_bytes != 0):
            raise ValueError("missing scenario artifacts cannot contain size or hash")
        if self.exists and self.sha256 is None:
            raise ValueError("existing scenario artifacts require a hash")
        return self


class ScenarioToolCallObservation(ContractModel):
    tool_name: NonEmptyText
    call_count: NonNegativeInt = 0
    successful_count: NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_counts(self) -> "ScenarioToolCallObservation":
        if self.successful_count > self.call_count:
            raise ValueError("successful_count cannot exceed call_count")
        return self


class ToolchainScenarioExecutionResult(ContractModel):
    scenario_id: Identifier
    kind: Literal[E2EScenarioKind.TOOLCHAIN] = E2EScenarioKind.TOOLCHAIN
    status: ScenarioStatus
    checkpoints: list[ScenarioCheckpointResult] = Field(default_factory=list)
    input_artifacts: list[ScenarioArtifactObservation] = Field(default_factory=list)
    output_artifacts: list[ScenarioArtifactObservation] = Field(default_factory=list)
    tool_calls: list[ScenarioToolCallObservation] = Field(default_factory=list)
    final_response_present: StrictBool = False
    duration_ms: NonNegativeInt = 0
    errors: list[ScenarioError] = Field(default_factory=list)


class ScenarioStepResult(ContractModel):
    step_id: Identifier
    action: ProcessAction
    status: ScenarioStatus
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    duration_ms: NonNegativeInt = 0
    observation_refs: list[Identifier] = Field(default_factory=list)
    error: ScenarioError | None = None

    @model_validator(mode="after")
    def validate_times(self) -> "ScenarioStepResult":
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("scenario step completed_at cannot precede started_at")
        return self


class IncrementalReadObservation(ContractModel):
    read_index: NonNegativeInt
    offset_before: NonNegativeInt
    offset_after: NonNegativeInt
    new_output_length: NonNegativeInt
    content_sha256: Sha256Digest | None = None
    required_markers_found: list[Identifier] = Field(default_factory=list)
    required_markers_missing: list[Identifier] = Field(default_factory=list)
    forbidden_markers_found: list[Identifier] = Field(default_factory=list)
    truncated: StrictBool = False

    @model_validator(mode="after")
    def validate_offsets(self) -> "IncrementalReadObservation":
        if self.offset_after < self.offset_before:
            raise ValueError("incremental read offsets must be monotonic")
        if self.new_output_length != self.offset_after - self.offset_before:
            raise ValueError("incremental read length must match offset delta")
        return self


class ProcessInputObservation(ContractModel):
    input_source: SafeRelativePath
    submitted: StrictBool
    accepted: StrictBool
    bytes_written: NonNegativeInt | None = None


class ProcessCleanupResult(ContractModel):
    attempted_process_ids: list[Identifier] = Field(default_factory=list)
    completed_process_ids: list[Identifier] = Field(default_factory=list)
    unresolved_process_ids: list[Identifier] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unresolved_process_ids


class ProcessScenarioExecutionResult(ContractModel):
    scenario_id: Identifier
    kind: Literal[E2EScenarioKind.PROCESS_BACKGROUND] = (
        E2EScenarioKind.PROCESS_BACKGROUND
    )
    status: ScenarioStatus
    checkpoints: list[ScenarioCheckpointResult] = Field(default_factory=list)
    steps: list[ScenarioStepResult] = Field(default_factory=list)
    process_id_safe: Identifier | None = None
    session_id_safe: Identifier | None = None
    initial_status: ScenarioProcessStatus | None = None
    final_status: ScenarioProcessStatus | None = None
    incremental_reads: list[IncrementalReadObservation] = Field(default_factory=list)
    input_events: list[ProcessInputObservation] = Field(default_factory=list)
    interrupt_requested: StrictBool = False
    kill_requested: StrictBool = False
    cleanup_result: ProcessCleanupResult | None = None
    duration_ms: NonNegativeInt = 0
    errors: list[ScenarioError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_transition(self) -> "ProcessScenarioExecutionResult":
        if self.initial_status in _TERMINAL_PROCESS_STATUSES and self.final_status in _ACTIVE_PROCESS_STATUSES:
            raise ValueError("terminal Process cannot transition back to active")
        return self


ScenarioExecutionResult = Annotated[
    ToolchainScenarioExecutionResult | ProcessScenarioExecutionResult,
    Field(discriminator="kind"),
]


__all__ = (
    "E2EScenarioKind",
    "IncrementalReadObservation",
    "ProcessAction",
    "ProcessAssertStatusStep",
    "ProcessCleanupResult",
    "ProcessCleanupSessionStep",
    "ProcessInputObservation",
    "ProcessInterruptStep",
    "ProcessKillStep",
    "ProcessReadIncrementalStep",
    "ProcessScenarioExecutionResult",
    "ProcessScenarioPlan",
    "ProcessSendInputStep",
    "ProcessStartStep",
    "ProcessStep",
    "ProcessWaitStep",
    "ScenarioArtifactObservation",
    "ScenarioCheckpoint",
    "ScenarioCheckpointResult",
    "ScenarioError",
    "ScenarioExecutionResult",
    "ScenarioPlan",
    "ScenarioProcessStatus",
    "ScenarioStatus",
    "ScenarioStepResult",
    "ScenarioToolCall",
    "ScenarioToolCallObservation",
    "ScenarioTraceRequirement",
    "ToolchainScenarioExecutionResult",
    "ToolchainScenarioPlan",
)
