import numpy as np
import pytest

from penguineer.engines.health import HealthCheck, HealthConfig, Severity, check_tag, runs

MIN = 60_000_000  # one minute in microseconds


def series(n=600, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.int64) * MIN
    x = 50 + np.sin(np.arange(n) / 50) + rng.normal(0, 0.2, n)
    return t, x


def checks(found):
    return [f["check"] for f in found]


def test_runs_helper():
    assert runs(np.array([0, 1, 1, 0, 1], dtype=bool)) == [(1, 2), (4, 4)]
    assert runs(np.array([], dtype=bool)) == []


def test_clean_signal_has_no_findings():
    t, x = series()
    found, summary = check_tag("T", t, x, HealthConfig())
    assert found == []
    assert summary.coverage == 1.0 and summary.median_period_s == 60


def test_flatline():
    t, x = series()
    x[100:200] = x[100]
    found, _ = check_tag("T", t, x, HealthConfig())
    assert checks(found) == [HealthCheck.flatline]
    assert found[0]["metrics"]["duration_s"] == 99 * 60


def test_flatline_allowed_for_setpoints():
    t, x = series()
    x[:] = 12.0
    assert check_tag("SP", t, x, HealthConfig(), allow_flat=True)[0] == []


def test_short_flat_run_is_not_reported():
    t, x = series()
    x[100:130] = x[100]
    assert check_tag("T", t, x, HealthConfig())[0] == []


def test_gap_severity():
    t, x = series()
    keep = np.ones(t.size, bool)
    keep[100:110] = False  # 11 min gap -> warning
    keep[300:400] = False  # 101 min gap -> critical (> 60 x median)
    found, summary = check_tag("T", t[keep], x[keep], HealthConfig())
    gaps = [f for f in found if f["check"] == HealthCheck.gap]
    assert [g["severity"] for g in gaps] == [Severity.warning, Severity.critical]
    assert gaps[0]["metrics"]["missing_samples"] == 10
    assert summary.coverage == pytest.approx(490 / 600, abs=1e-3)


def test_spike_but_not_step():
    t, x = series()
    x[250] += 10
    x[400:] += 10  # a level shift is a process change, not a spike
    found, _ = check_tag("T", t, x, HealthConfig())
    assert checks(found) == [HealthCheck.spike]
    assert found[0]["start"] == found[0]["end"]


def test_out_of_range():
    t, x = series()
    x[10:15] = 500
    found, _ = check_tag("T", t, x, HealthConfig(), value_range=(0, 100))
    oor = [f for f in found if f["check"] == HealthCheck.out_of_range]
    assert len(oor) == 1 and oor[0]["n_samples"] == 5 and oor[0]["severity"] == Severity.critical


def test_duplicate_timestamps():
    t, x = series()
    t = np.insert(t, 50, t[50])
    x = np.insert(x, 50, x[50])
    found, summary = check_tag("T", t, x, HealthConfig())
    assert checks(found) == [HealthCheck.timestamp]
    assert summary.n_samples == 600
