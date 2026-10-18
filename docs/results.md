# Results on Homeostat

How well penguineer 0.2.0 does on plants whose truth is known. Every number on this page comes
from [Homeostat](https://homeostat.kausalflow.com/docs/) plants: simulated, with the cause of
each off-spec lot injected and recorded, so that the answer can be checked. Real plants are
messier, so treat these numbers as an upper bound for your plant, not a promise.

## Root causes: the bench

[`bench homeostat`](concepts/bench.md) builds plants of one kind (two feeds, a mixer, a heater, a
reactor with a temperature loop and a lab analysis of the product), injects one of eight faults
into each, and runs `investigate` on the first off-spec lot exactly as on a real plant. Three
splits use different seeds:

- **train**: the test weights and rule scores in `calibration.json` were fitted on it;
- **validation**: the confidence thresholds were chosen on it, and every change was checked
  against it;
- **hidden**: its seed is a repository secret. It was run for this release, by the *Release
  gate* workflow, which prints only the scores below.

| Split | Scenarios | hit@1 | hit@3 | Wrong-confident | Data-fault catch |
|---|---|---|---|---|---|
| validation | 85 | 89% | 96% | 5% | 95% (19 of 20) |
| hidden | 171 | 87% | 93% (88% to 96%) | 5% (3% to 10%) | 95% (85% to 99%, n=44) |
| **gate** | ≥ 150 | | ≥ 70% | < 10% | ≥ 90% |

The hidden split was generated from 200 plants (25 per fault type); 29 never made an off-spec
lot and were skipped. A first run on 160 plants scored the same within a point (hit@3 93%,
wrong-confident 5%, data-fault catch 94%) but had only 136 scenarios, under the gate's 150, so
the default grew to 200. The intervals are 95% Wilson intervals. The hidden scores sit a few
points under validation's, as expected for a split nothing was tuned on.

On validation, by fault type:

| Fault | n | hit@1 | hit@3 |
|---|---|---|---|
| catalyst_loss | 12 | 92% | 100% |
| feed_b_flow | 10 | 80% | 100% |
| feed_flow | 7 | 100% | 100% |
| feed_property | 12 | 83% | 100% |
| reactor_fouling | 12 | 83% | 83% |
| reactor_sensor_bias | 12 | 92% | 92% |
| reactor_sensor_drift | 8 | 100% | 100% |
| reactor_setpoint | 12 | 92% | 100% |

Fouling is the hardest: it builds slowly, so by the first off-spec lot it looks much like a
change in the reactor's own behaviour. The one data fault missed on validation is a small sensor
bias that started inside the newest reference lot; see [Limits](concepts/bench.md#what-the-numbers-do-not-say).

## Delays: `lag`

[`lag`](concepts/lag.md#checked-on-homeostat-plants) was checked on six bench plants whose
volumes and back-mixing are known, fitting feed B's flow against the lab's product property. With
an hourly lab result over 20 days, the dead-plus-mixing model finds the mean delay within 1.7
minutes on average (the fixed dead time is off by 3.5 minutes, and by 14 on the plant with the most
mixing). With a result every 4 hours over 60 days, within 2.1 minutes.

## What these numbers do not say

- All plants are of one kind. A distillation column, a batch reactor or a plant with recycle has
  not been benchmarked.
- One fault per plant. Two causes at once are not in the bench.
- The plant graph is the true one. With a drafted graph (`graph draft`) an edge that is missing
  hides its cause; the confirm queue in `graph check` is there for that.
- The LLM is not scored: the bench runs without it, and the LLM never changes the ranking.
