"""Data lakes: Parquet, CSV or Delta tables on S3, Google Cloud Storage, Azure or local disk.

A source is a path or URL, optionally with a glob (``s3://bucket/historian/**/*.parquet``).
Polars reads object stores itself, so S3, GCS and Azure need no extra package; Delta tables
need ``penguineer[delta]``. Only the requested window and tags are read: the time and tag
filters are pushed down to the scan, so Parquet row groups outside them are skipped.

Credentials never go in parameters, because parameters are written to ``run.json``. They come
from the environment the cloud SDKs use (``AWS_ACCESS_KEY_ID``, ``AWS_PROFILE``,
``GOOGLE_APPLICATION_CREDENTIALS``, ``AZURE_STORAGE_ACCOUNT_KEY`` and so on), or an instance
role. Storage options carry the rest: region, endpoint, ``allow_http`` for a local MinIO.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

import polars as pl

from penguineer.core.errors import UsageError
from penguineer.io.historian import TIMESTAMP_NAMES
from penguineer.io.lims import ALIASES as LIMS_ALIASES
from penguineer.io.lims import LOT_ALIASES


class TableKind(StrEnum):
    historian = "historian"
    lims = "lims"
    lots = "lots"


class TableFormat(StrEnum):
    auto = "auto"
    parquet = "parquet"
    csv = "csv"
    delta = "delta"


# penguineer's names for each kind, with the spellings the file readers accept.
COLUMNS: dict[TableKind, dict[str, tuple[str, ...]]] = {
    TableKind.historian: {
        "timestamp": TIMESTAMP_NAMES,
        "tag": ("tag", "tag_name", "tagname", "point", "signal", "name"),
        "value": ("value", "val", "reading"),
        "quality": ("quality", "status", "flag"),
    },
    TableKind.lims: LIMS_ALIASES,
    TableKind.lots: LOT_ALIASES,
}
# The column the time window filters on, per kind.
TIME_COLUMN = {TableKind.historian: "timestamp", TableKind.lims: "sampled_at"}
_SECRET_WORDS = ("secret", "password", "token", "key", "sas", "credential")


@dataclass
class LakeTable:
    frame: pl.DataFrame  # penguineer's column names; other columns kept for lots
    columns: dict[str, str | None]  # penguineer name -> source column
    layout: str  # "long" or "wide" (historian), else the kind


def parse_storage_options(items: list[str]) -> dict[str, str]:
    """``key=value`` pairs; keys that look like credentials are refused."""
    options: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip().lower()
        if not sep or not key:
            raise UsageError(
                f"storage option {item!r}: use key=value, such as aws_region=eu-west-1"
            )
        if any(w in key for w in _SECRET_WORDS):
            raise UsageError(
                f"storage option {key!r} looks like a credential; parameters are written to "
                "run.json, so set it in the environment instead (for example AWS_ACCESS_KEY_ID)"
            )
        options[key] = value.strip()
    return options


def scan(
    source: str,
    *,
    fmt: TableFormat = TableFormat.auto,
    storage_options: dict[str, str] | None = None,
    hive: bool = False,
) -> pl.LazyFrame:
    """A lazy scan of ``source``; nothing is read until it is collected."""
    fmt = _format(source, fmt)
    opts = storage_options or None
    try:
        if fmt is TableFormat.delta:
            try:
                import deltalake  # noqa: F401
            except ImportError:
                raise UsageError(
                    "Delta tables need the delta extra: pip install 'penguineer[delta]'"
                ) from None
            return pl.scan_delta(source, storage_options=opts)
        if fmt is TableFormat.csv:
            return pl.scan_csv(
                source,
                storage_options=opts,
                separator="\t" if source.lower().endswith(".tsv") else ",",
                infer_schema_length=10_000,
                try_parse_dates=True,
            )
        return pl.scan_parquet(source, storage_options=opts, hive_partitioning=hive or None)
    except UsageError:
        raise
    except Exception as e:  # polars raises many kinds for a bad URL or missing object
        raise UsageError(f"{source}: cannot open: {e}") from None


def fetch(
    source: str,
    kind: TableKind = TableKind.historian,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    tags: list[str] | None = None,
    columns: dict[str, str] | None = None,
    fmt: TableFormat = TableFormat.auto,
    storage_options: dict[str, str] | None = None,
    hive: bool = False,
    timezone: str = "UTC",
) -> LakeTable:
    """Read the window of one table with penguineer's column names.

    ``columns`` maps penguineer names (``timestamp``, ``tag``, ``value``, ``sampled_at`` ...)
    to the source's columns; unmapped names are looked up by their common spellings. The
    window is ``[start, end)`` on the time column; lots overlapping it are kept. ``tags``
    selects tags in a long table and columns in a wide one.
    """
    lf = scan(source, fmt=fmt, storage_options=storage_options, hive=hive)
    try:
        schema = lf.collect_schema()
    except Exception as e:
        raise UsageError(f"{source}: cannot read the schema: {e}") from None
    found = _match(schema.names(), COLUMNS[kind], columns or {}, source)

    layout = kind.value
    if kind is TableKind.historian:
        layout = "long" if found["tag"] and found["value"] else "wide"
        if found["timestamp"] is None:
            raise UsageError(
                f"{source}: no timestamp column; map one with --column timestamp=<name> "
                f"(columns: {', '.join(schema.names()[:12])})"
            )
    else:
        required = ("property", "value", "sampled_at") if kind is TableKind.lims else tuple(found)
        missing = [n for n in required if found[n] is None]
        if missing:
            raise UsageError(
                f"{source}: no column for {', '.join(missing)}; map with --column "
                + " ".join(f"{n}=<name>" for n in missing)
            )

    if kind is TableKind.lots:
        start_col, end_col = found["start"], found["end"]
        if end is not None:
            lf = lf.filter(pl.col(start_col) < _literal(end, schema[start_col], timezone))
        if start is not None:
            lf = lf.filter(pl.col(end_col) > _literal(start, schema[end_col], timezone))
    else:
        time_col = found[TIME_COLUMN[kind]]
        if start is not None:
            lf = lf.filter(pl.col(time_col) >= _literal(start, schema[time_col], timezone))
        if end is not None:
            lf = lf.filter(pl.col(time_col) < _literal(end, schema[time_col], timezone))

    if kind is TableKind.historian and layout == "long":
        if tags:
            lf = lf.filter(pl.col(found["tag"]).cast(pl.String).is_in(tags))
        keep = {n: c for n, c in found.items() if c is not None}
        lf = lf.select([pl.col(c).alias(n) for n, c in keep.items()])
    elif kind is TableKind.historian:
        values = [c for c in schema.names() if c != found["timestamp"]]
        if tags:
            missing_tags = [t for t in tags if t not in values]
            if missing_tags:
                raise UsageError(f"{source}: no columns for tags {', '.join(missing_tags)}")
            values = list(tags)
        lf = lf.select(pl.col(found["timestamp"]).alias("timestamp"), *map(pl.col, values))
    elif kind is TableKind.lims:
        if tags:
            lf = lf.filter(pl.col(found["property"]).cast(pl.String).is_in(tags))
        keep = {n: c for n, c in found.items() if c is not None}
        lf = lf.select([pl.col(c).alias(n) for n, c in keep.items()])
    else:
        renamed = {c: n for n, c in found.items() if c is not None and c != n}
        lf = lf.rename(renamed)

    try:
        frame = lf.collect()
    except Exception as e:
        raise UsageError(f"{source}: read failed: {e}") from None
    return LakeTable(frame, found, layout)


def _format(source: str, fmt: TableFormat) -> TableFormat:
    if fmt is not TableFormat.auto:
        return fmt
    lower = source.lower().rstrip("/")
    if lower.endswith((".csv", ".tsv", ".txt", ".csv.gz")):
        return TableFormat.csv
    return TableFormat.parquet


def _match(
    names: list[str],
    aliases: dict[str, tuple[str, ...]],
    mapping: dict[str, str],
    source: str,
) -> dict[str, str | None]:
    unknown = sorted(set(mapping) - set(aliases))
    if unknown:
        raise UsageError(
            f"--column: unknown name {', '.join(unknown)}; this table has {', '.join(aliases)}"
        )
    absent = sorted(c for c in mapping.values() if c not in names)
    if absent:
        raise UsageError(f"{source}: no column {', '.join(absent)} (columns: {', '.join(names)})")
    lower = {c.lower(): c for c in names}
    taken = set(mapping.values())
    found: dict[str, str | None] = {}
    for name, options in aliases.items():
        if name in mapping:
            found[name] = mapping[name]
            continue
        hit = next((lower[a] for a in options if a in lower and lower[a] not in taken), None)
        found[name] = hit
        if hit is not None:
            taken.add(hit)
    return found


def _literal(t: datetime, dtype: pl.DataType, timezone: str) -> pl.Expr | datetime:
    """``t`` in the column's own type, so the comparison can be pushed down to the scan."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo(timezone))
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone is None:  # naive times in the source are local to ``timezone``
            return t.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
        return pl.lit(t.astimezone(UTC)).dt.convert_time_zone(dtype.time_zone)
    if dtype == pl.Date:
        return t.astimezone(ZoneInfo(timezone)).date()
    if dtype == pl.String:
        raise UsageError(
            "the time column holds text; a window on it cannot be pushed down. Store times as "
            "timestamps, or leave out --start and --end and filter after reading"
        )
    raise UsageError(f"the time column has type {dtype}, expected a date-time")
