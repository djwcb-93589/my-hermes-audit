"""Parent-side lifecycle for the isolated MyHermes capability probe."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json
from myhermes_audit.environment import (
    WORKER_INHERITED_ENVIRONMENT_ALLOWLIST,
    is_sensitive_environment_name,
)
from myhermes_audit.errors import SubjectCapabilityError
from myhermes_audit.integrations.myhermes.capability_contracts import (
    SubjectCapabilityProbeRequest,
    SubjectCapabilityReport,
)
from myhermes_audit.integrations.myhermes.config_builder import MyHermesConfigBuilder


_PROBE_TIMEOUT_SECONDS = 30
_MAX_REPORT_BYTES = 2 * 1024 * 1024
_PROBE_PREFIX = "myhermes-audit-capability-"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _casefolded_environment() -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in normalized or key == upper:
            normalized[upper] = value
    return normalized


def _probe_reference_value(name: str, parent: dict[str, str]) -> str | None:
    if is_sensitive_environment_name(name):
        return "myhermes-audit-capability-probe-placeholder"
    if name in parent:
        return parent[name]
    defaults = {
        "FALLBACK_BASE_URL": "https://invalid.local/v1",
        "FALLBACK_MAX_OUTPUT_TOKENS": "1",
        "FALLBACK_MODEL": "capability-probe-model",
        "MAX_ITERATIONS": "1",
        "MODEL": "capability-probe-model",
        "MODEL_MAX_OUTPUT_TOKENS": "1",
        "MODEL_TIMEOUT_SECONDS": "1",
        "OPENAI_BASE_URL": "https://invalid.local/v1",
    }
    return defaults.get(name)


def _build_probe_environment(
    *,
    subject_repo: Path,
    hermes_home: Path,
    workspace: Path,
    sqlite_path: Path,
    environment_references: tuple[str, ...],
) -> dict[str, str]:
    parent = _casefolded_environment()
    environment = {
        name: parent[name]
        for name in WORKER_INHERITED_ENVIRONMENT_ALLOWLIST
        if name in parent
    }
    for name in environment_references:
        value = _probe_reference_value(name, parent)
        if value is not None:
            environment[name] = value
    environment["OPENAI_API_KEY"] = (
        "myhermes-audit-capability-probe-placeholder"
    )
    environment["FALLBACK_API_KEY"] = (
        "myhermes-audit-capability-probe-placeholder"
    )
    audit_import_root = Path(__file__).resolve().parents[3]
    environment.update(
        {
            "DB_PATH": str(sqlite_path),
            "HERMES_HOME": str(hermes_home),
            "HERMES_WORKSPACE": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONSAFEPATH": "1",
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(subject_repo), str(audit_import_root))
            ),
        }
    )
    return environment


def _terminate_probe(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        elif os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError, ValueError):
        pass
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise SubjectCapabilityError(
            "Subject Capability Probe process group did not terminate"
        ) from exc


def _read_report(path: Path) -> SubjectCapabilityReport:
    if path.is_symlink() or not path.is_file():
        raise SubjectCapabilityError(
            "Subject Capability Probe did not produce a regular report"
        )
    try:
        if path.stat().st_size > _MAX_REPORT_BYTES:
            raise SubjectCapabilityError(
                "Subject Capability Probe report exceeds the size limit"
            )
        text = path.read_text(encoding="utf-8")
        json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        return SubjectCapabilityReport.model_validate_json(text)
    except SubjectCapabilityError:
        raise
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise SubjectCapabilityError(
            "Subject Capability Probe report is invalid"
        ) from exc


def _cleanup_probe_root(root: Path) -> None:
    resolved = root.resolve(strict=True)
    temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    if resolved.parent != temporary_parent or not resolved.name.startswith(
        _PROBE_PREFIX
    ):
        raise SubjectCapabilityError(
            "refusing to clean an unowned capability probe directory"
        )
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        raise SubjectCapabilityError(
            "cannot clean Subject Capability Probe directory"
        ) from exc


def _missing_capability_label(name: str, failure_type: str | None) -> str:
    description = {
        "module_unavailable": "public module unavailable",
        "symbol_missing": "public symbol missing",
        "symbol_unavailable": "public symbol unavailable",
        "symbol_not_callable": "public symbol is not callable",
        "call_shape_incompatible": "Worker call shape incompatible",
        "signature_unavailable": "public signature unavailable",
        "signature_validation_failed": "public signature validation failed",
    }.get(failure_type)
    return name if description is None else f"{name} ({description})"


def run_subject_capability_probe(
    *,
    subject_repo: Path,
    subject_config: Path,
    subject_commit: str,
    timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
) -> SubjectCapabilityReport:
    root = Path(tempfile.mkdtemp(prefix=_PROBE_PREFIX)).resolve(strict=True)
    failure: Exception | None = None
    report: SubjectCapabilityReport | None = None
    try:
        protocol_dir = root / "protocol"
        hermes_home = root / "hermes_home"
        workspace = root / "workspace"
        database_dir = root / "database"
        for directory in (protocol_dir, hermes_home, workspace, database_dir):
            directory.mkdir(exist_ok=False)
        request_path = protocol_dir / "request.json"
        result_path = protocol_dir / "result.json"
        sqlite_path = database_dir / "probe.db"
        builder = MyHermesConfigBuilder(subject_config)
        prepared = builder.write(hermes_home / "config.yaml", {})
        request = SubjectCapabilityProbeRequest(
            subject_repo=str(Path(subject_repo).resolve(strict=True)),
            subject_commit=subject_commit,
        )
        atomic_write_json(request_path, request)
        command = [
            sys.executable,
            "-P",
            "-m",
            "myhermes_audit.integrations.myhermes.capability_probe",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ]
        kwargs: dict[str, object] = {
            "cwd": str(workspace),
            "env": _build_probe_environment(
                subject_repo=Path(subject_repo).resolve(strict=True),
                hermes_home=hermes_home,
                workspace=workspace,
                sqlite_path=sqlite_path,
                environment_references=prepared.environment_references,
            ),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise SubjectCapabilityError(
                "cannot start Subject Capability Probe"
            ) from exc
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_probe(process)
            raise SubjectCapabilityError(
                "Subject Capability Probe timed out"
            ) from exc
        report = _read_report(result_path)
        if report.subject_commit != subject_commit:
            raise SubjectCapabilityError(
                "Subject Capability Probe commit identity does not match"
            )
        expected_returncode = 0 if report.compatible else 1
        if returncode != expected_returncode:
            raise SubjectCapabilityError(
                "Subject Capability Probe exit status does not match its report"
            )
    except Exception as exc:
        failure = exc
    finally:
        try:
            _cleanup_probe_root(root)
        except Exception as cleanup_exc:
            if failure is None:
                failure = cleanup_exc
    if failure is not None:
        if isinstance(failure, SubjectCapabilityError):
            raise failure
        raise SubjectCapabilityError(
            f"Subject Capability Probe failed: {type(failure).__name__}"
        ) from failure
    if report is None:
        raise SubjectCapabilityError("Subject Capability Probe returned no report")
    if not report.compatible:
        checks = {item.name: item for item in report.capabilities}
        missing = ", ".join(
            _missing_capability_label(
                name,
                None if checks.get(name) is None else checks[name].failure_type,
            )
            for name in report.missing_capabilities
        )
        capability_failures = {
            name: checks[name].failure_type
            for name in report.missing_capabilities
            if name in checks and checks[name].failure_type is not None
        }
        raise SubjectCapabilityError(
            f"required public MyHermes capabilities are missing: {missing}",
            missing_capabilities=list(report.missing_capabilities),
            capability_failures=capability_failures,
            public_api_fingerprint=report.public_api_fingerprint,
        )
    return report


__all__ = ("run_subject_capability_probe",)
