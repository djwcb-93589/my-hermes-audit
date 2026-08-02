"""Human-readable P1 console summary without unsupported placeholder scores."""

from __future__ import annotations

from myhermes_audit.contracts import (
    AuditRunResult,
    MetricStatus,
    TrialStatus,
)


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
        "Answer quality:    "
        f"{_number_or_missing(result.judge_summary.mean_answer_quality)}",
        "Judge coverage:    "
        f"{result.judge_summary.completed_count}/"
        f"{result.judge_summary.declared_count}",
        f"Judge errors:      {result.judge_summary.error_count}",
        f"Duration P50:      {_duration_or_missing(summary.duration_p50_ms)}",
        f"Duration P95:      {_duration_or_missing(summary.duration_p95_ms)}",
        f"Total tokens:      {_integer_or_missing(summary.total_tokens)}",
        "Langfuse experiment: " + _langfuse_experiment(result),
    ]
    memory_lines = _memory_summary(result)
    if memory_lines:
        lines.extend(memory_lines)
    ablation_lines = _ablation_summary(result)
    if ablation_lines:
        lines.extend(ablation_lines)
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


def _percent_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value * 100:.1f}%"


def _duration_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else f"{value / 1000:.1f}s"


def _integer_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else str(value)


def _number_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value:.3f}"


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
            f"comparability={comparison.comparability.value}"
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
                f"compression={variant.compression_mode.value}, task="
                f"{variant.task_success_rate * 100:.1f}%, "
                f"fact_loss={fact_loss}, distortions="
                f"{variant.distortion_count}, tokens={tokens}, "
                f"duration={variant.duration_ms}ms"
            )
        if comparison.comparability_reasons:
            lines.append(
                "  not comparable: "
                + ", ".join(comparison.comparability_reasons)
            )
    return lines


__all__ = ("render_console_summary",)
