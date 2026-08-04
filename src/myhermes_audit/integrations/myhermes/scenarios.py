"""Worker-local projections for the first P6.1 scenario families.

The module consumes only the response messages already returned by the public
conversation entry point.  It never starts a process, calls a Tool handler, or
uses a second ProcessManager.  Commands and input bodies are intentionally not
returned by any public model in this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from myhermes_audit.artifacts import atomic_write_text
from myhermes_audit.contracts import (
    E2EScenarioKind,
    IncrementalReadObservation,
    ProcessCleanupResult,
    ProcessInputObservation,
    ProcessScenarioExecutionResult,
    ProcessAction,
    ScenarioArtifactObservation,
    ScenarioCheckpointResult,
    ScenarioError,
    ScenarioExecutionResult,
    ScenarioProcessStatus,
    ScenarioStatus,
    ScenarioStepResult,
    ScenarioToolCallObservation,
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


def _tool_events(responses: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Project assistant tool calls and the following public tool results."""
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
                    }
            if message.get("role") not in {"tool", "tool_result"}:
                continue
            result = _json_content(message.get("content"))
            if result is None:
                continue
            call_id = message.get("tool_call_id", message.get("id"))
            event = pending.pop(str(call_id), None) if call_id is not None else None
            if event is None and pending:
                event = pending.pop(next(iter(pending)))
            if event is None:
                event = {"name": message.get("name", ""), "arguments": {}}
            event = dict(event)
            event["result"] = result
            events.append(event)
    return events


def _relative_target(
    workspace: Path,
    hermes_home: Path,
    relative_path: str,
) -> Path:
    parts = Path(relative_path).parts
    if not parts or parts[0] not in {"workspace", "hermes_home"}:
        raise ValueError("scenario Artifact path must be workspace/ or hermes_home/")
    root = (
        workspace if parts[0] == "workspace" else hermes_home
    ).resolve(strict=True)
    current = root
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("scenario Artifact path traverses a symbolic link")
    candidate = current.resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError("scenario Artifact path escaped its root")
    return candidate


def _artifact_observation(
    workspace: Path,
    hermes_home: Path,
    relative_path: str,
) -> ScenarioArtifactObservation:
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
    return ScenarioError(error_type=error_type, message=message, step_id=step_id)


def _event_matches_step(event: Mapping[str, object] | None, step) -> bool:
    """Require the observed public tool operation to match the typed step."""

    if event is None:
        return False
    name = event.get("name")
    arguments = event.get("arguments")
    arguments = arguments if isinstance(arguments, Mapping) else {}
    result = event.get("result")
    result = result if isinstance(result, Mapping) else {}
    observed_action = result.get("action") or arguments.get("action")
    if step.action is ProcessAction.START:
        return name == "terminal" and arguments.get("background") is True
    if name != "process":
        return False
    if step.action is ProcessAction.READ_INCREMENTAL:
        return observed_action in {"log", "poll"}
    if step.action is ProcessAction.SEND_INPUT:
        return observed_action == ("submit" if step.submit else "write")
    if step.action is ProcessAction.WAIT:
        return observed_action == "wait"
    if step.action is ProcessAction.INTERRUPT:
        return observed_action == "interrupt"
    if step.action is ProcessAction.KILL:
        return observed_action == "kill"
    if step.action is ProcessAction.ASSERT_STATUS:
        return observed_action in {"poll", "list"}
    return False


def _scenario_tool_call_seen(
    events: Sequence[Mapping[str, object]],
    expected,
) -> bool:
    """Match a declared Toolchain call against public tool observations."""

    for event in events:
        if event.get("name") != expected.tool_name:
            continue
        if not expected.arguments:
            return True
        actual = event.get("arguments")
        if not isinstance(actual, Mapping):
            continue
        if all(actual.get(key) == value for key, value in expected.arguments.items()):
            return True
    return False


def build_scenario_results(
    request: MyHermesWorkerRequest,
    *,
    responses: Sequence[Mapping[str, object]],
    observations: ObservationBundle,
    turns,
    cleanup_reports: Sequence[Mapping[str, object]],
    sensitive_values: Sequence[str] = (),
) -> tuple[list[ScenarioExecutionResult], list[ScenarioError], list[Path]]:
    events = _tool_events(responses)
    results: list[ScenarioExecutionResult] = []
    errors: list[ScenarioError] = []
    log_paths: list[Path] = []
    completed = len(turns) == len(request.turns) and all(
        item.runtime_status == "completed" for item in turns
    )
    for plan in request.scenarios:
        if plan.kind is E2EScenarioKind.TOOLCHAIN:
            calls = []
            requirement_names = list(dict.fromkeys(
                [item.tool_name for item in plan.trace_requirements]
                + [item.tool_name for item in plan.required_tool_calls]
                + [item.tool_name for item in plan.forbidden_tool_calls]
            ))
            for tool_name in requirement_names:
                matching = [item for item in observations.tool_calls if item.tool_name == tool_name]
                calls.append(
                    ScenarioToolCallObservation(
                        tool_name=tool_name,
                        call_count=len(matching),
                        successful_count=sum(item.success for item in matching),
                    )
                )
            outputs = [
                _artifact_observation(request.workspace, request.hermes_home, item)
                for item in plan.output_artifacts
            ]
            inputs = [
                _artifact_observation(request.workspace, request.hermes_home, item)
                for item in plan.input_artifacts
            ]
            checkpoint_results = []
            for item in plan.checkpoints:
                checkpoint_passed = completed
                lowered = item.checkpoint_id.lower()
                if "input" in lowered or "read" in lowered:
                    checkpoint_passed = checkpoint_passed and all(obs.exists for obs in inputs)
                if "output" in lowered or "write" in lowered:
                    checkpoint_passed = checkpoint_passed and all(obs.exists for obs in outputs)
                checkpoint_results.append(
                    ScenarioCheckpointResult(
                        checkpoint_id=item.checkpoint_id,
                        required=item.required,
                        passed=checkpoint_passed,
                        observed_status=(ScenarioStatus.COMPLETED if checkpoint_passed else ScenarioStatus.FAILED),
                    )
                )
            trace_ok = all(
                next((item.call_count >= req.minimum_calls and item.successful_count >= req.minimum_successful_calls
                      for item in calls if item.tool_name == req.tool_name), False)
                for req in plan.trace_requirements if req.required
            )
            if observations.truncated:
                # A bounded observation projection cannot prove required or
                # forbidden calls, so do not turn partial facts into success.
                trace_ok = False
            required_ok = all(
                _scenario_tool_call_seen(events, req)
                if req.arguments
                else any(
                    item.tool_name == req.tool_name and item.call_count > 0
                    for item in calls
                )
                for req in plan.required_tool_calls
            )
            forbidden_ok = all(
                not _scenario_tool_call_seen(events, req)
                if req.arguments
                else not any(
                    item.tool_name == req.tool_name and item.call_count > 0
                    for item in calls
                )
                for req in plan.forbidden_tool_calls
            )
            if observations.truncated:
                forbidden_ok = False if plan.forbidden_tool_calls else forbidden_ok
            status = (
                ScenarioStatus.COMPLETED
                if (
                    completed
                    and trace_ok
                    and required_ok
                    and forbidden_ok
                    and not observations.truncated
                    and all(item.exists for item in inputs + outputs)
                )
                else ScenarioStatus.FAILED
            )
            results.append(ToolchainScenarioExecutionResult(
                scenario_id=plan.scenario_id,
                status=status,
                checkpoints=checkpoint_results,
                input_artifacts=inputs,
                output_artifacts=outputs,
                tool_calls=calls,
                final_response_present=bool(turns and turns[-1].final_output),
                duration_ms=sum(item.duration_ms for item in turns),
                errors=[] if status is ScenarioStatus.COMPLETED else [_scenario_error("toolchain_gate_failed", "declared Toolchain observation was incomplete")],
            ))
            continue

        process_events = [item for item in events if item.get("name") in {"terminal", "process"}]
        process_ids = [
            str(item.get("result", {}).get("process_id"))
            for item in process_events
            if isinstance(item.get("result"), Mapping) and item.get("result", {}).get("process_id")
        ]
        safe_process_id = _safe_id(process_ids[0]) if process_ids else None
        step_results: list[ScenarioStepResult] = []
        incremental: list[IncrementalReadObservation] = []
        input_events: list[ProcessInputObservation] = []
        output_log = ""
        offset = 0
        event_index = 0
        interrupt_requested = False
        kill_requested = False
        final_status = ScenarioProcessStatus.UNKNOWN
        initial_status: ScenarioProcessStatus | None = None
        cleanup_complete = (
            all(bool(item.get("complete")) for item in cleanup_reports)
            if cleanup_reports
            else False
        )
        for step in plan.steps:
            # Session cleanup is performed by the existing Worker lifecycle,
            # not by a foreground tool call.  It therefore must not consume a
            # Process observation slot or be treated as a missing tool result.
            if step.action is ProcessAction.CLEANUP_SESSION:
                event = None
            else:
                event = (
                    process_events[event_index]
                    if event_index < len(process_events)
                    else None
                )
                event_index += 1
            result_payload = event.get("result", {}) if event else {}
            result_payload = result_payload if isinstance(result_payload, Mapping) else {}
            action = step.action
            if action is ProcessAction.START:
                initial_status = _status(result_payload.get("status"))
                final_status = initial_status
            elif action in {ProcessAction.READ_INCREMENTAL, ProcessAction.WAIT, ProcessAction.ASSERT_STATUS}:
                process_payload = result_payload.get("process")
                nested_status = (
                    process_payload.get("status")
                    if isinstance(process_payload, Mapping)
                    else None
                )
                final_status = _status(result_payload.get("status") or nested_status)
                if (
                    action is ProcessAction.WAIT
                    and result_payload.get("timed_out") is True
                    and final_status
                    in {
                        ScenarioProcessStatus.STARTING,
                        ScenarioProcessStatus.RUNNING,
                    }
                ):
                    final_status = ScenarioProcessStatus.TIMED_OUT
            elif action is ProcessAction.INTERRUPT:
                interrupt_requested = True
                final_status = _status(result_payload.get("status"))
            elif action is ProcessAction.KILL:
                kill_requested = True
                final_status = _status(result_payload.get("status"))
            elif action is ProcessAction.SEND_INPUT:
                accepted = bool(result_payload.get("ok") is True)
                bytes_written = result_payload.get("bytes_written")
                if type(bytes_written) is not int or bytes_written < 0:
                    bytes_written = None
                input_events.append(
                    ProcessInputObservation(
                        input_source=step.input_source,
                        submitted=step.submit,
                        accepted=accepted,
                        bytes_written=bytes_written,
                    )
                )
            cursor_gap = False
            if action is ProcessAction.READ_INCREMENTAL:
                output = result_payload.get("output", "")
                output = output if isinstance(output, str) else ""
                requested_offset = result_payload.get("requested_cursor", offset)
                requested_offset = (
                    requested_offset
                    if isinstance(requested_offset, int) and requested_offset >= 0
                    else offset
                )
                next_offset = result_payload.get(
                    "next_cursor",
                    requested_offset + len(output.encode("utf-8")),
                )
                next_offset = (
                    next_offset
                    if isinstance(next_offset, int) and next_offset >= requested_offset
                    else requested_offset
                )
                output_length = len(output.encode("utf-8"))
                # The public Process API is cursor based.  A repeated or stale
                # cursor must not make already observed bytes look new.  Keep
                # the prior monotonic offset and discard the duplicate page.
                duplicate_page = requested_offset < offset
                available_offset = result_payload.get("available_from_cursor")
                cursor_gap = requested_offset > offset or (
                    isinstance(available_offset, int)
                    and available_offset > requested_offset
                )
                cursor_length_mismatch = (
                    next_offset - requested_offset != output_length
                )
                if duplicate_page or cursor_gap or cursor_length_mismatch:
                    output = ""
                    next_offset = offset
                required_found = [marker for marker in step.required_markers if marker in output]
                forbidden_found = [marker for marker in step.forbidden_markers if marker in output]
                incremental.append(IncrementalReadObservation(
                    read_index=len(incremental), offset_before=offset, offset_after=next_offset,
                    new_output_length=next_offset - offset, content_sha256=(_hash_text(output) if output else None),
                    required_markers_found=[_safe_id(marker) for marker in required_found],
                    required_markers_missing=[_safe_id(marker) for marker in step.required_markers if marker not in output],
                    forbidden_markers_found=[_safe_id(marker) for marker in forbidden_found],
                    truncated=bool(result_payload.get("output_truncated")),
                ))
                offset = next_offset
                output_log += output
            if action is ProcessAction.CLEANUP_SESSION:
                passed = cleanup_complete
            else:
                passed = (
                    event is not None
                    and result_payload.get("ok", True) is not False
                    and _event_matches_step(event, step)
                )
            observed_status = final_status
            if action is ProcessAction.START:
                passed = passed and observed_status is step.expected_initial_status
            elif action is ProcessAction.READ_INCREMENTAL:
                read = incremental[-1]
                passed = passed and read.new_output_length >= step.minimum_new_output_length
                passed = passed and not read.required_markers_missing and not read.forbidden_markers_found
                passed = passed and not cursor_gap and not cursor_length_mismatch
            elif action is ProcessAction.WAIT:
                passed = passed and observed_status is step.expected_status
            elif action is ProcessAction.ASSERT_STATUS:
                passed = passed and observed_status is step.expected_status
            elif action is ProcessAction.INTERRUPT:
                passed = passed and observed_status is step.expected_terminal_status
            elif action is ProcessAction.KILL:
                passed = passed and observed_status is step.expected_terminal_status
            step_results.append(ScenarioStepResult(
                step_id=step.step_id,
                action=action,
                status=ScenarioStatus.COMPLETED if passed else ScenarioStatus.ERROR,
                observation_refs=[_safe_id(event.get("name"))] if event else [],
                error=(
                    None
                    if passed
                    else _scenario_error(
                        (
                            "process_cleanup_incomplete"
                            if action is ProcessAction.CLEANUP_SESSION
                            else "process_observation_missing"
                        ),
                        (
                            "Process session cleanup was not confirmed"
                            if action is ProcessAction.CLEANUP_SESSION
                            else "public process observation was not returned"
                        ),
                        step_id=step.step_id,
                    )
                ),
            ))
        unresolved_cleanup_ids = (
            []
            if cleanup_complete
            else [safe_process_id or "id-cleanup-unconfirmed"]
        )
        cleanup = ProcessCleanupResult(
            attempted_process_ids=[safe_process_id] if safe_process_id else [],
            completed_process_ids=[safe_process_id] if safe_process_id and cleanup_complete else [],
            unresolved_process_ids=unresolved_cleanup_ids,
        )
        required_step_ids = {
            item.step_id for item in plan.steps if item.required
        }
        observed_step_status = {
            item.step_id: item.status for item in step_results
        }
        required_steps_ok = all(
            observed_step_status.get(step_id) is ScenarioStatus.COMPLETED
            for step_id in required_step_ids
        )
        cleanup_required = any(
            item.action is ProcessAction.CLEANUP_SESSION and item.required
            for item in plan.steps
        )
        cleanup_ok = cleanup.complete if cleanup_required else True
        step_passed = {
            item.step_id: item.status is ScenarioStatus.COMPLETED
            for item in step_results
        }
        checkpoint_results = []
        for checkpoint in plan.checkpoints:
            lowered = checkpoint.checkpoint_id.lower()
            if any(token in lowered for token in ("start", "begin")):
                checkpoint_passed = any(
                    step.action is ProcessAction.START
                    and step_passed.get(step.step_id, False)
                    for step in plan.steps
                )
            elif any(token in lowered for token in ("read", "increment")):
                checkpoint_passed = any(
                    step.action is ProcessAction.READ_INCREMENTAL
                    and step_passed.get(step.step_id, False)
                    for step in plan.steps
                )
            elif "complete" in lowered:
                checkpoint_passed = final_status is ScenarioProcessStatus.COMPLETED
            elif "waiting" in lowered:
                checkpoint_passed = any(
                    step.action is ProcessAction.READ_INCREMENTAL
                    and step_passed.get(step.step_id, False)
                    for step in plan.steps
                )
            elif any(token in lowered for token in ("input", "submit")):
                checkpoint_passed = any(
                    step.action is ProcessAction.SEND_INPUT
                    and step_passed.get(step.step_id, False)
                    for step in plan.steps
                )
            elif any(token in lowered for token in ("clean", "cleanup")):
                checkpoint_passed = cleanup_ok
            elif "kill" in lowered:
                checkpoint_passed = final_status is ScenarioProcessStatus.KILLED
            elif "interrupt" in lowered:
                checkpoint_passed = final_status is ScenarioProcessStatus.INTERRUPTED
            elif "terminal" in lowered:
                checkpoint_passed = final_status in {
                    ScenarioProcessStatus.COMPLETED,
                    ScenarioProcessStatus.INTERRUPTED,
                    ScenarioProcessStatus.KILLED,
                }
            else:
                checkpoint_passed = required_steps_ok and cleanup_ok
            checkpoint_results.append(
                ScenarioCheckpointResult(
                    checkpoint_id=checkpoint.checkpoint_id,
                    required=checkpoint.required,
                    passed=checkpoint_passed,
                    observed_status=(
                        ScenarioStatus.COMPLETED
                        if checkpoint_passed
                        else ScenarioStatus.FAILED
                    ),
                )
            )
        scenario_status = (
            ScenarioStatus.COMPLETED
            if completed and required_steps_ok and cleanup_ok
            else ScenarioStatus.FAILED
        )
        result = ProcessScenarioExecutionResult(
            scenario_id=plan.scenario_id,
            status=scenario_status,
            checkpoints=checkpoint_results,
            steps=step_results,
            process_id_safe=safe_process_id,
            session_id_safe=(_safe_id(turns[0].session_id) if turns and turns[0].session_id else None),
            initial_status=initial_status,
            final_status=final_status,
            incremental_reads=incremental,
            input_events=input_events,
            interrupt_requested=interrupt_requested,
            kill_requested=kill_requested,
            cleanup_result=cleanup,
            duration_ms=sum(item.duration_ms for item in turns),
            errors=[] if scenario_status is ScenarioStatus.COMPLETED else [_scenario_error("process_gate_failed", "declared Process lifecycle was incomplete")],
        )
        results.append(result)
        if request.artifact_paths.process_output_logs:
            log_path = request.artifact_paths.process_output_logs[len(log_paths)]
            safe_log, _ = _bounded(redact_text(output_log, sensitive_values))
            atomic_write_text(log_path, safe_log)
            log_paths.append(log_path)
        errors.extend(result.errors)
        errors.extend(
            item.error
            for item in step_results
            if item.error is not None
        )
    return results, errors, log_paths


__all__ = ("build_scenario_results",)
