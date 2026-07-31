"""Atomic local artifact publication and portable references."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from myhermes_audit.contracts import ArtifactRef
from myhermes_audit.errors import ReportError, UnsafePathError
from myhermes_audit.serialization import pretty_json


def _require_regular_destination(path: Path) -> Path:
    requested = Path(path)
    if requested.is_symlink():
        raise UnsafePathError(requested, reason="artifact destination is a symlink")
    if requested.parent.is_symlink():
        raise UnsafePathError(
            requested.parent,
            reason="artifact parent directory is a symlink",
        )
    destination = requested.resolve(strict=False)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportError(
            "cannot prepare artifact directory",
            operation="prepare_artifact_directory",
        ) from exc
    return destination


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> Path:
    destination = _require_regular_destination(path)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        if mode is not None:
            try:
                temporary.chmod(mode)
            except OSError:
                pass
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReportError(
            "cannot publish local artifact",
            operation="atomic_write",
        ) from exc
    return destination


def atomic_write_json(path: Path, value: object, *, mode: int | None = None) -> Path:
    return atomic_write_text(path, pretty_json(value) + "\n", mode=mode)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReportError(
            "cannot hash artifact",
            operation="hash_artifact",
        ) from exc
    return digest.hexdigest()


def artifact_ref(
    path: Path,
    *,
    trial_root: Path,
    artifact_id: str,
    kind: str,
) -> ArtifactRef:
    resolved_root = Path(trial_root).resolve(strict=True)
    candidate = Path(path)
    if candidate.is_symlink():
        raise UnsafePathError(candidate, reason="artifact cannot be a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise UnsafePathError(candidate, reason="artifact escaped the Trial root")
    relative = resolved.relative_to(resolved_root).as_posix()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ReportError(
            "cannot stat artifact",
            operation="stat_artifact",
        ) from exc
    return ArtifactRef(
        artifact_id=artifact_id,
        kind=kind,
        relative_path=relative,
        sha256=sha256_file(resolved),
        size_bytes=size,
    )


__all__ = (
    "artifact_ref",
    "atomic_write_json",
    "atomic_write_text",
    "sha256_file",
)
