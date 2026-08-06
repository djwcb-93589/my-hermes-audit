"""Strict, content-safe Markdown rendering for existing Audit fact contracts.

This module deliberately has no aggregation, comparison, Judge, Langfuse, or
runner dependency.  It loads one of the already-validated public fact models
and formats only fields explicitly allowed in a final operational report.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from myhermes_audit.artifacts import atomic_write_text
from myhermes_audit.contracts import (
    AuditBaseline,
    AuditRegressionReport,
    AuditRunResult,
    REPORT_RENDER_SCHEMA_VERSION,
    ReportInputType,
    ReportRenderOptions,
)
from myhermes_audit.errors import ReportError


ReportSource: TypeAlias = AuditRunResult | AuditBaseline | AuditRegressionReport
_MAX_REPORT_INPUT_BYTES = 100 * 1024 * 1024
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def load_markdown_report_source(
    path: Path,
    *,
    input_type: ReportInputType | str = ReportInputType.AUTO,
) -> ReportSource:
    """Strictly load exactly one public source contract for rendering.

    Arbitrary mappings, permissive JSON, and unknown schema versions are never
    accepted.  The generic error deliberately avoids echoing source contents
    or local absolute paths.
    """

    try:
        selected_type = ReportInputType(input_type)
    except ValueError as exc:
        raise ReportError("report input type is invalid") from exc
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ReportError("report input cannot be a symbolic link")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_file():
        raise ReportError("report input must be a regular file")
    try:
        if resolved.stat().st_size > _MAX_REPORT_INPUT_BYTES:
            raise ReportError("report input exceeds the safe size limit")
        payload = resolved.read_text(encoding="utf-8")
    except ReportError:
        raise
    except OSError as exc:
        raise ReportError("report input cannot be read", operation="report_load") from exc

    candidates: tuple[tuple[ReportInputType, type[ReportSource]], ...] = (
        (ReportInputType.RUN, AuditRunResult),
        (ReportInputType.BASELINE, AuditBaseline),
        (ReportInputType.REGRESSION, AuditRegressionReport),
    )
    for kind, model_type in candidates:
        if selected_type is not ReportInputType.AUTO and selected_type is not kind:
            continue
        try:
            return model_type.model_validate_json(payload)
        except (TypeError, ValueError):
            continue
    raise ReportError(
        "report input does not match the selected strict fact contract",
        operation="report_load",
    )


def render_markdown_report(
    source: ReportSource,
    *,
    options: ReportRenderOptions | None = None,
) -> str:
    """Render facts already present in a strict Result, Baseline, or Report.

    The renderer never recalculates metrics or regression decisions.  It does
    not inspect prompts, outputs, evidence, local paths, configuration bodies,
    or credentials.
    """

    options = options or ReportRenderOptions()
    if not isinstance(options, ReportRenderOptions):
        raise TypeError("options must be a ReportRenderOptions instance")
    actual_type = _source_type(source)
    if options.input_type not in (ReportInputType.AUTO, actual_type):
        raise ReportError("report input type does not match render options")
    if isinstance(source, AuditRunResult):
        return _render_run(source, options)
    if isinstance(source, AuditBaseline):
        return _render_baseline(source, options)
    if isinstance(source, AuditRegressionReport):
        return _render_regression(source, options)
    raise TypeError("source must be a strict Audit result, Baseline, or Regression report")


def write_markdown_report(
    path: Path,
    source: ReportSource,
    *,
    options: ReportRenderOptions | None = None,
    overwrite: bool = False,
    protected: tuple[Path, ...] = (),
) -> Path:
    """Render and atomically publish a Markdown report without implicit overwrite."""

    output = _validate_markdown_output(path, overwrite=overwrite, protected=protected)
    text = render_markdown_report(source, options=options)
    return atomic_write_text(output, text)


def _validate_markdown_output(
    path: Path,
    *,
    overwrite: bool,
    protected: tuple[Path, ...],
) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ReportError("Markdown output cannot be a symbolic link")
    resolved = candidate.resolve(strict=False)
    if resolved.suffix.lower() != ".md":
        raise ReportError("Markdown output must use the .md extension")
    protected_paths = {
        Path(item).expanduser().resolve(strict=False) for item in protected
    }
    if resolved in protected_paths:
        raise ReportError("Markdown output cannot overwrite a report input")
    if resolved.exists() and not resolved.is_file():
        raise ReportError("Markdown output must be a regular file path")
    if resolved.exists() and not overwrite:
        raise ReportError("Markdown output exists; pass --overwrite explicitly")
    existing_parent = resolved.parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise ReportError("Markdown output parent is not a directory")
    return resolved


def _source_type(source: ReportSource) -> ReportInputType:
    if isinstance(source, AuditRunResult):
        return ReportInputType.RUN
    if isinstance(source, AuditBaseline):
        return ReportInputType.BASELINE
    if isinstance(source, AuditRegressionReport):
        return ReportInputType.REGRESSION
    raise TypeError("source must be a strict Audit result, Baseline, or Regression report")


def _render_run(result: AuditRunResult, options: ReportRenderOptions) -> str:
    summary = result.summary
    semantic_fingerprint = (
        result.audit_fingerprint.suite_comparison_sha256
        or result.audit_fingerprint.suite_sha256
    )
    lines = _document_start("Representative Audit Report", "AuditRunResult", result.schema_version)
    lines.extend(
        (
            "## Executive Summary",
            "",
            *_table(
                ("Fact", "Value"),
                (
                    ("Run ID", result.run_id),
                    ("Subject commit", result.subject_fingerprint.git_commit),
                    ("Audit commit", _missing(result.audit_fingerprint.audit_commit)),
                    ("Suite ID", result.suite_id),
                    ("Suite semantic fingerprint", semantic_fingerprint),
                    ("Run configuration fingerprint", result.run_configuration_fingerprint),
                    ("DeepSeek pricing fingerprint", _missing(result.deepseek_pricing_fingerprint)),
                    ("Cases", summary.case_count),
                    ("Trials", summary.trial_count),
                    ("Passed trials", summary.passed_count),
                    ("Final execution status", _enum_or_missing(result.local_execution_status)),
                    ("Regression", "not evaluated (no Baseline comparison in AuditRunResult)"),
                    ("Key failed Cases", _failed_case_ids(result)),
                    ("Key warnings", _run_warning_codes(result)),
                ),
            ),
            "",
            "## Representative Metrics",
            "",
            *_table(
                ("Metric", "Value", "Samples"),
                _summary_metric_rows(summary),
            ),
            "",
            "## Case Summary",
            "",
            *_table(
                (
                    "Case ID",
                    "Trials / passed",
                    "Aggregate pass rate",
                    "Iterations mean",
                    "Duration mean",
                    "Tokens mean",
                    "Tool calls mean",
                    "Cache",
                    "Cost",
                    "Stored evaluator facts",
                    "Failure category",
                    "Regression decision",
                ),
                tuple(_run_case_row(case) for case in result.cases),
            ),
        )
    )
    if options.include_diagnostics:
        lines.extend(_run_diagnostics(result))
    return "\n".join(lines) + "\n"


def _render_baseline(baseline: AuditBaseline, options: ReportRenderOptions) -> str:
    suite = baseline.suite_summary
    lines = _document_start("Representative Baseline Report", "AuditBaseline", baseline.schema_version)
    lines.extend(
        (
            "## Executive Summary",
            "",
            *_table(
                ("Fact", "Value"),
                (
                    ("Baseline ID", baseline.baseline_id),
                    ("Source run ID", baseline.source_run_id),
                    ("Subject commit", baseline.subject_commit),
                    ("Audit commit", _missing(baseline.audit_commit)),
                    ("Suite ID", baseline.suite_id),
                    ("Suite semantic fingerprint", _missing(baseline.suite_comparison_fingerprint)),
                    ("Run configuration fingerprint", baseline.run_configuration_fingerprint),
                    ("Model identity", _identity_value(baseline.model_identity)),
                    ("Trials", baseline.total_trial_count),
                    ("Declared trials per Case", _missing(baseline.declared_trials_per_case)),
                    ("Baseline status", "validated immutable fact projection"),
                    ("Regression", "not evaluated (Baseline is not a comparison)"),
                    ("Key failed Cases", _baseline_failed_case_ids(baseline)),
                    ("Key warnings", _codes_or_missing(baseline.warnings)),
                ),
            ),
            "",
            "## Representative Metrics",
            "",
            *_table(
                ("Metric", "Value", "Samples"),
                _summary_metric_rows(suite.summary),
            ),
            "",
            "## Case Summary",
            "",
            *_table(
                (
                    "Case ID",
                    "Trials / task success",
                    "Iterations mean",
                    "Duration mean",
                    "Tokens mean",
                    "Tool calls mean",
                    "Memory / review",
                    "Cache",
                    "Cost",
                    "Stored metric facts",
                    "Failure categories",
                    "Regression decision",
                ),
                tuple(_baseline_case_row(case) for case in baseline.case_summaries),
            ),
        )
    )
    if options.include_diagnostics:
        lines.extend(_baseline_diagnostics(baseline))
    return "\n".join(lines) + "\n"


def _render_regression(
    report: AuditRegressionReport,
    options: ReportRenderOptions,
) -> str:
    lines = _document_start(
        "Representative Regression Report", "AuditRegressionReport", report.schema_version
    )
    lines.extend(
        (
            "## Executive Summary",
            "",
            *_table(
                ("Fact", "Value"),
                (
                    ("Baseline ID", report.baseline_id),
                    ("Current run ID", report.current_run_id),
                    ("Baseline subject commit", report.baseline_subject_commit),
                    ("Current subject commit", report.current_subject_commit),
                    ("Suite ID", report.suite_id),
                    ("Suite semantic fingerprint", _missing(report.current_suite_comparison_fingerprint)),
                    ("Baseline / current run config", f"{report.baseline_run_configuration_fingerprint} / {report.current_run_configuration_fingerprint}"),
                    ("Baseline / current model", f"{_identity_value(report.baseline_model_identity)} / {_identity_value(report.current_model_identity)}"),
                    ("Baseline / current trials", f"{report.baseline_total_trial_count} / {report.current_total_trial_count}"),
                    ("Regression policy fingerprint", report.regression_policy_fingerprint),
                    ("Regression status", report.status.value),
                    ("Final regression gate", _gate(report.overall_regression_gate)),
                    ("Key currently failing Cases", _regression_case_ids(report, "current")),
                    ("Key regressed Cases", _regressed_case_ids(report)),
                    ("Key warnings", _codes_or_missing(report.warnings)),
                ),
            ),
            "",
            "## Metric Comparison",
            "",
            *_table(
                (
                    "Metric",
                    "Baseline",
                    "Current",
                    "Absolute delta",
                    "Relative delta",
                    "Samples",
                    "Policy",
                    "Decision",
                    "Reasons",
                ),
                tuple(_comparison_row(metric) for metric in report.suite_metrics),
            ),
            "",
            "## Case Comparison",
            "",
            *_table(
                (
                    "Case ID",
                    "Trials (baseline/current)",
                    "Task success / delta",
                    "Failure categories (baseline/current)",
                    "Review actions (baseline/current)",
                    "Runtime/cache/cost facts",
                    "Decision",
                ),
                tuple(_regression_case_row(case) for case in report.case_summaries),
            ),
        )
    )
    if options.include_diagnostics:
        lines.extend(_regression_diagnostics(report))
    return "\n".join(lines) + "\n"


def _document_start(title: str, source_contract: str, schema_version: str) -> list[str]:
    return [
        f"# {title}",
        "",
        "This Markdown is a presentation of an already validated strict JSON fact contract. "
        "It does not execute a Trial, recompute a metric, or change any gate.",
        "",
        f"- Report render contract: `{REPORT_RENDER_SCHEMA_VERSION}`",
        f"- Source contract: `{source_contract}` `{schema_version}`",
        "- Sensitive content is intentionally omitted.",
        "",
    ]


def _summary_metric_rows(summary) -> tuple[tuple[object, ...], ...]:
    cache = summary.deepseek_cache
    cost = summary.deepseek_cost
    return (
        ("Task success", _percent(summary.task_success_rate), summary.task_success_sample_count),
        ("Tool correctness", _percent(summary.tool_correctness_rate), summary.tool_correctness_sample_count),
        ("Memory evidence hit", _percent(summary.memory_required_evidence_hit_rate), summary.memory_required_evidence_sample_count),
        ("Memory Recall@K", _number(summary.memory_recall_at_k_mean), summary.memory_recall_at_k_sample_count),
        ("Memory MRR", _number(summary.memory_mrr_mean), summary.memory_mrr_sample_count),
        ("Background Review decision accuracy", _percent(summary.background_review_decision_accuracy), summary.background_review_decision_sample_count),
        ("Conversation turns", _number(summary.conversation_turn_count_mean), summary.conversation_turn_count_total),
        ("Agent iterations", _distribution(summary.agent_iterations_mean, summary.agent_iterations_p50, summary.agent_iterations_p95), summary.trial_count),
        ("Duration", _duration_distribution(summary.duration_mean_ms, summary.duration_p50_ms, summary.duration_p95_ms), summary.duration_sample_count),
        ("Prompt tokens", _number(summary.prompt_tokens_total), summary.prompt_tokens_sample_count),
        ("Completion tokens", _number(summary.completion_tokens_total), summary.completion_tokens_sample_count),
        ("Total tokens", _number(summary.total_tokens), summary.total_tokens_sample_count),
        ("Tool calls", _distribution(summary.tool_call_count_mean, summary.tool_call_count_p50, summary.tool_call_count_p95), summary.tool_call_sample_count),
        ("DeepSeek cache", _cache_fact(cache), _cache_coverage(cache)),
        ("DeepSeek cost", _cost_fact(cost), _cost_coverage(cost)),
        ("Failures", _percent(summary.failure_rate), summary.failure_count),
        ("Timeouts", _percent(summary.timeout_rate), summary.timeout_count),
        ("Environment errors", _number(summary.environment_error_count), summary.trial_count),
        ("Cancelled", _number(summary.cancelled_count), summary.trial_count),
    )


def _run_case_row(case) -> tuple[object, ...]:
    return (
        case.case_id,
        f"{case.trial_count} / {case.passed_count}",
        _percent(case.pass_rate),
        _number(case.agent_iterations_mean),
        _duration(case.duration_mean_ms),
        _number(case.total_tokens_mean_per_trial),
        _number(case.tool_call_count_mean),
        _cache_fact(case.deepseek_cache),
        _cost_fact(case.deepseek_cost),
        _metric_summaries_fact(case.metric_summaries),
        "not evaluated (not projected by CaseAggregate)",
        "not evaluated (no Regression report)",
    )


def _baseline_case_row(case) -> tuple[object, ...]:
    summary = case.summary
    return (
        case.case_id,
        f"{case.summary.trial_count} / {case.task_success_passed_count}/{case.task_success_sample_count}",
        _number(summary.agent_iterations_mean),
        _duration(summary.duration_mean_ms),
        _number(summary.total_tokens_mean_per_trial),
        _number(summary.tool_call_count_mean),
        f"task {_percent(case.task_success_rate)}; review {_percent(case.background_review_decision_accuracy)}",
        _cache_fact(case.deepseek_cache),
        _cost_fact(case.deepseek_cost),
        _metric_snapshots_fact(case.metrics),
        _distribution_codes(case.failure_categories),
        "not evaluated (Baseline is not a comparison)",
    )


def _comparison_row(metric) -> tuple[object, ...]:
    threshold = _threshold(metric)
    policy = f"{metric.policy_mode.value}; {metric.direction.value}; {threshold}"
    return (
        metric.metric_name,
        _number(metric.baseline_value),
        _number(metric.current_value),
        _signed(metric.absolute_delta),
        _signed(metric.relative_delta),
        f"{metric.baseline_sample_count} / {metric.current_sample_count}",
        policy,
        metric.decision.value,
        _codes_or_missing(metric.reason_codes),
    )


def _regression_case_row(case) -> tuple[object, ...]:
    runtime = "; ".join(
        (
            "iterations " + _case_metric_pair(case, "agent_iterations_mean"),
            "duration " + _case_metric_pair(case, "duration_mean_ms", duration=True),
            "tokens " + _case_metric_pair(case, "total_tokens_mean_per_trial"),
            "tools " + _case_metric_pair(case, "tool_call_count_mean"),
            "cache " + _case_metric_pair(case, "deepseek_cache_hit_rate", percent=True),
            "cost " + _case_metric_pair(case, "deepseek_cost_total_usd", currency=True),
        )
    )
    return (
        case.case_id,
        f"{case.baseline_trial_count} / {case.current_trial_count}",
        f"{_percent(case.baseline_task_success_rate)} / {_percent(case.current_task_success_rate)} / {_signed(case.task_success_rate_delta)}",
        f"{_distribution_codes(case.baseline_failure_categories)} / {_distribution_codes(case.current_failure_categories)}",
        f"{_distribution_codes(case.baseline_background_review_actions)} / {_distribution_codes(case.current_background_review_actions)}",
        runtime,
        case.decision.value,
    )


def _run_diagnostics(result: AuditRunResult) -> list[str]:
    trial_rows = tuple(
        (
            trial.case_id,
            _missing(trial.variant_id),
            trial.trial_number,
            trial.status.value,
            "not evaluated" if trial.error is None else trial.error.error_type,
            _codes_or_missing([warning.warning_type for warning in trial.warnings]),
        )
        for trial in result.trials
        if trial.passed is not True or trial.error is not None or trial.warnings
    )
    artifact_rows = tuple(
        (trial.case_id, artifact.kind, artifact.artifact_id, artifact.relative_path, artifact.sha256)
        for trial in result.trials
        for artifact in trial.artifacts
    )
    lines = ["", "## Diagnostics", "", "### Execution and evaluator status", ""]
    lines.extend(
        _table(
            ("Case ID", "Variant", "Trial", "Status", "Error code", "Warning codes"),
            trial_rows or (("not evaluated", "not evaluated", "not evaluated", "not evaluated", "not evaluated", "not evaluated"),),
        )
    )
    lines.extend(("", "### Safe artifacts", ""))
    lines.extend(
        _table(
            ("Case ID", "Kind", "Artifact ID", "Relative path", "SHA-256"),
            artifact_rows or (("not evaluated", "not evaluated", "not evaluated", "not evaluated", "not evaluated"),),
        )
    )
    lines.extend(("", "### Optional evaluators and publication", ""))
    publication = result.langfuse_publish_result
    lines.extend(
        _table(
            ("Integration", "Status", "Declared", "Completed", "Errors", "Safe result"),
            (
                (
                    "Optional Judge",
                    _judge_status(result),
                    result.judge_summary.declared_count,
                    result.judge_summary.completed_count,
                    result.judge_summary.error_count,
                    _number(result.judge_summary.mean_answer_quality),
                ),
                (
                    "Langfuse",
                    "not published" if publication is None else publication.status.value,
                    "not evaluated" if publication is None else result.summary.trial_count,
                    "not evaluated" if publication is None else publication.published_trial_count,
                    len(result.integration_errors) if publication is None else len(publication.errors),
                    "not evaluated",
                ),
            ),
        )
    )
    return lines


def _baseline_diagnostics(baseline: AuditBaseline) -> list[str]:
    return [
        "",
        "## Diagnostics",
        "",
        "### Identity and comparability prerequisites",
        "",
        *_table(
            ("Identity", "Status", "Value"),
            (
                ("Model", baseline.model_identity.status.value, _identity_value(baseline.model_identity)),
                ("Run configuration", baseline.run_configuration_identity.status.value, _identity_value(baseline.run_configuration_identity)),
                ("Worker protocol", baseline.worker_protocol_identity.status.value, _identity_value(baseline.worker_protocol_identity)),
                ("Result schema", baseline.result_schema_identity.status.value, _identity_value(baseline.result_schema_identity)),
                ("Metric contract", baseline.metric_contract_identity.status.value, _identity_value(baseline.metric_contract_identity)),
                ("Pricing fingerprint", "available" if baseline.pricing_fingerprint is not None else "not evaluated", _missing(baseline.pricing_fingerprint)),
            ),
        ),
        "",
        "### Stored diagnostic codes",
        "",
        *_table(("Scope", "Codes"), (("Baseline", _codes_or_missing(baseline.warnings)),)),
    ]


def _regression_diagnostics(report: AuditRegressionReport) -> list[str]:
    identity_rows = (
        ("Baseline model", report.baseline_model_identity.status.value, _identity_value(report.baseline_model_identity)),
        ("Current model", report.current_model_identity.status.value, _identity_value(report.current_model_identity)),
        ("Baseline run configuration", report.baseline_run_configuration_identity.status.value, _identity_value(report.baseline_run_configuration_identity)),
        ("Current run configuration", report.current_run_configuration_identity.status.value, _identity_value(report.current_run_configuration_identity)),
        ("Baseline worker protocol", report.baseline_worker_protocol_identity.status.value, _identity_value(report.baseline_worker_protocol_identity)),
        ("Current worker protocol", report.current_worker_protocol_identity.status.value, _identity_value(report.current_worker_protocol_identity)),
    )
    policy_rows = tuple(
        (
            item.metric_name,
            item.mode.value,
            item.direction.value,
            _threshold(item),
            item.require_pricing_match,
        )
        for item in report.regression_policy.metrics
    )
    return [
        "",
        "## Diagnostics",
        "",
        "### Identity and comparability",
        "",
        *_table(("Identity", "Status", "Safe value"), identity_rows),
        "",
        "### Regression policy snapshot",
        "",
        *_table(
            ("Metric", "Mode", "Direction", "Threshold", "Requires pricing match"),
            policy_rows or (("not evaluated", "not evaluated", "not evaluated", "not evaluated", "not evaluated"),),
        ),
        "",
        "### Report facts",
        "",
        *_table(
            ("Fact", "Value"),
            (
                ("Comparability reasons", _codes_or_missing(report.comparability_reasons)),
                ("Comparable core metrics", report.comparable_core_metric_count),
                ("Comparable pricing-local metrics", report.comparable_local_metric_count),
                ("Regression / improvement / unchanged", f"{report.regression_count} / {report.improvement_count} / {report.unchanged_count}"),
                ("Warning / not comparable / not evaluated", f"{report.warning_count} / {report.not_comparable_count} / {report.not_evaluated_count}"),
                ("Pricing identity", f"{_missing(report.baseline_pricing_fingerprint)} / {_missing(report.current_pricing_fingerprint)}"),
            ),
        ),
    ]


def _failed_case_ids(result: AuditRunResult) -> str:
    values = [case.case_id for case in result.cases if case.passed_count != case.trial_count]
    return _codes_or_missing(values)


def _baseline_failed_case_ids(baseline: AuditBaseline) -> str:
    values = [
        case.case_id
        for case in baseline.case_summaries
        if case.task_success_rate is not None and case.task_success_rate < 1
    ]
    return _codes_or_missing(values)


def _regression_case_ids(report: AuditRegressionReport, side: str) -> str:
    if side != "current":
        raise ValueError("unsupported regression side")
    values = [
        case.case_id
        for case in report.case_summaries
        if case.current_task_success_rate is not None and case.current_task_success_rate < 1
    ]
    return _codes_or_missing(values)


def _regressed_case_ids(report: AuditRegressionReport) -> str:
    return _codes_or_missing(
        [case.case_id for case in report.case_summaries if case.decision.value == "regressed"]
    )


def _run_warning_codes(result: AuditRunResult) -> str:
    return _codes_or_missing(
        [
            *(item.error_type for item in result.integration_errors),
            *(warning.warning_type for trial in result.trials for warning in trial.warnings),
        ]
    )


def _case_metric_pair(
    case,
    name: str,
    *,
    percent: bool = False,
    duration: bool = False,
    currency: bool = False,
) -> str:
    metric = next((item for item in case.metrics if item.metric_name == name), None)
    if metric is None:
        return "not evaluated"
    formatter = _number
    if percent:
        formatter = _percent
    elif duration:
        formatter = _duration
    elif currency:
        formatter = _usd
    return f"{formatter(metric.baseline_value)} / {formatter(metric.current_value)}"


def _metric_summaries_fact(metrics) -> str:
    if not metrics:
        return "not evaluated"
    return "; ".join(
        f"{_safe_code(metric.metric_name)}={_number(metric.mean)} "
        f"({metric.passed_count}/{metric.sample_count})"
        for metric in metrics
    )


def _metric_snapshots_fact(metrics) -> str:
    if not metrics:
        return "not evaluated"
    return "; ".join(
        f"{_safe_code(metric.metric_name)}={_number(metric.value)} "
        f"({metric.sample_count})"
        for metric in metrics
    )


def _cache_fact(cache) -> str:
    if cache is None:
        return "not evaluated"
    return (
        f"{cache.status.value}; hit {_number(cache.prompt_cache_hit_tokens)}; "
        f"miss {_number(cache.prompt_cache_miss_tokens)}; rate {_percent(cache.cache_hit_rate)}"
    )


def _cache_coverage(cache) -> str:
    if cache is None:
        return "not evaluated"
    return (
        f"model {_percent(cache.model_call_coverage_rate)}; "
        f"trial {_percent(cache.trial_coverage_rate)}"
    )


def _cost_fact(cost) -> str:
    if cost is None or cost.status.value == "not_evaluated":
        return "not evaluated"
    return (
        f"{cost.status.value}; total {_usd(cost.total_cost_usd)}; "
        f"mean/evaluated {_usd(cost.mean_cost_per_evaluated_trial_usd)}; "
        f"mean/success {_usd(cost.mean_cost_per_successful_trial_usd)}; "
        f"effective/success {_usd(cost.effective_cost_per_success_usd)}; "
        f"cache saving {_usd(cost.cache_savings_usd)}"
    )


def _cost_coverage(cost) -> str:
    if cost is None or cost.status.value == "not_evaluated":
        return "not evaluated"
    return _percent(cost.cost_coverage_rate)


def _judge_status(result: AuditRunResult) -> str:
    summary = result.judge_summary
    if summary.declared_count == 0:
        return "disabled"
    if summary.error_count:
        return "error"
    if summary.completed_count:
        return "completed"
    if summary.skipped_count:
        return "skipped"
    return "not evaluated"


def _identity_value(identity) -> str:
    value = identity.value
    if value is None:
        return "not evaluated"
    text = str(value)
    if (
        len(text) > 255
        or "\n" in text
        or "\r" in text
        or "\\" in text
        or text.startswith("/")
        or "://" in text
        or "=" in text
    ):
        return "not evaluated (unsafe identity omitted)"
    return text


def _threshold(item) -> str:
    values = (
        ("max drop", item.max_absolute_drop),
        ("max relative increase", item.max_relative_increase),
        ("max increase", item.max_absolute_increase),
    )
    return "; ".join(
        f"{label} {_number(value)}" for label, value in values if value is not None
    ) or "not evaluated"


def _table(headers: tuple[str, ...], rows: tuple[tuple[object, ...], ...]) -> list[str]:
    rendered_rows = tuple(tuple(_cell(value) for value in row) for row in rows)
    return [
        "| " + " | ".join(_cell(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in rendered_rows),
    ]


def _cell(value: object) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _text(value: object) -> str:
    if value is None:
        return "not evaluated"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _missing(value: object) -> str:
    return "not evaluated" if value is None else _text(value)


def _enum_or_missing(value: Enum | None) -> str:
    return "not evaluated" if value is None else value.value


def _number(value: object) -> str:
    return "not evaluated" if value is None else _text(value)


def _signed(value: object) -> str:
    if value is None:
        return "not evaluated"
    if isinstance(value, (int, float, Decimal)) and value >= 0:
        return "+" + _text(value)
    return _text(value)


def _percent(value: object) -> str:
    if value is None:
        return "not evaluated"
    if isinstance(value, Decimal):
        return format(value * Decimal("100"), "f") + "%"
    return f"{float(value) * 100:.2f}%"


def _duration(value: object) -> str:
    if value is None:
        return "not evaluated"
    return f"{float(value) / 1000:.3f}s"


def _distribution(mean: object, p50: object, p95: object) -> str:
    return f"mean {_number(mean)}; P50 {_number(p50)}; P95 {_number(p95)}"


def _duration_distribution(mean: object, p50: object, p95: object) -> str:
    return f"mean {_duration(mean)}; P50 {_duration(p50)}; P95 {_duration(p95)}"


def _usd(value: object) -> str:
    return "not evaluated" if value is None else "$" + _text(value)


def _gate(value: bool) -> str:
    return "passed" if value else "failed"


def _codes_or_missing(values) -> str:
    if not values:
        return "not evaluated"
    return ", ".join(_safe_code(value) for value in values)


def _distribution_codes(values: dict[str, int]) -> str:
    return (
        "not evaluated"
        if not values
        else ", ".join(
            f"{_safe_code(key)}:{value}" for key, value in sorted(values.items())
        )
    )


def _safe_code(value: object) -> str:
    text = str(value)
    return text if _SAFE_CODE_RE.fullmatch(text) else "redacted"


__all__ = (
    "ReportSource",
    "load_markdown_report_source",
    "render_markdown_report",
    "write_markdown_report",
)
