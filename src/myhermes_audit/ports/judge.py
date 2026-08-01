"""Provider-neutral Judge port."""

from __future__ import annotations

from typing import Protocol

from myhermes_audit.contracts import JudgeRequest, JudgeResult


class JudgePort(Protocol):
    def evaluate(self, request: JudgeRequest) -> JudgeResult:
        """Evaluate one final answer without exposing provider details to core code."""

    def shutdown(self) -> None:
        """Release provider resources without issuing another model request."""


__all__ = ("JudgePort",)
