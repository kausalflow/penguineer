from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from plantgraph import EdgeType, PlantGraph, Provenance, Source, Tag
from pydantic import BaseModel, Field, FilePath

from penguineer.core import CommandParams, CommandResult, RunContext, command
from penguineer.engines.lags import LagEstimate, estimate_dead_time
from penguineer.io.graph import load_graph, save_graph
from penguineer.io.historian import read_historian


class GraphLagsParams(CommandParams):
    data: FilePath = Field(
        description="Historian export, CSV or Parquet, wide or long",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    graph: FilePath = Field(
        description="Plant graph whose flows_to edges get dead times",
        json_schema_extra={"ui": "file", "accept": [".yaml", ".yml"]},
    )
    max_lag_minutes: float = Field(240.0, gt=0, description="Longest dead time to search")
    step_seconds: float | None = Field(
        None, gt=0, description="Grid step (default: the slower tag's median period)"
    )
    z_threshold: float = Field(5.0, gt=0, description="Peak strength needed to keep a lag")
    timezone: str = Field("UTC", description="Time zone of naive timestamps in the export")
    in_place: bool = Field(
        False, description="Write the estimates back into --graph and log them in its history"
    )


class LagStatus(StrEnum):
    estimated = "estimated"
    no_clear_lag = "no_clear_lag"
    kept_confirmed = "kept_confirmed"
    no_tags = "no_tags"
    no_data = "no_data"


class EdgeLag(BaseModel):
    edge_id: str
    from_id: str
    to_id: str
    from_tag: str | None
    to_tag: str | None
    status: LagStatus
    estimate: LagEstimate | None = None


class GraphLagsResult(CommandResult):
    graph: str
    graph_out: str = Field(description="Where the updated graph was written")
    updated: int
    edges: list[EdgeLag]

    def output_paths(self) -> list[Path]:
        return [Path(self.graph_out)]

    def summary(self) -> str:
        lines = [f"graph lags: {len(self.edges)} flows_to edges, {self.updated} updated"]
        for e in self.edges:
            head = f"  {e.edge_id}: {e.from_id} -> {e.to_id}"
            if e.estimate is not None:
                est = e.estimate
                lines.append(
                    f"{head}  {e.status}: {est.lag_min:g} min "
                    f"[{est.low_min:g}, {est.high_min:g}] corr {est.corr:+.2f} z {est.z:g}"
                    f"  ({e.from_tag} -> {e.to_tag})"
                )
            else:
                lines.append(f"{head}  {e.status}")
        lines.append(f"graph: {self.graph_out}")
        return "\n".join(lines)


def node_tags(graph: PlantGraph) -> dict[str, str]:
    """The historian tag measuring each equipment or stream node (first by ID)."""
    tags = {n.id: n for n in graph.nodes if isinstance(n, Tag)}
    found: dict[str, str] = {}
    for e in sorted(graph.edges_of(EdgeType.measures), key=lambda e: e.from_id):
        tag = tags.get(e.from_id)
        if tag is not None and tag.role in (None, "measurement"):
            found.setdefault(e.to_id, tag.tag)
    return found


@command("graph lags", params=GraphLagsParams, result=GraphLagsResult)
def graph_lags(p: GraphLagsParams, ctx: RunContext) -> GraphLagsResult:
    """Estimate dead times along flows_to edges from data; confirmed edges are never changed."""
    graph = load_graph(p.graph)
    measured = node_tags(graph)
    wanted = sorted(set(measured.values()))
    frame = read_historian(p.data, timezone=p.timezone, tags=wanted).frame if wanted else None
    series = {}
    if frame is not None:
        for (tag,), part in frame.partition_by("tag", as_dict=True).items():
            series[tag] = (part["timestamp"].dt.epoch("us").to_numpy(), part["value"].to_numpy())

    results: list[EdgeLag] = []
    history: list[dict] = []
    updated_edges = []
    flows = list(graph.edges_of(EdgeType.flows_to))
    for i, edge in enumerate(flows):
        ctx.progress(i, len(flows), edge.id)
        up, down = measured.get(edge.from_id), measured.get(edge.to_id)
        item = EdgeLag(
            edge_id=edge.id,
            from_id=edge.from_id,
            to_id=edge.to_id,
            from_tag=up,
            to_tag=down,
            status=LagStatus.no_tags,
        )
        if edge.provenance == Provenance.confirmed:
            item.status = LagStatus.kept_confirmed
        elif up and down and up != down:
            if up not in series or down not in series:
                item.status = LagStatus.no_data
            else:
                est = estimate_dead_time(
                    *series[up],
                    *series[down],
                    max_lag_s=p.max_lag_minutes * 60,
                    step_s=p.step_seconds,
                    z_threshold=p.z_threshold,
                )
                item.estimate = est
                if est is None:
                    item.status = LagStatus.no_data
                elif not est.significant:
                    item.status = LagStatus.no_clear_lag
                else:
                    item.status = LagStatus.estimated
                    before = edge.model_dump(mode="json", by_alias=True, exclude_defaults=True)
                    edge = edge.model_copy(
                        update={
                            "dead_time_min": est.lag_min,
                            "dead_time_low_min": est.low_min,
                            "dead_time_high_min": est.high_min,
                            "provenance": Provenance.inferred,
                            "source": Source.data,
                            "confidence": round(min(1.0, abs(est.corr)), 3),
                        }
                    )
                    history.append(
                        {
                            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "author": "penguineer graph lags",
                            "run_id": ctx.run_id,
                            "reason": f"dead time from data: {up} -> {down}, z {est.z:g}",
                            "edge_id": edge.id,
                            "before": before,
                            "after": edge.model_dump(
                                mode="json", by_alias=True, exclude_defaults=True
                            ),
                        }
                    )
                    updated_edges.append(edge)
        results.append(item)

    by_id = {e.id: e for e in updated_edges}
    new_graph = graph.model_copy(update={"edges": [by_id.get(e.id, e) for e in graph.edges]})
    if p.in_place:
        out = p.graph
        if history:
            log = p.graph.with_name("graph.history.jsonl")
            with log.open("a", encoding="utf-8") as f:
                for event in history:
                    f.write(json.dumps(event) + "\n")
    else:
        out = ctx.run_dir / "graph.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
    save_graph(new_graph, out)
    return GraphLagsResult(
        graph=str(p.graph), graph_out=str(out), updated=len(updated_edges), edges=results
    )
