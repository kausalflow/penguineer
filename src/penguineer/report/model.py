"""The investigation report: Pydantic models whose JSON form is ``report.json``.

HTML and Markdown are rendered from these models, never computed separately.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from penguineer.engines.health import HealthFinding


class HypothesisKind(StrEnum):
    process = "process"  # a measured process variable moved and the quality followed
    setpoint_change = "setpoint_change"  # an operator or recipe changed a loop setpoint
    lost_control = "lost_control"  # a loop's output saturated and its PV deviated
    unmeasured_disturbance = "unmeasured_disturbance"  # a loop absorbed something unmeasured
    measurement_fault = "measurement_fault"  # the data itself is wrong


class TestName(StrEnum):
    effect_size = "effect_size"
    onset = "onset"
    lag_correlation = "lag_correlation"


class TestResult(BaseModel):
    name: TestName
    strength: float | None = Field(description="0 to 1; null when the test could not run")
    value: float | None = Field(None, description="The test statistic (z, minutes, r)")
    detail: str


class EvidenceItem(BaseModel):
    id: str = Field(description="E001, E002, ...; stable within one report")
    kind: Literal["plot", "loop", "health", "explain_away"]
    title: str
    path: str | None = Field(None, description="File under evidence/, relative to the report")
    data: dict[str, float | str | None] = Field(default_factory=dict)


class RuledOut(BaseModel):
    cause: str = Field(description="Node or tag ID")
    reason: str


class Hypothesis(BaseModel):
    rank: int
    kind: HypothesisKind
    cause_node: str
    cause_tag: str | None = Field(None, description="Historian tag the evidence comes from")
    direction: Literal["high", "low", "changed"]
    magnitude: float | None = Field(None, description="Event median minus reference median")
    unit: str | None = None
    window_start: datetime | None = Field(None, description="Deviation start, at the cause")
    window_end: datetime | None = None
    graph_path: list[str] = Field(description="Node IDs from the cause to the sampled node")
    dead_time_min: float = Field(description="Cumulative dead time to the sampled node")
    tests: list[TestResult]
    score: float
    confidence: Literal["high", "medium", "low"]
    ruled_out: list[RuledOut] = Field(
        default_factory=list, description="Alternatives this hypothesis explains away"
    )
    health_warnings: list[str] = Field(
        default_factory=list, description="IDs of health findings on this tag in the window"
    )
    evidence: list[str] = Field(default_factory=list, description="Evidence item IDs")
    summary: str


class QualityEvent(BaseModel):
    lot: str
    property: str
    quality_node: str
    value: float
    spec_low: float | None
    spec_high: float | None
    unit: str | None
    direction: Literal["high", "low"]
    sampled_at: datetime
    sample_point: str | None
    sampled_node: str
    window_start: datetime = Field(description="Lot window at the sampled node")
    window_end: datetime
    onset: datetime | None = Field(None, description="When the online quality signal moved")
    proxy_tag: str | None = Field(None, description="Online analyzer used for timing")


class ReferenceWindow(BaseModel):
    lot: str
    start: datetime
    end: datetime
    value: float


class Report(BaseModel):
    penguineer_version: str
    run_id: str
    created_at: datetime
    event: QualityEvent
    regime: dict[str, float] = Field(description="Mode tag values during the lot")
    reference: list[ReferenceWindow]
    grid_step_s: float
    candidates: int = Field(description="Candidate tags tested")
    hypotheses: list[Hypothesis]
    ruled_out: list[RuledOut]
    evidence: list[EvidenceItem]
    health: list[HealthFinding] = Field(description="Health findings on candidate tags")
    notes: list[str]
    calibration: str = Field(description="How the confidence labels were set")
