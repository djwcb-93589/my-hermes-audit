"""Human-readable P1 console summary without unsupported placeholder scores."""

from __future__ import annotations

from myhermes_audit.contracts import AuditRunResult, TrialStatus


def render_console_summary(result: AuditRunResult) -> str:
    summary = result.summary
    lines = [
        f"MyHermes Audit: {result.suite_id}",
        "",
        f"Subject commit:    {result.subject_fingerprint.git_commit}",
        f"Cases:             {summary.case_count}",
        f"Trials:            {summary.trial_count}",
        f"Passed:            {summary.passed_count}",
        f"Task success:      {summary.pass_rate * 100:.1f}%",
        f"Tool correctness:  {_percent_or_missing(summary.tool_correctness_rate)}",
        f"Duration P50:      {_duration_or_missing(summary.duration_p50_ms)}",
        f"Duration P95:      {_duration_or_missing(summary.duration_p95_ms)}",
        f"Total tokens:      {_integer_or_missing(summary.total_tokens)}",
    ]
    failures = [trial for trial in result.trials if trial.passed is not True]
    if failures:
        lines.extend(("", "Failures:"))
        for trial in failures:
            lines.append(f"- {trial.case_id} trial {trial.trial_number}: {_failure(trial)}")
    return "\n".join(lines) + "\n"


def _failure(trial) -> str:
    if trial.status is not TrialStatus.COMPLETED:
        if trial.error is not None:
            return f"{trial.status.value}: {trial.error.error_type}"
        return trial.status.value
    failed_metrics = [metric for metric in trial.metrics if metric.passed is not True]
    if failed_metrics:
        return ", ".join(metric.metric_name for metric in failed_metrics)
    return "required evaluator did not pass"


def _percent_or_missing(value: float | None) -> str:
    return "not evaluated" if value is None else f"{value * 100:.1f}%"


def _duration_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else f"{value / 1000:.1f}s"


def _integer_or_missing(value: int | None) -> str:
    return "not evaluated" if value is None else str(value)


__all__ = ("render_console_summary",)
