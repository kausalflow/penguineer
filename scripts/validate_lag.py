"""Check ``penguineer lag`` against Homeostat plants whose volumes are known.

    uv run --extra bench python scripts/validate_lag.py [--plants 6] [--days 20] [--lab-hours 1]

The plant is the bench plant (``penguineer.bench.plant``) with no grade change and no fault, its
feed flows swinging more and faster, so that the throughput (and with it the delay) changes, and
the reactor's back-mixing time drawn per plant. Two pairs are fitted on each plant:

* ``FT-102 -> product_property`` (lab results, sampled every ``--lab-hours``), with the flow
  FT-101 + FT-102. Feed B's flow changes the feed ratio at the mixer at once; the change then
  crosses the mixer's outlet pipe and the reactor in plug flow, and is spread by the reactor's
  back-mixing. True dead volume: mixer pipe + reactor volume; true mixed volume:
  tau_mix · F_nom (a first order in the throughput).
* ``FT-102 -> AT-101`` (online), with the same flow. True dead volume: the mixer pipe; the
  analyzer adds its own 5-minute reporting delay, a 1-minute lag and a 10-minute sample hold,
  about 11 minutes at any flow, so only the mean delay is compared.

Prints one row per plant and fit and the mean absolute errors; ``--json`` writes them.
"""

from __future__ import annotations

import argparse
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from penguineer.bench.plant import BASE
from penguineer.engines.lagfit import fit_lag

MIN_US = 60_000_000
F_NOM = 20.0  # the reactor's nominal flow in BASE, m3/h
ANALYZER_MIN = 11.0  # online analyzer: 5 min to report, 1 min lag, about 5 min of hold


def config(days: int, lab_hours: float, plants: int, seed: int) -> dict:
    cfg = copy.deepcopy(BASE)
    cfg["seed"] = seed
    cfg["duration"] = f"{days}d"
    cfg["plan"] = {"start": "grade_A", "production": [], "maintenance": []}
    for source in cfg["exogenous"]:
        if source["target"] in ("MX-101.flow_a", "MX-101.flow_b"):
            source["source"]["std"] = {"uniform": [1.5, 2.5]}
            source["source"]["tau"] = "1h"
    reactor = next(u for u in cfg["units"] if u["id"] == "TIC-301")
    reactor["tau_mix"] = {"uniform": [300, 1200]}
    reactor["lab_period"] = int(lab_hours * 3600)
    cfg["family"] = {"plants": plants}
    return cfg


def lab_samples(obs, period_s: int, delay_s: int = 3600) -> tuple[np.ndarray, np.ndarray]:
    """The lab results at their sample times (the observed tag holds each from its report)."""
    values = obs["AT-301"].to_numpy(dtype=float)
    times, out = [], []
    for k in range(1, values.size * 60 // period_s):
        sampled = k * period_s
        at = (sampled + delay_s) // 60
        if at >= values.size:
            break
        if np.isfinite(values[at]):
            times.append(sampled // 60 * MIN_US)
            out.append(values[at])
    return np.asarray(times, dtype=np.int64), np.asarray(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plants", type=int, default=6)
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--lab-hours", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=3000)
    ap.add_argument("--json", type=Path)
    ap.add_argument(
        "--example", type=Path, help="Write one plant's historian and LIMS exports to this folder"
    )
    ap.add_argument("--example-plant", type=int, default=2)
    args = ap.parse_args()

    import homeostat

    fam = homeostat.family(
        config(args.days, args.lab_hours, args.plants, args.seed), plants=args.plants
    )
    rows = []
    for k in range(args.plants):
        run = fam.run(k)
        units = {u["id"]: u for u in run.meta["scenario"]["units"]}
        v_mixer, v_reactor = units["MX-101"]["volume"], units["TIC-301"]["volume"]
        tau_mix = units["TIC-301"]["tau_mix"]
        obs = run.observed
        t = np.arange(len(obs), dtype=np.int64) * MIN_US
        series = {c: obs[c].to_numpy(dtype=float) for c in ("FT-101", "FT-102", "AT-101")}
        ok = {c: np.isfinite(v) for c, v in series.items()}
        flow_ok = ok["FT-101"] & ok["FT-102"]
        flow = (t[flow_ok], series["FT-101"][flow_ok] + series["FT-102"][flow_ok])
        cause = t[ok["FT-102"]], series["FT-102"][ok["FT-102"]]
        mean_flow = float(np.mean(flow[1]))

        lab_t, lab_x = lab_samples(obs, int(args.lab_hours * 3600))
        lab = fit_lag(*cause, lab_t, lab_x, lab=True, flow=flow, max_lag_s=150 * 60)
        truth_dead = v_mixer + v_reactor
        truth_mix = tau_mix * F_NOM / 3600
        row = {
            "plant": k,
            "mean_flow": round(mean_flow, 2),
            "lab_samples": int(lab_t.size),
            "true_dead_m3": round(truth_dead, 3),
            "true_mixing_m3": round(truth_mix, 3),
            "true_delay_min": round((truth_dead + truth_mix) / mean_flow * 60, 1),
        }
        if lab is not None:
            row["lab_best"] = lab.best
            for f in lab.fits:
                row[f"lab_{f.model}"] = f.model_dump()

        online = fit_lag(
            *cause, t[ok["AT-101"]], series["AT-101"][ok["AT-101"]], flow=flow, max_lag_s=60 * 60
        )
        row["online_true_delay_min"] = round(v_mixer / mean_flow * 60 + ANALYZER_MIN, 1)
        row["online_true_dead_m3"] = round(v_mixer, 3)
        if online is not None:
            row["online_best"] = online.best
            for f in online.fits:
                row[f"online_{f.model}"] = f.model_dump()
        rows.append(row)
        if k == args.example_plant and args.example:
            _example(args.example, obs, lab_t, lab_x)
        _print(row)

    _errors(rows)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2))


def _example(folder: Path, obs, lab_t: np.ndarray, lab_x: np.ndarray) -> None:
    """A historian export (the flows and AT-101, 1-minute) and the lab results, for the docs."""
    import polars as pl

    folder.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    stamps = [start + timedelta(minutes=i) for i in range(len(obs))]
    columns = {c: np.round(obs[c].to_numpy(dtype=float), 4) for c in ("FT-101", "FT-102")}
    pl.DataFrame({"timestamp": stamps, **columns}).drop_nulls().write_parquet(
        folder / "historian.parquet"
    )
    sampled = [start + timedelta(microseconds=int(t)) for t in lab_t]
    pl.DataFrame(
        {"property": "product_property", "value": np.round(lab_x, 4), "sampled_at": sampled}
    ).write_csv(folder / "lims.csv")


def _print(row: dict) -> None:
    print(
        f"plant {row['plant']}: flow {row['mean_flow']} m3/h, {row['lab_samples']} lab samples; "
        f"true dead {row['true_dead_m3']} m3, mixing {row['true_mixing_m3']} m3, "
        f"delay {row['true_delay_min']} min"
    )
    for kind in ("lab", "online"):
        for model in ("fixed", "flow", "mixing"):
            f = row.get(f"{kind}_{model}")
            if f is None:
                continue
            print(
                f"  {kind:<6} {model:<6} dead {f['dead']:<7g} [{f['dead_low']:g}, "
                f"{f['dead_high']:g}]  mixing {f['mixing']:<6g}  delay {f['residence_min']:<6g} "
                f"corr {f['corr']:+.3f}  {'*' if f['significant'] else ' '}"
                + ("  best" if row.get(f"{kind}_best") == model else "")
            )
    print(
        f"  online true: dead {row['online_true_dead_m3']} m3, delay "
        f"{row['online_true_delay_min']} min"
    )


def _errors(rows: list[dict]) -> None:
    def mae(kind: str, model: str, key: str, truth: str) -> str:
        values = [abs(r[f"{kind}_{model}"][key] - r[truth]) for r in rows if f"{kind}_{model}" in r]
        return f"{np.mean(values):.2f}" if values else "-"

    print()
    print("lab: mean absolute error over the plants")
    for model in ("fixed", "flow", "mixing"):
        line = f"  {model:<6} delay {mae('lab', model, 'residence_min', 'true_delay_min')} min"
        if model != "fixed":
            line += f", dead {mae('lab', model, 'dead', 'true_dead_m3')} m3"
        if model == "mixing":
            line += f", mixing {mae('lab', model, 'mixing', 'true_mixing_m3')} m3"
        print(line)
    best = [r["lab_best"] for r in rows if "lab_best" in r]
    print(f"  chosen: {', '.join(f'{m} {best.count(m)}' for m in sorted(set(best)))}")
    print("online: mean absolute error of the delay")
    for model in ("fixed", "flow", "mixing"):
        print(f"  {model:<6} {mae('online', model, 'residence_min', 'online_true_delay_min')} min")


if __name__ == "__main__":
    main()
