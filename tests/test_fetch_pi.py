"""`fetch pi` against an in-process PI Web API."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from fake_piwebapi import PASSWORD, T0, USER, FakePIWebAPI
from typer.testing import CliRunner

import penguineer.commands  # noqa: F401
from penguineer.cli import build_app
from penguineer.connectors import pi
from penguineer.core import UsageError, default_registry
from penguineer.io.historian import read_historian
from penguineer.io.lims import read_lots

END = T0 + timedelta(hours=4)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv(pi.PASSWORD_ENV, PASSWORD)
    with FakePIWebAPI(auth="basic") as s:
        yield s


def client(server, **kw) -> pi.PIClient:
    return pi.PIClient(server.url, pi.PIAuth.basic, USER, **kw)


def test_recorded_pages_without_losing_or_repeating_values(server):
    c = client(server)
    stream = pi.resolve(c, "TI102.PV", "PISRV")
    rows = pi.recorded(c, stream, T0, END, page_size=5)
    assert len(rows) == 62  # 60 times, three values at one of them
    assert [r[1] for r in rows if r[0] == T0 + timedelta(minutes=48)] == [312.0, 400.0, 401.0]
    assert all(a[0] <= b[0] for a, b in zip(rows, rows[1:], strict=False))
    assert len([p for p in server.requests if "recorded" in p]) > 10


def test_too_many_values_at_one_time_says_so(server):
    c = client(server)
    stream = pi.resolve(c, "TI102.PV", "PISRV")
    start = T0 + timedelta(minutes=48)
    with pytest.raises(UsageError, match="raise --page-size"):
        pi.recorded(c, stream, start, END, page_size=2)


def test_quality_and_digital_states(server):
    got = pi.fetch(client(server), T0, END, tags=["FIC101.PV", "XV103.ST"], data_server="PISRV")
    flow = got.frame.filter(pl.col("tag") == "FIC101.PV")
    assert flow.height == 240
    bad = flow.filter(pl.col("timestamp") == T0 + timedelta(minutes=30)).row(0, named=True)
    assert bad["value"] is None and bad["quality"] == "I/O Timeout"
    assert flow.filter(pl.col("quality") == "questionable").height == 1
    valve = got.frame.filter(pl.col("tag") == "XV103.ST")
    assert set(valve["value"]) == {0.0, 1.0}  # a state's index is its value
    tags = got.tags.filter(pl.col("tag") == "FIC101.PV").row(0, named=True)
    assert (tags["unit"], tags["range_low"], tags["range_high"]) == ("m3/h", 0.0, 50.0)


def test_paths_attributes_filter_and_unknown_tags(server):
    got = pi.fetch(
        client(server),
        T0,
        END,
        tags=["\\\\PISRV\\TI102.PV", "\\\\AFSRV\\Plant\\Reactor|Feed Flow", "NOPE"],
        tag_filter="FIC*",
        data_server="PISRV",
    )
    assert set(got.tags["tag"]) == {
        "\\\\PISRV\\TI102.PV",
        "\\\\AFSRV\\Plant\\Reactor|Feed Flow",
        "FIC101.PV",
    }
    assert got.unresolved == ["NOPE"]
    # TI102's span is 0, so it has no range rather than an empty one.
    ti = got.tags.filter(pl.col("tag").str.contains("TI102")).row(0, named=True)
    assert ti["range_low"] is None


def test_interpolated_grid(server):
    c = client(server)
    stream = pi.resolve(c, "FIC101.PV", "PISRV")
    rows = pi.interpolated(c, stream, T0, END, "10m", page_size=4)
    assert [r[0] for r in rows] == [T0 + timedelta(minutes=10 * i) for i in range(24)]


def test_event_frames_become_lots(server):
    lots, still_open = pi.event_frames(
        client(server), "\\\\AFSRV\\Plant", T0, END + timedelta(hours=1), template="Batch"
    )
    assert lots["lot"].to_list() == ["L-001", "L-002"]
    assert still_open == 1


def test_wrong_password_names_the_problem(server, monkeypatch):
    monkeypatch.setenv(pi.PASSWORD_ENV, "wrong")
    with pytest.raises(UsageError, match="HTTP 401.*credentials"):
        pi.resolve(client(server), "FIC101.PV", "PISRV")


def test_missing_secret_is_not_a_parameter(server, monkeypatch):
    monkeypatch.delenv(pi.PASSWORD_ENV)
    with pytest.raises(UsageError, match="PI_WEBAPI_PASSWORD"):
        pi.resolve(client(server), "FIC101.PV", "PISRV")


def test_bearer_token(monkeypatch):
    monkeypatch.setenv(pi.TOKEN_ENV, "tok")
    with FakePIWebAPI(auth="bearer") as s:
        c = pi.PIClient(s.url, pi.PIAuth.bearer)
        assert pi.resolve(c, "FIC101.PV", "PISRV").unit == "m3/h"


def test_parse_time_seven_digits():
    assert pi.parse_time("2026-10-01T00:00:00.1234567Z") == datetime(
        2026, 10, 1, 0, 0, 0, 123456, tzinfo=UTC
    )


def test_command_writes_files_every_command_reads(server, tmp_path):
    outcome = default_registry.execute(
        "fetch pi",
        {
            "server": server.url,
            "start": T0,
            "end": END,
            "tags": ["FIC101.PV", "TI102.PV"],
            "data_server": "PISRV",
            "auth": "basic",
            "username": USER,
            "event_frames": "\\\\AFSRV\\Plant",
            "event_frame_template": "Batch",
        },
        project=tmp_path,
    )
    r = outcome.result
    assert r.n_tags == 2 and r.n_lots == 2
    assert r.quality["I/O Timeout"] == 1
    hist = read_historian(tmp_path / "data/pi/historian.parquet")
    assert set(hist.frame["tag"]) == {"FIC101.PV", "TI102.PV"}
    assert read_lots(tmp_path / "data/pi/lots.csv").height == 2
    # The password is never in the run record.
    record = (outcome.run_dir / "run.json").read_text()
    assert PASSWORD not in record


def test_cli_data_health_on_fetched_data(server, tmp_path):
    runner, app = CliRunner(), build_app(default_registry)
    args = ["fetch", "pi", "--server", server.url, "--data-server", "PISRV"]
    args += ["--tags", "FIC101.PV", "--start", T0.isoformat(), "--end", END.isoformat()]
    args += ["--auth", "basic", "--username", USER, "--project", str(tmp_path)]
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert "fetch pi: 1 tags, 240 values" in res.output
    tags_csv = tmp_path / "data/pi/tags.csv"
    assert tags_csv.read_text().startswith("tag,description,unit,range_low,range_high,path")
    data = tmp_path / "data/pi/historian.parquet"
    res = runner.invoke(app, ["data", "health", "--data", str(data), "--project", str(tmp_path)])
    assert res.exit_code == 0, res.output
