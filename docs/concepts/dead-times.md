# Dead times

Material takes time to travel through a plant. A change in the feed shows up in the reactor minutes later and in the product sample hours later. penguineer keeps these delays on the `flows_to` edges of the plant graph (`dead_time_min`, with `dead_time_low_min` and `dead_time_high_min` as its interval), and `investigate` will use them to line up each cause with the quality it affects.

## Estimating them from data

```bash
uv run penguineer graph lags --data historian.csv --graph graph.yaml
```

For every `flows_to` edge whose two ends are each measured by a tag (through a `measures` edge), `graph lags` estimates how long the downstream signal lags the upstream one:

1. Both tags are resampled to one grid, by default the slower tag's median sample period (`--step-seconds` overrides it).
2. An AR model is fitted to the upstream signal, with its order chosen by AIC, and both series are filtered with it. This is prewhitening: process signals are so autocorrelated that a raw cross-correlation has a broad hump instead of a peak.
3. The cross-correlation of the filtered series is computed for every lag up to `--max-lag-minutes` (240). The dead time is the lag with the largest absolute correlation. A negative correlation means a reverse-acting link.
4. The interval is the run of lags around the peak whose correlation is within the noise band (2/√n) of it.
5. The estimate is kept only when the peak is strong enough: |corr|·√n at least `--z-threshold` (5).

| Edge status | Meaning |
|---|---|
| `estimated` | A clear lag was found and written to the edge as `inferred`, with source `data` and the correlation as its confidence |
| `no_clear_lag` | Both ends are measured but no lag stands out of the noise |
| `kept_confirmed` | An engineer confirmed this edge, so it is never overwritten |
| `no_tags` | One end has no measuring tag |
| `no_data` | The tags are not in the export, or overlap too little |

## Where the result goes

By default the updated graph is written to the run folder (`runs/<run_id>/graph.yaml`), and the input graph is not touched. With `--in-place`, the estimates are written back into `--graph`, and each change is appended to `graph.history.jsonl` next to it, with the time, the run ID, the reason and the edge before and after.

## Limits

The estimate is a transport delay between two measured points, from the data as it is. Feedback control, recycles and mixing all smear a delay. Mixing in a vessel spreads a step out over its residence time, so the peak marks where the response is strongest, not where it starts. Material-time alignment, which accounts for flow-dependent delays and mixing, comes with `investigate`.
