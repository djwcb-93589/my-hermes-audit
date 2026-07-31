"""Static contracts plus the isolated P1 MyHermes Audit command."""

from __future__ import annotations

import argparse
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
    from myhermes_audit.reports import (
        render_console_summary,
        write_json_report,
    )
    from myhermes_audit.runners.myhermes import MyHermesTrialRunner
    from myhermes_audit.runners.orchestrator import AuditOrchestrator

    suite_path = arguments.suite.expanduser().resolve(strict=False)
    suite = load_suite(suite_path)
    selected = _select_cases(suite, arguments.case_ids)
    output = _report_path(arguments.output, suite.suite_id)
    _validate_report_destination(
        output,
        suite_path=suite_path,
        suite=suite,
        subject_repo=arguments.subject_repo,
        subject_config=arguments.subject_config,
    )
    runner = MyHermesTrialRunner(
        subject_repo=arguments.subject_repo,
        subject_config=arguments.subject_config,
        debug=arguments.debug,
    )
    orchestrator = AuditOrchestrator(
        runner=runner,
        subject_repo=arguments.subject_repo,
    )
    outcome = orchestrator.run(
        suite,
        cases=selected,
        preserve_on_failure=arguments.preserve_on_failure,
    )
    write_json_report(output, outcome.result)
    sys.stdout.write(render_console_summary(outcome.result))
    print(f"Report:             {output}")
    if outcome.preserved_sandboxes:
        print("Preserved Sandboxes:")
        for path in outcome.preserved_sandboxes:
            print(f"- {path}")
    return 0 if outcome.result.summary.passed_count == len(outcome.result.trials) else 1


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
    except AuditError as exc:
        if arguments.debug:
            traceback.print_exc()
        else:
            print(f"Audit command failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if arguments.debug:
            traceback.print_exc()
        else:
            print(
                f"Unexpected error: {type(exc).__name__}",
                file=sys.stderr,
            )
        return 3
    parser.error(f"unsupported command: {arguments.command}")
    return 2


def entrypoint() -> None:
    """Console Script 入口。"""

    raise SystemExit(main())
