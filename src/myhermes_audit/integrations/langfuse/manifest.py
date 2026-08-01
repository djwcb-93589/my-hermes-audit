"""Atomic, credential-free Langfuse publication manifest persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from myhermes_audit.artifacts import atomic_write_json, sha256_file
from myhermes_audit.contracts import (
    LangfusePublicationManifest,
    LangfusePublishError,
    PublicationItemStatus,
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
        score_submission_supported: bool = False,
        score_confirmation_supported: bool = False,
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
            score_submission_supported=score_submission_supported,
            score_confirmation_supported=score_confirmation_supported,
            status=PublicationManifestStatus.PENDING,
        )
        return self.write(manifest)

    def load_or_create(
        self,
        *,
        audit_run_id: str,
        experiment_name: str,
        dataset_name: str,
        score_submission_supported: bool = False,
        score_confirmation_supported: bool = False,
    ) -> LangfusePublicationManifest:
        if self.path.is_symlink():
            raise PublicationManifestError(
                "publication Manifest cannot be a symbolic link",
                path=str(self.path),
            )
        if not self.path.exists() and not self.path.is_symlink():
            return self.create(
                audit_run_id=audit_run_id,
                experiment_name=experiment_name,
                dataset_name=dataset_name,
                score_submission_supported=score_submission_supported,
                score_confirmation_supported=score_confirmation_supported,
            )
        manifest = self.read()
        if (
            manifest.audit_run_id != audit_run_id
            or manifest.experiment_name != experiment_name
            or manifest.dataset_name != dataset_name
        ):
            raise PublicationManifestError(
                "publication Manifest identity conflicts with the Audit run",
                path=str(self.path),
            )
        manifest, recovered = recover_interrupted_publications(manifest)
        needs_write = recovered
        if (
            manifest.score_submission_supported != score_submission_supported
            or manifest.score_confirmation_supported
            != score_confirmation_supported
        ):
            payload = manifest.model_dump(mode="python")
            payload.update(
                {
                    "score_submission_supported": score_submission_supported,
                    "score_confirmation_supported": score_confirmation_supported,
                }
            )
            manifest = LangfusePublicationManifest.model_validate(payload)
            needs_write = True
        if needs_write:
            manifest = self.write(manifest)
        return manifest

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


def recover_interrupted_publications(
    manifest: LangfusePublicationManifest,
) -> tuple[LangfusePublicationManifest, bool]:
    """Converge records left in-flight by a previous process interruption."""

    now = datetime.now(timezone.utc)
    recovered_errors: list[tuple[datetime, LangfusePublishError]] = []
    trials = []
    for record in manifest.trial_publications:
        if record.status is not PublicationItemStatus.PUBLISHING:
            trials.append(record)
            continue
        error = LangfusePublishError(
            phase="trial",
            error_type="interrupted_trial_publication",
            message=(
                "Previous Trial publication was interrupted before its remote "
                "outcome could be confirmed."
            ),
            trial_id=record.trial_id,
            retryable=True,
        )
        trials.append(
            record.model_copy(
                update={
                    "status": PublicationItemStatus.UNCERTAIN,
                    "updated_at": now,
                    "confirmed_at": None,
                    "error": error,
                }
            )
        )
        recovered_errors.append((record.last_attempt_at or record.updated_at, error))

    scores = []
    for record in manifest.score_publications:
        if record.status is not PublicationItemStatus.PUBLISHING:
            scores.append(record)
            continue
        error = LangfusePublishError(
            phase="score_confirmation",
            error_type="interrupted_score_publication",
            message=(
                "Previous Score publication was interrupted before its remote "
                "outcome could be confirmed."
            ),
            trial_id=record.identity.trial_id,
            retryable=True,
            metadata={"score_id": record.identity.score_id},
        )
        scores.append(
            record.model_copy(
                update={
                    "status": PublicationItemStatus.UNCERTAIN,
                    "updated_at": now,
                    "confirmed_at": None,
                    "error": error,
                }
            )
        )
        recovered_errors.append((record.last_attempt_at or record.updated_at, error))

    if not recovered_errors:
        return manifest, False
    payload = manifest.model_dump(mode="python")
    payload.update(
        {
            "trial_publications": trials,
            "score_publications": scores,
            "updated_at": now,
        }
    )
    recovered = LangfusePublicationManifest.model_validate(payload)
    latest_error = max(recovered_errors, key=lambda item: item[0])[1]
    payload = recovered.model_dump(mode="python")
    payload.update(
        {
            "status": publication_status_from_records(recovered),
            "last_error": latest_error,
        }
    )
    return LangfusePublicationManifest.model_validate(payload), True


def publication_status_from_records(
    manifest: LangfusePublicationManifest,
) -> PublicationManifestStatus:
    """Derive a manifest lifecycle status from all Trial and Score records."""

    records = (*manifest.trial_publications, *manifest.score_publications)
    if not records or all(
        item.status is PublicationItemStatus.PENDING for item in records
    ):
        return PublicationManifestStatus.PENDING
    if any(item.status is PublicationItemStatus.PUBLISHING for item in records):
        return PublicationManifestStatus.PUBLISHING
    if manifest.trial_publications and all(
        item.status is PublicationItemStatus.CONFIRMED for item in records
    ):
        return PublicationManifestStatus.PUBLISHED
    if any(item.status is PublicationItemStatus.CONFIRMED for item in records):
        return PublicationManifestStatus.PARTIALLY_PUBLISHED
    return PublicationManifestStatus.FAILED


__all__ = (
    "PublicationManifestStore",
    "publication_status_from_records",
    "publication_manifest_path",
    "recover_interrupted_publications",
)
