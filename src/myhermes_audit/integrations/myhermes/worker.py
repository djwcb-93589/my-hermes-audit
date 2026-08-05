"""Isolated file-protocol worker that invokes real MyHermes conversations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json
from myhermes_audit.contracts import (
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
    ReviewAction,
    ReviewAttempt,
    ReviewError,
    ReviewLifecycle,
    ReviewOutcome,
    ReviewStatus,
    CompressionEvent,
    CompressionEventStatus,
    ContextDiagnostic,
    DiagnosticStatus,
    FactContextObservation,
    MemoryErrorType,
    MemoryOperationError,
    MemoryQueryPhase,
    MemorySnapshotPhase,
    RetrievalStrategy,
    TokenCountSource,
    TurnResult,
)
from myhermes_audit.contracts import ScenarioError
from myhermes_audit.errors import (
    AuditError,
    CompressionConfigurationError,
    CompressionLimitError,
)
from myhermes_audit.fact_matching import (
    match_distortion_candidate,
    match_required_fact,
)
from myhermes_audit.integrations.myhermes.contracts import (
    AblationArtifact,
    BackgroundReviewArtifact,
    BackgroundReviewEvidenceArtifact,
    BackgroundReviewSnapshotsArtifact,
    MemoryArtifact,
    MemoryQueryPlan,
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
    ProcessCleanupArtifact,
    ProcessScenarioArtifact,
    ToolchainScenarioArtifact,
    WorkerError,
    WorkerStatus,
    WorkerTranscript,
    WorkerWarning,
)
from myhermes_audit.memory_state import diff_memory_snapshots
from myhermes_audit.integrations.myhermes.lifecycle import (
    close_runtime_resources,
)
from myhermes_audit.integrations.myhermes.observation_reader import (
    latest_run_id,
    read_observations,
)
from myhermes_audit.integrations.myhermes.scenarios import build_scenario_results
from myhermes_audit.metrics import aggregate_model_cache
from myhermes_audit.security import redact_text, sensitive_environment_values


_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_IDENTIFIER_CHARACTER = re.compile(r"[^A-Za-z0-9._:-]+")


class WorkerTerminationRequested(Exception):
    """Raised by cooperative process-group termination signals."""


class _ProcessMonotonicTracker:
    """Capture public PRE/POST Tool hook boundaries for Process observations.

    PRE is the public control hook before dispatch. POST is the public hook
    after the Observation batch is persisted. It is deliberately not treated
    as exact handler completion. The tracker records only monotonic nanoseconds
    keyed by the public Tool call ID. It never reads private AgentLoop state or
    persists command/input content; the Scenario projection later keeps only
    relative offsets and safe spans.
    """

    _TOOLS = frozenset({"terminal", "process"})

    def __init__(self) -> None:
        self._started_ns: dict[str, int] = {}
        self._completed_ns: dict[str, int] = {}

    @staticmethod
    def _identity(context) -> tuple[str, str] | None:
        payload = context.payload
        if not isinstance(payload, Mapping):
            return None
        tool_name = payload.get("tool_name")
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_name, str) or tool_name not in _ProcessMonotonicTracker._TOOLS:
            return None
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return None
        return tool_name, tool_call_id

    def on_pre(self, context) -> None:
        identity = self._identity(context)
        if identity is not None:
            self._started_ns[identity[1]] = time.monotonic_ns()

    def on_post(self, context) -> None:
        identity = self._identity(context)
        if identity is not None:
            self._completed_ns[identity[1]] = time.monotonic_ns()

    def boundaries(self) -> Mapping[str, tuple[int | None, int | None]]:
        """Return independent PRE and POST offsets for each public call.

        The absolute monotonic readings remain Worker-local.  A missing POST
        must not erase an otherwise reliable PRE boundary used by WAIT's
        PRE-to-PRE budget calculation.
        """

        call_ids = set(self._started_ns) | set(self._completed_ns)
        return {
            call_id: (
                self._started_ns.get(call_id),
                self._completed_ns.get(call_id),
            )
            for call_id in call_ids
        }


class _NoopBackgroundReviewCoordinator:
    """Prevent the Subject's foreground entry point from creating a singleton.

    P0--P4 deliberately have no P5 Review runtime.  MyHermes otherwise treats
    ``None`` as a request for its process-global coordinator, so the worker
    always supplies this tiny inert collaborator unless a P5 trial replaces it
    with the public, trial-local disabled coordinator.
    """

    @staticmethod
    def after_foreground_result(_connection, _session_id, _result) -> None:
        return None


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="myhermes-audit-myhermes-worker")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser.parse_args(argv)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load_request(path: Path) -> MyHermesWorkerRequest:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError("worker request is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError("worker request must be a regular file")
    if stat.st_size > _MAX_REQUEST_BYTES:
        raise ValueError("worker request exceeds the size limit")
    try:
        text = path.read_text(encoding="utf-8")
        json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        return MyHermesWorkerRequest.model_validate_json(text)
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise ValueError("worker request is invalid") from exc


def _validate_isolation_boundary(
    request: MyHermesWorkerRequest,
    *,
    request_path: Path,
    result_path: Path,
) -> None:
    workspace = request.workspace.resolve(strict=True)
    hermes_home = request.hermes_home.resolve(strict=True)
    sqlite_path = request.sqlite_path.resolve(strict=False)
    if request.workspace.is_symlink() or request.hermes_home.is_symlink():
        raise ValueError("worker roots cannot be symbolic links")
    if Path.cwd().resolve(strict=True) != workspace:
        raise ValueError("worker cwd does not match its isolated workspace")
    trial_root = workspace.parent.resolve(strict=True)
    database_dir = trial_root / "database"
    if (
        request.workspace.parent.is_symlink()
        or workspace.name != "workspace"
        or hermes_home.name != "hermes_home"
        or hermes_home.parent.resolve(strict=True) != trial_root
        or database_dir.is_symlink()
        or request.sqlite_path.is_symlink()
        or request.sqlite_path.parent.resolve(strict=True)
        != database_dir.resolve(strict=True)
        or sqlite_path != (trial_root / "database" / "hermes.db").resolve(
            strict=False
        )
    ):
        raise ValueError("worker runtime paths do not share the Trial root")
    env_home = os.environ.get("HERMES_HOME")
    env_db = os.environ.get("DB_PATH")
    if env_home is None or Path(env_home).resolve(strict=False) != hermes_home:
        raise ValueError("HERMES_HOME does not match the worker request")
    if env_db is None or Path(env_db).resolve(strict=False) != sqlite_path:
        raise ValueError("DB_PATH does not match the worker request")
    env_workspace = os.environ.get("HERMES_WORKSPACE")
    if (
        env_workspace is None
        or Path(env_workspace).resolve(strict=False) != workspace
    ):
        raise ValueError("HERMES_WORKSPACE does not match the worker request")
    if os.environ.get("MYHERMES_AUDIT_TRIAL_ID") != request.trial_id:
        raise ValueError("Audit Trial identity does not match the worker request")
    if request_path.resolve(strict=True) != request.artifact_paths.worker_request.resolve(
        strict=True
    ):
        raise ValueError("request path does not match the protocol envelope")
    if result_path.resolve(strict=False) != request.artifact_paths.worker_result.resolve(
        strict=False
    ):
        raise ValueError("result path does not match the protocol envelope")
    artifacts_root = request.artifact_paths.worker_result.parent.resolve(strict=True)
    if (
        request.artifact_paths.worker_result.parent.is_symlink()
        or artifacts_root != (trial_root / "artifacts").resolve(strict=True)
    ):
        raise ValueError("worker artifacts directory escaped the Trial root")
    env_artifacts = os.environ.get("MYHERMES_AUDIT_ARTIFACTS_DIR")
    if (
        env_artifacts is None
        or Path(env_artifacts).resolve(strict=False) != artifacts_root
    ):
        raise ValueError("Audit artifacts environment does not match the request")
    for field_name in type(request.artifact_paths).model_fields:
        if field_name == "schema_version":
            continue
        candidate = getattr(request.artifact_paths, field_name)
        if candidate is None:
            continue
        candidates = candidate if isinstance(candidate, list) else [candidate]
        for current in candidates:
            if current.parent.resolve(strict=True) != artifacts_root:
                raise ValueError("artifact path escaped the Trial artifacts directory")
            if current.is_symlink():
                raise ValueError("artifact paths cannot be symbolic links")
    config_path = hermes_home / "config.yaml"
    if not config_path.is_file() or config_path.is_symlink():
        raise ValueError("isolated MyHermes config is missing")
    if (hermes_home / ".env").exists():
        raise ValueError("isolated HERMES_HOME must not contain .env")


def _install_termination_handlers() -> None:
    def request_termination(_signum, _frame) -> None:
        raise WorkerTerminationRequested("worker termination requested")

    signal.signal(signal.SIGTERM, request_termination)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_termination)


def _execute(request: MyHermesWorkerRequest) -> MyHermesWorkerResult:
    started = time.perf_counter()
    sensitive_values = sensitive_environment_values(os.environ)
    connection = None
    session_ids: list[str] = []
    sessions_by_logical_id: dict[str, str] = {}
    process_manager = None
    model_client = None
    lifecycle_warnings = []
    turns: list[TurnResult] = []
    seen_run_ids: set[str] = set()
    tool_batches = 0
    response_tool_calls = 0
    failed_status: str | None = None
    failed_error_type: str | None = None
    failed_fatal = False
    failed_retryable = False
    observations = ObservationBundle()
    memory_adapter = None
    memory_provider = "unavailable"
    memory_query_results = []
    memory_snapshots = []
    memory_state_changes = []
    memory_errors: list[MemoryOperationError] = []
    memory_clear_attempted = False
    memory_clear_succeeded: bool | None = None
    memory_blocked = False
    compression_events = []
    context_diagnostics: list[ContextDiagnostic] = []
    fact_context_observations: list[FactContextObservation] = []
    background_review_adapter = None
    background_review_executor = None
    background_review_coordinator = _NoopBackgroundReviewCoordinator()
    background_review_results: list[BackgroundReviewExecutionResult] = []
    background_review_errors: list[BackgroundReviewExecutionError] = []
    review_failure_stops_foreground = False
    previous_session_id: str | None = None
    estimate_context_tokens = None
    response_messages: list[Mapping[str, object]] = []
    cleanup_reports: list[dict] = []
    process_monotonic_tracker = _ProcessMonotonicTracker()
    scenario_results = []
    process_errors: list[ScenarioError] = []
    process_output_paths: list[Path] = []
    fact_by_id = {
        fact.fact_id: fact
        for expectation in request.required_fact_expectations
        for fact in expectation.facts
    }
    checkpoints_by_turn: dict[int, list] = {}
    for checkpoint in request.checkpoints:
        checkpoints_by_turn.setdefault(checkpoint.after_turn, []).append(checkpoint)
    try:
        # This is the first hermes import in the worker. All environment and
        # cwd checks above have already completed.
        from hermes.config import (
            BACKGROUND_REVIEW_CONFIG,
            BROWSER_CONFIG,
            COMPRESSION_THRESHOLD,
            KEEP_RECENT_TOOL_RESULTS,
            PROTECT_FIRST,
            TAIL_TOKEN_BUDGET,
            DB_PATH,
            HERMES_HOME,
            MODEL,
            MODEL_MAX_OUTPUT_TOKENS,
            client,
        )
        from hermes.conversation import run_conversation
        from hermes.hooks import HookEventName, SyncHookRegistry
        from hermes.persistence.core import create_session
        from hermes.persistence.observation import configure_sqlite_observation_sink
        from hermes.persistence.schema import init_db
        from hermes.processes import process_manager as default_process_manager
        from hermes.prompt import build_system_prompt
        from hermes.tools import (
            ExecutionEnvironment,
            ToolPolicy,
            ToolRegistry,
            register_all,
            registry,
        )
        if request.effective_subject_configuration is not None:
            try:
                from hermes.tokens import estimate_tokens as estimate_context_tokens
            except (ImportError, AttributeError):
                estimate_context_tokens = None

        if Path(HERMES_HOME).resolve(strict=False) != request.hermes_home.resolve(
            strict=True
        ):
            raise RuntimeError("MyHermes loaded a different HERMES_HOME")
        if Path(DB_PATH).resolve(strict=False) != request.sqlite_path.resolve(
            strict=False
        ):
            raise RuntimeError("MyHermes loaded a different DB_PATH")
        _assert_public_capability_boundary(
            browser_config=BROWSER_CONFIG,
            background_review_config=BACKGROUND_REVIEW_CONFIG,
        )
        if request.effective_subject_configuration is not None:
            expected_compression = request.effective_subject_configuration.public_config_overrides.get(
                "compression",
                {},
            )
            actual_compression = {
                "threshold": COMPRESSION_THRESHOLD,
                "protect_first": PROTECT_FIRST,
                "keep_recent_tool_results": KEEP_RECENT_TOOL_RESULTS,
                "tail_token_budget": TAIL_TOKEN_BUDGET,
            }
            if any(
                actual_compression.get(name) != value
                for name, value in expected_compression.items()
            ):
                raise CompressionConfigurationError(
                    "Subject compression configuration does not match the Variant",
                    variant_id=request.variant_id,
                )
        model_client = client
        process_manager = default_process_manager
        connection = init_db(str(request.sqlite_path))
        register_all(process_manager=process_manager)

        enabled = frozenset(item.value for item in request.enabled_toolsets)
        tool_policy = ToolPolicy(
            ExecutionEnvironment.CLI,
            enabled_toolsets=enabled,
            unattended=True,
        )
        resolution = registry.resolve(tool_policy)
        if resolution.toolsets != enabled:
            missing = sorted(enabled - resolution.toolsets)
            raise RuntimeError(
                "MyHermes did not resolve requested toolsets: " + ", ".join(missing)
            )
        hook_registry = SyncHookRegistry()
        configure_sqlite_observation_sink(hook_registry, request.sqlite_path)
        if any(item.kind.value == "process_background" for item in request.scenarios):
            # MyHermes exposes public PRE/POST Tool hooks.  Register a
            # trial-local monotonic observer so WAIT remaining-budget and
            # Scenario duration do not depend on persisted timestamps or
            # summed handler durations.
            hook_registry.register(
                HookEventName.PRE_TOOL_CALL.value,
                process_monotonic_tracker.on_pre,
                hook_id="myhermes-audit:process-monotonic-pre",
            )
            hook_registry.register(
                HookEventName.POST_TOOL_CALL.value,
                process_monotonic_tracker.on_post,
                hook_id="myhermes-audit:process-monotonic-post",
            )

        if request.memory_strategy is not None:
            try:
                from myhermes_audit.integrations.myhermes.memory_adapter import (
                    MyHermesMemoryAdapter,
                )

                memory_adapter = MyHermesMemoryAdapter(
                    strategy=request.memory_strategy,
                )
                memory_provider = memory_adapter.provider
            except Exception as exc:
                memory_errors.append(
                    _memory_operation_error(
                        exc,
                        operation="capability",
                        fallback=MemoryErrorType.CAPABILITY,
                    )
                )
                memory_blocked = True
            if memory_adapter is not None and request.memory_fixture is not None:
                try:
                    asyncio.run(memory_adapter.seed(request.memory_fixture))
                except Exception as exc:
                    memory_errors.append(
                        _memory_operation_error(
                            exc,
                            operation="seed",
                            fallback=MemoryErrorType.SEED,
                        )
                    )
                    memory_blocked = True
            if memory_adapter is not None:
                try:
                    memory_snapshots.append(
                        asyncio.run(
                            memory_adapter.snapshot(
                                phase=MemorySnapshotPhase.BEFORE_CONVERSATION,
                            )
                        )
                    )
                except Exception as exc:
                    memory_errors.append(
                        _memory_operation_error(
                            exc,
                            operation="snapshot_before",
                            fallback=MemoryErrorType.SNAPSHOT,
                        )
                    )
                if not memory_blocked:
                    _run_memory_queries(
                        memory_adapter,
                        request.memory_queries,
                        phase=MemoryQueryPhase.BEFORE_CONVERSATION,
                        results=memory_query_results,
                        errors=memory_errors,
                    )

        if request.background_review_plans:
            try:
                from myhermes_audit.integrations.myhermes.background_review_adapter import (
                    MyHermesBackgroundReviewAdapter,
                )

                review_tool_registry = ToolRegistry()
                register_all(
                    review_tool_registry,
                    process_manager=process_manager,
                )
                background_review_adapter = MyHermesBackgroundReviewAdapter(
                    connection=connection,
                    sqlite_path=request.sqlite_path,
                    model=MODEL,
                    model_client=model_client,
                    tool_registry=review_tool_registry,
                    model_max_output_tokens=MODEL_MAX_OUTPUT_TOKENS,
                    memory_adapter=memory_adapter,
                    sensitive_values=sensitive_values,
                    plans=request.background_review_plans,
                )
                background_review_adapter.seed_skills(request.skill_fixtures)
                (
                    background_review_coordinator,
                    background_review_executor,
                ) = background_review_adapter.make_disabled_foreground_coordinator(
                    process_manager=process_manager,
                )
            except Exception as exc:
                failed_status = "background_review_error"
                failed_error_type = "background_review_capability_error"
                failed_fatal = True
                failed_retryable = False
                background_review_errors.append(
                    _background_review_error(
                        "background_review_capability_error",
                        "initialize",
                        "Background Review runtime could not be initialized",
                        exc,
                    )
                )

        if memory_blocked:
            first_error = memory_errors[0]
            failed_status = "memory_error"
            failed_error_type = first_error.error_type.value
            failed_fatal = True
            failed_retryable = first_error.retryable

        cached_prompt = (
            _build_system_prompt(
                build_system_prompt,
                request,
                sorted(resolution.toolsets),
            )
            if request.memory_strategy is None
            else None
        )

        for turn_number, requested_turn in enumerate(request.turns, start=1):
            if memory_blocked or (
                bool(request.background_review_plans)
                and background_review_adapter is None
            ):
                break
            user_message = requested_turn.message
            logical_session_id = requested_turn.session_id or "__trial_default__"
            session_id = sessions_by_logical_id.get(logical_session_id)
            if session_id is None:
                session_id = create_session(connection, source="cli")
                sessions_by_logical_id[logical_session_id] = session_id
                session_ids.append(session_id)
            prompt = cached_prompt or _build_system_prompt(
                build_system_prompt,
                request,
                sorted(resolution.toolsets),
            )
            turn_started = datetime.now(timezone.utc)
            turn_clock = time.perf_counter()
            response = run_conversation(
                user_message,
                connection,
                session_id,
                prompt,
                session_key=session_id,
                enabled_toolsets=sorted(resolution.toolsets),
                tool_context={"interactive_approval": False},
                tool_policy=tool_policy,
                hook_registry=hook_registry,
                background_review_coordinator=background_review_coordinator,
            )
            if isinstance(response, Mapping):
                response_messages.append(response)
            duration_ms = max(0, round((time.perf_counter() - turn_clock) * 1000))
            turn_finished = datetime.now(timezone.utc)
            run_id = latest_run_id(request.sqlite_path, seen_run_ids)
            if run_id is not None:
                seen_run_ids.add(run_id)
            runtime_status = _safe_status(response.get("status"), "invalid_result")
            ok = response.get("ok") is True and runtime_status == "completed"
            error_type = _optional_identifier(response.get("error_type"))
            final_output = response.get("final_response")
            if not isinstance(final_output, str) or not ok:
                final_output = None
            elif final_output is not None:
                final_output = redact_text(final_output, sensitive_values)
            turns.append(
                TurnResult(
                    turn_number=turn_number,
                    user_message=redact_text(user_message, sensitive_values),
                    session_id=requested_turn.session_id,
                    final_output=final_output,
                    runtime_status=runtime_status,
                    error_type=error_type,
                    started_at=turn_started,
                    finished_at=turn_finished,
                    duration_ms=duration_ms,
                    run_id=run_id,
                )
            )
            if background_review_adapter is not None:
                for plan in request.background_review_plans:
                    if (
                        plan.trigger_after_turn != turn_number
                        or plan.foreground_session_id != logical_session_id
                    ):
                        continue
                    try:
                        review_result = (
                            background_review_adapter.record_foreground_and_execute(
                                plan,
                                logical_session_id=logical_session_id,
                                session_id=session_id,
                                completed=ok,
                                tool_batches=_nonnegative_int(
                                    response.get("tool_batches")
                                ),
                            )
                        )
                        _append_background_review_result(
                            request,
                            background_review_results,
                            background_review_errors,
                            lifecycle_warnings,
                            review_result,
                        )
                        if (
                            review_result.status.value in {"failed", "stale"}
                            and not plan.continue_after_failure
                        ):
                            failed_status = failed_status or "background_review_error"
                            failed_error_type = failed_error_type or "background_review_error"
                            failed_fatal = True
                            review_failure_stops_foreground = True
                    except Exception:
                        review_result = _background_review_failure_result(
                            plan,
                            "background_review_execution_error",
                            "Background Review trigger execution failed",
                        )
                        _append_background_review_result(
                            request,
                            background_review_results,
                            background_review_errors,
                            lifecycle_warnings,
                            review_result,
                        )
                        if not plan.continue_after_failure:
                            failed_status = failed_status or "background_review_error"
                            failed_error_type = (
                                failed_error_type or "background_review_error"
                            )
                            failed_fatal = True
                            review_failure_stops_foreground = True
            if request.effective_subject_configuration is not None:
                diagnostic, fact_observations = _observe_p4_turn(
                    request,
                    turn_index=turn_number,
                    session_id=session_id,
                    previous_session_id=previous_session_id,
                    response=response,
                    estimate_context_tokens=estimate_context_tokens,
                    fact_by_id=fact_by_id,
                    checkpoints=checkpoints_by_turn.get(turn_number, []),
                )
                context_diagnostics.append(diagnostic)
                fact_context_observations.extend(fact_observations)
                previous_session_id = session_id
            tool_batches += _nonnegative_int(response.get("tool_batches"))
            response_tool_calls += _nonnegative_int(response.get("tool_call_count"))
            if not ok:
                current_fatal = response.get("fatal") is True
                if failed_status is None:
                    failed_status = runtime_status
                    failed_error_type = error_type or _safe_identifier(runtime_status)
                    failed_retryable = response.get("retryable") is True
                failed_fatal = failed_fatal or current_fatal
                if current_fatal:
                    break
            if review_failure_stops_foreground:
                break

        if background_review_adapter is not None:
            completed_review_ids = {
                item.review_id for item in background_review_results
            }
            for plan in request.background_review_plans:
                if plan.review_id in completed_review_ids:
                    continue
                review_result = background_review_adapter.mark_not_triggered(plan)
                _append_background_review_result(
                    request,
                    background_review_results,
                    background_review_errors,
                    lifecycle_warnings,
                    review_result,
                )
        elif request.background_review_plans:
            for plan in request.background_review_plans:
                review_result = _background_review_failure_result(
                    plan,
                    "background_review_capability_error",
                    "Background Review runtime was unavailable before the trigger",
                )
                _append_background_review_result(
                    request,
                    background_review_results,
                    background_review_errors,
                    lifecycle_warnings,
                    review_result,
                )

        if request.memory_strategy is not None and memory_adapter is not None:
            if not memory_blocked:
                _run_memory_queries(
                    memory_adapter,
                    request.memory_queries,
                    phase=MemoryQueryPhase.AFTER_CONVERSATION,
                    results=memory_query_results,
                    errors=memory_errors,
                )
            try:
                memory_snapshots.append(
                    asyncio.run(
                        memory_adapter.snapshot(
                            phase=MemorySnapshotPhase.AFTER_CONVERSATION,
                        )
                    )
                )
            except Exception as exc:
                memory_errors.append(
                    _memory_operation_error(
                        exc,
                        operation="snapshot_after",
                        fallback=MemoryErrorType.SNAPSHOT,
                    )
                )
            before_snapshot = next(
                (
                    item
                    for item in memory_snapshots
                    if item.phase is MemorySnapshotPhase.BEFORE_CONVERSATION
                ),
                None,
            )
            after_snapshot = next(
                (
                    item
                    for item in memory_snapshots
                    if item.phase is MemorySnapshotPhase.AFTER_CONVERSATION
                ),
                None,
            )
            if before_snapshot is not None and after_snapshot is not None:
                try:
                    memory_state_changes = diff_memory_snapshots(
                        before_snapshot,
                        after_snapshot,
                    )
                except Exception as exc:
                    memory_errors.append(
                        _memory_operation_error(
                            exc,
                            operation="state_diff",
                            fallback=MemoryErrorType.STATE_VALIDATION,
                        )
                    )

        run_durations = {
            turn.run_id: turn.duration_ms
            for turn in turns
            if turn.run_id is not None
        }
        if turns:
            observations = read_observations(
                request.sqlite_path,
                run_durations=run_durations,
                include_run_ids=frozenset(run_durations),
            )
        if request.effective_subject_configuration is not None:
            compression_events, compression_by_turn = (
                _project_public_compression_observations(
                    request,
                    turns=turns,
                    observations=observations,
                )
            )
            context_diagnostics = [
                item.model_copy(
                    update={
                        "compression_applied": compression_by_turn.get(
                            item.turn_index
                        )
                    }
                )
                for item in context_diagnostics
            ]
            fact_context_observations = [
                item.model_copy(
                    update={
                        "compression_applied": compression_by_turn.get(
                            item.turn_index
                        )
                    }
                )
                for item in fact_context_observations
            ]
        if any(turn.run_id is None for turn in turns):
            failed_status = failed_status or "observation_error"
            failed_error_type = failed_error_type or "observation_unavailable"
            failed_fatal = True
            failed_retryable = False
    finally:
        if request.background_review_plans:
            existing_review_ids = {
                item.review_id for item in background_review_results
            }
            for plan in request.background_review_plans:
                if plan.review_id in existing_review_ids:
                    continue
                review_result = _background_review_failure_result(
                    plan,
                    "background_review_trigger_error",
                    "Background Review did not reach its declared foreground trigger",
                )
                _append_background_review_result(
                    request,
                    background_review_results,
                    background_review_errors,
                    lifecycle_warnings,
                    review_result,
                )
        if request.effective_subject_configuration is not None:
            ablation_path = request.artifact_paths.ablation
            if ablation_path is None:
                raise RuntimeError("P4 worker request has no Ablation Artifact path")
            try:
                atomic_write_json(
                    ablation_path,
                    _build_ablation_artifact(
                        request,
                        compression_events=compression_events,
                        context_diagnostics=context_diagnostics,
                        fact_context_observations=fact_context_observations,
                    ),
                )
            except Exception as exc:
                lifecycle_warnings.append(
                    _worker_warning("ablation_artifact_checkpoint_error", exc)
                )
        if request.memory_strategy is not None:
            memory_path = request.artifact_paths.memory
            if memory_path is None:
                raise RuntimeError("P3 worker request has no Memory Artifact path")
            try:
                atomic_write_json(
                    memory_path,
                    _build_memory_artifact(
                        request,
                        provider=memory_provider,
                        adapter=memory_adapter,
                        query_results=memory_query_results,
                        snapshots=memory_snapshots,
                        state_changes=memory_state_changes,
                        errors=memory_errors,
                        clear_attempted=False,
                        clear_succeeded=None,
                    ),
                )
            except Exception as exc:
                lifecycle_warnings.append(
                    _worker_warning("memory_artifact_checkpoint_error", exc)
                )
        if memory_adapter is not None:
            memory_clear_attempted = True
            try:
                asyncio.run(memory_adapter.clear())
                memory_clear_succeeded = True
            except Exception as exc:
                memory_clear_succeeded = False
                memory_errors.append(
                    _memory_operation_error(
                        exc,
                        operation="clear",
                        fallback=MemoryErrorType.CLEAR,
                    )
                )
                lifecycle_warnings.append(
                    WorkerWarning(
                        warning_type="memory_clear_error",
                        message="MyHermes managed Memory cleanup failed",
                    )
                )
        if background_review_executor is not None:
            try:
                if background_review_executor.shutdown(2.0):
                    background_review_errors.append(
                        _background_review_error(
                            "background_review_cleanup_error",
                            "shutdown",
                            "trial-local Background Review executor did not stop",
                        )
                    )
                    lifecycle_warnings.append(
                        WorkerWarning(
                            warning_type="background_review_shutdown_incomplete",
                            message="trial-local Background Review executor did not stop",
                        )
                    )
                    failed_status = failed_status or "background_review_error"
                    failed_error_type = (
                        failed_error_type or "background_review_cleanup_error"
                    )
                    failed_fatal = True
            except Exception as exc:
                background_review_errors.append(
                    _background_review_error(
                        "background_review_cleanup_error",
                        "shutdown",
                        "trial-local Background Review executor shutdown failed",
                        exc,
                    )
                )
                lifecycle_warnings.append(
                    _worker_warning("background_review_shutdown_error", exc)
                )
                failed_status = failed_status or "background_review_error"
                failed_error_type = (
                    failed_error_type or "background_review_cleanup_error"
                )
                failed_fatal = True
        if request.background_review_plans:
            _checkpoint_background_review_artifacts(
                request,
                background_review_results,
                background_review_errors,
                lifecycle_warnings,
            )
        if process_manager is not None:
            try:
                lifecycle_warnings.extend(
                    close_runtime_resources(
                        connection=connection,
                        session_ids=session_ids,
                        process_manager=process_manager,
                        model_client=model_client,
                        shutdown_background_review=not bool(
                            request.background_review_plans
                        ),
                        cleanup_reports=cleanup_reports,
                    )
                )
            except Exception as exc:
                lifecycle_warnings.append(
                    _worker_warning("lifecycle_shutdown_error", exc)
                )
        elif connection is not None:
            try:
                connection.close()
            except Exception as exc:
                lifecycle_warnings.append(
                    _worker_warning("database_close_error", exc)
                )
        if process_manager is None and model_client is not None:
            close = getattr(model_client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    lifecycle_warnings.append(
                        _worker_warning("model_client_close_error", exc)
                    )
        if request.memory_strategy is not None:
            memory_path = request.artifact_paths.memory
            if memory_path is None:
                raise RuntimeError("P3 worker request has no Memory Artifact path")
            atomic_write_json(
                memory_path,
                _build_memory_artifact(
                    request,
                    provider=memory_provider,
                    adapter=memory_adapter,
                    query_results=memory_query_results,
                    snapshots=memory_snapshots,
                    state_changes=memory_state_changes,
                    errors=memory_errors,
                    clear_attempted=memory_clear_attempted,
                    clear_succeeded=memory_clear_succeeded,
                ),
            )
        if request.effective_subject_configuration is not None:
            ablation_path = request.artifact_paths.ablation
            if ablation_path is None:
                raise RuntimeError("P4 worker request has no Ablation Artifact path")
            atomic_write_json(
                ablation_path,
                _build_ablation_artifact(
                    request,
                    compression_events=compression_events,
                    context_diagnostics=context_diagnostics,
                    fact_context_observations=fact_context_observations,
                ),
            )

    if request.scenarios:
        try:
            scenario_results, process_errors, process_output_paths = build_scenario_results(
                request,
                responses=response_messages,
                observations=observations,
                turns=turns,
                cleanup_reports=cleanup_reports,
                process_hook_boundaries=process_monotonic_tracker.boundaries(),
                sensitive_values=sensitive_values,
            )
            toolchain_results = [
                item for item in scenario_results
                if item.kind.value == "toolchain"
            ]
            process_results = [
                item for item in scenario_results
                if item.kind.value == "process_background"
            ]
            if request.artifact_paths.toolchain_results is not None:
                atomic_write_json(
                    request.artifact_paths.toolchain_results,
                    ToolchainScenarioArtifact(
                        trial_id=request.trial_id,
                        case_id=request.case_id,
                        results=toolchain_results,
                    ),
                )
            if request.artifact_paths.process_scenario_results is not None:
                atomic_write_json(
                    request.artifact_paths.process_scenario_results,
                    ProcessScenarioArtifact(
                        trial_id=request.trial_id,
                        case_id=request.case_id,
                        results=process_results,
                    ),
                )
            if request.artifact_paths.process_cleanup is not None:
                atomic_write_json(
                    request.artifact_paths.process_cleanup,
                    ProcessCleanupArtifact(
                        trial_id=request.trial_id,
                        case_id=request.case_id,
                        reports=cleanup_reports,
                    ),
                )
        except Exception as exc:
            process_errors.append(
                ScenarioError(
                    error_type="scenario-artifact-error",
                    message="scenario observations could not be persisted",
                )
            )
            lifecycle_warnings.append(_worker_warning("scenario_artifact_error", exc))
            failed_status = failed_status or "scenario_artifact_error"
            failed_error_type = failed_error_type or "scenario-artifact-error"
            failed_fatal = True
    duration_ms = max(0, round((time.perf_counter() - started) * 1000))
    prompt_tokens = _complete_optional_sum(
        item.prompt_tokens for item in observations.model_calls
    )
    completion_tokens = _complete_optional_sum(
        item.completion_tokens for item in observations.model_calls
    )
    if prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    else:
        total_tokens = _complete_optional_sum(
            item.total_tokens for item in observations.model_calls
        )
    tool_names = list(
        dict.fromkeys(item.tool_name for item in observations.tool_calls)
    )
    if observations.truncated:
        lifecycle_warnings.append(
            WorkerWarning(
                warning_type="observation_truncated",
                message="public Observation projection reached the P1 size limit",
            )
        )
    cache_aggregation = aggregate_model_cache(
        observations.model_calls,
        invalid_model_call_count=observations.cache_invalid_model_call_count,
    )
    if cache_aggregation.invalid_model_call_count:
        lifecycle_warnings.append(
            WorkerWarning(
                warning_type="deepseek_cache_invalid",
                message="public DeepSeek cache token observations were invalid",
            )
        )
    run_ids = [turn.run_id for turn in turns if turn.run_id is not None]
    completed = failed_status is None and len(turns) == len(request.turns)
    if completed:
        error = None
        error_type = None
        final_output = turns[-1].final_output if turns else None
        runtime_status = turns[-1].runtime_status if turns else "completed"
    else:
        error_type = failed_error_type or "worker_runtime_error"
        error = WorkerError(
            error_type=error_type,
            message=f"MyHermes runtime ended with status {failed_status or 'failed'}",
        )
        final_output = None
        runtime_status = failed_status or "failed"

    transcript = WorkerTranscript(
        trial_id=request.trial_id,
        case_id=request.case_id,
        turns=turns,
    )
    atomic_write_json(request.artifact_paths.transcript, transcript)
    atomic_write_json(request.artifact_paths.observations, observations)
    return MyHermesWorkerResult(
        worker_status=(WorkerStatus.COMPLETED if completed else WorkerStatus.FAILED),
        runtime_status=runtime_status,
        final_output=final_output,
        turns=turns,
        run_ids=run_ids,
        error_type=error_type,
        fatal=failed_fatal,
        retryable=failed_retryable,
        iterations=sum(item.iterations for item in observations.runs),
        tool_batches=tool_batches,
        tool_call_count=max(
            response_tool_calls,
            len(observations.tool_calls),
        ),
        model_call_count=cache_aggregation.model_call_count,
        tool_names=tool_names,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        prompt_cache_hit_tokens=cache_aggregation.prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=cache_aggregation.prompt_cache_miss_tokens,
        deepseek_cache_hit_rate=cache_aggregation.deepseek_cache_hit_rate,
        deepseek_cache_status=cache_aggregation.deepseek_cache_status,
        deepseek_cache_evaluated_model_call_count=(
            cache_aggregation.evaluated_model_call_count
        ),
        deepseek_cache_invalid_model_call_count=(
            cache_aggregation.invalid_model_call_count
        ),
        duration_ms=duration_ms,
        observations_artifact=_artifact_relative(
            request.artifact_paths.observations
        ),
        transcript_artifact=_artifact_relative(request.artifact_paths.transcript),
        warnings=lifecycle_warnings,
        error=error,
        memory_artifact=(
            None
            if request.memory_strategy is None
            else _artifact_relative(request.artifact_paths.memory)
        ),
        memory_query_results=memory_query_results,
        memory_snapshots=memory_snapshots,
        memory_state_changes=memory_state_changes,
        memory_errors=memory_errors,
        variant_id=request.variant_id,
        effective_subject_configuration=request.effective_subject_configuration,
        ablation_artifact=(
            None
            if request.effective_subject_configuration is None
            else _artifact_relative(request.artifact_paths.ablation)
        ),
        compression_events=compression_events,
        context_diagnostics=context_diagnostics,
        fact_context_observations=fact_context_observations,
        background_review_results_artifact=(
            None
            if not request.background_review_plans
            else _artifact_relative(request.artifact_paths.background_review_results)
        ),
        background_review_evidence_artifact=(
            None
            if not request.background_review_plans
            else _artifact_relative(request.artifact_paths.background_review_evidence)
        ),
        background_review_snapshots_artifact=(
            None
            if not request.background_review_plans
            else _artifact_relative(request.artifact_paths.background_review_snapshots)
        ),
        background_review_results=background_review_results,
        background_review_errors=background_review_errors,
        scenario_results=scenario_results,
        process_errors=process_errors,
        toolchain_results_artifact=(
            None
            if request.artifact_paths.toolchain_results is None
            or not any(item.kind.value == "toolchain" for item in scenario_results)
            else _artifact_relative(request.artifact_paths.toolchain_results)
        ),
        process_scenario_results_artifact=(
            None
            if request.artifact_paths.process_scenario_results is None
            or not any(item.kind.value == "process_background" for item in scenario_results)
            else _artifact_relative(request.artifact_paths.process_scenario_results)
        ),
        process_cleanup_artifact=(
            None
            if request.artifact_paths.process_cleanup is None
            or not any(
                item.kind.value == "process_background"
                for item in scenario_results
            )
            else _artifact_relative(request.artifact_paths.process_cleanup)
        ),
        process_output_artifacts=[_artifact_relative(path) for path in process_output_paths],
        review_gate_passed=None,
    )


def _build_memory_artifact(
    request: MyHermesWorkerRequest,
    *,
    provider: str,
    adapter,
    query_results: list,
    snapshots: list,
    state_changes: list,
    errors: list[MemoryOperationError],
    clear_attempted: bool,
    clear_succeeded: bool | None,
) -> MemoryArtifact:
    strategy = request.memory_strategy
    if strategy is None:
        raise RuntimeError("cannot build a Memory Artifact without a strategy")
    return MemoryArtifact(
        trial_id=request.trial_id,
        case_id=request.case_id,
        strategy=strategy,
        provider=provider,
        seeded_memory_ids=([] if adapter is None else adapter.seeded_memory_ids),
        query_results=query_results,
        snapshots=snapshots,
        state_changes=state_changes,
        errors=errors,
        clear_attempted=clear_attempted,
        clear_succeeded=clear_succeeded,
    )


def _build_ablation_artifact(
    request: MyHermesWorkerRequest,
    *,
    compression_events: list,
    context_diagnostics: list[ContextDiagnostic],
    fact_context_observations: list[FactContextObservation],
) -> AblationArtifact:
    configuration = request.effective_subject_configuration
    if configuration is None or request.variant_id is None:
        raise RuntimeError("cannot build an Ablation Artifact without a Variant")
    return AblationArtifact(
        trial_id=request.trial_id,
        case_id=request.case_id,
        variant_id=request.variant_id,
        effective_subject_configuration=configuration,
        compression_events=compression_events,
        context_diagnostics=context_diagnostics,
        fact_context_observations=fact_context_observations,
    )


def _build_system_prompt(build_system_prompt, request, enabled_toolsets: list[str]) -> str:
    configuration = request.effective_subject_configuration
    native = request.memory_strategy is RetrievalStrategy.SUBJECT_NATIVE
    include_memory = native if configuration is None else configuration.include_memory
    include_user_profile = (
        native if configuration is None else configuration.include_user_profile
    )
    return build_system_prompt(
        str(request.workspace),
        enabled_toolsets=enabled_toolsets,
        include_soul=False,
        include_memory=include_memory,
        include_user_profile=include_user_profile,
        include_project_context=False,
    )


def _observe_p4_turn(
    request: MyHermesWorkerRequest,
    *,
    turn_index: int,
    session_id: str,
    previous_session_id: str | None,
    response: object,
    estimate_context_tokens,
    fact_by_id: dict,
    checkpoints: list,
) -> tuple[ContextDiagnostic, list[FactContextObservation]]:
    safe_session_id = _safe_identifier(session_id)
    session_changed = (
        previous_session_id is not None and previous_session_id != session_id
    )
    messages = response.get("messages") if isinstance(response, Mapping) else None
    if not isinstance(messages, list) or any(
        not isinstance(item, Mapping) for item in messages
    ):
        diagnostic = ContextDiagnostic(
            session_id=safe_session_id,
            turn_index=turn_index,
            token_source=TokenCountSource.UNAVAILABLE,
            compression_applied=None,
            session_changed=session_changed,
            status=DiagnosticStatus.ERROR,
            error_type="short_term_context_error",
        )
        observations = [
            FactContextObservation(
                fact_id=fact_id,
                checkpoint_id=checkpoint.checkpoint_id,
                turn_index=turn_index,
                session_id=safe_session_id,
                matched=None,
                compression_applied=None,
                session_changed=session_changed,
                error_type="fact_retention_error",
            )
            for checkpoint in checkpoints
            for fact_id in checkpoint.required_fact_ids
        ]
        return diagnostic, observations

    evidence = [
        content
        for message in messages
        if isinstance((content := message.get("content")), str)
    ]
    token_count: int | None = None
    token_source = TokenCountSource.UNAVAILABLE
    diagnostic_status = DiagnosticStatus.PARTIAL
    if callable(estimate_context_tokens):
        try:
            estimated = estimate_context_tokens(messages)
            if type(estimated) is not int or estimated < 0:
                raise ValueError("invalid context token estimate")
            token_count = estimated
            token_source = TokenCountSource.AUDIT_ESTIMATED
            diagnostic_status = DiagnosticStatus.AVAILABLE
        except Exception:
            token_count = None
    diagnostic = ContextDiagnostic(
        session_id=safe_session_id,
        turn_index=turn_index,
        message_count=len(messages),
        estimated_or_reported_token_count=token_count,
        token_source=token_source,
        compression_applied=None,
        session_changed=session_changed,
        status=diagnostic_status,
    )
    observations: list[FactContextObservation] = []
    for checkpoint in checkpoints:
        for fact_id in checkpoint.required_fact_ids:
            fact = fact_by_id.get(fact_id)
            if fact is None:
                observations.append(
                    FactContextObservation(
                        fact_id=fact_id,
                        checkpoint_id=checkpoint.checkpoint_id,
                        turn_index=turn_index,
                        session_id=safe_session_id,
                        matched=None,
                        compression_applied=None,
                        session_changed=session_changed,
                        error_type="required_fact_validation_error",
                    )
                )
                continue
            matched = match_required_fact(evidence, fact, include_value=False)
            distortion = (
                None
                if matched is not None
                else match_distortion_candidate(
                    evidence,
                    fact,
                    include_value=False,
                )
            )
            observations.append(
                FactContextObservation(
                    fact_id=fact.fact_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                    turn_index=turn_index,
                    session_id=safe_session_id,
                    matched=matched is not None,
                    matched_projection=matched,
                    distortion_type=(
                        None if distortion is None else distortion[0].distortion_type
                    ),
                    distortion_projection=(
                        None if distortion is None else distortion[1]
                    ),
                    compression_applied=None,
                    session_changed=session_changed,
                )
            )
    return diagnostic, observations


def _project_public_compression_observations(
    request: MyHermesWorkerRequest,
    *,
    turns: list[TurnResult],
    observations: ObservationBundle,
) -> tuple[list[CompressionEvent], dict[int, bool | None]]:
    """Project only public ModelCall fields; absence remains unknown."""

    configuration = request.effective_subject_configuration
    if configuration is None:
        return [], {}
    calls_by_run: dict[str, list] = {}
    for item in observations.model_calls:
        calls_by_run.setdefault(item.run_id, []).append(item)
    applied_by_turn: dict[int, bool | None] = {}
    events: list[CompressionEvent] = []
    compression_seen = False
    observation_gap_seen = False
    for turn in turns:
        calls = [] if turn.run_id is None else calls_by_run.get(turn.run_id, [])
        values = [item.compression_applied for item in calls]
        compression_seen = compression_seen or any(
            item is True for item in values
        )
        observation_gap_seen = observation_gap_seen or not values or any(
            item is None for item in values
        )
        applied: bool | None = (
            True
            if compression_seen
            else (None if observation_gap_seen else False)
        )
        applied_by_turn[turn.turn_number] = applied
        for call_index, call in enumerate(calls, start=1):
            if call.compression_applied is not True:
                continue
            events.append(
                CompressionEvent(
                    event_id=(
                        f"compression-{turn.turn_number}-{call_index}"
                    ),
                    session_id=turn.session_id,
                    turn_index=turn.turn_number,
                    trigger="subject_public_observation",
                    input_message_count=call.input_message_count,
                    output_message_count=call.output_message_count,
                    status=CompressionEventStatus.COMPLETED,
                )
            )
    if len(events) > configuration.maximum_compression_events:
        raise CompressionLimitError(
            "Subject Compression events exceed the declared P4 limit",
            variant_id=request.variant_id,
            observed_event_count=len(events),
            maximum_compression_events=(
                configuration.maximum_compression_events
            ),
        )
    return events, applied_by_turn


def _run_memory_queries(
    adapter,
    plans: list[MemoryQueryPlan],
    *,
    phase: MemoryQueryPhase,
    results: list,
    errors: list[MemoryOperationError],
) -> None:
    for plan in plans:
        if plan.phase is not phase:
            continue
        try:
            results.append(
                asyncio.run(
                    adapter.query(
                        plan.query,
                        query_id=plan.query_id,
                        phase=plan.phase,
                    )
                )
            )
        except Exception as exc:
            errors.append(
                _memory_operation_error(
                    exc,
                    operation="query",
                    fallback=MemoryErrorType.QUERY,
                    plan=plan,
                )
            )


def _memory_operation_error(
    error: Exception,
    *,
    operation: str,
    fallback: MemoryErrorType,
    plan: MemoryQueryPlan | None = None,
) -> MemoryOperationError:
    error_type = fallback
    if isinstance(error, AuditError):
        try:
            error_type = MemoryErrorType(error.code)
        except ValueError:
            error_type = fallback
    return MemoryOperationError(
        error_type=error_type,
        operation=operation,
        message=f"Memory operation failed: {error_type.value}",
        query_id=None if plan is None else plan.query_id,
        phase=None if plan is None else plan.phase,
        retryable=False,
        details={"exception_type": type(error).__name__},
    )


def _assert_public_capability_boundary(
    *,
    browser_config: object,
    background_review_config: object,
) -> None:
    if (
        not isinstance(background_review_config, Mapping)
        or background_review_config.get("enabled") is not False
    ):
        raise RuntimeError("background review must be disabled")
    if (
        not isinstance(browser_config, Mapping)
        or browser_config.get("enabled") is not False
    ):
        raise RuntimeError("browser must be disabled")


def _safe_status(value: object, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:128]
    return default


def _safe_identifier(value: str) -> str:
    normalized = _IDENTIFIER_CHARACTER.sub("_", value.strip())[:128]
    if not normalized or not normalized[0].isalnum():
        normalized = f"error-{normalized}"[:128]
    return normalized


def _optional_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _safe_identifier(value)


def _nonnegative_int(value: object) -> int:
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise RuntimeError("MyHermes returned an invalid non-negative counter")
    return value


def _complete_optional_sum(values) -> int | None:
    items = list(values)
    if not items or any(value is None for value in items):
        return None
    return sum(items)


def _worker_warning(warning_type: str, error: Exception) -> WorkerWarning:
    return WorkerWarning(
        warning_type=warning_type,
        message=f"MyHermes worker lifecycle warning: {type(error).__name__}",
    )


def _background_review_error(
    error_type: str,
    stage: str,
    message: str,
    error: Exception | None = None,
) -> BackgroundReviewExecutionError:
    return BackgroundReviewExecutionError(
        error_type=_safe_identifier(error_type),
        stage=_safe_identifier(stage),
        message=message,
        retryable=False,
        exception_type=(
            None if error is None else _safe_identifier(type(error).__name__)
        ),
    )


def _background_review_failure_result(
    plan,
    error_type: str,
    message: str,
) -> BackgroundReviewExecutionResult:
    execution_error = _background_review_error(
        error_type,
        "worker_fallback",
        message,
    )
    attempts = [
        ReviewAttempt(
            sequence=1,
            claim_valid=False,
            loop_executed=False,
            model_call_count=0,
            tool_call_count=0,
            state_change_count=0,
            error_type=execution_error.error_type,
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
                error_type=execution_error.error_type,
            )
        )
    return BackgroundReviewExecutionResult(
        review_id=plan.review_id,
        kind=plan.kind,
        lifecycle=plan.lifecycle,
        status=ReviewStatus.FAILED,
        actual_action=ReviewAction.NO_OP,
        outcome=ReviewOutcome(
            review_id=plan.review_id,
            kind=plan.kind,
            status=ReviewStatus.FAILED,
            error=ReviewError(error_type=execution_error.error_type, message=message),
        ),
        attempts=attempts,
        attempt_count=len(attempts),
        duplicate_rejected=(plan.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE),
        duration_ms=0,
        errors=[execution_error],
    )


def _write_background_review_artifacts(
    request: MyHermesWorkerRequest,
    results: list[BackgroundReviewExecutionResult],
    errors: list[BackgroundReviewExecutionError],
) -> None:
    if not request.background_review_plans:
        return
    paths = request.artifact_paths
    if (
        paths.background_review_results is None
        or paths.background_review_evidence is None
        or paths.background_review_snapshots is None
    ):
        raise RuntimeError("P5 worker request has incomplete Review Artifact paths")
    atomic_write_json(
        paths.background_review_results,
        BackgroundReviewArtifact(
            trial_id=request.trial_id,
            case_id=request.case_id,
            results=results,
            errors=errors,
        ),
    )
    atomic_write_json(
        paths.background_review_evidence,
        BackgroundReviewEvidenceArtifact(
            trial_id=request.trial_id,
            case_id=request.case_id,
            results=results,
        ),
    )
    atomic_write_json(
        paths.background_review_snapshots,
        BackgroundReviewSnapshotsArtifact(
            trial_id=request.trial_id,
            case_id=request.case_id,
            results=results,
        ),
    )


def _checkpoint_background_review_artifacts(
    request: MyHermesWorkerRequest,
    results: list[BackgroundReviewExecutionResult],
    errors: list[BackgroundReviewExecutionError],
    warnings: list[WorkerWarning],
) -> None:
    """Best-effort checkpoint after each finished Review plan.

    Each Artifact write is atomic.  The parent can recover the self-contained
    result projection if process termination happens between the three files,
    then regenerate a consistent Artifact set without discarding completed
    Review facts.
    """

    try:
        _write_background_review_artifacts(request, results, errors)
    except Exception as exc:
        if not any(
            warning.warning_type == "background_review_artifact_checkpoint_error"
            for warning in warnings
        ):
            warnings.append(
                _worker_warning("background_review_artifact_checkpoint_error", exc)
            )


def _append_background_review_result(
    request: MyHermesWorkerRequest,
    results: list[BackgroundReviewExecutionResult],
    errors: list[BackgroundReviewExecutionError],
    warnings: list[WorkerWarning],
    review_result: BackgroundReviewExecutionResult,
) -> None:
    """Record one plan fact and immediately publish a recoverable checkpoint."""

    results.append(review_result)
    errors.extend(review_result.errors)
    _checkpoint_background_review_artifacts(request, results, errors, warnings)


def _artifact_relative(path: Path | None) -> str:
    if path is None:
        raise RuntimeError("worker Artifact path is unavailable")
    return f"artifacts/{path.name}"


def _recover_background_review_results(
    request: MyHermesWorkerRequest,
) -> tuple[
    list[BackgroundReviewExecutionResult],
    list[BackgroundReviewExecutionError],
]:
    """Recover only verified, already-published P5 plan facts after a crash."""

    if (
        not request.background_review_plans
        or request.artifact_paths.background_review_results is None
    ):
        return [], []
    path = request.artifact_paths.background_review_results
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            return [], []
        artifact = BackgroundReviewArtifact.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError, ValidationError):
        return [], []
    if artifact.trial_id != request.trial_id or artifact.case_id != request.case_id:
        return [], []
    planned = {plan.review_id: plan for plan in request.background_review_plans}
    recovered_by_id = {item.review_id: item for item in artifact.results}
    if any(
        review_id not in planned
        or item.kind is not planned[review_id].kind
        or item.lifecycle is not planned[review_id].lifecycle
        for review_id, item in recovered_by_id.items()
    ):
        return [], []
    return (
        [
            recovered_by_id[plan.review_id]
            for plan in request.background_review_plans
            if plan.review_id in recovered_by_id
        ],
        list(artifact.errors),
    )


def _merge_background_review_results(
    plans: Sequence,
    recovered: Sequence[BackgroundReviewExecutionResult],
    *,
    error_type: str,
    message: str,
) -> list[BackgroundReviewExecutionResult]:
    """Preserve recovered facts and add fallback facts only for missing plans."""

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


def _merge_background_review_errors(
    recovered: Sequence[BackgroundReviewExecutionError],
    results: Sequence[BackgroundReviewExecutionResult],
) -> list[BackgroundReviewExecutionError]:
    """Keep global checkpoint diagnostics as well as per-plan diagnostics."""

    merged = list(recovered)
    for result in results:
        for error in result.errors:
            if error not in merged:
                merged.append(error)
    return merged


def _failure_result(
    request: MyHermesWorkerRequest,
    *,
    error_type: str,
    exception_type: str,
    duration_ms: int,
    recovered_background_review_results: Sequence[BackgroundReviewExecutionResult] = (),
    recovered_background_review_errors: Sequence[BackgroundReviewExecutionError] = (),
) -> MyHermesWorkerResult:
    memory_errors = (
        []
        if request.memory_strategy is None
        else [
            MemoryOperationError(
                error_type=MemoryErrorType.PROTOCOL,
                operation="worker",
                message="Memory pipeline ended during Worker failure",
                details={"exception_type": exception_type},
            )
        ]
    )
    background_results = _merge_background_review_results(
        request.background_review_plans,
        recovered_background_review_results,
        error_type="background_review_protocol_error",
        message="Background Review pipeline ended during Worker failure",
    )
    background_errors = _merge_background_review_errors(
        recovered_background_review_errors,
        background_results,
    )
    return MyHermesWorkerResult(
        worker_status=WorkerStatus.FAILED,
        runtime_status="worker_error",
        final_output=None,
        turns=[],
        run_ids=[],
        error_type=error_type,
        fatal=True,
        retryable=False,
        duration_ms=duration_ms,
        observations_artifact=_artifact_relative(
            request.artifact_paths.observations
        ),
        transcript_artifact=_artifact_relative(request.artifact_paths.transcript),
        memory_artifact=(
            None
            if request.memory_strategy is None
            else _artifact_relative(request.artifact_paths.memory)
        ),
        memory_errors=memory_errors,
        variant_id=request.variant_id,
        effective_subject_configuration=request.effective_subject_configuration,
        ablation_artifact=(
            None
            if request.effective_subject_configuration is None
            else _artifact_relative(request.artifact_paths.ablation)
        ),
        background_review_results_artifact=(
            None
            if not request.background_review_plans
            else _artifact_relative(request.artifact_paths.background_review_results)
        ),
        background_review_evidence_artifact=(
            None
            if not request.background_review_plans
            else _artifact_relative(request.artifact_paths.background_review_evidence)
        ),
        background_review_snapshots_artifact=(
            None
            if not request.background_review_plans
            else _artifact_relative(request.artifact_paths.background_review_snapshots)
        ),
        background_review_results=background_results,
        background_review_errors=background_errors,
        error=WorkerError(
            error_type=error_type,
            message=f"MyHermes worker failed: {exception_type}",
        ),
    )


def _recover_ablation_artifact(
    request: MyHermesWorkerRequest,
) -> AblationArtifact | None:
    configuration = request.effective_subject_configuration
    path = request.artifact_paths.ablation
    if configuration is None or request.variant_id is None or path is None:
        return None
    recovered: AblationArtifact | None = None
    try:
        if (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size <= 8 * 1024 * 1024
        ):
            candidate = AblationArtifact.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if (
                candidate.trial_id == request.trial_id
                and candidate.case_id == request.case_id
                and candidate.variant_id == request.variant_id
                and candidate.effective_subject_configuration == configuration
            ):
                recovered = candidate
    except (OSError, UnicodeError, ValueError, ValidationError):
        recovered = None
    if recovered is None:
        recovered = AblationArtifact(
            trial_id=request.trial_id,
            case_id=request.case_id,
            variant_id=request.variant_id,
            effective_subject_configuration=configuration,
        )
    atomic_write_json(path, recovered)
    return recovered


def _recover_memory_artifact(
    request: MyHermesWorkerRequest,
    protocol_errors: list[MemoryOperationError],
) -> MemoryArtifact | None:
    if request.memory_strategy is None or request.artifact_paths.memory is None:
        return None
    path = request.artifact_paths.memory
    recovered: MemoryArtifact | None = None
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 8 * 1024 * 1024:
            candidate = MemoryArtifact.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if (
                candidate.trial_id == request.trial_id
                and candidate.case_id == request.case_id
                and candidate.strategy is request.memory_strategy
            ):
                recovered = candidate
    except (OSError, UnicodeError, ValueError, ValidationError):
        recovered = None
    if recovered is None:
        recovered = MemoryArtifact(
            trial_id=request.trial_id,
            case_id=request.case_id,
            strategy=request.memory_strategy,
            provider="unavailable",
        )
    recovered = recovered.model_copy(
        update={"errors": [*recovered.errors, *protocol_errors]}
    )
    atomic_write_json(path, recovered)
    return recovered


def main(argv: Sequence[str] | None = None) -> int:
    started = time.perf_counter()
    arguments = _parse_arguments(argv)
    try:
        request_path = arguments.request.resolve(strict=False)
        result_path = arguments.result.resolve(strict=False)
        request = _load_request(request_path)
        _validate_isolation_boundary(
            request,
            request_path=request_path,
            result_path=result_path,
        )
    except Exception as exc:
        print(
            f"worker bootstrap failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    _install_termination_handlers()
    try:
        result = _execute(request)
    except Exception as exc:
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        error_type = (
            "worker_terminated"
            if isinstance(exc, WorkerTerminationRequested)
            else (exc.code if isinstance(exc, AuditError) else "worker_exception")
        )
        (
            recovered_background_review_results,
            recovered_background_review_errors,
        ) = _recover_background_review_results(request)
        result = _failure_result(
            request,
            error_type=error_type,
            exception_type=type(exc).__name__,
            duration_ms=duration_ms,
            recovered_background_review_results=recovered_background_review_results,
            recovered_background_review_errors=recovered_background_review_errors,
        )
        try:
            memory_artifact = _recover_memory_artifact(
                request,
                result.memory_errors,
            )
        except Exception:
            memory_artifact = None
        if memory_artifact is not None:
            result = result.model_copy(
                update={
                    "memory_query_results": memory_artifact.query_results,
                    "memory_snapshots": memory_artifact.snapshots,
                    "memory_state_changes": memory_artifact.state_changes,
                    "memory_errors": memory_artifact.errors,
                }
            )
        try:
            ablation_artifact = _recover_ablation_artifact(request)
        except Exception:
            ablation_artifact = None
        if ablation_artifact is not None:
            result = result.model_copy(
                update={
                    "compression_events": ablation_artifact.compression_events,
                    "context_diagnostics": ablation_artifact.context_diagnostics,
                    "fact_context_observations": (
                        ablation_artifact.fact_context_observations
                    ),
                }
            )
        try:
            _write_background_review_artifacts(
                request,
                result.background_review_results,
                result.background_review_errors,
            )
        except Exception as artifact_exc:
            print(
                "worker Background Review fallback artifact publication failed: "
                f"{type(artifact_exc).__name__}",
                file=sys.stderr,
            )
        empty_observations = ObservationBundle()
        empty_transcript = WorkerTranscript(
            trial_id=request.trial_id,
            case_id=request.case_id,
        )
        try:
            if not request.artifact_paths.observations.exists():
                atomic_write_json(
                    request.artifact_paths.observations,
                    empty_observations,
                )
            if not request.artifact_paths.transcript.exists():
                atomic_write_json(
                    request.artifact_paths.transcript,
                    empty_transcript,
                )
        except Exception as artifact_exc:
            print(
                "worker fallback artifact publication failed: "
                f"{type(artifact_exc).__name__}",
                file=sys.stderr,
            )
    try:
        atomic_write_json(result_path, result)
    except Exception as exc:
        print(
            f"worker result publication failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    return 0 if result.worker_status is WorkerStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
