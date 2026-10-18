# Investigating an off-spec lot

`investigate` takes one lot, finds the lab result that was out of spec, and ranks the process variables that could have caused it. Every hypothesis comes with its tests, its path through the plant graph, and plots. The engines are deterministic Python; an LLM can write the summary at the top of the report (see [LLM summaries](../guides/llm.md)), and never touches the ranking.

```bash
uv run penguineer investigate --lot L0803-A \
  --lims examples/offspec/lims.csv --data examples/offspec/historian.csv \
  --graph examples/offspec/graph.yaml --lots examples/offspec/lots.csv
```

```text
investigate: lot L0803-A, purity 98.2 wt% (spec >= 99), sampled 2026-08-03 07:59 at SP-1

  #1 TI-101.PV  high   score 0.97  process
     TI-101.PV (E-101) ran 5.74 degC low from 2026-08-03 05:29, 50 min upstream of the sample point; purity moved when it arrived.
  #2 PT-100.PV  medium score 0.46  process
     PT-100.PV (S-feed) ran 1.02 bar low from 2026-08-03 05:27, 52 min upstream of the sample point; purity moved 55 min after the expected arrival.
  #3 PI-103.PV  medium score 0.45  measurement_fault
     PI-103.PV has a flatline finding in the lot window: value frozen at 5.968 for 90 min

ruled out:
  FI-102.PV: held at setpoint by FIC-102 while its output moved 9.1 (+34.0 reference scales)
  TI-104.PV: its shift of -2.88 is 100% predicted from TI-101.PV (regression on the reference lots)
  PI-103.PV: did not move in the lot
```

The demo plant in `examples/offspec/` has a known answer: the feed preheater E-101 fouls, its outlet temperature TI-101 falls 6 °C, and the product purity follows 50 minutes later. A feed pressure dip at nearly the same time is a decoy, because the flow loop absorbs it. `scripts/make_offspec_demo.py` writes the data and documents every injected event.

[Open the example report](../examples/lot-report/index.html){ target="_blank" }: `report.html`, as `investigate` writes it.

## What it does

1. **Resolve.** Find the lot's results in the LIMS export, keep the properties that have a Quality node in the graph, and pick the one furthest out of spec (`--quality` picks one). Follow `reported_as` and `samples` edges to the node where the sample is taken. The lot window comes from `--lots` (a CSV of lot, start, end), or else runs from the previous lab sample of the same property to this one.
2. **Candidates.** Walk `flows_to` edges upstream from the sampled node (`--hops`, 6). Every measurement tag on those nodes is a candidate. Tags on the sampled node itself, such as an online analyzer, are symptoms, not causes: the first one is used to time the quality change.
3. **Regime, onset and reference.** Mode tags (`role: mode` in the graph, such as a grade) label the operating regime. The quality is assumed to have started moving in the lot before this one, or earlier when the lab results of the same regime step before the first off-spec lot (lots already affected but still in spec). The reference is the most recent in-spec lots of the same regime that end before that (`--reference-lots`, 3), so it is not made of material the fault had already reached. Without one, the command exits with code 3, because there is nothing to compare with.
4. **Align.** All series go on one grid (the slowest candidate's period, or `--step-seconds`) in material time at the sampled node: a tag with a cumulative dead time D along its path is read at t − D. Gaps longer than five sample periods stay empty rather than being interpolated across.
5. **Test.** Each candidate gets three tests (below), each with a strength from 0 to 1, and a score from them. Without an online analyzer on the sampled stream there is no quality onset to time against, so the effect is measured from the candidate's own step when it lands where the quality moved, and the correlation test uses earlier lab results instead of the analyzer.
6. **Loop check.** For a tag that is a loop's process value: if the setpoint moved, the setpoint change becomes its own hypothesis (an operator or recipe action). If the output saturated while the process value deviated, the hypothesis becomes lost control. If the process value held while the output moved, the loop absorbed a disturbance: the process value is ruled out and the output move is attached as evidence to the candidates upstream of the loop that moved and whose shift predicts at least 30% of the output's move (a regression on the reference lots: a tag the output never followed does not explain it). Held means the process value did not move by two reference scales, or that, once the loop had 20 minutes to settle after its output stepped, the median control error sits within the process value's sample noise of the reference's: a controlled value barely moves from lot to lot, so the short spike while the loop recovers would otherwise pass for a large move. When no upstream candidate explains the output, two hypotheses take their place: the process value reads wrong (a biased transmitter makes the loop hold the true value away from its setpoint), and something unmeasured disturbed the loop.
7. **Explain away.** When an upstream and a downstream candidate both moved, a regression on the reference lots predicts the downstream shift from the upstream one. If it explains at least 60% of it, the downstream tag is demoted and recorded as explained away.
8. **Hidden change.** When no measured tag moved clearly, the quality still moved: "something unmeasured changed just upstream of the sample" (a catalyst, a fouled surface, a feed component nobody analyzes) becomes a hypothesis at the nearest equipment.
9. **Data faults.** A candidate tag with a health finding (gap, flatline, spike, out of range) in its window also becomes a measurement-fault hypothesis, so a wrong measurement competes with the process causes.
10. **Rank and label.** Hypotheses are sorted by score. The leading one is labeled high confidence when its score and its lead over the next pass the calibrated thresholds; others are medium above a lower one. The weights and rule scores are fitted on the public homeostat training split and the thresholds on its validation split (see [The homeostat bench](bench.md)); `report.json` records the values used.

## The tests

| Test | What it asks | Strength |
|---|---|---|
| Effect size | Did the tag sit away from its reference level while the quality was off? Measured on a 30-minute moving mean, in units of the reference's spread or of the tag's lot-to-lot spread over earlier lots, whichever is larger, so a tag that wanders slowly does not count as moved | 0 below 2 scales, 1 from 6 |
| Onset | Does the tag's step, shifted by its dead time, land on the moment the quality moved? | 1 on time, falling off over the dead-time interval (at least 10 min); 0 when the tag moved after the quality |
| Lag correlation | Over the reference lots, does the shifted tag track the quality, with a sign that fits this deviation? | \|r\|, or 0 when the sign does not fit |

The score is their weighted mean (fitted weights; 0.5, 0.3 and 0.3 at present), halved when the tag did not move at all, and halved again when the tag has a data health finding in the window.

## The report

Each run writes `reports/<run_id>/` in the project (or `--out`):

| File | Contents |
|---|---|
| `report.json` | The `Report` model: the quality event, regime, reference lots, ranked hypotheses with their tests, ruled-out candidates, evidence items, health findings, notes and the summary |
| `report.html` | One self-contained page rendered from `report.json`, with images from `evidence/` |
| `report.md` | The same in Markdown |
| `llm-request.json` | With `--llm-dry-run` only: what would have been sent to the LLM |
| `evidence/` | `quality.png` (the quality over the period with the lot and reference windows) and one trend per hypothesis, shifted into material time |

The command's `--json` output carries the event, the hypotheses and the ruled-out list, so a caller does not need to read the files.

## Not yet

Change points on key flows as regime boundaries, flow-dependent dead times (`penguineer lag`) inside the ranking, the valve stiction test and PLS contribution analysis come later.
