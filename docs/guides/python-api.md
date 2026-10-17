# Python API

The Python API mirrors the CLI one to one.

```python
import penguineer

result = penguineer.run(
    "data health",
    data="data/isidis-01/live.csv",
    graph="data/isidis-01/graph.yaml",
    project="my-unit",
)
for finding in result.findings:
    print(finding.severity, finding.tag, finding.detail)
```

`penguineer.run(name, **params)` validates the params, runs the command and returns its `Result` model. You can pass a `Params` object instead of keyword arguments. Invalid params raise `UsageError`. Other failures raise subclasses of `PenguineerError`, each carrying the CLI's `exit_code`.

`penguineer.execute(...)` takes the same arguments plus `progress` and `cancelled` callbacks and a `cache_dir`. It returns a `RunOutcome` with the `result`, the `record` (the run record) and the `run_dir`.

`penguineer.schemas()` returns the JSON Schema of every command's `Params` and `Result`, keyed by command name.

## For Alchemist

Alchemist lists penguineer as a dependency and imports it. A thin wrapper inside Alchemist turns commands into agent tools from `penguineer.schemas()`, passes staged Parquet snapshots as the file params, maps `progress` and `cancelled` to its step progress and steering, and copies `run.json` and the outputs into its run folder before sealing. penguineer never imports Alchemist.

The Params and Result schemas are frozen by a snapshot test (`tests/snapshots/schemas.json`), so a change that would break Alchemist shows up in review. penguineer follows semantic versioning from v0.1.
