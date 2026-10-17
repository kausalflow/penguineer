"""Dead time between two signals: cross-correlation after prewhitening.

Process signals are strongly autocorrelated, so a raw cross-correlation has a broad peak that
says little about the delay. Both series are filtered with one AR model fitted to the upstream
signal (prewhitening); the delay is then the lag of the largest cross-correlation of the
filtered series, and its interval is the run of lags whose correlation is within the noise band
of that peak.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

_US = 1_000_000


class LagEstimate(BaseModel):
    lag_min: float = Field(description="Estimated dead time in minutes")
    low_min: float = Field(description="Low end of the interval, minutes")
    high_min: float = Field(description="High end of the interval, minutes")
    corr: float = Field(description="Cross-correlation of the prewhitened series at the lag")
    z: float = Field(description="Peak correlation in units of its noise level, |corr| √n")
    significant: bool = Field(description="z above the threshold")
    n: int = Field(description="Grid points used")
    step_s: float = Field(description="Grid step in seconds")
    ar_order: int


def resample(t: np.ndarray, x: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Linear interpolation of (t, x) onto ``grid`` (all int64 microseconds)."""
    return np.interp(grid.astype(float), t.astype(float), x.astype(float))


def fit_ar(x: np.ndarray, max_order: int = 12) -> np.ndarray:
    """AR coefficients by least squares, order chosen by AIC (order 0 gives an empty array)."""
    x = x - x.mean()
    n = x.size
    best_aic, best = n * np.log(np.var(x) + 1e-300), np.empty(0)
    for p in range(1, min(max_order, n // 10) + 1):
        lagged = np.column_stack([x[p - k - 1 : n - k - 1] for k in range(p)])
        target = x[p:]
        coef, *_ = np.linalg.lstsq(lagged, target, rcond=None)
        resid = target - lagged @ coef
        aic = target.size * np.log(np.var(resid) + 1e-300) + 2 * p
        if aic < best_aic:
            best_aic, best = aic, coef
    return best


def ar_filter(x: np.ndarray, coef: np.ndarray) -> np.ndarray:
    """Residuals of ``x`` under the AR model ``coef`` (length n - p)."""
    x = x - x.mean()
    p = coef.size
    if p == 0:
        return x
    n = x.size
    pred = sum(coef[k] * x[p - k - 1 : n - k - 1] for k in range(p))
    return x[p:] - pred


def cross_correlation(a: np.ndarray, b: np.ndarray, max_lag: int) -> np.ndarray:
    """corr(a[t], b[t + L]) for L = 0..max_lag: b lags a by L steps."""
    out = np.full(max_lag + 1, np.nan)
    for lag in range(max_lag + 1):
        u, v = a[: a.size - lag], b[lag:]
        if u.size < 10 or u.std() == 0 or v.std() == 0:
            continue
        out[lag] = np.corrcoef(u, v)[0, 1]
    return out


def estimate_dead_time(
    t_up: np.ndarray,
    x_up: np.ndarray,
    t_down: np.ndarray,
    x_down: np.ndarray,
    *,
    max_lag_s: float,
    min_lag_s: float = 0.0,
    step_s: float | None = None,
    z_threshold: float = 5.0,
) -> LagEstimate | None:
    """Dead time from upstream ``x_up`` to downstream ``x_down`` (times in int64 µs).

    Returns ``None`` when the overlap is too short to say anything.
    """
    if step_s is None:
        periods = [np.median(np.diff(t)) for t in (t_up, t_down) if t.size > 1]
        if not periods:
            return None
        step_s = max(periods) / _US
    step = int(step_s * _US)
    start, end = max(t_up[0], t_down[0]), min(t_up[-1], t_down[-1])
    if step <= 0 or end <= start:
        return None
    grid = np.arange(start, end + 1, step, dtype=np.int64)
    max_lag = int(max_lag_s // step_s)
    if grid.size < max(50, 4 * max_lag):
        return None

    up, down = resample(t_up, x_up, grid), resample(t_down, x_down, grid)
    coef = fit_ar(up)
    e_up, e_down = ar_filter(up, coef), ar_filter(down, coef)
    ccf = cross_correlation(e_up, e_down, max_lag)
    lo_lag = int(np.ceil(min_lag_s / step_s))
    ccf[:lo_lag] = np.nan
    if np.all(np.isnan(ccf)):
        return None

    peak = int(np.nanargmax(np.abs(ccf)))
    n = e_up.size - peak
    noise = 2 / np.sqrt(n)
    near = np.abs(ccf) >= np.abs(ccf[peak]) - noise
    low = high = peak
    while low - 1 >= 0 and near[low - 1]:
        low -= 1
    while high + 1 < ccf.size and near[high + 1]:
        high += 1
    z = float(abs(ccf[peak]) * np.sqrt(n))
    minutes = step_s / 60
    return LagEstimate(
        lag_min=round(peak * minutes, 3),
        low_min=round(low * minutes, 3),
        high_min=round(high * minutes, 3),
        corr=round(float(ccf[peak]), 4),
        z=round(z, 2),
        significant=z >= z_threshold,
        n=int(n),
        step_s=float(step_s),
        ar_order=int(coef.size),
    )
