from __future__ import annotations

from pathlib import Path

from pydantic import Field, FilePath

from penguineer.core import (
    CommandParams,
    CommandResult,
    LLMMode,
    NotImplementedYet,
    RunContext,
    command,
)


class GraphDraftParams(CommandParams):
    tags: FilePath = Field(
        description="Tag list (tag, description, unit) as CSV",
        json_schema_extra={"ui": "file", "accept": [".csv"]},
    )
    pid: FilePath | None = Field(
        None,
        description="Optional DEXPI P&ID",
        json_schema_extra={"ui": "file", "accept": [".xml"]},
    )
    out: Path = Field(Path("graph.yaml"), description="Where to write the drafted graph")
    llm: LLMMode = Field(LLMMode.none, description="LLM for relations rules cannot infer")


class GraphDraftResult(CommandResult):
    graph: str
    n_nodes: int
    n_edges: int
    n_inferred: int


@command("graph draft", params=GraphDraftParams, result=GraphDraftResult)
def graph_draft(p: GraphDraftParams, ctx: RunContext) -> GraphDraftResult:
    """Draft the plant graph from a tag list and an optional P&ID; every edge is inferred."""
    raise NotImplementedYet("graph draft", "LLM")
