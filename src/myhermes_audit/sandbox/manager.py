"""带所有权验证、路径隔离和默认清理的 AuditSandbox。"""

from __future__ import annotations

import errno
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from pydantic import TypeAdapter, ValidationError

from myhermes_audit.contracts.common import Identifier, validate_relative_path
from myhermes_audit.errors import SandboxError, UnsafePathError
from myhermes_audit.sandbox.layout import (
    SandboxLayout,
    SandboxManifest,
    SandboxManifestPaths,
)
from myhermes_audit.serialization import pretty_json


_MARKER_FILENAME = ".myhermes-audit-owned.json"
_MANIFEST_FILENAME = "manifest.json"
_MARKER_SIGNATURE = "myhermes-audit-sandbox-v1"


def _validated_identifier(value: str, field_name: str) -> str:
    try:
        return TypeAdapter(Identifier).validate_python(value)
    except ValidationError as exc:
        raise SandboxError(
            f"invalid {field_name}: {exc.errors(include_url=False)[0]['msg']}",
            operation="validate_identifier",
        ) from exc


class AuditSandbox:
    """为一个 Trial 创建并管理独立、可验证所有权的目录树。"""

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        trial_number: int,
        base_dir: Path | None = None,
        preserve: bool = False,
    ) -> None:
        self.run_id = _validated_identifier(run_id, "run_id")
        self.case_id = _validated_identifier(case_id, "case_id")
        if isinstance(trial_number, bool) or not isinstance(trial_number, int):
            raise SandboxError(
                "trial_number must be an integer",
                operation="validate_trial_number",
            )
        if trial_number < 1:
            raise SandboxError(
                "trial_number must start at 1",
                operation="validate_trial_number",
            )
        if not isinstance(preserve, bool):
            raise SandboxError(
                "preserve must be a boolean",
                operation="validate_preserve",
            )
        self.trial_number = trial_number
        self.base_dir = Path(base_dir).expanduser() if base_dir is not None else None
        self.preserve = preserve
        self._layout: SandboxLayout | None = None
        self._manifest: SandboxManifest | None = None
        self._owner_token: str | None = None
        self._controlled_root: Path | None = None
        self._created_parent_dirs: tuple[Path, ...] = ()
        self._shared_parent_dirs: tuple[Path, ...] = ()
        self._temporary_root = False
        self._created = False
        self._cleaned = False

    def _require_layout(self) -> SandboxLayout:
        if self._layout is None or not self._created or self._cleaned:
            raise SandboxError(
                "sandbox has not been created or was already cleaned",
                operation="access_layout",
            )
        return self._layout

    @property
    def layout(self) -> SandboxLayout:
        return self._require_layout()

    @property
    def root(self) -> Path:
        return self._require_layout().root

    @property
    def hermes_home(self) -> Path:
        return self._require_layout().hermes_home

    @property
    def workspace(self) -> Path:
        return self._require_layout().workspace

    @property
    def database_dir(self) -> Path:
        return self._require_layout().database_dir

    @property
    def sqlite_path(self) -> Path:
        return self._require_layout().sqlite_path

    @property
    def artifacts_dir(self) -> Path:
        return self._require_layout().artifacts_dir

    @property
    def fixtures_dir(self) -> Path:
        return self._require_layout().fixtures_dir

    @property
    def logs_dir(self) -> Path:
        return self._require_layout().logs_dir

    @property
    def manifest(self) -> SandboxManifest:
        self._require_layout()
        if self._manifest is None:
            raise SandboxError(
                "sandbox manifest is unavailable",
                operation="read_manifest",
            )
        return self._manifest

    def _prepare_controlled_root(self) -> Path:
        if self.base_dir is None:
            try:
                root = Path(tempfile.mkdtemp(prefix="myhermes-audit-"))
            except OSError as exc:
                raise SandboxError(
                    f"cannot create temporary root: {exc}",
                    operation="create",
                ) from exc
            self._temporary_root = True
            return root.resolve(strict=True)
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            return self.base_dir.resolve(strict=True)
        except OSError as exc:
            raise SandboxError(
                f"cannot prepare controlled root: {exc}",
                operation="create",
            ) from exc

    def create(self) -> SandboxLayout:
        """创建唯一目录、所有权标记和无凭据 manifest。"""

        if self._created:
            raise SandboxError(
                "sandbox instances cannot be created more than once",
                operation="create",
            )
        controlled_root = self._prepare_controlled_root()
        sandbox_id = uuid.uuid4().hex
        owner_token = uuid.uuid4().hex
        run_directory = controlled_root / self.run_id
        case_directory = run_directory / self.case_id
        candidate = case_directory / f"{self.trial_number}-{sandbox_id}"
        resolved_candidate = candidate.resolve(strict=False)
        if not resolved_candidate.is_relative_to(controlled_root):
            raise SandboxError(
                "sandbox path escaped the controlled root",
                operation="create",
            )
        created_parent_dirs: list[Path] = []
        try:
            for parent in (run_directory, case_directory):
                resolved_parent = parent.resolve(strict=False)
                if not resolved_parent.is_relative_to(controlled_root):
                    raise ValueError("sandbox parent escaped the controlled root")
                if parent.exists():
                    if parent.is_symlink() or not parent.is_dir():
                        raise ValueError("sandbox parent must be a real directory")
                else:
                    parent.mkdir(exist_ok=False)
                    created_parent_dirs.append(parent)
            resolved_candidate.mkdir(exist_ok=False)
            layout = SandboxLayout(
                root=resolved_candidate,
                hermes_home=resolved_candidate / "hermes_home",
                workspace=resolved_candidate / "workspace",
                database_dir=resolved_candidate / "database",
                sqlite_path=resolved_candidate / "database" / "hermes.db",
                artifacts_dir=resolved_candidate / "artifacts",
                fixtures_dir=resolved_candidate / "fixtures",
                logs_dir=resolved_candidate / "logs",
            )
            for directory in (
                layout.hermes_home,
                layout.workspace,
                layout.database_dir,
                layout.artifacts_dir,
                layout.fixtures_dir,
                layout.logs_dir,
            ):
                directory.mkdir(exist_ok=False)
            marker = {
                "signature": _MARKER_SIGNATURE,
                "owner_token": owner_token,
            }
            (layout.root / _MARKER_FILENAME).write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            manifest = SandboxManifest(
                sandbox_id=sandbox_id,
                run_id=self.run_id,
                case_id=self.case_id,
                trial_number=self.trial_number,
                created_at=datetime.now(timezone.utc),
                paths=SandboxManifestPaths(),
            )
            (layout.root / _MANIFEST_FILENAME).write_text(
                pretty_json(manifest) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError) as exc:
            if resolved_candidate.exists() and resolved_candidate.is_dir():
                shutil.rmtree(resolved_candidate)
            for parent in reversed(created_parent_dirs):
                if parent.exists():
                    try:
                        parent.rmdir()
                    except OSError as cleanup_exc:
                        if cleanup_exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                            continue
                        raise SandboxError(
                            f"cannot roll back sandbox parent: {cleanup_exc}",
                            operation="create",
                        ) from cleanup_exc
            if self._temporary_root and controlled_root.exists():
                try:
                    controlled_root.rmdir()
                except OSError as cleanup_exc:
                    raise SandboxError(
                        f"cannot roll back temporary root: {cleanup_exc}",
                        operation="create",
                    ) from cleanup_exc
            raise SandboxError(
                f"cannot create sandbox layout: {exc}",
                operation="create",
            ) from exc
        self._controlled_root = controlled_root
        self._layout = layout
        self._manifest = manifest
        self._owner_token = owner_token
        self._created_parent_dirs = tuple(created_parent_dirs)
        self._shared_parent_dirs = (case_directory, run_directory)
        self._created = True
        return layout

    def __enter__(self) -> "AuditSandbox":
        self.create()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self.preserve:
            self.cleanup()

    def environment_overrides(self) -> dict[str, str]:
        """返回隔离运行所需变量，不修改 ``os.environ``。"""

        layout = self._require_layout()
        return {
            "HERMES_HOME": str(layout.hermes_home),
            "DB_PATH": str(layout.sqlite_path),
            "HERMES_WORKSPACE": str(layout.workspace),
            "MYHERMES_AUDIT_ARTIFACTS_DIR": str(layout.artifacts_dir),
        }

    def _resolve_fixture_target(self, relative_path: str) -> Path:
        layout = self._require_layout()
        try:
            normalized = validate_relative_path(
                relative_path,
                allowed_roots=frozenset({"workspace", "hermes_home"}),
            )
        except ValueError as exc:
            raise UnsafePathError(relative_path, reason=str(exc)) from exc
        parts = normalized.split("/")
        if len(parts) < 2:
            raise UnsafePathError(
                relative_path,
                reason="target must name an entry below an allowed root",
            )
        allowed_root = (
            layout.workspace if parts[0] == "workspace" else layout.hermes_home
        )
        declared_candidate = layout.root / Path(*parts)
        if declared_candidate.is_symlink():
            raise UnsafePathError(
                relative_path,
                reason="symbolic-link targets are not allowed",
            )
        candidate = declared_candidate.resolve(strict=False)
        allowed_resolved = allowed_root.resolve(strict=True)
        if not candidate.is_relative_to(allowed_resolved):
            raise UnsafePathError(
                relative_path,
                reason="resolved target escapes its allowed root",
            )
        return candidate

    def _ensure_writable_target(self, target: Path, *, overwrite: bool) -> None:
        if target.exists() and target.is_symlink():
            raise UnsafePathError(target, reason="symbolic-link targets are not allowed")
        if target.exists() and not overwrite:
            raise SandboxError(
                f"fixture target already exists: {target}",
                operation="write_fixture",
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxError(
                f"cannot create fixture parent directory: {exc}",
                operation="write_fixture",
            ) from exc
        resolved_parent = target.parent.resolve(strict=True)
        if not (
            resolved_parent.is_relative_to(self.workspace.resolve(strict=True))
            or resolved_parent.is_relative_to(self.hermes_home.resolve(strict=True))
        ):
            raise UnsafePathError(
                target,
                reason="fixture parent resolved outside allowed roots",
            )

    def copy_fixture_file(
        self,
        source: Path,
        target_relative_path: str,
        *,
        overwrite: bool = False,
    ) -> Path:
        """把一个普通源文件复制到 workspace 或 hermes_home 内。"""

        source_path = Path(source).expanduser()
        if source_path.is_symlink():
            raise UnsafePathError(source_path, reason="fixture source cannot be a symlink")
        try:
            resolved_source = source_path.resolve(strict=True)
        except OSError as exc:
            raise SandboxError(
                f"cannot resolve fixture source: {exc}",
                operation="copy_fixture",
            ) from exc
        if not resolved_source.is_file():
            raise SandboxError(
                "fixture source is not a regular file",
                operation="copy_fixture",
            )
        target = self._resolve_fixture_target(target_relative_path)
        self._ensure_writable_target(target, overwrite=overwrite)
        created_target = False
        try:
            mode = "wb" if overwrite else "xb"
            with target.open(mode) as target_stream:
                created_target = not overwrite
                with resolved_source.open("rb") as source_stream:
                    shutil.copyfileobj(source_stream, target_stream)
        except OSError as exc:
            if created_target and target.exists() and not target.is_symlink():
                target.unlink(missing_ok=True)
            raise SandboxError(
                f"cannot copy fixture file: {exc}",
                operation="copy_fixture",
            ) from exc
        return target

    def write_fixture_content(
        self,
        content: str,
        target_relative_path: str,
        *,
        overwrite: bool = False,
    ) -> Path:
        """把 UTF-8 文本安全写入 workspace 或 hermes_home 内。"""

        if not isinstance(content, str):
            raise SandboxError(
                "fixture content must be a string",
                operation="write_fixture",
            )
        target = self._resolve_fixture_target(target_relative_path)
        self._ensure_writable_target(target, overwrite=overwrite)
        created_target = False
        try:
            mode = "w" if overwrite else "x"
            with target.open(mode, encoding="utf-8", newline="\n") as stream:
                created_target = not overwrite
                stream.write(content)
        except OSError as exc:
            if created_target and target.exists() and not target.is_symlink():
                target.unlink(missing_ok=True)
            raise SandboxError(
                f"cannot write fixture content: {exc}",
                operation="write_fixture",
            ) from exc
        return target

    def _validate_cleanup_marker(self) -> None:
        layout = self._require_layout()
        controlled_root = self._controlled_root
        if controlled_root is None or self._owner_token is None:
            raise SandboxError(
                "sandbox ownership state is incomplete",
                operation="cleanup",
            )
        if layout.root.is_symlink():
            raise SandboxError(
                "refusing to clean a symbolic-link sandbox root",
                operation="cleanup",
            )
        resolved_root = layout.root.resolve(strict=True)
        resolved_controlled = controlled_root.resolve(strict=True)
        if resolved_root == resolved_controlled or not resolved_root.is_relative_to(
            resolved_controlled
        ):
            raise SandboxError(
                "sandbox root is outside its controlled root",
                operation="cleanup",
            )
        marker_path = resolved_root / _MARKER_FILENAME
        if not marker_path.is_file() or marker_path.is_symlink():
            raise SandboxError(
                "sandbox ownership marker is missing or unsafe",
                operation="cleanup",
            )
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SandboxError(
                f"cannot verify sandbox ownership marker: {exc}",
                operation="cleanup",
            ) from exc
        if not isinstance(marker, dict) or marker != {
            "signature": _MARKER_SIGNATURE,
            "owner_token": self._owner_token,
        }:
            raise SandboxError(
                "sandbox ownership marker does not match this instance",
                operation="cleanup",
            )

    def cleanup(self) -> None:
        """验证所有权后只删除本实例创建的 Trial 根目录。"""

        if self.preserve or not self._created or self._cleaned:
            return
        self._validate_cleanup_marker()
        layout = self._require_layout()
        controlled_root = self._controlled_root
        try:
            shutil.rmtree(layout.root)
        except OSError as exc:
            raise SandboxError(
                f"cannot remove sandbox root: {exc}",
                operation="cleanup",
            ) from exc
        self._cleaned = True
        for directory in self._shared_parent_dirs:
            if directory.exists():
                try:
                    directory.rmdir()
                except OSError as exc:
                    if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                        continue
                    raise SandboxError(
                        f"cannot remove shared sandbox parent: {exc}",
                        operation="cleanup",
                    ) from exc
        if self._temporary_root and controlled_root is not None:
            try:
                controlled_root.rmdir()
            except OSError as exc:
                raise SandboxError(
                    f"cannot remove temporary controlled root: {exc}",
                    operation="cleanup",
                ) from exc
