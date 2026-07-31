"""Background Review 的证据、状态、变更与预期合同。"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, StrictBool, StrictStr, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    PositiveInt,
    Sha256Digest,
    UtcDatetime,
)
from myhermes_audit.contracts.memory import MemoryStateSnapshot


class ReviewKind(str, Enum):
    MEMORY = "memory"
    SKILL = "skill"


class ReviewEvidenceKind(str, Enum):
    USER_MESSAGE = "user_message"
    TOOL_OBSERVATION = "tool_observation"
    TOOL_ERROR = "tool_error"
    ASSISTANT_DECISION_UNVERIFIED = "assistant_decision_unverified"
    ASSISTANT_REPORT_UNVERIFIED = "assistant_report_unverified"


class ReviewAction(str, Enum):
    NO_OP = "no_op"
    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    REMOVE = "remove"
    REJECT = "reject"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    STALE = "stale"


class SkillSource(str, Enum):
    LOCAL = "local"
    BUNDLED = "bundled"
    INSTALLED = "installed"
    EXTERNAL = "external"


class SkillManagedBy(str, Enum):
    USER = "user"
    CURATOR = "curator"
    SYSTEM = "system"
    EXTERNAL = "external"


class ReviewEvidence(ContractModel):
    evidence_id: Identifier
    kind: ReviewEvidenceKind
    content: NonEmptyText
    sequence: PositiveInt
    source_run_id: Identifier | None = None
    metadata: JsonObject = Field(default_factory=dict)


class UserStateSnapshot(ContractModel):
    """USER.md 或未来用户画像存储的算法无关状态。"""

    content: StrictStr | None = None
    revision: Sha256Digest | None = None
    metadata: JsonObject = Field(default_factory=dict)


class SkillStateSnapshot(ContractModel):
    skill_id: Identifier
    name: NonEmptyText
    source: SkillSource
    managed_by: SkillManagedBy
    pinned: StrictBool
    revision: Sha256Digest
    governance_revision: Sha256Digest
    metadata: JsonObject = Field(default_factory=dict)


class ReviewStateSnapshot(ContractModel):
    snapshot_id: Identifier
    captured_at: UtcDatetime
    memory: MemoryStateSnapshot | None = None
    user: UserStateSnapshot | None = None
    skills: list[SkillStateSnapshot] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_skills(self) -> "ReviewStateSnapshot":
        ids = [skill.skill_id for skill in self.skills]
        if len(ids) != len(set(ids)):
            raise ValueError("skill_id must be unique within a review snapshot")
        return self


class ReviewRequest(ContractModel):
    review_id: Identifier
    kind: ReviewKind
    evidence: list[ReviewEvidence] = Field(min_length=1)
    before_snapshot: ReviewStateSnapshot
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evidence(self) -> "ReviewRequest":
        ids = [item.evidence_id for item in self.evidence]
        sequences = [item.sequence for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_id must be unique within a ReviewRequest")
        if len(sequences) != len(set(sequences)):
            raise ValueError("evidence sequence must not repeat")
        if sequences and sequences[0] != 1:
            raise ValueError("evidence sequence must start at 1")
        if sequences != sorted(sequences):
            raise ValueError("evidence must be sorted by ascending sequence")
        return self


class ReviewTarget(ContractModel):
    target_type: NonEmptyText
    target_id: Identifier


class ReviewChange(ContractModel):
    action: ReviewAction
    target_type: NonEmptyText
    target_id: Identifier
    before_hash: Sha256Digest | None = None
    after_hash: Sha256Digest | None = None
    reason: NonEmptyText
    evidence_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hash_transition(self) -> "ReviewChange":
        if self.action is ReviewAction.CREATE and self.before_hash is not None:
            raise ValueError("create changes must not declare before_hash")
        if self.action is ReviewAction.REMOVE and self.after_hash is not None:
            raise ValueError("remove changes must not declare after_hash")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must not repeat")
        return self


class ReviewError(ContractModel):
    error_type: Identifier
    message: NonEmptyText
    retryable: StrictBool = False


class ReviewOutcome(ContractModel):
    review_id: Identifier
    kind: ReviewKind
    status: ReviewStatus
    changes: list[ReviewChange] = Field(default_factory=list)
    no_op_reason: NonEmptyText | None = None
    error: ReviewError | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> "ReviewOutcome":
        if self.no_op_reason is not None and self.changes:
            raise ValueError("no_op_reason cannot be combined with changes")
        if self.status is ReviewStatus.FAILED and self.error is None:
            raise ValueError("failed review outcomes require error")
        if self.status is not ReviewStatus.FAILED and self.error is not None:
            raise ValueError("error is only valid for failed review outcomes")
        return self


class BackgroundReviewExpectation(ContractModel):
    expected_action: ReviewAction | None = None
    expected_target: ReviewTarget | None = None
    must_change: list[ReviewTarget] = Field(default_factory=list)
    must_not_change: list[ReviewTarget] = Field(default_factory=list)
    required_evidence_kinds: list[ReviewEvidenceKind] = Field(default_factory=list)
    forbidden_evidence_kinds: list[ReviewEvidenceKind] = Field(default_factory=list)
    must_be_no_op: StrictBool = False
    protected_targets: list[ReviewTarget] = Field(default_factory=list)
    expected_stale_rejection: StrictBool = False

    @model_validator(mode="after")
    def validate_expectation(self) -> "BackgroundReviewExpectation":
        required = set(self.required_evidence_kinds)
        forbidden = set(self.forbidden_evidence_kinds)
        if len(required) != len(self.required_evidence_kinds):
            raise ValueError("required_evidence_kinds must not repeat")
        if len(forbidden) != len(self.forbidden_evidence_kinds):
            raise ValueError("forbidden_evidence_kinds must not repeat")
        if required & forbidden:
            raise ValueError("an evidence kind cannot be both required and forbidden")
        changing = {(target.target_type, target.target_id) for target in self.must_change}
        unchanged = {
            (target.target_type, target.target_id) for target in self.must_not_change
        }
        protected = {
            (target.target_type, target.target_id) for target in self.protected_targets
        }
        if len(changing) != len(self.must_change):
            raise ValueError("must_change targets must not repeat")
        if len(unchanged) != len(self.must_not_change):
            raise ValueError("must_not_change targets must not repeat")
        if len(protected) != len(self.protected_targets):
            raise ValueError("protected_targets must not repeat")
        if changing & unchanged:
            raise ValueError("a target cannot both change and remain unchanged")
        if changing & protected:
            raise ValueError("a protected target cannot be required to change")
        if self.must_be_no_op and self.must_change:
            raise ValueError("must_be_no_op cannot be combined with must_change")
        if self.must_be_no_op and self.expected_action not in {
            None,
            ReviewAction.NO_OP,
        }:
            raise ValueError("must_be_no_op conflicts with expected_action")
        if self.must_be_no_op and self.expected_stale_rejection:
            raise ValueError("no-op and stale rejection are distinct expectations")
        if self.expected_stale_rejection and self.expected_action not in {
            None,
            ReviewAction.REJECT,
        }:
            raise ValueError("expected_stale_rejection requires reject action")
        return self
