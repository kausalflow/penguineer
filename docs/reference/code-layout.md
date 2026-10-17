# Code layout

One repository holds a uv workspace with two Python packages.

```text
packages/plantgraph/      the plant graph schema (depends only on Pydantic)
src/penguineer/
  core/                   command registry, RunContext, run records, errors
  cli/                    the Typer app generated from the registry, the CLI reference renderer
  commands/               one module per command: Params, Result, run()
  io/                     historian, LIMS, plant graph and Homeostat Live readers
  engines/                deterministic engines: data health, dead times, alignment, tests, investigate
  report/                 report models, evidence plots, HTML and Markdown templates
  graph/                  graph check
tests/                    unit, CLI, round-trip property, schema snapshot and Homeostat Live tests
examples/demo/            a small plant with injected data faults
examples/offspec/         a small plant with an off-spec lot and a known root cause
scripts/                  demo data, Homeostat Live download, CLI reference generator
docs/                     this site
```
