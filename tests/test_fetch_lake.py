"""`fetch lake` on local Parquet, CSV and Delta tables, and on S3 (a local moto server)."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

import penguineer
import penguineer.commands  # noqa: F401
from penguineer.connectors import lake
from penguineer.connectors.lake import TableKind
from penguineer.core import UsageError
from penguineer.io.historian import read_historian

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def long_table(days: int = 3) -> pl.DataFrame:
    times = [T0 + timedelta(minutes=10 * i) for i in range(days * 144)]
    return pl.DataFrame(
        {
            "event_time": times * 2,
            "point_id": ["FIC101"] * len(times) + ["TI102"] * len(times),
            "val": [float(i % 9) for i in range(len(times))] * 2,
            "plant": "P1",
        }
    ).with_columns(pl.col("event_time").dt.date().cast(pl.String).alias("day"))


@pytest.fixture
def partitioned(tmp_path):
    """A hive-partitioned Parquet dataset: day=YYYY-MM-DD/part.parquet."""
    root = tmp_path / "lake" / "historian"
    df = long_table()
    for (day,), part in df.group_by(["day"]):
        folder = root / f"day={day}"
        folder.mkdir(parents=True)
        part.drop("day").write_parquet(folder / "part.parquet")
    return root


def test_window_tags_and_column_mapping(partitioned, tmp_path):
    r = penguineer.run(
        "fetch lake",
        {
            "source": f"{partitioned}/**/*.parquet",
            "hive": True,
            "start": T0 + timedelta(days=1),
            "end": T0 + timedelta(days=2),
            "tags": ["TI102"],
            "column": ["timestamp=event_time", "tag=point_id"],
        },
        project=tmp_path,
    )
    assert r.layout == "long"
    assert r.columns["value"] == "val"  # found by its common spelling
    assert r.n_rows == 144 and r.n_tags == 1
    assert r.window_start == T0 + timedelta(days=1)
    hist = read_historian(tmp_path / "data/lake/historian.parquet")
    assert hist.frame["tag"].unique().to_list() == ["TI102"]


def test_wide_csv_in_local_time(tmp_path):
    src = tmp_path / "wide.csv"
    src.write_text(
        "time,FIC101,TI102\n" + "".join(f"2026-10-01 0{h}:00:00,{h},{h * 10}\n" for h in range(6))
    )
    got = lake.fetch(
        str(src),
        start=datetime(2026, 10, 1, 2),
        end=datetime(2026, 10, 1, 4),
        tags=["TI102"],
        timezone="Europe/Berlin",
    )
    assert got.layout == "wide"
    assert got.frame.columns == ["timestamp", "TI102"]
    assert got.frame["TI102"].to_list() == [20, 30]


def test_lims_and_lots_tables(tmp_path):
    lims = tmp_path / "lab.parquet"
    pl.DataFrame(
        {
            "analysis": ["purity", "purity", "water"],
            "result_value": [99.1, 97.0, 0.2],
            "sample_time": [T0, T0 + timedelta(days=2), T0],
            "batch_id": ["L1", "L3", "L1"],
        }
    ).write_parquet(lims)
    got = lake.fetch(str(lims), TableKind.lims, end=T0 + timedelta(days=1), tags=["purity"])
    assert got.frame.columns == ["property", "value", "sampled_at", "lot"]
    assert got.frame["value"].to_list() == [99.1]

    lots = tmp_path / "lots.parquet"
    pl.DataFrame(
        {
            "batch": ["L1", "L2", "L3"],
            "start_time": [T0, T0 + timedelta(days=1), T0 + timedelta(days=2)],
            "end_time": [T0 + timedelta(days=1), T0 + timedelta(days=2), T0 + timedelta(days=3)],
            "grade": ["A", "A", "B"],
        }
    ).write_parquet(lots)
    got = lake.fetch(
        str(lots), TableKind.lots, start=T0 + timedelta(hours=12), end=T0 + timedelta(days=1)
    )
    # Lots overlapping the window, with their other columns kept.
    assert got.frame["lot"].to_list() == ["L1"]
    assert got.frame.columns == ["lot", "start", "end", "grade"]


def test_delta_table(tmp_path):
    pytest.importorskip("deltalake")
    table = tmp_path / "delta"
    long_table(1).rename(
        {"event_time": "timestamp", "point_id": "tag", "val": "value"}
    ).write_delta(table)
    got = lake.fetch(str(table), fmt=lake.TableFormat.delta, tags=["FIC101"])
    assert got.frame.height == 144


def test_credentials_are_refused_as_parameters():
    with pytest.raises(UsageError, match="looks like a credential"):
        lake.parse_storage_options(["aws_secret_access_key=abc"])
    with pytest.raises(UsageError, match="looks like a credential"):
        lake.parse_storage_options(["azure_storage_account_key=abc"])
    assert lake.parse_storage_options(["AWS_REGION=eu-west-1"]) == {"aws_region": "eu-west-1"}


def test_helpful_errors(partitioned, tmp_path):
    with pytest.raises(UsageError, match="unknown name"):
        lake.fetch(f"{partitioned}/**/*.parquet", columns={"stamp": "event_time"})
    with pytest.raises(UsageError, match="no column nope"):
        lake.fetch(f"{partitioned}/**/*.parquet", columns={"timestamp": "nope"})
    with pytest.raises(UsageError, match="cannot"):
        lake.fetch(str(tmp_path / "missing.parquet"))


@pytest.fixture
def s3(monkeypatch):
    """A moto S3 server on localhost with one bucket, and test credentials in the environment."""
    moto_server = pytest.importorskip("moto.server")
    boto3 = pytest.importorskip("boto3")
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=endpoint).create_bucket(Bucket="plant")
    yield endpoint, boto3.client("s3", endpoint_url=endpoint)
    server.stop()


def test_s3_parquet_glob(s3, tmp_path):
    endpoint, client = s3
    df = long_table()
    for (day,), part in df.group_by(["day"]):
        path = tmp_path / f"{day}.parquet"
        part.drop("day").write_parquet(path)
        client.upload_file(str(path), "plant", f"historian/day={day}/part.parquet")
    r = penguineer.run(
        "fetch lake",
        {
            "source": "s3://plant/historian/**/*.parquet",
            "start": T0 + timedelta(days=2),
            "column": ["timestamp=event_time", "tag=point_id"],
            "storage_option": [
                f"aws_endpoint_url={endpoint}",
                "aws_allow_http=true",
                "aws_region=us-east-1",
            ],
        },
        project=tmp_path,
    )
    assert r.n_rows == 2 * 144 and r.n_tags == 2
