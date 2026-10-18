"""Evidence plots for ``penguineer lag`` (Matplotlib, PNG)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from penguineer.engines.lagfit import LagFit  # noqa: E402
from penguineer.report.plots import CAUSE, QUALITY, _style, _times  # noqa: E402

DAY_US = 86_400_000_000
COLORS = {"fixed": "#6b7280", "flow": CAUSE, "mixing": "#047857"}


def _unit(coordinate: str, flow_unit: str) -> str:
    return "minutes" if coordinate == "time" else flow_unit


def profile_plot(fit: LagFit, path: Path, *, flow_unit: str = "volume") -> Path:
    """Each model's correlation over its dead time or volume, with the chance level."""
    fig, axes = plt.subplots(1, len(fit.profiles), figsize=(3.4 * len(fit.profiles), 3), dpi=110)
    axes = np.atleast_1d(axes)
    found = {f.model: f for f in fit.fits}
    for ax, prof in zip(axes, fit.profiles, strict=True):
        f = found[prof.model]
        color = COLORS[prof.model]
        if prof.mixing.size == 1:
            ax.plot(prof.dead, prof.corr[0], color=color, lw=1.2)
        else:
            # One curve per mixing value; the fitted one is drawn strongest.
            best = int(np.nanargmin(np.abs(prof.mixing - f.mixing)))
            for i, m in enumerate(prof.mixing):
                strong = i == best
                ax.plot(
                    prof.dead,
                    prof.corr[i],
                    color=color,
                    lw=1.4 if strong else 0.6,
                    alpha=1.0 if strong else 0.3,
                    label=f"mixing {m:.3g} (nearest scanned)" if strong else None,
                )
            ax.legend(fontsize=7, frameon=False)
        ax.axvspan(f.dead_low, f.dead_high, color=color, alpha=0.12, lw=0)
        ax.axvline(f.dead, color=color, lw=0.8, ls="--")
        ax.axhline(0, color="#9ca3af", lw=0.6)
        if prof.chance is not None:
            for sign in (1, -1):
                ax.axhline(sign * prof.chance, color=QUALITY, lw=0.8, ls=":")
        unit = _unit(prof.coordinate, flow_unit)
        ax.set_xlabel(f"dead {'time' if prof.coordinate == 'time' else 'volume'} [{unit}]")
        title = f"{prof.model}: {f.dead:.3g} {unit}"
        if prof.mixing.size > 1:
            title += f", then mixing {f.mixing:.3g} {unit}"
        ax.set_title(title, fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=8)
    axes[0].set_ylabel("correlation", fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def fit_plot(fit: LagFit, path: Path, *, cause: str, effect: str) -> Path:
    """The effect and each model's prediction, scaled to it by least squares.

    Drawn over the last days only (two for an online effect, five for lab results), where the
    lines can still be told apart, and only for the fixed model and the chosen one; the scaling
    uses all of the data.
    """
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=110)
    lab = fit.effect_kind == "lab"
    shown_us = (5 if lab else 2) * DAY_US
    since = fit.times[-1] - shown_us if fit.times.size else 0
    shown = fit.times >= since
    times = _times(fit.times[shown])
    y = fit.effect[shown]
    if lab:
        ax.scatter(times, y, color="black", s=12, zorder=3, label=f"{effect} (lab)")
    else:
        ax.plot(times, y, color="black", lw=0.7, label=effect)
    for model, (t, y_model, pred) in fit.predictions.items():
        if model not in ("fixed", fit.best):
            continue
        ok = np.isfinite(pred)
        if ok.sum() < 3 or np.std(pred[ok]) == 0:
            continue
        a, b = np.polyfit(pred[ok], y_model[ok], 1)
        style = {"marker": "o", "ms": 3, "lw": 0.8} if lab else {"lw": 1.0}
        name = f"{cause}, {model}" + (" (best)" if model == fit.best else "")
        keep = t >= since
        ax.plot(
            _times(t[keep]),
            (a * pred + b)[keep],
            color=COLORS[model],
            alpha=0.9,
            label=name,
            **style,
        )
    ax.set_ylabel(effect, fontsize=9)
    ax.set_title(f"{effect} and {cause} moved by each fitted model", fontsize=10)
    ax.legend(fontsize=7, frameon=False, loc="best")
    _style(ax)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
