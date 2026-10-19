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
from plantgraph import EdgeType, Equipment, Loop, PlantGraph, Quality, Tag

from penguineer.core.errors import NotEnoughData, UsageError, ValidationFailed
from penguineer.engines.align import (
    Grid,
    NodePath,
    aligned,
    measuring_tags,
    median_period_us,
    upstream_paths,
)
from penguineer.engines.calibration import Calibration, default_calibration
from penguineer.engines.evidence import (
    Effect,
    change_point,
    effect_size,
    explained_fraction,
    lag_correlation,
    level_spread,
    onset_strength,
    smooth,
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

SMOOTH_MIN = 30  # minutes of moving mean before the effect test
SETTLE_MIN = 20  # minutes a loop is given to bring its PV back after its output steps
UPSTREAM_STRENGTH = 0.5  # effect strength at which a moved upstream tag explains a loop's move
UPSTREAM_EXPLAINS: float | None = 0.3  # share of the loop output's move it must predict
CYCLE_R2 = 0.3  # share of a tag's variance the day cycle must explain before it is removed
CYCLE_HARMONICS = 2  # Fourier terms of the day cycle fit
CYCLE_DAYS = 7  # cycles of earlier same-regime lots the day cycle is fitted on
REGIME_SCORE = 1.0  # a regime change in the lot or the one before it
REGIME_UNSETTLED_SCORE = 0.6  # a regime change after which no lot has been in spec
LATER_LOTS = 60  # lab results after the lot searched for a reference when none come before


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
    lab_history: int = 12  # earlier lab results of the same regime for the sample-level test
    cycle_hours: float | None = 24.0  # day cycle removed from tags that follow it; None: off
    calibration: Calibration = field(default_factory=default_calibration)


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
    calibration: Calibration = field(default_factory=default_calibration)


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


def _regime_change(
    raw: dict[str, tuple[np.ndarray, np.ndarray]],
    regime: dict[str, float],
    previous: dict[str, float],
    end: int,
) -> tuple[int, int, dict[str, tuple[float, float]]] | None:
    """When the plant last left the ``previous`` regime for ``regime`` before ``end``.

    Returns (start, settled, changes): the last time a changed mode tag still read its old
    value, the last time one did not yet read its new value, and old and new value by tag.
    """
    changes = {k: (previous[k], v) for k, v in regime.items() if k in previous and previous[k] != v}
    if not changes:
        return None
    settled, starts = None, []
    for tag, (old, new) in changes.items():
        t, x = raw[tag]
        before = t < end
        t, x = t[before], np.round(x[before], 6)
        off = np.flatnonzero(x != new)
        if off.size == 0:
            continue
        last_off = int(t[off[-1]])
        settled = last_off if settled is None else max(settled, last_off)
        at_old = np.flatnonzero((x == old) & (t <= last_off))
        starts.append(int(t[at_old[-1]]) if at_old.size else last_off)
    if settled is None:
        return None
    return min(starts), settled, changes


def _cycle_basis(times_us: np.ndarray, period_us: float) -> np.ndarray:
    phase = 2 * np.pi * (times_us % period_us) / period_us
    cols = [np.ones_like(phase)]
    for k in range(1, CYCLE_HARMONICS + 1):
        cols += [np.cos(k * phase), np.sin(k * phase)]
    return np.column_stack(cols)


def _fit_cycle(t: np.ndarray, v: np.ndarray, period_us: float) -> np.ndarray | None:
    covered = np.unique(((t % period_us) / period_us * 24).astype(int)).size
    if t.size < 20 or covered < 18 or v.std() == 0:
        return None
    coef, *_ = np.linalg.lstsq(_cycle_basis(t, period_us), v, rcond=None)
    return coef


def day_cycle(
    x: np.ndarray, times_us: np.ndarray, fit_mask: np.ndarray, period_us: float
) -> tuple[np.ndarray, float] | None:
    """The day cycle of ``x``, fitted by least squares on ``fit_mask``, and the share of the
    variance there it explains; None when the fit points span less than 1.5 cycles or the
    cycle does not repeat (fitted on each half of them, the two shapes differ).

    The cycle is a few Fourier terms of the given period, without the constant, so
    subtracting it leaves each value relative to the tag's usual level at that time of day.
    """
    ok = fit_mask & np.isfinite(x)
    t, v = times_us[ok], x[ok]
    if t.size < 40 or (t[-1] - t[0]) < 1.5 * period_us:
        return None
    coef = _fit_cycle(t, v, period_us)
    half = t < t[0] + (t[-1] - t[0]) / 2
    halves = [_fit_cycle(t[m], v[m], period_us) for m in (half, ~half)]
    if coef is None or any(c is None for c in halves):
        return None
    one = _cycle_basis(np.linspace(0, period_us, 48, endpoint=False), period_us)[:, 1:]
    a, b = (one @ c[1:] for c in halves)
    if a.std() == 0 or b.std() == 0 or np.corrcoef(a, b)[0, 1] < 0.7:
        return None
    basis = _cycle_basis(t, period_us)
    r2 = 1.0 - float(np.var(v - basis @ coef) / np.var(v))
    return _cycle_basis(times_us, period_us)[:, 1:] @ coef[1:], r2


# ---------------------------------------------------------------- scoring


def _score(tests: list[TestResult], cal: Calibration) -> float:
    weights = {
        TestName.effect_size: cal.effect,
        TestName.onset: cal.onset,
        TestName.lag_correlation: cal.lag,
    }
    ran = [(weights[t.name], t.strength) for t in tests if t.strength is not None]
    if not ran:
        return 0.0
    total = sum(w for w, _ in ran)
    if total <= 0:
        return 0.0
    value = sum(w * s for w, s in ran) / total
    effect = next((t.strength for t in tests if t.name == TestName.effect_size), None)
    return float(value if effect else 0.5 * value)


def _confidence(scores: list[float], cal: Calibration) -> list[str]:
    out = []
    for i, s in enumerate(scores):
        label = "high" if s >= cal.high else "medium" if s >= cal.medium else "low"
        if i == 0 and label == "high" and len(scores) > 1 and s - scores[1] < cal.margin:
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
    cal = s.calibration
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
        # The sample holds what the plant made last, so the lot counts as the regime it ended in.
        for tag in event_regime:
            t, x = raw[tag]
            last = x[(t < _us(ev_end)) & np.isfinite(x)]
            event_regime[tag] = float(np.round(last[-1], 6))
        notes.append("the lot spans a regime change; it counts as the regime it ended in")
    cycle_us = s.cycle_hours * 3600 * _US if s.cycle_hours else None
    lookback = _us(ev_start) - int(CYCLE_DAYS * cycle_us) if cycle_us else _us(ev_start)
    # Earlier lab results of the same regime, newest first, and the regime the plant came from
    # (the newest earlier lot of another regime).
    history: list[tuple[str, datetime, datetime, datetime, float]] = []
    cycle_lots: list[tuple[datetime, datetime]] = []
    previous: dict[str, float] | None = None
    skipped_regime = 0
    earlier = prop_rows.filter(pl.col("sampled_at") < ev_start).reverse()
    for r in earlier.iter_rows(named=True):
        full = len(history) >= s.lab_history + s.reference_lots + 6
        if full and _us(r["sampled_at"]) < lookback:
            break
        if r["lot"] in (None, lot) or r["value"] is None:
            continue
        start, end = _window(prop_rows, r["lot"], r["sampled_at"], lots, s)
        label, mix = _regime(raw, mode_tags, _us(start), _us(end))
        if mix or label != event_regime:
            skipped_regime += 1
            if previous is None and not mix:
                previous = label
            continue
        if _deviation(q, float(r["value"])) == 0 and _us(start) >= lookback:
            cycle_lots.append((start, end))
        if not full:
            history.append((r["lot"], start, end, r["sampled_at"], float(r["value"])))
    if skipped_regime:
        notes.append(f"{skipped_regime} earlier lots skipped: different regime")

    # A regime change after which no lot was in spec: in this lot or the one before, or
    # earlier when the plant has not made in-spec product in the new regime since.
    length = _us(ev_end) - _us(ev_start)
    change = _regime_change(raw, event_regime, previous, _us(ev_end)) if previous else None
    regime_score = 0.0
    if change is not None:
        since = prop_rows.filter(
            (pl.col("sampled_at") > _dt(change[1])) & (pl.col("sampled_at") < sampled_at)
        )["value"].drop_nulls()
        if any(_deviation(q, float(v)) == 0 for v in since):
            pass  # the plant made in-spec product in this regime since the change
        elif change[1] >= _us(ev_start) - length:
            regime_score = REGIME_SCORE
        else:
            regime_score = REGIME_UNSETTLED_SCORE

    step_us_guess = max((median_period_us(raw[k][0]) for k in event_regime), default=_US)

    def regime_text(label: dict[str, float]) -> str:
        return ", ".join(f"{k}={_fmt(v)}" for k, v in label.items()) or "one regime"

    def change_text() -> str:
        start, settled, moved = change
        what = ", ".join(f"{k} {_fmt(a)} to {_fmt(b)}" for k, (a, b) in sorted(moved.items()))
        hours = (_us(sampled_at) - settled) / 3600 / _US
        when = (
            f"at {_dt(settled):%Y-%m-%d %H:%M}"
            if settled - start <= step_us_guess
            else f"from {_dt(start):%Y-%m-%d %H:%M} to {_dt(settled):%H:%M}"
        )
        return f"the regime changed {when} ({what}), {hours:.1f} h before the sample"

    # Where the quality started to move: the lot before this one by default, or earlier when
    # the lab results step before the first off-spec lot (lots already affected but in spec).
    search_start = _us(ev_start) - length
    off = _deviation(q, value) > 0
    sign = -1.0 if q.spec_low is not None and value < q.spec_low else 1.0
    seq = np.array([h[4] for h in reversed(history)] + [value])
    if off and seq.size >= 6:
        cp = change_point(seq, min_segment=2)
        if cp is not None and abs(cp.z) >= 2 and np.sign(cp.shift) == sign:
            first = history[len(history) - 1 - cp.index] if cp.index < len(history) else None
            if first is not None and _us(first[1]) < search_start:
                search_start = _us(first[1])
                notes.append(
                    f"{q.property} started to move with lot {first[0]}, before it left spec"
                )
    search_start = max(search_start, _us(ev_end) - int(s.max_window_hours * 3600 * _US))

    # Reference: the most recent in-spec lots that end before the quality started to move.
    reference = [
        ReferenceWindow(lot=h[0], start=h[1], end=h[2], value=h[4])
        for h in history
        if _us(h[2]) <= search_start and _deviation(q, h[4]) == 0
    ][: s.reference_lots]
    lab_hist = [(_us(h[3]), h[4]) for h in history if _us(h[2]) <= search_start][: s.lab_history]
    if not reference:
        # None before (the first run of a grade in the export): the nearest in-spec lots of
        # the same regime after the lot, which an investigation after the fact often has.
        # The day cycle is then fitted on the lots after it too.
        horizon = _dt(_us(sampled_at) + int(CYCLE_DAYS * cycle_us)) if cycle_us else None
        later = prop_rows.filter(pl.col("sampled_at") > sampled_at)
        for k, r in enumerate(later.iter_rows(named=True)):
            if len(reference) >= s.reference_lots and (
                horizon is None or r["sampled_at"] > horizon
            ):
                break
            if k >= LATER_LOTS and not reference:
                break
            if r["lot"] in (None, lot) or r["value"] is None:
                continue
            if _deviation(q, float(r["value"])) > 0:
                continue
            start, end = _window(prop_rows, r["lot"], r["sampled_at"], lots, s)
            label, mix = _regime(raw, mode_tags, _us(start), _us(end))
            if mix or label != event_regime:
                continue
            cycle_lots.append((start, end))
            if len(reference) < s.reference_lots:
                reference.append(
                    ReferenceWindow(lot=r["lot"], start=start, end=end, value=float(r["value"]))
                )
        if reference:
            notes.append(
                f"no in-spec lot of {q.property} before {lot} in the same regime: compared with "
                f"the in-spec lots after it ({', '.join(w.lot for w in reference)})"
            )
    if not reference:
        raise NotEnoughData(
            f"no in-spec lot of {q.property} in the same regime as {lot} "
            f"({regime_text(event_regime)}), so there is nothing to compare the lot with"
            + (f"; {change_text()}, the likely cause" if regime_score else "")
        )
    if regime_score:
        notes.append(change_text())

    # 4. Align everything on one grid in sampled-node time.
    step(3, 7, "align")
    periods = [median_period_us(raw[c.tag.tag][0]) for c in candidates]
    if proxy_tag is not None:
        periods.append(median_period_us(raw[proxy_tag.tag][0]))
    step_us = int(s.step_s * _US) if s.step_s else max(max(periods), _US)
    grid_start = min([_us(w.start) for w in reference] + [search_start])
    grid_end = max([_us(w.end) for w in reference] + [_us(ev_end)])
    earlier_lots = [(h[1], h[2]) for h in history if _us(h[2]) <= search_start][: s.lab_history]
    if earlier_lots:
        grid_start = min(grid_start, _us(earlier_lots[-1][0]))
    if proxy_tag is None and lab_hist:
        grid_start = min(grid_start, lab_hist[-1][0] - 3600 * _US)
    # The day cycle is fitted on in-spec lots of the regime, so the grid reaches back to them.
    cycle_lots = [(a, b) for a, b in cycle_lots if _us(b) <= search_start or _us(a) >= _us(ev_end)]
    if cycle_us is not None and cycle_lots:
        grid_start = min(grid_start, min(_us(a) for a, _ in cycle_lots))
        grid_end = max(grid_end, max(_us(b) for _, b in cycle_lots))
    grid = Grid.spanning(grid_start, grid_end, step_us)
    event_mask = grid.mask(_us(ev_start), _us(ev_end))
    ref_mask = np.zeros(grid.n, dtype=bool)
    for w in reference:
        ref_mask |= grid.mask(_us(w.start), _us(w.end))
    cycle_mask = np.zeros(grid.n, dtype=bool)
    for a, b in cycle_lots:
        cycle_mask |= grid.mask(_us(a), _us(b))
    search_mask = grid.mask(search_start, _us(ev_end))
    lot_masks = [grid.mask(_us(a), _us(b)) for a, b in earlier_lots]
    search_times = grid.times[search_mask]

    # Tags that follow the day cycle are compared with their usual level at that time of day:
    # power, feeds and coolers that all rise by day otherwise all look moved whenever the lot
    # and its reference lots fall at different times of day.
    cycled: dict[str, float] = {}

    def without_cycle(tag: str, x: np.ndarray, dead_time_min: float) -> np.ndarray:
        if cycle_us is None:
            return x
        own_clock = grid.times - int(dead_time_min * 60 * _US)
        fit = day_cycle(x, own_clock, cycle_mask, cycle_us)
        if fit is None or fit[1] < CYCLE_R2:
            return x
        cycled[tag] = max(cycled.get(tag, 0.0), fit[1])
        return x - fit[0]

    proxy_raw = aligned(*raw[proxy_tag.tag], grid, 0.0) if proxy_tag is not None else None
    proxy = without_cycle(proxy_tag.tag, proxy_raw, 0.0) if proxy_raw is not None else None
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
        & (pl.col("timestamp") <= _dt(grid_end))
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

    lab_t = np.array([t for t, _ in lab_hist], dtype=np.int64)
    lab_v = np.array([v for _, v in lab_hist])

    def late_effect(x: np.ndarray) -> Effect | None:
        """Without a quality onset: the tag's own step, when it lands where the quality moved."""
        cp = change_point(x[search_mask])
        if cp is None or abs(cp.z) < 4:
            return None
        t = int(search_times[cp.index])
        after = grid.mask(t, _us(ev_end))
        if after.sum() < 3:
            return None
        return effect_size(x[after], x[ref_mask], level_spread(x, lot_masks))

    def lab_correlation(x: np.ndarray) -> tuple[float, int] | None:
        """Correlation of the tag, as the sampled material met it, with earlier lab results."""
        if lab_t.size < 6:
            return None
        width = max(step_us, 10 * 60 * _US)
        vals = []
        for t in lab_t:
            i0, i1 = np.searchsorted(grid.times, [t - width, t], side="right")
            seg = x[i0:i1]
            seg = seg[np.isfinite(seg)]
            vals.append(seg.mean() if seg.size else np.nan)
        a = np.array(vals)
        ok = np.isfinite(a)
        if ok.sum() < 6 or a[ok].std() == 0 or lab_v[ok].std() == 0:
            return None
        return float(np.corrcoef(a[ok], lab_v[ok])[0, 1]), int(ok.sum())

    # The effect test compares levels, so white measurement noise is averaged out first.
    smooth_n = max(int(SMOOTH_MIN * 60 * _US / step_us), 1)

    def effect_of(x: np.ndarray) -> Effect | None:
        x = smooth(x, smooth_n)
        eff = effect_size(x[dev_mask], x[ref_mask], level_spread(x, lot_masks))
        if onset_us is None:
            late = late_effect(x)
            if late is not None and (eff is None or abs(late.z) > abs(eff.z)):
                eff = late
        return eff

    def test_series(x: np.ndarray, path: NodePath) -> tuple[list[TestResult], Effect | None]:
        tests: list[TestResult] = []
        eff = effect_of(x)
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
        if eff is None or (proxy is None and lab_t.size < 6):
            tests.append(TestResult(name=TestName.lag_correlation, strength=None, detail="not run"))
        else:
            if proxy is not None:
                rc, over = lag_correlation(x[ref_mask], proxy[ref_mask]), "reference points"
            else:
                rc, over = lab_correlation(x), "earlier lab results"
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
                        detail=f"r = {r:+.2f} over {n} {over}"
                        + ("" if consistent else ", sign does not fit the deviation"),
                    )
                )
        return tests, eff

    def window_at(path: NodePath) -> tuple[datetime, datetime]:
        shift = path.dead_time_min * 60 * _US
        return _dt(dev_start - shift), _dt(_us(ev_end) - shift)

    for k, c in enumerate(candidates):
        series[c.tag.tag] = aligned(*raw[c.tag.tag], grid, c.path.dead_time_min)
        x = without_cycle(c.tag.tag, series[c.tag.tag], c.path.dead_time_min)
        tests, eff = test_series(x, c.path)
        w0, w1 = window_at(c.path)
        faults = findings_in(c.tag.tag, _us(w0), _us(w1))
        score = _score(tests, cal) * (0.5 if faults else 1.0)
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
                        score=cal.fault_critical
                        if f.severity == Severity.critical
                        else cal.fault_warning,
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

    def held_by_loop(pv_x: np.ndarray, sp_x: np.ndarray | None, op_x: np.ndarray | None) -> bool:
        """Whether a loop held its PV in the lot, apart from the transient after its output stepped.

        A controlled PV barely moves from lot to lot, so a short excursion while the loop
        recovers can score as a large effect in reference scales. Held means: once the loop has
        settled, the median control error sits within the PV's sample-to-sample noise of the
        reference's.
        """
        err = pv_x - sp_x if sp_x is not None else pv_x
        ref = err[ref_mask]
        ref = ref[np.isfinite(ref)]
        if ref.size < 10:
            return False
        noise = 1.4826 * float(np.median(np.abs(np.diff(ref)))) / np.sqrt(2)
        m = dev_mask.copy()
        cp = change_point(op_x[search_mask]) if op_x is not None else None
        if cp is not None and abs(cp.z) >= 4:
            settled = grid.times >= int(search_times[cp.index]) + SETTLE_MIN * 60 * _US
            if (m & settled).sum() >= 3:
                m &= settled
        ev = err[m]
        ev = ev[np.isfinite(ev)]
        if ev.size < 3 or noise == 0:
            return False
        return abs(float(np.median(ev)) - float(np.median(ref))) < noise

    def explains_output(up: Scored, op_x: np.ndarray | None, op_eff: Effect | None) -> bool:
        """Whether an upstream tag's shift predicts the loop output's (regression on the reference).

        A tag that moved at the same time but whose swings the output never followed in the
        reference does not explain the output's move.
        """
        if UPSTREAM_EXPLAINS is None or op_x is None or op_eff is None or up.series is None:
            return True
        frac = explained_fraction(
            up.series[ref_mask], op_x[ref_mask], up.effect.delta, op_eff.delta
        )
        return frac is None or frac >= UPSTREAM_EXPLAINS

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
        sp_raw = (
            aligned(*raw[sp_tag.tag], grid, c.path.dead_time_min)
            if (isinstance(sp_tag, Tag) and sp_tag.tag in raw)
            else None
        )
        op_raw = (
            aligned(*raw[op_tag.tag], grid, c.path.dead_time_min)
            if (isinstance(op_tag, Tag) and op_tag.tag in raw)
            else None
        )
        sp_x = (
            without_cycle(sp_tag.tag, sp_raw, c.path.dead_time_min) if sp_raw is not None else None
        )
        op_x = (
            without_cycle(op_tag.tag, op_raw, c.path.dead_time_min) if op_raw is not None else None
        )
        sp_eff = effect_of(sp_x) if sp_x is not None else None
        op_eff = effect_of(op_x) if op_x is not None else None
        pv_held = pv.effect is None or abs(pv.effect.z) < 2 or held_by_loop(pv.series, sp_x, op_x)
        sp_moved = sp_eff is not None and abs(sp_eff.z) >= 3 and abs(sp_eff.delta) > 1e-6
        op_moved = op_eff is not None and abs(op_eff.z) >= 3
        saturated = False
        if op_raw is not None and isinstance(op_tag, Tag):
            lo = op_tag.range_low if op_tag.range_low is not None else np.nanmin(op_raw)
            hi = op_tag.range_high if op_tag.range_high is not None else np.nanmax(op_raw)
            v = op_raw[search_mask]
            margin = 0.01 * (hi - lo)
            at_limit = np.isfinite(v) & ((v <= lo + margin) | (v >= hi - margin))
            # Saturated: at a limit for half an hour on end while the quality moved.
            need = max(int(30 * 60 * _US / step_us), 3)
            run = longest = 0
            for flag in at_limit:
                run = run + 1 if flag else 0
                longest = max(longest, run)
            saturated = longest >= need

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
                        score=round(_score(tests, cal), 3),
                        confidence="low",
                        summary="",
                    ),
                    eff,
                    sp_x,
                )
            )
            series[sp_tag.tag] = sp_raw
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
                and sc.effect is not None
                and sc.effect.strength >= UPSTREAM_STRENGTH
                and explains_output(sc, op_x, op_eff)
            ]
            for sc in upstream:
                sc.hypothesis.evidence.append(item.title)
            if not upstream and op_x is not None:
                w0, w1 = window_at(c.path)
                op_tests, op_test_eff = test_series(op_x, c.path)
                op_score = _score(op_tests, cal)
                series[op_tag.tag] = op_raw
                scored.append(
                    Scored(
                        Hypothesis(
                            rank=0,
                            kind=HypothesisKind.measurement_fault,
                            cause_node=c.tag.id,
                            cause_tag=c.tag.tag,
                            direction="changed",
                            window_start=w0,
                            window_end=w1,
                            graph_path=c.path.path,
                            dead_time_min=c.path.dead_time_min,
                            tests=op_tests,
                            score=round(cal.pv_bias * op_score, 3),
                            confidence="low",
                            evidence=[item.title],
                            summary=f"{c.tag.tag} may read wrong: {loop.id} held it at its "
                            f"setpoint by moving its output {_fmt(op_eff.delta)}, nothing "
                            f"measured upstream moved, and {q.property} moved, so the true "
                            "value may have left the setpoint",
                        ),
                        op_test_eff,
                        op_x,
                    )
                )
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
                            score=round(cal.loop_disturbance * op_score, 3),
                            confidence="low",
                            evidence=[item.title],
                            summary=f"something upstream of {loop.id} disturbed "
                            f"{c.path.node}; the loop absorbed it",
                        )
                    )
                )

    # A regime change (a grade or recipe change) in the lot or the one before, or one the
    # plant has not made in-spec product since. Tags that stepped with it are part of it.
    if regime_score:
        start, settled, moved_tags = change
        for sc in [sc for sc in scored if sc.hypothesis.kind in ("setpoint_change", "process")]:
            h = sc.hypothesis
            cp = change_point(sc.series[search_mask]) if sc.series is not None else None
            tol = 3600 * _US
            at = int(search_times[cp.index]) if cp is not None and abs(cp.z) >= 4 else None
            if at is not None:
                at -= int(h.dead_time_min * 60 * _US)  # back on the tag's own clock
            # A setpoint compared across a change inside the search window moved with it.
            spans = h.kind == "setpoint_change" and settled >= search_start
            if (
                spans
                or h.cause_tag in moved_tags
                or (at is not None and start - tol <= at <= settled + tol)
            ):
                if h.kind == "setpoint_change":
                    scored.remove(sc)
                if not any(r.cause == h.cause_tag for r in ruled_out):
                    ruled_out.append(
                        RuledOut(cause=h.cause_tag, reason="moved with the regime change")
                    )
        first = sorted(moved_tags)[0]
        node = next((n.id for n in graph.nodes if isinstance(n, Tag) and n.tag == first), first)
        series[first] = aligned(*raw[first], grid, 0.0)
        tail = (
            "; the lot was made during it or right after it"
            if regime_score >= REGIME_SCORE
            else f"; no lot of {q.property} has been in spec in this regime since"
        )
        scored.append(
            Scored(
                Hypothesis(
                    rank=0,
                    kind=HypothesisKind.regime_change,
                    cause_node=node,
                    cause_tag=first,
                    direction="changed",
                    window_start=_dt(start),
                    window_end=_dt(settled),
                    graph_path=[node],
                    dead_time_min=0.0,
                    tests=[],
                    score=regime_score,
                    confidence="low",
                    summary=change_text()[0].upper() + change_text()[1:] + tail + ".",
                )
            )
        )
    if cycled:
        period = f"{s.cycle_hours:.4g} h"
        notes.append(
            f"{', '.join(sorted(cycled))} follow the day cycle ({period}): each is compared "
            "with its usual level at that time of day"
        )

    # Nothing measured moved clearly: the change is in something unmeasured near the sample.
    moved = [sc.effect.strength for sc in scored if sc.effect is not None]
    near = sorted(
        (p for n, p in paths.items() if n != sampled_node and isinstance(by_id.get(n), Equipment)),
        key=lambda p: (len(p.path), p.node),
    )
    if near and max(moved, default=0.0) < cal.hidden_below:
        p0 = near[0]
        w0, w1 = window_at(p0)
        scored.append(
            Scored(
                Hypothesis(
                    rank=0,
                    kind=HypothesisKind.unmeasured_disturbance,
                    cause_node=p0.node,
                    direction="changed",
                    window_start=w0,
                    window_end=w1,
                    graph_path=p0.path,
                    dead_time_min=p0.dead_time_min,
                    tests=[],
                    score=cal.hidden,
                    confidence="low",
                    summary=f"no measured tag upstream moved enough to explain {q.property}; "
                    f"something unmeasured changed in {p0.node} or just before the sample "
                    "(a catalyst, a fouled surface, a feed component nobody analyzes)",
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
    labels = _confidence([sc.hypothesis.score for sc in kept], cal)
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
        proxy=proxy_raw,
        lab=lab,
        calibration=cal,
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
