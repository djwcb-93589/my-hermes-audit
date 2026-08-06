"""Subject-neutral, synchronous Background Review port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from myhermes_audit.contracts.background_review import (
    BackgroundReviewExecutionResult,
    BackgroundReviewPlan,
    BackgroundReviewStateSnapshot,
    ReviewKind,
    ReviewOutcome,
)
from myhermes_audit.contracts.common import Identifier


@runtime_checkable
class BackgroundReviewEvaluationPort(Protocol):
    """Trial-local Background Review execution without a queue or daemon.

    Review work must be complete before its isolated worker exits. An
    implementation caches by the stable plan ``review_id``: collecting a result
    is read-only and must never invoke a model, tool, or state write again.
    """

    def snapshot(self, kind: ReviewKind) -> BackgroundReviewStateSnapshot:
        """Capture an algorithm-neutral live state projection."""
        ...

    def execute(self, plan: BackgroundReviewPlan) -> Identifier:
        """Synchronously execute and cache one planned Review."""
        ...

    def collect_outcome(self, review_id: Identifier) -> ReviewOutcome:
        """Return the cached normalized outcome for a completed Review."""
        ...

    def collect_result(self, review_id: Identifier) -> BackgroundReviewExecutionResult:
        """Return the full cached execution fact without rerunning it."""
        ...
