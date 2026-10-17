"""Regenerate docs/reference/cli.md from the command registry.

uv run python scripts/gen_cli_reference.py
"""

from pathlib import Path

import penguineer  # noqa: F401  registers the commands
from penguineer.cli.reference import render
from penguineer.core import default_registry

OUT = Path(__file__).resolve().parents[1] / "docs" / "reference" / "cli.md"

if __name__ == "__main__":
    OUT.write_text(render(default_registry))
    print(f"wrote {OUT}")
