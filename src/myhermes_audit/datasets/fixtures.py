"""Safe fixture materialization into a Trial Sandbox."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from myhermes_audit.artifacts import atomic_write_json, sha256_file
from myhermes_audit.contracts.common import (
    ContractModel,
    FixtureTargetPath,
    NonNegativeInt,
    Sha256Digest,
)
from myhermes_audit.contracts.suite import FixtureSpec
from myhermes_audit.errors import (
    FixtureMaterializationError,
    UnsupportedCaseError,
)
from myhermes_audit.sandbox import AuditSandbox


_RESERVED_FILENAMES = frozenset({
    ".myhermes-audit-owned.json",
    "manifest.json",
})
_RESERVED_TARGETS = frozenset({
    "hermes_home/.env",
    "hermes_home/config.yaml",
    "hermes_home/hermes.db",
})


class FixtureManifestEntry(ContractModel):
    target: FixtureTargetPath
    sha256: Sha256Digest
    size_bytes: NonNegativeInt


class FixtureManifest(ContractModel):
    files: list[FixtureManifestEntry] = Field(default_factory=list)


def validate_p1_fixture_support(fixture: FixtureSpec) -> None:
    validate_runtime_fixture_support(
        fixture,
        allow_memory=False,
        allow_background_review=False,
    )


def validate_runtime_fixture_support(
    fixture: FixtureSpec,
    *,
    allow_memory: bool,
    allow_background_review: bool = False,
) -> None:
    unsupported: list[str] = []
    if fixture.memory is not None and not allow_memory:
        unsupported.append("memory")
    if fixture.skills and not allow_background_review:
        unsupported.append("skills")
    if fixture.database is not None:
        unsupported.append("database")
    if fixture.review_requests:
        unsupported.append("review_requests")
    if fixture.background_review_plans and not allow_background_review:
        unsupported.append("background_review_plans")
    if unsupported:
        raise UnsupportedCaseError(
            (
                "runner supports only declared file, Memory, and explicit Background Review fixtures"
                if allow_memory
                else "this execution mode supports only file fixtures"
            ),
            unsupported_fixtures=unsupported,
        )

    for item in fixture.files:
        _reject_reserved_target(item.path)
        if item.source is None:
            continue
        source = item.resolved_source
        if source is None:
            raise FixtureMaterializationError(
                "fixture source was not resolved by the Suite loader",
                target=item.path,
            )
        if source.is_symlink():
            raise FixtureMaterializationError(
                "fixture source cannot be a symbolic link",
                target=item.path,
            )
        try:
            if not source.resolve(strict=True).is_file():
                raise FixtureMaterializationError(
                    "fixture source is not a regular file",
                    target=item.path,
                )
        except OSError as exc:
            raise FixtureMaterializationError(
                "fixture source is unavailable",
                target=item.path,
            ) from exc


def materialize_fixtures(
    fixture: FixtureSpec,
    sandbox: AuditSandbox,
    *,
    allow_background_review: bool = False,
) -> tuple[FixtureManifest, Path]:
    validate_runtime_fixture_support(
        fixture,
        allow_memory=True,
        allow_background_review=allow_background_review,
    )
    entries: list[FixtureManifestEntry] = []
    for item in fixture.files:
        try:
            if item.content is not None:
                target = sandbox.write_fixture_content(item.content, item.path)
            else:
                source = item.resolved_source
                if source is None:
                    raise FixtureMaterializationError(
                        "fixture source is unavailable",
                        target=item.path,
                    )
                target = sandbox.copy_fixture_file(source, item.path)
            entries.append(
                FixtureManifestEntry(
                    target=item.path,
                    sha256=sha256_file(target),
                    size_bytes=target.stat().st_size,
                )
            )
        except FixtureMaterializationError:
            raise
        except (OSError, ValueError) as exc:
            raise FixtureMaterializationError(
                "fixture materialization failed",
                target=item.path,
            ) from exc

    manifest = FixtureManifest(files=entries)
    manifest_path = sandbox.artifacts_dir / "fixture-manifest.json"
    atomic_write_json(manifest_path, manifest)
    return manifest, manifest_path


def _reject_reserved_target(target: str) -> None:
    path = target.casefold()
    name = path.rsplit("/", 1)[-1]
    if (
        path in _RESERVED_TARGETS
        or any(path.startswith(f"{reserved}/") for reserved in _RESERVED_TARGETS)
        or name in _RESERVED_FILENAMES
    ):
        raise FixtureMaterializationError(
            "fixture target is reserved by the Audit runtime",
            target=target,
        )
    if path.startswith("hermes_home/database/"):
        raise FixtureMaterializationError(
            "fixture target overlaps the reserved database area",
            target=target,
        )


__all__ = (
    "FixtureManifest",
    "FixtureManifestEntry",
    "materialize_fixtures",
    "validate_p1_fixture_support",
    "validate_runtime_fixture_support",
)
