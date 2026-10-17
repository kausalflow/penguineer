# penguineer

Find the root cause of off-spec lots from historian and LIMS exports, on a laptop, offline.

penguineer reads files and writes files. Its engines are deterministic Python; an LLM is optional and never decides a ranking. Every command is defined once as a Pydantic parameter model, a result model and a function, and the CLI, the `--json` output and the Python API are generated from that one definition. [Alchemist](https://github.com/kausalflow/alchemist) imports penguineer as a library and adds the UI, runners and project management on top.

> Status: early skeleton. `init`, `data health`, `graph check` and `graph lags` work; `graph draft`, `investigate` and `bench homeostat` are registered with their parameters but not built yet (they exit with code 4).

## Documentation

The documentation is at <https://kausalflow.com/penguineer/>. Its source is in [`docs/`](docs/index.md) and builds with [Zensical](https://zensical.org):

```bash
uvx zensical serve   # preview on http://localhost:8000
```

## Install

```bash
uv tool install penguineer   # or: pip install penguineer; Python 3.11+
penguineer --help
```

From a checkout: `uv sync`, then `uv run penguineer --help`.

## Try it on the demo plant

```bash
uv run penguineer data health \
  --data examples/demo/historian.csv \
  --graph examples/demo/graph.yaml
```

```
data health: 5 tags, 3555 samples, 2026-08-03 00:00 to 2026-08-03 11:59 UTC
findings: 4 (1 critical, 3 warning, 0 info)

  critical PI-103.PV  out_of_range 2026-08-03 09:30  outside instrument range [0, 10]: min 10.918, max 11.114
  warning  AI-201.PV  spike        2026-08-03 08:15  spike of 2.5293 from 3.2329
  warning  FI-102.PV  gap          2026-08-03 05:59  no samples for 46 min (usual period 60 s)
  warning  TI-101.PV  flatline     2026-08-03 03:00  value frozen at 180.585 for 2.0 h
```

The demo data has exactly those four faults injected (see `scripts/make_demo_data.py`). The setpoint tag `FIC-102.SP` is constant all day and is not reported, because the graph says it is a loop setpoint.

Add `--json` to get the result model on stdout. Every run writes `runs/<run_id>/run.json` under `--project` (default: the current folder) with the params, the equivalent command line, versions and SHA-256 hashes of every input.

## Try it on a live simulated plant

[Homeostat Live](https://homeostat.kausalflow.com/live/) publishes a simulated plant's sensor data, lab results and data dictionary. This downloads a released week and derives a plant graph (tags, instrument ranges, control loops, lab qualities) from the dictionary:

```bash
uv run python scripts/fetch_homeostat_live.py --plant isidis-01 \
  --since 2026-10-02T17:00:00+00:00 --until 2026-10-09T17:00:00+00:00 --out data/isidis-01
uv run penguineer data health --data data/isidis-01/live.csv --graph data/isidis-01/graph.yaml
```

The tests in `tests/test_homeostat_live.py` run on that week. They are skipped without network access, and CI requires them.

## Commands

| Command | What it does | State |
|---|---|---|
| `penguineer init` | Create a project folder: `penguineer.yaml`, empty `graph.yaml`, folders | works |
| `penguineer data health --data FILE [--graph graph.yaml]` | Gaps, frozen tags, spikes, out-of-range values, duplicate and non-existent timestamps → `health.json` | works |
| `penguineer graph check --graph graph.yaml` | Schema and plant rules, plus the queue of inferred edges to confirm | works |
| `penguineer graph lags --data FILE --graph graph.yaml` | Estimate dead times along `flows_to` edges from the data (prewhitened cross-correlation) | works |
| `penguineer graph draft --tags tags.csv` | Draft the plant graph from a tag list and optional DEXPI P&ID | stub |
| `penguineer investigate --lot L --lims lims.csv --data FILE` | Align in material time, rank root-cause hypotheses, write the report | stub |
| `penguineer bench homeostat` | Score the pipeline on homeostat plants with injected faults | stub |

Exit codes: 0 success, 1 validation failed (for example graph errors), 2 usage error, 3 not enough data, 4 internal error or not implemented yet.

## Python API

```python
import penguineer

result = penguineer.run("data health", data="historian.csv", project="my-unit")
result.findings[0].detail

penguineer.schemas()  # JSON Schemas of every command's Params and Result
```

`penguineer.execute(...)` also returns the run record and run folder. Calls keep no global state: inputs and outputs live under the `project` folder passed in, so several processes can run side by side.

## Repository layout

```
packages/plantgraph/   plant context graph schema (depends only on Pydantic), shared with homeostat
src/penguineer/
  core/                command registry, RunContext, run records, errors
  cli/                 Typer app generated from the registry
  commands/            one module per command: Params, Result, run()
  io/                  historian, LIMS, graph and Homeostat Live readers
  engines/             deterministic engines: data health, dead times
  graph/               graph check
tests/                 unit, CLI, to_argv round-trip property tests, schema snapshot
examples/demo/         a small plant with injected data faults
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

A change to any command's Params or Result schema fails `tests/test_schemas.py` on purpose, because Alchemist builds on those schemas. After an intended change, regenerate the snapshot with `PENGUINEER_UPDATE_SCHEMAS=1 uv run pytest tests/test_schemas.py`.

## License

Apache-2.0
