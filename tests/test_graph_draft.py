from pathlib import Path

import pytest
from plantgraph import Actuator, Equipment, Loop, Tag

import penguineer
from penguineer.core import UsageError
from penguineer.graph.check import check_graph
from penguineer.graph.draft import TagRow, apply_pid, draft_from_tags, parse_tag, read_tag_list
from penguineer.io.dexpi import read_dexpi
from penguineer.io.graph import load_graph

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BENCH = FIXTURES / "bench" / "reactor_sensor_bias-004"
PID = FIXTURES / "dexpi" / "bench-reactor.xml"


@pytest.mark.parametrize(
    "tag, prefix, letters, number, variable, role",
    [
        ("FIC-102.SP", "", "FIC", "102", "flow", "setpoint"),
        ("FI-102.PV", "", "FI", "102", "flow", "measurement"),
        ("FT-102", "", "FT", "102", "flow", "measurement"),
        ("FV-102", "", "FV", "102", "flow", "output"),
        ("TIC-301.out", "", "TIC", "301", "temperature", "output"),
        ("TIC-301.mode", "", "TIC", "301", "temperature", "mode"),
        ("11FIC1001.PV", "11", "FIC", "1001", "flow", "measurement"),
        ("U1.TI101.PV", "U1", "TI", "101", "temperature", "measurement"),
        ("PDIC-5.OP", "", "PDIC", "5", "differential pressure", "output"),
        ("FQI-7", "", "FQI", "7", "totalized flow", "measurement"),
        ("LSHH-201", "", "LSHH", "201", "level", None),
        ("GRADE", "", None, None, None, None),
    ],
)
def test_isa_names(tag, prefix, letters, number, variable, role):
    p = parse_tag(tag)
    assert (p.prefix, p.letters, p.number, p.variable, p.role) == (
        prefix,
        letters,
        number,
        variable,
        role,
    )


def test_bench_plant_loops_come_out_as_drawn():
    # The bench plant's tag list has no descriptions: names and units only.
    draft = draft_from_tags(read_tag_list(BENCH / "tags.csv"))
    truth = load_graph(BENCH / "graph.yaml")

    def loops(g):
        tags = {n.id: n.tag for n in g.nodes if isinstance(n, Tag)}
        return {
            (tags.get(n.pv), tags.get(n.sp), tags.get(n.op), tags.get(n.mode))
            for n in g.nodes
            if isinstance(n, Loop)
        }

    assert loops(draft.graph) == loops(truth)
    valves = lambda g: {n.id for n in g.nodes if isinstance(n, Actuator)}  # noqa: E731
    assert valves(draft.graph) == valves(truth) == {"TV-201", "TV-301"}
    assert draft.unparsed == ["GRADE"]
    assert all(e.provenance == "inferred" for e in draft.graph.edges)
    issues, queue = check_graph(draft.graph)
    assert not [i for i in issues if i.level == "error"]
    assert {c.edge_id for c in queue} == {e.id for e in draft.graph.edges}


def test_valves_equipment_and_unit_checks():
    rows = [
        TagRow("FIC-102.SP", unit="m3/h"),
        TagRow("FIC-102.OP", unit="%"),
        TagRow("FT-102", "Feed flow", "m3/h"),
        TagRow("FV-102", "Feed valve position", "%"),
        TagRow("TI-104.PV", "Reactor R-101 temperature, see TI-101", "degC"),
        TagRow("PI-103.PV", "Reactor pressure", "degC", equipment="R-101"),
        TagRow("FT-102", "listed twice"),
        TagRow("LIC-7.SP"),
    ]
    draft = draft_from_tags(rows)
    g = draft.graph
    loop = next(n for n in g.nodes if n.id == "loop:FIC-102")
    assert (loop.pv, loop.sp, loop.op) == ("FT-102", "FIC-102.SP", "FIC-102.OP")
    # The valve tag is the position signal; the actuator is a node of its own.
    controls = next(e for e in g.edges if e.type == "controls" and e.from_id == loop.id)
    assert isinstance(g.node(controls.to_id), Actuator) and controls.confidence == 0.8
    reactor = g.node("R-101")
    assert isinstance(reactor, Equipment) and reactor.kind == "reactor"
    measured = {(e.from_id, e.to_id, e.measured_variable) for e in g.edges if e.type == "measures"}
    assert measured == {
        ("TI-104.PV", "R-101", "temperature"),
        ("PI-103.PV", "R-101", "pressure"),
    }
    # TI-101 in a description is a tag's name, not equipment.
    assert "TI-101" not in {n.id for n in g.nodes}
    assert any("PI-103.PV" in w and "temperature" in w for w in draft.warnings)
    assert any("listed twice" in w for w in draft.warnings)
    # A loop without a PV in the list is drafted, and said so.
    assert any("loop:LIC-7" in w for w in draft.warnings)


def test_tag_list_columns(tmp_path):
    f = tmp_path / "tags.csv"
    f.write_text("TagName,Desc,UOM,Min,Max\nFT-1,Feed,m3/h,0,30\n,blank,,,\n")
    rows = read_tag_list(f)
    assert rows == [TagRow("FT-1", "Feed", "m3/h", None, 0.0, 30.0)]
    f.write_text("description\nnothing\n")
    with pytest.raises(UsageError, match="no tag column"):
        read_tag_list(f)
    f.write_text("tag,min\nFT-1,low\n")
    with pytest.raises(UsageError, match="line 2"):
        read_tag_list(f)


def test_command_writes_the_draft(tmp_path):
    result = penguineer.run("graph draft", tags=BENCH / "tags.csv", project=tmp_path)
    assert (result.n_loops, result.n_actuators, result.n_tags) == (2, 2, 13)
    graph = load_graph(Path(result.graph))
    assert graph.unit == tmp_path.name
    assert "graph check" in result.summary()
    with pytest.raises(UsageError, match="--force"):
        penguineer.run("graph draft", tags=BENCH / "tags.csv", project=tmp_path)
    again = penguineer.run("graph draft", tags=BENCH / "tags.csv", project=tmp_path, force=True)
    assert again.n_flows == 0 and any("--pid" in n for n in again.notes)
    with_pid = penguineer.run(
        "graph draft", tags=BENCH / "tags.csv", pid=PID, project=tmp_path, force=True
    )
    assert (with_pid.n_flows, with_pid.n_equipment) == (9, 3)
    assert "9 flows_to" in with_pid.summary()
    assert any("PI-305" in w for w in with_pid.warnings)


def test_pid_reader():
    pid = read_dexpi(PID)
    assert {(q.name, q.kind) for q in pid.equipment} == {
        ("M-101", "mixer"),
        ("E-201", "exchanger"),
        ("R-301", "vessel"),
    }
    flows = {(f.from_name, f.to_name) for f in pid.flows}
    # Through a tee, and from the off-page feeds.
    assert {("M-101", "E-201"), ("E-201", "R-301"), ("Feed A", "M-101")} <= flows
    steam = next(f for f in pid.flows if f.from_name == "Steam")
    assert steam.valves == ["TV-201-body"]
    by_name = {i.name: i for i in pid.instruments}
    tic = by_name["TIC-201"]
    assert (tic.valve, tic.valve_target) == ("TV-201", "E-201")
    assert [(s.name, s.located_at, s.confidence) for s in tic.sensors] == [
        ("TT-201", "E-201", 0.85)
    ]
    # On a pipe, not a nozzle: the equipment or feed the pipe comes from, less sure.
    assert [(s.located_at, s.confidence) for s in by_name["FI-101"].sensors] == [("Feed A", 0.6)]
    assert by_name["TI-200"].sensors[0].located_at == "M-101"


def test_pid_completes_the_tag_list_draft():
    draft = draft_from_tags(read_tag_list(BENCH / "tags.csv"))
    apply_pid(draft, read_dexpi(PID))
    g = draft.graph
    truth = load_graph(BENCH / "graph.yaml")

    def edges(graph, kind):
        return {(e.from_id, e.to_id) for e in graph.edges if e.type == kind}

    # The truth's loops, valves, and where its loop sensors and valves act, as drawn.
    assert {(a.removeprefix("loop:"), b) for a, b in edges(g, "controls")} == edges(
        truth, "controls"
    )
    assert edges(g, "manipulates") == edges(truth, "manipulates")
    assert {("TT-201", "E-201"), ("TT-301", "R-301"), ("AT-101", "M-101")} <= edges(g, "measures")
    assert {("M-101", "E-201"), ("E-201", "R-301")} <= edges(g, "flows_to")
    assert all(e.provenance == "inferred" for e in g.edges)
    assert {e.source for e in g.edges if e.type != "measures"} == {"dexpi"}
    issues, _ = check_graph(g)
    assert not [i for i in issues if i.level == "error"]


def test_tag_list_wins_over_pid_when_more_sure(tmp_path):
    rows = [
        TagRow("TIC-201.SP"),
        TagRow("TT-201", "Heater outlet", "degC", equipment="R-301"),
        TagRow("TV-201", "Valve position", "%"),
    ]
    draft = draft_from_tags(rows)
    pid = read_dexpi(PID)
    pid.instruments = [i for i in pid.instruments if i.name == "TIC-201"]
    pid.instruments[0].valve = "TV-9"
    apply_pid(draft, pid)
    measures = {(e.from_id, e.to_id) for e in draft.graph.edges if e.type == "measures"}
    assert measures == {("TT-201", "R-301")}
    assert any("TT-201: the P&ID puts it on E-201" in w for w in draft.warnings)
    assert any("names its valve TV-9" in w for w in draft.warnings)
    # The equipment column named R-301 first; the P&ID confirms it and gives its kind.
    reactor = draft.graph.node("R-301")
    assert (reactor.source, reactor.kind, reactor.confidence) == ("dexpi", "reactor", 0.9)


def test_not_dexpi(tmp_path):
    f = tmp_path / "pid.xml"
    f.write_text("<Drawing/>")
    with pytest.raises(UsageError, match="not a DEXPI file"):
        read_dexpi(f)
    f.write_text("<PlantModel")
    with pytest.raises(UsageError, match="not readable as XML"):
        read_dexpi(f)
