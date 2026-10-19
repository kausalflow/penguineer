from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field, FilePath

from penguineer.connectors import pi
from penguineer.connectors.pi import PIAuth, PIClient, PIMode
from penguineer.core import CommandParams, CommandResult, RunContext, command
from penguineer.io.historian import read_historian


class FetchPIParams(CommandParams):
    server: str = Field(description="PI Web API base URL, such as https://pi.example.com/piwebapi")
    start: datetime = Field(description="Window start (inclusive)")
    end: datetime = Field(description="Window end (exclusive)")
    tags: list[str] = Field(
        default_factory=list,
        description="PI tag names (on --data-server), full point paths \\\\server\\tag, or AF "
        "attribute paths \\\\afserver\\database\\element|attribute",
    )
    tag_filter: str | None = Field(
        None, description="Also every point on --data-server matching this, such as U100.*"
    )
    data_server: str | None = Field(None, description="PI Data Archive that tag names are on")
    mode: PIMode = Field(PIMode.recorded, description="Archived values, or values on a grid")
    interval: str = Field("1m", description="Grid step for --mode interpolated, such as 10s, 1m")
    event_frames: str | None = Field(
        None,
        description="AF database \\\\afserver\\database whose event frames become lots.csv",
    )
    event_frame_template: str | None = Field(
        None, description="Only event frames of this template, such as Batch"
    )
    auth: PIAuth = Field(
        PIAuth.kerberos,
        description="kerberos (your ticket), basic (password from PI_WEBAPI_PASSWORD), bearer "
        "(token from PI_WEBAPI_TOKEN) or none",
    )
    username: str | None = Field(None, description="User for --auth basic (or PI_WEBAPI_USER)")
    ca_bundle: FilePath | None = Field(
        None, description="CA certificates for a server with an in-house certificate"
    )
    timeout: float = Field(60.0, gt=0, description="Seconds to wait for each request")
    page_size: int = Field(pi.PAGE_SIZE, gt=0, description="Values asked for per request")
    timezone: str = Field("UTC", description="Time zone of naive --start and --end")
    out: Path = Field(
        Path("data/pi"),
        description="Folder for historian.parquet, tags.csv and lots.csv (relative: the project)",
    )


class FetchPIResult(CommandResult):
    server: str
    window_start: datetime
    window_end: datetime
    n_tags: int
    n_samples: int
    quality: dict[str, int] = Field(description="Values per PI quality: good, bad, I/O Timeout ...")
    samples_per_tag: dict[str, int]
    unresolved: list[str] = Field(description="Tags the server does not know")
    n_lots: int | None = None
    notes: list[str]
    historian: str = Field(description="historian.parquet: timestamp, tag, value, quality")
    tags_csv: str = Field(description="tags.csv: the tag list graph draft reads")
    lots_csv: str | None = None

    def output_paths(self) -> list[Path]:
        paths = [Path(self.historian), Path(self.tags_csv)]
        return paths + ([Path(self.lots_csv)] if self.lots_csv else [])

    def summary(self) -> str:
        lines = [
            f"fetch pi: {self.n_tags} tags, {self.n_samples} values, "
            f"{self.window_start:%Y-%m-%d %H:%M} to {self.window_end:%Y-%m-%d %H:%M} UTC",
            "quality: " + ", ".join(f"{n} {q}" for q, n in self.quality.items()),
        ]
        if self.unresolved:
            lines.append("not found: " + ", ".join(self.unresolved))
        if self.n_lots is not None:
            lines.append(f"lots: {self.n_lots} event frames")
        lines += [f"note: {n}" for n in self.notes]
        lines.append(f"historian: {self.historian}")
        lines.append(f"tags: {self.tags_csv}")
        if self.lots_csv:
            lines.append(f"lots: {self.lots_csv}")
        return "\n".join(lines)


@command("fetch pi", params=FetchPIParams, result=FetchPIResult)
def fetch_pi(p: FetchPIParams, ctx: RunContext) -> FetchPIResult:
    """Download a window of AVEVA PI data through the PI Web API, as penguineer's input files."""
    start, end = _aware(p.start, p.timezone), _aware(p.end, p.timezone)
    client = PIClient(p.server, p.auth, p.username, p.ca_bundle, p.timeout)
    got = pi.fetch(
        client,
        start,
        end,
        tags=p.tags,
        tag_filter=p.tag_filter,
        data_server=p.data_server,
        mode=p.mode,
        interval=p.interval,
        page_size=p.page_size,
        event_frame_database=p.event_frames,
        event_frame_template=p.event_frame_template,
        progress=ctx.progress,
    )
    out = p.out if p.out.is_absolute() else ctx.project_root / p.out
    out.mkdir(parents=True, exist_ok=True)
    historian = out / "historian.parquet"
    got.frame.write_parquet(historian)
    tags_csv = out / "tags.csv"
    got.tags.write_csv(tags_csv)
    lots_csv = None
    notes: list[str] = []
    if got.lots is not None:
        lots_csv = out / "lots.csv"
        got.lots.write_csv(lots_csv)
        if got.open_event_frames:
            notes.append(f"{got.open_event_frames} event frames still open, left out of lots.csv")
    counts = got.frame.group_by("tag").len().sort("tag")
    empty = [t for t in got.tags["tag"] if t not in set(counts["tag"])]
    if empty:
        notes.append("no values in the window: " + ", ".join(empty))
    if got.frame["value"].is_not_null().any():
        # The file must read back the way every command reads it.
        read_historian(historian)
    quality = dict(got.frame.group_by("quality").len().sort("len", descending=True).iter_rows())
    return FetchPIResult(
        server=p.server,
        window_start=start,
        window_end=end,
        n_tags=got.tags.height,
        n_samples=got.frame.height,
        quality=quality,
        samples_per_tag=dict(counts.iter_rows()),
        unresolved=got.unresolved,
        n_lots=None if got.lots is None else got.lots.height,
        notes=notes,
        historian=str(historian),
        tags_csv=str(tags_csv),
        lots_csv=str(lots_csv) if lots_csv else None,
    )


def _aware(t: datetime, timezone: str) -> datetime:
    if t.tzinfo is None:
        from zoneinfo import ZoneInfo

        return t.replace(tzinfo=ZoneInfo(timezone))
    return t
