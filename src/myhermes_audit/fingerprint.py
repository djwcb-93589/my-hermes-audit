"""Suite、Audit 与本地 Git 仓库的稳定只读指纹。"""

from __future__ import annotations

import platform as platform_module
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from myhermes_audit import __version__
from myhermes_audit.contracts import AuditSuite
from myhermes_audit.contracts.fingerprint import AuditFingerprint, SubjectFingerprint
from myhermes_audit.errors import FingerprintError
from myhermes_audit.serialization import canonical_sha256


def suite_sha256(suite: AuditSuite) -> str:
    """按规范化合同而非 YAML 键顺序计算 Suite SHA-256。"""

    return canonical_sha256(suite)


def _run_git(repository: Path, *arguments: str) -> str:
    command = ["git", "--no-optional-locks", "-C", str(repository), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise FingerprintError(
            "Git executable is unavailable",
            operation="git",
            repository=repository,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(
            "Git read operation timed out",
            operation="git",
            repository=repository,
        ) from exc
    except OSError as exc:
        raise FingerprintError(
            f"Git read operation failed: {exc}",
            operation="git",
            repository=repository,
        ) from exc
    if completed.returncode != 0:
        reason = completed.stderr.strip() or "Git command failed"
        raise FingerprintError(
            reason,
            operation="git",
            repository=repository,
            returncode=completed.returncode,
        )
    return completed.stdout.strip()


def _read_python_requirement(repository: Path) -> str | None:
    pyproject = repository / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FingerprintError(
            f"cannot read Python requirement: {exc}",
            operation="read_pyproject",
            repository=repository,
        ) from exc
    project = document.get("project")
    if not isinstance(project, dict):
        return None
    requirement = project.get("requires-python")
    return requirement if isinstance(requirement, str) and requirement.strip() else None


def read_subject_fingerprint(
    repository: Path,
    *,
    python_requirement: str | None = None,
) -> SubjectFingerprint:
    """只读获取仓库 HEAD、tree 与 dirty 状态，不执行 checkout 或写操作。"""

    requested = Path(repository).expanduser().resolve(strict=False)
    if not requested.is_dir():
        raise FingerprintError(
            "repository path is not a directory",
            operation="validate_repository",
            repository=requested,
        )
    root_text = _run_git(requested, "rev-parse", "--show-toplevel")
    root = Path(root_text).resolve(strict=True)
    commit = _run_git(root, "rev-parse", "HEAD")
    tree_hash = _run_git(root, "rev-parse", "HEAD^{tree}")
    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    requirement = (
        python_requirement
        if python_requirement is not None
        else _read_python_requirement(root)
    )
    return SubjectFingerprint(
        repository=str(root),
        git_commit=commit,
        dirty=bool(status),
        tree_hash=tree_hash,
        python_requirement=requirement,
    )


def read_git_fingerprint(repository: Path) -> SubjectFingerprint:
    """兼容名称：读取本地 Git SubjectFingerprint。"""

    return read_subject_fingerprint(repository)


def build_audit_fingerprint(
    suite: AuditSuite,
    *,
    audit_version: str = __version__,
    created_at: datetime | None = None,
) -> AuditFingerprint:
    """构造当前 Audit 代码、Suite 与 Python 平台指纹。"""

    timestamp = created_at or datetime.now(timezone.utc)
    return AuditFingerprint(
        audit_version=audit_version,
        suite_sha256=suite_sha256(suite),
        python_version=platform_module.python_version(),
        platform=platform_module.platform(),
        created_at=timestamp,
    )
