"""Run ``investigate`` on real off-spec lots of the Homeostat Live plant isidis-01.

Downloads a window of public data (sensors, lab results, data dictionary), builds the plant
graph from the dictionary plus the flow order published in the plant card
(https://homeostat.kausalflow.com/live/isidis-01.md), labels each lab sample as a lot with the
grade the plant was making, and runs ``investigate`` on the first lot of the most recent
off-spec episodes. Only public API data is used.

    uv run python scripts/investigate_homeostat_live.py --out out/isidis-01

writes ``summary.md`` (lots, off-spec episodes, each investigation's ranked causes) and one
report folder per investigated lot. The Homeostat Live workflow runs it in GitHub Actions.

Grades and their lab ranges come from the plant card. Purity (AT-201) below the grade's range
is off spec; dew point (AT-202) above it is off spec (wetter gas), below it is only drier.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
from plantgraph import (
    Actuator,
    Edge,
    EdgeType,
    Equipment,
    Loop,
    PlantGraph,
    Quality,
    SamplePoint,
    Source,
    Stream,
    Tag,
)

import penguineer
from penguineer.core.errors import PenguineerError
from penguineer.io.graph import save_graph
from penguineer.io.historian import read_historian
from penguineer.io.homeostat_live import fetch_window, graph_from_tags

PLANT = "isidis-01"
PURITY, DEW = "AT-201", "AT-202"


@dataclass(frozen=True)
class Grade:
    name: str
    h2_rate: float  # recipe H2.rate, which tells the grades apart
    purity: tuple[float, float]  # %, range
    dew: tuple[float, float]  # degC, range


# From the plant card's "Product grades and specifications".
GRADES = (
    Grade("propellant", 4, (99.5, 99.9), (-80, -72)),
    Grade("surface fuel", 9, (96, 98), (-62, -48)),
    Grade("feedstock", 14, (81, 92), (-34, -20)),
)

# Flow order from the plant card: (from, to). Equipment and streams are named here; the card
# publishes no volumes, so the edges carry no dead times.
NODES = [
    Equipment(id="EL-101", name="Electrolysers", kind="electrolyser"),
    Stream(id="S-H2", name="Hydrogen feed", phase="gas"),
    Equipment(id="CA-102", name="CO2 acquisition", kind="compressor"),
    Stream(id="S-CO2", name="CO2 feed", phase="gas"),
    Stream(id="S-feed", name="Reactor feed gas", phase="gas"),
    Equipment(id="R-201", name="Sabatier reactor", kind="reactor"),
    Equipment(id="V-301", name="Receiver", kind="vessel"),
    Equipment(id="E-301", name="Condenser", kind="exchanger"),
    Equipment(id="D-401", name="Adsorbent dryer", kind="dryer"),
    Stream(id="S-product", name="Product methane", phase="gas"),
    SamplePoint(id="SP-product", name="Product sample (rover, every 3 h)"),
    Actuator(id="FV-101", kind="valve"),
    Actuator(id="FV-102", kind="valve"),
    Actuator(id="TV-201", kind="valve", description="coolant valve"),
]
FLOWS = [
    ("EL-101", "S-H2"),
    ("S-H2", "S-feed"),
    ("CA-102", "S-CO2"),
    ("S-CO2", "S-feed"),
    ("S-feed", "R-201"),
    ("R-201", "V-301"),
    ("V-301", "E-301"),
    ("E-301", "D-401"),
    ("D-401", "S-product"),
]
MEASURES = {  # tag -> (node, variable)
    "JT-001": ("EL-101", "power"),
    "FT-101": ("S-H2", "flow"),
    "FT-102": ("S-CO2", "flow"),
    "TT-200": ("S-feed", "temperature"),
    "TT-201": ("R-201", "temperature"),
    "TT-301": ("E-301", "temperature"),
}
VALVES = {  # loop PV -> (actuator, what it manipulates)
    "FT-101": ("FV-101", "S-H2"),
    "FT-102": ("FV-102", "S-CO2"),
    "TT-201": ("TV-201", "R-201"),
}
# Recipe tags that change with the grade. investigate compares a lot only with earlier lots of
# the same regime, which it reads from mode tags, so these count as mode tags here.
RECIPE = ("H2.rate", "DRY.bypass", "FFC-102.ratio", "TIC-201.sp")


def build_graph(tags_csv: Path, grade: Grade) -> PlantGraph:
    base = graph_from_tags(tags_csv, unit=PLANT)
    names = {n.tag for n in base.nodes if isinstance(n, Tag)}
    missing = sorted((set(MEASURES) | {"H2.rate"}) - names)
    if missing:
        raise SystemExit(f"the data dictionary has no tags {', '.join(missing)}")
    nodes = []
    for n in base.nodes:
        if isinstance(n, Tag) and n.tag in RECIPE:
            n = n.model_copy(update={"role": "mode"})
        if isinstance(n, Quality) and n.property == PURITY:
            n = n.model_copy(update={"spec_low": grade.purity[0]})
        if isinstance(n, Quality) and n.property == DEW:
            n = n.model_copy(update={"spec_high": grade.dew[1]})
        nodes.append(n)
    by_tag = {n.tag: n.id for n in nodes if isinstance(n, Tag)}
    loops = {n.pv: n.id for n in nodes if isinstance(n, Loop) and n.pv}
    qualities = [n.id for n in nodes if isinstance(n, Quality)]
    nodes += [n.model_copy(update={"source": Source.user}) for n in NODES]

    edges: list[Edge] = []

    def edge(kind: EdgeType, a: str, b: str, **kw) -> None:
        edges.append(Edge(id=f"{kind.value}:{a}->{b}", type=kind, **{"from": a, "to": b}, **kw))

    for a, b in FLOWS:
        edge(EdgeType.flows_to, a, b)
    for tag, (node, variable) in MEASURES.items():
        edge(EdgeType.measures, by_tag[tag], node, measured_variable=variable)
    for pv, (valve, target) in VALVES.items():
        loop = loops.get(by_tag[pv])
        if loop is not None:
            edge(EdgeType.controls, loop, valve, acting="direct")
        edge(EdgeType.manipulates, valve, target)
    edge(EdgeType.samples, "SP-product", "S-product")
    for q in qualities:
        edge(EdgeType.reported_as, "SP-product", q)
    return PlantGraph(unit=PLANT, nodes=nodes, edges=edges)


def grade_of(rate: float | None) -> Grade | None:
    if rate is None:
        return None
    best = min(GRADES, key=lambda g: abs(g.h2_rate - rate))
    return best if abs(best.h2_rate - rate) < 0.5 else None


def label_lots(lab_csv: Path, sensors_csv: Path) -> pl.DataFrame:
    """One row per lab sample: lot, sampled_at, grade, purity, dew point, off_spec."""
    lab = pl.read_csv(lab_csv, try_parse_dates=True)
    lab = lab.with_columns(pl.col("sampled_at").dt.replace_time_zone("UTC", non_existent="null"))
    wide = lab.pivot(on="tag", index="sampled_at", values="value", aggregate_function="first").sort(
        "sampled_at"
    )
    for col in (PURITY, DEW):
        if col not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
    rate = (
        read_historian(sensors_csv, tags=["H2.rate"])
        .frame.select("timestamp", "value")
        .drop_nulls()
        .sort("timestamp")
    )
    wide = wide.join_asof(rate, left_on="sampled_at", right_on="timestamp", strategy="backward")
    rows = []
    for r in wide.iter_rows(named=True):
        g = grade_of(r["value"])
        pur, dew = r[PURITY], r[DEW]
        off = []
        if g is not None and pur is not None and pur < g.purity[0]:
            off.append(PURITY)
        if g is not None and dew is not None and dew > g.dew[1]:
            off.append(DEW)
        rows.append(
            {
                "lot": f"S{r['sampled_at']:%Y%m%dT%H%M}",
                "sampled_at": r["sampled_at"],
                "grade": g.name if g else None,
                PURITY: pur,
                DEW: dew,
                "off_spec": ",".join(off),
            }
        )
    return pl.DataFrame(rows)


def lims_with_lots(lab_csv: Path, lots: pl.DataFrame, dest: Path) -> Path:
    lab = pl.read_csv(lab_csv, try_parse_dates=True).with_columns(
        pl.col("sampled_at").dt.replace_time_zone("UTC", non_existent="null")
    )
    lab = lab.join(lots.select("sampled_at", "lot"), on="sampled_at", how="left")
    lab.write_csv(dest)
    return dest


def episodes(lots: pl.DataFrame, gap: int = 3) -> list[dict]:
    """Runs of off-spec lots, broken by more than ``gap`` in-spec lots in a row (9 h)."""
    out: list[dict] = []
    current: dict | None = None
    in_spec = 0
    for r in lots.iter_rows(named=True):
        if r["off_spec"]:
            if current is None or in_spec > gap or r["grade"] != current["first"]["grade"]:
                current = {"first": r, "n": 0, "props": set()}
                out.append(current)
            current["n"] += 1
            current["props"] |= set(r["off_spec"].split(","))
            current["last"] = r
            in_spec = 0
        else:
            in_spec += 1
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    parser.add_argument("--since", type=datetime.fromisoformat, default=now - timedelta(days=28))
    parser.add_argument("--until", type=datetime.fromisoformat, default=now)
    parser.add_argument("--lot", help="Investigate this lot only (default: newest episodes)")
    parser.add_argument("--episodes", type=int, default=3, help="Off-spec episodes to investigate")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    out: Path = args.out
    window = fetch_window(PLANT, args.since, args.until, out / "data")
    lots = label_lots(window.lab, window.sensors)
    lims = lims_with_lots(window.lab, lots, out / "data" / "lims.csv")
    lots.write_csv(out / "lots.csv")

    first, last = (lots["sampled_at"].min(), lots["sampled_at"].max()) if lots.height else (0, 0)
    lines = [
        f"# investigate on Homeostat Live {PLANT}",
        "",
        f"Window {args.since:%Y-%m-%d %H:%M} to {args.until:%Y-%m-%d %H:%M} UTC. "
        f"{lots.height} lab samples"
        + (f", {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M}" if lots.height else "")
        + ".",
        "",
        "| Grade | Lots | Off spec |",
        "|---|---:|---:|",
    ]
    for (g,), part in sorted(lots.group_by("grade"), key=lambda x: str(x[0][0])):
        lines.append(f"| {g} | {part.height} | {(part['off_spec'] != '').sum()} |")
    eps = episodes(lots)
    lines += [
        "",
        f"{len(eps)} off-spec episodes (off-spec lots, broken by more than 3 in-spec lots):",
        "",
        "| First lot | Last lot | Off-spec lots | Grade | Off spec on | First purity | First dew |",
    ]
    lines += ["|---|---|---:|---|---|---:|---:|"]
    for e in eps:
        f = e["first"]
        lines.append(
            f"| {f['lot']} | {e['last']['lot']} | {e['n']} | {f['grade']} | "
            f"{', '.join(sorted(e['props']))} | {f[PURITY]} | {f[DEW]} |"
        )

    if args.lot:
        targets = lots.filter(pl.col("lot") == args.lot).to_dicts()
    else:
        targets = [e["first"] for e in reversed(eps)]
    done = 0
    for t in targets:
        if done >= args.episodes:
            break
        grade = next(g for g in GRADES if g.name == t["grade"]) if t["grade"] else None
        lines += ["", f"## Lot {t['lot']} ({t['grade']})", ""]
        if grade is None:
            lines.append("No grade at the sample time (H2.rate not on a recipe value); skipped.")
            continue
        graph = save_graph(build_graph(window.tags, grade), out / f"graph-{t['lot']}.yaml")
        try:
            result = penguineer.run(
                "investigate",
                lot=t["lot"],
                lims=lims,
                data=window.sensors,
                graph=graph,
                project=out,
                out=out / "reports" / t["lot"],
            )
        except PenguineerError as e:
            lines += [f"investigate stopped: {e}"]
            continue
        lines += ["```text", result.summary(), "```"]
        done += 1

    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
