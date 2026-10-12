"""Ingest run audit models (docs/02 §6.6).

``ingest_runs`` is insert-only apart from the terminal status update, so it
doubles as the durable single-flight lock (docs/02 §12) and as the NFR-01
data-handling audit trail.
"""

from __future__ import annotations

from datetime import date as _date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.models.weather import DATE_RE


class IngestStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FATAL = "failed_fatal"


class IngestTrigger(StrEnum):
    """``cli`` is an addition to the three triggers in docs/02 §6.6 — the
    scripted ``python -m app.cli backfill`` entrypoint (P1 DoD) is an operator
    action like ``admin`` but recorded distinctly for audit honesty."""

    SCHEDULER = "scheduler"
    ON_DEMAND = "on_demand"
    ADMIN = "admin"
    CLI = "cli"


class Window(BaseModel):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _check_date(cls, value: str) -> str:
        if not DATE_RE.match(value):
            raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
        _date.fromisoformat(value)
        return value


class IngestCounts(BaseModel):
    """Run counters (docs/02 §5.6, §6.6).

    ``rows_in`` counts *region-days* (post grid-combination, the unit we
    persist); ``cell_rows_in`` counts raw provider rows (per grid cell), which
    is why the two differ for grid regions.
    """

    rows_in: int = 0
    rows_valid: int = 0
    rows_written: int = 0
    outliers: int = 0
    missing: int = 0
    degraded_grid: int = 0
    duplicates: int = 0
    rejected: int = 0
    cell_rows_in: int = 0
    events: int = 0
    annual_stats: int = 0
    years: int = 0


class IngestError(BaseModel):
    code: str
    message: str


class IngestRun(BaseModel):
    region_id: str
    window: Window
    status: IngestStatus
    attempts: int = Field(ge=1, default=1)
    trigger: IngestTrigger
    counts: IngestCounts = Field(default_factory=IngestCounts)
    error: IngestError | None = None
    duration_ms: int | None = None
    started_at: str
    finished_at: str | None = None
