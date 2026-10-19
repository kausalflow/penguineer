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
- treats the recipe tags (`H2.rate`, `DRY.bypass`, `FFC-102.ratio`, `TIC-201.sp`) as regime tags, so a lot is compared only with earlier lots of the same grade.
- groups off-spec lots into episodes, skips episodes within lab accuracy of the limit, and runs `investigate` on the first lot of the five worst, moving on to the episode's next lot when there is nothing to compare one with. It writes `summary.md`, `lots.csv` and a report per lot, and flags lots sampled within 12 hours of a grade change.

The *Homeostat Live* workflow runs the script in GitHub Actions (start it from the Actions tab, optionally with a lot) and uploads the data and the reports as an artifact. The *Demo* workflow runs it every Saturday and publishes the result as the [live demo](../demo.md).

### What it found on 18 October 2026

The run covered 13 September to 18 October; lab results were released up to 9 October. Of 206 lab samples, 19 were off spec, in seven episodes. Three of them were within lab accuracy of the dew point limit (about 1 °C) and were not investigated.

| Episode | What happened | What `investigate` said |
|---|---|---|
| 15 Sep, surface fuel, purity 92.3 % | Lots right after a change from feedstock | Stopped: no earlier surface fuel lot in the window to compare with |
| 23 Sep, propellant, purity 94.5 % | Lots right after a change from surface fuel, the first propellant run in the data | Stopped: no earlier propellant lot to compare with |
| 26 Sep, propellant, dew point −70.5 °C | No grade change for 84 h | Medium confidence: the condenser outlet (`TT-301`) ran 10.6 °C warm, with solar power and both feeds high: the plant was at its daytime peak |
| 5 Oct, propellant, purity 98.0 % | Sampled 30 min after the change from surface fuel ended | High confidence: the reactor setpoint and both feed setpoints changed in the lot, so the grade change; `TT-201`, `FT-101` and `FT-102` only followed them |

The grade change on 5 October is the right answer. On 26 September the warmer condenser is a physically sound cause (it lets more water reach the dryer), but power, feeds and condenser temperature all rise together over the Martian day, so `investigate` cannot tell them apart, and its medium label says as much. The dryer's state is not published, so it cannot be checked.
