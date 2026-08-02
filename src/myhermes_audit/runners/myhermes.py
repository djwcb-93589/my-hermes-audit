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
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json, atomic_write_text
from myhermes_audit.contracts import (
    AblationVariant,
    AuditCase,
    CompressionMode,
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
    CompressionCapabilityError,
    CompressionConfigurationError,
    CompressionObservationError,
    MemoryCapabilityError,
    MemoryKindUnsupportedError,
    MemoryMappingError,
    MemoryProtocolError,
    MemoryScopeUnsupportedError,
    MemoryStrategyUnsupportedError,
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
from myhermes_audit.integrations.myhermes.contracts import (
    AblationArtifact,
    MemoryArtifact,
    MemoryQueryPlan,
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

    def p4_model_identifier(
        self,
        case: AuditCase,
        configuration: EffectiveSubjectConfiguration,
    ) -> str:
        prepared = self._config_builder.prepare(
            effective_config_overrides(case, configuration)
        )
        value = prepared.document.get("model")
        if isinstance(value, str) and value.strip():
            normalized = value.strip()
            if normalized == "${MODEL}":
                normalized = (
                    case.execution.environment_overrides.get("MODEL")
                    or self._parent_environment.get("MODEL")
                    or normalized
                )
            return redact_text(
                normalized,
                self._sensitive_values,
            )[:256]
        return "subject-default"

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
        if case.mode is CaseMode.SCRIPTED_MULTI_TURN and any(
            turn.role is not ConversationRole.USER for turn in case.input.turns
        ):
            raise UnsupportedCaseError(
                "P1 scripted turns must contain only user messages",
                case_id=case.case_id,
            )
        memory_case = _is_memory_case(case)
        validate_runtime_fixture_support(case.fixture, allow_memory=memory_case)
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
            }
        ]
        if unsupported_evaluators:
            raise UnsupportedCaseError(
                "case uses evaluators outside the implemented runtime boundary",
                case_id=case.case_id,
                evaluator_kinds=unsupported_evaluators,
            )
        if case.expected.background_reviews:
            raise UnsupportedCaseError(
                "case declares Background Review expectations outside P3",
                case_id=case.case_id,
            )
        if memory_case:
            self._preflight_memory_case(case)
        if case.ablation is not None:
            self._preflight_ablation_case(case)
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
        observation_capability = report.capability("compression_observation")
        observation_available = (
            observation_capability is not None
            and observation_capability.available
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
                    compression_mode=variant.compression_mode.value,
                    missing_capability="compression_toggle",
                )
            variant_expectations = applicable_fact_expectations(
                case,
                variant.variant_id,
            )
            if (
                variant.compression_mode is CompressionMode.ENABLED
                and any(
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
                    missing_capability="compression_observation",
                )
            configuration = effective_subject_configuration(
                case,
                variant,
                compression_observation_available=observation_available,
            )
            try:
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
            capability = report.capability("compression_observation")
            configuration = effective_subject_configuration(
                case,
                variant,
                compression_observation_available=(
                    capability is not None and capability.available
                ),
            )
        memory_case = (
            _is_memory_case(case)
            if configuration is None
            else configuration.include_memory
        )
        memory_strategy = (
            case.execution.memory_strategy
            if configuration is None
            else configuration.memory_strategy
        )
        paths = _worker_artifact_paths(
            sandbox,
            memory_enabled=memory_case,
            ablation_enabled=configuration is not None,
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
                timeout_seconds=timeout_seconds,
                artifact_paths=paths,
            )
            atomic_write_json(paths.worker_request, request)
            prepared = self._config_builder.write(
                sandbox.hermes_home / "config.yaml",
                (
                    case.execution.config_overrides
                    if configuration is None
                    else effective_config_overrides(case, configuration)
                ),
            )
            subject_model = (
                _safe_subject_model(
                    prepared.document,
                    sensitive_values,
                )
                if configuration is None
                else self.p4_model_identifier(case, configuration)
            )
            environment = self._build_worker_environment(
                case,
                sandbox,
                trial_id=trial_id,
                config_references=prepared.environment_references,
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
                result = _fallback_worker_result(
                    paths,
                    error_type="timeout",
                    message="MyHermes worker exceeded the Trial timeout",
                    duration_ms=duration_ms,
                    warnings=runtime_warnings,
                    memory_strategy=memory_strategy,
                    recovered_memory=recovered_memory,
                    variant_id=(None if variant is None else variant.variant_id),
                    configuration=configuration,
                    recovered_ablation=recovered_ablation,
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
            _validate_worker_artifacts(
                request,
                result,
                transcript,
                observations,
                memory_artifact,
                ablation_artifact,
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
                    tool_names=result.tool_names,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
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
            tool_calls=tool_calls,
            tool_trace_complete=(
                status is RunnerStatus.COMPLETED
                and observations is not None
                and not observations.truncated
            ),
            artifact_paths={
                field_name: value
                for field_name in type(paths).model_fields
                if field_name != "schema_version"
                and (value := getattr(paths, field_name)) is not None
                and value.exists()
            },
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


def _safe_subject_model(
    document: dict,
    sensitive_values: tuple[str, ...],
) -> str | None:
    value = document.get("model")
    if not isinstance(value, str) or not value.strip():
        return None
    return redact_text(value.strip(), sensitive_values)[:256]


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


def _capability_available(report: SubjectCapabilityReport, name: str) -> bool:
    capability = report.capability(name)
    return capability is not None and capability.available


def _worker_artifact_paths(
    sandbox: AuditSandbox,
    *,
    memory_enabled: bool,
    ablation_enabled: bool,
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
    )


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
    }
    if len(protocol_versions) != 1:
        raise WorkerProtocolError("worker artifact protocol versions do not match")
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
) -> MyHermesWorkerResult:
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
    return MyHermesWorkerResult(
        worker_status=WorkerStatus.FAILED,
        runtime_status=error_type,
        error_type=error_type,
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
        warnings=list(warnings),
        error=WorkerError(error_type=error_type, message=message),
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


__all__ = ("MyHermesTrialRunner",)
