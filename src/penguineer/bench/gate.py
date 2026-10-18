"""The release gate: the hidden split's scores against the thresholds in docs/concepts/bench.md.

``python -m penguineer.bench.gate bench.json`` prints one line per metric and exits with 1 when
any fails. It reads only the summary, so its output is safe to show for the hidden split.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from penguineer.bench.score import BenchSummary, Rate

# (metric, how it must compare, threshold)
GATES = (
    ("hit_at_3", ">=", 0.70),
    ("wrong_confident_rate", "<", 0.10),
    ("data_fault_catch_rate", ">=", 0.90),
)
MIN_SCENARIOS = 150


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def check(summary: BenchSummary) -> tuple[bool, list[str]]:
    """Whether the gate passes, and one line per metric."""
    lines = []
    ok = summary.n_scenarios >= MIN_SCENARIOS and summary.errors == 0
    lines.append(
        f"{'pass' if summary.n_scenarios >= MIN_SCENARIOS else 'FAIL'}  scenarios "
        f"{summary.n_scenarios} (at least {MIN_SCENARIOS})"
    )
    lines.append(f"{'pass' if summary.errors == 0 else 'FAIL'}  errors {summary.errors}")
    for name, op, threshold in GATES:
        rate: Rate = getattr(summary, name)
        v = rate.value
        passed = v is not None and (v >= threshold if op == ">=" else v < threshold)
        ok &= passed
        interval = (
            f" (95% {_pct(rate.low)} to {_pct(rate.high)}, n={rate.n})" if v is not None else ""
        )
        lines.append(
            f"{'pass' if passed else 'FAIL'}  {name} {_pct(v)}{interval}, needs {op} "
            f"{_pct(threshold)}"
        )
    lines.append("hit_at_1 " + _pct(summary.hit_at_1.value) + " (reported, no threshold)")
    return ok, lines


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m penguineer.bench.gate bench.json", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[0]).read_text())
    ok, lines = check(BenchSummary.model_validate(data["summary"]))
    print(f"release gate on the {data.get('split', '?')} split: {'PASSED' if ok else 'FAILED'}")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
