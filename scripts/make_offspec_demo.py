"""Regenerate examples/offspec/: three days of a small plant with one off-spec lot.

The plant: feed (flow loop FIC-102 on valve FV-102, header pressure PT-100) through preheater
E-101 (outlet temperature TI-101) into reactor R-101 (temperature TI-104, pressure PI-103),
then the product stream (online purity analyzer AI-201, lab sample point SP-1). GRADE is the
product grade (1 or 2). Lots are eight hours long and sampled at their end.

Known answer, all times UTC:
- 2026-08-03 05:00-06:00  E-101 fouls: TI-101 falls 6 degC and stays down. TI-104 follows
  5 min later with half the drop, and purity follows TI-104 45 min later. Lot L0803-A
  (00:00-08:00) is off-spec on purity. Root cause: E-101 / TI-101.
- 2026-08-03 04:30-07:00  the feed header pressure PT-100 drops; FIC-102 holds the flow by
  opening FV-102 (FIC-102.OP rises). A decoy: it starts at nearly the same time but the loop
  absorbs it, so it does not reach the product.
- 2026-08-03 06:00-07:30  PI-103 is frozen (a data fault on a tag on the path).
- 2026-08-02 08:00-16:00  grade 2, with its own setpoints and purity; not a valid reference.

Run: uv run python scripts/make_offspec_demo.py
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

OUT = Path(__file__).resolve().parents[1] / "examples" / "offspec"
START = datetime(2026, 8, 1, tzinfo=UTC)
DAYS = 3
LOT_HOURS = 8


def minute_of(day: int, hour: float) -> int:
    return int((day - 1) * 1440 + hour * 60)


def smooth(x: np.ndarray, k: int) -> np.ndarray:
    """First-order lag with time constant k samples."""
    y = np.empty_like(x)
    y[0] = x[0]
    a = 1.0 / k
    for i in range(1, x.size):
        y[i] = y[i - 1] + a * (x[i] - y[i - 1])
    return y


def delay(x: np.ndarray, k: int) -> np.ndarray:
    return np.concatenate([np.full(k, x[0]), x[:-k]]) if k else x.copy()


def main() -> None:
    rng = np.random.default_rng(7)
    n = DAYS * 1440
    m = np.arange(n)
    grade = np.ones(n)
    grade[minute_of(2, 8) : minute_of(2, 16)] = 2

    # Feed: header pressure with a dip, flow loop holding the setpoint by opening the valve.
    pt = 5.0 + 0.03 * np.sin(m / 200) + rng.normal(0, 0.02, n)
    dip = (m >= minute_of(3, 4.5)) & (m < minute_of(3, 7))
    pt[dip] -= 1.0
    pt = smooth(pt, 5)
    sp = np.where(grade == 2, 10.0, 12.0)
    fi = sp + rng.normal(0, 0.08, n)
    op = 45 + 3.0 * (sp - 12) + 9.0 * (5.0 - pt) + rng.normal(0, 0.3, n)

    # Preheater outlet: grade setpoint, slow wander, fouling step on day 3.
    ti101 = np.where(grade == 2, 184.0, 180.0) + 0.4 * np.sin(m / 150)
    foul = np.clip((m - minute_of(3, 5)) / 60, 0, 1) * (m >= minute_of(3, 5))
    ti101 = smooth(ti101 - 6.0 * foul, 3) + rng.normal(0, 0.08, n)

    # Reactor: temperature follows the preheater 5 min later with half its swings.
    ti104 = 175.0 + 0.5 * (delay(ti101, 5) - 180.0) + rng.normal(0, 0.06, n)
    pi103 = 6.0 + 0.05 * np.sin(m / 300) + rng.normal(0, 0.03, n)
    frozen = (m >= minute_of(3, 6)) & (m < minute_of(3, 7.5))
    pi103[frozen] = pi103[minute_of(3, 6) - 1]

    # Product purity follows the reactor temperature 45 min later.
    base = np.where(grade == 2, 98.7, 99.6)
    purity = base + 0.5 * (delay(ti104, 45) - 175.0) + rng.normal(0, 0.02, n)
    ai201 = purity + rng.normal(0, 0.01, n)

    ts = [START + timedelta(minutes=int(i)) for i in m]
    historian = pl.DataFrame(
        {
            "timestamp": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts],
            "PT-100.PV": pt.round(3),
            "FI-102.PV": fi.round(3),
            "FIC-102.SP": sp,
            "FIC-102.OP": op.round(2),
            "TI-101.PV": ti101.round(3),
            "TI-104.PV": ti104.round(3),
            "PI-103.PV": pi103.round(3),
            "AI-201.PV": ai201.round(3),
            "GRADE": grade.astype(int),
        }
    )

    lots, lims = [], []
    for k in range(DAYS * 24 // LOT_HOURS):
        start = START + timedelta(hours=k * LOT_HOURS)
        end = start + timedelta(hours=LOT_HOURS)
        name = f"L{start:%m%d}-{'ABC'[(k * LOT_HOURS % 24) // LOT_HOURS]}"
        lots.append({"lot": name, "start": start, "end": end})
        i = min(int((end - START).total_seconds() // 60) - 1, n - 1)
        value = float(purity[i] + rng.normal(0, 0.02))
        lims.append(
            {
                "lot": name,
                "sample_point": "SP-1",
                "property": "purity",
                "value": round(value, 2),
                "sampled_at": end - timedelta(minutes=1),
                "reported_at": end + timedelta(hours=2),
            }
        )

    OUT.mkdir(parents=True, exist_ok=True)
    historian.write_csv(OUT / "historian.csv")
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    pl.DataFrame(lots).with_columns(
        pl.col("start").dt.strftime(fmt), pl.col("end").dt.strftime(fmt)
    ).write_csv(OUT / "lots.csv")
    pl.DataFrame(lims).with_columns(
        pl.col("sampled_at").dt.strftime(fmt), pl.col("reported_at").dt.strftime(fmt)
    ).write_csv(OUT / "lims.csv")
    print(f"wrote {OUT}: {historian.height} rows, {len(lots)} lots")


if __name__ == "__main__":
    main()
