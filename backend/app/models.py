from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .errors import Status


@dataclass
class Calc:
    """Output of a pure metric calculation over real source records."""

    status: Status
    value: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)
    sample_size: int | None = None
    message: str | None = None


class MetricResult(BaseModel):
    id: str
    name: str
    status: Status
    value: float | None = None
    unit: str = ""
    summary: dict[str, Any] = Field(default_factory=dict)
    details: list[dict[str, Any]] = Field(default_factory=list)
    sampleSize: int | None = None
    message: str | None = None
    definition: str = ""
    sources: list[str] = Field(default_factory=list)
    scopeLevel: str = "project"
    timeRange: dict[str, str] | None = None
    fetchedAt: datetime | None = None
    lastSuccessfulFetch: datetime | None = None
    stale: bool = False


class DashboardResponse(BaseModel):
    organization: str
    project: str
    team: str | None
    person: dict[str, Any] | None
    timeRange: dict[str, str]
    iteration: dict[str, Any] | None
    generatedAt: datetime
    freshnessWindowMinutes: int
    metrics: list[MetricResult]
