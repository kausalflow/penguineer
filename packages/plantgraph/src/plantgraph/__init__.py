"""plantgraph: the plant context graph schema shared by penguineer and homeostat."""

from importlib.metadata import PackageNotFoundError, version

from plantgraph.model import (
    EDGE_ENDPOINTS,
    SCHEMA_VERSION,
    Actuator,
    Edge,
    EdgeType,
    Equipment,
    Loop,
    Node,
    PlantGraph,
    Provenance,
    Quality,
    SamplePoint,
    Source,
    Stream,
    Tag,
    Unit,
    json_schema,
)

try:
    __version__ = version("plantgraph")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "EDGE_ENDPOINTS",
    "SCHEMA_VERSION",
    "Actuator",
    "Edge",
    "EdgeType",
    "Equipment",
    "Loop",
    "Node",
    "PlantGraph",
    "Provenance",
    "Quality",
    "SamplePoint",
    "Source",
    "Stream",
    "Tag",
    "Unit",
    "json_schema",
]
