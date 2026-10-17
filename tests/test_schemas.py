"""Freeze the public Params/Result schemas so a change that would break callers shows in review.

Regenerate after an intended change: PENGUINEER_UPDATE_SCHEMAS=1 uv run pytest tests/test_schemas.py
"""

import json
import os
from pathlib import Path

import plantgraph

import penguineer

SNAPSHOT = Path(__file__).parent / "snapshots" / "schemas.json"


def current() -> str:
    data = {"commands": penguineer.schemas(), "plantgraph": plantgraph.json_schema()}
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def test_schemas_match_snapshot():
    text = current()
    if os.environ.get("PENGUINEER_UPDATE_SCHEMAS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text)
    assert SNAPSHOT.read_text() == text, "public schemas changed; see the module docstring"


def test_every_command_is_registered():
    assert sorted(penguineer.schemas()) == [
        "bench homeostat",
        "data health",
        "graph check",
        "graph draft",
        "graph lags",
        "init",
        "investigate",
    ]


def test_cli_reference_is_up_to_date():
    from penguineer.cli.reference import render
    from penguineer.core import default_registry

    page = Path(__file__).parents[1] / "docs" / "reference" / "cli.md"
    assert page.read_text() == render(default_registry), (
        "run: uv run python scripts/gen_cli_reference.py"
    )
