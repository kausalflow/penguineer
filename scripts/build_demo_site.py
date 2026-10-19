"""Turn a run of ``scripts/investigate_homeostat_live.py`` into the public demo page.

    uv run python scripts/investigate_homeostat_live.py --out out
    uv run --with markdown==3.7 python scripts/build_demo_site.py out demo-site

writes ``demo-site/index.html`` (the run's summary, with a link to every lot report),
``demo-site/reports/`` (each lot's report.html, report.md, report.json and plots) and
``demo-site/lots.csv``. The Demo workflow publishes it every week to
https://kausalflow.com/penguineer/demo/isidis-01/.
"""

from __future__ import annotations

import argparse
import html
import shutil
from datetime import UTC, datetime
from pathlib import Path

import markdown

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>penguineer live demo: Homeostat {plant}</title>
<style>
:root {{ --fg: #1d1d1f; --muted: #5f6368; --bg: #fff; --panel: #f4f5f7; --line: #d9dce1;
  --link: #1f5fbf; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --fg: #e8eaed; --muted: #a0a4aa; --bg: #16181b; --panel: #22252a; --line: #3a3e45;
    --link: #8ab4f8; }}
}}
body {{ margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1000px; margin: 0 auto; padding: 24px 16px 64px; }}
a {{ color: var(--link); }}
.lead {{ color: var(--muted); }}
.box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 16px; margin: 16px 0; }}
table {{ border-collapse: collapse; display: block; overflow-x: auto; font-size: 14px; }}
th, td {{ border: 1px solid var(--line); padding: 4px 8px; text-align: left; white-space: nowrap; }}
pre {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  padding: 12px; overflow-x: auto; font-size: 13px; }}
</style>
</head>
<body>
<main>
<h1>penguineer live demo</h1>
<p class="lead">Every week, <a href="https://kausalflow.com/penguineer/">penguineer</a> runs
<code>investigate</code> on the off-spec lots of
<a href="https://homeostat.kausalflow.com/live/">Homeostat Live</a> {plant}, a simulated methane
plant that publishes its sensor data, lab results and data dictionary. Nobody edits these
results: this page is what the open-source engine says, right or wrong.</p>
<div class="box">Run {run_at} UTC from commit <a href="{commit_url}"><code>{commit}</code></a>.
Reports: {reports}. All lab samples with their grade: <a href="lots.csv">lots.csv</a>.
How it works: <a href="https://kausalflow.com/penguineer/guides/homeostat-live/">Homeostat Live
guide</a>.</div>
{summary}
</main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="the --out folder of investigate_homeostat_live.py")
    parser.add_argument("site", type=Path, help="folder to write the page into")
    parser.add_argument("--plant", default="isidis-01")
    parser.add_argument("--commit", default="main")
    parser.add_argument("--repo", default="https://github.com/kausalflow/penguineer")
    args = parser.parse_args()

    run: Path = args.run
    site: Path = args.site
    site.mkdir(parents=True, exist_ok=True)
    reports = sorted(run.glob("reports/*/report.html"))
    for r in reports:
        shutil.copytree(r.parent, site / "reports" / r.parent.name, dirs_exist_ok=True)
    if (run / "lots.csv").exists():
        shutil.copy(run / "lots.csv", site / "lots.csv")

    # The summary names report paths on the machine that ran it; make them relative to the page.
    text = (run / "summary.md").read_text()
    for prefix in {f"{run.resolve()}/", f"{run}/"}:
        text = text.replace(prefix, "")
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    body = body.replace("<h1>", "<h2>").replace("</h1>", "</h2>")

    names = [html.escape(r.parent.name) for r in reports]
    links = ", ".join(f'<a href="reports/{n}/report.html">{n}</a>' for n in names)
    (site / "index.html").write_text(
        PAGE.format(
            plant=html.escape(args.plant),
            run_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
            commit=html.escape(args.commit[:7]),
            commit_url=f"{args.repo}/commit/{html.escape(args.commit)}",
            reports=links or "none this week",
            summary=body,
        )
    )
    print(f"wrote {site / 'index.html'} with {len(reports)} reports")


if __name__ == "__main__":
    main()
