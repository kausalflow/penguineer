from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import polars as pl
from pydantic import Field, FilePath

from penguineer.core import (
    CommandParams,
    CommandResult,
    NotEnoughData,
    RunContext,
    UsageError,
    command,
)
from penguineer.engines.lagfit import ModelFit, fit_lag
from penguineer.io.historian import read_historian
from penguineer.io.lims import read_lims


class LagParams(CommandParams):
    data: FilePath = Field(
        description="Historian export, CSV or Parquet, wide or long",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    cause: str = Field(description="Upstream tag")
    effect: str = Field(description="Downstream tag, or with --lims a lab property")
    lims: FilePath | None = Field(
        None,
        description="LIMS export: the effect is then a lab property, compared at its sample times",
        json_schema_extra={"ui": "file", "accept": [".csv", ".parquet"]},
    )
    flow: list[str] = Field(
        default_factory=list,
        description="Flow tags through the line, summed (enables the flow and volume models)",
    )
    flow_unit: str = Field("m3/h", description="Unit of the flow tags, for the volumes")
    start: datetime | None = Field(None, description="Window start (inclusive)")
    end: datetime | None = Field(None, description="Window end (exclusive)")
    max_lag_minutes: float = Field(240.0, gt=0, description="Longest delay to search")
    step_seconds: float | None = Field(
        None, gt=0, description="Grid step (default: the slower tag's median period)"
    )
    z_threshold: float = Field(
        5.0, gt=0, description="Peak strength needed for an online effect, |corr|·√n"
    )
    timezone: str = Field("UTC", description="Time zone of naive timestamps in the exports")
    out: Path | None = Field(None, description="Folder for the plots (default: the run folder)")


class LagResult(CommandResult):
    cause: str
    effect: str
    effect_kind: str = Field(description="online (a historian tag) or lab (a LIMS property)")
    flow_tags: list[str]
    volume_unit: str | None
    mean_flow: float | None
    n: int = Field(description="Points compared: grid points, or lab samples")
    step_s: float
    fits: list[ModelFit]
    best: str | None = Field(description="The simplest model that explains the most, if any")
    lag_min: float | None = Field(description="The best model's mean delay at the mean flow")
    plots: list[str]

    def output_paths(self) -> list[Path]:
        return [Path(p) for p in self.plots]

    def summary(self) -> str:
        where = f"{self.n} lab samples" if self.effect_kind == "lab" else f"{self.n} points"
        lines = [f"lag: {self.cause} -> {self.effect} ({self.effect_kind}, {where})"]
        if self.flow_tags:
            lines.append(
                f"flow: {' + '.join(self.flow_tags)}, mean {self.mean_flow:.3g} {self.flow_unit()}"
            )
        lines.append("")
        rows = []
        for f in self.fits:
            unit = "min" if f.coordinate == "time" else self.volume_unit
            dead = f"{f.dead:g} {unit} [{f.dead_low:g}, {f.dead_high:g}]"
            mixing = (
                f"{f.mixing:g} {unit} [{f.mixing_low:g}, {f.mixing_high:g}]"
                if f.model == "mixing"
                else "-"
            )
            strength = (
                f"{f.strength:g}x chance" if self.effect_kind == "lab" else f"z {f.strength:g}"
            )
            mark = "" if f.significant else "  (not significant)"
            rows.append(
                (
                    f.model,
                    dead,
                    mixing,
                    f"{f.residence_min:g} min",
                    f"{f.corr:+.2f}",
                    strength + mark,
                )
            )
        head = ("model", "dead", "mixing", "delay", "corr", "strength")
        widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
        for r in [head, *rows]:
            lines.append(
                "  "
                + "  ".join(
                    c.rjust(w) if i in (3, 4) else c.ljust(w)
                    for i, (c, w) in enumerate(zip(r, widths, strict=True))
                ).rstrip()
            )
        lines.append("")
        lines.append(f"best: {self.verdict()}")
        lines.extend(f"plot: {p}" for p in self.plots)
        return "\n".join(lines)

    def flow_unit(self) -> str:
        return f"{self.volume_unit}/h" if self.volume_unit else ""

    def verdict(self) -> str:
        if self.best is None:
            return "no model links the two above chance; no lag can be read from this data"
        f = next(f for f in self.fits if f.model == self.best)
        if f.model == "fixed":
            return f"a fixed dead time of {f.dead:g} min"
        if f.model == "flow":
            return (
                f"plug flow through {f.dead:g} {self.volume_unit}, "
                f"{f.residence_min:g} min at the mean flow"
            )
        if f.coordinate == "volume":
            return (
                f"plug flow through {f.dead:g} {self.volume_unit}, then a mixed volume of "
                f"{f.mixing:g} {self.volume_unit}: {f.residence_min:g} min on average at the "
                "mean flow"
            )
        return (
            f"a dead time of {f.dead:g} min, then mixing with a time constant of {f.mixing:g} "
            f"min: {f.residence_min:g} min on average"
        )


def volume_unit(flow_unit: str) -> str:
    """m3/h -> m3; anything else -> (unit)·h."""
    return flow_unit[:-2] if flow_unit.endswith("/h") else f"({flow_unit})·h"


def _series(frame: pl.DataFrame, tag: str) -> tuple[np.ndarray, np.ndarray]:
    part = frame.filter(pl.col("tag") == tag)
    return part["timestamp"].dt.epoch("us").to_numpy(), part["value"].to_numpy()


@command("lag", params=LagParams, result=LagResult)
def lag(p: LagParams, ctx: RunContext) -> LagResult:
    """Fit the actual delay from a cause to an effect: fixed, flow-driven, or with mixing."""
    tags = [p.cause, *p.flow] + ([] if p.lims is not None else [p.effect])
    frame = read_historian(
        p.data, timezone=p.timezone, start=p.start, end=p.end, tags=sorted(set(tags))
    ).frame
    present = set(frame["tag"].unique())
    missing = [t for t in tags if t not in present]
    if missing:
        raise UsageError(f"{p.data}: no samples for {', '.join(missing)}")

    cause = _series(frame, p.cause)
    if p.lims is not None:
        lab = read_lims(p.lims, timezone=p.timezone).frame.filter(pl.col("property") == p.effect)
        if lab.is_empty():
            raise UsageError(f"{p.lims}: no results for property {p.effect!r}")
        lab = lab.sort("sampled_at")
        effect = lab["sampled_at"].dt.epoch("us").to_numpy(), lab["value"].to_numpy()
    else:
        effect = _series(frame, p.effect)

    flow = None
    if p.flow:
        parts = [_series(frame, t) for t in p.flow]
        t0 = parts[0][0]
        total = sum(np.interp(t0.astype(float), t.astype(float), x) for t, x in parts)
        flow = (t0, np.asarray(total, dtype=float))

    fit = fit_lag(
        *cause,
        *effect,
        lab=p.lims is not None,
        flow=flow,
        max_lag_s=p.max_lag_minutes * 60,
        step_s=p.step_seconds,
        z_threshold=p.z_threshold,
        rng=ctx.rng,
    )
    if fit is None:
        need = "20 lab samples" if p.lims is not None else "four times the longest lag"
        raise NotEnoughData(
            f"{p.cause} and {p.effect} overlap too little to fit a lag (need at least {need} "
            "after the longest delay)"
        )

    from penguineer.report.lag_plots import fit_plot, profile_plot

    out = p.out if p.out is not None else ctx.run_dir
    out.mkdir(parents=True, exist_ok=True)
    vunit = volume_unit(p.flow_unit) if p.flow else None
    plots = [
        profile_plot(fit, out / "lag-profile.png", flow_unit=vunit or "volume"),
        fit_plot(fit, out / "lag-fit.png", cause=p.cause, effect=p.effect),
    ]
    best = next((f for f in fit.fits if f.model == fit.best), None)
    return LagResult(
        cause=p.cause,
        effect=p.effect,
        effect_kind=fit.effect_kind,
        flow_tags=list(p.flow),
        volume_unit=vunit,
        mean_flow=fit.mean_flow,
        n=fit.n,
        step_s=fit.step_s,
        fits=fit.fits,
        best=fit.best,
        lag_min=best.residence_min if best is not None else None,
        plots=[str(x) for x in plots],
    )
