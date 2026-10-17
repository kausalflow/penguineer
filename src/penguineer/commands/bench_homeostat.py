from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import DirectoryPath, Field

from penguineer.core import CommandParams, CommandResult, NotImplementedYet, RunContext, command


class Split(StrEnum):
    train = "train"
    validation = "validation"
    hidden = "hidden"


class BenchHomeostatParams(CommandParams):
    scenarios: Split = Field(Split.validation, description="Which scenario split to run")
    bundles: DirectoryPath | None = Field(
        None,
        description="Folder of exported homeostat scenario bundles",
        json_schema_extra={"ui": "folder"},
    )
    limit: int | None = Field(None, ge=1, description="Run at most this many scenarios")


class BenchHomeostatResult(CommandResult):
    split: Split
    n_scenarios: int
    hit_at_3: float
    wrong_confident_rate: float
    data_fault_catch_rate: float
    report: Path


@command("bench homeostat", params=BenchHomeostatParams, result=BenchHomeostatResult)
def bench_homeostat(p: BenchHomeostatParams, ctx: RunContext) -> BenchHomeostatResult:
    """Run investigate on homeostat plants with injected faults and score it."""
    raise NotImplementedYet("bench homeostat", "Bench")
