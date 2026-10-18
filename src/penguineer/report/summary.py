"""The report's prose summary: a template, or an LLM's draft that passes the checks.

An LLM draft is kept only if it is grounded in the report (the v0 form of "no claim without
evidence"). :func:`check` rejects a draft that

- names a cause that is not among the hypotheses, or any other plant node or tag that the
  hypotheses, their paths and the ruled-out list do not mention;
- cites an evidence ID that does not exist, or makes a claim without one;
- states a confidence higher than the hypothesis's label, or calls anything certain.

A rejected or failed draft falls back to the template, and the summary says why. The LLM sees
only the results in ``report.json`` (:func:`summary_input`), never the time series.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from penguineer.llm import LLMError, LLMProvider, load_prompt
from penguineer.report.model import Claim, Hypothesis, Report, Summary

PROMPT = ("summary", 1)
MAX_CLAIMS = 3

_LEVEL = {"low": 0, "medium": 1, "high": 2}
_CERTAIN = re.compile(r"\b(certain(?:ly)?|proven|definite(?:ly)?|undoubtedly|beyond doubt)\b", re.I)
_STATED = re.compile(r"\b(high|medium|low)[- ]confidence\b", re.I)
_EVIDENCE_ID = re.compile(r"\bE\d{3}\b")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\-]*[A-Za-z0-9]|[A-Za-z0-9]")


class SummaryDraft(BaseModel):
    """What the LLM is asked to return."""

    overview: str = Field(description="Two or three sentences: what went off spec, what leads")
    claims: list[Claim] = Field(description=f"At most {MAX_CLAIMS}, one per hypothesis, in rank")


def _cause(h: Hypothesis) -> str:
    return h.cause_tag or h.cause_node


def _phrase(h: Hypothesis) -> str:
    kind = {
        "process": f"{_cause(h)} moved {h.direction}",
        "setpoint_change": f"the {h.cause_tag} setpoint was changed",
        "lost_control": f"{h.cause_node} lost control",
        "unmeasured_disturbance": f"an unmeasured change at {h.cause_node}",
        "measurement_fault": f"{_cause(h)} may read wrong",
    }
    return kind.get(str(h.kind), f"{_cause(h)} {h.kind}")


def template_summary(report: Report) -> Summary:
    """The deterministic summary, used without an LLM and whenever an LLM draft is not kept."""
    # The report's first line already gives the lab value and the spec.
    if report.hypotheses:
        h = report.hypotheses[0]
        overview = f"The leading hypothesis, with {h.confidence} confidence: {_phrase(h)}."
    else:
        overview = "No hypothesis stood out."
    claims = [
        Claim(cause=_cause(h), confidence=h.confidence, evidence=h.evidence, sentence=h.summary)
        for h in report.hypotheses[:MAX_CLAIMS]
    ]
    return Summary(overview=overview, claims=claims, source="template")


def summary_input(report: Report) -> dict[str, Any]:
    """Everything the LLM sees: the results, without raw data. ``--llm-dry-run`` shows it."""
    return {
        "event": report.event.model_dump(mode="json"),
        "regime": report.regime,
        "reference_lots": [w.model_dump(mode="json") for w in report.reference],
        "hypotheses": [
            h.model_dump(mode="json", exclude={"health_warnings"}) for h in report.hypotheses
        ],
        "evidence": [
            e.model_dump(mode="json", include={"id", "kind", "title"}) for e in report.evidence
        ],
        "ruled_out": [r.model_dump(mode="json") for r in report.ruled_out],
        "notes": report.notes,
    }


def allowed_names(report: Report) -> set[str]:
    """Plant names a summary may mention: the hypotheses, their paths, what was ruled out."""
    ev = report.event
    names = {ev.quality_node, ev.sampled_node, ev.property}
    names |= {n for n in (ev.sample_point, ev.proxy_tag) if n}
    names |= set(report.regime)
    for h in report.hypotheses:
        names |= {h.cause_node, *h.graph_path}
        names |= {h.cause_tag} if h.cause_tag else set()
        names |= {r.cause for r in h.ruled_out}
    names |= {r.cause for r in report.ruled_out}
    return names


def check(draft: SummaryDraft, report: Report, plant_names: set[str]) -> list[str]:
    """Why ``draft`` cannot be kept; empty when it is grounded in ``report``.

    ``plant_names`` holds every node ID and tag of the plant graph: a name from it that the
    report does not mention is a cause the LLM brought in by itself.
    """
    problems: list[str] = []
    ids = {e.id for e in report.evidence}
    by_cause: dict[str, Hypothesis] = {}
    for h in report.hypotheses:
        by_cause.setdefault(_cause(h), h)
    for h in report.hypotheses:
        by_cause.setdefault(h.cause_node, h)
    allowed = allowed_names(report)
    top = max((_LEVEL[h.confidence] for h in report.hypotheses), default=0)

    def text_problems(where: str, text: str, ceiling: int) -> None:
        if m := _CERTAIN.search(text):
            problems.append(f"{where} says {m.group(0)!r}")
        for m in _STATED.finditer(text):
            if _LEVEL[m.group(1).lower()] > ceiling:
                problems.append(f"{where} states {m.group(0).lower()} above the label")
        for m in _EVIDENCE_ID.finditer(text):
            if m.group(0) not in ids:
                problems.append(f"{where} cites {m.group(0)}, which does not exist")
        for m in _TOKEN.finditer(text):
            token = m.group(0).rstrip(".:")
            if token in plant_names and token not in allowed:
                problems.append(f"{where} names {token}, which is not in the report")

    if not draft.overview.strip():
        problems.append("the overview is empty")
    text_problems("the overview", draft.overview, top)
    if len(draft.claims) > MAX_CLAIMS:
        problems.append(f"{len(draft.claims)} claims, more than {MAX_CLAIMS}")
    for i, c in enumerate(draft.claims, start=1):
        where = f"claim {i} ({c.cause})"
        h = by_cause.get(c.cause)
        if h is None:
            problems.append(f"claim {i} names {c.cause}, which is not a hypothesis")
            continue
        if _LEVEL[c.confidence] > _LEVEL[h.confidence]:
            problems.append(f"{where} says {c.confidence} confidence; the label is {h.confidence}")
        if not c.evidence:
            problems.append(f"{where} cites no evidence")
        for e in c.evidence:
            if e not in ids:
                problems.append(f"{where} cites {e}, which does not exist")
        if not c.sentence.strip():
            problems.append(f"{where} is empty")
        text_problems(where, c.sentence, _LEVEL[h.confidence])
    return problems


def write_summary(report: Report, provider: LLMProvider | None, plant_names: set[str]) -> Summary:
    """The template summary, or ``provider``'s draft when it passes :func:`check`."""
    template = template_summary(report)
    if provider is None:
        return template
    prompt = load_prompt(*PROMPT).with_input(summary_input(report))
    meta = {"provider": provider.name, "model": provider.model, "prompt": prompt.id}
    try:
        draft = provider.structured(prompt, SummaryDraft)
    except LLMError as e:
        return template.model_copy(update={**meta, "fallback": str(e)})
    problems = check(draft, report, plant_names)
    if problems:
        return template.model_copy(
            update={**meta, "fallback": "the draft was rejected: " + "; ".join(problems)}
        )
    return Summary(overview=draft.overview, claims=draft.claims, source="llm", **meta)
