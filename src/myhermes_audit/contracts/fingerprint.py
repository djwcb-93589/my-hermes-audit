"""被测项目与 Audit 运行的指纹合同。"""

from __future__ import annotations

from pydantic import Field, StrictBool

from myhermes_audit.contracts.common import (
    ContractModel,
    GitObjectId,
    NonEmptyText,
    Sha256Digest,
    UtcDatetime,
)


class SubjectFingerprint(ContractModel):
    """只读观测到的 MyHermes 仓库身份。"""

    repository: NonEmptyText = Field(description="Resolved repository path.")
    git_commit: GitObjectId = Field(description="Exact Git commit at HEAD.")
    dirty: StrictBool = Field(description="Whether tracked or untracked changes exist.")
    tree_hash: GitObjectId | None = Field(
        default=None,
        description="Committed Git tree hash when available.",
    )
    python_requirement: NonEmptyText | None = Field(
        default=None,
        description="Subject project's declared Python requirement.",
    )


class AuditFingerprint(ContractModel):
    """Audit 代码、Suite 与平台环境的可复现身份。"""

    audit_version: NonEmptyText
    audit_commit: GitObjectId | None = None
    suite_sha256: Sha256Digest
    python_version: NonEmptyText
    platform: NonEmptyText
    created_at: UtcDatetime
