# Getting started

penguineer needs Python 3.11 or newer. It is on [PyPI](https://pypi.org/project/penguineer/):

```bash
uv tool install penguineer   # or: pip install penguineer
penguineer --help
```

The examples on this page use the demo plant in the repository, so clone it and run penguineer from the checkout with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/kausalflow/penguineer
cd penguineer
uv sync
uv run penguineer --help
```

## Check the demo plant's data

`examples/demo/` holds twelve hours of one-minute data from a small reactor, with four data faults injected on purpose (see `scripts/make_demo_data.py`), and its plant graph.

```bash
uv run penguineer data health \
  --data examples/demo/historian.csv \
  --graph examples/demo/graph.yaml
```

```text
data health: 5 tags, 3555 samples, 2026-08-03 00:00 to 2026-08-03 11:59 UTC
findings: 4 (1 critical, 3 warning, 0 info)

  critical PI-103.PV  out_of_range 2026-08-03 09:30  outside instrument range [0, 10]: min 10.918, max 11.114
  warning  AI-201.PV  spike        2026-08-03 08:15  spike of 2.5293 from 3.2329
  warning  FI-102.PV  gap          2026-08-03 05:59  no samples for 46 min (usual period 60 s)
  warning  TI-101.PV  flatline     2026-08-03 03:00  value frozen at 180.585 for 2.0 h
```

The setpoint `FIC-102.SP` is constant all day and is not reported, because the graph says it is a setpoint.

Each run writes `runs/<run_id>/run.json` and `health.json` under the project folder (`--project`, default the current folder). Add `--json` to print the result model instead of the summary.

## Start a project for your own unit

```bash
uv run penguineer init --project my-unit
```

This creates `penguineer.yaml`, an empty `graph.yaml`, and the folders `data/`, `runs/`, `reports/` and `.penguineer/cache/`. Put your historian exports in `data/` and describe the plant in `graph.yaml` (see [The plant graph](concepts/plant-graph.md)).

Next, try a real simulated plant: [Homeostat Live](guides/homeostat-live.md).
