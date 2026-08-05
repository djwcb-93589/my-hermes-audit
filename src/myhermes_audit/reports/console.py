"""Human-readable P1 console summary without unsupported placeholder scores."""

from __future__ import annotations

from myhermes_audit.contracts import (
    AuditRunResult,
    MetricStatus,
    TrialStatus,
)
from myhermes_audit.contracts.regression import AuditRegressionReport


def render_console_summary(result: AuditRunResult) -> str:
    summary = result.summary
    lines = [
        f"MyHermes Audit: {result.suite_id}",
        "",
        f"Subject commit:    {result.subject_fingerprint.git_commit}",
        "Local execution:   "
        + (
            "unknown"
            if result.local_execution_status is None
            else result.local_execution_status.value
        ),
        f"Cases:             {summary.case_count}",
        f"Trials:            {summary.trial_count}",
        f"Passed:            {summary.passed_count}",
        f"Task success:      {_percent_or_missing(summary.task_success_rate)}",
        f"Tool correctness:  {_percent_or_missing(summary.tool_correctness_rate)}",
        f"Memory evidence:   {_percent_or_missing(summary.memory_required_evidence_hit_rate)}",
        f"Memory Recall@K:   {_number_or_missing(summary.memory_recall_at_k_mean)}",
        f"Memory MRR:        {_number_or_missing(summary.memory_mrr_mean)}",
        f"Review decision:   {_percent_or_missing(summary.background_review_decision_accuracy)}",
        f"Agent iterations:  {_number_or_missing(summary.agent_iterations_mean)} "
        f"(P50 {_integer_or_missing(summary.agent_iterations_p50)}, "
        f"P95 {_integer_or_missing(summary.agent_iterations_p95)})",
        f"Duration mean:     {_duration_or_missing(_as_int(summary.duration_mean_ms))}",
        f"Duration P50:      {_duration_or_missing(summary.duration_p50_ms)}",
        f"Duration P95:      {_duration_or_missing(summary.duration_p95_ms)}",
        f"Prompt tokens:     {_integer_or_missing(summary.prompt_tokens_total)}",
        f"Completion tokens: {_integer_or_missing(summary.completion_tokens_total)}",
        f"Total tokens:      {_integer_or_missing(summary.total_tokens)}",
        f"Tool calls:        {_number_or_missing(summary.tool_call_count_mean)} "
        f"(P50 {_integer_or_missing(summary.tool_call_count_p50)}, "
        f"P95 {_integer_or_missing(summary.tool_call_count_p95)})",
        "DeepSeek cache:    " + _cache_summary(summary.deepseek_cache),
        "DeepSeek cost:     " + _cost_summary(summary.deepseek_cost),
        f"Failure rate:       {_percent_or_missing(summary.failure_rate)}",
        f"Timeout rate:       {_percent_or_missing(summary.timeout_rate)}",
        "Langfuse experiment: " + _langfuse_experiment(result),
        "Optional Judge:     "
        f"{_number_or_missing(result.judge_summary.mean_answer_quality)} "
        f"({result.judge_summary.completed_count}/{result.judge_summary.declared_count})",
    ]
    memory_lines = _memory_summary(result)
    if memory_lines:
        lines.extend(memory_lines)
    ablation_lines = _ablation_summary(result)
    if ablation_lines:
        lines.extend(ablation_lines)
    scenario_lines = _scenario_summary(result)
    if scenario_lines:
        lines.extend(scenario_lines)
    if result.integration_errors:
        lines.append(f"Langfuse errors:   {len(result.integration_errors)}")
    publication = result.langfuse_publish_result
    if publication is not None:
        remote_status = result.remote_publication_status or publication.status
        manifest_path = (
            "unavailable"
            if publication.publication_manifest is None
            else publication.publication_manifest.path
        )
        lines.extend(
            (
                f"Remote publication: {remote_status.value}",
                f"Langfuse dataset:   {publication.dataset_sync_status.value}",
                "Langfuse traces:    "
                f"{publication.published_trial_count}/{summary.trial_count}",
                f"Experiment status: {publication.experiment_status.value}",
                "Experiment association: "
                + (
                    "unsupported by installed SDK"
                    if publication.experiment_status.value == "unsupported"
                    else (
                        f"{publication.associated_experiment_item_count}/"
                        f"{summary.trial_count}"
                    )
                ),
                "Score publication:",
                f"- confirmed: {publication.published_score_count}",
                f"- skipped: {publication.skipped_score_count}",
                f"- uncertain: {publication.uncertain_score_count}",
                f"- failed: {publication.failed_score_count}",
                "Publication Manifest: " + manifest_path,
            )
        )
    failures = [trial for trial in result.trials if trial.passed is not True]
    if failures:
        lines.extend(("", "Failures:"))
        for trial in failures:
            variant = "" if trial.variant_id is None else f"/{trial.variant_id}"
            lines.append(
                f"- {trial.case_id}{variant} trial {trial.trial_number}: "
                f"{_failure(trial)}"
            )
    return "\n".join(lines) + "\n"


def _failure(trial) -> str:
    if trial.status is not TrialStatus.COMPLETED:
        if trial.error is not None:
            return f"{trial.status.value}: {trial.error.error_type}"
        return trial.status.value
    failed_metrics = [
        metric
        for metric in trial.metrics
        if metric.metadata.get("required") is True
        and (
            metric.status is MetricStatus.ERROR
            or (
                metric.status is MetricStatus.COMPLETED
                and metric.passed is False
            )
        )
    ]
    if failed_metrics:
        return ", ".join(metric.metric_name for metric in failed_metrics)
    return "required evaluator did not pass"


def _scenario_summary(result: AuditRunResult) -> list[str]:
    scenarios = [item for trial in result.trials for item in trial.scenario_results]
    if not scenarios:
        return []
    process = [item for item in scenarios if item.kind.value == "process_background"]
    output_chars = sum(
        read.new_output_char_length
        for item in process
        for read in item.incremental_reads
    )
    return [
        f"Toolchain scenarios: {sum(item.kind.value == 'toolchain' for item in scenarios)}",
        f"Process scenarios:   {len(process)}",
        "Process gate:         "
        + _gate_or_missing(next((trial.process_gate_passed for trial in result.trials if trial.process_gate_passed is not None), None)),
        "Final process status:  "
        + (process[-1].final_status.value if process and process[-1].final_status is not None else "not evaluated"),
        f"Incremental output chars: {output_chars}",
        "Process cleanup:      "
        + _gate_or_missing(
            all(item.worker_cleanup_result.complete for item in process if item.worker_cleanup_result is not None)
            if process and all(item.worker_cleanup_result is not None for item in process)
            else None
        ),
    ]


def _gate_or_missing(value: bool | None) -> str:
    if value is None:
        return "not evaluated"
    return "passed" if value else "failed"


def _percent_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value * 100:.1f}%"


def _duration_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else f"{value / 1000:.1f}s"


def _integer_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else str(value)


def _number_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value:.3f}"


def _as_int(value: float | None) -> int | None:
    return None if value is None else round(value)


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


def _langfuse_experiment(result: AuditRunResult) -> str:
    identity = result.experiment_identity
    publication = result.langfuse_publish_result
    if identity is None or publication is None:
        return "not published"
    value = f"{identity.experiment_name} ({publication.status.value})"
    if identity.url is not None:
        value += f" {identity.url}"
    return value


def _memory_summary(result: AuditRunResult) -> list[str]:
    query_results = [
        item
        for trial in result.trials
        for item in trial.memory_query_results
    ]
    memory_metrics = [
        metric
        for trial in result.trials
        for metric in trial.metrics
        if metric.source.value == "retrieval"
    ]
    if not query_results and not memory_metrics:
        return []
    evidence = [
        metric
        for metric in memory_metrics
        if metric.metadata.get("metric_type") == "required_evidence"
    ]
    recalls = [
        float(metric.value)
        for metric in memory_metrics
        if metric.metadata.get("metric_type") == "recall_at_k"
        and metric.status is MetricStatus.COMPLETED
        and type(metric.value) in (int, float)
    ]
    mrrs = [
        float(metric.value)
        for metric in memory_metrics
        if metric.metadata.get("metric_type") == "mrr"
        and metric.status is MetricStatus.COMPLETED
        and type(metric.value) in (int, float)
    ]
    state_gates = [
        trial.memory_state_gate_passed
        for trial in result.trials
        if trial.memory_state_gate_passed is not None
    ]
    evidence_rate = (
        "not evaluated"
        if not evidence
        else f"{sum(item.passed is True for item in evidence) / len(evidence) * 100:.1f}%"
    )
    state_rate = (
        "not evaluated"
        if not state_gates
        else f"{sum(item is True for item in state_gates)}/{len(state_gates)}"
    )
    return [
        f"Memory queries:     {len(query_results)}/{len(evidence)} completed",
        f"Memory evidence:    {evidence_rate}",
        f"Memory Recall@K:    {_mean_or_missing(recalls)}",
        f"Memory MRR:         {_mean_or_missing(mrrs)}",
        f"Memory state gate:  {state_rate}",
    ]


def _mean_or_missing(values: list[float]) -> str:
    return "not evaluated" if not values else f"{sum(values) / len(values):.3f}"


def _ablation_summary(result: AuditRunResult) -> list[str]:
    if not result.ablation_comparisons:
        return []
    lines = [f"Ablation cases:     {len(result.ablation_comparisons)}"]
    for comparison in result.ablation_comparisons:
        lines.append(
            f"Ablation {comparison.case_id}: reference="
            f"{comparison.reference_variant_id}, "
            "comparability="
            f"structural:{comparison.structural_comparability.status.value},"
            f"token:{comparison.token_comparability.status.value},"
            "answer_quality:"
            f"{comparison.answer_quality_comparability.status.value},"
            f"duration:{comparison.duration_comparability.status.value}"
        )
        for variant in comparison.variant_results:
            fact_loss = (
                "not evaluated"
                if variant.required_fact_loss_rate is None
                else f"{variant.required_fact_loss_rate * 100:.1f}%"
            )
            tokens = (
                "not evaluated"
                if variant.total_tokens is None
                else str(variant.total_tokens)
            )
            lines.append(
                f"- {variant.variant_id}: memory={variant.memory_mode.value}, "
                "requested_compression_mode="
                f"{variant.requested_compression_mode.value}, task="
                f"{variant.task_success_rate * 100:.1f}%, "
                f"fact_loss={fact_loss}, distortions="
                f"{variant.distortion_count}, tokens={tokens}, "
                "duration="
                + (
                    "not evaluated"
                    if variant.duration_ms is None
                    else f"{variant.duration_ms}ms"
                )
            )
        for label, assessment in (
            ("structural", comparison.structural_comparability),
            ("token", comparison.token_comparability),
            ("answer_quality", comparison.answer_quality_comparability),
            ("duration", comparison.duration_comparability),
        ):
            if assessment.reasons:
                lines.append(
                    f"  {label} not comparable: "
                    + ", ".join(item.value for item in assessment.reasons)
                )
    return lines


def render_console_regression(report: AuditRegressionReport) -> str:
    """Render baseline/current/delta facts, not a weighted summary score."""

    lines = [
        "P7 Regression comparison",
        f"Status:              {report.status.value}",
        f"Baseline:            {report.baseline_id}",
        f"Current run:         {report.current_run_id}",
        f"Trials:              {report.baseline_trial_count} -> {report.current_trial_count}",
        f"Regression gate:     {'pass' if report.overall_regression_gate else 'fail'}",
        f"Comparability:       {'comparable' if not report.comparability_reasons else 'not comparable'}",
        "Counts:              "
        f"regression={report.regression_count} "
        f"improvement={report.improvement_count} "
        f"unchanged={report.unchanged_count} "
        f"warning={report.warning_count} "
        f"not_comparable={report.not_comparable_count}",
    ]
    if report.comparability_reasons:
        lines.append("Reasons:              " + ", ".join(report.comparability_reasons))
    lines.append("Metric changes:")
    for metric in report.suite_metrics:
        lines.append(
            f"- {metric.metric_name}: baseline={metric.baseline_value!r} "
            f"current={metric.current_value!r} delta={metric.absolute_delta!r} "
            f"samples={metric.baseline_sample_count}->{metric.current_sample_count} "
            f"decision={metric.decision.value}"
        )
    lines.append("Case changes:")
    for case in report.case_summaries:
        lines.append(
            f"- {case.case_id}: trials={case.baseline_trial_count}->{case.current_trial_count} "
            f"pass_rate={case.baseline_pass_rate:.4f}->{case.current_pass_rate:.4f} "
            f"delta={case.pass_rate_delta:+.4f} decision={case.decision.value}"
        )
    return "\n".join(lines) + "\n"


__all__ = ("render_console_summary", "render_console_regression")
