# Data health

`penguineer data health` says whether the data can be trusted before anyone blames the process. Each check writes findings: intervals on one tag with a severity (`info`, `warning` or `critical`), a plain-language detail and the numbers behind it.

| Check | Method | Severity |
|---|---|---|
| `gap` | An interval between samples longer than `--gap-factor` (3) times the tag's median period | warning; critical beyond 60 times |
| `flatline` | Consecutive values that change by at most `--deadband` for at least `--flatline-minutes` (60) | warning; critical beyond 24 h |
| `out_of_range` | Values outside the instrument range of the tag node in the graph | critical |
| `spike` | A robust z-score on first differences above `--spike-z` (8), where the signal returns to its level within a few samples. A level shift is a process change, not a spike, and is not reported | warning |
| `timestamp` | Duplicate timestamps, irregular sampling, and local times that do not exist (the daylight-saving change) | warning or info |

Setpoints, modes and settings (from the tag's `role`, or a loop's `sp` and `mode`) are never reported as frozen. Without `--graph`, the out-of-range check is skipped and the result says so.

Checks that compare an online analyzer with lab results (drifting bias, calibration steps, clock offset) need LIMS input and come later. The result lists them under `checks_not_run`.

## Output

`health.json` holds the result without its own path, so the same inputs and options give a byte-identical file. It is written to the run folder unless `--out` says otherwise. It also has a per-tag summary: sample count, first and last time, median period, coverage (the share of expected samples present), the number of findings and the worst severity.

Health findings are evidence in their own right. In `investigate`, a faulty tag on a causal path will become a hypothesis of the kind "the measurement is wrong", competing with process causes.
