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
    Number,
    PositiveInt,
    SafeRelativePath,
    Sha256Digest,
    UtcDatetime,
)


ScenarioTimeout = Annotated[PositiveInt, Field(le=3600)]
ScenarioStepTimeout = Annotated[PositiveInt, Field(le=600)]
NonNegativeSeconds = Annotated[Number, Field(ge=0)]


class E2EScenarioKind(str, Enum):
    TOOLCHAIN = "toolchain"
    PROCESS_BACKGROUND = "process_background"


class ScenarioStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_EVALUABLE = "not_evaluable"
    ERROR = "error"


class ProcessTimingStatus(str, Enum):
    """Whether a public Process observation supplied reliable timing facts."""

    AVAILABLE = "available"
    AVAILABLE_DURATION_ONLY = "available_duration_only"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class ProcessObservationSpanStatus(str, Enum):
    """Availability of the persistence-timestamp observation interval."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class ProcessTimingSource(str, Enum):
    """Public source used for a timing fact; sources are never inferred."""

    PUBLIC_OBSERVATION_PERSISTENCE = "public_observation_persistence"
    PUBLIC_DURATION_ONLY = "public_duration_only"
    UNAVAILABLE = "unavailable"


class ProcessHookTimingSource(str, Enum):
    """Source for a serialized public PRE or POST hook boundary."""

    WORKER_PRE_TOOL_CONTROL_HOOK = "worker_pre_tool_control_hook"
    WORKER_POST_TOOL_PERSISTENCE_HOOK = "worker_post_tool_persistence_hook"
    UNAVAILABLE = "unavailable"


class ProcessWaitTimingSource(str, Enum):
    """Source for the PRE-to-PRE WAIT budget interval."""

    WORKER_PRE_TOOL_CONTROL_HOOKS = "worker_pre_tool_control_hooks"
    UNAVAILABLE = "unavailable"


class ProcessHookSpanStatus(str, Enum):
    """Availability of the conservative PRE-to-POST hook span."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class ProcessHardTimeoutSource(str, Enum):
    """Watchdog that supplied the conservative scenario deadline."""

    TRIAL_WATCHDOG = "trial_watchdog"
    WORKER_PROCESS_SCENARIO_WATCHDOG = "worker_process_scenario_watchdog"
    UNAVAILABLE = "unavailable"


class WaitRemainingBudgetStatus(str, Enum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNAVAILABLE = "unavailable"
    FALLBACK_USED = "fallback_used"
    NOT_APPLICABLE = "not_applicable"


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


class _MarkerCheckpoint(ScenarioCheckpointBase):
    kind: Literal[ScenarioCheckpointKind.OUTPUT] = ScenarioCheckpointKind.OUTPUT
    required_markers: list[NonEmptyText] = Field(default_factory=list)
    forbidden_markers: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_markers(self) -> "_MarkerCheckpoint":
        if set(self.required_markers) & set(self.forbidden_markers):
            raise ValueError("checkpoint required and forbidden markers must be disjoint")
        return self


class ProcessOutputCheckpoint(_MarkerCheckpoint):
    """A checkpoint over one Process read step's incremental output."""

    target_step_id: Identifier
    minimum_new_output_length: NonNegativeInt = 0


class ArtifactOutputCheckpoint(_MarkerCheckpoint):
    """A checkpoint over one explicitly declared Toolchain Artifact."""

    target_artifact_id: FixtureTargetPath
    minimum_content_char_length: NonNegativeInt = 0


# Keep the public name used by existing Process integrations while making the
# two output domains explicit in the ScenarioCheckpoint union.
OutputCheckpoint = ProcessOutputCheckpoint


class CleanupCheckpoint(ScenarioCheckpointBase):
    kind: Literal[ScenarioCheckpointKind.CLEANUP] = ScenarioCheckpointKind.CLEANUP
    expect_agent_close: StrictBool = False
    expect_worker_cleanup: StrictBool = True
    expect_no_live_processes: StrictBool = True


# Both output checkpoint classes intentionally retain ``kind: output``. Strict
# ``extra=forbid`` makes the union unambiguous without guessing from IDs.
ScenarioCheckpoint = (
    StepStatusCheckpoint
    | ProcessStatusCheckpoint
    | ProcessOutputCheckpoint
    | ArtifactOutputCheckpoint
    | CleanupCheckpoint
)


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
    trace_requirements: list[ScenarioTraceRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_checkpoints(self) -> "ScenarioPlanBase":
        checkpoint_ids = [item.checkpoint_id for item in self.checkpoints]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("scenario checkpoint IDs must be unique")
        if len(self.required_toolsets) != len(set(self.required_toolsets)):
            raise ValueError("scenario required_toolsets must be unique")
        trace_names = [item.tool_name for item in self.trace_requirements]
        if len(trace_names) != len(set(trace_names)):
            raise ValueError("scenario trace requirement tool names must be unique")
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
        for checkpoint in self.checkpoints:
            if not isinstance(checkpoint, ArtifactOutputCheckpoint):
                raise ValueError(
                    "Toolchain scenarios require ArtifactOutputCheckpoint checkpoints"
                )
            if checkpoint.target_artifact_id not in set(self.output_artifacts):
                raise ValueError(
                    "Toolchain output checkpoint target_artifact_id must reference "
                    "a declared output Artifact"
                )
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
    cursor_before: NonNegativeInt | None = None
    cursor_source_step_id: Identifier | None = None
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
    allow_hard_watchdog_fallback: StrictBool = False


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
            if isinstance(step, ProcessWaitStep) and step.timeout_seconds > step.maximum_wait_seconds:
                raise ValueError("wait step timeout cannot exceed maximum_wait_seconds")
            if index < start_index and step.action is not ProcessAction.START:
                raise ValueError("Process start must precede every Process operation")
            if step.process_ref_step_id is not None and step.process_ref_step_id != self.steps[start_index].step_id:
                raise ValueError("Process steps may reference only the start step")
            if index > start_index and step.process_ref_step_id == self.steps[start_index].step_id:
                continue
        terminal_seen = False
        read_step_ids: list[str] = []
        step_indices = {step.step_id: index for index, step in enumerate(self.steps)}
        for index, step in enumerate(self.steps[start_index + 1 :], start=start_index + 1):
            if step.action is ProcessAction.CLOSE and terminal_seen:
                raise ValueError(
                    "Process close must target a running Process before terminal state"
                )
            if step.action in {ProcessAction.KILL, ProcessAction.INTERRUPT, ProcessAction.CLOSE}:
                terminal_seen = True
            elif (
                isinstance(step, ProcessWaitStep)
                and step.expected_status in _TERMINAL_PROCESS_STATUSES
            ) or (
                isinstance(step, ProcessAssertStatusStep)
                and step.expected_status in _TERMINAL_PROCESS_STATUSES
            ):
                terminal_seen = True
            elif step.action is ProcessAction.SEND_INPUT and terminal_seen:
                raise ValueError("send_input cannot follow a terminal Process action")
            if isinstance(step, ProcessReadIncrementalStep):
                if (step.cursor_before is None) == (step.cursor_source_step_id is None):
                    raise ValueError(
                        "Process read must declare exactly one of cursor_before or "
                        "cursor_source_step_id"
                    )
                if step.cursor_source_step_id is not None:
                    reference_index = step_indices.get(step.cursor_source_step_id)
                    if reference_index is None or reference_index >= index:
                        raise ValueError(
                            "Process cursor_source_step_id must reference a prior read step"
                        )
                    referenced = self.steps[reference_index]
                    if not isinstance(referenced, ProcessReadIncrementalStep):
                        raise ValueError(
                            "Process cursor_source_step_id must reference a read_incremental step"
                        )
                    if not read_step_ids or read_step_ids[-1] != step.cursor_source_step_id:
                        raise ValueError(
                            "Process cursor_source_step_id must reference the previous read step"
                        )
                elif step.cursor_before != 0 or read_step_ids:
                    raise ValueError(
                        "only the first Process read may declare initial cursor_before=0"
                    )
                read_step_ids.append(step.step_id)
        known_steps = set(step_ids)
        has_close_step = any(item.action is ProcessAction.CLOSE for item in self.steps)
        for checkpoint in self.checkpoints:
            target = getattr(checkpoint, "target_step_id", None)
            if target is not None and target not in known_steps:
                raise ValueError("checkpoint target_step_id must reference a declared step")
            if isinstance(checkpoint, ArtifactOutputCheckpoint):
                raise ValueError("Process scenarios require ProcessOutputCheckpoint")
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


ProcessEventReason = Literal[
    "unexpected_event",
    "missing_expected_event",
    "event_order_violation",
    "foreign_process_event",
    "unconsumed_event",
]


class ProcessEventDiagnostic(ContractModel):
    """Content-free alignment fact for one Process observation position."""

    event_index: NonNegativeInt
    tool_name: Identifier | None = None
    public_action: Identifier | None = None
    process_id_safe: Identifier | None = None
    tool_call_id_safe: Identifier | None = None
    observation_status: Identifier | None = None
    step_id: Identifier | None = None
    reason: ProcessEventReason


class ScenarioCheckpointResult(ContractModel):
    checkpoint_id: Identifier
    kind: ScenarioCheckpointKind
    required: StrictBool
    target_step_id: Identifier | None = None
    target_artifact_id: FixtureTargetPath | None = None
    passed: StrictBool | None = None
    observed_step_status: ScenarioStatus | None = None
    observed_process_status: ScenarioProcessStatus | None = None
    agent_close_observed: StrictBool | None = None
    worker_cleanup_completed: StrictBool | None = None
    artifact_exists: StrictBool | None = None
    content_sha256: Sha256Digest | None = None
    content_char_length: NonNegativeInt | None = None
    content_utf8_bytes: NonNegativeInt | None = None
    required_markers_found: list[Identifier] = Field(default_factory=list)
    missing_required_markers: list[Identifier] = Field(default_factory=list)
    forbidden_markers_found: list[Identifier] = Field(default_factory=list)
    required_marker_count: NonNegativeInt = 0
    missing_required_marker_count: NonNegativeInt = 0
    forbidden_marker_count: NonNegativeInt = 0
    truncated: StrictBool | None = None
    error: ScenarioError | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "ScenarioCheckpointResult":
        if self.kind is ScenarioCheckpointKind.OUTPUT:
            if (self.target_step_id is None) == (self.target_artifact_id is None):
                raise ValueError(
                    "output checkpoint result must identify exactly one step or Artifact"
                )
        elif self.target_artifact_id is not None:
            raise ValueError("non-output checkpoint result cannot identify an Artifact")
        if self.required_marker_count != len(self.required_markers_found):
            raise ValueError("required_marker_count must match marker facts")
        if self.missing_required_marker_count != len(self.missing_required_markers):
            raise ValueError("missing_required_marker_count must match marker facts")
        if self.forbidden_marker_count != len(self.forbidden_markers_found):
            raise ValueError("forbidden_marker_count must match marker facts")
        return self


class ScenarioArtifactObservation(ContractModel):
    relative_path: FixtureTargetPath
    exists: StrictBool
    sha256: Sha256Digest | None = None
    size_bytes: NonNegativeInt = 0
    content_char_length: NonNegativeInt | None = None
    content_utf8_bytes: NonNegativeInt | None = None
    truncated: StrictBool = False

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
    timing_status: ProcessTimingStatus = ProcessTimingStatus.UNAVAILABLE
    timing_source: ProcessTimingSource = ProcessTimingSource.UNAVAILABLE
    timed_out: StrictBool | None = None
    event_pre_hook_offset_ms: NonNegativeInt | None = None
    event_post_hook_offset_ms: NonNegativeInt | None = None
    event_pre_hook_source: ProcessHookTimingSource = (
        ProcessHookTimingSource.UNAVAILABLE
    )
    event_post_hook_source: ProcessHookTimingSource = (
        ProcessHookTimingSource.UNAVAILABLE
    )
    # WAIT-only budget facts.  ``None`` is intentional when the Worker has no
    # reliable PRE-to-PRE control-hook boundaries; duration sums are not
    # substituted.
    elapsed_before_wait_ms: NonNegativeInt | None = None
    scenario_remaining_before_wait_seconds: NonNegativeSeconds | None = None
    wait_remaining_budget_status: WaitRemainingBudgetStatus | None = None
    wait_timeout_budget_matched: StrictBool | None = None
    wait_budget_timing_source: ProcessWaitTimingSource | None = None
    hard_watchdog_fallback_allowed: StrictBool | None = None
    hard_watchdog_fallback_used: StrictBool | None = None
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
        if self.timing_status is ProcessTimingStatus.AVAILABLE:
            if (
                self.started_at is None
                or self.completed_at is None
                or self.duration_ms is None
            ):
                raise ValueError("available step timing requires timestamps and duration")
            if self.timing_source is ProcessTimingSource.UNAVAILABLE:
                raise ValueError("available step timing requires a timing source")
        elif self.timing_status is ProcessTimingStatus.AVAILABLE_DURATION_ONLY:
            if self.duration_ms is None:
                raise ValueError("duration-only timing requires duration")
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("duration-only timing cannot claim timestamps")
            if self.timing_source is ProcessTimingSource.UNAVAILABLE:
                raise ValueError("duration-only timing requires a timing source")
        else:
            if self.duration_ms is not None:
                raise ValueError("unavailable or invalid timing cannot claim duration")
            if self.timed_out is not None:
                raise ValueError("unavailable or invalid timing cannot claim timeout")
            if self.timing_source is not ProcessTimingSource.UNAVAILABLE:
                raise ValueError("unavailable timing cannot claim a timing source")
        if self.event_pre_hook_offset_ms is None:
            if self.event_pre_hook_source is not ProcessHookTimingSource.UNAVAILABLE:
                raise ValueError("missing PRE hook offset cannot claim a source")
        elif self.event_pre_hook_source is not ProcessHookTimingSource.WORKER_PRE_TOOL_CONTROL_HOOK:
            raise ValueError("PRE hook offset requires the public PRE hook source")
        if self.event_post_hook_offset_ms is None:
            if self.event_post_hook_source is not ProcessHookTimingSource.UNAVAILABLE:
                raise ValueError("missing POST hook offset cannot claim a source")
        elif self.event_post_hook_source is not ProcessHookTimingSource.WORKER_POST_TOOL_PERSISTENCE_HOOK:
            raise ValueError("POST hook offset requires the public POST hook source")
        if (
            self.event_pre_hook_offset_ms is not None
            and self.event_post_hook_offset_ms is not None
            and self.event_post_hook_offset_ms < self.event_pre_hook_offset_ms
        ):
            raise ValueError("POST hook offset cannot precede PRE hook offset")
        if self.timing_status in {
            ProcessTimingStatus.AVAILABLE,
            ProcessTimingStatus.AVAILABLE_DURATION_ONLY,
        }:
            expected_timeout = self.duration_ms > self.timeout_seconds * 1000
            if self.timed_out != expected_timeout:
                raise ValueError("step timed_out must match duration and timeout")
        wait_fields = (
            self.elapsed_before_wait_ms,
            self.scenario_remaining_before_wait_seconds,
            self.wait_remaining_budget_status,
            self.wait_timeout_budget_matched,
            self.wait_budget_timing_source,
            self.hard_watchdog_fallback_allowed,
            self.hard_watchdog_fallback_used,
        )
        if self.action is not ProcessAction.WAIT:
            if any(value is not None for value in wait_fields):
                raise ValueError("wait budget facts are only valid for WAIT steps")
        elif self.wait_remaining_budget_status is None:
            raise ValueError("WAIT steps require an explicit remaining-budget status")
        elif self.hard_watchdog_fallback_allowed is None or self.hard_watchdog_fallback_used is None:
            raise ValueError("WAIT steps require explicit watchdog fallback facts")
        elif self.wait_remaining_budget_status in {
            WaitRemainingBudgetStatus.MATCHED,
            WaitRemainingBudgetStatus.MISMATCHED,
        }:
            if (
                self.elapsed_before_wait_ms is None
                or self.scenario_remaining_before_wait_seconds is None
                or self.wait_timeout_budget_matched is None
                or self.wait_budget_timing_source
                is not ProcessWaitTimingSource.WORKER_PRE_TOOL_CONTROL_HOOKS
                or self.hard_watchdog_fallback_used
                or (
                    self.wait_remaining_budget_status
                    is WaitRemainingBudgetStatus.MATCHED
                    and self.wait_timeout_budget_matched is not True
                )
                or (
                    self.wait_remaining_budget_status
                    is WaitRemainingBudgetStatus.MISMATCHED
                    and self.wait_timeout_budget_matched is not False
                )
            ):
                raise ValueError("exact WAIT budget requires PRE-to-PRE facts")
        elif self.wait_remaining_budget_status is WaitRemainingBudgetStatus.FALLBACK_USED:
            if (
                self.elapsed_before_wait_ms is not None
                or self.scenario_remaining_before_wait_seconds is not None
                or self.wait_timeout_budget_matched is not None
                or self.wait_budget_timing_source
                is not ProcessWaitTimingSource.UNAVAILABLE
                or self.hard_watchdog_fallback_allowed is not True
                or self.hard_watchdog_fallback_used is not True
            ):
                raise ValueError("fallback WAIT budget cannot claim exact timing facts")
        elif self.wait_remaining_budget_status is WaitRemainingBudgetStatus.NOT_APPLICABLE:
            raise ValueError("WAIT steps cannot use a not-applicable budget status")
        else:
            if (
                self.elapsed_before_wait_ms is not None
                or self.scenario_remaining_before_wait_seconds is not None
                or self.wait_timeout_budget_matched is not None
                or self.wait_budget_timing_source
                is not ProcessWaitTimingSource.UNAVAILABLE
                or self.hard_watchdog_fallback_used
            ):
                raise ValueError("unavailable WAIT budget cannot claim timing facts")
        return self


class IncrementalReadObservation(ContractModel):
    step_id: Identifier
    read_index: NonNegativeInt
    cursor_unit: Literal["character"] = "character"
    cursor_before: NonNegativeInt
    cursor_after: NonNegativeInt
    cursor_source_step_id: Identifier | None = None
    cursor_reference_matched: StrictBool | None = None
    cursor_chain_matched: StrictBool | None = None
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
    file_fixture_read_observed: StrictBool | None = None
    file_fixture_read_sha256: Sha256Digest | None = None
    file_fixture_read_char_length: NonNegativeInt | None = None
    file_fixture_read_utf8_bytes: NonNegativeInt | None = None
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
    tool_calls: list[ScenarioToolCallObservation] = Field(default_factory=list)
    input_matched: StrictBool | None = None
    file_fixture_read_observed: StrictBool = False
    status_transitions_valid: StrictBool | None = None
    scenario_timeout_seconds: PositiveInt
    # Scenario timing is deliberately split.  Persistence timestamps describe
    # an observation interval; they are not per-handler start/end boundaries.
    scenario_observation_span_status: ProcessObservationSpanStatus = (
        ProcessObservationSpanStatus.UNAVAILABLE
    )
    scenario_observation_timing_source: ProcessTimingSource = (
        ProcessTimingSource.UNAVAILABLE
    )
    scenario_observation_started_at: UtcDatetime | None = None
    scenario_observation_completed_at: UtcDatetime | None = None
    scenario_observation_span_ms: NonNegativeInt | None = None
    scenario_hook_span_status: ProcessHookSpanStatus = ProcessHookSpanStatus.UNAVAILABLE
    scenario_pre_to_post_hook_span_ms: NonNegativeInt | None = None
    hard_timeout_source: ProcessHardTimeoutSource = ProcessHardTimeoutSource.UNAVAILABLE
    hard_timeout_seconds: PositiveInt | None = None
    hard_timeout_triggered: StrictBool = False
    trial_watchdog_timed_out: StrictBool = False
    scenario_watchdog_timed_out: StrictBool = False
    scenario_observation_span_exceeded: StrictBool | None = None
    wait_remaining_budget_status: WaitRemainingBudgetStatus = (
        WaitRemainingBudgetStatus.NOT_APPLICABLE
    )
    process_start_pre_hook_available: StrictBool = False
    wait_pre_hook_available: StrictBool | None = None
    elapsed_before_wait_ms: NonNegativeInt | None = None
    scenario_remaining_before_wait_seconds: NonNegativeSeconds | None = None
    wait_timeout_budget_matched: StrictBool | None = None
    wait_budget_timing_source: ProcessWaitTimingSource | None = None
    hard_watchdog_fallback_allowed: StrictBool = False
    hard_watchdog_fallback_used: StrictBool = False
    agent_close_required: StrictBool = False
    agent_close_observed: StrictBool = False
    worker_cleanup_result: ProcessCleanupResult | None = None
    unexpected_events: list[ProcessEventDiagnostic] = Field(default_factory=list)
    missing_expected_events: list[ProcessEventDiagnostic] = Field(default_factory=list)
    event_order_violations: list[ProcessEventDiagnostic] = Field(default_factory=list)
    foreign_process_events: list[ProcessEventDiagnostic] = Field(default_factory=list)
    unconsumed_events: list[ProcessEventDiagnostic] = Field(default_factory=list)
    tool_duration_sum_ms: NonNegativeInt | None = None
    # Deprecated compatibility alias.  It is only the persistence observation
    # span; it is never a handler-completion or hard-timeout measurement.
    duration_ms: NonNegativeInt | None = Field(default=None, deprecated=True)
    errors: list[ScenarioError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_transition(self) -> "ProcessScenarioExecutionResult":
        if self.initial_status in _TERMINAL_PROCESS_STATUSES and self.final_status in _ACTIVE_PROCESS_STATUSES:
            raise ValueError("terminal Process cannot transition back to active")
        if (self.scenario_observation_started_at is None) != (
            self.scenario_observation_completed_at is None
        ):
            raise ValueError("scenario observation timestamps must be paired")
        if (
            self.scenario_observation_started_at is not None
            and self.scenario_observation_completed_at is not None
            and self.scenario_observation_completed_at
            < self.scenario_observation_started_at
        ):
            raise ValueError("scenario observation completed_at cannot precede started_at")
        if self.scenario_observation_span_status is ProcessObservationSpanStatus.AVAILABLE:
            if (
                self.scenario_observation_started_at is None
                or self.scenario_observation_completed_at is None
                or self.scenario_observation_span_ms is None
                or self.scenario_observation_timing_source
                is not ProcessTimingSource.PUBLIC_OBSERVATION_PERSISTENCE
            ):
                raise ValueError("available observation span requires persistence facts")
            computed_ms = round(
                (
                    self.scenario_observation_completed_at
                    - self.scenario_observation_started_at
                ).total_seconds()
                * 1000
            )
            if computed_ms != self.scenario_observation_span_ms:
                raise ValueError("observation span must match persistence timestamps")
        else:
            if any(
                value is not None
                for value in (
                    self.scenario_observation_started_at,
                    self.scenario_observation_completed_at,
                    self.scenario_observation_span_ms,
                )
            ):
                raise ValueError("unavailable observation span cannot claim timestamps")
            if self.scenario_observation_timing_source is not ProcessTimingSource.UNAVAILABLE:
                raise ValueError("unavailable observation span cannot claim a source")
        if self.duration_ms is not None and self.duration_ms != self.scenario_observation_span_ms:
            raise ValueError("legacy Process duration must match observation span")
        if self.scenario_hook_span_status is ProcessHookSpanStatus.AVAILABLE:
            if self.scenario_pre_to_post_hook_span_ms is None:
                raise ValueError("available hook span requires a duration")
        elif self.scenario_pre_to_post_hook_span_ms is not None:
            raise ValueError("unavailable hook span cannot claim a duration")
        if self.hard_timeout_source is ProcessHardTimeoutSource.UNAVAILABLE:
            if self.hard_timeout_seconds is not None:
                raise ValueError("unavailable hard timeout cannot claim a budget")
        elif self.hard_timeout_seconds is None:
            raise ValueError("hard timeout source requires a budget")
        if self.scenario_observation_span_exceeded is not None:
            if (
                self.scenario_observation_span_ms is None
                or self.hard_timeout_seconds is None
                or self.scenario_observation_span_exceeded
                != self.scenario_observation_span_ms > self.hard_timeout_seconds * 1000
            ):
                raise ValueError("observation span exceeded must match the hard budget")
        wait_fields = (
            self.elapsed_before_wait_ms,
            self.scenario_remaining_before_wait_seconds,
            self.wait_timeout_budget_matched,
            self.wait_budget_timing_source,
        )
        if self.wait_remaining_budget_status is WaitRemainingBudgetStatus.NOT_APPLICABLE:
            if self.wait_pre_hook_available is not None:
                raise ValueError("not-applicable WAIT budget cannot claim a WAIT PRE fact")
        elif self.wait_pre_hook_available is None:
            raise ValueError("Process results require an explicit WAIT PRE fact")
        if self.wait_remaining_budget_status in {
            WaitRemainingBudgetStatus.MATCHED,
            WaitRemainingBudgetStatus.MISMATCHED,
        } and (
            not self.process_start_pre_hook_available
            or self.wait_pre_hook_available is not True
        ):
            raise ValueError("exact WAIT budget requires both PRE facts")
        if self.wait_remaining_budget_status in {
            WaitRemainingBudgetStatus.MATCHED,
            WaitRemainingBudgetStatus.MISMATCHED,
        }:
            if (
                self.elapsed_before_wait_ms is None
                or self.scenario_remaining_before_wait_seconds is None
                or self.wait_timeout_budget_matched is None
                or self.wait_budget_timing_source
                is not ProcessWaitTimingSource.WORKER_PRE_TOOL_CONTROL_HOOKS
                or self.hard_watchdog_fallback_used
                or (
                    self.wait_remaining_budget_status
                    is WaitRemainingBudgetStatus.MATCHED
                    and self.wait_timeout_budget_matched is not True
                )
                or (
                    self.wait_remaining_budget_status
                    is WaitRemainingBudgetStatus.MISMATCHED
                    and self.wait_timeout_budget_matched is not False
                )
            ):
                raise ValueError("exact aggregate WAIT budget requires PRE-to-PRE facts")
        elif self.wait_remaining_budget_status is WaitRemainingBudgetStatus.FALLBACK_USED:
            if (
                any(value is not None for value in wait_fields[:-1])
                or self.wait_budget_timing_source
                is not ProcessWaitTimingSource.UNAVAILABLE
                or self.wait_timeout_budget_matched is not None
                or self.hard_watchdog_fallback_allowed is not True
                or self.hard_watchdog_fallback_used is not True
            ):
                raise ValueError("fallback aggregate WAIT budget cannot claim exact facts")
        elif any(value is not None for value in wait_fields):
            if (
                self.wait_remaining_budget_status
                is not WaitRemainingBudgetStatus.UNAVAILABLE
                or self.wait_budget_timing_source
                is not ProcessWaitTimingSource.UNAVAILABLE
                or self.wait_timeout_budget_matched is not None
                or self.hard_watchdog_fallback_used
            ):
                raise ValueError("unavailable WAIT budget cannot claim timing facts")
        elif self.wait_remaining_budget_status is WaitRemainingBudgetStatus.NOT_APPLICABLE:
            if self.hard_watchdog_fallback_allowed or self.hard_watchdog_fallback_used:
                raise ValueError("not-applicable WAIT budget cannot claim fallback facts")
        return self


ScenarioExecutionResult = Annotated[
    ToolchainScenarioExecutionResult | ProcessScenarioExecutionResult,
    Field(discriminator="kind"),
]


__all__ = (
    "CleanupCheckpoint",
    "ArtifactOutputCheckpoint",
    "E2EScenarioKind",
    "IncrementalReadObservation",
    "OutputCheckpoint",
    "ProcessOutputCheckpoint",
    "ProcessEventDiagnostic",
    "ProcessEventReason",
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
    "ProcessHardTimeoutSource",
    "ProcessHookSpanStatus",
    "ProcessHookTimingSource",
    "ProcessObservationSpanStatus",
    "ProcessSendInputStep",
    "ProcessStartStep",
    "ProcessStatusCheckpoint",
    "ProcessStep",
    "ProcessStepBase",
    "ProcessTimingSource",
    "ProcessTimingStatus",
    "ProcessWaitTimingSource",
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
    "WaitRemainingBudgetStatus",
)
