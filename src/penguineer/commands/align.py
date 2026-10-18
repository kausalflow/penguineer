from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, FilePath

from penguineer.core import CommandParams, CommandResult, RunContext, UsageError, command
from penguineer.engines.windows import TagDeadTime, graph_dead_times, window_means
from penguineer.io.graph import load_graph
from penguineer.io.historian import read_historian
from penguineer.io.lims import read_lots


class AlignParams(CommandParams):
    data: FilePath = Field(
        description="Historian export, CSV or Parquet, wide or long",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    lots: FilePath = Field(
        description="Lot windows: lot, start and end columns, CSV or Parquet; other columns are "
        "kept in the output",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    tags: list[str] = Field(default_factory=list, description="Tags to average (default: all)")
    dead_time_min: float | None = Field(
        None,
        ge=0,
        description="One dead time for every tag, in minutes (with --graph: for tags the graph "
        "cannot place)",
    )
    graph: FilePath | None = Field(
        None,
        description="Plant graph: each tag's dead time to --sample-point along flows_to edges",
        json_schema_extra={"ui": "file", "accept": [".yaml", ".yml"]},
    )
    sample_point: str | None = Field(
        None, description="Graph node where the lots are measured (with --graph)"
    )
    timezone: str = Field("UTC", description="Time zone of naive timestamps in the exports")
    out: Path | None = Field(
        None, description="Where to write aligned.parquet and aligned.csv (default: run folder)"
    )


class DeadTimeUsed(BaseModel):
    tag: str
    dead_time_min: float
    source: str = Field(description="graph or fixed")
    path: list[str] = Field(description="Graph nodes from the tag to the sample point")


class AlignResult(CommandResult):
    n_lots: int
    tags: list[str]
    dead_times: list[DeadTimeUsed]
    empty_windows: dict[str, int] = Field(description="Lots without samples, per tag")
    notes: list[str]
    table: str = Field(description="aligned.parquet: one row per lot")
    table_csv: str
    rows: list[dict[str, Any]] = Field(
        description="The table: the lot's columns, then <tag> (mean) and <tag>_n (samples)"
    )

    def output_paths(self) -> list[Path]:
        return [Path(self.table), Path(self.table_csv)]

    def summary(self) -> str:
        lines = [f"align: {self.n_lots} lots, {len(self.tags)} tags"]
        for d in self.dead_times:
            empty = self.empty_windows.get(d.tag, 0)
            lines.append(
                f"  {d.tag}: dead time {d.dead_time_min:g} min ({d.source})"
                + (f", {empty} lots without samples" if empty else "")
            )
        lines += [f"note: {n}" for n in self.notes]
        lines.append(f"table: {self.table}")
        return "\n".join(lines)


def _plain(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


@command("align", params=AlignParams, result=AlignResult)
def align(p: AlignParams, ctx: RunContext) -> AlignResult:
    """Average each tag over the window that made each lot, shifted back by its dead time."""
    if p.dead_time_min is None and p.graph is None:
        raise UsageError("give --dead-time-min, or --graph with --sample-point")
    if p.graph is not None and p.sample_point is None:
        raise UsageError("--graph needs --sample-point, the node where the lots are measured")
    lots = read_lots(p.lots, timezone=p.timezone, keep=True)
    hist = read_historian(p.data, timezone=p.timezone, tags=p.tags or None)
    tags = p.tags or sorted(hist.frame["tag"].unique().to_list())
    missing = sorted(set(tags) - set(hist.frame["tag"].unique().to_list()))
    if missing:
        raise UsageError(f"tags not in the historian export: {', '.join(missing)}")

    notes: list[str] = []
    dead: dict[str, TagDeadTime] = {}
    if p.graph is not None:
        dead, notes = graph_dead_times(load_graph(p.graph), p.sample_point, tags)
    placed = set(dead)
    for tag in tags:
        if tag not in dead and p.dead_time_min is not None:
            dead[tag] = TagDeadTime(tag, p.dead_time_min, "fixed", [])
    if p.graph is not None and set(tags) - placed:
        not_up = ", ".join(sorted(set(tags) - placed))
        fate = "fixed dead time used" if p.dead_time_min is not None else "left out"
        notes.append(f"not upstream of {p.sample_point} in the graph ({fate}): {not_up}")
    if not dead:
        raise UsageError("no tag has a dead time; give --dead-time-min")

    table = window_means(hist.frame, lots, dead)
    out = p.out if p.out is not None else ctx.run_dir
    out.mkdir(parents=True, exist_ok=True)
    table.write_parquet(out / "aligned.parquet")
    table.write_csv(out / "aligned.csv")
    return AlignResult(
        n_lots=table.height,
        tags=list(dead),
        dead_times=[DeadTimeUsed(**vars(d)) for d in dead.values()],
        empty_windows={t: int((table[f"{t}_n"] == 0).sum()) for t in dead},
        notes=notes,
        table=str(out / "aligned.parquet"),
        table_csv=str(out / "aligned.csv"),
        rows=[{k: _plain(v) for k, v in r.items()} for r in table.iter_rows(named=True)],
    )
