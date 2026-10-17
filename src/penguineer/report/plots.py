"""Evidence plots (Matplotlib, written as PNG files under evidence/)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from penguineer.engines.investigate import Investigation  # noqa: E402
from penguineer.report.model import Hypothesis  # noqa: E402

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
QUALITY = "#b42318"
CAUSE = "#1d4ed8"
EVENT = "#fde68a"
REFERENCE = "#d1fae5"


def _times(us: np.ndarray) -> list[datetime]:
    return [_EPOCH + timedelta(microseconds=int(u)) for u in us]


def _style(ax: plt.Axes) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.tick_params(labelsize=8)


def quality_overview(inv: Investigation, path: Path) -> Path:
    """The quality over the whole period: online signal, lab results, spec, windows."""
    ev = inv.event
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=110)
    for w in inv.reference:
        ax.axvspan(w.start, w.end, color=REFERENCE, lw=0)
    ax.axvspan(ev.window_start, ev.window_end, color=EVENT, lw=0)
    if inv.proxy is not None:
        ax.plot(_times(inv.grid.times), inv.proxy, color="#6b7280", lw=0.8, label=ev.proxy_tag)
    lab_t = [t for t, _ in inv.lab if inv.grid.times[0] <= _us(t) <= inv.grid.times[-1]]
    lab_v = [v for t, v in inv.lab if inv.grid.times[0] <= _us(t) <= inv.grid.times[-1]]
    ax.scatter(lab_t, lab_v, color="black", s=18, zorder=3, label="lab")
    ax.scatter([ev.sampled_at], [ev.value], color=QUALITY, s=40, zorder=4, label=ev.lot)
    for limit in (ev.spec_low, ev.spec_high):
        if limit is not None:
            ax.axhline(limit, color=QUALITY, ls="--", lw=1)
    if ev.onset is not None:
        ax.axvline(ev.onset, color=QUALITY, lw=1, alpha=0.6)
    ax.set_ylabel(f"{ev.property}" + (f" [{ev.unit}]" if ev.unit else ""), fontsize=9)
    ax.set_title(f"{ev.property} for lot {ev.lot} (yellow) and reference lots (green)", fontsize=10)
    ax.legend(fontsize=7, frameon=False, loc="lower left")
    _style(ax)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def hypothesis_trend(inv: Investigation, h: Hypothesis, path: Path) -> Path | None:
    """The cause tag, shifted by its dead time, against the online quality signal."""
    if h.cause_tag is None or h.cause_tag not in inv.series:
        return None
    ev = inv.event
    length = ev.window_end - ev.window_start
    lo, hi = _us(ev.window_start - length), _us(ev.window_end)
    keep = (inv.grid.times >= lo) & (inv.grid.times <= hi)
    t = _times(inv.grid.times[keep])
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=110)
    ax.axvspan(ev.window_start, ev.window_end, color=EVENT, lw=0)
    ax.plot(t, inv.series[h.cause_tag][keep], color=CAUSE, lw=1.1)
    ax.set_ylabel(
        f"{h.cause_tag}"
        + (f" [{h.unit}]" if h.unit else "")
        + f"\nshifted {h.dead_time_min:g} min",
        fontsize=9,
        color=CAUSE,
    )
    if inv.proxy is not None:
        ax2 = ax.twinx()
        ax2.plot(t, inv.proxy[keep], color=QUALITY, lw=1.0, alpha=0.8)
        ax2.set_ylabel(ev.proxy_tag or ev.property, fontsize=9, color=QUALITY)
        ax2.spines[["top"]].set_visible(False)
        ax2.tick_params(labelsize=8)
    if ev.onset is not None:
        ax.axvline(ev.onset, color=QUALITY, lw=1, alpha=0.6)
    ax.set_title(f"#{h.rank} {h.cause_tag} in material time at {ev.sampled_node}", fontsize=10)
    _style(ax)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def _us(t: datetime) -> int:
    return int((t - _EPOCH) / timedelta(microseconds=1))
