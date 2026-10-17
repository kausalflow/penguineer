from __future__ import annotations

from datetime import datetime
from pathlib import Path

from plantgraph import Loop, Tag
from pydantic import Field, FilePath

from penguineer.core import CommandParams, CommandResult, RunContext, command
from penguineer.engines.health import (
    SEVERITY_ORDER,
    HealthCheck,
    HealthConfig,
    HealthFinding,
    Severity,
    TagHealth,
    assess,
)
from penguineer.io.graph import load_graph
from penguineer.io.historian import read_historian


class DataHealthParams(CommandParams):
    data: FilePath = Field(
        description="Historian export, CSV or Parquet, wide or long",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    graph: FilePath | None = Field(
        None,
        description="Plant graph; instrument ranges and loop setpoints come from it",
        json_schema_extra={"ui": "file", "accept": [".yaml", ".yml"]},
    )
    start: datetime | None = Field(None, description="Window start (inclusive)")
    end: datetime | None = Field(None, description="Window end (exclusive)")
    tags: list[str] = Field(default_factory=list, description="Only check these tags")
    timezone: str = Field("UTC", description="Time zone of naive timestamps in the export")
    gap_factor: float = Field(3.0, gt=1, description="Gap when an interval exceeds this × median")
    flatline_minutes: float = Field(60.0, gt=0, description="Shortest frozen run to report")
    deadband: float = Field(0.0, ge=0, description="Changes up to this count as frozen")
    spike_z: float = Field(8.0, gt=0, description="Robust z-score on first differences")
    out: Path | None = Field(None, description="Where to write health.json (default: run folder)")


class DataHealthResult(CommandResult):
    data: str
    window_start: datetime
    window_end: datetime
    n_tags: int
    n_samples: int
    layout: str
    checks: list[HealthCheck]
    checks_not_run: list[str]
    counts: dict[Severity, int]
    findings: list[HealthFinding]
    tags: list[TagHealth]
    notes: list[str]
    health_json: str = Field(description="Path of the written health.json")

    def output_paths(self) -> list[Path]:
        return [Path(self.health_json)]

    def summary(self) -> str:
        lines = [
            f"data health: {self.n_tags} tags, {self.n_samples} samples, "
            f"{self.window_start:%Y-%m-%d %H:%M} to {self.window_end:%Y-%m-%d %H:%M} UTC",
            f"findings: {len(self.findings)} ("
            + ", ".join(f"{self.counts.get(s, 0)} {s}" for s in reversed(list(Severity)))
            + ")",
        ]
        if self.findings:
            width = max(len(f.tag or "-") for f in self.findings)
            lines.append("")
            for f in sorted(self.findings, key=lambda f: -SEVERITY_ORDER[f.severity])[:30]:
                lines.append(
                    f"  {f.severity:<8} {(f.tag or '-'):<{width}}  {f.check:<12} "
                    f"{f.start:%Y-%m-%d %H:%M}  {f.detail}"
                )
            if len(self.findings) > 30:
                lines.append(f"  ... {len(self.findings) - 30} more in health.json")
        for note in self.notes:
            lines.append(f"note: {note}")
        lines.append(f"health.json: {self.health_json}")
        return "\n".join(lines)


@command("data health", params=DataHealthParams, result=DataHealthResult)
def data_health(p: DataHealthParams, ctx: RunContext) -> DataHealthResult:
    """Flag frozen tags, gaps, spikes, out-of-range values and timestamp problems."""
    hist = read_historian(
        p.data, timezone=p.timezone, start=p.start, end=p.end, tags=p.tags or None
    )
    ranges: dict[str, tuple[float | None, float | None]] = {}
    allow_flat: set[str] = set()
    notes: list[str] = []
    if p.graph is not None:
        graph = load_graph(p.graph)
        by_id = {n.id: n for n in graph.nodes}
        for node in graph.nodes:
            if isinstance(node, Tag) and node.role in ("setpoint", "mode", "setting"):
                allow_flat.add(node.tag)
            if isinstance(node, Tag) and (
                node.range_low is not None or node.range_high is not None
            ):
                ranges[node.tag] = (node.range_low, node.range_high)
            if isinstance(node, Loop):
                # Setpoints and mode tags are legitimately constant for long stretches.
                for ref in (node.sp, node.mode):
                    if ref is not None and isinstance(by_id.get(ref), Tag):
                        allow_flat.add(by_id[ref].tag)
    else:
        notes.append("no --graph given: out-of-range check skipped")

    config = HealthConfig(
        gap_factor=p.gap_factor,
        flatline_min_minutes=p.flatline_minutes,
        deadband=p.deadband,
        spike_z=p.spike_z,
    )
    findings, tags = assess(
        hist.frame, config, ranges, allow_flat, progress=ctx.progress, cancelled=ctx.cancelled
    )
    if hist.nonexistent_local_times:
        notes.append(
            f"{hist.nonexistent_local_times} timestamps do not exist in {p.timezone} "
            "(daylight-saving change) and were dropped"
        )
    if hist.dropped_columns:
        notes.append("non-numeric columns skipped: " + ", ".join(hist.dropped_columns))

    checks = [HealthCheck.gap, HealthCheck.flatline, HealthCheck.spike, HealthCheck.timestamp]
    if ranges:
        checks.insert(2, HealthCheck.out_of_range)
    counts = {s: sum(1 for f in findings if f.severity == s) for s in Severity}
    out = p.out if p.out is not None else ctx.run_dir / "health.json"
    ts = hist.frame["timestamp"]
    result = DataHealthResult(
        data=str(p.data),
        window_start=ts.min(),
        window_end=ts.max(),
        n_tags=len(tags),
        n_samples=hist.frame.height,
        layout=hist.layout,
        checks=checks,
        checks_not_run=["analyzer_vs_lab", "clock_offset_vs_lab"],
        counts=counts,
        findings=findings,
        tags=tags,
        notes=notes,
        health_json=str(out),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.model_dump_json(indent=2, exclude={"health_json"}) + "\n")
    return result
