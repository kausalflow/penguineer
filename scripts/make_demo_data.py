"""Regenerate examples/demo/historian.csv: 12 h of 1-minute data with known data faults.

Faults injected (all times UTC, on 2026-08-03):
- TI-101.PV  frozen at its last value from 03:00 to 05:00 (flatline)
- FI-102.PV  no samples from 06:00 to 06:45 (gap)
- AI-201.PV  one-sample spike at 08:15 (spike)
- PI-103.PV  above its 0-10 bar range from 09:30 to 09:40 (out of range)
- FIC-102.SP constant all day (a setpoint, so it must not be reported as frozen)

Run: uv run python scripts/make_demo_data.py
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

OUT = Path(__file__).resolve().parents[1] / "examples" / "demo" / "historian.csv"


def main() -> None:
    rng = np.random.default_rng(42)
    n = 12 * 60
    start = datetime(2026, 8, 3, tzinfo=UTC)
    ts = [start + timedelta(minutes=i) for i in range(n)]
    minute = np.arange(n)

    ti = 180 + 0.5 * np.sin(minute / 90) + rng.normal(0, 0.1, n)
    ti[180:301] = ti[180]
    fi = 12 + rng.normal(0, 0.2, n)
    sp = np.full(n, 12.0)
    ai = 3.2 + 0.05 * np.sin(minute / 60) + rng.normal(0, 0.01, n)
    ai[495] += 2.5
    pi = 6 + rng.normal(0, 0.1, n)
    pi[570:581] = 11 + rng.normal(0, 0.1, 11)

    df = pl.DataFrame(
        {
            "timestamp": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts],
            "TI-101.PV": ti.round(3),
            "FI-102.PV": fi.round(3),
            "FIC-102.SP": sp,
            "AI-201.PV": ai.round(4),
            "PI-103.PV": pi.round(3),
        }
    )
    gap = (minute >= 360) & (minute < 405)
    df = df.with_columns(
        pl.when(pl.Series(gap)).then(None).otherwise(pl.col("FI-102.PV")).alias("FI-102.PV")
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.write_csv(OUT)
    print(f"wrote {OUT} ({df.height} rows)")


if __name__ == "__main__":
    main()
