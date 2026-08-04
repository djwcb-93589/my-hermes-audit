"""Worker-local projections for P6.1 Toolchain and Process scenarios.

Only response messages and the public ObservationBundle are consumed.  Audit
never starts a Process, calls a Tool handler, or imports a ProcessManager.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from myhermes_audit.artifacts import atomic_write_text
from myhermes_audit.contracts import (
    CleanupCheckpoint,
    ArtifactOutputCheckpoint,
    E2EScenarioKind,
    IncrementalReadObservation,
    ProcessAction,
    ProcessCleanupResult,
    ProcessInputObservation,
    ProcessScenarioExecutionResult,
    ProcessOutputCheckpoint,
    ProcessTimingStatus,
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
_MAX_TOOLCHAIN_ARTIFACT_BYTES = 256 * 1024
_TRUNCATION_MARKER = "\n...[truncated by my-hermes-audit]...\n"


@dataclass(frozen=True)
class _ArtifactProjection:
    exists: bool
    content: str | None
    content_sha256: str | None
    content_char_length: int | None
    content_utf8_bytes: int | None
    truncated: bool
    read_error: bool = False


@dataclass(frozen=True)
class _FixtureReadFacts:
    observed: bool
    content_sha256: str | None = None
    content_char_length: int | None = None
    content_utf8_bytes: int | None = None


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


def _artifact_projection(
    workspace: Path,
    hermes_home: Path,
    relative_path: str,
) -> _ArtifactProjection:
    try:
        target = _relative_target(workspace, hermes_home, relative_path)
        if not target.is_file() or target.is_symlink():
            return _ArtifactProjection(False, None, None, None, None, False)
        with target.open("rb") as stream:
            payload = stream.read(_MAX_TOOLCHAIN_ARTIFACT_BYTES + 1)
    except (OSError, ValueError):
        return _ArtifactProjection(False, None, None, None, None, False, True)
    truncated = len(payload) > _MAX_TOOLCHAIN_ARTIFACT_BYTES
    if truncated:
        payload = payload[:_MAX_TOOLCHAIN_ARTIFACT_BYTES]
    content_sha256 = hashlib.sha256(payload).hexdigest()
    content_utf8_bytes = len(payload)
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        if truncated:
            content = payload.decode("utf-8", errors="ignore")
            return _ArtifactProjection(
                True,
                content,
                content_sha256,
                len(content),
                content_utf8_bytes,
                True,
            )
        return _ArtifactProjection(
            True,
            None,
            content_sha256,
            None,
            content_utf8_bytes,
            truncated,
            True,
        )
    return _ArtifactProjection(
        True,
        content,
        content_sha256,
        len(content),
        content_utf8_bytes,
        truncated,
    )


def _artifact_observation(
    workspace: Path,
    hermes_home: Path,
    relative_path: str,
) -> ScenarioArtifactObservation:
    projection = _artifact_projection(workspace, hermes_home, relative_path)
    return ScenarioArtifactObservation(
        relative_path=relative_path,
        exists=projection.exists,
        sha256=projection.content_sha256,
        size_bytes=projection.content_utf8_bytes or 0,
        content_char_length=projection.content_char_length,
        content_utf8_bytes=projection.content_utf8_bytes,
        truncated=projection.truncated,
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


def _event_timing(
    event: Mapping[str, object] | None,
) -> tuple[int | None, ProcessTimingStatus]:
    if event is None or "duration_ms" not in event or event.get("duration_ms") is None:
        return None, ProcessTimingStatus.UNAVAILABLE
    value = event.get("duration_ms")
    if type(value) is not int or value < 0:
        return None, ProcessTimingStatus.INVALID
    # The public projection currently exposes a duration but no authoritative
    # start/end pair.  Do not synthesize timestamps from an observation time.
    return value, ProcessTimingStatus.AVAILABLE_DURATION_ONLY


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


def _trace_tool_call_observations(
    plan,
    observations: ObservationBundle,
) -> list[ScenarioToolCallObservation]:
    names = list(dict.fromkeys(item.tool_name for item in plan.trace_requirements))
    return [
        ScenarioToolCallObservation(
            tool_name=tool_name,
            call_count=sum(item.tool_name == tool_name for item in observations.tool_calls),
            successful_count=sum(
                item.tool_name == tool_name and item.success
                for item in observations.tool_calls
            ),
        )
        for tool_name in names
    ]


def _normalized_public_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
        return None
    try:
        path = PurePosixPath(normalized)
    except (TypeError, ValueError):
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _fixture_read_facts(
    events: Sequence[Mapping[str, object]],
    *,
    expected_path: str,
    before_event_index: int,
    expected_facts: tuple[str, int, int] | None,
) -> _FixtureReadFacts:
    """Prove a fixture read from a successful public ``file`` Tool Call.

    The raw content is used only in-memory to cross-check the materialized
    fixture.  Only hashes and lengths leave this projection.
    """

    forbidden_reference = expected_path.replace("/", "\\").lower()
    forbidden_posix = expected_path.lower()
    for event in events:
        if event.get("name") != "terminal":
            continue
        command = _event_arguments(event).get("command")
        if not isinstance(command, str):
            continue
        lowered = command.lower().replace("\\", "/")
        if (
            forbidden_posix in lowered
            or forbidden_reference in command.lower()
        ):
            return _FixtureReadFacts(observed=False)

    start_event_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("name") == "terminal"
            and _event_arguments(event).get("background") is True
        ),
        before_event_index,
    )
    for index, event in enumerate(events):
        if (
            index >= min(before_event_index, start_event_index)
            or event.get("name") != "file"
        ):
            continue
        arguments = _event_arguments(event)
        if arguments.get("action") != "read":
            continue
        if _normalized_public_path(arguments.get("path")) != expected_path:
            continue
        result = _event_result(event)
        content = result.get("content")
        if result.get("ok") is not True or not isinstance(content, str):
            continue
        if result.get("truncated") is True:
            continue
        actual = (_hash_text(content), len(content), len(content.encode("utf-8")))
        if expected_facts is not None and actual != expected_facts:
            continue
        return _FixtureReadFacts(
            observed=True,
            content_sha256=actual[0],
            content_char_length=actual[1],
            content_utf8_bytes=actual[2],
        )
    return _FixtureReadFacts(observed=False)


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


def _process_contract_failure(plan, *, case_id: str) -> ProcessScenarioExecutionResult:
    error = _scenario_error(
        "process_scenario_count_error",
        f"Case {case_id} contains more than one process_background Scenario",
    )
    return ProcessScenarioExecutionResult(
        scenario_id=plan.scenario_id,
        status=ScenarioStatus.FAILED,
        scenario_timeout_seconds=plan.timeout_seconds,
        process_identity_matched=False,
        command_matched=False,
        input_matched=False,
        status_transitions_valid=False,
        errors=[error],
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
    process_plans = [
        item for item in request.scenarios
        if item.kind is E2EScenarioKind.PROCESS_BACKGROUND
    ]
    process_contract_error = len(process_plans) > 1
    results: list[ScenarioExecutionResult] = []
    errors: list[ScenarioError] = []
    log_paths: list[Path] = []
    completed = len(turns) == len(request.turns) and all(
        item.runtime_status == "completed" for item in turns
    )
    process_plan_ordinal = 0
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
            declared_artifacts = set(plan.output_artifacts)
            checkpoint_results: list[ScenarioCheckpointResult] = []
            checkpoint_errors: list[ScenarioError] = []
            for checkpoint in plan.checkpoints:
                passed = completed
                checkpoint_error = None
                artifact_projection = None
                required_found: list[str] = []
                required_missing: list[str] = []
                forbidden_found: list[str] = []
                if isinstance(checkpoint, ArtifactOutputCheckpoint):
                    target_id = checkpoint.target_artifact_id
                    if target_id not in declared_artifacts:
                        checkpoint_error = _scenario_error(
                            "toolchain_artifact_target_error",
                            "Toolchain checkpoint target Artifact was not declared",
                        )
                    else:
                        artifact_projection = _artifact_projection(
                            request.workspace,
                            request.hermes_home,
                            target_id,
                        )
                        if artifact_projection.read_error:
                            checkpoint_error = _scenario_error(
                                "toolchain_artifact_read_error",
                                "declared Toolchain Artifact could not be safely read",
                            )
                        elif not artifact_projection.exists:
                            checkpoint_error = _scenario_error(
                                "artifact_missing",
                                "declared Toolchain Artifact is missing",
                            )
                        elif artifact_projection.content is not None:
                            content = artifact_projection.content
                            required_found = [
                                _safe_id(marker)
                                for marker in checkpoint.required_markers
                                if marker in content
                            ]
                            required_missing = [
                                _safe_id(marker)
                                for marker in checkpoint.required_markers
                                if marker not in content
                            ]
                            forbidden_found = [
                                _safe_id(marker)
                                for marker in checkpoint.forbidden_markers
                                if marker in content
                            ]
                            if required_missing:
                                checkpoint_error = _scenario_error(
                                    "toolchain_required_marker_missing",
                                    "a required Toolchain Artifact marker was not found",
                                )
                            elif forbidden_found:
                                checkpoint_error = _scenario_error(
                                    "toolchain_forbidden_marker_present",
                                    "a forbidden Toolchain Artifact marker was found",
                                )
                            elif (
                                artifact_projection.content_char_length is None
                                or artifact_projection.content_char_length
                                < checkpoint.minimum_content_char_length
                            ):
                                checkpoint_error = _scenario_error(
                                    "toolchain_minimum_length_error",
                                    "Toolchain Artifact content length was below the declared minimum",
                                )
                            elif artifact_projection.truncated:
                                checkpoint_error = _scenario_error(
                                    "toolchain_artifact_read_error",
                                    "Toolchain Artifact exceeded the bounded read limit",
                                )
                    passed = (
                        passed
                        and checkpoint_error is None
                        and artifact_projection is not None
                        and artifact_projection.exists
                        and artifact_projection.content is not None
                        and not artifact_projection.truncated
                    )
                if checkpoint_error is not None:
                    checkpoint_errors.append(checkpoint_error)
                checkpoint_results.append(ScenarioCheckpointResult(
                    checkpoint_id=checkpoint.checkpoint_id,
                    kind=checkpoint.kind,
                    required=checkpoint.required,
                    target_step_id=getattr(checkpoint, "target_step_id", None),
                    target_artifact_id=getattr(checkpoint, "target_artifact_id", None),
                    passed=passed,
                    observed_step_status=(ScenarioStatus.COMPLETED if passed else ScenarioStatus.FAILED),
                    artifact_exists=(None if artifact_projection is None else artifact_projection.exists),
                    content_sha256=(None if artifact_projection is None else artifact_projection.content_sha256),
                    content_char_length=(None if artifact_projection is None else artifact_projection.content_char_length),
                    content_utf8_bytes=(None if artifact_projection is None else artifact_projection.content_utf8_bytes),
                    required_markers_found=required_found,
                    missing_required_markers=required_missing,
                    forbidden_markers_found=forbidden_found,
                    required_marker_count=len(required_found),
                    missing_required_marker_count=len(required_missing),
                    forbidden_marker_count=len(forbidden_found),
                    truncated=(None if artifact_projection is None else artifact_projection.truncated),
                    error=checkpoint_error,
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
                and all(item.passed is True for item in checkpoint_results if item.required)
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
                errors=(
                    checkpoint_errors
                    if checkpoint_errors
                    else ([] if status is ScenarioStatus.COMPLETED else [_scenario_error("toolchain-gate-failed", "declared Toolchain observation was incomplete")])
                ),
            ))
            errors.extend(checkpoint_errors)
            continue

        process_plan_ordinal += 1
        if process_contract_error:
            result = _process_contract_failure(plan, case_id=request.case_id)
            results.append(result)
            errors.extend(result.errors)
            if process_plan_ordinal <= len(request.artifact_paths.process_output_logs):
                log_path = request.artifact_paths.process_output_logs[process_plan_ordinal - 1]
                atomic_write_text(log_path, "")
                log_paths.append(log_path)
            continue
        process_events = [item for item in events if item.get("name") in {"terminal", "process"}]
        trace_tool_calls = _trace_tool_call_observations(plan, observations)
        trace_passed = all(
            any(
                item.tool_name == requirement.tool_name
                and item.call_count >= requirement.minimum_calls
                and item.successful_count >= requirement.minimum_successful_calls
                for item in trace_tool_calls
            )
            for requirement in plan.trace_requirements
            if requirement.required
        )
        fixture_read_required = any(
            requirement.required and requirement.tool_name == "file"
            for requirement in plan.trace_requirements
        )
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
        file_fixture_read_observed = False
        command_matched: bool | None = None
        process_identity_matched = True
        status_transitions_valid = True
        status_history: list[ScenarioProcessStatus] = []
        initial_status: ScenarioProcessStatus | None = None
        final_status = ScenarioProcessStatus.UNKNOWN
        agent_close_observed = False
        elapsed_ms = 0
        scenario_timed_out: bool | None = None
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
                submit_event_index = next(
                    (
                        index
                        for index, candidate in enumerate(events)
                        if candidate is event
                    ),
                    len(events),
                )
                fixture_read = _fixture_read_facts(
                    events,
                    expected_path=step.input_source,
                    before_event_index=submit_event_index,
                    expected_facts=expected,
                ) if fixture_read_required else _FixtureReadFacts(observed=False)
                file_fixture_read_observed = file_fixture_read_observed or fixture_read.observed
                input_matched = (
                    expected is not None and actual is not None
                    and expected_hash == actual_hash
                    and expected_chars == actual_chars
                    and expected_bytes == actual_bytes
                    and action_matched and identity_match
                    and (not fixture_read_required or fixture_read.observed)
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
                    file_fixture_read_observed=(fixture_read.observed if fixture_read_required else None),
                    file_fixture_read_sha256=(fixture_read.content_sha256 if fixture_read_required else None),
                    file_fixture_read_char_length=(fixture_read.content_char_length if fixture_read_required else None),
                    file_fixture_read_utf8_bytes=(fixture_read.content_utf8_bytes if fixture_read_required else None),
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
                source_read = (
                    read_by_step.get(step.cursor_source_step_id)
                    if step.cursor_source_step_id is not None
                    else None
                )
                source_present = (
                    step.cursor_source_step_id is None or source_read is not None
                )
                expected_cursor = (
                    source_read.cursor_after
                    if source_read is not None
                    else step.cursor_before
                )
                cursor_reference_matched = (
                    source_present
                    and expected_cursor is not None
                    and isinstance(requested, int)
                    and requested >= 0
                    and requested == expected_cursor
                )
                cursor_chain_matched = (
                    cursor_reference_matched
                    and expected_cursor == cursor
                )
                requested_ok = cursor_reference_matched and cursor_chain_matched
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
                    step_id=step.step_id,
                    read_index=len(incremental),
                    cursor_before=cursor,
                    cursor_after=next_value,
                    cursor_source_step_id=step.cursor_source_step_id,
                    cursor_reference_matched=cursor_reference_matched,
                    cursor_chain_matched=cursor_chain_matched,
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
            duration_ms, timing_status = _event_timing(event)
            elapsed_before_ms = elapsed_ms
            elapsed_ms += duration_ms or 0
            timed_out = (
                None
                if duration_ms is None
                else duration_ms > step.timeout_seconds * 1000
            )
            if timed_out is True:
                scenario_timed_out = True
            if step.action is ProcessAction.WAIT:
                wait_timeout = arguments.get("timeout")
                scenario_remaining = plan.timeout_seconds - elapsed_before_ms / 1000
                wait_ok = (
                    isinstance(wait_timeout, (int, float))
                    and not isinstance(wait_timeout, bool)
                    and wait_timeout >= 0
                    and wait_timeout <= step.timeout_seconds
                    and step.timeout_seconds <= step.maximum_wait_seconds
                    and step.maximum_wait_seconds <= scenario_remaining
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
            timing_gate = (
                timing_status in {
                    ProcessTimingStatus.AVAILABLE,
                    ProcessTimingStatus.AVAILABLE_DURATION_ONLY,
                }
                and timed_out is False
            ) or (
                not step.required
                and timing_status in {
                    ProcessTimingStatus.UNAVAILABLE,
                    ProcessTimingStatus.INVALID,
                }
            )
            passed = bool(
                action_matched
                and (identity_match or step.action is ProcessAction.START)
                and result_payload.get("ok", True) is not False
                and semantic_ok
                and timing_gate
            )
            if step.action is ProcessAction.START:
                passed = passed and actual_status is step.expected_initial_status and bool(command_matched)
            step_error_type = "process-observation-missing" if event is None else "process-step-gate-failed"
            if not passed and step.required:
                if timing_status is ProcessTimingStatus.UNAVAILABLE:
                    step_error_type = "process_step_timing_unavailable"
                elif timing_status is ProcessTimingStatus.INVALID:
                    step_error_type = "process_step_timing_invalid"
                elif timed_out is True:
                    step_error_type = "process_step_timeout"
            if step.action is ProcessAction.READ_INCREMENTAL:
                if not read.cursor_reference_matched:
                    step_error_type = "process_cursor_reference_error"
                elif not read.cursor_chain_matched:
                    step_error_type = "process_cursor_chain_error"
            step_results.append(ScenarioStepResult(
                step_id=step.step_id,
                action=step.action,
                status=ScenarioStatus.COMPLETED if passed else ScenarioStatus.ERROR,
                actual_action=str(actual_action) if isinstance(actual_action, str) else None,
                actual_status=actual_status,
                started_at=None,
                completed_at=None,
                duration_ms=duration_ms,
                timeout_seconds=step.timeout_seconds,
                timing_status=timing_status,
                timed_out=timed_out,
                observation_refs=[_safe_id(event.get("tool_call_id"))] if event else [],
                expected_process_id_safe=safe_process_id,
                actual_process_id_safe=event_safe_process_id,
                process_identity_matched=(True if step.action is ProcessAction.START else identity_match),
                action_matched=action_matched,
                error=None if passed else _scenario_error(
                    step_error_type,
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
        checkpoint_errors: list[ScenarioError] = []
        step_map = {item.step_id: item for item in step_results}
        for checkpoint in plan.checkpoints:
            passed = True
            observed_step_status = None
            observed_process_status = None
            agent_close = None
            worker_cleanup = None
            checkpoint_error = None
            required_markers_found: list[str] = []
            missing_required_markers: list[str] = []
            forbidden_markers_found: list[str] = []
            truncated = None
            if isinstance(checkpoint, StepStatusCheckpoint):
                step_observation = step_map.get(checkpoint.target_step_id)
                observed_step_status = None if step_observation is None else step_observation.status
                passed = observed_step_status is checkpoint.expected_step_status
            elif isinstance(checkpoint, ProcessStatusCheckpoint):
                step_observation = step_map.get(checkpoint.target_step_id)
                observed_process_status = None if step_observation is None else step_observation.actual_status
                passed = observed_process_status is checkpoint.expected_process_status
            elif isinstance(checkpoint, ProcessOutputCheckpoint):
                read = read_by_step.get(checkpoint.target_step_id)
                passed = read is not None and read.new_output_char_length >= checkpoint.minimum_new_output_length and not read.required_markers_missing and not read.forbidden_markers_found
                if read is None:
                    checkpoint_error = _scenario_error(
                        "process_cursor_reference_error",
                        "Process output checkpoint target read was not observed",
                    )
                elif read.cursor_source_step_id is not None and not read.cursor_reference_matched:
                    checkpoint_error = _scenario_error(
                        "process_cursor_reference_error",
                        "Process output checkpoint cursor reference was not observed",
                    )
                elif read.cursor_source_step_id is not None and not read.cursor_chain_matched:
                    checkpoint_error = _scenario_error(
                        "process_cursor_chain_error",
                        "Process output checkpoint cursor chain did not match",
                    )
                required_markers_found = list(read.required_markers_found) if read is not None else []
                missing_required_markers = list(read.required_markers_missing) if read is not None else []
                forbidden_markers_found = list(read.forbidden_markers_found) if read is not None else []
                truncated = read.truncated if read is not None else None
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
                target_artifact_id=getattr(checkpoint, "target_artifact_id", None),
                passed=passed,
                observed_step_status=observed_step_status,
                observed_process_status=observed_process_status,
                agent_close_observed=agent_close,
                worker_cleanup_completed=worker_cleanup,
                required_markers_found=required_markers_found,
                missing_required_markers=missing_required_markers,
                forbidden_markers_found=forbidden_markers_found,
                required_marker_count=len(required_markers_found),
                missing_required_marker_count=len(missing_required_markers),
                forbidden_marker_count=len(forbidden_markers_found),
                truncated=truncated,
                error=checkpoint_error,
            ))
            if checkpoint_error is not None:
                checkpoint_errors.append(checkpoint_error)
        required_step_timing_statuses = [
            step_result.timing_status
            for step, step_result in zip(plan.steps, step_results, strict=False)
            if step.required
        ]
        step_timing_statuses = required_step_timing_statuses
        if any(item is ProcessTimingStatus.INVALID for item in step_timing_statuses):
            scenario_timing_status = ProcessTimingStatus.INVALID
            scenario_duration_ms = None
            scenario_timed_out = None
        elif step_timing_statuses and all(
            item in {
                ProcessTimingStatus.AVAILABLE,
                ProcessTimingStatus.AVAILABLE_DURATION_ONLY,
            }
            for item in step_timing_statuses
        ):
            scenario_timing_status = ProcessTimingStatus.AVAILABLE_DURATION_ONLY
            scenario_duration_ms = elapsed_ms
            scenario_timed_out = (
                scenario_timed_out is True
                or scenario_duration_ms > plan.timeout_seconds * 1000
            )
        else:
            scenario_timing_status = ProcessTimingStatus.UNAVAILABLE
            scenario_duration_ms = None
            scenario_timed_out = None
        scenario_errors = list(checkpoint_errors)
        if fixture_read_required and not file_fixture_read_observed:
            scenario_errors.append(
                _scenario_error(
                    "process_fixture_read_missing",
                    "declared input fixture was not proven by a successful file read before submit",
                )
            )
        for step_result in step_results:
            if step_result.error is not None and step_result.error.error_type in {
                "process_step_timing_unavailable",
                "process_step_timing_invalid",
                "process_step_timeout",
            }:
                scenario_errors.append(step_result.error)
        scenario_status = ScenarioStatus.COMPLETED if (
            completed and required_steps_ok and cleanup_ok and process_identity_matched
            and bool(command_matched) and (input_matched is not False)
            and cursor_integrity and status_transitions_valid and trace_passed
            and (not fixture_read_required or file_fixture_read_observed)
            and scenario_timed_out is not True
            and scenario_duration_ms is not None
            and all(item.passed is True for item in checkpoint_results if item.required)
        ) else ScenarioStatus.FAILED
        if not scenario_errors and scenario_status is not ScenarioStatus.COMPLETED:
            scenario_errors.append(
                _scenario_error(
                    "process-gate-failed",
                    "declared Process lifecycle was incomplete",
                )
            )
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
            tool_calls=trace_tool_calls,
            input_matched=input_matched,
            file_fixture_read_observed=file_fixture_read_observed,
            status_transitions_valid=status_transitions_valid,
            scenario_timeout_seconds=plan.timeout_seconds,
            timing_status=scenario_timing_status,
            scenario_timed_out=scenario_timed_out,
            agent_close_required=any(
                item.action is ProcessAction.CLOSE and item.required
                for item in plan.steps
            ),
            agent_close_observed=agent_close_observed,
            worker_cleanup_result=cleanup,
            duration_ms=scenario_duration_ms,
            errors=scenario_errors,
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
