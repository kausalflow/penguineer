from __future__ import annotations

from pathlib import Path

import polars as pl
from pydantic import Field, FilePath

from penguineer.core import CommandParams, CommandResult, LLMMode, RunContext, UsageError, command
from penguineer.engines.investigate import Settings
from penguineer.engines.investigate import investigate as run_investigation
from penguineer.io.graph import load_graph
from penguineer.io.historian import read_historian, to_utc
from penguineer.io.lims import read_lims
from penguineer.report.model import Hypothesis, QualityEvent, RuledOut


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
    lots: FilePath | None = Field(
        None,
        description="Lot windows: a CSV with lot, start, end (default: from one lab sample "
        "of the property to the next)",
        json_schema_extra={"ui": "file", "accept": [".csv"]},
    )
    quality: str | None = Field(
        None, description="LIMS property to investigate (default: the lot's worst off-spec one)"
    )
    top: int = Field(5, ge=1, le=20, description="Hypotheses to report")
    hops: int = Field(6, ge=1, le=50, description="flows_to hops to search upstream")
    reference_lots: int = Field(3, ge=1, le=50, description="In-spec lots to compare with")
    step_seconds: float | None = Field(
        None, gt=0, description="Grid step (default: the slowest candidate tag's period)"
    )
    timezone: str = Field("UTC", description="Time zone of naive timestamps in the exports")
    llm: LLMMode = Field(LLMMode.none, description="LLM for the prose summary only")
    out: Path | None = Field(
        None, description="Report folder (default: reports/<run_id> in the project)"
    )


class InvestigateResult(CommandResult):
    lot: str
    event: QualityEvent
    hypotheses: list[Hypothesis]
    ruled_out: list[RuledOut]
    notes: list[str]
    report_dir: str
    report_json: str
    report_html: str
    report_md: str

    def output_paths(self) -> list[Path]:
        return [Path(self.report_json), Path(self.report_html), Path(self.report_md)]

    def summary(self) -> str:
        ev = self.event
        spec = " ".join(
            s
            for s in (
                f">= {ev.spec_low:g}" if ev.spec_low is not None else "",
                f"<= {ev.spec_high:g}" if ev.spec_high is not None else "",
            )
            if s
        )
        lines = [
            f"investigate: lot {ev.lot}, {ev.property} {ev.value:g}"
            + (f" {ev.unit}" if ev.unit else "")
            + (f" (spec {spec})" if spec else "")
            + f", sampled {ev.sampled_at:%Y-%m-%d %H:%M} at {ev.sample_point or ev.sampled_node}",
            "",
        ]
        width = max((len(h.cause_tag or h.cause_node) for h in self.hypotheses), default=5)
        for h in self.hypotheses:
            lines.append(
                f"  #{h.rank} {(h.cause_tag or h.cause_node):<{width}}  {h.confidence:<6} "
                f"score {h.score:.2f}  {h.kind}"
            )
            lines.append(f"     {h.summary}")
        if self.ruled_out:
            lines.append("")
            lines.append("ruled out:")
            for r in self.ruled_out:
                lines.append(f"  {r.cause}: {r.reason}")
        for note in self.notes:
            lines.append(f"note: {note}")
        lines.append(f"report: {self.report_html}")
        return "\n".join(lines)


def _read_lots(path: Path, timezone: str) -> pl.DataFrame:
    raw = pl.read_csv(path)
    lower = {c.lower(): c for c in raw.columns}
    missing = [c for c in ("lot", "start", "end") if c not in lower]
    if missing:
        raise UsageError(f"{path}: needs columns lot, start, end (missing {', '.join(missing)})")
    start, _ = to_utc(raw[lower["start"]].alias("start"), timezone)
    end, _ = to_utc(raw[lower["end"]].alias("end"), timezone)
    return pl.DataFrame([raw[lower["lot"]].cast(pl.String).alias("lot"), start, end])


@command("investigate", params=InvestigateParams, result=InvestigateResult)
def investigate(p: InvestigateParams, ctx: RunContext) -> InvestigateResult:
    """Align the data in time, rank root-cause hypotheses along graph paths, write the report."""
    from penguineer import __version__

    graph_path = p.graph if p.graph is not None else ctx.project_root / "graph.yaml"
    if not graph_path.exists():
        raise UsageError(f"no plant graph: pass --graph or create {graph_path}")
    lims = read_lims(p.lims, timezone=p.timezone).frame
    lots = _read_lots(p.lots, p.timezone) if p.lots is not None else None
    lot_rows = lims.filter(pl.col("lot") == p.lot)
    graph = load_graph(graph_path)
    if lot_rows.height:
        graph = graph.valid_on(lot_rows["sampled_at"].min().date())
    tags = sorted({n.tag for n in graph.nodes if getattr(n, "type", "") == "tag"})
    historian = read_historian(p.data, timezone=p.timezone, tags=tags).frame

    inv = run_investigation(
        graph,
        historian,
        lims,
        p.lot,
        lots=lots,
        quality=p.quality,
        settings=Settings(
            hops=p.hops, reference_lots=p.reference_lots, step_s=p.step_seconds, top=p.top
        ),
        progress=ctx.progress,
    )
    if p.llm != LLMMode.none:
        inv.notes.append(
            f"--llm {p.llm} is not used yet: the summaries are the template text "
            "(LLM summaries arrive with the LLM milestone)"
        )

    from penguineer.report.build import build_report

    out = p.out if p.out is not None else ctx.project_root / "reports" / ctx.run_id
    report = build_report(inv, out, run_id=ctx.run_id, version=__version__)
    return InvestigateResult(
        lot=p.lot,
        event=report.event,
        hypotheses=report.hypotheses,
        ruled_out=report.ruled_out,
        notes=report.notes,
        report_dir=str(out),
        report_json=str(out / "report.json"),
        report_html=str(out / "report.html"),
        report_md=str(out / "report.md"),
    )
