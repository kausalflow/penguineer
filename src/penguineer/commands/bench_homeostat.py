from __future__ import annotations

import csv
import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, DirectoryPath, Field

from penguineer.core import CommandParams, CommandResult, RunContext, UsageError, command


class Split(StrEnum):
    train = "train"
    validation = "validation"
    hidden = "hidden"


class BenchHomeostatParams(CommandParams):
    scenarios: Split = Field(Split.validation, description="Which scenario split to run")
    bundles: DirectoryPath | None = Field(
        None,
        description="Folder of exported scenario bundles (default: generate them with "
        "Homeostat into .penguineer/bench/ in the project)",
        json_schema_extra={"ui": "folder"},
    )
    per_fault: int = Field(12, ge=1, le=500, description="Plants per fault type to generate")
    faults: list[str] | None = Field(None, description="Fault types to generate (default: all)")
    split_seed: int | None = Field(
        None,
        description="Seed of the split (train and validation have fixed public seeds; "
        "the hidden split needs its secret one)",
    )
    limit: int | None = Field(None, ge=1, description="Run at most this many scenarios")
    out: Path | None = Field(
        None, description="Folder for bench.json and scenarios.csv (default: the run folder)"
    )


class FaultTypeRow(BaseModel):
    n: int
    hit_at_1: float
    hit_at_3: float


class BenchHomeostatResult(CommandResult):
    split: Split
    n_scenarios: int
    skipped: int = Field(description="Generated plants where no lot went off spec")
    errors: int = Field(description="Scenarios where investigate stopped with an error")
    hit_at_1: float | None
    hit_at_3: float | None
    hit_at_3_interval: tuple[float, float] | None = Field(description="95% Wilson interval")
    wrong_confident_rate: float | None
    data_fault_catch_rate: float | None
    by_fault_type: dict[str, FaultTypeRow]
    report: Path
    scenarios_csv: Path

    def output_paths(self) -> list[Path]:
        return [self.report, self.scenarios_csv]

    def summary(self) -> str:
        def pct(v: float | None) -> str:
            return "n/a" if v is None else f"{v:.0%}"

        lines = [
            f"bench homeostat: {self.n_scenarios} {self.split} scenarios"
            + (f" ({self.skipped} plants skipped: no lot off spec)" if self.skipped else ""),
            "",
            f"  hit@1                 {pct(self.hit_at_1)}",
            f"  hit@3                 {pct(self.hit_at_3)}"
            + (
                f"  (95% {self.hit_at_3_interval[0]:.0%} to {self.hit_at_3_interval[1]:.0%})"
                if self.hit_at_3_interval
                else ""
            ),
            f"  wrong-confident rate  {pct(self.wrong_confident_rate)}",
            f"  data-fault catch rate {pct(self.data_fault_catch_rate)}",
            "",
            "  fault type              n  hit@1  hit@3",
        ]
        for name, row in self.by_fault_type.items():
            lines.append(f"  {name:<22} {row.n:>2}  {row.hit_at_1:>5.0%}  {row.hit_at_3:>5.0%}")
        if self.errors:
            lines.append(
                f"\n{self.errors} scenarios stopped with an error; see {self.scenarios_csv}"
            )
        lines.append(f"report: {self.report}")
        return "\n".join(lines)


def _plant_hash() -> str:
    from penguineer.bench.plant import BASE, FAULTS, ONSET

    blob = json.dumps(
        [BASE, ONSET, {k: [f.intervention, f.root_cause, f.kind] for k, f in FAULTS.items()}],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def generated_bundles(
    root: Path, split: Split, split_seed: int | None, per_fault: int, faults: list[str] | None, ctx
) -> Path:
    """Bundles for a split, generated once and reused while the plant and Homeostat match."""
    from penguineer.bench.export import _homeostat, export_split
    from penguineer.bench.plant import SPLIT_SEEDS

    if split_seed is None:
        if split == Split.hidden:
            raise UsageError("the hidden split needs its secret --split-seed")
        split_seed = SPLIT_SEEDS[split]
    version = _homeostat().__version__
    key = f"{split}-{split_seed}-{per_fault}-{'+'.join(sorted(faults)) if faults else 'all'}"
    folder = root / f"{key}-{_plant_hash()}-homeostat{version}"
    if (folder / "index.json").exists():
        return folder
    export_split(
        folder, split_seed=split_seed, per_fault=per_fault, faults=faults, progress=ctx.progress
    )
    return folder


@command("bench homeostat", params=BenchHomeostatParams, result=BenchHomeostatResult)
def bench_homeostat(p: BenchHomeostatParams, ctx: RunContext) -> BenchHomeostatResult:
    """Run investigate on homeostat plants with injected faults and score it."""
    from penguineer.bench.score import run_bench
    from penguineer.io.homeostat import bundles_in

    if p.bundles is not None:
        folder = p.bundles
    else:
        folder = generated_bundles(
            ctx.project_root / ".penguineer" / "bench",
            p.scenarios,
            p.split_seed,
            p.per_fault,
            p.faults,
            ctx,
        )
    paths = bundles_in(folder)
    if p.limit is not None:
        paths = paths[: p.limit]
    if not paths:
        raise UsageError(f"no scenario bundles (folders with truth.json) in {folder}")
    index = folder / "index.json"
    skipped = len(json.loads(index.read_text()).get("skipped", [])) if index.exists() else 0

    scores, summary = run_bench(paths, progress=ctx.progress)
    out = p.out if p.out is not None else ctx.run_dir
    out.mkdir(parents=True, exist_ok=True)
    report = out / "bench.json"
    report.write_text(
        json.dumps(
            {
                "split": str(p.scenarios),
                "bundles": str(folder),
                "skipped": skipped,
                "summary": summary.model_dump(mode="json"),
                "scenarios": [s.model_dump(mode="json") for s in scores],
            },
            indent=2,
        )
    )
    table = out / "scenarios.csv"
    with table.open("w", newline="") as f:
        fields = list(scores[0].model_dump().keys())
        w = csv.DictWriter(f, fieldnames=[*fields, "hit_at_3"])
        w.writeheader()
        for s in scores:
            w.writerow({**s.model_dump(), "hit_at_3": s.hit_at_3})
    h3 = summary.hit_at_3
    return BenchHomeostatResult(
        split=p.scenarios,
        n_scenarios=summary.n_scenarios,
        skipped=skipped,
        errors=summary.errors,
        hit_at_1=summary.hit_at_1.value,
        hit_at_3=h3.value,
        hit_at_3_interval=(h3.low, h3.high) if h3.low is not None else None,
        wrong_confident_rate=summary.wrong_confident_rate.value,
        data_fault_catch_rate=summary.data_fault_catch_rate.value,
        by_fault_type={
            k: FaultTypeRow(n=v.n, hit_at_1=v.hit_at_1, hit_at_3=v.hit_at_3)
            for k, v in summary.by_fault_type.items()
        },
        report=report,
        scenarios_csv=table,
    )
