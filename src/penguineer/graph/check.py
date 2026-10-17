"""Graph check: plant rules on top of the plantgraph schema, plus the confirm queue."""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from plantgraph import EDGE_ENDPOINTS, EdgeType, Loop, PlantGraph, Provenance
from pydantic import BaseModel


class IssueLevel(StrEnum):
    error = "error"
    warning = "warning"


class GraphIssue(BaseModel):
    level: IssueLevel
    code: str
    message: str
    ids: list[str]


class ConfirmItem(BaseModel):
    edge_id: str
    type: EdgeType
    from_id: str
    to_id: str
    confidence: float
    dependent_qualities: int


def check_graph(graph: PlantGraph) -> tuple[list[GraphIssue], list[ConfirmItem]]:
    issues: list[GraphIssue] = []

    def err(code: str, message: str, *ids: str, level: IssueLevel = IssueLevel.error) -> None:
        issues.append(GraphIssue(level=level, code=code, message=message, ids=list(ids)))

    seen: dict[str, int] = defaultdict(int)
    for element in [*graph.nodes, *graph.edges]:
        seen[element.id] += 1
    for element_id, count in sorted(seen.items()):
        if count > 1:
            err("duplicate_id", f"ID {element_id!r} is used {count} times", element_id)

    types = {n.id: n.type for n in graph.nodes}
    for e in graph.edges:
        for end in (e.from_id, e.to_id):
            if end not in types:
                err("dangling_edge", f"edge {e.id!r} points to unknown node {end!r}", e.id, end)
        allowed_from, allowed_to = EDGE_ENDPOINTS[e.type]
        if e.from_id in types and allowed_from and types[e.from_id] not in allowed_from:
            err(
                "edge_endpoint",
                f"{e.type} edge {e.id!r} cannot start at a {types[e.from_id]} node",
                e.id,
            )
        if e.to_id in types and allowed_to and types[e.to_id] not in allowed_to:
            err(
                "edge_endpoint",
                f"{e.type} edge {e.id!r} cannot end at a {types[e.to_id]} node",
                e.id,
            )
        if e.valid_from and e.valid_to and e.valid_to <= e.valid_from:
            err("valid_time", f"edge {e.id!r} has valid_to before valid_from", e.id)

    for n in graph.nodes:
        if isinstance(n, Loop):
            for role in ("pv", "op"):
                ref = getattr(n, role)
                if ref is None:
                    err("loop_incomplete", f"loop {n.id!r} has no {role.upper()} tag", n.id)
            for role in ("pv", "sp", "op", "mode"):
                ref = getattr(n, role)
                if ref is not None and types.get(ref) != "tag":
                    err(
                        "loop_reference",
                        f"loop {n.id!r} {role} {ref!r} is not a tag node",
                        n.id,
                        ref,
                    )

    # flows_to cycles are allowed only through an edge flagged as recycle.
    downstream: dict[str, list[str]] = defaultdict(list)
    upstream_count: dict[str, int] = defaultdict(int)
    for e in graph.edges_of(EdgeType.flows_to):
        upstream_count[e.to_id] += 1
        if not e.recycle:
            downstream[e.from_id].append(e.to_id)
    cycle = _find_cycle(downstream)
    if cycle:
        err(
            "unflagged_cycle",
            "flows_to cycle without a recycle edge: " + " -> ".join(cycle),
            *cycle,
        )

    sampled_by: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges_of(EdgeType.samples):
        sampled_by[e.from_id].append(e.to_id)
    quality_sources: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges_of(EdgeType.reported_as):
        quality_sources[e.to_id].extend(sampled_by.get(e.from_id, []))
    has_flow = any(True for _ in graph.edges_of(EdgeType.flows_to))
    for q in graph.nodes_of("quality"):
        sources = quality_sources.get(q.id, [])
        if not sources:
            err("quality_unreachable", f"quality {q.id!r} has no sample point on the plant", q.id)
        elif has_flow and not any(upstream_count.get(s) for s in sources):
            err(
                "quality_unreachable",
                f"nothing flows_to the node where {q.id!r} is sampled",
                q.id,
                level=IssueLevel.warning,
            )

    queue = _confirm_queue(graph, downstream, quality_sources)
    return issues, queue


def _find_cycle(adj: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in adj.get(node, []):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt) :] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(adj):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def _confirm_queue(
    graph: PlantGraph, downstream: dict[str, list[str]], quality_sources: dict[str, list[str]]
) -> list[ConfirmItem]:
    def reachable(start: str) -> set[str]:
        seen, todo = {start}, [start]
        while todo:
            for nxt in downstream.get(todo.pop(), []):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    items = []
    for e in graph.edges:
        if e.provenance != Provenance.inferred:
            continue
        reach = reachable(e.to_id)
        deps = sum(1 for srcs in quality_sources.values() if reach.intersection(srcs))
        items.append(
            ConfirmItem(
                edge_id=e.id,
                type=e.type,
                from_id=e.from_id,
                to_id=e.to_id,
                confidence=e.confidence,
                dependent_qualities=deps,
            )
        )
    items.sort(key=lambda i: (-i.dependent_qualities, i.confidence, i.edge_id))
    return items
