# CLI reference

<!-- Generated from the command registry by scripts/gen_cli_reference.py. -->

Every command also takes `--json` (print the result model as JSON on stdout), `--project PATH` (the folder run records and outputs go under, default `.`) and `--seed INT` (default `0`).

Exit codes: 0 success, 1 validation failed, 2 usage error, 3 not enough data, 4 internal error or not implemented yet.

## `penguineer align`

Average each tag over the window that made each lot, shifted back by its dead time.

| Option | Type | Default | Description |
|---|---|---|---|
| `--data` | existing file | required | Historian export, CSV or Parquet, wide or long |
| `--lots` | existing file | required | Lot windows: lot, start and end columns, CSV or Parquet; other columns are kept in the output |
| `--tags` | str, repeatable |  | Tags to average (default: all) |
| `--dead-time-min` | float |  | One dead time for every tag, in minutes (with --graph: for tags the graph cannot place) |
| `--graph` | existing file |  | Plant graph: each tag's dead time to --sample-point along flows_to edges |
| `--sample-point` | str |  | Graph node where the lots are measured (with --graph) |
| `--timezone` | str | `UTC` | Time zone of naive timestamps in the exports |
| `--out` | path |  | Where to write aligned.parquet and aligned.csv (default: run folder) |

## `penguineer bench homeostat`

Run investigate on homeostat plants with injected faults and score it.

| Option | Type | Default | Description |
|---|---|---|---|
| `--scenarios` | `train` \| `validation` \| `hidden` | `validation` | Which scenario split to run |
| `--bundles` | existing folder |  | Folder of exported scenario bundles (default: generate them with Homeostat into .penguineer/bench/ in the project) |
| `--per-fault` | int | `12` | Plants per fault type to generate |
| `--faults` | str, repeatable |  | Fault types to generate (default: all) |
| `--split-seed` | int |  | Seed of the split (train and validation have fixed public seeds; the hidden split needs its secret one) |
| `--limit` | int |  | Run at most this many scenarios |
| `--out` | path |  | Folder for bench.json and scenarios.csv (default: the run folder) |

## `penguineer compare`

Compare a column between two groups: difference in means, Welch's t-test, Cohen's d.

| Option | Type | Default | Description |
|---|---|---|---|
| `--data` | existing file | required | A table, CSV or Parquet: for example align's aligned.parquet |
| `--value` | str | required | Column to compare |
| `--group` | str | required | Column that splits the rows into groups |
| `--a` | str | required | Group A: rows whose --group equals this |
| `--b` | str |  | Group B (default: every row not in group A) |
| `--where` | str, repeatable |  | Keep only rows where column=value; repeat to require several |

## `penguineer data health`

Flag frozen tags, gaps, spikes, out-of-range values and timestamp problems.

| Option | Type | Default | Description |
|---|---|---|---|
| `--data` | existing file | required | Historian export, CSV or Parquet, wide or long |
| `--graph` | existing file |  | Plant graph; instrument ranges and loop setpoints come from it |
| `--start` | datetime |  | Window start (inclusive) |
| `--end` | datetime |  | Window end (exclusive) |
| `--tags` | str, repeatable |  | Only check these tags |
| `--timezone` | str | `UTC` | Time zone of naive timestamps in the export |
| `--gap-factor` | float | `3.0` | Gap when an interval exceeds this × median |
| `--flatline-minutes` | float | `60.0` | Shortest frozen run to report |
| `--deadband` | float | `0.0` | Changes up to this count as frozen |
| `--spike-z` | float | `8.0` | Robust z-score on first differences |
| `--out` | path |  | Where to write health.json (default: run folder) |

## `penguineer fetch lake`

Read a window of a Parquet, CSV or Delta table on S3, GCS, Azure or disk into the project.

| Option | Type | Default | Description |
|---|---|---|---|
| `--source` | str | required | Table path or URL, globs allowed: s3://bucket/historian/**/*.parquet, gs://..., az://..., abfss://..., or a local path |
| `--kind` | `historian` \| `lims` \| `lots` | `historian` | What the table holds: historian, lims or lots |
| `--format` | `auto` \| `parquet` \| `csv` \| `delta` | `auto` | parquet, csv or delta (auto: by the file extension) |
| `--start` | datetime |  | Window start (inclusive) |
| `--end` | datetime |  | Window end (exclusive) |
| `--tags` | str, repeatable |  | Only these tags (historian) or properties (lims) |
| `--column` | str, repeatable |  | name=source_column for columns without a common name, such as timestamp=event_time or tag=point_id |
| `--storage-option` | str, repeatable |  | key=value object-store options such as aws_region=eu-west-1 or aws_endpoint_url=http://minio:9000; credentials come from the environment |
| `--hive` | bool | `False` | Read key=value folders as columns (Parquet) |
| `--timezone` | str | `UTC` | Time zone of naive timestamps |
| `--out` | path |  | Parquet file to write (default: data/lake/<kind>.parquet in the project) |

## `penguineer fetch pi`

Download a window of AVEVA PI data through the PI Web API, as penguineer's input files.

| Option | Type | Default | Description |
|---|---|---|---|
| `--server` | str | required | PI Web API base URL, such as https://pi.example.com/piwebapi |
| `--start` | datetime | required | Window start (inclusive) |
| `--end` | datetime | required | Window end (exclusive) |
| `--tags` | str, repeatable |  | PI tag names (on --data-server), full point paths \\server\tag, or AF attribute paths \\afserver\database\element\|attribute |
| `--tag-filter` | str |  | Also every point on --data-server matching this, such as U100.* |
| `--data-server` | str |  | PI Data Archive that tag names are on |
| `--mode` | `recorded` \| `interpolated` | `recorded` | Archived values, or values on a grid |
| `--interval` | str | `1m` | Grid step for --mode interpolated, such as 10s, 1m |
| `--event-frames` | str |  | AF database \\afserver\database whose event frames become lots.csv |
| `--event-frame-template` | str |  | Only event frames of this template, such as Batch |
| `--auth` | `none` \| `basic` \| `bearer` \| `kerberos` | `kerberos` | kerberos (your ticket), basic (password from PI_WEBAPI_PASSWORD), bearer (token from PI_WEBAPI_TOKEN) or none |
| `--username` | str |  | User for --auth basic (or PI_WEBAPI_USER) |
| `--ca-bundle` | existing file |  | CA certificates for a server with an in-house certificate |
| `--timeout` | float | `60.0` | Seconds to wait for each request |
| `--page-size` | int | `10000` | Values asked for per request |
| `--timezone` | str | `UTC` | Time zone of naive --start and --end |
| `--out` | path | `data/pi` | Folder for historian.parquet, tags.csv and lots.csv (relative: the project) |

## `penguineer graph check`

Validate the plant graph and list inferred edges to confirm, most important first.

| Option | Type | Default | Description |
|---|---|---|---|
| `--graph` | existing file | required | Plant graph in the plantgraph schema |

## `penguineer graph draft`

Draft the plant graph from a tag list and an optional P&ID; every edge is inferred.

| Option | Type | Default | Description |
|---|---|---|---|
| `--tags` | existing file | required | Tag list as CSV: tag, and optionally description, unit, equipment, range_low, range_high |
| `--pid` | existing file |  | DEXPI P&ID (Proteus XML): equipment, piping, where sensors and valves sit |
| `--out` | path | `graph.yaml` | Where to write the drafted graph (relative: the project) |
| `--unit` | str |  | Unit name (default: the project folder's name) |
| `--force` | bool | `False` | Overwrite an existing graph |
| `--llm` | `none` \| `local` \| `api` | `none` | LLM for relations rules cannot infer |
| `--llm-model` | str |  | Model for --llm (default: llm.model in penguineer.yaml) |
| `--llm-dry-run` | bool | `False` | Write what would be sent to the LLM, and send nothing |

## `penguineer graph lags`

Estimate dead times along flows_to edges from data; confirmed edges are never changed.

| Option | Type | Default | Description |
|---|---|---|---|
| `--data` | existing file | required | Historian export, CSV or Parquet, wide or long |
| `--graph` | existing file | required | Plant graph whose flows_to edges get dead times |
| `--max-lag-minutes` | float | `240.0` | Longest dead time to search |
| `--step-seconds` | float |  | Grid step (default: the slower tag's median period) |
| `--z-threshold` | float | `5.0` | Peak strength needed to keep a lag |
| `--timezone` | str | `UTC` | Time zone of naive timestamps in the export |
| `--in-place` | bool | `False` | Write the estimates back into --graph and log them in its history |

## `penguineer init`

Create a project folder for one unit: penguineer.yaml, an empty graph.yaml, folders.

| Option | Type | Default | Description |
|---|---|---|---|
| `--unit` | str |  | Unit name (default: the folder name) |
| `--timezone` | str | `UTC` | Time zone of naive timestamps in exports |
| `--force` | bool | `False` | Overwrite an existing penguineer.yaml |

## `penguineer investigate`

Align the data in time, rank root-cause hypotheses along graph paths, write the report.

| Option | Type | Default | Description |
|---|---|---|---|
| `--lot` | str | required | Lot ID as it appears in the LIMS export |
| `--lims` | existing file | required | LIMS export |
| `--data` | existing file | required | Historian export |
| `--graph` | existing file |  | Plant graph (default: graph.yaml in the project) |
| `--lots` | existing file |  | Lot windows: a CSV with lot, start, end (default: from one lab sample of the property to the next) |
| `--quality` | str |  | LIMS property to investigate (default: the lot's worst off-spec one) |
| `--top` | int | `5` | Hypotheses to report |
| `--hops` | int | `6` | flows_to hops to search upstream |
| `--reference-lots` | int | `3` | In-spec lots to compare with |
| `--step-seconds` | float |  | Grid step (default: the slowest candidate tag's period) |
| `--cycle-hours` | float | `24.0` | Length of the day cycle removed from tags that follow it, in hours (24.66 for a Mars sol) |
| `--no-cycle` | bool | `False` | Keep the day cycle in every tag |
| `--timezone` | str | `UTC` | Time zone of naive timestamps in the exports |
| `--llm` | `none` \| `local` \| `api` | `none` | LLM for the prose summary only |
| `--llm-model` | str |  | Model for --llm (default: llm.model in penguineer.yaml) |
| `--llm-dry-run` | bool | `False` | Write what would be sent to the LLM, and send nothing |
| `--out` | path |  | Report folder (default: reports/<run_id> in the project) |

## `penguineer lag`

Fit the actual delay from a cause to an effect: fixed, flow-driven, or with mixing.

| Option | Type | Default | Description |
|---|---|---|---|
| `--data` | existing file | required | Historian export, CSV or Parquet, wide or long |
| `--cause` | str | required | Upstream tag |
| `--effect` | str | required | Downstream tag, or with --lims a lab property |
| `--lims` | existing file |  | LIMS export: the effect is then a lab property, compared at its sample times |
| `--flow` | str, repeatable |  | Flow tags through the line, summed (enables the flow and volume models) |
| `--flow-unit` | str | `m3/h` | Unit of the flow tags, for the volumes |
| `--start` | datetime |  | Window start (inclusive) |
| `--end` | datetime |  | Window end (exclusive) |
| `--max-lag-minutes` | float | `240.0` | Longest delay to search |
| `--step-seconds` | float |  | Grid step (default: the slower tag's median period) |
| `--z-threshold` | float | `5.0` | Peak strength needed for an online effect, \|corr\|·√n |
| `--timezone` | str | `UTC` | Time zone of naive timestamps in the exports |
| `--out` | path |  | Folder for the plots (default: the run folder) |
