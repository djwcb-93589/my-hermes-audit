"""Trial-local adapter over MyHermes' public Background Review surface.

This module is imported only from the isolated worker after its environment and
filesystem boundaries have been validated.  It deliberately avoids MyHermes'
global coordinator and every private ``hermes.persistence.background_review``
helper: the real public Drivers own claim/evidence semantics, while Audit owns
only the synchronous trial lifecycle and safe fact projection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from myhermes_audit.contracts import (
    BackgroundReviewExecutionError,
    BackgroundReviewExecutionResult,
    BackgroundReviewPlan,
    BackgroundReviewSkillSnapshot,
    BackgroundReviewStateSnapshot,
    ObservedReviewAction,
    ObservedReviewChange,
    PreparedReviewRequest,
    ReviewAction,
    ReviewAttempt,
    ReviewChange,
    ReviewEvidenceKind,
    ReviewEvidenceProjection,
    ReviewKind,
    ReviewLifecycle,
    ReviewMemoryItemSnapshot,
    ReviewMemorySnapshot,
    ReviewOutcome,
    ReviewStatus,
    ReviewTarget,
    ReviewToolObservation,
    SkillManagedBy,
    SkillSource,
)
from myhermes_audit.contracts.memory import MemorySnapshotPhase
from myhermes_audit.ports.background_review import BackgroundReviewEvaluationPort
from myhermes_audit.security import redact_text
from myhermes_audit.serialization import canonical_sha256


_POLICY_REJECTION_ERROR_TYPES = frozenset(
    {"permission_denied", "tool_not_authorized", "safety_blocked"}
)
_NO_EXACT_SOURCE_EDGE = "no-exact-source-edge"
_STAGE_ERROR_TYPES = {
    "claim_validation": "background_review_claim_error",
    "claim_revalidation": "background_review_claim_error",
    "foreground_evidence": "background_review_evidence_error",
    "prepare_run": "background_review_prepare_error",
    "prepared_evidence": "background_review_evidence_error",
    "snapshot_before": "background_review_snapshot_error",
    "snapshot_after": "background_review_snapshot_error",
    "state_diff": "background_review_state_diff_error",
    "review_loop": "background_review_execution_error",
    "complete_claim": "background_review_completion_error",
}

_PREPARED_EVIDENCE_LABELS = {
    "USER_MESSAGE": ReviewEvidenceKind.USER_MESSAGE,
    "TOOL_OBSERVATION": ReviewEvidenceKind.TOOL_OBSERVATION,
    "TOOL_ERROR": ReviewEvidenceKind.TOOL_ERROR,
    "ASSISTANT_DECISION — UNVERIFIED": (
        ReviewEvidenceKind.ASSISTANT_DECISION_UNVERIFIED
    ),
    "ASSISTANT_REPORT — UNVERIFIED": (
        ReviewEvidenceKind.ASSISTANT_REPORT_UNVERIFIED
    ),
}
_PREPARED_LABEL_PATTERN = "|".join(
    re.escape(item) for item in _PREPARED_EVIDENCE_LABELS
)
_PREPARED_ENTRY_PATTERN = re.compile(
    rf"^\[(?P<label>{_PREPARED_LABEL_PATTERN})\]\r?$\n"
    r"(?P<content>.*?)"
    rf"(?=^\[(?:{_PREPARED_LABEL_PATTERN}|truncated)\]\r?$|"
    r"^Foreground task \d+:\r?$|\Z)",
    re.MULTILINE | re.DOTALL,
)
_REVIEW_TOOL_CALL_ID_PATTERN = re.compile(
    r"^tool_call_id:\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\s*$",
    re.MULTILINE,
)


class BackgroundReviewAdapterError(RuntimeError):
    """A safe internal boundary error; details never enter serialized facts."""


class MyHermesBackgroundReviewAdapter(BackgroundReviewEvaluationPort):
    """Run only explicitly planned Reviews inside one isolated worker.

    The adapter is intentionally stateful only for the life of a Trial.  Its
    cache is not process-global and is keyed by suite-declared ``review_id`` so
    duplicate collection/execute calls cannot accidentally start more work.
    """

    def __init__(
        self,
        *,
        connection,
        sqlite_path: Path,
        model: str,
        model_client: object,
        tool_registry: object,
        model_max_output_tokens: int,
        memory_adapter: object | None,
        sensitive_values: tuple[str, ...],
        plans: list[BackgroundReviewPlan],
    ) -> None:
        self._connection = connection
        self._sqlite_path = Path(sqlite_path)
        self._model = model
        self._model_client = model_client
        self._tool_registry = tool_registry
        self._model_max_output_tokens = model_max_output_tokens
        self._memory_adapter = memory_adapter
        self._sensitive_values = sensitive_values
        self._plans = {plan.review_id: plan for plan in plans}
        self._results: dict[str, BackgroundReviewExecutionResult] = {}
        self._events: dict[str, tuple[str, bool, int]] = {}
        self._foreground_source_texts: dict[
            str, list[tuple[str, ReviewEvidenceKind, str, str | None]]
        ] = {}
        self._skill_id_by_name: dict[str, str] = {}
        self._review_registry, self._drivers = self._build_drivers(plans)
        self._skill_service = self._new_skill_service() if any(
            plan.kind is ReviewKind.SKILL for plan in plans
        ) else None

    @property
    def review_driver_registry(self):
        """Expose the per-Trial public registry for the inert coordinator."""

        return self._review_registry

    def make_disabled_foreground_coordinator(self, *, process_manager: object):
        """Prevent ``run_conversation`` from lazily creating the global runtime."""

        from hermes.review.runtime import (
            BackgroundReviewConfig,
            BackgroundReviewCoordinator,
            BackgroundReviewExecutor,
        )

        executor = BackgroundReviewExecutor(
            driver_registry=self._review_registry,
            config=BackgroundReviewConfig(
                max_iterations=1,
                retry_cooldown_seconds=0,
                max_concurrent_jobs=1,
                max_pending_jobs=0,
            ),
            model=self._model,
            client=self._model_client,
            db_path=str(self._sqlite_path),
            tool_registry=self._tool_registry,
        )
        # The executor is only an inert public dependency required by the
        # Coordinator.  This adapter calls neither coordinator.after_foreground_result()
        # nor executor.submit(); this adapter drives each claim synchronously.
        coordinator = BackgroundReviewCoordinator(
            driver_registry=self._review_registry,
            executor=executor,
            enabled=False,
        )
        return coordinator, executor

    def seed_skills(self, fixtures: list[object]) -> None:
        """Seed declared Skills only through MyHermes' public service API."""

        if not fixtures:
            return
        if self._skill_service is None:
            self._skill_service = self._new_skill_service()
        from hermes.skills import SkillActor

        for fixture in fixtures:
            source = getattr(fixture, "source", None)
            managed_by = getattr(fixture, "managed_by", None)
            if getattr(source, "value", source) != SkillSource.LOCAL.value:
                raise BackgroundReviewAdapterError("unsupported_skill_fixture_source")
            actor_by_manager = {
                SkillManagedBy.USER.value: SkillActor.FOREGROUND,
                SkillManagedBy.CURATOR.value: SkillActor.BACKGROUND_REVIEW,
                SkillManagedBy.SYSTEM.value: SkillActor.SYSTEM,
            }
            actor = actor_by_manager.get(getattr(managed_by, "value", managed_by))
            if actor is None:
                raise BackgroundReviewAdapterError("unsupported_skill_fixture_manager")
            result = self._skill_service.create_skill(
                fixture.name,
                actor=actor,
                body=fixture.content,
                description="myhermes-audit synthetic fixture",
            )
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise BackgroundReviewAdapterError("skill_fixture_create_failed")
            descriptor = self._skill_descriptor(fixture.name)
            if descriptor is None:
                raise BackgroundReviewAdapterError("skill_fixture_inventory_missing")
            self._skill_id_by_name[fixture.name] = fixture.skill_id
            if fixture.pinned:
                pin = self._skill_service.pin_skill(
                    fixture.name,
                    actor=SkillActor.FOREGROUND,
                    expected_revision=descriptor["revision"],
                    expected_governance_revision=descriptor["governance_revision"],
                )
                if not isinstance(pin, dict) or pin.get("ok") is not True:
                    raise BackgroundReviewAdapterError("skill_fixture_pin_failed")

    def record_foreground_and_execute(
        self,
        plan: BackgroundReviewPlan,
        *,
        logical_session_id: str,
        session_id: str,
        completed: bool,
        tool_batches: int,
    ) -> BackgroundReviewExecutionResult:
        """Record the real triggering turn, then synchronously finish its Review."""

        if plan.review_id not in self._plans:
            raise BackgroundReviewAdapterError("unknown_background_review_plan")
        # The Suite names a stable *logical* session, whereas the public
        # MyHermes Driver must receive the native session ID returned by
        # ``create_session``.  Keeping both explicit prevents the suite label
        # from being accidentally used as a database identity.
        if plan.foreground_session_id != logical_session_id:
            raise BackgroundReviewAdapterError("foreground_session_mismatch")
        self._events[plan.review_id] = (session_id, completed, tool_batches)
        self.execute(plan)
        return self.collect_result(plan.review_id)

    def snapshot(self, kind: ReviewKind) -> BackgroundReviewStateSnapshot:
        return self._snapshot(kind, phase="current")

    def execute(self, plan: BackgroundReviewPlan) -> str:
        cached = self._results.get(plan.review_id)
        if cached is not None:
            return cached.review_id
        event = self._events.get(plan.review_id)
        if event is None:
            raise BackgroundReviewAdapterError("review_execute_requires_foreground_event")
        session_id, completed, tool_batches = event
        started = time.perf_counter()
        driver = self._drivers.get(plan.kind)
        if driver is None:
            self._results[plan.review_id] = self._failed_result(
                plan,
                started=started,
                error_type="background_review_capability_error",
                stage="driver_resolution",
                message="required Background Review driver is unavailable",
            )
            return plan.review_id
        if plan.lifecycle is ReviewLifecycle.STALE_BEFORE_EXECUTE:
            # The Subject Capability Probe gates this before a Sandbox is made.
            # Keep a defensive truthful result for direct worker callers rather
            # than fabricating a stale rejection from an unchanged state.
            self._results[plan.review_id] = self._failed_result(
                plan,
                started=started,
                error_type="background_review_capability_error",
                stage="stale_claim_validation",
                message="Subject does not expose governance-bound stale claim validation",
            )
            return plan.review_id
        try:
            from hermes.review.contracts import ForegroundReviewEvent

            driver.record_progress(
                self._connection,
                ForegroundReviewEvent(
                    session_id=session_id,
                    completed=completed,
                    tool_batches=tool_batches,
                ),
            )
            claim = driver.claim_due(self._connection, session_id)
        except Exception as exc:
            self._results[plan.review_id] = self._failed_result(
                plan,
                started=started,
                error_type="background_review_claim_error",
                stage="claim_due",
                message="Background Review claim could not be created",
                exception=exc,
            )
            return plan.review_id
        if claim is None:
            self._results[plan.review_id] = self._rejected_not_due_result(
                plan,
                started=started,
            )
            return plan.review_id
        result = self._execute_claim(
            plan,
            driver=driver,
            claim=claim,
            started=started,
        )
        self._results[plan.review_id] = result
        return plan.review_id

    def collect_outcome(self, review_id: str) -> ReviewOutcome:
        result = self.collect_result(review_id)
        if result.outcome is None:
            raise BackgroundReviewAdapterError("review_outcome_unavailable")
        return result.outcome

    def collect_result(self, review_id: str) -> BackgroundReviewExecutionResult:
        try:
            return self._results[review_id]
        except KeyError as exc:
            raise BackgroundReviewAdapterError("unknown_review_result") from exc

    def mark_not_triggered(
        self,
        plan: BackgroundReviewPlan,
        *,
        reason: str = "foreground_trigger_not_reached",
    ) -> BackgroundReviewExecutionResult:
        """Record a planned Review that truthfully never reached its trigger."""

        cached = self._results.get(plan.review_id)
        if cached is not None:
            return cached
        started = time.perf_counter()
        result = self._failed_result(
            plan,
            started=started,
            error_type="background_review_trigger_error",
            stage="foreground_trigger",
            message=reason,
        )
        self._results[plan.review_id] = result
        return result

    def _build_drivers(self, plans: list[BackgroundReviewPlan]):
        from hermes.review.memory import MemoryReviewDriver
        from hermes.review.memory_store import MemoryReviewStore
        from hermes.review.registry import ReviewDriverRegistry
        from hermes.review.skill import SkillReviewDriver
        from hermes.review.skill_store import SkillReviewStore

        registry = ReviewDriverRegistry()
        drivers: dict[ReviewKind, object] = {}
        claim_ttl = float(max(plan.timeout_seconds for plan in plans) + 30)
        if any(plan.kind is ReviewKind.MEMORY for plan in plans):
            if self._memory_adapter is None:
                raise BackgroundReviewAdapterError("memory_review_adapter_unavailable")
            driver = MemoryReviewDriver(
                store=MemoryReviewStore(),
                memory_interval=1,
                claim_ttl_seconds=claim_ttl,
                retry_cooldown_seconds=0,
                max_iterations=8,
            )
            registry.register(driver)
            drivers[ReviewKind.MEMORY] = driver
        if any(plan.kind is ReviewKind.SKILL for plan in plans):
            driver = SkillReviewDriver(
                store=SkillReviewStore(),
                skill_tool_batch_interval=1,
                claim_ttl_seconds=claim_ttl,
                retry_cooldown_seconds=0,
                max_iterations=8,
            )
            registry.register(driver)
            drivers[ReviewKind.SKILL] = driver
        return registry, drivers

    @staticmethod
    def _new_skill_service():
        from hermes.skills import SkillService

        return SkillService()

    def _execute_claim(
        self,
        plan: BackgroundReviewPlan,
        *,
        driver: object,
        claim: object,
        started: float,
    ) -> BackgroundReviewExecutionResult:
        attempts: list[ReviewAttempt] = []
        errors: list[BackgroundReviewExecutionError] = []
        foreground_evidence: list[ReviewEvidenceProjection] = []
        prepared: PreparedReviewRequest | None = None
        prepared_evidence: list[ReviewEvidenceProjection] = []
        before: BackgroundReviewStateSnapshot | None = None
        after: BackgroundReviewStateSnapshot | None = None
        observed: list[ObservedReviewChange] = []
        tool_observations: list[ReviewToolObservation] = []
        status = ReviewStatus.FAILED
        actual_action = ReviewAction.NO_OP
        actual_target: ReviewTarget | None = None
        outcome: ReviewOutcome | None = None
        loop_result = None
        deadline = time.monotonic() + plan.timeout_seconds
        stage = "claim_validation"
        try:
            claim_valid = bool(driver.validate_claim(claim)) and bool(
                driver.claim_is_valid(self._connection, claim)
            )
            if not claim_valid:
                attempts.append(self._attempt(1, claim_valid=False, error_type="claim_invalid"))
                stage = "snapshot_before"
                before = self._snapshot(
                    plan.kind, phase="before", review_id=plan.review_id
                )
                stage = "snapshot_after"
                after = self._snapshot(
                    plan.kind, phase="after", review_id=plan.review_id
                )
                stage = "state_diff"
                observed = self._diff_snapshots(before, after, plan.kind)
                status = ReviewStatus.REJECTED
                actual_action = ReviewAction.REJECT
                outcome = self._outcome_rejected(plan, "claim_invalid_before_execute")
                return self._result(
                    plan,
                    status=status,
                    actual_action=actual_action,
                    actual_target=None,
                    prepared=prepared,
                    foreground_evidence=foreground_evidence,
                    prepared_evidence=prepared_evidence,
                    before=before,
                    after=after,
                    observed=observed,
                    outcome=outcome,
                    tool_observations=tool_observations,
                    attempts=attempts,
                    errors=errors,
                    started=started,
                )

            stage = "foreground_evidence"
            foreground_evidence = self._foreground_evidence(plan, claim)
            stage = "snapshot_before"
            before = self._snapshot(
                plan.kind, phase="before", review_id=plan.review_id
            )
            stage = "prepare_run"
            run_spec = driver.prepare_run(self._connection, claim)
            stage = "prepared_evidence"
            prepared_evidence = self._prepared_evidence(
                plan,
                messages=run_spec.messages,
                foreground=foreground_evidence,
            )
            prepared = PreparedReviewRequest(
                review_id=plan.review_id,
                kind=plan.kind,
                evidence=prepared_evidence,
                message_count=len(run_spec.messages),
            )
            stage = "claim_revalidation"
            if not driver.claim_is_valid(self._connection, claim):
                attempts.append(self._attempt(1, claim_valid=False, error_type="claim_invalid"))
                stage = "snapshot_after"
                after = self._snapshot(
                    plan.kind, phase="after", review_id=plan.review_id
                )
                stage = "state_diff"
                observed = self._diff_snapshots(before, after, plan.kind)
                status = ReviewStatus.REJECTED
                actual_action = ReviewAction.REJECT
                outcome = self._outcome_rejected(plan, "claim_invalid_after_prepare")
            else:
                stage = "review_loop"
                loop_result, tool_observations, model_call_count = self._run_loop(
                    plan,
                    driver=driver,
                    claim=claim,
                    run_spec=run_spec,
                    deadline=deadline,
                )
                stage = "snapshot_after"
                after = self._snapshot(
                    plan.kind, phase="after", review_id=plan.review_id
                )
                stage = "state_diff"
                observed = self._diff_snapshots(before, after, plan.kind)
                actual_action, actual_target = self._actual_action(observed)
                attempts.append(
                    ReviewAttempt(
                        sequence=1,
                        claim_valid=True,
                        loop_executed=True,
                        model_call_count=model_call_count,
                        tool_call_count=len(tool_observations),
                        state_change_count=sum(
                            item.action is not ObservedReviewAction.UNCHANGED
                            for item in observed
                        ),
                        error_type=self._result_error_type(loop_result),
                    )
                )
                if time.monotonic() >= deadline:
                    status = ReviewStatus.FAILED
                    errors.append(
                        self._error(
                            "background_review_timeout",
                            "review_loop",
                            "Background Review exceeded its declared timeout",
                        )
                    )
                    self._fail_claim_if_valid(driver, claim, "review_timeout")
                    outcome = self._outcome_failed(
                        plan,
                        error_type="background_review_timeout",
                        message="Background Review exceeded its declared timeout",
                    )
                elif bool(getattr(loop_result, "ok", False)) and (
                    getattr(loop_result, "status", None) == "completed"
                ):
                    stage = "complete_claim"
                    if driver.complete(self._connection, claim):
                        status = ReviewStatus.COMPLETED
                        outcome = self._outcome_completed(plan, observed)
                    else:
                        # ``complete`` may lose a lease after the loop has
                        # finished.  If it has not, release it best-effort so
                        # a later attempt cannot inherit an abandoned claim.
                        self._fail_claim_if_valid(
                            driver,
                            claim,
                            "review_completion_lost_claim",
                        )
                        status = ReviewStatus.FAILED
                        errors.append(
                            self._error(
                                "background_review_completion_error",
                                "complete_claim",
                                "Background Review completion lost its claim",
                            )
                        )
                        outcome = self._outcome_failed(
                            plan,
                            error_type="background_review_completion_error",
                            message="Background Review completion lost its claim",
                        )
                else:
                    subject_error_type = self._result_error_type(loop_result)
                    if subject_error_type in _POLICY_REJECTION_ERROR_TYPES:
                        status = ReviewStatus.REJECTED
                        actual_action = ReviewAction.REJECT
                        self._fail_claim_if_valid(
                            driver,
                            claim,
                            "review_policy_rejected",
                        )
                        outcome = self._outcome_rejected(plan, "subject_review_policy_rejected")
                    else:
                        error_type = self._loop_failure_error_type(loop_result)
                        status = ReviewStatus.FAILED
                        self._fail_claim_if_valid(
                            driver,
                            claim,
                            f"review_failed:{getattr(loop_result, 'status', 'unknown')}:{subject_error_type}",
                        )
                        errors.append(
                            self._error(
                                error_type,
                                "review_loop",
                                "Background Review loop did not complete",
                                exception_type=subject_error_type,
                            )
                        )
                        outcome = self._outcome_failed(
                            plan,
                            error_type=error_type,
                            message="Background Review loop did not complete",
                        )
        except Exception as exc:
            error_type = _STAGE_ERROR_TYPES.get(
                stage,
                "background_review_execution_error",
            )
            errors.append(
                self._error(
                    error_type,
                    stage,
                    "Background Review execution failed safely",
                    exception=exc,
                )
            )
            self._fail_claim_if_valid(driver, claim, "review_execution_failed")
            if before is None:
                try:
                    before = self._snapshot(
                        plan.kind, phase="before", review_id=plan.review_id
                    )
                except Exception:
                    pass
            try:
                after = self._snapshot(
                    plan.kind, phase="after", review_id=plan.review_id
                )
            except Exception:
                pass
            if before is not None and after is not None:
                try:
                    observed = self._diff_snapshots(before, after, plan.kind)
                except Exception:
                    # Preserve the primary stage failure even when the
                    # best-effort recovery projection cannot be calculated.
                    observed = []
            status = ReviewStatus.FAILED
            outcome = self._outcome_failed(
                plan,
                error_type=error_type,
                message="Background Review execution failed safely",
            )
            actual_action, actual_target = self._actual_action(observed)
            if actual_action is ReviewAction.REJECT:
                actual_action = ReviewAction.NO_OP

        if plan.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE:
            try:
                duplicate_valid = bool(driver.claim_is_valid(self._connection, claim))
            except Exception as exc:
                self._fail_claim_if_valid(
                    driver,
                    claim,
                    "duplicate_claim_validation_failed",
                )
                attempts.append(
                    self._attempt(
                        2,
                        claim_valid=False,
                        error_type="duplicate_claim_validation_failed",
                    )
                )
                errors.append(
                    self._error(
                        "background_review_duplicate_error",
                        "duplicate_execute",
                        "duplicate Review claim could not be validated",
                        exception=exc,
                    )
                )
                status = ReviewStatus.FAILED
                outcome = self._outcome_failed(
                    plan,
                    error_type="background_review_duplicate_error",
                    message="duplicate Review claim could not be validated",
                )
                duplicate_valid = False
            else:
                attempts.append(
                    self._attempt(
                        2,
                        claim_valid=duplicate_valid,
                        error_type=(
                            None if not duplicate_valid else "claim_still_valid"
                        ),
                    )
                )
                # A completed claim must be invalid before a duplicate execute.
                # If it is not, do not rerun it; release it best-effort and
                # record the unexpected protocol fact.
                if duplicate_valid:
                    self._fail_claim_if_valid(
                        driver,
                        claim,
                        "duplicate_claim_still_valid",
                    )
                    errors.append(
                        self._error(
                            "background_review_duplicate_error",
                            "duplicate_execute",
                            "duplicate Review claim remained valid without rerun",
                        )
                    )
                    status = ReviewStatus.FAILED
                    outcome = self._outcome_failed(
                        plan,
                        error_type="background_review_duplicate_error",
                        message="duplicate Review claim remained valid without rerun",
                    )

        return self._result(
            plan,
            status=status,
            actual_action=actual_action,
            actual_target=actual_target,
            prepared=prepared,
            foreground_evidence=foreground_evidence,
            prepared_evidence=prepared_evidence,
            before=before,
            after=after,
            observed=observed,
            outcome=outcome,
            tool_observations=tool_observations,
            attempts=attempts,
            errors=errors,
            started=started,
        )

    def _run_loop(self, plan, *, driver, claim, run_spec, deadline: float):
        from hermes.hooks import SyncHookRegistry
        from hermes.persistence.observation import configure_sqlite_observation_sink
        from hermes.review.loop import ReviewAgentLoop

        # Resolve the Driver-declared policy first, then give the actual loop a
        # new registry containing only those public entries.  The full registry
        # is retained solely as a registration source for the inert foreground
        # coordinator; it is never handed to an executing Review loop.
        review_registry, resolution = self._restricted_review_registry(
            run_spec.tool_policy
        )
        if not resolution.definitions:
            raise BackgroundReviewAdapterError("review_tools_unavailable")
        hooks = SyncHookRegistry()
        configure_sqlite_observation_sink(
            hooks,
            self._sqlite_path,
            hook_id_prefix=_stable_id("review-hook", plan.review_id),
        )
        loop = ReviewAgentLoop(
            review_messages=run_spec.messages,
            review_instruction=run_spec.instruction,
            allowed_tool_names=resolution.allowed_tool_names,
            model=self._model,
            max_iterations=run_spec.max_iterations,
            tools=list(resolution.definitions),
            system_prompt=run_spec.system_prompt,
            registry=review_registry,
            client=self._model_client,
            session_key=claim.session_id,
            model_kwargs={"max_tokens": self._model_max_output_tokens},
            cancel_checker=lambda: (
                time.monotonic() >= deadline
                or not driver.claim_is_valid(self._connection, claim)
            ),
            tool_context=dict(run_spec.tool_context),
            hook_registry=hooks,
        )
        result = loop.run("")
        observations, model_count = self._review_tool_observations(
            review_id=plan.review_id,
            run_id=getattr(loop, "run_id", None),
        )
        return result, observations, model_count

    def _restricted_review_registry(self, tool_policy):
        """Copy only policy-resolved public ToolRegistry entries for one loop."""

        from hermes.tools import ToolRegistry

        source_resolution = self._tool_registry.resolve(tool_policy)
        restricted = ToolRegistry()
        for tool_name in sorted(source_resolution.allowed_tool_names):
            entry = self._tool_registry.get_entry(tool_name)
            if entry is None:
                raise BackgroundReviewAdapterError("review_tool_resolution_lost_entry")
            restricted.register(
                entry.name,
                entry.toolset,
                entry.schema,
                entry.handler,
                execution_environments=entry.execution_environments,
                unattended_allowed=entry.unattended_allowed,
                required_trusted_context=entry.required_trusted_context,
                approval_mode=entry.approval_mode,
                risk_level=entry.risk_level,
                default_enabled_environments=entry.default_enabled_environments,
                retry_safe=entry.retry_safe,
                unknown_on_crash=entry.unknown_on_crash,
                status_check=entry.status_check,
                supports_cancellation=entry.supports_cancellation,
            )
        # A second resolution verifies that the copied entries remain exactly
        # constrained by the Driver policy and supplies model tool definitions
        # that correspond to the registry actually used for dispatch.
        return restricted, restricted.resolve(tool_policy)

    def _review_tool_observations(self, *, review_id: str, run_id: str | None):
        if not run_id:
            return [], 0
        from myhermes_audit.integrations.myhermes.observation_reader import (
            read_observations,
        )

        bundle = read_observations(
            self._sqlite_path,
            run_durations={},
            include_run_ids=frozenset({run_id}),
        )
        items = [
            ReviewToolObservation(
                observation_id=_stable_id("review-tool", review_id, str(index)),
                tool_name=item.tool_name,
                status=item.status,
                success=item.success,
                error_type=item.error_type,
                duration_ms=item.duration_ms,
            )
            for index, item in enumerate(bundle.tool_calls, start=1)
        ]
        return items, len(bundle.model_calls)

    def _foreground_evidence(self, plan: BackgroundReviewPlan, claim: object):
        from hermes.persistence.core import get_session_messages_in_id_range

        payload = getattr(claim, "payload", {})
        after = payload.get("message_after")
        upto = payload.get("message_upto")
        if type(after) is not int or type(upto) is not int:
            raise BackgroundReviewAdapterError("review_claim_window_missing")
        messages = get_session_messages_in_id_range(
            self._connection,
            claim.session_id,
            after_message_id=after,
            upto_message_id=upto,
        )
        evidence: list[ReviewEvidenceProjection] = []
        source_texts: list[tuple[str, ReviewEvidenceKind, str, str | None]] = []
        for index, message in enumerate(messages, start=1):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            kind = self._foreground_kind(message)
            if kind is None:
                continue
            content = message.get("content", "")
            text = content if isinstance(content, str) else str(content)
            safe_text = redact_text(text, self._sensitive_values)
            evidence_id = _stable_id(
                "foreground-evidence", plan.review_id, str(index)
            )
            tool_call_id = (
                _safe_tool_call_id(
                    message.get("tool_call_id"),
                    fallback=_stable_id("foreground-tool", plan.review_id, str(index)),
                )
                if role == "tool"
                else None
            )
            evidence.append(
                ReviewEvidenceProjection(
                    evidence_id=evidence_id,
                    kind=kind,
                    content_sha256=_sha256(safe_text),
                    content_length=len(safe_text),
                    sequence=len(evidence) + 1,
                    source_turn_number=plan.trigger_after_turn,
                    source_tool_call_id=tool_call_id,
                )
            )
            source_texts.append((evidence_id, kind, safe_text, tool_call_id))
        self._foreground_source_texts[plan.review_id] = source_texts
        return evidence

    @staticmethod
    def _foreground_kind(message: dict) -> ReviewEvidenceKind | None:
        role = message.get("role")
        if role == "user":
            return ReviewEvidenceKind.USER_MESSAGE
        if role == "assistant":
            return (
                ReviewEvidenceKind.ASSISTANT_DECISION_UNVERIFIED
                if message.get("tool_calls")
                else ReviewEvidenceKind.ASSISTANT_REPORT_UNVERIFIED
            )
        if role == "tool":
            content = message.get("content", "")
            try:
                payload = json.loads(content) if isinstance(content, str) else {}
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict) and (
                payload.get("ok") is False
                or bool(payload.get("error_type"))
                or "error" in payload
            ):
                return ReviewEvidenceKind.TOOL_ERROR
            return ReviewEvidenceKind.TOOL_OBSERVATION
        return None

    def _prepared_evidence(self, plan, *, messages: list[dict], foreground):
        _ = foreground
        source_texts = self._foreground_source_texts.get(plan.review_id, [])
        used_source_ids: set[str] = set()
        prepared_count_by_kind: dict[ReviewEvidenceKind, int] = {}
        result: list[ReviewEvidenceProjection] = []
        for index, message in enumerate(messages, start=1):
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            text = content if isinstance(content, str) else str(content)
            # MyHermes renders the actual selected entries as bracketed source
            # labels.  Its fixed rule prefix repeats the same words, so never
            # scan arbitrary prompt text for labels: only these rendered entry
            # blocks constitute prepared evidence.
            for label, entry_text in _prepared_entries(text):
                kind = _PREPARED_EVIDENCE_LABELS[label]
                safe_text = redact_text(entry_text, self._sensitive_values)
                source_count = sum(
                    source_kind is kind
                    for _source_id, source_kind, _source_text, _tool_call_id
                    in source_texts
                )
                if (
                    source_count == 0
                    or prepared_count_by_kind.get(kind, 0) >= source_count
                ):
                    # A label has evidentiary meaning only if it corresponds to
                    # a real fact in the public claim window; cap it at the
                    # window's source count so embedded bracket text cannot
                    # manufacture additional Driver entries.
                    continue
                source_id = _strict_prepared_source_id(
                    kind,
                    safe_text,
                    source_texts,
                    used_source_ids,
                )
                if source_id is not None:
                    used_source_ids.add(source_id)
                source_identity = (
                    source_id
                    if source_id is not None
                    else f"{_NO_EXACT_SOURCE_EDGE}:{len(result) + 1}"
                )
                prepared_count_by_kind[kind] = (
                    prepared_count_by_kind.get(kind, 0) + 1
                )
                result.append(
                    ReviewEvidenceProjection(
                        evidence_id=_stable_id(
                            "prepared-evidence",
                            plan.review_id,
                            str(index),
                            source_identity,
                            kind.value,
                        ),
                        kind=kind,
                        content_sha256=_sha256(safe_text),
                        content_length=len(safe_text),
                        sequence=len(result) + 1,
                        # Driver compaction may deliberately deduplicate or
                        # transform a tool fact.  Preserve an exact evidence
                        # edge only when public rendered text proves it; the
                        # otherwise truthful relation is the real foreground
                        # turn/window, never a guessed ordinal mapping.
                        source_turn_number=(
                            plan.trigger_after_turn
                            if source_id is None
                            else None
                        ),
                        source_evidence_id=source_id,
                    )
                )
        return result

    def _snapshot(
        self,
        kind: ReviewKind,
        *,
        phase: str,
        review_id: str | None = None,
    ) -> BackgroundReviewStateSnapshot:
        memory = None
        skills: list[BackgroundReviewSkillSnapshot] = []
        if kind is ReviewKind.MEMORY:
            if self._memory_adapter is None:
                raise BackgroundReviewAdapterError("memory_snapshot_unavailable")
            memory_phase = (
                MemorySnapshotPhase.BEFORE_CONVERSATION
                if phase == "before"
                else MemorySnapshotPhase.AFTER_CONVERSATION
            )
            raw_memory = asyncio.run(
                self._memory_adapter.snapshot(phase=memory_phase)
            )
            memory = self._memory_snapshot_projection(raw_memory)
        if kind is ReviewKind.SKILL:
            skills = self._skill_snapshot()
        return BackgroundReviewStateSnapshot(
            snapshot_id=_stable_id(
                "review-snapshot", review_id or "current", kind.value, phase
            ),
            captured_at=datetime.now(timezone.utc),
            memory=memory,
            skills=skills,
        )

    @staticmethod
    def _memory_snapshot_projection(raw_memory) -> ReviewMemorySnapshot:
        """Hash public Memory state before a Review Artifact can be published."""

        return ReviewMemorySnapshot(
            snapshot_id=raw_memory.snapshot_id,
            phase=raw_memory.phase,
            strategy=raw_memory.strategy,
            provider=raw_memory.provider,
            captured_at=raw_memory.captured_at,
            items=[
                ReviewMemoryItemSnapshot(
                    memory_id=item.memory_id,
                    kind=item.kind,
                    content_sha256=_sha256(item.content),
                    content_length=len(item.content),
                    state_sha256=canonical_sha256(
                        item.model_dump(mode="json", exclude={"schema_version"})
                    ),
                )
                for item in raw_memory.items
            ],
        )

    def _skill_snapshot(self) -> list[BackgroundReviewSkillSnapshot]:
        if self._skill_service is None:
            raise BackgroundReviewAdapterError("skill_snapshot_unavailable")
        response = self._skill_service.list_skills()
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise BackgroundReviewAdapterError("skill_inventory_failed")
        raw_skills = response.get("skills")
        if not isinstance(raw_skills, list):
            raise BackgroundReviewAdapterError("skill_inventory_invalid")
        snapshots: list[BackgroundReviewSkillSnapshot] = []
        for raw in raw_skills:
            if not isinstance(raw, dict):
                raise BackgroundReviewAdapterError("skill_inventory_invalid")
            name = raw.get("name")
            native_id = raw.get("skill_id")
            if not isinstance(name, str) or not isinstance(native_id, str):
                raise BackgroundReviewAdapterError("skill_inventory_invalid")
            snapshots.append(
                BackgroundReviewSkillSnapshot(
                    skill_id=self._skill_id_by_name.get(name, native_id),
                    name_sha256=_sha256(name),
                    name_length=len(name),
                    source=SkillSource(raw["source"]),
                    managed_by=SkillManagedBy(raw["managed_by"]),
                    pinned=raw["pinned"],
                    revision=raw["revision"],
                    governance_revision=raw["governance_revision"],
                )
            )
        return sorted(snapshots, key=lambda item: item.skill_id)

    def _skill_descriptor(self, name: str) -> dict[str, Any] | None:
        if self._skill_service is None:
            return None
        response = self._skill_service.list_skills()
        if not isinstance(response, dict) or response.get("ok") is not True:
            return None
        for descriptor in response.get("skills", []):
            if isinstance(descriptor, dict) and descriptor.get("name") == name:
                return descriptor
        return None

    @staticmethod
    def _diff_snapshots(before, after, kind):
        if kind is ReviewKind.MEMORY:
            before_items = (
                {} if before.memory is None else {item.memory_id: item for item in before.memory.items}
            )
            after_items = (
                {} if after.memory is None else {item.memory_id: item for item in after.memory.items}
            )
            return _diff_maps(
                before_items,
                after_items,
                target_type="memory",
                value_hash=lambda item: canonical_sha256(
                    {
                        "kind": item.kind.value,
                        "content_sha256": item.content_sha256,
                        "content_length": item.content_length,
                        "state_sha256": item.state_sha256,
                    }
                ),
                governance_hash=lambda _item: None,
            )
        before_skills = {item.skill_id: item for item in before.skills}
        after_skills = {item.skill_id: item for item in after.skills}
        return _diff_maps(
            before_skills,
            after_skills,
            target_type="skill",
            value_hash=lambda item: canonical_sha256(
                {
                    "name_sha256": item.name_sha256,
                    "name_length": item.name_length,
                    "source": item.source.value,
                    "managed_by": item.managed_by.value,
                    "pinned": item.pinned,
                    "revision": item.revision,
                }
            ),
            governance_hash=lambda item: item.governance_revision,
        )

    @staticmethod
    def _actual_action(observed):
        changed = [
            item for item in observed if item.action is not ObservedReviewAction.UNCHANGED
        ]
        if not changed:
            return ReviewAction.NO_OP, None
        mapping = {
            ObservedReviewAction.CREATE: ReviewAction.CREATE,
            ObservedReviewAction.REMOVE: ReviewAction.REMOVE,
            ObservedReviewAction.UPDATE: ReviewAction.UPDATE,
            ObservedReviewAction.REPLACE: ReviewAction.REPLACE,
        }
        actions = {mapping[item.action] for item in changed}
        action = next(iter(actions)) if len(actions) == 1 else ReviewAction.REPLACE
        target = (
            ReviewTarget(target_type=changed[0].target_type, target_id=changed[0].target_id)
            if len(changed) == 1
            else None
        )
        return action, target

    def _result(
        self,
        plan,
        *,
        status,
        actual_action,
        actual_target,
        prepared,
        foreground_evidence,
        prepared_evidence,
        before,
        after,
        observed,
        outcome,
        tool_observations,
        attempts,
        errors,
        started,
    ):
        normalized_attempts = list(attempts)
        if plan.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE:
            if len(normalized_attempts) > 2:
                raise BackgroundReviewAdapterError("duplicate_review_attempt_overflow")
            if not normalized_attempts:
                normalized_attempts.append(
                    self._attempt(
                        1,
                        claim_valid=False,
                        error_type="duplicate_first_attempt_unavailable",
                    )
                )
            if len(normalized_attempts) == 1:
                # A setup/claim failure can prevent the first run, but the
                # The stable result still records that no second execution was
                # attempted.  It creates no new identity or side effect.
                normalized_attempts.append(
                    self._attempt(
                        2,
                        claim_valid=False,
                        error_type="duplicate_execute_rejected",
                    )
                )
        return BackgroundReviewExecutionResult(
            review_id=plan.review_id,
            kind=plan.kind,
            lifecycle=plan.lifecycle,
            status=status,
            actual_action=actual_action,
            actual_target=actual_target,
            prepared_request=prepared,
            foreground_evidence=foreground_evidence,
            subject_review_evidence=prepared_evidence,
            before_snapshot=before,
            after_snapshot=after,
            observed_changes=observed,
            outcome=outcome,
            tool_observations=tool_observations,
            attempts=normalized_attempts,
            attempt_count=len(normalized_attempts),
            duplicate_rejected=(plan.lifecycle is ReviewLifecycle.DUPLICATE_EXECUTE),
            stale_rejected=(status is ReviewStatus.STALE),
            duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            errors=errors,
        )

    def _failed_result(self, plan, *, started, error_type, stage, message, exception=None):
        before = after = None
        observed = []
        try:
            before = self._snapshot(
                plan.kind, phase="before", review_id=plan.review_id
            )
            after = self._snapshot(
                plan.kind, phase="after", review_id=plan.review_id
            )
            observed = self._diff_snapshots(before, after, plan.kind)
        except Exception:
            pass
        return self._result(
            plan,
            status=ReviewStatus.FAILED,
            actual_action=ReviewAction.NO_OP,
            actual_target=None,
            prepared=None,
            foreground_evidence=[],
            prepared_evidence=[],
            before=before,
            after=after,
            observed=observed,
            outcome=self._outcome_failed(plan, error_type=error_type, message=message),
            tool_observations=[],
            attempts=[self._attempt(1, claim_valid=False, error_type=error_type)],
            errors=[self._error(error_type, stage, message, exception=exception)],
            started=started,
        )

    def _rejected_not_due_result(self, plan, *, started):
        before = self._snapshot(
            plan.kind, phase="before", review_id=plan.review_id
        )
        after = self._snapshot(
            plan.kind, phase="after", review_id=plan.review_id
        )
        return self._result(
            plan,
            status=ReviewStatus.REJECTED,
            actual_action=ReviewAction.REJECT,
            actual_target=None,
            prepared=None,
            foreground_evidence=[],
            prepared_evidence=[],
            before=before,
            after=after,
            observed=self._diff_snapshots(before, after, plan.kind),
            outcome=self._outcome_rejected(plan, "review_not_due_after_foreground"),
            tool_observations=[],
            attempts=[self._attempt(1, claim_valid=False, error_type="review_not_due")],
            errors=[],
            started=started,
        )

    @staticmethod
    def _attempt(sequence, *, claim_valid, error_type=None):
        return ReviewAttempt(
            sequence=sequence,
            claim_valid=claim_valid,
            loop_executed=False,
            model_call_count=0,
            tool_call_count=0,
            state_change_count=0,
            error_type=error_type,
        )

    @staticmethod
    def _error(error_type, stage, message, *, exception=None, exception_type=None):
        return BackgroundReviewExecutionError(
            error_type=_safe_identifier(error_type),
            stage=_safe_identifier(stage),
            message=message,
            retryable=False,
            exception_type=(
                _safe_identifier(exception_type)
                if exception_type is not None
                else (None if exception is None else _safe_identifier(type(exception).__name__))
            ),
        )

    @staticmethod
    def _outcome_completed(plan, observed):
        changes = [
            ReviewChange(
                action={
                    ObservedReviewAction.CREATE: ReviewAction.CREATE,
                    ObservedReviewAction.UPDATE: ReviewAction.UPDATE,
                    ObservedReviewAction.REPLACE: ReviewAction.REPLACE,
                    ObservedReviewAction.REMOVE: ReviewAction.REMOVE,
                }[item.action],
                target_type=item.target_type,
                target_id=item.target_id,
                before_hash=item.before_hash,
                after_hash=item.after_hash,
                before_governance_revision=item.before_governance_revision,
                after_governance_revision=item.after_governance_revision,
                reason="observed_live_state_change",
            )
            for item in observed
            if item.action is not ObservedReviewAction.UNCHANGED
        ]
        return ReviewOutcome(
            review_id=plan.review_id,
            kind=plan.kind,
            status=ReviewStatus.COMPLETED,
            changes=changes,
            no_op_reason=(None if changes else "no_observed_state_change"),
        )

    @staticmethod
    def _outcome_failed(plan, *, error_type, message):
        from myhermes_audit.contracts.background_review import ReviewError

        return ReviewOutcome(
            review_id=plan.review_id,
            kind=plan.kind,
            status=ReviewStatus.FAILED,
            error=ReviewError(error_type=_safe_identifier(error_type), message=message),
        )

    @staticmethod
    def _outcome_rejected(plan, reason):
        return ReviewOutcome(
            review_id=plan.review_id,
            kind=plan.kind,
            status=ReviewStatus.REJECTED,
            no_op_reason=reason,
        )

    def _fail_claim_if_valid(self, driver, claim, error: str) -> None:
        try:
            if driver.claim_is_valid(self._connection, claim):
                driver.fail(self._connection, claim, error)
        except Exception:
            # A release failure is represented by the primary execution error;
            # never leak a token or raw Subject exception while handling it.
            return

    @staticmethod
    def _result_error_type(result) -> str:
        value = getattr(result, "error_type", None)
        return _safe_identifier(value if isinstance(value, str) else "unknown")

    @classmethod
    def _loop_failure_error_type(cls, result) -> str:
        if (
            getattr(result, "status", None) == "tool_error"
            or cls._result_error_type(result) == "tool_error"
        ):
            return "background_review_tool_error"
        return "background_review_execution_error"


def _prepared_entries(text: str) -> tuple[tuple[str, str], ...]:
    """Return only the Driver-rendered entries, never its rules/prompt text."""

    return tuple(
        (match.group("label"), match.group("content").rstrip("\r\n"))
        for match in _PREPARED_ENTRY_PATTERN.finditer(text)
        if match.group("content").strip()
    )


def _strict_prepared_source_id(
    kind: ReviewEvidenceKind,
    entry_text: str,
    sources: list[tuple[str, ReviewEvidenceKind, str, str | None]],
    used_source_ids: set[str],
) -> str | None:
    """Return a provenance edge only when the public rendered text proves it."""

    candidates = [
        item
        for item in sources
        if item[1] is kind and item[0] not in used_source_ids
    ]
    if not candidates:
        return None
    tool_call_match = _REVIEW_TOOL_CALL_ID_PATTERN.search(entry_text)
    if tool_call_match is not None:
        tool_call_id = tool_call_match.group(1)
        matches = [item for item in candidates if item[3] == tool_call_id]
        return matches[0][0] if len(matches) == 1 else None
    normalized_entry = entry_text.strip()
    matches = [
        item
        for item in candidates
        if item[2].strip() == normalized_entry
    ]
    return matches[0][0] if len(matches) == 1 else None


def _diff_maps(before, after, *, target_type, value_hash, governance_hash):
    changes: list[ObservedReviewChange] = []
    for target_id in sorted(set(before) | set(after)):
        old = before.get(target_id)
        new = after.get(target_id)
        old_hash = None if old is None else value_hash(old)
        new_hash = None if new is None else value_hash(new)
        old_governance = None if old is None else governance_hash(old)
        new_governance = None if new is None else governance_hash(new)
        if old is None:
            action = ObservedReviewAction.CREATE
        elif new is None:
            action = ObservedReviewAction.REMOVE
        elif old_hash == new_hash and old_governance == new_governance:
            action = ObservedReviewAction.UNCHANGED
        elif old_hash == new_hash:
            action = ObservedReviewAction.UPDATE
        else:
            action = ObservedReviewAction.REPLACE
        changes.append(
            ObservedReviewChange(
                action=action,
                target_type=target_type,
                target_id=target_id,
                before_hash=old_hash,
                after_hash=new_hash,
                before_governance_revision=old_governance,
                after_governance_revision=new_governance,
            )
        )
    return changes


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _safe_identifier(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "._:-" else "_"
        for character in value.strip()
    )[:128]
    if not normalized or not normalized[0].isalnum():
        return "error"
    return normalized


def _safe_tool_call_id(value: object, *, fallback: str) -> str:
    """Use a public persisted tool-call relation when it is safe to expose."""

    if isinstance(value, str):
        normalized = _safe_identifier(value)
        if normalized != "error":
            return normalized
    return fallback


__all__ = ("MyHermesBackgroundReviewAdapter",)
