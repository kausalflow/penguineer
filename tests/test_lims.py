from datetime import UTC, datetime

import pytest

from penguineer.core import UsageError
from penguineer.io.lims import read_lims


def test_homeostat_lab_csv(tmp_path):
    path = tmp_path / "lab.csv"
    path.write_text(
        "tag,name,unit,sampled_at,reported_at,value\n"
        "AT-201,Purity,%,2026-10-02T18:00:00.000Z,2026-10-02T20:00:00.000Z,97.5\n"
        "AT-202,Dew point,degC,2026-10-02T18:00:00.000Z,2026-10-02T20:00:00.000Z,-41\n"
    )
    lims = read_lims(path)
    assert lims.frame.columns == [
        "property",
        "value",
        "sampled_at",
        "reported_at",
        "lot",
        "sample_point",
    ]
    assert lims.frame["property"].to_list() == ["AT-201", "AT-202"]
    assert lims.frame["sampled_at"][0] == datetime(2026, 10, 2, 18, tzinfo=UTC)
    assert lims.columns["lot"] is None


def test_plant_export_with_local_times(tmp_path):
    path = tmp_path / "lims.csv"
    path.write_text(
        "Batch,Sample_Location,Analysis,Result,Sample_Time\n"
        "L-1042,SP-1,purity,98.9,2026-08-03 10:00\n"
        "L-1042,SP-1,purity,n/a,2026-08-03 12:00\n"
    )
    lims = read_lims(path, timezone="Europe/Berlin")
    assert lims.frame.height == 1  # the non-numeric result is dropped
    row = lims.frame.row(0, named=True)
    assert row["lot"] == "L-1042" and row["sample_point"] == "SP-1"
    assert row["sampled_at"] == datetime(2026, 8, 3, 8, tzinfo=UTC)
    assert row["reported_at"] is None


def test_missing_columns(tmp_path):
    path = tmp_path / "lims.csv"
    path.write_text("lot,value\nL1,1\n")
    with pytest.raises(UsageError, match="property, sampled_at"):
        read_lims(path)
