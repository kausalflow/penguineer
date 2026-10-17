"""The evidence tests run on each candidate: effect size, onset timing and lag correlation.

All three work on series already aligned to the sampled node's time (see ``align``), so a
candidate's change and the quality change are compared on one clock.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def robust_location_scale(x: np.ndarray) -> tuple[float, float]:
    """Median and 1.4826 × MAD of the finite values (scale 0 when there are too few)."""
    x = x[np.isfinite(x)]
    if x.size < 3:
        return (float(np.median(x)) if x.size else np.nan), 0.0
    med = float(np.median(x))
    return med, float(1.4826 * np.median(np.abs(x - med)))


def noise_scale(x: np.ndarray) -> float:
    """Sample-to-sample noise: robust scale of first differences / √2."""
    d = np.diff(x[np.isfinite(x)])
    return robust_location_scale(d)[1] / np.sqrt(2) if d.size >= 3 else 0.0


@dataclass
class Effect:
    delta: float  # event median minus reference median, in the tag's unit
    z: float  # delta in units of the reference scale
    n_event: int
    n_reference: int

    @property
    def strength(self) -> float:
        """0 below 2 reference scales, 1 from 6."""
        return float(np.clip((abs(self.z) - 2.0) / 4.0, 0.0, 1.0))


def effect_size(
    event: np.ndarray, reference: np.ndarray, scale_floor: float = 0.0
) -> Effect | None:
    """How far the event window sits from the reference, in reference scales.

    ``scale_floor`` is a lower bound on the scale, such as the spread of the tag's level from
    one earlier lot to the next: a tag that wanders slowly is not "moved" by being elsewhere
    in this lot than in the last few.
    """
    ev = event[np.isfinite(event)]
    ref = reference[np.isfinite(reference)]
    if ev.size < 3 or ref.size < 10:
        return None
    med_r, scale = robust_location_scale(ref)
    med_e = float(np.median(ev))
    floor = max(noise_scale(ref), 1e-9 * max(1.0, abs(med_r)))
    scale = max(scale, floor, scale_floor)
    delta = med_e - med_r
    return Effect(delta, delta / scale, int(ev.size), int(ref.size))


@dataclass
class ChangePoint:
    index: int  # first index of the new level
    before: float
    after: float
    z: float  # level shift in units of the noise scale

    @property
    def shift(self) -> float:
        return self.after - self.before


def change_point(x: np.ndarray, min_segment: int = 5) -> ChangePoint | None:
    """The single mean shift that best splits ``x`` (least squares); NaNs are ignored."""
    idx = np.flatnonzero(np.isfinite(x))
    if idx.size < 2 * min_segment:
        return None
    v = x[idx]
    c1 = np.cumsum(v)
    c2 = np.cumsum(v * v)
    n = v.size
    k = np.arange(min_segment, n - min_segment + 1)
    left_sse = c2[k - 1] - c1[k - 1] ** 2 / k
    right_sum = c1[-1] - c1[k - 1]
    right_sse = (c2[-1] - c2[k - 1]) - right_sum**2 / (n - k)
    best = int(k[np.argmin(left_sse + right_sse)])
    before, after = float(v[:best].mean()), float(v[best:].mean())
    noise = max(noise_scale(v), 1e-12)
    return ChangePoint(int(idx[best]), before, after, (after - before) / noise)


def onset_strength(candidate_min: float, quality_min: float, tolerance_min: float) -> float:
    """1 when the candidate's change lands on the quality onset, falling off with the gap.

    A candidate that changes after the quality has already moved gets 0 once it is later than
    the tolerance: an effect cannot come before its cause.
    """
    gap = candidate_min - quality_min
    if gap > tolerance_min:
        return 0.0
    return float(np.exp(-0.5 * (gap / tolerance_min) ** 2))


def lag_correlation(candidate: np.ndarray, quality: np.ndarray) -> tuple[float, int] | None:
    """Pearson correlation of two aligned series over the points where both are finite."""
    ok = np.isfinite(candidate) & np.isfinite(quality)
    if ok.sum() < 20:
        return None
    a, b = candidate[ok], quality[ok]
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1]), int(ok.sum())


def smooth(x: np.ndarray, n: int) -> np.ndarray:
    """Centred moving mean over ``n`` points, ignoring NaNs (NaN where none are finite)."""
    if n <= 1:
        return x
    ok = np.isfinite(x)
    v = np.where(ok, x, 0.0)
    k = np.ones(n)
    total = np.convolve(v, k, mode="same")
    count = np.convolve(ok.astype(float), k, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / count, np.nan)


def level_spread(x: np.ndarray, windows: list[np.ndarray]) -> float:
    """Standard deviation of the median of ``x`` over earlier windows (0 with fewer than 4)."""
    meds = []
    for m in windows:
        v = x[m]
        v = v[np.isfinite(v)]
        if v.size >= 3:
            meds.append(float(np.median(v)))
    return float(np.std(meds, ddof=1)) if len(meds) >= 4 else 0.0


def explained_fraction(
    upstream_ref: np.ndarray,
    downstream_ref: np.ndarray,
    upstream_delta: float,
    downstream_delta: float,
) -> float | None:
    """Share of the downstream deviation predicted from the upstream one.

    Fits downstream = a + b · upstream on the reference period and returns
    b · upstream_delta / downstream_delta.
    """
    ok = np.isfinite(upstream_ref) & np.isfinite(downstream_ref)
    if ok.sum() < 20 or downstream_delta == 0:
        return None
    u, d = upstream_ref[ok], downstream_ref[ok]
    if u.std() == 0:
        return None
    b = float(np.cov(u, d)[0, 1] / u.var(ddof=1))
    return b * upstream_delta / downstream_delta
