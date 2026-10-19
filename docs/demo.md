# Live demo

**[Open the live demo](https://kausalflow.com/penguineer/demo/isidis-01/)**

Every Saturday, the *Demo* workflow runs `investigate` on the newest off-spec lots of [Homeostat Live](https://homeostat.kausalflow.com/live/) `isidis-01`, a simulated Sabatier methane plant that publishes its sensor data, lab results and data dictionary. It publishes what it found, unedited:

- a summary of the last 35 days: lab samples per grade, grade changes, off-spec episodes and, for the worst five, the ranked causes `investigate` gives;
- the full report of every investigated lot, with its evidence plots;
- `lots.csv`, every lab sample with the grade the plant was making.

The page says which commit produced it. Nobody picks the lots or edits the answers, so it shows the engine as it is, including the cases where it stops or is unsure. [Homeostat Live](guides/homeostat-live.md) explains how lots, grades and the plant graph are derived, and what the run on 18 October 2026 found.

## Run it yourself

The demo uses only public data, so the same run works on a laptop:

```bash
uv run python scripts/investigate_homeostat_live.py --out out
uv run --with markdown==3.7 python scripts/build_demo_site.py out demo-site
```

`demo-site/index.html` is the page. To refresh the published demo before Saturday, start the *Demo* workflow from the Actions tab on `main`.
