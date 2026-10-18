from pathlib import Path

import pytest
from plantgraph import Actuator, Equipment, Loop, Tag

import penguineer
from penguineer.core import NotImplementedYet, UsageError
from penguineer.graph.check import check_graph
from penguineer.graph.draft import TagRow, draft_from_tags, parse_tag, read_tag_list
from penguineer.io.graph import load_graph

BENCH = Path(__file__).resolve().parent / "fixtures" / "bench" / "reactor_sensor_bias-004"


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
    again = penguineer.run(
        "graph draft", tags=BENCH / "tags.csv", project=tmp_path, force=True, llm="local"
    )
    assert any("rule-based" in n for n in again.notes)
    with pytest.raises(NotImplementedYet):
        penguineer.run(
            "graph draft", tags=BENCH / "tags.csv", pid=BENCH / "graph.yaml", project=tmp_path
        )
