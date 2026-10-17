"""Plant context graph: a typed property graph of equipment, streams, tags, loops and qualities.

Every node and edge carries a stable ID, a provenance, a source, a confidence and a
valid-time interval. The schema depends only on Pydantic so that penguineer and homeostat
describe a real plant and a simulated one with the same types.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Provenance(StrEnum):
    declared = "declared"
    inferred = "inferred"
    confirmed = "confirmed"


class Source(StrEnum):
    user = "user"
    llm = "llm"
    data = "data"
    dexpi = "dexpi"
    homeostat = "homeostat"


class _Element(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1, description="Stable ID, unique across nodes and edges")
    provenance: Provenance = Provenance.declared
    source: Source = Source.user
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    valid_from: date | None = None
    valid_to: date | None = None
    description: str | None = None

    def valid_on(self, day: date) -> bool:
        """Whether the element is part of the plant on ``day`` (valid time)."""
        if self.valid_from is not None and day < self.valid_from:
            return False
        return not (self.valid_to is not None and day >= self.valid_to)


class Unit(_Element):
    type: Literal["unit"] = "unit"
    name: str


class Equipment(_Element):
    type: Literal["equipment"] = "equipment"
    name: str | None = None
    kind: str | None = Field(None, description="vessel, exchanger, column, pump, ...")
    holdup_m3: float | None = Field(None, ge=0, description="Holdup volume in m³, if known")


class Stream(_Element):
    type: Literal["stream"] = "stream"
    name: str | None = None
    phase: Literal["liquid", "gas", "solid", "mixed"] | None = None


class Tag(_Element):
    type: Literal["tag"] = "tag"
    tag: str = Field(description="Historian tag name exactly as it appears in the export")
    unit_of_measure: str | None = None
    sample_period_s: float | None = Field(None, gt=0)
    range_low: float | None = Field(None, description="Instrument range, low end")
    range_high: float | None = Field(None, description="Instrument range, high end")


class Loop(_Element):
    type: Literal["loop"] = "loop"
    pv: str | None = Field(None, description="ID of the process value Tag node")
    sp: str | None = Field(None, description="ID of the setpoint Tag node")
    op: str | None = Field(None, description="ID of the controller output Tag node")
    mode: str | None = Field(None, description="ID of the controller mode Tag node")


class Actuator(_Element):
    type: Literal["actuator"] = "actuator"
    kind: str | None = Field(None, description="valve, drive, ...")


class SamplePoint(_Element):
    type: Literal["sample_point"] = "sample_point"
    name: str | None = None


class Quality(_Element):
    type: Literal["quality"] = "quality"
    property: str = Field(description="LIMS property name")
    spec_low: float | None = None
    spec_high: float | None = None
    unit_of_measure: str | None = None


AnyNode = Unit | Equipment | Stream | Tag | Loop | Actuator | SamplePoint | Quality
Node = Annotated[AnyNode, Field(discriminator="type")]
NodeType = Literal[
    "unit", "equipment", "stream", "tag", "loop", "actuator", "sample_point", "quality"
]


class EdgeType(StrEnum):
    flows_to = "flows_to"
    measures = "measures"
    controls = "controls"
    manipulates = "manipulates"
    samples = "samples"
    reported_as = "reported_as"
    affects = "affects"


# Allowed (from, to) node types per edge type. ``None`` means any node type.
EDGE_ENDPOINTS: dict[EdgeType, tuple[frozenset[str] | None, frozenset[str] | None]] = {
    EdgeType.flows_to: (frozenset({"equipment", "stream"}), frozenset({"equipment", "stream"})),
    EdgeType.measures: (frozenset({"tag"}), frozenset({"equipment", "stream"})),
    EdgeType.controls: (frozenset({"loop"}), frozenset({"actuator"})),
    EdgeType.manipulates: (frozenset({"actuator"}), frozenset({"equipment", "stream"})),
    EdgeType.samples: (frozenset({"sample_point"}), frozenset({"equipment", "stream"})),
    EdgeType.reported_as: (frozenset({"sample_point"}), frozenset({"quality"})),
    EdgeType.affects: (None, None),
}


class Edge(_Element):
    type: EdgeType
    from_id: str = Field(alias="from", min_length=1)
    to_id: str = Field(alias="to", min_length=1)
    dead_time_min: float | None = Field(None, ge=0, description="flows_to: dead time in minutes")
    dead_time_low_min: float | None = Field(None, ge=0)
    dead_time_high_min: float | None = Field(None, ge=0)
    recycle: bool = Field(False, description="flows_to: edge closes a recycle loop")
    measured_variable: str | None = Field(None, description="measures: temperature, flow, ...")
    acting: Literal["direct", "reverse"] | None = Field(None, description="controls")


class PlantGraph(BaseModel):
    """A whole plant graph. Structural rules (dangling IDs, cycles) are checked by tools."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    unit: str | None = Field(None, description="Name of the unit this graph describes")
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def node(self, node_id: str) -> AnyNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def nodes_of(self, node_type: NodeType) -> Iterator[AnyNode]:
        return (n for n in self.nodes if n.type == node_type)

    def edges_of(self, edge_type: EdgeType | str) -> Iterator[Edge]:
        return (e for e in self.edges if e.type == edge_type)

    def tag_by_name(self) -> dict[str, Tag]:
        """Tag nodes keyed by historian tag name."""
        return {n.tag: n for n in self.nodes if isinstance(n, Tag)}

    def valid_on(self, day: date) -> PlantGraph:
        """The graph as it was valid on ``day``."""
        return PlantGraph(
            schema_version=self.schema_version,
            unit=self.unit,
            nodes=[n for n in self.nodes if n.valid_on(day)],
            edges=[e for e in self.edges if e.valid_on(day)],
        )

    def to_data(self) -> dict:
        """Plain JSON-compatible dict, with edge endpoints as ``from``/``to``."""
        data = self.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        data["schema_version"] = self.schema_version
        # ``type`` is the discriminator, so it is kept even where it equals the default.
        for key, elements in (("nodes", self.nodes), ("edges", self.edges)):
            data[key] = [
                {"id": el.id, "type": str(el.type), **{k: v for k, v in d.items() if k != "id"}}
                for el, d in zip(elements, data.get(key, []), strict=True)
            ]
        return data


def json_schema() -> dict:
    """JSON Schema of :class:`PlantGraph`, for editors and other languages."""
    return PlantGraph.model_json_schema(by_alias=True)
