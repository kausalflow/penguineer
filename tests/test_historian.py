from datetime import UTC, datetime

import polars as pl
import pytest

from penguineer.core import UsageError
from penguineer.io.historian import read_historian


def test_wide_csv(demo_csv):
    h = read_historian(demo_csv)
    assert h.layout == "wide"
    assert h.frame.columns == ["timestamp", "tag", "value"]
    assert h.frame["timestamp"].dtype == pl.Datetime("us", "UTC")
    assert h.frame["tag"].n_unique() == 5


def test_long_parquet_with_window(tmp_path):
    df = pl.DataFrame(
        {
            "Time": ["2026-01-01 00:00", "2026-01-01 00:01", "2026-01-01 00:02"] * 2,
            "Tag": ["A"] * 3 + ["B"] * 3,
            "Value": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    path = tmp_path / "h.parquet"
    df.write_parquet(path)
    h = read_historian(path, start=datetime(2026, 1, 1, 0, 1, tzinfo=UTC), tags=["B"])
    assert h.layout == "long"
    assert h.frame["value"].to_list() == [5.0, 6.0]


def test_local_time_with_dst_gap(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text(
        "timestamp,A\n2026-03-29 01:30:00,1\n2026-03-29 02:30:00,2\n2026-03-29 03:30:00,3\n"
    )
    h = read_historian(path, timezone="Europe/Berlin")
    assert h.nonexistent_local_times == 1
    assert h.frame["timestamp"].to_list()[0] == datetime(2026, 3, 29, 0, 30, tzinfo=UTC)


def test_missing_timestamp_column(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text("when,A\n1,2\n")
    with pytest.raises(UsageError, match="timestamp"):
        read_historian(path)
