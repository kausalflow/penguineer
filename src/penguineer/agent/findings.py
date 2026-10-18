"""Findings and the numbers in a conclusion.

A finding is one claim with the value computed for it. Its value is canonicalised and hashed, so
a replay can tell whether it computed the same number. A conclusion names findings as ``{{F1}}``
(value with unit) or ``{{F1.value}}``; :func:`fill` replaces them and refuses a conclusion that
types a measured number itself, so no number in a report comes from a model's text.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import numpy as np

from penguineer.agent.protocol import CONFIDENCES, VERDICTS

PLACEHOLDER = re.compile(r"\{\{\s*(F\d+)(?:\.(value))?\s*\}\}")
TYPED_NUMBER = re.compile(
    r"(?<![\w.-])\d+(?:[.,]\d+)?\s*(?:%|°C|°F|percentage points|points|pts|bar|ppm|kg|mpm|m/min)"
    r"|(?<![\w.-])\d+\.\d+(?![\w.-])"
)


class ReportError(ValueError):
    """A conclusion that types a measured number or names a finding that does not exist."""


def canonical(value: Any) -> str:
    """A stable text form of a value: floats to 12 significant digits, dicts sorted."""
    if isinstance(value, np.floating | float):
        return repr(float(f"{float(value):.12g}"))
    if isinstance(value, np.integer | int):
        return str(int(value))
    if isinstance(value, dict):
        return json.dumps({k: canonical(v) for k, v in sorted(value.items())})
    if isinstance(value, list | tuple):
        return json.dumps([canonical(v) for v in value])
    return json.dumps(value, default=str)


def finding(
    fid: str,
    *,
    statement: str,
    value: Any = None,
    unit: str = "",
    verdict: str = "supported",
    confidence: str = "medium",
    hypothesis: str = "",
    evidence: list | None = None,
) -> dict:
    """One finding as a record, with ``value_sha256`` for comparing replays."""
    if verdict not in VERDICTS:
        raise ValueError("verdict must be supported, refuted or inconclusive")
    if confidence not in CONFIDENCES:
        raise ValueError("confidence must be high, medium or low")
    if isinstance(value, np.floating | np.integer):
        value = value.item()
    if isinstance(value, float):
        value = float(f"{value:.12g}") + 0.0  # + 0.0 turns -0.0 into 0.0
    return {
        "id": fid,
        "hypothesis": hypothesis,
        "statement": statement,
        "value": value,
        "unit": unit,
        "verdict": verdict,
        "confidence": confidence,
        "evidence": evidence or [],
        "value_sha256": hashlib.sha256(canonical(value).encode()).hexdigest(),
    }


def fmt(value: Any, unit: str = "") -> str:
    if isinstance(value, float):
        text = f"{value:,.2f}" if abs(value) >= 1 else f"{value:.3g}"
    else:
        text = str(value)
    return f"{text} {unit}".strip() if unit else text


def fill(text: str, findings: dict[str, dict]) -> str:
    """Replace ``{{Fn}}`` placeholders with the findings' values; refuse typed numbers."""
    typed = TYPED_NUMBER.findall(PLACEHOLDER.sub("", text))
    if typed:
        raise ReportError(
            "measured numbers must come from findings as {{Fn}} placeholders; "
            f"found typed: {typed[:5]}"
        )

    def repl(m: re.Match) -> str:
        fid, part = m.group(1), m.group(2)
        if fid not in findings:
            raise ReportError(f"{fid} is not a recorded finding")
        f = findings[fid]
        return fmt(f["value"]) if part == "value" else fmt(f["value"], f.get("unit", ""))

    return PLACEHOLDER.sub(repl, text)
