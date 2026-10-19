"""AVEVA PI (formerly OSIsoft PI) through the PI Web API.

Reads recorded or interpolated values of PI points or PI AF attributes, the points' metadata
(descriptor, engineering units, zero and span) and, optionally, AF event frames as lot windows.
Only ``GET`` requests are sent: nothing on the PI server is changed.

Credentials never go in parameters, because parameters are written to ``run.json``. Basic
authentication reads the password from ``PI_WEBAPI_PASSWORD``, bearer authentication the token
from ``PI_WEBAPI_TOKEN``, and Kerberos uses the caller's ticket (``penguineer[pi-kerberos]``).

Quality: a value PI marks bad, or a system digital state such as ``I/O Timeout``, becomes a
null value with the state's name as quality, so ``data health`` sees a gap instead of a number.
Questionable and substituted values keep their number and say so in ``quality``.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl

from penguineer.core.errors import UsageError

PASSWORD_ENV = "PI_WEBAPI_PASSWORD"
TOKEN_ENV = "PI_WEBAPI_TOKEN"
USER_ENV = "PI_WEBAPI_USER"

# The PI Web API answers at most MaxReturnedItemsPerCall values per call (150,000 by default).
PAGE_SIZE = 10_000
# Open event frames end at this time.
_OPEN_END = datetime(9999, 1, 1, tzinfo=UTC)
_FRACTION = re.compile(r"(\.\d{6})\d+")
_INTERVAL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


class PIAuth(StrEnum):
    none = "none"
    basic = "basic"
    bearer = "bearer"
    kerberos = "kerberos"


class PIMode(StrEnum):
    recorded = "recorded"
    interpolated = "interpolated"


@dataclass
class PIClient:
    """A read-only PI Web API client. ``base_url`` ends in ``/piwebapi``."""

    base_url: str
    auth: PIAuth = PIAuth.kerberos
    username: str | None = None
    ca_bundle: Path | None = None
    timeout: float = 60
    _session: Any = field(default=None, init=False, repr=False)

    def get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        """``GET base_url/path?params`` as parsed JSON."""
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        if self.auth is PIAuth.kerberos:
            return self._get_kerberos(url)
        headers = {"accept": "application/json", "user-agent": _user_agent()}
        if self.auth is PIAuth.basic:
            headers["authorization"] = "Basic " + _basic(self._username(), _secret(PASSWORD_ENV))
        elif self.auth is PIAuth.bearer:
            headers["authorization"] = "Bearer " + _secret(TOKEN_ENV)
        context = ssl.create_default_context(cafile=str(self.ca_bundle) if self.ca_bundle else None)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise UsageError(
                _http_error(e.code, url, e.read().decode("utf-8", "replace"))
            ) from None
        except urllib.error.URLError as e:
            raise UsageError(f"PI Web API: cannot reach {url}: {e.reason}") from None

    def _username(self) -> str:
        user = self.username or os.environ.get(USER_ENV)
        if not user:
            raise UsageError(f"PI Web API basic authentication needs --username or {USER_ENV}")
        return user

    def _get_kerberos(self, url: str) -> Any:
        if self._session is None:
            try:
                import requests
                from requests_kerberos import OPTIONAL, HTTPKerberosAuth
            except ImportError:
                raise UsageError(
                    "Kerberos authentication needs the pi-kerberos extra: "
                    "pip install 'penguineer[pi-kerberos]' (or pass --auth basic or bearer)"
                ) from None
            self._session = requests.Session()
            self._session.auth = HTTPKerberosAuth(mutual_authentication=OPTIONAL)
            self._session.headers.update(
                {"accept": "application/json", "user-agent": _user_agent()}
            )
            if self.ca_bundle:
                self._session.verify = str(self.ca_bundle)
        import requests

        try:
            r = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            raise UsageError(f"PI Web API: cannot reach {url}: {e}") from None
        if r.status_code >= 400:
            raise UsageError(_http_error(r.status_code, url, r.text))
        return r.json()


@dataclass
class PIStream:
    """A PI point or AF attribute, resolved to the WebId its values are read by."""

    name: str  # what the caller asked for, the tag in the fetched table
    web_id: str
    path: str
    description: str | None = None
    unit: str | None = None
    range_low: float | None = None
    range_high: float | None = None


@dataclass
class PIFetch:
    frame: pl.DataFrame  # timestamp (UTC), tag, value (null when bad), quality
    tags: pl.DataFrame  # tag, description, unit, range_low, range_high, path
    lots: pl.DataFrame | None = None  # lot, start, end, template
    unresolved: list[str] = field(default_factory=list)
    open_event_frames: int = 0


def resolve(client: PIClient, name: str, data_server: str | None) -> PIStream:
    """A tag name (on ``data_server``), a full point path or an AF attribute path.

    ``\\\\server\\tag`` is a PI point; ``\\\\afserver\\db\\element|attribute`` an AF attribute.
    """
    if "|" in name:
        a = client.get("attributes", {"path": name})
        return PIStream(
            name,
            a["WebId"],
            a.get("Path") or name,
            a.get("Description") or None,
            a.get("DefaultUnitsName") or a.get("DefaultUnitsNameAbbreviation") or None,
        )
    if name.startswith("\\\\"):
        path = name
    elif data_server:
        path = f"\\\\{data_server}\\{name}"
    else:
        raise UsageError(f"PI tag {name!r}: give --data-server, or the full path \\\\server\\tag")
    p = client.get("points", {"path": path})
    return _stream(name, p, path)


def search_points(
    client: PIClient, data_server: str, name_filter: str, *, max_count: int = 1000
) -> list[PIStream]:
    """Points on ``data_server`` whose names match ``name_filter`` (PI wildcards ``*`` ``?``)."""
    server = client.get("dataservers", {"path": f"\\\\{data_server}"})
    found: list[PIStream] = []
    while True:
        page = client.get(
            f"dataservers/{server['WebId']}/points",
            {"nameFilter": name_filter, "startIndex": len(found), "maxCount": max_count},
        )
        items = page.get("Items", [])
        found.extend(_stream(p["Name"], p, p.get("Path", "")) for p in items)
        if len(items) < max_count:
            return found


def recorded(
    client: PIClient,
    stream: PIStream,
    start: datetime,
    end: datetime,
    *,
    page_size: int = PAGE_SIZE,
) -> list[tuple[datetime, float | None, str]]:
    """Every archived value in ``[start, end)``, paged by time."""
    rows: list[tuple[datetime, float | None, str]] = []
    cursor, again = start, 0
    while True:
        page = client.get(
            f"streams/{stream.web_id}/recorded",
            {
                "startTime": _iso(cursor),
                "endTime": _iso(end),
                "boundaryType": "Inside",
                "maxCount": page_size,
                "selectedFields": "Items.Timestamp;Items.Value;Items.Good;"
                "Items.Questionable;Items.Substituted",
            },
        )
        items = [_value(i) for i in page.get("Items", [])]
        # A page starts at the last time read, so the values at that time come again.
        skip = 0
        while skip < min(again, len(items)) and items[skip][0] == cursor:
            skip += 1
        rows.extend(r for r in items[skip:] if r[0] < end)
        if len(items) < page_size:
            return rows
        if items[-1][0] == cursor:
            raise UsageError(
                f"PI tag {stream.name}: more than {page_size} values at {_iso(cursor)}; "
                "raise --page-size"
            )
        cursor = items[-1][0]
        again = sum(1 for r in reversed(items) if r[0] == cursor)


def interpolated(
    client: PIClient,
    stream: PIStream,
    start: datetime,
    end: datetime,
    interval: str,
    *,
    page_size: int = PAGE_SIZE,
) -> list[tuple[datetime, float | None, str]]:
    """Values every ``interval`` (PI syntax such as ``1m`` or ``10s``) in ``[start, end)``."""
    step = parse_interval(interval)
    span = step * page_size
    rows: list[tuple[datetime, float | None, str]] = []
    chunk = start
    while chunk < end:
        chunk_end = min(chunk + span, end)
        page = client.get(
            f"streams/{stream.web_id}/interpolated",
            {"startTime": _iso(chunk), "endTime": _iso(chunk_end), "interval": interval},
        )
        rows.extend(r for r in map(_value, page.get("Items", [])) if chunk <= r[0] < chunk_end)
        chunk = chunk_end
    return rows


def event_frames(
    client: PIClient,
    database: str,
    start: datetime,
    end: datetime,
    *,
    template: str | None = None,
    name_filter: str = "*",
    page_size: int = 1000,
) -> tuple[pl.DataFrame, int]:
    """AF event frames overlapping ``[start, end)`` as lot windows, and how many are still open.

    ``database`` is the AF database path, ``\\\\afserver\\database``.
    """
    db = client.get("assetdatabases", {"path": database})
    params: dict[str, Any] = {
        "startTime": _iso(start),
        "endTime": _iso(end),
        "searchMode": "Overlapped",
        "nameFilter": name_filter,
        "maxCount": page_size,
    }
    if template:
        params["templateName"] = template
    frames: list[dict[str, Any]] = []
    while True:
        page = client.get(
            f"assetdatabases/{db['WebId']}/eventframes", {**params, "startIndex": len(frames)}
        )
        items = page.get("Items", [])
        frames.extend(items)
        if len(items) < page_size:
            break
    rows, still_open = [], 0
    for f in frames:
        f_end = parse_time(f["EndTime"]) if f.get("EndTime") else _OPEN_END
        if f_end >= _OPEN_END:
            still_open += 1
            continue
        rows.append((f["Name"], parse_time(f["StartTime"]), f_end, f.get("TemplateName") or None))
    lots = pl.DataFrame(
        rows,
        schema={
            "lot": pl.String,
            "start": pl.Datetime("us", "UTC"),
            "end": pl.Datetime("us", "UTC"),
            "template": pl.String,
        },
        orient="row",
    ).sort("start")
    return lots, still_open


def fetch(
    client: PIClient,
    start: datetime,
    end: datetime,
    *,
    tags: list[str] = (),
    tag_filter: str | None = None,
    data_server: str | None = None,
    mode: PIMode = PIMode.recorded,
    interval: str = "1m",
    page_size: int = PAGE_SIZE,
    event_frame_database: str | None = None,
    event_frame_template: str | None = None,
    progress=None,
) -> PIFetch:
    """A window of PI data as penguineer tables, without writing anything."""
    start, end = _utc(start), _utc(end)
    if start >= end:
        raise UsageError("PI fetch: --start must be before --end")
    streams: list[PIStream] = []
    unresolved: list[str] = []
    for name in tags:
        try:
            streams.append(resolve(client, name, data_server))
        except UsageError as e:
            if "HTTP 404" not in str(e) and "HTTP 400" not in str(e):
                raise
            unresolved.append(name)
    if tag_filter:
        if not data_server:
            raise UsageError("--tag-filter searches one data server: give --data-server")
        known = {s.web_id for s in streams}
        streams.extend(
            s for s in search_points(client, data_server, tag_filter) if s.web_id not in known
        )
    if not streams:
        raise UsageError("PI fetch: no tags found" + _unresolved_note(unresolved))

    parts: list[pl.DataFrame] = []
    for i, s in enumerate(streams):
        if progress is not None:
            progress(i, len(streams), f"reading {s.name}")
        if mode is PIMode.recorded:
            rows = recorded(client, s, start, end, page_size=page_size)
        else:
            rows = interpolated(client, s, start, end, interval, page_size=page_size)
        parts.append(
            pl.DataFrame(
                rows,
                schema={
                    "timestamp": pl.Datetime("us", "UTC"),
                    "value": pl.Float64,
                    "quality": pl.String,
                },
                orient="row",
            ).with_columns(pl.lit(s.name).alias("tag"))
        )
    frame = pl.concat(parts).select("timestamp", "tag", "value", "quality").sort("tag", "timestamp")
    tag_table = pl.DataFrame(
        {
            "tag": [s.name for s in streams],
            "description": [s.description for s in streams],
            "unit": [s.unit for s in streams],
            "range_low": [s.range_low for s in streams],
            "range_high": [s.range_high for s in streams],
            "path": [s.path for s in streams],
        },
        schema_overrides={"range_low": pl.Float64, "range_high": pl.Float64},
    )
    lots, still_open = None, 0
    if event_frame_database:
        lots, still_open = event_frames(
            client, event_frame_database, start, end, template=event_frame_template
        )
    return PIFetch(frame, tag_table, lots, unresolved, still_open)


def parse_time(text: str) -> datetime:
    """A PI Web API timestamp (ISO 8601, up to 7 fractional digits, ``Z``) as UTC."""
    t = datetime.fromisoformat(_FRACTION.sub(r"\1", text.replace("Z", "+00:00")))
    return t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)


def parse_interval(text: str) -> timedelta:
    m = _INTERVAL.match(text)
    if not m or float(m.group(1)) <= 0:
        raise UsageError(f"interval {text!r}: use a number and ms, s, m, h or d, such as 1m")
    return timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)])


def _value(item: dict[str, Any]) -> tuple[datetime, float | None, str]:
    ts = parse_time(item["Timestamp"])
    v = item.get("Value")
    if isinstance(v, dict):  # a digital state
        if v.get("IsSystem"):
            return ts, None, str(v.get("Name") or "system state")
        v = v.get("Value")
    if isinstance(v, bool):
        v = float(v)
    if not isinstance(v, (int, float)):
        return ts, None, "non-numeric"
    if item.get("Good") is False:
        return ts, None, "bad"
    if item.get("Questionable"):
        return ts, float(v), "questionable"
    if item.get("Substituted"):
        return ts, float(v), "substituted"
    return ts, float(v), "good"


def _stream(name: str, point: dict[str, Any], path: str) -> PIStream:
    zero, span = point.get("Zero"), point.get("Span")
    low = float(zero) if isinstance(zero, (int, float)) else None
    high = low + float(span) if low is not None and isinstance(span, (int, float)) else None
    if high is not None and high <= low:
        low, high = None, None
    return PIStream(
        name,
        point["WebId"],
        point.get("Path") or path,
        point.get("Descriptor") or None,
        point.get("EngineeringUnits") or None,
        low,
        high,
    )


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)


def _iso(t: datetime) -> str:
    return _utc(t).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _secret(env: str) -> str:
    value = os.environ.get(env)
    if not value:
        raise UsageError(f"PI Web API: set {env} (credentials are never passed as parameters)")
    return value


def _basic(user: str, password: str) -> str:
    import base64

    return base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


def _user_agent() -> str:
    from penguineer import __version__

    return f"penguineer/{__version__}"


def _http_error(code: int, url: str, body: str) -> str:
    detail = body[:300]
    try:
        errors = json.loads(body).get("Errors")
        if errors:
            detail = "; ".join(map(str, errors))[:300]
    except (ValueError, AttributeError):
        pass
    hint = " (check the credentials and --auth)" if code in (401, 403) else ""
    return f"PI Web API: HTTP {code} for {url}{hint}: {detail}"


def _unresolved_note(names: list[str]) -> str:
    return f"; not found: {', '.join(names)}" if names else ""
