import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def test_packages_are_released_together():
    """penguineer and plantgraph share one version, and penguineer pins that plantgraph."""
    penguineer = _project(ROOT / "pyproject.toml")
    plantgraph = _project(ROOT / "packages" / "plantgraph" / "pyproject.toml")
    assert penguineer["version"] == plantgraph["version"]
    assert f"plantgraph=={plantgraph['version']}" in penguineer["dependencies"]
