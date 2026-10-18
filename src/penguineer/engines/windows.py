"""Lot windows in material time: process values averaged over the window that made each lot.

Material that leaves the line between a lot's start and end passed a tag with dead time D to the
sample point between start - D and end - D. Each tag is averaged over its own shifted window.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from plantgraph import EdgeType, PlantGraph

from penguineer.engines.align import measuring_tags, upstream_paths


@dataclass
class TagDeadTime:
    tag: str
    dead_time_min: float
    source: str  # "graph" or "fixed"
    path: list[str]


def graph_dead_times(
    graph: PlantGraph, sample_point: str, tags: list[str], max_hops: int = 50
) -> tuple[dict[str, TagDeadTime], list[str]]:
    """Dead time from each tag's node to the sample point along flows_to edges.

    Returns the tags found upstream (the others are left out), and notes on paths with an edge
    without a dead time.
    """
    sampled = next(
        (e.to_id for e in graph.edges_of(EdgeType.samples) if e.from_id == sample_point),
        sample_point,
    )  # a sample point stands for the stream or vessel it samples
    paths = upstream_paths(graph, sampled, max_hops)
    by_tag = {t.tag: node for node, ts in measuring_tags(graph).items() for t in ts}
    out: dict[str, TagDeadTime] = {}
    notes: list[str] = []
    for tag in tags:
        node = by_tag.get(tag)
        if node is None or node not in paths:
            continue
        p = paths[node]
        if not p.known:
            notes.append(f"{tag}: an edge on the path to {sample_point} has no dead time; 0 used")
        out[tag] = TagDeadTime(tag, p.dead_time_min, "graph", p.path)
    return out, notes


def window_means(
    historian: pl.DataFrame, lots: pl.DataFrame, dead_times: dict[str, TagDeadTime]
) -> pl.DataFrame:
    """One row per lot: the lot's columns, then ``<tag>`` (mean) and ``<tag>_n`` (samples).

    ``historian`` is long (timestamp, tag, value); ``lots`` has lot, start and end in UTC.
    Samples count in [start - D, end - D). A window without samples gives a null mean.
    """
    out = lots.with_row_index("_row")
    for tag, dt in dead_times.items():
        shift = pl.duration(microseconds=int(round(dt.dead_time_min * 60e6)))
        series = historian.filter(pl.col("tag") == tag).select("timestamp", "value")
        windows = lots.with_row_index("_row").select(
            "_row", (pl.col("start") - shift).alias("_from"), (pl.col("end") - shift).alias("_to")
        )
        agg = (
            windows.join_where(
                series,
                pl.col("timestamp") >= pl.col("_from"),
                pl.col("timestamp") < pl.col("_to"),
            )
            .group_by("_row")
            .agg(pl.col("value").mean().alias(tag), pl.len().alias(f"{tag}_n"))
        )
        out = out.join(agg, on="_row", how="left").with_columns(pl.col(f"{tag}_n").fill_null(0))
    return out.sort("_row").drop("_row")
