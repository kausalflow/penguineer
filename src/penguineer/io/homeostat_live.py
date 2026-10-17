"""Homeostat Live: the public simulated plants at https://homeostat.kausalflow.com/live/.

Downloads a window of a live plant's sensor rows, lab results and data dictionary as the CSV
files the site serves, and turns the data dictionary into a plant graph. Only public API data is
used; the plants' configurations stay private.

The sensor file (``live.csv``) is a wide historian export with a ``time`` column, so the normal
historian reader and every command read it unchanged.
"""

from __future__ import annotations

import csv
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from plantgraph import Loop, PlantGraph, Quality, Source, Tag

from penguineer.core.errors import UsageError

SERVER = "https://homeostat.kausalflow.com"
# The API answers at most 1,100 rows per request; at the 10-minute step that is 7.6 days.
CHUNK = timedelta(days=7)

_SPAN = re.compile(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*(?=[^\d\s-])")
_ROLES = {"measurement", "setpoint", "output", "mode", "setting"}


@dataclass
class LiveWindow:
    sensors: Path  # live.csv, wide, column ``time``
    lab: Path  # lab.csv: tag, name, unit, sampled_at, reported_at, value
    tags: Path  # tags.csv: tag, name, unit, kind, group, instrument, description


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, timeout: float) -> str:
    from penguineer import __version__

    request = urllib.request.Request(
        url, headers={"user-agent": f"penguineer/{__version__}", "accept": "text/csv"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise UsageError(f"homeostat live: HTTP {e.code} for {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise UsageError(f"homeostat live: cannot reach {url}: {e.reason}") from None


def fetch_window(
    plant: str,
    since: datetime,
    until: datetime,
    dest: Path,
    *,
    server: str = SERVER,
    timeout: float = 60,
) -> LiveWindow:
    """Download sensors, lab results and tags of ``[since, until)`` into ``dest``.

    Files already present are reused, so a released window is downloaded once.
    """
    dest.mkdir(parents=True, exist_ok=True)
    base = f"{server.rstrip('/')}/api/plants/{plant}"
    window = LiveWindow(dest / "live.csv", dest / "lab.csv", dest / "tags.csv")

    if not window.tags.exists():
        window.tags.write_text(_get(f"{base}/tags.csv", timeout))
    if not window.lab.exists():
        window.lab.write_text(
            _get(f"{base}/lab.csv?since={_iso(since)}&until={_iso(until)}", timeout)
        )
    if not window.sensors.exists():
        header: str | None = None
        rows: list[str] = []
        start = since
        while start < until:
            end = min(start + CHUNK, until)
            text = _get(f"{base}/live.csv?since={_iso(start)}&until={_iso(end)}", timeout)
            lines = [line for line in text.splitlines() if line]
            if lines:
                if header is None:
                    header = lines[0]
                elif lines[0] != header:
                    raise UsageError("homeostat live: the tag columns changed between requests")
                # Both ends may be inclusive; keep each time once.
                rows.extend(line for line in lines[1:] if not rows or line > rows[-1])
            start = end
        if header is None:
            raise UsageError(f"homeostat live: no sensor rows for {plant} in the window")
        tmp = window.sensors.with_suffix(".tmp")
        tmp.write_text("\n".join([header, *rows]) + "\n")
        tmp.replace(window.sensors)
    return window


def parse_span(instrument: str) -> tuple[float | None, float | None]:
    """Instrument range from text such as ``"gas flow transmitter, 0-20 Nm3/h; noise ..."``."""
    m = _SPAN.search(instrument or "")
    if not m:
        return None, None
    low, high = float(m.group(1)), float(m.group(2))
    return (low, high) if low < high else (None, None)


def graph_from_tags(path: Path, unit: str | None = None) -> PlantGraph:
    """A plant graph from a live plant's data dictionary: tags, loops and lab qualities.

    It has nodes only. The flow topology is not in the public dictionary, so edges are left
    to ``graph draft`` and the engineer.
    """
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    nodes: list = []
    groups: dict[str, dict[str, str]] = {}
    for row in rows:
        tag, kind, group = row["tag"], row.get("kind", ""), row.get("group", "")
        if kind == "lab":
            nodes.append(
                Quality(
                    id=f"Q:{tag}",
                    property=tag,
                    unit_of_measure=row.get("unit") or None,
                    description=row.get("name") or None,
                    source=Source.homeostat,
                )
            )
            continue
        low, high = parse_span(row.get("instrument", ""))
        nodes.append(
            Tag(
                id=tag,
                tag=tag,
                unit_of_measure=row.get("unit") or None,
                range_low=low,
                range_high=high,
                role=kind if kind in _ROLES else None,
                description=row.get("name") or None,
                source=Source.homeostat,
            )
        )
        if group and kind in ("measurement", "setpoint", "output", "mode"):
            groups.setdefault(group, {}).setdefault(kind, tag)
    for group, members in sorted(groups.items()):
        if "output" in members:
            nodes.append(
                Loop(
                    id=f"loop:{group}",
                    pv=members.get("measurement"),
                    sp=members.get("setpoint"),
                    op=members["output"],
                    mode=members.get("mode"),
                    source=Source.homeostat,
                )
            )
    return PlantGraph(unit=unit, nodes=nodes)
