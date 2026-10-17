from datetime import date

import pytest
from plantgraph import EdgeType, PlantGraph, Provenance, Tag, json_schema
from pydantic import ValidationError

DATA = {
    "unit": "u1",
    "nodes": [
        {"id": "R-1", "type": "equipment", "kind": "vessel", "holdup_m3": 3.0},
        {"id": "S-1", "type": "stream"},
        {
            "id": "TI-1",
            "type": "tag",
            "tag": "TI-1.PV",
            "range_low": 0,
            "range_high": 200,
            "valid_to": "2026-06-01",
        },
    ],
    "edges": [
        {
            "id": "e1",
            "type": "flows_to",
            "from": "S-1",
            "to": "R-1",
            "dead_time_min": 4,
            "provenance": "inferred",
            "source": "data",
            "confidence": 0.5,
        },
        {"id": "e2", "type": "measures", "from": "TI-1", "to": "R-1"},
    ],
}


def test_parses_typed_nodes_and_edges():
    g = PlantGraph.model_validate(DATA)
    assert isinstance(g.node("TI-1"), Tag)
    assert g.tag_by_name()["TI-1.PV"].range_high == 200
    e1 = next(g.edges_of(EdgeType.flows_to))
    assert (e1.from_id, e1.to_id, e1.provenance) == ("S-1", "R-1", Provenance.inferred)


def test_round_trip_through_plain_data():
    g = PlantGraph.model_validate(DATA)
    assert PlantGraph.model_validate(g.to_data()) == g
    assert g.to_data()["edges"][0]["from"] == "S-1"


def test_valid_time_filters_elements():
    g = PlantGraph.model_validate(DATA)
    assert "TI-1" in {n.id for n in g.valid_on(date(2026, 5, 31)).nodes}
    assert "TI-1" not in {n.id for n in g.valid_on(date(2026, 6, 1)).nodes}


@pytest.mark.parametrize(
    "bad",
    [
        {"nodes": [{"id": "x", "type": "pump"}]},
        {"nodes": [{"id": "x", "type": "tag"}]},  # tag name required
        {"edges": [{"id": "e", "type": "flows_to", "from": "a", "to": "b", "confidence": 2}]},
        {"nodes": [{"id": "x", "type": "stream", "colour": "blue"}]},
    ],
)
def test_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        PlantGraph.model_validate(bad)


def test_json_schema_uses_aliases():
    schema = json_schema()
    edge = schema["$defs"]["Edge"]["properties"]
    assert "from" in edge and "to" in edge
