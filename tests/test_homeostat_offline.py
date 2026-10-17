from penguineer.graph.check import check_graph
from penguineer.io.homeostat_live import graph_from_tags, parse_span

TAGS = """tag,name,unit,kind,group,instrument,description
JT-001,Available power,%,measurement,POWER,"power meter; noise 0.2 % of 0-100 %",Power
FT-101,Feed,Nm3/h,measurement,FIC-101,"gas flow, 0-20 Nm3/h; noise 0.5 % of span",Feed
H2.rate,Rate,Nm3/h,setting,FIC-101,,Recipe
FIC-101.rsp,Feed setpoint,Nm3/h,setpoint,FIC-101,,Setpoint
FIC-101.out,Feed valve,%,output,FIC-101,,Valve
FIC-101.mode,Mode,1,mode,FIC-101,,Mode
TT-301,Condenser outlet,degC,measurement,DRY,"RTD, -20-60 degC; noise 0.08 degC",Temp
AT-201,Purity,%,lab,,"lab analysis: a sample every 3 h, reported 2 h later",Purity
"""


def test_parse_span():
    assert parse_span("gas flow transmitter, 0-20 Nm3/h; noise 0.5 % of span") == (0, 20)
    assert parse_span("RTD, -20-60 degC; noise 0.08 degC") == (-20, 60)
    assert parse_span("power meter; noise 0.2 % of 0-100 %") == (0, 100)
    assert parse_span("lab analysis: a sample every 3 h") == (None, None)
    assert parse_span("") == (None, None)


def test_graph_from_tags(tmp_path):
    path = tmp_path / "tags.csv"
    path.write_text(TAGS)
    g = graph_from_tags(path, unit="demo")
    tags = g.tag_by_name()
    assert tags["FT-101"].range_high == 20 and tags["TT-301"].range_low == -20
    assert tags["H2.rate"].role == "setting" and tags["FIC-101.mode"].role == "mode"
    (loop,) = g.nodes_of("loop")
    assert (loop.pv, loop.sp, loop.op, loop.mode) == (
        "FT-101",
        "FIC-101.rsp",
        "FIC-101.out",
        "FIC-101.mode",
    )
    assert [q.property for q in g.nodes_of("quality")] == ["AT-201"]
    codes = {i.code for i in check_graph(g)[0]}
    assert codes <= {"quality_unreachable"}  # no topology in the public dictionary


def test_fetch_window_chunks_and_dedupes(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlparse

    from penguineer.io import homeostat_live
    from penguineer.io.historian import read_historian

    step = timedelta(minutes=10)
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("tags.csv"):
            return TAGS
        if parsed.path.endswith("lab.csv"):
            return "tag,name,unit,sampled_at,reported_at,value\n"
        q = parse_qs(parsed.query)
        since = datetime.fromisoformat(q["since"][0])
        until = datetime.fromisoformat(q["until"][0])
        lines = ["time,FT-101"]
        t = since
        while t <= until:  # both ends inclusive, like the real API may be
            lines.append(f"{t:%Y-%m-%dT%H:%M:%S}.000Z,{t.timestamp() % 7}")
            t += step
        return "\n".join(lines) + "\n"

    monkeypatch.setattr(homeostat_live, "_get", fake_get)
    since = datetime(2026, 10, 2, 17, tzinfo=UTC)
    until = since + timedelta(days=10)
    w = homeostat_live.fetch_window("p", since, until, tmp_path / "w")
    sensor_calls = [c for c in calls if "live.csv" in c]
    assert len(sensor_calls) == 2  # 7 days + 3 days
    h = read_historian(w.sensors)
    assert h.frame.height == 10 * 144 + 1
    assert h.frame["timestamp"].is_unique().all()
    # A second call reuses the files.
    homeostat_live.fetch_window("p", since, until, tmp_path / "w")
    assert len(calls) == 4
