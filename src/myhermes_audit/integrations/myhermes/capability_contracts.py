"""Strict file-protocol contracts for the read-only MyHermes capability probe."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictStr, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    GitObjectId,
    Identifier,
    NonEmptyText,
    Sha256Digest,
)
from myhermes_audit.contracts.memory import MemoryKind, RetrievalStrategy


CAPABILITY_PROTOCOL_VERSION = "2.0"
CapabilityProtocolVersion = Literal["2.0"]


class SubjectCapabilityProbeRequest(ContractModel):
    protocol_version: CapabilityProtocolVersion = CAPABILITY_PROTOCOL_VERSION
    subject_repo: StrictStr
    subject_commit: GitObjectId


class SubjectCapabilityCheck(ContractModel):
    name: Identifier
    required: StrictBool = True
    available: StrictBool
    module: NonEmptyText
    public_object: NonEmptyText
    signature: StrictStr | None = None
    failure_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> "SubjectCapabilityCheck":
        if self.available and self.failure_type is not None:
            raise ValueError("available capability cannot contain a failure type")
        return self


class SubjectCapabilityWarning(ContractModel):
    warning_type: Identifier
    message: NonEmptyText


class SubjectCapabilityProbeError(ContractModel):
    error_type: Identifier
    message: NonEmptyText


class SubjectCapabilityReport(ContractModel):
    protocol_version: CapabilityProtocolVersion = CAPABILITY_PROTOCOL_VERSION
    subject_commit: GitObjectId
    compatible: StrictBool
    capabilities: list[SubjectCapabilityCheck] = Field(default_factory=list)
    missing_capabilities: list[Identifier] = Field(default_factory=list)
    supported_memory_kinds: list[MemoryKind] = Field(default_factory=list)
    supported_retrieval_strategies: list[RetrievalStrategy] = Field(
        default_factory=list
    )
    memory_provider: NonEmptyText | None = None
    warnings: list[SubjectCapabilityWarning] = Field(default_factory=list)
    public_api_fingerprint: Sha256Digest
    error: SubjectCapabilityProbeError | None = None

    @model_validator(mode="after")
    def validate_report(self) -> "SubjectCapabilityReport":
        names = [item.name for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("capability names must be unique")
        missing = [
            item.name
            for item in self.capabilities
            if item.required and not item.available
        ]
        if self.missing_capabilities != missing:
            raise ValueError(
                "missing_capabilities must match unavailable capabilities in order"
            )
        if len(self.missing_capabilities) != len(set(self.missing_capabilities)):
            raise ValueError("missing_capabilities must not repeat")
        if len(self.supported_memory_kinds) != len(
            set(self.supported_memory_kinds)
        ):
            raise ValueError("supported_memory_kinds must not repeat")
        if len(self.supported_retrieval_strategies) != len(
            set(self.supported_retrieval_strategies)
        ):
            raise ValueError("supported_retrieval_strategies must not repeat")
        native_supported = (
            RetrievalStrategy.SUBJECT_NATIVE
            in self.supported_retrieval_strategies
        )
        if native_supported != (self.memory_provider is not None):
            raise ValueError(
                "memory_provider must be present exactly when subject_native is supported"
            )
        warning_types = [item.warning_type for item in self.warnings]
        if len(warning_types) != len(set(warning_types)):
            raise ValueError("capability warning types must not repeat")
        if self.compatible != (not self.missing_capabilities and self.error is None):
            raise ValueError("compatible must reflect missing capabilities and error")
        return self

    def capability(self, name: str) -> SubjectCapabilityCheck | None:
        return next((item for item in self.capabilities if item.name == name), None)


__all__ = (
    "CAPABILITY_PROTOCOL_VERSION",
    "SubjectCapabilityCheck",
    "SubjectCapabilityProbeError",
    "SubjectCapabilityProbeRequest",
    "SubjectCapabilityReport",
    "SubjectCapabilityWarning",
)
