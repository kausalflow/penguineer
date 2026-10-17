"""Material-time alignment: paths to the sampled node, cumulative dead times, a common grid.

Every series is put on one grid in the time of the sampled node (where the lab sample is
taken). A tag on a node with cumulative dead time D to the sampled node is read at t - D, so
that its value at grid time t is the material that reaches the sample point at t.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
from plantgraph import EdgeType, PlantGraph, Tag

_US = 1_000_000


@dataclass
class NodePath:
    """How material gets from ``node`` to the sampled node."""

    node: str
    path: list[str]  # node ids from ``node`` to the sampled node
    dead_time_min: float
    low_min: float
    high_min: float
    known: bool = True  # False when an edge on the path has no dead time
    hops: int = 0


def upstream_paths(graph: PlantGraph, target: str, max_hops: int) -> dict[str, NodePath]:
    """Nodes upstream of ``target`` along flows_to edges (recycles skipped), nearest first.

    The first path found (fewest hops) is kept for each node.
    """
    into: dict[str, list] = {}
    for e in graph.edges_of(EdgeType.flows_to):
        if not e.recycle:
            into.setdefault(e.to_id, []).append(e)
    found = {target: NodePath(target, [target], 0.0, 0.0, 0.0, True, 0)}
    queue = deque([target])
    while queue:
        here = found[queue.popleft()]
        if here.hops >= max_hops:
            continue
        for e in sorted(into.get(here.node, []), key=lambda e: e.from_id):
            if e.from_id in found:
                continue
            known = e.dead_time_min is not None
            d = e.dead_time_min or 0.0
            lo = e.dead_time_low_min if e.dead_time_low_min is not None else d
            hi = e.dead_time_high_min if e.dead_time_high_min is not None else d
            found[e.from_id] = NodePath(
                e.from_id,
                [e.from_id, *here.path],
                here.dead_time_min + d,
                here.low_min + lo,
                here.high_min + hi,
                here.known and known,
                here.hops + 1,
            )
            queue.append(e.from_id)
    return found


def measuring_tags(graph: PlantGraph) -> dict[str, list[Tag]]:
    """Node id -> the Tag nodes that measure it."""
    tags = {n.id: n for n in graph.nodes if isinstance(n, Tag)}
    out: dict[str, list[Tag]] = {}
    for e in graph.edges_of(EdgeType.measures):
        if e.from_id in tags:
            out.setdefault(e.to_id, []).append(tags[e.from_id])
    for v in out.values():
        v.sort(key=lambda t: t.id)
    return out


@dataclass
class Grid:
    start_us: int
    step_us: int
    n: int
    times: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.times = self.start_us + self.step_us * np.arange(self.n, dtype=np.int64)

    @classmethod
    def spanning(cls, start_us: int, end_us: int, step_us: int) -> Grid:
        return cls(start_us, step_us, int((end_us - start_us) // step_us) + 1)

    def index(self, t_us: int) -> int:
        return int(np.clip((t_us - self.start_us) // self.step_us, 0, self.n))

    def mask(self, start_us: int, end_us: int) -> np.ndarray:
        """Grid points in [start, end)."""
        return (self.times >= start_us) & (self.times < end_us)


def median_period_us(t: np.ndarray) -> int:
    return int(np.median(np.diff(t))) if t.size > 1 else 0


def resample(
    t: np.ndarray, x: np.ndarray, at: np.ndarray, max_gap_us: int | None = None
) -> np.ndarray:
    """Linear interpolation of (t, x) at times ``at``; NaN outside the data and across gaps."""
    out = np.full(at.size, np.nan)
    if t.size == 0:
        return out
    inside = (at >= t[0]) & (at <= t[-1])
    out[inside] = np.interp(at[inside].astype(float), t.astype(float), x.astype(float))
    if max_gap_us is not None and t.size > 1:
        right = np.clip(np.searchsorted(t, at, side="left"), 1, t.size - 1)
        span = t[right] - t[right - 1]
        exact = t[right] == at
        out[inside & (span > max_gap_us) & ~exact] = np.nan
    return out


def aligned(
    t: np.ndarray, x: np.ndarray, grid: Grid, dead_time_min: float, gap_factor: float = 5.0
) -> np.ndarray:
    """The series on ``grid`` in sampled-node time: its value at t - dead time."""
    period = median_period_us(t)
    shift = int(round(dead_time_min * 60 * _US))
    return resample(t, x, grid.times - shift, int(period * gap_factor) if period else None)
