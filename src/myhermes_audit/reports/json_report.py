"""Atomic stable JSON report publication."""

from __future__ import annotations

from pathlib import Path

from myhermes_audit.artifacts import atomic_write_json
from myhermes_audit.contracts import AuditRunResult


def write_json_report(path: Path, result: AuditRunResult) -> Path:
    return atomic_write_json(path, result)


__all__ = ("write_json_report",)
