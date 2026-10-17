"""LIMS export reader: one row per lab result.

Normalized columns: ``property`` (str), ``value`` (Float64), ``sampled_at`` (UTC),
``reported_at`` (UTC, or null when the export has no result time), and ``lot`` and
``sample_point`` (str, or null when the export has none). Column names are matched case
insensitively against common spellings, so a Homeostat Live ``lab.csv`` reads as is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from penguineer.core.errors import UsageError
from penguineer.io.historian import to_utc

ALIASES: dict[str, tuple[str, ...]] = {
    "property": ("property", "tag", "analysis", "test", "parameter", "component"),
    "value": ("value", "result", "result_value"),
    "sampled_at": ("sampled_at", "sample_time", "sampled", "sampling_time", "sample_date"),
    "reported_at": ("reported_at", "result_time", "reported", "released_at", "approved_at"),
    "lot": ("lot", "lot_id", "batch", "batch_id"),
    "sample_point": ("sample_point", "sample_location", "location", "sampling_point"),
}
REQUIRED = ("property", "value", "sampled_at")


@dataclass
class LimsData:
    frame: pl.DataFrame
    columns: dict[str, str | None]  # normalized name -> column found in the export
    nonexistent_local_times: int = 0


def read_lims(path: Path, *, timezone: str = "UTC") -> LimsData:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        raw = pl.read_parquet(path)
    elif suffix in (".csv", ".txt", ".tsv"):
        raw = pl.read_csv(
            path, separator="\t" if suffix == ".tsv" else ",", infer_schema_length=10_000
        )
    else:
        raise UsageError(f"unsupported LIMS file type {path.suffix!r}; use CSV or Parquet")

    lower = {c.lower(): c for c in raw.columns}
    found: dict[str, str | None] = {
        name: next((lower[a] for a in options if a in lower), None)
        for name, options in ALIASES.items()
    }
    missing = [n for n in REQUIRED if found[n] is None]
    if missing:
        raise UsageError(
            f"{path}: no column for {', '.join(missing)}; expected one of "
            + "; ".join(f"{n}: {', '.join(ALIASES[n])}" for n in missing)
        )

    def column(name: str, dtype: pl.DataType) -> pl.Series:
        src = found[name]
        if src is None:
            return pl.Series(name, [None] * raw.height, dtype=dtype)
        return raw[src].cast(dtype, strict=False).alias(name)

    sampled, n1 = to_utc(raw[found["sampled_at"]].alias("sampled_at"), timezone)
    if found["reported_at"] is not None:
        reported, n2 = to_utc(raw[found["reported_at"]].alias("reported_at"), timezone)
    else:
        reported, n2 = (
            pl.Series("reported_at", [None] * raw.height, dtype=pl.Datetime("us", "UTC")),
            0,
        )
    frame = (
        pl.DataFrame(
            [
                column("property", pl.String),
                column("value", pl.Float64),
                sampled,
                reported,
                column("lot", pl.String),
                column("sample_point", pl.String),
            ]
        )
        .drop_nulls(["property", "value", "sampled_at"])
        .sort(["property", "sampled_at"])
    )
    if frame.is_empty():
        raise UsageError(f"{path}: no lab results with a property, value and sample time")
    return LimsData(frame, found, n1 + n2)


def read_lots(path: Path, *, timezone: str = "UTC") -> pl.DataFrame:
    """Lot windows: a CSV with lot, start and end columns (any case), times in UTC."""
    raw = pl.read_csv(path)
    lower = {c.lower(): c for c in raw.columns}
    missing = [c for c in ("lot", "start", "end") if c not in lower]
    if missing:
        raise UsageError(f"{path}: needs columns lot, start, end (missing {', '.join(missing)})")
    start, _ = to_utc(raw[lower["start"]].alias("start"), timezone)
    end, _ = to_utc(raw[lower["end"]].alias("end"), timezone)
    return pl.DataFrame([raw[lower["lot"]].cast(pl.String).alias("lot"), start, end])
