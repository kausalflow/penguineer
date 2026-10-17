# CLI reference

<!-- Generated from the command registry by scripts/gen_cli_reference.py. -->

Every command also takes `--json` (print the result model as JSON on stdout), `--project PATH` (the folder run records and outputs go under, default `.`) and `--seed INT` (default `0`).

Exit codes: 0 success, 1 validation failed, 2 usage error, 3 not enough data, 4 internal error or not implemented yet.

## `penguineer bench homeostat`

Run investigate on homeostat plants with injected faults and score it.

| Option | Type | Default | Description |
|---|---|---|---|
| `--scenarios` | `train` \| `validation` \| `hidden` | `validation` | Which scenario split to run |
| `--bundles` | existing folder |  | Folder of exported homeostat scenario bundles |
| `--limit` | int |  | Run at most this many scenarios |

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

## `penguineer graph check`

Validate the plant graph and list inferred edges to confirm, most important first.

| Option | Type | Default | Description |
|---|---|---|---|
| `--graph` | existing file | required | Plant graph in the plantgraph schema |

## `penguineer graph draft`

Draft the plant graph from a tag list and an optional P&ID; every edge is inferred.

| Option | Type | Default | Description |
|---|---|---|---|
| `--tags` | existing file | required | Tag list (tag, description, unit) as CSV |
| `--pid` | existing file |  | Optional DEXPI P&ID |
| `--out` | path | `graph.yaml` | Where to write the drafted graph |
| `--llm` | `none` \| `local` \| `api` | `none` | LLM for relations rules cannot infer |

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
| `--timezone` | str | `UTC` | Time zone of naive timestamps in the exports |
| `--llm` | `none` \| `local` \| `api` | `none` | LLM for the prose summary only |
| `--out` | path |  | Report folder (default: reports/<run_id> in the project) |
