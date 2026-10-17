"""Tests on real data from Homeostat Live (https://homeostat.kausalflow.com/live/).

They use a fixed, already released week of the isidis-01 plant, so the data does not change
between runs. Without network access they are skipped, unless PENGUINEER_REQUIRE_HOMEOSTAT is
set (CI sets it), in which case they fail. Downloads are cached in PENGUINEER_HOMEOSTAT_CACHE
(default: tests/.cache/homeostat).
"""

import csv
import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

import penguineer
from penguineer.core import UsageError
from penguineer.graph.check import check_graph
from penguineer.io.graph import save_graph
from penguineer.io.historian import read_historian
from penguineer.io.homeostat_live import LiveWindow, fetch_window, graph_from_tags

PLANT = "isidis-01"
# The plant's first public benchmark week: released and frozen, so it never changes.
SINCE = datetime(2026, 10, 2, 17, tzinfo=UTC)
UNTIL = datetime(2026, 10, 9, 17, tzinfo=UTC)

pytestmark = pytest.mark.homeostat


@pytest.fixture(scope="module")
def window() -> LiveWindow:
    default = Path(__file__).parent / ".cache" / "homeostat"
    cache = Path(os.environ.get("PENGUINEER_HOMEOSTAT_CACHE", default))
    dest = cache / f"{PLANT}_{SINCE:%Y%m%dT%H%M}_{UNTIL:%Y%m%dT%H%M}"
    try:
        return fetch_window(PLANT, SINCE, UNTIL, dest)
    except UsageError as e:
        if os.environ.get("PENGUINEER_REQUIRE_HOMEOSTAT"):
            raise
        pytest.skip(f"Homeostat Live not reachable: {e}")


@pytest.fixture(scope="module")
def dictionary(window) -> list[dict[str, str]]:
    with window.tags.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def graph_path(window, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("graph") / "graph.yaml"
    return save_graph(graph_from_tags(window.tags, unit=PLANT), path)


def test_sensor_export_reads_as_a_historian(window, dictionary):
    h = read_historian(window.sensors)
    sensor_tags = {r["tag"] for r in dictionary if r["kind"] != "lab"}
    assert h.layout == "wide"
    assert set(h.frame["tag"].unique()) == sensor_tags
    ts = h.frame["timestamp"]
    assert ts.min() >= SINCE and ts.max() <= UNTIL
    periods = (
        h.frame.group_by("tag")
        .agg(pl.col("timestamp").diff().median().dt.total_seconds().alias("s"))["s"]
        .unique()
        .to_list()
    )
    assert periods == [600]  # one snapshot every 10 minutes


def test_lab_results_match_the_dictionary(window, dictionary):
    lab = pl.read_csv(window.lab, try_parse_dates=True)
    assert lab.columns == ["tag", "name", "unit", "sampled_at", "reported_at", "value"]
    assert lab.height > 0
    assert set(lab["tag"].unique()) <= {r["tag"] for r in dictionary if r["kind"] == "lab"}
    assert lab["value"].dtype == pl.Float64
    assert (lab["reported_at"] > lab["sampled_at"]).all()

    from penguineer.io.lims import read_lims

    lims = read_lims(window.lab)
    assert lims.frame.height == lab.height
    assert lims.frame["sampled_at"].min() >= SINCE


def test_graph_from_dictionary(window, dictionary, graph_path):
    from penguineer.io.graph import load_graph

    graph = load_graph(graph_path)
    tags = graph.tag_by_name()
    assert set(tags) == {r["tag"] for r in dictionary if r["kind"] != "lab"}
    assert any(t.range_high is not None for t in tags.values())
    assert list(graph.nodes_of("loop"))
    issues, _ = check_graph(graph)
    structural = {"duplicate_id", "dangling_edge", "edge_endpoint", "loop_reference"}
    assert not [i for i in issues if i.code in structural]


def test_data_health_on_live_week(window, graph_path, tmp_path):
    result = penguineer.run("data health", data=window.sensors, graph=graph_path, project=tmp_path)
    graph = graph_from_tags(window.tags)
    still = {t.tag for t in graph.nodes_of("tag") if t.role in ("setpoint", "mode", "setting")}
    assert result.n_tags == len(list(graph.nodes_of("tag")))
    assert result.exit_code() == 0
    # Setpoints, modes and recipe settings sit still by design and are never "frozen".
    assert not [f for f in result.findings if f.check == "flatline" and f.tag in still]
    # Every reading inside its instrument range is expected of a healthy simulated sensor;
    # out-of-range findings must name a tag that has a range.
    ranged = {t.tag for t in graph.nodes_of("tag") if t.range_high is not None}
    assert {f.tag for f in result.findings if f.check == "out_of_range"} <= ranged
