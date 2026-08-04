"""Strict P6.1 end-to-end scenario contracts.

Scenario plans describe public observations, never an executable Process DSL.
Commands, input bodies and raw output are deliberately absent from result
contracts; only bounded hashes, lengths and safe identifiers are retained.
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
    CLOSE = "close"
    ASSERT_STATUS = "assert_status"


class ScenarioCheckpointKind(str, Enum):
    STEP_STATUS = "step_status"
    PROCESS_STATUS = "process_status"
    OUTPUT = "output"
    CLEANUP = "cleanup"


class ScenarioCheckpointBase(ContractModel):
    checkpoint_id: Identifier
    required: StrictBool = True


class StepStatusCheckpoint(ScenarioCheckpointBase):
    kind: Literal[ScenarioCheckpointKind.STEP_STATUS] = ScenarioCheckpointKind.STEP_STATUS
    target_step_id: Identifier
    expected_step_status: ScenarioStatus


class ProcessStatusCheckpoint(ScenarioCheckpointBase):
    kind: Literal[ScenarioCheckpointKind.PROCESS_STATUS] = ScenarioCheckpointKind.PROCESS_STATUS
    target_step_id: Identifier
    expected_process_status: ScenarioProcessStatus


class OutputCheckpoint(ScenarioCheckpointBase):
    kind: Literal[ScenarioCheckpointKind.OUTPUT] = ScenarioCheckpointKind.OUTPUT
    target_step_id: Identifier
    artifact_scope: Literal["input", "output"] | None = None
    required_markers: list[NonEmptyText] = Field(default_factory=list)
    forbidden_markers: list[NonEmptyText] = Field(default_factory=list)
    minimum_new_output_length: NonNegativeInt = 0

    @model_validator(mode="after")
    def validate_markers(self) -> "OutputCheckpoint":
        if set(self.required_markers) & set(self.forbidden_markers):
            raise ValueError("checkpoint required and forbidden markers must be disjoint")
        return self


class CleanupCheckpoint(ScenarioCheckpointBase):
    kind: Literal[ScenarioCheckpointKind.CLEANUP] = ScenarioCheckpointKind.CLEANUP
    expect_agent_close: StrictBool = False
    expect_worker_cleanup: StrictBool = True
    expect_no_live_processes: StrictBool = True


ScenarioCheckpoint = Annotated[
    StepStatusCheckpoint
    | ProcessStatusCheckpoint
    | OutputCheckpoint
    | CleanupCheckpoint,
    Field(discriminator="kind"),
]


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
            raise ValueError("minimum_successful_calls cannot exceed minimum_calls")
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
            raise ValueError("scenario required_toolsets must be unique")
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
        for checkpoint in self.checkpoints:
            if isinstance(checkpoint, OutputCheckpoint) and checkpoint.artifact_scope is None:
                raise ValueError("Toolchain output checkpoints require artifact_scope")
        return self


class ProcessStepBase(ContractModel):
    step_id: Identifier
    required: StrictBool = True
    timeout_seconds: ScenarioStepTimeout = 30
    process_ref_step_id: Identifier | None = None


class ProcessStartStep(ProcessStepBase):
    action: Literal[ProcessAction.START] = ProcessAction.START
    command: NonEmptyText
    expected_initial_status: ScenarioProcessStatus = ScenarioProcessStatus.RUNNING

    @model_validator(mode="after")
    def validate_initial_status(self) -> "ProcessStartStep":
        if self.expected_initial_status not in _ACTIVE_PROCESS_STATUSES:
            raise ValueError("Process start must expect an active status")
        if self.process_ref_step_id is not None:
            raise ValueError("Process start cannot reference another process step")
        return self


class ProcessReadIncrementalStep(ProcessStepBase):
    action: Literal[ProcessAction.READ_INCREMENTAL] = ProcessAction.READ_INCREMENTAL
    cursor_before: NonNegativeInt = 0
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


class ProcessCloseStep(ProcessStepBase):
    action: Literal[ProcessAction.CLOSE] = ProcessAction.CLOSE


class ProcessAssertStatusStep(ProcessStepBase):
    action: Literal[ProcessAction.ASSERT_STATUS] = ProcessAction.ASSERT_STATUS
    expected_status: ScenarioProcessStatus


ProcessStep = Annotated[
    ProcessStartStep
    | ProcessReadIncrementalStep
    | ProcessSendInputStep
    | ProcessWaitStep
    | ProcessInterruptStep
    | ProcessKillStep
    | ProcessCloseStep
    | ProcessAssertStatusStep,
    Field(discriminator="action"),
]


class ProcessCleanupExpectation(ContractModel):
    required: StrictBool = True
    expect_no_live_processes: StrictBool = True
    expect_session_resources_released: StrictBool = True


class ProcessScenarioPlan(ScenarioPlanBase):
    kind: Literal[E2EScenarioKind.PROCESS_BACKGROUND] = E2EScenarioKind.PROCESS_BACKGROUND
    steps: list[ProcessStep] = Field(min_length=1)
    cleanup: ProcessCleanupExpectation | None = None

    @model_validator(mode="after")
    def validate_steps(self) -> "ProcessScenarioPlan":
        step_ids = [item.step_id for item in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Process scenario step IDs must be unique")
        if not any(item.required for item in self.steps):
            raise ValueError("Process scenario requires at least one required step")
        starts = [index for index, item in enumerate(self.steps) if item.action is ProcessAction.START]
        if len(starts) != 1:
            raise ValueError("Process scenario requires exactly one start step")
        start_index = starts[0]
        for index, step in enumerate(self.steps):
            if step.timeout_seconds > self.timeout_seconds:
                raise ValueError("Process step timeout cannot exceed scenario timeout")
            if isinstance(step, ProcessWaitStep) and step.maximum_wait_seconds > step.timeout_seconds:
                raise ValueError("wait maximum_wait_seconds cannot exceed step timeout")
            if index < start_index and step.action is not ProcessAction.START:
                raise ValueError("Process start must precede every Process operation")
            if step.process_ref_step_id is not None and step.process_ref_step_id != self.steps[start_index].step_id:
                raise ValueError("Process steps may reference only the start step")
            if index > start_index and step.process_ref_step_id == self.steps[start_index].step_id:
                continue
        terminal_seen = False
        previous_cursor = 0
        for step in self.steps[start_index + 1 :]:
            if step.action in {ProcessAction.KILL, ProcessAction.INTERRUPT, ProcessAction.CLOSE}:
                terminal_seen = True
            elif step.action is ProcessAction.SEND_INPUT and terminal_seen:
                raise ValueError("send_input cannot follow a terminal Process action")
            if isinstance(step, ProcessReadIncrementalStep):
                if step.cursor_before < previous_cursor:
                    raise ValueError("read cursor declarations must be monotonic")
                previous_cursor = step.cursor_before
        known_steps = set(step_ids)
        has_close_step = any(item.action is ProcessAction.CLOSE for item in self.steps)
        for checkpoint in self.checkpoints:
            target = getattr(checkpoint, "target_step_id", None)
            if target is not None and target not in known_steps:
                raise ValueError("checkpoint target_step_id must reference a declared step")
            if isinstance(checkpoint, OutputCheckpoint) and checkpoint.artifact_scope is not None:
                raise ValueError("Process output checkpoints cannot set artifact_scope")
            if (
                isinstance(checkpoint, CleanupCheckpoint)
                and (checkpoint.expect_worker_cleanup or checkpoint.expect_no_live_processes)
                and (self.cleanup is None or not self.cleanup.required)
            ):
                raise ValueError(
                    "cleanup checkpoint requiring Worker cleanup needs a cleanup expectation"
                )
            if isinstance(checkpoint, CleanupCheckpoint) and checkpoint.expect_agent_close and not has_close_step:
                raise ValueError(
                    "cleanup checkpoint requiring Agent close needs a close step"
                )
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
    kind: ScenarioCheckpointKind
    required: StrictBool
    target_step_id: Identifier | None = None
    artifact_scope: Literal["input", "output"] | None = None
    passed: StrictBool | None = None
    observed_step_status: ScenarioStatus | None = None
    observed_process_status: ScenarioProcessStatus | None = None
    agent_close_observed: StrictBool | None = None
    worker_cleanup_completed: StrictBool | None = None
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
    actual_action: NonEmptyText | None = None
    actual_status: ScenarioProcessStatus | None = None
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    duration_ms: NonNegativeInt | None = None
    timeout_seconds: PositiveInt
    timed_out: StrictBool = False
    observation_refs: list[Identifier] = Field(default_factory=list)
    expected_process_id_safe: Identifier | None = None
    actual_process_id_safe: Identifier | None = None
    process_identity_matched: StrictBool | None = None
    action_matched: StrictBool | None = None
    error: ScenarioError | None = None

    @model_validator(mode="after")
    def validate_times(self) -> "ScenarioStepResult":
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("scenario step completed_at cannot precede started_at")
        if self.duration_ms is not None and self.timeout_seconds is not None:
            if self.timed_out != (self.duration_ms > self.timeout_seconds * 1000):
                raise ValueError("step timed_out must match duration and timeout")
        return self


class IncrementalReadObservation(ContractModel):
    read_index: NonNegativeInt
    cursor_unit: Literal["character"] = "character"
    cursor_before: NonNegativeInt
    cursor_after: NonNegativeInt
    new_output_char_length: NonNegativeInt
    new_output_utf8_bytes: NonNegativeInt
    content_sha256: Sha256Digest | None = None
    required_markers_found: list[Identifier] = Field(default_factory=list)
    required_markers_missing: list[Identifier] = Field(default_factory=list)
    forbidden_markers_found: list[Identifier] = Field(default_factory=list)
    truncated: StrictBool = False

    @model_validator(mode="after")
    def validate_cursors(self) -> "IncrementalReadObservation":
        if self.cursor_after < self.cursor_before:
            raise ValueError("incremental read cursors must be monotonic")
        if self.new_output_char_length != self.cursor_after - self.cursor_before:
            raise ValueError("incremental read length must match character cursor delta")
        return self


class ProcessInputObservation(ContractModel):
    input_source: SafeRelativePath
    submitted: StrictBool
    accepted: StrictBool
    expected_input_sha256: Sha256Digest | None = None
    actual_input_sha256: Sha256Digest | None = None
    expected_input_char_length: NonNegativeInt | None = None
    actual_input_char_length: NonNegativeInt | None = None
    expected_input_utf8_bytes: NonNegativeInt | None = None
    actual_input_utf8_bytes: NonNegativeInt | None = None
    input_matched: StrictBool | None = None
    process_id_safe: Identifier | None = None
    process_identity_matched: StrictBool | None = None
    action_matched: StrictBool | None = None
    bytes_written: NonNegativeInt | None = None


class ProcessCleanupResult(ContractModel):
    required: StrictBool = True
    expect_no_live_processes: StrictBool = True
    expect_session_resources_released: StrictBool = True
    live_process_count_before: NonNegativeInt | None = None
    live_process_count_after: NonNegativeInt | None = None
    session_cleanup_completed: StrictBool = False
    cleanup_errors: list[Identifier] = Field(default_factory=list)
    attempted_process_ids: list[Identifier] = Field(default_factory=list)
    completed_process_ids: list[Identifier] = Field(default_factory=list)
    unresolved_process_ids: list[Identifier] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        if not self.required:
            return True
        if self.unresolved_process_ids or self.cleanup_errors:
            return False
        if self.expect_no_live_processes and self.live_process_count_after != 0:
            return False
        if self.expect_session_resources_released and not self.session_cleanup_completed:
            return False
        return True


class ProcessScenarioExecutionResult(ContractModel):
    scenario_id: Identifier
    kind: Literal[E2EScenarioKind.PROCESS_BACKGROUND] = E2EScenarioKind.PROCESS_BACKGROUND
    status: ScenarioStatus
    checkpoints: list[ScenarioCheckpointResult] = Field(default_factory=list)
    steps: list[ScenarioStepResult] = Field(default_factory=list)
    declared_command_sha256: Sha256Digest | None = None
    actual_command_sha256: Sha256Digest | None = None
    declared_command_length: NonNegativeInt | None = None
    actual_command_length: NonNegativeInt | None = None
    command_matched: StrictBool | None = None
    process_id_safe: Identifier | None = None
    expected_process_id_safe: Identifier | None = None
    process_identity_matched: StrictBool | None = None
    initial_status: ScenarioProcessStatus | None = None
    final_status: ScenarioProcessStatus | None = None
    cursor_unit: Literal["character"] = "character"
    incremental_reads: list[IncrementalReadObservation] = Field(default_factory=list)
    input_events: list[ProcessInputObservation] = Field(default_factory=list)
    input_matched: StrictBool | None = None
    status_transitions_valid: StrictBool | None = None
    scenario_timeout_seconds: PositiveInt
    scenario_timed_out: StrictBool = False
    agent_close_observed: StrictBool = False
    worker_cleanup_result: ProcessCleanupResult | None = None
    # A Process duration is only present when the public Observation window
    # supplied enough real timing facts.  ``None`` is deliberately distinct
    # from a measured zero so validators cannot turn missing timing into a
    # successful timeout gate.
    duration_ms: NonNegativeInt | None = None
    errors: list[ScenarioError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_transition(self) -> "ProcessScenarioExecutionResult":
        if self.initial_status in _TERMINAL_PROCESS_STATUSES and self.final_status in _ACTIVE_PROCESS_STATUSES:
            raise ValueError("terminal Process cannot transition back to active")
        if (
            self.duration_ms is not None
            and self.duration_ms > self.scenario_timeout_seconds * 1000
            and not self.scenario_timed_out
        ):
            raise ValueError("scenario_timed_out must reflect the scenario deadline")
        return self


ScenarioExecutionResult = Annotated[
    ToolchainScenarioExecutionResult | ProcessScenarioExecutionResult,
    Field(discriminator="kind"),
]


__all__ = (
    "CleanupCheckpoint",
    "E2EScenarioKind",
    "IncrementalReadObservation",
    "OutputCheckpoint",
    "ProcessAction",
    "ProcessAssertStatusStep",
    "ProcessCleanupExpectation",
    "ProcessCleanupResult",
    "ProcessCloseStep",
    "ProcessInputObservation",
    "ProcessInterruptStep",
    "ProcessKillStep",
    "ProcessReadIncrementalStep",
    "ProcessScenarioExecutionResult",
    "ProcessScenarioPlan",
    "ProcessSendInputStep",
    "ProcessStartStep",
    "ProcessStatusCheckpoint",
    "ProcessStep",
    "ProcessStepBase",
    "ProcessWaitStep",
    "ScenarioArtifactObservation",
    "ScenarioCheckpoint",
    "ScenarioCheckpointBase",
    "ScenarioCheckpointKind",
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
    "StepStatusCheckpoint",
    "ToolchainScenarioExecutionResult",
    "ToolchainScenarioPlan",
)
