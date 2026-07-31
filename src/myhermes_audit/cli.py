"""P0 仅提供 Suite 静态校验与 JSON Schema 输出。"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections import Counter
from pathlib import Path
from typing import Sequence

from myhermes_audit.contracts import AuditSuite
from myhermes_audit.datasets import load_suite
from myhermes_audit.errors import AuditError
from myhermes_audit.fingerprint import suite_sha256
from myhermes_audit.serialization import pretty_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myhermes-audit",
        description="Static MyHermes Audit contract tooling.",
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


def main(argv: Sequence[str] | None = None) -> int:
    """执行 CLI 并返回进程退出码。"""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate":
            return _validate_command(arguments.suite)
        if arguments.command == "schema":
            return _schema_command(arguments.output)
    except AuditError as exc:
        if arguments.debug:
            traceback.print_exc()
        else:
            print(f"Validation failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if arguments.debug:
            traceback.print_exc()
        else:
            print(
                f"Unexpected error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return 3
    parser.error(f"unsupported command: {arguments.command}")
    return 2


def entrypoint() -> None:
    """Console Script 入口。"""

    raise SystemExit(main())
