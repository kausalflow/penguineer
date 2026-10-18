# Changelog

penguineer and plantgraph are released together, with one version.

## 0.4.0

The investigation agent moves into penguineer, so any host can run an investigation with a model
and get the same method, review and findings.

### Added

- `penguineer.agent`: the tool loop (budgets, transcript, pause and resume, notes from people
  following the run), a tool-calling adapter for any provider through Pydantic AI, the planning
  and analysis method as prompt blocks, the protocol's tools (`submit_plan`, `record_finding`,
  `finish_analysis`, `build_report`), the independent review, findings with `{{Fn}}`
  placeholders, keyword search over plant documents, and penguineer's commands as tool
  definitions. See the [agent guide](https://kausalflow.com/penguineer/guides/agent/).
- The `agent` extra (`pip install "penguineer[agent]"`) installs `pydantic-ai-slim` for the
  provider adapter. The rest of `penguineer.agent` needs no extra.

The commands and their outputs are unchanged.

## 0.3.0

Lots in material time and group comparisons, so a caller such as an agent no longer needs its own
statistics for them.

### Added

- `align`: averages each tag over the window that made each lot, shifted back by the tag's dead
  time, taken from the plant graph (`--graph` with `--sample-point`) or given (`--dead-time-min`).
  Writes `aligned.parquet` and `aligned.csv`, one row per lot, with the lot file's other columns
  kept.
- `compare`: the difference between two groups of rows, with Welch's t-test and Cohen's d;
  `--where column=value` filters first.

### Changed

- Lot files can be Parquet, and the lot, start and end columns may also be spelled `lot_id`,
  `batch`, `start_ts`, `end_ts` and similar.
- Importing penguineer no longer switches matplotlib to the Agg backend, so notebooks keep their
  own. Plots are drawn on `Figure` objects instead of through `pyplot`.

`investigate` is unchanged; the [Results](https://kausalflow.com/penguineer/results/) of 0.2.0
still hold.

## 0.2.0

The first release that investigates: from an off-spec lot to ranked, evidenced root causes.

### Added

- `investigate`: aligns the historian and the lab in material time, finds the off-spec lot's
  deviation, follows the plant graph upstream, tests each candidate (lagged change, regime,
  loop behaviour, sensor faults) and writes `report.html`, `report.md` and `report.json`.
- `bench homeostat`: runs `investigate` on Homeostat plants with injected faults and scores it
  (hit@1, hit@3, wrong-confident rate, data-fault catch rate, with Wilson intervals). The test
  weights and confidence thresholds are fitted on the public splits and ship as
  `calibration.json`. A smoke bench runs on every pull request, the full public bench nightly.
- The release gate: the *Release gate* workflow runs the hidden split by hand and checks it with
  `python -m penguineer.bench.gate`.
  On the hidden split (171 scenarios): hit@3 93%, wrong-confident 5%, data-fault catch 95%;
  see [Results](https://kausalflow.com/penguineer/results/).
- `lag`: fits the actual delay from a cause to an effect, as a fixed dead time, plug flow through
  a volume, or dead time plus mixing, against the chance level of a rotated cause.
- `mcp`: serves every command as a tool over stdio (`pip install "penguineer[mcp]"`).
- `graph draft`: drafts `graph.yaml` from a tag list (ISA-5.1 names: tags, loops, valves,
  equipment), a DEXPI P&ID (`--pid`: equipment, piping, where sensors and valves sit) and,
  optionally, an LLM's proposals for what they leave open (`--llm`). Every drafted edge is
  `inferred`, for the engineer to confirm in `graph check`'s queue.
- Optional LLM (`--llm local` or `--llm api`, `pip install "penguineer[llm-api]"`): a grounded
  summary at the top of the investigate report, and graph relations. What the LLM returns is
  checked before it is used; `--llm-dry-run` shows the request and sends nothing. There is no
  default model.

### Changed

- Measurement faults that a control loop holds (a biased or drifting PV) are caught: 95% on the
  validation split, up from 75%.

## 0.1.0

The skeleton: command registry and generated CLI, run records, `init`, `data health`,
`graph check`, `graph lags`, the `plantgraph` schema package, the Homeostat Live reader, docs
and the release workflow. `investigate`, `bench homeostat` and `graph draft` were stubs.
