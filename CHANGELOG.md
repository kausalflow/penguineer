# Changelog

penguineer and plantgraph are released together, with one version.

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
