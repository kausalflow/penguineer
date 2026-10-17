"""The investigate pipeline: resolve, align, regime, candidates, tests, loop check, rank.

Deterministic and LLM-free. The command in ``penguineer.commands.investigate`` reads the files,
calls :func:`investigate`, and writes the report.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
from plantgraph import EdgeType, Loop, PlantGraph, Quality, Tag

from penguineer.core.errors import NotEnoughData, UsageError, ValidationFailed
from penguineer.engines.align import (
    Grid,
    NodePath,
    aligned,
    measuring_tags,
    median_period_us,
    upstream_paths,
)
from penguineer.engines.evidence import (
    Effect,
    change_point,
    effect_size,
    explained_fraction,
    lag_correlation,
    onset_strength,
)
from penguineer.engines.health import HealthConfig, HealthFinding, Severity, assess
from penguineer.report.model import (
    EvidenceItem,
    Hypothesis,
    HypothesisKind,
    QualityEvent,
    ReferenceWindow,
    RuledOut,
    TestName,
    TestResult,
)

_US = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Provisional: fitted on the homeostat training split in the Bench milestone.
WEIGHTS = {TestName.effect_size: 0.4, TestName.onset: 0.3, TestName.lag_correlation: 0.3}
FAULT_SCORE = {Severity.critical: 0.6, Severity.warning: 0.45, Severity.info: 0.15}
# Provisional: chosen on the homeostat validation split in the Bench milestone.
HIGH, MEDIUM, MARGIN = 0.65, 0.4, 0.15
CALIBRATION = (
    "Provisional thresholds (high: score >= 0.65 and 0.15 ahead of the next; medium: >= 0.4). "
    "They will be set on the public homeostat validation split."
)


def _us(t: datetime) -> int:
    return int((t - _EPOCH) / timedelta(microseconds=1))


def _dt(us: float) -> datetime:
    return _EPOCH + timedelta(microseconds=int(us))


@dataclass
class Settings:
    hops: int = 6
    reference_lots: int = 3
    step_s: float | None = None
    max_window_hours: float = 24.0
    default_window_hours: float = 8.0
    top: int = 5


@dataclass
class Candidate:
    tag: Tag
    path: NodePath
    loop: Loop | None = None


@dataclass
class Scored:
    hypothesis: Hypothesis
    effect: Effect | None = None
    series: np.ndarray | None = None  # aligned, for plots and explain-away


@dataclass
class Investigation:
    event: QualityEvent
    regime: dict[str, float]
    reference: list[ReferenceWindow]
    grid: Grid
    deviation_start_us: int
    candidates: int
    hypotheses: list[Hypothesis]
    ruled_out: list[RuledOut]
    evidence: list[EvidenceItem]
    health: list[HealthFinding]
    notes: list[str]
    # For plots: aligned series by tag, the online quality signal, lab results.
    series: dict[str, np.ndarray] = field(default_factory=dict)
    proxy: np.ndarray | None = None
    lab: list[tuple[datetime, float]] = field(default_factory=list)


# ---------------------------------------------------------------- resolve


def _deviation(q: Quality, v: float) -> float:
    """How far outside spec, in units of the spec span (0 inside spec)."""
    span = (
        (q.spec_high - q.spec_low)
        if q.spec_low is not None and q.spec_high is not None
        else max(abs(q.spec_low if q.spec_low is not None else q.spec_high or 1.0), 1e-9)
    )
    if q.spec_low is not None and v < q.spec_low:
        return (q.spec_low - v) / span
    if q.spec_high is not None and v > q.spec_high:
        return (v - q.spec_high) / span
    return 0.0


def _sampled_node(graph: PlantGraph, quality: Quality, sample_point: str | None) -> tuple:
    points = [e.from_id for e in graph.edges_of(EdgeType.reported_as) if e.to_id == quality.id]
    if sample_point in points:
        points = [sample_point]
    for sp in sorted(points):
        for e in graph.edges_of(EdgeType.samples):
            if e.from_id == sp:
                return sp, e.to_id
    raise ValidationFailed(
        f"quality {quality.id} has no sample point with a samples edge, "
        "so penguineer cannot tell where the lab sample is taken"
    )


def _window(
    lims_prop: pl.DataFrame,
    lot: str,
    sampled_at: datetime,
    lots: pl.DataFrame | None,
    s: Settings,
) -> tuple[datetime, datetime]:
    if lots is not None:
        row = lots.filter(pl.col("lot") == lot)
        if row.height:
            return row["start"][0], row["end"][0]
    earlier = lims_prop.filter(pl.col("sampled_at") < sampled_at)["sampled_at"]
    floor = sampled_at - timedelta(hours=s.max_window_hours)
    if earlier.len():
        return max(earlier.max(), floor), sampled_at + timedelta(minutes=1)
    return sampled_at - timedelta(hours=s.default_window_hours), sampled_at + timedelta(minutes=1)


# ---------------------------------------------------------------- regime


def _regime(
    raw: dict[str, tuple[np.ndarray, np.ndarray]], mode_tags: list[str], start: int, end: int
) -> tuple[dict[str, float], bool]:
    """Most common value of each mode tag in [start, end), and whether one value covers 90%."""
    label: dict[str, float] = {}
    mixed = False
    for tag in mode_tags:
        if tag not in raw:
            continue
        t, x = raw[tag]
        v = x[(t >= start) & (t < end) & np.isfinite(x)]
        if v.size == 0:
            continue
        counts = Counter(np.round(v, 6).tolist())
        value, n = counts.most_common(1)[0]
        label[tag] = float(value)
        mixed = mixed or n < 0.9 * v.size
    return label, mixed


# ---------------------------------------------------------------- scoring


def _score(tests: list[TestResult]) -> float:
    ran = [(WEIGHTS[t.name], t.strength) for t in tests if t.strength is not None]
    if not ran:
        return 0.0
    value = sum(w * s for w, s in ran) / sum(w for w, _ in ran)
    effect = next((t.strength for t in tests if t.name == TestName.effect_size), None)
    return float(value if effect else 0.5 * value)


def _confidence(scores: list[float]) -> list[str]:
    out = []
    for i, s in enumerate(scores):
        label = "high" if s >= HIGH else "medium" if s >= MEDIUM else "low"
        if i == 0 and label == "high" and len(scores) > 1 and s - scores[1] < MARGIN:
            label = "medium"
        if i > 0 and label == "high":
            label = "medium"  # only the leading hypothesis can be confident
        out.append(label)
    return out


def _fmt(v: float) -> str:
    return f"{v:.3g}" if abs(v) < 1000 else f"{v:.0f}"


# ---------------------------------------------------------------- pipeline


def investigate(
    graph: PlantGraph,
    historian: pl.DataFrame,
    lims: pl.DataFrame,
    lot: str,
    *,
    lots: pl.DataFrame | None = None,
    quality: str | None = None,
    settings: Settings | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> Investigation:
    s = settings or Settings()
    notes: list[str] = []
    step = progress or (lambda done, total, message: None)

    # 1. Resolve the lot, the quality and where it is sampled.
    step(0, 7, "resolve")
    rows = lims.filter(pl.col("lot") == lot)
    if rows.is_empty():
        known = lims["lot"].drop_nulls().unique(maintain_order=True).head(5).to_list()
        raise UsageError(
            f"lot {lot!r} is not in the LIMS export"
            + (f" (lots there include {', '.join(known)})" if known else " (it has no lot column)")
        )
    qualities = {n.property: n for n in graph.nodes if isinstance(n, Quality)}
    rows = rows.filter(pl.col("property").is_in(list(qualities)))
    if quality is not None:
        rows = rows.filter(pl.col("property") == quality)
    if rows.is_empty():
        raise UsageError(
            f"lot {lot!r} has no result for a property with a Quality node in the graph "
            f"({', '.join(sorted(qualities)) or 'none defined'})"
        )
    devs = [_deviation(qualities[r["property"]], r["value"]) for r in rows.iter_rows(named=True)]
    i = int(np.argmax(devs))
    if devs[i] == 0 and quality is None:
        raise NotEnoughData(
            f"lot {lot!r} is within spec on every property with a Quality node; "
            "pass --quality to investigate one anyway"
        )
    row = rows.row(i, named=True)
    q = qualities[row["property"]]
    value, sampled_at = float(row["value"]), row["sampled_at"]
    sample_point, sampled_node = _sampled_node(graph, q, row.get("sample_point"))
    prop_rows = lims.filter(pl.col("property") == q.property).sort("sampled_at")
    ev_start, ev_end = _window(prop_rows, lot, sampled_at, lots, s)

    # 2. Candidates: tags on nodes upstream of the sampled node, loops on them, mode tags.
    step(1, 7, "candidates")
    paths = upstream_paths(graph, sampled_node, s.hops)
    measured = measuring_tags(graph)
    by_id = {n.id: n for n in graph.nodes}
    loops_by_pv = {n.pv: n for n in graph.nodes if isinstance(n, Loop) and n.pv is not None}
    candidates: list[Candidate] = []
    for node, path in paths.items():
        if node == sampled_node:
            continue
        for tag in measured.get(node, []):
            if tag.role in (None, "measurement"):
                candidates.append(Candidate(tag, path, loops_by_pv.get(tag.id)))
    proxies = [t for t in measured.get(sampled_node, []) if t.role in (None, "measurement")]
    mode_tags = sorted(n.tag for n in graph.nodes if isinstance(n, Tag) and n.role == "mode")
    if not candidates:
        raise ValidationFailed(
            f"no measured tags upstream of {sampled_node} within {s.hops} flows_to hops; "
            "add measures and flows_to edges to the graph"
        )
    if any(not c.path.known for c in candidates):
        notes.append("some flows_to edges have no dead time; they count as 0 min")

    raw: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for (tag,), part in historian.partition_by("tag", as_dict=True).items():
        part = part.drop_nulls("value").sort("timestamp")
        raw[tag] = (part["timestamp"].dt.epoch("us").to_numpy(), part["value"].to_numpy())
    missing = sorted({c.tag.tag for c in candidates} - set(raw))
    if missing:
        notes.append("tags in the graph but not in the data: " + ", ".join(missing))
    candidates = [c for c in candidates if c.tag.tag in raw]
    proxy_tag = next((t for t in proxies if t.tag in raw), None)
    if not candidates:
        raise NotEnoughData("none of the candidate tags are in the historian export")

    # 3. Regime and reference windows: earlier in-spec lots in the same regime.
    step(2, 7, "regime")
    event_regime, mixed = _regime(raw, mode_tags, _us(ev_start), _us(ev_end))
    if not mode_tags:
        notes.append("no mode tags in the graph: every lot counts as the same regime")
    if mixed:
        notes.append("the lot spans more than one regime; its most common one is used")
    reference: list[ReferenceWindow] = []
    skipped_regime = 0
    for r in prop_rows.filter(pl.col("sampled_at") < ev_start).reverse().iter_rows(named=True):
        if len(reference) >= s.reference_lots:
            break
        if r["lot"] in (None, lot) or _deviation(q, r["value"]) > 0:
            continue
        start, end = _window(prop_rows, r["lot"], r["sampled_at"], lots, s)
        label, mix = _regime(raw, mode_tags, _us(start), _us(end))
        if mix or label != event_regime:
            skipped_regime += 1
            continue
        reference.append(ReferenceWindow(lot=r["lot"], start=start, end=end, value=r["value"]))
    if skipped_regime:
        notes.append(f"{skipped_regime} earlier in-spec lots skipped: different regime")
    if not reference:
        regime_text = ", ".join(f"{k}={_fmt(v)}" for k, v in event_regime.items()) or "one regime"
        raise NotEnoughData(
            f"no in-spec lot of {q.property} before {lot} in the same regime ({regime_text}), "
            "so there is nothing to compare the lot with"
        )

    # 4. Align everything on one grid in sampled-node time.
    step(3, 7, "align")
    periods = [median_period_us(raw[c.tag.tag][0]) for c in candidates]
    if proxy_tag is not None:
        periods.append(median_period_us(raw[proxy_tag.tag][0]))
    step_us = int(s.step_s * _US) if s.step_s else max(max(periods), _US)
    length = _us(ev_end) - _us(ev_start)
    grid_start = min(_us(reference[-1].start), _us(ev_start) - length)
    grid = Grid.spanning(grid_start, _us(ev_end), step_us)
    event_mask = grid.mask(_us(ev_start), _us(ev_end))
    ref_mask = np.zeros(grid.n, dtype=bool)
    for w in reference:
        ref_mask |= grid.mask(_us(w.start), _us(w.end))
    search_mask = grid.mask(_us(ev_start) - length, _us(ev_end))
    search_times = grid.times[search_mask]

    proxy = aligned(*raw[proxy_tag.tag], grid, 0.0) if proxy_tag is not None else None
    quality_sign = -1.0 if q.spec_low is not None and value < q.spec_low else 1.0
    if _deviation(q, value) == 0:
        ref_values = np.array([w.value for w in reference])
        quality_sign = 1.0 if value >= np.median(ref_values) else -1.0
    onset_us: int | None = None
    if proxy is not None:
        cp = change_point(proxy[search_mask])
        if cp is not None and abs(cp.z) >= 4 and np.sign(cp.shift) == quality_sign:
            onset_us = int(search_times[cp.index])
        else:
            notes.append(f"{proxy_tag.tag} shows no clear step in the lot: onset test skipped")
    else:
        notes.append(
            "no online analyzer on the sampled stream: onset and correlation tests skipped"
        )
    dev_start = onset_us if onset_us is not None else _us(ev_start)
    dev_mask = grid.mask(dev_start, _us(ev_end))
    if dev_mask.sum() < 3:
        dev_start, dev_mask = _us(ev_start), event_mask

    # 5. Health of the candidate tags around the lot and the reference.
    step(4, 7, "health")
    max_dead = max(c.path.dead_time_min for c in candidates)
    health_tags = [c.tag.tag for c in candidates] + ([proxy_tag.tag] if proxy_tag else [])
    h_start = _dt(grid_start - max_dead * 60 * _US)
    h_frame = historian.filter(
        pl.col("tag").is_in(health_tags)
        & (pl.col("timestamp") >= h_start)
        & (pl.col("timestamp") <= ev_end)
    )
    ranges = {
        c.tag.tag: (c.tag.range_low, c.tag.range_high)
        for c in candidates
        if c.tag.range_low is not None or c.tag.range_high is not None
    }
    health, _ = assess(h_frame, HealthConfig(), ranges)

    def findings_in(tag: str, start_us: int, end_us: int) -> list[HealthFinding]:
        return [
            f
            for f in health
            if f.tag == tag
            and _us(f.start) < end_us
            and _us(f.end) >= start_us
            and f.severity != Severity.info
        ]

    # 6. Tests on every candidate.
    step(5, 7, "tests")
    scored: list[Scored] = []
    ruled_out: list[RuledOut] = []
    evidence: list[EvidenceItem] = []
    series: dict[str, np.ndarray] = {}

    def test_series(x: np.ndarray, path: NodePath) -> tuple[list[TestResult], Effect | None]:
        tests: list[TestResult] = []
        eff = effect_size(x[dev_mask], x[ref_mask])
        tests.append(
            TestResult(
                name=TestName.effect_size,
                strength=eff.strength if eff else None,
                value=round(eff.z, 2) if eff else None,
                detail=(
                    f"median moved {_fmt(eff.delta)} ({eff.z:+.1f} reference scales)"
                    if eff
                    else "too little data in the lot or the reference"
                ),
            )
        )
        if onset_us is None:
            tests.append(TestResult(name=TestName.onset, strength=None, detail="no quality onset"))
        else:
            cp = change_point(x[search_mask])
            tol = max(path.high_min - path.low_min, 3 * step_us / 60 / _US, 10.0)
            if cp is None or abs(cp.z) < 4:
                tests.append(TestResult(name=TestName.onset, strength=0.0, detail="no clear step"))
            else:
                gap = (int(search_times[cp.index]) - onset_us) / 60 / _US
                tests.append(
                    TestResult(
                        name=TestName.onset,
                        strength=round(onset_strength(gap, 0.0, tol), 3),
                        value=round(gap, 1),
                        detail=f"step lands {gap:+.0f} min from the quality onset "
                        f"(tolerance {tol:.0f} min)",
                    )
                )
        if proxy is None or eff is None:
            tests.append(TestResult(name=TestName.lag_correlation, strength=None, detail="not run"))
        else:
            rc = lag_correlation(x[ref_mask], proxy[ref_mask])
            if rc is None:
                tests.append(
                    TestResult(
                        name=TestName.lag_correlation,
                        strength=None,
                        detail="too little variation in the reference",
                    )
                )
            else:
                r, n = rc
                consistent = np.sign(r) * np.sign(eff.delta) == quality_sign
                tests.append(
                    TestResult(
                        name=TestName.lag_correlation,
                        strength=round(abs(r), 3) if consistent else 0.0,
                        value=round(r, 3),
                        detail=f"r = {r:+.2f} over {n} reference points"
                        + ("" if consistent else ", sign does not fit the deviation"),
                    )
                )
        return tests, eff

    def window_at(path: NodePath) -> tuple[datetime, datetime]:
        shift = path.dead_time_min * 60 * _US
        return _dt(dev_start - shift), _dt(_us(ev_end) - shift)

    for k, c in enumerate(candidates):
        x = aligned(*raw[c.tag.tag], grid, c.path.dead_time_min)
        series[c.tag.tag] = x
        tests, eff = test_series(x, c.path)
        w0, w1 = window_at(c.path)
        faults = findings_in(c.tag.tag, _us(w0), _us(w1))
        score = _score(tests) * (0.5 if faults else 1.0)
        direction = "changed" if eff is None else ("high" if eff.delta > 0 else "low")
        h = Hypothesis(
            rank=0,
            kind=HypothesisKind.process,
            cause_node=c.path.node,
            cause_tag=c.tag.tag,
            direction=direction,
            magnitude=round(eff.delta, 4) if eff else None,
            unit=c.tag.unit_of_measure,
            window_start=w0,
            window_end=w1,
            graph_path=c.path.path,
            dead_time_min=c.path.dead_time_min,
            tests=tests,
            score=round(score, 3),
            confidence="low",
            health_warnings=[f.id for f in faults],
            summary="",
        )
        scored.append(Scored(h, eff, x))
        for f in faults:
            scored.append(
                Scored(
                    Hypothesis(
                        rank=0,
                        kind=HypothesisKind.measurement_fault,
                        cause_node=c.tag.id,
                        cause_tag=c.tag.tag,
                        direction="changed",
                        window_start=f.start,
                        window_end=f.end,
                        graph_path=c.path.path,
                        dead_time_min=c.path.dead_time_min,
                        tests=[],
                        score=FAULT_SCORE[f.severity],
                        confidence="low",
                        health_warnings=[f.id],
                        summary=f"{c.tag.tag} has a {f.check} finding in the lot window: "
                        f"{f.detail}",
                    )
                )
            )
        step(5, 7, f"tests {k + 1}/{len(candidates)}")
    if proxy_tag is not None:
        for f in findings_in(proxy_tag.tag, dev_start, _us(ev_end)):
            notes.append(f"the online analyzer {proxy_tag.tag} has a {f.check} finding: {f.detail}")

    # 7. Loop check, explain away, rank.
    step(6, 7, "loops and ranking")
    process = {sc.hypothesis.cause_tag: sc for sc in scored if sc.hypothesis.kind == "process"}
    for c in candidates:
        loop = c.loop
        if loop is None or c.tag.tag not in process:
            continue
        pv = process[c.tag.tag]
        sp_tag = by_id.get(loop.sp) if loop.sp else None
        op_tag = by_id.get(loop.op) if loop.op else None
        sp_x = (
            aligned(*raw[sp_tag.tag], grid, c.path.dead_time_min)
            if (isinstance(sp_tag, Tag) and sp_tag.tag in raw)
            else None
        )
        op_x = (
            aligned(*raw[op_tag.tag], grid, c.path.dead_time_min)
            if (isinstance(op_tag, Tag) and op_tag.tag in raw)
            else None
        )
        sp_eff = effect_size(sp_x[dev_mask], sp_x[ref_mask]) if sp_x is not None else None
        op_eff = effect_size(op_x[dev_mask], op_x[ref_mask]) if op_x is not None else None
        pv_held = pv.effect is None or abs(pv.effect.z) < 2
        sp_moved = sp_eff is not None and abs(sp_eff.z) >= 3 and abs(sp_eff.delta) > 1e-6
        op_moved = op_eff is not None and abs(op_eff.z) >= 3
        saturated = False
        if op_x is not None and isinstance(op_tag, Tag):
            lo = op_tag.range_low if op_tag.range_low is not None else np.nanmin(op_x)
            hi = op_tag.range_high if op_tag.range_high is not None else np.nanmax(op_x)
            v = op_x[dev_mask]
            v = v[np.isfinite(v)]
            margin = 0.01 * (hi - lo)
            saturated = bool(v.size) and np.mean((v <= lo + margin) | (v >= hi - margin)) >= 0.5

        if sp_moved and sp_x is not None and isinstance(sp_tag, Tag):
            tests, eff = test_series(sp_x, c.path)
            w0, w1 = window_at(c.path)
            scored.append(
                Scored(
                    Hypothesis(
                        rank=0,
                        kind=HypothesisKind.setpoint_change,
                        cause_node=loop.id,
                        cause_tag=sp_tag.tag,
                        direction="high" if sp_eff.delta > 0 else "low",
                        magnitude=round(sp_eff.delta, 4),
                        unit=sp_tag.unit_of_measure,
                        window_start=w0,
                        window_end=w1,
                        graph_path=c.path.path,
                        dead_time_min=c.path.dead_time_min,
                        tests=tests,
                        score=round(_score(tests), 3),
                        confidence="low",
                        summary="",
                    ),
                    eff,
                    sp_x,
                )
            )
            series[sp_tag.tag] = sp_x
            pv.hypothesis.score = round(pv.hypothesis.score * 0.5, 3)
            ruled_out.append(
                RuledOut(cause=c.tag.tag, reason=f"follows the {loop.id} setpoint change")
            )
        elif saturated and not pv_held:
            pv.hypothesis.kind = HypothesisKind.lost_control
            pv.hypothesis.cause_node = loop.id
            pv.hypothesis.score = round(max(pv.hypothesis.score, 0.5), 3)
            evidence.append(
                EvidenceItem(
                    id="",
                    kind="loop",
                    title=f"{loop.id} output saturated while {c.tag.tag} deviated",
                    data={"loop": loop.id, "op_tag": op_tag.tag if op_tag else None},
                )
            )
            pv.hypothesis.evidence.append(evidence[-1].title)
        elif pv_held and op_moved:
            pv.hypothesis.score = round(pv.hypothesis.score * 0.2, 3)
            reason = (
                f"held at setpoint by {loop.id} while its output moved "
                f"{_fmt(op_eff.delta)} ({op_eff.z:+.1f} reference scales)"
            )
            ruled_out.append(RuledOut(cause=c.tag.tag, reason=reason))
            item = EvidenceItem(
                id="",
                kind="loop",
                title=f"{loop.id} absorbed a disturbance: {c.tag.tag} held, output moved "
                f"{_fmt(op_eff.delta)}",
                data={"loop": loop.id, "op_delta": round(op_eff.delta, 4), "op_z": op_eff.z},
            )
            evidence.append(item)
            upstream = [
                sc
                for sc in scored
                if sc.hypothesis.kind == "process"
                and sc.hypothesis.cause_tag != c.tag.tag
                and c.path.node in sc.hypothesis.graph_path
            ]
            for sc in upstream:
                sc.hypothesis.evidence.append(item.title)
            if not upstream:
                w0, w1 = window_at(c.path)
                scored.append(
                    Scored(
                        Hypothesis(
                            rank=0,
                            kind=HypothesisKind.unmeasured_disturbance,
                            cause_node=c.path.node,
                            direction="changed",
                            window_start=w0,
                            window_end=w1,
                            graph_path=c.path.path,
                            dead_time_min=c.path.dead_time_min,
                            tests=[],
                            score=round(0.3 * op_eff.strength, 3),
                            confidence="low",
                            evidence=[item.title],
                            summary=f"something upstream of {loop.id} disturbed "
                            f"{c.path.node}; the loop absorbed it",
                        )
                    )
                )

    # Explain away: a downstream deviation predicted by an upstream one on the same path.
    strong = [
        sc
        for sc in scored
        if sc.hypothesis.kind == "process" and sc.effect is not None and sc.effect.strength >= 0.5
    ]
    for down in strong:
        for up in strong:
            if up is down or down.hypothesis.cause_node == up.hypothesis.cause_node:
                continue
            if down.hypothesis.cause_node not in up.hypothesis.graph_path:
                continue
            frac = explained_fraction(
                up.series[ref_mask], down.series[ref_mask], up.effect.delta, down.effect.delta
            )
            if frac is None or frac < 0.6:
                continue
            tag_d, tag_u = down.hypothesis.cause_tag, up.hypothesis.cause_tag
            reason = (
                f"its shift of {_fmt(down.effect.delta)} is {min(frac, 9.99):.0%} predicted "
                f"from {tag_u} (regression on the reference lots)"
            )
            down.hypothesis.score = round(down.hypothesis.score * 0.3, 3)
            up.hypothesis.ruled_out.append(RuledOut(cause=tag_d, reason=reason))
            ruled_out.append(RuledOut(cause=tag_d, reason=reason))
            evidence.append(
                EvidenceItem(
                    id="",
                    kind="explain_away",
                    title=f"{tag_d} explained by {tag_u}",
                    data={"explained": round(frac, 3), "upstream": tag_u, "downstream": tag_d},
                )
            )
            break

    for sc in scored:
        h = sc.hypothesis
        if h.kind == "process" and (sc.effect is None or sc.effect.strength == 0):
            if not any(r.cause == h.cause_tag for r in ruled_out):
                ruled_out.append(
                    RuledOut(cause=h.cause_tag or h.cause_node, reason="did not move in the lot")
                )

    ranked = sorted(scored, key=lambda sc: (-sc.hypothesis.score, sc.hypothesis.cause_node))
    kept = [
        sc
        for sc in ranked
        if not (
            sc.hypothesis.kind == "process"
            and any(r.cause == sc.hypothesis.cause_tag for r in ruled_out)
        )
    ][: s.top]
    labels = _confidence([sc.hypothesis.score for sc in kept])
    hypotheses = []
    for rank, (sc, label) in enumerate(zip(kept, labels, strict=True), start=1):
        h = sc.hypothesis
        h.rank, h.confidence = rank, label
        if not h.summary:
            h.summary = _summary(h, q)
        hypotheses.append(h)

    event = QualityEvent(
        lot=lot,
        property=q.property,
        quality_node=q.id,
        value=value,
        spec_low=q.spec_low,
        spec_high=q.spec_high,
        unit=q.unit_of_measure,
        direction="low" if quality_sign < 0 else "high",
        sampled_at=sampled_at,
        sample_point=sample_point,
        sampled_node=sampled_node,
        window_start=ev_start,
        window_end=ev_end,
        onset=_dt(onset_us) if onset_us is not None else None,
        proxy_tag=proxy_tag.tag if proxy_tag else None,
    )
    lab = [(r["sampled_at"], float(r["value"])) for r in prop_rows.iter_rows(named=True)]
    step(7, 7, "done")
    return Investigation(
        event=event,
        regime=event_regime,
        reference=reference,
        grid=grid,
        deviation_start_us=dev_start,
        candidates=len(candidates),
        hypotheses=hypotheses,
        ruled_out=ruled_out,
        evidence=evidence,
        health=[f for f in health if f.severity != Severity.info],
        notes=notes,
        series=series,
        proxy=proxy,
        lab=lab,
    )


def _summary(h: Hypothesis, q: Quality) -> str:
    where = f"{h.cause_tag} ({h.cause_node})" if h.cause_tag else h.cause_node
    moved = (
        f" {_fmt(abs(h.magnitude))}{' ' + h.unit if h.unit else ''} {h.direction}"
        if h.magnitude is not None
        else ""
    )
    when = f" from {h.window_start:%Y-%m-%d %H:%M}" if h.window_start else ""
    lead = {
        HypothesisKind.process: f"{where} ran{moved}{when}",
        HypothesisKind.setpoint_change: f"The setpoint {where} was changed{moved}{when}",
        HypothesisKind.lost_control: f"{h.cause_node} lost control: {h.cause_tag} ran{moved}",
    }.get(h.kind, where)
    onset = next((t for t in h.tests if t.name == TestName.onset and t.value is not None), None)
    tail = f", {h.dead_time_min:g} min upstream of the sample point"
    if onset is not None:
        tail += (
            f"; {q.property} moved {abs(onset.value):.0f} min "
            + ("after the expected arrival" if onset.value < 0 else "before the expected arrival")
            if abs(onset.value) >= 1
            else f"; {q.property} moved when it arrived"
        )
    return lead + tail + "."
