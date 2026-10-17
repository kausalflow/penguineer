"""Screenshot the built documentation site, for docs previews and pull requests.

    uvx zensical==0.0.70 build --clean
    uv run --with playwright==1.56.0 python scripts/screenshot_docs.py site

writes one PNG per page to site/_screenshots/ (desktop width, light theme), plus index.html
linking them. Pass page paths to limit it, e.g. ``getting-started/ reference/cli/``.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright


def pages_in(site: Path) -> list[str]:
    paths = []
    for html in sorted(site.rglob("index.html")):
        rel = html.parent.relative_to(site).as_posix()
        if rel.startswith("_screenshots") or rel.startswith("assets"):
            continue
        paths.append("" if rel == "." else rel + "/")
    return paths


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("site", type=Path)
    parser.add_argument("pages", nargs="*", help="page paths relative to the site root")
    parser.add_argument("--width", type=int, default=1280)
    args = parser.parse_args()

    site = args.site.resolve()
    out = site / "_screenshots"
    out.mkdir(exist_ok=True)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"

    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": 900}, color_scheme="light")
        for path in args.pages or pages_in(site):
            page.goto(base + path, wait_until="networkidle")
            name = f"{slug(path)}.png"
            page.screenshot(path=str(out / name), full_page=True)
            shots.append((path, name))
            print(out / name)
        browser.close()
    server.shutdown()

    img = '<img src="{}" style="max-width:100%;border:1px solid #ccc">'
    items = "\n".join(f'<h2><a href="../{p}">/{p}</a></h2>' + img.format(n) for p, n in shots)
    (out / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>Screenshots</title><body>{items}</body>"
    )


if __name__ == "__main__":
    main()
