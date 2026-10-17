# Commands and run records

## One definition per command

A command is four things registered under a name: a Pydantic `Params` model, a Pydantic `Result` model, a `run(params, ctx)` function and its docstring as help text.

```python
class DataHealthParams(CommandParams):
    data: FilePath = Field(description="Historian export, CSV or Parquet, wide or long")
    graph: FilePath | None = Field(None, description="Plant graph")
    spike_z: float = Field(8.0, gt=0, description="Robust z-score on first differences")


@command("data health", params=DataHealthParams, result=DataHealthResult)
def data_health(p: DataHealthParams, ctx: RunContext) -> DataHealthResult:
    """Flag frozen tags, gaps, spikes, out-of-range values and timestamp problems."""
```

From that one definition penguineer generates:

- the Typer command `penguineer data health`, with one option per field, its type, default and help, and file options that must exist;
- the `--json` output, which is the `Result` model;
- the Python call `penguineer.run("data health", data=...)`, which returns the same `Result`;
- the JSON Schemas in `penguineer.schemas()`, which Alchemist builds its agent tools and forms from.

Pydantic validates the parsed values, so the CLI and the Python API report the same errors. A group name such as `data` in `data health` comes from the registered name.

## The equivalent command line

`to_argv(command, params)` turns validated params into the canonical command line, leaving out defaults. A property test generates random params and checks that the CLI parses `to_argv(params)` back to the same params. This is what makes "the command this run equals" in a run record true.

## Run context

`RunContext` carries the project folder, the run ID, a seeded random generator, the LLM mode, the cache folder, a progress callback and a cancellation flag. Engines read only this context and never global state. All outputs of a call go under its project folder, so several penguineer processes can run side by side on different folders. Alchemist relies on this to run one call per runner.

## Run records

Every call, from the CLI or Python, writes `runs/<run_id>/run.json`, also when it fails. See [Run record](../reference/run-record.md) for its fields.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Validation failed, for example errors in `graph check` |
| 2 | Usage error: bad options or missing files |
| 3 | Not enough data to answer |
| 4 | Internal error, or the command is not implemented yet |
