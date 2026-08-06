"""Small, no-network safety helpers used only by the P8 GitHub workflows.

The helper intentionally validates the dispatch values before a shell command
uses them, and projects only safe fields from already strict Audit facts.  It
does not run an Audit, contact a model, or reimplement Benchmark aggregation.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json, atomic_write_text
from myhermes_audit.contracts import (
    AuditBaseline,
    AuditRegressionReport,
    AuditRunResult,
)


_MAX_FACT_BYTES = 100 * 1024 * 1024
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9._-]*$")
_SAFE_CODE = re.compile(r"^[a-z0-9_]{1,80}$")
_SAFE_OUTPUT_KEYS = frozenset(
    {
        "baseline_path",
        "current_failed_case_count",
        "regression_gate_exit_code",
        "regression_status",
        "subject_config_path",
        "trials",
    }
)


class CiSupportError(Exception):
    """A deliberately content- and path-free CI diagnostic."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_relative_path(value: str, *, code: str) -> PurePosixPath:
    if not value or value != value.strip():
        raise CiSupportError(code)
    if "\\" in value or ":" in value or "\x00" in value:
        raise CiSupportError(code)
    if any(ord(character) < 32 for character in value):
        raise CiSupportError(code)
    if value.startswith(("/", "~")):
        raise CiSupportError(code)
    raw_parts = value.split("/")
    if not raw_parts or any(
        not part
        or part in {".", ".."}
        or _SAFE_PATH_SEGMENT.fullmatch(part) is None
        for part in raw_parts
    ):
        raise CiSupportError(code)
    return PurePosixPath(*raw_parts)


def _strict_root(value: str, *, code: str) -> Path:
    requested = Path(value)
    if requested.is_symlink():
        raise CiSupportError(code)
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise CiSupportError(code) from exc
    if not resolved.is_dir():
        raise CiSupportError(code)
    return resolved


def _regular_child(root: Path, relative: PurePosixPath, *, code: str) -> Path:
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise CiSupportError(code)
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise CiSupportError(code) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise CiSupportError(code)
    return resolved


def _safe_output_path(value: str) -> Path:
    root = Path.cwd().resolve(strict=True)
    relative = _safe_relative_path(value, code="invalid_artifact_output")
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        if current.exists() and current.is_symlink():
            raise CiSupportError("invalid_artifact_output")
        if index < len(relative.parts) - 1 and current.exists() and not current.is_dir():
            raise CiSupportError("invalid_artifact_output")
    return root.joinpath(*relative.parts)


def _write_github_output(path_value: str, values: dict[str, str]) -> None:
    if not values.keys() <= _SAFE_OUTPUT_KEYS:
        raise CiSupportError("invalid_github_output")
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise CiSupportError("invalid_github_output")
    try:
        with Path(path_value).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    except OSError as exc:
        raise CiSupportError("github_output_unavailable") from exc


def _read_model(path_value: str, model_type: type[Any], *, code: str):
    path = Path(path_value)
    if path.is_symlink():
        raise CiSupportError(code)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > _MAX_FACT_BYTES:
            raise CiSupportError(code)
        text = resolved.read_text(encoding="utf-8")
        return model_type.model_validate_json(text)
    except CiSupportError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, ValidationError) as exc:
        raise CiSupportError(code) from exc


def _read_result(path_value: str) -> AuditRunResult:
    return _read_model(path_value, AuditRunResult, code="invalid_audit_run_result")


def _read_baseline(path_value: str) -> AuditBaseline:
    return _read_model(path_value, AuditBaseline, code="invalid_audit_baseline")


def _read_regression(path_value: str) -> AuditRegressionReport:
    return _read_model(
        path_value,
        AuditRegressionReport,
        code="invalid_audit_regression_report",
    )


def _parse_exit_code(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise CiSupportError("invalid_exit_code")
    parsed = int(value)
    if parsed > 255:
        raise CiSupportError("invalid_exit_code")
    return parsed


def _failed_case_count(result: AuditRunResult) -> int:
    return sum(case.passed_count < case.trial_count for case in result.cases)


def _run_manifest(result: AuditRunResult, *, exit_code: int) -> dict[str, object]:
    return {
        "artifact_manifest_version": "p8-ci-safe-manifest-v1",
        "fact_source": "AuditRunResult",
        "result_schema_version": result.schema_version,
        "run_id": result.run_id,
        "suite_id": result.suite_id,
        "subject_commit": result.subject_fingerprint.git_commit,
        "audit_commit": result.audit_fingerprint.audit_commit,
        "run_configuration_fingerprint": result.run_configuration_fingerprint,
        "run_exit_code": exit_code,
    }


def _run_console(result: AuditRunResult, *, exit_label: str, exit_code: int) -> str:
    return "\n".join(
        (
            "artifact_status=complete",
            "fact_source=audit_run_result",
            "strict_result=validated",
            f"{exit_label}={exit_code}",
            f"run_id={result.run_id}",
            f"suite_id={result.suite_id}",
            f"current_failed_case_count={_failed_case_count(result)}",
            f"failed_trial_count={result.summary.failure_count}",
            f"local_execution_status={result.local_execution_status.value}",
            "judge=disabled",
            "langfuse=disabled",
        )
    ) + "\n"


def _failure_manifest(*, stage: str, reason: str, exit_code: int) -> dict[str, object]:
    if _SAFE_CODE.fullmatch(stage) is None or _SAFE_CODE.fullmatch(reason) is None:
        raise CiSupportError("invalid_failure_summary")
    return {
        "artifact_manifest_version": "p8-ci-safe-manifest-v1",
        "artifact_status": "unavailable",
        "stage": stage,
        "reason": reason,
        "exit_code": exit_code,
    }


def _write_failure_artifacts(arguments: argparse.Namespace) -> None:
    exit_code = _parse_exit_code(arguments.exit_code)
    manifest = _failure_manifest(
        stage=arguments.stage,
        reason=arguments.reason,
        exit_code=exit_code,
    )
    atomic_write_json(_safe_output_path(arguments.manifest), manifest)
    atomic_write_text(
        _safe_output_path(arguments.console),
        "\n".join(
            (
                "artifact_status=unavailable",
                f"stage={arguments.stage}",
                f"reason={arguments.reason}",
                f"exit_code={exit_code}",
                "details=redacted",
            )
        )
        + "\n",
    )


def _write_run_artifacts(arguments: argparse.Namespace) -> None:
    result = _read_result(arguments.result)
    exit_code = _parse_exit_code(arguments.exit_code)
    if arguments.exit_label not in {"run_exit_code", "current_run_exit_code"}:
        raise CiSupportError("invalid_exit_label")
    atomic_write_json(
        _safe_output_path(arguments.manifest),
        _run_manifest(result, exit_code=exit_code),
    )
    atomic_write_text(
        _safe_output_path(arguments.console),
        _run_console(result, exit_label=arguments.exit_label, exit_code=exit_code),
    )


def _write_regression_artifacts(arguments: argparse.Namespace) -> None:
    result = _read_result(arguments.result)
    report = _read_regression(arguments.regression)
    run_exit_code = _parse_exit_code(arguments.run_exit_code)
    compare_exit_code = _parse_exit_code(arguments.compare_exit_code)
    if (
        report.current_run_id != result.run_id
        or report.suite_id != result.suite_id
        or report.current_subject_commit != result.subject_fingerprint.git_commit
        or report.current_result_schema_version != result.schema_version
    ):
        raise CiSupportError("regression_current_result_mismatch")
    manifest = {
        "artifact_manifest_version": "p8-ci-safe-manifest-v1",
        "fact_source": ["AuditRunResult", "AuditRegressionReport"],
        "current_result_schema_version": result.schema_version,
        "regression_schema_version": report.schema_version,
        "baseline_id": report.baseline_id,
        "current_run_id": report.current_run_id,
        "suite_id": report.suite_id,
        "current_subject_commit": report.current_subject_commit,
        "current_run_exit_code": run_exit_code,
        "compare_exit_code": compare_exit_code,
        "regression_status": report.status.value,
        "overall_regression_gate": report.overall_regression_gate,
    }
    atomic_write_json(_safe_output_path(arguments.manifest), manifest)
    atomic_write_text(
        _safe_output_path(arguments.console),
        "\n".join(
            (
                "artifact_status=complete",
                "fact_source=audit_run_result,audit_regression_report",
                "strict_current_result=validated",
                "strict_regression_report=validated",
                f"current_run_exit_code={run_exit_code}",
                f"compare_exit_code={compare_exit_code}",
                f"current_failed_case_count={_failed_case_count(result)}",
                f"regression_status={report.status.value}",
                f"overall_regression_gate={str(report.overall_regression_gate).lower()}",
                f"baseline_id={report.baseline_id}",
                f"current_run_id={report.current_run_id}",
                "judge=disabled",
                "langfuse=disabled",
            )
        )
        + "\n",
    )
    if arguments.github_output:
        _write_github_output(
            arguments.github_output,
            {
                "current_failed_case_count": str(_failed_case_count(result)),
                "regression_gate_exit_code": "0" if report.overall_regression_gate else "1",
                "regression_status": report.status.value,
            },
        )


def _validate_trials(arguments: argparse.Namespace) -> None:
    if re.fullmatch(r"[0-9]+", arguments.value) is None:
        raise CiSupportError("invalid_trials")
    trials = int(arguments.value)
    if not 1 <= trials <= 100:
        raise CiSupportError("invalid_trials")
    _write_github_output(arguments.github_output, {"trials": str(trials)})


def _validate_subject_config(arguments: argparse.Namespace) -> None:
    root = _strict_root(arguments.root, code="invalid_subject_checkout")
    relative = _safe_relative_path(arguments.value, code="invalid_subject_config")
    _regular_child(root, relative, code="invalid_subject_config")
    _write_github_output(
        arguments.github_output,
        {"subject_config_path": (Path("my-hermes") / relative).as_posix()},
    )


def _validate_baseline(arguments: argparse.Namespace) -> None:
    root = _strict_root(arguments.root, code="invalid_audit_checkout")
    relative = _safe_relative_path(arguments.value, code="invalid_baseline_path")
    baseline = _regular_child(root, relative, code="invalid_baseline_path")
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative.as_posix()],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CiSupportError("baseline_tracking_unavailable") from exc
    if tracked.returncode != 0:
        raise CiSupportError("baseline_not_tracked")
    _read_baseline(str(baseline))
    _write_github_output(
        arguments.github_output,
        {"baseline_path": relative.as_posix()},
    )


def _validate_fact(arguments: argparse.Namespace) -> None:
    loaders = {
        "run": _read_result,
        "baseline": _read_baseline,
        "regression": _read_regression,
    }
    loaders[arguments.kind](arguments.path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P8 CI safety helper")
    commands = parser.add_subparsers(dest="command", required=True)

    trials = commands.add_parser("validate-trials")
    trials.add_argument("--value", required=True)
    trials.add_argument("--github-output", required=True)
    trials.set_defaults(handler=_validate_trials)

    subject = commands.add_parser("validate-subject-config")
    subject.add_argument("--root", required=True)
    subject.add_argument("--value", required=True)
    subject.add_argument("--github-output", required=True)
    subject.set_defaults(handler=_validate_subject_config)

    baseline = commands.add_parser("validate-baseline")
    baseline.add_argument("--root", required=True)
    baseline.add_argument("--value", required=True)
    baseline.add_argument("--github-output", required=True)
    baseline.set_defaults(handler=_validate_baseline)

    fact = commands.add_parser("validate-fact")
    fact.add_argument("--kind", choices=("run", "baseline", "regression"), required=True)
    fact.add_argument("--path", required=True)
    fact.set_defaults(handler=_validate_fact)

    failure = commands.add_parser("write-failure-artifacts")
    failure.add_argument("--manifest", required=True)
    failure.add_argument("--console", required=True)
    failure.add_argument("--stage", required=True)
    failure.add_argument("--reason", required=True)
    failure.add_argument("--exit-code", required=True)
    failure.set_defaults(handler=_write_failure_artifacts)

    run = commands.add_parser("write-run-artifacts")
    run.add_argument("--result", required=True)
    run.add_argument("--manifest", required=True)
    run.add_argument("--console", required=True)
    run.add_argument("--exit-code", required=True)
    run.add_argument("--exit-label", required=True)
    run.set_defaults(handler=_write_run_artifacts)

    regression = commands.add_parser("write-regression-artifacts")
    regression.add_argument("--result", required=True)
    regression.add_argument("--regression", required=True)
    regression.add_argument("--manifest", required=True)
    regression.add_argument("--console", required=True)
    regression.add_argument("--run-exit-code", required=True)
    regression.add_argument("--compare-exit-code", required=True)
    regression.add_argument("--github-output")
    regression.set_defaults(handler=_write_regression_artifacts)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        arguments.handler(arguments)
    except CiSupportError as exc:
        print(f"ci_support_error={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("ci_support_error=unexpected_failure", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
