"""Graph draft, LLM pass: relations the rules could not infer, proposed and then checked.

After the rule-based pass (and the P&ID, if given), some tags still measure nothing, some valves
act on nothing, and without a P&ID there is no piping. The LLM is shown the plant's nodes, the
relations already drafted and those open tags and valves (names, units and descriptions; never
data) and proposes ``measures``, ``manipulates`` and, without piping, ``flows_to`` edges.

A proposal is kept only if :func:`check_proposal` finds nothing wrong: both ends exist and have
the right types, it places an open tag or valve (it never changes a drafted relation), it does
not repeat an edge, and piping closes no cycle. Kept edges are ``inferred`` with ``source: llm``
and a confidence below the rule-based ones; the engineer confirms them in ``graph check``'s
confirm queue like the rest. Rejected ones are reported.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from plantgraph import EDGE_ENDPOINTS, Edge, EdgeType, PlantGraph, Provenance, Source, Tag
from pydantic import BaseModel, Field

from penguineer.graph.draft import Draft, parse_tag
from penguineer.llm import DryRun, LLMError, LLMProvider, load_prompt

PROMPT = ("relations", 1)
BATCH = 40  # tags per request
CONFIDENCE = {"low": 0.3, "medium": 0.4, "high": 0.5}


class ProposedEdge(BaseModel):
    type: Literal["measures", "manipulates", "flows_to"]
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    measured_variable: str | None = None
    confidence: Literal["low", "medium", "high"]
    reason: str = Field(description="What in the input supports it, in one sentence")

    model_config = {"populate_by_name": True}


class RelationsDraft(BaseModel):
    """What the LLM is asked to return."""

    edges: list[ProposedEdge] = Field(description="Relations the input supports; may be empty")


@dataclass
class Proposals:
    kept: list[Edge] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    requests: int = 0
    failed: str | None = None  # why the LLM gave no answer, if it did not


def open_items(g: PlantGraph) -> tuple[list[Tag], list[str], bool]:
    """Tags that measure nothing, actuators that act on nothing, and whether piping is missing."""
    measuring = {e.from_id for e in g.edges if e.type == EdgeType.measures}
    acting = {e.from_id for e in g.edges if e.type == EdgeType.manipulates}
    tags = [
        n
        for n in g.nodes
        if isinstance(n, Tag)
        and n.id not in measuring
        and n.role in ("measurement", None)
        and parse_tag(n.tag).letters  # GRADE and other non-instrument tags stay out
    ]
    actuators = [n.id for n in g.nodes if n.type == "actuator" and n.id not in acting]
    no_piping = not any(e.type == EdgeType.flows_to for e in g.edges)
    return tags, actuators, no_piping


def request_input(
    g: PlantGraph, tags: list[Tag], actuators: list[str], ask_piping: bool
) -> dict[str, Any]:
    """Everything the LLM sees for one batch. ``--llm-dry-run`` shows it."""
    places = [
        {
            k: v
            for k, v in {
                "id": n.id,
                "type": n.type,
                "kind": getattr(n, "kind", None),
                "name": getattr(n, "name", None),
                "description": n.description,
            }.items()
            if v is not None
        }
        for n in g.nodes
        if n.type in ("equipment", "stream", "actuator")
    ]
    return {
        "nodes": places,
        "edges": [
            {"type": str(e.type), "from": e.from_id, "to": e.to_id}
            for e in g.edges
            if e.type in (EdgeType.measures, EdgeType.manipulates, EdgeType.flows_to)
        ],
        "tags_to_place": [
            {
                k: v
                for k, v in {
                    "id": t.id,
                    "tag": t.tag,
                    "variable": parse_tag(t.tag).variable,
                    "unit": t.unit_of_measure,
                    "description": t.description,
                }.items()
                if v is not None
            }
            for t in tags
        ],
        "actuators_to_place": actuators,
        "ask_piping": ask_piping,
    }


def _reaches(adj: dict[str, set[str]], start: str, goal: str) -> bool:
    stack, seen = [start], {start}
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        for nxt in adj[node] - seen:
            seen.add(nxt)
            stack.append(nxt)
    return False


def check_proposal(
    p: ProposedEdge,
    g: PlantGraph,
    open_tags: set[str],
    open_actuators: set[str],
    ask_piping: bool,
    flows: dict[str, set[str]],
) -> str | None:
    """Why proposal ``p`` cannot be kept, or None."""
    nodes = {n.id: n for n in g.nodes}
    a, b = nodes.get(p.from_id), nodes.get(p.to_id)
    if a is None or b is None:
        return f"{p.from_id if a is None else p.to_id} is not a node of the draft"
    allowed_from, allowed_to = EDGE_ENDPOINTS[EdgeType(p.type)]
    if (allowed_from and a.type not in allowed_from) or (allowed_to and b.type not in allowed_to):
        return f"a {p.type} edge cannot go from a {a.type} to a {b.type}"
    if p.type == "measures" and p.from_id not in open_tags:
        return f"{p.from_id} was not asked about (it already measures something, or has no role)"
    if p.type == "manipulates" and p.from_id not in open_actuators:
        return f"{p.from_id} was not asked about"
    if p.type == "flows_to":
        if not ask_piping:
            return "the draft has piping already"
        if p.from_id == p.to_id:
            return "flows into itself"
        if p.to_id in flows[p.from_id]:
            return "proposed twice"
        if _reaches(flows, p.to_id, p.from_id):
            return "would close a cycle"
    return None


def propose_relations(draft: Draft, provider: LLMProvider) -> Proposals:
    """Ask ``provider`` for the relations the draft lacks; add those that pass the checks."""
    g = draft.graph
    tags, actuators, ask_piping = open_items(g)
    out = Proposals()
    if not tags and not actuators and not ask_piping:
        return out
    prompt = load_prompt(*PROMPT)
    flows: dict[str, set[str]] = defaultdict(set)
    for e in g.edges:
        if e.type == EdgeType.flows_to:
            flows[e.from_id].add(e.to_id)
    batches = [tags[i : i + BATCH] for i in range(0, len(tags), BATCH)] or [[]]
    for i, batch in enumerate(batches):
        # Valves and piping are asked about once, with the first batch.
        batch_actuators = actuators if i == 0 else []
        batch_piping = ask_piping and i == 0
        if not batch and not batch_actuators and not batch_piping:
            continue
        out.requests += 1
        try:
            answer = provider.structured(
                prompt.with_input(request_input(g, batch, batch_actuators, batch_piping)),
                RelationsDraft,
            )
        except LLMError as e:
            out.failed = str(e)
            if isinstance(provider, DryRun):
                continue  # record every request
            break
        open_tags = {t.id for t in batch} - {
            e.from_id for e in g.edges if e.type == EdgeType.measures
        }
        open_actuators = set(batch_actuators) - {
            e.from_id for e in g.edges if e.type == EdgeType.manipulates
        }
        for p in answer.edges:
            problem = check_proposal(p, g, open_tags, open_actuators, batch_piping, flows)
            if problem:
                out.rejected.append(f"{p.from_id} -{p.type}-> {p.to_id}: {problem}")
                continue
            edge = Edge(
                id=f"draft-l{len(out.kept) + 1}",
                type=EdgeType(p.type),
                from_id=p.from_id,
                to_id=p.to_id,
                measured_variable=p.measured_variable if p.type == "measures" else None,
                provenance=Provenance.inferred,
                source=Source.llm,
                confidence=CONFIDENCE[p.confidence],
                description=p.reason,
            )
            g.edges.append(edge)
            out.kept.append(edge)
            if p.type == "flows_to":
                flows[p.from_id].add(p.to_id)
    return out
