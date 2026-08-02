"""P1 evaluator planning and deterministic validator dispatch."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field, StrictBool, model_validator

from myhermes_audit.contracts import AuditCase, JudgeExpectation, MetricResult
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
from myhermes_audit.validators.file import FileValidator
from myhermes_audit.validators.json_file import JsonFileValidator
from myhermes_audit.validators.memory import (
    evaluate_memory_expectation,
    evaluate_memory_state_expectation,
)
from myhermes_audit.validators.text import TextValidator
from myhermes_audit.validators.tool_trajectory import ToolTrajectoryValidator


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
    def final_answer_hard_gates_passed(self) -> bool | None:
        """Aggregate required deterministic final-answer gates independently."""

        return self.required_gate_status(
            evaluator_kind=EvaluatorKind.DETERMINISTIC,
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
    for evaluator in case.evaluators:
        if evaluator.kind is EvaluatorKind.LLM_JUDGE:
            continue
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
