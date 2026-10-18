"""The actual lag from a cause to an effect, fitted from data with three transport models.

A change at the cause reaches the effect after the material has travelled between them. How it
travels decides what the delay looks like:

``fixed``
    A constant dead time: effect(t) follows cause(t - θ). Right when the throughput is steady.
``flow``
    Plug flow through a volume V: the material at the effect now passed the cause when V less
    had flowed through the line, so the delay is V over the throughput and changes with it.
``mixing``
    Plug flow through a dead volume, then a well-mixed volume whose response is first order:
    the material is not only delayed but spread out. With a flow tag both volumes are in the
    flow's units times hours (m3 for a flow in m3/h); without one they are a dead time and a
    time constant in minutes.

The models work on one coordinate: the time, or the cumulative throughput ("material time",
the volume that has passed since the start). The cause is resampled onto an even grid of that
coordinate, mixed with a first-order filter on that grid and shifted along it, then read back at
the effect's times. Each model's parameters are the ones whose prediction correlates best with
the effect:

* a continuous effect (an online tag) is compared on an even time grid after prewhitening both
  series with one AR model of the cause, so that slow drifts do not pass for a link; a peak
  counts when |corr|·√n reaches ``z_threshold``.
* a lab effect is compared at its sample times, on levels. Prewhitening needs an even grid, so
  the chance level comes from the same scan run on the cause rotated by a random offset
  (``n_null`` times per model): the peak counts when it beats every rotated scan of every
  model.

Each interval is the run of parameter values whose |corr| is within two standard errors of the
peak's. The reported model is the fixed or the flow model, whichever correlates more, unless the
mixing model's extra parameter explains significantly more of the effect (an F test on r²).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from pydantic import BaseModel, Field

from penguineer.engines.lags import ar_filter, fit_ar, resample

_US = 1_000_000


class ModelFit(BaseModel):
    model: str = Field(description="fixed, flow or mixing")
    coordinate: str = Field(description="time (minutes) or volume (flow unit × hours)")
    dead: float = Field(description="Dead time (minutes) or dead volume")
    dead_low: float
    dead_high: float
    mixing: float = Field(0.0, description="Mixing time constant (minutes) or mixed volume")
    mixing_low: float = 0.0
    mixing_high: float = 0.0
    residence_min: float = Field(
        description="Mean delay in minutes at the mean flow: dead plus mixing"
    )
    corr: float = Field(description="Correlation of the model's prediction with the effect")
    strength: float = Field(
        description="|corr|·√n for an online effect; |corr| over the chance level for a lab one"
    )
    significant: bool


@dataclass
class Profile:
    """One model's scan, kept for the evidence plots."""

    model: str
    coordinate: str
    dead: np.ndarray  # scanned dead values, in the coordinate's units
    mixing: np.ndarray  # scanned mixing values (one 0 for the delay-only models)
    corr: np.ndarray  # shape (len(mixing), len(dead))
    chance: float | None = None  # |corr| that rotated causes reach (lab effects)


@dataclass
class LagFit:
    fits: list[ModelFit]
    best: str | None
    n: int
    step_s: float
    mean_flow: float | None
    effect_kind: str  # "online" or "lab"
    profiles: list[Profile] = field(default_factory=list)
    # For the fit plot: the effect, and each model's (times, effect, prediction) over the points
    # it was scored on (a model in volume starts where the longest dead volume has passed).
    times: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    effect: np.ndarray = field(default_factory=lambda: np.empty(0))
    predictions: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = field(default_factory=dict)


@dataclass
class _Coord:
    """A coordinate along the line: its value at each grid time, and an even grid of it."""

    name: str
    at_times: np.ndarray  # coordinate at each time-grid point
    grid: np.ndarray  # even grid in the coordinate
    cause: np.ndarray  # the cause on that grid
    per_min: float  # coordinate units per minute at the mean flow

    @property
    def step(self) -> float:
        return float(self.grid[1] - self.grid[0])


def first_order(x: np.ndarray, steps: float) -> np.ndarray:
    """First-order lag with time constant ``steps`` grid steps (0 returns ``x``)."""
    if steps <= 0:
        return x
    a = math.exp(-1.0 / steps)
    b = 1.0 - a
    out = np.empty_like(x)
    acc = float(x[0])
    for i, v in enumerate(x.tolist()):
        acc = a * acc + b * v
        out[i] = acc
    return out


def throughput(times: np.ndarray, t_flow: np.ndarray, x_flow: np.ndarray) -> np.ndarray:
    """Cumulative volume at each time (flow per hour integrated over hours), strictly rising."""
    rate = np.clip(resample(t_flow, x_flow, times), 0.0, None)
    hours = np.diff(times.astype(float)) / (3600 * _US)
    vol = np.r_[0.0, np.cumsum(0.5 * (rate[1:] + rate[:-1]) * hours)]
    return vol + np.arange(vol.size) * 1e-12


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 10 or a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _band(r: float, n: int) -> float:
    """Two standard errors of a correlation r from n points (Fisher)."""
    return 2 * (1 - r * r) / math.sqrt(max(n - 3, 1))


def _run(values: np.ndarray, peak: int, keep: np.ndarray) -> tuple[int, int]:
    low = high = peak
    while low - 1 >= 0 and keep[low - 1]:
        low -= 1
    while high + 1 < values.size and keep[high + 1]:
        high += 1
    return low, high


class _Scorer:
    """Scores a prediction of the effect, made on the coordinate grid, against the effect."""

    def __init__(self, effect_t, effect_x, coord_at_effect, start_coord, coef, kind):
        self.kind = kind
        self.coef = coef
        self.coord_at_effect = coord_at_effect
        self.start = start_coord
        valid = coord_at_effect >= start_coord
        self.valid = valid
        self.effect = effect_x[valid]
        self.times = effect_t[valid]
        online = kind == "online" and self.effect.size > coef.size + 10
        self.white = ar_filter(self.effect, coef) if online else self.effect
        self.n = int(self.white.size)

    def predict(self, coord: _Coord, filtered: np.ndarray, dead: float) -> np.ndarray:
        return np.interp(self.coord_at_effect[self.valid] - dead, coord.grid, filtered)

    def score(self, pred: np.ndarray) -> float:
        if self.kind == "online":
            return _corr(ar_filter(pred, self.coef), self.white)
        return _corr(pred, self.white)


def _scan(coord: _Coord, scorer: _Scorer, dead: np.ndarray, mixing: np.ndarray, cause=None):
    cause = coord.cause if cause is None else cause
    out = np.full((mixing.size, dead.size), np.nan)
    for i, m in enumerate(mixing):
        filtered = first_order(cause, m / coord.step)
        for j, d in enumerate(dead):
            out[i, j] = scorer.score(scorer.predict(coord, filtered, d))
    return out


def _fit(
    model: str,
    coord: _Coord,
    scorer: _Scorer,
    dead: np.ndarray,
    mixing: np.ndarray,
    z_threshold: float,
    n_null: int,
    rng: np.random.Generator,
) -> tuple[ModelFit, Profile, np.ndarray] | None:
    corr = _scan(coord, scorer, dead, mixing)
    if np.all(np.isnan(corr)):
        return None
    flat = int(np.nanargmax(np.abs(corr)))
    im, jd = divmod(flat, dead.size)
    r = float(corr[im, jd])
    band = _band(r, scorer.n)
    absc = np.nan_to_num(np.abs(corr), nan=0.0)
    d_low, d_high = _run(dead, jd, absc.max(axis=0) >= abs(r) - band)
    m_low, m_high = _run(mixing, im, absc.max(axis=1) >= abs(r) - band)

    # A finer scan between the coarse peak's neighbours, for the point estimate and the interval.
    fine_dead = np.linspace(dead[max(jd - 1, 0)], dead[min(jd + 1, dead.size - 1)], 21)
    if mixing.size > 1:
        lo, hi = mixing[max(im - 1, 0)], mixing[min(im + 1, mixing.size - 1)]
        fine_mixing = np.unique(np.r_[np.linspace(lo, hi, 15), mixing[im]])
    else:
        fine_mixing = mixing
    fine = _scan(coord, scorer, fine_dead, fine_mixing)
    fim, fjd = divmod(int(np.nanargmax(np.abs(fine))), fine_dead.size)
    if abs(fine[fim, fjd]) > abs(r):
        r, best_dead, best_mixing = float(fine[fim, fjd]), fine_dead[fjd], fine_mixing[fim]
    else:
        best_dead, best_mixing = dead[jd], mixing[im]
    absf = np.nan_to_num(np.abs(fine), nan=0.0)
    keep = abs(r) - _band(r, scorer.n)
    f_low, f_high = _run(fine_dead, fjd, absf.max(axis=0) >= keep)
    g_low, g_high = _run(fine_mixing, fim, absf.max(axis=1) >= keep)

    chance = None
    if scorer.kind == "online":
        strength = abs(r) * math.sqrt(scorer.n)
        significant = strength >= z_threshold
    else:
        # The same scan on the cause rotated by a random offset: links to the effect are cut,
        # but the cause's own autocorrelation, the effect and the scan's choices stay.
        n = coord.cause.size
        best = []
        for _ in range(n_null):
            shift = int(rng.integers(n // 5, 4 * n // 5))
            rolled = _scan(coord, scorer, dead, mixing, np.roll(coord.cause, shift))
            best.append(float(np.nanmax(np.abs(rolled))) if not np.all(np.isnan(rolled)) else 0)
        chance = max(best) if best else 0.0
        strength = abs(r) / chance if chance > 0 else float("inf")
        significant = abs(r) > chance

    per_min = coord.per_min
    filtered = first_order(coord.cause, best_mixing / coord.step)
    fit = ModelFit(
        model=model,
        coordinate=coord.name,
        dead=_round(best_dead),
        dead_low=_round(min(dead[d_low], fine_dead[f_low])),
        dead_high=_round(max(dead[d_high], fine_dead[f_high])),
        mixing=_round(best_mixing),
        mixing_low=_round(min(mixing[m_low], fine_mixing[g_low])),
        mixing_high=_round(max(mixing[m_high], fine_mixing[g_high])),
        residence_min=round((best_dead + best_mixing) / per_min, 2),
        corr=round(r, 4),
        strength=round(strength, 2),
        significant=bool(significant),
    )
    profile = Profile(model, coord.name, dead, mixing, corr, chance)
    return fit, profile, scorer.predict(coord, filtered, best_dead)


def _round(v: float) -> float:
    return float(f"{v:.4g}")


def _even(x: np.ndarray, coord_t: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """``x`` on ``n`` evenly spaced values of the coordinate ``coord_t``."""
    grid = np.linspace(coord_t[0], coord_t[-1], n)
    return grid, np.interp(grid, coord_t, x)


def fit_lag(
    t_cause: np.ndarray,
    x_cause: np.ndarray,
    t_effect: np.ndarray,
    x_effect: np.ndarray,
    *,
    lab: bool = False,
    flow: tuple[np.ndarray, np.ndarray] | None = None,
    max_lag_s: float = 240 * 60,
    step_s: float | None = None,
    n_dead: int = 120,
    n_mixing: int = 12,
    z_threshold: float = 5.0,
    n_null: int = 59,
    rng: np.random.Generator | None = None,
) -> LagFit | None:
    """Fit the fixed, flow and mixing models from the cause to the effect (times in int64 µs).

    ``lab`` marks the effect as lab results at their sample times. ``flow`` is the throughput
    (times, values per hour) between the two points; without it the flow model is skipped and
    the mixing model works in time. Returns ``None`` when the data overlap too little.
    """
    rng = rng if rng is not None else np.random.default_rng(0)
    if step_s is None:
        periods = [np.median(np.diff(t_cause))] if t_cause.size > 1 else []
        if not lab and t_effect.size > 1:
            periods.append(np.median(np.diff(t_effect)))
        if not periods:
            return None
        step_s = max(float(max(periods)) / _US, 1.0)
    step = int(step_s * _US)
    start, end = max(t_cause[0], t_effect[0] - int(max_lag_s * _US)), t_effect[-1]
    end = min(end, t_cause[-1] + (int(max_lag_s * _US) if lab else 0))
    if end <= start:
        return None
    times = np.arange(start, end + 1, step, dtype=np.int64)
    if times.size < 50:
        return None
    cause = resample(t_cause, x_cause, times)
    coef = fit_ar(cause)

    if lab:
        keep = (t_effect >= times[0]) & (t_effect <= times[-1])
        eff_t, eff_x = t_effect[keep], x_effect[keep].astype(float)
    else:
        eff_t, eff_x = times, resample(t_effect, x_effect, times)

    minutes = (times.astype(float) - times[0]) / (60 * _US)
    coords = [_Coord("time", minutes, minutes, cause, 1.0)]
    mean_flow = None
    if flow is not None:
        vol = throughput(times, *flow)
        mean_flow = float(vol[-1] / (minutes[-1] / 60)) if minutes[-1] > 0 else 0.0
        if mean_flow > 0:
            grid, on_vol = _even(cause, vol, times.size)
            coords.append(_Coord("volume", vol, grid, on_vol, mean_flow / 60))

    fits: list[ModelFit] = []
    profiles: list[Profile] = []
    predictions: dict[str, np.ndarray] = {}
    scorer_times = None
    for model in ("fixed", "flow", "mixing"):
        coord = coords[0] if model == "fixed" or len(coords) == 1 else coords[1]
        if model == "flow" and len(coords) == 1:
            continue
        span = max_lag_s / 60 * coord.per_min
        at_effect = np.interp(eff_t.astype(float), times.astype(float), coord.at_times)
        kind = "lab" if lab else "online"
        scorer = _Scorer(eff_t, eff_x, at_effect, coord.grid[0] + span, coef, kind)
        if scorer.n < (20 if lab else max(50, 4 * int(max_lag_s // step_s))):
            return None
        scorer_times = scorer
        dead = np.linspace(0.0, span, min(n_dead, int(max_lag_s // step_s) + 1))
        if model == "mixing":
            mixing = np.r_[0.0, np.geomspace(span / 100, span / 2, n_mixing - 1)]
            dead = dead[:: max(1, dead.size // 60)]
        else:
            mixing = np.zeros(1)
        result = _fit(model, coord, scorer, dead, mixing, z_threshold, n_null, rng)
        if result is None:
            continue
        fit, profile, pred = result
        fits.append(fit)
        profiles.append(profile)
        predictions[model] = (scorer.times, scorer.effect, pred)

    if not fits:
        return None
    if lab:
        # One chance level for all the models: the best any of them reached on rotated causes,
        # so that trying three models does not triple the chance of a false link.
        chance = max(p.chance or 0.0 for p in profiles)
        for p in profiles:
            p.chance = chance
        for f in fits:
            f.strength = round(abs(f.corr) / chance, 2) if chance > 0 else float("inf")
            f.significant = abs(f.corr) > chance
    best = _choose(fits, scorer_times.n)
    return LagFit(
        fits=fits,
        best=best,
        n=scorer_times.n,
        step_s=step_s,
        mean_flow=round(mean_flow, 4) if mean_flow is not None else None,
        effect_kind="lab" if lab else "online",
        profiles=profiles,
        times=scorer_times.times,
        effect=scorer_times.effect,
        predictions=predictions,
    )


# The F statistic one extra parameter must reach (p about 0.01 for 100 or more points).
F_EXTRA = 6.6


def _choose(fits: list[ModelFit], n: int) -> str | None:
    """The best significant delay-only model (fixed or flow, whichever correlates more), or the
    mixing model when its extra parameter explains significantly more (an F test on r²)."""
    found = {f.model: f for f in fits if f.significant}
    simple = [f for f in (found.get("fixed"), found.get("flow")) if f is not None]
    best = max(simple, key=lambda f: abs(f.corr), default=None)
    mixing = found.get("mixing")
    if mixing is None:
        return best.model if best is not None else None
    if best is None:
        return mixing.model
    r2_small, r2_big = best.corr**2, mixing.corr**2
    f_stat = (r2_big - r2_small) / max(1 - r2_big, 1e-12) * (n - 3)
    return mixing.model if f_stat >= F_EXTRA else best.model
