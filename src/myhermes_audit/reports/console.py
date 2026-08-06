"""Human-readable, fact-only console summaries without composite scores."""

from __future__ import annotations

from myhermes_audit.contracts import AuditRunResult
from myhermes_audit.contracts.regression import (
    AuditRegressionReport,
    BASELINE_SCHEMA_VERSION,
)
from myhermes_audit.regression_decision import derive_metric_role, resolve_metric_policy


def render_console_summary(result: AuditRunResult) -> str:
    """Render stored run facts in the operational reading order.

    This function intentionally does not re-aggregate Trials, infer process
    facts, or calculate a headline score.  Detailed event evidence remains in
    the strict JSON artifact and optional Markdown renderer.
    """

    summary = result.summary
    publication = result.langfuse_publish_result
    judge = result.judge_summary
    lines = [
        f"MyHermes Audit: {result.suite_id}",
        "",
        "Run identity:",
        f"- Run ID: {result.run_id}",
        f"- Subject commit: {result.subject_fingerprint.git_commit}",
        "- Suite semantics: "
        + (
            result.audit_fingerprint.suite_comparison_sha256
            or result.audit_fingerprint.suite_sha256
        ),
        f"- Run config: {result.run_configuration_fingerprint}",
        "",
        "Task:",
        f"- Cases: {summary.case_count}",
        f"- Trials: {summary.trial_count}",
        f"- Passed: {summary.passed_count}",
        f"- Task success: {_percent_or_missing(summary.task_success_rate)}",
        "",
        "Tool correctness:",
        f"- Required tool correctness: {_percent_or_missing(summary.tool_correctness_rate)}",
        "",
        "Memory:",
        f"- Evidence hit: {_percent_or_missing(summary.memory_required_evidence_hit_rate)}",
        f"- Recall@K: {_number_or_missing(summary.memory_recall_at_k_mean)}",
        f"- MRR: {_number_or_missing(summary.memory_mrr_mean)}",
        "",
        "Background Review:",
        f"- Decision accuracy: {_percent_or_missing(summary.background_review_decision_accuracy)}",
        "",
        "Agent iterations:",
        f"- Mean: {_number_or_missing(summary.agent_iterations_mean)}",
        f"- P50/P95: {_integer_or_missing(summary.agent_iterations_p50)} / {_integer_or_missing(summary.agent_iterations_p95)}",
        "",
        "Duration:",
        f"- Mean: {_duration_or_missing(_as_int(summary.duration_mean_ms))}",
        f"- P50/P95: {_duration_or_missing(summary.duration_p50_ms)} / {_duration_or_missing(summary.duration_p95_ms)}",
        "",
        "Tokens:",
        f"- Prompt: {_integer_or_missing(summary.prompt_tokens_total)}",
        f"- Completion: {_integer_or_missing(summary.completion_tokens_total)}",
        f"- Total: {_integer_or_missing(summary.total_tokens)}",
        "",
        "Tools:",
        f"- Calls mean: {_number_or_missing(summary.tool_call_count_mean)}",
        f"- Calls P50/P95: {_integer_or_missing(summary.tool_call_count_p50)} / {_integer_or_missing(summary.tool_call_count_p95)}",
        "",
        "DeepSeek cache:",
        "- " + _cache_summary(summary.deepseek_cache),
        "",
        "DeepSeek cost:",
        "- " + _cost_summary(summary.deepseek_cost),
        "",
        "Failure / timeout:",
        f"- Failures: {summary.failure_count} ({_percent_or_missing(summary.failure_rate)})",
        f"- Timeouts: {summary.timeout_count} ({_percent_or_missing(summary.timeout_rate)})",
        f"- Environment errors: {summary.environment_error_count}",
        f"- Cancelled: {summary.cancelled_count}",
        "",
        "Optional Judge:",
        "- Status: " + _judge_status(judge),
        f"- Declared/completed/errors: {judge.declared_count}/{judge.completed_count}/{judge.error_count}",
        f"- Answer quality: {_number_or_missing(judge.mean_answer_quality)}",
        "",
        "Langfuse:",
        "- Status: " + _langfuse_status(result),
        "- Published trials: "
        + (
            "not evaluated"
            if publication is None
            else f"{publication.published_trial_count}/{summary.trial_count}"
        ),
        "- Score publication: "
        + (
            "not evaluated"
            if publication is None
            else (
                f"confirmed {publication.published_score_count}; "
                f"skipped {publication.skipped_score_count}; "
                f"uncertain {publication.uncertain_score_count}; "
                f"failed {publication.failed_score_count}"
            )
        ),
        "",
        "Final status:",
        "- Local execution: "
        + (
            "not evaluated"
            if result.local_execution_status is None
            else result.local_execution_status.value
        ),
    ]
    return "\n".join(lines) + "\n"


def _percent_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value * 100:.1f}%"


def _duration_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else f"{value / 1000:.1f}s"


def _integer_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else str(value)


def _number_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value:.3f}"


def _signed_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value:+.4f}"


def _fingerprint_or_missing(value: str | None) -> str:
    return "<missing>" if value is None else value


def _pricing_reason(metric) -> str:
    return next(
        (
            reason
            for reason in metric.reason_codes
            if reason in {"pricing_fingerprint_missing", "pricing_fingerprint_mismatch"}
        ),
        "<none>",
    )


def _as_int(value: float | None) -> int | None:
    return None if value is None else round(value)


def _judge_status(summary) -> str:
    if summary.declared_count == 0:
        return "disabled"
    if summary.error_count:
        return "error"
    if summary.completed_count:
        return "completed"
    if summary.skipped_count:
        return "skipped"
    return "not evaluated"


def _langfuse_status(result: AuditRunResult) -> str:
    publication = result.langfuse_publish_result
    if publication is None:
        return "not published"
    return publication.status.value


def _cache_summary(value) -> str:
    if value is None:
        return "not evaluated"
    rate = _percent_or_missing(value.cache_hit_rate)
    model_coverage = _percent_or_missing(value.model_call_coverage_rate)
    return (
        f"{value.status.value}, hit {_integer_or_missing(value.prompt_cache_hit_tokens)}, "
        f"miss {_integer_or_missing(value.prompt_cache_miss_tokens)}, "
        "evaluated prompt "
        f"{_integer_or_missing(value.deepseek_cache_evaluated_prompt_tokens)}, "
        f"rate {rate}, model coverage {model_coverage}, "
        f"trial coverage {_percent_or_missing(value.trial_coverage_rate)}"
    )


def _cost_summary(value) -> str:
    if value is None:
        return "not evaluated"
    parts = [
        value.status.value,
        "Trials "
        f"{value.trial_count} (available {value.available_trial_count}, "
        f"partial {value.partial_trial_count}, "
        f"not evaluated {value.not_evaluated_trial_count}, "
        f"invalid {value.invalid_trial_count})",
    ]
    if value.status.value == "not_evaluated":
        parts.append("no monetary facts")
    if value.total_cost_usd is not None:
        parts.append(f"total USD {_money(value.total_cost_usd)}")
    if value.classified_cost_usd is not None and value.total_cost_usd is None:
        parts.append(f"classified USD {_money(value.classified_cost_usd)}")
    if value.available_total_cost_usd is not None and value.total_cost_usd is None:
        parts.append(
            f"available subtotal USD {_money(value.available_total_cost_usd)}"
        )
    if (
        value.available_estimated_cost_without_cache_usd is not None
        and value.total_cost_usd is None
    ):
        parts.append(
            "available no-cache USD "
            f"{_money(value.available_estimated_cost_without_cache_usd)}"
        )
    if value.available_cache_savings_usd is not None and value.total_cost_usd is None:
        parts.append(
            f"available savings USD {_money(value.available_cache_savings_usd)}"
        )
    if value.mean_cost_per_successful_trial_usd is not None:
        parts.append(
            "evaluated-success mean USD "
            f"{_money(value.mean_cost_per_successful_trial_usd)}"
        )
        parts.append(f"evaluated successes {value.cost_evaluated_success_count}")
    if value.effective_cost_per_success_usd is not None:
        parts.append(
            "effective USD " f"{_money(value.effective_cost_per_success_usd)}"
        )
    if value.cache_savings_usd is not None:
        savings = f"savings USD {_money(value.cache_savings_usd)}"
        if value.cache_savings_rate is not None:
            savings += f" ({_percent_decimal(value.cache_savings_rate)})"
        parts.append(savings)
    parts.append(
        "coverage "
        + (
            "not evaluated"
            if value.cost_coverage_rate is None
            else _percent_decimal(value.cost_coverage_rate)
        )
    )
    return ", ".join(parts)


def _money(value) -> str:
    return format(value, ".8f")


def _percent_decimal(value) -> str:
    return f"{value * 100:.1f}%"


def render_console_regression(report: AuditRegressionReport) -> str:
    """Render baseline/current/delta facts, not a weighted summary score."""

    policy_facts = report.regression_policy.to_facts()
    lines = [
        "Regression comparison",
        f"Status:              {report.status.value}",
        f"Baseline:            {report.baseline_id}",
        f"Current run:         {report.current_run_id}",
        f"Total Trials:        {report.baseline_total_trial_count} -> {report.current_total_trial_count}",
        f"Trials/Case:         {_integer_or_missing(report.baseline_declared_trials_per_case)} -> "
        f"{_integer_or_missing(report.current_declared_trials_per_case)}",
        f"Regression gate:     {'pass' if report.overall_regression_gate else 'fail'}",
        f"Comparability:       {'comparable' if report.status.value != 'not_comparable' else 'not comparable'}",
        f"Report schema:       {report.schema_version}",
        f"Baseline schema:     {BASELINE_SCHEMA_VERSION}",
        "Model identity:      "
        f"{report.baseline_model_identity.status.value} -> "
        f"{report.current_model_identity.status.value}",
        "Run config identity: "
        f"{report.baseline_run_configuration_identity.status.value} -> "
        f"{report.current_run_configuration_identity.status.value}",
        "Run config hash:     "
        f"{_fingerprint_or_missing(report.baseline_run_configuration_fingerprint)} -> "
        f"{_fingerprint_or_missing(report.current_run_configuration_fingerprint)}",
        "Suite semantics:     "
        f"{_fingerprint_or_missing(report.baseline_suite_comparison_fingerprint)} -> "
        f"{_fingerprint_or_missing(report.current_suite_comparison_fingerprint)}",
        "Result/Metric schema:"
        f" {report.baseline_result_schema_version}/"
        f"{report.current_result_schema_version}/"
        f"{report.metric_contract_version}",
        f"Policy schema:        {report.regression_policy.schema_version}",
        f"Policy fingerprint:   {report.regression_policy_fingerprint}",
        "Pricing identity:     "
        f"{_fingerprint_or_missing(report.baseline_pricing_fingerprint)} -> "
        f"{_fingerprint_or_missing(report.current_pricing_fingerprint)}",
        "Counts:              "
        f"regression={report.regression_count} "
        f"improvement={report.improvement_count} "
        f"unchanged={report.unchanged_count} "
        f"warning={report.warning_count} "
        f"not_comparable={report.not_comparable_count}",
        f"not_evaluated={report.not_evaluated_count}",
        f"Comparable core Metrics: {report.comparable_core_metric_count}",
        f"Comparable local Metrics: {report.comparable_local_metric_count}",
        "Suite task success:  "
        f"{report.baseline_total_trial_count} trials, "
        f"{report.baseline_suite_task_success_sample_count}/"
        f"{report.baseline_suite_task_success_passed_count} "
        f"{_percent_or_missing(report.baseline_suite_task_success_rate)} -> "
        f"{report.current_suite_task_success_sample_count}/"
        f"{report.current_suite_task_success_passed_count} "
        f"{_percent_or_missing(report.current_suite_task_success_rate)} "
        f"delta={_signed_or_missing(report.suite_task_success_rate_delta)}",
    ]
    if report.comparability_reasons:
        lines.append("Reasons:              " + ", ".join(report.comparability_reasons))
    lines.append("Metric changes:")
    for metric in report.suite_metrics:
        lines.append(
            f"- {metric.metric_name}: baseline={metric.baseline_value!r} "
            f"current={metric.current_value!r} delta={metric.absolute_delta!r} "
            f"samples={metric.baseline_sample_count}->{metric.current_sample_count} "
            f"evaluation={metric.evaluation_status.value} "
            f"comparability={metric.comparability_status.value} "
            f"policy={metric.policy_mode.value}/{metric.direction.value} "
            f"thresholds=(drop={metric.max_absolute_drop!r}, "
            f"relative_increase={metric.max_relative_increase!r}, "
            f"absolute_increase={metric.max_absolute_increase!r}) "
            "role="
            f"{derive_metric_role(resolve_metric_policy(metric.metric_name, policy_facts)).value} "
            f"requires_pricing_match={metric.requires_pricing_match} "
            f"pricing_reason={_pricing_reason(metric)} "
            f"decision={metric.decision.value} "
            f"reasons={','.join(metric.reason_codes) or '<none>'}"
        )
    lines.append("Case changes:")
    for case in report.case_summaries:
        lines.append(
            f"- {case.case_id}: trials={case.baseline_trial_count}->{case.current_trial_count} "
            f"repeats={case.baseline_declared_trial_count}->{case.current_declared_trial_count} "
            "task_success="
            f"{case.baseline_task_success_sample_count}/"
            f"{case.baseline_task_success_passed_count} "
            f"{_percent_or_missing(case.baseline_task_success_rate)} -> "
            f"{case.current_task_success_sample_count}/"
            f"{case.current_task_success_passed_count} "
            f"{_percent_or_missing(case.current_task_success_rate)} "
            f"delta={_signed_or_missing(case.task_success_rate_delta)} "
            f"decision={case.decision.value}"
        )
    return "\n".join(lines) + "\n"


__all__ = ("render_console_summary", "render_console_regression")
