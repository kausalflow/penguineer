# Plant data sources

Every penguineer command reads local files. The `fetch` commands fill those files from where a plant keeps its data: `fetch pi` from an AVEVA PI server, and `fetch lake` from Parquet, CSV or Delta tables on S3, Google Cloud Storage, Azure or a shared disk. They are the only commands that use the network, and only when you run them.

A fetch writes a snapshot of one window. The investigation then runs on that snapshot, so it gives the same answer next month, after the historian has compressed or the lake has been rewritten.

```text
PI Web API ─┐                 ┌─ historian.parquet → data health, lag, investigate
            ├─ fetch pi/lake ─┼─ tags.csv ─────────→ graph draft
S3, GCS ────┘                 ├─ lots.csv ─────────→ align, investigate
                              └─ lims.parquet ─────→ investigate, lag
```

## AVEVA PI

`fetch pi` reads the PI Web API, the REST interface of an AVEVA PI System (formerly OSIsoft PI). It sends only `GET` requests, so it changes nothing on the server.

```bash
penguineer fetch pi --server https://pi.example.com/piwebapi --data-server PISRV01 \
  --tags FIC101.PV --tags TI102.PV --tags AI201.PV \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z
```

It writes three files into `data/pi/` (change it with `--out`):

| File | What |
|---|---|
| `historian.parquet` | A long table: `timestamp` (UTC), `tag`, `value`, `quality` |
| `tags.csv` | `tag, description, unit, range_low, range_high, path`, from each point's descriptor, engineering units, zero and span. `graph draft --tags` reads it as is |
| `lots.csv` | `lot, start, end, template` from AF event frames, with `--event-frames` |

On the small PI Web API the tests run against (`tests/fake_piwebapi.py`), four hours of three tags with event frames gives:

```text
fetch pi: 3 tags, 326 values, 2026-10-01 00:00 to 2026-10-01 04:00 UTC
quality: 324 good, 1 I/O Timeout, 1 questionable
lots: 2 event frames
historian: data/pi/historian.parquet
tags: data/pi/tags.csv
lots: data/pi/lots.csv
```

```text
$ penguineer graph draft --tags data/pi/tags.csv
graph draft: 3 tags, 1 loops, 1 valves, 0 equipment; 1 edges (0 flows_to), all inferred
```

### Which tags

- `--tags FIC101.PV` names a point on `--data-server`.
- `--tags '\\PISRV01\FIC101.PV'` is a full point path, for points on other data servers.
- `--tags '\\AFSRV\Plant\Reactor R-101|Feed Flow'` is a PI AF attribute: the value the attribute points at, under the attribute's path.
- `--tag-filter 'U100.*'` adds every point on `--data-server` whose name matches, with PI's `*` and `?` wildcards.

Tags the server does not know are listed under "not found" and the rest are fetched.

### Recorded or interpolated

By default it reads every archived value (`--mode recorded`), paged so that no request asks for more than `--page-size` values. `--mode interpolated --interval 1m` reads a regular grid instead, which is smaller for fast tags over long windows. penguineer's engines resample on their own, so recorded values are the better default: they keep the real sample times that `data health` checks.

### Quality

A value PI marks bad, or a system digital state such as `I/O Timeout`, `Shutdown` or `Bad Input`, is kept with a null value and the state's name as its quality. Every command then sees a gap there, not a number. Questionable and substituted values keep their number, and `quality` says which they were. Digital tags such as valve states read as their state's index (`Closed` 0, `Open` 1).

### Lots from event frames

If the plant records batches or campaigns as AF event frames, they become lot windows:

```bash
penguineer fetch pi ... --event-frames '\\AFSRV\Plant' --event-frame-template Batch
```

Event frames that are still open have no end yet, so they are left out and counted in a note.

### Signing in

Credentials are never parameters, because parameters are written to every run's `run.json`.

| `--auth` | How |
|---|---|
| `kerberos` (default) | Your Windows or Kerberos sign-in, the PI Web API's own default. Needs `pip install "penguineer[pi-kerberos]"`; on Linux also the system Kerberos libraries (`libkrb5-dev`) and a ticket from `kinit` |
| `basic` | `--username` (or `PI_WEBAPI_USER`), and the password from `PI_WEBAPI_PASSWORD` |
| `bearer` | A token from `PI_WEBAPI_TOKEN`, for servers behind OpenID Connect |
| `none` | Anonymous, for test servers |

A server with an in-house certificate needs its CA certificates: `--ca-bundle corp-ca.pem`. Certificate checks cannot be turned off.

## Data lakes and object stores

`fetch lake` reads one table, a Parquet, CSV or Delta table, from a path or URL. Globs read many files as one table.

```bash
penguineer fetch lake --source 's3://plant-data/historian/**/*.parquet' --hive \
  --column timestamp=event_time --column tag=point_id \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z \
  --storage-option aws_region=eu-west-1
```

On a local S3 (the tests use moto) holding three days of two tags, partitioned by day:

```text
fetch lake: 288 rows of historian (long), 2 tags
window: 2026-10-03 00:00 to 2026-10-03 23:50 UTC
columns: timestamp=event_time, tag=point_id, value=val
table: data/lake/historian.parquet
```

| Source | Example |
|---|---|
| Amazon S3, MinIO, Ceph | `s3://bucket/path/**/*.parquet` |
| Google Cloud Storage | `gs://bucket/path/*.parquet` |
| Azure Blob Storage, ADLS Gen2 | `az://container/path/*.parquet`, `abfss://container@account.dfs.core.windows.net/path` |
| Hugging Face, HTTP | `hf://datasets/...`, `https://.../file.parquet` |
| A local or mounted disk | `/mnt/lake/historian/*.parquet` |

### What the table holds

`--kind` says which of penguineer's inputs the table is, and so which columns it needs:

| `--kind` | Columns | Window |
|---|---|---|
| `historian` (default) | Long: `timestamp`, `tag`, `value` and optionally `quality`. Or wide: `timestamp` and one column per tag | Rows with `start ≤ timestamp < end`; `--tags` keeps those tags (long) or columns (wide) |
| `lims` | `property`, `value`, `sampled_at`, and optionally `reported_at`, `lot`, `sample_point` | Samples with `start ≤ sampled_at < end`; `--tags` keeps those properties |
| `lots` | `lot`, `start`, `end`; other columns, such as grade, are kept | Lots overlapping the window |

Columns are found by their common spellings, in any case: `time`, `ts` and `datetime` for the timestamp, `point` or `tag_name` for the tag, `sample_time` for `sampled_at`, `batch_id` for the lot, and so on. Name the rest with `--column name=source_column`.

The written file is read back with the same reader every command uses, so a table that will not work fails here, with the reason, and not halfway through an investigation.

### Only the window is read

The time and tag filters are pushed down to the scan. On Parquet, row groups whose statistics fall outside the window are skipped without being downloaded, so on a table written in time order a month out of a year costs about a month of transfer. For a lake partitioned by folder (`day=2026-09-01/`), `--hive` reads the folder names as columns.

Naive timestamps in the table are read in `--timezone`. A time column stored as text cannot be filtered at the source: store times as timestamps, or leave out `--start` and `--end`.

### Credentials

Like `fetch pi`, `fetch lake` takes no credentials as parameters and refuses storage options that look like one. They come from where the cloud tools already look:

- S3: `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, or the instance role.
- Google Cloud Storage: `GOOGLE_APPLICATION_CREDENTIALS`, or the workload identity.
- Azure: `AZURE_STORAGE_ACCOUNT_NAME` with `AZURE_STORAGE_ACCOUNT_KEY`, or a managed identity.

`--storage-option` carries the rest, such as `aws_region=eu-west-1`, or `aws_endpoint_url=http://minio:9000` with `aws_allow_http=true` for a local MinIO.

### Delta tables

`--format delta` reads a Delta Lake table at its current version. It needs `pip install "penguineer[delta]"`.

## From Python and Alchemist

The commands are thin wrappers over `penguineer.connectors`, which returns Polars frames and writes nothing:

```python
from datetime import UTC, datetime

from penguineer.connectors import lake, pi

client = pi.PIClient("https://pi.example.com/piwebapi", pi.PIAuth.kerberos)
got = pi.fetch(
    client,
    datetime(2026, 9, 1, tzinfo=UTC),
    datetime(2026, 10, 1, tzinfo=UTC),
    tags=["FIC101.PV", "TI102.PV"],
    data_server="PISRV01",
)
got.frame  # timestamp, tag, value, quality
got.tags  # tag, description, unit, range_low, range_high, path

lab = lake.fetch("s3://plant-data/lims/*.parquet", lake.TableKind.lims)
lab.frame  # property, value, sampled_at, ...
```

[Alchemist](https://github.com/kausalflow/alchemist) keeps its own data catalog and stages sealed Parquet snapshots. It can call `penguineer.run("fetch pi", ...)` to stage a window into a run's project folder, or call the library functions and write the frames where its catalog wants them.
