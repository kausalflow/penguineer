from __future__ import annotations

import json
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
from penguineer.llm import DryRun, load_config, make_provider


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
    llm_model: str | None = Field(
        None, description="Model for --llm (default: llm.model in penguineer.yaml)"
    )
    llm_dry_run: bool = Field(
        False, description="Write what would be sent to the LLM, and send nothing"
    )


class GraphDraftResult(CommandResult):
    graph: str
    n_nodes: int
    n_edges: int
    n_inferred: int
    n_tags: int
    n_loops: int
    n_actuators: int
    n_equipment: int
    n_flows: int = Field(description="flows_to edges, from the P&ID's piping or the LLM")
    n_llm: int = Field(0, description="Edges proposed by the LLM and kept")
    unparsed: list[str] = Field(description="Tags whose names do not follow ISA-5.1")
    warnings: list[str]
    notes: list[str]
    llm_payloads: str | None = Field(
        None, description="With --llm-dry-run: the file holding what would have been sent"
    )

    def output_paths(self) -> list[Path]:
        return [Path(self.graph)] + ([Path(self.llm_payloads)] if self.llm_payloads else [])

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
        if self.llm_payloads:
            lines.append(f"LLM requests, not sent (--llm-dry-run): {self.llm_payloads}")
        lines.append(f"next: penguineer graph check --graph {self.graph}")
        return "\n".join(lines)


@command("graph draft", params=GraphDraftParams, result=GraphDraftResult)
def graph_draft(p: GraphDraftParams, ctx: RunContext) -> GraphDraftResult:
    """Draft the plant graph from a tag list and an optional P&ID; every edge is inferred."""
    if p.llm_dry_run and p.llm == LLMMode.none:
        raise UsageError("--llm-dry-run needs --llm local or --llm api")
    out = p.out if p.out.is_absolute() else ctx.project_root / p.out
    if out.exists() and not p.force:
        raise UsageError(f"{out} exists; pass --force to overwrite it, or --out another file")
    rows = read_tag_list(p.tags)
    pid = read_dexpi(p.pid) if p.pid is not None else None
    draft = draft_from_tags(rows, unit=p.unit or ctx.project_root.name)
    if pid is not None:
        apply_pid(draft, pid)
    notes = []
    provider = make_provider(
        load_config(ctx.project_root, p.llm, p.llm_model),
        ctx.cache_dir / "llm" if ctx.cache_dir else None,
        dry_run=p.llm_dry_run,
    )
    proposals = None
    if provider is not None:
        from penguineer.graph.propose import PROMPT, propose_relations

        ctx.llm_provider, ctx.llm_model = provider.name, provider.model
        ctx.llm_prompts.append(f"{PROMPT[0]}@{PROMPT[1]}")
        proposals = propose_relations(draft, provider)
        if proposals.failed and not isinstance(provider, DryRun):
            notes.append(f"the LLM gave no answer ({proposals.failed}); the draft is rule-based")
        elif proposals.requests:
            notes.append(
                f"the LLM proposed {len(proposals.kept) + len(proposals.rejected)} relations; "
                f"{len(proposals.kept)} kept (source llm), {len(proposals.rejected)} rejected"
            )
        notes += [f"rejected LLM proposal {r}" for r in proposals.rejected]
    if pid is None and not any(e.type == "flows_to" for e in draft.graph.edges):
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
    payloads = None
    if isinstance(provider, DryRun):
        payloads = out.with_name("llm-request.json")
        payloads.write_text(json.dumps(provider.sent, indent=2, ensure_ascii=False) + "\n")
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
        n_llm=len(proposals.kept) if proposals else 0,
        llm_payloads=str(payloads) if payloads else None,
    )
