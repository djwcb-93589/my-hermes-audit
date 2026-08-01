"""Parent-side MyHermes subprocess adapter; never imports hermes modules."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json, atomic_write_text
from myhermes_audit.contracts import (
    AuditCase,
    ModelObservationSummary,
    RunObservationSummary,
    ToolObservationSummary,
    TrialObservationSummary,
    TrialRuntimeSummary,
    TrialWarning,
)
from myhermes_audit.contracts.suite import (
    CaseMode,
    ConversationRole,
    EvaluatorKind,
    TextTarget,
)
from myhermes_audit.datasets.fixtures import validate_p1_fixture_support
from myhermes_audit.environment import (
    MODEL_ENVIRONMENT_ALLOWLIST,
    WORKER_INHERITED_ENVIRONMENT_ALLOWLIST,
)
from myhermes_audit.errors import (
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
    MyHermesWorkerRequest,
    MyHermesWorkerResult,
    ObservationBundle,
    WORKER_PROTOCOL_VERSION,
    WorkerArtifactPaths,
    WorkerError,
    WorkerMode,
    WorkerStatus,
    WorkerTranscript,
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
        validate_p1_fixture_support(case.fixture)
        self._config_builder.prepare(case.execution.config_overrides)

        unsupported_evaluators = [
            item.kind.value
            for item in case.evaluators
            if item.kind not in {
                EvaluatorKind.DETERMINISTIC,
                EvaluatorKind.TOOL_TRAJECTORY,
                EvaluatorKind.LLM_JUDGE,
            }
        ]
        if unsupported_evaluators:
            raise UnsupportedCaseError(
                "case uses evaluators outside the P1 boundary",
                case_id=case.case_id,
                evaluator_kinds=unsupported_evaluators,
            )
        if (
            case.expected.memories or case.expected.background_reviews
        ):
            raise UnsupportedCaseError(
                "case declares expectations outside the P1 boundary",
                case_id=case.case_id,
            )
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

    def run_trial(
        self,
        case: AuditCase,
        sandbox: AuditSandbox,
        *,
        trial_id: str,
        timeout_seconds: int,
    ) -> TrialRunnerOutcome:
        paths = _worker_artifact_paths(sandbox)
        started = time.perf_counter()
        captured_stdout = ""
        captured_stderr = ""
        process = None
        subject_model: str | None = None
        sensitive_values = self._sensitive_values
        try:
            turns = [
                redact_text(message, sensitive_values)
                for message in _case_turns(case)
            ]
            request = MyHermesWorkerRequest(
                trial_id=trial_id,
                case_id=case.case_id,
                mode=WorkerMode(case.mode.value),
                turns=turns,
                workspace=sandbox.workspace.resolve(strict=True),
                hermes_home=sandbox.hermes_home.resolve(strict=True),
                sqlite_path=sandbox.sqlite_path.resolve(strict=False),
                enabled_toolsets=case.execution.enabled_toolsets,
                timeout_seconds=timeout_seconds,
                artifact_paths=paths,
            )
            atomic_write_json(paths.worker_request, request)
            prepared = self._config_builder.write(
                sandbox.hermes_home / "config.yaml",
                case.execution.config_overrides,
            )
            subject_model = _safe_subject_model(
                prepared.document,
                sensitive_values,
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
                result = _fallback_worker_result(
                    paths,
                    error_type="timeout",
                    message="MyHermes worker exceeded the Trial timeout",
                    duration_ms=duration_ms,
                    warnings=runtime_warnings,
                )
                atomic_write_json(paths.worker_result, result)
                _ensure_empty_worker_artifacts(paths, trial_id, case.case_id)
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
            _validate_worker_artifacts(
                request,
                result,
                transcript,
                observations,
                returncode=process.returncode,
            )
            result, transcript = _redact_worker_content(
                result,
                transcript,
                sensitive_values,
            )
            atomic_write_json(paths.worker_result, result)
            atomic_write_json(paths.transcript, transcript)
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
            result = _fallback_worker_result(
                paths,
                error_type="environment_error",
                message=f"worker environment failed: {type(exc).__name__}",
                duration_ms=duration_ms,
                warnings=worker_warnings,
            )
            try:
                atomic_write_json(paths.worker_result, result)
                _ensure_empty_worker_artifacts(paths, trial_id, case.case_id)
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
            tool_calls=tool_calls,
            tool_trace_complete=(
                status is RunnerStatus.COMPLETED
                and observations is not None
                and not observations.truncated
            ),
            artifact_paths={
                field_name: getattr(paths, field_name)
                for field_name in type(paths).model_fields
                if field_name != "schema_version"
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


def _safe_subject_model(
    document: dict,
    sensitive_values: tuple[str, ...],
) -> str | None:
    value = document.get("model")
    if not isinstance(value, str) or not value.strip():
        return None
    return redact_text(value.strip(), sensitive_values)[:256]


def _case_turns(case: AuditCase) -> list[str]:
    if case.mode is CaseMode.SINGLE_TURN:
        if case.input.message is None:
            raise UnsupportedCaseError("single_turn case has no input message")
        return [case.input.message]
    return [turn.message for turn in case.input.turns]


def _worker_artifact_paths(sandbox: AuditSandbox) -> WorkerArtifactPaths:
    root = sandbox.artifacts_dir.resolve(strict=True)
    return WorkerArtifactPaths(
        worker_request=root / "worker-request.json",
        worker_result=root / "worker-result.json",
        transcript=root / "transcript.json",
        observations=root / "observations.json",
        validator_results=root / "validator-results.json",
        stdout_log=root / "worker.stdout.log",
        stderr_log=root / "worker.stderr.log",
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
    *,
    returncode: int,
) -> None:
    if transcript.trial_id != request.trial_id or transcript.case_id != request.case_id:
        raise WorkerProtocolError("worker transcript identity does not match request")
    if transcript.turns != result.turns:
        raise WorkerProtocolError("worker transcript turns do not match result")
    observed_messages = [turn.user_message for turn in result.turns]
    if observed_messages != request.turns[: len(observed_messages)]:
        raise WorkerProtocolError("worker transcript messages do not match request")
    if (
        result.worker_status is WorkerStatus.COMPLETED
        and len(result.turns) != len(request.turns)
    ):
        raise WorkerProtocolError("completed worker did not execute every requested turn")
    if result.observations_artifact != "artifacts/observations.json":
        raise WorkerProtocolError("worker result names an unexpected Observation artifact")
    if result.transcript_artifact != "artifacts/transcript.json":
        raise WorkerProtocolError("worker result names an unexpected transcript artifact")
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
) -> MyHermesWorkerResult:
    return MyHermesWorkerResult(
        worker_status=WorkerStatus.FAILED,
        runtime_status=error_type,
        error_type=error_type,
        fatal=True,
        retryable=False,
        duration_ms=duration_ms,
        observations_artifact=f"artifacts/{paths.observations.name}",
        transcript_artifact=f"artifacts/{paths.transcript.name}",
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
) -> None:
    if not paths.observations.exists():
        atomic_write_json(paths.observations, ObservationBundle())
    if not paths.transcript.exists():
        atomic_write_json(
            paths.transcript,
            WorkerTranscript(trial_id=trial_id, case_id=case_id),
        )


__all__ = ("MyHermesTrialRunner",)
