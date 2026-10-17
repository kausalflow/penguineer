"""Run the bench plant in Homeostat and write one scenario bundle per drawn plant.

A bundle is what a plant export looks like, plus the answer:

- ``historian.parquet``: every recorded tag except the lab, long format (timestamp, tag, value)
- ``lims.csv`` and ``lots.csv``: the lab results and the 4-hour lots they describe
- ``graph.yaml``: the true plant graph in the plantgraph schema
- ``tags.csv``: the tag list alone, the input for scoring ``graph draft``
- ``truth.json``: the fault, its onset, the root-cause set and the lot to investigate

Needs Homeostat (``pip install "penguineer[bench]"``). The engine never sees ``truth.json``.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import yaml
from plantgraph import (
    Actuator,
    Edge,
    Equipment,
    Loop,
    PlantGraph,
    Quality,
    SamplePoint,
    Source,
    Stream,
    Tag,
)

from penguineer.bench.plant import (
    DURATION_S,
    EQUIPMENT,
    FAULTS,
    LAB_DELAY_S,
    LAB_NOISE_PCT,
    LAB_PERIOD_S,
    LAB_SPAN,
    PROPERTY,
    START,
    FaultType,
    family_seed,
    scenario_config,
)
from penguineer.core.errors import UsageError
from penguineer.io.homeostat import Truth

LAB_TAG = "AT-301"
_T0 = datetime.fromisoformat(START).replace(tzinfo=UTC)


def _homeostat():
    try:
        import homeostat
    except ImportError:
        raise UsageError(
            'the bench needs Homeostat to generate scenarios: pip install "penguineer[bench]"'
        ) from None
    return homeostat


# ---------------------------------------------------------------- the graph


def _tag(name: str, unit: str | None, lo: float | None, hi: float | None, role=None) -> Tag:
    return Tag(
        id=name.replace(".", "-"),
        tag=name,
        unit_of_measure=unit,
        sample_period_s=60,
        range_low=lo,
        range_high=hi,
        role=role,
        source=Source.homeostat,
    )


def plant_graph(scenario: dict[str, Any]) -> PlantGraph:
    """The true graph of one drawn plant, from its concrete Homeostat scenario."""
    units = {u["id"]: u for u in scenario["units"]}
    flow = 20.0  # m3/h, both feeds at their means
    mix_min = units["MX-101"].get("volume", 2) / flow * 60
    reactor_min = units["TIC-301"].get("volume", 5) / flow * 60
    mix_tau_min = units["TIC-301"].get("tau_mix", 600) / 60

    nodes: list = [
        Stream(id="S-A", name="Feed A", phase="liquid"),
        Stream(id="S-B", name="Feed B", phase="liquid"),
        Equipment(id="M-101", name="Feed mixer", kind="mixer", holdup_m3=units["MX-101"]["volume"]),
        Stream(id="S-mix", name="Mixed feed", phase="liquid"),
        Equipment(id="E-201", name="Feed heater", kind="exchanger"),
        Equipment(id="R-301", name="Reactor", kind="vessel", holdup_m3=units["TIC-301"]["volume"]),
        Stream(id="S-product", name="Product", phase="liquid"),
        _tag("FT-101", "m3/h", 0, 30),
        _tag("FT-102", "m3/h", 0, 30),
        _tag("AT-101", None, 0, 10),
        _tag("TT-200", "degC", 0, 100),
        SamplePoint(id="SP-1", name="Product sample"),
        Quality(id="Q-product", property=PROPERTY),
        _tag("GRADE", None, None, None, "mode"),
    ]
    edges = [
        Edge(id="f1", type="flows_to", from_id="S-A", to_id="M-101", dead_time_min=0),
        Edge(id="f2", type="flows_to", from_id="S-B", to_id="M-101", dead_time_min=0),
        Edge(
            id="f3",
            type="flows_to",
            from_id="M-101",
            to_id="S-mix",
            dead_time_min=round(mix_min, 1),
            dead_time_low_min=round(0.7 * mix_min, 1),
            dead_time_high_min=round(1.4 * mix_min, 1),
        ),
        Edge(id="f3b", type="flows_to", from_id="S-mix", to_id="E-201", dead_time_min=0),
        Edge(
            id="f4",
            type="flows_to",
            from_id="E-201",
            to_id="R-301",
            dead_time_min=0,
            dead_time_low_min=0,
            dead_time_high_min=6,
        ),
        Edge(
            id="f5",
            type="flows_to",
            from_id="R-301",
            to_id="S-product",
            dead_time_min=round(reactor_min + mix_tau_min, 1),
            dead_time_low_min=round(reactor_min, 1),
            dead_time_high_min=round(reactor_min + 2.5 * mix_tau_min, 1),
        ),
        Edge(id="m1", type="measures", from_id="FT-101", to_id="S-A", measured_variable="flow"),
        Edge(id="m2", type="measures", from_id="FT-102", to_id="S-B", measured_variable="flow"),
        Edge(
            id="m3",
            type="measures",
            from_id="AT-101",
            to_id="M-101",
            measured_variable="composition",
        ),
        Edge(
            id="m4",
            type="measures",
            from_id="TT-200",
            to_id="S-mix",
            measured_variable="temperature",
        ),
        Edge(id="s1", type="samples", from_id="SP-1", to_id="S-product"),
        Edge(id="r1", type="reported_as", from_id="SP-1", to_id="Q-product"),
    ]
    for loop_id, unit_id, n in (("TIC-201", "TIC-201", "201"), ("TIC-301", "TIC-301", "301")):
        equipment = EQUIPMENT[unit_id]
        lo, hi = units[unit_id].get("span", (0.0, 200.0))
        pv = _tag(f"TT-{n}", "degC", lo, hi)
        sp = _tag(f"{loop_id}.sp", "degC", None, None, "setpoint")
        op = _tag(f"{loop_id}.out", "%", 0, 100, "output")
        mode = _tag(f"{loop_id}.mode", None, None, None, "setting")
        nodes += [
            pv,
            sp,
            op,
            mode,
            Loop(id=loop_id, pv=pv.id, sp=sp.id, op=op.id, mode=mode.id),
            Actuator(id=f"TV-{n}", kind="valve"),
        ]
        edges += [
            Edge(
                id=f"m-{n}",
                type="measures",
                from_id=pv.id,
                to_id=equipment,
                measured_variable="temperature",
            ),
            Edge(id=f"c-{n}", type="controls", from_id=loop_id, to_id=f"TV-{n}", acting="direct"),
            Edge(id=f"v-{n}", type="manipulates", from_id=f"TV-{n}", to_id=equipment),
        ]
    for n in nodes:
        n.source = Source.homeostat
    for e in edges:
        e.source = Source.homeostat
    return PlantGraph(unit="bench-reactor", nodes=nodes, edges=edges)


# ---------------------------------------------------------------- the data


def _grade(timeline, times_s: np.ndarray) -> np.ndarray:
    """GRADE as an operator would log it: 1 for grade_A, 2 for grade_B, from each start."""
    grade = np.ones(times_s.size)
    for row in timeline.itertuples():
        if row.kind == "regime":
            grade[times_s >= row.start] = 2.0 if row.name.endswith("B") else 1.0
    return grade


def _historian(run) -> pl.DataFrame:
    obs = run.observed
    times_s = np.asarray((obs.index - obs.index[0]).total_seconds())
    stamps = [_T0 + timedelta(seconds=float(s)) for s in times_s]
    columns = {c: obs[c].to_numpy(dtype=float) for c in obs.columns if c != LAB_TAG}
    columns["GRADE"] = _grade(run.meta["timeline"], times_s)
    frames = [
        pl.DataFrame(
            {"timestamp": stamps, "tag": [tag] * len(stamps), "value": np.round(values, 4)},
            schema={"timestamp": pl.Datetime("us", "UTC"), "tag": pl.String, "value": pl.Float64},
        )
        for tag, values in columns.items()
    ]
    return pl.concat(frames).drop_nans("value").sort("timestamp", "tag")


def _lab(run) -> list[dict[str, Any]]:
    """One row per lab result: the lot is the 4 hours of production before the sample."""
    obs = run.observed[LAB_TAG].to_numpy(dtype=float)
    rows = []
    for k in range(1, DURATION_S // LAB_PERIOD_S):
        sampled = k * LAB_PERIOD_S
        reported = sampled + LAB_DELAY_S
        if reported // 60 >= obs.size:
            break
        start = _T0 + timedelta(seconds=sampled - LAB_PERIOD_S)
        rows.append(
            {
                "lot": f"L{start:%m%d}-{start.hour // 4 + 1}",
                "sample_point": "SP-1",
                "property": PROPERTY,
                "value": round(float(obs[reported // 60]), 4),
                "sampled_at": _T0 + timedelta(seconds=sampled),
                "reported_at": _T0 + timedelta(seconds=reported),
                "start": start,
                "t_s": sampled,
            }
        )
    return rows


def _spec(rows: list[dict], onset_s: float, grade_b: tuple[float, float]) -> tuple[float, float]:
    """Spec limits from the in-control lots: grade A lots before the fault."""
    before = np.array(
        [
            r["value"]
            for r in rows
            if r["t_s"] < onset_s and not (grade_b[0] <= r["t_s"] <= grade_b[1] + LAB_PERIOD_S)
        ]
    )
    centre = float(np.median(before))
    robust_sd = 1.4826 * float(np.median(np.abs(before - centre)))
    lab_sd = LAB_NOISE_PCT / 100 * (LAB_SPAN[1] - LAB_SPAN[0])
    half = 3 * max(robust_sd, lab_sd)
    return round(centre - half, 3), round(centre + half, 3)


def export_run(run, fault: FaultType, scenario_id: str, out: Path) -> Truth | None:
    """Write one bundle; ``None`` (and nothing written) when no lot went off spec."""
    events = run.meta["events"]
    onset_s = float(events.iloc[-1]["at"])
    timeline = run.meta["timeline"]
    grade_b = next(
        (
            (r.start, r.end)
            for r in timeline.itertuples()
            if r.kind == "regime" and r.name.endswith("B")
        ),
        (0.0, 0.0),
    )
    rows = _lab(run)
    low, high = _spec(rows, onset_s, grade_b)
    affected = [r for r in rows if r["t_s"] > onset_s and not (low <= r["value"] <= high)]
    if not affected:
        return None

    scenario = run.meta["scenario"]
    graph = plant_graph(scenario)
    q = graph.node("Q-product")
    q.spec_low, q.spec_high = low, high

    out.mkdir(parents=True, exist_ok=True)
    _historian(run).write_parquet(out / "historian.parquet")
    with (out / "lims.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lot", "sample_point", "property", "value", "sampled_at", "reported_at"])
        for r in rows:
            w.writerow(
                [
                    r["lot"],
                    r["sample_point"],
                    r["property"],
                    r["value"],
                    f"{r['sampled_at']:%Y-%m-%dT%H:%M:%SZ}",
                    f"{r['reported_at']:%Y-%m-%dT%H:%M:%SZ}",
                ]
            )
    with (out / "lots.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lot", "start", "end"])
        for r in rows:
            w.writerow(
                [
                    r["lot"],
                    f"{r['start']:%Y-%m-%dT%H:%M:%SZ}",
                    f"{r['sampled_at']:%Y-%m-%dT%H:%M:%SZ}",
                ]
            )
    (out / "graph.yaml").write_text(
        yaml.safe_dump(graph.to_data(), sort_keys=False, allow_unicode=True)
    )
    with (out / "tags.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tag", "unit"])
        for t in sorted(graph.tag_by_name().values(), key=lambda t: t.tag):
            w.writerow([t.tag, t.unit_of_measure or ""])

    event = events.iloc[-1]
    truth = Truth(
        scenario=scenario_id,
        fault_type=fault.name,
        kind=fault.kind,
        data_fault=fault.data_fault,
        root_cause=list(fault.root_cause),
        onset=_T0 + timedelta(seconds=onset_s),
        magnitude=None if event["value"] != event["value"] else float(event["value"]),
        lot=affected[0]["lot"],
        affected_lots=[r["lot"] for r in affected],
        description=fault.description,
        homeostat=run.meta["reproducibility"]["homeostat"],
        config_hash=run.meta["reproducibility"]["config_hash"],
    )
    (out / "truth.json").write_text(truth.model_dump_json(indent=2))
    return truth


def export_split(
    out: Path,
    *,
    split_seed: int,
    per_fault: int,
    faults: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Generate ``per_fault`` plants of every fault type and write their bundles under ``out``.

    Writes ``index.json``: the scenarios written and the plants skipped because no lot went
    off spec (the fault was too small to matter, so it is not a miss).
    """
    homeostat = _homeostat()
    step = progress or (lambda done, total, message: None)
    names = faults or list(FAULTS)
    unknown = sorted(set(names) - set(FAULTS))
    if unknown:
        raise UsageError(f"unknown fault types: {', '.join(unknown)} (known: {', '.join(FAULTS)})")
    index: dict[str, Any] = {"split_seed": split_seed, "scenarios": [], "skipped": []}
    for i, name in enumerate(names):
        fault = FAULTS[name]
        step(i, len(names), f"simulating {name}")
        fam = homeostat.family(
            scenario_config(fault, family_seed(split_seed, name), per_fault), plants=per_fault
        )
        for k in range(per_fault):
            scenario_id = f"{name}-{k:03d}"
            truth = export_run(fam.run(k), fault, scenario_id, out / scenario_id)
            if truth is None:
                index["skipped"].append(scenario_id)
            else:
                index["scenarios"].append(scenario_id)
    index["homeostat"] = homeostat.__version__
    (out / "index.json").write_text(json.dumps(index, indent=2))
    step(len(names), len(names), "done")
    return index
