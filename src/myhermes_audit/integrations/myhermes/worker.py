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
    MemoryErrorType,
    MemoryOperationError,
    MemoryQueryPhase,
    MemorySnapshotPhase,
    RetrievalStrategy,
    TurnResult,
)
from myhermes_audit.errors import AuditError
from myhermes_audit.integrations.myhermes.contracts import (
    MemoryArtifact,
    MemoryQueryPlan,
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
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
from myhermes_audit.security import redact_text, sensitive_environment_values


_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_IDENTIFIER_CHARACTER = re.compile(r"[^A-Za-z0-9._:-]+")


class WorkerTerminationRequested(Exception):
    """Raised by cooperative process-group termination signals."""


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
        if candidate.parent.resolve(strict=True) != artifacts_root:
            raise ValueError("artifact path escaped the Trial artifacts directory")
        if candidate.is_symlink():
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
    try:
        # This is the first hermes import in the worker. All environment and
        # cwd checks above have already completed.
        from hermes.config import (
            BACKGROUND_REVIEW_CONFIG,
            BROWSER_CONFIG,
            DB_PATH,
            HERMES_HOME,
            client,
        )
        from hermes.conversation import run_conversation
        from hermes.hooks import SyncHookRegistry
        from hermes.persistence.core import create_session
        from hermes.persistence.observation import configure_sqlite_observation_sink
        from hermes.persistence.schema import init_db
        from hermes.processes import process_manager as default_process_manager
        from hermes.prompt import build_system_prompt
        from hermes.tools import (
            ExecutionEnvironment,
            ToolPolicy,
            register_all,
            registry,
        )

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
            if memory_blocked:
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
            )
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
            )
        if any(turn.run_id is None for turn in turns):
            failed_status = failed_status or "observation_error"
            failed_error_type = failed_error_type or "observation_unavailable"
            failed_fatal = True
            failed_retryable = False
    finally:
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
        if process_manager is not None:
            try:
                lifecycle_warnings.extend(
                    close_runtime_resources(
                        connection=connection,
                        session_ids=session_ids,
                        process_manager=process_manager,
                        model_client=model_client,
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
        tool_names=tool_names,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
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


def _build_system_prompt(build_system_prompt, request, enabled_toolsets: list[str]) -> str:
    native = request.memory_strategy is RetrievalStrategy.SUBJECT_NATIVE
    return build_system_prompt(
        str(request.workspace),
        enabled_toolsets=enabled_toolsets,
        include_soul=False,
        include_memory=native,
        include_user_profile=native,
        include_project_context=False,
    )


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


def _artifact_relative(path: Path | None) -> str:
    if path is None:
        raise RuntimeError("worker Artifact path is unavailable")
    return f"artifacts/{path.name}"


def _failure_result(
    request: MyHermesWorkerRequest,
    *,
    error_type: str,
    exception_type: str,
    duration_ms: int,
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
        error=WorkerError(
            error_type=error_type,
            message=f"MyHermes worker failed: {exception_type}",
        ),
    )


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
            else "worker_exception"
        )
        result = _failure_result(
            request,
            error_type=error_type,
            exception_type=type(exc).__name__,
            duration_ms=duration_ms,
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
