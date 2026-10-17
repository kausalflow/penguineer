import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from plantgraph import PlantGraph

import penguineer
from penguineer.core import NotEnoughData, UsageError
from penguineer.engines.align import Grid, resample, upstream_paths
from penguineer.engines.evidence import (
    change_point,
    effect_size,
    explained_fraction,
    onset_strength,
)
from penguineer.engines.investigate import investigate
from penguineer.io.graph import load_graph
from penguineer.io.historian import read_historian
from penguineer.io.lims import read_lims
from penguineer.report.model import Report

DEMO = Path(__file__).resolve().parents[1] / "examples" / "offspec"


# ------------------------------------------------------------ engines


def test_upstream_paths_sum_dead_times():
    graph = PlantGraph.model_validate(
        {
            "nodes": [{"id": i, "type": "equipment"} for i in "ABCD"],
            "edges": [
                {"id": "1", "type": "flows_to", "from": "A", "to": "B", "dead_time_min": 2},
                {
                    "id": "2",
                    "type": "flows_to",
                    "from": "B",
                    "to": "C",
                    "dead_time_min": 5,
                    "dead_time_low_min": 4,
                    "dead_time_high_min": 7,
                },
                {"id": "3", "type": "flows_to", "from": "C", "to": "B", "recycle": True},
                {"id": "4", "type": "flows_to", "from": "D", "to": "C"},
            ],
        }
    )
    paths = upstream_paths(graph, "C", max_hops=5)
    assert paths["A"].path == ["A", "B", "C"]
    assert (paths["A"].dead_time_min, paths["A"].low_min, paths["A"].high_min) == (7, 6, 9)
    assert not paths["D"].known and paths["D"].dead_time_min == 0
    assert set(upstream_paths(graph, "C", max_hops=1)) == {"C", "B", "D"}


def test_resample_leaves_gaps_empty():
    t = np.array([0, 60, 120, 600, 660]) * 1_000_000
    x = np.array([0.0, 1, 2, 3, 4])
    grid = Grid.spanning(0, 660 * 1_000_000, 60 * 1_000_000)
    y = resample(t, x, grid.times, max_gap_us=180 * 1_000_000)
    assert y[1] == 1 and np.isnan(y[4]) and y[-1] == 4


def test_change_point_and_effect_size():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(10, 0.1, 200), rng.normal(9, 0.1, 100)])
    cp = change_point(x)
    assert abs(cp.index - 200) <= 2 and cp.shift == pytest.approx(-1, abs=0.05)
    eff = effect_size(x[200:], x[:200])
    assert eff.z < -6 and eff.strength == 1
    assert effect_size(rng.normal(10, 0.1, 50), x[:200]).strength == 0


def test_onset_strength_rejects_late_causes():
    assert onset_strength(0, 0, 10) == 1
    assert 0 < onset_strength(-10, 0, 10) < 1
    assert onset_strength(15, 0, 10) == 0


def test_explained_fraction():
    rng = np.random.default_rng(1)
    up = rng.normal(0, 1, 500)
    down = 0.5 * up + rng.normal(0, 0.1, 500)
    assert explained_fraction(up, down, -6.0, -3.0) == pytest.approx(1, abs=0.05)
    assert explained_fraction(up, rng.normal(0, 1, 500), -6.0, -3.0) < 0.2


# ------------------------------------------------------------ the demo plant


@pytest.fixture(scope="module")
def demo():
    graph = load_graph(DEMO / "graph.yaml")
    historian = read_historian(DEMO / "historian.csv").frame
    lims = read_lims(DEMO / "lims.csv").frame
    return graph, historian, lims


def test_demo_finds_the_preheater(demo):
    graph, historian, lims = demo
    inv = investigate(graph, historian, lims, "L0803-A")
    top = inv.hypotheses[0]
    assert top.cause_tag == "TI-101.PV" and top.cause_node == "E-101"
    assert top.confidence == "high" and top.direction == "low"
    assert top.graph_path == ["E-101", "R-101", "S-product"] and top.dead_time_min == 50
    # The reactor temperature only passes the preheater's drop on.
    reasons = {r.cause: r.reason for r in inv.ruled_out}
    assert "predicted from TI-101.PV" in reasons["TI-104.PV"]
    # The flow loop held its flow while the header pressure dipped.
    assert "held at setpoint by FIC-102" in reasons["FI-102.PV"]
    decoy = next(h for h in inv.hypotheses if h.cause_tag == "PT-100.PV")
    assert decoy.score < top.score - 0.3
    # The frozen pressure is reported as a data fault, not as a process cause.
    fault = next(h for h in inv.hypotheses if h.cause_tag == "PI-103.PV")
    assert fault.kind == "measurement_fault"
    # The grade-2 lot is not a reference.
    assert "L0802-B" not in {w.lot for w in inv.reference}
    assert inv.regime == {"GRADE": 1.0}


def test_reference_needs_the_same_regime(demo):
    graph, historian, lims = demo
    with pytest.raises(NotEnoughData, match="same regime"):
        investigate(graph, historian, lims, "L0802-B", quality="purity")


def test_in_spec_lot_needs_quality(demo):
    graph, historian, lims = demo
    with pytest.raises(NotEnoughData, match="within spec"):
        investigate(graph, historian, lims, "L0801-B")


def test_unknown_lot(demo):
    graph, historian, lims = demo
    with pytest.raises(UsageError, match="not in the LIMS"):
        investigate(graph, historian, lims, "nope")


def test_setpoint_change_becomes_its_own_hypothesis(demo):
    graph, historian, lims = demo
    # An operator lowers the feed setpoint at 05:00 and the flow follows; nothing else moves.
    t0 = datetime(2026, 8, 3, 5, tzinfo=UTC)
    moved = historian.with_columns(
        pl.when(pl.col("tag").is_in(["FIC-102.SP", "FI-102.PV"]) & (pl.col("timestamp") >= t0))
        .then(pl.col("value") - 2.0)
        .otherwise(pl.col("value"))
        .alias("value")
    )
    inv = investigate(graph, moved, lims, "L0803-A")
    kinds = {h.cause_tag: h.kind for h in inv.hypotheses}
    assert kinds.get("FIC-102.SP") == "setpoint_change"
    assert any(r.cause == "FI-102.PV" and "setpoint" in r.reason for r in inv.ruled_out)


# ------------------------------------------------------------ the command


def test_command_writes_the_report(tmp_path):
    result = penguineer.run(
        "investigate",
        lot="L0803-A",
        lims=DEMO / "lims.csv",
        data=DEMO / "historian.csv",
        graph=DEMO / "graph.yaml",
        lots=DEMO / "lots.csv",
        project=tmp_path,
    )
    assert result.hypotheses[0].cause_tag == "TI-101.PV"
    report = Report.model_validate_json(Path(result.report_json).read_text())
    assert report.event.window_start == datetime(2026, 8, 3, tzinfo=UTC)
    out = Path(result.report_dir)
    for e in report.evidence:
        if e.path:
            assert (out / e.path).stat().st_size > 1000
    assert report.hypotheses[0].evidence[0] in {e.id for e in report.evidence}
    html = Path(result.report_html).read_text()
    assert "Lot L0803-A" in html and "TI-101.PV" in html and "<script" not in html
    assert "TI-101.PV" in Path(result.report_md).read_text()
    assert "TI-101.PV" in result.summary()


def test_command_exit_code_3(tmp_path):
    from typer.testing import CliRunner

    from penguineer.cli import build_app
    from penguineer.core import default_registry

    res = CliRunner().invoke(
        build_app(default_registry),
        [
            "investigate",
            "--lot",
            "L0801-B",
            "--lims",
            str(DEMO / "lims.csv"),
            "--data",
            str(DEMO / "historian.csv"),
            "--graph",
            str(DEMO / "graph.yaml"),
            "--project",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 3
    record = json.loads(next((tmp_path / "runs").glob("*/run.json")).read_text())
    assert record["exit_code"] == 3
