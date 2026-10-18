import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import penguineer
from penguineer.core import UsageError
from penguineer.engines.compare import t_two_sided, welch
from penguineer.engines.windows import TagDeadTime, window_means
from penguineer.io.lims import read_lots

OFFSPEC = Path(__file__).resolve().parents[1] / "examples" / "offspec"
T0 = datetime(2026, 8, 1, tzinfo=UTC)


# ------------------------------------------------------------ compare


@pytest.mark.parametrize(
    ("t", "df", "p"),
    [  # two-sided p-values from scipy.stats.t.sf
        (0.1, 1.0, 0.936548965138893),
        (50.0, 2.5, 8.130495084958722e-05),
        (3.0, 1000.0, 0.0027667090442381925),
        (2.0, 10.0, 0.07338803477074),
    ],
)
def test_t_distribution(t, df, p):
    assert t_two_sided(t, df) == pytest.approx(p, rel=1e-9)


def test_welch():
    c = welch(np.array([1.0, 2.0, 3.0, 4.0]), np.array([2.0, 4.0, 6.0, np.nan]))
    assert (c.n_a, c.n_b) == (4, 3) and c.diff == pytest.approx(-1.5)
    assert c.df == pytest.approx(3.2347, rel=1e-3)  # Welch-Satterthwaite
    assert 0.2 < c.p_value < 0.4 and c.cohens_d == pytest.approx(-1.5 / np.sqrt((5 / 3 + 4) / 2))
    one = welch(np.array([1.0]), np.array([2.0, 3.0]))
    assert one.p_value is None and one.cohens_d is None
    with pytest.raises(ValueError):
        welch(np.array([]), np.array([1.0]))


def test_compare_command(tmp_path):
    table = tmp_path / "t.parquet"
    pl.DataFrame(
        {
            "resin": ["R1", "R1", "R1", "R2", "R2", "R2", "R2"],
            "period": ["scope"] * 6 + ["baseline"],
            "yield": [90.0, 91.0, 89.5, 95.0, 96.0, 95.5, 50.0],
        }
    ).write_parquet(table)
    r = penguineer.run(
        "compare",
        data=table,
        value="yield",
        group="resin",
        a="R1",
        where=["period=scope"],
        project=tmp_path,
    )
    assert (r.n_a, r.n_b) == (3, 3) and r.diff == pytest.approx(90.1667 - 95.5, abs=1e-3)
    assert r.p_value < 0.01 and "Welch p" in r.summary()
    with pytest.raises(UsageError, match="column=value"):
        penguineer.run(
            "compare",
            data=table,
            value="yield",
            group="resin",
            a="R1",
            where=["nope=1"],
            project=tmp_path,
        )


# ------------------------------------------------------------ align


def test_lots_from_parquet_keep_other_columns(tmp_path):
    f = tmp_path / "lots.parquet"
    pl.DataFrame(
        {
            "lot_id": ["A"],
            "start_ts": [datetime(2026, 8, 1)],
            "end_ts": [datetime(2026, 8, 2)],
            "resin": ["R1"],
        }
    ).write_parquet(f)
    lots = read_lots(f, keep=True)
    assert lots.columns == ["lot", "start", "end", "resin"]
    assert lots["start"][0] == T0


def test_window_means_shift_by_dead_time():
    minutes = np.arange(0, 240)
    hist = pl.DataFrame(
        {
            "timestamp": [T0 + timedelta(minutes=int(m)) for m in minutes],
            "tag": "TI",
            "value": np.where(minutes < 60, 1.0, 2.0),
        }
    )
    lots = pl.DataFrame(
        {
            "lot": ["L1", "L2"],
            "start": [T0 + timedelta(minutes=60), T0 + timedelta(minutes=120)],
            "end": [T0 + timedelta(minutes=120), T0 + timedelta(minutes=180)],
        }
    )
    # with a 60-minute dead time, L1 was made from the material that passed in minutes 0-59
    out = window_means(hist, lots, {"TI": TagDeadTime("TI", 60.0, "fixed", [])})
    assert out["TI"].to_list() == [1.0, 2.0] and out["TI_n"].to_list() == [60, 60]
    late = window_means(hist, lots, {"TI": TagDeadTime("TI", 300.0, "fixed", [])})
    assert late["TI"].to_list() == [None, None] and late["TI_n"].to_list() == [0, 0]


def test_align_with_graph_dead_times(tmp_path):
    r = penguineer.run(
        "align",
        data=OFFSPEC / "historian.csv",
        lots=OFFSPEC / "lots.csv",
        graph=OFFSPEC / "graph.yaml",
        sample_point="SP-1",
        project=tmp_path,
    )
    dead = {d.tag: d for d in r.dead_times}
    assert dead["TI-101.PV"].dead_time_min == 50 and dead["TI-101.PV"].source == "graph"
    assert dead["TI-101.PV"].path == ["E-101", "R-101", "S-product"]
    assert "GRADE" not in dead and any("GRADE" in n for n in r.notes)
    rows = {row["lot"]: row for row in r.rows}
    # the preheater ran cold from 3 August, the days the lab found off spec
    before = [v["TI-101.PV"] for k, v in rows.items() if not k.startswith("L0803")]
    assert max(rows["L0803-B"]["TI-101.PV"], rows["L0803-C"]["TI-101.PV"]) < min(before) - 4
    assert pl.read_parquet(r.table).height == r.n_lots == 9


def test_align_needs_a_dead_time(tmp_path):
    with pytest.raises(UsageError, match="dead-time-min"):
        penguineer.run(
            "align", data=OFFSPEC / "historian.csv", lots=OFFSPEC / "lots.csv", project=tmp_path
        )


def test_import_leaves_the_matplotlib_backend_alone():
    """Callers such as notebooks keep their own backend."""
    code = (
        "import matplotlib; matplotlib.use('svg'); import penguineer, penguineer.report.plots, "
        "penguineer.report.lag_plots; print(matplotlib.get_backend())"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "svg"
