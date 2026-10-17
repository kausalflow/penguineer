from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo"


@pytest.fixture
def demo_csv() -> Path:
    return DEMO / "historian.csv"


@pytest.fixture
def demo_graph() -> Path:
    return DEMO / "graph.yaml"
