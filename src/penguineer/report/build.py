"""Turn an :class:`Investigation` into ``report.json``, ``report.html``, ``report.md`` and
``evidence/``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from jinja2 import Environment, select_autoescape

from penguineer.engines.investigate import Investigation
from penguineer.report import plots
from penguineer.report.model import EvidenceItem, Report, Summary
from penguineer.report.summary import template_summary


def build_report(
    inv: Investigation,
    out_dir: Path,
    *,
    run_id: str,
    version: str,
    summarize: Callable[[Report], Summary] | None = None,
) -> Report:
    """Write the report; ``summarize`` writes its summary (default: the template)."""
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    items: list[EvidenceItem] = []

    def add(item: EvidenceItem) -> EvidenceItem:
        item.id = f"E{len(items) + 1:03d}"
        items.append(item)
        return item

    overview = plots.quality_overview(inv, evidence_dir / "quality.png")
    add(
        EvidenceItem(
            id="",
            kind="plot",
            title=f"{inv.event.property}: lot {inv.event.lot} against the reference lots",
            path=overview.relative_to(out_dir).as_posix(),
        )
    )
    by_title = {}
    for item in inv.evidence:
        by_title[item.title] = add(item.model_copy())
    for h in inv.hypotheses:
        h.evidence = [by_title[t].id if t in by_title else t for t in h.evidence]
        safe = (h.cause_tag or h.cause_node).replace("/", "_").replace(" ", "_")
        png = plots.hypothesis_trend(inv, h, evidence_dir / f"{h.rank:02d}-{safe}.png")
        if png is not None:
            item = add(
                EvidenceItem(
                    id="",
                    kind="plot",
                    title=f"#{h.rank} {h.cause_tag} shifted {h.dead_time_min:g} min",
                    path=png.relative_to(out_dir).as_posix(),
                )
            )
            h.evidence.insert(0, item.id)

    report = Report(
        penguineer_version=version,
        run_id=run_id,
        created_at=datetime.now(UTC).replace(microsecond=0),
        event=inv.event,
        regime=inv.regime,
        reference=inv.reference,
        grid_step_s=inv.grid.step_us / 1_000_000,
        candidates=inv.candidates,
        hypotheses=inv.hypotheses,
        ruled_out=inv.ruled_out,
        evidence=items,
        health=inv.health,
        notes=inv.notes,
        calibration=inv.calibration.describe(),
    )
    report.summary = (summarize or template_summary)(report)
    (out_dir / "report.json").write_text(report.model_dump_json(indent=2))
    render(report, out_dir)
    return report


def _env() -> Environment:
    env = Environment(autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["num"] = lambda v: (
        "" if v is None else (f"{v:.3g}" if abs(v) < 1000 else f"{v:.0f}")
    )
    env.filters["when"] = lambda t: "" if t is None else f"{t:%Y-%m-%d %H:%M}"
    return env


def _template(name: str) -> str:
    return resources.files("penguineer.report").joinpath("templates", name).read_text()


def render(report: Report, out_dir: Path) -> tuple[Path, Path]:
    """Write report.html and report.md from the report model."""
    env = _env()
    evidence = {e.id: e for e in report.evidence}
    html = env.from_string(_template("report.html.j2")).render(r=report, evidence=evidence)
    md_env = _env()
    md_env.autoescape = False
    md = md_env.from_string(_template("report.md.j2")).render(r=report, evidence=evidence)
    (out_dir / "report.html").write_text(html)
    (out_dir / "report.md").write_text(md)
    return out_dir / "report.html", out_dir / "report.md"
