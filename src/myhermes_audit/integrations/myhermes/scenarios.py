"""Worker-local projections for P6.1 Toolchain and Process scenarios.

Only response messages and the public ObservationBundle are consumed.  Audit
never starts a Process, calls a Tool handler, or imports a ProcessManager.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from myhermes_audit.artifacts import atomic_write_text
from myhermes_audit.contracts import (
    CleanupCheckpoint,
    E2EScenarioKind,
    IncrementalReadObservation,
    OutputCheckpoint,
    ProcessAction,
    ProcessCleanupResult,
    ProcessInputObservation,
    ProcessScenarioExecutionResult,
    ScenarioArtifactObservation,
    ScenarioCheckpointResult,
    ScenarioError,
    ScenarioExecutionResult,
    ScenarioProcessStatus,
    ScenarioStatus,
    ScenarioStepResult,
    ScenarioToolCallObservation,
    StepStatusCheckpoint,
    ProcessStatusCheckpoint,
    ToolchainScenarioExecutionResult,
)
from myhermes_audit.integrations.myhermes.contracts import (
    MyHermesWorkerRequest,
    ObservationBundle,
)
from myhermes_audit.security import redact_text


_MAX_PROCESS_LOG_BYTES = 256 * 1024
_TRUNCATION_MARKER = "\n...[truncated by my-hermes-audit]...\n"


def _safe_id(value: object) -> str:
    return "id-" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_PROCESS_LOG_BYTES:
        return value, False
    budget = max(1, _MAX_PROCESS_LOG_BYTES - len(_TRUNCATION_MARKER.encode("utf-8")))
    half = budget // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    return head + _TRUNCATION_MARKER + tail, True


def _json_content(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [item.get("text", "") for item in value if isinstance(item, Mapping)]
        return _json_content("".join(part for part in parts if isinstance(part, str)))
    return None


def _tool_events(
    responses: Sequence[Mapping[str, object]],
    observations: ObservationBundle,
) -> list[dict[str, object]]:
    """Pair public Tool Calls with their Tool Results in source order."""

    durations = {item.tool_call_id: item.duration_ms for item in observations.tool_calls}
    created_at = {item.tool_call_id: item.created_at for item in observations.tool_calls}
    pending: dict[str, dict[str, object]] = {}
    events: list[dict[str, object]] = []
    for response in responses:
        messages = response.get("messages")
        if not isinstance(messages, Sequence):
            continue
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            calls = message.get("tool_calls")
            if isinstance(calls, Sequence):
                for call in calls:
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    function = function if isinstance(function, Mapping) else call
                    name = function.get("name")
                    arguments = function.get("arguments", {})
                    if not isinstance(name, str):
                        continue
                    call_id = call.get("id")
                    key = str(call_id) if call_id is not None else f"call-{len(pending)}"
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except (TypeError, ValueError):
                            arguments = {}
                    pending[key] = {
                        "name": name,
                        "arguments": arguments if isinstance(arguments, Mapping) else {},
                        "tool_call_id": key,
                    }
            if message.get("role") not in {"tool", "tool_result"}:
                continue
            result = _json_content(message.get("content"))
            if result is None:
                continue
            call_id = message.get("tool_call_id", message.get("id"))
            key = str(call_id) if call_id is not None else None
            event = pending.pop(key, None) if key is not None else None
            if event is None and pending:
                event = pending.pop(next(iter(pending)))
            if event is None:
                event = {
                    "name": message.get("name", ""),
                    "arguments": {},
                    "tool_call_id": key or f"result-{len(events)}",
                }
            event = dict(event)
            event["result"] = result
            event["duration_ms"] = durations.get(str(event["tool_call_id"]))
            event["created_at"] = created_at.get(str(event["tool_call_id"]))
            events.append(event)
    return events


def _relative_target(workspace: Path, hermes_home: Path, relative_path: str) -> Path:
    parts = Path(relative_path).parts
    if not parts or parts[0] not in {"workspace", "hermes_home"}:
        raise ValueError("scenario Artifact path must be workspace/ or hermes_home/")
    root = (workspace if parts[0] == "workspace" else hermes_home).resolve(strict=True)
    current = root
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("scenario Artifact path traverses a symbolic link")
    candidate = current.resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError("scenario Artifact path escaped its root")
    return candidate


def _artifact_observation(workspace: Path, hermes_home: Path, relative_path: str) -> ScenarioArtifactObservation:
    target = _relative_target(workspace, hermes_home, relative_path)
    try:
        if not target.is_file() or target.is_symlink():
            return ScenarioArtifactObservation(relative_path=relative_path, exists=False)
        payload = target.read_bytes()
    except OSError:
        return ScenarioArtifactObservation(relative_path=relative_path, exists=False)
    return ScenarioArtifactObservation(
        relative_path=relative_path,
        exists=True,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _status(value: object) -> ScenarioProcessStatus:
    normalized = str(value or "").strip().lower()
    if normalized in {"starting", "starting_up"}:
        return ScenarioProcessStatus.STARTING
    if normalized in {"running", "active"}:
        return ScenarioProcessStatus.RUNNING
    if normalized in {"waiting_for_input", "waiting", "awaiting_input"}:
        return ScenarioProcessStatus.WAITING_FOR_INPUT
    if normalized in {"completed", "exited", "exit"}:
        return ScenarioProcessStatus.COMPLETED
    if normalized in {"failed", "failed_start", "lost", "error"}:
        return ScenarioProcessStatus.FAILED
    if normalized in {"interrupted", "interrupt"}:
        return ScenarioProcessStatus.INTERRUPTED
    if normalized in {"killed", "terminated"}:
        return ScenarioProcessStatus.KILLED
    if normalized in {"timed_out", "timeout"}:
        return ScenarioProcessStatus.TIMED_OUT
    return ScenarioProcessStatus.UNKNOWN


def _scenario_error(error_type: str, message: str, *, step_id: str | None = None) -> ScenarioError:
    return ScenarioError(error_type=error_type.replace("_", "-"), message=message, step_id=step_id)


def _event_result(event: Mapping[str, object] | None) -> Mapping[str, object]:
    result = event.get("result") if event else None
    return result if isinstance(result, Mapping) else {}


def _event_arguments(event: Mapping[str, object] | None) -> Mapping[str, object]:
    arguments = event.get("arguments") if event else None
    return arguments if isinstance(arguments, Mapping) else {}


def _event_process_id(event: Mapping[str, object] | None) -> str | None:
    result = _event_result(event)
    direct = result.get("process_id")
    if isinstance(direct, str) and direct:
        return direct
    nested = result.get("process")
    if isinstance(nested, Mapping) and isinstance(nested.get("process_id"), str):
        return nested["process_id"]
    arguments = _event_arguments(event)
    return arguments.get("process_id") if isinstance(arguments.get("process_id"), str) else None


def _event_status(event: Mapping[str, object] | None) -> ScenarioProcessStatus:
    result = _event_result(event)
    nested = result.get("process")
    nested_status = nested.get("status") if isinstance(nested, Mapping) else None
    return _status(result.get("status") or nested_status)


def _event_matches_step(event: Mapping[str, object] | None, step) -> bool:
    """Require the observed public tool operation to match the typed step."""

    if event is None:
        return False
    name = event.get("name")
    arguments = _event_arguments(event)
    result = _event_result(event)
    observed_action = result.get("action") or arguments.get("action")
    if step.action is ProcessAction.START:
        return name == "terminal" and arguments.get("background") is True
    if name != "process":
        return False
    expected = {
        ProcessAction.READ_INCREMENTAL: "log",
        ProcessAction.SEND_INPUT: "submit" if step.submit else "write",
        ProcessAction.WAIT: "wait",
        ProcessAction.INTERRUPT: "interrupt",
        ProcessAction.KILL: "kill",
        ProcessAction.CLOSE: "close",
        ProcessAction.ASSERT_STATUS: "poll",
    }.get(step.action)
    return expected is not None and observed_action == expected


def _scenario_tool_call_seen(events: Sequence[Mapping[str, object]], expected) -> bool:
    for event in events:
        if event.get("name") != expected.tool_name:
            continue
        if not expected.arguments:
            return True
        actual = event.get("arguments")
        if isinstance(actual, Mapping) and all(
            actual.get(key) == value for key, value in expected.arguments.items()
        ):
            return True
    return False


def _fixture_text_facts(workspace: Path, hermes_home: Path, relative_path: str) -> tuple[str, int, int] | None:
    try:
        target_path = (
            relative_path
            if relative_path.split("/", 1)[0] in {"workspace", "hermes_home"}
            else f"workspace/{relative_path}"
        )
        path = _relative_target(workspace, hermes_home, target_path)
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    return _hash_text(text), len(text), len(text.encode("utf-8"))


def _cleanup_result(
    reports: Sequence[Mapping[str, object]],
    *,
    required: bool,
    expect_no_live_processes: bool,
    expect_session_resources_released: bool,
    process_id_safe: str | None,
) -> ProcessCleanupResult:
    complete = bool(reports) and all(item.get("complete") is True for item in reports)
    unresolved = [
        str(identifier)
        for item in reports
        for identifier in (item.get("unresolved_ids") or [])
        if isinstance(identifier, str)
    ]
    attempted_count = sum(
        int(item.get("attempted_count", 0))
        for item in reports
        if isinstance(item.get("attempted_count", 0), int)
    )
    completed_count = sum(
        int(item.get("completed_count", 0))
        for item in reports
        if isinstance(item.get("completed_count", 0), int)
    )
    cleanup_errors = [] if reports else ["cleanup-report-missing"]
    return ProcessCleanupResult(
        required=required,
        expect_no_live_processes=expect_no_live_processes,
        expect_session_resources_released=expect_session_resources_released,
        live_process_count_before=attempted_count,
        live_process_count_after=len(unresolved),
        session_cleanup_completed=complete,
        cleanup_errors=cleanup_errors,
        attempted_process_ids=[process_id_safe] if process_id_safe else [],
        completed_process_ids=[process_id_safe] if process_id_safe and complete else [],
        unresolved_process_ids=unresolved or ([process_id_safe] if process_id_safe and not complete else []),
    )


def build_scenario_results(
    request: MyHermesWorkerRequest,
    *,
    responses: Sequence[Mapping[str, object]],
    observations: ObservationBundle,
    turns,
    cleanup_reports: Sequence[Mapping[str, object]],
    sensitive_values: Sequence[str] = (),
) -> tuple[list[ScenarioExecutionResult], list[ScenarioError], list[Path]]:
    events = _tool_events(responses, observations)
    results: list[ScenarioExecutionResult] = []
    errors: list[ScenarioError] = []
    log_paths: list[Path] = []
    completed = len(turns) == len(request.turns) and all(
        item.runtime_status == "completed" for item in turns
    )
    for plan in request.scenarios:
        if plan.kind is E2EScenarioKind.TOOLCHAIN:
            calls: list[ScenarioToolCallObservation] = []
            requirement_names = list(dict.fromkeys(
                [item.tool_name for item in plan.trace_requirements]
                + [item.tool_name for item in plan.required_tool_calls]
                + [item.tool_name for item in plan.forbidden_tool_calls]
            ))
            for tool_name in requirement_names:
                matching = [item for item in observations.tool_calls if item.tool_name == tool_name]
                calls.append(ScenarioToolCallObservation(
                    tool_name=tool_name,
                    call_count=len(matching),
                    successful_count=sum(item.success for item in matching),
                ))
            outputs = [_artifact_observation(request.workspace, request.hermes_home, item) for item in plan.output_artifacts]
            inputs = [_artifact_observation(request.workspace, request.hermes_home, item) for item in plan.input_artifacts]
            checkpoint_results: list[ScenarioCheckpointResult] = []
            for checkpoint in plan.checkpoints:
                passed = completed
                if isinstance(checkpoint, OutputCheckpoint):
                    target = inputs if checkpoint.artifact_scope == "input" else outputs
                    passed = passed and all(item.exists for item in target)
                checkpoint_results.append(ScenarioCheckpointResult(
                    checkpoint_id=checkpoint.checkpoint_id,
                    kind=checkpoint.kind,
                    required=checkpoint.required,
                    target_step_id=getattr(checkpoint, "target_step_id", None),
                    artifact_scope=getattr(checkpoint, "artifact_scope", None),
                    passed=passed,
                    observed_step_status=(ScenarioStatus.COMPLETED if passed else ScenarioStatus.FAILED),
                ))
            trace_ok = all(
                any(
                    item.tool_name == req.tool_name
                    and item.call_count >= req.minimum_calls
                    and item.successful_count >= req.minimum_successful_calls
                    for item in calls
                )
                for req in plan.trace_requirements if req.required
            )
            required_ok = all(_scenario_tool_call_seen(events, req) for req in plan.required_tool_calls)
            forbidden_ok = all(not _scenario_tool_call_seen(events, req) for req in plan.forbidden_tool_calls)
            if observations.truncated:
                trace_ok = required_ok = forbidden_ok = False
            status = ScenarioStatus.COMPLETED if (
                completed and trace_ok and required_ok and forbidden_ok
                and not observations.truncated and all(item.exists for item in inputs + outputs)
            ) else ScenarioStatus.FAILED
            results.append(ToolchainScenarioExecutionResult(
                scenario_id=plan.scenario_id,
                status=status,
                checkpoints=checkpoint_results,
                input_artifacts=inputs,
                output_artifacts=outputs,
                tool_calls=calls,
                final_response_present=bool(turns and turns[-1].final_output),
                duration_ms=sum(item.duration_ms for item in turns),
                errors=[] if status is ScenarioStatus.COMPLETED else [_scenario_error("toolchain-gate-failed", "declared Toolchain observation was incomplete")],
            ))
            continue

        process_events = [item for item in events if item.get("name") in {"terminal", "process"}]
        event_index = 0
        start_process_id: str | None = None
        safe_process_id: str | None = None
        declared_command: str | None = None
        actual_command: str | None = None
        step_results: list[ScenarioStepResult] = []
        incremental: list[IncrementalReadObservation] = []
        input_events: list[ProcessInputObservation] = []
        read_by_step: dict[str, IncrementalReadObservation] = {}
        output_log = ""
        cursor = 0
        cursor_integrity = True
        input_matched: bool | None = None
        command_matched: bool | None = None
        process_identity_matched = True
        status_transitions_valid = True
        status_history: list[ScenarioProcessStatus] = []
        initial_status: ScenarioProcessStatus | None = None
        final_status = ScenarioProcessStatus.UNKNOWN
        agent_close_observed = False
        elapsed_ms = 0
        scenario_timed_out = False
        for step in plan.steps:
            event = process_events[event_index] if event_index < len(process_events) else None
            event_index += 1
            result_payload = _event_result(event)
            arguments = _event_arguments(event)
            actual_action = result_payload.get("action") or arguments.get("action")
            actual_status = _event_status(event) if event is not None else None
            event_process_id = _event_process_id(event)
            event_safe_process_id = _safe_id(event_process_id) if event_process_id else None
            action_matched = _event_matches_step(event, step)
            identity_match = (
                event_process_id is not None
                and start_process_id is not None
                and event_process_id == start_process_id
            ) if step.action is not ProcessAction.START else True
            if step.action is ProcessAction.START:
                start_process_id = event_process_id
                safe_process_id = _safe_id(start_process_id) if start_process_id else None
                declared_command = step.command
                actual_value = arguments.get("command")
                actual_command = actual_value if isinstance(actual_value, str) else None
                command_matched = (
                    action_matched and actual_command == declared_command
                    and arguments.get("background") is True and start_process_id is not None
                )
                if start_process_id is None:
                    process_identity_matched = False
                initial_status = actual_status
            elif step.action is ProcessAction.SEND_INPUT:
                expected = _fixture_text_facts(request.workspace, request.hermes_home, step.input_source)
                actual_value = arguments.get("data")
                actual = actual_value if isinstance(actual_value, str) else None
                expected_hash, expected_chars, expected_bytes = expected or (None, None, None)
                actual_hash = _hash_text(actual) if actual is not None else None
                actual_chars = len(actual) if actual is not None else None
                actual_bytes = len(actual.encode("utf-8")) if actual is not None else None
                input_matched = (
                    expected is not None and actual is not None
                    and expected_hash == actual_hash
                    and expected_chars == actual_chars
                    and expected_bytes == actual_bytes
                    and action_matched and identity_match
                )
                input_events.append(ProcessInputObservation(
                    input_source=step.input_source,
                    submitted=step.submit,
                    accepted=result_payload.get("ok") is True,
                    expected_input_sha256=expected_hash,
                    actual_input_sha256=actual_hash,
                    expected_input_char_length=expected_chars,
                    actual_input_char_length=actual_chars,
                    expected_input_utf8_bytes=expected_bytes,
                    actual_input_utf8_bytes=actual_bytes,
                    input_matched=input_matched,
                    process_id_safe=event_safe_process_id,
                    process_identity_matched=identity_match,
                    action_matched=action_matched,
                    bytes_written=(result_payload.get("bytes_written") if type(result_payload.get("bytes_written")) is int and result_payload.get("bytes_written") >= 0 else None),
                ))
            elif step.action is ProcessAction.READ_INCREMENTAL:
                output_value = result_payload.get("output")
                output = output_value if isinstance(output_value, str) else ""
                requested = result_payload.get("requested_cursor")
                next_cursor = result_payload.get("next_cursor")
                requested_ok = isinstance(requested, int) and requested >= 0 and requested == step.cursor_before and requested == cursor
                next_ok = isinstance(next_cursor, int) and next_cursor >= requested if isinstance(requested, int) else False
                char_length = len(output)
                delta_ok = next_ok and next_cursor - requested == char_length if isinstance(requested, int) and isinstance(next_cursor, int) else False
                available = result_payload.get("available_from_cursor")
                available_ok = not (isinstance(available, int) and isinstance(requested, int) and available > requested)
                valid_read = action_matched and identity_match and requested_ok and delta_ok and available_ok and result_payload.get("output_truncated") is not True
                if not valid_read:
                    cursor_integrity = False
                    accepted_output = ""
                    next_value = cursor
                else:
                    accepted_output = output
                    next_value = next_cursor
                required_found = [marker for marker in step.required_markers if marker in accepted_output]
                forbidden_found = [marker for marker in step.forbidden_markers if marker in accepted_output]
                read = IncrementalReadObservation(
                    read_index=len(incremental),
                    cursor_before=cursor,
                    cursor_after=next_value,
                    new_output_char_length=len(accepted_output),
                    new_output_utf8_bytes=len(accepted_output.encode("utf-8")),
                    content_sha256=_hash_text(accepted_output) if accepted_output else None,
                    required_markers_found=[_safe_id(marker) for marker in required_found],
                    required_markers_missing=[_safe_id(marker) for marker in step.required_markers if marker not in accepted_output],
                    forbidden_markers_found=[_safe_id(marker) for marker in forbidden_found],
                    truncated=result_payload.get("output_truncated") is True,
                )
                incremental.append(read)
                read_by_step[step.step_id] = read
                cursor = next_value
                output_log += accepted_output
            elif step.action is ProcessAction.CLOSE:
                agent_close_observed = bool(action_matched and identity_match and result_payload.get("ok") is True)
            if (
                actual_status is not None
                and actual_status is not ScenarioProcessStatus.UNKNOWN
                and step.action is not ProcessAction.CLOSE
            ):
                if status_history and status_history[-1] in {ScenarioProcessStatus.COMPLETED, ScenarioProcessStatus.FAILED, ScenarioProcessStatus.INTERRUPTED, ScenarioProcessStatus.KILLED, ScenarioProcessStatus.TIMED_OUT} and actual_status in {ScenarioProcessStatus.STARTING, ScenarioProcessStatus.RUNNING, ScenarioProcessStatus.WAITING_FOR_INPUT}:
                    status_transitions_valid = False
                status_history.append(actual_status)
                final_status = actual_status
            duration_raw = event.get("duration_ms") if event else None
            duration_ms = duration_raw if type(duration_raw) is int and duration_raw >= 0 else None
            timed_out = duration_ms is not None and duration_ms > step.timeout_seconds * 1000
            elapsed_ms += duration_ms or 0
            observation_completed_at = event.get("created_at") if event else None
            if not isinstance(observation_completed_at, datetime):
                observation_completed_at = None
            observation_started_at = (
                observation_completed_at - timedelta(milliseconds=duration_ms)
                if observation_completed_at is not None and duration_ms is not None
                else None
            )
            if timed_out:
                scenario_timed_out = True
            if step.action is ProcessAction.WAIT:
                wait_timeout = arguments.get("timeout", 30)
                wait_ok = (
                    isinstance(wait_timeout, (int, float))
                    and not isinstance(wait_timeout, bool)
                    and wait_timeout >= 0
                    and wait_timeout <= step.maximum_wait_seconds
                    and wait_timeout <= step.timeout_seconds
                    and wait_timeout <= max(
                        0,
                        plan.timeout_seconds
                        - elapsed_ms / 1000
                        + (duration_ms or 0) / 1000,
                    )
                )
            else:
                wait_ok = True
            if step.action is ProcessAction.READ_INCREMENTAL:
                read = read_by_step[step.step_id]
                semantic_ok = read.new_output_char_length >= step.minimum_new_output_length and not read.required_markers_missing and not read.forbidden_markers_found
            elif step.action is ProcessAction.SEND_INPUT:
                semantic_ok = bool(input_events[-1].input_matched)
            elif step.action is ProcessAction.WAIT:
                semantic_ok = actual_status is step.expected_status and wait_ok
            elif step.action in {ProcessAction.INTERRUPT, ProcessAction.KILL}:
                semantic_ok = actual_status is step.expected_terminal_status
            elif step.action is ProcessAction.ASSERT_STATUS:
                semantic_ok = actual_status is step.expected_status
            else:
                semantic_ok = True
            passed = bool(action_matched and (identity_match or step.action is ProcessAction.START) and result_payload.get("ok", True) is not False and semantic_ok and not timed_out)
            if step.action is ProcessAction.START:
                passed = passed and actual_status is step.expected_initial_status and bool(command_matched)
            step_results.append(ScenarioStepResult(
                step_id=step.step_id,
                action=step.action,
                status=ScenarioStatus.COMPLETED if passed else ScenarioStatus.ERROR,
                actual_action=str(actual_action) if isinstance(actual_action, str) else None,
                actual_status=actual_status,
                started_at=observation_started_at,
                completed_at=observation_completed_at,
                duration_ms=duration_ms,
                timeout_seconds=step.timeout_seconds,
                timed_out=timed_out,
                observation_refs=[_safe_id(event.get("tool_call_id"))] if event else [],
                expected_process_id_safe=safe_process_id,
                actual_process_id_safe=event_safe_process_id,
                process_identity_matched=(True if step.action is ProcessAction.START else identity_match),
                action_matched=action_matched,
                error=None if passed else _scenario_error(
                    "process-observation-missing" if event is None else "process-step-gate-failed",
                    "public Process observation did not satisfy the declared step",
                    step_id=step.step_id,
                ),
            ))
            if not identity_match and step.action is not ProcessAction.START:
                process_identity_matched = False
        cleanup_plan = plan.cleanup
        cleanup = _cleanup_result(
            cleanup_reports,
            required=cleanup_plan.required if cleanup_plan else False,
            expect_no_live_processes=cleanup_plan.expect_no_live_processes if cleanup_plan else False,
            expect_session_resources_released=cleanup_plan.expect_session_resources_released if cleanup_plan else False,
            process_id_safe=safe_process_id,
        )
        required_steps_ok = all(
            next((item.status is ScenarioStatus.COMPLETED for item in step_results if item.step_id == step.step_id), False)
            for step in plan.steps if step.required
        )
        cleanup_ok = cleanup.complete if cleanup_plan and cleanup_plan.required else True
        if cleanup_plan and cleanup_plan.expect_no_live_processes and cleanup.live_process_count_after != 0:
            cleanup_ok = False
        checkpoint_results: list[ScenarioCheckpointResult] = []
        step_map = {item.step_id: item for item in step_results}
        for checkpoint in plan.checkpoints:
            passed = True
            observed_step_status = None
            observed_process_status = None
            agent_close = None
            worker_cleanup = None
            if isinstance(checkpoint, StepStatusCheckpoint):
                step_observation = step_map.get(checkpoint.target_step_id)
                observed_step_status = None if step_observation is None else step_observation.status
                passed = observed_step_status is checkpoint.expected_step_status
            elif isinstance(checkpoint, ProcessStatusCheckpoint):
                step_observation = step_map.get(checkpoint.target_step_id)
                observed_process_status = None if step_observation is None else step_observation.actual_status
                passed = observed_process_status is checkpoint.expected_process_status
            elif isinstance(checkpoint, OutputCheckpoint):
                read = read_by_step.get(checkpoint.target_step_id)
                passed = read is not None and read.new_output_char_length >= checkpoint.minimum_new_output_length and not read.required_markers_missing and not read.forbidden_markers_found
            elif isinstance(checkpoint, CleanupCheckpoint):
                agent_close = agent_close_observed
                worker_cleanup = cleanup.complete
                passed = (
                    (not checkpoint.expect_agent_close or agent_close_observed)
                    and (not checkpoint.expect_worker_cleanup or cleanup.complete)
                    and (not checkpoint.expect_no_live_processes or cleanup.live_process_count_after == 0)
                )
            checkpoint_results.append(ScenarioCheckpointResult(
                checkpoint_id=checkpoint.checkpoint_id,
                kind=checkpoint.kind,
                required=checkpoint.required,
                target_step_id=getattr(checkpoint, "target_step_id", None),
                artifact_scope=getattr(checkpoint, "artifact_scope", None),
                passed=passed,
                observed_step_status=observed_step_status,
                observed_process_status=observed_process_status,
                agent_close_observed=agent_close,
                worker_cleanup_completed=worker_cleanup,
            ))
        timed_steps = [
            item for item in step_results
            if item.started_at is not None and item.completed_at is not None
        ]
        scenario_duration_ms: int | None = None
        if timed_steps:
            scenario_started_at = timed_steps[0].started_at
            scenario_completed_at = timed_steps[-1].completed_at
            if (
                scenario_started_at is not None
                and scenario_completed_at is not None
                and scenario_completed_at >= scenario_started_at
            ):
                scenario_duration_ms = max(
                    0,
                    round(
                        (scenario_completed_at - scenario_started_at).total_seconds()
                        * 1000
                    ),
                )
        scenario_timed_out = (
            scenario_duration_ms is not None
            and scenario_duration_ms > plan.timeout_seconds * 1000
        ) or scenario_timed_out
        scenario_status = ScenarioStatus.COMPLETED if (
            completed and required_steps_ok and cleanup_ok and process_identity_matched
            and bool(command_matched) and (input_matched is not False)
            and cursor_integrity and status_transitions_valid and not scenario_timed_out
            and scenario_duration_ms is not None
            and all(item.passed is True for item in checkpoint_results if item.required)
        ) else ScenarioStatus.FAILED
        result = ProcessScenarioExecutionResult(
            scenario_id=plan.scenario_id,
            status=scenario_status,
            checkpoints=checkpoint_results,
            steps=step_results,
            declared_command_sha256=_hash_text(declared_command) if declared_command is not None else None,
            actual_command_sha256=_hash_text(actual_command) if actual_command is not None else None,
            declared_command_length=len(declared_command) if declared_command is not None else None,
            actual_command_length=len(actual_command) if actual_command is not None else None,
            command_matched=command_matched,
            process_id_safe=safe_process_id,
            expected_process_id_safe=safe_process_id,
            process_identity_matched=process_identity_matched,
            initial_status=initial_status,
            final_status=final_status,
            incremental_reads=incremental,
            input_events=input_events,
            input_matched=input_matched,
            status_transitions_valid=status_transitions_valid,
            scenario_timeout_seconds=plan.timeout_seconds,
            scenario_timed_out=scenario_timed_out,
            agent_close_observed=agent_close_observed,
            worker_cleanup_result=cleanup,
            duration_ms=scenario_duration_ms,
            errors=[] if scenario_status is ScenarioStatus.COMPLETED else [_scenario_error("process-gate-failed", "declared Process lifecycle was incomplete")],
        )
        results.append(result)
        if request.artifact_paths.process_output_logs:
            log_index = len(log_paths)
            if log_index < len(request.artifact_paths.process_output_logs):
                log_path = request.artifact_paths.process_output_logs[log_index]
                safe_log, _ = _bounded(redact_text(output_log, sensitive_values))
                atomic_write_text(log_path, safe_log)
                log_paths.append(log_path)
        errors.extend(result.errors)
        errors.extend(item.error for item in step_results if item.error is not None)
    return results, errors, log_paths


__all__ = ("build_scenario_results",)
