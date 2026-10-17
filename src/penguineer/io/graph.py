"""Read and write ``graph.yaml`` in the plantgraph schema."""

from __future__ import annotations

from pathlib import Path

import yaml
from plantgraph import PlantGraph
from pydantic import ValidationError

from penguineer.core.errors import UsageError, ValidationFailed


def load_graph(path: Path) -> PlantGraph:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise UsageError(f"{path}: not valid YAML: {e}") from e
    try:
        return PlantGraph.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed(f"{path}: does not match the plantgraph schema\n{e}") from e


def dump_graph(graph: PlantGraph) -> str:
    return yaml.safe_dump(graph.to_data(), sort_keys=False, allow_unicode=True)


def save_graph(graph: PlantGraph, path: Path) -> Path:
    path.write_text(dump_graph(graph))
    return path
