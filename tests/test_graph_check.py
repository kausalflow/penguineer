from plantgraph import PlantGraph

from penguineer.graph.check import check_graph
from penguineer.io.graph import dump_graph, load_graph


def codes(issues):
    return sorted(i.code for i in issues)


def test_demo_graph_is_clean(demo_graph):
    issues, queue = check_graph(load_graph(demo_graph))
    assert issues == []
    assert [q.edge_id for q in queue] == ["f3"]
    assert queue[0].dependent_qualities == 1


def test_yaml_round_trip(demo_graph, tmp_path):
    g = load_graph(demo_graph)
    path = tmp_path / "g.yaml"
    path.write_text(dump_graph(g))
    assert load_graph(path) == g


def test_finds_structural_errors():
    g = PlantGraph.model_validate(
        {
            "nodes": [
                {"id": "A", "type": "equipment"},
                {"id": "B", "type": "equipment"},
                {"id": "T", "type": "tag", "tag": "T.PV"},
                {"id": "L", "type": "loop", "pv": "T"},
                {"id": "Q", "type": "quality", "property": "purity"},
                {"id": "B", "type": "stream"},
            ],
            "edges": [
                {"id": "e1", "type": "flows_to", "from": "A", "to": "B"},
                {"id": "e2", "type": "flows_to", "from": "B", "to": "A"},
                {"id": "e3", "type": "measures", "from": "A", "to": "B"},
                {"id": "e4", "type": "samples", "from": "X", "to": "A"},
            ],
        }
    )
    issues, _ = check_graph(g)
    assert codes(issues) == [
        "dangling_edge",
        "duplicate_id",
        "edge_endpoint",
        "loop_incomplete",
        "quality_unreachable",
        "unflagged_cycle",
    ]


def test_recycle_flag_allows_cycle():
    g = PlantGraph.model_validate(
        {
            "nodes": [{"id": "A", "type": "equipment"}, {"id": "B", "type": "equipment"}],
            "edges": [
                {"id": "e1", "type": "flows_to", "from": "A", "to": "B"},
                {"id": "e2", "type": "flows_to", "from": "B", "to": "A", "recycle": True},
            ],
        }
    )
    assert check_graph(g)[0] == []
