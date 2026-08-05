"""Parent-side MyHermes subprocess adapter; never imports hermes modules."""

from __future__ import annotations

import json
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import re
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json, atomic_write_text
from myhermes_audit.contracts import (
    AblationVariant,
    AuditCase,
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
    BackgroundReviewPlan,
    EffectiveSubjectConfiguration,
    MemoryMode,
    MemoryErrorType,
    MemoryOperationError,
    ModelObservationSummary,
    RunObservationSummary,
    ToolObservationSummary,
    TrialObservationSummary,
    TrialRuntimeSummary,
    TrialWarning,
    RetrievalStrategy,
    ToolsetName,
    ModelIdentifierSource,
    ReviewKind,
    ReviewLifecycle,
    ReviewAction,
    ReviewAttempt,
    ReviewError,
    ReviewOutcome,
    ReviewStatus,
    ProcessAction,
    E2EScenarioKind,
    ProcessScenarioExecutionResult,
    ProcessHardTimeoutSource,
    ScenarioError,
    ScenarioStatus,
    ToolchainScenarioExecutionResult,
)
from myhermes_audit.ablation import (
    applicable_checkpoints,
    applicable_fact_expectations,
    effective_config_overrides,
    effective_subject_configuration,
    effective_toolsets,
)
from myhermes_audit.contracts.suite import (
    CaseMode,
    ConversationRole,
    EvaluatorKind,
    TextTarget,
)
from myhermes_audit.datasets.fixtures import validate_runtime_fixture_support
from myhermes_audit.environment import (
    MODEL_ENVIRONMENT_ALLOWLIST,
    WORKER_INHERITED_ENVIRONMENT_ALLOWLIST,
)
from myhermes_audit.errors import (
    AblationCapabilityError,
    AblationVariantError,
    BackgroundReviewCapabilityError,
    CompressionCapabilityError,
    CompressionConfigurationError,
    CompressionObservationError,
    MemoryCapabilityError,
    MemoryKindUnsupportedError,
    MemoryMappingError,
    MemoryProtocolError,
    MemoryScopeUnsupportedError,
    MemoryStrategyUnsupportedError,
    SubjectCapabilityError,
    SubjectPreflightError,
    UnsupportedCaseError,
    WorkerProcessError,
    WorkerProtocolError,
)
from myhermes_audit.fingerprint import read_subject_fingerprint
from myhermes_audit.integrations.myhermes.capability_contracts import (
    SubjectCapabilityReport,
)
from myhermes_audit.integrations.myhermes.capability_runner import (
    run_subject_capability_probe,
)
from myhermes_audit.integrations.myhermes.config_builder import (
    MyHermesConfigBuilder,
)
from myhermes_audit.integrations.myhermes.model_identifier import (
    EffectiveModelIdentifier,
    apply_effective_model_to_worker_environment,
    resolve_effective_model_identifier,
)
from myhermes_audit.integrations.myhermes.contracts import (
    AblationArtifact,
    BackgroundReviewArtifact,
    BackgroundReviewEvidenceArtifact,
    BackgroundReviewSnapshotsArtifact,
    MemoryArtifact,
    MemoryQueryPlan,
    ProcessCleanupArtifact,
    ProcessScenarioArtifact,
    ToolchainScenarioArtifact,
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
    WORKER_PROTOCOL_VERSION,
    WorkerArtifactPaths,
    WorkerError,
    WorkerMode,
    WorkerStatus,
    WorkerTranscript,
    WorkerTurn,
    WorkerWarning,
)
from myhermes_audit.runners.base import (
    RunnerStatus,
    ToolTraceEntry,
    TrialRunnerOutcome,
)
from myhermes_audit.sandbox import AuditSandbox
from myhermes_audit.security import (
    redact_text,
    sensitive_environment_values,
    truncate_text_head_tail,
)
from myhermes_audit.validators.engine import preflight_evaluators


_LOG_BYTE_LIMIT = 1024 * 1024
_LOG_TRUNCATION_BYTES = b"\n...[truncated by my-hermes-audit]...\n"
_LOG_HEAD_BYTES = _LOG_BYTE_LIMIT // 2
_LOG_TAIL_BYTES = _LOG_BYTE_LIMIT - _LOG_HEAD_BYTES
_LOG_TRUNCATED_TAIL_BYTES = _LOG_TAIL_BYTES - len(_LOG_TRUNCATION_BYTES)
_MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
_TERMINATION_GRACE_SECONDS = 3.0


class _BoundedByteCapture:
    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.error_type: str | None = None

    def consume(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                self.total += len(chunk)
                head_remaining = max(0, _LOG_HEAD_BYTES - len(self.head))
                if head_remaining:
                    self.head.extend(chunk[:head_remaining])
                    chunk = chunk[head_remaining:]
                if chunk:
                    self.tail.extend(chunk)
                    if len(self.tail) > _LOG_TAIL_BYTES:
                        del self.tail[:-_LOG_TAIL_BYTES]
        except (OSError, ValueError) as exc:
            self.error_type = type(exc).__name__

    def render(self) -> str:
        if self.total <= _LOG_BYTE_LIMIT:
            payload = bytes(self.head + self.tail)
        else:
            payload = (
                bytes(self.head)
                + _LOG_TRUNCATION_BYTES
                + bytes(self.tail[-_LOG_TRUNCATED_TAIL_BYTES:])
            )
        return payload.decode("utf-8", errors="replace")


class MyHermesTrialRunner:
    def __init__(
        self,
        *,
        subject_repo: Path,
        subject_config: Path,
        debug: bool = False,
    ) -> None:
        self.subject_repo = Path(subject_repo).expanduser().resolve(strict=False)
        requested_config = Path(subject_config).expanduser()
        self.subject_config = requested_config.resolve(strict=False)
        self.debug = bool(debug)
        self._parent_environment: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper not in self._parent_environment or key == upper:
                self._parent_environment[upper] = value
        self._sensitive_values = sensitive_environment_values(os.environ)
        self._config_builder = MyHermesConfigBuilder(requested_config)
        self._capability_report: SubjectCapabilityReport | None = None

    @property
    def capability_report(self) -> SubjectCapabilityReport | None:
        return self._capability_report

    def _effective_model_identifier(
        self,
        case: AuditCase,
        subject_configuration: dict,
    ) -> EffectiveModelIdentifier:
        return resolve_effective_model_identifier(
            case_environment=case.execution.environment_overrides,
            parent_environment=self._parent_environment,
            subject_configuration=subject_configuration,
            sensitive_values=self._sensitive_values,
        )

    def p4_effective_subject_configuration(
        self,
        case: AuditCase,
        variant: AblationVariant,
    ) -> EffectiveSubjectConfiguration:
        report = self._capability_report
        if report is None:
            raise AblationCapabilityError(
                "P4 capability report is unavailable",
                case_id=case.case_id,
                variant_id=variant.variant_id,
            )
        preliminary = effective_subject_configuration(
            case,
            variant,
            compression_threshold_control=report.compression_threshold_control,
            emergency_overflow_compression_disable_supported=(
                report.emergency_overflow_compression_disable_supported
            ),
            compression_observation_supported=(
                report.compression_observation_supported
            ),
        )
        prepared = self._config_builder.prepare(
            effective_config_overrides(case, preliminary)
        )
        resolution = self._effective_model_identifier(case, prepared.document)
        return effective_subject_configuration(
            case,
            variant,
            compression_threshold_control=report.compression_threshold_control,
            emergency_overflow_compression_disable_supported=(
                report.emergency_overflow_compression_disable_supported
            ),
            compression_observation_supported=(
                report.compression_observation_supported
            ),
            model_identifier=resolution.model_identifier,
            model_identifier_source=resolution.source,
        )

    def preflight(self, cases: Sequence[AuditCase]) -> None:
        self._preflight_subject()
        for case in cases:
            self._preflight_case(case)

    def _preflight_subject(self) -> None:
        if not self.subject_repo.is_dir():
            raise SubjectPreflightError("subject repository is not a directory")
        hermes_package = self.subject_repo / "hermes"
        required = (
            hermes_package / "__init__.py",
            hermes_package / "config.py",
            hermes_package / "conversation.py",
            hermes_package / "prompt.py",
            self.subject_repo / "pyproject.toml",
        )
        if hermes_package.is_symlink() or any(
            not path.is_file() or path.is_symlink() for path in required
        ):
            raise SubjectPreflightError(
                "subject repository does not contain a regular importable hermes package"
            )
        fingerprint = read_subject_fingerprint(self.subject_repo)
        if (
            self._capability_report is None
            or self._capability_report.subject_commit != fingerprint.git_commit
        ):
            self._capability_report = run_subject_capability_probe(
                subject_repo=self.subject_repo,
                subject_config=self.subject_config,
                subject_commit=fingerprint.git_commit,
            )

    def _preflight_case(self, case: AuditCase) -> None:
        if case.mode not in {
            CaseMode.SINGLE_TURN,
            CaseMode.SCRIPTED_MULTI_TURN,
        }:
            raise UnsupportedCaseError(
                "P1 supports only single_turn and scripted_multi_turn",
                case_id=case.case_id,
                mode=case.mode.value,
            )
        if "enabled_toolsets" not in case.execution.model_fields_set:
            raise UnsupportedCaseError(
                "P1 cases must explicitly declare execution.enabled_toolsets",
                case_id=case.case_id,
            )
        if case.execution.workdir != "workspace":
            raise UnsupportedCaseError(
                "P1 requires execution.workdir=workspace",
                case_id=case.case_id,
            )
        if ToolsetName.SKILL_READ in case.execution.enabled_toolsets:
            self._preflight_skill_read_case(case)
        if case.scenarios:
            self._preflight_scenarios(case)
        if case.mode is CaseMode.SCRIPTED_MULTI_TURN and any(
            turn.role is not ConversationRole.USER for turn in case.input.turns
        ):
            raise UnsupportedCaseError(
                "P1 scripted turns must contain only user messages",
                case_id=case.case_id,
            )
        review_case = _is_background_review_case(case)
        memory_case = _is_memory_case(case) or any(
            plan.kind is ReviewKind.MEMORY
            for plan in case.fixture.background_review_plans
        )
        validate_runtime_fixture_support(
            case.fixture,
            allow_memory=memory_case,
            allow_background_review=review_case,
        )
        self._config_builder.prepare(case.execution.config_overrides)

        unsupported_evaluators = [
            item.kind.value
            for item in case.evaluators
            if item.kind not in {
                EvaluatorKind.DETERMINISTIC,
                EvaluatorKind.TOOL_TRAJECTORY,
                EvaluatorKind.LLM_JUDGE,
                EvaluatorKind.RETRIEVAL,
                EvaluatorKind.COMPRESSION,
                EvaluatorKind.BACKGROUND_REVIEW,
                EvaluatorKind.SCENARIO,
            }
        ]
        if unsupported_evaluators:
            raise UnsupportedCaseError(
                "case uses evaluators outside the implemented runtime boundary",
                case_id=case.case_id,
                evaluator_kinds=unsupported_evaluators,
            )
        if case.expected.background_reviews and not review_case:
            raise UnsupportedCaseError(
                "Background Review expectations require an explicit P5 plan",
                case_id=case.case_id,
            )
        if memory_case:
            self._preflight_memory_case(case)
        if case.ablation is not None:
            self._preflight_ablation_case(case)
        if review_case:
            if case.ablation is not None:
                raise UnsupportedCaseError(
                    "P5 Background Review plans cannot be combined with P4 ablations",
                    case_id=case.case_id,
                )
            self._preflight_background_review_case(case)
        if any(
            item.target is not TextTarget.FINAL_OUTPUT
            for item in case.expected.texts
        ):
            raise UnsupportedCaseError(
                "P1 text expectations support only final_output",
                case_id=case.case_id,
            )
        if any(item.calls for item in case.expected.tool_trajectories):
            raise UnsupportedCaseError(
                "P1 does not enforce exact ordered tool argument trajectories",
                case_id=case.case_id,
            )
        preflight_evaluators(case)

    def _preflight_scenarios(self, case: AuditCase) -> None:
        report = self._capability_report
        if report is None:
            raise SubjectCapabilityError(
                f"case={case.case_id}: Subject capability report is unavailable",
                case_id=case.case_id,
            )
        for scenario in case.scenarios:
            missing_toolsets = sorted(
                set(scenario.required_toolsets)
                - {item.value for item in case.execution.enabled_toolsets}
            )
            if missing_toolsets:
                raise SubjectCapabilityError(
                    (
                        f"case={case.case_id}, scenario={scenario.scenario_id}: "
                        "scenario required Toolset is not enabled by foreground: "
                        + ", ".join(missing_toolsets)
                    ),
                    case_id=case.case_id,
                    scenario_id=scenario.scenario_id,
                    missing_toolsets=missing_toolsets,
                    supported_toolsets=_supported_foreground_toolsets(report),
                )
            required: list[str] = []
            required.extend(
                name
                for toolset in scenario.required_toolsets
                for name in _SCENARIO_TOOLSET_CAPABILITIES.get(toolset, ())
            )
            if scenario.kind.value == "process_background":
                required.append("process_toolset_actions")
                required.append("process_start_via_terminal")
                for step in scenario.steps:
                    action = step.action
                    if action is ProcessAction.READ_INCREMENTAL:
                        required.append("process_log")
                    elif action is ProcessAction.SEND_INPUT:
                        required.append(
                            "process_submit" if step.submit else "process_write"
                        )
                    elif action is ProcessAction.WAIT:
                        required.append("process_wait")
                    elif action is ProcessAction.INTERRUPT:
                        required.append("process_interrupt")
                    elif action is ProcessAction.KILL:
                        required.append("process_kill")
                    elif action is ProcessAction.CLOSE:
                        required.append("process_close")
                    elif action is ProcessAction.ASSERT_STATUS:
                        required.append("process_poll")
                if scenario.cleanup is not None and scenario.cleanup.required:
                    # Worker cleanup is a lifecycle fact, not a foreground
                    # Process action.  The worker already exposes the public
                    # session cleanup report; no ProcessManager method is
                    # treated as an Agent capability here.
                    required.append("session_resource_cleanup")
            missing = [
                name
                for name in dict.fromkeys(required)
                if not _capability_available(report, name)
            ]
            if not missing:
                if scenario.kind.value == "process_background":
                    _preflight_process_statuses(
                        case_id=case.case_id,
                        scenario=scenario,
                        report=report,
                    )
                continue
            requested_action = next(
                (
                    step.action.value
                    for step in getattr(scenario, "steps", ())
                    if step.action is ProcessAction.INTERRUPT
                    and not _capability_available(report, "process_interrupt")
                ),
                None,
            )
            supported = ",".join(
                item.name
                for item in report.capabilities
                if item.available and item.name.startswith("process_")
            ) or "<none>"
            supported_actions = ",".join(report.supported_process_actions) or "<none>"
            raise SubjectCapabilityError(
                (
                    f"case={case.case_id}, scenario={scenario.scenario_id}: "
                    + (
                        f"requested_action={requested_action}; "
                        if requested_action is not None
                        else ""
                    )
                    + f"missing public capability={missing[0]}; "
                    + f"supported_process_actions={supported_actions}; "
                    + f"supported process capabilities={supported}"
                ),
                case_id=case.case_id,
                scenario_id=scenario.scenario_id,
                missing_capability=missing[0],
                missing_capabilities=missing,
                requested_action=requested_action,
                supported_process_actions=list(report.supported_process_actions),
                supported_toolsets=_supported_foreground_toolsets(report),
            )

    def _preflight_skill_read_case(self, case: AuditCase) -> None:
        report = self._capability_report
        required = _SKILL_READ_CAPABILITIES
        missing = (
            list(required)
            if report is None
            else [
                name
                for name in required
                if not _capability_available(report, name)
            ]
        )
        if not missing:
            return
        supported_toolsets = (
            []
            if report is None
            else _supported_foreground_toolsets(report)
        )
        missing_capability = missing[0]
        supported_display = ",".join(supported_toolsets) or "<none>"
        raise SubjectCapabilityError(
            (
                f"case={case.case_id}: requested toolset=skill_read is "
                f"unsupported; missing capability={missing_capability}; "
                f"supported toolsets={supported_display}"
            ),
            case_id=case.case_id,
            requested_toolset=ToolsetName.SKILL_READ.value,
            missing_capability=missing_capability,
            missing_capabilities=missing,
            supported_toolsets=supported_toolsets,
        )

    def _preflight_background_review_case(self, case: AuditCase) -> None:
        report = self._capability_report
        if report is None:
            raise BackgroundReviewCapabilityError(
                "Subject Background Review capability report is unavailable",
                case_id=case.case_id,
            )
        required = (
            "background_review_runtime",
            "background_review_runtime_config",
            "review_claim_contract",
            "review_driver_registry",
            "review_agent_loop",
            "review_loop_result_contract",
            "review_hook_registry",
            "review_observation_sink",
            "review_foreground_event",
            "review_tool_policy",
            "review_tool_registry_resolution",
            "review_tool_registration",
            "review_claim_validation",
            "review_claim_completion",
            "review_claim_failure",
            "review_evidence_window",
            "review_foreground_evidence_window",
            "review_outcome_observation",
            "review_shutdown",
        )
        missing = [name for name in required if not _capability_available(report, name)]
        if missing:
            raise BackgroundReviewCapabilityError(
                "Subject lacks required public Background Review capabilities",
                case_id=case.case_id,
                missing_capabilities=missing,
            )
        for plan in case.fixture.background_review_plans:
            kind_capability = f"{plan.kind.value}_review_supported"
            if (
                plan.kind not in report.supported_review_kinds
                or not _capability_available(report, kind_capability)
            ):
                raise BackgroundReviewCapabilityError(
                    "Subject does not support the planned Background Review kind",
                    case_id=case.case_id,
                    review_id=plan.review_id,
                    review_kind=plan.kind.value,
                    missing_capability=kind_capability,
                )
            if plan.lifecycle is ReviewLifecycle.STALE_BEFORE_EXECUTE and not _capability_available(
                report,
                "stale_review_detection",
            ):
                raise BackgroundReviewCapabilityError(
                    "Subject cannot publicly detect a governance-stale Review claim",
                    case_id=case.case_id,
                    review_id=plan.review_id,
                    missing_capability="stale_review_detection",
                )
            if plan.kind is ReviewKind.SKILL and not _capability_available(
                report,
                "skill_governance_revision",
            ):
                raise BackgroundReviewCapabilityError(
                    "Subject cannot snapshot the public Skill governance revision",
                    case_id=case.case_id,
                    review_id=plan.review_id,
                    missing_capability="skill_governance_revision",
                )
        if case.fixture.skills and not _capability_available(
            report,
            "review_state_snapshot",
        ):
            raise BackgroundReviewCapabilityError(
                "Subject cannot seed Skill fixtures through the public Skill API",
                case_id=case.case_id,
                missing_capability="review_state_snapshot",
            )
        if (
            any(fixture.pinned for fixture in case.fixture.skills)
            and not _capability_available(report, "skill_governance_revision")
        ):
            raise BackgroundReviewCapabilityError(
                "Subject cannot pin Skill fixtures without public governance revisions",
                case_id=case.case_id,
                missing_capability="skill_governance_revision",
            )
        for fixture in case.fixture.skills:
            if fixture.source.value != "local" or fixture.managed_by.value == "external":
                raise BackgroundReviewCapabilityError(
                    "Skill fixture cannot be seeded through the public Skill API",
                    case_id=case.case_id,
                    skill_id=fixture.skill_id,
                )

    def _preflight_memory_case(self, case: AuditCase) -> None:
        report = self._capability_report
        if report is None:
            raise MemoryCapabilityError(
                "Subject Memory capability report is unavailable",
                case_id=case.case_id,
            )
        strategy = case.execution.memory_strategy
        if strategy is None:
            raise MemoryCapabilityError(
                "P3 Memory cases must explicitly declare execution.memory_strategy",
                case_id=case.case_id,
                missing_capability="declared_memory_strategy",
            )
        supported = list(report.supported_retrieval_strategies)
        if strategy not in supported:
            missing_capability = (
                "ranked_query+declared_retrieval_strategies"
                if strategy in {
                    RetrievalStrategy.DENSE,
                    RetrievalStrategy.BM25,
                    RetrievalStrategy.HYBRID,
                }
                else "memory_prompt_render+memory_prompt_toggle"
            )
            raise MemoryStrategyUnsupportedError(
                "requested Memory retrieval strategy is not supported by Subject",
                case_id=case.case_id,
                requested_strategy=strategy.value,
                supported_strategies=[item.value for item in supported],
                missing_capability=missing_capability,
            )
        if (
            strategy is RetrievalStrategy.DISABLED
            and ToolsetName.MEMORY in case.execution.enabled_toolsets
        ):
            raise MemoryCapabilityError(
                "disabled Memory strategy cannot enable the memory toolset",
                case_id=case.case_id,
                missing_capability="disabled_tool_policy_conflict",
            )
        if ToolsetName.MEMORY in case.execution.enabled_toolsets:
            capability = report.capability("memory_tool")
            if capability is None or not capability.available:
                raise MemoryCapabilityError(
                    "Subject public memory tool is unavailable",
                    case_id=case.case_id,
                    missing_capability="memory_tool",
                )

        fixture_items = (
            [] if case.fixture.memory is None else case.fixture.memory.items
        )
        supported_kinds = set(report.supported_memory_kinds)
        requested_kinds = {item.kind for item in fixture_items}
        requested_kinds.update(
            kind
            for expectation in case.expected.memories
            for kind in expectation.required_kinds
        )
        requested_kinds.update(
            content.kind
            for expectation in case.expected.memory_states
            for content in (
                *expectation.required_added_content,
                *expectation.forbidden_added_content,
            )
            if content.kind is not None
        )
        unsupported_kinds = sorted(
            requested_kinds - supported_kinds,
            key=lambda item: item.value,
        )
        if unsupported_kinds:
            raise MemoryKindUnsupportedError(
                "Memory case requests kinds unsupported by Subject",
                case_id=case.case_id,
                requested_kinds=[item.value for item in unsupported_kinds],
                supported_kinds=[item.value for item in report.supported_memory_kinds],
            )
        by_target: dict[str, set[str]] = {"memory": set(), "user": set()}
        target_by_kind = {
            "long_term": "memory",
            "user_profile": "user",
        }
        for item in fixture_items:
            target = target_by_kind.get(item.kind.value)
            if target is None:
                continue
            normalized = " ".join(
                unicodedata.normalize("NFKC", item.content).split()
            ).casefold()
            if normalized in by_target[target]:
                raise MemoryMappingError(
                    "Memory fixture entries are indistinguishable in a Subject target",
                    case_id=case.case_id,
                    target=target,
                )
            by_target[target].add(normalized)
            if item.user_id is not None and not _capability_available(
                report,
                "user_filtering",
            ):
                raise MemoryScopeUnsupportedError(
                    "Subject cannot preserve fixture user scope",
                    case_id=case.case_id,
                    missing_capability="user_filtering",
                )
            if item.session_id is not None and not _capability_available(
                report,
                "session_filtering",
            ):
                raise MemoryScopeUnsupportedError(
                    "Subject cannot preserve fixture session scope",
                    case_id=case.case_id,
                    missing_capability="session_filtering",
                )
        for expectation in case.expected.memories:
            query = expectation.query
            if query.user_id is not None and not _capability_available(
                report,
                "user_filtering",
            ):
                raise MemoryScopeUnsupportedError(
                    "Subject does not support Memory user filtering",
                    case_id=case.case_id,
                    query_id=expectation.query_id,
                    missing_capability="user_filtering",
                )
            if query.session_id is not None and not _capability_available(
                report,
                "session_filtering",
            ):
                raise MemoryScopeUnsupportedError(
                    "Subject does not support Memory session filtering",
                    case_id=case.case_id,
                    query_id=expectation.query_id,
                    missing_capability="session_filtering",
                )
            if query.filters and not _capability_available(
                report,
                "query_filters",
            ):
                raise MemoryScopeUnsupportedError(
                    "Subject does not support declared Memory query filters",
                    case_id=case.case_id,
                    query_id=expectation.query_id,
                    missing_capability="query_filters",
                )

    def _preflight_ablation_case(self, case: AuditCase) -> None:
        plan = case.ablation
        report = self._capability_report
        if plan is None:
            return
        if report is None:
            raise AblationCapabilityError(
                "Subject capability report is unavailable for P4",
                case_id=case.case_id,
            )
        capability_summary = _compression_capability_summary(report)
        observation_available = report.compression_observation_supported
        if (
            plan.require_emergency_compression_disable
            and not report.emergency_overflow_compression_disable_supported
        ):
            raise CompressionCapabilityError(
                "required emergency overflow Compression disable is unavailable",
                case_id=case.case_id,
                requested_capability="emergency_compression_disable",
                missing_capability="emergency_compression_disable",
                supported_capabilities=capability_summary,
            )
        if plan.minimum_compression_events and not observation_available:
            raise CompressionObservationError(
                "required Compression occurrence cannot be observed publicly",
                case_id=case.case_id,
                requested_capability="compression_observation",
                missing_capability="compression_observation",
                supported_capabilities=capability_summary,
            )
        for variant in plan.variants:
            if variant.memory_mode not in report.supported_memory_modes:
                raise AblationCapabilityError(
                    "Subject does not support the requested Memory mode",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                    memory_mode=variant.memory_mode.value,
                    missing_capability="memory_mode",
                )
            if variant.compression_mode not in report.supported_compression_modes:
                raise CompressionCapabilityError(
                    "Subject does not expose safe public Compression control",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                    requested_compression_mode=variant.compression_mode.value,
                    requested_capability="compression_threshold_control",
                    missing_capability="compression_threshold_control",
                    supported_capabilities=capability_summary,
                )
            variant_expectations = applicable_fact_expectations(
                case,
                variant.variant_id,
            )
            if (
                any(
                    fact.must_survive_compression
                    for expectation in variant_expectations
                    for fact in expectation.facts
                )
                and not observation_available
            ):
                raise CompressionObservationError(
                    "required Compression survival cannot be observed publicly",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                    requested_capability="compression_observation",
                    missing_capability="compression_observation",
                    supported_capabilities=capability_summary,
                )
            try:
                configuration = self.p4_effective_subject_configuration(
                    case,
                    variant,
                )
                self._config_builder.prepare(
                    effective_config_overrides(case, configuration)
                )
            except Exception as exc:
                raise CompressionConfigurationError(
                    "Variant public configuration cannot be applied safely",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                    error_type=type(exc).__name__,
                ) from exc
            if configuration.memory_tool_enabled and not _capability_available(
                report,
                "memory_tool",
            ):
                raise AblationCapabilityError(
                    "Variant requires an unavailable public memory tool",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                    missing_capability="memory_tool",
                )

    def run_trial(
        self,
        case: AuditCase,
        sandbox: AuditSandbox,
        *,
        trial_id: str,
        timeout_seconds: int,
        variant: AblationVariant | None = None,
    ) -> TrialRunnerOutcome:
        for scenario in case.scenarios:
            if scenario.timeout_seconds > timeout_seconds:
                raise SubjectPreflightError(
                    "scenario timeout exceeds the Trial watchdog budget",
                    case_id=case.case_id,
                    scenario_id=scenario.scenario_id,
                    scenario_timeout_seconds=scenario.timeout_seconds,
                    trial_timeout_seconds=timeout_seconds,
                )
        configuration = None
        if variant is not None:
            if case.ablation is None:
                raise AblationVariantError(
                    "Variant execution requires an AblationPlan",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                )
            report = self._capability_report
            if report is None:
                raise AblationCapabilityError(
                    "P4 capability report is unavailable",
                    case_id=case.case_id,
                    variant_id=variant.variant_id,
                )
            configuration = self.p4_effective_subject_configuration(case, variant)
        memory_case = (
            _is_memory_case(case)
            or any(
                plan.kind is ReviewKind.MEMORY
                for plan in case.fixture.background_review_plans
            )
            if configuration is None
            else configuration.include_memory
        )
        memory_strategy = (
            case.execution.memory_strategy
            if configuration is None
            else configuration.memory_strategy
        )
        review_enabled = _is_background_review_case(case)
        required_process_scenarios = [
            item
            for item in case.scenarios
            if item.kind is E2EScenarioKind.PROCESS_BACKGROUND and item.required
        ]
        process_watchdog_enabled = len(required_process_scenarios) == 1
        # Only the single required Process Scenario is allowed to tighten the
        # parent Worker watchdog. Toolchain and optional Process scenarios do
        # not change the existing Trial/execution timeout.
        scenario_timeout = (
            min(timeout_seconds, required_process_scenarios[0].timeout_seconds)
            if process_watchdog_enabled
            else timeout_seconds
        )
        hard_timeout_source = (
            ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
            if process_watchdog_enabled
            else ProcessHardTimeoutSource.TRIAL_WATCHDOG
        )
        hard_timeout_scenario_id = (
            required_process_scenarios[0].scenario_id
            if process_watchdog_enabled
            else None
        )
        paths = _worker_artifact_paths(
            sandbox,
            memory_enabled=memory_case,
            ablation_enabled=configuration is not None,
            background_review_enabled=review_enabled,
            scenarios=case.scenarios,
        )
        started = time.perf_counter()
        captured_stdout = ""
        captured_stderr = ""
        process = None
        subject_model: str | None = None
        sensitive_values = self._sensitive_values
        try:
            turns = [
                turn.model_copy(
                    update={
                        "message": redact_text(turn.message, sensitive_values),
                    }
                )
                for turn in _case_turns(
                    case,
                    configuration=configuration,
                    variant_id=(None if variant is None else variant.variant_id),
                )
            ]
            enabled_toolsets = (
                case.execution.enabled_toolsets
                if configuration is None
                else effective_toolsets(case, configuration)
            )
            request = MyHermesWorkerRequest(
                trial_id=trial_id,
                case_id=case.case_id,
                mode=WorkerMode(case.mode.value),
                turns=turns,
                workspace=sandbox.workspace.resolve(strict=True),
                hermes_home=sandbox.hermes_home.resolve(strict=True),
                sqlite_path=sandbox.sqlite_path.resolve(strict=False),
                enabled_toolsets=enabled_toolsets,
                memory_strategy=memory_strategy,
                memory_fixture=(case.fixture.memory if memory_case else None),
                memory_queries=[
                    MemoryQueryPlan(
                        query_id=item.query_id,
                        phase=item.phase,
                        query=item.query,
                    )
                    for item in case.expected.memories
                ] if memory_case else [],
                variant_id=(None if variant is None else variant.variant_id),
                effective_subject_configuration=configuration,
                required_fact_expectations=(
                    []
                    if configuration is None
                    else applicable_fact_expectations(
                        case,
                        variant.variant_id,
                    )
                ),
                checkpoints=(
                    []
                    if case.ablation is None or configuration is None
                    else applicable_checkpoints(
                        case,
                        variant.variant_id,
                    )
                ),
                background_review_plans=list(
                    case.fixture.background_review_plans
                ),
                skill_fixtures=list(case.fixture.skills),
                scenarios=list(case.scenarios),
                process_watchdog_enabled=process_watchdog_enabled,
                hard_timeout_source=hard_timeout_source,
                hard_timeout_seconds=scenario_timeout,
                hard_timeout_scenario_id=hard_timeout_scenario_id,
                timeout_seconds=scenario_timeout,
                artifact_paths=paths,
            )
            atomic_write_json(paths.worker_request, request)
            prepared = self._config_builder.prepare(
                case.execution.config_overrides
                if configuration is None
                else effective_config_overrides(case, configuration)
            )
            model_resolution = self._effective_model_identifier(
                case,
                prepared.document,
            )
            if (
                model_resolution.source
                is ModelIdentifierSource.SUBJECT_CONFIGURATION
                and model_resolution.worker_model_value is not None
            ):
                prepared.document["model"] = model_resolution.worker_model_value
            self._config_builder.write_prepared(
                sandbox.hermes_home / "config.yaml",
                prepared,
            )
            subject_model = model_resolution.model_identifier
            if configuration is not None and (
                configuration.model_identifier != model_resolution.model_identifier
                or configuration.model_identifier_source is not model_resolution.source
            ):
                raise WorkerProtocolError(
                    "effective model identity changed after P4 preflight",
                    case_id=case.case_id,
                    variant_id=variant.variant_id if variant is not None else None,
                )
            environment = self._build_worker_environment(
                case,
                sandbox,
                trial_id=trial_id,
                config_references=prepared.environment_references,
                model_resolution=model_resolution,
            )
            process, stdout_capture, stderr_capture = self._start_worker(
                request,
                environment,
            )
            timed_out = False
            runtime_warnings: list[WorkerWarning] = []
            try:
                process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_worker(process)
            finally:
                captured_stdout, captured_stderr, inherited_pipe = _finish_captures(
                    process,
                    stdout_capture,
                    stderr_capture,
                )
                if inherited_pipe:
                    if timed_out:
                        runtime_warnings.append(
                            WorkerWarning(
                                warning_type="descendant_pipe_open",
                                message=(
                                    "worker descendant held an output pipe during "
                                    "timeout cleanup"
                                ),
                            )
                        )
                    else:
                        raise WorkerProcessError(
                            "worker descendant kept an output pipe open after worker exit"
                        )

            captured_stdout = redact_text(captured_stdout, sensitive_values)
            captured_stderr = redact_text(captured_stderr, sensitive_values)
            atomic_write_text(paths.stdout_log, captured_stdout)
            atomic_write_text(paths.stderr_log, captured_stderr)
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            if timed_out:
                recovered_memory = _recover_parent_memory_artifact(
                    paths,
                    trial_id=trial_id,
                    case_id=case.case_id,
                    strategy=memory_strategy,
                )
                recovered_ablation = _recover_parent_ablation_artifact(
                    paths,
                    trial_id=trial_id,
                    case_id=case.case_id,
                    variant_id=(None if variant is None else variant.variant_id),
                    configuration=configuration,
                )
                (
                    recovered_background_review_results,
                    recovered_background_review_errors,
                ) = (
                    _recover_parent_background_review_results(
                        paths,
                        trial_id=trial_id,
                        case_id=case.case_id,
                        plans=case.fixture.background_review_plans,
                    )
                )
                result = _fallback_worker_result(
                    paths,
                    error_type="timeout",
                    message=(
                        "MyHermes worker exceeded the required Process Scenario timeout"
                        if process_watchdog_enabled
                        else "MyHermes worker exceeded the Trial timeout"
                    ),
                    duration_ms=duration_ms,
                    warnings=runtime_warnings,
                    memory_strategy=memory_strategy,
                    recovered_memory=recovered_memory,
                    variant_id=(None if variant is None else variant.variant_id),
                    configuration=configuration,
                    recovered_ablation=recovered_ablation,
                    background_review_plans=case.fixture.background_review_plans,
                    recovered_background_review_results=(
                        recovered_background_review_results
                    ),
                    recovered_background_review_errors=(
                        recovered_background_review_errors
                    ),
                    scenarios=case.scenarios,
                    hard_timeout_source=hard_timeout_source,
                    hard_timeout_seconds=request.timeout_seconds,
                    trial_watchdog_timed_out=(
                        hard_timeout_source is ProcessHardTimeoutSource.TRIAL_WATCHDOG
                        and request.timeout_seconds >= timeout_seconds
                    ),
                )
                result, recovered_memory = _redact_memory_facts(
                    result,
                    recovered_memory,
                    sensitive_values,
                )
                result, recovered_ablation = _redact_ablation_facts(
                    result,
                    recovered_ablation,
                )
                result = _redact_background_review_facts(
                    result,
                    sensitive_values,
                )
                atomic_write_json(paths.worker_result, result)
                _ensure_empty_worker_artifacts(
                    paths,
                    trial_id,
                    case.case_id,
                    memory_strategy=memory_strategy,
                    memory_errors=result.memory_errors,
                    recovered_memory=recovered_memory,
                    variant_id=(None if variant is None else variant.variant_id),
                    configuration=configuration,
                    recovered_ablation=recovered_ablation,
                    background_review_plans=case.fixture.background_review_plans,
                    background_review_results=result.background_review_results,
                    background_review_errors=result.background_review_errors,
                    scenarios=case.scenarios,
                    scenario_results=result.scenario_results,
                )
                return self._outcome_from_result(
                    result,
                    paths,
                    status=RunnerStatus.TIMEOUT,
                    include_runtime=False,
                )

            result = _read_protocol_model(
                paths.worker_result,
                MyHermesWorkerResult,
            )
            transcript = _read_protocol_model(paths.transcript, WorkerTranscript)
            observations = _read_protocol_model(
                paths.observations,
                ObservationBundle,
            )
            memory_artifact = (
                None
                if paths.memory is None
                else _read_protocol_model(paths.memory, MemoryArtifact)
            )
            ablation_artifact = (
                None
                if paths.ablation is None
                else _read_protocol_model(paths.ablation, AblationArtifact)
            )
            review_artifact = (
                None
                if paths.background_review_results is None
                else _read_protocol_model(
                    paths.background_review_results,
                    BackgroundReviewArtifact,
                )
            )
            review_evidence_artifact = (
                None
                if paths.background_review_evidence is None
                else _read_protocol_model(
                    paths.background_review_evidence,
                    BackgroundReviewEvidenceArtifact,
                )
            )
            review_snapshots_artifact = (
                None
                if paths.background_review_snapshots is None
                else _read_protocol_model(
                    paths.background_review_snapshots,
                    BackgroundReviewSnapshotsArtifact,
                )
            )
            toolchain_artifact = (
                None
                if paths.toolchain_results is None
                else _read_protocol_model(
                    paths.toolchain_results,
                    ToolchainScenarioArtifact,
                )
            )
            process_artifact = (
                None
                if paths.process_scenario_results is None
                else _read_protocol_model(
                    paths.process_scenario_results,
                    ProcessScenarioArtifact,
                )
            )
            process_cleanup_artifact = (
                None
                if paths.process_cleanup is None
                else _read_protocol_model(
                    paths.process_cleanup,
                    ProcessCleanupArtifact,
                )
            )
            _validate_worker_artifacts(
                request,
                result,
                transcript,
                observations,
                memory_artifact,
                ablation_artifact,
                review_artifact,
                review_evidence_artifact,
                review_snapshots_artifact,
                toolchain_artifact,
                process_artifact,
                process_cleanup_artifact,
                returncode=process.returncode,
            )
            result, transcript = _redact_worker_content(
                result,
                transcript,
                sensitive_values,
            )
            result, memory_artifact = _redact_memory_facts(
                result,
                memory_artifact,
                sensitive_values,
            )
            result = _redact_background_review_facts(
                result,
                sensitive_values,
            )
            result, ablation_artifact = _redact_ablation_facts(
                result,
                ablation_artifact,
            )
            atomic_write_json(paths.worker_result, result)
            atomic_write_json(paths.transcript, transcript)
            if paths.memory is not None and memory_artifact is not None:
                atomic_write_json(paths.memory, memory_artifact)
            if paths.ablation is not None and ablation_artifact is not None:
                atomic_write_json(paths.ablation, ablation_artifact)
            _write_background_review_artifacts(
                paths,
                trial_id=trial_id,
                case_id=case.case_id,
                results=result.background_review_results,
                errors=result.background_review_errors,
            )
            status = (
                RunnerStatus.COMPLETED
                if result.worker_status is WorkerStatus.COMPLETED
                else (
                    RunnerStatus.ENVIRONMENT_ERROR
                    if result.error_type in {
                        "worker_exception",
                        "worker_terminated",
                    }
                    else RunnerStatus.FAILED
                )
            )
            return self._outcome_from_result(
                result,
                paths,
                status=status,
                observations=observations,
                subject_model=subject_model,
            )
        except Exception as exc:
            worker_warnings: list[WorkerWarning] = []
            if process is not None and process.poll() is None:
                try:
                    self._terminate_worker(process)
                except Exception as termination_exc:
                    worker_warnings.append(
                        _worker_warning(
                            "process_cleanup_error",
                            termination_exc,
                        )
                    )
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            if self.debug:
                captured_stderr += "\n" + _safe_traceback(exc)
            captured_stdout = redact_text(captured_stdout, sensitive_values)
            captured_stderr = redact_text(captured_stderr, sensitive_values)
            captured_stderr = truncate_text_head_tail(
                captured_stderr,
                limit=_LOG_BYTE_LIMIT,
            )
            try:
                atomic_write_text(paths.stdout_log, captured_stdout)
                atomic_write_text(paths.stderr_log, captured_stderr)
            except Exception as log_exc:
                worker_warnings.append(
                    _worker_warning("log_publication_error", log_exc)
                )
            recovered_memory = _recover_parent_memory_artifact(
                paths,
                trial_id=trial_id,
                case_id=case.case_id,
                strategy=memory_strategy,
            )
            recovered_ablation = _recover_parent_ablation_artifact(
                paths,
                trial_id=trial_id,
                case_id=case.case_id,
                variant_id=(None if variant is None else variant.variant_id),
                configuration=configuration,
            )
            (
                recovered_background_review_results,
                recovered_background_review_errors,
            ) = (
                _recover_parent_background_review_results(
                    paths,
                    trial_id=trial_id,
                    case_id=case.case_id,
                    plans=case.fixture.background_review_plans,
                )
            )
            result = _fallback_worker_result(
                paths,
                error_type="environment_error",
                message=f"worker environment failed: {type(exc).__name__}",
                duration_ms=duration_ms,
                warnings=worker_warnings,
                memory_strategy=memory_strategy,
                recovered_memory=recovered_memory,
                variant_id=(None if variant is None else variant.variant_id),
                configuration=configuration,
                recovered_ablation=recovered_ablation,
                background_review_plans=case.fixture.background_review_plans,
                recovered_background_review_results=(
                    recovered_background_review_results
                ),
                recovered_background_review_errors=(
                    recovered_background_review_errors
                ),
                scenarios=case.scenarios,
                hard_timeout_source=hard_timeout_source,
                hard_timeout_seconds=scenario_timeout,
            )
            result, recovered_memory = _redact_memory_facts(
                result,
                recovered_memory,
                sensitive_values,
            )
            result, recovered_ablation = _redact_ablation_facts(
                result,
                recovered_ablation,
            )
            result = _redact_background_review_facts(
                result,
                sensitive_values,
            )
            try:
                atomic_write_json(paths.worker_result, result)
                _ensure_empty_worker_artifacts(
                    paths,
                    trial_id,
                    case.case_id,
                    memory_strategy=memory_strategy,
                    memory_errors=result.memory_errors,
                    recovered_memory=recovered_memory,
                    variant_id=(None if variant is None else variant.variant_id),
                    configuration=configuration,
                    recovered_ablation=recovered_ablation,
                    background_review_plans=case.fixture.background_review_plans,
                    background_review_results=result.background_review_results,
                    background_review_errors=result.background_review_errors,
                    scenarios=case.scenarios,
                    scenario_results=result.scenario_results,
                )
            except Exception as artifact_exc:
                worker_warnings.append(
                    _worker_warning("fallback_artifact_error", artifact_exc)
                )
                result = _fallback_worker_result(
                    paths,
                    error_type="environment_error",
                    message=f"worker environment failed: {type(exc).__name__}",
                    duration_ms=duration_ms,
                    warnings=worker_warnings,
                    memory_strategy=memory_strategy,
                    recovered_memory=recovered_memory,
                    variant_id=(None if variant is None else variant.variant_id),
                    configuration=configuration,
                    recovered_ablation=recovered_ablation,
                    background_review_plans=case.fixture.background_review_plans,
                    recovered_background_review_results=(
                        recovered_background_review_results
                    ),
                    recovered_background_review_errors=(
                        recovered_background_review_errors
                    ),
                    scenarios=case.scenarios,
                    hard_timeout_source=hard_timeout_source,
                    hard_timeout_seconds=scenario_timeout,
                )
            return self._outcome_from_result(
                result,
                paths,
                status=RunnerStatus.ENVIRONMENT_ERROR,
                include_runtime=False,
            )

    def _build_worker_environment(
        self,
        case: AuditCase,
        sandbox: AuditSandbox,
        *,
        trial_id: str,
        config_references: tuple[str, ...],
        model_resolution: EffectiveModelIdentifier,
    ) -> dict[str, str]:
        environment: dict[str, str] = {}
        inherited_names = (
            WORKER_INHERITED_ENVIRONMENT_ALLOWLIST
            | MODEL_ENVIRONMENT_ALLOWLIST
            | set(config_references)
        )
        for name in inherited_names:
            value = self._parent_environment.get(name)
            if value is not None:
                environment[name] = value
        environment.update(case.execution.environment_overrides)
        apply_effective_model_to_worker_environment(environment, model_resolution)
        audit_import_root = Path(__file__).resolve().parents[2]
        environment.update(
            {
                "DB_PATH": str(sandbox.sqlite_path.resolve(strict=False)),
                "HERMES_HOME": str(sandbox.hermes_home.resolve(strict=True)),
                "HERMES_WORKSPACE": str(sandbox.workspace.resolve(strict=True)),
                "MYHERMES_AUDIT_ARTIFACTS_DIR": str(
                    sandbox.artifacts_dir.resolve(strict=True)
                ),
                "MYHERMES_AUDIT_TRIAL_ID": trial_id,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONSAFEPATH": "1",
                "PYTHONUTF8": "1",
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(self.subject_repo),
                        str(audit_import_root),
                    )
                ),
            }
        )
        return environment

    def _start_worker(
        self,
        request: MyHermesWorkerRequest,
        environment: dict[str, str],
    ):
        command = [
            sys.executable,
            "-P",
            "-m",
            "myhermes_audit.integrations.myhermes.worker",
            "--request",
            str(request.artifact_paths.worker_request),
            "--result",
            str(request.artifact_paths.worker_result),
        ]
        kwargs: dict = {
            "cwd": str(request.workspace),
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise WorkerProcessError("cannot start MyHermes worker") from exc
        try:
            stdout_capture = _start_capture(process.stdout)
            stderr_capture = _start_capture(process.stderr)
        except Exception as capture_exc:
            cleanup_error: Exception | None = None
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as cleanup_exc:
                cleanup_error = cleanup_exc
            finally:
                _close_pipe(process.stdout)
                _close_pipe(process.stderr)
            if cleanup_error is not None:
                raise WorkerProcessError(
                    "worker capture failed and process cleanup was incomplete"
                ) from cleanup_error
            raise WorkerProcessError("cannot start worker output capture") from capture_exc
        return process, stdout_capture, stderr_capture

    def _terminate_worker(self, process: subprocess.Popen) -> None:
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            elif os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError, ValueError):
            pass
        leader_exited = False
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            leader_exited = True
        except subprocess.TimeoutExpired:
            pass
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise WorkerProcessError(
                    "cannot force-kill the POSIX worker process group"
                ) from exc
        elif not leader_exited:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise WorkerProcessError("worker process group did not terminate") from exc

    def _outcome_from_result(
        self,
        result: MyHermesWorkerResult,
        paths: WorkerArtifactPaths,
        *,
        status: RunnerStatus,
        observations: ObservationBundle | None = None,
        subject_model: str | None = None,
        include_runtime: bool = True,
    ) -> TrialRunnerOutcome:
        result, _ = _redact_worker_content(
            result,
            None,
            self._sensitive_values,
        )
        tool_calls = (
            None
            if observations is None
            else tuple(
                ToolTraceEntry(
                    tool_call_id=item.tool_call_id,
                    tool_name=item.tool_name,
                    status=item.status,
                    success=item.success,
                    error_type=item.error_type,
                    duration_ms=item.duration_ms,
                )
                for item in observations.tool_calls
            )
        )
        return TrialRunnerOutcome(
            status=status,
            runtime_status=result.runtime_status,
            duration_ms=result.duration_ms,
            final_output=result.final_output,
            turns=tuple(result.turns),
            runtime=(
                TrialRuntimeSummary(
                    subject_model=subject_model,
                    iterations=result.iterations,
                    tool_batches=result.tool_batches,
                    tool_call_count=result.tool_call_count,
                    model_call_count=result.model_call_count,
                    tool_names=result.tool_names,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
                    prompt_cache_hit_tokens=result.prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=result.prompt_cache_miss_tokens,
                    deepseek_cache_evaluated_prompt_tokens=(
                        result.deepseek_cache_evaluated_prompt_tokens
                    ),
                    deepseek_cache_hit_rate=result.deepseek_cache_hit_rate,
                    deepseek_cache_status=result.deepseek_cache_status,
                    deepseek_cache_evaluated_model_call_count=(
                        result.deepseek_cache_evaluated_model_call_count
                    ),
                    deepseek_cache_invalid_model_call_count=(
                        result.deepseek_cache_invalid_model_call_count
                    ),
                )
                if include_runtime
                else None
            ),
            observations=_local_observations(observations),
            memory_query_results=tuple(result.memory_query_results),
            memory_snapshots=tuple(result.memory_snapshots),
            memory_state_changes=tuple(result.memory_state_changes),
            memory_errors=tuple(result.memory_errors),
            variant_id=result.variant_id,
            effective_subject_configuration=(
                result.effective_subject_configuration
            ),
            compression_events=tuple(result.compression_events),
            context_diagnostics=tuple(result.context_diagnostics),
            fact_context_observations=tuple(
                result.fact_context_observations
            ),
            background_review_results=tuple(result.background_review_results),
            background_review_errors=tuple(result.background_review_errors),
            scenario_results=tuple(result.scenario_results),
            process_errors=tuple(result.process_errors),
            review_gate_passed=result.review_gate_passed,
            tool_calls=tool_calls,
            tool_trace_complete=(
                status is RunnerStatus.COMPLETED
                and observations is not None
                and not observations.truncated
            ),
            artifact_paths=_existing_artifact_paths(paths),
            error_type=result.error_type,
            error_message=(None if result.error is None else result.error.message),
            retryable=(False if result.error is None else result.retryable),
            warnings=tuple(
                TrialWarning(
                    warning_type=item.warning_type,
                    message=item.message,
                )
                for item in result.warnings
            ),
        )


def _existing_artifact_paths(paths: WorkerArtifactPaths) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for field_name in type(paths).model_fields:
        if field_name == "schema_version":
            continue
        value = getattr(paths, field_name)
        if value is None:
            continue
        if isinstance(value, list):
            for index, path in enumerate(value):
                if path.exists():
                    result[f"{field_name}_{index}"] = path
        elif value.exists():
            result[field_name] = value
    return result


def _local_observations(
    observations: ObservationBundle | None,
) -> TrialObservationSummary | None:
    if observations is None:
        return None
    return TrialObservationSummary(
        worker_protocol_version=WORKER_PROTOCOL_VERSION,
        runs=[
            RunObservationSummary(
                run_id=item.run_id,
                parent_run_id=item.parent_run_id,
                status=item.status,
                stop_reason=item.stop_reason,
                iterations=item.iterations,
                tool_call_count=item.tool_call_count,
                has_final_reply=item.has_final_reply,
                duration_ms=item.duration_ms,
            )
            for item in observations.runs
        ],
        model_calls=[
            ModelObservationSummary(
                run_id=item.run_id,
                parent_run_id=item.parent_run_id,
                finish_reason=item.finish_reason,
                prompt_tokens=item.prompt_tokens,
                completion_tokens=item.completion_tokens,
                total_tokens=item.total_tokens,
                prompt_cache_hit_tokens=item.prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=item.prompt_cache_miss_tokens,
                duration_ms=item.duration_ms,
                tool_call_count=item.tool_call_count,
                error_category=item.error_category,
            )
            for item in observations.model_calls
        ],
        tool_calls=[
            ToolObservationSummary(
                run_id=item.run_id,
                parent_run_id=item.parent_run_id,
                tool_call_id=item.tool_call_id,
                tool_name=item.tool_name,
                status=item.status,
                success=item.success,
                error_type=item.error_type,
                duration_ms=item.duration_ms,
            )
            for item in observations.tool_calls
        ],
        truncated=observations.truncated,
        cache_invalid_model_call_count=observations.cache_invalid_model_call_count,
        deepseek_cache_evaluated_prompt_tokens=(
            observations.deepseek_cache_evaluated_prompt_tokens
        ),
    )


def _redact_worker_content(
    result: MyHermesWorkerResult,
    transcript: WorkerTranscript | None,
    sensitive_values: tuple[str, ...],
) -> tuple[MyHermesWorkerResult, WorkerTranscript | None]:
    def safe_turn(turn):
        return turn.model_copy(
            update={
                "user_message": redact_text(
                    turn.user_message,
                    sensitive_values,
                ),
                "final_output": (
                    None
                    if turn.final_output is None
                    else redact_text(turn.final_output, sensitive_values)
                ),
            }
        )

    safe_turns = [safe_turn(turn) for turn in result.turns]
    safe_error = (
        None
        if result.error is None
        else result.error.model_copy(
            update={
                "message": redact_text(result.error.message, sensitive_values),
            }
        )
    )
    safe_warnings = [
        warning.model_copy(
            update={
                "message": redact_text(warning.message, sensitive_values),
            }
        )
        for warning in result.warnings
    ]
    safe_result = result.model_copy(
        update={
            "final_output": (
                None
                if result.final_output is None
                else redact_text(result.final_output, sensitive_values)
            ),
            "turns": safe_turns,
            "error": safe_error,
            "warnings": safe_warnings,
        }
    )
    safe_transcript = (
        None
        if transcript is None
        else transcript.model_copy(
            update={"turns": [safe_turn(turn) for turn in transcript.turns]}
        )
    )
    return safe_result, safe_transcript


def _redact_memory_facts(
    result: MyHermesWorkerResult,
    artifact: MemoryArtifact | None,
    sensitive_values: tuple[str, ...],
) -> tuple[MyHermesWorkerResult, MemoryArtifact | None]:
    def safe_item(item):
        safe_content = redact_text(item.content, sensitive_values)
        metadata = _redact_json_value(item.metadata, sensitive_values)
        if safe_content != item.content:
            metadata = {
                **metadata,
                "local_content_redacted": True,
                "original_content_sha256": hashlib.sha256(
                    item.content.encode("utf-8")
                ).hexdigest(),
            }
        return item.model_copy(
            update={
                "content": safe_content,
                "metadata": metadata,
            }
        )

    safe_queries = []
    for query_result in result.memory_query_results:
        query = query_result.query.model_copy(
            update={
                "query": redact_text(query_result.query.query, sensitive_values),
                "filters": _redact_json_value(
                    query_result.query.filters,
                    sensitive_values,
                ),
            }
        )
        safe_queries.append(
            query_result.model_copy(
                update={
                    "query": query,
                    "items": [
                        item.model_copy(
                            update={
                                "content": redact_text(
                                    item.content,
                                    sensitive_values,
                                ),
                                "metadata": _redact_json_value(
                                    item.metadata,
                                    sensitive_values,
                                ),
                            }
                        )
                        for item in query_result.items
                    ],
                    "metadata": _redact_json_value(
                        query_result.metadata,
                        sensitive_values,
                    ),
                }
            )
        )
    safe_snapshots = [
        snapshot.model_copy(
            update={
                "items": [safe_item(item) for item in snapshot.items],
                "metadata": _redact_json_value(
                    snapshot.metadata,
                    sensitive_values,
                ),
            }
        )
        for snapshot in result.memory_snapshots
    ]
    safe_changes = [
        change.model_copy(
            update={
                "before": None if change.before is None else safe_item(change.before),
                "after": None if change.after is None else safe_item(change.after),
                "metadata": _redact_json_value(
                    change.metadata,
                    sensitive_values,
                ),
            }
        )
        for change in result.memory_state_changes
    ]
    safe_errors = [
        item.model_copy(
            update={
                "message": redact_text(item.message, sensitive_values),
                "details": _redact_json_value(item.details, sensitive_values),
            }
        )
        for item in result.memory_errors
    ]
    safe_result = result.model_copy(
        update={
            "memory_query_results": safe_queries,
            "memory_snapshots": safe_snapshots,
            "memory_state_changes": safe_changes,
            "memory_errors": safe_errors,
        }
    )
    safe_artifact = (
        None
        if artifact is None
        else artifact.model_copy(
            update={
                "query_results": safe_queries,
                "snapshots": safe_snapshots,
                "state_changes": safe_changes,
                "errors": safe_errors,
            }
        )
    )
    return safe_result, safe_artifact


def _redact_ablation_facts(
    result: MyHermesWorkerResult,
    artifact: AblationArtifact | None,
) -> tuple[MyHermesWorkerResult, AblationArtifact | None]:
    """Keep P4 protocol facts content-free even if a Subject exposes values."""

    def safe_observation(item):
        return item.model_copy(
            update={
                "matched_projection": (
                    None
                    if item.matched_projection is None
                    else item.matched_projection.model_copy(
                        update={"value": None}
                    )
                ),
                "distortion_projection": (
                    None
                    if item.distortion_projection is None
                    else item.distortion_projection.model_copy(
                        update={"value": None}
                    )
                ),
            }
        )

    safe_observations = [
        safe_observation(item) for item in result.fact_context_observations
    ]
    safe_result = result.model_copy(
        update={"fact_context_observations": safe_observations}
    )
    safe_artifact = (
        None
        if artifact is None
        else artifact.model_copy(
            update={"fact_context_observations": safe_observations}
        )
    )
    return safe_result, safe_artifact


def _redact_background_review_facts(
    result: MyHermesWorkerResult,
    _sensitive_values: tuple[str, ...],
) -> MyHermesWorkerResult:
    """Keep P5 result/artifact facts safe for local retention and replay.

    Worker-produced P5 evidence and snapshots use dedicated content-free
    contracts.  The parent therefore preserves their hashes, relationships,
    and state revisions as-is, while replacing every remaining free-form
    outcome or diagnostic string before it regenerates the three artifacts.
    """
    def safe_error(item):
        return item.model_copy(
            update={"message": "Background Review execution diagnostic"}
        )

    safe_results: list[BackgroundReviewExecutionResult] = []
    for item in result.background_review_results:
        outcome = item.outcome
        if outcome is not None:
            outcome_error = (
                None
                if outcome.error is None
                else outcome.error.model_copy(
                    update={
                        "message": "Background Review execution diagnostic"
                    }
                )
            )
            outcome = outcome.model_copy(
                update={
                    "changes": [
                        change.model_copy(
                            update={"reason": "observed_live_state_change"}
                        )
                        for change in outcome.changes
                    ],
                    "no_op_reason": (
                        None
                        if outcome.no_op_reason is None
                        else "review_no_op"
                    ),
                    "error": outcome_error,
                    "metadata": {},
                }
            )
        safe_results.append(
            item.model_copy(
                update={
                    "outcome": outcome,
                    "errors": [safe_error(value) for value in item.errors],
                }
            )
        )
    return result.model_copy(
        update={
            "background_review_results": safe_results,
            "background_review_errors": [
                safe_error(value) for value in result.background_review_errors
            ],
        }
    )


def _write_background_review_artifacts(
    paths: WorkerArtifactPaths,
    *,
    trial_id: str,
    case_id: str,
    results: Sequence[BackgroundReviewExecutionResult],
    errors: Sequence[BackgroundReviewExecutionError],
) -> None:
    """Rewrite all P5 projections from the same redacted parent fact set."""

    artifact_paths = (
        paths.background_review_results,
        paths.background_review_evidence,
        paths.background_review_snapshots,
    )
    if all(path is None for path in artifact_paths):
        return
    if any(path is None for path in artifact_paths):
        raise WorkerProtocolError("P5 worker Artifact paths are incomplete")
    results_path, evidence_path, snapshots_path = artifact_paths
    assert results_path is not None
    assert evidence_path is not None
    assert snapshots_path is not None
    result_values = list(results)
    error_values = list(errors)
    atomic_write_json(
        results_path,
        BackgroundReviewArtifact(
            trial_id=trial_id,
            case_id=case_id,
            results=result_values,
            errors=error_values,
        ),
    )
    atomic_write_json(
        evidence_path,
        BackgroundReviewEvidenceArtifact(
            trial_id=trial_id,
            case_id=case_id,
            results=result_values,
        ),
    )
    atomic_write_json(
        snapshots_path,
        BackgroundReviewSnapshotsArtifact(
            trial_id=trial_id,
            case_id=case_id,
            results=result_values,
        ),
    )


def _redact_json_value(value, sensitive_values: tuple[str, ...]):
    if isinstance(value, str):
        return redact_text(value, sensitive_values)
    if isinstance(value, list):
        return [_redact_json_value(item, sensitive_values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redact_json_value(
                item,
                sensitive_values,
            )
            for key, item in value.items()
        }
    return value


def _case_turns(
    case: AuditCase,
    *,
    configuration=None,
    variant_id: str | None = None,
) -> list[WorkerTurn]:
    if case.mode is CaseMode.SINGLE_TURN:
        if case.input.message is None:
            raise UnsupportedCaseError("single_turn case has no input message")
        turns = [
            WorkerTurn(
                message=case.input.message,
                session_id=case.input.session_id,
            )
        ]
    else:
        turns = [
            WorkerTurn(message=turn.message, session_id=turn.session_id)
            for turn in case.input.turns
        ]
    if configuration is None:
        return turns
    if variant_id is None:
        raise AblationVariantError("P4 turns require variant_id")
    variant_digest = hashlib.sha256(variant_id.encode("utf-8")).hexdigest()[:16]
    if configuration.session_context_mode.value == "subject_session":
        return [
            turn
            if turn.session_id is None
            else turn.model_copy(
                update={
                    "session_id": (
                        f"session-{variant_digest}-"
                        + hashlib.sha256(
                            turn.session_id.encode("utf-8")
                        ).hexdigest()[:16]
                    )
                }
            )
            for turn in turns
        ]
    return [
        turn.model_copy(
            update={"session_id": f"session-{variant_digest}-turn-{index}"}
        )
        for index, turn in enumerate(turns, start=1)
    ]


def _is_memory_case(case: AuditCase) -> bool:
    if case.ablation is not None:
        return any(
            variant.memory_mode
            in {MemoryMode.LONG_TERM_ONLY, MemoryMode.SHORT_AND_LONG_TERM}
            for variant in case.ablation.variants
        )
    return any(
        (
            case.execution.memory_strategy is not None,
            ToolsetName.MEMORY in case.execution.enabled_toolsets,
            case.fixture.memory is not None,
            bool(case.expected.memories),
            bool(case.expected.memory_states),
            any(
                evaluator.kind is EvaluatorKind.RETRIEVAL
                for evaluator in case.evaluators
            ),
        )
    )


def _is_background_review_case(case: AuditCase) -> bool:
    return bool(case.fixture.background_review_plans)


_SKILL_READ_CAPABILITIES = (
    "skill_read_toolset",
    "skill_view_tool",
    "skills_list_tool",
    "skill_read_tool_registration",
)

_PROCESS_SCENARIO_CAPABILITIES = (
    "process_toolset_actions",
    "process_start_via_terminal",
    "process_log",
    "process_poll",
    "process_write",
    "process_submit",
    "process_wait",
    "process_interrupt",
    "process_kill",
    "process_close",
    "background_process_supported",
)

_SCENARIO_TOOLSET_CAPABILITIES = {
    "file": ("file_tool_declaration",),
    "terminal": ("terminal_tool_declaration",),
    "memory": ("memory_tool",),
    "skill_read": _SKILL_READ_CAPABILITIES,
}

_FOREGROUND_TOOLSET_CAPABILITIES = (
    (ToolsetName.FILE.value, ("file_tool_declaration",)),
    (ToolsetName.TERMINAL.value, ("terminal_tool_declaration",)),
    (ToolsetName.MEMORY.value, ("memory_tool",)),
    (ToolsetName.SKILL_READ.value, _SKILL_READ_CAPABILITIES),
)


def _capability_available(report: SubjectCapabilityReport, name: str) -> bool:
    capability = report.capability(name)
    return capability is not None and capability.available


_PROCESS_STATUS_CANDIDATES = {
    "completed": ("completed", "exited"),
    "failed": ("failed", "failed_start", "lost"),
    "interrupted": ("interrupted",),
    "timed_out": ("timed_out",),
}


def _status_candidates(status: object) -> tuple[str, ...]:
    value = getattr(status, "value", status)
    if not isinstance(value, str) or not value:
        return ()
    return tuple(
        dict.fromkeys(
            (value, *_PROCESS_STATUS_CANDIDATES.get(value, ()))
        )
    )


def _required_process_status_requests(scenario: object) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    for checkpoint in getattr(scenario, "checkpoints", ()):
        if not getattr(checkpoint, "required", False):
            continue
        expected = getattr(checkpoint, "expected_process_status", None)
        if expected is not None:
            value = getattr(expected, "value", expected)
            if isinstance(value, str) and value:
                requests.append((f"checkpoint:{checkpoint.checkpoint_id}", value))
    for step in getattr(scenario, "steps", ()):
        if not getattr(step, "required", False):
            continue
        for field_name in (
            "expected_initial_status",
            "expected_status",
            "expected_terminal_status",
        ):
            expected = getattr(step, field_name, None)
            if expected is None:
                continue
            value = getattr(expected, "value", expected)
            if isinstance(value, str) and value:
                requests.append((f"step:{step.step_id}", value))
            break
    return requests


def _preflight_process_statuses(
    *,
    case_id: str,
    scenario: object,
    report: SubjectCapabilityReport,
) -> None:
    supported = set(report.supported_process_statuses)
    for location, requested in _required_process_status_requests(scenario):
        if supported.intersection(_status_candidates(requested)):
            continue
        supported_display = ",".join(report.supported_process_statuses) or "<none>"
        raise SubjectCapabilityError(
            (
                f"case={case_id}, scenario={scenario.scenario_id}, "
                f"location={location}: requested_process_status={requested}; "
                f"supported_process_statuses={supported_display}; "
                "missing public capability=supported_process_statuses"
            ),
            case_id=case_id,
            scenario_id=scenario.scenario_id,
            step_id=(
                location.removeprefix("step:")
                if location.startswith("step:")
                else None
            ),
            checkpoint_id=(
                location.removeprefix("checkpoint:")
                if location.startswith("checkpoint:")
                else None
            ),
            requested_process_status=requested,
            supported_process_statuses=list(report.supported_process_statuses),
            missing_capability="supported_process_statuses",
            capability_name="process_status_enum",
        )


def _supported_foreground_toolsets(
    report: SubjectCapabilityReport,
) -> list[str]:
    return [
        toolset
        for toolset, capabilities in _FOREGROUND_TOOLSET_CAPABILITIES
        if all(_capability_available(report, name) for name in capabilities)
    ]


def _compression_capability_summary(
    report: SubjectCapabilityReport,
) -> dict[str, bool]:
    return {
        "compression_threshold_control": report.compression_threshold_control,
        "compression_threshold_configuration": (
            report.compression_threshold_configuration
        ),
        "emergency_compression_disable": (
            report.emergency_overflow_compression_disable_supported
        ),
        "compression_observation": report.compression_observation_supported,
    }


def _worker_artifact_paths(
    sandbox: AuditSandbox,
    *,
    memory_enabled: bool,
    ablation_enabled: bool,
    background_review_enabled: bool,
    scenarios=(),
) -> WorkerArtifactPaths:
    root = sandbox.artifacts_dir.resolve(strict=True)
    return WorkerArtifactPaths(
        worker_request=root / "worker-request.json",
        worker_result=root / "worker-result.json",
        transcript=root / "transcript.json",
        observations=root / "observations.json",
        validator_results=root / "validator-results.json",
        stdout_log=root / "worker.stdout.log",
        stderr_log=root / "worker.stderr.log",
        memory=(root / "memory.json" if memory_enabled else None),
        ablation=(root / "ablation.json" if ablation_enabled else None),
        background_review_results=(
            root / "background-review-results.json"
            if background_review_enabled
            else None
        ),
        background_review_evidence=(
            root / "background-review-evidence.json"
            if background_review_enabled
            else None
        ),
        background_review_snapshots=(
            root / "background-review-snapshots.json"
            if background_review_enabled
            else None
        ),
        toolchain_results=(
            root / "toolchain-results.json"
            if any(item.kind.value == "toolchain" for item in scenarios)
            else None
        ),
        process_scenario_results=(
            root / "process-scenario-results.json"
            if any(item.kind.value == "process_background" for item in scenarios)
            else None
        ),
        process_cleanup=(
            root / "process-cleanup.json"
            if any(item.kind.value == "process_background" for item in scenarios)
            else None
        ),
        process_output_logs=[
            root / f"process-output-{_scenario_log_component(item.scenario_id)}.log"
            for item in scenarios
            if item.kind.value == "process_background"
        ],
    )


def _scenario_log_component(scenario_id: str) -> str:
    """Keep the conventional name while making unusual IDs path-safe."""

    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", scenario_id):
        return scenario_id
    return hashlib.sha256(scenario_id.encode("utf-8")).hexdigest()[:16]


def _start_capture(stream):
    if stream is None:
        raise WorkerProcessError("worker output pipe is unavailable")
    capture = _BoundedByteCapture()
    thread = threading.Thread(target=capture.consume, args=(stream,), daemon=True)
    thread.start()
    return capture, thread


def _close_pipe(stream) -> None:
    if stream is not None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _finish_captures(
    process,
    stdout_capture,
    stderr_capture,
) -> tuple[str, str, bool]:
    pairs = (
        (stdout_capture, process.stdout),
        (stderr_capture, process.stderr),
    )
    for (_capture, thread), _stream in pairs:
        thread.join(timeout=5.0)
    capture_failed = any(
        capture.error_type is not None
        for (capture, _thread), _stream in pairs
    )
    inherited_pipe_detected = any(
        thread.is_alive() for (_capture, thread), _stream in pairs
    )
    for _capture, stream in pairs:
        _close_pipe(stream)
    for (_capture, thread), _stream in pairs:
        if thread.is_alive():
            thread.join(timeout=1.0)
    for (_capture, thread), _stream in pairs:
        if thread.is_alive():
            raise WorkerProcessError("worker output capture did not terminate")
    if capture_failed:
        raise WorkerProcessError("worker output capture failed")
    return (
        stdout_capture[0].render(),
        stderr_capture[0].render(),
        inherited_pipe_detected,
    )


def _read_protocol_model(path: Path, model_type):
    if not path.is_file() or path.is_symlink():
        raise WorkerProtocolError("worker protocol artifact is missing")
    try:
        stat = path.stat()
        if stat.st_size > _MAX_PROTOCOL_BYTES:
            raise WorkerProtocolError("worker protocol artifact exceeds size limit")
        protocol_text = path.read_text(encoding="utf-8")
        json.loads(
            protocol_text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        return model_type.model_validate_json(protocol_text)
    except WorkerProtocolError:
        raise
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise WorkerProtocolError("worker protocol artifact is invalid") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _validate_worker_artifacts(
    request: MyHermesWorkerRequest,
    result: MyHermesWorkerResult,
    transcript: WorkerTranscript,
    observations: ObservationBundle,
    memory_artifact: MemoryArtifact | None,
    ablation_artifact: AblationArtifact | None,
    review_artifact: BackgroundReviewArtifact | None,
    review_evidence_artifact: BackgroundReviewEvidenceArtifact | None,
    review_snapshots_artifact: BackgroundReviewSnapshotsArtifact | None,
    toolchain_artifact: ToolchainScenarioArtifact | None,
    process_artifact: ProcessScenarioArtifact | None,
    process_cleanup_artifact: ProcessCleanupArtifact | None,
    *,
    returncode: int,
) -> None:
    protocol_versions = {
        request.protocol_version,
        result.protocol_version,
        transcript.protocol_version,
        observations.protocol_version,
        *(
            []
            if memory_artifact is None
            else [memory_artifact.protocol_version]
        ),
        *(
            []
            if ablation_artifact is None
            else [ablation_artifact.protocol_version]
        ),
        *(
            []
            if review_artifact is None
            else [review_artifact.protocol_version]
        ),
        *(
            []
            if review_evidence_artifact is None
            else [review_evidence_artifact.protocol_version]
        ),
        *(
            []
            if review_snapshots_artifact is None
            else [review_snapshots_artifact.protocol_version]
        ),
        *( [] if toolchain_artifact is None else [toolchain_artifact.protocol_version] ),
        *( [] if process_artifact is None else [process_artifact.protocol_version] ),
        *( [] if process_cleanup_artifact is None else [process_cleanup_artifact.protocol_version] ),
    }
    if len(protocol_versions) != 1:
        raise WorkerProtocolError("worker artifact protocol versions do not match")
    scenario_results = [
        item
        for artifact in (toolchain_artifact, process_artifact)
        if artifact is not None
        for item in artifact.results
    ]
    expected_scenario_ids = {item.scenario_id for item in request.scenarios}
    observed_scenario_ids = {item.scenario_id for item in result.scenario_results}
    if observed_scenario_ids - expected_scenario_ids:
        raise WorkerProtocolError("worker returned an undeclared scenario")
    if sorted(item.scenario_id for item in scenario_results) != sorted(
        item.scenario_id for item in result.scenario_results
    ):
        raise WorkerProtocolError("worker scenario Artifact facts do not match result")
    for process_result in (
        item
        for item in result.scenario_results
        if item.kind is E2EScenarioKind.PROCESS_BACKGROUND
    ):
        if (
            process_result.hard_timeout_source is not request.hard_timeout_source
            or process_result.hard_timeout_seconds != request.hard_timeout_seconds
        ):
            raise WorkerProtocolError(
                "Process result watchdog facts do not match Runner disposition"
            )
        if request.process_watchdog_enabled and (
            process_result.scenario_id != request.hard_timeout_scenario_id
        ):
            raise WorkerProtocolError(
                "Process result Scenario does not match watchdog disposition"
            )
        if (
            not request.process_watchdog_enabled
            and process_result.hard_timeout_source
            is ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
        ):
            raise WorkerProtocolError(
                "disabled Process watchdog cannot appear in a result"
            )
    if toolchain_artifact is not None and (
        toolchain_artifact.trial_id != request.trial_id
        or toolchain_artifact.case_id != request.case_id
    ):
        raise WorkerProtocolError("Toolchain Artifact identity does not match request")
    if process_artifact is not None and (
        process_artifact.trial_id != request.trial_id
        or process_artifact.case_id != request.case_id
    ):
        raise WorkerProtocolError("Process Artifact identity does not match request")
    if process_cleanup_artifact is not None and (
        process_cleanup_artifact.trial_id != request.trial_id
        or process_cleanup_artifact.case_id != request.case_id
    ):
        raise WorkerProtocolError("Process cleanup Artifact identity does not match request")
    has_process_results = any(
        item.kind.value == "process_background"
        for item in result.scenario_results
    )
    if process_cleanup_artifact is not None and not has_process_results:
        raise WorkerProtocolError("empty worker result cannot return Process cleanup")
    if result.toolchain_results_artifact is not None and toolchain_artifact is None:
        raise WorkerProtocolError("worker result names a missing Toolchain Artifact")
    if result.process_scenario_results_artifact is not None and process_artifact is None:
        raise WorkerProtocolError("worker result names a missing Process Artifact")
    expected_toolchain_ref = (
        None
        if request.artifact_paths.toolchain_results is None
        else f"artifacts/{request.artifact_paths.toolchain_results.name}"
    )
    expected_process_ref = (
        None
        if request.artifact_paths.process_scenario_results is None
        else f"artifacts/{request.artifact_paths.process_scenario_results.name}"
    )
    expected_cleanup_ref = (
        None
        if request.artifact_paths.process_cleanup is None
        else f"artifacts/{request.artifact_paths.process_cleanup.name}"
    )
    if result.toolchain_results_artifact != (
        expected_toolchain_ref if toolchain_artifact is not None else None
    ):
        raise WorkerProtocolError("worker Toolchain Artifact reference is unexpected")
    if result.process_scenario_results_artifact != (
        expected_process_ref if process_artifact is not None else None
    ):
        raise WorkerProtocolError("worker Process Artifact reference is unexpected")
    if result.process_cleanup_artifact != (
        expected_cleanup_ref if has_process_results else None
    ):
        raise WorkerProtocolError("worker cleanup Artifact reference is unexpected")
    expected_process_count = sum(
        item.kind.value == "process_background" for item in result.scenario_results
    )
    if len(result.process_output_artifacts) != expected_process_count:
        raise WorkerProtocolError("worker Process output Artifact count does not match scenarios")
    expected_output_refs = [
        f"artifacts/{path.name}"
        for path in request.artifact_paths.process_output_logs[:expected_process_count]
    ]
    if result.process_output_artifacts != expected_output_refs:
        raise WorkerProtocolError("worker Process output Artifact references are unexpected")
    if result.worker_status is WorkerStatus.COMPLETED:
        for path in request.artifact_paths.process_output_logs[:expected_process_count]:
            if path.is_symlink() or not path.is_file():
                raise WorkerProtocolError("worker Process output Artifact is missing")
    if transcript.trial_id != request.trial_id or transcript.case_id != request.case_id:
        raise WorkerProtocolError("worker transcript identity does not match request")
    if transcript.turns != result.turns:
        raise WorkerProtocolError("worker transcript turns do not match result")
    observed_messages = [turn.user_message for turn in result.turns]
    requested_messages = [turn.message for turn in request.turns]
    if observed_messages != requested_messages[: len(observed_messages)]:
        raise WorkerProtocolError("worker transcript messages do not match request")
    observed_sessions = [turn.session_id for turn in result.turns]
    requested_sessions = [turn.session_id for turn in request.turns]
    if observed_sessions != requested_sessions[: len(observed_sessions)]:
        raise WorkerProtocolError("worker transcript sessions do not match request")
    if (
        result.worker_status is WorkerStatus.COMPLETED
        and len(result.turns) != len(request.turns)
    ):
        raise WorkerProtocolError("completed worker did not execute every requested turn")
    if result.observations_artifact != "artifacts/observations.json":
        raise WorkerProtocolError("worker result names an unexpected Observation artifact")
    if result.transcript_artifact != "artifacts/transcript.json":
        raise WorkerProtocolError("worker result names an unexpected transcript artifact")
    if request.memory_strategy is None:
        if memory_artifact is not None or result.memory_artifact is not None:
            raise MemoryProtocolError("non-Memory worker returned a Memory Artifact")
        if any(
            (
                result.memory_query_results,
                result.memory_snapshots,
                result.memory_state_changes,
                result.memory_errors,
            )
        ):
            raise MemoryProtocolError("non-Memory worker returned Memory facts")
    else:
        if memory_artifact is None:
            raise MemoryProtocolError("Memory worker did not return a Memory Artifact")
        if result.memory_artifact != "artifacts/memory.json":
            raise MemoryProtocolError("worker result names an unexpected Memory Artifact")
        if (
            memory_artifact.trial_id != request.trial_id
            or memory_artifact.case_id != request.case_id
            or memory_artifact.strategy is not request.memory_strategy
        ):
            raise MemoryProtocolError("Memory Artifact identity does not match request")
        if (
            memory_artifact.query_results != result.memory_query_results
            or memory_artifact.snapshots != result.memory_snapshots
            or memory_artifact.state_changes != result.memory_state_changes
            or memory_artifact.errors != result.memory_errors
        ):
            raise MemoryProtocolError("Memory Artifact facts do not match worker result")
        plans = {item.query_id: item for item in request.memory_queries}
        for query_result in result.memory_query_results:
            plan = plans.get(query_result.query_id)
            if (
                plan is None
                or query_result.query != plan.query
                or query_result.phase is not plan.phase
                or query_result.strategy is not request.memory_strategy
            ):
                raise MemoryProtocolError(
                    "Memory query result does not match its declared plan"
                )
        if result.worker_status is WorkerStatus.COMPLETED:
            covered_query_ids = {
                item.query_id for item in result.memory_query_results
            } | {
                item.query_id
                for item in result.memory_errors
                if item.query_id is not None
            }
            if covered_query_ids != set(plans):
                raise MemoryProtocolError(
                    "completed Memory worker has incomplete query coverage"
                )
        if any(
            item.strategy is not request.memory_strategy
            for item in result.memory_snapshots
        ):
            raise MemoryProtocolError(
                "Memory snapshot strategy does not match request"
            )
    if request.effective_subject_configuration is None:
        if ablation_artifact is not None or result.ablation_artifact is not None:
            raise WorkerProtocolError(
                "non-P4 worker returned an Ablation Artifact"
            )
        if any(
            (
                result.compression_events,
                result.context_diagnostics,
                result.fact_context_observations,
            )
        ):
            raise WorkerProtocolError("non-P4 worker returned P4 facts")
    else:
        if ablation_artifact is None:
            raise WorkerProtocolError("P4 worker did not return an Ablation Artifact")
        if result.ablation_artifact != "artifacts/ablation.json":
            raise WorkerProtocolError(
                "worker result names an unexpected Ablation Artifact"
            )
        if (
            ablation_artifact.trial_id != request.trial_id
            or ablation_artifact.case_id != request.case_id
            or ablation_artifact.variant_id != request.variant_id
            or ablation_artifact.effective_subject_configuration
            != request.effective_subject_configuration
        ):
            raise WorkerProtocolError(
                "Ablation Artifact identity does not match request"
            )
        if (
            result.variant_id != request.variant_id
            or result.effective_subject_configuration
            != request.effective_subject_configuration
        ):
            raise WorkerProtocolError("worker P4 identity does not match request")
        if (
            ablation_artifact.compression_events != result.compression_events
            or ablation_artifact.context_diagnostics
            != result.context_diagnostics
            or ablation_artifact.fact_context_observations
            != result.fact_context_observations
        ):
            raise WorkerProtocolError(
                "Ablation Artifact facts do not match worker result"
            )
    if not request.background_review_plans:
        if any(
            item is not None
            for item in (
                review_artifact,
                review_evidence_artifact,
                review_snapshots_artifact,
                result.background_review_results_artifact,
                result.background_review_evidence_artifact,
                result.background_review_snapshots_artifact,
            )
        ) or result.background_review_results or result.background_review_errors:
            raise WorkerProtocolError("non-P5 worker returned Background Review facts")
    else:
        if any(
            item is None
            for item in (
                review_artifact,
                review_evidence_artifact,
                review_snapshots_artifact,
            )
        ):
            raise WorkerProtocolError("P5 worker did not return all Review Artifacts")
        if (
            result.background_review_results_artifact
            != "artifacts/background-review-results.json"
            or result.background_review_evidence_artifact
            != "artifacts/background-review-evidence.json"
            or result.background_review_snapshots_artifact
            != "artifacts/background-review-snapshots.json"
        ):
            raise WorkerProtocolError("worker result names unexpected Review Artifacts")
        assert review_artifact is not None
        assert review_evidence_artifact is not None
        assert review_snapshots_artifact is not None
        if any(
            artifact.trial_id != request.trial_id
            or artifact.case_id != request.case_id
            for artifact in (
                review_artifact,
                review_evidence_artifact,
                review_snapshots_artifact,
            )
        ):
            raise WorkerProtocolError("Background Review Artifact identity does not match request")
        planned = {item.review_id: item for item in request.background_review_plans}
        result_ids = {item.review_id for item in result.background_review_results}
        if result_ids != set(planned):
            raise WorkerProtocolError("Background Review result coverage does not match plans")
        if review_artifact.results != result.background_review_results or (
            review_artifact.errors != result.background_review_errors
        ):
            raise WorkerProtocolError("Background Review result Artifact facts do not match worker result")
        if review_evidence_artifact.results != result.background_review_results or (
            review_snapshots_artifact.results != result.background_review_results
        ):
            raise WorkerProtocolError("Background Review evidence/snapshot Artifacts do not match result")
        for item in result.background_review_results:
            plan = planned[item.review_id]
            if item.kind is not plan.kind or item.lifecycle is not plan.lifecycle:
                raise WorkerProtocolError("Background Review result does not match plan")
    if result.worker_status is WorkerStatus.COMPLETED and returncode != 0:
        raise WorkerProtocolError(
            "worker returned a completed envelope with non-zero exit status"
        )
    if result.worker_status is WorkerStatus.FAILED and returncode == 0:
        raise WorkerProtocolError(
            "worker returned a failed envelope with zero exit status"
        )
    known_run_ids = set(result.run_ids)
    observed_run_ids = {
        item.run_id
        for items in (observations.runs, observations.model_calls, observations.tool_calls)
        for item in items
    }
    if not observed_run_ids.issubset(known_run_ids):
        raise WorkerProtocolError("Observation run IDs do not match worker result")
    run_observation_ids = {item.run_id for item in observations.runs}
    if not observations.truncated and run_observation_ids != known_run_ids:
        raise WorkerProtocolError("run Observation coverage is incomplete")
    observed_tool_names = list(
        dict.fromkeys(item.tool_name for item in observations.tool_calls)
    )
    if result.tool_names != observed_tool_names:
        raise WorkerProtocolError("worker tool names do not match Observations")


def _fallback_background_review_results(
    plans: Sequence[BackgroundReviewPlan],
    *,
    error_type: str,
    message: str,
) -> list[BackgroundReviewExecutionResult]:
    """Produce complete, side-effect-free P5 facts for parent fallbacks.

    A timeout or worker-envelope failure must not silently erase a declared
    Review Plan.  These facts say only that no executable claim lifecycle was
    completed; they never infer a Subject decision or mutate Subject state.
    """

    results: list[BackgroundReviewExecutionResult] = []
    for plan in plans:
        execution_error = BackgroundReviewExecutionError(
            error_type=error_type,
            stage="parent_fallback",
            message=message,
            retryable=False,
        )
        attempts = [
            ReviewAttempt(
                sequence=1,
                claim_valid=False,
                loop_executed=False,
                model_call_count=0,
                tool_call_count=0,
                state_change_count=0,
                error_type=error_type,
            )
        ]
        if plan.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE:
            attempts.append(
                ReviewAttempt(
                    sequence=2,
                    claim_valid=False,
                    loop_executed=False,
                    model_call_count=0,
                    tool_call_count=0,
                    state_change_count=0,
                    error_type=error_type,
                )
            )
        results.append(
            BackgroundReviewExecutionResult(
                review_id=plan.review_id,
                kind=plan.kind,
                lifecycle=plan.lifecycle,
                status=ReviewStatus.FAILED,
                actual_action=ReviewAction.NO_OP,
                outcome=ReviewOutcome(
                    review_id=plan.review_id,
                    kind=plan.kind,
                    status=ReviewStatus.FAILED,
                    error=ReviewError(error_type=error_type, message=message),
                ),
                attempts=attempts,
                attempt_count=len(attempts),
                duplicate_rejected=(
                    plan.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE
                ),
                duration_ms=0,
                errors=[execution_error],
            )
        )
    return results


def _merge_recovered_background_review_results(
    plans: Sequence[BackgroundReviewPlan],
    recovered: Sequence[BackgroundReviewExecutionResult],
    *,
    error_type: str,
    message: str,
) -> list[BackgroundReviewExecutionResult]:
    """Retain verified checkpoint facts and fill only missing P5 plans."""

    recovered_by_id = {item.review_id: item for item in recovered}
    missing_plans = [plan for plan in plans if plan.review_id not in recovered_by_id]
    fallback_by_id = {
        item.review_id: item
        for item in _fallback_background_review_results(
            missing_plans,
            error_type=error_type,
            message=message,
        )
    }
    return [
        (
            recovered_by_id[plan.review_id]
            if plan.review_id in recovered_by_id
            else fallback_by_id[plan.review_id]
        )
        for plan in plans
    ]


def _merge_recovered_background_review_errors(
    recovered: Sequence[BackgroundReviewExecutionError],
    results: Sequence[BackgroundReviewExecutionResult],
) -> list[BackgroundReviewExecutionError]:
    """Preserve checkpoint-level diagnostics alongside per-plan errors."""

    merged = list(recovered)
    for result in results:
        for error in result.errors:
            if error not in merged:
                merged.append(error)
    return merged


def _fallback_scenario_results(
    scenarios: Sequence[object],
    *,
    duration_ms: int,
    error_type: str,
    timed_out: bool,
    hard_timeout_source: ProcessHardTimeoutSource = (
        ProcessHardTimeoutSource.TRIAL_WATCHDOG
    ),
    hard_timeout_seconds: int | None = None,
    trial_watchdog_timed_out: bool = False,
) -> tuple[list[object], list[ScenarioError]]:
    """Preserve declared P6.1 coverage when the Worker envelope is lost."""

    results: list[object] = []
    errors: list[ScenarioError] = []
    for plan in scenarios:
        error = ScenarioError(
            error_type=error_type.replace("_", "-"),
            message="Worker did not return a complete scenario observation",
        )
        if plan.kind is E2EScenarioKind.PROCESS_BACKGROUND:
            process_watchdog_active = (
                hard_timeout_source
                is ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
            )
            result = ProcessScenarioExecutionResult(
                scenario_id=plan.scenario_id,
                status=ScenarioStatus.FAILED,
                scenario_timeout_seconds=plan.timeout_seconds,
                hard_timeout_source=hard_timeout_source,
                hard_timeout_seconds=hard_timeout_seconds or plan.timeout_seconds,
                hard_timeout_triggered=timed_out,
                trial_watchdog_timed_out=(
                    trial_watchdog_timed_out
                    or (
                        timed_out
                        and hard_timeout_source
                        is ProcessHardTimeoutSource.TRIAL_WATCHDOG
                    )
                ),
                scenario_watchdog_timed_out=(
                    timed_out and process_watchdog_active
                ),
                duration_ms=None,
                errors=[error],
            )
        else:
            result = ToolchainScenarioExecutionResult(
                scenario_id=plan.scenario_id,
                status=ScenarioStatus.FAILED,
                duration_ms=duration_ms,
                errors=[error],
            )
        results.append(result)
        errors.append(error)
    return results, errors


def _fallback_worker_result(
    paths: WorkerArtifactPaths,
    *,
    error_type: str,
    message: str,
    duration_ms: int,
    warnings: Sequence[WorkerWarning] = (),
    memory_strategy: RetrievalStrategy | None = None,
    recovered_memory: MemoryArtifact | None = None,
    variant_id: str | None = None,
    configuration: EffectiveSubjectConfiguration | None = None,
    recovered_ablation: AblationArtifact | None = None,
    background_review_plans: Sequence[BackgroundReviewPlan] = (),
    recovered_background_review_results: Sequence[
        BackgroundReviewExecutionResult
    ] = (),
    recovered_background_review_errors: Sequence[
        BackgroundReviewExecutionError
    ] = (),
    scenarios: Sequence[object] = (),
    hard_timeout_source: ProcessHardTimeoutSource = (
        ProcessHardTimeoutSource.TRIAL_WATCHDOG
    ),
    hard_timeout_seconds: int | None = None,
    trial_watchdog_timed_out: bool = False,
) -> MyHermesWorkerResult:
    safe_error_type = error_type.replace("_", "-")
    protocol_errors = (
        []
        if memory_strategy is None
        else [
            MemoryOperationError(
                error_type=MemoryErrorType.PROTOCOL,
                operation="parent_fallback",
                message="Memory pipeline did not return a complete Worker envelope",
                details={"worker_error_type": error_type},
            )
        ]
    )
    memory_errors = [
        *([] if recovered_memory is None else recovered_memory.errors),
        *protocol_errors,
    ]
    background_review_results = _merge_recovered_background_review_results(
        background_review_plans,
        recovered_background_review_results,
        error_type="background_review_protocol_error",
        message="Background Review pipeline did not return a complete Worker envelope",
    )
    background_review_errors = _merge_recovered_background_review_errors(
        recovered_background_review_errors,
        background_review_results,
    )
    scenario_results, process_errors = _fallback_scenario_results(
        scenarios,
        duration_ms=duration_ms,
        error_type=error_type,
        timed_out=error_type == "timeout",
        hard_timeout_source=hard_timeout_source,
        hard_timeout_seconds=hard_timeout_seconds,
        trial_watchdog_timed_out=trial_watchdog_timed_out,
    )
    scenario_kinds = {item.kind.value for item in scenario_results}
    return MyHermesWorkerResult(
        worker_status=WorkerStatus.FAILED,
        runtime_status=error_type,
        error_type=safe_error_type,
        fatal=True,
        retryable=False,
        duration_ms=duration_ms,
        observations_artifact=f"artifacts/{paths.observations.name}",
        transcript_artifact=f"artifacts/{paths.transcript.name}",
        memory_artifact=(
            None
            if paths.memory is None
            else f"artifacts/{paths.memory.name}"
        ),
        memory_errors=memory_errors,
        memory_query_results=(
            [] if recovered_memory is None else recovered_memory.query_results
        ),
        memory_snapshots=(
            [] if recovered_memory is None else recovered_memory.snapshots
        ),
        memory_state_changes=(
            [] if recovered_memory is None else recovered_memory.state_changes
        ),
        variant_id=variant_id,
        effective_subject_configuration=configuration,
        ablation_artifact=(
            None
            if configuration is None
            else f"artifacts/{paths.ablation.name}"
        ),
        compression_events=(
            []
            if recovered_ablation is None
            else recovered_ablation.compression_events
        ),
        context_diagnostics=(
            []
            if recovered_ablation is None
            else recovered_ablation.context_diagnostics
        ),
        fact_context_observations=(
            []
            if recovered_ablation is None
            else recovered_ablation.fact_context_observations
        ),
        background_review_results_artifact=(
            None
            if not background_review_plans
            else f"artifacts/{paths.background_review_results.name}"
        ),
        background_review_evidence_artifact=(
            None
            if not background_review_plans
            else f"artifacts/{paths.background_review_evidence.name}"
        ),
        background_review_snapshots_artifact=(
            None
            if not background_review_plans
            else f"artifacts/{paths.background_review_snapshots.name}"
        ),
        background_review_results=background_review_results,
        background_review_errors=background_review_errors,
        scenario_results=scenario_results,
        process_errors=process_errors,
        toolchain_results_artifact=(
            None
            if "toolchain" not in scenario_kinds or paths.toolchain_results is None
            else f"artifacts/{paths.toolchain_results.name}"
        ),
        process_scenario_results_artifact=(
            None
            if "process_background" not in scenario_kinds
            or paths.process_scenario_results is None
            else f"artifacts/{paths.process_scenario_results.name}"
        ),
        process_cleanup_artifact=(
            None
            if "process_background" not in scenario_kinds or paths.process_cleanup is None
            else f"artifacts/{paths.process_cleanup.name}"
        ),
        warnings=list(warnings),
        error=WorkerError(error_type=safe_error_type, message=message),
    )


def _worker_warning(warning_type: str, error: Exception) -> WorkerWarning:
    return WorkerWarning(
        warning_type=warning_type,
        message=f"parent worker adapter warning: {type(error).__name__}",
    )


def _safe_traceback(error: Exception) -> str:
    frames = traceback.extract_tb(error.__traceback__, limit=50)
    return "".join(traceback.format_list(frames)) + f"{type(error).__name__}\n"


def _ensure_empty_worker_artifacts(
    paths: WorkerArtifactPaths,
    trial_id: str,
    case_id: str,
    *,
    memory_strategy: RetrievalStrategy | None,
    memory_errors: Sequence[MemoryOperationError],
    recovered_memory: MemoryArtifact | None,
    variant_id: str | None,
    configuration: EffectiveSubjectConfiguration | None,
    recovered_ablation: AblationArtifact | None,
    background_review_plans: Sequence[BackgroundReviewPlan] = (),
    background_review_results: Sequence[BackgroundReviewExecutionResult] = (),
    background_review_errors: Sequence[BackgroundReviewExecutionError] = (),
    scenarios: Sequence[object] = (),
    scenario_results: Sequence[object] = (),
) -> None:
    if not paths.observations.exists():
        atomic_write_json(paths.observations, ObservationBundle())
    if not paths.transcript.exists():
        atomic_write_json(
            paths.transcript,
            WorkerTranscript(trial_id=trial_id, case_id=case_id),
        )
    if memory_strategy is not None and paths.memory is not None:
        memory_artifact = recovered_memory or MemoryArtifact(
            trial_id=trial_id,
            case_id=case_id,
            strategy=memory_strategy,
            provider="unavailable",
        )
        atomic_write_json(
            paths.memory,
            memory_artifact.model_copy(
                update={"errors": list(memory_errors)}
            ),
        )
    if configuration is not None and variant_id is not None and paths.ablation is not None:
        atomic_write_json(
            paths.ablation,
            recovered_ablation
            or AblationArtifact(
                trial_id=trial_id,
                case_id=case_id,
                variant_id=variant_id,
                effective_subject_configuration=configuration,
            ),
        )
    if background_review_plans:
        if (
            paths.background_review_results is None
            or paths.background_review_evidence is None
            or paths.background_review_snapshots is None
        ):
            raise WorkerProtocolError("P5 fallback paths are incomplete")
        results = list(background_review_results) or _fallback_background_review_results(
            background_review_plans,
            error_type="background_review_protocol_error",
            message="Background Review pipeline did not return a complete Worker envelope",
        )
        errors = list(background_review_errors) or [
            error for item in results for error in item.errors
        ]
        atomic_write_json(
            paths.background_review_results,
            BackgroundReviewArtifact(
                trial_id=trial_id,
                case_id=case_id,
                results=results,
                errors=errors,
            ),
        )
        atomic_write_json(
            paths.background_review_evidence,
            BackgroundReviewEvidenceArtifact(
                trial_id=trial_id,
                case_id=case_id,
                results=results,
            ),
        )
        atomic_write_json(
            paths.background_review_snapshots,
            BackgroundReviewSnapshotsArtifact(
                trial_id=trial_id,
                case_id=case_id,
                results=results,
            ),
        )
    scenario_kinds = {item.kind.value for item in scenarios}
    result_items = list(scenario_results)
    if "toolchain" in scenario_kinds and paths.toolchain_results is not None:
        atomic_write_json(
            paths.toolchain_results,
            ToolchainScenarioArtifact(
                trial_id=trial_id,
                case_id=case_id,
                results=[item for item in result_items if item.kind is E2EScenarioKind.TOOLCHAIN],
            ),
        )
    if "process_background" in scenario_kinds:
        if paths.process_scenario_results is not None:
            atomic_write_json(
                paths.process_scenario_results,
                ProcessScenarioArtifact(
                    trial_id=trial_id,
                    case_id=case_id,
                    results=[item for item in result_items if item.kind is E2EScenarioKind.PROCESS_BACKGROUND],
                ),
            )
        if paths.process_cleanup is not None and not paths.process_cleanup.exists():
            atomic_write_json(
                paths.process_cleanup,
                ProcessCleanupArtifact(
                    trial_id=trial_id,
                    case_id=case_id,
                    reports=[],
                ),
            )


def _recover_parent_memory_artifact(
    paths: WorkerArtifactPaths,
    *,
    trial_id: str,
    case_id: str,
    strategy: RetrievalStrategy | None,
) -> MemoryArtifact | None:
    if strategy is None or paths.memory is None:
        return None
    try:
        artifact = _read_protocol_model(paths.memory, MemoryArtifact)
    except Exception:
        return None
    if (
        artifact.trial_id != trial_id
        or artifact.case_id != case_id
        or artifact.strategy is not strategy
    ):
        return None
    return artifact


def _recover_parent_ablation_artifact(
    paths: WorkerArtifactPaths,
    *,
    trial_id: str,
    case_id: str,
    variant_id: str | None,
    configuration: EffectiveSubjectConfiguration | None,
) -> AblationArtifact | None:
    if variant_id is None or configuration is None or paths.ablation is None:
        return None
    try:
        artifact = _read_protocol_model(paths.ablation, AblationArtifact)
    except Exception:
        return None
    if (
        artifact.trial_id != trial_id
        or artifact.case_id != case_id
        or artifact.variant_id != variant_id
        or artifact.effective_subject_configuration != configuration
    ):
        return None
    return artifact


def _recover_parent_background_review_results(
    paths: WorkerArtifactPaths,
    *,
    trial_id: str,
    case_id: str,
    plans: Sequence[BackgroundReviewPlan],
) -> tuple[
    list[BackgroundReviewExecutionResult],
    list[BackgroundReviewExecutionError],
]:
    """Recover a validated P5 result checkpoint after worker loss.

    The result Artifact contains the content-free evidence and snapshots for
    each plan.  It is sufficient for recovery even if termination interrupted
    publication of either derived Artifact; the fallback path rewrites all
    three projections from the recovered result set.
    """

    if not plans or paths.background_review_results is None:
        return [], []
    try:
        artifact = _read_protocol_model(
            paths.background_review_results,
            BackgroundReviewArtifact,
        )
    except Exception:
        return [], []
    if artifact.trial_id != trial_id or artifact.case_id != case_id:
        return [], []
    planned_by_id = {plan.review_id: plan for plan in plans}
    recovered_by_id = {item.review_id: item for item in artifact.results}
    if any(
        review_id not in planned_by_id
        or item.kind is not planned_by_id[review_id].kind
        or item.lifecycle is not planned_by_id[review_id].lifecycle
        for review_id, item in recovered_by_id.items()
    ):
        return [], []
    return (
        [
            recovered_by_id[plan.review_id]
            for plan in plans
            if plan.review_id in recovered_by_id
        ],
        list(artifact.errors),
    )


__all__ = ("MyHermesTrialRunner",)
