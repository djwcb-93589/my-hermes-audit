"""Atomic, credential-free Langfuse publication manifest persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json, sha256_file
from myhermes_audit.contracts import (
    LangfusePublicationManifest,
    PublicationManifestRef,
    PublicationManifestStatus,
)
from myhermes_audit.errors import AuditError, PublicationManifestError


def publication_manifest_path(report_path: Path, audit_run_id: str) -> Path:
    """Place a per-run manifest beside the requested local JSON report."""

    report = Path(report_path).expanduser().resolve(strict=False)
    return report.with_name(
        f"{report.stem}.{audit_run_id}.langfuse-manifest.json"
    )


class PublicationManifestStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)

    def create(
        self,
        *,
        audit_run_id: str,
        experiment_name: str,
        dataset_name: str,
    ) -> LangfusePublicationManifest:
        if self.path.exists() or self.path.is_symlink():
            raise PublicationManifestError(
                "publication Manifest destination already exists",
                path=str(self.path),
            )
        now = datetime.now(timezone.utc)
        manifest = LangfusePublicationManifest(
            audit_run_id=audit_run_id,
            experiment_name=experiment_name,
            dataset_name=dataset_name,
            created_at=now,
            updated_at=now,
            status=PublicationManifestStatus.PENDING,
        )
        return self.write(manifest)

    def read(self) -> LangfusePublicationManifest:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return LangfusePublicationManifest.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise PublicationManifestError(
                "cannot read a valid publication Manifest",
                path=str(self.path),
                exception_type=type(exc).__name__,
            ) from exc

    def write(
        self,
        manifest: LangfusePublicationManifest,
    ) -> LangfusePublicationManifest:
        if manifest.audit_run_id not in self.path.name:
            raise PublicationManifestError(
                "publication Manifest path does not match its Audit run",
                path=str(self.path),
            )
        payload = manifest.model_dump(mode="python")
        payload["updated_at"] = datetime.now(timezone.utc)
        updated = LangfusePublicationManifest.model_validate(payload)
        try:
            atomic_write_json(self.path, updated, mode=0o600)
        except AuditError as exc:
            raise PublicationManifestError(
                "cannot atomically write publication Manifest",
                path=str(self.path),
                cause=exc.code,
            ) from exc
        return updated

    def reference(
        self,
        manifest: LangfusePublicationManifest,
    ) -> PublicationManifestRef:
        try:
            digest = sha256_file(self.path)
        except AuditError as exc:
            raise PublicationManifestError(
                "cannot fingerprint publication Manifest",
                path=str(self.path),
                cause=exc.code,
            ) from exc
        return PublicationManifestRef(
            path=str(self.path),
            sha256=digest,
            status=manifest.status,
        )


__all__ = (
    "PublicationManifestStore",
    "publication_manifest_path",
)
