# The homeostat bench

`penguineer bench homeostat` measures how often `investigate` names the right root cause. It generates plants with [Homeostat](https://homeostat.kausalflow.com/docs/), injects one known fault into each, exports what a plant would export (historian, LIMS, lots, the plant graph), runs `investigate` on the first off-spec lot exactly as it runs on a real plant, and only then reads the answer.

```bash
pip install "penguineer[bench]"
penguineer bench homeostat --scenarios validation --per-fault 12
```

```text
bench homeostat: 85 validation scenarios (11 plants skipped: no lot off spec)

  hit@1                 89%
  hit@3                 96%  (95% 90% to 99%)
  wrong-confident rate  5%
  data-fault catch rate 95%

  fault type              n  hit@1  hit@3
  catalyst_loss          12    92%   100%
  feed_b_flow            10    80%   100%
  feed_flow               7   100%   100%
  feed_property          12    83%   100%
  reactor_fouling        12    83%    83%
  reactor_sensor_bias    12    92%    92%
  reactor_sensor_drift    8   100%   100%
  reactor_setpoint       12    92%   100%
```

The first run generates the scenarios, which takes a few minutes per fault type while Homeostat compiles each plant; later runs reuse them from `.penguineer/bench/` in the project. `--bundles` runs on bundles you already have, and `bench.json` and `scenarios.csv` in the run folder hold every scenario's result.

## The bench plant

A small continuous reactor train, defined in `src/penguineer/bench/plant.py`:

| Node | What it is | Tags |
|---|---|---|
| S-A, S-B | Two feeds | FT-101, FT-102 (flowmeters) |
| M-101 | Feed mixer; its outlet composition sets the reactor's feed ratio | AT-101 (online analyzer) |
| S-mix, E-201 | The mixed feed and its heater, loop TIC-201 | TT-200 (inlet temperature), TT-201, TIC-201 setpoint and output |
| R-301 | The reactor, loop TIC-301; the product property follows an Arrhenius law in its temperature, a power of the feed ratio and the catalyst activity | TT-301, TIC-301 setpoint and output |
| S-product, SP-1 | The product and its sample point | lab result every 4 hours, reported an hour later |

Each drawn plant differs in its volumes, its kinetics and its noise, and runs for three days: two grades alternate in the first day, and the fault arrives between hours 40 and 56. The graph each bundle carries is the true one, with dead times from the drawn volumes. Spec limits come from the in-control lots before the fault. A plant where no lot goes off spec is skipped: the fault was too small to matter, so it is neither a hit nor a miss.

## The faults

| Fault type | What happens | Counts as found when a hypothesis names |
|---|---|---|
| `feed_property` | Feed B's composition steps up | AT-101, M-101 or S-B |
| `feed_flow` | Feed A's flow rises | FT-101 or S-A |
| `feed_b_flow` | Feed B's flow drops | FT-102 or S-B |
| `reactor_setpoint` | The reactor setpoint is lowered | the TIC-301 setpoint change |
| `reactor_fouling` | The reactor's heating fouls until its valve saturates | TIC-301 lost control (or R-301) |
| `reactor_sensor_bias` | TT-301 reads high; the loop cools the true temperature to match | a measurement fault on TT-301 |
| `reactor_sensor_drift` | TT-301 drifts high | a measurement fault on TT-301 |
| `catalyst_loss` | The catalyst loses activity; nothing measured moves | an unmeasured change in R-301 |

A process hypothesis claims that its tag moved and caused the deviation, so it counts only when the tag itself is listed: a tag that merely sits on the faulty equipment does not. The other kinds (a setpoint change, lost control, a measurement fault, an unmeasured change) are claims about their node.

## Metrics

| Metric | Definition | Release gate |
|---|---|---|
| hit@3 | Share of scenarios where one of the top three hypotheses names the cause | ≥ 70% on the hidden split |
| hit@1 | The same for the leading hypothesis | reported |
| Wrong-confident rate | Share of scenarios where the leading hypothesis is labeled high confidence and is wrong | < 10% |
| Data-fault catch rate | Among measurement faults, share where the faulty tag leads as a measurement fault | ≥ 90% |

Rates carry a 95% Wilson interval in `bench.json`: at 85 scenarios an observed 96% is anything from 90% to 99%.

## Splits and calibration

| Split | Seed | Used for |
|---|---|---|
| train | public (1000) | Fitting the test weights and rule scores |
| validation | public (2000) | Choosing the confidence thresholds, and the numbers above |
| hidden | KausalFlow's secret | The release gate, run only on KausalFlow's runner |

`scripts/fit_calibration.py` searches the weights and rule scores on the train split (maximizing hit@3, plus half of hit@1 and half of the data-fault catch rate), then chooses the confidence thresholds on validation so that the wrong-confident rate stays at or under 5%. The result ships as `src/penguineer/engines/calibration.json`, and every `report.json` names the splits and the Homeostat version it was fitted with.

## What the numbers do not say

- The bench is one plant family. A real plant has more tags, more loops and other faults, so treat these numbers as an upper bound, not a promise.
- The data-fault catch rate is 95% (19 of 20) on validation and 100% (21 of 21) on train, but 20 scenarios give a wide interval: 76% to 99%. The one miss is a small bias that started seven hours before the first off-spec lot, inside the newest reference lot (the lots in between ran another grade), so the loop output's move is partly in the reference and the leading hypothesis is an unmeasured change in R-301.
- The plant has no analyzer on the sampled stream, so the onset test never runs on the bench, and its weight is kept rather than fitted.
- The hidden split, and so the release gate, is not run in this repository.

## In CI

The Bench workflow runs a smoke bench (3 validation plants per fault type) on every pull request, and the full train and validation bench every night. Each run uploads `bench.json` and `scenarios.csv` and writes the scores into the job summary.
