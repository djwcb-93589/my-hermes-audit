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
from myhermes_audit.contracts.ablation import (
    CompressionControl,
    CompressionMode,
    MemoryMode,
)
from myhermes_audit.contracts.memory import MemoryKind, RetrievalStrategy
from myhermes_audit.contracts.background_review import ReviewKind


CAPABILITY_PROTOCOL_VERSION = "5.1"
CapabilityProtocolVersion = Literal["5.1"]


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
    supported_review_kinds: list[ReviewKind] = Field(default_factory=list)
    memory_provider: NonEmptyText | None = None
    supported_memory_modes: list[MemoryMode] = Field(default_factory=list)
    supported_compression_modes: list[CompressionMode] = Field(
        default_factory=list
    )
    # Process actions are projected from the public Tool declaration enum and
    # statuses from the public ProcessStatus enum. These fields intentionally
    # contain no handler or ProcessManager implementation details.
    supported_process_actions: list[NonEmptyText] = Field(default_factory=list)
    supported_process_statuses: list[NonEmptyText] = Field(default_factory=list)
    process_toolset: NonEmptyText | None = None
    process_start_via_terminal: StrictBool = False
    process_log: StrictBool = False
    process_poll: StrictBool = False
    process_wait: StrictBool = False
    process_write: StrictBool = False
    process_submit: StrictBool = False
    process_kill: StrictBool = False
    process_close: StrictBool = False
    process_interrupt: StrictBool = False
    compression_control: CompressionControl = CompressionControl.UNAVAILABLE
    compression_configuration_paths: list[NonEmptyText] = Field(
        default_factory=list
    )
    compression_threshold_control: StrictBool = False
    compression_threshold_configuration: StrictBool = False
    emergency_overflow_compression_disable_supported: StrictBool = False
    compression_observation_supported: StrictBool = False
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
        if len(self.supported_review_kinds) != len(set(self.supported_review_kinds)):
            raise ValueError("supported_review_kinds must not repeat")
        native_supported = (
            RetrievalStrategy.SUBJECT_NATIVE
            in self.supported_retrieval_strategies
        )
        if native_supported != (self.memory_provider is not None):
            raise ValueError(
                "memory_provider must be present exactly when subject_native is supported"
            )
        if len(self.supported_memory_modes) != len(set(self.supported_memory_modes)):
            raise ValueError("supported_memory_modes must not repeat")
        if len(self.supported_compression_modes) != len(
            set(self.supported_compression_modes)
        ):
            raise ValueError("supported_compression_modes must not repeat")
        if len(self.supported_process_actions) != len(
            set(self.supported_process_actions)
        ):
            raise ValueError("supported_process_actions must not repeat")
        if len(self.supported_process_statuses) != len(
            set(self.supported_process_statuses)
        ):
            raise ValueError("supported_process_statuses must not repeat")
        action_set = set(self.supported_process_actions)
        if (self.process_toolset is None) != (not action_set):
            raise ValueError(
                "process_toolset must be present exactly when public process actions exist"
            )
        process_flags = {
            "process_log": "log",
            "process_poll": "poll",
            "process_wait": "wait",
            "process_write": "write",
            "process_submit": "submit",
            "process_kill": "kill",
            "process_close": "close",
            "process_interrupt": "interrupt",
        }
        for field_name, action in process_flags.items():
            if getattr(self, field_name) != (action in action_set):
                raise ValueError(f"{field_name} must match supported_process_actions")
        if self.process_start_via_terminal and self.process_toolset is None:
            raise ValueError("terminal Process start requires the public process toolset")
        if self.process_toolset is None and self.supported_process_statuses:
            raise ValueError(
                "supported_process_statuses require the public process toolset"
            )
        if len(self.compression_configuration_paths) != len(
            set(self.compression_configuration_paths)
        ):
            raise ValueError("compression configuration paths must not repeat")
        threshold_control = self.capability("compression_threshold_control")
        threshold_control_available = (
            threshold_control is not None and threshold_control.available
        )
        if self.compression_threshold_control != threshold_control_available:
            raise ValueError(
                "compression threshold control field must match its capability"
            )
        if threshold_control_available != (
            self.compression_control is not CompressionControl.UNAVAILABLE
        ):
            raise ValueError("compression_control must match threshold control")
        if threshold_control_available != bool(self.supported_compression_modes):
            raise ValueError(
                "supported compression modes must match compression control"
            )
        threshold_configuration = self.capability(
            "compression_threshold_configuration"
        )
        if self.compression_threshold_configuration != (
            threshold_configuration is not None
            and threshold_configuration.available
        ):
            raise ValueError(
                "compression threshold configuration field must match its capability"
            )
        if self.compression_threshold_configuration != bool(
            self.compression_configuration_paths
        ):
            raise ValueError(
                "compression configuration paths must match threshold configuration"
            )
        emergency_disable = self.capability("emergency_compression_disable")
        if self.emergency_overflow_compression_disable_supported != (
            emergency_disable is not None and emergency_disable.available
        ):
            raise ValueError(
                "emergency Compression disable field must match its capability"
            )
        observation = self.capability("compression_observation")
        if self.compression_observation_supported != (
            observation is not None and observation.available
        ):
            raise ValueError(
                "compression observation field must match the capability check"
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
