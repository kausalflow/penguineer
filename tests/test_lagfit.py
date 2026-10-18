import importlib.util
import os
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import penguineer
from penguineer.core import UsageError
from penguineer.engines.lagfit import first_order, fit_lag, throughput

MIN = 60_000_000
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def line(dead_m3=5.0, mix_m3=3.0, days=4, noise=0.3, flow_swing=0.6, seed=1):
    """A cause carried through plug flow (``dead_m3``) and a mixed volume (``mix_m3``)."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    t = np.arange(n, dtype=np.int64) * MIN
    f = np.zeros(n)
    x = np.zeros(n)
    for i in range(1, n):
        f[i] = 0.995 * f[i - 1] + rng.normal(0, flow_swing)
        x[i] = 0.97 * x[i - 1] + rng.normal()
    flow = 20 + f
    vol = throughput(t, t, flow)
    grid = np.linspace(0, vol[-1], n)
    mixed = first_order(np.interp(grid, vol, x), mix_m3 / (grid[1] - grid[0]))
    y = np.interp(vol - dead_m3, grid, mixed) + rng.normal(0, noise, n)
    return t, x, y, flow


def test_first_order_and_throughput():
    step = first_order(np.r_[np.zeros(5), np.ones(200)], 10.0)
    assert step[5 + 10] == pytest.approx(1 - np.exp(-1.1), abs=0.01)
    t = np.arange(61, dtype=np.int64) * MIN
    assert throughput(t, t, np.full(61, 30.0))[-1] == pytest.approx(30.0)


def test_mixing_in_volume_recovers_both_volumes():
    t, x, y, flow = line()
    fit = fit_lag(t, x, t, y, flow=(t, flow), max_lag_s=120 * 60)
    assert fit.best == "mixing"
    mixing = next(f for f in fit.fits if f.model == "mixing")
    assert mixing.coordinate == "volume"
    assert mixing.dead_low - 0.3 <= 5.0 <= mixing.dead_high + 0.3
    assert mixing.mixing == pytest.approx(3.0, rel=0.25)
    fixed = next(f for f in fit.fits if f.model == "fixed")
    assert abs(mixing.corr) > abs(fixed.corr)


def test_plug_flow_prefers_the_flow_model():
    t, x, y, flow = line(dead_m3=8.0, mix_m3=0.0, flow_swing=1.0)
    fit = fit_lag(t, x, t, y, flow=(t, flow), max_lag_s=120 * 60)
    assert fit.best == "flow"
    f = next(f for f in fit.fits if f.model == "flow")
    assert f.dead == pytest.approx(8.0, abs=0.3)


def test_without_flow_mixing_is_in_time():
    t, x, y, _ = line(flow_swing=0.0)
    fit = fit_lag(t, x, t, y, max_lag_s=120 * 60)
    assert [f.model for f in fit.fits] == ["fixed", "mixing"]
    mixing = fit.fits[1]
    assert mixing.coordinate == "time"
    # 5 m3 then 3 m3 at 20 m3/h: 15 min dead, 9 min time constant.
    assert mixing.dead == pytest.approx(15, abs=1.5)
    assert mixing.mixing == pytest.approx(9, rel=0.3)


def test_lab_effect_against_the_chance_level():
    t, x, y, flow = line(days=8)
    idx = np.arange(0, t.size, 60)
    fit = fit_lag(t, x, t[idx], y[idx], lab=True, flow=(t, flow), max_lag_s=120 * 60)
    assert fit.effect_kind == "lab" and fit.best is not None
    assert all(p.chance is not None and 0 < p.chance < 1 for p in fit.profiles)
    mixing = next(f for f in fit.fits if f.model == "mixing")
    assert mixing.residence_min == pytest.approx(8 / 20 * 60, abs=6)


def test_unrelated_lab_values_are_chance():
    t, x, _, flow = line(days=8)
    idx = np.arange(0, t.size, 60)
    noise = np.random.default_rng(0).normal(size=idx.size)
    fit = fit_lag(t, x, t[idx], noise, lab=True, flow=(t, flow), max_lag_s=120 * 60)
    assert fit.best is None


def test_too_little_data():
    t, x, y, _ = line(days=1)
    assert fit_lag(t[:100], x[:100], t[:100], y[:100], max_lag_s=120 * 60) is None


def test_lag_command(tmp_path):
    t, x, y, flow = line()
    stamps = pl.Series((t // 1000).astype("int64")).cast(pl.Datetime("ms", "UTC"))
    pl.DataFrame({"timestamp": stamps, "AT-1": x, "AT-2": y, "FT-1": flow}).write_csv(
        tmp_path / "h.csv"
    )
    result = penguineer.run(
        "lag",
        data=tmp_path / "h.csv",
        cause="AT-1",
        effect="AT-2",
        flow=["FT-1"],
        max_lag_minutes=120,
        project=tmp_path,
    )
    assert result.best == "mixing" and result.volume_unit == "m3"
    assert result.lag_min == pytest.approx(8 / 20 * 60, abs=3)
    text = result.summary()
    assert "plug flow through" in text and "mixed volume" in text
    assert all((tmp_path / "runs").glob("*/lag-*.png"))
    for p in result.plots:
        assert p.endswith(".png")


def test_lag_command_on_a_lab_property(tmp_path):
    t, x, y, flow = line(days=8)
    stamps = pl.Series((t // 1000).astype("int64")).cast(pl.Datetime("ms", "UTC"))
    pl.DataFrame({"timestamp": stamps, "AT-1": x, "FT-1": flow}).write_csv(tmp_path / "h.csv")
    idx = np.arange(0, t.size, 60)
    pl.DataFrame(
        {"property": "purity", "value": y[idx], "sampled_at": stamps.gather(idx)}
    ).write_csv(tmp_path / "lims.csv")
    result = penguineer.run(
        "lag",
        data=tmp_path / "h.csv",
        cause="AT-1",
        effect="purity",
        lims=tmp_path / "lims.csv",
        flow=["FT-1"],
        max_lag_minutes=120,
        project=tmp_path,
    )
    assert result.effect_kind == "lab" and result.n > 100
    assert "x chance" in result.summary()


def test_lag_command_missing_tag(tmp_path):
    t, x, y, _ = line(days=1)
    stamps = pl.Series((t // 1000).astype("int64")).cast(pl.Datetime("ms", "UTC"))
    pl.DataFrame({"timestamp": stamps, "AT-1": x}).write_csv(tmp_path / "h.csv")
    with pytest.raises(UsageError, match="no samples for AT-9"):
        penguineer.run(
            "lag", data=tmp_path / "h.csv", cause="AT-1", effect="AT-9", project=tmp_path
        )


@pytest.mark.skipif(
    os.environ.get("PENGUINEER_BENCH") != "1", reason="set PENGUINEER_BENCH=1 to run Homeostat"
)
def test_on_a_homeostat_plant():
    import homeostat

    spec = importlib.util.spec_from_file_location("validate_lag", SCRIPTS / "validate_lag.py")
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    run = homeostat.family(v.config(20, 1, 3, 3000), plants=3).run(2)
    units = {u["id"]: u for u in run.meta["scenario"]["units"]}
    obs = run.observed
    t = np.arange(len(obs), dtype=np.int64) * MIN
    total = (obs["FT-101"] + obs["FT-102"]).to_numpy(dtype=float)
    ok = np.isfinite(total)
    lab_t, lab_x = v.lab_samples(obs, 3600)
    fit = fit_lag(
        t[ok],
        obs["FT-102"].to_numpy(dtype=float)[ok],
        lab_t,
        lab_x,
        lab=True,
        flow=(t[ok], total[ok]),
        max_lag_s=150 * 60,
    )
    mixing = next(f for f in fit.fits if f.model == "mixing")
    dead = units["MX-101"]["volume"] + units["TIC-301"]["volume"]
    assert mixing.significant
    assert mixing.dead_low <= dead <= mixing.dead_high
