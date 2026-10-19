# Homeostat Live

[Homeostat Live](https://homeostat.kausalflow.com/live/) publishes a simulated plant's sensor data every 10 minutes, its released lab results and its data dictionary. The first plant is `isidis-01`, a Sabatier methane plant whose lab reports methane purity.

## Download a week

```bash
uv run python scripts/fetch_homeostat_live.py --plant isidis-01 \
  --since 2026-10-02T17:00:00+00:00 --until 2026-10-09T17:00:00+00:00 \
  --out data/isidis-01
```

This writes four files into `data/isidis-01/`:

| File | What |
|---|---|
| `live.csv` | Sensor rows, a wide historian export with a `time` column, fetched in 7-day chunks because the API answers at most 1,100 rows at a time |
| `lab.csv` | Lab results: `tag, name, unit, sampled_at, reported_at, value` |
| `tags.csv` | The data dictionary |
| `graph.yaml` | A plant graph derived from the dictionary |

Files that are already there are reused, so a released week is downloaded once.

## The derived graph

`penguineer.io.homeostat_live.graph_from_tags` builds a graph from the public dictionary: a tag node per sensor with its unit, its role and its instrument range (parsed from text such as "gas flow transmitter, 0-20 Nm3/h"), a loop per control group that has an output, and a quality node per lab analysis. The flow topology is not in the public dictionary, so the graph has no edges yet.

## Check the data

```bash
uv run penguineer data health --data data/isidis-01/live.csv --graph data/isidis-01/graph.yaml
```

## In the tests

`tests/test_homeostat_live.py` runs on the plant's first public benchmark week, 2 to 9 October 2026. That week is released and frozen, so its data never changes. The tests are skipped without network access. CI sets `PENGUINEER_REQUIRE_HOMEOSTAT=1`, so there they fail instead of skipping. Downloads are cached in `PENGUINEER_HOMEOSTAT_CACHE`, by default `tests/.cache/homeostat/`.

## Investigate a real off-spec lot

`scripts/investigate_homeostat_live.py` runs `investigate` on isidis-01's own off-spec lots:

```bash
uv run python scripts/investigate_homeostat_live.py --out out/isidis-01
```

It downloads the last 35 days, then:

- builds the plant graph from the data dictionary plus the flow order in the [plant card](https://homeostat.kausalflow.com/live/isidis-01.md): electrolysers and CO₂ acquisition, feed gas, Sabatier reactor, receiver, condenser, dryer, product. The card publishes no volumes, so the edges carry no dead times.
- labels each lab sample as a lot (`S20261005T0300` is the sample taken at 03:00 UTC on 5 October) and gives it the grade the plant was making, read from the recipe tag `H2.rate`.
- marks a lot off spec when purity (`AT-201`) is below the grade's range or the dew point (`AT-202`) is above it. A dew point below the range is drier gas, not off spec.
- treats the recipe tags (`H2.rate`, `DRY.bypass`, `FFC-102.ratio`, `TIC-201.sp`) as regime tags, so a lot is compared only with lots of the same grade (earlier ones, or the ones after it for a grade's first run).
- groups off-spec lots into episodes, skips episodes within lab accuracy of the limit, and runs `investigate` on the first lot of the five worst, moving on to the episode's next lot when there is nothing to compare one with. It writes `summary.md`, `lots.csv` and a report per lot, and flags lots sampled within 12 hours of a grade change.

The *Homeostat Live* workflow runs the script in GitHub Actions (start it from the Actions tab, optionally with a lot) and uploads the data and the reports as an artifact.

### What it found on 19 October 2026

The run covered 14 September to 19 October; lab results were released up to 9 October. Of the off-spec lots, four episodes were more than lab error (about 1 °C on the dew point) and were investigated. The script passes `cycle_hours` = 24.66, a Martian sol, so power, feeds and the condenser are compared with their usual level at that time of sol.

| Episode | What happened | What `investigate` said |
|---|---|---|
| 15 Sep, surface fuel, purity 92.3 % | Sampled 0.5 h after the change from feedstock settled, the first surface fuel run in the data | High: the grade change (`DRY.bypass` 0.9 to 0.03, `H2.rate` 14 to 9, `TIC-201.sp` 435 to 395). No earlier surface fuel lot was in spec, so the reference is the in-spec lots after it |
| 23 Sep, propellant, dew point −48.5 °C | Sampled 0.2 h after the change from feedstock, the first propellant run in the data | High: the grade change, compared with the in-spec propellant lots after it |
| 26 Sep, propellant, dew point −71.5 °C | No grade change for 84 h | Low: no measured tag moved beyond its usual level for that time of sol, so something unmeasured at the dryer `D-401` |
| 5 Oct, propellant, purity 98.0 % | Sampled 0.3 h after the change from surface fuel | High: the grade change; the setpoints and the reactor temperature moved with it |

Before the day cycle was removed, 26 September put the condenser outlet `TT-301` first at medium, with solar power and both feeds close behind: the lot (17:00 to 20:00) was compared with lots from other times of sol. Compared with the same time of sol, none of them moved. The plant card says the dryer bed nears its capacity between regenerations, about every nine days, and its condition is not published, so a dryer near breakthrough fits, but it cannot be checked from public data. Before the regime fallback, the first lots of 15 and 23 September stopped with nothing to compare with.
