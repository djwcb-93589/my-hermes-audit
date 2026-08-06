"""Stable aggregation and local report rendering."""

from myhermes_audit.reports.aggregate import (
    aggregate_audit,
    aggregate_cases,
    aggregate_judges,
)
from myhermes_audit.reports.console import (
    render_console_regression,
    render_console_summary,
)
from myhermes_audit.reports.json_report import write_json_report
from myhermes_audit.reports.markdown import (
    load_markdown_report_source,
    render_markdown_report,
    write_markdown_report,
)

__all__ = (
    "aggregate_audit",
    "aggregate_cases",
    "aggregate_judges",
    "render_console_summary",
    "render_console_regression",
    "load_markdown_report_source",
    "render_markdown_report",
    "write_markdown_report",
    "write_json_report",
)
