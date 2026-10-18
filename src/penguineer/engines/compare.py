"""Two-group comparison: difference in means, Welch's t-test and Cohen's d.

The t distribution is computed here (regularized incomplete beta, continued fraction) so the
engine needs only NumPy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class Comparison:
    n_a: int
    n_b: int
    mean_a: float
    mean_b: float
    sd_a: float | None
    sd_b: float | None
    diff: float  # mean_a - mean_b
    t: float | None
    df: float | None
    p_value: float | None  # two-sided
    cohens_d: float | None  # diff over the root mean of the two variances


def welch(a: np.ndarray, b: np.ndarray) -> Comparison:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if a.size == 0 or b.size == 0:
        raise ValueError("both groups need at least one value")
    ma, mb = float(a.mean()), float(b.mean())
    va = float(a.var(ddof=1)) if a.size > 1 else None
    vb = float(b.var(ddof=1)) if b.size > 1 else None
    t = df = p = d = None
    if va is not None and vb is not None:
        se2 = va / a.size + vb / b.size
        if se2 > 0:
            t = (ma - mb) / math.sqrt(se2)
            df = se2**2 / ((va / a.size) ** 2 / (a.size - 1) + (vb / b.size) ** 2 / (b.size - 1))
            p = t_two_sided(t, df)
        pooled = math.sqrt((va + vb) / 2)
        d = (ma - mb) / pooled if pooled else None
    return Comparison(
        n_a=int(a.size),
        n_b=int(b.size),
        mean_a=ma,
        mean_b=mb,
        sd_a=math.sqrt(va) if va is not None else None,
        sd_b=math.sqrt(vb) if vb is not None else None,
        diff=ma - mb,
        t=t,
        df=df,
        p_value=p,
        cohens_d=d,
    )


def t_two_sided(t: float, df: float) -> float:
    """P(|T| >= |t|) for Student's t with ``df`` degrees of freedom."""
    return betainc_reg(df / 2, 0.5, df / (df + t * t))


def betainc_reg(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return math.exp(log_front) * _beta_cf(a, b, x) / a
    return 1.0 - math.exp(log_front) * _beta_cf(b, a, 1 - x) / b


def _beta_cf(a: float, b: float, x: float, iterations: int = 300, eps: float = 1e-15) -> float:
    """Continued fraction for the incomplete beta (modified Lentz)."""
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, iterations + 1):
        for num in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1.0 + num * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < eps:
            break
    return h
