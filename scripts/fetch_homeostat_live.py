"""Download a window of a Homeostat Live plant and derive its plant graph.

Example (the first public benchmark week of isidis-01):

    uv run python scripts/fetch_homeostat_live.py --out data/isidis-01 \
        --plant isidis-01 --since 2026-10-02T17:00:00Z --until 2026-10-09T17:00:00Z

writes live.csv, lab.csv, tags.csv and graph.yaml into --out. Then:

    uv run penguineer data health --data data/isidis-01/live.csv --graph data/isidis-01/graph.yaml
"""

import argparse
from datetime import datetime
from pathlib import Path

from penguineer.io.graph import save_graph
from penguineer.io.homeostat_live import fetch_window, graph_from_tags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant", default="isidis-01")
    parser.add_argument("--since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--until", required=True, type=datetime.fromisoformat)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    window = fetch_window(args.plant, args.since, args.until, args.out)
    graph = save_graph(graph_from_tags(window.tags, unit=args.plant), args.out / "graph.yaml")
    for path in (window.sensors, window.lab, window.tags, graph):
        print(path)


if __name__ == "__main__":
    main()
