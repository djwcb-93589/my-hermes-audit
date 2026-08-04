"""Worker-local projections for P6.1 Toolchain and Process scenarios.

Only response messages and the public ObservationBundle are consumed.  Audit
never starts a Process, calls a Tool handler, or imports a ProcessManager.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from myhermes_audit.artifacts import atomic_write_text
from myhermes_audit.contracts import (
    CleanupCheckpoint,
    ArtifactOutputCheckpoint,
    E2EScenarioKind,
    IncrementalReadObservation,
    ProcessAction,
    ProcessCleanupResult,
    ProcessHardTimeoutSource,
    ProcessHookSpanStatus,
    ProcessHookTimingSource,
    ProcessEventDiagnostic,
    ProcessObservationSpanStatus,
    ProcessInputObservation,
    ProcessScenarioExecutionResult,
    ProcessOutputCheckpoint,
    ProcessTimingStatus,
    ProcessTimingSource,
    ProcessWaitTimingSource,
    ProcessAssertStatusStep,
    ProcessCloseStep,
    ProcessInterruptStep,
    ProcessKillStep,
    ProcessReadIncrementalStep,
    ProcessSendInputStep,
    ProcessStartStep,
    ProcessWaitStep,
    ScenarioArtifactObservation,
    ScenarioCheckpointResult,
    ScenarioError,
    ScenarioExecutionResult,
    ScenarioProcessStatus,
    ScenarioStatus,
    ScenarioStepResult,
    ScenarioToolCallObservation,
    WaitRemainingBudgetStatus,
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
_WAIT_BUDGET_EPSILON_SECONDS = 0.001


def _budget_at_most(value: object, budget: float) -> bool:
    """Compare bounded WAIT seconds with one documented float tolerance."""

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
        and float(value) <= budget + _WAIT_BUDGET_EPSILON_SECONDS
    )


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


@dataclass(frozen=True)
class _ProcessEventAlignment:
    """One-way binding of declared Process steps to public observations."""

    matched_events: list[Mapping[str, object] | None]
    matched_indices: list[int | None]
    unexpected_events: list[ProcessEventDiagnostic]
    missing_expected_events: list[ProcessEventDiagnostic]
    event_order_violations: list[ProcessEventDiagnostic]
    foreign_process_events: list[ProcessEventDiagnostic]
    unconsumed_events: list[ProcessEventDiagnostic]


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
    return value, ProcessTimingStatus.AVAILABLE_DURATION_ONLY


def _event_matches_step(event: Mapping[str, object] | None, step) -> bool:
    """Require the observed public tool operation to match the typed step."""

    if event is None:
        return False
    name = event.get("name")
    arguments = _event_arguments(event)
    result = _event_result(event)
    observed_action = result.get("action") or arguments.get("action")
    if isinstance(step, ProcessStartStep):
        if step.action is not ProcessAction.START:
            return False
        return name == "terminal" and arguments.get("background") is True
    if name != "process":
        return False
    if isinstance(step, ProcessSendInputStep):
        if step.action is not ProcessAction.SEND_INPUT:
            return False
        expected_action = "submit" if step.submit else "write"
    elif isinstance(step, ProcessReadIncrementalStep):
        if step.action is not ProcessAction.READ_INCREMENTAL:
            return False
        expected_action = "log"
    elif isinstance(step, ProcessWaitStep):
        if step.action is not ProcessAction.WAIT:
            return False
        expected_action = "wait"
    elif isinstance(step, ProcessInterruptStep):
        if step.action is not ProcessAction.INTERRUPT:
            return False
        expected_action = "interrupt"
    elif isinstance(step, ProcessKillStep):
        if step.action is not ProcessAction.KILL:
            return False
        expected_action = "kill"
    elif isinstance(step, ProcessCloseStep):
        if step.action is not ProcessAction.CLOSE:
            return False
        expected_action = "close"
    elif isinstance(step, ProcessAssertStatusStep):
        if step.action is not ProcessAction.ASSERT_STATUS:
            return False
        expected_action = "poll"
    else:
        return False
    return observed_action == expected_action


def _safe_diagnostic_label(value: object) -> str | None:
    """Return a bounded public label, hashing values outside the safe grammar."""

    if not isinstance(value, str) or not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isascii() and len(value) <= 128 and value[0].isalnum() and all(
        char.isalnum() or char in "._:-" for char in value
    ):
        return value
    return _safe_id(value)


def _event_public_action(event: Mapping[str, object] | None) -> object:
    result = _event_result(event)
    arguments = _event_arguments(event)
    return result.get("action") or arguments.get("action")


def _event_diagnostic(
    event: Mapping[str, object] | None,
    *,
    event_index: int,
    reason: str,
    step_id: str | None = None,
) -> ProcessEventDiagnostic:
    return ProcessEventDiagnostic(
        event_index=event_index,
        tool_name=_safe_diagnostic_label(event.get("name")) if event else None,
        public_action=_safe_diagnostic_label(_event_public_action(event)) if event else None,
        process_id_safe=(
            _safe_id(process_id)
            if (process_id := _event_process_id(event))
            else None
        ),
        tool_call_id_safe=(
            _safe_id(tool_call_id)
            if event and (tool_call_id := event.get("tool_call_id"))
            else None
        ),
        observation_status=(
            _safe_diagnostic_label(_event_status(event).value) if event else None
        ),
        step_id=step_id,
        reason=reason,
    )


def _event_observation_time(
    event: Mapping[str, object] | None,
) -> datetime | None:
    """Return only the public persistence timestamp for an observation.

    ``created_at`` is persistence metadata.  It is intentionally not combined
    with ``duration_ms`` to fabricate a per-tool start or completion boundary.
    """

    if event is None:
        return None
    created_at = event.get("created_at")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        return None
    return created_at.astimezone(timezone.utc)


def _align_process_events(
    plan,
    events: Sequence[Mapping[str, object]],
) -> _ProcessEventAlignment:
    """Align Process steps to observations with a bounded, forward-only cursor."""

    cursor = 0
    process_id: str | None = None
    matched_events: list[Mapping[str, object] | None] = []
    matched_indices: list[int | None] = []
    unexpected_events: list[ProcessEventDiagnostic] = []
    missing_expected_events: list[ProcessEventDiagnostic] = []
    event_order_violations: list[ProcessEventDiagnostic] = []
    foreign_process_events: list[ProcessEventDiagnostic] = []
    unconsumed_events: list[ProcessEventDiagnostic] = []

    for step_index, step in enumerate(plan.steps):
        matched: Mapping[str, object] | None = None
        matched_index: int | None = None
        while cursor < len(events):
            event_index = cursor
            event = events[event_index]
            cursor += 1
            event_process_id = _event_process_id(event)
            if step.action is not ProcessAction.START:
                if process_id is None:
                    unexpected_events.append(
                        _event_diagnostic(
                            event,
                            event_index=event_index,
                            reason="unexpected_event",
                            step_id=step.step_id,
                        )
                    )
                    continue
                if event_process_id != process_id:
                    foreign_process_events.append(
                        _event_diagnostic(
                            event,
                            event_index=event_index,
                            reason="foreign_process_event",
                            step_id=step.step_id,
                        )
                    )
                    continue
            if _event_matches_step(event, step):
                matched = event
                matched_index = event_index
                if step.action is ProcessAction.START:
                    process_id = event_process_id
                break
            future_match = (
                process_id is not None
                and event_process_id == process_id
                and any(
                    _event_matches_step(event, candidate)
                    for candidate in plan.steps[step_index + 1 :]
                )
            )
            diagnostic = _event_diagnostic(
                event,
                event_index=event_index,
                reason=(
                    "event_order_violation" if future_match else "unexpected_event"
                ),
                step_id=step.step_id,
            )
            if future_match:
                event_order_violations.append(diagnostic)
            else:
                unexpected_events.append(diagnostic)
        matched_events.append(matched)
        matched_indices.append(matched_index)
        if matched is None:
            missing_expected_events.append(
                _event_diagnostic(
                    None,
                    event_index=cursor,
                    reason="missing_expected_event",
                    step_id=step.step_id,
                )
            )

    for event_index in range(cursor, len(events)):
        unconsumed_events.append(
            _event_diagnostic(
                events[event_index],
                event_index=event_index,
                reason="unconsumed_event",
            )
        )
    return _ProcessEventAlignment(
        matched_events=matched_events,
        matched_indices=matched_indices,
        unexpected_events=unexpected_events,
        missing_expected_events=missing_expected_events,
        event_order_violations=event_order_violations,
        foreign_process_events=foreign_process_events,
        unconsumed_events=unconsumed_events,
    )


_ALIGNMENT_ERROR_TYPES = {
    "unexpected_event": "process_unexpected_event",
    "missing_expected_event": "process_missing_expected_event",
    "event_order_violation": "process_event_order_violation",
    "foreign_process_event": "process_foreign_process_event",
    "unconsumed_event": "process_unconsumed_event",
}


def _alignment_error(plan, diagnostic: ProcessEventDiagnostic) -> ScenarioError:
    error_type = _ALIGNMENT_ERROR_TYPES[diagnostic.reason]
    step_label = diagnostic.step_id or "none"
    return _scenario_error(
        error_type,
        "scenario="
        f"{plan.scenario_id}; step={step_label}; event_index={diagnostic.event_index}; "
        f"{error_type}",
        step_id=diagnostic.step_id,
    )


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
    process_hook_boundaries: Mapping[
        str, tuple[int | None, int | None]
    ] | None = None,
    sensitive_values: Sequence[str] = (),
) -> tuple[list[ScenarioExecutionResult], list[ScenarioError], list[Path]]:
    process_hook_boundaries = process_hook_boundaries or {}
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
        alignment = _align_process_events(plan, process_events)
        matched_events = alignment.matched_events
        matched_indices = alignment.matched_indices
        matched_hook_boundaries: list[tuple[int | None, int | None] | None] = []
        for event in matched_events:
            tool_call_id = event.get("tool_call_id") if event else None
            bounds = (
                process_hook_boundaries.get(tool_call_id)
                if isinstance(tool_call_id, str)
                else None
            )
            matched_hook_boundaries.append(bounds)
        start_step_index = next(
            (
                index
                for index, candidate in enumerate(plan.steps)
                if candidate.action is ProcessAction.START
            ),
            None,
        )
        process_start_pre_ns = (
            matched_hook_boundaries[start_step_index][0]
            if start_step_index is not None
            and start_step_index < len(matched_hook_boundaries)
            and matched_hook_boundaries[start_step_index] is not None
            else None
        )
        hook_pre_origin_ns = process_start_pre_ns
        if hook_pre_origin_ns is None:
            pre_values = [
                bounds[0]
                for bounds in matched_hook_boundaries
                if bounds is not None and bounds[0] is not None
            ]
            hook_pre_origin_ns = min(pre_values) if pre_values else None
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
        tool_duration_sum_ms = sum(
            event.get("duration_ms")
            for event in process_events
            if type(event.get("duration_ms")) is int and event.get("duration_ms") >= 0
        )
        worker_watchdog_timed_out = any(
            item.runtime_status in {"timeout", "timed_out"}
            for item in turns
        )
        # The parent Runner computed this disposition and carried it in the
        # Worker request.  Never infer watchdog scope from plan.required here:
        # optional Process plans execute under the Trial watchdog.
        process_watchdog_enabled = request.process_watchdog_enabled
        hard_timeout_source = request.hard_timeout_source
        hard_timeout_seconds = request.hard_timeout_seconds
        wait_remaining_budget_status = WaitRemainingBudgetStatus.NOT_APPLICABLE
        wait_elapsed_before_ms: int | None = None
        wait_remaining_seconds: float | int | None = None
        wait_timeout_budget_matched: bool | None = None
        wait_budget_timing_source: ProcessWaitTimingSource | None = None
        hard_watchdog_fallback_allowed = False
        hard_watchdog_fallback_used = False
        for step_index, step in enumerate(plan.steps):
            event = matched_events[step_index]
            matched_index = matched_indices[step_index]
            hook_boundaries = matched_hook_boundaries[step_index]
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
            timing_source = (
                ProcessTimingSource.PUBLIC_DURATION_ONLY
                if timing_status is ProcessTimingStatus.AVAILABLE_DURATION_ONLY
                else ProcessTimingSource.UNAVAILABLE
            )
            timed_out = (
                None
                if duration_ms is None
                else duration_ms > step.timeout_seconds * 1000
            )
            step_wait_status: WaitRemainingBudgetStatus | None = None
            step_wait_elapsed_ms: int | None = None
            step_wait_remaining_seconds: float | None = None
            step_wait_timeout_matched: bool | None = None
            step_wait_timing_source: ProcessWaitTimingSource | None = (
                ProcessWaitTimingSource.UNAVAILABLE
            )
            step_fallback_allowed: bool | None = None
            step_fallback_used: bool | None = None
            if step.action is ProcessAction.WAIT:
                wait_timeout = arguments.get("timeout")
                step_fallback_allowed = (
                    step.allow_hard_watchdog_fallback
                    and hard_timeout_source
                    is ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
                )
                if (
                    process_start_pre_ns is not None
                    and hook_boundaries is not None
                    and hook_boundaries[0] is not None
                    and hook_boundaries[0] >= process_start_pre_ns
                ):
                    elapsed_before_wait_seconds = (
                        hook_boundaries[0] - process_start_pre_ns
                    ) / 1_000_000_000
                    step_wait_elapsed_ms = round(
                        elapsed_before_wait_seconds * 1000
                    )
                    step_wait_remaining_seconds = max(
                        0.0,
                        float(plan.timeout_seconds)
                        - elapsed_before_wait_seconds,
                    )
                    static_wait_ok = (
                        _budget_at_most(wait_timeout, float(step.timeout_seconds))
                        and _budget_at_most(
                            wait_timeout,
                            float(step.maximum_wait_seconds),
                        )
                        and _budget_at_most(
                            wait_timeout,
                            float(plan.timeout_seconds),
                        )
                    )
                    step_wait_timeout_matched = bool(
                        static_wait_ok
                        and _budget_at_most(
                            wait_timeout,
                            step_wait_remaining_seconds,
                        )
                    )
                    step_wait_status = (
                        WaitRemainingBudgetStatus.MATCHED
                        if step_wait_timeout_matched
                        else WaitRemainingBudgetStatus.MISMATCHED
                    )
                    step_wait_timing_source = (
                        ProcessWaitTimingSource.WORKER_PRE_TOOL_CONTROL_HOOKS
                    )
                elif (
                    step_fallback_allowed
                    and hard_timeout_source
                    is ProcessHardTimeoutSource.WORKER_PROCESS_SCENARIO_WATCHDOG
                    and not worker_watchdog_timed_out
                ):
                    step_wait_status = WaitRemainingBudgetStatus.FALLBACK_USED
                    step_fallback_used = True
                else:
                    step_wait_status = WaitRemainingBudgetStatus.UNAVAILABLE
                    step_fallback_used = False
                wait_elapsed_before_ms = step_wait_elapsed_ms
                wait_remaining_seconds = step_wait_remaining_seconds
                wait_timeout_budget_matched = step_wait_timeout_matched
                wait_budget_timing_source = step_wait_timing_source
                wait_remaining_budget_status = step_wait_status
                hard_watchdog_fallback_allowed = bool(step_fallback_allowed)
                hard_watchdog_fallback_used = bool(step_fallback_used)
                wait_ok = step_wait_status in {
                    WaitRemainingBudgetStatus.MATCHED,
                    WaitRemainingBudgetStatus.FALLBACK_USED,
                }
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
            step_error_type = (
                "process_missing_expected_event"
                if event is None
                else "process_step_gate_failed"
            )
            if event is not None and not passed and step.required:
                if timing_status is ProcessTimingStatus.UNAVAILABLE:
                    step_error_type = "process_step_timing_unavailable"
                elif timing_status is ProcessTimingStatus.INVALID:
                    step_error_type = "process_step_timing_invalid"
                elif timed_out is True:
                    step_error_type = "process_step_timeout"
            if (
                event is not None
                and not passed
                and step.required
                and step.action is ProcessAction.WAIT
            ):
                if step_wait_status is WaitRemainingBudgetStatus.MISMATCHED:
                    step_error_type = "process_wait_remaining_budget_mismatch"
                elif step_wait_status is WaitRemainingBudgetStatus.UNAVAILABLE:
                    step_error_type = "process_wait_remaining_budget_unavailable"
            if event is not None and step.action is ProcessAction.READ_INCREMENTAL:
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
                # Public Observation persistence does not provide per-tool
                # boundaries; only the handler duration is projected here.
                started_at=None,
                completed_at=None,
                duration_ms=duration_ms,
                timeout_seconds=step.timeout_seconds,
                timing_status=timing_status,
                timing_source=timing_source,
                timed_out=timed_out,
                event_pre_hook_offset_ms=(
                    None
                    if hook_pre_origin_ns is None
                    or hook_boundaries is None
                    or hook_boundaries[0] is None
                    or hook_boundaries[0] < hook_pre_origin_ns
                    else round(
                        (hook_boundaries[0] - hook_pre_origin_ns) / 1_000_000
                    )
                ),
                event_post_hook_offset_ms=(
                    None
                    if hook_pre_origin_ns is None
                    or hook_boundaries is None
                    or hook_boundaries[1] is None
                    or hook_boundaries[1] < hook_pre_origin_ns
                    else round(
                        (hook_boundaries[1] - hook_pre_origin_ns) / 1_000_000
                    )
                ),
                event_pre_hook_source=(
                    ProcessHookTimingSource.WORKER_PRE_TOOL_CONTROL_HOOK
                    if hook_pre_origin_ns is not None
                    and hook_boundaries is not None
                    and hook_boundaries[0] is not None
                    and hook_boundaries[0] >= hook_pre_origin_ns
                    else ProcessHookTimingSource.UNAVAILABLE
                ),
                event_post_hook_source=(
                    ProcessHookTimingSource.WORKER_POST_TOOL_PERSISTENCE_HOOK
                    if hook_pre_origin_ns is not None
                    and hook_boundaries is not None
                    and hook_boundaries[1] is not None
                    and hook_boundaries[1] >= hook_pre_origin_ns
                    else ProcessHookTimingSource.UNAVAILABLE
                ),
                elapsed_before_wait_ms=(
                    wait_elapsed_before_ms if step.action is ProcessAction.WAIT else None
                ),
                scenario_remaining_before_wait_seconds=(
                    wait_remaining_seconds if step.action is ProcessAction.WAIT else None
                ),
                wait_remaining_budget_status=(
                    step_wait_status if step.action is ProcessAction.WAIT else None
                ),
                wait_timeout_budget_matched=(
                    step_wait_timeout_matched if step.action is ProcessAction.WAIT else None
                ),
                wait_budget_timing_source=(
                    step_wait_timing_source if step.action is ProcessAction.WAIT else None
                ),
                hard_watchdog_fallback_allowed=(
                    step_fallback_allowed if step.action is ProcessAction.WAIT else None
                ),
                hard_watchdog_fallback_used=(
                    bool(step_fallback_used)
                    if step.action is ProcessAction.WAIT
                    else None
                ),
                observation_refs=[_safe_id(event.get("tool_call_id"))] if event else [],
                expected_process_id_safe=safe_process_id,
                actual_process_id_safe=event_safe_process_id,
                process_identity_matched=(True if step.action is ProcessAction.START else identity_match),
                action_matched=action_matched,
                error=None if passed else _scenario_error(
                    step_error_type,
                    f"scenario={plan.scenario_id}; step={step.step_id}; "
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
        matched_observation_times = [
            observation_time
            for event in matched_events
            if (observation_time := _event_observation_time(event)) is not None
        ]
        matched_event_count = sum(event is not None for event in matched_events)
        observation_span_status = ProcessObservationSpanStatus.UNAVAILABLE
        observation_started_at = None
        observation_completed_at = None
        observation_span_ms = None
        all_observation_timestamps_available = (
            matched_event_count > 0
            and len(matched_observation_times) == matched_event_count
        )
        if all_observation_timestamps_available:
            timestamps_monotonic = all(
                current >= previous
                for previous, current in zip(
                    matched_observation_times,
                    matched_observation_times[1:],
                    strict=False,
                )
            )
        else:
            timestamps_monotonic = None
        if all_observation_timestamps_available:
            observation_started_at = matched_observation_times[0]
            observation_completed_at = matched_observation_times[-1]
        if timestamps_monotonic is True:
            observation_span_ms = round(
                (observation_completed_at - observation_started_at).total_seconds()
                * 1000
            )
            observation_span_status = ProcessObservationSpanStatus.AVAILABLE
        elif timestamps_monotonic is False:
            observation_span_status = ProcessObservationSpanStatus.INVALID
        scenario_observation_timing_source = (
            ProcessTimingSource.PUBLIC_OBSERVATION_PERSISTENCE
            if observation_span_status
            in {
                ProcessObservationSpanStatus.AVAILABLE,
                ProcessObservationSpanStatus.INVALID,
            }
            else ProcessTimingSource.UNAVAILABLE
        )
        pre_hook_values = [
            bounds[0]
            for bounds in matched_hook_boundaries
            if bounds is not None and bounds[0] is not None
        ]
        post_hook_values = [
            bounds[1]
            for bounds in matched_hook_boundaries
            if bounds is not None and bounds[1] is not None
        ]
        scenario_hook_span_status = ProcessHookSpanStatus.UNAVAILABLE
        scenario_pre_to_post_hook_span_ms = None
        if pre_hook_values and post_hook_values:
            first_pre_hook_ns = min(pre_hook_values)
            last_post_hook_ns = max(post_hook_values)
            if last_post_hook_ns >= first_pre_hook_ns:
                scenario_hook_span_status = ProcessHookSpanStatus.AVAILABLE
                scenario_pre_to_post_hook_span_ms = round(
                    (last_post_hook_ns - first_pre_hook_ns) / 1_000_000
                )
            else:
                scenario_hook_span_status = ProcessHookSpanStatus.INVALID
        effective_hard_timeout_seconds = hard_timeout_seconds
        scenario_observation_span_exceeded = (
            None
            if observation_span_ms is None
            else observation_span_ms > hard_timeout_seconds * 1000
        )
        hard_timeout_triggered = worker_watchdog_timed_out
        scenario_watchdog_timed_out = (
            worker_watchdog_timed_out and process_watchdog_enabled
        )
        trial_watchdog_timed_out = (
            worker_watchdog_timed_out and not process_watchdog_enabled
        )
        wait_steps = [
            item for item in step_results if item.action is ProcessAction.WAIT
        ]
        if not wait_steps:
            wait_remaining_budget_status = WaitRemainingBudgetStatus.NOT_APPLICABLE
            process_start_pre_hook_available = process_start_pre_ns is not None
            wait_pre_hook_available = None
            wait_elapsed_before_ms = None
            wait_remaining_seconds = None
            wait_timeout_budget_matched = None
            wait_budget_timing_source = None
            hard_watchdog_fallback_allowed = False
            hard_watchdog_fallback_used = False
        else:
            process_start_pre_hook_available = process_start_pre_ns is not None
            wait_pre_hook_available = all(
                item.event_pre_hook_offset_ms is not None
                for item in wait_steps
            )
            wait_statuses = [item.wait_remaining_budget_status for item in wait_steps]
            exact_wait_steps = [
                item
                for item in wait_steps
                if item.wait_remaining_budget_status
                in {
                    WaitRemainingBudgetStatus.MATCHED,
                    WaitRemainingBudgetStatus.MISMATCHED,
                }
            ]
            representative = exact_wait_steps[0] if exact_wait_steps else None
            hard_watchdog_fallback_allowed = any(
                item.hard_watchdog_fallback_allowed is True
                for item in wait_steps
            )
            hard_watchdog_fallback_used = any(
                item.hard_watchdog_fallback_used is True
                for item in wait_steps
            )
            if any(
                status is WaitRemainingBudgetStatus.MISMATCHED
                for status in wait_statuses
            ):
                wait_remaining_budget_status = WaitRemainingBudgetStatus.MISMATCHED
            elif any(
                status is WaitRemainingBudgetStatus.FALLBACK_USED
                for status in wait_statuses
            ):
                wait_remaining_budget_status = WaitRemainingBudgetStatus.FALLBACK_USED
            elif all(
                status is WaitRemainingBudgetStatus.MATCHED
                for status in wait_statuses
            ):
                wait_remaining_budget_status = WaitRemainingBudgetStatus.MATCHED
            else:
                wait_remaining_budget_status = WaitRemainingBudgetStatus.UNAVAILABLE
            if representative is None:
                wait_elapsed_before_ms = None
                wait_remaining_seconds = None
                wait_timeout_budget_matched = None
                wait_budget_timing_source = ProcessWaitTimingSource.UNAVAILABLE
            else:
                wait_elapsed_before_ms = representative.elapsed_before_wait_ms
                wait_remaining_seconds = representative.scenario_remaining_before_wait_seconds
                wait_timeout_budget_matched = (
                    all(item.wait_timeout_budget_matched is True for item in wait_steps)
                    if wait_remaining_budget_status is WaitRemainingBudgetStatus.MATCHED
                    else False
                )
                wait_budget_timing_source = ProcessWaitTimingSource.WORKER_PRE_TOOL_CONTROL_HOOKS
            if wait_remaining_budget_status is WaitRemainingBudgetStatus.FALLBACK_USED:
                wait_elapsed_before_ms = None
                wait_remaining_seconds = None
                wait_timeout_budget_matched = None
                wait_budget_timing_source = ProcessWaitTimingSource.UNAVAILABLE
        scenario_duration_ms = observation_span_ms
        alignment_diagnostics = (
            alignment.unexpected_events
            + alignment.missing_expected_events
            + alignment.event_order_violations
            + alignment.foreign_process_events
            + alignment.unconsumed_events
        )
        scenario_errors = [
            _alignment_error(plan, diagnostic)
            for diagnostic in alignment_diagnostics
        ] + list(checkpoint_errors)
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
                "process_wait_remaining_budget_unavailable",
                "process_wait_remaining_budget_mismatch",
            }:
                scenario_errors.append(step_result.error)
        if hard_timeout_triggered:
            scenario_errors.append(
                _scenario_error(
                    (
                        "process_scenario_watchdog_timeout"
                        if process_watchdog_enabled
                        else "trial_watchdog_timeout"
                    ),
                    f"scenario={plan.scenario_id}; Worker case watchdog exceeded",
                )
            )
        if observation_span_status is ProcessObservationSpanStatus.INVALID:
            scenario_errors.append(
                _scenario_error(
                    "process_scenario_observation_span_invalid",
                    f"scenario={plan.scenario_id}; public observation persistence timestamps were not ordered",
                )
            )
        if scenario_observation_span_exceeded is True:
            scenario_errors.append(
                _scenario_error(
                    "process_scenario_observation_span_exceeded",
                    f"scenario={plan.scenario_id}; public observation span exceeded the hard budget",
                )
            )
        scenario_status = ScenarioStatus.COMPLETED if (
            completed and required_steps_ok and cleanup_ok and process_identity_matched
            and bool(command_matched) and (input_matched is not False)
            and cursor_integrity and status_transitions_valid and trace_passed
            and (not fixture_read_required or file_fixture_read_observed)
            and not alignment_diagnostics
            and not hard_timeout_triggered
            and observation_span_status is not ProcessObservationSpanStatus.INVALID
            and scenario_observation_span_exceeded is not True
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
            scenario_observation_span_status=observation_span_status,
            scenario_observation_timing_source=scenario_observation_timing_source,
            scenario_observation_started_at=observation_started_at,
            scenario_observation_completed_at=observation_completed_at,
            scenario_observation_span_ms=observation_span_ms,
            scenario_hook_span_status=scenario_hook_span_status,
            scenario_pre_to_post_hook_span_ms=scenario_pre_to_post_hook_span_ms,
            hard_timeout_source=hard_timeout_source,
            hard_timeout_seconds=effective_hard_timeout_seconds,
            hard_timeout_triggered=hard_timeout_triggered,
            trial_watchdog_timed_out=trial_watchdog_timed_out,
            scenario_watchdog_timed_out=scenario_watchdog_timed_out,
            scenario_observation_span_exceeded=scenario_observation_span_exceeded,
            wait_remaining_budget_status=wait_remaining_budget_status,
            process_start_pre_hook_available=process_start_pre_hook_available,
            wait_pre_hook_available=wait_pre_hook_available,
            elapsed_before_wait_ms=wait_elapsed_before_ms,
            scenario_remaining_before_wait_seconds=wait_remaining_seconds,
            wait_timeout_budget_matched=wait_timeout_budget_matched,
            wait_budget_timing_source=wait_budget_timing_source,
            hard_watchdog_fallback_allowed=hard_watchdog_fallback_allowed,
            hard_watchdog_fallback_used=hard_watchdog_fallback_used,
            agent_close_required=any(
                item.action is ProcessAction.CLOSE and item.required
                for item in plan.steps
            ),
            agent_close_observed=agent_close_observed,
            worker_cleanup_result=cleanup,
            unexpected_events=alignment.unexpected_events,
            missing_expected_events=alignment.missing_expected_events,
            event_order_violations=alignment.event_order_violations,
            foreign_process_events=alignment.foreign_process_events,
            unconsumed_events=alignment.unconsumed_events,
            tool_duration_sum_ms=tool_duration_sum_ms,
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
