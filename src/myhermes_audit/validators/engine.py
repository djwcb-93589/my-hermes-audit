"""P1 evaluator planning and deterministic validator dispatch."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field, StrictBool, model_validator

from myhermes_audit.contracts import (
    AuditCase,
    CheckpointResult,
    DistortionResult,
    FactRetentionResult,
    JudgeExpectation,
    MetricResult,
    RequiredFactLossResult,
)
from myhermes_audit.contracts.common import ContractModel, Identifier, NonEmptyText
from myhermes_audit.contracts.suite import (
    EvaluatorKind,
    EvaluatorSpec,
    ToolTrajectoryExpectation,
)
from myhermes_audit.errors import UnsupportedCaseError
from myhermes_audit.validators.base import (
    ValidationContext,
    validator_error_metric,
)
from myhermes_audit.validators.ablation import evaluate_ablation
from myhermes_audit.validators.background_review import (
    evaluate_background_review_expectation,
)
from myhermes_audit.validators.file import FileValidator
from myhermes_audit.validators.json_file import JsonFileValidator
from myhermes_audit.validators.memory import (
    evaluate_memory_expectation,
    evaluate_memory_state_expectation,
)
from myhermes_audit.validators.text import TextValidator
from myhermes_audit.validators.tool_trajectory import ToolTrajectoryValidator
from myhermes_audit.validators.scenario import evaluate_scenario_plan


_DETERMINISTIC_GROUPS = frozenset({"all", "files", "texts", "json_values"})
_RETRIEVAL_GATE_METRIC_TYPES = frozenset(
    {"required_evidence", "recall_at_k", "mrr"}
)
_MEMORY_STATE_GATE_METRIC_TYPES = frozenset({"memory_state_gate"})


class EvaluatorValidationResult(ContractModel):
    evaluator_id: Identifier
    evaluator_kind: EvaluatorKind
    required: StrictBool
    passed: StrictBool
    metric_names: list[NonEmptyText] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_metric_names(self) -> "EvaluatorValidationResult":
        if len(self.metric_names) != len(set(self.metric_names)):
            raise ValueError("metric_names must be unique")
        return self


class ValidatorResultsArtifact(ContractModel):
    trial_id: Identifier
    case_id: Identifier
    evaluator_results: list[EvaluatorValidationResult] = Field(default_factory=list)
    metrics: list[MetricResult] = Field(default_factory=list)
    checkpoint_results: list[CheckpointResult] = Field(default_factory=list)
    fact_retention_results: list[FactRetentionResult] = Field(default_factory=list)
    required_fact_loss: RequiredFactLossResult | None = None
    distortion_results: list[DistortionResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_results(self) -> "ValidatorResultsArtifact":
        evaluator_ids = [item.evaluator_id for item in self.evaluator_results]
        if len(evaluator_ids) != len(set(evaluator_ids)):
            raise ValueError("evaluator_results must have unique evaluator_id values")
        metric_names = [item.metric_name for item in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("validator metrics must have unique metric_name values")
        declared = {
            name
            for evaluator in self.evaluator_results
            for name in evaluator.metric_names
        }
        if declared != set(metric_names):
            raise ValueError("evaluator metric_names must cover the metrics exactly")
        checkpoint_ids = [item.checkpoint_id for item in self.checkpoint_results]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("checkpoint results must have unique checkpoint IDs")
        fact_ids = [item.fact_id for item in self.fact_retention_results]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact retention results must have unique fact IDs")
        distortion_keys = [
            (item.fact_id, item.distortion_type)
            for item in self.distortion_results
        ]
        if len(distortion_keys) != len(set(distortion_keys)):
            raise ValueError("distortion results must be unique per fact and type")
        has_compression = any(
            item.evaluator_kind is EvaluatorKind.COMPRESSION
            for item in self.evaluator_results
        )
        has_structured_p4 = any(
            (
                self.checkpoint_results,
                self.fact_retention_results,
                self.required_fact_loss is not None,
                self.distortion_results,
            )
        )
        if has_structured_p4 != has_compression:
            raise ValueError(
                "structured P4 results require exactly one compression evaluator"
            )
        return self

    @property
    def task_hard_gates_passed(self) -> bool:
        """Return whether every required, local non-Judge evaluator passed."""

        return all(
            item.passed
            for item in self.evaluator_results
            if item.required and item.evaluator_kind is not EvaluatorKind.LLM_JUDGE
        )

    @property
    def retrieval_hard_gates_passed(self) -> bool | None:
        """Aggregate required retrieval-evidence gates independently."""

        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.RETRIEVAL,
            metric_types=_RETRIEVAL_GATE_METRIC_TYPES,
        )

    @property
    def memory_state_hard_gates_passed(self) -> bool | None:
        """Aggregate required Memory-state gates independently."""

        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.RETRIEVAL,
            metric_types=_MEMORY_STATE_GATE_METRIC_TYPES,
        )

    @property
    def required_fact_hard_gates_passed(self) -> bool | None:
        """Aggregate required P4 fact and checkpoint gates independently."""

        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.COMPRESSION,
            metric_types=frozenset(
                {"required_fact_retention", "checkpoint", "distortion"}
            ),
        )

    @property
    def final_answer_hard_gates_passed(self) -> bool | None:
        """Aggregate required deterministic final-answer gates independently."""

        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.DETERMINISTIC,
        )

    @property
    def review_hard_gates_passed(self) -> bool | None:
        """Aggregate only the required P5 Review hard gates."""

        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.BACKGROUND_REVIEW,
        )

    @property
    def toolchain_hard_gates_passed(self) -> bool | None:
        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.SCENARIO,
            metric_types=frozenset({"toolchain_gate", "toolchain_trace", "toolchain_artifacts", "toolchain_checkpoints"}),
        )

    @property
    def process_hard_gates_passed(self) -> bool | None:
        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.SCENARIO,
            metric_types=frozenset(
                {
                    "process_gate",
                    "process_steps",
                    "process_checkpoints",
                    "command_identity",
                    "process_identity",
                    "input_identity",
                    "business_status",
                    "process_status_checkpoint",
                    "process_output_checkpoint",
                    "step_action",
                    "cursor_integrity",
                    "cursor_reference_missing",
                    "cursor_chain_mismatch",
                    "marker_expectations",
                    "status_transitions",
                    "process_trace",
                    "fixture_read",
                    "step_timing",
                    "step_timeout",
                    "scenario_timing",
                    "scenario_timeout",
                    "agent_close",
                    "worker_cleanup",
                }
            ),
        )

    @property
    def hard_gates_passed(self) -> bool:
        """Compatibility alias for task_hard_gates_passed."""

        return self.task_hard_gates_passed

    @property
    def deterministic_hard_gates_passed(self) -> bool:
        return all(
            item.passed
            for item in self.evaluator_results
            if item.required and item.evaluator_kind is EvaluatorKind.DETERMINISTIC
        )

    def required_gate_status(
        self,
        *,
        evaluator_kind: EvaluatorKind,
        metric_types: frozenset[str] | None = None,
    ) -> bool | None:
        evaluator_ids = {
            item.evaluator_id
            for item in self.evaluator_results
            if item.required and item.evaluator_kind is evaluator_kind
        }
        selected = [
            metric
            for metric in self.metrics
            if metric.metadata.get("evaluator_id") in evaluator_ids
            and metric.metadata.get("hard_gate") is True
            and (
                metric_types is None
                or metric.metadata.get("metric_type") in metric_types
            )
        ]
        if not selected:
            return None
        return all(
            metric.status.value == "completed" and metric.passed is True
            for metric in selected
        )


def preflight_evaluators(case: AuditCase) -> None:
    """Reject unsupported or ambiguous evaluator declarations before execution."""

    covered_groups: set[str] = set()
    tool_trajectory_covered = False
    retrieval_covered = False
    compression_covered = False
    background_review_covered = False
    scenario_covered = False
    covered_judges: set[int] = set()
    for evaluator in case.evaluators:
        if evaluator.kind is EvaluatorKind.DETERMINISTIC:
            group = _deterministic_group(evaluator, case_id=case.case_id)
            selected = _deterministic_expectations(case, group)
            if not selected:
                raise UnsupportedCaseError(
                    "deterministic evaluator selects no expectations",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                    expectation_group=group,
                )
            if group == "all":
                covered_groups.update({"files", "texts", "json_values"})
            else:
                covered_groups.add(group)
            continue
        if evaluator.kind is EvaluatorKind.TOOL_TRAJECTORY:
            _validate_tool_config(evaluator, case_id=case.case_id)
            if not case.expected.tool_trajectories:
                raise UnsupportedCaseError(
                    "tool_trajectory evaluator selects no expectations",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if any(
                not _has_p1_tool_constraint(expectation)
                for expectation in case.expected.tool_trajectories
            ):
                raise UnsupportedCaseError(
                    "tool_trajectory expectations must declare a P1 constraint",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            tool_trajectory_covered = True
            continue
        if evaluator.kind is EvaluatorKind.LLM_JUDGE:
            index, _ = resolve_judge_expectation(case, evaluator)
            if index in covered_judges:
                raise UnsupportedCaseError(
                    "a Judge expectation cannot be evaluated more than once",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            covered_judges.add(index)
            continue
        if evaluator.kind is EvaluatorKind.RETRIEVAL:
            if evaluator.config:
                raise UnsupportedCaseError(
                    "retrieval evaluator config must be empty; use strict expectations",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if retrieval_covered:
                raise UnsupportedCaseError(
                    "Memory expectations cannot be evaluated more than once",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if not case.expected.memories and not case.expected.memory_states:
                raise UnsupportedCaseError(
                    "retrieval evaluator selects no Memory expectations",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            retrieval_covered = True
            continue
        if evaluator.kind is EvaluatorKind.COMPRESSION:
            if evaluator.config:
                raise UnsupportedCaseError(
                    "compression evaluator config must be empty; use strict P4 contracts",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if compression_covered:
                raise UnsupportedCaseError(
                    "P4 expectations cannot be evaluated more than once",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if case.ablation is None:
                raise UnsupportedCaseError(
                    "compression evaluator requires an ablation plan",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            has_p4_hard_gate = any(
                item.required or item.distortion_hard_gate
                for item in case.expected.required_facts
            ) or any(item.required for item in case.ablation.checkpoints)
            if evaluator.required is not has_p4_hard_gate:
                raise UnsupportedCaseError(
                    "compression evaluator required must match declared P4 hard gates",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            compression_covered = True
            continue
        if evaluator.kind is EvaluatorKind.BACKGROUND_REVIEW:
            if evaluator.config:
                raise UnsupportedCaseError(
                    "background_review evaluator config must be empty; use strict P5 contracts",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if background_review_covered:
                raise UnsupportedCaseError(
                    "Background Review expectations cannot be evaluated more than once",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if not case.fixture.background_review_plans or not case.expected.background_reviews:
                raise UnsupportedCaseError(
                    "background_review evaluator requires runtime plans and expectations",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            background_review_covered = True
            continue
        if evaluator.kind is EvaluatorKind.SCENARIO:
            if evaluator.config:
                raise UnsupportedCaseError(
                    "scenario evaluator config must be empty; use strict scenario contracts",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if scenario_covered:
                raise UnsupportedCaseError(
                    "scenario plans cannot be evaluated more than once",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            if not case.scenarios:
                raise UnsupportedCaseError(
                    "scenario evaluator requires at least one P6 scenario",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            has_required_scenario = any(item.required for item in case.scenarios)
            if evaluator.required is not has_required_scenario:
                raise UnsupportedCaseError(
                    "scenario evaluator required must match declared scenario gates",
                    case_id=case.case_id,
                    evaluator_id=evaluator.evaluator_id,
                )
            scenario_covered = True
            continue
        raise UnsupportedCaseError(
            "evaluator kind is outside the P1 boundary",
            case_id=case.case_id,
            evaluator_id=evaluator.evaluator_id,
            evaluator_kind=evaluator.kind.value,
        )

    orphan_groups = []
    for group, values in (
        ("files", case.expected.files),
        ("texts", case.expected.texts),
        ("json_values", case.expected.json_values),
    ):
        if values and group not in covered_groups:
            orphan_groups.append(group)
    if case.expected.tool_trajectories and not tool_trajectory_covered:
        orphan_groups.append("tool_trajectories")
    if case.expected.judges and covered_judges != set(
        range(len(case.expected.judges))
    ):
        orphan_groups.append("judges")
    if (case.expected.memories or case.expected.memory_states) and not retrieval_covered:
        orphan_groups.append("memories")
    if case.ablation is not None and not compression_covered:
        orphan_groups.append("ablation")
    if case.expected.background_reviews and not background_review_covered:
        orphan_groups.append("background_reviews")
    if case.scenarios and not scenario_covered:
        orphan_groups.append("scenarios")
    if orphan_groups:
        raise UnsupportedCaseError(
            "P1 expectations must be attached to an evaluator",
            case_id=case.case_id,
            expectation_groups=orphan_groups,
        )


def evaluate_case(
    case: AuditCase,
    context: ValidationContext,
    *,
    trial_id: str,
) -> ValidatorResultsArtifact:
    """Run every declared evaluator and preserve failures as explicit metrics."""

    evaluator_results: list[EvaluatorValidationResult] = []
    metrics: list[MetricResult] = []
    checkpoint_results: list[CheckpointResult] = []
    fact_retention_results: list[FactRetentionResult] = []
    required_fact_loss: RequiredFactLossResult | None = None
    distortion_results: list[DistortionResult] = []
    for evaluator in case.evaluators:
        if evaluator.kind is EvaluatorKind.LLM_JUDGE:
            continue
        if evaluator.kind is EvaluatorKind.COMPRESSION:
            ablation = evaluate_ablation(
                case.expected.required_facts,
                context,
                evaluator_id=evaluator.evaluator_id,
            )
            current = [
                _attach_evaluator_metadata(item, evaluator)
                for item in ablation.metrics
            ]
            checkpoint_results.extend(ablation.checkpoint_results)
            fact_retention_results.extend(ablation.fact_retention_results)
            required_fact_loss = ablation.required_fact_loss
            distortion_results.extend(ablation.distortion_results)
        else:
            current = _evaluate_one(case, evaluator, context)
        metrics.extend(current)
        evaluator_results.append(
            EvaluatorValidationResult(
                evaluator_id=evaluator.evaluator_id,
                evaluator_kind=evaluator.kind,
                required=evaluator.required,
                passed=all(
                    metric.status.value == "completed" and metric.passed is True
                    for metric in current
                    if metric.metadata.get("hard_gate") is True
                ),
                metric_names=[metric.metric_name for metric in current],
            )
        )
    return ValidatorResultsArtifact(
        trial_id=trial_id,
        case_id=case.case_id,
        evaluator_results=evaluator_results,
        metrics=metrics,
        checkpoint_results=checkpoint_results,
        fact_retention_results=fact_retention_results,
        required_fact_loss=required_fact_loss,
        distortion_results=distortion_results,
    )


def _evaluate_one(
    case: AuditCase,
    evaluator: EvaluatorSpec,
    context: ValidationContext,
) -> list[MetricResult]:
    if evaluator.kind is EvaluatorKind.DETERMINISTIC:
        group = _deterministic_group(evaluator, case_id=case.case_id)
        selected = _deterministic_expectations(case, group)
    elif evaluator.kind is EvaluatorKind.TOOL_TRAJECTORY:
        _validate_tool_config(evaluator, case_id=case.case_id)
        selected = [
            ("tool_trajectory", index, expectation, ToolTrajectoryValidator())
            for index, expectation in enumerate(
                case.expected.tool_trajectories,
                start=1,
            )
        ]
    elif evaluator.kind is EvaluatorKind.RETRIEVAL:
        if evaluator.config:
            raise UnsupportedCaseError(
                "retrieval evaluator config must be empty",
                evaluator_id=evaluator.evaluator_id,
            )
        results: list[MetricResult] = []
        for expectation in case.expected.memories:
            results.extend(
                evaluate_memory_expectation(
                    expectation,
                    context,
                    metric_prefix=(
                        f"{evaluator.evaluator_id}.memory.{expectation.query_id}"
                    ),
                )
            )
        for expectation in case.expected.memory_states:
            results.append(
                evaluate_memory_state_expectation(
                    expectation,
                    context,
                    metric_name=(
                        f"{evaluator.evaluator_id}.state.{expectation.state_id}"
                    ),
                )
            )
        return [
            _attach_evaluator_metadata(result, evaluator)
            for result in results
        ]
    elif evaluator.kind is EvaluatorKind.BACKGROUND_REVIEW:
        if evaluator.config:
            raise UnsupportedCaseError(
                "background_review evaluator config must be empty",
                evaluator_id=evaluator.evaluator_id,
            )
        results: list[MetricResult] = []
        for expectation in case.expected.background_reviews:
            if expectation.review_id is None:
                raise UnsupportedCaseError(
                    "runtime Background Review expectation requires review_id",
                    evaluator_id=evaluator.evaluator_id,
                )
            results.extend(
                evaluate_background_review_expectation(
                    expectation,
                    context,
                    metric_prefix=(
                        f"{evaluator.evaluator_id}.review.{expectation.review_id}"
                    ),
                )
            )
        return [
            _attach_evaluator_metadata(result, evaluator)
            for result in results
        ]
    elif evaluator.kind is EvaluatorKind.SCENARIO:
        if evaluator.config:
            raise UnsupportedCaseError(
                "scenario evaluator config must be empty",
                evaluator_id=evaluator.evaluator_id,
            )
        results: list[MetricResult] = []
        for scenario in case.scenarios:
            results.extend(
                evaluate_scenario_plan(
                    scenario,
                    context,
                    metric_prefix=(
                        f"{evaluator.evaluator_id}.scenario.{scenario.scenario_id}"
                    ),
                )
            )
        return [
            _attach_evaluator_metadata(result, evaluator)
            for result in results
        ]
    else:
        raise UnsupportedCaseError(
            "evaluator kind is outside the P1 boundary",
            evaluator_kind=evaluator.kind.value,
        )

    results: list[MetricResult] = []
    for kind, index, expectation, validator in selected:
        metric_name = f"{evaluator.evaluator_id}.{kind}.{index}"
        try:
            result = validator.validate(
                expectation,
                context,
                metric_name=metric_name,
            )
        except Exception as exc:
            source = (
                "runtime"
                if evaluator.kind is EvaluatorKind.TOOL_TRAJECTORY
                else "deterministic"
            )
            from myhermes_audit.contracts import MetricSource

            result = validator_error_metric(
                name=metric_name,
                source=MetricSource(source),
                error=exc,
            )
        result = _attach_evaluator_metadata(result, evaluator)
        results.append(result)
    return results


def _attach_evaluator_metadata(
    result: MetricResult,
    evaluator: EvaluatorSpec,
) -> MetricResult:
    return result.model_copy(
        update={
            "metadata": {
                **result.metadata,
                "evaluator_id": evaluator.evaluator_id,
                "evaluator_kind": evaluator.kind.value,
                "required": evaluator.required,
                "hard_gate": result.metadata.get("hard_gate", True),
            }
        }
    )


def _deterministic_group(evaluator: EvaluatorSpec, *, case_id: str) -> str:
    unknown = set(evaluator.config) - {"expectation_group"}
    group = evaluator.config.get("expectation_group", "all")
    if unknown or type(group) is not str or group not in _DETERMINISTIC_GROUPS:
        raise UnsupportedCaseError(
            "invalid deterministic evaluator config",
            case_id=case_id,
            evaluator_id=evaluator.evaluator_id,
        )
    return group


def _validate_tool_config(evaluator: EvaluatorSpec, *, case_id: str) -> None:
    if evaluator.config:
        raise UnsupportedCaseError(
            "P1 tool_trajectory evaluator config must be empty",
            case_id=case_id,
            evaluator_id=evaluator.evaluator_id,
        )


def resolve_judge_expectation(
    case: AuditCase,
    evaluator: EvaluatorSpec,
) -> tuple[int, JudgeExpectation]:
    if set(evaluator.config) != {"rubric_ref"}:
        raise UnsupportedCaseError(
            "llm_judge config must contain only rubric_ref",
            case_id=case.case_id,
            evaluator_id=evaluator.evaluator_id,
        )
    reference = evaluator.config.get("rubric_ref")
    if not isinstance(reference, str) or not reference.startswith("judges."):
        raise UnsupportedCaseError(
            "llm_judge rubric_ref must use judges.<zero-based-index>",
            case_id=case.case_id,
            evaluator_id=evaluator.evaluator_id,
        )
    suffix = reference.removeprefix("judges.")
    if not suffix.isdigit() or (len(suffix) > 1 and suffix.startswith("0")):
        raise UnsupportedCaseError(
            "llm_judge rubric_ref has an invalid index",
            case_id=case.case_id,
            evaluator_id=evaluator.evaluator_id,
        )
    index = int(suffix)
    if index >= len(case.expected.judges):
        raise UnsupportedCaseError(
            "llm_judge rubric_ref is out of range",
            case_id=case.case_id,
            evaluator_id=evaluator.evaluator_id,
        )
    return index, case.expected.judges[index]


def _has_p1_tool_constraint(
    expectation: ToolTrajectoryExpectation,
) -> bool:
    return any(
        (
            bool(expectation.required_tools),
            bool(expectation.forbidden_tools),
            expectation.minimum_tool_calls is not None,
            expectation.maximum_tool_calls is not None,
            bool(expectation.required_successful_tools),
        )
    )


def _deterministic_expectations(
    case: AuditCase,
    group: str,
) -> list[tuple[str, int, object, object]]:
    selected: list[tuple[str, int, object, object]] = []
    groups: Iterable[tuple[str, list, object]] = (
        ("file", case.expected.files, FileValidator()),
        ("text", case.expected.texts, TextValidator()),
        ("json", case.expected.json_values, JsonFileValidator()),
    )
    aliases = {"file": "files", "text": "texts", "json": "json_values"}
    for kind, expectations, validator in groups:
        if group not in {"all", aliases[kind]}:
            continue
        selected.extend(
            (kind, index, expectation, validator)
            for index, expectation in enumerate(expectations, start=1)
        )
    return selected


__all__ = (
    "EvaluatorValidationResult",
    "ValidatorResultsArtifact",
    "evaluate_case",
    "preflight_evaluators",
    "resolve_judge_expectation",
)
