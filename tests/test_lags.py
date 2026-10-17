import json

import numpy as np
import polars as pl
import pytest
import yaml

import penguineer
from penguineer.engines.lags import ar_filter, estimate_dead_time, fit_ar

MIN = 60_000_000


def delayed_pair(delay, n=3000, gain=0.8, noise=0.5, seed=1):
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.95 * x[i - 1] + rng.normal()
    y = np.r_[np.zeros(delay), gain * x[:-delay]] + rng.normal(0, noise, n)
    t = np.arange(n, dtype=np.int64) * MIN
    return t, x, y


def test_ar_fit_and_filter_whiten():
    rng = np.random.default_rng(0)
    x = np.zeros(5000)
    for i in range(1, x.size):
        x[i] = 0.9 * x[i - 1] + rng.normal()
    coef = fit_ar(x)
    assert coef[0] == pytest.approx(0.9, abs=0.05)
    e = ar_filter(x, coef)
    assert abs(np.corrcoef(e[:-1], e[1:])[0, 1]) < 0.05


@pytest.mark.parametrize("delay", [5, 37, 90])
def test_finds_known_delay(delay):
    t, x, y = delayed_pair(delay)
    est = estimate_dead_time(t, x, t, y, max_lag_s=180 * 60)
    assert est.significant
    assert est.low_min <= delay <= est.high_min
    assert est.lag_min == delay


def test_reverse_acting_link_has_negative_correlation():
    t, x, y = delayed_pair(20, gain=-0.8)
    est = estimate_dead_time(t, x, t, y, max_lag_s=60 * 60)
    assert est.lag_min == 20 and est.corr < 0


def test_unrelated_signals_are_not_significant():
    t, x, _ = delayed_pair(10)
    y = np.random.default_rng(9).normal(size=t.size)
    assert not estimate_dead_time(t, x, t, y, max_lag_s=60 * 60).significant


def test_different_sampling_rates():
    t, x, y = delayed_pair(30)
    est = estimate_dead_time(t, x, t[::5], y[::5], max_lag_s=120 * 60)
    assert est.step_s == 300
    assert est.low_min <= 30 <= est.high_min


def test_too_short_overlap():
    t, x, y = delayed_pair(10, n=40)
    assert estimate_dead_time(t, x, t, y, max_lag_s=60 * 60) is None


@pytest.fixture
def plant(tmp_path):
    t, x, y = delayed_pair(25)
    _, z, _ = delayed_pair(25, seed=7)
    ts = pl.Series("timestamp", t, dtype=pl.Datetime("us")).dt.replace_time_zone("UTC")
    data = tmp_path / "historian.csv"
    pl.DataFrame({"timestamp": ts, "FT-1": x, "TT-2": y, "TT-3": z}).write_csv(data)
    graph = {
        "nodes": [
            {"id": "S1", "type": "stream"},
            {"id": "E1", "type": "equipment"},
            {"id": "E2", "type": "equipment"},
            {"id": "E3", "type": "equipment"},
            {"id": "FT-1", "type": "tag", "tag": "FT-1"},
            {"id": "TT-2", "type": "tag", "tag": "TT-2"},
            {"id": "TT-3", "type": "tag", "tag": "TT-3"},
        ],
        "edges": [
            {"id": "f1", "type": "flows_to", "from": "S1", "to": "E1"},
            {
                "id": "f2",
                "type": "flows_to",
                "from": "E1",
                "to": "E2",
                "dead_time_min": 3,
                "provenance": "confirmed",
            },
            {"id": "f3", "type": "flows_to", "from": "S1", "to": "E3"},
            {"id": "f4", "type": "flows_to", "from": "E3", "to": "E2"},
            {"id": "m1", "type": "measures", "from": "FT-1", "to": "S1"},
            {"id": "m2", "type": "measures", "from": "TT-2", "to": "E1"},
            {"id": "m3", "type": "measures", "from": "TT-3", "to": "E3"},
        ],
    }
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph))
    return data, path


def test_graph_lags_command(plant, tmp_path):
    data, graph = plant
    original = graph.read_text()
    outcome = penguineer.execute("graph lags", data=data, graph=graph, project=tmp_path)
    status = {e.edge_id: e.status for e in outcome.result.edges}
    assert status == {
        "f1": "estimated",
        "f2": "kept_confirmed",
        "f3": "no_clear_lag",
        "f4": "no_tags",
    }
    assert graph.read_text() == original  # not in place: input untouched
    out = yaml.safe_load((outcome.run_dir / "graph.yaml").read_text())
    edges = {e["id"]: e for e in out["edges"]}
    assert edges["f1"]["dead_time_min"] == 25
    assert edges["f1"]["provenance"] == "inferred" and edges["f1"]["source"] == "data"
    assert edges["f2"]["dead_time_min"] == 3 and edges["f2"]["provenance"] == "confirmed"


def test_graph_lags_in_place_logs_history(plant, tmp_path):
    data, graph = plant
    penguineer.run("graph lags", data=data, graph=graph, in_place=True, project=tmp_path)
    assert "dead_time_min: 25" in graph.read_text()
    (event,) = [json.loads(line) for line in (tmp_path / "graph.history.jsonl").open()]
    assert event["edge_id"] == "f1" and event["after"]["dead_time_min"] == 25
