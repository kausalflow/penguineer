from __future__ import annotations

from pathlib import Path

from pydantic import Field, FilePath

from penguineer.core import (
    CommandParams,
    CommandResult,
    LLMMode,
    RunContext,
    UsageError,
    command,
)
from penguineer.graph.check import check_graph
from penguineer.graph.draft import apply_pid, draft_from_tags, read_tag_list
from penguineer.io.dexpi import read_dexpi
from penguineer.io.graph import save_graph


class GraphDraftParams(CommandParams):
    tags: FilePath = Field(
        description="Tag list as CSV: tag, and optionally description, unit, equipment, "
        "range_low, range_high",
        json_schema_extra={"ui": "file", "accept": [".csv"]},
    )
    pid: FilePath | None = Field(
        None,
        description="DEXPI P&ID (Proteus XML): equipment, piping, where sensors and valves sit",
        json_schema_extra={"ui": "file", "accept": [".xml"]},
    )
    out: Path = Field(
        Path("graph.yaml"), description="Where to write the drafted graph (relative: the project)"
    )
    unit: str | None = Field(None, description="Unit name (default: the project folder's name)")
    force: bool = Field(False, description="Overwrite an existing graph")
    llm: LLMMode = Field(LLMMode.none, description="LLM for relations rules cannot infer")


class GraphDraftResult(CommandResult):
    graph: str
    n_nodes: int
    n_edges: int
    n_inferred: int
    n_tags: int
    n_loops: int
    n_actuators: int
    n_equipment: int
    n_flows: int = Field(description="flows_to edges, from the P&ID's piping")
    unparsed: list[str] = Field(description="Tags whose names do not follow ISA-5.1")
    warnings: list[str]
    notes: list[str]

    def output_paths(self) -> list[Path]:
        return [Path(self.graph)]

    def summary(self) -> str:
        lines = [
            f"graph draft: {self.n_tags} tags, {self.n_loops} loops, {self.n_actuators} valves, "
            f"{self.n_equipment} equipment; {self.n_edges} edges ({self.n_flows} flows_to), "
            "all inferred",
            f"wrote {self.graph}",
        ]
        if self.unparsed:
            shown = ", ".join(self.unparsed[:10]) + (" ..." if len(self.unparsed) > 10 else "")
            lines.append(f"{len(self.unparsed)} tags without an ISA-5.1 name: {shown}")
        lines += [f"warning: {w}" for w in self.warnings]
        lines += [f"note: {n}" for n in self.notes]
        lines.append(f"next: penguineer graph check --graph {self.graph}")
        return "\n".join(lines)


@command("graph draft", params=GraphDraftParams, result=GraphDraftResult)
def graph_draft(p: GraphDraftParams, ctx: RunContext) -> GraphDraftResult:
    """Draft the plant graph from a tag list and an optional P&ID; every edge is inferred."""
    out = p.out if p.out.is_absolute() else ctx.project_root / p.out
    if out.exists() and not p.force:
        raise UsageError(f"{out} exists; pass --force to overwrite it, or --out another file")
    rows = read_tag_list(p.tags)
    pid = read_dexpi(p.pid) if p.pid is not None else None
    draft = draft_from_tags(rows, unit=p.unit or ctx.project_root.name)
    if pid is not None:
        apply_pid(draft, pid)
    notes = []
    if p.llm != LLMMode.none:
        notes.append(
            f"--llm {p.llm} is not used yet: this draft is rule-based; LLM-proposed relations "
            "come with the rest of the LLM milestone"
        )
    if pid is None:
        notes.append(
            "no piping (flows_to) in a tag list: add it, give the P&ID with --pid, or confirm "
            "what the LLM proposes"
        )
    g = draft.graph
    issues, _ = check_graph(g)
    # Structural errors would be a bug in the draft; an incomplete loop is the tag list's.
    errors = [i for i in issues if i.code in ("duplicate_id", "dangling_edge", "edge_endpoint")]
    if errors:
        raise UsageError("the draft fails graph check: " + "; ".join(i.message for i in errors))
    out.parent.mkdir(parents=True, exist_ok=True)
    save_graph(g, out)
    count = lambda t: sum(1 for n in g.nodes if n.type == t)  # noqa: E731
    return GraphDraftResult(
        graph=str(out),
        n_nodes=len(g.nodes),
        n_edges=len(g.edges),
        n_inferred=sum(1 for e in g.edges if e.provenance == "inferred"),
        n_tags=count("tag"),
        n_loops=count("loop"),
        n_actuators=count("actuator"),
        n_equipment=count("equipment"),
        n_flows=sum(1 for e in g.edges if e.type == "flows_to"),
        unparsed=draft.unparsed,
        warnings=draft.warnings,
        notes=notes,
    )
