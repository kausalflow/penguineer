from __future__ import annotations

from pydantic import BaseModel, Field, FilePath

from penguineer.core import (
    CommandParams,
    CommandResult,
    LLMMode,
    NotImplementedYet,
    RunContext,
    command,
)


class InvestigateParams(CommandParams):
    lot: str = Field(description="Lot ID as it appears in the LIMS export")
    lims: FilePath = Field(
        description="LIMS export", json_schema_extra={"ui": "file", "accept": [".csv"]}
    )
    data: FilePath = Field(
        description="Historian export",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    graph: FilePath | None = Field(
        None,
        description="Plant graph (default: graph.yaml in the project)",
        json_schema_extra={"ui": "file", "accept": [".yaml", ".yml"]},
    )
    top: int = Field(5, ge=1, le=20, description="Hypotheses to report")
    llm: LLMMode = Field(LLMMode.none, description="LLM for the prose summary only")


class Hypothesis(BaseModel):
    rank: int
    cause_node: str
    direction: str
    confidence: str
    graph_path: list[str]
    evidence: list[str]


class InvestigateResult(CommandResult):
    lot: str
    hypotheses: list[Hypothesis]
    report_dir: str


@command("investigate", params=InvestigateParams, result=InvestigateResult)
def investigate(p: InvestigateParams, ctx: RunContext) -> InvestigateResult:
    """Align the data in time, rank root-cause hypotheses along graph paths, write the report."""
    raise NotImplementedYet("investigate", "Investigate")
