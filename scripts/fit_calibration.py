"""Fit the ranking's weights on the homeostat training split and its thresholds on validation.

    uv run --extra bench python scripts/fit_calibration.py TRAIN_BUNDLES VALIDATION_BUNDLES

Both folders are made by ``penguineer bench homeostat`` (or ``penguineer.bench.export``).

1. Weights and rule scores: coordinate search over a small grid of each value in turn, keeping
   the others fixed, maximizing hit@3 (the release gate) plus half of hit@1 on the training split.
2. Thresholds, with those weights fixed, on the validation split: ``high`` and ``margin`` are the
   pair that labels the most correct leading hypotheses high while the wrong-confident rate
   stays at or under 5% (the release gate is 10%); ``medium`` is the lowest score above which the
   leading hypothesis is right at least 60% of the time, and never below 0.25.

Writes ``src/penguineer/engines/calibration.json``. The bench plant has no online analyzer on the
sampled stream, so the onset test never runs there: its weight is kept, not fitted.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

from penguineer.bench.score import score_investigation
from penguineer.commands.bench_homeostat import _plant_hash
from penguineer.core.errors import PenguineerError
from penguineer.engines.calibration import Calibration, FittedOn
from penguineer.engines.investigate import Settings, investigate
from penguineer.io.homeostat import bundles_in, read_bundle

OUT = Path(__file__).resolve().parents[1] / "src" / "penguineer" / "engines" / "calibration.json"

GRID = {
    "effect": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
    "lag": [0.1, 0.2, 0.3, 0.4, 0.5],
    "hidden": [0.2, 0.25, 0.3, 0.35, 0.4, 0.45],
    "hidden_below": [0.3, 0.4, 0.5, 0.6],
    "pv_bias": [0.5, 0.6, 0.8, 1.0],
    "loop_disturbance": [0.2, 0.3, 0.4, 0.5],
}


def run_all(bundles, cal: Calibration):
    settings = Settings(calibration=cal, top=10)
    out = []
    for b in bundles:
        try:
            inv = investigate(
                b.graph, b.historian, b.lims, b.truth.lot, lots=b.lots, settings=settings
            )
        except PenguineerError as e:
            out.append((score_investigation(None, b.truth, 0, str(e)), None))
            continue
        out.append((score_investigation(inv, b.truth, 0), inv))
    return out


def objective(results) -> float:
    n = len(results)
    h1 = sum(s.hit_at_1 for s, _ in results) / n
    h3 = sum(s.hit_at_3 for s, _ in results) / n
    return h3 + 0.5 * h1


def fit_weights(train, cal: Calibration) -> Calibration:
    best = objective(run_all(train, cal))
    print(f"start: {best:.4f}")
    for _ in range(3):
        changed = False
        for name, values in GRID.items():
            for v in values:
                trial = cal.model_copy(update={name: v})
                score = objective(run_all(train, trial))
                if score > best + 1e-9:
                    best, cal, changed = score, trial, True
                    print(f"  {name} = {v}: {best:.4f}")
        if not changed:
            break
    return cal


def fit_thresholds(validation, cal: Calibration) -> tuple[Calibration, float]:
    results = run_all(validation, cal)
    rows = []
    for s, inv in results:
        if inv is None or not inv.hypotheses:
            continue
        scores = [h.score for h in inv.hypotheses]
        rows.append((scores[0], scores[0] - (scores[1] if len(scores) > 1 else 0.0), s.hit_at_1))
    n = len(results)
    best = None
    for high in np.arange(0.4, 1.0001, 0.025):
        for margin in np.arange(0.0, 0.5001, 0.025):
            labelled = [ok for top, gap, ok in rows if top >= high and gap >= margin]
            wrong = sum(not ok for ok in labelled) / n
            right = sum(labelled)
            if wrong <= 0.05 and (best is None or right > best[0]):
                best = (right, float(high), float(margin), wrong)
    if best is None:
        best = (0, 1.0, 0.5, 0.0)
    _, high, margin, wrong = best
    medium = high
    for t in np.arange(0.25, high + 1e-9, 0.025):
        above = [ok for top, _, ok in rows if top >= t]
        if above and sum(above) / len(above) >= 0.6:
            medium = float(t)
            break
    fitted = cal.model_copy(
        update={"high": round(high, 3), "margin": round(margin, 3), "medium": round(medium, 3)}
    )
    return fitted, wrong


def load(folder: Path):
    paths = bundles_in(folder)
    print(f"{folder}: {len(paths)} scenarios")
    return [read_bundle(p) for p in paths]


def main() -> None:
    train_dir, validation_dir = Path(sys.argv[1]), Path(sys.argv[2])
    train, validation = load(train_dir), load(validation_dir)
    cal = fit_weights(train, Calibration())
    h1 = sum(s.hit_at_1 for s, _ in run_all(train, cal)) / len(train)
    cal, wrong = fit_thresholds(validation, cal)
    index = json.loads((train_dir / "index.json").read_text())
    cal.fitted_on = FittedOn(
        weights=f"the homeostat train split ({len(train)} scenarios)",
        thresholds=f"the homeostat validation split ({len(validation)} scenarios)",
        homeostat=index.get("homeostat"),
        plant=_plant_hash(),
        date=date.today().isoformat(),
        hit_at_1=round(h1, 4),
        wrong_confident_rate=round(wrong, 4),
    )
    OUT.write_text(cal.model_dump_json(indent=2) + "\n")
    print(cal.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
