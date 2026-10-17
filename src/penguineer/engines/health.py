"""Data health: per-tag checks that say whether the data can be trusted.

Each check returns :class:`HealthFinding` intervals with a severity. Findings are evidence in
their own right: a faulty tag on a causal path later becomes a "the measurement is wrong"
hypothesis that competes with process causes.

Checks run here: gaps, flatline, out of range, spikes and timestamp problems (duplicates,
irregular sampling, non-existent local times). Analyzer-versus-lab checks need LIMS input and
come later.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np
import polars as pl
from pydantic import BaseModel, Field

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = 1_000_000


class Severity(StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"


SEVERITY_ORDER = {Severity.info: 0, Severity.warning: 1, Severity.critical: 2}


class HealthCheck(StrEnum):
    gap = "gap"
    flatline = "flatline"
    out_of_range = "out_of_range"
    spike = "spike"
    timestamp = "timestamp"


class HealthFinding(BaseModel):
    id: str = Field(description="Stable within one report, e.g. H0003")
    tag: str | None = Field(description="Historian tag, or null for file-level findings")
    check: HealthCheck
    severity: Severity
    start: datetime
    end: datetime
    n_samples: int = Field(description="Samples inside the interval")
    detail: str
    metrics: dict[str, float] = Field(default_factory=dict)


class TagHealth(BaseModel):
    tag: str
    n_samples: int
    first: datetime
    last: datetime
    median_period_s: float | None
    coverage: float = Field(description="Share of expected samples present, 0 to 1")
    n_findings: int
    worst: Severity | None


class HealthConfig(BaseModel):
    gap_factor: float = 3.0
    critical_gap_factor: float = 60.0
    flatline_min_minutes: float = 60.0
    flatline_min_samples: int = 5
    deadband: float = 0.0
    spike_z: float = 8.0
    spike_max_samples: int = 5
    irregular_cv: float = 0.5


def _dt(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(us))


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive (start, end) index pairs of consecutive True values."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask.astype(bool), [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return [(int(s), int(e) - 1) for s, e in zip(edges[::2], edges[1::2], strict=True)]


def _robust_scale(d: np.ndarray) -> tuple[float, float]:
    med = float(np.median(d))
    dev = np.abs(d - med)
    scale = float(np.median(dev)) * 1.4826
    if scale == 0.0:
        scale = float(np.mean(dev)) * 1.2533
    return med, scale


def check_tag(
    tag: str,
    t: np.ndarray,
    x: np.ndarray,
    config: HealthConfig,
    value_range: tuple[float | None, float | None] = (None, None),
    allow_flat: bool = False,
) -> tuple[list[dict], TagHealth | None]:
    """Run all per-tag checks. ``t`` is int64 microseconds since the epoch, sorted."""
    found: list[dict] = []

    def add(check, severity, i0, i1, detail, n=None, **metrics):
        found.append(
            dict(
                tag=tag,
                check=check,
                severity=severity,
                start=_dt(t[i0]),
                end=_dt(t[i1]),
                n_samples=int(n if n is not None else i1 - i0 + 1),
                detail=detail,
                metrics={k: float(v) for k, v in metrics.items()},
            )
        )

    if t.size == 0:
        return found, None

    dt = np.diff(t)
    dup = dt == 0
    if dup.any():
        k = int(dup.sum())
        first = int(np.flatnonzero(dup)[0])
        add(
            HealthCheck.timestamp,
            Severity.warning,
            first,
            int(np.flatnonzero(dup)[-1]) + 1,
            f"{k} duplicate timestamps",
            n=k,
            duplicates=k,
        )
        keep = np.concatenate(([True], ~dup))
        t, x = t[keep], x[keep]
        dt = np.diff(t)

    median = float(np.median(dt)) if dt.size else None

    if median and median > 0:
        gap_idx = np.flatnonzero(dt > config.gap_factor * median)
        for i in gap_idx:
            gap_s = dt[i] / _US
            sev = (
                Severity.critical
                if dt[i] > config.critical_gap_factor * median
                else (Severity.warning)
            )
            add(
                HealthCheck.gap,
                sev,
                int(i),
                int(i) + 1,
                f"no samples for {_human(gap_s)} (usual period {_human(median / _US)})",
                n=0,
                gap_s=gap_s,
                expected_period_s=median / _US,
                missing_samples=round(dt[i] / median) - 1,
            )
        regular = dt[dt <= config.gap_factor * median]
        if regular.size > 10:
            cv = float(np.std(regular) / np.mean(regular))
            if cv > config.irregular_cv:
                add(
                    HealthCheck.timestamp,
                    Severity.info,
                    0,
                    t.size - 1,
                    f"irregular sampling (interval CV {cv:.2f})",
                    cv=cv,
                )

    if t.size >= 2 and not allow_flat:
        flat = np.abs(np.diff(x)) <= config.deadband
        for a, b in runs(flat):
            i0, i1 = a, b + 1
            dur = (t[i1] - t[i0]) / _US
            if (
                dur >= config.flatline_min_minutes * 60
                and i1 - i0 + 1 >= config.flatline_min_samples
            ):
                sev = Severity.critical if dur >= 24 * 3600 else Severity.warning
                add(
                    HealthCheck.flatline,
                    sev,
                    i0,
                    i1,
                    f"value frozen at {x[i0]:g} for {_human(dur)}",
                    duration_s=dur,
                    value=x[i0],
                )

    lo, hi = value_range
    if lo is not None or hi is not None:
        bad = np.zeros(x.size, dtype=bool)
        if lo is not None:
            bad |= x < lo
        if hi is not None:
            bad |= x > hi
        for i0, i1 in runs(bad):
            seg = x[i0 : i1 + 1]
            add(
                HealthCheck.out_of_range,
                Severity.critical,
                i0,
                i1,
                f"outside instrument range [{_fmt(lo, '-inf')}, {_fmt(hi, 'inf')}]: "
                f"min {seg.min():g}, max {seg.max():g}",
                min=seg.min(),
                max=seg.max(),
            )

    if x.size >= 5:
        d = np.diff(x)
        med, scale = _robust_scale(d)
        if scale > 0:
            z = (d - med) / scale
            for a, b in runs(np.abs(z) > config.spike_z):
                # Diffs a..b move samples a+1..b away from the baseline and back.
                if b == a or b - a > config.spike_max_samples or b + 1 >= x.size:
                    continue
                seg = x[a + 1 : b + 1]
                excursion = float(np.max(np.abs(seg - x[a])))
                if abs(x[b + 1] - x[a]) < 0.5 * excursion:
                    add(
                        HealthCheck.spike,
                        Severity.warning,
                        a + 1,
                        b,
                        f"spike of {excursion:g} from {x[a]:g}",
                        excursion=excursion,
                        robust_z=float(np.max(np.abs(z[a : b + 1]))),
                    )

    span = t[-1] - t[0]
    expected = span / median + 1 if median and median > 0 else float(t.size)
    worst = max((f["severity"] for f in found), key=SEVERITY_ORDER.get, default=None)
    summary = TagHealth(
        tag=tag,
        n_samples=int(t.size),
        first=_dt(t[0]),
        last=_dt(t[-1]),
        median_period_s=median / _US if median is not None else None,
        coverage=round(min(1.0, t.size / expected), 4),
        n_findings=len(found),
        worst=worst,
    )
    return found, summary


def assess(
    frame: pl.DataFrame,
    config: HealthConfig,
    ranges: dict[str, tuple[float | None, float | None]] | None = None,
    allow_flat: set[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[HealthFinding], list[TagHealth]]:
    """Check every tag of a long table (timestamp UTC, tag, value)."""
    ranges = ranges or {}
    allow_flat = allow_flat or set()
    raw: list[dict] = []
    tags: list[TagHealth] = []
    parts = frame.sort(["tag", "timestamp"]).partition_by("tag", as_dict=True)
    names = sorted(k[0] for k in parts)
    for i, name in enumerate(names):
        if cancelled is not None and cancelled():
            break
        part = parts[(name,)]
        t = part["timestamp"].dt.epoch("us").to_numpy()
        x = part["value"].to_numpy().astype(float)
        found, summary = check_tag(
            name, t, x, config, ranges.get(name, (None, None)), name in allow_flat
        )
        raw.extend(found)
        if summary is not None:
            tags.append(summary)
        if progress is not None:
            progress(i + 1, len(names), name)

    raw.sort(key=lambda f: (f["tag"] or "", f["check"].value, f["start"]))
    findings = [HealthFinding(id=f"H{i + 1:04d}", **f) for i, f in enumerate(raw)]
    return findings, tags


def _human(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:g} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _fmt(v: float | None, unbounded: str) -> str:
    return unbounded if v is None else f"{v:g}"
