"""Strict versioned file protocol shared by the parent and MyHermes worker."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, model_validator

from myhermes_audit.contracts import (
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
    BackgroundReviewPlan,
    CompressionEvent,
    ContextDiagnostic,
    EffectiveSubjectConfiguration,
    FactContextObservation,
    LongConversationCheckpoint,
    MemoryFixture,
    MemoryOperationError,
    MemoryQuery,
    MemoryQueryPhase,
    MemoryQueryResult,
    MemorySnapshotPhase,
    MemoryStateChange,
    MemoryStateSnapshot,
    RetrievalStrategy,
    RequiredFactExpectation,
    ScenarioError,
    ScenarioExecutionResult,
    ScenarioPlan,
    ToolsetName,
    TurnResult,
)
from myhermes_audit.contracts.suite import SkillFixture
from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    SafeRelativePath,
    UtcDatetime,
)


WORKER_PROTOCOL_VERSION = "myhermes-audit-worker-v10"
LEGACY_WORKER_PROTOCOL_VERSION = "myhermes-audit-worker-v9"
WorkerProtocolVersion = Literal["myhermes-audit-worker-v10"]


class WorkerMode(str, Enum):
    SINGLE_TURN = "single_turn"
    SCRIPTED_MULTI_TURN = "scripted_multi_turn"


class WorkerStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerTurn(ContractModel):
    message: NonEmptyText
    session_id: Identifier | None = None


class MemoryQueryPlan(ContractModel):
    query_id: Identifier
    phase: MemoryQueryPhase = MemoryQueryPhase.BEFORE_CONVERSATION
    query: MemoryQuery


class WorkerArtifactPaths(ContractModel):
    worker_request: Path
    worker_result: Path
    transcript: Path
    observations: Path
    validator_results: Path
    stdout_log: Path
    stderr_log: Path
    memory: Path | None = None
    ablation: Path | None = None
    background_review_results: Path | None = None
    background_review_evidence: Path | None = None
    background_review_snapshots: Path | None = None
    toolchain_results: Path | None = None
    process_scenario_results: Path | None = None
    process_cleanup: Path | None = None
    process_output_logs: list[Path] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_artifact_paths(self) -> "WorkerArtifactPaths":
        paths: list[Path] = []
        for name in type(self).model_fields:
            if name == "schema_version":
                continue
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, list):
                paths.extend(value)
            else:
                paths.append(value)
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
    turns: list[WorkerTurn] = Field(min_length=1)
    workspace: Path
    hermes_home: Path
    sqlite_path: Path
    enabled_toolsets: list[ToolsetName] = Field(default_factory=list)
    memory_strategy: RetrievalStrategy | None = None
    memory_fixture: MemoryFixture | None = None
    memory_queries: list[MemoryQueryPlan] = Field(default_factory=list)
    variant_id: Identifier | None = None
    effective_subject_configuration: EffectiveSubjectConfiguration | None = None
    required_fact_expectations: list[RequiredFactExpectation] = Field(
        default_factory=list
    )
    checkpoints: list[LongConversationCheckpoint] = Field(default_factory=list)
    background_review_plans: list[BackgroundReviewPlan] = Field(
        default_factory=list
    )
    skill_fixtures: list[SkillFixture] = Field(default_factory=list)
    scenarios: list[ScenarioPlan] = Field(default_factory=list)
    timeout_seconds: PositiveInt
    artifact_paths: WorkerArtifactPaths

    @model_validator(mode="after")
    def validate_request(self) -> "MyHermesWorkerRequest":
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("worker request protocol version is incompatible")
        runtime_paths = (self.workspace, self.hermes_home, self.sqlite_path)
        if any(not path.is_absolute() for path in runtime_paths):
            raise ValueError("worker runtime paths must be absolute")
        if self.workspace.resolve(strict=False) == self.hermes_home.resolve(strict=False):
            raise ValueError("workspace and hermes_home must be distinct")
        if len(self.enabled_toolsets) != len(set(self.enabled_toolsets)):
            raise ValueError("enabled_toolsets must not repeat")
        query_ids = [item.query_id for item in self.memory_queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("worker Memory query IDs must not repeat")
        memory_requested = any(
            (
                self.memory_fixture is not None,
                bool(self.memory_queries),
                ToolsetName.MEMORY in self.enabled_toolsets,
                self.memory_strategy is not None,
            )
        )
        if memory_requested and self.memory_strategy is None:
            raise ValueError("worker Memory requests require memory_strategy")
        if memory_requested and self.artifact_paths.memory is None:
            raise ValueError("worker Memory requests require a Memory Artifact path")
        if not memory_requested and self.artifact_paths.memory is not None:
            raise ValueError("non-Memory worker requests cannot name a Memory Artifact")
        if (
            self.memory_strategy is RetrievalStrategy.DISABLED
            and ToolsetName.MEMORY in self.enabled_toolsets
        ):
            raise ValueError("disabled strategy cannot enable the memory toolset")
        p4_enabled = self.effective_subject_configuration is not None
        if p4_enabled and self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("P4 requests require the current Worker protocol")
        if p4_enabled != (self.variant_id is not None):
            raise ValueError("P4 request requires Variant and effective configuration")
        if p4_enabled != (self.artifact_paths.ablation is not None):
            raise ValueError("P4 request requires an Ablation Artifact path")
        if not p4_enabled and (
            self.required_fact_expectations or self.checkpoints
        ):
            raise ValueError("P4 expectations require an effective configuration")
        if p4_enabled:
            configuration = self.effective_subject_configuration
            if configuration is None:
                raise ValueError("P4 effective configuration is missing")
            if len(self.turns) > configuration.maximum_turns:
                raise ValueError("worker turns exceed the P4 maximum_turns limit")
            if any(item.after_turn > len(self.turns) for item in self.checkpoints):
                raise ValueError("worker checkpoint exceeds the requested turns")
            long_term = configuration.include_memory
            if long_term != (self.memory_strategy is not None):
                raise ValueError("worker Memory request must match P4 Memory mode")
            if self.memory_strategy is not configuration.memory_strategy:
                raise ValueError("worker Memory strategy must match P4 configuration")
            if configuration.memory_tool_enabled != (
                ToolsetName.MEMORY in self.enabled_toolsets
            ):
                raise ValueError("worker memory toolset must match P4 configuration")
            if not long_term and (
                self.memory_fixture is not None or self.memory_queries
            ):
                raise ValueError("non-long-term P4 modes cannot expose Memory facts")
        p5_enabled = bool(self.background_review_plans)
        p5_paths = (
            self.artifact_paths.background_review_results,
            self.artifact_paths.background_review_evidence,
            self.artifact_paths.background_review_snapshots,
        )
        if p5_enabled != all(path is not None for path in p5_paths):
            raise ValueError("P5 Review requests require all Review Artifact paths")
        if not p5_enabled and any(path is not None for path in p5_paths):
            raise ValueError("non-P5 requests cannot name Review Artifact paths")
        if self.skill_fixtures and not p5_enabled:
            raise ValueError("Skill fixtures require a P5 Background Review plan")
        plan_ids = [plan.review_id for plan in self.background_review_plans]
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("P5 Review plan IDs must not repeat")
        scenario_ids = [item.scenario_id for item in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("P6 scenario IDs must not repeat")
        process_scenario_ids = [
            item.scenario_id
            for item in self.scenarios
            if item.kind.value == "process_background"
        ]
        if len(process_scenario_ids) > 1:
            raise ValueError(
                "worker request allows at most one process_background Scenario: "
                + ", ".join(process_scenario_ids)
            )
        scenario_kinds = {item.kind.value for item in self.scenarios}
        if ("toolchain" in scenario_kinds) != (
            self.artifact_paths.toolchain_results is not None
        ):
            raise ValueError("Toolchain scenarios require their Artifact path")
        if ("process_background" in scenario_kinds) != (
            self.artifact_paths.process_scenario_results is not None
        ):
            raise ValueError(
                "Process scenarios require their Scenario Artifact path"
            )
        has_process_scenarios = "process_background" in scenario_kinds
        if has_process_scenarios != (
            self.artifact_paths.process_cleanup is not None
        ):
            raise ValueError("Process scenarios require the cleanup Artifact path")
        process_count = sum(
            item.kind.value == "process_background" for item in self.scenarios
        )
        if process_count != len(self.artifact_paths.process_output_logs):
            raise ValueError(
                "each Process scenario requires one output Artifact path"
            )
        if not self.scenarios and self.artifact_paths.toolchain_results is not None:
            raise ValueError("non-P6 requests cannot name Toolchain Artifacts")
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
    compression_applied: StrictBool | None = None
    input_message_count: NonNegativeInt | None = None
    output_message_count: NonNegativeInt | None = None

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
    # MyHermes persists this public Observation timestamp as persistence
    # metadata.  It is suitable only for an observation-span projection; it
    # does not establish a per-handler start or completion boundary.
    created_at: UtcDatetime | None = None


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


class MemoryArtifact(ContractModel):
    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    strategy: RetrievalStrategy
    provider: NonEmptyText
    seeded_memory_ids: list[Identifier] = Field(default_factory=list)
    query_results: list[MemoryQueryResult] = Field(default_factory=list)
    snapshots: list[MemoryStateSnapshot] = Field(default_factory=list)
    state_changes: list[MemoryStateChange] = Field(default_factory=list)
    errors: list[MemoryOperationError] = Field(default_factory=list)
    clear_attempted: StrictBool = False
    clear_succeeded: StrictBool | None = None

    @model_validator(mode="after")
    def validate_memory_artifact(self) -> "MemoryArtifact":
        if len(self.seeded_memory_ids) != len(set(self.seeded_memory_ids)):
            raise ValueError("seeded_memory_ids must not repeat")
        query_ids = [item.query_id for item in self.query_results]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Memory Artifact query IDs must not repeat")
        snapshot_ids = [item.snapshot_id for item in self.snapshots]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("Memory Artifact snapshot IDs must not repeat")
        snapshot_phases = [item.phase for item in self.snapshots]
        if len(snapshot_phases) != len(set(snapshot_phases)):
            raise ValueError("Memory Artifact snapshot phases must not repeat")
        if any(
            item.phase is None
            or item.strategy is not self.strategy
            or item.provider is None
            for item in self.snapshots
        ):
            raise ValueError(
                "Memory Artifact snapshots require matching P3 semantics"
            )
        if any(
            item.strategy is not self.strategy or item.provider != self.provider
            for item in self.query_results
        ):
            raise ValueError(
                "Memory Artifact queries require matching P3 semantics"
            )
        if len({item.provider for item in self.snapshots}) > 1:
            raise ValueError("Memory Artifact snapshots must share a provider")
        if self.state_changes and set(snapshot_phases) != {
            MemorySnapshotPhase.BEFORE_CONVERSATION,
            MemorySnapshotPhase.AFTER_CONVERSATION,
        }:
            raise ValueError(
                "Memory Artifact state changes require before/after snapshots"
            )
        changed_ids = [item.memory_id for item in self.state_changes]
        if len(changed_ids) != len(set(changed_ids)):
            raise ValueError("Memory Artifact state change IDs must not repeat")
        if not self.clear_attempted and self.clear_succeeded is not None:
            raise ValueError("clear_succeeded requires clear_attempted")
        return self


class AblationArtifact(ContractModel):
    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    variant_id: Identifier
    effective_subject_configuration: EffectiveSubjectConfiguration
    compression_events: list[CompressionEvent] = Field(default_factory=list)
    context_diagnostics: list[ContextDiagnostic] = Field(default_factory=list)
    fact_context_observations: list[FactContextObservation] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_ablation_artifact(self) -> "AblationArtifact":
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("Ablation Artifacts require the current Worker protocol")
        event_ids = [item.event_id for item in self.compression_events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Ablation Artifact event IDs must not repeat")
        if (
            len(self.compression_events)
            > self.effective_subject_configuration.maximum_compression_events
        ):
            raise ValueError("Ablation Artifact exceeds compression event limit")
        context_keys = [
            (item.session_id, item.turn_index) for item in self.context_diagnostics
        ]
        if len(context_keys) != len(set(context_keys)):
            raise ValueError("Ablation context diagnostics must be unique")
        fact_keys = [
            (item.fact_id, item.checkpoint_id)
            for item in self.fact_context_observations
        ]
        if len(fact_keys) != len(set(fact_keys)):
            raise ValueError("Ablation fact observations must be unique")
        return self


class BackgroundReviewArtifact(ContractModel):
    """Worker-owned P5 result artifact; prompt and claim material are absent."""

    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    results: list[BackgroundReviewExecutionResult] = Field(default_factory=list)
    errors: list[BackgroundReviewExecutionError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_artifact(self) -> "BackgroundReviewArtifact":
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("Background Review Artifacts require the current Worker protocol")
        review_ids = [item.review_id for item in self.results]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("Background Review Artifact result IDs must be unique")
        return self


class BackgroundReviewEvidenceArtifact(ContractModel):
    """Dedicated evidence artifact kept separate from result and snapshot data."""

    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    results: list[BackgroundReviewExecutionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_artifact(self) -> "BackgroundReviewEvidenceArtifact":
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("Background Review Evidence requires the current Worker protocol")
        return self


class BackgroundReviewSnapshotsArtifact(ContractModel):
    """Dedicated before/after state artifact for deterministic Review diffs."""

    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    results: list[BackgroundReviewExecutionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshots_artifact(self) -> "BackgroundReviewSnapshotsArtifact":
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("Background Review Snapshots require the current Worker protocol")
        return self


class ToolchainScenarioArtifact(ContractModel):
    """Content-free Toolchain scenario observations produced by the Worker."""

    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    results: list[ScenarioExecutionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_toolchain_results(self) -> "ToolchainScenarioArtifact":
        if any(item.kind.value != "toolchain" for item in self.results):
            raise ValueError("Toolchain Artifact cannot contain Process results")
        scenario_ids = [item.scenario_id for item in self.results]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("Toolchain scenario IDs must be unique")
        return self


class ProcessScenarioArtifact(ContractModel):
    """Content-free Process lifecycle observations produced by the Worker."""

    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    results: list[ScenarioExecutionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_process_results(self) -> "ProcessScenarioArtifact":
        if any(item.kind.value != "process_background" for item in self.results):
            raise ValueError("Process Artifact cannot contain Toolchain results")
        scenario_ids = [item.scenario_id for item in self.results]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("Process scenario IDs must be unique")
        return self


class ProcessCleanupArtifact(ContractModel):
    """Safe cleanup facts; no command, environment, or raw process output."""

    protocol_version: WorkerProtocolVersion = WORKER_PROTOCOL_VERSION
    trial_id: Identifier
    case_id: Identifier
    reports: list[JsonObject] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reports(self) -> "ProcessCleanupArtifact":
        allowed = {"complete", "attempted_count", "completed_count", "unresolved_ids"}
        if any(set(report) - allowed for report in self.reports):
            raise ValueError("Process cleanup reports contain unsupported fields")
        for report in self.reports:
            if "complete" in report and type(report["complete"]) is not bool:
                raise ValueError("Process cleanup complete must be a boolean")
            for field_name in ("attempted_count", "completed_count"):
                value = report.get(field_name)
                if value is not None and (
                    type(value) is not int or value < 0
                ):
                    raise ValueError(
                        f"Process cleanup {field_name} must be a non-negative integer"
                    )
            unresolved = report.get("unresolved_ids")
            if unresolved is not None:
                if not isinstance(unresolved, list) or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"[0-9a-f]{16}", item) is None
                    for item in unresolved
                ):
                    raise ValueError(
                        "Process cleanup unresolved IDs must be safe digests"
                    )
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
    memory_artifact: SafeRelativePath | None = None
    memory_query_results: list[MemoryQueryResult] = Field(default_factory=list)
    memory_snapshots: list[MemoryStateSnapshot] = Field(default_factory=list)
    memory_state_changes: list[MemoryStateChange] = Field(default_factory=list)
    memory_errors: list[MemoryOperationError] = Field(default_factory=list)
    variant_id: Identifier | None = None
    effective_subject_configuration: EffectiveSubjectConfiguration | None = None
    ablation_artifact: SafeRelativePath | None = None
    compression_events: list[CompressionEvent] = Field(default_factory=list)
    context_diagnostics: list[ContextDiagnostic] = Field(default_factory=list)
    fact_context_observations: list[FactContextObservation] = Field(
        default_factory=list
    )
    background_review_results_artifact: SafeRelativePath | None = None
    background_review_evidence_artifact: SafeRelativePath | None = None
    background_review_snapshots_artifact: SafeRelativePath | None = None
    background_review_results: list[BackgroundReviewExecutionResult] = Field(
        default_factory=list
    )
    background_review_errors: list[BackgroundReviewExecutionError] = Field(
        default_factory=list
    )
    scenario_results: list[ScenarioExecutionResult] = Field(default_factory=list)
    process_errors: list[ScenarioError] = Field(default_factory=list)
    toolchain_results_artifact: SafeRelativePath | None = None
    process_scenario_results_artifact: SafeRelativePath | None = None
    process_cleanup_artifact: SafeRelativePath | None = None
    process_output_artifacts: list[SafeRelativePath] = Field(default_factory=list)
    review_gate_passed: StrictBool | None = None
    warnings: list[WorkerWarning] = Field(default_factory=list)
    error: WorkerError | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "MyHermesWorkerResult":
        if self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("worker result protocol version is incompatible")
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
        query_ids = [item.query_id for item in self.memory_query_results]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("worker Memory query IDs must not repeat")
        snapshot_ids = [item.snapshot_id for item in self.memory_snapshots]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("worker Memory snapshot IDs must not repeat")
        snapshot_phases = [item.phase for item in self.memory_snapshots]
        if len(snapshot_phases) != len(set(snapshot_phases)):
            raise ValueError("worker Memory snapshot phases must not repeat")
        if any(
            item.phase is None
            or item.strategy is None
            or item.provider is None
            for item in self.memory_snapshots
        ):
            raise ValueError(
                "worker Memory snapshots require phase, strategy, and provider"
            )
        changed_ids = [item.memory_id for item in self.memory_state_changes]
        if len(changed_ids) != len(set(changed_ids)):
            raise ValueError("worker Memory state change IDs must not repeat")
        p4_enabled = self.effective_subject_configuration is not None
        if p4_enabled and self.protocol_version != WORKER_PROTOCOL_VERSION:
            raise ValueError("P4 results require the current Worker protocol")
        if p4_enabled != (self.variant_id is not None):
            raise ValueError("worker P4 result requires Variant and configuration")
        if p4_enabled != (self.ablation_artifact is not None):
            raise ValueError("worker P4 result requires an Ablation Artifact")
        if not p4_enabled and any(
            (
                self.compression_events,
                self.context_diagnostics,
                self.fact_context_observations,
            )
        ):
            raise ValueError("non-P4 worker results cannot contain P4 observations")
        if p4_enabled and len(self.compression_events) > (
            self.effective_subject_configuration.maximum_compression_events
        ):
            raise ValueError("worker compression events exceed the declared limit")
        review_artifacts = (
            self.background_review_results_artifact,
            self.background_review_evidence_artifact,
            self.background_review_snapshots_artifact,
        )
        review_present = bool(self.background_review_results) or bool(
            self.background_review_errors
        ) or any(item is not None for item in review_artifacts)
        if review_present and not all(item is not None for item in review_artifacts):
            raise ValueError("P5 worker results require all Review Artifact refs")
        review_ids = [item.review_id for item in self.background_review_results]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("worker Background Review result IDs must be unique")
        # The parent-side deterministic validator owns the Review gate.  A
        # Worker only reports execution facts and must not pre-judge them.
        if self.review_gate_passed is not None:
            raise ValueError("worker must not calculate the Background Review gate")
        scenario_ids = [item.scenario_id for item in self.scenario_results]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("worker scenario result IDs must be unique")
        scenario_kinds = {item.kind.value for item in self.scenario_results}
        if ("toolchain" in scenario_kinds) != (
            self.toolchain_results_artifact is not None
        ):
            raise ValueError("Toolchain worker results require their Artifact ref")
        if ("process_background" in scenario_kinds) != (
            self.process_scenario_results_artifact is not None
        ):
            raise ValueError("Process worker results require their Artifact ref")
        has_process_results = "process_background" in scenario_kinds
        if has_process_results != (self.process_cleanup_artifact is not None):
            raise ValueError("Process worker results require cleanup Artifact ref")
        if "process_background" not in scenario_kinds and self.process_output_artifacts:
            raise ValueError(
                "process output Artifacts require Process scenario results"
            )
        return self


__all__ = (
    "AblationArtifact",
    "BackgroundReviewArtifact",
    "BackgroundReviewEvidenceArtifact",
    "BackgroundReviewSnapshotsArtifact",
    "ToolchainScenarioArtifact",
    "ProcessScenarioArtifact",
    "ProcessCleanupArtifact",
    "MyHermesWorkerRequest",
    "MyHermesWorkerResult",
    "MemoryArtifact",
    "LEGACY_WORKER_PROTOCOL_VERSION",
    "MemoryQueryPlan",
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
    "WorkerTurn",
    "WorkerWarning",
)
