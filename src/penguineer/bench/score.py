"""Score investigate on scenario bundles: hit@k, wrong-confident rate, data-fault catch rate.

Each scenario runs the same code path as ``penguineer investigate`` on a real plant: the bundle's
graph, historian, LIMS and lots go in, ``truth.json`` is only read afterwards to score the result.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from penguineer.core.errors import PenguineerError
from penguineer.engines.investigate import Investigation, Settings, investigate
from penguineer.io.homeostat import Truth, read_bundle
from penguineer.report.model import Hypothesis


class ScenarioScore(BaseModel):
    scenario: str
    fault_type: str
    data_fault: bool
    lot: str
    rank: int | None = Field(None, description="Rank of the first hypothesis naming the cause")
    top: str | None = Field(None, description="The leading hypothesis's cause")
    top_kind: str | None = None
    top_confidence: str | None = None
    kind_ok: bool = Field(False, description="The hit hypothesis has the fault's kind")
    data_fault_caught: bool | None = Field(
        None, description="Data faults: the faulty tag leads as a measurement fault"
    )
    error: str | None = Field(None, description="Why investigate stopped, if it did")
    seconds: float

    @property
    def hit_at_1(self) -> bool:
        return self.rank == 1

    @property
    def hit_at_3(self) -> bool:
        return self.rank is not None and self.rank <= 3

    @property
    def wrong_confident(self) -> bool:
        return self.top_confidence == "high" and self.rank != 1


class Rate(BaseModel):
    value: float | None
    n: int
    low: float | None = Field(None, description="95% Wilson interval, low end")
    high: float | None = None


class FaultTypeScore(BaseModel):
    n: int
    hit_at_1: float
    hit_at_3: float


class BenchSummary(BaseModel):
    n_scenarios: int
    hit_at_1: Rate
    hit_at_3: Rate
    wrong_confident_rate: Rate
    data_fault_catch_rate: Rate
    errors: int
    by_fault_type: dict[str, FaultTypeScore]


def wilson(k: int, n: int, z: float = 1.96) -> Rate:
    if n == 0:
        return Rate(value=None, n=0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return Rate(value=round(p, 4), n=n, low=round(centre - half, 4), high=round(centre + half, 4))


def names_cause(h: Hypothesis, roots: set[str]) -> bool:
    """Whether a hypothesis names the root cause.

    A process hypothesis claims that its tag moved and caused the deviation, so it counts only
    when the tag is in the truth set; the node a tag sits on is not enough, or every tag on the
    faulty equipment would score. The other kinds claim something about their node (a loop
    lost control, a measurement is wrong, something unmeasured changed there).
    """
    if h.kind == "process":
        return h.cause_tag in roots
    return h.cause_node in roots or (h.cause_tag is not None and h.cause_tag in roots)


def score_investigation(inv: Investigation | None, truth: Truth, seconds: float, error=None):
    roots = set(truth.root_cause)
    base = dict(
        scenario=truth.scenario,
        fault_type=truth.fault_type,
        data_fault=truth.data_fault,
        lot=truth.lot,
        seconds=round(seconds, 2),
    )
    if inv is None:
        return ScenarioScore(**base, error=error)
    hs = inv.hypotheses
    hit = next((h for h in hs if names_cause(h, roots)), None)
    top = hs[0] if hs else None
    caught = None
    if truth.data_fault:
        caught = top is not None and top.kind == "measurement_fault" and names_cause(top, roots)
    return ScenarioScore(
        **base,
        rank=hit.rank if hit else None,
        top=(top.cause_tag or top.cause_node) if top else None,
        top_kind=str(top.kind) if top else None,
        top_confidence=top.confidence if top else None,
        kind_ok=hit is not None and hit.kind == truth.kind,
        data_fault_caught=caught,
    )


def run_scenario(
    path: Path, settings: Settings | None = None
) -> tuple[ScenarioScore, Investigation | None]:
    b = read_bundle(path)
    t0 = time.perf_counter()
    try:
        inv = investigate(b.graph, b.historian, b.lims, b.truth.lot, lots=b.lots, settings=settings)
    except PenguineerError as e:
        return score_investigation(None, b.truth, time.perf_counter() - t0, str(e)), None
    return score_investigation(inv, b.truth, time.perf_counter() - t0), inv


def summarize(scores: list[ScenarioScore]) -> BenchSummary:
    n = len(scores)
    data = [s for s in scores if s.data_fault]
    by: dict[str, list[ScenarioScore]] = defaultdict(list)
    for s in scores:
        by[s.fault_type].append(s)
    return BenchSummary(
        n_scenarios=n,
        hit_at_1=wilson(sum(s.hit_at_1 for s in scores), n),
        hit_at_3=wilson(sum(s.hit_at_3 for s in scores), n),
        wrong_confident_rate=wilson(sum(s.wrong_confident for s in scores), n),
        data_fault_catch_rate=wilson(sum(bool(s.data_fault_caught) for s in data), len(data)),
        errors=sum(s.error is not None for s in scores),
        by_fault_type={
            k: FaultTypeScore(
                n=len(v),
                hit_at_1=round(sum(s.hit_at_1 for s in v) / len(v), 4),
                hit_at_3=round(sum(s.hit_at_3 for s in v) / len(v), 4),
            )
            for k, v in sorted(by.items())
        },
    )


def run_bench(
    paths: list[Path],
    settings: Settings | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[ScenarioScore], BenchSummary]:
    step = progress or (lambda done, total, message: None)
    scores = []
    for i, p in enumerate(paths):
        step(i, len(paths), p.name)
        scores.append(run_scenario(p, settings)[0])
    step(len(paths), len(paths), "done")
    return scores, summarize(scores)
