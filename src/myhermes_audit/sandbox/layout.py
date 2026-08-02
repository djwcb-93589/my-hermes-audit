"""AuditSandbox 的目录布局与无凭据 manifest 合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    PositiveInt,
    SafeRelativePath,
    UtcDatetime,
)


class SandboxLayout(ContractModel):
    """一个 Trial 的绝对目录位置；不负责创建或清理。"""

    root: Path
    hermes_home: Path
    workspace: Path
    database_dir: Path
    sqlite_path: Path
    artifacts_dir: Path
    fixtures_dir: Path
    logs_dir: Path


class SandboxManifestPaths(ContractModel):
    root: Literal["."] = "."
    hermes_home: SafeRelativePath = "hermes_home"
    workspace: SafeRelativePath = "workspace"
    database_dir: SafeRelativePath = "database"
    sqlite_path: SafeRelativePath = "database/hermes.db"
    artifacts_dir: SafeRelativePath = "artifacts"
    fixtures_dir: SafeRelativePath = "fixtures"
    logs_dir: SafeRelativePath = "logs"


class SandboxManifest(ContractModel):
    sandbox_id: Identifier
    run_id: Identifier
    case_id: Identifier
    variant_id: Identifier | None = None
    trial_number: PositiveInt
    created_at: UtcDatetime
    paths: SandboxManifestPaths = Field(default_factory=SandboxManifestPaths)
