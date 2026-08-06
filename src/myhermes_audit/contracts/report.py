"""Independent, strict options for the human-readable report renderer.

The renderer consumes existing versioned Audit facts.  Its own version is
deliberately independent from Result, Baseline, and Regression schemas so a
presentation change never redefines an execution fact contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictBool


REPORT_RENDER_SCHEMA_VERSION = "report-v1"
ReportRenderSchemaVersion = Literal["report-v1"]


class ReportInputType(str, Enum):
    """The only strict fact contracts accepted by the Markdown renderer."""

    AUTO = "auto"
    RUN = "run"
    BASELINE = "baseline"
    REGRESSION = "regression"


class ReportRenderOptions(BaseModel):
    """Strict, presentation-only options for a deterministic report render."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
        frozen=True,
    )

    schema_version: ReportRenderSchemaVersion = REPORT_RENDER_SCHEMA_VERSION
    input_type: ReportInputType = ReportInputType.AUTO
    include_diagnostics: StrictBool = True


__all__ = (
    "REPORT_RENDER_SCHEMA_VERSION",
    "ReportInputType",
    "ReportRenderOptions",
)
