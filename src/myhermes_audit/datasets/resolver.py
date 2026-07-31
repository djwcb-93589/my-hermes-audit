"""Suite 相对路径的只读解析与逃逸检查。"""

from __future__ import annotations

from pathlib import Path

from myhermes_audit.contracts import AuditSuite
from myhermes_audit.errors import DatasetLoadError


def _resolve_below(base: Path, declared_path: str) -> Path:
    base_resolved = base.resolve(strict=True)
    candidate = (base_resolved / declared_path).resolve(strict=False)
    if not candidate.is_relative_to(base_resolved):
        raise ValueError("resolved source escapes the Suite directory")
    return candidate


def resolve_suite_sources(suite: AuditSuite, yaml_path: Path) -> AuditSuite:
    """解析 Fixture source，但不检查存在性、不读取也不复制文件。"""

    yaml_file = Path(yaml_path).resolve(strict=True)
    suite_directory = yaml_file.parent
    for case_index, case in enumerate(suite.cases):
        for file_index, fixture_file in enumerate(case.fixture.files):
            if fixture_file.source is None:
                continue
            field_path = (
                f"cases[{case_index}].fixture.files[{file_index}].source"
            )
            try:
                resolved = _resolve_below(suite_directory, fixture_file.source)
            except (OSError, ValueError) as exc:
                raise DatasetLoadError(
                    yaml_file,
                    case_id=case.case_id,
                    field_path=field_path,
                    reason=str(exc),
                ) from exc
            fixture_file.set_resolved_source(resolved)

        database = case.fixture.database
        if database is not None and database.source is not None:
            field_path = f"cases[{case_index}].fixture.database.source"
            try:
                resolved = _resolve_below(suite_directory, database.source)
            except (OSError, ValueError) as exc:
                raise DatasetLoadError(
                    yaml_file,
                    case_id=case.case_id,
                    field_path=field_path,
                    reason=str(exc),
                ) from exc
            database.set_resolved_source(resolved)
    return suite
