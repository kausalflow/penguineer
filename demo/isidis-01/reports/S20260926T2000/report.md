# Lot S20260926T2000: AT-202 high

AT-202 was **-71.5 degC** (spec high -72), sampled at SP-product on 2026-09-26 20:00 UTC.

penguineer 0.4.0, run 20261024T104236Z-e470d4.

## Summary

The leading hypothesis, with low confidence: an unmeasured change at D-401.

- `D-401` (low): no measured tag upstream moved enough to explain AT-202; something unmeasured changed in D-401 or just before the sample (a catalyst, a fouled surface, a feed component nobody analyzes)
_Summary from the template._

## Hypotheses

| # | Cause | Kind | Score | Confidence | Summary |
|---|---|---|---|---|---|
| 1 | `D-401` | unmeasured disturbance | 0.20 | low | no measured tag upstream moved enough to explain AT-202; something unmeasured changed in D-401 or just before the sample (a catalyst, a fouled surface, a feed component nobody analyzes) |

### #1 D-401

Path: D-401 → S-product, dead time 0 min.


## Ruled out

- `TT-301`: did not move in the lot
- `TT-201`: did not move in the lot
- `TT-200`: did not move in the lot
- `FT-102`: did not move in the lot
- `FT-101`: did not move in the lot
- `JT-001`: did not move in the lot

## How this was worked out

- Lot window: 2026-09-26 17:00 to 2026-09-26 20:01 at S-product
- Quality onset: unknown
- Regime: DRY.bypass = 0, FFC-102.ratio = 0.25, FIC-101.mode = 2, FIC-102.mode = 2, H2.rate = 4, TIC-201.mode = 1, TIC-201.sp = 340
- Reference lots: S20260926T1100 (-76.5), S20260926T0800 (-78), S20260926T0500 (-80)
- Candidates tested: 6 tags on a 600 s grid
- Confidence: Confidence: high when the score is at least 0.4 and 0.025 ahead of the next hypothesis, medium from 0.25. Test weights: effect size 0.5, onset 0.3, lag correlation 0.3. Weights fitted on the homeostat train split (85 scenarios), thresholds chosen on the homeostat validation split (85 scenarios) (homeostat 0.1.0a3, bench plant 4144102c25e7).
- Note: some flows_to edges have no dead time; they count as 0 min
- Note: 31 earlier lots skipped: different regime
- Note: no online analyzer on the sampled stream: onset and correlation tests skipped
- Note: FFC-102.out, FIC-101.out, FIC-101.rsp, FT-101, FT-102, JT-001, TT-301 follow the day cycle (24.66 h): each is compared with its usual level at that time of day
