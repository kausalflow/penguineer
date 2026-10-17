"""Historian export reader: CSV or Parquet, wide or long, normalized to one long table.

The long table has columns ``timestamp`` (UTC), ``tag`` (str), ``value`` (Float64) and an
optional ``quality`` (str). Naive timestamps are read in the given time zone and converted to
UTC; local times that do not exist (daylight-saving spring-forward) become null and are
reported, not silently shifted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from penguineer.core.errors import UsageError

TIMESTAMP_NAMES = ("timestamp", "time", "ts", "datetime", "date_time", "t")
LONG_REQUIRED = ("tag", "value")


@dataclass
class HistorianData:
    frame: pl.DataFrame  # timestamp (UTC), tag, value[, quality]
    layout: str  # "wide" or "long"
    nonexistent_local_times: int = 0
    dropped_columns: list[str] = field(default_factory=list)


def _scan(path: Path) -> pl.LazyFrame:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pl.scan_parquet(path)
    if suffix in (".csv", ".txt", ".tsv"):
        sep = "\t" if suffix == ".tsv" else ","
        return pl.scan_csv(path, separator=sep, infer_schema_length=10_000)
    raise UsageError(f"unsupported historian file type {path.suffix!r}; use CSV or Parquet")


def _timestamp_column(columns: list[str]) -> str:
    lower = {c.lower(): c for c in columns}
    for name in TIMESTAMP_NAMES:
        if name in lower:
            return lower[name]
    raise UsageError(
        f"no timestamp column found; expected one of {', '.join(TIMESTAMP_NAMES)} "
        f"(got {', '.join(columns[:10])})"
    )


def read_historian(
    path: Path,
    *,
    timezone: str = "UTC",
    start: datetime | None = None,
    end: datetime | None = None,
    tags: list[str] | None = None,
) -> HistorianData:
    lf = _scan(path)
    columns = lf.collect_schema().names()
    ts_col = _timestamp_column(columns)
    lower = {c.lower(): c for c in columns}
    long_layout = all(name in lower for name in LONG_REQUIRED)

    if long_layout:
        select = [
            pl.col(ts_col).alias("timestamp"),
            pl.col(lower["tag"]).cast(pl.String).alias("tag"),
            pl.col(lower["value"]).cast(pl.Float64, strict=False).alias("value"),
        ]
        if "quality" in lower:
            select.append(pl.col(lower["quality"]).cast(pl.String).alias("quality"))
        lf = lf.select(select)
        dropped: list[str] = []
    else:
        value_cols = [c for c in columns if c != ts_col]
        lf = lf.select(
            pl.col(ts_col).alias("timestamp"),
            *[pl.col(c).cast(pl.Float64, strict=False) for c in value_cols],
        ).unpivot(index="timestamp", variable_name="tag", value_name="value")
        dropped = []

    if tags:
        lf = lf.filter(pl.col("tag").is_in(tags))

    df = lf.collect()
    df, nonexistent = _normalize_timestamps(df, timezone)
    if start is not None:
        df = df.filter(pl.col("timestamp") >= _aware(start, timezone))
    if end is not None:
        df = df.filter(pl.col("timestamp") < _aware(end, timezone))

    if not long_layout:
        all_null = (
            df.group_by("tag")
            .agg(pl.col("value").is_not_null().sum().alias("n"))
            .filter(pl.col("n") == 0)["tag"]
            .to_list()
        )
        if all_null:
            dropped = sorted(all_null)
            df = df.filter(~pl.col("tag").is_in(dropped))

    df = df.drop_nulls(["value"]).sort(["tag", "timestamp"])
    if df.is_empty():
        raise UsageError(f"{path}: no numeric samples in the requested window")
    return HistorianData(df, "long" if long_layout else "wide", nonexistent, dropped)


def _normalize_timestamps(df: pl.DataFrame, timezone: str) -> tuple[pl.DataFrame, int]:
    ts, nonexistent = to_utc(df["timestamp"], timezone)
    df = df.with_columns(ts.alias("timestamp")).drop_nulls(["timestamp"])
    return df, nonexistent


def to_utc(ts: pl.Series, timezone: str) -> tuple[pl.Series, int]:
    """Parse a timestamp column to UTC microseconds.

    Naive times are read in ``timezone``; local times that do not exist (the daylight-saving
    gap) become null and are counted.
    """
    if ts.dtype == pl.String:
        parsed = ts.str.to_datetime(time_unit="us", strict=False)
        if parsed.null_count() > ts.null_count():
            bad = ts.filter(parsed.is_null() & ts.is_not_null()).head(3).to_list()
            raise UsageError(f"unparseable timestamps in {ts.name!r}, for example {bad}")
        ts = parsed
    elif ts.dtype == pl.Date:
        ts = ts.cast(pl.Datetime("us"))
    if not isinstance(ts.dtype, pl.Datetime):
        raise UsageError(f"column {ts.name!r} has type {ts.dtype}, expected date-time")

    nonexistent = 0
    if ts.dtype.time_zone is None:
        before = ts.null_count()
        ts = ts.dt.replace_time_zone(timezone, ambiguous="earliest", non_existent="null")
        nonexistent = ts.null_count() - before
    return ts.dt.convert_time_zone("UTC").dt.cast_time_unit("us"), nonexistent


def _aware(dt: datetime, timezone: str) -> datetime:
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo

        dt = dt.replace(tzinfo=ZoneInfo(timezone))
    return dt
