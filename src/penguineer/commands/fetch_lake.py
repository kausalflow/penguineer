from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field

from penguineer.connectors import lake
from penguineer.connectors.lake import TableFormat, TableKind
from penguineer.core import CommandParams, CommandResult, RunContext, UsageError, command
from penguineer.io.historian import read_historian
from penguineer.io.lims import read_lims, read_lots


class FetchLakeParams(CommandParams):
    source: str = Field(
        description="Table path or URL, globs allowed: s3://bucket/historian/**/*.parquet, "
        "gs://..., az://..., abfss://..., or a local path"
    )
    kind: TableKind = Field(
        TableKind.historian, description="What the table holds: historian, lims or lots"
    )
    format: TableFormat = Field(
        TableFormat.auto, description="parquet, csv or delta (auto: by the file extension)"
    )
    start: datetime | None = Field(None, description="Window start (inclusive)")
    end: datetime | None = Field(None, description="Window end (exclusive)")
    tags: list[str] = Field(
        default_factory=list, description="Only these tags (historian) or properties (lims)"
    )
    column: list[str] = Field(
        default_factory=list,
        description="name=source_column for columns without a common name, such as "
        "timestamp=event_time or tag=point_id",
    )
    storage_option: list[str] = Field(
        default_factory=list,
        description="key=value object-store options such as aws_region=eu-west-1 or "
        "aws_endpoint_url=http://minio:9000; credentials come from the environment",
    )
    hive: bool = Field(False, description="Read key=value folders as columns (Parquet)")
    timezone: str = Field("UTC", description="Time zone of naive timestamps")
    out: Path | None = Field(
        None, description="Parquet file to write (default: data/lake/<kind>.parquet in the project)"
    )


class FetchLakeResult(CommandResult):
    source: str
    kind: TableKind
    layout: str = Field(description="long or wide for a historian table")
    columns: dict[str, str | None] = Field(description="penguineer name -> source column")
    n_rows: int
    n_tags: int | None = Field(None, description="Tags (historian) or properties (lims)")
    window_start: datetime | None = None
    window_end: datetime | None = None
    table: str = Field(description="The written Parquet file, read by every command as is")

    def output_paths(self) -> list[Path]:
        return [Path(self.table)]

    def summary(self) -> str:
        mapped = ", ".join(f"{n}={c}" for n, c in self.columns.items() if c is not None)
        lines = [f"fetch lake: {self.n_rows} rows of {self.kind} ({self.layout})"]
        if self.n_tags is not None:
            lines[0] += f", {self.n_tags} {'tags' if self.kind == 'historian' else 'properties'}"
        if self.window_start is not None:
            fmt = "%Y-%m-%d %H:%M"
            lines.append(f"window: {self.window_start:{fmt}} to {self.window_end:{fmt}} UTC")
        lines += [f"columns: {mapped}", f"table: {self.table}"]
        return "\n".join(lines)


@command("fetch lake", params=FetchLakeParams, result=FetchLakeResult)
def fetch_lake(p: FetchLakeParams, ctx: RunContext) -> FetchLakeResult:
    """Read a window of a Parquet, CSV or Delta table on S3, GCS, Azure or disk into the project."""
    mapping: dict[str, str] = {}
    for item in p.column:
        name, sep, col = item.partition("=")
        if not sep or not name.strip() or not col.strip():
            raise UsageError(f"--column {item!r}: use name=source_column, such as tag=point_id")
        mapping[name.strip()] = col.strip()
    if (
        p.start is not None
        and p.end is not None
        and _aware(p.start, p.timezone) >= _aware(p.end, p.timezone)
    ):
        raise UsageError("--start must be before --end")
    got = lake.fetch(
        p.source,
        p.kind,
        start=p.start,
        end=p.end,
        tags=p.tags or None,
        columns=mapping,
        fmt=p.format,
        storage_options=lake.parse_storage_options(p.storage_option),
        hive=p.hive,
        timezone=p.timezone,
    )
    if got.frame.is_empty():
        raise UsageError(f"{p.source}: no rows in the requested window")
    out = p.out if p.out is not None else Path("data/lake") / f"{p.kind}.parquet"
    out = out if out.is_absolute() else ctx.project_root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    got.frame.write_parquet(out)

    # Read the file back the way the commands will, so a bad table fails here, not later.
    n_tags = None
    window: tuple[datetime | None, datetime | None] = (None, None)
    if p.kind is TableKind.historian:
        hist = read_historian(out, timezone=p.timezone)
        n_tags = hist.frame["tag"].n_unique()
        window = hist.frame["timestamp"].min(), hist.frame["timestamp"].max()
    elif p.kind is TableKind.lims:
        lims = read_lims(out, timezone=p.timezone).frame
        n_tags = lims["property"].n_unique()
        window = lims["sampled_at"].min(), lims["sampled_at"].max()
    else:
        lots = read_lots(out, timezone=p.timezone)
        window = lots["start"].min(), lots["end"].max()
    return FetchLakeResult(
        source=p.source,
        kind=p.kind,
        layout=got.layout,
        columns=got.columns,
        n_rows=got.frame.height,
        n_tags=n_tags,
        window_start=window[0],
        window_end=window[1],
        table=str(out),
    )


def _aware(t: datetime, timezone: str) -> datetime:
    if t.tzinfo is None:
        from zoneinfo import ZoneInfo

        return t.replace(tzinfo=ZoneInfo(timezone))
    return t
