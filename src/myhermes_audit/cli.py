"""Static contracts plus the isolated P1 MyHermes Audit command."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Sequence

from myhermes_audit.contracts import AuditCase, AuditSuite
from myhermes_audit.datasets import load_suite
from myhermes_audit.errors import AuditError, ReportError, UnsupportedCaseError
from myhermes_audit.fingerprint import suite_sha256
from myhermes_audit.serialization import pretty_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myhermes-audit",
        description="MyHermes Audit contracts and isolated local runner.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show a traceback for unexpected or validation errors",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="load and statically validate an Audit Suite YAML file",
    )
    validate_parser.add_argument("suite", type=Path, help="path to the Suite YAML")

    schema_parser = subparsers.add_parser(
        "schema",
        help="print the AuditSuite JSON Schema",
    )
    schema_parser.add_argument(
        "--output",
        type=Path,
        help="write the schema to a UTF-8 file instead of stdout",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="run a Suite through isolated MyHermes worker processes",
    )
    run_parser.add_argument("suite", type=Path, help="path to the Suite YAML")
    run_parser.add_argument(
        "--subject-repo",
        type=Path,
        required=True,
        help="path to the read-only MyHermes repository",
    )
    run_parser.add_argument(
        "--subject-config",
        type=Path,
        required=True,
        help="base MyHermes config used to build isolated Trial configs",
    )
    run_parser.add_argument("--output", type=Path, help="JSON report path")
    run_parser.add_argument(
        "--case",
        dest="case_ids",
        action="append",
        default=[],
        help="run only this Case ID; may be repeated",
    )
    run_parser.add_argument(
        "--preserve-on-failure",
        action="store_true",
        help="preserve failed Trial Sandboxes and print their local paths",
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include internal worker diagnostics in the controlled stderr artifact",
    )
    run_parser.add_argument(
        "--judge",
        action="store_true",
        help="enable the environment-configured LLM Judge",
    )
    run_parser.add_argument(
        "--langfuse",
        action="store_true",
        help="ensure the Dataset and publish this run as a Langfuse Experiment",
    )
    run_parser.add_argument(
        "--dataset-name",
        help="Langfuse Dataset name required with --langfuse",
    )
    run_parser.add_argument(
        "--experiment-name",
        help="Langfuse Experiment/Dataset Run name required with --langfuse",
    )
    run_parser.add_argument(
        "--langfuse-no-content",
        action="store_true",
        help="publish hashes, lengths, statuses, and metrics without input/output content",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="plan or perform non-destructive Langfuse Dataset synchronization",
    )
    sync_parser.add_argument("suite", type=Path, help="path to the Suite YAML")
    sync_parser.add_argument(
        "--dataset-name",
        required=True,
        help="Langfuse Dataset name",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the local plan without importing, connecting, or writing Langfuse",
    )
    sync_parser.add_argument(
        "--langfuse-no-content",
        action="store_true",
        help="omit input and expected-output content from the remote projection",
    )
    sync_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show internal tracebacks while continuing to redact secret values",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check the local Audit installation and public MyHermes capabilities",
    )
    doctor_parser.add_argument(
        "--subject-repo",
        type=Path,
        required=True,
        help="path to the read-only MyHermes repository",
    )
    doctor_parser.add_argument(
        "--subject-config",
        type=Path,
        required=True,
        help="base MyHermes config checked without printing its values",
    )
    doctor_parser.add_argument(
        "--check-langfuse",
        action="store_true",
        help="check Langfuse dependency and environment variable presence only",
    )
    doctor_parser.add_argument(
        "--check-judge",
        action="store_true",
        help="check Judge dependency and environment variable presence only",
    )
    doctor_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show internal tracebacks while continuing to redact secret values",
    )
    return parser


def _validate_command(path: Path) -> int:
    suite = load_suite(path)
    modes = Counter(case.mode.value for case in suite.cases)
    evaluator_count = sum(len(case.evaluators) for case in suite.cases)
    print(f"Suite ID: {suite.suite_id}")
    print(f"Schema version: {suite.schema_version}")
    print(f"Case count: {len(suite.cases)}")
    distribution = ", ".join(
        f"{mode}={count}" for mode, count in sorted(modes.items())
    )
    print(f"Case modes: {distribution or '<none>'}")
    print(f"Evaluator count: {evaluator_count}")
    print(f"Suite SHA-256: {suite_sha256(suite)}")
    print("Validation succeeded")
    return 0


def _schema_command(output: Path | None) -> int:
    schema_text = pretty_json(AuditSuite.model_json_schema()) + "\n"
    if output is None:
        sys.stdout.write(schema_text)
        return 0
    output_path = output.expanduser().resolve(strict=False)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(schema_text, encoding="utf-8")
    except OSError as exc:
        raise AuditError(
            f"cannot write schema file {output_path}: {exc}",
            code="schema_write_error",
            details={"output": str(output_path)},
        ) from exc
    print(f"Schema written: {output_path}")
    return 0


def _run_command(arguments: argparse.Namespace) -> int:
    from myhermes_audit.integrations.judge import OpenAICompatibleJudgeAdapter
    from myhermes_audit.judges import JudgeService
    from myhermes_audit.reports import (
        render_console_summary,
        write_json_report,
    )
    from myhermes_audit.runners.myhermes import MyHermesTrialRunner
    from myhermes_audit.runners.orchestrator import AuditOrchestrator

    suite_path = arguments.suite.expanduser().resolve(strict=False)
    suite = load_suite(suite_path)
    selected = _select_cases(suite, arguments.case_ids)
    _validate_run_integration_options(arguments)
    output = _report_path(arguments.output, suite.suite_id)
    _validate_report_destination(
        output,
        suite_path=suite_path,
        suite=suite,
        subject_repo=arguments.subject_repo,
        subject_config=arguments.subject_config,
    )
    judge_adapter = None
    try:
        if arguments.judge:
            judge_adapter = OpenAICompatibleJudgeAdapter.from_environment()
        runner = MyHermesTrialRunner(
            subject_repo=arguments.subject_repo,
            subject_config=arguments.subject_config,
            debug=arguments.debug,
        )
        orchestrator = AuditOrchestrator(
            runner=runner,
            subject_repo=arguments.subject_repo,
            judge_service=JudgeService(judge_adapter),
        )
        outcome = orchestrator.run(
            suite,
            cases=selected,
            preserve_on_failure=arguments.preserve_on_failure,
        )
        # Persist the complete local fact before importing or calling Langfuse.
        write_json_report(output, outcome.result)
        if arguments.langfuse:
            from myhermes_audit.integrations.langfuse.publisher import (
                publish_audit_result,
            )

            published_result = publish_audit_result(
                suite=suite,
                cases=selected,
                result=outcome.result,
                report_path=output,
                dataset_name=arguments.dataset_name,
                experiment_name=arguments.experiment_name,
                no_content=arguments.langfuse_no_content,
            )
            outcome = type(outcome)(
                result=published_result,
                preserved_sandboxes=outcome.preserved_sandboxes,
            )
            write_json_report(output, outcome.result)
        sys.stdout.write(render_console_summary(outcome.result))
        print(f"Report:             {output}")
        if outcome.preserved_sandboxes:
            print("Preserved Sandboxes:")
            for path in outcome.preserved_sandboxes:
                print(f"- {path}")
        has_trial_failure = (
            outcome.result.summary.passed_count != len(outcome.result.trials)
        )
        has_integration_failure = bool(outcome.result.integration_errors)
        return 1 if has_trial_failure or has_integration_failure else 0
    finally:
        if judge_adapter is not None:
            judge_adapter.shutdown()


def _sync_command(arguments: argparse.Namespace) -> int:
    from myhermes_audit.integrations.langfuse import (
        LangfuseV4Adapter,
        build_dataset_sync_plan,
        dry_run_sync_result,
    )

    suite = load_suite(arguments.suite.expanduser().resolve(strict=False))
    plan = build_dataset_sync_plan(
        suite,
        dataset_name=arguments.dataset_name,
        dry_run=arguments.dry_run,
        no_content=arguments.langfuse_no_content,
    )
    if arguments.dry_run:
        result = dry_run_sync_result(plan)
    else:
        adapter = LangfuseV4Adapter.from_environment()
        try:
            adapter.check_connection()
            result = adapter.sync_dataset(plan)
            adapter.flush()
        finally:
            adapter.shutdown()
    print(f"Dataset:           {result.dataset.dataset_name}")
    print(f"Suite SHA-256:     {result.dataset.suite_sha256}")
    print(f"Planned items:     {result.planned_upsert_count}")
    print(f"Added:             {_count_or_unknown(result.added_count)}")
    print(f"Updated/versioned: {_count_or_unknown(result.updated_count)}")
    print(f"Unchanged:         {_count_or_unknown(result.unchanged_count)}")
    print(f"Remote write:      {'no' if result.dry_run else 'yes'}")
    for warning in result.warnings:
        print(f"Warning:           {warning}")
    return 0


def _doctor_command(arguments: argparse.Namespace) -> int:
    from myhermes_audit import __version__
    from myhermes_audit.fingerprint import read_subject_fingerprint
    from myhermes_audit.integrations.myhermes.capability_runner import (
        run_subject_capability_probe,
    )
    from myhermes_audit.integrations.myhermes.config_builder import (
        MyHermesConfigBuilder,
    )

    subject_repo = arguments.subject_repo.expanduser().resolve(strict=False)
    subject_config = arguments.subject_config.expanduser().resolve(strict=False)
    fingerprint = read_subject_fingerprint(subject_repo)
    MyHermesConfigBuilder(subject_config).prepare({})
    report = run_subject_capability_probe(
        subject_repo=subject_repo,
        subject_config=subject_config,
        subject_commit=fingerprint.git_commit,
    )
    langfuse_available = importlib.util.find_spec("langfuse") is not None
    judge_available = importlib.util.find_spec("openai") is not None

    langfuse_capability = None
    langfuse_check_error: AuditError | None = None
    if arguments.check_langfuse:
        from myhermes_audit.integrations.langfuse import (
            LangfuseV4Adapter,
            probe_langfuse_capabilities,
        )

        langfuse_capability = probe_langfuse_capabilities()
        try:
            langfuse_adapter = LangfuseV4Adapter.from_environment()
            langfuse_adapter.shutdown()
        except AuditError as exc:
            langfuse_check_error = exc
    if arguments.check_judge:
        from myhermes_audit.integrations.judge import (
            OpenAICompatibleJudgeAdapter,
        )

        judge_adapter = OpenAICompatibleJudgeAdapter.from_environment()
        judge_adapter.shutdown()

    print(f"Audit version: {__version__}")
    print(f"Subject commit: {fingerprint.git_commit}")
    print(f"Subject dirty: {'yes' if fingerprint.dirty else 'no'}")
    print("Base config: valid")
    print(
        "Subject capabilities: "
        f"{sum(item.available for item in report.capabilities)}"
        f"/{len(report.capabilities)}"
    )
    print(
        "Memory kinds: "
        + (
            ", ".join(item.value for item in report.supported_memory_kinds)
            or "none"
        )
    )
    print(
        "Memory strategies: "
        + (
            ", ".join(
                item.value for item in report.supported_retrieval_strategies
            )
            or "none"
        )
    )
    print(f"Memory provider: {report.memory_provider or 'unavailable'}")
    for label, capability_name in (
        ("Short-term context", "short_term_context"),
        ("Long-term memory", "long_term_memory"),
        ("Compression toggle", "compression_toggle"),
        ("Compression observation", "compression_observation"),
    ):
        capability = report.capability(capability_name)
        print(
            f"{label}: "
            + (
                "supported"
                if capability is not None and capability.available
                else "unsupported"
            )
        )
    print(f"Public API fingerprint: {report.public_api_fingerprint}")
    print(f"Optional dependency langfuse: {_presence(langfuse_available)}")
    print(f"Optional dependency openai: {_presence(judge_available)}")
    if arguments.check_langfuse:
        if langfuse_capability is None:
            raise RuntimeError("Langfuse capability report was lost")
        print(f"Langfuse SDK version: {langfuse_capability.version or 'not installed'}")
        print(
            "Langfuse minimum: "
            f"{langfuse_capability.required_minimum_version}"
        )
        print(
            "Langfuse compatible: "
            f"{'yes' if langfuse_capability.compatible else 'no'}"
        )
        print(
            "Langfuse Experiment strategy: "
            f"{langfuse_capability.experiment_strategy.value}"
        )
        print(
            "Langfuse Score idempotency: "
            f"{langfuse_capability.score_idempotency_strategy}"
        )
        print(
            "Langfuse Score submission supported: "
            f"{'yes' if langfuse_capability.score_submission_supported else 'no'}"
        )
        print(
            "Langfuse Score confirmation supported: "
            f"{'yes' if langfuse_capability.score_confirmation_supported else 'no'}"
        )
        for name in (
            "dataset_read",
            "dataset_create",
            "dataset_item_upsert",
            "experiment_runner",
            "experiment_item_association",
            "trace_observation",
            "score_submission_supported",
        ):
            print(
                f"Langfuse capability {name}: "
                f"{'available' if langfuse_capability.capabilities.get(name) else 'missing'}"
            )
        missing = ", ".join(langfuse_capability.missing_capabilities) or "none"
        print(f"Langfuse missing capabilities: {missing}")
        print("Langfuse connection: not attempted")
        if langfuse_check_error is None:
            print("Langfuse config: present; client initialized")
    if arguments.check_judge:
        print("Judge config: present; model request not attempted")
    print("Doctor checks completed")
    if langfuse_check_error is not None:
        raise langfuse_check_error
    return 0


def _presence(available: bool) -> str:
    return "installed" if available else "not installed"


def _count_or_unknown(value: int | None) -> str:
    return "unknown (no remote connection)" if value is None else str(value)


def _validate_run_integration_options(arguments: argparse.Namespace) -> None:
    if arguments.langfuse:
        missing = [
            name
            for name, value in (
                ("--dataset-name", arguments.dataset_name),
                ("--experiment-name", arguments.experiment_name),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise AuditError(
                "--langfuse requires " + " and ".join(missing),
                code="langfuse_config_error",
                details={"missing_options": missing},
            )
        dataset_name = arguments.dataset_name.strip()
        if len(dataset_name) > 200 or any(
            ord(character) < 32 for character in dataset_name
        ):
            raise AuditError(
                "--dataset-name must be a safe name up to 200 characters",
                code="langfuse_config_error",
                details={"field": "--dataset-name"},
            )
        experiment_name = arguments.experiment_name.strip()
        if len(experiment_name) > 200 or any(
            ord(character) < 32 for character in experiment_name
        ):
            raise AuditError(
                "--experiment-name must be a safe name up to 200 characters",
                code="langfuse_config_error",
                details={"field": "--experiment-name"},
            )
        arguments.dataset_name = dataset_name
        arguments.experiment_name = experiment_name
        return
    unused = []
    if arguments.dataset_name is not None:
        unused.append("--dataset-name")
    if arguments.experiment_name is not None:
        unused.append("--experiment-name")
    if arguments.langfuse_no_content:
        unused.append("--langfuse-no-content")
    if unused:
        raise AuditError(
            "Langfuse run options require --langfuse: " + ", ".join(unused),
            code="langfuse_config_error",
            details={"unused_options": unused},
        )


def _select_cases(suite: AuditSuite, requested: list[str]) -> list[AuditCase]:
    if len(requested) != len(set(requested)):
        raise UnsupportedCaseError("--case values must not repeat")
    if not requested:
        return list(suite.cases)
    known = {case.case_id: case for case in suite.cases}
    unknown = [case_id for case_id in requested if case_id not in known]
    if unknown:
        raise UnsupportedCaseError(
            "unknown Case ID",
            case_ids=unknown,
        )
    requested_set = set(requested)
    return [case for case in suite.cases if case.case_id in requested_set]


def _report_path(requested: Path | None, suite_id: str) -> Path:
    if requested is not None:
        candidate = requested.expanduser()
        if candidate.is_symlink():
            raise ReportError("report output cannot be a symbolic link")
        return candidate.resolve(strict=False)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename_id = re.sub(r"[^A-Za-z0-9._-]", "_", suite_id)
    return (Path.cwd() / "reports" / f"{filename_id}-{timestamp}.json").resolve(
        strict=False
    )


def _validate_report_destination(
    output: Path,
    *,
    suite_path: Path,
    suite: AuditSuite,
    subject_repo: Path,
    subject_config: Path,
) -> None:
    if output.exists() and not output.is_file():
        raise ReportError("report output must be a regular file path")
    existing_parent = output.parent
    while (
        not existing_parent.exists()
        and existing_parent != existing_parent.parent
    ):
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise ReportError("report output parent is not a directory")
    protected = {
        suite_path.resolve(strict=False),
        Path(subject_config).expanduser().resolve(strict=False),
    }
    protected.update(
        fixture.resolved_source.resolve(strict=False)
        for case in suite.cases
        for fixture in case.fixture.files
        if fixture.resolved_source is not None
    )
    if output in protected:
        raise ReportError("report output cannot overwrite Suite, config, or Fixture input")
    subject_root = Path(subject_repo).expanduser().resolve(strict=False)
    if output == subject_root or output.is_relative_to(subject_root):
        raise ReportError("report output cannot modify the subject repository")


def main(argv: Sequence[str] | None = None) -> int:
    """执行 CLI 并返回进程退出码。"""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate":
            return _validate_command(arguments.suite)
        if arguments.command == "schema":
            return _schema_command(arguments.output)
        if arguments.command == "run":
            return _run_command(arguments)
        if arguments.command == "sync":
            return _sync_command(arguments)
        if arguments.command == "doctor":
            return _doctor_command(arguments)
    except AuditError as exc:
        if arguments.debug:
            _print_safe_traceback()
        else:
            print(f"Audit command failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if arguments.debug:
            _print_safe_traceback()
        else:
            print(
                f"Unexpected error: {type(exc).__name__}",
                file=sys.stderr,
            )
        return 3
    parser.error(f"unsupported command: {arguments.command}")
    return 2


def _print_safe_traceback() -> None:
    from myhermes_audit.security import redact_text, sensitive_environment_values

    safe = redact_text(
        traceback.format_exc(),
        sensitive_environment_values(os.environ),
    )
    sys.stderr.write(safe)


def entrypoint() -> None:
    """Console Script 入口。"""

    raise SystemExit(main())
