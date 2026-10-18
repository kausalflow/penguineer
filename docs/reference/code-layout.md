# Code layout

One repository holds a uv workspace with two Python packages.

```text
packages/plantgraph/      the plant graph schema (depends only on Pydantic)
src/penguineer/
  core/                   command registry, RunContext, run records, errors
  cli/                    the Typer app generated from the registry, the CLI reference renderer
  commands/               one module per command: Params, Result, run()
  io/                     historian, LIMS, plant graph, DEXPI P&ID, Homeostat Live and scenario-bundle readers
  engines/                deterministic engines: data health, dead times, alignment, tests, investigate,
                          and the fitted calibration (calibration.json)
  bench/                  the homeostat bench plant and its faults, bundle export, scoring
  llm/                    optional LLM providers, response cache, versioned prompts
  report/                 report models, grounded summary, evidence plots, HTML and Markdown templates
  graph/                  graph check and graph draft (tag list and P&ID)
tests/                    unit, CLI, round-trip property, schema snapshot and Homeostat Live tests
examples/demo/            a small plant with injected data faults
examples/offspec/         a small plant with an off-spec lot and a known root cause
scripts/                  demo data, Homeostat Live download, CLI reference generator, calibration fit
docs/                     this site
```
