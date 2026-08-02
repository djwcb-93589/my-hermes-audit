"""Stable, non-destructive Langfuse Dataset synchronization planning."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import PurePosixPath

from myhermes_audit.ablation import (
    applicable_checkpoints,
    applicable_fact_expectations,
)

from myhermes_audit.contracts import (
    AuditCase,
    AuditSuite,
    DataClassification,
    LangfuseDatasetIdentity,
    LangfuseDatasetItemIdentity,
    LangfuseDatasetItemPlan,
    LangfuseDatasetSyncPlan,
    LangfuseDatasetSyncResult,
)
from myhermes_audit.contracts.data import classification_from_metadata
from myhermes_audit.errors import DatasetSyncError, LangfuseConfigError
from myhermes_audit.fingerprint import suite_sha256
from myhermes_audit.integrations.langfuse.redaction import project_remote_content
from myhermes_audit.security import sensitive_environment_values
from myhermes_audit.serialization import canonical_sha256


_DATASET_ITEM_NAMESPACE = uuid.UUID("6a1dc734-14f4-5e72-9b75-9d30e373e34b")
_SAFE_FIXTURE_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".html": "text/html",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".toml": "application/toml",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


def build_dataset_sync_plan(
    suite: AuditSuite,
    *,
    dataset_name: str,
    dry_run: bool,
    no_content: bool,
) -> LangfuseDatasetSyncPlan:
    dataset_name = _normalize_dataset_name(dataset_name)
    suite_hash = suite_sha256(suite)
    dataset = LangfuseDatasetIdentity(
        dataset_name=dataset_name,
        suite_id=suite.suite_id,
        suite_sha256=suite_hash,
    )
    sensitive_values = sensitive_environment_values(os.environ)
    suite_classification = classification_from_metadata(suite.defaults.metadata)
    items: list[LangfuseDatasetItemPlan] = []
    for case in suite.cases:
        classification = _case_classification(case, suite_classification)
        case_hash = canonical_sha256(case)
        variants = [None] if case.ablation is None else case.ablation.variants
        for variant in variants:
            variant_id = None if variant is None else variant.variant_id
            identity_parts = (
                (
                    dataset_name,
                    suite.suite_id,
                    case.case_id,
                    case_hash,
                )
                if variant_id is None
                else (
                    dataset_name,
                    suite.suite_id,
                    case.case_id,
                    variant_id,
                    case_hash,
                )
            )
            remote_item_id = str(
                uuid.uuid5(
                    _DATASET_ITEM_NAMESPACE,
                    "|".join(identity_parts),
                )
            )
            identity = LangfuseDatasetItemIdentity(
                dataset_name=dataset_name,
                case_id=case.case_id,
                variant_id=variant_id,
                case_sha256=case_hash,
                remote_item_id=remote_item_id,
            )
            input_projection = _case_input(case)
            if variant is not None:
                input_projection = {
                    **input_projection,
                    "ablation_variant": variant.model_dump(
                        mode="json",
                        exclude={"schema_version"},
                    ),
                }
            remote_input = project_remote_content(
                input_projection,
                classification=classification,
                no_content=no_content,
                sensitive_values=sensitive_values,
            )
            remote_expected_output = project_remote_content(
                _case_expectations(case, variant_id=variant_id),
                classification=classification,
                no_content=no_content,
                sensitive_values=sensitive_values,
            )
            fixture_summary = _fixture_manifest_summary(
                case,
                synthetic=classification is DataClassification.SYNTHETIC,
            )
            publication_metadata = {
                "audit_suite_id": suite.suite_id,
                "audit_suite_sha256": suite_hash,
                "audit_case_id": case.case_id,
                **(
                    {}
                    if variant_id is None
                    else {"audit_variant_id": variant_id}
                ),
                "audit_case_sha256": case_hash,
                "case_mode": case.mode.value,
                "data_classification": classification.value,
                "content_omitted": (
                    no_content or classification is DataClassification.SENSITIVE
                ),
                "fixture_content_uploaded": False,
                **fixture_summary,
                "memory_fixture_uploaded": False,
                "skill_content_uploaded": False,
                "database_fixture_uploaded": False,
            }
            projection_sha256 = canonical_sha256(
                {
                    "input": remote_input,
                    "expected_output": remote_expected_output,
                    "metadata": {
                        key: value
                        for key, value in publication_metadata.items()
                        if key != "audit_suite_sha256"
                    },
                }
            )
            items.append(
                LangfuseDatasetItemPlan(
                    identity=identity,
                    input=remote_input,
                    expected_output=remote_expected_output,
                    metadata={
                        **publication_metadata,
                        "audit_projection_sha256": projection_sha256,
                    },
                )
            )
    return LangfuseDatasetSyncPlan(
        dataset=dataset,
        items=items,
        dry_run=dry_run,
        no_content=no_content,
    )


def dry_run_sync_result(
    plan: LangfuseDatasetSyncPlan,
) -> LangfuseDatasetSyncResult:
    if not plan.dry_run:
        raise ValueError("dry_run_sync_result requires a dry-run plan")
    return LangfuseDatasetSyncResult(
        dataset=plan.dataset,
        items=[item.identity for item in plan.items],
        dry_run=True,
        planned_upsert_count=len(plan.items),
        added_count=None,
        updated_count=None,
        unchanged_count=None,
        warnings=[
            "remote add/update/unchanged counts are unknown because dry-run performs no connection"
        ],
    )


def _case_classification(
    case: AuditCase,
    suite_classification: DataClassification,
) -> DataClassification:
    if "data_classification" not in case.metadata:
        return suite_classification
    return classification_from_metadata(
        case.metadata,
        default=suite_classification,
    )


def _case_input(case: AuditCase) -> dict:
    identity = {
        "case_id": case.case_id,
        "case_mode": case.mode.value,
        "tags": list(case.tags),
    }
    if case.input.message is not None:
        return {
            **identity,
            "message": case.input.message,
            **(
                {}
                if case.input.session_id is None
                else {"session_id": case.input.session_id}
            ),
        }
    if case.input.turns:
        return {
            **identity,
            "turns": [
                {
                    "role": turn.role.value,
                    "message": turn.message,
                    **(
                        {}
                        if turn.session_id is None
                        else {"session_id": turn.session_id}
                    ),
                }
                for turn in case.input.turns
            ]
        }
    simulated = case.input.simulated_user
    return {
        **identity,
        "simulated_user": (
            None
            if simulated is None
            else simulated.model_dump(mode="json", exclude={"schema_version"})
        )
    }


def _case_expectations(
    case: AuditCase,
    *,
    variant_id: str | None,
) -> dict:
    expected = case.expected
    return {
        "files": [_without_schema(item) for item in expected.files],
        "texts": [_without_schema(item) for item in expected.texts],
        "json_values": [_without_schema(item) for item in expected.json_values],
        "tool_trajectories": [
            {
                "required_tools": item.required_tools,
                "forbidden_tools": item.forbidden_tools,
                "minimum_tool_calls": item.minimum_tool_calls,
                "maximum_tool_calls": item.maximum_tool_calls,
                "required_successful_tools": item.required_successful_tools,
            }
            for item in expected.tool_trajectories
        ],
        **(
            {}
            if not expected.memories
            else {
                "memories": [
                    _without_schema(item) for item in expected.memories
                ]
            }
        ),
        **(
            {}
            if not expected.memory_states
            else {
                "memory_states": [
                    _without_schema(item) for item in expected.memory_states
                ]
            }
        ),
        **(
            {}
            if variant_id is None
            else {
                "required_facts": [
                    _without_schema(item)
                    for item in applicable_fact_expectations(case, variant_id)
                ],
                "checkpoints": [
                    _without_schema(item)
                    for item in applicable_checkpoints(case, variant_id)
                ],
            }
        ),
        "judges": [_without_schema(item) for item in expected.judges],
    }


def _fixture_manifest_summary(
    case: AuditCase,
    *,
    synthetic: bool,
) -> dict[str, int | str]:
    summaries = []
    for fixture in case.fixture.files:
        target = PurePosixPath(fixture.path.replace("\\", "/")).as_posix()
        if fixture.content is not None:
            content = fixture.content.encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            size_bytes = len(content)
        else:
            source = fixture.resolved_source
            if source is None:
                raise DatasetSyncError(
                    "fixture source was not resolved before Dataset planning",
                    case_id=case.case_id,
                    fixture_target=fixture.path,
                )
            hasher = hashlib.sha256()
            size_bytes = 0
            try:
                with source.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        hasher.update(chunk)
                        size_bytes += len(chunk)
            except OSError as exc:
                raise DatasetSyncError(
                    "fixture could not be fingerprinted for Dataset planning",
                    case_id=case.case_id,
                    fixture_target=fixture.path,
                    exception_type=type(exc).__name__,
                ) from exc
            digest = hasher.hexdigest()
        summaries.append(
            {
                "target": target,
                "sha256": digest,
                "size_bytes": size_bytes,
                "content_type": _safe_fixture_content_type(target),
                "synthetic": synthetic,
            }
        )
    summaries.sort(
        key=lambda item: (
            item["target"],
            item["sha256"],
            item["size_bytes"],
            item["content_type"],
            item["synthetic"],
        )
    )
    return {
        "fixture_file_count": len(summaries),
        "fixture_total_bytes": sum(item["size_bytes"] for item in summaries),
        "fixture_manifest_sha256": canonical_sha256(summaries),
    }


def _safe_fixture_content_type(target: str) -> str:
    return _SAFE_FIXTURE_CONTENT_TYPES.get(
        PurePosixPath(target).suffix.lower(),
        "application/octet-stream",
    )


def _without_schema(value) -> dict:
    return _strip_schema_versions(
        value.model_dump(mode="json", exclude={"schema_version"})
    )


def _strip_schema_versions(value):
    if isinstance(value, dict):
        return {
            key: _strip_schema_versions(item)
            for key, item in value.items()
            if key != "schema_version"
        }
    if isinstance(value, list):
        return [_strip_schema_versions(item) for item in value]
    return value


def _normalize_dataset_name(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 32 for character in normalized)
    ):
        raise LangfuseConfigError(
            "dataset_name must be a non-empty safe name up to 200 characters",
            field="dataset_name",
        )
    return normalized


__all__ = ("build_dataset_sync_plan", "dry_run_sync_result")
