"""Background Review 的证据、状态、变更与预期合同。"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from myhermes_audit.contracts.common import (
    ContractModel,
    Identifier,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    PositiveInt,
    Sha256Digest,
    UtcDatetime,
)
from myhermes_audit.contracts.memory import (
    MemoryKind,
    MemorySnapshotPhase,
    MemoryStateSnapshot,
    RetrievalStrategy,
)


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


class ObservedReviewAction(str, Enum):
    """A deterministic live-state transition, never an assistant assertion."""

    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    REMOVE = "remove"
    UNCHANGED = "unchanged"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    STALE = "stale"


class ReviewTrigger(str, Enum):
    """The only supported P5 trigger is a completed foreground turn."""

    AFTER_FOREGROUND_TURN = "after_foreground_turn"


class ReviewLifecycle(str, Enum):
    NORMAL = "normal"
    STALE_BEFORE_EXECUTE = "stale_before_execute"
    DUPLICATE_EXECUTE = "duplicate_execute"


BACKGROUND_REVIEW_ERROR_TYPES = frozenset(
    {
        "background_review_capability_error",
        "background_review_plan_error",
        "background_review_trigger_error",
        "background_review_claim_error",
        "background_review_claim_stale",
        "background_review_evidence_error",
        "background_review_prepare_error",
        "background_review_execution_error",
        "background_review_tool_error",
        "background_review_completion_error",
        "background_review_snapshot_error",
        "background_review_state_diff_error",
        "background_review_duplicate_error",
        "background_review_timeout",
        "background_review_cleanup_error",
        "background_review_protocol_error",
    }
)


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


class ReviewMemoryItemSnapshot(ContractModel):
    """A content-free public Memory state projection for a P5 Review."""

    memory_id: Identifier
    kind: MemoryKind
    content_sha256: Sha256Digest
    content_length: NonNegativeInt
    state_sha256: Sha256Digest


class ReviewMemorySnapshot(ContractModel):
    """P5 live Memory snapshot without Memory bodies or free metadata."""

    snapshot_id: Identifier
    phase: MemorySnapshotPhase | None = None
    strategy: RetrievalStrategy | None = None
    provider: NonEmptyText | None = None
    captured_at: UtcDatetime
    items: list[ReviewMemoryItemSnapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ReviewMemorySnapshot":
        identifiers = [item.memory_id for item in self.items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("memory_id must be unique within a Review snapshot")
        return self


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


class BackgroundReviewSkillSnapshot(ContractModel):
    """P5 live Skill projection without a name, body, or free metadata."""

    skill_id: Identifier
    name_sha256: Sha256Digest
    name_length: NonNegativeInt
    source: SkillSource
    managed_by: SkillManagedBy
    pinned: StrictBool
    revision: Sha256Digest
    governance_revision: Sha256Digest


class BackgroundReviewStateSnapshot(ContractModel):
    """Content-free state used only by Worker-produced P5 execution facts."""

    snapshot_id: Identifier
    captured_at: UtcDatetime
    memory: ReviewMemorySnapshot | None = None
    skills: list[BackgroundReviewSkillSnapshot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_skills(self) -> "BackgroundReviewStateSnapshot":
        ids = [skill.skill_id for skill in self.skills]
        if len(ids) != len(set(ids)):
            raise ValueError("skill_id must be unique within a Background Review snapshot")
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
    before_governance_revision: Sha256Digest | None = None
    after_governance_revision: Sha256Digest | None = None
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


class ObservedReviewChange(ContractModel):
    """A before/after state fact used for Review safety gates.

    It is deliberately separate from ``ReviewOutcome.changes``: a failed or
    rejected Subject operation can still leave an observable half-write, and
    that fact must never be discarded merely because the Subject did not
    declare a successful outcome.
    """

    action: ObservedReviewAction
    target_type: NonEmptyText
    target_id: Identifier
    before_hash: Sha256Digest | None = None
    after_hash: Sha256Digest | None = None
    before_governance_revision: Sha256Digest | None = None
    after_governance_revision: Sha256Digest | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> "ObservedReviewChange":
        if self.action is ObservedReviewAction.CREATE:
            if self.before_hash is not None or self.after_hash is None:
                raise ValueError("observed create requires only after_hash")
        elif self.action is ObservedReviewAction.REMOVE:
            if self.before_hash is None or self.after_hash is not None:
                raise ValueError("observed remove requires only before_hash")
        elif self.action is ObservedReviewAction.UNCHANGED:
            if self.before_hash != self.after_hash:
                raise ValueError("unchanged observations require equal hashes")
            if self.before_governance_revision != self.after_governance_revision:
                raise ValueError(
                    "unchanged observations require equal governance revisions"
                )
        elif self.before_hash is None or self.after_hash is None:
            raise ValueError("observed updates require before_hash and after_hash")
        elif (
            self.before_hash == self.after_hash
            and self.before_governance_revision
            == self.after_governance_revision
        ):
            raise ValueError("observed updates must change state")
        return self


class ReviewError(ContractModel):
    error_type: Identifier
    message: NonEmptyText
    retryable: StrictBool = False


class BackgroundReviewExecutionError(ContractModel):
    """A safe P5 execution diagnostic with no prompt, claim, or file path."""

    error_type: Identifier
    stage: Identifier
    message: NonEmptyText
    retryable: StrictBool = False
    exception_type: Identifier | None = None

    @field_validator("error_type")
    @classmethod
    def validate_error_type(cls, value: str) -> str:
        if value not in BACKGROUND_REVIEW_ERROR_TYPES:
            raise ValueError("unsupported Background Review execution error type")
        return value


class ReviewEvidenceProjection(ContractModel):
    """A privacy-preserving projection of real foreground/prepared evidence."""

    evidence_id: Identifier
    kind: ReviewEvidenceKind
    content_sha256: Sha256Digest
    content_length: NonNegativeInt
    sequence: PositiveInt
    source_turn_number: PositiveInt | None = None
    source_tool_call_id: Identifier | None = None
    source_evidence_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> "ReviewEvidenceProjection":
        if self.source_turn_number is None and self.source_tool_call_id is None:
            # Prepared evidence can instead explicitly refer to one foreground
            # projection.  This keeps the relationship inspectable without
            # serializing MyHermes prompt text.
            if self.source_evidence_id is None:
                raise ValueError(
                    "evidence projection requires a foreground source reference"
                )
        return self


class PreparedReviewRequest(ContractModel):
    """The safe, actual ``ReviewRunSpec.messages`` projection.

    MyHermes' system prompt and instruction are intentionally absent.  They are
    Review prompt material, not audit evidence.
    """

    review_id: Identifier
    kind: ReviewKind
    evidence: list[ReviewEvidenceProjection] = Field(default_factory=list)
    message_count: NonNegativeInt

    @model_validator(mode="after")
    def validate_prepared_evidence(self) -> "PreparedReviewRequest":
        ids = [item.evidence_id for item in self.evidence]
        sequences = [item.sequence for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("prepared evidence IDs must be unique")
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("prepared evidence sequence must be contiguous")
        # A Subject may compact several classified evidence entries into one
        # prepared message.  Message count and projection count therefore have
        # independent semantics and must not be artificially coupled.
        return self


class ReviewToolObservation(ContractModel):
    """Safe tool telemetry from a single synchronous Review run."""

    observation_id: Identifier
    tool_name: NonEmptyText
    status: NonEmptyText
    success: StrictBool
    error_type: Identifier | None = None
    duration_ms: NonNegativeInt


class ReviewAttempt(ContractModel):
    """A deterministic record proving duplicate calls did not rerun Review."""

    sequence: PositiveInt
    claim_valid: StrictBool
    loop_executed: StrictBool
    model_call_count: NonNegativeInt
    tool_call_count: NonNegativeInt
    state_change_count: NonNegativeInt
    error_type: Identifier | None = None

    @model_validator(mode="after")
    def validate_unexecuted_attempt(self) -> "ReviewAttempt":
        if not self.loop_executed and any(
            (
                self.model_call_count,
                self.tool_call_count,
                self.state_change_count,
            )
        ):
            raise ValueError("unexecuted Review attempts cannot report side effects")
        return self


class ReviewLifecycleScenario(ContractModel):
    """A strict, stable P5 Review invocation plan."""

    review_id: Identifier
    kind: ReviewKind
    trigger: ReviewTrigger = ReviewTrigger.AFTER_FOREGROUND_TURN
    foreground_session_id: Identifier
    trigger_after_turn: PositiveInt
    timeout_seconds: PositiveInt = Field(le=600)
    lifecycle: ReviewLifecycle = ReviewLifecycle.NORMAL
    repeat_count: PositiveInt = Field(default=1, le=2)
    stale_target: ReviewTarget | None = None
    continue_after_failure: StrictBool = True

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ReviewLifecycleScenario":
        if self.trigger is not ReviewTrigger.AFTER_FOREGROUND_TURN:
            raise ValueError("P5 supports only after_foreground_turn triggers")
        if self.lifecycle is ReviewLifecycle.NORMAL:
            if self.repeat_count != 1 or self.stale_target is not None:
                raise ValueError("normal Reviews require one attempt and no stale target")
        elif self.lifecycle is ReviewLifecycle.STALE_BEFORE_EXECUTE:
            if self.repeat_count != 1 or self.stale_target is None:
                raise ValueError(
                    "stale Reviews require one attempt and a declared stale target"
                )
        elif self.repeat_count != 2 or self.stale_target is not None:
            raise ValueError(
                "duplicate Reviews require exactly two attempts and no stale target"
            )
        return self


class BackgroundReviewPlan(ReviewLifecycleScenario):
    """Named P5 plan type retained separately from lifecycle terminology."""


class BackgroundReviewExecutionResult(ContractModel):
    """The complete, trial-local fact record for one planned Background Review."""

    review_id: Identifier
    kind: ReviewKind
    lifecycle: ReviewLifecycle
    status: ReviewStatus
    actual_action: ReviewAction
    actual_target: ReviewTarget | None = None
    prepared_request: PreparedReviewRequest | None = None
    foreground_evidence: list[ReviewEvidenceProjection] = Field(default_factory=list)
    subject_review_evidence: list[ReviewEvidenceProjection] = Field(default_factory=list)
    before_snapshot: BackgroundReviewStateSnapshot | None = None
    after_snapshot: BackgroundReviewStateSnapshot | None = None
    observed_changes: list[ObservedReviewChange] = Field(default_factory=list)
    outcome: ReviewOutcome | None = None
    tool_observations: list[ReviewToolObservation] = Field(default_factory=list)
    attempts: list[ReviewAttempt] = Field(default_factory=list)
    attempt_count: NonNegativeInt
    duplicate_rejected: StrictBool = False
    stale_rejected: StrictBool = False
    duration_ms: NonNegativeInt
    errors: list[BackgroundReviewExecutionError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_execution_result(self) -> "BackgroundReviewExecutionResult":
        if self.prepared_request is not None and (
            self.prepared_request.review_id != self.review_id
            or self.prepared_request.kind is not self.kind
        ):
            raise ValueError("prepared Review request identity must match result")
        if self.outcome is not None and (
            self.outcome.review_id != self.review_id
            or self.outcome.kind is not self.kind
            or self.outcome.status is not self.status
        ):
            raise ValueError("Review outcome identity and status must match result")
        ids = [item.evidence_id for item in self.foreground_evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("foreground evidence IDs must be unique")
        sequences = [item.sequence for item in self.foreground_evidence]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("foreground evidence sequence must be contiguous")
        prepared_ids = [item.evidence_id for item in self.subject_review_evidence]
        if len(prepared_ids) != len(set(prepared_ids)):
            raise ValueError("subject Review evidence IDs must be unique")
        targets = [
            (item.target_type, item.target_id) for item in self.observed_changes
        ]
        if len(targets) != len(set(targets)):
            raise ValueError("observed Review change targets must be unique")
        attempt_sequences = [item.sequence for item in self.attempts]
        if attempt_sequences != list(range(1, len(attempt_sequences) + 1)):
            raise ValueError("Review attempts must be contiguous")
        if self.attempt_count != len(self.attempts):
            raise ValueError("attempt_count must equal the number of Review attempts")
        if self.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE:
            if not self.duplicate_rejected or len(self.attempts) != 2:
                raise ValueError("duplicate Reviews require a rejected second attempt")
            if self.attempts[1].loop_executed:
                raise ValueError("duplicate Review second attempt cannot execute a loop")
        elif self.duplicate_rejected:
            raise ValueError("only duplicate lifecycle may set duplicate_rejected")
        if self.stale_rejected != (self.status is ReviewStatus.STALE):
            raise ValueError("stale_rejected must exactly reflect stale status")
        if self.status is ReviewStatus.FAILED and not self.errors:
            raise ValueError("failed Reviews require a structured execution error")
        if self.status is ReviewStatus.COMPLETED and self.actual_action is ReviewAction.REJECT:
            raise ValueError("completed Review cannot have reject action")
        if self.status in {ReviewStatus.REJECTED, ReviewStatus.STALE} and (
            self.actual_action is not ReviewAction.REJECT
        ):
            raise ValueError("rejected and stale Reviews require reject action")
        return self


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
        if self.status is ReviewStatus.COMPLETED:
            if self.error is not None:
                raise ValueError("completed review outcomes must not contain error")
            if bool(self.changes) == (self.no_op_reason is not None):
                raise ValueError(
                    "completed review outcomes require either changes or no_op_reason"
                )
        elif self.status is ReviewStatus.FAILED:
            if self.error is None:
                raise ValueError("failed review outcomes require error")
            if self.changes or self.no_op_reason is not None:
                raise ValueError(
                    "failed review outcomes cannot contain changes or no_op_reason"
                )
        elif self.status in {ReviewStatus.REJECTED, ReviewStatus.STALE}:
            if self.error is not None or self.changes or self.no_op_reason is None:
                raise ValueError(
                    "rejected and stale outcomes require only a no_op_reason"
                )
        elif self.error is not None or self.changes or self.no_op_reason is not None:
            raise ValueError(
                "pending and running outcomes cannot contain terminal results"
            )
        return self


class BackgroundReviewExpectation(ContractModel):
    review_id: Identifier | None = None
    expected_action: ReviewAction | None = None
    expected_target: ReviewTarget | None = None
    expected_target_revision: Sha256Digest | None = None
    must_change: list[ReviewTarget] = Field(default_factory=list)
    must_not_change: list[ReviewTarget] = Field(default_factory=list)
    required_evidence_kinds: list[ReviewEvidenceKind] = Field(default_factory=list)
    forbidden_evidence_kinds: list[ReviewEvidenceKind] = Field(default_factory=list)
    must_be_no_op: StrictBool = False
    protected_targets: list[ReviewTarget] = Field(default_factory=list)
    expected_stale_rejection: StrictBool = False
    allow_other_changes: StrictBool = False

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
        if self.expected_target_revision is not None and self.expected_target is None:
            raise ValueError(
                "expected_target_revision requires an expected_target"
            )
        return self
