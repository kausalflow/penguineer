"""Homeostat scenario bundles: a simulated plant's export and its ground truth.

A bundle is a folder written by :mod:`penguineer.bench.export` (or by Homeostat itself, in the
same layout). Everything but ``truth.json`` is read through the same readers as a real plant's
export, so nothing in the engine knows it is looking at a synthetic plant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl
from plantgraph import PlantGraph
from pydantic import BaseModel, Field

from penguineer.core.errors import UsageError
from penguineer.io.graph import load_graph
from penguineer.io.historian import read_historian
from penguineer.io.lims import read_lims, read_lots


class Truth(BaseModel):
    """The answer of one scenario."""

    scenario: str
    fault_type: str
    kind: str = Field(description="The hypothesis kind that describes the fault")
    data_fault: bool = Field(description="The fault is in a measurement, not in the process")
    root_cause: list[str] = Field(
        description="Node IDs and tag names that each count as naming the root cause"
    )
    onset: datetime
    magnitude: float | None = None
    lot: str = Field(description="The first off-spec lot after the onset: the one to investigate")
    affected_lots: list[str] = Field(default_factory=list)
    description: str = ""
    homeostat: str | None = Field(None, description="Homeostat version that made the bundle")
    config_hash: str | None = None


@dataclass
class Bundle:
    path: Path
    graph: PlantGraph
    historian: pl.DataFrame
    lims: pl.DataFrame
    lots: pl.DataFrame
    truth: Truth


def read_truth(path: Path) -> Truth:
    return Truth.model_validate_json((path / "truth.json").read_text())


def read_bundle(path: Path) -> Bundle:
    """Read one scenario bundle folder."""
    needed = ["graph.yaml", "lims.csv", "lots.csv", "truth.json"]
    historian = next(
        (path / n for n in ("historian.parquet", "historian.csv") if (path / n).exists()), None
    )
    missing = [n for n in needed if not (path / n).exists()] + (
        ["historian.parquet"] if historian is None else []
    )
    if missing:
        raise UsageError(f"{path} is not a scenario bundle: missing {', '.join(missing)}")
    return Bundle(
        path=path,
        graph=load_graph(path / "graph.yaml"),
        historian=read_historian(historian).frame,
        lims=read_lims(path / "lims.csv").frame,
        lots=read_lots(path / "lots.csv"),
        truth=read_truth(path),
    )


def bundles_in(folder: Path) -> list[Path]:
    """Scenario folders under ``folder``, in name order."""
    return sorted(p for p in folder.iterdir() if (p / "truth.json").exists())
